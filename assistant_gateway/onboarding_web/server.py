"""Production web onboarding service."""

from __future__ import annotations

import asyncio
import copy
from dataclasses import dataclass
import logging
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import threading
import time
from typing import Any, Awaitable, Callable
from urllib import parse as urllib_parse
import uuid

import yaml
from aiohttp import web
from pydantic import ValidationError

from assistant_gateway.config import GatewayInstanceConfig
from runtime_settings.network import compose_public_base_url
from assistant_gateway.interactive_apps.builtin_registry import (
    build_interactive_app_registry,
)
from assistant_gateway.interactive_apps.catalog import build_interactive_app_catalog
from runtime_hosting.aiohttp import AiohttpSite, start_aiohttp_site
from assistant_gateway.onboarding_account_keys import onboarding_sharing_services
from assistant_gateway.onboarding_engine import (
    OnboardingWriteOptions,
    load_base_onboarding_template,
    normalize_onboarding_answers,
    write_onboarded_instance,
)
from assistant_gateway.onboarding_web.provider_test import test_provider_connection
from assistant_gateway.onboarding_web.settings_import import (
    ImportedSettings,
    OnboardingImportError,
    read_onboarding_import,
)
from assistant_gateway.onboarding_web.team_import import TeamImport
from runtime_settings.instance_layout import InstanceLayout
from runtime_settings.secrets_source import SECRETS_SOURCE_ENVIRONMENT, normalize_secrets_source
from custom_agents.agent_catalog import (
    get_onboarding_agent_catalog,
    resolve_agent_definition_paths,
)
from custom_agents.external_skills import (
    ExternalSkillsSyncError,
    MAX_EXTERNAL_SKILL_REPOSITORIES,
    normalize_external_skill_repository_urls,
    sync_external_skill_repositories,
    validate_external_skill_repository_url,
)
from custom_agents.xai_oauth_auth import (
    DEFAULT_XAI_OAUTH_MODEL,
    XAIDeviceAuthorization,
    XAIOAuthError,
    XAIOAuthSettings,
    is_xai_oauth_provider,
    poll_xai_device_token,
    request_xai_device_authorization,
    resolve_xai_oauth_settings,
    save_xai_oauth_device_tokens,
)
from default_values import (
    GATEWAY_DEFAULTS,
    LLM_DEFAULTS,
    NETWORK_DEFAULTS,
    UI_DEFAULTS,
    VOICE_DEFAULTS,
)


LOGGER = logging.getLogger(__name__)
ASSET_ROOT = Path(__file__).resolve().parent / "assets"
REPO_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_BASE_CONFIG = REPO_ROOT / "config.example.yaml"
MAX_JSON_BYTES = 512 * 1024
GIT_COMMAND_TIMEOUT_SECONDS = 30
MAX_SKILLS_REPOSITORIES = MAX_EXTERNAL_SKILL_REPOSITORIES
MAX_XAI_DEVICE_LOGIN_SESSIONS = 8
XAI_DEVICE_LOGIN_RETENTION_SECONDS = 10 * 60
MIN_PHOTON_NODE_MAJOR = 24
NODE_VERSION_CHECK_TIMEOUT_SECONDS = 2


def _provider_endpoint_log_fields(endpoint: str) -> tuple[str, str]:
    """Return useful endpoint fields without logging credentials or query values."""

    try:
        parsed = urllib_parse.urlsplit(endpoint)
        return parsed.hostname or "", parsed.path or "/"
    except ValueError:
        return "", ""


