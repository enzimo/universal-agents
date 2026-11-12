"""Delegation helpers: target checks, subtask registration and status probes.

Delegated tasks for a worker type are published to the type's shared queue and
wait there until a free worker takes one (ADR-019); ``delegated_work_runtime``
tracks them after publication.

A delegated task gets the files of the message of the request the delegating
task serves (its own message; for a plan step or an isolated recovery task, its
request's), or exactly the files the team lead names with ``attachment_ids``
(``selected_attachments``): files of its request, files the person sent with
guidance or an answer, and earlier files of its conversation, found within the
same scope the lead's prompt and tools use.
"""

from __future__ import annotations

import logging
from custom_tools.runtime_context import bound_task_id
from datetime import datetime, timezone
from typing import Any, Optional

from custom_agents.access_runtime import verified_person_id
from custom_agents.agent_requests import AgentUnreachable
from custom_agents.conversation_attachments import (
    AttachmentSelectionError,
    request_files,
    select_attachments,
    task_attachment_scope,
)
from custom_agents.runtime_paths import agent_layout
from custom_messages.contracts.attachments_v1 import UserTaskAttachment
from custom_agents.message_routing import construct_header
from custom_agents.task_manager import TaskInfo
from custom_agents.task_transport import (
    task_request_transport_copy,
    task_response_history_transport_copy,
)
from custom_messages.contracts.control_v1 import AgentStatus
from custom_messages.contracts.envelope_v1 import CommsMessage
from custom_messages.contracts.routing_v1 import MessageMeta
from custom_messages.contracts.task_v1 import Task
from custom_messages.enums import (
    MessageMetaType,
    RequesterType,
    TaskHistoryImportance,
    TaskHistorySource,
    TaskHistoryType,
)
from id_generation import new_id

logger = logging.getLogger(__name__)


async def selected_attachments(agent: Any, attachment_ids: Optional[list[str]]) -> Optional[list[UserTaskAttachment]]:
    """The files ``attachment_ids`` names for a task delegated from the current task; None when it names none.

    ``None`` (left out) means the new task gets the files of its request's
    message (``_give_subtask_files``). A list, ``[]`` included, is resolved
    with ``select_attachments`` in the scope of the task being handled: the
    one the team lead's prompt lists and its tools open, with the person from
    the task's signed context. An unknown, removed or excess id raises
    ``AttachmentSelectionError`` before the task is created; the outbox puts
    its message, which lists the files that can be passed, before the model.
    """

    if attachment_ids is None:
        return None
    task_id = bound_task_id()
    if not task_id:
        raise RuntimeError("Files can be chosen only for a task delegated while a task is handled.")
    task_manager = agent.task_manager
    conversation_store = getattr(agent, "_conversation_store", None)
    is_team_lead = bool(getattr(agent, "is_team_lead", False))
    person_id = verified_person_id(agent)
    uploads_dir = agent_layout(agent).uploads_dir

    def select() -> list[UserTaskAttachment]:
        scope = task_attachment_scope(
            task_manager, task_id, conversation_store=conversation_store, is_team_lead=is_team_lead, person_id=person_id
        )
        if scope is None:
            raise RuntimeError(f"Task {task_id} is no longer held, so no files can be chosen from it.")
        return select_attachments(scope, list(attachment_ids), uploads_dir=uploads_dir)

    try:
        # The task's request links and history are read in the task-manager lane.
        chosen: list[UserTaskAttachment] = await task_manager.operations_runtime.run(select)
        return chosen
    except AttachmentSelectionError as error:
        logger.warning(
            "A delegation named files the task may not pass; nothing was delegated. "
            "event=delegation_attachment_selection_refused agent_id=%s task_id=%s named=%d unknown_ids=%r deleted_ids=%r",
            getattr(agent, "id", None),
            task_id,
            len(attachment_ids),
            [value[:128] for value in error.unknown_ids[:10]],
            error.deleted_ids[:10],
        )
        raise


def _parent_and_message_files(
    task_manager: Any, task_id: str
) -> tuple[Optional[TaskInfo], list[UserTaskAttachment]]:
    """The delegating task and the files of the message of the request it serves; (None, []) for a task not held.

    The files of the task's own message for a request itself or an escalation;
    for a plan step or an isolated recovery task, which carry no files of their
    own, the request's (``conversation_attachments.request_files``). Read
    together in one task-manager lane operation.
    """

    info = task_manager.get_task_info(task_id)
    request = request_files(task_manager, task_id) if info is not None else None
    return info, (list(request.message_files) if request is not None else [])


def _give_subtask_files(
    self: Any,
    subtask: Task,
    parent: Task,
    selected: Optional[list[UserTaskAttachment]],
    message_files: list[UserTaskAttachment],
) -> None:
    """Give a new subtask the files the team lead chose or, when it chose none (None), its request's message files.

    ``message_files`` (``_parent_and_message_files``) are the parent's own
    files, or for a plan step or recovery task its request's.
    """

    own = list(message_files)
    if selected is None:
        if not subtask.attachments:
            subtask.attachments = [attachment.model_copy(deep=True) for attachment in own]
        return
    subtask.attachments = [attachment.model_copy(deep=True) for attachment in selected]
    chosen_ids = {attachment.attachment_id for attachment in selected}
    self._logger.info(
        "A delegated task gets the files the team lead chose. event=delegation_attachments_selected "
        "parent_task_id=%s subtask_id=%s attachment_ids=%s left_out_message_ids=%s",
        parent.task_id,
        subtask.task_id,
        ",".join(attachment.attachment_id for attachment in selected),
        ",".join(attachment.attachment_id for attachment in own if attachment.attachment_id not in chosen_ids),
    )


