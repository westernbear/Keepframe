"""The tracker's learned same-content matcher (fixes7): fetch media, make labelled region pairs, train, evaluate.

    python scripts/train_assoc.py fetch [--lottie 150] [--saas 40] [--mixkit 20]
    python scripts/train_assoc.py data            # resumable, one npz per clip under eval/assoc/pairs/
    python scripts/train_assoc.py train           # torch (CPU); writes keepframe/analyze/assoc.npz
    python scripts/train_assoc.py eval            # held-out metrics -> docs/qa/assoc/

Everything fetched or rendered lives under eval/ and is never committed (CLAUDE.md §6)."""
from __future__ import annotations
import argparse
import json
import re
import subprocess
import sys
import urllib.request
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
EVAL = ROOT / "eval"
UA = "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/141.0 Safari/537.36"
LOTTIE_QUERIES = ["saas", "app", "product launch", "dashboard", "startup", "technology", "interface", "onboarding",
                  "chart", "notification", "logo reveal", "text animation", "kinetic typography", "intro", "business",
                  "website", "mobile app", "analytics", "payment", "marketing"]
ENVATO_QUERIES = ["saas+promo", "product+launch", "app+promo", "product+promo", "website+promo", "tech+promo",
                  "startup+promo", "software+promo", "app+launch"]
MIXKIT_PAGES = ["titles", "openers", "logo", "slideshow", "social-media", "lower-thirds", "transitions", "business",
                "corporate", "typography", "intro"]
MIXKIT_SEEN = {543, 544, 571, 713, 800}   # eval/refs already


def _get(url: str, data: bytes | None = None, headers: dict | None = None, timeout: int = 60) -> bytes:
    req = urllib.request.Request(url, data=data, headers={"user-agent": UA, **(headers or {})})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read()


def _lottie_ok(doc: dict) -> bool:
    """Renderable here and worth tracking: 1-10 s, at most 2048 px, 2-40 layers, no external images."""
    try:
        frames = float(doc["op"]) - float(doc["ip"])
        external = any(a.get("p") and not str(a["p"]).startswith("data:") and "layers" not in a for a in doc.get("assets", []))
        return 30 <= frames <= 600 and max(doc["w"], doc["h"]) <= 2048 and 2 <= len(doc["layers"]) <= 40 and not external
    except (KeyError, TypeError, ValueError):
        return False


def fetch_lottie(n: int) -> None:
    """Free public animations from LottieFiles (Lottie Simple License), searched by SaaS and motion-design terms."""
    out = EVAL / "assoc" / "lottie"
    out.mkdir(parents=True, exist_ok=True)
    meta = out / "meta.jsonl"
    have = {json.loads(l)["id"] for l in meta.open()} if meta.exists() else set()
    q = "query($q:String!,$after:String){searchPublicAnimations(query:$q,first:50,after:$after){pageInfo{hasNextPage endCursor} edges{node{id name jsonUrl createdBy{username}}}}}"
    for term in LOTTIE_QUERIES:
        after = None
        for _ in range(2):
            if len(have) >= n:
                return
            page = json.loads(_get("https://graphql.lottiefiles.com/2022-08", json.dumps({"query": q, "variables": {"q": term, "after": after}}).encode(),
                                   {"content-type": "application/json"}))["data"]["searchPublicAnimations"]
            for e in page["edges"]:
                node = e["node"]
                if node["id"] in have or not node.get("jsonUrl") or len(have) >= n:
                    continue
                try:
                    raw = _get(node["jsonUrl"])
                    doc = json.loads(raw)
                except Exception as err:   # fail soft: a broken download is skipped
                    print("lottie skip", node["id"], err, file=sys.stderr)
                    continue
                if not _lottie_ok(doc):
                    continue
                (out / f"{node['id']}.json").write_bytes(raw)
                with meta.open("a") as fh:
                    fh.write(json.dumps({"id": node["id"], "name": node["name"], "author": (node.get("createdBy") or {}).get("username"),
                                         "url": node["jsonUrl"], "query": term, "license": "Lottie Simple License (LottieFiles free)"}) + "\n")
                have.add(node["id"])
            if not page["pageInfo"]["hasNextPage"]:
                break
            after = page["pageInfo"]["endCursor"]
    print("lottie", len(have))


def _browser_pages(urls: list[str], grab) -> dict[str, object]:
    from playwright.sync_api import sync_playwright
    found = {}
    with sync_playwright() as p:
        b = p.chromium.launch()
        pg = b.new_page(user_agent=UA)
        for url in urls:
            try:
                found[url] = grab(pg, url)
            except Exception as err:   # fail soft: one page that does not load is skipped
                print("page skip", url, err, file=sys.stderr)
        b.close()
    return found


