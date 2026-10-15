"""One durable launch boundary for manager, delegation and schedule requests."""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any, Literal, cast
from uuid import uuid4

from custom_agents.schedule_repair_guidance import (
    AGENT_DEFINITION_CHANGED,
    SCHEDULE_LAUNCH_INTENT_CHANGED,
)
from custom_agents.schedule_definition_identity import (
    definition_is_current,
    definition_source_of,
    launch_definition_digest,
)
from custom_messages.contracts.runtime_v1 import TEAM_ARCHITECT_AGENT_TYPE
from custom_messages.contracts.schedule_worker_v1 import (
    DEFINITION_SOURCE_RELEASE,
    AgentCreationResultV1,
    AgentLaunchResultV1,
    AgentLaunchState,
    AgentLaunchV1,
    ScheduleWorkerBindingV1,
    ScheduleWorkerRulesV1,
    utc_now,
    worker_digest,
)
from runtime_hosting.process_run import current_process_identity
from custom_agents.saved_runtime_config import current_launch_config
from runtime_settings.schedule_workers import ScheduleWorkersConfig
from storage.agent_launch_repository import (
    AgentLaunchRepository,
    LaunchCapacity,
    LaunchConflict,
)

# Names this process in every start it records: its runtime run, host, process
# number and start time (runtime_hosting.process_run), so the team lead can
# later prove whether the process is gone.
PROCESS_HOST_ID = current_process_identity().text()


def current_worker_limits(agent: Any) -> ScheduleWorkersConfig:
    """The saved scheduled-worker settings, never a startup copy (``current_launch_config``)."""
    configured = current_launch_config(agent).get("agent.schedule_workers")
    return ScheduleWorkersConfig.model_validate(configured or agent.schedule_workers)


def default_type_allowed(
    agent: Any, agent_type: str, definition: dict[str, Any]
) -> bool:
    """The default startup policy follows the shipped catalog.

    Shipped types change only with a deploy, which is the operator's approval.
    Team-written types need an explicit eligible_agent_types entry. Denies, the
    kill switch and the TeamArchitect ban are enforced by validate_type.
    """
    return bool(
        current_worker_limits(agent).default_startup_policy_enabled
        and agent_type != TEAM_ARCHITECT_AGENT_TYPE
        and definition_source_of(definition) == DEFINITION_SOURCE_RELEASE
    )


def effective_worker_rules(
    agent: Any, agent_type: str, overrides: dict[str, Any]
) -> ScheduleWorkerRulesV1:
    config = current_launch_config(agent)
    limits = ScheduleWorkersConfig.model_validate(
        config.get("agent.schedule_workers") or agent.schedule_workers
    )
    type_rules = config.get(f"agent_types.{agent_type}.agent.schedule_worker_rules")
    return limits.resolve_rules({**(type_rules or {}), **overrides})


