# Keepframe M1+M2 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build the deterministic core of Keepframe: a JSON IR for motion-graphics scenes, an HTML/GSAP composer, a frame-seeking renderer, a MoVer-style verifier (M1), and an analyzer that turns a flat 2D motion-graphics clip (range mode) into that IR plus the four review corrections (M2).

**Architecture:** One Python package `keepframe` with five sub-packages: `ir` (schema, track math, store, synthetic fixtures), `compose` (IR → self-contained HTML with a GSAP timeline and `window.__seek`), `render` (Playwright frame seeking → PNG frames → MP4, per-frame hashes, element bbox probe), `verify` (animation matrix, predicates, similarity, report) and `analyze` (video → background → text → regions → tracking → sprites → keyframes → constraints → report) plus `review` (four correction ops that re-run the pipeline from a stage with overrides). Every stage is a pure function over the IR; the only nondeterministic component (LLM edit agent) is out of scope for M1+M2.

**Tech Stack:** Python 3.11+, pydantic v2, numpy, scipy, opencv-python-headless, playwright (Chromium), GSAP 3.12 + CustomEase (vendored, inlined into HTML), ffmpeg CLI, torch (optional, for sprite refinement), rapidocr-onnxruntime (optional, for OCR), pytest.

**Spec:** `docs/superpowers/specs/2026-09-05-keepframe-design.md` (read §4 IR, §5 analyzer, §6 corrections, §8 render, §10 evaluation, §11 milestones first).

## Global Constraints

- Python ≥ 3.11. Package name `keepframe`. Tests with `pytest`; browser tests marked `@pytest.mark.browser`, OCR tests `@pytest.mark.ocr`, GPU-optional tests `@pytest.mark.gpu`.
- IR schema ids are exactly `keepframe.project/1` and `keepframe.scene/1` (spec §4).
- L0 raw measurements are never deleted (spec §4 invariant 1). Tracks use cubic-bezier easing; fit error bound is 2 px or 1 frame (invariant 2).
- Versions are append-only (invariant 4). Corrections are exactly four ops (spec §6).
- Renderer must be deterministic: same IR → same per-frame hashes (spec §8).
- Keep-predicates are mandatory verifier checks (invariant 3). Verifier repair loop cap is 4, asset generation cap is 2 (spec §9) — not implemented here, but do not hard-code anything that prevents it.
- VLM/LLM is used only for semantics (captions, roles, groups) and is OFF by default in M2 (`NullCaptioner`). Timing and position are always measured.
- Coordinate convention (used everywhere): scene pixel coordinates, origin top-left, y down. Element transform `M = T(x,y) · R(rot°) · SkewX(skx°) · S(sx,sy)` applied about the element anchor; `(x,y)` is where the anchor lands. Rotation positive = clockwise on screen (CSS convention).
- Commit after every task with the messages given. Never commit `.venv`, frames, or MP4s (add to `.gitignore` in Task 1).

---

## File Structure

```
pyproject.toml                       deps, pytest markers
.gitignore
keepframe/__init__.py
keepframe/ir/schema.py               pydantic models (Keyframe, Track, Element, Scene, Project, ...)
keepframe/ir/tracks.py               cubic-bezier, eval_track, affine matrix/decompose, bbox
keepframe/ir/store.py                project dir layout, save/load, append-only versions
keepframe/ir/synth.py                synthetic scenes + textures (fixtures + golden IR)
keepframe/compose/vendor/gsap.min.js, CustomEase.min.js   (vendored, inlined)
keepframe/compose/template.html      HTML skeleton with __seek/__bbox
keepframe/compose/composer.py        Scene → composition.html
keepframe/render/renderer.py         Playwright frame seek, hashes, bbox probe, ffmpeg
keepframe/analyze/composite.py       numpy compositor (reference implementation of the render model)
keepframe/verify/matrix.py           animation matrix + motion intervals
keepframe/verify/predicates.py       predicate parser/evaluator (MoVer subset)
keepframe/verify/similarity.py       temporal (displacement correlation) + appearance
keepframe/verify/verifier.py         VerifyReport
keepframe/analyze/constraints.py     tracks → Constraint list (strings the verifier can parse)
keepframe/analyze/video.py           frames in/out (cv2)
keepframe/analyze/background.py      LAB mode-colour background
keepframe/analyze/text.py            OCR + text tracking + optional copy-guided correction
keepframe/analyze/regions.py         colour clusters + connected components
keepframe/analyze/tracking.py        Hungarian region→object matching, ids
keepframe/analyze/sprites.py         canonical texture, per-frame affine (moments + ECC), opacity, z
keepframe/analyze/refine.py          torch affine refinement (optional)
keepframe/analyze/keyframes.py       keyframe reduction + ease fit
keepframe/analyze/semantics.py       heuristic roles/groups + Captioner protocol
keepframe/analyze/report.py          reconstruction error, confidences
keepframe/analyze/pipeline.py        analyze(video, start, end) → Project; stage cache + overrides
keepframe/analyze/golden.py          compare analyzed scene to golden synthetic scene
keepframe/review/corrections.py      reassign_id, set_region_mask, add_bbox_prompt, edit_text
keepframe/review/server.py           stdlib http.server: state, frames, keep toggles, correction jobs (Task 26)
keepframe/review/ui.html             single-file bilingual review screen; design rules in DESIGN.md (Task 27)
keepframe/cli.py                     synth / compose / render / verify / analyze / correct / review / gate
scripts/m1_gate.py, scripts/m2_gate.py
tests/...                            one test file per module
```

---

## M1 — IR, Composer, Renderer, Verifier

### Task 1: Project scaffold and vendored GSAP

**Files:**
- Create: `pyproject.toml`, `.gitignore`, `keepframe/__init__.py`, `keepframe/ir/__init__.py`, `keepframe/compose/__init__.py`, `keepframe/render/__init__.py`, `keepframe/verify/__init__.py`, `keepframe/analyze/__init__.py`, `keepframe/review/__init__.py`, `tests/conftest.py`, `keepframe/compose/vendor/gsap.min.js`, `keepframe/compose/vendor/CustomEase.min.js`

**Interfaces:**
- Produces: importable package `keepframe`, pytest markers `browser`, `ocr`, `gpu`, fixture `tmp_scene_dir` (a `Path` to a fresh temp dir).

- [ ] **Step 1: Write the failing test**

```python
# tests/test_scaffold.py
import importlib, pathlib

def test_package_imports():
    assert importlib.import_module("keepframe").__version__ == "0.1.0"

def test_vendor_js_present():
    v = pathlib.Path("keepframe/compose/vendor")
    assert (v / "gsap.min.js").stat().st_size > 50_000
    assert (v / "CustomEase.min.js").stat().st_size > 5_000
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_scaffold.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'keepframe'`

- [ ] **Step 3: Create the scaffold**

```toml
# pyproject.toml
[project]
name = "keepframe"
version = "0.1.0"
requires-python = ">=3.11"
dependencies = [
  "pydantic>=2.6",
  "numpy>=1.26",
  "scipy>=1.11",
  "opencv-python-headless>=4.9",
  "playwright>=1.44",
]
[project.optional-dependencies]
gpu = ["torch>=2.2"]
ocr = ["rapidocr-onnxruntime>=1.3"]
dev = ["pytest>=8"]
[project.scripts]
keepframe = "keepframe.cli:main"
[build-system]
requires = ["setuptools>=68"]
build-backend = "setuptools.build_meta"
[tool.setuptools.packages.find]
include = ["keepframe*"]
[tool.setuptools.package-data]
keepframe = ["compose/vendor/*.js", "compose/template.html"]
[tool.pytest.ini_options]
markers = [
  "browser: needs playwright chromium",
  "ocr: needs rapidocr",
  "gpu: needs torch (CPU fallback allowed)",
]
```

```gitignore
# .gitignore
.venv/
__pycache__/
*.egg-info/
out/
**/frames/
*.mp4
```

```python
# keepframe/__init__.py
__version__ = "0.1.0"
```

```python
# tests/conftest.py
import pytest, pathlib, tempfile

@pytest.fixture
def tmp_scene_dir():
    d = tempfile.mkdtemp(prefix="keepframe-")
    return pathlib.Path(d)
```

Vendor GSAP (all GSAP files are free to use since GSAP 3.13 / Webflow's 2024 announcement; keep the license header inside the files):

```bash
mkdir -p keepframe/compose/vendor
curl -sL -o keepframe/compose/vendor/gsap.min.js https://cdnjs.cloudflare.com/ajax/libs/gsap/3.12.5/gsap.min.js
curl -sL -o keepframe/compose/vendor/CustomEase.min.js https://cdnjs.cloudflare.com/ajax/libs/gsap/3.12.5/CustomEase.min.js
head -c 200 keepframe/compose/vendor/gsap.min.js   # must start with /*! gsap 3.12.5
```

Then install:

```bash
python3 -m venv .venv && . .venv/bin/activate
pip install -e ".[dev,ocr]" && playwright install chromium
```

Every other `__init__.py` is empty.

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/test_scaffold.py -v`
Expected: 2 PASS

- [ ] **Step 5: Commit**

```bash
git add pyproject.toml .gitignore keepframe tests/conftest.py tests/test_scaffold.py
git commit -m "chore: scaffold keepframe package, pytest markers, vendored GSAP"
```

---

### Task 2: IR schema

**Files:**
- Create: `keepframe/ir/schema.py`
- Test: `tests/test_schema.py`

**Interfaces:**
- Produces (exact names, used by every later task):
  - `Ease = tuple[float, float, float, float]`
  - `Keyframe(t: int, v: float, ease: Ease | None = None)`
  - `Track(keys: list[Keyframe])` — keys sorted by `t`, unique `t`.
  - `PROPS = ("x","y","sx","sy","rot","skx","sky","opacity")`, `DEFAULTS: dict[str, float]`
  - `FontGuess(family_guess: str, weight: int, size_px: float)`
  - `Canonical(width: float, height: float, anchor: tuple[float,float] = (0.5,0.5), texture: str|None, text: str|None, font: FontGuess|None, color: str|None)`
  - `FitError(max_px: float, max_frames: int)`
  - `Element(id, kind, role, canonical, visible: tuple[int,int], tracks: dict[str,Track], z: Track, raw: str|None, fit_error, confidence: float, provenance)`
  - `Group(id, members: list[str], reason: str)`, `Constraint(pred: str, keep: bool)`, `Background(kind, value, confidence)`
  - `Scene(schema_version alias "schema", id, size: tuple[int,int], fps: float, frames: int, background, elements, groups, constraints)` with `Scene.element(id) -> Element`
  - `SceneRef(id, frames: tuple[int,int], transition_out: dict|None)`, `Version(id, parent, note, auto, scene_file)`, `Project(schema_version alias "schema", source: dict, scenes: list[SceneRef], links: list[dict], versions: list[Version])`
  - `dump(model) -> str` (JSON, by alias, indent 2, sorted keys) and `load_scene_json(text) -> Scene`, `load_project_json(text) -> Project`.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_schema.py
import pytest
from pydantic import ValidationError
from keepframe.ir.schema import (Keyframe, Track, Element, Canonical, Scene, Background,
                                 Constraint, Project, SceneRef, Version, dump, load_scene_json, PROPS, DEFAULTS)

def make_scene():
    el = Element(id="e1", kind="sprite", canonical=Canonical(width=100, height=50, texture="assets/e1.png"),
                 visible=(0, 59), tracks={"x": Track(keys=[Keyframe(t=0, v=0, ease=(0.2, 0, 0, 1)), Keyframe(t=30, v=200)])})
    return Scene(id="s1", size=(640, 360), fps=30, frames=60, background=Background(value="#101418"),
                 elements=[el], constraints=[Constraint(pred="type(m_e1_1,'translation')", keep=True)])

def test_roundtrip_json_uses_schema_alias():
    s = make_scene()
    text = dump(s)
    assert '"schema": "keepframe.scene/1"' in text
    s2 = load_scene_json(text)
    assert s2 == s
    assert s2.element("e1").tracks["x"].keys[0].ease == (0.2, 0, 0, 1)

def test_track_requires_sorted_unique_times():
    with pytest.raises(ValidationError):
        Track(keys=[Keyframe(t=10, v=1), Keyframe(t=5, v=0)])
    with pytest.raises(ValidationError):
        Track(keys=[Keyframe(t=5, v=1), Keyframe(t=5, v=0)])

def test_unknown_track_property_rejected():
    with pytest.raises(ValidationError):
        Element(id="e", kind="sprite", canonical=Canonical(width=1, height=1), visible=(0, 0),
                tracks={"bogus": Track(keys=[Keyframe(t=0, v=0)])})

def test_defaults_cover_all_props():
    assert set(DEFAULTS) == set(PROPS)

def test_project_roundtrip():
    p = Project(source={"file": "ref.mp4", "fps": 30, "size": [640, 360], "mode": "range", "range": [0, 59]},
                scenes=[SceneRef(id="s1", frames=(0, 59))],
                versions=[Version(id="v1", parent=None, note="initial", scene_file="scenes/s1/scene.v1.json")])
    text = dump(p)
    assert '"schema": "keepframe.project/1"' in text
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_schema.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'keepframe.ir.schema'`

- [ ] **Step 3: Write the schema**

```python
# keepframe/ir/schema.py
from __future__ import annotations
import json
from typing import Literal, Optional
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

Ease = tuple[float, float, float, float]
PROPS = ("x", "y", "sx", "sy", "rot", "skx", "sky", "opacity")
DEFAULTS: dict[str, float] = {"x": 0.0, "y": 0.0, "sx": 1.0, "sy": 1.0, "rot": 0.0, "skx": 0.0, "sky": 0.0, "opacity": 1.0}


class Keyframe(BaseModel):
    t: int
    v: float
    ease: Optional[Ease] = None  # easing from this key to the next; None = linear


class Track(BaseModel):
    keys: list[Keyframe]

    @field_validator("keys")
    @classmethod
    def _sorted_unique(cls, keys: list[Keyframe]) -> list[Keyframe]:
        if not keys:
            raise ValueError("track needs at least one keyframe")
        ts = [k.t for k in keys]
        if ts != sorted(ts) or len(set(ts)) != len(ts):
            raise ValueError("keyframes must be sorted by t and unique")
        return keys


class FontGuess(BaseModel):
    family_guess: str = "sans-serif"
    weight: int = 400
    size_px: float = 32.0


class Canonical(BaseModel):
    width: float
    height: float
    anchor: tuple[float, float] = (0.5, 0.5)
    texture: Optional[str] = None
    text: Optional[str] = None
    font: Optional[FontGuess] = None
    color: Optional[str] = None


class FitError(BaseModel):
    max_px: float = 0.0
    max_frames: int = 0


def _zero_track() -> Track:
    return Track(keys=[Keyframe(t=0, v=0.0)])


class Element(BaseModel):
    id: str
    kind: Literal["text", "sprite", "ui", "3d", "group"]
    role: Literal["primary", "secondary", "text", "background"] = "secondary"
    canonical: Canonical
    visible: tuple[int, int]
    tracks: dict[str, Track] = Field(default_factory=dict)
    z: Track = Field(default_factory=_zero_track)
    raw: Optional[str] = None
    fit_error: FitError = Field(default_factory=FitError)
    confidence: float = 1.0
    provenance: Literal["auto", "manual"] = "auto"

    @field_validator("tracks")
    @classmethod
    def _known_props(cls, tracks: dict[str, Track]) -> dict[str, Track]:
        bad = set(tracks) - set(PROPS)
        if bad:
            raise ValueError(f"unknown track properties: {sorted(bad)}")
        return tracks

    @model_validator(mode="after")
    def _visible_range(self):
        a, b = self.visible
        if a > b:
            raise ValueError("visible[0] must be <= visible[1]")
        return self


class Group(BaseModel):
    id: str
    members: list[str]
    reason: str = ""


class Constraint(BaseModel):
    pred: str
    keep: bool = False


class Background(BaseModel):
    kind: Literal["color", "image"] = "color"
    value: str = "#000000"
    confidence: float = 1.0


class Scene(BaseModel):
    model_config = ConfigDict(populate_by_name=True)
    schema_version: str = Field("keepframe.scene/1", alias="schema")
    id: str
    size: tuple[int, int]
    fps: float
    frames: int
    background: Background
    elements: list[Element]
    groups: list[Group] = Field(default_factory=list)
    constraints: list[Constraint] = Field(default_factory=list)

    def element(self, eid: str) -> Element:
        for e in self.elements:
            if e.id == eid:
                return e
        raise KeyError(eid)


class SceneRef(BaseModel):
    id: str
    frames: tuple[int, int]
    transition_out: Optional[dict] = None


class Version(BaseModel):
    id: str
    parent: Optional[str] = None
    note: str = ""
    auto: bool = True
    scene_file: str


class Project(BaseModel):
    model_config = ConfigDict(populate_by_name=True)
    schema_version: str = Field("keepframe.project/1", alias="schema")
    source: dict
    scenes: list[SceneRef]
    links: list[dict] = Field(default_factory=list)
    versions: list[Version] = Field(default_factory=list)


def dump(model: BaseModel) -> str:
    return json.dumps(model.model_dump(by_alias=True), indent=2, sort_keys=True)


def load_scene_json(text: str) -> Scene:
    return Scene.model_validate(json.loads(text))


def load_project_json(text: str) -> Project:
    return Project.model_validate(json.loads(text))
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/test_schema.py -v`
Expected: 5 PASS

- [ ] **Step 5: Commit**

```bash
git add keepframe/ir/schema.py tests/test_schema.py
git commit -m "feat(ir): pydantic IR schema for scene/project with validators"
```

---

### Task 3: Track evaluation and affine math

**Files:**
- Create: `keepframe/ir/tracks.py`
- Test: `tests/test_tracks.py`

**Interfaces:**
- Produces:
  - `bezier_y(x: float, ease: Ease) -> float` — cubic-bezier easing value at progress x ∈ [0,1].
  - `eval_track(track: Track, f: float) -> float` — clamps before/after ends.
  - `eval_props(el: Element, f: float) -> dict[str, float]` — all `PROPS` filled with `DEFAULTS` when the track is absent.
  - `eval_z(el: Element, f: float) -> int` — stepwise (value of last key with `t <= f`).
  - `affine_matrix(p: dict[str, float]) -> np.ndarray` — 3×3, `T(x,y)·R(rot)·SkewX(skx)·S(sx,sy)`; `sky` is accepted but must be 0 (raise if not).
  - `decompose_affine(M: np.ndarray) -> dict[str, float]` — inverse of the above, returns keys x,y,sx,sy,rot,skx,sky(=0).
  - `local_corners(c: Canonical) -> np.ndarray` — (4,2) corners of the canonical box in local coords with the anchor at origin.
  - `element_corners(el: Element, f: float) -> np.ndarray` (4,2) scene coords; `element_bbox(el, f) -> tuple[float,float,float,float]` (x0,y0,x1,y1).
  - `PRESET_EASES: dict[str, Ease]` (linear, in_quad, out_quad, in_out_quad, in_cubic, out_cubic, in_out_cubic, out_back).

- [ ] **Step 1: Write the failing test**

```python
# tests/test_tracks.py
import math, numpy as np, pytest
from keepframe.ir.schema import Keyframe, Track, Element, Canonical
from keepframe.ir.tracks import (bezier_y, eval_track, eval_props, eval_z, affine_matrix,
                                 decompose_affine, element_bbox, PRESET_EASES)

def test_bezier_endpoints_and_linear():
    for e in PRESET_EASES.values():
        assert bezier_y(0.0, e) == pytest.approx(0.0, abs=1e-6)
        assert bezier_y(1.0, e) == pytest.approx(1.0, abs=1e-6)
    assert bezier_y(0.3, PRESET_EASES["linear"]) == pytest.approx(0.3, abs=1e-6)
    # ease-out is ahead of linear mid-way, ease-in is behind
    assert bezier_y(0.5, PRESET_EASES["out_quad"]) > 0.5 > bezier_y(0.5, PRESET_EASES["in_quad"])

def test_eval_track_clamps_and_interpolates():
    tr = Track(keys=[Keyframe(t=10, v=0.0), Keyframe(t=20, v=100.0)])
    assert eval_track(tr, 0) == 0.0 and eval_track(tr, 99) == 100.0
    assert eval_track(tr, 15) == pytest.approx(50.0)

def test_eval_props_fills_defaults_and_z_is_stepwise():
    el = Element(id="e", kind="sprite", canonical=Canonical(width=10, height=10), visible=(0, 10),
                 tracks={"x": Track(keys=[Keyframe(t=0, v=5.0)])},
                 z=Track(keys=[Keyframe(t=0, v=1), Keyframe(t=5, v=3)]))
    p = eval_props(el, 3)
    assert p["x"] == 5.0 and p["sx"] == 1.0 and p["opacity"] == 1.0
    assert eval_z(el, 4) == 1 and eval_z(el, 5) == 3

def test_affine_roundtrip():
    p = {"x": 120.0, "y": -30.0, "sx": 1.5, "sy": 0.5, "rot": 33.0, "skx": 10.0, "sky": 0.0, "opacity": 1.0}
    M = affine_matrix(p)
    q = decompose_affine(M)
    for k in ("x", "y", "sx", "sy", "rot", "skx"):
        assert q[k] == pytest.approx(p[k], abs=1e-6), k

def test_bbox_of_scaled_rotated_box():
    el = Element(id="e", kind="sprite", canonical=Canonical(width=100, height=40), visible=(0, 0),
                 tracks={"x": Track(keys=[Keyframe(t=0, v=200)]), "y": Track(keys=[Keyframe(t=0, v=100)]),
                         "sx": Track(keys=[Keyframe(t=0, v=2.0)]), "rot": Track(keys=[Keyframe(t=0, v=90)])})
    x0, y0, x1, y1 = element_bbox(el, 0)
    # width 200 rotated 90° becomes vertical extent 200, height 40 becomes horizontal extent 40
    assert (x1 - x0) == pytest.approx(40, abs=1e-6) and (y1 - y0) == pytest.approx(200, abs=1e-6)
    assert (x0 + x1) / 2 == pytest.approx(200) and (y0 + y1) / 2 == pytest.approx(100)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_tracks.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'keepframe.ir.tracks'`

- [ ] **Step 3: Implement**

```python
# keepframe/ir/tracks.py
from __future__ import annotations
import math
import numpy as np
from .schema import Canonical, Element, Ease, Track, PROPS, DEFAULTS

PRESET_EASES: dict[str, Ease] = {
    "linear": (0.0, 0.0, 1.0, 1.0),
    "in_quad": (0.11, 0.0, 0.5, 0.0),
    "out_quad": (0.5, 1.0, 0.89, 1.0),
    "in_out_quad": (0.45, 0.0, 0.55, 1.0),
    "in_cubic": (0.32, 0.0, 0.67, 0.0),
    "out_cubic": (0.33, 1.0, 0.68, 1.0),
    "in_out_cubic": (0.65, 0.0, 0.35, 1.0),
    "out_back": (0.34, 1.56, 0.64, 1.0),
}


def _bez(s: float, a: float, b: float) -> float:
    return 3 * (1 - s) ** 2 * s * a + 3 * (1 - s) * s ** 2 * b + s ** 3


def bezier_y(x: float, ease: Ease) -> float:
    x1, y1, x2, y2 = ease
    if x <= 0.0:
        return 0.0
    if x >= 1.0:
        return 1.0
    lo, hi, s = 0.0, 1.0, x
    for _ in range(40):  # bisection on the monotone x(s)
        if _bez(s, x1, x2) < x:
            lo = s
        else:
            hi = s
        s = 0.5 * (lo + hi)
    return _bez(s, y1, y2)


def eval_track(track: Track, f: float) -> float:
    keys = track.keys
    if f <= keys[0].t:
        return keys[0].v
    if f >= keys[-1].t:
        return keys[-1].v
    for a, b in zip(keys, keys[1:]):
        if a.t <= f <= b.t:
            u = (f - a.t) / (b.t - a.t)
            e = bezier_y(u, a.ease) if a.ease else u
            return a.v + (b.v - a.v) * e
    return keys[-1].v


def eval_props(el: Element, f: float) -> dict[str, float]:
    return {p: (eval_track(el.tracks[p], f) if p in el.tracks else DEFAULTS[p]) for p in PROPS}


def eval_z(el: Element, f: float) -> int:
    v = el.z.keys[0].v
    for k in el.z.keys:
        if k.t <= f:
            v = k.v
    return int(v)


def affine_matrix(p: dict[str, float]) -> np.ndarray:
    if abs(p.get("sky", 0.0)) > 1e-9:
        raise ValueError("sky must be 0 (decomposition uses skewX only)")
    r = math.radians(p["rot"])
    k = math.tan(math.radians(p["skx"]))
    R = np.array([[math.cos(r), -math.sin(r)], [math.sin(r), math.cos(r)]])
    K = np.array([[1.0, k], [0.0, 1.0]])
    S = np.diag([p["sx"], p["sy"]])
    M = np.eye(3)
    M[:2, :2] = R @ K @ S
    M[:2, 2] = [p["x"], p["y"]]
    return M


def decompose_affine(M: np.ndarray) -> dict[str, float]:
    a, b, c, d = M[0, 0], M[0, 1], M[1, 0], M[1, 1]
    rot = math.atan2(c, a)
    sx = math.hypot(a, c)
    R = np.array([[math.cos(rot), -math.sin(rot)], [math.sin(rot), math.cos(rot)]])
    U = R.T @ M[:2, :2]  # = [[sx, tan(skx)*sy],[0, sy]]
    sy = U[1, 1]
    skx = math.degrees(math.atan2(U[0, 1], sy)) if abs(sy) > 1e-12 else 0.0
    return {"x": float(M[0, 2]), "y": float(M[1, 2]), "sx": float(sx), "sy": float(sy),
            "rot": math.degrees(rot), "skx": float(skx), "sky": 0.0}


def local_corners(c: Canonical) -> np.ndarray:
    ax, ay = c.anchor
    x0, y0 = -ax * c.width, -ay * c.height
    return np.array([[x0, y0], [x0 + c.width, y0], [x0 + c.width, y0 + c.height], [x0, y0 + c.height]], dtype=float)


def element_corners(el: Element, f: float) -> np.ndarray:
    M = affine_matrix(eval_props(el, f))
    pts = np.c_[local_corners(el.canonical), np.ones(4)]
    return (pts @ M.T)[:, :2]


def element_bbox(el: Element, f: float) -> tuple[float, float, float, float]:
    P = element_corners(el, f)
    return float(P[:, 0].min()), float(P[:, 1].min()), float(P[:, 0].max()), float(P[:, 1].max())
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/test_tracks.py -v`
Expected: 5 PASS

- [ ] **Step 5: Commit**

```bash
git add keepframe/ir/tracks.py tests/test_tracks.py
git commit -m "feat(ir): track evaluation, cubic-bezier easing, affine compose/decompose"
```

---

### Task 4: Project store with append-only versions

**Files:**
- Create: `keepframe/ir/store.py`
- Test: `tests/test_store.py`

**Interfaces:**
- Produces:
  - `save_scene(scene: Scene, path: Path) -> None`, `load_scene(path: Path) -> Scene`
  - `init_project(root: Path, source: dict, scene: Scene, note: str = "initial analysis") -> Project` — writes `root/project.json` and `root/scenes/<scene.id>/scene.v1.json`; returns the Project. Assets are expected under `root/scenes/<id>/assets/`.
  - `load_project(root: Path) -> Project`
  - `scene_dir(root: Path, scene_id: str) -> Path`
  - `current_scene(root: Path, scene_id: str) -> tuple[Scene, Version]` — latest version for that scene.
  - `new_version(root: Path, scene_id: str, scene: Scene, note: str, auto: bool = True) -> Version` — appends `vN` with parent = previous latest; never overwrites existing files.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_store.py
import pytest
from keepframe.ir.schema import Scene, Element, Canonical, Background, Keyframe, Track
from keepframe.ir.store import init_project, load_project, current_scene, new_version, scene_dir, load_scene

def scene():
    return Scene(id="s1", size=(64, 32), fps=30, frames=10, background=Background(),
                 elements=[Element(id="e1", kind="sprite", canonical=Canonical(width=8, height=8), visible=(0, 9))])

def test_init_and_versions_are_append_only(tmp_scene_dir):
    root = tmp_scene_dir
    p = init_project(root, {"file": "x.mp4", "fps": 30, "size": [64, 32], "mode": "range", "range": [0, 9]}, scene())
    assert (root / "project.json").exists()
    assert (scene_dir(root, "s1") / "scene.v1.json").exists()
    s, v = current_scene(root, "s1")
    assert v.id == "v1" and v.parent is None and s.id == "s1"
    s2 = s.model_copy(deep=True)
    s2.elements[0].tracks["x"] = Track(keys=[Keyframe(t=0, v=3.0)])
    v2 = new_version(root, "s1", s2, note="moved e1", auto=False)
    assert v2.id == "v2" and v2.parent == "v1" and v2.auto is False
    assert load_scene(scene_dir(root, "s1") / "scene.v1.json").elements[0].tracks == {}   # v1 untouched
    assert load_project(root).versions[-1].id == "v2"
    s3, v3 = current_scene(root, "s1")
    assert v3.id == "v2" and s3.elements[0].tracks["x"].keys[0].v == 3.0
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_store.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'keepframe.ir.store'`

- [ ] **Step 3: Implement**

```python
# keepframe/ir/store.py
from __future__ import annotations
from pathlib import Path
from .schema import Project, Scene, SceneRef, Version, dump, load_project_json, load_scene_json


def save_scene(scene: Scene, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(dump(scene))


def load_scene(path: Path) -> Scene:
    return load_scene_json(Path(path).read_text())


def scene_dir(root: Path, scene_id: str) -> Path:
    return Path(root) / "scenes" / scene_id


def load_project(root: Path) -> Project:
    return load_project_json((Path(root) / "project.json").read_text())


def _save_project(root: Path, project: Project) -> None:
    (Path(root) / "project.json").write_text(dump(project))


def init_project(root: Path, source: dict, scene: Scene, note: str = "initial analysis") -> Project:
    root = Path(root)
    if (root / "project.json").exists():
        raise FileExistsError(root / "project.json")
    rel = f"scenes/{scene.id}/scene.v1.json"
    save_scene(scene, root / rel)
    project = Project(source=source, scenes=[SceneRef(id=scene.id, frames=(0, scene.frames - 1))],
                      versions=[Version(id="v1", parent=None, note=note, auto=True, scene_file=rel)])
    _save_project(root, project)
    return project


def _versions_for(project: Project, scene_id: str) -> list[Version]:
    return [v for v in project.versions if v.scene_file.startswith(f"scenes/{scene_id}/")]


def current_scene(root: Path, scene_id: str) -> tuple[Scene, Version]:
    project = load_project(root)
    v = _versions_for(project, scene_id)[-1]
    return load_scene(Path(root) / v.scene_file), v


def new_version(root: Path, scene_id: str, scene: Scene, note: str, auto: bool = True) -> Version:
    root = Path(root)
    project = load_project(root)
    prev = _versions_for(project, scene_id)
    n = len(prev) + 1
    rel = f"scenes/{scene_id}/scene.v{n}.json"
    if (root / rel).exists():
        raise FileExistsError(rel)  # append-only: never overwrite
    save_scene(scene, root / rel)
    v = Version(id=f"v{n}", parent=prev[-1].id if prev else None, note=note, auto=auto, scene_file=rel)
    project.versions.append(v)
    _save_project(root, project)
    return v
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/test_store.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add keepframe/ir/store.py tests/test_store.py
git commit -m "feat(ir): project store with append-only scene versions"
```

---

### Task 5: Synthetic scene generator (fixtures and golden IR)

**Files:**
- Create: `keepframe/ir/synth.py`
- Test: `tests/test_synth.py`

**Interfaces:**
- Produces:
  - `make_texture(path: Path, shape: str, w: int, h: int, color: tuple[int,int,int]) -> None` — writes an RGBA PNG (`rect` or `ellipse`, alpha 255 inside, 0 outside).
  - `make_text_texture(path: Path, text: str, size_px: int, color: tuple[int,int,int]) -> tuple[int,int]` — rasterizes text with `cv2.putText` (Hershey font), returns (w,h).
  - `make_synthetic_scene(scene_root: Path, seed: int, n_elements: int = 4, frames: int = 60, size: tuple[int,int] = (640, 360), fps: float = 30.0, with_text: bool = True, overlap: bool = True) -> Scene` — writes textures under `scene_root/assets/`, returns a validated Scene (not saved). Elements are sprites (plus one text element when `with_text`); each has x,y tracks with 2–4 keyframes and preset eases, optionally sx/sy/rot/opacity tracks; z distinct per element. Deterministic for a given seed. Element ids `e1..eN`. Text element canonical carries both `text` and a `texture`.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_synth.py
import cv2, numpy as np
from keepframe.ir.synth import make_synthetic_scene, make_texture
from keepframe.ir.schema import dump

def test_texture_is_rgba_with_alpha(tmp_scene_dir):
    p = tmp_scene_dir / "t.png"
    make_texture(p, "ellipse", 40, 20, (255, 0, 0))
    img = cv2.imread(str(p), cv2.IMREAD_UNCHANGED)
    assert img.shape == (20, 40, 4)
    assert img[10, 20, 3] == 255 and img[0, 0, 3] == 0

def test_synthetic_scene_is_deterministic_and_valid(tmp_scene_dir):
    a = make_synthetic_scene(tmp_scene_dir / "a", seed=7)
    b = make_synthetic_scene(tmp_scene_dir / "b", seed=7)
    assert dump(a) == dump(b)
    assert len(a.elements) == 5  # 4 sprites + 1 text
    assert all((tmp_scene_dir / "a" / e.canonical.texture).exists() for e in a.elements)
    assert len({e.z.keys[0].v for e in a.elements}) == 5
    assert any(k.ease is not None for e in a.elements for k in e.tracks["x"].keys)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_synth.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'keepframe.ir.synth'`

- [ ] **Step 3: Implement**

```python
# keepframe/ir/synth.py
from __future__ import annotations
import random
from pathlib import Path
import cv2, numpy as np
from .schema import Background, Canonical, Element, FontGuess, Keyframe, Scene, Track
from .tracks import PRESET_EASES

PALETTE = [(239, 71, 111), (255, 209, 102), (6, 214, 160), (17, 138, 178), (7, 59, 76), (255, 255, 255)]


def make_texture(path: Path, shape: str, w: int, h: int, color: tuple[int, int, int]) -> None:
    img = np.zeros((h, w, 4), np.uint8)
    bgr = (color[2], color[1], color[0])
    if shape == "rect":
        cv2.rectangle(img, (0, 0), (w - 1, h - 1), (*bgr, 255), -1)
    else:
        cv2.ellipse(img, (w // 2, h // 2), (w // 2 - 1, h // 2 - 1), 0, 0, 360, (*bgr, 255), -1)
    path.parent.mkdir(parents=True, exist_ok=True)
    cv2.imwrite(str(path), img)


def make_text_texture(path: Path, text: str, size_px: int, color: tuple[int, int, int]) -> tuple[int, int]:
    scale = size_px / 22.0  # Hershey simplex cap height ≈ 22 px at scale 1
    thick = max(1, int(round(scale * 2)))
    (tw, th), base = cv2.getTextSize(text, cv2.FONT_HERSHEY_SIMPLEX, scale, thick)
    w, h = tw + 4, th + base + 4
    img = np.zeros((h, w, 4), np.uint8)
    cv2.putText(img, text, (2, th + 2), cv2.FONT_HERSHEY_SIMPLEX, scale, (color[2], color[1], color[0], 255), thick, cv2.LINE_8)
    path.parent.mkdir(parents=True, exist_ok=True)
    cv2.imwrite(str(path), img)
    return w, h


def _track(rng: random.Random, frames: int, v0: float, v1: float, nkeys: int) -> Track:
    ts = sorted(rng.sample(range(1, frames - 1), nkeys - 2)) if nkeys > 2 else []
    ts = [0] + ts + [frames - 1]
    vals = [v0] + [v0 + (v1 - v0) * rng.random() for _ in ts[1:-1]] + [v1]
    keys = []
    for i, (t, v) in enumerate(zip(ts, vals)):
        ease = PRESET_EASES[rng.choice(list(PRESET_EASES))] if i < len(ts) - 1 else None
        keys.append(Keyframe(t=t, v=round(v, 2), ease=ease))
    return Track(keys=keys)


def make_synthetic_scene(scene_root: Path, seed: int, n_elements: int = 4, frames: int = 60,
                         size: tuple[int, int] = (640, 360), fps: float = 30.0, with_text: bool = True,
                         overlap: bool = True) -> Scene:
    rng = random.Random(seed)
    W, H = size
    elements: list[Element] = []
    n = n_elements + (1 if with_text else 0)
    for i in range(1, n + 1):
        eid = f"e{i}"
        color = PALETTE[(i - 1) % len(PALETTE)]
        is_text = with_text and i == n
        if is_text:
            text = rng.choice(["Launch", "Faster", "Keepframe", "New"])
            tw, th = make_text_texture(scene_root / "assets" / f"{eid}.png", text, 40, color)
            canonical = Canonical(width=tw, height=th, texture=f"assets/{eid}.png", text=text,
                                  font=FontGuess(family_guess="sans-serif", weight=700, size_px=40),
                                  color="#%02x%02x%02x" % color)
        else:
            w, h = rng.randint(40, 160), rng.randint(40, 120)
            make_texture(scene_root / "assets" / f"{eid}.png", rng.choice(["rect", "ellipse"]), w, h, color)
            canonical = Canonical(width=w, height=h, texture=f"assets/{eid}.png")
        cx0, cx1 = rng.uniform(0.15, 0.85) * W, rng.uniform(0.15, 0.85) * W
        cy0, cy1 = rng.uniform(0.2, 0.8) * H, rng.uniform(0.2, 0.8) * H
        if overlap and i > 1:  # make it cross the previous element's path
            cx1 = elements[-1].tracks["x"].keys[0].v
            cy1 = elements[-1].tracks["y"].keys[0].v
        tracks = {"x": _track(rng, frames, cx0, cx1, rng.randint(2, 4)),
                  "y": _track(rng, frames, cy0, cy1, rng.randint(2, 3))}
        if rng.random() < 0.5:
            tracks["rot"] = _track(rng, frames, 0.0, rng.choice([-45.0, 30.0, 90.0]), 2)
        if rng.random() < 0.5:
            s = rng.uniform(0.6, 1.6)
            tracks["sx"] = _track(rng, frames, 1.0, s, 2)
            tracks["sy"] = _track(rng, frames, 1.0, s, 2)
        if rng.random() < 0.5:
            tracks["opacity"] = Track(keys=[Keyframe(t=0, v=0.0, ease=PRESET_EASES["out_quad"]),
                                            Keyframe(t=rng.randint(6, 18), v=1.0)])
        first = rng.choice([0, 0, rng.randint(0, frames // 3)])
        elements.append(Element(id=eid, kind="text" if is_text else "sprite", role="text" if is_text else "secondary",
                                canonical=canonical, visible=(first, frames - 1), tracks=tracks,
                                z=Track(keys=[Keyframe(t=0, v=i)])))
    return Scene(id=f"synth{seed}", size=size, fps=fps, frames=frames,
                 background=Background(kind="color", value="#101418"), elements=elements)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/test_synth.py -v`
Expected: 2 PASS

- [ ] **Step 5: Commit**

```bash
git add keepframe/ir/synth.py tests/test_synth.py
git commit -m "feat(ir): synthetic scene generator with textures for fixtures and golden IR"
```

---

### Task 6: Composer (Scene → self-contained HTML with GSAP timeline)

**Files:**
- Create: `keepframe/compose/template.html`, `keepframe/compose/composer.py`
- Test: `tests/test_composer.py`

**Interfaces:**
- Produces: `compose(scene: Scene, scene_dir: Path, out_html: Path) -> Path` — writes one self-contained HTML file (vendor JS inlined, textures inlined as data URIs). The page exposes `window.__ready` (true after timeline build), `window.__seek(frameIndex)` (renders that frame) and `window.__bbox(elementId) -> [left, top, right, bottom]` in stage pixels. Element nodes have ids `el-<element.id>` and HyperFrames-style attributes `data-start` (seconds), `data-duration` (seconds), `data-track-index` (z at frame 0). The stage node is `#stage` with `data-composition-id`, `data-width`, `data-height`.
- Transform model in the page: `translate(x,y) rotate(rot) skewX(skx) scale(sx,sy)` about `transform-origin` = anchor, i.e. exactly `affine_matrix` from Task 3. GSAP tweens proxy objects and the page writes the transform string itself, so GSAP's own transform ordering is never relied on.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_composer.py
from keepframe.ir.synth import make_synthetic_scene
from keepframe.compose.composer import compose

def test_compose_is_self_contained_and_has_hooks(tmp_scene_dir):
    scene = make_synthetic_scene(tmp_scene_dir, seed=1)
    out = compose(scene, tmp_scene_dir, tmp_scene_dir / "composition.html")
    html = out.read_text()
    assert "gsap 3.12" in html and "CustomEase" in html          # vendor inlined
    assert 'src="data:image/png;base64,' in html                  # textures inlined
    assert "assets/" not in html.split("<script")[0]              # no external file refs in markup
    assert "window.__seek" in html and "window.__bbox" in html
    assert 'id="stage" data-composition-id="synth1" data-width="640" data-height="360"' in html
    for e in scene.elements:
        assert f'id="el-{e.id}"' in html
    text = [e for e in scene.elements if e.kind == "text"][0]
    assert f"<span" in html and text.canonical.text in html
    assert f'data-track-index="{int(text.z.keys[0].v)}"' in html
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_composer.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'keepframe.compose.composer'`

- [ ] **Step 3: Write the template and composer**

```html
<!-- keepframe/compose/template.html -->
<!doctype html>
<html><head><meta charset="utf-8"><title>{{ID}}</title>
<style>
  html,body{margin:0;padding:0;background:{{BG}};overflow:hidden}
  #stage{position:relative;width:{{WIDTH}}px;height:{{HEIGHT}}px;background:{{BG}};overflow:hidden}
  .el{position:absolute;will-change:transform;transition:none!important}
  .el img{display:block;width:100%;height:100%;image-rendering:auto}
  .el span{display:block;width:100%;height:100%;white-space:pre;overflow:visible}
</style>
<script>{{GSAP_JS}}</script>
<script>{{CUSTOMEASE_JS}}</script>
</head>
<body>
<div id="stage" data-composition-id="{{ID}}" data-width="{{WIDTH}}" data-height="{{HEIGHT}}">
{{ELEMENTS}}
</div>
<script>
const SCENE = {{SCENE_JSON}};
const FPS = SCENE.fps;
const DEFAULTS = {x:0,y:0,sx:1,sy:1,rot:0,skx:0,sky:0,opacity:1};
gsap.registerPlugin(CustomEase);
const tl = gsap.timeline({paused:true});
const appliers = [];
for (const el of SCENE.elements) {
  const node = document.getElementById('el-' + el.id);
  const st = Object.assign({}, DEFAULTS);
  const apply = () => {
    node.style.transform = `translate(${st.x}px, ${st.y}px) rotate(${st.rot}deg) skewX(${st.skx}deg) scale(${st.sx}, ${st.sy})`;
    node.style.opacity = String(st.opacity);
  };
  for (const [prop, track] of Object.entries(el.tracks)) {
    const keys = track.keys;
    st[prop] = keys[0].v;
    for (let i = 0; i < keys.length - 1; i++) {
      const a = keys[i], b = keys[i + 1];
      const ease = a.ease ? CustomEase.create('', `M0,0 C${a.ease[0]},${a.ease[1]} ${a.ease[2]},${a.ease[3]} 1,1`) : 'none';
      tl.fromTo(st, {[prop]: a.v}, {[prop]: b.v, duration: (b.t - a.t) / FPS, ease, immediateRender: false, onUpdate: apply}, a.t / FPS);
    }
  }
  const zAt = (f) => { let v = el.z.keys[0].v; for (const k of el.z.keys) if (k.t <= f) v = k.v; return v; };
  appliers.push((f) => {
    apply();
    node.style.visibility = (f >= el.visible[0] && f <= el.visible[1]) ? 'visible' : 'hidden';
    node.style.zIndex = String(zAt(f));
  });
}
window.__seek = (f) => { tl.time(f / FPS, false); for (const a of appliers) a(f); return f; };
window.__bbox = (id) => {
  const r = document.getElementById('el-' + id).getBoundingClientRect();
  const s = document.getElementById('stage').getBoundingClientRect();
  return [r.left - s.left, r.top - s.top, r.right - s.left, r.bottom - s.top];
};
window.__seek(0);
document.fonts.ready.then(() => { window.__ready = true; });
</script>
</body></html>
```

```python
# keepframe/compose/composer.py
from __future__ import annotations
import base64, html, json
from pathlib import Path
from ..ir.schema import Element, Scene
from ..ir.tracks import eval_z

VENDOR = Path(__file__).parent / "vendor"
TEMPLATE = Path(__file__).parent / "template.html"


def _data_uri(path: Path) -> str:
    return "data:image/png;base64," + base64.b64encode(path.read_bytes()).decode("ascii")


def _element_html(el: Element, scene_dir: Path, fps: float) -> str:
    c = el.canonical
    ax, ay = c.anchor
    style = (f"left:{-ax * c.width:.4f}px;top:{-ay * c.height:.4f}px;width:{c.width:.4f}px;height:{c.height:.4f}px;"
             f"transform-origin:{ax * 100:.4f}% {ay * 100:.4f}%;")
    start = el.visible[0] / fps
    dur = (el.visible[1] - el.visible[0] + 1) / fps
    if el.kind == "text" and c.text is not None and c.font is not None:
        f = c.font
        inner = (f'<span style="font-family:{html.escape(f.family_guess)};font-weight:{f.weight};'
                 f'font-size:{f.size_px}px;line-height:{c.height}px;color:{c.color or "#000"}">{html.escape(c.text)}</span>')
    elif c.texture:
        inner = f'<img src="{_data_uri(scene_dir / c.texture)}" alt="{html.escape(el.id)}">'
    else:
        inner = ""
    return (f'<div class="el" id="el-{html.escape(el.id)}" data-start="{start:.4f}" data-duration="{dur:.4f}" '
            f'data-track-index="{eval_z(el, 0)}" style="{style}">{inner}</div>')


def compose(scene: Scene, scene_dir: Path, out_html: Path) -> Path:
    scene_dir, out_html = Path(scene_dir), Path(out_html)
    elements = "\n".join(_element_html(e, scene_dir, scene.fps) for e in scene.elements)
    scene_json = json.dumps(scene.model_dump(by_alias=True), separators=(",", ":"))
    page = TEMPLATE.read_text()
    for k, v in {
        "{{ID}}": html.escape(scene.id), "{{WIDTH}}": str(scene.size[0]), "{{HEIGHT}}": str(scene.size[1]),
        "{{BG}}": scene.background.value if scene.background.kind == "color" else "#000000",
        "{{GSAP_JS}}": (VENDOR / "gsap.min.js").read_text(), "{{CUSTOMEASE_JS}}": (VENDOR / "CustomEase.min.js").read_text(),
        "{{ELEMENTS}}": elements, "{{SCENE_JSON}}": scene_json,
    }.items():
        page = page.replace(k, v)
    out_html.parent.mkdir(parents=True, exist_ok=True)
    out_html.write_text(page)
    return out_html
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/test_composer.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add keepframe/compose/template.html keepframe/compose/composer.py tests/test_composer.py
git commit -m "feat(compose): self-contained HTML composition with GSAP timeline and seek/bbox hooks"
```

---

### Task 7: Renderer (frame seeking, hashes, bbox probe, MP4)

**Files:**
- Create: `keepframe/render/renderer.py`
- Test: `tests/test_renderer.py`

**Interfaces:**
- Produces:
  - `@dataclass RenderResult: frames_dir: Path; frames: list[int]; hashes: list[str]; bboxes: dict[str, list[list[float]]]; mp4: Path | None`
  - `render(html: Path, scene: Scene, out_dir: Path, frames: list[int] | None = None, mp4: bool = False, probe: bool = True) -> RenderResult` — `frames=None` means all `range(scene.frames)`. Writes `out_dir/frames/f_00000.png` (index = position in `frames`, not frame number) and `out_dir/render.json` (the result as JSON). `bboxes[element_id][i]` is the probe for `frames[i]`.
  - `frame_hash(png_bytes: bytes) -> str` — sha256 of the decoded RGB pixel buffer.
  - `write_mp4(frames_dir: Path, fps: float, out: Path) -> Path` — ffmpeg libx264 yuv420p crf 18.
  - `load_frame(path: Path) -> np.ndarray` — RGB float32 [0,1].

- [ ] **Step 1: Write the failing test**

```python
# tests/test_renderer.py
import numpy as np, pytest
from keepframe.ir.schema import Scene, Element, Canonical, Background, Keyframe, Track
from keepframe.ir.synth import make_synthetic_scene, make_texture
from keepframe.ir.tracks import element_bbox
from keepframe.compose.composer import compose
from keepframe.render.renderer import render, load_frame

pytestmark = pytest.mark.browser

def test_render_is_deterministic(tmp_scene_dir):
    scene = make_synthetic_scene(tmp_scene_dir, seed=3, with_text=False)
    html = compose(scene, tmp_scene_dir, tmp_scene_dir / "c.html")
    a = render(html, scene, tmp_scene_dir / "r1", frames=[0, 10, 30])
    b = render(html, scene, tmp_scene_dir / "r2", frames=[0, 10, 30])
    assert a.hashes == b.hashes and len(a.hashes) == 3

def test_background_pixel(tmp_scene_dir):
    scene = make_synthetic_scene(tmp_scene_dir, seed=3, with_text=False)
    html = compose(scene, tmp_scene_dir, tmp_scene_dir / "c.html")
    r = render(html, scene, tmp_scene_dir / "r", frames=[0])
    img = load_frame(r.frames_dir / "f_00000.png")
    assert img.shape == (360, 640, 3)
    assert np.allclose(img[0, 0] * 255, [0x10, 0x14, 0x18], atol=1)

def test_probe_bbox_matches_python_bbox_with_rotation_skew_scale(tmp_scene_dir):
    make_texture(tmp_scene_dir / "assets" / "e1.png", "rect", 100, 40, (255, 0, 0))
    el = Element(id="e1", kind="sprite", canonical=Canonical(width=100, height=40, texture="assets/e1.png"), visible=(0, 9),
                 tracks={"x": Track(keys=[Keyframe(t=0, v=200), Keyframe(t=9, v=400)]),
                         "y": Track(keys=[Keyframe(t=0, v=150)]), "rot": Track(keys=[Keyframe(t=0, v=33)]),
                         "skx": Track(keys=[Keyframe(t=0, v=10)]), "sx": Track(keys=[Keyframe(t=0, v=1.5)]),
                         "sy": Track(keys=[Keyframe(t=0, v=0.7)])})
    scene = Scene(id="t", size=(640, 360), fps=30, frames=10, background=Background(value="#000000"), elements=[el])
    html = compose(scene, tmp_scene_dir, tmp_scene_dir / "c.html")
    r = render(html, scene, tmp_scene_dir / "r", frames=[0, 5, 9])
    for i, f in enumerate([0, 5, 9]):
        exp = np.array(element_bbox(el, f))
        got = np.array(r.bboxes["e1"][i])
        assert np.abs(exp - got).max() < 1.5, (f, exp, got)

def test_mp4_written(tmp_scene_dir):
    scene = make_synthetic_scene(tmp_scene_dir, seed=3, frames=12, with_text=False)
    html = compose(scene, tmp_scene_dir, tmp_scene_dir / "c.html")
    r = render(html, scene, tmp_scene_dir / "r", mp4=True)
    assert r.mp4 is not None and r.mp4.stat().st_size > 1000
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_renderer.py -v -m browser`
Expected: FAIL with `ModuleNotFoundError: No module named 'keepframe.render.renderer'`

- [ ] **Step 3: Implement**

```python
# keepframe/render/renderer.py
from __future__ import annotations
import hashlib, json, shutil, subprocess
from dataclasses import dataclass, asdict
from pathlib import Path
import cv2, numpy as np
from playwright.sync_api import sync_playwright
from ..ir.schema import Scene


@dataclass
class RenderResult:
    frames_dir: Path
    frames: list[int]
    hashes: list[str]
    bboxes: dict[str, list[list[float]]]
    mp4: Path | None = None


def frame_hash(png_bytes: bytes) -> str:
    arr = cv2.imdecode(np.frombuffer(png_bytes, np.uint8), cv2.IMREAD_COLOR)
    return hashlib.sha256(np.ascontiguousarray(arr).tobytes()).hexdigest()


def load_frame(path: Path) -> np.ndarray:
    bgr = cv2.imread(str(path), cv2.IMREAD_COLOR)
    return cv2.cvtColor(bgr, cv2.COLOR_BGR2RGB).astype(np.float32) / 255.0


def write_mp4(frames_dir: Path, fps: float, out: Path) -> Path:
    if shutil.which("ffmpeg") is None:
        raise RuntimeError("ffmpeg not found on PATH")
    subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-framerate", str(fps), "-i", str(frames_dir / "f_%05d.png"),
                    "-c:v", "libx264", "-pix_fmt", "yuv420p", "-crf", "18", str(out)], check=True)
    return out


