import time
from keepframe.web.jobs import JobStore


def test_for_project_returns_latest_analyze_job():
    store = JobStore()
    store.submit("analyze", lambda: {}, project_id="p1")
    later = store.submit("analyze", lambda: {}, project_id="p1")
    store.submit("analyze", lambda: {}, project_id="p2")
    assert store.for_project("p1", "analyze").id == later.id
    assert store.find(later.id) is later
    assert store.find("missing") is None


def test_job_runs_and_finishes():
    store = JobStore()
    j = store.submit("analyze", lambda: {"ok": True}, project_id="p1")
    assert j.status in ("queued", "running", "done")
    for _ in range(50):
        if store.get(j.id).status == "done":
            break
        time.sleep(0.02)
    got = store.get(j.id)
    assert got.status == "done" and got.result == {"ok": True}


def test_job_surfaces_error():
    store = JobStore()
    def boom():
        raise RuntimeError("gpu missing")
    j = store.submit("analyze", boom, project_id="p1")
    for _ in range(50):
        if store.get(j.id).status == "error":
            break
        time.sleep(0.02)
    assert "gpu missing" in store.get(j.id).error
