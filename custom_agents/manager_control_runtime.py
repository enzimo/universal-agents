"""Deterministic manager control handlers for gateway-originated NATS requests."""

from __future__ import annotations

from access_control.ownership import (
    OwnershipDecision,
    PRIVATE_CREATE,
    PRIVATE_DELETE,
    PRIVATE_READ,
    PRIVATE_UPDATE,
    RESOURCE_CONVERSATION,
    RESOURCE_OBJECTIVE,
    RESOURCE_SCHEDULE,
    RESOURCE_TASK,
    RESOURCE_TEAM_MEMORY,
    RESOURCE_USER_MEMORY,
    private_access,
)

from dataclasses import dataclass
from functools import partial
from typing import Any, Optional

from access_control.entities import PersonPrincipal
from access_control.identity import ISSUER_GATEWAY, IdentityRejected
from custom_agents.task_context_runtime import context_verifier
from custom_agents.pinned_memory_runtime import (
    PinnedMemoryActor,
    PinnedMemoryCommand,
    PinnedMemoryRequestContext,
    PinnedMemoryService,
    PinnedMemorySource,
    pinned_memory_reply,
    render_pinned_memory_result,
)
from custom_agents.control_runtime import stop_task_execution
from custom_agents.task_human_interaction_blockers import (
    blocker_request_ids,
    get_human_interaction_blocker,
)
from custom_agents.heartbeat_store import HeartbeatStore
from custom_agents.objective_lifecycle import (
    LinkSwitchResult,
    ObjectiveLifecycleOutcome,
    ObjectiveStatusChange,
    apply_objective_lifecycle,
)
from custom_agents.schedule_management import (
    ScheduleMutationPermit,
    ScheduleMutationAuthorityMode,
    ScheduleMutationOperation,
    SystemScheduleMutationDenied,
    build_schedule_mutation_permit,
    is_system_managed_schedule,
)
from custom_agents.schedule_store import (
    ScheduleStore,
    ScheduleWriteConflict,
    owner_canonical_user_id_for_schedule,
)
from custom_agents.task_manager_objectives import ObjectiveSaveFailed
from custom_messages.contracts.common_v1 import contract_sha256
from custom_messages.contracts.control_v1 import (
    ABANDON_OBJECTIVE,
    DELETE_OBJECTIVE,
    OBJECTIVE_ACTION_UNAVAILABLE,
    OBJECTIVE_ALREADY_DELETED,
    OBJECTIVE_CHANGED,
    OBJECTIVE_CONTROL_INCOMPLETE,
    OBJECTIVE_CONTROL_WORDS,
    OBJECTIVE_DELETED,
    OBJECTIVE_DELETION_INCOMPLETE,
    OBJECTIVE_NOT_FOUND,
    OBJECTIVE_OWNER_MISMATCH,
    OBJECTIVE_PART_TASK,
    OBJECTIVE_UPDATE_FAILED,
    PAUSE_OBJECTIVE,
    RESUME_OBJECTIVE,
    STOP_OBJECTIVE,
    ManagerMemoryCommand,
    ManagerRuntimeWorkCommand,
    ManagerTopicCommand,
    RuntimeWorkCancellationReceipt,
    RuntimeWorkChildSummary,
    RuntimeObjectiveSubgoal,
    RuntimeObjectiveSummary,
    RuntimeWorkScheduleSummary,
    RuntimeWorkSummary,
)
from custom_messages.contracts.envelope_v1 import MessageEnvelope
from custom_messages.contracts.routing_v1 import (
    MessageHeader,
    MessageMeta,
)
from custom_messages.contracts.schedule_v1 import (
    SCHEDULE_CHANGED_SINCE_LISTED,
    SYSTEM_SCHEDULE_CHANGE_NOT_ALLOWED,
    SYSTEM_SCHEDULE_CHANGE_REFUSED,
    ManagerScheduleCommand,
    ScheduledMessage,
    ScheduleWorkerAssignmentV1,
)
from custom_messages.contracts.task_v1 import TaskProgressChild, TaskProgressStatus
from custom_messages.enums import (
    MessageMetaType,
    ObjectiveStatus,
    ProgressState,
    RequesterType,
)
from custom_messages.records.objectives import ObjectiveHeartbeatLink, ObjectiveScheduleLink
from storage.pinned_memory_repository import PinnedMemoryConflictError


@dataclass(frozen=True)
class CommandPerson:
    """The person a gateway command acts for, taken only from its signed context.

    The gateway signs the signed-in person's task context (person, team and
    current roles) onto every command it sends the manager
    (``request_context.build_gateway_context``). Until 2026-10-07 the handlers
    compared a person written in the command body with an unsigned copy in the
    same message and trusted the body's roles and a "may change system
    schedules" flag, so anything able to publish to the control subject could
    claim any person and role.
    """

    canonical_user_id: str
    team_id: str
    roles: tuple[str, ...]


def verified_command_person(
    agent: Any, msg_meta: MessageMeta
) -> tuple[Optional[CommandPerson], Optional[str]]:
    """The command's person from its gateway-signed context, or why it is refused."""

    gateway_context = getattr(msg_meta, "gateway_context", None)
    signed = getattr(gateway_context, "task_context", None)
    refusal: Optional[str] = None
    try:
        context = context_verifier().verify(signed, team_id=str(getattr(agent, "team_id", "") or ""))
    except IdentityRejected as exc:
        refusal = f"unsigned_sender: {exc}"
    else:
        if context.issuer != ISSUER_GATEWAY:
            refusal = "sender_not_gateway"
        elif not context.person_id:
            refusal = "person_required"
        elif not context.person_roles:
            refusal = "active_team_membership_required"
    if refusal is not None:
        agent._logger.warning(
            "[%s] Refused a gateway command without a valid gateway-signed person. "
            "event=manager_command_sender_refused reason=%s claimed_person_id=%s",
            getattr(agent, "id", None),
            refusal,
            getattr(gateway_context, "canonical_user_id", None),
        )
        return None, refusal
    claimed = str(getattr(gateway_context, "canonical_user_id", "") or "").strip()
    if claimed and claimed != context.person_id:
        agent._logger.warning(
            "[%s] A gateway command names a person its signed context does not; using the signed one. "
            "event=manager_command_person_mismatch claimed_person_id=%s person_id=%s",
            getattr(agent, "id", None),
            claimed,
            context.person_id,
        )
    return (
        CommandPerson(
            canonical_user_id=str(context.person_id),
            team_id=context.team_id,
            roles=tuple(context.person_roles),
        ),
        None,
    )


def _may_change_system_schedules(agent: Any, person: CommandPerson) -> bool:
    """Whether the person may change schedules the runtime manages.

    The same decision the gateway's ``PersonChangeCheck`` makes for its own
    controls (people who could approve a ``system_schedule`` change), made here
    with the verified person and roles instead of trusting a flag in the command.
    With authorization off, any active team member may.
    """

    from access_control.inbox import StoredAccessInbox
    from access_control.requests import AccessRequests
    from custom_agents.access_runtime import runtime_access
    from custom_messages.contracts.runtime_v1 import TEAM_ARCHITECT_AGENT_TYPE
    from custom_messages.contracts.schedule_v1 import SYSTEM_SCHEDULE_CHANGE_KIND

    access = runtime_access(agent)
    if access.service is None:
        return True
    requests = AccessRequests(
        service=access.service,
        inbox=StoredAccessInbox(access.service.store),
        lead_agent_type=TEAM_ARCHITECT_AGENT_TYPE,
    )
    return requests.may_change_directly(
        PersonPrincipal(person.canonical_user_id, person.team_id, person.roles),
        SYSTEM_SCHEDULE_CHANGE_KIND,
    )


