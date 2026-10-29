from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, Callable, Optional

from strands.agent.conversation_manager.conversation_manager import ConversationManager

from custom_agents.context_engine.base import (
    AssembledContext,
    ContextAssemblyRequest,
    ContextBudgetExceededError,
    ContextCompactionReason,
    ContextEnginePlugin,
    ContextSection,
    TurnFinalizationRequest,
)
from custom_agents.context_engine.config import merge_context_engine_config
from custom_agents.context_engine.thread_renderer import (
    BudgetedThreadContextRenderer,
    BudgetedThreadContextRendererConfig,
)
from custom_agents.conversation_compaction.base import CompactionResult
from custom_agents.filtered_tool_result_context import (
    FilteredToolResultConversationManager,
    merge_inner_loop_context_config,
)
from custom_agents.instance_delivery_context import build_instance_delivery_context
from custom_agents.prompt_context_sanitization import (
    render_current_request_context,
    render_incoming_message_summary,
)
from custom_agents.token_estimation import count_text_tokens
from custom_agents.tool_result_offloader import TaskAwareWorkspaceStorage
from custom_agents.workspace_prompt_context import build_workspace_turn_context
from runtime_settings.instance_layout import InstanceLayout, workspace_scope

from custom_messages.contracts.routing_v1 import MessageMeta
from custom_messages.contracts.task_v1 import Task
from custom_messages.enums import MessageMetaType


def _no_tool_setup_context(_meta: MessageMeta) -> str:
    return ""


def _no_person_role_context(_meta: MessageMeta) -> str:
    return ""


@dataclass
class ContextEngineAdapters:
    build_request_time_context: Callable[[MessageMeta], str]
    build_request_conversation_context: Callable[[MessageMeta, Optional[str]], str]
    build_team_routing_prompt_context: Callable[[], str]
    # The person the turn verifiably works for (signed task context or schedule
    # run identity, custom_agents.access_runtime.verified_meta_person_id), or
    # None. The working-folder line names their folder, the same folder the
    # file tools use (custom_tools.workspace_scope); a person id in message
    # fields alone never chooses it.
    verified_person_of: Callable[[MessageMeta], Optional[str]]
    # TeamLead's line of the person's known tool setup states
    # (custom_agents.tool_setup_runtime.tool_setup_context); empty for others.
    build_tool_setup_context: Callable[[MessageMeta], str] = _no_tool_setup_context
    # TeamLead's line naming the verified person's team role, for guidance only
    # (custom_agents.person_role_context.person_role_context); empty for others.
    build_person_role_context: Callable[[MessageMeta], str] = _no_person_role_context


