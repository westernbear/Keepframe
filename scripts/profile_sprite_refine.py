import argparse
import json
import time
from contextlib import nullcontext
from pathlib import Path

import numpy as np
import torch
import torch.nn.functional as F

from keepframe.analyze.refine import refine_affine


def workload(frames, sprites, height, width, active_span):
    bg = (16, 20, 24)
    clip = np.empty((frames, height, width, 3), dtype=np.uint8)
    clip[:] = bg
    raws = {}
    textures = {}
    anchors = {}
    z = {}
    for i in range(sprites):
        key = f"s{i}"
        raw = np.full((frames, 8), np.nan, dtype=np.float32)
        start = i * max(1, frames - active_span) // max(sprites - 1, 1)
        end = min(frames, start + active_span)
        raw[start:end] = [width / 2, height / 2, 1, 1, 0, 0, 0, 1]
        tex = np.zeros((16, 16, 4), dtype=np.uint8)
        tex[2:14, 2:14, :3] = ((i * 67) % 220 + 35, (i * 43) % 220 + 35, (i * 29) % 220 + 35)
        tex[2:14, 2:14, 3] = 255
        raws[key] = raw
        textures[key] = tex
        anchors[key] = (0.5, 0.5)
        z[key] = i
    return clip, bg, raws, textures, anchors, z


def run(args, trace=None):
    clip, bg, raws, textures, anchors, z = workload(
        args.frames, args.sprites, args.height, args.width, min(args.active_span, args.frames),
    )
    sampled_batches = []
    original = F.grid_sample

    def counted(input, grid, *pos, **kwargs):
        sampled_batches.append(len(grid))
        return original(input, grid, *pos, **kwargs)

    F.grid_sample = counted
    try:
        context = nullcontext()
        profiler = None
        if trace:
            activities = [torch.profiler.ProfilerActivity.CPU]
            if args.device.startswith("cuda"):
                activities.append(torch.profiler.ProfilerActivity.CUDA)
            profiler = torch.profiler.profile(activities=activities, record_shapes=True, profile_memory=True)
            context = profiler
        if args.device.startswith("cuda"):
            torch.cuda.reset_peak_memory_stats()
        started = time.perf_counter()
        with context:
            refine_affine(
                clip, bg, raws, textures, anchors, z, iters=args.iters, scale=args.scale,
                device=args.device, checkpoint=args.checkpoint,
            )
        elapsed = time.perf_counter() - started
        if profiler:
            profiler.export_chrome_trace(str(trace))
        active_pairs = sum(np.count_nonzero(~np.isnan(raw[:, 0])) for raw in raws.values())
        result = {
            "seconds": round(elapsed, 3),
            "frames": args.frames,
            "sprites": args.sprites,
            "active_pairs": int(active_pairs),
            "density": round(active_pairs / max(args.frames * args.sprites, 1), 4),
            "grid_sample_calls": len(sampled_batches),
            "sampled_batch_total": sum(sampled_batches),
            "expected_sampled_batch_total": active_pairs * args.iters,
        }
        if args.device.startswith("cuda"):
            result["cuda_peak_allocated_gib"] = round(torch.cuda.max_memory_allocated() / 1024 ** 3, 3)
            result["cuda_peak_reserved_gib"] = round(torch.cuda.max_memory_reserved() / 1024 ** 3, 3)
        return result
    finally:
        F.grid_sample = original


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--frames", type=int, default=64)
    parser.add_argument("--sprites", type=int, default=32)
    parser.add_argument("--height", type=int, default=90)
    parser.add_argument("--width", type=int, default=160)
    parser.add_argument("--active-span", type=int, default=8)
    parser.add_argument("--iters", type=int, default=5)
    parser.add_argument("--scale", type=float, default=0.5)
    parser.add_argument("--device", default="cuda" if torch.cuda.is_available() else "cpu")
    parser.add_argument("--checkpoint", action=argparse.BooleanOptionalAction, default=None)
    parser.add_argument("--trace", type=Path)
    args = parser.parse_args()
    warmup = argparse.Namespace(**vars(args))
    warmup.frames = min(args.frames, 8)
    warmup.sprites = min(args.sprites, 4)
    warmup.active_span = min(args.active_span, warmup.frames)
    warmup.iters = 1
    warmup.trace = None
    run(warmup)
    print(json.dumps(run(args, args.trace), indent=2))


if __name__ == "__main__":
    main()
