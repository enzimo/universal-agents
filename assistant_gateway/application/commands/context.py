"""Typed actor coordinates shared by interactive command handlers."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol, runtime_checkable


DEBUG_ONLY_COMMAND_NOTICE = (
    "This command is available only when Web Chat debug mode is enabled."
)
UNLINKED_USER_NOTICE = (
    "This channel identity is not linked to a Universal Agents user. "
    "Ask an admin to add you or use an admin-provided /user link code."
)


@dataclass(frozen=True)
class GatewayActorContext:
    """Identity and delivery coordinates supplied by a trusted adapter."""

    channel_name: str | None = None
    user_id: str | None = None
    chat_id: str | None = None
    thread_id: str | None = None
    canonical_user_id: str | None = None


@runtime_checkable
class GatewayCommandActorResolver(Protocol):
    """Resolve a canonical user from trusted adapter coordinates."""

    def canonical_user_id_for_context(
        self,
        *,
        channel_name: str | None,
        user_id: str | None,
        chat_id: str | None,
        canonical_user_id: str | None = None,
    ) -> str | None:
        """Return the canonical user for the supplied actor coordinates."""


def resolve_actor_canonical_user_id(
    actor: GatewayActorContext,
    resolver: GatewayCommandActorResolver,
) -> str | None:
    """Prefer an authenticated actor id, then resolve trusted coordinates."""

    explicit_owner = str(actor.canonical_user_id or "").strip()
    if explicit_owner:
        return explicit_owner
    return resolver.canonical_user_id_for_context(
        channel_name=actor.channel_name,
        user_id=actor.user_id,
        chat_id=actor.chat_id,
    )
