"""aiohttp service for credentialed secure CLI execution."""

from __future__ import annotations

import asyncio
import base64
import dataclasses
import hashlib
import hmac
import html
import json
import logging
import os
import secrets
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from enum import StrEnum
from typing import Any, Awaitable, Callable
from urllib.parse import urlparse
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from aiohttp import web

from runtime_hosting.aiohttp import start_aiohttp_site
from access_control.caller_tokens import CALLER_MANAGER, BrokerCaller, verify_caller_token
from custom_messages.contracts.mcp_v1 import (
    MCPDiscoveryRequestV1,
    MCPHostLifecycleRequestV1,
    MCPInvocationRequestV1,
    MCPInvocationResponseV1,
)
from secure_tool_broker.auth_pages import (
    SECRET_INPUT_TOGGLE_SCRIPT,
    render_broker_auth_page,
    render_credential_input,
    render_close_page_button,
    render_markdown_text,
    render_session_countdown,
)
from secure_tool_broker.sign_in_pages import SIGN_IN_PAGE_SECONDS
from secure_tool_broker.x_sign_in import CALLBACK_PATH as X_CALLBACK_PATH
from secure_tool_broker.auth_flows import (
    BrokerAuthSession,
    available_auth_flow_methods,
    get_auth_flow,
)
from secure_tool_broker.access_gate import BrokerAccessGate
from secure_tool_broker.account_links import (
    STATUS_PENDING as ACCOUNT_LINK_PENDING,
    STATUS_UNAVAILABLE as ACCOUNT_LINK_UNAVAILABLE,
    AccountLinkRefused,
    AccountLinkSession,
    credential_choices,
    linkable_tool,
    owner_scope,
    read_choice,
    render_form,
    required_fields,
    finish_account_link,
    used_by_text,
)
from custom_messages.contracts.access_v2 import AccountLinkRequestV1, PersonCredentialDeletionV1
from secure_tool_broker.credential_transfer_api import add_transfer_routes
from secure_tool_broker.defaults import (
    ONBOARDING_ACCOUNT_KEYS_ROUTE,
    PERSON_CREDENTIAL_DELETION_ROUTE,
    TRANSFER_ROUTE_PREFIX,
)
from secure_tool_broker.departed_person_credentials import delete_person_credentials
from secure_tool_broker.onboarding_account_keys import onboarding_keys_handler
from secure_tool_broker.bitwarden_cli import (
    bitwarden_server_url_from_env,
    ensure_bitwarden_server_configured_from_env,
    set_bitwarden_server_url,
)
from secure_tool_broker.credential_catalog import collect_credential, delete_credential
from secure_tool_broker.credential_catalog_store import CredentialOwnerScope
from secure_tool_broker.credential_state import credential_owner_context
from secure_tool_broker.credential_templates import save_approved_template_variant
from secure_tool_broker.defaults import DEFAULT_BIND_HOST, DEFAULT_BIND_PORT
from secure_tool_broker.executors import (
    execute_validated_action,
    get_auth_status,
    login_bitwarden_with_api_key,
    login_github_with_token,
    process_pending_bitwarden_restart_unlock,
    save_bitwarden_restart_unlock_request,
)
from secure_tool_broker.google_workspace import (
    login_google_with_token_json,
    store_team_client,
)
from access_control.people import is_administrator
from runtime_settings.security import reject_removed_tool_credential_env_vars
from secure_tool_broker.mcp_config import BrokerMCPConfig
from secure_tool_broker.tool_setup_recheck import run_rechecks
from secure_tool_broker.tool_setup_status import TOOL_SETUP_PROVIDER, tool_setup_status
from secure_tool_broker.tool_setup_tracking import note_broker_result
from secure_tool_broker.redaction import redact_json, redact_text
from secure_tools.auth_methods import (
    DEFAULT_AUTH_METHODS,
    GOOGLE_AUTH_METHODS,
    GOOGLE_TEAM_CLIENT_METHOD,
    SIGN_IN_TOOL_BY_PROVIDER,
    TEAM_CLIENT_METHOD,
    X_SIGN_IN_PROVIDER,
    normalize_auth_method,
    sign_in_command,
)
from custom_messages.contracts.model_keys_v1 import MODEL_KEY_AUTH_PROVIDER, TEAM_MODEL_KEY_METHOD
from secure_tool_broker.defaults import (
    MODEL_KEY_ENDPOINTS_ROUTE,
    MODEL_KEY_LOOKUP_ROUTE,
    MODEL_KEY_REPORT_ROUTE,
    MODEL_KEY_ROUTE_PREFIX,
    MODEL_KEY_STATUS_ROUTE,
)
from secure_tool_broker.model_key_page import (
    model_key_reports,
    person_model_key_store,
    team_model_key_store,
    use_access_store,
)
from secure_tool_broker.model_keys import endpoints_handler, lookup_handler, report_handler, status_handler
from secure_tools.basic_providers import register_builtin_providers  # noqa: F401
from secure_tools.providers import ToolContext, ToolResult
from secure_tools.registry import get_provider, list_providers, set_provider_resolver
from secure_tools.installed_clis import installed_cli_provider


LOGGER = logging.getLogger(__name__)
BROKER_CALLER_KEY = web.RequestKey("broker_caller", BrokerCaller)
# Routes only the gateway calls: people signing in and filling credential forms.
GATEWAY_ROUTE_PREFIXES = (
    "/v1/auth/",
    "/v1/credential-collection/",
    "/v1/account-link/",
    "/v1/people/",  # deleting a removed person's credentials
)
# Starting and stopping hosted MCP servers: the manager (after the per-call
# access hook allowed an agent) and the gateway (after the access service
# allowed the signed-in person).
HOSTED_SERVER_ROUTE_PREFIXES = ("/v1/mcp/hosted/",)
# Routes processes that run agents call; each request passes the access gate.
AGENT_ROUTE_PREFIXES = ("/v1/tools/", "/v1/mcp/connections/")
# Model keys go only to processes that make model calls (secure_tool_broker.model_keys);
# no agent tool reaches these routes and sandboxed code cannot read the caller tokens.
MODEL_CALLER_ROUTE_PREFIXES = (MODEL_KEY_ROUTE_PREFIX,)
# The reason code of an MCP request refused for something other than access
# (a changed connection record, an operation outside the pinned catalog).
MCP_CALL_REFUSED = "mcp_call_refused"
# Broker actions the gateway runs for a person directly, after the person
# approved them in the conversation; they concern the person's own accounts.
GATEWAY_DIRECT_ACTIONS = frozenset({("credential_catalog", "signup.execute_approved")})
# The one provider whose actions also run in system work (a task for no
# person): web requests, which use the team's account links.
SYSTEM_WORK_PROVIDER = "secure_requests"
AUTH_PROVIDER_DISPLAY_NAMES = {
    "github": "GitHub",
    "google": "Google",
    "bitwarden": "Bitwarden",
    "onepassword": "1Password",
    "keepass": "KeePass/KeePassXC",
    "vault": "HashiCorp Vault",
    "sealbox": "Sealbox",
    X_SIGN_IN_PROVIDER: "X",
    MODEL_KEY_AUTH_PROVIDER: "model provider",
}
# What an administrator-only /auth method changes, for the refusal message.
TEAM_SETTING_NAMES = {
    ("google", TEAM_CLIENT_METHOD): "the team's Google sign-in app",
    (X_SIGN_IN_PROVIDER, TEAM_CLIENT_METHOD): "the team's X sign-in app",
    (MODEL_KEY_AUTH_PROVIDER, TEAM_MODEL_KEY_METHOD): "the team's model provider keys",
}
AUTH_STATUS_PROVIDERS = (
    "github",
    "google",
    X_SIGN_IN_PROVIDER,
    "bitwarden",
    "onepassword",
    "keepass",
    "vault",
    "sealbox",
)
INTERACTIVE_AUTH_FORM_PROVIDERS = {"github", "google", "bitwarden"}
FORM_AUTH_METHODS = {
    "github": {"token", "token_form", "form"},
    "google": GOOGLE_AUTH_METHODS - {"web"},
    "bitwarden": {"session", "form"},
}


def _format_epoch_seconds_for_display(
    value: Any, user_timezone: str | None = None
) -> str:
    try:
        epoch_seconds = int(value)
    except (TypeError, ValueError):
        return "unknown"
    timezone_name = str(user_timezone or "").strip()
    if timezone_name and len(timezone_name) <= 128:
        try:
            display_zone = ZoneInfo(timezone_name)
        except ZoneInfoNotFoundError:
            display_zone = timezone.utc
            timezone_name = ""
    else:
        display_zone = timezone.utc
        timezone_name = ""
    display_time = datetime.fromtimestamp(epoch_seconds, tz=display_zone)
    timezone_label = display_time.tzname() or "UTC"
    formatted = display_time.strftime("%Y-%m-%d %H:%M:%S")
    if timezone_name and timezone_label != timezone_name:
        return f"{formatted} {timezone_label} ({timezone_name})"
    return f"{formatted} {timezone_label}"


def _configured_user_timezone_from_env() -> str:
    return str(os.environ.get("DEFAULT_REQUEST_TIMEZONE") or "").strip()


COMPLETED_CREDENTIAL_COLLECTION_STATUSES = {
    "credential_stored",
    "credential_deleted",
    "manual_signup_collected",
    "signup_completed",
}
FAILED_CREDENTIAL_COLLECTION_STATUSES = {
    "credential_unavailable",
    "signup_failed",
}

CREDENTIAL_DELETION_FLOWS = {
    "credential_deletion",
    "credential_delete",
    "delete_credential",
}
# The account link result page ends with this; the waiting agent hears the
# outcome from the gateway.
ACCOUNT_LINK_CLOSE_TEXT = (
    "You may close this page. The gateway tells the waiting agent the result, without any credential value."
)


class FormSubmissionClaim(StrEnum):
    """Result of atomically claiming a short-lived broker form session."""

    ACCEPTED = "accepted"
    EXPIRED = "expired"
    IN_PROGRESS = "in_progress"
    RESOLVED = "resolved"


def _claim_form_submission(
    session: Any,
    *,
    now_utc: datetime | None = None,
) -> FormSubmissionClaim:
    """Linearize form acceptance before a handler yields to broker work."""

    if session.used:
        return FormSubmissionClaim.RESOLVED
    if session.submission_in_progress:
        return FormSubmissionClaim.IN_PROGRESS
    now = now_utc or datetime.now(timezone.utc)
    if session.expires_at <= now:
        return FormSubmissionClaim.EXPIRED
    session.submission_in_progress = True
    return FormSubmissionClaim.ACCEPTED


def _finish_form_submission(session: Any) -> None:
    session.submission_in_progress = False


async def _run_claimed_form_thread(
    session: Any,
    callback: Callable[..., Any],
    *args: Any,
    **kwargs: Any,
) -> Any:
    """Keep the claim until the worker exits, even if the request is cancelled."""

    worker = asyncio.create_task(asyncio.to_thread(callback, *args, **kwargs))
    worker.add_done_callback(lambda _worker: _finish_form_submission(session))
    return await asyncio.shield(worker)


def _uses_sensitive_session_material(path: str) -> bool:
    return path.startswith(
        (
            "/auth/",
            "/credential-collection/",
            "/account-link/",
            "/v1/auth/",
            "/v1/credential-collection/sessions",
            "/v1/account-link/sessions",
        )
    )


@web.middleware
async def _sensitive_session_no_store_middleware(
    request: web.Request,
    handler: Callable[[web.Request], Awaitable[web.StreamResponse]],
) -> web.StreamResponse:
    """Prevent browser and intermediary caching of bearer session material."""

    if not _uses_sensitive_session_material(request.path):
        return await handler(request)
    try:
        response = await handler(request)
    except web.HTTPException as exc:
        exc.headers["Cache-Control"] = "no-store"
        raise
    response.headers["Cache-Control"] = "no-store"
    return response


def _render_manual_signup_action(
    request_payload: dict[str, Any], *, service_name: str
) -> str:
    if str(request_payload.get("flow") or "").strip().lower() != "manual_signup":
        return ""
    signup_url = str(request_payload.get("signup_url") or "").strip()
    if not signup_url or any(ord(character) < 0x20 for character in signup_url):
        return ""
    parsed = urlparse(signup_url)
    if (
        parsed.scheme not in {"http", "https"}
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
    ):
        return ""
    safe_url = html.escape(signup_url, quote=True)
    safe_service_name = html.escape(service_name)
    safe_host = html.escape(parsed.hostname)
    return (
        '<div class="note">'
        f"<strong>Create the {safe_service_name} account first.</strong> "
        "Complete signup in a separate tab, then return here to store the credential. "
        f'<a class="button" href="{safe_url}" target="_blank" '
        f'rel="noopener noreferrer">Open {safe_service_name} signup on {safe_host}</a>'
        "</div>"
    )


def _session_payload_for_storage(payload: dict[str, Any]) -> dict[str, Any]:
    safe_payload: dict[str, Any] = {}
    for key, value in payload.items():
        lowered = str(key).lower()
        if any(
            marker in lowered for marker in ("token", "secret", "password", "session")
        ):
            continue
        if isinstance(value, (str, int, float, bool)) or value is None:
            safe_payload[str(key)] = value
        elif isinstance(value, (list, tuple)):
            safe_payload[str(key)] = [str(item) for item in value]
    return safe_payload


@dataclass
class CredentialCollectionSession:
    request_payload: dict[str, Any]
    tool_context: ToolContext
    created_at: datetime
    expires_at: datetime
    status: str = "pending"
    result: dict[str, Any] | None = None
    used: bool = False
    submission_in_progress: bool = False
    state: dict[str, Any] = field(default_factory=dict)


