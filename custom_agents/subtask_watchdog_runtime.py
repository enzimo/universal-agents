"""Subtask watchdog and reconciliation helpers for UniversalAgent."""

from __future__ import annotations

from custom_agents.execution_lanes_runtime import lane_of
from custom_agents.strands_parts import task_strands_messages
from custom_tools.runtime_context import task_work
from custom_messages.records.task_results import TaskResultUpdate

import asyncio
from datetime import datetime, timezone
from functools import partial
from typing import Any, List, Optional

from custom_agents.control_runtime import handle_interrupted_turn
from custom_agents.turn_failure_runtime import settle_failed_turn
from custom_agents.conversation_runtime import finalize_conversation_turn
from custom_agents.pending_work_runtime import schedule_pending_work
from custom_agents.task_manager import TaskInfo
from storage.delegated_task_repository import DELEGATED_QUEUED
from custom_messages.contracts.task_v1 import Task, TaskQuery, TaskStatus
from id_generation import new_id
from custom_messages.enums import (
    ExecutionPlanItemStatus,
    ExecutionUnknownStatus,
    MessageMetaType,
    ProgressState,
    RecoverablePauseTrigger,
    RuntimeBudgetPolicyMode,
    TaskHistoryImportance,
    TaskHistoryType,
)


SUBTASK_TIMEOUT_PROBE_THRESHOLDS: tuple[float, ...] = (0.25, 0.50, 0.75, 0.90, 0.95)
SUBTASK_TIMEOUT_PROBE_ACTION = "delegated_subtask_timeout_probe"


async def _resume_parent_task_if_ready(self: Any, parent_task_id: str) -> None:
    """Resume the parent once its subtasks finished, as that task's handling (``runtime_context.task_work``)."""

    with task_work(parent_task_id):
        await _resume_parent_task_when_ready(self, parent_task_id)


async def _resume_parent_task_when_ready(self: Any, parent_task_id: str) -> None:
    parent_task_info = await self.task_manager.operations_runtime.run(
        self.task_manager.get_task_info, parent_task_id
    )
    if not parent_task_info:
        self._logger.warning(
            f"[{self.id}] Parent task {parent_task_id} missing during subtask recovery."
        )
        return
    root_task_id = (
        await self.task_manager.operations_runtime.run(
            self.task_manager.resolve_root_task_id, parent_task_id
        )
        or parent_task_id
    )
    active_lease = await self.task_manager.operations_runtime.run(
        self.task_manager.get_active_runtime_lease, root_task_id
    )
    if active_lease is not None:
        self._logger.info(
            "[%s] Parent task %s has active runtime lease %s; deferring resume.",
            self.id,
            parent_task_id,
            active_lease.lease_id,
        )
        return

    now_ts = datetime.now(timezone.utc).isoformat()
    if await self.task_manager.operations_runtime.run(
        self.task_manager.has_pending_subtasks, parent_task_id
    ):
        self._logger.info(
            f"[{self.id}] Parent task {parent_task_id} still has pending subtasks. Waiting."
        )
        if parent_task_info.state != ProgressState.WAITING:
            await self.task_manager.operations_runtime.run(
                self.task_manager.update_main_task_state,
                parent_task_id,
                ProgressState.WAITING,
                now_ts,
            )
        return

    self._logger.info(
        f"[{self.id}] All subtasks for parent task {parent_task_id} are complete. Resuming processing."
    )
    if not await self.task_manager.operations_runtime.run(
        self.task_manager.update_main_task_state,
        parent_task_id,
        ProgressState.IN_PROGRESS,
        now_ts,
    ):
        self._logger.error(
            f"[{self.id}] Failed to set parent task {parent_task_id} back to IN_PROGRESS."
        )

    try:
        await self._refresh_self_agent_state()
        result = await self.process_llm_interaction(
            msg_content=parent_task_info.task,
            msg_header=parent_task_info.header,
            msg_meta=parent_task_info.meta,
        )
        if (
            isinstance(result, Task)
            and result.answer
            and not await self.task_manager.operations_runtime.run(
                self.task_manager.has_pending_subtasks, parent_task_id
            )
            and not await self.task_manager.operations_runtime.run(
                self.task_manager.is_final_review_pending, parent_task_id
            )
        ):
            if (
                await self.task_manager.operations_runtime.run(
                    self.task_manager.get_task_info, parent_task_id
                )
                is not None
            ):
                parent_task_info = await self.task_manager.operations_runtime.run(
                    self.task_manager.update_task_result,
                    parent_task_info.task.task_id,
                    TaskResultUpdate(answer=result.answer),
                )
                if parent_task_info is None:
                    return
                merged_errors = list(parent_task_info.task.errors or [])
                for error in list(result.errors or []):
                    if error not in merged_errors:
                        merged_errors.append(error)
                parent_task_info = await self.task_manager.operations_runtime.run(
                    self.task_manager.update_task_result,
                    parent_task_info.task.task_id,
                    TaskResultUpdate(errors=merged_errors or None),
                )
                if parent_task_info is None:
                    return
                if result.task_plan:
                    await self.task_manager.operations_runtime.run(
                        self.task_manager.set_task_plan,
                        parent_task_id,
                        result.task_plan,
                        agent_name=self.name,
                        agent_id=self.id,
                        rationale="Updated from resumed parent-task execution result.",
                    )
                now_ts = datetime.now(timezone.utc).isoformat()
                await finalize_conversation_turn(
                    conversation_store=self._conversation_store,
                    conversation_compactor=self._conversation_compactor,
                    conversation_artifact_offloader=self._conversation_artifact_offloader,
                    strands_messages=task_strands_messages(parent_task_info.task.task_id),
                    task_manager=self.task_manager,
                    task_info=parent_task_info,
                    task_status=ProgressState.COMPLETED.value,
                    logger=self._logger,
                    agent_id=self.id,
                    team_id=self.team_id,
                    named_agent_type=self.named_agent_type,
                    summary_generator=self._conversation_summary_generator,
                )
                await self._notify_context_after_turn(
                    task_info=parent_task_info,
                    task_status=ProgressState.COMPLETED.value,
                )
                await self._complete_task_and_schedule_aar_review(
                    parent_task_id, now_ts
                )
    except asyncio.CancelledError:
        # Runtime shutdown cancels this owner; a user stop cancels its child turn.
        if (owner := asyncio.current_task()) is not None and owner.cancelling():
            raise
        await handle_interrupted_turn(
            self, task_id=parent_task_id, task_info=parent_task_info
        )
    except Exception as e:
        # The one failed-turn handling (turn_failure_runtime): a hand-off is
        # logged, anything else fails the task and tells its requester, and a
        # wait-limit stop also tells the person. Before 2026-10-19 this path
        # failed the task itself, so a failover hand-off was failed too.
        await settle_failed_turn(self, parent_task_id, parent_task_info, e)
    finally:
        await self._refresh_self_agent_state()


