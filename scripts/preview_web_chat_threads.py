"""Sample Web Chat threads for the onboarding aesthetic preview's thread list.

``scripts/run_onboarding_aesthetic_preview.py`` serves Web Chat without a
gateway. This module keeps a small set of threads in every section of the
list (Needs you, Pinned, Recent with a woke thread, Snoozed, Dismissed) and
builds each row through the gateway's own code (``WebChatThreadRecord`` ->
``web_chat_thread`` -> ``thread_list_payload``, and the same revision digest),
so a change to the row format fails ``tests/test_onboarding_aesthetic_preview.py``
instead of leaving the preview page broken. Pin, snooze, dismiss, restore,
clearing a Woke mark and delete change the samples in memory, like the store
would; nothing is saved.

Each sample also has the agents that worked on its requests, built into the
row's agent trail by the gateway's own rule (``assistant_gateway.domain.
agent_trail``), and some have agents working now or a busy mark, which the
preview's live updates carry like the gateway's (``live_states``): a row with
two agents working, a busy row nobody has reported on yet, a row with twenty
turns, a Needs you row, and specialists whose hashed colours are blue, violet,
yellow and lime, the hardest for contrast.
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta

from assistant_gateway.application.request_agents_service import WorkingAgent, agent_name
from assistant_gateway.application.web_chat_threads import (
    InvalidThreadPlacement,
    ThreadListState,
    WebChatThread,
    snooze_wake_time,
    thread_list_payload,
    thread_list_state,
    web_chat_thread,
)
from assistant_gateway.application.website_application import (
    WEB_CHAT_THREAD_ACTION_REFUSAL,
    WebChatThreadAction,
    web_chat_threads_revision,
)
from assistant_gateway.domain.agent_trail import TRAIL_REQUESTS_READ, TrailAgent, encode_thread_trail, thread_trail
from assistant_gateway.domain.request_state import REQUEST_STATUS_WAITING_FOR_CREDENTIAL
from assistant_gateway.domain.sessions import ThreadHiddenAs, WebChatThreadRecord

OWNER = "usr_dev_preview"
PROACTIVE_SESSION_KEY = "heartbeat"
# One turn: who wrote it, what, and how many minutes ago.
Turn = tuple[str, str, int]
# The agent catalog's display names the preview shows (the gateway reads its own catalog).
AGENT_NAMES = {
    "WebResearchAgent": "Web Research Agent",
    "MemReviewAgent": "Memory Review Agent",
    "InternalImprovementAgent": "Internal Improvement Agent",
    "LedgerAgent": "Ledger Keeper",
    "SecurityAgent": "Security Reviewer",
    "DocsWriterAgent": "Docs Writer",
    "NetworkAgent": "Network Checker",
}
LEAD = TrailAgent("TeamArchitect", lead=True)


def _agents(*types: str) -> tuple[TrailAgent, ...]:
    """One request's agents: the team lead first (the gateway records it at hand-over), then these types."""

    return (LEAD, *(TrailAgent(agent_type) for agent_type in types))


@dataclass
class _Sample:
    record: WebChatThreadRecord
    turns: list[Turn] = field(default_factory=list)
    # The agents of each request, oldest request first; who works on the running one now; whether it is busy.
    requests: list[tuple[TrailAgent, ...]] = field(default_factory=list)
    working: tuple[str, ...] = ()
    busy: bool = False


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