def _require_auth_form_available(session: BrokerAuthSession) -> None:
    if session.submission_in_progress:
        raise web.HTTPConflict(
            text="Auth session submission is already being processed."
        )


def _claim_auth_form_submission(
    session: BrokerAuthSession,
    *,
    prune_expired: Callable[[], None],
) -> None:
    claim = _claim_form_submission(session)
    if claim is FormSubmissionClaim.EXPIRED:
        prune_expired()
        raise web.HTTPGone(text="Auth session expired before submission.")
    if claim is not FormSubmissionClaim.ACCEPTED:
        raise web.HTTPConflict(
            text="Auth session submission is already being processed."
        )


def _account_link_page(title: str, heading: str, text: str, *, status: int = 200) -> web.Response:
    """A one-message page of the account link form (result, refusal, used)."""

    body = (
        '<section class="content-panel"><p class="eyebrow">Account link</p>'
        f"<h1>{html.escape(heading)}</h1><p>{html.escape(text)}</p>{render_close_page_button()}</section>"
    )
    return web.Response(text=render_broker_auth_page(title, body), content_type="text/html", status=status)


def _claim_account_link_submission(
    session: AccountLinkSession,
    *,
    prune_expired: Callable[[], None],
) -> None:
    if session.submission_in_progress:
        raise web.HTTPConflict(text="This account link form is already being processed.")
    if session.status != ACCOUNT_LINK_PENDING or session.used:
        raise web.HTTPConflict(text="This account link form was already used.")
    claim = _claim_form_submission(session)
    if claim is FormSubmissionClaim.EXPIRED:
        prune_expired()
        raise web.HTTPGone(text="This account link form expired.")
    if claim is not FormSubmissionClaim.ACCEPTED:
        raise web.HTTPConflict(text="This account link form is already being processed.")


def _require_credential_collection_submission_available(
    session: CredentialCollectionSession,
) -> None:
    if session.submission_in_progress:
        raise web.HTTPConflict(
            text="Credential collection submission is already being processed."
        )
    if session.status != "pending":
        raise web.HTTPGone(
            text=redact_text(
                str((session.result or {}).get("error") or "")
                or "Credential collection session is no longer available."
            )
        )


def _claim_credential_collection_submission(
    session: CredentialCollectionSession,
    *,
    prune_expired: Callable[[], None],
) -> None:
    if session.status != "pending":
        raise web.HTTPGone(
            text="Credential collection session expired before submission."
        )
    claim = _claim_form_submission(session)
    if claim is FormSubmissionClaim.EXPIRED:
        prune_expired()
        raise web.HTTPGone(text="Credential collection session expired.")
    if claim is not FormSubmissionClaim.ACCEPTED:
        raise web.HTTPConflict(
            text="Credential collection submission is already being processed."
        )


def _mcp_refusal_response(exc: Exception) -> web.Response:
    """A refused MCP request as a 403 whose JSON body the agent's model reads.

    An access refusal carries the gate's reason code, tool, operation, level
    and next step (``MCPAccessRefused``); any other refusal (a changed record,
    an operation outside the pinned catalog) carries its message.
    """

    from secure_tool_broker.mcp_runtime import MCPAccessRefused

    if isinstance(exc, MCPAccessRefused):
        payload = dict(exc.payload)
    else:
        payload = {"error": str(exc), "reason_code": MCP_CALL_REFUSED}
    return web.json_response(payload, status=403)


def _auth_session_team(context_payload: dict[str, Any], team_id: str) -> str:
    """The broker's own team; a session that names another team is refused."""

    claimed = str(context_payload.get("team_id") or "").strip()
    if claimed and claimed != team_id:
        raise web.HTTPForbidden(text="The auth request names another team.")
    return team_id


def _require_method_authority(provider: str, method: str, person_id: str, roles: tuple[str, ...]) -> None:
    """Methods that change a team credential (a team sign-in app, the team's model keys) need an administrator."""

    if (provider, method) in TEAM_SETTING_NAMES:
        _require_team_administrator(provider, person_id, roles, action=f"{method}_session", method=method)


def _require_team_administrator(
    provider: str, person_id: str, roles: tuple[str, ...], *, action: str, method: str = TEAM_CLIENT_METHOD
) -> None:
    """Team credentials are an administrator's; roles come from the gateway (access_control.people)."""

    if is_administrator(roles):
        return
    LOGGER.warning(
        "Team credential change refused: not an administrator. event=team_credential_admin_required "
        "provider=%s action=%s person_id=%s",
        provider,
        action,
        person_id,
    )
    setting = TEAM_SETTING_NAMES.get((provider, method), f"the team's {provider} setting")
    raise web.HTTPForbidden(
        text=json.dumps(
            {
                "error": f"Only a team administrator or owner can change {setting}.",
                "reason_code": f"{provider}_{method}_admin_required",
            }
        ),
        content_type="application/json",
    )


def _google_client_form(session: BrokerAuthSession) -> str:
    """The team's Google sign-in app (OAuth client) form, for administrators.

    A person's own app is pasted on the Google sign-in page instead
    (``GoogleWebAuthFlow``), which stores it for that person's sign-ins.
    """

    countdown_html = render_session_countdown(session.expires_at)
    body = f"""
<section class="content-panel">
  <p class="eyebrow">Google</p>
  <h1>Set the team's Google sign-in app.</h1>
  <p class="lede">People sign in to Google with their own accounts through this app (OAuth client) when they have none of their own. Storing it gives nobody access to anyone's Google data.</p>
</section>
<section class="content-panel">
  <h2>OAuth client ID JSON</h2>
  <ol class="steps">
    <li class="step">Open Google Cloud Console &gt; APIs &amp; Services &gt; Credentials.</li>
    <li class="step">Publish the OAuth consent screen (status <strong>In Production</strong>); sign-ins made while it is in Testing expire after 7 days.</li>
    <li class="step">Create an OAuth client ID with Application type <strong>Desktop app</strong> and download its JSON. Web application clients do not work with gog.</li>
    <li class="step">Paste the full JSON below. The broker checks it and keeps it in the credential store.</li>
  </ol>
  {countdown_html}
  <form method="post">
    <div>
      <label for="credentials_json">OAuth client ID JSON</label>
      <textarea id="credentials_json" name="credentials_json" required rows="12" autocomplete="off"></textarea>
    </div>
    <button type="submit">Store the team's app</button>
  </form>
</section>"""
    return render_broker_auth_page("Google sign-in app", body)


async def _submit_google_form(session: BrokerAuthSession, form: Any, *, team_id: str) -> ToolResult:
    """Store the team's app, or the person's imported sign-in."""

    credentials_json = str(form.get("credentials_json") or "")
    person = session.owner_canonical_user_id
    if session.method == GOOGLE_TEAM_CLIENT_METHOD:
        # Checked when the session was made and again now, from the signed session.
        if not is_administrator(session.owner_roles):
            LOGGER.warning(
                "Team Google client refused at submission. event=team_credential_admin_required "
                "action=team_client_submit person_id=%s",
                person,
            )
            _finish_form_submission(session)
            return ToolResult(
                status="error",
                error="Only a team administrator or owner can set the team's Google sign-in app.",
                content={"error_code": "google_team_client_admin_required"},
            )
        return await _run_claimed_form_thread(
            session, store_team_client, team_id, credentials_json, stored_by=person
        )
    return await _run_claimed_form_thread(
        session,
        login_google_with_token_json,
        str(form.get("token_json") or ""),
        person_id=person,
        team_id=team_id,
        credentials_json=credentials_json,
    )


def _b64url_encode(raw: bytes) -> str:
    """Unpadded URL-safe base64, as used in signed sign-in session ids."""

    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def _b64url_decode(raw: str) -> bytes:
    padded = raw + ("=" * (-len(raw) % 4))
    return base64.urlsafe_b64decode(padded.encode("ascii"))


