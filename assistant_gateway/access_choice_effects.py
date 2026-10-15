"""What each review choice does, in plain words (owner's request 2026-09-30).

A one-word button such as "Approve" or "Allow and keep" does not say what
happens. Every card and chat announcement therefore says, for each choice, who
ends up holding what, for how long, whether the team lead may hand it on, and
what "Deny" does. The words follow "access follows the work"
(``access_control.requests.AccessRequests.approval_specs``):

- a worker's access for one task covers that task only, and nothing is kept;
- kept access from a worker's request goes to the team lead, which gives it to
  its workers' tasks; in a scheduled run it goes to that schedule instead;
- the team lead's own kept access may be handed on when the person lets it.

The web card picks a variant from its checkboxes: ``task`` or ``kept`` (Keep for
later tasks), each with a ``_no_hand_on`` form when the team lead's "may hand
it on" box is unticked. Trusted code writes every word here from the request's
own fields; the agent's reason never shapes them.
"""

from __future__ import annotations

from dataclasses import dataclass

from access_control.requests import ACCESS_PROPOSAL_CHANGE, LEVEL_WORDS
from custom_messages.contracts.access_v2 import (
    TASK_BOUND_LIFETIMES,
    AccessLevel,
    AccessRequestKind,
    AccessRequestV1,
    AccessReviewChoice,
)

TASK = "task"
KEPT = "kept"
NO_HAND_ON = "_no_hand_on"
ANY = "any"
REMOVE_HINT = "Remove it on the Access page any time."


@dataclass(frozen=True)
class ChoiceWords:
    """The names a request's effects are said with."""

    lead: str  # the team lead's agent type
    tool: str  # the target's display name
    whose: str  # "you" or the person's name, for "your later tasks"
    person_lacks_you: bool | None = None  # None: the person has the access; else whether it is the viewer

    @property
    def later(self) -> str:
        return "your later tasks" if self.whose == "you" else f"{self.whose}'s later tasks"


def default_variant(request: AccessRequestV1) -> str:
    """The variant a card shows before any box is touched: what the agent asked for.

    The "Keep for later tasks" box starts ticked when the agent asked to keep
    the access (owner's decision 2026-10-01: approving with the defaults gave
    TeamLead task-only access it had asked to keep and pass on).
    """

    return TASK if request.lifetime in TASK_BOUND_LIFETIMES else KEPT


def choice_effects(request: AccessRequestV1, words: ChoiceWords) -> dict[str, dict[str, str]]:
    """Choice value -> variant -> what it does (see the module docstring)."""

    if request.kind is AccessRequestKind.CHANGE:
        is_proposal = request.change is not None and request.change.kind == ACCESS_PROPOSAL_CHANGE
        approve = (
            "Applies every item above together. Access rows you did not tick for later last for this task only."
            if is_proposal
            else "Applies exactly what is listed above, once. Nothing else changes."
        )
        return {
            AccessReviewChoice.APPROVE.value: {ANY: approve},
            AccessReviewChoice.APPROVE_ALL.value: {ANY: approve},
            AccessReviewChoice.DENY.value: {ANY: "Nothing changes."},
        }
    agent = request.requester_agent_type or "The agent"
    lacks = ""
    if words.person_lacks_you is True:
        lacks = " You get this access too, because agents never exceed the person they work for."
    elif words.person_lacks_you is False:
        lacks = f" {words.whose} gets this access too, because agents never exceed the person they work for."

    def granted(level: AccessLevel) -> dict[str, str]:
        amount = LEVEL_WORDS[level]
        if agent == words.lead:
            return {
                TASK: f"{agent} can use {words.tool} ({amount}) for this task only, and so can the workers "
                f"helping with it; nothing is kept.{lacks}",
                TASK + NO_HAND_ON: f"{agent} can use {words.tool} ({amount}) for this task only; nothing is "
                f"kept.{lacks}",
                KEPT: f"{agent} keeps {amount} access to {words.tool} for {words.later} and gives it to its "
                f"workers' tasks without asking. {REMOVE_HINT}{lacks}",
                KEPT + NO_HAND_ON: f"{agent} keeps {amount} access to {words.tool} for {words.later}; its workers "
                f"ask each time. {REMOVE_HINT}{lacks}",
            }
        if request.schedule_id:
            return {
                TASK: f"{agent} can use {words.tool} ({amount}) for this run only; nothing is kept.{lacks}",
                KEPT: f"This scheduled job keeps {amount} access to {words.tool} for its later runs, and only "
                f"there. {REMOVE_HINT}{lacks}",
            }
        return {
            TASK: f"{agent} can use {words.tool} ({amount}) for this task only; nothing is kept.{lacks}",
            KEPT: f"{words.lead} keeps {amount} access to {words.tool} for {words.later} and gives it to "
            f"{agent} and its other workers' tasks without asking; {agent} can use it now. {REMOVE_HINT}{lacks}",
        }

    effects = {
        AccessReviewChoice.APPROVE.value: granted(request.level),
        AccessReviewChoice.APPROVE_TASK.value: {ANY: granted(request.level)[TASK]},
        AccessReviewChoice.DENY.value: {ANY: f"Nothing is given, and {agent} is told you said no."},
    }
    if request.level is not AccessLevel.READ:
        effects[AccessReviewChoice.APPROVE_READ.value] = granted(AccessLevel.READ)
    return effects


def chat_effect_lines(
    request: AccessRequestV1, words: ChoiceWords, labels: dict[str, str], choices: list[str]
) -> list[str]:
    """One line per chat button ("Allow for this task: ..."), for channels with no hover or card."""

    effects = choice_effects(request, words)
    lines = []
    for choice in choices:
        variants = effects.get(choice, {})
        # Chat's "Allow" buttons keep what was asked for ("Allow and keep"); "for this task" is its own button.
        text = variants.get(ANY) or variants.get(KEPT) or variants.get(TASK)
        if text and choice in labels:
            lines.append(f"{labels[choice]}: {text}")
    return lines


__all__ = [
    "ANY",
    "ChoiceWords",
    "KEPT",
    "NO_HAND_ON",
    "TASK",
    "chat_effect_lines",
    "choice_effects",
    "default_variant",
]
