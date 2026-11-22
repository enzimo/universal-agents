"""Broker-owned SSH runtime, action dispatcher, and gateway revocation routes.

All brokered SSH operations — both Sealbox-backed enrolled targets and
operator-pinned disk-key targets — run exclusively inside the
``secure_tool_broker`` process on its single event loop. Agent processes call
``POST /v1/tools/execution_ssh`` with their signed task context;
``BrokerAccessGate`` verifies the calling process token, the signed context,
and Cedar access on ``fn:execution_control``, and this module builds
``SSHCallerContext`` strictly from that verified context.

Operator SSH settings (``execution_control.ssh``) are reloaded from the
instance configuration file whenever it changes, so turning ``enabled`` off,
tightening network rules, changing ``allow_password_bootstrap``, or updating
pinned targets takes effect without restarting the broker:

- Turning ``enabled`` off stops all live SSH sessions and running commands and
  refuses new actions with ``ssh_disabled``.
- Tightening network rules or removing a pinned target re-checks live work once
  for the new settings and closes/stops sessions and commands that no longer pass.
- Any file version that cannot be read, validated, or applied (missing file,
  empty or incomplete file without ``schema_version``, invalid YAML, schema
  validation error, inconsistent pinned key bindings, or workspace-scoped key
  paths) follows one rule: keep the last good settings during a short grace
  period (default 3 seconds) so an in-place save in progress never drops live
  work; if the file is still unusable afterwards, fail closed (stop active work
  with the real reason and refuse actions with ``ssh_settings_unusable``).
"""

from __future__ import annotations

import asyncio
import base64
from collections.abc import AsyncIterator, Callable, Mapping
from dataclasses import dataclass
import hashlib
import logging
import os
from pathlib import Path
import time
from typing import Any

from aiohttp import web
import yaml

from custom_messages.contracts.runtime_v1 import TEAM_ARCHITECT_AGENT_TYPE
from default_values import EXECUTION_CONTROL_DEFAULTS
from id_generation import new_id
from runtime_hosting.artifacts import FilesystemArtifactStore
from runtime_hosting.authorization import (
    ExecutionAccessGuard,
    ExecutionAuthorizationDenied,
    SSH_DISABLED_MESSAGE,
    SSH_DISABLED_NEXT_STEP,
    SSHCallerContext,
    SSHDisabledError,
    SSHSettingsUnusableError,
)
from runtime_hosting.execution_contracts import InteractiveSessionV1, OwnerScopeKind
from runtime_hosting.ssh import (
    MAX_PTY_WRITE_BYTES,
    CatalogSSHCredentialMaterialProvider,
    CompositeSSHCredentialMaterialProvider,
    ConfiguredSSHCredentialMaterialProvider,
    PinnedSSHTargetKey,
    SSHCredentialMaterial,
    SSHSessionBroker,
    SSHSessionUnavailable,
    SSHSettingsSyncOutcome,
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
    ExecutionControlRepository,
    StaleExecutionFence,
    create_execution_control_repository,
)

LOGGER = logging.getLogger(__name__)
EXECUTION_SSH_PROVIDER = "execution_ssh"
# Workspace bundle uploads base64-encode up to 16 MiB (~21.3 MiB plus JSON overhead);
# applied only to POST /v1/tools/execution_ssh while other broker routes keep aiohttp's default limit.
EXECUTION_SSH_MAX_REQUEST_BYTES = 32 * 1024 * 1024
DEFAULT_UNUSABLE_SETTINGS_GRACE_SECONDS = 3.0
_UNCHECKED_FINGERPRINT = object()


class SSHRecoveryError(RuntimeError):
    """Raised when broker SSH crash recovery has not completed."""


