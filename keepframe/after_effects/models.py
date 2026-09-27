from __future__ import annotations

import hashlib
import json
import math
import re
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator


_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,255}$")


class _StrictRecord(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class _FrozenRecord(_StrictRecord):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)


def _finite_json(value: Any) -> Any:
    """Reject values which cannot be represented by canonical JSON."""
    try:
        json.dumps(value, ensure_ascii=False, allow_nan=False, sort_keys=True, separators=(",", ":"))
    except (TypeError, ValueError, OverflowError) as exc:
        raise ValueError("value must be finite JSON") from exc
    return value


def canonical_json(value: Any) -> bytes:
    """Return the one JSON encoding used for all persisted digests."""
    _finite_json(value)
    return json.dumps(
        value,
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def json_digest(value: Any) -> str:
    return hashlib.sha256(canonical_json(value)).hexdigest()


def _nonempty(value: str) -> str:
    if value is None:
        return value
    if not isinstance(value, str) or not value or len(value) > 4096:
        raise ValueError("value must be a non-empty string of at most 4096 characters")
    if "\x00" in value:
        raise ValueError("value contains NUL")
    return value


def _identifier(value: str) -> str:
    if value is None:
        return value
    if not isinstance(value, str) or not _ID_RE.fullmatch(value):
        raise ValueError("identifier is invalid")
    return value


def _digest(value: str) -> str:
    if value is None:
        return value
    if not isinstance(value, str) or not _SHA256_RE.fullmatch(value):
        raise ValueError("digest must be a lowercase SHA-256 digest")
    return value


def _bounded_int(value: int, *, minimum: int = 0, maximum: int = 1_000_000) -> int:
    if value < minimum or value > maximum:
        raise ValueError(f"integer must be between {minimum} and {maximum}")
    return value


class AEPlugin(_FrozenRecord):
    name: str
    version: str | None = None
    sha256: str | None = None

    _name = field_validator("name", "version")(_nonempty)
    _sha = field_validator("sha256")(_digest)


class AEFont(_FrozenRecord):
    family: str
    style: str | None = None
    version: str | None = None
    sha256: str | None = None

    _family = field_validator("family", "style", "version")(_nonempty)
    _sha = field_validator("sha256")(_digest)


class AECapabilities(_FrozenRecord):
    """The connector capability snapshot bound into an approved render plan."""

    ae_version: str
    os_version: str | None = None
    fonts: tuple[AEFont | dict[str, Any], ...] = ()
    plugins: tuple[AEPlugin | dict[str, Any], ...] = ()
    effects: tuple[dict[str, Any], ...] = ()
    property_schemas: tuple[dict[str, Any], ...] = ()
    # These scalar catalogs are useful for a connector that only reports names.
    font_names: tuple[str, ...] = ()
    effect_names: tuple[str, ...] = ()
    plugin_versions: tuple[dict[str, Any], ...] = ()
    version: str | None = None
    digest: str | None = None
    capability_hash: str | None = None

    _ae_version = field_validator("ae_version", "os_version", "version")(_nonempty)
    _digest_fields = field_validator("digest", "capability_hash")(_digest)
    _json_fields = field_validator("effects", "property_schemas", "plugin_versions")(_finite_json)
    _names = field_validator("font_names", "effect_names")(
        lambda values: tuple(_nonempty(item) for item in values)
    )




class AESubstitution(_FrozenRecord):
    source_element_id: str
    source_type: str
    proposed_layers: tuple[str | dict[str, Any], ...] = ()
    proposed_effects: tuple[str | dict[str, Any], ...] = ()
    lost_semantics: tuple[str, ...] = ()
    reason: str | None = None
    acknowledged: bool = False

    _source_id = field_validator("source_element_id", "source_type")(_identifier)
    _reason = field_validator("reason")(_nonempty)
    _lost = field_validator("lost_semantics")(
        lambda values: tuple(_nonempty(item) for item in values)
    )
    _proposed = field_validator("proposed_layers", "proposed_effects")(_finite_json)




class AECheckpoint(_FrozenRecord):
    index: int
    provenance: Literal["baseline", "agent", "manual"]
    passed: bool
    aep_artifact_id: str
    preview_artifact_id: str
    frame_artifact_ids: tuple[str, ...] = ()
    inspection: dict[str, Any] = Field(default_factory=dict)
    operations: tuple[dict[str, Any], ...] = ()
    verifier_report: dict[str, Any] = Field(default_factory=dict)
    model_response: dict[str, Any] = Field(default_factory=dict)
    capability_manifest: dict[str, Any] = Field(default_factory=dict)
    dependency_manifest: dict[str, Any] = Field(default_factory=dict)
    failure: str | None = None

    _index = field_validator("index")(
        lambda value: _bounded_int(value, minimum=0, maximum=1_000_000)
    )
    _artifacts = field_validator("aep_artifact_id", "preview_artifact_id")(_identifier)
    _frame_artifacts = field_validator("frame_artifact_ids")(
        lambda values: tuple(_identifier(item) for item in values)
    )
    _json_fields = field_validator(
        "inspection",
        "operations",
        "verifier_report",
        "model_response",
        "capability_manifest",
        "dependency_manifest",
    )(_finite_json)
    _failure = field_validator("failure")(_nonempty)




_SESSION_STATUSES = {
    "awaiting_approval",
    "waiting_for_connector",
    "baseline",
    "iterating",
    "pause_requested",
    "manual_edit",
    "finalizing",
    "done",
    "failed",
}


def _session_status(value: str) -> str:
    if not isinstance(value, str) or not value:
        raise ValueError("session status is required")
    if value not in _SESSION_STATUSES and not value.startswith("paused:"):
        raise ValueError("unknown session status")
    return value


class AESession(_FrozenRecord):
    id: str
    project_id: str
    plan_id: str
    plan_digest: str
    execution_id: str
    status: str = "waiting_for_connector"
    revision: int = 0
    device_id: str | None = None
    checkpoints: tuple[AECheckpoint, ...] = ()
    selected_checkpoint: int | None = None
    command_sequence: int = 0
    applied_command_sequence: int = 0
    last_command_id: str | None = None
    reason: str | None = None
    created_at: float = 0.0
    updated_at: float = 0.0

    _ids = field_validator("id", "project_id", "plan_id", "execution_id")(_identifier)
    _plan_digest = field_validator("plan_digest")(_digest)
    _status = field_validator("status")(_session_status)
    _revisions = field_validator("revision", "command_sequence", "applied_command_sequence")(
        lambda value: _bounded_int(value, minimum=0)
    )
    _device = field_validator("device_id", "last_command_id")(_identifier)
    _reason = field_validator("reason")(_nonempty)
    _timestamps = field_validator("created_at", "updated_at")(
        lambda value: value if math.isfinite(value) and value >= 0 else (_ for _ in ()).throw(ValueError("timestamp is invalid"))
    )




CommandKind = Literal[
    "heartbeat",
    "create_project",
    "open_project",
    "import_asset",
    "apply_batch",
    "inspect_layers",
    "save_checkpoint",
    "render_preview",
    "render_final",
    "package_project",
    "sync_manual",
]
CommandStatus = Literal["queued", "leased", "completed", "failed", "revoked"]


class AECommand(_FrozenRecord):
    id: str
    nonce: str
    project_id: str
    plan_id: str
    session_id: str
    device_id: str
    kind: CommandKind
    sequence: int
    expected_state: str
    expected_checkpoint: int | None = None
    payload: dict[str, Any] = Field(default_factory=dict)
    payload_digest: str
    lease_seconds: float = 30.0
    lease_expires_at: float | None = None
    status: CommandStatus = "queued"
    result: dict[str, Any] | None = None
    result_digest: str | None = None
    created_at: float = 0.0
    delivered_at: float | None = None

    _ids = field_validator("id", "nonce", "project_id", "plan_id", "session_id", "device_id")(_identifier)
    _sequence = field_validator("sequence")(
        lambda value: _bounded_int(value, minimum=1, maximum=1_000_000_000)
    )
    _state = field_validator("expected_state")(_session_status)
    _checkpoint = field_validator("expected_checkpoint")(
        lambda value: value if value is None else _bounded_int(value, minimum=0)
    )
    _payload = field_validator("payload")(_finite_json)
    _payload_digest = field_validator("payload_digest")(_digest)
    _result = field_validator("result")(_finite_json)
    _result_digest = field_validator("result_digest")(_digest)
    _lease = field_validator("lease_seconds")(
        lambda value: value if math.isfinite(value) and 0 < value <= 86_400 else (_ for _ in ()).throw(ValueError("lease is invalid"))
    )
    _lease_expiry = field_validator("lease_expires_at", "delivered_at", "created_at")(
        lambda value: value if value is None or math.isfinite(value) and value >= 0 else (_ for _ in ()).throw(ValueError("timestamp is invalid"))
    )




class AECommandResult(_FrozenRecord):
    command_id: str
    device_id: str
    sequence: int
    result: dict[str, Any] = Field(default_factory=dict)
    result_digest: str
    accepted_at: float = 0.0

    _ids = field_validator("command_id", "device_id")(_identifier)
    _sequence = field_validator("sequence")(
        lambda value: _bounded_int(value, minimum=1, maximum=1_000_000_000)
    )
    _result = field_validator("result")(_finite_json)
    _digest_field = field_validator("result_digest")(_digest)
    _accepted = field_validator("accepted_at")(
        lambda value: value if math.isfinite(value) and value >= 0 else (_ for _ in ()).throw(ValueError("timestamp is invalid"))
    )




ArtifactKind = Literal["png", "mp4", "aep", "zip"]
ArtifactStatus = Literal["reserved", "committed"]


class AEArtifactReservation(_FrozenRecord):
    id: str
    plan_id: str
    session_id: str
    kind: ArtifactKind
    max_length: int
    status: ArtifactStatus = "reserved"
    sha256: str | None = None
    length: int | None = None
    created_at: float = 0.0

    _ids = field_validator("id", "plan_id", "session_id")(_identifier)
    _max_length = field_validator("max_length")(
        lambda value: _bounded_int(value, minimum=1, maximum=4_294_967_296)
    )
    _digest_field = field_validator("sha256")(_digest)
    _length = field_validator("length")(
        lambda value: value if value is None or _bounded_int(value, minimum=0, maximum=4_294_967_296) else value
    )
    _created = field_validator("created_at")(
        lambda value: value if math.isfinite(value) and value >= 0 else (_ for _ in ()).throw(ValueError("timestamp is invalid"))
    )




class AEPublishedArtifact(_FrozenRecord):
    id: str
    reservation_id: str
    plan_id: str
    session_id: str
    kind: ArtifactKind
    length: int
    sha256: str
    mime_type: str

    _ids = field_validator("id", "reservation_id", "plan_id", "session_id")(_identifier)
    _length = field_validator("length")(
        lambda value: _bounded_int(value, minimum=0, maximum=4_294_967_296)
    )
    _digest_field = field_validator("sha256")(_digest)
    _mime = field_validator("mime_type")(_nonempty)




__all__ = [
    "AEArtifactReservation",
    "AECheckpoint",
    "AECommand",
    "AECommandResult",
    "AECapabilities",
    "AEFont",
    "AEPlugin",
    "AEPublishedArtifact",
    "AESession",
    "AESubstitution",
    "ArtifactKind",
    "CommandKind",
    "canonical_json",
    "json_digest",
]
