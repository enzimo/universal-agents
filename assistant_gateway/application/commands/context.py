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
    """Identity and delivery coordinates supplied by a trusted adapter.

    ``private_chat`` says the chat has only this person and the assistant
    (Telegram ``chat.type == "private"``, a Discord DM, Web Chat, a Signal or
    Photon chat that is not a group, a WhatsApp conversation, the local CLI).
    The channel decides; a chat it cannot vouch for is not private, so
    commands whose reply must stay private (``/upgrade``) refuse there.
    """

    channel_name: str | None = None
    user_id: str | None = None
    chat_id: str | None = None
    thread_id: str | None = None
    canonical_user_id: str | None = None
    private_chat: bool = False


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
