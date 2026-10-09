"""Font render-and-compare (Task 10): prefilter, size / weight / tracking / shear fit, top 3 with confidence, the
Hangul fallback, uploaded fonts and speed."""
import hashlib, json, statistics, time
import numpy as np, pytest
from keepframe.analyze.textstyle import cap_height, glyph_centres, stroke_width
from keepframe.fonts.match import FontFit, fit_family, fit_hangul_fallback, match_font, prefilter
from keepframe.fonts.raster import first_baseline, glyph_alpha
from keepframe.fonts.registry import FontRegistry
from keepframe.ir.synth import make_font_sample

REG = FontRegistry()
N = 30


def _inputs(alpha, text):
    """What the style phase hands the matcher (Task 9's measures on the same alpha)."""
    sw, cap = stroke_width(alpha), cap_height(alpha, max(1, text.count("\n") + 1))
    return {"stroke_ratio": sw / cap if cap > 0 and sw > 0 else None, "centres": glyph_centres(alpha, text)}


def _iou(a, b):
    return float(np.minimum(a, b).sum() / max(np.maximum(a, b).sum(), 1e-9))


@pytest.fixture(scope="module")
def synthetic_set():
    out = []
    for seed in range(1, N + 1):
        s = make_font_sample(seed)
        fits, conf = match_font(s.alpha, s.text, REG, **_inputs(s.alpha, s.text))
        out.append((s, fits, conf))
    return out


def test_font_sample_is_seeded_and_bundled():
    a, b = make_font_sample(7), make_font_sample(7)
    assert (a.family, a.weight, a.size_px, a.tracking_em, a.shear_deg, a.text) == (b.family, b.weight, b.size_px, b.tracking_em, b.shear_deg, b.text)
    np.testing.assert_array_equal(a.alpha, b.alpha)
    assert a.family in REG.families() and a.alpha.dtype == np.float32 and a.alpha.max() > 0.9
    families = {make_font_sample(s).family for s in range(1, 41)}
    assert len(families) >= 20


def test_font_top3_on_synthetic_set(synthetic_set):
    miss = [(s.seed, s.family, [f.family for f in fits]) for s, fits, _ in synthetic_set
            if s.family not in [f.family for f in fits[:3]]]
    assert (N - len(miss)) / N >= 0.90, miss
    for _, fits, conf in synthetic_set:
        assert len(fits) == 3 and all(isinstance(f, FontFit) for f in fits)
        assert fits[0].score >= fits[1].score >= fits[2].score and conf in (0.5, 1.0)


def test_weight_size_tracking_accuracy(synthetic_set):
    weight, size, tracking = [], [], []
    for s, fits, _ in synthetic_set:
        fit = next((f for f in fits if f.family == s.family), None) or fit_family(s.alpha, s.text, s.family, REG, **_inputs(s.alpha, s.text))
        weight.append(abs(fit.weight - s.weight) <= 100)
        size.append(abs(fit.size_px / s.size_px - 1) <= 0.03)
        tracking.append(abs(fit.tracking_em - s.tracking_em) <= 0.02)
    assert np.mean(weight) >= 0.8 and np.mean(size) >= 0.8 and np.mean(tracking) >= 0.8, (np.mean(weight), np.mean(size), np.mean(tracking))


def test_fitted_layout_redraws_the_observed_glyphs(synthetic_set):
    """dx / dy / size / tracking / shear place the production raster's glyphs on the observed ones (the box is the
    observed array, as for a texture)."""
    good = []
    for s, fits, _ in synthetic_set[:12]:
        fit = fits[0]
        face = REG.face(fit.family, fit.weight)
        h, w = s.alpha.shape
        redraw = glyph_alpha([s.text], fit.size_px, [face], weight=fit.weight, tracking_em=fit.tracking_em,
                             shear_deg=fit.shear_deg, box=(w, h), dx=fit.dx, dy=fit.dy)
        good.append(_iou(redraw, s.alpha) >= 0.7)   # native resolution: thin small text is less forgiving than 64 px
        if fit.family == s.family:
            base = fit.dy + first_baseline(face, fit.size_px, float(h))
            assert abs(fit.dx - s.origin[0]) <= 1.5 and abs(base - s.origin[1]) <= 1.5, (s.seed, fit, s.origin)
    assert np.mean(good) >= 0.9


