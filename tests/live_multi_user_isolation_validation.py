# Direct execution bootstraps the repository path before project imports.
# ruff: noqa: E402

from __future__ import annotations

import json
import os
import shutil
import sqlite3
import sys
from pathlib import Path
from types import SimpleNamespace

from dotenv import load_dotenv
from pytest import MonkeyPatch

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from config_loader import load_config
from custom_agents.conversation_store import ConversationStore
from custom_agents.schedule_store import ScheduleStore, schedule_record_digest
from custom_messages.contracts.routing_v1 import (
    GatewayContext,
    MessageHeader,
    MessageMeta,
)
from custom_messages.contracts.schedule_v1 import ScheduledMessage
from custom_messages.contracts.task_v1 import Task
from custom_messages.enums import MessageMetaType, RequesterType
from custom_tools.providers.schedules import (
    create_scheduled_job,
    list_scheduled_jobs,
    remove_scheduled_job,
)
from custom_tools.runtime_context import set_agent_ref
from storage.config import resolve_storage_settings
from storage.gateway_state_repository import SqliteGatewayStateRepository
from tests.broker_access_support import (
    context_signer,
    forget_task_context_keys,
    use_gateway_key,
)


RESULT_DIR = PROJECT_ROOT / "test_results" / "live_multi_user_isolation"
TEAM_ID = "team_live"


class _TaskManager:
    current_task_id = "task_current"

    def __init__(self, db_path: str, canonical_user_id: str, roles: tuple[str, ...]) -> None:
        from custom_agents.task_operation_runtime import TaskOperationRuntime

        self.operations_runtime = TaskOperationRuntime()
        self._db_path = db_path
        self.storage_settings = resolve_storage_settings(
            {"backend": "sqlite", "sqlite": {"path": db_path}},
            base_dir=str(PROJECT_ROOT),
        )
        self._canonical_user_id = canonical_user_id
        self._task_context = context_signer().issue(
            team_id=TEAM_ID, person_id=canonical_user_id, person_roles=roles,
        )
        self.history_entries: list[tuple[str, str]] = []

    def get_task_info(self, task_id: str):
        if task_id != self.current_task_id:
            return None
        return SimpleNamespace(
            header=MessageHeader(
                from_agent_id="gateway_queue",
                to_routing_path="manager_id",
                response_routing_key="gateway_queue",
                agent_signature="",
            ),
            meta=MessageMeta(
                from_agent_name="user",
                conversation_id=f"conversation_{self._canonical_user_id}",
                requester_type=RequesterType.USER_REQUESTER,
                gateway_context=GatewayContext(
                    source="live_validation",
                    user_id=self._canonical_user_id,
                    canonical_user_id=self._canonical_user_id,
                    task_context=self._task_context,
                    session_key=f"session_{self._canonical_user_id}",
                    channel_name="telegram",
                    chat_id=f"chat_{self._canonical_user_id}",
                ),
            ),
            task=Task(
                task_id=task_id,
                task_type=MessageMetaType.REQUEST,
                problem_statement="Validate multi-user schedule isolation.",
            ),
        )

    def append_history_entry(self, task_id: str, summary: str, **_kwargs):
        self.history_entries.append((task_id, summary))


class _Wake:
    def set(self) -> None:
        return None


class _Agent:
    def __init__(self, db_path: str, canonical_user_id: str, roles: tuple[str, ...]) -> None:
        self.id = "manager_id"
        self.name = "manager"
        self.team_id = TEAM_ID
        self.named_agent_type = "SchedulerAgent"
        self._schedule_store = ScheduleStore(db_path=db_path)
        self._conversation_store = ConversationStore(db_path=db_path, artifacts_dir=Path(db_path).parent / "conversation_artifacts")
        self._scheduler_wake = _Wake()
        self.task_manager = _TaskManager(db_path, canonical_user_id, roles)
        self.authorization_config = {"enabled": False}


def _reset_result_dir() -> None:
    if RESULT_DIR.exists():
        shutil.rmtree(RESULT_DIR)
    RESULT_DIR.mkdir(parents=True, exist_ok=True)