async def _fail_subtask_and_reconcile_parent(
    self,
    *,
    parent_task_id: str,
    subtask_info: TaskInfo,
    error_message: str,
    resume_parent: bool = False,
    synthetic_context: Optional[str] = None,
    from_runtime_mailbox: bool = False,
    runtime_event_id: Optional[str] = None,
) -> None:
    now_ts = datetime.now(timezone.utc).isoformat()
    if subtask_info.state in {
        ProgressState.COMPLETED,
        ProgressState.FAILED,
        ProgressState.CANCELLED,
        ProgressState.ABANDONED,
    }:
        return

    if not from_runtime_mailbox and await self.task_manager.operations_runtime.run(
        self._runtime_coordinator().defer_subtask_failure_if_blocked,
        parent_task_id=parent_task_id,
        subtask_id=subtask_info.task.task_id,
        error_message=error_message,
        resume_parent=resume_parent,
        synthetic_context=synthetic_context,
    ):
        return

    transitioned = await self.task_manager.operations_runtime.run(
        self.task_manager.try_transition_task_state,
        subtask_info.task.task_id,
        allowed_from={
            ProgressState.ACCEPTED,
            ProgressState.IN_PROGRESS,
            ProgressState.WAITING,
            ProgressState.PAUSED,
            ProgressState.PARTIAL,
            ProgressState.RETRYING,
            ProgressState.UNDEFINED,
        },
        to_state=ProgressState.FAILED,
        timestamp=now_ts,
        reason=error_message,
        event_id=runtime_event_id,
        source="subtask_recovery",
    )
    if not transitioned:
        self._logger.info(
            "[%s] event=subtask_failure_reconciliation_skipped subtask_id=%s parent_task_id=%s "
            "state=%s runtime_event_id=%s",
            self.id,
            subtask_info.task.task_id,
            parent_task_id,
            subtask_info.state.value,
            runtime_event_id,
        )
        return

    subtask_info = (
        await self.task_manager.operations_runtime.run(
            self.task_manager.get_task_info, subtask_info.task.task_id
        )
        or subtask_info
    )
    subtask_info = await self.task_manager.operations_runtime.run(
        self.task_manager.update_task_result,
        subtask_info.task.task_id,
        TaskResultUpdate(
            errors=list(
                dict.fromkeys([*(subtask_info.task.errors or []), error_message])
            )
        ),
    )
    if subtask_info is None:
        return
    await self.task_manager.operations_runtime.run(
        self.task_manager.append_history_entry,
        subtask_info.task.task_id,
        error_message,
        entry_type=TaskHistoryType.ERROR_EVENT,
        importance=TaskHistoryImportance.CRITICAL,
        timestamp=now_ts,
        agent_name=getattr(subtask_info.meta, "to_agent_name", None),
        related_task_id=parent_task_id,
        details={
            "event": "subtask_failure_reconciled",
            "runtime_event_id": runtime_event_id,
            "resume_parent": resume_parent,
        },
    )
    await self.task_manager.operations_runtime.run(
        self.task_manager.bubble_subtask_result,
        parent_task_id,
        subtask_info.task.task_id,
    )
    parent_task_info = await self.task_manager.operations_runtime.run(
        self.task_manager.get_task_info, parent_task_id
    )
    if not parent_task_info:
        return

    if resume_parent:
        synthetic_summary = synthetic_context or (
            f"Delegated subtask {subtask_info.task.task_id} was marked failed; resume parent task."
        )
        await self.task_manager.operations_runtime.run(
            self.task_manager.append_history_entry,
            parent_task_id,
            synthetic_summary,
            entry_type=TaskHistoryType.SUBTASK_EVENT,
            importance=TaskHistoryImportance.CRITICAL,
            timestamp=now_ts,
            agent_name=self.name,
            agent_id=self.id,
            related_task_id=subtask_info.task.task_id,
            details={
                "action": "resume_parent_after_delegated_subtask_timeout",
                "subtask_id": subtask_info.task.task_id,
                "error_message": error_message,
            },
        )
        if getattr(subtask_info.task, "failure", None) is not None:
            from custom_agents.delegation_wake import wake_for_failed_delegation

            if await wake_for_failed_delegation(
                self, parent_task_id=parent_task_id, subtask_info=subtask_info
            ):
                return
        await self._resume_parent_task_if_ready(parent_task_id)
        return

    self._logger.warning(
        f"[{self.id}] {error_message} Failing parent task {parent_task_id}."
    )
    await finalize_conversation_turn(
        conversation_store=self._conversation_store,
        conversation_compactor=self._conversation_compactor,
        conversation_artifact_offloader=self._conversation_artifact_offloader,
        strands_messages=task_strands_messages(parent_task_info.task.task_id),
        task_manager=self.task_manager,
        task_info=parent_task_info,
        task_status=ProgressState.FAILED.value,
        logger=self._logger,
        agent_id=self.id,
        team_id=self.team_id,
        named_agent_type=self.named_agent_type,
        summary_generator=self._conversation_summary_generator,
    )
    await self._notify_context_after_turn(
        task_info=parent_task_info,
        task_status=ProgressState.FAILED.value,
    )
    await self.task_manager.operations_runtime.run(
        self.task_manager.fail_task, parent_task_id, error_message, now_ts
    )
    await self._send_task_error_response(
        parent_task_info.task,
        parent_task_info.header,
        parent_task_info.meta,
        error_message,
    )


