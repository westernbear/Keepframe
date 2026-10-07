import json
import shutil

import pytest

from tests.ae_fake_runner import run_jsx


def test_run_jsx(tmp_path):
    script = tmp_path / "tiny.jsx"
    script.write_text('function sync(name) {\n'
                      ' if (!app.project.items.length) app.project.items.addComp(name, 10, 10, 1, 5, 30);\n'
                      ' return \'{"name":"\' + app.project.items[1].name + \'"}\';\n'
                      '}\nfunction plain() { return "plain"; }\n'
                      'function fail() { throw Error("oops"); }\n')
    state = tmp_path / "state.json"
    result = run_jsx(state, script, "sync", "first", documents=tmp_path)
    assert result == {"result": '{"name":"first"}', "value": {"name": "first"},
                      "undo_groups": 0, "writes": 1, "calls": {}}
    assert json.loads(state.read_text())["project"]["items"][0]["name"] == "first"
    assert run_jsx(state, script, "sync", "second")["value"] == {"name": "first"}
    assert "value" not in run_jsx(state, script, "plain")
    assert run_jsx(state, script, "fail")["error"] == "oops"


def test_missing_node_skips(tmp_path, monkeypatch):
    monkeypatch.setattr(shutil, "which", lambda _: None)
    with pytest.raises(pytest.skip.Exception, match="node"):
        run_jsx(tmp_path / "state.json", tmp_path / "tiny.jsx", "sync")
