"""What a tool returned, for tests: its JSON text, or the text of a failed result.

Tools return JSON text when a call succeeds and a failed Strands result when it
does not (``custom_tools.tool_results``); tests read both the same way.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from typing import Any

import yaml

from custom_tools.tool_results import result_text


def tool_json(result: str | Mapping[str, Any]) -> Any:
    return json.loads(result_text(result))


def tool_yaml(result: str | Mapping[str, Any]) -> Any:
    return yaml.safe_load(result_text(result))
