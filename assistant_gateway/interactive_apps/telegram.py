"""Telegram interactive app adapter built on top of the gateway bridge."""

from __future__ import annotations

import html
import io
import json
import logging
import mimetypes
import re
from collections import deque
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Optional

from assistant_gateway.application.gateway_application import GatewayDeliveryEvent
from runtime_settings.common import TELEGRAM_MANAGER_OFFLINE_POLICY_PAUSE_POLLING
from runtime_settings.interactive_apps import TelegramAppSettings
from assistant_gateway.inbound_attachments import (
    AttachmentValidationError,
    attachment_debug_summary,
)
from assistant_gateway.application.access_reviews import (
    ACCESS_REVIEW_CONTROL_PREFIX,
    AccessReviewOutcome,
)
from assistant_gateway.interactive_apps.base import ActivityContext, BaseInteractiveApp, ReviewControl
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
    project_text_delivery,
)
from assistant_gateway.translation import ChannelInboundMessage, ChannelOutboundMessage
from assistant_gateway.interactive_apps.telegram_reply_text import replied_message_text
from assistant_gateway.voice import (
    SpeechRequest,
    TranscriptionRequest,
    VoiceClient,
    should_synthesize_voice_reply,
    speech_text_for_outbound_reply,
    text_for_voice_turn_history,
)
from custom_messages.contracts.replied_message_v1 import RepliedMessage, RepliedMessageAuthor
from custom_messages.contracts.runtime_v1 import DELIVERY_CHANNEL_TELEGRAM
from env_secrets import get_env_with_file_fallback
from id_generation import new_id_suffix

try:
    from telegram import (
        BotCommand,
        InlineKeyboardButton,
        InlineKeyboardMarkup,
        ReplyParameters,
        Update,
    )
    from telegram.constants import ChatAction, ParseMode
    from telegram.ext import (
        Application,
        CallbackQueryHandler,
        CommandHandler,
        ContextTypes,
        MessageHandler,
        filters,
    )
    from telegram.request import HTTPXRequest
except (
    ImportError
):  # pragma: no cover - exercised in environments without telegram extras
    Application = None
    BotCommand = None
    CallbackQueryHandler = None
    ChatAction = None
    CommandHandler = None
    ContextTypes = None
    HTTPXRequest = None
    InlineKeyboardButton = None
    InlineKeyboardMarkup = None
    MessageHandler = None
    ParseMode = None
    ReplyParameters = None
    Update = object
    filters = None


logger = logging.getLogger(__name__)

TELEGRAM_MAX_MESSAGE_LEN = 4000
TELEGRAM_RICH_MESSAGE_MAX_CHARS = 32768
TELEGRAM_SEND_RICH_MESSAGE_ENDPOINT = "sendRichMessage"
TELEGRAM_TYPING_REPEAT_SECONDS = 4.0
TELEGRAM_INBOUND_DEDUPE_LIMIT = 2048
TELEGRAM_BOT_TOKEN_ENV_VAR = "TELEGRAM_BOT_TOKEN"
# Telegram shows at most 200 characters in a button's pop-up answer.
TELEGRAM_CALLBACK_ANSWER_MAX_CHARS = 200
VOICE_UNAVAILABLE_TEXT = "Voice input is not available for this assistant right now. Please send your message as text."
TELEGRAM_COMMAND_SPECS = (
    ("help", "Show available commands"),
    ("docs", "Open documentation"),
    ("tour", "Start the onboarding tour"),
    ("edit", "Open config editor"),
    ("link", "Sign in to a tool or link its account"),
    ("auth", "Check sign-ins and set up team sign-in apps"),
    ("new", "Start a new session"),
    ("stop", "Cancel the current task"),
    ("list_sessions", "List recent sessions"),
    ("list_conversations", "List conversations in this session"),
    ("preview_session", "Preview a session"),
    ("search_session", "Search session history"),
    ("switch_session", "Switch the active session"),
    ("switch_conversation", "Switch the active conversation"),
    ("rename_session", "Rename the current session"),
)
TELEGRAM_COMMANDS = tuple(command for command, _description in TELEGRAM_COMMAND_SPECS)
TELEGRAM_HELP_TEXT = build_interactive_session_help(
    include_reply=False,
    trailer="Other messages go to the manager. Reminder phrases like `remind me every 2 hours to ...` also work.",
)


def _configure_telegram_library_logging() -> None:
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)


def _build_telegram_bot_commands() -> list:
    if BotCommand is None:
        return []
    return [
        BotCommand(command=command, description=description)
        for command, description in TELEGRAM_COMMAND_SPECS
    ]


@dataclass(frozen=True)
class _TelegramVoiceTurnResult:
    request_text: str
    messages: list[ChannelOutboundMessage]


@dataclass(frozen=True)
class _TelegramInboundMedia:
    payload: object
    filename: str
    content_type: str


_MARKDOWN_CODE = re.compile(
    # A closing block fence may be longer than its opener, and EOF closes a block.
    r"^ {0,3}(?P<backticks>`{3,})[^`\n]*\n(?P<backtick_code>.*?)"
    r"(?:^ {0,3}(?P=backticks)`*[ \t]*(?=\n|\Z)|\Z)"
    r"|^ {0,3}(?P<tildes>~{3,})[^\n]*\n(?P<tilde_code>.*?)"
    r"(?:^ {0,3}(?P=tildes)~*[ \t]*(?=\n|\Z)|\Z)"
    r"|(?<!`)(?P<ticks>`+)(?!`)(?P<code>.*?)(?<!`)(?P=ticks)(?!`)",
    re.DOTALL | re.MULTILINE,
)
_MARKDOWN_UNDERLINE = re.compile(
    r"(?:https?://|mailto:|tel:|tg://)[^\s<>\"']+"
    r"|(?<!\w)__(?=\S)(?P<label>[^_\n]+?)__(?!\w|\.\w)"
)
_TELEGRAM_HTML_TOKENS = re.compile(r"</?(?:b|u|code|pre)>|&(?:#x[0-9a-fA-F]+|#[0-9]+|[A-Za-z]+);|[^<&]+|[<&]")


def _underline_prose(text: str) -> str:
    return _MARKDOWN_UNDERLINE.sub(
        lambda match: f"<u>{match['label']}</u>" if match['label'] is not None else match[0], text
    )


def _link_destination_end(text: str, start: int) -> int:
    """Find a link's closing parenthesis, including nested and escaped parentheses."""

    depth = 1
    index = start
    while index < len(text):
        char = text[index]
        if char == "\\":
            index += 2
            continue
        if char == "(":
            depth += 1
        elif char == ")":
            depth -= 1
            if depth == 0:
                return index + 1
        index += 1
    return len(text)


def _underline_labels(text: str) -> str:
    """Use Telegram's underline tags without changing link destinations."""

    parts = []
    start = 0
    while (opening := text.find("](", start)) >= 0:
        destination = opening + 2
        parts.append(_underline_prose(text[start:destination]))
        end = _link_destination_end(text, destination)
        parts.append(text[destination:end])
        start = end
    parts.append(_underline_prose(text[start:]))
    return "".join(parts)


