from __future__ import annotations

import numpy as np
import pytest

from keepframe.after_effects.verification import (
    AEInspection,
    AELayerInventory,
    AESourceSample,
    chunk_observation_pairs,
    merge_inspection_chunks,
    required_observation_frames,
    verify_inspection,
)
from keepframe.ir.schema import Background, Canonical, Constraint, Element, Keyframe, Scene, Track
from keepframe.verify.matrix import animation_matrix, extract_motions, extract_motions_from_matrices


def motion_scene(*constraints: str) -> Scene:
    return Scene(
        id="s",
        size=(200, 100),
        fps=30,
        frames=6,
        background=Background(),
        elements=[
            Element(
                id="e1",
                kind="sprite",
                canonical=Canonical(width=20, height=20),
                visible=(0, 5),
                tracks={"x": Track(keys=[Keyframe(t=0, v=0), Keyframe(t=5, v=10)])},
            ),
            Element(
                id="e2",
                kind="sprite",
                canonical=Canonical(width=20, height=20),
                visible=(0, 5),
                tracks={"x": Track(keys=[Keyframe(t=0, v=100)])},
            ),
        ],
        constraints=[Constraint(pred=pred, keep=True) for pred in constraints],
    )


def layers(*, source="e1"):
    return [
        AELayerInventory(
            layer_instance_id="l1",
            native_layer_id=101,
            source_element_id=source,
            kind="sprite",
            index=1,
            frame_start=0,
            frame_end=4,
        ),
        AELayerInventory(
            layer_instance_id="l2",
            native_layer_id=102,
            source_element_id=source,
            kind="sprite",
            index=2,
            frame_start=2,
            frame_end=6,
        ),
        AELayerInventory(
            layer_instance_id="l3",
            native_layer_id=103,
            source_element_id="e2",
            kind="sprite",
            index=3,
            frame_start=0,
            frame_end=6,
        ),
    ]


def sample(source, frame, *, x=0, bounds=(-10, -10, 10, 10), instance=None, provenance=()):
    return AESourceSample(
        source_element_id=source,
        frame=frame,
        active=True,
        transform=(x, 0, 1, 1, 0, 1),
        world_bounds=bounds,
        layer_instance_id=instance,
        provenance=provenance,
    )


def test_required_frames_resolve_motion_ids_and_spatial_frames():
    scene = motion_scene("mag(m_e1_1,10)", "left(e1,e2)", "right(e1,e2)@2")
    pairs = required_observation_frames(scene)
    assert pairs == [("e1", f) for f in range(6)] + [("e2", 2), ("e2", 5)]


def test_required_frames_reject_unknown_reference():
    scene = motion_scene("type(m_missing_1,'translation')")
    with pytest.raises(ValueError, match="unknown motion"):
        required_observation_frames(scene)


def test_chunker_is_deterministic_and_pair_capped():
    pairs = [("e2", 3), ("e1", 1), ("e1", 0), ("e2", 2)]
    assert chunk_observation_pairs(pairs, max_pairs=2) == [
        [("e1", 0), ("e1", 1)],
        [("e2", 2), ("e2", 3)],
    ]


def test_merge_unwraps_instance_rotation_across_chunks_and_full_turn():
    scene = motion_scene()
    inventory = [
        AELayerInventory(
            layer_instance_id="l1",
            native_layer_id=101,
            source_element_id="e1",
            kind="sprite",
            index=1,
            frame_start=0,
            frame_end=4,
        )
    ]
    rotations = [179.0, -179.0, -1.0, 1.0]
    chunks = []
    for frame, rotation in enumerate(rotations):
        chunks.append(
            AEInspection(
                layers=inventory,
                layer_sources={"l1": "e1"},
                layer_native_ids={"l1": 101},
                samples=[
                    sample("e1", frame, instance="l1").model_copy(
                        update={
                            "transform": (0.0, 0.0, 1.0, 1.0, rotation, 1.0)
                        }
                    )
                ],
            )
        )
    merged = merge_inspection_chunks(
        scene,
        chunks,
        authoritative_instance_sources={"l1": "e1"},
        authoritative_instance_native_ids={"l1": 101},
    )
    observed = [
        row.transform[4]
        for row in merged.samples
        if row.transform is not None
    ]
    assert observed == [179.0, 181.0, 359.0, 361.0]