async def _send_durable_response(
    agent: Any,
    *,
    content: Any,
    incoming_header: MessageHeader,
    incoming_meta: MessageMeta,
) -> None:
    response_key = incoming_header.response_routing_key
    if not response_key:
        agent._logger.warning(
            "[%s] Manager control response omitted because request %s had no response route.",
            agent.id,
            content.get_id(),
        )
        return
    envelope = MessageEnvelope(
        msg_content=content,
        msg_header=MessageHeader(
            from_agent_id=agent.id,
            to_routing_path=response_key,
            response_routing_key=agent.id,
            agent_signature="",
            idempotency_key=f"{content.get_id()}:response",
        ),
        msg_meta=MessageMeta(
            from_agent_name=agent.name,
            msg_correlation_id=incoming_meta.msg_correlation_id,
            conversation_id=incoming_meta.conversation_id,
            requester_type=RequesterType.AGENT_REQUESTER,
            is_response_requested=False,
            request_timezone=incoming_meta.request_timezone,
            gateway_context=incoming_meta.gateway_context,
        ),
    )
    await agent.team_comms.send_message(message=envelope, routing_key=response_key)


def _schedule_owned_by(
    scheduled_message: Any,
    *,
    canonical_user_id: str,
    action: str,
) -> bool:
    if scheduled_message is None:
        return False
    owner = owner_canonical_user_id_for_schedule(scheduled_message)
    return private_access(
        person_id=canonical_user_id,
        owner_id=owner,
        resource_type=RESOURCE_SCHEDULE,
        resource_id=scheduled_message.schedule_id,
        action=action,
    ).allowed


def _authenticated_gateway_schedule_permit(
    scheduled_message: Any,
    *,
    principal: CommandPerson,
    operation: ScheduleMutationOperation,
    candidate: Any = None,
) -> Optional[ScheduleMutationPermit]:
    return build_schedule_mutation_permit(
        schedule=scheduled_message,
        operation=operation,
        authority_mode=ScheduleMutationAuthorityMode.AUTHENTICATED_GATEWAY_USER,
        actor_principal_id=principal.canonical_user_id,
        owner_canonical_user_id=principal.canonical_user_id,
        team_id=principal.team_id,
        candidate=candidate,
    )


def _schedule_worker_assignment(
    store: ScheduleStore, schedule: ScheduledMessage, owner: str
) -> ScheduleWorkerAssignmentV1:
    """Project a schedule already authorized for this owner, in its saved team.

    Owner-wide inventory can include another team in shared storage. The viewing
    manager's team must not replace the schedule's persisted binding identity.
    """
    from datetime import datetime, timezone

    from custom_agents.local_schedule_policy import local_system_timer_spec
    from custom_agents.schedule_repair_guidance import person_explanation
    from custom_agents.stuck_schedules import not_running

    # A system timer an on-demand worker runs names its agent type (memory review: MemReviewAgent).
    system_timer = local_system_timer_spec(schedule)
    stuck = not_running(store, schedule, now=datetime.now(timezone.utc))
    binding = (
        store.worker_records.get_binding(
            schedule.schedule_id, team_id=schedule.claim_team_id, owner_id=owner,
        )
        if schedule.claim_team_id else None
    )
    return ScheduleWorkerAssignmentV1(
        agent_type=schedule.claim_named_agent_type,
        agent_id=schedule.claim_agent_id,
        review_agent_type=system_timer.agent_type if system_timer else None,
        upgrade_required=system_timer is not None and schedule.claim_named_agent_type != system_timer.agent_type,
        automatic_creation=(
            "not_configured" if binding is None
            else "disabled" if not binding.policy.enabled
            else "default_policy" if binding.authority_mode == "runtime_default"
            else "enabled"
        ),
        desired_agent_type=binding.target_agent_type if binding else None,
        reassignment_pending=bool(binding and (
            schedule.claim_agent_id
            or schedule.claim_named_agent_type != binding.target_agent_type
        )),
        not_running_since=stuck[0] if stuck else None,
        not_running_explanation=person_explanation(stuck[1]) if stuck else None,
    )


async def _apply_gateway_schedule_change(
    agent: Any,
    store: Any,
    command: ManagerScheduleCommand,
    scheduled_message: Any,
    person: CommandPerson,
) -> tuple[bool, str]:
    """Pause, resume, turn off or remove one schedule for the verified person: (applied, message)."""

    schedule_id = command.schedule_id or ""
    expected = command.expected_record_digest or None
    run = agent.task_manager.operations_runtime.run
    if command.action == "pause":
        candidate = scheduled_message.model_copy(deep=True)
        candidate.paused_until = command.paused_until
        permit = _authenticated_gateway_schedule_permit(
            scheduled_message, principal=person, operation=ScheduleMutationOperation.PAUSE, candidate=candidate
        )
        applied = await run(
            store.set_paused_until,
            schedule_id,
            command.paused_until,
            operation=ScheduleMutationOperation.PAUSE,
            system_mutation_permit=permit,
            expected_record_digest=expected,
        )
        return applied, "schedule_paused" if applied else "schedule_not_found"
    if command.action == "resume":
        candidate = scheduled_message.model_copy(deep=True)
        candidate.paused_until = None
        candidate.enabled = True
        permit = _authenticated_gateway_schedule_permit(
            scheduled_message, principal=person, operation=ScheduleMutationOperation.RESUME, candidate=candidate
        )
        applied = await run(store.resume, schedule_id, system_mutation_permit=permit, expected_record_digest=expected)
        return applied, "schedule_resumed" if applied else "schedule_not_found"
    if command.action == "disable":
        candidate = scheduled_message.model_copy(deep=True)
        candidate.enabled = False
        permit = _authenticated_gateway_schedule_permit(
            scheduled_message, principal=person, operation=ScheduleMutationOperation.DISABLE, candidate=candidate
        )
        applied = await run(
            store.set_enabled,
            schedule_id,
            False,
            operation=ScheduleMutationOperation.DISABLE,
            system_mutation_permit=permit,
            expected_record_digest=expected,
        )
        return applied, "schedule_disabled" if applied else "schedule_not_found"
    permit = _authenticated_gateway_schedule_permit(
        scheduled_message, principal=person, operation=ScheduleMutationOperation.DELETE
    )
    applied = await run(store.remove, schedule_id, system_mutation_permit=permit, expected_record_digest=expected)
    return applied, "schedule_removed" if applied else "schedule_not_found"


