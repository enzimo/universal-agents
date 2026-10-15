"""Memory-only gateway presentation contracts for short-lived user actions."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Final, Literal

from assistant_gateway.domain.transcript import GatewayTranscriptMessage


SECURE_CREDENTIAL_COLLECTION_READY_TEXT = (
    "Secure credential entry is ready in the trusted credential workflow."
)
TOOL_SETUP_READY_TEXT = "A setup page is ready in the trusted credential workflow."
# A setup page (sign-in, account link or team sign-in app) an agent task asked
# for; the task waits for the result (assistant_gateway.application.tool_setup).
TOOL_SETUP_ACTION_KIND: Final = "tool_setup"
# Form a person opened with /link; the gateway watches it and posts how it
# ended as a link_result message (assistant_gateway.application.link_results).
ACCOUNT_LINK_COMMAND_ACTION_KIND: Final = "account_link"


@dataclass(frozen=True)
class GatewayTransientAction:
    """One trusted browser action that must not enter durable transcript state."""

    kind: Literal[
        "broker_authentication",
        "account_link",
        "secure_credential_collection",
        "tool_setup",
    ]
    label: str
    description: str
    url: str
    expires_at_utc: str | None = None
    # Secret-free line kept in chat history in place of the card, such as
    # "Account link form opened for mcp:crawl4ai (the team's account)." Without
    # one, the transcript keeps its fixed safe text for the reply's type.
    receipt: str | None = None
    # The page's own id (a setup request id), when one chat request shows several
    # pages; Web Chat closes the card when a message with this correlation ends it.
    request_id: str | None = None


@dataclass(frozen=True)
class PendingCredentialAction:
    """Owner-scoped live link positioned by its secret-free transcript receipt."""

    owner: str
    session_key: str
    correlation_id: str
    request_id: str
    anchor: GatewayTranscriptMessage
    expires_at: datetime
    action: GatewayTransientAction = field(repr=False)
