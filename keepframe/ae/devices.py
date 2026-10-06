"""One-time pairing codes and persistent device credentials for After Effects."""

import copy
import hashlib
import hmac
import json
import math
import os
import secrets
import tempfile
import threading
import time
from dataclasses import dataclass, field, replace
from pathlib import Path


_ALPHABET = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"


def _hash(value):
    return hashlib.sha256(value.encode("utf-8", "surrogatepass")).hexdigest()


def _normalise(code):
    return "".join(code.upper().split()).replace("-", "").removeprefix("KF").translate(
        str.maketrans({"I": "1", "L": "1", "O": "0"}))


def _string(value, name, limit, nullable=False):
    if nullable and value is None:
        return None
    if not isinstance(value, str) or len(value) > limit:
        raise ValueError(f"invalid {name}")
    return value


def _info(info):
    if not isinstance(info, dict):
        raise ValueError("invalid info")
    result = {name: _string(info.get(name), name, limit)
              for name, limit in (("ae_version", 32), ("extension_version", 32), ("os", 64))}
    for name in ("host_build", "panel_build"):
        if name in info:
            result[name] = _string(info[name], name, 64)
    fonts = info.get("fonts")
    if not isinstance(fonts, list) or len(fonts) > 5000:
        raise ValueError("invalid fonts")
    result["fonts"] = []
    for font in fonts:
        if not isinstance(font, dict):
            raise ValueError("invalid font")
        result["fonts"].append({name: _string(font.get(name), name, 256, name != "family")
                                for name in ("family", "style", "postscript")})
    return result


def _stored_hash(value):
    if not isinstance(value, str) or len(value) != 64 or any(c not in "0123456789abcdef" for c in value):
        raise ValueError("invalid hash")
    return value


def _timestamp(value):
    if type(value) not in (int, float) or not math.isfinite(value):
        raise ValueError("invalid timestamp")
    return value


@dataclass
class Device:
    id: str
    created: float
    info: dict
    last_seen: float | None = None
    status: dict = field(default_factory=lambda: {"project_name": None, "project_saved": False})


class Devices:
    def __init__(self, workspace):
        self._path = Path(workspace) / ".ae" / "devices.json"
        self._lock = threading.Lock()
        self._codes = []
        self._devices = {}
        with self._lock:
            self._load()
            self._purge()

    def _load(self):
        try:
            data = json.loads(self._path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return
        except (json.JSONDecodeError, UnicodeDecodeError):
            raise ValueError("devices file is corrupt") from None
        try:
            if (not isinstance(data, dict) or set(data) != {"codes", "devices"}
                    or not isinstance(data["codes"], list) or not isinstance(data["devices"], list)):
                raise ValueError("invalid records")
            codes = [{"hash": _stored_hash(item["hash"]), "expires_at": _timestamp(item["expires_at"])}
                     for item in data["codes"]]
            devices = {}
            ids = set()
            for item in data["devices"]:
                token_hash = _stored_hash(item["token_hash"])
                device_id = _string(item["id"], "id", 14)
                if (len(device_id) != 14 or not device_id.startswith("d_")
                        or any(c not in "0123456789abcdef" for c in device_id[2:])
                        or token_hash in devices or device_id in ids):
                    raise ValueError("invalid device")
                devices[token_hash] = Device(device_id, _timestamp(item["created"]), _info(item["info"]))
                ids.add(device_id)
        except (KeyError, TypeError, ValueError, OverflowError):
            raise ValueError("devices file is corrupt") from None
        self._codes, self._devices = codes, devices

    def _purge(self, now=None):
        now = time.time() if now is None else now
        self._codes = [code for code in self._codes if code["expires_at"] > now]
        return now

    def _write(self, codes, devices):
        data = {"codes": codes, "devices": [
            {"id": device.id, "created": device.created, "info": device.info, "token_hash": token_hash}
            for token_hash, device in devices.items()]}
        self._path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        os.chmod(self._path.parent, 0o700)
        fd, temporary = tempfile.mkstemp(dir=self._path.parent, prefix=".devices.", suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as stream:
                os.chmod(temporary, 0o600)
                json.dump(data, stream, separators=(",", ":"))
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, self._path)
        finally:
            Path(temporary).unlink(missing_ok=True)
        self._codes, self._devices = codes, devices

    def _find(self, device_id):
        # ponytail: device-ID scans; add an ID index if large fleets make this slow.
        return next(((key, device) for key, device in self._devices.items() if device.id == device_id),
                    (None, None))

    def create_code(self, now=None) -> tuple[str, float]:
        with self._lock:
            now = self._purge(now)
            symbols = "".join(secrets.choice(_ALPHABET) for _ in range(8))
            code = f"KF-{symbols[:4]}-{symbols[4:]}"
            expires_at = now + 600
            codes = self._codes + [{"hash": _hash(_normalise(code)), "expires_at": expires_at}]
            self._write(sorted(codes, key=lambda item: item["expires_at"])[-5:], self._devices)
            return code, expires_at

    def pair(self, code, info, now=None) -> tuple[str, str] | None:
        with self._lock:
            now = self._purge(now)
            if not isinstance(code, str):
                return None
            code_hash, matched, codes = _hash(_normalise(code)), False, []
            for candidate in self._codes:
                if hmac.compare_digest(code_hash, candidate["hash"]):
                    matched = True
                else:
                    codes.append(candidate)
            if not matched:
                return None
            details = _info(info)
            device_id, token = "d_" + secrets.token_hex(6), secrets.token_urlsafe(32)
            devices = self._devices | {_hash(token): Device(device_id, now, details)}
            self._write(codes, devices)
            return device_id, token

    def authenticate(self, token) -> Device | None:
        with self._lock:
            self._purge()
            return copy.deepcopy(self._devices.get(_hash(token))) if isinstance(token, str) else None

    def touch(self, device_id, status, now=None):
        with self._lock:
            now = self._purge(now)
            if not isinstance(status, dict) or not isinstance(status.get("project_saved"), bool):
                raise ValueError("invalid status")
            clean = {"project_name": _string(status.get("project_name"), "project_name", 256, True),
                     "project_saved": status["project_saved"]}
            _, device = self._find(device_id)
            if device is not None:
                device.last_seen, device.status = now, clean

    def seen(self, device_id, now=None):
        with self._lock:
            now = self._purge(now)
            _, device = self._find(device_id)
            if device is not None:
                device.last_seen = now

    def update_info(self, device_id, info):
        with self._lock:
            self._purge()
            details = _info(info)
            key, device = self._find(device_id)
            if device is not None:
                self._write(self._codes, self._devices | {key: replace(device, info=details)})

    def fonts(self, device_id) -> list[dict] | None:
        with self._lock:
            self._purge()
            _, device = self._find(device_id)
            return copy.deepcopy(device.info["fonts"]) if device is not None else None

    def list(self, now=None) -> list[dict]:
        with self._lock:
            now = self._purge(now)
            return [{"id": device.id, "created": device.created, "last_seen": device.last_seen,
                     "ae_version": device.info["ae_version"], "extension_version": device.info["extension_version"],
                     "host_build": device.info.get("host_build"), "panel_build": device.info.get("panel_build"),
                     "os": device.info["os"], **device.status,
                     "connected": device.last_seen is not None and now - device.last_seen < 40}
                    for device in sorted(self._devices.values(), key=lambda device: device.created)]

    def revoke(self, device_id) -> bool:
        with self._lock:
            self._purge()
            key, device = self._find(device_id)
            if device is None:
                return False
            devices = self._devices.copy()
            del devices[key]
            self._write(self._codes, devices)
            return True
