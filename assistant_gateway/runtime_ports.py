"""Default runtime adapters for gateway application ports."""

from __future__ import annotations

import hashlib
import inspect
import logging
import secrets
import string
from datetime import datetime, timezone
from typing import Awaitable, Callable

from access_control.ownership import private_access
from access_control.people import (
    is_administrator,
    removal_refusal,
    role_change_refusal,
    status_change_refusal,
)
from assistant_gateway.application.gateway_application import GatewayDeliveryEvent
from id_generation import new_id

OutputSink = Callable[[str], Awaitable[None] | None]
EventSink = Callable[[GatewayDeliveryEvent], Awaitable[None] | None]
_LINK_CODE_ALPHABET = string.ascii_letters + string.digits
_LINK_CODE_LENGTH = 10


class SystemGatewayClock:
    """Production clock backed by the system UTC time."""

    def now_utc(self) -> datetime:
        return datetime.now(timezone.utc)


class UuidGatewayIdGenerator:
    """Production identifier generator using the shared operational format."""

    def new_correlation_id(self) -> str:
        return new_id("corr_id")

    def new_local_correlation_id(self) -> str:
        return new_id("local_corr")

    def new_scheduled_job_id(self) -> str:
        return new_id("cron")

    def new_session_key(self) -> str:
        return new_id("session")

    def new_conversation_id(self) -> str:
        return new_id("conversation_id")

    def new_transcript_message_id(self) -> str:
        return new_id("web_msg")

    def new_transcript_attachment_id(self) -> str:
        return new_id("web_attachment")


class MembershipRoleIdentityAuthorizer:
    """Who administers people, from current team roles (``access_control.people``).

    Owners and administrators create, list, enable, disable, remove and invite
    people and change roles; only an owner changes who is an owner or disables
    or removes an owner, and nobody removes themselves. Roles come from
    the actor's active team memberships, read by the identity service at each
    call. This replaced the earlier local evaluator in
    ``custom_agents.authorization``, which allowed the same actions to the
    ``admin`` role only.
    """

    def can_create_user(
        self,
        *,
        actor_canonical_user_id: str,
        team_id: str,
        roles: tuple[str, ...],
    ) -> bool:
        return bool(str(actor_canonical_user_id or "").strip()) and is_administrator(roles)

    def can_list_users(
        self,
        *,
        actor_canonical_user_id: str,
        team_id: str,
        roles: tuple[str, ...],
    ) -> bool:
        return bool(str(actor_canonical_user_id or "").strip()) and is_administrator(roles)

    def can_create_web_invite(
        self,
        *,
        actor_canonical_user_id: str,
        target_canonical_user_id: str | None,
        team_id: str,
        roles: tuple[str, ...],
    ) -> bool:
        return bool(str(actor_canonical_user_id or "").strip()) and is_administrator(roles)

    def role_change_refusal(
        self,
        *,
        actor_roles: tuple[str, ...],
        current_role: str,
        new_role: str,
        team_has_owner: bool,
    ) -> str | None:
        return role_change_refusal(
            actor_roles=actor_roles,
            current_role=current_role,
            new_role=new_role,
            team_has_owner=team_has_owner,
        )

    def status_change_refusal(
        self,
        *,
        actor_roles: tuple[str, ...],
        target_role: str | None,
    ) -> str | None:
        return status_change_refusal(actor_roles=actor_roles, target_role=target_role)

    def removal_refusal(
        self,
        *,
        actor_id: str,
        actor_roles: tuple[str, ...],
        target_id: str,
        target_role: str | None,
    ) -> str | None:
        return removal_refusal(
            actor_id=actor_id,
            actor_roles=actor_roles,
            target_id=target_id,
            target_role=target_role,
        )


class OwnershipPrivateResourceAuthorizer:
    """Owner-scoped gateway decisions (``access_control.ownership``)."""

    def can_access(
        self,
        *,
        actor_canonical_user_id: str | None,
        owner_canonical_user_id: str | None,
        resource_type: str,
        resource_id: str,
        action: str,
        team_id: str | None = None,
        roles: tuple[str, ...] = (),
    ) -> bool:
        return private_access(
            person_id=actor_canonical_user_id,
            owner_id=owner_canonical_user_id,
            resource_type=resource_type,
            resource_id=resource_id,
            action=action,
            team_id=team_id,
            roles=roles,
        ).allowed


class SystemGatewayIdentitySecrets:
    """Production identity credential generator preserving current formats."""

    def hash_secret(self, raw_secret: str) -> str:
        return hashlib.sha256(str(raw_secret or "").encode("utf-8")).hexdigest()

    def new_web_user_token(self) -> str:
        return f"uauser_{secrets.token_urlsafe(32)}"

    def new_web_user_invite(self) -> str:
        return f"uainvite_{secrets.token_urlsafe(32)}"

    def new_link_code(self) -> str:
        # Filtering URL-safe base64 output can shorten the result whenever it
        # contains '-' or '_'. Sample the accepted alphabet directly so the
        # externally documented length is an invariant, not a best effort.
        return "".join(
            secrets.choice(_LINK_CODE_ALPHABET) for _ in range(_LINK_CODE_LENGTH)
        )


class CallbackGatewayEventOutput:
    """Compatibility output adapter for existing event and text callbacks."""

    def __init__(
        self,
        *,
        logger: logging.Logger,
        output_sink: OutputSink | None = None,
        event_sink: EventSink | None = None,
    ) -> None:
        self.logger = logger
        self.output_sink = output_sink
        self.event_sink = event_sink

    async def emit(self, event: GatewayDeliveryEvent) -> None:
        if self.event_sink is not None:
            result = self.event_sink(event)
            if inspect.isawaitable(result):
                await result
            return
        rendered_text = event.text_with_attachment_paths()
        if self.output_sink is None:
            self.logger.info(
                "Gateway output [%s/%s]: %s",
                event.channel_name or "local",
                event.chat_id or event.session_key,
                rendered_text,
            )
            return
        result = self.output_sink(rendered_text)
        if inspect.isawaitable(result):
            await result
