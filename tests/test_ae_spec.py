import hashlib
import math
import os
import json
import struct
import zlib
from pathlib import Path

import numpy as np
import pytest
import subprocess

from keepframe.ae.spec import comp_spec, comp_spec_json, ease_to_ae, png_size, spec_asset_paths
from keepframe.ir.schema import Background, Canonical, Element, FontGuess, Group, Keyframe, Scene, Track


EASE = (0.25, 0.5, 0.75, 0.5)
CONTEXT = {"project": "demo", "scene_id": "s1", "version": "v3"}


def track(*keys):
    return Track(keys=[Keyframe(t=k[0], v=k[1], ease=k[2] if len(k) > 2 else None) for k in keys])


def element(eid="e1", kind="sprite", *, tracks=None, z=None, visible=(2, 30), **canonical):
    return Element(id=eid, kind=kind, canonical=Canonical(width=10, height=6, **canonical),
                   visible=visible, tracks=tracks or {}, z=z or track((0, 0)))


def scene(*elements, **kwargs):
    return Scene(id="stored-id", size=(320, 180), fps=30, frames=60,
                 background=kwargs.pop("background", Background(value="#AbC")),
                 elements=list(elements), **kwargs)


def png(path, width=20, height=12):
    def chunk(kind, data):
        return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data))

    path.parent.mkdir(parents=True, exist_ok=True)
    data = b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 6, 0, 0, 0))
    data += chunk(b"IDAT", zlib.compress((b"\0" + b"\xff\0\0\xff" * width) * height))
    data += chunk(b"IEND", b"")
    path.write_bytes(data)
    return data


def describe(scene_value, root, **kwargs):
    return comp_spec(scene_value, root, **CONTEXT, **kwargs)


def layer(spec, eid="e1"):
    return next(item for item in spec["layers"] if item["id"] == f"kf:{eid}")


def test_comp_contract_and_color_background(tmp_path):
    spec = describe(scene(), tmp_path)
    assert set(spec) == {"schema", "project", "scene", "version", "comp", "assets", "layers", "warnings"}
    assert spec["schema"] == "keepframe.ae-comp/1"
    assert (spec["project"], spec["scene"], spec["version"]) == ("demo", "s1", "v3")
    assert spec["comp"] == {"tag": "keepframe:demo/s1", "name": "Keepframe · demo · s1",
                            "width": 320, "height": 180, "fps": 30, "frames": 60}
    bg = layer(spec, "background")
    assert (bg["kind"], bg["source"], bg["order"], bg["in"], bg["out"]) == (
        "solid", {"color": "#aabbcc"}, 0, 0, 59)
    assert bg["anchor"] == [0, 0]
    assert bg["props"] == {"position_x": [[0, 0, None, None]], "position_y": [[0, 0, None, None]],
                           "scale": [[0, [100, 100], None, None]], "rotation": [[0, 0, None, None]],
                           "opacity": [[0, 100, None, None]]}
    assert bg["effects"] == {"skew": None, "reveal": None}
    assert spec["assets"] == spec["warnings"] == []


@pytest.mark.parametrize("ease,v0,v1,dt,expected", [
    (EASE, 10, 110, 2, {"out": [25, 100], "in": [25, 100]}),
    ((0.4, 0.1, 0.8, 0.6), 0, 60, 2, {"out": [40, 7.5], "in": [20, 60]}),
    (EASE, 110, 10, 2, {"out": [25, -100], "in": [25, -100]}),
    ((0, 1, 1, 0), 0, 100, 1, {"out": [0.1, 0], "in": [0.1, 0]}),
    ((2, 1, -1, 0), 0, 100, 1, {"out": [100, 50], "in": [100, 50]}),
    (EASE, 5, 5, 1, {"out": [25, 0], "in": [25, 0]}),
    ((1 / 3, 0.2, 2 / 3, 0.7), 0, 1, 3, {"out": [33.3333, 0.2], "in": [33.3333, 0.3]}),
])
def test_ease_conversion(ease, v0, v1, dt, expected):
    assert ease_to_ae(ease, v0, v1, dt) == expected


def test_image_pixel_anchor_scale_fix_and_property_mapping(tmp_path):
    data = png(tmp_path / "assets" / "original.png")
    el = element(texture="assets/original.png", anchor=(0.25, 0.75), tracks={
        "x": track((3, 10, EASE), (33, 110), (45, 120, EASE)), "y": track((5, 25)),
        "sx": track((0, 1, EASE), (30, 3)), "sy": track((0, 2), (30, 4)),
        "rot": track((0, -10), (30, 50)), "opacity": track((0, 0.2, EASE), (30, 0.8)),
        "rx": track((0, 90)), "ry": track((0, 45)),
    })
    spec = describe(scene(el), tmp_path)
    image = layer(spec)
    assert (image["kind"], image["name"], image["label"], image["in"], image["out"]) == (
        "image", "e1 · sprite", None, 2, 30)
    assert image["source"] == {"asset": "e1.png", "scale_fix": [0.5, 0.5]}
    assert image["anchor"] == [5, 9]
    assert image["props"] == {
        "position_x": [[3, 10, [[25, 200]], None], [33, 110, None, [[25, 200]]], [45, 120, None, None]],
        "position_y": [[5, 25, None, None]],
        "scale": [[0, [50, 100], [[25, 200], [25, 200]], None],
                  [30, [150, 200], None, [[25, 200], [25, 200]]]],
        "rotation": [[0, -10, None, None], [30, 50, None, None]],
        "opacity": [[0, 20, [[25, 120]], None], [30, 80, None, [[25, 120]]]],
    }
    assert image["effects"] == {"skew": None, "reveal": None}
    assert image["warnings"] == []
    assert spec["assets"] == [{"name": "e1.png", "sha256": hashlib.sha256(data).hexdigest(), "bytes": len(data)}]


def test_scale_merge_union_evaluates_tracks_and_selects_ease(tmp_path):
    el = element(kind="group", tracks={"sx": track((0, 1, EASE), (30, 3)),
                                       "sy": track((0, 2), (15, 3, EASE), (30, 4))})
    result = layer(describe(scene(el), tmp_path))
    assert result["props"]["scale"] == [
        [0, [100, 200], [[25, 400], [25, 400]], None],
        [15, [200, 300], [[25, 400], [25, 400]], [[25, 400], [25, 400]]],
        [30, [300, 400], None, [[25, 400], [25, 400]]],
    ]
    assert result["warnings"] == ["e1 has no image; not drawn in AE", "scale x/y keyframes of e1 merged"]


