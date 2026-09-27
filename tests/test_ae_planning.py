import json
from pathlib import Path
from types import SimpleNamespace

import pytest
from pydantic import ValidationError

from keepframe.after_effects.models import (
    AEEffect,
    AECapabilities,
    AECapabilityCatalog,
    AEFont,
)
from keepframe.after_effects.mapping import AEMappingError
from keepframe.after_effects.planning import (
    AERenderDraft,
    current_operation_manifest,
    prepare_ae_render_plan,
)
from keepframe.ir.schema import (
    Background,
    Canonical,
    Constraint,
    Element,
    FontGuess,
    Keyframe,
    Scene,
    Track,
)
from keepframe.ir.store import init_project, load_scene, save_scene
from keepframe.render.plan import PlanConflict, load_render_plan, load_render_plan_state
from keepframe.session.llm import AssistantReply


def _capabilities(*, fonts=("Arial",)) -> AECapabilities:
    return AECapabilities(
        version="24.0",
        major=24,
        host="after-effects",
        ready=True,
        project_open=True,
        capabilities=AECapabilityCatalog(
            font_names=fonts,
            fonts=tuple(AEFont(match_name=name, version_or_hash="1") for name in fonts),
            effect_names=("ADBE Fill",),
            effects=(AEEffect(match_name="ADBE Fill", properties={"ADBE Fill-0002": "color"}),),
            property_schemas={
                "ADBE Anchor Point": "vec2",
                "ADBE Position": "vec2",
                "ADBE Position X": "number",
                "ADBE Position Y": "number",
                "ADBE Scale": "vec2",
                "ADBE Scale X": "number",
                "ADBE Scale Y": "number",
                "ADBE Rotate Z": "number",
                "ADBE Skew": "number",
                "ADBE Skew Axis": "number",
                "ADBE Opacity": "number",
                "ADBE Fill Color": "color",
            },
        ),
    )


def _project(tmp_path: Path, *, unsupported: bool = False) -> Path:
    root = tmp_path / "p1"
    element = Element(
        id="title",
        kind="text",
        canonical=Canonical(
            width=100,
            height=40,
            text="Hello",
            font=FontGuess(family_guess="Papyrus" if unsupported else "Arial"),
        ),
        visible=(0, 9),
    )
    scene = Scene(
        id="s1",
        size=(1920, 1080),
        fps=30,
        frames=10,
        constraints=[Constraint(pred="intersect(title, title)", keep=True)],
        background=Background(),
        elements=[element],
    )
    init_project(root, {"file": "source.mp4", "fps": 30, "size": [1920, 1080]}, scene)
    (root / "meta.json").write_text(
        json.dumps({"id": "p1", "status": "approved", "version": "v1", "scene": "s1"}),
        encoding="utf-8",
    )
    return root


def _proposal(*, acknowledged: bool = False):
    return {
        "source_element_id": "title",
        "source_type": "text",
        "proposed_layers": [{"layer_type": "text", "name": "title-fallback", "font_name": "Arial"}],
        "lost_semantics": ["font"],
        "acknowledged": acknowledged,
    }


def test_supported_scene_creates_immutable_ae_plan_with_server_manifest(tmp_path):
    root = _project(tmp_path)
    plan = prepare_ae_render_plan(
        root,
        project_id="p1",
        scene_id="s1",
        version_id="v1",
        mode="preview",
        capabilities=_capabilities(),
        client=SimpleNamespace(),
    )

    assert plan.backend == "after_effects"
    assert plan.locked_targets == ("title",)
    assert plan.permitted_operations == current_operation_manifest()
    assert all(item["schema"]["additionalProperties"] is False for item in plan.permitted_operations)
    set_text = next(item for item in plan.permitted_operations if item["kind"] == "set_text")
    assert "font_name" in set_text["schema"]["properties"]
    assert "font" not in set_text["schema"]["properties"]
    assert {item["kind"] for item in plan.permitted_operations} >= {"set_transform", "set_effect"}
    assert plan.effect_schemas == ({"match_name": "ADBE Fill", "properties": {"ADBE Fill-0002": "color"}},)
    assert load_render_plan(root, plan.id) == plan


def test_unsupported_scene_returns_unapprovable_draft_after_one_proposal_call(tmp_path):
    root = _project(tmp_path, unsupported=True)

    class Client:
        calls = 0

        def complete(self, messages, tools):
            self.calls += 1
            return SimpleNamespace(content=json.dumps([_proposal()]))

    client = Client()
    draft = prepare_ae_render_plan(
        root,
        project_id="p1",
        scene_id="s1",
        version_id="v1",
        mode="preview",
        capabilities=_capabilities(),
        client=client,
    )

    assert isinstance(draft, AERenderDraft)
    assert draft.substitutions_acknowledged is False
    assert not hasattr(draft, "id")
    assert client.calls == 1
    assert AERenderDraft.model_validate(draft.model_dump(mode="json")) == draft


