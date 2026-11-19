"""Which Web Chat threads the sidebar lists, and their fixed names (web_chat_threads).

Owner's report, 2026-10-09: a newly set-up Web Chat listed two empty "New
Chat" rows (the gateway's proactive and memory-review delivery threads), and
a thread's name changed with every message. These run the real store query
and the session application's listing.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

import pytest

from assistant_gateway.application.person_maintenance import person_maintenance_session_key
from assistant_gateway.application.proactive_delivery import proactive_session_key
from assistant_gateway.application.web_chat_threads import (
    TITLE_MAX_CHARACTERS,
    InvalidThreadPlacement,
    NeedsYouReason,
    ThreadListState,
    WebChatThreadKind,
    last_message,
    needs_you_reason,
    snooze_wake_time,
    thread_kind,
    thread_list_state,
)
from assistant_gateway.application.website_application import (
    WebChatThreadAction,
    WebChatThreadPlacementResult,
    web_chat_threads_revision,
)
from assistant_gateway.application.website_services import WebsiteSessionApplication
from assistant_gateway.domain.request_state import (
    REQUEST_STATUS_WAITING_FOR_CREDENTIAL,
    REQUEST_STATUS_WAITING_FOR_STEERING,
)
from assistant_gateway.domain.sessions import WebChatThreadRecord
from assistant_gateway.domain.transcript import TranscriptAppendRequest, TranscriptAttachmentReference
from assistant_gateway.runtime_ports import OwnershipPrivateResourceAuthorizer
from assistant_gateway.store import GatewayStateStore

OWNER = "user_alpha"
OTHER_OWNER = "user_beta"
HEARTBEAT = "heartbeat"
THREAD = "website:web-chat:alpha"
NOW = datetime(2026, 11, 19, 15, 0, tzinfo=timezone.utc)
LATER = (NOW + timedelta(hours=1)).isoformat()


class _Clock:
    """A clock the test moves."""

    def __init__(self) -> None:
        self.now = NOW

    def now_utc(self) -> datetime:
        return self.now


class _Threads:
    """The store and the session application's thread listing over it."""

    def __init__(self, tmp_path: Path) -> None:
        self.store = GatewayStateStore(str(tmp_path / "gateway.db"))
        self._messages = 0
        self.clock = _Clock()
        unused = cast(Any, SimpleNamespace())
        self.application = WebsiteSessionApplication(
            sessions=unused,
            session_repository=self.store.session_repository,
            delivery_repository=unused,
            request_context=unused,
            private_authorizer=OwnershipPrivateResourceAuthorizer(),
            manager_agent_id="manager_1",
            team_id="team_1",
            awaiting_reply_session_keys=frozenset,
            proactive_session_key=HEARTBEAT,
            clock=self.clock,
        )

    def session(self, session_key: str, *, owner: str = OWNER, channel: str = "website", chat: str = "web-chat") -> None:
        self.store.session_repository.create_session(
            session_key=session_key,
            conversation_id=f"conversation_{session_key}",
            manager_agent_id="manager_1",
            channel_name=channel,
            user_id=owner,
            chat_id=chat,
            owner_canonical_user_id=owner,
        )

    def message(
        self,
        session_key: str,
        content: str,
        *,
        role: str = "user",
        attachments: tuple[TranscriptAttachmentReference, ...] = (),
    ) -> None:
        self._messages += 1
        self.store.transcript_repository.append_transcript_message(
            TranscriptAppendRequest(
                message_id=f"message_{self._messages}",
                session_key=session_key,
                conversation_id=f"conversation_{session_key}",
                owner_canonical_user_id=OWNER,
                channel_name="website",
                chat_id="web-chat",
                role=role,
                content=content,
                message_type="user_message" if role == "user" else "agent_response",
                message_level="normal",
                correlation_id=None,
                source_kind="client_request" if role == "user" else "delivery",
                source_id=f"source_{self._messages}",
                idempotency_key=f"idempotency_{self._messages}",
                attachment_references=attachments,
            )
        )

    def listed(self, owner: str = OWNER) -> dict[str, str | None]:
        return {thread.session_key: thread.title for thread in self.application.list_web_chat_threads(owner_canonical_user_id=owner)}

    def revision(self) -> str:
        return web_chat_threads_revision(self.application.list_web_chat_threads(owner_canonical_user_id=OWNER))

    def thread(self, session_key: str = THREAD):
        (listed,) = [
            thread
            for thread in self.application.list_web_chat_threads(owner_canonical_user_id=OWNER, include_last_message=True)
            if thread.session_key == session_key
        ]
        return listed

    def place(self, action: WebChatThreadAction, *, owner: str = OWNER, **options: Any) -> WebChatThreadPlacementResult:
        return self.application.set_web_chat_thread_placement(
            session_key=THREAD, action=action, owner_canonical_user_id=owner, **options
        )


