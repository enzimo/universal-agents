"""Structured schedule automation and access preparation tools."""

import json
from typing import Any

from pydantic import ValidationError
from strands import tool

from custom_messages.contracts.schedule_worker_v1 import (
    ScheduleCapabilityRequirementsV1,
    ScheduleWorkerPolicySpecV1,
)
from custom_tools.runtime_context import get_agent_ref
from custom_tools.tool_results import failed

# The one requirements_json shape the scheduled-worker tools accept.
REQUIREMENTS_SHAPE = (
    '{"requirements":[{"capability_id":"<tool catalog id, e.g. cli:google>",'
    '"operation":"<one of its operations, e.g. gmail_search>","necessity":"required"}],'
    '"runtime_discovery":true}'
)


def parse_requirements(requirements_json: str) -> ScheduleCapabilityRequirementsV1 | None:
    """The requirements a tool call gives, or None when it gives none (keep the saved ones).

    A wrong shape is an error that names the expected shape, never an empty set.
    """

    if not requirements_json:
        return None
    try:
        return ScheduleCapabilityRequirementsV1.model_validate_json(requirements_json)
    except ValidationError as exc:
        problems = "; ".join(
            f"{'.'.join(str(part) for part in error['loc']) or 'requirements_json'}: {error['msg']}"
            for error in exc.errors()
        )
        raise ValueError(
            f"requirements_json is not valid ({problems}). Use {REQUIREMENTS_SHAPE}; "
            "find_tools lists each tool's operations."
        ) from exc


def install_scheduled_worker_draft(
    agent: Any,
    store: Any,
    schedule: Any,
    *,
    target_agent_type: str,
    worker_policy_json: str,
    requirements_json: str,
) -> dict[str, Any] | None:
    """Persist an inert automatic job before checked policy activation."""
    from custom_agents.schedule_changes import WorkerConfigurationRequestV1
    from custom_agents.schedule_store import schedule_claim_scope_for_agent
    from custom_agents.system_schedule_changes import configure_worker

    claim_scope = schedule_claim_scope_for_agent(agent)
    if target_agent_type:
        claim_scope["claim_named_agent_type"] = target_agent_type
        claim_scope["claim_agent_id"] = None
        schedule.payload_meta.to_named_agent_type = target_agent_type
    automation = bool(worker_policy_json or requirements_json)
    if automation:
        schedule.enabled = False
    store.add(schedule, **claim_scope)
    if not automation:
        return None
    try:
        return configure_worker(
            agent,
            WorkerConfigurationRequestV1(
                schedule_id=schedule.schedule_id,
                target_agent_type=target_agent_type or agent.named_agent_type,
                expected_revision=0,
                activate_schedule=True,
                policy=ScheduleWorkerPolicySpecV1.model_validate_json(
                    worker_policy_json or "{}"
                ),
                capabilities=parse_requirements(requirements_json)
                or ScheduleCapabilityRequirementsV1(),
            ),
        )
    except (ValueError, PermissionError) as exc:
        return {
            "schedule_id": schedule.schedule_id,
            "enabled": False,
            "status": "draft",
            "reason": str(exc),
            "next_step": "Call inspect_scheduled_worker for eligibility and repair steps, then prepare_scheduled_job_access and configure_scheduled_worker. A custom registered type may need adding to agent.schedule_workers.eligible_agent_types through TeamLead runtime settings.",
        }


@tool
def inspect_scheduled_worker(schedule_id: str = "") -> str:
    """Inspect launch, admission and access state; workers may inspect their current job.

    TeamLead supplies a schedule id. A specialist can omit it and gets only the
    schedule backed by its current persisted task custody. The result includes
    current registration, explicit denies and automatic eligibility even for an
    unbound draft. TeamLead receives an exact suggested_settings_change for a
    missing custom type. Eligibility alone grants neither startup nor tool access.
    Read worker_configuration_supported first. Protected system and child-task
    timers return eligibility=null and their own execution/repair instructions.
    Memory timers retain TeamArchitect dispatch to MemReviewAgent over NATS;
    their ordinary automation_enabled=false is not a missing startup policy.
    """
    from custom_agents.schedule_worker_inspection import inspect_schedule_worker

    try:
        return json.dumps(inspect_schedule_worker(get_agent_ref(), schedule_id))
    except Exception as exc:
        return json.dumps({"error": str(exc)})


