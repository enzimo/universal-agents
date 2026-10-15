"""Runtime ports owned by the gateway application boundary."""

from __future__ import annotations

from datetime import datetime
from typing import Any, Awaitable, Callable, Protocol, runtime_checkable

from assistant_gateway.application.gateway_application import GatewayDeliveryEvent
from custom_messages.contracts.access_v2 import (
    AccountLinkRequestV1,
    OnboardingAccountKeysV1,
    PersonCredentialDeletionV1,
)
from custom_messages.contracts.envelope_v1 import MessageEnvelope


GatewayManagerEnvelopeHandler = Callable[[MessageEnvelope], Awaitable[None]]


class GatewaySecureBrokerRefusal(RuntimeError):
    """The secure broker answered a request with an HTTP error.

    ``message`` is the broker's own explanation (plain language for refusals
    such as "Only a team administrator or owner can link the team's
    account."); ``reason_code`` is the broker's stable code when it sent one.
    """

    def __init__(
        self, message: str, *, status: int, reason_code: str | None = None
    ) -> None:
        super().__init__(message)
        self.message = message
        self.status = status
        self.reason_code = reason_code


@runtime_checkable
class GatewayClock(Protocol):
    """Provide timezone-aware current time to gateway use cases."""

    def now_utc(self) -> datetime:
        """Return the current timezone-aware UTC timestamp."""


@runtime_checkable
class GatewayIdGenerator(Protocol):
    """Create identifiers whose format is part of the gateway contract."""

    def new_correlation_id(self) -> str:
        """Return a manager-request correlation identifier."""

    def new_local_correlation_id(self) -> str:
        """Return an identifier for a response produced without manager I/O."""

    def new_scheduled_job_id(self) -> str:
        """Return a manager-owned scheduled-job identifier."""

    def new_session_key(self) -> str:
        """Return a gateway session key."""

    def new_conversation_id(self) -> str:
        """Return a gateway conversation identifier."""

    def new_transcript_message_id(self) -> str:
        """Return an authoritative gateway transcript message identifier."""

    def new_transcript_attachment_id(self) -> str:
        """Return an identifier for a gateway-served response attachment."""


@runtime_checkable
class GatewayManagerTransport(Protocol):
    """Publish typed gateway envelopes to the manager transport boundary."""

    async def publish_durable(
        self,
        envelope: MessageEnvelope,
        *,
        routing_key: str,
    ) -> None:
        """Publish durable user or scheduled work."""

    async def publish_operational(
        self,
        envelope: MessageEnvelope,
        *,
        routing_key: str,
    ) -> None:
        """Publish non-durable operational control traffic."""


@runtime_checkable
class GatewayManagerRuntime(GatewayManagerTransport, Protocol):
    """Own the gateway's connection to its manager messaging runtime."""

    @property
    def connected(self) -> bool:
        """Return whether the runtime connection is active."""

    async def connect(
        self,
        *,
        on_response: GatewayManagerEnvelopeHandler,
        on_operational: GatewayManagerEnvelopeHandler,
        on_presence: GatewayManagerEnvelopeHandler,
    ) -> None:
        """Connect and install the gateway's inbound envelope handlers."""

    async def close(self) -> None:
        """Close subscriptions and the underlying runtime connection."""

    def stopped_receivers(self) -> tuple[Any, ...]:
        """Receive loops that ended without being asked to (shown on the Status page)."""

    async def manager_has_live_consumer(
        self,
        manager_id: str,
    ) -> bool | None:
        """Return whether the manager route has an active consumer when known."""

    async def put_object(
        self,
        *,
        bucket: str,
        object_name: str,
        path: str,
        ttl_seconds: float | None = None,
        max_bytes: int | None = None,
        headers: dict[str, str] | None = None,
    ) -> object:
        """Store an attachment object in the messaging runtime."""


@runtime_checkable
class GatewayEventOutput(Protocol):
    """Emit one application-owned delivery event."""

    async def emit(self, event: GatewayDeliveryEvent) -> None:
        """Deliver or render one gateway event."""


@runtime_checkable
class GatewayIdentityAuthorizer(Protocol):
    """Authorize identity-administration use cases."""

    def can_create_user(
        self,
        *,
        actor_canonical_user_id: str,
        team_id: str,
        roles: tuple[str, ...],
    ) -> bool:
        """Return whether the actor may create, link, enable, or disable users."""

    def can_list_users(
        self,
        *,
        actor_canonical_user_id: str,
        team_id: str,
        roles: tuple[str, ...],
    ) -> bool:
        """Return whether the actor may enumerate gateway users."""

    def can_create_web_invite(
        self,
        *,
        actor_canonical_user_id: str,
        target_canonical_user_id: str | None,
        team_id: str,
        roles: tuple[str, ...],
    ) -> bool:
        """Return whether the actor may invite a user into Web Chat."""

    def role_change_refusal(
        self,
        *,
        actor_roles: tuple[str, ...],
        current_role: str,
        new_role: str,
        team_has_owner: bool,
    ) -> str | None:
        """Return why the actor may not change a person's team role, or None."""

    def status_change_refusal(
        self,
        *,
        actor_roles: tuple[str, ...],
        target_role: str | None,
    ) -> str | None:
        """Return why the actor may not disable or enable a person, or None."""

    def removal_refusal(
        self,
        *,
        actor_id: str,
        actor_roles: tuple[str, ...],
        target_id: str,
        target_role: str | None,
    ) -> str | None:
        """Return why the actor may not remove a person from the team, or None."""


