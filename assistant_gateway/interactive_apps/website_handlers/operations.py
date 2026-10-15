from __future__ import annotations
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from aiohttp import web
from assistant_gateway.application.route_context import GatewayRouteContextV1
from typing import Protocol
from assistant_gateway.interactive_apps.website_support import WebChatSession


class AuthorizationErrorResponseOperation(Protocol):
    def __call__(self, error: Exception, *, operation: str) -> web.Response: ...


class InstanceConfigPathOperation(Protocol):
    def __call__(self) -> Path: ...


class JsonPayloadOperation(Protocol):
    async def __call__(self, request: web.Request) -> dict[str, Any]: ...


class PermissionApiContextOperation(Protocol):
    """The signed-in route for a Settings request on the browser's thread."""

    def __call__(
        self, request: web.Request, web_session: WebChatSession, *, session_key: str
    ) -> GatewayRouteContextV1: ...


class PermissionJsonResponseOperation(Protocol):
    def __call__(
        self, payload: dict[str, Any], *, status: int = 200
    ) -> web.Response: ...


class RequireBootstrapWebChatSessionOperation(Protocol):
    def __call__(self, request: web.Request) -> WebChatSession: ...


class RequirePermissionBrowserSessionOperation(Protocol):
    def __call__(self, request: web.Request) -> WebChatSession: ...


class RequirePermissionWriteAuthorityOperation(Protocol):
    def __call__(self, request: web.Request) -> WebChatSession: ...


class RequireWebChatSessionOperation(Protocol):
    def __call__(
        self, request: web.Request, *, allow_enrollment_pending: bool = False
    ) -> WebChatSession: ...


class RequiredWebChatOwnerOperation(Protocol):
    def __call__(self, web_session: WebChatSession) -> str: ...


class WebSessionOwnerScopeOperation(Protocol):
    def __call__(self, web_session: WebChatSession) -> str | None: ...


@dataclass(frozen=True, slots=True)
class WebsiteAccessHandlersOperations:
    pass


@dataclass(frozen=True, slots=True)
class WebsiteConfigurationHandlersOperations:
    _json_payload: JsonPayloadOperation
    _require_bootstrap_web_chat_session: RequireBootstrapWebChatSessionOperation


@dataclass(frozen=True, slots=True)
class WebsiteControlsHandlersOperations:
    _authorization_error_response: AuthorizationErrorResponseOperation
    _json_payload: JsonPayloadOperation
    _permission_api_context: PermissionApiContextOperation
    _permission_json_response: PermissionJsonResponseOperation
    _require_permission_browser_session: RequirePermissionBrowserSessionOperation
    _require_permission_write_authority: RequirePermissionWriteAuthorityOperation


@dataclass(frozen=True, slots=True)
class WebsiteConversationHandlersOperations:
    _json_payload: JsonPayloadOperation
    _require_web_chat_session: RequireWebChatSessionOperation
    _required_web_chat_owner: RequiredWebChatOwnerOperation
    _web_session_owner_scope: WebSessionOwnerScopeOperation


@dataclass(frozen=True, slots=True)
class WebsiteDiagnosticsHandlersOperations:
    _instance_config_path: InstanceConfigPathOperation
    _json_payload: JsonPayloadOperation
    _require_bootstrap_web_chat_session: RequireBootstrapWebChatSessionOperation
    _web_session_owner_scope: WebSessionOwnerScopeOperation


@dataclass(frozen=True, slots=True)
class WebsiteSignedInHandlersOperations:
    """Browser checks shared by the access Inbox, Access page and workflow pages."""

    _json_payload: JsonPayloadOperation
    _permission_json_response: PermissionJsonResponseOperation
    _require_permission_browser_session: RequirePermissionBrowserSessionOperation
    _require_permission_write_authority: RequirePermissionWriteAuthorityOperation


@dataclass(frozen=True, slots=True)
class WebsiteKnowledgeHandlersOperations:
    """The Knowledge page: signed-in reads, checked writes, and the browser's own thread."""

    _json_payload: JsonPayloadOperation
    _permission_api_context: PermissionApiContextOperation
    _permission_json_response: PermissionJsonResponseOperation
    _require_permission_browser_session: RequirePermissionBrowserSessionOperation
    _require_permission_write_authority: RequirePermissionWriteAuthorityOperation


@dataclass(frozen=True, slots=True)
class WebsiteProceduresHandlersOperations:
    _authorization_error_response: AuthorizationErrorResponseOperation
    _json_payload: JsonPayloadOperation
    _permission_api_context: PermissionApiContextOperation
    _permission_json_response: PermissionJsonResponseOperation
    _require_permission_browser_session: RequirePermissionBrowserSessionOperation
    _require_permission_write_authority: RequirePermissionWriteAuthorityOperation


@dataclass(frozen=True, slots=True)
class WebsiteSitesHandlersOperations:
    # A page's chat runs only in the signed-in Web Chat session of its owner.
    _require_web_chat_session: RequireWebChatSessionOperation
    _web_session_owner_scope: WebSessionOwnerScopeOperation


@dataclass(frozen=True, slots=True)
class WebsiteWorkspaceHandlersOperations:
    # Any signed-in person; their team roles are looked up again per request.
    _require_web_chat_session: RequireWebChatSessionOperation
