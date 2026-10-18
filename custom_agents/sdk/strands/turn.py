"""LLM turn processing and context assembly helpers for UniversalAgent."""

from __future__ import annotations

from custom_agents.turn_failure_runtime import UnroutableTaskInterruptError

import asyncio
import hashlib
import mimetypes
import sys
import time
from dataclasses import dataclass, field, replace
from functools import partial
import logging
from strands import Agent
from custom_agents.context_engine import ContextEngine
from custom_agents.model_turn import TurnDecisionOutcome
from custom_agents.task_operation_runtime import TaskOperationRuntime
from custom_agents.sdk.strands.turn_tool_stop import (
    TURN_CANCELLED_REASON,
    invoke_with_tool_stop,
    stop_tool_calls,
)
from custom_agents.sdk.strands.turn_ports import (
    TurnRuntimeSettings,
    TurnRuntimeEffects,
    TurnTaskOperations,
)
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional
from strands.types.content import ContentBlock
from strands.types.media import ImageFormat

from pydantic import BaseModel, ValidationError
from strands.agent.agent_result import AgentResult
from strands.handlers.callback_handler import CompositeCallbackHandler

from custom_agents.activity_callbacks import (
    ActivityPhaseCallbackHandler,
    TerminalReasoningCallbackHandler,
    TerminalReasoningDiagnosticsCallbackHandler,
)
from custom_agents.context_engine import (
    AssembledContext,
    ContextAssemblyRequest,
    ContextBudgetExceededError,
    ContextCompactionReason,
    ContextSection,
)
from custom_agents.context_engine.config import merge_context_engine_config
from custom_agents.context_recovery import (
    ProviderContextRetryDecision,
    decide_provider_context_retry,
    max_total_recovery_attempts,
)
from custom_agents.context_debug_dump import ContextDebugDumpPlugin
from custom_agents import model_failover_runtime
from custom_agents.litellm_compatibility import (
    api_base_hostname,
    uses_litellm_compatible_schema,
)
from custom_agents.otel_runtime import start_runtime_span
from custom_agents.provider_error_runtime import (
    ProviderConnectivityError,
    ProviderTimeoutError,
    is_provider_connectivity_error,
    is_provider_overload_error,
    is_provider_timeout_error,
)
from custom_agents.reasoning_summary_recovery import (
    ReasoningSummaryRepair,
    disable_unsupported_reasoning_summary,
    is_unsupported_reasoning_summary_error,
)
from custom_agents.runtime_guardrails import RuntimeGuardrailsPlugin
from custom_agents.structured_llm_messages import (
    COMPATIBLE_MODEL_AGENT_TURN_DECISION_FORCE_PROMPT,
    COMPATIBLE_WORKER_TURN_DECISION_FORCE_PROMPT,
    CompatibleModelAgentTurnDecision,
    CompatibleWorkerTurnDecision,
    MODEL_AGENT_TURN_DECISION_FORCE_PROMPT,
    MODEL_WORKER_TURN_DECISION_FORCE_PROMPT,
    ModelAgentTurnDecision,
    ModelWorkerTurnDecision,
)
from custom_tools.tool_stop import ToolStop
from custom_agents.tool_call_limits import (
    ToolCallLimitExceeded,
    ToolCallLimitCallbackHandler,
    compact_debug_preview as _compact_debug_preview,
)
from custom_messages.contracts.notification_v1 import (
    MODEL_PROVIDER_FAILOVER_NOTIFICATION_REASON,
)
from custom_messages.contracts.delivery_v1 import UserNotification
from custom_messages.contracts.envelope_v1 import (
    CommsMessage,
    ContentType,
    MessageEnvelope,
)
from custom_messages.contracts.routing_v1 import MessageHeader, MessageMeta
from custom_messages.contracts.task_v1 import Task
from custom_messages.enums import ObjectiveWorkSessionStatus
from custom_messages.enums import (
    MessageMetaType,
    RecoverablePauseTrigger,
    RuntimeBudgetPolicyMode,
    RequesterType,
    TaskHistoryImportance,
    TaskHistoryType,
)
from reply_routing_trace import is_reply_routing_trace_enabled


async def _apply_objective_work_invocation_limits(
    *,
    coordinator: Any,
    runtime: TaskOperationRuntime,
    session: Any,
    invoke_kwargs: dict[str, Any],
    runtime_instruction_parts: list[str],
    explicit_tool_call_limit: Optional[int],
) -> Optional[int]:
    runtime_instruction_parts.append(
        await runtime.run(coordinator.prompt_context, session)
    )
    invoke_kwargs["limits"] = await runtime.run(coordinator.invocation_limits, session)
    remaining_tool_calls = int(
        await runtime.run(coordinator.remaining_work_tool_calls, session)
    )
    if explicit_tool_call_limit is None:
        return remaining_tool_calls
    return min(explicit_tool_call_limit, remaining_tool_calls)


def _objective_work_invocation_usage(result: Any) -> tuple[dict[str, Any], list[Any]]:
    latest_invocation = getattr(
        getattr(result, "metrics", None), "latest_agent_invocation", None
    )
    return (
        getattr(latest_invocation, "usage", {}) or {},
        getattr(latest_invocation, "cycles", []) or [],
    )


async def _finalize_objective_work_invocation(
    *,
    model: Agent,
    logger: logging.Logger,
    coordinator: Any,
    runtime: TaskOperationRuntime,
    session: Any,
    result: Any,
    invocation_started: float,
    invoke_kwargs: dict[str, Any],
    tool_call_limit_handler: Optional[ToolCallLimitCallbackHandler],
    msg_content: ContentType,
    active_task_id: Optional[str],
    tool_stop: ToolStop,
) -> Optional[Task]:
    """Account one bounded invocation and produce a durable-yield response if needed."""
    usage, cycles = _objective_work_invocation_usage(result)
    session = await runtime.run(
        coordinator.account_invocation,
        session,
        elapsed_seconds=time.monotonic() - invocation_started,
        turns=len(cycles),
        input_tokens=int(usage.get("inputTokens", 0) or 0),
        output_tokens=int(usage.get("outputTokens", 0) or 0),
        total_tokens=int(usage.get("totalTokens", 0) or 0),
        cache_read_input_tokens=int(usage.get("cacheReadInputTokens", 0) or 0),
        cache_write_input_tokens=int(usage.get("cacheWriteInputTokens", 0) or 0),
        tool_calls=len(
            tool_call_limit_handler.tool_sequence
            if tool_call_limit_handler is not None
            else []
        ),
        stop_reason=getattr(result, "stop_reason", None),
    )
    missing_skills = await runtime.run(coordinator.missing_required_skills, session)
    limit_reached = str(getattr(result, "stop_reason", "") or "").startswith("limit_")
    session_boundary = session.status in {
        ObjectiveWorkSessionStatus.YIELD_REQUESTED,
        ObjectiveWorkSessionStatus.OVERRUN,
    }
    if not (missing_skills or limit_reached or session_boundary):
        return None
    if missing_skills:
        reason = "Required work-session skills were not activated: " + ", ".join(
            missing_skills
        )
    elif limit_reached:
        reason = (
            f"Strands stopped at the configured {result.stop_reason} safe boundary."
        )
    else:
        reason = (
            session.yield_reason or "The configured work-session boundary was reached."
        )

    checkpoint = None
    if session.checkpoint_id:
        checkpoints = await runtime.run(
            coordinator.objective_work_repository.list_checkpoints,
            session.objective_id,
            owner_agent_id=session.owner_agent_id,
            limit=1,
        )
        checkpoint = next(
            (
                item
                for item in checkpoints
                if item.checkpoint_id == session.checkpoint_id
            ),
            None,
        )
    elif not missing_skills:
        checkpoint_started = time.monotonic()
        checkpoint_handler = ToolCallLimitCallbackHandler(
            max_tool_calls=max(1, session.budget.checkpoint_reserve_tool_calls),
            raise_on_exceeded=True,
        )

        def checkpoint_callback(**callback_kwargs: Any) -> None:
            current_tool_use = callback_kwargs.get("current_tool_use") or {}
            tool_name = str(current_tool_use.get("name") or "")
            if tool_name and tool_name != "record_objective_work_checkpoint":
                raise RuntimeError(
                    "Checkpoint-only invocation attempted a non-checkpoint tool: "
                    f"{tool_name}"
                )
            checkpoint_handler(**callback_kwargs)

        checkpoint_kwargs = {
            "limits": await runtime.run(
                coordinator.checkpoint_invocation_limits, session
            ),
            "callback_handler": checkpoint_callback,
            "invocation_state": dict(invoke_kwargs.get("invocation_state") or {}),
        }
        checkpoint_kwargs["invocation_state"].pop(
            RuntimeGuardrailsPlugin._TURN_DECISION_CONTEXT_KEY,
            None,
        )
        checkpoint_prompt = (
            "CHECKPOINT-ONLY BOUNDED OBJECTIVE PASS\n"
            f"Session {session.session_id} reached this boundary: {reason}\n"
            "Do not begin or continue material work. Use the durable Objective, plan, "
            "task history, evidence, artifacts, and in-flight effects already available. "
            "Call record_objective_work_checkpoint exactly once with the most accurate "
            "OODA continuation state you can establish. Do not call any other tool."
        )
        try:
            checkpoint_result = await invoke_with_tool_stop(
                model,
                checkpoint_prompt,
                tool_stop=tool_stop,
                invoke_kwargs=checkpoint_kwargs,
                logger=logger,
            )
            checkpoint_usage, checkpoint_cycles = _objective_work_invocation_usage(
                checkpoint_result
            )
            session = await runtime.run(
                coordinator.account_invocation,
                session,
                elapsed_seconds=time.monotonic() - checkpoint_started,
                turns=len(checkpoint_cycles),
                input_tokens=int(checkpoint_usage.get("inputTokens", 0) or 0),
                output_tokens=int(checkpoint_usage.get("outputTokens", 0) or 0),
                total_tokens=int(checkpoint_usage.get("totalTokens", 0) or 0),
                cache_read_input_tokens=int(
                    checkpoint_usage.get("cacheReadInputTokens", 0) or 0
                ),
                cache_write_input_tokens=int(
                    checkpoint_usage.get("cacheWriteInputTokens", 0) or 0
                ),
                tool_calls=len(checkpoint_handler.tool_sequence),
                stop_reason=getattr(checkpoint_result, "stop_reason", None),
            )
            if session.checkpoint_id:
                checkpoints = await runtime.run(
                    coordinator.objective_work_repository.list_checkpoints,
                    session.objective_id,
                    owner_agent_id=session.owner_agent_id,
                    limit=1,
                )
                checkpoint = next(
                    (
                        item
                        for item in checkpoints
                        if item.checkpoint_id == session.checkpoint_id
                    ),
                    None,
                )
        except Exception as exc:
            logger.warning(
                "Checkpoint-only Objective invocation failed; using deterministic degraded checkpoint. "
                "event=objective_work_checkpoint_invocation_failed session_id=%s "
                "objective_id=%s error_type=%s error=%s",
                session.session_id,
                session.objective_id,
                type(exc).__name__,
                exc,
            )
    if checkpoint is None:
        checkpoint = await runtime.run(
            coordinator.degraded_checkpoint, session, reason=reason
        )
    return Task(
        task_id=msg_content.task_id
        if isinstance(msg_content, Task)
        else active_task_id,
        parent_task_id=msg_content.parent_task_id
        if isinstance(msg_content, Task)
        else None,
        task_type=MessageMetaType.RESPONSE,
        problem_statement=msg_content.problem_statement
        if isinstance(msg_content, Task)
        else None,
        answer=(
            "The bounded Objective work session yielded safely. "
            f"Checkpoint {checkpoint.checkpoint_id} preserves the continuation state."
        ),
        errors=["objective_work_session_yield", reason],
    )


def _turn_decision_initial_guidance(tool_name: str) -> str:
    return (
        "Terminal structured-output instruction: use regular tools as needed, but when this task turn is complete, "
        f"call exactly `{tool_name}` with one or more populated actions. Do not emit or draft the terminal answer "
        "as top-level prose. For an ordinary answer, use a task_response action and put the user-visible answer "
        "in actions[].task.answer."
    )


