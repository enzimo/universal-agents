"""One durable launch boundary for manager, delegation and schedule requests."""

from __future__ import annotations

import logging
from datetime import datetime, timedelta
from typing import Any, Literal, cast
from uuid import uuid4

from custom_agents.schedule_repair_guidance import (
    AGENT_DEFINITION_CHANGED,
    AGENT_DEFINITION_UNAVAILABLE,
    AGENT_TYPE_DISALLOWED,
    AGENT_TYPE_INELIGIBLE,
    SCHEDULE_LAUNCH_INTENT_CHANGED,
)
from custom_agents.schedule_definition_identity import (
    DEFINITION_DECLARATION_MISSING,
    DEFINITION_SOURCE_UNKNOWN,
    definition_is_current,
    definition_source_of,
    launch_definition_digest,
)
from custom_messages.contracts.runtime_v1 import TEAM_ARCHITECT_AGENT_TYPE
from custom_messages.contracts.schedule_worker_v1 import (
    AUTOMATIC_WORKER_CAPACITY_LIMIT,
    DEFINITION_SOURCE_RELEASE,
    TEAM_WORKER_CAPACITY_LIMIT,
    WORKER_LAUNCH_RATE_LIMIT,
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
from runtime_settings.schedule_workers import (
    OPERATOR_LIMITED_RULES,
    RULE_EXCEEDS_OPERATOR_LIMIT,
    ScheduleWorkersConfig,
)
from storage.agent_launch_repository import (
    LAUNCH_CONFLICT_CODES,
    AgentLaunchRepository,
    LaunchCapacity,
    LaunchConflict,
)

# Names this process in every start it records: its runtime run, host, process
# number and start time (runtime_hosting.process_run), so the team lead can
# later prove whether the process is gone.
PROCESS_HOST_ID = current_process_identity().text()

logger = logging.getLogger(__name__)

# Why a start ended before its worker was ready (``AgentLaunchV1.reason``).
# The worker pool controller counts such a start as a failed start of its type
# and waits longer before each next start (custom_agents.worker_pool_controller).
# These reasons reach TeamArchitect's review and administrators' notices, so a
# stored reason is only ever a project code, an error's type, or a NATS
# refusal's code and description (``refusal_code``, ``launcher_refusal_reason``,
# ``start_failure_reason``); the full error stays in the log.
CRASHED_BEFORE_READY = "crashed_before_ready"
EXITED_BEFORE_READY = "exited_before_ready"
LAUNCHER_REFUSED = "launcher_refused"
STARTUP_TIMED_OUT = "startup_timed_out"
# Only a schedule's own startup timeout, shorter than its type's, ran out: about
# that schedule, not the type (custom_agents.schedule_worker_controller).
SCHEDULE_STARTUP_TIMED_OUT = "schedule_startup_timed_out"
# The runtime stopped or restarted while the worker started.
STOPPED_BY_RUNTIME = "runtime_stopped_before_ready"
# The launcher found the start's process or thread gone with no end recorded:
# after a restart, or when the worker's own exit record could not be written.
LAUNCHER_RECONCILIATION = "launcher_reconciliation"
# Edits made while the worker started; the next start uses the new version.
LAUNCH_POLICY_REVOKED = "launch_policy_revoked_before_readiness"
DEFINITION_CHANGED_BEFORE_START = "agent_definition_changed_before_start"
DEFINITION_CHANGED_BEFORE_CONSTRUCTION = "agent_definition_changed_before_construction"
DEFINITION_CHANGED_DURING_STARTUP = "agent_definition_changed_during_startup"
IDLE_TTL_LIMIT_CHANGED = "idle_ttl_limit_changed_before_start"
AUTHORIZATION_MODE_CHANGED = "launcher_authorization_mode_changed_restart_required"
WORKER_POOLS_DISABLED = "worker_pools_disabled"
# The launcher's other refusals and failures.
LOCAL_LAUNCHER_UNAVAILABLE = "local_launcher_unavailable"
MANAGER_LAUNCH_FORBIDDEN = "automatic_manager_launch_forbidden"
MCP_PROFILES_FROM_AGENTS = "mcp_profile_names_not_accepted_from_agents"
LAUNCH_REQUIRES_OWNER = "launch_requires_canonical_owner"
SCHEDULE_LAUNCH_IDENTITY_MISMATCH = "schedule_launch_identity_mismatch"
IDLE_TTL_OUTSIDE_LIMITS = "idle_ttl_outside_operator_limits"
SCHEDULE_LAUNCH_NEEDS_BINDING = "schedule_launch_requires_durable_binding"
RUNTIME_LAUNCH_IDENTITY_MISMATCH = "runtime_launch_identity_mismatch"
STALE_RUNTIME_NOT_READY = "stale_runtime_cannot_become_ready"
CONSTRUCTION_FAILED = "construction_failed"
LAUNCHER_REJECTED_START = "launcher_rejected_start"
# Refusals of instantiate_agent_type (custom_agents.strands_team).
LAUNCH_IDENTITY_MISMATCH = "launch_identity_mismatch"
IDLE_TTL_NOT_POSITIVE = "idle_ttl_seconds_not_positive"
MCP_LAUNCH_PROFILE_UNAVAILABLE = "mcp_launch_profile_unavailable"

# Start ends that say nothing about the worker type, so they are not failed
# starts of it (``counts_as_failed_start``): the runtime ended the start, or an
# edit made while it started ended it. Counting them made a routine restart, or
# a schedule edited during its worker's start, hold back a healthy type and
# open a start_failed review (review of PR #160, 2026-11-20).
# AGENT_TYPE_INELIGIBLE: the operator switched automatic (scheduled) starts off,
# or took the type off their eligible list, while a scheduled worker started;
# a rule about scheduled starting, like WORKER_POOLS_DISABLED for pool starts.
UNCOUNTED_START_ENDS = frozenset(
    {
        AGENT_TYPE_INELIGIBLE,
        STOPPED_BY_RUNTIME,
        LAUNCHER_RECONCILIATION,
        LAUNCH_POLICY_REVOKED,
        SCHEDULE_LAUNCH_INTENT_CHANGED,
        SCHEDULE_STARTUP_TIMED_OUT,
        DEFINITION_CHANGED_BEFORE_START,
        DEFINITION_CHANGED_BEFORE_CONSTRUCTION,
        DEFINITION_CHANGED_DURING_STARTUP,
        IDLE_TTL_LIMIT_CHANGED,
        AUTHORIZATION_MODE_CHANGED,
        WORKER_POOLS_DISABLED,
    }
)

# Every code a start can end with or be refused with: the codes above, the
# scheduled-worker reasons the launcher raises, the launch store's conflicts,
# the capacity and hourly limits, and the definition checks. ``refusal_code``
# keeps an error's text only when it is one of these (or a refused rule's
# ``RULE_EXCEEDS_OPERATOR_LIMIT:<field>``), so text that merely looks like a
# code (a name, an id) never reaches a stored reason. A new code raised on the
# launch path must be added here; tests/test_worker_pool_failed_starts.py
# scans the raise sites and fails until it is.
KNOWN_START_CODES = (
    frozenset(
        {
            EXITED_BEFORE_READY,
            STARTUP_TIMED_OUT,
            AGENT_DEFINITION_CHANGED,
            AGENT_DEFINITION_UNAVAILABLE,
            AGENT_TYPE_DISALLOWED,
            DEFINITION_SOURCE_UNKNOWN,
            DEFINITION_DECLARATION_MISSING,
            LOCAL_LAUNCHER_UNAVAILABLE,
            MANAGER_LAUNCH_FORBIDDEN,
            MCP_PROFILES_FROM_AGENTS,
            LAUNCH_REQUIRES_OWNER,
            SCHEDULE_LAUNCH_IDENTITY_MISMATCH,
            IDLE_TTL_OUTSIDE_LIMITS,
            SCHEDULE_LAUNCH_NEEDS_BINDING,
            RUNTIME_LAUNCH_IDENTITY_MISMATCH,
            STALE_RUNTIME_NOT_READY,
            CONSTRUCTION_FAILED,
            LAUNCHER_REJECTED_START,
            LAUNCH_IDENTITY_MISMATCH,
            IDLE_TTL_NOT_POSITIVE,
            MCP_LAUNCH_PROFILE_UNAVAILABLE,
            AUTOMATIC_WORKER_CAPACITY_LIMIT,
            TEAM_WORKER_CAPACITY_LIMIT,
            WORKER_LAUNCH_RATE_LIMIT,
        }
    )
    | UNCOUNTED_START_ENDS
    | LAUNCH_CONFLICT_CODES
)
# The two formatted reasons; this release is the first to write them, so an
# older release's free error text never starts with either.
_FORMATTED_REASON_PREFIXES = (f"{CRASHED_BEFORE_READY}: ", f"{LAUNCHER_REFUSED}: ")


def is_known_code(text: str) -> bool:
    """Whether ``text`` is one of the project's start codes (``KNOWN_START_CODES``, or a refused rule with its field)."""

    if text in KNOWN_START_CODES:
        return True
    prefix, _, field = text.partition(":")
    return prefix == RULE_EXCEEDS_OPERATOR_LIMIT and field in OPERATOR_LIMITED_RULES


def is_current_reason(reason: str | None) -> bool:
    """Whether a start record's reason has a form this release writes.

    A known code, or one of the two formatted forms
    (``crashed_before_ready: <error type>...``, ``launcher_refused: <error type>``).
    Until this release the launcher stored a refusal's free error text, which
    can hold a host path or configuration values; such a reason is neither
    counted (``counts_as_failed_start``) nor shown (``shown_reason``).
    """

    return reason is not None and (
        is_known_code(reason) or reason.startswith(_FORMATTED_REASON_PREFIXES)
    )


def shown_reason(reason: str | None) -> str | None:
    """A start record's reason as people may see it: None for one this release does not write."""

    return reason if is_current_reason(reason) else None


def counts_as_failed_start(reason: str | None) -> bool:
    """Whether a failed start record counts as a failed start of its type.

    Only a reason this release writes counts (``is_current_reason``), and not
    one of ``UNCOUNTED_START_ENDS``. No reason, or the free error text an
    older release stored, does not count: an upgrade right after an old
    release's failures does not hold the type back. This release always
    writes a reason (the round-trip tests in tests/test_worker_pool_failed_starts.py
    check every writer), so its failures always count.
    """

    return is_current_reason(reason) and reason not in UNCOUNTED_START_ENDS


def refusal_code(error: BaseException) -> str | None:
    """The error's text when it is one of the project's start codes, else None.

    Only a ``ValueError``, a ``LaunchConflict`` or a ``PermissionError`` can
    carry a code, and only when its whole text is a known one
    (``is_known_code``). Any other text could hold anything, such as a host
    path, a pydantic validation error's input values, or a name that merely
    looks like a code.
    """

    if not isinstance(error, (ValueError, LaunchConflict, PermissionError)):
        return None
    text = str(error)
    return text if is_known_code(text) else None


def launcher_refusal_reason(error: BaseException) -> str:
    """The reason a refused start stores: its code, or ``launcher_refused: <error type>``."""

    return refusal_code(error) or f"{LAUNCHER_REFUSED}: {type(error).__name__}"


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
            raise ValueError(LOCAL_LAUNCHER_UNAVAILABLE)
        definition = runtime.resolve_definition(agent_type)
        if definition is None:
            raise ValueError(AGENT_DEFINITION_UNAVAILABLE)
        return cast(dict[str, Any], definition)

    def validate_type(
        self, agent_type: str, *, automatic: bool
    ) -> tuple[dict[str, Any], ScheduleWorkersConfig]:
        limits = current_worker_limits(self.agent)
        if agent_type in limits.denied_agent_types:
            raise PermissionError(AGENT_TYPE_DISALLOWED)
        definition = self.resolve_definition(agent_type)
        if automatic:
            if not limits.enabled or (
                agent_type not in limits.eligible_agent_types
                and not default_type_allowed(self.agent, agent_type, definition)
            ):
                raise PermissionError(AGENT_TYPE_INELIGIBLE)
            if agent_type == TEAM_ARCHITECT_AGENT_TYPE:
                raise PermissionError(MANAGER_LAUNCH_FORBIDDEN)
        return definition, limits

    def validate_pool_start(self, agent_type: str) -> None:
        """A worker pool start: pools on, and never TeamArchitect (denies are checked in validate_type)."""

        from custom_agents.worker_pool_settings import current_worker_pool_settings

        if not current_worker_pool_settings(self.agent).enabled:
            raise PermissionError(WORKER_POOLS_DISABLED)
        if agent_type == TEAM_ARCHITECT_AGENT_TYPE:
            raise PermissionError(MANAGER_LAUNCH_FORBIDDEN)

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
                raise PermissionError(MCP_PROFILES_FROM_AGENTS)
            definition, limits = self.validate_type(
                agent_type, automatic=source == "schedule"
            )
            if source == "pool":
                self.validate_pool_start(agent_type)
            digest = launch_definition_digest(definition)
            if binding is not None and not definition_is_current(binding, definition):
                raise PermissionError(AGENT_DEFINITION_CHANGED)
            if not owner_id:
                raise PermissionError(LAUNCH_REQUIRES_OWNER)
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
                    raise PermissionError(SCHEDULE_LAUNCH_IDENTITY_MISMATCH)
                self.validate_schedule_start(binding, occurrence_id)
            ttl = idle_ttl_seconds
            if ttl is not None and (ttl <= 0 or ttl > limits.max_idle_ttl_seconds):
                raise ValueError(IDLE_TTL_OUTSIDE_LIMITS)
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
                    raise PermissionError(DEFINITION_CHANGED_BEFORE_START)
                if ttl is not None and ttl > final_limits.max_idle_ttl_seconds:
                    raise PermissionError(IDLE_TTL_LIMIT_CHANGED)
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
                    self._fail_start(record, result.reason)
                return result.model_copy(update={"created": True})
            except (ValueError, PermissionError) as exc:
                reason = launcher_refusal_reason(exc)
                self._fail_start(record, reason, error=exc)
                return AgentLaunchResultV1(
                    status="denied", launch_id=launch_id, reason=reason, created=True
                )
            except Exception:
                self._fail_start(record, CONSTRUCTION_FAILED)
                self.agent._logger.exception(
                    "Worker construction failed",
                    extra={"launch_id": launch_id, "named_agent_type": agent_type},
                )
                return AgentLaunchResultV1(
                    status="failed",
                    launch_id=launch_id,
                    reason=CONSTRUCTION_FAILED,
                    created=True,
                )
        except (ValueError, PermissionError, LaunchConflict) as exc:
            return self._refused_before_reservation(exc, agent_type=agent_type)

    def _fail_start(
        self, record: AgentLaunchV1, reason: str | None, *, error: BaseException | None = None
    ) -> None:
        """Fail a reserved start with its reason and log it (``log_failed_start``).

        A launcher refusal that gave no reason is ``launcher_rejected_start``.
        The full error goes into the log line only when the reason kept just
        its type (a code says it all).
        """

        reason = reason or LAUNCHER_REJECTED_START
        failed = self.repository.update(record, state=AgentLaunchState.FAILED, reason=reason)
        untold = error is not None and refusal_code(error) is None
        log_failed_start(failed, reason, error=error if untold else None)

    def _refused_before_reservation(
        self, error: BaseException, *, agent_type: str
    ) -> AgentLaunchResultV1:
        """A start refused before its record was reserved: no record to fail.

        The reason is ``launcher_refusal_reason``; when it kept only the
        error's type, the full error is logged for operators.
        """

        reason = launcher_refusal_reason(error)
        if refusal_code(error) is None:
            logger.warning(
                "A worker start was refused by an error that is not a refusal code; only "
                "its type is kept as the reason. event=worker_launch_refused team_id=%s "
                "agent_type=%s reason=%s error=%s",
                self.agent.team_id,
                agent_type,
                reason,
                error,
            )
        return AgentLaunchResultV1(status="denied", reason=reason)

    def validate_schedule_start(
        self, binding: ScheduleWorkerBindingV1 | None, occurrence_id: str | None
    ) -> None:
        from custom_agents.schedule_execution_identity import execution_digest
        from custom_messages.contracts.schedule_worker_v1 import occurrence_identity

        if binding is None or occurrence_id is None:
            raise PermissionError(SCHEDULE_LAUNCH_NEEDS_BINDING)
        store = self.agent._schedule_store
        if bool(
            current_launch_config(self.agent).get("authorization.enabled", False)
        ) != bool(self.agent.authorization_config.get("enabled", False)):
            raise PermissionError(AUTHORIZATION_MODE_CHANGED)
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


def _error_chain(error: BaseException, *, depth: int = 5) -> list[BaseException]:
    """The error and those under it (``__cause__``, else ``__context__``), a few steps deep."""

    chain: list[BaseException] = []
    current: BaseException | None = error
    while current is not None and len(chain) < depth:
        chain.append(current)
        current = current.__cause__ or current.__context__
    return chain


def _nats_refusal(error: BaseException) -> tuple[int, str] | None:
    """A NATS JetStream refusal's code and description, when ``error`` is one."""

    err_code = getattr(error, "err_code", None)
    description = getattr(error, "description", None)
    if isinstance(err_code, int) and isinstance(description, str) and description:
        return err_code, description[:200]
    return None


def start_failure_reason(error: BaseException | None) -> str:
    """The start record's reason for a worker that ended before it was ready.

    A project refusal keeps its code (``refusal_code``), as refusals before
    the start already do. Otherwise only the error's type is kept, plus the
    code and description of a NATS JetStream refusal (server text such as
    "maximum consumers limit reached"). Other error text can carry anything,
    and this reason reaches TeamArchitect's review and administrators'
    notices; the full error is in the log (``event=agent_thread_crash``).

    The errors under the one that ended the thread are read too, newest
    first, so a cleanup error raised over the real refusal still names it.
    """

    if error is None:
        return EXITED_BEFORE_READY
    crashed = f"{CRASHED_BEFORE_READY}: {type(error).__name__}"
    for current in _error_chain(error):
        code = refusal_code(current)
        if code is not None:
            return code
        refusal = _nats_refusal(current)
        if refusal is not None:
            return f"{crashed} (NATS {refusal[0]}: {refusal[1]})"
    return crashed


def update_runtime_launch(
    agent: Any, *, state: AgentLaunchState, reason: str | None = None
) -> None:
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
        raise LaunchConflict(RUNTIME_LAUNCH_IDENTITY_MISMATCH)
    if state == AgentLaunchState.READY:
        if (
            current.state != AgentLaunchState.STARTING
            or current.lease_expires_at_utc <= utc_now()
        ):
            raise LaunchConflict(STALE_RUNTIME_NOT_READY)
        definition, _ = AgentInstantiationService(agent).validate_type(
            record.named_agent_type, automatic=record.source == "schedule"
        )
        if launch_definition_digest(definition) != record.definition_digest:
            raise LaunchConflict(DEFINITION_CHANGED_DURING_STARTUP)
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
                raise LaunchConflict(LAUNCH_POLICY_REVOKED)
    changes: dict[str, Any] = {"ready_at_utc": utc_now()} if state == AgentLaunchState.READY else {}
    if reason is not None:
        changes["reason"] = reason
    agent._worker_launch = repository.update(current, state=state, **changes)


def log_failed_start(
    record: AgentLaunchV1, reason: str, *, error: BaseException | None = None
) -> None:
    """One log line per start that ended before its worker was ready, where it ended.

    Warning when the reason counts as a failed start of the type (the worker
    pool controller then waits before the next start of it), info when it
    does not. ``error`` is the full error when the stored reason kept only its
    type; logs are for operators, the stored reason reaches people.
    """

    counted = counts_as_failed_start(reason)
    logger.log(
        logging.WARNING if counted else logging.INFO,
        "A worker start ended before the worker was ready. event=worker_start_failed "
        "team_id=%s agent_type=%s launch_id=%s agent_id=%s source=%s reason=%s counted=%s error=%s",
        record.team_id,
        record.named_agent_type,
        record.launch_id,
        record.agent_id,
        record.source,
        reason,
        counted,
        "-" if error is None else error,
    )


def record_worker_exit(
    agent: Any, crash: BaseException | None, *, stopped_because: str | None = None
) -> None:
    """Settle a started worker's record when its thread ends (``strands_team.instantiate_agent_type``).

    A worker that was ready retires. One that ended before it was ready failed:
    with the reason the runtime asked it to stop for (``stopped_because``: a
    startup timeout, a revoked start, the runtime stopping), since the stop
    caused the exit, or else with its crash (``start_failure_reason``). The
    worker pool controller counts it as a failed start of the type when the
    reason counts (``counts_as_failed_start``) and waits before the next start.
    """

    if agent._worker_ready:
        update_runtime_launch(agent, state=AgentLaunchState.RETIRED)
        return
    if agent._worker_launch is None:
        return
    reason = stopped_because or start_failure_reason(crash)
    update_runtime_launch(agent, state=AgentLaunchState.FAILED, reason=reason)
    log_failed_start(agent._worker_launch, reason)


def renew_runtime_launch(agent: Any) -> None:
    record = getattr(agent, "_worker_launch", None)
    if record is None:
        return
    agent._worker_launch = AgentLaunchRepository(
        agent.task_manager.storage_settings
    ).renew(record, lease_seconds=current_worker_limits(agent).lease_seconds)
