"""Identity and routing context for gateway request applications."""

from __future__ import annotations

from dataclasses import dataclass

from assistant_gateway.application.identity_service import (
    GatewayIdentityService,
    ResolvedGatewayUser,
)
from access_control.identity import ContextSigner
from access_control.people import effective_roles
from assistant_gateway.domain.sessions import SessionRecord
from custom_messages.contracts.routing_v1 import GatewayContext


@dataclass(frozen=True)
class GatewayRequestContextSettings:
    """Stable identity and routing settings shared by request applications."""

    instance_name: str
    team_id: str
    default_canonical_user_id: str
    default_display_name: str
    default_timezone: str
    heartbeat_source_id: str | None
    users_enabled: bool


@dataclass(frozen=True)
class GatewayRequestRoute:
    """Authenticated external route associated with one gateway request."""

    channel_name: str | None = None
    user_id: str | None = None
    chat_id: str | None = None
    chat_scope: str | None = None
    thread_id: str | None = None
    original_message_id: str | None = None
    canonical_user_id: str | None = None


@dataclass(frozen=True)
class GatewayRequesterPresentation:
    """Trusted requester identity and presentation settings."""

    canonical_user_id: str | None
    display_name: str
    timezone: str


@dataclass(frozen=True)
class GatewayAdmissionDecision:
    """Admission outcome for one interactive external route."""

    allowed: bool
    rejection_code: str | None = None
    message: str | None = None
    canonical_user_id: str | None = None
    user_status: str | None = None



@dataclass(frozen=True)
class GatewayCommandPerson:
    """The signed-in person a gateway operation is for, for the gateway's own checks."""

    canonical_user_id: str
    team_id: str
    roles: tuple[str, ...]

