"""PYTEST_DONT_REWRITE: keep pairing credentials out of assertion failure output."""

import base64
import hashlib
import hmac
import json
import os
import re
import stat
from concurrent.futures import ThreadPoolExecutor
from dataclasses import fields, is_dataclass

import pytest

from keepframe.ae import devices as ae_devices
from keepframe.ae.devices import Device, Devices


def info(**changes):
    return {"ae_version": "24.1", "extension_version": "1.0.0", "os": "Windows",
            "fonts": [{"family": "Example", "style": "Regular", "postscript": "Example-Regular"}],
            **changes}


@pytest.fixture
def clock(monkeypatch):
    current = [1000.0]
    monkeypatch.setattr(ae_devices.time, "time", lambda: current[0])
    return current


@pytest.fixture
def store(tmp_path, clock):
    return Devices(tmp_path)


def paired(store, details=None, now=1000):
    code, _ = store.create_code(now=now)
    return store.pair(code, info() if details is None else details, now=now)


def test_empty_store_and_unknown_devices_are_safe_noops(store, tmp_path):
    assert store.list() == []
    assert store.authenticate("unknown") is None
    assert store.fonts("unknown") is None
    assert store.revoke("unknown") is False
    store.touch("unknown", {"project_name": None, "project_saved": False})
    store.seen("unknown")
    store.update_info("unknown", info())
    assert not (tmp_path / ".ae" / "devices.json").exists()


def test_lone_surrogate_code_and_token_are_wrong_credentials(store):
    paired(store)
    assert store.pair("\ud800", info()) is None
    assert store.authenticate("\ud800") is None


def test_existing_loose_directory_is_tightened(store, tmp_path):
    directory = tmp_path / ".ae"
    directory.mkdir(mode=0o755)
    directory.chmod(0o755)
    store.create_code()
    assert stat.S_IMODE(directory.stat().st_mode) == 0o700


def test_codes_have_crockford_format_and_ten_minute_expiry(store):
    for _ in range(32):
        code, expires_at = store.create_code()
        assert re.fullmatch(r"KF-[0123456789ABCDEFGHJKMNPQRSTVWXYZ]{4}-[0123456789ABCDEFGHJKMNPQRSTVWXYZ]{4}", code)
        assert expires_at == 1600


@pytest.mark.parametrize("entered", ["KF-011A-BCDF", "kf-011a-bcdf", "011ABCDF",
    " k f - 0 1 1 a - b c d f ", "kf-oilA-bcdf", "OILABCDF", "kf-ollA-bcdf"])
def test_code_normalisation_accepts_case_spaces_hyphens_and_aliases(store, monkeypatch, entered):
    symbols = iter("011ABCDF")
    monkeypatch.setattr(ae_devices.secrets, "choice", lambda alphabet: next(symbols))
    store.create_code()
    assert store.pair(entered, info()) is not None


@pytest.mark.parametrize("elapsed, succeeds", [(599.999, True), (600, False), (600.001, False)])
def test_code_expires_at_exactly_600_seconds(store, elapsed, succeeds):
    code, _ = store.create_code(now=1000)
    assert (store.pair(code, info(), now=1000 + elapsed) is not None) is succeeds


def test_code_is_single_use_and_wrong_code_does_not_consume_it(store):
    code, _ = store.create_code()
    assert store.pair("incorrect", info()) is None
    assert store.pair(code, info()) is not None
    assert store.pair(code, info()) is None


def test_duplicate_generated_codes_still_can_only_be_used_once(store, monkeypatch):
    monkeypatch.setattr(ae_devices.secrets, "choice", lambda alphabet: "A")
    code, _ = store.create_code()
    store.create_code()
    assert store.pair(code, info()) is not None
    assert store.pair(code, info()) is None


def test_sixth_code_drops_oldest_by_creation_time(store, tmp_path):
    codes = [(now, store.create_code(now=now)[0]) for now in [1004, 1000, 1002, 1001, 1003, 1005]]
    assert len(json.loads((tmp_path / ".ae" / "devices.json").read_bytes())["codes"]) == 5
    for now, code in codes:
        assert (store.pair(code, info(), now=1006) is not None) is (now != 1000)


def test_expired_codes_do_not_count_towards_limit(store, tmp_path):
    old = [store.create_code(now=1000)[0] for _ in range(5)]
    new, _ = store.create_code(now=1600)
    assert len(json.loads((tmp_path / ".ae" / "devices.json").read_bytes())["codes"]) == 1
    assert all(store.pair(code, info(), now=1600) is None for code in old)
    assert store.pair(new, info(), now=1600) is not None


