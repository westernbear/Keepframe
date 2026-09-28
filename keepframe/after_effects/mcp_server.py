from __future__ import annotations

"""First-party MCP façade for the local AE bridge.

The MCP SDK is optional.  Importing this module is intentionally side-effect
free so native Keepframe deployments do not need the optional dependency.
"""
import asyncio
import importlib
import os
import re


from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any, cast

from pydantic import BaseModel, ConfigDict

from .bridge import Bridge, BridgeResult, FIXED_KINDS
from .operations import (
    ApprovedCapabilities,
    canonicalize_operation_context,
    validate_operation_batch,
)


class _DirectPayloadArguments(BaseModel):
    """MCP v2 argument model accepting the fixed command envelope object."""
    model_config = ConfigDict(extra="allow")

    def model_dump_one_level(self) -> dict[str, Any]:
        return dict(self.__pydantic_extra__ or {})


TOOL_NAMES: tuple[str, ...] = (
    "capability_heartbeat",
    "create_or_open_project",
    "import_server_asset",
    "apply_operation_batch",
    "inspect_mapped_layers",
    "save_checkpoint",
    "render_preview",
    "render_final",
    "package_project",
)
if frozenset(TOOL_NAMES) != FIXED_KINDS or len(TOOL_NAMES) != 9:  # pragma: no cover - module invariant
    raise RuntimeError("MCP tool registry is not the fixed nine-tool surface")

ToolForwarder = Callable[[Mapping[str, Any] | None], Any]
_ENVELOPE_FIELDS = frozenset({"command_id", "nonce", "payload"})
_APPLY_FIELDS = frozenset(
    {
        "batch",
        "approved_capabilities",
        "scene_frame_count",
        "duration",
        "layer_count",
        "baseline",
        "locked_source_ids",
        "layer_sources",
        "layer_native_ids",
        "project_id",
        "plan_id",
        "session_id",
    }
)
_SCOPE_FIELDS = ("project_id", "plan_id", "session_id")
_COMMAND_TIMEOUTS = {
    "apply_operation_batch": 30 * 60.0,
    "inspect_mapped_layers": 30 * 60.0,
    "render_preview": 30 * 60.0,
    "render_final": 30 * 60.0,
    "save_checkpoint": 30 * 60.0,
    "package_project": 15 * 60.0,
}
_SCOPE_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,255}$")

def _canonical_native_layer_ids(value: Any) -> dict[str, int]:
    if not isinstance(value, Mapping):
        raise ValueError("layer_native_ids must be an object")
    if len(value) > 1_000:
        raise ValueError("layer_native_ids exceeds 1000 layers")
    result: dict[str, int] = {}
    seen_native_ids: set[int] = set()
    for instance_id, native_id in value.items():
        if not isinstance(instance_id, str) or not _SCOPE_ID_RE.fullmatch(instance_id):
            raise ValueError("layer_native_ids contains an invalid instance id")
        if (
            not isinstance(native_id, int)
            or isinstance(native_id, bool)
            or native_id < 1
            or native_id > 1_000_000
        ):
            raise ValueError("native layer id must be a positive integer")
        if native_id in seen_native_ids:
            raise ValueError("duplicate native layer id")
        seen_native_ids.add(native_id)
        result[instance_id] = native_id
    return dict(sorted(result.items()))




def _result_value(value: Any) -> Any:
    if not isinstance(value, BridgeResult):
        return value
    if value.ok:
        return value.result
    return {"ok": False, "error": value.error or "AE bridge command failed"}


