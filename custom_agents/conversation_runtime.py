"""
Helpers for conversation-thread persistence and prompt-context assembly.

Whose conversation it is: a new thread and each turn are recorded as owned by
the person the task verifiably works for (its signed task context, or for a
scheduled run the schedule's owner it is signed for), passed in as
``owner_person_id``. The ``canonical_user_id`` in a message's gateway context
is a claim no signature covers, so it never decides ownership. The owner
decides who may read the conversation (``access_control.ownership``,
conversation tools), which threads topic recall offers, whose stale
conversations are reviewed, and what moves out with a person who leaves the
team. Work for no person records no owner.

Who may continue a conversation: a new top-level task joins an existing
conversation only when it works for that conversation's owner, and work for
no person only an ownerless one (``access_control.ownership.
conversation_continuation``). Joining adds the task's turn to the
conversation and shows the earlier turns to its model, so a task for someone
else would read another person's history, and a turn would be filed in a
conversation whose owner never asked for it. A task that names someone else's
conversation is refused here (``ValueError``, logged as
``conversation_owner_mismatch_refused``); its admission path reports the
refusal. Trusted code keeps work from naming such a conversation in the first
place: runs for no person start their own conversation
(``start_new_conversation``), and a schedule or heartbeat item that changes
hands continues in a conversation of its new person
(``custom_agents.person_departure_runtime``,
``custom_agents.schedule_context_refresh``,
``custom_agents.heartbeat_store.item_conversation_for_run``).
"""

from __future__ import annotations

import logging
from functools import partial
from typing import Any, Dict, List, Optional

from access_control.ownership import conversation_continuation
from custom_agents.conversation_summary import (
    ConversationSummaryGenerator,
    TurnSummaryRequest,
    build_turn_summary as build_turn_summary,
    stringify_task_answer,
    stringify_task_errors,
)
from custom_messages.contracts.routing_v1 import (
    GatewayContext,
    MessageHeader,
    MessageMeta,
)
from custom_messages.contracts.task_v1 import Task
from custom_messages.enums import (
    MessageMetaType,
    ProgressState,
    RequesterType,
    TaskHistoryImportance,
    TaskHistoryType,
)

logger = logging.getLogger(__name__)


def start_new_conversation(meta: MessageMeta) -> MessageMeta:
    """Make the run with this meta start a conversation of its own instead of continuing one.

    For work for no person (module docstring): its schedule may name a
    person's conversation (the session it was installed with), which such a
    run must not continue. Changes ``meta`` in place and returns it.
    """

    meta.conversation_id = None
    meta.followup_to_task_id = None
    return meta


def _require_conversation_owner(
    thread: Dict[str, Any], *, person_id: Optional[str], task_id: str
) -> None:
    """Refuse a task that would continue a conversation of someone else (module docstring); ``ValueError``."""

    conversation_id = str(thread.get("conversation_id") or "")
    owner = thread.get("owner_canonical_user_id")
    decision = conversation_continuation(
        person_id=person_id, owner_id=owner, conversation_id=conversation_id
    )
    if decision.allowed:
        return
    logger.warning(
        "A task named a conversation that belongs to someone else, so it was not added to it. "
        "event=conversation_owner_mismatch_refused conversation_id=%s task_id=%s "
        "conversation_owner=%s task_person=%s",
        conversation_id,
        task_id,
        owner or "-",
        person_id or "-",
    )
    raise ValueError(
        f"Conversation {conversation_id} belongs to someone other than the person task {task_id} "
        "works for, so the task cannot continue it. Start a new conversation instead."
    )


def build_turn_execution_history(task_manager: Any, task_id: str) -> str:
    return task_manager.summarize_task_history_markdown(
        task_id=task_id,
        max_history_entries=40,
        include_system=True,
        include_agent=True,
    ).strip()


def record_task_conversation_history(
    *,
    task_manager: Any,
    task_id: str,
    conversation_id: str,
    followup_to_task_id: Optional[str],
) -> None:
    task_info = task_manager.get_task_info(task_id)
    if not task_info:
        return
    summary = (
        f"Conversation thread {conversation_id} started for this task."
        if not followup_to_task_id
        else f"Task linked into conversation {conversation_id} as a follow-up to {followup_to_task_id}."
    )
    task_manager.append_history_entry(
        task_id,
        summary,
        entry_type=TaskHistoryType.TASK_EVENT,
        importance=TaskHistoryImportance.NORMAL,
        details={
            "conversation_id": conversation_id,
            "followup_to_task_id": followup_to_task_id,
        },
    )


