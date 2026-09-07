from __future__ import annotations

import os


def _wanted_device(explicit: str | None = None) -> str:
    raw = explicit if explicit is not None else os.environ.get("KEEPFRAME_DEVICE")
    return (raw or "").strip().lower()


def _device_required() -> bool:
    return _wanted_device() in ("cuda", "gpu")


def _cuda_available() -> bool:
    try:
        import torch
        return bool(torch.cuda.is_available())
    except ImportError:
        return False


def gpu_status() -> dict:
    """What the process can actually run refine on. UI must not hardcode this."""
    wanted = _wanted_device()
    required = wanted in ("cuda", "gpu")
    try:
        import torch
    except ImportError:
        return {
            "torch": False,
            "cuda": False,
            "device": "cpu",
            "name": "cpu",
            "required": required,
            "torch_version": None,
        }
    cuda = bool(torch.cuda.is_available())
    if wanted == "cpu":
        device, name = "cpu", "cpu"
    elif cuda:
        device, name = "cuda", torch.cuda.get_device_name(0)
    else:
        device, name = "cpu", "cpu"
    return {
        "torch": True,
        "cuda": cuda,
        "device": device,
        "name": name,
        "required": required,
        "torch_version": torch.__version__,
    }


def resolve_device(explicit: str | None = None) -> str:
    """Pick torch device. KEEPFRAME_DEVICE=cuda (GPU overlay) does not fall back to CPU."""
    wanted = _wanted_device(explicit)
    if wanted == "cpu":
        return "cpu"
    cuda = _cuda_available()
    if wanted in ("cuda", "gpu"):
        if not cuda:
            raise RuntimeError("KEEPFRAME_DEVICE=cuda but torch.cuda.is_available() is False")
        return "cuda"
    return "cuda" if cuda else "cpu"
