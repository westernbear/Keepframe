"""AE footage derivation limits (R60): real duration, caps during the encode, bounded concurrency and state."""
import subprocess
import threading
import time
from pathlib import Path

import numpy as np
import pytest

from keepframe.ae import footage
from keepframe.analyze.videoasset import encode_webm


def clip(root: Path, i: int, frames: int = 3) -> Path:
    path = root / f"c{i}.webm"
    encode_webm([np.full((24, 32, 3), (20 * i, 90, 200 - 20 * i), np.uint8) for _ in range(frames)], 30, path,
                alpha=False)
    return path


def wait_until(check, seconds=20):
    deadline = time.monotonic() + seconds
    while not check():
        assert time.monotonic() < deadline, "timed out"
        time.sleep(0.02)


def test_too_long_is_decided_by_the_file_not_the_caller(tmp_path, monkeypatch):
    src = clip(tmp_path, 0)                 # 3 frames at 30 fps = 0.1 s
    assert footage.probe_seconds(src) == pytest.approx(0.1, abs=0.04)   # the real probe
    encodes = []
    monkeypatch.setattr(footage, "_encode", lambda *args: encodes.append(args))
    monkeypatch.setattr(footage, "MAX_SECONDS", 0.05)
    with pytest.raises(footage.FootageError, match="footage_too_long"):
        footage.derive(src, "plate", 30, (32, 24), 0.01)   # the caller understates the length
    assert encodes == []
    monkeypatch.setattr(footage, "MAX_SECONDS", 120.0)
    probes = []

    def ffprobe(command, timeout):
        probes.append((command, timeout))
        return subprocess.CompletedProcess(command, 0, b"9999.000000\n", b"")

    monkeypatch.setattr(footage, "_ffprobe", ffprobe)
    with pytest.raises(footage.FootageError, match="footage_too_long"):
        footage.derive(clip(tmp_path, 1), "plate", 30, (32, 24), 0.1)
    (command, timeout), = probes
    assert isinstance(command, list) and command[0] == "ffprobe" and command[-1] == str(tmp_path / "c1.webm")
    assert timeout <= 10 and encodes == []


def test_caps_are_enforced_while_encoding(tmp_path, monkeypatch):
    commands, original = [], footage._encode
    monkeypatch.setattr(footage, "_encode", lambda command, timeout: commands.append((command, timeout))
                        or original(command, timeout))
    out = footage.derive(clip(tmp_path, 0), "plate", 30, (32, 24), 0.1)
    (command, timeout), = commands
    source = command.index("-i")
    assert command[command.index("-t") + 1] == repr(float(footage.MAX_SECONDS)) and command.index("-t") < source
    assert command[command.index("-fs") + 1] == str(footage.MAX_BYTES) and command.index("-fs") > source
    assert timeout == footage.encode_timeout(0.1) and footage.encode_timeout(1e9) == footage.MAX_TIMEOUT_S
    assert out.is_file()

    # Defence in depth: an output that reached the size cap is dropped, whatever ffmpeg reported.
    monkeypatch.setattr(footage, "MAX_BYTES", 4096)

    def big(command, timeout):
        Path(command[-1]).write_bytes(b"\0" * 4096)
        return subprocess.CompletedProcess(command, 0, b"", b"")

    monkeypatch.setattr(footage, "_encode", big)
    src = clip(tmp_path, 1)
    with pytest.raises(footage.FootageError, match="footage_too_large"):
        footage.derive(src, "plate", 30, (32, 24), 0.1)
    assert not [p for p in tmp_path.iterdir() if ".ae." in p.name and p.name.startswith((".c1", "c1"))]


def test_at_most_two_encodes_run_with_more_clips_pending(tmp_path, monkeypatch):
    monkeypatch.setattr(footage, "MAX_QUEUED", 1)
    release, lock, running, peak, original = threading.Event(), threading.Lock(), [0], [0], footage._encode

    def encode(command, timeout):
        with lock:
            running[0] += 1
            peak[0] = max(peak[0], running[0])
        try:
            assert release.wait(20)
            return original(command, timeout)
        finally:
            with lock:
                running[0] -= 1

    monkeypatch.setattr(footage, "_encode", encode)
    sources = [clip(tmp_path, i) for i in range(5)]
    assert all(footage.prepare(src, "plate", 30, (32, 24), 0.1) for src in sources)
    wait_until(lambda: running[0] == footage.MAX_ENCODES)
    time.sleep(0.3)
    assert running[0] == peak[0] == 2
    assert len(footage._threads) == footage.MAX_ENCODES + footage.MAX_QUEUED   # 2 encoding, 1 waiting for a slot
    release.set()
    wait_until(lambda: not footage._threads)
    assert sum(footage.prepare(src, "plate", 30, (32, 24), 0.1) for src in sources) == 2   # the two left start now
    wait_until(lambda: not any(footage.prepare(src, "plate", 30, (32, 24), 0.1) for src in sources))
    assert peak[0] == 2 and not footage._threads and not footage._active


def test_failure_and_thread_state_stay_bounded(tmp_path, monkeypatch):
    monkeypatch.setattr(footage, "FAILED_CAP", 3)
    monkeypatch.setattr(footage, "_encode", lambda command, timeout: subprocess.CompletedProcess(command, 1, b"", b"x"))
    sources = [clip(tmp_path, i) for i in range(5)]
    for src in sources:
        with pytest.raises(footage.FootageError, match="footage_failed"):
            footage.derive(src, "plate", 30, (32, 24), 0.1)
    assert len(footage._failed) <= 3
    assert footage.derived_path(sources[-1], "plate") in footage._failed
    assert footage.derived_path(sources[0], "plate") not in footage._failed      # the oldest was dropped
    for src in sources:
        footage.prepare(src, "plate", 30, (32, 24), 0.1)
    wait_until(lambda: not footage._threads)
    assert len(footage._failed) <= 3 and not footage._active


def test_derived_hash_is_primed_before_the_first_spec(tmp_path, monkeypatch):
    from keepframe.ae.spec import comp_spec, prepare_footage
    from tests.test_ae_spec import _video_scene
    value = _video_scene(tmp_path)
    assert prepare_footage(value, tmp_path)
    wait_until(lambda: not prepare_footage(value, tmp_path))
    original = footage.hashlib.file_digest

    def digest(stream, name):
        assert ".ae." not in Path(stream.name).name, f"derived file hashed in the request: {stream.name}"
        return original(stream, name)

    monkeypatch.setattr(footage.hashlib, "file_digest", digest)
    spec = comp_spec(value, tmp_path, project="p", scene_id="s", version="v", derive=False)
    assert [layer["kind"] for layer in spec["layers"]] == ["footage", "footage"]
