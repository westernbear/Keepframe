from __future__ import annotations


import pytest

from keepframe.after_effects.mapping import (
    AEMappingError,
    CompositionSettings,
    map_baseline,
    validate_mapping_domains,
)
from keepframe.after_effects.mcp_server import _canonical_apply_payload
from keepframe.after_effects.models import (
    AEEffect,
    AECapabilities,
    AECapabilityCatalog,
    AESubstitution,
)
from keepframe.after_effects.operations import ApprovedCapabilities
from keepframe.ir.schema import (
    Background,
    Canonical,
    Element,
    FontGuess,
    Group,
    Keyframe,
    Scene,
    Track,
)
from keepframe.render.plan import PlanAsset


CAP_PROPERTIES = {
    "ADBE Position X": "number",
    "ADBE Position Y": "number",
    "ADBE Scale X": "number",
    "ADBE Scale Y": "number",
    "ADBE Scale": "vec2",
    "ADBE Rotate Z": "number",
    "ADBE Skew": "number",
    "ADBE Skew Axis": "number",
    "ADBE Opacity": "number",
    "ADBE Anchor Point": "vec2",
    "ADBE Gaussian Blur 2-0001": "number",
}


def _capabilities(*, fonts=("Arial",), effects=("ADBE Gaussian Blur 2",)):
    effect_records = tuple(
        AEEffect(
            match_name=name,
            properties={"ADBE Gaussian Blur 2-0001": "number"},
        )
        for name in effects
    )
    catalog = AECapabilityCatalog(
        font_names=fonts,
        fonts=tuple(
            {
                "match_name": name,
                "version_or_hash": "installed",
            }
            for name in fonts
        ),
        effect_names=effects,
        effects=effect_records,
        property_schemas=CAP_PROPERTIES,
    )
    return AECapabilities(
        version="24.0",
        major=24,
        host="after-effects",
        ready=True,
        project_open=True,
        capabilities=catalog,
    )


def _asset(path: str, suffix: str = "a") -> PlanAsset:
    digest = (suffix * 64)[:64]
    return PlanAsset(
        id=digest,
        project_path=path,
        sha256=digest,
        length=1,
        media_kind="image/png",
    )


def _scene(*elements, background=None, groups=(), frames=20, fps=10.0):
    return Scene(
        id="scene-1",
        size=(100, 50),
        fps=fps,
        frames=frames,
        background=background or Background(kind="color", value="#112233"),
        elements=list(elements),
        groups=list(groups),
    )


def _sprite(eid="sprite-1", *, texture="img.png", visible=(0, 19), z=0, tracks=None):
    return Element(
        id=eid,
        kind="sprite",
        canonical=Canonical(width=20, height=10, texture=texture),
        visible=visible,
        z=Track(keys=[Keyframe(t=0, v=z)]),
        tracks=tracks or {},
    )


def _ops(result):
    return [operation for batch in result.batches for operation in batch.operations]


def test_maps_exact_comp_assets_textures_groups_and_visibility():
    text = Element(
        id="title",
        kind="text",
        canonical=Canonical(
            width=30,
            height=12,
            text="Hello",
            font=FontGuess(family_guess="Arial", size_px=18),
            color="#abcdef",
        ),
        visible=(2, 19),
        z=Track(keys=[Keyframe(t=0, v=2)]),
    )
    sprite = _sprite("sprite-1", visible=(4, 9), z=1)
    scene = _scene(text, sprite, groups=(Group(id="controls", members=["sprite-1"]),))
    image = _asset("scenes/demo/img.png")
    result = map_baseline(scene, (image,), "scenes/demo", _capabilities())

    assert result.composition.width == 100
    assert result.composition.height == 50
    assert result.composition.frame_rate == 10.0
    assert result.composition.duration == pytest.approx(2.0)
    assert result.composition.background_color == "#112233"
    assert result.imported_asset_ids == (image.id,)
    assert all(
        getattr(op, "asset_id", None) is None or "/" not in op.asset_id
        for op in _ops(result)
    )

    adds = [op for op in _ops(result) if op.kind == "add_layer"]
    assert [op.layer_type for op in adds] == ["null", "footage", "text"]
    assert adds[1].parent_instance_id == adds[0].layer_instance_id
    assert adds[2].parent_instance_id is None
    assert adds[1].source_element_id == "sprite-1"
    assert adds[2].source_element_id == "title"
    group_ops = [op for op in _ops(result) if op.layer_instance_id == adds[0].layer_instance_id]
    assert {(op.kind, getattr(op, "property_name", None)) for op in group_ops} >= {
        ("set_transform", "anchor"),
        ("set_transform", "position_x"),
        ("set_transform", "position_y"),
        ("set_transform", "scale"),
        ("set_transform", "rotation"),
        ("set_transform", "skew_x"),
    }
    visibility = {op.layer_instance_id: op for op in _ops(result) if op.kind == "set_visibility"}
    assert visibility[adds[2].layer_instance_id].frame_end == 20
    assert visibility[adds[1].layer_instance_id].frame_start == 4
    assert visibility[adds[1].layer_instance_id].frame_end == 10


