import json
import sys

import pytest

from scripts import eval_prompts
from keepframe.ir.schema import Background, Canonical, Element, Keyframe, Scene, Track
from keepframe.ir.store import init_project
from keepframe.session.llm import AssistantReply


@pytest.fixture
def scene():
    return Scene(
        id="s1", size=(200, 200), fps=30, frames=90, background=Background(),
        elements=[
            Element(id="headline", kind="text", visible=(0, 89),
                    canonical=Canonical(width=160, height=30, text="Explore a WEEKEND away — Straße")),
            Element(id="other", kind="text", visible=(0, 89), label="A Weekend Away",
                    canonical=Canonical(width=160, height=30, text="Other title")),
            Element(id="card", kind="sprite", visible=(0, 89),
                    canonical=Canonical(width=40, height=20), tracks={
                        "x": Track(keys=[Keyframe(t=0, v=20), Keyframe(t=36, v=100), Keyframe(t=89, v=160)]),
                        "y": Track(keys=[Keyframe(t=0, v=60)]),
                    }),
        ],
    )


@pytest.mark.parametrize("targets, expected", [
    ([{"element": "headline", "property": "text"}], True),
    ([{"element": "other", "property": "text", "value": "A Weekend Away"}], False),
    ([{"element": "headline", "property": "font"}], False),
    ([{"element": "missing", "property": "text"}], False),
    ([{"property": "text"}], False),
    ([], False),
])
def test_gold_text_matches_original_canonical_text_and_property(scene, targets, expected):
    assert eval_prompts.gold_match(scene, targets, {"property": "text", "text": "A Weekend Away"}) is expected


def test_gold_text_uses_casefold(scene):
    assert eval_prompts.gold_match(scene, [{"element": "headline", "property": "text"}],
                                   {"property": "text", "text": "STRASSE"}) is True


@pytest.mark.parametrize("target, expected", [
    ({"element": "card", "property": "color"}, True),
    ({"element": "other", "property": "color"}, False),
    ({"element": "card", "property": "text"}, False),
    ({"element": "missing", "property": "color"}, False),
    ({"property": "color"}, False),
])
def test_gold_point_matches_bbox_at_time_in_scene_percent(scene, target, expected):
    assert eval_prompts.gold_match(scene, [target], {"property": "color", "at": [50, 30, 1.2]}) is expected


def test_gold_point_uses_rounded_frame_and_includes_bbox_edge(scene):
    scene.fps = 24
    scene.element("card").tracks["x"] = Track(keys=[
        Keyframe(t=28, v=20), Keyframe(t=29, v=120),
    ])
    assert eval_prompts.gold_match(scene, [{"element": "card", "property": "color"}],
                                   {"property": "color", "at": [50, 30, 1.2]}) is True


@pytest.mark.parametrize("target, expected", [
    ({"property": "background"}, True),
    ({"element": None, "property": "background"}, True),
    ({"element": "card", "property": "background"}, False),
    ({"property": "color"}, False),
])
def test_gold_background_requires_scene_target_and_same_property(scene, target, expected):
    assert eval_prompts.gold_match(scene, [target], {"property": "background"}) is expected


def test_gold_timing_can_match_scene_target(scene):
    assert eval_prompts.gold_match(scene, [{"property": "timing", "speed": 1.5}],
                                   {"property": "timing"}) is True


def test_gold_constraints_must_match_the_same_target(scene):
    targets = [{"element": "headline", "property": "text"}, {"element": "card", "property": "color"}]
    assert eval_prompts.gold_match(scene, targets,
                                   {"property": "text", "text": "A Weekend Away", "at": [50, 30, 1.2]}) is False


def test_eval_gold_reports_correct_only_for_successful_typed_matching_targets(scene, tmp_path, monkeypatch, capsys):
    root = tmp_path / "project"
    init_project(root, {"file": "source.mp4", "fps": scene.fps, "size": list(scene.size)}, scene)
    before = {p.relative_to(root): p.read_bytes() for p in root.rglob("*") if p.is_file()}
    expectations = [
        {"property": "text", "text": "A Weekend Away"},
        {"property": "texture", "at": [50, 30, 1.2]},
        {"property": "background"},
        {"property": "timing"},
        {"property": "timing", "text": "A Weekend Away"},
        {"property": "font", "text": "A Weekend Away"},
        {"property": "color", "at": [50, 30, 1.2]},
        {"property": "text", "text": "A Weekend Away"},
    ]
    gold = tmp_path / "gold.json"
    gold.write_text(json.dumps(dict(zip(eval_prompts.PROMPTS, expectations)), ensure_ascii=False))

    def edit(index, target):
        return AssistantReply(tool_calls=[{
            "id": f"call{index}", "name": "edit",
            "arguments": {"prompt": eval_prompts.PROMPTS[index], "targets": [target]},
        }])

    replies = iter([
        edit(0, {"element": "headline", "property": "text", "value": "가을 신상"}),
        edit(1, {"element": "card", "property": "texture", "value": "attachment"}),
        edit(2, {"property": "background", "value": "#001133"}),
        AssistantReply(content="No edit."),
        edit(4, {"element": "card", "property": "timing", "delay": 0.3}),
        edit(5, {"element": "headline", "property": "font", "weight": 700}),
        edit(6, {"element": "card", "property": "color", "value": "invalid"}),
        AssistantReply(content="Invalid color."),
        TimeoutError("provider timeout"),
    ])

    class ScriptedLLM:
        def complete(self, messages, tools):
            reply = next(replies)
            if isinstance(reply, Exception):
                raise reply
            return reply

    monkeypatch.setattr(eval_prompts, "make_llm", ScriptedLLM)
    monkeypatch.setattr(sys, "argv", ["eval_prompts.py", "--project", str(root), "--gold", str(gold)])
    assert eval_prompts.main() == 0
    report = json.loads(capsys.readouterr().out)
    assert report["typed_ok"] == 5
    assert report["correct"] == 4 and report["n"] == 8
    assert [row["correct"] for row in report["rows"]] == [True, True, True, False, False, True, False, False]
    assert all(isinstance(row["correct"], bool) for row in report["rows"])
    assert report["rows"][0]["targets"][0]["value"] == "가을 신상"
    assert report["rows"][4]["ok"] is True
    assert report["rows"][6]["typed"] is True and report["rows"][6]["ok"] is False
    assert "error" in report["rows"][7]
    assert {p.relative_to(root): p.read_bytes() for p in root.rglob("*") if p.is_file()} == before


@pytest.mark.parametrize("gold_data", [None, [], {eval_prompts.PROMPTS[0]: None}])
def test_eval_rejects_invalid_gold_container_before_calling_provider(tmp_path, monkeypatch, capsys, gold_data):
    gold = tmp_path / "gold.json"
    gold.write_text(json.dumps(gold_data))
    monkeypatch.setattr(sys, "argv", ["eval_prompts.py", "--project", str(tmp_path / "missing"), "--gold", str(gold)])
    calls = []
    monkeypatch.setattr(eval_prompts, "make_llm", lambda: calls.append(True))
    with pytest.raises(SystemExit) as exc:
        eval_prompts.main()
    assert exc.value.code == 2
    assert calls == []
    assert "gold" in capsys.readouterr().err.lower()
