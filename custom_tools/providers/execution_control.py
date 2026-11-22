"""Immutable deterministic execution and brokered SSH tools.

Jobs and SSH sessions are ``write`` operations decided by the access decision
hook; the activations they create record who held access, and the runtime
decides again before each run and SSH use. Managed services, SSH target
enrollment, and bounded SSH delegations are exact changes an administrator
approves: the ``plan_*`` tools stage them and ``apply_change`` applies them
(``custom_agents.execution_changes``). Operational SSH tools live in
``custom_tools.providers._execution_ssh`` so this provider stays well under the
statement budget while allowing delegated worker types to use approved sessions
under real Cedar and ``ExecutionAccessGuard``.
"""

from __future__ import annotations

from datetime import datetime, timezone
import json
from typing import Any

from strands import tool

from custom_agents.execution_changes import ManagedServiceChangeService
from custom_tools.provider_contract import ToolProvider
from custom_tools.providers._execution_ssh import (
    SSH_TOOLS,
    _SSH_BROKERS,
    _error_payload,
    _json_strings,
    _manager_agent,
    _manager_service,
    _ssh_broker,
    _ssh_caller,
    _utc,
    close_execution_ssh_session,
    create_execution_ssh_session,
    enroll_execution_ssh_key,
    get_execution_ssh_command,
    list_execution_ssh_sessions,
    list_execution_ssh_targets,
    open_execution_ssh_session,
    plan_execution_ssh_interaction,
    plan_execution_ssh_target,
    probe_execution_ssh_host_key,
    read_execution_ssh_session,
    resize_execution_ssh_session,
    run_execution_ssh_command,
    run_execution_ssh_sudo,
    upload_execution_ssh_bundle,
    write_execution_ssh_session,
)
from runtime_hosting.execution_contracts import (
    AutomationScheduleKind,
    AutomationScheduleV1,
    MissedRunPolicy,
    OverlapPolicy,
    ServiceDesiredState,
    ServiceKind,
    ServiceRestartPolicy,
)


def _json_object(value: str, *, field_name: str) -> dict[str, str]:
    if not str(value or "").strip():
        return {}
    parsed = json.loads(value)
    if not isinstance(parsed, dict):
        raise ValueError(f"{field_name} must be a JSON object.")
    return {str(key): str(item) for key, item in parsed.items()}


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


@tool
def list_execution_profiles() -> str:
    """List operator-installed deterministic build and runtime profiles."""
    try:
        service, error = _manager_service("list_execution_profiles")
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
        service, error = _manager_service("promote_execution_revision")
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
        service, error = _manager_service("create_execution_job")
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
        service, error = _manager_service("set_execution_job_enabled")
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
        service, error = _manager_service("list_execution_jobs")
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
        service, error = _manager_service("list_execution_runs")
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
        service, error = _manager_service("list_managed_execution_services")
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
        *SSH_TOOLS,
    ),
)


__all__ = [
    "PROVIDER",
    "_SSH_BROKERS",
    "_ssh_broker",
    "_ssh_caller",
    "close_execution_ssh_session",
    "create_execution_job",
    "create_execution_ssh_session",
    "enroll_execution_ssh_key",
    "get_execution_ssh_command",
    "list_execution_jobs",
    "list_execution_profiles",
    "list_execution_runs",
    "list_execution_ssh_sessions",
    "list_execution_ssh_targets",
    "list_managed_execution_services",
    "open_execution_ssh_session",
    "plan_execution_ssh_interaction",
    "plan_execution_ssh_target",
    "plan_managed_execution_service",
    "plan_managed_execution_service_state",
    "probe_execution_ssh_host_key",
    "promote_execution_revision",
    "read_execution_ssh_session",
    "resize_execution_ssh_session",
    "run_execution_ssh_command",
    "run_execution_ssh_sudo",
    "set_execution_job_enabled",
    "upload_execution_ssh_bundle",
    "write_execution_ssh_session",
]
