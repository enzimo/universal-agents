"""Parallel turns keep separate prompts, SDK transcripts and saved conversation artifacts.

The provider is offline and transport publishing is captured by the existing
agent fixture. Context assembly, epoch rendering, Strands tool execution,
turn decisions, queue admission and conversation finalization are real.
"""

from __future__ import annotations

import asyncio
import copy
import json
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock

import pytest
from strands import Agent
from strands.models.model import Model

from custom_agents.conversation_compaction import RecursiveLargeTextArtifactOffloader
from custom_agents.execution_lanes import parallel_lane
from custom_agents.pending_work_runtime import close_pending_work
from custom_messages.contracts.routing_v1 import GatewayContext, MessageHeader, MessageMeta
from custom_messages.contracts.task_v1 import Task
from custom_messages.enums import MessageMetaType, ProgressState, RequesterType
from custom_tools.providers.context_memory import get_context_status
from custom_tools.runtime_context import bound_task_id, set_agent_ref
from tests.test_turn_action_outbox import _make_agent


class RecordingModel(Model):
    """Read context with a real tool, then answer; hold A after its tool returned."""

    def __init__(self) -> None:
        self.calls: dict[str, list[dict[str, Any]]] = {}
        self.ordinary_held = asyncio.Event()
        self.release_ordinary = asyncio.Event()

    def update_config(self, **config: Any) -> None:
        pass

    def get_config(self) -> dict[str, Any]:
        return {"model_id": "offline-context-test", "context_window_limit": 100_000}

    async def structured_output(self, *args: Any, **kwargs: Any):
        raise AssertionError("The turn decision must use Strands' structured-output tool")
        yield

    async def stream(self, messages, tool_specs=None, system_prompt=None, **kwargs):
        task_id = bound_task_id()
        assert task_id in {"task-a", "task-b", "task-a-followup"}
        calls = self.calls.setdefault(task_id, [])
        state = kwargs["model_state"]
        calls.append({"messages": copy.deepcopy(messages), "state_before": dict(state), "state_id": id(state)})
        state["context_isolation_probe"] = task_id
        if len(calls) == 1:
            name, arguments = "get_context_status", {}
        else:
            assert len(calls) == 2, "A tool failure must not be hidden by another model retry"
            if task_id == "task-a":
                self.ordinary_held.set()
                await self.release_ordinary.wait()
            name = next(spec["name"] for spec in tool_specs if spec["name"].endswith("TurnDecision"))
            arguments = {"actions": [{"action_type": "task_response", "task": {"answer": f"ANSWER_ONLY_{task_id}"}}]}
        yield {"messageStart": {"role": "assistant"}}
        yield {"contentBlockStart": {"start": {"toolUse": {"toolUseId": f"{task_id}-call-{len(calls)}", "name": name}}}}
        yield {"contentBlockDelta": {"delta": {"toolUse": {"input": json.dumps(arguments)}}}}
        yield {"contentBlockStop": {}}
        yield {"messageStop": {"stopReason": "tool_use"}}


def _append_turn(store, *, conversation: str, task_id: str, problem: str, answer: str = "") -> None:
    store.append_turn(
        conversation_id=conversation, task_id=task_id, followup_to_task_id=None,
        owner_agent_id="agent_id_manager", requester_name="user", requester_id="user",
        correlation_id=f"corr-{task_id}", problem_text=problem,
        requester_type=RequesterType.USER_REQUESTER.value, delivery_state="not_applicable",
        task_status=ProgressState.COMPLETED.value if answer else ProgressState.ACCEPTED.value,
    )
    if answer:
        store.update_turn_completion(
            task_id=task_id, task_status=ProgressState.COMPLETED.value,
            final_answer_text=answer, turn_summary_text="", user_request_summary=problem, assistant_response_summary=answer,
            context_summary_text="", artifact_summary=[], tool_artifact_manifest=[], summary_generator="heuristic",
        )


