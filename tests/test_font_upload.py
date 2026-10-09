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
from keepframe.fonts.upload import MAX_FONT_BYTES, FontRejected, inspect_font, list_fonts, scene_font_asset, store_font
from keepframe.ir.schema import Background, Canonical, Element, FontGuess, Scene
from keepframe.ir.store import current_scene, init_project, scene_dir
from tests.test_web_server import start

REG = FontRegistry()


# --- font bytes ---------------------------------------------------------------------------------------------------

@functools.lru_cache(maxsize=None)
def _ttf(family="Brand Wide", *, factor=1.3, flavor=None, source="Varela Round", drop=()) -> bytes:
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
        for nid, value in ((1, family), (2, "Regular"), (3, f"{family};test"), (4, f"{family} Regular"),
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
    assert first["font"] == {"family": "Brand Wide", "original_family": "Brand Wide", "style": "Regular",
                             "weight_range": [400, 400], "postscript": "BrandWide-Regular", "category": first["font"]["category"],
                             "ext": "ttf", "sha256": sha, "bytes": len(data), "latin": True, "hangul": False,
                             "file": f"{sha}.ttf"}
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
    assert code == 200 and json.loads(body)["fonts"] == index["fonts"] == list_fonts(root)
    reg = FontRegistry.for_project(root)
    assert reg.face("Brand Wide").source == "uploaded" and reg.face("Brand Block", 700).weight_range == (700, 700)
    entry, created = store_font(root, _write(root.parent / "dup.ttf", data), "dup.ttf")
    assert entry == first["font"] and created is False and not (root.parent / "dup.ttf").exists()


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
