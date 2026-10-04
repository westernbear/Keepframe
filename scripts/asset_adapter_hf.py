"""Loopback image-to-3D asset API backed by public Hugging Face Spaces.

Run: uv run --with gradio_client scripts/asset_adapter_hf.py --port 8790
Probe: uv run --with gradio_client scripts/asset_adapter_hf.py --probe
"""
from __future__ import annotations

import argparse
import base64
import binascii
import inspect
import io
import json
import os
import queue
import re
import tempfile
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Callable

from PIL import Image, ImageDraw

from keepframe.assets.client import AssetAPIError, MAX_2D, MAX_GLB, validate_glb

CANDIDATE_SPACES = (
    "stabilityai/stable-fast-3d",
    "tencent/Hunyuan3D-2",
    "trellis-community/TRELLIS",
)
TIMEOUT = 300.0
MAX_REQUEST = 14 * 1024 * 1024
_GENERATION_ENDPOINTS = ("/run_button", "/generation_all", "/generate_and_extract_glb")


def safe_error(error: Exception, token: str | bool | None = None) -> str:
    message = str(error)
    for secret in (token, os.environ.get("HF_TOKEN"), os.environ.get("KEEPFRAME_ASSET_API_KEY")):
        if isinstance(secret, str) and secret:
            message = message.replace(secret, "[REDACTED]")
    message = re.sub(r"hf_[A-Za-z0-9]+", "[REDACTED]", message)
    message = re.sub(r"(?i)Bearer\s+\S+", "Bearer [REDACTED]", message)
    return " ".join(message.split())[:120]


class AdapterError(RuntimeError):
    def __init__(self, message: str, *, status: int = 503, code: str = "asset_api_unavailable"):
        self.status, self.code = status, code
        super().__init__(message)


def _token() -> str | bool:
    token = os.environ.get("HF_TOKEN")
    if not token:
        from huggingface_hub import get_token
        token = get_token()
    return token or False


def _client_factory(space: str, *, token: str | bool, directory: str, timeout: float):
    # gradio_client is intentionally an optional, lazily imported CLI extra.
    from gradio_client import Client
    # Gradio 2.x renamed hf_token to token. Support existing 1.x installs too.
    token_name = "token" if "token" in inspect.signature(Client).parameters else "hf_token"
    return Client(
        space, **{token_name: token}, verbose=False, analytics_enabled=False,
        download_files=directory, httpx_kwargs={"timeout": timeout},
    )


def _handle_file(path: str):
    from gradio_client import handle_file
    return handle_file(path)


def _input_image(body: Any) -> bytes:
    if (not isinstance(body, dict) or body.get("schema") != "keepframe.asset.request/1"
            or body.get("task") != "generate" or body.get("kind") != "3d"
            or not isinstance(body.get("input_image"), dict)):
        raise AdapterError("only generate/3d with input_image is supported", status=501, code="not_implemented")
    image = body["input_image"]
    if image.get("mime") != "image/png" or not isinstance(image.get("data"), str):
        raise AdapterError("input_image must contain base64 PNG data", status=400, code="invalid_asset_request")
    if len(image["data"]) > (MAX_2D + 2) // 3 * 4:
        raise AdapterError("input_image is too large", status=413, code="asset_too_large")
    try:
        data = base64.b64decode(image["data"], validate=True)
        with Image.open(io.BytesIO(data)) as decoded:
            width, height = decoded.size
            if decoded.format != "PNG" or not (0 < width <= 16_384 and 0 < height <= 16_384) or width * height > 100_000_000:
                raise ValueError("invalid PNG dimensions")
            decoded.verify()
    except (ValueError, OSError, binascii.Error, Image.DecompressionBombError) as exc:
        raise AdapterError("invalid input PNG", status=400, code="invalid_asset_request") from exc
    return data