def test_delivery_threads_stay_hidden_until_something_arrives(tmp_path: Path) -> None:
    threads = _Threads(tmp_path)
    maintenance = person_maintenance_session_key(OWNER)
    # What the gateway makes at every start for the first person.
    threads.session(HEARTBEAT)
    threads.session(maintenance)
    assert threads.listed() == {}, "a newly set-up Web Chat lists no empty delivery threads"

    threads.session(THREAD)
    assert threads.listed() == {THREAD: None}, "a thread the person started is listed, unnamed until its first message"

    threads.message(HEARTBEAT, "Nothing urgent today.", role="assistant")
    threads.message(maintenance, "I tidied two memories.", role="assistant")
    assert threads.listed() == {THREAD: None, HEARTBEAT: "Updates", maintenance: "Memory review"}


def test_a_delivery_thread_keeps_its_name_even_when_the_person_typed_in_it(tmp_path: Path) -> None:
    threads = _Threads(tmp_path)
    maintenance = person_maintenance_session_key(OWNER)
    threads.session(maintenance)
    threads.message(maintenance, "Hello Rachel")
    assert threads.listed() == {maintenance: "Memory review"}
    threads.store.session_repository.set_session_alias(maintenance, "Old chat")
    assert threads.listed() == {maintenance: "Old chat"}, "a rename still wins"


def test_the_first_ordinary_message_names_a_thread_and_later_ones_never_rename_it(tmp_path: Path) -> None:
    threads = _Threads(tmp_path)
    threads.session(THREAD)
    threads.message(THREAD, "/status")
    assert threads.listed() == {THREAD: None}, "a slash command does not name a thread"

    threads.message(THREAD, "  Plan my   week ")
    named = threads.revision()
    assert threads.listed() == {THREAD: "Plan my week"}

    threads.message(THREAD, "Here is your week.", role="assistant")
    threads.message(THREAD, "Add a dentist visit on Friday")
    threads.message(THREAD, "Hello Rachel")
    assert threads.listed() == {THREAD: "Plan my week"}
    assert threads.revision() == named, "later messages do not make open pages reload the list"

    threads.store.session_repository.set_session_alias(THREAD, "Weekly plan")
    assert threads.listed() == {THREAD: "Weekly plan"}
    assert threads.revision() != named, "a rename does"


def test_an_attachment_only_first_message_is_named_by_its_file(tmp_path: Path) -> None:
    threads = _Threads(tmp_path)
    threads.session(THREAD)
    threads.message(
        THREAD,
        "",
        attachments=(
            TranscriptAttachmentReference(
                attachment_id="attachment_1", filename="budget.xlsx", content_type="application/vnd.ms-excel", kind="file"
            ),
        ),
    )
    threads.message(THREAD, "What does this say?")
    assert threads.listed() == {THREAD: "budget.xlsx"}


def test_a_long_first_message_is_shortened(tmp_path: Path) -> None:
    threads = _Threads(tmp_path)
    threads.session(THREAD)
    threads.message(THREAD, "word " * 200)
    title = threads.listed()[THREAD]
    assert title is not None and len(title) == TITLE_MAX_CHARACTERS and title.endswith("…")


