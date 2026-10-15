"""Which of a person's Web Chat threads the sidebar lists, and what each is called.

Besides the threads a person starts, the gateway keeps delivery threads of
its own on the person's default chat: the team's proactive session (heartbeat
updates and other team notices, ``proactive_delivery``) and each person's
memory-review session (``person_maintenance``). The gateway makes sure they
exist at every start, so a newly set-up Web Chat used to list two empty
"New Chat" rows, and the first message went into the memory-review thread
(owner's report, 2026-10-09). Now a delivery thread is listed only once
something has arrived in it, under a fixed name.

A thread's name is fixed once given (owner's request, 2026-10-09; it used to
follow the latest message): the name the person gave it, otherwise the
delivery thread's fixed name, otherwise the first ordinary message the person
typed in it (the earliest that is not a slash command), otherwise the file
name of that message's first attachment. A thread with none of these has no
name, and the page shows "New Chat".
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from enum import StrEnum

from assistant_gateway.application.person_maintenance import is_person_maintenance_session_key
from assistant_gateway.application.proactive_delivery import is_proactive_session_key
from assistant_gateway.domain.sessions import WebChatThreadRecord

LOGGER = logging.getLogger(__name__)

# Long first messages are cut here; the page shows the start of the name
# and the full name in a tooltip, so more would only grow every list read.
TITLE_MAX_CHARACTERS = 200
ATTACHMENT_TITLE = "Attachment"


class WebChatThreadKind(StrEnum):
    """Who a Web Chat thread is for."""

    CONVERSATION = "conversation"
    UPDATES = "updates"
    MEMORY_REVIEW = "memory_review"


# The fixed names of the gateway's delivery threads.
DELIVERY_THREAD_NAMES = {
    WebChatThreadKind.UPDATES: "Updates",
    WebChatThreadKind.MEMORY_REVIEW: "Memory review",
}


@dataclass(frozen=True)
class WebChatThread:
    """One row of the Web Chat thread list. ``title`` is ``None`` until named."""

    session_key: str
    session_alias: str | None
    conversation_id: str | None
    title: str | None
    updated_at: str
    kind: WebChatThreadKind


def thread_kind(session_key: str, *, proactive_session_key: str) -> WebChatThreadKind:
    """Whether a thread is the person's own or one of the gateway's delivery threads."""

    if is_proactive_session_key(session_key, base_session_key=proactive_session_key):
        return WebChatThreadKind.UPDATES
    if is_person_maintenance_session_key(session_key):
        return WebChatThreadKind.MEMORY_REVIEW
    return WebChatThreadKind.CONVERSATION


def _shortened(text: str) -> str:
    collapsed = " ".join(text.split())
    if len(collapsed) <= TITLE_MAX_CHARACTERS:
        return collapsed
    return collapsed[: TITLE_MAX_CHARACTERS - 1].rstrip() + "…"


def _first_attachment_name(record: WebChatThreadRecord) -> str | None:
    try:
        references = json.loads(record.first_message_attachments_json or "[]")
    except json.JSONDecodeError:
        LOGGER.warning(
            "A Web Chat thread's first message has unreadable attachment references; it is named "
            "'Attachment'. event=web_chat_thread_title_attachments_unreadable session_key=%s",
            record.session_key,
        )
        return ATTACHMENT_TITLE
    if not references:
        return None
    first = references[0] if isinstance(references[0], dict) else {}
    return str(first.get("filename") or "").strip() or ATTACHMENT_TITLE


def first_message_title(record: WebChatThreadRecord) -> str | None:
    """The name a thread's first ordinary message gives it, if it has one."""

    text = (record.first_message_text or "").strip()
    if text:
        return _shortened(text)
    name = _first_attachment_name(record)
    return _shortened(name) if name else None


def web_chat_thread(record: WebChatThreadRecord, *, proactive_session_key: str) -> WebChatThread | None:
    """The thread-list row for one session, or ``None`` when it is not listed."""

    kind = thread_kind(record.session_key, proactive_session_key=proactive_session_key)
    if kind is not WebChatThreadKind.CONVERSATION and not record.has_messages:
        return None
    alias = (record.session_alias or "").strip() or None
    title = alias or DELIVERY_THREAD_NAMES.get(kind) or first_message_title(record)
    return WebChatThread(
        session_key=record.session_key,
        session_alias=record.session_alias,
        conversation_id=record.conversation_id,
        title=title,
        updated_at=record.updated_at,
        kind=kind,
    )


__all__ = [
    "ATTACHMENT_TITLE",
    "DELIVERY_THREAD_NAMES",
    "TITLE_MAX_CHARACTERS",
    "WebChatThread",
    "WebChatThreadKind",
    "first_message_title",
    "thread_kind",
    "web_chat_thread",
]