async def _prepare_local_task_finalization_before_dispatch(
    self,
    *,
    incoming_task: Task,
    outgoing_task: Task,
) -> bool:
    """
    Record a terminal answer and enter FINALIZING_PLAN before it is emitted.

    Completion and archival happen only after the terminal Task response has
    been sent and any optional non-blocking follow-up context has been
    handled.
    """
    task_info = await self.task_manager.operations_runtime.run(
        self.task_manager.get_task_info, incoming_task.task_id
    )
    if task_info is None:
        return False

    now_ts = datetime.now(timezone.utc).isoformat()
    task_info = await self.task_manager.operations_runtime.run(
        self.task_manager.update_task_result,
        task_info.task.task_id,
        TaskResultUpdate(answer=outgoing_task.answer, errors=outgoing_task.errors),
    )
    if task_info is None:
        return False
    if outgoing_task.task_plan:
        await self.task_manager.operations_runtime.run(
            self.task_manager.set_task_plan,
            incoming_task.task_id,
            outgoing_task.task_plan,
            agent_name=self.name,
            agent_id=self.id,
            rationale="Updated from terminal task response before plan finalization.",
        )

    await finalize_conversation_turn(
        conversation_store=self._conversation_store,
        conversation_compactor=self._conversation_compactor,
        conversation_artifact_offloader=self._conversation_artifact_offloader,
        strands_messages=task_strands_messages(task_info.task.task_id),
        task_manager=self.task_manager,
        task_info=task_info,
        task_status=ProgressState.COMPLETED.value,
        logger=self._logger,
        agent_id=self.id,
        team_id=self.team_id,
        named_agent_type=self.named_agent_type,
        summary_generator=self._conversation_summary_generator,
    )
    await self._notify_context_after_turn(
        task_info=task_info,
        task_status=ProgressState.COMPLETED.value,
    )
    await self.task_manager.operations_runtime.run(
        self.task_manager.update_main_task_state,
        incoming_task.task_id,
        ProgressState.FINALIZING_PLAN,
        now_ts,
    )
    await self.task_manager.operations_runtime.run(
        self.task_manager.append_history_entry,
        incoming_task.task_id,
        "Terminal task response generated; entering plan finalization before completion.",
        entry_type=TaskHistoryType.PLAN_EVENT,
        importance=TaskHistoryImportance.NORMAL,
        timestamp=now_ts,
        agent_name=self.name,
        agent_id=self.id,
        tracks_to_plan=True,
        details={"action": "enter_finalizing_plan"},
    )
    return True


async def _complete_local_task_finalization_after_dispatch(
    self,
    *,
    task_id: str,
    after_action_report_sent: bool = False,
) -> bool:
    task_info = await self.task_manager.operations_runtime.run(
        self.task_manager.get_task_info, task_id
    )
    if task_info is None:
        return False

    now_ts = datetime.now(timezone.utc).isoformat()
    digest = await self.task_manager.operations_runtime.run(
        self.task_manager.get_execution_digest, task_id
    )
    open_plan_items: List[str] = []
    completed_plan_items: List[str] = []
    dropped_plan_items: List[str] = []
    open_unknowns: List[str] = []
    if digest is not None:
        for item in digest.plan_items:
            if item.status == ExecutionPlanItemStatus.COMPLETED:
                completed_plan_items.append(item.step_id)
            elif item.status == ExecutionPlanItemStatus.DROPPED:
                dropped_plan_items.append(item.step_id)
            else:
                open_plan_items.append(item.step_id)
        open_unknowns = [
            item.unknown_id
            for item in digest.unknowns
            if item.status == ExecutionUnknownStatus.OPEN
        ]
    await self.task_manager.operations_runtime.run(
        self.task_manager.append_history_entry,
        task_id,
        "Completed local plan finalization after terminal response dispatch.",
        entry_type=TaskHistoryType.PLAN_EVENT,
        importance=TaskHistoryImportance.NORMAL,
        timestamp=now_ts,
        agent_name=self.name,
        agent_id=self.id,
        tracks_to_plan=True,
        details={
            "action": "complete_finalizing_plan",
            "after_action_report_sent": after_action_report_sent,
            "open_plan_item_ids": open_plan_items,
            "completed_plan_item_ids": completed_plan_items,
            "dropped_plan_item_ids": dropped_plan_items,
            "open_unknown_ids": open_unknowns,
        },
    )
    await self._complete_task_and_schedule_aar_review(task_id, now_ts)
    return True


class TaskProbeNotPossible(Exception):
    """This process cannot ask about a delegated task yet (``custom_agents.agent_requests.cannot_ask``).

    ``clears`` says whether the reason passes by itself (the join, the asking agent's
    session, the team's view); the watchdog waits for those, within a limit
    (``_recover_stalled_subtasks``), and counts the others as nobody answering.
    """

    def __init__(self, reason: str, *, clears: bool) -> None:
        super().__init__(reason)
        self.reason, self.clears = reason, clears


