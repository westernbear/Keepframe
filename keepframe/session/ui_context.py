from __future__ import annotations

import base64
import binascii
import json
import re
from dataclasses import dataclass
from typing import Any

MAX_SUMMARY_BYTES = 16 * 1024
MAX_IMAGE_BYTES = 512 * 1024
MAX_IMAGES = 2
_PRIVATE_KEY = re.compile(r"cookie|password|file.?path|pairing.?code|relay.?url|command", re.I)


class UIContextError(ValueError):
    pass


@dataclass(frozen=True)
class UIContext:
    summary: str
    images: tuple[dict[str, str], ...]


def _redact(value: Any) -> Any:
    if isinstance(value, dict):
        return {key: _redact(item) for key, item in value.items() if not _PRIVATE_KEY.search(str(key))}
    if isinstance(value, list):
        return [_redact(item) for item in value]
    return value


def _image(raw: Any) -> dict[str, str]:
    if not isinstance(raw, dict):
        raise UIContextError("ui_context image is invalid")
    mime = str(raw.get("mime") or raw.get("type") or "").lower()
    encoded = raw.get("data") or raw.get("base64")
    if isinstance(encoded, str) and encoded.startswith("data:"):
        header, comma, encoded = encoded.partition(",")
        if not comma or ";base64" not in header:
            raise UIContextError("ui_context image data URL is invalid")
        mime = header[5:].split(";", 1)[0].lower()
    if mime not in {"image/jpeg", "image/png"} or not isinstance(encoded, str):
        raise UIContextError("ui_context image MIME is invalid")
    try:
        decoded = base64.b64decode(encoded, validate=True)
    except (binascii.Error, ValueError) as exc:
        raise UIContextError("ui_context image base64 is invalid") from exc
    if len(decoded) > MAX_IMAGE_BYTES:
        raise UIContextError("ui_context image is too large")
    signature_ok = decoded.startswith(b"\xff\xd8\xff") if mime == "image/jpeg" else decoded.startswith(b"\x89PNG\r\n\x1a\n")
    if not signature_ok:
        raise UIContextError("ui_context image signature does not match MIME")
    return {"mime": mime, "data": encoded}


def validate_ui_context(raw: Any) -> UIContext | None:
    if raw is None:
        return None
    if not isinstance(raw, dict) or raw.get("schema") != "keepframe.ui-context/1":
        raise UIContextError("ui_context schema is invalid")
    summary_value = _redact(raw.get("summary", raw.get("state", {})))
    summary = summary_value if isinstance(summary_value, str) else json.dumps(summary_value, ensure_ascii=False, separators=(",", ":"))
    if len(summary.encode("utf-8")) > MAX_SUMMARY_BYTES:
        raise UIContextError("ui_context summary is too large")
    raw_images = raw.get("images", raw.get("previews", []))
    if not isinstance(raw_images, list) or len(raw_images) > MAX_IMAGES:
        raise UIContextError("ui_context supports at most two images")
    return UIContext(summary=summary, images=tuple(_image(item) for item in raw_images))


def multimodal_content(message: str, context: UIContext) -> list[dict[str, Any]]:
    content: list[dict[str, Any]] = [
        {"type": "text", "text": f"{message}\n\n현재 Keepframe UI 관찰 요약:\n{context.summary}"}
    ]
    content.extend(
        {
            "type": "image_url",
            "image_url": {"url": f"data:{image['mime']};base64,{image['data']}", "detail": "low"},
        }
        for image in context.images
    )
    return content