def test_merge_unwraps_rotation_across_source_z_instances():
    scene = motion_scene()
    inventory = [
        AELayerInventory(
            layer_instance_id="l-out",
            native_layer_id=101,
            source_element_id="e1",
            kind="sprite",
            index=1,
            frame_start=0,
            frame_end=2,
        ),
        AELayerInventory(
            layer_instance_id="l-in",
            native_layer_id=102,
            source_element_id="e1",
            kind="sprite",
            index=2,
            frame_start=2,
            frame_end=4,
        ),
    ]
    chunks = [
        AEInspection(
            layers=inventory,
            layer_sources={"l-out": "e1", "l-in": "e1"},
            layer_native_ids={"l-out": 101, "l-in": 102},
            samples=[
                sample("e1", 1, instance="l-out").model_copy(
                    update={"transform": (0.0, 0.0, 1.0, 1.0, 179.0, 1.0)}
                ),
                sample("e1", 1, instance="l-in").model_copy(
                    update={"active": False, "transform": None, "world_bounds": None}
                ),
            ],
        ),
        AEInspection(
            layers=inventory,
            layer_sources={"l-out": "e1", "l-in": "e1"},
            layer_native_ids={"l-out": 101, "l-in": 102},
            samples=[
                sample("e1", 2, instance="l-out").model_copy(
                    update={"active": False, "transform": None, "world_bounds": None}
                ),
                sample("e1", 2, instance="l-in").model_copy(
                    update={"transform": (0.0, 0.0, 1.0, 1.0, -179.0, 1.0)}
                ),
            ],
        ),
    ]
    merged = merge_inspection_chunks(
        scene,
        chunks,
        authoritative_instance_sources={"l-out": "e1", "l-in": "e1"},
        authoritative_instance_native_ids={"l-out": 101, "l-in": 102},
    )
    observed = [
        row.transform[4]
        for row in sorted(merged.samples, key=lambda row: row.frame)
        if row.transform is not None
    ]
    assert observed == [179.0, 181.0]


def test_merge_raw_instance_rows_unions_bounds_and_chooses_lowest_id_transform():
    scene = motion_scene("left(e1,e2)@2")
    rows = [
        sample("e1", 2, x=7, bounds=(0, 0, 10, 10), instance="l1"),
        sample("e1", 2, x=99, bounds=(8, 2, 20, 12), instance="l2"),
        sample("e2", 2, x=100, bounds=(100, 0, 120, 20), instance="l3"),
    ]
    merged = merge_inspection_chunks(
        scene,
        [AEInspection(
            layers=layers(),
            layer_native_ids={"l1": 101, "l2": 102, "l3": 103},
            samples=rows,
        )],
        authoritative_instance_sources={"l1": "e1", "l2": "e1", "l3": "e2"},
        authoritative_instance_native_ids={"l1": 101, "l2": 102, "l3": 103},
    )
    e1 = next(row for row in merged.samples if row.source_element_id == "e1" and row.frame == 2)
    assert e1.transform == (7.0, 0.0, 1.0, 1.0, 0.0, 1.0)
    assert e1.world_bounds == (0.0, 0.0, 20.0, 12.0)


def test_merge_rejects_source_spoof():
    bad = AELayerInventory(
        layer_instance_id="l1",
        native_layer_id=101,
        source_element_id="e2",
        kind="sprite",
        index=1,
        frame_start=0,
        frame_end=6,
    )
    with pytest.raises(ValueError, match="source"):
        merge_inspection_chunks(
            motion_scene(),
            [AEInspection(layers=[bad], layer_native_ids={"l1": 101}, samples=[])],
            authoritative_instance_sources={"l1": "e1"},
            authoritative_instance_native_ids={"l1": 101},
        )