async def handle_manager_schedule_command(
    agent: Any,
    *,
    command: ManagerScheduleCommand,
    msg_header: MessageHeader,
    msg_meta: MessageMeta,
) -> None:
    if command.command_type == MessageMetaType.RESPONSE:
        return
    response = command.model_copy(deep=True)
    response.command_type = MessageMetaType.RESPONSE
    response.schedules = []
    response.worker_assignments = {}
    if not getattr(agent, "is_team_lead", False):
        response.success = False
        response.message = "manager_required"
        await _send_durable_response(
            agent, content=response, incoming_header=msg_header, incoming_meta=msg_meta
        )
        return
    person, refusal = verified_command_person(agent, msg_meta)
    if person is None:
        response.success = False
        response.message = refusal or "unsigned_sender"
        await _send_durable_response(
            agent, content=response, incoming_header=msg_header, incoming_meta=msg_meta
        )
        return

    store = agent._ensure_schedule_store()
    owner = person.canonical_user_id
    if command.action == "list":
        for row in await agent.task_manager.operations_runtime.run(store.get_all):
            schedule_id = str(row.get("schedule_id") or "").strip()
            scheduled_message = (
                await agent.task_manager.operations_runtime.run(
                    store.get_scheduled_message, schedule_id
                )
                if schedule_id
                else None
            )
            if scheduled_message is None or not _schedule_owned_by(
                scheduled_message,
                canonical_user_id=owner,
                action=PRIVATE_READ,
            ):
                continue
            gateway_context = getattr(
                getattr(scheduled_message, "payload_meta", None),
                "gateway_context",
                None,
            )
            if (
                command.session_key
                and getattr(gateway_context, "session_key", None) != command.session_key
            ):
                continue
            response.schedules.append(scheduled_message)
            response.worker_assignments[schedule_id] = await agent.task_manager.operations_runtime.run(
                _schedule_worker_assignment, store, scheduled_message, owner,
            )
        response.success = True
        response.message = "schedule_listed"
    else:
        scheduled_message = await agent.task_manager.operations_runtime.run(
            store.get_scheduled_message, command.schedule_id or ""
        )
        auth_action = (
            PRIVATE_DELETE
            if command.action == "remove"
            else PRIVATE_UPDATE
        )
        if not _schedule_owned_by(
            scheduled_message,
            canonical_user_id=owner,
            action=auth_action,
        ):
            response.success = False
            response.message = "schedule_not_found"
        elif (
            is_system_managed_schedule(scheduled_message)
            # Storage and Cedar may wait. Bind the live service owner so the
            # bounded operation lane copies only the verified person, keeping
            # the manager loop responsive and the decision before mutation.
            and not await agent.task_manager.operations_runtime.run(
                partial(_may_change_system_schedules, agent), person
            )
        ):
            # Only people who could approve a system_schedule change may make
            # one here, decided with the verified person and roles.
            agent._logger.warning(
                "[%s] Refused a gateway change to a system schedule. "
                "event=gateway_system_schedule_change_refused schedule_id=%s action=%s person_id=%s",
                agent.id,
                command.schedule_id,
                command.action,
                owner,
            )
            response.success = False
            response.message = SYSTEM_SCHEDULE_CHANGE_NOT_ALLOWED
        else:
            # The listed schedule the person acted on (expected_record_digest)
            # and the permit are checked in the store's write transaction.
            try:
                response.success, response.message = await _apply_gateway_schedule_change(
                    agent, store, command, scheduled_message, person
                )
            except (ScheduleWriteConflict, SystemScheduleMutationDenied) as exc:
                changed = isinstance(exc, ScheduleWriteConflict)
                agent._logger.warning(
                    "[%s] A gateway schedule change was refused. event=gateway_schedule_change_refused "
                    "schedule_id=%s action=%s person_id=%s reason=%s",
                    agent.id,
                    command.schedule_id,
                    command.action,
                    owner,
                    exc.reason_code,
                )
                response.success = False
                response.message = SCHEDULE_CHANGED_SINCE_LISTED if changed else SYSTEM_SCHEDULE_CHANGE_REFUSED
        if response.success and getattr(agent, "_scheduler_wake", None) is not None:
            agent._scheduler_wake.set()
    await _send_durable_response(
        agent, content=response, incoming_header=msg_header, incoming_meta=msg_meta
    )


def _authorize_manager_memory_command(
    command: ManagerMemoryCommand,
    *,
    person: CommandPerson,
) -> OwnershipDecision:
    payload = command.command
    action = {
        "list": PRIVATE_READ,
        "show": PRIVATE_READ,
        "create": PRIVATE_CREATE,
        "delete": PRIVATE_DELETE,
    }.get(payload.action, PRIVATE_UPDATE)
    is_team_memory = payload.bank == "coremem"
    return private_access(
        person_id=person.canonical_user_id,
        owner_id=(
            None if is_team_memory else person.canonical_user_id
        ),
        resource_type=(
            RESOURCE_TEAM_MEMORY if is_team_memory else RESOURCE_USER_MEMORY
        ),
        resource_id=(
            f"{payload.bank}:{payload.memory_id or payload.event_id or 'collection'}"
        ),
        action=action,
        team_id=person.team_id,
        roles=tuple(person.roles),
    )


async def handle_manager_memory_command(
    agent: Any,
    *,
    command: ManagerMemoryCommand,
    msg_header: MessageHeader,
    msg_meta: MessageMeta,
) -> None:
    if command.command_type == MessageMetaType.RESPONSE:
        return
    response = command.model_copy(deep=True)
    response.command_type = MessageMetaType.RESPONSE
    person, refusal = verified_command_person(agent, msg_meta)
    if not getattr(agent, "is_team_lead", False) or person is None:
        response.success = False
        response.rendered_result = refusal or "manager_required"
        await _send_durable_response(
            agent, content=response, incoming_header=msg_header, incoming_meta=msg_meta
        )
        return

    gateway_context = msg_meta.gateway_context
    payload = command.command
    decision = _authorize_manager_memory_command(command, person=person)
    if not decision.allowed:
        response.success = False
        response.error_code = "authorization_denied"
        response.rendered_result = decision.reason
        agent._logger.warning(
            "[%s] Manager denied pinned-memory command. "
            "event=manager_memory_authorization_denied actor=%s bank=%s "
            "action=%s reason=%s",
            agent.id,
            person.canonical_user_id,
            payload.bank,
            payload.action,
            decision.reason,
        )
        await _send_durable_response(
            agent,
            content=response,
            incoming_header=msg_header,
            incoming_meta=msg_meta,
        )
        return
    service = PinnedMemoryService(
        agent._pinned_memory_repository,
        config=dict(getattr(agent, "memory_config", {}) or {}),
        command_origin="gateway",
        logger=agent._logger,
    )
    parsed = PinnedMemoryCommand(**payload.model_dump())
    context = PinnedMemoryRequestContext(
        team_id=agent.team_id,
        canonical_user_id=person.canonical_user_id,
        request_timezone=msg_meta.request_timezone,
        source=PinnedMemorySource(
            conversation_id=msg_meta.conversation_id,
            gateway_session_key=getattr(gateway_context, "session_key", None),
            channel_name=getattr(gateway_context, "channel_name", None),
            chat_id=getattr(gateway_context, "chat_id", None),
            thread_id=getattr(gateway_context, "thread_id", None),
        ),
        actor=PinnedMemoryActor(
            kind="user",
            id=person.canonical_user_id,
        ),
    )
    try:
        result = service.execute(parsed, context=context)
        response.success = True
        response.result = pinned_memory_reply(result)
        response.rendered_result = render_pinned_memory_result(
            result,
            timezone_name=context.request_timezone,
        )
    except PinnedMemoryConflictError as exc:
        response.success = False
        response.error_code = "conflict"
        response.rendered_result = str(exc)
    except ValueError as exc:
        response.success = False
        response.error_code = "invalid_request"
        response.rendered_result = f"Pinned memory command failed: {exc}"
    except Exception as exc:
        agent._logger.warning(
            "[%s] Gateway pinned-memory command failed. event=manager_memory_command_failed action=%s",
            agent.id,
            payload.action,
            exc_info=True,
        )
        response.success = False
        response.error_code = "runtime_error"
        response.rendered_result = f"Pinned memory command failed: {exc}"
    await _send_durable_response(
        agent, content=response, incoming_header=msg_header, incoming_meta=msg_meta
    )


