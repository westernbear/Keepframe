import pytest
from keepframe.admin.memory import MemoryAdmin
from keepframe.admin.models import AuditEvent, QuarantineItem
from keepframe.admin.policy import RETRY_CAP

def test_seed_tenants_and_no_delete():
    svc = MemoryAdmin()
    ids = [t.id for t in svc.list_tenants()]
    assert "org_northwind" in ids and "org_closed" in ids
    assert not hasattr(svc, "delete_audit")
    n = len(svc.list_audit())
    svc.append_audit(AuditEvent(ts="15:00", actor="mina@keepframe.app", action="멤버 추가", target="Northwind", detail="x"))
    assert len(svc.list_audit()) == n + 1
    assert svc.list_audit()[0].action == "멤버 추가"


def test_seed_audit_is_newest_first():
    svc = MemoryAdmin()
    actions = [e.action for e in svc.list_audit()]
    assert actions[0] == "테넌트 정지 시도"
    assert actions[-1] == "업로드 검역"

def test_quarantine_has_no_video_field():
    item = QuarantineItem(id="q1", filename="a.mp4", tenant_id="org_solo", rejected_at="2026-09-06T09:12:00Z",
                          reason="실사. 평면 2D MG·UI 녹화만.")
    assert not hasattr(item, "video")
    assert "mp4" not in item.__dataclass_fields__ or "video" not in item.__dataclass_fields__

def test_job_retries_cannot_exceed_cap_in_seed():
    svc = MemoryAdmin()
    for j in svc.list_jobs():
        assert j["retries"] <= RETRY_CAP
def test_live_jobs_merge_seed_and_track_shared_store():
    from keepframe.jobs import JobStore

    class ControlledRunner:
        def enqueue(self, job, *, spec=None, fn=None):
            pass

    store = JobStore(runner=ControlledRunner())
    svc = MemoryAdmin(job_store=store)
    seed_ids = {row["id"] for row in svc.list_jobs()}
    job = store.submit("analyze", project_id="proj", scene_id="s1")
    rows = {row["id"]: row for row in svc.list_jobs()}
    assert seed_ids <= rows.keys()
    assert rows[job.id]["status"] == "대기"
    job.status = "running"
    assert {row["id"]: row for row in svc.list_jobs()}[job.id]["status"] == "실행"
    job.status = "done"
    assert {row["id"]: row for row in svc.list_jobs()}[job.id]["status"] == "완료"
    job.status = "error"
    job.error = "boom"
    row = {row["id"]: row for row in svc.list_jobs()}[job.id]
    assert row["status"] == "실패" and row["error"] == "boom"


def test_live_job_replaces_seed_id():
    from keepframe.jobs import Job, JobStore

    class ControlledRunner:
        def enqueue(self, job, *, spec=None, fn=None):
            pass

    store = JobStore(runner=ControlledRunner())
    svc = MemoryAdmin(job_store=store)
    store._jobs["job_1842"] = Job("job_1842", "export", "queued", "proj")
    rows = [row for row in svc.list_jobs() if row["id"] == "job_1842"]
    assert len(rows) == 1 and rows[0]["tenant"] == "Local" and rows[0]["kind"] == "export"
