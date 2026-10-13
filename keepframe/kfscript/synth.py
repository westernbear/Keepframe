"""Round-trip references (P2): hand-authored GSAP pages with known truth, written from the agent's reads of real
motion graphics (docs/research/2026-10-13-ai-legible-format/gold-reads.md), not from analyser features.

`make_template(kind, seed)` → Template(doc, truth, size, fps, frames). Truth level (a) lists IR-expressible facts per
element; level (b) `structure` lists what the IR cannot say yet (`ir: "P3"`) or can (`ir: "now"`). Boxes are added by
`render_template`, probed in Chromium through `window.__bbox`."""
from __future__ import annotations
import html
import json
import random
from dataclasses import dataclass
from pathlib import Path

import numpy as np

W, H, FPS = 960, 540, 30
VENDOR = Path(__file__).resolve().parents[1] / "compose" / "vendor"
FONT_DIR = Path(__file__).resolve().parents[1] / "fonts" / "files"
FONTS = ["Inter", "Manrope", "DMSans", "PlusJakartaSans", "Outfit", "Urbanist", "Sora", "Montserrat", "Roboto"]
PALETTES = [  # bg, fg, accent, surface
    ("#0b1020", "#ffffff", "#3b6cff", "#1a2238"), ("#ffffff", "#14161c", "#7b3fe4", "#eef0f5"),
    ("#050505", "#d7ff3a", "#ffffff", "#1c1c1c"), ("#f4f1ea", "#1d1b17", "#ff5a1f", "#e6e1d6"),
    ("#101418", "#e8f0ff", "#00b38f", "#1e252d"), ("#1b0f2e", "#ffffff", "#ff4fa3", "#2c1d47"),
]
WORDS = ["Ship", "faster", "Build", "together", "Plan", "smarter", "Grow", "your", "team", "Launch", "today", "Work",
         "flows", "Every", "channel", "Think", "bigger", "Move", "forward", "Clear", "steps", "Less", "busywork"]
BRANDS = ["Nexa", "Orbit", "Flowly", "Pulse", "Lumen", "Vanta", "Kiro", "Mosaic"]
KINDS = ("kinetic_title", "logo_sting", "chat_ui", "scroller", "block_transition", "prompt_typing", "counter", "stepped_zoom")


@dataclass
class Template:
    doc: str
    truth: dict
    size: tuple[int, int] = (W, H)
    fps: float = FPS
    frames: int = 120


def _f(t: float) -> int:
    return int(round(t * FPS))


def _page(bg: str, font: str, css: str, body: str, js: str) -> str:
    return f"""<!doctype html>
<html><head><meta charset="utf-8"><style>
@font-face {{ font-family: F; src: url({(FONT_DIR / f"{font}.woff2").as_uri()}); font-weight: 100 900; }}
html, body {{ margin: 0; width: {W}px; height: {H}px; overflow: hidden; background: {bg}; }}
#stage {{ position: relative; width: {W}px; height: {H}px; overflow: hidden; background: {bg}; font-family: F, sans-serif; }}
.a {{ position: absolute; white-space: nowrap; }}
.c {{ position: absolute; transform: translate(-50%, -50%); white-space: nowrap; }}
.w {{ display: inline-block; overflow: hidden; vertical-align: top; }} .w > span {{ display: inline-block; }}
{css}
</style></head><body><div id="stage">
{body}
</div>
<script src="{(VENDOR / "gsap.min.js").as_uri()}"></script>
<script>
const FPS = {FPS}, tl = gsap.timeline({{paused: true}});
{js}
window.__seek = f => {{ tl.seek(f / FPS, false); return f; }};
window.__bbox = id => {{ const r = document.getElementById(id).getBoundingClientRect(), s = document.getElementById("stage").getBoundingClientRect();
  return [r.left - s.left, r.top - s.top, r.right - s.left, r.bottom - s.top]; }};
document.fonts.ready.then(() => {{ window.__seek(0); window.__ready = true; }});
</script></body></html>
"""


def _words(text: str) -> str:
    return " ".join(f'<span class="w"><span>{html.escape(w)}</span></span>' for w in text.split())


def _el(id_, role, kind, visible, text=None, colour=None, font=None) -> dict:
    return {"id": id_, "role": role, "kind": kind, "text": text, "colour": colour, "font": font, "visible": list(visible)}


