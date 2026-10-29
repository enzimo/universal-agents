"""Test-only composition helpers for application-shaped adapter doubles."""

from __future__ import annotations

import inspect

from assistant_gateway.application.gateway_application import (
    GatewayApplicationResponse,
    GatewayCommandResult,
    GatewaySession,
)
from assistant_gateway.application.interactive_commands import GatewayCommandRequest
from assistant_gateway.application.request_context import (
    GatewayRequestRoute,
    GatewayRequestContextService,
    GatewayRequestContextSettings,
)
from assistant_gateway.application.session_service import GatewaySessionService
from assistant_gateway.application.transcript_service import GatewayTranscriptService
from assistant_gateway.application.website_services import (
    WebsiteIdentityApplication,
    WebsiteIdentitySettings,
    WebsiteSessionApplication,
    WebsiteTranscriptApplication,
)
from assistant_gateway.application.website_application import (
    WebMemoryListV1,
    WebMemoryMutationResultV1,
    WebObjectiveListV1,
    WebScheduleListV1,
)
from assistant_gateway.application.workspace_browser import (
    GatewayWorkspaceAccess,
    WebsiteWorkspaceBrowser,
)
from assistant_gateway.runtime_ports import UuidGatewayIdGenerator
from assistant_gateway.attachments import gateway_staged_attachment_reader
from assistant_gateway.interactive_apps.host_ports import (
    InteractiveAppRuntimeContext,
    LocalInteractiveAppAttachmentStore,
)
from assistant_gateway.inbound_attachments import (
    InboundAttachmentStoragePolicy,
    resolve_inbound_attachment_cache_dir,
)
from assistant_gateway.voice import build_voice_client
from assistant_gateway.website_auth import EnvironmentGatewayBootstrapTokenSource
from custom_messages.contracts.attachments_v1 import UserTaskAttachment


class RecordingLinkResults:
    """Records the pages /link asks the gateway to watch (GatewayLinkResultWatches)."""

    def __init__(self) -> None:
        self.account_links: list[dict] = []
        self.sign_ins: list[dict] = []

    def watch_account_link(self, **request) -> None:
        self.account_links.append(request)

    def watch_sign_in(self, **request) -> None:
        self.sign_ins.append(request)


def interactive_app_runtime_from(config, application, *, voice_client=None):
    """Build explicit adapter host ports while legacy-shaped tests use aggregate fixtures."""

    attachment_settings = config.interactive_apps.internal_website.attachments
    return InteractiveAppRuntimeContext(
        request_timeout_seconds=config.manager.request_timeout_seconds,
        voice=config.voice,
        authorization_presentation_mode=config.authorization.presentation.mode,
        voice_client=(
            build_voice_client(config.voice) if voice_client is None else voice_client
        ),
        attachments=LocalInteractiveAppAttachmentStore(
            InboundAttachmentStoragePolicy(
                cache_dir=resolve_inbound_attachment_cache_dir(config),
                max_file_bytes=attachment_settings.max_file_bytes,
                allowed_kinds=frozenset(attachment_settings.allowed_kinds),
            ),
            application,
        ),
        outgoing_attachments=gateway_staged_attachment_reader(config),
    )


class _ModelDumpResult:
    """Minimal response projection for legacy-shaped test fixtures."""

    def __init__(self, payload) -> None:
        self._payload = payload
        self.has_more = payload.get("has_more", False)
        self.total = payload.get("total", 0)

    def model_dump(self, **_options):
        return self._payload


_LIST_RESULT_KEYS = {
    "list_web_chat_team_members": "members",
    "list_web_chat_mcp_connections": "connections",
    "list_web_chat_hosted_mcp_servers": "servers",
}


def _legacy_request_values(method_name: str, request) -> dict:
    values = request.model_dump(mode="python", exclude={"schema_version"})
    context = values.pop("context", None)
    if isinstance(context, dict):
        context.pop("schema_version", None)
        values = {**context, **values}
    return values


