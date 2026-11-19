"""The store's side of Web Chat's thread list: placement and what each row shows.

Pin, snooze and dismiss are columns on ``gateway_sessions``; whether a snoozed
thread woke or a dismissed one came back is derived when the list is read
(``assistant_gateway.application.web_chat_threads``), from the baselines saved
here. The list query also brings each thread's latest ordinary message, its
last activity and what it waits on from the person. Everything runs on SQLite
and, with ``LIVE_TEST_POSTGRES_DSN``, on real PostgreSQL.
"""

from __future__ import annotations

import logging
from collections.abc import Iterator
from itertools import count
from pathlib import Path

import pytest

from assistant_gateway.domain.request_state import (
    RUN_STATE_RUNNING,
    REQUEST_STATUS_WAITING_FOR_STEERING,
    TOOL_SETUP_STATUS_WAITING,
    CredentialCollectionRecord,
    ToolSetupRecord,
)
from assistant_gateway.domain.sessions import ThreadHiddenAs
from assistant_gateway.domain.transcript import TranscriptAppendRequest, TranscriptAttachmentReference
from storage.config import StorageSettings, resolve_storage_settings
from storage.gateway_state_repository import LAST_MESSAGE_SQL_CHARACTERS, create_gateway_state_repository
from tests.test_gateway_question_store import _postgres_settings

ALICE = "alice"
BOB = "bob"
THREAD = "website:web-chat:alpha"
WAITING_FOR_CREDENTIAL = "waiting_for_credential"


@pytest.fixture(
    params=["sqlite", pytest.param("postgres", marks=[pytest.mark.live, pytest.mark.postgres])]
)
def settings(request, tmp_path: Path) -> Iterator[StorageSettings]:
    if request.param == "postgres":
        with _postgres_settings() as postgres:
            yield postgres
        return
    yield resolve_storage_settings(
        {"backend": "sqlite", "sqlite": {"path": str(tmp_path / "gateway_state.db")}}
    )


class _Store:
    """A gateway store with helpers that write what Web Chat writes."""

    def __init__(self, settings: StorageSettings) -> None:
        self.repo = create_gateway_state_repository(settings, logger=logging.getLogger("test"))
        self._ids = count(1)

    def thread(self, session_key: str = THREAD, *, owner: str = ALICE) -> None:
        self.repo.create_session(
            session_key=session_key,
            conversation_id=f"conv_{session_key}",
            manager_agent_id="manager_1",
            channel_name="website",
            user_id=owner,
            chat_id="web-chat",
            owner_canonical_user_id=owner,
        )

    def post(
        self,
        content: str,
        *,
        role: str = "user",
        session_key: str = THREAD,
        owner: str = ALICE,
        attachments: tuple[TranscriptAttachmentReference, ...] = (),
    ) -> int:
        number = next(self._ids)
        message = self.repo.append_transcript_message(
            TranscriptAppendRequest(
                message_id=f"message_{number}",
                session_key=session_key,
                conversation_id=f"conv_{session_key}",
                owner_canonical_user_id=owner,
                channel_name="website",
                chat_id="web-chat",
                role=role,
                content=content,
                message_type="user_message" if role == "user" else "assistant_message",
                message_level="normal",
                correlation_id=None,
                source_kind="test",
                source_id=f"source_{number}",
                idempotency_key=f"idempotency_{number}",
                attachment_references=attachments,
            )
        )
        return message.session_sequence

    def running_request(self, status: str, *, session_key: str = THREAD) -> str:
        correlation_id = f"request_{next(self._ids)}"
        self.repo.record_request(
            correlation_id=correlation_id,
            session_key=session_key,
            request_kind="interactive",
            request_text="Prepare the report",
            conversation_id=f"conv_{session_key}",
            followup_to_task_id=None,
            status=status,
            owner_canonical_user_id=ALICE,
            active_task_id="task_1",
            run_state=RUN_STATE_RUNNING,
        )
        return correlation_id

    def listed(self, *, owner: str = ALICE, include_last_message: bool = True, limit: int = 100):
        return {
            record.session_key: record
            for record in self.repo.list_web_chat_threads(
                owner_canonical_user_id=owner,
                channel_name="website",
                chat_id="web-chat",
                limit=limit,
                include_last_message=include_last_message,
            )
        }

    def one(self, **options):
        return self.listed(**options)[THREAD]


