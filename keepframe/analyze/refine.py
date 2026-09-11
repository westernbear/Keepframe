from __future__ import annotations
import contextlib
import gc
import math
import os
import time
from concurrent.futures import ThreadPoolExecutor
import cv2
import numpy as np
from ..log import get
from ..progress import report_stage
from .composite import hex_to_rgb  # noqa: F401  (kept for parity with compositor colours)
from .device import resolve_device

log = get("keepframe.analyze")

# ponytail: full-frame L1 on downscaled frames. Upgrade path: per-element crops and a texture prior (Suzuki et al., ECCV 2024).

# Adam under-shoots translation at lr=0.02 when sx/sy/rot are tied; boost xy-only steps.
_TRANS_LR_SCALE = 3.5
# L2 pull on non-translation params so loss cannot be reduced by scale/rotation drift.
_INIT_REG = 1000.0
# Autograd keeps canvas/grid/sample per sprite. ~12 float channels per sprite per pixel.
_BYTES_PER_SPRITE_PX = 12 * 4
# Per-sprite checkpoint saves the RGB canvas (fp32) and recomputes one warp at a time.
_BYTES_PER_CKPT_PX = 3 * 4
_VRAM_FRAC = 0.75


def torch_available() -> bool:
    try:
        import torch  # noqa: F401
        return True
    except ImportError:
        return False


def _downscale_cpu(frames: np.ndarray, h: int, w: int) -> np.ndarray:
    if frames.shape[1] == h and frames.shape[2] == w:
        return frames
    out = np.empty((len(frames), h, w, frames.shape[3]), dtype=frames.dtype)
    workers = max(1, min(8, os.cpu_count() or 4, len(frames)))
    if workers == 1:
        for i, f in enumerate(frames):
            out[i] = cv2.resize(f, (w, h), interpolation=cv2.INTER_AREA)
        return out
    # cv2.resize releases the GIL, so the sequential resize fans out across cores.
    with ThreadPoolExecutor(max_workers=workers) as ex:
        futs = [ex.submit(cv2.resize, f, (w, h), interpolation=cv2.INTER_AREA) for f in frames]
        for i, fut in enumerate(futs):
            out[i] = fut.result()
    return out


def _cuda_mem_info() -> tuple[int, int]:
    import torch
    try:
        torch.cuda.empty_cache()
        return torch.cuda.mem_get_info()
    except Exception:
        return 4 * 1024 ** 3, 4 * 1024 ** 3


def _chunk_from_free(N: int, per: int, free: int) -> int:
    # Floor of 8 avoids the chunk=1 mode where a fresh 200-iter Adam run, H2D upload
    # and per-sprite D2H syncs are paid once per frame; OOM halving remains the backstop.
    return min(N, max(8, int(free * _VRAM_FRAC / max(per, 1))))


def _bytes_per_frame(h: int, w: int, n_sprites: int, ckpt: bool) -> int:
    n = max(n_sprites, 1)
    live = _BYTES_PER_SPRITE_PX * h * w
    if ckpt:
        return n * _BYTES_PER_CKPT_PX * h * w + live
    return n * live


def _frame_plan(N: int, h: int, w: int, n_sprites: int, dev: str) -> tuple[int, bool]:
    """Frames per shot and whether to checkpoint sprite warps. CPU stays one shot."""
    if N <= 1 or not str(dev).startswith("cuda"):
        return N, False
    free, _ = _cuda_mem_info()
    chunk = _chunk_from_free(N, _bytes_per_frame(h, w, n_sprites, False), free)
    if chunk >= N:
        return N, False
    return _chunk_from_free(N, _bytes_per_frame(h, w, n_sprites, True), free), True


def _frame_chunk(N: int, h: int, w: int, n_sprites: int, dev: str) -> int:
    """How many frames fit in free VRAM. CPU and tiny clips stay one shot."""
    return _frame_plan(N, h, w, n_sprites, dev)[0]


