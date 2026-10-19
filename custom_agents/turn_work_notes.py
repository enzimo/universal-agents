"""Keep what a model turn did when its task waits, so the next turn continues it.

Why: every model turn starts with an empty model conversation
(``UniversalAgent._reset_llm_conversation``); the task's saved history is all a
later turn of the same task knows. A turn that did work (tool calls such as
page edits) and then left its task waiting (for delegated work, a manager
escalation, an answer to a question, a credential form, or a pause at its
tool-call budget) left nothing about that work in the history. The resumed
turn saw only the original request as "Current Request" and started over. On
2026-10-19 a weekly knowledge-base audit on a worker escalated its findings to
the team lead, waited for the reply, and on every resume ran the whole audit
again: four passes for one scheduled occurrence, each able to message the
person again.

What trusted code does instead:

- When a task request's turn ends with the task still open, the turn executor
  saves one history entry on the task (event ``turn_work_before_wait``): the
  tools the turn called (name, a short input preview, whether the call
  worked) and what it sent out (delegations, escalations, questions, a
  held-back final answer).
- Every later turn of the task shows these entries under "Work already done in
  this task" (context section ``earlier_turn_work``), says the task is
  continuing, and labels the request as started earlier
  (``prompt_context_sanitization.render_current_request_context``).

The previews are short on purpose: they tell the next turn what already
happened, not the full results, and they are stored with the task. The model
already saw these inputs in the turn that made them.
"""

from __future__ import annotations

import json
from typing import Any, Iterable, Mapping, Optional

from custom_messages.contracts.credentials_v1 import SecureCredentialCollectionRequest
from custom_messages.contracts.task_v1 import Task, TaskHistoryEntry, TaskQuery
from custom_messages.enums import MessageMetaType

# History entry ``details["event"]`` for a saved turn note.
TURN_WORK_EVENT = "turn_work_before_wait"

# Why the turn ended with its task open (``details["ended_by"]``).
ENDED_BY_WAIT = "waiting"
ENDED_BY_OPEN = "task_still_open"
ENDED_BY_TOOL_LIMIT = "tool_call_limit"

# Bounds that keep notes and their prompt section small.
MAX_TOOL_CALLS_PER_NOTE = 40
MAX_INPUT_PREVIEW_CHARS = 160
MAX_SENT_PREVIEW_CHARS = 600
MAX_NOTES_SHOWN = 6
# The whole section stays under this; the newest turns are kept. The section is
# pinned (never dropped for context room), so it needs its own bound: six full
# notes could reach about 54,000 characters.
MAX_SECTION_CHARS = 12_000

EARLIER_TURN_WORK_HEADING = "## Work already done in this task"
CONTINUATION_NOTE = (
    "This task started earlier and is continuing; this turn does not start it "
    "over. Earlier turns of this task already did the work listed below, and "
    "their changes are already made. Use the replies and results in the task "
    "history to finish. Repeat a step only when its result is missing, it "
    "failed, or a reply asks for it."
)


def _preview(value: Any, max_chars: int) -> str:
    if isinstance(value, str):
        text = value
    else:
        try:
            text = json.dumps(value, ensure_ascii=False, default=str, sort_keys=True)
        except (TypeError, ValueError):
            text = str(value)
    text = " ".join(text.split())
    return text if len(text) <= max_chars else text[: max_chars - 1] + "…"


def _walk_blocks(value: Any) -> Iterable[Mapping[str, Any]]:
    """Every content block in Strands messages, in order (nested content too)."""

    if isinstance(value, Mapping):
        yield value
        for child in value.values():
            yield from _walk_blocks(child)
    elif isinstance(value, list):
        for child in value:
            yield from _walk_blocks(child)


def tool_calls_from_messages(
    messages: Iterable[Any], *, exclude_tool_names: Iterable[str] = ()
) -> list[dict[str, Any]]:
    """The tools a turn called, in order: name, input preview and result status.

    ``exclude_tool_names`` drops the structured-output tool that carries the
    turn's decision; it is the turn's answer, not work. A call with no result
    (the turn stopped first) has status ``no_result``.
    """

    excluded = set(exclude_tool_names)
    calls: list[dict[str, Any]] = []
    by_id: dict[str, dict[str, Any]] = {}
    for block in _walk_blocks(list(messages)):
        tool_use = block.get("toolUse")
        if isinstance(tool_use, Mapping):
            call = _tool_call(tool_use, excluded)
            if call is not None:
                calls.append(call)
                if tool_use.get("toolUseId"):
                    by_id[str(tool_use["toolUseId"])] = call
        else:
            _apply_result(block.get("toolResult"), by_id)
    return calls