def test_image_background_is_black_comp_with_pinned_asset_layer():
    scene = _scene(
        _sprite(texture="bg.png"),
        background=Background(kind="image", value="bg.png"),
    )
    background = _asset("scenes/demo/bg.png")
    result = map_baseline(scene, [background], "scenes/demo", _capabilities())

    assert result.composition.background_color == "#000000"
    adds = [op for op in _ops(result) if op.kind == "add_layer"]
    assert adds[0].layer_type == "footage"
    assert adds[0].asset_id == background.id
    assert adds[0].source_element_id == scene.id
    assert result.imported_asset_ids == (background.id,)


def test_static_z_order_is_ascending_with_scene_order_ties():
    low = _sprite("low", z=-1)
    tie_a = _sprite("tie-a", z=2)
    tie_b = _sprite("tie-b", z=2)
    high = _sprite("high", z=3)
    result = map_baseline(
        _scene(low, tie_a, tie_b, high),
        [_asset("scenes/demo/img.png")],
        "scenes/demo",
        _capabilities(),
    )
    source_order = [
        op.source_element_id
        for op in _ops(result)
        if op.kind == "add_layer" and op.source_element_id in {"low", "tie-a", "tie-b", "high"}
    ]
    assert source_order == ["low", "tie-a", "tie-b", "high"]


def test_dynamic_z_segments_cross_static_sibling_by_creation_order():
    moving = _sprite(
        "moving",
        z=3,
    ).model_copy(
        update={
            "z": Track(
                keys=[
                    Keyframe(t=0, v=3),
                    Keyframe(t=10, v=-1),
                ]
            )
        }
    )
    result = map_baseline(
        _scene(moving, _sprite("static", z=1)),
        [_asset("scenes/demo/img.png")],
        "scenes/demo",
        _capabilities(),
    )
    adds = [
        op
        for op in _ops(result)
        if op.kind == "add_layer" and op.source_element_id in {"moving", "static"}
    ]
    assert [
        (op.source_element_id, op.layer_instance_id)
        for op in adds
    ] == [
        ("moving", "mapped:moving:segment:1:substitution:0"),
        ("static", "mapped:static:segment:0:substitution:0"),
        ("moving", "mapped:moving:segment:0:substitution:0"),
    ]


def test_dynamic_z_segments_are_nonoverlapping_and_source_aggregated():
    moving = _sprite(
        "moving",
        z=0,
        tracks={},
    ).model_copy(
        update={
            "z": Track(
                keys=[
                    Keyframe(t=0, v=0),
                    Keyframe(t=5, v=3),
                    Keyframe(t=10, v=-1),
                ]
            )
        }
    )
    result = map_baseline(
        _scene(moving, _sprite("other", z=1)),
        [_asset("scenes/demo/img.png")],
        "scenes/demo",
        _capabilities(),
    )
    records = [item for item in result.final_inventory if item.source_element_id == "moving"]
    assert sorted((item.frame_start, item.frame_end) for item in records) == [(0, 5), (5, 10), (10, 20)]
    assert len({item.instance_id for item in records}) == 3
    assert all(item.frame_start < item.frame_end for item in records)


