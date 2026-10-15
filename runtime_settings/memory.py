"""Memory configuration validation shared by config loaders and runtime owners."""

from typing import Annotated, Any

from pydantic import AfterValidator


BUILTIN_HYDRATION_PLUGIN_IDS = frozenset({"coremem", "usermem", "domain_topic_recall", "knowledge_recall"})


def validate_memory_plugin_config(config: dict[str, Any]) -> dict[str, Any]:
    """Reject unsupported plugin references even when hydration is disabled."""
    plugins = dict(config.get("plugins") or {})
    configured_ids = set(plugins.get("order") or []) | (set(plugins) - {"order"})
    unknown = configured_ids - BUILTIN_HYDRATION_PLUGIN_IDS
    if unknown:
        raise ValueError(
            f"Unknown memory plugin id(s): {', '.join(sorted(unknown))}. "
            "Remove unsupported entries from agent.memory.plugins and its order; "
            f"supported ids: {', '.join(sorted(BUILTIN_HYDRATION_PLUGIN_IDS))}."
        )
    return config


MemoryConfig = Annotated[dict[str, Any], AfterValidator(validate_memory_plugin_config)]