def fetch_saas(n: int) -> None:
    """SaaS product launch / promo template previews from Envato Elements (paid templates: previews read for training
    and research only, never committed). A search page carries each listed item's hover preview (540p); item pages sit
    behind a bot check this session cannot pass. One preview per item, keyed by its asset id."""
    out = EVAL / "assoc" / "saas"
    out.mkdir(parents=True, exist_ok=True)
    meta = out / "meta.jsonl"
    have = {json.loads(l)["asset"] for l in meta.open() if "asset" in json.loads(l)} if meta.exists() else set()

    def previews(pg, url):
        pg.goto(url, wait_until="domcontentloaded", timeout=60000)
        pg.wait_for_timeout(4000)
        return list(dict.fromkeys(re.findall(r'https://public-assets\.content-platform\.envatousercontent\.com/[^"\'\s]+?/preview_540p_[a-z0-9_]+\.mp4', pg.content())))

    pages = [f"https://elements.envato.com/video-templates/{q}" for q in ENVATO_QUERIES]
    for page, urls in _browser_pages(pages, previews).items():
        for mp4 in urls:
            asset = mp4.split("/")[3]
            if len(have) >= n or asset in have:
                continue
            path = out / f"{asset}.mp4"
            subprocess.run(["curl", "-sS", "-fL", "-o", str(path), mp4], check=False, timeout=300)
            if path.exists() and path.stat().st_size > 50_000:
                with meta.open("a") as fh:
                    fh.write(json.dumps({"asset": asset, "search": page, "preview": mp4,
                                         "license": "Envato Elements preview (paid template; training/research only, not committed)"}) + "\n")
                have.add(asset)
            else:
                path.unlink(missing_ok=True)
    print("saas", len(have))


def fetch_mixkit(n: int) -> None:
    """Free after-effects template previews from Mixkit (Mixkit free licence), for evaluation only (eval/refs2)."""
    out = EVAL / "refs2"
    out.mkdir(parents=True, exist_ok=True)
    have = {int(p.stem[2:]) for p in out.glob("mk*.mp4")}
    ids = []
    for page in MIXKIT_PAGES:
        for n_page in range(1, 5):   # a category lists its templates over several pages
            url = f"https://mixkit.co/free-after-effects-templates/{page}/" + (f"?page={n_page}" if n_page > 1 else "")
            try:
                html = _get(url).decode("utf-8", "ignore")
            except Exception as err:
                print("mixkit skip", url, err, file=sys.stderr)
                break
            before = len(ids)
            for i in re.findall(r"video-templates/(\d+)/", html):   # an id appears several times on its card
                if int(i) not in ids:
                    ids.append(int(i))
            if len(ids) == before:
                break
    for i in [i for i in ids if i not in MIXKIT_SEEN and i not in have][: max(0, n - len(have))]:
        path = out / f"mk{i}.mp4"
        subprocess.run(["curl", "-sS", "-fL", "-o", str(path), f"https://assets.mixkit.co/video-templates/{i}/mixkit-{i}-1080.mp4"],
                       check=False, timeout=300)
        if path.exists() and path.stat().st_size < 50_000:
            path.unlink()
    print("mixkit", len(list(out.glob("mk*.mp4"))))


# ---------------------------------------------------------------- data: regions, owners, labels, pairs

GATE_PX = 240          # tracking.GATE x max_dist: pairs further apart are never scored
LOTTIE_W, LOTTIE_FRAMES, MAX_OWNERS = 640, 120, 16
SAAS_W, SAAS_WINDOW, SAAS_WINDOWS = 640, 150, 3
QUARTER = 0.75         # at most a quarter foreign: the codebase's quarter convention


def clip_regions(frames: np.ndarray, fps: float, work: Path, ocr_box: list):
    """The pipeline's own stages up to the final tracking input, per scene of the clip: [(start, frames, rbf)]."""
    import shutil
    from keepframe.analyze.pipeline import AnalyzeOptions, _plate_stage, _stage_background, _stage_regions, _stage_text, _stage_tracking
    from keepframe.analyze.shots import auto_scenes
    opts = AnalyzeOptions(refine=False, generate_3d=False)
    scenes, _ = auto_scenes(frames)
    out = []
    for sc in scenes:
        a, b = sc["frames"]
        if b - a + 1 < 12:
            continue
        fr = np.ascontiguousarray(frames[a:b + 1])
        sd = work / f"s{a}"
        shutil.rmtree(sd, ignore_errors=True)
        (sd / "stages").mkdir(parents=True)
        (sd / "stages" / "overrides.json").write_text(json.dumps({"regions": [], "ids": {}, "merge": []}))
        bg, bconf, plate = _stage_background(fr, opts, sd)
        built: list = []
        boxes, tt, st, _ = _stage_text(fr, bg, opts, ocr_box[0] if ocr_box else None, sd, None, built=built, fps=fps)
        if built and not ocr_box:
            ocr_box.append(built[0])
        rbf = _stage_regions(fr, bg, boxes, opts, sd, plate=plate, text_tracks=tt, shape_tracks=st)
        obj = _stage_tracking(rbf, sd)
        _, rbf, _ = _plate_stage(fr, fps, rbf, boxes, tt, st, obj, bg, bconf, plate, opts, sd)
        out.append((a, fr, rbf))
        shutil.rmtree(sd, ignore_errors=True)
    return out


