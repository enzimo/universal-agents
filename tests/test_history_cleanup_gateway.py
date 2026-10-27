"""The gateway's part of a person's history clean-up (assistant_gateway.history_cleanup).

SQLite gateway and manager stores. Covers: the person's chat records a
clean-up covers are deleted (transcripts, finished requests, closed
questions, delivered messages, attachments and their upload files), by
conversation up to its own time and, for an age-based clean-up, every other
record from before the date (the Updates thread included); kept are newer
messages, the conversation the clean-up was asked in, requests still
running, deliveries still pending and other people's records; a Web Chat
thread a whole deleted conversation left empty is removed and a session
pointing at a deleted last task stops following it up; the loop sends one
notice and logs the gateway's side once, and running it again changes
nothing.
"""

from __future__ import annotations

import sqlite3
from datetime import date, datetime, timedelta, timezone
from itertools import count
from pathlib import Path

import pytest

from assistant_gateway.domain.history import ConversationCut, GatewayHistoryScope
from assistant_gateway.history_cleanup import GatewayHistoryCleanup, gateway_scope, notice_text
from custom_messages.contracts.access_v2 import AccessNoticeKind, ChangeApprovalMode
from storage.access_repository import AccessRepository
from storage.config import resolve_storage_settings
from storage.gateway_state_repository import SqliteGatewayStateRepository
from storage.history_cleanup_repository import (
    ConversationSelectionV1,
    HistoryCleanupPlanV1,
    HistoryCleanupRepository,
    OrderStatus,
)

TEAM = "team_gateway_cleanup"
ALICE = "alice"
BOB = "bob"
PROACTIVE = "proactive-session"
NOW = datetime.now(timezone.utc)
_SEQUENCE = count(1)


def _days(days: float) -> str:
    return (NOW - timedelta(days=days)).isoformat()


@pytest.fixture
def gateway(tmp_path: Path):
    repo = SqliteGatewayStateRepository(str(tmp_path / "gateway_state.db"))
    uploads = tmp_path / "uploads"
    uploads.mkdir()
    return repo, uploads, str(tmp_path / "gateway_state.db")


def _sql(path: str, query: str, params: tuple = ()) -> list[sqlite3.Row]:
    with sqlite3.connect(path) as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute(query, params).fetchall()
        conn.commit()
    return rows


def _session(repo, key: str, person: str, conversation: str, *, chat_id: str, last_task: str | None = None) -> None:
    repo.create_session(
        session_key=key, conversation_id=conversation, manager_agent_id="lead", channel_name="website" if chat_id == "web-chat" else "telegram",
        user_id=person, chat_id=chat_id, owner_canonical_user_id=person,
    )
    if last_task:
        repo.upsert_session(
            session_key=key, session_alias=None, conversation_id=conversation, last_task_id=last_task,
            manager_agent_id="lead", channel_name="website" if chat_id == "web-chat" else "telegram", user_id=person,
            chat_id=chat_id, thread_id=None, status="active", owner_canonical_user_id=person,
        )


def _message(
    path: str, key: str, person: str, conversation: str | None, age: float, text: str, correlation: str | None = None
) -> None:
    number = next(_SEQUENCE)
    _sql(
        path,
        "INSERT INTO gateway_transcript_messages (schema_version, message_id, session_key, conversation_id, "
        "owner_canonical_user_id, channel_name, chat_id, session_sequence, event_sequence, role, content, message_type, "
        "message_level, correlation_id, source_kind, source_id, idempotency_key, delivery_status, created_at_utc) "
        "VALUES (1, ?, ?, ?, ?, 'website', 'web-chat', ?, ?, 'user', ?, 'chat', 'info', ?, 'test', 'test', ?, 'delivered', ?)",
        (f"m{number}", key, conversation, person, number, number, text, correlation, f"idem{number}", _days(age)),
    )


def _request(path: str, request_id: str, key: str, person: str, conversation: str | None, age: float, *, running: bool = False) -> None:
    _sql(
        path,
        "INSERT INTO gateway_requests (correlation_id, session_key, request_kind, request_text, conversation_id, "
        "owner_canonical_user_id, created_at, status, run_state) VALUES (?, ?, 'chat', ?, ?, ?, ?, 'done', ?)",
        (request_id, key, f"text of {request_id}", conversation, person, _days(age), "running" if running else "finished"),
    )