def test_other_channels_and_other_people_never_crowd_out_or_join_the_list(tmp_path: Path) -> None:
    threads = _Threads(tmp_path)
    threads.session(THREAD)
    # Many newer threads elsewhere: the list used to read 50 threads of any
    # channel before keeping the Web Chat ones.
    for index in range(60):
        threads.session(f"telegram:chat:{index}", channel="telegram", chat=f"chat_{index}")
    threads.session("website:web-chat:beta", owner=OTHER_OWNER)
    assert threads.listed() == {THREAD: None}
    assert threads.listed(OTHER_OWNER) == {"website:web-chat:beta": None}


def test_delivery_threads_are_recognized_by_the_keys_their_own_code_makes() -> None:
    assert thread_kind(HEARTBEAT, proactive_session_key=HEARTBEAT) is WebChatThreadKind.UPDATES
    other_owner = proactive_session_key(HEARTBEAT, owner=OTHER_OWNER, default_owner=OWNER)
    assert thread_kind(other_owner, proactive_session_key=HEARTBEAT) is WebChatThreadKind.UPDATES
    assert thread_kind(person_maintenance_session_key(OWNER), proactive_session_key=HEARTBEAT) is WebChatThreadKind.MEMORY_REVIEW
    assert thread_kind("heartbeats_and_more", proactive_session_key=HEARTBEAT) is WebChatThreadKind.CONVERSATION
    assert thread_kind(THREAD, proactive_session_key=HEARTBEAT) is WebChatThreadKind.CONVERSATION


# Where a thread sits: derived from the stored baseline at read time.


def _record(**fields: Any) -> WebChatThreadRecord:
    values: dict[str, Any] = {
        "session_key": THREAD,
        "session_alias": None,
        "conversation_id": "conversation",
        "owner_canonical_user_id": OWNER,
        "updated_at": NOW.isoformat(),
        "first_message_text": "Plan my week",
        "first_message_attachments_json": "[]",
        "has_messages": True,
    }
    values.update(fields)
    return WebChatThreadRecord(**values)


def test_a_snooze_wakes_at_its_time() -> None:
    snoozed = _record(hidden_as="snoozed", hidden_until=LATER, hidden_at_sequence=3, last_sequence=3)
    assert thread_list_state(snoozed, now=NOW) is ThreadListState.SNOOZED
    assert thread_list_state(snoozed, now=NOW + timedelta(hours=1)) is ThreadListState.WOKE


def test_a_snooze_wakes_early_when_the_team_posts_but_not_for_the_persons_own_post() -> None:
    person_posted = _record(hidden_as="snoozed", hidden_until=LATER, hidden_at_sequence=3, last_sequence=4)
    assert thread_list_state(person_posted, now=NOW) is ThreadListState.SNOOZED
    team_posted = _record(
        hidden_as="snoozed", hidden_until=LATER, hidden_at_sequence=3, last_sequence=5, team_posted_after_hiding=True
    )
    assert thread_list_state(team_posted, now=NOW) is ThreadListState.WOKE


def test_a_dismissed_thread_returns_when_anything_new_is_posted() -> None:
    dismissed = _record(hidden_as="dismissed", hidden_at_sequence=3, last_sequence=3)
    assert thread_list_state(dismissed, now=NOW) is ThreadListState.DISMISSED
    assert thread_list_state(_record(hidden_as="dismissed", hidden_at_sequence=3, last_sequence=4), now=NOW) is None


def test_an_unreadable_wake_time_shows_the_thread_and_says_why(caplog: pytest.LogCaptureFixture) -> None:
    for stored in ("tomorrow", "2026-11-20T09:00:00", None):
        record = _record(hidden_as="snoozed", hidden_until=stored, hidden_at_sequence=0)
        with caplog.at_level(logging.WARNING):
            assert thread_list_state(record, now=NOW) is ThreadListState.WOKE
    assert caplog.text.count("event=web_chat_thread_snooze_unreadable") == 3


