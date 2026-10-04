from __future__ import annotations

import base64
import io
import json
import struct
import sys
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path

import pytest
from PIL import Image

from keepframe.assets.client import AssetClient, validate_glb
from scripts.asset_adapter_hf import (
    CANDIDATE_SPACES, HFAdapter, AdapterError, create_server, probe_spaces, _token,
)


def png():
    stream = io.BytesIO()
    Image.new("RGBA", (64, 64), (255, 0, 0, 255)).save(stream, format="PNG")
    return stream.getvalue()


def glb():
    document = json.dumps({"asset": {"version": "2.0"}}).encode()
    document += b" " * (-len(document) % 4)
    return struct.pack("<4sII", b"glTF", 2, 20 + len(document)) + struct.pack("<II", len(document), 0x4E4F534A) + document


def request(**update):
    return {
        "schema": "keepframe.asset.request/1", "task": "generate", "kind": "3d",
        "prompt": "a red circle", "size": {"width": 64, "height": 64},
        "input_image": {"mime": "image/png", "data": base64.b64encode(png()).decode()},
        **update,
    }


def parameter(name, component, default=None, optional=True):
    return {"parameter_name": name, "component": component,
            "parameter_has_default": optional, "parameter_default": default}


class FakeJob:
    def __init__(self, client, endpoint):
        self.client = client
        self.endpoint = endpoint

    def result(self, timeout):
        self.client.timeouts.append(timeout)
        if self.client.error:
            raise self.client.error
        return self.client.outputs.get(self.endpoint, self.client.output)

    def cancel(self):
        self.client.cancelled = True


class FakeClient:
    def __init__(self, output, *, endpoint="/run_button", parameters=None, error=None):
        self.output, self.error = output, error
        self.api = {"named_endpoints": {endpoint: {"parameters": parameters or [
            parameter("input_image", "Image", optional=False),
            parameter("seed", "Slider", 1234),
            parameter("randomize_seed", "Checkbox", True),
            parameter("texture_size", "Slider", 1024),
        ]}}}
        self.calls, self.timeouts = [], []
        self.cancelled = False
        self.outputs = {}

    def view_api(self, *, print_info, return_format):
        assert print_info is False and return_format == "dict"
        return self.api

    def submit(self, *args, **kwargs):
        self.calls.append(kwargs)
        image = kwargs.get("input_image", kwargs.get("image"))
        if image is not None:
            assert Path(image).read_bytes().startswith(b"\x89PNG")
        return FakeJob(self, kwargs["api_name"])


def adapter(client, **kwargs):
    return HFAdapter(client_factory=lambda *args, **kw: client,
                     file_handler=lambda path: path, token=False, **kwargs)


@pytest.fixture
def output(tmp_path):
    path = tmp_path / "model.glb"
    path.write_bytes(glb())
    return path


def test_real_asset_client_contract_over_loopback(output):
    fake = FakeClient((None, str(output)))
    server = create_server(adapter(fake), port=0)
    assert server.server_address[0] == "127.0.0.1"
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        client = AssetClient(f"http://127.0.0.1:{server.server_port}", timeout=2)
        response = client.request(task="generate", kind="3d", prompt="red circle", input_image=png(), size={"width":64,"height":64})
        assert response.mime == "model/gltf-binary"
        assert response.data == glb()
    finally:
        server.shutdown()
        server.server_close()
        thread.join()
    assert fake.calls[0]["seed"] == 0
    assert fake.calls[0]["randomize_seed"] is False
    assert fake.calls[0]["texture_size"] == 1024
    assert 0 < fake.timeouts[0] <= 300


@pytest.mark.parametrize("endpoint,image_name", [("/run_button", "input_image"), ("/generation_all", "image"), ("/generate_and_extract_glb", "image")])
def test_candidate_endpoint_signatures_and_nested_glb_outputs(output, endpoint, image_name):
    fake = FakeClient({"preview": "unused.mp4", "model": {"path": str(output)}}, endpoint=endpoint,
                      parameters=[parameter(image_name,"Image",optional=False),parameter("seed","Slider",8)])
    assert adapter(fake).generate(request()) == glb()
    assert fake.calls[0]["api_name"] == endpoint
    assert fake.calls[0]["seed"] == 0


@pytest.mark.parametrize("update", [{"task":"parse"},{"kind":"raster"},{"schema":"wrong"},{"input_image":None}])
def test_unsupported_requests_are_501_without_calling_space(output, update):
    fake = FakeClient(str(output))
    with pytest.raises(AdapterError) as error:
        adapter(fake).generate(request(**update))
    assert error.value.status == 501
    assert fake.calls == []


@pytest.mark.parametrize("image", [{"mime":"image/png","data":"%%%"},{"mime":"image/png","data":base64.b64encode(b"broken").decode()},{"mime":"text/plain","data":"AA=="}])
def test_malformed_images_are_rejected_before_space_call(output, image):
    fake = FakeClient(str(output))
    with pytest.raises(AdapterError) as error:
        adapter(fake).generate(request(input_image=image))
    assert error.value.status == 400
    assert fake.calls == []


def test_invalid_glb_and_remote_failures_are_unavailable(tmp_path):
    invalid = tmp_path / "broken.glb"
    invalid.write_bytes(b"broken")
    for fake in (FakeClient(str(invalid)), FakeClient(None, error=RuntimeError("quota exhausted"))):
        with pytest.raises(AdapterError) as error:
            adapter(fake).generate(request())
        assert error.value.code == "asset_api_unavailable"
        assert error.value.status == 503