def test_scale_merge_prefers_sx_key_even_when_linear_and_fills_defaults(tmp_path):
    el = element(kind="group", tracks={"sx": track((0, 1), (30, 2)),
                                       "sy": track((0, 1, EASE), (15, 2), (30, 3))})
    result = layer(describe(scene(el), tmp_path))
    assert result["props"]["scale"][0][2] is None
    only_y = element(kind="group", tracks={"sy": track((10, 2), (30, 3))})
    assert layer(describe(scene(only_y), tmp_path))["props"]["scale"] == [
        [0, [100, 200], None, None], [10, [100, 200], None, None], [30, [100, 300], None, None]]


def test_scale_easing_is_per_dimension_with_distinct_in_and_out_speeds(tmp_path):
    el = element(kind="group", tracks={"sx": track((5, 1, (0.4, 0.1, 0.8, 0.6)), (20, 3)),
                                       "sy": track((5, 2, EASE), (20, 1))})
    assert layer(describe(scene(el), tmp_path))["props"]["scale"] == [
        [5, [100, 200], [[40, 100], [40, -50]], None],
        [20, [300, 100], None, [[20, 800], [20, -400]]]]


def test_scale_merge_evaluates_bezier_value_at_other_axis_key(tmp_path):
    # x(s) = s, y(s) = s**3: halfway through sx gives 1 + 8/8 = 2, not 5.
    el = element(kind="group", tracks={"sx": track((0, 1, (1 / 3, 0, 2 / 3, 0)), (30, 9)),
                                       "sy": track((0, 1), (15, 4), (30, 3))})
    result = layer(describe(scene(el), tmp_path))
    assert [key[:2] for key in result["props"]["scale"]] == [
        [0, [100, 100]], [15, [200, 400]], [30, [900, 300]]]
    assert result["warnings"].count("scale x/y keyframes of e1 merged") == 1


def test_skew_uses_union_and_compensates_for_nonuniform_scale(tmp_path):
    el = element(kind="group", tracks={"skx": track((0, 45, EASE), (30, 45)),
                                       "sx": track((0, 2), (30, 2)),
                                       "sy": track((0, 1), (15, 2), (30, 4)), "sky": track((0, 0), (10, 3))})
    result = layer(describe(scene(el), tmp_path))
    assert result["effects"]["skew"] == {"skew": [[0, 26.5651, None, None], [15, 45, None, None],
                                                            [30, 63.4349, None, None]], "axis": 0}
    assert result["warnings"].count("y skew of e1 is not represented") == 1


def test_zero_skew_and_y_skew_only(tmp_path):
    el = element(kind="group", tracks={"skx": track((0, 0), (30, 0)), "sky": track((0, -1))})
    result = layer(describe(scene(el), tmp_path))
    assert result["effects"]["skew"] is None
    assert "y skew of e1 is not represented" in result["warnings"]


def test_reveal_completion_and_ease_speed(tmp_path):
    el = element(kind="group", tracks={"reveal": track((0, 0.25, EASE), (30, 1))})
    assert layer(describe(scene(el), tmp_path))["effects"]["reveal"] == {
        "completion": [[0, 75, [[25, -150]], None], [30, 0, None, [[25, -150]]]], "angle": 270, "feather": 0}


@pytest.mark.parametrize("keys", [None, [(0, 1)], [(0, 1), (30, 1)]])
def test_absent_or_full_reveal_has_no_effect(tmp_path, keys):
    el = element(kind="group", tracks={} if keys is None else {"reveal": track(*keys)})
    assert layer(describe(scene(el), tmp_path))["effects"]["reveal"] is None


@pytest.mark.parametrize("kind", ["sprite", "ui", "group", "3d"])
def test_texture_kinds_and_3d_candidate(tmp_path, kind):
    png(tmp_path / "texture.png", 10, 6)
    result = layer(describe(scene(element(kind=kind, texture="texture.png")), tmp_path))
    assert result["kind"] == "image"
    assert result["source"] == {"asset": "e1.png", "scale_fix": [1, 1]}
    assert result["warnings"] == (["3D candidate"] if kind == "3d" else [])


@pytest.mark.parametrize("kind", ["sprite", "ui", "group", "text"])
def test_missing_image_becomes_null_layer(tmp_path, kind):
    result = layer(describe(scene(element(kind=kind)), tmp_path))
    assert result["kind"] == "null"
    assert result["source"] is None
    assert result["anchor"] == [5, 3]
    assert result["warnings"] == ["e1 has no image; not drawn in AE"]


def test_model_source_fit_box_anchor_rotations_and_model_priority(tmp_path):
    data = b"tiny GLB fixture"
    (tmp_path / "object.glb").write_bytes(data)
    el = element(kind="3d", model="object.glb", texture="unused.png", anchor=(0.2, 0.5),
                 tracks={"rx": track((0, 10, EASE), (30, 30)), "ry": track((7, -45))})
    spec = describe(scene(el), tmp_path)
    result = layer(spec)
    assert result["kind"] == "model"
    assert result["source"] == {"asset": "e1.glb", "fit_box": [10, 6]}
    assert result["anchor"] == [2, 3]
    assert result["props"]["rotation_x"] == [[0, 10, [[25, 40]], None], [30, 30, None, [[25, 40]]]]
    assert result["props"]["rotation_y"] == [[7, -45, None, None]]
    assert result["warnings"] == []
    assert spec["assets"] == [{"name": "e1.glb", "sha256": hashlib.sha256(data).hexdigest(), "bytes": len(data)}]


def test_text_anchor_font_deferred_color_default_and_name(tmp_path):
    el = element(kind="text", text="안녕", font=FontGuess(family_guess="Example", size_px=20.123456),
                 texture="unused.png", anchor=(0.25, 0.75), tracks={"rx": track((0, 90)), "ry": track((0, 90))})
    el.label = "제목"
    spec = describe(scene(el), tmp_path)
    result = layer(spec)
    assert result["kind"] == "text" and result["anchor"] is None
    assert result["name"] == "e1 · 제목"
    assert result["source"] == {"text": "안녕", "font": {"postscript": None, "family": "Example", "style": None,
                                                       "substituted": False},
                                "size_px": 20.1235, "color": "#000000", "anchor_fraction": [0.25, 0.75], "box": [10, 6]}
    assert "rotation_x" not in result["props"] and "rotation_y" not in result["props"]
    assert spec["assets"] == [] and result["warnings"] == []