async def _probe_subtask_task_status(
    self, *, subtask_info: TaskInfo, holder_agent_id: Optional[str] = None
) -> Optional[TaskStatus]:
    """Ask about one delegated task (``task.probe``); only the worker holding it should answer.

    With ``holder_agent_id`` (from the task's claim) only that worker is asked. Without it
    every live worker of the task's type (or the named worker) is asked, from the team's
    agent records (``custom_agents.agent_requests.team_view``), and only an answer from one
    that knows the task counts: before 2026-10-04 the first reply won, so an idle worker's
    "unknown" could fail a task another worker was running. The answer keeps today's
    ``TaskStatus`` form for the watchdog's decision and its history entry.

    All targets are asked at once under one deadline (``agent_requests.ask_each``). A target
    that does not answer, refuses, or whose answer fails in any other way counts as no
    answer from that target, logged with the task and target ids; the others' answers are
    kept, and nothing is raised for a target. When this process cannot ask at all yet,
    ``TaskProbeNotPossible`` says why instead of "no answer".
    """

    from custom_agents.agent_requests import ask_each, cannot_ask, team_view

    if not self.team_id:
        return None
    task_id = subtask_info.task.task_id
    blocked = cannot_ask(self, need_view=holder_agent_id is None)
    if blocked is not None:
        raise TaskProbeNotPossible(blocked.reason, clears=blocked.clears)
    names = _probe_targets(self, team_view(), subtask_info, holder_agent_id)
    if not names:
        return None
    timeout = max(0.1, float(self.subtask_status_probe_timeout_seconds or 5.0))
    probe_id = new_id("task_status")
    answers = await ask_each(self, "task.probe", {"task_id": task_id}, sorted(names), timeout=timeout)
    statuses: list[Optional[TaskStatus]] = []
    for target, reply in answers.items():
        if _probe_reply_failed(self, task_id, target, reply):
            statuses.append(None)
            continue
        name, agent_type = names[target]
        statuses.append(_probe_answer(subtask_info, holder_agent_id, probe_id, target, name, agent_type, reply))
    if holder_agent_id is not None:
        return statuses[0]
    # A worker that does not hold the task says so; wait for the one that does.
    return next((status for status in statuses if status is not None and status.is_known_task), None)


def _probe_targets(agent: Any, view: Any, subtask_info: TaskInfo,
                   holder_agent_id: Optional[str]) -> dict[str, tuple[Optional[str], Optional[str]]]:
    """Whom to ask, with each one's name and type: the holder, or else the named worker or every worker of the type."""

    if holder_agent_id is not None:
        known = view.agent(holder_agent_id) if view is not None else None
        return {holder_agent_id: (known.display_name if known else None, known.agent_type if known else None)}
    if view is None:
        return {}
    target_name = getattr(subtask_info.meta, "to_agent_name", None)
    target_type = getattr(subtask_info.meta, "to_named_agent_type", None)
    named = view.named(target_name) if target_name else None
    candidates = [named] if named is not None else view.of_type(str(target_type or ""))
    return {item.agent: (item.display_name, item.agent_type) for item in candidates if item.agent != str(agent.id)}


def _probe_reply_failed(agent: Any, task_id: str, target: str, reply: Any) -> bool:
    """Whether a target's reply is an error, logged with the ids; ``TaskProbeNotPossible`` when this side cannot ask."""

    from custom_agents.agent_requests import AgentUnreachable
    from custom_transport.protocol1.live import RequestRefused, ServiceUnavailable

    if isinstance(reply, AgentUnreachable):
        raise TaskProbeNotPossible(str(reply), clears=reply.clears)
    if isinstance(reply, (ServiceUnavailable, RequestRefused)):
        agent._logger.info("[%s] A task probe got no answer. event=subtask_task_probe_unanswered task_id=%s "
                           "target_agent_id=%s reason=%s", agent.id, task_id, target, reply)
        return True
    if isinstance(reply, Exception):
        agent._logger.warning("[%s] Asking an agent about a delegated task failed; it counts as no answer from "
                              "that agent. event=subtask_task_probe_failed task_id=%s target_agent_id=%s "
                              "error_type=%s error=%s", agent.id, task_id, target, type(reply).__name__, reply)
        return True
    return False


def _probe_answer(subtask_info: TaskInfo, holder_agent_id: Optional[str], probe_id: str, target: str,
                  name: Optional[str], agent_type: Optional[str], reply: dict[str, Any]) -> TaskStatus:
    """One ``task.probe`` reply in the watchdog's ``TaskStatus`` form."""

    state = reply.get("state")
    return TaskStatus(
        task_status_id=probe_id,
        status_type=MessageMetaType.RESPONSE,
        task_id=subtask_info.task.task_id,
        parent_task_id=subtask_info.task.parent_task_id,
        target_agent_id=holder_agent_id,
        responder_agent_id=target,
        responder_agent_name=name,
        responder_named_agent_type=agent_type,
        is_known_task=bool(reply.get("known")),
        is_current_task=bool(reply.get("current")),
        task_state=ProgressState(state) if state else None,
        last_task_update_at=reply.get("last_update_at"),
        is_executing=bool(reply.get("executing")),
        is_exiting=bool(reply.get("exiting")),
    )


def _task_status_extends_subtask_wait(self, status: Optional[TaskStatus]) -> bool:
    if status is None:
        return False
    if not status.is_known_task or status.is_exiting:
        return False
    if status.task_state in {
        ProgressState.COMPLETED,
        ProgressState.FAILED,
        ProgressState.CANCELLED,
        ProgressState.ABANDONED,
    }:
        return False
    return True


@staticmethod
def _format_subtask_timeout_threshold(threshold: float) -> str:
    return f"{int(round(threshold * 100))}%"


