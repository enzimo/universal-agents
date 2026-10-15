from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import pytest

from custom_agents.runtime_setting_values import coerce_runtime_setting_value


@dataclass(frozen=True)
class _Spec:
    path: str = "test.setting"
    value_type: str = "str"
    minimum: float | int | None = None
    maximum: float | int | None = None
    choices: tuple[str, ...] = ()
    max_utf8_bytes: int | None = None


@pytest.mark.parametrize(
    ("value", "spec", "message"),
    (
        ("maybe", _Spec(value_type="bool"), "Expected boolean"),
        (True, _Spec(value_type="int"), "Expected integer"),
        ("nan", _Spec(value_type="float"), "finite"),
        ("inf", _Spec(value_type="optional_float"), "finite"),
        (True, _Spec(value_type="float"), "Expected numeric"),
        (0, _Spec(value_type="int", minimum=1), "must be >= 1"),
        ("other", _Spec(choices=("one", "two")), "must be one of"),
        ("éé", _Spec(max_utf8_bytes=3), "UTF-8 bytes"),
    ),
)
def test_coerce_runtime_setting_value_rejects_invalid_values(
    value: Any,
    spec: _Spec,
    message: str,
) -> None:
    with pytest.raises(ValueError, match=message):
        coerce_runtime_setting_value(value, spec)


def test_urgent_keyword_limit_is_enforced() -> None:
    spec = _Spec(path="delivery.urgent_keywords", value_type="list_str")

    with pytest.raises(ValueError, match="more than 100"):
        coerce_runtime_setting_value([f"word-{index}" for index in range(101)], spec)
