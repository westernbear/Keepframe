"""Per-device queue behavior, long-polling, and durable job records."""

import json
import os
import re
import stat
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import fields, is_dataclass

import pytest

from keepframe.ae import jobs as ae_jobs
from keepframe.ae.jobs import Job, Jobs


@pytest.fixture
def clock(monkeypatch):
    current = [1_800_000_000.0]
    monkeypatch.setattr(ae_jobs.time, "time", lambda: current[0])
    return current


@pytest.fixture
def queue(tmp_path, clock):
    return Jobs(tmp_path)


def enqueue(queue, kind="sync", **changes):
    return queue.enqueue(**{"device": "d_one", "kind": kind, "project": "project",
                            "scene": "scene", "version": "v1", **changes})


def applied_sync(queue, **changes):
    job = enqueue(queue, **changes)
    assert queue.next(job.device, wait=0).id == job.id
    return queue.finish(job.id, True, result={"applied": True})


def test_empty_queue_and_unknown_job(queue):
    assert queue.get("unknown") is None
    assert queue.last_synced("d_one", "project", "scene") is None
    assert queue.state("project", "scene") == {"jobs": [], "last_synced": {}}
    assert queue.next("d_one", wait=0) is None


def test_job_dataclass_fields_defaults_and_json(queue, clock):
    job = enqueue(queue)
    assert isinstance(job, Job) and is_dataclass(job)
    assert {item.name for item in fields(job)} == {
        "id", "device", "kind", "project", "scene", "version", "params", "state",
        "error", "result", "created", "started", "finished", "depends_on"}
    assert re.fullmatch(r"j_[0-9a-f]{16}", job.id)
    assert job.to_dict() == {
        "id": job.id, "device": "d_one", "kind": "sync", "project": "project",
        "scene": "scene", "version": "v1", "params": {}, "state": "queued",
        "error": None, "result": None, "created": clock[0], "started": None,
        "finished": None, "depends_on": None}
    assert json.loads(json.dumps(job.to_dict())) == job.to_dict()
    assert queue.get(job.id) == job


@pytest.mark.parametrize("kind", ["", "render", None, [], 12])
def test_invalid_kind_is_rejected_without_a_job(queue, kind):
    with pytest.raises(ValueError, match="kind"):
        enqueue(queue, kind)
    assert queue.state("project", "scene")["jobs"] == []


@pytest.mark.parametrize("name", ["device", "project", "scene", "version"])
@pytest.mark.parametrize("value", [None, "", 42])
def test_job_identity_requires_nonempty_strings(queue, name, value):
    with pytest.raises(ValueError, match=name):
        enqueue(queue, **{name: value})


@pytest.mark.parametrize("params", [[], "params", 7, {"value": object()}, {"value": float("nan")}])
def test_invalid_params_do_not_enqueue_a_prerequisite(queue, params):
    with pytest.raises(ValueError, match="params"):
        enqueue(queue, "package", params=params)
    assert queue.state("project", "scene")["jobs"] == []


def test_enqueue_and_finish_accept_zero_timestamps(queue):
    job = enqueue(queue, now=0)
    assert job.created == 0
    queue.next(job.device, wait=0)
    assert queue.finish(job.id, True, result={"applied": True}, now=0).finished == 0


@pytest.mark.parametrize("wait", [0.05, 0])
def test_next_times_out_without_work(queue, wait):
    start = time.monotonic()
    assert queue.next("d_one", wait=wait) is None
    elapsed = time.monotonic() - start
    assert wait <= elapsed < wait + 0.2


def test_enqueue_wakes_long_poll_in_under_200_ms(queue):
    entered = threading.Event()

    def poll():
        entered.set()
        return queue.next("d_one", wait=0.6)

    with ThreadPoolExecutor(max_workers=1) as executor:
        future = executor.submit(poll)
        assert entered.wait(0.2)
        time.sleep(0.03)
        assert not future.done()
        start = time.monotonic()
        queued = enqueue(queue)
        running = future.result(timeout=0.2)
        assert time.monotonic() - start < 0.2
    assert running.id == queued.id and running.state == "running"


