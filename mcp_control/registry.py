"""Neutral installed MCP connection contracts, registry, and desired-state resolution.

This module owns the secret-free control-plane state for remote MCP servers.
Configuration is desired state, not authority: after the one-time upgrade
baseline, a new or changed definition is persisted as an inert proposal and is
removed from the agent's runtime configuration until a person approves the
exact digest (an administrator reviewing TeamLead's access proposal,
``access_control.proposals``, or approving it in the gateway's MCP controls).

An active connection records how it was approved with the shared
``ChangeApprovalRecordV1``; servers listed in the operator's configuration
when the registry was first created carry the ``operator_configuration`` mode.
An active connection is one team tool in the catalog, ``mcp:<name>`` (access
plan 7d). Who may call it is decided per call by grants on that tool (the
broker's access gate); there are no per-agent-type attachment records. The
record's ``target_agent_types`` only says which agent types load its tools
with authorization off, and which types an approval lets use it.
"""

from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime
from enum import StrEnum
import hashlib
import ipaddress
import json
import logging
import os
from pathlib import Path
import re
import socket
import threading
from typing import Any, Iterable, Iterator, Literal, Mapping, Optional, Sequence
from urllib.parse import urlparse

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from custom_messages.contracts.access_v2 import ChangeApprovalMode, ChangeApprovalRecordV1
from custom_messages.contracts.common_v1 import contract_sha256, utc_now
from id_generation import new_id_suffix
from runtime_settings.instance_layout import InstanceLayout


LOGGER = logging.getLogger(__name__)
MCP_CONNECTION_SCHEMA_VERSION = 1  # descriptors and operations (their digests name connections)
# Version 2 records the approval as ChangeApprovalRecordV1; version 1 records
# are refused at startup and re-approved with `assistant_cli access approve-existing`.
MCP_CONNECTION_RECORD_SCHEMA_VERSION: Literal[2] = 2
# Who approved servers listed in the operator's configuration file.
OPERATOR_CONFIGURATION_APPROVER = "instance_config"
MCP_STATIC_BASELINE_SCHEMA_VERSION = 1
_SAFE_NAME = re.compile(r"[a-z][a-z0-9_.-]{0,119}\Z")
_ENV_REFERENCE = re.compile(r"\$\{([A-Z][A-Z0-9_]*)\}")
_OPERATION_NAME = re.compile(r"[A-Za-z][A-Za-z0-9_.-]{0,127}\Z")


class MCPConnectionTransport(StrEnum):
    STREAMABLE_HTTP = "streamable_http"
    LEGACY_SSE = "legacy_sse"
    WEBSOCKET = "websocket"
    STDIO = "stdio"


class MCPConnectionSource(StrEnum):
    STATIC_GLOBAL = "static_global"
    STATIC_AGENT_TYPE = "static_agent_type"
    CONFIGURED_PROFILE = "configured_profile"
    AGENT_PROPOSAL = "agent_proposal"
    HOSTED = "hosted"


class MCPConnectionLifecycle(StrEnum):
    PENDING = "pending"
    ACTIVE = "active"
    REJECTED = "rejected"
    DISABLED = "disabled"
    DRIFTED = "drifted"
    REMOVED = "removed"


def _canonical_json(value: Any) -> str:
    return json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        default=str,
    )


def _sha256(value: Any) -> str:
    return hashlib.sha256(_canonical_json(value).encode("utf-8")).hexdigest()


def normalize_mcp_transport(value: Any) -> MCPConnectionTransport:
    if isinstance(value, MCPConnectionTransport):
        return value
    normalized = str(value or "").strip().lower().replace("-", "_")
    aliases = {
        "sse": MCPConnectionTransport.LEGACY_SSE,
        "legacy_sse": MCPConnectionTransport.LEGACY_SSE,
        "streamable_http": MCPConnectionTransport.STREAMABLE_HTTP,
        "streamablehttp": MCPConnectionTransport.STREAMABLE_HTTP,
        "web_socket": MCPConnectionTransport.WEBSOCKET,
        "websocket": MCPConnectionTransport.WEBSOCKET,
        "ws": MCPConnectionTransport.WEBSOCKET,
        "stdio": MCPConnectionTransport.STDIO,
    }
    try:
        return aliases[normalized]
    except KeyError as exc:
        raise ValueError(
            "MCP transport must be streamable_http, legacy_sse, websocket, or stdio."
        ) from exc


def runtime_mcp_transport(value: MCPConnectionTransport | str) -> str:
    transport = normalize_mcp_transport(value)
    return "sse" if transport == MCPConnectionTransport.LEGACY_SSE else transport.value


def _normalized_name(value: Any, *, field_name: str) -> str:
    normalized = str(value or "").strip().lower()
    if not _SAFE_NAME.fullmatch(normalized):
        raise ValueError(
            f"{field_name} must start with a lowercase letter and contain only lowercase letters, digits, '.', '_' or '-'."
        )
    return normalized


def _normalized_operation_names(value: Any) -> tuple[str, ...]:
    if value in (None, ""):
        return ()
    if not isinstance(value, (list, tuple)):
        raise ValueError("MCP tool filters must be lists of exact operation names.")
    normalized = tuple(
        sorted({str(item or "").strip() for item in value if str(item or "").strip()})
    )
    if any(not _OPERATION_NAME.fullmatch(item) for item in normalized):
        raise ValueError("MCP tool filters contain an invalid operation name.")
    return normalized