@dataclass(frozen=True)
class BrokerExecutionSSHConfig:
    """Operator-owned SSH settings projected onto the secure tool broker."""

    instance_root_path: str
    instance_name: str
    storage_settings: StorageSettings
    ssh: ExecutionSSHSettings
    lead_agent_type: str = TEAM_ARCHITECT_AGENT_TYPE
    config_path: str | None = None
    config_source_revision: str | None = None

    @classmethod
    def from_runtime_settings(
        cls,
        *,
        instance_root_path: str,
        instance_name: str,
        storage_settings: StorageSettings,
        ssh: ExecutionSSHSettings,
        execution_enabled: bool = EXECUTION_CONTROL_DEFAULTS.enabled.value,
        lead_agent_type: str = TEAM_ARCHITECT_AGENT_TYPE,
        config_path: str | Path | None = None,
        config_source_revision: str | None = None,
    ) -> "BrokerExecutionSSHConfig":
        effective_ssh = ssh.model_copy(deep=True)
        if not execution_enabled and effective_ssh.enabled:
            effective_ssh = effective_ssh.model_copy(update={"enabled": False})
        return cls(
            instance_root_path=str(instance_root_path),
            instance_name=str(instance_name or "universal-agents"),
            storage_settings=storage_settings,
            ssh=effective_ssh,
            lead_agent_type=str(lead_agent_type or TEAM_ARCHITECT_AGENT_TYPE),
            config_path=str(config_path) if config_path is not None else None,
            config_source_revision=config_source_revision,
        )


@dataclass(frozen=True)
class ExecutionSSHSettingsSnapshot:
    """Validated SSH settings and broker key table, or a fail-closed unusable state."""

    settings: ExecutionSSHSettings
    materials: Mapping[str, SSHCredentialMaterial]
    unusable_error: str | None = None
    config_path: str | None = None


def _unusable_settings_message(snapshot: ExecutionSSHSettingsSnapshot) -> str:
    config_name = Path(snapshot.config_path).name if snapshot.config_path else "instance_config.yaml"
    detail = snapshot.unusable_error or "Unknown configuration error."
    return f"Broker SSH settings in {config_name} could not be loaded or applied: {detail}"


def _pinned_target_keys(
    ssh_settings: ExecutionSSHSettings,
) -> frozenset[PinnedSSHTargetKey]:
    return frozenset(
        (
            target.host,
            target.port,
            target.username,
            target.host_key_algorithm,
            target.host_key_fingerprint,
            target.credential_binding_id,
        )
        for target in ssh_settings.targets
    )