async def handle_manager_topic_command(
    agent: Any,
    *,
    command: ManagerTopicCommand,
    msg_header: MessageHeader,
    msg_meta: MessageMeta,
) -> None:
    if command.command_type == MessageMetaType.RESPONSE:
        return
    response = command.model_copy(deep=True)
    response.command_type = MessageMetaType.RESPONSE
    response.known_tags = []
    response.thread_matches = []
    response.applied_tags = []
    person, refusal = verified_command_person(agent, msg_meta)
    if not getattr(agent, "is_team_lead", False) or person is None:
        response.success = False
        response.message = refusal or "manager_required"
        await _send_durable_response(
            agent, content=response, incoming_header=msg_header, incoming_meta=msg_meta
        )
        return

    owner = person.canonical_user_id
    store = agent._conversation_store
    try:
        if command.action == "list_tags":
            response.known_tags = await agent.task_manager.operations_runtime.run(
                store.list_known_thread_tags,
                team_id=agent.team_id,
                canonical_user_id=owner,
            )
        elif command.action == "find_threads":
            response.thread_matches = await agent.task_manager.operations_runtime.run(
                store.find_threads_by_tags,
                tags=list(command.tags or ([command.tag] if command.tag else [])),
                team_id=agent.team_id,
                canonical_user_id=owner,
                limit=10,
            )
        else:
            thread = await agent.task_manager.operations_runtime.run(
                store.get_thread, command.conversation_id or ""
            )
            thread_owner = str(
                (thread or {}).get("owner_canonical_user_id") or ""
            ).strip()
            decision = private_access(
                person_id=owner,
                owner_id=thread_owner,
                resource_type=RESOURCE_CONVERSATION,
                resource_id=command.conversation_id or "unknown",
                action=PRIVATE_UPDATE,
                team_id=agent.team_id,
            )
            if not thread or not decision.allowed:
                raise ValueError("conversation not found")
            for tag in command.tags:
                row = await agent.task_manager.operations_runtime.run(
                    store.tag_thread,
                    conversation_id=command.conversation_id or "",
                    tag=tag,
                    team_id=agent.team_id,
                    canonical_user_id=owner if command.private else None,
                    source="gateway_command",
                    created_by_agent_id=agent.id,
                )
                response.applied_tags.append(str(row["tag"]))
        response.success = True
        response.message = "topic_command_completed"
    except Exception as exc:
        response.success = False
        response.message = str(exc)
    await _send_durable_response(
        agent, content=response, incoming_header=msg_header, incoming_meta=msg_meta
    )


def _task_owner_matches(task_info: Any, request: TaskProgressStatus) -> bool:
    """Whether the task's owner (recorded at admission) is the person the request names, in the same chat session."""
    meta = getattr(task_info, "meta", None)
    gateway_context = getattr(meta, "gateway_context", None)
    return bool(
        gateway_context
        and task_info.owner_person_id == request.canonical_user_id
        and getattr(gateway_context, "session_key", None) == request.session_key
        and getattr(meta, "conversation_id", None) == request.conversation_id
    )


def _latest_task_activity(task_info: Any) -> tuple[Optional[str], Optional[str]]:
    entries = list(getattr(task_info.task, "execution_history", None) or [])
    if entries:
        latest = max(
            entries, key=lambda entry: str(getattr(entry, "timestamp", "") or "")
        )
        summary = " ".join(str(getattr(latest, "summary", "") or "").split())
        return getattr(latest, "timestamp", None), summary[:240] or None
    states = list(getattr(task_info.task, "task_state", None) or [])
    if states:
        state, timestamp = states[-1]
        return timestamp, f"Task state changed to {getattr(state, 'value', state)}."
    return None, None


async def build_task_progress_response(
    agent: Any, request: TaskProgressStatus, msg_meta: MessageMeta
) -> TaskProgressStatus:
    response = request.model_copy(deep=True)
    response.status_type = MessageMetaType.RESPONSE
    response.responder_agent_id = agent.id
    response.responder_agent_name = agent.name
    response.children = []
    gateway_context = getattr(msg_meta, "gateway_context", None)
    if (
        not request.canonical_user_id
        or getattr(gateway_context, "canonical_user_id", None)
        != request.canonical_user_id
        or getattr(gateway_context, "session_key", None) != request.session_key
        or msg_meta.conversation_id != request.conversation_id
    ):
        response.status = "unauthorized"
        return response

    roots = [
        task_info
        for task_info in await agent.task_manager.operations_runtime.run(
            agent.task_manager.get_top_level_task_infos,
        )
        if _task_owner_matches(task_info, request)
    ]
    if not roots:
        response.status = "idle"
        return response
    root = max(
        roots,
        key=lambda info: str(
            (info.task.task_state[-1][1] if info.task.task_state else None)
            or getattr(info, "restored_runtime_updated_at", None)
            or ""
        ),
    )
    response.status = "active"
    response.root_task_id = root.task.task_id
    response.root_state = root.state
    plan_id = await agent.task_manager.operations_runtime.run(
        agent.task_manager.get_task_plan_id, root.task.task_id
    )
    plan = (
        await agent.task_manager.operations_runtime.run(
            agent.task_manager.get_plan, plan_id
        )
        if plan_id
        else None
    )
    if plan is not None:
        active_step = next(
            (step for step in plan.plan_steps if step.status == "in_progress"),
            None,
        )
        response.plan_phase = (
            getattr(active_step, "title", None)
            or plan.current_focus
            or plan.plan_summary
        )
        response.plan_status = getattr(plan.status, "value", plan.status)
    response.last_activity_at, response.last_activity = _latest_task_activity(root)
    if root.state == ProgressState.WAITING:
        response.blocker = "Waiting for delegated work or required input."
    from custom_agents.delegated_work_runtime import delegated_tasks

    delegated = {
        record.task_id: record
        for record in await agent.task_manager.operations_runtime.run(
            delegated_tasks(agent).list_for_parent, str(agent.team_id), root.task.task_id
        )
    }
    for child in await agent.task_manager.operations_runtime.run(
        agent.task_manager.get_subtasks, root.task.task_id
    ):
        record = delegated.get(child.task.task_id)
        last_updated = child.task.task_state[-1][1] if child.task.task_state else None
        response.children.append(
            TaskProgressChild(
                task_id=child.task.task_id,
                named_agent_type=getattr(child.meta, "to_named_agent_type", None),
                state=child.state,
                queue_status=getattr(record, "status", None),
                worker_name=getattr(record, "holder_agent_name", None),
                last_updated_at=last_updated,
            )
        )
    return response


async def handle_task_progress_status(
    agent: Any,
    *,
    request: TaskProgressStatus,
    msg_header: MessageHeader,
    msg_meta: MessageMeta,
) -> None:
    if request.status_type == MessageMetaType.RESPONSE:
        return
    response_key = msg_header.response_routing_key
    if not response_key:
        return
    response = await build_task_progress_response(agent, request, msg_meta)
    envelope = MessageEnvelope(
        msg_content=response,
        msg_header=MessageHeader(
            from_agent_id=agent.id,
            to_routing_path=response_key,
            agent_signature="",
        ),
        msg_meta=MessageMeta(
            from_agent_name=agent.name,
            msg_correlation_id=msg_meta.msg_correlation_id,
            conversation_id=msg_meta.conversation_id,
            requester_type=RequesterType.AGENT_REQUESTER,
            is_response_requested=False,
            gateway_context=msg_meta.gateway_context,
        ),
    )
    await agent._send_operational_message(envelope)


def _runtime_work_owner_matches(task_info: Any, canonical_user_id: str) -> bool:
    """Whether the person may see the task: its owner is the one recorded at admission."""
    return private_access(
        person_id=canonical_user_id,
        owner_id=task_info.owner_person_id,
        resource_type=RESOURCE_TASK,
        resource_id=task_info.task.task_id,
    ).allowed


