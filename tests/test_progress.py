import time

from keepframe.jobs import JobStore
from keepframe.progress import STAGES, bind_stage, remaining_eta, report_stage, reset_stage


def test_remaining_eta_shrinks_with_stage():
    assert remaining_eta("frames", 100) == 100
    assert remaining_eta("report", 100) == 10
    assert remaining_eta("unknown", 100) == 100


def test_report_stage_notifies_callback():
    seen = []
    token = bind_stage(lambda name, detail: seen.append((name, detail)))
    try:
        report_stage("text", "3/10")
    finally:
        reset_stage(token)
    assert seen == [("text", "3/10")]


def test_job_picks_up_pipeline_stage():
    store = JobStore()

    def work():
        report_stage("background")
        report_stage("text", "1/2")
        return {"ok": True}

    job = store.submit("analyze", fn=work, project_id="p1", eta_s=180)
    for _ in range(80):
        if store.get(job.id).status == "done":
            break
        time.sleep(0.02)
    got = store.get(job.id)
    assert got.status == "done"
    assert got.stage == "text"
    assert got.detail == "1/2"
    assert got.eta_s == remaining_eta("text", 180)
    assert "text" in STAGES
