"""Exercise build stamping without Docker, signing or Git commits."""
import shutil
from datetime import datetime, timezone
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
    monkeypatch.setattr("time.time_ns", lambda: 1791289845123000000)
    stamp(stage, ROOT)
    build = "1.20261006.123045123-abc1234" + ("-dirty" if dirty else "")
    manifest = (stage / "CSXS/manifest.xml").read_text()
    assert 'ExtensionBundleVersion="1.20261006.123045123"' in manifest
    assert '<Extension Id="com.keepframe.ae.panel" Version="1.20261006.123045123"' in manifest
    assert '<ExtensionManifest Version="11.0"' in manifest
    panel = (stage / "js/core.js").read_text()
    assert "const EXTENSION_VERSION = '1.20261006.123045123';" in panel
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


def test_final_build_versions_advance_with_time_even_when_commit_count_drops_or_tree_stays_dirty(tmp_path, monkeypatch):
    count = ["247"]
    monkeypatch.setattr("scripts.zxp.stamp.subprocess.check_output", lambda cmd, **kw: {
        ("rev-list", "--count", "HEAD"): count[0],
        ("rev-parse", "--short", "HEAD"): "abc1234",
        ("status", "--porcelain"): " M extension/js/core.js",
    }[tuple(cmd[3:])])
    versions = []
    dates = [datetime(2026, 10, 6, 23, 59, 59, 998000, tzinfo=timezone.utc),
             datetime(2026, 10, 6, 23, 59, 59, 999000, tzinfo=timezone.utc),
             datetime(2026, 10, 7, 0, 0, 0, tzinfo=timezone.utc)]
    for i, date in enumerate(dates):
        stage = tmp_path / f"stage{i}"
        shutil.copytree(ROOT / "extension", stage)
        count[0] = "247" if i == 0 else "1"
        monkeypatch.setattr("time.time_ns", lambda date=date: int(date.timestamp() * 1000) * 1_000_000)
        stamp(stage, ROOT)
        xml = (stage / "CSXS/manifest.xml").read_text()
        import re
        version = tuple(map(int, re.search(r'ExtensionBundleVersion="([^"]+)"', xml)[1].split(".")))
        assert version > (1, 0, 238)
        assert all(0 <= part <= 2147483647 for part in version)
        versions.append(version)
    assert versions[0] < versions[1] < versions[2]


def test_stamp_keeps_the_source_major_and_sorts_after_older_builds(tmp_path, monkeypatch):
    """The date stamp keeps the source's protocol major; panel features are declared by X-Keepframe-Spec-Level,
    since a build must sort above every installed date-stamped build (1.20261007.x) to replace it."""
    import re
    monkeypatch.setattr("scripts.zxp.stamp.subprocess.check_output", lambda cmd, **kw: {
        ("rev-parse", "--short", "HEAD"): "abc1234", ("status", "--porcelain"): ""}[tuple(cmd[3:])])
    monkeypatch.setattr("time.time_ns", lambda: 1791289845123000000)
    versions = {}
    for major in ("1.1.0", "2.0.0"):
        stage = tmp_path / major
        shutil.copytree(ROOT / "extension", stage)
        for name in ("CSXS/manifest.xml", "js/core.js"):
            path = stage / name
            path.write_text(path.read_text().replace("1.1.0", major))
        stamp(stage, ROOT)
        versions[major] = re.search(r"const EXTENSION_VERSION = '([^']+)';", (stage / "js/core.js").read_text())[1]
    assert versions == {"1.1.0": "1.20261006.123045123", "2.0.0": "2.20261006.123045123"}
    assert tuple(map(int, versions["1.1.0"].split("."))) > (1, 20261007 - 1, 95826219)
    assert "const SPEC_LEVEL = 2;" in (ROOT / "extension/js/core.js").read_text()