async def _runtime_work_root_summary(agent: Any, root: Any) -> RuntimeWorkSummary:
    task_manager = agent.task_manager
    root_task_id = root.task.task_id
    gateway_context = getattr(root.meta, "gateway_context", None)
    latest_activity_at, latest_activity = _latest_task_activity(root)
    pending_pause = await task_manager.operations_runtime.run(
        task_manager.get_pending_recoverable_pause, root_task_id
    )
    pending_query = await task_manager.operations_runtime.run(
        task_manager.get_latest_pending_task_query, root_task_id
    )
    human_blocker = await task_manager.operations_runtime.run(
        partial(get_human_interaction_blocker, task_manager), root_task_id
    )
    blocker = None
    if pending_pause is not None:
        blocker = pending_pause.friendly_reason or "Waiting for operator guidance."
    elif pending_query is not None:
        blocker = pending_query.message
    elif human_blocker is not None:
        request_ids = blocker_request_ids(human_blocker)
        blocker = (
            "Waiting for a person to decide access request(s) in the Inbox: "
            + ", ".join(request_ids)
            + "."
            if request_ids
            else "Waiting for required input."
        )
    elif root.state == ProgressState.WAITING:
        blocker = "Waiting for delegated work or required input."
    graph = await task_manager.operations_runtime.run(
        task_manager.get_execution_graph_task_infos, root_task_id
    )
    children: list[RuntimeWorkChildSummary] = []
    for child in graph:
        if child.task.task_id == root_task_id:
            continue
        child_updated_at = (
            child.task.task_state[-1][1] if child.task.task_state else None
        )
        children.append(
            RuntimeWorkChildSummary(
                task_id=child.task.task_id,
                state=child.state,
                target_agent_name=getattr(child.meta, "to_agent_name", None),
                target_named_agent_type=getattr(
                    child.meta, "to_named_agent_type", None
                ),
                last_updated_at=child_updated_at,
            )
        )
    return RuntimeWorkSummary(
        root_task_id=root_task_id,
        state=root.state,
        session_key=getattr(gateway_context, "session_key", None),
        conversation_id=getattr(root.meta, "conversation_id", None),
        objective_id=await task_manager.operations_runtime.run(
            task_manager.get_task_objective_id, root_task_id
        ),
        objective_work_session_id=getattr(root.meta, "objective_work_session_id", None),
        plan_id=await task_manager.operations_runtime.run(
            task_manager.get_task_plan_id, root_task_id
        ),
        latest_activity_at=latest_activity_at,
        latest_activity=latest_activity,
        blocker=blocker,
        children=sorted(children, key=lambda child: child.task_id),
    )


async def _runtime_work_roots(agent: Any, canonical_user_id: str) -> list[Any]:
    task_manager = agent.task_manager
    roots: dict[str, Any] = {}
    for task_info in await task_manager.operations_runtime.run(
        task_manager.list_task_infos
    ):
        if await task_manager.operations_runtime.run(
            task_manager.is_terminal_state, task_info.state
        ):
            continue
        if not _runtime_work_owner_matches(task_info, canonical_user_id):
            continue
        root_task_id = (
            await task_manager.operations_runtime.run(
                task_manager.resolve_root_task_id, task_info.task.task_id
            )
            or task_info.task.task_id
        )
        root = (
            await task_manager.operations_runtime.run(
                task_manager.get_task_info, root_task_id
            )
            or task_info
        )
        if _runtime_work_owner_matches(root, canonical_user_id):
            roots[root_task_id] = root
    return sorted(
        roots.values(),
        key=lambda item: str(
            item.task.task_state[-1][1] if item.task.task_state else ""
        ),
        reverse=True,
    )


async def _runtime_work_schedules(
    agent: Any,
    canonical_user_id: str,
) -> list[RuntimeWorkScheduleSummary]:
    try:
        store = agent._ensure_schedule_store()
    except Exception:
        agent._logger.exception(
            "Runtime work inventory could not open the schedule store. "
            "event=runtime_work_schedule_inventory_unavailable"
        )
        return []
    try:
        rows = await agent.task_manager.operations_runtime.run(store.get_all)
    except Exception:
        agent._logger.exception(
            "Runtime work inventory could not read schedules. "
            "event=runtime_work_schedule_inventory_unavailable"
        )
        return []
    schedules: list[RuntimeWorkScheduleSummary] = []
    for row in rows:
        schedule_id = str(row.get("schedule_id") or "").strip()
        try:
            schedule = (
                await agent.task_manager.operations_runtime.run(
                    store.get_scheduled_message, schedule_id
                )
                if schedule_id
                else None
            )
        except Exception:
            agent._logger.exception(
                "Runtime work inventory skipped an unreadable schedule. "
                "event=runtime_work_schedule_read_failed schedule_id=%s",
                schedule_id,
            )
            continue
        if (
            schedule is None
            or owner_canonical_user_id_for_schedule(schedule) != canonical_user_id
        ):
            continue
        schedules.append(
            RuntimeWorkScheduleSummary(
                schedule_id=schedule.schedule_id,
                schedule_name=schedule.schedule_name,
                objective_id=schedule.objective_id,
                enabled=bool(schedule.enabled),
                paused_until=schedule.paused_until,
                next_fire_at=schedule.next_fire_at,
            )
        )
    return sorted(
        schedules,
        key=lambda schedule: (schedule.next_fire_at or "", schedule.schedule_id),
    )


def _objective_available_actions(status: ObjectiveStatus) -> list[str]:
    """The control words a person is offered for an Objective in ``status``."""

    if status == ObjectiveStatus.DELETED:
        controls: tuple[str, ...] = ()
    elif status == ObjectiveStatus.DELETING:
        controls = (DELETE_OBJECTIVE,)
    elif status == ObjectiveStatus.PAUSED:
        controls = (RESUME_OBJECTIVE, STOP_OBJECTIVE, ABANDON_OBJECTIVE, DELETE_OBJECTIVE)
    elif status in {
        ObjectiveStatus.SATISFIED,
        ObjectiveStatus.FAILED,
        ObjectiveStatus.CANCELLED,
        ObjectiveStatus.ABANDONED,
    }:
        controls = (DELETE_OBJECTIVE,)
    else:
        controls = (PAUSE_OBJECTIVE, STOP_OBJECTIVE, ABANDON_OBJECTIVE, DELETE_OBJECTIVE)
    return [OBJECTIVE_CONTROL_WORDS[control] for control in controls]


def _objective_work_policy(agent: Any, objective: Any) -> Any:
    repository = getattr(agent.task_manager, "objective_work_repository", None)
    if repository is None:
        return None
    try:
        return repository.get_policy(
            objective.objective_id,
            owner_agent_id=str(objective.owner_agent_id or ""),
        )
    except Exception:
        agent._logger.exception(
            "Objective control inventory could not read the work policy. "
            "event=objective_control_policy_read_failed objective_id=%s",
            objective.objective_id,
        )
        return None