@pytest.mark.parametrize("operation", ["create_code", "pair", "authenticate", "touch", "seen",
                                      "update_info", "fonts", "list", "revoke", "restart"])
def test_every_call_purges_expired_codes(store, tmp_path, clock, operation):
    device_id, token = paired(store)
    expired, _ = store.create_code()
    clock[0] = 1600
    calls = {
        "create_code": lambda: store.create_code(),
        "pair": lambda: store.pair("incorrect", info()),
        "authenticate": lambda: store.authenticate(token),
        "touch": lambda: store.touch(device_id, {"project_name": None, "project_saved": False}),
        "seen": lambda: store.seen(device_id),
        "update_info": lambda: store.update_info(device_id, info()),
        "fonts": lambda: store.fonts(device_id),
        "list": lambda: store.list(),
        "revoke": lambda: store.revoke(device_id),
        "restart": lambda: Devices(tmp_path),
    }
    result = calls[operation]()
    clock[0] = 1000
    target = result if operation == "restart" else store
    assert target.pair(expired, info()) is None


def test_pair_compares_every_live_hash_even_when_first_matches(store, monkeypatch):
    codes = [store.create_code()[0] for _ in range(5)]
    comparisons = []
    original = hmac.compare_digest

    def compare(left, right):
        comparisons.append((left, right))
        return original(left, right)

    monkeypatch.setattr(ae_devices.hmac, "compare_digest", compare)
    assert store.pair("incorrect", info()) is None
    assert len(comparisons) == 5
    comparisons.clear()
    assert store.pair(codes[0], info()) is not None
    assert len(comparisons) == 5


def test_only_hashes_are_on_disk_and_permissions_are_private(store, tmp_path):
    code, _ = store.create_code()
    path = tmp_path / ".ae" / "devices.json"
    raw_symbols = code.replace("-", "")[2:]
    contents = path.read_bytes()
    assert code.encode() not in contents
    assert raw_symbols.encode() not in contents
    assert hashlib.sha256(raw_symbols.encode()).hexdigest().encode() in contents
    assert set(json.loads(contents)) == {"codes", "devices"}
    device_id, token = store.pair(code, info())
    assert re.fullmatch(r"d_[0-9a-f]{12}", device_id)
    assert len(base64.urlsafe_b64decode(token + "=" * (-len(token) % 4))) == 32
    contents = path.read_bytes()
    assert token.encode() not in contents
    assert code.encode() not in contents
    assert hashlib.sha256(token.encode()).hexdigest().encode() in contents
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert stat.S_IMODE(path.parent.stat().st_mode) == 0o700


def test_authentication_returns_only_device_fields_and_survives_restart(store, tmp_path):
    device_id, token = paired(store)
    device = store.authenticate(token)
    assert is_dataclass(device) and isinstance(device, Device)
    assert {field.name for field in fields(device)} == {"id", "created", "info", "last_seen", "status"}
    assert device.id == device_id
    assert device.created == 1000
    assert device.info == info()
    assert store.authenticate(token + "wrong") is None
    assert Devices(tmp_path).authenticate(token).id == device_id


def test_codes_survive_restart_until_used(store, tmp_path):
    code, _ = store.create_code()
    restarted = Devices(tmp_path)
    assert restarted.pair(code, info()) is not None
    assert Devices(tmp_path).pair(code, info()) is None


def test_build_ids_survive_pair_update_and_restart(store, tmp_path):
    builds = {"host_build": "123-abc1234", "panel_build": "124-def5678-dirty"}
    device_id, token = paired(store, info(**builds))
    assert store.authenticate(token).info == info(**builds)
    assert all(store.list()[0][key] == value for key, value in builds.items())
    assert Devices(tmp_path).authenticate(token).info == info(**builds)
    builds = {"host_build": "dev", "panel_build": "dev"}
    store.update_info(device_id, info(**builds))
    assert Devices(tmp_path).authenticate(token).info == info(**builds)
    # Older panels and stored records still load without build fields.
    store.update_info(device_id, info())
    assert Devices(tmp_path).authenticate(token).info == info()
    assert store.list()[0]["host_build"] is store.list()[0]["panel_build"] is None


@pytest.mark.parametrize("field", ["host_build", "panel_build"])
@pytest.mark.parametrize("bad", [None, 1, [], {}, "x" * 65])
def test_build_ids_are_short_strings_and_invalid_updates_preserve_info(store, field, bad):
    code, _ = store.create_code()
    with pytest.raises(ValueError, match=field):
        store.pair(code, info(**{field: bad}))
    device_id, token = store.pair(code, info(**{field: "x" * 64}))
    before = store.authenticate(token).info
    with pytest.raises(ValueError, match=field):
        store.update_info(device_id, info(**{field: bad}))
    assert store.authenticate(token).info == before


