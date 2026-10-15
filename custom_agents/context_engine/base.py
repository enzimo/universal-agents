from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any, Optional, Protocol

from strands.agent.conversation_manager.conversation_manager import ConversationManager

from custom_agents.conversation_compaction.base import CompactionResult
from custom_messages.contracts.envelope_v1 import ContentType
from custom_messages.contracts.routing_v1 import MessageHeader, MessageMeta


class ContextBudgetExceededError(RuntimeError):
    """Raised when assembled model-visible context exceeds the hard input budget."""

    def __init__(
        self,
        message: str,
        *,
        estimated_tokens: int,
        hard_limit_tokens: int,
        max_context_tokens: Optional[int],
        reserved_output_tokens: int,
        source_lengths: list[dict[str, Any]],
    ) -> None:
        super().__init__(message)
        self.estimated_tokens = estimated_tokens
        self.hard_limit_tokens = hard_limit_tokens
        self.max_context_tokens = max_context_tokens
        self.reserved_output_tokens = reserved_output_tokens
        self.source_lengths = source_lengths


class ContextCompactionReason(StrEnum):
    PREFLIGHT_BUDGET = "preflight_budget"
    POST_TURN = "post_turn"
    MANUAL = "manual"
    OVERFLOW_RECOVERY = "overflow_recovery"


@dataclass
class ContextSection:
    name: str
    text: str
    priority: int
    pinned: bool = False
    source: str = ""
    char_count: int = 0
    estimated_tokens: int = 0
    plugin_id: Optional[str] = None
    scope_kind: Optional[str] = None
    degradable: bool = False
    warning: Optional[str] = None


@dataclass
class AssembledContext:
    sections: list[ContextSection]
    prompt: str
    char_count: int
    estimated_tokens: int
    source_lengths: list[dict[str, Any]]
    warnings: list[dict[str, Any]] = field(default_factory=list)
    max_context_tokens: Optional[int] = None
    soft_limit_tokens: Optional[int] = None
    hard_limit_tokens: Optional[int] = None
    reserved_output_tokens: int = 0
    context_fill_ratio: Optional[float] = None
    context_epoch_id: Optional[str] = None
    recovery_attempt: int = 0
    degraded_sections: list[str] = field(default_factory=list)


@dataclass
class ContextAssemblyRequest:
    incoming_content: ContentType
    incoming_header: MessageHeader
    incoming_meta: MessageMeta
    # Original task metadata for verified identity checks. incoming_meta may be
    # sanitized for a plan-step or recovery prompt and must not select folders.
    identity_meta: MessageMeta
    # Trusted runtime task identity. Focused prompt copies use placeholder ids
    # and cannot select stored clarification records.
    execution_task_id: Optional[str]
    incoming_message_json: str
    isolated_recovery_task: bool = False
    plan_step_task: bool = False
    recovery_attempt: int = 0
    degraded_sections: tuple[str, ...] = ()
    provider_prompt_suffix: str = ""


@dataclass
class TurnFinalizationRequest:
    task_info: Any
    task_status: str
    strands_messages: list[dict[str, Any]]


class ContextEnginePlugin(Protocol):
    """Optional lifecycle hooks for context assembly plugins.

    Implementations may provide any subset of these methods. `DefaultContextEngine`
    checks for methods dynamically so plugins can stay small and focused.
    """

    def ingest(self, request: ContextAssemblyRequest) -> None:
        ...

    def assemble(
        self,
        request: ContextAssemblyRequest,
        assembly: AssembledContext,
    ) -> AssembledContext | None:
        ...

    def compact(
        self,
        reason: ContextCompactionReason,
        request: ContextAssemblyRequest | None = None,
    ) -> CompactionResult | None:
        ...

    def after_turn(self, request: TurnFinalizationRequest) -> None:
        ...

    def post_invocation(
        self,
        request: ContextAssemblyRequest,
        assembly: AssembledContext,
        **kwargs: Any,
    ) -> None:
        ...


class ContextEngine(Protocol):
    def assemble(self, request: ContextAssemblyRequest) -> AssembledContext:
        ...

    def compact(
        self,
        reason: ContextCompactionReason,
        request: ContextAssemblyRequest | None = None,
    ) -> CompactionResult | None:
        ...

    def after_turn(self, request: TurnFinalizationRequest) -> None:
        ...

    def post_invocation(
        self,
        request: ContextAssemblyRequest,
        assembly: AssembledContext,
        **kwargs: Any,
    ) -> None:
        ...

    def build_inner_loop_conversation_manager(self) -> ConversationManager | None:
        ...