async def _runtime_objective_summary(
    agent: Any, objective: Any
) -> RuntimeObjectiveSummary:
    policy = _objective_work_policy(agent, objective)
    plan = (
        await agent.task_manager.operations_runtime.run(
            agent.task_manager.get_plan, objective.current_plan_id
        )
        if objective.current_plan_id
        else None
    )
    plan_steps = list(getattr(plan, "plan_steps", ()) or ())
    if not plan_steps:
        plan_steps = list(getattr(plan, "plan_items", ()) or ())
    subgoals = [
        RuntimeObjectiveSubgoal(
            step_id=step.step_id,
            title=step.title,
            status=step.status,
            status_detail=getattr(step, "status_detail", None),
            result_summary=(
                getattr(step, "result_summary", None)
                or getattr(step, "resolution_summary", None)
            ),
        )
        for step in plan_steps
    ]
    return RuntimeObjectiveSummary(
        objective_id=objective.objective_id,
        record_digest=contract_sha256(objective.model_dump(mode="json")),
        title=objective.title,
        desired_outcome=objective.desired_outcome,
        status=objective.status,
        horizon=objective.horizon,
        priority=objective.priority,
        progress_summary=objective.progress_summary,
        current_focus=objective.current_focus,
        current_plan_id=objective.current_plan_id,
        plan_summary=getattr(plan, "plan_summary", None),
        subgoals=subgoals,
        success_criteria=list(objective.success_criteria),
        schedule_ids=[link.schedule_id for link in objective.schedule_links],
        heartbeat_item_ids=[
            link.heartbeat_item_id for link in objective.heartbeat_links
        ],
        work_policy_status=getattr(policy, "status", None),
        updated_at=objective.updated_at,
        deletion_requested_at=objective.deletion_requested_at,
        deleted_at=objective.deleted_at,
        deletion_summary=objective.deletion_summary,
        available_actions=_objective_available_actions(objective.status),
    )


async def _runtime_user_objectives(
    agent: Any,
    command: ManagerRuntimeWorkCommand,
    *,
    person: CommandPerson,
) -> list[RuntimeObjectiveSummary]:
    discovered = await agent.task_manager.operations_runtime.run(
        agent.task_manager.list_user_owned_objectives,
        owner_canonical_user_id=person.canonical_user_id,
        team_id=person.team_id,
        include_closed=True,
        limit=100,
    )
    objectives = []
    for item in discovered:
        objective = await agent.task_manager.operations_runtime.run(
            agent.task_manager.get_user_owned_objective,
            item.objective_id,
            owner_canonical_user_id=person.canonical_user_id,
            team_id=person.team_id,
            adopt_live_custody=True,
        )
        if objective is not None:
            objectives.append(objective)
    return [await _runtime_objective_summary(agent, item) for item in objectives]


# Each person's Objective control: the status it sets and the summary it
# records. The manager enforces the actions it publishes
# (``_objective_available_actions``), so a stale page or a typed command
# cannot, for example, make a satisfied Objective active again. A control
# whose status is already stored is a retry and is accepted: a pause or
# abandon whose schedule did not switch must be repeatable, and the lifecycle
# step records nothing new and switches only the links not yet switched.
_OBJECTIVE_CONTROLS: dict[str, tuple[ObjectiveStatus, str]] = {
    PAUSE_OBJECTIVE: (ObjectiveStatus.PAUSED, "Paused by authenticated user control."),
    STOP_OBJECTIVE: (ObjectiveStatus.PAUSED, "Stopped by authenticated user control."),
    RESUME_OBJECTIVE: (ObjectiveStatus.ACTIVE, "Resumed by authenticated user control."),
    ABANDON_OBJECTIVE: (ObjectiveStatus.ABANDONED, "Abandoned by authenticated user control."),
    DELETE_OBJECTIVE: (
        ObjectiveStatus.DELETING,
        "Recoverable deletion requested by authenticated user control.",
    ),
}
_OBJECTIVE_CONTROLS_THAT_STOP_WORK = frozenset(
    {STOP_OBJECTIVE, ABANDON_OBJECTIVE, DELETE_OBJECTIVE}
)


@dataclass
class _PersonLinkSwitch:
    """Turns an Objective's linked items on or off for the person its control works for.

    Schedules change under the person's gateway-signed permit
    (``AUTHENTICATED_GATEWAY_USER``), as these controls always did.
    """

    agent: Any
    person: CommandPerson
    _heartbeats: Optional[HeartbeatStore] = None

    def switch_schedule(
        self, objective_id: str, link: ObjectiveScheduleLink, enabled: bool
    ) -> LinkSwitchResult:
        schedule_store = self.agent._ensure_schedule_store()
        schedule = schedule_store.get_scheduled_message(link.schedule_id)
        if schedule is None:
            return LinkSwitchResult.MISSING
        operation = (
            ScheduleMutationOperation.ENABLE if enabled else ScheduleMutationOperation.DISABLE
        )
        candidate = schedule.model_copy(deep=True)
        candidate.enabled = enabled
        permit = build_schedule_mutation_permit(
            schedule=schedule,
            operation=operation,
            authority_mode=ScheduleMutationAuthorityMode.AUTHENTICATED_GATEWAY_USER,
            actor_principal_id=self.person.canonical_user_id,
            owner_canonical_user_id=self.person.canonical_user_id,
            team_id=self.person.team_id,
            candidate=candidate,
        )
        try:
            changed = schedule_store.set_enabled(
                link.schedule_id,
                enabled,
                operation=operation,
                system_mutation_permit=permit,
            )
        except Exception as exc:
            self.agent._logger.warning(
                "Objective control schedule mutation failed. "
                "event=objective_control_schedule_mutation_failed "
                "objective_id=%s schedule_id=%s error=%s",
                objective_id,
                link.schedule_id,
                exc,
            )
            return LinkSwitchResult.FAILED
        return LinkSwitchResult.SWITCHED if changed else LinkSwitchResult.FAILED

    def switch_heartbeat(
        self, objective_id: str, link: ObjectiveHeartbeatLink, enabled: bool
    ) -> LinkSwitchResult:
        try:
            if self._heartbeats is None:
                self._heartbeats = HeartbeatStore(
                    db_path=self.agent.task_manager._db_path,
                    logger_=self.agent._logger,
                    storage_settings=self.agent.task_manager.storage_settings,
                )
            item = self._heartbeats.set_item_enabled(link.heartbeat_item_id, enabled)
        except Exception as exc:
            self.agent._logger.warning(
                "Objective control heartbeat mutation failed. "
                "event=objective_control_heartbeat_mutation_failed "
                "objective_id=%s heartbeat_item_id=%s error=%s",
                objective_id,
                link.heartbeat_item_id,
                exc,
            )
            return LinkSwitchResult.FAILED
        return LinkSwitchResult.SWITCHED if item is not None else LinkSwitchResult.MISSING


@dataclass(frozen=True)
class _ObjectiveControlResult:
    """What one control's task operation found or changed (data only).

    A refusal changed nothing, so it carries no status: the gateway would
    otherwise tell the person the Objective "is now" that status.
    """

    refusal: Optional[str] = None
    already_deleted: bool = False
    outcome: Optional[ObjectiveLifecycleOutcome] = None


