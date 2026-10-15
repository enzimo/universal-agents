"""Photon Spectrum iMessage interactive app adapter."""

from __future__ import annotations

import asyncio
import base64
from collections import deque
from collections.abc import Coroutine
from contextlib import suppress
import json
import logging
import mimetypes
import os
from pathlib import Path
import secrets
import shutil
from typing import Any, Mapping, Optional

from assistant_gateway.application.gateway_application import GatewayDeliveryEvent
from runtime_settings.interactive_apps import PhotonAppSettings
from assistant_gateway.attachments import StagedAttachmentUnavailable
from assistant_gateway.inbound_attachments import (
    AttachmentValidationError,
)
from assistant_gateway.interactive_apps.base import ActivityContext, BaseInteractiveApp
from assistant_gateway.interactive_apps.contracts import (
    InteractiveAppApplication,
    InteractiveAppBridge,
)
from assistant_gateway.interactive_apps.host_ports import InteractiveAppRuntimeContext
from assistant_gateway.application.command_help import (
    build_interactive_session_help,
)
from assistant_gateway.translation import ChannelInboundMessage, ChannelOutboundMessage
from custom_messages.contracts.runtime_v1 import DELIVERY_CHANNEL_PHOTON
from custom_messages.contracts.attachments_v1 import UserTaskAttachment
from custom_messages.contracts.delivery_v1 import UserNotificationAttachment
from env_secrets import get_env_with_file_fallback

try:
    import aiohttp
except ImportError:  # pragma: no cover - aiohttp is part of requirements.txt
    aiohttp = None


logger = logging.getLogger(__name__)

PHOTON_HELP_TEXT = build_interactive_session_help(
    include_reply=False,
    trailer="Other messages go to the manager. Reminder phrases like `remind me every 2 hours to ...` also work.",
)
PHOTON_SIDECAR_DIR = Path(__file__).resolve().parent / "photon_sidecar"
PHOTON_SIDECAR_TOKEN_HEADER = "X-Universal-Agents-Sidecar-Token"
MAX_DEDUPED_MESSAGE_IDS = 4096
MAX_INBOUND_BACKOFF_SECONDS = 30.0
# The only variables the Photon sidecar takes from the gateway's (or adapter
# host's) environment. The sidecar is a third-party Node program, so it gets
# none of that process's secrets (task-context signing key, broker token, other
# channels' tokens): only the program search path, home and temporary
# directory, locale and time zone, and the proxy and CA settings its HTTPS and
# gRPC clients read. Its Photon values are added explicitly.
PHOTON_SIDECAR_INHERITED_ENV = frozenset(
    {
        "PATH",
        "HOME",
        "TMPDIR",
        "TZ",
        "LANG",
        "LANGUAGE",
        "HTTP_PROXY",
        "HTTPS_PROXY",
        "NO_PROXY",
        "http_proxy",
        "https_proxy",
        "no_proxy",
        "grpc_proxy",
        "no_grpc_proxy",
        "NODE_EXTRA_CA_CERTS",
        "SSL_CERT_FILE",
        "SSL_CERT_DIR",
        "GRPC_DEFAULT_SSL_ROOTS_FILE_PATH",
    }
)
PHOTON_SIDECAR_INHERITED_PREFIXES = ("LC_",)


class PhotonSidecarError(RuntimeError):
    """Raised when the local Photon sidecar returns an error."""


def build_photon_sidecar_environment(
    parent_env: Mapping[str, str], sidecar_values: Mapping[str, str]
) -> dict[str, str]:
    """The sidecar's environment: the allowed parent variables plus its own Photon values."""

    environment = {
        key: value
        for key, value in parent_env.items()
        if key in PHOTON_SIDECAR_INHERITED_ENV
        or key.startswith(PHOTON_SIDECAR_INHERITED_PREFIXES)
    }
    environment.update(sidecar_values)
    return environment


def _normalized_string(value: Any) -> str:
    return str(value or "").strip()


def _preview_text(value: str, *, limit: int = 120) -> str:
    text = " ".join((value or "").split())
    if len(text) <= limit:
        return text
    return text[: limit - 1] + "..."


