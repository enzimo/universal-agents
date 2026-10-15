"""Helpers for loading optional external AgentResult transformers."""

from importlib import import_module
from typing import Any, Callable, Optional


AgentResultTransformer = Callable[[Any, Any, Any], Any]


def load_agent_result_transformer(spec: Optional[str]) -> Optional[AgentResultTransformer]:
    """Load an optional result transformer from ``module:function`` notation."""
    if not spec:
        return None
    if ":" not in spec:
        raise ValueError(
            "agent_result_transformer_spec must use 'module:function' notation, "
            f"got {spec!r}."
        )

    module_name, function_name = spec.split(":", 1)
    module = import_module(module_name)
    transformer = getattr(module, function_name)
    if not callable(transformer):
        raise TypeError(f"Configured agent result transformer {spec!r} is not callable.")
    return transformer
