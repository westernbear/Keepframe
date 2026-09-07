from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

TenantStatus = Literal["ok", "quota", "suspended"]
MemberRole = Literal["admin", "billing", "review_lead", "maker"]


@dataclass
class Tenant:
    id: str
    name: str
    status: TenantStatus
    members: int
    analyze_min: int
    analyze_quota_min: int
    render_min: int
    created: str


@dataclass
class Member:
    email: str
    role: MemberRole
    tenant_id: str


@dataclass
class QuarantineItem:
    id: str
    filename: str
    tenant_id: str
    rejected_at: str
    reason: str


@dataclass
class AuditEvent:
    ts: str
    actor: str
    action: str
    target: str
    detail: str | None = None
