from __future__ import annotations

import hashlib
import hmac
import math
import re
import secrets
import time
from collections.abc import Generator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, ValidationError, field_validator

from ..render.plan import _atomic_write, _state_lock

from .models import canonical_json


_PROJECT_ID = re.compile(r"^[A-Za-z0-9_-]{1,128}$")
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_PAIRING_TTL_SECONDS = 600.0
_DUMMY_HASH = "0" * 64


class AEAuthError(RuntimeError):
    pass

class AEControllerAuthorizationError(AEAuthError):
    """The supplied browser controller credential is invalid."""


class _AuthRecord(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)


class _PendingPairing(_AuthRecord):
    code_hash: str
    controller_hash: str
    capability_request: dict[str, Any]
    capability_request_hash: str
    created_at: float
    expires_at: float

    @field_validator("code_hash", "controller_hash", "capability_request_hash")
    @classmethod
    def _digest(cls, value: str) -> str:
        if not _SHA256.fullmatch(value):
            raise ValueError("credential digest is invalid")
        return value

    @field_validator("created_at", "expires_at")
    @classmethod
    def _time(cls, value: float) -> float:
        if not math.isfinite(value) or value < 0:
            raise ValueError("credential timestamp is invalid")
        return value


class _DeviceCredential(_AuthRecord):
    id: str
    token_hash: str
    controller_hash: str
    capability_request_hash: str
    paired_at: float
    status: Literal["active", "draining"] = "active"

    @field_validator("id")
    @classmethod
    def _id(cls, value: str) -> str:
        if not re.fullmatch(r"device-[A-Za-z0-9_-]{16,64}", value):
            raise ValueError("device id is invalid")
        return value

    @field_validator("token_hash", "controller_hash", "capability_request_hash")
    @classmethod
    def _digest(cls, value: str) -> str:
        if not _SHA256.fullmatch(value):
            raise ValueError("credential digest is invalid")
        return value

    @field_validator("paired_at")
    @classmethod
    def _time(cls, value: float) -> float:
        if not math.isfinite(value) or value < 0:
            raise ValueError("credential timestamp is invalid")
        return value


class _AuthState(_AuthRecord):
    project_id: str
    revision: int = 0
    controller_hash: str | None = None
    pending: _PendingPairing | None = None
    device: _DeviceCredential | None = None
    revoking: bool = False

    @field_validator("project_id")
    @classmethod
    def _project(cls, value: str) -> str:
        if not _PROJECT_ID.fullmatch(value):
            raise ValueError("project id is invalid")
        return value

    @field_validator("revision")
    @classmethod
    def _revision(cls, value: int) -> int:
        if value < 0:
            raise ValueError("auth revision is invalid")
        return value

    @field_validator("controller_hash")
    @classmethod
    def _controller_digest(cls, value: str | None) -> str | None:
        if value is not None and not _SHA256.fullmatch(value):
            raise ValueError("credential digest is invalid")
        return value


@dataclass(frozen=True)
class AEPairingGrant:
    code: str
    controller_token: str
    expires_at: float


@dataclass(frozen=True)
class AEDeviceIdentity:
    project_id: str
    device_id: str
    status: Literal["active", "draining"] = "active"


@dataclass(frozen=True)
class AEDeviceGrant:
    identity: AEDeviceIdentity
    token: str
    capability_request: dict[str, Any]


def _hash_secret(secret: str) -> str:
    return hashlib.sha256(secret.encode("utf-8")).hexdigest()


def _secret_matches(secret: str | None, expected_hash: str | None) -> bool:
    candidate = secret if isinstance(secret, str) and len(secret) <= 512 else ""
    return hmac.compare_digest(_hash_secret(candidate), expected_hash or _DUMMY_HASH)


def controller_cookie_name(project_id: str) -> str:
    if not _PROJECT_ID.fullmatch(project_id):
        raise AEAuthError("project id is invalid")
    return f"keepframe_ae_controller_{project_id}"


