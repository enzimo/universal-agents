"""Neutral reviewed declarative MCP hosting contracts and state.

Hosted definitions contain JSON schemas and trusted behavior identifiers only.
They never contain Python imports, shell commands, package references, or
credential values. The secure broker owns sidecar processes and the final
authorization boundary for start/stop operations.
"""

from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime
from enum import StrEnum
import json
import logging
import os
from pathlib import Path
import re
from typing import Any, Iterator

from jsonschema.validators import Draft202012Validator
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from custom_messages.contracts.common_v1 import (
    contract_sha256,
    utc_now,
)
from id_generation import new_id_suffix
from runtime_settings.instance_layout import InstanceLayout


LOGGER = logging.getLogger(__name__)
HOSTED_MCP_SCHEMA_VERSION = 1
# The broker's answer when a hosted server's sidecar could not start
# (``HostedMCPStartError``): "temporarily unavailable", with this reason code.
HOSTED_MCP_START_FAILED = "hosted_mcp_start_failed"
HOSTED_MCP_START_FAILED_STATUS = 503
_NAME = re.compile(r"[a-z][a-z0-9_.-]{0,119}\Z")
_TOOL_NAME = re.compile(r"[A-Za-z][A-Za-z0-9_.-]{0,127}\Z")


class HostedMCPBehavior(StrEnum):
    ECHO_ARGUMENTS = "echo_arguments"
    STATIC_RESPONSE = "static_response"


class HostedMCPLifecycle(StrEnum):
    PENDING = "pending"
    STOPPED = "stopped"
    RUNNING = "running"
    REMOVED = "removed"


class HostedMCPToolDefinitionV1(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, str_strip_whitespace=True)

    name: str
    description: str = Field(default="", max_length=4000)
    input_schema: dict[str, Any] = Field(
        default_factory=lambda: {"type": "object", "additionalProperties": False}
    )
    output_schema: dict[str, Any] | None = None
    behavior: HostedMCPBehavior
    static_response: dict[str, Any] | None = None

    @field_validator("name")
    @classmethod
    def _validate_name(cls, value: str) -> str:
        normalized = str(value or "").strip()
        if not _TOOL_NAME.fullmatch(normalized):
            raise ValueError("Hosted MCP tool names must be exact MCP operation names.")
        return normalized

    @model_validator(mode="after")
    def _validate_contract(self) -> "HostedMCPToolDefinitionV1":
        Draft202012Validator.check_schema(self.input_schema)
        if self.output_schema is not None:
            Draft202012Validator.check_schema(self.output_schema)
        if self.behavior == HostedMCPBehavior.STATIC_RESPONSE:
            if self.static_response is None:
                raise ValueError("static_response behavior requires a response object.")
            if self.output_schema is not None:
                Draft202012Validator(self.output_schema).validate(self.static_response)
        elif self.static_response is not None:
            raise ValueError("Only static_response behavior accepts static_response data.")
        return self