def box_owners(truth: dict, f: int, shape, scale: float = 1.0) -> np.ndarray:
    """Pixel owners at frame f from truth boxes: 0 ground, k for element k-1; a smaller box wins where boxes overlap."""
    own = np.zeros(shape, np.int16)
    boxes = [(k + 1, e["boxes"][str(f)]) for k, e in enumerate(truth["elements"]) if str(f) in e.get("boxes", {})]
    for k, (x0, y0, x1, y1) in sorted(boxes, key=lambda kb: -(kb[1][2] - kb[1][0]) * (kb[1][3] - kb[1][1])):
        own[max(0, int(y0 * scale)):int(np.ceil(y1 * scale)), max(0, int(x0 * scale)):int(np.ceil(x1 * scale))] = k
    return own


def composition(r, owners: np.ndarray, n_owners: int) -> np.ndarray:
    x0, y0, x1, y1 = r.bbox
    o = owners[y0:y1, x0:x1][r.mask.astype(bool)]
    c = np.bincount(o.ravel(), minlength=n_owners + 1).astype(np.float64)
    return c / max(c.sum(), 1.0)


def _abs_iou(a, b) -> float:
    x0, y0, x1, y1 = max(a.bbox[0], b.bbox[0]), max(a.bbox[1], b.bbox[1]), min(a.bbox[2], b.bbox[2]), min(a.bbox[3], b.bbox[3])
    if x1 <= x0 or y1 <= y0:
        return 0.0
    ma = a.mask[y0 - a.bbox[1]:y1 - a.bbox[1], x0 - a.bbox[0]:x1 - a.bbox[0]].astype(bool)
    mb = b.mask[y0 - b.bbox[1]:y1 - b.bbox[1], x0 - b.bbox[0]:x1 - b.bbox[0]].astype(bool)
    inter = int((ma & mb).sum())
    return inter / max(1, a.area + b.area - inter)


def truth_label(a, ca: np.ndarray, b, cb: np.ndarray) -> int:
    """Same content: one dominant owner and at most a quarter foreign (ownership overlap >= 3/4); two ground-dominant
    regions are the same when their masks overlap by half or more where they sit (ground stays in place)."""
    da, db = int(ca.argmax()), int(cb.argmax())
    if da != db:
        return 0
    if da == 0:
        return int(_abs_iou(a, b) >= 0.5)
    return int(np.minimum(ca, cb).sum() >= QUARTER)


def ecc_measure(a, b, fa: np.ndarray, fb: np.ndarray, size: int = 96):
    """(correlation, coverage of b by warped a, coverage of warped a by b) for the best affine from a's masked crop to
    b's (None: too small to say). Correlation is ECC's own (gain and offset free: a fade still matches); 0 when ECC does
    not converge; for two flat regions, 1 when their colours are within titles.COLOUR_DE."""
    import cv2
    x0, y0 = min(a.bbox[0], b.bbox[0]), min(a.bbox[1], b.bbox[1])
    x1, y1 = max(a.bbox[2], b.bbox[2]), max(a.bbox[3], b.bbox[3])
    s = size / max(x1 - x0, y1 - y0, 1)
    W, H = max(8, round((x1 - x0) * s)), max(8, round((y1 - y0) * s))

    def part(r, frame):
        g = cv2.cvtColor(frame[y0:y1, x0:x1], cv2.COLOR_RGB2GRAY).astype(np.float32) / 255.0
        m = np.zeros((y1 - y0, x1 - x0), np.uint8)
        m[r.bbox[1] - y0:r.bbox[3] - y0, r.bbox[0] - x0:r.bbox[2] - x0] = r.mask
        return cv2.resize(g, (W, H), interpolation=cv2.INTER_AREA), cv2.resize(m, (W, H), interpolation=cv2.INTER_NEAREST)

    ga, ma = part(a, fa)
    gb, mb = part(b, fb)
    if ma.sum() < 8 or mb.sum() < 8:
        return None   # too small to say
    k = np.sqrt(max(b.area, 1) / max(a.area, 1))
    ax, ay = (a.centroid[0] - x0) * s, (a.centroid[1] - y0) * s
    bx, by = (b.centroid[0] - x0) * s, (b.centroid[1] - y0) * s
    warp = np.float32([[k, 0, bx - k * ax], [0, k, by - k * ay]])   # maps a's crop onto b's
    inv = cv2.invertAffineTransform(warp)                             # findTransformECC wants template -> input

    def cover(inv):
        wa = cv2.warpAffine(ma, inv, (W, H), flags=cv2.INTER_NEAREST | cv2.WARP_INVERSE_MAP)
        inter = float((wa & mb).sum())
        return inter / max(float(mb.sum()), 1.0), inter / max(float(wa.sum()), 1.0)

    def flat(r, frame):   # the region's own pixels (a resized crop mixes its edge with the ground)
        g = cv2.cvtColor(frame[r.bbox[1]:r.bbox[3], r.bbox[0]:r.bbox[2]], cv2.COLOR_RGB2GRAY)[r.mask.astype(bool)]
        return g.std() < 2.55

    if flat(a, fa) and flat(b, fb):   # two flat regions: nothing to correlate, so colour decides
        from keepframe.ir.colour import delta_e, srgb8_to_lab
        lab = lambda r: srgb8_to_lab(np.clip(np.round(np.asarray(r.color)), 0, 255).astype(np.uint8))
        return (1.0 if float(delta_e(lab(a), lab(b))) < 20.0 else 0.0), *cover(inv)   # titles.COLOUR_DE: a different colour
    try:
        rho, inv = cv2.findTransformECC(gb, ga, inv, cv2.MOTION_AFFINE,
                                        (cv2.TERM_CRITERIA_EPS | cv2.TERM_CRITERIA_COUNT, 50, 1e-4), mb, 3)
    except cv2.error:   # no convergence: the two do not correlate under any affine
        return 0.0, *cover(inv)
    return float(rho), *cover(inv)


