import base64
import re
import shutil
import subprocess
import xml.etree.ElementTree as ET
from pathlib import Path

import cv2
import numpy as np
import pytest

from keepframe.assets import AssetAPIError
from keepframe.edit.agent import edit
from keepframe.edit.apply import apply_edit
from keepframe.edit.intent import Target, interpret
from keepframe.ir.schema import Background, Canonical, Element, Keyframe, Scene, Track
from keepframe.ir.store import current_scene, init_project, load_project
from keepframe.session.tools import SessionContext, run_tool
from tests.test_three import _triangle_glb

STATIC = Path(__file__).resolve().parents[1] / "keepframe/web/static"
SVG = b'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 100 100"><circle cx="50" cy="50" r="30" fill="red"/></svg>'


def _url(mime, data):
    return f"data:{mime};base64," + base64.b64encode(data).decode("ascii")


@pytest.fixture(params=[
    ("texture", "data:image/svg+xml;base64,%%%", "invalid_attachment"),
    ("model", "data:model/gltf-binary;base64,%%%", "invalid_attachment"),
    ("texture", "data:image/png;base64,A", "invalid_attachment"),
    ("texture", "data:image/svg+xml;base64,é", "invalid_attachment"),
    ("texture", "data:image/svg+xml;base64,", "invalid_attachment"),
    ("texture", "data:image/svg+xml,<svg/>", "invalid_attachment"),
    ("texture", _url("image/svg+xml", b"<svg>"), "invalid_attachment"),
    ("texture", _url("image/svg+xml", b'<?xml version="1.0" encoding="unknown"?><svg/>'), "invalid_attachment"),
    ("model", _url("model/gltf-binary", b"not a glb"), "invalid_glb"),
    ("model", _url("model/gltf-binary", _triangle_glb()[:20] + b"\xff" + _triangle_glb()[21:]), "invalid_glb"),
    ("texture", Path("missing.svg"), "invalid_attachment"),
    ("model", Path("missing.glb"), "invalid_attachment"),
], ids=["svg-base64", "glb-base64", "base64-padding", "non-ascii-base64", "empty-data-url",
        "non-base64-url", "broken-svg", "unknown-svg-encoding", "broken-glb", "undecodable-glb",
        "missing-svg", "missing-glb"])
def invalid_attachment_case(request, tmp_path):
    property, attachment, error = request.param
    if isinstance(attachment, Path):
        attachment = str(tmp_path / attachment)
    return property, attachment, error


def _scene():
    return Scene(id="s1", size=(200, 100), fps=30, frames=3, background=Background(), elements=[
        Element(id="e1", kind="sprite", canonical=Canonical(width=40, height=20), visible=(0, 2),
                tracks={"x": Track(keys=[Keyframe(t=0, v=100)]), "y": Track(keys=[Keyframe(t=0, v=50)])}),
    ])


def _project(tmp_path):
    root = tmp_path / "ws/p1"
    scene = _scene()
    init_project(root, {"file": "ref.mp4", "fps": 30, "size": [200, 100], "mode": "range", "range": [0, 2]}, scene)
    return root, scene


@pytest.mark.browser
def test_svg_data_url_becomes_transparent_png_inside_original_box(tmp_path):
    scene = _scene()
    # Replacements continue to use the original crop box, as raster attachments do.
    (tmp_path / "assets").mkdir()
    cv2.imwrite(str(tmp_path / "assets/e1.png"), np.zeros((20, 40, 4), np.uint8))
    scene.element("e1").canonical.width = 80
    out = apply_edit(scene, tmp_path, [Target(element="e1", property="texture", value="attachment")], {}, _url("image/svg+xml", SVG))
    el = out.element("e1")
    texture = tmp_path / el.canonical.texture
    assert texture.suffix == ".png" and texture.read_bytes().startswith(b"\x89PNG\r\n\x1a\n")
    img = cv2.imread(str(texture), cv2.IMREAD_UNCHANGED)
    assert img.shape == (40, 80, 4)
    assert img[..., 3].min() == 0 and img[..., 3].max() == 255
    assert el.canonical.width <= 40 and el.canonical.height <= 20
    assert out.element("e1").tracks == scene.element("e1").tracks
    assert scene.element("e1").canonical.texture is None


@pytest.mark.browser
@pytest.mark.parametrize("width,height,shape", [
    (2048, 1024, (2048, 4096, 4)),
    (3072, 768, (1024, 4096, 4)),
    (768, 3072, (4096, 1024, 4)),
    (2304, 3072, (4096, 3072, 4)),
], ids=["at-cap", "wide", "tall", "both-over-cap"])
def test_svg_rasterizer_caps_resolution_and_preserves_box_aspect(width, height, shape):
    from keepframe.edit.svgraster import rasterize_svg

    data = rasterize_svg(SVG, width, height)
    img = cv2.imdecode(np.frombuffer(data, np.uint8), cv2.IMREAD_UNCHANGED)
    assert img.shape == shape
    assert img[..., 3].min() == 0 and img[..., 3].max() == 255


