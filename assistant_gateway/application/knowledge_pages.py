"""The Web Chat Knowledge page's use case: people's requests to the manager's knowledge base.

The knowledge base lives in the manager's database, so every request becomes
one ``ManagerKnowledgeCommand`` for the signed-in person
(``GatewayCommandOperationsApplication.execute_knowledge_command``). The
gateway checks the same ownership rule first (``access_control.knowledge_access``
through ``GatewayManagerCommandService``); the manager decides again with the
gateway-signed person and runs the knowledge service's rules.

Before answering the browser, the gateway names who made each change: an
agent by its type, the signed-in person as "You", another person by their
display name from the gateway's people records. Person ids never reach the
browser.
"""

from __future__ import annotations

import asyncio
from typing import Any, Callable, Protocol

from assistant_gateway.application.route_context import GatewayRouteContextV1
from custom_messages.contracts.control_v1 import (
    KnowledgeCommandPayload,
    KnowledgeCommandResultPayload,
    KnowledgeReviewFlagPayload,
    ManagerKnowledgeCommand,
)
from knowledge_base.vocabulary import ActorKind

# Keys of the manager's result that hold who made a change (KnowledgeEditorPayload).
_EDITOR_KEYS = frozenset({"editor", "last_editor"})
YOU_LABEL = "You"
TEAM_MEMBER_LABEL = "A team member"
SYSTEM_LABEL = "The system"
AGENT_LABEL = "An agent"
SIGN_IN_TEXT = "Sign in with a registered user account to use the Knowledge page."


class GatewayKnowledgeRefused(Exception):
    """The manager refused or could not run a Knowledge page request."""

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


class KnowledgeCommandRunner(Protocol):
    async def execute_knowledge_command(
        self, command: ManagerKnowledgeCommand, *, context: GatewayRouteContextV1
    ) -> ManagerKnowledgeCommand: ...


class GatewayKnowledgePages:
    """Run Knowledge page requests for the signed-in person (module docstring)."""

    def __init__(
        self, *, runner: KnowledgeCommandRunner, display_name: Callable[[str], str], enabled: bool
    ) -> None:
        self._runner = runner
        self._display_name = display_name
        self._enabled = bool(enabled)

    @property
    def enabled(self) -> bool:
        """Whether the team's knowledge base is on (``agent.knowledge.enabled``); the rail link shows only then."""

        return self._enabled

    async def run(
        self, context: GatewayRouteContextV1, payload: KnowledgeCommandPayload
    ) -> KnowledgeCommandResultPayload:
        """The manager's result with editors named; ``GatewayKnowledgeRefused`` when refused."""

        person_id = str(context.canonical_user_id or "").strip()
        if not person_id:
            raise PermissionError(SIGN_IN_TEXT)
        response = await self._runner.execute_knowledge_command(
            ManagerKnowledgeCommand(command=payload), context=context
        )
        if response.success is not True or response.result is None:
            raise GatewayKnowledgeRefused(
                response.error_code or "runtime_error",
                response.error_message,
                review_flags=tuple(response.review_flags),
                current_revision=response.current_revision,
                merged_into=response.merged_into,
            )
        # Names come from the gateway's people records (a database read).
        named = await asyncio.to_thread(self._named, response.result.model_dump(mode="json"), person_id)
        return KnowledgeCommandResultPayload.model_validate(named)

    def _named(self, value: Any, person_id: str) -> Any:
        """The result with every editor labelled and its person id removed."""

        if isinstance(value, list):
            return [self._named(item, person_id) for item in value]
        if not isinstance(value, dict):
            return value
        return {
            key: {**item, "label": self._editor_label(item, person_id), "person_id": ""}
            if key in _EDITOR_KEYS and isinstance(item, dict)
            else self._named(item, person_id)
            for key, item in value.items()
        }

    def _editor_label(self, editor: dict[str, Any], person_id: str) -> str:
        kind = editor.get("kind")
        if kind == ActorKind.AGENT.value:
            return str(editor.get("agent_type") or AGENT_LABEL)
        if kind != ActorKind.PERSON.value:
            return SYSTEM_LABEL
        editor_id = str(editor.get("person_id") or "")
        if editor_id == person_id:
            return YOU_LABEL
        name = self._display_name(editor_id) if editor_id else ""
        # The people records answer with the id itself when they have no name.
        return name if name and name != editor_id else TEAM_MEMBER_LABEL


__all__ = ["GatewayKnowledgePages", "GatewayKnowledgeRefused", "KnowledgeCommandRunner"]