def _kinetic_title(r: random.Random, pal, font) -> Template:
    bg, fg, acc, _ = pal
    title = " ".join(r.sample(WORDS, r.choice([2, 3])))
    sub = " ".join(r.sample(WORDS, 3)).lower()
    size, each, t0, te = r.choice([84, 92, 100]), r.choice([0.1, 0.14, 0.18]), r.choice([0.2, 0.3]), 2.2
    size = min(size, int(860 / (0.68 * len(title))))   # heavy display faces run ≈ 0.65 em per character
    n = len(title.split())
    css = f"#title {{ left: 50%; top: 210px; transform: translateX(-50%); font-size: {size}px; font-weight: 800; color: {fg}; }}\n" \
          f"#sub {{ left: 50%; top: 340px; transform: translateX(-50%); font-size: 30px; font-weight: 500; color: {acc}; }}"
    body = f'<div id="title" class="a">{_words(title)}</div>\n<div id="sub" class="a">{html.escape(sub)}</div>'
    js = "\n".join([
        f'tl.from("#title .w > span", {{yPercent: 110, duration: 0.5, ease: "power3.out", stagger: {each}}}, {t0});',
        f'tl.from("#sub", {{autoAlpha: 0, y: 14, duration: 0.4, ease: "power2.out"}}, {t0 + 0.6});',
        f'tl.to("#title .w > span", {{yPercent: -110, duration: 0.4, ease: "power2.in", stagger: {{each: {each}, from: "end"}}}}, {te});',
        f'tl.to("#sub", {{autoAlpha: 0, duration: 0.3}}, {te});'])
    end = _f(te + 0.4 + each * (n - 1))
    truth = {"elements": [_el("title", "headline", "text", (_f(t0), end), title, fg, font),
                          _el("sub", "subheadline", "text", (_f(t0 + 0.6), _f(te + 0.3)), sub, acc, font)],
             "structure": [{"type": "split", "targets": ["title"], "params": {"unit": "word", "n": n}, "ir": "P3"},
                           {"type": "stagger", "targets": ["title"], "params": {"each": each, "from": "start"}, "ir": "P3"},
                           {"type": "mask", "targets": ["title"], "params": {"edge": "bottom"}, "ir": "P3"},
                           {"type": "stagger", "targets": ["title"], "params": {"each": each, "from": "end", "exit": True}, "ir": "P3"}]}
    return Template(_page(bg, font, css, body, js), truth, frames=96)


