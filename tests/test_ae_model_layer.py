import json

import pytest

from keepframe.after_effects.compatibility import analyze_ae_compatibility, propose_ae_substitutions
from keepframe.after_effects.mapping import AEMappingError, map_baseline
from keepframe.after_effects.models import AECapabilities, AECapabilityCatalog
from keepframe.ir.schema import Background, Canonical, Element, Keyframe, Scene, Track
from keepframe.render.plan import PlanAsset
from tests.test_ae_reveal import capabilities as reveal_capabilities
from tests.test_ae_reveal import run_panel
from keepframe.after_effects.verification import AEInspection, AELayerInventory, AESourceSample, verify_inspection
from keepframe.ir.schema import Constraint
from keepframe.verify.matrix import extract_motions


def capabilities(*, model_layers=True):
    payload = reveal_capabilities().capabilities.model_dump()
    payload["model_layers"] = model_layers
    payload["property_schemas"].update({"ADBE Rotate X": "number", "ADBE Rotate Y": "number"})
    payload["properties"] = payload["property_schemas"]
    return AECapabilities(
        version="24.1", major=24, host="after-effects", ready=True, project_open=True,
        capabilities=AECapabilityCatalog.model_validate(payload),
    )


def model_scene():
    return Scene(
        id="scene", size=(200, 100), fps=10, frames=20, background=Background(),
        elements=[Element(
            id="model", kind="3d", visible=(0, 19),
            canonical=Canonical(width=20, height=10, model="object.glb", texture="preview.png"),
            tracks={
                "rx": Track(keys=[Keyframe(t=2, v=0, ease=(0.25, 0.125, 0.75, 0.875)), Keyframe(t=12, v=90)]),
                "ry": Track(keys=[Keyframe(t=0, v=-30), Keyframe(t=8, v=45)]),
            },
        )],
    )


def model_asset():
    return PlanAsset(id="b" * 64 + ".glb", project_path="object.glb", sha256="b" * 64, length=1, media_kind="model/gltf-binary")


def test_native_model_layer_imports_pinned_glb_and_maps_degree_rotations():
    scene = model_scene()
    caps = capabilities()
    assert analyze_ae_compatibility(scene, caps) == ()
    mapped = map_baseline(scene, [model_asset()], capabilities=caps)
    ops = [op for batch in mapped.batches for op in batch.operations]
    layer = next(op for op in ops if op.kind == "add_layer")
    assert layer.layer_type == "model"
    assert layer.asset_id == model_asset().id
    assert mapped.imported_asset_ids == (model_asset().id,)
    rotations = {op.property_name: op for op in ops if op.kind == "set_keyframes"}
    assert [key.value for key in rotations["ADBE Rotate X"].keyframes] == [0, 0, 90]
    assert [key.time for key in rotations["ADBE Rotate X"].keyframes] == [0, 0.2, 1.2]
    assert rotations["ADBE Rotate X"].keyframes[1].ease_out == [45.0, 25.0]
    assert [key.value for key in rotations["ADBE Rotate Y"].keyframes] == [-30, 45]
    assert [key.time for key in rotations["ADBE Rotate Y"].keyframes] == [0, 0.8]


def test_missing_model_capability_proposes_static_texture_and_requires_approval():
    scene = model_scene()
    caps = capabilities(model_layers=False)
    issues = analyze_ae_compatibility(scene, caps)
    assert [(issue.semantic_key, issue.lost_semantics) for issue in issues] == [("3d", ("3d", "rx", "ry"))]
    proposals = propose_ae_substitutions(None, scene, caps)
    assert len(proposals) == 1
    proposal = proposals[0]
    assert proposal.proposed_layers == ({"layer_type": "footage", "name": "model", "texture": "preview.png"},)
    assert not proposal.acknowledged
    texture = PlanAsset(id="c" * 64 + ".png", project_path="preview.png", sha256="c" * 64, length=1, media_kind="image/png")
    with pytest.raises(AEMappingError, match="not acknowledged"):
        map_baseline(scene, [texture], capabilities=caps, substitutions=proposals)
    mapped = map_baseline(scene, [texture], capabilities=caps, substitutions=[proposal.model_copy(update={"acknowledged": True})])
    ops = [op for batch in mapped.batches for op in batch.operations]
    assert next(op for op in ops if op.kind == "add_layer").layer_type == "footage"
    assert not any(getattr(op, "property_name", "") in {"ADBE Rotate X", "ADBE Rotate Y"} for op in ops)


