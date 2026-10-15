"""Gateway session-domain persistence records."""

from __future__ import annotations

from dataclasses import dataclass


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
    """

    session_key: str
    session_alias: str | None
    conversation_id: str | None
    owner_canonical_user_id: str | None
    updated_at: str
    first_message_text: str | None
    first_message_attachments_json: str | None
    has_messages: bool


@dataclass
class SessionConversationListRecord:
    session_key: str
    conversation_id: str
    last_task_id: str | None
    created_at: str
    updated_at: str
    latest_request_text: str | None
    owner_canonical_user_id: str | None = None