def test_next_returns_oldest_ready_job_and_sets_started(queue, clock):
    newer = enqueue(queue, scene="newer", now=clock[0] + 2)
    older = enqueue(queue, scene="older", now=clock[0] + 1)
    running = queue.next("d_one", wait=0)
    assert running.id == older.id and running.state == "running"
    assert running.started == clock[0] and running.finished is None
    assert queue.get(newer.id).state == "queued"


def test_one_running_job_per_device_and_other_device_is_independent(queue):
    first = enqueue(queue)
    second = enqueue(queue, scene="second")
    other = enqueue(queue, device="d_two")
    assert queue.next("d_one", wait=0).id == first.id
    start = time.monotonic()
    assert queue.next("d_one", wait=0.05) is None
    assert time.monotonic() - start >= 0.05
    assert queue.get(second.id).state == "queued"
    assert queue.next("d_two", wait=0).id == other.id


def test_concurrent_pollers_cannot_start_two_jobs_for_one_device(queue):
    enqueue(queue)
    enqueue(queue, scene="second")
    barrier = threading.Barrier(3)

    def poll():
        barrier.wait()
        return queue.next("d_one", wait=0.05)

    with ThreadPoolExecutor(max_workers=2) as executor:
        futures = [executor.submit(poll) for _ in range(2)]
        barrier.wait()
        jobs = [future.result(timeout=0.3) for future in futures]
    assert sum(job is not None for job in jobs) == 1


@pytest.mark.parametrize("abandon", [False, True])
def test_finishing_or_abandoning_wakes_poller_blocked_by_running_job(queue, abandon):
    first = enqueue(queue)
    queue.next("d_one", wait=0)
    second = enqueue(queue, scene="second")
    entered = threading.Event()

    def poll():
        entered.set()
        return queue.next("d_one", wait=0.6)

    with ThreadPoolExecutor(max_workers=1) as executor:
        future = executor.submit(poll)
        assert entered.wait(0.2)
        time.sleep(0.03)
        assert not future.done()
        start = time.monotonic()
        if abandon:
            queue.abandon("d_one", "panel restarted")
        else:
            queue.finish(first.id, True, result={"applied": True})
        assert future.result(timeout=0.2).id == second.id
        assert time.monotonic() - start < 0.2


@pytest.mark.parametrize("kind", ["render_frames", "render_final", "package"])
def test_prerequisite_sync_runs_before_dependent_job(queue, kind):
    job = enqueue(queue, kind, params={"frames": [0, 10]})
    sync = queue.get(job.depends_on)
    assert sync.kind == "sync" and sync.version == "v1"
    assert (sync.device, sync.project, sync.scene) == (job.device, job.project, job.scene)
    assert queue.next("d_one", wait=0).id == sync.id
    assert queue.next("d_one", wait=0) is None
    queue.finish(sync.id, True, result={"applied": True})
    assert queue.next("d_one", wait=0).id == job.id
    assert queue.get(job.id).params == {"frames": [0, 10]}


@pytest.mark.parametrize("kind", ["render_frames", "render_final", "package"])
def test_matching_applied_sync_avoids_prerequisite(queue, kind):
    applied_sync(queue)
    job = enqueue(queue, kind)
    assert job.depends_on is None
    assert queue.next("d_one", wait=0).id == job.id


@pytest.mark.parametrize("changes", [{"device": "d_two"}, {"project": "other"},
                                     {"scene": "other"}, {"version": "v2"}])
def test_sync_version_is_scoped_to_device_project_and_scene(queue, changes):
    applied_sync(queue)
    assert enqueue(queue, "package", **changes).depends_on is not None


