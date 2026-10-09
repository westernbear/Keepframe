"""After Effects footage derived from Keepframe's WebM clips (AE cannot import WebM; Task 13 makes them).

A plate (opaque background) becomes H.264 `.ae.mp4`; a sprite (RGBA) becomes ProRes 4444 `.ae.mov`
(yuva444p10le, straight alpha). Each is derived lazily, on the first AE export that needs it, as a flat file
beside its source in the scene's assets, named by the source's SHA-256: an unchanged clip is never derived
again and a changed one gets a new name. Frames are converted from the WebM's BT.601 to tagged BT.709, the
matrix AE assumes for both codecs, and scaled to the poster's size (`size`), which places the layer.
"""
from __future__ import annotations

import hashlib
import os
import subprocess
import threading
import time
from pathlib import Path

from ..log import get

log = get("keepframe.ae")
TIMEOUT_S = 600
_LOCK = threading.Lock()
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


class FootageError(RuntimeError):
    """`str(error)` is a short code for clients: ffmpeg_missing, footage_failed or footage_timeout."""


def derived_path(src: Path, kind: str) -> Path:
    digest = hashlib.sha256(src.read_bytes()).hexdigest()
    return src.with_name(f"{src.name.removesuffix('.webm')}.{digest[:16]}{FORMATS[kind][0]}")


def derive(src: Path, kind: str, fps: float, size: tuple[int, int]) -> Path:
    """The AE footage for the clip at `src` ("plate" or "sprite"); raises FootageError with a code."""
    try:
        out = derived_path(Path(src), kind)
    except OSError as exc:
        log.warning("AE footage source unreadable: %s", type(exc).__name__)
        raise FootageError("footage_failed") from None
    if out.is_file() and out.stat().st_size:
        return out
    with _LOCK:   # one encode at a time; a concurrent request for the same clip then finds it cached
        if out.is_file() and out.stat().st_size:
            return out
        suffix, decode, fmt, encode = FORMATS[kind]
        tmp = out.with_name(f".{out.name}.{os.getpid()}.tmp")
        command = ["ffmpeg", "-y", "-loglevel", "error", *decode, "-i", str(src), "-an",
                   "-vf", _SCALE.format(w=int(size[0]), h=int(size[1])) + fmt, "-r", repr(float(fps)), *encode, str(tmp)]
        started = time.monotonic()
        try:
            done = subprocess.run(command, capture_output=True, timeout=TIMEOUT_S)
            if done.returncode != 0 or not tmp.is_file() or not tmp.stat().st_size:
                log.warning("AE footage encode failed: %s", done.stderr[-300:].decode("utf-8", "replace"))
                raise FootageError("footage_failed")
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
        return out
