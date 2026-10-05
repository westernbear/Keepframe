import json
from types import SimpleNamespace

import pytest
from pydantic import ValidationError

from keepframe.after_effects.compatibility import (
    AEFontResolutionError,
    analyze_ae_compatibility,
    parse_ae_substitutions,
    propose_ae_substitutions,
    resolve_ae_font,
)


from keepframe.after_effects.models import (
    AEEffect,
    AECapabilities,
    AECapabilityCatalog,
    AEFont,
)
from keepframe.ir.schema import Background, Canonical, Element, FontGuess, Keyframe, Scene, Track


def _capabilities(*, fonts=("Arial",), effects=("ADBE Gaussian Blur 2",), font_records=None):
    if font_records is None:
        font_records = tuple(AEFont(match_name=name, version_or_hash="1") for name in fonts)
    else:
        font_records = tuple(font_records)
    effect_records = tuple(
        AEEffect(
            match_name=name,
            properties={
                "ADBE Gaussian Blur 2-0001": "number"
            }
            if name == "ADBE Gaussian Blur 2"
            else {"ADBE Fill-0002": "color"}
            if name == "ADBE Fill"
            else {},
        )
        for name in effects
    )
    return AECapabilities(
        version="24.0",
        major=24,
        host="after-effects",
        ready=True,
        project_open=True,
        capabilities=AECapabilityCatalog(
            font_names=fonts,
            fonts=font_records,
            effect_names=effects,
            effects=effect_records,
        ),
    )


def _element(eid, kind, *, texture=None, text=None, font=None, color=None, tracks=None):
    return Element(
        id=eid,
        kind=kind,
        canonical=Canonical(
            width=100,
            height=50,
            texture=texture,
            text=text,
            font=font,
            color=color,
        ),
        visible=(0, 9),
        tracks=tracks or {},
    )


def _scene(*elements, background=None, size=(1920, 1080), fps=30, frames=10, groups=None):
    return Scene(
        id="scene-1",
        size=size,
        fps=fps,
        frames=frames,
        background=background or Background(),
        elements=list(elements),
        groups=list(groups or ()),
    )


def test_exact_font_is_supported_but_unavailable_font_is_an_issue():
    supported = _scene(
        _element("title", "text", text="Launch", font=FontGuess(family_guess="Arial"))
    )
    assert analyze_ae_compatibility(supported, _capabilities()) == ()

    unsupported = _scene(
        _element("title", "text", text="Launch", font=FontGuess(family_guess="Papyrus"))
    )
    issues = analyze_ae_compatibility(unsupported, _capabilities())
    assert len(issues) == 1
    assert issues[0].source_element_id == "title"
    assert issues[0].source_type == "text"
    assert issues[0].semantic_key == "font"
    assert "font" in issues[0].lost_semantics


def test_compatibility_rejects_3d_sky_and_missing_texture():
    scene = _scene(
        _element("space", "3d"),
        _element("tilted", "sprite", texture="assets/tilted.png", tracks={"sky": Track(keys=[Keyframe(t=0, v=1)])}),
        _element("sprite", "sprite"),
        _element("ui", "ui"),
    )
    issues = analyze_ae_compatibility(scene, _capabilities())
    assert [(issue.source_element_id, issue.semantic_key) for issue in issues] == [
        ("space", "3d"),
        ("tilted", "sky"),
        ("sprite", "texture"),
        ("ui", "texture"),
    ]

def test_mismatched_scale_tracks_are_a_converter_gap():
    scene = _scene(
        _element(
            "scaled",
            "sprite",
            texture="assets/scaled.png",
            tracks={
                "sx": Track(keys=[Keyframe(t=0, v=1), Keyframe(t=5, v=2)]),
                "sy": Track(keys=[Keyframe(t=0, v=1), Keyframe(t=4, v=2)]),
            },
        )
    )
    issues = analyze_ae_compatibility(scene, _capabilities())
    assert [(issue.source_element_id, issue.semantic_key) for issue in issues] == [("scaled", "scale")]


