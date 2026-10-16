"""Round-trip references (P2): hand-authored GSAP pages with known truth, written from the agent's reads of real
motion graphics (docs/research/2026-10-13-ai-legible-format/gold-reads.md), not from analyser features.

`make_template(kind, seed)` → Template(doc, truth, size, fps, frames). Truth level (a) lists IR-expressible facts per
element; level (b) `structure` lists what the IR cannot say yet (`ir: "P3"`) or can (`ir: "now"`). Boxes are added by
`render_template`, probed in Chromium through `window.__bbox`."""
from __future__ import annotations
import hashlib
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
KINDS = ("kinetic_title", "logo_sting", "chat_ui", "scroller", "block_transition", "prompt_typing", "counter", "stepped_zoom",
         "hard_cut", "wipe", "word_build", "whip_pan", "word_swap", "big_scroll", "glow_title", "label_grid", "glow_letters",
         "glow_cut", "glow_merge")


@dataclass
class Template:
    doc: str
    truth: dict
    size: tuple[int, int] = (W, H)
    fps: float = FPS
    frames: int = 120


def _f(t: float) -> int:
    return int(round(t * FPS))


def _page(bg: str, font: str, css: str, body: str, js: str, font2: str | None = None) -> str:
    """`font2`: a second family G (loaded and waited for too); without it the page is as before."""
    face2 = f'\n@font-face {{ font-family: G; src: url({(FONT_DIR / f"{font2}.woff2").as_uri()}); font-weight: 100 900; }}' if font2 else ""
    wait2 = ', document.fonts.load("16px G")' if font2 else ""
    return f"""<!doctype html>
<html><head><meta charset="utf-8"><style>
@font-face {{ font-family: F; src: url({(FONT_DIR / f"{font}.woff2").as_uri()}); font-weight: 100 900; }}{face2}
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
// Build after the face has loaded: GSAP reads CSS translate(-50%) as pixels on first touch, so a fallback-font width
// would shift centred text. fonts.ready alone can resolve before the face starts loading.
Promise.all([document.fonts.load("16px F"){wait2}, document.fonts.ready]).then(() => {{
{js}
window.__seek(0); window.__ready = true; }});
window.__seek = f => {{ tl.seek(f / FPS, false); return f; }};
window.__bbox = id => {{ const r = document.getElementById(id).getBoundingClientRect(), s = document.getElementById("stage").getBoundingClientRect();
  return [r.left - s.left, r.top - s.top, r.right - s.left, r.bottom - s.top]; }};
</script></body></html>
"""


def _ink(surface: str) -> str:
    """A label colour readable on `surface` (#rrggbb): dark on light surfaces, white otherwise."""
    r, g, b = (int(surface[i:i + 2], 16) for i in (1, 3, 5))
    return "#111111" if 0.2126 * r + 0.7152 * g + 0.0722 * b > 160 else "#ffffff"


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
                           {"type": "pop", "targets": ["mark"], "params": {"scale": [0, 1]}, "ir": "now"},
                           {"type": "drift", "targets": ["url"], "params": {"axis": "y", "px": 20}, "ir": "now"},
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
             "shots": [[0, 44], [45, 89]], "transitions": [{"type": "blocks", "frames": [30, 68]}],
             "structure": [{"type": "transition", "targets": [f"k{i}" for i in range(k)], "params": {"type": "blocks", "frames": [30, 68]}, "ir": "P3"},
                           {"type": "stagger", "targets": [f"k{i}" for i in range(k)], "params": {"each": 0.06}, "ir": "P3"},
                           {"type": "repeat", "targets": [f"k{i}" for i in range(k)], "params": {"n": k}, "ir": "P3"}]}
    return Template(_page(bg, font, css, "\n".join(body), js), truth, frames=90)


def _prompt_typing(r: random.Random, pal, font) -> Template:
    bg, fg, acc, surf = pal
    head = r.choice(["Ask. And move forward.", "Just ask.", "What should we build?"])
    typed = ""
    for word in r.sample(WORDS, 6):   # the line must fit the bar left of the button (≈ 22 characters at 30 px)
        if len(typed) + len(word) + 2 > 22:
            break
        typed = f"{typed} {word}".strip()
    typed = typed.capitalize() + "."
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
                     f"#btn {{ left: 640px; top: 252px; width: 104px; height: 48px; border-radius: 24px; background: {acc}; color: {_ink(acc)}; "
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