def _logo_sting(r: random.Random, pal, font) -> Template:
    bg, fg, acc, _ = pal
    brand = r.choice(BRANDS) + " " + r.choice(["Labs", "Studio", "Cloud", "AI"])
    s, stroke, w2 = r.choice([120, 140, 160]), r.choice([10, 14]), r.choice([560, 620, 680])
    css = "\n".join([f"#bg {{ inset: 0; background: {bg}; }}",
                     f"#frame {{ left: 480px; top: 270px; width: {s}px; height: {s}px; box-sizing: border-box; border: {stroke}px solid {acc}; visibility: hidden; }}",
                     f"#mark {{ left: 480px; top: 270px; width: {s // 2}px; height: {s // 2}px; border-radius: 50%; background: {acc}; "
                     f"color: {bg}; font-size: {s // 3}px; font-weight: 800; line-height: {s // 2}px; text-align: center; }}",
                     f"#name {{ left: {480 - w2 // 2 + s}px; top: {270 - 30}px; font-size: 52px; font-weight: 700; color: {fg}; line-height: 60px; }}",
                     f"#url {{ left: 50%; top: {270 + s // 2 + 40}px; transform: translateX(-50%); font-size: 24px; color: {fg}; }}"])
    body = "\n".join(['<div id="bg" class="a"></div>', '<div id="frame" class="c"></div>',
                      f'<div id="mark" class="c">{html.escape(brand[0])}</div>', f'<div id="name" class="a">{_words(brand)}</div>',
                      f'<div id="url" class="a">www.{brand.split()[0].lower()}.com</div>'])
    js = "\n".join([
        'tl.fromTo("#bg", {clipPath: "inset(0% 50%)"}, {clipPath: "inset(0% 0%)", duration: 0.8, ease: "expo.inOut"}, 0.2);',
        'tl.set("#frame", {visibility: "visible"}, 0.9);',
        'tl.from("#frame", {width: 0, height: 0, duration: 0.6, ease: "back.out(1.6)"}, 0.9);',
        'tl.from("#mark", {scale: 0, duration: 0.4, ease: "back.out(2)"}, 1.3);',
        f'tl.to("#frame", {{width: {w2}, duration: 0.8, ease: "expo.inOut"}}, 2.0);',
        f'tl.to("#mark", {{x: {-(w2 // 2 - s // 2)}, duration: 0.8, ease: "expo.inOut"}}, 2.0);',
        'tl.from("#name .w > span", {yPercent: 110, duration: 0.5, ease: "power3.out", stagger: 0.1}, 2.6);',
        'tl.from("#url", {autoAlpha: 0, y: -20, duration: 1.0, ease: "expo.out"}, 3.0);'])
    truth = {"elements": [_el("bg", "background", "shape", (_f(0.2), 149), colour=bg), _el("frame", "frame", "shape", (_f(0.9), 149), colour=acc),
                          _el("mark", "logo", "shape", (_f(1.3), 149), brand[0], bg, font), _el("name", "brand_name", "text", (_f(2.6), 149), brand, fg, font),
                          _el("url", "url", "text", (_f(3.0), 149), f"www.{brand.split()[0].lower()}.com", fg, font)],
             "structure": [{"type": "mask", "targets": ["bg"], "params": {"from": "centre"}, "ir": "P3"},
                           {"type": "shape-size", "targets": ["frame"], "params": {"w": [0, s, w2], "stroke": stroke}, "ir": "P3"},
                           {"type": "split", "targets": ["name"], "params": {"unit": "word"}, "ir": "P3"},
                           {"type": "stagger", "targets": ["name"], "params": {"each": 0.1, "from": "start"}, "ir": "P3"},
                           {"type": "mask", "targets": ["name"], "params": {"edge": "bottom"}, "ir": "P3"}]}
    return Template(_page("#000000", font, css, body, js), truth, frames=150)


def _chat_ui(r: random.Random, pal, font) -> Template:
    bg, fg, acc, surf = pal
    n, k, title = 5, r.choice([0.7, 0.8, 0.9]), r.choice(["Support", "Assistant", "Team chat"])
    lines = [" ".join(r.sample(WORDS, r.choice([2, 3, 4]))) for _ in range(n)]
    css = [f"#head {{ left: 0; top: 0; width: {W}px; height: 64px; background: {surf}; }}",
           f"#title {{ left: 32px; top: 16px; font-size: 28px; font-weight: 700; color: {fg}; }}",
           "#list { left: 0; top: 64px; width: 960px; height: 476px; }",
           ".b { position: absolute; padding: 12px 20px; border-radius: 22px; font-size: 28px; white-space: nowrap; }"]
    body, js, els = ['<div id="head" class="a"></div>', f'<div id="title" class="a">{html.escape(title)}</div>', '<div id="list" class="a">'], [], []
    for i, line in enumerate(lines):
        left = i % 2 == 0
        side = "left: 40px;" if left else "right: 40px;"
        colour, back = (fg, surf) if left else ("#ffffff", acc)
        body.append(f'<div id="b{i}" class="b" style="{side} top: {40 + i * 76}px; background: {back}; color: {colour}">'
                    f'<span id="t{i}">{html.escape(line)}</span></div>')
        t = 0.4 + i * k
        js.append(f'tl.from("#b{i}", {{autoAlpha: 0, scale: 0.8, y: 20, transformOrigin: "{"left" if left else "right"} bottom", '
                  f'duration: 0.35, ease: "back.out(1.7)"}}, {t:.2f});')
        if i:
            js.append(f'tl.to("#list", {{y: {336 - 76 * i}, duration: 0.4, ease: "power2.inOut"}}, {t:.2f});')
        els += [_el(f"b{i}", "bubble", "shape", (_f(t), 149), colour=back), _el(f"t{i}", "message", "text", (_f(t), 149), line, colour, font)]
    body.append("</div>")
    js.insert(0, 'gsap.set("#list", {y: 336});')
    truth = {"elements": [_el("head", "header", "shape", (0, 149), colour=surf), _el("title", "title", "text", (0, 149), title, fg, font)] + els,
             "structure": [{"type": "repeat", "targets": [f"b{i}" for i in range(n)], "params": {"n": n, "every": k}, "ir": "P3"},
                           {"type": "container", "targets": [f"b{i}" for i in range(n)], "params": {"scroll": 76}, "ir": "P3"},
                           {"type": "stagger", "targets": [f"b{i}" for i in range(n)], "params": {"each": k}, "ir": "P3"}]}
    return Template(_page(bg, font, "\n".join(css), "\n".join(body), "\n".join(js)), truth, frames=150)


