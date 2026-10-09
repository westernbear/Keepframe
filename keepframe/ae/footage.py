"""After Effects footage derived from Keepframe's WebM clips (AE cannot import WebM; Task 13 makes them).

A plate (opaque background) becomes H.264 `.ae.mp4`; a sprite (RGBA) becomes ProRes 4444 `.ae.mov`
(yuva444p10le, straight alpha). Each is derived once, as a flat file beside its source in the scene's assets,
named by the source's SHA-256: an unchanged clip is never derived again and a changed one gets a new name.
Frames are converted from the WebM's BT.601 to tagged BT.709, the matrix AE assumes for both codecs, and scaled
to the poster's size (`size`), which places the layer.

The AE routes never encode inside a request: `prepare` derives in a background thread (one lock per clip) when a
sync job is created, the spec route answers "preparing" until it is done, and `derive(wait=False)` only looks up.
Encodes are bounded by the clip's length (`encode_timeout`), clips by `MAX_SECONDS`, files by `MAX_BYTES`; a
failure is remembered for `RETRY_S` and the layer keeps its poster with the failure's code.
"""
from __future__ import annotations

import hashlib
import os
import subprocess
import threading
import time
from collections import OrderedDict
from pathlib import Path

from ..log import get

log = get("keepframe.ae")
TIMEOUT_BASE_S, TIMEOUT_PER_CLIP_S = 30.0, 8.0
MAX_SECONDS = 300.0                 # longer clips stay posters (footage_too_long)
MAX_BYTES = 1 << 30                 # a bigger derived file is dropped (footage_too_large)
RETRY_S = 600.0
_TAGS = ["-colorspace", "bt709", "-color_primaries", "bt709", "-color_trc", "bt709", "-color_range", "tv"]
_SCALE = "scale=w={w}:h={h}:in_color_matrix=bt601:in_range=tv:out_color_matrix=bt709:out_range=tv"
FORMATS = {
    "plate": (".ae.mp4", [], ",pad=ceil(iw/2)*2:ceil(ih/2)*2,format=yuv420p",
              ["-c:v", "libx264", "-preset", "medium", "-crf", "14", "-pix_fmt", "yuv420p", *_TAGS,
               "-movflags", "+faststart", "-f", "mp4"]),
    "sprite": (".ae.mov", ["-c:v", "libvpx-vp9"], ",format=yuva444p10le",   # the native vp9 decoder drops alpha
               ["-c:v", "prores_ks", "-profile:v", "4444", "-pix_fmt", "yuva444p10le", "-alpha_bits", "16",
                "-vendor", "apl0", *_TAGS, "-movflags", "+write_colr", "-f", "mov"]),
}
_STATE = threading.Lock()
_locks: dict[Path, threading.Lock] = {}
_failed: dict[Path, tuple[str, float]] = {}
_running: dict[Path, threading.Thread] = {}
_digests: OrderedDict[tuple[str, int, int], str] = OrderedDict()


class FootageError(RuntimeError):
    """`str(error)` is a short code for clients: ffmpeg_missing, footage_failed, footage_timeout, footage_too_long,
    footage_too_large or footage_pending."""


def file_sha256(path: Path) -> str:
    """SHA-256 of a file, read in chunks, remembered per (path, size, mtime) (videos can be gigabytes)."""
    path = Path(path)
    stat = path.stat()
    key = (str(path), stat.st_size, stat.st_mtime_ns)
    with _STATE:
        if key in _digests:
            _digests.move_to_end(key)
            return _digests[key]
    with path.open("rb") as stream:
        digest = hashlib.file_digest(stream, "sha256").hexdigest()
    with _STATE:
        _digests[key] = digest
        while len(_digests) > 512:
            _digests.popitem(last=False)
    return digest


def encode_timeout(seconds: float) -> float:
    return TIMEOUT_BASE_S + TIMEOUT_PER_CLIP_S * seconds


def derived_path(src: Path, kind: str) -> Path:
    return src.with_name(f"{src.name.removesuffix('.webm')}.{file_sha256(src)[:16]}{FORMATS[kind][0]}")