def _tool_call(tool_use: Mapping[str, Any], excluded: set[str]) -> Optional[dict[str, Any]]:
    name = str(tool_use.get("name") or "")
    if not name or name in excluded:
        return None
    return {
        "tool": name,
        "input": _preview(tool_use.get("input"), MAX_INPUT_PREVIEW_CHARS),
        "status": "no_result",
    }


def _apply_result(tool_result: Any, by_id: dict[str, dict[str, Any]]) -> None:
    if not isinstance(tool_result, Mapping):
        return
    call = by_id.get(str(tool_result.get("toolUseId") or ""))
    if call is not None:
        call["status"] = str(tool_result.get("status") or "unknown")


def _action_content(action: Any) -> Any:
    """The message an action sends, for the action types that may wait."""

    action_type = getattr(action, "action_type", None)
    if action_type == "task_query":
        return getattr(action, "task_query", None)
    if action_type == "send_message":
        message = getattr(action, "message", None)
        to_comms = getattr(message, "to_comms_message", None)
        return to_comms().msg_content if callable(to_comms) else None
    if action_type == "secure_credential_collection_request":
        return getattr(action, "secure_credential_collection_request", None)
    if action_type == "signup_automation_request":
        return getattr(action, "signup_automation_request", None)
    return None


def action_starts_wait(action: Any) -> bool:
    """Whether the action sends something the task then waits for.

    A delegation (and a manager escalation, which is a delegation to the team
    lead), a question to the requester, a credential form or signup request.
    """

    if getattr(action, "action_type", None) == "delegate_task":
        return True
    content = _action_content(action)
    if isinstance(content, TaskQuery):
        return content.query_type == MessageMetaType.REQUEST
    if isinstance(content, SecureCredentialCollectionRequest):
        return content.request_type == MessageMetaType.REQUEST
    request_type = getattr(content, "request_type", None)
    return request_type == MessageMetaType.REQUEST


def decision_starts_wait(decision: Any) -> bool:
    """Whether the turn's decision makes its task wait for something it sent."""

    return any(action_starts_wait(action) for action in getattr(decision, "actions", None) or [])


def decision_finishes_task(decision: Any) -> bool:
    """Whether the decision ends with the task's own final answer."""

    actions = list(getattr(decision, "actions", None) or [])
    return bool(actions) and getattr(actions[-1], "action_type", None) == "task_response"


def leaves_task_open(decision: Any) -> bool:
    """A turn that waits, or ends without a final answer, leaves its task open.

    A final answer sent with a wait is held back until the wait ends
    (``turn_action_outbox_runtime._defer_terminal_task_response_for_pending_parent_work``),
    so such a turn leaves the task open as well.
    """

    return decision_starts_wait(decision) or not decision_finishes_task(decision)


def sent_items(decision: Any) -> list[str]:
    """Short descriptions of what the turn sent out, for the next turn."""

    described = (_describe_sent(action) for action in getattr(decision, "actions", None) or [])
    return [item for item in described if item]


def _describe_sent(action: Any) -> Optional[str]:
    action_type = getattr(action, "action_type", None)
    if action_type == "delegate_task":
        return _describe_delegation(action)
    if action_type == "task_response":
        answer = getattr(getattr(action, "task", None), "answer", None)
        answer_text = getattr(answer, "answer", answer) if answer is not None else ""
        return "final answer, held back until the wait ended: " + _preview(answer_text or "", MAX_SENT_PREVIEW_CHARS)
    content = _action_content(action)
    if isinstance(content, TaskQuery) and content.query_type == MessageMetaType.REQUEST:
        return f"question to the requester: {_preview(content.message, MAX_SENT_PREVIEW_CHARS)}"
    if content is not None and action_starts_wait(action):
        return f"{action_type} request"
    return None


