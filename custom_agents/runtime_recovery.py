"""Restart and role-takeover recovery helpers for UniversalAgent."""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from typing import Any, List, Optional

from custom_agents.agent_liveness import lookup_owner_status, owner_gone_by_launch
from custom_agents.liveness_observation import heartbeat_checks_deferred
from custom_agents.orphaned_subtasks import stop_left_out_delegations
from custom_agents.task_human_interaction_blockers import (
    has_pending_human_interaction,
)
from custom_agents.task_manager import TaskInfo
from custom_messages.contracts.runtime_v1 import TEAM_ARCHITECT_AGENT_TYPE
from custom_messages.enums import ProgressState, TaskHistoryImportance, TaskHistoryType
from storage.runtime_state_repository import RuntimeRecoveryConflict


def should_fail_stale_executing_recovery(
    agent: Any,
    task_info: TaskInfo,
    now_ts: str,
) -> tuple[bool, Optional[float]]:
    grace_seconds = agent._recovery_retry_deadline_seconds()
    if grace_seconds is None:
        return False, None
    snapshot_ts = agent._parse_iso_timestamp(task_info.restored_runtime_updated_at)
    now_dt = agent._parse_iso_timestamp(now_ts)
    if snapshot_ts is None or now_dt is None:
        return False, None
    age_seconds = max(0.0, (now_dt - snapshot_ts).total_seconds())
    return age_seconds > grace_seconds, age_seconds


async def fail_recovered_executing_task(
    agent: Any,
    task_info: TaskInfo,
    *,
    now_ts: str,
    reason: str,
    snapshot_age_seconds: Optional[float],
) -> None:
    task_id = task_info.task.task_id
    details = {
        "reason": reason,
        "restart_executing_task_grace_seconds": agent._recovery_retry_deadline_seconds(),
        "runtime_snapshot_updated_at": task_info.restored_runtime_updated_at,
    }
    if snapshot_age_seconds is not None:
        details["snapshot_age_seconds"] = round(snapshot_age_seconds, 3)
    await agent.task_manager.operations_runtime.run(
        agent.task_manager.append_history_entry,
        task_id,
        reason,
        entry_type=TaskHistoryType.ERROR_EVENT,
        importance=TaskHistoryImportance.CRITICAL,
        timestamp=now_ts,
        details=details,
    )
    await agent._finalize_conversation_turn(
        task_info, task_status=ProgressState.FAILED.value
    )
    await agent.task_manager.operations_runtime.run(
        agent.task_manager.fail_task, task_id, reason, now_ts
    )
    try:
        await agent._send_task_error_response(
            task_info.task,
            task_info.header,
            task_info.meta,
            reason,
        )
    except Exception as exc:
        agent._logger.warning(
            f"[{agent.id}] Failed to send stale-recovery error response for task {task_id}: {exc}"
        )


async def tell_task_about_its_restarts(agent: Any, task_id: str, now_ts: str) -> None:
    """Tell an interrupted task about manager restarts it asked for.

    A restart TeamArchitect asked for can end its own task before the agent
    reads the result, and the task's conversation is not kept across the
    restart. Without this, the resumed task sees only the person's message
    (for example "approve the restart") and asks for the same restart again,
    in a loop. Receipts name the task (``runtime_restart_service``); the
    directive is read at the task's next turn.
    """

    if getattr(agent, "named_agent_type", None) != TEAM_ARCHITECT_AGENT_TYPE:
        return
    from custom_agents.runtime_restart_service import (
        restart_resume_directive,
        restarts_dispatched_for_task,
    )

    try:
        restarts = await asyncio.to_thread(restarts_dispatched_for_task, agent, task_id)
    except Exception:
        agent._logger.exception(
            "Could not check which restarts an interrupted task asked for. "
            "event=restart_outcome_lookup_failed task_id=%s",
            task_id,
        )
        return
    if not restarts:
        return
    change_ids = ", ".join(item.change_id for item in restarts)
    await agent.task_manager.operations_runtime.run(
        agent.task_manager.append_runtime_resume_directive,
        task_id,
        restart_resume_directive(restarts),
    )
    await agent.task_manager.operations_runtime.run(
        agent.task_manager.append_history_entry,
        task_id,
        f"The manager restarted for the restart this task asked for ({change_ids}). "
        "The task resumes knowing the restart is done.",
        entry_type=TaskHistoryType.TASK_EVENT,
        importance=TaskHistoryImportance.HIGH,
        timestamp=now_ts,
    )
    agent._logger.info(
        "Told an interrupted task about the restarts it asked for. event=restart_outcome_recorded "
        "task_id=%s change_ids=%s restart_count=%s",
        task_id,
        change_ids,
        len(restarts),
    )