@pytest.mark.browser
@pytest.mark.parametrize("extra", [b"", b"<script>fetch('https://example.invalid/script')</script>",
    b'<image href="https://example.invalid/image.png"/><use xmlns:xlink="http://www.w3.org/1999/xlink" xlink:href="https://example.invalid/vector.svg"/>'],
    ids=["safe", "script", "external-href"])
def test_svg_rasterizer_strips_unsafe_content_and_blocks_network(monkeypatch, extra):
    from playwright.sync_api import Browser, Page
    from keepframe.edit.svgraster import rasterize_svg

    options, routes, requests, contents = [], [], [], []
    new_page, route, set_content = Browser.new_page, Page.route, Page.set_content

    def capture_page(self, *args, **kwargs):
        options.append(kwargs)
        page = new_page(self, *args, **kwargs)
        page.on("request", lambda request: requests.append(request.url))
        return page

    def capture_route(self, pattern, handler, **kwargs):
        routes.append(pattern)
        return route(self, pattern, handler, **kwargs)

    def capture_content(self, html, **kwargs):
        contents.append(html)
        return set_content(self, html, **kwargs)

    monkeypatch.setattr(Browser, "new_page", capture_page)
    monkeypatch.setattr(Page, "route", capture_route)
    monkeypatch.setattr(Page, "set_content", capture_content)
    data = rasterize_svg(SVG.replace(b"</svg>", extra + b"</svg>"), 40, 20)
    img = cv2.imdecode(np.frombuffer(data, np.uint8), cv2.IMREAD_UNCHANGED)
    assert img.shape == (40, 80, 4) and img[..., 3].max() == 255
    encoded = re.search(r'data:image/svg\+xml;base64,([^"\s]+)', contents[0]).group(1)
    cleaned = ET.fromstring(base64.b64decode(encoded))
    assert not any(e.tag.rsplit("}", 1)[-1] == "script" for e in cleaned.iter())
    assert b"example.invalid" not in base64.b64decode(encoded)
    assert options[0]["java_script_enabled"] is False
    assert options[0]["viewport"] == {"width": 80, "height": 40}
    assert routes == ["**/*"]
    assert requests == []


@pytest.mark.parametrize("mime", ["model/gltf-binary", "application/octet-stream"])
def test_glb_data_url_sets_model_and_kind(tmp_path, mime):
    data = _triangle_glb()
    scene = _scene()
    out = apply_edit(scene, tmp_path, [Target(element="e1", property="model", value="attachment")], {}, _url(mime, data))
    el = out.element("e1")
    assert el.kind == "3d" and el.canonical.model.endswith(".glb")
    assert (tmp_path / el.canonical.model).read_bytes() == data
    assert el.tracks == scene.element("e1").tracks
    assert scene.element("e1").kind == "sprite"


def test_invalid_glb_fails_before_writing_model(tmp_path):
    with pytest.raises(AssetAPIError, match="invalid_glb"):
        apply_edit(_scene(), tmp_path, [Target(element="e1", property="model", value="attachment")], {}, _url("model/gltf-binary", b"not a glb"))
    assert not list(tmp_path.rglob("*.glb"))


def test_model_attachment_intent_and_tool_only_preview(tmp_path, monkeypatch):
    root, scene = _project(tmp_path)
    parsed = interpret("e1 3D 모델 교체", scene, has_attachment=True)
    assert parsed.targets == [Target(element="e1", property="model", value="attachment")]
    assert "첨부" in parsed.summary and "생성" not in parsed.summary
    monkeypatch.setattr("keepframe.edit.agent.AssetClient", lambda *a, **k: pytest.fail("must not generate"))
    result = run_tool("edit", SessionContext(root, "s1", has_attachment=True),
                      {"prompt": "e1 3D 모델 교체", "confirm": True})
    assert result["ok"] and result["needs_confirm"]
    assert result["payload"]["intent"]["targets"][0]["value"] == "attachment"
    assert load_project(root).versions[0].id == "v1" and len(load_project(root).versions) == 1
    assert current_scene(root, "s1")[0] == scene


def test_confirm_model_without_attachment_does_not_generate(tmp_path, monkeypatch):
    root, _ = _project(tmp_path)
    monkeypatch.setattr("keepframe.edit.agent.AssetClient", lambda *a, **k: pytest.fail("must not generate"))
    res = edit(root, "s1", "3D 교체", confirm=True,
               intent={"targets": [{"element": "e1", "property": "model", "value": "attachment"}]})
    assert res.status == "failed" and res.error == "attachment_required" and res.attempts == 0