def clip_pairs(rbf: list, frames: np.ndarray, owners=None, n_owners: int = 0, max_pairs: int = 20000, seed: int = 0) -> dict:
    """Candidate pairs (a at f, b at f+1..f+8 inside the tracking gate): features, truth labels (when owners are
    known, else -1) and ECC measures (nan when ECC fails)."""
    import math
    from keepframe.analyze.assoc import MAX_GAP, descriptor, pair_features
    pairs = []
    for f, regs in enumerate(rbf):
        for g in range(f + 1, min(len(rbf), f + MAX_GAP + 1)):
            for a in regs:
                for b in rbf[g]:
                    reach = max(GATE_PX, 2 * math.sqrt(max(a.area, b.area)))
                    if math.hypot(b.centroid[0] - a.centroid[0], b.centroid[1] - a.centroid[1]) <= reach:
                        pairs.append((a, b, g - f))
    rng = np.random.default_rng(seed)
    if len(pairs) > max_pairs:
        pairs = [pairs[i] for i in sorted(rng.choice(len(pairs), max_pairs, replace=False))]
    desc, comp = {}, {}

    def d(r):
        key = (r.frame, r.bbox, r.area)
        if key not in desc:
            desc[key] = descriptor(r, frames[r.frame])
            if owners is not None:
                comp[key] = composition(r, owners[r.frame], n_owners)
        return key

    X, yt, ecc = [], [], []
    for a, b, gap in pairs:
        ka, kb = d(a), d(b)
        X.append(pair_features(a, desc[ka], b, desc[kb], gap))
        yt.append(truth_label(a, comp[ka], b, comp[kb]) if owners is not None else -1)
        m = ecc_measure(a, b, frames[a.frame], frames[b.frame])
        ecc.append(m if m is not None else (np.nan, np.nan, np.nan))
    return {"X": np.array(X, np.float32).reshape(-1, 16), "y_truth": np.array(yt, np.int8),
            "ecc": np.array(ecc, np.float32).reshape(-1, 3), "gap": np.array([p[2] for p in pairs], np.int8)}


def _lottie_page() -> str:
    vendor = ROOT / "keepframe" / "compose" / "vendor" / "lottie-5.12.2.min.js"
    return f"""<!doctype html><html><body style="margin:0;background:transparent"><div id="c"></div>
<script src="{vendor.as_uri()}"></script><script>
window.setup = (docs, w, h, lw, lh) => {{
  window.anims = docs.map((d, i) => {{
    const el = document.createElement('div');
    el.style.width = (i ? lw : w) + 'px'; el.style.height = (i ? lh : h) + 'px';
    document.getElementById('c').appendChild(el);
    return lottie.loadAnimation({{container: el, renderer: 'canvas', loop: false, autoplay: false, animationData: d,
      rendererSettings: {{preserveAspectRatio: 'xMidYMid meet', clearCanvas: true}}}});
  }});
  return new Promise(r => setTimeout(r, 300));
}};
window.frameAt = (f) => window.anims.map((a, i) => {{
  a.goToAndStop(f, true);
  const c = a.renderer.canvasContext.canvas, px = a.renderer.canvasContext.getImageData(0, 0, c.width, c.height).data;
  const step = i ? 4 : 1, out = new Uint8Array(i ? px.length / 4 : px.length);
  if (i) {{ for (let j = 0; j < out.length; j++) out[j] = px[j * 4 + 3]; }} else out.set(px);
  let s = ''; for (let j = 0; j < out.length; j += 8192) s += String.fromCharCode.apply(null, out.subarray(j, j + 8192));
  return btoa(s);
}});
</script></body></html>"""


