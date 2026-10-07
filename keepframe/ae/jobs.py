"""Persistent per-device After Effects jobs with dependency-aware long-polling."""

import copy
import json
import math
import os
import secrets
import tempfile
import threading
import time
from dataclasses import asdict, dataclass, field, replace
from pathlib import Path

from ..log import get


log = get("keepframe.ae.jobs")


_KINDS = ("sync", "render_frames", "render_final", "package")
_TERMINAL = ("done", "failed", "superseded")
_HAND_EDITED = "AE layers were edited by hand; resend with overwrite"


def _timestamp(now):
    now = time.time() if now is None else now
    if type(now) not in (int, float) or not math.isfinite(now):
        raise ValueError("invalid timestamp")
    return now


def _dictionary(value, name):
    if not isinstance(value, dict):
        raise ValueError(f"invalid {name}: expected a JSON dictionary")
    try:
        return json.loads(json.dumps(value, allow_nan=False))
    except (TypeError, ValueError, OverflowError):
        raise ValueError(f"invalid {name}: expected a JSON dictionary") from None


@dataclass
class Job:
    id: str
    device: str
    kind: str
    project: str
    scene: str
    version: str
    params: dict = field(default_factory=dict)
    state: str = "queued"
    error: str | None = None
    result: dict | None = None
    created: float = field(default_factory=time.time)
    started: float | None = None
    finished: float | None = None
    depends_on: str | None = None

    def to_dict(self) -> dict:
        return asdict(self)


