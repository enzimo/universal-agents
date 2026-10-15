"""Shared types for memory hydration and maintenance plugins.

Whom a turn's personal memory belongs to: personal hydration (``usermem``,
``domain_topic_recall``) and after-turn upkeep (memory review jobs) act only
for the person the task verifiably works for, its signed task context or
schedule run identity (``access_control.task_identity``). The manager gets
that person from a ``VerifiedPersonOf`` callable the agent supplies
(``custom_agents.access_runtime.verified_meta_person_id``), the same check the
task's tools use. The ``canonical_user_id`` a message's gateway context
carries is a claim no signature covers, so trusting it would let a message
that names another person load that person's private memories into the turn.
Work for no person (system work, or a task whose identity does not verify)
loads no personal memory.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, Callable, Optional, Protocol

from custom_messages.contracts.routing_v1 import GatewayContext, MessageMeta
from custom_messages.contracts.task_v1 import Task


# The person a task with this message meta verifiably works for, or None (module docstring).
VerifiedPersonOf = Callable[[Any], Optional[str]]


@dataclass(frozen=True)
class MemoryPluginContext:
    """What a hydration plugin may read for one turn.

    ``canonical_user_id`` is the verified person (module docstring), never the
    gateway context's claim; plugins must not read a person from
    ``gateway_context``.
    """

    agent_id: str
    team_id: Optional[str]
    named_agent_type: Optional[str]
    task: Task
    msg_meta: MessageMeta
    canonical_user_id: Optional[str]
    request_timezone: Optional[str]
    current_utc: str
    conversation_id: Optional[str]
    gateway_context: Optional[GatewayContext]
    memory_graph_repository: Any = None
    pinned_memory_repository: Any = None
    knowledge_repository: Any = None
    conversation_store: Any = None
    gateway_user_repository: Any = None
    config: dict[str, Any] | None = None
    logger: logging.Logger | None = None
    token_estimator: Any = None
    embed_text_fn: Any = None


@dataclass(frozen=True)
class MemoryHydrationSection:
    name: str
    text: str
    priority: int
    pinned: bool
    source: str
    plugin_id: str
    scope_kind: str
    degradable: bool = False
    warning: Optional[str] = None


@dataclass(frozen=True)
class MemoryMaintenanceEvent:
    event_type: str
    plugin_id: str
    payload: dict[str, Any]


@dataclass(frozen=True)
class ReviewJobRef:
    job_id: str
    plugin_id: str
    payload: dict[str, Any]


@dataclass(frozen=True)
class PruneResult:
    plugin_id: str
    pruned_count: int


class MemoryHydrationPlugin(Protocol):
    plugin_id: str
    default_priority: int
    default_pinned: bool
    scope_kind: str

    def enabled(self, config: dict[str, Any], context: MemoryPluginContext) -> bool:
        ...

    def hydrate(self, context: MemoryPluginContext) -> list[MemoryHydrationSection]:
        ...


class MemoryMaintenancePlugin(Protocol):
    plugin_id: str

    def after_turn(self, request: Any) -> list[MemoryMaintenanceEvent]:
        ...

    def enqueue_review_jobs(self, request: Any) -> list[ReviewJobRef]:
        ...

    def compact(self, reason: Any, request: Any) -> Any:
        ...

    def prune(self, now_utc: str) -> PruneResult:
        ...


class MemoryToolPlugin(Protocol):
    plugin_id: str

    def tool_specs(self) -> list[Any]:
        ...

    def command_specs(self) -> list[Any]:
        ...

    def authorize(self, action: str, actor: Any, scope: Any) -> bool:
        ...

    def execute(self, action: str, payload: dict[str, Any], context: MemoryPluginContext) -> Any:
        ...
