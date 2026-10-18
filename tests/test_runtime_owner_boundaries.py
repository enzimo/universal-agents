"""Exercise turn and mailbox owners without constructing either application host."""

import asyncio
import logging
from collections import deque
from dataclasses import fields
from types import SimpleNamespace

import pytest

from custom_agents.context_engine import AssembledContext
from custom_agents.task_operation_runtime import TaskOperationRuntime
from tests.async_operations import bind_async_operations
from custom_agents.sdk.strands.turn_ports import (
    TurnRuntimeEffects,
    TurnRuntimeSettings,
    TurnTaskOperations,
)
from custom_agents.sdk.strands.turn import LLMTurnExecutor
from custom_agents.structured_llm_messages import (
    StructuredAgentTurnDecision,
    StructuredTaskPayload,
    TaskResponseAction,
)
from custom_agents.task_manager_ports import RuntimeOperationsCoordinatorOperations
from custom_agents.task_manager_runtime_operations import RuntimeOperationsCoordinator
from custom_agents.task_runtime_coordinator import TaskRuntimeCoordinator
from custom_agents.task_runtime_ports import RuntimeMailboxDispatch
from custom_agents.task_runtime_state import (
    ExecutionRuntimeState,
    PlanRuntimeState,
    TaskRuntimeState,
)
from custom_messages.contracts.routing_v1 import MessageHeader, MessageMeta
from custom_messages.contracts.task_v1 import Task
from custom_messages.enums import MessageMetaType, ProgressState
from custom_tools.tool_stop import ToolStop


def unexpected(name):
    def fail(*args, **kwargs):
        raise AssertionError(f"Undeclared test interaction: {name}")

    return fail


def build_turn_owner(*, invoke, guidance, committed, cleared):
    decision = StructuredAgentTurnDecision(
        actions=[
            TaskResponseAction(
                task=StructuredTaskPayload(
                    task_type=MessageMetaType.RESPONSE, answer="done"
                ),
            )
        ]
    )

    async def publish_result(**kwargs):
        committed.append(kwargs)
        return Task(
            task_id=kwargs["incoming_content"].task_id,
            task_type=MessageMetaType.RESPONSE,
            answer="done",
        )

    effects = {item.name: unexpected(item.name) for item in fields(TurnRuntimeEffects)}
    effects.update(
        refresh_runtime_llm_selection=lambda: None,
        history_task_id_for_content=lambda content: content.task_id,
        set_active_execution_context=lambda task_id: ToolStop(agent_id="independent", task_id=task_id),
        select_turn_model=lambda task_id: None,
        clear_active_execution_context=lambda: cleared.append(True),
        ensure_fresh_strands_agent_for_new_turn=lambda **kwargs: False,
        reset_llm_conversation=lambda: None,
        refresh_skills_plugins=lambda: None,
        resolved_max_tool_calls_per_task=lambda: None,
        consume_pending_proactive_guidance_message=lambda task_id: guidance.popleft(),
        raise_if_stop_requested=lambda task_id: None,
        transform_result=lambda content, result: result,
        record_tool_result_budget_events=lambda **kwargs: None,
        record_model_invocation_success=lambda **kwargs: None,
        turn_decision_from_structured_output=lambda output: output,
        execute_turn_decision_outbox=publish_result,
        emit_activity_event=lambda **kwargs: None,
    )
    tasks = {item.name: unexpected(item.name) for item in fields(TurnTaskOperations)}
    tasks.update(
        runtime=TaskOperationRuntime(),
        objective_work_coordinator=None,
        is_isolated_recovery_task=lambda task_id: False,
        is_plan_step_task=lambda task_id: False,
        consume_runtime_resume_directive=lambda task_id: None,
        consume_partial_tool_call_limit=lambda task_id: None,
    )
    settings = TurnRuntimeSettings(
        id="independent",
        name="Independent",
        team_id="team",
        named_agent_type="Worker",
        model_provider="openai",
        model_id="test",
        system_prompt="",
        context_engine={
            "strands_state_snapshots": {"enabled": False},
        },
        debug={},
        inner_loop_context={},
        instance_config_path=None,
        workspace_dir=None,
        instance_root=None,
        is_team_lead=False,
        llm_override_path=None,
        litellm_api_base=None,
        litellm_api_mode="chat_completions",
        reasoning_effort=None,
        reasoning_summary=None,
        stream=False,
        supports_image_input=False,
        otel_enabled=False,
        otel_enable_traces=False,
    )
    context = SimpleNamespace(
        assemble=lambda request: AssembledContext(
            sections=[],
            prompt=request.incoming_message_json,
            char_count=len(request.incoming_message_json),
            estimated_tokens=10,
            source_lengths=[],
        ),
        estimate_tokens=lambda text: len(text) // 4,
        post_invocation=lambda **kwargs: None,
    )
    model = SimpleNamespace(invoke_async=invoke, messages=[])
    return LLMTurnExecutor(
        settings=lambda: settings,
        model=lambda: model,
        context=lambda: context,
        debug_dump=lambda: None,
        logger=logging.getLogger(__name__),
        tasks=TurnTaskOperations(**tasks),
        effects=TurnRuntimeEffects(
            **bind_async_operations(TurnRuntimeEffects, effects)
        ),
    ), SimpleNamespace(structured_output=decision)


