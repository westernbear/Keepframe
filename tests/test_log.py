import logging
import time

from keepframe.jobs import JobStore
from keepframe.log import configure, get as get_log
from tests.test_web_server import get, start


def test_info_and_error_go_to_stdout(capsys):
    configure(force=True)
    log = get_log("keepframe.test")
    log.info("hello-info")
    log.error("hello-error")
    out = capsys.readouterr().out
    assert "INFO" in out and "hello-info" in out
    assert "ERROR" in out and "hello-error" in out


def test_http_access_is_logged(tmp_path, caplog):
    caplog.set_level(logging.INFO, logger="keepframe.web")
    srv = start(tmp_path)
    get(srv, "/")
    srv.shutdown()
    assert any("GET / HTTP" in r.message for r in caplog.records)


def test_job_error_is_logged(caplog):
    caplog.set_level(logging.ERROR, logger="keepframe.jobs")
    store = JobStore()

    def boom():
        raise RuntimeError("gpu missing")

    job = store.submit("x", fn=boom, project_id="p1")
    for _ in range(80):
        if job.status == "error":
            break
        time.sleep(0.05)
    assert job.status == "error"
    assert any(r.levelname == "ERROR" and "failed" in r.message for r in caplog.records)