def test_text_color_normalized(tmp_path):
    el = element(kind="text", text="A", font=FontGuess(), color="#A0b1C2")
    assert layer(describe(scene(el), tmp_path))["source"]["color"] == "#a0b1c2"


def test_text_without_font_keeps_texture_anchor_scale_and_effects(tmp_path):
    png(tmp_path / "glyphs.png")
    el = element(kind="text", text="Reveal 한", texture="glyphs.png", anchor=(0.25, 0.75),
                 tracks={"sx": track((0, 2)), "sy": track((0, 3)),
                         "reveal": track((0, 0), (30, 1)), "skx": track((0, 10))})
    result = layer(describe(scene(el), tmp_path, fonts=[]))
    assert result["kind"] == "image"
    assert result["source"] == {"asset": "e1.png", "scale_fix": [0.5, 0.5], "text": "Reveal 한"}
    assert result["anchor"] == [5, 9]
    assert result["props"]["scale"] == [[0, [100, 150], None, None]]
    assert result["effects"] == layer(describe(scene(el.model_copy(update={"kind": "sprite"})), tmp_path))["effects"]
    assert result["effects"]["reveal"] is not None and result["effects"]["skew"] is not None
    assert result["warnings"] == ["text e1 kept as an image (no font detected)"]


@pytest.mark.parametrize("style,weight", [("Thin", 100), ("ExtraLight", 200), ("Ultra Light", 200),
    ("Light", 300), ("Regular", 400), ("Normal", 400), ("Book", 400), ("Roman", 400), ("Unknown", 400),
    ("Medium", 500), ("SemiBold", 600), ("Demi Bold", 600), ("Bold", 700), ("ExtraBold", 800),
    ("Ultra Bold", 800), ("Black", 900), ("Heavy", 900)])
def test_font_style_weights(tmp_path, style, weight):
    competing = "Black" if weight < 900 else "Thin"
    fonts = [{"family": "Example", "style": competing, "postscript": "Other"},
             {"family": "Example", "style": style, "postscript": "Chosen"}]
    el = element(kind="text", text="A", font=FontGuess(family_guess="example", weight=weight))
    assert layer(describe(scene(el), tmp_path, fonts=fonts))["source"]["font"] == {
        "postscript": "Chosen", "family": "Example", "style": style, "substituted": False}


def test_fonts_family_then_candidate_order_and_nearest_weight(tmp_path):
    fonts = [{"family": "Second", "style": "Medium", "postscript": "Second-Medium"},
             {"family": "First", "style": "Bold", "postscript": "First-Bold"},
             {"family": "First", "style": "SemiBold", "postscript": "First-SemiBold"},
             {"family": "Primary", "style": "Regular", "postscript": "Primary-Regular"}]
    el = element(kind="text", text="A", font=FontGuess(family_guess="primary", weight=620,
                                                       candidates=["FIRST", "second"]))
    assert layer(describe(scene(el), tmp_path, fonts=fonts))["source"]["font"]["postscript"] == "Primary-Regular"
    el.canonical.font.family_guess = "Absent"
    result = layer(describe(scene(el), tmp_path, fonts=fonts))
    assert result["source"]["font"]["postscript"] == "First-SemiBold"
    assert result["warnings"] == []


@pytest.mark.parametrize("italic", ["Italic", "Oblique"])
def test_font_ties_prefer_nonitalic(tmp_path, italic):
    fonts = [{"family": "Example", "style": f"Bold {italic}", "postscript": "Italic"},
             {"family": "Example", "style": "Medium", "postscript": "Upright"}]
    el = element(kind="text", text="A", font=FontGuess(family_guess="Example", weight=600))
    assert layer(describe(scene(el), tmp_path, fonts=fonts))["source"]["font"]["postscript"] == "Upright"


@pytest.mark.parametrize("weight,postscript,style", [
    (400, "ArialMT", "Regular"), (599, "ArialMT", "Regular"),
    (600, "Arial-BoldMT", "Bold"), (700, "Arial-BoldMT", "Bold"),
])
def test_font_missing_falls_back_to_arial(tmp_path, weight, postscript, style):
    el = element(kind="text", text="가을 여행", font=FontGuess(family_guess="Absent", weight=weight))
    result = layer(describe(scene(el), tmp_path, fonts=[]))
    assert result["source"]["font"] == {"postscript": postscript, "family": "Arial", "style": style,
                                         "substituted": True}
    assert result["warnings"] == [f"font Absent not installed; using Arial {style}"]


def test_font_null_style_ranks_as_regular_and_preserves_null_postscript(tmp_path):
    fonts = [{"family": "Example", "style": "Bold", "postscript": "Bold"},
             {"family": "Example", "style": None, "postscript": None}]
    el = element(kind="text", text="A", font=FontGuess(family_guess="Example", weight=400))
    assert layer(describe(scene(el), tmp_path, fonts=fonts))["source"]["font"] == {
        "postscript": None, "family": "Example", "style": None, "substituted": False}


def test_spec_asset_paths_share_spec_names_and_model_text_precedence(tmp_path):
    from keepframe.ae.spec import spec_asset_paths
    png(tmp_path / "deep" / "texture.png")
    png(tmp_path / "background.png")
    (tmp_path / "model.glb").write_bytes(b"GLB")
    value = scene(element("a/b", texture="deep/texture.png"),
                  element("model", kind="3d", model="model.glb", texture="unused.png"),
                  element("text", kind="text", text="A", font=FontGuess(), texture="unused.png"),
                  element("glyphs", kind="text", text="A", texture="deep/texture.png"),
                  element("empty", kind="text"),
                  background=Background(kind="image", value="background.png"))
    paths = spec_asset_paths(value, tmp_path)
    assert paths == {"a_b.png": tmp_path / "deep" / "texture.png", "model.glb": tmp_path / "model.glb",
                     "background.png": tmp_path / "background.png", "glyphs.png": tmp_path / "deep" / "texture.png"}
    assert set(paths) == {asset["name"] for asset in describe(value, tmp_path)["assets"]}