def _attachment(filename: str) -> TranscriptAttachmentReference:
    return TranscriptAttachmentReference(
        attachment_id="attachment_1", filename=filename, content_type="text/plain", kind="file", size_bytes=4
    )


# Placement


def test_pin_and_unpin_leave_the_thread_activity_time_alone(settings) -> None:
    store = _Store(settings)
    store.thread()
    before = store.repo.get_session(THREAD).updated_at
    assert store.repo.set_session_pinned(THREAD, pinned=True)
    assert store.one().pinned is True
    assert store.repo.set_session_pinned(THREAD, pinned=False)
    assert store.one().pinned is False
    assert store.repo.get_session(THREAD).updated_at == before


def test_pinning_brings_a_thread_back_from_snooze_but_snoozing_keeps_the_pin(settings) -> None:
    store = _Store(settings)
    store.thread()
    store.repo.set_session_pinned(THREAD, pinned=True)
    store.repo.set_session_hidden(
        THREAD, hidden_as=ThreadHiddenAs.SNOOZED, hidden_until="2030-01-01T09:00:00+00:00", seen_sequence=0
    )
    snoozed = store.one()
    assert (snoozed.pinned, snoozed.hidden_as, snoozed.hidden_until) == (
        True, "snoozed", "2030-01-01T09:00:00+00:00"
    )
    store.repo.set_session_pinned(THREAD, pinned=True)
    pinned = store.one()
    assert (pinned.pinned, pinned.hidden_as, pinned.hidden_until, pinned.hidden_at_sequence) == (True, None, None, None)


def test_hiding_records_the_smaller_of_what_the_page_saw_and_what_the_thread_holds(settings) -> None:
    store = _Store(settings)
    store.thread()
    store.post("one")
    store.post("two")
    third = store.post("three")
    # The page had shown only the first message: the two it never showed bring the thread back.
    store.repo.set_session_hidden(THREAD, hidden_as=ThreadHiddenAs.DISMISSED, hidden_until=None, seen_sequence=1)
    assert store.one().hidden_at_sequence == 1
    # A page cannot claim more than the thread holds.
    store.repo.set_session_hidden(THREAD, hidden_as=ThreadHiddenAs.DISMISSED, hidden_until=None, seen_sequence=99)
    dismissed = store.one()
    assert (dismissed.hidden_as, dismissed.hidden_at_sequence, dismissed.last_sequence) == ("dismissed", third, third)


def test_a_thread_without_messages_hides_at_sequence_zero(settings) -> None:
    store = _Store(settings)
    store.thread()
    store.repo.set_session_hidden(THREAD, hidden_as=ThreadHiddenAs.DISMISSED, hidden_until=None, seen_sequence=5)
    record = store.one()
    assert (record.hidden_at_sequence, record.last_sequence, record.last_activity_at) == (0, 0, None)


def test_restore_clears_snooze_and_dismiss(settings) -> None:
    store = _Store(settings)
    store.thread()
    store.repo.set_session_hidden(
        THREAD, hidden_as=ThreadHiddenAs.SNOOZED, hidden_until="2030-01-01T09:00:00+00:00", seen_sequence=0
    )
    assert store.repo.clear_session_hidden(THREAD)
    record = store.one()
    assert (record.hidden_as, record.hidden_until, record.hidden_at_sequence) == (None, None, None)


