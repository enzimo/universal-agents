"""Gateway request-coordination persistence records.

A thread (one session conversation) runs one request at a time. The request
a person's message starts is recorded as running (``RUN_STATE_RUNNING``) from
admission until the gateway records its final reply, together with the task
id the gateway gave it. A later message in that thread is guidance for it
(``assistant_gateway.application.thread_guidance``). This durable state, not
whether some HTTP or chat wait is still open, decides guidance or new work:
reply waits end at timeouts, cancellations and restarts while the request
keeps running.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

# A person's message that starts a request in its thread.
REQUEST_KIND_INTERACTIVE = "interactive"
# A later message in a busy thread, sent to the running request as guidance.
REQUEST_KIND_TASK_STEERING = "task_steering"

# Set only on a thread's request; other request rows (guidance, controls,
# question answers, automation, rows written before this field existed) keep
# None and never make a thread busy.
RUN_STATE_RUNNING = "running"
RUN_STATE_FINISHED = "finished"

# The running request paused with a question for the person (a manager
# interrupt request); the person's next message answers it.
REQUEST_STATUS_WAITING_FOR_STEERING = "waiting_for_steering"

# Guidance request statuses the gateway sets itself. The lead's answers use
# TaskSteeringStatus values (accepted, rejected, stale, expired).
GUIDANCE_STATUS_HELD = "held"
GUIDANCE_STATUS_REQUEST_FINISHED = "request_finished"


class ThreadRequestRunning(Exception):
    """Another request already runs in this thread, so a new one was not admitted."""

    def __init__(self, session_key: str, conversation_id: str | None) -> None:
        super().__init__(
            f"Thread {session_key} conversation {conversation_id or '-'} already has a running request."
        )
        self.session_key = session_key
        self.conversation_id = conversation_id


@dataclass
class RequestRecord:
    correlation_id: str
    session_key: str
    request_kind: str
    request_text: str
    conversation_id: str | None
    followup_to_task_id: str | None
    created_at: str
    status: str
    owner_canonical_user_id: str | None = None
    # The request's own task: given by the gateway when it admits a person's
    # request, later the task a manager reply names.
    active_task_id: str | None = None
    run_state: str | None = None

    def matches_task(self, task_id: str | None) -> bool:
        """A tracked chat request changes only for its admitted task.

        Other rows describe controls, guidance or repeating schedules rather
        than one running chat request, so they have no fixed admission task.
        A schedule action may share a tracked request's correlation, but that
        never makes the schedule's tasks replies to the original request.
        """

        return self.run_state is None or self.active_task_id == task_id


@dataclass(frozen=True)
class HeldGuidanceRecord:
    """Guidance the running request has not confirmed yet, kept until it does.

    ``steering_json`` is the person's ``TaskSteering`` (text, attachments,
    the message replied to, guidance or interrupt answer) without its target;
    the target is read from ``target_correlation_id``'s request each time the
    gateway sends it, under the guidance's own correlation id as its
    idempotency key, so a repeat is a duplicate the lead ignores.
    """

    correlation_id: str
    target_correlation_id: str
    session_key: str
    conversation_id: str | None
    steering_json: str
    created_at: str


@dataclass(frozen=True)
class CredentialCollectionRecord:
    """A form's recoverable watch, without submitted fields, secrets or page addresses."""

    credential_request_id: str
    broker_session_id: str
    person_id: str
    session_key: str
    correlation_id: str
    conversation_id: str | None
    parent_task_id: str | None
    service_name: str
    credential_key: str
    display_name: str | None
    flow: str
    source: str | None
    source_item_id: str | None
    source_item_name: str | None
    continue_task: bool
    expires_at_utc: str


@dataclass
class ToolSetupRecord:
    """One setup page a task waits on (``assistant_gateway.application.tool_setup``).

    ``status`` is ``waiting`` until the page's result is sent to the task,
    then how it ended (``completed``, ``declined``, ``expired``,
    ``unavailable``). Finishing is a conditional update, so exactly one result
    reaches the task even when the page's watcher and the person's own
    ``/link`` finish together. The row outlives a gateway restart, so a page
    nobody watches any more is reported instead of leaving the task waiting.
    ``correlation_id`` is the chat request the task answers. Page addresses
    are never stored.
    """

    setup_request_id: str
    parent_task_id: str
    person_id: str
    tool_id: str
    kind: str
    owner_kind: str
    session_key: str
    correlation_id: str
    conversation_id: str | None
    status: str
    created_at: str
    finished_at: str | None = None


class PendingQueryStatus(StrEnum):
    """Where one agent question to a person stands (``gateway_questions``).

    A question is ``open`` until exactly one of the others ends it; every end
    is a conditional update from ``open``, so two answers, or an answer and an
    expiry, cannot both take the same question.
    """

    OPEN = "open"
    ANSWERED = "answered"
    # The gateway's question timeout passed (``manager.request_timeout_seconds``).
    EXPIRED = "expired"
    # Its request finished, or the lead no longer knows its task.
    REQUEST_ENDED = "request_ended"
    # Its own request asked a newer question, which took its place in line.
    REPLACED = "replaced"


@dataclass
class PendingQueryRecord:
    """One agent question to a person, kept until it is answered or ends.

    ``task_query_id`` is the question's durable id: the agent gives every
    question its own, and the answer carries it back. A chat session can hold
    several open questions; they wait in ``queued_at`` order and the person sees
    one at a time (``assistant_gateway.application.pending_questions``).
    ``shown_at`` is set once the person was shown the question itself;
    ``created_at`` is when the agent asked, which its timeout counts from.
    """

    session_key: str
    correlation_id: str
    task_query_id: str
    parent_task_id: str | None
    conversation_id: str | None
    query_text: str
    created_at: str
    queued_at: str
    status: str
    query_payload_json: str | None = None
    owner_canonical_user_id: str | None = None
    shown_at: str | None = None
    closed_at: str | None = None

    @property
    def is_open(self) -> bool:
        return self.status == PendingQueryStatus.OPEN

    @property
    def was_shown(self) -> bool:
        return self.shown_at is not None


@dataclass(frozen=True)
class PendingQueryAdded:
    """What storing one arriving question did.

    ``stored`` is the question as kept: the new row, or the row already kept
    under the same id when the same question arrived again (a redelivered
    message). ``replaced`` lists open questions of the same request that the
    new one took the place of.
    """

    stored: PendingQueryRecord
    created: bool
    replaced: tuple[PendingQueryRecord, ...] = ()