def test_coalescing_supersedes_queued_sync_and_repoints_all_dependents(queue, clock):
    first = enqueue(queue, "render_frames")
    old_id = first.depends_on
    unrelated = enqueue(queue, scene="unrelated")
    clock[0] += 1
    newest = enqueue(queue, "render_final", version="v2")
    sync = queue.get(newest.depends_on)
    old = queue.get(old_id)
    assert old.state == "superseded" and old.finished == clock[0]
    assert old.started is None
    assert queue.get(first.id).depends_on == sync.id
    assert queue.get(unrelated.id).state == "queued"
    assert queue.next("d_one", wait=0).id == unrelated.id
    queue.finish(unrelated.id, True, result={"applied": True})
    assert queue.next("d_one", wait=0).id == sync.id
    queue.finish(sync.id, True, result={"applied": True})
    assert queue.next("d_one", wait=0).id == first.id


@pytest.mark.parametrize("changes", [{"device": "d_two"}, {"project": "other"}, {"scene": "other"}])
def test_coalescing_does_not_cross_device_project_or_scene(queue, changes):
    first = enqueue(queue)
    enqueue(queue, version="v2", **changes)
    assert queue.get(first.id).state == "queued"


def test_coalescing_does_not_supersede_running_sync(queue):
    first = enqueue(queue)
    queue.next("d_one", wait=0)
    newest = enqueue(queue, version="v2")
    assert queue.get(first.id).state == "running"
    assert queue.get(newest.id).state == "queued"


@pytest.mark.parametrize("result", [None, {}, {"applied": False}, {"applied": 1}, {"applied": "true"}])
def test_only_explicitly_applied_sync_counts_as_synced(queue, result):
    job = enqueue(queue)
    queue.next("d_one", wait=0)
    done = queue.finish(job.id, True, result=result)
    assert done.state == "done"
    assert queue.last_synced("d_one", "project", "scene") is None


def test_latest_successfully_applied_sync_is_last_synced(queue, clock):
    applied_sync(queue)
    clock[0] += 1
    applied_sync(queue, version="v2")
    unapplied = enqueue(queue, version="v3")
    queue.next("d_one", wait=0)
    queue.finish(unapplied.id, True, result={"applied": False})
    assert queue.last_synced("d_one", "project", "scene") == "v2"


@pytest.mark.parametrize("ok, result, error, dependent_error", [
    (False, None, "bad layer at line 12", "sync failed: bad layer at line 12"),
    (True, {"applied": False}, None, "AE layers were edited by hand; resend with overwrite"),
])
def test_sync_failure_or_hand_edits_fail_dependent_jobs(queue, clock, ok, result, error, dependent_error):
    dependent = enqueue(queue, "render_final")
    sync = queue.next("d_one", wait=0)
    done = queue.finish(sync.id, ok, result=result, error=error, now=clock[0] + 1)
    assert done.state == ("done" if ok else "failed")
    failed = queue.get(dependent.id)
    assert failed.state == "failed" and failed.error == dependent_error
    assert failed.finished == clock[0] + 1 and failed.started is None
    assert queue.next("d_one", wait=0) is None


def test_successful_non_sync_job_stores_result_and_failure_does_not_mark_synced(queue, clock):
    applied_sync(queue)
    job = enqueue(queue, "package")
    queue.next("d_one", wait=0)
    done = queue.finish(job.id, True, result={"file": "project.zip"}, now=clock[0] + 1)
    assert done.result == {"file": "project.zip"} and done.error is None
    assert done.state == "done" and done.finished == clock[0] + 1
    failed_sync = enqueue(queue, version="v2")
    queue.next("d_one", wait=0)
    assert queue.finish(failed_sync.id, False, error="AE error").error == "AE error"
    assert queue.last_synced("d_one", "project", "scene") == "v1"