def lottie_render(path: Path, work: Path):
    """(frames RGB on a plain ground, owners per frame, n_owners, fps) for a Lottie file, or None when it is not worth
    it (more than MAX_OWNERS visible top-level layers). Owners come from one pass per layer with every other top-level
    layer at opacity 0 (parenting and track mattes stay intact): a pixel belongs to the layer with the most alpha
    above half; a layer covering 90 % of the frame is ground."""
    import base64
    import copy
    from playwright.sync_api import sync_playwright
    doc = json.loads(path.read_text())
    layers = doc["layers"]
    assets = {a["id"]: a for a in doc.get("assets", []) if "layers" in a}
    drawn = lambda ls: [i for i, l in enumerate(ls) if l.get("ty") != 3 and not l.get("td")]   # not nulls, not mattes
    owners = [(i,) for i in drawn(layers)]
    for i in drawn(layers):   # a precomp's children are owners of their own while that keeps MAX_OWNERS
        kids = drawn(assets[layers[i]["refId"]]["layers"]) if layers[i].get("ty") == 0 and layers[i].get("refId") in assets else []
        if len(kids) > 1 and len(owners) - 1 + len(kids) <= MAX_OWNERS:
            owners = [o for o in owners if o != (i,)] + [(i, k) for k in kids]
    if not 1 <= len(owners) <= MAX_OWNERS:
        return None
    w = LOTTIE_W
    h = int(round(w * doc["h"] / doc["w"] / 2) * 2)
    lw, lh = w // 2, h // 2

    def only(ls, keep_i):   # every layer but keep_i (and the matte it uses) at opacity 0
        keep = {keep_i, keep_i - 1} if ls[keep_i].get("tt") else {keep_i}
        for j, l in enumerate(ls):
            if j not in keep:
                l["ks"] = {**l.get("ks", {}), "o": {"a": 0, "k": 0}}

    docs = [doc]
    for o in owners:
        d = copy.deepcopy(doc)
        only(d["layers"], o[0])
        if len(o) == 2:
            only(next(a for a in d["assets"] if a.get("id") == d["layers"][o[0]]["refId"])["layers"], o[1])
        docs.append(d)
    n = int(min(LOTTIE_FRAMES, float(doc["op"]) - float(doc["ip"])))
    page = work / "lottie.html"
    page.write_text(_lottie_page())
    rgba, alpha = np.zeros((n, h, w, 4), np.uint8), np.zeros((len(owners), n, lh, lw), np.uint8)
    with sync_playwright() as p:
        b = p.chromium.launch()
        pg = b.new_page(viewport={"width": w + 8, "height": h + 8})
        pg.goto(page.as_uri())
        pg.evaluate("([d, w, h, lw, lh]) => window.setup(d, w, h, lw, lh)", [docs, w, h, lw, lh])
        for f in range(n):
            out = pg.evaluate("f => window.frameAt(f)", f)   # goToAndStop counts from the animation's first frame
            rgba[f] = np.frombuffer(base64.b64decode(out[0]), np.uint8).reshape(h, w, 4)
            for k, s64 in enumerate(out[1:]):
                alpha[k, f] = np.frombuffer(base64.b64decode(s64), np.uint8).reshape(lh, lw)
        b.close()
    ground = np.array([((alpha[k] > 127).mean(axis=(1, 2)) >= 0.9).mean() >= 0.5 for k in range(len(owners))])
    seed = int(path.stem) if path.stem.isdigit() else 0
    bg = np.array([(14, 16, 28), (245, 246, 250), (28, 22, 46), (250, 244, 232)][seed % 4], np.float32)
    a = rgba[..., 3:4].astype(np.float32) / 255.0
    frames = (rgba[..., :3].astype(np.float32) * a + bg * (1 - a)).round().astype(np.uint8)
    best = alpha.argmax(axis=0)
    own = np.where(alpha.max(axis=0) > 127, best + 1, 0).astype(np.int16)
    for k in np.flatnonzero(ground):
        own[own == k + 1] = 0
    own = own.repeat(2, axis=1).repeat(2, axis=2)[:, :h, :w]
    return frames, own, len(owners), float(doc.get("fr", 30))


def _save(path: Path, d: dict, **meta) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    np.savez_compressed(path, **d, meta=np.array(json.dumps(meta)))


def data_synth(seeds, kinds, ocr_box) -> None:
    from keepframe.analyze.video import read_frames
    from keepframe.kfscript.synth import KINDS, render_template
    work = EVAL / "assoc" / "work"
    for seed in seeds:
        for kind in kinds or KINDS:
            out = EVAL / "assoc" / "pairs" / f"synth-{kind}-{seed}.npz"
            if out.exists():
                continue
            case = work / f"synth-{kind}-{seed}"
            mp4, truth_path = render_template(kind, seed, case)
            truth = json.loads(Path(truth_path).read_text())
            frames, fps = read_frames(mp4)
            parts = []
            for a, fr, rbf in clip_regions(frames, fps, work / "stages", ocr_box):
                owners = np.stack([box_owners(truth, a + f, fr.shape[1:3]) for f in range(len(fr))])
                parts.append(clip_pairs(rbf, fr, owners, len(truth["elements"]), seed=seed))
            if parts:
                _save(out, {k: np.concatenate([p[k] for p in parts]) for k in parts[0]}, source="synth", kind=kind, seed=seed)
            import shutil
            shutil.rmtree(case, ignore_errors=True)
            print("synth", kind, seed, flush=True)


