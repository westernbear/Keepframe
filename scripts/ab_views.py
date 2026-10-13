"""Blind A/B of the agent's scene views: brief vs kf-script (P1 Task 7, docs/superpowers/plans/2026-10-13-kfscript-p1-view.md).

  packets --root eval/kf --out docs/qa/kfscript/ab     questions.json, packets/<hash>.md, index.json (hash → clip, view)
  score   --dir docs/qa/kfscript/ab                     answers/<hash>.json → results.json + summary.md
  run     --dir docs/qa/kfscript/ab                     answer every packet with make_llm() (needs credentials)
  synth-gold --rt eval/out/rt-base --out docs/qa/kfscript/ab-synth   read_gold.json from round-trip truth (fresh clips)

Questions per clip: auto-gold from scene.json (visibility, end of last move, glyph reveal, ease, largest text, z order),
motion detail from scene.json (entrance, order, move duration, position and opacity at a held frame) and read-gold from
read_gold.json (ids of things the agent's read names; edit targets). Packets never name the view.
Decision (pre-registered 2026-10-13, docs/qa/kfscript/ab-test-prereg.md): kf-script becomes the default view when
H1 (comprehension kf − brief, paired, clip-clustered bootstrap 95 % CI lower bound > 0) and H2 (edit-target
accuracy kf − brief, same interval, lower bound > −10 points) hold. The P1 rule (point estimate + 10) is reported."""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import re
from itertools import groupby
from pathlib import Path

import numpy as np

from keepframe.analyze.keyframes import ERR
from keepframe.ir.colour import delta_e, hex_to_rgb8, srgb8_to_lab
from keepframe.ir.importance import rank_elements
from keepframe.ir.schema import Scene
from keepframe.ir.store import current_scene, scene_dir
from keepframe.ir.tracks import PRESET_EASES, element_bbox, eval_props, eval_track, eval_z
from keepframe.kfscript.emit import view
from keepframe.session.agent import SYSTEM
from keepframe.session.brief import scene_brief

EASES = {tuple(v): k for k, v in PRESET_EASES.items()}
VIEWS = ("brief", "kfscript")


def _ease(e) -> str:
    return "linear" if e is None else EASES.get(tuple(e), "custom")


def _last_move(el) -> int | None:
    ends = [b.t for p, tr in el.tracks.items() for a, b in zip(tr.keys, tr.keys[1:]) if abs(b.v - a.v) > ERR.get(p, 0.01)]
    return max(ends) if ends else None


