import cv2
import numpy as np
from refstudio.ir.synth import make_synthetic_scene, make_texture
from refstudio.ir.schema import dump


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