def test_finish_rejects_unknown_queued_and_terminal_jobs(queue):
    job = enqueue(queue)
    with pytest.raises(ValueError, match="running"):
        queue.finish("unknown", True)
    with pytest.raises(ValueError, match="running"):
        queue.finish(job.id, True)
    queue.next("d_one", wait=0)
    queue.finish(job.id, False, error="failed")
    with pytest.raises(ValueError, match="running"):
        queue.finish(job.id, True)


@pytest.mark.parametrize("error", [None, "", 7, [], "x" * 2001])
def test_failure_requires_readable_error_up_to_2000_characters(queue, error):
    job = enqueue(queue)
    queue.next("d_one", wait=0)
    with pytest.raises(ValueError, match="error"):
        queue.finish(job.id, False, error=error)
    assert queue.get(job.id).state == "running"
    assert queue.finish(job.id, False, error="x" * 2000).state == "failed"


@pytest.mark.parametrize("result", [[], "result", 7, {"value": object()}, {"value": float("inf")}])
def test_invalid_result_leaves_job_running(queue, result):
    job = enqueue(queue)
    queue.next("d_one", wait=0)
    with pytest.raises(ValueError, match="result"):
        queue.finish(job.id, True, result=result)
    assert queue.get(job.id).state == "running"


@pytest.mark.parametrize("elapsed, fails", [(59.999, False), (60, True), (60.001, True)])
def test_running_job_fails_after_device_silence(queue, clock, elapsed, fails):
    dependent = enqueue(queue, "package")
    running = queue.next("d_one", wait=0)
    now = clock[0] + elapsed
    failed = queue.sweep({"d_one": clock[0]}, now=now)
    assert {job.id for job in failed} == ({running.id, dependent.id} if fails else set())
    assert queue.get(running.id).state == ("failed" if fails else "running")
    if fails:
        assert queue.get(running.id).error == "AE disconnected"
        assert queue.get(dependent.id).error == "sync failed: AE disconnected"
        assert all(job.finished == now for job in failed)
        assert queue.sweep({}, now=now) == []


def test_missing_device_fails_running_job_but_keeps_unrelated_queued_jobs(queue, clock):
    running = enqueue(queue)
    queue.next("d_one", wait=0)
    queued = enqueue(queue, scene="other")
    assert [job.id for job in queue.sweep({}, now=clock[0])] == [running.id]
    assert queue.get(running.id).error == "AE disconnected"
    assert queue.get(queued.id).state == "queued"
    assert queue.next("d_one", wait=0).id == queued.id


@pytest.mark.parametrize("kind", ["sync", "render_frames", "render_final", "package"])
def test_abandon_only_fails_running_job_for_device(queue, clock, kind):
    done = applied_sync(queue)
    running = enqueue(queue, kind)
    assert queue.next("d_one", wait=0).id == running.id
    queued = enqueue(queue, scene="other")
    other = enqueue(queue, device="d_two")
    assert queue.next("d_two", wait=0).id == other.id
    clock[0] += 1
    failed = queue.abandon("d_one", "panel restarted")
    assert [job.id for job in failed] == [running.id]
    assert failed[0].state == "failed" and failed[0].error == "panel restarted"
    assert failed[0].finished == clock[0] and failed[0].started == clock[0] - 1
    assert queue.get(done.id).state == "done"
    assert queue.get(queued.id).state == "queued"
    assert queue.get(other.id).state == "running"
    assert queue.abandon("d_one", "panel restarted") == []
    failed[0].error = "changed snapshot"
    assert queue.get(running.id).error == "panel restarted"
    assert queue.next("d_one", wait=0).id == queued.id


def test_abandon_sync_fails_dependents_and_persists(queue, tmp_path, clock):
    dependent = enqueue(queue, "package")
    running = queue.next("d_one", wait=0)
    clock[0] += 1
    failed = queue.abandon("d_one", "panel restarted")
    assert [job.id for job in failed] == [running.id, dependent.id]
    assert all(job.state == "failed" and job.finished == clock[0] for job in failed)
    assert failed[1].error == "sync failed: panel restarted"
    restarted = Jobs(tmp_path)
    assert all(restarted.get(job.id) == job for job in failed)
    assert restarted.next("d_one", wait=0) is None


