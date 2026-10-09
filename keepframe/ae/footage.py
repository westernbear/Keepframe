"""After Effects footage derived from Keepframe's WebM clips (AE cannot import WebM; Task 13 makes them).

A plate (opaque background) becomes H.264 `.ae.mp4`; a sprite (RGBA) becomes ProRes 4444 `.ae.mov`
(yuva444p10le, straight alpha). Each is derived once, as a flat file beside its source in the scene's assets,
named by the source's SHA-256: an unchanged clip is never derived again and a changed one gets a new name.
Frames are converted from the WebM's BT.601 to tagged BT.709, the matrix AE assumes for both codecs, and scaled
to the poster's size (`size`), which places the layer.

The AE routes never encode inside a request: `prepare` derives in a background thread when a sync job is created,
the spec route answers "preparing" until it is done, and `derive(wait=False)` only looks up. Limits (R60):
- at most MAX_ENCODES ffmpeg encodes at once, and MAX_ENCODES + MAX_QUEUED clip threads; further clips stay
  pending until a later `prepare` finds room;
- the source's own duration (ffprobe) above MAX_SECONDS is footage_too_long, and ffmpeg reads at most MAX_SECONDS
  (`-t`) and writes at most MAX_BYTES (`-fs`; an output that reaches it is footage_too_large);
- an encode times out after `encode_timeout` (clip length, at most MAX_TIMEOUT_S), counted once it has a slot;
- a failure is remembered for RETRY_S (at most FAILED_CAP of them, oldest dropped) and the layer keeps its poster.
"""
from __future__ import annotations

import hashlib
import math
import os
import subprocess
import threading
import time
from collections import OrderedDict
from pathlib import Path

from ..log import get

log = get("keepframe.ae")
TIMEOUT_BASE_S, TIMEOUT_PER_CLIP_S, MAX_TIMEOUT_S = 30.0, 8.0, 600.0
PROBE_TIMEOUT_S = 10.0
MAX_SECONDS = 120.0                 # longer clips stay posters (footage_too_long)
MAX_BYTES = 1 << 30                 # an output reaching this is dropped (footage_too_large)
MAX_ENCODES, MAX_QUEUED = 2, 4
RETRY_S, FAILED_CAP = 600.0, 256
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
_STATE = threading.Condition()
_SLOTS = threading.BoundedSemaphore(MAX_ENCODES)
_active: set[Path] = set()                                   # clips being derived now (one deriver each)
_threads: dict[Path, threading.Thread] = {}                  # background derivers, removed when they end
_failed: OrderedDict[Path, tuple[str, float]] = OrderedDict()
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
    return min(MAX_TIMEOUT_S, TIMEOUT_BASE_S + TIMEOUT_PER_CLIP_S * seconds)


def derived_path(src: Path, kind: str) -> Path:
    return src.with_name(f"{src.name.removesuffix('.webm')}.{file_sha256(src)[:16]}{FORMATS[kind][0]}")


def _ready(out: Path) -> bool:
    return out.is_file() and out.stat().st_size > 0


def _encode(command, timeout):
    return subprocess.run(command, capture_output=True, timeout=timeout)


def _ffprobe(command, timeout):
    return subprocess.run(command, capture_output=True, timeout=timeout)


def probe_seconds(src: Path) -> float:
    """The clip's duration as its container states it (ffprobe, argument list, short timeout)."""
    command = ["ffprobe", "-v", "error", "-show_entries", "format=duration",
               "-of", "default=noprint_wrappers=1:nokey=1", str(src)]
    try:
        done = _ffprobe(command, PROBE_TIMEOUT_S)
    except FileNotFoundError:
        raise FootageError("ffmpeg_missing") from None
    except subprocess.TimeoutExpired:
        raise FootageError("footage_timeout") from None
    try:
        seconds = float(done.stdout.decode("ascii", "replace").strip()) if done.returncode == 0 else math.nan
    except ValueError:
        seconds = math.nan
    if not math.isfinite(seconds) or seconds < 0:
        log.warning("AE footage source has no duration")
        raise FootageError("footage_failed")
    return seconds


