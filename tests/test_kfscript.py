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