def _describe_delegation(action: Any) -> str:
    escalation = getattr(action, "manager_escalation", None)
    target = getattr(action, "target_agent_name", None) or getattr(action, "target_named_agent_type", None)
    if escalation is not None:
        kind = f"manager escalation ({escalation.escalation_type}) to {target}"
    else:
        kind = f"delegated task to {target}"
    statement = getattr(getattr(action, "task", None), "problem_statement", "") or ""
    return f"{kind}: {_preview(statement, MAX_SENT_PREVIEW_CHARS)}"


def note_details(
    *,
    tool_calls: list[dict[str, Any]],
    sent: list[str],
    started_wait: bool,
    ended_by: str,
) -> dict[str, Any]:
    """The ``details`` of a saved note; tool calls past the bound are counted."""

    shown = tool_calls[:MAX_TOOL_CALLS_PER_NOTE]
    return {
        "event": TURN_WORK_EVENT,
        "started_wait": started_wait,
        "ended_by": ended_by,
        "tool_calls": shown,
        "tool_calls_not_shown": max(0, len(tool_calls) - len(shown)),
        "sent": sent,
    }


def note_summary(details: Mapping[str, Any]) -> str:
    """The one-line history summary of a saved note."""

    count = len(details.get("tool_calls") or []) + int(details.get("tool_calls_not_shown") or 0)
    sent = details.get("sent") or []
    ending = {
        ENDED_BY_WAIT: "then waited",
        ENDED_BY_TOOL_LIMIT: "then paused at its tool-call budget",
    }.get(str(details.get("ended_by")), "and left the task open")
    return f"Turn called {count} tool(s), sent {len(sent)} message(s), {ending}."


def is_turn_work_note(entry: TaskHistoryEntry) -> bool:
    return isinstance(entry.details, Mapping) and entry.details.get("event") == TURN_WORK_EVENT


def turn_work_notes(task: Optional[Task]) -> list[TaskHistoryEntry]:
    """The task's saved turn notes, oldest first."""

    if task is None:
        return []
    return [entry for entry in task.execution_history or [] if is_turn_work_note(entry)]


def render_earlier_turn_work(notes: list[TaskHistoryEntry]) -> str:
    """The prompt section for a task's saved turn notes ("" when there are none)."""

    if not notes:
        return ""
    blocks: list[str] = []
    used = len(EARLIER_TURN_WORK_HEADING) + len(CONTINUATION_NOTE)
    # Newest first, so a bound drops the oldest turns.
    for number in range(len(notes), max(0, len(notes) - MAX_NOTES_SHOWN), -1):
        block = _render_note(number, notes[number - 1])
        if blocks and used + len(block) > MAX_SECTION_CHARS:
            break
        blocks.append(block[:MAX_SECTION_CHARS])
        used += len(block)
    lines = [EARLIER_TURN_WORK_HEADING, CONTINUATION_NOTE]
    omitted = len(notes) - len(blocks)
    if omitted > 0:
        lines.append(f"({omitted} older turn(s) not shown.)")
    lines.extend(reversed(blocks))
    return "\n".join(lines)


def _render_note(number: int, entry: TaskHistoryEntry) -> str:
    details = entry.details or {}
    lines = ["", f"### Turn {number} (ended {entry.timestamp}): {note_summary(details)}"]
    for call in details.get("tool_calls") or []:
        lines.append(f"- called `{call.get('tool')}` ({call.get('status')}): {call.get('input')}")
    hidden = int(details.get("tool_calls_not_shown") or 0)
    if hidden:
        lines.append(f"- and {hidden} more tool call(s)")
    for item in details.get("sent") or []:
        lines.append(f"- sent {item}")
    return "\n".join(lines)


__all__ = [
    "CONTINUATION_NOTE",
    "EARLIER_TURN_WORK_HEADING",
    "ENDED_BY_OPEN",
    "ENDED_BY_TOOL_LIMIT",
    "ENDED_BY_WAIT",
    "TURN_WORK_EVENT",
    "action_starts_wait",
    "decision_finishes_task",
    "decision_starts_wait",
    "is_turn_work_note",
    "leaves_task_open",
    "note_details",
    "note_summary",
    "render_earlier_turn_work",
    "sent_items",
    "tool_calls_from_messages",
    "turn_work_notes",
]