async def filter_stale_role_takeover_owner_ids(
    agent: Any, owner_agent_ids: List[str]
) -> List[str]:
    stale_owner_ids: List[str] = []
    now = datetime.now(timezone.utc)
    grace_seconds = agent._role_takeover_liveness_grace_seconds()

    for owner_agent_id in owner_agent_ids:
        if not owner_agent_id or owner_agent_id == agent.id:
            continue
        # A launch record decides when the owner has one: a heartbeat can be
        # stale while a slow worker still runs (custom_agents.agent_liveness).
        gone = owner_gone_by_launch(agent, owner_agent_id)
        if gone is True:
            stale_owner_ids.append(owner_agent_id)
            continue
        if gone is False:
            agent._logger.info(
                "Skipping role takeover: the owner's worker start has not ended. "
                "event=role_takeover_owner_launch_live owner_agent_id=%s",
                owner_agent_id,
            )
            continue
        owner_status = lookup_owner_status(agent, owner_agent_id)
        if owner_status is not None and owner_status.is_exiting:
            stale_owner_ids.append(owner_agent_id)
            continue
        if heartbeat_checks_deferred(agent):
            continue
        if owner_status is None:
            stale_owner_ids.append(owner_agent_id)
            continue
        last_seen_at = agent._parse_iso_timestamp(owner_status.last_seen_at)
        if last_seen_at is None:
            stale_owner_ids.append(owner_agent_id)
            continue
        age_seconds = (now - last_seen_at).total_seconds()
        if age_seconds > grace_seconds:
            stale_owner_ids.append(owner_agent_id)
            continue
        agent._logger.info(
            f"[{agent.id}] Skipping role takeover for live owner {owner_agent_id}; "
            f"last_seen_at={owner_status.last_seen_at} age={age_seconds:.1f}s."
        )
    return stale_owner_ids


async def discard_runtime_state_for_team_change(
    agent: Any, previous_team_id: str
) -> None:
    restored_task_infos = [
        task_info
        for task_info in await agent.task_manager.operations_runtime.run(
            agent.task_manager.get_top_level_task_infos,
        )
        if task_info.task.parent_task_id is None
    ]
    if not restored_task_infos:
        return

    error_message = (
        f"Discarded after restart because agent team_id changed from "
        f"{previous_team_id or 'unknown'} to {agent.team_id or 'unknown'}."
    )

    for task_info in restored_task_infos:
        now_ts = datetime.now(timezone.utc).isoformat()
        await agent.task_manager.operations_runtime.run(
            agent.task_manager.append_history_entry,
            task_info.task.task_id,
            error_message,
            entry_type=TaskHistoryType.ERROR_EVENT,
            importance=TaskHistoryImportance.CRITICAL,
            timestamp=now_ts,
            details={
                "previous_team_id": previous_team_id,
                "current_team_id": agent.team_id,
            },
        )
        await agent._finalize_conversation_turn(
            task_info, task_status=ProgressState.FAILED.value
        )
        await agent.task_manager.operations_runtime.run(
            agent.task_manager.fail_task, task_info.task.task_id, error_message, now_ts
        )
        try:
            await agent._send_task_error_response(
                task_info.task,
                task_info.header,
                task_info.meta,
                error_message,
            )
        except Exception as exc:
            agent._logger.warning(
                f"[{agent.id}] Failed to send team-change discard response for task "
                f"{task_info.task.task_id}: {exc}"
            )