DIRECT_MODEL_IMAGE_MAX_BYTES = 20 * 1024 * 1024
DIRECT_MODEL_IMAGE_FORMATS: dict[str, ImageFormat] = {
    "image/png": "png",
    "image/jpeg": "jpeg",
    "image/jpg": "jpeg",
    "image/gif": "gif",
    "image/webp": "webp",
}
DIRECT_MODEL_IMAGE_SUFFIX_FORMATS: dict[str, ImageFormat] = {
    ".png": "png",
    ".jpg": "jpeg",
    ".jpeg": "jpeg",
    ".gif": "gif",
    ".webp": "webp",
}


def _attachment_image_format(attachment: Any, path: Path) -> ImageFormat | None:
    content_type = (
        str(getattr(attachment, "content_type", "") or "")
        .split(";", 1)[0]
        .strip()
        .lower()
    )
    if content_type in DIRECT_MODEL_IMAGE_FORMATS:
        return DIRECT_MODEL_IMAGE_FORMATS[content_type]
    guessed_type = mimetypes.guess_type(str(path))[0]
    guessed_type = str(guessed_type or "").split(";", 1)[0].strip().lower()
    if guessed_type in DIRECT_MODEL_IMAGE_FORMATS:
        return DIRECT_MODEL_IMAGE_FORMATS[guessed_type]
    return DIRECT_MODEL_IMAGE_SUFFIX_FORMATS.get(path.suffix.lower())


def _attachment_local_path(attachment: Any) -> Path | None:
    local_path = None
    if hasattr(attachment, "local_path"):
        try:
            local_path = attachment.local_path()
        except Exception:
            local_path = None
    if not local_path:
        for ref in list(getattr(attachment, "storage_refs", None) or []):
            if getattr(ref, "storage_type", None) == "local" and getattr(
                ref, "path", None
            ):
                local_path = ref.path
                break
    if not local_path:
        return None
    return Path(str(local_path)).expanduser().resolve()


def _attachment_debug_summary(
    attachments: Any, *, max_items: int = 6
) -> list[dict[str, Any]]:
    summary: list[dict[str, Any]] = []
    values = list(attachments or [])
    for attachment in values[:max_items]:
        local_path = _attachment_local_path(attachment)
        local_exists = None
        local_size_bytes = None
        if local_path is not None:
            try:
                local_exists = local_path.exists() and local_path.is_file()
                local_size_bytes = local_path.stat().st_size if local_exists else None
            except OSError:
                local_exists = False
                local_size_bytes = None
        summary.append(
            {
                "attachment_id": str(getattr(attachment, "attachment_id", "") or ""),
                "filename": str(getattr(attachment, "filename", "") or ""),
                "origin": str(getattr(attachment, "origin", "") or ""),
                "kind": str(getattr(attachment, "kind", "") or ""),
                "content_type": str(getattr(attachment, "content_type", "") or ""),
                "size_bytes": getattr(attachment, "size_bytes", None),
                "sha256": str(getattr(attachment, "sha256", "") or ""),
                "caption_present": bool(getattr(attachment, "caption", None)),
                "storage_types": [
                    str(getattr(ref, "storage_type", "") or "")
                    for ref in list(getattr(attachment, "storage_refs", None) or [])
                ],
                "local_path": str(local_path) if local_path is not None else None,
                "local_exists": local_exists,
                "local_size_bytes": local_size_bytes,
            }
        )
    if len(values) > max_items:
        summary.append({"omitted_attachment_count": len(values) - max_items})
    return summary


def _strands_prompt_input_summary(value: str | list[ContentBlock]) -> dict[str, Any]:
    if isinstance(value, str):
        return {
            "input_type": "text",
            "text_chars": len(value),
            "block_count": 0,
            "image_block_count": 0,
        }
    image_block_count = 0
    text_chars = 0
    blocks: list[dict[str, Any]] = []
    for index, block in enumerate(value):
        if not isinstance(block, dict):
            blocks.append({"index": index, "type": type(block).__name__})
            continue
        if "text" in block:
            text = str(block.get("text") or "")
            text_chars += len(text)
            blocks.append({"index": index, "type": "text", "text_chars": len(text)})
            continue
        if "image" in block:
            image_block_count += 1
            image = block.get("image")
            source = (
                image.get("source")
                if isinstance(image, dict) and isinstance(image.get("source"), dict)
                else {}
            )
            data = source.get("bytes") if isinstance(source, dict) else None
            blocks.append(
                {
                    "index": index,
                    "type": "image",
                    "format": image.get("format") if isinstance(image, dict) else None,
                    "bytes": len(data)
                    if isinstance(data, (bytes, bytearray))
                    else None,
                    "sha256": hashlib.sha256(bytes(data)).hexdigest()
                    if isinstance(data, (bytes, bytearray))
                    else None,
                }
            )
            continue
        blocks.append(
            {
                "index": index,
                "type": "unknown",
                "keys": sorted(str(key) for key in block),
            }
        )
    return {
        "input_type": "content_blocks",
        "text_chars": text_chars,
        "block_count": len(value),
        "image_block_count": image_block_count,
        "blocks": blocks,
    }


def _provider_overload_error_summary(
    error: BaseException, *, max_chars: int = 900
) -> str:
    text = " ".join(str(error).split())
    if len(text) > max_chars:
        return text[: max_chars - 3] + "..."
    return text or "the provider reported an overload/rate-limit error"


def _provider_timeout_failure(error: BaseException) -> Optional[ProviderTimeoutError]:
    if not is_provider_timeout_error(error):
        return None
    return ProviderTimeoutError()


def _raise_provider_timeout_failure(error: BaseException) -> None:
    timeout_failure = _provider_timeout_failure(error)
    if timeout_failure is not None:
        raise timeout_failure from error


def _parsed_comms_routing_snapshot(
    comms_message: CommsMessage, header: MessageHeader
) -> Dict[str, Any]:
    content = comms_message.msg_content
    meta = comms_message.msg_meta
    snapshot: Dict[str, Any] = {
        "content_type": type(content).__name__,
        "resolved_to_routing_path": header.to_routing_path,
        "has_msg_meta": meta is not None,
    }
    if isinstance(content, Task):
        snapshot.update(
            {
                "task_id": content.task_id,
                "parent_task_id": content.parent_task_id,
                "task_type": str(content.task_type),
            }
        )
    if meta is not None:
        snapshot["msg_meta"] = {
            "to_named_agent_type": meta.to_named_agent_type,
            "to_agent_name": meta.to_agent_name,
            "is_response_requested": meta.is_response_requested,
            "requester_type": str(meta.requester_type) if meta.requester_type else None,
        }
    return snapshot


def _reasoning_summary_repair_notice(repair: ReasoningSummaryRepair) -> str:
    return (
        f"The model {repair.provider}/{repair.model_id} rejected the configured "
        f"reasoning_summary value {repair.requested_summary!r}. I updated "
        f"{repair.override_path} to ensure {repair.yaml_path} is null and retried the request. "
        "Reasoning summaries will remain disabled for this model until that model-specific override is changed."
    )


def _model_failover_notification_text(
    outcome: model_failover_runtime.ModelFailoverOutcome,
) -> str:
    if outcome.retry_inline:
        continuation = "is retrying the request"
    else:
        continuation = "will continue from the task's saved progress"
    return (
        f"Model failover: {outcome.from_candidate.key} could not complete this request, "
        f"so the system switched to {outcome.to_candidate.key} and {continuation}."
    )


def _model_failover_notification_id(
    *,
    task_id: Optional[str],
    outcome: model_failover_runtime.ModelFailoverOutcome,
) -> str:
    identity = "|".join(
        [
            str(task_id or ""),
            outcome.from_candidate.key,
            outcome.to_candidate.key,
        ]
    )
    return (
        f"model_provider_failover_{hashlib.sha256(identity.encode()).hexdigest()[:16]}"
    )


@dataclass
class _TurnState:
    """Invocation-local state; retained across provider recovery and SDK replacement."""

    msg_content: ContentType
    msg_header: MessageHeader
    msg_meta: MessageMeta
    prompt_content: ContentType = field(init=False)
    prompt_meta: MessageMeta = field(init=False)
    active_task_id: str | None = field(init=False)
    work_coordinator: Any = field(init=False)
    work_session: Any = field(init=False)
    turn_decision_model: type[BaseModel] = field(init=False)
    turn_decision_tool_name: str = field(init=False)
    context_request: ContextAssemblyRequest = field(init=False)
    assembly: AssembledContext = field(init=False)
    full_prompt: str = field(init=False)
    context_debug_dump_dir: Path | None = None
    result: Any = None
    content: ContentType | None = None
    after_action_report: Any = None
    invoke_kwargs: dict[str, Any] = field(init=False)
    tool_call_limit_handler: ToolCallLimitCallbackHandler | None = None
    partial_finalization_requested: bool = False
    runtime_instruction_parts: list[str] = field(init=False)
    invocation_started: float = field(init=False)
    span_attributes: dict[str, Any] = field(init=False)
    provider_retry_attempt: int = field(init=False)
    strands_invocation_attempt: int = field(init=False)
    reasoning_summary_repair_attempted: bool = field(init=False)
    # The turn's stop for its tool commands, from set_active_execution_context.
    tool_stop: ToolStop | None = None


