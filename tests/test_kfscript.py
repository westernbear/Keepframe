import re

from keepframe.ir.schema import Background, Canonical, Element, Keyframe, Scene, Track
from keepframe.ir.synth import make_synthetic_scene
from keepframe.ir.tracks import PRESET_EASES
from keepframe.kfscript import emit_page, header, timeline_js

OUT_CUBIC = PRESET_EASES["out_cubic"]


def _scene(elements, frames=60):
    return Scene(id="k", size=(320, 180), fps=30.0, frames=frames, elements=elements,
                 background=Background(kind="color", value="#101418"))


def _el(eid, tracks, **kw):
    return Element(id=eid, kind="sprite", visible=(0, 59), canonical=Canonical(width=20, height=10), tracks=tracks, **kw)


def test_emit_header_and_ids_match_ir(tmp_scene_dir):
    scene = make_synthetic_scene(tmp_scene_dir, seed=1)
    page = emit_page(scene, tmp_scene_dir, tmp_scene_dir / "kf.html").read_text()
    assert page.startswith("<!doctype html>\n<!-- kf-script v0 · synth1")
    assert page.index("-->") < page.index("<html>")
    assert "const EXPLICIT = true;" in page
    for e in scene.elements:
        assert f'id="el-{e.id}"' in page and f'S["{e.id}"]' in page
    assert 'data-role="' in page and 'data-conf="' in page


def test_shared_segments_merge():
    keys = lambda a, b: Track(keys=[Keyframe(t=0, v=a, ease=OUT_CUBIC), Keyframe(t=12, v=b)])
    js = timeline_js(_scene([_el("e1", {"x": keys(0, 100), "y": keys(5, 50), "opacity": Track(keys=[Keyframe(t=6, v=0), Keyframe(t=30, v=1)])})]))
    tweens = [l for l in js.splitlines() if l.startswith("tl.fromTo")]
    assert len(tweens) == 2
    assert tweens[0].startswith('tl.fromTo(S["e1"], {x: 0, y: 5}, {x: 100, y: 50, duration: 0.4000, ease: "out_cubic"}, 0.0000);')
    assert 'ease: "none"}, 0.2000);   // f6–f30' in tweens[1]
    assert 'gsap.set(S["e1"], {x: 0, y: 5, opacity: 0});' in js


def test_preset_eases_registered_by_name():
    lin = Track(keys=[Keyframe(t=0, v=0, ease=PRESET_EASES["linear"]), Keyframe(t=10, v=1)])
    odd = Track(keys=[Keyframe(t=0, v=0, ease=(0.2, 0.1, 0.3, 0.9)), Keyframe(t=10, v=1)])
    out = Track(keys=[Keyframe(t=0, v=0, ease=OUT_CUBIC), Keyframe(t=10, v=1, ease=OUT_CUBIC), Keyframe(t=20, v=2)])
    js = timeline_js(_scene([_el("a", {"x": lin, "y": odd}), _el("b", {"x": out})]))
    assert js.count('CustomEase.create("out_cubic", "M0,0 C0.33,1 0.68,1 1,1");') == 1
    assert 'CustomEase.create("ease1", "M0,0 C0.2,0.1 0.3,0.9 1,1");' in js
    assert '"linear"' not in js and 'ease: "none"' in js


def test_times_four_decimals():
    tr = Track(keys=[Keyframe(t=1, v=0), Keyframe(t=8, v=1)])
    scene = _scene([_el("e1", {"x": tr})]).model_copy(update={"fps": 29.97})
    line = next(l for l in timeline_js(scene).splitlines() if l.startswith("tl.fromTo"))
    assert re.search(r"duration: 0\.2335, ease: \"none\"\}, 0\.0334\);   // f1–f8$", line), line


def test_state_keys_are_json_escaped(tmp_scene_dir):
    bad = 'e"</script><script>window.pwned=1</script>'
    scene = _scene([_el(bad, {"x": Track(keys=[Keyframe(t=0, v=0), Keyframe(t=5, v=9)])}, caption="</div><b>x</b>")])
    js = timeline_js(scene)
    assert "</script>" not in js and "\\u003c/script\\u003e" in js
    assert "-->" not in header(scene)[4:-5]
    page = emit_page(scene, tmp_scene_dir, tmp_scene_dir / "kf.html").read_text()
    assert "window.pwned=1</script>" not in page.split("<body>")[1].split("<script>")[0]
    assert "&lt;/div&gt;&lt;b&gt;x&lt;/b&gt;" in page


def test_view_strips_vendor_and_data_uris(tmp_scene_dir):
    from keepframe.kfscript.emit import view
    scene = make_synthetic_scene(tmp_scene_dir, seed=1)
    text = view(scene, tmp_scene_dir)
    assert text.startswith("<!-- kf-script v0 · synth1") and '<div id="stage" style="width:640px;height:360px;' in text
    assert "data:" not in text and "gsap 3.12" not in text.lower() and "@font-face" not in text
    sprite = next(e for e in scene.elements if e.canonical.texture)
    assert f'src="{sprite.canonical.texture}"' in text
    assert all(f'S["{e.id}"]' in text for e in scene.elements if e.tracks) and "one-line summaries" not in text