def test_revoke_removes_device_and_token_persistently(store, tmp_path):
    device_id, token = paired(store)
    assert store.revoke(device_id) is True
    assert store.revoke(device_id) is False
    store.touch(device_id, {"project_name": "Still open", "project_saved": True})
    store.update_info(device_id, info())
    assert store.authenticate(token) is None
    assert store.fonts(device_id) is None
    assert store.list() == []
    restarted = Devices(tmp_path)
    assert restarted.authenticate(token) is None
    assert restarted.list() == []


@pytest.mark.parametrize("field, limit", [("ae_version", 32), ("extension_version", 32), ("os", 64)])
@pytest.mark.parametrize("bad", [None, 1, [], {}, "too long"])
def test_invalid_info_strings_raise_without_consuming_code(store, field, limit, bad):
    code, _ = store.create_code()
    value = "x" * (limit + 1) if bad == "too long" else bad
    with pytest.raises(ValueError, match=field):
        store.pair(code, info(**{field: value}))
    assert store.pair(code, info()) is not None


@pytest.mark.parametrize("field", ["ae_version", "extension_version", "os", "fonts"])
def test_info_requires_all_fields(store, field):
    code, _ = store.create_code()
    details = info()
    del details[field]
    with pytest.raises(ValueError, match=field):
        store.pair(code, details)


@pytest.mark.parametrize("details", [None, [], "invalid"])
def test_info_must_be_a_dict(store, details):
    code, _ = store.create_code()
    with pytest.raises(ValueError, match="info"):
        store.pair(code, details)


@pytest.mark.parametrize("fonts", [None, {}, (), "fonts"])
def test_fonts_must_be_a_list_of_font_dicts(store, fonts):
    code, _ = store.create_code()
    with pytest.raises(ValueError):
        store.pair(code, info(fonts=fonts))


@pytest.mark.parametrize("field", ["family", "style", "postscript"])
@pytest.mark.parametrize("bad", [123, [], "x" * 257])
def test_font_strings_are_validated(store, field, bad):
    font = {"family": "Example", "style": "Regular", "postscript": "Example-Regular", field: bad}
    code, _ = store.create_code()
    device_id, _ = store.pair(code, info(fonts=[font]))
    assert store.fonts(device_id) == []


def test_font_family_cannot_be_null(store):
    code, _ = store.create_code()
    device_id, _ = store.pair(code, info(fonts=[{"family": None, "style": None, "postscript": None}]))
    assert store.fonts(device_id) == []


def test_font_cap_truncates_overflow_and_accepts_all_maximum_lengths(store):
    font = {"family": "한" * 256, "style": "x" * 256, "postscript": "x" * 256}
    code, _ = store.create_code()
    device_id, _ = store.pair(code, info(fonts=[font] * 5001))
    assert store.fonts(device_id) == [font] * 5000
    code, _ = store.create_code()
    details = info(ae_version="a" * 32, extension_version="e" * 32, os="o" * 64, fonts=[font] * 5000)
    device_id, _ = store.pair(code, details)
    assert store.fonts(device_id) == [font] * 5000


def test_unknown_info_and_font_keys_are_dropped_and_nullable_fields_preserved(store, tmp_path):
    details = info(extra="ignored", fonts=[{"family": "Example", "style": None,
                                           "postscript": None, "extra": "ignored"}])
    device_id, token = paired(store, details)
    expected = info(fonts=[{"family": "Example", "style": None, "postscript": None}])
    assert store.authenticate(token).info == expected
    assert store.fonts(device_id) == expected["fonts"]
    assert b"ignored" not in (tmp_path / ".ae" / "devices.json").read_bytes()
    assert Devices(tmp_path).fonts(device_id) == expected["fonts"]


def test_update_info_persists_without_changing_identity_or_transient_status(store, tmp_path):
    device_id, token = paired(store)
    status = {"project_name": "Open project", "project_saved": True}
    store.touch(device_id, status, now=1001)
    updated = info(ae_version="25.0", fonts=[])
    store.update_info(device_id, updated)
    device = store.authenticate(token)
    assert device.id == device_id and device.created == 1000
    assert device.info == updated and device.last_seen == 1001 and device.status == status
    assert store.fonts(device_id) == []
    restarted = Devices(tmp_path)
    assert restarted.authenticate(token).info == updated
    assert restarted.list()[0]["connected"] is False
    assert restarted.list()[0]["last_seen"] is None


