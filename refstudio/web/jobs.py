from __future__ import annotations

import secrets
import threading
import traceback
from dataclasses import asdict, dataclass
from typing import Any, Callable


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


class JobStore:
    def __init__(self) -> None:
        self._jobs: dict[str, Job] = {}
        self._lock = threading.Lock()

    def get(self, jid: str) -> Job:
        return self._jobs[jid]

    def list(self) -> list[Job]:
        return list(self._jobs.values())

    def submit(self, kind: str, fn: Callable[[], Any], **meta) -> Job:
        j = Job(id="j" + secrets.token_hex(4), kind=kind, status="queued", **meta)
        self._jobs[j.id] = j

        def work():
            j.status = "running"
            try:
                j.result = fn() or {}
                j.status = "done"
            except Exception as e:
                j.status = "error"
                j.error = f"{type(e).__name__}: {e}"
                traceback.print_exc()

        def start():
            threading.Thread(target=work, daemon=True).start()

        threading.Timer(0, start).start()
        return j