def test_observed_reveal_two_spin_axes_and_later_translation_share_ir_numbering():
    scene = model_scene()
    element = scene.elements[0]
    element.tracks = {
        "reveal": Track(keys=[Keyframe(t=0, v=0), Keyframe(t=4, v=1)]),
        "rx": Track(keys=[Keyframe(t=0, v=0), Keyframe(t=8, v=720)]),
        "ry": Track(keys=[Keyframe(t=0, v=0), Keyframe(t=6, v=-180)]),
        "x": Track(keys=[Keyframe(t=10, v=0), Keyframe(t=16, v=60)]),
    }
    scene.constraints = [Constraint(pred=pred, keep=True) for pred in (
        "type(m_model_1, reveal)", "type(m_model_2, spin)", "mag(m_model_2, 720)",
        "type(m_model_3, spin)", "mag(m_model_3, -180)", "type(m_model_4, translation)",
    )]
    layer = AELayerInventory(
        layer_instance_id="l", native_layer_id=101, source_element_id="model", kind="model",
        index=1, frame_start=0, frame_end=20,
    )
    samples = [AESourceSample(
        source_element_id="model", layer_instance_id="l", frame=f, active=True,
        transform=(float(max(0, min(f - 10, 6)) * 10), 0.0, 1.0, 1.0, 0.0, 1.0),
        wipe_completion=float(max(0, 100 - f * 25)),
        rotation_x=float(min(f, 8) * 90), rotation_y=float(min(f, 6) * -30),
    ) for f in range(20)]
    def inspection_with(rows):
        return AEInspection(layers=[layer], layer_sources={"l": "model"}, layer_native_ids={"l": 101}, samples=rows)
    report = verify_inspection(scene, [inspection_with(samples)])
    assert report.passed, report.violations
    assert [(m.id, m.type, m.start, m.end, m.mag) for m in report.observed_motions] == [
        (m.id, m.type, m.start, m.end, m.mag) for m in extract_motions(scene)
    ]
    wrong = [row.model_copy(update={"rotation_x": 0.0}) for row in samples]
    assert not verify_inspection(scene, [inspection_with(wrong)]).passed
    missing = [row.model_copy(update={"rotation_y": None}) for row in samples]
    failed = verify_inspection(scene, [inspection_with(missing)])
    assert not failed.passed
    assert any("missing observation" in violation for violation in failed.violations)