def test_acknowledged_proposal_is_revalidated_and_copied_before_plan(tmp_path):
    root = _project(tmp_path, unsupported=True)
    proposal = _proposal()
    plan = prepare_ae_render_plan(
        root,
        project_id="p1",
        scene_id="s1",
        version_id="v1",
        mode="preview",
        capabilities=_capabilities(),
        substitutions=[proposal],
        substitutions_acknowledged=True,
    )

    assert plan.substitutions[0]["acknowledged"] is True
    assert plan.substitutions_acknowledged is True
    with pytest.raises(ValidationError):
        AERenderDraft.model_validate({**plan.model_dump(mode="json"), "id": "bad"})



def test_acknowledged_plan_merges_multiple_issues_for_one_source(tmp_path):
    root = _project(tmp_path)
    scene_path = root / "scenes" / "s1" / "scene.v1.json"
    scene = load_scene(scene_path)
    element = scene.elements[0].model_copy(
        update={
            "kind": "3d",
            "tracks": {"sky": Track(keys=[Keyframe(t=0, v=1)])},
        }
    )
    save_scene(scene.model_copy(update={"elements": [element]}), scene_path)
    capabilities = _capabilities()
    from keepframe.after_effects.compatibility import analyze_ae_compatibility

    issues = analyze_ae_compatibility(load_scene(scene_path), capabilities)
    assert [issue.semantic_key for issue in issues] == ["3d", "sky"]
    proposal_payload = [
        {
            "source_element_id": issue.source_element_id,
            "source_type": issue.source_type,
            "proposed_layers": [{"layer_type": "null", "name": f"fallback-{issue.semantic_key}"}],
            "lost_semantics": list(issue.lost_semantics),
        }
        for issue in issues
    ]

    class Client:
        def complete(self, messages, tools):
            return SimpleNamespace(content=json.dumps(proposal_payload))

    draft = prepare_ae_render_plan(
        root,
        project_id="p1",
        scene_id="s1",
        version_id="v1",
        mode="preview",
        capabilities=capabilities,
        client=Client(),
    )
    assert isinstance(draft, AERenderDraft)
    assert len(draft.substitutions) == 2
    plan = prepare_ae_render_plan(
        root,
        project_id="p1",
        scene_id="s1",
        version_id="v1",
        mode="preview",
        capabilities=capabilities,
        substitutions=draft.substitutions,
        substitutions_acknowledged=True,
    )
    assert len(plan.substitutions) == 1
    assert plan.substitutions[0]["source_element_id"] == "title"
    assert plan.substitutions[0]["lost_semantics"] == ["3d", "sky"]
    from keepframe.after_effects.mapping import map_baseline

    mapped = map_baseline(
        load_scene(scene_path),
        plan.assets,
        capabilities=capabilities,
        substitutions=plan.substitutions,
        locked_source_ids=plan.locked_targets,
    )
    assert any(layer.source_element_id == "title" for layer in mapped.layer_inventory)

def test_merging_substitutions_rebases_explicit_and_bare_effect_targets(tmp_path):
    root = _project(tmp_path)
    scene_path = root / "scenes" / "s1" / "scene.v1.json"
    scene = load_scene(scene_path)
    element = scene.elements[0].model_copy(
        update={
            "kind": "3d",
            "tracks": {"sky": Track(keys=[Keyframe(t=0, v=1)])},
        }
    )
    save_scene(scene.model_copy(update={"elements": [element]}), scene_path)
    capabilities = _capabilities()
    from keepframe.after_effects.compatibility import analyze_ae_compatibility

    issues = analyze_ae_compatibility(load_scene(scene_path), capabilities)
    proposals = [
        {
            "source_element_id": issue.source_element_id,
            "source_type": issue.source_type,
            "proposed_layers": (
                [
                    {"layer_type": "null", "name": "first-null"},
                    {"layer_type": "null", "name": "first-null-2"},
                ]
                if issue.semantic_key == "3d"
                else [{"layer_type": "null", "name": "second-null"}]
            ),
            "proposed_effects": (
                [
                    {"effect_name": "ADBE Fill", "properties": {}, "layer_index": 1},
                    "ADBE Fill",
                ]
                if issue.semantic_key == "3d"
                else [{"effect_name": "ADBE Fill", "properties": {}, "layer_index": 0}]
            ),
            "lost_semantics": list(issue.lost_semantics),
        }
        for issue in issues
    ]
    draft = prepare_ae_render_plan(
        root,
        project_id="p1",
        scene_id="s1",
        version_id="v1",
        mode="preview",
        capabilities=capabilities,
        substitutions=proposals,
        substitutions_acknowledged=True,
    )

    effects = draft.substitutions[0]["proposed_effects"]
    assert effects == [
        {"effect_name": "ADBE Fill", "properties": {}, "layer_index": 1},
        {"effect_name": "ADBE Fill", "properties": {}, "layer_index": 0},
        {"effect_name": "ADBE Fill", "properties": {}, "layer_index": 2},
    ]