class SecureToolBrokerService:
    def __init__(
        self,
        *,
        token: str,
        access_gate: BrokerAccessGate,
        logger: logging.Logger | None = None,
        mcp_config: BrokerMCPConfig | None = None,
    ) -> None:
        if not token:
            raise ValueError(
                "SECURE_TOOL_BROKER_TOKEN is required to start the secure tool broker"
            )
        # The root secret: callers present tokens derived from it per process
        # (access_control.caller_tokens), never the root itself.
        self._token = token
        self._access_gate = access_gate
        # Installed CLIs are providers from the current catalog (secure_tools.installed_clis).
        set_provider_resolver(lambda name: installed_cli_provider(self._access_gate.catalog, name))
        # Long-lived model key pages re-check the person's current roles, and a person's page saves their
        # own model (secure_tool_broker.model_key_page).
        use_access_store(access_gate.links, access_gate.team_id)
        self._logger = logger or LOGGER
        self._mcp_config = mcp_config
        self._mcp_runtime: Any | None = None
        self._auth_sessions: dict[str, BrokerAuthSession] = {}
        self._credential_collection_sessions: dict[
            str, CredentialCollectionSession
        ] = {}
        self._account_link_sessions: dict[str, AccountLinkSession] = {}
        self._account_link_session_by_request: dict[str, str] = {}

    def build_app(self) -> web.Application:
        app = web.Application(
            middlewares=[
                _sensitive_session_no_store_middleware,
                self._auth_middleware,
            ]
        )
        app.router.add_get("/health", self._handle_health)
        app.router.add_get("/v1/tools", self._handle_list_tools)
        app.router.add_post("/v1/tools/{provider}", self._handle_execute)
        app.router.add_post(
            "/v1/mcp/connections/{connection_id}/tools",
            self._handle_mcp_discover_tools,
        )
        app.router.add_post(
            "/v1/mcp/connections/{connection_id}/invoke",
            self._handle_mcp_invoke,
        )
        app.router.add_post(
            "/v1/mcp/hosted/{hosted_server_id}/{operation}",
            self._handle_mcp_host_lifecycle,
        )
        app.router.add_post(
            "/v1/credential-collection/sessions",
            self._handle_create_credential_collection_session,
        )
        app.router.add_get(
            "/v1/credential-collection/sessions/{session_id}",
            self._handle_credential_collection_session_status,
        )
        app.router.add_post(
            "/v1/account-link/sessions",
            self._handle_create_account_link_session,
        )
        app.router.add_get(
            "/v1/account-link/sessions/{session_id}",
            self._handle_account_link_session_status,
        )
        app.router.add_post(PERSON_CREDENTIAL_DELETION_ROUTE, self._handle_delete_person_credentials)
        add_transfer_routes(app, team_id=self._access_gate.team_id)
        app.router.add_post(ONBOARDING_ACCOUNT_KEYS_ROUTE, onboarding_keys_handler(self._access_gate))
        model_keys = team_model_key_store(self._access_gate.team_id)
        app.router.add_post(
            MODEL_KEY_LOOKUP_ROUTE,
            lookup_handler(model_keys, person_model_key_store, self._access_gate, BROKER_CALLER_KEY),
        )
        app.router.add_post(MODEL_KEY_REPORT_ROUTE, report_handler(model_key_reports(), BROKER_CALLER_KEY))
        app.router.add_get(MODEL_KEY_STATUS_ROUTE, status_handler(model_keys, model_key_reports()))
        app.router.add_get(MODEL_KEY_ENDPOINTS_ROUTE, endpoints_handler(model_keys))
        app.router.add_get("/v1/auth/status", self._handle_auth_status)
        app.router.add_get("/v1/auth/status/{provider}", self._handle_auth_status)
        app.router.add_post("/v1/auth/status", self._handle_auth_status)
        app.router.add_post("/v1/auth/status/{provider}", self._handle_auth_status)
        app.router.add_post(
            "/v1/auth/{provider}/sessions", self._handle_create_auth_session
        )
        app.cleanup_ctx.append(self._mcp_lifecycle_context)
        app.router.add_post(
            "/v1/auth/bitwarden/server", self._handle_bitwarden_server_config
        )
        # Registered before /auth/{provider}/{session_id}, which would also match it.
        app.router.add_get(X_CALLBACK_PATH, self._handle_x_sign_in_callback)
        app.router.add_get(
            "/auth/{provider}/{session_id}/status", self._handle_auth_session_status
        )
        app.router.add_get("/auth/{provider}/{session_id}", self._handle_auth_form)
        app.router.add_post(
            "/auth/{provider}/{session_id}", self._handle_submit_auth_form
        )
        app.router.add_get(
            "/credential-collection/{session_id}",
            self._handle_credential_collection_form,
        )
        app.router.add_post(
            "/credential-collection/{session_id}",
            self._handle_submit_credential_collection_form,
        )
        app.router.add_get("/account-link/{session_id}", self._handle_account_link_form)
        app.router.add_post("/account-link/{session_id}", self._handle_submit_account_link_form)
        return app

    async def _mcp_lifecycle_context(self, _app: web.Application):
        runtime = None
        if self._mcp_config is not None:
            try:
                runtime = self._get_mcp_runtime()
                await runtime.restore_hosted()
            except Exception:
                self._logger.exception(
                    "Hosted MCP restore failed during broker startup. "
                    "event=mcp_host_restore_startup_failed"
                )
        yield
        if runtime is not None:
            await runtime.close()

    @web.middleware
    async def _auth_middleware(
        self, request: web.Request, handler: Any
    ) -> web.StreamResponse:
        if (
            request.path == "/health"
            or request.path.startswith("/auth/")
            or request.path.startswith("/credential-collection/")
            or request.path.startswith("/account-link/")
        ):
            return await handler(request)
        caller = verify_caller_token(self._token, self._extract_token(request))
        if caller is None:
            raise web.HTTPUnauthorized(
                text="Missing or invalid secure tool broker token."
            )
        if not self._caller_may_use(caller, request.path):
            self._logger.warning(
                "Broker route refused for this caller. event=broker_route_refused "
                "caller=%s path=%s",
                caller.label,
                request.path,
            )
            raise web.HTTPForbidden(
                text=f"The {caller.label} process cannot use this broker route."
            )
        request[BROKER_CALLER_KEY] = caller
        return await handler(request)

    @staticmethod
    def _caller_may_use(caller: BrokerCaller, path: str) -> bool:
        """Which processes may call which routes (per-process broker tokens)."""

        if path.startswith(TRANSFER_ROUTE_PREFIX):
            # Team transfer: the operator's own command only (credential_transfer).
            return caller.is_operator
        if caller.is_operator:
            return False
        if path == "/v1/tools":
            return True
        if path.startswith(GATEWAY_ROUTE_PREFIXES):
            return caller.is_gateway
        if path.startswith(HOSTED_SERVER_ROUTE_PREFIXES):
            return caller.label == CALLER_MANAGER or caller.is_gateway
        if path.startswith("/v1/tools/"):
            # The gateway may run only GATEWAY_DIRECT_ACTIONS (_handle_execute).
            return caller.runs_agents or caller.is_gateway
        if path.startswith(MODEL_CALLER_ROUTE_PREFIXES):
            # The gateway's Grok voice may look up the xAI key only
            # (secure_tool_broker.model_keys.lookup_handler checks the address).
            return caller.runs_agents or caller.is_gateway
        if path.startswith(AGENT_ROUTE_PREFIXES):
            return caller.runs_agents
        return False

    @staticmethod
    def _extract_token(request: web.Request) -> str | None:
        auth_header = request.headers.get("Authorization", "")
        if auth_header.lower().startswith("bearer "):
            return auth_header.split(" ", 1)[1].strip() or None
        return request.headers.get("X-Secure-Tool-Broker-Token")

    async def _handle_health(self, _request: web.Request) -> web.Response:
        return web.json_response(
            {
                "status": "ok",
                "providers": list_providers(),
                "auth_session_format": "signed_v1",
            }
        )

    def _prune_auth_sessions(self) -> None:
        now = datetime.now(timezone.utc)
        expired = [
            key
            for key, session in self._auth_sessions.items()
            if session.expires_at <= now and not session.submission_in_progress
        ]
        for key in expired:
            session = self._auth_sessions.pop(key, None)
            if session is not None:
                flow = get_auth_flow(session.provider, session.method)
                if flow is not None:
                    flow.cleanup(session)

    def _decode_signed_auth_session_payload(
        self,
        provider: str,
        session_id: str,
    ) -> tuple[dict[str, Any] | None, str | None]:
        parts = session_id.split(".", 2)
        if len(parts) != 3 or parts[0] != "v1":
            return None, "legacy_unsigned_session_id"
        encoded_payload, supplied_signature = parts[1], parts[2]
        expected_signature = hmac.new(
            self._token.encode("utf-8"),
            encoded_payload.encode("ascii"),
            hashlib.sha256,
        ).digest()
        try:
            supplied_signature_bytes = _b64url_decode(supplied_signature)
        except Exception:
            return None, "invalid_signature_encoding"
        if not hmac.compare_digest(expected_signature, supplied_signature_bytes):
            return None, "invalid_signature"
        try:
            payload = json.loads(_b64url_decode(encoded_payload).decode("utf-8"))
        except Exception:
            return None, "invalid_payload_encoding"
        if not isinstance(payload, dict):
            return None, "invalid_payload_shape"
        if str(payload.get("provider") or "") != provider:
            return None, "provider_mismatch"
        try:
            expires_at = datetime.fromisoformat(str(payload.get("expires_at") or ""))
        except ValueError:
            return None, "invalid_expires_at"
        if expires_at.tzinfo is None:
            expires_at = expires_at.replace(tzinfo=timezone.utc)
        if expires_at <= datetime.now(timezone.utc):
            return None, "expired_signed_session"
        return payload, None

    def _sign_session_payload(self, payload: dict[str, Any]) -> str:
        raw_payload = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode(
            "utf-8"
        )
        encoded_payload = _b64url_encode(raw_payload)
        signature = hmac.new(
            self._token.encode("utf-8"),
            encoded_payload.encode("ascii"),
            hashlib.sha256,
        ).digest()
        return f"v1.{encoded_payload}.{_b64url_encode(signature)}"

    def _restore_signed_auth_session(
        self, provider: str, session_id: str
    ) -> BrokerAuthSession | None:
        payload, failure_reason = self._decode_signed_auth_session_payload(
            provider, session_id
        )
        if payload is None:
            self._logger.debug(
                "Signed broker auth session restore skipped. event=broker_auth_session_restore_skipped "
                "provider=%s reason=%s session_id_prefix=%s",
                provider,
                failure_reason,
                session_id[:12],
            )
            return None
        method = str(payload.get("method") or "").strip().lower()
        try:
            expires_at = datetime.fromisoformat(str(payload.get("expires_at") or ""))
        except ValueError:
            return None
        if expires_at.tzinfo is None:
            expires_at = expires_at.replace(tzinfo=timezone.utc)
        start_payload = (
            dict(payload.get("payload") or {})
            if isinstance(payload.get("payload"), dict)
            else {}
        )
        session = BrokerAuthSession(
            provider=provider,
            method=method,
            created_at=datetime.now(timezone.utc),
            expires_at=expires_at,
            owner_canonical_user_id=str(payload.get("owner_canonical_user_id") or ""),
            owner_team_id=str(payload.get("owner_team_id") or "") or None,
            owner_roles=tuple(str(role) for role in payload.get("owner_roles") or ()),
            state={"start_payload": start_payload, "status": "created"},
        )
        self._auth_sessions[session_id] = session
        self._logger.info(
            "Restored signed broker auth session. event=broker_auth_session_restored "
            "provider=%s method=%s session_id_prefix=%s expires_at=%s",
            provider,
            method,
            session_id[:12],
            expires_at.isoformat(),
        )
        return session

    def _auth_session_lookup_failure_reason(
        self, provider: str, session_id: str
    ) -> str:
        session = self._auth_sessions.get(session_id)
        if session is not None and session.provider != provider:
            return "active_session_provider_mismatch"
        _payload, failure_reason = self._decode_signed_auth_session_payload(
            provider, session_id
        )
        return failure_reason or "signed_session_not_loaded"

    @staticmethod
    def _auth_session_lookup_failure_text(provider: str, reason: str) -> str:
        command = sign_in_command(provider) if provider in SIGN_IN_TOOL_BY_PROVIDER else f"/auth {provider}"
        if reason == "legacy_unsigned_session_id":
            return (
                "Auth session not found or expired. This looks like an older in-memory broker auth URL. "
                f"Run {command} (or the /auth command you used) again and open the new signed URL."
            )
        if reason == "invalid_signature":
            return (
                "Auth session not found or expired. This signed auth URL could not be verified by this broker. "
                "Check that the browser is reaching the same broker deployment and broker token, then run "
                f"{command} (or the /auth command you used) again."
            )
        if reason == "expired_signed_session":
            return f"Auth session expired. Run {command} (or the /auth command you used) again to open a new page."
        return "Auth session not found or expired."

    async def _handle_list_tools(self, _request: web.Request) -> web.Response:
        providers: dict[str, list[str]] = {}
        provider_details: dict[str, dict[str, dict[str, Any]]] = {}
        for name in list_providers():
            provider = get_provider(name)
            providers[name] = sorted(provider.actions) if provider else []
            provider_details[name] = {
                action_name: {"description": spec.description}
                for action_name, spec in sorted(
                    (provider.actions if provider else {}).items()
                )
            }
        return web.json_response(
            {"providers": providers, "provider_details": provider_details}
        )

    @staticmethod
    def _validated_action(provider_name: str, payload: dict[str, Any]) -> Any:
        provider = get_provider(provider_name)
        if provider is None:
            raise web.HTTPNotFound(text=f"Unknown secure tool provider: {provider_name}")
        if str(payload.get("provider") or provider_name).strip() != provider_name:
            raise web.HTTPBadRequest(text="Provider path and payload provider do not match.")
        try:
            return provider.validate(str(payload.get("action") or ""), dict(payload.get("params") or {}))
        except Exception as exc:
            raise web.HTTPBadRequest(text=f"Invalid action: {exc}") from exc

    @staticmethod
    def _request_tool_context(
        payload: dict[str, Any], trusted_context: dict[str, Any], *, provider: str = ""
    ) -> ToolContext:
        """The request's context; the access gate then checks it against the signed task context.

        Actions need a person, except web requests (``secure_requests``) in
        system work: those use the team's account links, and the gate
        accepts no person only when the signed context names none.
        """

        def text(key: str) -> str | None:
            return str(trusted_context.get(key) or "") or None

        context = ToolContext(
            agent_id=text("agent_id"),
            task_id=text("task_id"),
            conversation_id=text("conversation_id"),
            reason=str(payload.get("reason") or "") or None,
            canonical_user_id=text("canonical_user_id"),
            team_id=text("team_id"),
            principal_type=text("principal_type"),
            named_agent_type=text("named_agent_type"),
            invocation_id=text("invocation_id"),
        )
        if provider == SYSTEM_WORK_PROVIDER and not context.canonical_user_id and context.principal_type == "system":
            return context
        if not context.canonical_user_id or context.principal_type != "user":
            raise web.HTTPForbidden(text="A trusted registered user context is required.")
        return context

    def _admitted_context(
        self, caller: BrokerCaller, validated: Any, context: ToolContext, trusted_context: dict[str, Any]
    ) -> ToolContext:
        """The context executors may use, after the caller's check.

        The gateway runs only actions a person approved directly; every agent
        request passes the access gate, which verifies the team and chooses the
        account. Executors use these, never request fields.
        """

        if caller.is_gateway:
            if (validated.provider, validated.action) not in GATEWAY_DIRECT_ACTIONS:
                raise web.HTTPForbidden(text="The gateway runs only actions a person approved directly.")
            return context
        gate = self._access_gate.check_broker_action(
            caller, validated.provider, validated.action, validated.params, trusted_context
        )
        if not gate.allowed:
            raise web.HTTPForbidden(text=json.dumps(gate.as_error()))
        return dataclasses.replace(context, team_id=self._access_gate.team_id, account_link=gate.account_link)

    async def _handle_execute(self, request: web.Request) -> web.Response:
        payload = await request.json()
        validated = self._validated_action(request.match_info["provider"], payload)
        trusted_context = payload.get("context") if isinstance(payload.get("context"), dict) else {}
        context = self._admitted_context(
            request[BROKER_CALLER_KEY],
            validated,
            self._request_tool_context(payload, trusted_context, provider=validated.provider),
            trusted_context,
        )
        if validated.provider == TOOL_SETUP_PROVIDER:
            # Needs the gate's account links and catalog (set_up_tools).
            return web.json_response(
                await asyncio.to_thread(tool_setup_status, validated.params, context, self._access_gate)
            )
        if (
            validated.provider == "credential_catalog"
            and validated.action == "signup.manual_fallback_session"
        ):
            request_payload = dict(validated.params.get("request") or validated.params)
            return self._create_credential_collection_session_response(
                request=request,
                request_payload=request_payload,
                tool_context=context,
                session_status="manual_signup_required",
            )
        result = await asyncio.to_thread(execute_validated_action, validated, context)
        # The person's last known tool setup state (access_control.tool_setup_states).
        await asyncio.to_thread(note_broker_result, self._access_gate, validated, context, result)
        body = {"status": result.status, "content": dict(result.content)}
        if result.error:
            body["error"] = result.error
        self._logger.info(
            "secure tool action completed provider=%s action=%s status=%s agent_id=%s task_id=%s",
            validated.provider,
            validated.action,
            result.status,
            context.agent_id,
            context.task_id,
        )
        return web.json_response(body)

    def _get_mcp_runtime(self) -> Any:
        if self._mcp_runtime is not None:
            return self._mcp_runtime
        if self._mcp_config is None:
            raise web.HTTPServiceUnavailable(
                text="Brokered MCP requires an operator-owned configuration projection."
            )
        from secure_tool_broker.mcp_runtime import BrokerMCPRuntime

        self._mcp_runtime = BrokerMCPRuntime(config=self._mcp_config, access_gate=self._access_gate)
        return self._mcp_runtime

    async def _handle_mcp_discover_tools(self, request: web.Request) -> web.Response:
        payload = await self._read_json_payload(request)
        connection_id = str(request.match_info["connection_id"] or "")
        try:
            contract = MCPDiscoveryRequestV1.model_validate(payload)
            result = await self._get_mcp_runtime().discover_tools(
                connection_id, contract, caller=request[BROKER_CALLER_KEY]
            )
        except (PermissionError, FileNotFoundError, ValueError) as exc:
            self._logger.warning(
                "Brokered MCP discovery denied. "
                "event=mcp_broker_discovery_denied connection_id=%s reason=%s",
                connection_id,
                str(exc),
            )
            return _mcp_refusal_response(exc)
        except Exception as exc:
            self._logger.exception(
                "Brokered MCP discovery failed. event=mcp_broker_discovery_failed"
            )
            raise web.HTTPBadGateway(text="Brokered MCP discovery failed.") from exc
        return web.json_response(
            result.model_dump(mode="json", by_alias=True, exclude_none=True)
        )

    async def _handle_mcp_invoke(self, request: web.Request) -> web.Response:
        payload = await self._read_json_payload(request)
        connection_id = str(request.match_info["connection_id"] or "")
        try:
            contract = MCPInvocationRequestV1.model_validate(payload)
            result = await self._get_mcp_runtime().invoke(
                connection_id,
                contract,
                caller=request[BROKER_CALLER_KEY],
            )
        except (PermissionError, FileNotFoundError, ValueError) as exc:
            self._logger.warning(
                "Brokered MCP invocation refused. event=mcp_broker_invocation_refused connection_id=%s reason=%s",
                connection_id,
                str(exc),
            )
            return _mcp_refusal_response(exc)
        except Exception as exc:
            self._logger.exception(
                "Brokered MCP invocation failed. event=mcp_broker_invocation_failed"
            )
            raise web.HTTPBadGateway(text="Brokered MCP invocation failed.") from exc
        response = MCPInvocationResponseV1(result=result)
        return web.json_response(
            response.model_dump(mode="json", by_alias=True, exclude_none=True)
        )

    async def _handle_mcp_host_lifecycle(
        self,
        request: web.Request,
    ) -> web.Response:
        payload = await self._read_json_payload(request)
        operation = str(request.match_info.get("operation") or "").strip()
        try:
            contract = MCPHostLifecycleRequestV1.model_validate(payload)
            result = await self._get_mcp_runtime().change_host_lifecycle(
                str(request.match_info.get("hosted_server_id") or ""),
                contract,
                operation=operation,
            )
        except (PermissionError, FileNotFoundError, ValueError) as exc:
            raise web.HTTPForbidden(text=str(exc)) from exc
        except Exception as exc:
            self._logger.exception(
                "Hosted MCP lifecycle failed. event=mcp_host_lifecycle_failed "
                "hosted_server_id=%s operation=%s",
                request.match_info.get("hosted_server_id"),
                operation,
            )
            raise web.HTTPBadGateway(text="Hosted MCP lifecycle failed.") from exc
        return web.json_response(result)

    def _prune_account_link_sessions(self) -> None:
        now = datetime.now(timezone.utc)
        for session_id, session in list(self._account_link_sessions.items()):
            if session.expires_at <= now and session.status == ACCOUNT_LINK_PENDING and not session.submission_in_progress:
                session.status = ACCOUNT_LINK_UNAVAILABLE
                session.result = {
                    "status": ACCOUNT_LINK_UNAVAILABLE,
                    "error": "The account link form expired before it was used.",
                    "reason_code": "account_link_session_expired",
                }
            if session.expires_at + timedelta(minutes=30) <= now and not session.submission_in_progress:
                self._account_link_sessions.pop(session_id, None)
        self._account_link_session_by_request = {
            key: value
            for key, value in self._account_link_session_by_request.items()
            if value in self._account_link_sessions
        }

    def _account_link_store(self) -> Any:
        store = self._access_gate.links
        if store is None:
            raise web.HTTPServiceUnavailable(text="The broker has no access store for account links.")
        return store

    async def _handle_delete_person_credentials(self, request: web.Request) -> web.Response:
        """Delete a removed person's credentials (``departed_person_credentials``); gateway only."""

        payload = await self._read_json_payload(request)
        try:
            deletion = PersonCredentialDeletionV1.model_validate(payload)
        except Exception as exc:
            raise web.HTTPBadRequest(text=f"Invalid credential deletion request: {exc}") from exc
        if deletion.team_id != self._access_gate.team_id:
            raise web.HTTPForbidden(text="The credential deletion request names another team.")
        result = await asyncio.to_thread(delete_person_credentials, deletion)
        return web.json_response(result.model_dump(mode="json"))

    async def _handle_create_account_link_session(self, request: web.Request) -> web.Response:
        payload = await self._read_json_payload(request)
        try:
            link_request = AccountLinkRequestV1.model_validate(payload)
        except Exception as exc:
            raise web.HTTPBadRequest(text=f"Invalid account link request: {exc}") from exc
        if link_request.team_id != self._access_gate.team_id:
            raise web.HTTPForbidden(text="The account link request names another team.")
        try:
            tool = linkable_tool(self._access_gate.catalog, link_request, self._access_gate.account_sharing())
        except AccountLinkRefused as exc:
            raise web.HTTPForbidden(
                text=json.dumps({"error": str(exc), "reason_code": exc.reason_code})
            ) from exc
        self._prune_account_link_sessions()
        existing_id = self._account_link_session_by_request.get(link_request.request_id)
        existing = self._account_link_sessions.get(existing_id or "")
        if existing is not None and existing_id is not None:
            if existing.request != link_request:
                raise web.HTTPConflict(text="The account link request id was reused with different content.")
            return self._account_link_session_response(request, existing_id, existing, tool.display_name)
        now = datetime.now(timezone.utc)
        session_id = secrets.token_urlsafe(32)
        session = AccountLinkSession(
            request=link_request,
            created_at=now,
            expires_at=min(
                now + timedelta(seconds=self._credential_collection_ttl_seconds()),
                now + timedelta(minutes=30),
            ),
        )
        self._account_link_sessions[session_id] = session
        self._account_link_session_by_request[link_request.request_id] = session_id
        self._logger.info(
            "Created account link session. event=account_link_session_created tool_id=%s owner_kind=%s "
            "person_id=%s session_id_prefix=%s",
            link_request.tool_id,
            link_request.owner_kind.value,
            link_request.person_id,
            session_id[:12],
        )
        return self._account_link_session_response(request, session_id, session, tool.display_name)

    @staticmethod
    def _account_link_session_response(
        request: web.Request, session_id: str, session: AccountLinkSession, tool_display_name: str
    ) -> web.Response:
        base_url = f"{request.scheme}://{request.host}"
        return web.json_response(
            {
                "status": session.status,
                "session_id": session_id,
                # The gateway names the tool this way in chat messages about the form.
                "tool_display_name": tool_display_name,
                "form_url": f"{base_url}/account-link/{session_id}",
                "status_url": f"{base_url}/v1/account-link/sessions/{session_id}",
                "expires_at": session.expires_at.isoformat(),
                "message": "Open the link to choose which stored credential this tool uses.",
            }
        )

    def _get_account_link_session(self, session_id: str) -> AccountLinkSession:
        self._prune_account_link_sessions()
        session = self._account_link_sessions.get(str(session_id or "").strip())
        if session is None:
            raise web.HTTPNotFound(text="Account link form not found or expired.")
        return session

    async def _handle_account_link_session_status(self, request: web.Request) -> web.Response:
        session_id = str(request.match_info["session_id"] or "").strip()
        session = self._get_account_link_session(session_id)
        return web.json_response(
            {
                "session_id": session_id,
                "status": session.status,
                "result": session.result,
                "expires_at": session.expires_at.isoformat(),
            }
        )

    async def _handle_account_link_form(self, request: web.Request) -> web.Response:
        session_id = str(request.match_info["session_id"] or "").strip()
        session = self._get_account_link_session(session_id)
        if session.status != ACCOUNT_LINK_PENDING:
            return _account_link_page(
                "Account link", session.status.replace("_", " ").capitalize(), "This one-time form was already used."
            )
        try:
            tool = linkable_tool(self._access_gate.catalog, session.request, self._access_gate.account_sharing())
        except AccountLinkRefused as exc:
            return _account_link_page("Account link unavailable", "Account link unavailable", str(exc), status=409)
        choices = credential_choices(owner_scope(session.request), tool)
        if not choices and not required_fields(tool):
            # Nothing to pick and no fields to type: the tool names no credential fields.
            return _account_link_page(
                "Account link unavailable",
                "No suitable stored credential",
                f"Save a credential with the fields {tool.display_name} needs first, then ask again.",
                status=409,
            )
        used_by = used_by_text(self._access_gate.access, tool, session.request)
        return web.Response(
            text=render_broker_auth_page(
                "Link an account",
                render_form(session_id, session, tool, choices, used_by=used_by),
                script=SECRET_INPUT_TOGGLE_SCRIPT,
            ),
            content_type="text/html",
        )

    async def _handle_submit_account_link_form(self, request: web.Request) -> web.Response:
        session_id = str(request.match_info["session_id"] or "").strip()
        session = self._get_account_link_session(session_id)
        form = await request.post()
        # Read everything before claiming, so nothing can fail between the
        # claim and the worker that releases it.
        store = self._account_link_store()
        catalog = self._access_gate.catalog
        confirmed = str(form.get("confirm") or "").strip().lower() == "yes"
        choice = read_choice(form)
        _claim_account_link_submission(session, prune_expired=self._prune_account_link_sessions)
        session.used = True
        # Storing a new credential runs the credential store's commands, so the
        # work runs in a thread that finishes even if the browser disconnects.
        sentence = await _run_claimed_form_thread(
            session, finish_account_link, session, store, catalog, confirmed=confirmed, choice=choice
        )
        return _account_link_page(
            "Account link result",
            session.status.replace("_", " ").capitalize(),
            f"{sentence} {ACCOUNT_LINK_CLOSE_TEXT}",
        )

    @staticmethod
    def _credential_collection_ttl_seconds() -> int:
        raw = str(
            os.getenv("SECURE_TOOL_BROKER_COLLECTION_SESSION_TTL_SECONDS") or ""
        ).strip()
        if not raw:
            return 900
        try:
            return max(60, min(int(raw), 86_400))
        except ValueError:
            return 900

    @staticmethod
    def _credential_collection_max_file_bytes() -> int:
        raw = str(
            os.getenv("SECURE_TOOL_BROKER_CREDENTIAL_COLLECTION_MAX_FILE_BYTES") or ""
        ).strip()
        if not raw:
            return 262_144
        try:
            return max(1, min(int(raw), 1_048_576))
        except ValueError:
            return 262_144

    @staticmethod
    def _credential_field_specs(
        request_payload: dict[str, Any],
    ) -> list[dict[str, Any]]:
        if SecureToolBrokerService._is_credential_deletion_request(request_payload):
            return []
        raw_fields = request_payload.get("fields")
        if isinstance(raw_fields, dict):
            rendered_fields = [
                dict({"name": key}, **dict(value or {}))
                for key, value in raw_fields.items()
                if isinstance(value, dict)
            ]
            if rendered_fields:
                return rendered_fields
        if isinstance(raw_fields, list):
            rendered_fields = [
                dict(value) for value in raw_fields if isinstance(value, dict)
            ]
            if rendered_fields:
                return rendered_fields

        username = str(
            request_payload.get("username")
            or request_payload.get("desired_username")
            or request_payload.get("email")
            or ""
        ).strip()
        password_field = (
            str(
                request_payload.get("password_field_name")
                or request_payload.get("password_field_ref")
                or "password"
            ).strip()
            or "password"
        )
        fields: list[dict[str, Any]] = [
            {
                "name": "username",
                "kind": "username",
                "visibility": "metadata_visible",
                "value_source": "user_or_agent_supplied_metadata",
                "required": True,
            },
            {
                "name": password_field,
                "kind": "password",
                "visibility": "secret",
                "value_source": "user_input",
                "required": True,
            },
        ]
        if username:
            fields[0]["value"] = username
        return fields

    @staticmethod
    def _is_credential_deletion_request(request_payload: dict[str, Any]) -> bool:
        flow = (
            str(
                request_payload.get("flow") or request_payload.get("request_kind") or ""
            )
            .strip()
            .lower()
        )
        return flow in CREDENTIAL_DELETION_FLOWS

    @staticmethod
    def _normalize_credential_collection_request(
        payload: dict[str, Any],
    ) -> dict[str, Any]:
        request_payload = dict(payload)
        owner_scope_kind = (
            str(request_payload.get("owner_scope_kind") or "user").strip().lower()
        )
        if owner_scope_kind not in {"user", "team"}:
            raise web.HTTPBadRequest(text="owner_scope_kind must be user or team.")
        request_payload["owner_scope_kind"] = owner_scope_kind
        request_payload["credential_key"] = str(
            request_payload.get("credential_key") or ""
        ).strip()
        if not request_payload["credential_key"]:
            raise web.HTTPBadRequest(text="credential_key is required.")
        if SecureToolBrokerService._is_credential_deletion_request(request_payload):
            request_payload["flow"] = "credential_deletion"
            request_payload.setdefault(
                "service_name",
                request_payload.get("display_name")
                or request_payload["credential_key"],
            )
            request_payload.setdefault(
                "display_name",
                request_payload.get("service_name")
                or request_payload["credential_key"],
            )
            request_payload.setdefault(
                "purpose", "Credential deletion requested by the user."
            )
            request_payload["fields"] = []
            return request_payload
        request_payload.setdefault(
            "display_name",
            request_payload.get("service_name") or request_payload["credential_key"],
        )
        request_payload.setdefault(
            "purpose",
            "Credential collected for approved agent workflow.",
        )
        request_payload["fields"] = SecureToolBrokerService._credential_field_specs(
            request_payload
        )
        for field_spec in request_payload["fields"]:
            if (
                SecureToolBrokerService._credential_form_field_is_secret(field_spec)
                and str(field_spec.get("value") or "").strip()
            ):
                raise web.HTTPBadRequest(
                    text=(
                        f"Secret field {field_spec.get('name') or field_spec.get('field_ref')} "
                        "must not include a value in the request payload."
                    )
                )
        return request_payload

    @staticmethod
    def _credential_collection_public_result(
        session: CredentialCollectionSession,
    ) -> dict[str, Any] | None:
        if session.result is None:
            return None
        result = dict(session.result)
        content = result.get("content")
        if isinstance(content, dict):
            result["content"] = {
                key: value
                for key, value in content.items()
                if key not in {"password", "secret", "token", "value", "raw_value"}
            }
        if result.get("error"):
            result["error"] = redact_text(str(result["error"]))
        return result

    def _prune_credential_collection_sessions(self) -> None:
        now = datetime.now(timezone.utc)
        for session_id, session in list(self._credential_collection_sessions.items()):
            if (
                session.expires_at <= now
                and session.status == "pending"
                and not session.submission_in_progress
            ):
                session.status = "credential_unavailable"
                session.result = {
                    "status": "error",
                    "error": "Credential collection session expired.",
                    "content": {
                        "status": "credential_unavailable",
                        "error_code": "credential_collection_session_expired",
                    },
                }
            if (
                session.expires_at <= now - timedelta(minutes=30)
                and not session.submission_in_progress
            ):
                self._credential_collection_sessions.pop(session_id, None)

    def _create_credential_collection_session_response(
        self,
        *,
        request: web.Request,
        request_payload: dict[str, Any],
        tool_context: ToolContext,
        session_status: str = "pending",
    ) -> web.Response:
        self._prune_credential_collection_sessions()
        normalized_payload = self._normalize_credential_collection_request(
            request_payload
        )
        now = datetime.now(timezone.utc)
        expires_at = now + timedelta(seconds=self._credential_collection_ttl_seconds())
        session_id = secrets.token_urlsafe(32)
        session = CredentialCollectionSession(
            request_payload=normalized_payload,
            tool_context=tool_context,
            created_at=now,
            expires_at=expires_at,
            status="pending",
            state={"initial_status": session_status},
        )
        if normalized_payload.get(
            "save_template_variant_on_approval"
        ) and normalized_payload.get("user_approved"):
            save_result = save_approved_template_variant(
                {
                    "user_approved": True,
                    "template_variant": normalized_payload.get("template_variant"),
                    "request": normalized_payload,
                }
            )
            session.state["template_variant_save_result"] = {
                "status": save_result.status,
                "content": dict(save_result.content or {}),
                "error": redact_text(save_result.error or "")
                if save_result.error
                else None,
            }
            if save_result.status != "success":
                raise web.HTTPBadRequest(
                    text=redact_text(
                        save_result.error or "Unable to save approved template variant."
                    )
                )
            normalized_payload["approved_template_variant_id"] = (
                save_result.content.get("template_id")
            )
        self._credential_collection_sessions[session_id] = session
        base_url = f"{request.scheme}://{request.host}"
        form_url = f"{base_url}/credential-collection/{session_id}"
        status_url = f"{base_url}/v1/credential-collection/sessions/{session_id}"
        credential_key = normalized_payload.get("credential_key")
        if self._is_credential_deletion_request(normalized_payload):
            response_message = "Open the URL and confirm credential deletion directly with the secure broker."
        else:
            response_message = "Open the URL and submit the requested credential fields directly to the secure broker."
        self._logger.info(
            "Created credential collection session. event=credential_collection_session_created "
            "credential_key=%s session_id_prefix=%s expires_at=%s",
            credential_key,
            session_id[:12],
            expires_at.isoformat(),
        )
        return web.json_response(
            {
                "status": "created",
                "credential_key": credential_key,
                "session_id": session_id,
                "form_url": form_url,
                "status_url": status_url,
                "expires_at": expires_at.isoformat(),
                "message": response_message,
            }
        )

    async def _handle_create_credential_collection_session(
        self, request: web.Request
    ) -> web.Response:
        payload = await self._read_json_payload(request)
        request_payload = dict(payload.get("request") or payload)
        context_payload = (
            payload.get("context") if isinstance(payload.get("context"), dict) else {}
        )
        tool_context = ToolContext(
            agent_id=str(context_payload.get("agent_id") or "") or None,
            task_id=str(context_payload.get("task_id") or "") or None,
            conversation_id=str(context_payload.get("conversation_id") or "") or None,
            canonical_user_id=str(context_payload.get("canonical_user_id") or "")
            or None,
            team_id=str(context_payload.get("team_id") or "") or None,
            principal_type=str(context_payload.get("principal_type") or "") or None,
            reason="gateway credential collection",
        )
        if not tool_context.canonical_user_id or tool_context.principal_type != "user":
            raise web.HTTPForbidden(
                text="A trusted registered user context is required."
            )
        return self._create_credential_collection_session_response(
            request=request,
            request_payload=request_payload,
            tool_context=tool_context,
        )

    def _get_credential_collection_session(
        self, session_id: str
    ) -> CredentialCollectionSession:
        self._prune_credential_collection_sessions()
        session = self._credential_collection_sessions.get(session_id)
        if session is None:
            raise web.HTTPNotFound(
                text="Credential collection session not found or expired."
            )
        return session

    async def _handle_credential_collection_session_status(
        self, request: web.Request
    ) -> web.Response:
        session_id = str(request.match_info["session_id"] or "").strip()
        session = self._get_credential_collection_session(session_id)
        return web.json_response(
            {
                "session_id": session_id,
                "status": session.status,
                "used": session.used,
                "credential_key": session.request_payload.get("credential_key"),
                "expires_at": session.expires_at.isoformat(),
                "result": self._credential_collection_public_result(session),
            }
        )

    @staticmethod
    def _credential_form_field_is_secret(spec: dict[str, Any]) -> bool:
        kind = str(spec.get("kind") or "").strip().lower()
        visibility = str(spec.get("visibility") or "").strip().lower()
        return visibility == "secret" or kind in {
            "password",
            "token",
            "api_key",
            "secret",
            "totp",
            "credential_file",
        }

    @staticmethod
    def _render_credential_deletion_form(session: CredentialCollectionSession) -> str:
        request_payload = session.request_payload
        service_name = str(
            request_payload.get("service_name")
            or request_payload.get("display_name")
            or "credential"
        )
        credential_key = str(request_payload.get("credential_key") or "")
        source = str(
            request_payload.get("source")
            or request_payload.get("credential_source")
            or request_payload.get("store_target")
            or ""
        ).strip()
        source_item = str(
            request_payload.get("source_item_id")
            or request_payload.get("source_item_name")
            or request_payload.get("item_id")
            or request_payload.get("item_name")
            or ""
        ).strip()
        source_item_kind = str(
            request_payload.get("source_item_kind")
            or request_payload.get("item_kind")
            or ("credential" if source_item else "")
        ).strip()
        source_html = (
            f"<p>Credential source: <code>{html.escape(source)}</code></p>"
            if source
            else ""
        )
        source_item_html = (
            f"<p>Source item: <code>{html.escape(source_item)}</code></p>"
            if source_item and source_item != credential_key
            else ""
        )
        delete_target_html = (
            f"<p>Delete target type: <code>{html.escape(source_item_kind)}</code></p>"
            if source_item_kind
            else ""
        )
        countdown_html = render_session_countdown(session.expires_at)
        body = f"""
  <section class="content-panel">
    <p class="eyebrow">Secure Credential Deletion</p>
    <h1>Delete credential for {html.escape(service_name)}.</h1>
    <p class="lede">This form will delete the selected credential from the secure broker store after you confirm below. Agents receive only redacted deletion metadata.</p>
  </section>
  <section class="content-panel">
    <p>Credential key: <code>{html.escape(credential_key)}</code></p>
    {source_html}
    {source_item_html}
    {delete_target_html}
    {countdown_html}
    <form method="post">
      <div class="note">
        <strong>This will delete the credential.</strong>
        <p>Any catalog metadata for this credential will also be removed when present.</p>
      </div>
      <label class="broker-confirmation">
        <input type="checkbox" name="confirm_delete" value="yes" required>
        <span>I understand. Proceed.</span>
      </label>
      <button type="submit" style="border-color:#b91c1c;background:#dc2626;color:#ffffff;">Delete</button>
    </form>
  </section>"""
        return render_broker_auth_page(
            "Secure Credential Deletion", body, script=SECRET_INPUT_TOGGLE_SCRIPT
        )

    @staticmethod
    def _render_credential_collection_form(session: CredentialCollectionSession) -> str:
        if SecureToolBrokerService._is_credential_deletion_request(
            session.request_payload
        ):
            return SecureToolBrokerService._render_credential_deletion_form(session)
        request_payload = session.request_payload
        service_name = str(
            request_payload.get("service_name")
            or request_payload.get("display_name")
            or "service"
        )
        credential_key = str(request_payload.get("credential_key") or "")
        countdown_html = render_session_countdown(session.expires_at)
        signup_html = _render_manual_signup_action(
            request_payload,
            service_name=service_name,
        )
        inputs: list[str] = []
        for spec in SecureToolBrokerService._credential_field_specs(request_payload):
            name = str(spec.get("name") or spec.get("field_ref") or "").strip()
            if not name:
                continue
            kind = str(spec.get("kind") or "custom").strip()
            label = str(spec.get("label") or name.replace("_", " ").title()).strip()
            value = str(spec.get("value") or "")
            value_source = str(spec.get("value_source") or "").strip().lower()
            is_secret = SecureToolBrokerService._credential_form_field_is_secret(spec)
            if is_secret and value_source == "store_generated_secret":
                inputs.append(
                    '<div class="note">'
                    f"<strong>{html.escape(label)}</strong> will be generated inside the credential store. "
                    "The generated value is never shown to the agent."
                    "</div>"
                )
                continue
            form_spec = spec.get("form") if isinstance(spec.get("form"), dict) else {}
            input_control = (
                str(
                    form_spec.get("input_control")
                    or ("password" if is_secret else "text")
                )
                .strip()
                .lower()
            )
            if input_control not in {"text", "password", "textarea", "file"}:
                input_control = "password" if is_secret else "text"
            input_type = (
                "password"
                if input_control == "password" or is_secret and input_control == "text"
                else "text"
            )
            autocomplete = str(
                form_spec.get("autocomplete")
                or ("new-password" if is_secret else "username")
            ).strip()
            required = " required" if bool(spec.get("required", True)) else ""
            help_text = str(
                form_spec.get("help_text") or spec.get("description") or ""
            ).strip()
            help_html = (
                render_markdown_text(help_text, class_name="muted") if help_text else ""
            )
            if input_control == "textarea":
                inputs.append(
                    "<div>"
                    f'<label for="field__{html.escape(name)}">{html.escape(label)}'
                    f' <span class="muted">({html.escape(kind)})</span></label>'
                    f'<textarea id="field__{html.escape(name)}" name="field__{html.escape(name)}" '
                    f'autocomplete="{html.escape(autocomplete)}"{required}>{html.escape(value)}</textarea>'
                    f"{help_html}</div>"
                )
                continue
            if input_control == "file":
                accept = ",".join(
                    str(item)
                    for item in form_spec.get("accept", [])
                    if str(item).strip()
                )
                accept_attr = f' accept="{html.escape(accept)}"' if accept else ""
                inputs.append(
                    "<div>"
                    f'<label for="field__{html.escape(name)}">{html.escape(label)}'
                    f' <span class="muted">({html.escape(kind)})</span></label>'
                    f'<input id="field__{html.escape(name)}" name="field__{html.escape(name)}" '
                    f'type="file" autocomplete="off"{accept_attr}{required}>'
                    f"{help_html}</div>"
                )
                continue
            inputs.append(
                "<div>"
                f'<label for="field__{html.escape(name)}">{html.escape(label)}'
                f' <span class="muted">({html.escape(kind)})</span></label>'
                + render_credential_input(
                    input_id=f"field__{name}",
                    input_type=input_type,
                    autocomplete=autocomplete,
                    value=value,
                    placeholder=str(spec.get("placeholder") or "").strip(),
                    required=bool(required),
                )
                + f"{help_html}</div>"
            )
        body = f"""
  <section class="content-panel">
    <p class="eyebrow">Secure Credential Collection</p>
    <h1>Submit credentials for {html.escape(service_name)}.</h1>
    <p class="lede">This form sends sensitive fields directly to the secure broker. Agents receive only redacted completion metadata.</p>
    {signup_html}
  </section>
  <section class="content-panel">
    <p>Credential key: <code>{html.escape(credential_key)}</code></p>
    {countdown_html}
    <form method="post" enctype="multipart/form-data">
      {"".join(inputs)}
      <button type="submit">Store Credential</button>
    </form>
  </section>"""
        return render_broker_auth_page(
            "Secure Credential Collection", body, script=SECRET_INPUT_TOGGLE_SCRIPT
        )

    async def _handle_credential_collection_form(
        self, request: web.Request
    ) -> web.Response:
        session_id = str(request.match_info["session_id"] or "").strip()
        session = self._get_credential_collection_session(session_id)
        if session.used:
            return web.Response(
                text=self._credential_collection_success_html(session),
                content_type="text/html",
            )
        _require_credential_collection_submission_available(session)
        return web.Response(
            text=self._render_credential_collection_form(session),
            content_type="text/html",
        )

    @staticmethod
    def _credential_collection_success_html(
        session: CredentialCollectionSession,
    ) -> str:
        result = (
            SecureToolBrokerService._credential_collection_public_result(session) or {}
        )
        status = str(
            result.get("content", {}).get("status")
            if isinstance(result.get("content"), dict)
            else session.status
        )
        if SecureToolBrokerService._is_credential_deletion_request(
            session.request_payload
        ):
            body = (
                '<section class="content-panel">'
                '<p class="eyebrow">Secure Credential Deletion</p>'
                "<h1>Credential deletion complete.</h1>"
                f'<p class="lede">Status: <code>{html.escape(status or session.status)}</code>. '
                "You can close this browser tab.</p>"
                f"{render_close_page_button()}"
                "</section>"
            )
            return render_broker_auth_page("Credential Deletion Complete", body)
        body = (
            '<section class="content-panel">'
            '<p class="eyebrow">Secure Credential Collection</p>'
            "<h1>Credential submission complete.</h1>"
            f'<p class="lede">Status: <code>{html.escape(status or session.status)}</code>. '
            "You can close this browser tab.</p>"
            f"{render_close_page_button()}"
            "</section>"
        )
        return render_broker_auth_page("Credential Submission Complete", body)

    async def _handle_submit_credential_collection_form(
        self, request: web.Request
    ) -> web.Response:
        session_id = str(request.match_info["session_id"] or "").strip()
        session = self._get_credential_collection_session(session_id)
        if session.used:
            return web.Response(
                text=self._credential_collection_success_html(session),
                content_type="text/html",
            )
        _require_credential_collection_submission_available(session)
        form = await request.post()
        if self._is_credential_deletion_request(session.request_payload):
            if str(form.get("confirm_delete") or "").strip().lower() not in {
                "yes",
                "true",
                "1",
                "on",
            }:
                body = (
                    '<section class="content-panel">'
                    '<p class="eyebrow">Secure Credential Deletion</p>'
                    "<h1>Confirmation required.</h1>"
                    '<p class="lede">Check "I understand. Proceed." before deleting this credential.</p>'
                    "</section>"
                )
                return web.Response(
                    text=render_broker_auth_page(
                        "Credential Deletion Confirmation Required", body
                    ),
                    content_type="text/html",
                    status=400,
                )
            _claim_credential_collection_submission(
                session,
                prune_expired=self._prune_credential_collection_sessions,
            )
            result = await _run_claimed_form_thread(
                session,
                delete_credential,
                session.request_payload,
                context=session.tool_context,
                scope=(
                    CredentialOwnerScope.team(session.tool_context.team_id or "")
                    if session.request_payload.get("owner_scope_kind") == "team"
                    else CredentialOwnerScope.user(
                        session.tool_context.canonical_user_id or ""
                    )
                ),
            )
            content = dict(result.content or {})
            session.result = {"status": result.status, "content": content}
            if result.error:
                session.result["error"] = redact_text(result.error)
            session.status = str(
                content.get("status")
                or (
                    "credential_deleted"
                    if result.status == "success"
                    else "credential_unavailable"
                )
            )
            session.used = result.status == "success"
            self._logger.info(
                "Credential deletion form submitted. event=credential_deletion_submitted credential_key=%s "
                "status=%s session_id_prefix=%s",
                session.request_payload.get("credential_key"),
                session.status,
                session_id[:12],
            )
            if result.status != "success":
                details = redact_text(result.error or "Credential deletion failed.")
                self._logger.warning(
                    "Credential deletion form failed. event=credential_deletion_failed credential_key=%s "
                    "status=%s session_id_prefix=%s error=%s content=%s",
                    session.request_payload.get("credential_key"),
                    session.status,
                    session_id[:12],
                    details,
                    redact_json(content),
                )
                body = (
                    '<section class="content-panel">'
                    '<p class="eyebrow">Secure Credential Deletion</p>'
                    "<h1>Credential deletion failed.</h1>"
                    f"{render_markdown_text(details, class_name='lede')}"
                    "</section>"
                )
                return web.Response(
                    text=render_broker_auth_page("Credential Deletion Failed", body),
                    content_type="text/html",
                    status=400,
                )
            return web.Response(
                text=self._credential_collection_success_html(session),
                content_type="text/html",
            )
        submitted_fields: dict[str, str] = {}
        for key, value in form.items():
            if not str(key).startswith("field__"):
                continue
            field_name = str(key).removeprefix("field__")
            if isinstance(value, web.FileField):
                raw_bytes = value.file.read()
                if len(raw_bytes) > self._credential_collection_max_file_bytes():
                    raise web.HTTPRequestEntityTooLarge(
                        max_size=self._credential_collection_max_file_bytes(),
                        actual_size=len(raw_bytes),
                    )
                try:
                    submitted_fields[field_name] = raw_bytes.decode("utf-8")
                except UnicodeDecodeError as exc:
                    raise web.HTTPBadRequest(
                        text="Uploaded credential files must be UTF-8 text."
                    ) from exc
            else:
                submitted_fields[field_name] = str(value)
        _claim_credential_collection_submission(
            session,
            prune_expired=self._prune_credential_collection_sessions,
        )
        result = await _run_claimed_form_thread(
            session,
            collect_credential,
            session.request_payload,
            submitted_fields,
            context=session.tool_context,
            scope=(
                CredentialOwnerScope.team(session.tool_context.team_id or "")
                if session.request_payload.get("owner_scope_kind") == "team"
                else CredentialOwnerScope.user(
                    session.tool_context.canonical_user_id or ""
                )
            ),
        )
        content = dict(result.content or {})
        if result.status == "success" and str(
            session.request_payload.get("flow")
            or session.request_payload.get("request_kind")
            or ""
        ).lower() in {
            "manual_signup",
            "signup_manual_fallback",
            "manual_signup_fallback",
        }:
            content["status"] = "manual_signup_collected"
        session.result = {"status": result.status, "content": content}
        if result.error:
            session.result["error"] = redact_text(result.error)
        session.status = str(
            content.get("status")
            or (
                "credential_stored"
                if result.status == "success"
                else "credential_unavailable"
            )
        )
        session.used = result.status == "success"
        self._logger.info(
            "Credential collection form submitted. event=credential_collection_submitted credential_key=%s "
            "status=%s session_id_prefix=%s",
            session.request_payload.get("credential_key"),
            session.status,
            session_id[:12],
        )
        if result.status != "success":
            details = redact_text(result.error or "Credential collection failed.")
            body = (
                '<section class="content-panel">'
                '<p class="eyebrow">Secure Credential Collection</p>'
                "<h1>Credential submission failed.</h1>"
                f"{render_markdown_text(details, class_name='lede')}"
                "</section>"
            )
            return web.Response(
                text=render_broker_auth_page("Credential Submission Failed", body),
                content_type="text/html",
                status=400,
            )
        return web.Response(
            text=self._credential_collection_success_html(session),
            content_type="text/html",
        )

    async def _handle_auth_status(self, request: web.Request) -> web.Response:
        payload = await self._read_json_payload(request)
        context_payload = (
            payload.get("context") if isinstance(payload.get("context"), dict) else {}
        )
        canonical_user_id = str(context_payload.get("canonical_user_id") or "").strip()
        if (
            not canonical_user_id
            or str(context_payload.get("principal_type") or "") != "user"
        ):
            raise web.HTTPForbidden(
                text="A trusted registered user context is required."
            )
        provider = request.match_info.get("provider")
        if provider:
            status = await asyncio.to_thread(
                self._run_as_owner,
                canonical_user_id,
                get_auth_status,
                provider,
                team_id=self._access_gate.team_id,
            )
            return web.json_response(status.to_dict())
        statuses = [
            await asyncio.to_thread(
                self._run_as_owner,
                canonical_user_id,
                get_auth_status,
                name,
                team_id=self._access_gate.team_id,
            )
            for name in AUTH_STATUS_PROVIDERS
        ]
        return web.json_response(
            {"providers": {status.provider: status.to_dict() for status in statuses}}
        )

    async def _handle_bitwarden_server_config(
        self, request: web.Request
    ) -> web.Response:
        payload = await self._read_json_payload(request)
        context_payload = (
            payload.get("context") if isinstance(payload.get("context"), dict) else {}
        )
        canonical_user_id = str(context_payload.get("canonical_user_id") or "").strip()
        if (
            not canonical_user_id
            or str(context_payload.get("principal_type") or "") != "user"
        ):
            raise web.HTTPForbidden(
                text="A trusted registered user context is required."
            )
        server_url = str(payload.get("server_url") or payload.get("url") or "").strip()
        if not server_url:
            raise web.HTTPBadRequest(text="server_url is required.")
        result = await asyncio.to_thread(
            self._run_as_owner,
            canonical_user_id,
            set_bitwarden_server_url,
            server_url,
        )
        body: dict[str, Any] = {
            "status": result.status,
            "content": dict(result.content or {}),
        }
        if result.error:
            body["error"] = result.error
        if result.status != "success":
            return web.json_response(body, status=400)
        self._logger.info(
            "Bitwarden CLI server URL configured. event=bitwarden_server_configured server_url=%s",
            body["content"].get("server_url"),
        )
        return web.json_response(body)

    async def _handle_create_auth_session(self, request: web.Request) -> web.Response:
        self._prune_auth_sessions()
        provider = str(request.match_info["provider"] or "").strip().lower()
        payload = await self._read_json_payload(request)
        context_payload = (
            payload.get("context") if isinstance(payload.get("context"), dict) else {}
        )
        canonical_user_id = str(context_payload.get("canonical_user_id") or "").strip()
        if (
            not canonical_user_id
            or str(context_payload.get("principal_type") or "") != "user"
        ):
            raise web.HTTPForbidden(
                text="A trusted registered user context is required."
            )
        team_id = _auth_session_team(context_payload, self._access_gate.team_id)
        person_roles = tuple(
            str(role) for role in context_payload.get("person_roles") or () if str(role).strip()
        )
        requested_method = (
            str(
                payload.get("method")
                or payload.get("flow")
                or request.query.get("method")
                or ""
            )
            .strip()
            .lower()
        )
        method = normalize_auth_method(
            provider, requested_method or DEFAULT_AUTH_METHODS.get(provider, "")
        )
        flow = get_auth_flow(provider, method)
        form_methods = FORM_AUTH_METHODS.get(provider, set())
        form_supported = (
            provider in INTERACTIVE_AUTH_FORM_PROVIDERS and method in form_methods
        )
        if flow is None and not form_supported:
            available_methods = sorted(
                set(form_methods) | set(available_auth_flow_methods(provider))
            )
            status = await asyncio.to_thread(
                self._run_as_owner,
                canonical_user_id,
                get_auth_status,
                provider,
                team_id=self._access_gate.team_id,
            )
            return web.json_response(
                {
                    "status": "unsupported",
                    "provider": provider,
                    "method": method or None,
                    "available_methods": available_methods,
                    "message": f"Interactive broker auth is not implemented for {provider}.",
                    "auth_status": status.to_dict(),
                },
                status=400,
            )
        _require_method_authority(provider, method, canonical_user_id, person_roles)
        now = datetime.now(timezone.utc)
        # A flow may keep its page open longer than a sign-in (the model key pages: 24 hours).
        expires_at = now + timedelta(seconds=getattr(flow, "page_seconds", SIGN_IN_PAGE_SECONDS))
        safe_payload = _session_payload_for_storage(payload)
        session_id = self._sign_session_payload(
            {
                "provider": provider,
                "method": method,
                "expires_at": expires_at.isoformat(),
                "nonce": secrets.token_urlsafe(16),
                "payload": safe_payload,
                "owner_canonical_user_id": canonical_user_id,
                "owner_team_id": team_id,
                "owner_roles": list(person_roles),
            }
        )
        session = BrokerAuthSession(
            provider=provider,
            method=method,
            created_at=now,
            expires_at=expires_at,
            owner_canonical_user_id=canonical_user_id,
            owner_team_id=team_id,
            owner_roles=person_roles,
            state={"start_payload": safe_payload, "status": "created"},
        )
        self._auth_sessions[session_id] = session
        self._logger.info(
            "Created broker auth session. event=broker_auth_session_created provider=%s method=%s "
            "flow_kind=%s session_id_format=signed_v1 session_id_prefix=%s expires_at=%s",
            provider,
            method,
            getattr(flow, "kind", "form") if flow is not None else "form",
            session_id[:12],
            expires_at.isoformat(),
        )
        if flow is not None and getattr(flow, "kind", "") != "external_browser":
            self._run_as_owner(
                canonical_user_id, flow.start, session, payload, self._logger
            )
        base_url = f"{request.scheme}://{request.host}"
        login_url = f"{base_url}/auth/{provider}/{session_id}"
        return web.json_response(
            {
                "status": "created",
                "provider": provider,
                "method": method,
                "flow_kind": getattr(flow, "kind", "form")
                if flow is not None
                else "form",
                "login_url": login_url,
                "expires_at": expires_at.isoformat(),
                "message": f"Open the URL and complete {provider} broker authentication.",
            }
        )

    async def _read_json_payload(self, request: web.Request) -> dict[str, Any]:
        if not request.can_read_body:
            return {}
        try:
            payload = await request.json()
        except (UnicodeDecodeError, ValueError) as exc:
            self._logger.warning(
                "Rejected malformed broker JSON request. "
                "event=broker_malformed_json method=%s path=%s content_type=%s "
                "error_type=%s",
                request.method,
                request.path,
                request.content_type,
                type(exc).__name__,
            )
            raise web.HTTPBadRequest(
                text="Request body must contain a valid JSON object."
            ) from exc
        if not isinstance(payload, dict):
            self._logger.warning(
                "Rejected non-object broker JSON request. "
                "event=broker_non_object_json method=%s path=%s content_type=%s",
                request.method,
                request.path,
                request.content_type,
            )
            raise web.HTTPBadRequest(
                text="Request body must contain a valid JSON object."
            )
        return dict(payload)

    @staticmethod
    def _run_as_owner(owner_id: str, callback: Any, *args: Any, **kwargs: Any) -> Any:
        with credential_owner_context(owner_id):
            return callback(*args, **kwargs)

    def _get_auth_session(self, provider: str, session_id: str) -> BrokerAuthSession:
        """The open sign-in page; a used one-time form answers 410."""

        session = self._find_auth_session(provider, session_id)
        if session.used:
            raise web.HTTPGone(text="Auth session has already been used.")
        return session

    def _find_auth_session(self, provider: str, session_id: str) -> BrokerAuthSession:
        """The sign-in page, used or not (its status route reports how a used one ended)."""

        self._prune_auth_sessions()
        session = self._auth_sessions.get(session_id)
        if session is None:
            session = self._restore_signed_auth_session(provider, session_id)
        if session is None or session.provider != provider:
            reason = self._auth_session_lookup_failure_reason(provider, session_id)
            self._logger.warning(
                "Broker auth session lookup failed. event=broker_auth_session_lookup_failed "
                "provider=%s reason=%s session_id_prefix=%s active_sessions=%s",
                provider,
                reason,
                session_id[:12],
                len(self._auth_sessions),
            )
            raise web.HTTPNotFound(
                text=self._auth_session_lookup_failure_text(provider, reason)
            )
        return session

    def _ensure_flow_started(self, session: BrokerAuthSession, flow: Any) -> None:
        if session.process is not None or session.state.get("flow_started"):
            return
        session.state["flow_started"] = True
        start_payload = dict(session.state.get("start_payload") or {})
        self._run_as_owner(
            session.owner_canonical_user_id,
            flow.start,
            session,
            start_payload,
            self._logger,
        )

    async def _handle_auth_form(self, request: web.Request) -> web.Response:
        provider = str(request.match_info["provider"] or "").strip().lower()
        session_id = str(request.match_info["session_id"] or "").strip()
        session = self._get_auth_session(provider, session_id)
        _require_auth_form_available(session)
        flow = get_auth_flow(provider, session.method)
        if flow is not None:
            self._ensure_flow_started(session, flow)
            return web.Response(text=flow.render(session), content_type="text/html")
        if provider not in INTERACTIVE_AUTH_FORM_PROVIDERS:
            raise web.HTTPBadRequest(text=f"Unsupported auth provider: {provider}")
        if provider == "github":
            body = self._github_auth_form(session)
        elif provider == "google":
            body = self._google_auth_form(session)
        else:
            body = self._bitwarden_auth_form(session)
        return web.Response(text=body, content_type="text/html")

    async def _handle_x_sign_in_callback(self, request: web.Request) -> web.Response:
        """X sends the browser here after the person approved, when the team's X app names this page.

        The one-time ``state`` finds the open sign-in (``XWebSignInFlow``); an
        address that matches none gets a plain page and changes nothing.
        """

        self._prune_auth_sessions()
        flow = get_auth_flow(X_SIGN_IN_PROVIDER, "web")
        state = str(request.query.get("state") or "")
        session = next(
            (
                candidate
                for candidate in list(self._auth_sessions.values())
                if candidate.provider == X_SIGN_IN_PROVIDER and flow.matches(candidate, state)
            ),
            None,
        )
        if session is None:
            self._logger.warning(
                "X sign-in callback matched no open sign-in. event=x_sign_in_callback_unmatched state_present=%s",
                bool(state),
            )
            body = (
                '<section class="content-panel"><p class="eyebrow">X</p><h1>This X sign-in is not open.</h1>'
                f"<p>It finished, expired or was started on another broker. Start again with "
                f"{html.escape(sign_in_command(X_SIGN_IN_PROVIDER))}.</p></section>"
            )
            return web.Response(text=render_broker_auth_page("X sign-in", body), content_type="text/html", status=404)
        _claim_auth_form_submission(session, prune_expired=self._prune_auth_sessions)
        await _run_claimed_form_thread(
            session,
            self._run_as_owner,
            session.owner_canonical_user_id,
            flow.complete,
            session,
            dict(request.query),
            self._logger,
        )
        return web.Response(text=flow.render(session), content_type="text/html")

    async def _handle_auth_session_status(self, request: web.Request) -> web.Response:
        """How a sign-in page stands: the page's own script and the gateway's /link watcher read it.

        One-time form pages (a pasted GitHub token, a gog token export) record
        ``success`` or ``error`` when submitted (``_handle_submit_auth_form``),
        so this route still answers after the form was used.
        """

        provider = str(request.match_info["provider"] or "").strip().lower()
        session_id = str(request.match_info["session_id"] or "").strip()
        session = self._find_auth_session(provider, session_id)
        flow = get_auth_flow(provider, session.method)
        if flow is not None:
            return web.json_response(flow.status_payload(session))
        status = str(session.state.get("status") or "")
        return web.json_response(
            {
                "provider": session.provider,
                "method": session.method,
                "status": status if status in {"success", "error"} else "pending",
                "expires_at": session.expires_at.isoformat(),
            }
        )

    @staticmethod
    def _github_auth_form(session: BrokerAuthSession) -> str:
        countdown_html = render_session_countdown(session.expires_at)
        body = f"""
  <section class="content-panel">
    <p class="eyebrow">GitHub</p>
    <h1>Authenticate broker-owned GitHub CLI access.</h1>
    <p class="lede">This form submits a GitHub token directly to the secure tool broker. The token is not returned to the agent.</p>
  </section>
  <section class="content-panel">
    <div class="note">For GitHub Projects v2 board access, the token must include <code>read:project</code> along with repository and organization scopes.</div>
    {countdown_html}
    <form method="post">
      <div>
        <label for="token">GitHub token</label>
        {render_credential_input(input_id="token", input_type="password", autocomplete="off", required=True)}
      </div>
      <div>
        <label for="hostname">Hostname</label>
        <input id="hostname" name="hostname" type="text" value="github.com">
      </div>
      <button type="submit">Authenticate GitHub CLI</button>
    </form>
  </section>"""
        return render_broker_auth_page(
            "GitHub Broker Auth", body, script=SECRET_INPUT_TOGGLE_SCRIPT
        )

    @staticmethod
    def _auth_failure_details(result: ToolResult) -> dict[str, str]:
        content = dict(result.content or {})
        details: dict[str, str] = {}
        error = redact_text(str(result.error or "Unknown error")).strip()
        if error:
            details["error"] = error
        for key in (
            "provider",
            "action",
            "error_code",
            "exit_code",
            "command",
            "stderr",
            "stdout",
            "stdout_bytes",
            "stderr_bytes",
            "stdout_lines",
            "stderr_lines",
        ):
            raw_value = content.get(key)
            if raw_value in (None, ""):
                continue
            if key == "command" and isinstance(raw_value, (list, tuple)):
                rendered = " ".join(str(part) for part in raw_value)
            elif isinstance(raw_value, (dict, list, tuple)):
                rendered = json.dumps(
                    redact_json(raw_value), sort_keys=True, default=str
                )
            else:
                rendered = str(raw_value)
            rendered = redact_text(rendered).strip()
            if rendered:
                details[key] = rendered
        for detail_key in (
            "form_diagnostics",
            "bw_auth_diagnostics",
            "bw_status_before",
            "bw_status_after",
            "fallback_attempts",
        ):
            raw_value = content.get(detail_key)
            if not raw_value:
                continue
            rendered = json.dumps(redact_json(raw_value), sort_keys=True, default=str)
            rendered = redact_text(rendered).strip()
            if rendered:
                details[detail_key] = rendered
        return details

    @staticmethod
    def _auth_failure_html(display_name: str, details: dict[str, str]) -> str:
        error = details.get("error") or "Unknown error"
        rows = []
        for label, key in (
            ("Provider", "provider"),
            ("Action", "action"),
            ("Error code", "error_code"),
            ("Exit code", "exit_code"),
            ("Command", "command"),
            ("stderr", "stderr"),
            ("stdout", "stdout"),
            ("stdout bytes", "stdout_bytes"),
            ("stderr bytes", "stderr_bytes"),
            ("Form diagnostics", "form_diagnostics"),
            ("Bitwarden auth diagnostics", "bw_auth_diagnostics"),
            ("bw status before auth", "bw_status_before"),
            ("bw status after auth", "bw_status_after"),
            ("Fallback attempts", "fallback_attempts"),
        ):
            value = details.get(key)
            if value:
                rows.append(
                    f"<dt>{html.escape(label)}</dt>"
                    f"<dd><pre>{html.escape(value)}</pre></dd>"
                )
        detail_html = f"<dl>{''.join(rows)}</dl>" if rows else ""
        body = (
            '<section class="content-panel">'
            f'<p class="eyebrow">{html.escape(display_name)}</p>'
            f"<h1>{html.escape(display_name)} authentication failed.</h1>"
            f"{render_markdown_text(error, class_name='lede')}"
            "</section>"
            f'<section class="content-panel">{detail_html}</section>'
        )
        return render_broker_auth_page(f"{display_name} authentication failed", body)

    @staticmethod
    def _google_auth_form(session: BrokerAuthSession) -> str:
        countdown_html = render_session_countdown(session.expires_at)
        if session.method == GOOGLE_TEAM_CLIENT_METHOD:
            return _google_client_form(session)
        body = f"""
  <section class="content-panel">
    <p class="eyebrow">Google</p>
    <h1>Import your Google sign-in.</h1>
    <p class="lede">This form submits a gog token export JSON directly to the secure tool broker. The broker stores it as yours and uses it only for requests made for you; it is not returned to the agent.</p>
  </section>
  <section class="content-panel">
    <h2>How to get the JSON</h2>
    <ol class="steps">
      <li class="step">For an External OAuth app, open Google Cloud Console &gt; APIs &amp; Services &gt; OAuth consent screen, click <strong>Publish App</strong>, and confirm the publishing status is <strong>In Production</strong>. Tokens issued while the app is in Testing expire after 7 days.</li>
      <li class="step">On a trusted machine with <code>gog</code> installed, run <code>gog auth add you@example.com --services=all --force-consent</code> and complete the Google login flow only after publishing.</li>
      <li class="step">Export the stored refresh token: <code>gog auth tokens export you@example.com --out gog-token.json --overwrite</code></li>
      <li class="step">Open <code>gog-token.json</code> locally, copy the entire JSON payload, and paste it below.</li>
      <li class="step">The token works only with the OAuth client that issued it. If that is not the team's app, paste that client's JSON below too; the broker stores it as your own app.</li>
      <li class="step">The broker imports the token in a private folder, checks it with Google (<code>gog auth list --check</code>), and stores it in the credential store.</li>
    </ol>
    <div class="note">Prefer <code>/link cli:google</code> to complete the browser-based manual flow through this broker page. Import only a token that was minted by the matching OAuth client; its lifetime is determined when Google issues it, so importing a Testing-mode token cannot make it durable.</div>
    {countdown_html}
    <form method="post">
      <div>
        <label for="token_json">gog token JSON</label>
        <textarea id="token_json" name="token_json" required rows="12" autocomplete="off"></textarea>
      </div>
      <div>
        <label for="credentials_json">OAuth client ID JSON, optional when the token was issued through the team's app</label>
        <textarea id="credentials_json" name="credentials_json" rows="8" autocomplete="off"></textarea>
      </div>
      <button type="submit">Import Google Tokens</button>
    </form>
  </section>"""
        return render_broker_auth_page("Google Broker Auth", body)

    @staticmethod
    def _bitwarden_auth_form(session: BrokerAuthSession) -> str:
        try:
            configured_server_url = bitwarden_server_url_from_env()
        except ValueError:
            configured_server_url = None
        configured_server_html = (
            f"<p>Current broker-side Bitwarden server URL: <code>{html.escape(configured_server_url)}</code></p>"
            if configured_server_url
            else ""
        )
        configured_user_timezone = _configured_user_timezone_from_env()
        countdown_html = render_session_countdown(session.expires_at)
        body = f"""
  <section class="content-panel">
    <p class="eyebrow">Bitwarden</p>
    <h1>Authenticate broker-owned Bitwarden CLI access.</h1>
    <p class="lede">This form logs in and unlocks Bitwarden inside the secure broker. The agent never receives the API key, master password, or session value.</p>
  </section>
  <section class="content-panel">
    {configured_server_html}
    <div class="note">For self-hosted Bitwarden or Vaultwarden, enter the server URL below or configure <code>security_layer.bitwarden_server_url</code> for the broker.</div>
    <h2>Broker-side login and unlock</h2>
    <ol class="steps">
      <li class="step">Paste a Bitwarden personal API key client ID and client secret, unless <code>BW_CLIENTID</code> and <code>BW_CLIENTSECRET</code> are already configured in the broker environment.</li>
      <li class="step">Enter the vault master password. It is sent only to the broker and used once to run <code>bw unlock --passwordenv BW_PASSWORD --raw</code>; it is not stored.</li>
      <li class="step">The broker logs in with <code>bw login --apikey</code> if needed, unlocks the vault in the broker-side Bitwarden CLI state directory, verifies with <code>bw status</code>, and stores only the resulting session.</li>
      <li class="step">Recommended current path: choose <strong>Save for Restart Unlock</strong>, then restart the broker or container. The broker saves a short-lived pending unlock request under persisted broker credentials, completes login during the next broker startup, and deletes the pending request after session verification.</li>
    </ol>
    <div class="note">Use <strong>Save for Restart Unlock</strong> for the current Bitwarden CLI workaround. <strong>Authenticate Bitwarden</strong> remains available as a diagnostic fallback, but may hit the known empty-output behavior in the web-request process.</div>
    {countdown_html}
    <form method="post">
      <input id="user_timezone" name="user_timezone" type="hidden" value="{html.escape(configured_user_timezone)}" data-user-timezone>
      <div>
        <label for="server_url">Bitwarden server URL, optional</label>
        <input id="server_url" name="server_url" type="url" autocomplete="off" placeholder="https://vault.example.com">
      </div>
      <fieldset>
        <legend>Broker-side API key login and unlock</legend>
        <div>
          <label for="client_id">API key client ID, optional if configured in broker env</label>
          {render_credential_input(input_id="client_id", input_type="password", autocomplete="off")}
        </div>
        <div>
          <label for="client_secret">API key client secret, optional if configured in broker env</label>
          {render_credential_input(input_id="client_secret", input_type="password", autocomplete="off")}
        </div>
        <div>
          <label for="master_password">Master password for one-time unlock</label>
          {render_credential_input(input_id="master_password", input_type="password", autocomplete="current-password", required=True)}
        </div>
      </fieldset>
      <div class="page-actions">
        <button type="submit" name="submit_mode" value="defer_restart">Save for Restart Unlock</button>
        <button class="secondary-action" type="submit" name="submit_mode" value="authenticate">Authenticate Bitwarden</button>
      </div>
    </form>
  </section>"""
        return render_broker_auth_page(
            "Bitwarden Broker Auth", body, script=SECRET_INPUT_TOGGLE_SCRIPT
        )

    async def _handle_submit_auth_form(self, request: web.Request) -> web.Response:
        provider = str(request.match_info["provider"] or "").strip().lower()
        session_id = str(request.match_info["session_id"] or "").strip()
        session = self._get_auth_session(provider, session_id)
        _require_auth_form_available(session)
        flow = get_auth_flow(provider, session.method)
        if flow is not None:
            submit = getattr(flow, "submit", None)
            if submit is None:
                raise web.HTTPMethodNotAllowed(
                    method=request.method,
                    allowed_methods=("GET",),
                    text="This auth flow is completed through the external browser page.",
                )
            form = await request.post()
            _claim_auth_form_submission(
                session,
                prune_expired=self._prune_auth_sessions,
            )
            await _run_claimed_form_thread(
                session,
                self._run_as_owner,
                session.owner_canonical_user_id,
                submit,
                session,
                form,
                self._logger,
            )
            return web.Response(text=flow.render(session), content_type="text/html")
        if provider not in INTERACTIVE_AUTH_FORM_PROVIDERS:
            raise web.HTTPBadRequest(text=f"Unsupported auth provider: {provider}")
        form = await request.post()
        _claim_auth_form_submission(
            session,
            prune_expired=self._prune_auth_sessions,
        )
        user_timezone = str(form.get("user_timezone") or "").strip()
        if provider == "github":
            token = str(form.get("token") or "")
            hostname = str(form.get("hostname") or "").strip() or None
            result = await _run_claimed_form_thread(
                session,
                self._run_as_owner,
                session.owner_canonical_user_id,
                login_github_with_token,
                token,
                hostname=hostname,
            )
        elif provider == "google":
            result = await _submit_google_form(session, form, team_id=self._access_gate.team_id)
        else:
            server_url = str(form.get("server_url") or "").strip()
            master_password = str(form.get("master_password") or "")
            client_id = str(form.get("client_id") or "").strip()
            client_secret = str(form.get("client_secret") or "").strip()
            submit_mode = str(form.get("submit_mode") or "authenticate").strip()
            bitwarden_form_diagnostics = {
                "method": session.method,
                "submit_mode": submit_mode,
                "user_timezone_present": bool(user_timezone),
                "server_url_field_present": bool(server_url),
                "server_url_field_chars": len(server_url),
                "master_password_present": bool(master_password),
                "master_password_chars": len(master_password),
                "master_password_has_surrounding_whitespace": master_password
                != master_password.strip(),
                "client_id_field_present": bool(client_id),
                "client_id_field_chars": len(client_id),
                "client_secret_field_present": bool(client_secret),
                "client_secret_field_chars": len(client_secret),
                "env_bitwarden_server_url_present": bool(
                    os.environ.get("BITWARDEN_SERVER_URL")
                ),
                "env_bw_server_url_present": bool(os.environ.get("BW_SERVER_URL")),
                "env_bw_clientid_present": bool(os.environ.get("BW_CLIENTID")),
                "env_bw_clientsecret_present": bool(os.environ.get("BW_CLIENTSECRET")),
                "env_bw_session_present": bool(os.environ.get("BW_SESSION")),
            }
            self._logger.info(
                "bitwarden broker auth form submitted: method=%s submit_mode=%s server_url_field_present=%s "
                "master_password_present=%s master_password_chars=%s client_id_field_present=%s "
                "client_secret_field_present=%s env_bitwarden_server_url_present=%s "
                "env_bw_clientid_present=%s env_bw_clientsecret_present=%s env_bw_session_present=%s",
                bitwarden_form_diagnostics["method"],
                bitwarden_form_diagnostics["submit_mode"],
                bitwarden_form_diagnostics["server_url_field_present"],
                bitwarden_form_diagnostics["master_password_present"],
                bitwarden_form_diagnostics["master_password_chars"],
                bitwarden_form_diagnostics["client_id_field_present"],
                bitwarden_form_diagnostics["client_secret_field_present"],
                bitwarden_form_diagnostics["env_bitwarden_server_url_present"],
                bitwarden_form_diagnostics["env_bw_clientid_present"],
                bitwarden_form_diagnostics["env_bw_clientsecret_present"],
                bitwarden_form_diagnostics["env_bw_session_present"],
            )
            if submit_mode == "defer_restart":
                result = await _run_claimed_form_thread(
                    session,
                    self._run_as_owner,
                    session.owner_canonical_user_id,
                    save_bitwarden_restart_unlock_request,
                    master_password=master_password,
                    client_id=client_id,
                    client_secret=client_secret,
                    server_url=server_url,
                )
                if result.status == "success":
                    content = dict(result.content or {})
                    self._logger.info(
                        "bitwarden pending restart unlock saved: event=bitwarden_pending_restart_unlock_saved "
                        "pending_file=%s expires_at_epoch=%s server_url_present=%s client_id_present=%s client_secret_present=%s",
                        content.get("pending_file", "-"),
                        content.get("expires_at_epoch", "-"),
                        content.get("server_url_present", "-"),
                        content.get("client_id_present", "-"),
                        content.get("client_secret_present", "-"),
                    )
            else:
                result = await _run_claimed_form_thread(
                    session,
                    self._run_as_owner,
                    session.owner_canonical_user_id,
                    login_bitwarden_with_api_key,
                    master_password=master_password,
                    client_id=client_id,
                    client_secret=client_secret,
                    server_url=server_url,
                )
            if result.status != "success":
                content = dict(result.content or {})
                content["form_diagnostics"] = bitwarden_form_diagnostics
                result = ToolResult(
                    status=result.status, content=content, error=result.error
                )
        session.used = True
        # Read by the status route, which the gateway's /link watcher polls.
        session.state["status"] = "success" if result.status == "success" else "error"
        display_name = AUTH_PROVIDER_DISPLAY_NAMES.get(provider, provider.title())
        if result.status != "success":
            details = self._auth_failure_details(result)
            self._logger.warning(
                "%s broker auth form failed: error=%s error_code=%s action=%s exit_code=%s command=%s "
                "stdout_bytes=%s stderr_bytes=%s stderr=%s stdout=%s form_diagnostics=%s "
                "bw_auth_diagnostics=%s bw_status_before=%s bw_status_after=%s fallback_attempts=%s",
                provider,
                details.get("error", "-"),
                details.get("error_code", "-"),
                details.get("action", "-"),
                details.get("exit_code", "-"),
                details.get("command", "-"),
                details.get("stdout_bytes", "-"),
                details.get("stderr_bytes", "-"),
                details.get("stderr", "-"),
                details.get("stdout", "-"),
                details.get("form_diagnostics", "-"),
                details.get("bw_auth_diagnostics", "-"),
                details.get("bw_status_before", "-"),
                details.get("bw_status_after", "-"),
                details.get("fallback_attempts", "-"),
            )
            return web.Response(
                text=self._auth_failure_html(display_name, details),
                content_type="text/html",
                status=400,
            )
        if (
            provider == "bitwarden"
            and result.status == "success"
            and str((result.content or {}).get("action") or "")
            == "pending_restart_unlock.saved"
        ):
            content = dict(result.content or {})
            expires_at = _format_epoch_seconds_for_display(
                content.get("expires_at_epoch"), user_timezone=user_timezone
            )
            body = (
                '<section class="content-panel">'
                '<p class="eyebrow">Bitwarden</p>'
                "<h1>Bitwarden restart unlock saved.</h1>"
                '<p class="lede">Restart the broker or container now. On startup, the broker will complete Bitwarden login, verify the session, write <code>BW_SESSION_FILE</code>, and delete the pending request.</p>'
                f"<p>Pending file: <code>{html.escape(str(content.get('pending_file') or ''))}</code></p>"
                f"<p>Expires at: <code>{html.escape(expires_at)}</code></p>"
                "</section>"
            )
            return web.Response(
                text=render_broker_auth_page("Bitwarden restart unlock saved", body),
                content_type="text/html",
            )
        if provider == "google" and session.method == GOOGLE_TEAM_CLIENT_METHOD:
            body = (
                '<section class="content-panel">'
                '<p class="eyebrow">Google</p>'
                "<h1>The team's Google sign-in app is stored.</h1>"
                f'<p class="lede">People who have not signed in yet run <code>{sign_in_command("google")}</code>. '
                "A sign-in made through a different app must be made again.</p>"
                f"{render_close_page_button()}"
                "</section>"
            )
            return web.Response(
                text=render_broker_auth_page("Google sign-in app stored", body),
                content_type="text/html",
            )
        self._logger.info("%s broker auth form completed successfully.", provider)
        success_body = (
            '<section class="content-panel">'
            f'<p class="eyebrow">{html.escape(display_name)}</p>'
            f"<h1>{html.escape(display_name)} authentication complete.</h1>"
            '<p class="lede">You can close this browser tab and retry the agent task.</p>'
            f"{render_close_page_button()}"
            "</section>"
        )
        return web.Response(
            text=render_broker_auth_page(
                f"{display_name} authentication complete", success_body
            ),
            content_type="text/html",
        )