def render(html: Path, scene: Scene, out_dir: Path, frames: list[int] | None = None,
           mp4: bool = False, probe: bool = True) -> RenderResult:
    out_dir = Path(out_dir)
    frames_dir = out_dir / "frames"
    if frames_dir.exists():
        shutil.rmtree(frames_dir)
    frames_dir.mkdir(parents=True)
    frames = list(range(scene.frames)) if frames is None else list(frames)
    W, H = scene.size
    ids = [e.id for e in scene.elements]
    hashes: list[str] = []
    bboxes: dict[str, list[list[float]]] = {i: [] for i in ids}
    with sync_playwright() as p:
        browser = p.chromium.launch(args=["--disable-gpu", "--hide-scrollbars", "--force-color-profile=srgb"])
        page = browser.new_page(viewport={"width": W, "height": H}, device_scale_factor=1)
        page.goto(Path(html).resolve().as_uri())
        page.wait_for_function("window.__ready === true")
        for i, f in enumerate(frames):
            page.evaluate("f => window.__seek(f)", f)
            png = page.screenshot(type="png", clip={"x": 0, "y": 0, "width": W, "height": H}, animations="disabled")
            (frames_dir / f"f_{i:05d}.png").write_bytes(png)
            hashes.append(frame_hash(png))
            if probe:
                rects = page.evaluate("ids => ids.map(i => window.__bbox(i))", ids)
                for eid, r in zip(ids, rects):
                    bboxes[eid].append([float(v) for v in r])
        browser.close()
    result = RenderResult(frames_dir=frames_dir, frames=frames, hashes=hashes, bboxes=bboxes)
    if mp4:
        result.mp4 = write_mp4(frames_dir, scene.fps, out_dir / "render.mp4")
    (out_dir / "render.json").write_text(json.dumps({**asdict(result), "frames_dir": str(frames_dir),
                                                      "mp4": str(result.mp4) if result.mp4 else None}, indent=2))
    return result
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/test_renderer.py -v -m browser`
Expected: 4 PASS (needs `playwright install chromium` and `ffmpeg` on PATH)

- [ ] **Step 5: Commit**

```bash
git add keepframe/render/renderer.py tests/test_renderer.py
git commit -m "feat(render): deterministic frame-seek renderer with hashes, bbox probe and mp4"
```

---

### Task 8: Numpy compositor (reference render model)

**Files:**
- Create: `keepframe/analyze/composite.py`
- Test: `tests/test_composite.py`

**Interfaces:**
- Produces:
  - `load_texture(path: Path) -> np.ndarray` — RGBA float32 [0,1], shape (h,w,4); a 3-channel PNG gets alpha 1.
  - `texture_to_scene_affine(el: Element, props: dict[str,float], tex_shape: tuple[int,int]) -> np.ndarray` — 2×3 matrix mapping texture pixel coords to scene coords (handles anchor and canonical-vs-texture size).
  - `composite_element(canvas: np.ndarray, tex: np.ndarray, A: np.ndarray, opacity: float) -> None` — in-place premultiplied "source-over".
  - `composite_scene(scene: Scene, scene_dir: Path, f: int, cache: dict | None = None) -> np.ndarray` — RGB float32 (H,W,3) for frame `f`, elements drawn in ascending `eval_z`, only when `visible[0] <= f <= visible[1]`.
  - `hex_to_rgb(s: str) -> tuple[float,float,float]` in [0,1].

- [ ] **Step 1: Write the failing test**

```python
# tests/test_composite.py
import numpy as np, pytest
from keepframe.ir.schema import Scene, Element, Canonical, Background, Keyframe, Track
from keepframe.ir.synth import make_synthetic_scene, make_texture
from keepframe.analyze.composite import composite_scene, hex_to_rgb

def test_translate_only_places_texture_exactly(tmp_scene_dir):
    make_texture(tmp_scene_dir / "assets" / "e1.png", "rect", 20, 10, (0, 255, 0))
    el = Element(id="e1", kind="sprite", canonical=Canonical(width=20, height=10, texture="assets/e1.png"), visible=(0, 0),
                 tracks={"x": Track(keys=[Keyframe(t=0, v=110)]), "y": Track(keys=[Keyframe(t=0, v=55)])})
    scene = Scene(id="t", size=(200, 100), fps=30, frames=1, background=Background(value="#000000"), elements=[el])
    img = composite_scene(scene, tmp_scene_dir, 0)
    assert img.shape == (100, 200, 3)
    assert np.allclose(img[55, 110], [0, 1, 0], atol=1e-3)     # anchor (centre) lands at (110,55)
    assert np.allclose(img[50, 100], [0, 1, 0], atol=1e-3)     # top-left corner at (100,50)
    assert np.allclose(img[49, 99], [0, 0, 0], atol=1e-3)
    assert np.allclose(img[60, 130], [0, 0, 0], atol=1e-3)

def test_hex():
    assert hex_to_rgb("#ff8000") == pytest.approx((1.0, 128 / 255, 0.0))

@pytest.mark.browser
def test_compositor_matches_browser(tmp_scene_dir):
    from keepframe.compose.composer import compose
    from keepframe.render.renderer import render, load_frame
    scene = make_synthetic_scene(tmp_scene_dir, seed=5, with_text=False)
    html = compose(scene, tmp_scene_dir, tmp_scene_dir / "c.html")
    r = render(html, scene, tmp_scene_dir / "r", frames=[0, 20, 40])
    for i, f in enumerate([0, 20, 40]):
        a = composite_scene(scene, tmp_scene_dir, f)
        b = load_frame(r.frames_dir / f"f_{i:05d}.png")
        assert np.abs(a - b).mean() < 0.02, f
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_composite.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'keepframe.analyze.composite'`

- [ ] **Step 3: Implement**

```python
# keepframe/analyze/composite.py
from __future__ import annotations
from pathlib import Path
import cv2, numpy as np
from ..ir.schema import Element, Scene
from ..ir.tracks import affine_matrix, eval_props, eval_z


def hex_to_rgb(s: str) -> tuple[float, float, float]:
    s = s.lstrip("#")
    return tuple(int(s[i:i + 2], 16) / 255.0 for i in (0, 2, 4))  # type: ignore[return-value]


def load_texture(path: Path) -> np.ndarray:
    img = cv2.imread(str(path), cv2.IMREAD_UNCHANGED)
    if img is None:
        raise FileNotFoundError(path)
    if img.ndim == 2:
        img = cv2.cvtColor(img, cv2.COLOR_GRAY2BGRA)
    elif img.shape[2] == 3:
        img = cv2.cvtColor(img, cv2.COLOR_BGR2BGRA)
    rgba = cv2.cvtColor(img, cv2.COLOR_BGRA2RGBA).astype(np.float32) / 255.0
    return rgba


def texture_to_scene_affine(el: Element, props: dict[str, float], tex_shape: tuple[int, int]) -> np.ndarray:
    th, tw = tex_shape
    c = el.canonical
    ax, ay = c.anchor
    # texture pixel -> local canonical coords (anchor at origin)
    L = np.array([[c.width / tw, 0.0, -ax * c.width], [0.0, c.height / th, -ay * c.height], [0.0, 0.0, 1.0]])
    M = affine_matrix(props) @ L
    return M[:2, :]


def composite_element(canvas: np.ndarray, tex: np.ndarray, A: np.ndarray, opacity: float) -> None:
    H, W = canvas.shape[:2]
    prem = tex.copy()
    prem[..., :3] *= prem[..., 3:4]
    warped = cv2.warpAffine(prem, A, (W, H), flags=cv2.INTER_LINEAR, borderMode=cv2.BORDER_CONSTANT, borderValue=0)
    a = warped[..., 3:4] * opacity
    canvas *= (1.0 - a)
    canvas += warped[..., :3] * opacity


