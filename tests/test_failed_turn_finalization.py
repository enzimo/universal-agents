"""Failure and cancellation persist history without requesting another model."""

import logging
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from custom_agents.conversation_runtime import ensure_task_conversation_context, finalize_conversation_turn
from custom_agents.conversation_store import ConversationStore
from custom_agents.conversation_summary import HeuristicConversationSummaryGenerator
from custom_agents.task_manager import TaskManager
from custom_messages.contracts.routing_v1 import MessageHeader, MessageMeta
from custom_messages.contracts.task_v1 import Task
from custom_messages.enums import MessageMetaType, ProgressState


@pytest.mark.asyncio
@pytest.mark.parametrize("status", [ProgressState.CANCELLED, ProgressState.COMPLETED])
async def test_only_normal_turns_use_configured_summary_and_compaction(tmp_path, status):
    manager = TaskManager(db_path=str(tmp_path / "tasks.db"))
    manager.set_owner_identity("lead", "Lead", "team", "TeamArchitect")
    store = ConversationStore(db_path=str(tmp_path / "conversations.db"), artifacts_dir=tmp_path / "conversation_artifacts")
    task = Task(task_id="failed_turn", task_type=MessageMetaType.REQUEST, problem_statement="Review files")
    header = MessageHeader(from_agent_id="gateway")
    meta = ensure_task_conversation_context(
        conversation_store=store, team_id="team", owner_agent_id="lead",
        owner_agent_name="Lead", task_request=task, msg_header=header,
        msg_meta=MessageMeta(), owner_person_id=None, owner_named_agent_type="TeamArchitect",
    )
    assert manager.add_task(task, header, meta, owner_person_id=None)
    summary = SimpleNamespace(summarize_turn=Mock(wraps=HeuristicConversationSummaryGenerator().summarize_turn))
    compactor = SimpleNamespace(compact_if_needed=Mock())
    await finalize_conversation_turn(
        conversation_store=store, conversation_compactor=compactor,
        conversation_artifact_offloader=None, strands_messages=[], task_manager=manager,
        task_info=manager.get_task_info(task.task_id), task_status=status.value,
        logger=logging.getLogger(__name__), agent_id="lead", team_id="team",
        named_agent_type="TeamArchitect", summary_generator=summary,
    )
    row = store.get_turn_by_task_id(task.task_id)
    assert row["task_status"] == status.value
    assert row["summary_generator"] == "heuristic"
    assert row["user_request_summary"]
    if status == ProgressState.COMPLETED:
        summary.summarize_turn.assert_called_once()
        compactor.compact_if_needed.assert_called_once()
    else:
        summary.summarize_turn.assert_not_called()
        compactor.compact_if_needed.assert_not_called()