def _canonical_apply_payload(payload: Mapping[str, Any]) -> dict[str, Any]:
    keys = set(payload)
    if keys - _APPLY_FIELDS:
        raise ValueError("apply_operation_batch payload contains unknown fields")
    missing = _APPLY_FIELDS - keys
    if missing:
        missing_fields = ", ".join(sorted(missing))
        raise ValueError(f"apply_operation_batch requires {missing_fields}")
    for field in _SCOPE_FIELDS:
        value = payload[field]
        if not isinstance(value, str) or not _SCOPE_ID_RE.fullmatch(value):
            raise ValueError(f"{field} is not a safe identifier")
    if type(payload["baseline"]) is not bool:
        raise ValueError("baseline must be a boolean")
    if not isinstance(payload["locked_source_ids"], list):
        raise ValueError("locked_source_ids must be a list")
    if not isinstance(payload["layer_sources"], Mapping):
        raise ValueError("layer_sources must be an object")
    layer_native_ids = _canonical_native_layer_ids(payload["layer_native_ids"])
    baseline, locked_source_ids, layer_sources = canonicalize_operation_context(
        baseline=payload["baseline"],
        locked_source_ids=payload["locked_source_ids"],
        layer_sources=payload["layer_sources"],
    )
    bounds = {
        name: payload[name]
        for name in ("scene_frame_count", "duration", "layer_count")
    }
    if any(bounds[name] is None for name in bounds):
        raise ValueError("apply_operation_batch bounds are required")
    capabilities = ApprovedCapabilities.model_validate(payload["approved_capabilities"])
    batch = validate_operation_batch(
        payload["batch"],
        approved_capabilities=capabilities,
        baseline=baseline,
        locked_source_ids=locked_source_ids,
        layer_sources=layer_sources,
        **bounds,
    )
    canonical: dict[str, Any] = {
        "batch": batch.model_dump(mode="json", by_alias=True, exclude_none=True),
        "approved_capabilities": capabilities.model_dump(mode="json", by_alias=True),
        "baseline": baseline,
        "locked_source_ids": locked_source_ids,
        "layer_sources": layer_sources,
        "layer_native_ids": layer_native_ids,
    }
    canonical.update(bounds)
    canonical.update({field: payload[field] for field in _SCOPE_FIELDS})
    return canonical


def _forwarder(bridge: Bridge, kind: str) -> ToolForwarder:
    # The connector always supplies this exact envelope.  Command identity is
    # deliberately kept outside the panel payload and bound by BridgeCommand.
    def forward(envelope: Mapping[str, Any] | None = None, **kwargs: Any) -> Any:
        if isinstance(envelope, BaseModel):
            envelope = envelope.model_dump()
        if envelope is None:
            envelope = kwargs
        elif kwargs:
            raise TypeError("MCP command must use one envelope object")
        if not isinstance(envelope, Mapping) or set(envelope) != _ENVELOPE_FIELDS:
            raise TypeError("MCP command envelope must contain command_id, nonce, and payload")
        command_id = envelope["command_id"]
        nonce = envelope["nonce"]
        payload = envelope["payload"]
        if not isinstance(command_id, str) or not isinstance(nonce, str):
            raise TypeError("MCP command identity must be strings")
        if not isinstance(payload, Mapping):
            raise TypeError("MCP command payload must be an object")
        normalized = _canonical_apply_payload(payload) if kind == "apply_operation_batch" else dict(payload)
        dispatch_kwargs: dict[str, Any] = {"command_id": command_id, "nonce": nonce}
        timeout = _COMMAND_TIMEOUTS.get(kind)
        if timeout is not None:
            dispatch_kwargs["timeout"] = timeout
        return _result_value(
            bridge.dispatch(
                kind,
                normalized,
                **dispatch_kwargs,
            )
        )

    forward.__name__ = kind
    forward.__qualname__ = kind
    forward.__doc__ = f"Forward the fixed {kind} command to the local AE panel."
    return forward


def fixed_tool_registry(bridge: Bridge) -> dict[str, ToolForwarder]:
    """Return exactly the nine fixed tools in wire-order."""
    return {name: _forwarder(bridge, name) for name in TOOL_NAMES}


# Explicit alias used by a few connector integrations.
tool_registry = fixed_tool_registry


def _mcp_server_class() -> type[Any]:
    """Load the SDK class only when a caller actually starts MCP."""
    candidates: list[tuple[str, str]] = [
        ("mcp.server.mcpserver", "MCPServer"),
        ("mcp.server", "MCPServer"),
        ("mcp.server.fastmcp", "FastMCP"),
        ("mcp.server", "Server"),
    ]
    for module_name, class_name in candidates:
        try:
            module = importlib.import_module(module_name)
        except ImportError:
            continue
        candidate = getattr(module, class_name, None)
        if candidate is not None:
            return candidate
    raise RuntimeError("After Effects MCP support requires mcp>=2.2,<3")


