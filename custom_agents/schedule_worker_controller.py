"""Deterministic coverage of overdue schedules, independent of the lead's model."""

from __future__ import annotations

import logging
from datetime import datetime, timedelta
from functools import partial
from typing import Any

from custom_agents.agent_instantiation_service import (
    LAUNCH_POLICY_REVOKED,
    LAUNCHER_RECONCILIATION,
    SCHEDULE_STARTUP_TIMED_OUT,
    STARTUP_TIMED_OUT,
    AgentInstantiationService,
    current_worker_limits,
    effective_worker_rules,
    is_current_reason,
    launcher_refusal_reason,
    log_failed_start,
    refusal_code,
)
from custom_agents.schedule_definition_identity import (
    definition_changes,
    definition_is_current,
    launch_definition_digest,
    refresh_definition_record,
)
from custom_agents.schedule_execution_identity import execution_digest
from custom_agents.schedule_repair_guidance import (
    AGENT_DEFINITION_CHANGED,
    EXISTING_WORKER_NOT_CLAIMING,
    KNOWN_CAPABILITY_BLOCKER,
    LAUNCH_LIVENESS_UNKNOWN,
    SCHEDULE_EXECUTION_CHANGED,
    WORKER_LAUNCH_ATTEMPTS_EXHAUSTED,
    WORKER_LAUNCH_UNAVAILABLE,
    WORKER_POLICY_EXPIRED,
    WORKER_READY_BUT_UNCLAIMED,
    WORKER_RECONCILIATION_FAILED,
    SCHEDULE_BLOCK_REASONS,
)
from custom_agents.schedule_readiness import (
    RESOURCE_CONSTRAINTS_UNSUPPORTED,
    effective_requirements,
    prepare_schedule_access,
)
from custom_messages.contracts.schedule_worker_v1 import (
    AgentLaunchState,
    occurrence_identity,
    utc_now,
)
from storage.agent_launch_repository import ACTIVE_LAUNCH_STATES, LaunchConflict


logger = logging.getLogger(__name__)

# The codes a schedule's blocked reason may hold: the scheduler's own block
# reasons, plus the readiness check's fixed code for a requirement no adapter
# enforces (caught inside that check today, but a code all the same).
_BLOCK_CODES = SCHEDULE_BLOCK_REASONS | {RESOURCE_CONSTRAINTS_UNSUPPORTED}


def block_reason(error: BaseException, *, schedule_id: str) -> str:
    """The blocked reason a refused check stores: a known code, or ``WORKER_RECONCILIATION_FAILED``.

    The reason reaches the schedule inspection and the owner's notice, so
    free error text (a host path in a file error, a validation error's input)
    is never stored; it is logged here instead. A known code is a block
    reason (``SCHEDULE_BLOCK_REASONS``) or a start reason this release writes
    (``is_current_reason``), as the launcher's results carry (review finding
    RC-6, round 3).
    """

    text = str(error)
    if text in _BLOCK_CODES or is_current_reason(text):
        return text
    logger.warning(
        "A scheduled worker check failed with an error that is not a known code; the "
        "schedule is blocked as worker_reconciliation_failed. "
        "event=schedule_worker_block_reason_replaced schedule_id=%s error_type=%s error=%s",
        schedule_id,
        type(error).__name__,
        error,
    )
    return WORKER_RECONCILIATION_FAILED


def _date(value: str | None) -> datetime | None:
    return datetime.fromisoformat(value.replace("Z", "+00:00")) if value else None


def schedule_ready_for_admission(agent: Any, schedule: Any) -> bool:
    """Existing workers obey the same current policy/readiness checks as starts."""
    binding = agent._schedule_store.worker_records.get_binding(
        schedule.schedule_id, team_id=agent.team_id, active=True
    )
    try:
        launch = getattr(agent, "_worker_launch", None)
        if launch is not None:
            AgentInstantiationService(agent).validate_type(
                launch.named_agent_type, automatic=False
            )
        if binding is None:
            return True
        # Access is decided on every tool call of the run (both the worker
        # type and the owner), so admission checks only the definition, the
        # execution identity and the worker type's tools.
        definition, _ = AgentInstantiationService(agent).validate_type(
            binding.target_agent_type,
            automatic=False,
        )
        if schedule.claim_named_agent_type != binding.target_agent_type:
            # A retarget waits behind an already-live claim. That occurrence
            # finishes with its prior target; pending policy grants do not apply.
            return False
        if not definition_is_current(binding, definition):
            raise ValueError(AGENT_DEFINITION_CHANGED)
        if execution_digest(schedule, binding.definition_digest) != binding.execution_digest:
            raise ValueError(SCHEDULE_EXECUTION_CHANGED)
        readiness = prepare_schedule_access(
            agent,
            schedule,
            effective_requirements(binding),
            existing_worker=agent.named_agent_type == binding.target_agent_type,
            binding=binding,
        )
        if readiness.status == "blocked":
            raise ValueError(KNOWN_CAPABILITY_BLOCKER)
        return True
    except (ValueError, PermissionError) as exc:
        agent._schedule_store.worker_records.mark_blocked(
            schedule.schedule_id, agent.team_id, block_reason(exc, schedule_id=schedule.schedule_id)
        )
        return False


