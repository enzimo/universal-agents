"""Which of a person's Web Chat threads the sidebar lists, and what each is called.

Besides the threads a person starts, the gateway keeps delivery threads of
its own on the person's default chat: the team's proactive session (heartbeat
updates and other team notices, ``proactive_delivery``) and each person's
memory-review session (``person_maintenance``). The gateway makes sure they
exist at every start, so a newly set-up Web Chat used to list two empty
"New Chat" rows, and the first message went into the memory-review thread
(owner's report, 2026-10-09). Now a delivery thread is listed only once
something has arrived in it, under a fixed name.

Each row also shows the thread's latest ordinary message (not a system card,
not a slash command), when anything was last posted, what the thread waits on
from the person ("Needs you"), and where the person placed it: pinned, snoozed
or dismissed. Placement is stored as a baseline on the thread
(``gateway_sessions``) and undone here, when the list is read, never by the
code that posts messages: a snoozed thread wakes at its time or when the team
posts after it was snoozed, and stays marked "woke" until the person opens it
(the page then sends ``clear_woke``, and the gateway clears the mark only while
the thread is still woke by this rule, so an old page cannot undo a newer
snooze); a dismissed thread comes back as soon as anything new is posted.
Open pages read the list every two seconds (``web_chat_thread_states``), so a
wake shows within seconds without a background job.

Each row also carries its agent trail (``assistant_gateway.domain.agent_trail``):
the last turns, the person's and each agent type's, and who works on the
thread's running request now (``request_agents_service.working``), both with
the names the page shows. A change to the trail changes the list's revision;
who works now rides on every live read.

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
import threading
from collections import OrderedDict
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from enum import StrEnum
from typing import Mapping, Protocol

from assistant_gateway.application.person_maintenance import is_person_maintenance_session_key
from assistant_gateway.application.request_agents_service import WorkingAgent, agent_name
from assistant_gateway.domain.agent_trail import EMPTY_TRAIL, AgentTrailUnreadable, ThreadTrail, decode_thread_trail
from assistant_gateway.application.proactive_delivery import is_proactive_session_key
from assistant_gateway.domain.request_state import (
    REQUEST_STATUS_WAITING_FOR_CREDENTIAL,
    REQUEST_STATUS_WAITING_FOR_STEERING,
)
from assistant_gateway.domain.sessions import ThreadHiddenAs, WebChatThreadRecord

LOGGER = logging.getLogger(__name__)

# Long first messages are cut here; the page shows the start of the name
# and the full name in a tooltip, so more would only grow every list read.
TITLE_MAX_CHARACTERS = 200
ATTACHMENT_TITLE = "Attachment"
# The furthest ahead a snooze may wake (the page offers up to next week, or a
# picked time).
SNOOZE_MAX_AHEAD = timedelta(days=366)


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


class NeedsYouReason(StrEnum):
    """What a thread waits on from the person, in the order the list checks them.

    A setup page comes before a credential wait because a page opened by the
    request's own task also sets the request to waiting for a credential
    (``tool_setup``), and the same card should get the same label.
    """

    QUESTION = "question"
    REPLY = "reply"
    SETUP = "setup"
    CREDENTIAL = "credential"


class ThreadListState(StrEnum):
    """Where a thread the person set aside stands now (``thread_list_state``)."""

    SNOOZED = "snoozed"
    # The snooze ended (its time came or the team posted); shown in its usual
    # section with a mark until the person opens the thread.
    WOKE = "woke"
    DISMISSED = "dismissed"


class InvalidThreadPlacement(ValueError):
    """A pin, snooze or dismiss change the gateway refuses; the message is for the person."""


@dataclass(frozen=True)
class WebChatThreadMessage:
    """The latest ordinary message, as the row's second line shows it."""

    role: str
    text: str
    created_at: str | None


