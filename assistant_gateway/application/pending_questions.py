"""Agent questions waiting on a person in one chat, asked one at a time.

Several tasks can wait on the same person in the same chat. Scheduled work
asks its questions in the chat it reports to, alongside the chat's interactive
request. The gateway once kept one question per
chat session, so a later question erased the earlier one: the person's answer
to the question they saw first went to the other request, and the first
request waited until its own limit.

Every question is now kept under its own id (``TaskQuery.task_query_id``) until
it is answered or ends, and a chat's open questions wait in line:

- The person is shown the first open question. A question that arrives while
  another is shown gets a short note instead, and is shown in its turn.
- A plain reply answers the shown question, and only that one. Callers name it
  (``waiting_question_id`` and then ``reply``), and taking the answer is a
  conditional update, so a second answer to the same question is refused
  instead of resuming the task twice.
- When the shown question is answered, expires or its task ends, the next
  question is shown.
- A newer question of the same task takes the place of that task's open
  question: the lead waits only for a task's latest question.

Chat apps show questions as text and none shows answer buttons, so one shown
question per chat is what every app can present unambiguously.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from enum import StrEnum

from assistant_gateway.application.delivery_presentation import (
    GatewayDeliveryPresentation,
)
from assistant_gateway.application.gateway_application import GatewayDeliveryEvent
from assistant_gateway.application.ports import GatewayClock, GatewayEventOutput
from assistant_gateway.domain.request_state import (
    PendingQueryRecord,
    PendingQueryStatus,
)
from assistant_gateway.ports.repositories import RequestRepository, SessionRepository
from custom_messages.contracts.task_v1 import TaskQuery
from custom_messages.enums import MessageMetaType


# What the person reads. A question's own text comes from the agent.
QUESTION_WAITS_IN_LINE_TEXT = (
    "I have another question for you, about a different request. "
    "I'll ask it after you answer the question that is waiting."
)
ALREADY_ANSWERED_TEXT = "That question already has an answer, so this message was not sent."
NO_LONGER_WAITING_TEXT = (
    "The question you were answering is no longer waiting, so this message was not sent."
)
QUESTION_WAITING_INTRO_TEXT = (
    "A question is waiting for your answer, so this message was not sent. "
    "Reply to answer it:"
)

QUESTION_MESSAGE_TYPE = "task_query"
NOTE_MESSAGE_TYPE = "system_status"


class QuestionDisplay(StrEnum):
    """What a chat shows when a question arrives."""

    # The question is the chat's shown question: show its text.
    QUESTION = "question"
    # Another question is shown: show the note that this one waits in line.
    WAITS_IN_LINE = "waits_in_line"
    # The same question arrived again after it ended: show nothing.
    NOTHING = "nothing"


@dataclass(frozen=True)
class QuestionArrival:
    question: PendingQueryRecord
    display: QuestionDisplay

    @property
    def text(self) -> str:
        if self.display == QuestionDisplay.WAITS_IN_LINE:
            return QUESTION_WAITS_IN_LINE_TEXT
        return self.question.query_text

    @property
    def message_type(self) -> str:
        if self.display == QuestionDisplay.WAITS_IN_LINE:
            return NOTE_MESSAGE_TYPE
        return QUESTION_MESSAGE_TYPE


def restore_task_query(
    payload_json: str | None,
    fallback: TaskQuery,
    *,
    logger: logging.Logger | None = None,
) -> TaskQuery:
    """Restore a persisted structured query, using its trusted fallback."""

    if not payload_json:
        return fallback
    try:
        return TaskQuery.model_validate_json(payload_json)
    except Exception:
        (logger or logging.getLogger(__name__)).warning(
            "Failed to restore structured TaskQuery payload from pending query state.",
            exc_info=True,
        )
        return fallback


def stored_task_query(
    question: PendingQueryRecord, *, logger: logging.Logger | None = None
) -> TaskQuery:
    """The question as the agent sent it, rebuilt from its row."""

    return restore_task_query(
        question.query_payload_json,
        TaskQuery(
            task_query_id=question.task_query_id,
            query_type=MessageMetaType.REQUEST,
            message=question.query_text,
            parent_task_id=question.parent_task_id,
        ),
        logger=logger,
    )


class GatewayPendingQuestions:
    """Keep each chat's agent questions in line and show them one at a time."""

    def __init__(
        self,
        *,
        repository: RequestRepository,
        sessions: SessionRepository,
        event_output: GatewayEventOutput,
        clock: GatewayClock,
        timeout_seconds: float | None,
        logger: logging.Logger | None = None,
    ) -> None:
        self._repository = repository
        self._sessions = sessions
        self._event_output = event_output
        self._clock = clock
        self._timeout_seconds = timeout_seconds if timeout_seconds is not None and timeout_seconds > 0 else None
        self._logger = logger or logging.getLogger(__name__)

    # -- arriving questions -------------------------------------------------

    def ask(
        self,
        *,
        session_key: str,
        correlation_id: str,
        conversation_id: str | None,
        query: TaskQuery,
        rendered_text: str,
    ) -> QuestionArrival:
        """Keep one arriving question and say what the chat should show.

        The question is stored, and marked shown when it is the chat's first
        open question, before the caller displays anything: a person can
        answer as soon as the chat shows it.
        """

        added = self._repository.add_pending_query(
            session_key=session_key,
            correlation_id=correlation_id,
            task_query_id=query.task_query_id,
            parent_task_id=query.parent_task_id,
            conversation_id=conversation_id,
            query_text=rendered_text,
            query_payload_json=query.model_dump_json(),
        )
        for earlier in added.replaced:
            self._logger.info(
                "A task asked a newer question; it takes the earlier one's place. "
                "event=gateway_question_replaced task_query_id=%s "
                "replaced_task_query_id=%s parent_task_id=%s correlation_id=%s "
                "session_key=%s",
                query.task_query_id,
                earlier.task_query_id,
                query.parent_task_id,
                correlation_id,
                session_key,
            )
        question = added.stored
        if not added.created:
            return self._repeated_arrival(question)
        current = self.current(session_key)
        if current is not None and current.task_query_id == question.task_query_id:
            self._repository.mark_pending_query_shown(question.task_query_id)
            return QuestionArrival(question, QuestionDisplay.QUESTION)
        self._logger.info(
            "A question waits behind the question the chat shows. "
            "event=gateway_question_waits_in_line task_query_id=%s "
            "shown_task_query_id=%s correlation_id=%s session_key=%s",
            question.task_query_id,
            current.task_query_id if current is not None else "-",
            correlation_id,
            session_key,
        )
        return QuestionArrival(question, QuestionDisplay.WAITS_IN_LINE)

    def _repeated_arrival(self, question: PendingQueryRecord) -> QuestionArrival:
        """The same question arrived again, as after a redelivered message."""

        self._logger.warning(
            "A question arrived again; its stored state is kept. "
            "event=gateway_question_repeated task_query_id=%s status=%s "
            "correlation_id=%s session_key=%s",
            question.task_query_id,
            question.status,
            question.correlation_id,
            question.session_key,
        )
        if not question.is_open:
            return QuestionArrival(question, QuestionDisplay.NOTHING)
        if question.was_shown:
            return QuestionArrival(question, QuestionDisplay.QUESTION)
        return QuestionArrival(question, QuestionDisplay.WAITS_IN_LINE)

    # -- reading ------------------------------------------------------------

    def current(self, session_key: str) -> PendingQueryRecord | None:
        """The chat's first open question: the one a reply would answer."""

        open_questions = self._repository.list_open_pending_queries(session_key)
        return open_questions[0] if open_questions else None

    def get(self, task_query_id: str) -> PendingQueryRecord | None:
        return self._repository.get_pending_query(task_query_id)

    def is_expired(self, question: PendingQueryRecord) -> bool:
        """Older than the gateway's question timeout, counted from the asking."""

        if self._timeout_seconds is None:
            return False
        try:
            asked_at = _parse_timestamp(question.created_at)
        except ValueError:
            self._logger.warning(
                "A question has an unreadable time; treating it as expired. "
                "event=gateway_pending_query_invalid_created_at "
                "task_query_id=%s session_key=%s created_at=%r",
                question.task_query_id,
                question.session_key,
                question.created_at,
            )
            return True
        return self._clock.now_utc() - asked_at > timedelta(seconds=self._timeout_seconds)

    # -- ending questions ---------------------------------------------------

    def claim_answer(self, task_query_id: str) -> bool:
        """Take the answer for one open question; False when it already ended."""

        return self._repository.close_pending_query(
            task_query_id, status=PendingQueryStatus.ANSWERED
        )

    def release_answer(self, question: PendingQueryRecord) -> None:
        """Open a claimed question again: its answer could not be sent."""

        if not self._repository.reopen_answered_pending_query(question.task_query_id):
            self._logger.error(
                "A question whose answer could not be sent was not reopened. "
                "event=gateway_question_reopen_failed task_query_id=%s session_key=%s",
                question.task_query_id,
                question.session_key,
            )

    def close(self, question: PendingQueryRecord, *, status: PendingQueryStatus) -> bool:
        closed = self._repository.close_pending_query(
            question.task_query_id, status=status
        )
        if closed:
            self._logger.warning(
                "A question ended without an answer. event=gateway_question_closed "
                "task_query_id=%s status=%s parent_task_id=%s correlation_id=%s "
                "session_key=%s",
                question.task_query_id,
                status.value,
                question.parent_task_id,
                question.correlation_id,
                question.session_key,
            )
        return closed

    async def task_finished(self, *, correlation_id: str | None, task_id: str | None) -> None:
        """A task's final reply arrived: its questions end, and the next is shown."""

        if not correlation_id or not task_id:
            return
        closed = self._repository.close_open_pending_queries_for_task(
            correlation_id=correlation_id,
            parent_task_id=task_id,
            status=PendingQueryStatus.REQUEST_ENDED,
        )
        for question in closed:
            self._logger.info(
                "A question ended because its task finished. "
                "event=gateway_question_task_finished task_query_id=%s "
                "parent_task_id=%s correlation_id=%s session_key=%s",
                question.task_query_id,
                task_id,
                correlation_id,
                question.session_key,
            )
        for session_key in dict.fromkeys(question.session_key for question in closed):
            await self.show_next(session_key)

    def expire_stale(self) -> int:
        if self._timeout_seconds is None:
            return 0
        cutoff = self._clock.now_utc() - timedelta(seconds=self._timeout_seconds)
        return self._repository.expire_pending_queries(cutoff_iso=cutoff.isoformat())

    # -- showing the next question ------------------------------------------

    def mark_shown(self, question: PendingQueryRecord) -> bool:
        return self._repository.mark_pending_query_shown(question.task_query_id)

    async def show_next(self, session_key: str) -> PendingQueryRecord | None:
        """Show the chat's first open question if the person has not seen it.

        The mark is conditional, so only one caller shows a question even when
        an answer and a finished task make room at the same time.
        """

        question = self.current(session_key)
        if question is None or question.was_shown:
            return None
        if not self.mark_shown(question):
            return None
        session = self._sessions.get_session(session_key)
        query = stored_task_query(question, logger=self._logger)
        event = GatewayDeliveryEvent(
            text=question.query_text,
            session_key=session_key,
            channel_name=session.channel_name if session else None,
            user_id=session.user_id if session else None,
            chat_id=session.chat_id if session else None,
            thread_id=session.thread_id if session else None,
            conversation_id=question.conversation_id,
            scheduled_job_id=None,
            correlation_id=question.correlation_id,
            original_message_id=None,
            message_type=QUESTION_MESSAGE_TYPE,
            message_level=GatewayDeliveryPresentation.message_level(query),
        )
        try:
            await self._event_output.emit(event)
        except BaseException as exc:
            # The mark reserves one display attempt. A failed or cancelled
            # attempt must not make a later message answer an unseen question.
            self._repository.clear_pending_query_shown(question.task_query_id)
            self._logger.warning(
                "The next question could not be shown and remains waiting. "
                "event=gateway_question_display_failed task_query_id=%s "
                "session_key=%s error_type=%s",
                question.task_query_id,
                session_key,
                type(exc).__name__,
            )
            raise
        self._logger.info(
            "Showed the next waiting question. event=gateway_question_shown "
            "task_query_id=%s correlation_id=%s session_key=%s",
            question.task_query_id,
            question.correlation_id,
            session_key,
        )
        return question


def _parse_timestamp(raw_timestamp: str) -> datetime:
    timestamp = datetime.fromisoformat(raw_timestamp)
    if timestamp.tzinfo is None:
        timestamp = timestamp.replace(tzinfo=timezone.utc)
    return timestamp
