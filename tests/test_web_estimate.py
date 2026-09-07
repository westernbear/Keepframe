from keepframe.web.estimate import SECONDS_PER_SCENE, estimate


def test_range_is_one_scene():
    e = estimate("range", frames=90, fps=30)
    assert e["scene_count"] == 1
    assert e["seconds"] == SECONDS_PER_SCENE
    assert e["note"] == "추정. SLA 아님."
    assert len(e["confirm_token"]) == 16


def test_full_uses_four_second_shots():
    e = estimate("full", frames=300, fps=30)  # 10s → 3 scenes
    assert e["scene_count"] == 3
    assert e["seconds"] == 3 * SECONDS_PER_SCENE