def _content_type_for_path(path: str, fallback: Optional[str]) -> str:
    if fallback:
        return fallback
    return mimetypes.guess_type(path)[0] or "application/octet-stream"


def _attachment_kind(attachment: UserNotificationAttachment) -> str:
    if attachment.kind == "audio":
        return "voice"
    return "attachment"


class PhotonInteractiveApp(BaseInteractiveApp):
    channel_name = DELIVERY_CHANNEL_PHOTON

    def __init__(
        self,
        config: PhotonAppSettings,
        application: InteractiveAppApplication,
        bridge: InteractiveAppBridge,
        runtime: InteractiveAppRuntimeContext,
    ) -> None:
        self.config = config
        self.application = application
        self.bridge = bridge
        self.request_timeout_seconds = runtime.request_timeout_seconds
        self.attachments = runtime.attachments
        self.outgoing_attachments = runtime.outgoing_attachments
        self._http_session: Any = None
        self._inbound_task: Optional[asyncio.Task] = None
        self._background_tasks: set[asyncio.Task[Any]] = set()
        self._sidecar_process: Optional[asyncio.subprocess.Process] = None
        self._sidecar_log_task: Optional[asyncio.Task] = None
        self._sidecar_token: Optional[str] = None
        self._seen_message_ids: set[str] = set()
        self._seen_message_queue: deque[str] = deque()
        self._mention_warning_emitted = False

    @property
    def enabled(self) -> bool:
        return bool(self.config.enabled)

    async def emit_text_activity_update(
        self, context: ActivityContext, text: str
    ) -> None:
        if not context.chat_id:
            return
        await self._send_text(chat_id=context.chat_id, text=text)

    def uses_native_activity_indicator(self, context: ActivityContext) -> bool:
        return bool(context.chat_id)

    async def emit_native_activity_indicator(self, context: ActivityContext) -> None:
        if not context.chat_id:
            return
        try:
            await self._sidecar_call(
                "/typing", {"spaceId": context.chat_id, "state": "start"}
            )
        except Exception:
            logger.debug(
                "Photon typing indicator failed for chat_id=%s.",
                context.chat_id,
                exc_info=True,
            )

    async def start(self) -> None:
        if not self.enabled:
            return
        if aiohttp is None:
            raise RuntimeError(
                "Photon support requires aiohttp. Install requirements.txt again."
            )
        self._warn_if_allow_all()
        self._sidecar_token = self._resolve_sidecar_token()
        project_id = self._resolve_project_id()
        project_secret = self._resolve_project_secret()
        if not project_id:
            raise RuntimeError(
                "Photon is enabled but no project id is configured. Set "
                "interactive_apps.plugins.builtin.photon.settings.project_id "
                f"or {self.config.project_id_env_var}."
            )
        if not project_secret:
            raise RuntimeError(
                "Photon is enabled but no project secret is configured. Set "
                f"{self.config.project_secret_env_var} or {self.config.project_secret_env_var}_FILE."
            )

        self._http_session = aiohttp.ClientSession()
        if self.config.sidecar_autostart:
            await self._start_sidecar(
                project_id=project_id, project_secret=project_secret
            )
        else:
            await self._sidecar_call("/healthz", {})
        self._inbound_task = asyncio.create_task(
            self._inbound_loop(), name="photon_inbound"
        )
        logger.info(
            "Photon adapter connected to sidecar on %s.", self._sidecar_base_url
        )

    async def stop(self) -> None:
        if self._inbound_task is not None:
            self._inbound_task.cancel()
            with suppress(asyncio.CancelledError):
                await self._inbound_task
            self._inbound_task = None
        background_tasks = tuple(self._background_tasks)
        for task in background_tasks:
            task.cancel()
        if background_tasks:
            await asyncio.gather(*background_tasks, return_exceptions=True)
        self._background_tasks.clear()
        await self._stop_sidecar()
        if self._http_session is not None:
            await self._http_session.close()
            self._http_session = None

    async def deliver_event(self, event: GatewayDeliveryEvent) -> None:
        if not event.chat_id:
            logger.warning("Photon outbound message missing chat_id.")
            return
        if event.text:
            await self._send_text(chat_id=event.chat_id, text=event.text)
        for attachment in event.attachments:
            await self._send_attachment(chat_id=event.chat_id, attachment=attachment)

    @property
    def _sidecar_base_url(self) -> str:
        return f"http://{self.config.sidecar_bind_host}:{self.config.sidecar_port}"

    def _resolve_project_id(self) -> str:
        configured = _normalized_string(self.config.project_id)
        if configured:
            return configured
        return _normalized_string(
            get_env_with_file_fallback(self.config.project_id_env_var)
        )

    def _resolve_project_secret(self) -> str:
        return _normalized_string(
            get_env_with_file_fallback(self.config.project_secret_env_var)
        )

    def _resolve_sidecar_token(self) -> str:
        configured = _normalized_string(
            get_env_with_file_fallback(self.config.sidecar_token_env_var)
        )
        if configured:
            return configured
        return secrets.token_urlsafe(32)

    async def _start_sidecar(self, *, project_id: str, project_secret: str) -> None:
        node_path = shutil.which(self.config.node_bin)
        if not node_path:
            raise RuntimeError(
                "Photon is enabled but Node.js is not available. Rebuild the Docker image with --nodejs "
                "or set interactive_apps.plugins.builtin.photon.settings.node_bin "
                "to a Node.js executable."
            )
        if not PHOTON_SIDECAR_DIR.joinpath("node_modules").is_dir():
            raise RuntimeError(
                "Photon sidecar dependencies are not installed. Run `npm ci` in "
                f"{PHOTON_SIDECAR_DIR} before enabling Photon."
            )
        env = build_photon_sidecar_environment(
            os.environ,
            {
                "PHOTON_PROJECT_ID": project_id,
                "PHOTON_PROJECT_SECRET": project_secret,
                "PHOTON_SIDECAR_BIND": self.config.sidecar_bind_host,
                "PHOTON_SIDECAR_PORT": str(self.config.sidecar_port),
                "PHOTON_SIDECAR_TOKEN": self._sidecar_token or "",
                "PHOTON_SIDECAR_WATCH_STDIN": "1",
                "PHOTON_MAX_INLINE_ATTACHMENT_BYTES": str(
                    self.config.max_inline_attachment_bytes
                ),
                "PHOTON_TELEMETRY": "true" if self.config.telemetry else "false",
            },
        )
        process = await asyncio.create_subprocess_exec(
            node_path,
            str(PHOTON_SIDECAR_DIR / "index.mjs"),
            cwd=str(PHOTON_SIDECAR_DIR),
            env=env,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
        )
        self._sidecar_process = process
        self._sidecar_log_task = asyncio.create_task(
            self._log_sidecar_output(process), name="photon_sidecar_logs"
        )
        await self._wait_for_sidecar()

    async def _wait_for_sidecar(self) -> None:
        deadline = asyncio.get_running_loop().time() + 15.0
        last_error: Optional[Exception] = None
        while asyncio.get_running_loop().time() < deadline:
            if (
                self._sidecar_process is not None
                and self._sidecar_process.returncode is not None
            ):
                raise RuntimeError(
                    f"Photon sidecar exited with code {self._sidecar_process.returncode}."
                )
            try:
                await self._sidecar_call("/healthz", {})
                return
            except Exception as exc:
                last_error = exc
                await asyncio.sleep(0.25)
        raise RuntimeError(
            f"Photon sidecar did not become healthy within 15 seconds: {last_error}"
        )

    async def _stop_sidecar(self) -> None:
        process = self._sidecar_process
        if process is None:
            return
        with suppress(Exception):
            await self._sidecar_call("/shutdown", {})
        if process.stdin is not None and not process.stdin.is_closing():
            process.stdin.close()
        try:
            await asyncio.wait_for(process.wait(), timeout=5.0)
        except asyncio.TimeoutError:
            process.terminate()
            with suppress(asyncio.TimeoutError):
                await asyncio.wait_for(process.wait(), timeout=3.0)
        if process.returncode is None:
            process.kill()
            await process.wait()
        if self._sidecar_log_task is not None:
            self._sidecar_log_task.cancel()
            with suppress(asyncio.CancelledError):
                await self._sidecar_log_task
            self._sidecar_log_task = None
        self._sidecar_process = None

    async def _log_sidecar_output(self, process: asyncio.subprocess.Process) -> None:
        if process.stdout is None:
            return
        while True:
            line = await process.stdout.readline()
            if not line:
                return
            logger.info("Photon sidecar: %s", line.decode("utf-8", "replace").rstrip())

    async def _inbound_loop(self) -> None:
        backoff_seconds = 1.0
        while True:
            try:
                await self._consume_inbound_stream()
                backoff_seconds = 1.0
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.warning(
                    "Photon inbound stream failed; retrying after %.1fs.",
                    backoff_seconds,
                    exc_info=True,
                )
                await asyncio.sleep(backoff_seconds)
                backoff_seconds = min(backoff_seconds * 2, MAX_INBOUND_BACKOFF_SECONDS)

    def _spawn_background_task(
        self,
        awaitable: Coroutine[Any, Any, Any],
        *,
        name: str,
    ) -> None:
        task = asyncio.create_task(awaitable, name=name)
        self._background_tasks.add(task)
        task.add_done_callback(self._on_background_task_done)

    def _on_background_task_done(self, task: asyncio.Task[Any]) -> None:
        self._background_tasks.discard(task)
        if task.cancelled():
            return
        error = task.exception()
        if error is None:
            return
        logger.error(
            "Photon inbound message processing failed; inbound stream remains active: task=%s.",
            task.get_name(),
            exc_info=(type(error), error, error.__traceback__),
        )

    async def _consume_inbound_stream(self) -> None:
        if self._http_session is None:
            raise RuntimeError("Photon app is not running.")
        async with self._http_session.get(
            f"{self._sidecar_base_url}/inbound",
            headers={PHOTON_SIDECAR_TOKEN_HEADER: self._sidecar_token or ""},
            timeout=aiohttp.ClientTimeout(total=None, sock_read=None),
        ) as response:
            if response.status >= 400:
                body = await response.text()
                raise PhotonSidecarError(
                    f"Photon sidecar inbound stream failed HTTP {response.status}: {body[:1000]}"
                )
            async for raw_line in response.content:
                line = raw_line.decode("utf-8", "replace").strip()
                if not line:
                    continue
                try:
                    event = json.loads(line)
                except json.JSONDecodeError:
                    logger.warning(
                        "Ignoring invalid Photon inbound JSON: %r", line[:500]
                    )
                    continue
                if not isinstance(event, dict):
                    logger.warning(
                        "Ignoring non-object Photon inbound JSON: %r", line[:500]
                    )
                    continue
                message_id = _normalized_string(event.get("messageId"))
                self._spawn_background_task(
                    self._process_inbound_event(event),
                    name=f"photon_inbound_event_{message_id or 'unknown'}",
                )

    async def _process_inbound_event(self, event: dict[str, Any]) -> None:
        raw_space = event.get("space")
        raw_sender = event.get("sender")
        raw_content = event.get("content")
        space: dict[str, Any] = raw_space if isinstance(raw_space, dict) else {}
        sender: dict[str, Any] = raw_sender if isinstance(raw_sender, dict) else {}
        content: dict[str, Any] = raw_content if isinstance(raw_content, dict) else {}
        message_id = _normalized_string(event.get("messageId"))
        if self._is_duplicate_message(message_id):
            return

        chat_id = _normalized_string(space.get("id"))
        if not chat_id:
            logger.warning("Ignoring Photon message without space.id.")
            return
        chat_type = (
            "group"
            if _normalized_string(space.get("type")).lower() == "group"
            else "dm"
        )
        user_id = (
            _normalized_string(sender.get("id"))
            or _normalized_string(space.get("phone"))
            or chat_id
        )
        if chat_type == "group" and not self._is_allowed_group(
            chat_id=chat_id, user_id=user_id
        ):
            return
        if chat_type != "group" and not self._is_allowed_sender(
            chat_id=chat_id, user_id=user_id
        ):
            return

        session = self.application.resolve_session(
            channel_name=self.channel_name,
            user_id=user_id,
            chat_id=chat_id,
            thread_id=None,
        )
        try:
            text, attachments = await self._normalize_inbound_content(
                content,
                session_key=session.session_key,
                conversation_id=session.conversation_id,
            )
        except AttachmentValidationError as exc:
            await self._send_text(chat_id=chat_id, text=str(exc))
            return
        except Exception as exc:
            logger.warning(
                "Photon attachment handling failed: user_id=%s chat_id=%s message_id=%s.",
                user_id,
                chat_id,
                message_id or "-",
                exc_info=True,
            )
            await self._send_text(
                chat_id=chat_id, text=f"Attachment handling failed: {exc}"
            )
            return
        if chat_type == "group" and self.config.group_policy == "mention":
            stripped_text = self._strip_group_mention(text, group_id=chat_id)
            if stripped_text is None:
                return
            text = stripped_text
        if not text and not attachments:
            logger.debug(
                "Ignoring contentless Photon message: user_id=%s chat_id=%s.",
                user_id,
                chat_id,
            )
            return

        activity_context = ActivityContext(
            chat_id=chat_id, message_id=message_id or None
        )
        handled = await self._maybe_handle_gateway_command(
            text=text,
            user_id=user_id,
            chat_id=chat_id,
            message_id=message_id or None,
            activity_context=activity_context,
            attachments=attachments,
        )
        if handled:
            return

        inbound = ChannelInboundMessage(
            channel_name=self.channel_name,
            user_id=user_id,
            chat_id=chat_id,
            thread_id=None,
            text=text,
            message_id=message_id or None,
            session_key=session.session_key,
            attachments=attachments,
        )
        logger.info(
            "Photon inbound to manager: user_id=%s chat_id=%s group=%s message_id=%s session=%s attachment_count=%d preview=%r",
            inbound.user_id,
            inbound.chat_id,
            chat_type == "group",
            inbound.message_id or "-",
            session.session_key,
            len(attachments),
            _preview_text(inbound.text),
        )
        try:
            messages = await self.run_with_activity_indicator(
                context=activity_context,
                awaitable=self.bridge.handle_inbound(inbound),
            )
        except TimeoutError:
            await self.send_timeout_notice(
                context=activity_context,
                timeout_seconds=self.request_timeout_seconds,
            )
            return
        for outbound in messages:
            await self._send_outbound(outbound)

    async def _maybe_handle_gateway_command(
        self,
        *,
        text: str,
        user_id: str,
        chat_id: str,
        message_id: Optional[str],
        activity_context: ActivityContext,
        attachments: list[UserTaskAttachment],
    ) -> bool:
        if not text.startswith("/"):
            return False
        if attachments:
            await self._send_text(
                chat_id=chat_id, text="Slash commands cannot include attachments."
            )
            return True
        try:
            outbound = await self.run_with_activity_indicator(
                context=activity_context,
                awaitable=self._handle_gateway_command(
                    raw_text=text,
                    user_id=user_id,
                    chat_id=chat_id,
                    message_id=message_id,
                ),
            )
        except TimeoutError:
            await self.send_timeout_notice(
                context=activity_context,
                timeout_seconds=self.request_timeout_seconds,
            )
            return True
        if outbound is not None:
            await self._send_outbound(outbound)
            return True
        return False

    async def _handle_gateway_command(
        self,
        *,
        raw_text: str,
        user_id: str,
        chat_id: str,
        message_id: Optional[str],
    ) -> Optional[ChannelOutboundMessage]:
        result = await self.application.execute_interactive_command(
            raw_text=raw_text,
            channel_name=self.channel_name,
            user_id=user_id,
            chat_id=chat_id,
            thread_id=None,
            help_text=PHOTON_HELP_TEXT,
            enable_reply_command=False,
        )
        if not result.handled:
            return None
        return ChannelOutboundMessage(
            channel_name=self.channel_name,
            chat_id=chat_id,
            text=result.text or "",
            in_reply_to_message_id=message_id,
            session_key=result.session_key,
            suppress_delivery=result.deferred,
        )

    async def _normalize_inbound_content(
        self,
        content: dict[str, Any],
        *,
        session_key: str,
        conversation_id: Optional[str],
    ) -> tuple[str, list[UserTaskAttachment]]:
        ctype = _normalized_string(content.get("type")).lower()
        if ctype == "text":
            return _normalized_string(content.get("text")), []
        if ctype in {"attachment", "voice"}:
            return await self._store_photon_attachment_content(
                content,
                session_key=session_key,
                conversation_id=conversation_id,
            )
        if ctype == "group":
            text_parts: list[str] = []
            attachments: list[UserTaskAttachment] = []
            items = content.get("items")
            for item in items if isinstance(items, list) else []:
                item_content = item.get("content") if isinstance(item, dict) else None
                if not isinstance(item_content, dict):
                    continue
                item_type = _normalized_string(item_content.get("type")).lower()
                if item_type == "text":
                    item_text = _normalized_string(item_content.get("text"))
                    if item_text:
                        text_parts.append(item_text)
                    continue
                if item_type in {"attachment", "voice"}:
                    marker, stored = await self._store_photon_attachment_content(
                        item_content,
                        session_key=session_key,
                        conversation_id=conversation_id,
                    )
                    attachments.extend(stored)
                    if marker and not stored:
                        text_parts.append(marker)
            text = "\n".join(part for part in text_parts if part).strip()
            if not text and attachments:
                text = "User sent attachment(s)."
            return text, attachments
        # Spectrum emits reactions and read receipts on the message stream,
        # but neither event contains a user prompt for the manager.
        if ctype in {"reaction", "read"}:
            return "", []
        return f"[Photon content type not handled: {ctype or 'unknown'}]", []

    async def _store_photon_attachment_content(
        self,
        payload: dict[str, Any],
        *,
        session_key: str,
        conversation_id: Optional[str],
    ) -> tuple[str, list[UserTaskAttachment]]:
        is_voice = _normalized_string(payload.get("type")).lower() == "voice"
        filename = Path(
            _normalized_string(payload.get("name"))
            or ("voice.m4a" if is_voice else "photon_attachment")
        ).name
        content_type = _normalized_string(payload.get("mimeType")) or (
            "audio/mp4" if is_voice else "application/octet-stream"
        )
        data = _normalized_string(payload.get("data"))
        if not data or _normalized_string(payload.get("encoding")).lower() != "base64":
            label = "voice" if is_voice else "attachment"
            return f"[Photon {label} received: {filename} ({content_type})]", []
        stored = self.attachments.store_bytes(
            data=base64.b64decode(data),
            filename=filename,
            content_type=content_type,
            session_key=session_key,
            conversation_id=conversation_id,
            origin="photon",
            caption=None,
        )
        return "(voice)" if is_voice else "(attachment)", [stored.attachment]

    def _is_duplicate_message(self, message_id: str) -> bool:
        if not message_id:
            return False
        if message_id in self._seen_message_ids:
            logger.debug(
                "Ignoring duplicate Photon message: message_id=%s.", message_id
            )
            return True
        self._seen_message_ids.add(message_id)
        self._seen_message_queue.append(message_id)
        while len(self._seen_message_queue) > MAX_DEDUPED_MESSAGE_IDS:
            expired = self._seen_message_queue.popleft()
            self._seen_message_ids.discard(expired)
        return False

    def _warn_if_allow_all(self) -> None:
        if self.config.allowed_sender_ids or self.config.allowed_group_ids:
            return
        logger.warning(
            "Photon interactive app is enabled with empty allowed_sender_ids and allowed_group_ids; "
            "all direct Photon senders reachable through the configured project can start sessions until "
            "an allowlist is configured."
        )

    def _is_allowed_sender(self, *, chat_id: str, user_id: str) -> bool:
        if self.is_registered_gateway_user(user_id):
            return True
        if not self.config.allowed_sender_ids:
            return True
        candidates = {chat_id, user_id}
        if candidates.intersection(self.config.allowed_sender_ids):
            return True
        logger.info(
            "Ignoring Photon message from non-allowed sender: chat_id=%s user_id=%s newuser_target=%s.",
            chat_id,
            user_id,
            user_id,
        )
        return False

    def _is_allowed_group(self, *, chat_id: str, user_id: str) -> bool:
        if not self.config.allowed_group_ids:
            logger.debug(
                "Ignoring Photon group message because allowed_group_ids is empty: chat_id=%s user_id=%s.",
                chat_id,
                user_id,
            )
            return False
        if chat_id in self.config.allowed_group_ids:
            return True
        logger.info(
            "Ignoring Photon message from non-allowed group: chat_id=%s user_id=%s newuser_target=%s.",
            chat_id,
            user_id,
            user_id,
        )
        return False

    def _strip_group_mention(self, text: str, *, group_id: str) -> Optional[str]:
        if not self.config.mention_markers:
            if not self._mention_warning_emitted:
                logger.warning(
                    "Photon group_policy=mention but mention_markers is empty; ignoring group messages. group_id=%s",
                    group_id,
                )
                self._mention_warning_emitted = True
            return None
        rendered = text
        matched = False
        for marker in self.config.mention_markers:
            if not marker:
                continue
            lower_marker = marker.lower()
            lower_rendered = rendered.lower()
            if lower_marker not in lower_rendered:
                continue
            matched = True
            index = lower_rendered.find(lower_marker)
            rendered = rendered[:index] + rendered[index + len(marker) :]
        if not matched:
            logger.debug(
                "Ignoring Photon group message without a configured mention marker: group_id=%s.",
                group_id,
            )
            return None
        return rendered.strip()

    async def _send_outbound(self, outbound: ChannelOutboundMessage) -> None:
        if outbound.suppress_delivery:
            return
        if not outbound.chat_id:
            logger.warning("Photon outbound message missing chat_id.")
            return
        await self._send_text(chat_id=outbound.chat_id, text=outbound.text)

    async def _send_text(self, *, chat_id: str, text: str) -> None:
        if not text:
            return
        payload: dict[str, Any] = {"spaceId": chat_id, "text": text}
        if self.config.markdown:
            payload["format"] = "markdown"
        await self._sidecar_call("/send", payload)

    async def _send_attachment(
        self, *, chat_id: str, attachment: UserNotificationAttachment
    ) -> None:
        """Send the checked bytes of a staged copy through the sidecar.

        The sidecar reads a path, so it gets a private 0600 copy of the bytes
        the gateway checked, removed once the call returns (the sidecar reads
        the file before it answers). A copy that changed or became a link
        after staging is skipped, and the reader logs why.
        """

        name = attachment.filename or Path(attachment.path).name
        try:
            with self.outgoing_attachments.private_copy(attachment) as private_path:
                payload = {
                    "spaceId": chat_id,
                    "path": str(private_path),
                    "name": name,
                    "mimeType": _content_type_for_path(name, attachment.content_type),
                    "caption": attachment.caption or "",
                    "kind": _attachment_kind(attachment),
                }
                await self._sidecar_call("/send-attachment", payload)
        except StagedAttachmentUnavailable:
            return

    async def _sidecar_call(self, path: str, payload: dict[str, Any]) -> dict[str, Any]:
        if self._http_session is None:
            raise RuntimeError("Photon app is not running.")
        async with self._http_session.post(
            f"{self._sidecar_base_url}{path}",
            headers={PHOTON_SIDECAR_TOKEN_HEADER: self._sidecar_token or ""},
            json=payload,
            timeout=aiohttp.ClientTimeout(total=self.config.request_timeout_seconds),
        ) as response:
            body = await response.text()
            if response.status >= 400:
                raise PhotonSidecarError(
                    f"Photon sidecar {path} failed HTTP {response.status}: {body[:1000]}"
                )
            if not body:
                return {}
            try:
                data = json.loads(body)
            except json.JSONDecodeError as exc:
                raise PhotonSidecarError(
                    f"Photon sidecar {path} returned invalid JSON: {body[:1000]}"
                ) from exc
            if isinstance(data, dict) and data.get("ok") is False:
                raise PhotonSidecarError(
                    f"Photon sidecar {path} returned error: {data.get('error') or data}"
                )
            return data if isinstance(data, dict) else {}