GROUNDS = [("#0b1020", "#f5f6fb"), ("#1b0f2e", "#f4f1ea"), ("#050505", "#ffffff")]   # dark, light


def _hard_cut(r: random.Random, pal, font) -> Template:
    """Title cards cut on the beat. Shot 1: a light title over a dark gradient with a glow that rises and falls; a hard
    cut to shot 2: a dark title and its subtitle on a light ground, where later the title alone is swapped for another
    on the same ground while the subtitle and a footer stay (a swap inside a shot, as the reads mark it: not a cut)."""
    _, _, acc, _ = pal
    dark, light = r.choice(GROUNDS)
    a, b, c = (" ".join(r.sample(WORDS, 2)) for _ in range(3))
    sub = " ".join(r.sample(WORDS, 3)).lower()
    c1, sw = r.choice([38, 40, 42]), r.choice([78, 80, 82])
    css = "\n".join([f"#s1 {{ inset: 0; background: linear-gradient(180deg, {dark} 45%, {acc} 170%); }}",
                     f"#glow {{ left: 80px; top: 300px; width: 800px; height: 480px; border-radius: 50%; "
                     f"background: radial-gradient(closest-side, {acc}, transparent); }}",
                     f"#s2 {{ inset: 0; background: {light}; visibility: hidden; }}",
                     "#t1 { left: 480px; top: 250px; font-size: 84px; font-weight: 800; color: #ffffff; }",
                     "#t2, #t3 { left: 480px; top: 240px; font-size: 72px; font-weight: 700; color: #14161c; visibility: hidden; }",
                     "#sub2 { left: 480px; top: 330px; font-size: 28px; color: #14161c; visibility: hidden; }",
                     f"#foot {{ left: 60px; top: 440px; width: 840px; height: 64px; border-radius: 16px; background: {acc}; "
                     "color: #ffffff; font-size: 30px; font-weight: 700; line-height: 64px; text-indent: 28px; visibility: hidden; }"])
    brand = r.choice(BRANDS)
    body = "\n".join(['<div id="s1" class="a"></div>', '<div id="glow" class="a"></div>', '<div id="s2" class="a"></div>',
                      f'<div id="t1" class="c">{html.escape(a)}</div>', f'<div id="t2" class="c">{html.escape(b)}</div>',
                      f'<div id="sub2" class="c">{html.escape(sub)}</div>', f'<div id="t3" class="c">{html.escape(c)}</div>',
                      f'<div id="foot" class="a">{html.escape(brand)}</div>'])
    show = lambda ids, v, f: f'tl.set({json.dumps(ids)}, {{visibility: "{v}"}}, {f / FPS:.4f});'
    js = "\n".join(['tl.fromTo("#glow", {opacity: 0.25, scale: 0.85}, {opacity: 0.9, scale: 1.15, duration: 0.6, ease: "sine.inOut", '
                    'yoyo: true, repeat: 1}, 0);',
                    'tl.from("#t1", {autoAlpha: 0, y: 30, duration: 0.5, ease: "power3.out"}, 0.2);',
                    show(["#s1", "#glow", "#t1"], "hidden", c1), show(["#s2", "#t2", "#sub2", "#foot"], "visible", c1),
                    f'tl.from("#t2", {{x: 60, duration: 0.5, ease: "power3.out"}}, {c1 / FPS:.4f});',
                    f'tl.from("#sub2", {{autoAlpha: 0, duration: 0.4}}, {c1 / FPS + 0.4:.4f});',
                    show(["#t2"], "hidden", sw), show(["#t3"], "visible", sw),
                    f'tl.from("#t3", {{y: 40, duration: 0.4, ease: "power3.out"}}, {sw / FPS:.4f});'])
    truth = {"elements": [_el("t1", "headline", "text", (_f(0.2), c1 - 1), a, "#ffffff", font),
                          _el("t2", "headline_2", "text", (c1, sw - 1), b, "#14161c", font),
                          _el("sub2", "subheadline", "text", (c1 + _f(0.4), 119), sub, "#14161c", font),
                          _el("t3", "headline_3", "text", (sw, 119), c, "#14161c", font),
                          _el("foot", "footer", "shape", (c1, 119), brand, acc, font)],
             "shots": [[0, c1 - 1], [c1, 119]], "transitions": [{"type": "cut", "frames": [c1, c1]}],
             "structure": []}
    return Template(_page(dark, font, css, body, js), truth, frames=120)


