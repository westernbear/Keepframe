from __future__ import annotations

import base64
import ipaddress
import json
import os
import struct
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from typing import Any, Literal

import cv2
import numpy as np

from keepframe.ir.schema import UIModel

MAX_UI = 1 * 1024 * 1024
MAX_2D = 10 * 1024 * 1024
MAX_GLB = 50 * 1024 * 1024


class AssetAPIError(RuntimeError):
    def __init__(self, code: str, message: str | None = None) -> None:
        self.code = code
        super().__init__(message or code)


@dataclass(frozen=True)
class AssetResponse:
    mime: str
    data: bytes
    ui: UIModel | None = None


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise AssetAPIError("asset_api_redirect", "asset API redirects are not allowed")


def _safe_url(raw: str) -> str:
    parsed = urllib.parse.urlparse(raw)
    if parsed.username or parsed.password or parsed.fragment or parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise AssetAPIError("asset_api_url_invalid")
    if parsed.scheme == "http":
        try:
            loopback = ipaddress.ip_address(parsed.hostname).is_loopback
        except ValueError:
            loopback = parsed.hostname.lower() == "localhost"
        if not loopback:
            raise AssetAPIError("asset_api_url_invalid", "asset API HTTP is allowed only on loopback")
    return raw.rstrip("/")


def _read_limited(response, limit: int) -> bytes:
    announced = response.headers.get("Content-Length")
    if announced:
        try:
            if int(announced) > limit:
                raise AssetAPIError("asset_too_large")
        except ValueError as exc:
            raise AssetAPIError("invalid_asset_response") from exc
    data = response.read(limit + 1)
    if len(data) > limit:
        raise AssetAPIError("asset_too_large")
    return data


def sanitize_svg(data: bytes) -> bytes:
    if len(data) > MAX_2D:
        raise AssetAPIError("asset_too_large")
    try:
        root = ET.fromstring(data)
    except ET.ParseError as exc:
        raise AssetAPIError("invalid_svg") from exc
    for element in root.iter():
        tag = element.tag.rsplit("}", 1)[-1].lower()
        if tag in {"script", "foreignobject", "iframe", "object", "embed"}:
            raise AssetAPIError("unsafe_svg")
        if tag == "style" and ("@import" in (element.text or "").lower() or "url(" in (element.text or "").lower()):
            raise AssetAPIError("unsafe_svg")
        for name, value in element.attrib.items():
            local = name.rsplit("}", 1)[-1].lower()
            if local.startswith("on"):
                raise AssetAPIError("unsafe_svg")
            if local in {"href", "src"} and value and not value.startswith("#"):
                raise AssetAPIError("unsafe_svg")
            if "url(" in value.lower() and "url(#" not in value.lower():
                raise AssetAPIError("unsafe_svg")
    return ET.tostring(root, encoding="utf-8", xml_declaration=True)


def _png(data: bytes) -> bytes:
    if len(data) > MAX_2D:
        raise AssetAPIError("asset_too_large")
    if len(data) < 24 or not data.startswith(b"\x89PNG\r\n\x1a\n"):
        raise AssetAPIError("invalid_raster")
    width, height = struct.unpack_from(">II", data, 16)
    if not width or not height or width > 16_384 or height > 16_384 or width * height > 100_000_000:
        raise AssetAPIError("invalid_raster")
    image = cv2.imdecode(np.frombuffer(data, np.uint8), cv2.IMREAD_UNCHANGED)
    if image is None or image.ndim not in {2, 3}:
        raise AssetAPIError("invalid_raster")
    ok, encoded = cv2.imencode(".png", image)
    if not ok:
        raise AssetAPIError("invalid_raster")
    return encoded.tobytes()


