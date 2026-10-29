"""Background completion must never archive another task's model transcript."""

from contextlib import nullcontext
import asyncio
import logging
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from custom_agents import task_message_runtime
from custom_agents.conversation_compaction.artifact_offloader import RecursiveLargeTextArtifactOffloader
from custom_agents.conversation_store import ConversationStore
from custom_agents.strands_agent import UniversalAgent
from custom_agents.strands_parts import task_strands_messages
from custom_agents.task_manager import TaskManager
from custom_messages.contracts.credentials_v1 import SecureCredentialCollectionResult
from custom_messages.contracts.routing_v1 import GatewayContext, MessageHeader, MessageMeta
from custom_messages.contracts.task_v1 import Task
from custom_messages.enums import MessageMetaType, ProgressState, RequesterType
from custom_tools.runtime_context import task_work
from tests.task_work_support import strands_parts


class _Host(SimpleNamespace):
    _finalize_conversation_turn = UniversalAgent._finalize_conversation_turn
    _notify_context_after_turn = UniversalAgent._notify_context_after_turn


def _messages(marker):
    # Exercise the production artifact threshold with recognizably distinct text.
    return [{"role": "user", "content": [{"toolResult": {
        "toolUseId": "result", "status": "success", "content": [{"text": marker * 300}],
    }}]}]


def _host(tmp_path):
    manager = TaskManager(db_path=str(tmp_path / "tasks.db"))
    manager.set_owner_identity("lead", "Lead", "team", "TeamArchitect")
    store = ConversationStore(db_path=str(tmp_path / "conversations.db"), artifacts_dir=tmp_path / "artifacts")
    for name in ("a", "b"):
        task_id, conversation_id = f"task-{name}", f"conversation-{name}"
        meta = MessageMeta(conversation_id=conversation_id, requester_type=RequesterType.USER_REQUESTER,
            gateway_context=GatewayContext(parallel_thread=name == "b"))
        manager.add_task(Task(task_id=task_id, task_type=MessageMetaType.REQUEST,
            problem_statement=f"Work for {name}"), MessageHeader(response_routing_key="reply"), meta, owner_person_id=None)
        store.ensure_thread(conversation_id=conversation_id, team_id="team", owner_agent_id="lead",
            owner_agent_name="Lead", requester_name="Person", requester_id="person", root_task_id=task_id)
        store.append_turn(conversation_id=conversation_id, task_id=task_id, followup_to_task_id=None,
            owner_agent_id="lead", requester_name="Person", requester_id="person", correlation_id=f"correlation-{name}",
            problem_text=f"Work for {name}")
    notifications = []
    host = _Host(id="lead", name="Lead", team_id="team", named_agent_type="TeamArchitect",
        _logger=logging.getLogger(__name__), task_manager=manager, _conversation_store=store,
        _conversation_compactor=None, _conversation_artifact_offloader=RecursiveLargeTextArtifactOffloader(),
        _conversation_summary_generator=None, _strands_agent=SimpleNamespace(messages=_messages("PRIVATE_A_RESULT")),
        _context_engine=SimpleNamespace(after_turn=notifications.append),
        _complete_task_and_schedule_aar_review=AsyncMock())
    return host, store, notifications


@pytest.mark.asyncio
@pytest.mark.parametrize("bound", [None, "task-a", "task-b-without-model"])
async def test_credential_completion_does_not_archive_an_ambient_transcript(tmp_path, monkeypatch, bound):
    host, store, notifications = _host(tmp_path)
    info = host.task_manager.get_task_info("task-b")
    response = Task(task_id="task-b", task_type=MessageMetaType.RESPONSE, answer="Account saved")
    monkeypatch.setattr(task_message_runtime, "_send_terminal_task_response", AsyncMock(return_value=response))
    result = SecureCredentialCollectionResult(credential_request_id="credential-b", parent_task_id="task-b",
        credential_key="example-account", service_name="Example", status="credential_stored",
        metadata={"flow": "credential_collection"})
    handling = task_work("task-b" if bound == "task-b-without-model" else bound) if bound else nullcontext()
    with handling as work:
        if bound == "task-a":
            work.strands = strands_parts(host._strands_agent)
        await task_message_runtime._handle_secure_credential_collection_result(host, result, info.header, info.meta)

    turn = store.get_turn_by_task_id("task-b")
    assert turn["final_answer_text"] == "Account saved"
    assert turn["tool_artifact_manifest"] == []
    assert len(notifications) == 1
    assert notifications[0].task_info.task.task_id == "task-b"
    assert notifications[0].strands_messages == []
    assert "PRIVATE_A_RESULT" in host._strands_agent.messages[0]["content"][0]["toolResult"]["content"][0]["text"]


@pytest.mark.asyncio
async def test_finalization_archives_its_own_transcript_after_the_turn_ends(tmp_path):
    host, store, notifications = _host(tmp_path)
    own = SimpleNamespace(messages=_messages("PRIVATE_B_RESULT"))
    with task_work("task-b") as work:
        work.strands = strands_parts(own)
        snapshot = task_strands_messages("task-b")
        # The storage snapshot is detached even if the SDK messages change later.
        own.messages[0]["content"][0]["toolResult"]["content"][0]["text"] += "LATER_EDIT"
        assert "LATER_EDIT" not in snapshot[0]["content"][0]["toolResult"]["content"][0]["text"]
        await host._finalize_conversation_turn(host.task_manager.get_task_info("task-b"), ProgressState.COMPLETED.value)

    artifacts = store.get_turn_by_task_id("task-b")["tool_artifact_manifest"]
    assert artifacts
    assert all(item["task_id"] == "task-b" and item["conversation_id"] == "conversation-b" for item in artifacts)
    assert "PRIVATE_B_RESULT" in artifacts[0]["summary_text"]
    assert "PRIVATE_A_RESULT" not in artifacts[0]["summary_text"]
    assert notifications[0].strands_messages == own.messages


@pytest.mark.asyncio
async def test_delayed_completion_rejects_expired_work_after_main_sdk_reuse(tmp_path, monkeypatch):
    host, store, notifications = _host(tmp_path)
    info = host.task_manager.get_task_info("task-b")
    response = Task(task_id="task-b", task_type=MessageMetaType.RESPONSE, answer="Account saved")
    monkeypatch.setattr(task_message_runtime, "_send_terminal_task_response", AsyncMock(return_value=response))
    result = SecureCredentialCollectionResult(credential_request_id="credential-b", parent_task_id="task-b",
        credential_key="example-account", service_name="Example", status="credential_stored",
        metadata={"flow": "credential_collection"})
    loop = asyncio.get_running_loop()
    completed = loop.create_future()

    def on_timer():
        completed.set_result(asyncio.create_task(task_message_runtime._handle_secure_credential_collection_result(
            host, result, info.header, info.meta,
        )))

    with task_work("task-b") as expired:
        host._strands_agent.messages = _messages("PRIVATE_B_RESULT")
        expired.strands = strands_parts(host._strands_agent)
        # call_soon retains this context, just as an outbox retry timer does.
        loop.call_soon(on_timer)
    with task_work("task-a") as current:
        current.strands = expired.strands
        host._strands_agent.messages = _messages("PRIVATE_A_RESULT")
        await (await asyncio.wait_for(completed, 5))

    assert store.get_turn_by_task_id("task-b")["tool_artifact_manifest"] == []
    assert notifications[0].strands_messages == []
    assert expired.handling_finished