def test_missing_required_sample_fails_report_without_crashing():
    scene = motion_scene("mag(m_e1_1,10)")
    report = verify_inspection(
        scene,
        [AEInspection(
            layers=layers(),
            layer_native_ids={"l1": 101, "l2": 102, "l3": 103},
            samples=[],
        )],
        authoritative_instance_sources={"l1": "e1", "l2": "e1", "l3": "e2"},
        authoritative_instance_native_ids={"l1": 101, "l2": 102, "l3": 103},
    )
    assert any("missing" in violation.lower() for violation in report.violations)


def test_spatial_predicates_use_explicit_and_final_frames():
    scene = motion_scene("left(e1,e2)", "right(e2,e1)@2")
    rows = [
        sample("e1", 5, bounds=(0, 0, 10, 10), provenance=("l1", "l2")),
        sample("e2", 5, bounds=(20, 0, 30, 10), provenance=("l3",)),
        sample("e1", 2, bounds=(0, 0, 10, 10), provenance=("l1", "l2")),
        sample("e2", 2, bounds=(20, 0, 30, 10), provenance=("l3",)),
    ]
    report = verify_inspection(
        scene,
        [AEInspection(
            layers=layers(),
            layer_native_ids={"l1": 101, "l2": 102, "l3": 103},
            samples=rows,
        )],
        authoritative_instance_sources={"l1": "e1", "l2": "e1", "l3": "e2"},
        authoritative_instance_native_ids={"l1": 101, "l2": 102, "l3": 103},
    )
    assert {row.frame for row in report.inspection.samples} == {2, 5}


def test_active_motion_sample_without_transform_is_missing():
    scene = motion_scene("mag(m_e1_1,10)")
    rows = [
        sample("e1", frame, provenance=("l1", "l2"))
        for frame in range(scene.frames)
    ]
    rows[2] = rows[2].model_copy(update={"transform": None})
    report = verify_inspection(
        scene,
        [
            AEInspection(
                layers=layers(),
                layer_native_ids={"l1": 101, "l2": 102, "l3": 103},
                samples=rows,
            )
        ],
        authoritative_instance_sources={"l1": "e1", "l2": "e1", "l3": "e2"},
        authoritative_instance_native_ids={"l1": 101, "l2": 102, "l3": 103},
    )
    assert report.passed is False
    assert "missing observation: e1@2" in report.violations


def test_active_spatial_sample_without_bounds_is_missing():
    scene = motion_scene("left(e1,e2)@2")
    rows = [
        sample("e1", 2, provenance=("l1", "l2")).model_copy(
            update={"world_bounds": None}
        ),
        sample("e2", 2, bounds=(20, 0, 30, 10), provenance=("l3",)),
    ]
    report = verify_inspection(
        scene,
        [
            AEInspection(
                layers=layers(),
                layer_native_ids={"l1": 101, "l2": 102, "l3": 103},
                samples=rows,
            )
        ],
        authoritative_instance_sources={"l1": "e1", "l2": "e1", "l3": "e2"},
        authoritative_instance_native_ids={"l1": 101, "l2": 102, "l3": 103},
    )
    assert report.passed is False
    assert "missing observation: e1@2" in report.violations


def test_active_raw_contributor_without_transform_blocks_motion_aggregation():
    scene = motion_scene("mag(m_e1_1,10)")
    rows = [
        sample("e1", frame, provenance=("l1", "l2"))
        for frame in (0, 1, 3, 4, 5)
    ]
    rows.insert(2, sample("e1", 2, instance="l1"))
    rows.insert(
        3,
        sample("e1", 2, instance="l2").model_copy(update={"transform": None}),
    )
    report = verify_inspection(
        scene,
        [
            AEInspection(
                layers=layers(),
                layer_native_ids={"l1": 101, "l2": 102, "l3": 103},
                samples=rows,
            )
        ],
        authoritative_instance_sources={"l1": "e1", "l2": "e1", "l3": "e2"},
        authoritative_instance_native_ids={"l1": 101, "l2": 102, "l3": 103},
    )
    assert report.passed is False
    assert "missing observation: e1@2" in report.violations


