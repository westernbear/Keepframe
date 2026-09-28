from __future__ import annotations

import hashlib
import json
import math
import re
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


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


def _digest(value: str | None) -> str | None:
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


def _bounded_text(value: str) -> str:
    if not isinstance(value, str) or len(value) > 4096 or "\x00" in value:
        raise ValueError("text is invalid")
    return value


def _optional_bounded_text(value: str | None) -> str | None:
    if value is None:
        return None
    value = _bounded_text(value)
    if not value.strip():
        raise ValueError("text must not be blank")
    return value


class AEFont(_FrozenRecord):
    match_name: str
    family: str = ""
    style: str = ""
    version: str | None = None
    version_or_hash: str | None = None
    sha256: str | None = None

    _match_name = field_validator("match_name")(_nonempty)
    _text = field_validator("family", "style")(_bounded_text)
    _versions = field_validator("version", "version_or_hash")(_optional_bounded_text)
    _sha = field_validator("sha256")(_digest)

    @model_validator(mode="after")
    def _exact_identity(self) -> "AEFont":
        if self.version_or_hash is None and self.sha256 is None:
            raise ValueError("font requires a version or hash")
        return self


class AEEffect(_FrozenRecord):
    match_name: str
    display_name: str = ""
    version: str | None = None
    version_or_hash: str | None = None
    properties: dict[str, str] = Field(default_factory=dict)

    _match_name = field_validator("match_name")(_nonempty)
    _text = field_validator("display_name")(_bounded_text)
    _versions = field_validator("version", "version_or_hash")(_optional_bounded_text)

    @field_validator("properties")
    @classmethod
    def _properties(cls, value: dict[str, str]) -> dict[str, str]:
        if len(value) > 100_000:
            raise ValueError("effect properties are too large")
        for name, schema in value.items():
            _nonempty(name)
            _nonempty(schema)
        return dict(value)


class AECapabilityCatalog(_FrozenRecord):
    font_names: tuple[str, ...] = ()
    fonts: tuple[AEFont, ...] = ()
    effect_names: tuple[str, ...] = ()
    effects: tuple[AEEffect, ...] = ()
    property_schemas: dict[str, str] = Field(default_factory=dict)
    # The panel currently emits both spellings.  Persist both after checking
    # they describe the same object so the wire snapshot remains lossless.
    properties: dict[str, str] = Field(default_factory=dict)
    plugin_versions: dict[str, str | None] = Field(default_factory=dict)

    @field_validator("font_names", "effect_names", mode="before")
    @classmethod
    def _name_sequence(cls, values: Any) -> Any:
        if isinstance(values, list):
            return tuple(values)
        return values

    @field_validator("font_names", "effect_names")
    @classmethod
    def _names(cls, values: tuple[str, ...]) -> tuple[str, ...]:
        if len(values) > 100_000:
            raise ValueError("capability names are too large")
        normalized = tuple(_nonempty(item) for item in values)
        if len(set(normalized)) != len(normalized):
            raise ValueError("capability names contain duplicates")
        return normalized
    @field_validator("fonts", "effects", mode="before")
    @classmethod
    def _record_sequence(cls, values: Any) -> Any:
        if isinstance(values, list):
            return tuple(values)
        return values

    @staticmethod
    def _schemas(value: dict[str, str]) -> dict[str, str]:
        if len(value) > 100_000:
            raise ValueError("property schemas are too large")
        for name, schema in value.items():
            _nonempty(name)
            _nonempty(schema)
        return dict(value)

    @field_validator("property_schemas", "properties")
    @classmethod
    def _property_schemas(cls, value: dict[str, str]) -> dict[str, str]:
        return cls._schemas(value)

    @field_validator("plugin_versions")
    @classmethod
    def _plugin_versions(cls, value: dict[str, str | None]) -> dict[str, str | None]:
        if len(value) > 100_000:
            raise ValueError("plugin versions are too large")
        for name, version in value.items():
            _nonempty(name)
            _optional_bounded_text(version)
        return dict(value)

    @model_validator(mode="after")
    def _normalize_catalog(self) -> "AECapabilityCatalog":
        if self.property_schemas and self.properties and self.property_schemas != self.properties:
            raise ValueError("property schema aliases differ")
        if not self.property_schemas and self.properties:
            object.__setattr__(self, "property_schemas", dict(self.properties))
        elif not self.properties and self.property_schemas:
            object.__setattr__(self, "properties", dict(self.property_schemas))
        font_metadata = [font.match_name for font in self.fonts]
        effect_metadata = [effect.match_name for effect in self.effects]
        if len(set(font_metadata)) != len(font_metadata):
            raise ValueError("font metadata contains duplicates")
        if len(set(effect_metadata)) != len(effect_metadata):
            raise ValueError("effect metadata contains duplicates")
        if set(font_metadata) != set(self.font_names):
            raise ValueError("font names do not match font metadata")
        if set(effect_metadata) != set(self.effect_names):
            raise ValueError("effect names do not match effect metadata")
        object.__setattr__(self, "font_names", tuple(sorted(self.font_names)))
        object.__setattr__(
            self,
            "fonts",
            tuple(sorted(self.fonts, key=lambda item: item.match_name)),
        )
        object.__setattr__(self, "effect_names", tuple(sorted(self.effect_names)))
        object.__setattr__(
            self,
            "effects",
            tuple(sorted(self.effects, key=lambda item: item.match_name)),
        )
        return self


