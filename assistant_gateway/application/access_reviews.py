"""Deciding access reviews with buttons in chat channels (access plan section 9.1).

The gateway announces each waiting access request once, in a chat where the
person can decide it (``assistant_gateway.access_inbox``). The announcement
carries the request id and the review choices; Telegram and Discord show them
as buttons, other channels show the Inbox link in the text.

A press carries only the request id and the choice. Who decides is the person
linked to the pressing chat account, looked up again here with the same
lookup the ``/access`` command uses; the access service then checks that
person's current team roles when deciding (``GatewayAccessInbox.decide``). A
button therefore grants nothing by itself: its data needs no secret, and it
keeps working after the gateway restarts. A decision in chat wakes the
waiting agent exactly like a decision in the Inbox.
"""

from __future__ import annotations

from enum import StrEnum
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from custom_messages.contracts.access_v2 import AccessRequestStatus, AccessReviewChoice

# Chat buttons (no checkbox there): "keep" is its own button (ui/DESIGN.md,
# "Access Requests in Chat Channels").
ACCESS_REVIEW_CHOICE_LABELS: dict[str, str] = {
    AccessReviewChoice.APPROVE_TASK.value: "Allow for this task",
    AccessReviewChoice.APPROVE.value: "Allow and keep",
    AccessReviewChoice.APPROVE_READ.value: "Allow reading only",
    AccessReviewChoice.APPROVE_ALL.value: "Approve all as proposed",
    AccessReviewChoice.DENY.value: "Deny",
}
# The web card's buttons: the "Keep for later tasks" checkbox says how long.
WEB_REVIEW_CHOICE_LABELS: dict[str, str] = {
    AccessReviewChoice.APPROVE.value: "Approve",
    AccessReviewChoice.APPROVE_READ.value: "Approve reading only",
    AccessReviewChoice.DENY.value: "Deny",
}
# A staged change on the web card: approved as a whole.
WEB_CHANGE_CHOICE_LABELS: dict[str, str] = {
    AccessReviewChoice.APPROVE.value: "Approve",
    AccessReviewChoice.DENY.value: "Deny",
}
# An access proposal on the web card (ui/DESIGN.md): one button approves every
# row, and the rows the reviewer ticked are kept for later tasks.
WEB_PROPOSAL_CHOICE_LABELS: dict[str, str] = {
    AccessReviewChoice.APPROVE.value: "Approve all",
    AccessReviewChoice.DENY.value: "Deny",
}
# Button data is "uaar:<code>:<request id>"; Telegram allows 64 bytes of
# callback data, so choices travel as one letter.
ACCESS_REVIEW_CONTROL_PREFIX = "uaar:"
_CHOICE_CODES = {
    AccessReviewChoice.APPROVE.value: "a",
    AccessReviewChoice.APPROVE_READ.value: "r",
    AccessReviewChoice.APPROVE_TASK.value: "t",
    AccessReviewChoice.APPROVE_ALL.value: "p",
    AccessReviewChoice.DENY.value: "d",
}
_CODE_CHOICES = {code: choice for choice, code in _CHOICE_CODES.items()}
DECIDED_WORDS = {
    AccessReviewChoice.APPROVE.value: "Allowed and kept",
    AccessReviewChoice.APPROVE_READ.value: "Allowed reading only",
    AccessReviewChoice.APPROVE_TASK.value: "Allowed for this task",
    AccessReviewChoice.APPROVE_ALL.value: "Approved as proposed",
    AccessReviewChoice.DENY.value: "Denied",
}
CLOSED_TEXT = {
    AccessRequestStatus.GRANTED.value: "This request was already approved.",
    AccessRequestStatus.DENIED.value: "This request was already denied.",
    AccessRequestStatus.WITHDRAWN.value: "This request is no longer waiting: its task ended.",
}
NOT_LINKED_TEXT = (
    "This chat account is not linked to a gateway user, so it cannot decide requests. "
    "Link it with /user code and /user link, or decide in the Inbox."
)


def encode_review_control(request_id: str, choice: str) -> str:
    """Button data for one review choice."""

    return f"{ACCESS_REVIEW_CONTROL_PREFIX}{_CHOICE_CODES[choice]}:{request_id}"


def decode_review_control(data: str) -> tuple[str, str] | None:
    """(request id, choice) from button data, or None when it is not a review button."""

    text = str(data or "")
    if not text.startswith(ACCESS_REVIEW_CONTROL_PREFIX):
        return None
    code, separator, request_id = text[len(ACCESS_REVIEW_CONTROL_PREFIX) :].partition(":")
    choice = _CODE_CHOICES.get(code)
    if not separator or choice is None or not request_id.strip():
        return None
    return request_id.strip(), choice


def review_choice_label(choice: str) -> str:
    return ACCESS_REVIEW_CHOICE_LABELS.get(choice, choice)


class AccessReviewOutcome(StrEnum):
    DECIDED = "decided"  # this press decided the request
    CLOSED = "closed"  # the request is no longer waiting (decided elsewhere or withdrawn)
    REFUSED = "refused"  # the person may not decide it; the buttons stay
    SUBMITTED = "submitted"  # sent through the adapter host; the result arrives as a message


class AccessReviewDecisionRequestV1(BaseModel):
    """One button press: the choice and the chat route it came from."""

    model_config = ConfigDict(extra="forbid", frozen=True, str_strip_whitespace=True)
    schema_version: Literal[1] = 1
    request_id: str = Field(min_length=1, max_length=240)
    choice: str = Field(min_length=1, max_length=40)
    channel_name: str = Field(min_length=1, max_length=120)
    user_id: str = Field(min_length=1, max_length=240)
    chat_id: str = Field(min_length=1, max_length=240)
    thread_id: str | None = Field(default=None, max_length=240)


class AccessReviewDecisionResultV1(BaseModel):
    """What happened, as one plain line for the chat."""

    model_config = ConfigDict(extra="forbid", frozen=True)
    schema_version: Literal[1] = 1
    outcome: AccessReviewOutcome
    text: str = Field(min_length=1, max_length=2000)

    @property
    def closes_controls(self) -> bool:
        """Whether the buttons should be removed from the announcement."""

        return self.outcome in {AccessReviewOutcome.DECIDED, AccessReviewOutcome.CLOSED}


__all__ = [
    "ACCESS_REVIEW_CHOICE_LABELS",
    "ACCESS_REVIEW_CONTROL_PREFIX",
    "AccessReviewDecisionRequestV1",
    "AccessReviewDecisionResultV1",
    "AccessReviewOutcome",
    "CLOSED_TEXT",
    "DECIDED_WORDS",
    "NOT_LINKED_TEXT",
    "WEB_CHANGE_CHOICE_LABELS",
    "WEB_PROPOSAL_CHOICE_LABELS",
    "WEB_REVIEW_CHOICE_LABELS",
    "decode_review_control",
    "encode_review_control",
    "review_choice_label",
]
