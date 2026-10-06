"""Run real JSX against the strict Node fake; preserve the CLI error payload."""
import json
from pathlib import Path
import shutil
import subprocess


def run_jsx(state_path, script_path, entry, *args, documents=None) -> dict:
    node = shutil.which("node")
    if node is None:
        import pytest
        pytest.skip("node is not installed")
    command = [node, str(Path(__file__).parent / "ae_fake" / "run.js"),
               str(state_path), str(script_path), entry, *map(str, args)]
    if documents is not None:
        command += ["--documents", str(documents)]
    completed = subprocess.run(command, capture_output=True, text=True)
    payload = json.loads(completed.stdout)
    if isinstance(payload.get("result"), str):
        try:
            payload["value"] = json.loads(payload["result"])
        except ValueError:
            pass
    return payload
