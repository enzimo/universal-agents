from __future__ import annotations
import json
import logging
from runtime_settings.instance_layout import InstanceLayout
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from aiohttp import web
from assistant_gateway.application.debug_controls import normalize_context_dump_mode
from assistant_gateway.application.command_help import (
    build_web_chat_command_guide_payload,
)
from assistant_gateway.application.website_application import (
    WebsiteGatewayApplication,
)
from assistant_gateway.config import GatewayInstanceConfig
from assistant_gateway.interactive_apps.host_ports import (
    InternalWebsiteHostEnvironment,
)
from custom_messages.contracts.runtime_v1 import (
    DELIVERY_CHANNEL_WEBSITE,
)
from assistant_gateway.interactive_apps.website_support import (
    LOGGER,
    PUBLIC_VERSION,
    _payload_bool,
    _safe_client_id,
)
from .operations import WebsiteDiagnosticsHandlersOperations


class WebsiteDiagnosticsHandlers:
    """Own diagnostics HTTP behavior with explicit state and bound operations."""

    def __init__(
        self,
        *,
        application: WebsiteGatewayApplication,
        environment: InternalWebsiteHostEnvironment,
        logger: logging.Logger | None,
    ) -> None:
        self.operations: WebsiteDiagnosticsHandlersOperations
        context_dump = environment.agent_debug.get("context_dump")
        self._context_dump = (
            dict(context_dump) if isinstance(context_dump, dict) else {}
        )
        self._logger = logger or LOGGER
        self.application = application
        self.channel_name = DELIVERY_CHANNEL_WEBSITE
        self.environment = environment

    async def _handle_web_chat_settings(self, _request: web.Request) -> web.Response:
        return web.json_response(
            {
                "assistant_name": self.environment.assistant_display_name,
                "version": PUBLIC_VERSION,
                "appearance": self.environment.appearance,
                "commands": build_web_chat_command_guide_payload(),
                "debug": {
                    "context_dump": self._web_chat_context_dump_settings(),
                },
            }
        )

    async def _handle_web_chat_debug_context_dump(
        self, request: web.Request
    ) -> web.Response:
        session = self.operations._require_bootstrap_web_chat_session(request)
        payload = await self.operations._json_payload(request)
        current = self._web_chat_context_dump_settings()
        enabled = _payload_bool(
            payload.get("enabled"), default=bool(current["enabled"])
        )
        mode = normalize_context_dump_mode(payload.get("mode") or current["mode"])
        saved_path = self._save_context_dump_settings(enabled=enabled, mode=mode)
        client_id = _safe_client_id(payload.get("client_id"))
        session_key = (
            str(payload.get("session_key") or "").strip()
            or f"website:web-chat:{client_id}"
        )
        conversation_id = str(payload.get("conversation_id") or "").strip() or None
        try:
            response = (
                await self.application.operations.send_context_debug_dump_control(
                    enabled=enabled,
                    mode=mode,
                    session_key=session_key,
                    conversation_id=conversation_id,
                    channel_name=self.channel_name,
                    user_id=self.operations._web_session_owner_scope(session)
                    or client_id,
                    chat_id="web-chat",
                    thread_id=None,
                    canonical_user_id=session.canonical_user_id,
                )
            )
        except TimeoutError as exc:
            raise web.HTTPGatewayTimeout(text=str(exc)) from exc
        live_message = str(getattr(response, "text", "") or "")
        live_applied = "Context debug dump settings applied:" in live_message
        return web.json_response(
            {
                "status": "ok",
                "context_dump": self._web_chat_context_dump_status(),
                "saved_path": str(saved_path),
                "live_applied": live_applied,
                "message": live_message,
            }
        )

    async def _handle_web_chat_debug_context_dump_status(
        self, request: web.Request
    ) -> web.Response:
        self.operations._require_bootstrap_web_chat_session(request)
        return web.json_response(
            {
                "status": "ok",
                "context_dump": self._web_chat_context_dump_status(),
            }
        )

    def _web_chat_context_dump_settings(self) -> dict[str, Any]:
        context_dump = dict(self._context_dump)
        mode = str(context_dump.get("mode") or "on_error").strip().lower()
        if mode not in {"always", "on_error", "on_budget_warning"}:
            mode = "on_error"
        return {
            "enabled": bool(context_dump.get("enabled", False)),
            "mode": mode,
            "controls_visible": bool(
                context_dump.get("web_chat_controls_enabled", False)
            ),
        }

    def _web_chat_context_dump_status(self) -> dict[str, Any]:
        return {
            **self._web_chat_context_dump_settings(),
            "last_dump": self._latest_context_dump_status(),
        }

    def _latest_context_dump_status(self) -> dict[str, Any]:
        output_dir = self._context_dump_output_dir()
        base_payload = {
            "available": False,
            "dumped_at": None,
            "dump_id": None,
            "task_id": None,
            "correlation_id": None,
            "relative_path": None,
            "output_dir": self._workspace_relative_path(output_dir),
        }
        if not output_dir.exists():
            return base_payload

        latest: tuple[float, datetime, Path] | None = None
        try:
            for metadata_name in ("post_metadata.json", "pre_metadata.json"):
                for metadata_path in output_dir.rglob(metadata_name):
                    if not metadata_path.is_file():
                        continue
                    dumped_at = self._context_dump_metadata_timestamp(metadata_path)
                    rank = dumped_at.timestamp()
                    if latest is None or rank > latest[0]:
                        latest = (rank, dumped_at, metadata_path)
        except OSError as exc:
            self._logger.warning(
                "Could not inspect context debug dump directory %s: %s", output_dir, exc
            )
            return base_payload

        if latest is None:
            return base_payload
        _, dumped_at, metadata_path = latest
        identifiers = self._context_dump_identifiers(metadata_path)
        return {
            **base_payload,
            **identifiers,
            "available": True,
            "dumped_at": dumped_at.astimezone(timezone.utc).isoformat(),
            "relative_path": self._workspace_relative_path(metadata_path.parent),
        }

    def _context_dump_output_dir(self) -> Path:
        layout = InstanceLayout.at(self.environment.instance_root_path)
        return layout.diagnostics_dir / "context_dumps"

    def _context_dump_metadata_timestamp(self, metadata_path: Path) -> datetime:
        payload = self._context_dump_metadata_payload(metadata_path)
        raw_created_at = str(payload.get("created_at") or "").strip()
        if raw_created_at:
            try:
                timestamp = datetime.fromisoformat(
                    raw_created_at.replace("Z", "+00:00")
                )
                if timestamp.tzinfo is None:
                    timestamp = timestamp.replace(tzinfo=timezone.utc)
                return timestamp.astimezone(timezone.utc)
            except ValueError:
                pass
        return datetime.fromtimestamp(metadata_path.stat().st_mtime, timezone.utc)

    def _context_dump_metadata_payload(self, metadata_path: Path) -> dict[str, Any]:
        try:
            payload = json.loads(metadata_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError, TypeError):
            return {}
        return payload if isinstance(payload, dict) else {}

    def _context_dump_identifiers(self, metadata_path: Path) -> dict[str, str | None]:
        payload = self._context_dump_metadata_payload(metadata_path)
        candidate = payload.get("task")
        task_payload = candidate if isinstance(candidate, dict) else {}
        task_id = (
            str(
                task_payload.get("task_id") or metadata_path.parent.parent.name or ""
            ).strip()
            or None
        )
        correlation_id = str(task_payload.get("correlation_id") or "").strip() or None
        if correlation_id is None:
            dump_dir_parts = metadata_path.parent.name.split("_", 2)
            if len(dump_dir_parts) == 3:
                correlation_id = dump_dir_parts[2] or None
        return {
            "dump_id": task_id,
            "task_id": task_id,
            "correlation_id": correlation_id,
        }

    def _workspace_relative_path(self, path: Path) -> str:
        """Paths shown to the operator, relative to the instance root."""

        resolved_path = path.expanduser().resolve()
        root = self.environment.instance_root_path.expanduser().resolve()
        try:
            return str(resolved_path.relative_to(root))
        except ValueError:
            return str(resolved_path)

    def _save_context_dump_settings(self, *, enabled: bool, mode: str) -> Path:
        config_path = self.operations._instance_config_path()
        try:
            config = GatewayInstanceConfig.load_saved(config_path)
        except FileNotFoundError as exc:
            raise RuntimeError(
                "The operator-owned instance config is unavailable."
            ) from exc
        debug_config = dict(config.agent.debug or {})
        context_dump = dict(debug_config.get("context_dump") or {})
        context_dump["enabled"] = bool(enabled)
        context_dump["mode"] = normalize_context_dump_mode(mode)
        debug_config["context_dump"] = context_dump
        config.agent.debug = debug_config
        saved_path = config.save(config_path)
        self._context_dump = dict((config.agent.debug or {}).get("context_dump") or {})
        return saved_path
