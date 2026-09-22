from __future__ import annotations

import csv
import io
from datetime import datetime, timezone
from pathlib import Path

from keepframe.jobs import JobStore

from keepframe.session.provider import ProviderConfig, load_llm_settings, save_llm_settings

from .models import AuditEvent, Member, QuarantineItem, Tenant


class MemoryAdmin:
    def __init__(
        self,
        seed: bool = True,
        workspace: Path | None = None,
        job_store: JobStore | None = None,
    ) -> None:
        self._workspace = Path(workspace) if workspace is not None else None
        self._job_store = job_store
        self._tenants: dict[str, Tenant] = {}
        self._members: list[Member] = []
        self._jobs: list[dict] = []
        self._quarantine: list[QuarantineItem] = []
        self._audit: list[AuditEvent] = []
        self._llm_settings = load_llm_settings(self._workspace) or ProviderConfig()
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
            Member(email="mina@keepframe.app", role="admin", tenant_id="org_northwind"),
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
                ts="09:12",
                actor="시스템",
                action="업로드 검역",
                target="Solo / clip_street.mp4",
                detail="실사",
            ),
            AuditEvent(
                ts="12:55",
                actor="jun@northwind",
                action="내보내기 MP4+zip",
                target="Autumn drop v4",
                detail=None,
            ),
            AuditEvent(
                ts="13:18",
                actor="mina@keepframe.app",
                action="멤버 추가",
                target="Northwind",
                detail="jun@northwind · 역할 검수 리드",
            ),
            AuditEvent(
                ts="13:41",
                actor="lee@hanbit",
                action="keep 해제 t_e3",
                target="Paywall s1",
                detail='술어: "제목은 바꿈"',
            ),
            AuditEvent(
                ts="14:02",
                actor="mina@keepframe.app",
                action="테넌트 정지 시도",
                target="Closed shop",
                detail=None,
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
                ts=datetime.now(timezone.utc).strftime("%H:%M"),
                actor=actor,
                action="테넌트 정지",
                target=tenant.name,
                detail=None,
            )
        )
        return updated

    def list_jobs(self) -> list[dict]:
        if self._job_store is None:
            return list(self._jobs)
        statuses = {"queued": "대기", "running": "실행", "done": "완료", "error": "실패"}
        live: dict[str, dict] = {}
        for job in self._job_store.list():
            target = job.project_id + (f" {job.scene_id}" if job.scene_id else "")
            live[job.id] = {
                "id": job.id,
                "tenant": "Local",
                "kind": job.kind,
                "target": target,
                "status": statuses.get(job.status, job.status),
                "retries": 0,
                "gpu": "",
                "error": job.error,
            }
        rows = [live.pop(row["id"], row) for row in self._jobs]
        rows.extend(live.values())
        return rows

    def list_quarantine(self) -> list[QuarantineItem]:
        return list(self._quarantine)

    def add_quarantine(self, item: QuarantineItem) -> QuarantineItem:
        self._quarantine.append(item)
        return item

    def list_audit(self) -> list[AuditEvent]:
        return list(reversed(self._audit))

    def append_audit(self, event: AuditEvent) -> None:
        self._audit.append(event)

    def get_llm_settings(self) -> ProviderConfig:
        return self._llm_settings

    def set_llm_settings(self, config: ProviderConfig, actor: str) -> ProviderConfig:
        self._llm_settings = config
        if self._workspace is not None:
            save_llm_settings(self._workspace, config)
        self.append_audit(
            AuditEvent(
                ts=datetime.now(timezone.utc).strftime("%H:%M"),
                actor=actor,
                action="LLM 프로바이더 변경",
                target=f"{config.provider}/{config.model}",
                detail=None,
            )
        )
        return config

    def audit_csv(self) -> str:
        buf = io.StringIO()
        writer = csv.writer(buf)
        writer.writerow(["ts", "actor", "action", "target", "detail"])
        for event in self.list_audit():
            writer.writerow(
                [event.ts, event.actor, event.action, event.target, event.detail or ""]
            )
        return buf.getvalue()
