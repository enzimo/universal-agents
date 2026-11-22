"""Broker-owned SSH runtime, action dispatcher, and gateway revocation routes.

All Sealbox-backed SSH credential resolution, ephemeral ``ssh-agent`` management,
live PTY ownership, async remote commands, secret scrubbing, and startup crash
recovery run exclusively inside the ``secure_tool_broker`` process on its single
event loop. Agent processes call ``POST /v1/tools/execution_ssh`` with their
signed task context; ``BrokerAccessGate`` verifies the calling process token,
the signed context, and Cedar access on ``fn:execution_control``, and this
module builds ``SSHCallerContext`` strictly from that verified context.
"""

from __future__ import annotations

import asyncio
import base64
from collections.abc import AsyncIterator, Mapping
from dataclasses import dataclass
import logging
import os
from pathlib import Path
from typing import Any

from aiohttp import web

from custom_messages.contracts.runtime_v1 import TEAM_ARCHITECT_AGENT_TYPE
from id_generation import new_id
from runtime_hosting.artifacts import FilesystemArtifactStore
from runtime_hosting.authorization import (
    ExecutionAccessGuard,
    ExecutionAuthorizationDenied,
    SSHCallerContext,
)
from runtime_hosting.execution_contracts import InteractiveSessionV1, OwnerScopeKind
from runtime_hosting.ssh import (
    CatalogSSHCredentialMaterialProvider,
    CompositeSSHCredentialMaterialProvider,
    ConfiguredSSHCredentialMaterialProvider,
    SSHCredentialMaterial,
    SSHSessionBroker,
    SSHSessionUnavailable,
)
from runtime_hosting.ssh_credentials import (
    InvalidSSHCredentialValueError,
    make_catalog_secret_resolver,
)
from runtime_hosting.ssh_network import SSHNetworkPolicy
from runtime_hosting.ssh_operations import MAX_BUNDLE_FILES, MAX_BUNDLE_TOTAL_BYTES
from runtime_settings.execution import ExecutionSSHSettings
from runtime_settings.instance_layout import InstanceLayout
from secure_tool_broker.access_gate import BrokerAccessGate
from secure_tool_broker.defaults import (
    EXECUTION_SSH_REVOKE_SESSION_ROUTE,
    EXECUTION_SSH_REVOKE_TARGET_ROUTE,
)
from secure_tools.providers import ToolContext, ValidatedAction
from storage.config import StorageSettings
from storage.execution_control_repository import (
    StaleExecutionFence,
    create_execution_control_repository,
)

LOGGER = logging.getLogger(__name__)
EXECUTION_SSH_PROVIDER = "execution_ssh"


@dataclass(frozen=True)
class BrokerExecutionSSHConfig:
    """Operator-owned SSH settings projected onto the secure tool broker."""

    instance_root_path: str
    instance_name: str
    storage_settings: StorageSettings
    ssh: ExecutionSSHSettings
    lead_agent_type: str = TEAM_ARCHITECT_AGENT_TYPE

    @classmethod
    def from_runtime_settings(
        cls,
        *,
        instance_root_path: str,
        instance_name: str,
        storage_settings: StorageSettings,
        ssh: ExecutionSSHSettings,
        lead_agent_type: str = TEAM_ARCHITECT_AGENT_TYPE,
    ) -> "BrokerExecutionSSHConfig":
        return cls(
            instance_root_path=str(instance_root_path),
            instance_name=str(instance_name or "universal-agents"),
            storage_settings=storage_settings,
            ssh=ssh,
            lead_agent_type=str(lead_agent_type or TEAM_ARCHITECT_AGENT_TYPE),
        )


def classify_ssh_error(exc: Exception) -> dict[str, Any]:
    """Return structured error metadata (never raw secrets) for an SSH exception."""

    payload: dict[str, Any] = {"error": str(exc)}
    if isinstance(exc, InvalidSSHCredentialValueError):
        payload["error_code"] = "invalid_credential_value"
        payload["credential_key"] = exc.credential_key
        payload["field_name"] = exc.field_name
        payload["next_step"] = (
            f"Ask an administrator to update field {exc.field_name!r} in credential "
            f"{exc.credential_key!r} as a single line without line breaks and save it again, then retry."
        )
    elif isinstance(exc, StaleExecutionFence):
        payload["error_code"] = "session_lease_lost"
        payload["next_step"] = (
            "The SSH session lease was lost or superseded; inspect session status with "
            "list_execution_ssh_sessions and open a new session if needed."
        )
    elif isinstance(exc, (ExecutionAuthorizationDenied, PermissionError)):
        payload["error_code"] = "execution_access_refused"
    elif isinstance(exc, TimeoutError):
        payload["error_code"] = "execution_timeout"
    elif isinstance(exc, (FileNotFoundError, LookupError)):
        payload["error_code"] = "not_found"
    elif isinstance(exc, SSHSessionUnavailable):
        lowered = str(exc).lower()
        payload["error_code"] = "not_found" if "does not exist" in lowered else "session_unavailable"
    elif isinstance(exc, ValueError):
        payload["error_code"] = "invalid_argument"
    return payload