class AEProjectAuth:
    def __init__(self, project_root: Path, project_id: str) -> None:
        if not _PROJECT_ID.fullmatch(project_id):
            raise AEAuthError("project id is invalid")
        root = Path(project_root)
        try:
            resolved = root.resolve(strict=True)
        except OSError as exc:
            raise AEAuthError("project authorization failed") from exc
        if root.is_symlink() or not resolved.is_dir():
            raise AEAuthError("project authorization failed")
        self.root: Path = resolved
        self.project_id: str = project_id
        self.path: Path = resolved / "ae-auth.json"

    def _load_unlocked(self) -> _AuthState:
        if not self.path.exists():
            return _AuthState(project_id=self.project_id)
        if self.path.is_symlink() or not self.path.is_file():
            raise AEAuthError("project authorization failed")
        try:
            state = _AuthState.model_validate_json(self.path.read_bytes())
        except (OSError, ValidationError) as exc:
            raise AEAuthError("project authorization failed") from exc
        if state.project_id != self.project_id:
            raise AEAuthError("project authorization failed")
        return state

    def _write_unlocked(self, state: _AuthState) -> None:
        if self.path.is_symlink():
            raise AEAuthError("project authorization failed")
        try:
            _atomic_write(self.path, canonical_json(state.model_dump(mode="json")))
        except OSError as exc:
            raise AEAuthError("project authorization failed") from exc

    def _refresh_expired_unlocked(
        self,
        state: _AuthState,
        current_time: float,
    ) -> _AuthState:
        pending = state.pending
        if pending is None or pending.expires_at >= current_time:
            return state
        device = state.device
        if device is not None and device.status == "draining" and not state.revoking:
            device = device.model_copy(update={"status": "active"})
        refreshed = state.model_copy(
            update={
                "revision": state.revision + 1,
                "pending": None,
                "device": device,
            }
        )
        self._write_unlocked(refreshed)
        return refreshed

    @staticmethod
    def _capability_request(value: object) -> tuple[dict[str, Any], str]:
        if not isinstance(value, Mapping):
            raise AEAuthError("capability request is invalid")
        request = dict(value)
        try:
            encoded = canonical_json(request)
        except (TypeError, ValueError, OverflowError) as exc:
            raise AEAuthError("capability request is invalid") from exc
        if len(encoded) > 64 * 1024:
            raise AEAuthError("capability request is too large")
        return request, hashlib.sha256(encoded).hexdigest()

    def create_pairing(
        self,
        controller_token: str | None,
        capability_request: Mapping[str, Any],
        *,
        now: float | None = None,
    ) -> AEPairingGrant:
        request, request_hash = self._capability_request(capability_request)
        created_at = time.time() if now is None else float(now)
        if not math.isfinite(created_at) or created_at < 0:
            raise AEAuthError("pairing time is invalid")
        with _state_lock(self.path):
            state = self._load_unlocked()
            if state.controller_hash is None:
                controller = secrets.token_urlsafe(32)
            elif _secret_matches(controller_token, state.controller_hash):
                controller = controller_token or ""
            else:
                raise AEControllerAuthorizationError(
                    "controller authorization failed"
                )
            controller_hash = _hash_secret(controller)
            code = secrets.token_urlsafe(24)
            expires_at = created_at + _PAIRING_TTL_SECONDS
            pending = _PendingPairing(
                code_hash=_hash_secret(code),
                controller_hash=controller_hash,
                capability_request=request,
                capability_request_hash=request_hash,
                created_at=created_at,
                expires_at=expires_at,
            )
            device = state.device
            if device is not None:
                device = device.model_copy(
                    update={
                        "status": "draining",
                        "controller_hash": controller_hash,
                    }
                )
            self._write_unlocked(
                state.model_copy(
                    update={
                        "revision": state.revision + 1,
                        "controller_hash": controller_hash,
                        "pending": pending,
                        "device": device,
                        "revoking": False,
                    }
                )
            )
            return AEPairingGrant(code=code, controller_token=controller, expires_at=expires_at)

    def redeem_pairing(self, code: str, *, now: float | None = None) -> AEDeviceGrant:
        current_time = time.time() if now is None else float(now)
        if not math.isfinite(current_time) or current_time < 0:
            raise AEAuthError("pairing time is invalid")
        with _state_lock(self.path):
            state = self._refresh_expired_unlocked(self._load_unlocked(), current_time)
            pending = state.pending
            valid = (
                pending is not None
                and hmac.compare_digest(pending.controller_hash, state.controller_hash or _DUMMY_HASH)
                and _secret_matches(code, pending.code_hash)
            )
            if not valid or pending is None:
                raise AEAuthError("pairing code is invalid or expired")
            token = secrets.token_urlsafe(32)
            device_id = "device-" + secrets.token_urlsafe(16)
            identity = AEDeviceIdentity(project_id=self.project_id, device_id=device_id)
            credential = _DeviceCredential(
                id=device_id,
                token_hash=_hash_secret(token),
                controller_hash=pending.controller_hash,
                capability_request_hash=pending.capability_request_hash,
                paired_at=current_time,
                status="active",
            )
            self._write_unlocked(
                state.model_copy(
                    update={
                        "revision": state.revision + 1,
                        "pending": None,
                        "device": credential,
                        "revoking": False,
                    }
                )
            )
            return AEDeviceGrant(
                identity=identity,
                token=token,
                capability_request=dict(pending.capability_request),
            )

    def controller_configured(self) -> bool:
        with _state_lock(self.path):
            return self._load_unlocked().controller_hash is not None


    def authenticate_controller(self, token: str | None) -> bool:
        try:
            with _state_lock(self.path):
                state = self._load_unlocked()
                return _secret_matches(token, state.controller_hash)
        except AEAuthError:
            _ = _secret_matches(token, None)
            return False

    @contextmanager
    def authorize_device(
        self,
        token: str | None,
        *,
        allow_draining: bool,
        now: float | None = None,
    ) -> Generator[AEDeviceIdentity | None]:
        try:
            current_time = time.time() if now is None else float(now)
        except (TypeError, ValueError) as exc:
            raise AEAuthError("authentication time is invalid") from exc
        if not math.isfinite(current_time) or current_time < 0:
            raise AEAuthError("authentication time is invalid")
        with _state_lock(self.path):
            state = self._refresh_expired_unlocked(self._load_unlocked(), current_time)
            device = state.device
            if (
                not _secret_matches(token, device.token_hash if device is not None else None)
                or device is None
                or not hmac.compare_digest(
                    device.controller_hash,
                    state.controller_hash or _DUMMY_HASH,
                )
                or (device.status == "draining" and not allow_draining)
            ):
                yield None
                return
            yield AEDeviceIdentity(
                project_id=self.project_id,
                device_id=device.id,
                status=device.status,
            )

    def authenticate_device(
        self,
        token: str | None,
        *,
        allow_draining: bool = True,
        now: float | None = None,
    ) -> AEDeviceIdentity | None:
        try:
            with self.authorize_device(
                token,
                allow_draining=allow_draining,
                now=now,
            ) as identity:
                return identity
        except AEAuthError:
            _ = _secret_matches(token, None)
            return None

    def active_device_id(self) -> str | None:
        with _state_lock(self.path):
            state = self._load_unlocked()
            return state.device.id if state.device is not None else None

    def begin_unpair(self, controller_token: str | None) -> str | None:
        with _state_lock(self.path):
            state = self._load_unlocked()
            if not _secret_matches(controller_token, state.controller_hash):
                raise AEAuthError("controller authorization failed")
            device = state.device
            device_id = device.id if device is not None else None
            if device is not None:
                device = device.model_copy(update={"status": "draining"})
            self._write_unlocked(
                state.model_copy(
                    update={
                        "revision": state.revision + 1,
                        "pending": None,
                        "device": device,
                        "revoking": True,
                    }
                )
            )
            return device_id

    def finish_unpair(
        self,
        controller_token: str | None,
        device_id: str | None,
    ) -> str | None:
        with _state_lock(self.path):
            state = self._load_unlocked()
            if (
                not _secret_matches(controller_token, state.controller_hash)
                or not state.revoking
                or (state.device.id if state.device is not None else None) != device_id
            ):
                raise AEAuthError("controller authorization failed")
            self._write_unlocked(
                state.model_copy(
                    update={
                        "revision": state.revision + 1,
                        "controller_hash": None,
                        "pending": None,
                        "device": None,
                        "revoking": False,
                    }
                )
            )
            return device_id


__all__ = [
    "AEAuthError",
    "AEControllerAuthorizationError",
    "AEDeviceGrant",
    "AEDeviceIdentity",
    "AEPairingGrant",
    "AEProjectAuth",
    "controller_cookie_name",
]