def _wipe(r: random.Random, pal, font) -> Template:
    """A circle wipe from a dark scene with a light title to a light scene whose dark title fades in after it."""
    _, _, acc, _ = pal
    dark, light = r.choice(GROUNDS)
    a, b = " ".join(r.sample(WORDS, 2)), " ".join(r.sample(WORDS, 3))
    t0, dur, cx = r.choice([1.0, 1.1, 1.2]), r.choice([0.3, 0.4]), r.choice(["20% 80%", "80% 80%", "50% 90%"])
    w0, w1 = _f(t0), _f(t0 + dur)
    css = "\n".join([f"#s1 {{ inset: 0; background: linear-gradient(160deg, {dark} 50%, {acc} 190%); }}",
                     f"#s2 {{ inset: 0; background: {light}; }}",
                     "#t1 { left: 480px; top: 230px; font-size: 80px; font-weight: 800; color: #ffffff; }",
                     "#t2 { left: 480px; top: 270px; font-size: 64px; font-weight: 700; color: #14161c; }"])
    body = "\n".join(['<div id="s1" class="a"></div>', f'<div id="t1" class="c">{html.escape(a)}</div>',
                      f'<div id="s2" class="a"><div id="t2" class="c">{html.escape(b)}</div></div>'])
    js = "\n".join(['tl.from("#t1", {autoAlpha: 0, y: 24, duration: 0.5, ease: "power3.out"}, 0.2);',
                    f'tl.fromTo("#s2", {{clipPath: "circle(0% at {cx})"}}, {{clipPath: "circle(120% at {cx})", duration: {dur}, ease: "power2.in"}}, {t0});',
                    f'tl.set("#t1", {{visibility: "hidden"}}, {w1 / FPS:.4f});',
                    f'tl.from("#t2", {{autoAlpha: 0, y: 20, duration: 0.4, ease: "power2.out"}}, {(w1 + 3) / FPS:.4f});'])
    mid = (w0 + w1) // 2
    truth = {"elements": [_el("t1", "headline", "text", (_f(0.2), w1 - 1), a, "#ffffff", font),
                          _el("t2", "headline", "text", (w1 + 3, 89), b, "#14161c", font)],
             "shots": [[0, mid - 1], [mid, 89]], "transitions": [{"type": "wipe", "frames": [w0, w1]}],
             "structure": [{"type": "transition", "targets": [], "params": {"type": "circle-wipe", "frames": [w0, w1]}, "ir": "P3"}]}
    return Template(_page(dark, font, css, body, js), truth, frames=90)


def _word_build(r: random.Random, pal, font) -> Template:
    """Captions built word by word: a centred line that re-centres as each word lands, and a left-aligned line whose
    words land in place, which then shrinks about its centre and fades."""
    bg, fg, _, _ = pal
    w1, w2 = r.sample(WORDS, 4), r.sample(WORDS, 3)
    each = r.choice([0.2, 0.25, 0.3])
    spans = lambda ws: "".join(f'<span class="wd">{html.escape(w)}{" " if i < len(ws) - 1 else ""}</span>' for i, w in enumerate(ws))
    css = "\n".join([".wd { display: none; white-space: pre; }",
                     f"#l1 {{ left: 480px; top: 190px; font-size: 64px; font-weight: 700; color: {fg}; }}",
                     f"#l2 {{ left: 140px; top: 320px; font-size: 56px; font-weight: 600; color: {fg}; transform-origin: 50% 50%; }}"])
    body = "\n".join([f'<div id="l1" class="c">{spans(w1)}</div>', f'<div id="l2" class="a">{spans(w2)}</div>'])
    t1 = [0.3 + i * each for i in range(len(w1))]
    t2 = [t1[-1] + 0.4 + i * each for i in range(len(w2))]
    ts = t2[-1] + 0.9
    js = "\n".join([f'tl.set("#l1 .wd:nth-child({i + 1})", {{display: "inline"}}, {t:.4f});' for i, t in enumerate(t1)]
                   + [f'tl.set("#l2 .wd:nth-child({i + 1})", {{display: "inline"}}, {t:.4f});' for i, t in enumerate(t2)]
                   + [f'tl.to("#l2", {{scale: 0.55, opacity: 0.4, duration: 0.6, ease: "power2.in"}}, {ts:.4f});',
                      f'tl.set("#l2", {{visibility: "hidden"}}, {ts + 0.6:.4f});'])
    frames = _f(ts + 0.6) + 15
    truth = {"elements": [_el("l1", "caption", "text", (_f(t1[0]), frames - 1), " ".join(w1), fg, font),
                          _el("l2", "caption_2", "text", (_f(t2[0]), _f(ts + 0.6) - 1), " ".join(w2), fg, font)],
             "structure": [{"type": "build", "targets": ["l1"], "params": {"unit": "word", "frames": [_f(t) for t in t1]}, "ir": "now"},
                           {"type": "build", "targets": ["l2"], "params": {"unit": "word", "frames": [_f(t) for t in t2]}, "ir": "now"},
                           {"type": "shrink", "targets": ["l2"], "params": {"scale": [1, 0.55], "frames": [_f(ts), _f(ts + 0.6)]}, "ir": "now"}]}
    return Template(_page(bg, font, css, body, js), truth, frames=frames)