def _delivery(path: str, delivery_id: str, key: str, person: str, correlation: str | None, age: float, status: str) -> None:
    _sql(
        path,
        "INSERT INTO gateway_deliveries (delivery_id, correlation_id, session_key, origin_kind, payload_json, rendered_text, "
        "available_after, owner_canonical_user_id, status, created_at) VALUES (?, ?, ?, 'task', '{}', 'answer', ?, ?, ?, ?)",
        (delivery_id, correlation, key, _days(age), person, status, _days(age)),
    )


def _attachment(path: str, uploads: Path, attachment_id: str, key: str, person: str, conversation: str, age: float) -> Path:
    folder = uploads / conversation
    folder.mkdir(exist_ok=True)
    file = folder / f"{attachment_id}.txt"
    file.write_text("uploaded", encoding="utf-8")
    _sql(
        path,
        "INSERT INTO gateway_attachments (attachment_id, session_key, conversation_id, owner_canonical_user_id, filename, "
        "content_type, kind, size_bytes, sha256, local_path, created_at) VALUES (?, ?, ?, ?, 'f.txt', 'text/plain', 'file', 8, 'x', ?, ?)",
        (attachment_id, key, conversation, person, str(file), _days(age)),
    )
    return file


def _seed(gateway) -> dict[str, Path]:
    repo, uploads, path = gateway
    _session(repo, "web:alice:1", ALICE, "conv_whole", chat_id="web-chat", last_task="whole_t2")
    _message(path, "web:alice:1", ALICE, "conv_whole", 400, "whole question")
    _message(path, "web:alice:1", ALICE, "conv_whole", 399, "whole answer")
    _request(path, "req_whole", "web:alice:1", ALICE, "conv_whole", 400)
    _delivery(path, "del_whole", "web:alice:1", ALICE, "req_whole", 399, "delivered")
    upload = _attachment(path, uploads, "att_whole", "web:alice:1", ALICE, "conv_whole", 400)

    _session(repo, "tg:alice", ALICE, "conv_part", chat_id="tg-chat", last_task="part_t3")
    _message(path, "tg:alice", ALICE, "conv_part", 300, "old part")
    _message(path, "tg:alice", ALICE, "conv_part", 5, "new part")
    _request(path, "req_part_old", "tg:alice", ALICE, "conv_part", 300)
    _request(path, "req_part_running", "tg:alice", ALICE, "conv_part", 301, running=True)
    _delivery(path, "del_pending", "tg:alice", ALICE, "req_part_old", 300, "pending")

    _session(repo, f"{PROACTIVE}:alice", ALICE, "conv_updates", chat_id="web-chat")
    _message(path, f"{PROACTIVE}:alice", ALICE, None, 500, "old update notice")

    _session(repo, "web:alice:asked", ALICE, "conv_asked", chat_id="web-chat")
    _message(path, "web:alice:asked", ALICE, "conv_asked", 400, "asked here long ago")

    _session(repo, "web:bob", BOB, "conv_bob", chat_id="web-chat")
    _message(path, "web:bob", BOB, "conv_bob", 400, "bob's old message")
    _message(path, "web:bob", BOB, None, 400, "bob's old update")
    return {"upload": upload}


def _scope() -> GatewayHistoryScope:
    return GatewayHistoryScope(
        person_id=ALICE,
        selected_at=NOW.isoformat(),
        conversations=(
            ConversationCut("conv_whole", until=NOW.isoformat(), whole=True, latest_task_id="whole_t2"),
            ConversationCut("conv_part", until=_days(10), whole=False, latest_task_id="part_t1"),
        ),
        older_than=_days(100),
        kept_conversation_ids=("conv_asked",),
    )


def _contents(path: str) -> set[str]:
    return {row["content"] for row in _sql(path, "SELECT content FROM gateway_transcript_messages")}


def _removable(key: str):
    return lambda session: session.chat_id == "web-chat" and not session.session_key.startswith(PROACTIVE)


