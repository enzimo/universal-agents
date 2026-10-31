"""Gateway session-domain persistence records."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum


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


@dataclass
class SessionConversationListRecord:
    session_key: str
    conversation_id: str
    last_task_id: str | None
    created_at: str
    updated_at: str
    latest_request_text: str | None
    owner_canonical_user_id: str | None = None