def validate_glb(data: bytes) -> bytes:
    if len(data) > MAX_GLB:
        raise AssetAPIError("asset_too_large")
    if len(data) < 20 or data[:4] != b"glTF":
        raise AssetAPIError("invalid_glb")
    version, length = struct.unpack_from("<II", data, 4)
    if version != 2 or length != len(data):
        raise AssetAPIError("invalid_glb")
    offset = 12
    document = None
    while offset < len(data):
        if offset + 8 > len(data):
            raise AssetAPIError("invalid_glb")
        chunk_length, chunk_type = struct.unpack_from("<II", data, offset)
        offset += 8
        chunk = data[offset : offset + chunk_length]
        if len(chunk) != chunk_length:
            raise AssetAPIError("invalid_glb")
        if chunk_type == 0x4E4F534A:
            try:
                document = json.loads(chunk.rstrip(b" \t\r\n\x00"))
            except json.JSONDecodeError as exc:
                raise AssetAPIError("invalid_glb") from exc
        offset += chunk_length
    if offset != len(data) or not isinstance(document, dict):
        raise AssetAPIError("invalid_glb")
    for collection in (document.get("buffers") or [], document.get("images") or []):
        for item in collection:
            uri = item.get("uri") if isinstance(item, dict) else None
            if isinstance(uri, str) and not uri.startswith("data:"):
                raise AssetAPIError("glb_external_reference")
    return data


def _validated(mime: str, data: bytes, kind: str) -> AssetResponse:
    mime = mime.split(";", 1)[0].strip().lower()
    if mime == "image/png":
        return AssetResponse("image/png", _png(data))
    if mime == "image/svg+xml" and kind == "vector":
        return AssetResponse(mime, sanitize_svg(data))
    if mime in {"model/gltf-binary", "application/octet-stream"} and kind == "3d":
        return AssetResponse("model/gltf-binary", validate_glb(data))
    if mime == "application/json" and kind == "ui":
        if len(data) > MAX_UI:
            raise AssetAPIError("asset_too_large")
        try:
            ui = UIModel.model_validate_json(data)
        except ValueError as exc:
            raise AssetAPIError("invalid_ui_schema") from exc
        return AssetResponse(mime, ui.model_dump_json(by_alias=True).encode("utf-8"), ui)
    raise AssetAPIError("asset_mime_not_allowed", mime)


class AssetClient:
    def __init__(self, url: str | None = None, api_key: str | None = None, timeout: float = 120.0) -> None:
        raw_url = url or os.environ.get("KEEPFRAME_ASSET_API_URL")
        if not raw_url:
            raise AssetAPIError("asset_api_not_configured")
        self.url = _safe_url(raw_url)
        self.api_key = api_key if api_key is not None else os.environ.get("KEEPFRAME_ASSET_API_KEY", "")
        self.timeout = timeout
        self.opener = urllib.request.build_opener(_NoRedirect())

    def request(
        self,
        *,
        task: Literal["parse", "generate"],
        kind: Literal["vector", "raster", "ui", "3d"],
        prompt: str,
        input_image: bytes | None = None,
        size: dict[str, int] | None = None,
    ) -> AssetResponse:
        body: dict[str, Any] = {
            "schema": "keepframe.asset.request/1",
            "task": task,
            "kind": kind,
            "prompt": prompt,
            "size": size or {},
        }
        if input_image is not None:
            body["input_image"] = {"mime": "image/png", "data": base64.b64encode(input_image).decode("ascii")}
        headers = {"Content-Type": "application/json", "Accept": "application/json, image/png, image/svg+xml, model/gltf-binary"}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
        request = urllib.request.Request(self.url, json.dumps(body).encode("utf-8"), headers, method="POST")
        try:
            with self.opener.open(request, timeout=self.timeout) as response:
                mime = response.headers.get_content_type()
                limit = MAX_GLB if kind == "3d" else MAX_UI if kind == "ui" else MAX_2D
                data = _read_limited(response, limit)
        except AssetAPIError:
            raise
        except urllib.error.HTTPError as exc:
            raise AssetAPIError("asset_api_http_error", str(exc.code)) from exc
        except urllib.error.URLError as exc:
            raise AssetAPIError("asset_api_unavailable", str(exc.reason)) from exc
        if mime == "application/json" and kind != "ui":
            try:
                wrapper = json.loads(data)
                mime = str(wrapper["mime"])
                data = base64.b64decode(wrapper["data"], validate=True)
            except Exception as exc:
                raise AssetAPIError("invalid_asset_response") from exc
        return _validated(mime, data, kind)