def test_mapped_batch_payload_crosses_mcp_canonical_boundary():
    capabilities = _capabilities()
    scene = _scene(_sprite())
    result = map_baseline(scene, [_asset("scenes/demo/img.png")], "scenes/demo", capabilities)
    batch = result.batches[0]
    approved = ApprovedCapabilities(
        digest=capabilities.digest,
        fonts=capabilities.capabilities.font_names,
        effects=capabilities.capabilities.effect_names,
        properties=capabilities.capabilities.property_schemas,
    )

    canonical = _canonical_apply_payload(
        {
            "batch": batch.to_payload(),
            "approved_capabilities": approved.model_dump(mode="json"),
            "scene_frame_count": scene.frames,
            "duration": result.composition.duration,
            "layer_count": batch.layer_count,
            "baseline": True,
            "locked_source_ids": [],
            "layer_sources": batch.start_layer_sources,
            "project_id": "project",
            "plan_id": "plan",
            "session_id": "session",
        }
    )

    assert canonical["batch"] == batch.to_payload()
    assert canonical["layer_sources"] == batch.start_layer_sources


def test_text_substitution_uses_exact_approved_font_name():
    element = Element(
        id="three-d",
        kind="3d",
        canonical=Canonical(width=20, height=10),
        visible=(0, 19),
        z=Track(keys=[Keyframe(t=0, v=0)]),
    )
    substitution = AESubstitution(
        source_element_id="three-d",
        source_type="3d",
        proposed_layers=(
            {
                "layer_type": "text",
                "name": "replacement",
                "font_name": "Arial",
            },
        ),
        lost_semantics=("3d",),
        acknowledged=True,
    )
    result = map_baseline(_scene(element), (), "scenes/demo", _capabilities(), (substitution,))
    text = next(op for op in _ops(result) if op.kind == "set_text")
    assert text.font_name == "Arial"

    with pytest.raises(AEMappingError, match="font_name"):
        map_baseline(
            _scene(element),
            (),
            "scenes/demo",
            _capabilities(),
            (
                substitution.model_copy(
                    update={
                        "proposed_layers": (
                            {
                                "layer_type": "text",
                                "name": "replacement",
                                "font_name": "Missing",
                            },
                        )
                    }
                ),
            ),
        )


def test_native_text_uses_exact_capability_font_match_name():
    base = _capabilities(fonts=("ArialMT",))
    catalog = AECapabilityCatalog(
        font_names=("ArialMT",),
        fonts=(
            {
                "match_name": "ArialMT",
                "family": "Arial",
                "style": "Regular",
                "version_or_hash": "installed",
            },
        ),
        effect_names=base.capabilities.effect_names,
        effects=base.capabilities.effects,
        property_schemas=base.capabilities.property_schemas,
    )
    capabilities = AECapabilities(
        version=base.version,
        major=base.major,
        host=base.host,
        ready=base.ready,
        project_open=base.project_open,
        capabilities=catalog,
    )
    text = Element(
        id="title",
        kind="text",
        canonical=Canonical(
            width=30,
            height=12,
            text="Hello",
            font=FontGuess(family_guess="Arial", size_px=18),
        ),
        visible=(0, 19),
        z=Track(keys=[Keyframe(t=0, v=0)]),
    )
    result = map_baseline(_scene(text), (), "scenes/demo", capabilities)
    operation = next(op for op in _ops(result) if op.kind == "set_text")
    assert operation.font_name == "ArialMT"