def _whip_pan(r: random.Random, pal, font) -> Template:
    """One shot: a card, then a fast pan (a whip, blurred at its peak) to a second card. Not a cut."""
    bg, fg, acc, surf = pal
    a, b = " ".join(r.sample(WORDS, 2)), " ".join(r.sample(WORDS, 2))
    t0, dur = r.choice([1.0, 1.2]), r.choice([0.4, 0.5])
    card = f"position: absolute; top: 150px; width: 520px; height: 240px; border-radius: 24px; background: {acc}; "
    css = "\n".join(["#pan { left: 0; top: 0; width: 1920px; height: 540px; }",
                     f"#ca {{ {card} left: 220px; }} #cb {{ {card} left: 1180px; }}",
                     f".ct {{ position: absolute; left: 40px; top: 80px; font-size: 56px; font-weight: 700; color: {_ink(acc)}; white-space: nowrap; }}"])
    body = (f'<div id="pan" class="a"><div id="ca"><div id="ta" class="ct">{html.escape(a)}</div></div>'
            f'<div id="cb"><div id="tb" class="ct">{html.escape(b)}</div></div></div>')
    js = "\n".join(['tl.from("#ca", {autoAlpha: 0, y: 30, duration: 0.4, ease: "power2.out"}, 0.2);',
                    f'tl.to("#pan", {{x: -960, duration: {dur}, ease: "power3.inOut"}}, {t0});',
                    f'tl.to("#pan", {{filter: "blur(10px)", duration: {dur / 2}, yoyo: true, repeat: 1, ease: "power2.in"}}, {t0});'])
    end = _f(t0 + dur)
    truth = {"elements": [_el("ca", "card", "shape", (_f(0.2), end), colour=acc), _el("ta", "card_title", "text", (_f(0.2), end), a, _ink(acc), font),
                          _el("cb", "card_2", "shape", (_f(t0), 89), colour=acc), _el("tb", "card_title_2", "text", (_f(t0), 89), b, _ink(acc), font)],
             "structure": [{"type": "pan", "targets": ["ca", "cb"], "params": {"px": -960, "frames": [_f(t0), end]}, "ir": "P3"}]}
    return Template(_page(bg, font, css, body, js), truth, frames=90)


def _word_swap(r: random.Random, pal, font) -> Template:
    """A caption built word by word (left-aligned) whose last word is later replaced in place by an icon: the line then
    reads only its first words (a read that changes inside a shot)."""
    bg, fg, acc, _ = pal
    w = r.sample(WORDS, 3)
    each, hold = r.choice([0.25, 0.3]), r.choice([1.0, 1.2])
    css = "\n".join([".wd { display: none; white-space: pre; }",
                     f"#line {{ left: 120px; top: 230px; font-size: 64px; font-weight: 700; color: {fg}; }}",
                     f"#icon {{ display: none; width: 64px; height: 64px; border-radius: 50%; background: {acc}; vertical-align: -10px; position: relative; }}",
                     "#icon::after { content: ''; position: absolute; left: 25px; top: 18px; border-left: 18px solid #fff; "
                     "border-top: 14px solid transparent; border-bottom: 14px solid transparent; }"])
    body = (f'<div id="line" class="a"><span id="full"><span id="short"><span class="wd">{html.escape(w[0])} </span>'
            f'<span class="wd">{html.escape(w[1])} </span></span><span id="w3" class="wd">{html.escape(w[2])}</span></span>'
            '<span id="icon"></span></div>')
    t = [0.3 + i * each for i in range(3)]
    ts = t[-1] + hold
    js = "\n".join([f'tl.set("#line .wd:nth-of-type({i + 1})", {{display: "inline"}}, {x:.4f});' for i, x in enumerate(t[:2])]
                   + [f'tl.set("#w3", {{display: "inline"}}, {t[2]:.4f});', f'tl.set("#w3", {{display: "none"}}, {ts:.4f});',
                      f'tl.set("#icon", {{display: "inline-block"}}, {ts:.4f});'])
    frames = _f(ts + 1.4)
    sw = _f(ts)
    truth = {"elements": [_el("full", "caption", "text", (_f(t[0]), sw - 1), " ".join(w), fg, font),
                          _el("short", "caption_2", "text", (sw, frames - 1), " ".join(w[:2]), fg, font),
                          _el("icon", "icon", "shape", (sw, frames - 1), colour=acc)],
             "structure": []}
    return Template(_page(bg, font, css, body, js), truth, frames=frames)


