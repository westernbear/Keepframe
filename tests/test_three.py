import json
import struct

from keepframe.compose.composer import compose
from keepframe.ir.schema import Background, Canonical, Element, Keyframe, Scene, Track
from keepframe.render.renderer import render


def _triangle_glb() -> bytes:
    positions = struct.pack("<9f", -0.8, -0.7, 0, 0.8, -0.7, 0, 0, 0.8, 0)
    indices = struct.pack("<3H", 0, 1, 2) + b"\0\0"
    binary = positions + indices
    document = {
        "asset": {"version": "2.0"},
        "scene": 0,
        "scenes": [{"nodes": [0]}],
        "nodes": [{"mesh": 0}],
        "meshes": [{"primitives": [{"attributes": {"POSITION": 0}, "indices": 1}]}],
        "buffers": [{"byteLength": len(binary)}],
        "bufferViews": [{"buffer": 0, "byteOffset": 0, "byteLength": len(positions), "target": 34962}, {"buffer": 0, "byteOffset": len(positions), "byteLength": 6, "target": 34963}],
        "accessors": [{"bufferView": 0, "componentType": 5126, "count": 3, "type": "VEC3", "min": [-0.8, -0.7, 0], "max": [0.8, 0.8, 0]}, {"bufferView": 1, "componentType": 5123, "count": 3, "type": "SCALAR"}],
    }
    encoded = json.dumps(document, separators=(",", ":")).encode("utf-8")
    encoded += b" " * (-len(encoded) % 4)
    length = 12 + 8 + len(encoded) + 8 + len(binary)
    return b"glTF" + struct.pack("<II", 2, length) + struct.pack("<II", len(encoded), 0x4E4F534A) + encoded + struct.pack("<II", len(binary), 0x004E4942) + binary


def test_three_glb_loads_and_seeks_deterministically(tmp_path):
    assets = tmp_path / "assets"
    assets.mkdir()
    (assets / "triangle.glb").write_bytes(_triangle_glb())
    scene = Scene(
        id="s1", size=(128, 128), fps=30, frames=2, background=Background(value="#000000"),
        elements=[Element(
            id="model", kind="3d", canonical=Canonical(width=96, height=96, model="assets/triangle.glb"), visible=(0, 1),
            tracks={"x": Track(keys=[Keyframe(t=0, v=64)]), "y": Track(keys=[Keyframe(t=0, v=64)])},
        )],
    )
    html = compose(scene, tmp_path, tmp_path / "composition.html")
    result = render(html, scene, tmp_path / "render", frames=[0, 1])
    assert result.hashes[0] == result.hashes[1]
    assert result.bboxes["model"][0] == [16.0, 16.0, 112.0, 112.0]