def test_shear_found_only_when_slanted():
    slanted = make_font_sample(3, family="Inter", weight=600, text="Night Market", shear_deg=10.0, tracking_em=0.0)
    upright = make_font_sample(3, family="Inter", weight=600, text="Night Market", shear_deg=0.0, tracking_em=0.0)
    a = fit_family(slanted.alpha, slanted.text, "Inter", REG, **_inputs(slanted.alpha, slanted.text))
    b = fit_family(upright.alpha, upright.text, "Inter", REG, **_inputs(upright.alpha, upright.text))
    assert abs(a.shear_deg - 10.0) <= 2.0 and b.shear_deg == 0.0


def test_tracking_without_centres_uses_the_search():
    s = make_font_sample(5, family="Montserrat", weight=500, text="Grand Opening", tracking_em=0.08, shear_deg=0.0)
    fit = fit_family(s.alpha, s.text, "Montserrat", REG, stroke_ratio=_inputs(s.alpha, s.text)["stroke_ratio"], centres=None)
    assert abs(fit.tracking_em - 0.08) <= 0.02 and abs(fit.weight - 500) <= 100


def test_prefilter_keeps_the_true_family(synthetic_set):
    kept = [s.family in prefilter(s.alpha, s.text, REG, k=8, **_inputs(s.alpha, s.text)) for s, _, _ in synthetic_set[:15]]
    assert np.mean(kept) >= 0.9
    first = synthetic_set[0][0]
    assert len(prefilter(first.alpha, first.text, REG, k=8, **_inputs(first.alpha, first.text))) == 8


def test_prefilter_sizes_from_the_cap_height():
    """Montserrat 800's i-dot reaches higher than at the prefilter's fixed weight: sized from the ink height, the
    size comes out 10 % large, no glyph position fits and the family drops out of the eight."""
    s = make_font_sample(103, family="Montserrat", weight=800, text="Summer Lines", size_px=41.0, tracking_em=0.025,
                         shear_deg=0.0)
    assert "Montserrat" in prefilter(s.alpha, s.text, REG, k=8, **_inputs(s.alpha, s.text))


def _upload(root, family, data: bytes, ext: str, **meta):
    fonts = root / "fonts"
    fonts.mkdir(parents=True, exist_ok=True)
    sha = hashlib.sha256(data).hexdigest()
    (fonts / f"{sha}.{ext}").write_bytes(data)
    index = fonts / "index.json"
    items = json.loads(index.read_text())["fonts"] if index.is_file() else []
    items.append({"file": f"{sha}.{ext}", "family": family, "sha256": sha, "weight_range": [400, 400], "latin": True, **meta})
    index.write_text(json.dumps({"fonts": items}))


def _wide_copy(face, factor: float) -> bytes:
    """A distinct 'brand' font: the bundled outlines stretched horizontally."""
    import io
    from fontTools.pens.transformPen import TransformPen
    from fontTools.pens.ttGlyphPen import TTGlyphPen
    from fontTools.ttLib import TTFont
    font = TTFont(str(face.path))
    font.flavor = None
    gs = font.getGlyphSet()
    glyf, hmtx = font["glyf"], font["hmtx"]
    new = {}
    for name in font.getGlyphOrder():
        pen = TTGlyphPen(gs)
        gs[name].draw(TransformPen(pen, (factor, 0, 0, 1, 0, 0)))
        new[name] = pen.glyph()
    for name, g in new.items():
        glyf[name] = g
        adv, lsb = hmtx[name]
        hmtx[name] = (int(round(adv * factor)), int(round(lsb * factor)))
    for tag in ("GPOS", "kern"):
        if tag in font:
            del font[tag]
    buf = io.BytesIO()
    font.save(buf)
    return buf.getvalue()


