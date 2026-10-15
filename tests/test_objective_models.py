from __future__ import annotations

from custom_messages.contracts.schedule_v1 import ScheduledMessage
from custom_messages.contracts.task_v1 import Task, TaskHistoryEntry
from custom_messages.enums import MessageMetaType, TaskHistoryImportance, TaskHistorySource, TaskHistoryType


def test_objective_links_do_not_make_task_payload_carry_objective_id() -> None:
    task = Task(
        task_type=MessageMetaType.REQUEST,
        problem_statement="Simple executable task.",
        execution_history=[
            TaskHistoryEntry(
                source=TaskHistorySource.SYSTEM,
                entry_type=TaskHistoryType.PLAN_EVENT,
                importance=TaskHistoryImportance.HIGH,
                summary="Linked to objective.",
                objective_id="objective_id_test",
            )
        ],
    )

    dumped = task.model_dump()

    assert "objective_id" not in dumped
    assert dumped["execution_history"][0]["objective_id"] == "objective_id_test"


def test_scheduled_message_can_carry_objective_link() -> None:
    message = ScheduledMessage(
        schedule_id="sched_objective",
        objective_id="objective_id_test",
        payload=Task(
            task_type=MessageMetaType.REQUEST,
            problem_statement="Run objective check.",
        ),
        interval_ms=60_000,
        repeat_count=None,
    )

    restored = ScheduledMessage.model_validate_json(message.model_dump_json())

    assert restored.objective_id == "objective_id_test"
    assert restored.payload.problem_statement == "Run objective check."
