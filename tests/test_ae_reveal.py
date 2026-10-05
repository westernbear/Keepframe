import json
from pathlib import Path
import shutil
import subprocess

import pytest

from keepframe.after_effects.compatibility import analyze_ae_compatibility
from keepframe.after_effects.mapping import AEMappingError, map_baseline
from keepframe.after_effects.models import AECapabilities, AECapabilityCatalog, AEEffect
from keepframe.ir.schema import Background, Canonical, Element, Keyframe, Scene, Track
from keepframe.render.plan import PlanAsset
from keepframe.after_effects.verification import AEInspection, AELayerInventory, AESourceSample, verify_inspection
from keepframe.ir.schema import Constraint
from keepframe.verify.matrix import extract_motions


def capabilities(*, wipe=True, properties=None):
    schemas = {
        "ADBE Anchor Point": "vec2", "ADBE Position X": "number",
        "ADBE Position Y": "number", "ADBE Scale": "vec2",
        "ADBE Rotate Z": "number", "ADBE Skew": "number", "ADBE Opacity": "number",
    }
    wipe_schemas = {f"ADBE Linear Wipe-000{i}": "number" for i in (1, 2, 3)}
    if wipe:
        schemas.update(wipe_schemas)
    if properties is not None:
        schemas = properties
    return AECapabilities(
        version="24.1", major=24, host="after-effects", ready=True, project_open=True,
        capabilities=AECapabilityCatalog(
            effect_names=("ADBE Linear Wipe",) if wipe else (),
            effects=(AEEffect(match_name="ADBE Linear Wipe", properties=wipe_schemas),) if wipe else (),
            property_schemas=schemas,
        ),
    )


def scene_with_reveal(keys):
    return Scene(
        id="scene", size=(200, 100), fps=10, frames=20, background=Background(),
        elements=[Element(
            id="e", kind="sprite", canonical=Canonical(width=20, height=10, texture="sprite.png"),
            visible=(0, 19), tracks={"reveal": Track(keys=keys)},
        )],
    )


def image_asset():
    return PlanAsset(id="a" * 64, project_path="sprite.png", sha256="a" * 64, length=1, media_kind="image/png")


