import base64
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import cv2
import numpy as np
import pytest

from keepframe.assets import AssetAPIError, AssetClient, sanitize_svg, validate_glb
from keepframe.assets import AssetResponse
from keepframe.assets.client import MAX_2D
from keepframe.analyze.pipeline import _parse_ui
from keepframe.ir.schema import Background, Scene, UIComponent, UIModel
from keepframe.session.agent import SessionAgent, SessionTurn
from keepframe.session.store import append_turn, llm_history, page
from keepframe.session.ui_context import MAX_IMAGE_BYTES, UIContextError, multimodal_content, validate_ui_context
from tests.test_three import _triangle_glb


def test_asset_api_contract_reencodes_raster_and_rejects_unsafe_payloads():
    captured = {}
    source = np.full((8, 9, 3), (12, 34, 56), np.uint8)
    ok, png = cv2.imencode(".png", source)
    assert ok

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            captured.update(json.loads(self.rfile.read(int(self.headers["Content-Length"]))))
            self.send_response(200)
            self.send_header("Content-Type", "image/png")
            self.end_headers()
            self.wfile.write(png.tobytes())

        def log_message(self, *_args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        result = AssetClient(f"http://127.0.0.1:{server.server_address[1]}").request(task="generate", kind="raster", prompt="icon")
    finally:
        server.shutdown()
    assert captured["schema"] == "keepframe.asset.request/1"
    assert captured["task"] == "generate" and captured["kind"] == "raster"
    assert cv2.imdecode(np.frombuffer(result.data, np.uint8), cv2.IMREAD_UNCHANGED).shape[:2] == (8, 9)
    with pytest.raises(AssetAPIError, match="unsafe_svg"):
        sanitize_svg(b'<svg xmlns="http://www.w3.org/2000/svg"><script>alert(1)</script></svg>')
    assert validate_glb(_triangle_glb()).startswith(b"glTF")


def test_asset_api_rejects_unsafe_url_redirect_oversize_and_mime():
    with pytest.raises(AssetAPIError) as invalid:
        AssetClient("http://example.com")
    assert invalid.value.code == "asset_api_url_invalid"

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            if self.path == "/redirect":
                self.send_response(302)
                self.send_header("Location", "/mime")
                self.end_headers()
            elif self.path == "/large":
                self.send_response(200)
                self.send_header("Content-Type", "image/png")
                self.send_header("Content-Length", str(MAX_2D + 1))
                self.end_headers()
            else:
                self.send_response(200)
                self.send_header("Content-Type", "text/plain")
                self.end_headers()
                self.wfile.write(b"not an image")

        def log_message(self, *_args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{server.server_address[1]}"
    try:
        for path, error in (
            ("/redirect", "asset_api_redirect"),
            ("/large", "asset_too_large"),
            ("/mime", "asset_mime_not_allowed"),
        ):
            with pytest.raises(AssetAPIError) as caught:
                AssetClient(base + path).request(task="generate", kind="raster", prompt="icon")
            assert caught.value.code == error
    finally:
        server.shutdown()


def test_session_pagination_history_and_ui_context_are_ephemeral(tmp_path):
    root = tmp_path / "project"
    root.mkdir()
    for index in range(5):
        append_turn(root, "s1", f"user-{index}", SessionTurn(reply=f"reply-{index}", status="pending" if index < 2 else "done"))
    last = page(root, "s1", limit=2)
    assert [record["user"] for record in last["turns"]] == ["user-3", "user-4"]
    previous = page(root, "s1", before=last["next_before"], limit=2)
    assert [record["user"] for record in previous["turns"]] == ["user-1", "user-2"]
    assert len(llm_history(root, "s1")) == 10

    jpeg = b"\xff\xd8\xff\xd9"
    context = validate_ui_context({
        "schema": "keepframe.ui-context/1",
        "summary": {"route": "/agent", "password": "secret", "relay_url": "https://private"},
        "images": [{"mime": "image/jpeg", "data": base64.b64encode(jpeg).decode()}],
    })
    assert context is not None and "secret" not in context.summary and "private" not in context.summary
    assert multimodal_content("hello", context)[1]["type"] == "image_url"
    transcript = (root / "sessions" / "s1.jsonl").read_text(encoding="utf-8")
    assert "data:image" not in transcript and "secret" not in transcript


def test_only_latest_pending_session_turn_is_actionable(tmp_path):
    root = tmp_path / "project"
    root.mkdir()
    append_turn(root, "s1", "first", SessionTurn(status="pending", needs_confirm=True))
    append_turn(root, "s1", "done", SessionTurn(status="done"))
    append_turn(root, "s1", "latest", SessionTurn(status="pending", needs_choice=True))

    turns = page(root, "s1")["turns"]
    assert [(turn["actionable"], turn["read_only"]) for turn in turns] == [
        (False, True),
        (False, False),
        (True, False),
    ]


def test_llm_history_obeys_turn_and_byte_caps(tmp_path):
    count_root = tmp_path / "count"
    count_root.mkdir()
    for index in range(21):
        append_turn(count_root, "s1", f"user-{index}", SessionTurn(reply=f"reply-{index}"))
    count_history = llm_history(count_root, "s1")
    assert len(count_history) == 40
    assert count_history[0]["content"] == "user-1"

    byte_root = tmp_path / "bytes"
    byte_root.mkdir()
    for index in range(3):
        append_turn(byte_root, "s1", f"{index}:" + "x" * 30_000, SessionTurn(reply="ok"))
    byte_history = llm_history(byte_root, "s1")
    assert len(byte_history) == 4
    assert byte_history[0]["content"].startswith("1:")

    oversized_root = tmp_path / "oversized"
    oversized_root.mkdir()
    append_turn(oversized_root, "s1", "x" * (64 * 1024), SessionTurn(reply="ok"))
    assert llm_history(oversized_root, "s1") == []


def test_ui_context_enforces_two_images_signature_and_decoded_size():
    image = {"mime": "image/jpeg", "data": base64.b64encode(b"\xff\xd8\xff\xd9").decode()}
    base = {"schema": "keepframe.ui-context/1", "summary": {}}

    with pytest.raises(UIContextError, match="at most two images"):
        validate_ui_context({**base, "images": [image, image, image]})
    with pytest.raises(UIContextError, match="signature"):
        validate_ui_context({**base, "images": [{"mime": "image/png", "data": image["data"]}]})
    with pytest.raises(UIContextError, match="too large"):
        validate_ui_context({
            **base,
            "images": [{"mime": "image/jpeg", "data": base64.b64encode(b"\xff\xd8\xff" + b"x" * MAX_IMAGE_BYTES).decode()}],
        })


def test_vision_unsupported_stops_before_model_or_tools(tmp_path):
    class NoVision:
        supports_vision = False

        def complete(self, *_args):
            raise AssertionError("vision-incompatible model must not be called")

    context = validate_ui_context({
        "schema": "keepframe.ui-context/1",
        "summary": {},
        "images": [{"mime": "image/png", "data": base64.b64encode(b"\x89PNG\r\n\x1a\n").decode()}],
    })
    turn = SessionAgent(NoVision()).turn(type("Context", (), {"root": tmp_path, "scene_id": "s1", "version": None})(), "edit", [], context)
    assert turn.status == "error" and turn.error == "vision_unsupported" and turn.tool_calls == []


def test_ui_parse_regenerates_semantic_elements_and_keeps_source_texture(tmp_path, monkeypatch):
    model = UIModel(components=[UIComponent(id="nav", kind="nav", bbox=(0, 0, 100, 24), text="Home")])

    class FakeAssets:
        def request(self, **kwargs):
            assert kwargs["task"] == "parse" and kwargs["kind"] == "ui"
            return AssetResponse("application/json", model.model_dump_json(by_alias=True).encode(), model)

    monkeypatch.setenv("KEEPFRAME_ASSET_API_URL", "http://127.0.0.1:1")
    monkeypatch.setattr("keepframe.analyze.pipeline.AssetClient", FakeAssets)
    scene = Scene(id="s1", size=(100, 60), fps=30, frames=3, background=Background(), elements=[])
    parsed = _parse_ui(np.zeros((3, 60, 100, 3), np.uint8), scene, tmp_path)
    assert parsed.ui == model
    assert parsed.elements[0].kind == "ui"
    assert parsed.elements[0].canonical.texture == "assets/nav.ui.png"
    assert (tmp_path / "assets" / "nav.ui.png").is_file()