def data_lottie(ocr_box, limit: int) -> None:
    import shutil
    work = EVAL / "assoc" / "work"
    work.mkdir(parents=True, exist_ok=True)
    for path in sorted((EVAL / "assoc" / "lottie").glob("*.json"))[:limit]:
        out = EVAL / "assoc" / "pairs" / f"lottie-{path.stem}.npz"
        skip = out.with_suffix(".skip")
        if out.exists() or skip.exists():
            continue
        try:
            got = lottie_render(path, work)
        except Exception as err:   # fail soft: a file lottie-web cannot render is skipped, and remembered
            print("lottie render skip", path.stem, err, file=sys.stderr)
            got = None
        if got is None:
            skip.parent.mkdir(parents=True, exist_ok=True)
            skip.write_text("")
            continue
        frames, own, n_own, fps = got
        parts = [clip_pairs(rbf, fr, own[a:a + len(fr)], n_own, seed=int(path.stem) % 1000)
                 for a, fr, rbf in clip_regions(frames, fps, work / "stages-lottie", ocr_box)]
        if parts:
            _save(out, {k: np.concatenate([p[k] for p in parts]) for k in parts[0]}, source="lottie", id=path.stem)
        print("lottie", path.stem, flush=True)
    shutil.rmtree(work / "stages-lottie", ignore_errors=True)


def data_mk571(ocr_box) -> None:
    import cv2
    from keepframe.analyze.video import read_frames
    from keepframe.kfscript.synth import probe_boxes
    work = EVAL / "assoc" / "work"
    truth = json.loads((ROOT / "docs/qa/kfscript/gold/mk571.truth.json").read_text())
    probe_boxes(ROOT / truth["page"], truth, frames=False)
    for w0 in (0, 160, 320):
        out = EVAL / "assoc" / "pairs" / f"gold-mk571-{w0}.npz"
        if out.exists():
            continue
        frames, fps = read_frames(ROOT / "eval/refs/mk571.mp4", w0, w0 + 149)
        frames = np.stack([cv2.resize(f, (960, 540), interpolation=cv2.INTER_AREA) for f in frames])
        parts = []
        for a, fr, rbf in clip_regions(frames, fps, work / "stages", ocr_box):
            owners = np.stack([box_owners(truth, w0 + a + f, fr.shape[1:3], 0.5) for f in range(len(fr))])
            parts.append(clip_pairs(rbf, fr, owners, len(truth["elements"]), seed=w0))
        if parts:
            _save(out, {k: np.concatenate([p[k] for p in parts]) for k in parts[0]}, source="gold", id=f"mk571-{w0}")
        print("mk571", w0, flush=True)


def _thumbs(path: Path, every_s: float = 1.0, limit: int = 120) -> np.ndarray:
    import cv2
    cap = cv2.VideoCapture(str(path))
    step = max(1, round((cap.get(cv2.CAP_PROP_FPS) or 30) * every_s))
    out, i = [], 0
    while len(out) < limit:
        ok, bgr = cap.read()
        if not ok:
            break
        if i % step == 0:
            t = cv2.resize(cv2.cvtColor(bgr, cv2.COLOR_BGR2GRAY), (64, 36), interpolation=cv2.INTER_AREA).astype(np.float32).ravel()
            out.append((t - t.mean()) / (t.std() + 1e-6))
        i += 1
    cap.release()
    return np.array(out)


def dedupe_saas() -> set[str]:
    """Previews that show an eval clip (3+ one-per-second thumbnails correlating >= 0.95 with one of it) are excluded:
    the eval clips stay out of training. Written once to saas/excluded.txt."""
    path = EVAL / "assoc" / "saas" / "excluded.txt"
    if path.exists():
        return set(path.read_text().split())
    refs = [_thumbs(p, limit=200) for p in sorted((EVAL / "clips").glob("*.mp4"))]
    out = []
    for mp4 in sorted((EVAL / "assoc" / "saas").glob("*.mp4")):
        t = _thumbs(mp4)
        if any(len(t) and len(r) and ((t @ r.T / t.shape[1]).max(axis=1) >= 0.95).sum() >= 3 for r in refs):
            out.append(mp4.stem)
    path.write_text("\n".join(out) + ("\n" if out else ""))
    print("saas excluded", out, flush=True)
    return set(out)


def data_saas(ocr_box) -> None:
    import cv2
    from keepframe.analyze.video import read_frames
    work = EVAL / "assoc" / "work"
    excluded = dedupe_saas()
    for mp4 in sorted((EVAL / "assoc" / "saas").glob("*.mp4")):
        if mp4.stem in excluded:
            continue
        cap = cv2.VideoCapture(str(mp4))
        n = int(cap.get(cv2.CAP_PROP_FRAME_COUNT))
        cap.release()
        starts = [int(s) for s in np.linspace(0, max(0, n - SAAS_WINDOW), SAAS_WINDOWS)] if n > SAAS_WINDOW else [0]
        for w0 in sorted(set(starts)):
            out = EVAL / "assoc" / "pairs" / f"saas-{mp4.stem}-{w0}.npz"
            if out.exists():
                continue
            frames, fps = read_frames(mp4, w0, min(n, w0 + SAAS_WINDOW) - 1)
            if frames.shape[2] != SAAS_W:
                h = int(round(SAAS_W * frames.shape[1] / frames.shape[2] / 2) * 2)
                frames = np.stack([cv2.resize(f, (SAAS_W, h), interpolation=cv2.INTER_AREA) for f in frames])
            parts = [clip_pairs(rbf, fr, seed=w0) for a, fr, rbf in clip_regions(frames, fps, work / "stages-saas", ocr_box)]
            if parts:
                _save(out, {k: np.concatenate([p[k] for p in parts]) for k in parts[0]}, source="saas", id=mp4.stem, start=w0)
            print("saas", mp4.stem, w0, flush=True)