class AECapabilities(_FrozenRecord):
    """Canonical wire snapshot returned by the installed AE panel."""

    version: str
    major: int
    host: str
    ready: bool
    project_open: bool
    capabilities: AECapabilityCatalog
    timestamp: float | None = None
    capability_hash: str | None = None

    _version = field_validator("version")(_nonempty)

    @field_validator("timestamp")
    @classmethod
    def _time(cls, value: float | None) -> float | None:
        if value is not None and (
            not isinstance(value, (int, float))
            or isinstance(value, bool)
            or not math.isfinite(value)
            or value < 0
        ):
            raise ValueError("heartbeat timestamp is invalid")
        return value

    @field_validator("major")
    @classmethod
    def _major(cls, value: int) -> int:
        return _bounded_int(value, minimum=0, maximum=1_000_000)

    @field_validator("host")
    @classmethod
    def _host(cls, value: str) -> str:
        if value != "after-effects":
            raise ValueError("heartbeat host is invalid")
        return value

    @field_validator("capability_hash")
    @classmethod
    def _hash(cls, value: str | None) -> str | None:
        return _digest(value)

    @model_validator(mode="after")
    def _canonical_hash(self) -> "AECapabilities":
        payload = self.model_dump(
            mode="json",
            exclude={"capability_hash", "project_open", "timestamp"},
        )
        encoded = canonical_json(payload)
        if len(encoded) > 1 * 1024 * 1024:
            raise ValueError("capability snapshot exceeds 1 MiB")
        digest = hashlib.sha256(encoded).hexdigest()
        if self.capability_hash is not None and self.capability_hash != digest:
            raise ValueError("capability hash does not match snapshot")
        object.__setattr__(self, "capability_hash", digest)
        return self

    @classmethod
    def from_heartbeat(cls, value: Any) -> "AECapabilities":
        return cls.model_validate(value)

    @property
    def ae_version(self) -> str:
        return self.version

    @property
    def digest(self) -> str:
        return self.capability_hash or ""



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
    lineage_valid: bool = True
    aep_artifact_id: str
    preview_artifact_id: str
    frame_artifact_ids: tuple[str, ...] = ()
    # The digest is a server-side pointer used in compact session summaries.
    # It is deliberately excluded from the context bytes it identifies.
    context_digest: str | None = Field(default=None, exclude=True)
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
    _context_digest = field_validator("context_digest")(_digest)
    _json_fields = field_validator(
        "inspection",
        "operations",
        "verifier_report",
        "model_response",
        "capability_manifest",
        "dependency_manifest",
    )(_finite_json)
    _failure = field_validator("failure")(_nonempty)


