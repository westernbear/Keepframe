from __future__ import annotations

import secrets
from typing import Any, Callable

from .runner import JobRunner, load_runner
from .spec import Job, JobSpec


class JobStore:
    def __init__(self, runner: JobRunner | None = None) -> None:
        self._jobs: dict[str, Job] = {}
        self._runner = runner if runner is not None else load_runner()

    def get(self, jid: str) -> Job:
        return self._jobs[jid]

    def find(self, jid: str) -> Job | None:
        return self._jobs.get(jid)

    def for_project(self, project_id: str, kind: str | None = None) -> Job | None:
        found = None
        for j in self._jobs.values():
            if j.project_id == project_id and (kind is None or j.kind == kind):
                found = j
        return found

    def list(self) -> list[Job]:
        return list(self._jobs.values())

    def submit(
        self,
        kind: str,
        fn: Callable[[], Any] | None = None,
        spec: JobSpec | None = None,
        **meta: Any,
    ) -> Job:
        j = Job(id="j" + secrets.token_hex(4), kind=kind, status="queued", **meta)
        self._jobs[j.id] = j
        self._runner.enqueue(j, spec=spec, fn=fn)
        return j
