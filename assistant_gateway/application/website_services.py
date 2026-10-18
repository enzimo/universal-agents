"""Website-only identity, session, and operational application services."""

from __future__ import annotations

import hmac
import logging
from collections.abc import Callable
from dataclasses import dataclass
from typing import Literal, Protocol, cast

from assistant_gateway.application.objective_control_text import objective_control_text
from assistant_gateway.application.gateway_application import (
    GatewayApplicationResponse,
    GatewayDeliveryEvent,
    GatewaySession,
)
from assistant_gateway.application.command_operations import (
    GatewayPinnedMemoryConflictError,
    GatewayPinnedMemoryOperationResult,
)
from assistant_gateway.application.route_context import GatewayRouteContextV1
from assistant_gateway.application.identity_service import GatewayIdentityService
from assistant_gateway.application.operational_control import (
    GatewayContextDebugControlRequest,
    GatewayOperationalControlApplication,
    GatewayStopControlRequest,
)
from assistant_gateway.application.ports import (
    GatewayEventOutput,
    GatewayPrivateResourceAuthorizer,
)
from assistant_gateway.application.private_resources import (
    PRIVATE_ACTION_READ,
    PRIVATE_ACTION_UPDATE,
    RESOURCE_GATEWAY_ATTACHMENT,
    RESOURCE_GATEWAY_SESSION,
)
from assistant_gateway.application.request_context import (
    GatewayRequestContextService,
    GatewayRequestRoute,
)
from assistant_gateway.application.schedule_management import GatewayScheduleApplication
from assistant_gateway.application.runtime_application import (
    gateway_response_view,
    gateway_session_view,
)
from assistant_gateway.application.session_service import (
    GatewaySessionRoute,
    GatewaySessionService,
)
from assistant_gateway.application.transient_presentation import PendingCredentialAction
from assistant_gateway.application.transcript_service import GatewayTranscriptService
from assistant_gateway.application.website_application import (
    ActivatedWebEnrollmentTokenV1,
    WEB_CHAT_CHAT_ID,
    WebChatThreadStatesV1,
    web_chat_threads_revision,
    GatewayAttachmentContent,
    GatewayGeneratedSiteSession,
    GatewaySessionAliasConflict,
    GatewayWorkspaceFileContent,
    GatewayWebChatIdentity,
    IssuedWebEnrollmentTokenV1,
    WebHostedMCPServerListV1,
    WebHostedMCPServerListRequestV1,
    WebMCPConnectionDecisionRequestV1,
    WebMCPConnectionDecisionResultV1,
    WebMCPConnectionListRequestV1,
    WebMCPConnectionListV1,
    WebMCPLifecycleRequestV1,
    WebMCPLifecycleResultV1,
    WebMemoryListRequestV1,
    WebMemoryCreateRequestV1,
    WebMemoryListV1,
    WebMemoryConflictError,
    WebMemoryMutationRequestV1,
    WebMemoryMutationResultV1,
    WebMemoryV1,
    WebObjectiveControlRequestV1,
    WebObjectiveControlResultV1,
    WebObjectiveListV1,
    WebObjectiveV1,
    WebScheduleControlRequestV1,
    WebScheduleControlResultV1,
    WebScheduleListV1,
    WebScheduleV1,
    WebScheduleWorkerAssignmentV1,
    WebAppearanceV1,
    WebTeamMemberListV1,
    WebTranscriptAttachmentV1,
    WebTranscriptEventPageV1,
    WebTranscriptHistoryPageV1,
    WebTranscriptMessageV1,
    WebWorkspaceDirectoryV1,
    WebWorkspaceFileV1,
)
from assistant_gateway.application.workspace_browser import (
    GatewayWorkspaceAccess,
    WebsiteWorkspaceBrowser,
)
from assistant_gateway.application.web_chat_threads import WebChatThread, web_chat_thread
from assistant_gateway.domain.sessions import SessionListRecord, SessionRecord, WebChatThreadRecord
from assistant_gateway.domain.transcript import (
    TRANSCRIPT_ROLE_USER,
    TRANSCRIPT_SOURCE_ACCESS_REVIEW,
    TRANSCRIPT_SOURCE_CLIENT_REQUEST,
    GatewayTranscriptMessage,
)
from assistant_gateway.ports.repositories import DeliveryRepository, SessionRepository
from custom_messages.contracts.runtime_v1 import DELIVERY_CHANNEL_WEBSITE
from custom_messages.contracts.common_v1 import contract_sha256
from custom_messages.contracts.control_v1 import PINNED_MEMORY_SORT_NEWEST, ManagerRuntimeWorkCommand, PinnedMemoryCommandPayload, PinnedMemoryFactPayload
from custom_messages.enums import ScheduleManagementClass
from assistant_gateway.application.website_application import (
    WebTaskListRequestV1, WebTaskListV1, WebTaskControlRequestV1, WebTaskControlResultV1,
)
from assistant_gateway.application.website_tasks import list_tasks, control_tasks
from runtime_settings.appearance import DarkMetalAppearanceSettings, retired_dark_keys

LOGGER = logging.getLogger(__name__)


class GatewayBootstrapTokenSource(Protocol):
    """Read the current bootstrap browser credential at the infrastructure edge."""

    def read(self) -> str | None: ...


class WebsiteTeamPresence(Protocol):
    """Live team presence and defined agent classes (``assistant_gateway.team_presence``)."""

    def list_web_chat_team_members(self, *, presence_only: bool = False) -> WebTeamMemberListV1: ...


