"""Synthetic round trip (P2 Task 4, docs/superpowers/plans/2026-10-13-kfscript-p2-roundtrip.md): a reference page with
known truth → video → analysis → kf-script → render → diff against the truth.

Only `kf_parity` gates (the kf-script page must render like compose() does); every analysis number is recorded as the
baseline P3/P4 must beat. Gold rows (`NAME=CLIP,ROOT`) use docs/qa/kfscript/gold/NAME.truth.json and its hand-written page;
an existing project at ROOT is used as it is, never re-analysed."""
from __future__ import annotations

import json
import time
from pathlib import Path

from ..analyze.pipeline import AnalyzeOptions, analyze
from ..compose.composer import compose
from ..ir.store import current_scene, load_project, scene_dir
from ..kfscript import emit_page
from ..kfscript.check import fidelity
from ..kfscript.diff import diff
from ..kfscript.emit import view
from ..kfscript.synth import KINDS, make_template, probe_boxes, render_template, template_id
from .edits import _analyse

ROUNDTRIP_GATES = {"kf_parity": 0.001}   # |L1 kf-script page − L1 compose page|, per case
REPO = Path(__file__).resolve().parents[2]
GOLD = REPO / "docs" / "qa" / "kfscript" / "gold"
SYNTH_OPTIONS = AnalyzeOptions(refine=False, generate_3d=False)


def _measure(out: Path, name: str, page: Path, root: Path, truth: dict, seconds: float | None) -> dict:
    t0 = time.perf_counter()
    project = load_project(root)
    scenes = [(current_scene(root, ref.id)[0], ref.frames[0] - project.scenes[0].frames[0]) for ref in project.scenes]
    rows = {}
    for scene, start in scenes:
        sd = scene_dir(root, scene.id)
        kf = emit_page(scene, sd, out / "pages" / f"{name}.{scene.id}.kf.html")
        cp = compose(scene, sd, out / "pages" / f"{name}.{scene.id}.compose.html")
        rows[scene.id] = {"start": start, "kf": fidelity(root, scene.id, kf, sheet=out / "sheets" / f"{name}.{scene.id}.png"),
                          "compose": fidelity(root, scene.id, cp),
                          "recon": json.loads((sd / "report.json").read_text())["reconstruction"]["mean_l1"],
                          "view_chars": len(view(scene, sd))}
    first = scenes[0][0].id
    src = fidelity(root, first, page) if len(scenes) == 1 else None   # the reference page itself: codec floor / gold ceiling
    n = len(rows)
    parity = max(abs(r["kf"]["l1"] - r["compose"]["l1"]) for r in rows.values())
    d = diff(truth, scenes, view_chars=sum(r["view_chars"] for r in rows.values()), truth_chars=len(page.read_text()))
    return {"name": name, "kind": truth.get("kind", "gold"), "seed": truth.get("seed"), "frames": truth["frames"],
            "scenes": len(scenes), "l1_page": src["l1"] if src else None, "l1_recon": round(sum(r["recon"] for r in rows.values()) / n, 5),
            "l1_kf": round(sum(r["kf"]["l1"] for r in rows.values()) / n, 5),
            "l1_compose": round(sum(r["compose"]["l1"] for r in rows.values()) / n, 5), "kf_parity": round(parity, 7),
            "worst_kf": {sid: [round(r["kf"]["worst"], 4), r["kf"]["worst_frame"]] for sid, r in rows.items()},
            "analyse_seconds": round(seconds, 1) if seconds is not None else None,
            "measure_seconds": round(time.perf_counter() - t0, 1), "diff": d}


def _synthetic(out: Path, kind: str, seed: int, reuse: bool) -> dict:
    name = f"{kind}-{seed}"
    case = out / "cases" / name
    page, mp4, truth = case / "page.html", case / "source.mp4", case / "truth.json"
    fresh = not (reuse and mp4.exists() and truth.exists()
                 and json.loads(truth.read_text()).get("template") == template_id(make_template(kind, seed)))
    if fresh:
        render_template(kind, seed, case)
    t = json.loads(truth.read_text())
    seconds = _analyse(mp4, t["frames"], out / "proj" / name, SYNTH_OPTIONS, None, reuse and not fresh)
    return _measure(out, name, page, out / "proj" / name, t, seconds)


def _gold(out: Path, name: str, clip: Path, root: Path) -> dict:
    truth = json.loads((GOLD / f"{name}.truth.json").read_text())
    page = REPO / truth["page"]
    seconds = None
    if not (root / "project.json").exists():
        t0 = time.perf_counter()
        analyze(clip, 0, truth["frames"] - 1, root, AnalyzeOptions(generate_3d=False))
        seconds = time.perf_counter() - t0
    probe_boxes(page, truth, frames=False)
    (out / "cases").mkdir(parents=True, exist_ok=True)
    (out / "cases" / f"{name}.truth.json").write_text(json.dumps(truth, indent=1))
    return _measure(out, name, page, root, truth, seconds)


def _fmt(v, nd=4) -> str:
    return "—" if v is None else f"{v:.{nd}f}" if isinstance(v, float) else str(v)


def _summary(m: dict) -> str:
    lines = ["# kf-script round trip", "", f"Gate: kf_parity ≤ {ROUNDTRIP_GATES['kf_parity']} on every case → **{'pass' if m['passed'] else 'FAIL'}**. "
             "Everything else is recorded (P2 baseline; P3/P4 must beat it).", "",
             "| Case | Scenes | L1 page | L1 recon | L1 kf | Parity | Truth / IR els | Matched | Fragments | Pieces | Text | ΔE | Centre px | Visible err f | Structure found / missed / unsupported | DL ratio | Analyse s |",
             "|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|"]
    for r in m["cases"]:
        d, s = r["diff"], r["diff"]["structure"]
        lines.append(f"| {r['name']} | {r['scenes']} | {_fmt(r['l1_page'])} | {_fmt(r['l1_recon'])} | {_fmt(r['l1_kf'])} | {r['kf_parity']:.6f} | "
                     f"{d['truth_elements']} / {d['ir_elements']} | {_fmt(d['matched_rate'], 2)} | {_fmt(d['fragments_mean'], 2)} | {_fmt(d['pieces_mean'], 2)} | "
                     f"{_fmt(d['text_ratio'], 2)} | {_fmt(d['colour_de'], 1)} | {_fmt(d['centre_px'], 1)} | {_fmt(d['visible_err'], 1)} | "
                     f"{len(s['found'])} / {len(s['missed'])} / {len(s['unsupported'])} | {_fmt(d['dl_ratio'], 2)} | {_fmt(r['analyse_seconds'], 0)} |")
    return "\n".join(lines) + "\n"


def eval_roundtrip(out: Path, kinds=KINDS, seeds: int = 1, reuse: bool = False, gold: list[str] = (), first_seed: int = 1) -> dict:
    out = Path(out)
    out.mkdir(parents=True, exist_ok=True)
    cases = [_synthetic(out, k, s, reuse) for s in range(first_seed, first_seed + seeds) for k in kinds]
    for g in gold:
        name, paths = g.split("=", 1)
        clip, root = paths.split(",", 1)
        cases.append(_gold(out, name, Path(clip), Path(root)))
    m = {"gates": ROUNDTRIP_GATES, "passed": all(c["kf_parity"] <= ROUNDTRIP_GATES["kf_parity"] for c in cases), "cases": cases}
    (out / "metrics.json").write_text(json.dumps(m, indent=1, ensure_ascii=False))
    (out / "summary.md").write_text(_summary(m))
    return m