def composite_scene(scene: Scene, scene_dir: Path, f: int, cache: dict | None = None) -> np.ndarray:
    W, H = scene.size
    canvas = np.empty((H, W, 3), np.float32)
    canvas[:] = hex_to_rgb(scene.background.value) if scene.background.kind == "color" else (0, 0, 0)
    cache = {} if cache is None else cache
    order = sorted((e for e in scene.elements if e.visible[0] <= f <= e.visible[1]), key=lambda e: eval_z(e, f))
    for el in order:
        if not el.canonical.texture:
            continue
        tex = cache.get(el.canonical.texture)
        if tex is None:
            tex = cache[el.canonical.texture] = load_texture(Path(scene_dir) / el.canonical.texture)
        p = eval_props(el, f)
        composite_element(canvas, tex, texture_to_scene_affine(el, p, tex.shape[:2]), p["opacity"])
    return canvas
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/test_composite.py -v` then `pytest tests/test_composite.py -v -m browser`
Expected: PASS. If `test_compositor_matches_browser` fails, the culprit is almost always sub-pixel interpolation; the threshold 0.02 mean L1 is generous, so investigate `texture_to_scene_affine` before touching the threshold.

- [ ] **Step 5: Commit**

```bash
git add keepframe/analyze/composite.py tests/test_composite.py
git commit -m "feat(analyze): numpy compositor matching the browser render model"
```

---

### Task 9: Animation matrix and motion intervals

**Files:**
- Create: `keepframe/verify/matrix.py`
- Test: `tests/test_matrix.py`

**Interfaces:**
- Produces:
  - `COLS = ("x","y","sx","sy","rot","opacity")`
  - `animation_matrix(scene: Scene) -> dict[str, np.ndarray]` — per element, array `(scene.frames, 6)` in `COLS` order; rows outside `visible` are NaN.
  - `bbox_matrix(scene: Scene) -> dict[str, np.ndarray]` — per element `(frames, 4)` = x0,y0,x1,y1; NaN when not visible.
  - `@dataclass Motion: id: str; element: str; type: str; start: int; end: int; dir: tuple[float,float] | None; mag: float; dur: int` where `type ∈ {"translation","rotation","scale","opacity"}`, `end` is the frame where the value stops changing (inclusive), `dur = end - start`.
  - `EPS = {"translation": 0.5, "rotation": 0.25, "scale": 0.002, "opacity": 0.005}` (per-frame change thresholds)
  - `extract_motions(scene: Scene, eps: dict[str, float] | None = None) -> list[Motion]` — ids `m_<element>_<n>` numbered per element by start frame (ties by type order translation, rotation, scale, opacity). Runs shorter than 2 frames are ignored; gaps of 1 frame are bridged.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_matrix.py
import numpy as np
from keepframe.ir.schema import Scene, Element, Canonical, Background, Keyframe, Track
from keepframe.ir.tracks import PRESET_EASES
from keepframe.verify.matrix import animation_matrix, bbox_matrix, extract_motions, COLS

def scene():
    e1 = Element(id="e1", kind="sprite", canonical=Canonical(width=20, height=20), visible=(0, 59),
                 tracks={"x": Track(keys=[Keyframe(t=0, v=0, ease=PRESET_EASES["out_quad"]), Keyframe(t=20, v=100)]),
                         "rot": Track(keys=[Keyframe(t=30, v=0), Keyframe(t=50, v=90)])})
    e2 = Element(id="e2", kind="sprite", canonical=Canonical(width=10, height=10), visible=(10, 59),
                 tracks={"opacity": Track(keys=[Keyframe(t=10, v=0), Keyframe(t=20, v=1)]),
                         "sx": Track(keys=[Keyframe(t=25, v=1), Keyframe(t=45, v=2)]),
                         "sy": Track(keys=[Keyframe(t=25, v=1), Keyframe(t=45, v=2)])})
    return Scene(id="s", size=(200, 200), fps=30, frames=60, background=Background(), elements=[e1, e2])

def test_matrix_shape_and_nan_outside_visible():
    m = animation_matrix(scene())
    assert m["e1"].shape == (60, len(COLS)) and m["e2"].shape == (60, 6)
    assert np.isnan(m["e2"][5]).all() and not np.isnan(m["e2"][10]).any()
    assert m["e1"][20, 0] == 100.0

def test_bbox_matrix():
    b = bbox_matrix(scene())
    assert np.allclose(b["e1"][0], [-10, -10, 10, 10])

def test_motions_extracted_in_order_with_types():
    ms = extract_motions(scene())
    by = {m.id: m for m in ms}
    assert [m.id for m in ms if m.element == "e1"] == ["m_e1_1", "m_e1_2"]
    t = by["m_e1_1"]
    assert t.type == "translation" and t.start == 0 and 18 <= t.end <= 20 and t.dir == (1.0, 0.0) and abs(t.mag - 100) < 1
    r = by["m_e1_2"]
    assert r.type == "rotation" and r.start == 30 and abs(r.mag - 90) < 1 and r.dir is None
    o, s = by["m_e2_1"], by["m_e2_2"]
    assert o.type == "opacity" and s.type == "scale" and abs(s.mag - 2.0) < 0.05
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_matrix.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'keepframe.verify.matrix'`

- [ ] **Step 3: Implement**

```python
# keepframe/verify/matrix.py
from __future__ import annotations
from dataclasses import dataclass
import numpy as np
from ..ir.schema import Scene
from ..ir.tracks import element_bbox, eval_props

COLS = ("x", "y", "sx", "sy", "rot", "opacity")
EPS = {"translation": 0.5, "rotation": 0.25, "scale": 0.002, "opacity": 0.005}
TYPE_ORDER = ("translation", "rotation", "scale", "opacity")


def animation_matrix(scene: Scene) -> dict[str, np.ndarray]:
    out = {}
    for el in scene.elements:
        m = np.full((scene.frames, len(COLS)), np.nan)
        for f in range(el.visible[0], min(el.visible[1], scene.frames - 1) + 1):
            p = eval_props(el, f)
            m[f] = [p[c] for c in COLS]
        out[el.id] = m
    return out


def bbox_matrix(scene: Scene) -> dict[str, np.ndarray]:
    out = {}
    for el in scene.elements:
        b = np.full((scene.frames, 4), np.nan)
        for f in range(el.visible[0], min(el.visible[1], scene.frames - 1) + 1):
            b[f] = element_bbox(el, f)
        out[el.id] = b
    return out


@dataclass
class Motion:
    id: str
    element: str
    type: str
    start: int
    end: int
    dir: tuple[float, float] | None
    mag: float
    dur: int


def _runs(moving: np.ndarray) -> list[tuple[int, int]]:
    """Contiguous True runs over the derivative index; bridges single-frame gaps; drops runs < 2."""
    idx = np.flatnonzero(moving)
    if idx.size == 0:
        return []
    runs, s, prev = [], idx[0], idx[0]
    for i in idx[1:]:
        if i - prev > 2:  # gap of 2+ frames ends the run (a 1-frame gap is bridged)
            runs.append((s, prev)); s = i
        prev = i
    runs.append((s, prev))
    return [(a, b) for a, b in runs if b - a + 1 >= 2]


def extract_motions(scene: Scene, eps: dict[str, float] | None = None) -> list[Motion]:
    eps = {**EPS, **(eps or {})}
    mat = animation_matrix(scene)
    motions: list[Motion] = []
    for el in scene.elements:
        m = mat[el.id]
        d = np.diff(m, axis=0)  # d[f] = m[f+1]-m[f]
        found: list[tuple[int, str, int, tuple | None, float]] = []
        signals = {
            "translation": np.hypot(d[:, 0], d[:, 1]),
            "rotation": np.abs(d[:, 4]),
            "scale": np.abs(d[:, 2]) + np.abs(d[:, 3]),
            "opacity": np.abs(d[:, 5]),
        }
        for typ in TYPE_ORDER:
            sig = np.nan_to_num(signals[typ], nan=0.0)
            for a, b in _runs(sig > eps[typ]):
                start, end = int(a), int(b) + 1
                if typ == "translation":
                    v = m[end, :2] - m[start, :2]
                    n = float(np.hypot(*v))
                    direction = (float(v[0] / n), float(v[1] / n)) if n > 0 else None
                    mag = n
                elif typ == "rotation":
                    direction, mag = None, float(m[end, 4] - m[start, 4])
                elif typ == "scale":
                    direction = None
                    mag = float(0.5 * (m[end, 2] / m[start, 2] + m[end, 3] / m[start, 3]))
                else:
                    direction, mag = None, float(m[end, 5] - m[start, 5])
                found.append((start, typ, end, direction, mag))
        found.sort(key=lambda r: (r[0], TYPE_ORDER.index(r[1])))
        for n, (start, typ, end, direction, mag) in enumerate(found, 1):
            motions.append(Motion(id=f"m_{el.id}_{n}", element=el.id, type=typ, start=start, end=end,
                                  dir=direction, mag=mag, dur=end - start))
    return motions
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/test_matrix.py -v`
Expected: 3 PASS

- [ ] **Step 5: Commit**

```bash
git add keepframe/verify/matrix.py tests/test_matrix.py
git commit -m "feat(verify): animation matrix, bbox matrix and motion interval extraction"
```

---

### Task 10: Predicate parser and evaluator (MoVer subset)

**Files:**
- Create: `keepframe/verify/predicates.py`
- Test: `tests/test_predicates.py`

**Interfaces:**
- Grammar (strings stored in `Constraint.pred`): `name(arg, arg, ...)` optionally followed by `@<frame>`. Args are identifiers (`e1`, `m_e1_1`), single-quoted strings (`'translation'`), numbers, or 2-vectors (`[1,0]`).
- Supported predicates: `type(m,'translation'|'rotation'|'scale'|'opacity')`, `dir(m,[dx,dy])` (cosine ≥ 0.9, translation only), `mag(m,v)` (within max(10 %, 2 units)), `dur(m,frames)` (±2 frames), `before(m1,m2)`, `after(m1,m2)`, `while(m1,m2)`, `left(o1,o2)`, `right(o1,o2)`, `top(o1,o2)`, `bottom(o1,o2)`, `intersect(o1,o2)`. Spatial predicates default to the last frame; `@f` selects a frame. Unknown motion or element ids evaluate to `False`; unknown predicate names raise `ValueError`.
- Produces:
  - `parse_pred(s: str) -> tuple[str, list, int | None]` — (name, args, frame)
  - `@dataclass PredContext: scene: Scene; motions: dict[str, Motion]; bboxes: dict[str, np.ndarray]`
  - `build_context(scene: Scene) -> PredContext`
  - `eval_pred(s: str, ctx: PredContext) -> bool`
  - `TOL = {"cos": 0.9, "mag_rel": 0.10, "mag_abs": 2.0, "dur_frames": 2, "px": 2.0}`

- [ ] **Step 1: Write the failing test**

```python
# tests/test_predicates.py
import pytest
from keepframe.ir.schema import Scene, Element, Canonical, Background, Keyframe, Track
from keepframe.verify.predicates import parse_pred, build_context, eval_pred

def scene():
    e1 = Element(id="e1", kind="sprite", canonical=Canonical(width=20, height=20), visible=(0, 59),
                 tracks={"x": Track(keys=[Keyframe(t=0, v=0), Keyframe(t=20, v=100)]), "y": Track(keys=[Keyframe(t=0, v=50)])})
    e2 = Element(id="e2", kind="sprite", canonical=Canonical(width=20, height=20), visible=(0, 59),
                 tracks={"x": Track(keys=[Keyframe(t=0, v=150)]), "y": Track(keys=[Keyframe(t=0, v=50)]),
                         "rot": Track(keys=[Keyframe(t=30, v=0), Keyframe(t=40, v=45)])})
    return Scene(id="s", size=(200, 100), fps=30, frames=60, background=Background(), elements=[e1, e2])

def test_parse():
    assert parse_pred("dir(m_e1_1,[1,0])") == ("dir", ["m_e1_1", [1.0, 0.0]], None)
    assert parse_pred("type(m_e1_1,'translation')") == ("type", ["m_e1_1", "translation"], None)
    assert parse_pred("left(e1,e2)@10") == ("left", ["e1", "e2"], 10)

@pytest.mark.parametrize("pred,expected", [
    ("type(m_e1_1,'translation')", True), ("type(m_e1_1,'rotation')", False),
    ("dir(m_e1_1,[1,0])", True), ("dir(m_e1_1,[-1,0])", False),
    ("mag(m_e1_1,100)", True), ("mag(m_e1_1,60)", False),
    ("dur(m_e1_1,20)", True), ("dur(m_e1_1,10)", False),
    ("before(m_e1_1,m_e2_1)", True), ("after(m_e1_1,m_e2_1)", False), ("while(m_e1_1,m_e2_1)", False),
    ("left(e1,e2)", True), ("right(e1,e2)", False), ("intersect(e1,e2)", False), ("left(e1,e2)@0", True),
    ("type(m_e9_1,'translation')", False),
])
def test_eval(pred, expected):
    ctx = build_context(scene())
    assert eval_pred(pred, ctx) is expected

def test_unknown_predicate_raises():
    with pytest.raises(ValueError):
        eval_pred("dance(e1)", build_context(scene()))
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_predicates.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'keepframe.verify.predicates'`

- [ ] **Step 3: Implement**

```python
# keepframe/verify/predicates.py
from __future__ import annotations
import re
from dataclasses import dataclass
import numpy as np
from ..ir.schema import Scene
from .matrix import Motion, bbox_matrix, extract_motions

TOL = {"cos": 0.9, "mag_rel": 0.10, "mag_abs": 2.0, "dur_frames": 2, "px": 2.0}
_HEAD = re.compile(r"^\s*([a-z_]+)\s*\((.*)\)\s*(?:@\s*(\d+))?\s*$")


def _split_args(s: str) -> list[str]:
    out, depth, cur = [], 0, ""
    for ch in s:
        if ch == "[":
            depth += 1
        elif ch == "]":
            depth -= 1
        if ch == "," and depth == 0:
            out.append(cur.strip()); cur = ""
        else:
            cur += ch
    if cur.strip():
        out.append(cur.strip())
    return out


def _arg(tok: str):
    if tok.startswith("'") and tok.endswith("'"):
        return tok[1:-1]
    if tok.startswith("["):
        return [float(x) for x in tok[1:-1].split(",")]
    try:
        return float(tok)
    except ValueError:
        return tok


def parse_pred(s: str) -> tuple[str, list, int | None]:
    m = _HEAD.match(s)
    if not m:
        raise ValueError(f"bad predicate syntax: {s!r}")
    name, args, frame = m.group(1), [_arg(t) for t in _split_args(m.group(2))], m.group(3)
    return name, args, (int(frame) if frame is not None else None)


@dataclass
class PredContext:
    scene: Scene
    motions: dict[str, Motion]
    bboxes: dict[str, np.ndarray]


def build_context(scene: Scene) -> PredContext:
    return PredContext(scene=scene, motions={m.id: m for m in extract_motions(scene)}, bboxes=bbox_matrix(scene))


def _bbox(ctx: PredContext, eid, frame: int | None):
    f = ctx.scene.frames - 1 if frame is None else frame
    b = ctx.bboxes.get(eid)
    if b is None or f < 0 or f >= len(b) or np.isnan(b[f]).any():
        return None
    return b[f]


def eval_pred(s: str, ctx: PredContext) -> bool:
    name, a, frame = parse_pred(s)
    M, px = ctx.motions, TOL["px"]
    if name == "type":
        return a[0] in M and M[a[0]].type == a[1]
    if name == "dir":
        m = M.get(a[0])
        if m is None or m.dir is None:
            return False
        v = np.array(a[1], float); n = np.linalg.norm(v)
        return n > 0 and float(np.dot(m.dir, v / n)) >= TOL["cos"]
    if name == "mag":
        m = M.get(a[0])
        return m is not None and abs(m.mag - a[1]) <= max(TOL["mag_rel"] * abs(a[1]), TOL["mag_abs"])
    if name == "dur":
        m = M.get(a[0])
        return m is not None and abs(m.dur - a[1]) <= TOL["dur_frames"]
    if name in ("before", "after", "while"):
        m1, m2 = M.get(a[0]), M.get(a[1])
        if m1 is None or m2 is None:
            return False
        if name == "before":
            return m1.end <= m2.start
        if name == "after":
            return m2.end <= m1.start
        return min(m1.end, m2.end) - max(m1.start, m2.start) >= 1
    if name in ("left", "right", "top", "bottom", "intersect"):
        b1, b2 = _bbox(ctx, a[0], frame), _bbox(ctx, a[1], frame)
        if b1 is None or b2 is None:
            return False
        if name == "left":
            return b1[2] <= b2[0] + px
        if name == "right":
            return b1[0] >= b2[2] - px
        if name == "top":
            return b1[3] <= b2[1] + px
        if name == "bottom":
            return b1[1] >= b2[3] - px
        ox = min(b1[2], b2[2]) - max(b1[0], b2[0]); oy = min(b1[3], b2[3]) - max(b1[1], b2[1])
        return ox > 0 and oy > 0
    raise ValueError(f"unknown predicate: {name}")
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/test_predicates.py -v`
Expected: all PASS

- [ ] **Step 5: Commit**

```bash
git add keepframe/verify/predicates.py tests/test_predicates.py
git commit -m "feat(verify): MoVer-subset predicate parser and evaluator"
```

---

### Task 11: Constraint extraction from tracks

**Files:**
- Create: `keepframe/analyze/constraints.py`
- Test: `tests/test_constraints.py`

**Interfaces:**
- Produces: `extract_constraints(scene: Scene) -> list[Constraint]` — for every motion: `type`, `dir` (translation only, rounded to 2 decimals), `mag` (rounded to 1 decimal), `dur`; for every pair of motions of different elements exactly one temporal relation (`before`/`after`/`while`, whichever holds; skipped if none does, which happens only when intervals touch without overlapping—`before` covers `end <= start`); for every pair of elements visible at the last frame all of `left/right/top/bottom` that hold plus `intersect` when it holds. All `keep=False`. Output order is deterministic (motion id order, then pair order).
- Invariant tested: every extracted constraint evaluates `True` on the scene it came from.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_constraints.py
from keepframe.ir.synth import make_synthetic_scene
from keepframe.ir.schema import Keyframe
from keepframe.analyze.constraints import extract_constraints
from keepframe.verify.predicates import build_context, eval_pred

def test_extracted_constraints_hold_on_source_scene(tmp_scene_dir):
    for seed in range(1, 6):
        scene = make_synthetic_scene(tmp_scene_dir / str(seed), seed=seed)
        cs = extract_constraints(scene)
        assert cs and all(c.keep is False for c in cs)
        ctx = build_context(scene)
        failing = [c.pred for c in cs if not eval_pred(c.pred, ctx)]
        assert failing == [], (seed, failing)

def test_reversed_direction_breaks_dir_constraint(tmp_scene_dir):
    scene = make_synthetic_scene(tmp_scene_dir, seed=2, with_text=False)
    cs = [c for c in extract_constraints(scene) if c.pred.startswith("dir(m_e1_1")]
    assert cs
    flipped = scene.model_copy(deep=True)
    for prop in ("x", "y"):   # reverse the whole displacement so the direction vector flips
        ks = flipped.elements[0].tracks[prop].keys
        v0, v1 = ks[0].v, ks[-1].v
        flipped.elements[0].tracks[prop].keys = [Keyframe(t=ks[0].t, v=v1), Keyframe(t=ks[-1].t, v=v0)]
    assert eval_pred(cs[0].pred, build_context(flipped)) is False
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_constraints.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'keepframe.analyze.constraints'`

- [ ] **Step 3: Implement**

```python
# keepframe/analyze/constraints.py
from __future__ import annotations
from itertools import combinations
from ..ir.schema import Constraint, Scene
from ..verify.matrix import extract_motions
from ..verify.predicates import build_context, eval_pred


def extract_constraints(scene: Scene) -> list[Constraint]:
    motions = extract_motions(scene)
    preds: list[str] = []
    for m in motions:
        preds.append(f"type({m.id},'{m.type}')")
        if m.type == "translation" and m.dir is not None:
            preds.append(f"dir({m.id},[{m.dir[0]:.2f},{m.dir[1]:.2f}])")
        preds.append(f"mag({m.id},{m.mag:.1f})")
        preds.append(f"dur({m.id},{m.dur})")
    for m1, m2 in combinations(motions, 2):
        if m1.element == m2.element:
            continue
        if m1.end <= m2.start:
            preds.append(f"before({m1.id},{m2.id})")
        elif m2.end <= m1.start:
            preds.append(f"after({m1.id},{m2.id})")
        elif min(m1.end, m2.end) - max(m1.start, m2.start) >= 1:
            preds.append(f"while({m1.id},{m2.id})")
    last = scene.frames - 1
    ctx = build_context(scene)
    visible = [e for e in scene.elements if e.visible[0] <= last <= e.visible[1]]
    for a, b in combinations(visible, 2):
        for rel in ("left", "right", "top", "bottom", "intersect"):
            p = f"{rel}({a.id},{b.id})"
            if eval_pred(p, ctx):
                preds.append(p)
    return [Constraint(pred=p, keep=False) for p in preds]
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/test_constraints.py -v`
Expected: 2 PASS. If `test_extracted_constraints_hold_on_source_scene` fails on `mag`/`dur`, the rounding in the string differs from the motion value by more than the tolerance only when `mag < 2`; `TOL["mag_abs"]=2.0` covers it, so a failure means `extract_motions` is nondeterministic—fix that, not the tolerance.

- [ ] **Step 5: Commit**

```bash
git add keepframe/analyze/constraints.py tests/test_constraints.py
git commit -m "feat(analyze): extract MoVer-style constraints from tracks"
```

---

### Task 12: Similarity metrics (temporal, appearance)

**Files:**
- Create: `keepframe/verify/similarity.py`
- Test: `tests/test_similarity.py`

**Interfaces:**
- Produces:
  - `centroid_tracks(scene: Scene) -> dict[str, np.ndarray]` — `(frames, 2)` anchor positions, NaN when invisible.
  - `tracklet_correlation(a: np.ndarray, b: np.ndarray, static_eps: float = 0.25) -> float` — Animation2Code-style displacement agreement: per frame `cos(da,db) * min(|da|,|db|)/max(|da|,|db|)`; frames where both are static count 1.0, where exactly one is static count 0.0; averaged over frames where both are defined; 0.0 if none.
  - `temporal_similarity(ref: dict[str,np.ndarray], out: dict[str,np.ndarray]) -> float` — symmetric Chamfer aggregation (mean over ref of best out, and vice versa, averaged), clamped to [0,1]; 0.0 if either side is empty.
  - `appearance_similarity(tex_a: np.ndarray, tex_b: np.ndarray) -> float` — `1 - mean L1` of premultiplied RGBA after resizing `b` to `a`'s size.
  - `frame_l1(a: np.ndarray, b: np.ndarray) -> float`.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_similarity.py
import numpy as np, pytest
from keepframe.ir.synth import make_synthetic_scene
from keepframe.verify.similarity import centroid_tracks, tracklet_correlation, temporal_similarity, appearance_similarity, frame_l1

def test_identical_scene_scores_one(tmp_scene_dir):
    s = make_synthetic_scene(tmp_scene_dir, seed=4)
    c = centroid_tracks(s)
    assert temporal_similarity(c, c) == pytest.approx(1.0, abs=1e-6)

def test_reversed_motion_scores_low():
    t = np.linspace(0, 100, 30)
    a = np.c_[t, np.zeros_like(t)]
    b = np.c_[t[::-1], np.zeros_like(t)]
    assert tracklet_correlation(a, a) == pytest.approx(1.0)
    assert tracklet_correlation(a, b) < 0.0
    assert temporal_similarity({"e": a}, {"e": b}) == 0.0   # clamped

def test_static_vs_moving_is_zero_and_speed_ratio_counts():
    t = np.linspace(0, 100, 30)
    moving = np.c_[t, np.zeros_like(t)]
    static = np.zeros_like(moving)
    assert tracklet_correlation(moving, static) == pytest.approx(0.0)
    half = np.c_[t / 2, np.zeros_like(t)]
    assert tracklet_correlation(moving, half) == pytest.approx(0.5)

def test_appearance_and_frame_l1():
    a = np.zeros((10, 10, 4), np.float32); a[..., 3] = 1; a[..., 0] = 1
    b = a.copy(); b[..., 0] = 0.5
    assert appearance_similarity(a, a) == pytest.approx(1.0)
    assert appearance_similarity(a, b) == pytest.approx(1 - 0.5 / 4)
    assert frame_l1(np.zeros((2, 2, 3)), np.ones((2, 2, 3))) == 1.0
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_similarity.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'keepframe.verify.similarity'`

- [ ] **Step 3: Implement**

```python
# keepframe/verify/similarity.py
from __future__ import annotations
import cv2, numpy as np
from ..ir.schema import Scene
from .matrix import animation_matrix


def centroid_tracks(scene: Scene) -> dict[str, np.ndarray]:
    return {eid: m[:, :2].copy() for eid, m in animation_matrix(scene).items()}


def tracklet_correlation(a: np.ndarray, b: np.ndarray, static_eps: float = 0.25) -> float:
    n = min(len(a), len(b)) - 1
    if n < 1:
        return 0.0
    da, db = np.diff(a[: n + 1], axis=0), np.diff(b[: n + 1], axis=0)
    ok = ~(np.isnan(da).any(1) | np.isnan(db).any(1))
    if not ok.any():
        return 0.0
    da, db = da[ok], db[ok]
    na, nb = np.linalg.norm(da, axis=1), np.linalg.norm(db, axis=1)
    both_static = (na < static_eps) & (nb < static_eps)
    one_static = ((na < static_eps) | (nb < static_eps)) & ~both_static
    with np.errstate(divide="ignore", invalid="ignore"):
        cos = (da * db).sum(1) / (na * nb)
        ratio = np.minimum(na, nb) / np.maximum(na, nb)
    score = np.where(both_static, 1.0, np.where(one_static, 0.0, cos * ratio))
    return float(np.mean(score))


def temporal_similarity(ref: dict[str, np.ndarray], out: dict[str, np.ndarray]) -> float:
    if not ref or not out:
        return 0.0
    r2o = np.mean([max(tracklet_correlation(r, o) for o in out.values()) for r in ref.values()])
    o2r = np.mean([max(tracklet_correlation(o, r) for r in ref.values()) for o in out.values()])
    return float(np.clip(0.5 * (r2o + o2r), 0.0, 1.0))


def appearance_similarity(tex_a: np.ndarray, tex_b: np.ndarray) -> float:
    if tex_b.shape[:2] != tex_a.shape[:2]:
        tex_b = cv2.resize(tex_b, (tex_a.shape[1], tex_a.shape[0]), interpolation=cv2.INTER_AREA)
    pa, pb = tex_a.copy(), tex_b.copy()
    pa[..., :3] *= pa[..., 3:4]; pb[..., :3] *= pb[..., 3:4]
    return float(1.0 - np.abs(pa - pb).mean())


def frame_l1(a: np.ndarray, b: np.ndarray) -> float:
    return float(np.abs(a.astype(np.float32) - b.astype(np.float32)).mean())
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/test_similarity.py -v`
Expected: 4 PASS

- [ ] **Step 5: Commit**

```bash
git add keepframe/verify/similarity.py tests/test_similarity.py
git commit -m "feat(verify): temporal displacement-correlation and appearance similarity"
```

---

### Task 13: Verifier report

**Files:**
- Create: `keepframe/verify/verifier.py`
- Test: `tests/test_verifier.py`

**Interfaces:**
- Produces:
  - `class LayerError(BaseModel): element: str; frame: int; expected: list[float]; actual: list[float]; err_px: float`
  - `class VerifyReport(BaseModel): schema_ok: bool; keep_results: list[dict]  # {"pred": str, "passed": bool}; keep_pass_rate: float; layer_errors: list[LayerError]; layer_max_err_px: float; temporal: float | None; appearance: float | None; passed: bool; messages: list[str]`
  - `verify(scene: Scene, scene_dir: Path, render_result: RenderResult | None = None, reference: Scene | None = None, reference_dir: Path | None = None, tol_px: float = 2.0) -> VerifyReport`
  - `passed` = `schema_ok and keep_pass_rate == 1.0 and layer_max_err_px <= tol_px`. Similarity never gates `passed` (it is reported for the M2 gate).

- [ ] **Step 1: Write the failing test**

```python
# tests/test_verifier.py
import pytest
from keepframe.ir.synth import make_synthetic_scene
from keepframe.ir.schema import Constraint, Keyframe
from keepframe.analyze.constraints import extract_constraints
from keepframe.verify.verifier import verify

def test_keep_predicates_gate(tmp_scene_dir):
    scene = make_synthetic_scene(tmp_scene_dir, seed=8, with_text=False)
    scene.constraints = [c.model_copy(update={"keep": True}) for c in extract_constraints(scene)]
    ok = verify(scene, tmp_scene_dir)
    assert ok.schema_ok and ok.keep_pass_rate == 1.0 and ok.passed
    broken = scene.model_copy(deep=True)
    xs = broken.elements[0].tracks["x"].keys
    broken.elements[0].tracks["x"].keys = [Keyframe(t=xs[0].t, v=xs[-1].v), Keyframe(t=xs[-1].t, v=xs[0].v)]
    bad = verify(broken, tmp_scene_dir, reference=scene, reference_dir=tmp_scene_dir)
    assert bad.keep_pass_rate < 1.0 and not bad.passed
    assert bad.temporal is not None and bad.temporal < 1.0 and bad.appearance == pytest.approx(1.0)

def test_missing_texture_fails_schema(tmp_scene_dir):
    scene = make_synthetic_scene(tmp_scene_dir, seed=8, with_text=False)
    scene.elements[0].canonical.texture = "assets/nope.png"
    r = verify(scene, tmp_scene_dir)
    assert not r.schema_ok and not r.passed and any("nope.png" in m for m in r.messages)

@pytest.mark.browser
def test_layer_check_against_render(tmp_scene_dir):
    from keepframe.compose.composer import compose
    from keepframe.render.renderer import render
    scene = make_synthetic_scene(tmp_scene_dir, seed=8, with_text=False)
    rr = render(compose(scene, tmp_scene_dir, tmp_scene_dir / "c.html"), scene, tmp_scene_dir / "r", frames=[0, 30, 59])
    r = verify(scene, tmp_scene_dir, render_result=rr)
    assert r.layer_max_err_px <= 2.0 and r.passed, r.layer_errors
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_verifier.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'keepframe.verify.verifier'`

- [ ] **Step 3: Implement**

