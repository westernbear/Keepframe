import hashlib
import json
import struct
import zlib

import pytest

from keepframe.ae.spec import comp_spec, comp_spec_json, ease_to_ae, png_size
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


@pytest.mark.parametrize("kind", ["sprite", "ui", "group"])
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
                                "size_px": 20.1235, "color": "#000000", "anchor_fraction": [0.25, 0.75]}
    assert "rotation_x" not in result["props"] and "rotation_y" not in result["props"]
    assert spec["assets"] == [] and result["warnings"] == []


def test_text_color_normalized(tmp_path):
    el = element(kind="text", text="A", font=FontGuess(), color="#A0b1C2")
    assert layer(describe(scene(el), tmp_path))["source"]["color"] == "#a0b1c2"


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


def test_font_missing_falls_back_to_arial(tmp_path):
    el = element(kind="text", text="A", font=FontGuess(family_guess="Absent"))
    result = layer(describe(scene(el), tmp_path, fonts=[]))
    assert result["source"]["font"] == {"postscript": "ArialMT", "family": "Arial", "style": "Regular",
                                         "substituted": True}
    assert result["warnings"] == ["font Absent not installed; using Arial"]


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
                  element("text", kind="text", text="A", texture="unused.png"),
                  background=Background(kind="image", value="background.png"))
    paths = spec_asset_paths(value, tmp_path)
    assert paths == {"a_b.png": tmp_path / "deep" / "texture.png", "model.glb": tmp_path / "model.glb",
                     "background.png": tmp_path / "background.png"}
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