def _scroller(r: random.Random, pal, font) -> Template:
    bg, fg, _, _ = pal
    a, b = " ".join(r.sample(WORDS, 2)).upper(), " ".join(r.sample(WORDS, 2)).upper()
    lh, n, flash = 96, 8, r.choice([36, 40, 44])
    css = f"#col {{ left: 60px; top: 40px; font-size: 76px; font-weight: 900; line-height: {lh}px; color: {fg}; }} #col div {{ white-space: nowrap; }}"
    texts = [a if i % 2 == 0 else b for i in range(n)]
    body = '<div id="col" class="a">' + "".join(f'<div id="l{i}">{html.escape(t)}</div>' for i, t in enumerate(texts)) + "</div>"
    js = "\n".join([f'tl.fromTo("#col", {{y: 0}}, {{y: {-lh * 3}, duration: 3, ease: "none"}}, 0);',
                    f'tl.set("#stage", {{backgroundColor: "{fg}"}}, {flash / FPS:.4f}); tl.set("#col", {{color: "{bg}"}}, {flash / FPS:.4f});',
                    f'tl.set("#stage", {{backgroundColor: "{bg}"}}, {(flash + 8) / FPS:.4f}); tl.set("#col", {{color: "{fg}"}}, {(flash + 8) / FPS:.4f});'])
    truth = {"elements": [_el(f"l{i}", "line", "text", (0, 89), t, fg, font) for i, t in enumerate(texts)],
             "structure": [{"type": "container", "targets": [f"l{i}" for i in range(n)], "params": {"scroll": -3 * lh}, "ir": "P3"},
                           {"type": "transition", "targets": [], "params": {"type": "flash", "frames": [flash, flash + 7]}, "ir": "P3"},
                           {"type": "repeat", "targets": [f"l{i}" for i in range(n)], "params": {"n": n}, "ir": "P3"}]}
    return Template(_page(bg, font, css, body, js), truth, frames=90)


def _block_transition(r: random.Random, pal, font) -> Template:
    bg, fg, acc, surf = pal
    ta, tb, k = " ".join(r.sample(WORDS, 2)), " ".join(r.sample(WORDS, 2)), r.choice([4, 5, 6])
    bw = W / k
    css = "\n".join([f"#ta, #tb {{ left: 50%; top: 230px; transform: translateX(-50%); font-size: 72px; font-weight: 800; color: {fg}; }}",
                     "#tb { visibility: hidden; }", f".blk {{ position: absolute; top: 0; width: {bw + 1:.1f}px; height: {H}px; background: {acc}; }}"])
    body = [f'<div id="ta" class="a">{html.escape(ta)}</div>', f'<div id="tb" class="a">{html.escape(tb)}</div>']
    body += [f'<div id="k{i}" class="blk" style="left: {i * bw:.1f}px"></div>' for i in range(k)]
    js = "\n".join([f'gsap.set(".blk", {{y: {H}}});',
                    f'tl.to(".blk", {{y: 0, duration: 0.4, ease: "power3.inOut", stagger: 0.06}}, 1.0);',
                    f'tl.set("#ta", {{visibility: "hidden"}}, 1.5); tl.set("#tb", {{visibility: "visible"}}, 1.5); '
                    f'tl.set("#stage", {{backgroundColor: "{surf}"}}, 1.5);',
                    f'tl.to(".blk", {{y: {-H}, duration: 0.4, ease: "power3.inOut", stagger: 0.06}}, 1.6);'])
    truth = {"elements": [_el("ta", "headline", "text", (0, 44), ta, fg, font), _el("tb", "headline", "text", (45, 89), tb, fg, font)]
             + [_el(f"k{i}", "block", "shape", (30, 68), colour=acc) for i in range(k)],
             "shots": [[0, 44], [45, 89]],
             "structure": [{"type": "transition", "targets": [f"k{i}" for i in range(k)], "params": {"type": "blocks", "frames": [30, 68]}, "ir": "P3"},
                           {"type": "stagger", "targets": [f"k{i}" for i in range(k)], "params": {"each": 0.06}, "ir": "P3"},
                           {"type": "repeat", "targets": [f"k{i}" for i in range(k)], "params": {"n": k}, "ir": "P3"}]}
    return Template(_page(bg, font, css, "\n".join(body), js), truth, frames=90)


