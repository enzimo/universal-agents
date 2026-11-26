"""Signal interactive app adapter backed by signal-cli-rest-api."""

from __future__ import annotations

import asyncio
import base64
import binascii
from collections import deque
from contextlib import suppress
from dataclasses import dataclass
import logging
import mimetypes
import re
from typing import Any, Optional
from urllib.parse import quote

from assistant_gateway.application.gateway_application import GatewayDeliveryEvent
from runtime_settings.interactive_apps import SignalAppSettings
from assistant_gateway.inbound_attachments import (
    AttachmentValidationError,
    StoredInboundAttachment,
    validate_raw_attachment_filename,
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
ATTACHMENT_DOWNLOAD_CHUNK_BYTES = 64 * 1024
# How much of a failed attachment response is read for the error message; the
# rest is never read so a huge error body cannot exhaust memory.
ATTACHMENT_ERROR_BODY_PREVIEW_BYTES = 1000
MAX_SIGNAL_ATTACHMENT_ID_LENGTH = 256
# Signal attachment ids are numeric or base64url keys plus an optional extension.
_SIGNAL_ATTACHMENT_ID_PATTERN = re.compile(r"[A-Za-z0-9_=-][A-Za-z0-9._=-]{0,255}")


class SignalRestError(RuntimeError):
    """Raised when signal-cli-rest-api returns an unsuccessful response."""


class _SignalAttachmentTooLargeError(AttachmentValidationError):
    """Raised when an attachment exceeds the configured upload byte limit."""

    def __init__(self, message: str, *, observed_bytes: int | None = None) -> None:
        super().__init__(message)
        self.observed_bytes = observed_bytes


@dataclass(frozen=True)
class _SignalInboundAttachment:
    attachment_id: str | None
    filename: str
    content_type: str
    base64_data: str | None = None
    declared_size_bytes: int | None = None


def _normalized_string(value: Any) -> str:
    return str(value or "").strip()


def _parse_declared_attachment_size(raw: dict[str, Any]) -> int | None:
    """Parse signal-cli's declared attachment ``size`` in bytes when valid."""

    value = raw.get("size")
    if isinstance(value, bool) or not isinstance(value, int):
        return None
    return value if value >= 0 else None


def _parse_content_length(header_value: str | None) -> int | None:
    text = str(header_value or "").strip()
    if text.isascii() and text.isdigit():
        return int(text)
    return None


def _is_valid_signal_attachment_id(attachment_id: str) -> bool:
    if not attachment_id or len(attachment_id) > MAX_SIGNAL_ATTACHMENT_ID_LENGTH:
        return False
    if ".." in attachment_id:
        return False
    return _SIGNAL_ATTACHMENT_ID_PATTERN.fullmatch(attachment_id) is not None


def _log_safe_attachment_id(attachment_id: str | None) -> str:
    if not attachment_id:
        return "-"
    return re.sub(r"[\x00-\x1f\x7f]+", "_", attachment_id)[:128]


def _check_base64_encoded_size(
    encoded: str, *, max_bytes: int, filename: str
) -> str:
    """Check encoded base64 payload size before decoding and return compact text."""

    max_compact_len = ((max_bytes + 2) // 3) * 4
    # Reject huge whitespace-padded strings in O(1) before allocating a copy.
    if len(encoded) > max_compact_len * 2 + 1024:
        raise _SignalAttachmentTooLargeError(
            f"{filename} exceeds the configured upload limit.",
            observed_bytes=None,
        )
    compact = "".join(encoded.split())
    unpadded_len = len(compact.rstrip("="))
    estimated_bytes = (unpadded_len * 3) // 4
    if estimated_bytes > max_bytes:
        raise _SignalAttachmentTooLargeError(
            f"{filename} exceeds the configured upload limit.",
            observed_bytes=estimated_bytes,
        )
    return compact


def _decode_compact_base64(
    compact: str, *, max_bytes: int, filename: str
) -> bytes:
    """Decode pre-checked compact base64 strictly within ``max_bytes``."""

    try:
        data = base64.b64decode(compact, validate=True)
    except (ValueError, binascii.Error) as exc:
        raise AttachmentValidationError(
            f"{filename} contains invalid base64 attachment data."
        ) from exc
    if len(data) > max_bytes:
        raise _SignalAttachmentTooLargeError(
            f"{filename} exceeds the configured upload limit.",
            observed_bytes=len(data),
        )
    return data


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

    async def serve_attachment(
        self, attachment: str, *, max_bytes: int
    ) -> tuple[bytes, str]:
        return await self._request_bytes(
            "GET",
            f"/v1/attachments/{quote(attachment, safe='')}",
            max_bytes=max_bytes,
        )

    async def _request_bytes(
        self,
        method: str,
        path: str,
        *,
        max_bytes: int,
        params: Optional[dict[str, Any]] = None,
        timeout_seconds: Optional[float] = None,
    ) -> tuple[bytes, str]:
        if aiohttp is None:
            raise RuntimeError(
                "Signal support requires aiohttp. Install requirements.txt again."
            )
        limit = max(1, int(max_bytes))
        timeout = aiohttp.ClientTimeout(
            total=float(timeout_seconds or self.request_timeout_seconds)
        )
        url = f"{self.base_url}{path}"
        async with self.session.request(
            method, url, params=params, timeout=timeout, allow_redirects=False
        ) as response:
            if response.status < 200 or response.status >= 300:
                preview = await response.content.read(
                    ATTACHMENT_ERROR_BODY_PREVIEW_BYTES
                )
                response.close()
                body = preview.decode("utf-8", errors="replace")
                raise SignalRestError(
                    f"signal-cli-rest-api returned HTTP {response.status}: {body}"
                )
            content_length = _parse_content_length(
                response.headers.get("Content-Length")
            )
            if content_length is not None and content_length > limit:
                response.close()
                raise _SignalAttachmentTooLargeError(
                    f"Signal attachment exceeds configured upload limit ({content_length} > {limit} bytes).",
                    observed_bytes=content_length,
                )
            chunk_size = min(ATTACHMENT_DOWNLOAD_CHUNK_BYTES, limit + 1)
            buffer = bytearray()
            async for chunk in response.content.iter_chunked(chunk_size):
                if not chunk:
                    continue
                if len(buffer) + len(chunk) > limit:
                    observed = len(buffer) + len(chunk)
                    response.close()
                    raise _SignalAttachmentTooLargeError(
                        f"Signal attachment exceeds configured upload limit (>{limit} bytes).",
                        observed_bytes=observed,
                    )
                buffer.extend(chunk)
            return bytes(buffer), response.headers.get("Content-Type", "")

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
        if attachments and text.startswith("/"):
            # Refuse before fetching so a refused command downloads nothing.
            await self._send_text(
                chat_id=chat_id, text="Slash commands cannot include attachments."
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
                sender_id=user_id,
            )
        except AttachmentValidationError as exc:
            await self._send_text(chat_id=chat_id, text=str(exc))
            return
        except Exception:
            logger.warning(
                "Signal attachment download failed: user_id=%s chat_id=%s message_id=%s.",
                user_id,
                chat_id,
                message_id or "-",
                exc_info=True,
            )
            await self._send_text(
                chat_id=chat_id, text="Attachment download failed."
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
                        private_chat=not is_group,
                    ),
                )
                if text.startswith("/")
                else await self._handle_gateway_command(
                    raw_text=text,
                    user_id=user_id,
                    chat_id=chat_id,
                    message_id=message_id,
                    private_chat=not is_group,
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
        if not isinstance(raw, dict):
            return _SignalInboundAttachment(
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
        filename = (
            _normalized_string(raw.get("fileName"))
            or _normalized_string(raw.get("filename"))
            or _normalized_string(raw.get("name"))
        )
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
        declared_size_bytes = _parse_declared_attachment_size(raw)
        if not filename:
            suffix = (
                mimetypes.guess_extension(content_type.split(";", 1)[0].strip().lower())
                or ".bin"
            )
            stem_source = (
                attachment_id
                if _is_valid_signal_attachment_id(attachment_id)
                else f"signal_attachment_{index}"
            )
            filename = f"{_safe_signal_filename_stem(stem_source)}{suffix}"
        return _SignalInboundAttachment(
            attachment_id=attachment_id or None,
            filename=filename,
            content_type=content_type,
            base64_data=base64_data or None,
            declared_size_bytes=declared_size_bytes,
        )

    def _log_attachment_refusal(
        self,
        *,
        reason: str,
        attachment_id: str | None,
        sender_id: str,
        size_bytes: int | None = None,
        max_bytes: int | None = None,
    ) -> None:
        logger.warning(
            "Signal attachment refused: event=signal_attachment_refused reason=%s "
            "attachment_id=%s sender=%s size_bytes=%s max_bytes=%s.",
            reason,
            _log_safe_attachment_id(attachment_id),
            sender_id or "-",
            size_bytes if size_bytes is not None else "-",
            max_bytes if max_bytes is not None else "-",
        )

    def _attachment_refusal(
        self,
        *,
        reason: str,
        message: str,
        item: _SignalInboundAttachment,
        sender_id: str,
        max_bytes: int,
        size_bytes: int | None = None,
    ) -> AttachmentValidationError:
        self._log_attachment_refusal(
            reason=reason,
            attachment_id=item.attachment_id,
            sender_id=sender_id,
            size_bytes=size_bytes,
            max_bytes=max_bytes,
        )
        return AttachmentValidationError(message)

    def _validate_attachment_before_read(
        self,
        item: _SignalInboundAttachment,
        *,
        max_bytes: int,
        sender_id: str,
    ) -> str | None:
        if not item.base64_data and not item.attachment_id:
            raise self._attachment_refusal(
                reason="missing_source",
                message=f"{item.filename} does not include an attachment id or inline data.",
                item=item,
                sender_id=sender_id,
                max_bytes=max_bytes,
            )
        if "/" in item.filename or "\\" in item.filename or "\x00" in item.filename:
            raise self._attachment_refusal(
                reason="invalid_filename",
                message=f"{item.filename} is not an allowed attachment filename.",
                item=item,
                sender_id=sender_id,
                max_bytes=max_bytes,
            )
        try:
            validate_raw_attachment_filename(item.filename)
        except AttachmentValidationError:
            self._log_attachment_refusal(
                reason="invalid_filename",
                attachment_id=item.attachment_id,
                sender_id=sender_id,
                max_bytes=max_bytes,
            )
            raise
        if not item.base64_data and not _is_valid_signal_attachment_id(
            item.attachment_id or ""
        ):
            raise self._attachment_refusal(
                reason="invalid_attachment_id",
                message=f"{item.filename} has an invalid attachment id.",
                item=item,
                sender_id=sender_id,
                max_bytes=max_bytes,
            )
        if item.declared_size_bytes is not None and item.declared_size_bytes > max_bytes:
            raise self._attachment_refusal(
                reason="declared_size_exceeds_limit",
                message=f"{item.filename} exceeds the configured upload limit.",
                item=item,
                sender_id=sender_id,
                size_bytes=item.declared_size_bytes,
                max_bytes=max_bytes,
            )
        if item.base64_data:
            try:
                return _check_base64_encoded_size(
                    item.base64_data, max_bytes=max_bytes, filename=item.filename
                )
            except _SignalAttachmentTooLargeError as exc:
                raise self._attachment_refusal(
                    reason="base64_size_exceeds_limit",
                    message=f"{item.filename} exceeds the configured upload limit.",
                    item=item,
                    sender_id=sender_id,
                    size_bytes=exc.observed_bytes,
                    max_bytes=max_bytes,
                ) from exc
        return None

    async def _attachment_bytes(
        self,
        item: _SignalInboundAttachment,
        *,
        compact_base64: str | None,
        max_bytes: int,
        sender_id: str,
        position: int,
    ) -> tuple[bytes, str]:
        if compact_base64 is not None:
            try:
                data = _decode_compact_base64(
                    compact_base64, max_bytes=max_bytes, filename=item.filename
                )
            except _SignalAttachmentTooLargeError as exc:
                raise self._attachment_refusal(
                    reason="base64_size_exceeds_limit",
                    message=f"{item.filename} exceeds the configured upload limit.",
                    item=item,
                    sender_id=sender_id,
                    size_bytes=exc.observed_bytes,
                    max_bytes=max_bytes,
                ) from exc
            except AttachmentValidationError:
                self._log_attachment_refusal(
                    reason="invalid_base64",
                    attachment_id=item.attachment_id,
                    sender_id=sender_id,
                    max_bytes=max_bytes,
                )
                raise
            return data, item.content_type
        assert item.attachment_id is not None
        if self._client is None:
            raise RuntimeError("Signal app is not running.")
        try:
            data, content_type = await self._client.serve_attachment(
                item.attachment_id, max_bytes=max_bytes
            )
        except _SignalAttachmentTooLargeError as exc:
            raise self._attachment_refusal(
                reason="download_size_exceeds_limit",
                message=f"{item.filename} exceeds the configured upload limit.",
                item=item,
                sender_id=sender_id,
                size_bytes=exc.observed_bytes,
                max_bytes=max_bytes,
            ) from exc
        except Exception as exc:
            logger.warning(
                "Signal attachment download failed: "
                "event=signal_attachment_download_failed attachment_id=%s "
                "position=%d error_type=%s sender=%s.",
                _log_safe_attachment_id(item.attachment_id),
                position,
                type(exc).__name__,
                sender_id or "-",
            )
            raise
        if len(data) > max_bytes:
            raise self._attachment_refusal(
                reason="download_size_exceeds_limit",
                message=f"{item.filename} exceeds the configured upload limit.",
                item=item,
                sender_id=sender_id,
                size_bytes=len(data),
                max_bytes=max_bytes,
            )
        return data, content_type

    async def _store_signal_attachments(
        self,
        raw_attachments: list[Any],
        *,
        session_key: str,
        conversation_id: Optional[str],
        caption: Optional[str],
        sender_id: str,
    ) -> list[Any]:
        max_bytes = self.attachments.max_file_bytes
        items = [
            self._normalize_signal_attachment(raw, index=index)
            for index, raw in enumerate(raw_attachments)
        ]
        # Validate every attachment before fetching or decoding any bytes.
        validated = [
            (
                item,
                self._validate_attachment_before_read(
                    item, max_bytes=max_bytes, sender_id=sender_id
                ),
            )
            for item in items
        ]
        # Fetch, check and save one attachment at a time so a message never holds
        # more than one payload in memory, and stop at the first refusal. Files
        # are saved unregistered and registered only after every attachment of
        # the message passed, so a refused or failed attachment leaves none of
        # the message's files behind (all or nothing, like
        # adapter_host/gateway_application.py).
        staged: list[StoredInboundAttachment] = []
        registered: list[Any] = []
        fetched_ids: set[str] = set()
        try:
            for position, (item, compact_base64) in enumerate(validated):
                if compact_base64 is None and item.attachment_id is not None:
                    if item.attachment_id in fetched_ids:
                        # A repeated id names the same file; fetching it again
                        # would only multiply downloads and disk use.
                        logger.info(
                            "Skipping repeated Signal attachment: "
                            "event=signal_attachment_duplicate_skipped "
                            "attachment_id=%s position=%d sender=%s.",
                            _log_safe_attachment_id(item.attachment_id),
                            position,
                            sender_id or "-",
                        )
                        continue
                    fetched_ids.add(item.attachment_id)
                staged.append(
                    await self._fetch_and_stage_attachment(
                        item,
                        compact_base64=compact_base64,
                        max_bytes=max_bytes,
                        sender_id=sender_id,
                        position=position,
                        session_key=session_key,
                        caption=caption,
                    )
                )
            while staged:
                self.attachments.register_staged(
                    staged[0],
                    session_key=session_key,
                    conversation_id=conversation_id,
                )
                registered.append(staged.pop(0).attachment)
        finally:
            for leftover in staged:
                self.attachments.discard_staged(leftover)
        return registered

    async def _fetch_and_stage_attachment(
        self,
        item: _SignalInboundAttachment,
        *,
        compact_base64: str | None,
        max_bytes: int,
        sender_id: str,
        position: int,
        session_key: str,
        caption: Optional[str],
    ) -> StoredInboundAttachment:
        data, content_type = await self._attachment_bytes(
            item,
            compact_base64=compact_base64,
            max_bytes=max_bytes,
            sender_id=sender_id,
            position=position,
        )
        resolved_content_type = content_type or item.content_type
        try:
            self.attachments.check_bytes(
                data=data,
                filename=item.filename,
                content_type=resolved_content_type,
            )
        except AttachmentValidationError:
            self._log_attachment_refusal(
                reason="policy_rejected",
                attachment_id=item.attachment_id,
                sender_id=sender_id,
                size_bytes=len(data),
                max_bytes=max_bytes,
            )
            raise
        return self.attachments.stage_bytes(
            data=data,
            filename=item.filename,
            content_type=resolved_content_type,
            session_key=session_key,
            origin="signal",
            caption=caption,
        )

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
        private_chat: bool,
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
            # A Signal message without group info is a one-to-one chat.
            private_chat=private_chat,
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