def _big_scroll(r: random.Random, pal, font) -> Template:
    """A huge tracked-out headline crossing the frame right to left, fast, slow at the middle, fast again, its letters
    coming out of a blur one by one as it enters and the whole line blurring as it leaves: a word or two shows at a time."""
    bg, fg, acc, surf = pal
    text = " ".join(r.sample(WORDS, 3))
    t0, dur = 0.3, r.choice([2.4, 2.6])
    letters = "".join(f'<span class="l">{html.escape(c)}</span>' if c != " " else " " for c in text)
    css = "\n".join([f"#bg {{ inset: 0; background: linear-gradient(90deg, {surf}, {bg}); }}",
                     f"#big {{ left: 960px; top: 90px; font-size: 300px; font-weight: 800; line-height: 360px; color: {acc}; letter-spacing: 0.15em; }}",
                     "#big .l { display: inline-block; }"])
    body = f'<div id="bg" class="a"></div><div id="big" class="a">{letters}</div>'
    js = "\n".join([f'tl.to("#big", {{xPercent: -50, x: -480, duration: {dur / 2}, ease: "power2.out"}}, {t0});',
                    f'tl.to("#big", {{xPercent: -100, x: -960, duration: {dur / 2}, ease: "power2.in"}}, {t0 + dur / 2});',
                    f'tl.from("#big .l", {{opacity: 0, filter: "blur(16px)", duration: 0.4, stagger: 0.05, ease: "power1.out"}}, {t0});',
                    f'tl.to("#big", {{filter: "blur(8px)", duration: {dur / 2}, ease: "power2.in"}}, {t0 + dur / 2});'])
    frames = _f(t0 + dur) + 6
    truth = {"elements": [_el("big", "headline", "text", (_f(t0), frames - 1), text, acc, font)], "structure": []}
    return Template(_page(bg, font, css, body, js), truth, frames=frames)


def _glow_title(r: random.Random, pal, font) -> Template:
    """A title that comes in out of a blur, holds and fades away over a dark gradient whose glow rises and falls (the
    background is a moving picture, not a fitted gradient)."""
    _, _, acc, _ = pal
    dark, _ = r.choice(GROUNDS)
    word = " ".join(r.sample(WORDS, r.choice([1, 2])))
    t_in, t_out = r.choice([0.3, 0.4]), r.choice([2.0, 2.2])
    css = "\n".join([f"#s1 {{ inset: 0; background: linear-gradient(180deg, {dark} 45%, {acc} 170%); }}",
                     f"#glow {{ left: 80px; top: 300px; width: 800px; height: 480px; border-radius: 50%; "
                     f"background: radial-gradient(closest-side, {acc}, transparent); }}",
                     "#title { left: 480px; top: 240px; font-size: 110px; font-weight: 800; color: #ffffff; }"])
    body = '<div id="s1" class="a"></div><div id="glow" class="a"></div>' + f'<div id="title" class="c">{html.escape(word)}</div>'
    js = "\n".join(['tl.fromTo("#glow", {opacity: 0.25, scale: 0.85}, {opacity: 0.9, scale: 1.15, duration: 0.75, ease: "sine.inOut", '
                    'yoyo: true, repeat: 3}, 0);',
                    f'tl.fromTo("#title", {{autoAlpha: 0, filter: "blur(14px)"}}, {{autoAlpha: 1, filter: "blur(0px)", duration: 0.6, ease: "power2.out"}}, {t_in});',
                    f'tl.to("#title", {{autoAlpha: 0, filter: "blur(10px)", duration: 0.5, ease: "power2.in"}}, {t_out});'])
    truth = {"elements": [_el("title", "headline", "text", (_f(t_in), _f(t_out + 0.5) - 1), word, "#ffffff", font)], "structure": []}
    return Template(_page(dark, font, css, body, js), truth, frames=90)


