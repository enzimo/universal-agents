from __future__ import annotations

import re
from typing import Any

from custom_agents.memory_plugins.base import MemoryHydrationSection, MemoryPluginContext
from custom_agents.memory_plugins.config import plugin_settings
from custom_agents.pinned_memory_metadata import public_pinned_memory_metadata
from storage.conversation_repository import normalize_topic_tag


class BaseHydrationPlugin:
    plugin_id = ""
    default_priority = 50
    default_pinned = False
    default_degradable = False
    scope_kind = "global"

    def enabled(self, config: dict[str, Any], context: MemoryPluginContext) -> bool:
        settings = plugin_settings(config, self.plugin_id)
        return bool(settings.get("enabled", True))

    def _section(self, context: MemoryPluginContext, *, name: str, text: str, source: str) -> MemoryHydrationSection:
        settings = plugin_settings(context.config or {}, self.plugin_id)
        return MemoryHydrationSection(
            name=name,
            text=text,
            priority=int(settings.get("priority", self.default_priority)),
            pinned=bool(settings.get("pinned", self.default_pinned)),
            source=source,
            plugin_id=self.plugin_id,
            scope_kind=self.scope_kind,
            degradable=bool(settings.get("degradable", self.default_degradable)),
        )


class CorememHydrationPlugin(BaseHydrationPlugin):
    plugin_id = "coremem"
    default_priority = 34
    default_pinned = True
    scope_kind = "team_id"

    def enabled(self, config: dict[str, Any], context: MemoryPluginContext) -> bool:
        return (
            super().enabled(config, context)
            and bool((config.get("coremem") or {}).get("enabled", True))
            and bool((config.get("coremem") or {}).get("hydrate_into_context", True))
            and bool(context.team_id)
            and context.pinned_memory_repository is not None
        )

    def hydrate(self, context: MemoryPluginContext) -> list[MemoryHydrationSection]:
        rows = context.pinned_memory_repository.list_coremem(
            team_id=context.team_id,
            now_utc=context.current_utc,
        )
        if not rows:
            return []
        lines = ["## Pinned Memory Context", "", "### Core Memory"]
        for row in rows:
            details = [f"valid_from_utc={row.valid_from_utc}"]
            if row.invalid_after_utc:
                details.append(f"invalid_after_utc={row.invalid_after_utc}")
            if row.tags:
                details.append(f"tags={','.join(row.tags)}")
            lines.append(f"[{row.memory_id}] {row.fact_text}")
            lines.append(f"    {'; '.join(details)}")
            metadata = public_pinned_memory_metadata(row.metadata)
            if metadata:
                lines.append(f"    metadata={metadata}")
        return [self._section(context, name="coremem", text="\n".join(lines), source="PinnedMemoryRepository.list_coremem")]


class UsermemHydrationPlugin(BaseHydrationPlugin):
    """The verified person's personal facts; none for work for no person (``memory_plugins.base``)."""

    plugin_id = "usermem"
    default_priority = 35
    default_pinned = True
    scope_kind = "canonical_user_id"

    def enabled(self, config: dict[str, Any], context: MemoryPluginContext) -> bool:
        return (
            super().enabled(config, context)
            and bool((config.get("usermem") or {}).get("enabled", True))
            and bool((config.get("usermem") or {}).get("hydrate_into_context", True))
            and bool(context.canonical_user_id)
            and context.pinned_memory_repository is not None
        )

    def hydrate(self, context: MemoryPluginContext) -> list[MemoryHydrationSection]:
        rows = context.pinned_memory_repository.list_usermem(
            canonical_user_id=context.canonical_user_id,
            now_utc=context.current_utc,
        )
        if not rows:
            return []
        lines = ["## User Memory"]
        for row in rows:
            details = [f"valid_from_utc={row.valid_from_utc}"]
            if row.invalid_after_utc:
                details.append(f"invalid_after_utc={row.invalid_after_utc}")
            if row.tags:
                details.append(f"tags={','.join(row.tags)}")
            lines.append(f"[{row.memory_id}] {row.fact_text}")
            lines.append(f"    {'; '.join(details)}")
            metadata = public_pinned_memory_metadata(row.metadata)
            if metadata:
                lines.append(f"    metadata={metadata}")
        return [self._section(context, name="usermem", text="\n".join(lines), source="PinnedMemoryRepository.list_usermem")]