def test_uploaded_font_joins_and_wins(tmp_path):
    root = tmp_path / "proj"
    _upload(root, "Brand Wide", _wide_copy(REG.face("Varela Round"), 1.3), "ttf", category="rounded_sans")
    reg = FontRegistry.for_project(root)
    face = reg.face("Brand Wide")
    assert face is not None and face.source == "uploaded"
    a = glyph_alpha(["Brand Day 26"], 46, [face], weight=400, pad=5)
    assert "Brand Wide" in prefilter(a, "Brand Day 26", reg, k=8)
    fits, _ = match_font(a, "Brand Day 26", reg, **_inputs(a, "Brand Day 26"))
    assert fits[0].family == "Brand Wide" and fits[0].score >= 0.85
    assert "Brand Wide" not in [f.family for f in match_font(a, "Brand Day 26", REG, **_inputs(a, "Brand Day 26"))[0]]


def test_style_phase_matches_against_the_project_fonts(tmp_path):
    from keepframe.analyze.pipeline import _font_registry
    root = tmp_path / "proj"
    _upload(root, "Brand Wide", _wide_copy(REG.face("Varela Round"), 1.3), "ttf", category="rounded_sans")
    assert "Brand Wide" in _font_registry(root / "scenes" / "s1").families()
    assert "Brand Wide" not in _font_registry(tmp_path / "elsewhere").families()


def test_low_confidence_on_tie(tmp_path):
    lobster = REG.face("Lobster")
    a = glyph_alpha(["Grand Opening"], 50, [lobster], weight=400, pad=5)
    fits, conf = match_font(a, "Grand Opening", REG, **_inputs(a, "Grand Opening"))
    assert fits[0].family == "Lobster" and fits[0].score >= 0.8 and conf == 1.0
    root = tmp_path / "proj"
    _upload(root, "Lobster Copy", lobster.path.read_bytes(), "woff2", category="script")
    fits, conf = match_font(a, "Grand Opening", FontRegistry.for_project(root), **_inputs(a, "Grand Opening"))
    assert {f.family for f in fits[:2]} == {"Lobster", "Lobster Copy"}
    assert abs(fits[0].score - fits[1].score) < 0.02 and conf == 0.5


@pytest.mark.parametrize("truth_scale", [0.89, 0.95])   # both 0.03 from the default 0.92: the scale is fitted
def test_hangul_fallback_inter_bold_to_pretendard_700(truth_scale):
    text = "Big Sale 지금 시작"
    inter, pretendard = REG.face("Inter", 700), REG.face("Pretendard", 700)
    a = glyph_alpha([text], 48, [inter, pretendard], weight=700, fallback_weight=700, fallback_scale=truth_scale, pad=5)
    fits, _ = match_font(a, text, REG, **_inputs(a, text))
    assert fits[0].family == "Inter", [f.family for f in fits]
    best = fits[0]
    assert abs(best.weight - 700) <= 100 and abs(best.size_px / 48 - 1) <= 0.03
    family, weight, scale = fit_hangul_fallback(best, a, text, REG)
    assert (family, weight) == ("Pretendard", 700)
    assert 0.88 <= scale <= 0.96 and abs(scale - truth_scale) <= 0.015


def test_serif_maps_to_noto_serif_kr():
    text = "Grand 오픈 Opening"
    serif, kr = REG.face("Playfair Display", 400), REG.face("Noto Serif KR", 400)
    a = glyph_alpha([text], 44, [serif, kr], weight=400, fallback_weight=400, fallback_scale=0.9, pad=5)
    fits, _ = match_font(a, text, REG, **_inputs(a, text))
    best = fits[0]
    assert REG.face(best.family).category == "serif" and not REG.face(best.family).hangul
    family, weight, scale = fit_hangul_fallback(best, a, text, REG)
    assert family == "Noto Serif KR" and 300 <= weight <= 500 and 0.84 <= scale <= 0.96