def _glow_letters(r: random.Random, pal, font) -> Template:
    """A title over a pulsing glow (the background is a moving picture) whose letters come out of a blur one after
    another as the line slides in, then holds and fades: its first letters can be read while the rest are still soft."""
    _, _, acc, _ = pal
    dark, _ = r.choice(GROUNDS)
    word = " ".join(r.sample(WORDS, 2))
    t0, each = r.choice([0.3, 0.4]), r.choice([0.08, 0.1])
    n = sum(c != " " for c in word)
    t_out = t0 + each * n + 1.4
    letters = "".join(f'<span class="l">{html.escape(c)}</span>' if c != " " else " " for c in word)
    css = "\n".join([f"#s1 {{ inset: 0; background: linear-gradient(180deg, {dark} 45%, {acc} 170%); }}",
                     f"#glow {{ left: 80px; top: 300px; width: 800px; height: 480px; border-radius: 50%; "
                     f"background: radial-gradient(closest-side, {acc}, transparent); }}",
                     "#title { left: 150px; top: 170px; font-size: 104px; font-weight: 800; color: #ffffff; }",
                     "#title .l { display: inline-block; }"])
    body = '<div id="s1" class="a"></div><div id="glow" class="a"></div>' + f'<div id="title" class="a">{letters}</div>'
    js = "\n".join(['tl.fromTo("#glow", {opacity: 0.25, scale: 0.85}, {opacity: 0.9, scale: 1.15, duration: 0.75, ease: "sine.inOut", '
                    'yoyo: true, repeat: 3}, 0);',
                    f'tl.from("#title", {{x: 160, duration: {each * n + 0.5:.2f}, ease: "power2.out"}}, {t0});',
                    f'tl.from("#title .l", {{opacity: 0, filter: "blur(18px)", duration: 0.5, stagger: {each}, ease: "power1.out"}}, {t0});',
                    f'tl.to("#title", {{autoAlpha: 0, duration: 0.4, ease: "power2.in"}}, {t_out:.2f});'])
    frames = _f(t_out + 0.4) + 6
    truth = {"elements": [_el("title", "headline", "text", (_f(t0), _f(t_out + 0.4) - 1), word, "#ffffff", font)], "structure": []}
    return Template(_page(dark, font, css, body, js), truth, frames=frames)


def _glow_cut(r: random.Random, pal, font) -> Template:
    """A hard cut over one pulsing glow on one ground: a small title line cuts to a single huge word while the glow goes
    on through the cut, so most of what is off the ground stays (the cut is the title)."""
    _, _, acc, _ = pal
    dark, _ = r.choice(GROUNDS)
    words = r.sample(WORDS, 4)
    a, b = " ".join(words[:3]), words[3]
    c = r.choice([42, 45, 48])   # whole tenths of a second: a set at a rounded-up time would land a frame late
    css = "\n".join([f"#s1 {{ inset: 0; background: linear-gradient(180deg, {dark} 45%, {acc} 170%); }}",
                     f"#glow {{ left: 130px; top: 270px; width: 700px; height: 240px; border-radius: 50%; "
                     f"background: radial-gradient(closest-side, {acc}, transparent); }}",
                     "#t1 { left: 50%; top: 120px; transform: translateX(-50%); font-size: 56px; font-weight: 700; color: #ffffff; }",
                     "#t2 { left: 60px; top: -20px; font-size: 230px; font-weight: 800; color: #ffffff; visibility: hidden; }"])
    body = "\n".join(['<div id="s1" class="a"></div>', '<div id="glow" class="a"></div>',
                      f'<div id="t1" class="a">{html.escape(a)}</div>', f'<div id="t2" class="a">{html.escape(b)}</div>'])
    js = "\n".join(['tl.fromTo("#glow", {opacity: 0.35, scale: 0.9}, {opacity: 0.9, scale: 1.1, duration: 0.75, ease: "sine.inOut", '
                    'yoyo: true, repeat: 4}, 0);',
                    'tl.from("#t1", {autoAlpha: 0, y: 30, duration: 0.5, ease: "power3.out"}, 0.2);',
                    f'tl.set("#t1", {{visibility: "hidden"}}, {c / FPS:.4f});',
                    f'tl.set("#t2", {{visibility: "visible"}}, {c / FPS:.4f});'])
    truth = {"elements": [_el("t1", "headline", "text", (_f(0.2), c - 1), a, "#ffffff", font),
                          _el("t2", "headline_2", "text", (c, 119), b, "#ffffff", font)],
             "shots": [[0, c - 1], [c, 119]], "transitions": [{"type": "cut", "frames": [c, c]}], "structure": []}
    return Template(_page(dark, font, css, body, js), truth, frames=120)


