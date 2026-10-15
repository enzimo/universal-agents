import logging
import sqlite3
from pathlib import Path

from tests.postgres_test_helpers import patch_fake_postgres
from storage.config import resolve_storage_settings
from storage.conversation_repository import create_conversation_repository


def test_conversation_repository_roundtrips_threads_turns_and_delivery(tmp_path: Path) -> None:
    settings = resolve_storage_settings(
        {"backend": "sqlite", "sqlite": {"path": "runtime/task_history.db"}},
        base_dir=str(tmp_path),
    )
    repo = create_conversation_repository(settings, logger=logging.getLogger("test"), artifacts_dir=Path(settings.sqlite_path or '.').parent / "conversation_artifacts")

    thread = repo.ensure_thread(
        conversation_id="conversation_id_test",
        team_id="team_1",
        owner_agent_id="agent_1",
        owner_agent_name="Agent 1",
        requester_name="user_1",
        requester_id="user_1",
        root_task_id="task_1",
        subject_preview="First request",
    )
    turn = repo.append_turn(
        conversation_id="conversation_id_test",
        task_id="task_1",
        followup_to_task_id=None,
        owner_agent_id="agent_1",
        requester_name="user_1",
        requester_id="user_1",
        correlation_id="corr_1",
        problem_text="Do the first thing",
        requester_type="user_req",
        gateway_context={
            "gateway_instance_name": "test_gateway",
            "canonical_user_id": "canonical_user_1",
            "delivery_mode": "interactive",
        },
        task_status="accepted",
    )
    repo.append_clarification(task_id="task_1", message="Need one more detail.")
    completed = repo.update_turn_completion(
        task_id="task_1",
        task_status="completed",
        final_answer_text="Done.",
        turn_summary_text="Finished the first thing.",
        user_request_summary="Do the first thing.",
        assistant_response_summary="Done.",
        context_summary_text="The first thing was completed.",
        artifact_summary=[{"artifact_type": "note", "summary_text": "No artifacts needed."}],
        summary_generator="heuristic",
        execution_history_text="accepted -> completed",
    )
    delivered = repo.update_turn_delivery(
        conversation_id="conversation_id_test",
        correlation_id="corr_1",
        delivery_state="delivered",
        delivery_via="immediate",
        delivered_at="2026-04-05T01:00:00+00:00",
    )

    assert thread["conversation_id"] == "conversation_id_test"
    assert turn["turn_index"] == 1
    assert turn["gateway_context"]["canonical_user_id"] == "canonical_user_1"
    assert completed is not None
    assert completed["gateway_context"]["canonical_user_id"] == "canonical_user_1"
    assert completed["final_answer_text"] == "Done."
    assert completed["user_request_summary"] == "Do the first thing."
    assert completed["assistant_response_summary"] == "Done."
    assert completed["context_summary_text"] == "The first thing was completed."
    assert completed["artifact_summary"][0]["artifact_type"] == "note"
    assert completed["summary_generator"] == "heuristic"
    assert completed["summary_generated_at"]
    assert delivered is not None
    assert delivered["delivery_state"] == "delivered"
    assert repo.get_latest_task_link("conversation_id_test") is not None
    assert repo.get_thread("conversation_id_test")["status"] == "completed"
    assert len(repo.get_conversation_turns(conversation_id="conversation_id_test")) == 1


def test_conversation_repository_filters_turns_by_created_at(tmp_path: Path) -> None:
    settings = resolve_storage_settings(
        {"backend": "sqlite", "sqlite": {"path": "runtime/task_history.db"}},
        base_dir=str(tmp_path),
    )
    repo = create_conversation_repository(settings, logger=logging.getLogger("test"), artifacts_dir=Path(settings.sqlite_path or '.').parent / "conversation_artifacts")

    repo.ensure_thread(
        conversation_id="conversation_id_range",
        team_id="team_1",
        owner_agent_id="agent_1",
        owner_agent_name="Agent 1",
        requester_name="user_1",
        requester_id="user_1",
        root_task_id="task_1",
        subject_preview="Ranged request",
    )
    repo.append_turn(
        conversation_id="conversation_id_range",
        task_id="task_1",
        followup_to_task_id=None,
        owner_agent_id="agent_1",
        requester_name="user_1",
        requester_id="user_1",
        correlation_id="corr_1",
        problem_text="First request",
        requester_type="user_req",
        task_status="accepted",
    )
    repo.append_turn(
        conversation_id="conversation_id_range",
        task_id="task_2",
        followup_to_task_id="task_1",
        owner_agent_id="agent_1",
        requester_name="user_1",
        requester_id="user_1",
        correlation_id="corr_2",
        problem_text="Second request",
        requester_type="user_req",
        task_status="accepted",
    )

    db_path = Path(settings.sqlite_path)
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            "UPDATE conversation_task_links SET created_at = ?, updated_at = ? WHERE task_id = ?",
            ("2026-04-01T08:00:00+00:00", "2026-04-01T08:00:00+00:00", "task_1"),
        )
        conn.execute(
            "UPDATE conversation_task_links SET created_at = ?, updated_at = ? WHERE task_id = ?",
            ("2026-04-02T08:00:00+00:00", "2026-04-02T08:00:00+00:00", "task_2"),
        )
        conn.commit()

    filtered = repo.get_conversation_turns(
        conversation_id="conversation_id_range",
        created_after="2026-04-02",
        created_before="2026-04-03",
    )

    assert [row["task_id"] for row in filtered] == ["task_2"]


def test_conversation_repository_accepts_postgres_when_driver_is_available(tmp_path: Path, monkeypatch) -> None:
    settings = resolve_storage_settings(
        {
            "backend": "postgres",
            "postgres": {"dsn": "postgresql://team:secret@db/team_agents"},
        },
        base_dir=str(tmp_path),
    )
    patch_fake_postgres(monkeypatch)

    repo = create_conversation_repository(settings, logger=logging.getLogger("test"), artifacts_dir=Path(settings.sqlite_path or '.').parent / "conversation_artifacts")
    assert repo is not None


def test_long_texts_go_to_the_instance_layouts_folder_not_beside_the_database(tmp_path: Path) -> None:
    """The folder comes from InstanceLayout for every backend; nothing guesses it from a path."""

    from runtime_settings.instance_layout import InstanceLayout

    layout = InstanceLayout.at(tmp_path / "instance")
    settings = resolve_storage_settings({"backend": "sqlite", "sqlite": {"path": str(tmp_path / "elsewhere" / "task.db")}})
    repo = create_conversation_repository(
        settings, logger=logging.getLogger("test"), artifacts_dir=layout.conversation_artifacts_dir
    )

    stored = repo.offload_large_text(
        conversation_id="conversation_long", task_id="task_1", artifact_type="tool_result",
        raw_text="x" * 10_000, summary_text="A long result.",
    )

    assert layout.conversation_artifacts_dir == tmp_path / "instance" / "dbs" / "conversation_artifacts"
    assert Path(stored["storage_path"]).parent == layout.conversation_artifacts_dir / "conversation_long"
    assert not (tmp_path / "elsewhere" / "conversation_artifacts").exists()
