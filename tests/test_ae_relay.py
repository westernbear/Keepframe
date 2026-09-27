import os
from threading import Thread
from urllib.error import HTTPError
from urllib.parse import urlencode
from urllib.request import urlopen

import pytest

from keepframe.after_effects.relay import make_relay_server
from tests.test_ae_coordinator import _coordinator


def _serve(workspace):
    server = make_relay_server(workspace)
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server, thread


def _get(server, path, query=None):
    suffix = f"?{urlencode(query)}" if query else ""
    return urlopen(f"http://127.0.0.1:{server.server_address[1]}{path}{suffix}")


def test_relay_rejects_nonfinite_wait_and_private_routes(tmp_path):
    workspace = tmp_path / "ws"
    _, plan, _, _ = _coordinator(workspace)
    server, thread = _serve(workspace)
    try:
        with pytest.raises(HTTPError) as invalid_wait:
            _get(
                server,
                "/next",
                {"project": "p1", "plan": plan.id, "device": "device-1", "wait": "nan"},
            )
        assert invalid_wait.value.code == 409
        with pytest.raises(HTTPError) as private_route:
            _get(server, "/api/projects")
        assert private_route.value.code == 404
    finally:
        server.shutdown()
        server.server_close()
        thread.join()


def test_relay_rejects_project_symlink_outside_workspace(tmp_path):
    external_root, plan, _, _ = _coordinator(tmp_path / "outside")
    workspace = tmp_path / "ws"
    workspace.mkdir()
    try:
        os.symlink(external_root, workspace / "p1", target_is_directory=True)
    except OSError as exc:
        pytest.skip(f"directory symlink unavailable: {exc}")

    server, thread = _serve(workspace)
    try:
        with pytest.raises(HTTPError) as escaped:
            _get(
                server,
                "/next",
                {"project": "p1", "plan": plan.id, "device": "device-1"},
            )
        assert escaped.value.code == 409
    finally:
        server.shutdown()
        server.server_close()
        thread.join()