def test_linear_ease_is_supported_but_endpoint_degenerate_ease_is_a_gap():
    linear = _scene(
        _element(
            "linear",
            "sprite",
            texture="assets/linear.png",
            tracks={"x": Track(keys=[Keyframe(t=0, v=0, ease=(0.0, 0.0, 1.0, 1.0))])},
        )
    )
    assert analyze_ae_compatibility(linear, _capabilities()) == ()

    degenerate = _scene(
        _element(
            "degenerate",
            "sprite",
            texture="assets/degenerate.png",
            tracks={"x": Track(keys=[Keyframe(t=0, v=0, ease=(0.0, 0.2, 0.5, 1.0))])},
        )
    )
    issues = analyze_ae_compatibility(degenerate, _capabilities())
    assert [(issue.source_element_id, issue.semantic_key) for issue in issues] == [
        ("degenerate", "easing")
    ]


def test_image_background_with_a_fixed_asset_reference_is_supported():
    assert analyze_ae_compatibility(
        _scene(background=Background(kind="image", value="assets/background.png")),
        _capabilities(),
    ) == ()
    issues = analyze_ae_compatibility(
        _scene(background=Background(kind="image", value="")),
        _capabilities(),
    )
    assert [(issue.source_element_id, issue.semantic_key) for issue in issues] == [
        ("scene-1", "background")
    ]


@pytest.mark.parametrize("reference", ["../outside.png", "assets/../../outside.png", "/outside.png", "C:\\outside.png", "https://example.com/a.png", "assets/a\n.png"])
def test_image_background_rejects_unsafe_references(reference):
    issues = analyze_ae_compatibility(
        _scene(background=Background(kind="image", value=reference)), _capabilities()
    )
    assert [(issue.source_element_id, issue.semantic_key) for issue in issues] == [("scene-1", "background")]


def test_valid_substitution_proposal_is_bound_to_issue_and_capability():
    issue = analyze_ae_compatibility(
        _scene(_element("space", "3d")),
        _capabilities(),
    )[0]
    proposals = parse_ae_substitutions(
        [
            {
                "source_element_id": issue.source_element_id,
                "source_type": issue.source_type,
                "proposed_layers": [{"layer_type": "null", "name": "space-fallback"}],
                "proposed_effects": ["ADBE Gaussian Blur 2"],
                "lost_semantics": list(issue.lost_semantics),
                "acknowledged": False,
            }
        ],
        (issue,),
        _capabilities(),
    )
    assert len(proposals) == 1
    assert proposals[0].source_element_id == "space"


@pytest.mark.parametrize(
    "payload",
    [
        [],
        [{"source_element_id": "space", "source_type": "text", "proposed_layers": ["null"], "lost_semantics": ["3d"]}],
        [{"source_element_id": "space", "source_type": "3d", "proposed_layers": ["camera"], "lost_semantics": ["3d"]}],
        [{"source_element_id": "space", "source_type": "3d", "proposed_layers": ["null"], "proposed_effects": ["Not Approved"], "lost_semantics": ["3d"]}],
        [{"source_element_id": "space", "source_type": "3d", "proposed_layers": [{"layer_type": "null", "path": "/tmp/escape"}], "lost_semantics": ["3d"]}],
        [{"source_element_id": "space", "source_type": "3d", "proposed_layers": ["null"], "lost_semantics": ["3d"], "acknowledged": True}],
    ],
)
def test_invalid_or_untrusted_substitution_payloads_are_rejected(payload):
    issue = analyze_ae_compatibility(_scene(_element("space", "3d")), _capabilities())[0]
    with pytest.raises((ValidationError, ValueError)):
        parse_ae_substitutions(payload, (issue,), _capabilities())