def _glb_outputs(value: Any):
    if isinstance(value, bytes):
        yield value
    elif isinstance(value, (str, Path)):
        path = Path(value)
        if path.suffix.lower() == ".glb" and path.is_file():
            with path.open("rb") as stream:
                yield stream.read(MAX_GLB + 1)
    elif isinstance(value, dict):
        if value.get("mime") == "model/gltf-binary" and isinstance(value.get("data"), str):
            if len(value["data"]) <= (MAX_GLB + 2) // 3 * 4:
                yield base64.b64decode(value["data"], validate=True)
        else:
            for nested in value.values():
                yield from _glb_outputs(nested)
    elif isinstance(value, (list, tuple)):
        for nested in value:
            yield from _glb_outputs(nested)


class HFAdapter:
    def __init__(self, space: str = CANDIDATE_SPACES[0], *, client_factory: Callable = _client_factory,
                 file_handler: Callable = _handle_file, token: str | bool | None = None,
                 timeout: float = TIMEOUT):
        self.space, self.client_factory, self.file_handler = space, client_factory, file_handler
        self.token, self.timeout = token, timeout

    def _run(self, image: bytes, observation: dict[str, Any] | None = None) -> bytes:
        deadline = time.monotonic() + self.timeout
        def remaining():
            seconds = deadline - time.monotonic()
            if seconds <= 0:
                raise TimeoutError("Space request timed out")
            return seconds
        with tempfile.TemporaryDirectory(prefix="keepframe-hf-") as directory:
            token = self.token or False
            client = None
            try:
                if self.token is None:
                    token = _token()
                client = self.client_factory(self.space, token=token, directory=directory, timeout=remaining())
                api = client.view_api(print_info=False, return_format="dict")
                endpoints = api["named_endpoints"]
                if observation is not None:
                    observation.update(reachable=True, endpoints=sorted(endpoints))
                endpoint = next((name for name in _GENERATION_ENDPOINTS if name in endpoints), None)
                if endpoint is None:
                    # ponytail: three inspected Space APIs; add explicit recipes
                    # when another Space exposes a different generation pipeline.
                    raise AdapterError("Space has no supported image-to-GLB endpoint")
                path = Path(directory) / "input.png"
                path.write_bytes(image)
                image_file = self.file_handler(str(path))

                def invoke(name: str):
                    parameters: dict[str, Any] = {}
                    image_supplied = False
                    for parameter in endpoints[name]["parameters"]:
                        key = parameter["parameter_name"]
                        if parameter.get("component") == "Image" and not image_supplied:
                            parameters[key] = image_file
                            image_supplied = True
                        elif key == "seed" or (key.endswith("_seed") and "random" not in key):
                            parameters[key] = 0
                        elif "randomize" in key and "seed" in key:
                            parameters[key] = False
                        elif parameter.get("parameter_has_default"):
                            parameters[key] = parameter.get("parameter_default")
                        else:
                            raise AdapterError(f"unsupported required Space parameter: {key}")
                    if name == endpoint and not image_supplied:
                        raise AdapterError("Space generation endpoint has no image input")
                    remaining()
                    job = client.submit(api_name=name, **parameters)
                    try:
                        return job.result(timeout=remaining())
                    except TimeoutError:
                        job.cancel()
                        raise

                # These UI callbacks populate server session state and must use
                # the same client/session as the single generation call.
                if endpoint == "/run_button" and "/requires_bg_remove" in endpoints:
                    invoke("/requires_bg_remove")
                if endpoint == "/generate_and_extract_glb":
                    if "/start_session" in endpoints:
                        invoke("/start_session")
                    if "/preprocess_image" in endpoints:
                        processed = invoke("/preprocess_image")
                        if isinstance(processed, dict):
                            processed = processed.get("path")
                        image_file = self.file_handler(processed)
                result = invoke(endpoint)
                if endpoint == "/generation_all" and isinstance(result, (tuple, list)) and len(result) >= 2:
                    # Hunyuan returns shape-only, then textured mesh. Prefer the
                    # textured output while retaining the shape fallback.
                    result = [result[1], result[0], *result[2:]]
                for data in _glb_outputs(result):
                    try:
                        return validate_glb(data)
                    except AssetAPIError:
                        continue
                raise AdapterError("Space returned no valid GLB")
            except Exception as exc:
                raise AdapterError(safe_error(exc, token)) from exc
            finally:
                # Stop Gradio's heartbeat; fake clients need only view_api and submit.
                close = getattr(client, "close", None)
                if close is not None:
                    try:
                        close()
                    except Exception:
                        pass

    def _bounded_run(self, image: bytes, observation: dict[str, Any] | None = None) -> bytes:
        results: queue.Queue = queue.Queue(maxsize=1)
        def work():
            try:
                results.put((self._run(image, observation), None))
            except Exception as exc:
                results.put((None, exc))
        # The outer deadline includes Space discovery, uploads and downloads,
        # not just waiting in the GPU queue. A stalled setup cannot block HTTP.
        threading.Thread(target=work, daemon=True).start()
        try:
            data, error = results.get(timeout=self.timeout)
        except queue.Empty as exc:
            raise AdapterError(f"Space request timed out after {self.timeout:g} seconds") from exc
        if error is not None:
            raise error
        return data

    def generate(self, body: Any) -> bytes:
        return self._bounded_run(_input_image(body))