@dataclass(frozen=True)
class WebChatThread:
    """One row of the Web Chat thread list. ``title`` is ``None`` until named.

    ``parallel_thread`` is the thread's Run concurrently setting, which the
    switch below Send shows; ``has_open_request`` locks that switch while a
    request is open in the thread's conversation.
    """

    session_key: str
    session_alias: str | None
    conversation_id: str | None
    title: str | None
    updated_at: str
    kind: WebChatThreadKind
    parallel_thread: bool = False
    has_open_request: bool = False
    # Read only for the sessions route (the live read leaves it out).
    last_message: WebChatThreadMessage | None = None
    last_activity_at: str | None = None
    last_sequence: int = 0
    needs_you: NeedsYouReason | None = None
    pinned: bool = False
    list_state: ThreadListState | None = None
    # The snooze's wake time while the thread is snoozed.
    snoozed_until: str | None = None
    # The agent trail (module docstring): the stored trail, the task of the running request agents name, who
    # works on it now, and the name the page shows for each agent type in the trail.
    trail: ThreadTrail = EMPTY_TRAIL
    running_task_id: str | None = None
    working: tuple[WorkingAgent, ...] = ()
    agent_names: Mapping[str, str] = field(default_factory=dict)


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
    return _attachment_name(record.first_message_attachments_json, record.session_key, "first")


