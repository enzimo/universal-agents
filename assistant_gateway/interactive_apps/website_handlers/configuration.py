from __future__ import annotations
import asyncio
import json
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
import yaml
from aiohttp import web
from config_backup_retention import prune_config_backups
from config_file_updates import ConfigFileConflict, config_revision, replace_config_file
from assistant_gateway.config import GatewayInstanceConfig
from runtime_settings.instance_layout import InstanceLayout
from assistant_gateway.interactive_apps.host_ports import (
    InternalWebsiteHostEnvironment,
)
from custom_messages.contracts.runtime_v1 import (
    RESTART_SCOPE_ALL,
    AUTHORITY_MODE_AUTHENTICATED_GATEWAY_OPERATOR,
    RUNTIME_RESTART_REQUEST_SCHEMA_VERSION,
)
from id_generation import new_id
from assistant_gateway.interactive_apps.website_support import LOGGER
from .operations import WebsiteConfigurationHandlersOperations


class WebsiteConfigurationHandlers:
    """Own configuration HTTP behavior with explicit state and bound operations."""

    def __init__(
        self,
        *,
        environment: InternalWebsiteHostEnvironment,
        logger: logging.Logger | None,
    ) -> None:
        self.operations: WebsiteConfigurationHandlersOperations
        self._logger = logger or LOGGER
        self.environment = environment

    async def _handle_config_editor_get_config(
        self, request: web.Request
    ) -> web.Response:
        self.operations._require_bootstrap_web_chat_session(request)
        config_path = self._instance_config_path()
        if not config_path.is_file():
            raise web.HTTPNotFound(text="instance_config.yaml is unavailable.")
        content = config_path.read_bytes()
        return web.json_response(
            {
                "status": "ok",
                "path": str(config_path),
                "content": content.decode("utf-8"),
                "revision": config_revision(content),
                "backups": self._list_instance_config_backups(),
            }
        )

    async def _handle_config_editor_save_config(
        self, request: web.Request
    ) -> web.Response:
        self.operations._require_bootstrap_web_chat_session(request)
        payload = await self.operations._json_payload(request)
        content = str(payload.get("content") or "")
        self._validate_instance_config_text(content)
        config_path = self._instance_config_path()
        if not config_path.is_file():
            raise web.HTTPNotFound(text="instance_config.yaml is unavailable.")
        backup_path = self._instance_config_backup_path(config_path)
        revision = await self._replace_config(
            config_path, content, self._expected_revision(payload), backup_path
        )
        prune_config_backups(
            config_path,
            event_logger=self._logger,
            preserve_paths=(backup_path,),
        )
        return web.json_response(
            {
                "status": "ok",
                "path": str(config_path),
                "revision": revision,
                "backup": self._config_backup_payload(backup_path),
                "backups": self._list_instance_config_backups(),
            }
        )

    async def _handle_config_editor_download_config(
        self, request: web.Request
    ) -> web.FileResponse:
        self.operations._require_bootstrap_web_chat_session(request)
        config_path = self._instance_config_path()
        if not config_path.is_file():
            raise web.HTTPNotFound(text="instance_config.yaml is unavailable.")
        return web.FileResponse(
            config_path,
            headers={
                "Content-Disposition": 'attachment; filename="instance_config.yaml"'
            },
        )

    async def _handle_config_editor_restore_backup(
        self, request: web.Request
    ) -> web.Response:
        self.operations._require_bootstrap_web_chat_session(request)
        payload = await self.operations._json_payload(request)
        expected_revision = self._expected_revision(payload)
        config_path = self._instance_config_path()
        backup_path = self._resolve_instance_config_backup(
            request.match_info.get("backup_name")
        )
        if not backup_path.is_file():
            raise web.HTTPNotFound(text="Backup not found.")
        backup_content = backup_path.read_text(encoding="utf-8")
        self._validate_instance_config_text(backup_content)
        current_backup_path = self._instance_config_backup_path(config_path)
        revision = await self._replace_config(
            config_path, backup_content, expected_revision, current_backup_path
        )
        prune_config_backups(
            config_path,
            event_logger=self._logger,
            preserve_paths=(current_backup_path,),
        )
        return web.json_response(
            {
                "status": "ok",
                "restored_from": backup_path.name,
                "revision": revision,
                "backup": self._config_backup_payload(current_backup_path),
                "backups": self._list_instance_config_backups(),
            }
        )

    async def _handle_config_editor_restart(self, request: web.Request) -> web.Response:
        session = self.operations._require_bootstrap_web_chat_session(request)
        restart_path = self._write_config_editor_restart_request(
            canonical_user_id=session.canonical_user_id,
        )
        return web.json_response(
            {
                "status": "ok",
                "scope": RESTART_SCOPE_ALL,
                "restart_request_path": str(restart_path),
                "message": "Supervised runtime restart requested.",
            }
        )

    def _instance_config_path(self) -> Path:
        # The one saved settings file (GatewayInstanceConfig.load_saved), even
        # when this process was started from another path.
        saved_path = InstanceLayout.at(self.environment.instance_root_path).instance_config_path
        if saved_path.is_file():
            return saved_path
        return self.environment.config_path.resolve()

    def _validate_instance_config_text(self, content: str) -> None:
        if not content.strip():
            raise web.HTTPBadRequest(text="instance_config.yaml content is required.")
        try:
            parsed = yaml.safe_load(content)
        except yaml.YAMLError as exc:
            raise web.HTTPBadRequest(text=f"Invalid YAML: {exc}") from exc
        if not isinstance(parsed, dict):
            raise web.HTTPBadRequest(
                text="instance_config.yaml must be a YAML mapping."
            )
        try:
            GatewayInstanceConfig.model_validate(parsed)
        except Exception as exc:
            raise web.HTTPBadRequest(
                text=f"Invalid instance_config.yaml: {exc}"
            ) from exc

    def _instance_config_backup_path(self, config_path: Path) -> Path:
        if not config_path.is_file():
            raise web.HTTPNotFound(text="instance_config.yaml is unavailable.")
        timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S.%fZ")
        backup_path = config_path.with_name(f"{config_path.name}.bak.{timestamp}")
        return backup_path

    @staticmethod
    def _expected_revision(payload: dict[str, Any]) -> str:
        revision = payload.get("revision")
        if not isinstance(revision, str) or len(revision) != 64 or any(
            character not in "0123456789abcdef" for character in revision
        ):
            raise web.HTTPConflict(text="Reload the configuration editor before saving or restoring settings.")
        return revision

    @staticmethod
    async def _replace_config(path: Path, content: str, expected_revision: str, backup: Path) -> str | None:
        try:
            return await asyncio.to_thread(
                replace_config_file,
                path,
                content.encode("utf-8"),
                expected_revision=expected_revision,
                backup_path=backup,
            )
        except ConfigFileConflict as exc:
            raise web.HTTPConflict(text=str(exc)) from exc

    def _list_instance_config_backups(self) -> list[dict[str, Any]]:
        config_path = self._instance_config_path()
        backup_prefix = f"{config_path.name}.bak."
        backups: list[dict[str, Any]] = []
        for backup_path in config_path.parent.glob(f"{backup_prefix}*"):
            if backup_path.is_file():
                try:
                    backups.append(self._config_backup_payload(backup_path))
                except OSError:
                    self._logger.warning(
                        "Could not inspect config backup %s", backup_path, exc_info=True
                    )
        return sorted(backups, key=lambda item: str(item["created_at"]), reverse=True)

    def _config_backup_payload(self, backup_path: Path) -> dict[str, Any]:
        stat = backup_path.stat()
        return {
            "name": backup_path.name,
            "path": str(backup_path),
            "created_at": datetime.fromtimestamp(
                stat.st_mtime, timezone.utc
            ).isoformat(),
            "size_bytes": stat.st_size,
        }

    def _resolve_instance_config_backup(self, backup_name: Any) -> Path:
        config_path = self._instance_config_path()
        rendered = str(backup_name or "").strip()
        backup_prefix = f"{config_path.name}.bak."
        if (
            not rendered
            or Path(rendered).name != rendered
            or not rendered.startswith(backup_prefix)
        ):
            raise web.HTTPBadRequest(text="Invalid backup name.")
        backup_path = (config_path.parent / rendered).resolve()
        config_dir = config_path.parent.resolve()
        try:
            backup_path.relative_to(config_dir)
        except ValueError as exc:
            raise web.HTTPBadRequest(text="Invalid backup path.") from exc
        return backup_path

    def _write_config_editor_restart_request(
        self,
        *,
        canonical_user_id: str | None,
    ) -> Path:
        request_dir = (
            self.environment.instance_root_path / "runtime" / "restart_requests"
        )
        request_dir.mkdir(parents=True, exist_ok=True)
        payload = {
            "schema_version": RUNTIME_RESTART_REQUEST_SCHEMA_VERSION,
            "request_id": new_id("restart"),
            "created_at": datetime.now(timezone.utc).isoformat(),
            "scope": RESTART_SCOPE_ALL,
            "reason": "Config editor requested restart after instance_config.yaml changes.",
            "requested_by": "gateway_config_editor",
            "authority": {
                "mode": AUTHORITY_MODE_AUTHENTICATED_GATEWAY_OPERATOR,
                "source": "bootstrap_config_editor_session",
                "canonical_user_id": str(canonical_user_id or "").strip() or None,
            },
        }
        path = request_dir / f"{payload['request_id']}.json"
        tmp_path = path.with_suffix(".json.tmp")
        tmp_path.write_text(
            json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8"
        )
        tmp_path.replace(path)
        return path
