import json
import logging
from pathlib import Path


from custom_agents.conversation_store import ConversationStore
from custom_messages.contracts.task_v1 import Task
from custom_messages.enums import MessageMetaType
from tests.postgres_test_helpers import patch_fake_postgres
from storage.config import resolve_storage_settings
from storage.task_archive_repository import create_task_archive_repository


def test_task_archive_repository_scopes_same_task_id_per_owner(tmp_path: Path) -> None:
    settings = resolve_storage_settings(
        {"backend": "sqlite", "sqlite": {"path": "runtime/task_history.db"}},
        base_dir=str(tmp_path),
    )
    repo = create_task_archive_repository(settings, logger=logging.getLogger("test"))

    task_a = Task(task_id="shared_task", task_type=MessageMetaType.REQUEST, problem_statement="Agent A view")
    task_b = Task(task_id="shared_task", task_type=MessageMetaType.REQUEST, problem_statement="Agent B view")

    repo.save_completed_task(
        archive_id="agent_a::shared_task",
        task_id="shared_task",
        owner_agent_id="agent_a",
        owner_agent_name="Agent A",
        team_id="team_1",
        task_json=task_a.model_dump_json(),
        status="completed",
    )
    repo.save_completed_task(
        archive_id="agent_b::shared_task",
        task_id="shared_task",
        owner_agent_id="agent_b",
        owner_agent_name="Agent B",
        team_id="team_1",
        task_json=task_b.model_dump_json(),
        status="completed",
    )

    row_a = repo.get_archived_task("shared_task", owner_agent_id="agent_a", include_all_agents=False)
    row_b = repo.get_archived_task("shared_task", owner_agent_id="agent_b", include_all_agents=False)

    assert Task.model_validate_json(row_a.task_json).problem_statement == "Agent A view"
    assert Task.model_validate_json(row_b.task_json).problem_statement == "Agent B view"


def test_task_archive_repository_lists_conversation_link_metadata(tmp_path: Path) -> None:
    settings = resolve_storage_settings(
        {"backend": "sqlite", "sqlite": {"path": "runtime/task_history.db"}},
        base_dir=str(tmp_path),
    )
    db_path = settings.sqlite_path
    repo = create_task_archive_repository(settings, logger=logging.getLogger("test"))

    task = Task(task_id="task_1", task_type=MessageMetaType.REQUEST, problem_statement="Completed archival task")
    repo.save_completed_task(
        archive_id="agent_a::task_1",
        task_id="task_1",
        owner_agent_id="agent_a",
        owner_agent_name="Agent A",
        team_id="team_1",
        task_json=task.model_dump_json(),
        status="completed",
    )

    conversation_store = ConversationStore(db_path=db_path, artifacts_dir=Path(db_path).parent / "conversation_artifacts")
    conversation_store.ensure_thread(
        conversation_id="conversation_id_archived1234",
        team_id="team_1",
        owner_agent_id="agent_a",
        owner_agent_name="Agent A",
        requester_name="TestAgent",
        requester_id="test_agent",
        root_task_id=task.task_id,
        subject_preview="Completed archival task",
    )
    conversation_store.append_turn(
        conversation_id="conversation_id_archived1234",
        task_id=task.task_id,
        followup_to_task_id=None,
        owner_agent_id="agent_a",
        requester_name="TestAgent",
        requester_id="test_agent",
        correlation_id="corr_id_archived1234",
        problem_text="Completed archival task",
        task_status="completed",
    )

    archived = repo.list_archived_tasks(
        limit=5,
        status=None,
        owner_agent_id="agent_a",
        include_all_agents=False,
    )

    assert len(archived) == 1
    assert archived[0].conversation_id == "conversation_id_archived1234"
    assert json.loads(archived[0].task_json)["problem_statement"] == "Completed archival task"


def test_task_archive_repository_filters_by_owner_canonical_user_id(tmp_path: Path) -> None:
    settings = resolve_storage_settings(
        {"backend": "sqlite", "sqlite": {"path": "runtime/task_history.db"}},
        base_dir=str(tmp_path),
    )
    repo = create_task_archive_repository(settings, logger=logging.getLogger("test"))

    task_a = Task(task_id="task_a", task_type=MessageMetaType.REQUEST, problem_statement="User A task")
    task_b = Task(task_id="task_b", task_type=MessageMetaType.REQUEST, problem_statement="User B task")
    repo.save_completed_task(
        archive_id="agent_a::task_a",
        task_id="task_a",
        owner_agent_id="agent_a",
        owner_agent_name="Agent A",
        team_id="team_1",
        owner_canonical_user_id="usr_a",
        task_json=task_a.model_dump_json(),
        status="completed",
    )
    repo.save_completed_task(
        archive_id="agent_a::task_b",
        task_id="task_b",
        owner_agent_id="agent_a",
        owner_agent_name="Agent A",
        team_id="team_1",
        owner_canonical_user_id="usr_b",
        task_json=task_b.model_dump_json(),
        status="completed",
    )

    listed = repo.list_archived_tasks(
        limit=10,
        status=None,
        owner_agent_id="agent_a",
        include_all_agents=False,
        owner_canonical_user_id="usr_a",
    )
    hidden = repo.get_archived_task(
        "task_b",
        owner_agent_id="agent_a",
        include_all_agents=False,
        owner_canonical_user_id="usr_a",
    )

    assert [row.task_id for row in listed] == ["task_a"]
    assert listed[0].owner_canonical_user_id == "usr_a"
    assert hidden is None


def test_task_archive_repository_accepts_postgres_when_driver_is_available(tmp_path: Path, monkeypatch) -> None:
    settings = resolve_storage_settings(
        {
            "backend": "postgres",
            "postgres": {"dsn": "postgresql://team:secret@db/team_agents"},
        },
        base_dir=str(tmp_path),
    )
    patch_fake_postgres(monkeypatch)

    repo = create_task_archive_repository(settings, logger=logging.getLogger("test"))
    assert repo is not None