def resolve_bind_from_base_url(base_url: str | None) -> tuple[str, int]:
    parsed = urlparse(base_url or "")
    if parsed.hostname and parsed.port:
        host = parsed.hostname
        if host in {"localhost", "127.0.0.1", "::1"}:
            host = DEFAULT_BIND_HOST
        return host, int(parsed.port)
    return DEFAULT_BIND_HOST, DEFAULT_BIND_PORT


async def run_secure_tool_broker(
    *,
    host: str,
    port: int,
    token: str,
    access_gate: BrokerAccessGate,
    mcp_config: BrokerMCPConfig | None = None,
    tool_setup_recheck_days: int = 0,
) -> None:
    # A token left in the environment would be a team-wide credential for a
    # per-person tool; refuse to start and name the sign-in that replaces it.
    reject_removed_tool_credential_env_vars()
    bitwarden_server_url = bitwarden_server_url_from_env()
    if bitwarden_server_url:
        config_result = await asyncio.to_thread(
            ensure_bitwarden_server_configured_from_env
        )
        if config_result is not None:
            details = dict(config_result.content or {})
            LOGGER.warning(
                "Bitwarden CLI server URL could not be configured at broker startup. "
                "event=bitwarden_server_config_failed server_url=%s error=%s exit_code=%s command=%s stderr=%s stdout=%s",
                bitwarden_server_url,
                config_result.error,
                details.get("exit_code", "-"),
                details.get("command", "-"),
                details.get("stderr", "-"),
                details.get("stdout", "-"),
            )
        else:
            LOGGER.info(
                "Configured Bitwarden CLI server URL from broker environment: %s",
                bitwarden_server_url,
            )
    pending_bitwarden_result = await asyncio.to_thread(
        process_pending_bitwarden_restart_unlock
    )
    if pending_bitwarden_result is not None:
        pending_details = redact_text(
            json.dumps(
                redact_json(dict(pending_bitwarden_result.content or {})),
                sort_keys=True,
                default=str,
            )
        )
        if pending_bitwarden_result.status == "success":
            LOGGER.info(
                "Bitwarden pending restart unlock completed. "
                "event=bitwarden_pending_restart_unlock_completed details=%s",
                pending_details,
            )
        else:
            LOGGER.warning(
                "Bitwarden pending restart unlock failed. "
                "event=bitwarden_pending_restart_unlock_failed error=%s details=%s",
                redact_text(str(pending_bitwarden_result.error or "Unknown error")),
                pending_details,
            )
    else:
        LOGGER.info(
            "Bitwarden pending restart unlock check found no pending request. "
            "event=bitwarden_pending_restart_unlock_not_found credentials_dir_present=%s "
            "bitwardencli_appdata_dir_present=%s",
            bool(os.environ.get("SECURE_TOOL_BROKER_CREDENTIALS_DIR")),
            bool(os.environ.get("BITWARDENCLI_APPDATA_DIR")),
        )
    service = SecureToolBrokerService(
        token=token,
        access_gate=access_gate,
        mcp_config=mcp_config,
    )
    hosted_site = await start_aiohttp_site(
        service.build_app(),
        host=host,
        port=port,
        access_log=None,
        web_module=web,
    )
    LOGGER.info(
        "Secure tool broker listening on %s auth_session_format=signed_v1 module_path=%s",
        hosted_site.base_url,
        __file__,
    )
    try:
        # Runs forever: the periodic re-check of ready tool setups
        # (security_layer.tool_setup_recheck_days; 0 only sleeps).
        await run_rechecks(access_gate, every_days=tool_setup_recheck_days)
    finally:
        await hosted_site.cleanup()
