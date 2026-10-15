"""The chat commands people are told to send, named once for every process that names them.

The gateway parses these commands (``assistant_gateway.application.commands``);
the gateway's replies, the broker's refusals and the agents' runtime tell
people which one to send. Building every such hint here keeps a renamed
command from leaving stale text in another process. Help pages and docs may
still spell commands out in prose.
"""

from __future__ import annotations

from typing import Literal

from custom_messages.contracts.access_v2 import LinkOwnerKind
from custom_messages.contracts.model_keys_v1 import MODEL_KEY_AUTH_PROVIDER

# Sign in to a tool, or choose which stored credential it uses (``/link <tool_id> [me|team]``).
LINK_COMMAND = "/link"
# Whose account agents use for each service, and the administrators' change (``access_control.account_sharing``).
ACCOUNTS_COMMAND = "/accounts"
ACCOUNTS_SHARE = "share"
ACCOUNTS_PRIVATE = "private"
# The subject ``/accounts`` takes for the team's model keys.
ACCOUNTS_MODEL_SUBJECT = "model"
# What a person still sets up for their own work (``assistant_gateway.personal_setup``).
SETUP_COMMAND = "/setup"
# The model key pages: the team's (administrators) and a person's own (``me``).
MODEL_KEY_COMMAND = f"/auth {MODEL_KEY_AUTH_PROVIDER}"
OWN_MODEL_COMMAND = f"{MODEL_KEY_COMMAND} me"


def link_command(tool_id: str, owner: LinkOwnerKind | None = None) -> str:
    """``/link <tool_id>`` for the person's own account, or ``/link <tool_id> team`` for the team's."""

    return f"{LINK_COMMAND} {tool_id} {LinkOwnerKind.TEAM.value}" if owner is LinkOwnerKind.TEAM else f"{LINK_COMMAND} {tool_id}"


def accounts_command(action: Literal["share", "private"], subject: str) -> str:
    """``/accounts share|private <tool_id|model>``."""

    return f"{ACCOUNTS_COMMAND} {action} {subject}"


__all__ = [
    "ACCOUNTS_COMMAND",
    "ACCOUNTS_MODEL_SUBJECT",
    "ACCOUNTS_PRIVATE",
    "ACCOUNTS_SHARE",
    "LINK_COMMAND",
    "MODEL_KEY_COMMAND",
    "OWN_MODEL_COMMAND",
    "SETUP_COMMAND",
    "accounts_command",
    "link_command",
]