```python
# keepframe/verify/verifier.py
from __future__ import annotations
from pathlib import Path
import numpy as np
from pydantic import BaseModel, Field
from ..ir.schema import Scene
from ..ir.tracks import element_bbox
from ..render.renderer import RenderResult
from ..analyze.composite import load_texture
from .predicates import build_context, eval_pred
from .similarity import appearance_similarity, centroid_tracks, temporal_similarity


class LayerError(BaseModel):
    element: str
    frame: int
    expected: list[float]
    actual: list[float]
    err_px: float


class VerifyReport(BaseModel):
    schema_ok: bool
    keep_results: list[dict] = Field(default_factory=list)
    keep_pass_rate: float = 1.0
    layer_errors: list[LayerError] = Field(default_factory=list)
    layer_max_err_px: float = 0.0
    temporal: float | None = None
    appearance: float | None = None
    passed: bool = False
    messages: list[str] = Field(default_factory=list)


def verify(scene: Scene, scene_dir: Path, render_result: RenderResult | None = None,
           reference: Scene | None = None, reference_dir: Path | None = None, tol_px: float = 2.0) -> VerifyReport:
    scene_dir = Path(scene_dir)
    rep = VerifyReport(schema_ok=True)
    try:
        Scene.model_validate(scene.model_dump(by_alias=True))
    except Exception as e:  # pydantic ValidationError
        rep.schema_ok = False; rep.messages.append(f"schema: {e}")
    for el in scene.elements:
        if el.canonical.texture and not (scene_dir / el.canonical.texture).exists():
            rep.schema_ok = False; rep.messages.append(f"missing texture {el.canonical.texture} for {el.id}")
    ctx = build_context(scene)
    keep = [c for c in scene.constraints if c.keep]
    for c in keep:
        ok = eval_pred(c.pred, ctx)
        rep.keep_results.append({"pred": c.pred, "passed": ok})
    rep.keep_pass_rate = (sum(r["passed"] for r in rep.keep_results) / len(keep)) if keep else 1.0
    if render_result is not None:
        for el in scene.elements:
            if not render_result.bboxes.get(el.id):   # rendered with probe=False
                continue
            for i, f in enumerate(render_result.frames):
                if not (el.visible[0] <= f <= el.visible[1]):
                    continue
                exp = np.array(element_bbox(el, f)); act = np.array(render_result.bboxes[el.id][i])
                err = float(np.abs(exp - act).max())
                rep.layer_max_err_px = max(rep.layer_max_err_px, err)
                if err > tol_px:
                    rep.layer_errors.append(LayerError(element=el.id, frame=f, expected=exp.tolist(), actual=act.tolist(), err_px=err))
    if reference is not None:
        rep.temporal = temporal_similarity(centroid_tracks(reference), centroid_tracks(scene))
        if reference_dir is not None:
            scores = []
            for el in scene.elements:
                try:
                    ref_el = reference.element(el.id)
                except KeyError:
                    continue
                if el.canonical.texture and ref_el.canonical.texture:
                    scores.append(appearance_similarity(load_texture(Path(reference_dir) / ref_el.canonical.texture),
                                                        load_texture(scene_dir / el.canonical.texture)))
            rep.appearance = float(np.mean(scores)) if scores else None
    rep.passed = rep.schema_ok and rep.keep_pass_rate == 1.0 and rep.layer_max_err_px <= tol_px
    return rep
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/test_verifier.py -v` (and `-m browser` for the render check)
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add keepframe/verify/verifier.py tests/test_verifier.py
git commit -m "feat(verify): verifier report with keep predicates, layer bbox check and similarity"
```

---

### Task 14: CLI and M1 gate

**Files:**
- Create: `keepframe/cli.py`, `keepframe/gates.py`, `scripts/m1_gate.py`
- Test: `tests/test_cli.py`

**Interfaces:**
- CLI (`keepframe <cmd>`):
  - `synth --out DIR --seed N [--frames 60] [--no-text]` → writes `DIR/scene.json` + `DIR/assets/`
  - `compose --scene SCENE.json --out HTML`  (scene dir = the scene file's parent)
  - `render --scene SCENE.json --html HTML --out DIR [--frames 0,10,20] [--mp4]`
  - `verify --scene SCENE.json [--render-json DIR/render.json] [--reference REF.json]` → prints report JSON, exit 1 when `passed` is false
  - `gate-m1 --out DIR [--n 20]` → runs `gates.m1_gate`, prints table, exit 1 on failure
- `gates.m1_gate(out_root: Path, n: int = 20, frames_per_scene: int = 3) -> dict` — for seeds `1..n`: synth → extract constraints → all predicates hold; compose → render the frame set twice → hashes equal; layer bbox error ≤ 2 px. Returns `{"n": n, "constraints_ok": int, "hash_ok": int, "layer_ok": int, "passed": bool, "rows": [...]}`; `passed` requires all three counts == n (spec §11 M1).
- `render_result_from_json(path: Path) -> RenderResult` (in `keepframe/render/renderer.py`, add in this task).

- [ ] **Step 1: Write the failing test**

```python
# tests/test_cli.py
import json, subprocess, sys, pytest
from keepframe.gates import m1_gate

def run(*args):
    return subprocess.run([sys.executable, "-m", "keepframe.cli", *args], capture_output=True, text=True)

def test_synth_compose_verify_cli(tmp_scene_dir):
    d = tmp_scene_dir / "s"
    assert run("synth", "--out", str(d), "--seed", "11").returncode == 0
    assert (d / "scene.json").exists()
    assert run("compose", "--scene", str(d / "scene.json"), "--out", str(d / "c.html")).returncode == 0
    r = run("verify", "--scene", str(d / "scene.json"))
    assert r.returncode == 0 and json.loads(r.stdout)["passed"] is True

@pytest.mark.browser
def test_m1_gate_small(tmp_scene_dir):
    res = m1_gate(tmp_scene_dir, n=3)
    assert res["passed"], res
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_cli.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'keepframe.gates'`

- [ ] **Step 3: Implement**

Add to `keepframe/render/renderer.py`:

```python
def render_result_from_json(path: Path) -> RenderResult:
    d = json.loads(Path(path).read_text())
    return RenderResult(frames_dir=Path(d["frames_dir"]), frames=d["frames"], hashes=d["hashes"],
                        bboxes=d["bboxes"], mp4=Path(d["mp4"]) if d.get("mp4") else None)
```

```python
# keepframe/gates.py
from __future__ import annotations
from pathlib import Path
from .analyze.constraints import extract_constraints
from .compose.composer import compose
from .ir.store import save_scene
from .ir.synth import make_synthetic_scene
from .render.renderer import render
from .verify.predicates import build_context, eval_pred
from .verify.verifier import verify


