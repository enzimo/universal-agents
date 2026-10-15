"""Values configuration and contracts."""

from __future__ import annotations
import copy
from typing import Any, Optional, Dict
from default_values import AUTHORIZATION_DEFAULTS


def _normalize_positive_float(value: Any, *, field_name: str) -> float:
    try:
        parsed = float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{field_name} must be a number.") from exc
    if parsed <= 0:
        raise ValueError(f"{field_name} must be greater than 0.")
    return parsed


def _normalize_positive_int(
    value: Any,
    *,
    field_name: str,
    max_value: Optional[int] = None,
) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{field_name} must be an integer.") from exc
    if parsed < 1:
        raise ValueError(f"{field_name} must be greater than 0.")
    if max_value is not None and parsed > max_value:
        raise ValueError(f"{field_name} must be at most {max_value}.")
    return parsed


DEFAULT_AUTHORIZATION_CONFIG = {
    "enabled": AUTHORIZATION_DEFAULTS.enabled.value,
    "cedar_binary_path": AUTHORIZATION_DEFAULTS.cedar_binary_path.value,
    "starter_profile": AUTHORIZATION_DEFAULTS.starter_profile.value,
    "presentation": copy.deepcopy(AUTHORIZATION_DEFAULTS.presentation.value),
}


def parse_bool_env_value(name: str, value: str) -> bool:
    """Parse a boolean environment value using the gateway's accepted spelling set."""
    lowered = value.strip().lower()
    if lowered in {"1", "true", "yes", "on"}:
        return True
    if lowered in {"0", "false", "no", "off"}:
        return False
    raise ValueError(f"Invalid boolean environment value for {name}: {value!r}")


def deep_merge_dicts(base: Dict[str, Any], override: Dict[str, Any]) -> Dict[str, Any]:
    """Recursively merge dictionaries without mutating the inputs."""
    merged = dict(base)
    for key, value in (override or {}).items():
        if value is None and isinstance(merged.get(key), dict):
            continue
        if isinstance(value, dict) and isinstance(merged.get(key), dict):
            merged[key] = deep_merge_dicts(merged[key], value)
        else:
            merged[key] = value
    return merged


def _mapping_or_empty(value: Any) -> Dict[str, Any]:
    return dict(value) if isinstance(value, dict) else {}
