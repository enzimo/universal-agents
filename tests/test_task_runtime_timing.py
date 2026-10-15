from custom_agents.task_manager import TaskManager
from custom_messages.contracts.routing_v1 import MessageHeader, MessageMeta
from custom_messages.contracts.task_v1 import Task
from custom_messages.enums import MessageMetaType, ProgressState


def test_task_manager_records_message_handled_timing_without_schema_fields(tmp_path):
    manager = TaskManager(db_path=str(tmp_path / "timing.db"))
    manager.set_owner_identity(
        "agent_timing",
        "Timing Agent",
        "team_timing",
        "TeamArchitect",
    )
    task = Task(
        task_id="task_timing_1",
        task_type=MessageMetaType.REQUEST,
        problem_statement="Track timing.",
    )
    header = MessageHeader(
        from_agent_id="user_queue",
        message_id="msg_timing_1",
        timestamp="2026-05-05T10:00:00+00:00",
        agent_signature="",
    )
    meta = MessageMeta(from_agent_name="User")

    assert manager.add_task(
        task,
        header,
        meta,
        handled_at="2026-05-05T10:00:02+00:00",
        handled_queue_name="team_timing_TeamArchitect",
        owner_person_id=None,
    )

    entries = manager.get_task_history_entries(task.task_id, limit=None)
    handled = next(entry for entry in entries if entry.details.get("event") == "message_handled")
    assert handled.timestamp == "2026-05-05T10:00:02+00:00"
    assert handled.details["message_id"] == "msg_timing_1"
    assert handled.details["message_created_at"] == "2026-05-05T10:00:00+00:00"
    assert handled.details["queue_name"] == "team_timing_TeamArchitect"

    manager.update_main_task_state(
        task.task_id,
        ProgressState.IN_PROGRESS,
        "2026-05-05T10:00:03+00:00",
    )
    timing = manager.get_task_runtime_timing(task.task_id)
    assert timing == {
        "message_created_at": "2026-05-05T10:00:00+00:00",
        "handled_at": "2026-05-05T10:00:02+00:00",
        "processing_started_at": "2026-05-05T10:00:03+00:00",
        "processing_finished_at": None,
    }

    rendered = manager.render_task_runtime_timing_markdown(task.task_id)
    assert "Task Runtime Timing:" in rendered
    assert "Message created" in rendered
    assert "Accepted by this agent" in rendered
    assert "Processing started" in rendered
    assert "Processing finished" not in rendered

    terminal_events: list[tuple[str, ProgressState, str]] = []
    manager.add_task_terminal_callback(
        lambda task_id, state, timestamp: terminal_events.append((task_id, state, timestamp))
    )
    manager.update_main_task_state(
        task.task_id,
        ProgressState.COMPLETED,
        "2026-05-05T10:00:04+00:00",
    )
    manager.update_main_task_state(
        task.task_id,
        ProgressState.COMPLETED,
        "2026-05-05T10:00:05+00:00",
    )

    assert terminal_events == [
        (task.task_id, ProgressState.COMPLETED, "2026-05-05T10:00:04+00:00")
    ]