def _patch_direct_payload(server: Any, name: str) -> None:
    manager = getattr(server, "_tool_manager", None)
    if manager is None:
        manager = getattr(server, "tool_manager", None)
    getter = getattr(manager, "get_tool", None)
    tool = getter(name) if callable(getter) else None
    if tool is None:
        return
    metadata = getattr(tool, "fn_metadata", None)
    if metadata is not None:
        object.__setattr__(metadata, "arg_model", _DirectPayloadArguments)
    try:
        setattr(tool, "parameters", {"type": "object", "additionalProperties": True})
    except (AttributeError, TypeError, ValueError):
        pass


def _register(server: Any, name: str, function: ToolForwarder) -> None:
    add_tool = getattr(server, "add_tool", None)
    if callable(add_tool):
        add_tool(function, name=name)
        _patch_direct_payload(server, name)
        return
    decorator = getattr(server, "tool", None)
    if callable(decorator):
        try:
            wrapped = cast(Callable[..., Any], decorator)(name=name)(function)
        except TypeError:
            # A small compatibility seam for MCP v2's alternate decorator
            # spelling; both paths still register the fixed name.
            wrapped = cast(Callable[..., Any], decorator)()(function)
        if wrapped is not None and wrapped is not function:
            # Some test/future SDK implementations return a replacement
            # callable but retain the name in their internal registry.
            _patch_direct_payload(server, name)
            return
        _patch_direct_payload(server, name)
        return
    raise RuntimeError("installed MCP server does not expose tool registration")


def _default_root() -> Path:
    configured = os.environ.get("KEEPFRAME_AE_BRIDGE_ROOT")
    if configured:
        return Path(configured)
    local_appdata = os.environ.get("LOCALAPPDATA")
    if local_appdata:
        return Path(local_appdata) / "Keepframe" / "ae-bridge"
    return Path.home() / ".local" / "share" / "Keepframe" / "ae-bridge"


def create_server(
    root: str | Path | None = None,
    *,
    bridge: Bridge | None = None,
) -> Any:
    """Build and register one MCP server; importing mcp happens inside this call."""
    if bridge is None:
        bridge = Bridge(_default_root() if root is None else root)
    server_class = _mcp_server_class()
    try:
        server = server_class("keepframe-after-effects")
    except TypeError:
        server = server_class(name="keepframe-after-effects")
    for name, function in fixed_tool_registry(bridge).items():
        _register(server, name, function)
    return server


# Name matching the plan's terminology.
build_server = create_server


def _run_low_level(server: Any) -> None:
    """Run low-level SDK servers without importing that SDK on module import."""

    async def runner() -> None:
        stdio_module = importlib.import_module("mcp.server.stdio")
        async with stdio_module.stdio_server() as streams:
            run = getattr(server, "run")
            # MCP v2 low-level Server.run needs initialization options; callers
            # using FastMCP take the simpler synchronous ``run`` path below.
            models = importlib.import_module("mcp.server.models")
            options = models.InitializationOptions(
                server_name="keepframe-after-effects",
                server_version="1",
                capabilities=server.get_capabilities(
                    notification_options=models.NotificationOptions(),
                    experimental_capabilities={},
                ),
            )
            await run(streams[0], streams[1], options)

    asyncio.run(runner())


def main(root: str | Path | None = None) -> int:
    server = create_server(root)
    run = getattr(server, "run", None)
    if callable(run):
        result = run()
        if asyncio.iscoroutine(result):
            asyncio.run(result)
    else:
        _run_low_level(server)
    return 0


if __name__ == "__main__":  # pragma: no cover - exercised by the connector process
    raise SystemExit(main())


__all__ = [
    "TOOL_NAMES",
    "build_server",
    "create_server",
    "fixed_tool_registry",
    "main",
    "tool_registry",
]
