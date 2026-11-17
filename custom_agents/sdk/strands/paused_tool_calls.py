"""Recognise a tool call that resumes after its pause.

Why: a call to a Strands agent pauses when a tool call waits for a person
(a local Strands interrupt: ``set_up_tools`` waiting for setup pages,
``request_access`` or ``apply_change`` waiting for a decision). Resuming it
runs the paused tool call again with its answer, and Strands fires
``BeforeToolCallEvent`` for it a second time. Nothing in that event tells a
hook that the call resumes. The steering plugin
(``custom_agents.task_steering_runtime.RuntimeSteeringPlugin``) cancels a new
call when a person's guidance is waiting; doing that to a resumed call threw
away the answer the task waited for, and the model read a cancellation
instead of the tool's result.

The rule: a call resumes when its tool use id started in an earlier call to
the agent that paused, and has not finished since. ``PausedToolCalls`` keeps
one set of tool use ids:

- ``start`` (called from the plugin's own ``BeforeToolCallEvent`` callback,
  so hook order does not matter) says whether the id is already in the set,
  then adds it;
- ``AfterToolCallEvent`` removes it: the call finished, with a result, an
  error or a cancellation;
- ``AfterInvocationEvent`` clears the set unless the call to the agent paused
  (``result.stop_reason == "interrupt"``). After a pause the set holds exactly
  the calls that paused, because every call that finished was removed.

Only public Strands hooks and results are used. The rule relies on this
Strands behaviour, which ``tests/test_resumed_tool_call_guidance.py`` pins on
the locked SDK version:

- a call that pauses, inside the tool or in a ``BeforeToolCallEvent`` hook,
  fires no ``AfterToolCallEvent``, and every ``BeforeToolCallEvent`` callback
  still runs after another one paused the call;
- a resume runs the paused calls again, with the same tool use, before the
  model is called; a call of the paused step that never ran (Strands'
  sequential executor stops at the first pause) runs then for the first time
  and counts as new;
- ``AfterInvocationEvent`` ends every call to the agent, also one that failed
  (then with no result), so a failed or cancelled call leaves nothing behind.

Strands' interrupt ids contain the tool use id, but Strands documents them as
opaque, and its own record of paused calls is private; neither is read here.

One set serves one Strands agent: the plugin is built per agent
(``custom_agents.strands_agent_builder``). That agent runs one call at a time,
and while paused it serves only its own resume
(``task_steering_runtime.StrandsInterruptedTurnSession``), so the set needs no
task key. An id leaves the set as soon as its call finishes, so a later new
call that reuses the id counts as new.
"""

from __future__ import annotations

from strands.hooks import AfterInvocationEvent, AfterToolCallEvent, BeforeToolCallEvent, HookRegistry

# AgentResult.stop_reason of a call to the agent that paused for an interrupt.
INTERRUPT_STOP_REASON = "interrupt"


class PausedToolCalls:
    """The tool calls a paused call to the Strands agent left unfinished (module docstring)."""

    def __init__(self) -> None:
        # Tool use ids of calls that started and have not finished.
        self._unfinished: set[str] = set()

    def register_hooks(self, registry: HookRegistry) -> None:
        registry.add_callback(AfterToolCallEvent, self._finished)
        registry.add_callback(AfterInvocationEvent, self._invocation_ended)

    def start(self, event: BeforeToolCallEvent) -> bool:
        """Record that this call starts; return whether it resumes a call a pause left unfinished."""

        tool_use_id = event.tool_use["toolUseId"]
        resumed = tool_use_id in self._unfinished
        self._unfinished.add(tool_use_id)
        return resumed

    def _finished(self, event: AfterToolCallEvent) -> None:
        self._unfinished.discard(event.tool_use["toolUseId"])

    def _invocation_ended(self, event: AfterInvocationEvent) -> None:
        if event.result is None or event.result.stop_reason != INTERRUPT_STOP_REASON:
            self._unfinished.clear()


__all__ = ["INTERRUPT_STOP_REASON", "PausedToolCalls"]