def _broker_material_path(layout: InstanceLayout, value: str) -> Path:
    path = Path(value).expanduser()
    if not path.is_absolute():
        path = layout.root / path
    resolved = path.resolve()
    workspace_root = layout.workspaces_dir.resolve()
    if resolved.is_relative_to(workspace_root):
        raise PermissionError("SSH key material must stay outside the agent workspace.")
    return resolved


class BrokerExecutionSSHRuntime:
    """Single-owner SSH broker runtime inside ``secure_tool_broker``."""

    def __init__(
        self,
        *,
        config: BrokerExecutionSSHConfig,
        access_gate: BrokerAccessGate,
        logger: logging.Logger | None = None,
    ) -> None:
        self._config = config
        self._access_gate = access_gate
        self._logger = logger or LOGGER
        self._layout = InstanceLayout(Path(config.instance_root_path))
        self._repository = create_execution_control_repository(config.storage_settings)
        self._artifact_store = FilesystemArtifactStore(self._layout.execution_artifacts_dir)
        self._broker: SSHSessionBroker | None = None

    @property
    def enabled(self) -> bool:
        return bool(self._config.ssh.enabled)

    @property
    def broker(self) -> SSHSessionBroker:
        if not self._config.ssh.enabled:
            raise RuntimeError("execution_control.ssh.enabled is false.")
        if self._broker is None:
            self._broker = self._build_broker()
        return self._broker

    def _build_broker(self) -> SSHSessionBroker:
        ssh_settings = self._config.ssh
        materials: dict[str, SSHCredentialMaterial] = {}
        for target in ssh_settings.targets:
            identity = _broker_material_path(self._layout, target.identity_file)
            known_hosts = _broker_material_path(self._layout, target.known_hosts_file)
            material = SSHCredentialMaterial(
                identity_file=identity,
                known_hosts_file=known_hosts,
            )
            prior = materials.get(target.credential_binding_id)
            if prior is not None and prior != material:
                raise ValueError(
                    "An SSH credential binding id maps to inconsistent operator key paths."
                )
            materials[target.credential_binding_id] = material

        catalog_provider = CatalogSSHCredentialMaterialProvider(
            ephemeral_base_dir=self._layout.ssh_ephemeral_dir,
            secret_resolver=make_catalog_secret_resolver(),
            logger=self._logger,
        )
        material_provider = CompositeSSHCredentialMaterialProvider(
            configured_provider=ConfiguredSSHCredentialMaterialProvider(materials),
            catalog_provider=catalog_provider,
        )
        auth_enabled = self._access_gate.access is not None
        guard = ExecutionAccessGuard(
            authorization_enabled=auth_enabled,
            access=self._access_gate.access if auth_enabled else None,
            catalogs=self._access_gate.catalogs if auth_enabled else None,
        )
        return SSHSessionBroker(
            repository=self._repository,
            authorization_guard=guard,
            material_provider=material_provider,
            artifact_store=self._artifact_store,
            team_id=self._access_gate.team_id,
            broker_id=f"{self._config.instance_name}.secure-broker-ssh.{os.getpid()}",
            ssh_executable=ssh_settings.ssh_executable,
            lease_ttl_seconds=ssh_settings.lease_ttl_seconds,
            transcript_limit_bytes=ssh_settings.transcript_limit_bytes,
            network_policy=SSHNetworkPolicy.from_settings(ssh_settings),
            allow_password_bootstrap=ssh_settings.allow_password_bootstrap,
            logger=self._logger,
        )

    async def startup(self) -> None:
        """Recover crashed sessions and sweep orphaned ephemeral SSH dirs on broker start."""

        if not self._config.ssh.enabled:
            return
        broker = self.broker
        lost = await asyncio.to_thread(broker.recover_after_restart)
        self._logger.info(
            "Initialized broker-owned SSH runtime. event=broker_ssh_runtime_started "
            "team_id=%s recovered_lost_sessions=%d",
            self._access_gate.team_id,
            len(lost),
        )

    async def shutdown(self) -> None:
        """Terminate running SSH commands, close live PTYs, and wipe ephemeral dirs."""

        if self._broker is None:
            return
        broker = self._broker
        self._broker = None
        await broker.shutdown()

    def _require_owned_session(self, session_id: str, person_id: str) -> InteractiveSessionV1:
        session = self._repository.get_interactive_session(
            team_id=self._access_gate.team_id,
            session_id=session_id,
        )
        if session is None:
            raise LookupError("SSH session not found.")
        if session.owner.team_id != self._access_gate.team_id:
            raise LookupError("SSH session not found.")
        if session.owner.scope_kind is OwnerScopeKind.USER and session.owner.owner_id != person_id:
            raise LookupError("SSH session not found.")
        return session

    def _verified_caller(self, context: ToolContext) -> SSHCallerContext:
        person_id = context.require_user()
        agent_type = str(context.named_agent_type or "").strip()
        if not agent_type:
            raise ExecutionAuthorizationDenied("Verified agent type is required for SSH operations.")
        return SSHCallerContext(
            agent_type=agent_type,
            task_id=context.work_id,
            person_id=person_id,
        )

    @staticmethod
    def _decode_bundle_entries(raw_entries: Any) -> list[tuple[str, bytes]]:
        if not isinstance(raw_entries, list) or not raw_entries:
            raise ValueError("entries must be a non-empty list of bundle files.")
        if len(raw_entries) > MAX_BUNDLE_FILES:
            raise ValueError(f"Workspace bundle exceeds the maximum file count ({MAX_BUNDLE_FILES}).")
        entries: list[tuple[str, bytes]] = []
        total_bytes = 0
        for item in raw_entries:
            if not isinstance(item, Mapping):
                raise ValueError("Each bundle entry must be an object with path and content_base64.")
            rel_path = str(item.get("path") or "").strip()
            encoded = str(item.get("content_base64") or "")
            if not rel_path:
                raise ValueError("Bundle entry path must not be empty.")
            try:
                data = base64.b64decode(encoded.encode("ascii"), validate=True)
            except Exception as exc:
                raise ValueError(f"Invalid base64 payload for bundle entry {rel_path!r}.") from exc
            total_bytes += len(data)
            if total_bytes > MAX_BUNDLE_TOTAL_BYTES:
                raise ValueError(
                    f"Total workspace bundle size exceeds the maximum limit ({MAX_BUNDLE_TOTAL_BYTES} bytes)."
                )
            entries.append((rel_path, data))
        return entries

    async def execute_action(
        self,
        validated: ValidatedAction,
        context: ToolContext,
    ) -> dict[str, Any]:
        """Dispatch one gate-admitted ``execution_ssh`` action on the broker's event loop."""

        try:
            content = await self._dispatch_action(validated.action, validated.params, context)
            return {"status": "success", "content": content}
        except Exception as exc:
            error_payload = classify_ssh_error(exc)
            message = str(error_payload.pop("error", str(exc)))
            self._logger.warning(
                "Broker SSH action failed. event=broker_ssh_action_failed action=%s "
                "error_code=%s agent_type=%s",
                validated.action,
                error_payload.get("error_code", "-"),
                context.named_agent_type or "-",
            )
            return {"status": "error", "error": message, "content": error_payload}

    async def _dispatch_action(
        self,
        action: str,
        params: Mapping[str, Any],
        context: ToolContext,
    ) -> dict[str, Any]:
        caller = self._verified_caller(context)
        broker = self.broker

        if action == "probe_execution_ssh_host_key":
            if caller.agent_type != self._config.lead_agent_type:
                raise ExecutionAuthorizationDenied(
                    f"probe_execution_ssh_host_key is a manager-only tool. Escalate to {self._config.lead_agent_type}."
                )
            host = str(params.get("host") or "").strip()
            port = int(params.get("port", 22))
            probe_timeout = int(params.get("timeout_seconds", 10))
            probe_result = await asyncio.to_thread(
                broker.probe_host_key,
                host=host,
                port=port,
                timeout_seconds=probe_timeout,
            )
            return probe_result.to_payload()

        session_id = str(params.get("session_id") or "").strip()
        if not session_id:
            raise ValueError("session_id is required.")
        self._require_owned_session(session_id, caller.person_id or "")

        if action == "open_execution_ssh_session":
            opened = await broker.open(
                session_id=session_id,
                lease_token=new_id("ssh_lease"),
                caller=caller,
            )
            return opened.model_dump(mode="json")

        if action == "read_execution_ssh_session":
            maximum_bytes = max(1, min(int(params.get("maximum_bytes", 65536)), 1_048_576))
            read_timeout = max(0.0, min(float(params.get("timeout_seconds", 1.0)), 30.0))
            payload = await broker.read(
                session_id=session_id,
                maximum_bytes=maximum_bytes,
                timeout_seconds=read_timeout,
                caller=caller,
            )
            return {
                "session_id": session_id,
                "bytes": len(payload),
                "text": payload.decode("utf-8", errors="replace"),
                "base64": base64.b64encode(payload).decode("ascii"),
            }

        if action == "write_execution_ssh_session":
            text = str(params.get("text") if params.get("text") is not None else "")
            written = await broker.write(
                session_id=session_id,
                payload=text.encode("utf-8"),
                caller=caller,
            )
            return {"session_id": session_id, "written_bytes": written}

        if action == "resize_execution_ssh_session":
            rows = int(params["terminal_rows"])
            columns = int(params["terminal_columns"])
            await broker.resize(
                session_id=session_id,
                rows=rows,
                columns=columns,
                caller=caller,
            )
            return {
                "session_id": session_id,
                "terminal_rows": rows,
                "terminal_columns": columns,
            }

        if action == "close_execution_ssh_session":
            closed = await broker.close(session_id=session_id, caller=caller)
            return closed.model_dump(mode="json")

        if action == "run_execution_ssh_command":
            command = str(params.get("command") or "")
            cmd_timeout = float(params.get("timeout_seconds", 120.0))
            wait_seconds = max(0.0, min(float(params.get("wait_seconds", 5.0)), 60.0))
            cmd_result = await broker.run_command(
                session_id=session_id,
                command=command,
                caller=caller,
                timeout_seconds=cmd_timeout,
                wait_seconds=wait_seconds,
            )
            return cmd_result.to_payload()

        if action == "get_execution_ssh_command":
            command_id = str(params.get("command_id") or "").strip()
            wait_seconds = max(0.0, min(float(params.get("wait_seconds", 0.0)), 60.0))
            cmd_result = await broker.get_command(
                session_id=session_id,
                command_id=command_id,
                caller=caller,
                wait_seconds=wait_seconds,
            )
            return cmd_result.to_payload()

        if action == "enroll_execution_ssh_key":
            enroll_timeout = float(params.get("timeout_seconds", 60.0))
            enroll_result = await broker.enroll_ssh_key(
                session_id=session_id,
                caller=caller,
                timeout_seconds=enroll_timeout,
            )
            return enroll_result.to_payload()

        if action == "run_execution_ssh_sudo":
            raw_recipe_params = params.get("recipe_params")
            recipe_params = dict(raw_recipe_params) if isinstance(raw_recipe_params, Mapping) else None
            sudo_timeout = float(params.get("timeout_seconds", 120.0))
            wait_seconds = max(0.0, min(float(params.get("wait_seconds", 5.0)), 60.0))
            sudo_result = await broker.run_sudo(
                session_id=session_id,
                caller=caller,
                command=str(params.get("command") or "") or None,
                secret_recipe=str(params.get("secret_recipe") or "") or None,
                recipe_params=recipe_params,
                extra_secret_field=str(params.get("extra_secret_field") or "") or None,
                timeout_seconds=sudo_timeout,
                wait_seconds=wait_seconds,
            )
            return sudo_result.to_payload()

        if action == "upload_execution_ssh_bundle":
            entries = self._decode_bundle_entries(params.get("entries"))
            remote_destination = str(params.get("remote_destination") or "")
            upload_timeout = float(params.get("timeout_seconds", 120.0))
            upload_result = await broker.upload_bundle(
                session_id=session_id,
                entries=entries,
                remote_destination=remote_destination,
                caller=caller,
                timeout_seconds=upload_timeout,
            )
            return upload_result.to_payload()

        raise ValueError(f"Unsupported execution_ssh action: {action!r}")

    async def revoke_session(self, *, session_id: str, revoked_by: str) -> dict[str, Any]:
        if not self._config.ssh.enabled:
            self._repository.revoke_interactive_session(
                team_id=self._access_gate.team_id,
                session_id=session_id,
                revoked_by=revoked_by,
            )
            return {"revoked": True, "session_id": session_id, "terminated_live": False}
        was_live = session_id in self.broker._sessions
        closed = await self.broker.revoke_session(session_id=session_id, revoked_by=revoked_by)
        return {
            "revoked": True,
            "session_id": session_id,
            "terminated_live": was_live,
            "session": closed.model_dump(mode="json") if closed is not None else None,
        }

    async def revoke_target(self, *, target_id: str, revoked_by: str) -> dict[str, Any]:
        if not self._config.ssh.enabled:
            revoked = self._repository.revoke_ssh_target(
                team_id=self._access_gate.team_id,
                target_id=target_id,
                revoked_by=revoked_by,
            )
            return {
                "revoked": revoked is not None,
                "target_id": target_id,
                "target": revoked.model_dump(mode="json") if revoked is not None else None,
            }
        revoked = await self.broker.revoke_target(target_id=target_id, revoked_by=revoked_by)
        return {
            "revoked": revoked is not None,
            "target_id": target_id,
            "target": revoked.model_dump(mode="json") if revoked is not None else None,
        }