def test_panel_version_gate_glb_import_renderer_vectors_and_spin_samples(tmp_path):
    scene = model_scene()
    for prop, end in (("sx", 2), ("sy", 3)):
        scene.elements[0].tracks[prop] = Track(keys=[Keyframe(t=0, v=1, ease=(0.25, 0.125, 0.75, 0.875)), Keyframe(t=10, v=end)])
    caps = capabilities()
    mapped = map_baseline(scene, [model_asset()], capabilities=caps)
    ops = [op.model_dump(mode="json", exclude_none=True) for batch in mapped.batches for op in batch.operations]
    run_panel("const ops = " + json.dumps(ops) + ";\n" + r"""
for (const [version, supported] of [['23.6.0', false], ['24.0.9', false], ['24.1.0', true], ['24.10.0', true], ['25.0.0', true]]) {
    app.version = version;
    assert.equal(panel.heartbeat().capabilities.model_layers, supported);
}
app.version = '24.1.0';
const catalog = panel.heartbeat().capabilities;
const approved = {model_layers: true, effects: catalog.effect_names, properties: catalog.property_schemas};
assert.throws(() => panel.validate({approved_capabilities: {...approved, model_layers: false}}, ops[0], 20), /model_layers/);
asset.comment = '';
let importedPath;
app.project.importFile = options => { importedPath = options.file.fsName; return asset; };
panel.importAsset({asset_id: 'b'.repeat(64) + '.glb'});
assert.ok(importedPath.endsWith('.glb'));
for (const op of ops) { panel.validate({approved_capabilities: approved}, op, 20); panel.apply(op); }
assert.equal(comp.renderer, 'ADBE Advanced 3d');
assert.deepEqual(layer.transform.anchorPoint.value, [10,5,0]);
assert.deepEqual(layer.transform.scale.value, [200,300,100]);
const eased = layer.transform.scale.eases.at(-1);
assert.equal(eased[1].length, 3);
assert.equal(eased[1][2].speed, 0);
assert.equal(layer.transform.xRotation.value, 90);
assert.equal(layer.transform.yRotation.value, 45);
// Model layers can provide spin evidence even when no 2D source rect exists.
delete layer.sourceRectAtTime;
const sample = panel.sample(layer, 1.2, {});
assert.equal(sample.rotation_x, 90);
assert.equal(sample.rotation_y, 45);
assert.equal(sample.sx, 2);
assert.equal(sample.sy, 3);
assert.equal(sample.xmin, null);
""", tmp_path)


def test_static_model_proposal_stays_a_draft_until_existing_plan_acknowledgement(tmp_path):
    from keepframe.after_effects.planning import AERenderDraft, prepare_ae_render_plan
    from keepframe.ir.store import load_scene, save_scene
    from keepframe.render.plan import PlanConflict
    from tests.test_ae_planning import _project

    root = _project(tmp_path)
    path = root / "scenes/s1/scene.v1.json"
    scene = model_scene()
    scene.id = "s1"
    scene.elements[0].canonical.model = None
    scene.elements[0].pending_asset = "3d"
    (path.parent / "preview.png").write_bytes(b"static texture")
    save_scene(scene, path)
    args = dict(project_id="p1", scene_id="s1", version_id="v1", mode="preview", capabilities=capabilities(model_layers=False))
    draft = prepare_ae_render_plan(root, **args)
    assert isinstance(draft, AERenderDraft)
    assert not draft.substitutions_acknowledged
    with pytest.raises(PlanConflict, match="acknowledg"):
        prepare_ae_render_plan(root, **args, substitutions=draft.substitutions)
    plan = prepare_ae_render_plan(root, **args, substitutions=draft.substitutions, substitutions_acknowledged=True)
    assert plan.substitutions_acknowledged
    mapped = map_baseline(load_scene(path), plan.assets, "scenes/s1", args["capabilities"], plan.substitutions)
    assert next(op for batch in mapped.batches for op in batch.operations if op.kind == "add_layer").layer_type == "footage"


def test_missing_model_asset_or_rotation_catalog_keeps_compatibility_issue():
    scene = model_scene()
    scene.elements[0].canonical.model = None
    scene.elements[0].pending_asset = "3d"
    assert [issue.semantic_key for issue in analyze_ae_compatibility(scene, capabilities())] == ["3d"]
    with pytest.raises(AEMappingError, match="GLB"):
        map_baseline(scene, [], capabilities=capabilities())
    scene.elements[0].canonical.model = "object.glb"
    payload = capabilities().capabilities.model_dump()
    payload["property_schemas"].pop("ADBE Rotate Y")
    payload["properties"] = payload["property_schemas"]
    caps = capabilities().model_copy(update={"capabilities": AECapabilityCatalog.model_validate(payload)})
    issues = analyze_ae_compatibility(scene, caps)
    assert [issue.semantic_key for issue in issues] == ["3d"]
    assert "Rotate X/Y" in issues[0].reason
    with pytest.raises(AEMappingError, match="Rotate X/Y"):
        map_baseline(scene, [model_asset()], capabilities=caps)


