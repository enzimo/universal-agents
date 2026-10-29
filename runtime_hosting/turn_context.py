"""Context variables that belong to one turn, and a context without them.

asyncio copies the current context into every task and timer callback it
starts. Long-lived background work started during a turn (the queue drain,
the pass that starts parallel lanes and its lane workers, a delayed wake-up)
would therefore keep that turn's values after the turn ended: its task
binding, its execution lane, the person's summary model and the model keys
of its last call. Found in the PR #57 review: a drain started inside one
request's turn replayed another request's saved delegation as a child of the
first request.

Each module registers such a variable with ``turn_scoped`` where it defines
it. ``detached_context`` gives background work a copy of the current context
with all of them cleared; everything else, such as the agent reference tools
read, is kept.
"""

from __future__ import annotations

import contextvars
from typing import Any, TypeVar

_T = TypeVar("_T")

_TURN_SCOPED: list[tuple[contextvars.ContextVar[Any], Any]] = []


def turn_scoped(variable: contextvars.ContextVar[_T], empty: _T) -> contextvars.ContextVar[_T]:
    """Register a variable that belongs to one turn; ``empty`` is its value outside any turn."""

    _TURN_SCOPED.append((variable, empty))
    return variable


def detached_context() -> contextvars.Context:
    """A copy of the current context with every turn-scoped variable cleared (module docstring)."""

    context = contextvars.copy_context()
    context.run(_clear_turn_variables)
    return context


def _clear_turn_variables() -> None:
    for variable, empty in _TURN_SCOPED:
        variable.set(empty)


__all__ = ["detached_context", "turn_scoped"]
