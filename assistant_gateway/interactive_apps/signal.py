"""Signal interactive app adapter backed by signal-cli-rest-api."""

from __future__ import annotations

import asyncio
import base64
from collections import deque
from contextlib import suppress
from dataclasses import dataclass
import logging
import mimetypes
from pathlib import Path
import re
from typing import Any, Optional
from urllib.parse import quote

from assistant_gateway.application.gateway_application import GatewayDeliveryEvent
from runtime_settings.interactive_apps import SignalAppSettings
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
from assistant_gateway.interactive_apps.delivery_projection import (
    preview_text,
    project_flattened_delivery,
)
from assistant_gateway.translation import ChannelInboundMessage, ChannelOutboundMessage
from custom_messages.contracts.runtime_v1 import DELIVERY_CHANNEL_SIGNAL

try:
    import aiohttp
except ImportError:  # pragma: no cover - aiohttp is part of requirements.txt
    aiohttp = None


logger = logging.getLogger(__name__)

SIGNAL_HELP_TEXT = build_interactive_session_help(
    include_reply=False,
    trailer="Other messages go to the manager. Reminder phrases like `remind me every 2 hours to ...` also work.",
)
MAX_DEDUPED_MESSAGE_KEYS = 1024
MAX_RECEIVE_BACKOFF_SECONDS = 30.0


class SignalRestError(RuntimeError):
    """Raised when signal-cli-rest-api returns an unsuccessful response."""


@dataclass(frozen=True)
class _SignalInboundAttachment:
    raw: Any
    attachment_id: str | None
    filename: str
    content_type: str
    local_path: str | None = None
    base64_data: str | None = None


def _normalized_string(value: Any) -> str:
    return str(value or "").strip()


def _extract_signal_messages(payload: Any) -> list[dict[str, Any]]:
    if isinstance(payload, list):
        return [item for item in payload if isinstance(item, dict)]
    if not isinstance(payload, dict):
        return []
    if isinstance(payload.get("messages"), list):
        return [item for item in payload["messages"] if isinstance(item, dict)]
    if isinstance(payload.get("envelope"), dict):
        return [payload]
    return []


def _signal_envelope(message: dict[str, Any]) -> dict[str, Any]:
    envelope = message.get("envelope")
    return envelope if isinstance(envelope, dict) else message


def _render_signal_mentions(text: str, mentions: Any) -> str:
    if not text or "\ufffc" not in text or not isinstance(mentions, list):
        return text
    rendered = text
    sorted_mentions = sorted(
        [mention for mention in mentions if isinstance(mention, dict)],
        key=lambda mention: int(mention.get("start") or 0),
        reverse=True,
    )
    for mention in sorted_mentions:
        start = int(mention.get("start") or 0)
        length = int(mention.get("length") or 1)
        identifier = (
            _normalized_string(mention.get("number"))
            or _normalized_string(mention.get("uuid"))
            or "user"
        )
        if start < 0 or start > len(rendered):
            continue
        rendered = (
            rendered[:start] + f"@{identifier}" + rendered[start + max(length, 1) :]
        )
    return rendered


def _safe_signal_filename_stem(value: str) -> str:
    rendered = re.sub(r"[^A-Za-z0-9_.-]+", "_", str(value or "").strip()).strip("._-")
    return rendered or "signal_attachment"