def operation_permitted_by_descriptor(
    descriptor: "MCPConnectionDescriptorV1", operation_name: str
) -> bool:
    """Apply the operator's exact ``include_tools``/``exclude_tools`` filters.

    An empty ``include_tools`` permits every operation the server advertises;
    ``exclude_tools`` always wins. The broker's discovery and invocation both
    use this one rule, so an operation the operator excluded is never listed
    and cannot be invoked even if an older catalog still lists it.
    """

    if operation_name in descriptor.exclude_tools:
        return False
    return not descriptor.include_tools or operation_name in descriptor.include_tools


def _endpoint_from_config(config: Mapping[str, Any]) -> tuple[str | None, str | None]:
    env_name = str(config.get("server_url_env") or "").strip()
    endpoint = str(config.get("server_url") or "").strip() or None
    if env_name and not re.fullmatch(r"[A-Z][A-Z0-9_]*", env_name):
        raise ValueError(
            "MCP server_url_env must be an uppercase environment variable name."
        )
    if endpoint:
        parsed = urlparse(endpoint)
        if parsed.scheme not in {"http", "https", "ws", "wss"} or not parsed.hostname:
            raise ValueError(
                "Remote MCP server_url must be an absolute HTTP(S) or WS(S) URL."
            )
        if parsed.username or parsed.password:
            raise ValueError("MCP server_url must not contain embedded credentials.")
    return endpoint, env_name or None


def _header_contract(
    config: Mapping[str, Any],
) -> tuple[tuple[str, ...], tuple[str, ...], bool]:
    raw_headers = config.get("headers") or {}
    if not isinstance(raw_headers, Mapping):
        raise ValueError("MCP headers must be a mapping.")
    names: list[str] = []
    env_names: set[str] = set()
    literal_credentials = False
    for raw_name, raw_value in raw_headers.items():
        name = str(raw_name or "").strip()
        if not name or any(
            ord(char) < 33 or ord(char) > 126 or char == ":" for char in name
        ):
            raise ValueError(
                "MCP header names must contain visible HTTP token characters."
            )
        names.append(name.lower())
        value = str(raw_value or "")
        refs = set(_ENV_REFERENCE.findall(value))
        env_names.update(refs)
        residual = _ENV_REFERENCE.sub("", value).strip()
        if residual and name.lower() in {
            "authorization",
            "cookie",
            "proxy-authorization",
            "x-api-key",
            "api-key",
        }:
            literal_credentials = True
    return tuple(sorted(set(names))), tuple(sorted(env_names)), literal_credentials


def _stdio_contract(
    config: Mapping[str, Any],
) -> tuple[str | None, tuple[str, ...], tuple[str, ...], bool]:
    command = str(config.get("command") or "").strip() or None
    args = tuple(str(item) for item in (config.get("args") or []))
    raw_env = config.get("env") or {}
    if not isinstance(raw_env, Mapping):
        raise ValueError("stdio MCP env must be a mapping.")
    env_names = tuple(sorted(str(key) for key in raw_env if str(key).strip()))
    literal_env = any(
        bool(str(value or "").strip())
        and not _ENV_REFERENCE.fullmatch(str(value or "").strip())
        for value in raw_env.values()
    )
    return command, args, env_names, literal_env


class MCPConnectionDescriptorV1(BaseModel):
    """Secret-free immutable description of one exact MCP endpoint intent."""

    model_config = ConfigDict(extra="forbid", frozen=True, str_strip_whitespace=True)

    schema_version: int = MCP_CONNECTION_SCHEMA_VERSION
    connection_name: str
    source: MCPConnectionSource
    source_locator: str = Field(min_length=1, max_length=500)
    team_id: str = Field(min_length=1, max_length=240)
    owner_canonical_user_id: str = Field(min_length=1, max_length=240)
    transport: MCPConnectionTransport
    endpoint: str | None = Field(default=None, max_length=2048)
    server_url_env: str | None = Field(default=None, max_length=160)
    command: str | None = Field(default=None, max_length=500)
    args: tuple[str, ...] = ()
    stdio_env_names: tuple[str, ...] = ()
    header_names: tuple[str, ...] = ()
    credential_env_names: tuple[str, ...] = ()
    literal_credential_material_present: bool = False
    tool_name_prefix: str = Field(min_length=1, max_length=120)
    include_tools: tuple[str, ...] = ()
    exclude_tools: tuple[str, ...] = ()
    startup_timeout_seconds: float = Field(default=30.0, gt=0, le=600)
    operation_timeout_seconds: float = Field(default=300.0, gt=0, le=3600)
    source_config_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    credential_injections: tuple["MCPCredentialInjectionV1", ...] = ()

    @field_validator("connection_name")
    @classmethod
    def _validate_connection_name(cls, value: str) -> str:
        return _normalized_name(value, field_name="MCP connection name")

    @model_validator(mode="after")
    def _validate_transport_fields(self) -> "MCPConnectionDescriptorV1":
        remote = self.transport != MCPConnectionTransport.STDIO
        if remote and not (self.endpoint or self.server_url_env):
            raise ValueError(
                "Remote MCP connections require endpoint or server_url_env."
            )
        if not remote and not self.command:
            raise ValueError("stdio MCP connections require a command.")
        if remote and self.command:
            raise ValueError("Remote MCP connections cannot include a command.")
        if self.transport == MCPConnectionTransport.STDIO and (
            self.endpoint or self.server_url_env
        ):
            raise ValueError("stdio MCP connections cannot include a remote endpoint.")
        return self

    @property
    def descriptor_sha256(self) -> str:
        return contract_sha256(self)

    @property
    def capability_id(self) -> str:
        return f"mcp:{self.connection_name}"

    @property
    def requires_legacy_credentials(self) -> bool:
        """True when credentials come from the connection's own configuration.

        Headers, environment values, or literal material in the operator's
        server config need operator approval. Declared ``credential_injections``
        slots instead take their values from the account linked to the
        connection's catalog tool, filled by the broker at use time.
        """

        return bool(
            self.literal_credential_material_present
            or self.header_names
            or self.stdio_env_names
        ) and not self.credential_injections