def test_transforms_and_cubic_ease_map_to_typed_keyframes():
    tracks = {
        "x": Track(
            keys=[
                Keyframe(t=0, v=10, ease=(0.25, 0.1, 0.75, 0.9)),
                Keyframe(t=5, v=0),
            ]
        ),
        "sx": Track(
            keys=[
                Keyframe(t=0, v=1, ease=(0.25, 0.1, 0.75, 0.9)),
                Keyframe(t=5, v=2),
            ]
        ),
        "sy": Track(
            keys=[
                Keyframe(t=0, v=1, ease=(0.25, 0.1, 0.75, 0.9)),
                Keyframe(t=5, v=2),
            ]
        ),
        "rot": Track(keys=[Keyframe(t=0, v=4)]),
        "skx": Track(keys=[Keyframe(t=0, v=8)]),
        "opacity": Track(keys=[Keyframe(t=0, v=0.5)]),
    }
    element = _sprite("transform", tracks=tracks)
    result = map_baseline(_scene(element), [_asset("scenes/demo/img.png")], "scenes/demo", _capabilities())
    layer = next(op.layer_instance_id for op in _ops(result) if op.kind == "add_layer")
    keyframes = {
        op.property_name: op
        for op in _ops(result)
        if op.kind == "set_keyframes" and op.layer_instance_id == layer
    }
    assert keyframes["ADBE Position X"].keyframes[0].ease_out == pytest.approx([-8.0, 25.0])
    assert keyframes["ADBE Position X"].keyframes[1].ease_in == pytest.approx([-8.0, 25.0])
    assert keyframes["ADBE Scale"].keyframes[0].value == pytest.approx([100, 100])
    assert keyframes["ADBE Scale"].keyframes[0].ease_out[0][0] == pytest.approx(80.0)


def test_substitution_layers_effect_targets_and_acknowledgement():
    element = Element(
        id="three-d",
        kind="3d",
        canonical=Canonical(width=20, height=10),
        visible=(0, 19),
        z=Track(keys=[Keyframe(t=0, v=0)]),
    )
    substitution = AESubstitution(
        source_element_id="three-d",
        source_type="3d",
        proposed_layers=("solid", "null"),
        proposed_effects=(
            {
                "effect_name": "ADBE Gaussian Blur 2",
                "properties": {"ADBE Gaussian Blur 2-0001": 3},
                "layer_index": 1,
            },
        ),
        lost_semantics=("3d",),
        acknowledged=True,
    )
    result = map_baseline(_scene(element), (), "scenes/demo", _capabilities(), (substitution,))
    adds = [op for op in _ops(result) if op.kind == "add_layer"]
    assert [op.layer_type for op in adds] == ["solid", "null"]
    assert {op.source_element_id for op in adds} == {"three-d"}
    effect = next(op for op in _ops(result) if op.kind == "set_effect")
    assert effect.layer_instance_id == adds[1].layer_instance_id

    with pytest.raises(AEMappingError, match="acknowledged"):
        map_baseline(
            _scene(element),
            (),
            "scenes/demo",
            _capabilities(),
            (substitution.model_copy(update={"acknowledged": False}),),
        )


def test_batches_validate_progressive_inventory_and_limits():
    elements = tuple(_sprite(f"e-{index}") for index in range(24))
    result = map_baseline(
        _scene(*elements),
        [_asset("scenes/demo/img.png")],
        "scenes/demo",
        _capabilities(),
    )
    assert len(result.batches) > 1
    previous = {}
    for batch in result.batches:
        assert len(batch.operations) <= 128
        assert batch.layer_count == len(batch.layer_sources)
        assert batch.layer_sources == previous
        previous = dict(batch.layer_sources)
        for operation in batch.operations:
            if operation.kind == "add_layer":
                previous[operation.layer_instance_id] = operation.source_element_id
            elif operation.kind == "remove_layer":
                previous.pop(operation.layer_instance_id)
    assert set(previous) == {record.instance_id for record in result.final_inventory}


@pytest.mark.parametrize(
    "scene, assets, prefix, message",
    [
        (_scene(_sprite(texture="missing.png")), (), "scenes/demo", "asset"),
        (_scene(Element(id="bad", kind="3d", canonical=Canonical(width=1, height=1), visible=(0, 1))), (), "scenes/demo", "unsupported"),
        (
            _scene(_sprite("member"), groups=(Group(id="a", members=["member"]), Group(id="b", members=["member"]))),
            [_asset("scenes/demo/img.png")],
            "scenes/demo",
            "overlap",
        ),
    ],
)
def test_rejects_unmappable_inputs(scene, assets, prefix, message):
    with pytest.raises(AEMappingError, match=message):
        map_baseline(scene, assets, prefix, _capabilities())