def test_what_a_thread_waits_on_is_checked_in_order() -> None:
    assert needs_you_reason(_record()) is None
    everything = _record(
        has_open_question=True,
        running_request_status=REQUEST_STATUS_WAITING_FOR_STEERING,
        has_waiting_setup=True,
        has_waiting_credential_form=True,
    )
    assert needs_you_reason(everything) is NeedsYouReason.QUESTION
    assert needs_you_reason(_record(running_request_status=REQUEST_STATUS_WAITING_FOR_STEERING)) is NeedsYouReason.REPLY
    own_task_setup = _record(running_request_status=REQUEST_STATUS_WAITING_FOR_CREDENTIAL, has_waiting_setup=True)
    assert needs_you_reason(own_task_setup) is NeedsYouReason.SETUP, "a setup page is labelled as one (C05)"
    assert needs_you_reason(_record(running_request_status=REQUEST_STATUS_WAITING_FOR_CREDENTIAL)) is NeedsYouReason.CREDENTIAL
    assert needs_you_reason(_record(has_waiting_credential_form=True)) is NeedsYouReason.CREDENTIAL
    assert needs_you_reason(_record(running_request_status="running")) is None


def test_the_latest_message_is_sent_as_stored_or_named_by_its_file() -> None:
    """The page makes the plain one-line snippet (chat_thread_list.mjs plainSnippet, C15)."""

    assert last_message(_record()) is None
    message = last_message(_record(last_message_role="assistant", last_message_text="**All**\n\n- done", last_message_at=LATER))
    assert message is not None and (message.role, message.text, message.created_at) == ("assistant", "**All**\n\n- done", LATER)
    files = last_message(
        _record(last_message_role="user", last_message_text=" ", last_message_attachments_json='[{"filename": "budget.xlsx"}]')
    )
    assert files is not None and files.text == "budget.xlsx"


def test_a_snooze_time_must_be_a_future_moment_within_a_year() -> None:
    assert snooze_wake_time("2026-11-19T18:00:00+01:00", now=NOW) == "2026-11-19T17:00:00+00:00"
    for refused in ("2026-11-19T18:00:00", "tonight", None, 7, NOW.isoformat(), (NOW + timedelta(days=400)).isoformat()):
        with pytest.raises(InvalidThreadPlacement):
            snooze_wake_time(refused, now=NOW)


# Placement through the session application


def test_pin_snooze_dismiss_and_restore_change_the_list_and_its_revision(tmp_path: Path) -> None:
    threads = _Threads(tmp_path)
    threads.session(THREAD)
    threads.message(THREAD, "Plan my week")
    revisions = [threads.revision()]

    assert threads.place(WebChatThreadAction.PIN) is WebChatThreadPlacementResult.SAVED
    assert threads.thread().pinned is True
    revisions.append(threads.revision())

    assert threads.place(WebChatThreadAction.SNOOZE, snooze_until=LATER, seen_sequence=1) is WebChatThreadPlacementResult.SAVED
    snoozed = threads.thread()
    assert (snoozed.list_state, snoozed.snoozed_until, snoozed.pinned) == (ThreadListState.SNOOZED, LATER, True)
    revisions.append(threads.revision())

    # Moving the snooze to another time is shown on other open pages too (C09).
    tomorrow = (NOW + timedelta(days=1)).isoformat()
    threads.place(WebChatThreadAction.SNOOZE, snooze_until=tomorrow, seen_sequence=1)
    revisions.append(threads.revision())

    threads.clock.now = NOW + timedelta(days=2)
    assert threads.thread().list_state is ThreadListState.WOKE
    revisions.append(threads.revision())

    assert threads.place(WebChatThreadAction.RESTORE) is WebChatThreadPlacementResult.SAVED
    assert threads.thread().list_state is None
    revisions.append(threads.revision())

    threads.place(WebChatThreadAction.DISMISS, seen_sequence=1)
    assert threads.thread().list_state is ThreadListState.DISMISSED
    revisions.append(threads.revision())
    threads.message(THREAD, "One more thing", role="assistant")
    assert threads.thread().list_state is None, "a new message brings a dismissed thread back"
    revisions.append(threads.revision())
    changed = [before != after for before, after in zip(revisions, revisions[1:])]
    assert all(changed), "every change of place reaches other open pages"