def test_active_raw_contributor_without_bounds_blocks_spatial_aggregation():
    scene = motion_scene("left(e1,e2)@2")
    rows = [
        sample("e1", 2, instance="l1"),
        sample("e1", 2, instance="l2").model_copy(update={"world_bounds": None}),
        sample("e2", 2, instance="l3"),
    ]
    report = verify_inspection(
        scene,
        [
            AEInspection(
                layers=layers(),
                layer_native_ids={"l1": 101, "l2": 102, "l3": 103},
                samples=rows,
            )
        ],
        authoritative_instance_sources={"l1": "e1", "l2": "e1", "l3": "e2"},
        authoritative_instance_native_ids={"l1": 101, "l2": 102, "l3": 103},
    )
    assert report.passed is False
    assert "missing observation: e1@2" in report.violations


def test_omitted_active_contributor_is_missing_even_when_subset_would_pass():
    scene = motion_scene("left(e1,e2)@2")
    report = verify_inspection(
        scene,
        [
            AEInspection(
                layers=layers(),
                layer_native_ids={"l1": 101, "l2": 102, "l3": 103},
                samples=[
                    sample("e1", 2, bounds=(0, 0, 10, 10), instance="l1"),
                    sample("e2", 2, bounds=(20, 0, 30, 10), instance="l3"),
                ],
            )
        ],
        authoritative_instance_sources={"l1": "e1", "l2": "e1", "l3": "e2"},
        authoritative_instance_native_ids={"l1": 101, "l2": 102, "l3": 103},
    )
    assert report.passed is False
    assert "missing observation: e1@2" in report.violations


def test_inactive_contributor_is_supported_when_returned_as_inactive():
    scene = motion_scene("left(e1,e2)")
    report = verify_inspection(
        scene,
        [
            AEInspection(
                layers=layers(),
                layer_native_ids={"l1": 101, "l2": 102, "l3": 103},
                samples=[
                    AESourceSample(
                        source_element_id="e1",
                        frame=5,
                        active=False,
                        layer_instance_id="l1",
                    ),
                    sample(
                        "e1",
                        5,
                        bounds=(0, 0, 10, 10),
                        instance="l2",
                    ),
                    sample(
                        "e2",
                        5,
                        bounds=(20, 0, 30, 10),
                        instance="l3",
                    ),
                ],
            )
        ],
        authoritative_instance_sources={"l1": "e1", "l2": "e1", "l3": "e2"},
        authoritative_instance_native_ids={"l1": 101, "l2": 102, "l3": 103},
    )
    assert report.passed is True


def test_omitted_inactive_contributor_is_missing():
    scene = motion_scene("left(e1,e2)")
    report = verify_inspection(
        scene,
        [
            AEInspection(
                layers=layers(),
                layer_native_ids={"l1": 101, "l2": 102, "l3": 103},
                samples=[
                    sample(
                        "e1",
                        5,
                        bounds=(0, 0, 10, 10),
                        instance="l2",
                    ),
                    sample(
                        "e2",
                        5,
                        bounds=(20, 0, 30, 10),
                        instance="l3",
                    ),
                ],
            )
        ],
        authoritative_instance_sources={"l1": "e1", "l2": "e1", "l3": "e2"},
        authoritative_instance_native_ids={"l1": 101, "l2": 102, "l3": 103},
    )
    assert report.passed is False
    assert "missing observation: e1@5" in report.violations