def test_view_budget_by_rank(tmp_scene_dir):
    from keepframe.ir.importance import rank_elements
    from keepframe.kfscript.emit import view
    scene = make_synthetic_scene(tmp_scene_dir, seed=2, n_elements=8)
    full = view(scene, tmp_scene_dir, max_chars=10**9)
    small = view(scene, tmp_scene_dir, max_chars=len(full) // 2)
    assert len(small) < len(full) and "one-line summaries" in small
    first = rank_elements(scene)[0].id
    assert f'id="el-{first}"' in small
    stubbed = [e.id for e in scene.elements if f'id="el-{e.id}"' not in small]
    assert stubbed and all(f"<!-- {i} " in small and f'S["{i}"]' not in small for i in stubbed)


def test_cli_kfscript_page_and_view(tmp_scene_dir, capsys):
    import json
    from keepframe.cli import main
    assert main(["synth", "--out", str(tmp_scene_dir)]) == 0
    capsys.readouterr()
    assert main(["kfscript", "--scene", str(tmp_scene_dir / "scene.json"), "--out", str(tmp_scene_dir / "kf.html"),
                 "--view", str(tmp_scene_dir / "view.txt")]) == 0
    out = json.loads(capsys.readouterr().out)
    assert out["page_chars"] > out["view_chars"] > 0
    assert (tmp_scene_dir / "kf.html").read_text().startswith("<!doctype html>\n<!-- kf-script v0")
    assert (tmp_scene_dir / "view.txt").read_text().startswith("<!-- kf-script v0")


def test_view_summarises_every_element_and_prints_groups(tmp_scene_dir):
    """V1/V2: each shown element has its summary comment (size, entrance position, visible seconds) before its markup,
    so the agent need not dig the position out of the script; groups appear as in the brief."""
    from keepframe.ir.schema import GlyphReveal, Group
    from keepframe.kfscript.emit import view
    scene = make_synthetic_scene(tmp_scene_dir, seed=2, n_elements=4)
    el = scene.elements[0]
    el.canonical.glyphs = GlyphReveal(x=[0.2, 0.8], delays=[0, 6], ramp=2)
    scene.groups = [Group(id="g1", members=[e.id for e in scene.elements[:2]], reason="moves together")]
    v = view(scene, tmp_scene_dir, max_chars=10**9)
    for e in scene.elements:
        line = next(ln for ln in v.splitlines() if ln.startswith(f"<!-- {e.id} "))
        assert re.search(r"\d+x\d+px at \(-?\d+, -?\d+\) visible \d+\.\d\d–\d+\.\d\d s", line) and "motion not shown" not in line
        assert v.index(line) < v.index(f'id="el-{e.id}"')
    assert "types glyph by glyph" in next(ln for ln in v.splitlines() if ln.startswith(f"<!-- {el.id} "))
    assert f"group g1: {scene.elements[0].id}, {scene.elements[1].id} (moves together)" in v


def test_view_steps_start_and_end_at_real_changes():
    """Holds before the first change and after the last are not printed as steps (they read as motion)."""
    from keepframe.kfscript.emit import _element_view_js
    el = _el("e1", {"x": Track(keys=[Keyframe(t=0, v=10.0), Keyframe(t=6, v=10.0), Keyframe(t=12, v=90.0),
                                     Keyframe(t=20, v=90.0), Keyframe(t=30, v=40.0), Keyframe(t=59, v=40.0)]),
                    "y": Track(keys=[Keyframe(t=0, v=5.0)])})
    js = _element_view_js(el, 30.0, {})
    assert "tl.to(S[\"e1\"], {keyframes: [{x: 90, duration: 0.200" in js and "0.200);   // x f6–f30" in js
    assert js.count("tl.to") == 1   # the constant y is only the gsap.set


def test_view_shows_sub_tolerance_changes_as_holds():
    """A 1 px settle after a move and ±0.5 px wiggles are within the analysis tolerance (2 px): the view does not print
    them as motion. A slow drift that adds up past the tolerance still shows."""
    from keepframe.kfscript.emit import _element_view_js
    el = _el("e1", {"x": Track(keys=[Keyframe(t=0, v=10.0, ease=OUT_CUBIC), Keyframe(t=12, v=248.0), Keyframe(t=20, v=248.5),
                                     Keyframe(t=30, v=247.6), Keyframe(t=59, v=247.0)]),
                    "y": Track(keys=[Keyframe(t=0, v=50.0), Keyframe(t=20, v=51.0), Keyframe(t=40, v=52.0), Keyframe(t=59, v=53.0)])})
    js = _element_view_js(el, 30.0, {tuple(OUT_CUBIC): "out_cubic"})
    x = next(ln for ln in js.splitlines() if "// x" in ln)
    assert x.endswith("// x f0–f12") and "248.5" not in x and "247" not in x
    y = next(ln for ln in js.splitlines() if "// y" in ln)
    assert "y: 53" in y and "y: 51" not in y   # 50 → 53 shows once past 2 px; the 1 px steps do not
