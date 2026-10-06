"""Exercise build stamping without Docker, signing or Git commits."""
import shutil
from pathlib import Path

import pytest

from scripts.zxp.stamp import stamp
from tests.ae_fake_runner import run_jsx


ROOT = Path(__file__).resolve().parents[1]


@pytest.mark.parametrize("dirty", ["", " M extension/js/core.js\n", "?? new-file\n"])
def test_stamp_changes_only_stage_and_reports_matching_runtime_builds(tmp_path, monkeypatch, dirty):
    stage = tmp_path / "stage"
    names = ["CSXS/manifest.xml", "js/core.js", "host/keepframe.jsx"]
    sources = {name: (ROOT / "extension" / name).read_bytes() for name in names}
    for name in names:
        destination = stage / name
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(ROOT / "extension" / name, destination)

    def git(command, *, text):
        assert command[:3] == ["git", "-C", str(ROOT)]
        assert text is True
        return {("rev-list", "--count", "HEAD"): "247\n",
                ("rev-parse", "--short", "HEAD"): "abc1234\n",
                ("status", "--porcelain"): dirty}[tuple(command[3:])]

    monkeypatch.setattr("scripts.zxp.stamp.subprocess.check_output", git)
    stamp(stage, ROOT)
    build = "247-abc1234" + ("-dirty" if dirty else "")
    manifest = (stage / "CSXS/manifest.xml").read_text()
    assert 'ExtensionBundleVersion="1.0.247"' in manifest
    assert '<Extension Id="com.keepframe.ae.panel" Version="1.0.247"' in manifest
    assert '<ExtensionManifest Version="11.0"' in manifest
    panel = (stage / "js/core.js").read_text()
    assert "const EXTENSION_VERSION = '1.0.247';" in panel
    assert f'HOST_BUILD = "{build}"' in panel
    # The actual ExtendScript entry point exposes the stamped value.
    monkeypatch.undo()
    assert run_jsx(tmp_path / "ae.json", stage / "host/keepframe.jsx", "kfInfo")["value"]["host_build"] == build
    assert sources == {name: (ROOT / "extension" / name).read_bytes() for name in names}
    script = (ROOT / "scripts/build_zxp.sh").read_text()
    assert script.index('python3 "$root/scripts/zxp/stamp.py" "$stage" "$root"') < script.index("docker build")


def test_stamp_refuses_source_directory():
    with pytest.raises(ValueError, match="outside the source"):
        stamp(ROOT / "extension", ROOT)