def test_rejects_ambiguous_assets_and_unknown_group_members():
    scene = _scene(_sprite(), groups=(Group(id="group", members=["missing"]),))
    with pytest.raises(AEMappingError, match="unknown member"):
        map_baseline(scene, [_asset("scenes/demo/img.png")], "scenes/demo", _capabilities())

    ambiguous = _scene(_sprite())
    with pytest.raises(AEMappingError, match="ambiguous"):
        map_baseline(
            ambiguous,
            [
                _asset("scenes/demo/img.png", "a"),
                _asset("scenes/demo/img.png", "b"),
            ],
            "scenes/demo",
            _capabilities(),
        )


def test_rejects_degenerate_temporal_cubic_controls():
    element = _sprite(
        "bad-ease",
        tracks={
            "x": Track(
                keys=[
                    Keyframe(t=0, v=0, ease=(0.0, 0.0, 0.5, 1.0)),
                    Keyframe(t=5, v=1),
                ]
            )
        },
    )
    with pytest.raises(AEMappingError, match="temporal"):
        map_baseline(
            _scene(element),
            [_asset("scenes/demo/img.png")],
            "scenes/demo",
            _capabilities(),
        )


def test_exact_linear_cubic_is_emitted_without_temporal_ease():
    element = _sprite(
        "linear-ease",
        tracks={
            "x": Track(
                keys=[
                    Keyframe(t=0, v=0, ease=(0.0, 0.0, 1.0, 1.0)),
                    Keyframe(t=5, v=1),
                ]
            )
        },
    )
    result = map_baseline(
        _scene(element),
        [_asset("scenes/demo/img.png")],
        "scenes/demo",
        _capabilities(),
    )
    operation = next(
        op
        for op in _ops(result)
        if op.kind == "set_keyframes" and op.property_name == "ADBE Position X"
    )
    assert all(keyframe.ease_in is None and keyframe.ease_out is None for keyframe in operation.keyframes)

def test_rejects_more_than_one_thousand_layers_before_operations():
    elements = tuple(_sprite(f"e-{index}") for index in range(1001))
    with pytest.raises(AEMappingError, match="1000"):
        map_baseline(
            _scene(*elements),
            [_asset("scenes/demo/img.png")],
            "scenes/demo",
            _capabilities(),
        )


def test_same_content_addressed_asset_id_can_bind_multiple_project_paths():
    shared = _asset("scenes/demo/first.png")
    second = shared.model_copy(update={"project_path": "scenes/demo/second.png"})
    scene = _scene(
        _sprite("first", texture="first.png"),
        _sprite("second", texture="second.png"),
    )

    result = map_baseline(scene, [shared, second], "scenes/demo", _capabilities())

    assert result.imported_asset_ids == (shared.id,)


@pytest.mark.parametrize(
    "identity_update",
    [
        {"sha256": "b" * 64},
        {"length": 2},
        {"media_kind": "image/jpeg"},
    ],
)
def test_same_asset_id_with_conflicting_identity_is_rejected(identity_update):
    first = _asset("scenes/demo/first.png")
    second = first.model_copy(
        update={"project_path": "scenes/demo/second.png", **identity_update}
    )

    with pytest.raises(AEMappingError, match="identity"):
        map_baseline(
            _scene(
                _sprite("first", texture="first.png"),
                _sprite("second", texture="second.png"),
            ),
            [first, second],
            "scenes/demo",
            _capabilities(),
        )


def test_static_sources_are_not_segmented_by_dynamic_z_boundaries():
    moving = _sprite("moving", z=3).model_copy(
        update={
            "z": Track(
                keys=[
                    Keyframe(t=0, v=3),
                    Keyframe(t=10, v=-1),
                ]
            )
        }
    )
    static = tuple(_sprite(f"static-{index}", z=0) for index in range(998))

    result = map_baseline(
        _scene(*static, moving),
        [_asset("scenes/demo/img.png")],
        "scenes/demo",
        _capabilities(),
    )

    assert len(result.final_inventory) == 1000
    assert sum(item.source_element_id == "moving" for item in result.final_inventory) == 2
    assert sum(item.source_element_id != "moving" for item in result.final_inventory) == 998


