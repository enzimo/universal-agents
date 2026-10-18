#!/usr/bin/env python3
"""Serve the standalone Universal Agents onboarding aesthetic preview."""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
from functools import partial
from http import HTTPStatus
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import socket
import sys
import time
from urllib import parse as urllib_parse
import webbrowser


REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from runtime_settings.appearance import (  # noqa: E402
    DarkMetalAppearanceSettings,
    OnboardingAppearanceSettings,
)
from assistant_gateway.application.website_application import WebAppearanceV1  # noqa: E402
from custom_messages.contracts.status_v1 import StatusSeverity  # noqa: E402
from runtime_settings.network import compose_public_base_url  # noqa: E402
from assistant_gateway.application.command_help import (  # noqa: E402
    build_web_chat_command_guide_payload,
    build_web_chat_help,
)
from assistant_gateway.application.workspace_text_presentation import (  # noqa: E402
    present_workspace_text,
)
from assistant_gateway.onboarding_web import provider_test as provider_test_runtime  # noqa: E402
from custom_agents.agent_catalog import get_onboarding_agent_catalog  # noqa: E402
from scripts import preview_knowledge, preview_web_chat_samples  # noqa: E402

DEFAULT_ASSET_ROOT = REPO_ROOT / "assistant_gateway" / "onboarding_web" / "assets"
EXPORT_FILENAME = "universal_agents_onboarding_appearance.json"
CHAT_ASSET_FILENAME = "chat.html"
WEB_CHAT_SESSION_COOKIE = "ua_web_chat_session"
WEB_CHAT_PREVIEW_SESSION = "dev-preview-session"
WEB_CHAT_APPEARANCE_ROUTE = "/api/web-chat/appearance"
WEB_CHAT_PREVIEW_SESSION_KEY = "website:web-chat:dev"
WEB_CHAT_PREVIEW_CONVERSATION_ID = "dev-preview"
PREVIEW_OBJECTIVE_DIGEST = "a" * 64
PREVIEW_CONNECTION_DIGEST = "c" * 64
PREVIEW_HOSTED_DIGEST = "d" * 64
PUBLIC_VERSION = (REPO_ROOT / "VERSION").read_text(encoding="utf-8").strip()
# The real live updates stream sends a page every 2 seconds; Web Chat
# reconnects when nothing arrives for 15 (chat_live_events.mjs).
PREVIEW_STREAM_HEARTBEAT_SECONDS = 2
PREVIEW_THREADS_REVISION = "preview-threads:1"