async def _record_subtask_timeout_probe(
    self,
    *,
    parent_task_id: str,
    subtask_info: TaskInfo,
    threshold: float,
    timeout_seconds: float,
    age_seconds: float,
    cycle_started_at: Optional[str],
    status: Optional[TaskStatus],
    timestamp: str,
    final_probe: bool = False,
) -> None:
    threshold_label = self._format_subtask_timeout_threshold(threshold)
    extends_wait = self._task_status_extends_subtask_wait(status)
    target_label = (
        getattr(subtask_info.meta, "to_agent_name", None)
        or getattr(subtask_info.meta, "to_named_agent_type", None)
        or subtask_info.header.to_routing_path
        or "worker"
    )
    if extends_wait:
        summary = (
            f"Refreshed delegated subtask {subtask_info.task.task_id} at "
            f"{threshold_label} of timeout; "
            f"{status.responder_agent_name or status.responder_agent_id or target_label} "
            f"reported task state {status.task_state}."
        )
    elif status is None:
        summary = (
            f"Tried to refresh delegated subtask {subtask_info.task.task_id} at "
            f"{threshold_label} of timeout but received no TaskStatus response."
        )
    else:
        summary = (
            f"Tried to refresh delegated subtask {subtask_info.task.task_id} at "
            f"{threshold_label} of timeout; response did not extend the wait "
            f"(known={status.is_known_task}, exiting={status.is_exiting}, state={status.task_state})."
        )
    await self.task_manager.operations_runtime.run(
        self.task_manager.append_history_entry,
        parent_task_id,
        summary,
        entry_type=TaskHistoryType.SUBTASK_EVENT
        if extends_wait
        else TaskHistoryType.ERROR_EVENT,
        importance=TaskHistoryImportance.HIGH,
        timestamp=timestamp,
        agent_name=(
            status.responder_agent_name
            if status is not None
            else getattr(subtask_info.meta, "to_agent_name", None)
        ),
        agent_id=(status.responder_agent_id if status is not None else None),
        related_task_id=subtask_info.task.task_id,
        details={
            "action": SUBTASK_TIMEOUT_PROBE_ACTION,
            "threshold": threshold,
            "threshold_percent": int(round(threshold * 100)),
            "timeout_seconds": timeout_seconds,
            "age_seconds": age_seconds,
            "cycle_started_at": cycle_started_at,
            "response_received": status is not None,
            "probe_timeout_seconds": self.subtask_status_probe_timeout_seconds,
            "target_agent_name": subtask_info.meta.to_agent_name,
            "target_named_agent_type": subtask_info.meta.to_named_agent_type,
            "extends_wait": extends_wait,
            "task_status_id": getattr(status, "task_status_id", None),
            "task_state": str(getattr(status, "task_state", ""))
            if status is not None
            else None,
            "responder_agent_id": getattr(status, "responder_agent_id", None),
            "responder_agent_name": getattr(status, "responder_agent_name", None),
            "responder_named_agent_type": getattr(
                status, "responder_named_agent_type", None
            ),
            "final_probe": final_probe,
        },
    )


async def _attempted_subtask_timeout_thresholds(
    self,
    *,
    parent_task_id: str,
    subtask_id: str,
    cycle_started_at: Optional[str],
) -> set[float]:
    attempted: set[float] = set()
    for entry in await self.task_manager.operations_runtime.run(
        self.task_manager.get_task_history_entries, parent_task_id, limit=None
    ):
        details = entry.details or {}
        if details.get("action") != SUBTASK_TIMEOUT_PROBE_ACTION:
            continue
        if entry.related_task_id != subtask_id:
            continue
        if details.get("cycle_started_at") != cycle_started_at:
            continue
        try:
            attempted.add(round(float(details.get("threshold")), 2))
        except (TypeError, ValueError):
            continue
    return attempted


async def _next_due_subtask_timeout_threshold(
    self,
    *,
    parent_task_id: str,
    subtask_info: TaskInfo,
    cycle_started_at: Optional[str],
    age_seconds: float,
    timeout_seconds: float,
) -> Optional[float]:
    attempted = await self._attempted_subtask_timeout_thresholds(
        parent_task_id=parent_task_id,
        subtask_id=subtask_info.task.task_id,
        cycle_started_at=cycle_started_at,
    )
    for threshold in SUBTASK_TIMEOUT_PROBE_THRESHOLDS:
        normalized = round(threshold, 2)
        if normalized in attempted:
            continue
        if age_seconds >= timeout_seconds * threshold:
            return threshold
    return None


async def _subtask_timeout_probe_summary(
    self,
    *,
    parent_task_id: str,
    subtask_id: str,
    cycle_started_at: Optional[str],
) -> str:
    entries = [
        entry
        for entry in await self.task_manager.operations_runtime.run(
            self.task_manager.get_task_history_entries, parent_task_id, limit=None
        )
        if (entry.details or {}).get("action") == SUBTASK_TIMEOUT_PROBE_ACTION
        and entry.related_task_id == subtask_id
        and (entry.details or {}).get("cycle_started_at") == cycle_started_at
    ]
    if not entries:
        return (
            "No pre-timeout TaskStatus refresh probes were recorded before the timeout."
        )
    fragments: list[str] = []
    for entry in entries:
        details = entry.details or {}
        threshold_percent = details.get("threshold_percent")
        prefix = (
            f"{threshold_percent}%"
            if threshold_percent is not None
            else "unknown threshold"
        )
        if details.get("extends_wait"):
            fragments.append(f"{prefix}: worker responded and the wait was refreshed")
        elif details.get("response_received"):
            fragments.append(f"{prefix}: worker response did not extend the wait")
        else:
            fragments.append(f"{prefix}: no TaskStatus response")
    return "Refresh probe history: " + "; ".join(fragments) + "."