def create_server(adapter: HFAdapter, *, port: int = 8790) -> ThreadingHTTPServer:
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_args):
            pass  # Never log request URLs, authorization headers or credentials.

        def do_POST(self):
            try:
                length = int(self.headers.get("Content-Length", "0"))
                if not 0 < length <= MAX_REQUEST:
                    raise AdapterError("invalid request length", status=413, code="asset_too_large")
                body = json.loads(self.rfile.read(length))
                data = adapter.generate(body)
                status, mime = 200, "model/gltf-binary"
            except (ValueError, UnicodeError):
                status, mime = 400, "application/json"
                data = json.dumps({"code": "invalid_asset_request"}).encode()
            except AdapterError as exc:
                status, mime = exc.status, "application/json"
                data = json.dumps({"code": exc.code, "error": exc.code, "message": safe_error(exc, adapter.token)}).encode()
            self.send_response(status)
            self.send_header("Content-Type", mime)
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

    return ThreadingHTTPServer(("127.0.0.1", port), Handler)


def probe_spaces(*, spaces=CANDIDATE_SPACES, client_factory: Callable = _client_factory,
                 file_handler: Callable = _handle_file, token: str | bool | None = False,
                 emit: Callable | None = None) -> list[dict[str, Any]]:
    image = Image.new("RGBA", (64, 64), (0, 0, 0, 0))
    ImageDraw.Draw(image).ellipse((12, 12, 51, 51), fill=(255, 0, 0, 255))
    stream = io.BytesIO()
    image.save(stream, format="PNG")
    rows = []
    for space in spaces:
        row = {"space": space, "reachable": False, "endpoints": [], "valid_glb": False, "error": ""}
        adapter = HFAdapter(space, client_factory=client_factory, file_handler=file_handler, token=token)
        try:
            adapter._bounded_run(stream.getvalue(), row)
            row["valid_glb"] = True
        except Exception as exc:
            row["error"] = safe_error(exc, token)
        rows.append(dict(row))
        if emit is not None:
            emit(dict(row))
    return rows


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", type=int, default=8790)
    parser.add_argument("--space", help="Space ID (default: stabilityai/stable-fast-3d)")
    parser.add_argument("--probe", action="store_true", help="probe each candidate anonymously once and exit")
    args = parser.parse_args()
    if args.probe:
        probe_spaces(spaces=(args.space,) if args.space else CANDIDATE_SPACES,
                     token=False, emit=lambda row: print(json.dumps(row), flush=True))
        return 0
    server = create_server(HFAdapter(args.space or CANDIDATE_SPACES[0]), port=args.port)
    print(f"Asset adapter listening on http://127.0.0.1:{server.server_port}", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