class MCPOperationDefinitionV1(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, str_strip_whitespace=True)

    schema_version: int = MCP_CONNECTION_SCHEMA_VERSION
    operation_name: str
    description: str = Field(default="", max_length=4000)
    input_schema: dict[str, Any]
    output_schema: dict[str, Any] | None = None
    operation_digest: str = ""

    @field_validator("operation_name")
    @classmethod
    def _validate_operation(cls, value: str) -> str:
        normalized = str(value or "").strip()
        if not _OPERATION_NAME.fullmatch(normalized):
            raise ValueError("Invalid MCP operation name.")
        return normalized

    @model_validator(mode="after")
    def _validate_digest(self) -> "MCPOperationDefinitionV1":
        expected = contract_sha256(
            self.model_dump(mode="json", exclude={"operation_digest"})
        )
        if self.operation_digest and self.operation_digest != expected:
            raise ValueError("MCP operation digest mismatch.")
        return self

    @classmethod
    def build(cls, **values: Any) -> "MCPOperationDefinitionV1":
        payload = dict(values)
        payload["operation_digest"] = ""
        candidate = cls.model_validate(payload)
        return cls.model_validate(
            {
                **candidate.model_dump(mode="python"),
                "operation_digest": contract_sha256(
                    candidate.model_dump(mode="json", exclude={"operation_digest"})
                ),
            }
        )


class MCPCredentialInjectionV1(BaseModel):
    """A secret-free credential slot the connection declares.

    The broker fills it from the connection's account link (the connection
    owner's own link first, then the team's; ``AccountLinkV1``), which also
    makes the connection a tool that needs a linked account in the catalog.
    ``owner_scope_kind`` is kept only so installed records keep their digest;
    the account link, not the slot, decides whose account is used.
    """

    model_config = ConfigDict(extra="forbid", frozen=True, str_strip_whitespace=True)

    owner_scope_kind: Literal["user", "team"] = "team"
    target_kind: Literal["header", "env"]
    target_name: str = Field(min_length=1, max_length=240)
    field_name: str = Field(min_length=1, max_length=160)
    # How the value is written, for example "Bearer {value}"; None writes it as is.
    template: Optional[str] = Field(default=None, max_length=200)

    @field_validator("template")
    @classmethod
    def _validate_template(cls, value: Optional[str]) -> Optional[str]:
        if value is not None and (value.count("{value}") != 1 or "\n" in value or "\r" in value):
            raise ValueError("An MCP credential template contains '{value}' exactly once on one line.")
        return value

    @field_validator("target_name")
    @classmethod
    def _validate_target_name(cls, value: str) -> str:
        normalized = str(value or "").strip()
        if any(ord(char) < 32 or ord(char) == 127 for char in normalized):
            raise ValueError("MCP credential injection names cannot contain controls.")
        return normalized


MCPConnectionDescriptorV1.model_rebuild()


