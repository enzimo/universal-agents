"""Repo-internal memory hydration and maintenance plugins."""

from custom_agents.memory_plugins.base import (
    MemoryHydrationPlugin,
    MemoryHydrationSection,
    MemoryMaintenancePlugin,
    MemoryPluginContext,
    MemoryToolPlugin,
)

__all__ = [
    "MemoryHydrationPlugin",
    "MemoryHydrationSection",
    "MemoryMaintenancePlugin",
    "MemoryPluginContext",
    "MemoryToolPlugin",
    "MemoryPluginManager",
    "build_memory_plugins",
]


def __getattr__(name: str):
    if name == "MemoryPluginManager":
        from custom_agents.memory_plugins.manager import MemoryPluginManager

        return MemoryPluginManager
    if name == "build_memory_plugins":
        from custom_agents.memory_plugins.registry import build_memory_plugins

        return build_memory_plugins
    raise AttributeError(name)
