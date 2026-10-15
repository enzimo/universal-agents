"""Deterministic coverage of overdue schedules, independent of the lead's model."""

from __future__ import annotations

from datetime import datetime, timedelta
from functools import partial
from typing import Any

from custom_agents.agent_instantiation_service import (
    AgentInstantiationService,
    current_worker_limits,
    effective_worker_rules,
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
)
from custom_agents.schedule_readiness import (
    effective_requirements,
    prepare_schedule_access,
)
from custom_messages.contracts.schedule_worker_v1 import (
    AgentLaunchState,
    occurrence_identity,
    utc_now,
)
from storage.agent_launch_repository import ACTIVE_LAUNCH_STATES, LaunchConflict


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
            schedule.schedule_id, agent.team_id, str(exc)
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
                await self._block(binding, str(exc))
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
                binding = (
                    self.store.worker_records.get_binding(
                        record.schedule_id, team_id=self.agent.team_id
                    )
                    if record.schedule_id
                    else None
                )
                revoked = False
                startup_timeout = limits.defaults.startup_timeout_seconds
                try:
                    startup_timeout = effective_worker_rules(
                        self.agent,
                        record.named_agent_type,
                        binding.policy.rules if binding else {},
                    ).startup_timeout_seconds
                except (ValueError, PermissionError):
                    revoked = True
                if record.source == "schedule":
                    try:
                        definition, _ = self.launcher.validate_type(
                            record.named_agent_type, automatic=True
                        )
                        revoked = revoked or (
                            binding is None
                            or not binding.policy.enabled
                            or (binding.authority_mode == "runtime_default" and not limits.default_startup_policy_enabled)
                            or launch_definition_digest(definition)
                            != record.definition_digest
                            or not definition_is_current(binding, definition)
                            or (
                                binding.policy.expires_at_utc is not None
                                and binding.policy.expires_at_utc <= now
                            )
                        )
                    except (ValueError, PermissionError):
                        revoked = True
                if (
                    revoked
                    or record.created_at_utc + timedelta(seconds=startup_timeout) <= now
                ):
                    stopped = await self.runtime.run(
                        self.agent._local_team_runtime.stop_launch, record
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
                try:
                    await self.runtime.run(
                        self.launcher.repository.update,
                        record,
                        state=target,
                        reason="launcher_reconciliation",
                    )
                except LaunchConflict:
                    # The child can publish readiness or exit while inspected.
                    self.agent._logger.info(
                        "Launch state changed during reconciliation",
                        extra={"launch_id": record.launch_id},
                    )
                    continue
                self._log_launch_transition(record, target, evidence)

    def _log_launch_transition(
        self, record: Any, target: AgentLaunchState, evidence: str
    ) -> None:
        """One log line per state change, so an unknown start is never silent."""
        if target == AgentLaunchState.UNKNOWN:
            self.agent._logger.warning(
                "A worker start's process cannot be checked, so its slot stays taken "
                "until its process is proven gone. event=worker_launch_liveness_unknown "
                "launch_id=%s agent_type=%s schedule_id=%s host_id=%s evidence=%s",
                record.launch_id,
                record.named_agent_type,
                record.schedule_id,
                record.host_id,
                evidence,
            )
        elif record.state == AgentLaunchState.UNKNOWN:
            self.agent._logger.warning(
                "A worker start whose process could not be checked is now settled. "
                "event=worker_launch_unknown_settled launch_id=%s agent_type=%s "
                "schedule_id=%s host_id=%s state=%s",
                record.launch_id,
                record.named_agent_type,
                record.schedule_id,
                record.host_id,
                target.value,
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
