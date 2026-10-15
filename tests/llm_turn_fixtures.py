"""Build the turn owner independently from small, explicit test doubles."""

from dataclasses import fields
from types import SimpleNamespace

from custom_agents import model_failover_runtime, task_steering_runtime
from custom_agents.sdk.strands.turn_ports import (
    TurnRuntimeEffects,
    TurnRuntimeSettings,
    TurnTaskOperations,
)
from custom_agents.sdk.strands.turn import LLMTurnExecutor
from custom_agents.task_operation_runtime import TaskOperationRuntime
from tests.async_operations import bind_async_operations


def turn_executor_for(subject) -> LLMTurnExecutor:
    if subject is None:
        subject = SimpleNamespace()
    existing = getattr(subject, "turn_executor", None)
    if isinstance(existing, LLMTurnExecutor):
        return existing

    def unexpected(name):
        def fail(*args, **kwargs):
            raise AssertionError(f"Unexpected turn operation: {name}")

        return fail

    task_manager = getattr(subject, "task_manager", SimpleNamespace())
    task_operations = {
        field.name: getattr(task_manager, field.name, unexpected(field.name))
        for field in fields(TurnTaskOperations)
        if field.name != "objective_work_coordinator"
    }
    task_operations["objective_work_coordinator"] = getattr(
        task_manager, "objective_work_coordinator", None
    )
    task_operations["runtime"] = getattr(
        task_manager, "operations_runtime", TaskOperationRuntime()
    )
    effect_operations = {
        field.name: getattr(
            subject,
            field.name,
            getattr(subject, "_" + field.name, unexpected(field.name)),
        )
        for field in fields(TurnRuntimeEffects)
    }
    effect_operations.update(
        select_turn_model=getattr(subject, "_select_turn_model", lambda task_id: None),
        should_notify_user=lambda: model_failover_runtime.should_notify_user(subject),
        handle_provider_failure=lambda **kwargs: (
            model_failover_runtime.handle_provider_failure(subject, **kwargs)
        ),
        requeue_task_after_failover=lambda **kwargs: (
            model_failover_runtime.requeue_task_after_failover(subject, **kwargs)
        ),
        ensure_fresh_strands_agent_for_new_turn=lambda **kwargs: (
            task_steering_runtime.ensure_fresh_strands_agent_for_new_turn(
                subject, **kwargs
            )
        ),
        consume_pending_proactive_guidance_message=lambda task_id: (
            task_steering_runtime.consume_pending_proactive_guidance_message(
                subject, task_id
            )
        ),
        send_interrupt_request_for_result=lambda **kwargs: (
            task_steering_runtime.send_interrupt_request_for_result(subject, **kwargs)
        ),
        transform_result=lambda content, result: result,
        publish=lambda **kwargs: subject.team_comms.send_message(**kwargs),
    )
    executor = LLMTurnExecutor(
        settings=lambda: TurnRuntimeSettings(
            **{
                field.name: getattr(subject, field.name, None)
                for field in fields(TurnRuntimeSettings)
            }
        ),
        model=lambda: getattr(subject, "_strands_agent", None),
        context=lambda: getattr(subject, "_context_engine", None),
        debug_dump=lambda: getattr(subject, "_context_debug_dump_plugin", None),
        logger=getattr(subject, "_logger", None),
        tasks=TurnTaskOperations(**task_operations),
        effects=TurnRuntimeEffects(
            **bind_async_operations(TurnRuntimeEffects, effect_operations)
        ),
    )
    # Existing unit doubles may override a single turn phase or diagnostic.
    for name, value in vars(subject).items():
        if name.startswith("_") and callable(value) and hasattr(executor, name):
            setattr(executor, name, value)
    subject.turn_executor = executor
    return executor