def _prompt_typing(r: random.Random, pal, font) -> Template:
    bg, fg, acc, surf = pal
    head = r.choice(["Ask. And move forward.", "Just ask.", "What should we build?"])
    typed = " ".join(r.sample(WORDS, 4)).capitalize() + "."
    t0, cps = 0.8, r.choice([16, 20, 24])
    times, t = [], t0
    for ch in typed:
        times.append(t)
        t += 1 / cps + (r.choice([0.0, 0.0, 0.12]) if ch == " " else 0.0)
    css = "\n".join([f"#head {{ left: 50%; top: 120px; transform: translateX(-50%); font-size: 56px; font-weight: 800; color: {fg}; }}",
                     f"#bar {{ left: 200px; top: 240px; width: 560px; height: 72px; box-sizing: border-box; border-radius: 36px; "
                     f"border: 2px solid {acc}; background: {surf}; }}",
                     f"#typed {{ left: 236px; top: 258px; font-size: 30px; font-weight: 500; color: {fg}; }} #typed .ch {{ display: none; }}",
                     f"#caret {{ display: inline-block; width: 3px; height: 30px; background: {fg}; vertical-align: -4px; margin-left: 2px; }}",
                     f"#btn {{ left: 640px; top: 252px; width: 104px; height: 48px; border-radius: 24px; background: {acc}; color: #fff; "
                     "font-size: 24px; font-weight: 700; line-height: 48px; text-align: center; }",
                     "#cursor { left: 0; top: 0; width: 28px; height: 28px; }",
                     f"#chip {{ left: 50%; top: 360px; transform: translateX(-50%); font-size: 22px; color: {fg}; padding: 8px 20px; "
                     f"border: 1px solid {acc}; border-radius: 20px; }}"])
    chars = "".join(f'<span class="ch">{html.escape(c)}</span>' for c in typed)
    chip = r.choice(["One prompt. A clear next step.", "Answers in seconds."])
    body = "\n".join([f'<div id="head" class="a">{html.escape(head)}</div>', '<div id="bar" class="a"></div>',
                      f'<div id="typed" class="a">{chars}<span id="caret"></span></div>', '<div id="btn" class="a">Ask</div>',
                      f'<svg id="cursor" class="a" viewBox="0 0 24 24"><path d="M3 2l7 19 2.5-8 8-2.5z" fill="#ffffff" stroke="#000" stroke-width="1.2"/></svg>',
                      f'<div id="chip" class="a">{html.escape(chip)}</div>'])
    sets = [f'tl.set("#typed .ch:nth-child({i + 1})", {{display: "inline"}}, {tm:.4f});' for i, tm in enumerate(times)]
    blink = [f'tl.set("#caret", {{opacity: {o}}}, {b / 2:.2f});' for b, o in zip(range(1, 10), [0, 1] * 5)]
    tc = t + 0.4
    js = "\n".join([f'tl.from("#head", {{autoAlpha: 0, y: 20, filter: "blur(8px)", duration: 0.5, ease: "power2.out"}}, 0.2);',
                    'tl.from(["#bar", "#btn", "#typed"], {autoAlpha: 0, duration: 0.4}, 0.4);', *sets, *blink,
                    f'gsap.set("#cursor", {{x: 900, y: 500, autoAlpha: 0}});',
                    f'tl.to("#cursor", {{autoAlpha: 1, duration: 0.1}}, {tc:.2f});',
                    f'tl.to("#cursor", {{x: 690, y: 272, duration: 0.7, ease: "power2.out"}}, {tc:.2f});',
                    f'tl.to(["#btn", "#cursor"], {{scale: 0.88, duration: 0.08, yoyo: true, repeat: 1}}, {tc + 0.9:.2f});',
                    f'tl.from("#chip", {{autoAlpha: 0, duration: 0.3}}, {tc + 1.2:.2f});'])
    truth = {"elements": [_el("head", "headline", "text", (_f(0.2), 149), head, fg, font), _el("bar", "input", "shape", (_f(0.4), 149), colour=surf),
                          _el("typed", "typed_prompt", "text", (_f(t0), 149), typed, fg, font), _el("btn", "button", "shape", (_f(0.4), 149), "Ask", acc, font),
                          _el("cursor", "cursor", "image", (_f(tc), 149), colour="#ffffff"), _el("chip", "chip", "text", (_f(tc + 1.2), 149), chip, fg, font)],
             "structure": [{"type": "typing", "targets": ["typed"], "params": {"frames": [_f(x) for x in times]}, "ir": "now"},
                           {"type": "blink", "targets": ["typed"], "params": {"every": 0.5}, "ir": "P3"},
                           {"type": "cursor", "targets": ["cursor"], "params": {"to": "btn", "frames": [_f(tc), _f(tc + 0.7)]}, "ir": "P3"},
                           {"type": "press", "targets": ["btn"], "params": {"frame": _f(tc + 0.9)}, "ir": "P3"}]}
    return Template(_page(bg, font, css, body, js), truth, frames=150)


