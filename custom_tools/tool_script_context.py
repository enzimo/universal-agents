"""Whether the tool call running now comes from a tool script (``run_tool_script``).

Two hooks treat such calls differently (``custom_agents.tool_script_runtime``):

- ``custom_agents.tool_loading.OnRequestToolHook`` gives a script's call its
  tool without loading its group, so a script never adds definitions to the
  model's tool list;
- ``custom_agents.tool_result_offloader`` leaves a script's results whole,
  because they go to the script, not to the model; the script's own result is
  offloaded like any tool result.

Every other hook, the access hook first, treats the call exactly like a
direct one. A context variable carries the mark, so it reaches the thread
Strands' coroutine bridge runs the call in and no other turn.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar

_IN_TOOL_SCRIPT: ContextVar[bool] = ContextVar("ua_tool_script_call", default=False)


def in_tool_script() -> bool:
    """Whether the current tool call was made by a tool script."""

    return _IN_TOOL_SCRIPT.get()


@contextmanager
def tool_script_call() -> Iterator[None]:
    """Mark the tool calls made inside this block as a tool script's."""

    token = _IN_TOOL_SCRIPT.set(True)
    try:
        yield
    finally:
        _IN_TOOL_SCRIPT.reset(token)


__all__ = ["in_tool_script", "tool_script_call"]
