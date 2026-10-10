"""Per-project font uploads (Task 11): the upload trust boundary, storage by sha256, and the uploaded faces in
analysis, text edits, final renders and the AE export."""
import functools
import hashlib
import http.client
import io
import json
import re
import socket
import struct
import threading
from pathlib import Path
from urllib.error import HTTPError
from urllib.parse import quote
from urllib.request import Request, urlopen

import cv2
import numpy as np
import pytest

from keepframe.fonts.registry import FontRegistry
from keepframe.fonts.upload import (MAX_FONT_BYTES, FontRejected, inspect_font, list_fonts, public_font, scene_font_asset,
                                    store_font)
from keepframe.ir.schema import Background, Canonical, Element, FontGuess, Scene
from keepframe.ir.store import current_scene, init_project, scene_dir
from tests.test_web_server import start

REG = FontRegistry()


# --- font bytes ---------------------------------------------------------------------------------------------------

@functools.lru_cache(maxsize=None)
def _ttf(family="Brand Wide", *, factor=1.3, flavor=None, source="Varela Round", drop=(), style="Regular") -> bytes:
    """A distinct 'brand' TrueType font: a bundled face stretched horizontally and renamed (same bytes every call)."""
    from fontTools.pens.transformPen import TransformPen
    from fontTools.pens.ttGlyphPen import TTGlyphPen
    from fontTools.ttLib import TTFont
    font = TTFont(str(REG.face(source).path))
    font.flavor, font.recalcTimestamp = None, False
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
    for tag in ("GPOS", "kern", *drop):
        if tag in font:
            del font[tag]
    if "name" in font:
        table = font["name"]
        table.names = [r for r in table.names if r.nameID not in (1, 2, 3, 4, 6, 16, 17)]
        for nid, value in ((1, family), (2, style), (3, f"{family};test"), (4, f"{family} {style}"),
                           (6, family.replace(" ", "") + "-Regular")):
            table.setName(value, nid, 3, 1, 0x409)
    font.flavor = flavor
    buf = io.BytesIO()
    font.save(buf)
    return buf.getvalue()


@functools.lru_cache(maxsize=None)
def _otf(family="Brand Block", chars="ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz") -> bytes:
    """A CFF ('OTTO') font of solid bars, weight 700 (same bytes every call)."""
    from fontTools.fontBuilder import FontBuilder
    from fontTools.pens.t2CharStringPen import T2CharStringPen
    names = [".notdef", "space"] + [f"g{ord(c):04x}" for c in chars]
    fb = FontBuilder(1000, isTTF=False)
    fb.setupGlyphOrder(names)
    fb.setupCharacterMap({0x20: "space", **{ord(c): f"g{ord(c):04x}" for c in chars}})
    strings = {}
    for i, name in enumerate(names):
        pen = T2CharStringPen(600, None)
        if name != "space":
            pen.moveTo((50, 0)); pen.lineTo((50, 700 - 5 * (i % 20))); pen.lineTo((550, 700)); pen.lineTo((550, 0))
            pen.closePath()
        strings[name] = pen.getCharString()
    ps = family.replace(" ", "") + "-Bold"
    fb.setupCFF(ps, {"FullName": f"{family} Bold"}, strings, {})
    fb.setupHorizontalMetrics({n: (600, 50) for n in names})
    fb.setupHorizontalHeader(ascent=800, descent=-200)
    fb.setupNameTable({"familyName": family, "styleName": "Bold", "psName": ps})
    fb.setupOS2(usWeightClass=700, sTypoAscender=800, sTypoDescender=-200, usWinAscent=800, usWinDescent=200)
    fb.setupPost()
    fb.font["head"].created = fb.font["head"].modified = 3_000_000_000
    fb.font.recalcTimestamp = False
    buf = io.BytesIO()
    fb.save(buf)
    return buf.getvalue()


def _b128(n: int) -> bytes:
    out = [n & 0x7F]
    n >>= 7
    while n:
        out.append(0x80 | (n & 0x7F))
        n >>= 7
    return bytes(reversed(out))


def _woff2(tables, stream: bytes, *, total_sfnt=None) -> bytes:
    """A WOFF2 file from scratch: tables = [(known tag index, origLength)] (null transforms), `stream` the brotli
    data. Header fields are what the file declares, not necessarily what the stream holds."""
    directory = b"".join(bytes([index | (0xC0 if index in (10, 11) else 0)]) + _b128(orig) for index, orig in tables)
    total_sfnt = total_sfnt if total_sfnt is not None else 12 + 16 * len(tables) + sum(o for _, o in tables)
    length = 48 + len(directory) + len(stream)
    header = struct.pack(">4sIIHHIIHHIIIII", b"wOF2", 0x00010000, length, len(tables), 0, total_sfnt, len(stream),
                         1, 0, 0, 0, 0, 0, 0)
    return header + directory + stream


# --- server helpers -----------------------------------------------------------------------------------------------

def _workspace(tmp_path: Path) -> Path:
    from keepframe.ir.synth import make_synthetic_scene
    ws = tmp_path / "ws"
    root = ws / "p1"
    scene = make_synthetic_scene(root / "scenes" / "s1", seed=8, with_text=False, frames=6).model_copy(update={"id": "s1"})
    init_project(root, {"file": "source.mp4", "fps": scene.fps, "size": list(scene.size)}, scene)
    (root / "meta.json").write_text(json.dumps({"id": "p1", "title": "t", "status": "review", "scene": "s1"}))
    return ws


def _base(srv) -> str:
    return f"http://127.0.0.1:{srv.server_address[1]}"


def _upload(srv, data: bytes, name: str | None = "brand.ttf", *, pid="p1", origin: str | None = "", query=None):
    url = f"{_base(srv)}/api/projects/{pid}/fonts" + (query if query is not None else
                                                      f"?name={quote(name)}" if name is not None else "")
    headers = {"Content-Type": "application/octet-stream"}
    if origin is not None:
        headers["Origin"] = origin or _base(srv)
    try:
        with urlopen(Request(url, data=data, method="POST", headers=headers)) as r:
            return r.status, json.loads(r.read())
    except HTTPError as e:
        return e.code, json.loads(e.read())


def _get(srv, path: str) -> tuple[int, bytes]:
    try:
        with urlopen(f"{_base(srv)}{path}") as r:
            return r.status, r.read()
    except HTTPError as e:
        return e.code, e.read()


def _stray(ws: Path) -> list[Path]:
    """Anything a refused upload may have left in the workspace (the index lock file aside)."""
    return [p for p in ws.rglob("*") if p.is_file() and (p.name.endswith(".part") or p.parent.name == "fonts")
            and p.name != ".index.lock"]


@pytest.fixture
def server(tmp_path):
    ws = _workspace(tmp_path)
    srv = start(ws)
    yield srv, ws
    srv.shutdown()
    srv.server_close()


# --- the route ----------------------------------------------------------------------------------------------------

def test_oversize_font_rejected_before_body_read(server):
    srv, ws = server
    port = srv.server_address[1]

    def raw(headers: str) -> bytes:
        with socket.create_connection(("127.0.0.1", port), timeout=10) as s:
            s.sendall((f"POST /api/projects/p1/fonts?name=big.ttf HTTP/1.1\r\nHost: 127.0.0.1:{port}\r\n"
                       f"Origin: http://127.0.0.1:{port}\r\nContent-Type: application/octet-stream\r\n"
                       f"{headers}\r\n").encode())
            out = b""
            while chunk := s.recv(65536):   # the answer comes with no body byte sent: none was waited for
                out += chunk
            return out

    big = raw(f"Content-Length: {MAX_FONT_BYTES + 1}\r\n")
    assert big.split(b"\r\n", 1)[0].split(b" ")[1] == b"413"
    assert json.loads(big.split(b"\r\n\r\n", 1)[1]) == {"error": "too_large"}
    unsized = raw("")
    assert unsized.split(b"\r\n", 1)[0].split(b" ")[1] == b"411"
    chunked = raw("Transfer-Encoding: chunked\r\n")
    assert chunked.split(b"\r\n", 1)[0].split(b" ")[1] in (b"400", b"411")
    assert _stray(ws) == []


