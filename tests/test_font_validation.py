import json
import logging
import subprocess
import sys
import time

import pytest
from pydantic import ValidationError

from keepframe.edit.intent import Target
from keepframe.ir import schema
from keepframe.ir.schema import Background, Canonical, Element, FontGuess, Scene
from keepframe.ir.store import current_scene, init_project, load_project, load_scene, scene_dir
from keepframe.jobs import JobStore, ThreadRunner
from keepframe.session.agent import SessionTurn
from keepframe.session.llm import NullClient
from tests.test_web_agent import _post
from tests.test_web_server import get, start


INVALID_INPUT_FAMILIES = ["x;color:red", "Noto_Sans", "x" * 65, "", "   ", None, 123]


@pytest.mark.parametrize("family", ["sans-serif", "Noto Sans CJK KR", "맑은 고딕", "Font-123", "x", "x" * 64])
def test_validate_font_family_accepts_and_strips_safe_names(family):
    assert schema.validate_font_family(f" \t{family}\n ") == family
    assert schema.FONT_FAMILY_RE.fullmatch(family)


@pytest.mark.parametrize("family", [
    *INVALID_INPUT_FAMILIES, "x,y", "Noto.Sans", "x\ny", "x\\y", "x'", 'x"', [], {}, True,
])
def test_validate_font_family_rejects_invalid_values(family):
    with pytest.raises(ValueError, match="font family"):
        schema.validate_font_family(family)


def test_target_still_rejects_underscore_and_strips_valid_family():
    with pytest.raises(ValidationError, match="font family"):
        Target(property="font", value="Noto_Sans")
    assert Target(property="font", value="  Noto Sans  ").value == "Noto Sans"
    assert Target(property="font", weight=700).value is None
    assert Target(property="font", value="   ", weight=700).value is None


@pytest.fixture
def font_project(tmp_path):
    root = tmp_path / "ws" / "p1"
    scene = Scene(
        id="s1", size=(300, 100), fps=30, frames=5, background=Background(),
        elements=[Element(
            id="e1", kind="text", visible=(0, 4),
            canonical=Canonical(width=200, height=40, text="Sale", font=FontGuess()),
        )],
    )
    init_project(root, {"file": "ref.mp4"}, scene)
    (root / "meta.json").write_text(json.dumps({"id": "p1", "status": "review"}))
    return root, scene


def _scene_files(root):
    return {path.name: path.read_bytes() for path in scene_dir(root, "s1").glob("scene.*.json")}


def _assert_unchanged(root, scene, project_before, scenes_before):
    assert (root / "project.json").read_bytes() == project_before
    assert _scene_files(root) == scenes_before
    assert len(load_project(root).versions) == 1
    assert current_scene(root, "s1")[0] == scene


def _cli_correct(root, args):
    return subprocess.run(
        [sys.executable, "-m", "keepframe.cli", "correct", "--root", str(root),
         "--scene", "s1", "--op", "text", "--args", json.dumps(args)],
        capture_output=True, text=True, timeout=15,
    )


@pytest.mark.parametrize("family", INVALID_INPUT_FAMILIES)
def test_cli_correct_rejects_invalid_family_without_new_version(font_project, family):
    root, scene = font_project
    project_before, scenes_before = (root / "project.json").read_bytes(), _scene_files(root)
    result = _cli_correct(root, {"element_id": "e1", "text": "Changed", "font": {"family_guess": family}})
    assert result.returncode == 2
    assert "font family" in result.stderr
    assert "Traceback" not in result.stderr
    _assert_unchanged(root, scene, project_before, scenes_before)


@pytest.mark.parametrize("family", INVALID_INPUT_FAMILIES)
def test_api_correct_rejects_invalid_family_without_new_version(font_project, family):
    root, scene = font_project
    project_before, scenes_before = (root / "project.json").read_bytes(), _scene_files(root)
    srv = start(root.parent)
    try:
        code, body = _post(srv, "/api/correct", {
            "project": "p1", "scene": "s1", "op": "text",
            "args": {"element_id": "e1", "text": "Changed", "font": {"family_guess": family}},
        })
    finally:
        srv.shutdown()
        srv.server_close()
    assert code == 400
    assert "font family" in body["error"]
    _assert_unchanged(root, scene, project_before, scenes_before)


def _wait_for_job(store, job_id):
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        job = store.get(job_id)
        if job.status in {"done", "error"}:
            return job
        time.sleep(0.01)
    pytest.fail("correction job did not finish")


