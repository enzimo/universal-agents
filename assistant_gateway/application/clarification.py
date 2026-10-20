"""Answering agent questions to people, and the questions' validity checks.

Which question a chat shows, and their order, belong to
``assistant_gateway.application.pending_questions``.
"""

from __future__ import annotations

import asyncio
import logging
import re
from collections.abc import Sequence
from dataclasses import dataclass

from assistant_gateway.application.local_responses import GatewayLocalResponseFactory
from assistant_gateway.application.manager_client import GatewayManagerClient
from assistant_gateway.application.pending_questions import (
    ALREADY_ANSWERED_TEXT,
    NO_LONGER_WAITING_TEXT,
    NOTE_MESSAGE_TYPE,
    QUESTION_MESSAGE_TYPE,
    QUESTION_WAITING_INTRO_TEXT,
    GatewayPendingQuestions,
    stored_task_query,
)
from assistant_gateway.application.ports import GatewayIdGenerator
from assistant_gateway.application.request_context import (
    GatewayRequestContextService,
    GatewayRequestRoute,
)
from assistant_gateway.application.request_service import GatewayRequestService
from assistant_gateway.application.response_coordinator import (
    GatewayResponseCoordinator,
)
from assistant_gateway.application.response_models import GatewayResponse
from assistant_gateway.application.session_service import (
    GatewaySessionRoute,
    GatewaySessionService,
)
from assistant_gateway.domain.request_state import (
    PendingQueryRecord,
    PendingQueryStatus,
)
from assistant_gateway.ports.repositories import SessionRepository
from custom_messages.contracts.envelope_v1 import MessageEnvelope
from custom_messages.contracts.replied_message_v1 import RepliedMessage
from custom_messages.contracts.routing_v1 import MessageHeader, MessageMeta
from custom_messages.contracts.task_v1 import (
    TaskQuery,
    TaskQueryChoiceOption,
    TaskStatus,
)
from custom_messages.enums import MessageMetaType, RequesterType


# Shown after a refused answer when the next question is shown in its reply.
NEXT_QUESTION_INTRO_TEXT = "This question is waiting for your answer:"


@dataclass(frozen=True)
class GatewayClarificationSettings:
    """Stable configuration for question validity checks and answers."""

    instance_name: str
    response_route: str
    manager_named_agent_type: str
    request_timezone: str
    default_session_key: str
    default_owner_canonical_user_id: str
    parent_probe_timeout_seconds: float
    manager_unavailable_text: str


def _choice_maps(
    choices: Sequence[TaskQueryChoiceOption],
) -> tuple[dict[str, str], dict[str, str], dict[str, str]]:
    by_id = {choice.option_id.lower(): choice.option_id for choice in choices}
    by_number = {
        str(index): choice.option_id for index, choice in enumerate(choices, start=1)
    }
    by_label = {
        choice.label.lower(): choice.option_id for choice in choices if choice.label
    }
    return by_id, by_number, by_label


def _normalized_choice_token(value: str) -> str:
    return str(value or "").strip().strip("`'\"()[]{}.:").lower()


def _selected_ids_from_tokens(
    response_text: str,
    *,
    by_id: dict[str, str],
    by_number: dict[str, str],
    by_label: dict[str, str],
) -> list[str]:
    selected: list[str] = []
    for raw_token in re.split(r"[\s,;]+", str(response_text or "")):
        token = _normalized_choice_token(raw_token)
        if not token:
            continue
        option_id = by_id.get(token) or by_number.get(token) or by_label.get(token)
        if option_id and option_id not in selected:
            selected.append(option_id)
    return selected


def selected_choice_ids_from_text(
    task_query: TaskQuery,
    response_text: str,
) -> list[str]:
    """Resolve stable choice identifiers from one user response."""

    choices = list(task_query.choices or [])
    if not choices:
        return []
    by_id, by_number, by_label = _choice_maps(choices)
    normalized_response = _normalized_choice_token(response_text)
    if normalized_response in by_id:
        return [by_id[normalized_response]]
    if normalized_response in by_label:
        return [by_label[normalized_response]]
    selected = _selected_ids_from_tokens(
        response_text,
        by_id=by_id,
        by_number=by_number,
        by_label=by_label,
    )
    if task_query.choice_mode != "multiple" and len(selected) > 1:
        return selected[:1]
    return selected