def run_panel(script, tmp_path):
    """Execute the shipped panel against the external AE API boundary."""
    node = shutil.which("node")
    if node is None:
        pytest.skip("node unavailable")
    source = Path("keepframe/after_effects/assets/keepframe_panel.jsx").read_text()
    # Skip ScriptUI startup and expose the bridge handlers for this AE mock.
    source = source[:source.index("    $.global.KeepframeBridge_poll =")]
    source += """
    $.global.panel = { heartbeat: heartbeat, validate: validateOperation, apply: applyOperation,
        sample: inspectLayerSample, importAsset: importServerAsset,
        bind: function (comp) { SESSION_COMP = comp; SESSION_MARKER = comp.comment; MEDIA_DIR = {fsName: '/media'}; }
    };
}(this));
"""
    harness = r"""
const assert = require('node:assert/strict');
globalThis.$ = {getenv: () => '/tmp', global: globalThis};
globalThis.Folder = function(path) { return {fsName: path}; };
Folder.userData = {fsName: '/tmp'};
globalThis.File = function(path) { return {fsName: path, exists: true}; };
globalThis.ImportOptions = function(file) { this.file = file; };
globalThis.CompItem = function() {};
globalThis.KeyframeEase = function(speed, influence) { this.speed = speed; this.influence = influence; };
function prop(value) {
    return {value, keys: [], eases: [], valueAtTime: function() { return this.value; },
        setValue: function(v) { if (Array.isArray(this.value)) assert.equal(v.length, this.value.length); this.value = v; },
        setValueAtTime: function(t, v) { this.setValue(v); this.keys.push([t, v]); },
        nearestKeyIndex: function() { return this.keys.length; },
        keyInTemporalEase: function() { return Array.from({length: Array.isArray(this.value) ? this.value.length : 1}, () => new KeyframeEase(0, 33)); },
        keyOutTemporalEase: function() { return this.keyInTemporalEase(); },
        setTemporalEaseAtKey: function(i, a, b) { this.eases.push([i, a, b]); }
    };
}
const effects = {};
const parade = {property: n => effects[n], addProperty: n => {
    const properties = {};
    for (const suffix of ['0001', '0002', '0003']) properties[n + '-' + suffix] = prop(0);
    return effects[n] = {property: name => properties[name]};
}};
const layer = {id: 101, comment: 'keepframe:layer=l;source_element_id=e', enabled: true,
    transform: {anchorPoint: prop([0,0]), position: prop([0,0]), xPosition: prop(0), yPosition: prop(0),
        scale: prop([100,100]), zRotation: prop(0), xRotation: prop(0), yRotation: prop(0), opacity: prop(100)},
    property: name => name === 'ADBE Effect Parade' ? parade : null,
    toComp: p => p, sourceRectAtTime: () => ({left: 0, top: 0, width: 20, height: 10})
};
const comp = new CompItem();
Object.assign(comp, {comment: 'session', frameRate: 10, numLayers: 1, layer: () => layer,
    renderers: ['ADBE Classic 3d', 'ADBE Advanced 3d'], renderer: 'ADBE Classic 3d',
    layers: {add: () => { comp.numLayers++; layer.threeDLayer = true;
        layer.transform.anchorPoint = prop([0,0,0]); layer.transform.position = prop([0,0,0]);
        layer.transform.scale = prop([100,100,100]); return layer; }}
});
const asset = {comment: 'keepframe:asset=' + 'b'.repeat(64) + '.glb', mainSource: {}, name: 'object.glb'};
globalThis.app = {version: '24.1.0', effects: [{matchName: 'ADBE Linear Wipe'}],
    project: {numItems: 2, item: i => i === 1 ? comp : asset, importFile: () => asset},
    beginUndoGroup: () => {}, endUndoGroup: () => {}
};
"""
    path = tmp_path / "panel.cjs"
    path.write_text(harness + source + "\npanel.bind(comp);\n" + script)
    result = subprocess.run([node, str(path)], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr


def test_panel_executes_wipe_keyframes_and_reads_completion(tmp_path):
    scene = scene_with_reveal([Keyframe(t=0, v=0.25), Keyframe(t=12, v=1)])
    mapped = map_baseline(scene, [image_asset()], capabilities=capabilities())
    ops = [op.model_copy(update={"layer_instance_id": "l"}).model_dump(mode="json", exclude_none=True)
           for batch in mapped.batches for op in batch.operations
           if op.kind == "set_effect" or getattr(op, "property_name", None) == "ADBE Linear Wipe-0001"]
    run_panel("const ops = " + json.dumps(ops) + ";\n" + r"""
const catalog = panel.heartbeat().capabilities;
assert.ok(catalog.effect_names.includes('ADBE Linear Wipe'));
const approved = {effects: catalog.effect_names, properties: catalog.property_schemas};
for (const op of ops) { panel.validate({approved_capabilities: approved}, op, 20); panel.apply(op); }
const wipe = effects['ADBE Linear Wipe'];
assert.equal(wipe.property('ADBE Linear Wipe-0002').value, 270);
assert.equal(wipe.property('ADBE Linear Wipe-0003').value, 0);
assert.deepEqual(wipe.property('ADBE Linear Wipe-0001').keys, [[0, 75], [1.2, 0]]);
assert.equal(panel.sample(layer, 1.2, {}).wipe_completion, 0);
""", tmp_path)


def test_reveal_maps_completion_angle_feather_times_and_signed_ease():
    scene = scene_with_reveal([
        Keyframe(t=2, v=0.25, ease=(0.25, 0.125, 0.75, 0.875)),
        Keyframe(t=12, v=1),
    ])
    assert analyze_ae_compatibility(scene, capabilities()) == ()
    result = map_baseline(scene, [image_asset()], capabilities=capabilities())
    ops = [op for batch in result.batches for op in batch.operations]
    wipe = next(op for op in ops if op.kind == "set_effect")
    assert wipe.effect_name == "ADBE Linear Wipe"
    assert wipe.properties == {
        "ADBE Linear Wipe-0001": 75.0,
        "ADBE Linear Wipe-0002": 270.0,
        "ADBE Linear Wipe-0003": 0.0,
    }
    completion = next(op for op in ops if getattr(op, "property_name", None) == "ADBE Linear Wipe-0001")
    assert [key.frame for key in completion.keyframes] == [0, 2, 12]
    assert [key.time for key in completion.keyframes] == [0, 0.2, 1.2]
    assert [key.value for key in completion.keyframes] == [75, 75, 0]
    assert completion.keyframes[1].ease_out == [-37.5, 25.0]
    assert completion.keyframes[2].ease_in == [-37.5, 25.0]


def test_reveal_before_translation_uses_ir_motion_ids_and_observed_completion():
    scene = scene_with_reveal([Keyframe(t=0, v=0), Keyframe(t=4, v=1)])
    scene.elements[0].tracks["x"] = Track(keys=[Keyframe(t=6, v=0), Keyframe(t=12, v=60)])
    scene.constraints = [Constraint(pred=pred, keep=True) for pred in (
        "type(m_e_1, reveal)", "type(m_e_2, translation)", "before(m_e_1, m_e_2)",
    )]
    layer = AELayerInventory(
        layer_instance_id="l", native_layer_id=101, source_element_id="e",
        kind="footage", index=1, frame_start=0, frame_end=20,
    )
    samples = [AESourceSample(
        source_element_id="e", layer_instance_id="l", frame=f, active=True,
        transform=(float(max(0, min(f - 6, 6)) * 10), 0.0, 1.0, 1.0, 0.0, 1.0),
        wipe_completion=float(max(0, 100 - f * 25)),
    ) for f in range(20)]
    def inspection_with(rows):
        return AEInspection(layers=[layer], layer_sources={"l": "e"}, layer_native_ids={"l": 101}, samples=rows)

    inspection = inspection_with(samples)
    report = verify_inspection(scene, [inspection])
    assert report.passed, report.violations
    assert [(m.id, m.type, m.start, m.end) for m in report.observed_motions] == [
        (m.id, m.type, m.start, m.end) for m in extract_motions(scene)
    ] == [("m_e_1", "reveal", 0, 4), ("m_e_2", "translation", 6, 12)]
    wrong = [row.model_copy(update={"wipe_completion": 0.0}) for row in samples]
    assert not verify_inspection(scene, [inspection_with(wrong)]).passed
    missing = [row.model_copy(update={"wipe_completion": None}) for row in samples]
    failed = verify_inspection(scene, [inspection_with(missing)])
    assert not failed.passed
    assert any("missing observation" in violation for violation in failed.violations)


@pytest.mark.parametrize("missing", ["effect", "schema"])
def test_missing_wipe_catalog_retains_issue_and_blocks_native_mapping(missing):
    scene = scene_with_reveal([Keyframe(t=0, v=0.5)])
    caps = capabilities(wipe=missing != "effect")
    if missing == "schema":
        schemas = dict(caps.capabilities.property_schemas)
        schemas.pop("ADBE Linear Wipe-0002")
        caps = capabilities(properties=schemas)
    assert [issue.semantic_key for issue in analyze_ae_compatibility(scene, caps)] == ["reveal"]
    with pytest.raises(AEMappingError, match="Linear Wipe"):
        map_baseline(scene, [image_asset()], capabilities=caps)


def test_static_reveal_clips_without_opacity_changes_and_fully_visible_needs_no_effect():
    scene = scene_with_reveal([Keyframe(t=0, v=0.5)])
    mapped = map_baseline(scene, [image_asset()], capabilities=capabilities())
    ops = [op for batch in mapped.batches for op in batch.operations]
    assert next(op for op in ops if op.kind == "set_effect").properties["ADBE Linear Wipe-0001"] == 50
    assert next(op for op in ops if op.kind == "set_opacity").opacity == 1
    scene.elements[0].tracks["reveal"] = Track(keys=[Keyframe(t=0, v=1)])
    assert analyze_ae_compatibility(scene, capabilities(wipe=False)) == ()
    mapped = map_baseline(scene, [image_asset()], capabilities=capabilities(wipe=False))
    assert not any(op.kind == "set_effect" for batch in mapped.batches for op in batch.operations)


def test_reveal_easing_gap_and_out_of_range_fraction_are_reported():
    scene = scene_with_reveal([Keyframe(t=0, v=0, ease=(0, 0.2, 0.5, 1)), Keyframe(t=10, v=1)])
    assert [issue.semantic_key for issue in analyze_ae_compatibility(scene, capabilities())] == ["easing"]
    scene.elements[0].tracks["reveal"] = Track(keys=[Keyframe(t=0, v=1.2)])
    assert [issue.semantic_key for issue in analyze_ae_compatibility(scene, capabilities())] == ["reveal"]
    with pytest.raises(AEMappingError, match="reveal"):
        map_baseline(scene, [image_asset()], capabilities=capabilities())