class ExecutionSSHSettingsSource:
    """Reload and validate ``execution_control.ssh`` (plus pinned key bindings) from disk.

    Unified rule for unusable file versions (missing file after it existed,
    empty/incomplete file without ``schema_version``, invalid YAML, schema
    validation error, duplicate pinned ``credential_binding_id`` with differing
    key paths, or key paths inside ``workspaces/``):

    - Remember the file stat fingerprint after a failed load so each broken
      version is parsed and logged once rather than on every tick or action.
    - Keep the last good settings for ``unusable_grace_seconds`` (default 3 s)
      so an in-place editor save does not drop live work.
    - If the file is still unusable once the grace period elapses, fail closed
      (``enabled=False`` with ``unusable_error`` set) until a valid version is
      saved.
    """

    def __init__(
        self,
        initial: ExecutionSSHSettings,
        *,
        config_path: Path | None,
        layout: InstanceLayout | None = None,
        initial_revision: str | None = None,
        unusable_grace_seconds: float = DEFAULT_UNUSABLE_SETTINGS_GRACE_SECONDS,
        clock: Callable[[], float] = time.monotonic,
        logger: logging.Logger | None = None,
    ) -> None:
        self._current = initial.model_copy(deep=True)
        self._layout = layout
        self._config_path = config_path.expanduser().resolve() if config_path is not None else None
        self._initial_revision = initial_revision
        self._checked_initial_revision = False
        self._unusable_grace_seconds = max(0.0, float(unusable_grace_seconds))
        self._clock = clock
        self._logger = logger or LOGGER
        # Never stamp the current disk fingerprint at construction: the file may have
        # changed between process config loading and constructing this source.
        self._last_checked_fp: Any = _UNCHECKED_FINGERPRINT
        self._existed = self._initial_revision is not None or self._stat_fingerprint() is not None
        self._unusable_since: float | None = None
        self._unusable_error: str | None = None
        self._failed_closed_logged = False
        self._current_materials: dict[str, SSHCredentialMaterial] = {}
        if self._layout is not None:
            try:
                self._current_materials = _build_configured_materials(self._layout, self._current)
            except Exception as exc:
                self._unusable_error = str(exc)
                self._unusable_since = self._clock() - self._unusable_grace_seconds
                self._logger.error(
                    "Initial execution_control.ssh settings cannot be applied; disabling broker SSH fail-closed. "
                    "event=broker_ssh_settings_reload_failed path=%s error=%s",
                    self._config_path,
                    exc,
                )

    def _stat_fingerprint(self) -> tuple[int, int, int] | None:
        if self._config_path is None:
            return None
        try:
            st = self._config_path.stat()
        except OSError:
            return None
        return (int(st.st_ino), int(st.st_size), int(st.st_mtime_ns))

    def _parse_and_validate(
        self,
        raw_bytes: bytes,
    ) -> tuple[ExecutionSSHSettings, dict[str, SSHCredentialMaterial]]:
        raw_text = raw_bytes.decode("utf-8")
        parsed = yaml.safe_load(raw_text)
        if not isinstance(parsed, Mapping):
            raise ValueError(
                "Instance configuration file is empty or incomplete (must be a YAML mapping with schema_version)."
            )
        if "schema_version" not in parsed or parsed["schema_version"] is None:
            raise ValueError("instance_config.yaml must declare schema_version.")
        execution_raw = parsed.get("execution_control")
        execution_enabled = EXECUTION_CONTROL_DEFAULTS.enabled.value
        if execution_raw is None:
            ssh_raw: Mapping[str, Any] = {}
        elif isinstance(execution_raw, Mapping):
            raw_exec_enabled = execution_raw.get("enabled", execution_enabled)
            if not isinstance(raw_exec_enabled, bool):
                raise ValueError("execution_control.enabled must be a boolean.")
            execution_enabled = raw_exec_enabled
            raw_ssh_section = execution_raw.get("ssh")
            if raw_ssh_section is None:
                ssh_raw = {}
            elif isinstance(raw_ssh_section, Mapping):
                ssh_raw = raw_ssh_section
            else:
                raise ValueError("execution_control.ssh must be a YAML mapping.")
        else:
            raise ValueError("execution_control must be a YAML mapping.")
        reloaded = ExecutionSSHSettings.model_validate(ssh_raw)
        if not execution_enabled and reloaded.enabled:
            reloaded = reloaded.model_copy(update={"enabled": False})
        materials = (
            _build_configured_materials(self._layout, reloaded)
            if self._layout is not None
            else {}
        )
        return reloaded, materials

    def _record_unusable(self, fp: tuple[int, int, int] | None, error: str) -> None:
        self._last_checked_fp = fp
        self._unusable_error = error
        if self._unusable_since is None:
            self._unusable_since = self._clock()
        config_name = self._config_path.name if self._config_path is not None else "instance_config.yaml"
        if self._failed_closed_logged or (self._clock() - self._unusable_since) >= self._unusable_grace_seconds:
            self._logger.error(
                "Could not load or apply execution_control.ssh from %s; broker SSH remains "
                "disabled fail-closed. event=broker_ssh_settings_reload_failed path=%s error=%s",
                config_name,
                self._config_path,
                error,
            )
        else:
            self._logger.error(
                "Could not load or apply execution_control.ssh from %s; keeping previous settings "
                "for up to %.1fs before failing closed. event=broker_ssh_settings_reload_failed "
                "path=%s error=%s",
                config_name,
                self._unusable_grace_seconds,
                self._config_path,
                error,
            )

    def _build_snapshot(self) -> ExecutionSSHSettingsSnapshot:
        path_str = str(self._config_path) if self._config_path is not None else None
        if self._unusable_since is not None:
            elapsed = self._clock() - self._unusable_since
            if elapsed < self._unusable_grace_seconds:
                return ExecutionSSHSettingsSnapshot(
                    settings=self._current,
                    materials=self._current_materials,
                    unusable_error=None,
                    config_path=path_str,
                )
            if not self._failed_closed_logged:
                self._failed_closed_logged = True
                self._logger.error(
                    "Disabling broker SSH fail-closed because execution_control.ssh remained unusable "
                    "past the %.1fs grace period. event=broker_ssh_settings_failed_closed path=%s error=%s",
                    self._unusable_grace_seconds,
                    self._config_path,
                    self._unusable_error,
                )
            return ExecutionSSHSettingsSnapshot(
                settings=ExecutionSSHSettings(enabled=False),
                materials={},
                unusable_error=self._unusable_error,
                config_path=path_str,
            )
        return ExecutionSSHSettingsSnapshot(
            settings=self._current,
            materials=self._current_materials,
            unusable_error=None,
            config_path=path_str,
        )

    def snapshot(self) -> ExecutionSSHSettingsSnapshot:
        if self._config_path is None:
            return self._build_snapshot()
        fp = self._stat_fingerprint()
        if fp != self._last_checked_fp:
            if fp is None:
                if self._existed:
                    self._record_unusable(None, "Instance configuration file is missing.")
                else:
                    self._last_checked_fp = None
                return self._build_snapshot()
            self._existed = True
            try:
                raw_bytes = self._config_path.read_bytes()
                if not self._checked_initial_revision and self._initial_revision is not None:
                    self._checked_initial_revision = True
                    # Matches config_file_updates.config_revision (lowercase SHA-256 hex)
                    # without importing custom_agents into the broker process.
                    if (
                        self._unusable_error is None
                        and hashlib.sha256(raw_bytes).hexdigest() == self._initial_revision
                    ):
                        self._last_checked_fp = fp
                        return self._build_snapshot()
                self._checked_initial_revision = True
                reloaded, materials = self._parse_and_validate(raw_bytes)
            except Exception as exc:
                self._checked_initial_revision = True
                self._record_unusable(fp, str(exc))
                return self._build_snapshot()
            was_failed_closed = self._failed_closed_logged
            self._current = reloaded
            self._current_materials = materials
            self._last_checked_fp = fp
            self._unusable_since = None
            self._unusable_error = None
            self._failed_closed_logged = False
            if was_failed_closed:
                self._logger.info(
                    "Reloaded valid execution_control.ssh settings after prior failure. "
                    "event=broker_ssh_settings_recovered path=%s enabled=%s",
                    self._config_path,
                    reloaded.enabled,
                )
        return self._build_snapshot()

    def current(self) -> ExecutionSSHSettings:
        return self.snapshot().settings


