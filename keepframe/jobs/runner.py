from __future__ import annotations

import importlib
import os
import threading
import traceback
from typing import Any, Callable, Protocol

from .dispatch import run_job
from .spec import Job, JobSpec


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
            try:
                job.result = (run_job(spec) if spec is not None else fn()) or {}
                job.status = "done"
            except Exception as e:
                job.status = "error"
                job.error = f"{type(e).__name__}: {e}"
                traceback.print_exc()

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
