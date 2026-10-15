from custom_agents.task_manager import TaskManager
from custom_messages.contracts.routing_v1 import MessageHeader, MessageMeta
from custom_messages.contracts.task_v1 import Task
from custom_messages.enums import MessageMetaType, RequesterType


def test_task_execution_summary_avoids_redundant_task_id_and_empty_metadata() -> None:
    manager = TaskManager()
    task = Task(
        task_id="task_summary_1",
        task_type=MessageMetaType.REQUEST,
        problem_statement="Summarize cleanly.",
    )
    header = MessageHeader(
        from_agent_id="gateway",
        timestamp="2026-06-11T16:50:32+00:00",
        agent_signature="",
    )
    meta = MessageMeta(
        from_agent_name="Taylor",
        requester_type=RequesterType.USER_REQUESTER,
    )

    assert manager.add_task(task, header, meta, owner_person_id=None)

    summary = manager.summarize_task_history_markdown(task.task_id)
    top_level = summary.split("## Rolling History", maxsplit=1)[0]

    assert summary.startswith("# Task Execution Summary: `task_summary_1`")
    assert "- **Task ID:**" not in top_level
    assert top_level.index("- **Current State:**") < top_level.index("- **From Agent:**")
    assert "- **To Agent:**" not in top_level
    assert "- **To Named Agent Type:**" not in top_level
    assert "- **Plan:** N/A" not in top_level
    assert "- **Errors:** N/A" not in top_level
