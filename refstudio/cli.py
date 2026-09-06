from __future__ import annotations
import argparse, json, sys
from pathlib import Path
from .compose.composer import compose
from .ir.store import load_scene, save_scene
from .ir.synth import make_synthetic_scene
from .render.renderer import render, render_result_from_json
from .verify.verifier import verify


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="refstudio")
    sub = ap.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("synth"); s.add_argument("--out", required=True); s.add_argument("--seed", type=int, default=1)
    s.add_argument("--frames", type=int, default=60); s.add_argument("--no-text", action="store_true")
    c = sub.add_parser("compose"); c.add_argument("--scene", required=True); c.add_argument("--out", required=True)
    r = sub.add_parser("render"); r.add_argument("--scene", required=True); r.add_argument("--html", required=True)
    r.add_argument("--out", required=True); r.add_argument("--frames", default=None); r.add_argument("--mp4", action="store_true")
    v = sub.add_parser("verify"); v.add_argument("--scene", required=True); v.add_argument("--render-json", default=None)
    v.add_argument("--reference", default=None)
    g = sub.add_parser("gate-m1"); g.add_argument("--out", required=True); g.add_argument("--n", type=int, default=20)
    a = ap.parse_args(argv)

    if a.cmd == "synth":
        out = Path(a.out)
        scene = make_synthetic_scene(out, seed=a.seed, frames=a.frames, with_text=not a.no_text)
        save_scene(scene, out / "scene.json"); print(out / "scene.json"); return 0
    if a.cmd == "compose":
        sp = Path(a.scene); print(compose(load_scene(sp), sp.parent, Path(a.out))); return 0
    if a.cmd == "render":
        sp = Path(a.scene); frames = [int(x) for x in a.frames.split(",")] if a.frames else None
        res = render(Path(a.html), load_scene(sp), Path(a.out), frames=frames, mp4=a.mp4)
        print(json.dumps({"frames": len(res.frames), "mp4": str(res.mp4) if res.mp4 else None})); return 0
    if a.cmd == "verify":
        sp = Path(a.scene); scene = load_scene(sp)
        rr = render_result_from_json(Path(a.render_json)) if a.render_json else None
        ref = load_scene(Path(a.reference)) if a.reference else None
        ref_dir = Path(a.reference).parent if a.reference else None
        rep = verify(scene, sp.parent, render_result=rr, reference=ref, reference_dir=ref_dir)
        print(rep.model_dump_json(indent=2)); return 0 if rep.passed else 1
    if a.cmd == "gate-m1":
        from .gates import m1_gate
        res = m1_gate(Path(a.out), n=a.n)
        for row in res["rows"]:
            print(row)
        print({k: v for k, v in res.items() if k != "rows"}); return 0 if res["passed"] else 1
    return 2


if __name__ == "__main__":
    sys.exit(main())