def test_unknown_style_does_not_match_weight_substrings(tmp_path):
    fonts = [{"family": "Example", "style": "Light", "postscript": "Light"},
             {"family": "Example", "style": "Highlight", "postscript": "Unknown"}]
    el = element(kind="text", text="A", font=FontGuess(family_guess="Example", weight=400))
    assert layer(describe(scene(el), tmp_path, fonts=fonts))["source"]["font"]["postscript"] == "Unknown"


def test_image_background_is_bottom_and_fills_comp(tmp_path):
    data = png(tmp_path / "plate.png", 160, 60)
    spec = describe(scene(element(kind="group", z=track((0, -100))),
                          background=Background(kind="image", value="plate.png")), tmp_path)
    bg = spec["layers"][0]
    assert (bg["id"], bg["kind"], bg["order"], bg["in"], bg["out"]) == ("kf:background", "image", 0, 0, 59)
    assert bg["source"] == {"asset": "background.png", "scale_fix": [2, 3]}
    assert bg["anchor"] == [0, 0]
    assert bg["props"]["position_x"] == bg["props"]["position_y"] == [[0, 0, None, None]]
    assert bg["props"]["scale"] == [[0, [200, 300], None, None]]
    assert spec["assets"] == [{"name": "background.png", "sha256": hashlib.sha256(data).hexdigest(), "bytes": len(data)}]


def test_order_first_visible_z_ties_and_one_warning_per_element(tmp_path):
    a = element("a", kind="group", visible=(10, 20), z=track((0, -1), (10, 5), (15, -2), (20, 6), (25, 10)))
    b = element("b", kind="group", z=track((0, 2)))
    c = element("c", kind="group", z=track((0, 2)))
    d = element("d", kind="group", visible=(10, 20), z=track((0, 100), (5, 0), (25, -100)))
    spec = describe(scene(a, b, c, d), tmp_path)
    assert [item["id"] for item in spec["layers"]] == ["kf:background", "kf:d", "kf:b", "kf:c", "kf:a"]
    assert [item["order"] for item in spec["layers"]] == [0, 1, 2, 3, 4]
    assert spec["warnings"] == ["z-order of a changes over time; ordered by its first visible frame"]


def test_group_labels_wrap_at_sixteen_and_follow_group_order(tmp_path):
    elements = [element(f"e{i}", kind="group") for i in range(18)]
    groups = [Group(id=f"g{i}", members=[f"e{i}"]) for i in range(17)]
    groups[0].members.append("e17")
    spec = describe(scene(*elements, groups=groups), tmp_path)
    assert [layer(spec, f"e{i}")["label"] for i in range(18)] == list(range(1, 17)) + [1, 1]
    assert layer(describe(scene(element(kind="group")), tmp_path))["label"] is None


def test_assets_flat_sorted_sanitized_and_deduplicated(tmp_path):
    data = png(tmp_path / "deep" / "texture.png")
    elements = [element("z /\\:한", texture="deep/texture.png"),
                element("a-good_2", texture="deep/texture.png"),
                element("a-good_2", texture="deep/texture.png")]
    assets = describe(scene(*elements), tmp_path)["assets"]
    assert [asset["name"] for asset in assets] == ["a-good_2.png", "z_____.png"]
    assert all("/" not in asset["name"] and "\\" not in asset["name"] for asset in assets)
    assert all(asset["sha256"] == hashlib.sha256(data).hexdigest() and asset["bytes"] == len(data) for asset in assets)


def test_conflicting_sanitized_asset_names_fail_instead_of_losing_an_asset(tmp_path):
    png(tmp_path / "first.png", 1, 1)
    png(tmp_path / "second.png", 2, 2)
    value = scene(element("a/b", texture="first.png"), element("a\\b", texture="second.png"))
    with pytest.raises(ValueError, match="conflicting assets named a_b.png"):
        describe(value, tmp_path)


def test_zero_x_scale_with_nonzero_shear_reports_unrepresentable_transform(tmp_path):
    value = scene(element(kind="group", tracks={"sx": track((0, 0)), "skx": track((0, 45))}))
    with pytest.raises(ValueError, match="x scale of e1 is zero; skew cannot be represented"):
        describe(value, tmp_path)


@pytest.mark.parametrize("source", ["texture", "model", "background"])
@pytest.mark.parametrize("escape", ["../outside.png", "/tmp/outside.png", "linked/outside.png"])
def test_every_asset_path_must_stay_inside_scene(tmp_path, source, escape):
    outside = tmp_path.parent / f"{tmp_path.name}-outside"
    outside.mkdir()
    (tmp_path / "linked").symlink_to(outside, target_is_directory=True)
    if source == "background":
        value = scene(background=Background(kind="image", value=escape))
    else:
        value = scene(element(kind="3d" if source == "model" else "sprite", **{source: escape}))
    with pytest.raises(ValueError, match="scene directory"):
        describe(value, tmp_path)


def test_png_size_reads_ihdr_without_imaging_dependencies(tmp_path):
    png(tmp_path / "one.png", 1, 1)
    assert png_size(tmp_path / "one.png") == (1, 1)
    png(tmp_path / "large.png", 257, 129)
    assert png_size(tmp_path / "large.png") == (257, 129)


def test_fix8_text_source_carries_canonical_box(tmp_path):
    el = element(kind="text", text="Title", font=FontGuess(), anchor=(0.25, 0.75))
    assert layer(describe(scene(el), tmp_path))["source"]["box"] == [10, 6]


@pytest.mark.parametrize("text", ["가을 여행", "\u1100\u1161", "\u3131\u314f"])
@pytest.mark.parametrize("weight,style,default_ps", [
    (400, "Regular", "MalgunGothic"), (599, "Regular", "MalgunGothic"),
    (600, "Bold", "MalgunGothicBold"), (700, "Bold", "MalgunGothicBold"),
])
def test_fix8_hangul_fallback_uses_device_malgun_style(tmp_path, text, weight, style, default_ps):
    fonts = [{"family": "Malgun Gothic", "style": "Regular", "postscript": "DeviceMalgun-Regular"},
             {"family": "Malgun Gothic", "style": "Bold", "postscript": "DeviceMalgun-Bold"}]
    el = element(kind="text", text=text, font=FontGuess(family_guess="Absent", weight=weight))
    result = layer(describe(scene(el), tmp_path, fonts=fonts))
    assert result["source"]["font"] == {"postscript": f"DeviceMalgun-{style}",
        "family": "Malgun Gothic", "style": style, "substituted": True}
    assert result["warnings"] == [f"font Absent not installed; using Malgun Gothic {style}"]
    for font in fonts:
        font["postscript"] = None
    assert layer(describe(scene(el), tmp_path, fonts=fonts))["source"]["font"]["postscript"] == default_ps
    assert layer(describe(scene(el), tmp_path, fonts=[]))["source"]["font"]["family"] == "Arial"
    el.canonical.text = "Autumn trip"
    assert layer(describe(scene(el), tmp_path, fonts=fonts))["source"]["font"]["family"] == "Arial"