def classify_ssh_error(exc: Exception) -> dict[str, Any]:
    """Return structured error metadata (never raw secrets) for an SSH exception."""

    payload: dict[str, Any] = {"error": str(exc)}
    if isinstance(exc, SSHDisabledError):
        payload["error_code"] = "ssh_disabled"
        payload["next_step"] = SSH_DISABLED_NEXT_STEP
    elif isinstance(exc, SSHSettingsUnusableError):
        payload["error_code"] = "ssh_settings_unusable"
        payload["next_step"] = exc.next_step
    elif isinstance(exc, SSHRecoveryError):
        payload["error_code"] = "ssh_recovery_failed"
        payload["next_step"] = (
            "Broker SSH startup recovery could not complete; check execution storage health "
            "and broker logs, then retry."
        )
    elif isinstance(exc, InvalidSSHCredentialValueError):
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


def _build_configured_materials(
    layout: InstanceLayout,
    ssh_settings: ExecutionSSHSettings,
) -> dict[str, SSHCredentialMaterial]:
    materials: dict[str, SSHCredentialMaterial] = {}
    for target in ssh_settings.targets:
        identity = _broker_material_path(layout, target.identity_file)
        known_hosts = _broker_material_path(layout, target.known_hosts_file)
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
    return materials


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
        watch_path = (
            Path(config.config_path)
            if config.config_path is not None
            else self._layout.instance_config_path
        )
        self._settings_source = ExecutionSSHSettingsSource(
            config.ssh,
            config_path=watch_path,
            layout=self._layout,
            initial_revision=config.config_source_revision,
            logger=self._logger,
        )
        # Opened lazily only when SSH is enabled (or an admin revokes a record),
        # so starting the broker with SSH off never touches execution storage or
        # creates the execution artifacts directory.
        self._repository: ExecutionControlRepository | None = None
        self._artifact_store: FilesystemArtifactStore | None = None
        self._catalog_provider: CatalogSSHCredentialMaterialProvider | None = None
        self._broker: SSHSessionBroker | None = None
        self._applied_settings: ExecutionSSHSettings | None = None
        self._applied_materials: dict[str, SSHCredentialMaterial] | None = None
        self._recovered = False
        self._shutting_down = False
        self._recovery_lock = asyncio.Lock()
        self._settings_lock = asyncio.Lock()

    def _ensure_repository(self) -> ExecutionControlRepository:
        if self._repository is None:
            self._repository = create_execution_control_repository(self._config.storage_settings)
        return self._repository

    def _ensure_artifact_store(self) -> FilesystemArtifactStore:
        if self._artifact_store is None:
            self._artifact_store = FilesystemArtifactStore(self._layout.execution_artifacts_dir)
        return self._artifact_store

    def current_settings(self) -> ExecutionSSHSettings:
        return self._settings_source.current()

    @property
    def enabled(self) -> bool:
        if self._shutting_down:
            return False
        snapshot = self._settings_source.snapshot()
        return snapshot.unusable_error is None and bool(snapshot.settings.enabled)

    def _ensure_broker_sync(self, snapshot: ExecutionSSHSettingsSnapshot) -> SSHSessionBroker:
        if self._broker is None:
            self._broker = self._build_broker(snapshot)
            self._applied_settings = snapshot.settings.model_copy(deep=True)
            self._applied_materials = dict(snapshot.materials)
            return self._broker
        self._apply_settings_sync(snapshot)
        return self._broker

    def _apply_settings_sync(
        self,
        snapshot: ExecutionSSHSettingsSnapshot,
    ) -> tuple[bool, bool]:
        ssh_settings = snapshot.settings
        materials = dict(snapshot.materials)
        if (
            self._broker is None
            or (
                self._applied_settings == ssh_settings
                and self._applied_materials == materials
            )
        ):
            return False, False
        new_policy = SSHNetworkPolicy.from_settings(ssh_settings)
        old_policy = (
            SSHNetworkPolicy.from_settings(self._applied_settings)
            if self._applied_settings is not None
            else self._broker.network_policy
        )
        network_changed = old_policy != new_policy
        pinned_changed = (
            self._applied_settings is None
            or self._applied_settings.targets != ssh_settings.targets
            or self._applied_materials != materials
        )
        if network_changed:
            self._broker.network_policy = new_policy
        self._broker.allow_password_bootstrap = ssh_settings.allow_password_bootstrap
        self._broker.ssh_executable = ssh_settings.ssh_executable
        self._broker.lease_ttl_seconds = ssh_settings.lease_ttl_seconds
        self._broker.transcript_limit_bytes = ssh_settings.transcript_limit_bytes
        if pinned_changed:
            self._broker.material_provider = CompositeSSHCredentialMaterialProvider(
                configured_provider=ConfiguredSSHCredentialMaterialProvider(materials),
                catalog_provider=self._catalog_provider,
            )
            self._broker.allowed_pinned_targets = _pinned_target_keys(ssh_settings)
        self._applied_settings = ssh_settings.model_copy(deep=True)
        self._applied_materials = materials
        return network_changed, pinned_changed

    async def _apply_settings_and_enforce(self) -> SSHSessionBroker:
        """Reload settings under ``_settings_lock``, fail closed if needed, and enforce live changes."""

        async with self._settings_lock:
            while True:
                if self._shutting_down:
                    raise SSHSessionUnavailable("SSH session broker is shutting down.")
                snapshot = self._settings_source.snapshot()
                if snapshot.unusable_error is not None:
                    reason = _unusable_settings_message(snapshot)
                    broker = self._broker
                    if broker is not None and broker._has_monitored_work():
                        await broker._fail_closed_all_monitored_work(reason)
                    raise SSHSettingsUnusableError(reason, config_path=snapshot.config_path)
                if not snapshot.settings.enabled:
                    broker = self._broker
                    if broker is not None and broker._has_monitored_work():
                        await broker._fail_closed_all_monitored_work(SSH_DISABLED_MESSAGE)
                    raise SSHDisabledError()
                if self._broker is None:
                    return self._ensure_broker_sync(snapshot)
                network_changed, pinned_changed = self._apply_settings_sync(snapshot)
                broker = self._broker
                if broker._shutting_down:
                    raise SSHSessionUnavailable("SSH session broker is shutting down.")
                need_network = network_changed or broker.network_policy != broker._checked_network_policy
                need_pinned = (
                    pinned_changed
                    or broker.material_provider is not broker._checked_material_provider
                    or broker.allowed_pinned_targets != broker._checked_allowed_pinned_targets
                )
                if not need_network and not need_pinned:
                    return broker
                await broker.enforce_settings_on_live_work(
                    check_network=need_network,
                    check_pinned_targets=need_pinned,
                )
                if self._shutting_down or self._broker is not broker or broker._shutting_down:
                    raise SSHSessionUnavailable("SSH session broker is shutting down.")

    def _sync_broker_settings(self) -> SSHSettingsSyncOutcome:
        """Reload operator settings and sync broker attributes without awaiting teardowns."""

        if self._shutting_down:
            return SSHSettingsSyncOutcome(
                enabled=False,
                stop_reason="SSH session broker is shutting down.",
                stop_event="ssh_broker_shutting_down",
            )
        snapshot = self._settings_source.snapshot()
        if snapshot.unusable_error is not None:
            return SSHSettingsSyncOutcome(
                enabled=False,
                stop_reason=_unusable_settings_message(snapshot),
                stop_event="ssh_broker_settings_unusable_fail_closed",
                unusable=True,
                config_path=snapshot.config_path,
            )
        if not snapshot.settings.enabled:
            return SSHSettingsSyncOutcome(
                enabled=False,
                stop_reason=SSH_DISABLED_MESSAGE,
                stop_event="ssh_broker_disabled_killswitch",
            )
        self._apply_settings_sync(snapshot)
        return SSHSettingsSyncOutcome(enabled=True)

    @property
    def broker(self) -> SSHSessionBroker:
        if self._shutting_down:
            raise SSHSessionUnavailable("SSH session broker is shutting down.")
        snapshot = self._settings_source.snapshot()
        if snapshot.unusable_error is not None:
            raise SSHSettingsUnusableError(
                _unusable_settings_message(snapshot),
                config_path=snapshot.config_path,
            )
        if not snapshot.settings.enabled:
            raise SSHDisabledError()
        return self._ensure_broker_sync(snapshot)

    def _build_broker(self, snapshot: ExecutionSSHSettingsSnapshot) -> SSHSessionBroker:
        ssh_settings = snapshot.settings
        configured_provider = ConfiguredSSHCredentialMaterialProvider(dict(snapshot.materials))
        catalog_provider = CatalogSSHCredentialMaterialProvider(
            ephemeral_base_dir=self._layout.ssh_ephemeral_dir,
            secret_resolver=make_catalog_secret_resolver(),
            logger=self._logger,
        )
        self._catalog_provider = catalog_provider
        material_provider = CompositeSSHCredentialMaterialProvider(
            configured_provider=configured_provider,
            catalog_provider=catalog_provider,
        )
        auth_enabled = self._access_gate.access is not None
        guard = ExecutionAccessGuard(
            authorization_enabled=auth_enabled,
            access=self._access_gate.access if auth_enabled else None,
            catalogs=self._access_gate.catalogs if auth_enabled else None,
        )
        return SSHSessionBroker(
            repository=self._ensure_repository(),
            authorization_guard=guard,
            material_provider=material_provider,
            artifact_store=self._ensure_artifact_store(),
            team_id=self._access_gate.team_id,
            broker_id=f"{self._config.instance_name}.secure-broker-ssh.{os.getpid()}",
            ssh_executable=ssh_settings.ssh_executable,
            lease_ttl_seconds=ssh_settings.lease_ttl_seconds,
            transcript_limit_bytes=ssh_settings.transcript_limit_bytes,
            network_policy=SSHNetworkPolicy.from_settings(ssh_settings),
            allowed_pinned_targets=_pinned_target_keys(ssh_settings),
            allow_password_bootstrap=ssh_settings.allow_password_bootstrap,
            settings_sync=self._sync_broker_settings,
            logger=self._logger,
        )

    async def _ensure_recovered(self, broker: SSHSessionBroker) -> None:
        if self._recovered:
            return
        async with self._recovery_lock:
            if self._recovered:
                return
            if self._shutting_down or self._broker is not broker or broker._shutting_down:
                raise SSHSessionUnavailable("SSH session broker is shutting down.")
            try:
                lost = await asyncio.to_thread(broker.recover_after_restart)
            except Exception as exc:
                self._logger.exception(
                    "Broker SSH startup recovery failed. event=broker_ssh_recovery_failed team_id=%s",
                    self._access_gate.team_id,
                )
                raise SSHRecoveryError(
                    f"Broker SSH startup recovery failed: {exc}"
                ) from exc
            if self._shutting_down or self._broker is not broker or broker._shutting_down:
                raise SSHSessionUnavailable("SSH session broker is shutting down.")
            self._recovered = True
            self._logger.info(
                "Initialized broker-owned SSH runtime. event=broker_ssh_runtime_started "
                "team_id=%s recovered_lost_sessions=%d",
                self._access_gate.team_id,
                len(lost),
            )

    async def _active_broker(self) -> SSHSessionBroker:
        broker = await self._apply_settings_and_enforce()
        await self._ensure_recovered(broker)
        if self._shutting_down or self._broker is not broker or broker._shutting_down:
            raise SSHSessionUnavailable("SSH session broker is shutting down.")
        return broker

    async def startup(self) -> None:
        """Recover crashed sessions and sweep orphaned ephemeral SSH dirs on broker start."""

        async with self._settings_lock:
            if self._shutting_down:
                return
            snapshot = self._settings_source.snapshot()
            if snapshot.unusable_error is not None:
                self._logger.error(
                    "Skipping broker SSH startup because settings cannot be used. "
                    "event=broker_ssh_startup_settings_unusable error=%s",
                    snapshot.unusable_error,
                )
                return
            if not snapshot.settings.enabled:
                return
            broker = self._ensure_broker_sync(snapshot)
        try:
            await self._ensure_recovered(broker)
        except SSHSessionUnavailable:
            if self._shutting_down:
                return
            raise

    async def shutdown(self) -> None:
        """Terminate running SSH commands, close live PTYs, and wipe ephemeral dirs."""

        self._shutting_down = True
        if self._broker is None:
            return
        broker = self._broker
        self._broker = None
        self._catalog_provider = None
        self._applied_settings = None
        self._applied_materials = None
        self._recovered = False
        await broker.shutdown()

    def _require_owned_session(self, session_id: str, person_id: str) -> InteractiveSessionV1:
        session = self._ensure_repository().get_interactive_session(
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
        broker = await self._active_broker()

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
            encoded = text.encode("utf-8")
            if len(encoded) > MAX_PTY_WRITE_BYTES:
                raise ValueError(
                    f"SSH terminal write exceeds the maximum size ({MAX_PTY_WRITE_BYTES} bytes); "
                    "use upload_execution_ssh_bundle for larger content."
                )
            write_timeout = max(0.1, min(float(params.get("timeout_seconds", 10.0)), 30.0))
            written = await broker.write(
                session_id=session_id,
                payload=encoded,
                caller=caller,
                timeout_seconds=write_timeout,
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
        if self._broker is not None:
            was_live = session_id in self._broker._sessions
            closed = await self._broker.revoke_session(session_id=session_id, revoked_by=revoked_by)
            return {
                "revoked": closed is not None or was_live,
                "session_id": session_id,
                "terminated_live": was_live,
                "session": closed.model_dump(mode="json") if closed is not None else None,
            }
        repo = self._ensure_repository()
        revocation = repo.revoke_interactive_session(
            team_id=self._access_gate.team_id,
            session_id=session_id,
            revoked_by=revoked_by,
        )
        session = (
            repo.get_interactive_session(team_id=self._access_gate.team_id, session_id=session_id)
            if revocation is not None
            else None
        )
        return {
            "revoked": revocation is not None,
            "session_id": session_id,
            "terminated_live": False,
            "session": session.model_dump(mode="json") if session is not None else None,
        }

    async def revoke_target(self, *, target_id: str, revoked_by: str) -> dict[str, Any]:
        if self._broker is not None:
            revoked = await self._broker.revoke_target(target_id=target_id, revoked_by=revoked_by)
            return {
                "revoked": revoked is not None,
                "target_id": target_id,
                "target": revoked.model_dump(mode="json") if revoked is not None else None,
            }
        revoked = self._ensure_repository().revoke_ssh_target(
            team_id=self._access_gate.team_id,
            target_id=target_id,
            revoked_by=revoked_by,
        )
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
    "DEFAULT_UNUSABLE_SETTINGS_GRACE_SECONDS",
    "EXECUTION_SSH_MAX_REQUEST_BYTES",
    "EXECUTION_SSH_PROVIDER",
    "ExecutionSSHSettingsSnapshot",
    "ExecutionSSHSettingsSource",
    "SSHRecoveryError",
    "build_execution_ssh_runtime",
    "classify_ssh_error",
    "execute_ssh_broker_action",
    "register_execution_ssh_app",
]
