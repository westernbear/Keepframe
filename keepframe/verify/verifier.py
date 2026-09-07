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