async def _subtask_probe_cycle_start(
    agent, parent_task_id: str, subtask: TaskInfo, claimed_at: Optional[str] = None
) -> Optional[str]:
    """Keep liveness timing separate from progress, including across restart.

    A cycle starts at the newest of the subtask's last state change, the time a
    worker took it (its claim) and the last probe its worker answered.
    """
    latest = subtask.task.task_state[-1][1] if subtask.task.task_state else None
    latest_at = agent._parse_iso_timestamp(latest)
    claimed = agent._parse_iso_timestamp(claimed_at)
    if claimed is not None and (latest_at is None or claimed > latest_at):
        latest, latest_at = claimed_at, claimed
    entries = await agent.task_manager.operations_runtime.run(
        agent.task_manager.get_task_history_entries, parent_task_id, limit=None
    )
    for entry in entries:
        if (
            entry.related_task_id != subtask.task.task_id
            or entry.details.get("action") != SUBTASK_TIMEOUT_PROBE_ACTION
            or not entry.details.get("extends_wait")
        ):
            continue
        probed_at = agent._parse_iso_timestamp(entry.timestamp)
        if probed_at is not None and (latest_at is None or probed_at > latest_at):
            latest, latest_at = entry.timestamp, probed_at
    return latest


async def _delegated_holders(agent: Any) -> dict[str, Any]:
    """This agent's open delegated-task records by task id (empty if unreadable)."""

    from custom_agents.delegated_work_runtime import delegated_tasks

    try:
        records = await agent.task_manager.operations_runtime.run(
            delegated_tasks(agent).list_open, str(agent.team_id), str(agent.id)
        )
    except Exception:
        agent._logger.exception(
            "Could not read delegated-task records; probing delegated tasks by type. "
            "event=delegated_task_records_unreadable agent_id=%s",
            agent.id,
        )
        return {}
    return {record.task_id: record for record in records}


def _holder(holders: dict[str, Any], subtask_info: TaskInfo) -> Optional[str]:
    record = holders.get(subtask_info.task.task_id)
    return getattr(record, "holder_agent_id", None)


async def _queue_tracking(
    agent: Any, holders: dict[str, Any], subtask_info: TaskInfo
) -> tuple[bool, Optional[str]]:
    """Whether a delegated task still waits in a shared queue, and when a worker took it.

    A task waiting in its worker type's shared queue is held by the queue:
    any worker of the type can take it, so there is nothing to probe and no
    timeout; the worker pool controller starts workers and reviews long waits.
    A task sent to one named worker's own mailbox is different: only that
    worker can take it, so it is probed and timed out like running work, and a
    worker that died with it in its inbox is noticed (fixed 2026-10-05).
    """

    record = holders.get(subtask_info.task.task_id)
    if record is None:
        await _warn_untracked_subtask(agent, subtask_info.task.task_id)
        return False, None
    if record.status == DELEGATED_QUEUED and not record.target_agent_name:
        if await _never_published(agent, record):
            _warn_never_published(agent, record)
            return False, None
        return True, None
    claimed = record.claimed_at_utc
    return False, claimed.isoformat() if claimed is not None else None


async def _never_published(agent: Any, record: Any) -> bool:
    """A queued record without a stream sequence that no outbox action still publishes.

    The requester records a delegated task before publishing it and adds the
    sequence afterwards. When the outbox gives up publishing it ends the
    subtask (``delegated_work_runtime.end_unpublished_delegation``); this is
    the safeguard for a record that outlived that, so it is checked and timed
    out like untracked work instead of waiting for ever. It acts only when
    the outbox gave up or holds no action for the task; the record's age
    alone misjudged a requester that was busy or had restarted.
    """
    if record.publish_sequence is not None:
        return False
    from custom_agents.turn_action_outbox_runtime import delegation_publish_given_up

    return await agent.task_manager.operations_runtime.run(
        partial(delegation_publish_given_up, agent.task_manager, record.task_id)
    )


def _warn_never_published(agent: Any, record: Any) -> None:
    key = f"never_published:{record.task_id}"
    warned = agent._untracked_subtask_warnings
    if key in warned:
        return
    warned.add(key)
    agent._logger.warning(
        "A delegated task was recorded but never sent to a worker; it is now checked and "
        "timed out like untracked work. event=delegated_record_never_published agent_id=%s "
        "task_id=%s subtask_id=%s record_team_id=%s record_task_id=%s recorded_at=%s",
        agent.id,
        record.parent_task_id,
        record.task_id,
        record.team_id,
        record.task_id,
        record.updated_at_utc.isoformat(),
    )


async def _warn_untracked_subtask(agent: Any, task_id: str) -> None:
    warned = agent._untracked_subtask_warnings
    if task_id in warned:
        return
    warned.add(task_id)
    agent._logger.warning(
        "A delegated task has no delegated-task record; it is probed by worker type, "
        "and only a worker that knows it can answer. event=delegated_task_untracked "
        "agent_id=%s task_id=%s",
        agent.id,
        task_id,
    )


async def _watchdog_probe(
    agent: Any,
    subtask_info: TaskInfo,
    holder_agent_id: Optional[str],
    *,
    parent_task_id: str,
    overdue_seconds: Optional[float],
    timeout_seconds: float = 0.0,
) -> tuple[bool, Optional[TaskStatus]]:
    """Probe a delegated task for the watchdog: whether to decide on it now, and the answer.

    When this process cannot ask yet for a reason that clears by itself (its join, its
    session or the team's view is not there yet), the watchdog waits rather than read it
    as no answer: a lead that has just restarted must not fail work that still runs. The
    wait is bounded: once the subtask is overdue by one more timeout, the watchdog decides
    as if nobody answered. A reason that never clears counts as no answer at once, as
    before. Each case is logged with the ids, as a warning once per subtask and reason.
    """

    try:
        return True, await agent._probe_subtask_task_status(subtask_info=subtask_info, holder_agent_id=holder_agent_id)
    except TaskProbeNotPossible as blocked:
        waits = blocked.clears and (overdue_seconds is None or overdue_seconds <= timeout_seconds)
        _log_probe_not_possible(agent, parent_task_id, subtask_info.task.task_id, blocked.reason, waits=waits,
                                overdue_seconds=overdue_seconds)
        return (False, None) if waits else (True, None)