class WebsiteMCPControls(Protocol):
    """People's MCP connection and hosted-server controls (``assistant_gateway.mcp_controls``)."""

    def list_web_chat_mcp_connections(
        self,
        request: WebMCPConnectionListRequestV1,
    ) -> WebMCPConnectionListV1: ...

    def list_web_chat_hosted_mcp_servers(
        self,
        request: WebHostedMCPServerListRequestV1,
    ) -> WebHostedMCPServerListV1: ...

    def decide_web_chat_mcp_connection(
        self,
        request: WebMCPConnectionDecisionRequestV1,
    ) -> WebMCPConnectionDecisionResultV1: ...

    async def control_web_chat_mcp_lifecycle(
        self,
        request: WebMCPLifecycleRequestV1,
    ) -> WebMCPLifecycleResultV1: ...


class WebsitePinnedMemoryOperations(Protocol):
    """Manager-backed pinned-memory operations needed by Website Settings."""

    async def execute_pinned_memory_operation(
        self,
        *,
        command: PinnedMemoryCommandPayload,
        session_key: str,
        channel_name: str,
        user_id: str,
        chat_id: str,
        thread_id: str | None,
        canonical_user_id: str,
    ) -> GatewayPinnedMemoryOperationResult: ...


@dataclass(frozen=True)
class WebsiteIdentitySettings:
    """Bootstrap identity shown when browser authentication uses the host token."""

    bootstrap_canonical_user_id: str | None
    bootstrap_display_name: str | None


class WebsiteIdentityApplication:
    """Own browser authentication and enrollment over the identity service."""

    def __init__(
        self,
        *,
        identity: GatewayIdentityService,
        bootstrap_tokens: GatewayBootstrapTokenSource,
        settings: WebsiteIdentitySettings,
    ) -> None:
        self._identity = identity
        self._bootstrap_tokens = bootstrap_tokens
        self._settings = settings

    def authenticate_web_chat_token(
        self,
        raw_token: str,
    ) -> GatewayWebChatIdentity | None:
        resolved_user = self._identity.resolve_web_token(raw_token)
        if resolved_user is not None:
            return GatewayWebChatIdentity(
                auth_kind="user",
                canonical_user_id=resolved_user.user.canonical_user_id,
                display_name=resolved_user.user.display_name,
                credential_kind="user_token",
            )
        invited_user = self._identity.consume_web_invite(raw_token)
        if invited_user is not None:
            return GatewayWebChatIdentity(
                auth_kind="user",
                canonical_user_id=invited_user.user.canonical_user_id,
                display_name=invited_user.user.display_name,
                credential_kind="invite",
                enrollment_required=True,
                enrollment_label=(invited_user.web_invite_label or "Initial browser"),
            )
        expected = str(self._bootstrap_tokens.read() or "").strip()
        if not expected or not hmac.compare_digest(str(raw_token or ""), expected):
            return None
        return GatewayWebChatIdentity(
            auth_kind="bootstrap",
            canonical_user_id=self._settings.bootstrap_canonical_user_id,
            display_name=self._settings.bootstrap_display_name,
            credential_kind="bootstrap_token",
        )

    def issue_web_user_enrollment_token(
        self,
        *,
        canonical_user_id: str,
        label: str,
        pending_expires_at_utc: str,
        replacement_token_id: str | None = None,
    ) -> IssuedWebEnrollmentTokenV1 | None:
        issued = self._identity.issue_enrollment_token(
            canonical_user_id=canonical_user_id,
            label=label,
            pending_expires_at_utc=pending_expires_at_utc,
            replacement_token_id=replacement_token_id,
        )
        if issued is None:
            return None
        return IssuedWebEnrollmentTokenV1(
            raw_token=issued.raw_token,
            token_id=issued.record.token_id,
            label=issued.record.label,
        )

    def activate_web_user_enrollment_token(
        self,
        *,
        canonical_user_id: str,
        token_id: str,
    ) -> ActivatedWebEnrollmentTokenV1 | None:
        activated = self._identity.activate_enrollment_token(
            canonical_user_id=canonical_user_id,
            token_id=token_id,
        )
        if activated is None:
            return None
        return ActivatedWebEnrollmentTokenV1(
            token_id=activated.token_id,
            label=activated.label,
        )

    def get_web_chat_appearance(
        self,
        canonical_user_id: str,
    ) -> WebAppearanceV1 | None:
        appearance = self._identity.get_web_chat_appearance(canonical_user_id)
        if appearance is None:
            return None
        retired = retired_dark_keys(appearance.get("dark"))
        if retired:
            return self._drop_retired_dark_tuning(canonical_user_id, appearance, retired)
        return WebAppearanceV1.model_validate(appearance)

    def _drop_retired_dark_tuning(
        self,
        canonical_user_id: str,
        appearance: dict[str, object],
        retired: list[str],
    ) -> WebAppearanceV1:
        # Dark settings saved with settings that no longer exist were tuned
        # for a lighting that is gone. Replace only the dark part with the
        # defaults, keep the person's mode and light-theme choices, and save
        # the result so this happens once rather than on every page load.
        LOGGER.warning(
            "A saved Web Chat appearance held dark background settings that no longer "
            "exist; the dark part was reset to the defaults. "
            "event=web_chat_appearance_retired_dark_dropped canonical_user_id=%s settings=%s",
            canonical_user_id,
            ",".join(retired),
        )
        cleaned = dict(appearance)
        cleaned["dark"] = DarkMetalAppearanceSettings().model_dump(mode="json")
        return self.save_web_chat_appearance(
            canonical_user_id=canonical_user_id,
            appearance=WebAppearanceV1.model_validate(cleaned),
        )

    def save_web_chat_appearance(
        self,
        *,
        canonical_user_id: str,
        appearance: WebAppearanceV1,
    ) -> WebAppearanceV1:
        saved = self._identity.save_web_chat_appearance(
            canonical_user_id=canonical_user_id,
            appearance=appearance.model_dump(
                mode="json",
                exclude={"schema_version"},
            ),
        )
        return WebAppearanceV1.model_validate(saved)

    def reset_web_chat_appearance(self, canonical_user_id: str) -> bool:
        return self._identity.reset_web_chat_appearance(canonical_user_id)

    def revoke_pending_web_user_enrollment_token(
        self,
        *,
        canonical_user_id: str,
        token_id: str,
    ) -> bool:
        return self._identity.revoke_pending_enrollment_token(
            canonical_user_id=canonical_user_id,
            token_id=token_id,
        )

    def is_web_chat_user_active(self, canonical_user_id: str) -> bool:
        return self._identity.is_web_chat_user_active(canonical_user_id)


