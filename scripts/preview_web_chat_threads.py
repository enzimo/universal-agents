"""Sample Web Chat threads for the onboarding aesthetic preview's thread list.

``scripts/run_onboarding_aesthetic_preview.py`` serves Web Chat without a
gateway. This module keeps a small set of threads in every section of the
list (Needs you, Pinned, Recent with a woke thread, Snoozed, Dismissed) and
builds each row through the gateway's own code (``WebChatThreadRecord`` ->
``web_chat_thread`` -> ``thread_list_payload``, and the same revision digest),
so a change to the row format fails ``tests/test_onboarding_aesthetic_preview.py``
instead of leaving the preview page broken. Pin, snooze, dismiss, restore and
delete change the samples in memory, like the store would; nothing is saved.
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta

from assistant_gateway.application.web_chat_threads import (
    InvalidThreadPlacement,
    WebChatThread,
    snooze_wake_time,
    thread_list_payload,
    web_chat_thread,
)
from assistant_gateway.application.website_application import (
    WebChatThreadAction,
    web_chat_threads_revision,
)
from assistant_gateway.domain.request_state import REQUEST_STATUS_WAITING_FOR_CREDENTIAL
from assistant_gateway.domain.sessions import ThreadHiddenAs, WebChatThreadRecord

OWNER = "usr_dev_preview"
PROACTIVE_SESSION_KEY = "heartbeat"
# One turn: who wrote it, what, and how many minutes ago.
Turn = tuple[str, str, int]


@dataclass
class _Sample:
    record: WebChatThreadRecord
    turns: list[Turn] = field(default_factory=list)


def _record(session_key: str, title: str, **fields: object) -> WebChatThreadRecord:
    values: dict[str, object] = {
        "session_key": session_key,
        "session_alias": title,
        "conversation_id": "conversation_" + session_key.rsplit(":", 1)[-1],
        "owner_canonical_user_id": OWNER,
        "updated_at": "",
        "first_message_text": title,
        "first_message_attachments_json": "[]",
        "has_messages": True,
    }
    values.update(fields)
    return WebChatThreadRecord(**values)  # type: ignore[arg-type]


def _samples() -> list[_Sample]:
    offsite = [
        ("user", "Can you find three venues within two hours of the office for 20 people in March?", 1500),
        ("assistant", "Five venues fit; three are within budget: **Lakeside Lodge**, The Old Mill and Pine Hollow.", 1490),
        ("system", "Status: idle", 1485),
        ("user", "Yes, and keep the shortlist somewhere the team can see it.", 1480),
        ("assistant", "I saved the venue shortlist to `team/offsite.md`. Lakeside Lodge and The Old Mill have the 14th free.", 1470),
    ]
    return [
        _Sample(_record("website:web-chat:portainer", "Portainer stack health",
                        running_request_status=REQUEST_STATUS_WAITING_FOR_CREDENTIAL, has_waiting_credential_form=True),
                [("user", "Can you check which Portainer stacks are unhealthy?", 2),
                 ("assistant", "I need a Portainer API token to read your stacks. I've asked for one below.", 1)]),
        _Sample(_record("website:web-chat:github", "Finish the GitHub sign-in", has_waiting_setup=True),
                [("user", "Open a pull request for the docs fix.", 6),
                 ("assistant", "GitHub needs you to sign in first; the setup page is below.", 4)]),
        _Sample(_record("website:web-chat:weekly", "Weekly infrastructure report", pinned=True),
                [("assistant", "## Weekly report\n\n- All 14 hosts healthy\n- Disk on nas-2 at 81%", 2880)]),
        _Sample(_record("website:web-chat:vectors", "Compare vector databases"),
                [("user", "Which of them support hybrid search out of the box?", 0)]),
        _Sample(_record("website:web-chat:tls", "Renew the TLS certificate", hidden_as=ThreadHiddenAs.SNOOZED.value),
                [("user", "Remind me about the staging certificate.", 300),
                 ("assistant", "The staging certificate expires Friday.", 5)]),
        _Sample(_record("website:web-chat:onboarding", "Draft onboarding email for new hires"),
                [("assistant", "Here's a draft with the three links new hires ask for most.", 12)]),
        _Sample(_record("website:web-chat:backup", "Fix flaky backup job"),
                [("assistant", "The backup now retries twice before it reports a failure.", 190),
                 ("user", "Thanks, that did it.", 180)]),
        _Sample(_record("website:web-chat:offsite", "Plan the team offsite"), list(offsite)),
        _Sample(_record("website:web-chat:contracts", "Summarize the vendor contracts"),
                [("assistant", "Two contracts renew in January; both allow 60 days' notice.", 2900)]),
        _Sample(_record("website:web-chat:budget", "Quarterly budget review", hidden_as=ThreadHiddenAs.SNOOZED.value),
                [("assistant", "The draft budget is ready for your review.", 600)]),
        _Sample(_record("website:web-chat:newsletter", "Read the security newsletter", hidden_as=ThreadHiddenAs.SNOOZED.value),
                [("assistant", "Three advisories this week; none affect your hosts.", 900)]),
        _Sample(_record("website:web-chat:deploy", "Old deployment notes", hidden_as=ThreadHiddenAs.DISMISSED.value),
                [("assistant", "The notes are archived in team/deploy.md.", 5000)]),
        _Sample(_record("website:web-chat:printer", "Printer setup", hidden_as=ThreadHiddenAs.DISMISSED.value),
                [("user", "The printer works now, thanks.", 7000)]),
    ]


def _ordinary(role: str, content: str) -> bool:
    return role != "system" and not (role == "user" and content.lstrip(" \t\n\r").startswith("/")) and bool(content.strip())


class PreviewThreads:
    """The preview's thread list, kept in memory for one preview server."""

    def __init__(self, now: datetime) -> None:
        self._started = now
        self._samples = {sample.record.session_key: sample for sample in _samples()}
        for sample in self._samples.values():
            self._refresh(sample)
            if sample.record.hidden_as == ThreadHiddenAs.SNOOZED.value:
                # The TLS thread woke a minute ago; the others sleep until tomorrow.
                woke = sample.record.session_key.endswith(":tls")
                wake = now - timedelta(minutes=1) if woke else now + timedelta(days=1)
                sample.record = replace(sample.record, hidden_until=wake.isoformat(), hidden_at_sequence=0 if woke else sample.record.last_sequence)
            elif sample.record.hidden_as == ThreadHiddenAs.DISMISSED.value:
                sample.record = replace(sample.record, hidden_at_sequence=sample.record.last_sequence)

    def _when(self, minutes: int) -> str:
        return (self._started - timedelta(minutes=minutes)).isoformat()

    def _refresh(self, sample: _Sample) -> None:
        turns = sample.turns
        ordinary = [turn for turn in turns if _ordinary(turn[0], turn[1])]
        last = ordinary[-1] if ordinary else None
        sample.record = replace(
            sample.record,
            updated_at=self._when(turns[-1][2]) if turns else self._started.isoformat(),
            last_activity_at=self._when(turns[-1][2]) if turns else None,
            last_sequence=len(turns),
            last_message_role=last[0] if last else None,
            last_message_text=last[1][:400] if last else None,
            last_message_attachments_json="[]" if last else None,
            last_message_at=self._when(last[2]) if last else None,
        )

    def _threads(self, now: datetime) -> list[WebChatThread]:
        threads: list[WebChatThread] = []
        for sample in self._samples.values():
            thread = web_chat_thread(sample.record, proactive_session_key=PROACTIVE_SESSION_KEY, now=now)
            if thread is not None:
                threads.append(thread)
        return threads

    def rows(self, now: datetime) -> list[dict[str, object]]:
        return [thread_list_payload(thread) for thread in self._threads(now)]

    def revision(self, now: datetime) -> str:
        return web_chat_threads_revision(self._threads(now))

    def owns(self, session_key: str) -> bool:
        return session_key in self._samples

    def archive(self, session_key: str) -> bool:
        return self._samples.pop(session_key, None) is not None

    def place(self, session_key: str, payload: dict[str, object], now: datetime) -> str | None:
        """Apply a thread_action like the gateway; the refusal text on failure."""

        sample = self._samples.get(session_key)
        if sample is None:
            return "Thread not found."
        try:
            action = WebChatThreadAction(str(payload.get("thread_action") or ""))
        except ValueError:
            return "thread_action must be pin, unpin, snooze, dismiss or restore."
        record = sample.record
        seen = payload.get("seen_sequence")
        baseline = min(seen, record.last_sequence) if isinstance(seen, int) and not isinstance(seen, bool) else None
        try:
            if action is WebChatThreadAction.PIN:
                record = replace(record, pinned=True, hidden_as=None, hidden_until=None, hidden_at_sequence=None)
            elif action is WebChatThreadAction.UNPIN:
                record = replace(record, pinned=False)
            elif action is WebChatThreadAction.RESTORE:
                record = replace(record, hidden_as=None, hidden_until=None, hidden_at_sequence=None)
            elif baseline is None or baseline < 0:
                raise InvalidThreadPlacement("Reload the chat and try again.")
            elif action is WebChatThreadAction.SNOOZE:
                wake = snooze_wake_time(payload.get("snooze_until"), now=now)
                record = replace(record, hidden_as=ThreadHiddenAs.SNOOZED.value, hidden_until=wake,
                                 hidden_at_sequence=baseline, team_posted_after_hiding=False)
            else:
                record = replace(record, hidden_as=ThreadHiddenAs.DISMISSED.value, hidden_until=None,
                                 hidden_at_sequence=baseline)
        except InvalidThreadPlacement as refused:
            return str(refused)
        sample.record = record
        return None

    def history(self, session_key: str, *, limit: int, turn_count: bool) -> dict[str, object] | None:
        """A history page in the history route's shape, oldest first."""

        sample = self._samples.get(session_key)
        if sample is None:
            return None
        messages: list[dict[str, object]] = [
            {
                "message_id": f"{session_key}:{index}",
                "session_key": session_key,
                "conversation_id": sample.record.conversation_id,
                "session_sequence": index,
                "event_sequence": index,
                "role": role,
                "content": content,
                "message_type": "user_message" if role == "user" else "agent_response",
                "message_level": "normal",
                "attachments": [],
                "delivery_status": "presented",
                "created_at_utc": self._when(minutes),
            }
            for index, (role, content, minutes) in enumerate(sample.turns, start=1)
        ]
        page: dict[str, object] = {
            "status": "ok",
            "messages": messages[-max(1, limit):],
            "before_cursor": None,
            "has_more": len(messages) > limit,
        }
        if turn_count:
            page["turn_count"] = sum(1 for role, content, _ in sample.turns if _ordinary(role, content))
        return page