def _counter(r: random.Random, pal, font) -> Template:
    bg, fg, acc, _ = pal
    n1, n2 = r.choice([80, 125, 200]), r.choice([60, 150, 320])
    l1, l2 = r.choice(["Tickets resolved", "Active users", "Hours saved"]), r.choice(["Integrations", "Countries", "Teams"])
    ink = "#111111" if acc in ("#ffffff", "#d7ff3a") or bg in ("#ffffff", "#f4f1ea") else "#ffffff"
    css = "\n".join([f".card {{ position: absolute; top: 150px; width: 360px; height: 240px; border-radius: 20px; background: {acc}; }}",
                     f".lab {{ position: absolute; left: 28px; top: 26px; font-size: 30px; color: {ink}; white-space: nowrap; }}",
                     f".num {{ position: absolute; left: 28px; top: 90px; font-size: 96px; font-weight: 600; color: {ink}; white-space: nowrap; }}"])
    body = "\n".join([f'<div id="c1" class="card" style="left: 100px"><div id="l1" class="lab">{l1}</div><div id="n1" class="num">0K</div></div>',
                      f'<div id="c2" class="card" style="left: 500px"><div id="l2" class="lab">{l2}</div><div id="n2" class="num">0</div></div>'])
    js = "\n".join(['tl.from(".card", {autoAlpha: 0, scale: 0.9, y: 30, duration: 0.4, ease: "back.out(1.6)", stagger: 0.1}, 0.2);',
                    'const v1 = {v: 0}, v2 = {v: 0}, n1 = document.getElementById("n1"), n2 = document.getElementById("n2");',
                    f'tl.to(v1, {{v: {n1}, duration: 1.4, ease: "sine.inOut", onUpdate: () => n1.textContent = Math.round(v1.v) + "K"}}, 0.5);',
                    f'tl.to(v2, {{v: {n2}, duration: 1.4, ease: "sine.inOut", onUpdate: () => n2.textContent = String(Math.round(v2.v))}}, 0.6);'])
    truth = {"elements": [_el("c1", "card", "shape", (_f(0.2), 119), colour=acc), _el("c2", "card", "shape", (_f(0.3), 119), colour=acc),
                          _el("l1", "label", "text", (_f(0.2), 119), l1, ink, font), _el("l2", "label", "text", (_f(0.3), 119), l2, ink, font),
                          _el("n1", "counter", "text", (_f(0.2), 119), f"{n1}K", ink, font), _el("n2", "counter", "text", (_f(0.3), 119), str(n2), ink, font)],
             "structure": [{"type": "counter", "targets": ["n1"], "params": {"from": 0, "to": n1, "fmt": "{}K", "frames": [_f(0.5), _f(1.9)]}, "ir": "P3"},
                           {"type": "counter", "targets": ["n2"], "params": {"from": 0, "to": n2, "fmt": "{}", "frames": [_f(0.6), _f(2.0)]}, "ir": "P3"},
                           {"type": "stagger", "targets": ["c1", "c2"], "params": {"each": 0.1}, "ir": "P3"}]}
    return Template(_page(bg, font, css, body, js), truth, frames=120)