def test_fix8_hangul_prefers_malgun_to_arial_candidate_but_preserves_installed_family(tmp_path):
    fonts = [{"family": "Arial", "style": "Bold", "postscript": "Arial-BoldMT"},
             {"family": "Malgun Gothic", "style": "Bold", "postscript": "MalgunGothicBold"}]
    el = element(kind="text", text="가을 여행", font=FontGuess(family_guess="Absent", weight=700, candidates=["Arial"]))
    assert layer(describe(scene(el), tmp_path, fonts=fonts))["source"]["font"]["family"] == "Malgun Gothic"
    fonts.append({"family": "Absent", "style": "Bold", "postscript": "Detected-Bold"})
    result = layer(describe(scene(el), tmp_path, fonts=fonts))
    assert result["source"]["font"]["postscript"] == "Detected-Bold" and result["warnings"] == []


def test_fix8_fontless_text_emits_image_and_hidden_editable_companion(tmp_path):
    png(tmp_path / "glyphs.png")
    el = element(kind="text", text="Reveal 한", texture="glyphs.png", anchor=(0.25, 0.75),
                 tracks={"sx": track((0, 2)), "sy": track((0, 3)), "x": track((0, 10), (30, 100)),
                         "reveal": track((0, 0), (30, 1)), "skx": track((0, 10))})
    spec = describe(scene(el), tmp_path, fonts=[])
    assert [item["id"] for item in spec["layers"]] == ["kf:background", "kf:e1", "kf:e1~text"]
    image, text = layer(spec), layer(spec, "e1~text")
    assert image["kind"] == "image" and not image.get("hidden", False)
    assert image["source"]["text"] == text["source"]["text"] == "Reveal 한"
    assert text["kind"] == "text" and text["hidden"] is True and text["anchor"] is None
    assert text["source"]["size_px"] == 4.8 and text["source"]["color"] == "#ff0000"
    assert text["source"]["box"] == [10, 6] and text["source"]["anchor_fraction"] == [0.25, 0.75]
    for field in ("in", "out", "effects", "label"):
        assert text[field] == image[field]
    for name in ("position_x", "position_y", "rotation", "opacity"):
        assert text["props"][name] == image["props"][name]
    # Text uses canonical pixels; footage scale keys also correct for texture dimensions.
    assert text["props"]["scale"] == [[0, [200, 300], None, None]]
    assert text["order"] == image["order"] + 1


@pytest.mark.parametrize("channels", [3, 4])
@pytest.mark.parametrize("filter_type", range(5))
def test_fix8_png_mean_reconstructs_filters_and_ignores_transparent_pixels(tmp_path, channels, filter_type):
    from keepframe.ae.spec import png_mean_color

    rows = [bytes([20, 40, 60, 255, 200, 100, 0, 128, 255, 255, 255, 127]),
            bytes([100, 80, 60, 255, 0, 0, 0, 0, 255, 255, 255, 1])]
    if channels == 3:
        rows = [bytes(v for i, v in enumerate(row) if i % 4 != 3) for row in rows]
    encoded = bytearray()
    previous = bytes(len(rows[0]))
    for row in rows:
        encoded.append(filter_type)
        for i, value in enumerate(row):
            left = row[i - channels] if i >= channels else 0
            up = previous[i]
            corner = previous[i - channels] if i >= channels else 0
            p = left + up - corner
            paeth = min((left, up, corner), key=lambda v: abs(p - v))
            predictor = (0, left, up, (left + up) // 2, paeth)[filter_type]
            encoded.append((value - predictor) % 256)
        previous = row

    def chunk(kind, data):
        return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data))

    path = tmp_path / "filtered.png"
    compressed = zlib.compress(encoded)
    path.write_bytes(b"\x89PNG\r\n\x1a\n"
                     + chunk(b"IHDR", struct.pack(">IIBBBBB", 3, 2, 8, 2 if channels == 3 else 6, 0, 0, 0))
                     + chunk(b"IDAT", compressed[:5]) + chunk(b"IDAT", compressed[5:]) + chunk(b"IEND", b""))
    assert png_mean_color(path) == ("#8a7a69" if channels == 3 else "#6b4928")


@pytest.mark.parametrize("form", ["palette", "16bit", "interlaced", "invalid_filter", "truncated", "transparent"])
def test_fix8_png_mean_unsupported_or_empty_is_white(tmp_path, form):
    from keepframe.ae.spec import png_mean_color

    def chunk(kind, data):
        return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data))

    path = tmp_path / "glyphs.png"
    header = struct.pack(">IIBBBBB", 1, 1, 16 if form == "16bit" else 8,
                         3 if form == "palette" else 6, 0, 0, int(form == "interlaced"))
    pixels = bytes([5 if form == "invalid_filter" else 0, 10, 20, 30, 0 if form == "transparent" else 255])
    data = b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", header) + chunk(b"IDAT", zlib.compress(pixels)) + chunk(b"IEND", b"")
    path.write_bytes(data[:-15] if form == "truncated" else data)
    assert png_mean_color(path) == "#ffffff"


@pytest.mark.parametrize("data", [b"JPEG bytes", b"\x89PNG\r\n\x1a\n", b"\x89PNG\r\n\x1a\n" + b"\0" * 25])
def test_non_png_or_invalid_header_reports_filename(tmp_path, data):
    path = tmp_path / "bad.jpg"
    path.write_bytes(data)
    with pytest.raises(ValueError, match="bad.jpg"):
        png_size(path)
    with pytest.raises(ValueError, match="bad.jpg"):
        describe(scene(element(texture="bad.jpg")), tmp_path)