class ScheduleWorkerController:
    def __init__(self, agent: Any) -> None:
        self.agent = agent
        self.store = agent._schedule_store
        self.launcher = AgentInstantiationService(agent)
        self.runtime = agent.task_manager.operations_runtime

    async def reconcile(self) -> None:
        now = utc_now()
        limits = current_worker_limits(self.agent)
        await self._reconcile_launches(now, limits)
        bindings = await self.runtime.run(
            self.store.worker_records.list_bindings, self.agent.team_id
        )
        rows = await self.runtime.run(self.store.get_all_enabled)
        by_id = {row["schedule_id"]: row for row in rows}
        for binding in sorted(
            bindings,
            key=lambda item: str(
                by_id.get(item.schedule_id, {}).get("next_fire_at") or ""
            ),
        ):
            try:
                await self.runtime.run(
                    self.store.worker_records.apply_pending,
                    binding.schedule_id,
                    self.agent.team_id,
                )
                binding = await self.runtime.run(
                    self.store.worker_records.get_binding,
                    binding.schedule_id,
                    team_id=self.agent.team_id,
                )
                await self._refresh_definition_record(binding)
                row = by_id.get(binding.schedule_id)
                if (
                    row is not None
                    and binding.policy.enabled
                ):
                    await self._cover(binding, row, now, limits)
            except (PermissionError, ValueError, LaunchConflict) as exc:
                await self._block(binding, block_reason(exc, schedule_id=binding.schedule_id))
            except Exception:
                self.agent._logger.exception(
                    "Schedule worker reconciliation failed",
                    extra={
                        "schedule_id": binding.schedule_id,
                        "team_id": self.agent.team_id,
                    },
                )
                await self._block(binding, WORKER_RECONCILIATION_FAILED)
        await self.runtime.run(
            self.launcher.repository.prune,
            self.agent.team_id,
            before=now - timedelta(days=limits.retention_days),
        )
        await self.runtime.run(
            self.store.worker_records.prune,
            self.agent.team_id,
            before=now - timedelta(days=limits.retention_days),
        )

    async def _reconcile_launches(self, now: datetime, limits: Any) -> None:
        records = await self.runtime.run(
            self.launcher.repository.list, self.agent.team_id
        )
        for record in records:
            if record.state not in ACTIVE_LAUNCH_STATES:
                continue
            # A recent reservation may not have entered the adapter yet.
            if (
                record.state == AgentLaunchState.RESERVED
                and record.lease_expires_at_utc > now
            ):
                continue
            evidence = await self.runtime.run(
                self.agent._local_team_runtime.inspect_launch, record
            )
            target = None
            stop_reason = None
            if evidence == "absent":
                if record.ready_at_utc or record.lease_expires_at_utc <= now:
                    target = (
                        AgentLaunchState.RETIRED
                        if record.ready_at_utc
                        else AgentLaunchState.FAILED
                    )
            elif evidence == "unknown" and record.lease_expires_at_utc <= now:
                target = AgentLaunchState.UNKNOWN
            elif evidence == "ready" and record.lease_expires_at_utc <= now:
                target = AgentLaunchState.UNKNOWN
            elif evidence == "starting":
                stop_reason = self._why_stop_starting(record, now, limits)
                if stop_reason is not None:
                    stopped = await self.runtime.run(
                        self.agent._local_team_runtime.stop_launch,
                        record,
                        reason=stop_reason,
                    )
                    target = (
                        AgentLaunchState.FAILED if stopped else AgentLaunchState.UNKNOWN
                    )
                elif record.state == AgentLaunchState.STARTING:
                    try:
                        await self.runtime.run(
                            self.launcher.repository.renew,
                            record,
                            lease_seconds=limits.lease_seconds,
                        )
                    except LaunchConflict:
                        pass  # Readiness or exit won the same fence.
            if target is not None and target != record.state:
                # The first reason stays, as on the worker's handle: a start
                # that timed out and outlived the join is "unknown" with
                # startup_timed_out, and keeps it when it later fails, even if
                # another check (a schedule edit) stops it again meanwhile. A
                # start still starting has no reason yet; one found gone with
                # none gets this generic one.
                reason = record.reason or stop_reason or LAUNCHER_RECONCILIATION
                try:
                    await self.runtime.run(
                        self.launcher.repository.update,
                        record,
                        state=target,
                        reason=reason,
                    )
                except LaunchConflict:
                    # The child can publish readiness or exit while inspected.
                    self.agent._logger.info(
                        "Launch state changed during reconciliation",
                        extra={"launch_id": record.launch_id},
                    )
                    continue
                self._log_launch_transition(record, target, evidence, reason)

    def _why_stop_starting(
        self, record: Any, now: datetime, limits: Any
    ) -> str | None:
        """Why a worker of this process that is still starting must stop, or None.

        Checked in this order, and the reason becomes its start record's:

        - the rules it started with are refused: the type's rules with a
          scheduled start's own overrides on top, as the launcher checked
          them (a pool start is not checked against them before it starts).
          ``_rules_refusal`` decides whose rule it is: the type's own (its
          code, for example ``schedule_worker_rule_exceeds_operator_limit:<field>``,
          which lasts until the configuration is fixed and counts as a failed
          start of the type) or only the schedule's (``LAUNCH_POLICY_REVOKED``,
          which does not count);
        - a scheduled start's type is refused now: its code. A denied type
          counts; automatic starts switched off, or the type taken off the
          eligible list, does not (``UNCOUNTED_START_ENDS``);
        - a scheduled start's policy or definition changed:
          ``LAUNCH_POLICY_REVOKED`` (does not count);
        - the start took longer than its startup timeout (a schedule's own
          value when it sets one); ``_timeout_reason`` says whose ran out.
        """

        binding = (
            self.store.worker_records.get_binding(
                record.schedule_id, team_id=self.agent.team_id
            )
            if record.schedule_id
            else None
        )
        try:
            rules = effective_worker_rules(
                self.agent, record.named_agent_type, binding.policy.rules if binding else {}
            )
        except (ValueError, PermissionError) as exc:
            return self._rules_refusal(record, binding, exc)
        if record.source == "schedule":
            refused = self._scheduled_start_refusal(record, binding, now, limits)
            if refused is not None:
                return refused
        if record.created_at_utc + timedelta(seconds=rules.startup_timeout_seconds) <= now:
            return self._timeout_reason(record, binding, now)
        return None

    def _timeout_reason(self, record: Any, binding: Any, now: datetime) -> str:
        """Whose startup timeout ran out: the type's own (``STARTUP_TIMED_OUT``, counts) or only a schedule's.

        A schedule may set its own startup timeout. When only that shorter
        value ran out, the type may be fine and the schedule's setting is the
        cause: ``SCHEDULE_STARTUP_TIMED_OUT``, which does not count against the
        type (review finding GEN-1, 2026-11-20); the schedule's own start
        attempts and its owner's notice still report it. When the type's own
        rules are refused (a schedule's override is what made the start
        allowed), the type has no valid timeout to compare with, and the
        timeout counts.
        """

        if binding is None:
            return STARTUP_TIMED_OUT
        try:
            own = effective_worker_rules(self.agent, record.named_agent_type, {})
        except (ValueError, PermissionError):
            return STARTUP_TIMED_OUT
        if record.created_at_utc + timedelta(seconds=own.startup_timeout_seconds) <= now:
            return STARTUP_TIMED_OUT
        return SCHEDULE_STARTUP_TIMED_OUT

    def _rules_refusal(self, record: Any, binding: Any, error: BaseException) -> str:
        """Whose worker rules were refused: the type's own (counted code) or only a schedule's overrides.

        A schedule's own rule above an operator maximum (one lowered while its
        worker started) is about that schedule, so it does not count against
        the type; the type's rules alone decide that.
        """

        if binding is None:
            return self._stop_refusal_reason(record, error)
        try:
            effective_worker_rules(self.agent, record.named_agent_type, {})
        except (ValueError, PermissionError) as type_error:
            return self._stop_refusal_reason(record, type_error)
        return LAUNCH_POLICY_REVOKED

    def _scheduled_start_refusal(
        self, record: Any, binding: Any, now: datetime, limits: Any
    ) -> str | None:
        """Why a scheduled start may no longer become ready: its type is refused (its code), or its policy or definition changed."""

        try:
            definition, _ = self.launcher.validate_type(
                record.named_agent_type, automatic=True
            )
        except (ValueError, PermissionError) as exc:
            return self._stop_refusal_reason(record, exc)
        if (
            binding is None
            or not binding.policy.enabled
            or (binding.authority_mode == "runtime_default" and not limits.default_startup_policy_enabled)
            or launch_definition_digest(definition) != record.definition_digest
            or not definition_is_current(binding, definition)
            or (
                binding.policy.expires_at_utc is not None
                and binding.policy.expires_at_utc <= now
            )
        ):
            return LAUNCH_POLICY_REVOKED
        return None

    def _stop_refusal_reason(self, record: Any, error: BaseException) -> str:
        """The stop reason for a refused check (``launcher_refusal_reason``); the full error is logged when only its type is kept."""

        reason = launcher_refusal_reason(error)
        if refusal_code(error) is None:
            self.agent._logger.warning(
                "A starting worker is stopped because a check refused it with an error "
                "that is not a refusal code; only its type is kept as the reason. "
                "event=worker_launch_refused team_id=%s agent_type=%s launch_id=%s "
                "reason=%s error=%s",
                record.team_id,
                record.named_agent_type,
                record.launch_id,
                reason,
                error,
            )
        return reason

    def _log_launch_transition(
        self, record: Any, target: AgentLaunchState, evidence: str, reason: str
    ) -> None:
        """One log line per state change, so an unknown start is never silent.

        A start failed here also gets the one ``event=worker_start_failed``
        line every failed start gets (``log_failed_start``).
        """
        if target == AgentLaunchState.FAILED:
            log_failed_start(record, reason)
        if target == AgentLaunchState.UNKNOWN:
            self.agent._logger.warning(
                "A worker start's process cannot be checked, or the worker was asked to "
                "stop and has not ended yet, so its slot stays taken until it is proven "
                "gone. event=worker_launch_liveness_unknown launch_id=%s agent_type=%s "
                "schedule_id=%s host_id=%s evidence=%s reason=%s",
                record.launch_id,
                record.named_agent_type,
                record.schedule_id,
                record.host_id,
                evidence,
                reason,
            )
        elif record.state == AgentLaunchState.UNKNOWN:
            self.agent._logger.warning(
                "A worker start whose process could not be checked is now settled. "
                "event=worker_launch_unknown_settled launch_id=%s agent_type=%s "
                "schedule_id=%s host_id=%s state=%s reason=%s",
                record.launch_id,
                record.named_agent_type,
                record.schedule_id,
                record.host_id,
                target.value,
                reason,
            )

    async def _cover(
        self, binding: Any, row: dict[str, Any], now: datetime, limits: Any
    ) -> None:
        due, pause, expiry, lease = (
            _date(row.get(key))
            for key in (
                "next_fire_at",
                "paused_until",
                "expires_at_utc",
                "claim_expires_at",
            )
        )
        if (
            due is None
            or due > now
            or (pause and pause > now)
            or (expiry and expiry <= now)
            or (lease and lease > now)
        ):
            return
        definition, _ = self.launcher.validate_type(
            binding.target_agent_type, automatic=True
        )
        if binding.policy.expires_at_utc and binding.policy.expires_at_utc <= now:
            raise PermissionError(WORKER_POLICY_EXPIRED)
        if not definition_is_current(binding, definition):
            raise PermissionError(AGENT_DEFINITION_CHANGED)
        rules = effective_worker_rules(
            self.agent, binding.target_agent_type, binding.policy.rules
        )
        if due + timedelta(seconds=rules.claim_wait_seconds) > now:
            return
        schedule = await self.runtime.run(
            self.store.get_scheduled_message, binding.schedule_id
        )
        if not await self.runtime.run(
            partial(schedule_ready_for_admission, self.agent), schedule
        ):
            return
        occurrence = occurrence_identity(binding.schedule_id, row["next_fire_at"])
        if await self.runtime.run(
            self.store.worker_records.get_occurrence, occurrence, self.agent.team_id
        ):
            return
        attempt = (
            await self.runtime.run(
                self.store.worker_records.attempt, occurrence, self.agent.team_id
            )
            or {}
        )
        if attempt.get("launch_id"):
            launch = await self.runtime.run(
                self.launcher.repository.get, attempt["launch_id"], self.agent.team_id
            )
            if launch and launch.state in ACTIVE_LAUNCH_STATES:
                if launch.state == AgentLaunchState.UNKNOWN:
                    raise ValueError(LAUNCH_LIVENESS_UNKNOWN)
                if (
                    launch.ready_at_utc
                    and launch.ready_at_utc
                    + timedelta(seconds=rules.admission_timeout_seconds)
                    <= now
                ):
                    raise ValueError(WORKER_READY_BUT_UNCLAIMED)
                return
        if (_date(attempt.get("next_attempt_at")) or due) > now:
            return
        if int(attempt.get("attempts", 0)) >= rules.max_launch_attempts_per_occurrence:
            raise ValueError(WORKER_LAUNCH_ATTEMPTS_EXHAUSTED)
        workers = [
            status for status in self.agent._local_team_runtime.local_workers(self.agent.team_id)
            if status.named_agent_type == binding.target_agent_type
        ]
        if workers:
            if (
                any(status.has_execution_capacity for status in workers)
                or not rules.launch_when_busy
            ):
                deadline = due + timedelta(
                    seconds=rules.claim_wait_seconds + rules.admission_timeout_seconds
                )
                await self._block(
                    binding, EXISTING_WORKER_NOT_CLAIMING if now >= deadline else None
                )
                return
        ttl = rules.idle_ttl_seconds
        if ttl is None:
            from custom_agents.runtime_paths import agent_team_composition_path
            from custom_agents.team_composition import (
                ephemeral_idle_ttl_for_agent_type,
                load_team_composition,
            )

            path = agent_team_composition_path(self.agent)
            ttl = ephemeral_idle_ttl_for_agent_type(
                load_team_composition(path), binding.target_agent_type
            )
        # Give a cold worker its admission window before idle retirement.
        if ttl is not None:
            ttl = max(ttl, rules.admission_timeout_seconds)
        result = await self.runtime.run(
            self.launcher.launch,
            agent_type=binding.target_agent_type,
            owner_id=binding.owner_canonical_user_id,
            request_id=f"{occurrence}:{int(attempt.get('attempts', 0)) + 1}",
            source="schedule",
            binding=binding,
            occurrence_id=occurrence,
            idle_ttl_seconds=ttl,
        )
        await self.runtime.run(
            self.store.worker_records.record_attempt,
            occurrence_id=occurrence,
            schedule_id=binding.schedule_id,
            team_id=self.agent.team_id,
            launch_id=result.launch_id,
            next_attempt_at=now
            + timedelta(
                seconds=min(
                    rules.retry_backoff_seconds
                    * (2 ** int(attempt.get("attempts", 0))),
                    300,
                )
            ),
            reason=result.reason,
            increment=bool(result.launch_id),
        )
        if not result.accepted:
            raise ValueError(result.reason or WORKER_LAUNCH_UNAVAILABLE)
        await self._block(binding, None)
        self.agent._logger.info(
            "Scheduled worker startup requested",
            extra={
                "schedule_id": binding.schedule_id,
                "occurrence_id": occurrence,
                "launch_id": result.launch_id,
                "agent_id": result.agent_id,
            },
        )

    async def _refresh_definition_record(self, binding: Any) -> None:
        """Keep the definition record current; it is evidence, never a blocker."""
        try:
            await self.runtime.run(
                partial(refresh_definition_record, self.agent), binding
            )
        except Exception:
            self.agent._logger.exception(
                "Could not record the scheduled worker definition. "
                "event=schedule_worker_definition_record_failed schedule_id=%s",
                binding.schedule_id,
                extra={"schedule_id": binding.schedule_id, "team_id": self.agent.team_id},
            )

    async def _definition_change_summary(self, binding: Any) -> str:
        """Short description of a team definition change for the block warning."""
        try:
            definition = await self.runtime.run(
                self.launcher.resolve_definition, binding.target_agent_type
            )
            record = await self.runtime.run(
                self.store.worker_records.get_definition_record,
                binding.schedule_id,
                binding.team_id,
            )
            changes = definition_changes(binding, record, definition)
        except Exception:
            self.agent._logger.exception(
                "Could not compare the scheduled worker definition. "
                "event=schedule_worker_definition_compare_failed schedule_id=%s",
                binding.schedule_id,
            )
            return "unavailable"
        if not changes.saved_record_available:
            return "no saved copy of the earlier definition"
        fields = [
            name
            for name, changed in (
                ("role", changes.role_before != changes.role_after),
                ("own_prompt", changes.own_prompt_changed),
                ("include_shared_prompt", changes.include_shared_prompt_changed),
                ("backstory", changes.backstory_changed),
                ("skills_enabled", changes.skills_enabled_changed),
                ("source", changes.source_before != changes.source_after),
                (
                    "default_tools",
                    bool(changes.default_tools_added or changes.default_tools_removed),
                ),
            )
            if changed
        ]
        return ",".join(fields) or "none recorded"

    async def _block(self, binding: Any, reason: str | None) -> None:
        await self.runtime.run(
            self.store.worker_records.mark_blocked,
            binding.schedule_id,
            self.agent.team_id,
            reason,
        )
        if reason and reason != binding.blocked_reason:
            changed = (
                await self._definition_change_summary(binding)
                if reason == AGENT_DEFINITION_CHANGED
                else None
            )
            self.agent._logger.warning(
                "Scheduled worker needs review. schedule_id=%s policy_revision=%s "
                "reason=%s definition_changes=%s",
                binding.schedule_id,
                binding.revision,
                reason,
                changed,
                extra={
                    "schedule_id": binding.schedule_id,
                    "policy_revision": binding.revision,
                    "reason": reason,
                },
            )


