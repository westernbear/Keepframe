import pytest
from refstudio.admin.memory import MemoryAdmin
from refstudio.admin.models import AuditEvent, QuarantineItem
from refstudio.admin.policy import RETRY_CAP

def test_seed_tenants_and_no_delete():
    svc = MemoryAdmin()
    ids = [t.id for t in svc.list_tenants()]
    assert "org_northwind" in ids and "org_closed" in ids
    assert not hasattr(svc, "delete_audit")
    n = len(svc.list_audit())
    svc.append_audit(AuditEvent(ts="15:00", actor="mina@keepframe.app", action="멤버 추가", target="Northwind", detail="x"))
    assert len(svc.list_audit()) == n + 1
    assert svc.list_audit()[0].action == "멤버 추가"

def test_quarantine_has_no_video_field():
    item = QuarantineItem(id="q1", filename="a.mp4", tenant_id="org_solo", rejected_at="2026-09-06T09:12:00Z",
                          reason="실사 푸티지. 평면 2D MG·UI 녹화만 받음.")
    assert not hasattr(item, "video")
    assert "mp4" not in item.__dataclass_fields__ or "video" not in item.__dataclass_fields__

def test_job_retries_cannot_exceed_cap_in_seed():
    svc = MemoryAdmin()
    for j in svc.list_jobs():
        assert j["retries"] <= RETRY_CAP