class GatewayClarificationCore:
    """Check that a chat's question still waits, and publish answers."""

    def __init__(
        self,
        *,
        settings: GatewayClarificationSettings,
        requests: GatewayRequestService,
        questions: GatewayPendingQuestions,
        sessions: GatewaySessionService,
        session_repository: SessionRepository,
        request_context: GatewayRequestContextService,
        manager: GatewayManagerClient,
        id_generator: GatewayIdGenerator,
        logger: logging.Logger | None = None,
    ) -> None:
        self._settings = settings
        self._requests = requests
        self._questions = questions
        self._sessions = sessions
        self._session_repository = session_repository
        self._request_context = request_context
        self._manager = manager
        self._id_generator = id_generator
        self._logger = logger or logging.getLogger(__name__)

    def expire_stale_on_startup(self) -> int:
        expired_count = self._questions.expire_stale()
        if expired_count:
            self._logger.warning(
                "Gateway expired %s waiting question(s) past the question timeout "
                "during startup. event=gateway_questions_expired_on_startup",
                expired_count,
            )
        return expired_count

    async def resolve_waiting_question(
        self,
        session_key: str,
    ) -> PendingQueryRecord | None:
        """The chat's first open question that still waits; ends stale ones.

        A question past the timeout, or whose task the lead no longer knows,
        is ended, and the next one in line is checked in its place.
        """

        while (question := self._questions.current(session_key)) is not None:
            if self._questions.is_expired(question):
                self._questions.close(question, status=PendingQueryStatus.EXPIRED)
                continue
            if not await self._parent_is_active(question):
                self._questions.close(question, status=PendingQueryStatus.REQUEST_ENDED)
                continue
            return question
        return None

    async def _parent_is_active(self, pending_query: PendingQueryRecord) -> bool:
        parent_task_id = pending_query.parent_task_id
        if not parent_task_id:
            return False
        manager_id = self._manager.manager_id
        if manager_id is None or not self._manager.connected:
            return True
        session = self._session_repository.get_session(pending_query.session_key)
        canonical_user_id = str(
            getattr(session, "owner_canonical_user_id", "") or ""
        ).strip()
        correlation_id = self._id_generator.new_correlation_id()
        request = TaskStatus(
            status_type=MessageMetaType.REQUEST,
            task_id=parent_task_id,
            target_agent_id=manager_id,
            target_named_agent_type=self._settings.manager_named_agent_type,
            correlation_id=correlation_id,
        )
        async with self._manager.control_channel(correlation_id) as queue:
            try:
                envelope = MessageEnvelope(
                    msg_content=request,
                    msg_header=MessageHeader(
                        from_agent_id=self._settings.response_route,
                        to_routing_path=manager_id,
                        response_routing_key=self._settings.response_route,
                        agent_signature="",
                        idempotency_key=request.task_status_id,
                    ),
                    msg_meta=MessageMeta(
                        from_agent_name=f"{self._settings.instance_name}_gateway",
                        msg_correlation_id=correlation_id,
                        conversation_id=pending_query.conversation_id,
                        request_timezone=self._settings.request_timezone,
                        requester_type=RequesterType.USER_REQUESTER,
                        is_response_requested=True,
                        gateway_context=(
                            self._request_context.build_gateway_context(
                                session_key=pending_query.session_key,
                                delivery_mode="task_status_probe",
                                route=GatewayRequestRoute(
                                    canonical_user_id=(canonical_user_id or None)
                                ),
                                manager_agent_id=manager_id,
                            )
                        ),
                    ),
                )
                await self._manager.publish_operational(envelope)
                response_envelope = await asyncio.wait_for(
                    queue.get(),
                    timeout=self._settings.parent_probe_timeout_seconds,
                )
                response = response_envelope.msg_content
                if (
                    response_envelope.msg_header.from_agent_id != manager_id
                    or not isinstance(response, TaskStatus)
                    or response.task_status_id != request.task_status_id
                ):
                    return True
                return bool(response.is_known_task)
            except (asyncio.TimeoutError, RuntimeError) as exc:
                self._logger.warning(
                    "Gateway could not validate parent task %s for pending "
                    "TaskQuery %s; preserving pending query: %s",
                    parent_task_id,
                    pending_query.task_query_id,
                    exc,
                )
                return True

    async def reply_to_task_query(
        self,
        task_query: TaskQuery,
        *,
        correlation_id: str,
        response_text: str,
        session_key: str,
        conversation_id: str | None,
        replied_message: RepliedMessage | None = None,
    ) -> None:
        manager_id = self._manager.manager_id
        if manager_id is None:
            raise RuntimeError(self._settings.manager_unavailable_text)
        session = self._sessions.resolve_session(
            requested_session_key=session_key,
            default_session_key=self._settings.default_session_key,
            manager_agent_id=manager_id,
            owner_canonical_user_id=(self._settings.default_owner_canonical_user_id),
            default_owner_canonical_user_id=(
                self._settings.default_owner_canonical_user_id
            ),
            route=GatewaySessionRoute(),
            enforce_owner_scope=False,
        )
        presentation = self._request_context.requester_presentation_for_session(session)
        envelope = MessageEnvelope(
            msg_content=TaskQuery(
                task_query_id=task_query.task_query_id,
                query_type=MessageMetaType.RESPONSE,
                message=response_text,
                replied_message=replied_message,
                parent_task_id=task_query.parent_task_id,
                choice_mode=task_query.choice_mode,
                choices=task_query.choices,
                allow_free_text=task_query.allow_free_text,
                selected_choice_ids=selected_choice_ids_from_text(
                    task_query,
                    response_text,
                ),
            ),
            msg_meta=MessageMeta(
                from_agent_name=presentation.display_name,
                msg_correlation_id=correlation_id,
                conversation_id=conversation_id,
                request_timezone=presentation.timezone,
                requester_type=RequesterType.USER_REQUESTER,
                is_response_requested=True,
                gateway_context=self._request_context.build_gateway_context(
                    session_key=session.session_key,
                    delivery_mode="interactive",
                    route=GatewayRequestRoute(
                        channel_name=session.channel_name,
                        user_id=session.user_id,
                        chat_id=session.chat_id,
                        thread_id=session.thread_id,
                        canonical_user_id=session.owner_canonical_user_id,
                    ),
                    manager_agent_id=manager_id,
                ),
            ),
            msg_header=MessageHeader(
                from_agent_id=self._settings.response_route,
                to_routing_path=manager_id,
                response_routing_key=self._settings.response_route,
                agent_signature="",
            ),
        )
        self._requests.record(
            correlation_id=correlation_id,
            session_key=session.session_key,
            request_kind="interactive_query_response",
            request_text=response_text,
            conversation_id=conversation_id,
            followup_to_task_id=task_query.parent_task_id,
            status="responded",
        )
        await self._manager.publish_durable(envelope, routing_key=manager_id)