class DomainTopicRecallPlugin(BaseHydrationPlugin):
    """Hints to the verified person's own earlier threads that share a topic tag with the request.

    Threads belong to people, and the store's no-person query returns
    team-level tags on anyone's threads, so work for no person gets no hints
    (``memory_plugins.base``).
    """

    plugin_id = "domain_topic_recall"
    default_priority = 44
    default_pinned = False
    default_degradable = True
    scope_kind = "team_and_user"

    def enabled(self, config: dict[str, Any], context: MemoryPluginContext) -> bool:
        return (
            super().enabled(config, context)
            and bool((config.get("domain") or {}).get("enabled", True))
            and bool(context.canonical_user_id)
            and context.conversation_store is not None
        )

    def hydrate(self, context: MemoryPluginContext) -> list[MemoryHydrationSection]:
        request_text = str(getattr(context.task, "problem_statement", "") or "").strip()
        if not request_text:
            return []
        domain_config = dict((context.config or {}).get("domain") or {})
        known_tags = context.conversation_store.list_known_thread_tags(
            team_id=context.team_id,
            canonical_user_id=context.canonical_user_id,
            limit=int(domain_config.get("known_tag_scan_limit", 200)),
        )
        matched_tags = self._matched_tags(request_text, [item["tag"] for item in known_tags])
        if not matched_tags:
            return []
        matches = context.conversation_store.find_threads_by_tags(
            tags=matched_tags,
            team_id=context.team_id,
            canonical_user_id=context.canonical_user_id,
            limit=int(domain_config.get("max_recall_threads", 5)),
        )
        matches = [
            item for item in matches
            if item.get("conversation_id") != context.conversation_id
        ]
        if not matches:
            return []
        lines = [
            "## Related Topic Thread Hints",
            "These are compact pointers to prior tagged threads. Do not assume the full prior thread is in context.",
        ]
        for item in matches:
            tags = ", ".join(item.get("matched_tags") or [])
            preview = str(item.get("subject_preview") or "").replace("\n", " ").strip()
            if len(preview) > 120:
                preview = preview[:117] + "..."
            lines.append(
                f"- conversation_id={item.get('conversation_id')} tags={tags} "
                f"updated_at={item.get('updated_at')} preview={preview or '-'}"
            )
        return [
            self._section(
                context,
                name="domain_topic_recall",
                text="\n".join(lines),
                source="ConversationStore.find_threads_by_tags",
            )
        ]

    @staticmethod
    def _matched_tags(request_text: str, known_tags: list[str]) -> list[str]:
        normalized_text = request_text.lower()
        explicit_tags = {
            normalize_topic_tag(match)
            for match in re.findall(r"#([A-Za-z0-9_-]+)", request_text)
        }
        matched: list[str] = []
        for tag in known_tags:
            normalized_tag = normalize_topic_tag(tag)
            readable = normalized_tag.replace("-", " ")
            if normalized_tag in explicit_tags or normalized_tag in normalized_text or readable in normalized_text:
                matched.append(normalized_tag)
        return matched


class KnowledgeRecallPlugin(BaseHydrationPlugin):
    """Pointers to knowledge pages that match the request: team pages, and the verified person's own.

    Only titles and summaries, so the turn stays small; the agent reads a page
    with ``knowledge_read`` when it needs it. Personal pages come only from
    the verified person (``memory_plugins.base``); work for no person sees
    team pages only.
    """

    plugin_id = "knowledge_recall"
    default_priority = 45
    default_pinned = False
    default_degradable = True
    scope_kind = "team_and_user"
    default_max_pages = 5

    def enabled(self, config: dict[str, Any], context: MemoryPluginContext) -> bool:
        return super().enabled(config, context) and context.knowledge_repository is not None

    def hydrate(self, context: MemoryPluginContext) -> list[MemoryHydrationSection]:
        from knowledge_base.records import KnowledgeActor
        from knowledge_base.service import KnowledgeService
        from knowledge_base.vocabulary import ActorKind
        from runtime_settings.knowledge import KnowledgeSettings

        request_text = str(getattr(context.task, "problem_statement", "") or "").strip()
        if not request_text or not context.team_id:
            return []
        settings = plugin_settings(context.config or {}, self.plugin_id)
        limit = int(settings.get("max_pages", self.default_max_pages))
        service = KnowledgeService(context.knowledge_repository, settings=KnowledgeSettings())
        reader = KnowledgeActor(
            kind=ActorKind.AGENT,
            actor_id=context.agent_id,
            team_id=str(context.team_id),
            agent_type=str(context.named_agent_type or ""),
            work_person_id=str(context.canonical_user_id or ""),
        )
        hits = service.search(reader, query=request_text, limit=limit)
        if not hits:
            return []
        lines = [
            "## Knowledge pages that may help",
            "Pointers only; read a page with knowledge_read before relying on it.",
        ]
        for hit in hits:
            summary = hit.page.summary or hit.snippet
            lines.append(f"- {hit.page.reference()}: {hit.page.title}" + (f" ({summary})" if summary else ""))
        return [self._section(context, name="knowledge_recall", text="\n".join(lines), source="KnowledgeService.search")]
