from pathlib import Path

from keepframe.after_effects import connector


def test_run_connector_reports_missing_token(monkeypatch, capsys):
    monkeypatch.delenv("KEEPFRAME_AE_RELAY_TOKEN", raising=False)
    assert connector.run_connector("https://relay.example", code=None, project="demo") == 1
    err = capsys.readouterr().err
    assert "ae-connect failed: deployment credential is unavailable" in err
    assert "KEEPFRAME_AE_RELAY_TOKEN" in err


def test_failure_text_never_echoes_the_token(monkeypatch):
    monkeypatch.setenv("KEEPFRAME_AE_RELAY_TOKEN", "s3cret-token-value")
    text = connector._connector_failure_text(connector.ConnectorError("bad s3cret-token-value"))
    assert "s3cret-token-value" not in text and "[redacted]" in text


def test_panel_failure_has_a_hint():
    text = connector._connector_failure_text(connector.ConnectorError("After Effects panel is not ready"))
    assert "Keepframe Panel" in text


def test_agent_pairing_values_have_copy_buttons():
    html = Path("keepframe/web/static/agent.html").read_text(encoding="utf-8")
    js = Path("keepframe/web/static/js/agent.js").read_text(encoding="utf-8")
    assert 'data-copy-target="render-pairing-code"' in html
    assert 'data-copy-target="render-pairing-command"' in html
    assert "setTextIfChanged(renderPairingCodeEl" in js
    assert 'document.execCommand("copy")' in js


def test_private_root_grants_the_sid_with_star_prefix(tmp_path):
    calls = []
    root = connector.ensure_private_root(
        tmp_path / "bridge",
        sid_provider=lambda: "S-1-5-21-1-2-3-1001",
        acl_runner=lambda argv, **kw: calls.append(argv),
        windows=lambda: True,
    )
    assert root == tmp_path / "bridge"
    grant = calls[1]
    assert grant[0].lower().endswith("icacls.exe")
    assert "*S-1-5-21-1-2-3-1001:(OI)(CI)F" in grant


def test_private_root_failure_includes_icacls_detail(tmp_path):
    import subprocess
    import pytest

    def fail(argv, **kw):
        raise subprocess.CalledProcessError(1, argv, stderr="No mapping between account names and security IDs was done.")

    with pytest.raises(connector.PreflightError) as err:
        connector.ensure_private_root(tmp_path / "b", sid_provider=lambda: "S-1-5-21-1", acl_runner=fail, windows=lambda: True)
    assert "No mapping between account names" in str(err.value)
