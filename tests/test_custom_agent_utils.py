from __future__ import annotations

import pytest

from custom_agents.custom_agent_utils import parse_json_from_raw


def test_parse_json_from_raw_returns_object_when_requested() -> None:
    assert parse_json_from_raw('{"answer": 42}', return_dict=True) == {"answer": 42}


def test_parse_json_from_raw_rejects_non_object_dict_request() -> None:
    with pytest.raises(ValueError, match="must contain an object"):
        parse_json_from_raw("[1, 2, 3]", return_dict=True)
