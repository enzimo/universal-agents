from __future__ import annotations

import sqlite3
from pathlib import Path

from storage.config import resolve_storage_settings
from storage.conversation_repository import SqliteConversationRepository
from storage.gateway_state_repository import SqliteGatewayStateRepository
from storage.multi_user_owner_migration import GATEWAY_OWNER_TABLES, migrate_legacy_null_owner_rows


OWNER_ADMIN = "usr_admin"
OWNER_MEMBER = "usr_member"


def _sqlite_settings(path: Path):
    return resolve_storage_settings({"backend": "sqlite", "sqlite": {"path": str(path)}})


def _owner_values(db_path: Path, table: str, id_column: str, id_value: str) -> list[str | None]:
    with sqlite3.connect(db_path) as conn:
        rows = conn.execute(
            f"SELECT owner_canonical_user_id FROM {table} WHERE {id_column} = ?",
            (id_value,),
        ).fetchall()
    return [row[0] for row in rows]


def test_gateway_rows_with_no_owner_go_to_the_administrator_once(tmp_path: Path) -> None:
    gateway_db = tmp_path / "gateway.db"
    gateway_repo = SqliteGatewayStateRepository(str(gateway_db))
    gateway_repo.create_session(
        session_key="session_legacy",
        conversation_id="conversation_legacy",
        manager_agent_id="manager",
    )
    gateway_repo.register_conversation_session(
        session_key="session_legacy",
        conversation_id="conversation_legacy",
        last_task_id="task_legacy",
    )
    gateway_repo.create_session(
        session_key="session_member",
        conversation_id="conversation_member",
        manager_agent_id="manager",
        owner_canonical_user_id=OWNER_MEMBER,
    )

    report = migrate_legacy_null_owner_rows(
        owner_canonical_user_id=OWNER_ADMIN,
        gateway_storage_settings=_sqlite_settings(gateway_db),
    )

    assert report.updated_rows == 2
    assert _owner_values(gateway_db, "gateway_sessions", "session_key", "session_legacy") == [OWNER_ADMIN]
    assert _owner_values(gateway_db, "gateway_session_conversations", "conversation_id", "conversation_legacy") == [
        OWNER_ADMIN
    ]
    assert _owner_values(gateway_db, "gateway_sessions", "session_key", "session_member") == [OWNER_MEMBER]
    assert {table.table for table in report.tables} == set(GATEWAY_OWNER_TABLES)

    second_report = migrate_legacy_null_owner_rows(
        owner_canonical_user_id=OWNER_ADMIN,
        gateway_storage_settings=_sqlite_settings(gateway_db),
    )
    assert second_report.updated_rows == 0


def test_context_epoch_creation_inherits_thread_owner(tmp_path: Path) -> None:
    db_path = tmp_path / "manager.db"
    conversation_repo = SqliteConversationRepository(str(db_path), artifacts_dir=Path(db_path).parent / "conversation_artifacts")
    conversation_repo.ensure_thread(
        conversation_id="conversation_owned",
        team_id="team",
        owner_canonical_user_id=OWNER_ADMIN,
        owner_agent_id="manager",
        owner_agent_name="manager",
        requester_name="admin",
        requester_id="admin",
        root_task_id="task_owned",
    )

    epoch = conversation_repo.create_context_epoch(
        conversation_id="conversation_owned",
        trigger="test",
        through_turn_index=1,
        preserved_suffix_start_turn=2,
        latest_task_id="task_owned",
    )

    assert epoch["owner_canonical_user_id"] == OWNER_ADMIN