async def reconcile_schedule_workers(agent: Any) -> None:
    if not getattr(agent, "is_team_lead", False) or agent._local_team_runtime is None:
        return
    try:
        await ScheduleWorkerController(agent).reconcile()
    except Exception:
        agent._logger.exception(
            "Schedule worker controller scan failed", extra={"team_id": agent.team_id}
        )


async def local_schedule_runtime_loop(agent: Any) -> None:
    """Host-owned startup supervision, independent of the lead's task/claim loop.

    The host facade supplies storage, configuration and authorization ports.
    Automatic launches use a separate service principal, never its current task.
    """
    import asyncio
    from custom_agents.local_schedule_policy import install_schedule_default
    from custom_agents.memory_review_cleanup_runtime import ReviewCleanupController

    from custom_agents.worker_pool_controller import reconcile_worker_pools

    review_cleanup = ReviewCleanupController(agent)
    if getattr(agent, "is_team_lead", False):
        from custom_agents.schedule_execution_digest_upgrade import (
            run_execution_digest_upgrade,
        )

        try:
            await run_execution_digest_upgrade(agent)
        except Exception:
            agent._logger.exception(
                "The one-time scheduled-worker fingerprint upgrade failed; affected "
                "schedules wait for review. event=schedule_execution_digest_upgrade_failed "
                "team_id=%s",
                agent.team_id,
            )
    while not agent.get_exit_event().is_set():
        try:
            rows = await agent.task_manager.operations_runtime.run(agent._schedule_store.get_all)
            for row in rows:
                try:
                    schedule = await agent.task_manager.operations_runtime.run(
                        agent._schedule_store.get_scheduled_message, row["schedule_id"]
                    )
                    if schedule and schedule.claim_team_id == agent.team_id:
                        await agent.task_manager.operations_runtime.run(partial(install_schedule_default, agent), schedule)
                except Exception:
                    agent._logger.exception("Could not configure default schedule startup. event=local_schedule_default_failed team_id=%s schedule_id=%s", agent.team_id, row["schedule_id"])
            await reconcile_schedule_workers(agent)
            # Delegated tasks waiting in worker queues: start workers, drain
            # surplus ones, and review problems (custom_agents.worker_pool_controller).
            await reconcile_worker_pools(agent)
            try:
                await review_cleanup.reconcile()
            except Exception:
                agent._logger.exception("Review cleanup scan failed. event=memory_review_cleanup_scan_failed team_id=%s", agent.team_id)
        except asyncio.CancelledError:
            raise
        except Exception:
            agent._logger.exception("Local schedule startup supervision failed. event=local_schedule_runtime_failed team_id=%s", agent.team_id)
        try:
            delay = current_worker_limits(agent).reconcile_interval_seconds
        except (ValueError, OSError):
            # A malformed config must not terminate the supervisor or launch.
            delay = 5.0
        await asyncio.sleep(delay)
