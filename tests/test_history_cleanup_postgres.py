"""History clean-up on a real PostgreSQL server: the manager's part and the gateway's part.

The same deletions as the SQLite tests (tests/test_history_cleanup.py and
tests/test_history_cleanup_gateway.py) through the Postgres dialect: the
order record, choosing the person's turns and finished tasks (the JSONB
parent lookup), deleting a whole and a partly deleted conversation in one
transaction, and the gateway's chat records. Needs ``LIVE_TEST_POSTGRES_DSN``;
each test gets its own schema.
"""

from __future__ import annotations

import json
import os
from contextlib import contextmanager
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Iterator
from uuid import uuid4

import pytest

from assistant_gateway.domain.history import ConversationCut, GatewayHistoryScope
from storage.config import StorageSettings, resolve_storage_settings
from storage.conversation_repository import create_conversation_repository
from storage.gateway_state_repository import create_gateway_state_repository
from storage.history_cleanup_repository import (
    HistoryCleanupPlanV1,
    HistoryCleanupRepository,
    OrderStatus,
    select_conversation_turns,
    task_ids_to_delete,
)
from storage.task_archive_repository import create_task_archive_repository

pytestmark = [pytest.mark.live, pytest.mark.postgres, pytest.mark.integration]

TEAM = "team_pg_cleanup"
ALICE = "alice_pg"
BOB = "bob_pg"
NOW = datetime.now(timezone.utc)


def _days(days: float) -> str:
    return (NOW - timedelta(days=days)).isoformat()


@contextmanager
def postgres_storage() -> Iterator[StorageSettings]:
    from psycopg.conninfo import make_conninfo

    from storage.postgres_connection import connect_postgres

    dsn = os.environ.get("LIVE_TEST_POSTGRES_DSN")
    if not dsn:
        pytest.skip("LIVE_TEST_POSTGRES_DSN is required for real PostgreSQL history clean-up tests")
    schema = "history_cleanup_test_" + uuid4().hex
    with connect_postgres(dsn) as conn:
        conn.execute(f"CREATE SCHEMA {schema}")
        conn.commit()
    try:
        yield resolve_storage_settings(
            {"backend": "postgres", "postgres": {"dsn": make_conninfo(dsn, options=f"-csearch_path={schema}")}}
        )
    finally:
        with connect_postgres(dsn) as conn:
            conn.execute(f"DROP SCHEMA {schema} CASCADE")
            conn.commit()


@pytest.fixture
def storage() -> Iterator[StorageSettings]:
    with postgres_storage() as created:
        yield created


def _execute(storage: StorageSettings, query: str, params: tuple = ()) -> list:
    from storage.postgres_connection import connect_postgres

    with connect_postgres(storage.postgres_dsn or "") as conn:
        cursor = conn.execute(query, params)
        rows = cursor.fetchall() if query.lstrip().upper().startswith("SELECT") else []
        conn.commit()
    return rows


def _seed(storage: StorageSettings, artifacts: Path, person: str, conversation_id: str, ages: list[float]) -> list[str]:
    repo = create_conversation_repository(storage, artifacts_dir=artifacts)
    archive = create_task_archive_repository(storage)
    repo.ensure_thread(
        conversation_id=conversation_id, team_id=TEAM, owner_agent_id="lead", owner_agent_name="TeamArchitect",
        requester_name=None, requester_id=None, root_task_id=f"{conversation_id}_t1", subject_preview="title",
        owner_canonical_user_id=person,
    )
    task_ids = []
    for index, age in enumerate(ages, start=1):
        task_id = f"{conversation_id}_t{index}"
        task_ids.append(task_id)
        repo.append_turn(
            conversation_id=conversation_id, task_id=task_id, followup_to_task_id=None, owner_agent_id="lead",
            requester_name=None, requester_id=None, correlation_id=None, problem_text=f"question {index}",
            task_status="completed", owner_canonical_user_id=person,
        )
        for archived, parent in ((task_id, None), (f"{task_id}_sub", task_id)):
            archive.save_completed_task(
                archive_id=f"archive_{archived}", task_id=archived, owner_agent_id="lead", owner_agent_name="TeamArchitect",
                team_id=TEAM, task_json=json.dumps({"task_id": archived, "parent_task_id": parent}), status="completed",
                owner_canonical_user_id=person,
            )
        _execute(storage, "UPDATE conversation_task_links SET created_at=%s, updated_at=%s WHERE task_id=%s", (_days(age), _days(age), task_id))
        _execute(storage, "UPDATE completed_tasks SET timestamp=%s WHERE task_id IN (%s, %s)", (_days(age), task_id, f"{task_id}_sub"))
    repo.create_context_epoch(
        conversation_id=conversation_id, trigger="test", through_turn_index=len(ages),
        preserved_suffix_start_turn=len(ages) + 1, summary_markdown="summary",
    )
    return task_ids


