from __future__ import annotations

from custom_agents.context_engine.default import ContextEngineAdapters, DefaultContextEngine


def build_context_engine(
    engine_type: str,
    **kwargs,
):
    normalized = (engine_type or "default").strip().lower()
    if normalized == "default":
        return DefaultContextEngine(**kwargs)
    raise ValueError(f"Unsupported context engine type: {engine_type}")


__all__ = ["ContextEngineAdapters", "DefaultContextEngine", "build_context_engine"]
