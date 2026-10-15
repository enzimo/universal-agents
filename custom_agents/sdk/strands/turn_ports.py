"""Explicit contracts for the LLM turn owner and composition adapters."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable, Dict, Optional, Protocol

from strands.agent.agent_result import AgentResult

from custom_agents.model_failover_runtime import ModelFailoverOutcome
from custom_agents.objective_work_sessions import ObjectiveWorkSessionCoordinator
from custom_agents.structured_llm_messages import StructuredAgentTurnDecision
from custom_agents.task_records import TaskInfo
from custom_agents.task_operation_runtime import TaskOperationRuntime
from custom_agents.tool_call_limits import ToolCallLimitExceeded
from custom_messages.contracts.envelope_v1 import (
    CommsMessage,
    ContentType,
    MessageEnvelope,
)
from custom_messages.contracts.routing_v1 import MessageHeader, MessageMeta
from custom_messages.contracts.task_v1 import (
    Task,
    TaskHistoryEntry,
    TaskQuery,
    TaskSteering,
)
from custom_messages.enums import (
    RecoverablePauseTrigger,
    TaskActivityStage,
    TaskActivityType,
    TaskHistoryImportance,
    TaskHistorySource,
    TaskHistoryType,
)


@dataclass(frozen=True, slots=True)
class TurnRuntimeSettings:
    """Values captured by the composition root; contains no runtime host."""

    context_engine: Dict[str, Any]
    debug: Dict[str, Any]
    id: str
    inner_loop_context: Dict[str, Any]
    instance_config_path: Optional[str]
    litellm_api_base: Optional[str]
    litellm_api_mode: str
    llm_override_path: Optional[str]
    model_id: str
    model_provider: str
    name: str
    named_agent_type: Optional[str]
    otel_enable_traces: bool
    otel_enabled: bool
    reasoning_effort: Optional[str]
    reasoning_summary: Optional[str]
    stream: Optional[bool]
    supports_image_input: Optional[bool]
    system_prompt: str
    team_id: Optional[str]
    workspace_dir: Optional[str]
    instance_root: Optional[str]
    # TeamArchitect gets the full turn answer; specialists get the one without
    # credential-form and signup actions (structured_llm_messages.ModelWorkerTurnDecision).
    is_team_lead: bool


class ClearActiveExecutionContextOperation(Protocol):
    def __call__(self) -> None: ...


class CreateContextEngineOperation(Protocol):
    def __call__(self) -> None: ...


class ExecuteTurnDecisionOutboxOperation(Protocol):
    async def __call__(
        self,
        *,
        decision: StructuredAgentTurnDecision,
        incoming_content: ContentType,
        incoming_header: MessageHeader,
        incoming_meta: MessageMeta,
        structured_output_details: Optional[Dict[str, Any]] = ...,
    ) -> ContentType: ...


class FriendlyBudgetReasonOperation(Protocol):
    def __call__(
        self,
        *,
        trigger: RecoverablePauseTrigger,
        msg_content: ContentType,
        limit_error: Optional[ToolCallLimitExceeded] = ...,
        detail: Optional[str] = ...,
    ) -> str: ...


class HistoryTaskIdForContentOperation(Protocol):
    def __call__(self, msg_content: ContentType) -> Optional[str]: ...


class MaxUserGuidedPausesPerTaskOperation(Protocol):
    def __call__(self) -> int: ...


class RaiseIfStopRequestedOperation(Protocol):
    def __call__(self, task_id: Optional[str]) -> None: ...


class RecordModelInvocationSuccessOperation(Protocol):
    def __call__(self, *, task_id: Optional[str]) -> None: ...


class RecordToolCallLimitExceededOperation(Protocol):
    async def __call__(
        self, *, msg_content: ContentType, limit_error: ToolCallLimitExceeded
    ) -> None: ...


class RecordToolResultBudgetEventsOperation(Protocol):
    async def __call__(
        self, *, msg_content: ContentType, result: AgentResult
    ) -> None: ...


class RefreshSkillsPluginsOperation(Protocol):
    def __call__(self, force: bool = ...) -> None: ...


class ResetLlmConversationOperation(Protocol):
    def __call__(self) -> None: ...


class ResolvedMaxToolCallsPerTaskOperation(Protocol):
    def __call__(self) -> Optional[int]: ...


class RuntimeBudgetPolicyModeOperation(Protocol):
    def __call__(self) -> str: ...


class RuntimeBudgetTriggerEnabledOperation(Protocol):
    def __call__(self, trigger: RecoverablePauseTrigger) -> bool: ...


class SendPausedSubtaskResponseOperation(Protocol):
    async def __call__(
        self,
        *,
        task_request: Task,
        msg_header: MessageHeader,
        msg_meta: MessageMeta,
        error_message: str,
        trigger: RecoverablePauseTrigger,
    ) -> Task: ...


class HandleDelegatedProviderLimitOperation(Protocol):
    """Wait out a provider limit on a delegated task (custom_agents.provider_limits); always raises."""

    async def __call__(self, *, task_info: Any, error: BaseException) -> None: ...


class SendRuntimePauseQueryOperation(Protocol):
    async def __call__(
        self,
        *,
        task_info: TaskInfo,
        trigger: RecoverablePauseTrigger,
        friendly_reason: str,
        base_tool_call_limit: Optional[int] = ...,
        affected_subtask_id: Optional[str] = ...,
        affected_agent_id: Optional[str] = ...,
        affected_named_agent_type: Optional[str] = ...,
    ) -> TaskQuery: ...


class SetActiveExecutionContextOperation(Protocol):
    def __call__(self, task_id: Optional[str]) -> None: ...


class SelectTurnModelOperation(Protocol):
    """Choose the model for the turn of ``task_id``: a person's own one or the agent's (``person_model_runtime``).

    The return value (the chosen person's model, if any) is not used by the turn.
    """

    def __call__(self, task_id: Optional[str]) -> object: ...


class PrepareObjectiveWorkCompletionOperation(Protocol):
    def __call__(
        self, task: Task, meta: MessageMeta, session: Any,
    ) -> StructuredAgentTurnDecision: ...


class TurnDecisionFromCommsMessageOperation(Protocol):
    def __call__(self, comms_message: CommsMessage) -> StructuredAgentTurnDecision: ...


class TurnDecisionFromStructuredOutputOperation(Protocol):
    def __call__(
        self, structured_output: Any
    ) -> Optional[StructuredAgentTurnDecision]: ...


class AppendHistoryEntryOperation(Protocol):
    def __call__(
        self,
        task_id: str,
        summary: str,
        *,
        source: TaskHistorySource = ...,
        entry_type: TaskHistoryType = ...,
        importance: TaskHistoryImportance = ...,
        timestamp: Optional[str] = ...,
        agent_name: Optional[str] = ...,
        agent_id: Optional[str] = ...,
        related_task_id: Optional[str] = ...,
        plan_step_id: Optional[str] = ...,
        tracks_to_plan: Optional[bool] = ...,
        details: Optional[Dict[str, Any]] = ...,
    ) -> Optional[TaskHistoryEntry]: ...


class ConsumePartialToolCallLimitOperation(Protocol):
    def __call__(self, task_id: Optional[str]) -> Optional[int]: ...


class ConsumePendingProactiveGuidanceMessageOperation(Protocol):
    async def __call__(self, task_id: Optional[str]) -> str: ...


class ConsumeRuntimeResumeDirectiveOperation(Protocol):
    def __call__(self, task_id: Optional[str]) -> Optional[str]: ...


class CountRecoverablePausesOperation(Protocol):
    def __call__(self, task_id: str) -> int: ...


class EmitActivityEventOperation(Protocol):
    def __call__(
        self,
        *,
        activity_type: TaskActivityType,
        stage: TaskActivityStage,
        summary: str,
        detail: Optional[str] = ...,
        task_id: Optional[str] = ...,
        parent_task_id: Optional[str] = ...,
        top_level_task_id: Optional[str] = ...,
        objective_id: Optional[str] = ...,
        objective_title: Optional[str] = ...,
        meta: Optional[MessageMeta] = ...,
        user_visible: bool = ...,
        details: Optional[Dict[str, Any]] = ...,
        created_at: Optional[str] = ...,
    ) -> None: ...


class EnsureFreshStrandsAgentForNewTurnOperation(Protocol):
    def __call__(self, *, task_id: Optional[str]) -> bool: ...


class GetTaskInfoOperation(Protocol):
    def __call__(self, task_id: str) -> Optional[TaskInfo]: ...


class HandleProviderFailureOperation(Protocol):
    def __call__(
        self, *, error: BaseException, task_id: Optional[str], tool_calls_started: bool
    ) -> Optional[ModelFailoverOutcome]: ...


class IsIsolatedRecoveryTaskOperation(Protocol):
    def __call__(self, task_id: str) -> bool: ...


class IsPlanStepTaskOperation(Protocol):
    def __call__(self, task_id: str) -> bool: ...


class RefreshPersistentLlmOverrideOperation(Protocol):
    def __call__(self) -> None: ...


class RefreshRuntimeLlmSelectionOperation(Protocol):
    def __call__(self) -> None: ...


class RequeueTaskAfterFailoverOperation(Protocol):
    async def __call__(
        self,
        *,
        task_id: Optional[str],
        outcome: ModelFailoverOutcome,
        error: BaseException,
    ) -> bool: ...


class SendInterruptRequestForResultOperation(Protocol):
    async def __call__(
        self,
        *,
        result: Any,
        task: Task,
        msg_header: MessageHeader,
        msg_meta: MessageMeta,
        strands_agent: Any,
        invoke_kwargs: Dict[str, Any],
        full_prompt: str,
    ) -> TaskSteering: ...


class ShouldNotifyUserOperation(Protocol):
    def __call__(self) -> bool: ...


class SummarizeTaskHistoryMarkdownOperation(Protocol):
    def __call__(
        self,
        task_id: str,
        task_query_list: Optional[list[TaskQuery]] = ...,
        *,
        max_history_entries: int = ...,
        include_system: bool = ...,
        include_agent: bool = ...,
        include_execution_digest: bool = ...,
    ) -> str: ...


@dataclass(frozen=True, slots=True)
class TurnRuntimeEffects:
    clear_active_execution_context: ClearActiveExecutionContextOperation
    create_context_engine: CreateContextEngineOperation
    execute_turn_decision_outbox: ExecuteTurnDecisionOutboxOperation
    prepare_objective_work_completion: PrepareObjectiveWorkCompletionOperation
    friendly_budget_reason: FriendlyBudgetReasonOperation
    history_task_id_for_content: HistoryTaskIdForContentOperation
    max_user_guided_pauses_per_task: MaxUserGuidedPausesPerTaskOperation
    raise_if_stop_requested: RaiseIfStopRequestedOperation
    record_model_invocation_success: RecordModelInvocationSuccessOperation
    record_tool_call_limit_exceeded: RecordToolCallLimitExceededOperation
    record_tool_result_budget_events: RecordToolResultBudgetEventsOperation
    refresh_skills_plugins: RefreshSkillsPluginsOperation
    reset_llm_conversation: ResetLlmConversationOperation
    resolved_max_tool_calls_per_task: ResolvedMaxToolCallsPerTaskOperation
    runtime_budget_policy_mode: RuntimeBudgetPolicyModeOperation
    runtime_budget_trigger_enabled: RuntimeBudgetTriggerEnabledOperation
    send_paused_subtask_response: SendPausedSubtaskResponseOperation
    handle_delegated_provider_limit: HandleDelegatedProviderLimitOperation
    send_runtime_pause_query: SendRuntimePauseQueryOperation
    set_active_execution_context: SetActiveExecutionContextOperation
    select_turn_model: SelectTurnModelOperation
    turn_decision_from_comms_message: TurnDecisionFromCommsMessageOperation
    turn_decision_from_structured_output: TurnDecisionFromStructuredOutputOperation
    consume_pending_proactive_guidance_message: (
        ConsumePendingProactiveGuidanceMessageOperation
    )
    emit_activity_event: EmitActivityEventOperation
    ensure_fresh_strands_agent_for_new_turn: EnsureFreshStrandsAgentForNewTurnOperation
    handle_provider_failure: HandleProviderFailureOperation
    refresh_persistent_llm_override: RefreshPersistentLlmOverrideOperation
    refresh_runtime_llm_selection: RefreshRuntimeLlmSelectionOperation
    requeue_task_after_failover: RequeueTaskAfterFailoverOperation
    send_interrupt_request_for_result: SendInterruptRequestForResultOperation
    should_notify_user: ShouldNotifyUserOperation
    transform_result: Callable[[ContentType, AgentResult], AgentResult]
    publish: PublishTurnMessage


class PublishTurnMessage(Protocol):
    # Returns the stream sequence when the message was stored durably.
    async def __call__(self, *, message: MessageEnvelope, routing_key: str) -> Optional[int]: ...


@dataclass(frozen=True, slots=True)
class TurnTaskOperations:
    runtime: TaskOperationRuntime
    append_history_entry: AppendHistoryEntryOperation
    consume_partial_tool_call_limit: ConsumePartialToolCallLimitOperation
    consume_runtime_resume_directive: ConsumeRuntimeResumeDirectiveOperation
    count_recoverable_pauses: CountRecoverablePausesOperation
    get_task_info: GetTaskInfoOperation
    is_isolated_recovery_task: IsIsolatedRecoveryTaskOperation
    is_plan_step_task: IsPlanStepTaskOperation
    summarize_task_history_markdown: SummarizeTaskHistoryMarkdownOperation
    objective_work_coordinator: ObjectiveWorkSessionCoordinator | None
