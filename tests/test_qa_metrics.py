import cv2, numpy as np
from keepframe.edit.textraster import render_lines
from keepframe.ir.gradient import render_gradient
from keepframe.ir.schema import Canonical, Element, Gradient, GradientStop, Scene, Background
from keepframe.qa.metrics import (alpha_errors, bg_leak_fraction, glyph_colour_delta, halo_ring, leak_correlation,
                                  outside_glyph_delta, plate_residue, smear_score, title_integrity)

W, H = 640, 360


def _plate():
    g = Gradient(angle=120, stops=[GradientStop(offset=0, color="#f3d9b1"), GradientStop(offset=1, color="#c06c84")])
    return render_gradient(g, W, H).astype(np.float32)


def _glyphs(text="SALE", size=72, at=(220, 140)):
    bgra = render_lines([text], size, (255, 255, 255))
    a = np.zeros((H, W), np.float32)
    h, w = bgra.shape[:2]
    a[at[1]:at[1] + h, at[0]:at[0] + w] = bgra[..., 3] / 255.0
    return a


def _over(fg_rgb, alpha, bg):
    return np.asarray(fg_rgb, np.float32) * alpha[..., None] + bg * (1 - alpha[..., None])


def test_plate_residue_clean_vs_smear():
    plate, a = _plate(), _glyphs()
    mask = cv2.dilate((a > 0.02).astype(np.uint8), np.ones((5, 5), np.uint8)).astype(bool)
    clean = plate + np.random.default_rng(0).normal(0, 0.6, plate.shape).astype(np.float32)
    smear = _over((171, 0, 4), 0.8 * a, plate)              # the median plate absorbed a held title
    r_clean, r_smear = plate_residue(clean, mask), plate_residue(smear, mask)
    assert r_clean["mean_de"] < 0.5 and r_clean["residue_fraction"] <= 0.02 and 0.5 <= r_clean["hf_ratio"] <= 2
    assert r_smear["mean_de"] > 8 and r_smear["residue_fraction"] > 0.02 and r_smear["hf_ratio"] > 2
    blurred = clean.copy()
    blurred[mask] = cv2.GaussianBlur(clean, (0, 0), 6)[mask]
    noisy = plate + np.random.default_rng(1).normal(0, 4, plate.shape).astype(np.float32)
    flat_fill = noisy.copy()
    flat_fill[mask] = cv2.GaussianBlur(noisy, (0, 0), 6)[mask]
    assert plate_residue(blurred, mask)["mean_de"] < 0.5
    assert plate_residue(flat_fill, mask)["hf_ratio"] < 0.5   # a smooth fill inside a textured plate
    exclude = np.zeros_like(mask)
    exclude[:, :300] = True                                   # another layer covers the left half
    assert plate_residue(smear, mask, exclude=exclude)["pixels"] < mask.sum()


def test_bg_leak_fraction_flags_binary_crop():
    plate, a = _plate(), _glyphs()
    red = np.array([171, 0, 4], np.float32)
    frame = _over(red, a, plate)
    ys, xs = np.nonzero(a > 0.02)
    crop = np.zeros((H, W, 4), np.float32)                    # binary crop: the whole box is opaque, F = frame
    crop[ys.min():ys.max() + 1, xs.min():xs.max() + 1, :3] = frame[ys.min():ys.max() + 1, xs.min():xs.max() + 1]
    crop[ys.min():ys.max() + 1, xs.min():xs.max() + 1, 3] = 1.0
    clean = np.zeros((H, W, 4), np.float32)
    clean[..., :3] = red
    clean[..., 3] = a
    assert bg_leak_fraction(crop, plate) > 0.2
    assert bg_leak_fraction(clean, plate) < 0.01
    assert leak_correlation(crop, plate) > 0.5 and abs(leak_correlation(clean, plate)) < 0.1
    spill = clean.copy()
    spill[..., :3] = 0.7 * red + 0.3 * plate                  # 30 % of the plate mixed into the colour
    assert abs(leak_correlation(spill, plate) - 0.3) < 0.03 and bg_leak_fraction(spill, plate) < 0.01
    assert leak_correlation(crop, np.full_like(plate, 128)) == 0.0
    as_uint8 = np.dstack([clean[..., :3], clean[..., 3:] * 255]).round().astype(np.uint8)
    assert bg_leak_fraction(as_uint8, plate) < 0.01