def test_new_messages_change_the_row_but_not_the_revision(tmp_path: Path) -> None:
    threads = _Threads(tmp_path)
    threads.session(THREAD)
    threads.message(THREAD, "Plan my week")
    before = threads.revision()
    threads.message(THREAD, "Here is your week.", role="assistant")
    thread = threads.thread()
    assert thread.last_message is not None and thread.last_message.text == "Here is your week."
    assert thread.last_sequence == 2 and thread.last_activity_at is not None
    assert threads.revision() == before, "pages update the line from transcript events, not by reloading the list"


def test_a_question_changes_the_revision(tmp_path: Path) -> None:
    threads = _Threads(tmp_path)
    threads.session(THREAD)
    before = threads.revision()
    threads.store.request_repository.add_pending_query(
        session_key=THREAD,
        correlation_id="request_1",
        task_query_id="query_1",
        parent_task_id="task_1",
        conversation_id=f"conversation_{THREAD}",
        query_text="Which folder?",
        owner_canonical_user_id=OWNER,
    )
    assert threads.thread().needs_you is NeedsYouReason.QUESTION
    assert threads.revision() != before


def test_one_person_cannot_place_another_persons_thread(tmp_path: Path) -> None:
    threads = _Threads(tmp_path)
    threads.session(THREAD)
    for action, options in (
        (WebChatThreadAction.PIN, {}),
        (WebChatThreadAction.SNOOZE, {"snooze_until": LATER, "seen_sequence": 0}),
        (WebChatThreadAction.DISMISS, {"seen_sequence": 0}),
        (WebChatThreadAction.RESTORE, {}),
    ):
        assert threads.place(action, owner=OTHER_OWNER, **options) is WebChatThreadPlacementResult.NOT_FOUND
    thread = threads.thread()
    assert (thread.pinned, thread.list_state) == (False, None)


def test_threads_on_other_chats_and_archived_threads_cannot_be_placed(tmp_path: Path) -> None:
    threads = _Threads(tmp_path)
    threads.session(THREAD, channel="telegram", chat="tg-chat")
    assert threads.place(WebChatThreadAction.PIN) is WebChatThreadPlacementResult.NOT_FOUND
    other = _Threads(tmp_path / "archived")
    other.session(THREAD)
    other.store.session_repository.set_session_status(THREAD, "archived")
    assert other.place(WebChatThreadAction.PIN) is WebChatThreadPlacementResult.NOT_FOUND


def test_hiding_needs_the_last_message_the_page_showed_and_a_valid_time(tmp_path: Path) -> None:
    threads = _Threads(tmp_path)
    threads.session(THREAD)
    with pytest.raises(InvalidThreadPlacement):
        threads.place(WebChatThreadAction.DISMISS)
    with pytest.raises(InvalidThreadPlacement):
        threads.place(WebChatThreadAction.SNOOZE, snooze_until=NOW.isoformat(), seen_sequence=0)
    assert threads.thread().list_state is None


def test_a_sequence_the_store_cannot_keep_is_refused_and_every_refusal_is_logged(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    threads = _Threads(tmp_path)
    threads.session(THREAD)
    with caplog.at_level(logging.INFO):
        for refused in (2**63, -1, True):
            with pytest.raises(InvalidThreadPlacement):
                threads.place(WebChatThreadAction.DISMISS, seen_sequence=refused)
    assert threads.thread().list_state is None
    refusals = [line for line in caplog.text.splitlines() if "event=web_chat_thread_placement outcome=refused" in line]
    assert len(refusals) == 3 and all(THREAD in line and "action=dismiss" in line for line in refusals)