def test_acknowledgement_and_predecessor_are_fail_closed(tmp_path):
    root = _project(tmp_path, unsupported=True)
    with pytest.raises(PlanConflict, match="acknowledgement"):
        prepare_ae_render_plan(
            root,
            project_id="p1",
            scene_id="s1",
            version_id="v1",
            mode="preview",
            capabilities=_capabilities(),
            substitutions=[_proposal()],
        )
    supported_root = _project(tmp_path / "supported")
    with pytest.raises(PlanConflict, match="predecessor"):
        prepare_ae_render_plan(
            supported_root,
            project_id="p1",
            scene_id="s1",
            version_id="v1",
            mode="preview",
            capabilities=_capabilities(),
            predecessor_id="missing",
            predecessor_digest=None,
        )


def test_generic_ae_plan_canonicalizes_acknowledged_substitutions_and_locks(tmp_path):
    from keepframe.render.plan import create_render_plan

    root = _project(tmp_path)
    replacement = root / "scenes" / "s1" / "assets" / "replacement.png"
    replacement.parent.mkdir(parents=True, exist_ok=True)
    replacement.write_bytes(b"replacement")
    capabilities = _capabilities()
    manifest = capabilities.model_dump(
        mode="json",
        exclude={"capability_hash", "project_open", "timestamp"},
    )
    substitution = {
        "source_element_id": "title",
        "source_type": "text",
        "proposed_layers": [
            {"layer_type": "footage", "name": "replacement", "texture": "assets/replacement.png"}
        ],
        "lost_semantics": ["font"],
        "acknowledged": True,
    }
    plan = create_render_plan(
        root,
        project_id="p1",
        scene_id="s1",
        version_id="v1",
        backend="after_effects",
        mode="preview",
        locked_targets=["title", "title"],
        capability_hash=capabilities.capability_hash,
        capability_manifest=manifest,
        substitutions=[substitution],
        substitutions_acknowledged=True,
    )
    assert plan.locked_targets == ("title",)
    assert plan.substitutions[0]["acknowledged"] is True
    assert any(asset.project_path == "scenes/s1/assets/replacement.png" for asset in plan.assets)

    with pytest.raises(PlanConflict, match="scene element or group"):
        create_render_plan(
            root,
            project_id="p1",
            scene_id="s1",
            version_id="v1",
            backend="after_effects",
            mode="preview",
            locked_targets=["missing"],
            capability_hash=capabilities.capability_hash,
            capability_manifest=manifest,
        )


def test_successor_requires_and_binds_an_immutable_predecessor(tmp_path):
    root = _project(tmp_path)
    capabilities = _capabilities()
    first = prepare_ae_render_plan(
        root,
        project_id="p1",
        scene_id="s1",
        version_id="v1",
        mode="preview",
        capabilities=capabilities,
    )
    successor = prepare_ae_render_plan(
        root,
        project_id="p1",
        scene_id="s1",
        version_id="v1",
        mode="preview",
        capabilities=capabilities,
        predecessor_id=first.id,
        predecessor_digest=first.digest,
    )
    assert successor.predecessor_id == first.id
    assert successor.predecessor_digest == first.digest
    with pytest.raises(PlanConflict, match="predecessor"):
        prepare_ae_render_plan(
            root,
            project_id="p1",
            scene_id="s1",
            version_id="v1",
            mode="preview",
            capabilities=capabilities,
            predecessor_id=first.id,
            predecessor_digest="0" * 64,
        )


def test_agent_turn_reuses_one_client_and_ignores_model_confirm(tmp_path, monkeypatch):
    from keepframe.web import server as web_server
    from tests.test_web_agent import _post as web_post
    from tests.test_web_server import start

    root = _project(tmp_path)
    scene_id = "s1"
    client = type(
        "Client",
        (),
        {
            "calls": 0,
            "complete": lambda self, messages, tools: (
                setattr(self, "calls", self.calls + 1)
                or AssistantReply(
                    tool_calls=[
                        {
                            "id": "render-1",
                            "name": "render",
                            "arguments": {"backend": "native", "confirm": True},
                        }
                    ]
                )
            ),
        },
    )()
    created = []
    monkeypatch.setattr(web_server, "make_llm", lambda *args, **kwargs: created.append(client) or client)
    server = start(tmp_path)
    try:
        code, body = web_post(
            server,
            "/api/agent",
            {"project": "p1", "scene": scene_id, "message": "render"},
        )
    finally:
        server.shutdown()

    assert code == 200
    assert body["status"] == "pending"
    assert body["results"][0]["needs_confirm"] is True
    plan_id = body["results"][0]["payload"]["render_plan"]["id"]
    assert created == [client]
    assert client.calls == 1
    assert load_render_plan(root, plan_id).backend == "native"
    assert load_render_plan_state(root, plan_id).status == "awaiting_approval"