@pytest.mark.parametrize("reason", [None, "", " ", 7, [], "x" * 2001])
def test_abandon_requires_readable_error_without_changing_job(queue, reason):
    running = enqueue(queue)
    queue.next("d_one", wait=0)
    with pytest.raises(ValueError, match="error"):
        queue.abandon("d_one", reason)
    assert queue.get(running.id).state == "running"


def test_state_has_last_ten_scene_jobs_newest_first_and_per_device_versions(queue, clock):
    applied_sync(queue)
    applied_sync(queue, device="d_two", version="v2")
    enqueue(queue, project="other")
    enqueue(queue, scene="other")
    ids = []
    for index in range(12):
        ids.append(enqueue(queue, "package", now=clock[0] + index + 1).id)
    state = queue.state("project", "scene")
    assert [job["id"] for job in state["jobs"]] == ids[-10:][::-1]
    assert state["last_synced"] == {"d_one": "v1", "d_two": "v2"}


def test_inputs_returned_jobs_and_dicts_are_snapshots(queue):
    params = {"frames": [0, 5]}
    job = enqueue(queue, params=params)
    params["frames"].append(10)
    job.params["frames"].append(20)
    job.state = "done"
    snapshot = queue.get(job.id)
    snapshot.params["frames"].append(30)
    snapshot.to_dict()["params"]["frames"].append(40)
    queue.state("project", "scene")["jobs"][0]["params"]["frames"].append(50)
    assert queue.get(job.id).params == {"frames": [0, 5]}
    assert queue.get(job.id).state == "queued"
    queue.next("d_one", wait=0)
    result = {"applied": True, "warnings": []}
    finished = queue.finish(job.id, True, result=result)
    result["warnings"].append("external")
    finished.result["warnings"].append("snapshot")
    assert queue.get(job.id).result == {"applied": True, "warnings": []}


def test_restart_keeps_queued_jobs_fails_running_jobs_and_their_dependents(queue, tmp_path, clock):
    dependent = enqueue(queue, "render_frames")
    running = queue.next("d_one", wait=0)
    queued = enqueue(queue, scene="other")
    clock[0] += 1
    restarted = Jobs(tmp_path)
    assert restarted.get(running.id).state == "failed"
    assert restarted.get(running.id).error == "server restarted"
    assert restarted.get(running.id).finished == clock[0]
    assert restarted.get(dependent.id).error == "sync failed: server restarted"
    assert restarted.get(queued.id).state == "queued"
    assert restarted.next("d_one", wait=0).id == queued.id
    assert Jobs(tmp_path).get(running.id).error == "server restarted"


def test_queued_dependencies_and_applied_sync_survive_restart(queue, tmp_path):
    applied_sync(queue, device="d_two", version="v2")
    dependent = enqueue(queue, "package")
    restarted = Jobs(tmp_path)
    assert restarted.get(dependent.id).depends_on == dependent.depends_on
    assert restarted.next("d_one", wait=0).id == dependent.depends_on
    assert restarted.last_synced("d_two", "project", "scene") == "v2"


def test_coalescing_and_hand_edit_failure_are_persistent(queue, tmp_path):
    dependent = enqueue(queue, "package")
    old_id = dependent.depends_on
    new_sync = enqueue(queue, version="v2")
    restarted = Jobs(tmp_path)
    assert restarted.get(old_id).state == "superseded"
    assert restarted.get(dependent.id).depends_on == new_sync.id
    restarted.next("d_one", wait=0)
    restarted.finish(new_sync.id, True, result={"applied": False})
    again = Jobs(tmp_path)
    assert again.get(dependent.id).state == "failed"
    assert again.last_synced("d_one", "project", "scene") is None