def test_llm_proposal_makes_one_call_and_does_not_retry_invalid_json():
    issue = analyze_ae_compatibility(_scene(_element("space", "3d")), _capabilities())[0]
    payload = [{
        "source_element_id": "space",
        "source_type": "3d",
        "proposed_layers": ["null"],
        "lost_semantics": list(issue.lost_semantics),
    }]

    class Client:
        def __init__(self, content):
            self.content = content
            self.calls = 0

        def complete(self, messages, tools):
            self.calls += 1
            assert tools == []
            assert json.loads(messages[-1]["content"])["issues"][0]["source_element_id"] == "space"
            return SimpleNamespace(content=self.content)

    client = Client(json.dumps(payload))
    result = propose_ae_substitutions(client, (issue,), _capabilities())
    assert result[0].source_element_id == "space"
    assert client.calls == 1

    bad = Client("not json")
    with pytest.raises(ValueError, match="JSON"):
        propose_ae_substitutions(bad, (issue,), _capabilities())
    assert bad.calls == 1


def _font_capabilities(*records):
    return _capabilities(
        fonts=tuple(record.match_name for record in records),
        font_records=records,
    )


def test_llm_response_must_be_one_stripped_json_value():
    issue = analyze_ae_compatibility(_scene(_element("space", "3d")), _capabilities())[0]
    payload = [{
        "source_element_id": "space",
        "source_type": "3d",
        "proposed_layers": ["null"],
        "lost_semantics": list(issue.lost_semantics),
    }]

    class Client:
        def __init__(self, content):
            self.content = content
            self.calls = 0

        def complete(self, messages, tools):
            self.calls += 1
            return SimpleNamespace(content=self.content)

    for content in (
        f"prose {json.dumps(payload)}",
        f"```json\n{json.dumps(payload)}\n```",
        f"{json.dumps(payload)} {json.dumps(payload)}",
    ):
        client = Client(content)
        with pytest.raises(ValueError, match="JSON"):
            propose_ae_substitutions(client, (issue,), _capabilities())
        assert client.calls == 1


def test_font_resolver_returns_exact_match_name_for_family_and_weight():
    caps = _font_capabilities(
        AEFont(match_name="AcmePS-Bold", family="Acme", style="Bold", version_or_hash="1"),
    )
    assert resolve_ae_font(FontGuess(family_guess="Acme", weight=700), caps) == "AcmePS-Bold"


@pytest.mark.parametrize(("style", "weight"), [("Extra Light", 200), ("Book Italic", 400)])
def test_font_style_weight_aliases_are_resolved_without_family_fallback(style, weight):
    caps = _font_capabilities(
        AEFont(match_name=f"AcmePS-{weight}", family="Acme", style=style, version_or_hash="1"),
    )
    assert resolve_ae_font(FontGuess(family_guess="Acme", weight=weight), caps) == f"AcmePS-{weight}"


@pytest.mark.parametrize(
    ("records", "weight", "reason"),
    [
        (
            (AEFont(match_name="AcmePS-Regular", family="Acme", style="Regular", version_or_hash="1"),),
            700,
            "no exact",
        ),
        (
            (
                AEFont(match_name="AcmePS-Bold", family="Acme", style="Bold", version_or_hash="1"),
                AEFont(match_name="AcmePS-BoldAlt", family="Acme", style="Bold", version_or_hash="2"),
            ),
            700,
            "ambiguous",
        ),
    ],
)
def test_font_weight_mismatch_or_ambiguity_is_a_font_issue(records, weight, reason):
    caps = _font_capabilities(*records)
    scene = _scene(
        _element("title", "text", text="Launch", font=FontGuess(family_guess="Acme", weight=weight))
    )
    issues = analyze_ae_compatibility(scene, caps)
    assert [(issue.source_element_id, issue.semantic_key) for issue in issues] == [("title", "font")]
    assert reason in issues[0].reason
    with pytest.raises(AEFontResolutionError):
        resolve_ae_font(FontGuess(family_guess="Acme", weight=weight), caps)


def test_empty_text_font_family_is_reported_as_a_font_issue():
    scene = _scene(
        _element("title", "text", text="Launch", font=FontGuess.model_construct(family_guess="")),
    )
    issues = analyze_ae_compatibility(scene, _capabilities())
    assert [(issue.source_element_id, issue.semantic_key) for issue in issues] == [("title", "font")]


