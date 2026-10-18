"""Delegation helpers: target checks, subtask registration and status probes.

Delegated tasks for a worker type are published to the type's shared queue and
wait there until a free worker takes one (ADR-019); ``delegated_work_runtime``
tracks them after publication.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from typing import Optional

from custom_agents.message_routing import construct_header
from custom_agents.task_manager import TaskInfo
from custom_agents.task_transport import (
    task_request_transport_copy,
    task_response_history_transport_copy,
)
from custom_messages.contracts.control_v1 import AgentStatus
from custom_messages.contracts.envelope_v1 import CommsMessage, MessageEnvelope
from custom_messages.contracts.routing_v1 import MessageHeader, MessageMeta
from custom_messages.contracts.task_v1 import Task
from custom_messages.enums import (
    ControlType,
    MessageMetaType,
    RequesterType,
    RoutingType,
    TaskHistoryImportance,
    TaskHistorySource,
    TaskHistoryType,
)
from id_generation import new_id


def _resolve_manager_identity(self) -> tuple[Optional[str], Optional[str]]:
    if self.is_team_lead:
        return self.id, self.name
    for status in self.known_agents.agent_map.values():
        if status.team_id == self.team_id and status.is_team_lead:
            return status.self_id, status.name
    return None, None


def _agent_status_matches_named_type(
    self,
    status: AgentStatus,
    named_agent_type: Optional[str],
    target_agent_name: Optional[str] = None,
) -> bool:
    # A winding-down worker stays present to finish held tasks but takes no
    # new work, so it cannot satisfy a delegation.
    if status.is_exiting or not status.accepting_work:
        return False
    if named_agent_type and status.named_agent_type != named_agent_type:
        return False
    if status.team_id != self.team_id:
        return False
    return not target_agent_name or status.name == target_agent_name


def _resolve_pending_agent_status_probes(
    self,
    agent_status: AgentStatus,
    correlation_id: Optional[str],
    sent_at: Optional[datetime],
) -> int:
    """Resolve matching liveness probes from a response or fresh presence update."""

    if correlation_id:
        pending = self._pending_agent_status_futures.get(correlation_id)
        candidates = [(correlation_id, pending)] if pending is not None else []
        verification_source = "correlated_response"
    else:
        # A newly created worker can publish presence after a probe was sent but
        # before its operational subscription existed. Only a snapshot sent
        # during this probe can substitute for its correlated response; retained
        # presence from before the probe is discovery data, not a live reply.
        candidates = list(self._pending_agent_status_futures.items())
        verification_source = "presence_update"

    resolved = 0
    for pending_correlation_id, pending in candidates:
        expected_named_agent_type, expected_agent_name, started_at, future = pending
        if not correlation_id:
            if sent_at is None or not started_at <= sent_at <= datetime.now(timezone.utc):
                self._logger.debug(
                    "[%s] Presence cannot answer this live probe. "
                    "event=agent_status_probe_presence_ignored correlation_id=%s "
                    "responding_agent_id=%s sent_at=%s probe_started_at=%s",
                    self.id, pending_correlation_id, agent_status.self_id,
                    sent_at, started_at.isoformat(),
                )
                continue
        matches_probe = self._agent_status_matches_named_type(
            agent_status,
            expected_named_agent_type,
            expected_agent_name,
        )
        if not future.done() and matches_probe:
            self._logger.debug(
                "[%s] event=agent_status_probe_matched correlation_id=%s "
                "expected_named_agent_type=%s responding_agent_id=%s "
                "responding_team_id=%s responding_is_exiting=%s "
                "verification_source=%s",
                self.id,
                pending_correlation_id,
                expected_named_agent_type,
                agent_status.self_id,
                agent_status.team_id,
                agent_status.is_exiting,
                verification_source,
            )
            future.set_result(agent_status)
            resolved += 1
        elif correlation_id and not future.done():
            self._logger.warning(
                "[%s] event=agent_status_probe_response_rejected correlation_id=%s "
                "expected_named_agent_type=%s expected_agent_name=%s "
                "response_named_agent_type=%s response_agent_name=%s "
                "response_team_id=%s local_team_id=%s response_is_exiting=%s",
                self.id,
                pending_correlation_id,
                expected_named_agent_type,
                expected_agent_name,
                agent_status.named_agent_type,
                agent_status.name,
                agent_status.team_id,
                self.team_id,
                agent_status.is_exiting,
            )
    return resolved


async def _record_delegation_subtask_abort(
    self,
    *,
    failure_phase: str,
    parent_task_id: Optional[str],
    candidate_task_id: Optional[str],
    target_named_agent_type: Optional[str],
    target_agent_name: Optional[str],
    detail: Optional[str] = None,
) -> None:
    self._logger.error(
        "[%s] event=delegation_subtask_registration_aborted final_failure_phase=%s "
        "parent_task_id=%s candidate_task_id=%s target_named_agent_type=%s "
        "target_agent_name=%s detail=%s",
        self.id,
        failure_phase,
        parent_task_id,
        candidate_task_id,
        target_named_agent_type,
        target_agent_name,
        detail,
    )

    if (
        parent_task_id
        and await self.task_manager.operations_runtime.run(
            self.task_manager.get_task_info, parent_task_id
        )
        is not None
    ):
        target_label = target_named_agent_type or target_agent_name or "unknown target"
        summary = (
            "Delegated subtask dispatch aborted before transport publish: "
            f"{failure_phase.replace('_', ' ')} for {target_label}."
        )
        await self.task_manager.operations_runtime.run(
            self.task_manager.append_history_entry,
            parent_task_id,
            summary,
            source=TaskHistorySource.SYSTEM,
            entry_type=TaskHistoryType.ERROR_EVENT,
            importance=TaskHistoryImportance.HIGH,
            timestamp=datetime.now(timezone.utc).isoformat(),
            agent_name=self.name,
            agent_id=self.id,
            related_task_id=candidate_task_id,
            details={
                "event": "delegation_subtask_registration_aborted",
                "final_failure_phase": failure_phase,
                "candidate_task_id": candidate_task_id,
                "target_named_agent_type": target_named_agent_type,
                "target_agent_name": target_agent_name,
                "detail": detail,
            },
        )


async def _probe_live_named_agent_type(
    self,
    named_agent_type: Optional[str],
    target_agent_name: Optional[str] = None,
) -> Optional[AgentStatus]:
    if not self.team_id or not (named_agent_type or target_agent_name):
        return None
    if not self._operational_exchange_name:
        self._logger.warning(
            "[%s] event=agent_status_probe_unavailable target_named_agent_type=%s",
            self.id,
            named_agent_type,
        )
        return None

    correlation_id = new_id("corr_agent_prb")
    timeout = max(0.1, float(self.subtask_status_probe_timeout_seconds or 5.0))
    operational_exchange = (
        self._operational_exchange_name or f"{self.team_id}_operational"
    )
    response_route = self.id
    self._logger.debug(
        "[%s] event=agent_status_probe_start correlation_id=%s target_named_agent_type=%s "
        "operational_exchange=%s response_queue=%s timeout_seconds=%s agent_id=%s team_id=%s "
        "named_agent_type=%s",
        self.id,
        correlation_id,
        named_agent_type,
        operational_exchange,
        response_route,
        timeout,
        self.id,
        self.team_id,
        self.named_agent_type,
    )
    status_request = AgentStatus(
        self_id=self.id,
        team_id=self.team_id,
        named_agent_type=self.named_agent_type,
        is_paused=bool(self.paused),
        is_executing=bool(self.is_executing),
        has_execution_capacity=bool(self._has_execution_capacity()),
        is_busy=bool(self.is_executing),
        name=self.name,
        agent_role=self.role,
        status_type=MessageMetaType.REQUEST,
        info_type=ControlType.STATUS_REQUEST_BASIC,
    )
    loop = asyncio.get_running_loop()
    future: asyncio.Future = loop.create_future()
    self._pending_agent_status_futures[correlation_id] = (
        named_agent_type,
        target_agent_name,
        datetime.now(timezone.utc),
        future,
    )
    envelope = MessageEnvelope(
        msg_content=status_request,
        msg_header=MessageHeader(
            from_agent_id=self.id,
            to_routing_path=RoutingType.ALL_AGENTS_ROUTE,
            response_routing_key=response_route,
            agent_signature="",
        ),
        msg_meta=MessageMeta(
            from_agent_name=self.name,
            to_agent_name=target_agent_name,
            to_named_agent_type=named_agent_type,
            msg_correlation_id=correlation_id,
            requester_type=RequesterType.AGENT_REQUESTER,
            is_response_requested=True,
        ),
    )
    try:
        await self._send_operational_message(envelope)
        self._logger.debug(
            "[%s] event=agent_status_probe_published correlation_id=%s target_named_agent_type=%s "
            "operational_exchange=%s response_queue=%s timeout_seconds=%s",
            self.id,
            correlation_id,
            named_agent_type,
            operational_exchange,
            response_route,
            timeout,
        )
        return await asyncio.wait_for(future, timeout=timeout)
    except asyncio.TimeoutError:
        self._logger.debug(
            "[%s] event=agent_status_probe_timeout correlation_id=%s target_named_agent_type=%s "
            "timeout_seconds=%s",
            self.id,
            correlation_id,
            named_agent_type,
            timeout,
        )
        return None
    except Exception as exc:
        self._logger.exception(
            "[%s] Live AgentStatus probe for %s failed: %s "
            "event=agent_status_probe_failed correlation_id=%s target_named_agent_type=%s "
            "operational_exchange=%s response_queue=%s timeout_seconds=%s",
            self.id,
            named_agent_type,
            exc,
            correlation_id,
            named_agent_type,
            operational_exchange,
            response_route,
            timeout,
        )
        return None
    finally:
        self._pending_agent_status_futures.pop(correlation_id, None)


def _delegation_type_is_registered(self, named_agent_type: str) -> bool:
    """Whether a worker type exists in the team's current definition catalog.

    Read live (``LocalAgentTeamRuntime.agent_types``) because TeamArchitect can
    create a specialist type while the team runs
    (``custom_agents.generated_agent_provisioning``) and delegate to it at once.
    A newly seen type is added to this agent's route map so every later lookup
    agrees.
    """

    if named_agent_type in (self.named_agent_type_queue_map or {}):
        return True
    runtime = getattr(self, "_local_team_runtime", None)
    if runtime is None or named_agent_type not in runtime.agent_types():
        return False
    self.named_agent_type_queue_map[named_agent_type] = f"{self.team_id}_{named_agent_type}"
    return True


async def exact_delegation_route(
    self,
    named_agent_type: Optional[str],
    target_agent_name: str,
) -> Optional[str]:
    """The private mailbox (agent id) of one named worker, or None when it is not live.

    The team lead asks the worker for a live status first and uses the id it
    answers with, so a restarted worker is reached under its new id and no
    other worker is substituted; with ``named_agent_type`` the worker must also
    be of that type. Other agents have no live probe here and use the names
    they learned from presence.
    """

    if not self.is_team_lead:
        return self._agent_id_by_name.get(target_agent_name)
    verified_status = await self._probe_live_named_agent_type(named_agent_type, target_agent_name)
    if verified_status is None:
        self._logger.error(
            "[%s] Exact delegation target %s (%s) is not live; refusing to "
            "substitute a different worker. event=exact_delegation_target_unavailable",
            self.id,
            target_agent_name,
            named_agent_type,
        )
        return None
    return str(verified_status.self_id)


async def _verify_delegation_target(
    self,
    named_agent_type: Optional[str],
    target_agent_name: Optional[str] = None,
) -> bool:
    """Whether a delegated task may be published to this target.

    A worker type needs only to exist in the team's catalog: the task waits in
    the type's shared queue until a free worker takes it, and the worker pool
    controller starts workers while tasks wait (ADR-019). Nothing is probed or
    started here; before 2026-10-04 this probed for a live worker and started
    one, and a later check failed tasks that were only waiting in a busy
    worker's inbox. An exact agent (``target_agent_name``) is reached at its own
    mailbox, so it must answer a live status probe (``exact_delegation_route``).
    """

    if target_agent_name:
        return await exact_delegation_route(self, named_agent_type, target_agent_name) is not None
    if not named_agent_type or not self.is_team_lead:
        return True
    if _delegation_type_is_registered(self, str(named_agent_type)):
        return True
    self._logger.error(
        "[%s] Delegation target type %s is not a registered agent type; the task was "
        "not published. event=delegation_target_type_unknown",
        self.id,
        named_agent_type,
    )
    return False


async def _create_subtask_request(
    self,
    llm_comms_message: CommsMessage,
) -> Optional[TaskInfo]:
    """
    Creates a new subtask based on LLM output (formatted as CommsMessage).

    Args:
        llm_comms_message (CommsMessage): CommsMessage from the LLM.

    Returns:
        Optional[TaskInfo]: The created TaskInfo object for the subtask, or None on failure.
    """
    parent_task_id = self.task_manager.current_task_id
    if not parent_task_id:
        self._logger.error(
            f"[{self.id}] Cannot create subtask: No current parent task in progress."
        )
        await self._record_delegation_subtask_abort(
            failure_phase="missing_current_parent",
            parent_task_id=None,
            candidate_task_id=None,
            target_named_agent_type=None,
            target_agent_name=None,
            detail="No current parent task in progress.",
        )
        return None

    parent_task_info = await self.task_manager.operations_runtime.run(
        self.task_manager.get_task_info, parent_task_id
    )
    if not parent_task_info:
        self._logger.error(
            f"[{self.id}] Cannot create subtask: Parent task info not found."
        )
        await self._record_delegation_subtask_abort(
            failure_phase="parent_task_missing",
            parent_task_id=parent_task_id,
            candidate_task_id=None,
            target_named_agent_type=None,
            target_agent_name=None,
            detail="Parent task info was not found in the local TaskManager.",
        )
        return None

    if not isinstance(llm_comms_message.msg_content, Task):
        self._logger.error(
            f"[{self.id}] LLM output for subtask creation is not a Task object."
        )
        await self._record_delegation_subtask_abort(
            failure_phase="invalid_llm_subtask_content",
            parent_task_id=parent_task_id,
            candidate_task_id=None,
            target_named_agent_type=getattr(
                llm_comms_message.msg_meta, "to_named_agent_type", None
            ),
            target_agent_name=getattr(
                llm_comms_message.msg_meta, "to_agent_name", None
            ),
            detail=f"Expected Task content, got {type(llm_comms_message.msg_content).__name__}.",
        )
        return None

    sub_task_to_create: Task = llm_comms_message.msg_content
    llm_meta: Optional[MessageMeta] = llm_comms_message.msg_meta
    if (
        not sub_task_to_create.task_id
        or await self.task_manager.operations_runtime.run(
            self.task_manager.get_task_info, sub_task_to_create.task_id
        )
        is not None
    ):
        original_task_id = sub_task_to_create.task_id or "(missing)"
        index = 1
        while True:
            candidate_task_id = f"{parent_task_id}_sub_{index}"
            if (
                await self.task_manager.operations_runtime.run(
                    self.task_manager.get_task_info, candidate_task_id
                )
                is None
            ):
                break
            index += 1
        self._logger.warning(
            "[%s] Runtime corrected delegated Task request task_id from '%s' to '%s' "
            "to avoid local task-id collision with parent task '%s'.",
            self.id,
            original_task_id,
            candidate_task_id,
            parent_task_id,
        )
        sub_task_to_create.task_id = candidate_task_id

    # Populate subtask fields
    sub_task_to_create.parent_task_id = parent_task_id
    sub_task_to_create.task_type = MessageMetaType.REQUEST
    sub_task_to_create.answer = None
    sub_task_to_create.errors = None
    if not getattr(sub_task_to_create, "attachments", None):
        sub_task_to_create.attachments = [
            attachment.model_copy(deep=True)
            for attachment in list(
                getattr(parent_task_info.task, "attachments", None) or []
            )
        ]

    # Determine target agent
    target_agent_name = None
    to_named_agent_type_from_llm = None
    if llm_meta:
        target_agent_name = llm_meta.to_agent_name
        to_named_agent_type_from_llm = llm_meta.to_named_agent_type
    # A named worker is reached at its own mailbox, even when a type is also
    # given (the type then only checks that the worker is of that type); a
    # type alone goes to the type's shared queue. Before 2026-10-05 a type
    # always won, so a task meant for one worker went to the shared queue.
    exact_route: Optional[str] = None
    if target_agent_name:
        exact_route = await exact_delegation_route(
            self, to_named_agent_type_from_llm, target_agent_name
        )
        target_ok = exact_route is not None
    else:
        target_ok = await self._verify_delegation_target(to_named_agent_type_from_llm, None)
    if not target_ok:
        await self._record_delegation_subtask_abort(
            failure_phase="availability_verification_failed",
            parent_task_id=parent_task_id,
            candidate_task_id=sub_task_to_create.task_id,
            target_named_agent_type=to_named_agent_type_from_llm,
            target_agent_name=target_agent_name,
            detail="The target is not a registered agent type, or the exact target agent is not live.",
        )
        return None

    # Create header and meta for subtask
    subtask_header = await construct_header(
        comms_message=llm_comms_message,
        explicit_to_path=exact_route,
        route_response_to_self=True,
        agent_id=self.id,
        agent_name=self.name,
        team_id=self.team_id,
        named_agent_type_queue_map=self.named_agent_type_queue_map,
        agent_id_by_name=self._agent_id_by_name,
        logger=self._logger,
        repair_recorder=getattr(self, "_record_runtime_repair_anomaly", None),
    )
    try:
        self._validate_outbound_request_routing(
            content=sub_task_to_create,
            header=subtask_header,
            meta=llm_meta,
        )
    except RuntimeError as exc:
        self._logger.error(f"[{self.id}] Cannot create subtask: {exc}")
        await self._record_delegation_subtask_abort(
            failure_phase="routing_validation_failed",
            parent_task_id=parent_task_id,
            candidate_task_id=sub_task_to_create.task_id,
            target_named_agent_type=to_named_agent_type_from_llm,
            target_agent_name=target_agent_name,
            detail=str(exc),
        )
        return None

    correlation_id = None
    if parent_task_info.meta and parent_task_info.meta.msg_correlation_id:
        correlation_id = parent_task_info.meta.msg_correlation_id
    elif parent_task_info.header:
        correlation_id = parent_task_info.header.message_id
    else:
        correlation_id = new_id("corr_id")

    subtask_meta = MessageMeta(
        from_agent_name=self.name,
        to_agent_name=target_agent_name,
        msg_correlation_id=correlation_id,
        conversation_id=getattr(parent_task_info.meta, "conversation_id", None),
        objective_id=getattr(parent_task_info.meta, "objective_id", None),
        request_timezone=getattr(parent_task_info.meta, "request_timezone", None),
        gateway_context=(
            parent_task_info.meta.gateway_context.model_copy(deep=True)
            if parent_task_info.meta
            and parent_task_info.meta.gateway_context is not None
            else None
        ),
        requester_type=RequesterType.AGENT_REQUESTER,
        is_response_requested=True,
        to_named_agent_type=to_named_agent_type_from_llm,
        manager_escalation=getattr(llm_meta, "manager_escalation", None)
        if llm_meta
        else None,
    )

    # Add subtask to TaskManager
    task_info = await self.task_manager.operations_runtime.run(
        self.task_manager.add_subtask,
        parent_task_id,
        sub_task_to_create,
        subtask_header,
        subtask_meta,
    )
    if task_info:
        return task_info
    else:
        self._logger.error(f"[{self.id}] Failed to add subtask to TaskManager.")
        await self._record_delegation_subtask_abort(
            failure_phase="task_manager_registration_failed",
            parent_task_id=parent_task_id,
            candidate_task_id=sub_task_to_create.task_id,
            target_named_agent_type=to_named_agent_type_from_llm,
            target_agent_name=target_agent_name,
            detail="TaskManager.add_subtask returned None.",
        )
        return None


@staticmethod
def _task_request_transport_copy(task: Task) -> Task:
    return task_request_transport_copy(task)


@staticmethod
def _task_response_history_transport_copy(task: Task) -> list:
    return task_response_history_transport_copy(task)