def test_cross_origin_upload_forbidden(server):
    srv, ws = server
    data = _ttf()
    for origin in ("http://evil.example", f"http://127.0.0.1:{srv.server_address[1] + 1}", "null", None):
        code, body = _upload(srv, data, origin=origin)
        assert (code, body) == (403, {"error": "browser origin is not allowed"}), origin
    conn = http.client.HTTPConnection("127.0.0.1", srv.server_address[1], timeout=10)
    conn.request("GET", "/api/projects/p1/fonts", headers={"Host": "evil.example"})
    assert conn.getresponse().status == 403
    conn.close()
    assert _stray(ws) == []


@pytest.mark.parametrize("name", ["../evil.ttf", "a/b.ttf", "a\\b.ttf", "font.ttc", "font.exe", "font", ".ttf",
                                  "x" * 129 + ".ttf", "font.woff", "font.ttf.png", None])
def test_bad_name_and_traversal_rejected(server, name):
    srv, ws = server
    data = _ttf()
    assert _upload(srv, data, name) == (400, {"error": "bad_type"})
    assert _upload(srv, data, query="?name=a.ttf&name=b.ttf") == (400, {"error": "bad_type"})
    for pid in ("..", "%2e%2e", "p1%2F..", "p1%00", "nope"):
        assert _upload(srv, data, "brand.ttf", pid=pid)[0] == 404, pid
    assert _stray(ws) == []
    code, body = _upload(srv, data, "Brand Font (1).TTF")   # the control: a plain name, any case of extension
    assert code == 201 and body["font"]["family"] == "Brand Wide"


def test_bad_magic_rejected(server):
    srv, ws = server
    ttf, otf, woff2 = _ttf(), _otf(), _ttf(flavor="woff2")
    png = cv2.imencode(".png", np.zeros((8, 8, 3), np.uint8))[1].tobytes()
    collection = b"ttcf" + struct.pack(">HHI", 1, 0, 1) + struct.pack(">I", 16) + ttf
    for data, name in ((png, "logo.ttf"), (otf, "x.woff2"), (ttf, "x.woff2"), (woff2, "x.ttf"), (woff2, "x.otf"),
                       (otf, "x.ttf"), (ttf, "x.otf"), (collection, "x.ttf"), (collection, "x.otf"), (b"\0\1", "x.ttf")):
        assert _upload(srv, data, name) == (400, {"error": "bad_type"}), name
    assert _stray(ws) == []
    for data, name in ((ttf, "x.ttf"), (otf, "x.otf"), (woff2, "x.woff2")):
        code, body = _upload(srv, data, name)
        assert code == 201 and body["font"]["ext"] == name.rsplit(".", 1)[1], name
    assert not list(ws.rglob("*.part"))


def test_invalid_font_tables_rejected(server, tmp_path):
    import brotli
    srv, ws = server
    garbage = b"\0\1\0\0" + bytes(range(256)) * 4
    assert _upload(srv, garbage, "x.ttf") == (400, {"error": "bad_tables"})
    assert _upload(srv, _ttf(drop=("name",)), "x.ttf") == (400, {"error": "bad_tables"})
    assert _upload(srv, _ttf(drop=("cmap",)), "x.ttf") == (400, {"error": "bad_tables"})
    zeros = brotli.compress(b"\0" * 2**21)
    cases = {
        "header totalSfntSize over 64 MiB": _woff2([(0, 1000)], brotli.compress(b"\0" * 1000), total_sfnt=65 * 2**20),
        "one table origLength over 64 MiB": _woff2([(0, 65 * 2**20)], zeros, total_sfnt=1000),
        "table origLengths summing over 64 MiB": _woff2([(0, 40 * 2**20), (1, 40 * 2**20)], zeros, total_sfnt=1000),
        "brotli stream longer than declared": _woff2([(0, 1000)], zeros),
        "brotli stream not brotli": _woff2([(0, 1000)], b"\xff" * 64),
    }
    for reason, data in cases.items():
        path = tmp_path / "case.woff2"
        path.write_bytes(data)
        with pytest.raises(FontRejected) as err:
            inspect_font(path, "case.woff2")
        assert err.value.code == "bad_tables", reason
        assert _upload(srv, data, "case.woff2") == (400, {"error": "bad_tables"}), reason
    digits = _otf("Digits Only", chars="0123456789")
    assert _upload(srv, digits, "digits.otf") == (400, {"error": "unsupported"})
    assert _stray(ws) == []


