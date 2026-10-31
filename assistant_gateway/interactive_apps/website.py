"""Compose website HTTP owners, routes, and server lifecycle."""

from __future__ import annotations
import logging
from runtime_settings.instance_layout import InstanceLayout
from pathlib import Path
from typing import Any
from aiohttp import ClientSession, ClientTimeout, web
from assistant_gateway.application.gateway_application import (
    GatewayDeliveryEvent,
)
from assistant_gateway.application.website_application import (
    WebsiteGatewayApplication,
)
from assistant_gateway.channel_bridge import ChannelBridge
from runtime_settings.interactive_apps import InternalWebsiteAppSettings
from runtime_hosting.aiohttp import AiohttpSite, start_aiohttp_site
from assistant_gateway.interactive_apps.base import ActivityContext, BaseInteractiveApp
from assistant_gateway.interactive_apps.host_ports import (
    InteractiveAppRuntimeContext,
    InternalWebsiteHostEnvironment,
)
from custom_messages.contracts.runtime_v1 import (
    DELIVERY_CHANNEL_WEBSITE,
)
from reply_routing_trace import is_reply_routing_trace_enabled
from user_docs.service import UserDocsService
from assistant_gateway.interactive_apps.website_support import (
    DOCKER_COMPOSE_SETUP_PATH,
    LOGGER,
    ONBOARDING_ASSET_ROOT,
    _CONFIG_EDITOR_ASSETS,
    WEB_CHAT_MODULE_ASSETS,
    _WEB_CHAT_CSP,
    _WEB_CHAT_ROUTE,
    _render_operator_ui_shell,
    resolve_internal_website_published_sites,
)
from assistant_gateway.interactive_apps.website_handlers.off_loop import check_signed_in_person
from assistant_gateway.interactive_apps.website_handlers import operations
from assistant_gateway.interactive_apps.website_handlers.access_inbox import WebsiteAccessInboxHandlers
from assistant_gateway.interactive_apps.website_handlers.access_map import WebsiteAccessMapHandlers
from assistant_gateway.interactive_apps.website_handlers.workflows import WebsiteWorkflowHandlers
from assistant_gateway.interactive_apps.website_handlers.access import (
    WebsiteAccessHandlers,
)
from assistant_gateway.interactive_apps.website_handlers.configuration import (
    WebsiteConfigurationHandlers,
)
from assistant_gateway.interactive_apps.website_handlers.controls import (
    WebsiteControlsHandlers,
)
from assistant_gateway.interactive_apps.website_handlers.conversation import (
    WebsiteConversationHandlers,
)
from assistant_gateway.interactive_apps.website_handlers.diagnostics import (
    WebsiteDiagnosticsHandlers,
)
from assistant_gateway.interactive_apps.website_handlers.status import WebsiteStatusHandlers
from assistant_gateway.interactive_apps.website_handlers.accounts import WebsiteAccountsHandlers
from assistant_gateway.interactive_apps.website_handlers.file_retention import WebsiteFileRetentionHandlers
from assistant_gateway.interactive_apps.website_handlers.knowledge import WebsiteKnowledgeHandlers
from assistant_gateway.interactive_apps.website_handlers.procedures import (
    WebsiteProcedureHandlers,
)
from assistant_gateway.interactive_apps.website_handlers.sites import (
    WebsiteSitesHandlers,
)
from assistant_gateway.interactive_apps.website_handlers.workspace import (
    WebsiteWorkspaceHandlers,
)