def test_confirm_invalid_glb_returns_failure_message(tmp_path):
    root, scene = _project(tmp_path)
    res = edit(root, "s1", "3D 교체", confirm=True, attachment=_url("model/gltf-binary", b"bad"),
               intent={"targets": [{"element": "e1", "property": "model", "value": "attachment"}]})
    assert res.status == "failed" and res.error == "invalid_glb" and res.attempts == 0
    assert current_scene(root, "s1")[0] == scene
    assert len(load_project(root).versions) == 1


def test_confirm_invalid_attachment_returns_failure_without_version(tmp_path, invalid_attachment_case):
    property, attachment, error = invalid_attachment_case
    root, scene = _project(tmp_path)
    res = edit(root, "s1", "e1 첨부로 교체", confirm=True, attachment=attachment,
               intent={"targets": [{"element": "e1", "property": property, "value": "attachment"}]})
    assert res.status == "failed" and res.error == error and res.attempts == 0
    assert res.version is None
    assert current_scene(root, "s1")[0] == scene
    assert [v.id for v in load_project(root).versions] == ["v1"]
    assert not list((root / "scenes/s1/assets").glob("*"))


def test_chat_accepts_svg_and_glb_and_localizes_attachment_errors():
    html = (STATIC / "agent.html").read_text()
    accept = re.search(r'id="agent-attach" accept="([^"]+)"', html).group(1).split(",")
    assert {"image/png", "image/jpeg", "image/svg+xml", ".svg", "model/gltf-binary", ".glb"} <= set(accept)
    js = (STATIC / "js/agent.js").read_text()
    assert 'T("agent.invalidGlb")' in js
    i18n = (STATIC / "js/i18n.js").read_text()
    assert i18n.count('"agent.invalidGlb":') == 2


def test_chat_file_reader_normalizes_unknown_svg_and_glb_mime(tmp_path):
    node = shutil.which("node")
    if not node:
        pytest.skip("node unavailable")
    source = (STATIC / "js/agent.js").read_text()
    assert "async function readChatAttachment(" in source
    helper = source[source.index("async function readChatAttachment("):source.index("\nfunction attachmentMeta(")]
    script = tmp_path / "attachments.mjs"
    script.write_text("import assert from 'node:assert/strict';\n" +
                      "const readFileAsDataUrl = async file => file.url;\n" + helper + "\n" + """
for (const [name, type, mime] of [['MODEL.GLB', '', 'model/gltf-binary'],
    ['model.glb', 'application/octet-stream', 'model/gltf-binary'],
    ['VECTOR.SVG', '', 'image/svg+xml'], ['photo.png', 'image/png', 'image/png']]) {
  const file = {name, type, url:`data:${type || 'application/octet-stream'};base64,Zm9v`};
  assert.equal(await readChatAttachment(file), `data:${mime};base64,Zm9v`);
}
""")
    result = subprocess.run([node, str(script)], text=True, capture_output=True)
    assert result.returncode == 0, result.stdout + result.stderr


@pytest.mark.browser
@pytest.mark.parametrize("property,mime,data", [("texture", "image/svg+xml", SVG), ("model", "model/gltf-binary", _triangle_glb())])
def test_chat_preview_then_browser_confirmation_applies_attachment(tmp_path, monkeypatch, property, mime, data):
    from keepframe.session.llm import AssistantReply
    from tests.test_session_agent import StubLLM
    from tests.test_web_edit import _post
    from tests.test_web_server import start

    root, scene = _project(tmp_path)
    llm = StubLLM([AssistantReply(tool_calls=[{"id": "c1", "name": "edit", "arguments": {
        "prompt": "e1 첨부로 교체", "confirm": True,
        "targets": [{"element": "e1", "property": property, "value": "attachment"}],
    }}]), AssistantReply(content="Confirm the attachment")])
    monkeypatch.setattr("keepframe.web.server.make_llm", lambda *_: llm)
    srv = start(tmp_path / "ws")
    try:
        code, preview = _post(srv, "/api/agent", {"project": "p1", "scene": "s1", "message": "e1 첨부로 교체",
            "ui_context": {"schema": "keepframe.ui-context/1", "summary": {"attachment": {"name": "asset"}}}})
        assert code == 200 and preview["needs_confirm"]
        assert current_scene(root, "s1")[0] == scene and len(load_project(root).versions) == 1
        code, done = _post(srv, "/api/edit", {"project": "p1", "scene": "s1", "v": "v1",
            "prompt": "e1 첨부로 교체", "confirm": True, "attachment": _url(mime, data),
            "intent": preview["results"][0]["payload"]["intent"], "choices": {}})
        assert code == 200 and done["status"] == "done", done
        assert done["version"]["id"] == "v2"
        el = current_scene(root, "s1")[0].element("e1")
        assert el.canonical.texture if property == "texture" else el.canonical.model
    finally:
        srv.shutdown()
        srv.server_close()