class MCPConnectionRecordV1(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: Literal[2] = MCP_CONNECTION_RECORD_SCHEMA_VERSION
    connection_id: str
    record_digest: str = ""
    descriptor: MCPConnectionDescriptorV1
    lifecycle: MCPConnectionLifecycle
    created_at_utc: datetime
    updated_at_utc: datetime
    target_agent_types: tuple[str, ...] = ()
    operations: tuple[MCPOperationDefinitionV1, ...] = ()
    operation_catalog_sha256: str | None = Field(
        default=None, pattern=r"^[0-9a-f]{64}$"
    )
    approval: ChangeApprovalRecordV1 | None = None  # set when the connection became active
    rejection_reason: str | None = Field(default=None, max_length=2000)
    drift_reason: str | None = Field(default=None, max_length=2000)

    @field_validator("connection_id")
    @classmethod
    def _validate_connection_id(cls, value: str) -> str:
        normalized = str(value or "").strip()
        if not re.fullmatch(r"mcp_conn_[a-z0-9_.-]{1,120}_[0-9a-f]{16}", normalized):
            raise ValueError("Invalid MCP connection id.")
        return normalized

    @model_validator(mode="after")
    def _validate_record(self) -> "MCPConnectionRecordV1":
        expected_catalog = (
            _sha256([item.model_dump(mode="json") for item in self.operations])
            if self.operations
            else None
        )
        if self.operation_catalog_sha256 != expected_catalog:
            raise ValueError("MCP operation catalog digest mismatch.")
        expected = self.calculate_digest()
        if self.record_digest and self.record_digest != expected:
            raise ValueError("MCP connection record digest mismatch.")
        if self.lifecycle == MCPConnectionLifecycle.ACTIVE and self.approval is None:
            raise ValueError(
                "Active MCP connection records require approval provenance."
            )
        return self

    def calculate_digest(self) -> str:
        return contract_sha256(
            self.model_dump(
                mode="json",
                exclude={"record_digest", "updated_at_utc"},
                exclude_none=True,
            )
        )

    @classmethod
    def build(cls, **values: Any) -> "MCPConnectionRecordV1":
        payload = dict(values)
        operations = tuple(payload.get("operations") or ())
        payload["operation_catalog_sha256"] = (
            _sha256(
                [
                    item.model_dump(mode="json")
                    if isinstance(item, BaseModel)
                    else item
                    for item in operations
                ]
            )
            if operations
            else None
        )
        payload["record_digest"] = ""
        candidate = cls.model_validate(payload)
        return cls.model_validate(
            {
                **candidate.model_dump(mode="python"),
                "record_digest": candidate.calculate_digest(),
            }
        )

    @property
    def from_operator_configuration(self) -> bool:
        """Listed in the operator's configuration when the registry was first created."""

        return bool(self.approval and self.approval.mode is ChangeApprovalMode.OPERATOR_CONFIGURATION)

    def permits_agent_type(self, named_agent_type: str) -> bool:
        """Whether the record names this agent type (or every type, ``*``).

        Decides which agents load the server with authorization off, and which
        types an approval lets use it; with authorization on, calls are
        decided by grants on ``mcp:<name>`` alone.
        """

        return (
            "*" in self.target_agent_types
            or named_agent_type in self.target_agent_types
        )


class MCPStaticBaselineV1(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: int = MCP_STATIC_BASELINE_SCHEMA_VERSION
    initialized_at_utc: datetime
    instance_config_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    source_config_digests: tuple[str, ...]
    connection_ids: tuple[str, ...]
    upgrade_mode: str


class MCPConnectionPaths(BaseModel):
    model_config = ConfigDict(arbitrary_types_allowed=True, frozen=True)

    root: Path
    records: Path
    receipts: Path
    baseline: Path
    lock: Path

    def ensure(self) -> None:
        for path in (
            self.root,
            self.records,
            self.receipts,
        ):
            path.mkdir(parents=True, mode=0o700, exist_ok=True)


def resolve_mcp_connection_paths(instance_root: str | Path) -> MCPConnectionPaths:
    """The installed MCP connection folder of an instance (``InstanceLayout.mcp_connections_dir``).

    The location is derived from the instance root and is not a setting, so
    every process (gateway, broker, agents, catalog) reads the same records.
    """

    root = InstanceLayout.at(instance_root).mcp_connections_dir
    return MCPConnectionPaths(
        root=root,
        records=root / "records",
        receipts=root / "receipts",
        baseline=root / "static-baseline-v1.json",
        lock=root / ".registry.lock",
    )


# Registry locks this thread holds, so a store call made inside a held lock
# (reconcile_instance_mcp_connections) does not wait for itself. flock locks an
# open file, not a process: a second open of the lock file in the same process
# would wait for the first forever.
_HELD_REGISTRY_LOCKS = threading.local()


@contextmanager
def _registry_lock(paths: MCPConnectionPaths) -> Iterator[None]:
    """Hold the instance's registry lock; the thread that holds it may enter again."""

    held: set[str] | None = getattr(_HELD_REGISTRY_LOCKS, "paths", None)
    if held is None:
        held = set()
        _HELD_REGISTRY_LOCKS.paths = held
    key = str(paths.lock)
    if key in held:
        yield
        return
    paths.ensure()
    handle = paths.lock.open("a+", encoding="utf-8")
    try:
        try:
            import fcntl

            fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        except ImportError:
            pass
        held.add(key)
        yield
    finally:
        held.discard(key)
        try:
            import fcntl

            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        except ImportError:
            pass
        handle.close()


def _atomic_write_json(path: Path, payload: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, mode=0o700, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{new_id_suffix()}.tmp")
    try:
        temporary.write_text(
            json.dumps(dict(payload), indent=2, sort_keys=True, default=str) + "\n",
            encoding="utf-8",
        )
        os.replace(temporary, path)
        try:
            path.chmod(0o600)
        except OSError:
            pass
    finally:
        temporary.unlink(missing_ok=True)


class MCPConnectionStore:
    """File-owned repository for immutable definitions and lifecycle observations."""

    def __init__(self, paths: MCPConnectionPaths) -> None:
        self.paths = paths
        self.paths.ensure()

    def list_records(self) -> tuple[MCPConnectionRecordV1, ...]:
        records: list[MCPConnectionRecordV1] = []
        for path in sorted(self.paths.records.glob("mcp_conn_*.json")):
            records.append(
                MCPConnectionRecordV1.model_validate_json(
                    path.read_text(encoding="utf-8")
                )
            )
        return tuple(records)

    def get(self, connection_id: str) -> MCPConnectionRecordV1 | None:
        path = self.paths.records / f"{str(connection_id or '').strip()}.json"
        if not path.is_file():
            return None
        return MCPConnectionRecordV1.model_validate_json(
            path.read_text(encoding="utf-8")
        )

    def require(self, connection_id: str) -> MCPConnectionRecordV1:
        record = self.get(connection_id)
        if record is None:
            raise FileNotFoundError(f"MCP connection record not found: {connection_id}")
        return record

    def find_by_source_digest(
        self,
        source_config_sha256: str,
        *,
        source: MCPConnectionSource | None = None,
        source_locator: str | None = None,
    ) -> MCPConnectionRecordV1 | None:
        for record in self.list_records():
            if record.descriptor.source_config_sha256 != source_config_sha256:
                continue
            if source is not None and record.descriptor.source != source:
                continue
            if (
                source_locator is not None
                and record.descriptor.source_locator != source_locator
            ):
                continue
            return record
        return None

    def save(self, record: MCPConnectionRecordV1) -> MCPConnectionRecordV1:
        validated = MCPConnectionRecordV1.model_validate(record)
        with _registry_lock(self.paths):
            _atomic_write_json(
                self.paths.records / f"{validated.connection_id}.json",
                validated.model_dump(mode="json", exclude_none=True),
            )
        return validated

    def active_for_tool(self, tool_id: str) -> MCPConnectionRecordV1 | None:
        """The active connection whose catalog tool is ``tool_id`` (``mcp:<name>``), if any.

        Connection names are unique among records that are not rejected or
        removed, so at most one active record matches.
        """

        normalized = str(tool_id or "").strip().lower()
        for record in self.list_records():
            if (
                record.lifecycle == MCPConnectionLifecycle.ACTIVE
                and record.descriptor.capability_id == normalized
            ):
                return record
        return None

    def baseline(self) -> MCPStaticBaselineV1 | None:
        if not self.paths.baseline.is_file():
            return None
        return MCPStaticBaselineV1.model_validate_json(
            self.paths.baseline.read_text(encoding="utf-8")
        )

    def save_baseline(self, baseline: MCPStaticBaselineV1) -> None:
        with _registry_lock(self.paths):
            if self.paths.baseline.is_file():
                existing = MCPStaticBaselineV1.model_validate_json(
                    self.paths.baseline.read_text(encoding="utf-8")
                )
                if existing != baseline:
                    raise RuntimeError(
                        "MCP static upgrade baseline is immutable once recorded."
                    )
                return
            _atomic_write_json(
                self.paths.baseline,
                baseline.model_dump(mode="json", exclude_none=True),
            )

    def set_operations(
        self,
        connection_id: str,
        operations: Sequence[MCPOperationDefinitionV1],
    ) -> MCPConnectionRecordV1:
        current = self.require(connection_id)
        normalized = tuple(sorted(operations, key=lambda item: item.operation_name))
        if current.operations and current.operations != normalized:
            payload = current.model_dump(
                mode="python",
                exclude={"record_digest", "operation_catalog_sha256"},
            )
            payload.update(
                lifecycle=MCPConnectionLifecycle.DRIFTED,
                updated_at_utc=utc_now(),
                drift_reason="The discovered MCP operation catalog or schema changed.",
            )
            drifted = MCPConnectionRecordV1.build(**payload)
            self.save(drifted)
            raise PermissionError("MCP operation catalog drifted and was quarantined.")
        if current.operations == normalized:
            return current
        updated = MCPConnectionRecordV1.build(
            **current.model_dump(
                mode="python",
                exclude={
                    "record_digest",
                    "operation_catalog_sha256",
                    "operations",
                    "updated_at_utc",
                },
            ),
            operations=normalized,
            updated_at_utc=utc_now(),
        )
        return self.save(updated)

    def update_lifecycle(
        self,
        connection_id: str,
        *,
        lifecycle: MCPConnectionLifecycle,
        approval: ChangeApprovalRecordV1 | None = None,
        reason: str | None = None,
    ) -> MCPConnectionRecordV1:
        current = self.require(connection_id)
        payload = current.model_dump(
            mode="python",
            exclude={"record_digest", "operation_catalog_sha256"},
        )
        payload.update(
            lifecycle=lifecycle,
            approval=approval if approval is not None else current.approval,
            updated_at_utc=utc_now(),
            rejection_reason=(
                reason if lifecycle == MCPConnectionLifecycle.REJECTED else None
            ),
        )
        return self.save(MCPConnectionRecordV1.build(**payload))


def descriptor_from_server_config(
    server_config: Mapping[str, Any],
    *,
    source: MCPConnectionSource,
    source_locator: str,
    team_id: str,
    owner_canonical_user_id: str,
) -> MCPConnectionDescriptorV1:
    raw = dict(server_config)
    name = _normalized_name(raw.get("name"), field_name="MCP connection name")
    transport = normalize_mcp_transport(raw.get("transport"))
    endpoint: str | None = None
    server_url_env: str | None = None
    command: str | None = None
    args: tuple[str, ...] = ()
    stdio_env_names: tuple[str, ...] = ()
    literal_stdio_env = False
    if transport == MCPConnectionTransport.STDIO:
        command, args, stdio_env_names, literal_stdio_env = _stdio_contract(raw)
    else:
        endpoint, server_url_env = _endpoint_from_config(raw)
    header_names, header_env_names, literal_headers = _header_contract(raw)
    include_tools = _normalized_operation_names(raw.get("include_tools"))
    exclude_tools = _normalized_operation_names(raw.get("exclude_tools"))
    raw_injections = raw.get("credential_injections") or []
    if not isinstance(raw_injections, list):
        raise ValueError("MCP credential_injections must be a list.")
    injections = tuple(
        MCPCredentialInjectionV1.model_validate(item) for item in raw_injections
    )
    return MCPConnectionDescriptorV1(
        connection_name=name,
        source=source,
        source_locator=source_locator,
        team_id=str(team_id or "").strip(),
        owner_canonical_user_id=str(owner_canonical_user_id or "").strip(),
        transport=transport,
        endpoint=endpoint,
        server_url_env=server_url_env,
        command=command,
        args=args,
        stdio_env_names=stdio_env_names,
        header_names=header_names,
        credential_env_names=header_env_names,
        literal_credential_material_present=literal_headers or literal_stdio_env,
        tool_name_prefix=str(raw.get("tool_name_prefix") or name).strip(),
        include_tools=include_tools,
        exclude_tools=exclude_tools,
        startup_timeout_seconds=float(raw.get("startup_timeout", 30)),
        operation_timeout_seconds=float(raw.get("tool_timeout_seconds", 300)),
        source_config_sha256=_sha256(raw),
        credential_injections=injections,
    )


def _connection_id(descriptor: MCPConnectionDescriptorV1) -> str:
    return f"mcp_conn_{descriptor.connection_name}_{descriptor.descriptor_sha256[:16]}"


def _new_record(
    descriptor: MCPConnectionDescriptorV1,
    *,
    lifecycle: MCPConnectionLifecycle,
    target_agent_types: Sequence[str] = (),
    approval: ChangeApprovalRecordV1 | None = None,
    now: datetime | None = None,
) -> MCPConnectionRecordV1:
    observed = now or utc_now()
    return MCPConnectionRecordV1.build(
        connection_id=_connection_id(descriptor),
        descriptor=descriptor,
        lifecycle=lifecycle,
        created_at_utc=observed,
        updated_at_utc=observed,
        target_agent_types=tuple(sorted(set(target_agent_types))),
        operations=(),
        approval=approval,
    )


def _upgrade_mode(config: Any) -> str:
    static = getattr(getattr(config, "mcp", None), "static_reconciliation", None)
    return str(getattr(static, "upgrade_mode", "review_required") or "review_required")


def _configured_sources(
    config: Any,
) -> list[tuple[Mapping[str, Any], MCPConnectionSource, str, tuple[str, ...]]]:
    configured: list[
        tuple[Mapping[str, Any], MCPConnectionSource, str, tuple[str, ...]]
    ] = []
    for index, raw in enumerate(config.agent.external_mcp_servers or []):
        if not isinstance(raw, Mapping) or raw.get("enabled", True) is False:
            continue
        name = str(raw.get("name") or f"index-{index}").strip()
        configured.append(
            (
                raw,
                MCPConnectionSource.STATIC_GLOBAL,
                f"agent.external_mcp_servers.{name}",
                ("*",),
            )
        )
    for agent_type, settings in sorted(config.agent_types.items()):
        override = getattr(settings, "agent", None)
        values = (
            getattr(override, "external_mcp_servers", None)
            if override is not None
            else None
        )
        if values is None:
            continue
        for index, raw in enumerate(values or []):
            if not isinstance(raw, Mapping) or raw.get("enabled", True) is False:
                continue
            name = str(raw.get("name") or f"index-{index}").strip()
            configured.append(
                (
                    raw,
                    MCPConnectionSource.STATIC_AGENT_TYPE,
                    f"agent_types.{agent_type}.agent.external_mcp_servers.{name}",
                    (agent_type,),
                )
            )
    for profile_name, raw in sorted(dict(config.mcp_profiles or {}).items()):
        if not isinstance(raw, Mapping) or raw.get("enabled", True) is False:
            continue
        payload = {**dict(raw), "name": profile_name}
        configured.append(
            (
                payload,
                MCPConnectionSource.CONFIGURED_PROFILE,
                f"mcp_profiles.{profile_name}",
                ("*",),
            )
        )
    return configured


def resolve_record_source_config(
    config: Any,
    record: MCPConnectionRecordV1,
) -> dict[str, Any]:
    """Resolve an installed record back to its operator-owned desired config.

    This function is intended for the secure broker only. Callers must not
    serialize or return the resulting values to an agent process.
    """

    if record.descriptor.source in {
        MCPConnectionSource.AGENT_PROPOSAL,
        MCPConnectionSource.HOSTED,
    }:
        descriptor = record.descriptor
        return {
            "name": descriptor.connection_name,
            "transport": runtime_mcp_transport(descriptor.transport),
            "server_url": descriptor.endpoint,
            "server_url_env": descriptor.server_url_env,
            "tool_name_prefix": descriptor.tool_name_prefix,
            "include_tools": list(descriptor.include_tools),
            "exclude_tools": list(descriptor.exclude_tools),
            "startup_timeout": descriptor.startup_timeout_seconds,
            "tool_timeout_seconds": descriptor.operation_timeout_seconds,
        }

    matches = [
        dict(raw)
        for raw, source, locator, _targets in _configured_sources(config)
        if source == record.descriptor.source
        and locator == record.descriptor.source_locator
        and _sha256(dict(raw)) == record.descriptor.source_config_sha256
    ]
    if len(matches) != 1:
        raise PermissionError(
            "Installed MCP connection no longer matches exactly one configured source."
        )
    return matches[0]


def reconcile_instance_mcp_connections(
    config: Any,
) -> tuple[MCPConnectionRecordV1, ...]:
    """Reconcile every static definition without performing network I/O.

    The whole reconciliation holds the registry lock. The manager and each
    startup agent reconcile as they start, often at the same moment; when each
    read "no baseline yet" outside the lock, each recorded its own baseline and
    records stamped with its own time, and the second one stopped its process
    with "MCP static upgrade baseline is immutable once recorded". Under the
    lock the first process records them and the others find them.
    """

    store = MCPConnectionStore(resolve_mcp_connection_paths(config.instance_root_path))
    with _registry_lock(store.paths):
        return _reconcile_locked(config, store)


def _reconcile_locked(
    config: Any, store: MCPConnectionStore
) -> tuple[MCPConnectionRecordV1, ...]:
    sources = _configured_sources(config)
    baseline = store.baseline()
    upgrade_mode = _upgrade_mode(config)
    now = utc_now()
    grandfather = baseline is None and upgrade_mode == "grandfather_current"
    approval = (
        ChangeApprovalRecordV1(
            mode=ChangeApprovalMode.OPERATOR_CONFIGURATION,
            approved_by=OPERATOR_CONFIGURATION_APPROVER,
            approved_at_utc=now,
        )
        if grandfather
        else None
    )
    desired: list[MCPConnectionRecordV1] = []
    for raw, source, locator, targets in sources:
        descriptor = descriptor_from_server_config(
            raw,
            source=source,
            source_locator=locator,
            team_id=config.manager.team_id,
            owner_canonical_user_id=config.user.canonical_user_id,
        )
        existing = store.find_by_source_digest(
            descriptor.source_config_sha256,
            source=descriptor.source,
            source_locator=descriptor.source_locator,
        )
        if existing is not None:
            desired.append(existing)
            continue
        record = _new_record(
            descriptor,
            lifecycle=(
                MCPConnectionLifecycle.ACTIVE
                if grandfather
                else MCPConnectionLifecycle.PENDING
            ),
            target_agent_types=targets,
            approval=approval,
            now=now,
        )
        store.save(record)
        desired.append(record)
        LOGGER.info(
            "MCP connection definition reconciled. event=mcp_connection_reconciled "
            "connection_id=%s source=%s lifecycle=%s descriptor_sha256=%s",
            record.connection_id,
            descriptor.source.value,
            record.lifecycle.value,
            descriptor.descriptor_sha256,
        )
    desired_connection_ids = {record.connection_id for record in desired}
    static_sources = {
        MCPConnectionSource.STATIC_GLOBAL,
        MCPConnectionSource.STATIC_AGENT_TYPE,
        MCPConnectionSource.CONFIGURED_PROFILE,
    }
    for existing in store.list_records():
        if (
            existing.descriptor.source in static_sources
            and existing.connection_id not in desired_connection_ids
            and existing.lifecycle
            not in {
                MCPConnectionLifecycle.DISABLED,
                MCPConnectionLifecycle.REJECTED,
                MCPConnectionLifecycle.REMOVED,
            }
        ):
            store.update_lifecycle(
                existing.connection_id,
                lifecycle=MCPConnectionLifecycle.DISABLED,
            )
            LOGGER.warning(
                "Removed or changed static MCP definition was disabled. "
                "event=mcp_static_connection_disabled connection_id=%s source_locator=%s",
                existing.connection_id,
                existing.descriptor.source_locator,
            )
    if baseline is None:
        baseline = MCPStaticBaselineV1(
            initialized_at_utc=now,
            instance_config_sha256=_sha256(
                [
                    {
                        "source": source.value,
                        "locator": locator,
                        "config_sha256": _sha256(dict(raw)),
                        "targets": list(targets),
                    }
                    for raw, source, locator, targets in sources
                ]
            ),
            source_config_digests=tuple(
                sorted(record.descriptor.source_config_sha256 for record in desired)
            ),
            connection_ids=tuple(sorted(record.connection_id for record in desired)),
            upgrade_mode=upgrade_mode,
        )
        store.save_baseline(baseline)
    return tuple(desired)


def _record_for_config(
    store: MCPConnectionStore,
    raw: Mapping[str, Any],
    *,
    source_candidates: Sequence[MCPConnectionSource],
) -> MCPConnectionRecordV1 | None:
    """The record of one operator-configured server, matched by the digest of its configuration.

    Only the operator's own server entries reach this lookup (the agent type
    lists and launch profiles); an agent-side client configuration
    (``brokered_mcp_server_config``) is never fed back as configuration.
    """

    digest = _sha256(dict(raw))
    for record in store.list_records():
        if (
            record.lifecycle
            not in {MCPConnectionLifecycle.REMOVED, MCPConnectionLifecycle.REJECTED}
            and record.descriptor.source_config_sha256 == digest
            and record.descriptor.source in source_candidates
        ):
            return record
    return None


def brokered_mcp_server_config(
    record: MCPConnectionRecordV1,
    *,
    named_agent_type: str,
) -> dict[str, Any]:
    """The secret-free configuration of one record's agent-side client.

    ``custom_agents.sdk.strands.mcp.brokered_mcp_client`` reads exactly these
    keys. They name the connection and record digest, never an endpoint,
    header or credential; the broker applies the operator's tool filters and
    decides every call.
    """

    descriptor = record.descriptor
    return {
        "name": descriptor.connection_name,
        "tool_name_prefix": descriptor.tool_name_prefix,
        "tool_timeout_seconds": descriptor.operation_timeout_seconds,
        "mcp_connection_id": record.connection_id,
        "mcp_connection_record_digest": record.record_digest,
        "mcp_brokered": True,
        "mcp_target_agent_type": named_agent_type,
    }


def validate_mcp_network_target(
    endpoint: str,
    *,
    allow_loopback: bool,
    allow_private_networks: bool,
    allowed_private_hosts: Iterable[str] = (),
) -> tuple[str, ...]:
    """Resolve and validate every address before an approved remote connection."""

    parsed = urlparse(str(endpoint or "").strip())
    if parsed.scheme not in {"http", "https", "ws", "wss"} or not parsed.hostname:
        raise PermissionError("MCP endpoint must be an absolute HTTP(S) or WS(S) URL.")
    if parsed.username or parsed.password:
        raise PermissionError("MCP endpoints cannot contain embedded credentials.")
    endpoint_host = parsed.hostname.rstrip(".").casefold()
    private_host_allowed = endpoint_host in set(
        normalize_mcp_private_hosts(allowed_private_hosts)
    )
    try:
        results = socket.getaddrinfo(
            parsed.hostname,
            parsed.port or (443 if parsed.scheme in {"https", "wss"} else 80),
            type=socket.SOCK_STREAM,
        )
    except OSError as exc:
        raise PermissionError(f"MCP endpoint DNS resolution failed: {exc}") from exc
    addresses = tuple(sorted({str(item[4][0]) for item in results}))
    if not addresses:
        raise PermissionError("MCP endpoint DNS resolution returned no addresses.")
    for raw in addresses:
        address = ipaddress.ip_address(raw)
        if address.is_loopback and not allow_loopback:
            raise PermissionError(
                "MCP endpoint resolves to loopback, which policy forbids."
            )
        if (
            (
                address.is_private
                or address.is_link_local
                or address.is_reserved
                or address.is_unspecified
                or address.is_multicast
            )
            and not (address.is_loopback and allow_loopback)
            and not allow_private_networks
            and not (
                private_host_allowed
                and address.is_private
                and not address.is_loopback
                and not address.is_link_local
                and not address.is_reserved
                and not address.is_unspecified
                and not address.is_multicast
            )
        ):
            raise PermissionError(
                "MCP endpoint resolves to a private, link-local, reserved, or non-routable address."
            )
    return addresses


def normalize_mcp_private_hosts(values: Iterable[str]) -> tuple[str, ...]:
    """Normalize exact operator-owned private host exceptions."""

    if isinstance(values, (str, bytes)):
        raise ValueError(
            "mcp.network_policy.allowed_private_hosts must be a list of exact hosts."
        )
    normalized: list[str] = []
    for value in values:
        host = str(value or "").strip().rstrip(".").casefold()
        if (
            not host
            or host == "*"
            or "://" in host
            or "/" in host
            or any(character.isspace() for character in host)
        ):
            raise ValueError(
                "mcp.network_policy.allowed_private_hosts entries must be exact "
                "hostnames or IP literals without a scheme, path, or wildcard."
            )
        if host not in normalized:
            normalized.append(host)
    return tuple(normalized)


__all__ = [
    "MCP_CONNECTION_RECORD_SCHEMA_VERSION",
    "OPERATOR_CONFIGURATION_APPROVER",
    "MCPConnectionDescriptorV1",
    "MCPConnectionLifecycle",
    "MCPConnectionPaths",
    "MCPConnectionRecordV1",
    "MCPConnectionSource",
    "MCPConnectionStore",
    "MCPConnectionTransport",
    "MCPCredentialInjectionV1",
    "MCPOperationDefinitionV1",
    "MCPStaticBaselineV1",
    "brokered_mcp_server_config",
    "descriptor_from_server_config",
    "normalize_mcp_private_hosts",
    "normalize_mcp_transport",
    "reconcile_instance_mcp_connections",
    "resolve_mcp_connection_paths",
    "resolve_record_source_config",
    "runtime_mcp_transport",
    "validate_mcp_network_target",
]
