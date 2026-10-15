"""Pure value normalization for bounded runtime settings.

Persistence and authorization do not belong in this module.  It translates a
declared setting value into its canonical Python representation and enforces
the definition's scalar constraints, which makes the behavior independently
testable from the staged-operation lifecycle.
"""

from __future__ import annotations

from collections.abc import Callable
from math import isfinite
from typing import Any, Protocol


class RuntimeSettingValueSpec(Protocol):
    path: str
    value_type: str
    minimum: float | int | None
    maximum: float | int | None
    choices: tuple[str, ...]
    max_utf8_bytes: int | None


def _coerce_boolean(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    rendered = str(value).strip().lower()
    if rendered in {"1", "true", "yes", "on"}:
        return True
    if rendered in {"0", "false", "no", "off"}:
        return False
    raise ValueError(f"Expected boolean value, got {value!r}.")


def _coerce_text(value: Any, definition: RuntimeSettingValueSpec) -> str:
    normalized = str(value if value is not None else "")
    if not normalized.strip():
        raise ValueError(
            f"Runtime setting {definition.path!r} requires non-empty text."
        )
    return normalized


def _coerce_required_string(value: Any, definition: RuntimeSettingValueSpec) -> str:
    normalized = str(value if value is not None else "").strip()
    if not normalized:
        raise ValueError(
            f"Runtime setting {definition.path!r} requires a non-empty string."
        )
    return normalized


def _coerce_optional_string(
    value: Any,
    _definition: RuntimeSettingValueSpec,
) -> str | None:
    return str(value if value is not None else "").strip() or None


def _coerce_boolean_value(
    value: Any,
    _definition: RuntimeSettingValueSpec,
) -> bool:
    return _coerce_boolean(value)


def _coerce_optional_boolean(
    value: Any,
    _definition: RuntimeSettingValueSpec,
) -> bool | None:
    if value is None or str(value).strip() == "":
        return None
    return _coerce_boolean(value)


def _coerce_integer(value: Any, _definition: RuntimeSettingValueSpec) -> int:
    if isinstance(value, bool):
        raise ValueError(f"Expected integer value, got {value!r}.")
    return int(value)


def _coerce_optional_integer(
    value: Any,
    definition: RuntimeSettingValueSpec,
) -> int | None:
    if value is None or str(value).strip() == "":
        return None
    return _coerce_integer(value, definition)


def _coerce_float(value: Any, _definition: RuntimeSettingValueSpec) -> float:
    if isinstance(value, bool):
        raise ValueError(f"Expected numeric value, got {value!r}.")
    normalized = float(value)
    if not isfinite(normalized):
        raise ValueError("Expected finite numeric value.")
    return normalized


def _coerce_optional_float(
    value: Any,
    definition: RuntimeSettingValueSpec,
) -> float | None:
    if value is None or str(value).strip() == "":
        return None
    return _coerce_float(value, definition)


def _coerce_string_list(
    value: Any,
    _definition: RuntimeSettingValueSpec,
) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        return [part.strip() for part in value.split(",") if part.strip()]
    if isinstance(value, list):
        return [str(part).strip() for part in value if str(part).strip()]
    raise ValueError(
        f"Expected list or comma-separated string, got {type(value).__name__}."
    )


def _coerce_endpoint_url(value: Any, _definition: RuntimeSettingValueSpec) -> str:
    """A model endpoint address (``model_providers.endpoints.checked_endpoint_url``): http(s), a host, no credentials."""

    from model_providers.endpoints import checked_endpoint_url

    return checked_endpoint_url(value)


def _coerce_context_dump_mode(
    value: Any,
    _definition: RuntimeSettingValueSpec,
) -> str:
    return str(value or "").strip().lower()


ValueCoercer = Callable[[Any, RuntimeSettingValueSpec], Any]

_VALUE_COERCERS: dict[str, ValueCoercer] = {
    "text": _coerce_text,
    "str": _coerce_required_string,
    "optional_str": _coerce_optional_string,
    "bool": _coerce_boolean_value,
    "optional_bool": _coerce_optional_boolean,
    "int": _coerce_integer,
    "optional_int": _coerce_optional_integer,
    "float": _coerce_float,
    "optional_float": _coerce_optional_float,
    "list_str": _coerce_string_list,
    "context_dump_mode": _coerce_context_dump_mode,
    "endpoint_url": _coerce_endpoint_url,
}


def _validate_choice(value: Any, definition: RuntimeSettingValueSpec) -> None:
    if value is None or not definition.choices or value in definition.choices:
        return
    raise ValueError(
        f"Runtime setting {definition.path!r} must be one of: "
        f"{', '.join(definition.choices)}."
    )


def _validate_numeric_range(
    value: Any,
    definition: RuntimeSettingValueSpec,
) -> None:
    if value is None or isinstance(value, bool) or not isinstance(value, (int, float)):
        return
    if definition.minimum is not None and value < definition.minimum:
        raise ValueError(
            f"Runtime setting {definition.path!r} must be >= {definition.minimum}."
        )
    if definition.maximum is not None and value > definition.maximum:
        raise ValueError(
            f"Runtime setting {definition.path!r} must be <= {definition.maximum}."
        )


def _validate_encoded_size(value: Any, definition: RuntimeSettingValueSpec) -> None:
    if value is None or definition.max_utf8_bytes is None:
        return
    if len(str(value).encode("utf-8")) > definition.max_utf8_bytes:
        raise ValueError(
            f"Runtime setting {definition.path!r} cannot exceed "
            f"{definition.max_utf8_bytes} UTF-8 bytes."
        )


def _validate_setting_specific_limits(
    value: Any,
    definition: RuntimeSettingValueSpec,
) -> None:
    if definition.path == "delivery.urgent_keywords" and len(value) > 100:
        raise ValueError(
            "delivery.urgent_keywords cannot contain more than 100 values."
        )


def coerce_runtime_setting_value(
    value: Any,
    definition: RuntimeSettingValueSpec,
) -> Any:
    """Normalize and validate one value against its trusted setting spec."""
    coercer = _VALUE_COERCERS.get(definition.value_type)
    if coercer is None:
        raise ValueError(
            f"Unsupported runtime setting value type {definition.value_type!r}."
        )
    normalized = coercer(value, definition)
    _validate_choice(normalized, definition)
    _validate_numeric_range(normalized, definition)
    _validate_encoded_size(normalized, definition)
    _validate_setting_specific_limits(normalized, definition)
    return normalized