def test_interval_spoof_cannot_hide_missing_instance():
    scene = motion_scene("left(e1,e2)@2")
    spoofed_layers = layers()
    spoofed_layers[1] = spoofed_layers[1].model_copy(
        update={"frame_start": 3, "frame_end": 6}
    )
    report = verify_inspection(
        scene,
        [
            AEInspection(
                layers=spoofed_layers,
                layer_native_ids={"l1": 101, "l2": 102, "l3": 103},
                samples=[
                    sample(
                        "e1",
                        2,
                        bounds=(0, 0, 10, 10),
                        instance="l1",
                    ),
                    sample(
                        "e2",
                        2,
                        bounds=(20, 0, 30, 10),
                        instance="l3",
                    ),
                ],
            )
        ],
        authoritative_instance_sources={"l1": "e1", "l2": "e1", "l3": "e2"},
        authoritative_instance_native_ids={"l1": 101, "l2": 102, "l3": 103},
    )
    assert report.passed is False
    assert "missing observation: e1@2" in report.violations


def test_source_less_layer_does_not_contribute_to_requested_coverage():
    scene = motion_scene("left(e1,e2)@2")
    inventory = layers() + [
        AELayerInventory(
            layer_instance_id="agent1",
            native_layer_id=104,
            source_element_id=None,
            kind="shape",
            index=4,
            frame_start=0,
            frame_end=6,
        )
    ]
    report = verify_inspection(
        scene,
        [
            AEInspection(
                layers=inventory,
                layer_native_ids={"l1": 101, "l2": 102, "l3": 103, "agent1": 104},
                samples=[
                    sample("e1", 2, bounds=(0, 0, 10, 10), provenance=("l1", "l2")),
                    sample("e2", 2, bounds=(20, 0, 30, 10), provenance=("l3",)),
                ],
            )
        ],
        authoritative_instance_sources={
            "l1": "e1",
            "l2": "e1",
            "l3": "e2",
            "agent1": None,
        },
        authoritative_instance_native_ids={
            "l1": 101,
            "l2": 102,
            "l3": 103,
            "agent1": 104,
        },
    )
    assert report.passed is True


def test_requested_pair_without_result_is_missing():
    scene = motion_scene("left(e1,e2)@2")
    report = verify_inspection(
        scene,
        [
            AEInspection(
                layers=layers(),
                layer_native_ids={"l1": 101, "l2": 102, "l3": 103},
                requested=[
                    {"source_element_id": "e1", "frame": 2},
                    {"source_element_id": "e2", "frame": 2},
                ],
                samples=[
                    sample("e1", 2, bounds=(0, 0, 10, 10), instance="l1"),
                    sample("e1", 2, bounds=(8, 2, 20, 12), instance="l2"),
                ],
            )
        ],
        authoritative_instance_sources={"l1": "e1", "l2": "e1", "l3": "e2"},
        authoritative_instance_native_ids={"l1": 101, "l2": 102, "l3": 103},
    )
    assert report.passed is False
    assert "missing observation: e2@2" in report.violations


def test_sample_outside_declared_request_is_rejected():
    scene = motion_scene("left(e1,e2)@2")
    with pytest.raises(ValueError, match="requested"):
        merge_inspection_chunks(
            scene,
            [
                AEInspection(
                    layers=layers(),
                    layer_native_ids={"l1": 101, "l2": 102, "l3": 103},
                    requested=[{"source_element_id": "e1", "frame": 2}],
                    samples=[
                        sample("e1", 5, bounds=(0, 0, 10, 10), provenance=("l2",)),
                    ],
                )
            ],
            authoritative_instance_sources={"l1": "e1", "l2": "e1", "l3": "e2"},
            authoritative_instance_native_ids={"l1": 101, "l2": 102, "l3": 103},
        )


def test_motion_extraction_from_matrices_matches_scene_path():
    scene = motion_scene("mag(m_e1_1,10)")
    matrices = animation_matrix(scene)
    direct = extract_motions(scene)
    from_matrices = extract_motions_from_matrices(scene, matrices)
    assert [m.__dict__ for m in from_matrices] == [m.__dict__ for m in direct]
    bad = dict(matrices)
    bad["e1"] = np.zeros((scene.frames, 5), dtype=float)
    with pytest.raises(ValueError, match="shape"):
        extract_motions_from_matrices(scene, bad)