def _format_telegram_markdown(
    text: str, *, prose: Callable[[str, bool], str], code: Callable[[re.Match[str]], str]
) -> str:
    """Format prose separately so emphasis never changes code, including fenced blocks."""

    parts = []
    start = 0
    for match in _MARKDOWN_CODE.finditer(text):
        parts.append(prose(text[start:match.start()], start == 0 or text[start - 1] == "\n"))
        parts.append(code(match))
        start = match.end()
    parts.append(prose(text[start:], start == 0 or text[start - 1] == "\n"))
    return "".join(parts)


def _telegram_prose_html(text: str, at_line_start: bool) -> str:
    value = html.escape(text)
    value = re.sub(r"\*\*(.+?)\*\*", r"<b>\1</b>", value, flags=re.DOTALL)
    value = _underline_labels(value)
    # A segment after inline code can start in the middle of an original line.
    bullet_start = r"(?m)^- " if at_line_start else r"(?<=\n)- "
    value = re.sub(bullet_start, "• ", value)
    return value


def _telegram_code_html(match: re.Match[str]) -> str:
    block = match['backtick_code'] if match['backtick_code'] is not None else match['tilde_code']
    if block is not None:
        value = block.removesuffix("\n")
        return f"<pre><code>{html.escape(value)}</code></pre>"
    return f"<code>{html.escape(match['code'])}</code>"


def _markdown_to_telegram_html(text: str) -> str:
    return _format_telegram_markdown(text or "", prose=_telegram_prose_html, code=_telegram_code_html)


def _split_telegram_html(
    text: str, *, limit: int = TELEGRAM_MAX_MESSAGE_LEN
) -> list[str]:
    """Split rendered HTML with complete tags and entities in every message.

    Formatting the whole reply first preserves code context across messages.
    The raw HTML length is a conservative limit on Telegram's displayed text.
    """

    if not text.strip():
        return [""]
    chunks: list[str] = []
    parts: list[str] = []
    tags: list[str] = []
    length = 0

    def closing_tags() -> str:
        return "".join(f"</{tag}>" for tag in reversed(tags))

    def flush() -> None:
        nonlocal parts, length
        chunks.append("".join(parts) + closing_tags())
        parts = [f"<{tag}>" for tag in tags]
        length = sum(map(len, parts))

    for token in _TELEGRAM_HTML_TOKENS.findall(text.strip()):
        if token.startswith("</"):
            tags.pop()
            parts.append(token)
            length += len(token)
            continue
        if token.startswith("<"):
            tag = token[1:-1]
            if length + len(token) + len(closing_tags()) + len(tag) + 3 > limit:
                flush()
            tags.append(tag)
            parts.append(token)
            length += len(token)
            continue
        while token:
            room = limit - length - len(closing_tags())
            if room <= 0 or (token.startswith("&") and len(token) > room):
                flush()
                continue
            take = min(len(token), room)
            if take < len(token) and not token.startswith("&"):
                boundary = max(token.rfind("\n", 0, take), token.rfind(" ", 0, take))
                if boundary >= 0:
                    take = boundary + 1
            parts.append(token[:take])
            length += take
            token = token[take:]
            if token:
                flush()
    if parts:
        chunks.append("".join(parts))
    return chunks


def _telegram_rich_message_payload(text: str) -> dict[str, str]:
    # Rich Markdown treats __text__ as bold, but supports <u> for underline.
    # Keep its native lists, tables and code rather than using legacy HTML.
    return {"markdown": _format_telegram_markdown(
        text, prose=lambda value, _at_line_start: _underline_labels(value), code=lambda match: match[0]
    )}


def _telegram_reply_parameters_payload(
    reply_to_message_id: Optional[int],
) -> Optional[dict[str, Any]]:
    if reply_to_message_id is None:
        return None
    return {
        "message_id": reply_to_message_id,
        "allow_sending_without_reply": True,
    }


def _exception_text(exc: BaseException) -> str:
    return f"{exc.__class__.__name__}: {exc}".lower()


def _is_rich_capability_error(exc: BaseException) -> bool:
    rendered = _exception_text(exc)
    if "method not found" in rendered or "unknown method" in rendered:
        return True
    endpoint_name = TELEGRAM_SEND_RICH_MESSAGE_ENDPOINT.lower()
    if endpoint_name not in rendered:
        return False
    return any(
        marker in rendered
        for marker in (
            "404",
            "not found",
            "unsupported",
            "does not exist",
        )
    )


def _is_rich_fallback_error(exc: BaseException) -> bool:
    rendered = _exception_text(exc)
    if _is_rich_capability_error(exc):
        return True
    if exc.__class__.__name__ == "BadRequest":
        return True
    return any(
        marker in rendered
        for marker in (
            "bad request",
            "can't parse",
            "cannot parse",
            "entity",
            "invalid rich",
            "markdown",
            "message is too long",
            "message text is empty",
            "parse",
            "rich_message",
            "too long",
        )
    )


