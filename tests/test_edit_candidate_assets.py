"""Final review: edit candidates see the scene's assets through hard links (a copy where linking fails), without the
derived AE footage or temporaries; a candidate that rewrites an asset name never writes through to the scene."""
import os

import cv2
import numpy as np

from keepframe.edit import agent as edit_agent
from keepframe.edit.agent import edit
from tests.test_edit_background_facts import NAVY, _colour_intent, _project


def _seed(sd):
    assets = sd / "assets"
    (assets / "e1.video.webm").write_bytes(b"webm" * 1000)
    (assets / "background.0123456789abcdef.ae.mp4").write_bytes(b"derived plate")
    (assets / "e1.video.0123456789abcdef.ae.mov").write_bytes(b"derived sprite")
    (assets / ".e1.png.123.tmp").write_bytes(b"a temporary")


def _spy(monkeypatch):
    seen = []
    real = edit_agent.apply_edit

    def apply(scene, candidate, *a, **k):
        seen.append({p.name: p.stat().st_ino for p in (candidate / "assets").iterdir()})
        return real(scene, candidate, *a, **k)

    monkeypatch.setattr(edit_agent, "apply_edit", apply)
    return seen


def test_candidate_links_assets_and_skips_derived_footage(tmp_path, monkeypatch):
    root, sd, scene = _project(tmp_path, "video", monkeypatch)
    _seed(sd)
    seen = _spy(monkeypatch)
    done = edit(root, "s1", "navy", confirm=True, intent=_colour_intent())
    assert done.status == "done", done
    (names,) = seen
    assert set(names) == {"background.png", "background.webm", "e1.video.webm"}   # no .ae.* footage, no dotfiles
    for name, inode in names.items():
        assert inode == (sd / "assets" / name).stat().st_ino, name                 # linked, not copied


def test_candidate_copies_when_links_fail(tmp_path, monkeypatch):
    root, sd, scene = _project(tmp_path, "video", monkeypatch)
    _seed(sd)
    seen = _spy(monkeypatch)

    def no_link(*a, **k):
        raise OSError(18, "Invalid cross-device link")

    monkeypatch.setattr(edit_agent.os, "link", no_link)
    done = edit(root, "s1", "navy", confirm=True, intent=_colour_intent())
    assert done.status == "done", done
    (names,) = seen
    assert set(names) == {"background.png", "background.webm", "e1.video.webm"}
    assert all(inode != (sd / "assets" / name).stat().st_ino for name, inode in names.items())


def test_rewriting_an_element_texture_in_a_candidate_leaves_the_scene_file(tmp_path):
    """guard_reference_edit rebuilds restored fragments with _elements_from_props into the candidate: the names exist
    there as links to the scene's files, so the writes must replace them, not write through."""
    from keepframe.analyze.pipeline import _elements_from_props
    sd, cand = tmp_path / "scene", tmp_path / "cand"
    (sd / "assets").mkdir(parents=True)
    (cand / "assets").mkdir(parents=True)
    for name in ("e1.png", "e1_raw.npz"):
        (sd / "assets" / name).write_bytes(b"the scene's own bytes")
        os.link(sd / "assets" / name, cand / "assets" / name)
    canon = np.zeros((6, 8, 4), np.uint8)
    canon[..., 1:] = 200
    raw = np.full((3, 2), 5.0)
    props = {"o1": {"raw": raw, "canon": canon, "cf": 0, "kind": "sprite", "z": 0, "first": 0, "last": 2}}
    _elements_from_props(props, cand, {"o1": "e1"})
    for name in ("e1.png", "e1_raw.npz"):
        assert (sd / "assets" / name).read_bytes() == b"the scene's own bytes", name
    assert cv2.imread(str(cand / "assets" / "e1.png"), cv2.IMREAD_UNCHANGED).shape == (6, 8, 4)
    assert not [p.name for p in (cand / "assets").iterdir() if p.name.startswith(".")]   # no temporaries left