class InternalWebsiteInteractiveApp(BaseInteractiveApp):
    channel_name = DELIVERY_CHANNEL_WEBSITE

    def __init__(
        self,
        config: InternalWebsiteAppSettings,
        application: WebsiteGatewayApplication,
        bridge: ChannelBridge | None = None,
        runtime: InteractiveAppRuntimeContext | None = None,
        *,
        logger: logging.Logger | None = None,
    ) -> None:
        environment = runtime.website if runtime is not None else None
        if not isinstance(environment, InternalWebsiteHostEnvironment):
            raise TypeError(
                "Internal Website requires the explicit gateway-owned website host environment."
            )
        self.environment = environment
        self.runtime = runtime
        self.application = application
        self.bridge = bridge or ChannelBridge(application)
        self.settings = config
        # Page records in state/published_sites, page files in people's folders.
        self.published_sites = resolve_internal_website_published_sites(environment)
        self._logger = logger or LOGGER
        self._hosted_site: AiohttpSite | None = None
        self.access = WebsiteAccessHandlers(
            application=application,
            environment=environment,
            logger=logger,
            runtime=runtime,
        )
        self.configuration = WebsiteConfigurationHandlers(
            environment=environment, logger=logger
        )
        self.controls = WebsiteControlsHandlers(
            application=application, runtime=runtime
        )
        self.conversation = WebsiteConversationHandlers(
            application=application, config=config, logger=logger, runtime=runtime
        )
        self.diagnostics = WebsiteDiagnosticsHandlers(
            application=application, environment=environment, logger=logger
        )
        self.procedures = WebsiteProcedureHandlers(application=application)
        self.sites = WebsiteSitesHandlers(
            application=application,
            logger=logger,
            published_sites=self.published_sites,
        )
        self.workspace = WebsiteWorkspaceHandlers(
            application=application, logger=logger
        )
        self.access.operations = operations.WebsiteAccessHandlersOperations()
        self.configuration.operations = operations.WebsiteConfigurationHandlersOperations(
            _json_payload=self.access._json_payload,
            _require_bootstrap_web_chat_session=self.access._require_bootstrap_web_chat_session,
        )
        self.controls.operations = operations.WebsiteControlsHandlersOperations(
            _authorization_error_response=self.access._authorization_error_response,
            _json_payload=self.access._json_payload,
            _permission_api_context=self.access._permission_api_context,
            _permission_json_response=self.access._permission_json_response,
            _require_permission_browser_session=self.access._require_permission_browser_session,
            _require_permission_write_authority=self.access._require_permission_write_authority,
        )
        self.conversation.operations = operations.WebsiteConversationHandlersOperations(
            _json_payload=self.access._json_payload,
            _require_web_chat_session=self.access._require_web_chat_session,
            _required_web_chat_owner=self.access._required_web_chat_owner,
            _web_session_owner_scope=self.access._web_session_owner_scope,
        )
        self.diagnostics.operations = operations.WebsiteDiagnosticsHandlersOperations(
            _instance_config_path=self.configuration._instance_config_path,
            _json_payload=self.access._json_payload,
            _require_bootstrap_web_chat_session=self.access._require_bootstrap_web_chat_session,
            _web_session_owner_scope=self.access._web_session_owner_scope,
        )
        signed_in = operations.WebsiteSignedInHandlersOperations(
            _json_payload=self.access._json_payload,
            _permission_json_response=self.access._permission_json_response,
            _require_permission_browser_session=self.access._require_permission_browser_session,
            _require_permission_write_authority=self.access._require_permission_write_authority,
        )
        self.workflows = WebsiteWorkflowHandlers(application=application, operations=signed_in)
        self.access_inbox = WebsiteAccessInboxHandlers(application=application, operations=signed_in)
        self.access_map = WebsiteAccessMapHandlers(application=application, operations=signed_in)
        self.status = WebsiteStatusHandlers(application=application, operations=signed_in)
        self.accounts = WebsiteAccountsHandlers(application=application, operations=signed_in)
        self.file_retention = WebsiteFileRetentionHandlers(application=application, operations=signed_in)
        self.knowledge = WebsiteKnowledgeHandlers(
            application=application,
            operations=operations.WebsiteKnowledgeHandlersOperations(
                _json_payload=self.access._json_payload,
                _permission_api_context=self.access._permission_api_context,
                _permission_json_response=self.access._permission_json_response,
                _require_permission_browser_session=self.access._require_permission_browser_session,
                _require_permission_write_authority=self.access._require_permission_write_authority,
            ),
        )
        self.procedures.operations = operations.WebsiteProceduresHandlersOperations(
            _authorization_error_response=self.access._authorization_error_response,
            _json_payload=self.access._json_payload,
            _permission_api_context=self.access._permission_api_context,
            _permission_json_response=self.access._permission_json_response,
            _require_permission_browser_session=self.access._require_permission_browser_session,
            _require_permission_write_authority=self.access._require_permission_write_authority,
        )
        self.sites.operations = operations.WebsiteSitesHandlersOperations(
            _require_web_chat_session=self.access._require_web_chat_session,
            _web_session_owner_scope=self.access._web_session_owner_scope,
        )
        self.workspace.operations = operations.WebsiteWorkspaceHandlersOperations(
            _require_web_chat_session=self.access._require_web_chat_session
        )

    @property
    def enabled(self) -> bool:
        return bool(self.settings.enabled)

    @property
    def base_url(self) -> str:
        return self.environment.base_url

    def _web_chat_client_max_size(self) -> int:
        attachments = getattr(self.settings, "attachments", None)
        max_file_bytes = int(
            getattr(attachments, "max_file_bytes", 30 * 1024 * 1024) or 30 * 1024 * 1024
        )
        return max(max_file_bytes + (2 * 1024 * 1024), 1024 * 1024)

    async def start(self) -> None:
        self.published_sites.records_dir.mkdir(parents=True, exist_ok=True)
        app = self.build_app()
        self._hosted_site = await start_aiohttp_site(
            app,
            host=self.settings.bind_host,
            port=self.settings.bind_port,
            access_log=None,
            web_module=web,
        )
        self._logger.info(
            "Internal website interactive app listening on %s site_records_dir=%s",
            self.base_url,
            self.published_sites.records_dir,
        )

    async def stop(self) -> None:
        if self._hosted_site is not None:
            await self._hosted_site.stop()
            self._hosted_site = None

    async def deliver_event(self, event: GatewayDeliveryEvent) -> None:
        if (
            event.channel_name == self.channel_name
            and event.chat_id == "web-chat"
            and event.session_key
        ):
            if not event.message_id or event.event_sequence is None:
                self._logger.error(
                    "event=web_chat_async_delivery_missing_transcript_identity "
                    "session=%s conversation=%s correlation_id=%s",
                    event.session_key,
                    event.conversation_id,
                    event.correlation_id,
                )
                raise RuntimeError(
                    "Web Chat delivery reached the Website adapter before transcript "
                    "persistence."
                )
            self._logger.info(
                "event=web_chat_async_delivery_persisted session=%s conversation=%s "
                "correlation_id=%s message_id=%s event_sequence=%s",
                event.session_key,
                event.conversation_id,
                event.correlation_id,
                event.message_id,
                event.event_sequence,
            )
            return
        if is_reply_routing_trace_enabled(self.environment.agent_debug):
            self._logger.warning(
                "event=web_chat_async_delivery_observed session=%s conversation=%s correlation_id=%s "
                "chat_id=%s channel=%s; browser chat responses are delivered synchronously "
                "through /api/sites/{site_id}/chat or /api/web-chat/chat, so this async event "
                "will not update an already-returned browser request.",
                event.session_key,
                event.conversation_id,
                event.correlation_id,
                event.chat_id,
                event.channel_name,
            )
            return
        self._logger.info(
            "Website app received outbound gateway event for session=%s conversation=%s; "
            "browser chat responses are delivered synchronously through /api/sites/{site_id}/chat.",
            event.session_key,
            event.conversation_id,
        )

    async def emit_text_activity_update(
        self, context: ActivityContext, text: str
    ) -> None:
        self._logger.debug(
            "Website app activity update session=%s conversation=%s text=%s",
            context.session_key,
            context.conversation_id,
            text,
        )

    def build_app(self) -> web.Application:
        extra_routes: list[tuple[str, str, Any]] = [
            ("GET", "/docker-compose-setup.html", self._handle_docker_compose_setup),
            # Browsers ask every site for /favicon.ico for its tab icon, even
            # pages that name no icon.
            ("GET", "/favicon.ico", self._handle_web_chat_asset),
        ]
        if bool(getattr(self.settings, "web_chat_enabled", True)):
            extra_routes.extend([
            ("GET", "/workflows/{workflow_id}", self.workflows.page),
            ("GET", "/workflows", self.workflows.page),
            ("GET", "/api/web-chat/workflows", self.workflows.handle),
            ("GET", "/api/web-chat/workflows/{workflow_id}", self.workflows.handle),
            ("GET", "/api/web-chat/workflows/{workflow_id}/{action:diagnostics}", self.workflows.handle),
            (
                "POST",
                "/api/web-chat/workflows/{workflow_id}/{action:review|decisions|setup_done}",
                self.workflows.handle,
            ),
            ])
            extra_routes.extend(
                ("GET", f"/{name}", self._handle_web_chat_asset)
                for name in WEB_CHAT_MODULE_ASSETS
            )
            extra_routes.extend(
                [
                    ("GET", "/agent-mark.png", self._handle_web_chat_asset),
                    ("GET", "/styles.css", self._handle_web_chat_asset),
                    ("GET", "/appearance.js", self._handle_web_chat_asset),
                    (
                        "GET",
                        "/appearance_customizer.js",
                        self._handle_web_chat_asset,
                    ),
                    ("GET", "/metallic_mesh.js", self._handle_web_chat_asset),
                    ("GET", "/mesh_motion.js", self._handle_web_chat_asset),
                    ("GET", "/glass_mesh.js", self._handle_web_chat_asset),
                    ("GET", "/ui_motion.js", self._handle_web_chat_asset),
                    ("GET", "/intro_word.js", self._handle_web_chat_asset),
                    ("GET", "/panel_motion.js", self._handle_web_chat_asset),
                    ("GET", "/chat.js", self._handle_web_chat_asset),
                    ("GET", "/chat_delivery.js", self._handle_web_chat_asset),
                    ("GET", "/workspace_browser.js", self._handle_web_chat_asset),
                    ("GET", "/chat_markdown.js", self._handle_web_chat_asset),
                    ("GET", "/operational_ids.js", self._handle_web_chat_asset),
                    ("GET", "/credential_controls.js", self._handle_web_chat_asset),
                    ("GET", "/action_feedback.js", self._handle_web_chat_asset),
                    ("GET", "/task_controls.js", self._handle_web_chat_asset),
                    ("GET", "/pagination.js", self._handle_web_chat_asset),
                    ("GET", "/config-editor.js", self._handle_web_chat_asset),
                    ("GET", "/api/agents/status", self._handle_agent_status),
                    (
                        "GET",
                        "/api/web-chat/settings",
                        self.diagnostics._handle_web_chat_settings,
                    ),
                    (
                        "GET",
                        "/api/web-chat/appearance",
                        self.controls._handle_web_chat_appearance,
                    ),
                    (
                        "PUT",
                        "/api/web-chat/appearance",
                        self.controls._handle_web_chat_appearance,
                    ),
                    (
                        "DELETE",
                        "/api/web-chat/appearance",
                        self.controls._handle_web_chat_appearance,
                    ),
                    ("POST", "/api/web-chat/auth", self.access._handle_web_chat_auth),
                    (
                        "GET",
                        "/api/web-chat/session",
                        self.access._handle_web_chat_session,
                    ),
                    ("GET", "/api/web-chat/team", self.controls._handle_web_chat_team),
                    (
                        "GET",
                        "/api/web-chat/memories",
                        self.controls._handle_web_chat_memories,
                    ),
                    (
                        "PATCH",
                        "/api/web-chat/memories/{scope}/{memory_id}",
                        self.controls._handle_web_chat_memory_mutation,
                    ),
                    (
                        "POST",
                        "/api/web-chat/memories/{scope}",
                        self.controls._handle_web_chat_memory_mutation,
                    ),
                    (
                        "DELETE",
                        "/api/web-chat/memories/{scope}/{memory_id}",
                        self.controls._handle_web_chat_memory_mutation,
                    ),
                    (
                        "GET",
                        "/api/web-chat/workspace",
                        self.workspace._handle_web_chat_workspace_directory,
                    ),
                    (
                        "GET",
                        "/api/web-chat/workspace/file",
                        self.workspace._handle_web_chat_workspace_file,
                    ),
                    (
                        "GET",
                        "/api/web-chat/workspace/content",
                        self.workspace._handle_web_chat_workspace_content,
                    ),
                    (
                        "GET",
                        "/api/web-chat/objectives",
                        self.controls._handle_web_chat_objectives,
                    ),
                    ("GET", "/api/web-chat/tasks", self.controls._handle_web_chat_tasks),
                    ("POST", "/api/web-chat/tasks/control", self.controls._handle_web_chat_tasks),
                    (
                        "POST",
                        "/api/web-chat/objectives/{objective_id}/control",
                        self.controls._handle_web_chat_objective_control,
                    ),
                    (
                        "GET",
                        "/api/web-chat/schedules",
                        self.controls._handle_web_chat_schedules,
                    ),
                    (
                        "POST",
                        "/api/web-chat/schedules/{schedule_id}/control",
                        self.controls._handle_web_chat_schedule_control,
                    ),
                    (
                        "POST",
                        "/api/web-chat/mcp/{resource_kind}/{resource_id}/control",
                        self.controls._handle_web_chat_mcp_lifecycle,
                    ),
                    (
                        "POST",
                        "/api/web-chat/mcp-connections/{connection_id}/decision",
                        self.controls._handle_web_chat_mcp_connection_decision,
                    ),
                    (
                        "POST",
                        "/api/web-chat/session-context",
                        self.access._handle_web_chat_session_context,
                    ),
                    ("GET", "/api/web-chat/procedures/proposals", self.procedures.list_proposals),
                    ("GET", "/api/web-chat/procedures/installed", self.procedures.list_installed),
                    (
                        "POST",
                        "/api/web-chat/procedures/proposals/{proposal_id}/install",
                        self.procedures.install,
                    ),
                    (
                        "POST",
                        "/api/web-chat/procedures/proposals/{proposal_id}/reject",
                        self.procedures.reject,
                    ),
                    (
                        "POST",
                        "/api/web-chat/enrollment-token",
                        self.access._handle_web_chat_enrollment_token,
                    ),
                    (
                        "POST",
                        "/api/web-chat/enrollment-complete",
                        self.access._handle_web_chat_enrollment_complete,
                    ),
                    (
                        "POST",
                        "/api/web-chat/logout",
                        self.access._handle_web_chat_logout,
                    ),
                    (
                        "GET",
                        "/api/config-editor/config",
                        self.configuration._handle_config_editor_get_config,
                    ),
                    (
                        "PUT",
                        "/api/config-editor/config",
                        self.configuration._handle_config_editor_save_config,
                    ),
                    (
                        "GET",
                        "/api/config-editor/download",
                        self.configuration._handle_config_editor_download_config,
                    ),
                    (
                        "POST",
                        "/api/config-editor/backups/{backup_name}/restore",
                        self.configuration._handle_config_editor_restore_backup,
                    ),
                    (
                        "POST",
                        "/api/config-editor/restart",
                        self.configuration._handle_config_editor_restart,
                    ),
                    (
                        "GET",
                        "/api/web-chat/sessions",
                        self.conversation._handle_web_chat_sessions,
                    ),
                    (
                        "POST",
                        "/api/web-chat/sessions",
                        self.conversation._handle_web_chat_new_session,
                    ),
                    (
                        "GET",
                        "/api/web-chat/events/stream",
                        self.conversation._handle_web_chat_event_stream,
                    ),
                    (
                        "GET",
                        "/api/web-chat/events",
                        self.conversation._handle_web_chat_events,
                    ),
                    (
                        "GET",
                        "/api/web-chat/sessions/{session_key}/messages",
                        self.conversation._handle_web_chat_messages,
                    ),
                    (
                        "GET",
                        "/api/web-chat/attachments/{attachment_id}",
                        self.conversation._handle_web_chat_attachment,
                    ),
                    (
                        "POST",
                        "/api/web-chat/chat",
                        self.conversation._handle_web_chat_message,
                    ),
                    ("GET", "/api/web-chat/access-inbox", self.access_inbox.list),
                    ("GET", "/api/web-chat/access-inbox/counts", self.access_inbox.counts),
                    (
                        "GET",
                        "/api/web-chat/access-inbox/requests/{request_id}",
                        self.access_inbox.review,
                    ),
                    (
                        "POST",
                        "/api/web-chat/access-inbox/requests/{request_id}/decision",
                        self.access_inbox.decide,
                    ),
                    (
                        "POST",
                        "/api/web-chat/access-inbox/notices/read",
                        self.access_inbox.mark_notices_read,
                    ),
                    (
                        "POST",
                        "/api/web-chat/access-inbox/grants/{grant_id}/revoke",
                        self.access_inbox.revoke_grant,
                    ),
                    ("GET", "/api/web-chat/status", self.status.report),
                    # The Knowledge page (website_handlers.knowledge).
                    ("GET", "/api/web-chat/knowledge/access", self.knowledge.access),
                    ("GET", "/api/web-chat/knowledge/pages", self.knowledge.list_pages),
                    ("GET", "/api/web-chat/knowledge/pages/{space}/{page}", self.knowledge.read_page),
                    ("GET", "/api/web-chat/knowledge/pages/{space}/{page}/history", self.knowledge.history),
                    ("POST", "/api/web-chat/knowledge/pages/{space}", self.knowledge.create_page),
                    (
                        "POST",
                        "/api/web-chat/knowledge/pages/{space}/{page}/{change:save|notes|archive|revert}",
                        self.knowledge.change_page,
                    ),
                    ("GET", "/api/web-chat/knowledge/counts", self.knowledge.counts),
                    ("GET", "/api/web-chat/knowledge/contributions", self.knowledge.list_contributions),
                    (
                        "GET",
                        "/api/web-chat/knowledge/contributions/{contribution_id}",
                        self.knowledge.read_contribution,
                    ),
                    (
                        "POST",
                        "/api/web-chat/knowledge/contributions/{contribution_id}/{decision:publish|withdraw}",
                        self.knowledge.decide_contribution,
                    ),
                    ("GET", "/api/web-chat/status/access", self.status.access),
                    # Settings > Accounts (website_handlers.accounts), read only.
                    ("GET", "/api/web-chat/accounts", self.accounts.view),
                    # Settings > Team > File Retention (website_handlers.file_retention).
                    ("GET", "/api/web-chat/file-retention", self.file_retention.view),
                    ("PUT", "/api/web-chat/file-retention", self.file_retention.save),
                    ("GET", "/api/web-chat/access-map", self.access_map.view),
                    (
                        "GET",
                        "/api/web-chat/access-map/tools/{tool_id}/operations",
                        self.access_map.tool_operations,
                    ),
                    ("POST", "/api/web-chat/access-map/blocks", self.access_map.block),
                    (
                        "POST",
                        "/api/web-chat/access-map/blocks/{block_id}/remove",
                        self.access_map.unblock,
                    ),
                    (
                        "POST",
                        "/api/web-chat/access-map/shares/{share_id}/stop",
                        self.access_map.stop_sharing,
                    ),
                    ("POST", "/api/web-chat/access-map/data-folders", self.access_map.add_data_folder),
                    (
                        "POST",
                        "/api/web-chat/access-map/data-folders/{name}/remove",
                        self.access_map.remove_data_folder,
                    ),
                    ("POST", "/api/web-chat/access-map/starter-profile", self.access_map.set_starter_profile),
                    ("POST", "/api/web-chat/access-map/history-cleanup", self.access_map.set_history_cleanup),
                    ("GET", "/api/web-chat/files/people", self.access_map.team_people),
                    ("POST", "/api/web-chat/files/share", self.access_map.share),
                    ("POST", "/api/web-chat/files/send", self.access_map.send),
                    (
                        "GET",
                        "/api/web-chat/debug/context-dump",
                        self.diagnostics._handle_web_chat_debug_context_dump_status,
                    ),
                    (
                        "PATCH",
                        "/api/web-chat/debug/context-dump",
                        self.diagnostics._handle_web_chat_debug_context_dump,
                    ),
                    (
                        "PATCH",
                        "/api/web-chat/sessions/{session_key}",
                        self.conversation._handle_web_chat_update_session,
                    ),
                    (
                        "POST",
                        "/api/web-chat/sessions/{session_key}/archive",
                        self.conversation._handle_web_chat_archive_session,
                    ),
                    ("GET", "/config-editor", self._handle_config_editor_shell),
                    ("GET", "/config-editor/", self._handle_config_editor_shell),
                    ("GET", _WEB_CHAT_ROUTE, self._handle_web_chat_shell),
                    ("GET", _WEB_CHAT_ROUTE + "/", self._handle_web_chat_shell),
                ]
            )
        managed_skill_root = (
            self.environment.instance_root_path / "runtime" / "skill-installation"
        )
        service = UserDocsService(
            docs_root=self.settings.docs_root,
            skills_dir=self._resolved_skills_dir(),
            managed_skills_dir=managed_skill_root / "installed",
            managed_skill_attestations_dir=managed_skill_root / "attestations",
            published_sites=self.published_sites,
            generated_site_chat_handler=self.sites._handle_site_chat,
            generated_site_viewer=self._signed_in_person,
            service_base_url=self.base_url,
            appearance=self.environment.appearance,
            extra_routes=extra_routes,
            client_max_size=self._web_chat_client_max_size(),
            logger=self._logger,
        )
        app = service.build_app()
        # Before every handler, so each /api/ request checks the person off the loop.
        app.middlewares.append(
            check_signed_in_person(self.access._session_person, self.application.identity.is_web_chat_user_active)
        )
        return app

    def _signed_in_person(self, request: web.Request) -> str | None:
        """The person signed in to Web Chat in this browser, or None.

        The generated-site index lists only this person's pages; without a
        session it shows a sign-in hint instead of a list.
        """

        try:
            session = self.access._require_web_chat_session(request)
        except web.HTTPException:
            return None
        return self.access._web_session_owner_scope(session)

    def _resolved_skills_dir(self) -> str:
        layout = InstanceLayout.at(self.environment.instance_root_path)
        raw_skills_dir = str(self.settings.skills_dir or "").strip()
        if not raw_skills_dir:
            return str(layout.skills_dir)
        skills_path = Path(raw_skills_dir).expanduser()
        if not skills_path.is_absolute():
            skills_path = layout.control_dir / skills_path
        return str(skills_path.resolve())

    async def _handle_web_chat_asset(self, request: web.Request) -> web.FileResponse:
        asset_name = request.path.rsplit("/", 1)[-1]
        if asset_name not in _CONFIG_EDITOR_ASSETS:
            raise web.HTTPNotFound(text="Asset not found.")
        asset_path = (ONBOARDING_ASSET_ROOT / asset_name).resolve()
        try:
            asset_path.relative_to(ONBOARDING_ASSET_ROOT)
        except ValueError as exc:
            raise web.HTTPNotFound(text="Asset not found.") from exc
        if not asset_path.is_file():
            raise web.HTTPNotFound(text="Asset not found.")
        # Operator UI assets must move in lockstep with their HTML shells. A cached
        # stylesheet paired with fresh markup can make security controls misleading
        # or unusable, so require the browser to fetch the complete UI bundle.
        return web.FileResponse(
            asset_path,
            headers={"Cache-Control": "no-store"},
        )

    async def _handle_agent_status(self, request: web.Request) -> web.Response:
        self.access._require_web_chat_session(request)
        base_url = self.environment.activity_collector_base_url
        token = self.environment.activity_collector_admin_token
        if not base_url or not token:
            return web.json_response(
                {
                    "status": "unavailable",
                    "agents": [],
                    "message": "Activity Collector status is unavailable because its base URL or admin token is not configured.",
                }
            )
        url = base_url.rstrip("/") + "/api/admin/agents"
        timeout = ClientTimeout(
            total=self.environment.activity_collector_request_timeout_seconds
        )
        try:
            async with ClientSession(timeout=timeout) as session:
                async with session.get(
                    url, headers={"Authorization": f"Bearer {token}"}
                ) as response:
                    if response.status >= 400:
                        text = await response.text()
                        return web.json_response(
                            {
                                "status": "unavailable",
                                "agents": [],
                                "message": text
                                or f"Activity Collector returned HTTP {response.status}.",
                            }
                        )
                    payload = await response.json()
        except Exception as exc:
            self._logger.warning(
                "Could not fetch Activity Collector agent status from %s: %s", url, exc
            )
            return web.json_response(
                {
                    "status": "unavailable",
                    "agents": [],
                    "message": "Activity Collector agent status could not be reached yet.",
                }
            )
        return web.json_response(
            {
                "status": "ok",
                "source": "activity_collector",
                "agents": list(payload.get("agents") or []),
            }
        )

    async def _handle_web_chat_shell(self, _request: web.Request) -> web.Response:
        return web.Response(
            text=_render_operator_ui_shell("chat.html"),
            content_type="text/html",
            headers={
                "Cache-Control": "no-store",
                "Content-Security-Policy": _WEB_CHAT_CSP,
                "Referrer-Policy": "no-referrer",
                "X-Content-Type-Options": "nosniff",
                "X-Frame-Options": "DENY",
            },
        )

    async def _handle_config_editor_shell(self, _request: web.Request) -> web.Response:
        return web.Response(
            text=_render_operator_ui_shell("config_editor.html"),
            content_type="text/html",
            headers={"Cache-Control": "no-store"},
        )

    async def _handle_docker_compose_setup(
        self, _request: web.Request
    ) -> web.FileResponse:
        if not DOCKER_COMPOSE_SETUP_PATH.is_file():
            raise web.HTTPNotFound(text="Docker Compose setup guide not found.")
        return web.FileResponse(DOCKER_COMPOSE_SETUP_PATH)
