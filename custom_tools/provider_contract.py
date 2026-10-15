"""Declarative contract for composing trusted in-process Strands tools."""

from __future__ import annotations

from dataclasses import dataclass
import inspect
from typing import Any, Iterable


def tool_name(tool: object) -> str:
    """Return the stable public name used by Strands and capability policy."""
    if inspect.ismodule(tool):
        raise ValueError(
            f"Tool module {tool.__name__!r} must declare its concrete Strands operations. "
            "A module name is not an invocation identity."
        )
    return str(
        getattr(tool, "tool_name", None)
        or getattr(tool, "__name__", None)
        or getattr(tool, "name", str(tool))
    ).strip()


def dedupe_tools(tools: Iterable[Any]) -> tuple[Any, ...]:
    """Preserve declaration order while removing repeated tool names."""
    result: list[Any] = []
    seen: set[str] = set()
    for tool in tools:
        name = tool_name(tool)
        if name in seen:
            continue
        result.append(tool)
        seen.add(name)
    return tuple(result)


@dataclass(frozen=True)
class ToolProvider:
    """One cohesive, trusted family of in-process Strands tools."""

    name: str
    tools: tuple[Any, ...]

    def __post_init__(self) -> None:
        normalized_name = self.name.strip()
        if not normalized_name:
            raise ValueError("Tool provider name must not be empty.")
        names = tuple(tool_name(tool) for tool in self.tools)
        if not names:
            raise ValueError(f"Tool provider {normalized_name!r} must declare tools.")
        if len(names) != len(set(names)):
            raise ValueError(
                f"Tool provider {normalized_name!r} declares duplicate tool names."
            )
        object.__setattr__(self, "name", normalized_name)