@pytest.mark.parametrize(
    ("kwargs", "needle"),
    [
        ({"size": (3, 1080)}, "width"),
        ({"size": (1920, 30001)}, "height"),
        ({"fps": 0.5}, "fps"),
        ({"fps": 100}, "fps"),
        ({"fps": 1, "frames": 10801}, "duration"),
        ({"fps": 99, "frames": 1_000_001}, "frame count"),
        ({"fps": 1, "frames": 10**400}, "frame count"),
    ],
)
def test_ae_composition_limits_are_compatibility_issues(kwargs, needle):
    issues = analyze_ae_compatibility(_scene(**kwargs), _capabilities())
    assert len(issues) == 1
    assert issues[0].source_element_id == "scene-1"
    assert issues[0].source_type == "scene"
    assert issues[0].semantic_key == "composition"
    assert needle in issues[0].reason


def test_non_neutral_group_transform_is_a_compatibility_issue():
    scene = _scene(
        _element(
            "group",
            "group",
            tracks={"x": Track(keys=[Keyframe(t=0, v=12.0)])},
        )
    )
    issues = analyze_ae_compatibility(scene, _capabilities())
    assert [(issue.source_element_id, issue.semantic_key) for issue in issues] == [
        ("group", "group_transform")
    ]


def test_missing_proposal_has_an_actionable_issue_error():
    from keepframe.after_effects import compatibility

    scene = _scene(_element("model", "3d", texture="static.png"))
    issue = analyze_ae_compatibility(scene, _capabilities())[0]
    with pytest.raises(ValueError, match="model.*3d.*proposal"):
        compatibility._proposal_for_issue([], issue)


def test_temporal_ease_requires_ae_influences_at_least_point_one_percent():
    scene = _scene(
        _element(
            "animated",
            "sprite",
            texture="assets/animated.png",
            tracks={
                "x": Track(
                    keys=[
                        Keyframe(t=0, v=0, ease=(0.0005, 0.5, 0.9995, 0.5)),
                        Keyframe(t=1, v=1),
                    ]
                )
            },
        )
    )
    issues = analyze_ae_compatibility(scene, _capabilities())
    assert [(issue.source_element_id, issue.semantic_key) for issue in issues] == [
        ("animated", "easing")
    ]
    assert "influence" in issues[0].reason


def test_temporal_ease_speed_uses_scene_fps_and_frame_delta():
    scene = _scene(
        _element(
            "animated",
            "sprite",
            texture="assets/animated.png",
            tracks={
                "x": Track(
                    keys=[
                        Keyframe(t=0, v=0, ease=(0.5, 1.0, 0.5, 0.0)),
                        Keyframe(t=1, v=1_000_000),
                    ]
                )
            },
        ),
        fps=30,
    )
    issues = analyze_ae_compatibility(scene, _capabilities())
    assert [(issue.source_element_id, issue.semantic_key) for issue in issues] == [
        ("animated", "easing")
    ]
    assert "speed" in issues[0].reason


def test_temporal_ease_speed_matches_mapper_scale_units():
    scene = _scene(
        _element(
            "scaled",
            "sprite",
            texture="assets/scaled.png",
            tracks={
                "sx": Track(
                    keys=[
                        Keyframe(t=0, v=0, ease=(0.5, 0.5, 0.5, 0.5)),
                        Keyframe(t=1, v=10_000),
                    ]
                )
            },
        )
    )
    issues = analyze_ae_compatibility(scene, _capabilities())
    assert [(issue.source_element_id, issue.semantic_key) for issue in issues] == [
        ("scaled", "easing")
    ]
    assert "speed" in issues[0].reason


def test_non_opaque_source_colors_are_not_declared_ae_compatible():
    text = _element(
        "title",
        "text",
        text="Launch",
        font=FontGuess(family_guess="Arial"),
        color="#11223380",
    )
    scene = _scene(text, background=Background(kind="color", value="#44556680"))
    issues = analyze_ae_compatibility(scene, _capabilities())
    assert [(issue.source_element_id, issue.semantic_key) for issue in issues] == [
        ("scene-1", "alpha"),
        ("title", "alpha"),
    ]