@runtime_checkable
class GatewayPeopleDirectory(Protocol):
    """Keep the access store's copy of people's standing and roles current.

    The manager's scheduler reads the copy at every scheduled run
    (``access_control.people_directory``); the gateway's own records stay the
    source. Implemented by ``assistant_gateway.people_directory_sync``.
    """

    def record(self, canonical_user_id: str) -> bool:
        """Copy one person's current standing after a change; False when the write failed (logged)."""


@runtime_checkable
class GatewayPersonSchedules(Protocol):
    """Install every per-person system schedule for one person.

    Per-person system schedules (``scope`` ``PERSON`` in
    ``custom_messages.system_schedules``; today the personal-facts audit and
    the stale-conversation review of memory review) are installed once per
    active person and owned by them. Identity code calls this after a person
    is added, enabled again, added back or restored, once the change is
    committed and the people copy is written. Safe to call repeatedly: a
    stored schedule keeps its pause and cadence. It never raises; when a set
    of schedules is off, the manager is unavailable or the person is not
    active it only logs. Implemented by
    ``GatewayMaintenanceScheduleApplication.ensure_person_schedules``.
    """

    async def ensure_person_schedules(self, person_id: str) -> None:
        """Install (or refresh) this person's schedules; does nothing, logged, when it cannot."""


@runtime_checkable
class GatewayIdentitySecrets(Protocol):
    """Create and hash gateway-owned user credentials."""

    def hash_secret(self, raw_secret: str) -> str:
        """Return the stable one-way representation persisted by identity storage."""

    def new_web_user_token(self) -> str:
        """Return a new browser authentication token."""

    def new_web_user_invite(self) -> str:
        """Return a new one-time browser invitation credential."""

    def new_link_code(self) -> str:
        """Return a short-lived cross-channel identity link code."""


@runtime_checkable
class GatewayPrivateResourceAuthorizer(Protocol):
    """Authorize one owner-scoped gateway or manager resource operation."""

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
        """Return whether the actor may perform the action on the resource."""


@runtime_checkable
class GatewayPersonChangeCheck(Protocol):
    """Whether a signed-in person may make one kind of platform change directly.

    The same people who could approve that change kind in the access Inbox
    (administrators with authorization on; any active member with it off).
    """

    def may_change(self, person_id: str | None, kind: str) -> bool:
        """Return whether the person may make changes of this kind."""


@runtime_checkable
class GatewaySecureBroker(Protocol):
    """Perform authenticated HTTP operations against the secure broker."""

    @property
    def available(self) -> bool:
        """Return whether the broker endpoint and gateway credential are present."""

    @property
    def collection_ttl_seconds(self) -> int:
        """Return the configured lifetime of one credential collection session."""

    @property
    def auth_unavailable_message(self) -> str | None:
        """Return a user-facing reason auth cannot start, or ``None``."""

    def user_form_url(self, internal_url: str) -> str:
        """Rewrite an internal form URL to the browser-facing broker origin."""

    async def get_auth_status(
        self,
        *,
        provider: str | None,
        context: dict[str, Any],
    ) -> dict[str, Any]:
        """Return broker authentication status for one user and provider."""

    async def configure_bitwarden_server(
        self,
        *,
        server_url: str,
        context: dict[str, Any],
    ) -> dict[str, Any]:
        """Configure the Bitwarden CLI server for one authenticated user."""

    async def create_auth_session(
        self,
        *,
        provider: str,
        method: str | None,
        context: dict[str, Any],
        options: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Create one broker-owned interactive authentication session.

        ``options`` are non-secret page inputs (``/auth model`` passes the
        configured model endpoints).
        """

    async def get_model_key_status(self) -> dict[str, Any]:
        """What the broker knows about the team's model keys (``ModelKeysStatusV1``; never a key)."""

    async def create_collection_session(
        self,
        *,
        request: dict[str, Any],
        context: dict[str, Any],
    ) -> dict[str, Any]:
        """Create a credential-collection session."""

    async def create_account_link_session(
        self, request: AccountLinkRequestV1
    ) -> dict[str, Any]:
        """Open the broker's account link form for one person and tool.

        Raises ``GatewaySecureBrokerRefusal`` with the broker's message and
        ``reason_code`` when the broker refuses (unknown tool, a tool that
        needs no account, or a team link by someone who is not an
        administrator).
        """

    async def execute_approved_signup(
        self,
        *,
        request: dict[str, Any],
        context: dict[str, Any],
    ) -> dict[str, Any] | None:
        """Attempt or classify an approved signup workflow."""

    async def get_collection_status(
        self,
        *,
        status_url: str,
        session_id: str,
    ) -> dict[str, Any]:
        """Fetch the current state of one credential-collection session."""

    async def get_account_link_status(
        self,
        *,
        status_url: str,
        session_id: str,
    ) -> dict[str, Any]:
        """Fetch the current state of one account link form."""

    async def get_sign_in_status(self, *, status_url: str) -> dict[str, Any]:
        """Fetch the current state of one sign-in page (``<login_url>/status``)."""

    async def delete_person_credentials(
        self, request: PersonCredentialDeletionV1
    ) -> dict[str, Any]:
        """Delete a removed person's stored credentials (``PersonCredentialDeletionResultV1`` as JSON)."""

    async def link_onboarding_account_keys(
        self, request: OnboardingAccountKeysV1
    ) -> dict[str, Any]:
        """Link the keys typed during onboarding as the team's accounts (``OnboardingAccountKeysResultV1`` as JSON)."""


@runtime_checkable
class GatewayActivitySource(Protocol):
    """Fetch user-visible activity from an external activity service."""

    @property
    def enabled(self) -> bool:
        """Return whether activity polling has a usable endpoint and credential."""

    async def fetch_recent_activity(self) -> list[dict[str, Any]]:
        """Return recent activity records for the configured user."""

    async def close(self) -> None:
        """Release any source-owned HTTP resources."""
