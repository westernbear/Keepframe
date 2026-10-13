"""Blind A/B of the agent's scene views: brief vs kf-script (P1 Task 7, docs/superpowers/plans/2026-10-13-kfscript-p1-view.md).

  packets --root eval/kf --out docs/qa/kfscript/ab     questions.json, packets/<hash>.md, index.json (hash → clip, view)
  score   --dir docs/qa/kfscript/ab                     answers/<hash>.json → results.json + summary.md
  run     --dir docs/qa/kfscript/ab                     answer every packet with make_llm() (needs credentials)
  synth-gold --rt eval/out/rt-base --out docs/qa/kfscript/ab-synth   read_gold.json from round-trip truth (fresh clips)

Questions per clip: auto-gold from scene.json (visibility, end of last move, glyph reveal, ease, largest text, z order)
and read-gold from read_gold.json (ids of things the agent's read names; edit targets). Packets never name the view.
Pre-registered rule: kf-script becomes the default view only if its edit accuracy >= the brief's and its
comprehension accuracy >= the brief's + 10 points."""
from __future__ import annotations

import argparse
import hashlib
import json
import re
from pathlib import Path

from keepframe.analyze.keyframes import ERR
from keepframe.ir.colour import delta_e, hex_to_rgb8, srgb8_to_lab
from keepframe.ir.importance import rank_elements
from keepframe.ir.schema import Scene
from keepframe.ir.store import current_scene, scene_dir
from keepframe.ir.tracks import PRESET_EASES, eval_z
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


def read_questions(gold: dict) -> list[dict]:
    qs = [{"cat": "read", "kind": "idset" if r.get("set") else "id", "q": r["q"], "gold": r["ids"]} for r in gold.get("read", [])]
    qs += [{"cat": "edit", "kind": "edit", "q": "Edit request: " + r["q"], "gold": r["ids"], "property": r["property"]}
           for r in gold.get("edit", [])]
    return qs


def synth_gold(rt: Path) -> dict:
    """read_gold.json for round-trip cases (eval-roundtrip output): one question per truth element whose role is unique in
    its case, answered by the IR ids the diff found inside it over its life; one colour edit on the first such text element."""
    gold = {"_note": "generated by synth_gold from eval-roundtrip truth and diff fragments"}
    for case in json.loads((rt / "metrics.json").read_text())["cases"]:
        if case["kind"] == "gold":
            continue
        truth = json.loads((rt / "cases" / case["name"] / "truth.json").read_text())
        frags = {r["id"]: r["piece_ids"] for r in case["diff"]["rows"]}
        roles = [e["role"] for e in truth["elements"]]
        unique = [e for e in truth["elements"] if roles.count(e["role"]) == 1 and e["role"] != "background" and frags.get(e["id"])]
        read = [{"q": f"Which element id(s) make up the {e['role'].replace('_', ' ')} ({e['kind']})? (list)", "ids": frags[e["id"]], "set": True}
                for e in unique]
        text = next((e for e in unique if e["kind"] == "text"), None)
        edit = [{"q": f"make the {text['role'].replace('_', ' ')} red", "ids": frags[text["id"]], "property": "color"}] if text else []
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
        qs = auto_questions(scene) + read_questions(gold[clip])
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
    rule = (acc.get("kfscript/edit", 0) >= acc.get("brief/edit", 0)
            and acc.get("kfscript/comprehension", 0) >= acc.get("brief/comprehension", 0) + 10)
    res = {"accuracy": acc, "switch_default_to_kfscript": rule, "rows": rows}
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
        print(json.dumps(score(Path(a.dir))["accuracy"], indent=1))
    else:
        run(Path(a.dir))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