def _seed_conversation(agent, label: str) -> str:
    store = agent._conversation_store
    conversation = f"conversation-{label}"
    store.ensure_thread(
        conversation_id=conversation, team_id=agent.team_id, owner_agent_id=agent.id,
        owner_agent_name=agent.name, requester_name="user", requester_id="user", root_task_id=f"old-{label}",
    )
    _append_turn(store, conversation=conversation, task_id=f"old-{label}", problem=f"RAW_PRE_EPOCH_{label}", answer="Old answer")
    # The context-status tool returns enough readable text to exercise the
    # real conversation artifact threshold without triggering noise filters.
    summary = "\n".join(
        f"EPOCH_ONLY_{label}: Earlier turn detail {index} belongs to this conversation and remains relevant."
        for index in range(60)
    )
    epoch = store.create_context_epoch(
        conversation_id=conversation, trigger="manual", through_turn_index=1, preserved_suffix_start_turn=2,
        summary_json={"summary_markdown": summary},
        summary_markdown=summary, rehydration_manifest={"source_turn_range": [1, 1]},
    )
    _append_turn(store, conversation=conversation, task_id=f"recent-{label}", problem=f"RECENT_REQUEST_{label}", answer=f"RECENT_ANSWER_{label}")
    return epoch["context_epoch_id"]


def _add_request(agent, task_id: str, *, label: str, parallel: bool) -> None:
    conversation = f"conversation-{label}"
    problem = f"REQUEST_ONLY_{task_id}"
    _append_turn(agent._conversation_store, conversation=conversation, task_id=task_id, problem=problem)
    assert agent.task_manager.add_task(
        Task(task_id=task_id, task_type=MessageMetaType.REQUEST, problem_statement=problem),
        MessageHeader(from_agent_id="gateway", response_routing_key="user_reply"),
        MessageMeta(
            conversation_id=conversation, msg_correlation_id=f"corr-{task_id}",
            requester_type=RequesterType.USER_REQUESTER, is_response_requested=True,
            gateway_context=GatewayContext(parallel_thread=parallel, delivery_mode="interactive"),
        ),
        owner_person_id=None,
    )


def _context_result(call: dict[str, Any]) -> dict[str, Any]:
    results = [
        block["toolResult"] for message in call["messages"] for block in message.get("content", [])
        if "toolResult" in block and block["toolResult"]["toolUseId"].endswith("-call-1")
    ]
    assert len(results) == 1 and results[0]["status"] == "success"
    return json.loads(results[0]["content"][0]["text"])


