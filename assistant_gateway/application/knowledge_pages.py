"""The Web Chat Knowledge page's use case: people's requests to the team's knowledge base.

The knowledge base lives in the team's database, which the team services serve: each
request becomes one ``knowledge.*`` command for the signed-in person, naming them by
their person reference (``assistant_gateway.application.team_commands``). The team
services decide whether the person may (``team_services.knowledge``); the gateway only
converts the request and the reply (``knowledge_wire``).

Before answering the browser, the gateway names who made each change: an agent by its
type, the signed-in person as "You", another person by their display name from the
gateway's people records. Person ids and references never reach the browser.
"""

from __future__ import annotations

import asyncio
from typing import Any, Callable

from assistant_gateway.application.knowledge_wire import (
    KnowledgeTextTooLong,
    current_revision,
    knowledge_request,
    knowledge_view,
    refusal_code,
    review_flags,
)
from assistant_gateway.application.route_context import GatewayRouteContextV1
from assistant_gateway.application.team_commands import GatewayPersonReferences, GatewayTeamCommandService
from custom_messages.contracts.control_v1 import (
    KnowledgeCommandPayload,
    KnowledgeCommandResultPayload,
    KnowledgeReviewFlagPayload,
)

YOU_LABEL = "You"
TEAM_MEMBER_LABEL = "A team member"
SYSTEM_LABEL = "The system"
AGENT_LABEL = "An agent"
SIGN_IN_TEXT = "Sign in with a registered user account to use the Knowledge page."


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
        references: GatewayPersonReferences,
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
        except KnowledgeTextTooLong as error:
            raise GatewayKnowledgeRefused("invalid_request", str(error)) from error
        reply = await self._team_commands.request(kind, body, person_id=person_id)
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
            if reference and reference == own_reference:
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