def test_panel_nested_inspection_wire_is_flattened_and_missing_is_reported():
    scene = motion_scene("left(e1,e2)@2")
    payload = {
        "schema_version": "keepframe.ae-inspection/1",
        "frame_count": 6,
        "fps": 30,
        "layer_sources": {"l1": "e1", "l2": "e1", "l3": "e2"},
        "layer_native_ids": {"l1": 101, "l2": 102, "l3": 103},
        "layers": [
            {"layer_instance_id": "l1", "native_layer_id": 101, "source_element_id": "e1", "index": 1, "frame_start": 0, "frame_end": 4},
            {"layer_instance_id": "l2", "native_layer_id": 102, "source_element_id": "e1", "index": 2, "frame_start": 2, "frame_end": 6},
            {"layer_instance_id": "l3", "native_layer_id": 103, "source_element_id": "e2", "index": 3, "frame_start": 0, "frame_end": 6},
        ],
        "samples": [
            {
                "source_element_id": "e1",
                "frame": 2,
                "instances": [
                    {
                        "layer_instance_id": "l1",
                        "sample": {
                            "x": 0,
                            "y": 0,
                            "sx": 1,
                            "sy": 1,
                            "rot": 0,
                            "opacity": 1,
                            "xmin": 0,
                            "ymin": 0,
                            "xmax": 10,
                            "ymax": 10,
                        },
                    }
                ],
            },
            {"source_element_id": "e2", "frame": 2, "instances": []},
        ],
        "missing_pairs": [],
        "requested": [
            {"source_element_id": "e1", "frame": 2},
            {"source_element_id": "e2", "frame": 2},
        ],
        "source_aggregated": False,
    }
    report = verify_inspection(scene, [payload])
    assert report.passed is False
    assert any("missing" in violation.lower() for violation in report.violations)


def test_inactive_nested_instance_is_not_a_missing_pair():
    payload = {
        "schema_version": "keepframe.ae-inspection/1",
        "frame_count": 6,
        "fps": 30,
        "layer_sources": {"l1": "e1"},
        "layer_native_ids": {"l1": 101},
        "layers": [
            {
                "layer_instance_id": "l1",
                "native_layer_id": 101,
                "source_element_id": "e1",
                "kind": "sprite",
                "index": 1,
                "frame_start": 0,
                "frame_end": 4,
            }
        ],
        "samples": [
            {
                "source_element_id": "e1",
                "frame": 5,
                "instances": [
                    {"layer_instance_id": "l1", "active": False, "sample": None}
                ],
            }
        ],
        "missing_pairs": [],
        "source_aggregated": False,
    }
    inspection = AEInspection.model_validate(payload)
    assert inspection.samples[0].active is False
    merged = merge_inspection_chunks(
        motion_scene(),
        [inspection],
        authoritative_instance_sources={"l1": "e1"},
    )
    assert merged.missing_pairs == ()
    assert merged.samples[0].active is False