async def _preserve_or_requeue_recovered_pause(
    agent: Any,
    *,
    task_id: str,
    now_ts: str,
) -> None:
    from functools import partial
    from custom_agents.runtime_task_controls import has_explicit_queue_pause

    if await agent.task_manager.operations_runtime.run(
        partial(has_explicit_queue_pause, agent.task_manager), task_id,
    ):
        return
    pending_guidance = await agent.task_manager.operations_runtime.run(
        agent.task_manager.get_pending_recoverable_pause, task_id
    ) or await agent.task_manager.operations_runtime.run(
        agent.task_manager.is_waiting_on_task_query, task_id
    )
    if pending_guidance:
        await agent.task_manager.operations_runtime.run(
            agent.task_manager.append_history_entry,
            task_id,
            "Recovery preserved the operator-guided pause; the task will not "
            "run until its exact query is resolved.",
            entry_type=TaskHistoryType.TASK_EVENT,
            importance=TaskHistoryImportance.NORMAL,
            timestamp=now_ts,
        )
        return
    await agent.task_manager.operations_runtime.run(
        agent.task_manager.append_history_entry,
        task_id,
        "Recovery found a paused task without a pending query and re-queued "
        "it as an anomalous orphaned pause.",
        entry_type=TaskHistoryType.TASK_EVENT,
        importance=TaskHistoryImportance.HIGH,
        timestamp=now_ts,
    )
    await agent.task_manager.operations_runtime.run(
        agent.task_manager.update_main_task_state,
        task_id,
        ProgressState.ACCEPTED,
        now_ts,
    )
    await agent.task_manager.operations_runtime.run(
        agent.task_manager.enqueue_task, task_id
    )


class RuntimeRecoveryStartFailed(RuntimeError):
    """Restoring this agent's own saved work conflicted twice at start; the agent does not start.

    ``commit_task_runtime_recovery`` refuses a recovery whose saved work
    changed after it was read, or one that would adopt a task another agent
    claimed meanwhile (``RuntimeRecoveryConflict``). The whole restore is read
    again once (``_restore_with_one_retry``). A second conflict while taking
    over gone agents' work starts without that work; a second conflict on the
    agent's own saved work stops the start: starting without it could leave
    it unrun, or let the agent's first save replace it.
    """

    def __init__(self, agent_id: str, team_id: Optional[str], named_agent_type: Optional[str]) -> None:
        super().__init__(
            f"Agent {agent_id} ({named_agent_type}, team {team_id}) could not restore its own saved work: "
            "it changed twice while it was being restored."
        )
        self.agent_id = agent_id
        self.team_id = team_id
        self.named_agent_type = named_agent_type


async def _restore_saved_tasks(
    agent: Any, same_role_takeover_enabled: bool, *, last_attempt: bool = False
) -> Optional[dict[str, Any]]:
    """Adopt the saved work of gone agents of this type, else restore this agent's own.

    On the last attempt a conflict is final: a contested takeover is skipped
    (``event=role_takeover_lost_race``) and a conflict on the agent's own work
    stops the start (``RuntimeRecoveryStartFailed``).
    """
    restored_runtime = None
    if same_role_takeover_enabled:
        restored_runtime = await _take_over_gone_owners(agent, last_attempt=last_attempt)
    if not restored_runtime:
        try:
            restored_runtime = await agent.task_manager.operations_runtime.run(
                agent.task_manager.restore_runtime_state,
            )
        except RuntimeRecoveryConflict as conflict:
            if not last_attempt:
                raise
            agent._logger.error(
                "This agent's own saved work changed again while it was restored; the agent does not start. "
                "event=runtime_recovery_conflict_start_failed agent_id=%s team_id=%s named_agent_type=%s error=%s",
                agent.id,
                agent.team_id,
                agent.named_agent_type,
                conflict,
            )
            raise RuntimeRecoveryStartFailed(agent.id, agent.team_id, agent.named_agent_type) from conflict
    return restored_runtime


