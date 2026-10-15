"""Twilio-backed WhatsApp interactive app adapter built on top of the gateway bridge."""

from __future__ import annotations

import asyncio
import logging
import mimetypes
from dataclasses import dataclass
from typing import Optional

from assistant_gateway.application.gateway_application import GatewayDeliveryEvent
from runtime_settings.interactive_apps import WhatsAppAppSettings
from runtime_hosting.aiohttp import start_aiohttp_site
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
from custom_messages.contracts.runtime_v1 import DELIVERY_CHANNEL_WHATSAPP
from env_secrets import get_env_with_file_fallback

try:
    import aiohttp
    from aiohttp import web
except (
    ImportError
):  # pragma: no cover - exercised in environments without whatsapp extras
    aiohttp = None
    web = None

try:
    from twilio.request_validator import RequestValidator
    from twilio.rest import Client as TwilioClient
except (
    ImportError
):  # pragma: no cover - exercised in environments without whatsapp extras
    RequestValidator = None
    TwilioClient = None


logger = logging.getLogger(__name__)

TWILIO_ACCOUNT_SID_ENV_VAR = "TWILIO_ACCOUNT_SID"
TWILIO_AUTH_TOKEN_ENV_VAR = "TWILIO_AUTH_TOKEN"
TWILIO_WHATSAPP_FROM_ENV_VAR = "TWILIO_WHATSAPP_FROM"
WHATSAPP_HELP_TEXT = build_interactive_session_help(
    include_reply=False,
    trailer="Other messages go to the manager. Reminder phrases like `remind me every 2 hours to ...` also work.",
)


@dataclass(frozen=True)
class _WhatsAppInboundMedia:
    url: str
    filename: str
    content_type: str


def _configure_twilio_library_logging() -> None:
    logging.getLogger("twilio.http_client").setLevel(logging.WARNING)
    logging.getLogger("urllib3.connectionpool").setLevel(logging.WARNING)


def _normalize_whatsapp_address(value: Optional[str]) -> str:
    normalized = str(value or "").strip()
    if normalized.lower().startswith("whatsapp:"):
        normalized = normalized.split(":", 1)[1].strip()
    return normalized


def _format_whatsapp_address(value: str) -> str:
    normalized = _normalize_whatsapp_address(value)
    if not normalized:
        return ""
    return (
        normalized
        if normalized.lower().startswith("whatsapp:")
        else f"whatsapp:{normalized}"
    )


