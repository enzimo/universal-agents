from __future__ import annotations

from copy import deepcopy
from typing import Any, Mapping

from default_values import AGENT_DEFAULTS


def deep_merge_context_dicts(base: dict[str, Any], override: Mapping[str, Any] | None) -> dict[str, Any]:
    merged = deepcopy(base)
    for key, value in dict(override or {}).items():
        if isinstance(value, Mapping) and isinstance(merged.get(key), dict):
            merged[key] = deep_merge_context_dicts(merged[key], value)
        else:
            merged[key] = deepcopy(value)
    return merged


def merge_context_engine_config(config: Mapping[str, Any] | None = None) -> dict[str, Any]:
    return deep_merge_context_dicts(
        dict(AGENT_DEFAULTS.context_engine.value),
        dict(config or {}),
    )