def refine_affine(frames: np.ndarray, bg_rgb: tuple, raws: dict[str, np.ndarray], textures: dict[str, np.ndarray],
                  anchors: dict[str, tuple[float, float]], z: dict[str, int], iters: int = 200, lr: float = 0.02,
                  scale: float = 0.5, device: str | None = None, checkpoint: bool | None = None) -> dict[str, np.ndarray]:
    if not torch_available():
        raise RuntimeError("torch is required for refine_affine (pip install 'keepframe[gpu]')")
    import torch
    dev = resolve_device(device)
    N, H, W = frames.shape[:3]
    h, w = int(round(H * scale)), int(round(W * scale))
    work = _downscale_cpu(frames, h, w)
    consts = _upload_consts(dev, bg_rgb, raws, textures, anchors, z, H, W)
    chunk, auto_ckpt = _frame_plan(N, h, w, len(raws), dev)
    if checkpoint is not None:
        auto_ckpt = checkpoint
    cuda = str(dev).startswith("cuda")
    free, _ = _cuda_mem_info() if cuda else (0, 0)
    per = _bytes_per_frame(h, w, len(raws), auto_ckpt)
    log.info(
        "refine_affine device=%s frames=%s %sx%s work=%sx%s sprites=%s iters=%s chunk=%s ckpt=%s free=%.1fGiB per_frame=%.2fGiB",
        dev, N, H, W, h, w, len(raws), iters, chunk, auto_ckpt, free / 1024 ** 3, per / 1024 ** 3,
    )
    while chunk >= 1:
        try:
            return _refine_chunks(work, consts, raws, iters, lr, dev, H, W, h, w, chunk, auto_ckpt)
        except RuntimeError as e:
            if not cuda or "out of memory" not in str(e).lower() or chunk == 1:
                raise
            log.warning("refine OOM chunk=%s; retry half", chunk)
            gc.collect()
            torch.cuda.empty_cache()
            chunk = max(1, chunk // 2)
            auto_ckpt = True


def _upload_consts(dev, bg_rgb: tuple, raws: dict[str, np.ndarray], textures: dict[str, np.ndarray],
                   anchors: dict[str, tuple[float, float]], z: dict[str, int], H: int, W: int) -> dict:
    """Upload textures/anchor constants once for the whole refine; chunks reuse them."""
    import torch
    order = sorted(raws, key=lambda k: z[k])
    texs, tex_const = {}, {}
    for k in order:
        t = torch.tensor(textures[k], dtype=torch.float32, device=dev).permute(2, 0, 1) / 255.0
        # Premultiply RGB by alpha before warp to match composite_scene / cv2 path.
        texs[k] = torch.cat([t[:3] * t[3:4], t[3:4]], 0)
        th, tw = texs[k].shape[1:]
        ax, ay = anchors[k]
        tex_const[k] = (
            torch.tensor([[2 / tw, 0.0], [0.0, 2 / th]], device=dev),
            torch.tensor([ax * tw, ay * th], device=dev).view(1, 2, 1),
            torch.tensor([1.0 / tw - 1.0, 1.0 / th - 1.0], device=dev).view(1, 2, 1),
        )
    return {
        "order": order,
        "bg": torch.tensor(np.array(bg_rgb, np.float32) / 255.0, device=dev).view(1, 3, 1, 1),
        "s_scene": torch.tensor([[W / 2, 0.0], [0.0, H / 2]], device=dev),
        "texs": texs,
        "tex_const": tex_const,
    }


def _refine_chunks(work, consts, raws, iters, lr, dev, H, W, h, w, chunk, checkpoint):
    N = len(work)
    if chunk >= N:
        return _refine_chunk(work, consts, raws, iters, lr, dev, H, W, h, w, checkpoint)
    out = {k: r.copy() for k, r in raws.items()}
    n_parts = math.ceil(N / chunk)
    t0 = time.perf_counter()
    for i, s in enumerate(range(0, N, chunk)):
        e = min(N, s + chunk)
        report_stage("sprites", f"refine {i + 1}/{n_parts} frames {s + 1}-{e}/{N}")
        part = _refine_chunk(
            work[s:e], consts, {k: r[s:e] for k, r in raws.items()},
            iters, lr, dev, H, W, h, w, checkpoint,
        )
        for k, r in part.items():
            out[k][s:e] = r
    log.info("refine chunks=%s frames=%s chunk=%s %.2fs", n_parts, N, chunk, time.perf_counter() - t0)
    return out


def _refine_chunk(frames: np.ndarray, consts: dict, raws: dict[str, np.ndarray],
                   iters: int, lr: float, dev: str, H: int, W: int, h: int, w: int,
                   checkpoint: bool = False) -> dict[str, np.ndarray]:
    import torch, torch.nn.functional as F
    from torch.utils.checkpoint import checkpoint as ckpt
    N = len(frames)
    cuda = str(dev).startswith("cuda")
    order = consts["order"]
    target = torch.tensor(frames, dtype=torch.float32, device=dev).permute(0, 3, 1, 2) / 255.0
    params, masks, init = {}, {}, {}
    for k in order:
        r = raws[k]
        valid = ~np.isnan(r[:, 0])
        p = torch.tensor(np.nan_to_num(r[:, [0, 1, 2, 3, 4, 7]]), dtype=torch.float32, device=dev)
        params[k] = p.clone().requires_grad_(True)
        init[k] = p.detach().clone()
        masks[k] = torch.tensor(valid, device=dev)
    texs, tex_const = consts["texs"], consts["tex_const"]
    bg, s_scene = consts["bg"], consts["s_scene"]
    opt = torch.optim.Adam(list(params.values()), lr=lr * _TRANS_LR_SCALE)
    x_only_until = iters * 38 // 80 if iters >= 80 else iters // 2
    amp_dtype = None
    if cuda:
        amp_dtype = torch.bfloat16 if torch.cuda.is_bf16_supported() else torch.float16
    amp = torch.autocast(device_type="cuda", dtype=amp_dtype) if amp_dtype is not None else contextlib.nullcontext()

    def theta_for(k, p):
        """Build the affine_grid theta (scene-normalised -> texture-normalised) for every frame."""
        x, y, sx, sy, rot, _ = p.unbind(1)
        r = rot * math.pi / 180
        cos, sin = torch.cos(r), torch.sin(r)
        # scene pixel = T(x,y) R S (tex_px - anchor_px);  invert: tex_px = S^-1 R^-1 (scene - t) + anchor_px
        a = torch.stack([torch.stack([cos / sx, sin / sx], 1), torch.stack([-sin / sy, cos / sy], 1)], 1)   # N,2,2
        S_tex_inv, anchor_px, half = tex_const[k]
        A = S_tex_inv @ a @ s_scene                                              # N,2,2
        t_scene = torch.stack([(W - 1) / 2 - x, (H - 1) / 2 - y], 1).unsqueeze(2)   # N,2,1
        b = S_tex_inv @ (a @ t_scene + anchor_px) + half
        return torch.cat([A, b], 2)                                             # N,2,3

    def stamp(canvas, p, tex, mask, k):
        theta = theta_for(k, p)
        grid = F.affine_grid(theta, (N, 4, h, w), align_corners=False)
        samp = F.grid_sample(tex.unsqueeze(0).expand(N, -1, -1, -1), grid, align_corners=False, padding_mode="zeros")
        alpha = samp[:, 3:4] * p[:, 5].clamp(0, 1).view(N, 1, 1, 1) * mask.view(N, 1, 1, 1)
        return canvas * (1 - alpha) + samp[:, :3] * alpha

    for step in range(iters):
        opt.zero_grad(set_to_none=True)
        with amp:
            canvas = bg.expand(N, 3, h, w).clone()
            for k in order:
                if checkpoint:
                    canvas = ckpt(stamp, canvas, params[k], texs[k], masks[k], k, use_reentrant=False)
                else:
                    canvas = stamp(canvas, params[k], texs[k], masks[k], k)
            loss = (canvas - target).abs().mean()
            for k in order:
                d = params[k] - init[k]
                loss = loss + _INIT_REG * (
                    d[:, 2].pow(2).mean() + d[:, 3].pow(2).mean() + d[:, 4].pow(2).mean() + d[:, 5].pow(2).mean()
                )
        loss.backward()
        for k in order:
            g = params[k].grad
            if g is None:
                continue
            g[:, 2:] = 0
            if step < x_only_until:
                g[:, 1] = 0
        opt.step()
    # One stacked D2H sync instead of one per sprite.
    out = {}
    P = torch.stack([params[k] for k in order]).detach().cpu().numpy() if order else None
    for i, k in enumerate(order):
        r = raws[k].copy()
        p = P[i]
        valid = ~np.isnan(r[:, 0])
        r[valid, 0] = p[valid, 0]; r[valid, 1] = p[valid, 1]; r[valid, 2] = p[valid, 2]; r[valid, 3] = p[valid, 3]
        r[valid, 4] = p[valid, 4]; r[valid, 7] = np.clip(p[valid, 5], 0, 1)
        out[k] = r
    return out