def test_static_model_substitution_preserves_supported_reveal_clip():
    scene = model_scene()
    scene.elements[0].tracks["reveal"] = Track(keys=[Keyframe(t=0, v=0.5), Keyframe(t=10, v=1)])
    caps = capabilities(model_layers=False)
    proposals = propose_ae_substitutions(None, scene, caps)
    texture = PlanAsset(id="c" * 64 + ".png", project_path="preview.png", sha256="c" * 64, length=1, media_kind="image/png")
    mapped = map_baseline(scene, [texture], capabilities=caps, substitutions=[proposals[0].model_copy(update={"acknowledged": True})])
    ops = [op for batch in mapped.batches for op in batch.operations]
    assert next(op for op in ops if op.kind == "set_effect").effect_name == "ADBE Linear Wipe"
    assert [key.value for op in ops if getattr(op, "property_name", None) == "ADBE Linear Wipe-0001" for key in op.keyframes] == [50, 0]


def test_model_capability_hash_preserves_legacy_absence_and_binds_true():
    from tests.test_ae_coordinator import _AE_CAPABILITY_HASH, _AE_CAPABILITY_MANIFEST

    legacy = AECapabilities.from_heartbeat({**_AE_CAPABILITY_MANIFEST, "project_open": True})
    assert not legacy.capabilities.model_layers
    assert legacy.digest == _AE_CAPABILITY_HASH
    assert AECapabilities.from_heartbeat(legacy.model_dump(mode="json")).digest == legacy.digest
    payload = legacy.model_dump(mode="json", exclude={"capability_hash"})
    payload["capabilities"]["model_layers"] = True
    assert AECapabilities.from_heartbeat(payload).digest != legacy.digest


def test_model_capability_survives_workflow_compaction_and_mcp_validation():
    from types import SimpleNamespace
    from keepframe.after_effects.mcp_server import _canonical_apply_payload
    from keepframe.after_effects.workflow import AEWorkflowService

    scene = model_scene()
    caps = capabilities()
    mapped = map_baseline(scene, [model_asset()], capabilities=caps)
    batch = mapped.batches[0]
    approved = AEWorkflowService._approved_capabilities(SimpleNamespace(capability_hash=caps.digest), caps.model_dump(mode="json"))
    compact = AEWorkflowService._approved_capabilities_for_batch(approved, batch)
    assert compact["model_layers"] is True
    payload = {
        "batch": batch.to_payload(), "approved_capabilities": compact,
        "scene_frame_count": scene.frames, "duration": mapped.composition.duration,
        "layer_count": batch.layer_count, "baseline": True, "locked_source_ids": [],
        "layer_sources": batch.start_layer_sources, "layer_native_ids": {},
        "project_id": "project", "plan_id": "plan", "session_id": "session",
    }
    assert _canonical_apply_payload(payload)["approved_capabilities"]["model_layers"] is True
    payload["approved_capabilities"] = {**compact, "model_layers": False}
    with pytest.raises(ValueError, match="model_layers"):
        _canonical_apply_payload(payload)


def test_panel_spin_wire_without_projected_bounds_verifies_motion_only_keeps():
    scene = model_scene()
    scene.constraints = [Constraint(pred="type(m_model_2, spin)", keep=True)]
    wire = {
        "layers": [{"layer_instance_id": "l", "native_layer_id": 101, "source_element_id": "model", "kind": "model", "index": 1, "frame_start": 0, "frame_end": 20}],
        "layer_sources": {"l": "model"}, "layer_native_ids": {"l": 101},
        "samples": [{"source_element_id": "model", "frame": f, "instances": [{
            "layer_instance_id": "l", "active": True, "sample": {
                "x": 0, "y": 0, "sx": 1, "sy": 1, "rot": 0, "opacity": 1,
                "wipe_completion": 0, "rotation_x": max(0, min(f - 2, 10)) * 9,
                "rotation_y": -30 + min(f, 8) * 9.375,
                "xmin": None, "ymin": None, "xmax": None, "ymax": None,
            },
        }]} for f in range(20)],
    }
    report = verify_inspection(scene, [wire])
    assert report.passed, report.violations
    assert all(sample.world_bounds is None for sample in report.inspection.samples)