def test_invalid_update_info_preserves_existing_info_and_file(store, tmp_path):
    device_id, token = paired(store)
    path = tmp_path / ".ae" / "devices.json"
    before = path.read_bytes()
    with pytest.raises(ValueError, match="fonts"):
        store.update_info(device_id, info(fonts=None))
    assert store.authenticate(token).info == info()
    assert path.read_bytes() == before


def test_touch_never_writes_file_and_restart_clears_connection(store, tmp_path, monkeypatch):
    device_id, token = paired(store)
    path = tmp_path / ".ae" / "devices.json"
    before = path.read_bytes()

    def forbidden_write(*args):
        pytest.fail("touch must not write devices.json")

    with monkeypatch.context() as patch:
        patch.setattr(ae_devices.os, "replace", forbidden_write)
        store.touch(device_id, {"project_name": "Project.aep", "project_saved": True}, now=1001)
    assert path.read_bytes() == before
    device = store.authenticate(token)
    assert device.last_seen == 1001
    assert device.status == {"project_name": "Project.aep", "project_saved": True}
    restarted = Devices(tmp_path).list(now=1002)[0]
    assert restarted["last_seen"] is None and restarted["connected"] is False
    assert restarted["project_name"] is None and restarted["project_saved"] is False


def test_seen_refreshes_in_memory_without_overwriting_status(store, tmp_path, clock, monkeypatch):
    device_id, token = paired(store)
    status = {"project_name": "Project.aep", "project_saved": True}
    store.touch(device_id, status, now=1001)
    path = tmp_path / ".ae" / "devices.json"
    before = path.read_bytes()

    def forbidden_write(*args):
        pytest.fail("seen must not write devices.json")

    with monkeypatch.context() as patch:
        patch.setattr(ae_devices.os, "replace", forbidden_write)
        store.seen(device_id, now=1002)
        assert store.authenticate(token).last_seen == 1002
        clock[0] = 1003
        store.seen(device_id)
    device = store.authenticate(token)
    assert device.last_seen == 1003 and device.status == status
    assert path.read_bytes() == before
    assert Devices(tmp_path).authenticate(token).last_seen is None


@pytest.mark.parametrize("operation", ["create_code", "pair", "update_info", "revoke"])
def test_other_writes_never_persist_touch_status(store, tmp_path, operation):
    device_id, token = paired(store)
    other_id, _ = paired(store)
    store.touch(device_id, {"project_name": "Transient project", "project_saved": True})
    if operation == "create_code":
        store.create_code()
    elif operation == "pair":
        paired(store)
    elif operation == "update_info":
        store.update_info(device_id, info())
    else:
        store.revoke(other_id)
    contents = (tmp_path / ".ae" / "devices.json").read_bytes()
    assert b"Transient project" not in contents
    assert b"last_seen" not in contents and b"status" not in contents
    assert Devices(tmp_path).authenticate(token).last_seen is None


@pytest.mark.parametrize("status", [None, [], {},
    {"project_name": "x" * 257, "project_saved": False},
    {"project_name": 1, "project_saved": False},
    {"project_name": None, "project_saved": "true"},
    {"project_name": None, "project_saved": 1},
    {"project_name": None, "project_saved": None}])
def test_touch_validates_status_without_changing_last_seen(store, status):
    device_id, token = paired(store)
    before = store.authenticate(token)
    with pytest.raises(ValueError):
        store.touch(device_id, status)
    assert store.authenticate(token) == before


@pytest.mark.parametrize("name", [None, "x" * 256])
def test_touch_accepts_status_limits_and_drops_unknown_keys(store, name):
    device_id, token = paired(store)
    store.touch(device_id, {"project_name": name, "project_saved": False, "extra": "ignored"})
    assert store.authenticate(token).status == {"project_name": name, "project_saved": False}


@pytest.mark.parametrize("elapsed, connected", [(0, True), (39.999, True), (40, False), (40.001, False)])
def test_connected_flips_at_40_seconds_including_zero_timestamp(store, elapsed, connected):
    device_id, _ = paired(store, now=0)
    store.touch(device_id, {"project_name": None, "project_saved": False}, now=0)
    assert store.list(now=elapsed)[0]["connected"] is connected


def test_list_is_sorted_and_contains_only_public_summary_fields(store):
    newer, _ = paired(store, now=1002)
    older, _ = paired(store, now=1000)
    store.touch(older, {"project_name": "Project.aep", "project_saved": True}, now=1003)
    rows = store.list(now=1004)
    assert [row["id"] for row in rows] == [older, newer]
    assert rows[0] == {"id": older, "ae_version": "24.1", "extension_version": "1.0.0",
                       "host_build": None, "panel_build": None,
                       "os": "Windows", "project_name": "Project.aep", "project_saved": True,
                       "connected": True, "last_seen": 1003, "created": 1000}
    assert set(rows[1]) == set(rows[0])
    assert rows[1]["connected"] is False