def _glow_merge(r: random.Random, pal, font) -> Template:
    """A glow along the bottom of an empty dark frame, then a big word whose letters come out of a blur one after
    another just above it, close enough that their blur touches the glow: the glow alone first, glow and letters after."""
    _, _, acc, _ = pal
    dark, _ = r.choice(GROUNDS)
    word = r.choice([w for w in WORDS if 5 <= len(w) <= 7])
    t0, each = r.choice([0.5, 0.6]), r.choice([0.15, 0.2])
    letters = "".join(f'<span class="l">{html.escape(c)}</span>' for c in word)
    css = "\n".join([f"#s1 {{ inset: 0; background: linear-gradient(180deg, {dark} 55%, {acc} 190%); }}",
                     f"#glow {{ left: -120px; top: 330px; width: 1200px; height: 420px; border-radius: 50%; "
                     f"background: radial-gradient(closest-side, {acc}, transparent); }}",
                     "#title { left: 90px; top: 230px; font-size: 190px; font-weight: 800; color: #ffffff; }",
                     "#title .l { display: inline-block; }"])
    body = '<div id="s1" class="a"></div><div id="glow" class="a"></div>' + f'<div id="title" class="a">{letters}</div>'
    js = "\n".join(['tl.fromTo("#glow", {opacity: 0.7}, {opacity: 0.9, duration: 1, ease: "sine.inOut", yoyo: true, repeat: 3}, 0);',
                    f'tl.from("#title .l", {{opacity: 0, filter: "blur(30px)", duration: 1.2, stagger: {each}, ease: "power1.in"}}, {t0});'])
    truth = {"elements": [_el("title", "headline", "text", (_f(t0), 119), word, "#ffffff", font)], "structure": []}
    return Template(_page(dark, font, css, body, js), truth, frames=120)