def test_hangul_only_text_matches_hangul_families():
    text = "지금 시작하기"
    a = glyph_alpha([text], 40, [REG.face("Pretendard", 600)], weight=600, pad=5)
    fits, _ = match_font(a, text, REG, **_inputs(a, text))
    assert fits[0].family == "Pretendard" and all(REG.face(f.family).hangul for f in fits)
    assert fit_hangul_fallback(fits[0], a, text, REG) == (None, None, 1.0)


def test_two_line_text_matches_and_redraws():
    """Lines sit one box-height / n apart (the raster's line box): stage 1 sizes from the first line's top to the
    last line's bottom, and a fit starts where the prefilter left it."""
    lines, box = ["Big", "Sale"], (150, 130)
    text = "\n".join(lines)
    a = glyph_alpha(lines, 48, [REG.face("Inter", 700)], weight=700, box=box, dx=6)
    fits, _ = match_font(a, text, REG, **_inputs(a, text))
    assert "Inter" in [f.family for f in fits], [f.family for f in fits]
    fit = fit_family(a, text, "Inter", REG, **_inputs(a, text))
    assert abs(fit.weight - 700) <= 100 and abs(fit.size_px / 48 - 1) <= 0.03
    redraw = glyph_alpha(lines, fit.size_px, [REG.face("Inter", fit.weight)], weight=fit.weight, tracking_em=fit.tracking_em,
                         shear_deg=fit.shear_deg, box=box, dx=fit.dx, dy=fit.dy)
    assert _iou(redraw, a) >= 0.8


def test_long_text_is_capped_to_the_longest_words():
    text = "Join us for the grand opening this Friday"
    s = make_font_sample(11, family="DM Sans", weight=500, text=text, tracking_em=0.0, shear_deg=0.0)
    t0 = time.perf_counter()
    fits, _ = match_font(s.alpha, text, REG, **_inputs(s.alpha, text))
    assert time.perf_counter() - t0 < 6.0
    fit = next(f for f in fits if f.family == "DM Sans")
    assert abs(fit.size_px / s.size_px - 1) <= 0.03 and abs(fit.dx - s.origin[0]) <= 2.0


def test_empty_alpha_is_an_error():
    with pytest.raises(ValueError):
        match_font(np.zeros((20, 40), np.float32), "Sale", REG)
    with pytest.raises(ValueError):
        match_font(np.ones((20, 40), np.float32), "   ", REG)


def test_match_runtime():
    texts = ["Night Market", "Best of 2026", "Grand Finale", "Summer Lines", "Weekend Away", "Open Studios", "City Lights!"]
    secs = []
    for i, text in enumerate(texts):
        s = make_font_sample(100 + i, text=text)
        inputs = _inputs(s.alpha, text)
        t0 = time.perf_counter()
        match_font(s.alpha, text, REG, **inputs)
        secs.append(time.perf_counter() - t0)
    assert statistics.median(secs) <= 1.0, secs


# --- the scene: serial, deterministic, capped by work (fix round 1, R43 / R44) -------------------------------------

WORDS = ("Night", "Market", "Best", "Sale", "Grand", "Open", "City", "Lights", "Summer", "Fresh", "Coming", "Soon",
         "Last", "Chance", "Studio", "Away", "Motion", "Hello", "Big", "Launch", "Free", "Week", "Arts", "Show")


def _texts(n: int) -> list[str]:
    """n distinct 12-character texts: two words and a number."""
    out = []
    for i in range(10 * n):
        a, b = WORDS[i % len(WORDS)], WORDS[(i * 7 + 3) % len(WORDS)]
        t = f"{a} {b} {i:03d}"[:12].rstrip()
        if len(t) == 12 and t not in out:
            out.append(t)
        if len(out) == n:
            return out
    raise AssertionError("not enough texts")