def _ready(out: Path) -> bool:
    return out.is_file() and out.stat().st_size > 0


def _encode(command, timeout):
    return subprocess.run(command, capture_output=True, timeout=timeout)


def derive(src: Path, kind: str, fps: float, size: tuple[int, int], seconds: float, *, wait: bool = True) -> Path:
    """The AE footage for the clip at `src` ("plate" or "sprite"); raises FootageError with a code. Without `wait`
    it only looks up: a clip that is neither derived nor failed raises footage_pending."""
    try:
        out = derived_path(Path(src), kind)
    except OSError as exc:
        log.warning("AE footage source unreadable: %s", type(exc).__name__)
        raise FootageError("footage_failed") from None
    if _ready(out):
        return out
    with _STATE:
        failed = _failed.get(out)
        if failed is not None and time.monotonic() - failed[1] < RETRY_S:
            raise FootageError(failed[0])
        if not wait:
            raise FootageError("footage_pending")
        lock = _locks.setdefault(out, threading.Lock())
    with lock:   # one encode per clip; a concurrent caller then finds it derived (or failed)
        if _ready(out):
            return out
        try:
            if seconds > MAX_SECONDS:
                raise FootageError("footage_too_long")
            _encode_to(Path(src), out, kind, fps, size, seconds)
        except FootageError as exc:
            with _STATE:
                _failed[out] = (str(exc), time.monotonic())
            raise
        return out


def _encode_to(src, out, kind, fps, size, seconds):
    suffix, decode, fmt, encode = FORMATS[kind]
    tmp = out.with_name(f".{out.name}.{os.getpid()}.{threading.get_ident()}.tmp")
    command = ["ffmpeg", "-y", "-loglevel", "error", *decode, "-i", str(src), "-an",
               "-vf", _SCALE.format(w=int(size[0]), h=int(size[1])) + fmt, "-r", repr(float(fps)), *encode, str(tmp)]
    started = time.monotonic()
    try:
        done = _encode(command, encode_timeout(seconds))
        if done.returncode != 0 or not tmp.is_file() or not tmp.stat().st_size:
            log.warning("AE footage encode failed: %s", done.stderr[-300:].decode("utf-8", "replace"))
            raise FootageError("footage_failed")
        if tmp.stat().st_size > MAX_BYTES:
            log.warning("AE footage %s is %d bytes; over the %d byte cap", kind, tmp.stat().st_size, MAX_BYTES)
            raise FootageError("footage_too_large")
        os.replace(tmp, out)
    except FileNotFoundError:
        raise FootageError("ffmpeg_missing") from None
    except subprocess.TimeoutExpired:
        raise FootageError("footage_timeout") from None
    except OSError as exc:
        log.warning("AE footage write failed: %s", type(exc).__name__)
        raise FootageError("footage_failed") from None
    finally:
        tmp.unlink(missing_ok=True)
    log.info("ae footage %s %.2fs", kind, time.monotonic() - started)


def _background(src, kind, fps, size, seconds):
    try:
        derive(src, kind, fps, size, seconds)
    except FootageError:
        pass   # remembered in _failed; the spec reports its code
    except Exception:
        log.exception("AE footage derivation failed")
        with _STATE:
            _failed[derived_path(Path(src), kind)] = ("footage_failed", time.monotonic())


def prepare(src: Path, kind: str, fps: float, size: tuple[int, int], seconds: float) -> bool:
    """Derive in the background (if needed); True while the clip is neither derived nor failed."""
    out = derived_path(Path(src), kind)
    if _ready(out):
        return False
    with _STATE:
        failed = _failed.get(out)
        if failed is not None and time.monotonic() - failed[1] < RETRY_S:
            return False
        thread = _running.get(out)
        if thread is None or not thread.is_alive():
            thread = threading.Thread(target=_background, args=(Path(src), kind, fps, size, seconds),
                                      name=f"keepframe-ae-footage-{out.name}", daemon=True)
            _running[out] = thread
            thread.start()
    return True