def test_the_manager_part_deletes_old_turns_and_tasks_on_postgres(storage: StorageSettings, tmp_path: Path) -> None:
    store = HistoryCleanupRepository(storage)
    mixed = _seed(storage, tmp_path, ALICE, "pg_mixed", [300, 5])
    _seed(storage, tmp_path, ALICE, "pg_old", [400])
    _seed(storage, tmp_path, BOB, "pg_bob", [400])
    limit = _days(100)
    selections, left_out = select_conversation_turns(
        store.person_turns(ALICE), limit_at=limit, selected_at=NOW.isoformat(), active_since=_days(1)
    )
    assert {item.conversation_id: item.whole for item in selections} == {"pg_mixed": False, "pg_old": True}
    assert left_out == []
    tasks = task_ids_to_delete(store.person_tasks(ALICE), limit_at=limit, roots=None)
    assert {task.task_id for task in tasks} == {mixed[0], f"{mixed[0]}_sub", "pg_old_t1", "pg_old_t1_sub"}
    plan = HistoryCleanupPlanV1(
        order_id="history_cleanup_aaaaaaaaaaaa", team_id=TEAM, person_id=ALICE, actor_agent_id="lead",
        actor_agent_type="TeamArchitect", source_task_id="task", authority_mode="person_approval", what="both",
        older_than=date.fromisoformat(limit[:10]), created_at=NOW.isoformat(),
        expires_at=(NOW + timedelta(hours=24)).isoformat(), selected_at=NOW.isoformat(), conversations=tuple(selections),
    )
    store.save_plan(plan, at=NOW)
    assert store.start_deleting(plan.order_id, approval={"mode": "person_approval"}, artifact_paths=[], at=NOW)
    assert not store.start_deleting(plan.order_id, approval={}, artifact_paths=[], at=NOW)
    counts = store.delete_manager_records(plan, task_ids=[task.task_id for task in tasks], turn_task_ids=[mixed[0], "pg_old_t1"])
    assert counts["conversation_task_links"] == 2 and counts["completed_tasks"] == 4
    repo = create_conversation_repository(storage, artifacts_dir=tmp_path)
    repo.ensure_thread(
        conversation_id="pg_old", team_id=TEAM, owner_agent_id="lead", owner_agent_name="TeamArchitect",
        requester_name=None, requester_id=None, root_task_id="pg_after", subject_preview="New question",
        owner_canonical_user_id=ALICE,
    )
    repo.append_turn(conversation_id="pg_old", task_id="pg_after", followup_to_task_id=None, owner_agent_id="lead",
                     requester_name=None, requester_id=None, correlation_id=None, problem_text="Keep this question",
                     owner_canonical_user_id=ALICE)
    repo.create_context_epoch(conversation_id="pg_old", trigger="test", through_turn_index=1,
                              preserved_suffix_start_turn=2, summary_markdown="Keep this new summary")
    again = store.delete_manager_records(plan, task_ids=[task.task_id for task in tasks], turn_task_ids=[])
    assert again == counts, "a retry returns the committed result without deleting again"
    remaining = {row["task_id"] for row in _execute(storage, "SELECT task_id FROM conversation_task_links")}
    assert remaining == {mixed[1], "pg_bob_t1", "pg_after"}
    assert {row["conversation_id"] for row in _execute(storage, "SELECT conversation_id FROM conversation_threads")} == {"pg_mixed", "pg_bob", "pg_old"}
    assert {row["conversation_id"] for row in _execute(storage, "SELECT conversation_id FROM conversation_context_epochs")} == {"pg_bob", "pg_old"}
    assert store.finish_manager_part(plan.order_id, result={}, at=NOW)
    assert [order.plan.order_id for order in store.waiting_for_gateway(TEAM)] == [plan.order_id]
    assert store.conversation_deleted(TEAM, ALICE, "pg_old") and not store.conversation_deleted(TEAM, ALICE, "pg_mixed")
    assert store.finish_gateway_part(plan.order_id, result={}, at=NOW)
    assert store.get(plan.order_id).status is OrderStatus.DONE
    assert store.prune(TEAM, now=NOW + timedelta(days=31)) == 1


