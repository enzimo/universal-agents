"""Gateway session-domain persistence records."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

from assistant_gateway.domain.message_types import (
    COMMAND_REPLY_MESSAGE_TYPES,
    LINK_RESULT_MESSAGE_TYPE,
    TASK_PROGRESS_STATUS_MESSAGE_TYPE,
)
from assistant_gateway.domain.transcript import TRANSCRIPT_SOURCE_GUIDANCE_ANSWER


class ParallelSettingOutcome(StrEnum):
    """What a change to a thread's **Run concurrently** setting did.

    The setting belongs to the thread's current conversation, and each request
    takes it when the gateway admits the request. It changes only while no
    request is open in that conversation: a message sent while one is open is
    guidance for that request and reaches it in its own lane, so a change then
    could not apply to anything the person can see (owner, 2026-10-29).
    """

    SAVED = "saved"
    REQUEST_OPEN = "request_open"
    CONVERSATION_CHANGED = "conversation_changed"
    NOT_FOUND = "not_found"


class ThreadHiddenAs(StrEnum):
    """How a person set a Web Chat thread aside (``gateway_sessions.hidden_as``).

    Both are undone on read rather than by the code that posts messages
    (``assistant_gateway.application.web_chat_threads``): a snoozed thread
    wakes at its time or when the team posts after ``hidden_at_sequence``,
    and a dismissed thread returns when anything new is posted after it.
    """

    SNOOZED = "snoozed"
    DISMISSED = "dismissed"


# What does not count as "the team posting" for a snoozed thread (owner's
# rule: it comes back early when the team posts; every message the team writes
# wakes it). Only the person's own rows (role ``user``) and the gateway's
# automatic answers to something the person did stay silent, each told by
# what produced it, marked when it was written:
# - a slash command's reply (its own message types);
# - how a /link the person ran ended (``link_result``);
# - the answer to "?" (``task_progress_status``, only ever that answer);
# - the answer to a message sent while a request ran (source kind
#   ``gateway_guidance_answer``; the team's own pause that asks for a reply
#   has the same message type and wakes it).
# Anything not listed wakes it: a thread that comes back once too often costs
# a glance, one that stays hidden hides the team's message.
SNOOZE_SILENT_SOURCE_KINDS = (TRANSCRIPT_SOURCE_GUIDANCE_ANSWER,)
SNOOZE_SILENT_MESSAGE_TYPES = (
    *COMMAND_REPLY_MESSAGE_TYPES,
    LINK_RESULT_MESSAGE_TYPE,
    TASK_PROGRESS_STATUS_MESSAGE_TYPE,
)


@dataclass(frozen=True)
class ThreadPlacementRecord:
    """Where one active thread sits now, read to clear a Woke mark safely.

    The same raw values the thread list reads (``WebChatThreadRecord``), so
    the one woke rule (``web_chat_threads.thread_list_state``) applies to
    both; ``clear_session_hidden(only_if=...)`` clears only while the stored
    values still equal these.
    """

    session_key: str
    hidden_as: str | None
    hidden_until: str | None
    hidden_at_sequence: int | None
    last_sequence: int
    team_posted_after_hiding: bool


@dataclass
class SessionRecord:
    session_key: str
    session_alias: str | None
    conversation_id: str | None
    last_task_id: str | None
    manager_agent_id: str | None
    channel_name: str | None
    user_id: str | None
    chat_id: str | None
    thread_id: str | None
    status: str
    created_at: str
    updated_at: str
    owner_canonical_user_id: str | None = None
    # The thread's Run concurrently setting in Web Chat: a request admitted
    # while it is on may run at the same time as the team lead's other work
    # (custom_agents.execution_lanes). Changed only through
    # ``set_session_parallel``; stored as an integer column.
    parallel_thread: bool = False

    def __post_init__(self) -> None:
        self.parallel_thread = bool(self.parallel_thread)


@dataclass
class SessionConversationRecord:
    session_key: str
    conversation_id: str
    last_task_id: str | None
    created_at: str
    updated_at: str
    owner_canonical_user_id: str | None = None


@dataclass
class SessionSearchRecord:
    session_key: str
    session_alias: str | None
    conversation_id: str | None
    last_task_id: str | None
    manager_agent_id: str | None
    channel_name: str | None
    user_id: str | None
    chat_id: str | None
    thread_id: str | None
    status: str
    created_at: str
    updated_at: str
    matched_request_text: str | None
    owner_canonical_user_id: str | None = None


@dataclass
class SessionListRecord:
    session_key: str
    session_alias: str | None
    conversation_id: str | None
    last_task_id: str | None
    manager_agent_id: str | None
    channel_name: str | None
    user_id: str | None
    chat_id: str | None
    thread_id: str | None
    status: str
    created_at: str
    updated_at: str
    latest_request_text: str | None
    owner_canonical_user_id: str | None = None


@dataclass
class WebChatThreadRecord:
    """One of a person's Web Chat threads, with what names it.

    ``first_message_text`` and ``first_message_attachments_json`` come from
    the thread's first ordinary message the person typed: the earliest user
    transcript row that is not a slash command and has text or attachments.
    ``has_messages`` says whether the thread has any transcript row at all.
    ``parallel_thread`` is its Run concurrently setting, and
    ``has_open_request`` whether a request is open in its current
    conversation, which locks that setting (``ParallelSettingOutcome``).
    The remaining fields are raw store values; the application derives the
    thread's section, waiting reason and snippet from them
    (``assistant_gateway.application.web_chat_threads``).
    """

    session_key: str
    session_alias: str | None
    conversation_id: str | None
    owner_canonical_user_id: str | None
    updated_at: str
    first_message_text: str | None
    first_message_attachments_json: str | None
    has_messages: bool
    parallel_thread: bool = False
    has_open_request: bool = False
    # The latest ordinary message (not a system card, not a slash command),
    # read only for the sessions route (``include_last_message``); its text is
    # cut to ``LAST_MESSAGE_SQL_CHARACTERS`` in the query.
    last_message_role: str | None = None
    last_message_text: str | None = None
    last_message_attachments_json: str | None = None
    last_message_at: str | None = None
    # When anything was last posted in the thread and its last session
    # sequence (``gateway_transcript_session_sequences``); 0 with no message.
    last_activity_at: str | None = None
    last_sequence: int = 0
    # What the thread may be waiting on from the person: an open question,
    # the status of its running request, setup pages still open for the
    # person in it, and when each credential form for the person in it
    # expires (as the broker wrote it; the application compares it with now).
    has_open_question: bool = False
    running_request_status: str | None = None
    has_waiting_setup: bool = False
    credential_form_expiries: tuple[str, ...] = ()
    # The person's placement of the thread: pinned, and snoozed or dismissed
    # (``ThreadHiddenAs``) with the snooze's wake time and the last message
    # sequence the person had seen when setting it aside.
    pinned: bool = False
    hidden_as: str | None = None
    hidden_until: str | None = None
    hidden_at_sequence: int | None = None
    # Whether the team posted after a snoozed thread's ``hidden_at_sequence``
    # (``SNOOZE_SILENT_*`` say what does not count); read only for snoozed threads.
    team_posted_after_hiding: bool = False
    # Web Chat's agent trail: the thread's stored copy (``assistant_gateway.domain.agent_trail``), and the task of
    # the request running in its current conversation, which agents name in their records.
    agent_trail_json: str | None = None
    running_task_id: str | None = None


@dataclass
class SessionConversationListRecord:
    session_key: str
    conversation_id: str
    last_task_id: str | None
    created_at: str
    updated_at: str
    latest_request_text: str | None
    owner_canonical_user_id: str | None = None