def nodejs_available() -> bool:
    node_bin = shutil.which("node")
    if node_bin is None:
        return False
    try:
        completed = subprocess.run(
            [node_bin, "--version"],
            check=False,
            capture_output=True,
            text=True,
            timeout=NODE_VERSION_CHECK_TIMEOUT_SECONDS,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        LOGGER.warning(
            "Node.js availability probe failed node_bin=%s error=%s",
            node_bin,
            exc,
        )
        return False
    if completed.returncode != 0:
        LOGGER.warning(
            "Node.js availability probe exited nonzero node_bin=%s returncode=%s",
            node_bin,
            completed.returncode,
        )
        return False
    try:
        major = int(completed.stdout.strip().removeprefix("v").split(".", 1)[0])
    except ValueError:
        LOGGER.warning(
            "Node.js availability probe returned an invalid version node_bin=%s version=%r",
            node_bin,
            completed.stdout.strip(),
        )
        return False
    return major >= MIN_PHOTON_NODE_MAJOR


@dataclass(frozen=True)
class OnboardingWebSettings:
    host: str = "0.0.0.0"
    port: int = 18090
    instance_root: str = "/data"
    base_config: str | None = None
    base_env_file: str | None = None
    restart_on_complete: bool = True


@dataclass
class XAIDeviceLoginSession:
    login_id: str
    authorization: XAIDeviceAuthorization
    settings: XAIOAuthSettings
    instance_root: str
    cancel_event: threading.Event
    task: asyncio.Task[None] | None = None
    status: str = "pending"
    message: str = "Waiting for approval in the xAI authorization page."
    error_code: str | None = None
    relogin_required: bool = False
    finished_at_epoch_seconds: float | None = None


def normalize_skills_repository_urls(raw_repositories: Any) -> list[str]:
    if not isinstance(raw_repositories, list):
        return []
    repositories: list[str] = []
    seen: set[str] = set()
    for raw_url in raw_repositories:
        url = str(raw_url or "").strip()
        if not url or url in seen:
            continue
        seen.add(url)
        repositories.append(url)
        if len(repositories) >= MAX_SKILLS_REPOSITORIES:
            break
    return repositories


def validate_skill_repositories(repositories: list[str]) -> list[dict[str, Any]]:
    return [_validate_skill_repository(url) for url in repositories[:MAX_SKILLS_REPOSITORIES]]


def merge_team_skills_repositories(repositories: list[str], config: GatewayInstanceConfig) -> list[dict[str, Any]]:
    destination = _resolve_runtime_skills_dir(config)
    if not repositories:
        return []
    try:
        result = sync_external_skill_repositories(repositories, destination)
    except (ExternalSkillsSyncError, ValueError) as exc:
        return [
            {
                "url": "",
                "ok": False,
                "copied": 0,
                "message": str(exc),
                "skills_dir": str(destination),
            }
        ]
    return [repository.as_dict(destination=destination) for repository in result.repositories]


def _validate_skill_repository(url: str) -> dict[str, Any]:
    validation = _validate_repo_url(url)
    if validation is not None:
        return {"url": url, "ok": False, "message": validation}
    result = _run_git_command(["git", "ls-remote", url])
    return {"url": url, "ok": result["ok"], "message": result["message"]}


def _validate_repo_url(url: str) -> str | None:
    try:
        validate_external_skill_repository_url(url)
    except ValueError as exc:
        return str(exc)
    return None


def _run_git_command(command: list[str]) -> dict[str, Any]:
    try:
        completed = subprocess.run(
            command,
            check=False,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            timeout=GIT_COMMAND_TIMEOUT_SECONDS,
        )
    except FileNotFoundError:
        return {"ok": False, "message": "git is not installed in this environment."}
    except subprocess.TimeoutExpired:
        return {"ok": False, "message": "Timed out while contacting the repository."}
    if completed.returncode == 0:
        return {"ok": True, "message": "Repository is reachable."}
    message = (completed.stderr or completed.stdout or "Repository could not be reached.").strip().splitlines()
    return {"ok": False, "message": message[-1] if message else "Repository could not be reached."}


def _resolve_runtime_skills_dir(config: GatewayInstanceConfig) -> Path:
    return config.layout.skills_dir


def _friendly_validation_message(exc: ValidationError) -> tuple[str, list[str]]:
    details: list[str] = []
    for error in exc.errors():
        loc = ".".join(str(part) for part in error.get("loc", ()) if part != "__root__")
        message = str(error.get("msg") or "Invalid configuration.")
        if message.startswith("Value error, "):
            message = message.removeprefix("Value error, ")
        details.append(f"{loc}: {message}" if loc else message)
    joined = "\n".join(details)
    if (
        "interactive_apps.plugins.builtin.signal.settings.account is required"
        in joined
    ):
        return (
            "Signal is enabled, but the Account field is empty. Return to Communication > Signal and enter the registered Signal account or turn Signal off.",
            details,
        )
    return (
        "Configuration could not be finalized. Review the validation details below.",
        details,
    )


class OnboardingWebService:
    def __init__(
        self,
        settings: OnboardingWebSettings,
        *,
        logger: logging.Logger | None = None,
    ) -> None:
        self.settings = settings
        self.logger = logger or LOGGER
        base_config = settings.base_config
        if base_config is None and DEFAULT_BASE_CONFIG.is_file():
            base_config = str(DEFAULT_BASE_CONFIG)
        self.template = load_base_onboarding_template(base_config)
        self.completion_event: asyncio.Event | None = None
        self.last_completion: dict[str, Any] | None = None
        self.xai_device_logins: dict[str, XAIDeviceLoginSession] = {}
        # Let's Begin > Import a team export (assistant_gateway.onboarding_web.team_import).
        self.team_import = TeamImport(
            self._instance_layout, on_imported=self._record_team_import, logger=self.logger
        )

    def _effective_public_origin(self, configured_origin: str | None = None) -> str:
        return (
            os.getenv("NETWORK_PUBLIC_ORIGIN", "").strip()
            or str(configured_origin or "").strip()
            or NETWORK_DEFAULTS.public_origin.value
        )

    def _effective_internal_website_public_port(
        self,
        configured_port: int | None,
    ) -> int | None:
        raw_public_port = os.getenv("INTERNAL_WEBSITE_PUBLIC_PORT")
        if raw_public_port is None:
            return configured_port
        if not raw_public_port.strip():
            return None
        try:
            return int(raw_public_port)
        except ValueError:
            self.logger.warning(
                "Ignoring invalid INTERNAL_WEBSITE_PUBLIC_PORT=%r; using configured port %r.",
                raw_public_port,
                configured_port,
            )
            return configured_port

    def onboarding_browser_url(self) -> str:
        public_origin = self._effective_public_origin(
            self.template.network.public_origin,
        )
        public_port = self._effective_internal_website_public_port(self.settings.port)
        try:
            return compose_public_base_url(public_origin, public_port) + "/"
        except ValueError:
            self.logger.warning(
                "Ignoring invalid public onboarding URL settings origin=%r public_port=%r.",
                public_origin,
                public_port,
            )
            return compose_public_base_url(NETWORK_DEFAULTS.public_origin.value, self.settings.port) + "/"

    def build_app(self) -> web.Application:
        app = web.Application(
            client_max_size=MAX_JSON_BYTES,
            middlewares=[self._xai_device_no_store_middleware],
        )
        app.router.add_get("/", self._handle_index)
        app.router.add_get("/health", self._handle_health)
        app.router.add_get("/api/bootstrap", self._handle_bootstrap)
        app.router.add_get("/api/status", self._handle_status)
        app.router.add_post("/api/provider-test", self._handle_provider_test)
        app.router.add_post("/api/xai-oauth/device-login", self._handle_xai_device_login_start)
        app.router.add_get("/api/xai-oauth/device-login/{login_id}", self._handle_xai_device_login_status)
        app.router.add_post("/api/onboarding/skills-repos/validate", self._handle_validate_skills_repos)
        app.router.add_post("/api/onboarding/import-config", self._handle_import_config)
        app.router.add_post("/api/onboarding/complete", self._handle_complete)
        app.router.add_post("/api/onboarding/restart", self._handle_restart)
        app.router.add_get("/download/instance_config.yaml", self._handle_download_instance_config)
        app.router.add_get("/download/team.env", self._handle_download_team_env)
        self.team_import.add_routes(app)
        app.router.add_get("/{path:.*}", self._handle_asset)
        app.on_cleanup.append(self._cleanup_xai_device_logins)
        app.on_cleanup.append(self.team_import.close)
        return app

    @web.middleware
    async def _xai_device_no_store_middleware(
        self,
        request: web.Request,
        handler: Callable[[web.Request], Awaitable[web.StreamResponse]],
    ) -> web.StreamResponse:
        if not request.path.startswith("/api/xai-oauth/device-login"):
            return await handler(request)
        try:
            response = await handler(request)
        except web.HTTPException as exc:
            exc.headers["Cache-Control"] = "no-store"
            raise
        response.headers["Cache-Control"] = "no-store"
        return response

    async def _handle_index(self, _request: web.Request) -> web.FileResponse:
        return web.FileResponse(
            ASSET_ROOT / "index.html",
            headers={"Cache-Control": "no-store"},
        )

    async def _handle_asset(self, request: web.Request) -> web.FileResponse:
        raw_path = str(request.match_info.get("path") or "").strip("/")
        if not raw_path:
            return web.FileResponse(
                ASSET_ROOT / "index.html",
                headers={"Cache-Control": "no-store"},
            )
        asset_path = (ASSET_ROOT / raw_path).resolve()
        try:
            asset_path.relative_to(ASSET_ROOT)
        except ValueError as exc:
            raise web.HTTPNotFound(text="Asset not found.") from exc
        if not asset_path.is_file():
            raise web.HTTPNotFound(text="Asset not found.")
        return web.FileResponse(asset_path, headers={"Cache-Control": "no-store"})

    async def _handle_health(self, _request: web.Request) -> web.Response:
        return web.json_response(
            {
                "status": "ok",
                "service": "onboard-web",
                "writes_runtime_files": True,
                "instance_root": str(Path(self.settings.instance_root).expanduser().resolve()),
            }
        )

    async def _handle_bootstrap(self, _request: web.Request) -> web.Response:
        instance_root = str(Path(self.settings.instance_root).expanduser().resolve())
        settings = self._settings_from_template(instance_root=instance_root)
        # A rerun keeps the secrets source the instance already uses.
        existing_source = self._existing_secrets_source()
        if existing_source:
            settings["secrets_source"] = existing_source
        return web.json_response(settings)

    def _existing_secrets_source(self) -> str | None:
        """``launch.secrets_source`` of an instance already at the root, if any."""

        config_path = self._instance_layout().root / "instance_config.yaml"
        if not config_path.is_file():
            return None
        try:
            raw = yaml.safe_load(config_path.read_text(encoding="utf-8")) or {}
            launch = raw.get("launch") if isinstance(raw, dict) else None
            source = launch.get("secrets_source") if isinstance(launch, dict) else None
            return normalize_secrets_source(source) if source else None
        except (OSError, yaml.YAMLError, ValueError) as exc:
            self.logger.warning(
                "Could not read launch.secrets_source from the existing instance config; "
                "the form starts from the base config. event=onboarding_existing_secrets_source_unreadable "
                "path=%s error_type=%s",
                config_path,
                type(exc).__name__,
            )
            return None

    def _instance_layout(self) -> InstanceLayout:
        return InstanceLayout.at(self.settings.instance_root)

    async def _handle_status(self, _request: web.Request) -> web.Response:
        instance_root = Path(self.settings.instance_root).expanduser().resolve()
        config_path = instance_root / "instance_config.yaml"
        env_path = self._instance_layout().team_env_path
        return web.json_response(
            {
                "status": "configured" if config_path.exists() else "onboarding",
                "instance_config_exists": config_path.exists(),
                "team_env_exists": env_path.exists(),
                "completion": self.last_completion,
            }
        )

    async def _handle_provider_test(self, request: web.Request) -> web.Response:
        payload = await self._json_payload(request)
        instance_root = str(Path(self.settings.instance_root).expanduser().resolve())
        raw_xai_oauth = payload.get("xai_oauth") if isinstance(payload.get("xai_oauth"), dict) else None
        if _is_ui_xai_oauth_provider(payload.get("provider")):
            try:
                xai_settings = self._resolve_browser_xai_oauth_settings(raw_xai_oauth)
            except (XAIOAuthError, ValueError) as exc:
                return web.json_response(
                    {
                        "ok": False,
                        "message": str(exc),
                        "code": getattr(exc, "code", "xai_oauth_settings_invalid"),
                    },
                    status=502,
                )
            raw_xai_oauth = {
                "base_url": xai_settings.base_url,
                "auth_store_path": xai_settings.auth_store_path,
                "refresh_skew_seconds": xai_settings.refresh_skew_seconds,
                "refresh_timeout_seconds": xai_settings.refresh_timeout_seconds,
            }
        result = await asyncio.to_thread(
            test_provider_connection,
            provider=str(payload.get("provider") or ""),
            endpoint=str(payload.get("endpoint") or ""),
            token=str(payload.get("token") or ""),
            model=str(payload.get("model") or ""),
            reasoning_effort=str(payload.get("reasoning_effort") or ""),
            reasoning_summary=payload.get("reasoning_summary"),
            litellm_api_mode=str(payload.get("litellm_api_mode") or "chat_completions"),
            stream=payload.get("stream"),
            xai_oauth=raw_xai_oauth,
            instance_root=instance_root,
        )
        if not result.ok:
            endpoint_host, endpoint_path = _provider_endpoint_log_fields(
                str(payload.get("endpoint") or "")
            )
            token = str(payload.get("token") or "")
            safe_message = (
                result.message.replace(token, "[redacted]") if token else result.message
            )
            LOGGER.warning(
                "Provider connection test failed. "
                "event=onboarding_provider_test_failed provider=%s endpoint_host=%s "
                "endpoint_path=%s error=%s",
                result.provider,
                endpoint_host,
                endpoint_path,
                safe_message,
            )
        status = 200 if result.ok else 502
        return web.json_response(
            {
                "status": "ok" if result.ok else "error",
                "provider": result.provider,
                "message": result.message,
                "sample": result.sample,
            },
            status=status,
        )

    async def _handle_xai_device_login_start(self, request: web.Request) -> web.Response:
        payload = await self._json_payload(request)
        self._prune_xai_device_logins()
        active_login_count = sum(
            1 for session in self.xai_device_logins.values() if session.finished_at_epoch_seconds is None
        )
        if active_login_count >= MAX_XAI_DEVICE_LOGIN_SESSIONS:
            return web.json_response(
                {
                    "status": "error",
                    "message": "Too many xAI device logins are already active. Wait for one to finish or expire.",
                },
                status=429,
            )
        raw_settings = (
            dict(payload.get("xai_oauth") or {})
            if isinstance(payload.get("xai_oauth"), dict)
            else dict(LLM_DEFAULTS.xai_oauth.value)
        )
        try:
            settings = self._resolve_browser_xai_oauth_settings(raw_settings)
            instance_root = str(Path(self.settings.instance_root).expanduser().resolve())
            authorization = await asyncio.to_thread(
                request_xai_device_authorization,
                settings,
                instance_root=instance_root,
            )
        except (XAIOAuthError, ValueError) as exc:
            return web.json_response(
                {
                    "status": "error",
                    "message": str(exc),
                    "code": getattr(exc, "code", "xai_oauth_device_start_failed"),
                },
                status=502,
            )

        login_id = uuid.uuid4().hex
        session = XAIDeviceLoginSession(
            login_id=login_id,
            authorization=authorization,
            settings=settings,
            instance_root=instance_root,
            cancel_event=threading.Event(),
        )
        self.xai_device_logins[login_id] = session
        session.task = asyncio.create_task(
            self._run_xai_device_login(session),
            name=f"xai-device-login-{login_id[:8]}",
        )
        return web.json_response(self._xai_device_login_payload(session), status=202)

    def _resolve_browser_xai_oauth_settings(
        self,
        raw_settings: dict[str, Any] | None,
    ) -> XAIOAuthSettings:
        instance_root_path = Path(self.settings.instance_root).expanduser().resolve()
        instance_root = str(instance_root_path)
        settings = resolve_xai_oauth_settings(
            raw_settings,
            instance_root=instance_root,
        )
        auth_store_path = Path(str(settings.auth_store_path)).expanduser().resolve()
        try:
            auth_store_path.relative_to(instance_root_path)
        except ValueError as exc:
            raise XAIOAuthError(
                "Browser onboarding may only use the xAI OAuth auth store inside the configured instance root.",
                code="xai_oauth_store_outside_instance",
            ) from exc
        return settings

    async def _handle_xai_device_login_status(self, request: web.Request) -> web.Response:
        self._prune_xai_device_logins()
        login_id = str(request.match_info.get("login_id") or "").strip()
        session = self.xai_device_logins.get(login_id)
        if session is None:
            raise web.HTTPNotFound(text="xAI device login was not found or has expired.")
        return web.json_response(self._xai_device_login_payload(session))

    async def _run_xai_device_login(self, session: XAIDeviceLoginSession) -> None:
        try:
            token_payload = await asyncio.to_thread(
                poll_xai_device_token,
                session.authorization,
                timeout_seconds=session.settings.refresh_timeout_seconds,
                cancel_event=session.cancel_event,
            )
            await asyncio.to_thread(
                save_xai_oauth_device_tokens,
                session.authorization,
                token_payload,
                session.settings,
                instance_root=session.instance_root,
            )
            session.status = "complete"
            session.message = "xAI OAuth login succeeded. You can test the connection now."
        except asyncio.CancelledError:
            session.cancel_event.set()
            session.status = "cancelled"
            session.message = "xAI device login was cancelled."
            raise
        except XAIOAuthError as exc:
            session.status = "error"
            session.message = str(exc)
            session.error_code = exc.code
            session.relogin_required = exc.relogin_required
        except Exception:
            self.logger.exception("Unexpected xAI OAuth device login failure login_id=%s", session.login_id)
            session.status = "error"
            session.message = "xAI OAuth login failed unexpectedly. Check the onboarding service logs."
            session.error_code = "xai_oauth_device_unexpected"
        finally:
            session.finished_at_epoch_seconds = time.time()

    def _xai_device_login_payload(self, session: XAIDeviceLoginSession) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "status": session.status,
            "login_id": session.login_id,
            "message": session.message,
            "expires_in_seconds": max(
                0,
                int(session.authorization.expires_at_epoch_seconds - time.time()),
            ),
            **session.authorization.public_payload(),
        }
        if session.error_code:
            payload["code"] = session.error_code
            payload["relogin_required"] = session.relogin_required
        return payload

    def _prune_xai_device_logins(self) -> None:
        now = time.time()
        removable: list[str] = []
        for login_id, session in self.xai_device_logins.items():
            if session.finished_at_epoch_seconds is not None:
                if now - session.finished_at_epoch_seconds >= XAI_DEVICE_LOGIN_RETENTION_SECONDS:
                    removable.append(login_id)
            elif now > session.authorization.expires_at_epoch_seconds + 60:
                session.cancel_event.set()
                removable.append(login_id)
        for login_id in removable:
            session = self.xai_device_logins.pop(login_id, None)
            if session and session.task and not session.task.done():
                session.task.cancel()

    async def _cleanup_xai_device_logins(self, _app: web.Application) -> None:
        tasks: list[asyncio.Task[None]] = []
        for session in self.xai_device_logins.values():
            session.cancel_event.set()
            if session.task and not session.task.done():
                session.task.cancel()
                tasks.append(session.task)
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        self.xai_device_logins.clear()

    async def _handle_validate_skills_repos(self, request: web.Request) -> web.Response:
        payload = await self._json_payload(request)
        repositories = normalize_skills_repository_urls(payload.get("repositories"))
        results = await asyncio.to_thread(validate_skill_repositories, repositories)
        ok = all(result.get("ok") for result in results)
        return web.json_response(
            {
                "status": "ok" if ok else "warning",
                "repositories": results,
            },
            status=200 if ok else 400,
        )

    async def _handle_import_config(self, request: web.Request) -> web.Response:
        payload = await self._json_payload(request)
        filename = Path(str(payload.get("filename") or "uploaded.yaml").strip() or "uploaded.yaml").name
        try:
            imported = read_onboarding_import(filename, str(payload.get("content") or ""))
            settings = self._settings_from_import(imported)
        except OnboardingImportError as exc:
            return web.json_response({"status": "error", "message": str(exc)}, status=400)
        except Exception:
            # The import only fills form fields; no file may end the page in a 500.
            self.logger.exception(
                "Settings import failed. event=onboarding_import_failed filename=%s", filename
            )
            return web.json_response(
                {
                    "status": "error",
                    "message": f"{filename} could not be imported. Check that it is a config.yaml or "
                    "instance_config.yaml file, or skip the import and fill in the form.",
                },
                status=400,
            )
        self.logger.info(
            "Imported onboarding settings. event=onboarding_import_read kind=%s imported_count=%s skipped_count=%s",
            imported.kind,
            len(imported.values),
            len(imported.skipped),
        )
        return web.json_response(
            {
                "status": "ok",
                "kind": imported.kind,
                "settings": settings,
                "skipped": [item.as_dict() for item in imported.skipped],
            }
        )

    async def _handle_complete(self, request: web.Request) -> web.Response:
        payload = await self._json_payload(request)
        # The instance always lands in this service's folder; a folder the page
        # still remembers (for example from an older import) never moves it.
        payload["instance_root"] = str(self._instance_layout().root)
        try:
            team_skills_repositories = normalize_external_skill_repository_urls(
                payload.get("team_skills_repositories")
            )
        except ValueError as exc:
            return web.json_response(
                {"status": "error", "message": str(exc)},
                status=400,
            )
        try:
            answers = normalize_onboarding_answers(payload, self.template)
        except ValueError as exc:
            # An answer the page should not send (for example a sharing choice
            # for a service that is always private); say which, plainly.
            self.logger.warning(
                "Onboarding answers were refused. event=onboarding_answers_refused error_type=%s", type(exc).__name__
            )
            return web.json_response({"status": "error", "message": str(exc)}, status=400)
        secrets_from_environment = answers["secrets_source"] == SECRETS_SOURCE_ENVIRONMENT
        source_env_given = bool(str(payload.get("source_env_content") or "").strip() or self.settings.base_env_file)
        source_env_file_path, temp_dir = (
            (None, None) if secrets_from_environment else self._source_env_file(payload)
        )
        try:
            result = write_onboarded_instance(
                answers=answers,
                template=self.template,
                options=OnboardingWriteOptions(
                    source_env_file_path=source_env_file_path,
                    generate_internal_credentials=bool(answers["generate_internal_credentials"]),
                    rotate_internal_credentials=False,
                    overwrite_existing=True,
                ),
            )
        except ValidationError as exc:
            message, details = _friendly_validation_message(exc)
            return web.json_response(
                {
                    "status": "error",
                    "message": message,
                    "details": details,
                },
                status=400,
            )
        finally:
            if temp_dir is not None:
                temp_dir.cleanup()
        result.config.workspace.external_skill_repositories = list(team_skills_repositories)
        result.config.save(result.config.config_path)
        skills_repo_results = await asyncio.to_thread(
            merge_team_skills_repositories,
            team_skills_repositories,
            result.config,
        )
        self.last_completion = {
            "config_path": str(result.config.config_path),
            "env_path": str(result.env_path) if result.env_path is not None else None,
            "report_path": str(result.report.detail_path),
            "skills_repos": skills_repo_results,
            "secrets_source": answers["secrets_source"],
            "missing_secrets": [item.as_dict() for item in result.missing_secrets],
            "unsaved_secret_names": list(result.unsaved_secret_names),
            "source_env_ignored": secrets_from_environment and source_env_given,
        }
        restart = self._restart_allowed() and not bool(payload.get("finalize_only"))
        if restart:
            self._schedule_restart()
        return web.json_response(
            {
                "status": "ok",
                "restart_required": restart,
                **self.last_completion,
            }
        )

    def _source_env_file(
        self, payload: dict[str, Any]
    ) -> tuple[str | None, tempfile.TemporaryDirectory[str] | None]:
        """The .env to merge into team.env: an uploaded one (in a temporary folder) or --base-env-file."""

        content = str(payload.get("source_env_content") or "")
        if not content.strip():
            return self.settings.base_env_file, None
        temp_dir = tempfile.TemporaryDirectory(prefix="ua-onboarding-env-")
        filename = Path(str(payload.get("source_env_filename") or "uploaded.env")).name or "uploaded.env"
        source_env_path = Path(temp_dir.name) / filename
        source_env_path.write_text(content, encoding="utf-8")
        return str(source_env_path), temp_dir

    def _record_team_import(self, completion: dict[str, Any]) -> bool:
        """An Import wrote the instance: finish as Launch does; True when the service restarts."""

        self.last_completion = completion
        restart = self._restart_allowed()
        if restart:
            self._schedule_restart()
        return restart

    def _restart_allowed(self) -> bool:
        """Restart only into a runtime that has every secret it needs."""

        return bool(self.settings.restart_on_complete) and not (self.last_completion or {}).get("missing_secrets")

    def _schedule_restart(self) -> None:
        if self.completion_event is not None:
            asyncio.get_running_loop().call_later(0.5, self.completion_event.set)

    async def _handle_restart(self, _request: web.Request) -> web.Response:
        if (self.last_completion or {}).get("missing_secrets"):
            # Secrets come from the environment and some are missing: the
            # runtime would refuse to start. Adding Docker secrets restarts the
            # container anyway.
            return web.json_response(
                {
                    "status": "error",
                    "message": "Some secrets this configuration needs are not in the environment. "
                    "Add them as Docker secrets and restart the container; the team then starts.",
                    "missing_secrets": self.last_completion["missing_secrets"],  # type: ignore[index]
                },
                status=409,
            )
        if self._restart_allowed() and self.completion_event is not None:
            self._schedule_restart()
            return web.json_response({"status": "ok", "restart_required": True})
        return web.json_response({"status": "ok", "restart_required": False})

    async def _handle_download_instance_config(self, _request: web.Request) -> web.FileResponse:
        config_path = Path(self.settings.instance_root).expanduser().resolve() / "instance_config.yaml"
        if not config_path.is_file():
            raise web.HTTPNotFound(text="instance_config.yaml has not been generated yet.")
        return web.FileResponse(
            config_path,
            headers={"Content-Disposition": 'attachment; filename="instance_config.yaml"'},
        )

    async def _handle_download_team_env(self, _request: web.Request) -> web.FileResponse:
        env_path = self._instance_layout().team_env_path
        if not env_path.is_file():
            raise web.HTTPNotFound(
                text="There is no team.env: it has not been generated yet, or this instance's "
                "secrets come from the environment."
            )
        return web.FileResponse(
            env_path,
            headers={"Content-Disposition": 'attachment; filename="team.env"'},
        )

    async def _json_payload(self, request: web.Request) -> dict[str, Any]:
        try:
            payload = await request.json()
        except Exception as exc:
            raise web.HTTPBadRequest(text="Request body must be JSON.") from exc
        if not isinstance(payload, dict):
            raise web.HTTPBadRequest(text="Request body must be a JSON object.")
        return payload

    def _settings_from_template(self, *, instance_root: str, template: Any | None = None) -> dict[str, Any]:
        template = template or self.template
        appearance = template.ui.onboarding.appearance
        internal_website = template.interactive_apps.internal_website
        telegram = template.interactive_apps.telegram
        signal = template.interactive_apps.signal
        photon = template.interactive_apps.photon
        public_origin = self._effective_public_origin(
            template.network.public_origin,
        )
        internal_website_bind_port = int(internal_website.bind_port)
        internal_website_public_port = self._effective_internal_website_public_port(
            internal_website.public_port,
        )
        internal_website_base_url = compose_public_base_url(
            public_origin,
            internal_website_public_port,
        )
        return {
            "account_sharing_services": onboarding_sharing_services(),
            "interactive_app_catalog": self._interactive_app_catalog_payload(template),
            "network_public_origin": public_origin,
            "documentation_port": int(internal_website_public_port or internal_website_bind_port),
            "internal_website_bind_port": internal_website_bind_port,
            "internal_website_public_port": internal_website_public_port,
            "internal_website_base_url": internal_website_base_url,
            "instance_root": instance_root,
            "secrets_source": template.secrets_source,
            "agent_catalog": self._agent_catalog_payload(
                template.agent.agent_definition_paths,
                instance_root=instance_root,
            ),
            "team_id": template.team_id,
            "timezone": template.timezone,
            "assistant_name": GATEWAY_DEFAULTS.assistant_display_name.value,
            "model_provider": _ui_model_provider_label(
                template.model_provider,
                _ui_model_endpoint(
                    template.model_provider,
                    template.litellm_api_base,
                    template.openai_codex,
                    template.xai_oauth,
                ),
            ),
            "model_endpoint": _ui_model_endpoint(
                template.model_provider,
                template.litellm_api_base,
                template.openai_codex,
                template.xai_oauth,
            ),
            "model_id": (
                template.model_id
                or (
                    DEFAULT_XAI_OAUTH_MODEL
                    if _is_ui_xai_oauth_provider(template.model_provider)
                    else LLM_DEFAULTS.model_id.value
                )
            ),
            "openai_codex": _openai_codex_payload(template.openai_codex),
            "xai_oauth": _xai_oauth_payload(template.xai_oauth),
            "reasoning_effort": template.reasoning_effort or "",
            "reasoning_summary": template.reasoning_summary,
            "litellm_api_mode": template.litellm_api_mode,
            "context_window_tokens": template.llm_context_window_tokens,
            "stream": template.llm_stream,
            "request_timeout_seconds": template.llm_request_timeout_seconds,
            "supports_image_input": template.supports_image_input,
            "supports_audio_input": template.supports_audio_input,
            "supports_file_input": template.supports_file_input,
            "model_capabilities": template.model_capabilities,
            "llm_failover": template.llm_failover,
            "embedding_enabled": bool(template.agent.memory_graph_enabled),
            "embedding": {
                "provider": template.embedding_provider,
                "model_id": template.embedding_model_id,
                "api_base": template.embedding_api_base,
                "dimensions": template.embedding_dimensions,
                "timeout_seconds": template.embedding_timeout_seconds,
                "organization": template.embedding_organization,
            },
            "quiet_enabled": bool(template.delivery.quiet_hours.enabled),
            "quiet_start": int(template.delivery.quiet_hours.start_hour_local),
            "quiet_end": int(template.delivery.quiet_hours.end_hour_local),
            "voice_enabled": bool(template.voice.enabled),
            "voice_response_mode": str(
                getattr(template.voice, "response_mode", VOICE_DEFAULTS.response_mode.value)
            ),
            "security_layer": template.security_layer.model_dump(mode="json"),
            "activity_collector": template.activity_collector.model_dump(mode="json"),
            "nodejs_available": nodejs_available(),
            "internal_website_docs_root": internal_website.docs_root,
            "internal_website_skills_dir": internal_website.skills_dir,
            "telegram_rich_messages": bool(telegram.rich_messages),
            "signal_enabled": bool(signal.enabled),
            "signal_base_url": signal.base_url,
            "signal_account": signal.account,
            "signal_allowed_sender_ids": list(signal.allowed_sender_ids),
            "signal_allowed_group_ids": list(signal.allowed_group_ids),
            "signal_group_policy": signal.group_policy,
            "signal_mention_markers": list(signal.mention_markers),
            "signal_poll_timeout_seconds": signal.poll_timeout_seconds,
            "signal_max_messages_per_poll": signal.max_messages_per_poll,
            "signal_drop_pending_messages_on_startup": bool(signal.drop_pending_messages_on_startup),
            "photon_enabled": bool(photon.enabled),
            "photon_project_id": photon.project_id,
            "photon_allowed_sender_ids": list(photon.allowed_sender_ids),
            "photon_allowed_group_ids": list(photon.allowed_group_ids),
            "photon_group_policy": photon.group_policy,
            "photon_mention_markers": list(photon.mention_markers),
            "appearance": appearance.model_dump(mode="json"),
            "appearance_defaults": self._appearance_defaults(),
        }

    def _settings_from_import(self, imported: ImportedSettings) -> dict[str, Any]:
        """Form settings: this service's defaults with the imported values on top.

        Catalogs, the instance folder and anything the import did not find
        come from this service's own base config; the old instance's folder
        is never sent to the page.
        """

        settings = self._settings_from_template(instance_root=str(self._instance_layout().root))
        settings.pop("instance_root", None)
        values = dict(imported.values)
        provider = values.pop("llm_provider", None)
        litellm_api_base = values.pop("llm_litellm_api_base", None)
        startup_agents = values.pop("startup_agents", None)
        for key, value in values.items():
            # Sections keep this service's defaults for sub-settings the file lacks.
            base = settings.get(key)
            settings[key] = (
                _deep_merged(base, value) if isinstance(base, dict) and isinstance(value, dict) else value
            )
        if provider is not None or litellm_api_base is not None:
            self._apply_imported_model(settings, provider=provider, litellm_api_base=litellm_api_base)
        if startup_agents is not None:
            settings["selected_agents"] = {
                agent["named_agent_type"]: bool(agent["enabled"]) for agent in startup_agents
            }
            settings["selected_agent_counts"] = {
                agent["named_agent_type"]: agent["count"] for agent in startup_agents
            }
        self._apply_imported_website(settings)
        return settings

    def _apply_imported_model(
        self, settings: dict[str, Any], *, provider: Any, litellm_api_base: Any
    ) -> None:
        provider = provider if provider is not None else self.template.model_provider
        endpoint = _ui_model_endpoint(
            provider,
            litellm_api_base if litellm_api_base is not None else self.template.litellm_api_base,
            settings.get("openai_codex"),
            settings.get("xai_oauth"),
        )
        settings["model_provider"] = _ui_model_provider_label(provider, endpoint)
        settings["model_endpoint"] = endpoint
        if not settings.get("model_id") and _is_ui_xai_oauth_provider(provider):
            settings["model_id"] = DEFAULT_XAI_OAUTH_MODEL

    def _apply_imported_website(self, settings: dict[str, Any]) -> None:
        """Recompute the browser-facing website values from the imported origin and ports."""

        public_origin = self._effective_public_origin(settings.get("network_public_origin"))
        public_port = self._effective_internal_website_public_port(settings.get("internal_website_public_port"))
        settings["network_public_origin"] = public_origin
        settings["internal_website_public_port"] = public_port
        try:
            settings["internal_website_base_url"] = compose_public_base_url(public_origin, public_port)
        except ValueError:
            # The page shows the imported origin; finalizing reports it in plain words.
            self.logger.warning(
                "Imported public origin is not a valid http(s) origin. "
                "event=onboarding_import_invalid_public_origin"
            )
        settings["documentation_port"] = public_port or settings.get("internal_website_bind_port")

    @staticmethod
    def _agent_catalog_payload(raw_paths: Any, *, instance_root: str) -> list[dict[str, Any]]:
        paths = resolve_agent_definition_paths(raw_paths, instance_root=instance_root)
        return get_onboarding_agent_catalog(paths, include_configured=False)

    @staticmethod
    def _interactive_app_catalog_payload(config: Any) -> list[dict[str, object]]:
        registry = build_interactive_app_registry(config)
        return [
            entry.as_dict()
            for entry in build_interactive_app_catalog(
                config.interactive_apps,
                registry,
            )
        ]

    @staticmethod
    def _appearance_defaults() -> dict[str, Any]:
        return {
            "color_mode": UI_DEFAULTS.onboarding_color_mode.value,
            "light_colors": list(UI_DEFAULTS.onboarding_light_colors.value),
            "light_diffusion": UI_DEFAULTS.onboarding_light_diffusion.value,
            "polygon_opacity_low": UI_DEFAULTS.onboarding_polygon_opacity_low.value,
            "polygon_opacity_high": UI_DEFAULTS.onboarding_polygon_opacity_high.value,
            "polygon_line_color": UI_DEFAULTS.onboarding_polygon_line_color.value,
            "polygon_line_opacity": UI_DEFAULTS.onboarding_polygon_line_opacity.value,
            "polygon_seed": UI_DEFAULTS.onboarding_polygon_seed.value,
            "dynamic_background": UI_DEFAULTS.onboarding_dynamic_background.value,
            "dark": copy.deepcopy(UI_DEFAULTS.onboarding_dark_appearance.value),
        }


