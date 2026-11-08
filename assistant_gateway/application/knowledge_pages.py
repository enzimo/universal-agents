"""The Web Chat Knowledge page's use case: people's requests to the team's knowledge base.

The knowledge base lives in the team's database, which the team services serve: each
request becomes one ``knowledge.*`` command for the signed-in person, naming them by
their person reference (``assistant_gateway.application.team_commands``). The team
services decide whether the person may (``team_services.knowledge``); the gateway only
converts the request and the reply (``knowledge_wire``).

Before answering the browser, the gateway names who made each change: an agent by its
type, the signed-in person as "You", another person by their display name from the
gateway's people records. Person ids and references never reach the browser.

A list reply can hold fewer rows than asked for, when more would not fit in one reply
(contract README "Reply size"); it then says where to continue (``next_offset``). The
browser pages through fixed pages of 20 with a total, so the gateway asks again from
there until it has the page the browser asked for (``_whole_page``).
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any, Callable, Protocol

from pydantic import ValidationError

from assistant_gateway.application.knowledge_wire import (
    KnowledgeRequestNotSent,
    current_revision,
    knowledge_request,
    knowledge_view,
    refusal_code,
    review_flags,
)
from assistant_gateway.application.route_context import GatewayRouteContextV1
from assistant_gateway.application.team_commands import GatewayTeamCommandService
from custom_messages.contracts.control_v1 import (
    KnowledgeCommandAction,
    KnowledgeCommandPayload,
    KnowledgeCommandResultPayload,
    KnowledgeReviewFlagPayload,
)

LOGGER = logging.getLogger(__name__)

YOU_LABEL = "You"
TEAM_MEMBER_LABEL = "A team member"
SYSTEM_LABEL = "The system"
AGENT_LABEL = "An agent"
SIGN_IN_TEXT = "Sign in with a registered user account to use the Knowledge page."
NOT_SENDABLE_TEXT = (
    "Nothing was sent because the request was not valid (a value is missing, too long or of the wrong kind). "
    "Check the fields and try again."
)
# Lists the gateway completes from their continuations (module docstring), and their rows.
CONTINUED_ROWS = {
    KnowledgeCommandAction.LIST: "pages",
    KnowledgeCommandAction.LIST_CONTRIBUTIONS: "contributions",
    KnowledgeCommandAction.HISTORY: "revisions",
}


class KnowledgeEditorReferences(Protocol):
    """Reads the gateway's copy of the team's people to name editors; never writes it."""

    def person_ref(self, person_id: str) -> str | None:
        """The person's opaque reference, or None when the copy does not hold them."""

    def person_id(self, person_ref: str) -> str | None:
        """Who a reference in a reply names, or None when no recorded person has it."""


class GatewayKnowledgeRefused(Exception):
    """The team services refused or could not run a Knowledge page request."""

    def __init__(
        self,
        code: str,
        message: str,
        *,
        review_flags: tuple[KnowledgeReviewFlagPayload, ...] = (),
        current_revision: int = 0,
        merged_into: str = "",
    ) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.review_flags = review_flags
        self.current_revision = current_revision
        self.merged_into = merged_into