def test_timeout_cancels_job(output):
    fake = FakeClient(str(output), error=TimeoutError("queue timed out"))
    with pytest.raises(AdapterError, match="timed out"):
        adapter(fake).generate(request())
    assert fake.cancelled


def test_timeout_also_bounds_client_setup():
    def slow_factory(*args, **kwargs):
        time.sleep(0.15)
        raise RuntimeError("unreachable")
    instance = HFAdapter(client_factory=slow_factory, file_handler=lambda p:p, token=False, timeout=0.02)
    started = time.monotonic()
    with pytest.raises(AdapterError, match="timed out"):
        instance.generate(request())
    assert time.monotonic() - started < 0.1


def test_probe_order_reachability_generation_and_redaction(output):
    visited = []
    def factory(space, **kwargs):
        visited.append(space)
        if space == CANDIDATE_SPACES[1]:
            raise RuntimeError("Bearer secret-for-test hf_abc123 quota " + "x" * 140)
        return FakeClient(str(output), error=RuntimeError("GPU quota") if space == CANDIDATE_SPACES[2] else None)
    rows = probe_spaces(client_factory=factory, file_handler=lambda p:p, token=False)
    assert visited == list(CANDIDATE_SPACES)
    assert [row["reachable"] for row in rows] == [True, False, True]
    assert [row["valid_glb"] for row in rows] == [True, False, False]
    assert rows[0]["endpoints"] == ["/run_button"]
    assert len(rows[1]["error"]) <= 120
    assert "secret-for-test" not in rows[1]["error"] and "hf_abc123" not in rows[1]["error"]
    assert validate_glb(glb()) == glb()


def test_missing_optional_dependency_returns_unavailable(monkeypatch):
    def missing_token():
        raise ImportError("optional library unavailable")
    monkeypatch.setattr("scripts.asset_adapter_hf._token", missing_token)
    with pytest.raises(AdapterError, match="optional library unavailable"):
        HFAdapter().generate(request())


def test_hunyuan_prefers_textured_glb_over_shape_only_glb(tmp_path):
    shape = tmp_path / "shape.glb"
    texture = tmp_path / "textured.glb"
    shape.write_bytes(glb())
    document = json.dumps({"asset":{"version":"2.0"},"materials":[{"name":"textured"}]}).encode()
    document += b" " * (-len(document) % 4)
    texture.write_bytes(struct.pack("<4sII",b"glTF",2,20+len(document)) + struct.pack("<II",len(document),0x4E4F534A) + document)
    fake = FakeClient((str(shape), str(texture)), endpoint="/generation_all")
    assert adapter(fake).generate(request()) == texture.read_bytes()


@pytest.mark.parametrize("endpoint,preparation", [("/run_button", ["/requires_bg_remove"]),("/generate_and_extract_glb", ["/start_session","/preprocess_image"])])
def test_space_session_preparation_precedes_one_generation(output, tmp_path, endpoint, preparation):
    fake = FakeClient(str(output), endpoint=endpoint)
    prepared = tmp_path / "prepared.png"
    prepared.write_bytes(png())
    for name in preparation:
        fake.api["named_endpoints"][name] = {"parameters": [] if name == "/start_session" else [parameter("image","Image",optional=False)]}
        fake.outputs[name] = str(prepared) if name == "/preprocess_image" else None
    assert adapter(fake).generate(request()) == glb()
    assert [call["api_name"] for call in fake.calls] == preparation + [endpoint]


def test_token_lookup_prefers_environment_and_falls_back_to_cache(monkeypatch):
    from types import SimpleNamespace
    monkeypatch.setenv("HF_TOKEN", "fake-env-value")
    monkeypatch.setitem(sys.modules, "huggingface_hub", SimpleNamespace(get_token=lambda:"fake-cached-value"))
    assert _token() == "fake-env-value"
    monkeypatch.delenv("HF_TOKEN")
    assert _token() == "fake-cached-value"
    monkeypatch.setitem(sys.modules, "huggingface_hub", SimpleNamespace(get_token=lambda:None))
    assert _token() is False


def test_probe_defaults_to_anonymous_even_when_token_lookup_is_available(output, monkeypatch):
    def forbidden_token_lookup():
        raise AssertionError("anonymous probes must not consult credentials")
    monkeypatch.setattr("scripts.asset_adapter_hf._token", forbidden_token_lookup)
    def factory(*args, **kwargs):
        assert kwargs["token"] is False
        return FakeClient(str(output))
    rows = probe_spaces(client_factory=factory, file_handler=lambda p:p)
    assert all(row["valid_glb"] for row in rows)


@pytest.mark.parametrize("body,status,code", [(request(kind="raster"),501,"not_implemented"),(request(),503,"asset_api_unavailable"),(b"invalid JSON",400,"invalid_asset_request")])
def test_http_errors_use_safe_json_and_expected_status(output, body, status, code):
    server = create_server(adapter(FakeClient(str(output), error=RuntimeError("Bearer sensitive-for-test quota"))), port=0)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        payload = body if isinstance(body, bytes) else json.dumps(body).encode()
        req = urllib.request.Request(f"http://127.0.0.1:{server.server_port}", data=payload, headers={"Content-Type":"application/json"})
        with pytest.raises(urllib.error.HTTPError) as error:
            urllib.request.urlopen(req, timeout=2)
        assert error.value.code == status
        response = error.value.read()
        assert json.loads(response)["code"] == code
        assert b"sensitive-for-test" not in response
    finally:
        server.shutdown()
        server.server_close()
        thread.join()
