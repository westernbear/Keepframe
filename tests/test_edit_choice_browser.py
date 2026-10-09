"""Task 12 on the agent page: a background colour request on a picture background offers tint / replace / cancel,
and cancel applies nothing (no version, no asset)."""
import hashlib

import cv2
import numpy as np
import pytest

from keepframe.ir.schema import Background, Canonical, Element, Keyframe, Scene, Track
from keepframe.ir.store import current_scene, init_project, load_project, scene_dir
from keepframe.session.agent import SessionTurn
from keepframe.session.store import append_turn
from keepframe.session.tools import SessionContext, run_tool
from tests.test_web_server import start

pytestmark = pytest.mark.browser
PROMPT = "make the background navy"


def _seed(workspace):
    root = workspace / "p1"
    sd = root / "scenes" / "s1"
    (sd / "assets").mkdir(parents=True)
    ys, xs = np.mgrid[0:270, 0:480]
    plate = np.dstack([40 + xs * 0.3, 80 + ys * 0.4, 160 + 0 * xs]).clip(0, 255).astype(np.uint8)
    cv2.imwrite(str(sd / "assets" / "background.png"), plate[..., ::-1])
    el = Element(id="e1", kind="sprite", canonical=Canonical(width=40, height=30, color="#ffffff"), visible=(0, 5),
                 tracks={"x": Track(keys=[Keyframe(t=0, v=200)]), "y": Track(keys=[Keyframe(t=0, v=120)])})
    scene = Scene(id="s1", size=(480, 270), fps=30, frames=6, elements=[el],
                  background=Background(kind="image", value="assets/background.png", confidence=0.8))
    init_project(root, {"file": "source.mp4"}, scene)
    (sd / "stages").mkdir()
    np.save(sd / "stages" / "frames.npy", np.stack([plate] * scene.frames))
    args = {"prompt": PROMPT, "targets": [{"property": "background", "value": "#1a2a6c"}]}
    result = run_tool("edit", SessionContext(root, "s1"), args)
    assert result["ok"] and result["needs_confirm"]
    append_turn(root, "s1", PROMPT, SessionTurn(reply=result["message"], status="pending", needs_confirm=True,
                                                 tool_calls=[{"name": "edit", "arguments": args}], results=[result]))
    return root


def _assets(root):
    return {p.name: hashlib.sha256(p.read_bytes()).hexdigest() for p in sorted((scene_dir(root, "s1") / "assets").iterdir())}


def test_agent_shows_three_choices_and_cancel_does_not_apply(tmp_path, monkeypatch):
    """Three states, worded for what happened: previewed ("확인 필요", the summary asks how), cancelled (nothing
    applied, "취소됨"), tinted (a new version, "Tinted the background …")."""
    from playwright.sync_api import expect, sync_playwright
    from keepframe.render.renderer import RenderResult
    from keepframe.verify.verifier import VerifyReport
    monkeypatch.setattr("keepframe.edit.agent.render", lambda html, scene, out_dir: RenderResult(
        frames_dir=out_dir / "frames", frames=list(range(scene.frames)), hashes=[], bboxes={}))
    monkeypatch.setattr("keepframe.edit.agent.verify", lambda *_a, **_k: VerifyReport(
        schema_ok=True, keep_pass_rate=1, temporal=1, layer_probe_complete=True, passed=True))
    root = _seed(tmp_path)
    before = _assets(root)
    original = (scene_dir(root, "s1") / "assets" / "background.png").read_bytes()
    server = start(tmp_path)
    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(args=["--no-sandbox"])
            page = browser.new_page(viewport={"width": 1440, "height": 900})
            page.set_default_timeout(10000)
            errors = []
            page.on("pageerror", lambda error: errors.append(str(error)))
            page.goto(f"http://127.0.0.1:{server.server_address[1]}/agent?project=p1&scene=s1")
            status, said = page.locator(".toolcall .toolcall__status"), page.locator(".msg--agent .msg__body")
            expect(status).to_have_text("확인 필요")                       # previewed, nothing applied yet
            expect(said.last).to_have_text("배경에 #1a2a6c 적용 — 방식을 고르세요. 트랙은 유지합니다.")
            page.locator("#agent-log button.btn--primary").last.click()          # confirm the previewed edit
            choice = page.locator(".agent-choice")
            expect(choice).to_have_count(1)
            expect(choice.locator(".agent-choice__reason")).to_have_text(
                "배경이 그림이나 그라데이션입니다. 색만 입힐까요, 단색으로 바꿀까요?")
            expect(choice.locator("label")).to_have_text(["색 입히기 (밝고 어두운 결 유지)", "단색으로 바꾸기", "취소"])
            expect(choice.locator("input[type=radio]")).to_have_count(3)
            expect(choice.locator("input[value=tint]")).to_be_checked()
            choice.locator("input[value=cancel]").check()
            choice.get_by_role("button", name="선택").click()
            expect(said.last).to_have_text("취소했습니다. 바뀐 것은 없습니다.")
            expect(status).to_have_text("취소됨")
            expect(page.get_by_text("편집 적용됨")).to_have_count(0)
            expect(choice.get_by_role("button", name="선택")).to_be_disabled()
            expect(page.locator("#agent-banner")).to_be_hidden()
            assert [v.id for v in load_project(root).versions] == ["v1"] and _assets(root) == before

            page.locator("[data-lang-toggle]").click()
            page.reload()
            expect(status).to_have_text("Needs confirmation")
            page.locator("#agent-log button.btn--primary").last.click()
            choice = page.locator(".agent-choice")
            expect(choice.locator(".agent-choice__reason")).to_have_text(
                "The background is a picture or gradient. Tint it, or replace it with a flat colour?")
            expect(choice.locator("label")).to_have_text(["Tint (keep its light and dark)", "Replace with a flat colour",
                                                          "Cancel"])
            choice.get_by_role("button", name="Choose").click()               # tint, the default
            expect(said.last).to_have_text("Tinted the background #1a2a6c; its light and dark are kept.")
            expect(status).to_have_text("Edit applied")
            assert [v.id for v in load_project(root).versions] == ["v1", "v2"]
            bg = current_scene(root, "s1")[0].background
            assert (bg.kind, bg.value) == ("image", "assets/background.tint1.png")
            assert (scene_dir(root, "s1") / "assets" / "background.png").read_bytes() == original
            assert not errors
            browser.close()
    finally:
        server.shutdown()
        server.server_close()