def _samples(now: datetime) -> list[_Sample]:
    # The Portainer form is still open for a quarter of an hour.
    form_open_until = (now + timedelta(minutes=15)).isoformat()
    offsite = [
        ("user", "Can you find three venues within two hours of the office for 20 people in March?", 1500),
        ("assistant", "Five venues fit; three are within budget: **Lakeside Lodge**, The Old Mill and Pine Hollow.", 1490),
        ("system", "Status: idle", 1485),
        ("user", "Yes, and keep the shortlist somewhere the team can see it.", 1480),
        ("assistant", "I saved the venue shortlist to `team/offsite.md`. Lakeside Lodge and The Old Mill have the 14th free.", 1470),
    ]
    return [
        _Sample(_record("website:web-chat:portainer", "Portainer stack health",
                        running_request_status=REQUEST_STATUS_WAITING_FOR_CREDENTIAL,
                        credential_form_expiries=(form_open_until,)),
                [("user", "Can you check which Portainer stacks are unhealthy?", 2),
                 ("assistant", "I need a Portainer API token to read your stacks. I've asked for one below.", 1)],
                requests=[_agents("NetworkAgent")]),
        _Sample(_record("website:web-chat:github", "Finish the GitHub sign-in", has_waiting_setup=True),
                [("user", "Open a pull request for the docs fix.", 6),
                 ("assistant", "GitHub needs you to sign in first; the setup page is below.", 4)]),
        _Sample(_record("website:web-chat:weekly", "Weekly infrastructure report", pinned=True),
                [("assistant", "## Weekly report\n\n- All 14 hosts healthy\n- Disk on nas-2 at 81%", 2880)],
                requests=[_agents("SecurityAgent"), _agents("SecurityAgent", "LedgerAgent")]),
        _Sample(_record("website:web-chat:vectors", "Compare vector databases"),
                [("user", "Which of them support hybrid search out of the box?", 0)],
                requests=[_agents("WebResearchAgent"), _agents("WebResearchAgent", "InternalImprovementAgent")],
                working=("WebResearchAgent", "InternalImprovementAgent"), busy=True),
        _Sample(_record("website:web-chat:tls", "Renew the TLS certificate", hidden_as=ThreadHiddenAs.SNOOZED.value),
                [("user", "Remind me about the staging certificate.", 300),
                 ("assistant", "The staging certificate expires Friday.", 5)]),
        _Sample(_record("website:web-chat:onboarding", "Draft onboarding email for new hires"),
                [("assistant", "Here's a draft with the three links new hires ask for most.", 12)],
                requests=[_agents("DocsWriterAgent")]),
        _Sample(_record("website:web-chat:backup", "Fix flaky backup job"),
                [("assistant", "The backup now retries twice before it reports a failure.", 190),
                 ("user", "Thanks, that did it.", 180)],
                requests=[_agents("InternalImprovementAgent"), _agents()]),
        # Twenty turns: the row shows six, the leftmost hollow.
        _Sample(_record("website:web-chat:offsite", "Plan the team offsite"), list(offsite),
                requests=[_agents()] * 6 + [_agents("WebResearchAgent"), _agents("MemReviewAgent", "LedgerAgent")]),
        _Sample(_record("website:web-chat:contracts", "Summarize the vendor contracts"),
                [("assistant", "Two contracts renew in January; both allow 60 days' notice.", 2900)],
                # Busy, and nobody has reported yet: the team lead's triangle pulses.
                requests=[_agents("LedgerAgent"), _agents()], busy=True),
        _Sample(_record("website:web-chat:budget", "Quarterly budget review", hidden_as=ThreadHiddenAs.SNOOZED.value),
                [("assistant", "The draft budget is ready for your review.", 600)]),
        _Sample(_record("website:web-chat:newsletter", "Read the security newsletter", hidden_as=ThreadHiddenAs.SNOOZED.value),
                [("assistant", "Three advisories this week; none affect your hosts.", 900)]),
        _Sample(_record("website:web-chat:deploy", "Old deployment notes", hidden_as=ThreadHiddenAs.DISMISSED.value),
                [("assistant", "The notes are archived in team/deploy.md.", 5000)]),
        _Sample(_record("website:web-chat:printer", "Printer setup", hidden_as=ThreadHiddenAs.DISMISSED.value),
                [("user", "The printer works now, thanks.", 7000)]),
    ]


def _with_agents(thread: WebChatThread, sample: _Sample) -> WebChatThread:
    """The row with who works now and its trail's names, as the gateway's ``_with_agents`` adds them."""

    named = {(agent.agent_type, agent.lead) for agents in sample.requests for agent in agents}
    names = {agent_type: agent_name(agent_type, lead, AGENT_NAMES) for agent_type, lead in named}
    working = tuple(
        WorkingAgent(agent_type, agent_name(agent_type, False, AGENT_NAMES), False) for agent_type in sample.working
    )
    return replace(thread, working=working, agent_names=names)


def _trail_json(requests: list[tuple[TrailAgent, ...]]) -> str | None:
    """The thread's stored trail, built like the store builds it (``storage.gateway_agent_trails``)."""

    if not requests:
        return None
    return encode_thread_trail(thread_trail(list(reversed(requests[-TRAIL_REQUESTS_READ:])), requests))


def _ordinary(role: str, content: str) -> bool:
    return role != "system" and not (role == "user" and content.lstrip(" \t\n\r").startswith("/")) and bool(content.strip())


class PreviewThreads:
    """The preview's thread list, kept in memory for one preview server."""

    def __init__(self, now: datetime) -> None:
        self._started = now
        self._samples = {sample.record.session_key: sample for sample in _samples(now)}
        for sample in self._samples.values():
            sample.record = replace(sample.record, agent_trail_json=_trail_json(sample.requests))
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
                threads.append(_with_agents(thread, sample))
        return threads

    def live_states(self, now: datetime) -> dict[str, object]:
        """The live read's thread states as the gateway sends them (``WebChatThreadStatesV1``)."""

        threads = self._threads(now)
        busy = [sample.record.session_key for sample in self._samples.values() if sample.busy]
        return {
            "threads_revision": web_chat_threads_revision(threads),
            "busy_session_keys": busy,
            "open_request_session_keys": busy,
            "working": [
                {"session_key": thread.session_key, "agents": [
                    {"agent_type": agent.agent_type, "lead": agent.lead, "name": agent.name} for agent in thread.working
                ]}
                for thread in threads if thread.working
            ],
        }

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
            return WEB_CHAT_THREAD_ACTION_REFUSAL
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
            elif action is WebChatThreadAction.CLEAR_WOKE:
                # Like the gateway: only a thread that is woke by the list's own rule.
                if thread_list_state(record, now=now) is ThreadListState.WOKE:
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