def test_halo_ring_flags_white_cutouts():
    navy = np.full((H, W, 3), (26, 42, 108), np.float32)
    disc = np.zeros((H * 4, W * 4), np.uint8)
    cv2.circle(disc, (W * 2, H * 2), 300, 255, -1, cv2.LINE_AA)
    a = cv2.resize(disc, (W, H), interpolation=cv2.INTER_AREA).astype(np.float32) / 255
    ideal = _over((230, 57, 70), a, navy)
    # cut from a white background: soft edge pixels keep white in F and the matte was grown by 2 px
    grown = cv2.dilate(a, np.ones((5, 5), np.uint8))
    f = np.where((a > 0.98)[..., None], np.float32((230, 57, 70)), np.float32((255, 255, 255)))
    halo = _over(f, grown, navy)
    good = ideal + np.random.default_rng(0).normal(0, 0.3, ideal.shape).astype(np.float32)
    assert halo_ring(halo, ideal, a) > 10
    assert halo_ring(good, ideal, a) < 1


def test_title_edit_metrics_flag_smear_and_changed_background():
    plate = _plate()
    old_a, new_a = _glyphs("SALE", at=(220, 140)), _glyphs("FALL", at=(222, 140))
    red = (171, 0, 4)
    src = _over(red, old_a, plate)
    smeared_plate = _over(red, 0.8 * old_a, plate)
    edited_clean, edited_smear = _over(red, new_a, plate), _over(red, new_a, smeared_plate)
    ys, xs = np.nonzero((old_a > 0.02) | (new_a > 0.02))
    box = (xs.min() - 8, ys.min() - 8, xs.max() + 9, ys.max() + 9)
    assert smear_score(edited_clean, old_a, new_a) < 1 and smear_score(edited_smear, old_a, new_a) > 3
    assert outside_glyph_delta(src, src, edited_clean, old_a, new_a, box) < 0.5
    shifted = edited_clean.copy()
    shifted[box[1]:box[3], box[0]:box[2]] += 12                # the box was pasted back with a colour cast
    assert outside_glyph_delta(src, src, shifted, old_a, new_a, box) > 1
    assert glyph_colour_delta(edited_clean, new_a, "#ab0004") < 5
    assert glyph_colour_delta(_over(red, 0.6 * new_a, plate), 0.6 * new_a, "#ab0004") is None   # fading: no solid glyphs
    assert glyph_colour_delta(edited_clean, new_a, "#fb9d9d") > 20


def test_alpha_errors_edge_band():
    a = _glyphs()
    assert alpha_errors(a, a)["sad"] == 0
    hard = (a > 0.5).astype(np.float32)
    grown = cv2.dilate(a, np.ones((3, 3), np.uint8))
    e_hard, e_grown = alpha_errors(hard, a), alpha_errors(grown, a)
    assert 0 < e_hard["sad"] < e_grown["sad"] and e_grown["mse"] > 0


def _scene(*texts, spans=None):
    els = [Element(id=f"e{i}", kind="text", canonical=Canonical(width=10, height=10, text=t),
                   visible=spans[i] if spans else (0, 1)) for i, t in enumerate(texts)]
    return Scene(id="s1", size=(W, H), fps=30, frames=60, background=Background(), elements=els)


def test_title_integrity_whole_vs_fragments():
    gold = [{"text": "A Weekend Away", "role": "title"}, {"text": "Build", "role": "kinetic"}]
    whole = title_integrity(_scene("A WEEKEND  away", "B", "u"), gold)
    assert whole[0]["whole"] and whole[0]["element"] == "e0" and whole[0]["fragments"] == 0 and whole[0]["role"] == "title"
    assert not whole[1]["whole"] and whole[1]["fragments"] == 2
    split = title_integrity(_scene("A Weekend", "Away", "Unrelated words"), gold[:1])
    assert not split[0]["whole"] and split[0]["element"] is None and split[0]["fragments"] == 2
    assert title_integrity(_scene("A Weekend Awaj"), gold[:1])[0]["whole"]           # one misread letter
    assert title_integrity(_scene("ＡＷｅｅｋｅｎｄ Ａｗａｙ"), [{"text": "AWeekend Away"}])[0]["whole"]   # NFKC
    twice = title_integrity(_scene("A Weekend Away", "AWeekend Away", spans=[(8, 13), (10, 59)]), gold[:1])[0]
    assert twice["element"] == "e1" and twice["duplicates"] == 1      # the held copy is the title, the intro copy a duplicate


def test_plate_residue_one_sided_ring_stays_bounded():
    plate = _plate()
    mask = np.zeros((H, W), bool)
    mask[4:50, 590:636] = True                                # a corner logo
    exclude = np.zeros_like(mask)
    exclude[:, :600] = True                                   # other layers leave only a sliver of ring
    exclude[60:, :] = True
    r = plate_residue(plate, mask, exclude=exclude)
    assert r["mean_de"] < 3 and r["fit_degree"] < 2
    assert plate_residue(plate, mask)["fit_degree"] == 2