def _resolve_manager_identity(self) -> tuple[Optional[str], Optional[str]]:
    if self.is_team_lead:
        return self.id, self.name
    for status in self.known_agents.agent_map.values():
        if status.team_id == self.team_id and status.is_team_lead:
            return status.self_id, status.name
    return None, None


# A delegation by name refused because the requester itself cannot ask yet (``exact_delegation_route``).
REQUESTER_CANNOT_ASK = "requester_cannot_ask"


def requester_cannot_ask_detail(error: Exception) -> str:
    """What the requester is told when it cannot ask a named worker: its own reason, not the worker's.

    Whether trying again can help depends on whether the reason clears by itself
    (``AgentUnreachable.clears``): a session still opening does; a process that drains or
    runs without a message host does not.
    """

    if getattr(error, "clears", True):
        return (f"This agent cannot send requests yet ({error}); the named worker was not checked. "
                "Try again shortly.")
    return (f"This agent cannot send requests ({error}); the named worker was not checked. That will not change "
            "while this process runs: delegate by type, do it another way, or tell the person.")


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
    """A live agent of the type (or with the name) that accepts work, or None.

    By type, the team's agent records answer at once (``custom_agents.team_directory``):
    nothing is sent and nothing waits, because work for a type waits in the type's queue.
    By name, the agent must take the task from its own mailbox, so the lookup waits a
    bounded time for a just-started agent to appear and then asks it for its status: one
    that does not answer is refused at once (``team_directory.live_named_agent``). By name,
    ``AgentUnreachable`` is raised when this agent itself cannot ask yet.
    """

    from custom_agents.team_directory import live_agent_status, live_named_agent

    if not self.team_id or not (named_agent_type or target_agent_name):
        return None
    if target_agent_name:
        status = await live_named_agent(self, named_agent_type, target_agent_name)
    else:
        status = live_agent_status(self, named_agent_type)
    self._logger.debug(
        "[%s] event=agent_liveness_lookup target_named_agent_type=%s target_agent_name=%s found_agent_id=%s",
        self.id,
        named_agent_type,
        target_agent_name,
        status.self_id if status is not None else None,
    )
    return status


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

    The team lead finds the worker by name in the team's agent records, waiting a
    bounded time for one just started, and asks it for its status; it uses the id
    of the worker that answered, so a restarted worker is reached under its new id
    and no other worker is substituted, and a worker that does not answer is refused
    at once. With ``named_agent_type`` the worker must also be of that type. Other
    agents get the same bounded wait and the records' checks (it takes work, it is
    of that type) without asking: the contract lets only the lead's host, the team
    services and the gateway ask for a status. Raises ``AgentUnreachable`` when the team lead
    itself cannot ask yet (``team_directory.live_named_agent``): the worker was not checked.
    """

    if not self.is_team_lead:
        from custom_agents.team_directory import live_named_agent

        found = await live_named_agent(self, named_agent_type, target_agent_name, ask=False)
        if found is None:
            self._logger.error(
                "[%s] Exact delegation target %s (%s) is not live or takes no work; refusing to "
                "substitute a different worker. event=exact_delegation_target_unavailable",
                self.id,
                target_agent_name,
                named_agent_type,
            )
            return None
        return str(found.self_id)
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
    mailbox, so for the team lead it must be in the team's records and answer its
    status (``exact_delegation_route``).
    """

    if target_agent_name:
        try:
            return await exact_delegation_route(self, named_agent_type, target_agent_name) is not None
        except AgentUnreachable:
            return False  # this agent cannot ask yet; logged with its reason (team_directory.live_named_agent)
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
    *,
    chosen_files: Optional[list[UserTaskAttachment]] = None,
) -> Optional[TaskInfo]:
    """
    Creates a new subtask based on LLM output (formatted as CommsMessage).

    Args:
        llm_comms_message (CommsMessage): CommsMessage from the LLM.
        chosen_files: the files the team lead chose (``selected_attachments``),
            ``[]`` for none; None gives the subtask the files of the message of
            the parent task's request (``_give_subtask_files``).

    Returns:
        Optional[TaskInfo]: The created TaskInfo object for the subtask, or None on failure.
    """
    parent_task_id = bound_task_id()
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

    task_manager = self.task_manager
    # The lane copies an operation's arguments, so the manager is not passed as one.
    parent_task_info, message_files = await task_manager.operations_runtime.run(
        lambda: _parent_and_message_files(task_manager, parent_task_id)
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
    _give_subtask_files(self, sub_task_to_create, parent_task_info.task, chosen_files, message_files)

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
        try:
            exact_route = await exact_delegation_route(
                self, to_named_agent_type_from_llm, target_agent_name
            )
        except AgentUnreachable as error:
            # It is this agent that cannot ask yet, not the worker that is gone.
            await self._record_delegation_subtask_abort(
                failure_phase=REQUESTER_CANNOT_ASK,
                parent_task_id=parent_task_id,
                candidate_task_id=sub_task_to_create.task_id,
                target_named_agent_type=to_named_agent_type_from_llm,
                target_agent_name=target_agent_name,
                detail=requester_cannot_ask_detail(error),
            )
            return None
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