class LLMTurnExecutor:
    """Owns turn preparation, invocation recovery, and result interpretation.

    SDK lifecycle and application effects remain explicit composition adapters.
    Neither this owner nor its operation bundles retain the UniversalAgent host.
    """

    def __init__(
        self,
        *,
        settings: Callable[[], TurnRuntimeSettings],
        model: Callable[[], Agent | None],
        context: Callable[[], ContextEngine | None],
        debug_dump: Callable[[], ContextDebugDumpPlugin | None],
        logger: logging.Logger,
        tasks: TurnTaskOperations,
        effects: TurnRuntimeEffects,
    ) -> None:
        self.read_settings = settings
        self.model = model
        self.context = context
        self.debug_dump = debug_dump
        self.logger = logger
        self.tasks = tasks
        self.effects = effects
        self.last_context_assembly: AssembledContext | None = None

    @property
    def settings(self) -> TurnRuntimeSettings:
        # Runtime profile recovery can replace the SDK/configuration during a turn.
        return self.read_settings()

    def _require_model(self) -> Agent:
        model = self.model()
        if model is None:
            raise RuntimeError("LLM turn requires an initialized Strands agent.")
        return model

    async def _end_tool_work_of_cancelled_turn(self, turn: _TurnState) -> None:
        """End the turn's tool work, wherever the stop landed.

        A stop can land after the model call returned (the late-guidance
        check, applying the result, a retry wait), where the invocation no
        longer ends the turn's tool work. Doing it twice is harmless.
        """

        if turn.tool_stop is not None:
            await stop_tool_calls(turn.tool_stop, self.logger, reason=TURN_CANCELLED_REASON)

    @staticmethod
    def _require_tool_stop(turn: _TurnState) -> ToolStop:
        if turn.tool_stop is None:
            raise RuntimeError("LLM turn requires the ToolStop set with its execution context.")
        return turn.tool_stop

    def _require_context(self) -> ContextEngine:
        context = self.context()
        if context is None:
            raise RuntimeError("LLM turn requires an initialized context engine.")
        return context

    async def _resolve_objective_work_context(
        self,
        msg_meta: MessageMeta,
        active_task_id: Optional[str],
    ) -> tuple[Any, Any]:
        coordinator = self.tasks.objective_work_coordinator
        if coordinator is None:
            return None, None
        session = await self.tasks.runtime.run(
            coordinator.session_for_meta,
            getattr(msg_meta, "objective_work_session_id", None),
        )
        if session is None:
            session = await self.tasks.runtime.run(
                coordinator.session_for_task, active_task_id
            )
        return coordinator, session

    def _uses_compatible_turn_decision_schema(self) -> bool:
        return uses_litellm_compatible_schema(
            provider=self.settings.model_provider,
            model_id=self.settings.model_id,
            api_base=self.settings.litellm_api_base,
        )

    def _turn_decision_structured_output_model(self) -> type[BaseModel]:
        # Only TeamArchitect may ask the person for anything, so specialists
        # get no credential-form or signup actions and escalate instead.
        worker = not self.settings.is_team_lead
        if self._uses_compatible_turn_decision_schema():
            return CompatibleWorkerTurnDecision if worker else CompatibleModelAgentTurnDecision
        return ModelWorkerTurnDecision if worker else ModelAgentTurnDecision

    def _turn_decision_structured_output_prompt(self) -> str:
        # Recovery must name the same tool and actions as the provider schema.
        return {
            ModelAgentTurnDecision: MODEL_AGENT_TURN_DECISION_FORCE_PROMPT,
            ModelWorkerTurnDecision: MODEL_WORKER_TURN_DECISION_FORCE_PROMPT,
            CompatibleModelAgentTurnDecision: COMPATIBLE_MODEL_AGENT_TURN_DECISION_FORCE_PROMPT,
            CompatibleWorkerTurnDecision: COMPATIBLE_WORKER_TURN_DECISION_FORCE_PROMPT,
        }[self._turn_decision_structured_output_model()]

    def _append_provider_prompt_suffix(
        self,
        assembly: AssembledContext,
        *,
        text: str,
        name: str,
        source: str,
    ) -> AssembledContext:
        """Extend an assembly while keeping provider-facing accounting exact."""

        suffix = str(text or "")
        if not suffix:
            self.last_context_assembly = assembly
            return assembly

        prompt = f"{assembly.prompt}\n\n{suffix}" if assembly.prompt else suffix
        estimator = getattr(self.context(), "estimate_tokens", None)
        if callable(estimator):
            previous_prompt_tokens = int(estimator(assembly.prompt))
            prompt_tokens = int(estimator(prompt))
            suffix_tokens = int(estimator(suffix))
        else:  # pragma: no cover - context engines expose token estimation
            previous_prompt_tokens = max(1, len(assembly.prompt) // 4)
            prompt_tokens = max(1, len(prompt) // 4)
            suffix_tokens = max(1, len(suffix) // 4)

        estimated_tokens = max(
            0,
            int(assembly.estimated_tokens) - previous_prompt_tokens + prompt_tokens,
        )
        section = ContextSection(
            name=name,
            text=suffix,
            priority=120,
            pinned=True,
            source=source,
            char_count=len(suffix),
            estimated_tokens=suffix_tokens,
        )
        source_length = {
            "name": section.name,
            "source": section.source,
            "chars": section.char_count,
            "estimated_tokens": section.estimated_tokens,
            "pinned": section.pinned,
            "plugin_id": section.plugin_id,
            "scope_kind": section.scope_kind,
            "degradable": section.degradable,
        }
        hard_limit = assembly.hard_limit_tokens
        fill_ratio = round(estimated_tokens / hard_limit, 4) if hard_limit else None
        warnings = list(assembly.warnings)
        if hard_limit is not None and estimated_tokens > hard_limit:
            warnings.append(
                {
                    "warning": "context_hard_limit_exceeded_by_runtime_suffix",
                    "section": name,
                    "estimated_tokens": estimated_tokens,
                    "hard_limit_tokens": hard_limit,
                }
            )
            self.logger.warning(
                "[%s] Runtime prompt suffix crossed the context hard limit: section=%s estimated_tokens=%s hard_limit_tokens=%s.",
                self.settings.id,
                name,
                estimated_tokens,
                hard_limit,
            )

        updated = replace(
            assembly,
            sections=[*assembly.sections, section],
            prompt=prompt,
            char_count=max(
                0, int(assembly.char_count) - len(assembly.prompt) + len(prompt)
            ),
            estimated_tokens=estimated_tokens,
            source_lengths=[*assembly.source_lengths, source_length],
            warnings=warnings,
            context_fill_ratio=fill_ratio,
        )
        self.last_context_assembly = updated
        return updated

    def _terminal_reasoning_enabled(self) -> bool:
        debug_config = self.settings.debug or {}
        if not isinstance(debug_config, dict):
            return False
        terminal_reasoning = debug_config.get("terminal_reasoning") or {}
        if not isinstance(terminal_reasoning, dict):
            return False
        return bool(terminal_reasoning.get("enabled", False))

    def _terminal_reasoning_model_diagnostics(self) -> dict[str, Any]:
        model = getattr(self.model(), "model", None)
        diagnostics: dict[str, Any] = {
            "model_adapter": type(model).__name__ if model is not None else None,
            "model_config_model_id": None,
            "model_config_stream": None,
            "model_config_reasoning_effort": None,
            "model_config_reasoning_summary": None,
            "model_config_params": {},
        }
        get_config = getattr(model, "get_config", None)
        if not callable(get_config):
            return diagnostics
        try:
            model_config = get_config() or {}
        except Exception as exc:
            diagnostics["model_config_error"] = f"{type(exc).__name__}: {exc}"
            return diagnostics
        if not isinstance(model_config, dict):
            diagnostics["model_config_type"] = type(model_config).__name__
            return diagnostics

        params = model_config.get("params") or {}
        safe_params: dict[str, Any] = {}
        if isinstance(params, dict):
            for key, value in params.items():
                normalized_key = str(key).lower()
                if any(
                    secret_token in normalized_key
                    for secret_token in ("key", "token", "secret", "password")
                ):
                    safe_params[str(key)] = "[redacted]"
                else:
                    safe_params[str(key)] = value

        reasoning_effort = safe_params.get("reasoning_effort")
        reasoning_config = safe_params.get("reasoning")
        if reasoning_effort is None and isinstance(reasoning_config, dict):
            reasoning_effort = reasoning_config.get("effort")
        reasoning_summary = (
            reasoning_config.get("summary")
            if isinstance(reasoning_config, dict)
            else None
        )

        diagnostics.update(
            {
                "model_config_model_id": model_config.get("model_id"),
                "model_config_stream": safe_params.get("stream"),
                "model_config_reasoning_effort": reasoning_effort,
                "model_config_reasoning_summary": reasoning_summary,
                "model_config_params": safe_params,
            }
        )
        return diagnostics

    def _collect_direct_model_image_blocks(
        self, msg_content: ContentType
    ) -> tuple[list[ContentBlock], list[dict[str, Any]]]:
        if self.settings.supports_image_input is not True:
            return [], []
        blocks: list[ContentBlock] = []
        diagnostics: list[dict[str, Any]] = []
        for attachment in list(getattr(msg_content, "attachments", None) or []):
            attachment_id = str(getattr(attachment, "attachment_id", "") or "")
            filename = str(
                getattr(attachment, "filename", "") or attachment_id or "image"
            )
            kind = str(getattr(attachment, "kind", "") or "").lower()
            content_type = (
                str(getattr(attachment, "content_type", "") or "")
                .split(";", 1)[0]
                .strip()
                .lower()
            )
            if kind != "image" and not content_type.startswith("image/"):
                continue
            path = _attachment_local_path(attachment)
            if path is None:
                diagnostics.append(
                    {
                        "attachment_id": attachment_id,
                        "filename": filename,
                        "status": "skipped",
                        "reason": "no_local_path",
                    }
                )
                continue
            image_format = _attachment_image_format(attachment, path)
            if image_format is None:
                diagnostics.append(
                    {
                        "attachment_id": attachment_id,
                        "filename": filename,
                        "status": "skipped",
                        "reason": "unsupported_image_format",
                        "content_type": content_type,
                    }
                )
                continue
            if not path.exists() or not path.is_file():
                diagnostics.append(
                    {
                        "attachment_id": attachment_id,
                        "filename": filename,
                        "status": "skipped",
                        "reason": "local_path_unavailable",
                        "path": str(path),
                    }
                )
                continue
            size_bytes = path.stat().st_size
            if size_bytes > DIRECT_MODEL_IMAGE_MAX_BYTES:
                diagnostics.append(
                    {
                        "attachment_id": attachment_id,
                        "filename": filename,
                        "status": "skipped",
                        "reason": "image_too_large_for_direct_model_input",
                        "size_bytes": size_bytes,
                        "max_bytes": DIRECT_MODEL_IMAGE_MAX_BYTES,
                    }
                )
                continue
            image_bytes = path.read_bytes()
            caption = str(getattr(attachment, "caption", "") or "").strip()
            label = (
                f"Direct image attachment: filename={filename}; attachment_id={attachment_id}; "
                f"content_type={content_type or 'unknown'}; size_bytes={size_bytes}."
            )
            if caption:
                label += f" Caption: {caption}"
            blocks.append({"text": label})
            blocks.append(
                {"image": {"format": image_format, "source": {"bytes": image_bytes}}}
            )
            diagnostics.append(
                {
                    "attachment_id": attachment_id,
                    "filename": filename,
                    "status": "included",
                    "format": image_format,
                    "size_bytes": size_bytes,
                    "sha256": hashlib.sha256(image_bytes).hexdigest(),
                }
            )
        return blocks, diagnostics

    def _build_strands_prompt_input(
        self, full_prompt: str, msg_content: ContentType
    ) -> str | list[ContentBlock]:
        attachments = list(getattr(msg_content, "attachments", None) or [])
        if attachments:
            has_image_attachment = any(
                str(getattr(attachment, "kind", "") or "").lower() == "image"
                or str(getattr(attachment, "content_type", "") or "")
                .lower()
                .startswith("image/")
                for attachment in attachments
            )
            self.logger.info(
                "[%s] Model input attachment bridge: supports_image_input=%s attachment_count=%d attachment_summary=%s",
                self.settings.id,
                self.settings.supports_image_input,
                len(attachments),
                _compact_debug_preview(
                    _attachment_debug_summary(attachments), max_chars=1600
                ),
            )
            if has_image_attachment and self.settings.supports_image_input is not True:
                self.logger.warning(
                    "[%s] Image attachment(s) are metadata-only because supports_image_input=%s for model_provider=%s model_id=%s.",
                    self.settings.id,
                    self.settings.supports_image_input,
                    self.settings.model_provider,
                    self.settings.model_id,
                )
        image_blocks, diagnostics = self._collect_direct_model_image_blocks(msg_content)
        included_count = sum(
            1 for item in diagnostics if item.get("status") == "included"
        )
        skipped = [item for item in diagnostics if item.get("status") == "skipped"]
        if diagnostics:
            self.logger.info(
                "[%s] Direct model image input attachments: included=%s skipped=%s diagnostics=%s",
                self.settings.id,
                included_count,
                len(skipped),
                _compact_debug_preview(diagnostics, max_chars=1200),
            )
        if not image_blocks:
            return full_prompt
        prompt_text = (
            full_prompt
            + "\n\nRuntime multimodal input: "
            + f"{included_count} image attachment(s) are included as native image content blocks after this text. "
            + "Use those image blocks directly when answering questions about the attached image(s). "
            + "Use inspect_user_attachment only when you need attachment metadata, text extraction, or fallback inspection."
        )
        return [{"text": prompt_text}, *image_blocks]

    def _write_strands_prompt_input_debug_dump(
        self,
        *,
        request: ContextAssemblyRequest,
        assembly: AssembledContext,
        strands_prompt_input: str | list[ContentBlock],
        attempt: int,
    ) -> None:
        plugin = self.debug_dump()
        if plugin is None or not getattr(plugin, "enabled", False):
            return
        dump = getattr(plugin, "dump_strands_prompt_input", None)
        if not callable(dump):
            return
        dump(
            request,
            assembly,
            strands_prompt_input=strands_prompt_input,
            attempt=attempt,
        )

    def _context_debug_agent_metadata(self) -> Dict[str, Any]:
        debug_plugin = self.debug_dump()
        return {
            "agent_id": self.settings.id,
            "agent_name": self.settings.name,
            "team_id": self.settings.team_id,
            "named_agent_type": self.settings.named_agent_type,
            "workspace_dir": str(self.settings.workspace_dir or ""),
            "instance_config_path": self.settings.instance_config_path,
            "system_prompt": self.settings.system_prompt or "",
            "model_provider": self.settings.model_provider,
            "model_id": self.settings.model_id,
            "context_dump_mode": (
                (self.settings.debug or {}).get("context_dump") or {}
            ).get("mode"),
            "model_call_observer": dict(
                getattr(debug_plugin, "model_call_observer_status", {}) or {}
            ),
        }

    def _context_debug_task_history_summary(
        self, msg_content: ContentType
    ) -> Optional[str]:
        if not isinstance(msg_content, Task):
            return None
        try:
            return self.tasks.summarize_task_history_markdown(
                task_id=msg_content.task_id, include_execution_digest=True
            )
        except Exception as exc:
            self.logger.debug(
                "[%s] Failed to build context-debug task history summary for task %s: %s",
                self.settings.id,
                msg_content.task_id,
                exc,
            )
            return None

    def _context_debug_strands_messages(self) -> list[dict[str, Any]]:
        raw_messages = list(getattr(self.model(), "messages", []) or [])
        normalized: list[dict[str, Any]] = []
        for message in raw_messages:
            if hasattr(message, "model_dump"):
                try:
                    normalized.append(message.model_dump(mode="json"))
                    continue
                except Exception:
                    pass
            if isinstance(message, dict):
                normalized.append(message)
            else:
                normalized.append({"message": repr(message)})
        return normalized

    def _context_debug_reasoning_artifacts(
        self,
        *,
        request: ContextAssemblyRequest,
        assembly: AssembledContext,
        strands_messages: list[dict[str, Any]],
        after_action_report: Any = None,
    ) -> Dict[str, Any]:
        execution_digest = getattr(request.incoming_content, "execution_digest", None)
        if execution_digest is not None and hasattr(execution_digest, "model_dump"):
            execution_digest_payload = execution_digest.model_dump(mode="json")
        else:
            execution_digest_payload = execution_digest
        return {
            "note": (
                "This file contains observable runtime reasoning artifacts only. "
                "It does not contain hidden model chain-of-thought."
            ),
            "context_warnings": assembly.warnings,
            "execution_digest": execution_digest_payload,
            "after_action_report": after_action_report,
            "tool_trace": self._context_debug_tool_trace(strands_messages),
        }

    def _context_debug_tool_trace(
        self, strands_messages: list[dict[str, Any]]
    ) -> list[dict[str, Any]]:
        trace: list[dict[str, Any]] = []
        for message_index, message in enumerate(strands_messages):
            self._collect_context_debug_tool_trace(
                message,
                trace=trace,
                message_index=message_index,
            )
        return trace

    def _collect_context_debug_tool_trace(
        self,
        value: Any,
        *,
        trace: list[dict[str, Any]],
        message_index: int,
    ) -> None:
        if isinstance(value, dict):
            tool_use = value.get("toolUse")
            if isinstance(tool_use, dict):
                trace.append(
                    {
                        "event": "tool_use",
                        "message_index": message_index,
                        "tool_use_id": tool_use.get("toolUseId"),
                        "tool_name": tool_use.get("name"),
                        "input_preview": _compact_debug_preview(
                            tool_use.get("input"), max_chars=2000
                        ),
                    }
                )
            tool_result = value.get("toolResult")
            if isinstance(tool_result, dict):
                trace.append(
                    {
                        "event": "tool_result",
                        "message_index": message_index,
                        "tool_use_id": tool_result.get("toolUseId"),
                        "status": tool_result.get("status"),
                        "content_item_count": len(tool_result.get("content") or []),
                    }
                )
            for child in value.values():
                self._collect_context_debug_tool_trace(
                    child, trace=trace, message_index=message_index
                )
        elif isinstance(value, list):
            for child in value:
                self._collect_context_debug_tool_trace(
                    child, trace=trace, message_index=message_index
                )

    async def _write_context_debug_post_turn(
        self,
        *,
        request: ContextAssemblyRequest,
        assembly: AssembledContext,
        dump_dir: Optional[Path],
        result: Any = None,
        parsed_content: Any = None,
        after_action_report: Any = None,
        error: BaseException | None = None,
    ) -> Optional[Path]:
        del dump_dir
        context = self.context()
        if context is None:
            return None
        try:
            # SDK results and provider exceptions are not task records and may
            # contain non-copyable handles. The adapter retains their read-only
            # custody until this awaited callback finishes (including cancellation).
            post_invocation = partial(
                context.post_invocation, result=result, error=error
            )
            await self.tasks.runtime.run(
                post_invocation,
                request=request,
                assembly=assembly,
                parsed_content=parsed_content,
                after_action_report=after_action_report,
            )
        except Exception as exc:
            self.logger.warning(
                "[%s] Failed to write post-turn context debug dump: %s",
                self.settings.id,
                exc,
            )
        return None

    def _should_recover_context_overflow(self) -> bool:
        config = merge_context_engine_config(self.settings.context_engine)
        if (
            str(config.get("hard_limit_behavior") or "").strip().lower()
            != "recover_then_error"
        ):
            return False
        recovery = config.get("overflow_recovery") or {}
        if not isinstance(recovery, dict):
            recovery = {}
        return bool(recovery.get("enabled", True))

    def _context_overflow_degrade_sections(self) -> tuple[str, ...]:
        config = merge_context_engine_config(self.settings.context_engine)
        recovery = config.get("overflow_recovery") or {}
        if not isinstance(recovery, dict):
            recovery = {}
        sections = recovery.get("degrade_sections")
        if not isinstance(sections, list):
            sections = [
                "task_runtime_timing",
                "domain_topic_recall",
                "knowledge_recall",
                "task_history",
                "workspace_user_context",
                "instance_delivery_context",
            ]
        return tuple(str(section) for section in sections if str(section).strip())

    def _max_context_recovery_attempts(self) -> int:
        config = merge_context_engine_config(self.settings.context_engine)
        recovery = config.get("overflow_recovery") or {}
        if not isinstance(recovery, dict):
            recovery = {}
        try:
            return max(1, min(int(recovery.get("max_preflight_attempts", 2)), 4))
        except (TypeError, ValueError):
            return 2

    def _max_provider_context_retry_attempts(self) -> int:
        config = merge_context_engine_config(self.settings.context_engine)
        recovery = config.get("overflow_recovery") or {}
        if not isinstance(recovery, dict):
            recovery = {}
        try:
            return max(0, min(int(recovery.get("max_provider_retry_attempts", 1)), 2))
        except (TypeError, ValueError):
            return 1

    def _is_provider_context_overflow_error(self, error: BaseException) -> bool:
        text = str(error).lower()
        markers = [
            "input length",
            "context length",
            "maximum context",
            "maximum allowed length",
            "prompt is too long",
            "too many tokens",
            "context_length_exceeded",
            "request too large",
            "413",
        ]
        return any(marker in text for marker in markers)

    def _is_provider_overload_error(self, error: BaseException) -> bool:
        return is_provider_overload_error(error)

    def _is_provider_connectivity_error(self, error: BaseException) -> bool:
        return is_provider_connectivity_error(error)

    def _configured_provider_endpoint_host(self) -> Optional[str]:
        if str(self.settings.model_provider or "").strip().lower() != "litellm":
            return None
        return api_base_hostname(self.settings.litellm_api_base)

    def _provider_connectivity_failure(
        self, _error: BaseException
    ) -> ProviderConnectivityError:
        provider = str(self.settings.model_provider or "unknown")
        model_id = str(self.settings.model_id or "unknown")
        message = (
            f"Model provider connectivity/configuration failure for {provider}/{model_id}: "
            "the model request failed while establishing the provider connection."
        )
        if provider.strip().lower() == "litellm":
            message += " Check llm.litellm_api_base and any LITELLM_API_BASE override from inside the UA runtime."
            endpoint_host = self._configured_provider_endpoint_host()
            if endpoint_host in {"localhost", "127.0.0.1", "::1"}:
                message += " The endpoint uses localhost; in Docker, localhost is the UA container itself."
        return ProviderConnectivityError(message)

    async def _record_context_overflow_recovery_event(
        self,
        *,
        task_id: Optional[str],
        attempt: int,
        error: ContextBudgetExceededError,
        compaction_result: Any = None,
        degraded_sections: tuple[str, ...] = (),
    ) -> None:
        if not task_id:
            return
        largest_sources = sorted(
            list(getattr(error, "source_lengths", []) or []),
            key=lambda item: int(
                item.get("estimated_tokens") or item.get("tokens") or 0
            ),
            reverse=True,
        )[:8]
        details = {
            "reason": "context_overflow_recovery",
            "attempt": attempt,
            "estimated_tokens": getattr(error, "estimated_tokens", None),
            "hard_limit_tokens": getattr(error, "hard_limit_tokens", None),
            "max_context_tokens": getattr(error, "max_context_tokens", None),
            "reserved_output_tokens": getattr(error, "reserved_output_tokens", None),
            "largest_sources": largest_sources,
            "degraded_sections": list(degraded_sections),
            "context_epoch_id": getattr(compaction_result, "context_epoch_id", None),
            "through_turn_index": getattr(
                compaction_result, "through_turn_index", None
            ),
        }
        await self.tasks.runtime.run(
            self.tasks.append_history_entry,
            task_id,
            "Context overflow recovery attempted bounded compaction"
            + (
                f" and degraded sections {list(degraded_sections)}"
                if degraded_sections
                else ""
            )
            + ".",
            entry_type=TaskHistoryType.ERROR_EVENT,
            importance=TaskHistoryImportance.HIGH,
            details=details,
            agent_name=self.settings.name,
            agent_id=self.settings.id,
        )

    async def _record_provider_context_retry_event(
        self,
        *,
        task_id: Optional[str],
        attempt: int,
        error: BaseException,
        assembly: AssembledContext,
        compaction_result: Any = None,
        degraded_sections: tuple[str, ...] = (),
        total_recovery_attempt: Optional[int] = None,
        max_total_recovery_attempts: Optional[int] = None,
    ) -> None:
        if not task_id:
            return
        largest_sources = sorted(
            list(getattr(assembly, "source_lengths", []) or []),
            key=lambda item: int(
                item.get("estimated_tokens") or item.get("tokens") or 0
            ),
            reverse=True,
        )[:8]
        await self.tasks.runtime.run(
            self.tasks.append_history_entry,
            task_id,
            "Provider rejected model input as too large; retrying after context epoch recovery.",
            entry_type=TaskHistoryType.ERROR_EVENT,
            importance=TaskHistoryImportance.HIGH,
            details={
                "reason": "provider_context_overflow_retry",
                "attempt": attempt,
                "total_recovery_attempt": total_recovery_attempt,
                "max_total_recovery_attempts": max_total_recovery_attempts,
                "error_preview": _compact_debug_preview(str(error), max_chars=1600),
                "estimated_tokens": getattr(assembly, "estimated_tokens", None),
                "hard_limit_tokens": getattr(assembly, "hard_limit_tokens", None),
                "context_epoch_id": getattr(
                    compaction_result, "context_epoch_id", None
                ),
                "degraded_sections": list(degraded_sections),
                "largest_sources": largest_sources,
            },
            agent_name=self.settings.name,
            agent_id=self.settings.id,
        )

    async def _record_provider_context_retry_suppressed_event(
        self,
        *,
        task_id: Optional[str],
        error: BaseException,
        decision: ProviderContextRetryDecision,
    ) -> None:
        """Record why UA refused to restart a rejected provider call."""

        recovery_attempt = decision.recovery_attempt
        details = {
            "reason": "provider_context_overflow_retry_suppressed",
            "suppression_reason": decision.reason,
            "error_preview": _compact_debug_preview(str(error), max_chars=1600),
            "total_recovery_attempt": getattr(recovery_attempt, "attempt", None),
            "max_total_recovery_attempts": getattr(
                recovery_attempt, "max_attempts", None
            ),
        }
        self.logger.warning(
            "[%s] event=provider_context_overflow_retry_suppressed task_id=%s reason=%s total_recovery_attempt=%s max_total_recovery_attempts=%s",
            self.settings.id,
            task_id or "unknown",
            decision.reason,
            details["total_recovery_attempt"],
            details["max_total_recovery_attempts"],
        )
        if not task_id:
            return
        await self.tasks.runtime.run(
            self.tasks.append_history_entry,
            task_id,
            "Provider context-overflow retry was suppressed to avoid an unsafe or unbounded reinvocation.",
            entry_type=TaskHistoryType.ERROR_EVENT,
            importance=TaskHistoryImportance.HIGH,
            details=details,
            agent_name=self.settings.name,
            agent_id=self.settings.id,
        )

    async def _resolve_provider_context_retry(
        self,
        *,
        task_id: Optional[str],
        error: BaseException,
        invocation_state: Dict[str, Any],
        provider_retry_attempt: int,
        tool_call_limit_handler: Any,
    ) -> tuple[bool, bool, ProviderContextRetryDecision]:
        """Resolve context retry policy and fail immediately for a handled denial."""

        tool_calls_started = self._model_failover_tool_activity_started(
            tool_call_limit_handler
        )
        decision = decide_provider_context_retry(
            invocation_state,
            recovery_enabled=self._should_recover_context_overflow(),
            is_context_overflow=self._is_provider_context_overflow_error(error),
            tool_calls_started=tool_calls_started,
            provider_retry_attempt=provider_retry_attempt,
            max_provider_retry_attempts=self._max_provider_context_retry_attempts(),
            max_total_attempts=max_total_recovery_attempts(
                self.settings.inner_loop_context
            ),
        )
        if decision.handled and not decision.allowed:
            await self._record_provider_context_retry_suppressed_event(
                task_id=task_id, error=error, decision=decision
            )
            raise error
        return decision.allowed, tool_calls_started, decision

    async def _assemble_context_with_recovery(
        self,
        *,
        request: ContextAssemblyRequest,
        original_content: ContentType,
    ) -> AssembledContext:
        if self.context() is None:
            self.effects.create_context_engine()
        try:
            return await self.tasks.runtime.run(
                self._require_context().assemble, request
            )
        except ContextBudgetExceededError as initial_error:
            if not self._should_recover_context_overflow():
                raise
            last_error: ContextBudgetExceededError = initial_error
            history_task_id = self.effects.history_task_id_for_content(original_content)
            max_attempts = self._max_context_recovery_attempts()
            configured_degraded_sections = self._context_overflow_degrade_sections()
            for attempt in range(1, max_attempts + 1):
                compaction_result = None
                try:
                    compaction_result = await self.tasks.runtime.run(
                        self._require_context().compact,
                        ContextCompactionReason.OVERFLOW_RECOVERY,
                        request,
                    )
                except Exception as exc:
                    self.logger.warning(
                        "[%s] Context overflow compaction attempt failed: %s",
                        self.settings.id,
                        exc,
                    )

                degraded_sections: tuple[str, ...] = ()
                if attempt > 1 or not getattr(compaction_result, "compacted", False):
                    degraded_sections = configured_degraded_sections

                await self._record_context_overflow_recovery_event(
                    task_id=history_task_id,
                    attempt=attempt,
                    error=last_error,
                    compaction_result=compaction_result,
                    degraded_sections=degraded_sections,
                )
                retry_request = replace(
                    request,
                    recovery_attempt=attempt,
                    degraded_sections=degraded_sections,
                )
                try:
                    assembly = await self.tasks.runtime.run(
                        self._require_context().assemble, retry_request
                    )
                    assembly.warnings.append(
                        {
                            "warning": "context_overflow_recovered",
                            "attempt": attempt,
                            "context_epoch_id": getattr(
                                compaction_result, "context_epoch_id", None
                            ),
                            "degraded_sections": list(degraded_sections),
                        }
                    )
                    return assembly
                except ContextBudgetExceededError as retry_error:
                    last_error = retry_error
                    continue
            raise last_error

    def _take_optional_strands_snapshot(self) -> Any:
        config = merge_context_engine_config(self.settings.context_engine)
        snapshot_config = config.get("strands_state_snapshots") or {}
        if not isinstance(snapshot_config, dict):
            snapshot_config = {}
        enabled = snapshot_config.get("enabled", "auto")
        if enabled is False or str(enabled).strip().lower() in {
            "false",
            "off",
            "disabled",
        }:
            return None
        agent = self.model()
        take_snapshot = getattr(agent, "take_snapshot", None)
        if not callable(take_snapshot):
            return None
        try:
            return take_snapshot()
        except Exception as exc:
            self.logger.debug(
                "[%s] Optional Strands snapshot failed: %s", self.settings.id, exc
            )
            return None

    def _extract_comms_message(self, result: AgentResult) -> CommsMessage:
        """
        Extract a validated CommsMessage from a Strands AgentResult.

        Structured output is required; raw text is not a transport contract.
        """
        structured_output = getattr(result, "structured_output", None)
        if structured_output is not None:
            if isinstance(structured_output, CommsMessage):
                return structured_output
            if isinstance(structured_output, BaseModel):
                return CommsMessage.model_validate(structured_output.model_dump())
            return CommsMessage.model_validate(structured_output)

        raise RuntimeError("Strands result did not include structured_output.")

    def _structured_output_fallback_details(
        self, result: AgentResult
    ) -> Optional[Dict[str, Any]]:
        state = getattr(result, "state", None)
        if not isinstance(state, dict):
            return None
        details = state.get(RuntimeGuardrailsPlugin._STRUCTURED_FALLBACK_KEY)
        return details if isinstance(details, dict) else None

    def _structured_output_input_details(
        self, result: AgentResult
    ) -> Optional[Dict[str, Any]]:
        state = getattr(result, "state", None)
        if not isinstance(state, dict):
            return None
        details = state.get(RuntimeGuardrailsPlugin._STRUCTURED_LAST_INPUT_KEY)
        return details if isinstance(details, dict) else None

    def _apply_agent_result_transformer(
        self,
        incoming_content: ContentType,
        result: AgentResult,
    ) -> AgentResult:
        return self.effects.transform_result(incoming_content, result)

    def _model_failover_tool_activity_started(
        self,
        tool_call_limit_handler: Optional[ToolCallLimitCallbackHandler],
    ) -> bool:
        if (
            tool_call_limit_handler is not None
            and tool_call_limit_handler.tool_sequence
        ):
            return True
        messages = list(getattr(self.model(), "messages", []) or [])
        for message in messages[-12:]:
            if not isinstance(message, dict):
                continue
            content = message.get("content")
            if not isinstance(content, list):
                continue
            for block in content:
                if isinstance(block, dict) and (
                    "toolUse" in block or "toolResult" in block
                ):
                    return True
        return False

    async def _record_reasoning_summary_repair_event(
        self,
        *,
        task_id: Optional[str],
        repair: ReasoningSummaryRepair,
        error: BaseException,
    ) -> None:
        self.logger.warning(
            "[%s] event=reasoning_summary_unsupported_repaired task_id=%s provider=%s model_id=%s requested_summary=%s override_path=%s yaml_path=%s backup_path=%s changed=%s action=retry_with_null",
            self.settings.id,
            task_id or "unknown",
            repair.provider,
            repair.model_id,
            repair.requested_summary,
            repair.override_path,
            repair.yaml_path,
            repair.backup_path,
            repair.changed,
        )
        if not task_id:
            return
        await self.tasks.runtime.run(
            self.tasks.append_history_entry,
            task_id,
            f"The provider rejected reasoning_summary={repair.requested_summary!r} for {repair.provider}/{repair.model_id}; the runtime ensured {repair.yaml_path}=null and is retrying the model call.",
            entry_type=TaskHistoryType.ERROR_EVENT,
            importance=TaskHistoryImportance.HIGH,
            details={
                "event": "reasoning_summary_unsupported_repaired",
                "provider": repair.provider,
                "model_id": repair.model_id,
                "requested_summary": repair.requested_summary,
                "override_path": str(repair.override_path),
                "yaml_path": repair.yaml_path,
                "backup_path": str(repair.backup_path) if repair.backup_path else None,
                "changed": repair.changed,
                "error_preview": _compact_debug_preview(str(error), max_chars=1600),
            },
            agent_name=self.settings.name,
            agent_id=self.settings.id,
        )

    async def _notify_user_of_reasoning_summary_repair(
        self,
        *,
        repair: ReasoningSummaryRepair,
        task_id: Optional[str],
        msg_header: MessageHeader,
        msg_meta: MessageMeta,
    ) -> bool:
        routing_key = str(getattr(msg_header, "response_routing_key", "") or "").strip()
        gateway_context = getattr(msg_meta, "gateway_context", None)
        send_message = self.effects.publish
        if not routing_key or gateway_context is None or not callable(send_message):
            self.logger.warning(
                "[%s] event=reasoning_summary_repair_notification_deferred task_id=%s reason=missing_gateway_route_or_context",
                self.settings.id,
                task_id or "unknown",
            )
            return False

        identity = "|".join(
            [
                str(task_id or ""),
                repair.provider,
                repair.model_id,
                repair.requested_summary,
                str(repair.override_path),
            ]
        )
        notification_id = f"reasoning_summary_repair_{hashlib.sha256(identity.encode()).hexdigest()[:16]}"
        gateway_context_copy = (
            gateway_context.model_copy(deep=True)
            if hasattr(gateway_context, "model_copy")
            else gateway_context
        )
        envelope = MessageEnvelope(
            msg_content=UserNotification(
                notification_id=notification_id,
                text=_reasoning_summary_repair_notice(repair),
                urgency="normal",
                reason="reasoning_summary_unsupported_repaired",
                source_task_id=task_id,
                source_agent_id=self.settings.id,
                source_agent_name=self.settings.name,
            ),
            msg_meta=MessageMeta(
                from_agent_name=self.settings.name,
                msg_correlation_id=getattr(msg_meta, "msg_correlation_id", None),
                conversation_id=getattr(msg_meta, "conversation_id", None),
                followup_to_task_id=task_id,
                objective_id=getattr(msg_meta, "objective_id", None),
                request_timezone=getattr(msg_meta, "request_timezone", None),
                requester_type=RequesterType.AGENT_REQUESTER,
                is_response_requested=False,
                gateway_context=gateway_context_copy,
            ),
            msg_header=MessageHeader(
                from_agent_id=self.settings.id,
                to_routing_path=routing_key,
                response_routing_key=self.settings.id,
                idempotency_key=notification_id,
                agent_signature="",
            ),
        )
        try:
            await send_message(message=envelope, routing_key=routing_key)
            return True
        except Exception as exc:
            self.logger.warning(
                "[%s] event=reasoning_summary_repair_notification_failed task_id=%s error_type=%s error=%s",
                self.settings.id,
                task_id or "unknown",
                type(exc).__name__,
                exc,
            )
            return False

    def _configured_gateway_response_route(self: Any) -> Optional[str]:
        instance_config_path = str(self.settings.instance_config_path or "").strip()
        if not instance_config_path:
            return None
        try:
            from assistant_gateway.config import GatewayInstanceConfig

            config = GatewayInstanceConfig.load_for_process(instance_config_path)
        except Exception as exc:
            self.logger.warning(
                "[%s] event=model_provider_failover_notification_route_failed instance_config_path=%s error_type=%s error=%s",
                self.settings.id,
                instance_config_path,
                type(exc).__name__,
                exc,
            )
            return None
        return str(config.manager.response_queue_name or "").strip() or None

    def _model_failover_notification_route(
        self,
        *,
        msg_content: ContentType,
        msg_header: MessageHeader,
    ) -> Optional[str]:
        if isinstance(msg_content, Task) and msg_content.parent_task_id is None:
            direct_route = str(
                getattr(msg_header, "response_routing_key", "") or ""
            ).strip()
            if direct_route:
                return direct_route
        return self._configured_gateway_response_route()

    async def _notify_user_of_model_failover(
        self,
        *,
        outcome: model_failover_runtime.ModelFailoverOutcome,
        task_id: Optional[str],
        msg_content: ContentType,
        msg_header: MessageHeader,
        msg_meta: MessageMeta,
    ) -> bool:
        if not model_failover_runtime.is_provider_failover_outcome(outcome):
            return False
        if not self.effects.should_notify_user():
            return False

        gateway_context = getattr(msg_meta, "gateway_context", None)
        routing_key = self._model_failover_notification_route(
            msg_content=msg_content, msg_header=msg_header
        )
        send_message = self.effects.publish
        if gateway_context is None or not routing_key or not callable(send_message):
            self.logger.warning(
                "[%s] event=model_provider_failover_notification_deferred task_id=%s from_model=%s to_model=%s reason=missing_gateway_context_route_or_transport",
                self.settings.id,
                task_id or "unknown",
                outcome.from_candidate.key,
                outcome.to_candidate.key,
            )
            return False

        notification_id = _model_failover_notification_id(
            task_id=task_id,
            outcome=outcome,
        )
        gateway_context_copy = (
            gateway_context.model_copy(deep=True)
            if hasattr(gateway_context, "model_copy")
            else gateway_context
        )
        envelope = MessageEnvelope(
            msg_content=UserNotification(
                notification_id=notification_id,
                text=_model_failover_notification_text(outcome),
                urgency="normal",
                reason=MODEL_PROVIDER_FAILOVER_NOTIFICATION_REASON,
                source_task_id=task_id,
                source_agent_id=self.settings.id,
                source_agent_name=self.settings.name,
            ),
            msg_meta=MessageMeta(
                from_agent_name=self.settings.name,
                msg_correlation_id=notification_id,
                conversation_id=getattr(msg_meta, "conversation_id", None),
                followup_to_task_id=task_id,
                objective_id=getattr(msg_meta, "objective_id", None),
                request_timezone=getattr(msg_meta, "request_timezone", None),
                requester_type=RequesterType.AGENT_REQUESTER,
                is_response_requested=False,
                gateway_context=gateway_context_copy,
            ),
            msg_header=MessageHeader(
                from_agent_id=self.settings.id,
                to_routing_path=routing_key,
                response_routing_key=self.settings.id,
                idempotency_key=notification_id,
                agent_signature="",
            ),
        )
        try:
            await send_message(message=envelope, routing_key=routing_key)
        except Exception as exc:
            self.logger.warning(
                "[%s] event=model_provider_failover_notification_failed task_id=%s notification_id=%s from_model=%s to_model=%s error_type=%s error=%s",
                self.settings.id,
                task_id or "unknown",
                notification_id,
                outcome.from_candidate.key,
                outcome.to_candidate.key,
                type(exc).__name__,
                exc,
            )
            return False

        self.logger.info(
            "[%s] event=model_provider_failover_notification_sent task_id=%s notification_id=%s from_model=%s to_model=%s",
            self.settings.id,
            task_id or "unknown",
            notification_id,
            outcome.from_candidate.key,
            outcome.to_candidate.key,
        )
        return True

    async def _recover_after_provider_failure(
        self,
        *,
        error: BaseException,
        task_id: Optional[str],
        tool_calls_started: bool,
        msg_content: ContentType,
        msg_header: MessageHeader,
        msg_meta: MessageMeta,
        tool_call_limit_handler: Any,
    ) -> bool:
        """Apply a configured fallback and report whether the turn should retry inline."""

        outcome = await self.effects.handle_provider_failure(
            error=error, task_id=task_id, tool_calls_started=tool_calls_started
        )
        if outcome is None:
            return False

        await self._notify_user_of_model_failover(
            outcome=outcome,
            task_id=task_id,
            msg_content=msg_content,
            msg_header=msg_header,
            msg_meta=msg_meta,
        )
        if outcome.retry_inline:
            self.effects.reset_llm_conversation()
            self.effects.refresh_skills_plugins()
            self._take_optional_strands_snapshot()
            if tool_call_limit_handler is not None:
                tool_call_limit_handler.cancel_callback = getattr(
                    self.model(), "cancel", None
                )
            return True

        if await self.effects.requeue_task_after_failover(
            task_id=task_id, outcome=outcome, error=error
        ):
            raise model_failover_runtime.ProviderFailoverRequeued(
                outcome.notice
            ) from error
        return False

    async def process_llm_interaction(
        self, msg_content: ContentType, msg_header: MessageHeader, msg_meta: MessageMeta
    ) -> ContentType:
        """Execute one turn through explicit preparation, model, and result phases."""
        completed_session = await self._finish_checkpointed_work_session(
            msg_content, msg_header, msg_meta
        )
        if completed_session is not None:
            return completed_session
        turn = _TurnState(msg_content, msg_header, msg_meta)
        try:
            early_response = await self._prepare_turn(turn)
        except BaseException:
            self.effects.clear_active_execution_context()
            raise
        if early_response is not None:
            # A turn that pauses before the model runs gives back a person's model too.
            self.effects.clear_active_execution_context()
            return early_response
        try:
            return await self._run_prepared_turn(turn)
        except asyncio.CancelledError:
            await self._end_tool_work_of_cancelled_turn(turn)
            raise
        finally:
            self.effects.clear_active_execution_context()

    async def _run_prepared_turn(self, turn: _TurnState) -> ContentType:
        """Run and recover inside the cancellation owner, including error-handler awaits."""

        try:
            await self._prepare_invocation(turn)
            await self._invoke_model(turn)
            return await self._complete_turn(turn)
        except ToolCallLimitExceeded as e:
            await self._write_context_debug_post_turn(
                request=turn.context_request,
                assembly=turn.assembly,
                dump_dir=turn.context_debug_dump_dir,
                result=turn.result,
                parsed_content=turn.content,
                after_action_report=turn.after_action_report,
                error=e,
            )
            await self.effects.record_tool_call_limit_exceeded(
                msg_content=turn.msg_content, limit_error=e
            )
            trigger = RecoverablePauseTrigger.TOOL_CALL_BUDGET_EXCEEDED
            task_info = (
                await self.tasks.runtime.run(
                    self.tasks.get_task_info, turn.active_task_id
                )
                if turn.active_task_id
                else None
            )
            if (
                task_info is not None
                and (not turn.partial_finalization_requested)
                and (
                    self.effects.runtime_budget_policy_mode()
                    == RuntimeBudgetPolicyMode.USER_GUIDED.value
                )
                and self.effects.runtime_budget_trigger_enabled(trigger)
                and (
                    await self.tasks.runtime.run(
                        self.tasks.count_recoverable_pauses, task_info.task.task_id
                    )
                    < self.effects.max_user_guided_pauses_per_task()
                )
            ):
                friendly_reason = self.effects.friendly_budget_reason(
                    trigger=trigger, msg_content=turn.msg_content, limit_error=e
                )
                if (
                    isinstance(turn.msg_content, Task)
                    and turn.msg_content.parent_task_id
                ):
                    return await self.effects.send_paused_subtask_response(
                        task_request=turn.msg_content,
                        msg_header=turn.msg_header,
                        msg_meta=turn.msg_meta,
                        error_message=f"Recoverable runtime pause: {friendly_reason}.",
                        trigger=trigger,
                    )
                return await self.effects.send_runtime_pause_query(
                    task_info=task_info,
                    trigger=trigger,
                    friendly_reason=friendly_reason,
                    base_tool_call_limit=e.max_tool_calls,
                )
            raise
        except ValidationError as e:
            await self._write_context_debug_post_turn(
                request=turn.context_request,
                assembly=turn.assembly,
                dump_dir=turn.context_debug_dump_dir,
                result=turn.result,
                parsed_content=turn.content,
                after_action_report=turn.after_action_report,
                error=e,
            )
            self.logger.error(
                f"[{self.settings.id}] Failed to parse LLM response as CommsMessage: {e}"
            )
            raise
        except model_failover_runtime.ProviderFailoverRequeued as e:
            await self._write_context_debug_post_turn(
                request=turn.context_request,
                assembly=turn.assembly,
                dump_dir=turn.context_debug_dump_dir,
                result=turn.result,
                parsed_content=turn.content,
                after_action_report=turn.after_action_report,
                error=e,
            )
            raise
        except Exception as e:
            await self._write_context_debug_post_turn(
                request=turn.context_request,
                assembly=turn.assembly,
                dump_dir=turn.context_debug_dump_dir,
                result=turn.result,
                parsed_content=turn.content,
                after_action_report=turn.after_action_report,
                error=e,
            )
            if isinstance(e, (ProviderTimeoutError, UnroutableTaskInterruptError)):
                # The task owner records and reports this classified failure once.
                raise
            if self._is_provider_connectivity_error(e):
                connectivity_error = self._provider_connectivity_failure(e)
                self.logger.error(
                    "[%s] event=model_provider_connectivity_failure task_id=%s provider=%s model_id=%s endpoint_host=%s error_type=%s",
                    self.settings.id,
                    turn.active_task_id,
                    self.settings.model_provider,
                    self.settings.model_id,
                    self._configured_provider_endpoint_host(),
                    type(e).__name__,
                )
                raise connectivity_error from e
            trigger = RecoverablePauseTrigger.PROVIDER_OVERLOADED
            task_info = (
                await self.tasks.runtime.run(
                    self.tasks.get_task_info, turn.active_task_id
                )
                if turn.active_task_id
                else None
            )
            if (
                self._is_provider_overload_error(e)
                and task_info is not None
                and isinstance(turn.msg_content, Task)
                and turn.msg_content.parent_task_id
            ):
                # Delegated work waits out the limit and retries without asking
                # anyone; it raises a hand-off or a typed failure
                # (custom_agents.provider_limits).
                await self.effects.handle_delegated_provider_limit(task_info=task_info, error=e)
            if (
                self._is_provider_overload_error(e)
                and task_info is not None
                and self.effects.runtime_budget_trigger_enabled(trigger)
                and (
                    await self.tasks.runtime.run(
                        self.tasks.count_recoverable_pauses, task_info.task.task_id
                    )
                    < self.effects.max_user_guided_pauses_per_task()
                )
            ):
                friendly_reason = self.effects.friendly_budget_reason(
                    trigger=trigger,
                    msg_content=turn.msg_content,
                    detail=_provider_overload_error_summary(e),
                )
                self.logger.warning(
                    "[%s] Provider overload while processing task %s; asking requester how to proceed.",
                    self.settings.id,
                    task_info.task.task_id,
                )
                if (
                    isinstance(turn.msg_content, Task)
                    and turn.msg_content.parent_task_id
                ):
                    return await self.effects.send_paused_subtask_response(
                        task_request=turn.msg_content,
                        msg_header=turn.msg_header,
                        msg_meta=turn.msg_meta,
                        error_message=f"Recoverable provider overload: {friendly_reason}.",
                        trigger=trigger,
                    )
                return await self.effects.send_runtime_pause_query(
                    task_info=task_info,
                    trigger=trigger,
                    friendly_reason=friendly_reason,
                    base_tool_call_limit=self.effects.resolved_max_tool_calls_per_task(),
                )
            self.logger.exception(f"[{self.settings.id}] Error in LLM interaction: {e}")
            raise
    async def _finish_checkpointed_work_session(
        self, content: ContentType, header: MessageHeader, meta: MessageMeta,
    ) -> ContentType | None:
        """A resumed session root closes from its checkpoint without more model work."""
        if not isinstance(content, Task) or content.task_type != MessageMetaType.REQUEST or not meta.objective_work_session_id:
            return None
        coordinator = self.tasks.objective_work_coordinator
        if coordinator is None:
            return None
        session = await self.tasks.runtime.run(
            coordinator.session_for_meta, meta.objective_work_session_id,
        )
        if (
            session is None or session.task_id != content.task_id
            or session.status != ObjectiveWorkSessionStatus.CHECKPOINTED
            or not session.checkpoint_id
        ):
            return None
        decision = await self.tasks.runtime.run(
            self.effects.prepare_objective_work_completion, content, meta, session,
        )
        return await self.effects.execute_turn_decision_outbox(
            decision=decision, incoming_content=content,
            incoming_header=header, incoming_meta=meta,
            structured_output_details=None,
        )

    async def _prepare_turn(self, turn: _TurnState) -> ContentType | None:
        self.effects.refresh_runtime_llm_selection()
        isolated_recovery_task = (
            isinstance(turn.msg_content, Task)
            and turn.msg_content.task_type == MessageMetaType.REQUEST
            and await self.tasks.runtime.run(
                self.tasks.is_isolated_recovery_task, turn.msg_content.task_id
            )
        )
        plan_step_task = (
            isinstance(turn.msg_content, Task)
            and turn.msg_content.task_type == MessageMetaType.REQUEST
            and await self.tasks.runtime.run(
                self.tasks.is_plan_step_task, turn.msg_content.task_id
            )
        )
        turn.prompt_content = turn.msg_content
        turn.prompt_meta = turn.msg_meta
        if (isolated_recovery_task or plan_step_task) and isinstance(
            turn.msg_content, Task
        ):
            prompt_task = turn.msg_content.model_copy(deep=True)
            prompt_task.task_id = (
                "current_recovery_task"
                if isolated_recovery_task
                else "current_plan_step_task"
            )
            prompt_task.parent_task_id = None
            prompt_task.task_state = []
            prompt_task.execution_history = []
            prompt_task.execution_digest = None
            prompt_task.task_plan = None
            turn.prompt_content = prompt_task
            turn.prompt_meta = turn.msg_meta.model_copy(deep=True)
            turn.prompt_meta.msg_correlation_id = (
                "current_recovery_correlation"
                if isolated_recovery_task
                else "current_plan_step_correlation"
            )
            turn.prompt_meta.conversation_id = (
                "current_recovery_conversation"
                if isolated_recovery_task
                else "current_plan_step_conversation"
            )
            turn.prompt_meta.followup_to_task_id = None
            turn.prompt_meta.gateway_context = None
        llm_msg = CommsMessage(
            msg_content=turn.prompt_content, msg_meta=turn.prompt_meta
        )
        user_message = llm_msg.model_dump_json()
        if self.context() is None:
            self.effects.create_context_engine()
        turn.active_task_id = self.effects.history_task_id_for_content(turn.msg_content)
        # Before assembly, so the prompt budget uses the window of the model this turn runs on.
        self.effects.select_turn_model(turn.active_task_id)
        (
            turn.work_coordinator,
            turn.work_session,
        ) = await self._resolve_objective_work_context(
            turn.msg_meta, turn.active_task_id
        )
        turn.turn_decision_model = self._turn_decision_structured_output_model()
        turn.turn_decision_tool_name = turn.turn_decision_model.__name__
        turn.context_request = ContextAssemblyRequest(
            incoming_content=turn.prompt_content,
            incoming_header=turn.msg_header,
            incoming_meta=turn.prompt_meta,
            identity_meta=turn.msg_meta,
            execution_task_id=turn.active_task_id,
            incoming_message_json=user_message,
            isolated_recovery_task=isolated_recovery_task,
            plan_step_task=plan_step_task,
            provider_prompt_suffix=_turn_decision_initial_guidance(
                turn.turn_decision_tool_name
            ),
        )
        try:
            turn.assembly = await self._assemble_context_with_recovery(
                request=turn.context_request, original_content=turn.msg_content
            )
        except ContextBudgetExceededError as e:
            trigger = RecoverablePauseTrigger.CONTEXT_BUDGET_EXCEEDED
            task_info = (
                await self.tasks.runtime.run(
                    self.tasks.get_task_info, turn.active_task_id
                )
                if turn.active_task_id
                else None
            )
            if (
                task_info is not None
                and self.effects.runtime_budget_policy_mode()
                == RuntimeBudgetPolicyMode.USER_GUIDED.value
                and self.effects.runtime_budget_trigger_enabled(trigger)
                and (
                    await self.tasks.runtime.run(
                        self.tasks.count_recoverable_pauses, task_info.task.task_id
                    )
                    < self.effects.max_user_guided_pauses_per_task()
                )
            ):
                friendly_reason = self.effects.friendly_budget_reason(
                    trigger=trigger, msg_content=turn.msg_content, detail=str(e)
                )
                if (
                    isinstance(turn.msg_content, Task)
                    and turn.msg_content.parent_task_id
                ):
                    return await self.effects.send_paused_subtask_response(
                        task_request=turn.msg_content,
                        msg_header=turn.msg_header,
                        msg_meta=turn.msg_meta,
                        error_message=f"Recoverable runtime pause: {friendly_reason}.",
                        trigger=trigger,
                    )
                return await self.effects.send_runtime_pause_query(
                    task_info=task_info,
                    trigger=trigger,
                    friendly_reason=friendly_reason,
                    base_tool_call_limit=self.effects.resolved_max_tool_calls_per_task(),
                )
            raise
        self.last_context_assembly = turn.assembly
        turn.full_prompt = turn.assembly.prompt
        turn.context_debug_dump_dir = None
        self.logger.info(
            f"[{self.settings.id}] Invoking Strands Agent with prompt length: {len(turn.full_prompt)} chars"
        )
        turn.tool_stop = self.effects.set_active_execution_context(turn.active_task_id)
        return None

    async def _prepare_invocation(self, turn: _TurnState) -> None:
        self.effects.ensure_fresh_strands_agent_for_new_turn(
            task_id=turn.active_task_id
        )
        self.effects.reset_llm_conversation()
        self.effects.refresh_skills_plugins()
        self._take_optional_strands_snapshot()
        self.logger.debug(
            f"[{self.settings.id}] self._strands_agent ID = {id(self.model())}"
        )
        turn.invoke_kwargs = {
            "structured_output_model": turn.turn_decision_model,
            "structured_output_prompt": self._turn_decision_structured_output_prompt(),
            "invocation_state": {
                RuntimeGuardrailsPlugin._TURN_DECISION_CONTEXT_KEY: {
                    "agent_id": self.settings.id,
                    "named_agent_type": self.settings.named_agent_type,
                    "task_id": turn.active_task_id,
                    "correlation_id": getattr(
                        turn.msg_meta, "msg_correlation_id", None
                    ),
                    "conversation_id": getattr(turn.msg_meta, "conversation_id", None),
                    "provider": self.settings.model_provider,
                    "model_id": self.settings.model_id,
                    "expected_tool_name": turn.turn_decision_tool_name,
                },
                ContextDebugDumpPlugin.INVOCATION_CONTEXT_KEY: {
                    "agent_id": self.settings.id,
                    "agent_name": self.settings.name,
                    "named_agent_type": self.settings.named_agent_type,
                    "task_id": getattr(
                        turn.context_request.incoming_content, "task_id", None
                    )
                    or turn.active_task_id,
                    "correlation_id": getattr(
                        turn.msg_meta, "msg_correlation_id", None
                    ),
                    "conversation_id": getattr(turn.msg_meta, "conversation_id", None),
                    "provider": self.settings.model_provider,
                    "model_id": self.settings.model_id,
                },
            },
        }
        callbacks: List[Callable[..., None]] = []
        existing_callback = getattr(self.model(), "callback_handler", None)
        if callable(existing_callback):
            callbacks.append(existing_callback)
        tool_call_limit = self.effects.resolved_max_tool_calls_per_task()
        turn.tool_call_limit_handler = None
        runtime_resume_directive = await self.tasks.runtime.run(
            self.tasks.consume_runtime_resume_directive, turn.active_task_id
        )
        partial_tool_call_limit = await self.tasks.runtime.run(
            self.tasks.consume_partial_tool_call_limit, turn.active_task_id
        )
        turn.partial_finalization_requested = partial_tool_call_limit is not None
        if partial_tool_call_limit is not None:
            tool_call_limit = partial_tool_call_limit
        turn.runtime_instruction_parts = []
        if turn.work_session is not None:
            tool_call_limit = await _apply_objective_work_invocation_limits(
                coordinator=turn.work_coordinator,
                runtime=self.tasks.runtime,
                session=turn.work_session,
                invoke_kwargs=turn.invoke_kwargs,
                runtime_instruction_parts=turn.runtime_instruction_parts,
                explicit_tool_call_limit=partial_tool_call_limit,
            )
        if runtime_resume_directive:
            turn.runtime_instruction_parts.append(runtime_resume_directive)
        queued_guidance = await self.effects.consume_pending_proactive_guidance_message(
            turn.active_task_id
        )
        if queued_guidance:
            turn.runtime_instruction_parts.append(
                f"Runtime mid-turn requester steering was received before this task turn resumed. Treat this requester input as authoritative and update the task objective accordingly:\n{queued_guidance}"
            )
        if turn.partial_finalization_requested:
            turn.runtime_instruction_parts.append(
                "Runtime partial-result instruction: the requester asked for the best partial result(s) now. Use existing evidence and task history; avoid broad new research or exploratory tool use. Return the clearest useful partial answer you can."
            )
        if turn.runtime_instruction_parts:
            turn.assembly = self._append_provider_prompt_suffix(
                turn.assembly,
                text="\n".join(turn.runtime_instruction_parts),
                name="runtime_turn_instructions",
                source="UniversalAgent.process_llm_interaction",
            )
            turn.full_prompt = turn.assembly.prompt
        if tool_call_limit is not None:
            turn.tool_call_limit_handler = ToolCallLimitCallbackHandler(
                max_tool_calls=tool_call_limit,
                raise_on_exceeded=False,
                cancel_callback=None
                if turn.work_session is not None
                else getattr(self.model(), "cancel", None),
            )
            callbacks.append(turn.tool_call_limit_handler)
        terminal_reasoning_enabled = self._terminal_reasoning_enabled()
        model_diagnostics = self._terminal_reasoning_model_diagnostics()
        self.logger.info(
            "[%s] Terminal reasoning effective state: task_id=%s enabled=%s provider=%s model_id=%s stream=%s reasoning_effort=%s reasoning_summary=%s litellm_api_mode=%s model_adapter=%s model_config_model_id=%s model_config_stream=%s model_config_reasoning_effort=%s model_config_reasoning_summary=%s stderr_isatty=%s stderr_type=%s model_config_params=%s",
            self.settings.id,
            turn.active_task_id or "-",
            terminal_reasoning_enabled,
            self.settings.model_provider,
            self.settings.model_id,
            self.settings.stream,
            self.settings.reasoning_effort,
            self.settings.reasoning_summary,
            self.settings.litellm_api_mode,
            model_diagnostics.get("model_adapter"),
            model_diagnostics.get("model_config_model_id"),
            model_diagnostics.get("model_config_stream"),
            model_diagnostics.get("model_config_reasoning_effort"),
            model_diagnostics.get("model_config_reasoning_summary"),
            getattr(sys.stderr, "isatty", lambda: False)(),
            type(sys.stderr).__name__,
            _compact_debug_preview(
                model_diagnostics.get("model_config_params"), max_chars=800
            ),
        )
        if terminal_reasoning_enabled:
            callbacks.append(TerminalReasoningCallbackHandler())
            callbacks.append(
                TerminalReasoningDiagnosticsCallbackHandler(
                    logger=self.logger,
                    agent_id=self.settings.id,
                    task_id=turn.active_task_id,
                )
            )
        callbacks.append(
            ActivityPhaseCallbackHandler(
                emit_activity=self.effects.emit_activity_event,
                task_id=turn.active_task_id,
            )
        )
        callback_handler = CompositeCallbackHandler(*callbacks)
        turn.invoke_kwargs["callback_handler"] = callback_handler
        turn.span_attributes = {
            "team_id": self.settings.team_id or "",
            "agent_id": self.settings.id,
            "named_agent_type": self.settings.named_agent_type or "",
            "correlation_id": getattr(turn.msg_meta, "msg_correlation_id", None) or "",
            "task_id": getattr(turn.msg_content, "task_id", None) or "",
            "conversation_id": getattr(turn.msg_meta, "conversation_id", None) or "",
        }

    async def _invoke_model(self, turn: _TurnState) -> None:
        with start_runtime_span(
            "process_llm_interaction",
            enabled=self.settings.otel_enabled and self.settings.otel_enable_traces,
            attributes=turn.span_attributes,
        ):
            turn.provider_retry_attempt = 0
            turn.strands_invocation_attempt = 0
            turn.reasoning_summary_repair_attempted = False
            turn.invocation_started = time.monotonic()
            while True:
                try:
                    strands_prompt_input = self._build_strands_prompt_input(
                        turn.full_prompt, turn.prompt_content
                    )
                    prompt_input_summary = _strands_prompt_input_summary(
                        strands_prompt_input
                    )
                    self.logger.info(
                        "[%s] Strands invoke input summary: attempt=%s summary=%s",
                        self.settings.id,
                        turn.strands_invocation_attempt,
                        _compact_debug_preview(prompt_input_summary, max_chars=1600),
                    )
                    self._write_strands_prompt_input_debug_dump(
                        request=turn.context_request,
                        assembly=turn.assembly,
                        strands_prompt_input=strands_prompt_input,
                        attempt=turn.strands_invocation_attempt,
                    )
                    turn.result = await invoke_with_tool_stop(
                        self._require_model(),
                        strands_prompt_input,
                        tool_stop=self._require_tool_stop(turn),
                        invoke_kwargs=turn.invoke_kwargs,
                        logger=self.logger,
                    )
                    late_guidance = (
                        await self.effects.consume_pending_proactive_guidance_message(
                            turn.active_task_id
                        )
                    )
                    if late_guidance:
                        self.logger.info(
                            "[%s] Re-invoking task %s because mid-turn steering arrived after the model call.",
                            self.settings.id,
                            turn.active_task_id or "unknown",
                        )
                        late_guidance_instruction = (
                            "Runtime mid-turn requester steering:\n"
                            + late_guidance
                            + "\n\nThis steering arrived before the task result was committed. Treat it as authoritative and regenerate the task result accordingly."
                        )
                        turn.assembly = self._append_provider_prompt_suffix(
                            turn.assembly,
                            text=late_guidance_instruction,
                            name="late_requester_steering",
                            source="task_steering_runtime.consume_pending_proactive_guidance_message",
                        )
                        turn.full_prompt = turn.assembly.prompt
                        self.effects.reset_llm_conversation()
                        self.effects.refresh_skills_plugins()
                        self._take_optional_strands_snapshot()
                        turn.strands_invocation_attempt += 1
                        continue
                    break
                except Exception as invoke_error:
                    await self._recover_invocation(turn, invoke_error)

    async def _recover_invocation(
        self, turn: _TurnState, invoke_error: Exception
    ) -> None:
        if turn.tool_stop is not None and turn.tool_stop.is_requested():
            # The failed invocation's commands were stopped (turn_tool_stop):
            # its tool work was cut short, and a set stop would cancel any new
            # invocation at once, so the turn is not run again inline.
            raise invoke_error
        if (
            not turn.reasoning_summary_repair_attempted
            and is_unsupported_reasoning_summary_error(
                invoke_error,
                provider=self.settings.model_provider,
                model_id=self.settings.model_id,
                litellm_api_mode=self.settings.litellm_api_mode,
                requested_summary=self.settings.reasoning_summary,
            )
        ):
            turn.reasoning_summary_repair_attempted = True
            try:
                repair = disable_unsupported_reasoning_summary(
                    provider=self.settings.model_provider,
                    model_id=self.settings.model_id,
                    requested_summary=self.settings.reasoning_summary,
                    instance_root=self.settings.instance_root,
                    explicit_override_path=self.settings.llm_override_path,
                )
            except Exception as repair_error:
                self.logger.exception(
                    "[%s] event=reasoning_summary_repair_persistence_failed task_id=%s provider=%s model_id=%s error_type=%s error=%s",
                    self.settings.id,
                    turn.active_task_id or "unknown",
                    self.settings.model_provider,
                    self.settings.model_id,
                    type(repair_error).__name__,
                    repair_error,
                )
                raise RuntimeError(
                    "The model rejected reasoning_summary, but the runtime could not safely update LLM_OVERRIDES.yaml; the request was not retried."
                ) from repair_error
            await self._record_reasoning_summary_repair_event(
                task_id=turn.active_task_id, repair=repair, error=invoke_error
            )
            notified = await self._notify_user_of_reasoning_summary_repair(
                repair=repair,
                task_id=turn.active_task_id,
                msg_header=turn.msg_header,
                msg_meta=turn.msg_meta,
            )
            if not notified:
                turn.runtime_instruction_parts.append(
                    "Runtime configuration notice for the final user response: "
                    + _reasoning_summary_repair_notice(repair)
                )
                turn.assembly = self._append_provider_prompt_suffix(
                    turn.assembly,
                    text=turn.runtime_instruction_parts[-1],
                    name="reasoning_summary_repair_notice",
                    source="reasoning_summary_recovery",
                )
                turn.full_prompt = turn.assembly.prompt
            self.effects.refresh_persistent_llm_override()
            self.effects.reset_llm_conversation()
            self.effects.refresh_skills_plugins()
            self._take_optional_strands_snapshot()
            if turn.tool_call_limit_handler is not None:
                turn.tool_call_limit_handler.cancel_callback = getattr(
                    self.model(), "cancel", None
                )
            turn.strands_invocation_attempt += 1
            return None
        (
            context_retry_allowed,
            tool_calls_started,
            context_retry_decision,
        ) = await self._resolve_provider_context_retry(
            task_id=turn.active_task_id,
            error=invoke_error,
            invocation_state=turn.invoke_kwargs["invocation_state"],
            provider_retry_attempt=turn.provider_retry_attempt,
            tool_call_limit_handler=turn.tool_call_limit_handler,
        )
        if context_retry_allowed:
            turn.provider_retry_attempt += 1
            compaction_result = None
            try:
                compaction_result = await self.tasks.runtime.run(
                    self._require_context().compact,
                    ContextCompactionReason.OVERFLOW_RECOVERY,
                    turn.context_request,
                )
            except Exception as compact_error:
                self.logger.warning(
                    "[%s] Provider-overflow compaction attempt failed: %s",
                    self.settings.id,
                    compact_error,
                )
            degraded_sections = self._context_overflow_degrade_sections()
            await self._record_provider_context_retry_event(
                task_id=turn.active_task_id,
                attempt=turn.provider_retry_attempt,
                error=invoke_error,
                assembly=turn.assembly,
                compaction_result=compaction_result,
                degraded_sections=degraded_sections,
                total_recovery_attempt=getattr(
                    context_retry_decision.recovery_attempt, "attempt", None
                ),
                max_total_recovery_attempts=getattr(
                    context_retry_decision.recovery_attempt, "max_attempts", None
                ),
            )
            turn.context_request = replace(
                turn.context_request,
                recovery_attempt=max(
                    int(turn.context_request.recovery_attempt or 0),
                    turn.provider_retry_attempt,
                ),
                degraded_sections=degraded_sections,
            )
            turn.assembly = await self.tasks.runtime.run(
                self._require_context().assemble, turn.context_request
            )
            turn.assembly.warnings.append(
                {
                    "warning": "provider_context_overflow_retry",
                    "attempt": turn.provider_retry_attempt,
                    "context_epoch_id": getattr(
                        compaction_result, "context_epoch_id", None
                    ),
                    "degraded_sections": list(degraded_sections),
                }
            )
            self.last_context_assembly = turn.assembly
            turn.full_prompt = turn.assembly.prompt
            if turn.runtime_instruction_parts:
                turn.assembly = self._append_provider_prompt_suffix(
                    turn.assembly,
                    text="\n".join(turn.runtime_instruction_parts),
                    name="runtime_turn_instructions",
                    source="UniversalAgent.process_llm_interaction",
                )
                turn.full_prompt = turn.assembly.prompt
            self.effects.reset_llm_conversation()
            self.effects.refresh_skills_plugins()
            self._take_optional_strands_snapshot()
            turn.strands_invocation_attempt += 1
            return None
        if await self._recover_after_provider_failure(
            error=invoke_error,
            task_id=turn.active_task_id,
            tool_calls_started=tool_calls_started,
            msg_content=turn.msg_content,
            msg_header=turn.msg_header,
            msg_meta=turn.msg_meta,
            tool_call_limit_handler=turn.tool_call_limit_handler,
        ):
            turn.provider_retry_attempt = 0
            turn.strands_invocation_attempt += 1
            return None
        _raise_provider_timeout_failure(invoke_error)
        raise invoke_error

    def decode_result(
        self, *, result: AgentResult, msg_content: ContentType, msg_meta: MessageMeta
    ) -> TurnDecisionOutcome:
        """Interpret SDK state once, before exposing a decision to workflows."""
        fallback_details = self._structured_output_fallback_details(result)
        structured_output_details = self._structured_output_input_details(result)
        if is_reply_routing_trace_enabled(self.settings.debug):
            structured_output = getattr(result, "structured_output", None)
            structured_actions = getattr(structured_output, "actions", None)
            action_types = None
            if isinstance(structured_actions, list):
                action_types = [
                    getattr(action, "action_type", action.__class__.__name__)
                    for action in structured_actions
                ]
            self.logger.info(
                "[%s] event=llm_structured_output_received task_id=%s correlation_id=%s conversation_id=%s structured_output_type=%s action_types=%s structured_input_details=%s fallback_failures=%s fallback_last_error=%s",
                self.settings.id,
                getattr(msg_content, "task_id", None) or "unknown",
                getattr(msg_meta, "msg_correlation_id", None) or "unknown",
                getattr(msg_meta, "conversation_id", None) or "unknown",
                structured_output.__class__.__name__
                if structured_output is not None
                else None,
                action_types,
                structured_output_details,
                (fallback_details or {}).get("observed_failures"),
                (fallback_details or {}).get("last_error_preview"),
            )
        if fallback_details:
            error_message = f"ModelAgentTurnDecision validation failed repeatedly; the runtime stopped the turn without asking the model for handwritten JSON. Last error: {fallback_details.get('last_error_preview', 'not available')}"
            self.logger.error(
                "[%s] ModelAgentTurnDecision failed validation %s consecutive times; stopping turn because raw-text JSON is not a transport contract. task_id=%s correlation_id=%s last_error=%s",
                self.settings.id,
                fallback_details.get("observed_failures", "unknown"),
                getattr(msg_content, "task_id", None) or "unknown",
                getattr(msg_meta, "msg_correlation_id", None) or "unknown",
                fallback_details.get("last_error_preview", "not available"),
            )
            raise RuntimeError(error_message)
        else:
            structured_output = getattr(result, "structured_output", None)
            decision = self.effects.turn_decision_from_structured_output(
                structured_output
            )
            if decision is None:
                comms_message = self._extract_comms_message(result)
                decision = self.effects.turn_decision_from_comms_message(comms_message)
        return TurnDecisionOutcome(decision, structured_output_details)

    async def _complete_turn(self, turn: _TurnState) -> ContentType:
        if turn.work_session is not None:
            work_yield_response = await _finalize_objective_work_invocation(
                model=self._require_model(),
                logger=self.logger,
                coordinator=turn.work_coordinator,
                runtime=self.tasks.runtime,
                session=turn.work_session,
                result=turn.result,
                invocation_started=turn.invocation_started,
                invoke_kwargs=turn.invoke_kwargs,
                tool_call_limit_handler=turn.tool_call_limit_handler,
                msg_content=turn.msg_content,
                active_task_id=turn.active_task_id,
                tool_stop=self._require_tool_stop(turn),
            )
            if work_yield_response is not None:
                return work_yield_response
        self.effects.raise_if_stop_requested(turn.active_task_id)
        turn.result = self._apply_agent_result_transformer(
            turn.msg_content, turn.result
        )
        if getattr(turn.result, "stop_reason", None) == "interrupt" and isinstance(
            turn.msg_content, Task
        ):
            return await self.effects.send_interrupt_request_for_result(
                result=turn.result,
                task=turn.msg_content,
                msg_header=turn.msg_header,
                msg_meta=turn.msg_meta,
                strands_agent=self.model(),
                invoke_kwargs=turn.invoke_kwargs,
                full_prompt=turn.full_prompt,
            )
        if (
            turn.tool_call_limit_handler is not None
            and turn.tool_call_limit_handler.limit_error is not None
        ):
            raise turn.tool_call_limit_handler.limit_error
        await self.effects.record_tool_result_budget_events(
            msg_content=turn.msg_content, result=turn.result
        )
        self.effects.record_model_invocation_success(task_id=turn.active_task_id)
        outcome = self.decode_result(
            result=turn.result, msg_content=turn.msg_content, msg_meta=turn.msg_meta
        )
        turn.content = await self.effects.execute_turn_decision_outbox(
            decision=outcome.decision,
            incoming_content=turn.msg_content,
            incoming_header=turn.msg_header,
            incoming_meta=turn.msg_meta,
            structured_output_details=outcome.structured_output_details,
        )
        await self._write_context_debug_post_turn(
            request=turn.context_request,
            assembly=turn.assembly,
            dump_dir=turn.context_debug_dump_dir,
            result=turn.result,
            parsed_content=turn.content,
            after_action_report=turn.after_action_report,
        )
        return turn.content
