"""Which inputs each action of an action tool takes (one tool, several operations).

Related functions are merged into one tool that takes an ``action`` argument
(owner's decision 2026-10-10): every model request carries fewer tool
definitions, and the catalog still gives each action its own level
(``argument: action`` in ``ua_definitions/access/tools/functions.yaml``), so
the access hook decides each call by its action. A JSON schema cannot say
which inputs each action needs in a way every model provider accepts, so each
tool declares them here and refuses a call that misses an input its action
needs or passes one it does not use, with a message the model can act on,
rather than guessing what was meant.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping

from custom_tools.tool_results import failed


@dataclass(frozen=True)
class ActionInputs:
    """The inputs one action needs and the ones it may take besides."""

    required: frozenset[str] = frozenset()
    optional: frozenset[str] = frozenset()


def actions(**specs: tuple[tuple[str, ...], tuple[str, ...]]) -> dict[str, ActionInputs]:
    """``actions(create=(("fact_text",), ("tags",)), ...)``: per action, (required, optional) input names."""

    return {action: ActionInputs(frozenset(required), frozenset(optional)) for action, (required, optional) in specs.items()}


def _given(value: Any) -> bool:
    # Models often send an empty value, or false for a flag, for an input they
    # mean to leave out. Zero is a value (``is``, since False == 0).
    if value is None or value is False:
        return False
    return value != "" and value != [] and value != {}


def input_problem(tool: str, action: str, specs: Mapping[str, ActionInputs], inputs: Mapping[str, Any]) -> str | None:
    """Why the call cannot run as given, or None: ``inputs`` are the call's inputs other than ``action``."""

    spec = specs.get(action)
    if spec is None:
        return f"{tool} has no action {action!r}; use one of: {', '.join(specs)}."
    given = {name for name, value in inputs.items() if _given(value)}
    problems = []
    missing = sorted(spec.required - given)
    if missing:
        problems.append(f"{tool}(action={action!r}) needs {', '.join(missing)}")
    unused = sorted(given - spec.required - spec.optional)
    if unused:
        problems.append(f"{tool}(action={action!r}) does not take {', '.join(unused)}")
    return "; ".join(problems) + "." if problems else None


def input_refusal(
    tool: str, action: str, specs: Mapping[str, ActionInputs], inputs: Mapping[str, Any]
) -> dict[str, Any] | None:
    """``input_problem`` as the failed result a tool returns (``error_code`` invalid_input), or None."""

    problem = input_problem(tool, action, specs, inputs)
    return failed({"error": problem, "error_code": "invalid_input"}) if problem else None


__all__ = ["ActionInputs", "actions", "input_problem", "input_refusal"]