class AgentInstantiationService:
    def __init__(self, agent: Any) -> None:
        self.agent = agent
        self.repository = AgentLaunchRepository(agent.task_manager.storage_settings)

    def resolve_definition(self, agent_type: str) -> dict[str, Any]:
        runtime = self.agent._local_team_runtime
        if runtime is None:
            raise ValueError("local_launcher_unavailable")
        definition = runtime.resolve_definition(agent_type)
        if definition is None:
            raise ValueError("agent_definition_unavailable")
        return cast(dict[str, Any], definition)

    def validate_type(
        self, agent_type: str, *, automatic: bool
    ) -> tuple[dict[str, Any], ScheduleWorkersConfig]:
        limits = current_worker_limits(self.agent)
        if agent_type in limits.denied_agent_types:
            raise PermissionError("agent_type_disallowed")
        definition = self.resolve_definition(agent_type)
        if automatic:
            if not limits.enabled or (
                agent_type not in limits.eligible_agent_types
                and not default_type_allowed(self.agent, agent_type, definition)
            ):
                raise PermissionError("agent_type_ineligible_for_automatic_launch")
            if agent_type == TEAM_ARCHITECT_AGENT_TYPE:
                raise PermissionError("automatic_manager_launch_forbidden")
        return definition, limits

    def validate_pool_start(self, agent_type: str) -> None:
        """A worker pool start: pools on, and never TeamArchitect (denies are checked in validate_type)."""

        from custom_agents.worker_pool_settings import current_worker_pool_settings

        if not current_worker_pool_settings(self.agent).enabled:
            raise PermissionError("worker_pools_disabled")
        if agent_type == TEAM_ARCHITECT_AGENT_TYPE:
            raise PermissionError("automatic_manager_launch_forbidden")

    def capacity(self, agent_type: str, limits: ScheduleWorkersConfig) -> LaunchCapacity:
        """The limits a start is checked against, read now (custom_agents.worker_pool_limits)."""

        from custom_agents.worker_pool_limits import team_limit, type_limit
        from custom_agents.worker_pool_settings import current_worker_pool_settings

        pools = current_worker_pool_settings(self.agent)
        return LaunchCapacity(
            lease_seconds=limits.lease_seconds,
            max_total_workers_per_team=team_limit(self.agent, pools).effective,
            max_launches_per_type_per_hour=pools.max_launches_per_type_per_hour,
            max_workers_of_type=type_limit(self.agent, agent_type, pools).effective,
        )

    def launch(
        self,
        *,
        agent_type: str,
        owner_id: str,
        request_id: str,
        source: str = "manual",
        idle_ttl_seconds: float | None = None,
        mcp_profile_names: list[str] | None = None,
        binding: ScheduleWorkerBindingV1 | None = None,
        occurrence_id: str | None = None,
        join_pending: bool | None = None,
    ) -> AgentLaunchResultV1:
        try:
            if mcp_profile_names:
                # An agent cannot add MCP servers to a new agent; MCP
                # connections are attached through the MCP registry.
                raise PermissionError("mcp_profile_names_not_accepted_from_agents")
            definition, limits = self.validate_type(
                agent_type, automatic=source == "schedule"
            )
            if source == "pool":
                self.validate_pool_start(agent_type)
            digest = launch_definition_digest(definition)
            if binding is not None and not definition_is_current(binding, definition):
                raise PermissionError(AGENT_DEFINITION_CHANGED)
            if not owner_id:
                raise PermissionError("launch_requires_canonical_owner")
            # Who may start agents is decided before this point: the per-call
            # access hook for instantiate_agent, the schedule's saved and
            # validated policy for scheduled starts, and trusted runtime code
            # for delegation and internal maintenance.
            if source == "schedule":
                if binding is None or (
                    binding.team_id != self.agent.team_id
                    or binding.owner_canonical_user_id != owner_id
                    or binding.target_agent_type != agent_type
                ):
                    raise PermissionError("schedule_launch_identity_mismatch")
                self.validate_schedule_start(binding, occurrence_id)
            ttl = idle_ttl_seconds
            if ttl is not None and (ttl <= 0 or ttl > limits.max_idle_ttl_seconds):
                raise ValueError("idle_ttl_outside_operator_limits")
            launch_id = (
                "launch_"
                + worker_digest([self.agent.team_id, owner_id, request_id])[:32]
            )
            now = utc_now()
            launch = AgentLaunchV1(
                launch_id=launch_id,
                agent_id="agent_" + uuid4().hex,
                team_id=self.agent.team_id,
                owner_canonical_user_id=owner_id,
                named_agent_type=agent_type,
                definition_digest=digest,
                host_id=PROCESS_HOST_ID,
                source=source,
                schedule_id=binding.schedule_id if binding else None,
                policy_revision=binding.revision if binding else None,
                occurrence_id=occurrence_id,
                idle_ttl_seconds=ttl,
                lease_expires_at_utc=now + timedelta(seconds=limits.lease_seconds),
            )
            # NATS presence can describe remote workers. Only this process's
            # registered workers consume capacity for the local launch policy.
            external = tuple(
                (status.self_id, status.named_agent_type)
                for status in self.agent._local_team_runtime.local_workers(self.agent.team_id)
            )
            record, created = self.repository.reserve(
                launch,
                self.capacity(agent_type, limits),
                join=source not in {"manual", "pool"} if join_pending is None else join_pending,
                external_workers=external,
            )
            if not created:
                return launch_result(record)
            record = self.repository.update(record, state=AgentLaunchState.STARTING)
            try:
                # Reservation is not permission to start after a concurrent edit.
                final_definition, final_limits = self.validate_type(
                    agent_type, automatic=source == "schedule"
                )
                if launch_definition_digest(final_definition) != record.definition_digest:
                    raise PermissionError("agent_definition_changed_before_start")
                if ttl is not None and ttl > final_limits.max_idle_ttl_seconds:
                    raise PermissionError("idle_ttl_limit_changed_before_start")
                if source == "schedule":
                    self.validate_schedule_start(binding, occurrence_id)
                if source == "pool":
                    self.validate_pool_start(agent_type)
                result: AgentLaunchResultV1 = (
                    self.agent._local_team_runtime.spawn_agent(
                        agent_type,
                        self.agent.team_id,
                        launch=record,
                        mcp_profile_names=mcp_profile_names,
                        idle_ttl_seconds=ttl,
                        runtime_config=current_launch_config(self.agent),
                    )
                )
                if not result.accepted:
                    self.repository.update(
                        record,
                        state=AgentLaunchState.FAILED,
                        reason=result.reason or "launcher_rejected_start",
                    )
                return result.model_copy(update={"created": True})
            except (ValueError, PermissionError) as exc:
                self.repository.update(
                    record, state=AgentLaunchState.FAILED, reason=str(exc)
                )
                return AgentLaunchResultV1(
                    status="denied", launch_id=launch_id, reason=str(exc), created=True
                )
            except Exception:
                self.repository.update(
                    record, state=AgentLaunchState.FAILED, reason="construction_failed"
                )
                self.agent._logger.exception(
                    "Worker construction failed",
                    extra={"launch_id": launch_id, "named_agent_type": agent_type},
                )
                return AgentLaunchResultV1(
                    status="failed",
                    launch_id=launch_id,
                    reason="construction_failed",
                    created=True,
                )
        except (ValueError, PermissionError, LaunchConflict) as exc:
            return AgentLaunchResultV1(status="denied", reason=str(exc))

    def validate_schedule_start(
        self, binding: ScheduleWorkerBindingV1 | None, occurrence_id: str | None
    ) -> None:
        from custom_agents.schedule_execution_identity import execution_digest
        from custom_messages.contracts.schedule_worker_v1 import occurrence_identity

        if binding is None or occurrence_id is None:
            raise PermissionError("schedule_launch_requires_durable_binding")
        store = self.agent._schedule_store
        if bool(
            current_launch_config(self.agent).get("authorization.enabled", False)
        ) != bool(self.agent.authorization_config.get("enabled", False)):
            raise PermissionError(
                "launcher_authorization_mode_changed_restart_required"
            )
        current = store.worker_records.get_binding(
            binding.schedule_id, team_id=self.agent.team_id
        )
        schedule = store.get_scheduled_message(binding.schedule_id)
        now = utc_now()
        if (
            current is None
            or current != binding
            or current.revision != binding.revision
            or not current.policy.enabled
            or (current.policy.expires_at_utc and current.policy.expires_at_utc <= now)
            or schedule is None
            or not schedule.enabled
            or not schedule.next_fire_at
            or (
                schedule.paused_until
                and datetime.fromisoformat(schedule.paused_until) > now
            )
            or (
                schedule.expires_at_utc
                and datetime.fromisoformat(schedule.expires_at_utc) <= now
            )
            or datetime.fromisoformat(schedule.next_fire_at) > now
            or occurrence_identity(schedule.schedule_id, schedule.next_fire_at)
            != occurrence_id
            or execution_digest(schedule, current.definition_digest)
            != current.execution_digest
            or store.worker_records.get_occurrence(occurrence_id, self.agent.team_id)
            is not None
            or (
                current.authority_mode == "runtime_default"
                and not current_worker_limits(self.agent).default_startup_policy_enabled
            )
        ):
            raise PermissionError(SCHEDULE_LAUNCH_INTENT_CHANGED)
        effective_worker_rules(
            self.agent, current.target_agent_type, current.policy.rules
        )

    def default_idle_ttl(self, agent_type: str) -> float | None:
        """Use the same ephemeral-worker policy for chat and runtime launches."""
        from custom_agents.runtime_paths import agent_team_composition_path
        from custom_agents.team_composition import (
            ephemeral_idle_ttl_for_agent_type,
            load_team_composition,
        )

        path = agent_team_composition_path(self.agent)
        return ephemeral_idle_ttl_for_agent_type(load_team_composition(path), agent_type)

    def create(
        self, request: Any, *, source: Literal["manual", "delegation"] = "manual"
    ) -> AgentCreationResultV1:
        from custom_agents.access_runtime import verified_person_id

        if request.team_id != self.agent.team_id:
            return AgentCreationResultV1(
                request_id=request.request_id,
                results=(
                    AgentLaunchResultV1(status="denied", reason="same_team_required"),
                ),
            )
        owner_id = verified_person_id(self.agent) or ""  # the signed context's person
        ttl = request.idle_ttl_seconds
        if ttl is None:
            ttl = self.default_idle_ttl(request.named_agent_type)
        return AgentCreationResultV1(
            request_id=request.request_id,
            results=tuple(
                self.launch(
                    agent_type=request.named_agent_type,
                    owner_id=owner_id,
                    request_id=f"{request.request_id}:{index}",
                    idle_ttl_seconds=ttl,
                    source=source,
                    join_pending=source == "delegation" and index == 0,
                    mcp_profile_names=request.mcp_profile_names,
                )
                for index in range(request.agent_quantity)
            ),
        )


