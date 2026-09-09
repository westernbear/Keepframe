from __future__ import annotations
import math
import numpy as np
from ..log import get
from .composite import hex_to_rgb  # noqa: F401  (kept for parity with compositor colours)
from .device import resolve_device

log = get("keepframe.analyze")

# ponytail: full-frame L1 on downscaled frames. Upgrade path: per-element crops and a texture prior (Suzuki et al., ECCV 2024).

# Adam under-shoots translation at lr=0.02 when sx/sy/rot are tied; boost xy-only steps.
_TRANS_LR_SCALE = 3.5
# L2 pull on non-translation params so loss cannot be reduced by scale/rotation drift.
_INIT_REG = 1000.0


def torch_available() -> bool:
    try:
        import torch  # noqa: F401
        return True
    except ImportError:
        return False


def refine_affine(frames: np.ndarray, bg_rgb: tuple, raws: dict[str, np.ndarray], textures: dict[str, np.ndarray],
                  anchors: dict[str, tuple[float, float]], z: dict[str, int], iters: int = 200, lr: float = 0.02,
                  scale: float = 0.5, device: str | None = None) -> dict[str, np.ndarray]:
    if not torch_available():
        raise RuntimeError("torch is required for refine_affine (pip install 'keepframe[gpu]')")
    import torch, torch.nn.functional as F
    dev = resolve_device(device)
    N, H, W = frames.shape[:3]
    log.info("refine_affine device=%s frames=%s sprites=%s iters=%s", dev, N, len(raws), iters)
    h, w = int(round(H * scale)), int(round(W * scale))
    target = torch.tensor(frames, dtype=torch.float32, device=dev).permute(0, 3, 1, 2) / 255.0
    if scale != 1.0:
        target = F.interpolate(target, size=(h, w), mode="bilinear", align_corners=False)
    bg = torch.tensor(np.array(bg_rgb, np.float32) / 255.0, device=dev).view(1, 3, 1, 1)
    order = sorted(raws, key=lambda k: z[k])
    params, masks, texs, init = {}, {}, {}, {}
    for k in order:
        r = raws[k]
        valid = ~np.isnan(r[:, 0])
        p = torch.tensor(np.nan_to_num(r[:, [0, 1, 2, 3, 4, 7]]), dtype=torch.float32, device=dev)
        params[k] = p.clone().requires_grad_(True)
        init[k] = p.detach().clone()
        masks[k] = torch.tensor(valid, device=dev)
        t = torch.tensor(textures[k], dtype=torch.float32, device=dev).permute(2, 0, 1) / 255.0
        # Premultiply RGB by alpha before warp to match composite_scene / cv2 path.
        texs[k] = torch.cat([t[:3] * t[3:4], t[3:4]], 0)
    opt = torch.optim.Adam(list(params.values()), lr=lr * _TRANS_LR_SCALE)
    x_only_until = iters * 38 // 80 if iters >= 80 else iters // 2

    def theta_for(k, p):
        """Build the affine_grid theta (scene-normalised -> texture-normalised) for every frame."""
        th, tw = texs[k].shape[1:]
        ax, ay = anchors[k]
        x, y, sx, sy, rot, _ = p.unbind(1)
        r = rot * math.pi / 180
        cos, sin = torch.cos(r), torch.sin(r)
        # scene pixel = T(x,y) R S (tex_px - anchor_px);  invert: tex_px = S^-1 R^-1 (scene - t) + anchor_px
        a = torch.stack([torch.stack([cos / sx, sin / sx], 1), torch.stack([-sin / sy, cos / sy], 1)], 1)   # N,2,2
        # normalised coordinates: scene [-1,1] over (W,H); texture [-1,1] over (tw,th)
        S_scene = torch.tensor([[W / 2, 0.0], [0.0, H / 2]], device=dev)
        S_tex_inv = torch.tensor([[2 / tw, 0.0], [0.0, 2 / th]], device=dev)
        A = S_tex_inv @ a @ S_scene                                              # N,2,2
        # align_corners=False: scene px centre = (W/2)*g + (W-1)/2 ; texture normalised n = (2*p + 1)/tw - 1
        t_scene = torch.stack([(W - 1) / 2 - x, (H - 1) / 2 - y], 1).unsqueeze(2)   # N,2,1
        anchor_px = torch.tensor([ax * tw, ay * th], device=dev).view(1, 2, 1)
        half = torch.tensor([1.0 / tw - 1.0, 1.0 / th - 1.0], device=dev).view(1, 2, 1)
        b = S_tex_inv @ (a @ t_scene + anchor_px) + half
        return torch.cat([A, b], 2)                                             # N,2,3

    for step in range(iters):
        opt.zero_grad()
        canvas = bg.expand(N, 3, h, w).clone()
        for k in order:
            p = params[k]
            theta = theta_for(k, p)
            grid = F.affine_grid(theta, (N, 4, h, w), align_corners=False)
            samp = F.grid_sample(texs[k].unsqueeze(0).expand(N, -1, -1, -1), grid, align_corners=False, padding_mode="zeros")
            alpha = samp[:, 3:4] * p[:, 5].clamp(0, 1).view(N, 1, 1, 1) * masks[k].view(N, 1, 1, 1)
            canvas = canvas * (1 - alpha) + samp[:, :3] * alpha
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
    out = {}
    for k in order:
        r = raws[k].copy()
        p = params[k].detach().cpu().numpy()
        valid = ~np.isnan(r[:, 0])
        r[valid, 0] = p[valid, 0]; r[valid, 1] = p[valid, 1]; r[valid, 2] = p[valid, 2]; r[valid, 3] = p[valid, 3]
        r[valid, 4] = p[valid, 4]; r[valid, 7] = np.clip(p[valid, 5], 0, 1)
        out[k] = r
    return out