# ---------------------------------------------------------------- train / eval

def _held_out(meta: dict) -> bool:
    """Whole clips held out: synthetic seeds 8-9, a fifth of the Lottie animations and of the SaaS videos."""
    import zlib
    if meta["source"] == "synth":
        return int(meta["seed"]) >= 8
    if meta["source"] in ("lottie", "saas"):
        return zlib.crc32(str(meta["id"]).encode()) % 5 == 0
    return False   # gold mk571 trains (the user's choice)


def load_pairs() -> list[dict]:
    out = []
    for p in sorted((EVAL / "assoc" / "pairs").glob("*.npz")):
        with np.load(p) as d:
            meta = json.loads(str(d["meta"]))
            out.append({"name": p.stem, "meta": meta, "held": _held_out(meta), **{k: d[k] for k in ("X", "y_truth", "ecc")}})
    return out


def ecc_labels(ecc: np.ndarray, rho: float) -> np.ndarray:
    """Same content when one texture and one affine show both: correlation >= rho, each warped mask covering the
    other by at least 3/4; -1 where ECC could not say (too small)."""
    y = ((ecc[:, 0] >= rho) & (ecc[:, 1] >= QUARTER) & (ecc[:, 2] >= QUARTER)).astype(np.int8)
    return np.where(np.isnan(ecc[:, 0]), -1, y)


def calibrate_rho(clips: list[dict]) -> tuple[float, float]:
    """rho where the video-derived labels agree best with truth on the training clips that have truth."""
    tr = [c for c in clips if not c["held"] and c["meta"]["source"] in ("synth", "lottie")]
    ecc = np.concatenate([c["ecc"] for c in tr])
    yt = np.concatenate([c["y_truth"] for c in tr])
    best = (0.0, 0.0)
    for rho in np.round(np.arange(0.30, 0.96, 0.05), 2):
        y = ecc_labels(ecc, rho)
        ok = y >= 0
        agree = float((y[ok] == yt[ok]).mean()) if ok.any() else 0.0
        best = max(best, (agree, float(rho)))
    return best[1], best[0]


def labelled(clips: list[dict], rho: float):
    """(X, y, weight, source) of the clips: truth where known, else the video-derived label; each source weighs the
    same in total, and inside a source the two classes weigh the same."""
    X, y, src = [], [], []
    for c in clips:
        lab = c["y_truth"] if c["meta"]["source"] != "saas" else ecc_labels(c["ecc"], rho)
        keep = lab >= 0
        X.append(c["X"][keep]); y.append(lab[keep]); src += [c["meta"]["source"]] * int(keep.sum())
    X, y, src = np.concatenate(X), np.concatenate(y).astype(np.float32), np.array(src)
    w = np.zeros(len(y), np.float32)
    names = sorted(set(src))
    for s_ in names:
        for cls in (0, 1):
            m = (src == s_) & (y == cls)
            if m.any():
                w[m] = 1.0 / (2 * len(names) * m.sum())
    return X, y, w * len(y), src


def fit_mlp(X, y, w, epochs: int = 30, seed: int = 0) -> dict:
    import torch
    torch.manual_seed(seed)
    mean, std = X.mean(0), X.std(0) + 1e-6
    xt = torch.tensor((X - mean) / std, dtype=torch.float32)
    yt, wt = torch.tensor(y), torch.tensor(w)
    net = torch.nn.Sequential(torch.nn.Linear(16, 32), torch.nn.ReLU(), torch.nn.Linear(32, 32), torch.nn.ReLU(), torch.nn.Linear(32, 1))
    opt = torch.optim.Adam(net.parameters(), lr=1e-3, weight_decay=1e-4)
    g = torch.Generator().manual_seed(seed)
    for _ in range(epochs):
        for idx in torch.randperm(len(yt), generator=g).split(1024):
            loss = (torch.nn.functional.binary_cross_entropy_with_logits(net(xt[idx])[:, 0], yt[idx], reduction="none") * wt[idx]).mean()
            opt.zero_grad(); loss.backward(); opt.step()
    lin = [m for m in net if isinstance(m, torch.nn.Linear)]
    out = {"mean": mean.astype(np.float32), "std": std.astype(np.float32)}
    for i, m in enumerate(lin, 1):
        out[f"W{i}"] = m.weight.detach().numpy().T.astype(np.float32)
        out[f"b{i}"] = m.bias.detach().numpy().astype(np.float32)
    return out