def test_prune_eight_day_old_finished_jobs_but_keep_queued_and_seven_day_boundary(queue, tmp_path, clock):
    old = clock[0] - 8 * 86400
    done = enqueue(queue, scene="done", now=old)
    queue.next("d_one", wait=0)
    queue.finish(done.id, True, result={"applied": True}, now=old)
    failed = enqueue(queue, scene="failed", now=old)
    queue.next("d_one", wait=0)
    queue.finish(failed.id, False, error="old failure", now=old)
    superseded = enqueue(queue, scene="coalesced", now=old)
    kept = enqueue(queue, scene="coalesced", version="v2", now=old)
    boundary = enqueue(queue, scene="boundary", now=clock[0] - 7 * 86400)
    queue.next("d_one", wait=0)
    queue.finish(kept.id, True, result={"applied": True}, now=clock[0])
    assert queue.next("d_one", wait=0).id == boundary.id
    queue.finish(boundary.id, True, result={"applied": True}, now=clock[0] - 7 * 86400)
    queued = enqueue(queue, scene="queued", now=old)
    restarted = Jobs(tmp_path)
    for job in (done, failed, superseded):
        assert restarted.get(job.id) is None
        assert not (tmp_path / ".ae" / "jobs" / f"{job.id}.json").exists()
    assert restarted.get(boundary.id).state == "done"
    assert restarted.get(queued.id).state == "queued"


def test_pruning_successful_prerequisite_does_not_strand_queued_job(queue, tmp_path, clock):
    old = clock[0] - 8 * 86400
    dependent = enqueue(queue, "package", now=old)
    sync = queue.next("d_one", wait=0)
    queue.finish(sync.id, True, result={"applied": True}, now=old)
    restarted = Jobs(tmp_path)
    assert restarted.get(sync.id) is None
    assert restarted.next("d_one", wait=0).id == dependent.id


def test_job_files_use_private_directory_and_atomic_replace(queue, tmp_path, monkeypatch):
    directory = tmp_path / ".ae" / "jobs"
    original = os.replace
    replacements = []

    def replace(source, destination):
        source = type(directory)(source)
        assert source.parent == directory and source != destination
        assert stat.S_IMODE(source.stat().st_mode) == 0o600
        replacements.append(destination)
        original(source, destination)

    monkeypatch.setattr(ae_jobs.os, "replace", replace)
    job = enqueue(queue)
    queue.next("d_one", wait=0)
    finished = queue.finish(job.id, True, result={"applied": True})
    assert len(replacements) == 3
    assert stat.S_IMODE(directory.stat().st_mode) == 0o700
    assert [path.name for path in directory.iterdir()] == [f"{job.id}.json"]
    assert json.loads((directory / f"{job.id}.json").read_text()) == finished.to_dict()


@pytest.mark.parametrize("operation", ["enqueue", "next", "finish"])
def test_atomic_write_failure_is_loud_and_preserves_existing_job(queue, tmp_path, monkeypatch, operation):
    job = enqueue(queue)
    if operation == "finish":
        queue.next("d_one", wait=0)
    before = queue.get(job.id)
    path = tmp_path / ".ae" / "jobs" / f"{job.id}.json"
    contents = path.read_bytes()

    def fail_replace(*args):
        raise OSError("disk full")

    with monkeypatch.context() as patch:
        patch.setattr(ae_jobs.os, "replace", fail_replace)
        with pytest.raises(OSError, match="disk full"):
            if operation == "enqueue":
                enqueue(queue, scene="other")
            elif operation == "next":
                queue.next("d_one", wait=0)
            else:
                queue.finish(job.id, True, result={"applied": True})
    assert queue.get(job.id) == before
    assert path.read_bytes() == contents
    assert [item.name for item in path.parent.iterdir()] == [path.name]