def _control_objective_in_operation(
    agent: Any,
    *,
    command: ManagerRuntimeWorkCommand,
    person: CommandPerson,
    summary_override: Optional[str],
) -> _ObjectiveControlResult:
    """Check and apply one person's Objective control, all inside one task operation.

    The owner check, the page's record digest and the allowed actions are
    checked against the same stored record the change starts from, so a
    change that lands after the person looked is either refused
    (``objective_changed``) or seen. The status, work policy and links then
    change in ``apply_objective_lifecycle`` before anything else runs.
    """

    task_manager = agent.task_manager
    objective_id = str(command.target_objective_id or "").strip()
    objective = task_manager.get_user_owned_objective(
        objective_id,
        owner_canonical_user_id=person.canonical_user_id,
        team_id=person.team_id,
        adopt_live_custody=True,
    )
    if objective is None:
        return _ObjectiveControlResult(refusal=OBJECTIVE_NOT_FOUND)
    decision = private_access(
        person_id=person.canonical_user_id,
        owner_id=objective.owner_canonical_user_id,
        resource_type=RESOURCE_OBJECTIVE,
        resource_id=objective.objective_id,
        action=PRIVATE_DELETE if command.action == DELETE_OBJECTIVE else PRIVATE_UPDATE,
    )
    if not decision.allowed:
        return _ObjectiveControlResult(refusal=OBJECTIVE_OWNER_MISMATCH)
    expected_digest = str(command.expected_objective_digest or "").strip()
    if expected_digest and expected_digest != contract_sha256(objective.model_dump(mode="json")):
        return _ObjectiveControlResult(refusal=OBJECTIVE_CHANGED)
    if objective.status == ObjectiveStatus.DELETED:
        return _ObjectiveControlResult(already_deleted=True)
    target_status, summary = _OBJECTIVE_CONTROLS[command.action]
    offered = OBJECTIVE_CONTROL_WORDS[command.action] in _objective_available_actions(objective.status)
    if not offered and target_status != objective.status:
        agent._logger.info(
            "Refused an Objective control its status does not offer. "
            "event=objective_control_action_unavailable objective_id=%s action=%s status=%s",
            objective.objective_id,
            command.action,
            objective.status.value,
        )
        return _ObjectiveControlResult(
            refusal=f"{OBJECTIVE_ACTION_UNAVAILABLE}:{objective.status.value}",
        )
    deleting = command.action == DELETE_OBJECTIVE
    outcome = apply_objective_lifecycle(
        task_manager,
        objective.objective_id,
        ObjectiveStatusChange(
            status=target_status,
            summary=summary_override or summary,
            deletion_operation_id=command.command_id if deleting else None,
            policy_person_id=person.canonical_user_id,
            tolerate_missing_links=deleting,
        ),
        _PersonLinkSwitch(agent, person),
    )
    if outcome is None:
        return _ObjectiveControlResult(refusal=OBJECTIVE_NOT_FOUND)
    return _ObjectiveControlResult(outcome=outcome)


async def _finish_objective_deletion(
    agent: Any,
    objective_id: str,
    *,
    incomplete: list[str],
    response: ManagerRuntimeWorkCommand,
) -> None:
    """Record a deletion as waiting on ``incomplete`` controls, or as done.

    Each write loads the stored record in its own operation. A save that
    fails raises ``ObjectiveSaveFailed``, so the person is never told an
    Objective was deleted that the store still holds.
    """

    task_manager = agent.task_manager
    if incomplete:
        waiting = "Deletion is waiting for controls to finish: " + ", ".join(incomplete)

        def note_waiting(objective: Any) -> bool:
            if objective.deletion_summary == waiting:
                return False
            objective.deletion_summary = waiting
            return True

        await task_manager.operations_runtime.run(
            partial(task_manager.objective_coordinator.change_objective, objective_id, note_waiting)
        )
        response.success = False
        response.message = f"{OBJECTIVE_DELETION_INCOMPLETE}:" + ",".join(incomplete)
        response.objective_status = ObjectiveStatus.DELETING
        return
    deleted = await task_manager.operations_runtime.run(
        task_manager.update_objective_status,
        objective_id,
        ObjectiveStatus.DELETED,
        summary=(
            "Future execution is disabled and linked active work was stopped. "
            "Plans, evidence, checkpoints, and audit history were retained."
        ),
        user_visible=True,
    )
    if deleted is None:
        raise ObjectiveSaveFailed(objective_id)
    response.success = True
    response.message = OBJECTIVE_DELETED
    response.objective_status = ObjectiveStatus.DELETED


def _runtime_work_scope_matches(
    root: Any,
    command: ManagerRuntimeWorkCommand,
) -> bool:
    root_meta = getattr(root, "meta", None)
    gateway_context = getattr(root_meta, "gateway_context", None)
    if command.scope == "user":
        return True
    if command.scope == "session":
        return bool(
            command.session_key
            and getattr(gateway_context, "session_key", None) == command.session_key
        )
    if command.scope == "conversation":
        return bool(
            command.conversation_id
            and getattr(root_meta, "conversation_id", None) == command.conversation_id
        )
    if command.scope == "objective":
        root_meta = getattr(root, "meta", None)
        return bool(
            command.target_objective_id
            and getattr(root_meta, "objective_id", None) == command.target_objective_id
        )
    return bool(root.task.task_id == command.target_task_id)


async def _runtime_work_inventory(
    agent: Any,
    command: ManagerRuntimeWorkCommand,
    *,
    person: CommandPerson,
) -> tuple[list[Any], list[RuntimeWorkSummary], list[RuntimeWorkScheduleSummary], str]:
    roots = await _runtime_work_roots(agent, person.canonical_user_id)
    summaries = [await _runtime_work_root_summary(agent, root) for root in roots]
    schedules = await _runtime_work_schedules(
        agent, person.canonical_user_id
    )
    digest = contract_sha256(
        {
            "canonical_user_id": person.canonical_user_id,
            "roots": [summary.model_dump(mode="json") for summary in summaries],
            "schedules": [schedule.model_dump(mode="json") for schedule in schedules],
        }
    )
    return roots, summaries, schedules, digest


async def _populate_runtime_work_inventory(
    agent: Any,
    *,
    command: ManagerRuntimeWorkCommand,
    person: CommandPerson,
    response: ManagerRuntimeWorkCommand,
    include_objectives: bool = False,
) -> list[Any]:
    (
        roots,
        response.roots,
        response.schedules,
        response.inventory_digest,
    ) = await _runtime_work_inventory(agent, command, person=person)
    if include_objectives:
        response.objectives = await _runtime_user_objectives(agent, command, person=person)
    return roots


async def _select_runtime_work_roots(
    agent: Any,
    *,
    roots: list[Any],
    command: ManagerRuntimeWorkCommand,
) -> list[Any]:
    if command.scope != "task":
        return [root for root in roots if _runtime_work_scope_matches(root, command)]
    selected: list[Any] = []
    for root in roots:
        graph_ids = {
            item.task.task_id
            for item in await agent.task_manager.operations_runtime.run(
                agent.task_manager.get_execution_graph_task_infos, root.task.task_id
            )
        }
        if command.target_task_id in graph_ids:
            selected.append(root)
    return selected


async def _cancel_runtime_work_roots(
    agent: Any,
    *,
    roots: list[Any],
    command: ManagerRuntimeWorkCommand,
    person: CommandPerson,
) -> list[RuntimeWorkCancellationReceipt]:
    receipts: list[RuntimeWorkCancellationReceipt] = []
    for root in roots:
        root_task_id = root.task.task_id
        try:
            result = await stop_task_execution(
                agent,
                task_info=root,
                reason=command.reason,
                cascade_to_subtasks=True,
                canonical_user_id=person.canonical_user_id,
                command_id=command.command_id,
                send_response=False,
            )
            durable_cancellation_recorded = bool(
                result.get("durable_cancellation_recorded")
            )
            receipts.append(
                RuntimeWorkCancellationReceipt(
                    root_task_id=root_task_id,
                    confirmed=durable_cancellation_recorded,
                    cancelled_task_count=int(result.get("cancelled_task_count") or 0),
                    stop_signals_sent=int(result.get("stop_signals_sent") or 0),
                    unconfirmed_worker_count=int(
                        result.get("unconfirmed_worker_count") or 0
                    ),
                    message=(
                        "Execution graph cancelled locally and its restart barrier was persisted."
                        if durable_cancellation_recorded
                        else "Execution graph cancelled locally, but its restart barrier could not be persisted."
                    ),
                )
            )
        except Exception as exc:
            agent._logger.exception(
                "Runtime work cancellation failed. root_task_id=%s command_id=%s",
                root_task_id,
                command.command_id,
            )
            receipts.append(
                RuntimeWorkCancellationReceipt(
                    root_task_id=root_task_id,
                    confirmed=False,
                    message=str(exc),
                )
            )
    return receipts