def test_duplicate_upload_is_idempotent(server):
    srv, ws = server
    data = _ttf()
    sha = hashlib.sha256(data).hexdigest()
    code, first = _upload(srv, data, "brand.ttf")
    assert code == 201 and first["created"] is True
    assert first["font"] == {"family": "Brand Wide", "style": "Regular",
                             "weight_range": [400, 400], "postscript": "BrandWide-Regular", "category": first["font"]["category"],
                             "ext": "ttf", "sha256": sha, "bytes": len(data), "latin": True, "hangul": False}
    code, again = _upload(srv, data, "copy of brand.ttf")
    assert code == 200 and again == {"font": first["font"], "created": False}
    results, otf = [], _otf()
    threads = [threading.Thread(target=lambda: results.append(_upload(srv, otf, "block.otf"))) for _ in range(3)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert sorted(code for code, _ in results) == [200, 200, 201]
    root = ws / "p1"
    index = json.loads((root / "fonts" / "index.json").read_text())
    assert [it["family"] for it in index["fonts"]] == ["Brand Wide", "Brand Block"]
    assert sorted(p.name for p in (root / "fonts").iterdir() if not p.name.startswith(".")) == sorted(
        [f"{sha}.ttf", f"{hashlib.sha256(_otf()).hexdigest()}.otf", "index.json"])
    code, body = _get(srv, "/api/projects/p1/fonts")
    assert index["fonts"] == list_fonts(root) and index["fonts"][0] == {**first["font"], "original_family": "Brand Wide",
                                                                         "file": f"{sha}.ttf"}
    assert code == 200 and json.loads(body)["fonts"] == [public_font(it) for it in index["fonts"]]
    reg = FontRegistry.for_project(root)
    assert reg.face("Brand Wide").source == "uploaded" and reg.face("Brand Block", 700).weight_range == (700, 700)
    entry, created = store_font(root, _write(root.parent / "dup.ttf", data), "dup.ttf")
    assert entry == index["fonts"][0] and created is False and not (root.parent / "dup.ttf").exists()


def _write(path: Path, data: bytes) -> Path:
    path.write_bytes(data)
    return path


def test_font_bytes_never_served(server):
    srv, ws = server
    data = _ttf()
    sha = hashlib.sha256(data).hexdigest()
    assert _upload(srv, data, "brand.ttf")[0] == 201
    root = ws / "p1"
    entry = list_fonts(root)[0]
    asset = scene_font_asset(scene_dir(root, "s1"), entry, root)
    assert asset == f"assets/font-{sha[:16]}.ttf" and (scene_dir(root, "s1") / asset).read_bytes() == data
    listing = ["/api/projects/p1/fonts", f"/api/projects/p1/fonts?name={sha}.ttf"]
    files = [f"/api/projects/p1/fonts/{sha}.ttf", f"/api/projects/p1/fonts/{sha}", f"/assets/{sha}.ttf?project=p1",
             f"/assets/font-{sha[:16]}.ttf?project=p1&scene=s1", f"/assets/../../fonts/{sha}.ttf?project=p1",
             f"/assets/font-{sha[:16]}.ttf%00.png?project=p1&scene=s1", f"/static/../../p1/fonts/{sha}.ttf",
             f"/p1/fonts/{sha}.ttf", f"/fonts/{sha}.ttf"]
    for path in listing + files + ["/api/projects/p1", "/api/projects", "/api/state?project=p1&scene=s1"]:
        code, body = _get(srv, path)
        assert data[:256] not in body and data[-256:] not in body, path
        if path in listing:
            assert code == 200 and len(body) < 2048 and json.loads(body)["fonts"][0]["sha256"] == sha
        elif path in files:
            assert code in (400, 404), (path, code)


# --- analysis, edits, final renders -------------------------------------------------------------------------------

def test_uploaded_font_used_in_analysis_and_copied_to_scene_assets(tmp_path, monkeypatch):
    from keepframe.analyze.pipeline import AnalyzeOptions, analyze
    from keepframe.fonts.raster import glyph_alpha
    from tests.test_textstyle import CX, CY, _box_raw, _even, _grad, _render
    root = tmp_path / "project"
    root.mkdir()
    data = _ttf()
    entry, created = store_font(root, _write(tmp_path / "up.ttf", data), "Brand.ttf")
    assert created
    face = FontRegistry.for_project(root).face("Brand Wide")
    a = _even(glyph_alpha(["Brand Day"], 46, [face], weight=400, pad=6))
    tex = np.zeros(a.shape + (4,), np.uint8)
    tex[..., :3] = (20, 30, 120)
    tex[..., 3] = np.clip(np.rint(a * 255), 0, 255).astype(np.uint8)
    frames = _render(tmp_path / "src", [("t", tex, CX, CY, 1)], Background(kind="gradient", gradient=_grad("#fff4e0", "#9ad1d4")),
                     8, 1.0, 0)
    _, box, _ = _box_raw(a > 0.05, a.shape, 8)

    class FakeOcr:
        def __init__(self, max_side=None):
            pass

        def __call__(self, frame):
            return [("Brand Day", box, 0.95)]

    monkeypatch.setattr("keepframe.analyze.pipeline.read_frames", lambda *args: (frames, 30.0))
    monkeypatch.setattr("keepframe.analyze.text.RapidOcr", FakeOcr)
    analyze(tmp_path / "clip.mp4", 0, 7, root, AnalyzeOptions(refine=False, use_ecc=False, generate_3d=False), ocr=FakeOcr())
    scene, _ = current_scene(root, "s1")
    font = next(e for e in scene.elements if e.kind == "text").canonical.font
    assert font.family_guess == "Brand Wide" and font.source == "uploaded", font
    assert font.postscript == "BrandWide-Regular"
    assert font.file == f"assets/font-{entry['sha256'][:16]}.ttf"
    assert (scene_dir(root, "s1") / font.file).read_bytes() == data


def _text_project(tmp_path: Path):
    root = tmp_path / "proj"
    el = Element(id="e1", kind="text", canonical=Canonical(width=420, height=60, text="Sale", color="#ffffff",
                 font=FontGuess(family_guess="Brand Wide", weight=400, size_px=40, source="uploaded")), visible=(0, 4))
    scene = Scene(id="s1", size=(640, 160), fps=30, frames=5, background=Background(), elements=[el])
    init_project(root, {"file": "ref.mp4", "fps": 30, "size": [640, 160]}, scene)
    store_font(root, _write(tmp_path / "up.ttf", _ttf()), "brand.ttf")
    return root


def _fake_render(monkeypatch):
    from keepframe.ir.tracks import element_bbox
    from keepframe.render.renderer import RenderResult
    monkeypatch.setattr("keepframe.edit.agent.render", lambda html, scene, out_dir: RenderResult(
        frames_dir=out_dir / "frames", frames=list(range(scene.frames)), hashes=[],
        bboxes={e.id: [list(element_bbox(e, f)) for f in range(scene.frames)] for e in scene.elements}))


def test_text_edit_uses_uploaded_font(tmp_path, monkeypatch):
    from keepframe.edit import agent, apply
    from keepframe.edit.agent import edit
    from keepframe.edit.intent import Intent, Target
    from keepframe.fonts.raster import resolve_fonts
    root = _text_project(tmp_path)
    _fake_render(monkeypatch)
    used, composed = [], []
    real_write, real_compose = apply.write_text_texture, agent.compose

    def write(path, text, size, color, **kw):
        used.append(resolve_fonts(kw["font"], text, kw.get("fonts"), kw.get("scene_dir")).primary if kw.get("font") else None)
        return real_write(path, text, size, color, **kw)

    def compose(scene, sd, out, **kw):
        html = real_compose(scene, sd, out, **kw)
        composed.append(Path(html).read_text())
        return html

    monkeypatch.setattr(apply, "write_text_texture", write)
    monkeypatch.setattr(agent, "compose", compose)
    done = edit(root, "s1", "문구를 Mega로", confirm=True,
                intent=Intent(targets=[Target(element="e1", property="text", value="Mega")]))
    assert done.status == "done", done
    assert used and used[0] is not None and used[0].source == "uploaded" and used[0].family == "Brand Wide", used
    assert re.search(r"@font-face\{font-family:kf-brand-wide-[0-9a-f]{8};src:url\(data:font/woff2;base64,", composed[0])
    edited, _ = current_scene(root, "s1")
    assert edited.element("e1").canonical.text == "Mega"


def test_font_edit_to_uploaded_family_pins_the_face(tmp_path):
    from keepframe.edit.apply import apply_edit
    from keepframe.edit.intent import Target
    root = _text_project(tmp_path)
    scene, _ = current_scene(root, "s1")
    scene.element("e1").canonical.font = FontGuess(family_guess="Inter", weight=700, size_px=40, source="bundled")
    store_font(root, _write(tmp_path / "block.otf", _otf()), "block.otf")
    sd = scene_dir(root, "s1")
    out = apply_edit(scene, sd, [Target(element="e1", property="font", value="Brand Block")], {}, None,
                     fonts=FontRegistry.for_project(root))
    font = out.element("e1").canonical.font
    sha = hashlib.sha256(_otf()).hexdigest()
    assert (font.family_guess, font.source, font.file, font.postscript) == (
        "Brand Block", "uploaded", f"assets/font-{sha[:16]}.otf", "BrandBlock-Bold")
    assert (sd / font.file).read_bytes() == _otf()


def _styled_scene(tmp_path: Path):
    from keepframe.ir.schema import TextStyle
    sd = tmp_path / "scene"
    (sd / "assets").mkdir(parents=True)
    el = Element(id="e1", kind="text", canonical=Canonical(width=300, height=60, text="Sale", color="#ffffff", style=TextStyle(),
                 font=FontGuess(family_guess="Inter", weight=700, size_px=40, source="bundled")), visible=(0, 2))
    return Scene(id="s1", size=(320, 100), fps=30, frames=3, background=Background(), elements=[el]), sd


def test_final_compose_waits_long_and_fails_clearly(tmp_path, monkeypatch):
    """R39: a final render waits (>= 300 s) for every embedded face and fails with a clear error rather than baking
    a fallback; previews keep the short wait and fall back."""
    from concurrent.futures import Future
    from keepframe.compose.composer import compose
    from keepframe.fonts import css
    scene, sd = _styled_scene(tmp_path)
    assert css.FINAL_WAIT_S >= 300 and css.SUBSET_WAIT_S == 20
    monkeypatch.setattr(css, "_request", lambda *a, **k: Future())   # a subset that never finishes
    monkeypatch.setattr(css, "SUBSET_WAIT_S", 0.05)
    html = compose(scene, sd, sd / "preview.html")
    assert "@font-face" not in Path(html).read_text()
    with pytest.raises(css.FontEmbedError, match="Inter"):
        compose(scene, sd, sd / "final.html", font_wait=0.05)


def test_final_render_sites_pass_the_project_fonts_and_the_long_wait(tmp_path, monkeypatch):
    from keepframe.ae import verify as ae_verify
    from keepframe.fonts import css
    from keepframe.jobs import dispatch
    from keepframe.render import native
    from keepframe.render.plan import PlanConflict
    from tests.test_native_plan import _approved_plan, _project
    calls = []

    def fail(scene, sd, out, **kw):
        calls.append(kw)
        raise css.FontEmbedError("font Brand Wide could not be embedded for the final render (still subsetting)")

    root = _project(tmp_path)
    store_font(root, _write(tmp_path / "up.ttf", _ttf()), "brand.ttf")
    monkeypatch.setattr(native, "compose", fail)
    with pytest.raises(PlanConflict, match="Brand Wide could not be embedded"):
        native.prepare_native_job(root, _approved_plan(root, "final").id)
    monkeypatch.setattr(ae_verify, "compose", fail)
    scene, _ = current_scene(root, "s1")
    with pytest.raises(css.FontEmbedError):
        ae_verify.verify(scene, scene_dir(root, "s1"), {0: tmp_path / "f0.png"}, tmp_path / "out")
    monkeypatch.setattr("keepframe.compose.composer.compose", fail)
    with pytest.raises(css.FontEmbedError):
        dispatch._run_export({"scene": str(scene_dir(root, "s1") / "scene.v1.json"), "out": str(tmp_path / "x")})
    assert len(calls) == 3
    for kw in calls:
        assert kw["font_wait"] >= 300 and kw["fonts"].face("Brand Wide").source == "uploaded"


# --- After Effects ------------------------------------------------------------------------------------------------

def _ae_layer(font: FontGuess, fonts):
    from keepframe.ae.spec import comp_spec
    el = Element(id="e1", kind="text", canonical=Canonical(width=10, height=6, text="Sale", font=font), visible=(0, 5))
    spec = comp_spec(Scene(id="s1", size=(320, 180), fps=30, frames=6, background=Background(), elements=[el]), Path("."),
                     project="p", scene_id="s1", version="v1", fonts=fonts)
    return next(item for item in spec["layers"] if item["id"] == "kf:e1"), spec["warnings"]


def test_ae_postscript_match_wins():
    device = [{"family": "Brand Wide", "style": "Bold", "postscript": "BrandWide-Bold"},
              {"family": "BrandWide AE", "style": "Book", "postscript": "BrandWide-Regular"},
              {"family": "Inter", "style": "Bold", "postscript": "Inter-Bold"}]
    font = FontGuess(family_guess="Brand Wide", weight=700, source="uploaded", postscript="BrandWide-Regular",
                     candidates=["Brand Wide", "Inter"])
    layer, warnings = _ae_layer(font, device)
    assert layer["source"]["font"] == {"postscript": "BrandWide-Regular", "family": "BrandWide AE", "style": "Book",
                                       "substituted": False}
    assert warnings == [] and layer["warnings"] == []


def test_ae_missing_postscript_keeps_substitution_warning():
    font = FontGuess(family_guess="Brand Wide", weight=400, source="uploaded", postscript="BrandWide-Regular")
    layer, _ = _ae_layer(font, [{"family": "Inter", "style": "Regular", "postscript": "Inter-Regular"}])
    assert layer["source"]["font"] == {"postscript": "ArialMT", "family": "Arial", "style": "Regular", "substituted": True}
    assert layer["warnings"] == ["font Brand Wide not installed; using Arial Regular"]
    bold = FontGuess(family_guess="Brand Wide", weight=700, source="uploaded", postscript="BrandWide-Regular")
    layer, _ = _ae_layer(bold, [{"family": "Brand Wide", "style": "Bold", "postscript": "BrandWide-Bold"}])
    assert layer["source"]["font"]["postscript"] == "BrandWide-Bold"   # no exact name: the family match, as before


# --- fix round 1: R46 (no font bytes in ZIPs), R47 (gone uploads), R48 (hardening) ---------------------------------

def _font_free(zip_path: Path, data: bytes) -> list[str]:
    import zipfile
    with zipfile.ZipFile(zip_path) as zf:
        names = zf.namelist()
        assert not [n for n in names if n.startswith("fonts/") or re.search(r"(^|/)\.?font-[0-9a-f]{16}\.", n)
                    or n.lower().endswith((".ttf", ".otf", ".woff", ".woff2", ".ttc"))], names
        assert not [n for n in names if data[:256] in zf.read(n) or data[-256:] in zf.read(n)], names
    return names


def _native_project(tmp_path: Path, *, family="Brand Wide", pinned=True, approved=True):
    """test_native_plan's project whose v1 also has a text element in an uploaded family (its face stored and, when
    `pinned`, copied into the scene's assets)."""
    from keepframe.ir.store import save_scene
    from tests.test_native_plan import _project
    root = _project(tmp_path, approved=approved)
    entry, _ = store_font(root, _write(tmp_path / "up.ttf", _ttf()), "brand.ttf")
    scene, version = current_scene(root, "s1")
    sd = scene_dir(root, "s1")
    font = FontGuess(family_guess=family, size_px=40, source="uploaded",
                     file=scene_font_asset(sd, entry, root) if pinned else None)
    el = Element(id="t1", kind="text", canonical=Canonical(width=300, height=60, text="Brand", color="#ffffff",
                 font=font), visible=(0, scene.frames - 1))
    save_scene(scene.model_copy(update={"elements": [*scene.elements, el]}), root / version.scene_file)
    return root


def test_project_zips_carry_no_font_bytes(tmp_path, monkeypatch):
    """R46: neither the final render's Project ZIP (staged snapshot) nor a whole-root export carries the uploaded font
    (the composition embeds the subset)."""
    from keepframe.jobs import dispatch, run_job
    from keepframe.render.native import prepare_native_job
    from tests.test_native_plan import _approved_plan
    data = _ttf()
    root = _native_project(tmp_path)

    class Result:
        frames, mp4 = [0], None

    monkeypatch.setattr("keepframe.render.renderer.render", lambda *a, **k: Result())
    plan = _approved_plan(root, "final")
    spec = prepare_native_job(root, plan.id)
    assert (Path(spec.args["snapshot"]) / "scenes/s1/assets").is_dir()
    names = _font_free(Path(run_job(spec)["zip"]), data)
    assert "composition.html" in names and "scenes/s1/scene.v1.json" in names
    assert re.search(r"font-family:kf-brand-wide-[0-9a-f]{8};src:url\(data:font/woff2;base64,",
                     (Path(spec.args["snapshot"]) / "composition.html").read_text())
    whole = dispatch._run_export({"scene": str(scene_dir(root, "s1") / "scene.v1.json"), "out": str(tmp_path / "export")})
    names = _font_free(Path(whole["zip"]), data)
    assert "project.json" in names and not [n for n in names if n.endswith(".part")]


def test_reimported_project_without_font_file_falls_back_with_a_message(tmp_path, monkeypatch):
    """R46: a project unpacked from the ZIP keeps FontGuess.file pointing at a missing asset: plans still build, the
    composer takes the project's registry, and the render names what happened."""
    from keepframe.compose.composer import compose
    from keepframe.render.native import native_warnings, prepare_native_job
    from keepframe.render.plan import create_render_plan, approve_render_plan
    root = _native_project(tmp_path)
    for p in (scene_dir(root, "s1") / "assets").glob("font-*"):
        p.unlink()
    for p in (root / "fonts").iterdir():
        p.unlink()
    plan = create_render_plan(root, project_id="p1", scene_id="s1", version_id="v1", backend="native", mode="preview")
    assert not [a for a in plan.assets if "font-" in a.project_path]
    approve_render_plan(root, plan.id, digest=plan.digest, revision=0)
    prepare_native_job(root, plan.id)
    warnings = native_warnings(root, plan.id)
    assert [(w["kind"], w["family"]) for w in warnings] == [("font_substituted", "Brand Wide")], warnings
    store_font(root, _write(tmp_path / "again.ttf", _ttf()), "brand.ttf")   # uploaded again: the registry serves it
    scene, _ = current_scene(root, "s1")
    notes: list = []
    html = compose(scene, scene_dir(root, "s1"), tmp_path / "c.html", fonts=FontRegistry.for_project(root),
                   font_wait=300, warnings=notes)
    assert [w["kind"] for w in notes] == ["font_file_missing"] and "kf-brand-wide-" in Path(html).read_text()


def test_final_render_fails_when_an_uploaded_font_is_gone(tmp_path):
    """R47: an uploaded family that no longer resolves to an uploaded face stops a final render with its name; the
    preview composition falls back and reports the stand-in."""
    from keepframe.compose.composer import compose
    from keepframe.fonts import css
    from keepframe.render.native import prepare_native_job
    from keepframe.render.plan import PlanConflict
    from tests.test_native_plan import _approved_plan
    root = _native_project(tmp_path, family="Ghost Brand", pinned=False)
    scene, _ = current_scene(root, "s1")
    with pytest.raises(css.FontEmbedError, match="Ghost Brand"):
        compose(scene, scene_dir(root, "s1"), tmp_path / "f.html", fonts=FontRegistry.for_project(root), font_wait=300)
    notes: list = []
    compose(scene, scene_dir(root, "s1"), tmp_path / "p.html", fonts=FontRegistry.for_project(root), warnings=notes)
    assert notes == [{"kind": "font_substituted", "element": "t1", "family": "Ghost Brand", "used": notes[0]["used"]}]
    assert notes[0]["used"] and notes[0]["used"] != "Ghost Brand"
    with pytest.raises(PlanConflict, match="Ghost Brand"):
        prepare_native_job(root, _approved_plan(root, "final").id)


def test_preview_plan_warns_when_an_uploaded_font_is_gone(tmp_path):
    """R47: a preview-mode native plan renders with the stand-in and its state names the substituted family."""
    from keepframe.render.native import prepare_native_job
    from keepframe.web.server import _render_state_payload
    from tests.test_native_plan import _approved_plan
    root = _native_project(tmp_path, family="Ghost Brand", pinned=False, approved=False)
    plan = _approved_plan(root, "preview")
    prepare_native_job(root, plan.id)
    prepare_native_job(root, plan.id)   # the published stage is reused: the warning stays
    warnings = _render_state_payload(root, plan)["warnings"]
    assert [(w["kind"], w["family"]) for w in warnings] == [("font_substituted", "Ghost Brand")] and warnings[0]["used"]


def test_unexpected_upload_failures_answer_bad_tables(server, monkeypatch):
    """R48: nothing in the receive/store path drops the connection; only a short body is `incomplete`."""
    import errno
    from keepframe.web import server as web
    srv, ws = server
    data = _ttf()

    def boom(*a, **k):
        raise RuntimeError("surprise")

    monkeypatch.setattr(web, "store_font", boom)
    assert _upload(srv, data) == (400, {"error": "bad_tables"})
    monkeypatch.setattr(web, "receive_font", lambda *a, **k: (_ for _ in ()).throw(OSError(errno.ENOSPC, "No space")))
    assert _upload(srv, data) == (400, {"error": "bad_tables"})
    monkeypatch.undo()
    import shutil
    shutil.rmtree(ws / "p1" / "fonts")
    (ws / "p1" / "fonts").write_text("not a directory")
    assert _upload(srv, data) == (400, {"error": "bad_tables"})
    (ws / "p1" / "fonts").unlink()
    port = srv.server_address[1]
    with socket.create_connection(("127.0.0.1", port), timeout=10) as s:
        s.sendall((f"POST /api/projects/p1/fonts?name=short.ttf HTTP/1.1\r\nHost: 127.0.0.1:{port}\r\n"
                   f"Origin: http://127.0.0.1:{port}\r\nContent-Length: 5000\r\n\r\n").encode() + data[:100])
        s.shutdown(socket.SHUT_WR)
        out = b""
        while chunk := s.recv(65536):
            out += chunk
    assert out.split(b"\r\n", 1)[0].split(b" ")[1] == b"400"
    assert json.loads(out.split(b"\r\n\r\n", 1)[1]) == {"error": "incomplete"}
    assert _stray(ws) == []


def test_brotli_with_bounded_output_is_required():
    import brotli
    assert '"brotli>=1.2"' in Path("pyproject.toml").read_text()
    assert hasattr(brotli.Decompressor(), "can_accept_more_data")


def test_check_child_is_confined(tmp_path, monkeypatch):
    """R48: the check child gets a minimal environment, no stdin, CPU/file/size limits and bounded output; the parent
    re-validates what it reports."""
    import subprocess
    from keepframe.fonts import upload
    monkeypatch.setenv("OPENAI_API_KEY", "sk-secret-value")
    monkeypatch.setenv("KEEPFRAME_ADMIN_TOKEN", "tok-secret-value")
    seen = []
    real = subprocess.run

    def spy(cmd, **kw):
        seen.append(kw)
        return real(cmd, **kw)

    monkeypatch.setattr(upload.subprocess, "run", spy)
    path = _write(tmp_path / "brand.ttf", _ttf())
    assert inspect_font(path, "brand.ttf")["family"] == "Brand Wide"
    kw = seen[0]
    assert kw["stdin"] == subprocess.DEVNULL and "secret-value" not in json.dumps(kw["env"])
    assert set(kw["env"]) <= {"PATH", "PYTHONPATH", "PYTHONDONTWRITEBYTECODE", "PYTHONNOUSERSITE", "LC_ALL", "LANG",
                              "SYSTEMROOT"}
    limits = real([__import__("sys").executable, "-P", "-c",
                   "from keepframe.fonts.upload import _confine; _confine(); import resource as r; "
                   "print([r.getrlimit(x)[0] for x in (r.RLIMIT_AS, r.RLIMIT_CPU, r.RLIMIT_NOFILE, r.RLIMIT_FSIZE)])"],
                  capture_output=True, text=True, env=upload._child_env(), stdin=subprocess.DEVNULL)
    mem, cpu, files, size = json.loads(limits.stdout)
    assert mem == upload.INSPECT_MEMORY and 0 < cpu <= upload.INSPECT_TIMEOUT_S and 0 < files <= 64
    assert 0 < size <= upload.CHILD_OUTPUT_BYTES
    # a child that floods stderr and stdout stays bounded and is refused
    monkeypatch.setattr(upload, "_CHILD_CODE", "import sys; sys.stdout.write('y' * 50_000_000); "
                        "open(sys.argv[2], 'w').write('{\"meta\": {}}' + ' ' * 50_000_000)")
    with pytest.raises(FontRejected) as err:
        upload._inspect_child(path)
    assert err.value.code == "bad_tables"
    for bad in ({"original_family": 5}, {"weight_range": [900, 100]}, {"postscript": "bad name!"}, {"category": "x"},
                {"latin": "yes"}, {"latin": False, "hangul": False}, {"style": "s" * 500}):
        meta = {"original_family": "Brand", "style": "Regular", "weight_range": [400, 400], "postscript": None,
                "category": "serif", "latin": True, "hangul": False, **bad}
        monkeypatch.setattr(upload, "_CHILD_CODE", f"import json, sys; open(sys.argv[2], 'w').write(json.dumps({{'meta': {meta!r}}}))")
        with pytest.raises(FontRejected) as err:
            upload._inspect_child(path)
        assert err.value.code == "bad_tables", bad


@pytest.mark.parametrize("change", ["upm_small", "upm_large", "head_bbox", "glyph_bbox"])
def test_absurd_metrics_rejected(tmp_path, change):
    """R48: unitsPerEm outside 16-16384 or outlines far outside the em are refused (glyphs are later rasterised
    in-process)."""
    from fontTools.ttLib import TTFont
    font = TTFont(io.BytesIO(_ttf()))
    font.recalcBBoxes = font.recalcTimestamp = False
    head = font["head"]
    if change == "upm_small":
        head.unitsPerEm = 8
    elif change == "upm_large":
        head.unitsPerEm = 20000
    elif change == "head_bbox":
        head.yMax = min(32000, head.unitsPerEm * 12)
    else:
        glyph = font["glyf"]["A"]
        glyph.yMax = min(32000, head.unitsPerEm * 12)
    buf = io.BytesIO()
    font.save(buf)
    with pytest.raises(FontRejected) as err:
        inspect_font(_write(tmp_path / "x.ttf", buf.getvalue()), "x.ttf")
    assert err.value.code == "bad_tables"


def test_stale_temporaries_are_swept(tmp_path):
    """R48: abandoned upload parts and asset copies older than an hour go on the next upload / copy."""
    import os
    import time as _time
    root = tmp_path / "proj"
    fonts = root / "fonts"
    fonts.mkdir(parents=True)
    old, fresh = fonts / ".upload-old.part", fonts / ".upload-fresh.part"
    assets = root / "scenes" / "s1" / "assets"
    assets.mkdir(parents=True)
    stale_copy = assets / ".font-0123456789abcdef.ttf.x.tmp"
    for p in (old, fresh, stale_copy):
        p.write_bytes(b"x")
    hours_ago = _time.time() - 7200
    os.utime(old, (hours_ago, hours_ago))
    os.utime(stale_copy, (hours_ago, hours_ago))
    entry, _ = store_font(root, _write(tmp_path / "up.ttf", _ttf()), "brand.ttf")
    from keepframe.fonts.upload import receive
    receive(root, io.BytesIO(b"abc"), 3).unlink()
    assert not old.exists() and fresh.exists()
    scene_font_asset(assets.parent, entry, root)
    assert not stale_copy.exists()


def test_promoted_assets_skip_dotfiles(tmp_path):
    from keepframe.edit.agent import _promote_assets
    src, dest = tmp_path / "cand", tmp_path / "scene" / "assets"
    (src / "assets").mkdir(parents=True)
    for name in ("e1.txt1.png", ".font-abc.ttf.1.tmp", ".hidden"):
        (src / "assets" / name).write_bytes(b"x")
    (src / "assets" / "subdir").mkdir()
    _promote_assets(src, dest, set())
    assert sorted(p.name for p in dest.iterdir()) == ["e1.txt1.png"]



# --- fix round 2: R49 ---------------------------------------------------------------------------------------------

def test_font_with_fonttools_warnings_is_accepted(tmp_path, monkeypatch):
    """R49: fontTools logging a warning about a real-world quirk (extra OS/2 bytes) does not reach the result channel;
    a crashing check names its stderr tail in the reason."""
    from fontTools.ttLib import TTFont
    from fontTools.ttLib.tables.DefaultTable import DefaultTable
    from keepframe.fonts import upload
    font = TTFont(io.BytesIO(_ttf()))
    extra = DefaultTable("OS/2")
    extra.data = font.reader["OS/2"] + b"\0" * 16
    font["OS/2"] = extra
    buf = io.BytesIO()
    font.save(buf)
    meta = inspect_font(_write(tmp_path / "quirky.ttf", buf.getvalue()), "quirky.ttf")
    assert meta["family"] == "Brand Wide" and meta["latin"]
    monkeypatch.setattr(upload, "_CHILD_CODE", "import sys; sys.stderr.write('decoder-exploded-here'); sys.exit(3)")
    with pytest.raises(FontRejected) as err:
        upload._inspect_child(tmp_path / "quirky.ttf")
    assert err.value.code == "bad_tables" and "exit 3" in err.value.reason and "decoder-exploded-here" in err.value.reason


def test_storage_failures_answer_large_uploads(server, monkeypatch):
    """R49: when the font cannot be stored (fonts is a file; disk full mid-stream) the rest of a multi-MiB body is read
    and dropped so the client gets the JSON answer, not a reset."""
    import errno
    import shutil
    from keepframe.fonts import upload
    srv, ws = server
    data = _ttf() + b"\0" * (8 * 2**20)
    shutil.rmtree(ws / "p1" / "fonts", ignore_errors=True)
    (ws / "p1" / "fonts").write_text("not a directory")
    assert [_upload(srv, data) for _ in range(3)] == [(400, {"error": "bad_tables"})] * 3
    (ws / "p1" / "fonts").unlink()

    class Full:
        def __init__(self, fd):
            self.f = open(fd, "wb")

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            self.f.close()

        def write(self, chunk):
            if self.f.tell() >= 2**20:
                raise OSError(errno.ENOSPC, "No space left on device")
            return self.f.write(chunk)

    monkeypatch.setattr(upload, "_part_writer", Full)
    assert [_upload(srv, data) for _ in range(3)] == [(400, {"error": "bad_tables"})] * 3
    assert _stray(ws) == []


def test_zip_filter_takes_only_uploaded_font_files(tmp_path):
    """R49: the R46 filter applies to files: font-<sha16> copies, their temporaries, font suffixes and the project's
    fonts/ — a font-preview.png or a scene called font-demo survive."""
    import zipfile
    from keepframe.jobs.dispatch import project_zip
    root = tmp_path / "p1"
    keep = ["project.json", "scenes/s1/assets/font-preview.png", "scenes/font-demo/scene.v1.json",
            "scenes/font-demo/assets/e1.png", "scenes/s1/fonts.json", "renders/x/native/snapshot/fonts/index.json"]
    drop = ["fonts/index.json", "fonts/" + "a" * 64 + ".ttf", "scenes/s1/assets/font-0123456789abcdef.ttf",
            "scenes/s1/assets/font-0123456789abcdef.woff2", "scenes/s1/assets/.font-0123456789abcdef.otf.x1.tmp",
            "scenes/s1/assets/Brand.TTF", "scenes/font-demo/assets/x.Woff2", "scenes/s1/assets/legacy.ttc"]
    for rel in keep + drop:
        (root / rel).parent.mkdir(parents=True, exist_ok=True)
        (root / rel).write_bytes(b"x")
    project_zip(root, tmp_path / "out.zip")
    with zipfile.ZipFile(tmp_path / "out.zip") as zf:
        assert sorted(zf.namelist()) == sorted(keep)



# --- fix round 3: R50 (nothing internal reaches a client; logs keep a path-free reason) -----------------------------

SECRET = "/home/secret-operator/keepframe-ws"


def _raw_post(srv, data: bytes, name="brand.ttf") -> bytes:
    """The response body bytes of a font upload, exactly as the browser gets them."""
    base = _base(srv)
    try:
        with urlopen(Request(f"{base}/api/projects/p1/fonts?name={name}", data=data, method="POST",
                             headers={"Origin": base})) as r:
            return r.read()
    except HTTPError as e:
        return e.read()


def test_rejection_details_stay_in_scrubbed_server_logs(server, monkeypatch, caplog):
    """R50: a check that dies with a traceback full of server paths answers the code only; the index never sees it;
    the server log keeps the reason with paths shortened."""
    import logging
    from keepframe.fonts import upload
    from keepframe.web import server as web
    srv, ws = server
    trace = (f'Traceback (most recent call last):\n  File "{SECRET}/p1/fonts/.upload-abc.part", line 1\n'
             f"OSError: [Errno 5] {ws}/p1/fonts/x /tmp/kf-fontcheck-zz/result.json")
    monkeypatch.setattr(upload, "_CHILD_CODE", f"import sys; sys.stderr.write({trace!r}); sys.exit(1)")
    caplog.set_level(logging.INFO)
    assert _raw_post(srv, _ttf()) == b'{"error": "bad_tables"}'
    assert not (ws / "p1" / "fonts" / "index.json").exists()
    rejected = [r.getMessage() for r in caplog.records if "rejected bad_tables" in r.getMessage()]
    assert rejected and "exit 1" in rejected[0] and "…/result.json" in rejected[0], rejected
    log_text = "\n".join(r.getMessage() for r in caplog.records)
    assert SECRET not in log_text and str(ws) not in log_text and "/tmp/kf-fontcheck" not in log_text
    assert str(FontRejected("bad_tables", f"{SECRET}/x.ttf")) == "bad_tables"
    assert SECRET not in FontRejected("bad_tables", f"read {SECRET}/x.ttf").reason

    def broken(*a, **k):
        raise OSError(28, "No space left on device", f"{ws}/p1/fonts/.upload-zz.part")

    monkeypatch.setattr(web, "store_font", broken)
    caplog.clear()
    assert _raw_post(srv, _ttf()) == b'{"error": "bad_tables"}'
    log_text = "\n".join(r.getMessage() for r in caplog.records)
    assert "failed" in log_text and str(ws) not in log_text and "…/.upload-zz.part" in log_text


def test_font_metadata_reaching_clients_is_sanitised(server):
    """R50: the browser gets the public fields only (codes and sanitised names); the index keeps the font's own name
    without server-looking paths."""
    srv, ws = server
    data = _ttf(family="Brand " + SECRET + "/x", style="Bold/../../etc<script>")
    code, body = _upload(srv, data, "brand.ttf")
    assert code == 201
    font = body["font"]
    assert set(font) == {"family", "style", "weight_range", "postscript", "category", "ext", "sha256", "bytes",
                         "latin", "hangul"}
    assert "/" not in font["family"] + font["style"] and "<" not in font["style"] and font["postscript"] is None
    _, listing = _get(srv, "/api/projects/p1/fonts")
    assert json.loads(listing)["fonts"] == [font] and SECRET.encode() not in listing
    index = (ws / "p1" / "fonts" / "index.json").read_text()
    assert SECRET not in index and "…/x" in json.loads(index)["fonts"][0]["original_family"]


def test_font_messages_reaching_clients_carry_no_paths(tmp_path, monkeypatch):
    """R50: report messages, final-render errors and render warnings about fonts name families, never paths."""
    from concurrent.futures import Future
    from keepframe.analyze.pipeline import _texture_messages
    from keepframe.compose.composer import compose
    from keepframe.fonts import css
    msgs = _texture_messages({"o1": {"font_error": f"FileNotFoundError: [Errno 2] No such file: '{SECRET}/p1/fonts/a.ttf'",
                                     "font": None}}, {"o1": "e1"})
    assert msgs and SECRET not in msgs[0] and msgs[0].startswith("e1: font match failed")
    scene, sd = _styled_scene(tmp_path)

    def failing(*a, **k):
        f = Future()
        f.set_exception(OSError(2, "No such file or directory", f"{SECRET}/cache/x.woff2"))
        return f

    monkeypatch.setattr(css, "_request", failing)
    with pytest.raises(css.FontEmbedError) as err:
        compose(scene, sd, sd / "final.html", font_wait=5)
    assert "Inter" in str(err.value) and SECRET not in str(err.value) and "cache" not in str(err.value)
    root = _native_project(tmp_path / "n")
    for p in (scene_dir(root, "s1") / "assets").glob("font-*"):
        p.unlink()
    scene, _ = current_scene(root, "s1")
    issues = css.uploaded_font_issues(scene, scene_dir(root, "s1"), FontRegistry.for_project(root))
    assert issues == [{"kind": "font_file_missing", "element": "t1", "family": "Brand Wide"}]


# --- fix round 4: R51 (client-visible analysis/job text carries codes only; log tails and the scrubber hardened) -----

BYPASS = [   # every path form the round-3 scrubber let through (R51)
    repr(r"C:\Users\op\keepframe-ws\p1\fonts\a.ttf"),
    "'/Users/John Doe/keepframe-ws/p1/fonts/a.ttf'",
    "error:/home/secret-operator/keepframe-ws/a.ttf",
    "PATH=/usr/bin:/home/secret-operator/bin",
    r"\\fileserver\share\secret-operator\a.ttf",
    "~/secret-operator/keepframe-ws/a.ttf",
    "../../home/secret-operator/keepframe-ws/a.ttf",
    "file:///home/secret-operator/keepframe-ws/a.html",
    "%2Fhome%2Fsecret-operator%2Fkeepframe-ws%2Fa.ttf",
    '{"p": "\\/home\\/secret-operator\\/keepframe-ws\\/a.ttf"}',
    "keepframe-ws/p1/fonts/a.ttf",
]
LEAKS = ("secret-operator", "keepframe-ws", "John Doe", "Users", "fileserver", "share", "p1/", "usr/")


def _leaks(text: str) -> list[str]:
    return [frag for frag in LEAKS if frag in text]


def test_font_and_style_failures_reach_clients_as_codes_only(tmp_path, monkeypatch, caplog):
    """R51: a font match or text style failure whose exception carries any path form leaves a code in the stage
    data and a fixed report message (family only); the detail goes to the server log, scrubbed."""
    import logging
    import pickle
    from keepframe.analyze import textstyle
    from keepframe.analyze.pipeline import AnalyzeOptions, _stage_sprites, _texture_messages
    from keepframe.analyze.text import TextBox, TextTrack
    from keepframe.fonts import match as fontmatch
    detail = "No such file or directory: " + " ".join(BYPASS)

    def font_boom(*a, **k):
        raise FileNotFoundError(2, detail, r"C:\Users\op\keepframe-ws\p1\fonts\b.ttf")

    def style_boom(*a, **k):
        raise OSError(detail)

    frames = np.zeros((1, 30, 50, 3), np.uint8)
    frames[0, 8:12, 10:24] = 255
    track = TextTrack(id=1, boxes={0: TextBox(0, "Text", (5, 3, 35, 23), 0.9)}, text="Text")
    caplog.set_level(logging.INFO)
    for target, name, boom in ((fontmatch, "match_font", font_boom), (textstyle, "analyse_text_style", style_boom),
                               (fontmatch, "font_guesses", font_boom)):
        with monkeypatch.context() as m:
            m.setattr(target, name, boom)
            sd = tmp_path / name
            (sd / "stages").mkdir(parents=True)
            props = _stage_sprites(frames, (0, 0, 0), [track], [], [], AnalyzeOptions(refine=False), sd, 1)
        p = props["t1"]
        stored = (sd / "stages/props.pkl").read_bytes()
        assert not _leaks(stored.decode("latin-1")), name
        msgs = [x for x in _texture_messages(props, {"t1": "e1"}) if "font" in x or "style" in x]
        if name == "analyse_text_style":
            assert p["style_error"] == "style_failed" and "font_error" not in p
            assert msgs == ["e1: text style failed; kept the core-mask colour"], msgs
        else:
            assert p["font_error"] == "match_failed" and "style_error" not in p
            assert msgs == ["e1: font match failed; kept sans-serif"], msgs
        assert pickle.loads(stored)["t1"].get("font_error") == p.get("font_error")
    assert "No such file or directory" in caplog.text and not _leaks(caplog.text), _leaks(caplog.text)
    legacy = {"o1": {"font_error": f"FileNotFoundError: [Errno 2] {detail}", "style_error": f"OSError: {detail}",
                     "font": FontGuess(family_guess="Inter", size_px=20)}}
    assert _texture_messages(legacy, {"o1": "e1"}) == ["e1: text style failed; kept the core-mask colour",
                                                       "e1: font match failed; kept Inter"]
    skipped = {"o1": {"font_skipped": "work_cap", "font": None}}
    assert _texture_messages(skipped, {"o1": "e1"}) == ["e1: font not matched (scene work cap reached); kept sans-serif"]


def test_check_log_tail_starting_mid_path_leaks_no_fragment(monkeypatch, caplog):
    """R51: the check child's log tail is read from an arbitrary byte offset; whatever the offset, the reason and
    the log line keep no fragment of a path (the partial first line is dropped, the whole tail is scrubbed before
    it is cut) and keep the end of the log, where the error is."""
    import logging
    import subprocess
    import types
    from keepframe.fonts import upload
    path = f"{SECRET}/p1/fonts/.upload-abc.part"
    caplog.set_level(logging.INFO)
    for pad in range(0, len(path) + 2, 3):
        payload = ("\n".join([path] * 80) + "\n" + "B" * pad + "\nOSError: boom\n").encode()

        def run(cmd, *, stdout, **kw):
            stdout.write(payload)
            return subprocess.CompletedProcess(cmd, 1)

        monkeypatch.setattr(upload, "subprocess", types.SimpleNamespace(
            run=run, DEVNULL=subprocess.DEVNULL, TimeoutExpired=subprocess.TimeoutExpired))
        caplog.clear()
        with pytest.raises(FontRejected) as err:
            upload._inspect_child(Path("/nonexistent.ttf"))
        for text in [err.value.reason, *(r.getMessage() for r in caplog.records)]:
            rest = text.replace("…/.upload-abc.part", "")
            assert not re.search(r"secret|operator|keepframe|ws/|p1|fonts|/", rest), (pad, text)
        assert "OSError: boom" in err.value.reason, (pad, err.value.reason)


@pytest.mark.parametrize("text, expected", [
    ("No such file: '/Users/John Doe/keepframe-ws/p1/a.ttf'", "No such file: '…/a.ttf'"),
    (repr(r"C:\Users\op\keepframe-ws\a.ttf"), "'…/a.ttf'"),
    (str(FileNotFoundError(2, "No such file or directory", r"C:\Users\op\ws\a.ttf")),
     "[Errno 2] No such file or directory: '…/a.ttf'"),
    ("error:/home/secret-operator/keepframe-ws/a.ttf", "error:…/a.ttf"),
    ("PATH=/usr/bin:/home/secret-operator/bin", "PATH=…/bin"),
    (r"\\fileserver\share\secret-operator\a.ttf", "…/a.ttf"),
    ("~/secret-operator/keepframe-ws/a.ttf", "…/a.ttf"),
    ("x=~/secret-operator/a.ttf", "x=…/a.ttf"),
    ("../../home/secret-operator/a.ttf", "…/a.ttf"),
    ("file:///home/secret-operator/keepframe-ws/a.html", "file://…/a.html"),
    ("%2Fhome%2Fsecret-operator%2Fa.ttf", "…/a.ttf"),
    ('{"p": "\\/home\\/secret-operator\\/a.ttf"}', '{"p": "…/a.ttf"}'),
    ("keepframe-ws/p1/fonts/a.ttf", "…/a.ttf"),
    ("me/secret-operator/keepframe-ws/p1/fonts/.upload-abc.part x", "…/.upload-abc.part x"),
    ("/home/x/My Fonts (copy)/a.ttf", "…/a.ttf"),
    ("/home/secret-operator/한글 폰트/a.ttf", "…/a.ttf"),
    ('File "/home/secret-operator/venv/lib/fontTools/ttFont.py", line 3', 'File "…/ttFont.py", line 3'),
    ("copy /tmp/a/x.ttf to /home/secret-operator/y.ttf", "copy …/x.ttf to …/y.ttf"),
    ("Brand /home/x", "Brand …/x"),
    ("--workspace=/home/secret-operator/ws", "--workspace=…/ws"),
    # ordinary log text and URLs stay as they are
    ("https://example.com/a/b?x=1", None), ("see http://localhost:8765/api/projects/p1 now", None),
    ("example.com/a/b", None), ("AC/DC Sans", None), ("Font 8/16", None), ("image/png", None),
    ("2026/10/09", None), ("a / b", None), ("…/x.ttf", None), ("exit 1: …/result.json", None),
])
def test_scrub_paths_cuts_every_path_form(text, expected):
    """R51 (defence in depth for the server log): paths with spaces, Windows repr, colon-prefixed, UNC, ~, ../,
    file://, URL-encoded, JSON-escaped and relative workspace paths are cut to their last part; URLs and ordinary
    text are kept."""
    from keepframe.log import scrub_paths
    assert scrub_paths(text) == (text if expected is None else expected)


@pytest.mark.parametrize("method, error", [
    ("write", PermissionError(13, "Permission denied", f"{SECRET}/p1/scenes/s1/a.png")),
    ("write", FileNotFoundError(2, "No such file or directory", f"{SECRET}/p1/scenes/s1/gone.png")),
    ("close", OSError(28, "No space left on device", f"{SECRET}/p1/renders/project.zip")),
], ids=["permission", "vanished", "disk_full"])
def test_project_zip_failure_is_a_code_only_job_error(tmp_path, monkeypatch, caplog, method, error):
    """R51: a Project ZIP that cannot be written fails the export job with the code `export_failed` (what the
    render card shows); the detail is in the server log without paths; no partial ZIP is left."""
    import logging
    import time
    import zipfile
    from keepframe.jobs import JobSpec, JobStore, ThreadRunner
    from tests.test_native_plan import _project
    root = _project(tmp_path)
    html = tmp_path / "c.html"
    html.write_text("<html></html>")

    class Result:
        frames, mp4 = [0], None

    def fail(self, *a, **k):
        raise error

    monkeypatch.setattr("keepframe.render.renderer.render", lambda *a, **k: Result())
    monkeypatch.setattr(zipfile.ZipFile, method, fail)
    caplog.set_level(logging.INFO)
    out = tmp_path / "export"
    store = JobStore(runner=ThreadRunner())
    job = store.submit("export", project_id="p1", spec=JobSpec(kind="export", args={
        "scene": str(scene_dir(root, "s1") / "scene.v1.json"), "html": str(html), "out": str(out)}))
    for _ in range(200):
        if store.get(job.id).status in ("done", "error"):
            break
        time.sleep(0.05)
    monkeypatch.undo()
    job = store.get(job.id)
    assert job.status == "error" and job.error == "export_failed", job.error
    failure = "\n".join(r.getMessage() for r in caplog.records if "failed" in r.getMessage())
    assert "project zip failed" in failure and type(error).__name__ in failure and not _leaks(failure), failure
    assert not [p.name for p in out.iterdir() if "zip" in p.name], list(out.iterdir())


def test_project_font_cap_refuses_a_new_font_but_not_a_duplicate(server, monkeypatch):
    from keepframe.fonts import upload
    srv, _ = server
    monkeypatch.setattr(upload, "MAX_PROJECT_FONTS", 1)
    first = _ttf()
    assert _upload(srv, first)[0] == 201
    assert _upload(srv, _ttf(factor=1.4)) == (409, {"error": "too_many_fonts"})
    assert _upload(srv, first)[0] == 200