def test_the_persons_covered_chat_records_go_and_everything_else_stays(gateway) -> None:
    repo, _uploads, path = gateway
    _seed(gateway)
    deleted = repo.delete_history_records(_scope(), removable_thread=_removable(""))

    assert _contents(path) == {"new part", "asked here long ago", "bob's old message", "bob's old update"}
    assert deleted.transcript_messages == 4 and deleted.transcript_chars > 0
    requests = {row["correlation_id"] for row in _sql(path, "SELECT correlation_id FROM gateway_requests")}
    assert requests == {"req_part_running"}
    deliveries = {row["delivery_id"] for row in _sql(path, "SELECT delivery_id FROM gateway_deliveries")}
    assert deliveries == {"del_pending"}
    assert _sql(path, "SELECT * FROM gateway_attachments") == []
    assert len(deleted.attachment_paths) == 1
    sessions = {row["session_key"] for row in _sql(path, "SELECT session_key FROM gateway_sessions")}
    assert "web:alice:1" not in sessions and deleted.sessions_removed == 1
    assert {"tg:alice", f"{PROACTIVE}:alice", "web:alice:asked", "web:bob"} <= sessions
    part = _sql(path, "SELECT last_task_id FROM gateway_sessions WHERE session_key='tg:alice'")[0]
    assert part["last_task_id"] == "part_t3", "a partly deleted conversation keeps its follow-up"


def test_running_the_deletion_again_changes_nothing(gateway) -> None:
    repo, _uploads, path = gateway
    _seed(gateway)
    repo.delete_history_records(_scope(), removable_thread=_removable(""))
    before = _contents(path)
    again = repo.delete_history_records(_scope(), removable_thread=_removable(""))
    assert _contents(path) == before
    assert again.counts() == {key: 0 for key in again.counts()}


def test_a_telegram_session_on_a_deleted_conversation_starts_afresh_and_stays(gateway) -> None:
    repo, _uploads, path = gateway
    _session(repo, "tg:alice:gone", ALICE, "conv_gone", chat_id="tg-chat", last_task="gone_t1")
    _message(path, "tg:alice:gone", ALICE, "conv_gone", 50, "gone")
    scope = GatewayHistoryScope(
        person_id=ALICE,
        selected_at=NOW.isoformat(),
        conversations=(ConversationCut("conv_gone", until=NOW.isoformat(), whole=True, latest_task_id="gone_t1"),),
    )
    deleted = repo.delete_history_records(scope, removable_thread=_removable(""))
    row = _sql(path, "SELECT conversation_id, last_task_id FROM gateway_sessions WHERE session_key='tg:alice:gone'")[0]
    assert row["conversation_id"] == "conv_gone" and row["last_task_id"] is None
    assert deleted.sessions_restarted == 1 and deleted.sessions_removed == 0


def _order(tmp_path: Path, *, mode: str, grant_id: str | None) -> tuple[HistoryCleanupRepository, str, object]:
    storage = resolve_storage_settings({"backend": "sqlite", "sqlite": {"path": str(tmp_path / "task_history.db")}})
    store = HistoryCleanupRepository(storage)
    plan = HistoryCleanupPlanV1(
        order_id="history_cleanup_0123456789ab",
        team_id=TEAM,
        person_id=ALICE,
        actor_agent_id="agent_lead",
        actor_agent_type="TeamArchitect",
        source_task_id="task_1",
        authority_mode="person_approval",
        what="both",
        older_than=date.fromisoformat(_days(100)[:10]),
        created_at=NOW.isoformat(),
        expires_at=(NOW + timedelta(hours=24)).isoformat(),
        selected_at=NOW.isoformat(),
        excluded_conversation_ids=("conv_asked",),
        conversations=(
            ConversationSelectionV1(
                conversation_id="conv_whole", through_turn_index=2, whole=True, turns=2, first_at=_days(400),
                last_at=_days(399), latest_task_id="whole_t2",
            ),
        ),
    )
    store.save_plan(plan, at=NOW)
    assert store.start_deleting(plan.order_id, approval={"mode": mode, "grant_id": grant_id}, artifact_paths=[], at=NOW)
    assert store.finish_manager_part(
        plan.order_id, result={"conversations": 1, "conversations_partly": 0, "finished_tasks": 4, "finished_task_chars": 2000}, at=NOW
    )
    return store, plan.order_id, storage


