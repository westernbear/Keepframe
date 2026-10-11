import numpy as np
from keepframe.analyze.titles import fragment, group_fragments, partial_ratio

SIZE, FPS, N = (640, 360), 30, 90


def prop(text, x0, w, first=0, last=60, y=100.0, h=40, rgb=(255, 255, 255), kind="text", extra=None, vx=0.0):
    raw = np.full((N, 11), np.nan)
    for f in range(first, last + 1):
        raw[f] = [x0 + w / 2 + vx * f, y, 1, 1, 0, 0, 0, 1, 1, 0, 0]
    canon = np.zeros((h, w, 4), np.uint8)
    canon[...] = (*rgb, 255)
    p = {"raw": raw, "canon": canon, "kind": kind, "first": first, "last": last}
    p["text" if kind == "text" and text else "ocr_text"] = text
    return {**p, **(extra or {})}


def frags(*items):
    out = [fragment(k, p, SIZE) for k, p in items]
    assert all(out)
    return out


def keys(groups):
    return sorted(sorted(f.key for f in g) for g in groups)


def test_partial_ratio():
    assert partial_ratio("st", "You just") == 1.0
    assert partial_ratio("New", "new arrivals") == 1.0
    assert partial_ratio("Arrivals", "Arrival5") >= 0.8
    assert partial_ratio("Hello", "World") < 0.8
    assert partial_ratio("", "abc") == 0.0


def test_suffix_fragments_join_their_line():   # ig3: "You just" read as "You just", "ust", "st"
    g = group_fragments(frags(("t1", prop("You just", 100, 200, 0, 30)), ("t2", prop("ust", 220, 80, 31, 50)),
                              ("s3", prop("st", 260, 40, 51, 60, kind="sprite"))), [], FPS)
    assert keys(g) == [["s3", "t1", "t2"]]


def test_side_by_side_words_join():   # seeds 4/6: "New" / "Arrivals" split by a crossing mover
    f = frags(("t1", prop("New", 100, 80)), ("t2", prop("Arrivals", 190, 140, 5, 60)))
    assert keys(group_fragments(f, [], FPS)) == [["t1", "t2"]]


def test_letter_fragments_join():   # envato1: single-letter shapes plus glyph-sized sprites
    items = [(f"s{i}", prop(c, 100 + 32 * i, 30, 2 * i, 60, kind="sprite")) for i, c in enumerate("Buil")]
    items += [("t9", prop("d", 228, 30)), ("o1", prop("", 262, 30, 10, 60, kind="sprite"))]
    assert len(group_fragments(frags(*items), [], FPS)) == 1


def test_two_lines_stay_separate():
    f = frags(("t1", prop("Build the", 100, 200)), ("t2", prop("future", 100, 140, y=160)))
    assert len(group_fragments(f, [], FPS)) == 2


def test_successive_different_titles_stay_separate():
    f = frags(("t1", prop("Hello", 100, 100, 0, 30)), ("t2", prop("World", 100, 100, 31, 60)))
    assert len(group_fragments(f, [], FPS)) == 2


def test_far_or_large_same_colour_sprite_stays_out():
    word = ("t1", prop("Build", 100, 150))
    far = ("o1", prop("", 100 + 150 + 120, 30, kind="sprite"))
    big = ("o2", prop("", 260, 150, kind="sprite", h=100))
    assert keys(group_fragments(frags(word, far, big), [], FPS)) == [["o1"], ["o2"], ["t1"]]


def test_vlm_strings_split_adjacent_titles():
    f = frags(("t1", prop("New", 100, 80)), ("t2", prop("Arrivals", 190, 140)))
    assert len(group_fragments(f, ["New", "Arrivals"], FPS)) == 2
    assert len(group_fragments(f, ["New Arrivals"], FPS)) == 1
    assert len(group_fragments(f, ["Other\nNew Arrivals"], FPS)) == 1


def test_movers_and_solid_members_are_not_fragments():
    p = prop("x", 0, 50)
    assert fragment("m3", p, SIZE) is None and fragment("solid0", p, SIZE) is None
    assert fragment("o1", {**p, "mover": True}, SIZE) is None
    assert fragment("o1", {**p, "video": "stages/m1.video.webm"}, SIZE) is None
    assert fragment("t1", p, SIZE).text == "x"


def test_sprite_does_not_bridge_separate_texts():
    # "Sale" and "Today" are 70 px apart (> GAP_H*h = 48); the bullet sits 10 px from one and 30 px from the other
    f = frags(("t1", prop("Sale", 100, 80)), ("o1", prop("", 190, 30, kind="sprite")), ("t2", prop("Today", 250, 100)))
    assert keys(group_fragments(f, [], FPS)) == [["o1"], ["t1"], ["t2"]]
    assert keys(group_fragments(f, ["Sale", "Today"], FPS)) == [["o1"], ["t1"], ["t2"]]


def test_real_solid_member_keys_are_not_fragments():
    p = prop("", 0, 30, kind="sprite")
    assert fragment("o7", p, SIZE, members={"o7"}) is None and fragment("o8", p, SIZE, members={"o7"})


def test_concurrent_line_at_same_place_is_not_succession():
    # same place, overlapping in time, but not side by side (gap < 0 passes) -> keep onset gap beyond ONSET_S
    f = frags(("t1", prop("Hello", 100, 100, 0, 40)), ("t2", prop("Hello", 100, 100, 35, 80)))
    assert len(group_fragments(f, [], FPS)) == 2