class GatewayRequestContextService:
    """Own canonical identity and manager-facing request context construction."""

    def __init__(
        self,
        *,
        settings: GatewayRequestContextSettings,
        identity: GatewayIdentityService,
        context_signer: ContextSigner | None = None,
    ) -> None:
        self.settings = settings
        self._identity = identity
        self._context_signer = context_signer

    @property
    def users_enabled(self) -> bool:
        return self.settings.users_enabled

    def chat_scope(self, route: GatewayRequestRoute) -> str:
        if route.chat_scope is not None:
            return str(route.chat_scope).strip()
        channel_name = str(route.channel_name or "").strip()
        if channel_name == "cli":
            return str(route.chat_id or f"cli:{self.settings.instance_name}").strip()
        return ""

    def resolve_canonical_user(
        self,
        route: GatewayRequestRoute,
    ) -> ResolvedGatewayUser | None:
        if not self.settings.users_enabled:
            return None
        channel_name = str(route.channel_name or "").strip()
        external_user_id = str(route.user_id or "").strip()
        chat_scope = self.chat_scope(route)
        if not channel_name and not external_user_id:
            channel_name = "cli"
            external_user_id = self.settings.default_canonical_user_id
            chat_scope = f"cli:{self.settings.instance_name}"
        if not channel_name or not external_user_id:
            return None
        return self._identity.resolve_canonical_user(
            channel_name=channel_name,
            external_user_id=external_user_id,
            chat_scope=chat_scope,
            seed_default_if_missing=channel_name == "cli",
        )

    def canonical_user_id(self, route: GatewayRequestRoute) -> str | None:
        requested = str(route.canonical_user_id or "").strip()
        if requested:
            return requested
        resolved = self.resolve_canonical_user(route)
        if resolved is not None:
            return resolved.user.canonical_user_id
        if not route.channel_name or route.channel_name == "cli":
            return self.settings.default_canonical_user_id
        return None

    def resolve_requesting_user(
        self,
        route: GatewayRequestRoute,
    ) -> ResolvedGatewayUser | None:
        channel_user = self.resolve_canonical_user(route)
        requested_user_id = str(route.canonical_user_id or "").strip()
        if not requested_user_id:
            return channel_user
        authenticated_user = self._identity.resolve_user_by_id(requested_user_id)
        if authenticated_user is None:
            return None
        if (
            channel_user is not None
            and channel_user.user.canonical_user_id != requested_user_id
        ):
            raise PermissionError(
                "The authenticated canonical user does not match the channel identity."
            )
        return authenticated_user

    def admit_interactive_route(
        self,
        route: GatewayRequestRoute,
    ) -> GatewayAdmissionDecision:
        if (
            not self.settings.users_enabled
            or route.canonical_user_id is not None
            or not route.channel_name
            or str(route.channel_name).strip() == "cli"
        ):
            return GatewayAdmissionDecision(allowed=True)
        _binding, bound_user = self._identity.lookup_bound_user(
            channel_name=route.channel_name,
            external_user_id=route.user_id,
            chat_scope=self.chat_scope(route),
        )
        if bound_user is not None and bound_user.status != "active":
            return GatewayAdmissionDecision(
                allowed=False,
                rejection_code="disabled_user",
                message=(
                    "This Universal Agents user is disabled. "
                    "Ask an admin to re-enable the user."
                ),
                canonical_user_id=bound_user.canonical_user_id,
                user_status=bound_user.status,
            )
        if bound_user is None:
            return GatewayAdmissionDecision(
                allowed=False,
                rejection_code="unregistered_user",
                message=(
                    "This channel identity is not linked to a Universal Agents user. "
                    "Ask an admin to add you or use an admin-provided /user link code."
                ),
            )
        # An active person who was removed from this team (or was never in
        # it) resolves to nobody (identity.resolve_user_by_id), so refuse
        # them here rather than let the request run without a person.
        if self._identity.resolve_user_by_id(bound_user.canonical_user_id) is None:
            return GatewayAdmissionDecision(
                allowed=False,
                rejection_code="not_team_member",
                message=(
                    "You are not a member of this team. Ask an owner or "
                    "administrator to add you again."
                ),
                canonical_user_id=bound_user.canonical_user_id,
                user_status=bound_user.status,
            )
        return GatewayAdmissionDecision(allowed=True)

    def requester_presentation(
        self,
        route: GatewayRequestRoute,
    ) -> GatewayRequesterPresentation:
        if self.settings.users_enabled:
            resolved = self.resolve_requesting_user(route)
            if resolved is not None:
                return GatewayRequesterPresentation(
                    canonical_user_id=resolved.user.canonical_user_id,
                    display_name=resolved.user.display_name,
                    timezone=(
                        resolved.user.timezone or self._identity.timezone_for_user(None)
                    ),
                )
        return GatewayRequesterPresentation(
            canonical_user_id=self.canonical_user_id(route),
            display_name=self.settings.default_display_name,
            timezone=self.settings.default_timezone,
        )

    def requester_presentation_for_session(
        self,
        session: SessionRecord,
        route: GatewayRequestRoute = GatewayRequestRoute(),
    ) -> GatewayRequesterPresentation:
        effective_canonical_user_id = (
            session.owner_canonical_user_id or route.canonical_user_id
        )
        presentation = self.requester_presentation(
            GatewayRequestRoute(
                channel_name=route.channel_name or session.channel_name,
                user_id=route.user_id or session.user_id,
                chat_id=route.chat_id or session.chat_id,
                chat_scope=route.chat_scope,
                thread_id=route.thread_id or session.thread_id,
                original_message_id=route.original_message_id,
                canonical_user_id=effective_canonical_user_id,
            )
        )
        return GatewayRequesterPresentation(
            canonical_user_id=effective_canonical_user_id,
            display_name=presentation.display_name,
            timezone=presentation.timezone,
        )

    def manager_principal(
        self,
        route: GatewayRequestRoute,
    ) -> "GatewayCommandPerson":
        """The signed-in person for the gateway's own checks, if an active team member.

        Raises PermissionError otherwise. Commands to the manager do not carry
        this: the manager reads the person and roles only from the signed
        context the command carries (``build_gateway_context``).
        """

        resolved = self.resolve_canonical_user(route)
        requested_user_id = str(route.canonical_user_id or "").strip()
        if resolved is None and requested_user_id:
            resolved = self._identity.resolve_user_by_id(requested_user_id)
        if resolved is None:
            raise PermissionError(
                "This operation requires an active registered canonical user."
            )
        if requested_user_id and resolved.user.canonical_user_id != requested_user_id:
            raise PermissionError(
                "The requested canonical user does not match the authenticated gateway user."
            )
        roles = list(self._active_team_roles(resolved))
        if not roles:
            raise PermissionError(
                "This operation requires active membership in the current team."
            )
        return GatewayCommandPerson(
            canonical_user_id=resolved.user.canonical_user_id,
            team_id=self.settings.team_id,
            roles=tuple(roles),
        )

    def team_roles(self, canonical_user_id: str | None) -> tuple[str, ...]:
        """The person's current active roles in this gateway's team.

        Read from the gateway identity service each time, so a role removed a
        moment ago is not vouched for. Signed task contexts and account link
        forms both rely on this.
        """

        if not canonical_user_id:
            return ()
        resolved = self._identity.resolve_user_by_id(canonical_user_id)
        if resolved is None:
            return ()
        return self._active_team_roles(resolved)

    def _active_team_roles(self, resolved: ResolvedGatewayUser) -> tuple[str, ...]:
        # An owner is also an administrator everywhere roles are checked.
        return effective_roles(
            membership.role
            for membership in resolved.memberships
            if membership.team_id == self.settings.team_id
            and membership.status == "active"
        )

    def build_gateway_context(
        self,
        *,
        session_key: str,
        delivery_mode: str,
        route: GatewayRequestRoute = GatewayRequestRoute(),
        manager_agent_id: str | None,
        scheduled_job_id: str | None = None,
    ) -> GatewayContext:
        canonical_user_id = self.canonical_user_id(route)
        # The signed context is what enforcement points trust: the person and
        # their current team roles, vouched for by the gateway.
        task_context = (
            self._context_signer.issue(
                team_id=self.settings.team_id,
                person_id=canonical_user_id,
                person_roles=self.team_roles(canonical_user_id),
                schedule_id=scheduled_job_id,
            )
            if self._context_signer is not None
            else None
        )
        return GatewayContext(
            gateway_instance_name=self.settings.instance_name,
            session_key=session_key,
            channel_name=route.channel_name,
            user_id=route.user_id,
            canonical_user_id=canonical_user_id,
            chat_id=route.chat_id,
            thread_id=route.thread_id,
            delivery_mode=delivery_mode,
            scheduled_job_id=scheduled_job_id,
            schedule_owner_agent_id=manager_agent_id,
            heartbeat_source_id=self.settings.heartbeat_source_id,
            original_message_id=route.original_message_id,
            task_context=task_context,
        )