def test_the_loop_applies_the_order_tells_the_person_once_and_closes_it(gateway, tmp_path: Path) -> None:
    repo, uploads, path = gateway
    files = _seed(gateway)
    store, order_id, storage = _order(tmp_path, mode=ChangeApprovalMode.HELD_ACCESS.value, grant_id="grant_alice")
    access = AccessRepository(storage)
    cleanup = GatewayHistoryCleanup(
        team_id=TEAM, storage=storage, state_store=repo, uploads_dir=uploads, proactive_session_key=PROACTIVE,
        send_notice_once=access.create_notice_if_absent, clock=lambda: NOW,
    )
    assert cleanup.run_once_sync() == 1
    assert not files["upload"].exists()
    order = store.get(order_id)
    assert order is not None and order.status is OrderStatus.DONE
    notices = [n for n, _read in access.notices_for(TEAM, person_id=ALICE, roles=("member",))]
    assert len(notices) == 1 and notices[0].kind is AccessNoticeKind.HISTORY_CLEANED_UP
    assert notices[0].grant_id == "grant_alice"
    assert "Remembered facts are kept" in notices[0].summary
    assert "whole question" not in notices[0].summary
    events = [event for event in access.events(TEAM) if event.kind == "history_cleanup"]
    assert [event.detail["side"] for event in events] == ["gateway"]

    assert cleanup.run_once_sync() == 0
    assert len([n for n, _read in access.notices_for(TEAM, person_id=ALICE, roles=("member",))]) == 1
    assert len([event for event in access.events(TEAM) if event.kind == "history_cleanup"]) == 1


def test_the_web_chat_threads_kept_are_the_gateways_own_and_other_channels(tmp_path: Path, gateway) -> None:
    repo, uploads, _path = gateway
    store, _order_id, storage = _order(tmp_path, mode=ChangeApprovalMode.PERSON_APPROVAL.value, grant_id=None)
    cleanup = GatewayHistoryCleanup(
        team_id=TEAM, storage=storage, state_store=repo, uploads_dir=uploads, proactive_session_key=PROACTIVE,
        send_notice_once=lambda notice: True,
    )
    _session(repo, "web:alice:mine", ALICE, "c1", chat_id="web-chat")
    _session(repo, f"{PROACTIVE}:alice", ALICE, "c2", chat_id="web-chat")
    _session(repo, "tg:alice", ALICE, "c3", chat_id="tg-chat")
    sessions = {session.session_key: session for session in repo.list_sessions(owner_canonical_user_id=ALICE, limit=10)}
    assert cleanup.removable_thread(sessions["web:alice:mine"])
    assert not cleanup.removable_thread(sessions[f"{PROACTIVE}:alice"])
    assert not cleanup.removable_thread(sessions["tg:alice"])


def test_the_notice_names_counts_and_how_it_was_approved_never_content(tmp_path: Path) -> None:
    store, order_id, _storage = _order(tmp_path, mode=ChangeApprovalMode.PERSON_APPROVAL.value, grant_id=None)
    order = store.get(order_id)
    assert order is not None
    scope = gateway_scope(order.plan)
    assert scope is not None and scope.kept_conversation_ids == ("conv_asked",) and scope.older_than is not None
    from assistant_gateway.domain.history import GatewayHistoryDeleted

    title, summary = notice_text(order, GatewayHistoryDeleted(transcript_messages=3, transcript_chars=1500))
    assert title == "TeamLead deleted part of your chat history"
    assert "1 conversation (1 entirely)" in summary and "3 chat messages" in summary and "4 finished tasks" in summary
    assert "You approved this clean-up in the Inbox." in summary
    assert "1 conversation stayed" in summary


def test_the_first_kept_question_stays_although_the_gateway_wrote_it_first(gateway) -> None:
    repo, _uploads, path = gateway
    _session(repo, "tg:alice:edge", ALICE, "conv_edge", chat_id="tg-chat")
    _message(path, "tg:alice:edge", ALICE, "conv_edge", 20, "deleted question", correlation="req_old")
    _message(path, "tg:alice:edge", ALICE, "conv_edge", 19.9, "deleted answer", correlation="req_old")
    # Written a moment before the manager started the turn (keep_from).
    _message(path, "tg:alice:edge", ALICE, "conv_edge", 10.0001, "first kept question", correlation="req_keep")
    _message(path, "tg:alice:edge", ALICE, "conv_edge", 9.9, "first kept answer", correlation="req_keep")
    scope = GatewayHistoryScope(
        person_id=ALICE,
        selected_at=NOW.isoformat(),
        conversations=(
            ConversationCut(
                "conv_edge", until=_days(10), whole=False, latest_task_id="edge_t1", keep_correlation_id="req_keep"
            ),
        ),
    )
    repo.delete_history_records(scope, removable_thread=_removable(""))
    assert _contents(path) == {"first kept question", "first kept answer"}
