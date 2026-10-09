"""Video layers (Task 13): a background that keeps moving and an element animating in place become VP9 WebM
(yuva420p for sprites) beside their poster PNG. Frames are piped to and from ffmpeg as raw RGB(A); nothing holds a
whole clip in memory. Chromium decodes VP9 (not H.264), so HTML and preview use these files as they are."""
from __future__ import annotations
import functools, os, shutil, subprocess, threading
from collections import OrderedDict
from pathlib import Path
import numpy as np
from ..log import get

log = get("keepframe.analyze")

CRF = 30
CRF_SMALL = 36            # re-encode at this when the first file is over MAX_BYTES
MAX_BYTES = 200 * 2 ** 20
CACHE = 8                 # decoded frames a reader keeps
BAND = 32                 # rows per pass of the per-frame plate fill
TIMEOUT_S = 600


@functools.lru_cache(maxsize=1)
def ffmpeg_vp9_ok() -> bool:
    """ffmpeg on PATH with the libvpx-vp9 encoder."""
    exe = shutil.which("ffmpeg")
    if exe is None:
        return False
    try:
        out = subprocess.run([exe, "-hide_banner", "-encoders"], capture_output=True, text=True, timeout=30).stdout
    except (OSError, subprocess.SubprocessError):
        return False
    return "libvpx-vp9" in out


