from __future__ import annotations

from custom_agents.memory_plugins.config import normalize_memory_config
from custom_agents.memory_plugins.hydration import (
    CorememHydrationPlugin,
    DomainTopicRecallPlugin,
    KnowledgeRecallPlugin,
    UsermemHydrationPlugin,
)


BUILTIN_HYDRATION_PLUGINS = {
    "coremem": CorememHydrationPlugin,
    "usermem": UsermemHydrationPlugin,
    "domain_topic_recall": DomainTopicRecallPlugin,
    "knowledge_recall": KnowledgeRecallPlugin,
}


def build_memory_plugins(config: dict | None = None) -> list[object]:
    normalized = normalize_memory_config(config)
    order = list((normalized.get("plugins") or {}).get("order") or [])
    return [BUILTIN_HYDRATION_PLUGINS[plugin_id]() for plugin_id in order]
