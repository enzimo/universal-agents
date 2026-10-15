"""Immutable deterministic execution tools for TeamArchitect.

Jobs and SSH sessions are ``write`` operations decided by the access decision
hook; the activations they create record who held access, and the runtime
decides again before each run and SSH use. Managed services and typing into an
SSH session are exact changes a person approves: the ``plan_*`` tools stage
them and ``apply_change`` applies them (``custom_agents.execution_changes``).
"""

from __future__ import annotations

from datetime import datetime, timezone
import base64
import json
from pathlib import Path
from typing import Any

from strands import tool

from custom_agents.execution_changes import ManagedServiceChangeService, SSHInteractionChangeService
from custom_agents.execution_control_service import ExecutionControlService
from custom_messages.contracts.runtime_v1 import TEAM_ARCHITECT_AGENT_TYPE
from custom_tools.provider_contract import ToolProvider
from custom_tools.providers._runtime import _require_manager_tool
from custom_tools.runtime_context import get_agent_ref
from id_generation import new_id
from runtime_hosting.ssh import (
    ConfiguredSSHCredentialMaterialProvider,
    SSHCredentialMaterial,
    SSHSessionBroker,
)
from runtime_hosting.execution_contracts import (
    AutomationScheduleKind,
    AutomationScheduleV1,
    MissedRunPolicy,
    OverlapPolicy,
    ServiceKind,
    ServiceDesiredState,
    ServiceRestartPolicy,
)


_SSH_BROKERS: dict[str, SSHSessionBroker] = {}


def _error_payload(exc: Exception) -> str:
    payload: dict[str, Any] = {"error": str(exc)}
    if isinstance(exc, PermissionError):
        payload["error_code"] = "execution_access_refused"
    return json.dumps(payload)


def _manager_agent(tool_name: str) -> tuple[Any, str | None]:
    agent = get_agent_ref()
    if agent is None:
        return None, "Agent reference not set"
    manager_error = _require_manager_tool(agent, tool_name)
    if manager_error:
        return None, f"{manager_error} Escalate to {TEAM_ARCHITECT_AGENT_TYPE}."
    return agent, None


def _service(tool_name: str) -> tuple[ExecutionControlService | None, str | None]:
    agent, error = _manager_agent(tool_name)
    if error:
        return None, error
    return ExecutionControlService(agent), None


def _json_object(value: str, *, field_name: str) -> dict[str, str]:
    if not str(value or "").strip():
        return {}
    parsed = json.loads(value)
    if not isinstance(parsed, dict):
        raise ValueError(f"{field_name} must be a JSON object.")
    return {str(key): str(item) for key, item in parsed.items()}


def _json_strings(value: str, *, field_name: str) -> tuple[str, ...]:
    if not str(value or "").strip():
        return ()
    parsed = json.loads(value)
    if not isinstance(parsed, list) or not all(isinstance(item, str) for item in parsed):
        raise ValueError(f"{field_name} must be a JSON list of strings.")
    return tuple(parsed)