@pytest.mark.asyncio
async def test_turn_owner_reapplies_late_steering_before_committing_output():
    prompts, committed, cleared = [], [], []

    async def invoke(prompt, **kwargs):
        prompts.append(prompt)
        return result

    executor, result = build_turn_owner(
        invoke=invoke,
        guidance=deque(["", "Use the corrected objective", ""]),
        committed=committed,
        cleared=cleared,
    )
    task = Task(
        task_id="first",
        task_type=MessageMetaType.REQUEST,
        problem_statement="Original objective",
    )
    response = await executor.process_llm_interaction(
        task, MessageHeader(), MessageMeta()
    )
    assert response.task_id == task.task_id
    assert len(prompts) == 2
    assert "Use the corrected objective" not in prompts[0]
    assert "Use the corrected objective" in prompts[1]
    assert len(committed) == 1
    assert cleared == [True]
    other, _ = build_turn_owner(
        invoke=invoke, guidance=deque([""]), committed=[], cleared=[]
    )
    assert other.last_context_assembly is None


@pytest.mark.asyncio
async def test_turn_cancellation_clears_execution_context_without_dispatch():
    started = asyncio.Event()
    committed, cleared = [], []

    async def invoke(*args, **kwargs):
        started.set()
        await asyncio.Event().wait()

    executor, _ = build_turn_owner(
        invoke=invoke,
        guidance=deque([""]),
        committed=committed,
        cleared=cleared,
    )
    execution = asyncio.create_task(
        executor.process_llm_interaction(
            Task(
                task_id="cancelled",
                task_type=MessageMetaType.REQUEST,
                problem_statement="Wait",
            ),
            MessageHeader(),
            MessageMeta(),
        )
    )
    await asyncio.wait_for(started.wait(), timeout=2)
    execution.cancel()
    with pytest.raises(asyncio.CancelledError):
        await execution
    assert committed == []
    assert cleared == [True]


@pytest.mark.asyncio
async def test_mailbox_owner_replays_failure_using_only_declared_state_and_operations():
    persisted, dispatched = [], []
    operations = {
        item.name: unexpected(item.name)
        for item in fields(RuntimeOperationsCoordinatorOperations)
    }
    operations.update(
        get_task_info=lambda task_id: None,
        persist_runtime_state=lambda: persisted.append(True),
        append_history_entry=lambda *args, **kwargs: None,
    )
    runtime = RuntimeOperationsCoordinator(
        operations=RuntimeOperationsCoordinatorOperations(**operations),
        execution_state=ExecutionRuntimeState(),
        plan_state=PlanRuntimeState(),
        task_state=TaskRuntimeState(),
        logger=logging.getLogger(__name__),
    )

    async def dispatch(**kwargs):
        dispatched.append(kwargs["sub_task_response"].task_id)
        assert coordinator.replay_event_id is not None
        if len(dispatched) == 1:
            raise RuntimeError("Temporary handler failure")

    dispatch_operations = {
        item.name: unexpected(item.name) for item in fields(RuntimeMailboxDispatch)
    }
    dispatch_operations["handle_task_response"] = dispatch
    coordinator = TaskRuntimeCoordinator(
        agent_id="owner",
        logger=logging.getLogger(__name__),
        configuration=lambda: {},
        available=lambda: True,
        runtime=runtime,
        operations_runtime=TaskOperationRuntime(),
        get_task_info=lambda task_id: SimpleNamespace(state=ProgressState.IN_PROGRESS),
        is_terminal_state=lambda state: False,
        dispatch=RuntimeMailboxDispatch(**dispatch_operations),
    )
    lease = coordinator.begin_batch(
        root_task_id="root", source_task_id="root", batch_id="batch"
    )
    assert coordinator.defer_envelope_if_blocked(
        root_task_id="root",
        event_type="task_response",
        event_class="deferrable",
        content=Task(
            task_id="child", parent_task_id="root", task_type=MessageMetaType.RESPONSE
        ),
        header=MessageHeader(),
        meta=MessageMeta(),
    )
    runtime.complete_runtime_batch_lease(lease.lease_id)
    assert await coordinator.drain_deferred_events("root") is False
    assert coordinator.replay_event_id is None
    assert len(runtime.list_deferred_runtime_events("root")) == 1
    assert await coordinator.drain_deferred_events("root") is True
    assert runtime.list_deferred_runtime_events("root") == []
    assert coordinator.replay_event_id is None
    assert dispatched == ["child", "child"]
    assert persisted