@pytest.mark.parametrize("contents", [b"{broken", b"\xff", b"[]", b"{}"])
def test_corrupt_job_file_fails_loudly_without_overwriting(tmp_path, contents):
    directory = tmp_path / ".ae" / "jobs"
    directory.mkdir(parents=True)
    path = directory / "j_0123456789abcdef.json"
    path.write_bytes(contents)
    with pytest.raises(ValueError, match="corrupt"):
        Jobs(tmp_path)
    assert path.read_bytes() == contents


@pytest.mark.parametrize("changes", [{"created": None}, {"depends_on": []},
                                    {"state": "unknown"}, {"created": float("nan")}])
def test_invalid_stored_job_fields_fail_loudly(queue, tmp_path, changes):
    job = enqueue(queue)
    path = tmp_path / ".ae" / "jobs" / f"{job.id}.json"
    path.write_text(json.dumps(job.to_dict() | changes))
    before = path.read_bytes()
    with pytest.raises(ValueError, match="corrupt"):
        Jobs(tmp_path)
    assert path.read_bytes() == before


@pytest.mark.parametrize("ok, result, error, message", [
    (False, None, "AE error", "sync failed: AE error"),
    (True, {"applied": False}, None, "AE layers were edited by hand; resend with overwrite"),
])
def test_partial_dependency_write_cannot_dispatch_a_render(queue, monkeypatch, ok, result, error, message):
    dependent = enqueue(queue, "render_frames")
    sync = queue.next("d_one", wait=0)
    original = os.replace
    writes = 0

    def replace(source, destination):
        nonlocal writes
        writes += 1
        if writes == 2:
            raise OSError("disk full")
        original(source, destination)

    with monkeypatch.context() as patch:
        patch.setattr(ae_jobs.os, "replace", replace)
        with pytest.raises(OSError, match="disk full"):
            queue.finish(sync.id, ok, result=result, error=error)
    assert queue.next("d_one", wait=0) is None
    assert queue.get(dependent.id).state == "failed"
    assert queue.get(dependent.id).error == message


def test_missing_prerequisite_fails_queued_job_on_restart(queue, tmp_path):
    dependent = enqueue(queue, "package")
    (tmp_path / ".ae" / "jobs" / f"{dependent.depends_on}.json").unlink()
    restarted = Jobs(tmp_path)
    assert restarted.get(dependent.id).state == "failed"
    assert restarted.get(dependent.id).error == "sync failed: prerequisite is missing"


def test_partial_coalescing_write_does_not_leave_a_dependency_waiting_forever(queue, tmp_path, monkeypatch):
    dependent = enqueue(queue, "package")
    original = os.replace
    writes = 0

    def replace(source, destination):
        nonlocal writes
        writes += 1
        if writes == 3:
            raise OSError("disk full")
        original(source, destination)

    with monkeypatch.context() as patch:
        patch.setattr(ae_jobs.os, "replace", replace)
        with pytest.raises(OSError, match="disk full"):
            enqueue(queue, version="v2")
    restarted = Jobs(tmp_path)
    assert restarted.get(dependent.id).state == "failed"
    assert restarted.get(dependent.id).error == "sync failed: prerequisite was superseded"


@pytest.mark.parametrize("wait", [-1, float("nan"), float("inf"), None, "0.05"])
def test_invalid_poll_wait_is_rejected(queue, wait):
    with pytest.raises(ValueError, match="wait"):
        queue.next("d_one", wait=wait)


@pytest.mark.parametrize("now", [float("nan"), float("inf"), "now", True])
def test_invalid_timestamp_cannot_enqueue_a_job(queue, now):
    with pytest.raises(ValueError, match="timestamp"):
        enqueue(queue, now=now)
    assert queue.state("project", "scene")["jobs"] == []


@pytest.mark.parametrize("ok", [None, "false", 0, 1])
def test_finish_requires_boolean_ok(queue, ok):
    job = enqueue(queue)
    queue.next("d_one", wait=0)
    with pytest.raises(ValueError, match="ok"):
        queue.finish(job.id, ok, error="failed")
    assert queue.get(job.id).state == "running"