def _stepped_zoom(r: random.Random, pal, font) -> Template:
    bg, fg, acc, _ = pal
    word, lab = r.choice(BRANDS), " ".join(r.sample(WORDS, 2)).upper()
    steps = [(0, r.choice([2.4, 2.8])), (10, r.choice([0.5, 0.6])), (20, 1.0)]
    css = "\n".join([f"#word {{ left: 480px; top: 250px; font-size: 140px; font-weight: 900; color: {acc}; }}",
                     f"#lab1 {{ left: 200px; top: 400px; font-size: 28px; font-weight: 700; color: {fg}; }}",
                     f"#lab2 {{ left: 560px; top: 400px; font-size: 28px; font-weight: 700; color: {fg}; }}"])
    body = "\n".join([f'<div id="word" class="c">{word}</div>', f'<div id="lab1" class="a">{lab}</div>', f'<div id="lab2" class="a">{lab}</div>'])
    js = "\n".join([f'gsap.set("#word", {{scale: {steps[0][1]}}});'] + [f'tl.set("#word", {{scale: {s}}}, {f / FPS:.4f});' for f, s in steps[1:]]
                   + ['tl.from(["#lab1", "#lab2"], {autoAlpha: 0, duration: 0.01}, 24 / FPS);'])
    truth = {"elements": [_el("word", "logo", "text", (0, 89), word, acc, font), _el("lab1", "label", "text", (24, 89), lab, fg, font),
                          _el("lab2", "label", "text", (24, 89), lab, fg, font)],
             "structure": [{"type": "steps", "targets": ["word"], "params": {"scale": steps}, "ir": "P3"}]}
    return Template(_page(bg, font, css, body, js), truth, frames=90)


_MAKERS = {"kinetic_title": _kinetic_title, "logo_sting": _logo_sting, "chat_ui": _chat_ui, "scroller": _scroller,
           "block_transition": _block_transition, "prompt_typing": _prompt_typing, "counter": _counter, "stepped_zoom": _stepped_zoom}


def make_template(kind: str, seed: int) -> Template:
    """A seeded reference page and its truth; the same (kind, seed) always gives the same page."""
    r = random.Random(f"kfscript-synth:{kind}:{seed}")
    t = _MAKERS[kind](r, r.choice(PALETTES), r.choice(FONTS))
    t.truth.update({"kind": kind, "seed": seed, "size": list(t.size), "fps": t.fps, "frames": t.frames})
    t.truth.setdefault("shots", [[0, t.frames - 1]])
    return t


def _clip(box, size) -> list[float]:
    x0, y0, x1, y1 = box
    return [round(min(max(x0, 0), size[0]), 1), round(min(max(y0, 0), size[1]), 1),
            round(min(max(x1, 0), size[0]), 1), round(min(max(y1, 0), size[1]), 1)]


def render_template(kind: str, seed: int, out: Path) -> tuple[Path, Path]:
    """page.html, source.mp4 (as the analysis will see it) and truth.json with Chromium-probed boxes per visible frame."""
    from ..analyze.video import write_video
    from ..ir.schema import Background, Canonical, Element, Scene
    from ..render.renderer import load_frame, render
    out = Path(out)
    out.mkdir(parents=True, exist_ok=True)
    t = make_template(kind, seed)
    page = out / "page.html"
    page.write_text(t.doc)
    ids = [e["id"] for e in t.truth["elements"]]
    stub = Scene(id=f"{kind}-{seed}", size=t.size, fps=t.fps, frames=t.frames, background=Background(kind="color", value="#000000"),
                 elements=[Element(id=i, kind="sprite", visible=(0, t.frames - 1), canonical=Canonical(width=1, height=1)) for i in ids])
    res = render(page, stub, out / "render", probe=True)
    frames = np.stack([(load_frame(res.frames_dir / f"f_{i:05d}.png") * 255).round().astype(np.uint8) for i in range(t.frames)])
    mp4 = write_video(frames, t.fps, out / "source.mp4")
    for e in t.truth["elements"]:
        a, b = e["visible"]
        boxes = {f: _clip(res.bboxes[e["id"]][f], t.size) for f in range(a, b + 1)}
        on = [f for f, x in boxes.items() if x[2] > x[0] and x[3] > x[1]]
        e["visible"] = [on[0], on[-1]] if on else [a, a - 1]
        e["boxes"] = {str(f): boxes[f] for f in on}
    truth = out / "truth.json"
    truth.write_text(json.dumps(t.truth, indent=1))
    return mp4, truth