def _failure(out: Path):
    """The remembered failure code of a clip (expired ones are dropped); call with _STATE held."""
    failed = _failed.get(out)
    if failed is not None and time.monotonic() - failed[1] >= RETRY_S:
        del _failed[out]
        failed = None
    return failed and failed[0]


def _remember(out: Path, code: str):
    with _STATE:
        _failed[out] = (code, time.monotonic())
        _failed.move_to_end(out)
        while len(_failed) > FAILED_CAP:
            _failed.popitem(last=False)


def derive(src: Path, kind: str, fps: float, size: tuple[int, int], seconds: float, *, wait: bool = True) -> Path:
    """The AE footage for the clip at `src` ("plate" or "sprite"); raises FootageError with a code. Without `wait`
    it only looks up: a clip that is neither derived nor failed raises footage_pending."""
    src = Path(src)
    try:
        out = derived_path(src, kind)
    except OSError as exc:
        log.warning("AE footage source unreadable: %s", type(exc).__name__)
        raise FootageError("footage_failed") from None
    if _ready(out):
        return out
    with _STATE:
        if (code := _failure(out)) or not wait:
            raise FootageError(code or "footage_pending")
        while out in _active:   # another caller derives this clip: wait for its outcome
            _STATE.wait()
        if _ready(out):
            return out
        if code := _failure(out):
            raise FootageError(code)
        _active.add(out)
    try:
        real = probe_seconds(src)
        if max(real, seconds) > MAX_SECONDS:
            raise FootageError("footage_too_long")
        with _SLOTS:   # the timeout clock starts once a slot is free
            _encode_to(src, out, kind, fps, size, max(real, seconds))
        return out
    except FootageError as exc:
        _remember(out, str(exc))
        raise
    finally:
        with _STATE:
            _active.discard(out)
            _STATE.notify_all()


def _encode_to(src, out, kind, fps, size, seconds):
    suffix, decode, fmt, encode = FORMATS[kind]
    tmp = out.with_name(f".{out.name}.{os.getpid()}.{threading.get_ident()}.tmp")
    command = ["ffmpeg", "-y", "-loglevel", "error", *decode, "-t", repr(float(MAX_SECONDS)), "-i", str(src), "-an",
               "-vf", _SCALE.format(w=int(size[0]), h=int(size[1])) + fmt, "-r", repr(float(fps)), *encode,
               "-fs", str(MAX_BYTES), str(tmp)]
    started = time.monotonic()
    try:
        done = _encode(command, encode_timeout(seconds))
        if done.returncode != 0 or not tmp.is_file() or not tmp.stat().st_size:
            log.warning("AE footage encode failed: %s", done.stderr[-300:].decode("utf-8", "replace"))
            raise FootageError("footage_failed")
        if tmp.stat().st_size >= MAX_BYTES:   # -fs stops at the cap: the clip is cut short
            log.warning("AE footage %s reached the %d byte cap", kind, MAX_BYTES)
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
    file_sha256(out)   # prime the memo here, so the first spec request does not hash the clip
    log.info("ae footage %s %.2fs", kind, time.monotonic() - started)


def _background(src, kind, fps, size, seconds, out):
    try:
        derive(src, kind, fps, size, seconds)
    except FootageError:
        pass   # remembered in _failed; the spec reports its code
    except Exception:
        log.exception("AE footage derivation failed")
        _remember(out, "footage_failed")
    finally:
        with _STATE:
            if _threads.get(out) is threading.current_thread():
                del _threads[out]


def prepare(src: Path, kind: str, fps: float, size: tuple[int, int], seconds: float) -> bool:
    """Derive in the background (if needed); True while the clip is neither derived nor failed. With
    MAX_ENCODES + MAX_QUEUED derivers already running the clip stays pending until a later call finds room."""
    out = derived_path(Path(src), kind)
    if _ready(out):
        return False
    with _STATE:
        if _failure(out):
            return False
        if out not in _threads and len(_threads) < MAX_ENCODES + MAX_QUEUED:
            thread = threading.Thread(target=_background, args=(Path(src), kind, fps, size, seconds, out),
                                      name=f"keepframe-ae-footage-{out.name}", daemon=True)
            _threads[out] = thread
            thread.start()
    return True