class WhatsAppInteractiveApp(BaseInteractiveApp):
    channel_name = DELIVERY_CHANNEL_WHATSAPP

    def __init__(
        self,
        config: WhatsAppAppSettings,
        application: InteractiveAppApplication,
        bridge: InteractiveAppBridge,
        runtime: InteractiveAppRuntimeContext,
    ) -> None:
        self.config = config
        self.application = application
        self.bridge = bridge
        self.request_timeout_seconds = runtime.request_timeout_seconds
        self.attachments = runtime.attachments
        self._client = None
        self._validator = None
        self._account_sid = ""
        self._auth_token = ""
        self._from_address = ""
        self._web_app = None
        self._runner = None
        self._site = None
        self._hosted_site = None
        self._background_tasks: set[asyncio.Task] = set()

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
        if web is None or TwilioClient is None or RequestValidator is None:
            raise RuntimeError(
                "WhatsApp support requires aiohttp and twilio. Install requirements.txt again."
            )

        account_sid = get_env_with_file_fallback(TWILIO_ACCOUNT_SID_ENV_VAR)
        auth_token = get_env_with_file_fallback(TWILIO_AUTH_TOKEN_ENV_VAR)
        from_address = get_env_with_file_fallback(TWILIO_WHATSAPP_FROM_ENV_VAR)
        if not account_sid:
            raise RuntimeError(
                f"WhatsApp is enabled but env var {TWILIO_ACCOUNT_SID_ENV_VAR} is not set."
            )
        if not auth_token:
            raise RuntimeError(
                f"WhatsApp is enabled but env var {TWILIO_AUTH_TOKEN_ENV_VAR} is not set."
            )
        if not from_address:
            raise RuntimeError(
                f"WhatsApp is enabled but env var {TWILIO_WHATSAPP_FROM_ENV_VAR} is not set."
            )

        _configure_twilio_library_logging()
        self._client = TwilioClient(account_sid, auth_token)
        self._validator = RequestValidator(auth_token)
        self._account_sid = account_sid
        self._auth_token = auth_token
        self._from_address = _format_whatsapp_address(from_address)

        self._web_app = web.Application()
        self._web_app.router.add_post(self.config.webhook_path, self._handle_webhook)
        self._hosted_site = await start_aiohttp_site(
            self._web_app,
            host=self.config.webhook_host,
            port=self.config.webhook_port,
            web_module=web,
        )
        self._runner = self._hosted_site.runner
        self._site = self._hosted_site.site
        logger.info(
            "WhatsApp webhook listener active on %s%s.",
            self._hosted_site.base_url,
            self.config.webhook_path,
        )
        if self.config.public_base_url:
            logger.info(
                "Configure the Twilio WhatsApp webhook URL to %s%s.",
                self.config.public_base_url,
                self.config.webhook_path,
            )
        elif self.config.validate_twilio_signature:
            logger.warning(
                "WhatsApp signature validation uses the incoming request URL. "
                "If you run behind a reverse proxy or tunnel, set "
                "interactive_apps.plugins.builtin.whatsapp.settings.public_base_url "
                "to the externally visible HTTPS base URL so Twilio signature validation stays stable."
            )

    async def stop(self) -> None:
        for task in list(self._background_tasks):
            task.cancel()
        if self._background_tasks:
            await asyncio.gather(*self._background_tasks, return_exceptions=True)
        self._background_tasks.clear()
        if self._hosted_site is not None:
            await self._hosted_site.stop()
            self._hosted_site = None
        self._site = None
        self._runner = None
        self._web_app = None
        self._client = None
        self._validator = None
        self._account_sid = ""
        self._auth_token = ""
        self._from_address = ""

    async def deliver_event(self, event: GatewayDeliveryEvent) -> None:
        await self._send_outbound(
            project_flattened_delivery(event, channel_name=self.channel_name)
        )

    async def _handle_webhook(self, request):
        if web is None:
            raise RuntimeError("aiohttp is not installed.")
        form_data = await request.post()
        payload = {key: str(value) for key, value in form_data.items()}
        if self.config.validate_twilio_signature and not self._is_valid_signature(
            request, payload
        ):
            logger.warning("Rejected WhatsApp webhook with invalid Twilio signature.")
            return web.Response(status=403, text="invalid signature")
        self._spawn_background_task(
            self._process_inbound_payload(payload),
            name="whatsapp_inbound",
        )
        return web.Response(status=204)

    def _is_valid_signature(self, request, payload: dict[str, str]) -> bool:
        if not self.config.validate_twilio_signature:
            return True
        if self._validator is None:
            raise RuntimeError("WhatsApp app is not running.")
        signature = request.headers.get("X-Twilio-Signature", "")
        if not signature:
            return False
        request_url = self._resolve_request_url(request)
        try:
            return bool(self._validator.validate(request_url, payload, signature))
        except Exception:
            logger.warning(
                "WhatsApp Twilio signature validation failed unexpectedly for url=%s.",
                request_url,
                exc_info=True,
            )
            return False

    def _resolve_request_url(self, request) -> str:
        if self.config.public_base_url:
            return f"{self.config.public_base_url}{request.path_qs}"
        return str(request.url)

    def _spawn_background_task(self, coro, *, name: str) -> None:
        task = asyncio.create_task(coro, name=name)
        self._background_tasks.add(task)
        task.add_done_callback(self._background_tasks.discard)

    async def _process_inbound_payload(self, payload: dict[str, str]) -> None:
        raw_from = payload.get("From", "")
        chat_id = _normalize_whatsapp_address(raw_from)
        external_user_id = _normalize_whatsapp_address(payload.get("ExternalUserId"))
        wa_id = _normalize_whatsapp_address(payload.get("WaId"))
        user_id = chat_id or external_user_id or wa_id or "whatsapp_unknown"
        message_id = str(payload.get("MessageSid", "")).strip() or None
        text = (payload.get("Body") or payload.get("ButtonText") or "").strip()
        media_items = self._extract_media_items(payload)
        if not chat_id:
            logger.warning(
                "Ignoring WhatsApp webhook without a routable From address: message_id=%s external_user_id=%s wa_id=%s.",
                message_id or "-",
                external_user_id or "-",
                wa_id or "-",
            )
            return
        if not self._is_allowed(
            chat_id=chat_id,
            user_id=user_id,
            external_user_id=external_user_id,
            wa_id=wa_id,
        ):
            return
        if not text and not media_items:
            logger.info(
                "Ignoring WhatsApp message without text body or media: chat_id=%s user_id=%s message_id=%s.",
                chat_id,
                user_id,
                message_id or "-",
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
            attachments = await self._store_media_items(
                media_items,
                session_key=session.session_key,
                conversation_id=session.conversation_id,
                caption=text or None,
            )
        except AttachmentValidationError as exc:
            await self._send_text(chat_id=chat_id, text=str(exc))
            return
        except Exception as exc:
            logger.warning(
                "WhatsApp media download failed: chat_id=%s user_id=%s message_id=%s.",
                chat_id,
                user_id,
                message_id or "-",
                exc_info=True,
            )
            await self._send_text(
                chat_id=chat_id, text=f"Attachment download failed: {exc}"
            )
            return
        if attachments and text.startswith("/"):
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
                if text.startswith("/") and not attachments
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
            attachments=attachments,
        )
        logger.info(
            "WhatsApp inbound to manager: user_id=%s chat_id=%s message_id=%s session=%s attachment_count=%d external_user_id=%s wa_id=%s preview=%r",
            inbound.user_id,
            inbound.chat_id,
            inbound.message_id or "-",
            session.session_key,
            len(attachments),
            external_user_id or "-",
            wa_id or "-",
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

    def _extract_media_items(
        self, payload: dict[str, str]
    ) -> list[_WhatsAppInboundMedia]:
        try:
            count = int(str(payload.get("NumMedia") or "0").strip() or "0")
        except ValueError:
            count = 0
        items: list[_WhatsAppInboundMedia] = []
        for index in range(max(0, count)):
            url = str(payload.get(f"MediaUrl{index}") or "").strip()
            if not url:
                continue
            content_type = str(payload.get(f"MediaContentType{index}") or "").strip()
            media_sid = str(payload.get(f"MediaSid{index}") or "").strip()
            filename = str(
                payload.get(f"MediaFilename{index}") or ""
            ).strip() or self._media_filename(
                message_id=str(payload.get("MessageSid") or "whatsapp_media"),
                media_sid=media_sid,
                index=index,
                content_type=content_type,
            )
            items.append(
                _WhatsAppInboundMedia(
                    url=url, filename=filename, content_type=content_type
                )
            )
        return items

    def _media_filename(
        self, *, message_id: str, media_sid: str, index: int, content_type: str
    ) -> str:
        suffix = (
            mimetypes.guess_extension(
                str(content_type or "").split(";", 1)[0].strip().lower()
            )
            or ""
        )
        stem = media_sid or f"{message_id}_{index}"
        return f"{stem}{suffix or '.bin'}"

    async def _download_twilio_media(
        self, media: _WhatsAppInboundMedia
    ) -> tuple[bytes, str]:
        if aiohttp is None:
            raise RuntimeError(
                "WhatsApp media download requires aiohttp. Install requirements.txt again."
            )
        if not self._account_sid or not self._auth_token:
            raise RuntimeError("WhatsApp app is not running.")
        timeout = aiohttp.ClientTimeout(
            total=max(5.0, float(self.request_timeout_seconds))
        )
        auth = aiohttp.BasicAuth(self._account_sid, self._auth_token)
        async with aiohttp.ClientSession(timeout=timeout, auth=auth) as session:
            async with session.get(media.url) as response:
                if response.status >= 400:
                    body = await response.text()
                    raise RuntimeError(
                        f"Twilio media download failed with HTTP {response.status}: {body[:500]}"
                    )
                content_type = media.content_type or response.headers.get(
                    "Content-Type", ""
                )
                return await response.read(), content_type

    async def _store_media_items(
        self,
        media_items: list[_WhatsAppInboundMedia],
        *,
        session_key: str,
        conversation_id: Optional[str],
        caption: Optional[str],
    ):
        attachments = []
        for media in media_items:
            data, content_type = await self._download_twilio_media(media)
            stored = self.attachments.store_bytes(
                data=data,
                filename=media.filename,
                content_type=content_type or media.content_type,
                session_key=session_key,
                conversation_id=conversation_id,
                origin="whatsapp",
                caption=caption,
            )
            attachments.append(stored.attachment)
        return attachments

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
            help_text=WHATSAPP_HELP_TEXT,
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

    def _warn_if_allow_all(self) -> None:
        if self.config.allowed_sender_ids:
            return
        logger.warning(
            "WhatsApp interactive app is enabled with empty allowed_sender_ids; "
            "all Twilio-validated WhatsApp senders can start sessions until an "
            "allowlist is configured."
        )

    def _is_allowed(
        self,
        *,
        chat_id: str,
        user_id: str,
        external_user_id: str,
        wa_id: str,
    ) -> bool:
        candidates = {
            value for value in {chat_id, user_id, external_user_id, wa_id} if value
        }
        if any(self.is_registered_gateway_user(candidate) for candidate in candidates):
            return True
        if not self.config.allowed_sender_ids:
            return True
        if candidates.intersection(self.config.allowed_sender_ids):
            return True
        newuser_target = user_id or external_user_id or chat_id or wa_id
        logger.info(
            "Ignoring WhatsApp message from non-allowed sender: chat_id=%s user_id=%s external_user_id=%s "
            "wa_id=%s newuser_target=%s.",
            chat_id,
            user_id,
            external_user_id or "-",
            wa_id or "-",
            newuser_target,
        )
        return False

    async def _send_outbound(self, outbound: ChannelOutboundMessage) -> None:
        if outbound.suppress_delivery:
            return
        if not outbound.chat_id:
            logger.warning("WhatsApp outbound message missing chat_id.")
            return
        # Access reviews carry the Inbox link in their text; this channel has no buttons.
        await self._send_text(chat_id=outbound.chat_id, text=outbound.text)
        logger.info(
            "WhatsApp outbound to user: chat_id=%s session=%s conversation=%s preview=%r",
            outbound.chat_id,
            outbound.session_key or "-",
            outbound.conversation_id or "-",
            preview_text(outbound.text),
        )

    async def _send_text(
        self,
        *,
        chat_id: str,
        text: str,
    ) -> None:
        if self._client is None:
            raise RuntimeError("WhatsApp app is not running.")
        rendered = (text or "").strip()
        if not rendered:
            logger.warning(
                "Skipping empty WhatsApp outbound message for chat_id=%s.", chat_id
            )
            return
        to_address = _format_whatsapp_address(chat_id)
        if not to_address:
            logger.warning("WhatsApp outbound target is empty after normalization.")
            return

        def _send() -> None:
            self._client.messages.create(
                body=rendered,
                from_=self._from_address,
                to=to_address,
            )

        await asyncio.to_thread(_send)