class DefaultContextEngine:
    """Default outer-loop context assembly and accounting engine."""

    def __init__(
        self,
        *,
        agent_id: str,
        logger: logging.Logger,
        task_manager: Any,
        conversation_store: Any,
        conversation_compactor: Any,
        memory_graph_repository: Any = None,
        system_prompt: str = "",
        context_window_tokens: Optional[int] = None,
        turn_context_window: Optional[Callable[[], Optional[int]]] = None,
        soft_context_ratio: float = 0.85,
        reserved_output_tokens: int = 4096,
        model: Any = None,
        hard_limit_behavior: str = "error",
        thread_window: Optional[dict[str, Any]] = None,
        task_runtime_timing: Optional[dict[str, Any]] = None,
        plugins: Optional[list[ContextEnginePlugin]] = None,
        inner_loop_context: Optional[dict[str, Any]] = None,
        workspace_dir: str = ".",
        instance_root: Optional[str] = None,
        is_team_lead: bool = False,
        instance_config_path: Optional[str] = None,
        adapters: ContextEngineAdapters,
        memory_plugin_manager: Any = None,
    ) -> None:
        self.agent_id = agent_id
        self.logger = logger or logging.getLogger(__name__)
        self.task_manager = task_manager
        self.conversation_store = conversation_store
        self.conversation_compactor = conversation_compactor
        self.memory_graph_repository = memory_graph_repository
        self.system_prompt = system_prompt or ""
        self.context_window_tokens = context_window_tokens
        # The window of the model the turn being assembled uses, when it is not
        # the team's (a person's own model, custom_agents.person_model_runtime).
        # Read per assembly: one engine serves turns that run at once.
        self.turn_context_window = turn_context_window
        self.soft_context_ratio = self._bounded_ratio(soft_context_ratio, default=0.85)
        self.reserved_output_tokens = max(0, int(reserved_output_tokens or 0))
        self.model = model
        self.hard_limit_behavior = (hard_limit_behavior or "error").strip().lower()
        self.task_runtime_timing = dict(task_runtime_timing or {})
        self.inner_loop_context = merge_inner_loop_context_config(inner_loop_context)
        self.workspace_dir = workspace_dir or "."
        self.instance_root = instance_root
        self.is_team_lead = bool(is_team_lead)
        self.instance_config_path = instance_config_path
        self.adapters = adapters
        self.memory_plugin_manager = memory_plugin_manager
        self.plugins = list(plugins or [])
        self.thread_renderer = BudgetedThreadContextRenderer(
            config=self._thread_renderer_config(thread_window),
            estimate_tokens=self.estimate_tokens,
        )

    def assemble(self, request: ContextAssemblyRequest) -> AssembledContext:
        self._notify_plugins("ingest", request)
        sections: list[ContextSection] = []
        prompt_parts: list[str] = []
        current_request_context = ""
        degraded_sections = set(request.degraded_sections or ())

        # Account for the provider/system prompt even though it is passed to Strands separately.
        if self.system_prompt:
            sections.append(
                self._section(
                    name="system_prompt",
                    text=self.system_prompt,
                    priority=0,
                    pinned=True,
                    source="strands_agent.system_prompt",
                )
            )

        request_time_context = self.adapters.build_request_time_context(request.incoming_meta)
        self._append_prompt_section(
            sections,
            prompt_parts,
            name="request_time_context",
            text=request_time_context,
            priority=10,
            pinned=True,
            source="request_prompt_context.build_request_time_context",
        )

        if not request.isolated_recovery_task and not request.plan_step_task:
            request_conversation_context = self.adapters.build_request_conversation_context(
                request.incoming_meta,
                getattr(request.incoming_content, "problem_statement", None),
            )
            self._append_prompt_section(
                sections,
                prompt_parts,
                name="request_conversation_context",
                text=request_conversation_context,
                priority=20,
                pinned=True,
                source="request_prompt_context.build_request_conversation_context",
            )

        workspace_context = (
            ""
            if "workspace_user_context" in degraded_sections
            else build_workspace_turn_context(
                scope=workspace_scope(
                    self.workspace_dir, self.adapters.verified_person_of(request.identity_meta)
                ),
                soul_file=InstanceLayout.at(self.instance_root).soul_path
                if self.is_team_lead and self.instance_root
                else None,
            )
        )
        self._append_prompt_section(
            sections,
            prompt_parts,
            name="workspace_user_context",
            text=workspace_context,
            priority=24,
            pinned=True,
            source="workspace_prompt_context.build_workspace_turn_context",
        )

        delivery_context = (
            ""
            if "instance_delivery_context" in degraded_sections
            else build_instance_delivery_context(
                instance_config_path=self.instance_config_path,
                logger=self.logger,
            )
        )
        self._append_prompt_section(
            sections,
            prompt_parts,
            name="tool_setup_context",
            text=self._tool_setup_context(request, degraded_sections),
            priority=25,
            pinned=True,
            source="tool_setup_runtime.tool_setup_context",
        )
        self._append_prompt_section(
            sections,
            prompt_parts,
            name="person_role_context",
            text=self.adapters.build_person_role_context(request.incoming_meta),
            priority=25,
            pinned=True,
            source="person_role_context.person_role_context",
        )
        self._append_prompt_section(
            sections,
            prompt_parts,
            name="instance_delivery_context",
            text=delivery_context,
            priority=24,
            pinned=True,
            source="instance_delivery_context.build_instance_delivery_context",
        )

        incoming_task = (
            request.incoming_content
            if isinstance(request.incoming_content, Task)
            and request.incoming_content.task_type == MessageMetaType.REQUEST
            else None
        )
        # What earlier turns of this task did before it waited
        # (custom_agents.turn_work_notes); empty on the task's first turn.
        earlier_turn_work = (
            self.task_manager.render_earlier_turn_work_markdown(request.execution_task_id)
            if incoming_task is not None
            else ""
        )
        if incoming_task is not None:
            current_request_context = render_current_request_context(
                incoming_task,
                request.incoming_meta,
                continuing=bool(earlier_turn_work),
            )
            if current_request_context:
                sections.append(
                    self._section(
                        name="current_request",
                        text=current_request_context,
                        priority=110,
                        pinned=True,
                        source="prompt_context_sanitization.render_current_request_context",
                    )
                )

        team_routing_context = self.adapters.build_team_routing_prompt_context()
        if team_routing_context:
            team_routing_context = f"Team Routing Context:\n{team_routing_context}"
        self._append_prompt_section(
            sections,
            prompt_parts,
            name="team_routing_context",
            text=team_routing_context,
            priority=30,
            pinned=True,
            source="UniversalAgent._build_team_routing_prompt_context",
        )

        if isinstance(request.incoming_content, Task) and request.incoming_content.task_type == MessageMetaType.REQUEST:
            if request.isolated_recovery_task or request.plan_step_task:
                # Focused turns omit general history, but retain answers to
                # this task's questions. Read them by the original task id.
                if request.execution_task_id is None:
                    raise ValueError("Focused task context requires its original execution task id.")
                self._append_prompt_section(
                    sections,
                    prompt_parts,
                    name="task_clarifications",
                    text=self.task_manager.render_task_query_history_markdown(request.execution_task_id, latest_only=True),
                    priority=70,
                    pinned=True,
                    source="TaskManager.render_task_query_history_markdown",
                )
                self._append_earlier_turn_work(sections, prompt_parts, earlier_turn_work)
            if not request.isolated_recovery_task and not request.plan_step_task:
                if self.memory_plugin_manager is not None:
                    for memory_section in self.memory_plugin_manager.hydrate(request):
                        if memory_section.degradable and (
                            memory_section.name in degraded_sections
                            or (memory_section.plugin_id and memory_section.plugin_id in degraded_sections)
                        ):
                            continue
                        sections.append(memory_section)
                        prompt_parts.append(memory_section.text)

                execution_digest = (
                    ""
                    if "execution_digest" in degraded_sections
                    else self.task_manager.render_execution_digest_markdown(
                        task_id=request.incoming_content.task_id,
                    )
                )

                markdown_summary = (
                    ""
                    if "task_history" in degraded_sections
                    else self.task_manager.summarize_task_history_markdown(
                        task_id=request.incoming_content.task_id,
                        include_execution_digest=False,
                    )
                )
                if markdown_summary:
                    markdown_summary = f"Task History:\n{markdown_summary}"

                if request.incoming_content.parent_task_id is None and getattr(request.incoming_meta, "conversation_id", None):
                    available_thread_tokens = self._available_thread_tokens(
                        sections=sections,
                        incoming_message_summary=self._render_incoming_message_context(request),
                        pending_pinned_texts=[execution_digest, markdown_summary],
                    )
                    rendered_thread = self.thread_renderer.render(
                        store=self.conversation_store,
                        conversation_id=request.incoming_meta.conversation_id,
                        latest_request_text=request.incoming_content.problem_statement or "",
                        exclude_task_id=request.incoming_content.task_id,
                        available_tokens=available_thread_tokens,
                    )
                    conversation_context = rendered_thread.text
                    if conversation_context:
                        conversation_context = f"Conversation Thread Context:\n{conversation_context}"
                    self._append_prompt_section(
                        sections,
                        prompt_parts,
                        name="conversation_thread_context",
                        text=conversation_context,
                        priority=50,
                        pinned=False,
                        source="BudgetedThreadContextRenderer.render",
                    )

                self._append_prompt_section(
                    sections,
                    prompt_parts,
                    name="execution_digest",
                    text=execution_digest,
                    priority=60,
                    pinned=True,
                    source="TaskManager.render_execution_digest_markdown",
                )

                self._append_prompt_section(
                    sections,
                    prompt_parts,
                    name="task_history",
                    text=markdown_summary,
                    priority=70,
                    pinned=True,
                    source="TaskManager.summarize_task_history_markdown",
                )
                self._append_earlier_turn_work(sections, prompt_parts, earlier_turn_work)

                task_runtime_timing_context = (
                    ""
                    if "task_runtime_timing" in degraded_sections
                    else self._render_task_runtime_timing_context(request)
                )
                self._append_prompt_section(
                    sections,
                    prompt_parts,
                    name="task_runtime_timing",
                    text=task_runtime_timing_context,
                    priority=80,
                    pinned=True,
                    source="TaskManager.render_task_runtime_timing_markdown",
                )

        incoming_message_context = self._render_incoming_message_context(request)
        self._append_prompt_section(
            sections,
            prompt_parts,
            name="incoming_message",
            text=incoming_message_context,
            priority=100,
            pinned=True,
            source="prompt_context_sanitization.render_incoming_message_summary",
        )

        provider_prompt_suffix = str(request.provider_prompt_suffix or "")
        if provider_prompt_suffix:
            sections.append(
                self._section(
                    name="provider_prompt_suffix",
                    text=provider_prompt_suffix,
                    priority=120,
                    pinned=True,
                    source="ContextAssemblyRequest.provider_prompt_suffix",
                )
            )

        prompt = self._build_prompt(
            prompt_parts,
            final_text=current_request_context,
            suffix_text=provider_prompt_suffix,
        )
        source_lengths = self._source_lengths(sections)
        estimated_tokens = self._estimate_prompt_tokens(prompt)
        max_tokens = self._window()
        hard_limit = self._hard_limit_tokens()
        soft_limit = int(hard_limit * self.soft_context_ratio) if hard_limit else None
        fill_ratio = round(estimated_tokens / hard_limit, 4) if hard_limit else None
        warnings = []
        if soft_limit is not None and estimated_tokens >= soft_limit:
            warning = {
                "warning": "context_soft_limit_reached",
                "estimated_tokens": estimated_tokens,
                "soft_limit_tokens": soft_limit,
                "hard_limit_tokens": hard_limit,
                "max_context_tokens": max_tokens,
                "reserved_output_tokens": self.reserved_output_tokens,
            }
            warnings.append(warning)
            self.logger.warning(
                "[%s] Context soft budget reached: estimated_tokens=%s soft_limit_tokens=%s "
                "hard_limit_tokens=%s source_lengths=%s",
                self.agent_id,
                estimated_tokens,
                soft_limit,
                hard_limit,
                source_lengths,
            )
            warning_text = self._render_preflight_warning(
                warning=warning,
                source_lengths=source_lengths,
            )
            warning_prompt = self._build_prompt(
                prompt_parts + [warning_text],
                final_text=current_request_context,
                suffix_text=provider_prompt_suffix,
            )
            warning_estimated_tokens = self._estimate_prompt_tokens(warning_prompt)
            if hard_limit is None or warning_estimated_tokens <= hard_limit:
                self._append_prompt_section(
                    sections,
                    prompt_parts,
                    name="context_preflight_warning",
                    text=warning_text,
                    priority=90,
                    pinned=True,
                    source="DefaultContextEngine.preflight_enforcement",
                )
                prompt = self._build_prompt(
                    prompt_parts,
                    final_text=current_request_context,
                    suffix_text=provider_prompt_suffix,
                )
                source_lengths = self._source_lengths(sections)
                estimated_tokens = warning_estimated_tokens
                fill_ratio = round(estimated_tokens / hard_limit, 4) if hard_limit else None
            else:
                warnings.append(
                    {
                        "warning": "context_soft_limit_warning_omitted",
                        "reason": "warning_section_would_exceed_hard_limit",
                        "estimated_tokens_with_warning": warning_estimated_tokens,
                    }
                )

        if hard_limit is not None and estimated_tokens > hard_limit:
            source_lengths = self._source_lengths(sections)
            message = (
                "Assembled context exceeds the hard input budget: "
                f"estimated_tokens={estimated_tokens}, hard_limit_tokens={hard_limit}, "
                f"max_context_tokens={max_tokens}, reserved_output_tokens={self.reserved_output_tokens}. "
                "Reduce pinned context, lower retrieved history, or increase llm.context_window_tokens."
            )
            self.logger.error("[%s] %s", self.agent_id, message)
            if self.hard_limit_behavior in {"error", "recover_then_error"}:
                raise ContextBudgetExceededError(
                    message,
                    estimated_tokens=estimated_tokens,
                    hard_limit_tokens=hard_limit,
                    max_context_tokens=max_tokens,
                    reserved_output_tokens=self.reserved_output_tokens,
                    source_lengths=source_lengths,
                )
            warnings.append(
                {
                    "warning": "context_hard_limit_exceeded",
                    "estimated_tokens": estimated_tokens,
                    "hard_limit_tokens": hard_limit,
                    "hard_limit_behavior": self.hard_limit_behavior,
                }
            )

        assembly = AssembledContext(
            sections=sections,
            prompt=prompt,
            char_count=len(prompt) + len(self.system_prompt),
            estimated_tokens=estimated_tokens,
            source_lengths=source_lengths,
            warnings=warnings,
            max_context_tokens=max_tokens,
            soft_limit_tokens=soft_limit,
            hard_limit_tokens=hard_limit,
            reserved_output_tokens=self.reserved_output_tokens,
            context_fill_ratio=fill_ratio,
            context_epoch_id=self._latest_context_epoch_id(request),
            recovery_attempt=int(request.recovery_attempt or 0),
            degraded_sections=sorted(degraded_sections),
        )
        for plugin in self.plugins:
            hook = getattr(plugin, "assemble", None)
            if not callable(hook):
                continue
            try:
                plugin_result = hook(request, assembly)
            except Exception as exc:
                self.logger.warning("[%s] Context plugin assemble hook failed: %s", self.agent_id, exc)
                continue
            if plugin_result is not None:
                assembly = plugin_result
        return assembly

    def compact(
        self,
        reason: ContextCompactionReason,
        request: ContextAssemblyRequest | None = None,
    ) -> CompactionResult | None:
        for plugin in self.plugins:
            hook = getattr(plugin, "compact", None)
            if not callable(hook):
                continue
            try:
                result = hook(reason, request)
            except Exception as exc:
                self.logger.warning("[%s] Context plugin compact hook failed: %s", self.agent_id, exc)
                continue
            if result is not None:
                return result
        if self.conversation_store is None or self.conversation_compactor is None or request is None:
            return None
        conversation_id = getattr(request.incoming_meta, "conversation_id", None)
        if not conversation_id:
            return None
        return self.conversation_compactor.compact_if_needed(
            store=self.conversation_store,
            conversation_id=conversation_id,
            latest_request_text=getattr(request.incoming_content, "problem_statement", "") or "",
            exclude_task_id=getattr(request.incoming_content, "task_id", None),
        )

    def after_turn(self, request: TurnFinalizationRequest) -> None:
        if self.memory_plugin_manager is not None:
            self.memory_plugin_manager.after_turn(request)
            self.memory_plugin_manager.enqueue_review_jobs(request)
        self._notify_plugins("after_turn", request)

    def post_invocation(
        self,
        request: ContextAssemblyRequest,
        assembly: AssembledContext,
        **kwargs: Any,
    ) -> None:
        self._notify_plugins("post_invocation", request, assembly, **kwargs)

    def build_inner_loop_conversation_manager(
        self, storage: TaskAwareWorkspaceStorage
    ) -> ConversationManager | None:
        """The inner-loop manager, saving results in ``storage`` (the per-call offloader's)."""

        if not self.inner_loop_context.get("enabled", True):
            return None
        return FilteredToolResultConversationManager(
            config=self.inner_loop_context,
            storage=storage,
            logger_=self.logger,
            model=self.model,
        )

    def _available_thread_tokens(
        self,
        *,
        sections: list[ContextSection],
        incoming_message_summary: str,
        pending_pinned_texts: list[str],
    ) -> int | None:
        max_tokens = self._window()
        if not max_tokens:
            return None
        hard_limit = self._hard_limit_tokens()
        if not hard_limit:
            return None
        soft_limit = int(hard_limit * self.soft_context_ratio)
        reserved = self.estimate_tokens(incoming_message_summary)
        reserved += sum(section.estimated_tokens for section in sections)
        reserved += sum(self.estimate_tokens(text) for text in pending_pinned_texts if text)
        return max(0, soft_limit - reserved)

    def _tool_setup_context(self, request: ContextAssemblyRequest, degraded_sections: set[str]) -> str:
        """TeamLead's line of the person's known tool setup states (empty when degraded or for others)."""

        if "tool_setup_context" in degraded_sections:
            return ""
        return self.adapters.build_tool_setup_context(request.incoming_meta)

    def _append_earlier_turn_work(
        self, sections: list[ContextSection], prompt_parts: list[str], text: str
    ) -> None:
        """Pinned: without it a resumed turn starts its task over (turn_work_notes)."""

        self._append_prompt_section(
            sections,
            prompt_parts,
            name="earlier_turn_work",
            text=text,
            priority=72,
            pinned=True,
            source="TaskManager.render_earlier_turn_work_markdown",
        )

    def _append_prompt_section(
        self,
        sections: list[ContextSection],
        prompt_parts: list[str],
        *,
        name: str,
        text: str,
        priority: int,
        pinned: bool,
        source: str,
    ) -> None:
        if not text:
            return
        sections.append(
            self._section(
                name=name,
                text=text,
                priority=priority,
                pinned=pinned,
                source=source,
            )
        )
        prompt_parts.append(text)

    def _section(
        self,
        *,
        name: str,
        text: str,
        priority: int,
        pinned: bool,
        source: str,
    ) -> ContextSection:
        return ContextSection(
            name=name,
            text=text,
            priority=priority,
            pinned=pinned,
            source=source,
            char_count=len(text or ""),
            estimated_tokens=self.estimate_tokens(text),
        )

    def _render_incoming_message_context(self, request: ContextAssemblyRequest) -> str:
        return render_incoming_message_summary(
            content=request.incoming_content,
            header=request.incoming_header,
            meta=request.incoming_meta,
        )

    def _render_task_runtime_timing_context(self, request: ContextAssemblyRequest) -> str:
        config = self.task_runtime_timing
        if config.get("enabled", True) is False:
            return ""
        if not isinstance(request.incoming_content, Task):
            return ""
        renderer = getattr(self.task_manager, "render_task_runtime_timing_markdown", None)
        if not callable(renderer):
            return ""
        return renderer(
            request.incoming_content.task_id,
            include_message_created_at=bool(config.get("include_message_created_at", True)),
            include_handled_at=bool(config.get("include_handled_at", True)),
            include_processing_started_at=bool(config.get("include_processing_started_at", True)),
            include_processing_finished_at=bool(config.get("include_processing_finished_at", False)),
        )

    def _latest_context_epoch_id(self, request: ContextAssemblyRequest) -> str | None:
        conversation_id = getattr(request.incoming_meta, "conversation_id", None)
        if not conversation_id or self.conversation_store is None:
            return None
        getter = getattr(self.conversation_store, "get_latest_context_epoch", None)
        if not callable(getter):
            return None
        try:
            epoch = getter(conversation_id)
        except Exception:
            return None
        return epoch.get("context_epoch_id") if isinstance(epoch, dict) else None

    def _build_prompt(
        self,
        prompt_parts: list[str],
        *,
        final_text: str = "",
        suffix_text: str = "",
    ) -> str:
        parts = [part for part in prompt_parts if part]
        if final_text:
            parts.append(final_text)
        if suffix_text:
            parts.append(suffix_text)
        return "\n\n".join(parts)

    def _estimate_prompt_tokens(self, prompt: str) -> int:
        return self.estimate_tokens(prompt) + self.estimate_tokens(self.system_prompt)

    def _window(self) -> Optional[int]:
        """The context window of the model the current turn uses (``turn_context_window``), else the team's."""

        override = self.turn_context_window() if self.turn_context_window is not None else None
        return override or self.context_window_tokens

    def _hard_limit_tokens(self) -> int | None:
        window = self._window()
        if not window:
            return None
        return max(1, int(window) - self.reserved_output_tokens)

    def _source_lengths(self, sections: list[ContextSection]) -> list[dict[str, Any]]:
        return [
            {
                "name": section.name,
                "source": section.source,
                "chars": section.char_count,
                "estimated_tokens": section.estimated_tokens,
                "pinned": section.pinned,
                "plugin_id": section.plugin_id,
                "scope_kind": section.scope_kind,
                "degradable": section.degradable,
            }
            for section in sections
        ]

    def _render_preflight_warning(
        self,
        *,
        warning: dict[str, Any],
        source_lengths: list[dict[str, Any]],
    ) -> str:
        largest = sorted(
            source_lengths,
            key=lambda item: int(item.get("estimated_tokens") or 0),
            reverse=True,
        )[:6]
        lines = [
            "Context Budget Warning:",
            f"- Estimated input tokens: {warning['estimated_tokens']}",
            f"- Soft input budget: {warning['soft_limit_tokens']}",
            f"- Hard input budget: {warning['hard_limit_tokens']}",
            f"- Reserved output tokens: {warning['reserved_output_tokens']}",
            "- Strategy: avoid broad retrieval; use targeted context recovery tools for omitted history.",
        ]
        if largest:
            lines.append("- Largest context sections:")
            lines.extend(
                f"  - {item['name']}: {item['estimated_tokens']} estimated tokens"
                for item in largest
            )
        return "\n".join(lines)

    def _thread_renderer_config(self, config: Optional[dict[str, Any]]) -> BudgetedThreadContextRendererConfig:
        cfg = dict(config or {})
        defaults = BudgetedThreadContextRendererConfig()
        return BudgetedThreadContextRendererConfig(
            recent_full_turns=int(
                cfg.get("recent_full_turns", defaults.recent_full_turns)
                or defaults.recent_full_turns
            ),
            per_turn_summary_max_tokens=int(
                cfg.get("per_turn_summary_max_tokens", defaults.per_turn_summary_max_tokens)
                or defaults.per_turn_summary_max_tokens
            ),
            max_turns_to_load=int(
                cfg.get("max_turns_to_load", defaults.max_turns_to_load)
                or defaults.max_turns_to_load
            ),
            omitted_notice_enabled=bool(
                cfg.get("omitted_notice_enabled", defaults.omitted_notice_enabled)
            ),
            include_turn_ids=bool(cfg.get("include_turn_ids", defaults.include_turn_ids)),
            retrieval_tool_hint=bool(cfg.get("retrieval_tool_hint", defaults.retrieval_tool_hint)),
        )

    def _notify_plugins(self, hook_name: str, *args: Any, **kwargs: Any) -> None:
        for plugin in self.plugins:
            hook = getattr(plugin, hook_name, None)
            if not callable(hook):
                continue
            try:
                hook(*args, **kwargs)
            except Exception as exc:
                self.logger.warning("[%s] Context plugin %s hook failed: %s", self.agent_id, hook_name, exc)

    def estimate_tokens(self, text: str) -> int:
        return count_text_tokens(self.model, text)

    @staticmethod
    def _bounded_ratio(value: float, *, default: float) -> float:
        try:
            parsed = float(value)
        except (TypeError, ValueError):
            return default
        return min(1.0, max(0.01, parsed))


def context_engine_kwargs_from_config(config: Optional[dict[str, Any]]) -> dict[str, Any]:
    merged = merge_context_engine_config(config)
    return {
        "soft_context_ratio": merged.get("max_context_ratio", 0.85),
        "reserved_output_tokens": merged.get("reserved_output_tokens", 4096),
        "hard_limit_behavior": merged.get("hard_limit_behavior", "error"),
        "thread_window": merged.get("thread_window", {}),
        "task_runtime_timing": merged.get("task_runtime_timing", {}),
    }
