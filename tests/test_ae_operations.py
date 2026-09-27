import hashlib
import json

import pytest
from pydantic import ValidationError

from keepframe.after_effects.operations import (
    ApprovedCapabilities,
    OperationBatch,
    canonical_operation_digest,
    validate_operation_batch,
)


CAP_DIGEST = "a" * 64


def _capabilities() -> ApprovedCapabilities:
    return ApprovedCapabilities(
        digest=CAP_DIGEST,
        fonts=("Arial",),
        effects=("ADBE Gaussian Blur 2",),
        properties={
            "ADBE Position": "vec2",
            "ADBE Opacity": "number",
            "ADBE Gaussian Blur 2-0001": "number",
        },
    )


def test_operation_batch_is_strict_closed_and_capability_bound():
    batch = validate_operation_batch(
        {
            "capability_digest": CAP_DIGEST,
            "operations": [
                {"kind": "set_font", "layer_instance_id": "layer-1", "font_name": "Arial"},
                {
                    "kind": "set_effect",
                    "layer_instance_id": "layer-1",
                    "effect_name": "ADBE Gaussian Blur 2",
                    "properties": {"ADBE Gaussian Blur 2-0001": 3.0},
                },
                {
                    "kind": "set_keyframes",
                    "layer_instance_id": "layer-1",
                    "property": "ADBE Position",
                    "keyframes": [
                        {"frame": 0, "value": [0.0, 0.0]},
                        {"frame": 10, "value": [10.0, 20.0]},
                    ],
                },
            ],
        },
        approved_capabilities=_capabilities(),
        scene_frame_count=24,
        duration=1.0,
    )
    assert isinstance(batch, OperationBatch)
    assert batch.operations[0].font_name == "Arial"

    with pytest.raises(ValidationError):
        OperationBatch.model_validate(
            {
                "operations": [
                    {
                        "kind": "set_opacity",
                        "layer_instance_id": "layer-1",
                        "opacity": "0.5",
                    }
                ]
            }
        )
    with pytest.raises(ValidationError):
        OperationBatch.model_validate(
            {"operations": [{"kind": "execute_code", "code": "app.project"}]}
        )


def test_operation_batch_rejects_nan_huge_values_and_untrusted_payload_fields():
    with pytest.raises((ValidationError, ValueError)):
        OperationBatch.model_validate(
            {
                "operations": [
                    {"kind": "set_opacity", "layer_instance_id": "layer-1", "opacity": float("nan")}
                ]
            }
        )
    with pytest.raises((ValidationError, ValueError)):
        OperationBatch.model_validate(
            {
                "operations": [
                    {"kind": "set_opacity", "layer_instance_id": "layer-1", "opacity": 1_000_001.0}
                ]
            }
        )
    with pytest.raises((ValidationError, ValueError)):
        OperationBatch.model_validate(
            {
                "operations": [
                    {
                        "kind": "set_property",
                        "layer_instance_id": "layer-1",
                        "property": "ADBE Position",
                        "value": {"path": "/tmp/escape"},
                    }
                ]
            }
        )


def test_operation_batch_enforces_count_size_time_and_color_domains():
    with pytest.raises((ValidationError, ValueError)):
        OperationBatch.model_validate(
            {
                "operations": [
                    {"kind": "set_opacity", "layer_instance_id": "layer-1", "opacity": 0.5}
                ]
                * 129
            }
        )
    with pytest.raises((ValidationError, ValueError)):
        OperationBatch.model_validate(
            {
                "operations": [
                    {"kind": "set_color", "layer_instance_id": "layer-1", "color": [2.0, 0.0, 0.0]}
                ]
            }
        )
    with pytest.raises((ValidationError, ValueError)):
        validate_operation_batch(
            {
                "operations": [
                    {
                        "kind": "set_keyframes",
                        "layer_instance_id": "layer-1",
                        "property": "ADBE Position",
                        "keyframes": [{"frame": 24, "value": [0.0, 0.0]}],
                    }
                ]
            },
            approved_capabilities=_capabilities(),
            scene_frame_count=24,
        )


def test_keyframe_count_is_bounded_by_scene_frames_not_128():
    keyframes = [
        {"frame": frame, "value": [float(frame), 0.0]}
        for frame in range(256)
    ]
    batch = validate_operation_batch(
        {
            "capability_digest": CAP_DIGEST,
            "operations": [
                {
                    "kind": "set_keyframes",
                    "layer_instance_id": "layer-1",
                    "property": "ADBE Position",
                    "keyframes": keyframes,
                }
            ],
        },
        approved_capabilities=_capabilities(),
        scene_frame_count=256,
    )
    assert len(batch.operations[0].keyframes) == 256


def test_fill_color_requires_the_fill_effect_capability():
    capabilities = ApprovedCapabilities(
        digest=CAP_DIGEST,
        properties={"ADBE Fill Color": "color"},
    )
    with pytest.raises((ValidationError, ValueError), match="ADBE Fill"):
        validate_operation_batch(
            {
                "capability_digest": CAP_DIGEST,
                "operations": [
                    {
                        "kind": "set_color",
                        "layer_instance_id": "layer-1",
                        "color": [1.0, 0.0, 0.0],
                    }
                ],
            },
            approved_capabilities=capabilities,
        )