def test_active_nested_instance_without_sample_remains_missing():
    payload = {
        "schema_version": "keepframe.ae-inspection/1",
        "frame_count": 6,
        "fps": 30,
        "layer_sources": {"l1": "e1"},
        "layer_native_ids": {"l1": 101},
        "layers": [
            {
                "layer_instance_id": "l1",
                "native_layer_id": 101,
                "source_element_id": "e1",
                "kind": "sprite",
                "index": 1,
                "frame_start": 0,
                "frame_end": 6,
            }
        ],
        "samples": [
            {
                "source_element_id": "e1",
                "frame": 5,
                "instances": [
                    {"layer_instance_id": "l1", "active": True, "sample": None}
                ],
            }
        ],
        "missing_pairs": [],
        "source_aggregated": False,
    }
    inspection = AEInspection.model_validate(payload)
    assert inspection.samples[0].active is True
    assert [(pair.source_element_id, pair.frame) for pair in inspection.missing_pairs] == [
        ("e1", 5)
    ]
    merged = merge_inspection_chunks(
        motion_scene("mag(m_e1_1,10)"),
        [inspection],
        authoritative_instance_sources={"l1": "e1"},
    )
    assert [(pair.source_element_id, pair.frame) for pair in merged.missing_pairs] == [
        ("e1", 5)
    ]
    report = verify_inspection(
        motion_scene("mag(m_e1_1,10)"),
        [inspection],
        authoritative_instance_sources={"l1": "e1"},
    )
    assert report.passed is False
    assert any("missing observation: e1@5" == violation for violation in report.violations)
 
def test_inspection_requires_positive_unique_native_layer_ids():
    with pytest.raises(ValueError, match="native"):
        AELayerInventory(
            layer_instance_id="l1",
            source_element_id="e1",
            kind="sprite",
            index=1,
            frame_start=0,
            frame_end=6,
        )

    rows = layers()
    rows[1] = rows[1].model_copy(update={"native_layer_id": rows[0].native_layer_id})
    with pytest.raises(ValueError, match="native"):
        AEInspection(
            layers=rows,
            layer_native_ids={
                row.layer_instance_id: row.native_layer_id for row in rows
            },
        )

    with pytest.raises(ValueError, match="native"):
        AEInspection(
            layers=layers(),
            layer_native_ids={"l1": 999, "l2": 102, "l3": 103},
        )


def test_merge_rejects_replacement_with_same_comment_but_different_native_id():
    scene = motion_scene()
    rows = layers()
    rows[0] = rows[0].model_copy(update={"native_layer_id": 999})
    inspection = AEInspection(
        layers=rows,
        layer_sources={row.layer_instance_id: row.source_element_id for row in rows},
        layer_native_ids={row.layer_instance_id: row.native_layer_id for row in rows},
        samples=[],
    )

    with pytest.raises(ValueError, match="native"):
        merge_inspection_chunks(
            scene,
            [inspection],
            authoritative_instance_sources={"l1": "e1", "l2": "e1", "l3": "e2"},
            authoritative_instance_native_ids={"l1": 101, "l2": 102, "l3": 103},
        )


def test_manual_inventory_allows_source_less_deletion_but_requires_exact_existing_identity():
    from keepframe.after_effects.verification import validate_manual_inventory

    prior_rows = layers()
    prior_rows[2] = prior_rows[2].model_copy(update={"source_element_id": None})
    prior = AEInspection(
        layers=prior_rows,
        layer_sources={"l1": "e1", "l2": "e1", "l3": None},
        layer_native_ids={"l1": 101, "l2": 102, "l3": 103},
    )
    current_rows = [row for row in prior.layers if row.layer_instance_id != "l3"]
    current = AEInspection(
        layers=current_rows,
        layer_sources={"l1": "e1", "l2": "e1"},
        layer_native_ids={"l1": 101, "l2": 102},
    )

    sources, native_ids = validate_manual_inventory(
        current,
        prior_layer_sources=prior.layer_sources,
        prior_layer_native_ids=prior.layer_native_ids,
        allow_new_agent_ids=(),
    )

    assert sources == {"l1": "e1", "l2": "e1"}
    assert native_ids == {"l1": 101, "l2": 102}

    replacement = current.layers[0].model_copy(update={"native_layer_id": 999})
    with pytest.raises(ValueError, match="native"):
        validate_manual_inventory(
            current.model_copy(
                update={
                    "layers": (replacement,) + current.layers[1:],
                    "layer_native_ids": {"l1": 999, "l2": 102},
                }
            ),
            prior_layer_sources=prior.layer_sources,
            prior_layer_native_ids=prior.layer_native_ids,
            allow_new_agent_ids=(),
        )