def _legacy_result(method_name: str, result):
    if hasattr(result, "model_dump"):
        return result
    list_key = _LIST_RESULT_KEYS.get(method_name)
    if list_key is not None:
        return _ModelDumpResult({list_key: result})
    return _ModelDumpResult(result)


class _LegacyTypedFacetDouble:
    """Translate typed application calls for focused legacy-shaped test fakes."""

    def __init__(self, candidate) -> None:
        self._candidate = candidate

    def __getattr__(self, method_name):
        method = getattr(self._candidate, method_name)
        if inspect.iscoroutinefunction(method):

            async def invoke(request):
                return _legacy_result(
                    method_name,
                    await method(**_legacy_request_values(method_name, request)),
                )

            return invoke

        def invoke(request=None):
            values = (
                _legacy_request_values(method_name, request)
                if request is not None
                else {}
            )
            return _legacy_result(method_name, method(**values))

        return invoke


class CoreApplicationDouble:
    def __init__(self, candidate, *, config=None, command_application=None) -> None:
        self._candidate = candidate
        self._config = config or getattr(candidate, "config", None)
        self._commands = command_application or getattr(
            candidate,
            "command_application",
            None,
        )

    def __getattr__(self, name):
        return getattr(self._candidate, name)

    @property
    def default_session_key(self) -> str:
        return str(self._config.user.default_session_key)

    def get_active_session(self, **route):
        repository = getattr(self._candidate, "session_repository")
        record = repository.get_active_session_for_channel(**route)
        if record is None:
            return None
        return GatewaySession(
            session_key=record.session_key,
            conversation_id=record.conversation_id,
            session_alias=getattr(record, "session_alias", None),
            last_task_id=getattr(record, "last_task_id", None),
            channel_name=getattr(record, "channel_name", None),
            user_id=getattr(record, "user_id", None),
            chat_id=getattr(record, "chat_id", None),
            thread_id=getattr(record, "thread_id", None),
            owner_canonical_user_id=getattr(
                record,
                "owner_canonical_user_id",
                None,
            ),
        )

    def is_registered_user(
        self,
        *,
        channel_name: str,
        external_user_id: str,
        chat_scope: str = "",
    ) -> bool:
        resolver = getattr(self._candidate, "resolve_canonical_user", None)
        if resolver is None:
            users_enabled = getattr(self._candidate, "gateway_users_enabled", None)
            return bool(users_enabled is not None and not users_enabled())
        return (
            resolver(
                channel_name=channel_name,
                external_user_id=external_user_id,
                chat_scope=chat_scope,
            )
            is not None
        )

    def register_inbound_attachment(
        self,
        attachment: UserTaskAttachment,
        *,
        session_key: str,
        conversation_id: str | None,
        correlation_id: str | None = None,
        expires_at: str | None = None,
        owner_canonical_user_id: str | None = None,
    ) -> None:
        local_path = attachment.local_path()
        if local_path is None:
            raise ValueError(
                "Inbound attachments must include a validated local storage reference."
            )
        repository = getattr(self._candidate, "delivery_repository")
        repository.upsert_attachment(
            attachment_id=attachment.attachment_id,
            session_key=session_key,
            conversation_id=conversation_id,
            correlation_id=correlation_id,
            filename=attachment.filename,
            content_type=attachment.content_type,
            kind=attachment.kind,
            size_bytes=attachment.size_bytes,
            sha256=attachment.sha256,
            local_path=local_path,
            expires_at=expires_at,
            owner_canonical_user_id=owner_canonical_user_id,
        )

    async def execute_interactive_command(self, **values) -> GatewayCommandResult:
        if self._commands is None:
            raise RuntimeError("Test command application was not composed.")
        if values.get("current_session_key") is None:
            active = self.get_active_session(
                channel_name=values["channel_name"],
                user_id=values["user_id"],
                chat_id=values["chat_id"],
                thread_id=values.get("thread_id"),
            )
            values["current_session_key"] = active.session_key if active else None
        query_handler = values.pop("query_handler", None)
        request = GatewayCommandRequest(**values, query_handler=query_handler)
        return await self._commands.execute(request)

    async def waiting_question_id(self, session_key: str) -> str | None:
        resolver = getattr(self._candidate, "waiting_question_id_for_session")
        return await resolver(session_key)

    async def send_user_message(
        self,
        text: str,
        *,
        session_key: str | None = None,
        conversation_id: str | None = None,
        channel_name: str | None = None,
        user_id: str | None = None,
        chat_id: str | None = None,
        thread_id: str | None = None,
        original_message_id: str | None = None,
        canonical_user_id: str | None = None,
        attachments: list[UserTaskAttachment] | None = None,
        query_handler=None,
        replied_message=None,
    ):
        transcript = getattr(self._candidate, "_transcript_service", None)
        accepted = None
        transcript_session = None
        transcript_route = GatewayRequestRoute(
            channel_name=channel_name,
            user_id=user_id,
            chat_id=chat_id,
            thread_id=thread_id,
            original_message_id=original_message_id,
            canonical_user_id=canonical_user_id,
        )
        if transcript is not None and session_key:
            transcript_session = self._candidate.session_repository.get_session(
                session_key
            )
            if transcript_session is not None:
                retry = transcript.find_retry_exchange(
                    session=transcript_session,
                    route=transcript_route,
                )
                if retry is not None and retry[1] is not None:
                    reply = retry[1]
                    return GatewayApplicationResponse(
                        text=reply.content,
                        conversation_id=reply.conversation_id,
                        correlation_id=reply.correlation_id,
                        message_type=reply.message_type,
                        message_level=reply.message_level,
                    )
                accepted = transcript.accept_user_message(
                    session=transcript_session,
                    route=transcript_route,
                    conversation_id=conversation_id,
                    text=text,
                    attachments=tuple(attachments or ()),
                )
        response = await self._candidate.send_user_message(
            text,
            session_key=session_key,
            conversation_id=conversation_id,
            channel_name=channel_name,
            user_id=user_id,
            chat_id=chat_id,
            thread_id=thread_id,
            original_message_id=original_message_id,
            canonical_user_id=canonical_user_id,
            attachments=attachments,
            query_handler=query_handler,
            replied_message=replied_message,
        )
        view = (
            response
            if isinstance(response, GatewayApplicationResponse)
            else GatewayApplicationResponse(
                text=response.text,
                conversation_id=getattr(response, "conversation_id", None),
                correlation_id=getattr(response, "correlation_id", None),
                task_id=getattr(response, "task_id", None),
                message_type=getattr(response, "message_type", None),
                message_level=getattr(response, "message_level", "normal"),
                attachments=tuple(getattr(response, "attachments", ()) or ()),
                transcript_text=getattr(response, "transcript_text", None),
                transient_action=getattr(response, "transient_action", None),
            )
        )
        if (
            transcript is not None
            and transcript_session is not None
            and accepted is not None
        ):
            transcript.append_gateway_response(
                session=transcript_session,
                route=transcript_route,
                response=view,
                reply_to=accepted,
            )
        return view