def test_json_is_byte_deterministic_compact_unicode_and_rounded(tmp_path):
    png(tmp_path / "texture.png", 3, 7)
    el = element(texture="texture.png", anchor=(0.1234567, 0.7654321), tracks={
        "x": track((0, 0.123456, (1 / 3, 0.2, 2 / 3, 0.7)), (30, 1.234567)),
        "sx": track((0, 0.1234567)), "sy": track((0, 0.2345678))})
    el.label = "한글"
    value = scene(el)
    value.fps = 29.123456
    before = value.model_dump()
    first = comp_spec_json(value, tmp_path, **CONTEXT)
    second = comp_spec_json(value.model_copy(deep=True), tmp_path, **CONTEXT)
    spec = describe(value, tmp_path)
    assert first.encode() == second.encode()
    assert first == json.dumps(spec, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    assert "한글" in first and "\\u" not in first and "\n" not in first
    assert spec["comp"]["fps"] == 29.1235
    assert layer(spec)["source"]["scale_fix"] == [3.3333, 0.8571]
    assert layer(spec)["anchor"] == [0.3704, 5.358]
    assert layer(spec)["props"]["scale"][0][1] == [41.1522, 20.1058]
    assert layer(spec)["props"]["position_x"][0][1] == 0.1235
    assert value.model_dump() == before

    def check_rounding(item):
        if isinstance(item, float):
            assert item == round(item, 4)
        elif isinstance(item, dict):
            for child in item.values():
                check_rounding(child)
        elif isinstance(item, list):
            for child in item:
                check_rounding(child)

    check_rounding(spec)


@pytest.mark.parametrize("format,extension", [("JPEG", "jpg"), ("WEBP", "webp")])
def test_final_texture_edit_with_real_bytes_under_png_name(tmp_path, format, extension):
    from io import BytesIO
    from PIL import Image
    from keepframe.edit.apply import apply_edit
    from keepframe.edit.intent import Target

    image = BytesIO()
    Image.new("RGB", (40, 24), "red").save(image, format=format)
    edited = apply_edit(scene(element()), tmp_path,
                        [Target(element="e1", property="texture", value="attachment")], {}, image.getvalue())
    texture = tmp_path / edited.element("e1").canonical.texture
    assert texture.suffix == ".png"
    assert texture.read_bytes() == image.getvalue()
    spec = describe(edited, tmp_path)
    name = f"e1.{extension}"
    assert spec["assets"] == [{"name": name, "sha256": hashlib.sha256(image.getvalue()).hexdigest(),
                               "bytes": len(image.getvalue())}]
    assert layer(spec)["source"] == {"asset": name, "scale_fix": [0.25, 0.25]}
    assert layer(spec)["anchor"] == [20, 12]
    assert spec_asset_paths(edited, tmp_path) == {name: texture}


# --- Task 14: native AE export of gradients, video and styled text -------------------------------------------------

GOLDEN = Path(__file__).parent / "golden" / "ae_spec_legacy.json"
GLB = (struct.pack("<III", 0x46546C67, 2, 48) + struct.pack("<II", 28, 0x4E4F534A)
       + b'{"asset":{"version":"2.0"}}' + b" ")


def legacy_scene(root):
    """A scene with none of Task 14's features: image plate, sprite, plain text, fontless text, group, model."""
    png(root / "assets" / "plate.png", 32, 18)
    png(root / "assets" / "sprite.png", 20, 12)
    png(root / "assets" / "glyphs.png", 30, 10)
    (root / "assets" / "model.glb").write_bytes(GLB)
    return scene(
        element("sprite", texture="assets/sprite.png", anchor=(0.25, 0.75), tracks={
            "x": track((3, 10, EASE), (33, 110)), "sx": track((0, 1, EASE), (30, 3)), "sy": track((0, 2), (30, 4)),
            "opacity": track((0, 0.2, EASE), (30, 0.8)), "skx": track((0, 10), (30, 20)),
            "reveal": track((0, 0, EASE), (30, 1))}),
        element("title", kind="text", text="Hello 한", color="#f80", tracks={"skx": track((0, 8))},
                font=FontGuess(family_guess="Inter", weight=700, size_px=24, postscript="Inter-Bold")),
        element("glyphs", kind="text", text="Glyphs", texture="assets/glyphs.png"),
        element("box", kind="group"),
        element("model", kind="3d", model="assets/model.glb", tracks={"rx": track((0, 10), (30, 90))}),
        background=Background(kind="image", value="assets/plate.png"),
        groups=[Group(id="g", members=["sprite", "title"])])


def test_spec_unchanged_for_legacy_scene(tmp_path):
    fonts = [{"family": "Inter", "style": "Bold", "postscript": "Inter-Bold"}]
    text = comp_spec_json(legacy_scene(tmp_path), tmp_path, **CONTEXT, fonts=fonts)
    if os.environ.get("KEEPFRAME_WRITE_GOLDEN"):
        GOLDEN.write_text(text + "\n", encoding="utf-8")
    assert (text + "\n").encode() == GOLDEN.read_bytes()


def _gradient(kind="linear", *stops, **geometry):
    from keepframe.ir.schema import Gradient, GradientStop
    return Gradient(kind=kind, stops=[GradientStop(offset=o, color=c) for o, c in stops], **geometry)


def _keyed(keys):
    """The value of single-key (static) AE keys."""
    assert len(keys) == 1 and keys[0][2:] == [None, None]
    return keys[0][1]


def test_gradient_background_ramp_points_from_css_angle(tmp_path):
    from keepframe.ir.gradient import gradient_t
    from keepframe.ir.schema import GradientKey

    def hd(bg):
        return Scene(id="s", size=(1920, 1080), fps=30, frames=60, background=bg, elements=[])

    g = _gradient("linear", (0, "#ff0000"), (1, "#0000ff"), angle=135)
    spec = describe(hd(Background(kind="gradient", value="#808080", gradient=g)), tmp_path)
    bg = layer(spec, "background")
    assert (bg["kind"], bg["source"], bg["anchor"]) == ("solid", {"color": "#808080"}, [0, 0])
    ramp = bg["effects"]["gradient"]
    assert ramp["shape"] == 1
    assert _keyed(ramp["start"]) == pytest.approx([210, -210], abs=0.5)
    assert _keyed(ramp["end"]) == pytest.approx([1710, 1290], abs=0.5)
    assert (_keyed(ramp["start_color"]), _keyed(ramp["end_color"])) == ([1, 0, 0, 1], [0, 0, 1, 1])
    assert spec["assets"] == bg["warnings"] == spec["warnings"] == []
    assert (bg["effects"]["skew"], bg["effects"]["reveal"]) == (None, None)

    # Stop offsets move the ramp ends: the Ramp's t is CSS's t between the two stops, at any pixel.
    g = _gradient("linear", (0.2, "#102030"), (0.7, "#f0e0d0"), angle=200)
    ramp = layer(describe(hd(Background(kind="gradient", value="#808080", gradient=g)), tmp_path), "background")["effects"]["gradient"]
    start, end = (_keyed(ramp[k]) for k in ("start", "end"))
    xs, ys = [0.5, 333.5, 1919.5, 960.5], [0.5, 1079.5, 77.5, 540.5]
    css = (gradient_t(g, 1920, 1080, (np.array(xs), np.array(ys))) - 0.2) / 0.5
    d = [end[0] - start[0], end[1] - start[1]]
    for x, y, t in zip(xs, ys, css):
        assert ((x - start[0]) * d[0] + (y - start[1]) * d[1]) / (d[0] ** 2 + d[1] ** 2) == pytest.approx(t, abs=1e-3)

    # Radial: the start is the centre, the end lies one radius away; animated keys keep their frames.
    a = _gradient("radial", (0, "#000000"), (1, "#ffffff"), center=(0.25, 0.5), radius=1.0)
    b = _gradient("radial", (0, "#ff0000"), (1, "#ffffff"), center=(0.75, 0.5), radius=0.5)
    bg = layer(describe(hd(Background(kind="gradient", value="#808080", gradient_keys=[
        GradientKey(t=0, gradient=a), GradientKey(t=30, gradient=b)])), tmp_path), "background")
    ramp, r = bg["effects"]["gradient"], math.hypot(1920, 1080) / 2
    assert ramp["shape"] == 2
    assert [k[0] for k in ramp["start"]] == [k[0] for k in ramp["end_color"]] == [0, 30]
    assert [k[1] for k in ramp["start"]] == [[480, 540], [1440, 540]]
    assert [v for k in ramp["end"] for v in k[1]] == pytest.approx([480 + r, 540, 1440 + r / 2, 540], abs=1e-3)
    assert [k[1] for k in ramp["start_color"]] == [[0, 0, 0, 1], [1, 0, 0, 1]]
    assert bg["warnings"] == []


def test_multistop_gradient_exports_poster_with_warning(tmp_path):
    from keepframe.ir.schema import GradientKey
    data = png(tmp_path / "assets" / "poster.png", 32, 18)
    three = _gradient("linear", (0, "#ff0000"), (0.5, "#00ff00"), (1, "#0000ff"))
    two = _gradient("linear", (0, "#ff0000"), (1, "#0000ff"))
    for bg in (Background(kind="gradient", value="#808080", gradient=three, poster="assets/poster.png"),
               Background(kind="gradient", value="#808080", poster="assets/poster.png", gradient_keys=[
                   GradientKey(t=0, gradient=two), GradientKey(t=9, gradient=three)])):
        spec = describe(scene(background=bg), tmp_path)
        result = layer(spec, "background")
        assert (result["kind"], result["source"]) == ("image", {"asset": "background.png", "scale_fix": [10, 10]})
        assert "gradient" not in result["effects"]
        assert result["warnings"] == spec["warnings"] == ["gradient background has more than 2 stops; exported as its poster image"]
        assert spec["assets"] == [{"name": "background.png", "sha256": hashlib.sha256(data).hexdigest(), "bytes": len(data)}]
        assert spec_asset_paths(scene(background=bg), tmp_path) == {"background.png": tmp_path / "assets" / "poster.png"}
    spec = describe(scene(background=Background(kind="gradient", value="#808080", gradient=three)), tmp_path)
    assert (layer(spec, "background")["kind"], layer(spec, "background")["source"]) == ("solid", {"color": "#808080"})
    assert spec["warnings"] == ["gradient background has more than 2 stops; exported as a flat colour"]


def _video_scene(root, fail=None):
    from keepframe.analyze.videoasset import encode_webm
    assets = root / "assets"
    encode_webm([np.full((48, 64, 3), (40 * i, 90, 200 - 30 * i), np.uint8) for i in range(6)], 30,
                assets / "background.webm", alpha=False)
    sprite = []
    for i in range(4):
        frame = np.zeros((12, 20, 4), np.uint8)
        frame[:, :10] = (200, 40 * i, 60, 255)
        sprite.append(frame)
    encode_webm(sprite, 30, assets / "e1.video.webm", alpha=True)
    png(assets / "background.png", 64, 48)
    png(assets / "e1.png", 20, 12)
    el = Element(id="e1", kind="sprite", visible=(2, 5), canonical=Canonical(
        width=10, height=6, anchor=(0.25, 0.75), texture="assets/e1.png", video="assets/e1.video.webm"))
    return Scene(id="s", size=(64, 48), fps=30, frames=6, elements=[el], background=Background(
        kind="video", value="assets/background.webm", poster="assets/background.png"))


def _probe(path):
    out = subprocess.run(["ffprobe", "-v", "error", "-select_streams", "v:0", "-count_frames", "-show_entries",
                          "stream=codec_name,pix_fmt,width,height,nb_read_frames", "-of", "json", str(path)],
                         capture_output=True, text=True, check=True).stdout
    return json.loads(out)["streams"][0]


def test_video_layers_use_derived_footage_and_start_time(tmp_path, monkeypatch):
    from keepframe.ae import footage
    value = _video_scene(tmp_path)
    spec = describe(value, tmp_path)
    bg, sprite = layer(spec, "background"), layer(spec, "e1")
    assert (bg["kind"], bg["source"], bg["anchor"]) == (
        "footage", {"asset": "background.mp4", "scale_fix": [1, 1], "start_time": 0}, [0, 0])
    assert (bg["in"], bg["out"], bg["warnings"]) == (0, 5, [])
    assert (sprite["kind"], sprite["source"], sprite["anchor"]) == (
        "footage", {"asset": "e1.mov", "scale_fix": [0.5, 0.5], "start_time": 0.0667}, [5, 9])
    assert (sprite["in"], sprite["out"], sprite["warnings"], spec["warnings"]) == (2, 5, [], [])
    paths = spec_asset_paths(value, tmp_path)
    plate_sha = hashlib.sha256((tmp_path / "assets" / "background.webm").read_bytes()).hexdigest()
    sprite_sha = hashlib.sha256((tmp_path / "assets" / "e1.video.webm").read_bytes()).hexdigest()
    assert paths == {"background.mp4": tmp_path / "assets" / f"background.{plate_sha[:16]}.ae.mp4",
                     "e1.mov": tmp_path / "assets" / f"e1.video.{sprite_sha[:16]}.ae.mov"}
    assert spec["assets"] == [{"name": name, "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
                               "bytes": path.stat().st_size} for name, path in sorted(paths.items())]
    plate, clip = _probe(paths["background.mp4"]), _probe(paths["e1.mov"])
    assert (plate["codec_name"], plate["pix_fmt"], plate["width"], plate["height"], plate["nb_read_frames"]) == (
        "h264", "yuv420p", 64, 48, "6")
    assert (clip["codec_name"], clip["width"], clip["height"], clip["nb_read_frames"]) == ("prores", 20, 12, "4")
    assert clip["pix_fmt"].startswith("yuva444p")
    # Cached by the source's SHA: a second export never runs ffmpeg.
    monkeypatch.setattr(footage.subprocess, "run", lambda *a, **k: pytest.fail("derived again"))
    assert describe(value, tmp_path) == spec


def test_video_footage_failure_exports_posters_with_codes(tmp_path, monkeypatch):
    from keepframe.ae import footage
    value = _video_scene(tmp_path)

    def fail(*args, **kwargs):
        raise footage.FootageError("footage_failed")

    monkeypatch.setattr(footage, "derive", fail)
    spec = describe(value, tmp_path)
    bg, sprite = layer(spec, "background"), layer(spec, "e1")
    assert (bg["kind"], bg["source"]["asset"], sprite["kind"], sprite["source"]["asset"]) == (
        "image", "background.png", "image", "e1.png")
    assert spec["warnings"] == ["video background exported as its poster image (footage_failed)"]
    assert sprite["warnings"] == ["e1 video sprite exported as its poster image (footage_failed)"]
    assert set(spec_asset_paths(value, tmp_path)) == {"background.png", "e1.png"}


def _styled(**style):
    from keepframe.ir.schema import TextStyle
    return element("t", kind="text", text="Sale", color="#ffffff", style=TextStyle(**style),
                   font=FontGuess(family_guess="Inter", weight=700, size_px=40, postscript="Inter-Bold"))


def test_text_stroke_tracking_shadow_glow_fill_spec(tmp_path):
    from keepframe.ir.schema import AlphaStop, Fade, TextEffect
    fill = _gradient("linear", (0, "#ff3d00"), (1, "#ffd600"), angle=180)
    el = _styled(tracking_em=0.12, shear_deg=10, fill=fill, fade=Fade(stops=[AlphaStop(offset=0, alpha=1), AlphaStop(offset=1, alpha=0.2)]),
                 effects=[TextEffect(kind="shadow", color="#1a0a3a", opacity=0.7, dx=4, dy=5, blur=4),
                          TextEffect(kind="stroke", color="#112233", width=1.5),
                          TextEffect(kind="glow", color="#ffd27a", opacity=0.9, blur=3)])
    result = layer(describe(scene(el), tmp_path), "t")
    assert result["kind"] == "text"
    source = result["source"]
    assert source["tracking"] == 120
    assert source["stroke"] == {"color": "#112233", "width": 3, "over_fill": False}
    assert source["font"]["postscript"] is None and source["color"] == "#ffffff"
    shadow, glow = result["effects"]["shadows"]
    assert shadow == {"color": pytest.approx([26 / 255, 10 / 255, 58 / 255, 1], abs=1e-4), "opacity": 70,
                      "direction": pytest.approx(math.degrees(math.atan2(4, -5)), abs=1e-4),
                      "distance": pytest.approx(math.hypot(4, 5), abs=1e-4), "softness": 4}
    assert glow == {"color": pytest.approx([1, 210 / 255, 122 / 255, 1], abs=1e-4), "opacity": 90,
                    "direction": 0, "distance": 0, "softness": 3}
    ramp = result["effects"]["fill"]
    assert ramp["shape"] == 1
    assert (_keyed(ramp["start"]), _keyed(ramp["end"])) == ([5, 0], [5, 6])   # box coordinates, 180deg = down
    assert (_keyed(ramp["start_color"]), _keyed(ramp["end_color"])) == (
        pytest.approx([1, 61 / 255, 0, 1], abs=1e-4), pytest.approx([1, 214 / 255, 0, 1], abs=1e-4))
    # Shear folds into the skew effect (CSS skewX(-shear) about the first baseline, the text layer's y = 0).
    skew = result["effects"]["skew"]
    assert _keyed(skew["skew"]) == pytest.approx(-10, abs=1e-4)
    assert skew["baseline_shear"] == pytest.approx(-math.tan(math.radians(10)), abs=1e-4)
    assert result["warnings"] == ["text t stroke takes the gradient fill in AE", "text fade not exported"]

    # With an element skew and non-uniform scale: tan(skew) = tan(skx)·sy/sx − tan(shear).
    el = _styled(shear_deg=-8)
    el.tracks = {"skx": track((0, 12)), "sx": track((0, 2)), "sy": track((0, 3))}
    result = layer(describe(scene(el), tmp_path), "t")
    k = math.tan(math.radians(12)) * 3 / 2 + math.tan(math.radians(8))
    assert _keyed(result["effects"]["skew"]["skew"]) == pytest.approx(math.degrees(math.atan(k)), abs=1e-4)
    assert result["source"]["stroke"] is None and result["source"]["tracking"] == 0
    assert "shadows" not in result["effects"] and "fill" not in result["effects"]
    assert result["warnings"] == []

    # Unstyled text keeps today's source and effects exactly.
    plain = layer(describe(scene(element("t", kind="text", text="Sale", font=FontGuess())), tmp_path), "t")
    assert set(plain["source"]) == {"text", "font", "size_px", "color", "anchor_fraction", "box"}
    assert plain["effects"] == {"skew": None, "reveal": None}


def test_texture_pad_places_the_box_not_the_padding(tmp_path):
    """A texture padded past its box (R41) maps its box, not the whole PNG, onto the canonical size (as numpy)."""
    png(tmp_path / "t.png", 30, 16)
    el = element("p", texture="t.png", anchor=(0.25, 0.5), texture_pad=5.0)
    result = layer(describe(scene(el), tmp_path), "p")
    assert result["source"] == {"asset": "p.png", "scale_fix": [0.5, 1]}
    assert result["anchor"] == [10, 8]