def ensure_task_conversation_context(
    *,
    conversation_store: Any,
    team_id: Optional[str],
    owner_agent_id: str,
    owner_agent_name: str,
    task_request: Task,
    msg_header: MessageHeader,
    msg_meta: MessageMeta,
    owner_person_id: Optional[str],
    owner_named_agent_type: Optional[str] = None,
) -> MessageMeta:
    """Record a top-level task as the next turn of its conversation (a new thread when there is none).

    ``owner_person_id`` is the person the task verifiably works for (module
    docstring); None for work for no person. Raises ``ValueError`` (logged)
    when the named conversation belongs to someone else.
    """

    if task_request.parent_task_id is not None or conversation_store is None:
        return msg_meta

    normalized_meta = msg_meta.model_copy(deep=True) if msg_meta else MessageMeta()
    requester_name = getattr(normalized_meta, "from_agent_name", None)
    requester_id = getattr(msg_header, "from_agent_id", None)
    subject_preview = (task_request.problem_statement or "").strip()[:160] or None
    gateway_context = getattr(normalized_meta, "gateway_context", None)
    owner_canonical_user_id = str(owner_person_id or "").strip() or None

    thread = conversation_store.ensure_thread(
        conversation_id=normalized_meta.conversation_id,
        team_id=team_id,
        owner_canonical_user_id=owner_canonical_user_id,
        owner_agent_id=owner_agent_id,
        owner_agent_name=owner_agent_name,
        requester_name=requester_name,
        requester_id=requester_id,
        root_task_id=task_request.task_id,
        subject_preview=subject_preview,
    )
    # A new thread is this task's own; an existing one must be its person's.
    _require_conversation_owner(
        thread, person_id=owner_canonical_user_id, task_id=task_request.task_id
    )
    normalized_meta.conversation_id = thread.get("conversation_id")

    latest_link = conversation_store.get_latest_task_link(
        normalized_meta.conversation_id
    )
    followup_to_task_id = normalized_meta.followup_to_task_id
    requester_type = getattr(normalized_meta, "requester_type", None)
    delivery_mode = (
        getattr(gateway_context, "delivery_mode", None) if gateway_context else None
    )
    delivery_state = (
        "pending_gateway"
        if requester_type == RequesterType.TIMER_REQUESTER
        else "not_applicable"
    )

    if followup_to_task_id:
        followup_turn = conversation_store.get_turn_by_task_id(followup_to_task_id)
        if not followup_turn:
            raise ValueError(
                f"followup_to_task_id {followup_to_task_id} does not exist in the conversation store."
            )
        if followup_turn.get("conversation_id") != normalized_meta.conversation_id:
            raise ValueError(
                f"followup_to_task_id {followup_to_task_id} does not belong to conversation {normalized_meta.conversation_id}."
            )
    elif (
        requester_type != RequesterType.TIMER_REQUESTER
        and latest_link
        and latest_link.get("task_id") != task_request.task_id
    ):
        # Sharing history does not make a timer run a reply someone is awaiting.
        # Explicit follow-ups above still require normal response delivery.
        followup_to_task_id = latest_link.get("task_id")

    normalized_meta.followup_to_task_id = followup_to_task_id
    conversation_store.append_turn(
        conversation_id=normalized_meta.conversation_id,
        task_id=task_request.task_id,
        followup_to_task_id=followup_to_task_id,
        owner_agent_id=owner_agent_id,
        owner_named_agent_type=owner_named_agent_type,
        requester_name=requester_name,
        requester_id=requester_id,
        correlation_id=getattr(normalized_meta, "msg_correlation_id", None),
        problem_text=task_request.problem_statement or "",
        requester_type=requester_type,
        delivery_mode=delivery_mode,
        delivery_state=delivery_state,
        gateway_context=(
            gateway_context.model_dump(mode="json")
            if isinstance(gateway_context, GatewayContext)
            else dict(gateway_context)
            if isinstance(gateway_context, dict)
            else None
        ),
        task_status=ProgressState.ACCEPTED.value,
        owner_canonical_user_id=owner_canonical_user_id,
    )
    return normalized_meta


def build_conversation_prompt_context(
    *,
    conversation_store: Any,
    conversation_compactor: Any,
    task_request: Task,
    msg_meta: MessageMeta,
) -> str:
    if (
        conversation_store is None
        or task_request.parent_task_id is not None
        or task_request.task_type != MessageMetaType.REQUEST
        or not getattr(msg_meta, "conversation_id", None)
        or conversation_compactor is None
    ):
        return ""

    return conversation_compactor.build_prompt_context(
        store=conversation_store,
        conversation_id=msg_meta.conversation_id,
        latest_request_text=task_request.problem_statement or "",
        exclude_task_id=task_request.task_id,
    )