def m1_gate(out_root: Path, n: int = 20, frames_per_scene: int = 3) -> dict:
    out_root = Path(out_root)
    rows, c_ok, h_ok, l_ok = [], 0, 0, 0
    for seed in range(1, n + 1):
        d = out_root / f"synth{seed}"
        scene = make_synthetic_scene(d, seed=seed)
        save_scene(scene, d / "scene.json")
        ctx = build_context(scene)
        cs = extract_constraints(scene)
        c_pass = all(eval_pred(c.pred, ctx) for c in cs)
        html = compose(scene, d, d / "composition.html")
        frames = sorted({0, scene.frames // 2, scene.frames - 1} | set(range(0, scene.frames, max(1, scene.frames // frames_per_scene))))
        r1 = render(html, scene, d / "r1", frames=frames)
        r2 = render(html, scene, d / "r2", frames=frames, probe=False)
        h_pass = r1.hashes == r2.hashes
        rep = verify(scene, d, render_result=r1)
        l_pass = rep.layer_max_err_px <= 2.0
        c_ok += c_pass; h_ok += h_pass; l_ok += l_pass
        rows.append({"seed": seed, "constraints": len(cs), "constraints_ok": c_pass, "hash_ok": h_pass,
                     "layer_max_err_px": rep.layer_max_err_px, "layer_ok": l_pass})
    return {"n": n, "constraints_ok": c_ok, "hash_ok": h_ok, "layer_ok": l_ok,
            "passed": c_ok == n and h_ok == n and l_ok == n, "rows": rows}
```

```python
# keepframe/cli.py
from __future__ import annotations
import argparse, json, sys
from pathlib import Path
from .compose.composer import compose
from .ir.store import load_scene, save_scene
from .ir.synth import make_synthetic_scene
from .render.renderer import render, render_result_from_json
from .verify.verifier import verify


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="keepframe")
    sub = ap.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("synth"); s.add_argument("--out", required=True); s.add_argument("--seed", type=int, default=1)
    s.add_argument("--frames", type=int, default=60); s.add_argument("--no-text", action="store_true")
    c = sub.add_parser("compose"); c.add_argument("--scene", required=True); c.add_argument("--out", required=True)
    r = sub.add_parser("render"); r.add_argument("--scene", required=True); r.add_argument("--html", required=True)
    r.add_argument("--out", required=True); r.add_argument("--frames", default=None); r.add_argument("--mp4", action="store_true")
    v = sub.add_parser("verify"); v.add_argument("--scene", required=True); v.add_argument("--render-json", default=None)
    v.add_argument("--reference", default=None)
    g = sub.add_parser("gate-m1"); g.add_argument("--out", required=True); g.add_argument("--n", type=int, default=20)
    a = ap.parse_args(argv)

    if a.cmd == "synth":
        out = Path(a.out)
        scene = make_synthetic_scene(out, seed=a.seed, frames=a.frames, with_text=not a.no_text)
        save_scene(scene, out / "scene.json"); print(out / "scene.json"); return 0
    if a.cmd == "compose":
        sp = Path(a.scene); print(compose(load_scene(sp), sp.parent, Path(a.out))); return 0
    if a.cmd == "render":
        sp = Path(a.scene); frames = [int(x) for x in a.frames.split(",")] if a.frames else None
        res = render(Path(a.html), load_scene(sp), Path(a.out), frames=frames, mp4=a.mp4)
        print(json.dumps({"frames": len(res.frames), "mp4": str(res.mp4) if res.mp4 else None})); return 0
    if a.cmd == "verify":
        sp = Path(a.scene); scene = load_scene(sp)
        rr = render_result_from_json(Path(a.render_json)) if a.render_json else None
        ref = load_scene(Path(a.reference)) if a.reference else None
        ref_dir = Path(a.reference).parent if a.reference else None
        rep = verify(scene, sp.parent, render_result=rr, reference=ref, reference_dir=ref_dir)
        print(rep.model_dump_json(indent=2)); return 0 if rep.passed else 1
    if a.cmd == "gate-m1":
        from .gates import m1_gate
        res = m1_gate(Path(a.out), n=a.n)
        for row in res["rows"]:
            print(row)
        print({k: v for k, v in res.items() if k != "rows"}); return 0 if res["passed"] else 1
    return 2


if __name__ == "__main__":
    sys.exit(main())
```

```python
# scripts/m1_gate.py
import sys
from keepframe.cli import main
sys.exit(main(["gate-m1", "--out", "out/m1", "--n", "20"]))
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/test_cli.py -v` then `python scripts/m1_gate.py`
Expected: tests PASS; the gate prints 20 rows and `'passed': True`. This is the M1 completion criterion from spec §11.

- [ ] **Step 5: Commit**

```bash
git add keepframe/cli.py keepframe/gates.py keepframe/render/renderer.py scripts/m1_gate.py tests/test_cli.py
git commit -m "feat: CLI (synth/compose/render/verify) and M1 gate over 20 synthetic scenes"
```

---

## M2 — Analyzer (flat 2D motion graphics, range mode) and review corrections

Fixture strategy for M2: synthetic scenes from Task 5 are rendered to video with the numpy compositor (no browser needed), then analyzed; the synthetic scene is the golden IR. Real clips (spec §10) plug into the same gate through `gate-m2-real` (Task 25).

### Task 15: Video I/O and background estimation

**Files:**
- Create: `keepframe/analyze/video.py`, `keepframe/analyze/background.py`
- Test: `tests/test_video_background.py`

**Interfaces:**
- Produces:
  - `read_frames(path: Path, start: int = 0, end: int | None = None) -> tuple[np.ndarray, float]` — frames `(N,H,W,3)` uint8 RGB for frame indices `start..end` inclusive, and fps. `path` may be a video file or a directory of `*.png`.
  - `write_video(frames: np.ndarray, fps: float, out: Path) -> Path` — ffmpeg libx264, `-pix_fmt yuv444p -crf 8` (near-lossless; the analyzer must tolerate this compression).
  - `render_scene_video(scene: Scene, scene_dir: Path, out: Path) -> Path` — compositor over all frames → `write_video`.
  - `rgb_to_lab(img_rgb_uint8: np.ndarray) -> np.ndarray` float32 LAB (OpenCV scaling L∈[0,255]).
  - `estimate_background(frames: np.ndarray, k: int = 6) -> tuple[tuple[int,int,int], float]` — mode LAB cluster over subsampled pixels; returns RGB and the cluster's pixel fraction as confidence.
  - `foreground_mask(frame: np.ndarray, bg_rgb: tuple[int,int,int], thr: float = 12.0) -> np.ndarray` — bool mask where ΔE76 (OpenCV LAB units) > thr.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_video_background.py
import numpy as np, pytest
from keepframe.ir.synth import make_synthetic_scene
from keepframe.analyze.video import read_frames, write_video, render_scene_video
from keepframe.analyze.background import estimate_background, foreground_mask
from keepframe.analyze.composite import composite_scene

def test_video_roundtrip_and_background(tmp_scene_dir):
    scene = make_synthetic_scene(tmp_scene_dir, seed=21, frames=12, with_text=False)
    vid = render_scene_video(scene, tmp_scene_dir, tmp_scene_dir / "v.mp4")
    frames, fps = read_frames(vid)
    assert frames.shape == (12, 360, 640, 3) and frames.dtype == np.uint8 and fps == pytest.approx(30, abs=0.01)
    ref = (composite_scene(scene, tmp_scene_dir, 5) * 255).round()
    assert np.abs(frames[5].astype(float) - ref).mean() < 2.0          # near-lossless
    bg, conf = estimate_background(frames)
    assert max(abs(bg[0] - 0x10), abs(bg[1] - 0x14), abs(bg[2] - 0x18)) <= 3 and conf > 0.5
    fg = foreground_mask(frames[5], bg)
    e = scene.elements[0]
    from keepframe.ir.tracks import element_bbox
    x0, y0, x1, y1 = [int(v) for v in element_bbox(e, 5)]
    cx, cy = (x0 + x1) // 2, (y0 + y1) // 2
    assert fg[cy, cx] and not fg[2, 2]

def test_read_frames_range_and_png_dir(tmp_scene_dir):
    scene = make_synthetic_scene(tmp_scene_dir, seed=21, frames=12, with_text=False)
    vid = render_scene_video(scene, tmp_scene_dir, tmp_scene_dir / "v.mp4")
    sub, _ = read_frames(vid, start=3, end=6)
    assert sub.shape[0] == 4
    import cv2
    d = tmp_scene_dir / "pngs"; d.mkdir()
    for i in range(3):
        cv2.imwrite(str(d / f"f_{i:05d}.png"), cv2.cvtColor(sub[i], cv2.COLOR_RGB2BGR))
    fr, fps = read_frames(d)
    assert fr.shape[0] == 3 and fps == 30.0
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_video_background.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'keepframe.analyze.video'`

- [ ] **Step 3: Implement**

```python
# keepframe/analyze/video.py
from __future__ import annotations
import shutil, subprocess, tempfile
from pathlib import Path
import cv2, numpy as np
from ..ir.schema import Scene
from .composite import composite_scene


def read_frames(path: Path, start: int = 0, end: int | None = None) -> tuple[np.ndarray, float]:
    path = Path(path)
    if path.is_dir():
        files = sorted(path.glob("*.png"))[start: (end + 1) if end is not None else None]
        frames = [cv2.cvtColor(cv2.imread(str(f), cv2.IMREAD_COLOR), cv2.COLOR_BGR2RGB) for f in files]
        return np.stack(frames), 30.0
    cap = cv2.VideoCapture(str(path))
    if not cap.isOpened():
        raise FileNotFoundError(path)
    fps = cap.get(cv2.CAP_PROP_FPS) or 30.0
    cap.set(cv2.CAP_PROP_POS_FRAMES, start)
    frames, i = [], start
    while True:
        ok, bgr = cap.read()
        if not ok or (end is not None and i > end):
            break
        frames.append(cv2.cvtColor(bgr, cv2.COLOR_BGR2RGB)); i += 1
    cap.release()
    if not frames:
        raise ValueError(f"no frames read from {path} in [{start},{end}]")
    return np.stack(frames), float(fps)


def write_video(frames: np.ndarray, fps: float, out: Path) -> Path:
    if shutil.which("ffmpeg") is None:
        raise RuntimeError("ffmpeg not found on PATH")
    out = Path(out); out.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory() as td:
        for i, f in enumerate(frames):
            cv2.imwrite(f"{td}/f_{i:05d}.png", cv2.cvtColor(np.ascontiguousarray(f), cv2.COLOR_RGB2BGR))
        subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-framerate", str(fps), "-i", f"{td}/f_%05d.png",
                        "-c:v", "libx264", "-pix_fmt", "yuv444p", "-crf", "8", str(out)], check=True)
    return out


def render_scene_video(scene: Scene, scene_dir: Path, out: Path) -> Path:
    cache: dict = {}
    frames = np.stack([(composite_scene(scene, scene_dir, f, cache) * 255).round().clip(0, 255).astype(np.uint8)
                       for f in range(scene.frames)])
    return write_video(frames, scene.fps, out)
```

```python
# keepframe/analyze/background.py
from __future__ import annotations
import cv2, numpy as np


def rgb_to_lab(img_rgb_uint8: np.ndarray) -> np.ndarray:
    return cv2.cvtColor(np.ascontiguousarray(img_rgb_uint8), cv2.COLOR_RGB2LAB).astype(np.float32)


def estimate_background(frames: np.ndarray, k: int = 6) -> tuple[tuple[int, int, int], float]:
    sub = frames[::max(1, len(frames) // 12), ::4, ::4].reshape(-1, 3)
    lab = rgb_to_lab(sub.reshape(-1, 1, 3)).reshape(-1, 3)
    crit = (cv2.TERM_CRITERIA_EPS + cv2.TERM_CRITERIA_MAX_ITER, 30, 0.5)
    _, labels, centers = cv2.kmeans(lab, k, None, crit, 3, cv2.KMEANS_PP_CENTERS)
    counts = np.bincount(labels.ravel(), minlength=k)
    mode = int(counts.argmax())
    rgb = sub[labels.ravel() == mode].mean(0)
    return tuple(int(round(v)) for v in rgb), float(counts[mode] / counts.sum())  # type: ignore[return-value]


def foreground_mask(frame: np.ndarray, bg_rgb: tuple[int, int, int], thr: float = 12.0) -> np.ndarray:
    lab = rgb_to_lab(frame)
    bg = rgb_to_lab(np.array(bg_rgb, np.uint8).reshape(1, 1, 3))[0, 0]
    return np.linalg.norm(lab - bg, axis=2) > thr
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/test_video_background.py -v`
Expected: 2 PASS

- [ ] **Step 5: Commit**

```bash
git add keepframe/analyze/video.py keepframe/analyze/background.py tests/test_video_background.py
git commit -m "feat(analyze): video io, synthetic video rendering, LAB background estimation"
```

---

### Task 16: Region extraction (colour clusters + connected components)

**Files:**
- Create: `keepframe/analyze/regions.py`
- Test: `tests/test_regions.py`

**Interfaces:**
- Produces:
  - `@dataclass Region: frame: int; label: int; color: tuple[float,float,float]  # RGB 0-255; bbox: tuple[int,int,int,int]  # x0,y0,x1,y1 exclusive; area: int; centroid: tuple[float,float]; mask: np.ndarray  # bool crop of bbox size`
  - `build_palette(frames: np.ndarray, fg_masks: np.ndarray, k: int = 8) -> np.ndarray` — `(k',3)` LAB centres of foreground pixels (k' ≤ k; duplicates merged when closer than ΔE 8).
  - `label_frame(frame: np.ndarray, fg_mask: np.ndarray, palette_lab: np.ndarray) -> np.ndarray` — int32 label map, `-1` for background.
  - `extract_regions(frame_idx: int, frame: np.ndarray, fg_mask: np.ndarray, palette_lab: np.ndarray, min_area: int = 30, exclude_mask: np.ndarray | None = None, overrides: list[tuple[np.ndarray, int]] | None = None) -> list[Region]` — connected components (8-connectivity) per palette label; `exclude_mask` pixels (text) are removed from the foreground first; `overrides` are `(mask, label)` pairs that force one region each (used by corrections, Task 24).

- [ ] **Step 1: Write the failing test**

```python
# tests/test_regions.py
import numpy as np
from keepframe.analyze.background import foreground_mask
from keepframe.analyze.regions import build_palette, extract_regions

def frame_with_rects():
    f = np.full((100, 200, 3), (0x10, 0x14, 0x18), np.uint8)
    f[10:40, 20:60] = (239, 71, 111)     # red rect
    f[50:90, 120:180] = (6, 214, 160)    # green rect
    f[60:70, 20:30] = (239, 71, 111)     # small red square (same colour, separate component)
    return f

def test_regions_split_by_colour_and_component():
    f = frame_with_rects(); bg = (0x10, 0x14, 0x18)
    fg = foreground_mask(f, bg)
    pal = build_palette(f[None], fg[None])
    regs = extract_regions(0, f, fg, pal, min_area=30)
    assert len(regs) == 3
    big_red = max((r for r in regs if r.color[0] > 200), key=lambda r: r.area)
    assert big_red.bbox == (20, 10, 60, 40) and big_red.area == 1200 and big_red.mask.shape == (30, 40)
    assert abs(big_red.centroid[0] - 39.5) < 1e-6 and abs(big_red.centroid[1] - 24.5) < 1e-6

def test_exclude_mask_and_override():
    f = frame_with_rects(); bg = (0x10, 0x14, 0x18)
    fg = foreground_mask(f, bg); pal = build_palette(f[None], fg[None])
    ex = np.zeros_like(fg); ex[50:90, 120:180] = True
    regs = extract_regions(0, f, fg, pal, exclude_mask=ex)
    assert all(r.color[1] < 200 for r in regs)                      # green removed
    ov = np.zeros_like(fg); ov[10:40, 20:60] = True; ov[60:70, 20:30] = True
    regs = extract_regions(0, f, fg, pal, overrides=[(ov, 7)])
    assert any(r.label == 7 and r.area == 1300 for r in regs)      # forced single region
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_regions.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'keepframe.analyze.regions'`

- [ ] **Step 3: Implement**

```python
# keepframe/analyze/regions.py
from __future__ import annotations
from dataclasses import dataclass
import cv2, numpy as np
from .background import rgb_to_lab

# ponytail: colour-cluster connected components. Upgrade path: Canny + trapped-ball (Motico §4.2) for gradients/outlines.


@dataclass
class Region:
    frame: int
    label: int
    color: tuple[float, float, float]
    bbox: tuple[int, int, int, int]
    area: int
    centroid: tuple[float, float]
    mask: np.ndarray


def build_palette(frames: np.ndarray, fg_masks: np.ndarray, k: int = 8) -> np.ndarray:
    step = max(1, len(frames) // 8)
    px = np.concatenate([frames[i][fg_masks[i]] for i in range(0, len(frames), step)])
    if len(px) == 0:
        return np.zeros((0, 3), np.float32)
    px = px[:: max(1, len(px) // 20000)]
    lab = rgb_to_lab(px.reshape(-1, 1, 3)).reshape(-1, 3)
    k = min(k, len(lab))
    crit = (cv2.TERM_CRITERIA_EPS + cv2.TERM_CRITERIA_MAX_ITER, 30, 0.5)
    _, _, centers = cv2.kmeans(lab, k, None, crit, 3, cv2.KMEANS_PP_CENTERS)
    merged: list[np.ndarray] = []
    for c in centers:
        if all(np.linalg.norm(c - m) > 8 for m in merged):
            merged.append(c)
    return np.array(merged, np.float32)


def label_frame(frame: np.ndarray, fg_mask: np.ndarray, palette_lab: np.ndarray) -> np.ndarray:
    labels = np.full(fg_mask.shape, -1, np.int32)
    if len(palette_lab) == 0 or not fg_mask.any():
        return labels
    lab = rgb_to_lab(frame)[fg_mask]
    d = np.linalg.norm(lab[:, None, :] - palette_lab[None, :, :], axis=2)
    labels[fg_mask] = d.argmin(1)
    return labels


def _region(frame_idx: int, frame: np.ndarray, comp_mask: np.ndarray, label: int) -> Region:
    ys, xs = np.nonzero(comp_mask)
    x0, x1, y0, y1 = int(xs.min()), int(xs.max()) + 1, int(ys.min()), int(ys.max()) + 1
    color = tuple(float(v) for v in frame[comp_mask].mean(0))
    return Region(frame=frame_idx, label=label, color=color, bbox=(x0, y0, x1, y1), area=int(comp_mask.sum()),
                  centroid=(float(xs.mean()), float(ys.mean())), mask=comp_mask[y0:y1, x0:x1].copy())


def extract_regions(frame_idx: int, frame: np.ndarray, fg_mask: np.ndarray, palette_lab: np.ndarray, min_area: int = 30,
                    exclude_mask: np.ndarray | None = None,
                    overrides: list[tuple[np.ndarray, int]] | None = None) -> list[Region]:
    fg = fg_mask.copy()
    if exclude_mask is not None:
        fg &= ~exclude_mask
    out: list[Region] = []
    for mask, label in overrides or []:
        m = mask & fg
        if m.any():
            out.append(_region(frame_idx, frame, m, label)); fg &= ~mask
    labels = label_frame(frame, fg, palette_lab)
    for lab in np.unique(labels[labels >= 0]):
        n, comp, stats, _ = cv2.connectedComponentsWithStats((labels == lab).astype(np.uint8), connectivity=8)
        for c in range(1, n):
            if stats[c, cv2.CC_STAT_AREA] >= min_area:
                out.append(_region(frame_idx, frame, comp == c, int(lab)))
    return out
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/test_regions.py -v`
Expected: 2 PASS

- [ ] **Step 5: Commit**

```bash
git add keepframe/analyze/regions.py tests/test_regions.py
git commit -m "feat(analyze): palette labelling and connected-component region extraction with overrides"
```

---

### Task 17: Region tracking across frames

**Files:**
- Create: `keepframe/analyze/tracking.py`
- Test: `tests/test_tracking.py`

**Interfaces:**
- Produces:
  - `@dataclass ObjectTrack: id: int; regions: dict[int, Region]` with properties `first -> int`, `last -> int`.
  - `match_cost(prev: Region, pred_centroid: tuple[float,float], cand: Region, max_dist: float) -> float` — `dist/max_dist + |log(area ratio)| + ΔRGB/60`.
  - `track_regions(regions_by_frame: list[list[Region]], first_frame: int = 0, max_dist: float = 80.0, cost_thr: float = 1.2, max_gap: int = 2) -> list[ObjectTrack]` — Hungarian assignment per frame (scipy `linear_sum_assignment`), constant-velocity prediction, unmatched regions start new tracks, tracks unmatched for more than `max_gap` frames are closed. `# ponytail: no split/merge; upgrade path: Motico's 8 mapping types.`
  - `assignments(tracks: list[ObjectTrack]) -> dict[int, dict[int, int]]` — `frame -> {region_index_in_frame -> track_id}` is NOT needed; instead `track_regions` keeps `Region` objects, and `ObjectTrack.regions[f]` is the region for frame `f`.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_tracking.py
import numpy as np
from keepframe.ir.schema import Scene, Element, Canonical, Background, Keyframe, Track
from keepframe.ir.synth import make_texture
from keepframe.ir.tracks import eval_props
from keepframe.analyze.composite import composite_scene
from keepframe.analyze.background import foreground_mask
from keepframe.analyze.regions import build_palette, extract_regions
from keepframe.analyze.tracking import track_regions

def three_object_scene(d):
    make_texture(d / "assets/a.png", "rect", 40, 30, (239, 71, 111))
    make_texture(d / "assets/b.png", "ellipse", 36, 36, (6, 214, 160))
    make_texture(d / "assets/c.png", "rect", 24, 24, (255, 209, 102))
    els = [
        Element(id="a", kind="sprite", canonical=Canonical(width=40, height=30, texture="assets/a.png"), visible=(0, 29),
                tracks={"x": Track(keys=[Keyframe(t=0, v=60), Keyframe(t=29, v=260)]), "y": Track(keys=[Keyframe(t=0, v=60)])}),
        Element(id="b", kind="sprite", canonical=Canonical(width=36, height=36, texture="assets/b.png"), visible=(0, 29),
                tracks={"x": Track(keys=[Keyframe(t=0, v=260), Keyframe(t=29, v=60)]), "y": Track(keys=[Keyframe(t=0, v=120)])}),
        Element(id="c", kind="sprite", canonical=Canonical(width=24, height=24, texture="assets/c.png"), visible=(10, 29),
                tracks={"x": Track(keys=[Keyframe(t=10, v=160)]), "y": Track(keys=[Keyframe(t=10, v=30), Keyframe(t=29, v=150)])}),
    ]
    return Scene(id="t3", size=(320, 180), fps=30, frames=30, background=Background(value="#101418"), elements=els)

def test_tracks_are_consistent_and_follow_ground_truth(tmp_scene_dir):
    scene = three_object_scene(tmp_scene_dir)
    frames = np.stack([(composite_scene(scene, tmp_scene_dir, f) * 255).round().astype(np.uint8) for f in range(30)])
    bg = (0x10, 0x14, 0x18)
    fg = np.stack([foreground_mask(f, bg) for f in frames])
    pal = build_palette(frames, fg)
    rbf = [extract_regions(i, frames[i], fg[i], pal) for i in range(30)]
    tracks = track_regions(rbf)
    assert len(tracks) == 3
    by_first = sorted(tracks, key=lambda t: (t.first, t.regions[t.first].centroid[0]))
    assert [t.first for t in by_first] == [0, 0, 10] and all(t.last == 29 for t in tracks)
    gt = {"a": scene.elements[0], "b": scene.elements[1], "c": scene.elements[2]}
    for t, eid in zip(by_first, ["a", "b", "c"]):
        for f, r in t.regions.items():
            p = eval_props(gt[eid], f)
            assert abs(r.centroid[0] - p["x"]) < 1.5 and abs(r.centroid[1] - p["y"]) < 1.5, (eid, f)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_tracking.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'keepframe.analyze.tracking'`

- [ ] **Step 3: Implement**

```python
# keepframe/analyze/tracking.py
from __future__ import annotations
import math
from dataclasses import dataclass, field
import numpy as np
from scipy.optimize import linear_sum_assignment
from .regions import Region

# ponytail: one-to-one matching only (no split/merge). Upgrade path: Motico's eight mapping types with differentiable compositing.


@dataclass
class ObjectTrack:
    id: int
    regions: dict[int, Region] = field(default_factory=dict)

    @property
    def first(self) -> int:
        return min(self.regions)

    @property
    def last(self) -> int:
        return max(self.regions)


def match_cost(prev: Region, pred_centroid: tuple[float, float], cand: Region, max_dist: float) -> float:
    d = math.hypot(cand.centroid[0] - pred_centroid[0], cand.centroid[1] - pred_centroid[1])
    area = abs(math.log(max(cand.area, 1) / max(prev.area, 1)))
    col = float(np.linalg.norm(np.array(cand.color) - np.array(prev.color))) / 60.0
    return d / max_dist + area + col


def _predict(track: ObjectTrack, f: int) -> tuple[float, float]:
    fs = sorted(track.regions)
    r1 = track.regions[fs[-1]]
    if len(fs) < 2:
        return r1.centroid
    r0 = track.regions[fs[-2]]
    dt = fs[-1] - fs[-2]
    vx = (r1.centroid[0] - r0.centroid[0]) / dt
    vy = (r1.centroid[1] - r0.centroid[1]) / dt
    k = f - fs[-1]
    return (r1.centroid[0] + vx * k, r1.centroid[1] + vy * k)


def track_regions(regions_by_frame: list[list[Region]], first_frame: int = 0, max_dist: float = 80.0,
                  cost_thr: float = 1.2, max_gap: int = 2) -> list[ObjectTrack]:
    tracks: list[ObjectTrack] = []
    active: list[ObjectTrack] = []
    next_id = 1
    for i, regions in enumerate(regions_by_frame):
        f = first_frame + i
        active = [t for t in active if f - t.last <= max_gap]
        matched_r: set[int] = set()
        if active and regions:
            C = np.array([[match_cost(t.regions[t.last], _predict(t, f), r, max_dist) for r in regions] for t in active])
            rows, cols = linear_sum_assignment(C)
            for a, b in zip(rows, cols):
                if C[a, b] <= cost_thr:
                    active[a].regions[f] = regions[b]; matched_r.add(int(b))
        for j, r in enumerate(regions):
            if j not in matched_r:
                t = ObjectTrack(id=next_id, regions={f: r}); next_id += 1
                tracks.append(t); active.append(t)
    return tracks
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/test_tracking.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add keepframe/analyze/tracking.py tests/test_tracking.py
git commit -m "feat(analyze): Hungarian region tracking with velocity prediction"
```

---

### Task 18: Sprites — canonical texture, per-frame affine, opacity, z-order

**Files:**
- Create: `keepframe/analyze/sprites.py`
- Test: `tests/test_sprites.py`

**Interfaces:**
- Produces:
  - `RAW_COLS = ("x","y","sx","sy","rot","skx","sky","opacity")` (same order as `PROPS`).
  - `canonical_texture(track: ObjectTrack, frames: np.ndarray) -> tuple[np.ndarray, int]` — RGBA uint8 crop from the frame where the region area is largest (alpha = mask·255), and that frame index.
  - `props_from_moments(region: Region, canon: np.ndarray) -> dict[str, float]` — x,y = centroid; sx = sy = sqrt(area / canon_area); rot from second central moments when the canonical shape is elongated (aspect ≥ 1.25) else 0; skx = sky = 0.
  - `refine_ecc(frame: np.ndarray, region: Region, canon: np.ndarray, anchor: tuple[float,float], init: dict[str,float]) -> dict[str,float]` — refines x,y,sx,sy,rot,skx with `cv2.findTransformECC(MOTION_AFFINE)` on grayscale; returns `init` unchanged when ECC fails or moves the centroid more than 8 px.
  - `estimate_opacity(frame: np.ndarray, region: Region, canon_color: tuple, bg_rgb: tuple) -> float` — projection of (pixel−bg) onto (canon−bg), clipped to [0,1].
  - `z_order(tracks: list[ObjectTrack], frames: np.ndarray, bg_rgb: tuple) -> dict[int, int]` — votes from overlap pixels; falls back to first-appearance order. `# ponytail: one z per object`.
  - `sprite_props(track: ObjectTrack, frames: np.ndarray, bg_rgb: tuple, n_frames: int, first_frame: int, use_ecc: bool = True) -> tuple[np.ndarray, np.ndarray, int]` — returns `(raw (n_frames, 8) with NaN when absent, canonical RGBA, canonical frame)`.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_sprites.py
import numpy as np, pytest
from keepframe.ir.synth import make_synthetic_scene
from keepframe.ir.tracks import eval_props
from keepframe.analyze.composite import composite_scene
from keepframe.analyze.background import foreground_mask
from keepframe.analyze.regions import build_palette, extract_regions
from keepframe.analyze.tracking import track_regions
from keepframe.analyze.sprites import sprite_props, z_order, RAW_COLS

def analyzed_tracks(scene, d):
    frames = np.stack([(composite_scene(scene, d, f) * 255).round().astype(np.uint8) for f in range(scene.frames)])
    bg = (0x10, 0x14, 0x18)
    fg = np.stack([foreground_mask(f, bg) for f in frames])
    pal = build_palette(frames, fg)
    tracks = track_regions([extract_regions(i, frames[i], fg[i], pal) for i in range(scene.frames)])
    return frames, bg, tracks

def test_props_match_golden_when_unoccluded(tmp_scene_dir):
    scene = make_synthetic_scene(tmp_scene_dir, seed=31, with_text=False, overlap=False)
    frames, bg, tracks = analyzed_tracks(scene, tmp_scene_dir)
    assert len(tracks) == len(scene.elements)
    for t in tracks:
        raw, canon, cf = sprite_props(t, frames, bg, scene.frames, 0)
        assert raw.shape == (scene.frames, len(RAW_COLS)) and canon.shape[2] == 4
        # find the golden element whose position at the canonical frame is nearest
        gold = min(scene.elements, key=lambda e: abs(eval_props(e, cf)["x"] - raw[cf, 0]) + abs(eval_props(e, cf)["y"] - raw[cf, 1]))
        errs = []
        for f in range(gold.visible[0], gold.visible[1] + 1):
            p = eval_props(gold, f)
            if p["opacity"] < 0.5 or np.isnan(raw[f, 0]):
                continue
            errs.append(abs(raw[f, 0] - p["x"]) + abs(raw[f, 1] - p["y"]))
            assert abs(raw[f, 2] - p["sx"]) < 0.08, (gold.id, f)
            assert abs(raw[f, 7] - p["opacity"]) < 0.15, (gold.id, f)
        assert np.median(errs) < 2.0, (gold.id, np.median(errs))

def test_z_order_from_overlap(tmp_scene_dir):
    scene = make_synthetic_scene(tmp_scene_dir, seed=32, with_text=False, overlap=True)
    frames, bg, tracks = analyzed_tracks(scene, tmp_scene_dir)
    z = z_order(tracks, frames, bg)
    assert set(z) == {t.id for t in tracks} and len(set(z.values())) == len(tracks)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_sprites.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'keepframe.analyze.sprites'`

- [ ] **Step 3: Implement**

```python
# keepframe/analyze/sprites.py
from __future__ import annotations
import math
from itertools import combinations
import cv2, numpy as np
from ..ir.tracks import affine_matrix, decompose_affine
from .regions import Region
from .tracking import ObjectTrack

RAW_COLS = ("x", "y", "sx", "sy", "rot", "skx", "sky", "opacity")


def canonical_texture(track: ObjectTrack, frames: np.ndarray) -> tuple[np.ndarray, int]:
    cf = max(track.regions, key=lambda f: track.regions[f].area)
    r = track.regions[cf]
    x0, y0, x1, y1 = r.bbox
    crop = frames[cf][y0:y1, x0:x1]
    rgba = np.dstack([crop, (r.mask * 255).astype(np.uint8)])
    return rgba, cf


def _orientation(mask: np.ndarray) -> tuple[float, float]:
    m = cv2.moments(mask.astype(np.uint8), binaryImage=True)
    if m["m00"] == 0:
        return 0.0, 1.0
    mu20, mu02, mu11 = m["mu20"] / m["m00"], m["mu02"] / m["m00"], m["mu11"] / m["m00"]
    theta = 0.5 * math.atan2(2 * mu11, mu20 - mu02)
    l1 = 0.5 * (mu20 + mu02) + 0.5 * math.sqrt(4 * mu11 ** 2 + (mu20 - mu02) ** 2)
    l2 = 0.5 * (mu20 + mu02) - 0.5 * math.sqrt(4 * mu11 ** 2 + (mu20 - mu02) ** 2)
    return math.degrees(theta), math.sqrt(max(l1, 1e-9) / max(l2, 1e-9))


def props_from_moments(region: Region, canon: np.ndarray) -> dict[str, float]:
    canon_mask = canon[..., 3] > 127
    s = math.sqrt(region.area / max(int(canon_mask.sum()), 1))
    th_c, aspect = _orientation(canon_mask)
    th_r, _ = _orientation(region.mask)
    rot = (th_r - th_c) if aspect >= 1.25 else 0.0
    rot = (rot + 90) % 180 - 90
    return {"x": region.centroid[0], "y": region.centroid[1], "sx": s, "sy": s, "rot": rot, "skx": 0.0, "sky": 0.0, "opacity": 1.0}


def _canon_anchor_px(canon: np.ndarray, anchor: tuple[float, float]) -> tuple[float, float]:
    h, w = canon.shape[:2]
    return anchor[0] * w, anchor[1] * h


def refine_ecc(frame: np.ndarray, region: Region, canon: np.ndarray, anchor: tuple[float, float],
               init: dict[str, float], margin: int = 8) -> dict[str, float]:
    """ECC on a crop around the region so other objects do not pull the warp."""
    H, W = frame.shape[:2]
    x0, y0 = max(0, region.bbox[0] - margin), max(0, region.bbox[1] - margin)
    x1, y1 = min(W, region.bbox[2] + margin), min(H, region.bbox[3] + margin)
    th, tw = canon.shape[:2]
    ax, ay = _canon_anchor_px(canon, anchor)
    L = np.array([[1.0, 0.0, -ax], [0.0, 1.0, -ay], [0.0, 0.0, 1.0]])            # texture px -> local (anchor at origin)
    Tc = np.array([[1.0, 0.0, -x0], [0.0, 1.0, -y0], [0.0, 0.0, 1.0]])           # scene -> crop
    M_tex_to_crop = Tc @ affine_matrix(init) @ L
    tpl = cv2.cvtColor(canon[..., :3], cv2.COLOR_RGB2GRAY).astype(np.float32) / 255.0
    tpl *= canon[..., 3].astype(np.float32) / 255.0
    img = cv2.cvtColor(frame[y0:y1, x0:x1], cv2.COLOR_RGB2GRAY).astype(np.float32) / 255.0
    try:
        crit = (cv2.TERM_CRITERIA_EPS | cv2.TERM_CRITERIA_COUNT, 60, 1e-5)
        init_crop_to_tex = np.linalg.inv(M_tex_to_crop)[:2].astype(np.float32)
        # template = crop, input = canonical: ECC finds warp W with crop(x) ~ canonical(W x), i.e. crop -> texture
        _, w_crop_to_tex = cv2.findTransformECC(img, tpl, init_crop_to_tex, cv2.MOTION_AFFINE, crit, None, 5)
        M2 = np.linalg.inv(Tc) @ np.linalg.inv(np.vstack([w_crop_to_tex, [0, 0, 1]]))   # texture -> scene
    except cv2.error:
        return init
    props = decompose_affine(M2 @ np.linalg.inv(L))
    if math.hypot(props["x"] - init["x"], props["y"] - init["y"]) > 8 or not (0.2 < props["sx"] < 5) or not (0.2 < props["sy"] < 5):
        return init
    props["opacity"] = init["opacity"]
    return props


def estimate_opacity(frame: np.ndarray, region: Region, canon_color: tuple, bg_rgb: tuple) -> float:
    x0, y0, x1, y1 = region.bbox
    px = frame[y0:y1, x0:x1][region.mask].astype(np.float32)
    c = np.array(canon_color, np.float32) - np.array(bg_rgb, np.float32)
    n = float(np.dot(c, c))
    if n < 1e-6 or len(px) == 0:
        return 1.0
    a = ((px - np.array(bg_rgb, np.float32)) @ c) / n
    return float(np.clip(np.median(a), 0.0, 1.0))


def z_order(tracks: list[ObjectTrack], frames: np.ndarray, bg_rgb: tuple) -> dict[int, int]:
    # ponytail: one z per object from overlap votes; per-frame z when votes flip is the upgrade path.
    above: dict[tuple[int, int], int] = {}
    for a, b in combinations(tracks, 2):
        for f in set(a.regions) & set(b.regions):
            ra, rb = a.regions[f], b.regions[f]
            x0, y0 = max(ra.bbox[0], rb.bbox[0]), max(ra.bbox[1], rb.bbox[1])
            x1, y1 = min(ra.bbox[2], rb.bbox[2]), min(ra.bbox[3], rb.bbox[3])
            if x1 <= x0 or y1 <= y0:
                continue
            ma = ra.mask[y0 - ra.bbox[1]:y1 - ra.bbox[1], x0 - ra.bbox[0]:x1 - ra.bbox[0]]
            mb = rb.mask[y0 - rb.bbox[1]:y1 - rb.bbox[1], x0 - rb.bbox[0]:x1 - rb.bbox[0]]
            # the object whose mask pixels touch the other's bbox interior wins (visible on top)
            va, vb = int(ma.sum()), int(mb.sum())
            if va != vb:
                key = (a.id, b.id) if va > vb else (b.id, a.id)
                above[key] = above.get(key, 0) + 1
    ids = sorted((t.id for t in tracks), key=lambda i: next(t.first for t in tracks if t.id == i))
    score = {i: 0 for i in ids}
    for (top, bottom), v in above.items():
        score[top] += v; score[bottom] -= v
    order = sorted(ids, key=lambda i: (score[i], ids.index(i)))
    return {i: k + 1 for k, i in enumerate(order)}


def sprite_props(track: ObjectTrack, frames: np.ndarray, bg_rgb: tuple, n_frames: int, first_frame: int,
                 use_ecc: bool = True) -> tuple[np.ndarray, np.ndarray, int]:
    canon, cf = canonical_texture(track, frames)
    canon_color = tuple(float(v) for v in canon[canon[..., 3] > 127][..., :3].mean(0))
    raw = np.full((n_frames, len(RAW_COLS)), np.nan)
    for f, r in track.regions.items():
        p = props_from_moments(r, canon)
        if use_ecc:
            p = refine_ecc(frames[f], r, canon, (0.5, 0.5), p)
        p["opacity"] = estimate_opacity(frames[f], r, canon_color, bg_rgb)
        raw[f - first_frame] = [p[c] for c in RAW_COLS]
    return raw, canon, cf
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/test_sprites.py -v`
Expected: 2 PASS. If `refine_ecc` makes results worse on some frames, its guard (8 px / scale range) returns the moment estimate; do not loosen the test.

- [ ] **Step 5: Commit**

```bash
git add keepframe/analyze/sprites.py tests/test_sprites.py
git commit -m "feat(analyze): canonical textures, moment+ECC affine per frame, opacity and z-order"
```

---

### Task 19: Keyframe reduction and easing fit

**Files:**
- Create: `keepframe/analyze/keyframes.py`
- Test: `tests/test_keyframes.py`

**Interfaces:**
- Produces:
  - `ERR = {"x": 2.0, "y": 2.0, "sx": 0.01, "sy": 0.01, "rot": 1.0, "skx": 1.0, "sky": 1.0, "opacity": 0.02}`
  - `fit_ease(values: np.ndarray) -> tuple[Ease | None, float]` — values over one segment (first and last are the endpoints); returns the preset ease (or `None` for linear) with the smallest max abs error, and that error in value units.
  - `reduce_curve(values: np.ndarray, t0: int, max_err: float) -> tuple[list[Keyframe], float]` — recursive split at the point of maximum error until every segment fits within `max_err`; returns keys (with `ease`) and the achieved max error.
  - `tracks_from_raw(raw: np.ndarray, first: int) -> tuple[dict[str, Track], FitError]` — `raw` is `(n_visible, 8)` for frames `first..first+n-1` (no NaN rows); properties that never deviate from `DEFAULTS` by more than `ERR` get no track.
  - `fill_gaps(raw_full: np.ndarray) -> np.ndarray` — linear interpolation of interior NaN rows (occlusion gaps ≤ 2 frames), leaving leading/trailing NaN.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_keyframes.py
import numpy as np, pytest
from keepframe.ir.schema import Keyframe, Track, PROPS, DEFAULTS
from keepframe.ir.tracks import eval_track, PRESET_EASES
from keepframe.analyze.keyframes import reduce_curve, tracks_from_raw, fill_gaps, ERR

def dense(track, n):
    return np.array([eval_track(track, f) for f in range(n)])

def test_reduce_recovers_two_key_eased_segment():
    tr = Track(keys=[Keyframe(t=0, v=0.0, ease=PRESET_EASES["out_cubic"]), Keyframe(t=30, v=200.0)])
    keys, err = reduce_curve(dense(tr, 31), 0, 2.0)
    assert len(keys) <= 3 and err <= 2.0
    rec = Track(keys=keys)
    assert np.abs(dense(rec, 31) - dense(tr, 31)).max() <= 2.0
    assert keys[0].ease is not None

def test_reduce_splits_multi_segment():
    tr = Track(keys=[Keyframe(t=0, v=0.0), Keyframe(t=10, v=50.0, ease=PRESET_EASES["in_quad"]), Keyframe(t=40, v=0.0)])
    keys, err = reduce_curve(dense(tr, 41), 0, 2.0)
    assert 3 <= len(keys) <= 5 and err <= 2.0

def test_tracks_from_raw_skips_constant_defaults_and_reports_fit_error():
    n = 20
    raw = np.tile([[0, 0, 1, 1, 0, 0, 0, 1]], (n, 1)).astype(float)
    raw[:, 0] = np.linspace(100, 160, n)   # x moves; everything else default
    tracks, fe = tracks_from_raw(raw, first=5)
    assert set(tracks) == {"x"} and tracks["x"].keys[0].t == 5 and tracks["x"].keys[-1].t == 24
    assert fe.max_px <= 2.0

def test_fill_gaps_interpolates_interior_only():
    raw = np.full((6, 8), np.nan); raw[1] = 0; raw[2] = np.nan; raw[3] = 2; raw[4] = 3
    out = fill_gaps(raw)
    assert np.isnan(out[0]).all() and np.isnan(out[5]).all() and out[2, 0] == pytest.approx(1.0)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_keyframes.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'keepframe.analyze.keyframes'`

- [ ] **Step 3: Implement**

```python
# keepframe/analyze/keyframes.py
from __future__ import annotations
import numpy as np
from ..ir.schema import DEFAULTS, Ease, FitError, Keyframe, PROPS, Track
from ..ir.tracks import PRESET_EASES, bezier_y

ERR = {"x": 2.0, "y": 2.0, "sx": 0.01, "sy": 0.01, "rot": 1.0, "skx": 1.0, "sky": 1.0, "opacity": 0.02}


def _segment_error(values: np.ndarray, ease: Ease | None) -> float:
    n = len(values) - 1
    if n <= 0:
        return 0.0
    u = np.arange(n + 1) / n
    e = np.array([bezier_y(x, ease) for x in u]) if ease else u
    pred = values[0] + (values[-1] - values[0]) * e
    return float(np.abs(pred - values).max())


def fit_ease(values: np.ndarray) -> tuple[Ease | None, float]:
    best: tuple[Ease | None, float] = (None, _segment_error(values, None))
    for name, ease in PRESET_EASES.items():
        if name == "linear":
            continue
        err = _segment_error(values, ease)
        if err < best[1] - 1e-9:
            best = (ease, err)
    return best


def reduce_curve(values: np.ndarray, t0: int, max_err: float) -> tuple[list[Keyframe], float]:
    n = len(values)
    if n == 1:
        return [Keyframe(t=t0, v=float(values[0]))], 0.0

    def rec(a: int, b: int) -> tuple[list[tuple[int, float, Ease | None]], float]:
        seg = values[a:b + 1]
        ease, err = fit_ease(seg)
        if err <= max_err or b - a < 2:
            return [(a, float(values[a]), ease)], err
        u = np.arange(b - a + 1) / (b - a)
        e = np.array([bezier_y(x, ease) for x in u]) if ease else u
        pred = values[a] + (values[b] - values[a]) * e
        split = a + int(np.argmax(np.abs(pred - seg)[1:-1])) + 1
        left, el = rec(a, split)
        right, er = rec(split, b)
        return left + right, max(el, er)

    parts, err = rec(0, n - 1)
    keys = [Keyframe(t=t0 + a, v=v, ease=ease) for a, v, ease in parts]
    keys.append(Keyframe(t=t0 + n - 1, v=float(values[-1])))
    return keys, err


def fill_gaps(raw_full: np.ndarray) -> np.ndarray:
    out = raw_full.copy()
    valid = ~np.isnan(out[:, 0])
    if valid.sum() < 2:
        return out
    idx = np.flatnonzero(valid)
    for col in range(out.shape[1]):
        interior = np.arange(idx[0], idx[-1] + 1)
        out[interior, col] = np.interp(interior, idx, out[idx, col])
    return out


def tracks_from_raw(raw: np.ndarray, first: int) -> tuple[dict[str, Track], FitError]:
    tracks: dict[str, Track] = {}
    max_px, max_frames = 0.0, 0
    for i, prop in enumerate(PROPS):
        col = raw[:, i]
        if np.abs(col - DEFAULTS[prop]).max() <= ERR[prop]:
            continue
        keys, err = reduce_curve(col, first, ERR[prop])
        tracks[prop] = Track(keys=keys)
        if prop in ("x", "y"):
            max_px = max(max_px, err)
    return tracks, FitError(max_px=max_px, max_frames=max_frames)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/test_keyframes.py -v`
Expected: 4 PASS

- [ ] **Step 5: Commit**

```bash
git add keepframe/analyze/keyframes.py tests/test_keyframes.py
git commit -m "feat(analyze): keyframe reduction with preset cubic-bezier ease fitting"
```

---

### Task 20: Text spotting and tracking

**Files:**
- Create: `keepframe/analyze/text.py`
- Test: `tests/test_text.py`

**Interfaces:**
- Produces:
  - `@dataclass TextBox: frame: int; text: str; bbox: tuple[int,int,int,int]; conf: float`
  - `class Ocr(Protocol): def __call__(self, frame_rgb: np.ndarray) -> list[tuple[str, tuple[int,int,int,int], float]]`
  - `class RapidOcr` — lazy-imports `rapidocr_onnxruntime`; implements `Ocr`.
  - `ocr_frames(frames: np.ndarray, ocr: Ocr, step: int = 1) -> list[list[TextBox]]` — per frame (frames skipped by `step` get an empty list).
  - `@dataclass TextTrack: id: int; boxes: dict[int, TextBox]; text: str` with `first`, `last`.
  - `track_text(boxes_by_frame: list[list[TextBox]], first_frame: int = 0, iou_thr: float = 0.3, max_dist: float = 60.0, max_gap: int = 3) -> list[TextTrack]` — greedy per-frame matching by IoU or centre distance plus text similarity (`difflib.SequenceMatcher.ratio() >= 0.5`); `text` is the majority vote over the track.
  - `apply_copy(tracks: list[TextTrack], copy: list[str]) -> None` — monotone assignment of user-provided copy words to tracks in order of first frame; replaces `text` when similarity ≥ 0.6. `# ponytail: greedy monotone; upgrade: DTW (Haraguchi et al. 2022)`.
  - `text_exclusion_mask(boxes: list[TextBox], shape: tuple[int,int], pad: int = 2) -> np.ndarray` — bool mask.
  - `stroke_mask(frame: np.ndarray, bbox, bg_rgb, thr: float = 12.0) -> np.ndarray` — bool crop mask of text pixels. `# ponytail: colour threshold; upgrade: Hi-SAM`.
  - `text_props(track: TextTrack, frames: np.ndarray, bg_rgb: tuple, n_frames: int, first_frame: int) -> tuple[np.ndarray, np.ndarray, int, FontGuess, str]` — `(raw (n_frames,8), canonical RGBA, canonical frame, font guess, hex colour)`; x,y = box centre; sx,sy = box size / canonical box size; opacity from stroke pixels (Task 18 `estimate_opacity` logic re-implemented locally on stroke pixels).

- [ ] **Step 1: Write the failing test**

```python
# tests/test_text.py
import numpy as np, pytest
from keepframe.ir.synth import make_synthetic_scene
from keepframe.ir.tracks import element_bbox, eval_props
from keepframe.analyze.composite import composite_scene
from keepframe.analyze.text import TextBox, ocr_frames, track_text, apply_copy, text_exclusion_mask, text_props

class FakeOcr:
    """Returns the golden text box (jittered) so tracking can be tested without a real OCR model."""
    def __init__(self, scene, noise=0.0):
        self.scene, self.noise, self.f = scene, noise, 0
    def __call__(self, frame):
        out = []
        for e in self.scene.elements:
            if e.kind == "text" and e.visible[0] <= self.f <= e.visible[1] and eval_props(e, self.f)["opacity"] > 0.3:
                x0, y0, x1, y1 = [int(round(v)) for v in element_bbox(e, self.f)]
                txt = e.canonical.text if (self.f % 7) else e.canonical.text[:-1] + "x"   # occasional misread
                out.append((txt, (x0, y0, x1, y1), 0.9))
        self.f += 1
        return out

def test_track_text_and_copy(tmp_scene_dir):
    scene = make_synthetic_scene(tmp_scene_dir, seed=41)
    frames = np.stack([(composite_scene(scene, tmp_scene_dir, f) * 255).round().astype(np.uint8) for f in range(scene.frames)])
    gold = [e for e in scene.elements if e.kind == "text"][0]
    boxes = ocr_frames(frames, FakeOcr(scene))
    tracks = track_text(boxes)
    assert len(tracks) == 1 and tracks[0].text == gold.canonical.text          # majority vote fixes misreads
    apply_copy(tracks, ["Totally different", gold.canonical.text])
    assert tracks[0].text == gold.canonical.text
    mask = text_exclusion_mask(boxes[gold.visible[0]], frames.shape[1:3])
    x0, y0, x1, y1 = [int(v) for v in element_bbox(gold, gold.visible[0])]
    assert mask[(y0 + y1) // 2, (x0 + x1) // 2]
    raw, canon, cf, font, color = text_props(tracks[0], frames, (0x10, 0x14, 0x18), scene.frames, 0)
    p = eval_props(gold, cf)
    assert abs(raw[cf, 0] - p["x"]) < 2 and abs(raw[cf, 1] - p["y"]) < 2 and canon.shape[2] == 4
    assert 25 <= font.size_px <= 60 and color.startswith("#")

@pytest.mark.ocr
def test_rapidocr_reads_synthetic_text(tmp_scene_dir):
    from keepframe.analyze.text import RapidOcr
    import difflib
    scene = make_synthetic_scene(tmp_scene_dir, seed=41)
    gold = [e for e in scene.elements if e.kind == "text"][0]
    f = gold.visible[1]
    frame = (composite_scene(scene, tmp_scene_dir, f) * 255).round().astype(np.uint8)
    res = RapidOcr()(frame)
    assert res and max(difflib.SequenceMatcher(None, t.lower(), gold.canonical.text.lower()).ratio() for t, _, _ in res) >= 0.6
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_text.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'keepframe.analyze.text'`

- [ ] **Step 3: Implement**

```python
# keepframe/analyze/text.py
from __future__ import annotations
import difflib, math
from collections import Counter
from dataclasses import dataclass, field
from typing import Protocol
import numpy as np
from ..ir.schema import FontGuess
from .background import foreground_mask

# ponytail: greedy tracking + colour-threshold stroke masks. Upgrade path: frozen image spotter + light tracker (GoMatching++),
# DTW copy alignment (Haraguchi et al. 2022), Hi-SAM stroke masks.


@dataclass
class TextBox:
    frame: int
    text: str
    bbox: tuple[int, int, int, int]
    conf: float


class Ocr(Protocol):
    def __call__(self, frame_rgb: np.ndarray) -> list[tuple[str, tuple[int, int, int, int], float]]: ...


class RapidOcr:
    def __init__(self):
        from rapidocr_onnxruntime import RapidOCR  # lazy import
        self._ocr = RapidOCR()

    def __call__(self, frame_rgb: np.ndarray):
        result, _ = self._ocr(frame_rgb[..., ::-1])  # expects BGR
        out = []
        for quad, text, conf in result or []:
            xs = [p[0] for p in quad]; ys = [p[1] for p in quad]
            out.append((text, (int(min(xs)), int(min(ys)), int(max(xs)), int(max(ys))), float(conf)))
        return out


def ocr_frames(frames: np.ndarray, ocr: Ocr, step: int = 1) -> list[list[TextBox]]:
    out: list[list[TextBox]] = []
    for i, f in enumerate(frames):
        out.append([TextBox(i, t, b, c) for t, b, c in ocr(f)] if i % step == 0 else [])
    return out


@dataclass
class TextTrack:
    id: int
    boxes: dict[int, TextBox] = field(default_factory=dict)
    text: str = ""

    @property
    def first(self) -> int:
        return min(self.boxes)

    @property
    def last(self) -> int:
        return max(self.boxes)


def _iou(a, b) -> float:
    ix = max(0, min(a[2], b[2]) - max(a[0], b[0])); iy = max(0, min(a[3], b[3]) - max(a[1], b[1]))
    inter = ix * iy
    ua = (a[2] - a[0]) * (a[3] - a[1]) + (b[2] - b[0]) * (b[3] - b[1]) - inter
    return inter / ua if ua > 0 else 0.0


def _centre(b) -> tuple[float, float]:
    return ((b[0] + b[2]) / 2, (b[1] + b[3]) / 2)


def _sim(a: str, b: str) -> float:
    return difflib.SequenceMatcher(None, a.lower(), b.lower()).ratio()


def track_text(boxes_by_frame: list[list[TextBox]], first_frame: int = 0, iou_thr: float = 0.3,
               max_dist: float = 60.0, max_gap: int = 3) -> list[TextTrack]:
    tracks: list[TextTrack] = []
    next_id = 1
    for i, boxes in enumerate(boxes_by_frame):
        f = first_frame + i
        active = [t for t in tracks if f - t.last <= max_gap]
        used: set[int] = set()
        for box in boxes:
            best, best_s = None, 0.0
            for t in active:
                if t.id in used:
                    continue
                last = t.boxes[t.last]
                geo = max(_iou(last.bbox, box.bbox), 1 - math.dist(_centre(last.bbox), _centre(box.bbox)) / max_dist)
                if geo < iou_thr or _sim(last.text, box.text) < 0.5:
                    continue
                if geo > best_s:
                    best, best_s = t, geo
            if best is None:
                best = TextTrack(id=next_id); next_id += 1; tracks.append(best)
            best.boxes[f] = box; used.add(best.id)
    for t in tracks:
        t.text = Counter(b.text for b in t.boxes.values()).most_common(1)[0][0]
    return tracks


def apply_copy(tracks: list[TextTrack], copy: list[str]) -> None:
    j = 0
    for t in sorted(tracks, key=lambda t: t.first):
        best_k, best_s = None, 0.0
        for k in range(j, len(copy)):
            s = _sim(t.text, copy[k])
            if s > best_s:
                best_k, best_s = k, s
        if best_k is not None and best_s >= 0.6:
            t.text = copy[best_k]; j = best_k + 1


def text_exclusion_mask(boxes: list[TextBox], shape: tuple[int, int], pad: int = 2) -> np.ndarray:
    m = np.zeros(shape, bool)
    for b in boxes:
        x0, y0, x1, y1 = b.bbox
        m[max(0, y0 - pad):y1 + pad, max(0, x0 - pad):x1 + pad] = True
    return m


def stroke_mask(frame: np.ndarray, bbox, bg_rgb: tuple, thr: float = 12.0) -> np.ndarray:
    x0, y0, x1, y1 = bbox
    return foreground_mask(frame[y0:y1, x0:x1], bg_rgb, thr)


def text_props(track: TextTrack, frames: np.ndarray, bg_rgb: tuple, n_frames: int, first_frame: int):
    cf = max(track.boxes, key=lambda f: (track.boxes[f].bbox[2] - track.boxes[f].bbox[0]) * (track.boxes[f].bbox[3] - track.boxes[f].bbox[1]))
    cb = track.boxes[cf].bbox
    crop = frames[cf][cb[1]:cb[3], cb[0]:cb[2]]
    sm = stroke_mask(frames[cf], cb, bg_rgb)
    canon = np.dstack([crop, (sm * 255).astype(np.uint8)])
    cw, ch = cb[2] - cb[0], cb[3] - cb[1]
    stroke_px = crop[sm].astype(np.float32) if sm.any() else crop.reshape(-1, 3).astype(np.float32)
    col = np.median(stroke_px, axis=0)
    color = "#%02x%02x%02x" % tuple(int(v) for v in col)
    c = col - np.array(bg_rgb, np.float32); n = float(np.dot(c, c))
    raw = np.full((n_frames, 8), np.nan)
    for f, b in track.boxes.items():
        x0, y0, x1, y1 = b.bbox
        m = stroke_mask(frames[f], b.bbox, bg_rgb)
        px = frames[f][y0:y1, x0:x1][m].astype(np.float32) if m.any() else np.zeros((0, 3), np.float32)
        opacity = float(np.clip(np.median(((px - np.array(bg_rgb, np.float32)) @ c) / n), 0, 1)) if (n > 1e-6 and len(px)) else 1.0
        raw[f - first_frame] = [(x0 + x1) / 2, (y0 + y1) / 2, (x1 - x0) / cw, (y1 - y0) / ch, 0.0, 0.0, 0.0, opacity]
    font = FontGuess(family_guess="sans-serif", weight=700 if sm.mean() > 0.35 else 400, size_px=float(ch * 0.8))
    return raw, canon, cf, font, color
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/test_text.py -v` and `pytest tests/test_text.py -v -m ocr`
Expected: PASS (the `ocr` test needs the `ocr` extra installed)

- [ ] **Step 5: Commit**

```bash
git add keepframe/analyze/text.py tests/test_text.py
git commit -m "feat(analyze): OCR adapter, text tracking, copy alignment and text sprite props"
```

---

### Task 21: Torch affine refinement (optional GPU)

**Files:**
- Create: `keepframe/analyze/refine.py`
- Test: `tests/test_refine.py`

**Interfaces:**
- Produces:
  - `torch_available() -> bool`
  - `refine_affine(frames: np.ndarray, bg_rgb: tuple, raws: dict[str, np.ndarray], textures: dict[str, np.ndarray], anchors: dict[str, tuple[float,float]], z: dict[str, int], iters: int = 200, lr: float = 0.02, scale: float = 0.5, device: str | None = None) -> dict[str, np.ndarray]` — jointly optimises x,y,sx,sy,rot (skx fixed) and opacity per element per frame to minimise L1 between the composited scene and the video frames (downscaled by `scale`); NaN rows are left untouched; returns refined raws. Raises `RuntimeError` when torch is missing. `# ponytail: full-frame loss on downscaled frames; upgrade path: per-element crops, texture prior (Suzuki et al. 2024).`

- [ ] **Step 1: Write the failing test**

```python
# tests/test_refine.py
import numpy as np, pytest
from keepframe.ir.synth import make_synthetic_scene
from keepframe.ir.tracks import eval_props
from keepframe.analyze.composite import composite_scene, load_texture
from keepframe.analyze.refine import refine_affine, torch_available

pytestmark = pytest.mark.gpu

@pytest.mark.skipif(not torch_available(), reason="torch not installed")
def test_refinement_reduces_position_error(tmp_scene_dir):
    scene = make_synthetic_scene(tmp_scene_dir, seed=51, n_elements=2, frames=8, size=(160, 90), with_text=False, overlap=False)
    frames = np.stack([(composite_scene(scene, tmp_scene_dir, f) * 255).round().astype(np.uint8) for f in range(8)])
    raws, tex, anchors, z = {}, {}, {}, {}
    for e in scene.elements:
        r = np.array([[eval_props(e, f)[k] for k in ("x", "y", "sx", "sy", "rot", "skx", "sky", "opacity")] for f in range(8)])
        r[:, 0] += 3.0; r[:, 1] -= 2.0                      # perturb
        raws[e.id] = r; tex[e.id] = (load_texture(tmp_scene_dir / e.canonical.texture) * 255).astype(np.uint8)
        anchors[e.id] = e.canonical.anchor; z[e.id] = int(e.z.keys[0].v)
    out = refine_affine(frames, (0x10, 0x14, 0x18), raws, tex, anchors, z, iters=80, scale=1.0, device="cpu")
    for e in scene.elements:
        gt = np.array([[eval_props(e, f)["x"], eval_props(e, f)["y"]] for f in range(8)])
        before = np.abs(raws[e.id][:, :2] - gt).mean(); after = np.abs(out[e.id][:, :2] - gt).mean()
        assert after < before and after < 1.0, (e.id, before, after)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_refine.py -v -m gpu`
Expected: FAIL with `ModuleNotFoundError: No module named 'keepframe.analyze.refine'`

- [ ] **Step 3: Implement**

```python
# keepframe/analyze/refine.py
from __future__ import annotations
import math
import numpy as np
from .composite import hex_to_rgb  # noqa: F401  (kept for parity with compositor colours)

# ponytail: full-frame L1 on downscaled frames. Upgrade path: per-element crops and a texture prior (Suzuki et al., ECCV 2024).


def torch_available() -> bool:
    try:
        import torch  # noqa: F401
        return True
    except ImportError:
        return False


def refine_affine(frames: np.ndarray, bg_rgb: tuple, raws: dict[str, np.ndarray], textures: dict[str, np.ndarray],
                  anchors: dict[str, tuple[float, float]], z: dict[str, int], iters: int = 200, lr: float = 0.02,
                  scale: float = 0.5, device: str | None = None) -> dict[str, np.ndarray]:
    if not torch_available():
        raise RuntimeError("torch is required for refine_affine (pip install 'keepframe[gpu]')")
    import torch, torch.nn.functional as F
    dev = device or ("cuda" if torch.cuda.is_available() else "cpu")
    N, H, W = frames.shape[:3]
    h, w = int(round(H * scale)), int(round(W * scale))
    target = torch.tensor(frames, dtype=torch.float32, device=dev).permute(0, 3, 1, 2) / 255.0
    if scale != 1.0:
        target = F.interpolate(target, size=(h, w), mode="bilinear", align_corners=False)
    bg = torch.tensor(np.array(bg_rgb, np.float32) / 255.0, device=dev).view(1, 3, 1, 1)
    order = sorted(raws, key=lambda k: z[k])
    params, masks, texs = {}, {}, {}
    for k in order:
        r = raws[k]
        valid = ~np.isnan(r[:, 0])
        p = torch.tensor(np.nan_to_num(r[:, [0, 1, 2, 3, 4, 7]]), dtype=torch.float32, device=dev)
        params[k] = p.clone().requires_grad_(True)
        masks[k] = torch.tensor(valid, device=dev)
        texs[k] = torch.tensor(textures[k], dtype=torch.float32, device=dev).permute(2, 0, 1) / 255.0  # 4,th,tw
    opt = torch.optim.Adam(list(params.values()), lr=lr)

    def theta_for(k, p):
        """Build the affine_grid theta (scene-normalised -> texture-normalised) for every frame."""
        th, tw = texs[k].shape[1:]
        ax, ay = anchors[k]
        x, y, sx, sy, rot, _ = p.unbind(1)
        r = rot * math.pi / 180
        cos, sin = torch.cos(r), torch.sin(r)
        # scene pixel = T(x,y) R S (tex_px - anchor_px);  invert: tex_px = S^-1 R^-1 (scene - t) + anchor_px
        a = torch.stack([torch.stack([cos / sx, sin / sx], 1), torch.stack([-sin / sy, cos / sy], 1)], 1)   # N,2,2
        # normalised coordinates: scene [-1,1] over (W,H); texture [-1,1] over (tw,th)
        S_scene = torch.tensor([[W / 2, 0.0], [0.0, H / 2]], device=dev)
        S_tex_inv = torch.tensor([[2 / tw, 0.0], [0.0, 2 / th]], device=dev)
        A = S_tex_inv @ a @ S_scene                                              # N,2,2
        # align_corners=False: scene px centre = (W/2)*g + (W-1)/2 ; texture normalised n = (2*p + 1)/tw - 1
        t_scene = torch.stack([(W - 1) / 2 - x, (H - 1) / 2 - y], 1).unsqueeze(2)   # N,2,1
        anchor_px = torch.tensor([ax * tw, ay * th], device=dev).view(1, 2, 1)
        half = torch.tensor([1.0 / tw - 1.0, 1.0 / th - 1.0], device=dev).view(1, 2, 1)
        b = S_tex_inv @ (a @ t_scene + anchor_px) + half
        return torch.cat([A, b], 2)                                             # N,2,3

    for _ in range(iters):
        opt.zero_grad()
        canvas = bg.expand(N, 3, h, w).clone()
        for k in order:
            p = params[k]
            theta = theta_for(k, p)
            grid = F.affine_grid(theta, (N, 4, h, w), align_corners=False)
            samp = F.grid_sample(texs[k].unsqueeze(0).expand(N, -1, -1, -1), grid, align_corners=False, padding_mode="zeros")
            alpha = samp[:, 3:4] * p[:, 5].clamp(0, 1).view(N, 1, 1, 1) * masks[k].view(N, 1, 1, 1)
            canvas = canvas * (1 - alpha) + samp[:, :3] * alpha
        loss = (canvas - target).abs().mean()
        loss.backward()
        opt.step()
    out = {}
    for k in order:
        r = raws[k].copy()
        p = params[k].detach().cpu().numpy()
        valid = ~np.isnan(r[:, 0])
        r[valid, 0] = p[valid, 0]; r[valid, 1] = p[valid, 1]; r[valid, 2] = p[valid, 2]; r[valid, 3] = p[valid, 3]
        r[valid, 4] = p[valid, 4]; r[valid, 7] = np.clip(p[valid, 5], 0, 1)
        out[k] = r
    return out
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/test_refine.py -v -m gpu`
Expected: PASS on CPU in under a minute. If the loss does not decrease, the sign convention in `theta_for` is wrong: verify with a single element at `rot=0, sx=sy=1` that `theta` maps the scene centre to the texture anchor before touching the optimiser.

- [ ] **Step 5: Commit**

```bash
git add keepframe/analyze/refine.py tests/test_refine.py
git commit -m "feat(analyze): optional torch refinement of per-frame affine and opacity"
```

---

### Task 22: Semantics — roles, groups, captioner protocol

**Files:**
- Create: `keepframe/analyze/semantics.py`
- Test: `tests/test_semantics.py`

**Interfaces:**
- Produces:
  - `class Captioner(Protocol): def caption(self, texture: np.ndarray, text: str | None) -> str`
  - `class NullCaptioner` — returns `""` (default; VLM captions are opt-in and never affect timing).
  - `assign_roles(elements: list[Element]) -> None` — text elements → `"text"`; among the rest the one with the largest `canonical.width*height*(visible span)` → `"primary"`; others `"secondary"`.
  - `group_by_motion(elements: list[Element], raws: dict[str, np.ndarray], corr_thr: float = 0.98, start_tol: int = 1) -> list[Group]` — union-find over pairs whose x/y displacement sequences over common frames correlate ≥ `corr_thr` and whose first frames differ ≤ `start_tol`; groups of size ≥ 2 only, ids `g1..`, reason `"moves together"`.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_semantics.py
import numpy as np
from keepframe.ir.schema import Element, Canonical, Keyframe, Track
from keepframe.analyze.semantics import assign_roles, group_by_motion, NullCaptioner

def el(i, w, h, kind="sprite", vis=(0, 59)):
    return Element(id=f"e{i}", kind=kind, canonical=Canonical(width=w, height=h, text="T" if kind == "text" else None), visible=vis)

def test_roles():
    els = [el(1, 50, 50), el(2, 200, 100), el(3, 80, 20, kind="text")]
    assign_roles(els)
    assert [e.role for e in els] == ["secondary", "primary", "text"]

def test_group_by_motion():
    n = 30
    base = np.zeros((n, 8)); base[:, 0] = np.linspace(0, 100, n); base[:, 1] = np.linspace(0, 50, n); base[:, 2:4] = 1; base[:, 7] = 1
    other = base.copy(); other[:, 0] = np.linspace(100, 0, n)
    raws = {"e1": base, "e2": base + np.array([40, 0, 0, 0, 0, 0, 0, 0]), "e3": other}
    els = [el(1, 10, 10, vis=(0, n - 1)), el(2, 10, 10, vis=(0, n - 1)), el(3, 10, 10, vis=(0, n - 1))]
    g = group_by_motion(els, raws)
    assert len(g) == 1 and sorted(g[0].members) == ["e1", "e2"] and g[0].reason == "moves together"

def test_null_captioner():
    assert NullCaptioner().caption(np.zeros((2, 2, 4), np.uint8), None) == ""
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_semantics.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'keepframe.analyze.semantics'`

- [ ] **Step 3: Implement**

```python
# keepframe/analyze/semantics.py
from __future__ import annotations
from typing import Protocol
import numpy as np
from ..ir.schema import Element, Group

# ponytail: heuristic roles and motion-correlation groups; VLM captions are opt-in through Captioner and never touch timing.


class Captioner(Protocol):
    def caption(self, texture: np.ndarray, text: str | None) -> str: ...


class NullCaptioner:
    def caption(self, texture: np.ndarray, text: str | None) -> str:
        return ""


def assign_roles(elements: list[Element]) -> None:
    non_text = [e for e in elements if e.kind != "text"]
    primary = max(non_text, key=lambda e: e.canonical.width * e.canonical.height * (e.visible[1] - e.visible[0] + 1), default=None)
    for e in elements:
        e.role = "text" if e.kind == "text" else ("primary" if e is primary else "secondary")


def _corr(a: np.ndarray, b: np.ndarray) -> float:
    da, db = np.diff(a, axis=0).ravel(), np.diff(b, axis=0).ravel()
    if np.linalg.norm(da) < 1e-6 or np.linalg.norm(db) < 1e-6:
        return 1.0 if np.linalg.norm(da) < 1e-6 and np.linalg.norm(db) < 1e-6 else 0.0
    return float(np.dot(da, db) / (np.linalg.norm(da) * np.linalg.norm(db)))


def group_by_motion(elements: list[Element], raws: dict[str, np.ndarray], corr_thr: float = 0.98, start_tol: int = 1) -> list[Group]:
    parent = {e.id: e.id for e in elements}

    def find(x):
        while parent[x] != x:
            parent[x] = parent[parent[x]]; x = parent[x]
        return x

    for i, a in enumerate(elements):
        for b in elements[i + 1:]:
            if abs(a.visible[0] - b.visible[0]) > start_tol:
                continue
            lo, hi = max(a.visible[0], b.visible[0]), min(a.visible[1], b.visible[1])
            if hi - lo < 2:
                continue
            ra, rb = raws[a.id][lo:hi + 1, :2], raws[b.id][lo:hi + 1, :2]
            ok = ~(np.isnan(ra[:, 0]) | np.isnan(rb[:, 0]))
            if ok.sum() >= 3 and _corr(ra[ok], rb[ok]) >= corr_thr:
                parent[find(a.id)] = find(b.id)
    buckets: dict[str, list[str]] = {}
    for e in elements:
        buckets.setdefault(find(e.id), []).append(e.id)
    groups = [sorted(m) for m in buckets.values() if len(m) >= 2]
    return [Group(id=f"g{i}", members=m, reason="moves together") for i, m in enumerate(sorted(groups), 1)]
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/test_semantics.py -v`
Expected: 3 PASS

- [ ] **Step 5: Commit**

```bash
git add keepframe/analyze/semantics.py tests/test_semantics.py
git commit -m "feat(analyze): heuristic roles, motion groups, captioner protocol"
```

---

### Task 23: Report and analysis pipeline with stage cache

**Files:**
- Create: `keepframe/analyze/report.py`, `keepframe/analyze/pipeline.py`
- Test: `tests/test_pipeline.py`

**Interfaces:**
- `report.py`:
  - `reconstruction_error(scene: Scene, scene_dir: Path, frames: np.ndarray, first_frame: int, sample: int = 1) -> dict` — `{"mean_l1": float, "per_frame": {frame: l1}}` comparing `composite_scene` against the source frames.
  - `element_confidence(scene: Scene, scene_dir: Path, frames: np.ndarray, first_frame: int) -> dict[str, float]` — per element `1 - min(1, l1_in_bbox / 0.10)` averaged over sampled visible frames (`# ponytail: bbox-local L1 as confidence`).
  - `write_report(scene_dir: Path, data: dict) -> Path` → `scene_dir/report.json`.
- `pipeline.py`:
  - `@dataclass AnalyzeOptions: bg_override: str | None = None; copy: list[str] | None = None; ocr: bool = True; refine: bool = True; refine_iters: int = 200; min_area: int = 30; use_ecc: bool = True`
  - `STAGES = ("frames", "background", "text", "regions", "tracking", "sprites", "keyframes", "semantics", "constraints", "report")`
  - `analyze(video: Path, start: int, end: int, out_root: Path, options: AnalyzeOptions | None = None, ocr: Ocr | None = None) -> Project` — runs all stages, writes `scenes/s1/assets/*.png`, `assets/*_raw.npz` (keys `raw`, `first`), `scenes/s1/stages/{background.json,text.pkl,regions.pkl,tracks.pkl,props.pkl,ids.json,overrides.json}`, and the project.
  - `rerun(root: Path, scene_id: str, from_stage: str, note: str, options: AnalyzeOptions | None = None) -> Version` — reloads cached inputs of `from_stage`, applies `overrides.json` (`{"regions": [{"frame": int, "mask": "stages/ov_<n>.png", "label": int}], "ids": {"<object_key>": "e3"}, "merge": [["e2","e5"]]}`), reruns the remaining stages and appends a version (`auto=False`).
  - Element ids: text tracks and object tracks are ordered by `(first_frame, x)` and named `e1..eN`; the mapping `object_key -> element_id` (object keys `"t<id>"` for text, `"o<id>"` for sprites) is stored in `stages/ids.json` so corrections (Task 24) can address elements.
  - When `options.ocr` is true and `ocr is None`, `RapidOcr()` is used if importable, otherwise text stage is skipped with a message in the report.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_pipeline.py
import json, numpy as np
from keepframe.ir.synth import make_synthetic_scene
from keepframe.ir.store import current_scene, scene_dir
from keepframe.analyze.video import render_scene_video
from keepframe.analyze.pipeline import analyze, rerun, AnalyzeOptions

def test_analyze_synthetic_video_end_to_end(tmp_scene_dir):
    gold = make_synthetic_scene(tmp_scene_dir / "gold", seed=61, with_text=False, overlap=False)
    vid = render_scene_video(gold, tmp_scene_dir / "gold", tmp_scene_dir / "gold.mp4")
    root = tmp_scene_dir / "proj"
    project = analyze(vid, 0, gold.frames - 1, root, AnalyzeOptions(ocr=False, refine=False))
    scene, v = current_scene(root, "s1")
    assert v.id == "v1" and scene.frames == gold.frames and scene.size == gold.size
    assert len(scene.elements) == len(gold.elements)
    assert all(e.raw and (scene_dir(root, "s1") / e.raw).exists() for e in scene.elements)
    assert all(e.canonical.texture and (scene_dir(root, "s1") / e.canonical.texture).exists() for e in scene.elements)
    assert scene.constraints and all(c.keep is False for c in scene.constraints)
    rep = json.loads((scene_dir(root, "s1") / "report.json").read_text())
    assert rep["reconstruction"]["mean_l1"] < 0.03
    assert set(rep["confidence"]) == {e.id for e in scene.elements}
    assert (scene_dir(root, "s1") / "stages" / "ids.json").exists()

def test_rerun_from_keyframes_appends_version(tmp_scene_dir):
    gold = make_synthetic_scene(tmp_scene_dir / "gold", seed=62, frames=24, with_text=False, overlap=False)
    vid = render_scene_video(gold, tmp_scene_dir / "gold", tmp_scene_dir / "gold.mp4")
    root = tmp_scene_dir / "proj"
    analyze(vid, 0, 23, root, AnalyzeOptions(ocr=False, refine=False))
    v2 = rerun(root, "s1", "keyframes", note="rerun test")
    assert v2.id == "v2" and v2.auto is False
    s1, _ = current_scene(root, "s1")
    assert len(s1.elements) == len(gold.elements)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_pipeline.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'keepframe.analyze.pipeline'`

- [ ] **Step 3: Implement**

```python
# keepframe/analyze/report.py
from __future__ import annotations
import json
from pathlib import Path
import numpy as np
from ..ir.schema import Scene
from ..ir.tracks import element_bbox
from .composite import composite_scene


def reconstruction_error(scene: Scene, scene_dir: Path, frames: np.ndarray, first_frame: int, sample: int = 1) -> dict:
    cache: dict = {}
    per = {}
    for f in range(0, scene.frames, sample):
        a = composite_scene(scene, scene_dir, f, cache)
        b = frames[f].astype(np.float32) / 255.0
        per[f] = float(np.abs(a - b).mean())
    return {"mean_l1": float(np.mean(list(per.values()))), "per_frame": per}


def element_confidence(scene: Scene, scene_dir: Path, frames: np.ndarray, first_frame: int) -> dict[str, float]:
    cache: dict = {}
    out = {}
    for el in scene.elements:
        fs = list(range(el.visible[0], el.visible[1] + 1, max(1, (el.visible[1] - el.visible[0] + 1) // 6)))
        l1s = []
        for f in fs:
            a = composite_scene(scene, scene_dir, f, cache)
            b = frames[f].astype(np.float32) / 255.0
            x0, y0, x1, y1 = [int(round(v)) for v in element_bbox(el, f)]
            x0, y0 = max(0, x0), max(0, y0); x1, y1 = min(scene.size[0], x1), min(scene.size[1], y1)
            if x1 > x0 and y1 > y0:
                l1s.append(float(np.abs(a[y0:y1, x0:x1] - b[y0:y1, x0:x1]).mean()))
        out[el.id] = float(1.0 - min(1.0, np.mean(l1s) / 0.10)) if l1s else 0.0   # ponytail: bbox-local L1 as confidence
    return out


def write_report(scene_dir: Path, data: dict) -> Path:
    p = Path(scene_dir) / "report.json"
    p.write_text(json.dumps(data, indent=2))
    return p
```

```python
# keepframe/analyze/pipeline.py
from __future__ import annotations
import json, pickle
from dataclasses import dataclass, asdict
from pathlib import Path
import cv2, numpy as np
from ..ir.schema import Background, Canonical, Element, Keyframe, Project, Scene, Track, Version
from ..ir.store import current_scene, init_project, new_version, scene_dir as _scene_dir
from .background import estimate_background, foreground_mask
from .constraints import extract_constraints
from .keyframes import fill_gaps, tracks_from_raw
from .regions import build_palette, extract_regions
from .report import element_confidence, reconstruction_error, write_report
from .semantics import assign_roles, group_by_motion
from .sprites import RAW_COLS, sprite_props, z_order
from .text import Ocr, apply_copy, ocr_frames, text_exclusion_mask, text_props, track_text
from .tracking import track_regions
from .video import read_frames

STAGES = ("frames", "background", "text", "regions", "tracking", "sprites", "keyframes", "semantics", "constraints", "report")


@dataclass
class AnalyzeOptions:
    bg_override: str | None = None
    copy: list[str] | None = None
    ocr: bool = True
    refine: bool = True
    refine_iters: int = 200
    min_area: int = 30
    use_ecc: bool = True


def _hex(rgb) -> str:
    return "#%02x%02x%02x" % tuple(int(v) for v in rgb)


def _rgb(hexs: str):
    h = hexs.lstrip("#")
    return tuple(int(h[i:i + 2], 16) for i in (0, 2, 4))


def _load_overrides(sd: Path) -> dict:
    p = sd / "stages" / "overrides.json"
    return json.loads(p.read_text()) if p.exists() else {"regions": [], "ids": {}, "merge": []}


def _pk(sd: Path, name: str, obj=None):
    p = sd / "stages" / f"{name}.pkl"
    if obj is None:
        return pickle.loads(p.read_bytes())
    p.parent.mkdir(parents=True, exist_ok=True); p.write_bytes(pickle.dumps(obj)); return obj


def _stage_text(frames, bg, opts, ocr, sd):
    boxes = [[] for _ in frames]; tracks = []; msg = None
    if opts.ocr:
        if ocr is None:
            try:
                from .text import RapidOcr
                ocr = RapidOcr()
            except Exception as e:  # rapidocr missing
                msg = f"text stage skipped: {e}"
        if ocr is not None:
            boxes = ocr_frames(frames, ocr)
            tracks = track_text(boxes)
            if opts.copy:
                apply_copy(tracks, opts.copy)
    _pk(sd, "text", {"boxes": boxes, "tracks": tracks, "message": msg})
    return boxes, tracks, msg


def _stage_regions(frames, bg, boxes, opts, sd):
    fg = np.stack([foreground_mask(f, bg) for f in frames])
    pal = build_palette(frames, fg)
    ov = _load_overrides(sd)
    ov_by_frame: dict[int, list] = {}
    for o in ov.get("regions", []):
        m = cv2.imread(str(sd / o["mask"]), cv2.IMREAD_GRAYSCALE) > 127
        ov_by_frame.setdefault(o["frame"], []).append((m, int(o["label"])))
    rbf = [extract_regions(i, frames[i], fg[i], pal, min_area=opts.min_area, exclude_mask=text_exclusion_mask(boxes[i], fg[i].shape),
                           overrides=ov_by_frame.get(i)) for i in range(len(frames))]
    return _pk(sd, "regions", rbf)


def _stage_tracking(rbf, sd):
    tracks = track_regions(rbf)
    ov = _load_overrides(sd)
    # overrides: forced region labels >= 1000 pin regions to an object id (label - 1000)
    for t in tracks:
        for f, r in list(t.regions.items()):
            if r.label >= 1000:
                target = r.label - 1000
                dst = next((u for u in tracks if u.id == target), None)
                if dst is not None and dst is not t:
                    dst.regions[f] = r; del t.regions[f]
    tracks = [t for t in tracks if t.regions]
    return _pk(sd, "tracks", tracks)


def _stage_sprites(frames, bg, text_tracks, obj_tracks, opts, sd, n_frames):
    props = {}   # object_key -> dict(raw, canon, cf, kind, font, color)
    for t in text_tracks:
        raw, canon, cf, font, color = text_props(t, frames, bg, n_frames, 0)
        props[f"t{t.id}"] = {"raw": raw, "canon": canon, "cf": cf, "kind": "text", "text": t.text, "font": font, "color": color, "first": t.first, "last": t.last}
    z = z_order(obj_tracks, frames, bg)
    for t in obj_tracks:
        raw, canon, cf = sprite_props(t, frames, bg, n_frames, 0, use_ecc=opts.use_ecc)
        props[f"o{t.id}"] = {"raw": raw, "canon": canon, "cf": cf, "kind": "sprite", "z": z[t.id], "first": t.first, "last": t.last}
    ov = _load_overrides(sd)
    for a, b in ov.get("merge", []):   # merge object b into a (element ids resolved via ids.json at correction time)
        if a in props and b in props:
            pa, pb = props[a], props[b]
            m = np.isnan(pa["raw"][:, 0]) & ~np.isnan(pb["raw"][:, 0])
            pa["raw"][m] = pb["raw"][m]; pa["first"] = min(pa["first"], pb["first"]); pa["last"] = max(pa["last"], pb["last"])
            del props[b]
    if opts.refine and any(p["kind"] == "sprite" for p in props.values()):
        try:
            from .refine import refine_affine, torch_available
            if torch_available():
                keys = [k for k, p in props.items() if p["kind"] == "sprite"]
                refined = refine_affine(frames, bg, {k: props[k]["raw"] for k in keys}, {k: props[k]["canon"] for k in keys},
                                        {k: (0.5, 0.5) for k in keys}, {k: props[k]["z"] for k in keys}, iters=opts.refine_iters)
                for k in keys:
                    props[k]["raw"] = refined[k]
        except Exception as e:
            props["_message"] = f"refine skipped: {e}"
    return _pk(sd, "props", props)


def _elements_from_props(props: dict, sd: Path, ids: dict) -> tuple[list[Element], dict[str, np.ndarray]]:
    keys = [k for k in props if not k.startswith("_")]
    keys.sort(key=lambda k: (props[k]["first"], float(np.nanmean(props[k]["raw"][:, 0]))))
    elements, raws = [], {}
    for k in keys:
        eid = ids.get(k) or f"e{len(ids) + 1}"
        ids[k] = eid
        p = props[k]
        raw_full = fill_gaps(p["raw"])
        valid = np.flatnonzero(~np.isnan(raw_full[:, 0]))
        first, last = int(valid[0]), int(valid[-1])
        tracks, fe = tracks_from_raw(raw_full[first:last + 1], first)
        (sd / "assets").mkdir(parents=True, exist_ok=True)
        cv2.imwrite(str(sd / "assets" / f"{eid}.png"), cv2.cvtColor(p["canon"], cv2.COLOR_RGBA2BGRA))
        np.savez_compressed(sd / "assets" / f"{eid}_raw.npz", raw=raw_full, first=first, cols=np.array(RAW_COLS))
        h, w = p["canon"].shape[:2]
        canonical = Canonical(width=w, height=h, texture=f"assets/{eid}.png",
                              text=p.get("text"), font=p.get("font"), color=p.get("color"))
        elements.append(Element(id=eid, kind=p["kind"], canonical=canonical, visible=(first, last), tracks=tracks,
                                z=Track(keys=[Keyframe(t=0, v=p.get("z", 0))]), raw=f"assets/{eid}_raw.npz", fit_error=fe))
        raws[eid] = raw_full
    return elements, raws


def _finish(sd: Path, scene: Scene, frames: np.ndarray, raws: dict, messages: list[str]) -> Scene:
    assign_roles(scene.elements)
    scene.groups = group_by_motion(scene.elements, raws)
    scene.constraints = extract_constraints(scene)
    rec = reconstruction_error(scene, sd, frames, 0)
    conf = element_confidence(scene, sd, frames, 0)
    for e in scene.elements:
        e.confidence = conf[e.id]
    write_report(sd, {"reconstruction": rec, "confidence": conf, "messages": messages,
                      "elements": len(scene.elements), "fit_error_max_px": max([e.fit_error.max_px for e in scene.elements] or [0])})
    return scene


def analyze(video: Path, start: int, end: int, out_root: Path, options: AnalyzeOptions | None = None,
            ocr: Ocr | None = None) -> Project:
    opts = options or AnalyzeOptions()
    out_root = Path(out_root)
    sd = _scene_dir(out_root, "s1")
    (sd / "stages").mkdir(parents=True, exist_ok=True)
    frames, fps = read_frames(video, start, end)
    np.save(sd / "stages" / "frames.npy", frames)
    n, H, W = frames.shape[:3]
    bg, bconf = (_rgb(opts.bg_override), 1.0) if opts.bg_override else estimate_background(frames)
    (sd / "stages" / "background.json").write_text(json.dumps({"rgb": list(bg), "confidence": bconf}))
    boxes, text_tracks, msg = _stage_text(frames, bg, opts, ocr, sd)
    rbf = _stage_regions(frames, bg, boxes, opts, sd)
    obj_tracks = _stage_tracking(rbf, sd)
    props = _stage_sprites(frames, bg, text_tracks, obj_tracks, opts, sd, n)
    ids: dict = {}
    elements, raws = _elements_from_props(props, sd, ids)
    (sd / "stages" / "ids.json").write_text(json.dumps(ids, indent=2))
    scene = Scene(id="s1", size=(W, H), fps=fps, frames=n, background=Background(kind="color", value=_hex(bg), confidence=bconf), elements=elements)
    messages = [m for m in (msg, props.get("_message")) if m]
    scene = _finish(sd, scene, frames, raws, messages)
    (sd / "stages" / "options.json").write_text(json.dumps(asdict(opts)))
    return init_project(out_root, {"file": str(video), "fps": fps, "size": [W, H], "mode": "range", "range": [start, end]}, scene)


def rerun(root: Path, scene_id: str, from_stage: str, note: str, options: AnalyzeOptions | None = None) -> Version:
    if from_stage not in STAGES:
        raise ValueError(from_stage)
    root = Path(root); sd = _scene_dir(root, scene_id)
    opts = options or AnalyzeOptions(**json.loads((sd / "stages" / "options.json").read_text()))
    frames = np.load(sd / "stages" / "frames.npy")
    bgj = json.loads((sd / "stages" / "background.json").read_text()); bg = tuple(bgj["rgb"])
    n = len(frames)
    i = STAGES.index(from_stage)
    text = _pk(sd, "text"); boxes, text_tracks = text["boxes"], text["tracks"]
    if i <= STAGES.index("text"):
        boxes, text_tracks, _ = _stage_text(frames, bg, opts, None, sd)
    rbf = _stage_regions(frames, bg, boxes, opts, sd) if i <= STAGES.index("regions") else _pk(sd, "regions")
    obj_tracks = _stage_tracking(rbf, sd) if i <= STAGES.index("tracking") else _pk(sd, "tracks")
    props = _stage_sprites(frames, bg, text_tracks, obj_tracks, opts, sd, n) if i <= STAGES.index("sprites") else _pk(sd, "props")
    ids = json.loads((sd / "stages" / "ids.json").read_text())
    ids.update(_load_overrides(sd).get("ids", {}))
    elements, raws = _elements_from_props(props, sd, ids)
    (sd / "stages" / "ids.json").write_text(json.dumps(ids, indent=2))
    prev, _ = current_scene(root, scene_id)
    for e in elements:   # keep manual text edits across reruns
        try:
            old = prev.element(e.id)
            if old.provenance == "manual" and old.kind == "text":
                e.canonical.text, e.canonical.font, e.provenance = old.canonical.text, old.canonical.font, "manual"
        except KeyError:
            pass
    scene = Scene(id=scene_id, size=prev.size, fps=prev.fps, frames=n, background=prev.background, elements=elements)
    scene = _finish(sd, scene, frames, raws, [m for m in [props.get("_message")] if m])
    return new_version(root, scene_id, scene, note=note, auto=False)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/test_pipeline.py -v`
Expected: 2 PASS

- [ ] **Step 5: Commit**

```bash
git add keepframe/analyze/report.py keepframe/analyze/pipeline.py tests/test_pipeline.py
git commit -m "feat(analyze): end-to-end analysis pipeline with stage cache, overrides and report"
```

---

### Task 24: Review corrections (four operations)

**Files:**
- Create: `keepframe/review/corrections.py`
- Test: `tests/test_corrections.py`

**Interfaces:**
- Produces (each returns the new `Version`, `auto=False`, and never edits an existing version file):
  - `reassign_id(root: Path, scene_id: str, frames: tuple[int,int], from_id: str, to_id: str, note: str = "reassign id") -> Version` — object-key lookup through `stages/ids.json`; for the frame range, regions of `from_id`'s object are pinned to `to_id`'s object via a region override label `1000 + object_id`; reruns from `regions`. When `frames` covers the whole scene the two elements are merged instead (`overrides.merge`).
  - `set_region_mask(root, scene_id, frame: int, mask_png: Path, object_id: str, note="set region mask") -> Version` — copies the mask to `stages/ov_<n>.png`, appends `{"frame", "mask", "label": 1000 + obj}` to overrides, reruns from `regions`.
  - `add_bbox_prompt(root, scene_id, frame: int, bbox: tuple[int,int,int,int], object_id: str, note="bbox prompt") -> Version` — builds a mask from the foreground inside `bbox` and calls `set_region_mask`.
  - `edit_text(root, scene_id, element_id: str, text: str | None = None, font: FontGuess | None = None, note="edit text") -> Version` — edits the current scene JSON only; sets `provenance="manual"`; no rerun.
  - `_object_of(sd: Path, element_id: str) -> str` (object key) and `_object_num(key) -> int`.
- Known ceiling (`# ponytail:`): object ids are re-derived by tracking on every rerun. They are stable for tracks that start before the corrected frame because tracking is deterministic and overrides only change regions at that frame; tracks that start at or after it may shift. Upgrade path: persist track seeds (first region per object) in `stages/` and re-seed `track_regions` from them.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_corrections.py
import json, numpy as np, cv2
from keepframe.ir.synth import make_synthetic_scene
from keepframe.ir.store import current_scene, scene_dir, load_project
from keepframe.ir.schema import FontGuess
from keepframe.analyze.video import render_scene_video
from keepframe.analyze.pipeline import analyze, AnalyzeOptions
from keepframe.review.corrections import edit_text, reassign_id, add_bbox_prompt

def project(tmp, seed, frames=24):
    gold = make_synthetic_scene(tmp / "gold", seed=seed, frames=frames, with_text=False, overlap=False)
    vid = render_scene_video(gold, tmp / "gold", tmp / "gold.mp4")
    root = tmp / "proj"
    analyze(vid, 0, frames - 1, root, AnalyzeOptions(ocr=False, refine=False))
    return gold, root

def test_edit_text_creates_manual_version(tmp_scene_dir):
    _, root = project(tmp_scene_dir, 71)
    s, _ = current_scene(root, "s1")
    eid = s.elements[0].id
    v = edit_text(root, "s1", eid, text="Hello", font=FontGuess(size_px=48))
    assert v.id == "v2" and v.auto is False
    s2, _ = current_scene(root, "s1")
    assert s2.element(eid).canonical.text == "Hello" and s2.element(eid).provenance == "manual"
    assert load_project(root).versions[0].scene_file.endswith("scene.v1.json")

def test_reassign_whole_range_merges_elements(tmp_scene_dir):
    _, root = project(tmp_scene_dir, 72)
    s, _ = current_scene(root, "s1")
    a, b = s.elements[0].id, s.elements[1].id
    v = reassign_id(root, "s1", (0, s.frames - 1), from_id=b, to_id=a)
    s2, _ = current_scene(root, "s1")
    assert v.id == "v2" and len(s2.elements) == len(s.elements) - 1
    ov = json.loads((scene_dir(root, "s1") / "stages" / "overrides.json").read_text())
    assert ov["merge"]

def test_bbox_prompt_appends_override_and_reruns(tmp_scene_dir):
    _, root = project(tmp_scene_dir, 73)
    s, _ = current_scene(root, "s1")
    from keepframe.ir.tracks import element_bbox
    x0, y0, x1, y1 = [int(v) for v in element_bbox(s.elements[0], 0)]
    v = add_bbox_prompt(root, "s1", 0, (x0 - 2, y0 - 2, x1 + 2, y1 + 2), s.elements[0].id)
    assert v.id == "v2"
    ov = json.loads((scene_dir(root, "s1") / "stages" / "overrides.json").read_text())
    assert ov["regions"] and (scene_dir(root, "s1") / ov["regions"][0]["mask"]).exists()
    s2, _ = current_scene(root, "s1")
    assert len(s2.elements) == len(s.elements)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_corrections.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'keepframe.review.corrections'`

- [ ] **Step 3: Implement**

```python
# keepframe/review/corrections.py
from __future__ import annotations
import json, shutil
from pathlib import Path
import cv2, numpy as np
from ..ir.schema import FontGuess, Version
from ..ir.store import current_scene, new_version, scene_dir
from ..analyze.background import foreground_mask
from ..analyze.pipeline import rerun


def _overrides_path(sd: Path) -> Path:
    return sd / "stages" / "overrides.json"


def _load(sd: Path) -> dict:
    p = _overrides_path(sd)
    return json.loads(p.read_text()) if p.exists() else {"regions": [], "ids": {}, "merge": []}


def _save(sd: Path, ov: dict) -> None:
    _overrides_path(sd).write_text(json.dumps(ov, indent=2))


def _object_of(sd: Path, element_id: str) -> str:
    ids = json.loads((sd / "stages" / "ids.json").read_text())
    for key, eid in ids.items():
        if eid == element_id:
            return key
    raise KeyError(element_id)


def _object_num(key: str) -> int:
    return int(key[1:])


def edit_text(root: Path, scene_id: str, element_id: str, text: str | None = None, font: FontGuess | None = None,
              note: str = "edit text") -> Version:
    scene, _ = current_scene(root, scene_id)
    el = scene.element(element_id)
    if text is not None:
        el.canonical.text = text
    if font is not None:
        el.canonical.font = font
    el.provenance = "manual"
    return new_version(root, scene_id, scene, note=note, auto=False)


def set_region_mask(root: Path, scene_id: str, frame: int, mask_png: Path, object_id: str, note: str = "set region mask") -> Version:
    sd = scene_dir(root, scene_id)
    ov = _load(sd)
    n = len(ov["regions"]) + 1
    dst = sd / "stages" / f"ov_{n}.png"
    shutil.copy(mask_png, dst)
    key = _object_of(sd, object_id)
    if key.startswith("t"):
        raise ValueError("region masks apply to sprite objects, not text")
    ov["regions"].append({"frame": int(frame), "mask": f"stages/ov_{n}.png", "label": 1000 + _object_num(key)})
    _save(sd, ov)
    return rerun(root, scene_id, "regions", note=note)


def add_bbox_prompt(root: Path, scene_id: str, frame: int, bbox: tuple[int, int, int, int], object_id: str,
                    note: str = "bbox prompt") -> Version:
    sd = scene_dir(root, scene_id)
    frames = np.load(sd / "stages" / "frames.npy")
    bg = tuple(json.loads((sd / "stages" / "background.json").read_text())["rgb"])
    m = np.zeros(frames.shape[1:3], np.uint8)
    x0, y0, x1, y1 = [max(0, v) for v in bbox]
    m[y0:y1, x0:x1] = foreground_mask(frames[frame], bg)[y0:y1, x0:x1] * 255
    tmp = sd / "stages" / "_bbox_tmp.png"
    cv2.imwrite(str(tmp), m)
    try:
        return set_region_mask(root, scene_id, frame, tmp, object_id, note=note)
    finally:
        tmp.unlink(missing_ok=True)


def reassign_id(root: Path, scene_id: str, frames: tuple[int, int], from_id: str, to_id: str, note: str = "reassign id") -> Version:
    sd = scene_dir(root, scene_id)
    scene, _ = current_scene(root, scene_id)
    ov = _load(sd)
    src, dst = _object_of(sd, from_id), _object_of(sd, to_id)
    if frames[0] <= 0 and frames[1] >= scene.frames - 1:
        ov["merge"].append([dst, src])
        _save(sd, ov)
        return rerun(root, scene_id, "sprites", note=note)
    tracks = __import__("pickle").loads((sd / "stages" / "tracks.pkl").read_bytes())
    t = next(t for t in tracks if t.id == _object_num(src))
    for f in range(frames[0], frames[1] + 1):
        r = t.regions.get(f)
        if r is None:
            continue
        m = np.zeros((scene.size[1], scene.size[0]), np.uint8)
        x0, y0, x1, y1 = r.bbox
        m[y0:y1, x0:x1] = r.mask * 255
        n = len(ov["regions"]) + 1
        cv2.imwrite(str(sd / "stages" / f"ov_{n}.png"), m)
        ov["regions"].append({"frame": f, "mask": f"stages/ov_{n}.png", "label": 1000 + _object_num(dst)})
    _save(sd, ov)
    return rerun(root, scene_id, "regions", note=note)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/test_corrections.py -v`
Expected: 3 PASS

- [ ] **Step 5: Commit**

```bash
git add keepframe/review/corrections.py tests/test_corrections.py
git commit -m "feat(review): four correction ops (reassign id, region mask, bbox prompt, edit text) with reruns"
```

---

### Task 25: Golden comparison, analyze/correct CLI, M2 gate

**Files:**
- Create: `keepframe/analyze/golden.py`, `scripts/m2_gate.py`
- Modify: `keepframe/gates.py` (add `m2_gate`, `m2_gate_real`), `keepframe/cli.py` (add `analyze`, `correct`, `gate-m2`, `gate-m2-real`)
- Test: `tests/test_golden_gate.py`

**Interfaces:**
- `golden.compare(golden: Scene, golden_dir: Path, analyzed: Scene, analyzed_dir: Path, source_frames: np.ndarray) -> dict` — Hungarian match of elements by mean centroid distance over common visible frames; returns `{"matched": int, "tracking_errors": int (unmatched golden + extra analyzed + matched with mean distance > 5 px), "pos_err_px": float, "scale_err": float, "sprite_rgb_l1": float, "sprite_alpha_l1": float, "temporal": float, "frame_l1": float}`.
- `gates.m2_gate(out_root: Path, n: int = 20, refine: bool | None = None) -> dict` — synthetic sprite-only scenes (seeds 1..n, `overlap=True`), thresholds from spec §10 M2: `frame_l1 <= 0.02` on ≥ 80 % of clips, `tracking_errors <= 5` on every clip, mean `temporal >= 0.7`; `passed` when all hold. `refine=None` means "use torch if installed".
- `gates.m2_gate_real(clips_dir: Path, out_root: Path) -> dict` — for every `*.mp4` in `clips_dir`: analyze the whole file; if `<name>.gt.json` exists with `{"elements": [{"id": str, "frames": {"<frame>": [cx, cy], ...}}]}` compute `pos_err_px` by matching each annotated element to the nearest analyzed centroid at its first annotated frame; always report `reconstruction mean_l1`, element count, and messages. No pass/fail (real clips are judged by the §10 table by hand).
- CLI additions:
  - `analyze --video V --start S --end E --out ROOT [--copy "A,B"] [--no-ocr] [--no-refine] [--bg #rrggbb]`
  - `correct --root ROOT --scene s1 --op reassign|mask|bbox|text --args '<json>'` where args are the keyword arguments of the Task 24 functions (`frames` as `[a,b]`, `bbox` as `[x0,y0,x1,y1]`, `font` as `{"size_px":..}`)
  - `gate-m2 --out DIR [--n 20]`, `gate-m2-real --clips DIR --out DIR`

- [ ] **Step 1: Write the failing test**

```python
# tests/test_golden_gate.py
import json, subprocess, sys
import numpy as np
from keepframe.ir.synth import make_synthetic_scene
from keepframe.ir.store import current_scene, scene_dir
from keepframe.analyze.video import render_scene_video, read_frames
from keepframe.analyze.pipeline import analyze, AnalyzeOptions
from keepframe.analyze.golden import compare
from keepframe.gates import m2_gate

def test_compare_on_analyzed_synthetic(tmp_scene_dir):
    gold = make_synthetic_scene(tmp_scene_dir / "gold", seed=81, with_text=False, overlap=False)
    vid = render_scene_video(gold, tmp_scene_dir / "gold", tmp_scene_dir / "gold.mp4")
    root = tmp_scene_dir / "proj"
    analyze(vid, 0, gold.frames - 1, root, AnalyzeOptions(ocr=False, refine=False))
    scene, _ = current_scene(root, "s1")
    frames, _ = read_frames(vid)
    m = compare(gold, tmp_scene_dir / "gold", scene, scene_dir(root, "s1"), frames)
    assert m["matched"] == len(gold.elements) and m["tracking_errors"] == 0
    assert m["pos_err_px"] < 2.5 and m["frame_l1"] < 0.03 and m["temporal"] > 0.8

def test_m2_gate_small(tmp_scene_dir):
    res = m2_gate(tmp_scene_dir, n=3, refine=False)
    assert res["n"] == 3 and len(res["rows"]) == 3 and "passed" in res

def test_cli_analyze_and_correct(tmp_scene_dir):
    gold = make_synthetic_scene(tmp_scene_dir / "gold", seed=82, frames=24, with_text=False, overlap=False)
    vid = render_scene_video(gold, tmp_scene_dir / "gold", tmp_scene_dir / "gold.mp4")
    root = tmp_scene_dir / "proj"
    r = subprocess.run([sys.executable, "-m", "keepframe.cli", "analyze", "--video", str(vid), "--start", "0", "--end", "23",
                        "--out", str(root), "--no-ocr", "--no-refine"], capture_output=True, text=True)
    assert r.returncode == 0, r.stderr
    s, _ = current_scene(root, "s1")
    r = subprocess.run([sys.executable, "-m", "keepframe.cli", "correct", "--root", str(root), "--scene", "s1", "--op", "text",
                        "--args", json.dumps({"element_id": s.elements[0].id, "text": "Hi"})], capture_output=True, text=True)
    assert r.returncode == 0 and '"id": "v2"' in r.stdout
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_golden_gate.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'keepframe.analyze.golden'`

- [ ] **Step 3: Implement**

```python
# keepframe/analyze/golden.py
from __future__ import annotations
from pathlib import Path
import numpy as np
from scipy.optimize import linear_sum_assignment
from ..ir.schema import Scene
from ..verify.matrix import animation_matrix
from ..verify.similarity import appearance_similarity, centroid_tracks, temporal_similarity
from .composite import composite_scene, load_texture


def _mean_dist(a: np.ndarray, b: np.ndarray) -> float:
    ok = ~(np.isnan(a[:, 0]) | np.isnan(b[:, 0]))
    return float(np.linalg.norm(a[ok] - b[ok], axis=1).mean()) if ok.sum() >= 2 else 1e9


def compare(golden: Scene, golden_dir: Path, analyzed: Scene, analyzed_dir: Path, source_frames: np.ndarray) -> dict:
    G, A = animation_matrix(golden), animation_matrix(analyzed)
    gids, aids = [e.id for e in golden.elements], [e.id for e in analyzed.elements]
    C = np.array([[_mean_dist(G[g][:, :2], A[a][:, :2]) for a in aids] for g in gids]) if gids and aids else np.zeros((0, 0))
    rows, cols = linear_sum_assignment(C) if C.size else ([], [])
    pairs = [(gids[r], aids[c]) for r, c in zip(rows, cols) if C[r, c] <= 5.0]
    errors = (len(gids) - len(pairs)) + (len(aids) - len(pairs))
    pos, scale, rgb, alpha = [], [], [], []
    for g, a in pairs:
        pos.append(C[gids.index(g), aids.index(a)])
        ok = ~(np.isnan(G[g][:, 2]) | np.isnan(A[a][:, 2]))
        scale.append(float(np.abs(G[g][ok, 2] - A[a][ok, 2]).mean()) if ok.any() else 0.0)
        tg = load_texture(golden_dir / golden.element(g).canonical.texture)
        ta = load_texture(analyzed_dir / analyzed.element(a).canonical.texture)
        import cv2
        ta = cv2.resize(ta, (tg.shape[1], tg.shape[0]), interpolation=cv2.INTER_AREA)
        m = (tg[..., 3] > 0.5) | (ta[..., 3] > 0.5)
        rgb.append(float(np.abs(tg[..., :3] - ta[..., :3])[m].mean()) if m.any() else 0.0)
        alpha.append(float(np.abs(tg[..., 3] - ta[..., 3]).mean()))
    cache: dict = {}
    fl1 = float(np.mean([np.abs(composite_scene(analyzed, analyzed_dir, f, cache) - source_frames[f] / 255.0).mean()
                         for f in range(0, analyzed.frames, max(1, analyzed.frames // 10))]))
    return {"matched": len(pairs), "tracking_errors": int(errors), "pos_err_px": float(np.mean(pos)) if pos else 1e9,
            "scale_err": float(np.mean(scale)) if scale else 1e9, "sprite_rgb_l1": float(np.mean(rgb)) if rgb else 1e9,
            "sprite_alpha_l1": float(np.mean(alpha)) if alpha else 1e9,
            "temporal": temporal_similarity(centroid_tracks(golden), centroid_tracks(analyzed)), "frame_l1": fl1}
```

Add to `keepframe/gates.py`:

```python
def m2_gate(out_root: Path, n: int = 20, refine: bool | None = None) -> dict:
    import numpy as np
    from .analyze.golden import compare
    from .analyze.pipeline import AnalyzeOptions, analyze
    from .analyze.video import read_frames, render_scene_video
    from .ir.store import current_scene, scene_dir
    if refine is None:
        from .analyze.refine import torch_available
        refine = torch_available()
    out_root = Path(out_root); rows = []
    for seed in range(1, n + 1):
        gd = out_root / f"gold{seed}"
        gold = make_synthetic_scene(gd, seed=seed, with_text=False, overlap=True)
        save_scene(gold, gd / "scene.json")
        vid = render_scene_video(gold, gd, out_root / f"gold{seed}.mp4")
        root = out_root / f"proj{seed}"
        analyze(vid, 0, gold.frames - 1, root, AnalyzeOptions(ocr=False, refine=refine))
        scene, _ = current_scene(root, "s1")
        frames, _ = read_frames(vid)
        m = compare(gold, gd, scene, scene_dir(root, "s1"), frames)
        rows.append({"seed": seed, **m})
    l1_ok = sum(r["frame_l1"] <= 0.02 for r in rows)
    trk_ok = all(r["tracking_errors"] <= 5 for r in rows)
    temporal = float(np.mean([r["temporal"] for r in rows]))
    return {"n": n, "refine": refine, "frame_l1_ok": l1_ok, "tracking_ok": trk_ok, "temporal_mean": temporal,
            "passed": l1_ok >= int(round(0.8 * n)) and trk_ok and temporal >= 0.7, "rows": rows}


def m2_gate_real(clips_dir: Path, out_root: Path) -> dict:
    import json, numpy as np
    from .analyze.pipeline import AnalyzeOptions, analyze
    from .analyze.video import read_frames
    from .ir.store import current_scene, scene_dir
    from .verify.matrix import animation_matrix
    rows = []
    for clip in sorted(Path(clips_dir).glob("*.mp4")):
        frames, _ = read_frames(clip)
        root = Path(out_root) / clip.stem
        analyze(clip, 0, len(frames) - 1, root, AnalyzeOptions())
        scene, _ = current_scene(root, "s1")
        rep = json.loads((scene_dir(root, "s1") / "report.json").read_text())
        row = {"clip": clip.name, "elements": len(scene.elements), "mean_l1": rep["reconstruction"]["mean_l1"], "messages": rep["messages"]}
        gt = clip.with_suffix(".gt.json")
        if gt.exists():
            M = animation_matrix(scene); errs = []
            for g in json.loads(gt.read_text())["elements"]:
                items = sorted((int(f), np.array(xy)) for f, xy in g["frames"].items())
                f0, xy0 = items[0]
                best = min(M, key=lambda k: np.linalg.norm(np.nan_to_num(M[k][f0, :2], nan=1e6) - xy0))
                errs += [float(np.linalg.norm(np.nan_to_num(M[best][f, :2], nan=1e6) - xy)) for f, xy in items]
            row["pos_err_px"] = float(np.mean(errs)) if errs else None
        rows.append(row)
    return {"clips": len(rows), "rows": rows}
```

Add to `keepframe/cli.py` (inside `main`, new sub-parsers and branches):

```python
    an = sub.add_parser("analyze"); an.add_argument("--video", required=True); an.add_argument("--start", type=int, default=0)
    an.add_argument("--end", type=int, required=True); an.add_argument("--out", required=True); an.add_argument("--copy", default=None)
    an.add_argument("--no-ocr", action="store_true"); an.add_argument("--no-refine", action="store_true"); an.add_argument("--bg", default=None)
    co = sub.add_parser("correct"); co.add_argument("--root", required=True); co.add_argument("--scene", default="s1")
    co.add_argument("--op", required=True, choices=["reassign", "mask", "bbox", "text"]); co.add_argument("--args", required=True)
    g2 = sub.add_parser("gate-m2"); g2.add_argument("--out", required=True); g2.add_argument("--n", type=int, default=20)
    g2r = sub.add_parser("gate-m2-real"); g2r.add_argument("--clips", required=True); g2r.add_argument("--out", required=True)
```

```python
    if a.cmd == "analyze":
        from .analyze.pipeline import AnalyzeOptions, analyze
        opts = AnalyzeOptions(bg_override=a.bg, copy=a.copy.split(",") if a.copy else None, ocr=not a.no_ocr, refine=not a.no_refine)
        p = analyze(Path(a.video), a.start, a.end, Path(a.out), opts)
        print(json.dumps({"scenes": [s.id for s in p.scenes], "version": p.versions[-1].id})); return 0
    if a.cmd == "correct":
        from .review import corrections as C
        from .ir.schema import FontGuess
        kw = json.loads(a.args)
        if a.op == "reassign":
            v = C.reassign_id(Path(a.root), a.scene, tuple(kw["frames"]), kw["from_id"], kw["to_id"], note=kw.get("note", "reassign id"))
        elif a.op == "mask":
            v = C.set_region_mask(Path(a.root), a.scene, kw["frame"], Path(kw["mask_png"]), kw["object_id"], note=kw.get("note", "set region mask"))
        elif a.op == "bbox":
            v = C.add_bbox_prompt(Path(a.root), a.scene, kw["frame"], tuple(kw["bbox"]), kw["object_id"], note=kw.get("note", "bbox prompt"))
        else:
            v = C.edit_text(Path(a.root), a.scene, kw["element_id"], text=kw.get("text"),
                            font=FontGuess(**kw["font"]) if kw.get("font") else None, note=kw.get("note", "edit text"))
        print(v.model_dump_json(indent=2)); return 0
    if a.cmd == "gate-m2":
        from .gates import m2_gate
        res = m2_gate(Path(a.out), n=a.n)
        for row in res["rows"]:
            print(row)
        print({k: v for k, v in res.items() if k != "rows"}); return 0 if res["passed"] else 1
    if a.cmd == "gate-m2-real":
        from .gates import m2_gate_real
        print(json.dumps(m2_gate_real(Path(a.clips), Path(a.out)), indent=2)); return 0
```

```python
# scripts/m2_gate.py
import sys
from keepframe.cli import main
sys.exit(main(["gate-m2", "--out", "out/m2", "--n", "20"]))
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/test_golden_gate.py -v` then `python scripts/m2_gate.py`
Expected: tests PASS; the gate prints 20 rows and `'passed': True`. If `frame_l1_ok` is below 16/20 with `refine=False`, install the `gpu` extra and rerun — the spec's M2 gate assumes the optimisation step (§5.5) is available. Then run the real-clip hook on the licensed 30-clip set once it exists: `keepframe gate-m2-real --clips data/clips --out out/m2real`.

- [ ] **Step 5: Commit**

```bash
git add keepframe/analyze/golden.py keepframe/gates.py keepframe/cli.py scripts/m2_gate.py tests/test_golden_gate.py
git commit -m "feat: golden comparison, analyze/correct CLI, M2 synthetic gate and real-clip hook"
```

---

## Self-review notes (done while writing)

- **Spec coverage.** §4 IR → Tasks 2–4 (invariants: raw kept in `*_raw.npz` and `Element.raw`, cubic-bezier tracks with `fit_error`, keep flags, append-only versions, `kind=ui` reserved but unused in M2). §5.2–5.6, 5.8–5.10 → Tasks 15–23 (5.1 shot splitting and 5.7 UI parsing are M4 by spec §11 and are not in this plan). §6 → Task 24. §7 edit agent is M3 (not in this plan); the verifier it needs is Task 13. §8 → Tasks 6–7 (HyperFrames attributes, deterministic hashes, MP4). §10 metrics → Tasks 12, 13, 25. §11 M1/M2 completion criteria → Tasks 14 and 25.
- **Deliberate simplifications** are marked `# ponytail:` with the upgrade path in code comments (regions, tracking, z-order, text masks, refinement loss, confidence).
- **Type consistency.** `RAW_COLS` order equals `PROPS` order (x,y,sx,sy,rot,skx,sky,opacity) everywhere raw arrays are indexed; `RenderResult.bboxes[id][i]` pairs with `frames[i]`; `Motion.id` strings are produced by `extract_motions` and consumed by `extract_constraints`/`eval_pred`; `Version`/`new_version` signatures match between store, pipeline and corrections.

## M2 (added 2026-09-05) — Review screen

Design authority: `PRODUCT.md` and `DESIGN.md` at the repo root (tokens, type, layout, principles). The implementer of Task 27 reads DESIGN.md before writing markup and follows it verbatim; the reviewer checks against it.

### Task 26: Review server (stdlib http.server)

**Files:**
- Create: `keepframe/review/server.py`
- Modify: `keepframe/cli.py` (add `review --root ROOT [--scene s1] [--port 8765]`)
- Test: `tests/test_review_server.py`

**Interfaces:**
- `class ReviewState(root: Path, scene_id: str = "s1")` — holds `job = {"status": "idle"|"running"|"done"|"error", "op": str|None, "error": str|None, "version": str|None}`, a lock, lazy `frames()` (mmap of `stages/frames.npy`), a PNG cache keyed `(version_id, frame)`.
- `make_server(root: Path, scene_id: str = "s1", port: int = 8765, host: str = "127.0.0.1") -> ThreadingHTTPServer` — returns an unstarted server (tests call `serve_forever` in a thread).
- HTTP API (all JSON responses `application/json; charset=utf-8`, errors `{"error": str}` with 4xx/5xx):
  - `GET /` → `keepframe/review/ui.html` (Task 27; until then a 404 with `{"error": "ui.html missing"}`)
  - `GET /api/state[?v=vN]` → `{"project": <project.json>, "version": <Version>, "scene": <scene json>, "report": <report.json or null>, "job": <job>}`
  - `GET /frame/orig/<f>` → PNG of source frame `f`; `GET /frame/recon/<f>[?v=vN]` → PNG of `composite_scene` for that version (cached)
  - `GET /assets/<name>` → file from `scenes/<id>/assets/` (PNG only; any `..` → 400)
  - `POST /api/keep` body `{"changes": [{"pred": str, "keep": bool}, ...], "note": str}` → appends one new version with the toggled constraints → `{"version": <Version>}`
  - `POST /api/correct` body `{"op": "reassign"|"mask"|"bbox"|"text", "args": {...}}` → 202 `{"job": ...}`; runs the Task 24 function in a background thread; `mask` args carry `"mask_png_base64"` which the server writes to a temp file. A second POST while `running` → 409.
  - `GET /api/job` → `job`
- Frame PNG encoding: `cv2.imencode(".png", bgr)`; source frames come from `stages/frames.npy` (RGB) so convert.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_review_server.py
import base64, json, threading, time, urllib.request, cv2, numpy as np
from keepframe.ir.synth import make_synthetic_scene
from keepframe.ir.store import current_scene
from keepframe.analyze.video import render_scene_video
from keepframe.analyze.pipeline import analyze, AnalyzeOptions
from keepframe.review.server import make_server

def project(tmp, seed=91, frames=16):
    gold = make_synthetic_scene(tmp / "gold", seed=seed, frames=frames, with_text=False, overlap=False)
    vid = render_scene_video(gold, tmp / "gold", tmp / "gold.mp4")
    root = tmp / "proj"
    analyze(vid, 0, frames - 1, root, AnalyzeOptions(ocr=False, refine=False))
    return root

def get(url):
    with urllib.request.urlopen(url, timeout=30) as r:
        return r.status, r.headers.get("content-type", ""), r.read()

def post(url, body):
    req = urllib.request.Request(url, data=json.dumps(body).encode(), headers={"content-type": "application/json"}, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return r.status, json.loads(r.read())
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read())

def test_state_frames_keep_and_correct(tmp_scene_dir):
    root = project(tmp_scene_dir)
    srv = make_server(root, port=0)
    port = srv.server_address[1]
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{port}"
    st, ct, body = get(base + "/api/state")
    assert st == 200 and "json" in ct
    state = json.loads(body)
    assert state["version"]["id"] == "v1" and state["scene"]["schema"] == "keepframe.scene/1" and state["report"]["reconstruction"]
    st, ct, png = get(base + "/frame/orig/3")
    assert st == 200 and ct == "image/png" and cv2.imdecode(np.frombuffer(png, np.uint8), 1).shape == (360, 640, 3)
    st, ct, png2 = get(base + "/frame/recon/3")
    assert st == 200 and ct == "image/png"
    pred = state["scene"]["constraints"][0]["pred"]
    st, res = post(base + "/api/keep", {"changes": [{"pred": pred, "keep": True}], "note": "keep test"})
    assert st == 200 and res["version"]["id"] == "v2"
    s2, _ = current_scene(root, "s1")
    assert any(c.pred == pred and c.keep for c in s2.constraints)
    eid = state["scene"]["elements"][0]["id"]
    st, res = post(base + "/api/correct", {"op": "text", "args": {"element_id": eid, "text": "Hi"}})
    assert st == 202
    for _ in range(100):
        job = json.loads(get(base + "/api/job")[2])
        if job["status"] in ("done", "error"):
            break
        time.sleep(0.1)
    assert job["status"] == "done" and job["version"] == "v3", job
    st, _, _ = get(base + "/assets/" + state["scene"]["elements"][0]["canonical"]["texture"].split("/")[-1])
    assert st == 200
    srv.shutdown()

def test_path_traversal_and_bad_op_rejected(tmp_scene_dir):
    root = project(tmp_scene_dir, seed=92, frames=8)
    srv = make_server(root, port=0); port = srv.server_address[1]
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{port}"
    try:
        urllib.request.urlopen(base + "/assets/../scene.v1.json", timeout=10); assert False
    except urllib.error.HTTPError as e:
        assert e.code == 400
    st, res = post(base + "/api/correct", {"op": "dance", "args": {}})
    assert st == 400 and "op" in res["error"]
    srv.shutdown()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_review_server.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'keepframe.review.server'`

- [ ] **Step 3: Implement**

```python
# keepframe/review/server.py
from __future__ import annotations
import base64, json, tempfile, threading, traceback
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse
import cv2, numpy as np
from ..analyze.composite import composite_scene
from ..ir.schema import FontGuess
from ..ir.store import current_scene, load_project, load_scene, new_version, scene_dir
from . import corrections

UI = Path(__file__).parent / "ui.html"
OPS = {"reassign", "mask", "bbox", "text"}


class ReviewState:
    def __init__(self, root: Path, scene_id: str = "s1"):
        self.root, self.scene_id = Path(root), scene_id
        self.job = {"status": "idle", "op": None, "error": None, "version": None}
        self.lock = threading.Lock()
        self._frames = None
        self._png: dict[tuple[str, int], bytes] = {}

    def frames(self):
        if self._frames is None:
            self._frames = np.load(scene_dir(self.root, self.scene_id) / "stages" / "frames.npy", mmap_mode="r")
        return self._frames

    def scene(self, version: str | None = None):
        if version:
            p = load_project(self.root)
            v = next(v for v in p.versions if v.id == version and v.scene_file.startswith(f"scenes/{self.scene_id}/"))
            return load_scene(self.root / v.scene_file), v
        return current_scene(self.root, self.scene_id)

    def recon_png(self, f: int, version: str | None) -> bytes:
        scene, v = self.scene(version)
        key = (v.id, f)
        if key not in self._png:
            rgb = (composite_scene(scene, scene_dir(self.root, self.scene_id), f) * 255).round().clip(0, 255).astype(np.uint8)
            self._png[key] = _png(rgb)
        return self._png[key]

    def run_correction(self, op: str, args: dict) -> None:
        with self.lock:
            if self.job["status"] == "running":
                raise RuntimeError("busy")
            self.job = {"status": "running", "op": op, "error": None, "version": None}

        def work():
            try:
                if op == "reassign":
                    v = corrections.reassign_id(self.root, self.scene_id, tuple(args["frames"]), args["from_id"], args["to_id"], note=args.get("note", "reassign id"))
                elif op == "mask":
                    with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as t:
                        t.write(base64.b64decode(args["mask_png_base64"]))
                    v = corrections.set_region_mask(self.root, self.scene_id, int(args["frame"]), Path(t.name), args["object_id"], note=args.get("note", "set region mask"))
                elif op == "bbox":
                    v = corrections.add_bbox_prompt(self.root, self.scene_id, int(args["frame"]), tuple(int(x) for x in args["bbox"]), args["object_id"], note=args.get("note", "bbox prompt"))
                else:
                    v = corrections.edit_text(self.root, self.scene_id, args["element_id"], text=args.get("text"),
                                              font=FontGuess(**args["font"]) if args.get("font") else None, note=args.get("note", "edit text"))
                self._png.clear()
                self.job = {"status": "done", "op": op, "error": None, "version": v.id}
            except Exception as e:  # surfaced to the UI, never hidden
                self.job = {"status": "error", "op": op, "error": f"{type(e).__name__}: {e}", "version": None}
                traceback.print_exc()

        threading.Thread(target=work, daemon=True).start()


def _png(rgb: np.ndarray) -> bytes:
    ok, buf = cv2.imencode(".png", cv2.cvtColor(np.ascontiguousarray(rgb), cv2.COLOR_RGB2BGR))
    return buf.tobytes()


def make_server(root: Path, scene_id: str = "s1", port: int = 8765, host: str = "127.0.0.1") -> ThreadingHTTPServer:
    state = ReviewState(root, scene_id)

    class H(BaseHTTPRequestHandler):
        def log_message(self, *a):  # quiet
            pass

        def _send(self, code: int, body: bytes, ctype: str) -> None:
            self.send_response(code); self.send_header("content-type", ctype); self.send_header("content-length", str(len(body)))
            self.end_headers(); self.wfile.write(body)

        def _json(self, code: int, obj) -> None:
            self._send(code, json.dumps(obj, ensure_ascii=False).encode("utf-8"), "application/json; charset=utf-8")

        def do_GET(self):
            u = urlparse(self.path); q = parse_qs(u.query); parts = u.path.strip("/").split("/")
            try:
                if u.path == "/":
                    if not UI.exists():
                        return self._json(404, {"error": "ui.html missing"})
                    return self._send(200, UI.read_bytes(), "text/html; charset=utf-8")
                if u.path == "/api/state":
                    scene, v = state.scene(q.get("v", [None])[0])
                    sd = scene_dir(state.root, state.scene_id)
                    rep = json.loads((sd / "report.json").read_text()) if (sd / "report.json").exists() else None
                    return self._json(200, {"project": json.loads(load_project(state.root).model_dump_json(by_alias=True)),
                                            "version": json.loads(v.model_dump_json()), "scene": json.loads(scene.model_dump_json(by_alias=True)),
                                            "report": rep, "job": state.job})
                if u.path == "/api/job":
                    return self._json(200, state.job)
                if parts[:2] == ["frame", "orig"]:
                    f = int(parts[2]); fr = state.frames()
                    if not 0 <= f < len(fr):
                        return self._json(404, {"error": "frame out of range"})
                    return self._send(200, _png(np.asarray(fr[f])), "image/png")
                if parts[:2] == ["frame", "recon"]:
                    return self._send(200, state.recon_png(int(parts[2]), q.get("v", [None])[0]), "image/png")
                if parts[0] == "assets":
                    name = "/".join(parts[1:])
                    if ".." in name or not name.endswith(".png"):
                        return self._json(400, {"error": "bad asset path"})
                    p = scene_dir(state.root, state.scene_id) / "assets" / name
                    if not p.exists():
                        return self._json(404, {"error": "no such asset"})
                    return self._send(200, p.read_bytes(), "image/png")
                return self._json(404, {"error": "not found"})
            except Exception as e:
                traceback.print_exc(); return self._json(500, {"error": f"{type(e).__name__}: {e}"})

        def do_POST(self):
            n = int(self.headers.get("content-length", "0"))
            try:
                body = json.loads(self.rfile.read(n) or b"{}")
            except json.JSONDecodeError:
                return self._json(400, {"error": "body must be JSON"})
            try:
                if self.path == "/api/keep":
                    scene, _ = state.scene()
                    wanted = {c["pred"]: bool(c["keep"]) for c in body.get("changes", [])}
                    for c in scene.constraints:
                        if c.pred in wanted:
                            c.keep = wanted[c.pred]
                    v = new_version(state.root, state.scene_id, scene, note=body.get("note", "keep 조건 수정"), auto=False)
                    state._png.clear()
                    return self._json(200, {"version": json.loads(v.model_dump_json())})
                if self.path == "/api/correct":
                    op = body.get("op")
                    if op not in OPS:
                        return self._json(400, {"error": f"unknown op {op!r}; expected one of {sorted(OPS)}"})
                    try:
                        state.run_correction(op, body.get("args", {}))
                    except RuntimeError:
                        return self._json(409, {"error": "a correction is already running"})
                    return self._json(202, {"job": state.job})
                return self._json(404, {"error": "not found"})
            except Exception as e:
                traceback.print_exc(); return self._json(500, {"error": f"{type(e).__name__}: {e}"})

    return ThreadingHTTPServer((host, port), H)
```

Add to `keepframe/cli.py`:

```python
    rv = sub.add_parser("review"); rv.add_argument("--root", required=True); rv.add_argument("--scene", default="s1"); rv.add_argument("--port", type=int, default=8765)
```

```python
    if a.cmd == "review":
        from .review.server import make_server
        srv = make_server(Path(a.root), a.scene, a.port)
        print(f"review UI: http://127.0.0.1:{srv.server_address[1]}/  (Ctrl+C to stop)")
        try:
            srv.serve_forever()
        except KeyboardInterrupt:
            pass
        return 0
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/test_review_server.py -v`
Expected: 2 PASS

- [ ] **Step 5: Commit**

```bash
git add keepframe/review/server.py keepframe/cli.py tests/test_review_server.py
git commit -m "feat(review): stdlib http server with state, frames, keep toggles and correction jobs"
```

---

### Task 27: Review screen (single HTML file)

**Files:**
- Create: `keepframe/review/ui.html`
- Modify: `pyproject.toml` (package-data: add `review/ui.html`)
- Test: `tests/test_review_ui.py`

**Design authority:** read `DESIGN.md` and `PRODUCT.md` first. Tokens, type, layout, principles and copy rules there are requirements. The page is one file: `<style>` + markup + `<script>`, no external resources, no framework.

**Interfaces / behavior:**
- Loads `/api/state` on start; `?v=vN` in the page URL selects a version; header select lists all versions (`id` + `note`) and switches by reloading with `?v=`.
- Viewer: two `<img>` panes (`#orig`, `#recon`) sized to the scene aspect, updated on scrub with `/frame/orig/{f}` and `/frame/recon/{f}?v=`. Prefetch the next 4 frames. A selected element's bbox is drawn as an SVG overlay on both panes from its tracks (JS mirror of `eval_track` / corners; skew via `skx` only).
- Transport: play/pause button and `Space`; `←`/`→` one frame, `Shift` = 10 frames; `Home`/`End`; `[`/`]` jump to previous/next error peak (per-frame L1 above the 80th percentile). Frame counter `f / N` and time `mm:ss.ff`, tabular numerals. Playback steps frames at the scene fps with `requestAnimationFrame`; `prefers-reduced-motion` disables only the 150 ms UI transitions (playback is content, not decoration).
- Timeline (SVG, full width of the viewer column, 8 px per row min): one row per element (visible span bar in the role hue at 60 %, keyframe ticks in `--text-2`), the error strip (one `rect` per frame, height ∝ L1, `--error` above the peak threshold, `--text-2` otherwise) and a playhead. Click/drag scrubs. Row click selects the element.
- Side panel: element list (swatch by role, id, kind, text or texture thumbnail from `/assets/`, confidence as a 5-segment bar with the number, provenance badge 자동/수동; confidence < 0.7 shows the number in `--caution`); selected element section with its constraints (checkbox per predicate, `keep` state) and the four corrections as `<details>` disclosures with forms: 재할당 (from id prefilled, to id select, frame range), 마스크 (file input PNG → base64), 박스 프롬프트 (frame prefilled; drag a rectangle on the original pane fills x0,y0,x1,y1), 텍스트 (text + size). Buttons: "재할당 실행", "마스크 적용", "박스 적용", "텍스트 저장", and "유지 조건 저장" for pending keep changes (disabled until something changed).
- Jobs: after a correction POST, poll `/api/job` every 700 ms; while running, the whole side panel form group is `disabled` and a banner shows "보정 실행 중: <op>"; on `done` reload with `?v=<new version>`; on `error` show the message in the banner in `--error` with the recovery hint "값을 확인하고 다시 실행하세요".
- Empty state (no elements): the panel says "인식된 요소가 없습니다. 원본 화면에서 박스를 그려 첫 요소를 지정하세요." and the bbox disclosure is open.
- i18n: `const T = {ko: {...}, en: {...}}`; every visible string uses `data-i18n="key"` (or `data-i18n-title`); the header toggle button switches `document.documentElement.lang`, re-renders strings and stores the choice in `localStorage("keepframe.lang")`. All keys exist in both languages (test enforces).
- Accessibility: every control has a visible label or `aria-label`; focus ring per DESIGN.md; the timeline SVG has `role="slider"` with `aria-valuenow`; error/job banner uses `role="status"`; contrast pairs from DESIGN.md only.
- Browser surfaces themed: `::selection`, scrollbar (`scrollbar-width: thin; scrollbar-color`), `caret-color`, focus rings, `accent-color` for checkboxes, `text-underline-offset`.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_review_ui.py
import json, re, threading, pytest
from pathlib import Path
from keepframe.ir.synth import make_synthetic_scene
from keepframe.analyze.video import render_scene_video
from keepframe.analyze.pipeline import analyze, AnalyzeOptions
from keepframe.review.server import make_server

UI = Path("keepframe/review/ui.html")

def test_ui_is_self_contained_and_bilingual():
    html = UI.read_text()
    assert "<script src=" not in html and "<link " not in html and "@import" not in html
    keys = set(re.findall(r'data-i18n(?:-title)?="([a-zA-Z0-9_.]+)"', html))
    assert len(keys) >= 25
    for lang in ("ko", "en"):
        table = re.search(r"\b%s\s*:\s*\{(.*?)\n\s*\}" % lang, html, re.S).group(1)
        defined = set(re.findall(r'^\s*([a-zA-Z0-9_.]+)\s*:', table, re.M))
        assert keys <= defined, (lang, keys - defined)
    for token in ("--ink", "--panel", "--accent", "--caution", "--error", "tabular-nums", "prefers-reduced-motion", "::selection", "scrollbar-color", "role=\"slider\"", "role=\"status\""):
        assert token in html, token
    assert "<details" in html and "dialog" not in html.lower()

@pytest.mark.browser
def test_ui_loads_scrubs_and_toggles_language(tmp_scene_dir):
    from playwright.sync_api import sync_playwright
    gold = make_synthetic_scene(tmp_scene_dir / "gold", seed=93, frames=16, with_text=False, overlap=False)
    vid = render_scene_video(gold, tmp_scene_dir / "gold", tmp_scene_dir / "gold.mp4")
    root = tmp_scene_dir / "proj"
    analyze(vid, 0, 15, root, AnalyzeOptions(ocr=False, refine=False))
    srv = make_server(root, port=0); port = srv.server_address[1]
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    with sync_playwright() as p:
        b = p.chromium.launch(); page = b.new_page(viewport={"width": 1280, "height": 800})
        page.goto(f"http://127.0.0.1:{port}/")
        page.wait_for_selector("#frame-counter")
        page.wait_for_function("document.querySelector('#frame-counter').textContent.trim().startsWith('0 / 16')")
        page.wait_for_function("document.querySelector('#orig').naturalWidth === 640")
        page.keyboard.press("ArrowRight")
        page.wait_for_function("document.querySelector('#frame-counter').textContent.trim().startsWith('1 / 16')")
        assert page.evaluate("document.querySelectorAll('.el-row').length") == len(gold.elements)
        page.click(".el-row")
        assert page.evaluate("document.querySelectorAll('svg.overlay rect').length") >= 1
        assert page.text_content("#title").strip().startswith("검수")
        page.click("#lang-toggle")
        page.wait_for_function("document.documentElement.lang === 'en'")
        assert "Review" in page.text_content("#title")
        assert page.evaluate("getComputedStyle(document.body).backgroundColor") == "rgb(27, 30, 36)"
        b.close()
    srv.shutdown()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_review_ui.py -v`
Expected: FAIL with `FileNotFoundError: keepframe/review/ui.html`

- [ ] **Step 3: Build the page**

Write `keepframe/review/ui.html` following DESIGN.md. The skeleton below is the required structure; complete the CSS and JS so every behavior in the Interfaces section works.

```html
<!doctype html>
<html lang="ko"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Keepframe 검수</title>
<style>
:root{--ink:#1B1E24;--panel:#232730;--line:#343A45;--raise:#2C313B;--text:#E8EAEE;--text-2:#A9B0BC;--accent:#59D3C3;--on-accent:#0F1A18;--caution:#F0B44A;--error:#F27D72;
--role-text:#B39CFF;--role-primary:#59D3C3;--role-secondary:#7FA6FF;--role-background:#6F7785;--t:150ms cubic-bezier(.16,1,.3,1)}
@media (prefers-reduced-motion:reduce){:root{--t:0ms}}
*{box-sizing:border-box}html,body{margin:0;height:100%}
body{background:var(--ink);color:var(--text);font:14px/1.5 "Pretendard Variable",Pretendard,-apple-system,"Segoe UI","Noto Sans KR",sans-serif;caret-color:var(--accent);accent-color:var(--accent);scrollbar-width:thin;scrollbar-color:var(--line) var(--panel)}
::selection{background:var(--accent);color:var(--on-accent)}
a{color:var(--accent);text-underline-offset:.2em}
:focus-visible{outline:2px solid var(--accent);outline-offset:2px}
button,select,input,textarea{font:inherit;color:inherit}
button{background:var(--panel);border:1px solid var(--line);border-radius:6px;padding:6px 12px;cursor:pointer;transition:background var(--t),border-color var(--t)}
button:hover{background:var(--raise)}button:disabled{opacity:.5;cursor:not-allowed}
button.primary{background:var(--accent);color:var(--on-accent);border-color:transparent;font-weight:600}
.num{font-variant-numeric:tabular-nums}
header{height:48px;display:flex;align-items:center;gap:16px;padding:0 16px;background:var(--panel);border-bottom:1px solid var(--line)}
header h1{font-size:16px;font-weight:600;margin:0}
main{display:grid;grid-template-columns:minmax(0,1fr) 360px;height:calc(100% - 48px)}
@media (max-width:1100px){main{grid-template-columns:1fr;height:auto}}
#viewer{display:grid;grid-template-rows:auto auto 1fr;min-width:0;padding:16px;gap:12px}
.panes{display:grid;grid-template-columns:1fr 1fr;gap:12px}
.pane{position:relative;background:#000;border:1px solid var(--line)}
.pane img{display:block;width:100%;height:auto}
.pane svg.overlay{position:absolute;inset:0;width:100%;height:100%}
.pane h2{position:absolute;top:8px;left:8px;margin:0;font-size:12px;font-weight:600;color:var(--text-2);background:rgba(27,30,36,.8);padding:2px 6px;border-radius:4px}
#timeline{width:100%;height:100%;min-height:160px;background:var(--panel);border:1px solid var(--line);border-radius:6px;cursor:col-resize}
aside{background:var(--panel);border-left:1px solid var(--line);overflow:auto;padding:16px;display:flex;flex-direction:column;gap:16px}
.el-row{display:grid;grid-template-columns:12px 1fr auto;gap:8px;align-items:center;padding:6px 8px;border-radius:6px;cursor:pointer;transition:background var(--t)}
.el-row:hover{background:var(--raise)}.el-row[aria-selected="true"]{background:var(--raise);box-shadow:inset 0 0 0 1px var(--accent)}
.swatch{width:12px;height:12px;border-radius:3px}
.badge{font-size:12px;color:var(--text-2);border:1px solid var(--line);border-radius:4px;padding:0 6px}
.badge.manual{color:var(--on-accent);background:var(--accent);border-color:transparent}
.conf{display:inline-grid;grid-template-columns:repeat(5,8px);gap:2px;vertical-align:middle;margin-right:6px}.conf i{height:8px;background:var(--line);border-radius:1px}.conf i.on{background:var(--text-2)}
.low{color:var(--caution)}
details{border:1px solid var(--line);border-radius:6px;padding:8px 12px}details summary{cursor:pointer;font-weight:600}
form{display:grid;gap:8px;margin-top:8px}label{display:grid;gap:4px;font-size:13px;color:var(--text-2)}
input[type=text],input[type=number],select{background:var(--ink);border:1px solid var(--line);border-radius:6px;padding:6px 8px}
#banner{display:none;padding:8px 12px;border-radius:6px;background:var(--raise);border:1px solid var(--line)}#banner.error{border-color:var(--error);color:var(--error)}
</style></head>
<body>
<header><h1 id="title" data-i18n="title">검수</h1><span class="num" id="scene-label"></span>
<select id="version" aria-label="버전" data-i18n-title="version"></select>
<span style="flex:1"></span>
<button id="lang-toggle" aria-label="언어 전환">EN</button></header>
<main>
 <section id="viewer">
  <div class="panes">
   <figure class="pane" style="margin:0"><h2 data-i18n="orig">원본</h2><img id="orig" alt=""><svg class="overlay" aria-hidden="true"></svg></figure>
   <figure class="pane" style="margin:0"><h2 data-i18n="recon">재구성</h2><img id="recon" alt=""><svg class="overlay" aria-hidden="true"></svg></figure>
  </div>
  <div class="transport"><button id="play" data-i18n="play">재생</button> <span class="num" id="frame-counter">0 / 0</span> <span class="num" id="time">00:00.00</span> <span id="hint" data-i18n="hint">Space 재생, ←/→ 프레임, [ ] 오차 구간</span></div>
  <svg id="timeline" role="slider" aria-label="타임라인" aria-valuemin="0" aria-valuenow="0" tabindex="0"></svg>
 </section>
 <aside>
  <div id="banner" role="status"></div>
  <h2 style="font-size:16px;margin:0"><span data-i18n="elements">요소</span> <span class="num" id="el-count"></span></h2>
  <div id="el-list"></div>
  <div id="empty" hidden data-i18n="empty">인식된 요소가 없습니다. 원본 화면에서 박스를 그려 첫 요소를 지정하세요.</div>
  <fieldset id="forms" style="border:0;padding:0;margin:0;display:grid;gap:12px">
   <section id="selected" hidden><h2 style="font-size:16px;margin:0" data-i18n="selected">선택한 요소</h2><div id="constraints"></div><button id="save-keep" class="primary" disabled data-i18n="saveKeep">유지 조건 저장</button></section>
   <details><summary data-i18n="reassign">재할당</summary><form id="f-reassign"><label><span data-i18n="fromId">원래 id</span><input type="text" name="from_id"></label><label><span data-i18n="toId">대상 id</span><select name="to_id"></select></label><label><span data-i18n="frames">프레임 범위</span><span><input type="number" name="f0" value="0" style="width:5em"> – <input type="number" name="f1" style="width:5em"></span></label><button class="primary" data-i18n="runReassign">재할당 실행</button></form></details>
   <details><summary data-i18n="mask">마스크</summary><form id="f-mask"><label><span data-i18n="frame">프레임</span><input type="number" name="frame"></label><label><span data-i18n="maskFile">마스크 PNG</span><input type="file" name="mask" accept="image/png"></label><label><span data-i18n="objectId">대상 요소</span><select name="object_id"></select></label><button class="primary" data-i18n="runMask">마스크 적용</button></form></details>
   <details id="d-bbox"><summary data-i18n="bbox">박스 프롬프트</summary><form id="f-bbox"><p data-i18n="bboxHelp">원본 화면에서 드래그하면 값이 채워집니다.</p><label><span data-i18n="frame">프레임</span><input type="number" name="frame"></label><span><input type="number" name="x0" style="width:4.5em"> <input type="number" name="y0" style="width:4.5em"> <input type="number" name="x1" style="width:4.5em"> <input type="number" name="y1" style="width:4.5em"></span><label><span data-i18n="objectId">대상 요소</span><select name="object_id"></select></label><button class="primary" data-i18n="runBbox">박스 적용</button></form></details>
   <details><summary data-i18n="text">텍스트</summary><form id="f-text"><label><span data-i18n="textValue">문구</span><input type="text" name="text"></label><label><span data-i18n="fontSize">글자 크기 (px)</span><input type="number" name="size_px"></label><button class="primary" data-i18n="runText">텍스트 저장</button></form></details>
  </fieldset>
 </aside>
</main>
<script>
const T = {
  ko: { title: "검수", orig: "원본", recon: "재구성", play: "재생", pause: "일시정지", hint: "Space 재생, ←/→ 프레임, [ ] 오차 구간", elements: "요소", empty: "인식된 요소가 없습니다. 원본 화면에서 박스를 그려 첫 요소를 지정하세요.", selected: "선택한 요소", saveKeep: "유지 조건 저장", reassign: "재할당", fromId: "원래 id", toId: "대상 id", frames: "프레임 범위", runReassign: "재할당 실행", mask: "마스크", frame: "프레임", maskFile: "마스크 PNG", objectId: "대상 요소", runMask: "마스크 적용", bbox: "박스 프롬프트", bboxHelp: "원본 화면에서 드래그하면 값이 채워집니다.", runBbox: "박스 적용", text: "텍스트", textValue: "문구", fontSize: "글자 크기 (px)", runText: "텍스트 저장", version: "버전", auto: "자동", manual: "수동", running: "보정 실행 중", failed: "보정 실패", retry: "값을 확인하고 다시 실행하세요.", keepSaved: "유지 조건을 저장했습니다.", unsupported: "미지원"
  },
  en: { title: "Review", orig: "Reference", recon: "Reconstruction", play: "Play", pause: "Pause", hint: "Space play, ←/→ frame, [ ] error peaks", elements: "Elements", empty: "No elements were recognized. Drag a box on the reference to define the first one.", selected: "Selected element", saveKeep: "Save keep rules", reassign: "Reassign id", fromId: "From id", toId: "To id", frames: "Frame range", runReassign: "Run reassign", mask: "Region mask", frame: "Frame", maskFile: "Mask PNG", objectId: "Target element", runMask: "Apply mask", bbox: "Box prompt", bboxHelp: "Drag on the reference pane to fill the values.", runBbox: "Apply box", text: "Text", textValue: "Copy", fontSize: "Font size (px)", runText: "Save text", version: "Version", auto: "auto", manual: "manual", running: "Correction running", failed: "Correction failed", retry: "Check the values and run again.", keepSaved: "Keep rules saved.", unsupported: "unsupported"
  }
};
// state: {scene, report, version, versions, frame, playing, selected, keepChanges}
// bezierY / evalTrack / corners: mirror of keepframe/ir/tracks.py (cubic-bezier by bisection, clamp outside keys, T·R·SkewX·S about the anchor)
// render(): panes (img src = /frame/orig/{f}, /frame/recon/{f}?v=), overlay rects for the selected element, timeline rows + error strip + playhead, element list, constraints with keep checkboxes
// keyboard: Space, ArrowLeft/Right (+Shift ×10), Home, End, [ and ] (peaks = per_frame L1 > 80th percentile)
// timeline pointer events scrub; drag on #orig's overlay fills the bbox form; forms POST /api/correct then poll /api/job every 700 ms
// applyLang(): every [data-i18n] / [data-i18n-title] from T[lang]; document.documentElement.lang; localStorage "keepframe.lang"
</script></body></html>
```

Rules while completing the JS (from DESIGN.md and the craft floor): no extra libraries; transitions only on state changes; every string through `T`; role hues only for swatches and timeline bars; the error strip and both panes must be visible without scrolling at 1280×800; every interactive control has default, hover, focus, disabled states.

- [ ] **Step 4: Run the tests, then the design detector and a screenshot pass**

Run: `pytest tests/test_review_ui.py -v` and `pytest tests/test_review_ui.py -v -m browser`
Expected: PASS.
Then run the mechanical design detector once over the page and fix everything it reports before committing:
`node /home/singlerr/.claude/skills/impeccable/scripts/detect.mjs --json keepframe/review/ui.html`
Expected: no findings (an empty list). Finally take two screenshots with Playwright (1280×800 and 1440×900) of a synthetic project, look at them, and fix overlaps or clipped copy in one batch; save the screenshots under `out/review-screens/` (git-ignored).

- [ ] **Step 5: Commit**

```bash
git add keepframe/review/ui.html pyproject.toml tests/test_review_ui.py
git commit -m "feat(review): bilingual review screen with side-by-side viewer, error strip timeline and corrections"
```

---
