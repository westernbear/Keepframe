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

ROOT = Path(__file__).resolve().parents[1]
EVAL = ROOT / "eval"
UA = "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/141.0 Safari/537.36"
LOTTIE_QUERIES = ["saas", "app", "product launch", "dashboard", "startup", "technology", "interface", "onboarding",
                  "chart", "notification", "logo reveal", "text animation", "kinetic typography", "intro", "business",
                  "website", "mobile app", "analytics", "payment", "marketing"]
ENVATO_QUERIES = ["saas+promo", "product+launch", "app+promo", "product+promo", "website+promo", "tech+promo"]
MIXKIT_PAGES = ["titles", "openers", "logo", "slideshow", "social-media", "lower-thirds", "transitions"]
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
    and research only, never committed). One 540p preview per template; duplicates across host apps by title+author."""
    out = EVAL / "assoc" / "saas"
    out.mkdir(parents=True, exist_ok=True)
    meta = out / "meta.jsonl"
    have = {json.loads(l)["slug"] for l in meta.open()} if meta.exists() else set()

    def items(pg, url):
        pg.goto(url, wait_until="domcontentloaded", timeout=60000)
        pg.wait_for_timeout(4000)
        return sorted(set(re.findall(r'href="/([a-z0-9-]+-[A-Z0-9]{7})"', pg.content())))

    slugs = []
    for found in _browser_pages([f"https://elements.envato.com/video-templates/{q}" for q in ENVATO_QUERIES], items).values():
        slugs += [s for s in found if s not in slugs]

    def preview(pg, url):   # the item's own preview is the first video the page requests
        with pg.expect_request(lambda r: ".mp4" in r.url, timeout=20000) as req:
            pg.goto(url, wait_until="domcontentloaded", timeout=60000)
        return req.value.url, pg.title().split(" - ")[0]

    todo = [s for s in slugs if s not in have][: max(0, n - len(have))]
    for url, (mp4, title) in _browser_pages([f"https://elements.envato.com/{s}" for s in todo], preview).items():
        slug = url.rsplit("/", 1)[1]
        if not mp4:
            continue
        subprocess.run(["curl", "-sS", "-L", "-o", str(out / f"{slug}.mp4"), mp4], check=False, timeout=300)
        if (out / f"{slug}.mp4").exists() and (out / f"{slug}.mp4").stat().st_size > 50_000:
            with meta.open("a") as fh:
                fh.write(json.dumps({"slug": slug, "title": title, "page": url, "preview": mp4,
                                     "license": "Envato Elements preview (paid template; training/research only, not committed)"}) + "\n")
            have.add(slug)
    print("saas", len(have))


def fetch_mixkit(n: int) -> None:
    """Free after-effects template previews from Mixkit (Mixkit free licence), for evaluation only (eval/refs2)."""
    out = EVAL / "refs2"
    out.mkdir(parents=True, exist_ok=True)
    have = {int(p.stem[2:]) for p in out.glob("mk*.mp4")}
    ids = []
    for page in MIXKIT_PAGES:
        try:
            html = _get(f"https://mixkit.co/free-after-effects-templates/{page}/").decode("utf-8", "ignore")
        except Exception as err:
            print("mixkit skip", page, err, file=sys.stderr)
            continue
        ids += [int(i) for i in re.findall(r"video-templates/(\d+)/", html) if int(i) not in ids]
    for i in [i for i in ids if i not in MIXKIT_SEEN and i not in have][: max(0, n - len(have))]:
        path = out / f"mk{i}.mp4"
        subprocess.run(["curl", "-sS", "-fL", "-o", str(path), f"https://assets.mixkit.co/video-templates/{i}/mixkit-{i}-1080.mp4"],
                       check=False, timeout=300)
        if path.exists() and path.stat().st_size < 50_000:
            path.unlink()
    print("mixkit", len(list(out.glob("mk*.mp4"))))


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    f = sub.add_parser("fetch")
    f.add_argument("--lottie", type=int, default=150)
    f.add_argument("--saas", type=int, default=40)
    f.add_argument("--mixkit", type=int, default=20)
    a = ap.parse_args(argv)
    if a.cmd == "fetch":
        fetch_lottie(a.lottie)
        fetch_saas(a.saas)
        fetch_mixkit(a.mixkit)
    return 0


if __name__ == "__main__":
    sys.exit(main())