@pytest.mark.parametrize(
    "field, value, message",
    [
        ("width", 3, "dimensions"),
        ("width", 30001, "dimensions"),
        ("frame_rate", 0.5, "fps"),
        ("frame_rate", 99.1, "fps"),
        ("duration", 10801.0, "duration"),
    ],
)
def test_composition_settings_enforces_after_effects_limits(field, value, message):
    with pytest.raises(ValueError, match=message):
        CompositionSettings(
            width=value if field == "width" else 100,
            height=50,
            frame_rate=value if field == "frame_rate" else 10.0,
            duration=value if field == "duration" else 2.0,
            background_color="#112233",
        )


@pytest.mark.parametrize(
    "update, message",
    [
        ({"size": (3, 50)}, "width"),
        ({"size": (30001, 50)}, "width"),
        ({"fps": 0.5}, "fps"),
        ({"fps": 99.1}, "fps"),
        ({"frames": 108001}, "duration"),
    ],
)
def test_scene_domain_limits_are_rejected_before_mapping(update, message):
    with pytest.raises(AEMappingError, match=message):
        map_baseline(
            _scene().model_copy(update=update),
            (),
            "scenes/demo",
            _capabilities(),
        )


def test_paired_explicit_linear_easing_emits_no_temporal_ease():
    linear = (0.0, 0.0, 1.0, 1.0)
    element = _sprite(
        "linear-scale",
        tracks={
            "sx": Track(
                keys=[
                    Keyframe(t=0, v=1, ease=linear),
                    Keyframe(t=5, v=2),
                ]
            ),
            "sy": Track(
                keys=[
                    Keyframe(t=0, v=1, ease=linear),
                    Keyframe(t=5, v=2),
                ]
            ),
        },
    )
    result = map_baseline(
        _scene(element),
        [_asset("scenes/demo/img.png")],
        "scenes/demo",
        _capabilities(),
    )
    operation = next(
        op
        for op in _ops(result)
        if op.kind == "set_keyframes" and op.property_name == "ADBE Scale"
    )

    assert all(
        keyframe.ease_in is None and keyframe.ease_out is None
        for keyframe in operation.keyframes
    )


def test_mapping_domain_preflight_rejects_opacity_outside_operation_domain():
    element = _sprite(
        "opaque-limit",
        tracks={"opacity": Track(keys=[Keyframe(t=0, v=2)])},
    )

    with pytest.raises(AEMappingError, match="opacity"):
        validate_mapping_domains(_scene(element), _capabilities())


def test_mapping_domain_preflight_rejects_scaled_operation_overflow():
    element = _sprite(
        "scale-limit",
        tracks={"sx": Track(keys=[Keyframe(t=0, v=10001)])},
    )

    with pytest.raises(AEMappingError, match="scale"):
        validate_mapping_domains(_scene(element), _capabilities())


def test_mapping_domain_preflight_rejects_anchor_operation_overflow():
    element = _sprite("anchor-limit").model_copy(
        update={
            "canonical": Canonical(
                width=20,
                height=10,
                anchor=(100000, 0),
                texture="img.png",
            )
        }
    )

    with pytest.raises(AEMappingError, match="anchor"):
        validate_mapping_domains(_scene(element), _capabilities())


def test_mapping_domain_preflight_rejects_font_size_below_operation_domain():
    element = Element(
        id="font-limit",
        kind="text",
        canonical=Canonical(
            width=20,
            height=10,
            text="small",
            font=FontGuess(family_guess="Arial", size_px=0.0000001),
        ),
        visible=(0, 19),
        z=Track(keys=[Keyframe(t=0, v=0)]),
    )

    with pytest.raises(AEMappingError, match="font size"):
        validate_mapping_domains(_scene(element), _capabilities())


def test_acknowledged_nonopaque_background_substitution_maps_opaque_layers():
    scene = _scene(background=Background(kind="color", value="#11223380"))
    substitution = AESubstitution(
        source_element_id=scene.id,
        source_type="background",
        proposed_layers=(
            {
                "layer_type": "solid",
                "name": "opaque replacement",
                "color": "#445566",
            },
        ),
        lost_semantics=("alpha",),
        acknowledged=True,
    )

    result = map_baseline(
        scene,
        (),
        "scenes/demo",
        _capabilities(),
        (substitution,),
    )

    assert result.composition.background_color == "#000000"
    add = next(op for op in _ops(result) if op.kind == "add_layer")
    assert add.source_element_id == scene.id
    assert add.layer_type == "solid"
    assert add.color == "#445566"


