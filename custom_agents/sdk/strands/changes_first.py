"""Run a model step's record changes first; skip the rest of the step when one fails.

Why: a model often sends several tool calls in one step, and Strands runs them
side by side (``ConcurrentToolExecutor``). The model chose those calls
assuming each change goes through. When a change to a stored record fails (a
memory fact, a knowledge page, a schedule, an Objective, a heartbeat item,
a file or an exact staged plan), running the step's other calls anyway acts
on a premise that is no longer true: a reply sent, a schedule switched on,
or a file written next to a change that was refused. As a person's separate requests start to
overlap, a refused change becomes an ordinary outcome, so a step has to stop
at it.

Which calls change records comes from the tool catalog: operations with a
``record_kind`` (``custom_messages.enums.ChangeRecordKind``,
``ua_definitions/access/tools/functions.yaml``). This executor:

- runs the step's record changes first, one at a time, in the model's order;
- stops at the first one that does not succeed: an error result (a refusal,
  a failed check, an access denial) or a pause for a decision (a Strands
  interrupt, such as an approval). Every later call of the step, changes and
  other calls alike, then gets a "not run" error result instead of running;
- otherwise runs the other calls side by side, as Strands does by default.

A "not run" result is final for its call. When the step paused, Strands keeps
the results made so far with the pause and, on resume, runs only the calls
that have none: the paused call starts again and the "not run" calls never
run. The model sees why and sends them again if they still make sense.
Skipped calls never start, so no ``BeforeToolCallEvent`` or
``AfterToolCallEvent`` hook sees them.

Results are kept in the model's call order, as the default executor keeps them.

This subclasses Strands' ``ConcurrentToolExecutor`` and uses the SDK's private
``ToolExecutor._execute`` and ``ToolExecutor._stream_with_trace``, as Strands'
own executors do. ``tests/test_changes_first_executor.py`` runs real Strands
agents on the locked SDK version and fails if those names or the resume
behavior change.
"""

from __future__ import annotations

import logging
from collections.abc import AsyncGenerator, Callable, Mapping
from typing import TYPE_CHECKING, Any

from strands.tools.executors import ConcurrentToolExecutor
from strands.tools.executors._executor import ToolExecutor
from strands.types._events import ToolInterruptEvent, ToolResultEvent, TypedEvent
from strands.types.tools import ToolResult, ToolUse
from typing_extensions import override

from custom_messages.enums import ChangeRecordKind

if TYPE_CHECKING:  # pragma: no cover
    from strands import Agent
    from strands.telemetry.metrics import Trace
    from strands.tools.structured_output._structured_output_context import StructuredOutputContext

logger = logging.getLogger(__name__)

NOT_RUN_TEXT = (
    "Not run: an earlier change in this step did not go through, so this call was skipped. "
    "Send it again if it still makes sense."
)

RecordKindOf = Callable[[str, Mapping[str, Any]], ChangeRecordKind | None]


class ChangesFirstToolExecutor(ConcurrentToolExecutor):
    """Record changes first, one at a time; the rest of the step is skipped once one fails."""

    def __init__(self, record_kind_of: RecordKindOf) -> None:
        super().__init__()
        self._record_kind_of = record_kind_of

    def _split(self, tool_uses: list[ToolUse]) -> tuple[list[ToolUse], list[ToolUse]]:
        """The step's record changes and its other calls, each in the model's order."""

        changes: list[ToolUse] = []
        others: list[ToolUse] = []
        for tool_use in tool_uses:
            arguments = tool_use.get("input")
            kind = self._record_kind_of(tool_use["name"], arguments if isinstance(arguments, Mapping) else {})
            (others if kind is None else changes).append(tool_use)
        return changes, others

    @override
    async def _execute(
        self,
        agent: "Agent",
        tool_uses: list[ToolUse],
        tool_results: list[ToolResult],
        cycle_trace: "Trace",
        cycle_span: Any,
        invocation_state: dict[str, Any],
        structured_output_context: "StructuredOutputContext | None" = None,
    ) -> AsyncGenerator[TypedEvent, None]:
        first_result = len(tool_results)
        changes, others = self._split(tool_uses)
        trace = (cycle_trace, cycle_span, invocation_state, structured_output_context)
        for index, tool_use in enumerate(changes):
            paused = False
            async for event in ToolExecutor._stream_with_trace(agent, tool_use, tool_results, *trace):
                paused = paused or isinstance(event, ToolInterruptEvent)
                yield event
            if paused or not _succeeded(tool_results, tool_use):
                for skipped in _skip(agent, tool_use, paused, changes[index + 1 :] + others, tool_results):
                    yield skipped
                others = []  # skipped above
                break
        if others:
            async for event in super()._execute(agent, others, tool_results, *trace):
                yield event
        _keep_call_order(tool_results, first_result, tool_uses)


def _skip(
    agent: "Agent", failed: ToolUse, paused: bool, skipped: list[ToolUse], tool_results: list[ToolResult]
) -> list[ToolResultEvent]:
    """Give each skipped call its "not run" result, and log why the step stopped."""

    events: list[ToolResultEvent] = []
    for tool_use in skipped:
        result: ToolResult = {"toolUseId": tool_use["toolUseId"], "status": "error", "content": [{"text": NOT_RUN_TEXT}]}
        tool_results.append(result)
        events.append(ToolResultEvent(result))
    if skipped:
        logger.info(
            "A change in a model step did not go through; skipped the step's later calls. "
            "event=changes_first_step_stopped agent=%s tool=%s tool_use_id=%s outcome=%s skipped=%s",
            getattr(agent, "name", ""),
            failed["name"],
            failed["toolUseId"],
            "paused" if paused else "failed",
            ",".join(tool_use["name"] for tool_use in skipped),
        )
    return events


def _succeeded(tool_results: list[ToolResult], tool_use: ToolUse) -> bool:
    tool_use_id = tool_use["toolUseId"]
    for result in tool_results[::-1]:
        if result["toolUseId"] == tool_use_id:
            return result["status"] == "success"
    return False


def _keep_call_order(tool_results: list[ToolResult], first_result: int, tool_uses: list[ToolUse]) -> None:
    """Put this step's results back in the model's call order (results before ``first_result`` stay)."""

    position = {tool_use["toolUseId"]: index for index, tool_use in enumerate(tool_uses)}
    tool_results[first_result:] = sorted(
        tool_results[first_result:], key=lambda result: position.get(result["toolUseId"], len(position))
    )


__all__ = ["NOT_RUN_TEXT", "ChangesFirstToolExecutor", "RecordKindOf"]