class WebsiteSessionApplication:
    """Own browser session visibility, mutation, and attachment access."""

    def __init__(
        self,
        *,
        sessions: GatewaySessionService,
        session_repository: SessionRepository,
        delivery_repository: DeliveryRepository,
        request_context: GatewayRequestContextService,
        private_authorizer: GatewayPrivateResourceAuthorizer,
        manager_agent_id: str | None,
        team_id: str,
        awaiting_reply_session_keys: Callable[[], frozenset[str]],
        proactive_session_key: str,
    ) -> None:
        self._sessions = sessions
        self._session_repository = session_repository
        self._delivery_repository = delivery_repository
        self._request_context = request_context
        self._private_authorizer = private_authorizer
        self._manager_agent_id = manager_agent_id
        self._team_id = team_id
        # Sessions with a request whose agent reply the gateway still awaits
        # (in memory, so every open page sees the same busy dot the sender does).
        self._awaiting_reply_session_keys = awaiting_reply_session_keys
        # The configured key of the team's proactive session (heartbeat.
        # session_key): its threads are listed as "Updates" (web_chat_threads).
        self._proactive_session_key = proactive_session_key

    def _owner_allows(
        self,
        session: SessionRecord | SessionListRecord | WebChatThreadRecord,
        owner_canonical_user_id: str | None,
        *,
        action: str = PRIVATE_ACTION_READ,
        resource_type: str = RESOURCE_GATEWAY_SESSION,
        resource_id: str | None = None,
    ) -> bool:
        return self._private_authorizer.can_access(
            actor_canonical_user_id=owner_canonical_user_id,
            owner_canonical_user_id=session.owner_canonical_user_id,
            resource_type=resource_type,
            resource_id=resource_id or session.session_key,
            action=action,
            team_id=self._team_id,
        )

    def list_web_chat_threads(
        self,
        *,
        owner_canonical_user_id: str | None,
        limit: int = 50,
    ) -> list[WebChatThread]:
        """The person's Web Chat threads as the sidebar lists them (web_chat_threads)."""

        threads: list[WebChatThread] = []
        for record in self._session_repository.list_web_chat_threads(
            owner_canonical_user_id=owner_canonical_user_id,
            channel_name=DELIVERY_CHANNEL_WEBSITE,
            chat_id=WEB_CHAT_CHAT_ID,
            limit=limit,
        ):
            if not self._owner_allows(record, owner_canonical_user_id):
                continue
            thread = web_chat_thread(record, proactive_session_key=self._proactive_session_key)
            if thread is not None:
                threads.append(thread)
        return threads

    def web_chat_thread_states(
        self,
        *,
        owner_canonical_user_id: str | None,
    ) -> WebChatThreadStatesV1:
        threads = self.list_web_chat_threads(
            owner_canonical_user_id=owner_canonical_user_id,
        )
        awaiting = self._awaiting_reply_session_keys()
        return WebChatThreadStatesV1(
            threads_revision=web_chat_threads_revision(threads),
            busy_session_keys=tuple(
                thread.session_key
                for thread in threads
                if thread.session_key in awaiting
            ),
        )

    def can_access_web_chat_session(
        self,
        *,
        session_key: str,
        owner_canonical_user_id: str | None,
        allow_missing: bool = False,
    ) -> bool:
        session = self._session_repository.resolve_session_reference(session_key)
        return (
            allow_missing
            if session is None
            else self._owner_allows(session, owner_canonical_user_id)
        )

    def archive_web_chat_session(
        self,
        *,
        session_key: str,
        owner_canonical_user_id: str | None,
    ) -> bool:
        session = self._session_repository.resolve_session_reference(session_key)
        if (
            session is None
            or session.channel_name != DELIVERY_CHANNEL_WEBSITE
            or session.chat_id != WEB_CHAT_CHAT_ID
            or not self._owner_allows(
                session,
                owner_canonical_user_id,
                action=PRIVATE_ACTION_UPDATE,
            )
        ):
            return False
        return bool(
            self._session_repository.set_session_status(session.session_key, "archived")
        )

    def rename_web_chat_session(
        self,
        *,
        session_key: str,
        session_alias: str,
        owner_canonical_user_id: str | None,
    ) -> GatewaySession | None:
        session = self._session_repository.resolve_session_reference(session_key)
        if (
            session is None
            or session.channel_name != DELIVERY_CHANNEL_WEBSITE
            or session.chat_id != WEB_CHAT_CHAT_ID
            or not self._owner_allows(
                session,
                owner_canonical_user_id,
                action=PRIVATE_ACTION_UPDATE,
            )
        ):
            return None
        existing_alias = self._session_repository.get_session_by_alias(session_alias)
        if (
            existing_alias is not None
            and existing_alias.session_key != session.session_key
        ):
            raise GatewaySessionAliasConflict(session_alias)
        self._session_repository.set_session_alias(session.session_key, session_alias)
        updated = self._session_repository.get_session(session.session_key)
        if updated is None:
            raise RuntimeError("Renamed Website session could not be reloaded.")
        return gateway_session_view(updated)

    def get_web_chat_attachment(
        self,
        *,
        attachment_id: str,
        owner_canonical_user_id: str | None,
        allow_missing_session: bool = False,
    ) -> GatewayAttachmentContent | None:
        record = self._delivery_repository.get_attachment(attachment_id)
        if record is None:
            return None
        session = self._session_repository.resolve_session_reference(record.session_key)
        if session is None:
            if not allow_missing_session:
                return None
        elif not self._owner_allows(
            session,
            owner_canonical_user_id,
            resource_type=RESOURCE_GATEWAY_ATTACHMENT,
            resource_id=record.attachment_id,
        ):
            return None
        return GatewayAttachmentContent(
            attachment_id=record.attachment_id,
            session_key=record.session_key,
            storage_reference=record.local_path,
            sha256=record.sha256,
            filename=record.filename,
            content_type=record.content_type,
            kind=record.kind,
        )

    def resolve_generated_site_session(
        self,
        *,
        site_id: str,
        client_id: str,
        requested_session_key: str,
        conversation_id: str | None,
        owner_canonical_user_id: str,
    ) -> GatewayGeneratedSiteSession:
        """The session a page's chat message goes to, for the page's signed-in owner.

        An existing session or conversation must belong to that person
        (``PermissionError`` otherwise) and a new session is created as
        theirs, so a page's chat never acts in someone else's thread.
        """

        if requested_session_key:
            return self._requested_site_session(
                site_id=site_id,
                client_id=client_id,
                requested_session_key=requested_session_key,
                conversation_id=conversation_id,
                owner=owner_canonical_user_id,
            )
        if conversation_id:
            mapped = self._session_repository.get_session_by_conversation_id(
                conversation_id
            )
            if mapped is not None:
                self._require_site_session_owner(mapped, owner_canonical_user_id)
                return GatewayGeneratedSiteSession(
                    session_key=mapped.session_key,
                    preserve_channel_binding=True,
                )
        session_key = f"{DELIVERY_CHANNEL_WEBSITE}:{site_id}:{client_id}"
        existing = self._session_repository.resolve_session_reference(session_key)
        if existing is not None:
            self._require_site_session_owner(existing, owner_canonical_user_id)
        return GatewayGeneratedSiteSession(
            session_key=session_key,
            preserve_channel_binding=False,
        )

    def _requested_site_session(
        self,
        *,
        site_id: str,
        client_id: str,
        requested_session_key: str,
        conversation_id: str | None,
        owner: str,
    ) -> GatewayGeneratedSiteSession:
        session = self._session_repository.resolve_session_reference(
            requested_session_key
        )
        created = session is None
        if session is None:
            session = self._sessions.resolve_session(
                requested_session_key=requested_session_key,
                default_session_key=requested_session_key,
                manager_agent_id=self._manager_agent_id,
                owner_canonical_user_id=owner,
                default_owner_canonical_user_id=owner,
                route=GatewaySessionRoute(
                    channel_name=DELIVERY_CHANNEL_WEBSITE,
                    user_id=client_id,
                    chat_id=site_id,
                    thread_id=site_id,
                ),
                enforce_owner_scope=True,
            )
        else:
            self._require_site_session_owner(session, owner)
        if (
            conversation_id
            and self._session_repository.get_session_conversation(conversation_id)
            is None
        ):
            self._session_repository.register_conversation_session(
                session_key=session.session_key,
                conversation_id=conversation_id,
                last_task_id=(
                    session.last_task_id
                    if session.conversation_id == conversation_id
                    else None
                ),
            )
        return GatewayGeneratedSiteSession(
            session_key=session.session_key,
            preserve_channel_binding=not created,
        )

    def _require_site_session_owner(
        self, session: SessionRecord, owner: str
    ) -> None:
        if not self._owner_allows(session, owner):
            raise PermissionError("Session not found.")

    def get_session_details(self, session_reference: str) -> GatewaySession | None:
        session = self._session_repository.resolve_session_reference(session_reference)
        return gateway_session_view(session) if session is not None else None

    def ensure_web_chat_session(
        self,
        *,
        session_key: str,
        canonical_user_id: str,
    ) -> GatewaySession:
        route = GatewayRequestRoute(
            channel_name=DELIVERY_CHANNEL_WEBSITE,
            user_id=canonical_user_id,
            chat_id=WEB_CHAT_CHAT_ID,
            canonical_user_id=canonical_user_id,
        )
        session = self._sessions.resolve_session(
            requested_session_key=session_key,
            default_session_key=session_key,
            manager_agent_id=self._manager_agent_id,
            owner_canonical_user_id=self._request_context.canonical_user_id(route),
            default_owner_canonical_user_id=canonical_user_id,
            route=GatewaySessionRoute(
                channel_name=DELIVERY_CHANNEL_WEBSITE,
                user_id=canonical_user_id,
                chat_id=WEB_CHAT_CHAT_ID,
            ),
            enforce_owner_scope=self._request_context.users_enabled,
        )
        return gateway_session_view(session)