def _jobs(n: int, seed0: int = 600):
    from keepframe.fonts.match import TextJob
    jobs = []
    for i, text in enumerate(_texts(n)):
        s = make_font_sample(seed0 + i, text=text)
        jobs.append(TextJob(f"e{i}", s.alpha, s.text, **_inputs(s.alpha, s.text)))
    return jobs


def test_scene_of_44_texts_within_budget(tmp_path):
    """44 distinct 12-character texts through the style phase's path (`font_guesses`) in a new process — imports
    excluded, first match's cold start included — with the per-font data a previous process left on disk (the
    steady state: it is measured once per font file)."""
    import pickle, subprocess, sys
    from keepframe.fonts import match as M
    jobs = _jobs(44)
    for job in jobs:   # a previous process's prefilter data: stage 1 over every eligible family
        obs = M._observe(job.alpha, job.text, job.stroke_ratio, job.centres)
        for name in M._eligible(REG, obs.chars):
            M._stage1(obs, M._Family(name, REG, obs.chars))
    M.flush_cache()
    (tmp_path / "jobs.pkl").write_bytes(pickle.dumps([(j.key, j.alpha, j.text, j.stroke_ratio, j.centres) for j in jobs]))
    script = (
        "import json, pickle, sys, time\n"
        "from keepframe.fonts.match import TextJob, font_guesses\n"
        "from keepframe.fonts.registry import FontRegistry\n"
        "jobs = [TextJob(*j) for j in pickle.loads(open(sys.argv[1], 'rb').read())]\n"
        "t0 = time.perf_counter()\n"
        "out = font_guesses(jobs, FontRegistry())\n"
        "print(json.dumps({'seconds': time.perf_counter() - t0, 'status': [out[j.key][0] for j in jobs]}))\n")
    run = subprocess.run([sys.executable, "-c", script, str(tmp_path / "jobs.pkl")], capture_output=True, text=True,
                         timeout=300)
    assert run.returncode == 0, run.stderr[-2000:]
    res = json.loads(run.stdout.strip().splitlines()[-1])
    print(f"44 texts: {res['seconds']:.1f} s")
    assert res["seconds"] <= 20.0, res["seconds"]
    assert res["status"].count("ok") == 44, res["status"]


def _same(a, b):
    ga, la = a[1], a[2]
    gb, lb = b[1], b[2]
    return (ga.model_dump(), la) == (gb.model_dump(), lb)


def test_pipeline_path_equals_serial_and_threads_change_nothing():
    """The style phase's path (`font_guesses`), one text at a time, and the old pipeline's way (a 4-thread pool,
    other matches competing for the GIL and FreeType) give identical candidates, scores and layouts: the search
    never depends on how long anything took."""
    from concurrent.futures import ThreadPoolExecutor
    from keepframe.fonts.match import font_guess, font_guesses
    jobs = _jobs(8, seed0=700)

    def one(job):
        return ("ok", *font_guess(job.alpha, job.text, REG, stroke_ratio=job.stroke_ratio, centres=job.centres))

    serial = {job.key: one(job) for job in jobs}
    scene = font_guesses(jobs, REG)
    with ThreadPoolExecutor(4) as ex:
        threaded = dict(zip([j.key for j in jobs], ex.map(one, jobs)))
    for job in jobs:
        assert scene[job.key][0] == "ok"
        assert _same(scene[job.key], serial[job.key]), job.key
        assert _same(threaded[job.key], serial[job.key]), job.key
    assert all(len(serial[j.key][1].candidates) == 3 for j in jobs)