class AECheckpointContext(_FrozenRecord):
    """Server-owned checkpoint metadata bound to one save command."""

    digest: str
    command_id: str
    plan_digest: str
    session_id: str
    checkpoint: AECheckpoint

    _digest_field = field_validator("digest", "plan_digest")(_digest)
    _ids = field_validator("command_id", "session_id")(_identifier)

    @model_validator(mode="after")
    def _digest_matches_checkpoint(self) -> AECheckpointContext:
        if self.digest != json_digest(self.checkpoint.model_dump(mode="json")):
            raise ValueError("checkpoint context digest does not match checkpoint")
        return self




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
    checkpoint_required: bool = False
    # Baseline progress is persisted separately from checkpoint pass/fail so a
    # partial checkpoint cannot make a later Continue skip deterministic work.
    baseline_mapping_digest: str | None = None
    baseline_batch_count: int | None = None
    baseline_completed_batches: tuple[int, ...] = ()
    baseline_complete: bool = False
    manual_epoch: int = 0
    # Set only by the browser's explicit Sync Manual mutation.  Polling may
    # reopen a checkpoint but must not create a sync attempt on its own.
    manual_sync_requested: bool = False
    manual_attempt_id: str | None = None
    open_checkpoint: int | None = None
    # Source-less AE layer IDs are never recycled after a removal.  Keeping the
    # tombstones in the durable session makes the rule survive process restart.
    issued_instance_id_tombstones: tuple[str, ...] = ()
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
    _baseline_digest = field_validator("baseline_mapping_digest")(_digest)
    _baseline_count = field_validator("baseline_batch_count")(
        lambda value: value if value is None else _bounded_int(value, minimum=0)
    )

    @field_validator("baseline_completed_batches")
    @classmethod
    def _baseline_batch_indexes(cls, values: tuple[int, ...]) -> tuple[int, ...]:
        normalized = tuple(_bounded_int(value, minimum=0) for value in values)
        if len(set(normalized)) != len(normalized):
            raise ValueError("baseline batch indexes contain duplicates")
        return tuple(sorted(normalized))

    _manual_epoch = field_validator("manual_epoch")(
        lambda value: _bounded_int(value, minimum=0)
    )
    _manual_attempt = field_validator("manual_attempt_id")(_identifier)
    _open_checkpoint = field_validator("open_checkpoint")(
        lambda value: value if value is None else _bounded_int(value, minimum=0)
    )

    @model_validator(mode="after")
    def _baseline_progress_is_consistent(self) -> AESession:
        if self.baseline_batch_count is not None and any(
            index >= self.baseline_batch_count
            for index in self.baseline_completed_batches
        ):
            raise ValueError("baseline batch index exceeds batch count")
        if self.baseline_complete and self.baseline_batch_count is not None:
            if set(self.baseline_completed_batches) != set(
                range(self.baseline_batch_count)
            ):
                raise ValueError("baseline completeness is inconsistent")
        return self


    @field_validator("issued_instance_id_tombstones")
    @classmethod
    def _instance_tombstones(cls, values: tuple[str, ...]) -> tuple[str, ...]:
        normalized = tuple(_identifier(value) for value in values)
        if len(set(normalized)) != len(normalized):
            raise ValueError("instance id tombstones contain duplicates")
        return normalized

    @property
    def instance_id_tombstones(self) -> tuple[str, ...]:
        return self.issued_instance_id_tombstones

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
    plan_digest: str
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
    _plan_digest = field_validator("plan_digest", "payload_digest")(_digest)
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
    "AECheckpointContext",
    "AECommand",
    "AECommandResult",
    "AECapabilities",
    "AECapabilityCatalog",
    "AEEffect",
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