class PreviewRequestHandler(SimpleHTTPRequestHandler):
    """Static-file handler with an explicit health route for preview polling."""

    server_version = "UniversalAgentsOnboardingPreview/1.0"

    def __init__(
        self,
        *args: object,
        directory: str | None = None,
        export_dir: str | None = None,
        **kwargs: object,
    ) -> None:
        self.export_dir = (
            Path(export_dir).expanduser() if export_dir else Path.home() / "Downloads"
        )
        super().__init__(*args, directory=directory, **kwargs)

    def log_message(self, format: str, *args: object) -> None:
        message = format % args
        route = urllib_parse.urlparse(self.path).path
        if self._is_web_chat_route(route):
            message = message.replace(route, "/[web-chat-token]")
        sys.stderr.write(
            "%s - - [%s] %s\n"
            % (self.client_address[0], self.log_date_time_string(), message)
        )

    def end_headers(self) -> None:
        self.send_header("Cache-Control", "no-store")
        super().end_headers()

    def do_GET(self) -> None:  # noqa: N802 - stdlib handler API
        route = urllib_parse.urlparse(self.path).path
        if route == "/health":
            self._write_health()
            return
        if route in {"/preview-settings", "/api/bootstrap"}:
            self._write_preview_settings()
            return
        if route == "/api/status":
            self._write_json(
                {
                    "status": "dev-preview",
                    "instance_config_exists": False,
                    "team_env_exists": False,
                    "completion": None,
                }
            )
            return
        if route == "/api/web-chat/settings":
            self._write_web_chat_settings()
            return
        if route == WEB_CHAT_APPEARANCE_ROUTE:
            self._handle_preview_appearance("GET")
            return
        if route == "/api/web-chat/workspace":
            if not self._web_chat_session_valid():
                self._write_json_error(
                    HTTPStatus.UNAUTHORIZED, "Web Chat session is invalid or expired."
                )
                return
            self._write_preview_workspace_directory()
            return
        if route == "/api/web-chat/workspace/file":
            if not self._web_chat_session_valid():
                self._write_json_error(
                    HTTPStatus.UNAUTHORIZED, "Web Chat session is invalid or expired."
                )
                return
            self._write_preview_workspace_file()
            return
        if route == "/api/web-chat/workspace/content":
            if not self._web_chat_session_valid():
                self._write_json_error(
                    HTTPStatus.UNAUTHORIZED, "Web Chat session is invalid or expired."
                )
                return
            self._write_preview_workspace_content()
            return
        if route.startswith(preview_knowledge.KNOWLEDGE_PREFIX + "/"):
            self._write_preview_knowledge_read(route)
            return
        authenticated_preview_routes = {
            "/api/web-chat/session": self._write_authenticated_web_chat_session,
            "/api/web-chat/team": self._write_preview_team,
            "/api/web-chat/objectives": self._write_preview_objectives,
            "/api/web-chat/schedules": self._write_preview_schedules,
            "/api/web-chat/events/stream": self._write_preview_event_stream,
            "/api/web-chat/memories": self._write_preview_memories,
            "/api/web-chat/tasks": self._write_preview_tasks,
            "/api/web-chat/access-inbox/counts": self._write_preview_inbox_counts,
            "/api/web-chat/access-inbox": self._write_preview_inbox,
            "/api/web-chat/accounts": self._write_preview_accounts,
            "/api/web-chat/access-map": self._write_preview_access_map,
            "/api/web-chat/status/access": self._write_preview_status_access,
            "/api/web-chat/status": self._write_preview_status,
        }
        authenticated_preview_writer = authenticated_preview_routes.get(route)
        if authenticated_preview_writer is not None:
            if not self._web_chat_session_valid():
                self._write_json_error(
                    HTTPStatus.UNAUTHORIZED, "Web Chat session is invalid or expired."
                )
                return
            authenticated_preview_writer()
            return
        if route == "/api/web-chat/sessions":
            if not self._web_chat_session_valid():
                self._write_json_error(
                    HTTPStatus.UNAUTHORIZED, "Web Chat session is invalid or expired."
                )
                return
            self._write_json(
                {
                    "status": "ok",
                    "sessions": [self._preview_session_payload()],
                    "event_cursor": self._preview_event_cursor(),
                    # The live updates carry the same revision, so Web Chat
                    # reloads the thread list only when it changes.
                    "threads_revision": PREVIEW_THREADS_REVISION,
                }
            )
            return
        if route.startswith("/api/web-chat/sessions/") and route.endswith("/messages"):
            if not self._web_chat_session_valid():
                self._write_json_error(
                    HTTPStatus.UNAUTHORIZED, "Web Chat session is invalid or expired."
                )
                return
            self._write_json(
                {
                    "status": "ok",
                    "messages": self._preview_messages(),
                    "before_cursor": None,
                    "has_more": False,
                    "event_cursor": self._preview_event_cursor(),
                }
            )
            return
        if route.rstrip("/") == "/api/agents/status":
            self._write_json(
                {
                    "status": "ok",
                    "source": "dev_preview",
                    "agents": [
                        {
                            "agent_id": "preview-manager",
                            "name": "Ava",
                            "named_agent_type": "TeamArchitect",
                            "is_team_lead": True,
                            "is_executing": False,
                            "has_execution_capacity": True,
                            "is_paused": False,
                            "is_exiting": False,
                            "top_level_queue_depth": 0,
                            "waiting_top_level_task_count": 0,
                            "deferred_message_count": 0,
                            "diagnostic": {"activity_source": "dev_preview"},
                            "last_seen_at": "preview",
                            "recent_activity": [
                                {
                                    "created_at": "preview",
                                    "summary": "Development preview status is online.",
                                    "activity_type": "status",
                                    "stage": "completed",
                                }
                            ],
                        }
                    ],
                }
            )
            return
        if route == "/download/instance_config.yaml":
            self._write_text_download(
                "instance_config.yaml",
                "# Development aesthetic preview only.\n# Production onboarding writes the real instance_config.yaml.\n",
            )
            return
        if route == "/download/team.env":
            self._write_text_download(
                "team.env",
                "# Development aesthetic preview only.\n# Production onboarding writes the real team.env.\n",
            )
            return
        if self._is_web_chat_route(route):
            self._serve_chat_shell()
            return
        super().do_GET()

    def do_POST(self) -> None:  # noqa: N802 - stdlib handler API
        route = self.path.split("?", 1)[0]
        if route == "/export-appearance":
            self._write_appearance_export()
            return
        if route in {"/test-provider-connection", "/api/provider-test"}:
            self._test_provider_connection()
            return
        if route == "/api/onboarding/complete":
            if self._read_json_payload("onboarding payload") is None:
                return
            self._write_json(
                {
                    "status": "ok",
                    "restart_required": False,
                    "config_path": "",
                    "env_path": "",
                    "report_path": "",
                    "dev_preview": True,
                }
            )
            return
        if route == "/api/onboarding/skills-repos/validate":
            payload = self._read_json_payload("skills repository payload")
            if payload is None:
                return
            repositories = payload.get("repositories")
            if not isinstance(repositories, list):
                repositories = []
            self._write_json(
                {
                    "status": "ok",
                    "repositories": [
                        {
                            "url": str(repository),
                            "ok": True,
                            "message": "Validation is skipped in the aesthetic preview.",
                        }
                        for repository in repositories
                        if str(repository or "").strip()
                    ],
                    "dev_preview": True,
                }
            )
            return
        if route == "/api/onboarding/restart":
            self._write_json(
                {"status": "ok", "restart_required": False, "dev_preview": True}
            )
            return
        if route.startswith(preview_knowledge.KNOWLEDGE_PREFIX + "/"):
            self._write_preview_knowledge_change(route)
            return
        if route == "/api/web-chat/auth":
            payload = self._read_json_payload("web chat auth payload")
            if payload is None:
                return
            if not str(payload.get("token") or "").strip():
                self._write_json_error(
                    HTTPStatus.UNAUTHORIZED, "Enter a Web Chat authentication token."
                )
                return
            self._write_authenticated_web_chat_session(
                headers={
                    "Set-Cookie": (
                        f"{WEB_CHAT_SESSION_COOKIE}={WEB_CHAT_PREVIEW_SESSION}; "
                        "Path=/; Max-Age=43200; HttpOnly; SameSite=Strict"
                    )
                },
            )
            return
        if route == "/api/web-chat/sessions":
            if not self._web_chat_session_valid():
                self._write_json_error(
                    HTTPStatus.UNAUTHORIZED, "Web Chat session is invalid or expired."
                )
                return
            self._write_json(
                {
                    "status": "ok",
                    "session": self._preview_session_payload(),
                    "event_cursor": self._preview_event_cursor(),
                },
                status=HTTPStatus.CREATED,
            )
            return
        if route.startswith("/api/web-chat/sessions/") and route.endswith("/archive"):
            if not self._web_chat_session_valid():
                self._write_json_error(
                    HTTPStatus.UNAUTHORIZED, "Web Chat session is invalid or expired."
                )
                return
            self._write_json({"status": "ok", "dev_preview": True})
            return
        if route == "/api/web-chat/chat":
            if not self._web_chat_session_valid():
                self._write_json_error(
                    HTTPStatus.UNAUTHORIZED, "Web Chat session is invalid or expired."
                )
                return
            payload = self._read_json_payload("web chat message payload")
            if payload is None:
                return
            message = str(payload.get("message") or "").strip()
            if not message:
                self._write_json_error(HTTPStatus.BAD_REQUEST, "message is required.")
                return
            self.server.preview_last_message_at = time.monotonic()  # type: ignore[attr-defined]
            if message.startswith("/"):
                reply = build_web_chat_help()
                system = True
            else:
                reply = "Development aesthetic preview only. Production Web Chat routes this message through the live gateway."
                system = False
            messages = self._append_preview_exchange(
                payload=payload,
                user_text=message,
                response_text=reply,
                response_role="system" if system else "assistant",
            )
            self._write_json(
                {
                    "status": "ok",
                    "session_key": WEB_CHAT_PREVIEW_SESSION_KEY,
                    "conversation_id": WEB_CHAT_PREVIEW_CONVERSATION_ID,
                    "messages": messages,
                    "event_cursor": self._preview_event_cursor(),
                }
            )
            return
        if self._handle_preview_runtime_control(route):
            return
        self.send_error(HTTPStatus.NOT_FOUND, "Unknown preview endpoint")

    def do_PUT(self) -> None:  # noqa: N802 - stdlib handler API
        if self.path.split("?", 1)[0] == WEB_CHAT_APPEARANCE_ROUTE:
            self._handle_preview_appearance("PUT")
            return
        self.send_error(HTTPStatus.NOT_FOUND)

    def do_DELETE(self) -> None:  # noqa: N802 - stdlib handler API
        if self.path.split("?", 1)[0] == WEB_CHAT_APPEARANCE_ROUTE:
            self._handle_preview_appearance("DELETE")
            return
        self.send_error(HTTPStatus.NOT_FOUND)

    # The signed-in person's saved Web Chat appearance, kept in this preview
    # server's memory only (never written to disk), so Customize Theme's Done,
    # Cancel and Reset to Defaults behave as in a real instance. Saves are
    # checked by the same models as the gateway's handler.
    def _handle_preview_appearance(self, method: str) -> None:
        if not self._web_chat_session_valid():
            self._write_json_error(
                HTTPStatus.UNAUTHORIZED, "Web Chat session is invalid or expired."
            )
            return
        if method == "DELETE":
            self.server.preview_appearance = None  # type: ignore[attr-defined]
        elif method == "PUT":
            payload = self._read_json_payload("appearance payload")
            if payload is None:
                return
            raw_appearance = payload.get("appearance")
            if not isinstance(raw_appearance, dict):
                self._write_json_error(HTTPStatus.BAD_REQUEST, "appearance must be a JSON object.")
                return
            try:
                normalized = OnboardingAppearanceSettings.model_validate(raw_appearance)
                saved = WebAppearanceV1.model_validate(normalized.model_dump(mode="json"))
            except ValueError as exc:
                self._write_json_error(HTTPStatus.BAD_REQUEST, str(exc))
                return
            self.server.preview_appearance = saved.model_dump(  # type: ignore[attr-defined]
                mode="json", exclude={"schema_version"}
            )
        self._write_json(
            {"status": "ok", "appearance": getattr(self.server, "preview_appearance", None)}
        )

    def do_PATCH(self) -> None:  # noqa: N802 - stdlib handler API
        route = self.path.split("?", 1)[0]
        if route.startswith("/api/web-chat/sessions/"):
            if not self._web_chat_session_valid():
                self._write_json_error(
                    HTTPStatus.UNAUTHORIZED, "Web Chat session is invalid or expired."
                )
                return
            payload = self._read_json_payload("thread rename payload")
            if payload is None:
                return
            session_alias = str(
                payload.get("session_alias") or payload.get("title") or ""
            ).strip()
            if not session_alias:
                self._write_json_error(
                    HTTPStatus.BAD_REQUEST, "Thread name is required."
                )
                return
            self._write_json(
                {"status": "ok", "session_alias": session_alias, "dev_preview": True}
            )
            return
        self.send_error(HTTPStatus.NOT_FOUND, "Unknown preview endpoint")

    def _is_web_chat_route(self, route: str) -> bool:
        return route.rstrip("/") == "/webchat"

    def _web_chat_session_valid(self) -> bool:
        cookie = self.headers.get("Cookie") or ""
        auth_header = self.headers.get("Authorization") or ""
        header_session = self.headers.get("X-Web-Chat-Session") or ""
        return (
            f"{WEB_CHAT_SESSION_COOKIE}={WEB_CHAT_PREVIEW_SESSION}" in cookie
            or auth_header == f"Bearer {WEB_CHAT_PREVIEW_SESSION}"
            or header_session == WEB_CHAT_PREVIEW_SESSION
        )

    def _read_preview_runtime_control(
        self,
        description: str,
    ) -> dict[str, object] | None:
        if not self._web_chat_session_valid():
            self._write_json_error(
                HTTPStatus.UNAUTHORIZED, "Web Chat session is invalid or expired."
            )
            return None
        return self._read_json_payload(description)

    def _handle_preview_runtime_control(self, route: str) -> bool:
        if not route.endswith("/control"):
            return False
        if route.startswith("/api/web-chat/objectives/"):
            payload = self._read_preview_runtime_control("Objective control payload")
            if payload is not None:
                objective_id = route.removeprefix(
                    "/api/web-chat/objectives/"
                ).removesuffix("/control")
                action = str(payload.get("action") or "").strip()
                self._write_json(
                    {
                        "status": "active" if action == "resume" else "paused",
                        "objective_id": objective_id,
                        "action": action,
                        "success": True,
                        "message": (
                            "Development preview accepted the Objective action."
                        ),
                        "cancellation_receipts": [],
                    }
                )
            return True
        if route.startswith("/api/web-chat/schedules/"):
            payload = self._read_preview_runtime_control("schedule control payload")
            if payload is not None:
                schedule_id = route.removeprefix(
                    "/api/web-chat/schedules/"
                ).removesuffix("/control")
                self._write_json(
                    {
                        "status": "ok",
                        "schedule_id": schedule_id,
                        "action": str(payload.get("action") or "").strip(),
                        "success": True,
                    }
                )
            return True
        if route.startswith("/api/web-chat/mcp/"):
            payload = self._read_preview_runtime_control("MCP control payload")
            if payload is not None:
                resource_path = route.removeprefix("/api/web-chat/mcp/").removesuffix(
                    "/control"
                )
                resource_kind, _, resource_id = resource_path.partition("/")
                action = str(payload.get("action") or "").strip()
                self._write_json(
                    {
                        "status": "removed" if action == "remove" else "updated",
                        "resource_kind": resource_kind,
                        "resource_id": resource_id,
                        "action": action,
                        "request_digest": "e" * 64,
                        "result_digest": "f" * 64,
                        "recoverable": True,
                    }
                )
            return True
        return False

    def _write_authenticated_web_chat_session(
        self,
        *,
        headers: dict[str, str] | None = None,
    ) -> None:
        self._write_json(
            {
                "status": "ok",
                "assistant_name": "Ava",
                "session_expires_at": 0,
                "user_bound": True,
                "canonical_user_id": "usr_dev_preview",
                "display_name": "Preview User",
                "auth_kind": "bootstrap",
                "credential_kind": "bootstrap_token",
                "is_bootstrap_user": True,
                "permanent_token_required": False,
                "permanent_token_status": "complete",
                "enrollment_label": "",
                "permission_csrf_token": "dev-preview-csrf-token",
            },
            headers=headers,
        )

    def _preview_session_payload(self) -> dict[str, object]:
        return {
            "session_key": WEB_CHAT_PREVIEW_SESSION_KEY,
            "session_alias": "Development Preview",
            "conversation_id": WEB_CHAT_PREVIEW_CONVERSATION_ID,
            # The thread's fixed name, as the gateway sends it (web_chat_threads).
            "title": "Development Preview",
            "updated_at": datetime.now(timezone.utc).isoformat(),
        }

    def _preview_messages(self) -> list[dict[str, object]]:
        messages = getattr(self.server, "web_chat_preview_messages", None)
        if not isinstance(messages, list):
            messages = []
            setattr(self.server, "web_chat_preview_messages", messages)
        return messages

    @staticmethod
    def _preview_workspace_files() -> dict[str, dict[str, object]]:
        return {
            "README.md": {
                "preview_kind": "markdown",
                "content_type": "text/markdown",
                "content": (
                    "# Universal Agents workspace\n\n"
                    "This Markdown file is rendered through the Web Chat renderer.\n\n"
                    "| Preview | Status |\n| --- | --- |\n| Markdown | Rendered |\n"
                ),
            },
            "demo.html": {
                "preview_kind": "html",
                "content_type": "text/html",
                "content": (
                    "<!doctype html><meta charset='utf-8'>"
                    "<style>body{font-family:system-ui;padding:2rem;color:#163c4b}"
                    "h1{color:#075985}</style>"
                    "<h1>Rendered HTML preview</h1>"
                    "<p>Scripts and external resources are disabled.</p>"
                ),
            },
            "src/gateway.py": {
                "preview_kind": "text",
                "content_type": "text/x-python",
                "content": (
                    "def route_message(message: str) -> str:\n"
                    '    return f"Gateway received: {message}"\n'
                ),
            },
            "crypto_candidate_dossiers_version_2026_08_26_final.json": {
                "preview_kind": "text",
                "content_type": "application/json",
                "content": (
                    '{"generated_at":"2026-08-26T12:00:00Z","candidates":'
                    '[{"symbol":"UA","score":0.94,"reviewed":true}]}\n'
                ),
            },
            "artifacts/model.weights": {
                "preview_kind": "unsupported",
                "content_type": None,
                "content": "",
                "message": "Preview is not available for .weights files.",
            },
        }

    def _write_preview_workspace_directory(self) -> None:
        query = urllib_parse.parse_qs(urllib_parse.urlparse(self.path).query)
        path = str((query.get("path") or [""])[0])
        now = datetime.now(timezone.utc).isoformat()
        if path == "":
            entries = [
                {"path": "artifacts", "name": "artifacts", "kind": "directory"},
                {"path": "src", "name": "src", "kind": "directory"},
                {
                    "path": "crypto_candidate_dossiers_version_2026_08_26_final.json",
                    "name": "crypto_candidate_dossiers_version_2026_08_26_final.json",
                    "kind": "file",
                    "preview_kind": "text",
                    "content_type": "application/json",
                },
                {
                    "path": "demo.html",
                    "name": "demo.html",
                    "kind": "file",
                    "preview_kind": "html",
                    "content_type": "text/html",
                },
                {
                    "path": "README.md",
                    "name": "README.md",
                    "kind": "file",
                    "preview_kind": "markdown",
                    "content_type": "text/markdown",
                },
            ]
        elif path == "src":
            entries = [
                {
                    "path": "src/gateway.py",
                    "name": "gateway.py",
                    "kind": "file",
                    "preview_kind": "text",
                    "content_type": "text/x-python",
                }
            ]
        elif path == "artifacts":
            entries = [
                {
                    "path": "artifacts/model.weights",
                    "name": "model.weights",
                    "kind": "file",
                    "preview_kind": "unsupported",
                    "content_type": None,
                }
            ]
        else:
            self._write_json_error(
                HTTPStatus.NOT_FOUND, "Workspace path was not found."
            )
            return
        for entry in entries:
            entry["modified_at_utc"] = now
            entry["size_bytes"] = (
                None
                if entry["kind"] == "directory"
                else len(
                    str(
                        self._preview_workspace_files()[str(entry["path"])]["content"]
                    ).encode("utf-8")
                )
            )
        self._write_json(
            {
                "status": "ok",
                "path": path,
                "entries": entries,
                "truncated": False,
                "entry_limit": 2000,
            }
        )

    def _write_preview_workspace_file(self) -> None:
        query = urllib_parse.parse_qs(urllib_parse.urlparse(self.path).query)
        path = str((query.get("path") or [""])[0])
        file_data = self._preview_workspace_files().get(path)
        if file_data is None:
            self._write_json_error(
                HTTPStatus.NOT_FOUND, "Workspace path was not found."
            )
            return
        payload = {
            "status": "ok",
            "path": path,
            "name": path.rsplit("/", 1)[-1],
            "size_bytes": len(str(file_data["content"]).encode("utf-8")),
            "modified_at_utc": datetime.now(timezone.utc).isoformat(),
            "preview_kind": file_data["preview_kind"],
        }
        if file_data.get("content_type"):
            payload["content_type"] = file_data["content_type"]
        if file_data["preview_kind"] in {"markdown", "text"}:
            payload["content"] = file_data["content"]
        if file_data["preview_kind"] == "text":
            content = str(file_data["content"])
            presentation = present_workspace_text(
                name=str(payload["name"]),
                content=content,
                size_bytes=len(content.encode("utf-8")),
            )
            if presentation.syntax_language:
                payload["syntax_language"] = presentation.syntax_language
            if presentation.highlighted_html:
                payload["highlighted_html"] = presentation.highlighted_html
            if presentation.formatted_content:
                payload["formatted_content"] = presentation.formatted_content
        if file_data.get("message"):
            payload["message"] = file_data["message"]
        self._write_json(payload)

    def _write_preview_workspace_content(self) -> None:
        query = urllib_parse.parse_qs(urllib_parse.urlparse(self.path).query)
        path = str((query.get("path") or [""])[0])
        file_data = self._preview_workspace_files().get(path)
        if file_data is None or file_data["preview_kind"] != "html":
            self._write_json_error(
                HTTPStatus.UNSUPPORTED_MEDIA_TYPE,
                "Raw preview content is unavailable for this file.",
            )
            return
        body = str(file_data["content"]).encode("utf-8")
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header(
            "Content-Security-Policy",
            "sandbox; default-src 'none'; script-src 'none'; style-src 'unsafe-inline'; frame-ancestors 'self'",
        )
        self.send_header("X-Frame-Options", "SAMEORIGIN")
        self.end_headers()
        self.wfile.write(body)

    # After a chat message the preview pretends the team works on it for a few
    # seconds, so Web Chat's activity mesh can be seen lighting up: the team
    # lead first, then a researcher and a writer. Windows are seconds after
    # the message.
    PREVIEW_ACTIVITY_WINDOWS = {
        "preview-manager": (0.0, 9.0),
        "preview-researcher": (2.0, 7.0),
        "preview-writer": (4.0, 8.5),
    }

    def _preview_status(self, agent_id: str) -> dict[str, str]:
        sent_at = getattr(self.server, "preview_last_message_at", None)
        start, end = self.PREVIEW_ACTIVITY_WINDOWS[agent_id]
        if sent_at is not None and start <= time.monotonic() - sent_at < end:
            return {"key": "busy", "label": "Working", "tone": "warning"}
        return {"key": "available", "label": "Available", "tone": "success"}

    # The preview signs in as an administrator, who may open the Status page;
    # its report is an all-clear with no findings.
    def _write_preview_knowledge_read(self, route: str) -> None:
        """The Knowledge page's reads (scripts/preview_knowledge.py)."""

        if not self._web_chat_session_valid():
            self._write_json_error(HTTPStatus.UNAUTHORIZED, "Web Chat session is invalid or expired.")
            return
        payload = preview_knowledge.answer_get(route, urllib_parse.parse_qs(urllib_parse.urlparse(self.path).query))
        if payload is None:
            self._write_json(
                {"status": "refused", "error": {"code": "not_found", "message": "There is no such page in the preview."}},
                status=HTTPStatus.NOT_FOUND,
            )
            return
        self._write_json(payload)

    def _write_preview_knowledge_change(self, route: str) -> None:
        """The Knowledge page's changes; nothing is stored (scripts/preview_knowledge.py)."""

        if not self._web_chat_session_valid():
            self._write_json_error(HTTPStatus.UNAUTHORIZED, "Web Chat session is invalid or expired.")
            return
        body = self._read_json_payload("knowledge change")
        if body is None:
            return
        answered = preview_knowledge.answer_post(route, body)
        if answered is None:
            self._write_json_error(HTTPStatus.NOT_FOUND, "There is no such change in the preview.")
            return
        status, payload = answered
        self._write_json(payload, status=status)

    def _write_preview_status_access(self) -> None:
        self._write_json({"status": "ok", "allowed": True})

    def _write_preview_status(self) -> None:
        self._write_json(
            {
                "status": "ok",
                "checked_at": datetime.now(timezone.utc).isoformat(),
                "unavailable_sources": [],
                "counts": {severity.value: 0 for severity in StatusSeverity},
                "findings": [],
                "total": 0,
                "has_more": False,
            }
        )

    def _preview_members(self) -> list[dict[str, object]]:
        now = datetime.now(timezone.utc).isoformat()
        return [
            {
                "agent_id": "preview-manager",
                "name": "Ava",
                "named_agent_type": "TeamArchitect",
                "is_team_lead": True,
                "status": self._preview_status("preview-manager"),
                "available_skills": ["delegation", "planning", "review"],
                "last_seen_at": now,
            },
            {
                "agent_id": "preview-researcher",
                "name": "Atlas",
                "named_agent_type": "WebResearchAgent",
                "is_team_lead": False,
                "status": self._preview_status("preview-researcher"),
                "available_skills": ["web-research", "source-review"],
                "last_seen_at": now,
            },
            {
                "agent_id": "preview-writer",
                "name": "Mira",
                "named_agent_type": "CodeWritingAgent",
                "is_team_lead": False,
                "status": self._preview_status("preview-writer"),
                "available_skills": ["implementation", "testing"],
                "last_seen_at": now,
            },
        ]

    def _write_preview_team(self) -> None:
        now = datetime.now(timezone.utc).isoformat()
        self._write_json(
            {
                "status": "ok",
                "definitions": [
                    {"named_agent_type": "TeamArchitect", "display_name": "Team Architect", "description": ""},
                    {"named_agent_type": "WebResearchAgent", "display_name": "Web Research Agent", "description": ""},
                    {"named_agent_type": "CodeWritingAgent", "display_name": "Code Writing Agent", "description": ""},
                ],
                "members": self._preview_members(),
                "mcp_connections": [
                    {
                        "connection_id": "connection-preview-evidence",
                        "record_digest": PREVIEW_CONNECTION_DIGEST,
                        "lifecycle": "active",
                        "connection_name": "Release Evidence",
                        "transport": "streamable_http",
                        "source": "operator_configured",
                        "source_locator": "preview:release-evidence",
                        "target_agent_types": ["TeamArchitect"],
                        "tool_id": "mcp:release-evidence",
                        "operation_count": 1,
                        "operations": ["read_release_evidence"],
                        "credential_mode": "operator_configured",
                        "legacy_sse": False,
                        "grandfathered": False,
                        "created_at_utc": now,
                        "updated_at_utc": now,
                        "can_decide": False,
                        "available_actions": ["disable"],
                    },
                    {
                        "connection_id": "connection-preview-docs",
                        "record_digest": "sha256:" + "3" * 64,
                        "lifecycle": "pending",
                        "connection_name": "OpenAI Developer Docs",
                        "transport": "streamable_http",
                        "source": "agent_proposal",
                        "source_locator": "preview:openai-developer-docs",
                        "target_agent_types": ["TeamArchitect"],
                        "tool_id": "mcp:openai-developer-docs",
                        "operation_count": 0,
                        "operations": [],
                        "credential_mode": "none",
                        "legacy_sse": False,
                        "grandfathered": False,
                        "created_at_utc": now,
                        "updated_at_utc": now,
                        "can_decide": True,
                        "available_actions": [],
                    }
                ],
                "hosted_mcp_servers": [
                    {
                        "hosted_server_id": "hosted-preview-notes",
                        "definition_digest": PREVIEW_HOSTED_DIGEST,
                        "server_name": "Release Notes Helper",
                        "purpose": "Format reviewed release evidence.",
                        "lifecycle": "stopped",
                        "target_agent_types": ["TeamArchitect"],
                        "tools": ["format_release_notes"],
                        "bind_host": "127.0.0.1",
                        "port": 18741,
                        "available_actions": ["start", "remove"],
                    }
                ],
            }
        )

    def _write_preview_objectives(self) -> None:
        now = datetime.now(timezone.utc)
        self._write_json(
            {
                "status": "ok",
                "source": "dev_preview",
                "objectives": [
                    {
                        "objective_id": "objective-preview-release",
                        "record_digest": PREVIEW_OBJECTIVE_DIGEST,
                        "title": "Ship the verified release",
                        "status": "active",
                        "horizon": "long_horizon",
                        "priority": "normal",
                        "desired_outcome": (
                            "A production-ready release with automated verification and "
                            "clear operator notes."
                        ),
                        "progress_summary": (
                            "The release candidate is built; integration verification is "
                            "in progress."
                        ),
                        "current_focus": "Validate gateway and Collector integration",
                        "current_plan_id": "plan-preview-release",
                        "plan_summary": "Build, verify, document, and publish the release.",
                        "success_criteria": [
                            {
                                "description": "All required checks pass",
                                "status": "open",
                            },
                            {
                                "description": "Operator notes are complete",
                                "status": "met",
                            },
                        ],
                        "subgoals": [
                            {"title": "Build release candidate", "status": "completed"},
                            {
                                "title": "Run integration checks",
                                "status": "in_progress",
                            },
                            {"title": "Publish operator notes", "status": "planned"},
                        ],
                        "schedule_ids": ["schedule-preview-release"],
                        "heartbeat_item_ids": [],
                        "work_policy_status": "enabled",
                        "updated_at": now.isoformat(),
                        "available_actions": ["pause", "stop", "abandon", "delete"],
                    }
                ],
            }
        )

    def _write_preview_schedules(self) -> None:
        self._write_json(preview_web_chat_samples.schedules(datetime.now(timezone.utc)))

    def _query_value(self, name: str, default: str = "") -> str:
        query = urllib_parse.parse_qs(urllib_parse.urlparse(self.path).query)
        return str((query.get(name) or [default])[0])

    def _write_preview_memories(self) -> None:
        scope = "team" if self._query_value("scope") == "team" else "personal"
        self._write_json(preview_web_chat_samples.memories(scope, datetime.now(timezone.utc)))

    def _write_preview_tasks(self) -> None:
        view = "finished" if self._query_value("view") == "finished" else "active"
        self._write_json(
            preview_web_chat_samples.tasks(
                self._query_value("agent_id"), view, datetime.now(timezone.utc)
            )
        )

    def _write_preview_inbox_counts(self) -> None:
        self._write_json(preview_web_chat_samples.inbox_counts())

    def _write_preview_inbox(self) -> None:
        self._write_json(preview_web_chat_samples.inbox(datetime.now(timezone.utc)))

    def _write_preview_accounts(self) -> None:
        self._write_json(preview_web_chat_samples.accounts())

    def _write_preview_access_map(self) -> None:
        offset = self._query_value("offset", "0")
        self._write_json(
            preview_web_chat_samples.access_map(
                self._query_value("view", "agents"), int(offset) if offset.isdigit() else 0
            )
        )

    def _preview_event_page(self, cursor: str) -> dict[str, object]:
        """One live updates page, in the shape the gateway's stream sends (conversation.py)."""

        messages = self._preview_messages()
        start = self._preview_cursor_position(cursor, maximum=len(messages))
        return {
            "status": "ok",
            "messages": messages[start:],
            "next_cursor": self._preview_event_cursor(),
            "has_more": False,
            "threads_revision": PREVIEW_THREADS_REVISION,
            "busy_session_keys": [],
            "team_activity": {"members": self._preview_members()},
            "transient_actions": [],
        }

    def _write_preview_event_stream(self) -> None:
        """Server-sent events like the gateway's stream, until the browser closes it.

        Without it Web Chat shows "Updates disconnected. Reconnecting..." and
        the Tasks view waits for the team's status forever.
        """

        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", "text/event-stream")
        self.end_headers()  # adds Cache-Control: no-store
        cursor = self._query_value("after")
        try:
            while True:
                page = self._preview_event_page(cursor)
                self.wfile.write(b"data: " + json.dumps(page, sort_keys=True).encode("utf-8") + b"\n\n")
                self.wfile.flush()
                cursor = str(page["next_cursor"])
                time.sleep(PREVIEW_STREAM_HEARTBEAT_SECONDS)
        except ConnectionError:
            return  # the browser closed the stream

    def _preview_event_cursor(self) -> str:
        return f"preview-stream:{len(self._preview_messages())}"

    @staticmethod
    def _preview_cursor_position(cursor: str, *, maximum: int) -> int:
        prefix = "preview-stream:"
        if not cursor.startswith(prefix):
            return 0
        try:
            return min(maximum, max(0, int(cursor.removeprefix(prefix))))
        except ValueError:
            return 0

    def _append_preview_exchange(
        self,
        *,
        payload: dict[str, object],
        user_text: str,
        response_text: str,
        response_role: str,
    ) -> list[dict[str, object]]:
        client_request_id = str(payload.get("client_request_id") or "preview-request")
        messages = self._preview_messages()
        existing = [
            message
            for message in messages
            if message.get("correlation_id") == client_request_id
        ]
        if existing:
            return existing
        created_at = datetime.now(timezone.utc).isoformat()
        start_sequence = len(messages) + 1
        exchange = [
            self._preview_message(
                message_id=f"{client_request_id}:user",
                sequence=start_sequence,
                role="user",
                content=user_text,
                message_type="user_message",
                correlation_id=client_request_id,
                created_at=created_at,
            ),
            self._preview_message(
                message_id=f"{client_request_id}:reply",
                sequence=start_sequence + 1,
                role=response_role,
                content=response_text,
                message_type=(
                    "command" if response_role == "system" else "assistant_message"
                ),
                correlation_id=client_request_id,
                created_at=created_at,
            ),
        ]
        messages.extend(exchange)
        return exchange

    @staticmethod
    def _preview_message(
        *,
        message_id: str,
        sequence: int,
        role: str,
        content: str,
        message_type: str,
        correlation_id: str,
        created_at: str,
    ) -> dict[str, object]:
        return {
            "schema_version": 1,
            "message_id": message_id,
            "client_request_id": correlation_id if role == "user" else None,
            "session_key": WEB_CHAT_PREVIEW_SESSION_KEY,
            "conversation_id": WEB_CHAT_PREVIEW_CONVERSATION_ID,
            "session_sequence": sequence,
            "event_sequence": sequence,
            "role": role,
            "content": content,
            "message_type": message_type,
            "message_level": "normal",
            "correlation_id": correlation_id,
            "attachments": [],
            "delivery_status": "delivered",
            "created_at_utc": created_at,
        }

    def _write_preview_settings(self) -> None:
        bind_port = int(
            os.environ.get("INTERNAL_WEBSITE_BIND_PORT")
            or getattr(self.server, "server_port", 18090)
        )
        raw_public_port = os.environ.get("INTERNAL_WEBSITE_PUBLIC_PORT")
        try:
            public_port = (
                bind_port
                if raw_public_port is None
                else (int(raw_public_port) if raw_public_port.strip() else None)
            )
        except ValueError:
            public_port = bind_port
        public_origin = (
            os.environ.get("NETWORK_PUBLIC_ORIGIN", "").strip() or "http://127.0.0.1"
        )
        self._write_json(
            {
                "network_public_origin": public_origin,
                "documentation_port": int(public_port or bind_port),
                "internal_website_bind_port": bind_port,
                "internal_website_public_port": public_port,
                "internal_website_base_url": compose_public_base_url(
                    public_origin, public_port
                ),
                "instance_root": "",
                "workspace_dir": "",
                "assistant_name": "Ava",
                "model_provider": "OpenRouter",
                "model_endpoint": "https://openrouter.ai/api/v1",
                "model_id": "deepseek/deepseek-v4.1-flash",
                "agent_catalog": get_onboarding_agent_catalog(),
                "appearance": self._default_appearance_settings(),
            }
        )

    def _write_web_chat_settings(self) -> None:
        self._write_json(
            {
                "assistant_name": "Ava",
                "version": PUBLIC_VERSION,
                "appearance": self._default_appearance_settings(),
                "commands": build_web_chat_command_guide_payload(),
            }
        )

    def _default_appearance_settings(self) -> dict[str, object]:
        return {
            "color_mode": "dark",
            "light_colors": ["#082f73", "#ffd60a", "#20f05a", "#ff3030"],
            "light_diffusion": 58,
            "polygon_opacity_low": 20,
            "polygon_opacity_high": 70,
            "polygon_line_color": "#202426",
            "polygon_line_opacity": 8,
            "dynamic_background": True,
            "dark": DarkMetalAppearanceSettings().model_dump(mode="json"),
        }

    def _serve_chat_shell(self) -> None:
        chat_path = Path(self.directory) / CHAT_ASSET_FILENAME
        try:
            body = chat_path.read_bytes()
        except OSError:
            self.send_error(HTTPStatus.NOT_FOUND, "Web chat shell is unavailable")
            return
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _write_appearance_export(self) -> None:
        payload = self._read_json_payload("export payload")
        if payload is None:
            return
        if "instance_config" not in payload:
            self.send_error(
                HTTPStatus.BAD_REQUEST, "Export payload must include instance_config"
            )
            return

        try:
            self.export_dir.mkdir(parents=True, exist_ok=True)
            target_path = self.export_dir / EXPORT_FILENAME
            target_path.write_text(
                json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8"
            )
        except OSError as exc:
            self.send_error(
                HTTPStatus.INTERNAL_SERVER_ERROR, f"Unable to write export: {exc}"
            )
            return

        self._write_json(
            {
                "status": "ok",
                "path": str(target_path),
            }
        )

    def _read_json_payload(self, payload_name: str) -> dict[str, object] | None:
        try:
            content_length = int(self.headers.get("Content-Length") or "0")
        except ValueError:
            self.send_error(HTTPStatus.BAD_REQUEST, "Invalid Content-Length")
            return None
        if content_length <= 0:
            self.send_error(HTTPStatus.BAD_REQUEST, f"Missing {payload_name}")
            return None
        if content_length > 65536:
            self.send_error(
                HTTPStatus.REQUEST_ENTITY_TOO_LARGE,
                f"{payload_name.title()} is too large",
            )
            return None

        try:
            payload = json.loads(self.rfile.read(content_length).decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            self.send_error(
                HTTPStatus.BAD_REQUEST, f"{payload_name.title()} must be JSON"
            )
            return None
        if not isinstance(payload, dict):
            self.send_error(
                HTTPStatus.BAD_REQUEST, f"{payload_name.title()} must be a JSON object"
            )
            return None
        return payload

    def _test_provider_connection(self) -> None:
        payload = self._read_json_payload("connection test payload")
        if payload is None:
            return

        provider = str(payload.get("provider") or "").strip()
        endpoint = str(payload.get("endpoint") or "").strip()
        token = str(payload.get("token") or "").strip()
        model = str(payload.get("model") or "").strip()
        missing = [
            label
            for label, value in (
                ("provider", provider),
                ("endpoint", endpoint),
                ("token", token),
                ("model", model),
            )
            if not value
        ]
        if missing:
            self._write_json_error(
                HTTPStatus.BAD_REQUEST,
                "Missing required connection fields: " + ", ".join(missing),
            )
            return
        if not endpoint.startswith(("http://", "https://")):
            self._write_json_error(
                HTTPStatus.BAD_REQUEST,
                "API Base URL must start with http:// or https://",
            )
            return

        xai_oauth = payload.get("xai_oauth")
        result = provider_test_runtime.test_provider_connection(
            provider=provider,
            endpoint=endpoint,
            token=token,
            model=model,
            reasoning_effort=str(payload.get("reasoning_effort") or ""),
            reasoning_summary=(
                str(payload["reasoning_summary"])
                if payload.get("reasoning_summary") is not None
                else None
            ),
            litellm_api_mode=str(payload.get("litellm_api_mode") or "chat_completions"),
            stream=payload.get("stream")
            if isinstance(payload.get("stream"), bool)
            else None,
            xai_oauth=xai_oauth if isinstance(xai_oauth, dict) else None,
        )
        if not result.ok:
            self._write_json_error(HTTPStatus.BAD_GATEWAY, result.message)
            return
        self._write_json(
            {
                "status": "ok",
                "provider": result.provider,
                "message": result.message,
                "sample": result.sample,
            }
        )

    def _write_health(self) -> None:
        payload = {
            "status": "ok",
            "preview": "onboarding_aesthetic",
            "writes_runtime_files": False,
        }
        self._write_json(payload)

    def _write_json_error(self, status: HTTPStatus, message: str) -> None:
        self._write_json({"status": "error", "message": message}, status=status)

    def _write_text_download(self, filename: str, body: str) -> None:
        raw_body = body.encode("utf-8")
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.send_header("Content-Length", str(len(raw_body)))
        self.send_header("Content-Disposition", f'attachment; filename="{filename}"')
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(raw_body)

    def _write_json(
        self,
        payload: dict[str, object],
        *,
        status: HTTPStatus = HTTPStatus.OK,
        headers: dict[str, str] | None = None,
    ) -> None:
        body = json.dumps(payload, sort_keys=True).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        for key, value in (headers or {}).items():
            self.send_header(key, value)
        self.end_headers()
        self.wfile.write(body)


def build_handler(
    asset_root: Path = DEFAULT_ASSET_ROOT, export_dir: Path | None = None
) -> type[PreviewRequestHandler]:
    """Return a request handler bound to the preview asset directory."""

    return partial(
        PreviewRequestHandler,
        directory=str(asset_root.resolve()),
        export_dir=str(export_dir.expanduser().resolve()) if export_dir else None,
    )  # type: ignore[return-value]


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Serve the onboarding aesthetic preview without starting the runtime."
    )
    parser.add_argument(
        "--host", default="127.0.0.1", help="Bind host for the preview server."
    )
    parser.add_argument(
        "--port", type=int, default=18090, help="Bind port for the preview server."
    )
    parser.add_argument(
        "--asset-root",
        default=str(DEFAULT_ASSET_ROOT),
        help="Static asset directory to serve.",
    )
    parser.add_argument(
        "--open",
        action="store_true",
        help="Open the preview URL in the default browser.",
    )
    parser.add_argument(
        "--export-dir",
        default=str(Path.home() / "Downloads"),
        help="Directory for one-time appearance exports.",
    )
    return parser