def test_scene_work_cap_keeps_first_guesses_in_cap_height_order():
    """Past the cap (counted candidate renders, never time) the remaining texts are reported skipped, largest cap
    height first."""
    from keepframe.fonts.match import TextJob, font_guesses
    big = make_font_sample(801, text="Grand Opening", size_px=64.0)
    small = make_font_sample(802, text="This Friday", size_px=28.0)
    mid = make_font_sample(803, text="Last Chance", size_px=44.0)
    jobs = [TextJob(k, s.alpha, s.text, **_inputs(s.alpha, s.text)) for k, s in (("a", small), ("b", big), ("c", mid))]
    out = font_guesses(jobs, REG, max_renders=1)
    assert out["b"][0] == "ok" and out["a"][0] == out["c"][0] == "skipped"
    assert "work cap" in out["a"][1]
    assert [v[0] for v in font_guesses(jobs, REG, max_renders=0).values()] == ["skipped"] * 3


def test_repeated_text_takes_the_first_match():
    """A text repeated in the scene (e.g. one title tracked twice) is fitted on its own coverage with the first
    match's family, not matched again."""
    from keepframe.fonts import match as M
    a = make_font_sample(811, family="Montserrat", weight=700, text="Big Launch", size_px=56.0, tracking_em=0.0,
                         shear_deg=0.0)
    b = make_font_sample(811, family="Montserrat", weight=700, text="Big Launch", size_px=40.0, tracking_em=0.0,
                         shear_deg=0.0)
    jobs = [M.TextJob(k, s.alpha, s.text, **_inputs(s.alpha, s.text)) for k, s in (("a", a), ("b", b))]
    calls = []
    orig = M.font_guess
    try:
        M.font_guess = lambda *x, **k: calls.append(1) or orig(*x, **k)
        out = M.font_guesses(jobs, REG)
    finally:
        M.font_guess = orig
    assert len(calls) == 1
    ga, gb = out["a"][1], out["b"][1]
    assert gb.family_guess == ga.family_guess == "Montserrat" and gb.candidates == ga.candidates
    assert abs(gb.size_px / 40 - 1) <= 0.03 and abs(ga.size_px / 56 - 1) <= 0.03


def test_weight_search_evaluates_each_weight_once(monkeypatch):
    """R44: a weight already in a round's cache is not drawn again — here the golden section also leaves the
    weights one step past each end, which the search's edge extension then asks for."""
    from keepframe.fonts import match as M
    seen, rounds = [], []
    orig_at, orig_round, orig_golden = M._Search._at_weight, M._Search.round, M._golden

    def golden(fn, a, b, n, cache, step=5.0, key=None):
        best = orig_golden(fn, a, b, n, cache, step, key)
        for x in (a - 100, b + 100):
            k = key(x) if key is not None else x
            if k not in cache:
                cache[k] = fn(k)
        return best

    monkeypatch.setattr(M, "_golden", golden)

    def at(self, w):
        seen.append(w)
        return orig_at(self, w)

    def rnd(self, r):
        seen.clear()
        try:
            return orig_round(self, r)
        finally:
            rounds.append(list(seen))

    monkeypatch.setattr(M._Search, "_at_weight", at)
    monkeypatch.setattr(M._Search, "round", rnd)
    for seed in (2, 4, 9):
        s = make_font_sample(seed)
        match_font(s.alpha, s.text, REG, **_inputs(s.alpha, s.text))
    assert rounds and all(len(r) == len(set(r)) for r in rounds), [r for r in rounds if len(r) != len(set(r))]


def test_failed_second_round_keeps_the_first_rounds_fit(monkeypatch):
    """R44: an exception in a family's second round keeps its first-round fit."""
    from keepframe.fonts import match as M
    s = make_font_sample(5)
    want, _ = match_font(s.alpha, s.text, REG, **_inputs(s.alpha, s.text))
    orig = M._Search.round

    def boom(self, rnd):
        if rnd == 1:
            self.w, self.score = -1.0, -1.0   # half-updated state must not leak into the result
            raise RuntimeError("round 2")
        return orig(self, rnd)

    monkeypatch.setattr(M._Search, "round", boom)
    fits, _ = match_font(s.alpha, s.text, REG, **_inputs(s.alpha, s.text))
    assert len(fits) == 3 and s.family in [f.family for f in fits] and all(f.weight > 0 for f in fits)