def _utc(value: str, *, field_name: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(str(value).strip().replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError(f"{field_name} must be an ISO 8601 timestamp.") from exc
    if parsed.tzinfo is None:
        raise ValueError(f"{field_name} must include a timezone.")
    return parsed.astimezone(timezone.utc)


def _schedule(
    *,
    schedule_kind: str,
    interval_seconds: int,
    cron_expression: str,
    one_shot_at_utc: str,
    timezone_name: str,
    missed_run_policy: str,
    overlap_policy: str,
    maximum_backlog: int,
    start_deadline_seconds: int,
) -> AutomationScheduleV1:
    kind = AutomationScheduleKind(schedule_kind)
    return AutomationScheduleV1(
        schedule_kind=kind,
        interval_seconds=interval_seconds if kind == AutomationScheduleKind.INTERVAL else None,
        cron_expression=cron_expression or None if kind == AutomationScheduleKind.CRON else None,
        one_shot_at_utc=(
            _utc(one_shot_at_utc, field_name="one_shot_at_utc")
            if kind == AutomationScheduleKind.ONE_SHOT
            else None
        ),
        timezone=timezone_name,
        missed_run_policy=MissedRunPolicy(missed_run_policy),
        overlap_policy=OverlapPolicy(overlap_policy),
        maximum_backlog=maximum_backlog,
        start_deadline_seconds=start_deadline_seconds or None,
    )


def _job_payload(job: Any) -> dict[str, Any]:
    return {
        **job.model_dump(mode="json"),
        "activation_resource_digest": job.activation_resource_digest(),
    }


def _ssh_broker(service: ExecutionControlService) -> SSHSessionBroker:
    agent_id = str(service.agent.id)
    existing = _SSH_BROKERS.get(agent_id)
    if existing is not None:
        return existing
    ssh_settings = service.config.execution_control.ssh
    if not ssh_settings.enabled:
        raise RuntimeError("execution_control.ssh.enabled is false.")
    materials: dict[str, SSHCredentialMaterial] = {}
    for target in ssh_settings.targets:
        identity = _broker_material_path(service, target.identity_file)
        known_hosts = _broker_material_path(service, target.known_hosts_file)
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
    broker = SSHSessionBroker(
        repository=service.repository,
        # The same checks as the execution controller, with this agent's access service.
        authorization_guard=service.access_guard(),
        material_provider=ConfiguredSSHCredentialMaterialProvider(materials),
        artifact_store=service.artifact_store,
        team_id=service.owner.team_id,
        broker_id=f"{service.config.instance_name}.agent-ssh-broker.{agent_id}",
        ssh_executable=ssh_settings.ssh_executable,
        lease_ttl_seconds=ssh_settings.lease_ttl_seconds,
        transcript_limit_bytes=ssh_settings.transcript_limit_bytes,
    )
    broker.recover_after_restart()
    _SSH_BROKERS[agent_id] = broker
    return broker


def _broker_material_path(service: ExecutionControlService, value: str) -> Path:
    path = Path(value).expanduser()
    if not path.is_absolute():
        path = service.config.instance_root_path / path
    resolved = path.resolve()
    if resolved.is_relative_to(service.config.workspace_path):
        raise PermissionError("SSH key material must stay outside the agent workspace.")
    return resolved


@tool
def list_execution_profiles() -> str:
    """List operator-installed deterministic build and runtime profiles."""
    try:
        service, error = _service("list_execution_profiles")
        if error:
            return json.dumps({"error": error})
        assert service is not None
        profiles = service.list_profiles()
        return json.dumps({"returned": len(profiles), "profiles": profiles})
    except Exception as exc:
        return _error_payload(exc)


@tool
def promote_execution_revision(
    source: str,
    source_name: str,
    language: str,
    profile_id: str,
    args_json: str = "",
    non_secret_env_json: str = "",
    broker_capability_binding_ids_json: str = "",
) -> str:
    """Compile source under an exact profile and persist an immutable runnable revision."""
    try:
        service, error = _service("promote_execution_revision")
        if error:
            return json.dumps({"error": error})
        assert service is not None
        revision = service.promote_revision(
            source=source,
            source_name=source_name,
            language=language,
            profile_id=profile_id,
            args=_json_strings(args_json, field_name="args_json"),
            non_secret_env=_json_object(
                non_secret_env_json, field_name="non_secret_env_json"
            ),
            broker_capability_binding_ids=_json_strings(
                broker_capability_binding_ids_json,
                field_name="broker_capability_binding_ids_json",
            ),
        )
        return revision.model_dump_json()
    except Exception as exc:
        return _error_payload(exc)


@tool
def create_execution_job(
    name: str,
    revision_id: str,
    schedule_kind: str = "one_shot",
    interval_seconds: int = 0,
    cron_expression: str = "",
    one_shot_at_utc: str = "",
    timezone_name: str = "UTC",
    missed_run_policy: str = "coalesce",
    overlap_policy: str = "skip_if_running",
    maximum_backlog: int = 1,
    start_deadline_seconds: int = 0,
    description: str = "",
    activate: bool = False,
    confirmed: bool = False,
) -> str:
    """Create a finite or periodic immutable job, optionally activating its exact digest.

    Activating records that you and the person hold write access here; the job
    runs later, so that access must last beyond this task. With authorization
    off, get the person's agreement first and pass confirmed=true.
    """
    try:
        service, error = _service("create_execution_job")
        if error:
            return json.dumps({"error": error})
        assert service is not None
        if schedule_kind == AutomationScheduleKind.ONE_SHOT.value and not one_shot_at_utc:
            one_shot_at_utc = datetime.now(timezone.utc).isoformat()
        job = service.create_job(
            name=name,
            revision_id=revision_id,
            schedule=_schedule(
                schedule_kind=schedule_kind,
                interval_seconds=interval_seconds,
                cron_expression=cron_expression,
                one_shot_at_utc=one_shot_at_utc,
                timezone_name=timezone_name,
                missed_run_policy=missed_run_policy,
                overlap_policy=overlap_policy,
                maximum_backlog=maximum_backlog,
                start_deadline_seconds=start_deadline_seconds,
            ),
            description=description or None,
            activate=activate,
            agreement_confirmed=confirmed,
        )
        return json.dumps(_job_payload(job))
    except Exception as exc:
        return _error_payload(exc)


@tool
def set_execution_job_enabled(
    job_id: str,
    enabled: bool,
    confirmed: bool = False,
) -> str:
    """Enable or disable a job by advancing its generation and recording who enabled it.

    With authorization off, get the person's agreement first and pass confirmed=true.
    """
    try:
        service, error = _service("set_execution_job_enabled")
        if error:
            return json.dumps({"error": error})
        assert service is not None
        job = service.set_job_enabled(
            job_id=job_id,
            enabled=enabled,
            agreement_confirmed=confirmed,
        )
        return json.dumps(_job_payload(job))
    except Exception as exc:
        return _error_payload(exc)


@tool
def list_execution_jobs(limit: int = 100) -> str:
    """List owner-scoped immutable jobs and their exact activation targets."""
    try:
        service, error = _service("list_execution_jobs")
        if error:
            return json.dumps({"error": error})
        assert service is not None
        jobs = service.list_jobs(limit=max(1, min(int(limit), 1000)))
        return json.dumps({"returned": len(jobs), "jobs": [_job_payload(job) for job in jobs]})
    except Exception as exc:
        return _error_payload(exc)


@tool
def list_execution_runs(job_id: str = "", limit: int = 100) -> str:
    """List fenced attempt status and output artifact references for an owner-scoped job."""
    try:
        service, error = _service("list_execution_runs")
        if error:
            return json.dumps({"error": error})
        assert service is not None
        runs = service.list_runs(
            job_id=job_id or None,
            limit=max(1, min(int(limit), 1000)),
        )
        return json.dumps(
            {"returned": len(runs), "runs": [run.model_dump(mode="json") for run in runs]}
        )
    except Exception as exc:
        return _error_payload(exc)


@tool
def plan_managed_execution_service(
    name: str,
    revision_id: str,
    reason: str,
    service_kind: str = "process",
    restart_policy: str = "always",
    port: int = 0,
    mcp_path: str = "/mcp",
    mcp_tool_names_json: str = "",
    mcp_target_agent_types_json: str = "",
) -> str:
    """Stage creating and starting a Kubernetes-managed process or MCP server.

    Returns a change_id: show the effect to the person, then call apply_change
    with it. With authorization on an administrator approves the exact service
    in the Inbox; with it off, the person's agreement applies it.
    """
    try:
        agent, error = _manager_agent("plan_managed_execution_service")
        if error:
            return json.dumps({"error": error})
        staged = ManagedServiceChangeService(agent).stage_create(
            name=name,
            revision_id=revision_id,
            service_kind=ServiceKind(service_kind),
            restart_policy=ServiceRestartPolicy(restart_policy),
            reason=reason,
            port=port or None,
            mcp_path=mcp_path,
            mcp_tool_names=_json_strings(mcp_tool_names_json, field_name="mcp_tool_names_json"),
            mcp_target_agent_types=_json_strings(
                mcp_target_agent_types_json,
                field_name="mcp_target_agent_types_json",
            ),
        )
        return json.dumps(staged)
    except Exception as exc:
        return _error_payload(exc)


@tool
def list_managed_execution_services() -> str:
    """List owner-scoped managed process and MCP service state."""
    try:
        service, error = _service("list_managed_execution_services")
        if error:
            return json.dumps({"error": error})
        assert service is not None
        services = service.list_services()
        return json.dumps(
            {
                "returned": len(services),
                "services": [item.model_dump(mode="json") for item in services],
            }
        )
    except Exception as exc:
        return _error_payload(exc)


@tool
def plan_managed_execution_service_state(
    service_id: str,
    desired_state: str,
    reason: str,
) -> str:
    """Stage starting, stopping, or removing a managed service (running, stopped, removed).

    Returns a change_id to apply with apply_change after a person approves it.
    """
    try:
        agent, error = _manager_agent("plan_managed_execution_service_state")
        if error:
            return json.dumps({"error": error})
        staged = ManagedServiceChangeService(agent).stage_state(
            service_id=service_id,
            desired_state=ServiceDesiredState(desired_state),
            reason=reason,
        )
        return json.dumps(staged)
    except Exception as exc:
        return _error_payload(exc)


@tool
def list_execution_ssh_targets() -> str:
    """List installed SSH targets without exposing key-file paths or key material."""
    try:
        service, error = _service("list_execution_ssh_targets")
        if error:
            return json.dumps({"error": error})
        assert service is not None
        targets = service.list_ssh_targets()
        return json.dumps({"returned": len(targets), "targets": targets})
    except Exception as exc:
        return _error_payload(exc)


@tool
def create_execution_ssh_session(
    target_id: str,
    terminal_rows: int = 24,
    terminal_columns: int = 80,
    confirmed: bool = False,
) -> str:
    """Create a session for one installed SSH target that you can open, read and resize.

    Typing needs a person's approval for the session: plan_execution_ssh_interaction,
    then apply_change. With authorization off, get the person's agreement first and
    pass confirmed=true.
    """
    try:
        service, error = _service("create_execution_ssh_session")
        if error:
            return json.dumps({"error": error})
        assert service is not None
        session = service.create_ssh_session(
            target_id=target_id,
            terminal_rows=terminal_rows,
            terminal_columns=terminal_columns,
            agreement_confirmed=confirmed,
        )
        return session.model_dump_json()
    except Exception as exc:
        return _error_payload(exc)


@tool
def plan_execution_ssh_interaction(session_id: str, reason: str) -> str:
    """Stage permission to type into one SSH session; apply it with apply_change.

    Whatever you type runs on that host, so a person approves each session.
    """
    try:
        agent, error = _manager_agent("plan_execution_ssh_interaction")
        if error:
            return json.dumps({"error": error})
        staged = SSHInteractionChangeService(agent).stage(session_id=session_id, reason=reason)
        return json.dumps(staged)
    except Exception as exc:
        return _error_payload(exc)


@tool
async def open_execution_ssh_session(session_id: str) -> str:
    """Open a created SSH session's PTY using broker-owned identity material."""
    try:
        service, error = _service("open_execution_ssh_session")
        if error:
            return json.dumps({"error": error})
        assert service is not None
        service.owned_session(session_id)  # only the person's own sessions
        opened = await _ssh_broker(service).open(
            session_id=session_id,
            lease_token=new_id("ssh_lease"),
        )
        return opened.model_dump_json()
    except Exception as exc:
        return _error_payload(exc)


@tool
async def read_execution_ssh_session(
    session_id: str,
    maximum_bytes: int = 65536,
    timeout_seconds: float = 1.0,
) -> str:
    """Read bounded output from a live SSH PTY and renew its fenced lease."""
    try:
        service, error = _service("read_execution_ssh_session")
        if error:
            return json.dumps({"error": error})
        assert service is not None
        service.owned_session(session_id)  # only the person's own sessions
        payload = await _ssh_broker(service).read(
            session_id=session_id,
            maximum_bytes=max(1, min(int(maximum_bytes), 1_048_576)),
            timeout_seconds=max(0.0, min(float(timeout_seconds), 30.0)),
        )
        return json.dumps(
            {
                "session_id": session_id,
                "bytes": len(payload),
                "text": payload.decode("utf-8", errors="replace"),
                "base64": base64.b64encode(payload).decode("ascii"),
            }
        )
    except Exception as exc:
        return _error_payload(exc)


@tool
async def write_execution_ssh_session(session_id: str, text: str) -> str:
    """Type UTF-8 input into a live SSH PTY; needs a person's approval for this session."""
    try:
        service, error = _service("write_execution_ssh_session")
        if error:
            return json.dumps({"error": error})
        assert service is not None
        service.owned_session(session_id)  # only the person's own sessions
        written = await _ssh_broker(service).write(
            session_id=session_id,
            payload=text.encode("utf-8"),
        )
        return json.dumps({"session_id": session_id, "written_bytes": written})
    except Exception as exc:
        return _error_payload(exc)


@tool
async def resize_execution_ssh_session(
    session_id: str,
    terminal_rows: int,
    terminal_columns: int,
) -> str:
    """Resize a live SSH PTY and renew its fenced lease."""
    try:
        service, error = _service("resize_execution_ssh_session")
        if error:
            return json.dumps({"error": error})
        assert service is not None
        service.owned_session(session_id)  # only the person's own sessions
        await _ssh_broker(service).resize(
            session_id=session_id,
            rows=terminal_rows,
            columns=terminal_columns,
        )
        return json.dumps(
            {
                "session_id": session_id,
                "terminal_rows": terminal_rows,
                "terminal_columns": terminal_columns,
            }
        )
    except Exception as exc:
        return _error_payload(exc)


@tool
async def close_execution_ssh_session(session_id: str) -> str:
    """Close a live SSH PTY and persist its bounded transcript artifact."""
    try:
        service, error = _service("close_execution_ssh_session")
        if error:
            return json.dumps({"error": error})
        assert service is not None
        service.owned_session(session_id)  # only the person's own sessions
        closed = await _ssh_broker(service).close(session_id=session_id)
        return closed.model_dump_json()
    except Exception as exc:
        return _error_payload(exc)


@tool
def list_execution_ssh_sessions() -> str:
    """List owner-scoped SSH sessions, whether typing is approved, and transcript references."""
    try:
        service, error = _service("list_execution_ssh_sessions")
        if error:
            return json.dumps({"error": error})
        assert service is not None
        sessions = service.list_ssh_sessions()
        return json.dumps(
            {
                "returned": len(sessions),
                "sessions": [
                    {
                        **item.model_dump(mode="json"),
                        "typing_approved": service.ssh_interaction(item.session_id) is not None,
                    }
                    for item in sessions
                ],
            }
        )
    except Exception as exc:
        return _error_payload(exc)


PROVIDER = ToolProvider(
    name="execution_control",
    tools=(
        list_execution_profiles,
        promote_execution_revision,
        create_execution_job,
        set_execution_job_enabled,
        list_execution_jobs,
        list_execution_runs,
        plan_managed_execution_service,
        list_managed_execution_services,
        plan_managed_execution_service_state,
        list_execution_ssh_targets,
        create_execution_ssh_session,
        open_execution_ssh_session,
        read_execution_ssh_session,
        plan_execution_ssh_interaction,
        write_execution_ssh_session,
        resize_execution_ssh_session,
        close_execution_ssh_session,
        list_execution_ssh_sessions,
    ),
)


__all__ = [
    "PROVIDER",
    "create_execution_job",
    "list_execution_jobs",
    "list_execution_profiles",
    "list_execution_runs",
    "list_managed_execution_services",
    "plan_execution_ssh_interaction",
    "plan_managed_execution_service",
    "plan_managed_execution_service_state",
    "promote_execution_revision",
    "set_execution_job_enabled",
    "close_execution_ssh_session",
    "create_execution_ssh_session",
    "list_execution_ssh_sessions",
    "list_execution_ssh_targets",
    "open_execution_ssh_session",
    "read_execution_ssh_session",
    "resize_execution_ssh_session",
    "write_execution_ssh_session",
]
