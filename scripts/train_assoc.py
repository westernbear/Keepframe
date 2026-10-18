"""The tracker's learned same-content matcher (fixes7): fetch media, make labelled region pairs, train, evaluate.

    python scripts/train_assoc.py fetch [--lottie 150] [--saas 40] [--mixkit 20]
    python scripts/train_assoc.py data            # resumable, one npz per clip under eval/assoc/pairs/
    python scripts/train_assoc.py train           # torch (CPU); writes keepframe/analyze/assoc.npz
    python scripts/train_assoc.py eval            # held-out metrics -> docs/qa/assoc/
    python scripts/train_assoc.py data --sources solids,solid-scenes   # the solid judgement's candidates
    python scripts/train_assoc.py solids --select # torch; writes keepframe/analyze/solids.npz

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
    import os
    opts = AnalyzeOptions(refine=False, generate_3d=False)
    work = work.with_name(f"{work.name}-{os.getpid()}")   # workers running side by side never share a stage dir
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


def owner_stats(owners: np.ndarray, n_owners: int) -> np.ndarray:
    """Per frame and owner: (centroid x, centroid y, area)."""
    n, H, W = owners.shape
    yy, xx = np.mgrid[:H, :W]
    out = np.zeros((n, n_owners + 1, 3), np.float32)
    for f in range(n):
        o = owners[f].ravel()
        area = np.bincount(o, minlength=n_owners + 1).astype(np.float32)
        cx = np.bincount(o, weights=xx.ravel(), minlength=n_owners + 1) / np.maximum(area, 1)
        cy = np.bincount(o, weights=yy.ravel(), minlength=n_owners + 1) / np.maximum(area, 1)
        out[f] = np.stack([cx, cy, area], 1)[: n_owners + 1]
    return out


LAB_COLS = ["dom_a", "dom_b", "overlap", "abs_iou", "ax", "ay", "bx", "by", "a_area", "b_area",
            "ocx_f", "ocy_f", "oarea_f", "ocx_g", "ocy_g", "oarea_g"]


def label_ingredients(a, ca: np.ndarray, b, cb: np.ndarray, stats: np.ndarray) -> list[float]:
    """What a truth label needs, stored so labels can be computed (and their definition revised) at training time."""
    da, db = int(ca.argmax()), int(cb.argmax())
    return [da, db, float(np.minimum(ca, cb).sum()), _abs_iou(a, b), *a.centroid, *b.centroid, a.area, b.area,
            *stats[a.frame, da], *stats[b.frame, da]]


def truth_labels(lab: np.ndarray) -> np.ndarray:
    """Same content: one dominant owner, at most a quarter foreign (ownership overlap >= 3/4), and b where the owner's
    own motion takes a (the owner's centroid shift and scale between the two frames; within half of b's size), so two
    parts of one layer are not the same; or a and b each the owner's main body (more than half of what shows of it),
    wherever an occluder moved what shows. Two ground-dominant regions are the same when their masks overlap by half
    or more where they sit (ground stays in place). -1 where no truth is known."""
    L = {c: lab[:, i] for i, c in enumerate(LAB_COLS)}
    s = np.sqrt(np.maximum(L["oarea_g"], 1) / np.maximum(L["oarea_f"], 1))
    px, py = L["ocx_g"] + s * (L["ax"] - L["ocx_f"]), L["ocy_g"] + s * (L["ay"] - L["ocy_f"])
    follows = np.hypot(L["bx"] - px, L["by"] - py) <= 0.5 * np.sqrt(L["b_area"])
    body = (L["a_area"] > 0.5 * L["oarea_f"]) & (L["b_area"] > 0.5 * L["oarea_g"])
    same_owner = (L["dom_a"] == L["dom_b"]) & (L["oarea_g"] > 0)
    y = np.where(L["dom_a"] == 0, L["abs_iou"] >= 0.5, same_owner & (L["overlap"] >= QUARTER) & (follows | body))
    y = np.where(L["dom_a"] != L["dom_b"], False, y).astype(np.int8)
    return np.where(np.isnan(L["dom_a"]), -1, y)


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
    """Candidate pairs (a at f, b at f+1..f+8 inside the tracking gate) with a's anchor n: the region of a's owner at
    the earliest frame within MAX_GAP of b (teacher forcing: the truth track's oldest region in the tracker's window; a
    itself without owners or for ground). Features (assoc.track_features), truth-label ingredients of (a, b) and of
    (n, b) (nan without owners; see truth_labels) and ECC measures of (a, b) (nan: too small to say)."""
    import math
    from keepframe.analyze.assoc import MAX_GAP, N_FEATURES, track_features
    from keepframe.analyze.tracking import _Descriptors
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
    desc, comp = _Descriptors(frames, 0, rbf), {}
    stats = owner_stats(owners, n_owners) if owners is not None else None

    def c(r):
        key = (r.frame, r.bbox, r.area)
        if key not in comp:
            comp[key] = composition(r, owners[r.frame], n_owners)
        return comp[key]

    def anchor(a, b):
        o = int(c(a).argmax()) if owners is not None else 0
        if o == 0:
            return a
        for h in range(max(0, b.frame - MAX_GAP), a.frame):
            body = [r for r in rbf[h] if int(c(r).argmax()) == o]
            if body:
                return max(body, key=lambda r: c(r)[o] * r.area)
        return a

    X, lab, lab_n, ecc = [], [], [], []
    nan = [np.nan] * len(LAB_COLS)
    for a, b, gap in pairs:
        n = anchor(a, b)
        X.append(track_features(a, n, b, desc))
        lab.append(label_ingredients(a, c(a), b, c(b), stats) if owners is not None else nan)
        lab_n.append(label_ingredients(n, c(n), b, c(b), stats) if owners is not None else nan)
        m = ecc_measure(a, b, frames[a.frame], frames[b.frame])
        ecc.append(m if m is not None else (np.nan, np.nan, np.nan))
    return {"X": np.array(X, np.float32).reshape(-1, N_FEATURES), "lab": np.array(lab, np.float32).reshape(-1, len(LAB_COLS)),
            "lab_n": np.array(lab_n, np.float32).reshape(-1, len(LAB_COLS)),
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


def _claim(out: Path) -> bool:
    """One worker per clip: a claim file next to the output; one older than 30 min was left by a stopped worker."""
    import os
    import time
    lock = out.with_suffix(".lock")
    out.parent.mkdir(parents=True, exist_ok=True)
    try:
        os.close(os.open(lock, os.O_CREAT | os.O_EXCL))
        return True
    except FileExistsError:
        if time.time() - lock.stat().st_mtime > 1800:
            lock.touch()
            return True
        return False


def _save(path: Path, d: dict, **meta) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    np.savez_compressed(path, **d, meta=np.array(json.dumps(meta)))
    path.with_suffix(".lock").unlink(missing_ok=True)


def data_synth(seeds, kinds, ocr_box) -> None:
    from keepframe.analyze.video import read_frames
    from keepframe.kfscript.synth import KINDS, render_template
    work = EVAL / "assoc" / "work"
    for seed in seeds:
        for kind in kinds or KINDS:
            out = EVAL / "assoc" / "pairs" / f"synth-{kind}-{seed}.npz"
            if out.exists() or not _claim(out):
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
        if out.exists() or skip.exists() or not _claim(out):
            continue
        try:
            got = lottie_render(path, work)
        except Exception as err:   # fail soft: a file lottie-web cannot render is skipped, and remembered
            print("lottie render skip", path.stem, err, file=sys.stderr)
            got = None
        if got is None:
            skip.parent.mkdir(parents=True, exist_ok=True)
            skip.write_text("")
            out.with_suffix(".lock").unlink(missing_ok=True)
            continue
        frames, own, n_own, fps = got
        parts = [clip_pairs(rbf, fr, own[a:a + len(fr)], n_own, seed=int(path.stem) % 1000)
                 for a, fr, rbf in clip_regions(frames, fps, work / "stages-lottie", ocr_box)]
        if parts:
            _save(out, {k: np.concatenate([p[k] for p in parts]) for k in parts[0]}, source="lottie", id=path.stem)
        print("lottie", path.stem, flush=True)
    shutil.rmtree(work / "stages-lottie", ignore_errors=True)


def scene_owners(scene, root: Path) -> np.ndarray:
    """Pixel owners of an ir scene from each element's exact alpha (keepframe.ir.synth.layers): the top element covering
    at least half of a pixel, else ground (0)."""
    from keepframe.ir.synth import layers
    order = sorted(scene.elements, key=lambda e: e.z.keys[0].v)   # shortcut: constant z, as the ir generators make it
    ids = {e.id: k for k, e in enumerate(scene.elements, 1)}
    best = np.full((scene.frames, scene.size[1], scene.size[0]), 0.5, np.float32)
    own = np.zeros(best.shape, np.int16)
    for eid, f, a, _ in layers(scene.model_copy(update={"elements": order}), root, range(scene.frames)):
        m = a >= best[f]   # bottom to top: an element above takes the pixels it covers
        best[f][m], own[f][m] = a[m], ids[eid]
    return own


def ir_scene(gen: str, seed: int, case: Path):
    """One draw of the ir generators the tests use (keepframe.ir.synth, "synthetic" or "reference"); raises on a draw
    the generator cannot make (a stagger title in a font without glyph centres)."""
    import random
    from keepframe.ir.synth import make_reference_scene, make_synthetic_scene
    rng = random.Random(seed)
    if gen == "synthetic":
        return make_synthetic_scene(case, seed, n_elements=rng.randint(3, 6), frames=rng.choice([24, 60]),
                                    with_text=rng.random() < 0.5, overlap=rng.random() < 0.5)
    return make_reference_scene(case, seed, plate=rng.choice(["flat", "gradient", "animated", "image"]),
                                n_sprites=rng.randint(1, 4), n_titles=rng.randint(1, 2),
                                mover=rng.choice([False, True, "inplace"]), title=rng.choice([None, "stagger", "plane"]))


def data_scenes(seeds, ocr_box) -> None:
    """The ir generators the tests draw from (keepframe.ir.synth), on seeds the tests never use: rotation, scale, fades,
    eased jumps, crossings, titles over plates and movers."""
    import shutil
    from keepframe.analyze.video import read_frames, render_scene_video
    work = EVAL / "assoc" / "work"
    for seed in seeds:
        for gen in ("synthetic", "reference"):
            out = EVAL / "assoc" / "pairs" / f"scene-{gen}-{seed}.npz"
            skip = out.with_suffix(".skip")
            if out.exists() or skip.exists() or not _claim(out):
                continue
            case = work / f"scene-{gen}-{seed}"
            try:
                scene = ir_scene(gen, seed, case)
            except Exception as err:   # fail soft: a draw the generator cannot make (a stagger title in a font without glyph centres)
                print("scene skip", gen, seed, err, file=sys.stderr)
                skip.touch(); out.with_suffix(".lock").unlink(missing_ok=True)
                continue
            frames, fps = read_frames(render_scene_video(scene, case, case / "clip.mp4"))
            owners = scene_owners(scene, case)
            parts = [clip_pairs(rbf, fr, owners[a:a + len(fr)], len(scene.elements), seed=seed)
                     for a, fr, rbf in clip_regions(frames, fps, work / "stages-scene", ocr_box)]
            if parts:
                _save(out, {k: np.concatenate([p[k] for p in parts]) for k in parts[0]}, source="scene", kind=gen, seed=seed)
            shutil.rmtree(case, ignore_errors=True)
            print("scene", gen, seed, flush=True)


def data_mk571(ocr_box) -> None:
    import cv2
    from keepframe.analyze.video import read_frames
    from keepframe.kfscript.synth import probe_boxes
    work = EVAL / "assoc" / "work"
    truth = json.loads((ROOT / "docs/qa/kfscript/gold/mk571.truth.json").read_text())
    probe_boxes(ROOT / truth["page"], truth, frames=False)
    for w0 in (0, 160, 320):
        out = EVAL / "assoc" / "pairs" / f"gold-mk571-{w0}.npz"
        if out.exists() or not _claim(out):
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
            if out.exists() or not _claim(out):
                continue
            frames, fps = read_frames(mp4, w0, min(n, w0 + SAAS_WINDOW) - 1)
            if frames.shape[2] != SAAS_W:
                h = int(round(SAAS_W * frames.shape[1] / frames.shape[2] / 2) * 2)
                frames = np.stack([cv2.resize(f, (SAAS_W, h), interpolation=cv2.INTER_AREA) for f in frames])
            parts = [clip_pairs(rbf, fr, seed=w0) for a, fr, rbf in clip_regions(frames, fps, work / "stages-saas", ocr_box)]
            if parts:
                _save(out, {k: np.concatenate([p[k] for p in parts]) for k in parts[0]}, source="saas", id=mp4.stem, start=w0)
            print("saas", mp4.stem, w0, flush=True)


# ---------------------------------------------------------------- data: the solid judgement (fixes7 S)
# A candidate is a solid when one texture cannot show it: a 3D body turning (sphere, cube) or a look that changes in
# place (a noise blob). One texture moved, scaled, turned in the plane or faded (a card, a disc, a word) is not.

def _rodrigues(axis: np.ndarray, angle: float) -> np.ndarray:
    import cv2
    return cv2.Rodrigues((axis * angle).astype(np.float64))[0]


def _axis(rng, max_z: float) -> np.ndarray:
    """A random unit axis at most `max_z` towards the viewer (a turn about the view axis is a turn in the plane)."""
    while True:
        a = rng.normal(size=3)
        a /= np.linalg.norm(a)
        if abs(a[2]) <= max_z:
            return a


def _two_colours(rng):
    while True:
        c1, c2 = rng.uniform(0, 255, 3), rng.uniform(0, 255, 3)
        if np.abs(c1 - c2).sum() >= 120:
            return c1, c2


def _sphere(rng, n: int, scale: float):
    """A textured sphere turning about an axis off the view axis, its texture varying around that axis."""
    r0, grow = rng.uniform(10, 70) * scale, rng.uniform(0.75, 1.3)
    speed = np.radians(rng.uniform(2, 15)) * rng.choice([-1, 1])
    a = _axis(rng, 0.5)
    u = np.cross(a, [0.0, 0.0, 1.0]); u /= np.linalg.norm(u); w = np.cross(a, u)
    k, wob, pattern, shade = int(rng.integers(3, 11)), rng.uniform(0, 1), rng.choice(["stripes", "checker"]), rng.random() < 0.5
    c1, c2 = _two_colours(rng)
    S = int(2 * r0 * max(grow, 1.0)) + 3
    yy, xx = np.mgrid[:S, :S] - (S - 1) / 2

    def draw(f):
        r = r0 * grow ** (f / max(n - 1, 1))
        inside = xx ** 2 + yy ** 2 <= r * r
        z = np.sqrt(np.maximum(r * r - xx ** 2 - yy ** 2, 0))
        p = np.stack([xx, yy, z], -1)
        lon = np.arctan2(p @ u, p @ w) - speed * f
        lat = np.arcsin(np.clip((p @ a) / r, -1, 1))
        t = np.sin(k * lon + wob * np.sin(6 * lat)) if pattern == "stripes" else np.sign(np.sin(k * lon) * np.sin(k * lat))
        rgb = c1 + (c2 - c1) * (0.5 + 0.5 * t)[..., None]
        if shade:
            rgb = rgb * (0.45 + 0.55 * z / max(r, 1))[..., None]
        return rgb, inside.astype(np.float32)
    return draw


def _face_texture(rng, T: int = 32) -> np.ndarray:
    c1, c2 = _two_colours(rng)
    yy, xx = np.mgrid[:T, :T]
    kind = rng.choice(["flat", "stripes", "checker"])
    t = np.zeros((T, T)) if kind == "flat" else np.sin(xx * np.pi / rng.integers(3, 9)) if kind == "stripes" else \
        np.sign(np.sin(xx * np.pi / 8) * np.sin(yy * np.pi / 8))
    return (c1 + (c2 - c1) * (0.5 + 0.5 * t)[..., None]).astype(np.float32)


def _cube(rng, n: int, scale: float):
    """A box tumbling about an axis off the view axis: flat-shaded faces, plain or patterned."""
    import cv2
    s = rng.uniform(16, 80) * scale
    dims = s * rng.uniform(0.6, 1.0, 3)
    speed, a = np.radians(rng.uniform(2, 10)) * rng.choice([-1, 1]), _axis(rng, 0.6)
    R0 = _rodrigues(_axis(rng, 1.0), rng.uniform(0, np.pi))
    tex = [_face_texture(rng) for _ in range(6)]
    S = int(np.linalg.norm(dims)) + 3
    c = (S - 1) / 2

    def draw(f):
        R = _rodrigues(a, speed * f) @ R0
        rgb, alpha = np.zeros((S, S, 3), np.float32), np.zeros((S, S), np.float32)
        for i, (ax, sg) in enumerate((ax, sg) for ax in range(3) for sg in (-1, 1)):
            nrm = R[:, ax] * sg
            if nrm[2] <= 0:
                continue
            j, k = [d for d in range(3) if d != ax]
            def corner(sj, sk):
                p = np.zeros(3); p[ax], p[j], p[k] = sg * dims[ax] / 2, sj * dims[j] / 2, sk * dims[k] / 2
                q = R @ p
                return [q[0] + c, q[1] + c]
            quad = np.float32([corner(-1, -1), corner(1, -1), corner(1, 1), corner(-1, 1)])
            M = cv2.getAffineTransform(np.float32([[0, 0], [32, 0], [0, 32]]), quad[[0, 1, 3]])
            face = cv2.warpAffine(tex[i], M, (S, S), flags=cv2.INTER_LINEAR, borderMode=cv2.BORDER_REPLICATE)
            m = np.zeros((S, S), np.uint8)
            cv2.fillConvexPoly(m, np.round(quad).astype(np.int32), 1)
            rgb[m > 0] = face[m > 0] * (0.4 + 0.6 * nrm[2])
            alpha[m > 0] = 1
        return rgb, alpha
    return draw


def _blob(rng, n: int, scale: float):
    """A shape whose texture changes in place: noise fields turning into one another, or fresh every frame."""
    import cv2
    w, h = (int(v) for v in rng.uniform(16, 90, 2) * scale)
    sigma, rate, fresh = rng.choice([0, 1, 2, 4]), rng.uniform(0.15, 1.6), rng.random() < 0.3
    mean, amp = rng.uniform(60, 200, 3), rng.uniform(30, 90)
    def field():
        x = rng.normal(size=(h, w, 3)).astype(np.float32)
        x = cv2.GaussianBlur(x, (0, 0), float(sigma)) if sigma else x
        return x / max(float(x.std()), 1e-6)
    A, B = field(), field()
    shape = np.zeros((h, w), np.uint8)
    if rng.random() < 0.5:
        cv2.ellipse(shape, (((w - 1) / 2, (h - 1) / 2), (w, h), 0), 1, -1)
    else:
        shape[:] = 1

    def draw(f):
        x = field() if fresh else A * np.cos(rate * f) + B * np.sin(rate * f)
        return mean + amp * x, shape.astype(np.float32)
    return draw


def _card_texture(rng, w: int, h: int) -> tuple[np.ndarray, np.ndarray]:
    import cv2
    c1, c2 = _two_colours(rng)
    yy, xx = np.mgrid[:h, :w]
    kind = rng.choice(["flat", "gradient", "stripes", "checker", "noise", "text"])
    alpha = np.ones((h, w), np.float32)
    if kind == "flat":
        t = np.zeros((h, w))
    elif kind == "gradient":
        ang = rng.uniform(0, np.pi)
        t = 2 * ((xx * np.cos(ang) + yy * np.sin(ang)) / max(w, h)) - 1
    elif kind == "stripes":
        t = np.sin((xx + yy * rng.uniform(-1, 1)) * np.pi / rng.integers(3, 12))
    elif kind == "checker":
        q = int(rng.integers(4, 16))
        t = np.sign(np.sin(xx * np.pi / q) * np.sin(yy * np.pi / q))
    elif kind == "noise":
        t = cv2.GaussianBlur(rng.normal(size=(h, w)).astype(np.float32), (0, 0), float(rng.choice([1, 2, 4])))
        t = np.clip(t / max(float(t.std()), 1e-6) / 2, -1, 1)
    else:
        word = "".join(rng.choice(list("ABCDEFGHKMNPRSTUVWXYZ")) for _ in range(int(rng.integers(2, 7))))
        m = np.zeros((h, w), np.uint8)
        fs = h / 30
        cv2.putText(m, word, (2, int(h * 0.8)), cv2.FONT_HERSHEY_SIMPLEX, fs, 1, max(1, int(fs * 2.5)), cv2.LINE_AA)
        alpha, t = m.astype(np.float32), np.ones((h, w))
    shape = rng.choice(["rect", "ellipse"]) if kind != "text" else "rect"
    if shape == "ellipse":
        m = np.zeros((h, w), np.uint8)
        cv2.ellipse(m, (((w - 1) / 2, (h - 1) / 2), (w, h), 0), 1, -1)
        alpha = alpha * m
    return (c1 + (c2 - c1) * (0.5 + 0.5 * t)[..., None]).astype(np.float32), alpha


def _card(rng, n: int, scale: float):
    """One texture moved, scaled, turned in the plane (slowly, or all the way round like an in-place mover) and faded."""
    import cv2
    w, h = (int(v) for v in rng.uniform(16, 160, 2) * scale)
    rgb, alpha = _card_texture(rng, w, h)
    turn = rng.choice(["none", "slow", "spin"], p=[0.5, 0.3, 0.2])
    deg = 0.0 if turn == "none" else rng.uniform(0.5, 4) if turn == "slow" else 360.0 * rng.integers(1, 3) / n
    deg *= rng.choice([-1, 1])
    s0, s1 = (1.0, 1.0) if rng.random() < 0.6 else tuple(rng.uniform(0.6, 1.5, 2))
    fade = rng.choice(["none", "in", "out", "pulse"], p=[0.55, 0.15, 0.15, 0.15])
    S = int(np.hypot(w, h) * max(s0, s1)) + 3
    src = np.dstack([rgb, alpha])

    def draw(f):
        t = f / max(n - 1, 1)
        M = cv2.getRotationMatrix2D(((w - 1) / 2, (h - 1) / 2), deg * f, s0 + (s1 - s0) * t)
        M[:, 2] += [(S - w) / 2, (S - h) / 2]
        out = cv2.warpAffine(src, M, (S, S), flags=cv2.INTER_LINEAR)
        op = {"none": 1.0, "in": min(1.0, 0.1 + 1.5 * t), "out": max(0.0, 1.0 - 1.5 * t),
              "pulse": 0.55 + 0.45 * np.cos(2 * np.pi * t)}[fade]
        return out[..., :3], out[..., 3] * op
    return draw


def _spinner(rng, n: int, scale: float):
    """One patterned texture (stripes, sectors, checker, noise...) turning in the plane, mostly in place: a badge, a
    logo, a loader. Its pattern's pieces break up in the region tracker as a turning body's surface does."""
    import cv2
    d = int(rng.uniform(24, 120) * scale)
    if rng.random() < 0.3:
        c1, c2 = _two_colours(rng)
        yy, xx = np.mgrid[:d, :d] - (d - 1) / 2
        t = np.sign(np.sin(int(rng.integers(3, 9)) * np.arctan2(yy, xx)))
        rgb, alpha = (c1 + (c2 - c1) * (0.5 + 0.5 * t)[..., None]).astype(np.float32), np.ones((d, d), np.float32)
    else:
        rgb, alpha = _card_texture(rng, d, d)
    if rng.random() < 0.7:
        m = np.zeros((d, d), np.uint8)
        cv2.circle(m, (d // 2, d // 2), d // 2, 1, -1)
        alpha = alpha * m
    deg = rng.uniform(2, 20) * rng.choice([-1, 1])
    src = np.dstack([rgb, alpha])

    def draw(f):
        out = cv2.warpAffine(src, cv2.getRotationMatrix2D(((d - 1) / 2, (d - 1) / 2), deg * f, 1.0), (d, d), flags=cv2.INTER_LINEAR)
        return out[..., :3], out[..., 3]
    return draw


def solid_clip(seed: int):
    """A clip with known truth for the solid judgement (seeds the tests never use): on a flat, gradient or textured
    ground, 0-2 objects one texture cannot show and 0-4 it can (one at least: cards, or patterns spinning in the
    plane), moving, in random order. Returns (frames, per-frame
    mask of the first kind dilated 2 px, the ground's hex when it is flat else None)."""
    import cv2
    rng = np.random.default_rng(seed)
    n = int(rng.choice([12, 16, 24, 30, 36, 48, 60]))
    W, H = [(240, 180), (320, 180), (480, 270), (640, 360)][int(rng.integers(4))]
    scale = H / 180
    c1, c2 = _two_colours(rng)
    ground = rng.choice(["flat", "gradient", "texture"], p=[0.5, 0.3, 0.2])
    yy, xx = np.mgrid[:H, :W]
    if ground == "flat":
        base = np.broadcast_to(c1, (H, W, 3))
    elif ground == "gradient":
        base = c1 + (c2 - c1) * (xx / W if rng.random() < 0.5 else yy / H)[..., None]
    else:
        t = cv2.GaussianBlur(rng.normal(size=(H, W)).astype(np.float32), (0, 0), 6.0)
        base = c1 + (c2 - c1) * np.clip(0.5 + t / max(float(t.std()), 1e-6) / 4, 0, 1)[..., None] * 0.3
    makers = [(rng.choice([_sphere, _cube, _blob], p=[0.45, 0.3, 0.25]), True) for _ in range(rng.choice([0, 1, 1, 2]))]
    makers += [(rng.choice([_card, _spinner], p=[0.75, 0.25]), False) for _ in range(max(int(rng.integers(0, 5)), not makers))]
    rng.shuffle(makers)
    objs = []
    for make, positive in makers:
        c0 = rng.uniform([0.1 * W, 0.1 * H], [0.9 * W, 0.9 * H])
        v = rng.uniform(-3, 3, 2) * scale * (rng.random() < 0.7)
        objs.append((make(rng, n, scale), positive, c0, v))
    frames = np.empty((n, H, W, 3), np.uint8)
    pos = np.zeros((n, H, W), bool)
    for f in range(n):
        img, own = base.astype(np.float32).copy(), np.zeros((H, W), np.int8)   # 1: a solid's pixel, -1: another object's
        for draw, positive, c0, v in objs:
            rgb, a = draw(f)
            h, w = a.shape
            x, y = (int(round(q)) for q in c0 + v * f - [w / 2, h / 2])
            x0, y0, x1, y1 = max(x, 0), max(y, 0), min(x + w, W), min(y + h, H)
            if x1 <= x0 or y1 <= y0:
                continue
            aa = a[y0 - y:y1 - y, x0 - x:x1 - x]
            img[y0:y1, x0:x1] = img[y0:y1, x0:x1] * (1 - aa[..., None]) + rgb[y0 - y:y1 - y, x0 - x:x1 - x] * aa[..., None]
            own[y0:y1, x0:x1][aa > 0.5] = 1 if positive else -1
        frames[f] = np.clip(np.round(img), 0, 255).astype(np.uint8)
        pos[f] = cv2.dilate((own == 1).astype(np.uint8), np.ones((5, 5), np.uint8)) > 0
    bg = "#%02x%02x%02x" % tuple(int(round(v)) for v in c1) if ground == "flat" and rng.random() < 0.5 else None
    return frames, pos, bg


class _Done(BaseException):
    """Stops an analysis once its solids stage has run: what follows is not the judge's input."""


def solid_candidates(frames: np.ndarray, bg: str | None, work: Path) -> list[list]:
    """The solids stage's candidates on a clip, as the pipeline makes them: [solid, features, rule's call] each."""
    from keepframe.analyze import pipeline, solids
    rec: list[list] = []
    feats, keep, stage = solids._features, solids._keep, pipeline._stage_solids

    def features(*a):
        x = feats(*a)
        rec.append([a[1], x, None])
        return x

    def judged(x, rule):
        rec[-1][2] = rule
        return rule

    def stop(*a, **k):
        stage(*a, **k)
        raise _Done
    solids._features, solids._keep, pipeline._stage_solids = features, judged, stop
    try:   # shortcut: a solids stage re-run without its movers records both runs' candidates; rare, left in
        pipeline.analyze_scene_frames(frames, 30.0, work, "s1", pipeline.AnalyzeOptions(
            bg_override=bg, ocr=False, refine=False, use_ecc=False, generate_3d=False))
    except _Done:
        pass
    finally:
        solids._features, solids._keep, pipeline._stage_solids = feats, keep, stage
    return rec


def solid_share(solid, pos: np.ndarray) -> float:
    """Share of the candidate's pixels, over its frames, on an object one texture cannot show."""
    on = tot = 0
    for f, ((x0, y0, x1, y1), m) in solid.frames.items():
        on += np.count_nonzero(m & pos[f, y0:y1, x0:x1])
        tot += np.count_nonzero(m)
    return on / max(tot, 1)


def data_solids(seeds) -> None:
    """One npz per clip under eval/assoc/solids/: each candidate's features, its truth share and the rule's call. Half
    the clips go through an mp4v encode, as the tests' videos do."""
    import shutil
    import cv2
    from keepframe.analyze.solids import N_FEATURES
    from keepframe.analyze.video import read_frames
    work = EVAL / "assoc" / "work"
    for seed in seeds:
        out = EVAL / "assoc" / "solids" / f"clip-{seed}.npz"
        if out.exists() or not _claim(out):
            continue
        frames, pos, bg = solid_clip(seed)
        case = work / f"solids-{seed}"
        case.mkdir(parents=True, exist_ok=True)
        if seed % 2:
            wr = cv2.VideoWriter(str(case / "clip.mp4"), cv2.VideoWriter_fourcc(*"mp4v"), 30, frames.shape[2:0:-1])
            for fr in frames:
                wr.write(cv2.cvtColor(fr, cv2.COLOR_RGB2BGR))
            wr.release()
            frames = read_frames(case / "clip.mp4")[0][:len(pos)]
        rec = solid_candidates(frames, bg, case)
        shutil.rmtree(case, ignore_errors=True)
        share = np.array([solid_share(s, pos) for s, _, _ in rec], np.float32)
        _save(out, {"X": np.array([x for _, x, _ in rec], np.float32).reshape(-1, N_FEATURES), "share": share,
                    "rule": np.array([bool(r) for *_, r in rec], bool)}, source="solids", seed=seed)
        print("solids", seed, len(rec), int((share >= 0.5).sum()), flush=True)


def data_solid_scenes(seeds) -> None:
    """The ir generators' scenes (titles over plates, in-place and crossing movers, fades), where every element is one
    texture: each candidate is labelled not a solid."""
    import shutil
    from keepframe.analyze.solids import N_FEATURES
    from keepframe.analyze.video import read_frames, render_scene_video
    work = EVAL / "assoc" / "work"
    for seed in seeds:
        for gen in ("synthetic", "reference"):
            out = EVAL / "assoc" / "solids" / f"scene-{gen}-{seed}.npz"
            if out.exists() or out.with_suffix(".skip").exists() or not _claim(out):
                continue
            case = work / f"solids-scene-{gen}-{seed}"
            try:
                scene = ir_scene(gen, seed, case)
            except Exception as err:   # fail soft: a draw the generator cannot make
                print("solid scene skip", gen, seed, err, file=sys.stderr)
                out.with_suffix(".skip").touch(); out.with_suffix(".lock").unlink(missing_ok=True)
                continue
            frames, _ = read_frames(render_scene_video(scene, case, case / "clip.mp4"))
            rec = solid_candidates(frames, None, case / "analysis")
            shutil.rmtree(case, ignore_errors=True)
            _save(out, {"X": np.array([x for _, x, _ in rec], np.float32).reshape(-1, N_FEATURES), "share": np.zeros(len(rec), np.float32),
                        "rule": np.array([bool(r) for *_, r in rec], bool)}, source="solids", kind=gen, seed=seed)
            print("solid scene", gen, seed, len(rec), flush=True)


# ---------------------------------------------------------------- train / eval

def _held_out(meta: dict) -> bool:
    """Whole clips held out: synthetic seeds 8-9, a fifth of the ir scenes (seed % 5 == 0), of the Lottie animations and
    of the SaaS videos."""
    import zlib
    if meta["source"] == "synth":
        return int(meta["seed"]) >= 8
    if meta["source"] == "scene":
        return int(meta["seed"]) % 5 == 0
    if meta["source"] in ("lottie", "saas"):
        return zlib.crc32(str(meta["id"]).encode()) % 5 == 0
    return False   # gold mk571 trains (the user's choice)


def load_pairs() -> list[dict]:
    out = []
    for p in sorted((EVAL / "assoc" / "pairs").glob("*.npz")):
        if p.with_suffix(".lock").exists():   # still being written by a data worker
            continue
        with np.load(p) as d:
            meta = json.loads(str(d["meta"]))
            ya, yn = truth_labels(d["lab"]), truth_labels(d["lab_n"])   # b continues a, and the track's content at n
            out.append({"name": p.stem, "meta": meta, "held": _held_out(meta), "X": d["X"], "ecc": d["ecc"],
                        "y_truth": np.where((ya < 0) | (yn < 0), -1, ya & yn).astype(np.int8)})
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


def fit_mlp(X, y, w, epochs: int = 30, seed: int = 0, hidden: int = 32) -> dict:
    import torch
    torch.manual_seed(seed)
    torch.set_num_threads(2)   # the data workers share the 4 cores
    mean, std = X.mean(0), X.std(0) + 1e-6
    xt = torch.tensor((X - mean) / std, dtype=torch.float32)
    yt, wt = torch.tensor(y), torch.tensor(w)
    net = torch.nn.Sequential(torch.nn.Linear(X.shape[1], hidden), torch.nn.ReLU(), torch.nn.Linear(hidden, hidden), torch.nn.ReLU(),
                              torch.nn.Linear(hidden, 1))
    opt = torch.optim.Adam(net.parameters(), lr=1e-3, weight_decay=1e-4)
    g = torch.Generator().manual_seed(seed)
    for _ in range(epochs):
        for idx in torch.randperm(len(yt), generator=g).split(4096):
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


def train_main(epochs: int, out: Path, saas: bool = True, hidden: int = 32) -> None:
    clips = load_pairs()
    rho, agree = calibrate_rho(clips)
    train = [c for c in clips if not c["held"] and (saas or c["meta"]["source"] != "saas")]
    held = [c for c in clips if c["held"]]
    X, y, w, src = labelled(train, rho)
    weights = fit_mlp(X, y, w, epochs, hidden=hidden)
    commit = subprocess.run(["git", "rev-parse", "--short", "HEAD"], capture_output=True, text=True, cwd=ROOT).stdout.strip()
    meta = {"commit": commit, "trained_on_saas": saas, "hidden": hidden, "rho": rho, "rho_agreement_with_truth": round(agree, 4), "epochs": epochs,
            "train_pairs": {s_: int((src == s_).sum()) for s_ in sorted(set(src))},
            "train_clips": len(train), "held_out_clips": len(held),
            "held_out": metrics(weights, held, rho), "train": metrics(weights, train, rho),
            "sources": "synthetic kinds (seeds 2-7 train, 8-9 held), LottieFiles free animations (Lottie Simple License), "
                       "gold mk571 (Mixkit free)" + (", Envato Elements SaaS previews (paid templates; previews never committed)" if saas else "")}
    np.savez(out, **weights, meta=np.array(json.dumps(meta)))
    if out == ROOT / "keepframe" / "analyze" / "assoc.npz":   # the shipped weights: their record goes with them
        qa = ROOT / "docs" / "qa" / "assoc"
        qa.mkdir(parents=True, exist_ok=True)
        (qa / "train.json").write_text(json.dumps(meta, indent=1))
    print(json.dumps(meta, indent=1))


def eval_main(epochs: int, saas: bool = True, hidden: int = 32) -> None:
    """Leave one synthetic kind out: train without it (all other training data), score it."""
    clips = load_pairs()
    rho, _ = calibrate_rho(clips)
    kinds = sorted({c["meta"]["kind"] for c in clips if c["meta"]["source"] == "synth"})
    rows = {}
    for k in kinds:
        tr = [c for c in clips if not c["held"] and c["meta"].get("kind") != k and (saas or c["meta"]["source"] != "saas")]
        te = [c for c in clips if c["meta"].get("kind") == k]
        X, y, w, _ = labelled(tr, rho)
        rows[k] = metrics(fit_mlp(X, y, w, epochs, hidden=hidden), te, rho).get("synth")
        print(k, rows[k], flush=True)
    qa = ROOT / "docs" / "qa" / "assoc"
    qa.mkdir(parents=True, exist_ok=True)
    (qa / "leave-one-kind-out.json").write_text(json.dumps({"rho": rho, "epochs": epochs, "hidden": hidden, "trained_on_saas": saas, "kinds": rows}, indent=1))


SOLID_GRID = [(8, 500), (8, 1500), (16, 500), (16, 1500), (32, 500)]   # (hidden, epochs) the cross-validation picks from


def solids_main(epochs: int, out: Path, hidden: int, select: bool = False) -> None:
    """Train the solid judgement on the clips with seed % 5 != 0 (with `select`, the SOLID_GRID size whose mean balanced
    accuracy over 5 folds of them is best), score the rest against truth with the rule beside it."""
    from keepframe.analyze.assoc import Matcher
    from keepframe.analyze.solids import N_FEATURES
    rows = []
    for p in sorted((EVAL / "assoc" / "solids").glob("*.npz")):
        if not p.with_suffix(".lock").exists():
            with np.load(p) as d:
                rows.append((json.loads(str(d["meta"])), d["X"].reshape(-1, N_FEATURES), d["share"] >= 0.5, d["rule"]))

    def cat(sel):
        return (np.concatenate([r[1] for r in sel]), np.concatenate([r[2] for r in sel]), np.concatenate([r[3] for r in sel]))
    train, held = [r for r in rows if r[0]["seed"] % 5], [r for r in rows if not r[0]["seed"] % 5]

    def fit(sel, h, e):
        X, y, _ = cat(sel)
        w = np.where(y, 0.5 / max(y.mean(), 1e-6), 0.5 / max(1 - y.mean(), 1e-6)).astype(np.float32)   # balanced
        return fit_mlp(X, y.astype(np.float32), w, e, hidden=h)
    cv = {}
    if select:   # 5 folds of the training clips by seed: the held-out clips never choose
        for h, e in SOLID_GRID:
            accs = []
            for k in range(5):
                X, y, _ = cat([r for r in train if r[0]["seed"] // 5 % 5 == k])
                p = Matcher(fit([r for r in train if r[0]["seed"] // 5 % 5 != k], h, e), N_FEATURES).prob(X) >= 0.5
                accs.append((p[y].mean() + (~p[~y]).mean()) / 2)
            cv[f"h{h} e{e}"] = round(float(np.mean(accs)), 4)
            print("cv", h, e, cv[f"h{h} e{e}"], flush=True)
        hidden, epochs = SOLID_GRID[int(np.argmax(list(cv.values())))]
    weights = fit(train, hidden, epochs)
    _, y, _ = cat(train)
    m = Matcher(weights, N_FEATURES)

    def score(sel):
        if not sel:
            return None
        X, y, rule = cat(sel)
        p = m.prob(X)
        calls = lambda c: {"accuracy": round(float((c == y).mean()), 4), "solid_recall": round(float(c[y].mean()), 4) if y.any() else None,
                           "other_recall": round(float((~c[~y]).mean()), 4) if (~y).any() else None}
        return {"candidates": int(len(y)), "solids": int(y.sum()), "auc": _auc(p, y.astype(int)), "learned": calls(p >= 0.5), "rule": calls(rule)}
    commit = subprocess.run(["git", "rev-parse", "--short", "HEAD"], capture_output=True, text=True, cwd=ROOT).stdout.strip()
    kinds = lambda sel, k: [r for r in sel if r[0].get("kind", "clip") == k]
    meta = {"commit": commit, "epochs": epochs, "hidden": hidden, "cv_balanced_accuracy": cv, "train_clips": len(train), "held_out_clips": len(held),
            "train_candidates": int(len(y)), "train_solids": int(y.sum()),
            "held_out": {"all": score(held), "clips": score(kinds(held, "clip")),
                         "ir_scenes": score(kinds(held, "synthetic") + kinds(held, "reference"))},
            "train": score(train),
            "sources": "solid clips (scripts/train_assoc.py solid_clip, seeds 6000+: spheres, boxes and noise blobs among "
                       "cards, words and discs) and the keepframe.ir.synth generators (seeds 7000+), regions and tracks from "
                       "the pipeline with the shipped assoc.npz; held out: seed % 5 == 0"}
    np.savez(out, **weights, meta=np.array(json.dumps(meta)))
    if out == ROOT / "keepframe" / "analyze" / "solids.npz":   # the shipped weights: their record goes with them
        (ROOT / "docs" / "qa" / "assoc" / "solids.json").write_text(json.dumps(meta, indent=1))
    print(json.dumps(meta, indent=1))


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
    dsub.add_argument("--scene-seeds", default="5000-5059", help="ir scene seeds (the tests use seeds below 100)")
    dsub.add_argument("--kinds", default="")
    dsub.add_argument("--lottie-limit", type=int, default=1000)
    dsub.add_argument("--solid-seeds", default="6000-7599", help="solid clips (solid_clip)")
    dsub.add_argument("--solid-scene-seeds", default="7000-7099", help="ir scenes for the solid judgement")
    sj = sub.add_parser("solids", help="train the solid judgement; writes keepframe/analyze/solids.npz")
    sj.add_argument("--epochs", type=int, default=3000)
    sj.add_argument("--hidden", type=int, default=16)
    sj.add_argument("--out", type=Path, default=ROOT / "keepframe" / "analyze" / "solids.npz")
    sj.add_argument("--select", action="store_true", help="choose hidden and epochs by 5-fold cross-validation (SOLID_GRID)")
    for name in ("train", "eval"):
        sp = sub.add_parser(name)
        sp.add_argument("--epochs", type=int, default=30)
        sp.add_argument("--out", type=Path, default=ROOT / "keepframe" / "analyze" / "assoc.npz")
        sp.add_argument("--no-saas", action="store_true", help="train without the video-labelled SaaS pairs")
        sp.add_argument("--hidden", type=int, default=32, help="units in each of the MLP's two hidden layers")
    a = ap.parse_args(argv)
    if a.cmd == "train":
        train_main(a.epochs, a.out, not a.no_saas, a.hidden)
    if a.cmd == "eval":
        eval_main(a.epochs, not a.no_saas, a.hidden)
    if a.cmd == "solids":
        solids_main(a.epochs, a.out, a.hidden, a.select)
    if a.cmd == "data":
        ocr_box: list = []
        lo, hi = (int(x) for x in a.seeds.split("-"))
        for src in a.sources.split(","):
            if src == "synth":
                data_synth(range(lo, hi + 1), [k for k in a.kinds.split(",") if k], ocr_box)
            elif src == "gold":
                data_mk571(ocr_box)
            elif src == "scene":
                s0, s1 = (int(x) for x in a.scene_seeds.split("-"))
                data_scenes(range(s0, s1 + 1), ocr_box)
            elif src == "lottie":
                data_lottie(ocr_box, a.lottie_limit)
            elif src == "saas":
                data_saas(ocr_box)
            elif src == "solids":
                s0, s1 = (int(x) for x in a.solid_seeds.split("-"))
                data_solids(range(s0, s1 + 1))
            elif src == "solid-scenes":
                s0, s1 = (int(x) for x in a.solid_scene_seeds.split("-"))
                data_solid_scenes(range(s0, s1 + 1))
    if a.cmd == "fetch":
        fetch_lottie(a.lottie)
        fetch_saas(a.saas)
        fetch_mixkit(a.mixkit)
    return 0


if __name__ == "__main__":
    sys.exit(main())
