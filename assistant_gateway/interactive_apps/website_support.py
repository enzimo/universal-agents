from __future__ import annotations
import hashlib
import logging
import re
from runtime_settings.instance_layout import InstanceLayout
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from aiohttp import web
from assistant_gateway.interactive_apps.host_ports import (
    InternalWebsiteHostEnvironment,
    InteractiveAppRuntimeContext,
)
from id_generation import new_id
from secure_tools.chat_commands import ACCOUNTS_COMMAND, LINK_COMMAND, SETUP_COMMAND
from user_docs.published_sites import PublishedSiteStore

"""Local website host for generated single-page assistant artifacts."""

LOGGER = logging.getLogger(__name__)


def require_website_runtime(
    runtime: InteractiveAppRuntimeContext | None,
) -> InteractiveAppRuntimeContext:
    """Fail explicitly if a host exposes runtime routes before binding services."""
    if runtime is None:
        raise web.HTTPServiceUnavailable(
            text="Web Chat runtime services are unavailable."
        )
    return runtime


_CLIENT_ID_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")

_WEB_CHAT_ROUTE = "/webchat"

_WEB_CHAT_SESSION_COOKIE = "ua_web_chat_session"

_WEB_CHAT_SESSION_TTL_SECONDS = 12 * 60 * 60

_WEB_CHAT_CSP = "; ".join(
    (
        "default-src 'self'",
        "base-uri 'none'",
        "connect-src 'self'",
        "font-src 'self'",
        "form-action 'self'",
        "frame-src 'self'",
        "frame-ancestors 'none'",
        "img-src 'self' data: blob:",
        "media-src 'self' blob:",
        "object-src 'none'",
        "script-src 'self'",
        # Existing chat canvas and tooltip positioning use narrow dynamic style
        # attributes. Agent-provided content is escaped before markup rendering.
        "style-src 'self' 'unsafe-inline'",
    )
)

_WORKSPACE_HTML_PREVIEW_CSP = "; ".join(
    (
        "sandbox",
        "default-src 'none'",
        "base-uri 'none'",
        "connect-src 'none'",
        "font-src data:",
        "form-action 'none'",
        "frame-src 'none'",
        "frame-ancestors 'self'",
        "img-src data:",
        "media-src 'none'",
        "object-src 'none'",
        "script-src 'none'",
        "style-src 'unsafe-inline'",
    )
)

_WEB_CHAT_ALLOWED_COMMANDS = {
    "/help",
    "/docs",
    "/tour",
    "/auth",
    LINK_COMMAND,
    ACCOUNTS_COMMAND,
    SETUP_COMMAND,
    "/access",
    "/user",
    "/new",
    "/objectives",
    "/pause",
    "/resume",
    "/stop",
    "/abandon",
    "/delete",
    "/schedules",
    "/mcp",
    "/disable",
}

_WEB_CHAT_COMMAND_NOT_AVAILABLE = (
    "This command is not available in Web Chat. Use /help for supported commands."
)

REPO_ROOT = Path(__file__).resolve().parents[2]

ONBOARDING_ASSET_ROOT = (
    Path(__file__).resolve().parents[1] / "onboarding_web" / "assets"
)

DOCKER_COMPOSE_SETUP_PATH = REPO_ROOT / "docker-compose-setup.html"

WEB_CHAT_MODULE_ASSETS = (
    "chat_submission.mjs",
    "workflow.mjs",
    "workflow_graph.mjs",
    "workflow_card.mjs",
    "workflow.css",
    "browser_lifecycle.mjs",
    "chat_appearance.mjs",
    "chat_authentication.mjs",
    "chat_assistant_name.mjs",
    "chat_sign_in.mjs",
    "chat_diagnostics.mjs",
    "chat_runtime_views.mjs",
    "chat_live_events.mjs",
    "chat_attention.mjs",
    "chat_links.mjs",
    "chat_access_review.mjs",
    "chat_transcript_view.mjs",
    "access_inbox.mjs",
    "access_map.mjs",
    "accounts_view.mjs",
    "workspace_file_actions.mjs",
    "settings_support.mjs",
    "settings_controls.mjs",
    "team_controls.mjs",
    "file_retention_controls.mjs",
    "memory_controls.mjs",
    "status_page.mjs",
    "knowledge_page.mjs",
    "knowledge_render.mjs",
    "knowledge_navigation.mjs",
    "activity_mesh.mjs",
    "team_activity.mjs",
)
_CONFIG_EDITOR_ASSETS = {
    *WEB_CHAT_MODULE_ASSETS,
    "agent-mark.png",
    "favicon.ico",
    "styles.css",
    "appearance.js",
    "appearance_customizer.js",
    "metallic_mesh.js",
    "mesh_motion.js",
    "glass_mesh.js",
    "ui_motion.js",
    "intro_word.js",
    "panel_motion.js",
    "chat.js",
    "chat_delivery.js",
    "workspace_browser.js",
    "chat_markdown.js",
    "operational_ids.js",
    "credential_controls.js",
    "action_feedback.js",
    "task_controls.js",
    "pagination.js",
    "config-editor.js",
}


def _operator_ui_asset_version() -> str:
    """Return one content revision shared by the trusted operator UI bundle."""
    digest = hashlib.sha256()
    for asset_name in sorted(_CONFIG_EDITOR_ASSETS):
        digest.update(asset_name.encode("utf-8"))
        digest.update((ONBOARDING_ASSET_ROOT / asset_name).read_bytes())
    return digest.hexdigest()[:16]


def _render_operator_ui_shell(shell_name: str) -> str:
    """Render a shell whose local assets bypass older browser cache entries."""
    markup = (ONBOARDING_ASSET_ROOT / shell_name).read_text(encoding="utf-8")
    revision = _operator_ui_asset_version()
    for asset_name in sorted(_CONFIG_EDITOR_ASSETS):
        markup = markup.replace(
            f'"/{asset_name}"',
            f'"/{asset_name}?v={revision}"',
        )
    return markup


def _load_public_version() -> str:
    version_path = REPO_ROOT / "VERSION"
    try:
        version = version_path.read_text(encoding="utf-8").strip()
    except OSError:
        LOGGER.warning(
            "Unable to read the public version for Web Chat",
            extra={"version_path": str(version_path)},
            exc_info=True,
        )
        return ""
    if not version:
        LOGGER.warning(
            "Public version file is empty; Web Chat will omit the version footer",
            extra={"version_path": str(version_path)},
        )
    return version


PUBLIC_VERSION = _load_public_version()


@dataclass(frozen=True)
class WebChatSession:
    expires_at: float
    auth_kind: str
    permission_csrf_token: str
    canonical_user_id: str | None = None
    display_name: str | None = None
    credential_kind: str = "bootstrap_token"
    enrollment_required: bool = False
    enrollment_label: str = ""
    pending_token_id: str | None = None


def resolve_internal_website_published_sites(
    environment: InternalWebsiteHostEnvironment,
) -> PublishedSiteStore:
    """Page records in state/published_sites; page files in people's folders.

    Both are written only by publish_single_page_site; the host serves a page
    only through its record (user_docs.published_sites).
    """

    return PublishedSiteStore(
        records_dir=InstanceLayout.at(environment.instance_root_path).published_sites_dir,
        workspaces_dir=Path(environment.workspace_path),
    )


def _safe_client_id(raw_value: Any) -> str:
    candidate = str(raw_value or "").strip()
    if candidate and _CLIENT_ID_PATTERN.match(candidate):
        return candidate
    return new_id("browser")


def _payload_bool(value: Any, *, default: bool = False) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in {"1", "true", "yes", "on"}
