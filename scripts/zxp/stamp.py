"""Stamp the staged extension only; run before ZXP signing."""
import re
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path


def stamp(stage, repo):
    stage, repo = Path(stage).resolve(), Path(repo).resolve()
    if stage == repo or repo in stage.parents:
        raise ValueError("stage must be outside the source repository")

    def git(*args):
        return subprocess.check_output(["git", "-C", str(repo), *args], text=True).strip()

    # The source version (bumped when the AE spec gains kinds or fields) is the placeholder a build replaces.
    source = re.search(r"const EXTENSION_VERSION = '(([0-9]+)\.[0-9]+\.[0-9]+)';", (stage / "js/core.js").read_text(encoding="utf-8"))
    if source is None:
        raise ValueError("unexpected build placeholder in js/core.js")
    placeholder, major = source[1], source[2]
    now = time.time_ns()
    date = datetime.fromtimestamp(now // 1_000_000_000, timezone.utc)
    # UTC date and millisecond time advance independently of branch history, including dirty builds: every build
    # sorts above the installed one (an equal or older version does not replace it). The minor therefore cannot
    # carry features; the panel declares what it draws in X-Keepframe-Spec-Level. The protocol major is kept.
    version = f"{major}.{date:%Y%m%d}.{int(date.strftime('%H%M%S')) * 1000 + now // 1_000_000 % 1000}"
    build = version + "-" + git("rev-parse", "--short", "HEAD")
    if git("status", "--porcelain"):
        build += "-dirty"
    replacements = {
        "CSXS/manifest.xml": [(f'Version="{placeholder}"', f'Version="{version}"', 2)],
        "js/core.js": [(f"const EXTENSION_VERSION = '{placeholder}';", f"const EXTENSION_VERSION = '{version}';", 1),
                       ('HOST_BUILD = "dev"', f'HOST_BUILD = "{build}"', 1)],
        "host/keepframe.jsx": [('HOST_BUILD = "dev"', f'HOST_BUILD = "{build}"', 1)],
    }
    for name, changes in replacements.items():
        path = stage / name
        text = path.read_text(encoding="utf-8")
        for old, new, expected in changes:
            if text.count(old) != expected:
                raise ValueError(f"unexpected build placeholder in {name}")
            text = text.replace(old, new)
        path.write_text(text, encoding="utf-8")
    print(f"extension {version} · build {build}")


if __name__ == "__main__":
    stamp(*sys.argv[1:])
