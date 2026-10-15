"""Provider interfaces for the secure tool broker."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Mapping, Protocol

from custom_messages.contracts.access_v2 import AccountLinkV1


@dataclass(frozen=True)
class ActionSpec:
    name: str
    description: str = ""
    returns_secret: bool = False


@dataclass(frozen=True)
class ValidatedAction:
    provider: str
    action: str
    params: Mapping[str, Any]


@dataclass(frozen=True)
class ToolContext:
    agent_id: str | None = None
    task_id: str | None = None
    conversation_id: str | None = None
    reason: str | None = None
    canonical_user_id: str | None = None
    team_id: str | None = None
    principal_type: str | None = None
    named_agent_type: str | None = None
    invocation_id: str | None = None
    # Chosen by the broker's access gate, never by the request.
    account_link: AccountLinkV1 | None = None

    def require_user(self) -> str:
        user_id = str(self.canonical_user_id or "").strip()
        if not user_id:
            raise ValueError(
                "A registered canonical user is required for credential operations."
            )
        return user_id


@dataclass
class ToolResult:
    status: str
    content: Mapping[str, Any] = field(default_factory=dict)
    error: str | None = None


class SecureToolProvider(Protocol):
    name: str
    actions: Mapping[str, ActionSpec]

    def validate(self, action: str, params: dict[str, Any]) -> ValidatedAction: ...

    def execute(self, action: ValidatedAction, context: ToolContext) -> ToolResult: ...