def _label_grid(r: random.Random, pal, font) -> Template:
    """A dense board: a grid of cells, each a bold heading in one family over a regular label in another, popping in
    row by row (more texts than a scene's font-matching budget)."""
    bg, fg, _, _ = pal
    font2 = r.choice([f for f in FONTS if f != font])
    pairs = r.sample([f"{a} {b}" for a in WORDS for b in WORDS if a != b], 48)
    heads, labels = pairs[:24], [p.lower() for p in pairs[24:]]
    css = "\n".join([f".h {{ position: absolute; font-size: 21px; font-weight: 700; color: {fg}; white-space: nowrap; }}",
                     f".l {{ position: absolute; font-family: G, sans-serif; font-size: 16px; font-weight: 400; color: {fg}; white-space: nowrap; }}"])
    body, js, elements = [], [], []
    for i in range(24):
        x, y = 30 + (i % 4) * 240, 18 + (i // 4) * 88
        body += [f'<div id="h{i}" class="h" style="left: {x}px; top: {y}px">{html.escape(heads[i])}</div>',
                 f'<div id="l{i}" class="l" style="left: {x}px; top: {y + 32}px">{html.escape(labels[i])}</div>']
        t = 0.2 + (i // 4) * 0.2
        js.append(f'tl.from(["#h{i}", "#l{i}"], {{autoAlpha: 0, y: 12, duration: 0.3, ease: "power2.out"}}, {t:.4f});')
        elements += [_el(f"h{i}", f"heading_{i}", "text", (_f(t), 89), heads[i], fg, font),
                     _el(f"l{i}", f"label_{i}", "text", (_f(t), 89), labels[i], fg, font2)]
    truth = {"elements": elements, "structure": []}
    return Template(_page(bg, font, css, "\n".join(body), "\n".join(js), font2=font2), truth, frames=90)


_MAKERS = {"kinetic_title": _kinetic_title, "logo_sting": _logo_sting, "chat_ui": _chat_ui, "scroller": _scroller,
           "block_transition": _block_transition, "prompt_typing": _prompt_typing, "counter": _counter, "stepped_zoom": _stepped_zoom,
           "hard_cut": _hard_cut, "wipe": _wipe, "word_build": _word_build, "whip_pan": _whip_pan, "word_swap": _word_swap,
           "big_scroll": _big_scroll, "glow_title": _glow_title, "label_grid": _label_grid, "glow_letters": _glow_letters,
           "glow_cut": _glow_cut, "glow_merge": _glow_merge}


def make_template(kind: str, seed: int) -> Template:
    """A seeded reference page and its truth; the same (kind, seed) always gives the same page."""
    r = random.Random(f"kfscript-synth:{kind}:{seed}")
    t = _MAKERS[kind](r, r.choice(PALETTES), r.choice(FONTS))
    t.truth.update({"kind": kind, "seed": seed, "size": list(t.size), "fps": t.fps, "frames": t.frames})
    t.truth.setdefault("shots", [[0, t.frames - 1]])
    return t


def template_id(t: Template) -> str:
    """Changes whenever the page or its declared truth changes (round-trip --reuse keys on it); not with the checkout
    the bundled fonts and GSAP are linked from."""
    doc = t.doc.replace(VENDOR.as_uri(), "vendor:").replace(FONT_DIR.as_uri(), "fonts:")
    return hashlib.sha1((doc + json.dumps(t.truth, sort_keys=True)).encode()).hexdigest()[:12]


def _clip(box, size) -> list[float]:
    x0, y0, x1, y1 = box
    return [round(min(max(x0, 0), size[0]), 1), round(min(max(y0, 0), size[1]), 1),
            round(min(max(x1, 0), size[0]), 1), round(min(max(y1, 0), size[1]), 1)]


def probe_boxes(page: Path, truth: dict, frames: bool = True) -> np.ndarray | None:
    """Fill truth["elements"][*]["boxes"] (clipped to the stage) and narrow "visible" to frames with area on the stage,
    probing the page through window.__bbox. Returns the rendered frames (uint8) unless `frames` is False (a 1080p gold
    page's 482 frames are 3 GB)."""
    import tempfile
    from ..ir.schema import Background, Canonical, Element, Scene
    from ..render.renderer import load_frame, render
    size, n = tuple(truth["size"]), truth["frames"]
    ids = [e["id"] for e in truth["elements"]]
    stub = Scene(id="truth", size=size, fps=truth["fps"], frames=n, background=Background(kind="color", value="#000000"),
                 elements=[Element(id=i, kind="sprite", visible=(0, n - 1), canonical=Canonical(width=1, height=1)) for i in ids])
    with tempfile.TemporaryDirectory(prefix="keepframe-truth-") as td:
        res = render(page, stub, Path(td), probe=True)
        out = np.stack([(load_frame(res.frames_dir / f"f_{i:05d}.png") * 255).round().astype(np.uint8) for i in range(n)]) if frames else None
    for e in truth["elements"]:
        a, b = e["visible"]
        boxes = {f: _clip(res.bboxes[e["id"]][f], size) for f in range(a, b + 1)}
        on = [f for f, x in boxes.items() if x[2] > x[0] and x[3] > x[1]]
        e["visible"] = [on[0], on[-1]] if on else [a, a - 1]
        e["boxes"] = {str(f): boxes[f] for f in on}
    return out


def render_template(kind: str, seed: int, out: Path) -> tuple[Path, Path]:
    """page.html, source.mp4 (as the analysis will see it) and truth.json with Chromium-probed boxes per visible frame."""
    from ..analyze.video import write_video
    out = Path(out)
    out.mkdir(parents=True, exist_ok=True)
    t = make_template(kind, seed)
    page = out / "page.html"
    page.write_text(t.doc)
    tid = template_id(t)
    mp4 = write_video(probe_boxes(page, t.truth), t.fps, out / "source.mp4")
    truth = out / "truth.json"
    truth.write_text(json.dumps({**t.truth, "template": tid}, indent=1))
    return mp4, truth