def build_execution_ssh_runtime(
    *,
    config: BrokerExecutionSSHConfig | None,
    access_gate: BrokerAccessGate,
    logger: logging.Logger | None = None,
) -> BrokerExecutionSSHRuntime | None:
    if config is None:
        return None
    return BrokerExecutionSSHRuntime(config=config, access_gate=access_gate, logger=logger)


async def execute_ssh_broker_action(
    runtime: BrokerExecutionSSHRuntime | None,
    validated: ValidatedAction,
    context: ToolContext,
) -> dict[str, Any]:
    if runtime is None:
        return {
            "status": "error",
            "error": "Brokered SSH runtime is not configured on this secure tool broker.",
            "content": {"error_code": "broker_unavailable"},
        }
    return await runtime.execute_action(validated, context)


def register_execution_ssh_app(
    app: web.Application,
    runtime: BrokerExecutionSSHRuntime | None,
) -> None:
    """Register gateway-only SSH revocation routes and broker startup/shutdown lifecycle."""

    async def _handle_revoke_session(request: web.Request) -> web.Response:
        if runtime is None:
            raise web.HTTPServiceUnavailable(
                text="Brokered SSH runtime is not configured on this secure tool broker."
            )
        session_id = str(request.match_info.get("session_id") or "").strip()
        if not session_id:
            raise web.HTTPBadRequest(text="session_id is required.")
        try:
            body = await request.json()
        except Exception:
            body = {}
        revoked_by = str((body or {}).get("revoked_by") or "administrator").strip() or "administrator"
        result = await runtime.revoke_session(session_id=session_id, revoked_by=revoked_by)
        return web.json_response(result)

    async def _handle_revoke_target(request: web.Request) -> web.Response:
        if runtime is None:
            raise web.HTTPServiceUnavailable(
                text="Brokered SSH runtime is not configured on this secure tool broker."
            )
        target_id = str(request.match_info.get("target_id") or "").strip()
        if not target_id:
            raise web.HTTPBadRequest(text="target_id is required.")
        try:
            body = await request.json()
        except Exception:
            body = {}
        revoked_by = str((body or {}).get("revoked_by") or "administrator").strip() or "administrator"
        result = await runtime.revoke_target(target_id=target_id, revoked_by=revoked_by)
        return web.json_response(result)

    async def _ssh_lifecycle_context(_app: web.Application) -> AsyncIterator[None]:
        if runtime is not None:
            try:
                await runtime.startup()
            except Exception:
                LOGGER.exception(
                    "Broker SSH startup recovery failed. event=broker_ssh_startup_failed"
                )
        yield
        if runtime is not None:
            await runtime.shutdown()

    app.router.add_post(EXECUTION_SSH_REVOKE_SESSION_ROUTE, _handle_revoke_session)
    app.router.add_post(EXECUTION_SSH_REVOKE_TARGET_ROUTE, _handle_revoke_target)
    app.cleanup_ctx.append(_ssh_lifecycle_context)


__all__ = [
    "BrokerExecutionSSHConfig",
    "BrokerExecutionSSHRuntime",
    "EXECUTION_SSH_PROVIDER",
    "build_execution_ssh_runtime",
    "classify_ssh_error",
    "execute_ssh_broker_action",
    "register_execution_ssh_app",
]