@pytest.mark.integration
@pytest.mark.asyncio
async def test_parallel_sdk_turns_keep_their_context_and_artifacts_then_main_continues(tmp_path: Path) -> None:
    agent = _make_agent(
        tmp_path, instance_root=str(tmp_path / "instance"), workspace_dir=str(tmp_path / "workspace"),
        authorization_config={"enabled": False}, model_provider="openai", model_id="offline-context-test",
        context_window_tokens=100_000, max_tool_result_tokens_per_call=20_000,
        memory_config={"plugin_manager": {"enabled": False}}, conversation_summary_generator_type="heuristic",
        context_compaction_trigger_turn_count=100, context_compaction_trigger_message_count=100,
        debug={"context_dump": {"enabled": False}},
    )
    model = RecordingModel()
    agent.tools = [get_context_status]
    agent._strands_model = model
    agent._conversation_artifact_offloader = RecursiveLargeTextArtifactOffloader()
    agent._create_context_engine()
    # The fixture replaces Agent during construction only. All running turns use the real SDK.
    agent._create_strands_agent()
    assert isinstance(agent._strands_agent, Agent)
    # Run the queue steps explicitly so a release cannot start an unobserved drain.
    agent._execution_lanes.on_release = None
    object.__setattr__(agent, "_refresh_self_agent_state", AsyncMock())
    object.__setattr__(agent, "_publish_presence_status", AsyncMock())
    epochs = {label: _seed_conversation(agent, label) for label in ("A", "B")}
    _add_request(agent, "task-a", label="A", parallel=False)
    _add_request(agent, "task-b", label="B", parallel=True)
    set_agent_ref(agent)
    ordinary = asyncio.create_task(agent._check_and_process_next_task())

    async def parallel_turn() -> bool:
        async with agent._execution_scope(parallel_lane("task-b")):
            return await agent._check_and_process_next_task(parallel_lane("task-b"))

    parallel = None
    try:
        await asyncio.wait_for(model.ordinary_held.wait(), 10)
        parallel = asyncio.create_task(parallel_turn())
        assert await asyncio.wait_for(parallel, 10)
        assert not ordinary.done(), "The main SDK call remains held while the parallel turn finishes"
        assert agent.task_manager.get_task_info("task-a").state == ProgressState.IN_PROGRESS
        parallel_sdk = agent._parallel_lanes.parts[parallel_lane("task-b")].agent
        assert isinstance(parallel_sdk, Agent) and parallel_sdk is not agent._strands_agent
        assert parallel_sdk.model is agent._strands_agent.model is model
        model.release_ordinary.set()
        assert await asyncio.wait_for(ordinary, 10)

        for task_id, own, other in (("task-a", "A", "B"), ("task-b", "B", "A")):
            calls = model.calls[task_id]
            assert len(calls) == 2
            for call in calls:
                text = json.dumps(call["messages"])
                assert f"EPOCH_ONLY_{own}" in text and f"RECENT_ANSWER_{own}" in text
                assert f"EPOCH_ONLY_{other}" not in text and f"RECENT_ANSWER_{other}" not in text
                assert f"RAW_PRE_EPOCH_{own}" not in text
            status = _context_result(calls[1])
            assert (status["current_task_id"], status["conversation_id"], status["context_epoch_id"]) == (
                task_id, f"conversation-{own}", epochs[own],
            )
            assert calls[0]["state_before"] == {}
            assert calls[1]["state_before"]["context_isolation_probe"] == task_id
            saved = agent._conversation_store.get_turn_by_task_id(task_id)
            assert saved["task_status"] == ProgressState.COMPLETED.value
            assert saved["final_answer_text"] == f"ANSWER_ONLY_{task_id}"
            assert saved["tool_artifact_manifest"], "The actual tool result must reach final storage"
            for artifact in saved["tool_artifact_manifest"]:
                assert artifact["task_id"] == task_id and artifact["conversation_id"] == f"conversation-{own}"
                raw = Path(artifact["storage_path"]).read_text()
                assert f"EPOCH_ONLY_{own}" in raw and f"EPOCH_ONLY_{other}" not in raw

        _add_request(agent, "task-a-followup", label="A", parallel=False)
        assert await asyncio.wait_for(agent._check_and_process_next_task(), 10)
        followup = model.calls["task-a-followup"]
        text = json.dumps(followup[0]["messages"])
        assert "ANSWER_ONLY_task-a" in text, "A later turn must read its persisted answer"
        assert "ANSWER_ONLY_task-b" not in text and "EPOCH_ONLY_B" not in text
        assert not any("toolUse" in block or "toolResult" in block for message in followup[0]["messages"] for block in message["content"])
        assert followup[0]["state_before"] == {}, "The next turn starts with fresh provider state"
        assert _context_result(followup[1])["current_task_id"] == "task-a-followup"
        assert agent._conversation_store.get_turn_by_task_id("task-a-followup")["task_status"] == ProgressState.COMPLETED.value
    finally:
        model.release_ordinary.set()
        for task in (ordinary, parallel):
            if task is not None and not task.done():
                task.cancel()
        await asyncio.gather(*(task for task in (ordinary, parallel) if task is not None), return_exceptions=True)
        await close_pending_work(agent)
        set_agent_ref(None)