def encode_webm(frames, fps: float, out, *, alpha: bool, crf: int = CRF) -> Path:
    """VP9 WebM from an iterable of H×W×3 (alpha: H×W×4) uint8 frames: libvpx-vp9, yuva420p | yuv420p, constant
    quality (`-b:v 0 -crf`), `-deadline good -cpu-used 4 -row-mt 1`. Written beside `out` and renamed into place;
    raises RuntimeError("encode_failed") on any error (nothing left behind)."""
    out = Path(out)
    tmp = out.with_name(f".{out.name}.tmp")
    it = iter(frames)
    first = np.ascontiguousarray(next(it), np.uint8)
    h, w = first.shape[:2]
    if first.shape != (h, w, 4 if alpha else 3):
        raise ValueError("frames must be H×W×4 (alpha) or H×W×3")
    cmd = ["ffmpeg", "-y", "-loglevel", "error", "-f", "rawvideo", "-pix_fmt", "rgba" if alpha else "rgb24",
           "-s", f"{w}x{h}", "-r", repr(float(fps)), "-i", "-", "-an", "-c:v", "libvpx-vp9",
           "-pix_fmt", "yuva420p" if alpha else "yuv420p", "-b:v", "0", "-crf", str(int(crf)),
           "-deadline", "good", "-cpu-used", "4", "-row-mt", "1", "-f", "webm", str(tmp)]
    proc = None
    try:
        proc = subprocess.Popen(cmd, stdin=subprocess.PIPE, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
        frame = first
        while frame is not None:
            if frame.shape != first.shape:
                raise ValueError("frames differ in size")
            proc.stdin.write(np.ascontiguousarray(frame, np.uint8).tobytes())
            frame = next(it, None)
        proc.stdin.close()
        err = proc.stderr.read()
        if proc.wait(timeout=TIMEOUT_S) != 0 or not tmp.is_file() or tmp.stat().st_size == 0:
            log.warning("webm encode failed: %s", err[-300:].decode("utf-8", "replace"))
            raise RuntimeError("encode_failed")
        os.replace(tmp, out)
        return out
    except Exception as e:
        if proc is not None and proc.poll() is None:
            proc.kill()
            proc.wait()
        tmp.unlink(missing_ok=True)
        if isinstance(e, RuntimeError) and str(e) == "encode_failed":
            raise
        raise RuntimeError("encode_failed") from e


def encode_capped(frames_fn, fps: float, out, *, alpha: bool) -> tuple[Path | None, str | None]:
    """encode_webm at CRF, again at CRF_SMALL when the file is over MAX_BYTES (`frames_fn()` yields the frames
    afresh). Returns (path, None), or (None, code) with no file left: "video_too_large", "encode_failed"."""
    out = Path(out)
    for crf in (CRF, CRF_SMALL):
        try:
            encode_webm(frames_fn(), fps, out, alpha=alpha, crf=crf)
        except RuntimeError:
            out.unlink(missing_ok=True)
            return None, "encode_failed"
        if out.stat().st_size <= MAX_BYTES:
            return out, None
    out.unlink(missing_ok=True)
    return None, "video_too_large"


class VideoReader:
    """Frames of a WebM as H×W×3 (alpha: ×4) uint8 at `size` (W, H): one ffmpeg rawvideo pipe read in order,
    restarted on a backward seek past the LRU cache of the last CACHE frames; past the end, the last frame."""

    def __init__(self, path, size, alpha: bool = False):
        self.path, self.size, self.alpha = Path(path), (int(size[0]), int(size[1])), bool(alpha)
        self.starts = 0
        self._proc = None
        self._next = 0
        self._last: np.ndarray | None = None
        self._end: int | None = None   # frame count, once the stream ended
        self._cache: OrderedDict[int, np.ndarray] = OrderedDict()
        self._lock = threading.Lock()

    def _start(self) -> None:
        self._stop()
        w, h = self.size
        cmd = ["ffmpeg", "-loglevel", "error", "-nostdin", *(["-c:v", "libvpx-vp9"] if self.alpha else []),   # the native
               "-i", str(self.path), "-map", "0:v:0", "-fps_mode", "passthrough", "-f", "rawvideo",             # vp9 decoder
               "-pix_fmt", "rgba" if self.alpha else "rgb24", "-s", f"{w}x{h}", "-"]                             # drops alpha
        self._proc = subprocess.Popen(cmd, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
        self._next = 0
        self.starts += 1

    def _stop(self) -> None:
        if self._proc is not None:
            if self._proc.poll() is None:
                self._proc.kill()
            self._proc.stdout.close()
            self._proc.wait()
            self._proc = None

    def _read(self) -> np.ndarray | None:
        w, h = self.size
        c = 4 if self.alpha else 3
        n = w * h * c
        buf = self._proc.stdout.read(n)
        if len(buf) < n:
            self._stop()
            return None
        return np.frombuffer(buf, np.uint8).reshape(h, w, c)

    def frame(self, i: int) -> np.ndarray:
        i = max(0, int(i))
        with self._lock:
            if self._end is not None:
                i = min(i, self._end - 1)
            hit = self._cache.get(i)
            if hit is not None:
                self._cache.move_to_end(i)
                return hit
            if self._proc is None or i < self._next:
                self._start()
            while self._next <= i:
                img = self._read()
                if img is None:   # past the end: the last frame
                    if self._next == 0:
                        raise RuntimeError("decode_failed")
                    self._end = self._next
                    return self._last
                self._last = img
                self._cache[self._next] = img
                while len(self._cache) > CACHE:
                    self._cache.popitem(last=False)
                self._next += 1
            return self._last

    def close(self) -> None:
        with self._lock:
            self._stop()

    def __enter__(self):
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    def __del__(self):
        try:
            self._stop()
        except Exception:
            pass


def fill_video_plate(frames, occ_all, hole_fill, out, *, band: int = BAND) -> np.memmap:
    """The plate behind every frame, written to `out` (.npy, opened as a memmap): a pixel uncovered in frame f is
    that frame's; a covered one takes the nearest frame (in time) that shows it, the earlier on a tie, found by a
    forward and a backward pass per band of rows; a pixel no frame shows keeps `hole_fill` (the still plate)."""
    n, H, W = frames.shape[:3]
    plate = np.lib.format.open_memmap(Path(out), mode="w+", dtype=np.uint8, shape=(n, H, W, 3))
    big = np.iinfo(np.int32).max // 2
    for y0 in range(0, H, band):
        y1 = min(H, y0 + band)
        occ = np.asarray(occ_all[:, y0:y1], bool)
        dist = np.empty((n, y1 - y0, W), np.int32)
        seen = np.array(hole_fill[y0:y1], np.uint8)
        at = np.full((y1 - y0, W), -big, np.int64)
        for f in range(n):
            free = ~occ[f]
            seen[free] = frames[f, y0:y1][free]
            at[free] = f
            plate[f, y0:y1] = seen
            dist[f] = np.minimum(f - at, big)
        seen = np.array(hole_fill[y0:y1], np.uint8)
        at = np.full((y1 - y0, W), 2 * big, np.int64)
        for f in range(n - 1, -1, -1):
            free = ~occ[f]
            seen[free] = frames[f, y0:y1][free]
            at[free] = f
            nearer = (at - f) < dist[f]
            if nearer.any():
                plate[f, y0:y1][nearer] = seen[nearer]
    plate.flush()
    return plate