class GatewayKnowledgePages:
    """Run Knowledge page requests for the signed-in person (module docstring)."""

    def __init__(
        self,
        *,
        team_commands: GatewayTeamCommandService,
        references: KnowledgeEditorReferences,
        display_name: Callable[[str], str],
        enabled: bool,
    ) -> None:
        self._team_commands = team_commands
        self._references = references
        self._display_name = display_name
        self._enabled = bool(enabled)

    @property
    def enabled(self) -> bool:
        """Whether the team's knowledge base is on (``agent.knowledge.enabled``); the rail link shows only then."""

        return self._enabled

    async def run(
        self, context: GatewayRouteContextV1, payload: KnowledgeCommandPayload
    ) -> KnowledgeCommandResultPayload:
        """The team services' result with editors named; ``GatewayKnowledgeRefused`` when refused."""

        person_id = str(context.canonical_user_id or "").strip()
        if not person_id:
            raise PermissionError(SIGN_IN_TEXT)
        try:
            kind, body = knowledge_request(payload)
        except KnowledgeRequestNotSent as error:
            raise GatewayKnowledgeRefused("invalid_request", str(error)) from error
        reply = await self._request(kind, body, person_id, payload.action)
        rows = CONTINUED_ROWS.get(payload.action)
        if rows is not None:
            reply = await self._whole_page(kind, body, person_id, payload.action, reply, rows)
        outcome = str(reply.get("outcome"))
        if outcome != "ok":
            raise GatewayKnowledgeRefused(
                refusal_code(outcome),
                str(reply.get("message") or ""),
                review_flags=review_flags(reply),
                current_revision=current_revision(reply),
                merged_into=str(reply.get("merged_into") or ""),
            )
        # Names come from the gateway's people records (database reads).
        return await asyncio.to_thread(self._view, payload, reply, person_id)

    async def _request(self, kind: str, body: dict[str, Any], person_id: str,
                       action: KnowledgeCommandAction) -> dict[str, Any]:
        try:
            return await self._team_commands.request(kind, body, person_id=person_id)
        except ValidationError as error:
            # The browser's request model let through a value the contract refuses: the gateway's defect.
            LOGGER.error("A Knowledge page request did not fit the message contract and was not sent. "
                         "event=web_knowledge_request_not_sendable action=%s errors=%s", action.value,
                         [".".join(str(part) for part in item["loc"]) for item in error.errors()])
            raise GatewayKnowledgeRefused("invalid_request", NOT_SENDABLE_TEXT) from error

    async def _whole_page(self, kind: str, body: dict[str, Any], person_id: str, action: KnowledgeCommandAction,
                          reply: dict[str, Any], rows: str) -> dict[str, Any]:
        """The rows the browser asked for, asking again from each ``next_offset`` (module docstring)."""

        end = int(body["offset"]) + int(body["limit"])
        while reply.get("outcome") == "ok" and int(reply.get("next_offset", end)) < end:
            start = int(reply["next_offset"])
            more = await self._request(kind, {**body, "offset": start, "limit": end - start}, person_id, action)
            if more.get("outcome") != "ok":
                return more
            if not more.get(rows):
                LOGGER.warning("A Knowledge list continuation returned no rows; the browser gets the rows so far. "
                               "event=web_knowledge_continuation_empty action=%s offset=%s", action.value, start)
                return reply
            reply = {**more, rows: [*reply[rows], *more[rows]]}
        return reply

    def _view(self, payload: KnowledgeCommandPayload, reply: dict[str, Any], person_id: str) -> KnowledgeCommandResultPayload:
        own_reference = self._references.person_ref(person_id)
        names: dict[str, str] = {}

        def label(editor: dict[str, Any]) -> str:
            kind = editor.get("kind")
            if kind == "agent":
                return str(editor.get("agent_type") or AGENT_LABEL)
            if kind != "person":
                return SYSTEM_LABEL
            reference = str(editor.get("person") or "")
            if not reference:
                # Someone the team's people records no longer hold (contract README, "Knowledge").
                return TEAM_MEMBER_LABEL
            if reference == own_reference:
                return YOU_LABEL
            if reference not in names:
                names[reference] = self._person_name(reference)
            return names[reference]

        return knowledge_view(payload.action, reply, label=label)

    def _person_name(self, reference: str) -> str:
        editor_id = self._references.person_id(reference) if reference else None
        name = self._display_name(editor_id) if editor_id else ""
        # The people records answer with the id itself when they have no name.
        return name if name and name != editor_id else TEAM_MEMBER_LABEL


__all__ = ["GatewayKnowledgePages", "GatewayKnowledgeRefused"]