def test_the_gateway_part_deletes_chat_records_on_postgres(storage: StorageSettings, tmp_path: Path) -> None:
    repo = create_gateway_state_repository(storage)
    repo.create_session(
        session_key="web:pg", conversation_id="pg_conv", manager_agent_id="lead", channel_name="website",
        user_id=ALICE, chat_id="web-chat", owner_canonical_user_id=ALICE,
    )
    for number, (conversation, age, person) in enumerate(
        [("pg_conv", 400, ALICE), (None, 400, ALICE), ("pg_kept", 400, ALICE), ("pg_conv", 400, BOB)], start=1
    ):
        _execute(
            storage,
            "INSERT INTO gateway_transcript_messages (schema_version, message_id, session_key, conversation_id, "
            "owner_canonical_user_id, channel_name, chat_id, session_sequence, event_sequence, role, content, message_type, "
            "message_level, source_kind, source_id, idempotency_key, delivery_status, created_at_utc) "
            "VALUES (1, %s, %s, %s, %s, 'website', 'web-chat', %s, %s, 'user', %s, 'chat', 'info', 'test', 'test', %s, 'delivered', %s)",
            (f"pg_m{number}", "web:pg" if person == ALICE else "web:pg_bob", conversation, person, number, 100 + number,
             f"message {number}", f"pg_idem{number}", _days(age)),
        )
    scope = GatewayHistoryScope(
        person_id=ALICE,
        selected_at=NOW.isoformat(),
        conversations=(ConversationCut("pg_conv", until=NOW.isoformat(), whole=True, latest_task_id="pg_t1"),),
        older_than=_days(100),
        kept_conversation_ids=("pg_kept",),
        order_id="history_cleanup_pg_gateway",
    )
    upload = tmp_path / "upload.txt"
    upload.write_text("uploaded", encoding="utf-8")
    _execute(storage, "INSERT INTO gateway_attachments (attachment_id, session_key, conversation_id, "
             "owner_canonical_user_id, filename, content_type, kind, size_bytes, sha256, local_path, created_at) "
             "VALUES ('pg_attachment', 'web:pg', 'pg_conv', %s, 'f.txt', 'text/plain', 'file', 8, 'x', %s, %s)",
             (ALICE, str(upload), _days(400)))
    deleted = repo.delete_history_records(scope, removable_thread=lambda session: session.chat_id == "web-chat")
    # The thread keeps a message of the kept conversation, so it stays.
    assert deleted.transcript_messages == 2 and deleted.sessions_removed == 0
    remaining = {row["content"] for row in _execute(storage, "SELECT content FROM gateway_transcript_messages")}
    assert remaining == {"message 3", "message 4"}
    restarted = create_gateway_state_repository(storage)
    again = restarted.delete_history_records(scope, removable_thread=lambda session: True)
    assert again.counts() == deleted.counts() and again.attachment_paths == (str(upload),)
    assert restarted.history_cleanup_order_ids() == (scope.order_id,)
    restarted.forget_history_cleanup_order(scope.order_id)
    assert restarted.history_cleanup_order_ids() == ()