def _log_probe_not_possible(agent: Any, parent_task_id: str, task_id: str, reason: str, *, waits: bool,
                            overdue_seconds: Optional[float]) -> None:
    """Log that the watchdog could not ask: a warning once per subtask and reason, then at debug."""

    then = ("the watchdog asks again at its next pass" if waits
            else "it counts as no answer, so the watchdog decides on the subtask now")
    event = "subtask_task_probe_deferred" if waits else "subtask_task_probe_not_possible"
    key = f"probe_not_possible:{task_id}:{event}:{reason}"
    # The agent's memory of warnings already given about its subtasks.
    warned = getattr(agent, "_untracked_subtask_warnings", None)
    log = agent._logger.debug if warned is not None and key in warned else agent._logger.warning
    if warned is not None:
        warned.add(key)
    log("[%s] This process cannot ask about a delegated task yet; %s. event=%s agent_id=%s task_id=%s subtask_id=%s "
        "reason=%s overdue_seconds=%s", agent.id, then, event, agent.id, parent_task_id, task_id, reason,
        None if overdue_seconds is None else int(overdue_seconds))


async def _recover_stalled_subtasks(self) -> None:
    timeout_seconds = self.subtask_timeout_seconds
    if timeout_seconds is None or timeout_seconds <= 0:
        return
    if self.paused:
        return

    now = datetime.now(timezone.utc)
    pre_timeout_probe_due: List[tuple[str, TaskInfo, float, float, Optional[str]]] = []
    overdue: List[tuple[str, TaskInfo, float, Optional[str]]] = []
    holders = await _delegated_holders(self)
    for subtask_info in await self.task_manager.operations_runtime.run(
        self.task_manager.get_pending_subtasks,
    ):
        parent_task_id = await self.task_manager.operations_runtime.run(
            self.task_manager.get_parent_task_id, subtask_info.task.task_id
        )
        if not parent_task_id:
            continue
        # The parent's execution lane runs a turn now; a later pass looks
        # again. Other requests' lanes are not held back by it
        # (custom_agents.execution_lanes).
        if self._execution_lanes.lane_busy(await lane_of(self, parent_task_id)):
            continue
        waiting_in_queue, claimed_at = await _queue_tracking(self, holders, subtask_info)
        if waiting_in_queue:
            continue
        if (
            await self.task_manager.operations_runtime.run(
                self.task_manager.get_pending_recoverable_pause, parent_task_id
            )
            is not None
        ):
            self._logger.debug(
                "[%s] Skipping watchdog probes while parent task %s is paused for operator guidance.",
                self.id,
                parent_task_id,
            )
            continue
        last_timestamp = await _subtask_probe_cycle_start(
            self,
            parent_task_id,
            subtask_info,
            claimed_at=claimed_at,
        )
        last_update_at = self._parse_iso_timestamp(last_timestamp)
        if last_update_at is None:
            continue
        age_seconds = (now - last_update_at).total_seconds()
        threshold = await self._next_due_subtask_timeout_threshold(
            parent_task_id=parent_task_id,
            subtask_info=subtask_info,
            cycle_started_at=last_timestamp,
            age_seconds=age_seconds,
            timeout_seconds=float(timeout_seconds),
        )
        if age_seconds <= timeout_seconds and threshold is not None:
            pre_timeout_probe_due.append(
                (parent_task_id, subtask_info, age_seconds, threshold, last_timestamp)
            )
            continue
        if age_seconds > timeout_seconds:
            overdue.append((parent_task_id, subtask_info, age_seconds, last_timestamp))

    for (
        parent_task_id,
        subtask_info,
        age_seconds,
        threshold,
        cycle_started_at,
    ) in pre_timeout_probe_due:
        asked, status = await _watchdog_probe(
            self, subtask_info, _holder(holders, subtask_info), parent_task_id=parent_task_id,
            overdue_seconds=None,
        )
        if not asked:
            continue
        now_ts = datetime.now(timezone.utc).isoformat()
        await self._record_subtask_timeout_probe(
            parent_task_id=parent_task_id,
            subtask_info=subtask_info,
            threshold=threshold,
            timeout_seconds=float(timeout_seconds),
            age_seconds=age_seconds,
            cycle_started_at=cycle_started_at,
            status=status,
            timestamp=now_ts,
        )

    recovered_any = False
    for parent_task_id, subtask_info, age_seconds, cycle_started_at in overdue:
        target_label = (
            getattr(subtask_info.meta, "to_agent_name", None)
            or getattr(subtask_info.meta, "to_named_agent_type", None)
            or subtask_info.header.to_routing_path
        )
        asked, status = await _watchdog_probe(
            self, subtask_info, _holder(holders, subtask_info), parent_task_id=parent_task_id,
            overdue_seconds=age_seconds - float(timeout_seconds), timeout_seconds=float(timeout_seconds),
        )
        if not asked:
            continue
        timeout_probe_ts = datetime.now(timezone.utc).isoformat()
        await self._record_subtask_timeout_probe(
            parent_task_id=parent_task_id,
            subtask_info=subtask_info,
            threshold=1.0,
            timeout_seconds=float(timeout_seconds),
            age_seconds=age_seconds,
            cycle_started_at=cycle_started_at,
            status=status,
            timestamp=timeout_probe_ts,
            final_probe=True,
        )
        if self._task_status_extends_subtask_wait(status):
            continue
        probe_summary = await self._subtask_timeout_probe_summary(
            parent_task_id=parent_task_id,
            subtask_id=subtask_info.task.task_id,
            cycle_started_at=cycle_started_at,
        )
        if (
            self._runtime_budget_policy_mode()
            == RuntimeBudgetPolicyMode.USER_GUIDED.value
            and self._runtime_budget_trigger_enabled(
                RecoverablePauseTrigger.DELEGATED_SUBTASK_TIMEOUT
            )
        ):
            parent_task_info = await self.task_manager.operations_runtime.run(
                self.task_manager.get_task_info, parent_task_id
            )
            if parent_task_info is not None:
                pending_pause = await self.task_manager.operations_runtime.run(
                    self.task_manager.get_pending_recoverable_pause, parent_task_id
                )
                if pending_pause is not None:
                    recovered_any = True
                    continue
                if (
                    await self.task_manager.operations_runtime.run(
                        self.task_manager.count_recoverable_pauses, parent_task_id
                    )
                    < self._max_user_guided_pauses_per_task()
                ):
                    friendly_reason = self._friendly_budget_reason(
                        trigger=RecoverablePauseTrigger.DELEGATED_SUBTASK_TIMEOUT,
                        msg_content=parent_task_info.task,
                        detail=(
                            f"a delegated worker has not returned a result within the configured wait window. "
                            f"{probe_summary}"
                        ),
                    )
                    await self._send_runtime_pause_query(
                        task_info=parent_task_info,
                        trigger=RecoverablePauseTrigger.DELEGATED_SUBTASK_TIMEOUT,
                        friendly_reason=friendly_reason,
                        base_tool_call_limit=self._resolved_max_tool_calls_per_task(),
                        affected_subtask_id=subtask_info.task.task_id,
                        affected_agent_id=getattr(
                            subtask_info.header, "to_routing_path", None
                        ),
                        affected_named_agent_type=getattr(
                            subtask_info.meta, "to_named_agent_type", None
                        ),
                    )
                    recovered_any = True
                    continue
        await self._fail_subtask_and_reconcile_parent(
            parent_task_id=parent_task_id,
            subtask_info=subtask_info,
            error_message=(
                f"Delegated subtask timed out after {int(age_seconds)}s waiting for "
                f"{target_label or 'worker response'}."
            ),
            resume_parent=True,
            synthetic_context=(
                f"Tried to refresh delegated subtask {subtask_info.task.task_id} status before timeout. "
                f"{probe_summary} Subtask marked as failed; continue the parent task by deciding whether "
                "to summarize partial findings, retry with a narrower task, delegate to another worker, "
                "or ask the user how to proceed."
            ),
        )
        recovered_any = True

    overdue_query_waits: List[tuple[TaskInfo, TaskQuery, float]] = []
    for task_info in await self.task_manager.operations_runtime.run(
        self.task_manager.list_task_infos
    ):
        if await self.task_manager.operations_runtime.run(
            self.task_manager.is_terminal_state, task_info.state
        ):
            continue
        if (
            await self.task_manager.operations_runtime.run(
                self.task_manager.get_pending_recoverable_pause, task_info.task.task_id
            )
            is not None
        ):
            continue
        pending_query = await self.task_manager.operations_runtime.run(
            self.task_manager.get_latest_pending_task_query, task_info.task.task_id
        )
        if pending_query is None:
            continue

        request_timestamp = None
        for query_event in reversed(
            await self.task_manager.operations_runtime.run(
                self.task_manager.get_task_history_entries,
                task_info.task.task_id,
                limit=None,
            )
        ):
            if (
                query_event.entry_type == TaskHistoryType.QUERY_EVENT
                and query_event.related_task_id == pending_query.task_query_id
                and (query_event.details or {}).get("query_type")
                == MessageMetaType.REQUEST.value
            ):
                request_timestamp = query_event.timestamp
                break

        last_update_at = self._parse_iso_timestamp(request_timestamp)
        if last_update_at is None:
            continue
        age_seconds = (now - last_update_at).total_seconds()
        if age_seconds <= timeout_seconds:
            continue
        overdue_query_waits.append((task_info, pending_query, age_seconds))

    for task_info, pending_query, age_seconds in overdue_query_waits:
        task_id = task_info.task.task_id
        error_message = (
            f"TaskQuery {pending_query.task_query_id} timed out after {int(age_seconds)}s "
            f"waiting for a response."
        )
        self._logger.warning(f"[{self.id}] {error_message} Failing task {task_id}.")
        now_ts = datetime.now(timezone.utc).isoformat()
        await self.task_manager.operations_runtime.run(
            self.task_manager.fail_task, task_id, error_message, now_ts
        )
        await finalize_conversation_turn(
            conversation_store=self._conversation_store,
            conversation_compactor=self._conversation_compactor,
            conversation_artifact_offloader=self._conversation_artifact_offloader,
            strands_messages=task_strands_messages(task_info.task.task_id),
            task_manager=self.task_manager,
            task_info=task_info,
            task_status=ProgressState.FAILED.value,
            logger=self._logger,
            agent_id=self.id,
            team_id=self.team_id,
            named_agent_type=self.named_agent_type,
            summary_generator=self._conversation_summary_generator,
        )
        await self._notify_context_after_turn(
            task_info=task_info,
            task_status=ProgressState.FAILED.value,
        )
        await self._send_task_error_response(
            task_info.task,
            task_info.header,
            task_info.meta,
            error_message,
        )
        recovered_any = True

    from custom_agents.delegation_wake import wake_due_check_backs

    # Check-back times the requester's model set on its delegations.
    recovered_any = bool(await wake_due_check_backs(self)) or recovered_any
    if recovered_any:
        # The retained drain runs the recovered work, never this scheduler pass.
        schedule_pending_work(self)