def _auc(p: np.ndarray, y: np.ndarray) -> float | None:
    pos, neg = p[y == 1], p[y == 0]
    if not len(pos) or not len(neg):
        return None
    order = np.argsort(np.concatenate([pos, neg]), kind="stable")
    ranks = np.empty(len(order)); ranks[order] = np.arange(1, len(order) + 1)
    return float((ranks[:len(pos)].sum() - len(pos) * (len(pos) + 1) / 2) / (len(pos) * len(neg)))


def metrics(weights: dict, clips: list[dict], rho: float) -> dict:
    from keepframe.analyze.assoc import Matcher
    m = Matcher(weights)
    out = {}
    for source in sorted({c["meta"]["source"] for c in clips}):
        sel = [c for c in clips if c["meta"]["source"] == source]
        if not sel:
            continue
        X, y, _, _ = labelled(sel, rho)
        p = m.prob(X)
        out[source] = {"pairs": int(len(y)), "same": int(y.sum()), "auc": _auc(p, y), "accuracy": float(((p >= 0.5) == (y == 1)).mean())}
    return out


def train_main(epochs: int, out: Path) -> None:
    clips = load_pairs()
    rho, agree = calibrate_rho(clips)
    train = [c for c in clips if not c["held"]]
    held = [c for c in clips if c["held"]]
    X, y, w, src = labelled(train, rho)
    weights = fit_mlp(X, y, w, epochs)
    commit = subprocess.run(["git", "rev-parse", "--short", "HEAD"], capture_output=True, text=True, cwd=ROOT).stdout.strip()
    meta = {"commit": commit, "rho": rho, "rho_agreement_with_truth": round(agree, 4), "epochs": epochs,
            "train_pairs": {s_: int((src == s_).sum()) for s_ in sorted(set(src))},
            "train_clips": len(train), "held_out_clips": len(held),
            "held_out": metrics(weights, held, rho), "train": metrics(weights, train, rho),
            "sources": "synthetic kinds (seeds 2-7 train, 8-9 held), LottieFiles free animations (Lottie Simple License), "
                       "gold mk571 (Mixkit free), Envato Elements SaaS previews (paid templates; previews never committed)"}
    np.savez(out, **weights, meta=np.array(json.dumps(meta)))
    if out == ROOT / "keepframe" / "analyze" / "assoc.npz":   # the shipped weights: their record goes with them
        qa = ROOT / "docs" / "qa" / "assoc"
        qa.mkdir(parents=True, exist_ok=True)
        (qa / "train.json").write_text(json.dumps(meta, indent=1))
    print(json.dumps(meta, indent=1))


def eval_main(epochs: int) -> None:
    """Leave one synthetic kind out: train without it (all other training data), score it."""
    clips = load_pairs()
    rho, _ = calibrate_rho(clips)
    kinds = sorted({c["meta"]["kind"] for c in clips if c["meta"]["source"] == "synth"})
    rows = {}
    for k in kinds:
        tr = [c for c in clips if not c["held"] and c["meta"].get("kind") != k]
        te = [c for c in clips if c["meta"].get("kind") == k]
        X, y, w, _ = labelled(tr, rho)
        rows[k] = metrics(fit_mlp(X, y, w, epochs), te, rho).get("synth")
        print(k, rows[k], flush=True)
    qa = ROOT / "docs" / "qa" / "assoc"
    qa.mkdir(parents=True, exist_ok=True)
    (qa / "leave-one-kind-out.json").write_text(json.dumps({"rho": rho, "epochs": epochs, "kinds": rows}, indent=1))


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    f = sub.add_parser("fetch")
    f.add_argument("--lottie", type=int, default=150)
    f.add_argument("--saas", type=int, default=40)
    f.add_argument("--mixkit", type=int, default=20)
    dsub = sub.add_parser("data")
    dsub.add_argument("--sources", default="synth,gold,lottie,saas")
    dsub.add_argument("--seeds", default="2-9")
    dsub.add_argument("--kinds", default="")
    dsub.add_argument("--lottie-limit", type=int, default=1000)
    for name in ("train", "eval"):
        sp = sub.add_parser(name)
        sp.add_argument("--epochs", type=int, default=30)
        sp.add_argument("--out", type=Path, default=ROOT / "keepframe" / "analyze" / "assoc.npz")
    a = ap.parse_args(argv)
    if a.cmd == "train":
        train_main(a.epochs, a.out)
    if a.cmd == "eval":
        eval_main(a.epochs)
    if a.cmd == "data":
        ocr_box: list = []
        lo, hi = (int(x) for x in a.seeds.split("-"))
        for src in a.sources.split(","):
            if src == "synth":
                data_synth(range(lo, hi + 1), [k for k in a.kinds.split(",") if k], ocr_box)
            elif src == "gold":
                data_mk571(ocr_box)
            elif src == "lottie":
                data_lottie(ocr_box, a.lottie_limit)
            elif src == "saas":
                data_saas(ocr_box)
    if a.cmd == "fetch":
        fetch_lottie(a.lottie)
        fetch_saas(a.saas)
        fetch_mixkit(a.mixkit)
    return 0


if __name__ == "__main__":
    sys.exit(main())