def auto_questions(scene: Scene) -> list[dict]:
    """Questions whose answers come from the IR itself, so both views are judged against the same truth."""
    fps, ranked, qs = scene.fps, rank_elements(scene)[:12], []
    t = scene.frames // 2
    on = [e for e in ranked if e.visible[0] <= t <= e.visible[1]]
    off = [e for e in ranked if not e.visible[0] <= t <= e.visible[1]]
    for e, ans in ((on[0], "yes") if on else (None, None), (off[0], "no") if off else (None, None)):
        if e:
            qs.append({"cat": "auto", "kind": "yesno", "q": f"Is element {e.id} visible at t = {t / fps:.2f} s? (yes/no)", "gold": ans})
    mover = next(((e, f) for e in ranked if (f := _last_move(e)) is not None), None)
    if mover:
        qs.append({"cat": "auto", "kind": "number", "q": f"At what time (seconds) does element {mover[0].id} finish its last movement?",
                   "gold": mover[1] / fps, "tol": 2 / fps})
    glyphs = [e.id for e in scene.elements if e.canonical.glyphs]
    qs.append({"cat": "auto", "kind": "idset", "q": "Which element ids reveal their text glyph by glyph (typing)? (list, or [] if none)",
               "gold": glyphs})
    best = None
    for e in ranked:
        for p in ("x", "y"):
            tr = e.tracks.get(p)
            for a, b in zip(tr.keys, tr.keys[1:]) if tr else ():
                if abs(b.v - a.v) > 10 and (best is None or abs(b.v - a.v) > best[0]):
                    best = (abs(b.v - a.v), e.id, p, _ease(a.ease))
    if best:
        axis = "horizontal" if best[2] == "x" else "vertical"
        qs.append({"cat": "auto", "kind": "exact", "q": f"Which easing does element {best[1]}'s biggest {axis} move use? "
                   f"(one of: {', '.join(PRESET_EASES)}, custom)", "gold": best[3]})
    texts = [e for e in scene.elements if e.kind == "text" and e.canonical.text]
    if texts:
        big = max(texts, key=lambda e: e.canonical.width * e.canonical.height)
        qs.append({"cat": "auto", "kind": "text", "q": "What is the exact text of the largest text element (largest box area, width × height)?", "gold": big.canonical.text})
        if big.canonical.color:
            qs.append({"cat": "auto", "kind": "colour", "q": "What is the colour (#rrggbb) of the largest text element (largest box area, width × height)?", "gold": big.canonical.color})
    for f in range(0, scene.frames, max(1, scene.frames // 10)):
        vis = [e for e in ranked if e.visible[0] <= f <= e.visible[1]]
        pair = next(((a, b) for i, a in enumerate(vis) for b in vis[i + 1:] if eval_z(a, f) != eval_z(b, f)), None)
        if pair:
            a, b = pair
            qs.append({"cat": "auto", "kind": "id", "q": f"At t = {f / fps:.2f} s, which element is in front: {a.id} or {b.id}?",
                       "gold": [a.id if eval_z(a, f) > eval_z(b, f) else b.id]})
            break
    return qs


def _moves(el, p: str):
    """Moves on one axis: maximal runs of consecutive key segments each over ERR in the same direction, as
    (|total|, start frame, end frame)."""
    out, run = [], None
    for a, b in zip(el.tracks[p].keys, el.tracks[p].keys[1:]) if p in el.tracks else ():
        d = b.v - a.v
        if abs(d) <= ERR.get(p, 0.01):
            run = None
            continue
        if run and (d > 0) == (run[3] > 0):
            run[0], run[2], run[3] = run[0] + abs(d), b.t, run[3] + d
        else:
            run = [abs(d), a.t, b.t, d]
            out.append(run)
    return [(r[0], r[1], r[2]) for r in out]


def _held(el, f: int) -> bool:
    return all(abs(eval_track(t, g) - eval_track(t, f)) < 1e-9 for t in el.tracks.values() for g in (f - 2, f + 2))


def motion_questions(scene: Scene) -> list[dict]:
    """Motion detail an editor needs (pre-registered 2026-10-13, docs/qa/kfscript/ab-test-prereg.md): entrance time,
    which of two appears first, the duration of the biggest move, and position and opacity at a frame where the
    element holds still. Gold from the IR, the same for both views."""
    fps, ranked, qs = scene.fps, rank_elements(scene)[:12], []
    late = next((e for e in ranked if e.visible[0] > 0), None)
    if late:
        qs.append({"cat": "motion", "kind": "number", "q": f"At what time (seconds) does element {late.id} first appear?",
                   "gold": late.visible[0] / fps, "tol": 2 / fps})
    pair = next(((a, b) for i, a in enumerate(ranked) for b in ranked[i + 1:] if abs(a.visible[0] - b.visible[0]) >= 3), None)
    if pair:
        a, b = pair
        qs.append({"cat": "motion", "kind": "id", "q": f"Which element appears first: {a.id} or {b.id}?",
                   "gold": [a.id if a.visible[0] < b.visible[0] else b.id]})
    best = max(((m, e, p) for e in ranked for p in ("x", "y") for m in _moves(e, p) if m[0] > 10), key=lambda t: t[0][0], default=None)
    if best:
        (_, t0, t1), e, p = best
        qs.append({"cat": "motion", "kind": "number", "q": f"How long (seconds) does element {e.id}'s biggest "
                   f"{'horizontal' if p == 'x' else 'vertical'} move take?", "gold": (t1 - t0) / fps, "tol": max(2 / fps, 0.1 * (t1 - t0) / fps)})
    for e in ranked:
        held = [f for f in range(e.visible[0] + 2, e.visible[1] - 1) if _held(e, f)]
        if not held:
            continue
        longest = max(([f for _, f in g] for _, g in groupby(enumerate(held), key=lambda t: t[1] - t[0])), key=len)
        f = longest[len(longest) // 2]
        x0, y0, x1, y1 = element_bbox(e, f, scene.size)
        qs.append({"cat": "motion", "kind": "point", "q": f"Where is the centre of element {e.id} at t = {f / fps:.2f} s? "
                   "(answer [x, y] in pixels)", "gold": [(x0 + x1) / 2, (y0 + y1) / 2], "tol": 8.0})
        qs.append({"cat": "motion", "kind": "number", "q": f"What is element {e.id}'s opacity at t = {f / fps:.2f} s? (0 to 1)",
                   "gold": eval_props(e, f)["opacity"], "tol": 0.05})
        break
    return qs


def read_questions(gold: dict) -> list[dict]:
    qs = [{"cat": "read", "kind": "idset" if r.get("set") else "id", "q": r["q"], "gold": r["ids"]} for r in gold.get("read", [])]
    qs += [{"cat": "edit", "kind": "edit", "q": "Edit request: " + r["q"], "gold": r["ids"], "property": r["property"]}
           for r in gold.get("edit", [])]
    return qs


def synth_gold(rt: Path) -> dict:
    """read_gold.json for round-trip cases (eval-roundtrip output): one question per truth element whose role is unique in
    its case, answered by the IR ids the diff found inside it over its life (each id owned by the smallest truth element
    containing it); one edit per such element (a text's text, a shape's colour) and one timing edit (pre-registered
    2026-10-13)."""
    gold = {"_note": "generated by synth_gold from eval-roundtrip truth and diff fragments"}
    for case in json.loads((rt / "metrics.json").read_text())["cases"]:
        if case["kind"] == "gold":
            continue
        truth = json.loads((rt / "cases" / case["name"] / "truth.json").read_text())
        area = {e["id"]: (b[2] - b[0]) * (b[3] - b[1]) for e in truth["elements"] if e.get("boxes")
                for b in [e["boxes"][str((e["visible"][0] + e["visible"][1]) // 2)]]}
        owner = {}   # an IR id inside several truth boxes (title inside header) belongs to the smallest
        for r in case["diff"]["rows"]:
            for i in r["piece_ids"]:
                if i not in owner or area[r["id"]] < area[owner[i]]:
                    owner[i] = r["id"]
        frags = {r["id"]: [i for i in r["piece_ids"] if owner[i] == r["id"]] for r in case["diff"]["rows"]}
        roles = [e["role"] for e in truth["elements"]]
        unique = [e for e in truth["elements"] if roles.count(e["role"]) == 1 and e["role"] != "background" and frags.get(e["id"])]
        read = [{"q": f"Which element id(s) make up the {e['role'].replace('_', ' ')} ({e['kind']})? (list)", "ids": frags[e["id"]], "set": True}
                for e in unique]
        name = lambda e: e["role"].replace("_", " ")
        edit = [{"q": f"change the {name(e)} text to 'Hello'", "ids": frags[e["id"]], "property": "text"} if e["kind"] == "text"
                else {"q": f"make the {name(e)} red", "ids": frags[e["id"]], "property": "color"} for e in unique]
        edit += [{"q": f"make the {name(unique[0])} appear 0.5 s later", "ids": frags[unique[0]["id"]], "property": "timing"}] if unique else []
        gold[case["name"]] = {"read": read, "edit": edit}
    return gold


def _packet(view_text: str, questions: list[dict]) -> str:
    lines = ["# Task", "", "You are the Keepframe session agent. Below are your system prompt and the scene description you would "
             "receive as the second system message. Answer every question using only that description. Reply with ONLY one JSON "
             "object mapping question ids to answers (strings, numbers, lists or objects as asked).", "",
             "For edit requests answer the edit tool target you would send: {\"element\": \"<id or null>\", \"property\": "
             "\"text|color|texture|model|background|font|timing\"}.", "", "## System prompt", "", "```text", SYSTEM, "```", "",
             "## Scene description", "", "```text", view_text, "```", "", "## Questions", ""]
    lines += [f"Q{i + 1}: {q['q']}" for i, q in enumerate(questions)]
    return "\n".join(lines) + "\n"


def packets(root: Path, out: Path) -> dict:
    gold = json.loads((out / "read_gold.json").read_text())
    (out / "packets").mkdir(parents=True, exist_ok=True)
    questions, index = {}, {}
    for clip in [c for c in gold if not c.startswith("_")]:
        scene, _ = current_scene(root / clip, "s1")
        qs = auto_questions(scene) + motion_questions(scene) + read_questions(gold[clip])
        questions[clip] = qs
        texts = {"brief": scene_brief(scene), "kfscript": view(scene, scene_dir(root / clip, "s1"))}
        for v in VIEWS:
            name = hashlib.sha1(f"keepframe-ab:{clip}:{v}".encode()).hexdigest()[:12]
            (out / "packets" / f"{name}.md").write_text(_packet(texts[v], qs))
            index[name] = {"clip": clip, "view": v, "chars": len(texts[v])}
    (out / "questions.json").write_text(json.dumps(questions, indent=1, ensure_ascii=False))
    (out / "index.json").write_text(json.dumps(index, indent=1))
    return index


def _norm(s) -> str:
    return re.sub(r"\s+", "", str(s)).casefold()


def score_one(q: dict, a) -> float:
    k = q["kind"]
    try:
        if k == "yesno":
            return float(_norm(a) == q["gold"])
        if k == "number":
            return float(abs(float(a) - q["gold"]) <= q["tol"])
        if k == "exact":
            return float(_norm(a) == _norm(q["gold"]))
        if k == "text":
            return float(_norm(a) == _norm(q["gold"]))
        if k == "colour":
            return float(float(delta_e(srgb8_to_lab(hex_to_rgb8(str(a))), srgb8_to_lab(hex_to_rgb8(q["gold"])))) < 10)
        if k == "point":
            xy = [float(v) for v in (re.findall(r"-?\d+(?:\.\d+)?", a) if isinstance(a, str) else a)][:2]
            return float(len(xy) == 2 and math.dist(xy, q["gold"]) <= q["tol"])
        if k == "id":
            return float(str(a).strip() in q["gold"])
        if k == "idset":
            got, want = set(a if isinstance(a, list) else [a]) - {None, ""}, set(q["gold"])
            return 1.0 if not got and not want else len(got & want) / len(got | want)
        if k == "edit":   # one target, a list of targets, or one target naming several elements: all must match
            targets = a if isinstance(a, list) else [a]
            ids = [i for t in targets for i in (t.get("element") if isinstance(t.get("element"), list) else [t.get("element")])]
            return float(bool(targets) and all(t.get("property") == q["property"] for t in targets) and all(i in q["gold"] for i in ids))
    except (TypeError, ValueError, KeyError, AttributeError):
        return 0.0
    return 0.0


EDIT_MARGIN, BOOT, BOOT_SEED = 10.0, 10_000, 0   # pre-registered 2026-10-13 (docs/qa/kfscript/ab-test-prereg.md)


def paired_ci(rows: list[dict], cats: set[str], n: int = BOOT, seed: int = BOOT_SEED) -> dict:
    """kf-script − brief accuracy (points) over the questions in `cats`, paired per question, with a 95 % bootstrap
    interval that resamples clips (questions within a clip are correlated; Miller 2024, arXiv 2411.00640)."""
    pairs: dict[tuple, dict] = {}
    for r in rows:
        if r["cat"] in cats:
            pairs.setdefault((r["clip"], r["q"]), {})[r["view"]] = r["score"]
    by: dict[str, list[float]] = {}
    for (clip, _), v in pairs.items():
        if len(v) == 2:
            by.setdefault(clip, []).append(v["kfscript"] - v["brief"])
    if not by:
        return {"mean": 0.0, "lo": 0.0, "hi": 0.0, "clips": 0, "n": 0}
    clips = sorted(by)
    sums, counts = np.array([sum(by[c]) for c in clips]), np.array([len(by[c]) for c in clips])
    idx = np.random.default_rng(seed).integers(0, len(clips), (n, len(clips)))
    means = sums[idx].sum(1) / counts[idx].sum(1)
    return {"mean": round(100 * sums.sum() / counts.sum(), 2), "lo": round(100 * float(np.percentile(means, 2.5)), 2),
            "hi": round(100 * float(np.percentile(means, 97.5)), 2), "clips": len(clips), "n": int(counts.sum())}


def score(d: Path) -> dict:
    questions, index = json.loads((d / "questions.json").read_text()), json.loads((d / "index.json").read_text())
    rows, totals = [], {}
    for name, meta in index.items():
        f = d / "answers" / f"{name}.json"
        answers = json.loads(f.read_text()) if f.exists() else {}
        for i, q in enumerate(questions[meta["clip"]]):
            s = score_one(q, answers.get(f"Q{i + 1}"))
            rows.append({"clip": meta["clip"], "view": meta["view"], "q": f"Q{i + 1}", "cat": q["cat"], "score": s,
                         "answer": answers.get(f"Q{i + 1}"), "gold": q["gold"]})
            totals.setdefault((meta["view"], q["cat"]), []).append(s)
    acc = {f"{v}/{c}": round(100 * sum(s) / len(s), 1) for (v, c), s in sorted(totals.items())}
    comp = {v: [s for (vv, c), ss in totals.items() if vv == v and c != "edit" for s in ss] for v in VIEWS}
    acc.update({f"{v}/comprehension": round(100 * sum(s) / len(s), 1) for v, s in comp.items() if s})
    ci = {"comprehension": paired_ci(rows, {"auto", "motion", "read"}), "comprehension_old_set": paired_ci(rows, {"auto", "read"}),
          "edit": paired_ci(rows, {"edit"})}
    h1 = ci["comprehension"]["lo"] > 0                              # superiority, primary
    h2 = ci["edit"]["n"] == 0 or ci["edit"]["lo"] > -EDIT_MARGIN    # non-inferiority of edit targets
    p1 = (acc.get("kfscript/edit", 0) >= acc.get("brief/edit", 0)   # the P1 rule, reported for continuity only
          and acc.get("kfscript/comprehension", 0) >= acc.get("brief/comprehension", 0) + 10)
    res = {"accuracy": acc, "ci": ci, "h1": h1, "h2": h2, "switch_default_to_kfscript": h1 and h2, "p1_rule": p1, "rows": rows}
    (d / "results.json").write_text(json.dumps(res, indent=1, ensure_ascii=False))
    return res


def run(d: Path) -> None:
    from keepframe.session.llm import NullClient, make_llm
    llm = make_llm()
    if isinstance(llm, NullClient):
        raise SystemExit("no LLM configured; answer the packets with subagents instead")
    (d / "answers").mkdir(exist_ok=True)
    for p in sorted((d / "packets").glob("*.md")):
        reply = llm.complete([{"role": "user", "content": p.read_text()}], tools=[]).content or ""
        m = re.search(r"\{.*\}", reply, re.S)
        (d / "answers" / f"{p.stem}.json").write_text(m.group(0) if m else "{}")


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("packets"); p.add_argument("--root", required=True); p.add_argument("--out", required=True)
    g = sub.add_parser("synth-gold"); g.add_argument("--rt", required=True); g.add_argument("--out", required=True)
    s = sub.add_parser("score"); s.add_argument("--dir", required=True)
    r = sub.add_parser("run"); r.add_argument("--dir", required=True)
    a = ap.parse_args(argv)
    if a.cmd == "synth-gold":
        Path(a.out).mkdir(parents=True, exist_ok=True)
        (Path(a.out) / "read_gold.json").write_text(json.dumps(synth_gold(Path(a.rt)), indent=1))
    elif a.cmd == "packets":
        print(json.dumps(packets(Path(a.root), Path(a.out)), indent=1))
    elif a.cmd == "score":
        res = score(Path(a.dir))
        print(json.dumps({k: res[k] for k in ("accuracy", "ci", "h1", "h2", "switch_default_to_kfscript", "p1_rule")}, indent=1))
    else:
        run(Path(a.dir))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