def application_from(candidate, **composition):
    """Return a real composed application or an already application-shaped fake."""

    application = getattr(candidate, "application", None)
    if application is not None:
        return application
    return CoreApplicationDouble(candidate, **composition)


class WebsiteApplicationDouble(CoreApplicationDouble):
    """Expose explicit Website facets over one test double.

    Production code never performs this flattening; older adapter fixtures use
    one compact object so individual HTTP behavior tests can stay focused.
    """

    def __init__(self, candidate, **composition) -> None:
        super().__init__(candidate, **composition)
        state_store = candidate.state_store
        if state_store.storage_settings.sqlite_path == ":memory:":
            state_store = type(state_store)(
                str(self._config.instance_root_path / "runtime" / "adapter-test.db")
            )
            candidate.state_store = state_store
            candidate.session_repository = state_store.session_repository
            candidate.request_repository = state_store.request_repository
            candidate.delivery_repository = state_store.delivery_repository
        website = self._config.interactive_apps.internal_website
        user = getattr(self._config, "user", None)
        self._web_identity = WebsiteIdentityApplication(
            identity=getattr(candidate, "identity_service", candidate),
            bootstrap_tokens=EnvironmentGatewayBootstrapTokenSource(
                website.web_chat_token_env_var
            ),
            settings=WebsiteIdentitySettings(
                bootstrap_canonical_user_id=getattr(
                    user,
                    "canonical_user_id",
                    None,
                ),
                bootstrap_display_name=getattr(user, "display_name", None),
            ),
        )
        request_context = GatewayRequestContextService(
            settings=GatewayRequestContextSettings(
                instance_name=self._config.instance_name,
                team_id=self._config.manager.team_id,
                default_canonical_user_id=self._config.user.canonical_user_id,
                default_display_name=self._config.user.display_name,
                default_timezone=self._config.user.timezone,
                heartbeat_source_id=None,
                users_enabled=bool(self._config.gateway.users.enabled),
            ),
            identity=getattr(candidate, "identity_service", candidate),
        )
        private_authorizer = getattr(candidate, "_private_authorizer")
        session_service = GatewaySessionService(
            repository=candidate.session_repository,
            id_generator=UuidGatewayIdGenerator(),
            authorizer=private_authorizer,
            team_id=self._config.manager.team_id,
        )
        delivery_repository = getattr(
            candidate,
            "delivery_repository",
            candidate.state_store.delivery_repository,
        )
        self._web_sessions = WebsiteSessionApplication(
            sessions=session_service,
            session_repository=candidate.session_repository,
            delivery_repository=delivery_repository,
            request_context=request_context,
            private_authorizer=private_authorizer,
            manager_agent_id=self._config.manager.manager_agent_id,
            team_id=self._config.manager.team_id,
            awaiting_reply_session_keys=lambda: frozenset(
                getattr(candidate, "sessions_awaiting_reply", ())
            ),
            proactive_session_key=self._config.heartbeat.session_key,
        )
        transcript_service = GatewayTranscriptService(
            repository=state_store.transcript_repository,
            unit_of_work_factory=state_store,
            session_repository=candidate.session_repository,
            id_generator=UuidGatewayIdGenerator(),
        )
        self._web_transcripts = WebsiteTranscriptApplication(
            credential_actions=lambda owner: (),
            transcript=transcript_service,
            session_repository=candidate.session_repository,
            private_authorizer=private_authorizer,
            team_id=self._config.manager.team_id,
        )
        self._workspace_browser = WebsiteWorkspaceBrowser(
            workspace_root=self._config.workspace_path,
            authorizer=private_authorizer,
            team_id=self._config.manager.team_id,
        )
        candidate._transcript_service = transcript_service
        self._typed_facets = _LegacyTypedFacetDouble(candidate)

    @property
    def identity(self):
        return self._web_identity

    @property
    def sessions(self):
        return self._web_sessions

    @property
    def operations(self):
        return self

    def start_new_session(
        self,
        *,
        channel_name=None,
        user_id=None,
        chat_id=None,
        thread_id=None,
        canonical_user_id=None,
        parallel_thread=False,
    ) -> GatewaySession:
        session = self._candidate.session_repository.create_session(
            session_key=UuidGatewayIdGenerator().new_session_key(),
            conversation_id=UuidGatewayIdGenerator().new_conversation_id(),
            manager_agent_id=self._config.manager.manager_agent_id,
            channel_name=channel_name,
            user_id=user_id,
            chat_id=chat_id,
            thread_id=thread_id,
            owner_canonical_user_id=canonical_user_id,
            parallel_thread=parallel_thread,
        )
        return GatewaySession(
            session_key=session.session_key,
            conversation_id=session.conversation_id,
            session_alias=session.session_alias,
            last_task_id=session.last_task_id,
            channel_name=session.channel_name,
            user_id=session.user_id,
            chat_id=session.chat_id,
            thread_id=session.thread_id,
            owner_canonical_user_id=session.owner_canonical_user_id,
            updated_at=session.updated_at,
            parallel_thread=session.parallel_thread,
        )

    async def emit_delivery_event(self, event) -> bool:
        self._candidate._transcript_service.append_delivery_event(event)
        await self._candidate.emit_delivery_event(event)
        return True

    @property
    def transcripts(self):
        return self._web_transcripts

    def list_web_chat_team_members(self, *, presence_only=False):
        return _legacy_result("list_web_chat_team_members",
                              self._candidate.list_web_chat_team_members(presence_only=presence_only))

    def list_web_chat_workspace_directory(
        self,
        *,
        canonical_user_id,
        roles,
        path,
    ):
        return self._workspace_browser.list_directory(
            access=GatewayWorkspaceAccess(canonical_user_id, roles),
            path=path,
        )

    def inspect_web_chat_workspace_file(
        self,
        *,
        canonical_user_id,
        roles,
        path,
    ):
        return self._workspace_browser.inspect_file(
            access=GatewayWorkspaceAccess(canonical_user_id, roles),
            path=path,
        )

    def read_web_chat_workspace_content(
        self,
        *,
        canonical_user_id,
        roles,
        path,
    ):
        return self._workspace_browser.read_file_content(
            access=GatewayWorkspaceAccess(canonical_user_id, roles),
            path=path,
        )

    async def list_web_chat_memories(self, request):
        result = await self._candidate.list_web_chat_memories(
            **_legacy_request_values("list_web_chat_memories", request)
        )
        return WebMemoryListV1.model_validate(result)

    async def mutate_web_chat_memory(self, request):
        result = await self._candidate.mutate_web_chat_memory(
            **_legacy_request_values("mutate_web_chat_memory", request)
        )
        return WebMemoryMutationResultV1.model_validate(result)

    async def create_web_chat_memory(self, request):
        result = await self._candidate.create_web_chat_memory(
            **_legacy_request_values("create_web_chat_memory", request)
        )
        return WebMemoryMutationResultV1.model_validate(result)

    def list_web_chat_mcp_connections(self, request):
        return self._typed_facets.list_web_chat_mcp_connections(request)

    def list_web_chat_hosted_mcp_servers(self, request):
        return self._typed_facets.list_web_chat_hosted_mcp_servers(request)

    def decide_web_chat_mcp_connection(self, request):
        return self._typed_facets.decide_web_chat_mcp_connection(request)

    async def list_web_chat_objectives(self, context):
        result = await self._candidate.list_web_chat_objectives(
            **context.model_dump(mode="python", exclude={"schema_version"})
        )
        return WebObjectiveListV1.model_validate({"objectives": result})

    async def list_web_chat_tasks(self, request):
        return await self._typed_facets.list_web_chat_tasks(request)

    async def control_web_chat_tasks(self, request):
        return await self._typed_facets.control_web_chat_tasks(request)

    async def list_web_chat_schedules(self, context):
        result = await self._candidate.list_web_chat_schedules(
            **context.model_dump(mode="python", exclude={"schema_version"})
        )
        return WebScheduleListV1.model_validate({"schedules": result})

    async def control_web_chat_objective(self, request):
        return await self._typed_facets.control_web_chat_objective(request)

    async def control_web_chat_schedule(self, request):
        return await self._typed_facets.control_web_chat_schedule(request)

    async def control_web_chat_mcp_lifecycle(self, request):
        return await self._typed_facets.control_web_chat_mcp_lifecycle(request)


def website_application_from(candidate, **composition):
    application = getattr(candidate, "application", None)
    if application is not None:
        return application
    return WebsiteApplicationDouble(candidate, **composition)