async def _take_over_gone_owners(agent: Any, *, last_attempt: bool) -> Optional[dict[str, Any]]:
    """Adopt the saved work of gone agents of this type; a final conflict starts without it.

    That work stays saved with whoever holds it now, and the lead's
    replacement pass adopts it if that holder is gone
    (``orphaned_work_takeover``); a startup agent that stopped here would stop
    the whole runtime.
    """
    role_runtime_candidates = (
        agent.task_manager.runtime_state_store.peek_task_runtime_by_role(
            agent.team_id,
            agent.named_agent_type,
        )
        if agent.team_id and agent.named_agent_type
        else []
    )
    stale_owner_ids = await filter_stale_role_takeover_owner_ids(
        agent,
        [row.owner_agent_id for row in role_runtime_candidates],
    )
    try:
        return await agent.task_manager.operations_runtime.run(
            agent.task_manager.restore_runtime_state_for_owner_ids, stale_owner_ids
        )
    except RuntimeRecoveryConflict as conflict:
        if not last_attempt:
            raise
        agent._logger.error(
            "Saved work of gone agents changed again while this agent took it over; starting without it. "
            "event=role_takeover_lost_race agent_id=%s team_id=%s owner_agent_ids=%s error=%s",
            agent.id,
            agent.team_id,
            stale_owner_ids,
            conflict,
        )
        return None


async def _restore_with_one_retry(agent: Any, same_role_takeover_enabled: bool) -> Optional[dict[str, Any]]:
    """Restore saved work; after a custody conflict, read the whole restore again once.

    Startup did not handle ``RuntimeRecoveryConflict`` before 2026-10-17: a
    spawned worker's launch failed, and a startup agent's or the lead's process
    exited, every later start the same way. A failed restore leaves nothing
    adopted, and the second read sees the new holders and leaves their tasks
    out (``RuntimeSnapshotCoordinator._taken_by_other_agents``). A conflict on
    the second read is final (``_restore_saved_tasks``): a contested takeover
    is skipped, a contested restore of the agent's own work stops the start.
    """
    try:
        return await _restore_saved_tasks(agent, same_role_takeover_enabled)
    except RuntimeRecoveryConflict as first:
        agent._logger.warning(
            "Saved work changed while this agent adopted it; reading it again. "
            "event=runtime_recovery_conflict_retrying agent_id=%s team_id=%s named_agent_type=%s error=%s",
            agent.id,
            agent.team_id,
            agent.named_agent_type,
            first,
        )
    return await _restore_saved_tasks(agent, same_role_takeover_enabled, last_attempt=True)