class WebsiteTranscriptApplication:
    """Owner-authorized Web Chat transcript application facet."""

    def __init__(
        self,
        *,
        transcript: GatewayTranscriptService,
        credential_actions: Callable[[str], tuple[PendingCredentialAction, ...]],
        session_repository: SessionRepository,
        private_authorizer: GatewayPrivateResourceAuthorizer,
        team_id: str,
    ) -> None:
        self._transcript = transcript
        self._credential_actions = credential_actions
        self._sessions = session_repository
        self._private_authorizer = private_authorizer
        self._team_id = team_id

    def list_credential_actions(
        self, *, owner_canonical_user_id: str
    ) -> tuple[PendingCredentialAction, ...]:
        return tuple(
            item
            for item in self._credential_actions(owner_canonical_user_id)
            if self._visible_session(item.session_key, owner_canonical_user_id) is not None
        )[:20]

    def snapshot_cursor(self) -> str:
        return self._transcript.snapshot_cursor()

    def list_messages(
        self,
        *,
        owner_canonical_user_id: str,
        session_key: str,
        before_cursor: str | None,
        limit: int,
    ) -> WebTranscriptHistoryPageV1 | None:
        if self._visible_session(session_key, owner_canonical_user_id) is None:
            return None
        page = self._transcript.history_page(
            owner_canonical_user_id=owner_canonical_user_id,
            session_key=session_key,
            before_cursor=before_cursor,
            limit=limit,
        )
        return WebTranscriptHistoryPageV1(
            messages=tuple(self._message_view(message) for message in page.messages),
            before_cursor=page.before_cursor,
            has_more=page.has_more,
        )

    def list_events(
        self,
        *,
        owner_canonical_user_id: str,
        after_cursor: str | None,
        limit: int,
    ) -> WebTranscriptEventPageV1:
        page = self._transcript.event_page(
            owner_canonical_user_id=owner_canonical_user_id,
            after_cursor=after_cursor,
            limit=limit,
        )
        return WebTranscriptEventPageV1(
            messages=tuple(self._message_view(message) for message in page.messages),
            next_cursor=page.next_cursor,
            has_more=page.has_more,
        )

    def record_local_exchange(
        self,
        *,
        owner_canonical_user_id: str,
        session_key: str,
        conversation_id: str | None,
        client_request_id: str,
        user_text: str,
        response_text: str,
        response_message_type: str,
    ) -> tuple[WebTranscriptMessageV1, WebTranscriptMessageV1] | None:
        session = self._visible_session(session_key, owner_canonical_user_id)
        if session is None:
            return None
        user_message, reply = self._transcript.append_local_exchange(
            session=session,
            route=self._route(owner_canonical_user_id, client_request_id),
            conversation_id=conversation_id or session.conversation_id,
            text=user_text,
            response_text=response_text,
            response_message_type=response_message_type,
        )
        return self._message_view(user_message), self._message_view(reply)

    def record_local_user_message(
        self,
        *,
        owner_canonical_user_id: str,
        session_key: str,
        conversation_id: str | None,
        client_request_id: str,
        user_text: str,
    ) -> tuple[WebTranscriptMessageV1, bool] | None:
        session = self._visible_session(session_key, owner_canonical_user_id)
        if session is None:
            return None
        acceptance = self._transcript.accept_user_message_with_status(
            session=session,
            route=self._route(owner_canonical_user_id, client_request_id),
            conversation_id=conversation_id or session.conversation_id,
            text=user_text,
            attachments=(),
        )
        if acceptance is None:
            return None
        return self._message_view(acceptance.message), acceptance.created

    def record_local_response(
        self,
        *,
        owner_canonical_user_id: str,
        session_key: str,
        client_request_id: str,
        response: GatewayApplicationResponse,
    ) -> WebTranscriptMessageV1 | None:
        session = self._visible_session(session_key, owner_canonical_user_id)
        if session is None:
            return None
        route = self._route(owner_canonical_user_id, client_request_id)
        retry = self._transcript.find_retry_exchange(session=session, route=route)
        if retry is None:
            return None
        user_message, existing_reply = retry
        if existing_reply is not None:
            return self._message_view(existing_reply)
        reply = self._transcript.append_gateway_response(
            session=session,
            route=route,
            response=response,
            reply_to=user_message,
            source_kind="gateway_local_response",
        )
        return self._message_view(reply) if reply is not None else None

    def get_exchange(
        self,
        *,
        owner_canonical_user_id: str,
        session_key: str,
        client_request_id: str,
    ) -> tuple[WebTranscriptMessageV1, WebTranscriptMessageV1 | None] | None:
        session = self._visible_session(session_key, owner_canonical_user_id)
        if session is None:
            return None
        retry = self._transcript.find_retry_exchange(
            session=session,
            route=self._route(owner_canonical_user_id, client_request_id),
        )
        if retry is None:
            return None
        user_message, reply = retry
        return (
            self._message_view(user_message),
            self._message_view(reply) if reply is not None else None,
        )

    def _visible_session(
        self,
        session_key: str,
        owner_canonical_user_id: str,
    ) -> SessionRecord | None:
        session = self._sessions.resolve_session_reference(session_key)
        if (
            session is None
            or session.channel_name != DELIVERY_CHANNEL_WEBSITE
            or session.chat_id != WEB_CHAT_CHAT_ID
            or not self._private_authorizer.can_access(
                actor_canonical_user_id=owner_canonical_user_id,
                owner_canonical_user_id=session.owner_canonical_user_id,
                resource_type=RESOURCE_GATEWAY_SESSION,
                resource_id=session.session_key,
                action=PRIVATE_ACTION_READ,
                team_id=self._team_id,
            )
        ):
            return None
        return session

    @staticmethod
    def _route(
        owner_canonical_user_id: str,
        client_request_id: str,
    ) -> GatewayRequestRoute:
        return GatewayRequestRoute(
            channel_name=DELIVERY_CHANNEL_WEBSITE,
            user_id=owner_canonical_user_id,
            chat_id=WEB_CHAT_CHAT_ID,
            original_message_id=client_request_id,
            canonical_user_id=owner_canonical_user_id,
        )

    @staticmethod
    def _message_view(message: GatewayTranscriptMessage) -> WebTranscriptMessageV1:
        return WebTranscriptMessageV1(
            message_id=message.message_id,
            client_request_id=(
                message.source_id
                if message.role == TRANSCRIPT_ROLE_USER
                and message.source_kind == TRANSCRIPT_SOURCE_CLIENT_REQUEST
                else None
            ),
            access_request_id=(
                message.source_id if message.source_kind == TRANSCRIPT_SOURCE_ACCESS_REVIEW else None
            ),
            session_key=message.session_key,
            conversation_id=message.conversation_id,
            session_sequence=message.session_sequence,
            event_sequence=message.event_sequence,
            role=cast(
                Literal["user", "assistant", "system"],
                message.role,
            ),
            content=message.content,
            message_type=message.message_type,
            message_level=message.message_level,
            correlation_id=message.correlation_id,
            attachments=tuple(
                WebTranscriptAttachmentV1(
                    attachment_id=attachment.attachment_id,
                    filename=attachment.filename,
                    content_type=attachment.content_type,
                    kind=attachment.kind,
                    size_bytes=attachment.size_bytes,
                    caption=attachment.caption,
                    url=(f"/api/web-chat/attachments/{attachment.attachment_id}"),
                )
                for attachment in message.attachment_references
            ),
            delivery_status=message.delivery_status,
            created_at_utc=message.created_at_utc,
        )