class TelegramInteractiveApp(BaseInteractiveApp):
    channel_name = DELIVERY_CHANNEL_TELEGRAM

    def __init__(
        self,
        config: TelegramAppSettings,
        application: InteractiveAppApplication,
        bridge: InteractiveAppBridge,
        runtime: InteractiveAppRuntimeContext,
    ) -> None:
        self.config = config
        self.application = application
        self.bridge = bridge
        self.request_timeout_seconds = runtime.request_timeout_seconds
        self.voice_settings = runtime.voice
        self.attachments = runtime.attachments
        self.outgoing_attachments = runtime.outgoing_attachments
        self._app = None
        self._running = False
        self._polling_active = False
        self._bot_username: Optional[str] = None
        self._bot_user_id: Optional[int] = None
        self._rich_messages_disabled = False
        self._processed_inbound_message_keys: set[tuple[str, Optional[str], str]] = (
            set()
        )
        self._processed_inbound_message_order: deque[tuple[str, Optional[str], str]] = (
            deque()
        )
        self._voice_client: Optional[VoiceClient] = runtime.voice_client

    @property
    def enabled(self) -> bool:
        return bool(self.config.enabled)

    def pauses_inbound_when_manager_unavailable(self) -> bool:
        return (
            self.config.manager_offline_policy
            == TELEGRAM_MANAGER_OFFLINE_POLICY_PAUSE_POLLING
        )

    def uses_native_activity_indicator(self, _context: ActivityContext) -> bool:
        return True

    def native_activity_refresh_interval_seconds(self) -> float:
        return TELEGRAM_TYPING_REPEAT_SECONDS

    async def start(self) -> None:
        if not self.enabled:
            return
        self._warn_if_allow_all()
        if (
            Application is None
            or HTTPXRequest is None
            or filters is None
            or CommandHandler is None
            or CallbackQueryHandler is None
            or MessageHandler is None
        ):
            raise RuntimeError(
                "Telegram support requires python-telegram-bot. Install requirements.txt again."
            )
        token = get_env_with_file_fallback(TELEGRAM_BOT_TOKEN_ENV_VAR)
        if not token:
            raise RuntimeError(
                f"Telegram is enabled but env var {TELEGRAM_BOT_TOKEN_ENV_VAR} is not set."
            )
        _configure_telegram_library_logging()

        api_request = HTTPXRequest(
            connection_pool_size=self.config.connection_pool_size,
            pool_timeout=self.config.pool_timeout,
            connect_timeout=30.0,
            read_timeout=30.0,
            proxy=self.config.proxy,
        )
        poll_request = HTTPXRequest(
            connection_pool_size=4,
            pool_timeout=self.config.pool_timeout,
            connect_timeout=30.0,
            read_timeout=30.0,
            proxy=self.config.proxy,
        )
        self._app = (
            Application.builder()
            .token(token)
            .request(api_request)
            .get_updates_request(poll_request)
            .concurrent_updates(self.config.concurrent_updates)
            .build()
        )
        self._app.add_handler(
            CommandHandler(list(TELEGRAM_COMMANDS), self._on_command_message)
        )
        self._app.add_handler(
            CallbackQueryHandler(
                self._on_review_callback,
                pattern=f"^{ACCESS_REVIEW_CONTROL_PREFIX}",
            )
        )
        self._app.add_handler(MessageHandler(filters.ALL, self._on_message))
        await self._app.initialize()
        await self._app.start()
        me = await self._app.bot.get_me()
        self._bot_username = getattr(me, "username", None)
        self._bot_user_id = getattr(me, "id", None)
        bot_commands = _build_telegram_bot_commands()
        if bot_commands:
            try:
                await self._app.bot.set_my_commands(bot_commands)
            except Exception:
                logger.warning(
                    "Failed to register Telegram slash commands with Bot API for bot_username=%s.",
                    self._bot_username or "-",
                    exc_info=True,
                )
        self._running = True
        await self.resume_inbound()

    async def stop(self) -> None:
        self._running = False
        if self._app is None:
            return
        await self.pause_inbound()
        await self._app.stop()
        await self._app.shutdown()
        self._app = None

    async def pause_inbound(self) -> None:
        if self._app is None or not self._polling_active:
            return
        updater = getattr(self._app, "updater", None)
        if updater is None:
            return
        await updater.stop()
        self._polling_active = False
        logger.info("Telegram polling paused.")

    async def resume_inbound(self) -> None:
        if self._app is None or self._polling_active:
            return
        updater = getattr(self._app, "updater", None)
        if updater is None:
            return
        await updater.start_polling(
            allowed_updates=["message", "channel_post", "callback_query"],
            drop_pending_updates=self.config.drop_pending_updates,
        )
        self._polling_active = True
        logger.info("Telegram polling active.")

    async def deliver_event(self, event: GatewayDeliveryEvent) -> None:
        outbound = project_text_delivery(event, channel_name=self.channel_name)
        if event.text.strip() or not event.attachments:
            await self._send_outbound(outbound)
        if event.attachments:
            await self._send_event_attachments(event)

    async def _send_event_attachments(self, event: GatewayDeliveryEvent) -> None:
        if self._app is None:
            raise RuntimeError("Telegram app is not running.")
        if not event.chat_id:
            logger.warning("Telegram attachment delivery missing chat_id.")
            return
        try:
            chat_id = int(event.chat_id)
        except ValueError:
            logger.warning(
                "Telegram attachment delivery has non-numeric chat_id=%s", event.chat_id
            )
            return
        thread_id = None
        if event.thread_id:
            try:
                thread_id = int(event.thread_id)
            except ValueError:
                thread_id = None
        reply_to_message_id = None
        if self.config.reply_to_message and event.original_message_id:
            try:
                reply_to_message_id = int(event.original_message_id)
            except ValueError:
                reply_to_message_id = None
        # Only the checked bytes of the gateway's staged copy are sent; a copy
        # that changed or became a link after staging is skipped (and logged).
        for attachment, data in self.outgoing_attachments.readable(event.attachments):
            payload = io.BytesIO(data)
            payload.name = attachment.filename or Path(attachment.path).name
            kwargs = {
                "chat_id": chat_id,
                "message_thread_id": thread_id,
                "caption": attachment.caption,
            }
            if reply_to_message_id is not None and ReplyParameters is not None:
                kwargs["reply_parameters"] = ReplyParameters(
                    message_id=reply_to_message_id,
                    allow_sending_without_reply=True,
                )
            if attachment.kind == "image" or str(
                attachment.content_type or ""
            ).startswith("image/"):
                kwargs["photo"] = payload
                await self._app.bot.send_photo(
                    **{k: v for k, v in kwargs.items() if v is not None}
                )
            else:
                kwargs["document"] = payload
                kwargs["filename"] = attachment.filename or Path(attachment.path).name
                await self._app.bot.send_document(
                    **{k: v for k, v in kwargs.items() if v is not None}
                )

    async def _on_command_message(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE
    ) -> None:
        message = self._extract_message(update)
        if message is None:
            return
        text = getattr(message, "text", None) or ""
        if not text.strip():
            return
        await self._handle_update_text(update, text=text)

    async def _on_review_callback(
        self,
        update: Update,
        _context: ContextTypes.DEFAULT_TYPE,
    ) -> None:
        """An access review button: decide as the pressing person's linked gateway user.

        The button data holds only the request id and the choice; the gateway
        looks up the person from this Telegram account and checks their
        current roles. The announcement is then updated with the outcome.
        """

        query = getattr(update, "callback_query", None)
        if query is None:
            return
        message = getattr(query, "message", None)
        user = getattr(query, "from_user", None)
        chat = getattr(message, "chat", None)
        if message is None or user is None or chat is None:
            await query.answer()
            return
        result = await self.decide_review_control(
            str(getattr(query, "data", None) or ""),
            user_id=str(getattr(user, "id", "") or ""),
            chat_id=str(getattr(chat, "id", "") or ""),
            thread_id=self._resolve_thread_id(message),
        )
        if result is None:
            await query.answer()
            return
        answer = result.text[:TELEGRAM_CALLBACK_ANSWER_MAX_CHARS]
        if result.closes_controls:
            await self._close_review_message(query, message, result.text)
            await query.answer(answer)
            return
        await query.answer(answer, show_alert=result.outcome is AccessReviewOutcome.REFUSED)

    async def _close_review_message(self, query, message, outcome: str) -> None:
        """Replace the buttons with the outcome line; keep the announcement text."""

        original = str(getattr(message, "text", "") or "").strip()
        updated = f"{original}\n\n{outcome}" if original else outcome
        try:
            # Plain text: the announcement came back from Telegram without
            # formatting, and reinterpreting it as markdown could change it.
            await query.edit_message_text(text=updated[:TELEGRAM_MAX_MESSAGE_LEN], reply_markup=None)
        except Exception:
            logger.warning(
                "Could not add the outcome to a Telegram access review; removing its buttons only. "
                "event=telegram_access_review_edit_failed chat_id=%s message_id=%s",
                getattr(getattr(message, "chat", None), "id", "-"),
                getattr(message, "message_id", "-"),
                exc_info=True,
            )
            await query.edit_message_reply_markup(reply_markup=None)

    @staticmethod
    def _review_inline_keyboard(controls: tuple[ReviewControl, ...], review_url: str | None):
        if InlineKeyboardButton is None or InlineKeyboardMarkup is None:
            return None
        rows = [[InlineKeyboardButton(control.label, callback_data=control.data)] for control in controls]
        if review_url and review_url.startswith("https://"):
            rows.append([InlineKeyboardButton("Open Inbox", url=review_url)])
        return InlineKeyboardMarkup(rows)

    async def _on_message(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE
    ) -> None:
        message = self._extract_message(update)
        if message is None:
            return
        text = getattr(message, "text", None) or getattr(message, "caption", None) or ""
        voice_input_available = (
            self._voice_client is not None and self.voice_settings.input_enabled
        )
        media_items = self._extract_inbound_media(
            message,
            include_voice_audio=bool(text.strip()) or not voice_input_available,
        )
        if (
            self._has_voice_payload(message)
            and not text.strip()
            and not media_items
            and voice_input_available
        ):
            await self._handle_update_voice(update)
            return
        if not text.strip() and not media_items:
            return
        await self._handle_update_text(update, text=text)

    async def _handle_update_text(self, update: Update, *, text: str) -> None:
        message = self._extract_message(update)
        if message is None:
            return
        if not self._is_allowed(update):
            return
        if not self._should_process_message(update, text):
            return
        normalized_text = self._normalize_text(text)
        voice_input_available = (
            self._voice_client is not None and self.voice_settings.input_enabled
        )
        media_items = self._extract_inbound_media(
            message,
            include_voice_audio=bool(normalized_text) or not voice_input_available,
        )
        if not normalized_text and not media_items:
            return
        if not self._claim_inbound_message(message):
            return

        activity_context = ActivityContext(
            chat_id=str(message.chat_id),
            thread_id=self._resolve_thread_id(message),
            message_id=str(message.message_id),
        )
        user_id = self._resolve_user_id(update)
        chat_id = str(message.chat_id)
        thread_id = self._resolve_thread_id(message)
        session = self.application.resolve_session(
            channel_name=self.channel_name,
            user_id=user_id,
            chat_id=chat_id,
            thread_id=thread_id,
        )
        try:
            attachments = await self._store_message_attachments(
                message=message,
                media_items=media_items,
                session_key=session.session_key,
                conversation_id=session.conversation_id,
                caption=normalized_text or getattr(message, "caption", None),
            )
        except AttachmentValidationError as exc:
            await self._send_text(
                chat_id=message.chat_id,
                text=str(exc),
                thread_id=getattr(message, "message_thread_id", None),
                reply_to_message_id=message.message_id,
            )
            return
        if attachments and normalized_text.startswith("/"):
            await self._send_text(
                chat_id=message.chat_id,
                text="Slash commands cannot include attachments.",
                thread_id=getattr(message, "message_thread_id", None),
                reply_to_message_id=message.message_id,
            )
            return
        try:
            if normalized_text.startswith("/") and not attachments:
                handled = await self.run_with_activity_indicator(
                    context=activity_context,
                    awaitable=self._handle_gateway_command(
                        update,
                        normalized_text,
                        message=message,
                    ),
                )
            else:
                handled = await self._handle_gateway_command(
                    update,
                    normalized_text,
                    message=message,
                )
        except TimeoutError:
            await self.send_timeout_notice(
                context=activity_context,
                timeout_seconds=self.request_timeout_seconds,
            )
            return
        if handled:
            return

        reply_to_message_id, replied_message = self._replied_message(update, message)
        inbound = ChannelInboundMessage(
            channel_name=self.channel_name,
            user_id=user_id,
            chat_id=chat_id,
            thread_id=thread_id,
            text=normalized_text,
            message_id=str(message.message_id),
            session_key=session.session_key,
            attachments=attachments,
            reply_to_message_id=reply_to_message_id,
            replied_message=replied_message,
        )
        logger.info(
            "Telegram inbound to manager: user_id=%s chat_id=%s thread_id=%s message_id=%s session=%s attachment_count=%d attachment_summary=%s preview=%r",
            inbound.user_id,
            inbound.chat_id,
            inbound.thread_id or "-",
            inbound.message_id,
            session.session_key,
            len(attachments),
            json.dumps(attachment_debug_summary(attachments), sort_keys=True),
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

    async def _handle_update_voice(self, update: Update) -> None:
        message = self._extract_message(update)
        if message is None:
            return
        if not self._is_allowed(update):
            return
        voice_payload = self._extract_voice_payload(message)
        if voice_payload is None:
            return
        if not self._should_process_message(
            update, getattr(message, "caption", None) or ""
        ):
            return
        if not self._claim_inbound_message(message):
            return
        if self._voice_client is None or not self.voice_settings.input_enabled:
            logger.info(
                "Ignoring Telegram voice message because voice input is disabled: chat_id=%s message_id=%s "
                "voice_enabled=%s input_enabled=%s output_enabled=%s provider=%s client_available=%s.",
                getattr(message, "chat_id", "-"),
                getattr(message, "message_id", "-"),
                self.voice_settings.enabled,
                self.voice_settings.input_enabled,
                self.voice_settings.output_enabled,
                self.voice_settings.provider,
                self._voice_client is not None,
            )
            await self._send_text(
                chat_id=message.chat_id,
                text=VOICE_UNAVAILABLE_TEXT,
                thread_id=getattr(message, "message_thread_id", None),
                reply_to_message_id=message.message_id,
            )
            return

        activity_context = ActivityContext(
            chat_id=str(message.chat_id),
            thread_id=self._resolve_thread_id(message),
            message_id=str(message.message_id),
        )
        try:
            voice_turn = await self.run_with_activity_indicator(
                context=activity_context,
                awaitable=self._handle_voice_turn(
                    update, message=message, voice_payload=voice_payload
                ),
            )
        except TimeoutError:
            await self.send_timeout_notice(
                context=activity_context,
                timeout_seconds=self.request_timeout_seconds,
            )
            return
        except Exception as exc:
            logger.warning(
                "Telegram voice request failed before a manager reply could be delivered: chat_id=%s message_id=%s.",
                getattr(message, "chat_id", "-"),
                getattr(message, "message_id", "-"),
                exc_info=True,
            )
            await self._send_text(
                chat_id=message.chat_id,
                text=f"Voice request failed: {exc}",
                thread_id=getattr(message, "message_thread_id", None),
                reply_to_message_id=message.message_id,
            )
            return
        for outbound in voice_turn.messages:
            await self._send_voice_outbound(
                outbound,
                request_text=voice_turn.request_text,
            )

    async def _handle_voice_turn(
        self,
        update: Update,
        *,
        message,
        voice_payload,
    ) -> _TelegramVoiceTurnResult:
        audio = await self._download_voice_payload(voice_payload)
        transcription = await self._voice_client.transcribe(
            TranscriptionRequest(
                audio=audio,
                filename=self._voice_filename(voice_payload),
                content_type=self._voice_content_type(voice_payload),
            )
        )
        normalized_text = self._normalize_text(transcription.text)
        if not normalized_text:
            await self._send_text(
                chat_id=message.chat_id,
                text="I could not transcribe that voice message.",
                thread_id=getattr(message, "message_thread_id", None),
                reply_to_message_id=message.message_id,
            )
            return _TelegramVoiceTurnResult(request_text="", messages=[])

        reply_to_message_id, replied_message = self._replied_message(update, message)
        inbound = ChannelInboundMessage(
            channel_name=self.channel_name,
            user_id=self._resolve_user_id(update),
            chat_id=str(message.chat_id),
            thread_id=self._resolve_thread_id(message),
            text=normalized_text,
            message_id=str(message.message_id),
            reply_to_message_id=reply_to_message_id,
            replied_message=replied_message,
        )
        active_session = self.application.get_active_session(
            channel_name=self.channel_name,
            user_id=inbound.user_id,
            chat_id=inbound.chat_id,
            thread_id=inbound.thread_id,
        )
        logger.info(
            "Telegram voice inbound to manager: user_id=%s chat_id=%s thread_id=%s message_id=%s session=%s preview=%r",
            inbound.user_id,
            inbound.chat_id,
            inbound.thread_id or "-",
            inbound.message_id,
            active_session.session_key if active_session is not None else "-",
            preview_text(inbound.text),
        )
        return _TelegramVoiceTurnResult(
            request_text=normalized_text,
            messages=await self.bridge.handle_inbound(inbound),
        )

    async def _handle_gateway_command(
        self, update: Update, raw_text: str, *, message
    ) -> bool:
        command = raw_text.strip()
        if not command.startswith("/"):
            return False

        user_id = self._resolve_user_id(update)
        chat_id = str(message.chat_id)
        thread_id = self._resolve_thread_id(message)
        result = await self.application.execute_interactive_command(
            raw_text=command,
            channel_name=self.channel_name,
            user_id=user_id,
            chat_id=chat_id,
            thread_id=thread_id,
            help_text=TELEGRAM_HELP_TEXT,
            enable_reply_command=False,
        )
        if result.handled:
            if result.deferred:
                return True
            await self._send_text(
                chat_id=message.chat_id,
                text=result.text or "",
                thread_id=getattr(message, "message_thread_id", None),
                reply_to_message_id=message.message_id,
            )
            return True

        return False

    def _extract_message(self, update: Update):
        return getattr(update, "effective_message", None) or getattr(
            update, "channel_post", None
        )

    def _has_voice_payload(self, message) -> bool:
        return self._extract_voice_payload(message) is not None

    def _extract_voice_payload(self, message):
        return getattr(message, "voice", None) or getattr(message, "audio", None)

    async def _download_voice_payload(self, voice_payload) -> bytes:
        if self._app is None:
            raise RuntimeError("Telegram app is not running.")
        file_id = getattr(voice_payload, "file_id", None)
        if not file_id:
            raise RuntimeError("Telegram voice payload is missing file_id.")
        file_obj = await self._app.bot.get_file(file_id)
        if hasattr(file_obj, "download_as_bytearray"):
            return bytes(await file_obj.download_as_bytearray())
        if hasattr(file_obj, "download_to_memory"):
            buffer = io.BytesIO()
            await file_obj.download_to_memory(buffer)
            return buffer.getvalue()
        raise RuntimeError("Telegram file object does not support download.")

    def _extract_inbound_media(
        self, message, *, include_voice_audio: bool = False
    ) -> list[_TelegramInboundMedia]:
        items: list[_TelegramInboundMedia] = []
        photos = list(getattr(message, "photo", None) or [])
        if photos:
            photo = photos[-1]
            identifier = self._safe_payload_identifier(
                getattr(photo, "file_unique_id", None)
                or getattr(photo, "file_id", None)
                or "photo"
            )
            items.append(
                _TelegramInboundMedia(
                    payload=photo,
                    filename=f"telegram_photo_{identifier}.jpg",
                    content_type="image/jpeg",
                )
            )
        document = getattr(message, "document", None)
        if document is not None:
            filename = str(getattr(document, "file_name", None) or "")
            content_type = str(getattr(document, "mime_type", None) or "")
            items.append(
                _TelegramInboundMedia(
                    payload=document,
                    filename=filename
                    or self._filename_for_payload(
                        document,
                        default_stem="telegram_document",
                        content_type=content_type,
                    ),
                    content_type=content_type,
                )
            )
        video = getattr(message, "video", None) or getattr(message, "animation", None)
        if video is not None:
            content_type = str(getattr(video, "mime_type", None) or "video/mp4")
            items.append(
                _TelegramInboundMedia(
                    payload=video,
                    filename=self._filename_for_payload(
                        video, default_stem="telegram_video", content_type=content_type
                    ),
                    content_type=content_type,
                )
            )
        sticker = getattr(message, "sticker", None)
        if sticker is not None:
            content_type = str(getattr(sticker, "mime_type", None) or "image/webp")
            items.append(
                _TelegramInboundMedia(
                    payload=sticker,
                    filename=self._filename_for_payload(
                        sticker,
                        default_stem="telegram_sticker",
                        content_type=content_type,
                    ),
                    content_type=content_type,
                )
            )
        if include_voice_audio:
            audio = getattr(message, "audio", None) or getattr(message, "voice", None)
            if audio is not None:
                content_type = self._voice_content_type(audio)
                items.append(
                    _TelegramInboundMedia(
                        payload=audio,
                        filename=self._filename_for_payload(
                            audio,
                            default_stem="telegram_audio",
                            content_type=content_type,
                        ),
                        content_type=content_type,
                    )
                )
        return items

    def _filename_for_payload(
        self, payload, *, default_stem: str, content_type: str
    ) -> str:
        filename = str(getattr(payload, "file_name", None) or "").strip()
        if filename:
            return filename
        suffix = (
            mimetypes.guess_extension(
                str(content_type or "").split(";", 1)[0].strip().lower()
            )
            or ""
        )
        identifier = self._safe_payload_identifier(
            getattr(payload, "file_unique_id", None)
            or getattr(payload, "file_id", None)
            or new_id_suffix()
        )
        return f"{default_stem}_{identifier}{suffix}"

    def _safe_payload_identifier(self, value: object) -> str:
        rendered = re.sub(r"[^A-Za-z0-9_.-]+", "_", str(value or "").strip()).strip(
            "._-"
        )
        return rendered or new_id_suffix()

    async def _download_telegram_payload(self, payload) -> bytes:
        if self._app is None:
            raise RuntimeError("Telegram app is not running.")
        file_id = getattr(payload, "file_id", None)
        if not file_id:
            raise RuntimeError("Telegram media payload is missing file_id.")
        file_obj = await self._app.bot.get_file(file_id)
        if hasattr(file_obj, "download_as_bytearray"):
            return bytes(await file_obj.download_as_bytearray())
        if hasattr(file_obj, "download_to_memory"):
            buffer = io.BytesIO()
            await file_obj.download_to_memory(buffer)
            return buffer.getvalue()
        raise RuntimeError("Telegram file object does not support download.")

    async def _store_message_attachments(
        self,
        *,
        message,
        media_items: list[_TelegramInboundMedia],
        session_key: str,
        conversation_id: Optional[str],
        caption: Optional[str],
    ):
        attachments = []
        for item in media_items:
            data = await self._download_telegram_payload(item.payload)
            stored = self.attachments.store_bytes(
                data=data,
                filename=item.filename,
                content_type=item.content_type,
                session_key=session_key,
                conversation_id=conversation_id,
                origin="telegram",
                caption=caption,
            )
            attachments.append(stored.attachment)
        return attachments

    def _voice_content_type(self, voice_payload) -> str:
        return str(getattr(voice_payload, "mime_type", None) or "audio/ogg")

    def _voice_filename(self, voice_payload) -> str:
        content_type = self._voice_content_type(voice_payload)
        if content_type == "audio/mpeg":
            return "telegram_audio.mp3"
        if content_type == "audio/wav":
            return "telegram_audio.wav"
        return "telegram_voice.ogg"

    def _resolve_user_id(self, update: Update) -> str:
        user = getattr(update, "effective_user", None)
        if user is not None and getattr(user, "id", None) is not None:
            return str(user.id)
        message = self._extract_message(update)
        if message is not None and getattr(message, "sender_chat", None) is not None:
            sender_chat = getattr(message, "sender_chat")
            if getattr(sender_chat, "id", None) is not None:
                return str(sender_chat.id)
        chat = getattr(update, "effective_chat", None)
        return str(getattr(chat, "id", "telegram_unknown"))

    def _replied_message(
        self, update: Update, message: Any
    ) -> tuple[Optional[str], Optional[RepliedMessage]]:
        """The chat message ``message`` replies to: its id, and its text for the agent.

        Telegram delivers the replied message with the update, so this is what
        the chat showed (custom_messages.contracts.replied_message_v1 explains
        why the agent needs it). Inside a forum topic Telegram also fills
        ``reply_to_message`` with the topic's opening service message when the
        person did not reply to anything; that is not a reply and is skipped.
        Rich messages are normalized from their visible blocks. A replied
        message without text or caption (a sticker, a photo alone)
        keeps its id but gives the agent nothing to read.
        """

        replied = getattr(message, "reply_to_message", None)
        if replied is None or getattr(replied, "forum_topic_created", None) is not None:
            return None, None
        replied_id = getattr(replied, "message_id", None)
        reply_to_message_id = str(replied_id) if replied_id is not None else None
        text = replied_message_text(replied)
        if not text:
            logger.debug(
                "Telegram reply names a message without text: chat_id=%s message_id=%s reply_to=%s.",
                getattr(message, "chat_id", "-"),
                getattr(message, "message_id", "-"),
                reply_to_message_id or "-",
            )
            return reply_to_message_id, None
        author = self._replied_message_author(update, replied)
        if author is None:
            return reply_to_message_id, None
        sent_at = getattr(replied, "date", None)
        if isinstance(sent_at, datetime):
            sent_at = (sent_at if sent_at.tzinfo else sent_at.replace(tzinfo=timezone.utc)).astimezone(timezone.utc)
        quote = getattr(message, "quote", None)
        replied_message = RepliedMessage.bounded(
            author=author,
            author_name=self._telegram_sender_name(replied) if author == "other" else None,
            text=text,
            quoted_text=getattr(quote, "text", None) if quote is not None else None,
            sent_at=sent_at.isoformat() if isinstance(sent_at, datetime) else None,
        )
        logger.info(
            "Telegram reply carries the replied message: chat_id=%s message_id=%s reply_to=%s "
            "author=%s text_length=%d shortened=%s quoted=%s.",
            getattr(message, "chat_id", "-"),
            getattr(message, "message_id", "-"),
            reply_to_message_id or "-",
            author,
            len(text),
            replied_message.shortened,
            replied_message.quoted_text is not None,
        )
        return reply_to_message_id, replied_message

    def _replied_message_author(self, update: Update, replied: Any) -> Optional[RepliedMessageAuthor]:
        """Whether the bot, the person replying or someone else wrote ``replied``."""

        sender = getattr(replied, "from_user", None)
        sender_id = getattr(sender, "id", None) if sender is not None else None
        if self._bot_user_id is None:
            # Set at start from getMe before polling, so a message cannot arrive
            # without it; without it the bot's own report would be labeled as
            # another member's message, so nothing is passed on.
            logger.warning(
                "Telegram reply context skipped because the bot's own user id is unknown. "
                "event=telegram_reply_context_bot_id_missing"
            )
            return None
        if sender_id is not None and sender_id == self._bot_user_id:
            return "assistant"
        if (
            getattr(replied, "sender_chat", None) is None
            and sender_id is not None
            and str(sender_id) == self._resolve_user_id(update)
        ):
            return "requester"
        return "other"

    @staticmethod
    def _telegram_sender_name(replied: Any) -> Optional[str]:
        """The display name Telegram shows for a message's sender."""

        sender_chat = getattr(replied, "sender_chat", None)
        if sender_chat is not None:
            return getattr(sender_chat, "title", None) or getattr(sender_chat, "username", None)
        sender = getattr(replied, "from_user", None)
        if sender is None:
            return None
        names = [getattr(sender, "first_name", None), getattr(sender, "last_name", None)]
        full_name = " ".join(str(name) for name in names if name)
        return full_name or getattr(sender, "username", None)

    def _resolve_thread_id(self, message) -> Optional[str]:
        if not self.config.topic_scoped_sessions:
            return None
        thread_id = getattr(message, "message_thread_id", None)
        return str(thread_id) if thread_id is not None else None

    def _warn_if_allow_all(self) -> None:
        if self.config.allowed_user_ids or self.config.allowed_chat_ids:
            return
        logger.warning(
            "Telegram interactive app is enabled with empty allowed_user_ids and "
            "allowed_chat_ids; all Telegram senders accepted by the bot token can "
            "start sessions until an allowlist is configured."
        )

    def _is_allowed(self, update: Update) -> bool:
        chat = getattr(update, "effective_chat", None)
        user_id = self._resolve_user_id(update)
        chat_id = str(getattr(chat, "id", ""))
        chat_type = str(getattr(chat, "type", "unknown"))
        registered_user = self.is_registered_gateway_user(user_id)
        if registered_user and chat_type not in {"group", "supergroup"}:
            return True
        if self.config.allowed_chat_ids and chat_id not in self.config.allowed_chat_ids:
            logger.info(
                "Ignoring Telegram message from non-allowed source chat: chat_id=%s chat_type=%s user_id=%s. "
                "newuser_target=%s. Note: in private chats, Telegram chat_id is usually the same numeric value as "
                "the user's id.",
                chat_id,
                chat_type,
                user_id,
                user_id,
            )
            return False
        if (
            self.config.allowed_user_ids
            and user_id not in self.config.allowed_user_ids
            and not registered_user
        ):
            logger.info(
                "Ignoring Telegram message from non-allowed source user: user_id=%s chat_id=%s chat_type=%s "
                "newuser_target=%s.",
                user_id,
                chat_id,
                chat_type,
                user_id,
            )
            return False
        return True

    def _should_process_message(self, update: Update, text: str) -> bool:
        chat = getattr(update, "effective_chat", None)
        chat_type = getattr(chat, "type", "private")
        if chat_type not in {"group", "supergroup"}:
            return True
        if self.config.group_policy == "open":
            return True
        message = self._extract_message(update)
        if message is None:
            return False
        if self._message_mentions_bot(message, text):
            return True
        reply_to = getattr(message, "reply_to_message", None)
        reply_user = (
            getattr(reply_to, "from_user", None) if reply_to is not None else None
        )
        return bool(
            reply_user is not None
            and getattr(reply_user, "id", None) == self._bot_user_id
        )

    def _message_mentions_bot(self, message, text: str) -> bool:
        username = self._bot_username
        if not username:
            return False
        lowered = text.lower()
        if f"@{username.lower()}" in lowered:
            return True
        entities = list(getattr(message, "entities", None) or [])
        for entity in entities:
            if getattr(entity, "type", "") != "mention":
                continue
            offset = getattr(entity, "offset", 0)
            length = getattr(entity, "length", 0)
            if lowered[offset : offset + length] == f"@{username.lower()}":
                return True
        return False

    def _normalize_text(self, text: str) -> str:
        normalized = (text or "").strip()
        if self._bot_username:
            normalized = re.sub(
                rf"@{re.escape(self._bot_username)}\b",
                "",
                normalized,
                flags=re.IGNORECASE,
            ).strip()
        return normalized

    def _claim_inbound_message(self, message) -> bool:
        message_id = getattr(message, "message_id", None)
        chat_id = getattr(message, "chat_id", None)
        if message_id is None or chat_id is None:
            return True
        key = (str(chat_id), self._resolve_thread_id(message), str(message_id))
        if key in self._processed_inbound_message_keys:
            logger.info(
                "Ignoring duplicate Telegram inbound message: chat_id=%s thread_id=%s message_id=%s.",
                key[0],
                key[1] or "-",
                key[2],
            )
            return False
        self._processed_inbound_message_keys.add(key)
        self._processed_inbound_message_order.append(key)
        while (
            len(self._processed_inbound_message_order) > TELEGRAM_INBOUND_DEDUPE_LIMIT
        ):
            old_key = self._processed_inbound_message_order.popleft()
            self._processed_inbound_message_keys.discard(old_key)
        return True

    async def _send_outbound(self, outbound: ChannelOutboundMessage) -> None:
        if not outbound.chat_id:
            logger.warning("Telegram outbound message missing chat_id.")
            return
        rendered_text = outbound.text
        try:
            chat_id = int(outbound.chat_id)
        except ValueError:
            logger.warning(
                "Telegram outbound message has non-numeric chat_id=%s", outbound.chat_id
            )
            return
        reply_to_message_id = None
        if self.config.reply_to_message and outbound.in_reply_to_message_id:
            try:
                reply_to_message_id = int(outbound.in_reply_to_message_id)
            except ValueError:
                reply_to_message_id = None
        thread_id = None
        if outbound.thread_id:
            try:
                thread_id = int(outbound.thread_id)
            except ValueError:
                thread_id = None
        controls = self.review_controls_for(outbound)
        if controls:
            await self._send_legacy_text(
                chat_id=chat_id,
                text=rendered_text,
                thread_id=thread_id,
                reply_to_message_id=reply_to_message_id,
                reply_markup=self._review_inline_keyboard(controls, outbound.review_url),
            )
        else:
            await self._send_text(
                chat_id=chat_id,
                text=rendered_text,
                thread_id=thread_id,
                reply_to_message_id=reply_to_message_id,
            )
        logger.info(
            "Telegram outbound to user: chat_id=%s thread_id=%s reply_to=%s session=%s conversation=%s preview=%r",
            outbound.chat_id,
            outbound.thread_id or "-",
            outbound.in_reply_to_message_id or "-",
            outbound.session_key or "-",
            outbound.conversation_id or "-",
            preview_text(outbound.text),
        )

    async def _send_outbound_audio(self, outbound: ChannelOutboundMessage) -> None:
        if self._app is None:
            raise RuntimeError("Telegram app is not running.")
        if not outbound.chat_id:
            logger.warning("Telegram voice outbound message missing chat_id.")
            return
        try:
            chat_id = int(outbound.chat_id)
        except ValueError:
            logger.warning(
                "Telegram voice outbound message has non-numeric chat_id=%s",
                outbound.chat_id,
            )
            return
        thread_id = None
        if outbound.thread_id:
            try:
                thread_id = int(outbound.thread_id)
            except ValueError:
                thread_id = None
        reply_to_message_id = None
        if self.config.reply_to_message and outbound.in_reply_to_message_id:
            try:
                reply_to_message_id = int(outbound.in_reply_to_message_id)
            except ValueError:
                reply_to_message_id = None
        speech_text, trimmed_history = speech_text_for_outbound_reply(outbound.text)
        if trimmed_history:
            logger.warning(
                "Telegram voice outbound text looked like copied conversation history; speaking latest final answer only "
                "(chat_id=%s thread_id=%s session=%s conversation=%s).",
                outbound.chat_id,
                outbound.thread_id or "-",
                outbound.session_key or "-",
                outbound.conversation_id or "-",
            )
        speech = await self._voice_client.synthesize(SpeechRequest(text=speech_text))
        audio = io.BytesIO(speech.audio)
        audio.name = self._audio_filename(speech.content_type)
        kwargs = {
            "chat_id": chat_id,
            "audio": audio,
            "message_thread_id": thread_id,
        }
        if reply_to_message_id is not None and ReplyParameters is not None:
            kwargs["reply_parameters"] = ReplyParameters(
                message_id=reply_to_message_id,
                allow_sending_without_reply=True,
            )
        await self._app.bot.send_audio(
            **{k: v for k, v in kwargs.items() if v is not None}
        )
        logger.info(
            "Telegram voice outbound to user: chat_id=%s thread_id=%s reply_to=%s session=%s conversation=%s preview=%r",
            outbound.chat_id,
            outbound.thread_id or "-",
            outbound.in_reply_to_message_id or "-",
            outbound.session_key or "-",
            outbound.conversation_id or "-",
            preview_text(outbound.text),
        )

    async def _send_voice_outbound(
        self,
        outbound: ChannelOutboundMessage,
        *,
        request_text: str,
    ) -> None:
        text_outbound = ChannelOutboundMessage(
            channel_name=outbound.channel_name,
            chat_id=outbound.chat_id,
            thread_id=outbound.thread_id,
            text=text_for_voice_turn_history(
                request_text=request_text, reply_text=outbound.text
            ),
            in_reply_to_message_id=outbound.in_reply_to_message_id,
            session_key=outbound.session_key,
            conversation_id=outbound.conversation_id,
        )
        await self._send_outbound(text_outbound)
        if (
            not self.voice_settings.output_enabled
            or self._voice_client is None
            or not should_synthesize_voice_reply(outbound.text)
        ):
            return
        try:
            await self._send_outbound_audio(outbound)
        except Exception as exc:
            logger.warning(
                "Telegram voice reply synthesis failed after text delivery: chat_id=%s thread_id=%s session=%s conversation=%s.",
                outbound.chat_id,
                outbound.thread_id or "-",
                outbound.session_key or "-",
                outbound.conversation_id or "-",
                exc_info=True,
            )
            try:
                chat_id = int(outbound.chat_id)
            except ValueError:
                logger.warning(
                    "Telegram voice synthesis failure notice skipped for non-numeric chat_id=%s.",
                    outbound.chat_id,
                )
                return
            thread_id = None
            if outbound.thread_id:
                try:
                    thread_id = int(outbound.thread_id)
                except ValueError:
                    thread_id = None
            reply_to_message_id = None
            if outbound.in_reply_to_message_id:
                try:
                    reply_to_message_id = int(outbound.in_reply_to_message_id)
                except ValueError:
                    reply_to_message_id = None
            await self._send_text(
                chat_id=chat_id,
                text=f"Voice reply failed: {exc}",
                thread_id=thread_id,
                reply_to_message_id=reply_to_message_id,
            )

    def _audio_filename(self, content_type: str) -> str:
        normalized = str(content_type or "").split(";")[0].strip().lower()
        if normalized == "audio/wav":
            return "reply.wav"
        if normalized == "audio/ogg":
            return "reply.ogg"
        return "reply.mp3"

    async def _send_text(
        self,
        *,
        chat_id: int,
        text: str,
        thread_id: Optional[int],
        reply_to_message_id: Optional[int],
    ) -> None:
        if self._app is None:
            raise RuntimeError("Telegram app is not running.")
        if await self._try_send_rich_text(
            chat_id=chat_id,
            text=text,
            thread_id=thread_id,
            reply_to_message_id=reply_to_message_id,
        ):
            return
        await self._send_legacy_text(
            chat_id=chat_id,
            text=text,
            thread_id=thread_id,
            reply_to_message_id=reply_to_message_id,
        )

    async def _try_send_rich_text(
        self,
        *,
        chat_id: int,
        text: str,
        thread_id: Optional[int],
        reply_to_message_id: Optional[int],
    ) -> bool:
        if not self.config.rich_messages or self._rich_messages_disabled:
            return False
        if self._app is None:
            raise RuntimeError("Telegram app is not running.")
        rendered_text = (text or "").strip()
        if not rendered_text:
            return False
        if len(rendered_text) > TELEGRAM_RICH_MESSAGE_MAX_CHARS:
            logger.info(
                "Telegram rich message skipped because text exceeds rich-message limit: "
                "chat_id=%s thread_id=%s length=%d limit=%d.",
                chat_id,
                thread_id or "-",
                len(rendered_text),
                TELEGRAM_RICH_MESSAGE_MAX_CHARS,
            )
            return False
        bot_post = getattr(self._app.bot, "_post", None)
        if not callable(bot_post):
            return False
        kwargs: dict[str, Any] = {
            "chat_id": chat_id,
            "rich_message": _telegram_rich_message_payload(rendered_text),
            "message_thread_id": thread_id,
        }
        reply_parameters = _telegram_reply_parameters_payload(reply_to_message_id)
        if reply_parameters is not None:
            kwargs["reply_parameters"] = reply_parameters
        try:
            await bot_post(
                TELEGRAM_SEND_RICH_MESSAGE_ENDPOINT,
                api_kwargs={k: v for k, v in kwargs.items() if v is not None},
            )
            return True
        except Exception as exc:
            if _is_rich_capability_error(exc):
                self._rich_messages_disabled = True
                logger.warning(
                    "Telegram rich messages disabled after Bot API capability rejection: "
                    "chat_id=%s thread_id=%s error=%s.",
                    chat_id,
                    thread_id or "-",
                    exc,
                )
                return False
            if _is_rich_fallback_error(exc):
                logger.warning(
                    "Telegram rich message rejected; falling back to legacy HTML sendMessage: "
                    "chat_id=%s thread_id=%s error=%s.",
                    chat_id,
                    thread_id or "-",
                    exc,
                )
                return False
            raise

    async def _send_legacy_text(
        self,
        *,
        chat_id: int,
        text: str,
        thread_id: Optional[int],
        reply_to_message_id: Optional[int],
        reply_markup=None,
        reply_markup_all_chunks: bool = False,
    ) -> None:
        if self._app is None:
            raise RuntimeError("Telegram app is not running.")
        chunks = _split_telegram_html(_markdown_to_telegram_html(text))
        for chunk_index, chunk in enumerate(chunks):
            kwargs = {
                "chat_id": chat_id,
                "text": chunk,
                "parse_mode": ParseMode.HTML if ParseMode is not None else None,
                "message_thread_id": thread_id,
            }
            if reply_to_message_id is not None and ReplyParameters is not None:
                kwargs["reply_parameters"] = ReplyParameters(
                    message_id=reply_to_message_id,
                    allow_sending_without_reply=True,
                )
            if reply_markup is not None and (reply_markup_all_chunks or chunk_index == len(chunks) - 1):
                kwargs["reply_markup"] = reply_markup
            await self._app.bot.send_message(
                **{k: v for k, v in kwargs.items() if v is not None}
            )

    async def emit_native_activity_indicator(self, context: ActivityContext) -> None:
        if not context.chat_id:
            return
        try:
            chat_id = int(context.chat_id)
        except ValueError:
            logger.debug(
                "Telegram native activity indicator skipped for non-numeric chat_id=%s.",
                context.chat_id,
            )
            return
        thread_id = None
        if context.thread_id:
            try:
                thread_id = int(context.thread_id)
            except ValueError:
                thread_id = None
        await self._send_typing_action(chat_id=chat_id, thread_id=thread_id)

    async def emit_text_activity_update(
        self, context: ActivityContext, text: str
    ) -> None:
        if not context.chat_id:
            return
        try:
            chat_id = int(context.chat_id)
        except ValueError:
            logger.warning(
                "Telegram activity update skipped for non-numeric chat_id=%s",
                context.chat_id,
            )
            return
        thread_id = None
        if context.thread_id:
            try:
                thread_id = int(context.thread_id)
            except ValueError:
                thread_id = None
        reply_to_message_id = None
        if context.message_id:
            try:
                reply_to_message_id = int(context.message_id)
            except ValueError:
                reply_to_message_id = None
        await self._send_text(
            chat_id=chat_id,
            text=text,
            thread_id=thread_id,
            reply_to_message_id=reply_to_message_id,
        )

    async def _send_typing_action(
        self, *, chat_id: int, thread_id: Optional[int]
    ) -> None:
        if self._app is None:
            return
        kwargs = {
            "chat_id": chat_id,
            "action": ChatAction.TYPING if ChatAction is not None else "typing",
            "message_thread_id": thread_id,
        }
        try:
            await self._app.bot.send_chat_action(
                **{k: v for k, v in kwargs.items() if v is not None}
            )
        except Exception:
            logger.debug(
                "Telegram typing indicator failed for chat_id=%s thread_id=%s.",
                chat_id,
                thread_id or "-",
                exc_info=True,
            )