async def recover_runtime_after_restart(agent: Any) -> None:
    stored_runtime_team_id = await agent.task_manager.operations_runtime.run(
        agent.task_manager.get_stored_runtime_team_id,
    )
    stored_deferred_team_id = await agent.deferred_inbox.stored_team_id()
    same_role_takeover_enabled = bool(
        getattr(agent, "same_role_runtime_takeover_enabled", True)
    )

    restored_deferred = False

    if stored_runtime_team_id and stored_runtime_team_id != agent.team_id:
        restored_runtime = await agent.task_manager.operations_runtime.run(
            agent.task_manager.restore_runtime_state,
        )
        if restored_runtime:
            await discard_runtime_state_for_team_change(agent, stored_runtime_team_id)
    else:
        restored_runtime = await _restore_with_one_retry(agent, same_role_takeover_enabled)
        if restored_runtime:
            await stop_left_out_delegations(agent, restored_runtime.get("left_out_delegations") or [])

    if stored_deferred_team_id and stored_deferred_team_id != agent.team_id:
        agent._logger.warning(
            f"[{agent.id}] Discarding deferred messages from prior team "
            f"{stored_deferred_team_id} after restart into {agent.team_id}."
        )
        await agent.deferred_inbox.clear()
    else:
        if same_role_takeover_enabled:
            restored_deferred = await agent.deferred_inbox.restore_role(
                stale_owner_filter=lambda owner_ids: (
                    filter_stale_role_takeover_owner_ids(agent, owner_ids)
                ),
            )
        if not restored_deferred:
            restored_deferred = await agent.deferred_inbox.restore()

    if not restored_runtime and not restored_deferred:
        return

    now_ts = datetime.now(timezone.utc).isoformat()
    for task_info in await agent.task_manager.operations_runtime.run(
        agent.task_manager.get_locally_executable_task_infos,
    ):
        task_id = task_info.task.task_id
        # The supervisor can stop us after another tool has moved this task
        # into a wait, or after recovery has re-queued it. Its saved state
        # does not tell us whether it asked for a restart.
        await tell_task_about_its_restarts(agent, task_id, now_ts)

        if await agent.task_manager.operations_runtime.run(
            agent.task_manager.is_executing_state, task_info.state
        ):
            await agent.task_manager.operations_runtime.run(
                agent.task_manager.append_history_entry,
                task_id,
                "Agent restarted while this task was executing. Attempting recovery from the last durable state.",
                entry_type=TaskHistoryType.TASK_EVENT,
                importance=TaskHistoryImportance.HIGH,
                timestamp=now_ts,
            )
            should_fail, snapshot_age_seconds = should_fail_stale_executing_recovery(
                agent,
                task_info,
                now_ts,
            )
            if should_fail:
                error_message = (
                    "Agent restarted while this task was executing, and the persisted execution snapshot "
                    f"was stale for {snapshot_age_seconds:.1f}s. Recovery failed the task instead of "
                    "retrying the same in-flight tool call."
                )
                await fail_recovered_executing_task(
                    agent,
                    task_info,
                    now_ts=now_ts,
                    reason=error_message,
                    snapshot_age_seconds=snapshot_age_seconds,
                )
                continue
            if await agent.task_manager.operations_runtime.run(
                agent.task_manager.has_pending_subtasks, task_id
            ) or await agent.task_manager.operations_runtime.run(
                agent.task_manager.is_waiting_on_task_query, task_id
            ):
                await agent.task_manager.operations_runtime.run(
                    agent.task_manager.append_history_entry,
                    task_id,
                    "Recovery detected external dependencies still pending; task returned to WAITING.",
                    entry_type=TaskHistoryType.STATE_CHANGE,
                    importance=TaskHistoryImportance.NORMAL,
                    timestamp=now_ts,
                )
                await agent.task_manager.operations_runtime.run(
                    agent.task_manager.update_main_task_state,
                    task_id,
                    ProgressState.WAITING,
                    now_ts,
                )
            else:
                await agent.task_manager.operations_runtime.run(
                    agent.task_manager.append_history_entry,
                    task_id,
                    "Recovery re-queued the interrupted task to retry from the latest durable checkpoint.",
                    entry_type=TaskHistoryType.TASK_EVENT,
                    importance=TaskHistoryImportance.HIGH,
                    timestamp=now_ts,
                )
                await agent.task_manager.operations_runtime.run(
                    agent.task_manager.update_main_task_state,
                    task_id,
                    ProgressState.ACCEPTED,
                    now_ts,
                )
                await agent.task_manager.operations_runtime.run(
                    agent.task_manager.enqueue_task, task_id, front=True
                )
            continue

        if task_info.state == ProgressState.PAUSED:
            await _preserve_or_requeue_recovered_pause(
                agent,
                task_id=task_id,
                now_ts=now_ts,
            )
            continue

        if await agent.task_manager.operations_runtime.run(
            agent.task_manager.is_blocked_state, task_info.state
        ):
            if (
                has_pending_human_interaction(agent.task_manager, task_id)
                or await agent.task_manager.operations_runtime.run(
                    agent.task_manager.has_pending_subtasks, task_id
                )
                or await agent.task_manager.operations_runtime.run(
                    agent.task_manager.is_waiting_on_task_query, task_id
                )
            ):
                continue
            await agent.task_manager.operations_runtime.run(
                agent.task_manager.append_history_entry,
                task_id,
                "Recovery found no remaining blockers for this waiting task; it was re-queued to resume.",
                entry_type=TaskHistoryType.TASK_EVENT,
                importance=TaskHistoryImportance.NORMAL,
                timestamp=now_ts,
            )
            await agent.task_manager.operations_runtime.run(
                agent.task_manager.update_main_task_state,
                task_id,
                ProgressState.ACCEPTED,
                now_ts,
            )
            await agent.task_manager.operations_runtime.run(
                agent.task_manager.enqueue_task, task_id, front=True
            )
            continue

        if await agent.task_manager.operations_runtime.run(
            agent.task_manager.is_runnable_state, task_info.state
        ):
            await agent.task_manager.operations_runtime.run(
                agent.task_manager.enqueue_task, task_id
            )
