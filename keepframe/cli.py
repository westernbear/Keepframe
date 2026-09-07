from __future__ import annotations
import argparse, json, sys
from pathlib import Path
from .compose.composer import compose
from .ir.store import load_scene, save_scene
from .ir.synth import make_synthetic_scene
from .render.renderer import render, render_result_from_json
from .verify.verifier import verify


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="keepframe")
    sub = ap.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("synth"); s.add_argument("--out", required=True); s.add_argument("--seed", type=int, default=1)
    s.add_argument("--frames", type=int, default=60); s.add_argument("--no-text", action="store_true")
    c = sub.add_parser("compose"); c.add_argument("--scene", required=True); c.add_argument("--out", required=True)
    r = sub.add_parser("render"); r.add_argument("--scene", required=True); r.add_argument("--html", required=True)
    r.add_argument("--out", required=True); r.add_argument("--frames", default=None); r.add_argument("--mp4", action="store_true")
    v = sub.add_parser("verify"); v.add_argument("--scene", required=True); v.add_argument("--render-json", default=None)
    v.add_argument("--reference", default=None)
    g = sub.add_parser("gate-m1"); g.add_argument("--out", required=True); g.add_argument("--n", type=int, default=20)
    an = sub.add_parser("analyze"); an.add_argument("--video", required=True); an.add_argument("--start", type=int, default=0)
    an.add_argument("--end", type=int, required=True); an.add_argument("--out", required=True); an.add_argument("--copy", default=None)
    an.add_argument("--no-ocr", action="store_true"); an.add_argument("--no-refine", action="store_true"); an.add_argument("--bg", default=None)
    co = sub.add_parser("correct"); co.add_argument("--root", required=True); co.add_argument("--scene", default="s1")
    co.add_argument("--op", required=True, choices=["reassign", "mask", "bbox", "text"]); co.add_argument("--args", required=True)
    g2 = sub.add_parser("gate-m2"); g2.add_argument("--out", required=True); g2.add_argument("--n", type=int, default=20)
    g2r = sub.add_parser("gate-m2-real"); g2r.add_argument("--clips", required=True); g2r.add_argument("--out", required=True)
    sv = sub.add_parser("serve"); sv.add_argument("--workspace", required=True); sv.add_argument("--port", type=int, default=8765)
    sv.add_argument("--host", default="127.0.0.1")
    sv.add_argument("--admin", action="store_true")
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
    if a.cmd == "analyze":
        from .analyze.pipeline import AnalyzeOptions, analyze
        opts = AnalyzeOptions(bg_override=a.bg, copy=a.copy.split(",") if a.copy else None, ocr=not a.no_ocr, refine=not a.no_refine)
        p = analyze(Path(a.video), a.start, a.end, Path(a.out), opts)
        print(json.dumps({"scenes": [s.id for s in p.scenes], "version": p.versions[-1].id})); return 0
    if a.cmd == "correct":
        from .review import corrections as C
        from .ir.schema import FontGuess
        kw = json.loads(a.args)
        if a.op == "reassign":
            v = C.reassign_id(Path(a.root), a.scene, tuple(kw["frames"]), kw["from_id"], kw["to_id"], note=kw.get("note", "reassign id"))
        elif a.op == "mask":
            v = C.set_region_mask(Path(a.root), a.scene, kw["frame"], Path(kw["mask_png"]), kw["object_id"], note=kw.get("note", "set region mask"))
        elif a.op == "bbox":
            v = C.add_bbox_prompt(Path(a.root), a.scene, kw["frame"], tuple(kw["bbox"]), kw["object_id"], note=kw.get("note", "bbox prompt"))
        else:
            v = C.edit_text(Path(a.root), a.scene, kw["element_id"], text=kw.get("text"),
                            font=FontGuess(**kw["font"]) if kw.get("font") else None, note=kw.get("note", "edit text"))
        print(v.model_dump_json(indent=2)); return 0
    if a.cmd == "gate-m2":
        from .gates import m2_gate
        res = m2_gate(Path(a.out), n=a.n)
        for row in res["rows"]:
            print(row)
        print({k: v for k, v in res.items() if k != "rows"}); return 0 if res["passed"] else 1
    if a.cmd == "gate-m2-real":
        from .gates import m2_gate_real
        print(json.dumps(m2_gate_real(Path(a.clips), Path(a.out)), indent=2)); return 0
    if a.cmd == "serve":
        from .web.server import make_server
        host = a.host
        kwargs: dict = {"admin": a.admin}
        if a.admin:
            from .admin.memory import MemoryAdmin
            from .admin.auth import MemoryAuth
            kwargs["admin_svc"] = MemoryAdmin()
            kwargs["admin_auth"] = MemoryAuth()
        srv = make_server(Path(a.workspace), port=a.port, host=host, **kwargs)
        print(f"http://{host}:{a.port}/")
        if a.admin:
            print(f"http://{host}:{a.port}/admin/")
        srv.serve_forever()
        return 0
    return 2


if __name__ == "__main__":
    sys.exit(main())