def launch_result(record: AgentLaunchV1) -> AgentLaunchResultV1:
    status = {
        AgentLaunchState.READY: "ready",
        AgentLaunchState.FAILED: "failed",
        AgentLaunchState.RETIRED: "failed",
        AgentLaunchState.UNKNOWN: "waiting",
    }.get(record.state, "starting")
    return AgentLaunchResultV1(
        status=status,
        launch_id=record.launch_id,
        agent_id=record.agent_id,
        reason=record.reason,
    )


def update_runtime_launch(agent: Any, *, state: AgentLaunchState) -> None:
    record = agent._worker_launch
    if record is None:
        return
    repository = AgentLaunchRepository(agent.task_manager.storage_settings)
    current = repository.get(record.launch_id, agent.team_id)
    if (
        current is None
        or current.agent_id != agent.id
        or current.host_id != PROCESS_HOST_ID
        or current.fence != record.fence
    ):
        raise LaunchConflict("runtime_launch_identity_mismatch")
    if state == AgentLaunchState.READY:
        if (
            current.state != AgentLaunchState.STARTING
            or current.lease_expires_at_utc <= utc_now()
        ):
            raise LaunchConflict("stale_runtime_cannot_become_ready")
        definition, _ = AgentInstantiationService(agent).validate_type(
            record.named_agent_type, automatic=record.source == "schedule"
        )
        if launch_definition_digest(definition) != record.definition_digest:
            raise LaunchConflict("agent_definition_changed_during_startup")
        if record.source == "schedule":
            binding = agent._schedule_store.worker_records.get_binding(
                record.schedule_id, team_id=agent.team_id
            )
            schedule = agent._schedule_store.get_scheduled_message(record.schedule_id)
            if (
                binding is None
                or not binding.policy.enabled
                or (binding.authority_mode == "runtime_default" and not current_worker_limits(agent).default_startup_policy_enabled)
                or binding.revision != record.policy_revision
                or (
                    binding.policy.expires_at_utc is not None
                    and binding.policy.expires_at_utc <= utc_now()
                )
                or binding.target_agent_type != record.named_agent_type
                # The launch digest was just checked against this definition,
                # so the launched definition is the one the policy must accept.
                or not definition_is_current(binding, definition)
                or schedule is None
                or not schedule.enabled
            ):
                raise LaunchConflict("launch_policy_revoked_before_readiness")
    changes = {"ready_at_utc": utc_now()} if state == AgentLaunchState.READY else {}
    agent._worker_launch = repository.update(current, state=state, **changes)


def renew_runtime_launch(agent: Any) -> None:
    record = getattr(agent, "_worker_launch", None)
    if record is None:
        return
    agent._worker_launch = AgentLaunchRepository(
        agent.task_manager.storage_settings
    ).renew(record, lease_seconds=current_worker_limits(agent).lease_seconds)