def test_placement_changes_only_active_threads(settings) -> None:
    store = _Store(settings)
    store.thread()
    store.repo.set_session_status(THREAD, "archived")
    assert not store.repo.set_session_pinned(THREAD, pinned=True)
    assert not store.repo.set_session_hidden(
        THREAD, hidden_as=ThreadHiddenAs.DISMISSED, hidden_until=None, seen_sequence=0
    )
    assert not store.repo.clear_session_hidden(THREAD)
    assert not store.repo.set_session_pinned("missing", pinned=True)


def test_only_the_team_posting_after_a_snooze_counts(settings) -> None:
    store = _Store(settings)
    store.thread()
    seen = store.post("before the snooze")
    store.post("an older reply", role="assistant")
    store.repo.set_session_hidden(
        THREAD, hidden_as=ThreadHiddenAs.SNOOZED, hidden_until="2030-01-01T09:00:00+00:00", seen_sequence=seen
    )
    # The reply after what the page saw counts as the team posting.
    assert store.one().team_posted_after_hiding is True
    store.repo.set_session_hidden(
        THREAD, hidden_as=ThreadHiddenAs.SNOOZED, hidden_until="2030-01-01T09:00:00+00:00", seen_sequence=99
    )
    assert store.one().team_posted_after_hiding is False
    store.post("the person writes from another device")
    assert store.one().team_posted_after_hiding is False
    store.post("A card", role="system")
    assert store.one().team_posted_after_hiding is True


def test_the_team_post_check_is_only_for_snoozed_threads(settings) -> None:
    store = _Store(settings)
    store.thread()
    store.repo.set_session_hidden(THREAD, hidden_as=ThreadHiddenAs.DISMISSED, hidden_until=None, seen_sequence=0)
    store.post("a reply", role="assistant")
    assert store.one().team_posted_after_hiding is False


# What each row shows


def test_the_latest_message_skips_cards_and_commands(settings) -> None:
    store = _Store(settings)
    store.thread()
    store.post("Please check the backups")
    store.post("All backups finished.", role="assistant")
    store.post("/status")
    store.post("Status: idle", role="system")
    record = store.one()
    assert (record.last_message_role, record.last_message_text) == ("assistant", "All backups finished.")
    assert record.last_message_at is not None
    assert record.last_sequence == 4, "the last activity still counts every row"


def test_an_attachment_only_message_is_the_latest_message(settings) -> None:
    store = _Store(settings)
    store.thread()
    store.post("Here is the report", role="assistant")
    store.post("", attachments=(_attachment("budget.xlsx"),))
    record = store.one()
    assert record.last_message_role == "user" and record.last_message_text == ""
    assert "budget.xlsx" in (record.last_message_attachments_json or "")


def test_the_latest_message_text_is_cut_in_the_query(settings) -> None:
    store = _Store(settings)
    store.thread()
    store.post("x" * (LAST_MESSAGE_SQL_CHARACTERS * 5))
    assert len(store.one().last_message_text or "") == LAST_MESSAGE_SQL_CHARACTERS


def test_the_live_read_leaves_the_latest_message_out(settings) -> None:
    store = _Store(settings)
    store.thread()
    seen = store.post("hello")
    record = store.one(include_last_message=False)
    assert (record.last_message_role, record.last_message_text, record.last_message_at) == (None, None, None)
    assert record.last_sequence == seen and record.last_activity_at is not None


def test_pinned_threads_come_first_and_the_rest_by_last_activity(settings) -> None:
    store = _Store(settings)
    for key in ("first", "second", "third"):
        store.thread(key)
    # "first" was created first, but the team posted in it last.
    store.post("late reply", role="assistant", session_key="first")
    store.repo.set_session_pinned("second", pinned=True)
    keys = [
        record.session_key
        for record in store.repo.list_web_chat_threads(
            owner_canonical_user_id=ALICE, channel_name="website", chat_id="web-chat", limit=2
        )
    ]
    assert keys == ["second", "first"], "a pinned thread is never cut by the limit"


# What a thread waits on