def _display_host(host: str) -> str:
    if host in {"0.0.0.0", "::", ""}:
        return "127.0.0.1"
    return host


def serve(
    *,
    host: str,
    port: int,
    asset_root: Path,
    export_dir: Path,
    open_browser: bool = False,
) -> int:
    if not asset_root.is_dir():
        raise SystemExit(f"Preview asset directory does not exist: {asset_root}")

    handler = build_handler(asset_root, export_dir=export_dir)
    try:
        httpd = ThreadingHTTPServer((host, port), handler)
    except OSError as exc:
        raise SystemExit(
            f"Unable to bind onboarding aesthetic preview on {host}:{port}: {exc}"
        ) from exc

    actual_host, actual_port = httpd.server_address[:2]
    url = f"http://{_display_host(str(actual_host))}:{actual_port}/"
    print(f"Onboarding aesthetic preview listening on {url}")
    print(
        "This preview serves static assets only; it does not write bootstrap or runtime files."
    )
    print(f"Appearance exports are written to {export_dir / EXPORT_FILENAME}.")
    if open_browser:
        webbrowser.open(url)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\nStopping onboarding aesthetic preview.")
    finally:
        httpd.server_close()
    return 0


def main() -> int:
    args = build_arg_parser().parse_args()
    host = str(args.host)
    try:
        socket.getaddrinfo(host, args.port)
    except socket.gaierror as exc:
        raise SystemExit(f"Unable to resolve bind host {host!r}: {exc}") from exc
    return serve(
        host=host,
        port=int(args.port),
        asset_root=Path(args.asset_root).expanduser().resolve(),
        export_dir=Path(args.export_dir).expanduser().resolve(),
        open_browser=bool(args.open),
    )


if __name__ == "__main__":
    raise SystemExit(main())