def test_text_substitution_requires_an_exact_approved_font_match_name():
    issue = analyze_ae_compatibility(_scene(_element("space", "3d")), _capabilities())[0]
    caps = _font_capabilities(
        AEFont(match_name="AcmePS-Regular", family="Acme", style="Regular", version_or_hash="1"),
    )
    base = {
        "source_element_id": issue.source_element_id,
        "source_type": issue.source_type,
        "lost_semantics": list(issue.lost_semantics),
        "acknowledged": False,
    }
    with pytest.raises(ValueError, match="font"):
        parse_ae_substitutions(
            [{**base, "proposed_layers": [{"layer_type": "text", "name": "fallback"}]}],
            (issue,),
            caps,
        )
    with pytest.raises(ValueError, match="approved"):
        parse_ae_substitutions(
            [{
                **base,
                "proposed_layers": [
                    {"layer_type": "text", "name": "fallback", "font_name": "Papyrus"}
                ],
            }],
            (issue,),
            caps,
        )
    proposals = parse_ae_substitutions(
        [{
            **base,
            "proposed_layers": [
                {"layer_type": "text", "name": "fallback", "font_name": "AcmePS-Regular"}
            ],
        }],
        (issue,),
        caps,
    )
    assert proposals[0].proposed_layers == (
        {"layer_type": "text", "name": "fallback", "font_name": "AcmePS-Regular"},
    )


def test_non_opaque_solid_substitution_color_is_rejected():
    issue = analyze_ae_compatibility(_scene(_element("space", "3d")), _capabilities())[0]
    payload = {
        "source_element_id": issue.source_element_id,
        "source_type": issue.source_type,
        "proposed_layers": [
            {"layer_type": "solid", "name": "fallback", "color": "#11223380"}
        ],
        "lost_semantics": list(issue.lost_semantics),
        "acknowledged": False,
    }
    with pytest.raises(ValueError, match="alpha"):
        parse_ae_substitutions([payload], (issue,), _capabilities())


def test_opaque_alpha_substitution_layer_color_is_canonicalized():
    issue = analyze_ae_compatibility(_scene(_element("space", "3d")), _capabilities())[0]
    payload = {
        "source_element_id": issue.source_element_id,
        "source_type": issue.source_type,
        "proposed_layers": [
            {"layer_type": "solid", "name": "fallback", "color": "#aBcDeFfF"}
        ],
        "lost_semantics": list(issue.lost_semantics),
        "acknowledged": False,
    }

    proposals = parse_ae_substitutions([payload], (issue,), _capabilities())

    assert proposals[0].proposed_layers == (
        {"layer_type": "solid", "name": "fallback", "color": "#ABCDEF"},
    )


def test_substitution_effect_color_lists_are_rgb_only():
    issue = analyze_ae_compatibility(_scene(_element("space", "3d")), _capabilities())[0]
    caps = _capabilities(effects=("ADBE Fill",))
    base = {
        "source_element_id": issue.source_element_id,
        "source_type": issue.source_type,
        "lost_semantics": list(issue.lost_semantics),
        "acknowledged": False,
    }
    payload = {
        **base,
        "proposed_layers": ["null"],
        "proposed_effects": [
            {
                "effect_name": "ADBE Fill",
                "properties": {"ADBE Fill-0002": [0.1, 0.2, 0.3]},
            }
        ],
    }

    proposals = parse_ae_substitutions([payload], (issue,), caps)

    assert proposals[0].proposed_effects == (
        {
            "effect_name": "ADBE Fill",
            "properties": {"ADBE Fill-0002": [0.1, 0.2, 0.3]},
        },
    )

    with pytest.raises(ValueError, match="three"):
        parse_ae_substitutions(
            [
                {
                    **base,
                    "proposed_layers": ["null"],
                    "proposed_effects": [
                        {
                            "effect_name": "ADBE Fill",
                            "properties": {"ADBE Fill-0002": [0.1, 0.2, 0.3, 1.0]},
                        }
                    ],
                }
            ],
            (issue,),
            caps,
        )