def test_an_open_question_is_found(settings) -> None:
    store = _Store(settings)
    store.thread()
    assert store.one().has_open_question is False
    store.repo.add_pending_query(
        session_key=THREAD,
        correlation_id="request_1",
        task_query_id="query_1",
        parent_task_id="task_1",
        conversation_id=f"conv_{THREAD}",
        query_text="Which folder?",
        owner_canonical_user_id=ALICE,
    )
    assert store.one().has_open_question is True


def test_the_running_requests_status_is_read_with_its_lock(settings) -> None:
    store = _Store(settings)
    store.thread()
    assert (store.one().running_request_status, store.one().has_open_request) == (None, False)
    store.running_request(REQUEST_STATUS_WAITING_FOR_STEERING)
    record = store.one()
    assert (record.running_request_status, record.has_open_request) == (REQUEST_STATUS_WAITING_FOR_STEERING, True)


def test_a_setup_page_opened_by_the_requests_own_task_is_a_setup_wait(settings) -> None:
    """tool_setup sets the request to waiting_for_credential too, so the setup must be visible (C05)."""

    store = _Store(settings)
    store.thread()
    correlation_id = store.running_request("running")
    store.repo.record_tool_setup(
        ToolSetupRecord(
            setup_request_id="setup_1",
            parent_task_id="task_1",
            person_id=ALICE,
            tool_id="cli:github",
            kind="sign_in",
            owner_kind="personal",
            session_key=THREAD,
            correlation_id=correlation_id,
            conversation_id=f"conv_{THREAD}",
            status=TOOL_SETUP_STATUS_WAITING,
            created_at="2026-11-19T10:00:00+00:00",
        )
    )
    store.repo.update_request_coordination(correlation_id, status=WAITING_FOR_CREDENTIAL, active_task_id="task_1")
    record = store.one()
    assert (record.has_waiting_setup, record.running_request_status) == (True, WAITING_FOR_CREDENTIAL)
    assert store.listed(owner=BOB) == {}, "only the owner's threads are listed"
    store.repo.finish_tool_setup("setup_1", status="finished")
    assert store.one().has_waiting_setup is False


def test_setup_pages_and_forms_count_only_for_the_threads_person(settings) -> None:
    store = _Store(settings)
    store.thread()
    store.repo.record_tool_setup(
        ToolSetupRecord(
            setup_request_id="setup_bob",
            parent_task_id="task_1",
            person_id=BOB,
            tool_id="cli:github",
            kind="sign_in",
            owner_kind="personal",
            session_key=THREAD,
            correlation_id="request_1",
            conversation_id=None,
            status=TOOL_SETUP_STATUS_WAITING,
            created_at="2026-11-19T10:00:00+00:00",
        )
    )
    assert store.one().has_waiting_setup is False


def test_an_open_credential_form_is_found_until_it_ends(settings) -> None:
    store = _Store(settings)
    store.thread()
    store.repo.record_credential_collection(
        CredentialCollectionRecord(
            credential_request_id="form_1",
            broker_session_id="broker_1",
            person_id=ALICE,
            session_key=THREAD,
            correlation_id="request_1",
            conversation_id=f"conv_{THREAD}",
            parent_task_id="task_1",
            service_name="portainer",
            credential_key="api_token",
            display_name="Portainer",
            flow="api_key",
            source=None,
            source_item_id=None,
            source_item_name=None,
            continue_task=True,
            expires_at_utc="2030-01-01T00:00:00+00:00",
        )
    )
    assert store.one().has_waiting_credential_form is True
    store.repo.remove_credential_collection("form_1")
    assert store.one().has_waiting_credential_form is False


# Turn count for the hover preview


def test_turns_count_ordinary_messages_of_the_owners_thread_only(settings) -> None:
    store = _Store(settings)
    store.thread()
    store.post("Please check the backups")
    store.post("Done.", role="assistant")
    store.post("/status")
    store.post("Status: idle", role="system")
    assert store.repo.count_transcript_turns(owner_canonical_user_id=ALICE, session_key=THREAD) == 2
    assert store.repo.count_transcript_turns(owner_canonical_user_id=BOB, session_key=THREAD) == 0
