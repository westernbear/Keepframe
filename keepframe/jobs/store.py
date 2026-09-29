from __future__ import annotations

import secrets
import threading
from typing import Any, Callable

from .runner import JobRunner, load_runner
from .spec import Job, JobSpec


class JobStore:
    def __init__(self, runner: JobRunner | None = None) -> None:
        self._jobs: dict[str, Job] = {}
        self._runner = runner if runner is not None else load_runner()
        self._lock = threading.RLock()

    def get(self, jid: str) -> Job:
        with self._lock:
            return self._jobs[jid]

    def find(self, jid: str) -> Job | None:
        with self._lock:
            return self._jobs.get(jid)

    def for_project(self, project_id: str, kind: str | None = None) -> Job | None:
        with self._lock:
            found = None
            for j in self._jobs.values():
                if j.project_id == project_id and (kind is None or j.kind == kind):
                    found = j
            return found

    def list(self) -> list[Job]:
        with self._lock:
            return list(self._jobs.values())

    def submit(
        self,
        kind: str,
        fn: Callable[[], Any] | None = None,
        spec: JobSpec | None = None,
        *,
        job_id: str | None = None,
        **meta: Any,
    ) -> Job:
        with self._lock:
            if job_id is not None:
                if not isinstance(job_id, str) or not job_id:
                    raise ValueError("job_id must be a non-empty string")
                existing = self._jobs.get(job_id)
                if existing is not None:
                    return existing
            jid = job_id or "j" + secrets.token_hex(4)
            j = Job(id=jid, kind=kind, status="queued", **meta)
            self._jobs[j.id] = j
            try:
                self._runner.enqueue(j, spec=spec, fn=fn)
            except Exception:
                if self._jobs.get(j.id) is j:
                    del self._jobs[j.id]
                raise
            return j
