from __future__ import annotations
import hashlib, json, shutil, subprocess
from dataclasses import dataclass, asdict
from pathlib import Path
import cv2, numpy as np
from playwright.sync_api import sync_playwright
from ..ir.schema import Scene


@dataclass
class RenderResult:
    frames_dir: Path
    frames: list[int]
    hashes: list[str]
    bboxes: dict[str, list[list[float]]]
    mp4: Path | None = None


def frame_hash(png_bytes: bytes) -> str:
    arr = cv2.imdecode(np.frombuffer(png_bytes, np.uint8), cv2.IMREAD_COLOR)
    return hashlib.sha256(np.ascontiguousarray(arr).tobytes()).hexdigest()


def load_frame(path: Path) -> np.ndarray:
    bgr = cv2.imread(str(path), cv2.IMREAD_COLOR)
    return cv2.cvtColor(bgr, cv2.COLOR_BGR2RGB).astype(np.float32) / 255.0


def write_mp4(frames_dir: Path, fps: float, out: Path) -> Path:
    if shutil.which("ffmpeg") is None:
        raise RuntimeError("ffmpeg not found on PATH")
    subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-framerate", str(fps), "-i", str(frames_dir / "f_%05d.png"),
                    "-c:v", "libx264", "-pix_fmt", "yuv420p", "-crf", "18", str(out)], check=True)
    return out


def render(html: Path, scene: Scene, out_dir: Path, frames: list[int] | None = None,
           mp4: bool = False, probe: bool = True) -> RenderResult:
    out_dir = Path(out_dir)
    frames_dir = out_dir / "frames"
    if frames_dir.exists():
        shutil.rmtree(frames_dir)
    frames_dir.mkdir(parents=True)
    frames = list(range(scene.frames)) if frames is None else list(frames)
    W, H = scene.size
    ids = [e.id for e in scene.elements]
    hashes: list[str] = []
    bboxes: dict[str, list[list[float]]] = {i: [] for i in ids}
    with sync_playwright() as p:
        browser = p.chromium.launch(args=["--disable-gpu", "--hide-scrollbars", "--force-color-profile=srgb"])
        page = browser.new_page(viewport={"width": W, "height": H}, device_scale_factor=1)
        page.goto(Path(html).resolve().as_uri())
        page.wait_for_function("window.__ready === true")
        for i, f in enumerate(frames):
            page.evaluate("f => window.__seek(f)", f)
            png = page.screenshot(type="png", clip={"x": 0, "y": 0, "width": W, "height": H}, animations="disabled")
            (frames_dir / f"f_{i:05d}.png").write_bytes(png)
            hashes.append(frame_hash(png))
            if probe:
                rects = page.evaluate("ids => ids.map(i => window.__bbox(i))", ids)
                for eid, r in zip(ids, rects):
                    bboxes[eid].append([float(v) for v in r])
        browser.close()
    result = RenderResult(frames_dir=frames_dir, frames=frames, hashes=hashes, bboxes=bboxes)
    if mp4:
        result.mp4 = write_mp4(frames_dir, scene.fps, out_dir / "render.mp4")
    (out_dir / "render.json").write_text(json.dumps({**asdict(result), "frames_dir": str(frames_dir),
                                                      "mp4": str(result.mp4) if result.mp4 else None}, indent=2))
    return result


def render_result_from_json(path: Path) -> RenderResult:
    d = json.loads(Path(path).read_text())
    return RenderResult(frames_dir=Path(d["frames_dir"]), frames=d["frames"], hashes=d["hashes"],
                        bboxes=d["bboxes"], mp4=Path(d["mp4"]) if d.get("mp4") else None)