class MCPHostedServerDefinitionV1(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: int = HOSTED_MCP_SCHEMA_VERSION
    hosted_server_id: str
    definition_digest: str = ""
    server_name: str
    purpose: str = Field(min_length=1, max_length=2000)
    owner_canonical_user_id: str = Field(min_length=1, max_length=240)
    team_id: str = Field(min_length=1, max_length=240)
    target_agent_types: tuple[str, ...]
    tools: tuple[HostedMCPToolDefinitionV1, ...]
    bind_host: str
    port: int = Field(ge=1024, le=65535)
    lifecycle: HostedMCPLifecycle
    created_at_utc: datetime
    updated_at_utc: datetime
    installed_by_principal_id: str | None = None
    last_transition_by_principal_id: str | None = None

    @field_validator("server_name")
    @classmethod
    def _validate_server_name(cls, value: str) -> str:
        normalized = str(value or "").strip().lower()
        if not _NAME.fullmatch(normalized):
            raise ValueError("Hosted MCP server names must be lowercase semantic names.")
        return normalized

    @model_validator(mode="after")
    def _validate_definition(self) -> "MCPHostedServerDefinitionV1":
        names = [tool.name for tool in self.tools]
        if not names or len(names) != len(set(names)):
            raise ValueError("Hosted MCP definitions require unique tools.")
        expected = self.calculate_definition_digest()
        if self.definition_digest and self.definition_digest != expected:
            raise ValueError("Hosted MCP definition digest mismatch.")
        expected_id = f"mcp_host_{self.server_name}_{expected[:16]}"
        if self.hosted_server_id != expected_id:
            raise ValueError("Hosted MCP server id does not match its definition.")
        return self

    def calculate_definition_digest(self) -> str:
        return contract_sha256(
            self.model_dump(
                mode="json",
                exclude={
                    "hosted_server_id",
                    "definition_digest",
                    "lifecycle",
                    "created_at_utc",
                    "updated_at_utc",
                    "installed_by_principal_id",
                    "last_transition_by_principal_id",
                },
            )
        )

    @classmethod
    def build(cls, **values: Any) -> "MCPHostedServerDefinitionV1":
        now = values.get("created_at_utc") or utc_now()
        normalized_values = {
            **values,
            "server_name": str(values.get("server_name") or "").strip().lower(),
            "target_agent_types": tuple(values.get("target_agent_types") or ()),
            "tools": tuple(
                item
                if isinstance(item, HostedMCPToolDefinitionV1)
                else HostedMCPToolDefinitionV1.model_validate(item)
                for item in values.get("tools") or ()
            ),
            "lifecycle": HostedMCPLifecycle(values.get("lifecycle")),
        }
        seed = cls.model_construct(
            schema_version=HOSTED_MCP_SCHEMA_VERSION,
            hosted_server_id="pending",
            definition_digest="",
            created_at_utc=now,
            updated_at_utc=values.get("updated_at_utc") or now,
            installed_by_principal_id=normalized_values.get(
                "installed_by_principal_id"
            ),
            last_transition_by_principal_id=normalized_values.get(
                "last_transition_by_principal_id"
            ),
            **{
                key: value
                for key, value in normalized_values.items()
                if key
                not in {
                    "created_at_utc",
                    "updated_at_utc",
                    "installed_by_principal_id",
                    "last_transition_by_principal_id",
                }
            },
        )
        digest = seed.calculate_definition_digest()
        return cls.model_validate(
            {
                **seed.model_dump(mode="python"),
                "hosted_server_id": f"mcp_host_{seed.server_name}_{digest[:16]}",
                "definition_digest": digest,
            }
        )


class HostedMCPStartFailureV1(BaseModel):
    """Why the broker could not start the sidecar of a server set to run, and when it last tried.

    The broker writes it beside the server's running state and deletes it when
    a start succeeds or the server is stopped; the server stays set to run, so
    the broker tries again at its next start and when an agent uses the tools.
    A missing file means no failure, so instances that never wrote one need no
    upgrade step.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: int = 1
    hosted_server_id: str = Field(min_length=1, max_length=240)
    reason: str = Field(min_length=1, max_length=4000)
    failed_at: datetime


class HostedMCPStartError(RuntimeError):
    """A hosted MCP server's sidecar did not start; the message says why and what to do.

    A start failure is not a refusal: what failed is the server's process, for
    example on a port another process holds, not anyone's access. Nothing about
    access or the connection changes when someone frees that port, so a caller
    that waits for such a change would wait forever.
    The broker therefore answers it with ``HOSTED_MCP_START_FAILED_STATUS``
    (503) and ``HOSTED_MCP_START_FAILED``, never with a refusal's 403, and the
    agents' loader (``custom_agents.mcp_server_tools``) tries the server again
    on its schedule, as it does a server that does not answer. Both clients of
    the broker (the agent's ``BrokeredMCPClient`` and the manager's
    ``MCPHostingAdministration``) raise this error again from that answer, so
    agents, Web Chat and ``/mcp`` all show the same reason. It is a
    ``RuntimeError`` so those callers' existing handling shows its text.
    """

    def broker_answer(self) -> dict[str, str]:
        """The JSON body of the broker's answer: the reason, and the code that marks a start failure."""

        return {"error": str(self), "reason_code": HOSTED_MCP_START_FAILED}

    @classmethod
    def from_broker_answer(cls, status_code: int, body: str) -> "HostedMCPStartError | None":
        """The error again from the broker's answer, or ``None`` when the answer is not a start failure.

        Only the 503 that carries ``HOSTED_MCP_START_FAILED`` counts: any other
        503 (for example from a proxy) stays an ordinary HTTP error.
        """

        if status_code != HOSTED_MCP_START_FAILED_STATUS:
            return None
        try:
            payload = json.loads(body)
        except ValueError:
            return None
        if not isinstance(payload, dict) or payload.get("reason_code") != HOSTED_MCP_START_FAILED:
            return None
        return cls(str(payload.get("error") or "").strip() or "The hosted MCP server could not start.")


def start_failure_sentence(failure: HostedMCPStartFailureV1) -> str:
    """The plain words lists of hosted servers use for a server set to run whose sidecar did not start."""

    return (
        f"Set to run, but it could not start: {failure.reason} "
        f"(last tried {failure.failed_at:%Y-%m-%d %H:%M} UTC)"
    )


class MCPHostingPaths(BaseModel):
    model_config = ConfigDict(arbitrary_types_allowed=True, frozen=True)

    instance_root: Path
    definitions: Path
    removed: Path
    state: Path
    tokens: Path
    logs: Path
    start_failures: Path
    lock: Path

    def ensure(self) -> None:
        for path in (
            self.definitions,
            self.removed,
            self.state,
            self.tokens,
            self.logs,
            self.start_failures,
        ):
            path.mkdir(parents=True, mode=0o700, exist_ok=True)

    def shown(self, path: Path) -> str:
        """A hosting file as reasons name it: relative to the instance root.

        Start-failure reasons reach agents' models and every person they work
        for, so they never carry the host's absolute paths (account names and
        the host's folder layout); the broker's own log has those.
        """

        return path.relative_to(self.instance_root).as_posix()


def resolve_mcp_hosting_paths(config: Any) -> MCPHostingPaths:
    """Hosted MCP server folders, derived from the instance root (``InstanceLayout``)."""

    layout = InstanceLayout.at(config.instance_root_path)
    definitions = layout.mcp_hosting_definitions_dir
    state = layout.mcp_hosting_state_dir
    return MCPHostingPaths(
        instance_root=layout.root,
        definitions=definitions,
        removed=state / "removed",
        state=state,
        tokens=state / "tokens",
        logs=state / "logs",
        start_failures=state / "start-failures",
        lock=state / ".hosting.lock",
    )


@contextmanager
def _lock(paths: MCPHostingPaths) -> Iterator[None]:
    paths.ensure()
    handle = paths.lock.open("a+", encoding="utf-8")
    try:
        try:
            import fcntl

            fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        except ImportError:
            pass
        yield
    finally:
        try:
            import fcntl

            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        except ImportError:
            pass
        handle.close()


def _atomic_json(path: Path, value: BaseModel) -> None:
    temporary = path.with_name(f".{path.name}.{new_id_suffix()}.tmp")
    try:
        temporary.write_text(
            json.dumps(value.model_dump(mode="json"), indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        temporary.chmod(0o600)
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


class MCPHostedServerStore:
    def __init__(self, paths: MCPHostingPaths) -> None:
        self.paths = paths
        paths.ensure()

    def list_definitions(self) -> tuple[MCPHostedServerDefinitionV1, ...]:
        return tuple(
            MCPHostedServerDefinitionV1.model_validate_json(path.read_text(encoding="utf-8"))
            for path in sorted(self.paths.definitions.glob("mcp_host_*.json"))
        )

    def require(self, hosted_server_id: str) -> MCPHostedServerDefinitionV1:
        path = self.paths.definitions / f"{str(hosted_server_id or '').strip()}.json"
        if not path.is_file():
            raise FileNotFoundError(f"Hosted MCP definition not found: {hosted_server_id}")
        return MCPHostedServerDefinitionV1.model_validate_json(path.read_text(encoding="utf-8"))

    def save(self, definition: MCPHostedServerDefinitionV1) -> MCPHostedServerDefinitionV1:
        validated = MCPHostedServerDefinitionV1.model_validate(definition)
        with _lock(self.paths):
            _atomic_json(
                self.paths.definitions / f"{validated.hosted_server_id}.json",
                validated,
            )
        return validated

    def update_lifecycle(
        self,
        hosted_server_id: str,
        lifecycle: HostedMCPLifecycle,
        *,
        principal_id: str,
    ) -> MCPHostedServerDefinitionV1:
        current = self.require(hosted_server_id)
        updated = MCPHostedServerDefinitionV1.model_validate(
            {
                **current.model_dump(mode="python"),
                "lifecycle": lifecycle,
                "updated_at_utc": utc_now(),
                "installed_by_principal_id": (
                    principal_id
                    if lifecycle == HostedMCPLifecycle.STOPPED
                    and current.lifecycle == HostedMCPLifecycle.PENDING
                    else current.installed_by_principal_id
                ),
                "last_transition_by_principal_id": principal_id,
            }
        )
        return self.save(updated)

    def remove_recoverably(
        self,
        hosted_server_id: str,
        *,
        principal_id: str,
    ) -> MCPHostedServerDefinitionV1:
        current = self.require(hosted_server_id)
        removed = MCPHostedServerDefinitionV1.model_validate(
            {
                **current.model_dump(mode="python"),
                "lifecycle": HostedMCPLifecycle.REMOVED,
                "updated_at_utc": utc_now(),
                "last_transition_by_principal_id": principal_id,
            }
        )
        with _lock(self.paths):
            destination = self.paths.removed / f"{removed.hosted_server_id}.json"
            _atomic_json(destination, removed)
            (self.paths.definitions / f"{removed.hosted_server_id}.json").unlink()
        return removed

    def _start_failure_path(self, hosted_server_id: str) -> Path:
        return self.paths.start_failures / f"{str(hosted_server_id or '').strip()}.json"

    def record_start_failure(self, hosted_server_id: str, reason: str, *, at: datetime) -> HostedMCPStartFailureV1:
        """Keep why the server's sidecar could not start (the broker, for a server set to run)."""

        failure = HostedMCPStartFailureV1(hosted_server_id=hosted_server_id, reason=reason[:4000], failed_at=at)
        with _lock(self.paths):
            _atomic_json(self._start_failure_path(hosted_server_id), failure)
        return failure

    def clear_start_failure(self, hosted_server_id: str) -> None:
        """Forget the last start failure: the sidecar started, or the server was stopped."""

        with _lock(self.paths):
            self._start_failure_path(hosted_server_id).unlink(missing_ok=True)

    def start_failure(self, hosted_server_id: str) -> HostedMCPStartFailureV1 | None:
        path = self._start_failure_path(hosted_server_id)
        if not path.is_file():
            return None
        return HostedMCPStartFailureV1.model_validate_json(path.read_text(encoding="utf-8"))




def resolve_hosted_broker_config(config: Any, hosted_server_id: str) -> dict[str, Any]:
    """Return broker-only transport material, including the sidecar bearer token."""

    paths = resolve_mcp_hosting_paths(config)
    definition = MCPHostedServerStore(paths).require(hosted_server_id)
    token_path = paths.tokens / f"{definition.hosted_server_id}.token"
    if not token_path.is_file():
        raise PermissionError("Hosted MCP sidecar token has not been provisioned.")
    token = token_path.read_text(encoding="utf-8").strip()
    if not token:
        raise PermissionError("Hosted MCP sidecar token is empty.")
    return {
        "name": definition.server_name,
        "transport": "streamable_http",
        "server_url": f"http://{definition.bind_host}:{definition.port}/mcp",
        "headers": {"Authorization": f"Bearer {token}"},
        "tool_name_prefix": definition.server_name,
        "include_tools": [tool.name for tool in definition.tools],
    }

__all__ = [
    "HOSTED_MCP_SCHEMA_VERSION",
    "HOSTED_MCP_START_FAILED",
    "HOSTED_MCP_START_FAILED_STATUS",
    "HostedMCPBehavior",
    "HostedMCPLifecycle",
    "HostedMCPStartError",
    "HostedMCPStartFailureV1",
    "HostedMCPToolDefinitionV1",
    "MCPHostedServerDefinitionV1",
    "MCPHostedServerStore",
    "MCPHostingPaths",
    "resolve_hosted_broker_config",
    "resolve_mcp_hosting_paths",
    "start_failure_sentence",
]