class Jobs:
    def __init__(self, workspace):
        self._path = Path(workspace) / ".ae" / "jobs"
        self._condition = threading.Condition(threading.Lock())
        self._jobs = {}
        with self._condition:
            self._path.mkdir(mode=0o700, parents=True, exist_ok=True)
            self._path.chmod(0o700)
            self._load()

    def _save(self, job):
        fd, temporary = tempfile.mkstemp(dir=self._path, prefix=".job.", suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as stream:
                json.dump(job.to_dict(), stream, allow_nan=False, separators=(",", ":"))
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, self._path / f"{job.id}.json")
        finally:
            Path(temporary).unlink(missing_ok=True)
        self._jobs[job.id] = job

    def _load(self):
        for path in self._path.glob("*.json"):
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
                if not isinstance(data, dict):
                    raise ValueError("invalid job record")
                job = Job(**{key: value for key, value in data.items() if key in Job.__dataclass_fields__})
                if (path.name != f"{job.id}.json" or len(job.id) != 18
                        or not job.id.startswith("j_")
                        or any(c not in "0123456789abcdef" for c in job.id[2:])
                        or job.kind not in _KINDS or job.state not in ("queued", "running", *_TERMINAL)
                        or job.created is None
                        or (job.depends_on is not None and not isinstance(job.depends_on, str))
                        or (job.state in _TERMINAL and job.finished is None)
                        or (job.state == "running" and job.started is None)):
                    raise ValueError("invalid job record")
                for name in ("device", "project", "scene", "version"):
                    if not isinstance(getattr(job, name), str) or not getattr(job, name):
                        raise ValueError("invalid job identity")
                for value in (job.created, job.started, job.finished):
                    if value is not None:
                        _timestamp(value)
                _dictionary(job.params, "params")
                if job.result is not None:
                    _dictionary(job.result, "result")
                if job.state == "failed" and (not isinstance(job.error, str) or not job.error):
                    raise ValueError("failed job has no error")
            except (TypeError, ValueError, UnicodeDecodeError) as exc:
                path.rename(path.with_suffix(".corrupt"))
                log.warning("Quarantined invalid job file %s: %s", path.name, exc)
                continue
            self._jobs[job.id] = job
        now = _timestamp(None)
        for job in list(self._jobs.values()):
            if job.state == "running":
                self._end(job, False, None, "server restarted", now)
        for job in list(self._jobs.values()):
            self._fail_dependents(job, now)
        expired = {job.id for job in self._jobs.values()
                   if job.state in _TERMINAL and job.finished < now - 7 * 86400}
        for job in list(self._jobs.values()):
            if job.state == "queued" and job.depends_on is not None:
                if job.depends_on not in self._jobs:
                    self._end(job, False, None, "sync failed: prerequisite is missing", now)
                elif job.depends_on in expired:
                    self._save(replace(job, depends_on=None))
        for job_id in expired:
            (self._path / f"{job_id}.json").unlink()
            del self._jobs[job_id]

    def _fail_dependents(self, job, now):
        if job.kind != "sync":
            return []
        if job.state == "failed":
            error = f"sync failed: {job.error}"
        elif job.state == "superseded":
            error = "sync failed: prerequisite was superseded"
        elif job.state == "done" and (job.result or {}).get("applied") is False:
            error = _HAND_EDITED
        else:
            return []
        failed = []
        for dependent in list(self._jobs.values()):
            if dependent.state == "queued" and dependent.depends_on == job.id:
                updated = replace(dependent, state="failed", error=error, finished=now)
                self._save(updated)
                failed.append(updated)
        return failed

    def _end(self, job, ok, result, error, now):
        updated = replace(job, state="done" if ok else "failed", result=result,
                          error=None if ok else error, finished=now)
        self._save(updated)
        return [updated, *self._fail_dependents(updated, now)]

    def _last_synced(self, device, project, scene):
        synced = [job for job in self._jobs.values()
                  if (job.device, job.project, job.scene) == (device, project, scene)
                  and job.kind == "sync" and job.state == "done"
                  and (job.result or {}).get("applied") is True]
        return max(synced, key=lambda job: (job.finished, job.created)).version if synced else None

    def _enqueue(self, device, kind, project, scene, version, params, now):
        dependency = None
        if kind != "sync" and self._last_synced(device, project, scene) != version:
            dependency = self._enqueue(device, "sync", project, scene, version, {}, now).id
        job = Job("j_" + secrets.token_hex(8), device, kind, project, scene, version,
                  params=params, created=now, depends_on=dependency)
        self._save(job)
        if kind == "sync":
            superseded = [old for old in self._jobs.values()
                          if old.id != job.id and old.kind == "sync" and old.state == "queued"
                          and (old.device, old.project, old.scene) == (device, project, scene)]
            old_ids = {old.id for old in superseded}
            for old in superseded:
                self._save(replace(old, state="superseded", finished=now))
            for dependent in list(self._jobs.values()):
                if dependent.state == "queued" and dependent.depends_on in old_ids:
                    self._save(replace(dependent, depends_on=job.id))
        return job

    def enqueue(self, device, kind, project, scene, version, params=None, now=None) -> Job:
        if kind not in _KINDS:
            raise ValueError("invalid job kind")
        for name, value in (("device", device), ("project", project), ("scene", scene), ("version", version)):
            if not isinstance(value, str) or not value:
                raise ValueError(f"invalid {name}")
        params = _dictionary({} if params is None else params, "params")
        with self._condition:
            try:
                return copy.deepcopy(self._enqueue(device, kind, project, scene, version, params, _timestamp(now)))
            finally:
                self._condition.notify_all()

    def next(self, device, wait=25.0) -> Job | None:
        if type(wait) not in (int, float) or not math.isfinite(wait) or wait < 0:
            raise ValueError("invalid wait")
        deadline = time.monotonic() + wait
        with self._condition:
            while True:
                # ponytail: scan retained jobs; add indexes if seven-day queues make polling slow.
                if not any(job.device == device and job.state == "running" for job in self._jobs.values()):
                    for job in sorted(self._jobs.values(), key=lambda job: job.created):
                        if job.device != device or job.state != "queued":
                            continue
                        dependency = self._jobs.get(job.depends_on)
                        if job.depends_on is not None and dependency is None:
                            self._end(job, False, None, "sync failed: prerequisite is missing", _timestamp(None))
                            continue
                        if dependency is not None:
                            self._fail_dependents(dependency, _timestamp(None))
                            job = self._jobs[job.id]
                        if (job.state == "queued"
                                and (job.depends_on is None or (dependency and dependency.state == "done"))):
                            running = replace(job, state="running", started=_timestamp(None))
                            self._save(running)
                            return copy.deepcopy(running)
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return None
                self._condition.wait(remaining)

    def finish(self, job_id, ok, result=None, error=None, now=None) -> Job:
        if not isinstance(ok, bool):
            raise ValueError("invalid ok: expected a boolean")
        if not ok and (not isinstance(error, str) or not error.strip() or len(error) > 2000):
            raise ValueError("invalid error: required, at most 2000 characters")
        result = None if result is None else _dictionary(result, "result")
        with self._condition:
            job = self._jobs.get(job_id)
            if job is None or job.state != "running":
                raise ValueError("job must be running")
            try:
                return copy.deepcopy(self._end(job, ok, result, error, _timestamp(now))[0])
            finally:
                self._condition.notify_all()

    def sweep(self, last_seen: dict[str, float], now=None) -> list[Job]:
        with self._condition:
            now, failed = _timestamp(now), []
            try:
                for job in list(self._jobs.values()):
                    seen = last_seen.get(job.device)
                    if job.state == "running" and (seen is None or now - seen >= 60):
                        failed.extend(self._end(job, False, None, "AE disconnected", now))
                return copy.deepcopy(failed)
            finally:
                self._condition.notify_all()

    def abandon(self, device, reason, now=None, *, queued=False) -> list[Job]:
        if not isinstance(reason, str) or not reason.strip() or len(reason) > 2000:
            raise ValueError("invalid error: required, at most 2000 characters")
        with self._condition:
            now, failed = _timestamp(now), []
            try:
                for job in list(self._jobs.values()):
                    if job.device == device and job.state in (("queued", "running") if queued else ("running",)):
                        # Disconnect every active job with the same reason, including sync dependents.
                        if queued:
                            updated = replace(job, state="failed", result=None, error=reason, finished=now)
                            self._save(updated)
                            failed.append(updated)
                        else:
                            failed.extend(self._end(job, False, None, reason, now))
                return copy.deepcopy(failed)
            finally:
                self._condition.notify_all()

    def get(self, job_id) -> Job | None:
        with self._condition:
            return copy.deepcopy(self._jobs.get(job_id))

    def latest(self, project, scene, kind, *, version=None) -> Job | None:
        with self._condition:
            jobs = (job for job in self._jobs.values()
                    if (job.project, job.scene, job.kind) == (project, scene, kind)
                    and (version is None or job.version == version))
            return copy.deepcopy(max(jobs, key=lambda job: job.created, default=None))

    def last_synced(self, device, project, scene) -> str | None:
        with self._condition:
            return self._last_synced(device, project, scene)

    def state(self, project, scene) -> dict:
        with self._condition:
            jobs = [job for job in self._jobs.values() if (job.project, job.scene) == (project, scene)]
            devices = {job.device for job in jobs}
            return {"jobs": [job.to_dict() for job in sorted(jobs, key=lambda job: job.created)[-10:][::-1]],
                    "last_synced": {device: version for device in devices
                                    if (version := self._last_synced(device, project, scene)) is not None}}