def test_mutating_inputs_or_returned_snapshots_cannot_change_store(store):
    details = info()
    device_id, token = paired(store, details)
    details["fonts"][0]["family"] = "Changed outside"
    device = store.authenticate(token)
    device.info["fonts"][0]["family"] = "Changed snapshot"
    device.status["project_name"] = "Changed snapshot"
    fonts = store.fonts(device_id)
    fonts[0]["family"] = "Changed fonts"
    status = {"project_name": "Original", "project_saved": True}
    store.touch(device_id, status)
    status["project_name"] = "Changed input"
    assert store.authenticate(token).info == info()
    assert store.list()[0]["project_name"] == "Original"


def test_writes_use_private_temporary_file_in_same_directory(store, tmp_path, monkeypatch):
    original = os.replace
    replacements = []
    path = tmp_path / ".ae" / "devices.json"

    def replace(source, destination):
        source = type(path)(source)
        assert source.parent == path.parent and source != path
        assert stat.S_IMODE(source.stat().st_mode) == 0o600
        replacements.append(destination)
        original(source, destination)

    monkeypatch.setattr(ae_devices.os, "replace", replace)
    device_id, _ = paired(store)
    store.update_info(device_id, info(fonts=[]))
    store.revoke(device_id)
    assert len(replacements) == 4
    assert all(destination == path for destination in replacements)
    assert sorted(item.name for item in path.parent.iterdir()) == ["devices.json"]


@pytest.mark.parametrize("operation", ["create_code", "pair", "update_info", "revoke"])
def test_failed_atomic_write_keeps_file_and_memory_unchanged(store, tmp_path, monkeypatch, operation):
    device_id, token = paired(store)
    code, _ = store.create_code()
    path = tmp_path / ".ae" / "devices.json"
    before = path.read_bytes()

    def fail_replace(*args):
        raise OSError("simulated write failure")

    with monkeypatch.context() as patch:
        patch.setattr(ae_devices.os, "replace", fail_replace)
        with pytest.raises(OSError, match="simulated write failure"):
            if operation == "create_code":
                store.create_code()
            elif operation == "pair":
                store.pair(code, info())
            elif operation == "update_info":
                store.update_info(device_id, info(fonts=[]))
            else:
                store.revoke(device_id)
    assert path.read_bytes() == before
    assert store.authenticate(token).info == info()
    assert store.pair(code, info()) is not None
    assert sorted(item.name for item in path.parent.iterdir()) == ["devices.json"]


def test_concurrent_pairing_consumes_code_once(store, tmp_path):
    code, _ = store.create_code()
    with ThreadPoolExecutor(max_workers=8) as executor:
        results = list(executor.map(lambda _: store.pair(code, info()), range(16)))
    assert sum(result is not None for result in results) == 1
    assert len(store.list()) == 1
    assert len(Devices(tmp_path).list()) == 1


@pytest.mark.parametrize("contents", [b"{broken", b"\xff", b"[]", b"{}",
    b'{"codes": [], "devices": null}', b'{"codes": [null], "devices": []}',
    b'{"codes": [], "devices": [{}]}'])
def test_corrupt_file_fails_loudly_without_overwriting(tmp_path, contents):
    directory = tmp_path / ".ae"
    directory.mkdir()
    path = directory / "devices.json"
    path.write_bytes(contents)
    with pytest.raises(ValueError, match="^devices file is corrupt$"):
        Devices(tmp_path)
    assert path.read_bytes() == contents


def test_final_font_cap_keeps_order_and_drops_invalid_kept_entries(store, tmp_path):
    from tests.ae_fake_runner import run_jsx
    from tests.test_ae_host_sync import HOST

    state = tmp_path / "ae.json"
    state.write_text(json.dumps({"app": {"fonts": [[
        {"familyName": f"Font {i}", "styleName": "Regular", "postScriptName": f"Font-{i}"}
        for i in range(5002)]]}}))
    host_info = run_jsx(state, HOST, "kfInfo", "true")["value"]
    fonts = host_info["fonts"]
    fonts[1]["family"] = None
    fonts[2]["style"] = "x" * 257
    fonts[3] = None
    device_id, _ = paired(store, info(fonts=fonts))
    assert store.fonts(device_id) == [fonts[0], *fonts[4:5000]]
    store.update_info(device_id, info(fonts=fonts))
    assert Devices(tmp_path).fonts(device_id) == store.fonts(device_id)
