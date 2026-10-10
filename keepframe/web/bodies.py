"""Request body length, decided from the headers before any body byte is read (browser and extension routes)."""
from __future__ import annotations

import re

_DIGITS = re.compile(r"[0-9]{1,19}")


class LengthError(ValueError):
    """An unusable or too large declared body: `status` is the HTTP answer (400, 411 or 413)."""

    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status = status


def content_length(headers, cap: int, *, required: bool = False) -> int:
    """The single Content-Length (no Transfer-Encoding), at most `cap`; 0 when absent and not `required`."""
    lengths = headers.get_all("Content-Length") or []
    if not lengths and required:
        raise LengthError(411, "Content-Length is required")
    if len(lengths) > 1 or headers.get("Transfer-Encoding"):
        raise LengthError(400, "invalid Content-Length")
    value = lengths[0].strip() if lengths else "0"
    if not _DIGITS.fullmatch(value):
        raise LengthError(400, "invalid Content-Length")
    length = int(value)
    if length > cap:
        raise LengthError(413, "request body is too large")
    return length
