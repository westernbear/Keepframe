from __future__ import annotations

import importlib
import os
import threading
from typing import Any, Callable, Protocol

from keepframe.log import get
from keepframe.progress import bind_stage, remaining_eta, reset_stage

from .dispatch import run_job
from .spec import Job, JobSpec

log = get("keepframe.jobs")


class JobRunner(Protocol):
    """How a Job reaches a worker.

    Open-source default: ThreadRunner (same process).
    Cloud: implement enqueue() to put spec.to_json() on a queue; the worker
    process calls run_job(JobSpec.from_json(payload)). Do not fork analyze/render.
    """

    def enqueue(
        self,
        job: Job,
        *,
        spec: JobSpec | None = None,
        fn: Callable[[], Any] | None = None,
    ) -> None: ...


class ThreadRunner:
    """Local edition: daemon thread in the serve process."""

    def enqueue(
        self,
        job: Job,
        *,
        spec: JobSpec | None = None,
        fn: Callable[[], Any] | None = None,
    ) -> None:
        if spec is None and fn is None:
            raise ValueError("enqueue needs spec or fn")

        def work() -> None:
            job.status = "running"
            orig_eta = job.eta_s

            def on_stage(name: str, detail: str | None = None) -> None:
                job.stage = name
                job.detail = detail
                job.eta_s = remaining_eta(name, orig_eta)
                extra = f" {detail}" if detail else ""
                log.info("job %s stage=%s%s", job.id, name, extra)

            token = bind_stage(on_stage)
            log.info("job %s %s start project=%s", job.id, job.kind, job.project_id)
            try:
                job.result = (run_job(spec) if spec is not None else fn()) or {}
                job.status = "done"
                log.info("job %s %s done project=%s", job.id, job.kind, job.project_id)
            except Exception as e:
                job.status = "error"
                job.error = f"{type(e).__name__}: {e}"
                log.exception("job %s %s failed project=%s", job.id, job.kind, job.project_id)
            finally:
                reset_stage(token)

        def start() -> None:
            threading.Thread(target=work, daemon=True).start()

        threading.Timer(0, start).start()


def load_runner(name: str | None = None) -> JobRunner:
    """`thread` (default) or `package.module:Class` from KEEPFRAME_JOB_RUNNER."""
    name = name if name is not None else os.environ.get("KEEPFRAME_JOB_RUNNER", "thread")
    if name in ("", "thread", "local"):
        return ThreadRunner()
    module, sep, attr = name.partition(":")
    if not sep or not attr:
        raise ValueError("KEEPFRAME_JOB_RUNNER must be 'thread' or 'module:Class'")
    obj = getattr(importlib.import_module(module), attr)
    return obj() if isinstance(obj, type) else obj