@pytest.mark.parametrize(
    ("mutate", "reason"),
    [
        (
            lambda scene: scene.model_copy(update={"size": (3, scene.size[1])}),
            "AE composition limits: width/height must be between 4 and 30000",
        ),
        (
            lambda scene: scene.model_copy(
                update={
                    "elements": [
                        *scene.elements,
                        Element(
                            id="group",
                            kind="group",
                            canonical=Canonical(width=10, height=10),
                            visible=(0, scene.frames - 1),
                            tracks={"x": Track(keys=[Keyframe(t=0, v=1)])},
                        ),
                    ]
                }
            ),
            "group transforms must remain neutral AE null transforms",
        ),
        (
            lambda scene: scene.model_copy(
                update={"background": Background(kind="image", value="../outside.png")}
            ),
            "image background has no fixed scene-relative asset mapping",
        ),
    ],
)
def test_non_substitutable_compatibility_issue_fails_before_llm_or_plan(
    tmp_path, mutate, reason
):
    root = _project(tmp_path)
    scene_path = root / "scenes" / "s1" / "scene.v1.json"
    save_scene(mutate(load_scene(scene_path)), scene_path)

    class Client:
        calls = 0

        def complete(self, messages, tools):
            self.calls += 1
            raise AssertionError("hard compatibility issues must not invoke the LLM")

    client = Client()
    with pytest.raises(PlanConflict, match=reason):
        prepare_ae_render_plan(
            root,
            project_id="p1",
            scene_id="s1",
            version_id="v1",
            mode="preview",
            capabilities=_capabilities(),
            client=client,
        )

    assert client.calls == 0
    assert not (root / "renders").exists()


def test_mapper_domain_error_is_a_plan_conflict_before_persistence(tmp_path):
    root = _project(tmp_path)
    scene_path = root / "scenes" / "s1" / "scene.v1.json"
    scene = load_scene(scene_path)
    element = scene.elements[0].model_copy(
        update={"tracks": {"opacity": Track(keys=[Keyframe(t=0, v=2)])}}
    )
    save_scene(scene.model_copy(update={"elements": [element]}), scene_path)

    with pytest.raises(PlanConflict, match="opacity is outside AE bounds") as error:
        prepare_ae_render_plan(
            root,
            project_id="p1",
            scene_id="s1",
            version_id="v1",
            mode="preview",
            capabilities=_capabilities(),
        )
    assert isinstance(error.value.__cause__, AEMappingError)

    assert not (root / "renders").exists()


def test_acknowledged_mapper_infeasible_substitution_is_rejected_before_persistence(tmp_path):
    root = _project(tmp_path)
    scene_path = root / "scenes" / "s1" / "scene.v1.json"
    scene = load_scene(scene_path).model_copy(
        update={"background": Background(kind="color", value="#11223380")}
    )
    save_scene(scene, scene_path)

    with pytest.raises(PlanConflict, match="requires an explicit opaque color"):
        prepare_ae_render_plan(
            root,
            project_id="p1",
            scene_id="s1",
            version_id="v1",
            mode="preview",
            capabilities=_capabilities(),
            substitutions=[
                {
                    "source_element_id": "s1",
                    "source_type": "background",
                    "proposed_layers": [{"layer_type": "solid", "name": "fallback"}],
                    "lost_semantics": ["alpha"],
                }
            ],
            substitutions_acknowledged=True,
        )

    assert not (root / "renders").exists()


def test_acknowledged_nonopaque_background_substitution_remains_feasible(tmp_path):
    root = _project(tmp_path)
    scene_path = root / "scenes" / "s1" / "scene.v1.json"
    scene = load_scene(scene_path).model_copy(
        update={"background": Background(kind="color", value="#11223380")}
    )
    save_scene(scene, scene_path)

    plan = prepare_ae_render_plan(
        root,
        project_id="p1",
        scene_id="s1",
        version_id="v1",
        mode="preview",
        capabilities=_capabilities(),
        substitutions=[
            {
                "source_element_id": "s1",
                "source_type": "background",
                "proposed_layers": [
                    {"layer_type": "solid", "name": "opaque-fallback", "color": "#112233"}
                ],
                "lost_semantics": ["alpha"],
            }
        ],
        substitutions_acknowledged=True,
    )

    assert plan.substitutions[0]["acknowledged"] is True