def test_nonopaque_background_substitution_rejects_implicit_solid_color():
    scene = _scene(background=Background(kind="color", value="#11223380"))
    substitution = AESubstitution(
        source_element_id=scene.id,
        source_type="background",
        proposed_layers=("solid",),
        lost_semantics=("alpha",),
        acknowledged=True,
    )

    with pytest.raises(AEMappingError, match="explicit opaque color"):
        map_baseline(scene, (), "scenes/demo", _capabilities(), (substitution,))


def test_acknowledged_invalid_easing_is_linearized_without_ease_fields():
    element = _sprite(
        "substituted",
        tracks={
            "x": Track(
                keys=[
                    Keyframe(t=0, v=0, ease=(0.0, 0.0, 0.5, 1.0)),
                    Keyframe(t=5, v=10),
                ]
            )
        },
    )
    substitution = AESubstitution(
        source_element_id="substituted",
        source_type="sprite",
        proposed_layers=("null",),
        lost_semantics=("easing",),
        acknowledged=True,
    )

    result = map_baseline(
        _scene(element),
        (),
        "scenes/demo",
        _capabilities(),
        (substitution,),
    )
    operation = next(
        op
        for op in _ops(result)
        if op.kind == "set_keyframes" and op.property_name == "ADBE Position X"
    )
    assert [keyframe.frame for keyframe in operation.keyframes] == [0, 5]
    assert [keyframe.value for keyframe in operation.keyframes] == [0, 10]
    assert all(keyframe.ease_in is None and keyframe.ease_out is None for keyframe in operation.keyframes)


def test_mapping_domain_preflight_rejects_generated_layer_overflow():
    elements = tuple(_sprite(f"e-{index}") for index in range(1001))
    with pytest.raises(AEMappingError, match="1000"):
        validate_mapping_domains(_scene(*elements), _capabilities())


@pytest.mark.parametrize("width", [3, 30001])
def test_mapping_domain_preflight_rejects_solid_dimensions(width):
    element = _sprite("solid-limit").model_copy(
        update={
            "canonical": Canonical(width=width, height=10, texture="img.png"),
        }
    )
    substitution = AESubstitution(
        source_element_id="solid-limit",
        source_type="sprite",
        proposed_layers=(
            {
                "layer_type": "solid",
                "name": "replacement",
                "color": "#010203",
            },
        ),
        acknowledged=True,
    )

    with pytest.raises(AEMappingError, match="solid"):
        validate_mapping_domains(_scene(element), _capabilities(), (substitution,))


def test_opaque_substitution_color_is_canonicalized_before_operations():
    element = Element(
        id="solid-color",
        kind="3d",
        canonical=Canonical(width=20, height=10),
        visible=(0, 19),
        z=Track(keys=[Keyframe(t=0, v=0)]),
    )
    substitution = AESubstitution(
        source_element_id="solid-color",
        source_type="3d",
        proposed_layers=(
            {
                "layer_type": "solid",
                "name": "replacement",
                "color": "#445566FF",
            },
        ),
        acknowledged=True,
    )

    result = map_baseline(
        _scene(element),
        (),
        "scenes/demo",
        _capabilities(),
        (substitution,),
    )
    add = next(op for op in _ops(result) if op.kind == "add_layer")
    assert add.color == "#445566"


def test_opaque_alpha_source_colors_are_canonicalized_to_rgb():
    text = Element(
        id="title",
        kind="text",
        canonical=Canonical(
            width=30,
            height=12,
            text="Hello",
            font=FontGuess(family_guess="Arial", size_px=18),
            color="#AABBCCFF",
        ),
        visible=(0, 19),
    )
    result = map_baseline(
        _scene(text, background=Background(kind="color", value="#112233FF")),
        (),
        "scenes/demo",
        _capabilities(),
    )

    assert result.composition.background_color == "#112233"
    set_text = next(op for op in _ops(result) if op.kind == "set_text")
    assert set_text.color == "#AABBCC"
