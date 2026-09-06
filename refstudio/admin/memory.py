from __future__ import annotations

import csv
import io

from .models import AuditEvent, Member, QuarantineItem, Tenant


class MemoryAdmin:
    def __init__(self, seed: bool = True) -> None:
        self._tenants: dict[str, Tenant] = {}
        self._members: list[Member] = []
        self._jobs: list[dict] = []
        self._quarantine: list[QuarantineItem] = []
        self._audit: list[AuditEvent] = []
        if seed:
            self._seed()

    def _seed(self) -> None:
        self._tenants = {
            "org_northwind": Tenant(
                id="org_northwind",
                name="Northwind Studio",
                status="ok",
                members=6,
                analyze_min=41,
                analyze_quota_min=120,
                render_min=18,
                created="2025-03-01",
            ),
            "org_hanbit": Tenant(
                id="org_hanbit",
                name="Hanbit Motion",
                status="quota",
                members=2,
                analyze_min=120,
                analyze_quota_min=120,
                render_min=9,
                created="2025-06-15",
            ),
            "org_solo": Tenant(
                id="org_solo",
                name="Solo",
                status="ok",
                members=1,
                analyze_min=7,
                analyze_quota_min=30,
                render_min=2,
                created="2026-01-10",
            ),
            "org_closed": Tenant(
                id="org_closed",
                name="Closed shop",
                status="suspended",
                members=0,
                analyze_min=0,
                analyze_quota_min=0,
                render_min=0,
                created="2024-11-20",
            ),
        }
        self._members = [
            Member(email="mina@ref.studio", role="admin", tenant_id="org_northwind"),
            Member(email="jun@northwind", role="review_lead", tenant_id="org_northwind"),
            Member(email="lee@hanbit", role="maker", tenant_id="org_hanbit"),
            Member(email="solo@lee", role="maker", tenant_id="org_solo"),
        ]
        self._jobs = [
            {
                "id": "job_1842",
                "tenant": "Northwind",
                "kind": "analyze",
                "target": "Autumn drop s1",
                "status": "실행",
                "retries": 0,
                "gpu": "GPU 0",
            },
            {
                "id": "job_1841",
                "tenant": "Northwind",
                "kind": "render",
                "target": "Autumn drop v4",
                "status": "대기",
                "retries": 0,
                "gpu": "",
            },
            {
                "id": "job_1838",
                "tenant": "Hanbit",
                "kind": "correct",
                "target": "Paywall s1",
                "status": "실패",
                "retries": 4,
                "gpu": "",
            },
            {
                "id": "job_1837",
                "tenant": "Solo",
                "kind": "analyze",
                "target": "거부됨 실사",
                "status": "검역",
                "retries": 0,
                "gpu": "",
            },
        ]
        self._quarantine = [
            QuarantineItem(
                id="q_clip_street",
                filename="clip_street.mp4",
                tenant_id="org_solo",
                rejected_at="2026-09-06T09:12:00Z",
                reason="실사 푸티지. 평면 2D MG·UI 녹화만 받음.",
            ),
        ]
        self._audit = [
            AuditEvent(
                ts="14:02",
                actor="mina@ref.studio",
                action="테넌트 정지 시도",
                target="Closed shop",
                detail=None,
            ),
            AuditEvent(
                ts="13:41",
                actor="lee@hanbit",
                action="keep 해제 t_e3",
                target="Paywall s1",
                detail='술어: "제목은 바꿈"',
            ),
            AuditEvent(
                ts="13:18",
                actor="mina@ref.studio",
                action="멤버 추가",
                target="Northwind",
                detail="jun@northwind · 역할 검수 리드",
            ),
            AuditEvent(
                ts="12:55",
                actor="jun@northwind",
                action="내보내기 MP4+zip",
                target="Autumn drop v4",
                detail=None,
            ),
            AuditEvent(
                ts="09:12",
                actor="시스템",
                action="업로드 검역",
                target="Solo / clip_street.mp4",
                detail="실사",
            ),
        ]

    def list_tenants(self) -> list[Tenant]:
        return list(self._tenants.values())

    def get_tenant(self, id: str) -> Tenant:
        return self._tenants[id]

    def list_members(self, tenant_id: str) -> list[Member]:
        return [m for m in self._members if m.tenant_id == tenant_id]

    def suspend_tenant(self, id: str, actor: str) -> Tenant:
        tenant = self._tenants[id]
        updated = Tenant(
            id=tenant.id,
            name=tenant.name,
            status="suspended",
            members=tenant.members,
            analyze_min=tenant.analyze_min,
            analyze_quota_min=tenant.analyze_quota_min,
            render_min=tenant.render_min,
            created=tenant.created,
        )
        self._tenants[id] = updated
        self.append_audit(
            AuditEvent(
                ts="",
                actor=actor,
                action="테넌트 정지",
                target=tenant.name,
                detail=None,
            )
        )
        return updated

    def list_jobs(self) -> list[dict]:
        return list(self._jobs)

    def list_quarantine(self) -> list[QuarantineItem]:
        return list(self._quarantine)

    def add_quarantine(self, item: QuarantineItem) -> QuarantineItem:
        self._quarantine.append(item)
        return item

    def list_audit(self) -> list[AuditEvent]:
        return list(reversed(self._audit))

    def append_audit(self, event: AuditEvent) -> None:
        self._audit.append(event)

    def audit_csv(self) -> str:
        buf = io.StringIO()
        writer = csv.writer(buf)
        writer.writerow(["ts", "actor", "action", "target", "detail"])
        for event in self.list_audit():
            writer.writerow(
                [event.ts, event.actor, event.action, event.target, event.detail or ""]
            )
        return buf.getvalue()