def main() -> int:
    if (PROJECT_ROOT / "debug.env").exists():
        load_dotenv(PROJECT_ROOT / "debug.env")
    loaded_config = load_config(str(PROJECT_ROOT / "config.yaml"))

    # The stand-in represents gateway-admitted requests, so its identities must
    # verify exactly like those requests. Test keys never come from live secrets.
    with MonkeyPatch.context() as monkeypatch:
        use_gateway_key(monkeypatch)
        try:
            return _validate_isolation(bool(loaded_config))
        finally:
            set_agent_ref(None)
            forget_task_context_keys()


def _validate_isolation(config_loaded: bool) -> int:
    _reset_result_dir()
    db_path = str(RESULT_DIR / "multi_user_isolation.db")

    gateway_repo = SqliteGatewayStateRepository(str(RESULT_DIR / "gateway_state.db"))
    admin = gateway_repo.create_gateway_user(display_name="Admin", timezone="UTC")
    member = gateway_repo.create_gateway_user(display_name="Member", timezone="UTC")
    gateway_repo.create_team_membership(
        canonical_user_id=admin.canonical_user_id, team_id=TEAM_ID, role="admin"
    )
    gateway_repo.create_team_membership(
        canonical_user_id=member.canonical_user_id, team_id=TEAM_ID, role="member"
    )
    gateway_repo.create_session(
        session_key="session_admin",
        conversation_id="conversation_admin",
        manager_agent_id="manager_id",
        owner_canonical_user_id=admin.canonical_user_id,
    )
    gateway_repo.create_session(
        session_key="session_member",
        conversation_id="conversation_member",
        manager_agent_id="manager_id",
        owner_canonical_user_id=member.canonical_user_id,
    )

    conversation_store = ConversationStore(db_path, artifacts_dir=Path(db_path).parent / "conversation_artifacts")
    conversation_store.ensure_thread(
        conversation_id="conversation_admin",
        team_id=TEAM_ID,
        owner_canonical_user_id=admin.canonical_user_id,
        owner_agent_id="manager_id",
        owner_agent_name="manager",
        requester_name="admin",
        requester_id="admin",
        root_task_id="task_admin",
    )
    conversation_store.ensure_thread(
        conversation_id="conversation_member",
        team_id=TEAM_ID,
        owner_canonical_user_id=member.canonical_user_id,
        owner_agent_id="manager_id",
        owner_agent_name="manager",
        requester_name="member",
        requester_id="member",
        root_task_id="task_member",
    )

    admin_agent = _Agent(db_path, admin.canonical_user_id, ("admin",))
    member_agent = _Agent(db_path, member.canonical_user_id, ("member",))
    set_agent_ref(admin_agent)
    created = json.loads(
        create_scheduled_job("Admin-owned private schedule.", interval_seconds=60)
    )
    assert "schedule_id" in created, created
    schedule_id = created["schedule_id"]
    admin_schedule_view = json.loads(list_scheduled_jobs(schedule_id=schedule_id))
    assert admin_schedule_view["returned"] == 1, admin_schedule_view
    assert admin_schedule_view["schedules"][0]["owner_canonical_user_id"] == admin.canonical_user_id

    set_agent_ref(member_agent)
    member_schedule_view = json.loads(list_scheduled_jobs())

    legacy_schedule_store = ScheduleStore(db_path=db_path)
    legacy_schedule = ScheduledMessage(
        schedule_id="sched_live_legacy_owner",
        payload=Task(
            task_type=MessageMetaType.REQUEST,
            problem_statement="Validate upgraded schedule owner propagation.",
        ),
        payload_meta=MessageMeta(
            gateway_context=GatewayContext(
                session_key="session_live_legacy",
                channel_name="telegram",
            )
        ),
        interval_ms=60_000,
        repeat_count=None,
    )
    legacy_schedule_store.add(legacy_schedule)
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            "UPDATE scheduled_messages SET owner_canonical_user_id = ? WHERE schedule_id = ?",
            (admin.canonical_user_id, legacy_schedule.schedule_id),
        )
        conn.commit()
    # The row's owner is authoritative: loading the schedule puts it into the run's context.
    restored_legacy_schedule = legacy_schedule_store.get_scheduled_message(
        legacy_schedule.schedule_id
    )
    assert restored_legacy_schedule is not None
    assert restored_legacy_schedule.payload_meta is not None
    restored_gateway_context = restored_legacy_schedule.payload_meta.gateway_context
    assert restored_gateway_context is not None

    # A missing relational owner must override stale personal metadata too.
    ownerless = legacy_schedule.model_copy(deep=True)
    ownerless.schedule_id = "sched_live_ownerless_stale_person"
    ownerless.payload_meta = admin_agent.task_manager.get_task_info(
        admin_agent.task_manager.current_task_id
    ).meta.model_copy(deep=True)
    ownerless.payload_meta.conversation_id = "conversation_admin"
    ownerless.payload_meta.followup_to_task_id = "task_admin"
    legacy_schedule_store.add(ownerless)
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            "UPDATE scheduled_messages SET owner_canonical_user_id = NULL WHERE schedule_id = ?",
            (ownerless.schedule_id,),
        )
        conn.commit()
    restored_ownerless = legacy_schedule_store.get_scheduled_message(ownerless.schedule_id)
    assert restored_ownerless is not None
    assert restored_ownerless.payload_meta.gateway_context.canonical_user_id is None
    assert restored_ownerless.payload_meta.gateway_context.task_context is None
    assert restored_ownerless.payload_meta.gateway_context.session_key is None
    assert restored_ownerless.payload_meta.conversation_id is None
    assert restored_ownerless.payload_meta.followup_to_task_id is None

    ownerless_views = {}
    for person_id, person_agent in (
        (admin.canonical_user_id, admin_agent),
        (member.canonical_user_id, member_agent),
    ):
        set_agent_ref(person_agent)
        ownerless_views[person_id] = json.loads(list_scheduled_jobs(schedule_id=ownerless.schedule_id))
        assert ownerless_views[person_id]["total"] == 0
        assert ownerless_views[person_id]["schedules"] == []
        # Even the schedule's exact version gives no way in.
        removal = json.loads(remove_scheduled_job(ownerless.schedule_id, schedule_record_digest(restored_ownerless)))
        assert removal["removed"] is False, removal
    with sqlite3.connect(db_path) as conn:
        remaining_owner = conn.execute(
            "SELECT owner_canonical_user_id FROM scheduled_messages WHERE schedule_id = ?",
            (ownerless.schedule_id,),
        ).fetchone()
    assert remaining_owner == (None,)

    admin_conversations = conversation_store.list_conversations(
        owner_canonical_user_id=admin.canonical_user_id,
    )
    member_conversations = conversation_store.list_conversations(
        owner_canonical_user_id=member.canonical_user_id,
    )

    result = {
        "config_loaded": config_loaded,
        "storage_backend_env": os.getenv("STORAGE_BACKEND"),
        "admin_user_id": admin.canonical_user_id,
        "member_user_id": member.canonical_user_id,
        "admin_sessions": [
            row.session_key
            for row in gateway_repo.list_sessions(
                owner_canonical_user_id=admin.canonical_user_id
            )
        ],
        "member_sessions": [
            row.session_key
            for row in gateway_repo.list_sessions(
                owner_canonical_user_id=member.canonical_user_id
            )
        ],
        "admin_conversations": [row["conversation_id"] for row in admin_conversations],
        "member_conversations": [
            row["conversation_id"] for row in member_conversations
        ],
        "admin_schedule_id": schedule_id,
        "member_schedule_view": member_schedule_view,
        "legacy_schedule_restored_owner": restored_gateway_context.canonical_user_id,
        "ownerless_schedule_restored_owner": restored_ownerless.payload_meta.gateway_context.canonical_user_id,
        "ownerless_schedule_hidden_from_people": ownerless_views,
    }
    (RESULT_DIR / "result.json").write_text(
        json.dumps(result, indent=2, sort_keys=True), encoding="utf-8"
    )

    assert result["admin_sessions"] == ["session_admin"]
    assert result["member_sessions"] == ["session_member"]
    assert "conversation_admin" in result["admin_conversations"]
    assert "conversation_member" not in result["admin_conversations"]
    assert result["member_conversations"] == ["conversation_member"]
    assert member_schedule_view == {
        "total": 0, "returned": 0, "offset": 0, "next_offset": None, "schedules": [],
    }
    assert result["legacy_schedule_restored_owner"] == admin.canonical_user_id
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