@tool
def authorize_scheduled_job_access(schedule_id: str, expected_revision: int) -> str:
    """Give this schedule the access its declared requirements need.

    The schedule holds its own access (every agent in its runs uses it, and
    only there): one grant per tool at the highest level its requirements
    need. Save the target and requirements with configure_scheduled_worker
    first and pass that revision. For each tool the schedule lacks, TeamLead
    hands on access it may give (people get a notice; under "ask me each time"
    a person approves in the Inbox); setup access the requirements no longer
    need ends (listed in ended). What TeamLead cannot give is listed in
    ask_a_person with the request_access call to use; the worker asks when it
    first needs it. When the owner lacks a tool, only a team administrator can
    give it. This starts no worker and runs no tool.
    """
    from custom_agents.schedule_worker_service import ScheduleWorkerService

    try:
        return json.dumps(
            ScheduleWorkerService(get_agent_ref()).authorize_access(
                schedule_id, expected_revision=expected_revision
            )
        )
    except Exception as exc:
        return json.dumps({"error": str(exc)})


@tool
def prepare_scheduled_job_access(
    schedule_id: str, target_agent_type: str = "", requirements_json: str = ""
) -> str:
    """Preview the target worker's tools and access without running the job.

    requirements_json is {"requirements":[{"capability_id":"<tool catalog id, e.g.
    fn:web_research>","operation":"<one of its operations, e.g. exa_search>",
    "necessity":"required"}],"runtime_discovery":true}; find_tools lists tool ids and
    operations. Omit it to inspect the saved requirements. missing_access lists each tool the worker type or the schedule's
    owner does not hold yet (access that outlasts one task); give it with
    authorize_scheduled_job_access. Unknown needs are decided when the tool runs.
    """
    from custom_agents.schedule_worker_service import ScheduleWorkerService

    try:
        return json.dumps(
            ScheduleWorkerService(get_agent_ref()).prepare(
                schedule_id,
                agent_type=target_agent_type or None,
                capabilities=parse_requirements(requirements_json),
            )
        )
    except Exception as exc:
        return json.dumps({"error": str(exc)})


@tool
def configure_scheduled_worker(
    schedule_id: str,
    target_agent_type: str,
    expected_revision: int,
    worker_policy_json: str = "",
    requirements_json: str = "",
    activate_schedule: bool = False,
    convert_exact_target: bool = False,
) -> str | dict[str, Any]:
    """Save which worker type runs a schedule and when it starts automatically.

    Use revision 0 for the first policy. worker_policy_json accepts enabled,
    expires_at_utc and rules such as claim_wait_seconds, startup_timeout_seconds,
    admission_timeout_seconds, max_launch_attempts_per_occurrence, retry_backoff_seconds,
    launch_when_busy and idle_ttl_seconds. Values remain within operator limits.
    Leave worker_policy_json empty to keep the saved settings ("{}" resets them to
    defaults). requirements_json uses the same shape as prepare_scheduled_job_access
    and replaces the saved requirements, also while automatic startup is off;
    leave it empty to keep them. The result's saved_requirements and summary show
    what was saved and whether it is ready. Re-saving with the
    same target and the current revision repairs a changed team definition after
    the owner agrees; inspect_scheduled_worker returns the values as suggested_refresh. Retargeting
    takes effect after a current claim ends. Set enabled=false to suspend automatic
    startup. activate_schedule enables a draft only when known requirements pass,
    including access the worker type and the owner hold. Inspect eligibility first.
    TeamLead can add a registered specialist through the eligible_agent_types runtime
    setting; explicit operator denies still win. For a memory-review schedule the
    runtime manages, the result is approval_required with a change_id for apply_change.
    """
    from custom_agents.schedule_changes import WorkerConfigurationRequestV1
    from custom_agents.system_schedule_changes import configure_worker

    try:
        request = WorkerConfigurationRequestV1(
            schedule_id=str(schedule_id or "").strip(),
            target_agent_type=str(target_agent_type or "").strip(),
            expected_revision=expected_revision,
            policy=ScheduleWorkerPolicySpecV1.model_validate_json(worker_policy_json)
            if worker_policy_json
            else None,
            capabilities=parse_requirements(requirements_json),
            activate_schedule=activate_schedule,
            convert_exact_target=convert_exact_target,
        )
        return json.dumps(configure_worker(get_agent_ref(), request), default=str)
    except Exception as exc:
        return failed({"error": str(exc)})
