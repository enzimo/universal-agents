"""Results of tool calls that failed, in the form Strands reads as failed.

Strands takes a tool's outcome only from its result status: a returned dict with
``status`` and ``content`` passes through, and anything else (a string, another
dict) is wrapped as ``success`` (``strands.tools.decorator``). A tool that
returns ``{"error": ...}`` JSON therefore reports a failed call as a success.

That matters for calls that change records: the tool executor stops a model
step at the first change whose status is not ``success`` and skips the step's
later calls, which were chosen assuming the change would go through
(``custom_agents.sdk.strands.changes_first``). So tools that change records
return ``failed(...)`` whenever the change was not made. The model reads the
same JSON text as before.

A change that was made stays a success, also with warnings; so does a change
staged for a person's approval or proposed for review, because the tool did
what it does.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from typing import Any

FAILED = "error"  # Strands' ToolResult status of a failed call


def failed(payload: Mapping[str, Any]) -> dict[str, Any]:
    """A failed call's result: ``payload`` as JSON text, with status ``error``."""

    return {"status": FAILED, "content": [{"text": json.dumps(payload, default=str)}]}


def result_text(result: str | Mapping[str, Any]) -> str:
    """The text the model reads from a tool's return value (a string, or a result's first text)."""

    if isinstance(result, str):
        return result
    return str(result["content"][0]["text"])


__all__ = ["FAILED", "failed", "result_text"]