class GatewayClarificationApplication:
    """Answer the question a chat shows, named by its id, then show the next."""

    def __init__(
        self,
        *,
        core: GatewayClarificationCore,
        questions: GatewayPendingQuestions,
        manager: GatewayManagerClient,
        responses: GatewayResponseCoordinator,
        local_responses: GatewayLocalResponseFactory,
        logger: logging.Logger | None = None,
    ) -> None:
        self._core = core
        self._questions = questions
        self._manager = manager
        self._responses = responses
        self._local_responses = local_responses
        self._logger = logger or logging.getLogger(__name__)

    async def waiting_question_id(self, session_key: str) -> str | None:
        """The question a message in this chat answers, if one waits.

        Callers pass the id back to ``reply``, so the answer goes to the
        question this check found even if another message answers it first.
        """

        question = await self._core.resolve_waiting_question(session_key)
        return question.task_query_id if question is not None else None

    async def reply(
        self,
        *,
        session_key: str,
        task_query_id: str,
        response_text: str,
        replied_message: RepliedMessage | None = None,
    ) -> GatewayResponse:
        question = self._questions.get(task_query_id)
        if question is None or question.session_key != session_key:
            raise ValueError(
                f"No question {task_query_id} waits in session_key={session_key}"
            )
        if not question.is_open:
            return self._not_sent(question)
        if not question.was_shown:
            # The person has not seen this question (its turn came while they
            # wrote), so their message cannot be its answer.
            self._questions.mark_shown(question)
            return self._show(question, intro=QUESTION_WAITING_INTRO_TEXT)

        correlation_id = question.correlation_id
        if self._manager.manager_id is None:
            # Nothing is sent, so the question stays open for a later answer.
            async with self._manager.response_channel(correlation_id):
                await self._responses.queue_manager_unavailable(correlation_id)
                return await self._responses.await_terminal_response(
                    correlation_id=correlation_id,
                    session_key=session_key,
                    query_handler=None,
                )
        if not self._questions.claim_answer(task_query_id):
            # Another message answered it, or it ended, since the check.
            return self._not_sent(self._questions.get(task_query_id) or question)
        async with self._manager.response_channel(correlation_id):
            try:
                await self._core.reply_to_task_query(
                    stored_task_query(question),
                    correlation_id=correlation_id,
                    response_text=response_text,
                    session_key=session_key,
                    conversation_id=question.conversation_id,
                    replied_message=replied_message,
                )
            except Exception:
                self._questions.release_answer(question)
                raise
            self._logger.info(
                "Sent a person's answer to its question. event=gateway_question_answered "
                "task_query_id=%s parent_task_id=%s correlation_id=%s session_key=%s",
                task_query_id,
                question.parent_task_id,
                correlation_id,
                session_key,
            )
            # Publication consumed this answer. Waiting may store a new
            # question, or time out while the lead continues; neither permits
            # sending the answer again.
            await self._questions.show_next(session_key)
            return await self._responses.await_terminal_response(
                correlation_id=correlation_id,
                session_key=session_key,
                query_handler=None,
            )

    def _not_sent(self, question: PendingQueryRecord) -> GatewayResponse:
        """Refuse a message for a question that is no longer open.

        When the chat's next question has not been shown yet, the refusal
        shows it, so the person sees what their next reply answers.
        """

        reason = (
            ALREADY_ANSWERED_TEXT
            if question.status == PendingQueryStatus.ANSWERED
            else NO_LONGER_WAITING_TEXT
        )
        following = self._questions.current(question.session_key)
        if (
            following is not None
            and not following.was_shown
            and self._questions.mark_shown(following)
        ):
            return self._show(following, intro=f"{reason}\n\n{NEXT_QUESTION_INTRO_TEXT}")
        return self._local_responses.create(
            text=reason,
            message_type=NOTE_MESSAGE_TYPE,
            conversation_id=question.conversation_id,
        )

    def _show(self, question: PendingQueryRecord, *, intro: str) -> GatewayResponse:
        return self._local_responses.create(
            text=f"{intro}\n\n{question.query_text}",
            message_type=QUESTION_MESSAGE_TYPE,
            correlation_id=question.correlation_id,
            conversation_id=question.conversation_id,
        )