def _deep_merged(base: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
    merged = copy.deepcopy(base)
    for key, value in override.items():
        current = merged.get(key)
        merged[key] = (
            _deep_merged(current, value)
            if isinstance(current, dict) and isinstance(value, dict)
            else copy.deepcopy(value)
        )
    return merged


def _ui_model_provider_label(provider: Any, endpoint: Any = "") -> str:
    normalized = str(provider or "").strip().lower()
    endpoint_text = str(endpoint or "").strip().lower()
    if normalized == "openai":
        return "OpenAI"
    if _is_ui_openai_codex_provider(provider):
        return "OpenAI Codex OAuth"
    if _is_ui_xai_oauth_provider(provider):
        return "xAI Grok OAuth"
    if normalized == "anthropic":
        return "Anthropic"
    if normalized in {"gemini", "google", "google ai"}:
        return "Google AI"
    if "openrouter.ai" in endpoint_text:
        return "OpenRouter"
    return "OpenAI Compatible"


def _openai_codex_payload(settings: Any) -> dict[str, Any]:
    if hasattr(settings, "model_dump"):
        payload = settings.model_dump(mode="json")
    elif isinstance(settings, dict):
        payload = dict(settings)
    else:
        payload = {}
    return payload if isinstance(payload, dict) else {}


def _openai_codex_base_url(settings: Any) -> str:
    payload = _openai_codex_payload(settings)
    return str(payload.get("base_url") or "").strip()


def _xai_oauth_payload(settings: Any) -> dict[str, Any]:
    if hasattr(settings, "model_dump"):
        payload = settings.model_dump(mode="json")
    elif isinstance(settings, dict):
        payload = dict(settings)
    else:
        payload = {}
    return payload if isinstance(payload, dict) else {}


def _xai_oauth_base_url(settings: Any) -> str:
    payload = _xai_oauth_payload(settings)
    return str(payload.get("base_url") or LLM_DEFAULTS.xai_oauth.value["base_url"]).strip()


def _ui_model_endpoint(
    provider: Any,
    litellm_api_base: Any = "",
    openai_codex: Any = None,
    xai_oauth: Any = None,
) -> str:
    if _is_ui_openai_codex_provider(provider):
        return _openai_codex_base_url(openai_codex)
    if _is_ui_xai_oauth_provider(provider):
        return _xai_oauth_base_url(xai_oauth)
    return str(litellm_api_base or "").strip()


def _is_ui_openai_codex_provider(provider: Any) -> bool:
    normalized = str(provider or "").strip().lower().replace("_", "-")
    return normalized in {
        "codex",
        "openai-codex",
        "openai codex",
        "openai codex oauth",
        "openai subscription",
        "openai-subscription",
        "openai subscription service",
        "chatgpt",
        "chatgpt codex",
    }


def _is_ui_xai_oauth_provider(provider: Any) -> bool:
    return is_xai_oauth_provider(provider)


async def start_onboarding_web_service(settings: OnboardingWebSettings) -> tuple[OnboardingWebService, AiohttpSite]:
    service = OnboardingWebService(settings)
    app = service.build_app()
    site = await start_aiohttp_site(
        app,
        host=settings.host,
        port=settings.port,
        access_log=None,
        web_module=web,
    )
    return service, site


async def run_onboarding_web(settings: OnboardingWebSettings) -> None:
    service = OnboardingWebService(settings)
    app = service.build_app()
    completion_event = asyncio.Event()
    service.completion_event = completion_event
    site = await start_aiohttp_site(
        app,
        host=settings.host,
        port=settings.port,
        access_log=None,
        web_module=web,
    )
    LOGGER.info("Universal Agents onboarding is ready.")
    LOGGER.info("Open this URL in your browser: %s", service.onboarding_browser_url())
    LOGGER.info("If Docker is running on another machine, set NETWORK_PUBLIC_ORIGIN to that machine's http(s) origin.")
    # LOGGER.info("Web onboarding bound to %s", site.base_url)
    try:
        await completion_event.wait()
    finally:
        await site.stop()