def test_effect_subproperties_require_the_dedicated_effect_operation():
    capabilities = ApprovedCapabilities(
        digest=CAP_DIGEST,
        effects=("ADBE Gaussian Blur 2",),
        properties={"ADBE Gaussian Blur 2-0001": "number"},
    )
    for operation in (
        {
            "kind": "set_property",
            "layer_instance_id": "layer-1",
            "property": "ADBE Gaussian Blur 2-0001",
            "value": 3.0,
        },
        {
            "kind": "set_keyframes",
            "layer_instance_id": "layer-1",
            "property": "ADBE Gaussian Blur 2-0001",
            "keyframes": [{"frame": 0, "value": 3.0}],
        },
    ):
        with pytest.raises((ValidationError, ValueError), match="set_effect"):
            validate_operation_batch(
                {
                    "capability_digest": CAP_DIGEST,
                    "operations": [operation],
                },
                approved_capabilities=capabilities,
            )


def test_operations_require_an_exact_fixed_property_translation():
    capabilities = ApprovedCapabilities(
        digest=CAP_DIGEST,
        effects=("ADBE Gaussian Blur 2", "ADBE Fill"),
        properties={
            "custom": "number",
            "ADBE Fill-0002": "color",
        },
    )
    operations = (
        {
            "kind": "set_property",
            "layer_instance_id": "layer-1",
            "property": "custom",
            "value": 1,
        },
        {
            "kind": "set_effect",
            "layer_instance_id": "layer-1",
            "effect_name": "ADBE Gaussian Blur 2",
            "properties": {"ADBE Fill-0002": [1.0, 0.0, 0.0]},
        },
    )
    for operation in operations:
        with pytest.raises(ValueError, match="fixed"):
            validate_operation_batch(
                {
                    "capability_digest": CAP_DIGEST,
                    "operations": [operation],
                },
                approved_capabilities=capabilities,
            )


def test_generic_opacity_and_temporal_ease_use_ae_domains():
    with pytest.raises((ValidationError, ValueError)):
        validate_operation_batch(
            {
                "capability_digest": CAP_DIGEST,
                "operations": [
                    {
                        "kind": "set_property",
                        "layer_instance_id": "layer-1",
                        "property": "ADBE Opacity",
                        "value": 1.01,
                    }
                ],
            },
            approved_capabilities=_capabilities(),
        )
    with pytest.raises((ValidationError, ValueError)):
        OperationBatch.model_validate(
            {
                "operations": [
                    {
                        "kind": "set_keyframes",
                        "layer_instance_id": "layer-1",
                        "property": "ADBE Opacity",
                        "keyframes": [
                            {
                                "frame": 0,
                                "value": 0.5,
                                "ease_in": [0.0, 101.0],
                            }
                        ],
                    }
                ]
            }
        )


def test_operation_digest_is_canonical_and_stable():
    left = {
        "capability_digest": CAP_DIGEST,
        "operations": [
            {"kind": "set_opacity", "layer_instance_id": "layer-1", "opacity": 0.5}
        ],
    }
    right = json.loads(json.dumps(left, sort_keys=True))
    assert canonical_operation_digest(left) == canonical_operation_digest(right)
    assert canonical_operation_digest(left) == hashlib.sha256(
        json.dumps(left, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()
    ).hexdigest()


def test_operation_batch_rejects_unapproved_names_and_digest_mismatch():
    with pytest.raises((ValidationError, ValueError)):
        validate_operation_batch(
            {
                "capability_digest": CAP_DIGEST,
                "operations": [
                    {"kind": "set_font", "layer_instance_id": "layer-1", "font_name": "Papyrus"}
                ],
            },
            approved_capabilities=_capabilities(),
        )
    with pytest.raises((ValidationError, ValueError)):
        validate_operation_batch(
            {"capability_digest": "b" * 64, "operations": []},
            approved_capabilities=_capabilities(),
        )


def test_property_catalog_is_exact_typed_and_layer_growth_is_bounded():
    with pytest.raises((ValidationError, ValueError)):
        ApprovedCapabilities(digest=CAP_DIGEST, properties={"ADBE Position": "unknown"})
    with pytest.raises((ValidationError, ValueError)):
        validate_operation_batch(
            {
                "capability_digest": CAP_DIGEST,
                "operations": [
                    {
                        "kind": "set_property",
                        "layer_instance_id": "layer-1",
                        "property_name": "ADBE Position",
                        "value": [1.0, 2.0],
                    }
                ],
            },
            approved_capabilities=ApprovedCapabilities(digest=CAP_DIGEST),
        )
    with pytest.raises((ValidationError, ValueError)):
        validate_operation_batch(
            {
                "capability_digest": CAP_DIGEST,
                "layer_count": 1000,
                "operations": [
                    {
                        "kind": "add_layer",
                        "layer_instance_id": "agent-1",
                        "layer_type": "null",
                        "name": "agent",
                    }
                ],
            },
            approved_capabilities=_capabilities(),
        )


def test_generic_json_integers_can_be_negative_but_remain_bounded():
    batch = OperationBatch.model_validate(
        {
            "operations": [
                {
                    "kind": "set_property",
                    "layer_instance_id": "layer-1",
                    "property_name": "custom",
                    "value": -3,
                }
            ]
        }
    )
    assert batch.operations[0].value == -3