def _answer_unchanged_objective(
    result: _ObjectiveControlResult,
    *,
    command: ManagerRuntimeWorkCommand,
    response: ManagerRuntimeWorkCommand,
) -> None:
    """Answer a control that changed nothing: a refusal, or an already deleted Objective.

    Only a deletion reports the deleted status as its outcome; any other
    control carries no status, so the gateway never reports one as new.
    """

    if result.already_deleted:
        response.success = command.action == DELETE_OBJECTIVE
        response.message = OBJECTIVE_DELETED if response.success else OBJECTIVE_ALREADY_DELETED
        if response.success:
            response.objective_status = ObjectiveStatus.DELETED
        return
    response.success = False
    response.message = result.refusal or OBJECTIVE_UPDATE_FAILED


async def _apply_objective_control(
    agent: Any,
    *,
    command: ManagerRuntimeWorkCommand,
    person: CommandPerson,
    response: ManagerRuntimeWorkCommand,
    summary_override: Optional[str] = None,
) -> None:
    """Apply one objective action for its owner.

    ``summary_override`` replaces the status summary recorded on the
    objective; trusted runtime callers acting for the owner say why
    (``custom_agents.person_departure_runtime`` when the owner leaves).
    The checks and the change run as one task operation
    (``_control_objective_in_operation``); stopping active work and finishing
    a deletion follow as their own operations.
    """

    try:
        result = await agent.task_manager.operations_runtime.run(
            partial(
                _control_objective_in_operation,
                agent,
                command=command,
                person=person,
                summary_override=summary_override,
            )
        )
    except Exception:
        agent._logger.exception(
            "Objective lifecycle control failed before active-work cancellation. "
            "event=objective_control_prepare_failed objective_id=%s action=%s",
            command.target_objective_id,
            command.action,
        )
        response.success = False
        response.message = OBJECTIVE_UPDATE_FAILED
        return
    if result.outcome is None:
        _answer_unchanged_objective(result, command=command, response=response)
        return

    outcome = result.outcome
    if getattr(agent, "_scheduler_wake", None):
        agent._scheduler_wake.set()
    response.objective_status = outcome.objective.status
    if command.action in _OBJECTIVE_CONTROLS_THAT_STOP_WORK:
        roots = await _runtime_work_roots(
            agent,
            person.canonical_user_id,
        )
        selected = await _select_runtime_work_roots(
            agent,
            roots=roots,
            command=command,
        )
        response.cancellation_receipts = await _cancel_runtime_work_roots(
            agent,
            roots=selected,
            command=command,
            person=person,
        )

    unconfirmed = [
        receipt.root_task_id
        for receipt in response.cancellation_receipts
        if not receipt.confirmed
    ]
    incomplete = list(outcome.incomplete) + [f"{OBJECTIVE_PART_TASK}:{task_id}" for task_id in unconfirmed]
    if command.action == DELETE_OBJECTIVE:
        try:
            await _finish_objective_deletion(
                agent, outcome.objective.objective_id, incomplete=incomplete, response=response
            )
        except ObjectiveSaveFailed:
            response.success = False
            response.message = OBJECTIVE_UPDATE_FAILED
            response.objective_status = ObjectiveStatus.DELETING
        return

    response.success = not incomplete
    response.message = (
        f"objective_{outcome.objective.status.value}"
        if response.success
        else f"{OBJECTIVE_CONTROL_INCOMPLETE}:" + ",".join(incomplete)
    )


async def handle_manager_runtime_work_command(
    agent: Any,
    *,
    command: ManagerRuntimeWorkCommand,
    msg_header: MessageHeader,
    msg_meta: MessageMeta,
) -> None:
    """List or cancel owner-scoped work without entering the model loop."""

    if command.command_type == MessageMetaType.RESPONSE:
        return
    response = command.model_copy(deep=True)
    response.command_type = MessageMetaType.RESPONSE
    response.roots = []
    response.schedules = []
    response.objectives = []
    response.cancellation_receipts = []
    person, refusal = verified_command_person(agent, msg_meta)
    worker_task_control = command.action in {"list_tasks", "list_finished_tasks", "remove_task", "purge_tasks"}
    if (not getattr(agent, "is_team_lead", False) and not worker_task_control) or person is None:
        response.success = False
        response.message = refusal or "manager_required"
        await _send_durable_response(
            agent,
            content=response,
            incoming_header=msg_header,
            incoming_meta=msg_meta,
        )
        return

    if command.action == "list_finished_tasks":
        from custom_agents.runtime_task_controls import finished_task_inventory

        response.finished_tasks, response.task_total = await agent.task_manager.operations_runtime.run(
            partial(finished_task_inventory, agent, person=person), command,
        )
        response.success = True
        response.message = "Finished tasks loaded."
        await _send_durable_response(agent, content=response, incoming_header=msg_header, incoming_meta=msg_meta)
        return

    if command.action in {"list_tasks", "remove_task", "purge_tasks"}:
        from custom_agents.runtime_task_controls import apply_task_control

        await apply_task_control(
            agent, command, response, person=person, list_roots=_runtime_work_roots,
            summarize_root=_runtime_work_root_summary, cancel_roots=_cancel_runtime_work_roots,
        )
        await _send_durable_response(
            agent, content=response, incoming_header=msg_header, incoming_meta=msg_meta
        )
        return

    if command.action == "list_objectives":
        response.success = True
        response.message = "objectives_listed"
        await _populate_runtime_work_inventory(
            agent,
            command=command,
            response=response,
            include_objectives=True,
            person=person,
        )
        await _send_durable_response(
            agent,
            content=response,
            incoming_header=msg_header,
            incoming_meta=msg_meta,
        )
        return

    if command.action in _OBJECTIVE_CONTROLS:
        await _apply_objective_control(
            agent,
            command=command,
            response=response,
            person=person,
        )
        await _populate_runtime_work_inventory(
            agent, command=command, response=response, person=person
        )
        await _send_durable_response(
            agent,
            content=response,
            incoming_header=msg_header,
            incoming_meta=msg_meta,
        )
        return

    roots = await _populate_runtime_work_inventory(
        agent,
        command=command,
        response=response,
        person=person,
    )
    if command.action == "inventory":
        response.success = True
        response.message = "runtime_work_inventory"
        await _send_durable_response(
            agent,
            content=response,
            incoming_header=msg_header,
            incoming_meta=msg_meta,
        )
        return

    selected_roots = await _select_runtime_work_roots(
        agent,
        roots=roots,
        command=command,
    )
    if not selected_roots:
        response.success = False
        response.message = "no_matching_active_work"
    else:
        response.cancellation_receipts = await _cancel_runtime_work_roots(
            agent,
            roots=selected_roots,
            command=command,
            person=person,
        )
        response.success = all(
            receipt.confirmed for receipt in response.cancellation_receipts
        )
        response.message = (
            "runtime_work_cancelled"
            if response.success
            else "runtime_work_cancellation_incomplete"
        )

    await _populate_runtime_work_inventory(agent, command=command, response=response, person=person)
    await _send_durable_response(
        agent,
        content=response,
        incoming_header=msg_header,
        incoming_meta=msg_meta,
    )