def _agent_correction(root, monkeypatch, args):
    store = JobStore(runner=ThreadRunner())
    monkeypatch.setattr("keepframe.web.server.JOBS", store)
    monkeypatch.setattr("keepframe.web.server.make_llm", lambda *_: NullClient())

    def submit_correction(_self, ctx, _message, _history):
        # Exercise the retained queue callback directly; the correct tool is preview-only.
        job = ctx.submit_job("correct", {"op": "text", "args": args}, "correct")
        return SessionTurn(reply="submitted", results=[{"payload": {"job": job}}])

    monkeypatch.setattr("keepframe.web.server.SessionAgent.turn", submit_correction)
    srv = start(root.parent)
    try:
        code, body = _post(srv, "/api/agent", {"project": "p1", "scene": "s1", "message": "correct"})
        assert code == 200, body
        job = _wait_for_job(store, body["results"][0]["payload"]["job"]["id"])
        code, _, payload = get(srv, f"/api/jobs/{job.id}")
        assert code == 200
        return json.loads(payload)
    finally:
        srv.shutdown()
        srv.server_close()


@pytest.mark.parametrize("family", INVALID_INPUT_FAMILIES)
def test_agent_correction_job_fails_invalid_family_without_new_version(font_project, monkeypatch, family):
    root, scene = font_project
    project_before, scenes_before = (root / "project.json").read_bytes(), _scene_files(root)
    job = _agent_correction(root, monkeypatch, {
        "element_id": "e1", "text": "Changed", "font": {"family_guess": family},
    })
    assert job["status"] == "error"
    assert "font family" in job["error"]
    assert job["result"] is None
    _assert_unchanged(root, scene, project_before, scenes_before)


@pytest.mark.parametrize("path", ["cli", "api", "agent_job"])
@pytest.mark.parametrize("font, expected", [
    ({"family_guess": " \tNoto Sans\n ", "weight": 700}, FontGuess(family_guess="Noto Sans", weight=700)),
    ({"weight": 700}, FontGuess(weight=700)),
    (None, FontGuess()),
])
def test_correction_paths_accept_valid_partial_and_text_only_input(font_project, monkeypatch, path, font, expected):
    root, _ = font_project
    args = {"element_id": "e1", "text": "Changed"}
    if font is not None:
        args["font"] = font
    if path == "cli":
        result = _cli_correct(root, args)
        assert result.returncode == 0, result.stderr
    elif path == "agent_job":
        job = _agent_correction(root, monkeypatch, args)
        assert job["status"] == "done", job["error"]
        assert job["result"]["version"]["id"] == "v2"
    else:
        srv = start(root.parent)
        try:
            code, body = _post(srv, "/api/correct", {"project": "p1", "scene": "s1", "op": "text", "args": args})
            assert code == 202, body
            deadline = time.monotonic() + 5
            while time.monotonic() < deadline:
                state = json.loads(get(srv, "/api/state?project=p1&scene=s1")[2])
                if state["job"]["status"] in {"done", "error"}:
                    break
                time.sleep(0.01)
            assert state["job"]["status"] == "done", state["job"]
        finally:
            srv.shutdown()
            srv.server_close()
    scene, version = current_scene(root, "s1")
    assert version.id == "v2"
    assert len(load_project(root).versions) == 2
    assert scene.element("e1").canonical.text == "Changed"
    assert scene.element("e1").canonical.font == expected


@pytest.mark.parametrize("family", ["x;color:red", "Noto_Sans", None, "", "x" * 65])
def test_loading_stored_scene_keeps_tolerant_fallback_and_warning(font_project, monkeypatch, caplog, family):
    root, scene = font_project
    data = scene.model_dump(by_alias=True)
    data["elements"][0]["canonical"]["font"]["family_guess"] = family
    path = scene_dir(root, "s1") / "scene.v1.json"
    path.write_text(json.dumps(data))
    before = path.read_bytes()
    monkeypatch.setattr(schema, "get", logging.getLogger)
    with caplog.at_level(logging.WARNING, logger="keepframe.ir"):
        loaded = load_scene(path)
    assert loaded.element("e1").canonical.font.family_guess == "sans-serif"
    assert "invalid stored font family; using sans-serif" in caplog.text
    assert path.read_bytes() == before
    assert len(load_project(root).versions) == 1
