"""replied message v1 contracts, independent of runtime hosts and SDKs.

A person can answer a specific earlier chat message with their channel's reply
feature (Telegram's Reply). The message they answered often comes from another
conversation than the one their chat continues: a scheduled job's report, a
background check or a notification is delivered into the chat but filed under
its own conversation. Without the answered message the agent sees only the
person's words ("expected") and cannot tell what they refer to.

Channels fill ``RepliedMessage`` from the platform's own reply data (for
Telegram, the replied message the Bot API delivers with the update), so it is
what the chat showed. It travels beside the person's words on Task, TaskQuery
and TaskSteering, never inside them:
agents show it labeled by its author and as background. Text from the assistant
or another member never counts as the person's instructions, because the
assistant's earlier messages can quote outside content (emails, web pages) and
other members' messages are not the person's.
"""

from pydantic import BaseModel
from pydantic import Field
from pydantic import field_validator
from typing import Literal
from typing import Optional


# Longest replied text an agent receives. Telegram rich messages reach 32,768
# characters; a report that long would crowd the request, so the text is cut
# and marked as shortened (the channel logs the cut).
REPLIED_MESSAGE_MAX_CHARS = 16000
REPLIED_MESSAGE_AUTHOR_NAME_MAX_CHARS = 128

# Who wrote the replied message: this team's assistant (the bot), the person
# sending the reply, or another member of the chat.
RepliedMessageAuthor = Literal["assistant", "requester", "other"]


class RepliedMessage(BaseModel):
    """An earlier chat message that a person's request or steering replies to."""

    author: RepliedMessageAuthor = Field(
        description="Who wrote the replied message: 'assistant' (this team's assistant), 'requester' (the person replying) or 'other' (another chat member)."
    )
    author_name: Optional[str] = Field(
        default=None,
        max_length=REPLIED_MESSAGE_AUTHOR_NAME_MAX_CHARS,
        description="The chat's display name for another member; set only when author is 'other'. Chosen by that member, so it is a label, not an identity.",
    )
    text: str = Field(
        min_length=1,
        max_length=REPLIED_MESSAGE_MAX_CHARS,
        description="Text (or caption) of the replied message as the chat showed it.",
    )
    shortened: bool = Field(
        default=False,
        description="True when the text was cut to REPLIED_MESSAGE_MAX_CHARS.",
    )
    quoted_text: Optional[str] = Field(
        default=None,
        max_length=REPLIED_MESSAGE_MAX_CHARS,
        description="The part of the replied message the person highlighted, when the channel supports quoting part of a message.",
    )
    sent_at: Optional[str] = Field(
        default=None,
        description="When the replied message was sent (ISO 8601, UTC), when the channel reports it.",
    )

    @field_validator("author_name")
    @classmethod
    def _one_line_name(cls, value: Optional[str]) -> Optional[str]:
        """A display name is shown inside a label line, so it stays on one line."""

        if value is None:
            return None
        return " ".join(value.split()) or None

    @classmethod
    def bounded(
        cls,
        *,
        author: RepliedMessageAuthor,
        text: str,
        author_name: Optional[str] = None,
        quoted_text: Optional[str] = None,
        sent_at: Optional[str] = None,
    ) -> "RepliedMessage":
        """Build one from channel data, cutting text to the contract's limits."""

        shortened = len(text) > REPLIED_MESSAGE_MAX_CHARS
        name = " ".join((author_name or "").split()) or None
        quote = (quoted_text or "").strip() or None
        return cls(
            author=author,
            author_name=name[:REPLIED_MESSAGE_AUTHOR_NAME_MAX_CHARS] if name and author == "other" else None,
            text=text[:REPLIED_MESSAGE_MAX_CHARS],
            shortened=shortened,
            quoted_text=quote[:REPLIED_MESSAGE_MAX_CHARS] if quote else None,
            sent_at=sent_at,
        )