class WebsiteOperationsApplication:
    """Compose Website team, MCP, delivery, and debug-control capabilities."""

    def __init__(
        self,
        *,
        team: WebsiteTeamPresence,
        mcp: WebsiteMCPControls,
        event_output: GatewayEventOutput,
        operational_controls: GatewayOperationalControlApplication,
        schedules: GatewayScheduleApplication,
        memory_operations: WebsitePinnedMemoryOperations,
        workspace_browser: WebsiteWorkspaceBrowser,
    ) -> None:
        self._team = team
        self._mcp = mcp
        self._event_output = event_output
        self._operational_controls = operational_controls
        self._schedules = schedules
        self._memory_operations = memory_operations
        self._workspace_browser = workspace_browser

    def list_web_chat_team_members(self, *, presence_only: bool = False) -> WebTeamMemberListV1:
        return self._team.list_web_chat_team_members(presence_only=presence_only)

    def list_web_chat_workspace_directory(
        self,
        *,
        canonical_user_id: str,
        roles: tuple[str, ...],
        path: str,
    ) -> WebWorkspaceDirectoryV1:
        return self._workspace_browser.list_directory(
            access=GatewayWorkspaceAccess(
                canonical_user_id=canonical_user_id,
                roles=roles,
            ),
            path=path,
        )

    def inspect_web_chat_workspace_file(
        self,
        *,
        canonical_user_id: str,
        roles: tuple[str, ...],
        path: str,
    ) -> WebWorkspaceFileV1:
        return self._workspace_browser.inspect_file(
            access=GatewayWorkspaceAccess(
                canonical_user_id=canonical_user_id,
                roles=roles,
            ),
            path=path,
        )

    def read_web_chat_workspace_content(
        self,
        *,
        canonical_user_id: str,
        roles: tuple[str, ...],
        path: str,
    ) -> GatewayWorkspaceFileContent:
        return self._workspace_browser.read_file_content(
            access=GatewayWorkspaceAccess(
                canonical_user_id=canonical_user_id,
                roles=roles,
            ),
            path=path,
        )

    async def list_web_chat_memories(
        self,
        request: WebMemoryListRequestV1,
    ) -> WebMemoryListV1:
        bank = self._memory_bank(request.scope)
        operation = await self._memory_operation(
            request.context,
            PinnedMemoryCommandPayload(
                bank=bank,
                action="list",
                sort=PINNED_MEMORY_SORT_NEWEST,
                range_start=request.offset + 1,
                range_end=request.offset + request.limit,
            ),
        )
        result = operation.result
        total = int(result.total or 0)
        return WebMemoryListV1(
            scope=request.scope,
            memories=tuple(
                self._memory_view(
                    scope=request.scope,
                    fact=fact,
                    available_actions=operation.available_actions,
                )
                for fact in result.facts
            ),
            offset=request.offset,
            limit=request.limit,
            total=total,
            has_more=request.offset + len(result.facts) < total,
            can_create=operation.can_create,
        )

    async def create_web_chat_memory(
        self, request: WebMemoryCreateRequestV1
    ) -> WebMemoryMutationResultV1:
        operation = await self._memory_operation(
            request.context,
            PinnedMemoryCommandPayload(
                bank=self._memory_bank(request.scope),
                action="create",
                fact_text=request.fact_text,
                tags=list(request.tags),
                invalid_after_utc=(
                    request.invalid_after_utc.isoformat()
                    if request.invalid_after_utc is not None
                    else "never"
                ),
            ),
        )
        fact = operation.result.fact
        if fact is None:
            raise RuntimeError("The manager did not return the created memory.")
        return WebMemoryMutationResultV1(
            scope=request.scope,
            memory_id=fact.memory_id,
            action="create",
            memory=self._memory_view(
                scope=request.scope,
                fact=fact,
                available_actions=operation.available_actions,
            ),
            event_id=operation.result.event_id,
        )

    async def mutate_web_chat_memory(
        self,
        request: WebMemoryMutationRequestV1,
    ) -> WebMemoryMutationResultV1:
        if request.action == "edit" and request.fact_text is None:
            raise ValueError("fact_text is required when editing a memory.")
        bank = self._memory_bank(request.scope)
        expiration = None
        if request.action == "edit":
            expiration = (
                request.invalid_after_utc.isoformat()
                if request.invalid_after_utc is not None
                else "never"
            )
        command = PinnedMemoryCommandPayload(
            bank=bank,
            action="replace" if request.action == "edit" else "delete",
            memory_id=request.memory_id,
            fact_text=request.fact_text,
            tags=list(request.tags) if request.tags is not None else None,
            invalid_after_utc=expiration,
            expected_record_digest=request.expected_record_digest,
        )
        operation = await self._memory_operation(request.context, command)
        result = operation.result
        memory = (
            self._memory_view(
                scope=request.scope,
                fact=result.fact,
                available_actions=operation.available_actions,
            )
            if result.fact is not None
            else None
        )
        return WebMemoryMutationResultV1(
            scope=request.scope,
            memory_id=request.memory_id,
            action=request.action,
            memory=memory,
            event_id=result.event_id,
            deleted=bool(result.deleted),
        )

    async def _memory_operation(
        self,
        context: GatewayRouteContextV1,
        command: PinnedMemoryCommandPayload,
    ) -> GatewayPinnedMemoryOperationResult:
        canonical_user_id = str(context.canonical_user_id or "").strip()
        if not canonical_user_id:
            raise PermissionError("Memory Settings require a registered user.")
        try:
            return await self._memory_operations.execute_pinned_memory_operation(
                command=command,
                session_key=context.session_key,
                channel_name=str(context.channel_name or DELIVERY_CHANNEL_WEBSITE),
                user_id=str(context.user_id or canonical_user_id),
                chat_id=str(context.chat_id or WEB_CHAT_CHAT_ID),
                thread_id=None,
                canonical_user_id=canonical_user_id,
            )
        except GatewayPinnedMemoryConflictError as exc:
            raise WebMemoryConflictError(str(exc)) from exc

    @staticmethod
    def _memory_bank(scope: str) -> Literal["coremem", "usermem"]:
        return "coremem" if scope == "team" else "usermem"

    @staticmethod
    def _memory_view(
        *,
        scope: Literal["personal", "team"],
        fact: PinnedMemoryFactPayload,
        available_actions: tuple[Literal["edit", "delete"], ...],
    ) -> WebMemoryV1:
        return WebMemoryV1(
            scope=scope,
            memory_id=fact.memory_id,
            fact_text=fact.fact_text,
            tags=tuple(fact.tags),
            valid_from_utc=fact.valid_from_utc,
            invalid_after_utc=fact.invalid_after_utc,
            created_at_utc=fact.created_at_utc,
            updated_at_utc=fact.updated_at_utc,
            record_digest=fact.record_digest,
            available_actions=available_actions,
        )

    def list_web_chat_mcp_connections(
        self,
        request: WebMCPConnectionListRequestV1,
    ) -> WebMCPConnectionListV1:
        return self._mcp.list_web_chat_mcp_connections(request)

    def list_web_chat_hosted_mcp_servers(
        self,
        request: WebHostedMCPServerListRequestV1,
    ) -> WebHostedMCPServerListV1:
        return self._mcp.list_web_chat_hosted_mcp_servers(request)

    def decide_web_chat_mcp_connection(
        self,
        request: WebMCPConnectionDecisionRequestV1,
    ) -> WebMCPConnectionDecisionResultV1:
        return self._mcp.decide_web_chat_mcp_connection(request)

    async def control_web_chat_mcp_lifecycle(
        self,
        request: WebMCPLifecycleRequestV1,
    ) -> WebMCPLifecycleResultV1:
        return await self._mcp.control_web_chat_mcp_lifecycle(request)

    @staticmethod
    def _route(
        context: GatewayRouteContextV1,
    ) -> GatewayRequestRoute:
        return GatewayRequestRoute(
            channel_name=context.channel_name,
            user_id=context.user_id,
            chat_id=context.chat_id,
            canonical_user_id=context.canonical_user_id,
        )

    async def _runtime_work_command(
        self,
        *,
        context: GatewayRouteContextV1,
        action: str,
        objective_id: str | None = None,
        expected_digest: str | None = None,
    ) -> ManagerRuntimeWorkCommand:
        response = await self._operational_controls.stop(
            GatewayStopControlRequest(
                session_key=context.session_key,
                action=action,
                scope="objective",
                target_objective_id=objective_id,
                expected_objective_digest=expected_digest,
                command_text=(
                    "/objectives"
                    if action == "list_objectives"
                    else f"/{action} objective {objective_id or ''}".strip()
                ),
                route=self._route(context),
                reason="Authenticated Web Runtime Controls request.",
            )
        )
        content = getattr(response.raw_envelope, "msg_content", None)
        if not isinstance(content, ManagerRuntimeWorkCommand):
            raise RuntimeError("Manager did not return Objective control state.")
        return content

    def _require_task_agent(self, agent_id: str) -> None:
        # Browser input selects only an observed member of this gateway's team.
        if not any(member.agent_id == agent_id for member in
                   self._team.list_web_chat_team_members(presence_only=True).members):
            raise ValueError("This agent is no longer available. Refresh Tasks.")

    async def list_web_chat_tasks(self, request: WebTaskListRequestV1) -> WebTaskListV1:
        self._require_task_agent(request.agent_id)
        return await list_tasks(self._operational_controls, request)

    async def control_web_chat_tasks(self, request: WebTaskControlRequestV1) -> WebTaskControlResultV1:
        self._require_task_agent(request.agent_id)
        return await control_tasks(self._operational_controls, request)

    async def list_web_chat_objectives(
        self,
        context: GatewayRouteContextV1,
    ) -> WebObjectiveListV1:
        response = await self._runtime_work_command(
            context=context,
            action="list_objectives",
        )
        if not response.success:
            raise RuntimeError(response.message or "Objective inventory failed.")
        return WebObjectiveListV1(
            objectives=tuple(
                WebObjectiveV1.model_validate(item.model_dump(mode="json"))
                for item in response.objectives
            )
        )

    async def control_web_chat_objective(
        self,
        request: WebObjectiveControlRequestV1,
    ) -> WebObjectiveControlResultV1:
        action = {
            "pause": "pause_objective",
            "resume": "resume_objective",
            "stop": "stop_objective",
            "abandon": "abandon_objective",
            "delete": "delete_objective",
        }[request.action]
        response = await self._runtime_work_command(
            context=request.context,
            action=action,
            objective_id=request.objective_id,
            expected_digest=request.expected_record_digest,
        )
        return WebObjectiveControlResultV1(
            objective_id=request.objective_id,
            action=request.action,
            status=(
                response.objective_status.value
                if response.objective_status is not None
                else "unknown"
            ),
            success=bool(response.success),
            message=objective_control_text(response, for_web=True) or response.message,
            cancellation_receipts=tuple(
                item.model_dump(mode="json") for item in response.cancellation_receipts
            ),
        )

    async def list_web_chat_schedules(
        self,
        context: GatewayRouteContextV1,
    ) -> WebScheduleListV1:
        response = await self._schedules.list_with_worker_assignments(
            session_key=context.session_key,
            route=self._route(context),
            owner_wide=True,
        )
        if not response.success:
            raise RuntimeError(response.message or "Schedule inventory failed.")
        # Schedules the runtime manages offer controls only to people who may
        # change them (administrators with authorization on); checked once.
        may_change_system = self._schedules.may_change_system_schedules(self._route(context))
        projected: list[WebScheduleV1] = []
        for schedule in response.schedules:
            actions: tuple[Literal["pause", "resume", "disable", "remove"], ...] = (
                ("resume", "remove")
                if not schedule.enabled or schedule.paused_until
                else ("pause", "disable", "remove")
            )
            if schedule.management_class == ScheduleManagementClass.SYSTEM and not may_change_system:
                actions = ()
            projected.append(
                WebScheduleV1(
                    schedule_id=schedule.schedule_id,
                    record_digest=contract_sha256(schedule.model_dump(mode="json")),
                    schedule_name=schedule.schedule_name,
                    objective_id=schedule.objective_id,
                    management_class=schedule.management_class.value,
                    system_schedule_kind=(
                        schedule.effective_system_schedule_kind.value
                        if schedule.effective_system_schedule_kind is not None
                        else None
                    ),
                    enabled=schedule.enabled,
                    paused_until=schedule.paused_until,
                    next_fire_at=schedule.next_fire_at,
                    worker_assignment=WebScheduleWorkerAssignmentV1.model_validate(
                        response.worker_assignments[schedule.schedule_id].model_dump(mode="json")
                    ),
                    available_actions=actions,
                )
            )
        return WebScheduleListV1(schedules=tuple(projected))

    async def control_web_chat_schedule(
        self,
        request: WebScheduleControlRequestV1,
    ) -> WebScheduleControlResultV1:
        paused_until = request.paused_until
        if request.action == "pause" and paused_until is None:
            raise ValueError("Schedule pause requires paused_until.")
        method = getattr(self._schedules, request.action)
        kwargs = {
            "session_key": request.context.session_key,
            "expected_record_digest": request.expected_record_digest,
        }
        if request.action == "pause":
            assert paused_until is not None
            kwargs["paused_until"] = paused_until.isoformat()
        success = await method(request.schedule_id, **kwargs)
        return WebScheduleControlResultV1(
            schedule_id=request.schedule_id,
            action=request.action,
            success=bool(success),
        )

    async def emit_delivery_event(self, event: GatewayDeliveryEvent) -> bool:
        await self._event_output.emit(event)
        return True

    async def send_context_debug_dump_control(
        self,
        *,
        enabled: bool,
        mode: str,
        session_key: str,
        conversation_id: str | None,
        channel_name: str,
        user_id: str,
        chat_id: str,
        thread_id: str | None,
        canonical_user_id: str | None,
    ) -> GatewayApplicationResponse:
        response = await self._operational_controls.set_context_debug_dump(
            GatewayContextDebugControlRequest(
                enabled=enabled,
                mode=mode,
                session_key=session_key,
                conversation_id=conversation_id,
                route=GatewayRequestRoute(
                    channel_name=channel_name,
                    user_id=user_id,
                    chat_id=chat_id,
                    thread_id=thread_id,
                    canonical_user_id=canonical_user_id,
                ),
            )
        )
        return gateway_response_view(response)
