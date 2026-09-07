from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any


@dataclass
class Job:
    id: str
    kind: str
    status: str
    project_id: str
    scene_id: str | None = None
    stage: str | None = None
    error: str | None = None
    eta_s: int | None = None
    result: dict | None = None

    def to_json(self) -> dict:
        return asdict(self)


@dataclass
class JobSpec:
    """JSON-serializable work unit. Cloud queues this; workers call run_job(spec)."""

    kind: str
    args: dict[str, Any] = field(default_factory=dict)

    def to_json(self) -> dict[str, Any]:
        return {"kind": self.kind, "args": dict(self.args)}

    @classmethod
    def from_json(cls, data: dict[str, Any]) -> JobSpec:
        return cls(kind=str(data["kind"]), args=dict(data.get("args") or {}))