class SignalRestClient:
    def __init__(
        self,
        *,
        base_url: str,
        session: Any,
        request_timeout_seconds: float,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.session = session
        self.request_timeout_seconds = float(request_timeout_seconds)

    async def about(self) -> dict[str, Any]:
        payload = await self._request_json("GET", "/v1/about")
        return payload if isinstance(payload, dict) else {}

    async def receive(
        self,
        *,
        account: str,
        timeout_seconds: float,
        ignore_attachments: bool,
        ignore_stories: bool,
        ignore_avatars: bool,
        ignore_stickers: bool,
        max_messages: int,
        send_read_receipts: bool,
    ) -> Any:
        request_timeout = max(self.request_timeout_seconds, timeout_seconds + 5.0)
        return await self._request_json(
            "GET",
            f"/v1/receive/{quote(account, safe='')}",
            params={
                "timeout": timeout_seconds,
                "ignore_attachments": str(bool(ignore_attachments)).lower(),
                "ignore_stories": str(bool(ignore_stories)).lower(),
                "ignore_avatars": str(bool(ignore_avatars)).lower(),
                "ignore_stickers": str(bool(ignore_stickers)).lower(),
                "max_messages": int(max_messages),
                "send_read_receipts": str(bool(send_read_receipts)).lower(),
            },
            timeout_seconds=request_timeout,
        )

    async def send(
        self,
        *,
        account: str,
        recipients: list[str],
        message: str,
        text_mode: str,
    ) -> dict[str, Any]:
        payload = await self._request_json(
            "POST",
            "/v2/send",
            json_body={
                "number": account,
                "recipients": recipients,
                "message": message,
                "text_mode": text_mode,
            },
        )
        return payload if isinstance(payload, dict) else {}

    async def serve_attachment(self, attachment: str) -> tuple[bytes, str]:
        return await self._request_bytes(
            "GET",
            f"/v1/attachments/{quote(attachment, safe='')}",
        )

    async def _request_bytes(
        self,
        method: str,
        path: str,
        *,
        params: Optional[dict[str, Any]] = None,
        timeout_seconds: Optional[float] = None,
    ) -> tuple[bytes, str]:
        if aiohttp is None:
            raise RuntimeError(
                "Signal support requires aiohttp. Install requirements.txt again."
            )
        timeout = aiohttp.ClientTimeout(
            total=float(timeout_seconds or self.request_timeout_seconds)
        )
        url = f"{self.base_url}{path}"
        async with self.session.request(
            method, url, params=params, timeout=timeout
        ) as response:
            if response.status >= 400:
                body = await response.text()
                raise SignalRestError(
                    f"signal-cli-rest-api returned HTTP {response.status}: {body[:1000]}"
                )
            return await response.read(), response.headers.get("Content-Type", "")

    async def _request_json(
        self,
        method: str,
        path: str,
        *,
        params: Optional[dict[str, Any]] = None,
        json_body: Optional[dict[str, Any]] = None,
        timeout_seconds: Optional[float] = None,
    ) -> Any:
        if aiohttp is None:
            raise RuntimeError(
                "Signal support requires aiohttp. Install requirements.txt again."
            )
        timeout = aiohttp.ClientTimeout(
            total=float(timeout_seconds or self.request_timeout_seconds)
        )
        url = f"{self.base_url}{path}"
        async with self.session.request(
            method,
            url,
            params=params,
            json=json_body,
            timeout=timeout,
        ) as response:
            if response.status >= 400:
                body = await response.text()
                raise SignalRestError(
                    f"signal-cli-rest-api returned HTTP {response.status}: {body[:1000]}"
                )
            if response.status == 204:
                return {}
            try:
                return await response.json(content_type=None)
            except Exception as exc:
                body = await response.text()
                raise SignalRestError(
                    f"signal-cli-rest-api returned invalid JSON: {body[:1000]}"
                ) from exc


class SignalInteractiveApp(BaseInteractiveApp):
    channel_name = DELIVERY_CHANNEL_SIGNAL

    def __init__(
        self,
        config: SignalAppSettings,
        application: InteractiveAppApplication,
        bridge: InteractiveAppBridge,
        runtime: InteractiveAppRuntimeContext,
    ) -> None:
        self.config = config
        self.application = application
        self.bridge = bridge
        self.request_timeout_seconds = runtime.request_timeout_seconds
        self.attachments = runtime.attachments
        self._http_session: Any = None
        self._client: Optional[SignalRestClient] = None
        self._poll_task: Optional[asyncio.Task] = None
        self._background_tasks: set[asyncio.Task] = set()
        self._seen_message_keys: set[tuple[str, str, str]] = set()
        self._seen_message_queue: deque[tuple[str, str, str]] = deque()
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

    async def start(self) -> None:
        if not self.enabled:
            return
        self._warn_if_allow_all()
        if aiohttp is None:
            raise RuntimeError(
                "Signal support requires aiohttp. Install requirements.txt again."
            )
        if not self.config.account:
            raise RuntimeError(
                "Signal is enabled but "
                "interactive_apps.plugins.builtin.signal.settings.account is not set."
            )
        if not self.config.base_url:
            raise RuntimeError(
                "Signal is enabled but "
                "interactive_apps.plugins.builtin.signal.settings.base_url is not set."
            )
        if (
            self.config.allowed_group_ids
            and self.config.group_policy == "mention"
            and not self.config.mention_markers
        ):
            raise RuntimeError(
                "Signal groups are enabled with group_policy='mention', but "
                "interactive_apps.plugins.builtin.signal.settings.mention_markers is empty."
            )

        self._http_session = aiohttp.ClientSession()
        self._client = SignalRestClient(
            base_url=self.config.base_url,
            session=self._http_session,
            request_timeout_seconds=self.config.request_timeout_seconds,
        )
        await self._client.about()
        if self.config.drop_pending_messages_on_startup:
            await self._drop_pending_messages_on_startup()
        self._poll_task = asyncio.create_task(
            self._poll_loop(), name="signal_interactive_app"
        )
        logger.info(
            "Signal adapter polling %s for account=%s.",
            self.config.base_url,
            self.config.account,
        )

    async def stop(self) -> None:
        if self._poll_task is not None:
            self._poll_task.cancel()
            with suppress(asyncio.CancelledError):
                await self._poll_task
            self._poll_task = None
        for task in list(self._background_tasks):
            task.cancel()
        if self._background_tasks:
            await asyncio.gather(*self._background_tasks, return_exceptions=True)
        self._background_tasks.clear()
        if self._http_session is not None:
            await self._http_session.close()
            self._http_session = None
        self._client = None

    async def deliver_event(self, event: GatewayDeliveryEvent) -> None:
        await self._send_outbound(
            project_flattened_delivery(event, channel_name=self.channel_name)
        )

    async def _drop_pending_messages_on_startup(self) -> None:
        if self._client is None:
            return
        try:
            payload = await self._client.receive(
                account=self.config.account,
                timeout_seconds=1.0,
                ignore_attachments=self.config.ignore_attachments,
                ignore_stories=self.config.ignore_stories,
                ignore_avatars=self.config.ignore_avatars,
                ignore_stickers=self.config.ignore_stickers,
                max_messages=self.config.max_messages_per_poll,
                send_read_receipts=False,
            )
        except Exception:
            logger.warning(
                "Failed to drop pending Signal messages on startup.", exc_info=True
            )
            return
        dropped = len(_extract_signal_messages(payload))
        if dropped:
            logger.warning(
                "Dropped %d pending Signal message(s) on startup because "
                "interactive_apps.plugins.builtin.signal.settings."
                "drop_pending_messages_on_startup is true.",
                dropped,
            )

    async def _poll_loop(self) -> None:
        backoff_seconds = 1.0
        while True:
            try:
                if self._client is None:
                    raise RuntimeError("Signal app is not running.")
                payload = await self._client.receive(
                    account=self.config.account,
                    timeout_seconds=self.config.poll_timeout_seconds,
                    ignore_attachments=self.config.ignore_attachments,
                    ignore_stories=self.config.ignore_stories,
                    ignore_avatars=self.config.ignore_avatars,
                    ignore_stickers=self.config.ignore_stickers,
                    max_messages=self.config.max_messages_per_poll,
                    send_read_receipts=self.config.send_read_receipts,
                )
                backoff_seconds = 1.0
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.warning(
                    "Signal receive poll failed; retrying after %.1fs.",
                    backoff_seconds,
                    exc_info=True,
                )
                await asyncio.sleep(backoff_seconds)
                backoff_seconds = min(backoff_seconds * 2, MAX_RECEIVE_BACKOFF_SECONDS)
                continue

            messages = _extract_signal_messages(payload)
            if not messages:
                await asyncio.sleep(self.config.poll_interval_seconds)
                continue
            for message in messages:
                self._spawn_background_task(
                    self._process_inbound_payload(message), name="signal_inbound"
                )

    def _spawn_background_task(self, coro, *, name: str) -> None:
        task = asyncio.create_task(coro, name=name)
        self._background_tasks.add(task)
        task.add_done_callback(self._background_tasks.discard)

    async def _process_inbound_payload(self, payload: dict[str, Any]) -> None:
        envelope = _signal_envelope(payload)
        data_message = envelope.get("dataMessage")
        if not isinstance(data_message, dict):
            logger.debug("Ignoring Signal envelope without dataMessage.")
            return

        source_number = _normalized_string(envelope.get("sourceNumber"))
        source_uuid = _normalized_string(envelope.get("sourceUuid"))
        source = _normalized_string(envelope.get("source"))
        source_name = _normalized_string(envelope.get("sourceName"))
        source_candidates = {
            value for value in {source_number, source_uuid, source} if value
        }
        if self.config.account in source_candidates:
            logger.debug(
                "Ignoring Signal self message from account=%s.", self.config.account
            )
            return

        group_info = data_message.get("groupInfo")
        group_info = group_info if isinstance(group_info, dict) else {}
        group_id = _normalized_string(group_info.get("groupId"))
        is_group = bool(group_id)
        user_id = source_uuid or source_number or source or "signal_unknown"
        chat_id = group_id if is_group else source_number or source or source_uuid
        message_timestamp = _normalized_string(
            data_message.get("timestamp")
        ) or _normalized_string(envelope.get("timestamp"))
        message_id = message_timestamp or None
        if not chat_id:
            logger.warning(
                "Ignoring Signal message without a routable chat id: user_id=%s timestamp=%s.",
                user_id,
                message_timestamp or "-",
            )
            return
        if self._is_duplicate_message(
            chat_id=chat_id, user_id=user_id, message_id=message_timestamp
        ):
            return
        if is_group and not self._is_allowed_group(group_id=group_id, user_id=user_id):
            return
        if not is_group and not self._is_allowed_sender(
            chat_id=chat_id, user_id=user_id, source_candidates=source_candidates
        ):
            return

        text = _normalized_string(data_message.get("message"))
        text = _render_signal_mentions(text, data_message.get("mentions"))
        if is_group and self.config.group_policy == "mention":
            stripped_text = self._strip_group_mention(text, group_id=group_id)
            if stripped_text is None:
                return
            text = stripped_text

        attachments = data_message.get("attachments")
        attachments = attachments if isinstance(attachments, list) else []
        if not text and not attachments:
            logger.debug(
                "Ignoring contentless Signal message: user_id=%s chat_id=%s.",
                user_id,
                chat_id,
            )
            return

        activity_context = ActivityContext(
            chat_id=chat_id,
            message_id=message_id,
        )
        session = self.application.resolve_session(
            channel_name=self.channel_name,
            user_id=user_id,
            chat_id=chat_id,
            thread_id=None,
        )
        try:
            stored_attachments = await self._store_signal_attachments(
                attachments,
                session_key=session.session_key,
                conversation_id=session.conversation_id,
                caption=text or None,
            )
        except AttachmentValidationError as exc:
            await self._send_text(chat_id=chat_id, text=str(exc))
            return
        except Exception as exc:
            logger.warning(
                "Signal attachment download failed: user_id=%s chat_id=%s message_id=%s.",
                user_id,
                chat_id,
                message_id or "-",
                exc_info=True,
            )
            await self._send_text(
                chat_id=chat_id, text=f"Attachment download failed: {exc}"
            )
            return
        if stored_attachments and text.startswith("/"):
            await self._send_text(
                chat_id=chat_id, text="Slash commands cannot include attachments."
            )
            return
        try:
            handled = (
                await self.run_with_activity_indicator(
                    context=activity_context,
                    awaitable=self._handle_gateway_command(
                        raw_text=text,
                        user_id=user_id,
                        chat_id=chat_id,
                        message_id=message_id,
                    ),
                )
                if text.startswith("/") and not stored_attachments
                else await self._handle_gateway_command(
                    raw_text=text,
                    user_id=user_id,
                    chat_id=chat_id,
                    message_id=message_id,
                )
            )
        except TimeoutError:
            await self.send_timeout_notice(
                context=activity_context,
                timeout_seconds=self.request_timeout_seconds,
            )
            return
        if handled is not None:
            await self._send_outbound(handled)
            return

        inbound = ChannelInboundMessage(
            channel_name=self.channel_name,
            user_id=user_id,
            chat_id=chat_id,
            thread_id=None,
            text=text,
            message_id=message_id,
            session_key=session.session_key,
            attachments=stored_attachments,
        )
        logger.info(
            "Signal inbound to manager: user_id=%s chat_id=%s group=%s message_id=%s session=%s attachment_count=%d source_name=%s preview=%r",
            inbound.user_id,
            inbound.chat_id,
            group_id or "-",
            inbound.message_id or "-",
            session.session_key,
            len(stored_attachments),
            source_name or "-",
            preview_text(inbound.text),
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

    def _normalize_signal_attachment(
        self, raw: Any, *, index: int
    ) -> _SignalInboundAttachment:
        if isinstance(raw, str):
            path = raw if Path(raw).exists() else None
            filename = Path(raw).name if path else f"signal_attachment_{index}"
            return _SignalInboundAttachment(
                raw=raw,
                attachment_id=None if path else raw,
                filename=filename,
                content_type="application/octet-stream",
                local_path=path,
            )
        if not isinstance(raw, dict):
            return _SignalInboundAttachment(
                raw=raw,
                attachment_id=None,
                filename=f"signal_attachment_{index}",
                content_type="application/octet-stream",
            )
        content_type = (
            _normalized_string(raw.get("contentType"))
            or _normalized_string(raw.get("content_type"))
            or _normalized_string(raw.get("mimeType"))
            or _normalized_string(raw.get("mime_type"))
            or "application/octet-stream"
        )
        local_path = (
            _normalized_string(raw.get("path"))
            or _normalized_string(raw.get("localPath"))
            or _normalized_string(raw.get("storedFilename"))
            or _normalized_string(raw.get("stored_filename"))
        )
        filename = (
            _normalized_string(raw.get("fileName"))
            or _normalized_string(raw.get("filename"))
            or _normalized_string(raw.get("name"))
        )
        if local_path and not filename:
            filename = Path(local_path).name
        if filename and Path(filename).is_absolute() and not local_path:
            local_path = filename
            filename = Path(filename).name
        attachment_id = (
            _normalized_string(raw.get("id"))
            or _normalized_string(raw.get("attachmentId"))
            or _normalized_string(raw.get("attachment_id"))
            or _normalized_string(raw.get("digest"))
        )
        base64_data = (
            _normalized_string(raw.get("base64"))
            or _normalized_string(raw.get("data"))
            or _normalized_string(raw.get("base64Data"))
        )
        if not filename:
            suffix = (
                mimetypes.guess_extension(content_type.split(";", 1)[0].strip().lower())
                or ".bin"
            )
            filename = f"{_safe_signal_filename_stem(attachment_id or 'signal_attachment_' + str(index))}{suffix}"
        return _SignalInboundAttachment(
            raw=raw,
            attachment_id=attachment_id,
            filename=filename,
            content_type=content_type,
            local_path=local_path or None,
            base64_data=base64_data or None,
        )

    async def _attachment_bytes(
        self, item: _SignalInboundAttachment
    ) -> tuple[bytes, str]:
        if item.base64_data:
            return base64.b64decode(item.base64_data), item.content_type
        if item.local_path and Path(item.local_path).exists():
            return Path(item.local_path).read_bytes(), item.content_type
        if item.attachment_id:
            if self._client is None:
                raise RuntimeError("Signal app is not running.")
            return await self._client.serve_attachment(item.attachment_id)
        raise RuntimeError(
            f"Signal attachment {item.filename} did not include retrievable bytes."
        )

    async def _store_signal_attachments(
        self,
        raw_attachments: list[Any],
        *,
        session_key: str,
        conversation_id: Optional[str],
        caption: Optional[str],
    ):
        attachments = []
        for index, raw in enumerate(raw_attachments):
            item = self._normalize_signal_attachment(raw, index=index)
            data, content_type = await self._attachment_bytes(item)
            stored = self.attachments.store_bytes(
                data=data,
                filename=item.filename,
                content_type=content_type or item.content_type,
                session_key=session_key,
                conversation_id=conversation_id,
                origin="signal",
                caption=caption,
            )
            attachments.append(stored.attachment)
        return attachments

    def _is_duplicate_message(
        self, *, chat_id: str, user_id: str, message_id: str
    ) -> bool:
        if not message_id:
            return False
        key = (chat_id, user_id, message_id)
        if key in self._seen_message_keys:
            logger.debug(
                "Ignoring duplicate Signal message: chat_id=%s user_id=%s message_id=%s.",
                chat_id,
                user_id,
                message_id,
            )
            return True
        self._seen_message_keys.add(key)
        self._seen_message_queue.append(key)
        while len(self._seen_message_queue) > MAX_DEDUPED_MESSAGE_KEYS:
            expired = self._seen_message_queue.popleft()
            self._seen_message_keys.discard(expired)
        return False

    def _warn_if_allow_all(self) -> None:
        if self.config.allowed_sender_ids or self.config.allowed_group_ids:
            return
        logger.warning(
            "Signal interactive app is enabled with empty allowed_sender_ids and "
            "allowed_group_ids; all direct Signal senders reachable through the "
            "configured account can start sessions until an allowlist is configured."
        )

    def _is_allowed_sender(
        self, *, chat_id: str, user_id: str, source_candidates: set[str]
    ) -> bool:
        if self.is_registered_gateway_user(user_id):
            return True
        if not self.config.allowed_sender_ids:
            return True
        candidates = {chat_id, user_id, *source_candidates}
        if candidates.intersection(self.config.allowed_sender_ids):
            return True
        logger.info(
            "Ignoring Signal message from non-allowed sender: chat_id=%s user_id=%s candidates=%s newuser_target=%s.",
            chat_id,
            user_id,
            sorted(candidate for candidate in candidates if candidate),
            user_id,
        )
        return False

    def _is_allowed_group(self, *, group_id: str, user_id: str) -> bool:
        if not self.config.allowed_group_ids:
            logger.debug(
                "Ignoring Signal group message because allowed_group_ids is empty: group_id=%s user_id=%s.",
                group_id,
                user_id,
            )
            return False
        if group_id in self.config.allowed_group_ids:
            return True
        logger.info(
            "Ignoring Signal message from non-allowed group: group_id=%s user_id=%s newuser_target=%s.",
            group_id,
            user_id,
            user_id,
        )
        return False

    def _strip_group_mention(self, text: str, *, group_id: str) -> Optional[str]:
        if not self.config.mention_markers:
            if not self._mention_warning_emitted:
                logger.warning(
                    "Signal group_policy=mention but mention_markers is empty; ignoring group messages. group_id=%s",
                    group_id,
                )
                self._mention_warning_emitted = True
            return None
        matched = False
        rendered = text
        for marker in self.config.mention_markers:
            if not marker:
                continue
            pattern = re.compile(re.escape(marker), flags=re.IGNORECASE)
            if pattern.search(rendered):
                matched = True
                rendered = pattern.sub("", rendered)
        if not matched:
            logger.debug(
                "Ignoring Signal group message without a configured mention marker: group_id=%s.",
                group_id,
            )
            return None
        return rendered.strip()

    async def _handle_gateway_command(
        self,
        *,
        raw_text: str,
        user_id: str,
        chat_id: str,
        message_id: Optional[str],
    ) -> Optional[ChannelOutboundMessage]:
        command = (raw_text or "").strip()
        if not command.startswith("/"):
            return None

        result = await self.application.execute_interactive_command(
            raw_text=command,
            channel_name=self.channel_name,
            user_id=user_id,
            chat_id=chat_id,
            thread_id=None,
            help_text=SIGNAL_HELP_TEXT,
            enable_reply_command=False,
        )
        if result.handled:
            return ChannelOutboundMessage(
                channel_name=self.channel_name,
                chat_id=chat_id,
                text=result.text or "",
                in_reply_to_message_id=message_id,
                session_key=result.session_key,
                suppress_delivery=result.deferred,
            )
        return None

    async def _send_outbound(self, outbound: ChannelOutboundMessage) -> None:
        if outbound.suppress_delivery:
            return
        if not outbound.chat_id:
            logger.warning("Signal outbound message missing chat_id.")
            return
        # Access reviews carry the Inbox link in their text; this channel has no buttons.
        await self._send_text(chat_id=outbound.chat_id, text=outbound.text)
        logger.info(
            "Signal outbound to user: chat_id=%s session=%s conversation=%s preview=%r",
            outbound.chat_id,
            outbound.session_key or "-",
            outbound.conversation_id or "-",
            preview_text(outbound.text),
        )

    async def _send_text(self, *, chat_id: str, text: str) -> None:
        if self._client is None:
            raise RuntimeError("Signal app is not running.")
        rendered = (text or "").strip()
        if not rendered:
            logger.warning(
                "Skipping empty Signal outbound message for chat_id=%s.", chat_id
            )
            return
        await self._client.send(
            account=self.config.account,
            recipients=[chat_id],
            message=rendered,
            text_mode=self.config.text_mode,
        )
