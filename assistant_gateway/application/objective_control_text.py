"""Plain words for the outcome of a person's Objective control.

The manager answers an Objective control with a reply code in
``ManagerRuntimeWorkCommand.message`` (``custom_messages.contracts.control_v1``).
People saw those codes as they were ("Control incomplete:
objective_action_unavailable:satisfied"), and a refused control was also shown
as if it had worked. Chat replies (``operational_control``) and the Web
Runtime controls page (``website_services``) word them here instead.
"""

from __future__ import annotations

from typing import Optional

from custom_messages.contracts.control_v1 import (
    ABANDON_OBJECTIVE,
    DELETE_OBJECTIVE,
    OBJECTIVE_ACTION_UNAVAILABLE,
    OBJECTIVE_ALREADY_DELETED,
    OBJECTIVE_CHANGED,
    OBJECTIVE_CONTROL_INCOMPLETE,
    OBJECTIVE_CONTROL_WORDS,
    OBJECTIVE_DELETED,
    OBJECTIVE_DELETION_INCOMPLETE,
    OBJECTIVE_NOT_FOUND,
    OBJECTIVE_OWNER_MISMATCH,
    OBJECTIVE_PART_HEARTBEAT,
    OBJECTIVE_PART_POLICY,
    OBJECTIVE_PART_RECORD,
    OBJECTIVE_PART_SCHEDULE,
    OBJECTIVE_PART_TASK,
    OBJECTIVE_UPDATE_FAILED,
    PAUSE_OBJECTIVE,
    RESUME_OBJECTIVE,
    STOP_OBJECTIVE,
    ManagerRuntimeWorkCommand,
)
from custom_messages.enums import ObjectiveStatus

_DONE_WORDS = {
    PAUSE_OBJECTIVE: "paused",
    RESUME_OBJECTIVE: "resumed",
    STOP_OBJECTIVE: "stopped",
    ABANDON_OBJECTIVE: "abandoned",
    DELETE_OBJECTIVE: "deleted",
}
_PART_WORDS = {
    OBJECTIVE_PART_SCHEDULE: "schedule",
    OBJECTIVE_PART_HEARTBEAT: "heartbeat item",
    OBJECTIVE_PART_POLICY: "work-session setting",
    OBJECTIVE_PART_TASK: "task (its stop was not confirmed)",
}
_STATUS_PREFIX = "objective_"


def _status_words(status: str) -> str:
    return "being deleted" if status == ObjectiveStatus.DELETING.value else status


def _parts(details: str) -> str:
    words = []
    for item in details.split(","):
        part, _, item_id = item.partition(":")
        if part == OBJECTIVE_PART_RECORD:
            words.append("recording which links it changed")
        else:
            words.append(f"{_PART_WORDS.get(part, part)} {item_id}".strip())
    return ", ".join(words)


def objective_control_text(response: ManagerRuntimeWorkCommand) -> Optional[str]:
    """One plain sentence for an Objective control's reply, or None when it is not one."""

    objective_id = str(response.target_objective_id or "")
    if response.action not in OBJECTIVE_CONTROL_WORDS or not objective_id:
        return None
    code, _, details = str(response.message or "").partition(":")
    retry = f"/{OBJECTIVE_CONTROL_WORDS[response.action]} objective {objective_id}"
    if code == OBJECTIVE_ACTION_UNAVAILABLE:
        return (
            f"Objective {objective_id} is {_status_words(details)}, so it can't be "
            f"{_DONE_WORDS[response.action]}. Nothing was changed."
        )
    if code == OBJECTIVE_CONTROL_INCOMPLETE:
        return (
            f"Not every part finished: {_parts(details)}. "
            f"Send {retry} again to retry them."
        )
    if code == OBJECTIVE_DELETION_INCOMPLETE:
        return (
            f"Objective {objective_id} is still being deleted; these did not finish: "
            f"{_parts(details)}. Send {retry} again to finish it."
        )
    fixed = {
        OBJECTIVE_CHANGED: (
            f"Objective {objective_id} changed since you last looked at it, so nothing was "
            "done. Check /objectives and try again."
        ),
        OBJECTIVE_NOT_FOUND: f"No Objective {objective_id} was found for you, so nothing was done.",
        OBJECTIVE_OWNER_MISMATCH: f"Objective {objective_id} belongs to someone else, so you can't change it.",
        OBJECTIVE_UPDATE_FAILED: (
            f"The change to Objective {objective_id} could not be saved. Try again later; if it "
            "keeps failing, tell an administrator."
        ),
        OBJECTIVE_ALREADY_DELETED: f"Objective {objective_id} was already deleted.",
        OBJECTIVE_DELETED: f"Objective {objective_id} was deleted.",
    }
    if code in fixed:
        return fixed[code]
    if code.startswith(_STATUS_PREFIX) and response.success:
        return f"Objective {objective_id} is now {_status_words(code.removeprefix(_STATUS_PREFIX))}."
    return None


__all__ = ["objective_control_text"]