def extract_conversation_tool_artifacts(
    *,
    conversation_store: Any,
    conversation_artifact_offloader: Any,
    strands_messages: List[Dict[str, Any]],
    task_info: Any,
    logger: logging.Logger,
    agent_id: str,
) -> List[Dict[str, Any]]:
    if (
        conversation_store is None
        or conversation_artifact_offloader is None
        or not task_info.meta
        or not getattr(task_info.meta, "conversation_id", None)
        or not strands_messages
    ):
        return []
    try:
        return conversation_artifact_offloader.extract_large_text_artifacts(
            store=conversation_store,
            conversation_id=task_info.meta.conversation_id,
            task_id=task_info.task.task_id,
            messages=list(strands_messages),
        )
    except Exception as exc:
        logger.warning(
            f"[{agent_id}] Failed to extract conversation artifacts for task {task_info.task.task_id}: {exc}"
        )
        return []


async def finalize_conversation_turn(
    *,
    conversation_store: Any,
    conversation_compactor: Any,
    conversation_artifact_offloader: Any,
    strands_messages: List[Dict[str, Any]],
    task_manager: Any,
    task_info: Any,
    task_status: str,
    logger: logging.Logger,
    agent_id: str,
    team_id: Optional[str],
    named_agent_type: Optional[str],
    summary_generator: Optional[ConversationSummaryGenerator] = None,
) -> None:
    """Persist detached turn data without transferring service or SDK ownership."""
    operation = partial(
        _finalize_conversation_turn,
        conversation_store=conversation_store,
        conversation_compactor=conversation_compactor,
        conversation_artifact_offloader=conversation_artifact_offloader,
        task_manager=task_manager,
        logger=logger,
        agent_id=agent_id,
        team_id=team_id,
        named_agent_type=named_agent_type,
        summary_generator=summary_generator,
    )
    await task_manager.operations_runtime.run(
        operation,
        strands_messages=strands_messages,
        task_info=task_info,
        task_status=task_status,
    )


def _finalize_conversation_turn(
    *,
    conversation_store: Any,
    conversation_compactor: Any,
    conversation_artifact_offloader: Any,
    strands_messages: List[Dict[str, Any]],
    task_manager: Any,
    task_info: Any,
    task_status: str,
    logger: logging.Logger,
    agent_id: str,
    team_id: Optional[str],
    named_agent_type: Optional[str],
    summary_generator: Optional[ConversationSummaryGenerator] = None,
) -> None:
    if (
        conversation_store is None
        or not task_info.meta
        or not getattr(task_info.meta, "conversation_id", None)
        or task_info.task.parent_task_id is not None
    ):
        return

    final_answer_text = stringify_task_answer(task_info.task.answer)
    if task_info.task.errors:
        error_text = stringify_task_errors(task_info.task.errors)
        if error_text:
            final_answer_text = (
                f"{final_answer_text}\n\nErrors:\n{error_text}".strip()
                if final_answer_text
                else f"Errors:\n{error_text}"
            )
    artifact_manifest = extract_conversation_tool_artifacts(
        conversation_store=conversation_store,
        conversation_artifact_offloader=conversation_artifact_offloader,
        strands_messages=strands_messages,
        task_info=task_info,
        logger=logger,
        agent_id=agent_id,
    )
    # Stop and failure cleanup must finish even when the model service is down.
    # Use deterministic summaries here and defer optional compaction to a normal turn.
    interrupted = task_status in {ProgressState.FAILED.value, ProgressState.CANCELLED.value}
    generator = None if interrupted else summary_generator
    if generator is None:
        from custom_agents.conversation_summary import (
            HeuristicConversationSummaryGenerator,
        )

        generator = HeuristicConversationSummaryGenerator()
    from custom_agents.person_model_runtime import summaries_with_person_model

    # LLM summaries of a person's conversation use their own model when they have one.
    with summaries_with_person_model(task_info.meta):
        summary_result = generator.summarize_turn(
            TurnSummaryRequest(
                task=task_info.task,
                final_answer_text=final_answer_text,
                artifact_manifest=artifact_manifest,
            )
        )
    turn_summary = (
        summary_result.legacy_turn_summary_text or summary_result.context_summary_text
    )
    execution_history_text = build_turn_execution_history(
        task_manager, task_info.task.task_id
    )
    conversation_store.update_turn_completion(
        task_id=task_info.task.task_id,
        task_status=task_status,
        final_answer_text=final_answer_text,
        execution_history_text=execution_history_text,
        turn_summary_text=turn_summary,
        user_request_summary=summary_result.user_request_summary,
        assistant_response_summary=summary_result.assistant_response_summary,
        context_summary_text=summary_result.context_summary_text,
        artifact_summary=summary_result.artifact_summary,
        summary_generator=summary_result.summary_generator,
        clarification_summary_text=None,
        tool_artifact_manifest=artifact_manifest,
    )
    if conversation_compactor is not None and not interrupted:
        with summaries_with_person_model(task_info.meta):
            conversation_compactor.compact_if_needed(
                store=conversation_store,
                conversation_id=task_info.meta.conversation_id,
                latest_request_text="",
            )
