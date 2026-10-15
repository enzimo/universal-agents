from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any, Optional

from custom_agents.context_engine.base import ContextAssemblyRequest, ContextSection
from custom_agents.memory_plugins.base import MemoryHydrationSection, MemoryPluginContext, VerifiedPersonOf
from custom_agents.memory_plugins.config import normalize_memory_config, plugin_settings
from custom_agents.memory_plugins.maintenance import (
    DomainTopicTaggerPlugin,
    MemoryNotificationPlugin,
    PinnedMemoryPrunerPlugin,
    PinnedMemoryReviewPlugin,
)
from custom_agents.memory_plugins.registry import build_memory_plugins
from custom_messages.contracts.task_v1 import Task


class MemoryPluginManager:
    """Coordinates repo-internal memory hydration plugins before thread budgeting.

    ``verified_person_of`` names the person a turn's message verifiably works
    for (``memory_plugins.base``); personal hydration and review jobs use only
    that person.
    """

    def __init__(
        self,
        *,
        agent_id: str,
        team_id: Optional[str],
        named_agent_type: Optional[str],
        verified_person_of: VerifiedPersonOf,
        config: Optional[dict[str, Any]] = None,
        logger: Optional[logging.Logger] = None,
        memory_review_store: Any = None,
        memory_graph_repository: Any = None,
        pinned_memory_repository: Any = None,
        knowledge_repository: Any = None,
        conversation_store: Any = None,
        gateway_user_repository: Any = None,
        token_estimator: Any = None,
        embed_text_fn: Any = None,
        hydration_plugins: Optional[list[Any]] = None,
        maintenance_plugins: Optional[list[Any]] = None,
        review_job_sink: Any = None,
    ) -> None:
        self.agent_id = agent_id
        self.team_id = team_id
        self.named_agent_type = named_agent_type
        self.verified_person_of = verified_person_of
        self.config = normalize_memory_config(config)
        self.logger = logger or logging.getLogger(__name__)
        self.memory_review_store = memory_review_store
        self.memory_graph_repository = memory_graph_repository
        self.pinned_memory_repository = pinned_memory_repository
        self.knowledge_repository = knowledge_repository
        self.conversation_store = conversation_store
        self.gateway_user_repository = gateway_user_repository
        self.token_estimator = token_estimator
        self.embed_text_fn = embed_text_fn
        self.review_job_sink = review_job_sink
        self.hydration_plugins = hydration_plugins if hydration_plugins is not None else build_memory_plugins(self.config)
        self.maintenance_plugins = maintenance_plugins if maintenance_plugins is not None else [
            PinnedMemoryPrunerPlugin(repository=self.pinned_memory_repository),
            PinnedMemoryReviewPlugin(
                memory_review_store=self.memory_review_store,
                conversation_store=self.conversation_store,
                team_id=self.team_id,
                named_agent_type=self.named_agent_type,
                agent_id=self.agent_id,
                memory_config=self.config,
                verified_person_of=self.verified_person_of,
            ),
            DomainTopicTaggerPlugin(
                conversation_store=self.conversation_store,
                team_id=self.team_id,
                agent_id=self.agent_id,
            ),
            MemoryNotificationPlugin(),
        ]

    @property
    def enabled(self) -> bool:
        return bool((self.config.get("plugin_manager") or {}).get("enabled", True))

    def build_context(self, request: ContextAssemblyRequest, *, current_utc: Optional[str] = None) -> MemoryPluginContext:
        meta = request.incoming_meta
        gateway_context = getattr(meta, "gateway_context", None)
        return MemoryPluginContext(
            agent_id=self.agent_id,
            team_id=self.team_id,
            named_agent_type=self.named_agent_type,
            task=request.incoming_content,
            msg_meta=meta,
            canonical_user_id=self.verified_person_of(meta),
            request_timezone=getattr(meta, "request_timezone", None),
            current_utc=current_utc or datetime.now(timezone.utc).isoformat(),
            conversation_id=getattr(meta, "conversation_id", None),
            gateway_context=gateway_context,
            memory_graph_repository=self.memory_graph_repository,
            pinned_memory_repository=self.pinned_memory_repository,
            knowledge_repository=self.knowledge_repository,
            conversation_store=self.conversation_store,
            gateway_user_repository=self.gateway_user_repository,
            config=self.config,
            logger=self.logger,
            token_estimator=self.token_estimator,
            embed_text_fn=self.embed_text_fn,
        )

    def hydrate(self, request: ContextAssemblyRequest) -> list[ContextSection]:
        if not self.enabled or not isinstance(request.incoming_content, Task):
            return []
        context = self.build_context(request)
        sections: list[MemoryHydrationSection] = []
        warnings: list[MemoryHydrationSection] = []
        fail_open = bool((self.config.get("plugin_manager") or {}).get("fail_open", True))
        for plugin in self.hydration_plugins:
            plugin_id = getattr(plugin, "plugin_id", plugin.__class__.__name__)
            settings = plugin_settings(self.config, plugin_id)
            try:
                if not plugin.enabled(self.config, context):
                    continue
                sections.extend(plugin.hydrate(context))
            except Exception as exc:
                self.logger.warning(
                    "Memory hydration plugin failed. event=memory_plugin_hydration_failed plugin_id=%s",
                    plugin_id,
                    exc_info=True,
                )
                pinned = bool(settings.get("pinned", getattr(plugin, "default_pinned", False)))
                if pinned:
                    warnings.append(
                        MemoryHydrationSection(
                            name=f"{plugin_id}_hydration_warning",
                            text=f"Memory hydration warning: {plugin_id} could not be loaded for this turn.",
                            priority=int(settings.get("priority", getattr(plugin, "default_priority", 50))),
                            pinned=True,
                            source="MemoryPluginManager.hydrate",
                            plugin_id=plugin_id,
                            scope_kind=getattr(plugin, "scope_kind", "unknown"),
                            degradable=False,
                            warning=str(exc),
                        )
                    )
                if not fail_open and pinned:
                    raise
        return [self.to_context_section(section) for section in sorted(sections + warnings, key=lambda item: item.priority)]

    def to_context_section(self, section: MemoryHydrationSection) -> ContextSection:
        text = section.text or ""
        estimator = self.token_estimator
        estimated_tokens = estimator(text) if callable(estimator) else max(1, len(text) // 4)
        return ContextSection(
            name=section.name,
            text=text,
            priority=section.priority,
            pinned=section.pinned,
            source=section.source,
            char_count=len(text),
            estimated_tokens=estimated_tokens,
            plugin_id=section.plugin_id,
            scope_kind=section.scope_kind,
            degradable=section.degradable,
            warning=section.warning,
        )

    def after_turn(self, request: Any) -> list[Any]:
        events: list[Any] = []
        for plugin in self.maintenance_plugins:
            hook = getattr(plugin, "after_turn", None)
            if not callable(hook):
                continue
            try:
                events.extend(hook(request) or [])
            except Exception:
                self.logger.warning(
                    "Memory maintenance plugin after_turn failed. event=memory_plugin_after_turn_failed plugin_id=%s",
                    getattr(plugin, "plugin_id", plugin.__class__.__name__),
                    exc_info=True,
                )
        return events

    def enqueue_review_jobs(self, request: Any) -> list[Any]:
        jobs: list[Any] = []
        for plugin in self.maintenance_plugins:
            hook = getattr(plugin, "enqueue_review_jobs", None)
            if not callable(hook):
                continue
            try:
                jobs.extend(hook(request) or [])
            except Exception:
                self.logger.warning(
                    "Memory maintenance plugin enqueue failed. event=memory_plugin_review_enqueue_failed plugin_id=%s",
                    getattr(plugin, "plugin_id", plugin.__class__.__name__),
                    exc_info=True,
                )
        if self.review_job_sink is not None:
            for job in jobs:
                try:
                    self.review_job_sink(job)
                except Exception:
                    self.logger.warning(
                        "Memory review job sink failed. event=memory_review_job_sink_failed plugin_id=%s job_id=%s",
                        getattr(job, "plugin_id", None),
                        getattr(job, "job_id", None),
                        exc_info=True,
                    )
        return jobs

    def prune(self, now_utc: Optional[str] = None) -> list[Any]:
        timestamp = now_utc or datetime.now(timezone.utc).isoformat()
        results: list[Any] = []
        for plugin in self.maintenance_plugins:
            hook = getattr(plugin, "prune", None)
            if not callable(hook):
                continue
            try:
                results.append(hook(timestamp))
            except Exception:
                self.logger.warning(
                    "Memory maintenance plugin prune failed. event=memory_plugin_prune_failed plugin_id=%s",
                    getattr(plugin, "plugin_id", plugin.__class__.__name__),
                    exc_info=True,
                )
        return results