def _attachment_name(attachments_json: str | None, session_key: str, which: str) -> str | None:
    """The first attachment's file name, ``Attachment`` when unreadable, or ``None`` without one."""

    try:
        references = json.loads(attachments_json or "[]")
    except json.JSONDecodeError:
        LOGGER.warning(
            "A Web Chat thread's %s message has unreadable attachment references; it shows "
            "'Attachment'. event=web_chat_thread_attachments_unreadable session_key=%s",
            which,
            session_key,
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


def last_message(record: WebChatThreadRecord) -> WebChatThreadMessage | None:
    """The latest ordinary message as the store cut it, or its first file's name.

    The page makes the row's plain one-line snippet (``chat_thread_list.mjs``
    ``plainSnippet``: Markdown markers out, whitespace collapsed, shortened),
    for this text and for messages that arrive live alike, so one rule
    decides what the line shows.
    """

    if not record.last_message_role:
        return None
    text = record.last_message_text or ""
    if not text.strip():
        text = _attachment_name(record.last_message_attachments_json, record.session_key, "latest") or ""
    return WebChatThreadMessage(role=record.last_message_role, text=text, created_at=record.last_message_at)


def needs_you_reason(record: WebChatThreadRecord, *, now: datetime) -> NeedsYouReason | None:
    """What the thread waits on from the person at ``now``, or ``None``."""

    if record.has_open_question:
        return NeedsYouReason.QUESTION
    if record.running_request_status == REQUEST_STATUS_WAITING_FOR_STEERING:
        return NeedsYouReason.REPLY
    if record.has_waiting_setup:
        return NeedsYouReason.SETUP
    if _credential_form_open(record, now=now) or record.running_request_status == REQUEST_STATUS_WAITING_FOR_CREDENTIAL:
        return NeedsYouReason.CREDENTIAL
    return None


def _credential_form_open(record: WebChatThreadRecord, *, now: datetime) -> bool:
    """Whether a credential form in the thread still waits for the person.

    A form's row stays until its result reaches the task, which can be after
    the form was submitted or expired (the manager may be unreachable), so a
    form counts only until its expiry time. An expiry nobody can read counts
    as open, with a warning: the label stays as it was rather than hiding a
    form that may still wait.
    """

    for value in record.credential_form_expiries:
        expires = _stored_time(value)
        if expires is None:
            LOGGER.warning(
                "A Web Chat credential form has an unreadable expiry time; the thread is shown as waiting. "
                "event=web_chat_credential_form_expiry_unreadable session_key=%s expires_at_utc=%r",
                record.session_key,
                value,
            )
            return True
        if expires > now:
            return True
    return False


class ThreadPlacement(Protocol):
    """The stored placement values the woke rule reads.

    Both the thread list's ``WebChatThreadRecord`` and the placement read that
    clears a Woke mark (``ThreadPlacementRecord``) have them, so the list and
    ``clear_woke`` decide "woke" with this one function.
    """

    @property
    def session_key(self) -> str: ...

    @property
    def hidden_as(self) -> str | None: ...

    @property
    def hidden_until(self) -> str | None: ...

    @property
    def hidden_at_sequence(self) -> int | None: ...

    @property
    def last_sequence(self) -> int: ...

    @property
    def team_posted_after_hiding(self) -> bool: ...


def thread_list_state(record: ThreadPlacement, *, now: datetime) -> ThreadListState | None:
    """Whether a thread is snoozed, woke from a snooze, dismissed, or none of these, at ``now``."""

    if record.hidden_as is None:
        return None
    if record.hidden_as == ThreadHiddenAs.DISMISSED:
        # Anything posted after the last message the person had seen brings it back.
        returned = record.last_sequence > (record.hidden_at_sequence or 0)
        return None if returned else ThreadListState.DISMISSED
    if record.hidden_as == ThreadHiddenAs.SNOOZED:
        if record.team_posted_after_hiding:
            return ThreadListState.WOKE
        wake = _stored_time(record.hidden_until)
        if wake is None:
            # Never hide a thread on a time nobody can read: show it as woke.
            LOGGER.warning(
                "A snoozed Web Chat thread has an unreadable wake time; it is shown as woke. "
                "event=web_chat_thread_snooze_unreadable session_key=%s hidden_until=%r",
                record.session_key,
                record.hidden_until,
            )
            return ThreadListState.WOKE
        return ThreadListState.WOKE if now >= wake else ThreadListState.SNOOZED
    LOGGER.warning(
        "A Web Chat thread has an unknown placement; it is listed as usual. "
        "event=web_chat_thread_placement_unknown session_key=%s hidden_as=%r",
        record.session_key,
        record.hidden_as,
    )
    return None


def _stored_time(value: str | None) -> datetime | None:
    try:
        parsed = datetime.fromisoformat(str(value or ""))
    except ValueError:
        return None
    return parsed if parsed.tzinfo is not None else None


def snooze_wake_time(value: object, *, now: datetime) -> str:
    """The UTC wake time to store for a snooze the page asked for, or ``InvalidThreadPlacement``.

    The page works out "this evening" and the other choices in the person's
    own time and sends one moment with its offset; the gateway only checks
    that it is later than now and within ``SNOOZE_MAX_AHEAD``.
    """

    wake = _stored_time(value if isinstance(value, str) else None)
    if wake is None:
        raise InvalidThreadPlacement("Choose a date and time to snooze until.")
    if wake <= now:
        raise InvalidThreadPlacement("Choose a time in the future to snooze until.")
    if wake - now > SNOOZE_MAX_AHEAD:
        raise InvalidThreadPlacement("Snooze for at most a year.")
    return wake.astimezone(timezone.utc).isoformat()


def web_chat_thread(
    record: WebChatThreadRecord, *, proactive_session_key: str, now: datetime
) -> WebChatThread | None:
    """The thread-list row for one session at ``now``, or ``None`` when it is not listed."""

    kind = thread_kind(record.session_key, proactive_session_key=proactive_session_key)
    if kind is not WebChatThreadKind.CONVERSATION and not record.has_messages:
        return None
    alias = (record.session_alias or "").strip() or None
    title = alias or DELIVERY_THREAD_NAMES.get(kind) or first_message_title(record)
    list_state = thread_list_state(record, now=now)
    return WebChatThread(
        trail=thread_trail_of(record),
        running_task_id=record.running_task_id,
        session_key=record.session_key,
        session_alias=record.session_alias,
        conversation_id=record.conversation_id,
        title=title,
        updated_at=record.updated_at,
        kind=kind,
        parallel_thread=record.parallel_thread,
        has_open_request=record.has_open_request,
        last_message=last_message(record),
        last_activity_at=record.last_activity_at,
        last_sequence=record.last_sequence,
        needs_you=needs_you_reason(record, now=now),
        pinned=record.pinned,
        list_state=list_state,
        snoozed_until=record.hidden_until if list_state is ThreadListState.SNOOZED else None,
    )



# Unreadable stored trails already logged, as (session key, stored value): each is logged once, not at every
# two-second read (plan review C15); a new value (the next rebuild) is logged again if it is unreadable too.
_UNREADABLE_TRAILS_LOGGED: OrderedDict[tuple[str, str], None] = OrderedDict()
_UNREADABLE_TRAILS_LOGGED_MAX = 1024
_UNREADABLE_TRAILS_LOCK = threading.Lock()


def _first_time_unreadable(session_key: str, stored: str) -> bool:
    with _UNREADABLE_TRAILS_LOCK:
        key = (session_key, stored)
        if key in _UNREADABLE_TRAILS_LOGGED:
            return False
        _UNREADABLE_TRAILS_LOGGED[key] = None
        while len(_UNREADABLE_TRAILS_LOGGED) > _UNREADABLE_TRAILS_LOGGED_MAX:
            _UNREADABLE_TRAILS_LOGGED.popitem(last=False)
        return True


def thread_trail_of(record: WebChatThreadRecord) -> ThreadTrail:
    """The thread's stored agent trail; an unreadable one is logged once and the row shows none."""

    try:
        return decode_thread_trail(record.agent_trail_json)
    except AgentTrailUnreadable as error:
        if not _first_time_unreadable(record.session_key, record.agent_trail_json or ""):
            return EMPTY_TRAIL
        LOGGER.warning(
            "A Web Chat thread's agent trail is unreadable; its row shows none until the next change rebuilds it. "
            "event=web_chat_thread_trail_unreadable session_key=%s error=%s",
            record.session_key,
            error,
        )
        return EMPTY_TRAIL


def _agent_payload(agent_type: str, lead: bool, names: Mapping[str, str]) -> dict[str, object]:
    return {"agent_type": agent_type, "lead": lead, "name": names.get(agent_type) or agent_name(agent_type, lead, {})}


def trail_payload(thread: WebChatThread) -> dict[str, object]:
    """The row's trail as the page draws it: entries oldest first, whether older ones exist, and every agent
    type that worked in the thread (the hover card's names)."""

    trail, names = thread.trail, thread.agent_names
    return {
        "entries": [
            {"by": "person"} if entry.agent is None
            else {"by": "agent", **_agent_payload(entry.agent.agent_type, entry.agent.lead, names)}
            for entry in trail.entries
        ],
        "has_older": trail.has_older,
        "agents": [_agent_payload(agent.agent_type, agent.lead, names) for agent in trail.agents],
    }


def working_payload(working: tuple[WorkingAgent, ...]) -> list[dict[str, object]]:
    return [{"agent_type": agent.agent_type, "lead": agent.lead, "name": agent.name} for agent in working]


def thread_list_payload(thread: WebChatThread) -> dict[str, object]:
    """One row of the sessions route's ``sessions`` list, as the page reads it.

    ``title`` is the thread's fixed name (``None`` until named; the page shows
    "New Chat"). ``parallel_thread`` is its Run concurrently setting; whether
    a request is open, which locks that switch, rides on every event page. The
    rest draws the row: its latest ordinary message as stored (the page makes
    the plain snippet), when anything was last posted and that message's
    sequence (sent back with snooze and dismiss), what it waits on from the
    person, and where the person placed it. The aesthetic preview builds its
    sample rows through this too.
    """

    message = thread.last_message
    return {
        "session_key": thread.session_key,
        "session_alias": thread.session_alias,
        "conversation_id": thread.conversation_id,
        "title": thread.title,
        "updated_at": thread.updated_at,
        "parallel_thread": thread.parallel_thread,
        "last_message": (
            {"role": message.role, "text": message.text, "created_at": message.created_at}
            if message is not None
            else None
        ),
        "last_activity_at": thread.last_activity_at,
        "last_sequence": thread.last_sequence,
        "needs_you": thread.needs_you.value if thread.needs_you else None,
        "pinned": thread.pinned,
        "list_state": thread.list_state.value if thread.list_state else None,
        "snoozed_until": thread.snoozed_until,
        "trail": trail_payload(thread),
        "working": working_payload(thread.working),
    }

__all__ = [
    "ATTACHMENT_TITLE",
    "DELIVERY_THREAD_NAMES",
    "InvalidThreadPlacement",
    "NeedsYouReason",
    "SNOOZE_MAX_AHEAD",
    "ThreadListState",
    "TITLE_MAX_CHARACTERS",
    "ThreadPlacement",
    "WebChatThread",
    "WebChatThreadKind",
    "WebChatThreadMessage",
    "first_message_title",
    "last_message",
    "needs_you_reason",
    "snooze_wake_time",
    "thread_kind",
    "thread_list_payload",
    "thread_list_state",
    "web_chat_thread",
]
