# Round 2 evaluation

Commands (clips in the main checkout's `eval/`, not committed):

```
keepframe gate-m2-real --clips eval/clips --out eval/out/<name> --max-frames 150 --render-check
python scripts/eval_prompts.py --project eval/out/<name>/<clip> --workspace eval/ws --gold docs/qa/round2/gold/<clip>.json
```

- `mean_l1`: the analysis reconstruction compared with the source frames (numpy composite).
- `render_l1`: the composed HTML rendered in Chromium, compared with the source on every 10th frame. This is the end-to-end number.

## Baseline (2026-10-04, commit ff9e6f3 = master analysis code)

| clip | elements | kinds | mean_l1 | render_l1 | low_conf | keep_on / constraints |
| --- | --- | --- | --- | --- | --- | --- |
| envato1 | 56 | sprite 47, text 9 | 0.0750 | 0.0868 | 43 | 4734 / 4744 |
| ig1 | 128 | sprite 84, text 44 | 0.0393 | 0.0425 | 86 | 55444 / 55484 |
| ig2 | 54 | sprite 39, text 15 | 0.0644 | 0.0738 | 47 | 5543 / 5549 |
| ig3 | 71 | sprite 61, text 10 | 0.0175 | 0.0219 | 38 | 6443 / 6443 |

`mean_l1` is identical to the round-1 final run, so analysis is deterministic.

Stage seconds (4 CPU cores, run alongside other work; indicative only, speed decisions use a separate quiet A/B run):

| clip | text (OCR) | regions | sprites | total |
| --- | --- | --- | --- | --- |
| envato1 | 477 | 460 | 406 | 1513 |
| ig1 | 810 | 207 | 157 | 1970 |
| ig2 | 572 | 336 | 326 | 1342 |
| ig3 | 459 | 44 | 24 | 633 |

### Prompt evaluation with gold (`gpt-6.1-sol`, preview only, scenes captioned)

The gold answers in `docs/qa/round2/gold/` (committed so they can be reviewed) are written from the frames, by content and position, so they stay valid when a scene is re-analysed.
- "ok" means the edit is a valid typed edit.
- "correct" means every target is the element a person would pick.
- Not graded: the logo prompt on clips with no logo, and the card prompt everywhere (there are several cards, or none). That leaves 25 gradable prompts.

| clip | ok / 8 | correct / gradable |
| --- | --- | --- |
| envato1 | 4 | 3 / 6 |
| ig1 | 5 | 2 / 6 |
| ig2 | 7 | 5 / 7 |
| ig3 | 3 | 2 / 6 |
| Total | 19 / 32 | 12 / 25 |

The background and whole-scene speed prompts are correct everywhere. Every miss comes from fragmentation:
- **"Second text":** the agent picks a fragment every time (envato1 `ul`, ig1 the title, ig2 a giant-word `e`, ig3 `U`).
- **Title prompts:** they fail on envato1, ig1 and ig3, where the headline is split into pieces, and the agent asks which piece to edit.
- **Logo prompt on ig2:** the Airbnb logo is not found, because it is not its own element.
