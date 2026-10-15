"""Discord interactive app adapter built on top of the gateway bridge."""

from __future__ import annotations

import asyncio
import io
import inspect
import logging
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from assistant_gateway.application.gateway_application import GatewayDeliveryEvent
from runtime_settings.interactive_apps import DiscordAppSettings
from assistant_gateway.inbound_attachments import (
    AttachmentValidationError,
)
from assistant_gateway.application.access_reviews import ACCESS_REVIEW_CONTROL_PREFIX
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
from assistant_gateway.voice import (
    SpeechRequest,
    TranscriptionRequest,
    VoiceClient,
    should_synthesize_voice_reply,
    speech_text_for_outbound_reply,
    text_for_voice_turn_history,
)
from custom_messages.contracts.runtime_v1 import DELIVERY_CHANNEL_DISCORD
from env_secrets import get_env_with_file_fallback

try:
    import discord
    from discord import app_commands
    from discord.ext import commands as discord_commands
except (
    ImportError
):  # pragma: no cover - exercised in environments without discord extras
    app_commands = None
    discord = None
    discord_commands = None


logger = logging.getLogger(__name__)

DISCORD_BOT_TOKEN_ENV_VAR = "DISCORD_BOT_TOKEN"
DISCORD_MAX_MESSAGE_LEN = 2000
DISCORD_TYPING_REPEAT_SECONDS = 8.0
VOICE_UNAVAILABLE_TEXT = "Voice input is not available for this assistant right now. Please send your message as text."
DISCORD_COMMAND_SPECS = (
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
DISCORD_HELP_TEXT = build_interactive_session_help(
    include_reply=False,
    trailer="Other messages go to the manager. Reminder phrases like `remind me every 2 hours to ...` also work.",
)
START_READY_TIMEOUT_SECONDS = 30.0


class _DiscordVoiceDependencyContext(logging.Filter):
    """Explain SDK voice-channel warnings without hiding missing dependencies."""

    def filter(self, record: logging.LogRecord) -> bool:
        dependencies = {
            "davey is not installed, voice will NOT be supported": "davey",
            "PyNaCl is not installed, voice will NOT be supported": "PyNaCl",
        }
        dependency = dependencies.get(record.getMessage())
        if dependency:
            record.msg = (
                "Discord live voice-channel support is unavailable: %s is not installed. "
                "This app uses text and audio attachments, which do not require it. "
                "Audio transcription and replies use the configured speech service. "
                "event=discord_live_voice_dependency_missing dependency=%s"
            )
            record.args = (dependency, dependency)
        return True


def _configure_discord_library_logging() -> None:
    logging.getLogger("discord").setLevel(logging.WARNING)
    logging.getLogger("discord.http").setLevel(logging.WARNING)
    client_logger = logging.getLogger("discord.client")
    if not any(
        isinstance(item, _DiscordVoiceDependencyContext)
        for item in client_logger.filters
    ):
        client_logger.addFilter(_DiscordVoiceDependencyContext())


def _split_discord_text(
    text: str, *, limit: int = DISCORD_MAX_MESSAGE_LEN
) -> list[str]:
    stripped = (text or "").strip()
    if not stripped:
        return [""]
    parts: list[str] = []
    remaining = stripped
    while len(remaining) > limit:
        split_at = remaining.rfind("\n", 0, limit)
        if split_at <= 0:
            split_at = remaining.rfind(" ", 0, limit)
        if split_at <= 0:
            split_at = limit
        parts.append(remaining[:split_at].rstrip())
        remaining = remaining[split_at:].lstrip()
    if remaining:
        parts.append(remaining)
    return parts


@dataclass(frozen=True)
class _DiscordVoiceTurnResult:
    request_text: str
    messages: list[ChannelOutboundMessage]


_DISCORD_BOT_BASE = discord_commands.Bot if discord_commands is not None else object


class _DiscordGatewayBot(_DISCORD_BOT_BASE):
    def __init__(self, adapter: "DiscordInteractiveApp") -> None:
        if discord is None or discord_commands is None:
            raise RuntimeError("discord.py is not installed.")
        intents = discord.Intents.default()
        intents.message_content = True
        super().__init__(
            command_prefix=discord_commands.when_mentioned, intents=intents
        )
        self._adapter = adapter

    async def setup_hook(self) -> None:
        await self._adapter._setup_bot(self)

    async def on_ready(self) -> None:
        await self._adapter._on_ready(self)

    async def on_message(self, message) -> None:
        await self._adapter._on_message(message)

    async def on_interaction(self, interaction) -> None:
        # Review buttons are handled here rather than by live views, so a
        # press still works after the gateway restarts.
        await self._adapter._on_component_interaction(interaction)


class DiscordInteractiveApp(BaseInteractiveApp):
    channel_name = DELIVERY_CHANNEL_DISCORD

    def __init__(
        self,
        config: DiscordAppSettings,
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
        self._bot: Optional[_DiscordGatewayBot] = None
        self._runner_task: Optional[asyncio.Task] = None
        self._ready_event = asyncio.Event()
        self._self_user_id: Optional[int] = None
        self._voice_client: Optional[VoiceClient] = runtime.voice_client

    @property
    def enabled(self) -> bool:
        return bool(self.config.enabled)

    def uses_native_activity_indicator(self, _context: ActivityContext) -> bool:
        return True

    def native_activity_refresh_interval_seconds(self) -> float:
        return DISCORD_TYPING_REPEAT_SECONDS

    async def start(self) -> None:
        if not self.enabled:
            return
        self._warn_if_allow_all()
        if discord is None or discord_commands is None or app_commands is None:
            raise RuntimeError(
                "Discord support requires discord.py. Install requirements.txt again."
            )
        token = get_env_with_file_fallback(DISCORD_BOT_TOKEN_ENV_VAR)
        if not token:
            raise RuntimeError(
                f"Discord is enabled but env var {DISCORD_BOT_TOKEN_ENV_VAR} is not set."
            )
        _configure_discord_library_logging()

        self._ready_event.clear()
        self._bot = _DiscordGatewayBot(self)
        self._runner_task = asyncio.create_task(
            self._bot.start(token),
            name="discord_interactive_app",
        )
        await self._wait_until_ready()

    async def stop(self) -> None:
        if self._bot is not None:
            await self._bot.close()
        if self._runner_task is not None:
            try:
                await self._runner_task
            except asyncio.CancelledError:
                pass
            except Exception:
                logger.warning(
                    "Discord background task exited with an error.", exc_info=True
                )
        self._bot = None
        self._runner_task = None
        self._self_user_id = None
        self._ready_event.clear()

    async def deliver_event(self, event: GatewayDeliveryEvent) -> None:
        outbound = project_text_delivery(event, channel_name=self.channel_name)
        if event.text.strip() or not event.attachments:
            await self._send_outbound(outbound)
        if event.attachments:
            await self._send_event_attachments(event)

    async def _wait_until_ready(self) -> None:
        if self._runner_task is None:
            raise RuntimeError("Discord app did not start.")
        ready_task = asyncio.create_task(self._ready_event.wait())
        try:
            done, pending = await asyncio.wait(
                {self._runner_task, ready_task},
                return_when=asyncio.FIRST_COMPLETED,
                timeout=START_READY_TIMEOUT_SECONDS,
            )
            if not done:
                raise RuntimeError(
                    "Discord app did not become ready within 30 seconds."
                )
            if ready_task in done:
                return
            exc = self._runner_task.exception()
            if exc is not None:
                raise exc
            raise RuntimeError("Discord app stopped before becoming ready.")
        finally:
            if not ready_task.done():
                ready_task.cancel()

    async def _setup_bot(self, bot: _DiscordGatewayBot) -> None:
        await self._register_slash_commands(bot)
        await self._sync_slash_commands(bot)

    async def _register_slash_commands(self, bot: _DiscordGatewayBot) -> None:
        tree = bot.tree

        @tree.command(name="help", description="Show available commands")
        async def help_command(interaction: discord.Interaction) -> None:
            await self._handle_slash_command(interaction, "/help")

        @tree.command(name="docs", description="Open documentation")
        async def docs_command(interaction: discord.Interaction) -> None:
            await self._handle_slash_command(interaction, "/docs")

        @tree.command(name="tour", description="Start the onboarding tour")
        async def tour_command(interaction: discord.Interaction) -> None:
            await self._handle_slash_command(interaction, "/tour")

        edit_group = app_commands.Group(
            name="edit", description="Edit gateway-managed files"
        )

        @edit_group.command(
            name="config", description="Open instance_config.yaml editor"
        )
        async def edit_config_command(interaction: discord.Interaction) -> None:
            await self._handle_slash_command(interaction, "/edit config")

        tree.add_command(edit_group)

        @app_commands.describe(
            tool="Tool id, for example cli:github, cli:google, cli:x or http:portainer",
            option="Optional: me (default), team, or a way to sign in such as web",
        )
        @tree.command(name="link", description="Sign in to a tool or link its account")
        async def link_command(
            interaction: discord.Interaction,
            tool: str,
            option: Optional[str] = None,
        ) -> None:
            raw = f"/link {tool}" if not option else f"/link {tool} {option}"
            await self._handle_slash_command(interaction, raw)

        @app_commands.describe(
            provider="Optional, for example status, model, google team_client, x team_client, bitwarden, or bitwarden server <url>"
        )
        @tree.command(
            name="auth", description="Check sign-ins and set up team sign-in apps"
        )
        async def auth_command(
            interaction: discord.Interaction,
            provider: Optional[str] = None,
        ) -> None:
            raw = "/auth" if not provider else f"/auth {provider}"
            await self._handle_slash_command(interaction, raw)

        @tree.command(name="new", description="Start a new session")
        async def new_command(interaction: discord.Interaction) -> None:
            await self._handle_slash_command(interaction, "/new")

        @tree.command(name="stop", description="Cancel the current task")
        async def stop_command(interaction: discord.Interaction) -> None:
            await self._handle_slash_command(interaction, "/stop")

        @app_commands.describe(range_text="Optional range like 1-7")
        @tree.command(name="list_sessions", description="List recent sessions")
        async def list_sessions_command(
            interaction: discord.Interaction,
            range_text: Optional[str] = None,
        ) -> None:
            raw = "/list_sessions"
            if range_text:
                raw = f"{raw} {range_text}"
            await self._handle_slash_command(interaction, raw)

        @tree.command(
            name="list_conversations",
            description="List conversations in the active session",
        )
        async def list_conversations_command(interaction: discord.Interaction) -> None:
            await self._handle_slash_command(interaction, "/list_conversations")

        @app_commands.describe(target="Session key or alias")
        @tree.command(name="preview_session", description="Preview a session")
        async def preview_session_command(
            interaction: discord.Interaction,
            target: str,
        ) -> None:
            await self._handle_slash_command(interaction, f"/preview_session {target}")

        @app_commands.describe(query="Search text")
        @tree.command(name="search_session", description="Search session history")
        async def search_session_command(
            interaction: discord.Interaction,
            query: str,
        ) -> None:
            await self._handle_slash_command(interaction, f"/search_session {query}")

        @app_commands.describe(target="Session key or alias")
        @tree.command(name="switch_session", description="Switch the active session")
        async def switch_session_command(
            interaction: discord.Interaction,
            target: str,
        ) -> None:
            await self._handle_slash_command(interaction, f"/switch_session {target}")

        @app_commands.describe(target="Conversation id or 1-based index")
        @tree.command(
            name="switch_conversation", description="Switch the active conversation"
        )
        async def switch_conversation_command(
            interaction: discord.Interaction,
            target: str,
        ) -> None:
            await self._handle_slash_command(
                interaction, f"/switch_conversation {target}"
            )

        @app_commands.describe(
            alias="Alias to assign",
            session="Optional session key or alias to rename",
        )
        @tree.command(
            name="rename_session",
            description="Rename the current or a referenced session",
        )
        async def rename_session_command(
            interaction: discord.Interaction,
            alias: str,
            session: Optional[str] = None,
        ) -> None:
            raw = (
                f"/rename_session {alias}"
                if not session
                else f"/rename_session {session} {alias}"
            )
            await self._handle_slash_command(interaction, raw)

    async def _sync_slash_commands(self, bot: _DiscordGatewayBot) -> None:
        guild_ids = [
            int(str(raw).strip())
            for raw in self.config.command_guild_ids
            if str(raw).strip()
        ]
        for guild_id in guild_ids:
            guild = discord.Object(id=guild_id)
            bot.tree.copy_global_to(guild=guild)
            synced = await bot.tree.sync(guild=guild)
            logger.info(
                "Discord slash commands synced to guild_id=%s count=%s.",
                guild_id,
                len(synced),
            )
        synced = await bot.tree.sync()
        logger.info("Discord global slash commands synced count=%s.", len(synced))

    async def _on_ready(self, bot: _DiscordGatewayBot) -> None:
        if bot.user is not None:
            self._self_user_id = bot.user.id
            logger.info(
                "Discord app connected as user_id=%s username=%s.",
                bot.user.id,
                getattr(bot.user, "name", "-"),
            )
        self._ready_event.set()

    async def _on_message(self, message) -> None:
        if self._bot is None:
            return
        author = getattr(message, "author", None)
        if author is None or getattr(author, "bot", False):
            return
        text = getattr(message, "content", None) or ""
        audio_attachment = self._extract_audio_attachment(message)
        voice_input_available = (
            self._voice_client is not None and self.voice_settings.input_enabled
        )
        if audio_attachment is not None and not text.strip() and voice_input_available:
            await self._handle_voice_message(message, audio_attachment)
            return
        inbound_attachments = list(getattr(message, "attachments", None) or [])
        if not text.strip() and not inbound_attachments:
            return
        if not self._is_allowed(message):
            return
        if not self._should_process_message(message, text):
            return
        normalized_text = self._normalize_text(text)
        if not normalized_text and not inbound_attachments:
            return

        chat_id, thread_id = self._resolve_channel_ids(
            getattr(message, "channel", None)
        )
        user_id = str(getattr(author, "id", "discord_unknown"))
        session = self.application.resolve_session(
            channel_name=self.channel_name,
            user_id=user_id,
            chat_id=chat_id,
            thread_id=thread_id,
        )
        activity_context = ActivityContext(
            chat_id=chat_id,
            thread_id=thread_id,
            message_id=str(getattr(message, "id", "")),
        )
        try:
            attachments = await self._store_message_attachments(
                inbound_attachments,
                session_key=session.session_key,
                conversation_id=session.conversation_id,
                caption=normalized_text or None,
            )
        except AttachmentValidationError as exc:
            await self._send_text(
                chat_id=chat_id,
                thread_id=thread_id,
                text=str(exc),
                reply_to_message_id=str(getattr(message, "id", "")),
            )
            return
        if attachments and normalized_text.startswith("/"):
            await self._send_text(
                chat_id=chat_id,
                thread_id=thread_id,
                text="Slash commands cannot include attachments.",
                reply_to_message_id=str(getattr(message, "id", "")),
            )
            return
        if normalized_text.startswith("/") and not attachments:
            try:
                handled = await self.run_with_activity_indicator(
                    context=activity_context,
                    awaitable=self._handle_gateway_command(
                        raw_text=normalized_text,
                        user_id=user_id,
                        chat_id=chat_id,
                        thread_id=thread_id,
                        message_id=str(getattr(message, "id", "")),
                    ),
                )
            except TimeoutError:
                await self.send_timeout_notice(
                    context=activity_context,
                    timeout_seconds=self.request_timeout_seconds,
                )
                return
        else:
            handled = await self._handle_gateway_command(
                raw_text=normalized_text,
                user_id=user_id,
                chat_id=chat_id,
                thread_id=thread_id,
                message_id=str(getattr(message, "id", "")),
            )
        if handled is not None:
            await self._send_outbound(handled)
            return

        inbound = ChannelInboundMessage(
            channel_name=self.channel_name,
            user_id=user_id,
            chat_id=chat_id,
            thread_id=thread_id,
            text=normalized_text,
            message_id=str(getattr(message, "id", "")),
            session_key=session.session_key,
            attachments=attachments,
        )
        logger.info(
            "Discord inbound to manager: user_id=%s chat_id=%s thread_id=%s message_id=%s session=%s attachment_count=%d preview=%r",
            inbound.user_id,
            inbound.chat_id,
            inbound.thread_id or "-",
            inbound.message_id,
            session.session_key,
            len(attachments),
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

    async def _handle_voice_message(self, message, audio_attachment) -> None:
        if not self._is_allowed(message):
            return
        if not self._should_process_message(
            message, getattr(message, "content", None) or ""
        ):
            return
        if self._voice_client is None or not self.voice_settings.input_enabled:
            logger.info(
                "Ignoring Discord voice message because voice input is disabled: message_id=%s "
                "voice_enabled=%s input_enabled=%s output_enabled=%s provider=%s client_available=%s.",
                getattr(message, "id", "-"),
                self.voice_settings.enabled,
                self.voice_settings.input_enabled,
                self.voice_settings.output_enabled,
                self.voice_settings.provider,
                self._voice_client is not None,
            )
            chat_id, thread_id = self._resolve_channel_ids(
                getattr(message, "channel", None)
            )
            await self._send_text(
                chat_id=chat_id,
                thread_id=thread_id,
                text=VOICE_UNAVAILABLE_TEXT,
                reply_to_message_id=str(getattr(message, "id", "")),
            )
            return

        chat_id, thread_id = self._resolve_channel_ids(
            getattr(message, "channel", None)
        )
        user_id = str(
            getattr(getattr(message, "author", None), "id", "discord_unknown")
        )
        activity_context = ActivityContext(
            chat_id=chat_id,
            thread_id=thread_id,
            message_id=str(getattr(message, "id", "")),
        )
        try:
            voice_turn = await self.run_with_activity_indicator(
                context=activity_context,
                awaitable=self._handle_voice_turn(
                    message=message,
                    audio_attachment=audio_attachment,
                    user_id=user_id,
                    chat_id=chat_id,
                    thread_id=thread_id,
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
                "Discord voice request failed before a manager reply could be delivered: message_id=%s.",
                getattr(message, "id", "-"),
                exc_info=True,
            )
            await self._send_text(
                chat_id=chat_id,
                thread_id=thread_id,
                text=f"Voice request failed: {exc}",
                reply_to_message_id=str(getattr(message, "id", "")),
            )
            return
        for outbound in voice_turn.messages:
            await self._send_voice_outbound(
                outbound, request_text=voice_turn.request_text
            )

    async def _handle_voice_turn(
        self,
        *,
        message,
        audio_attachment,
        user_id: str,
        chat_id: str,
        thread_id: Optional[str],
    ) -> _DiscordVoiceTurnResult:
        audio = await audio_attachment.read()
        transcription = await self._voice_client.transcribe(
            TranscriptionRequest(
                audio=bytes(audio),
                filename=str(
                    getattr(audio_attachment, "filename", None) or "discord_audio"
                ),
                content_type=self._attachment_content_type(audio_attachment),
            )
        )
        normalized_text = self._normalize_text(transcription.text)
        if not normalized_text:
            return _DiscordVoiceTurnResult(
                request_text="",
                messages=[
                    ChannelOutboundMessage(
                        channel_name=self.channel_name,
                        chat_id=chat_id,
                        thread_id=thread_id,
                        text="I could not transcribe that voice message.",
                        in_reply_to_message_id=str(getattr(message, "id", "")),
                    )
                ],
            )
        inbound = ChannelInboundMessage(
            channel_name=self.channel_name,
            user_id=user_id,
            chat_id=chat_id,
            thread_id=thread_id,
            text=normalized_text,
            message_id=str(getattr(message, "id", "")),
        )
        active_session = self.application.get_active_session(
            channel_name=self.channel_name,
            user_id=inbound.user_id,
            chat_id=inbound.chat_id,
            thread_id=inbound.thread_id,
        )
        logger.info(
            "Discord voice inbound to manager: user_id=%s chat_id=%s thread_id=%s message_id=%s session=%s preview=%r",
            inbound.user_id,
            inbound.chat_id,
            inbound.thread_id or "-",
            inbound.message_id,
            active_session.session_key if active_session is not None else "-",
            preview_text(inbound.text),
        )
        return _DiscordVoiceTurnResult(
            request_text=normalized_text,
            messages=await self.bridge.handle_inbound(inbound),
        )

    async def _handle_slash_command(
        self,
        interaction,
        raw_text: str,
    ) -> None:
        user = getattr(interaction, "user", None)
        channel = getattr(interaction, "channel", None)
        user_id = str(getattr(user, "id", "discord_unknown"))
        chat_id, thread_id = self._resolve_channel_ids(channel)
        activity_context = ActivityContext(
            chat_id=chat_id,
            thread_id=thread_id,
            interaction=interaction,
        )
        try:
            outbound = await self.run_with_activity_indicator(
                context=activity_context,
                awaitable=self._handle_gateway_command(
                    raw_text=raw_text,
                    user_id=user_id,
                    chat_id=chat_id,
                    thread_id=thread_id,
                    message_id=None,
                ),
            )
        except TimeoutError:
            await self.send_timeout_notice(
                context=activity_context,
                timeout_seconds=self.request_timeout_seconds,
            )
            return
        if outbound is None:
            outbound = ChannelOutboundMessage(
                channel_name=self.channel_name,
                chat_id=chat_id,
                thread_id=thread_id,
                text="Unsupported command.",
            )
        await self._send_outbound(outbound, interaction=interaction)

    async def _handle_gateway_command(
        self,
        *,
        raw_text: str,
        user_id: str,
        chat_id: str,
        thread_id: Optional[str],
        message_id: Optional[str],
    ) -> Optional[ChannelOutboundMessage]:
        command = raw_text.strip()
        if not command.startswith("/"):
            return None

        result = await self.application.execute_interactive_command(
            raw_text=command,
            channel_name=self.channel_name,
            user_id=user_id,
            chat_id=chat_id,
            thread_id=thread_id,
            help_text=DISCORD_HELP_TEXT,
            enable_reply_command=False,
        )
        if result.handled:
            return ChannelOutboundMessage(
                channel_name=self.channel_name,
                chat_id=chat_id,
                thread_id=thread_id,
                text=result.text or "",
                in_reply_to_message_id=message_id,
                session_key=result.session_key,
                suppress_delivery=result.deferred,
            )

        return None

    def _resolve_channel_ids(self, channel) -> tuple[str, Optional[str]]:
        if channel is None:
            return "", None
        thread_cls = getattr(discord, "Thread", None) if discord is not None else None
        if (
            thread_cls is not None
            and isinstance(channel, thread_cls)
            and getattr(channel, "parent", None) is not None
        ):
            chat_id = str(channel.parent.id)
            thread_id = str(channel.id) if self.config.thread_scoped_sessions else None
            return chat_id, thread_id
        return str(getattr(channel, "id", "")), None

    def _warn_if_allow_all(self) -> None:
        if self.config.allowed_user_ids or self.config.allowed_channel_ids:
            return
        logger.warning(
            "Discord interactive app is enabled with empty allowed_user_ids and "
            "allowed_channel_ids; all Discord users and channels reachable by the "
            "bot can start sessions until an allowlist is configured."
        )

    def _is_allowed(self, message) -> bool:
        author = getattr(message, "author", None)
        channel = getattr(message, "channel", None)
        user_id = str(getattr(author, "id", "discord_unknown"))
        channel_id = str(getattr(channel, "id", ""))
        chat_id, _thread_id = self._resolve_channel_ids(channel)
        channel_type = getattr(
            channel,
            "type",
            type(channel).__name__ if channel is not None else "unknown",
        )
        registered_user = self.is_registered_gateway_user(user_id)
        is_direct_message = getattr(message, "guild", None) is None
        if registered_user and is_direct_message:
            return True
        if (
            self.config.allowed_channel_ids
            and channel_id not in self.config.allowed_channel_ids
            and chat_id not in self.config.allowed_channel_ids
        ):
            logger.info(
                "Ignoring Discord message from non-allowed channel: channel_id=%s chat_id=%s channel_type=%s "
                "user_id=%s newuser_target=%s.",
                channel_id,
                chat_id,
                channel_type,
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
                "Ignoring Discord message from non-allowed user: user_id=%s channel_id=%s chat_id=%s "
                "channel_type=%s newuser_target=%s.",
                user_id,
                channel_id,
                chat_id,
                channel_type,
                user_id,
            )
            return False
        return True

    def _should_process_message(self, message, text: str) -> bool:
        if getattr(message, "guild", None) is None:
            return True
        if self.config.group_policy == "open":
            return True
        if self._message_mentions_bot(message, text):
            return True
        reference = getattr(message, "reference", None)
        resolved = (
            getattr(reference, "resolved", None) if reference is not None else None
        )
        author = getattr(resolved, "author", None) if resolved is not None else None
        return bool(
            author is not None and getattr(author, "id", None) == self._self_user_id
        )

    def _message_mentions_bot(self, message, text: str) -> bool:
        if self._self_user_id is None:
            return False
        mentions = list(getattr(message, "mentions", None) or [])
        for mention in mentions:
            if getattr(mention, "id", None) == self._self_user_id:
                return True
        pattern = rf"<@!?{self._self_user_id}>"
        return re.search(pattern, text or "") is not None

    def _normalize_text(self, text: str) -> str:
        normalized = (text or "").strip()
        if self._self_user_id is not None:
            normalized = re.sub(
                rf"<@!?{self._self_user_id}>",
                "",
                normalized,
            ).strip()
        return normalized

    def _extract_audio_attachment(self, message):
        for attachment in list(getattr(message, "attachments", None) or []):
            content_type = self._attachment_content_type(attachment)
            filename = str(getattr(attachment, "filename", "") or "").lower()
            if content_type.startswith("audio/") or filename.endswith(
                (".mp3", ".ogg", ".oga", ".wav", ".m4a", ".webm")
            ):
                return attachment
        return None

    def _attachment_content_type(self, attachment) -> str:
        return str(
            getattr(attachment, "content_type", None) or "application/octet-stream"
        )

    async def _store_message_attachments(
        self,
        attachments: list,
        *,
        session_key: str,
        conversation_id: Optional[str],
        caption: Optional[str],
    ):
        stored_attachments = []
        for attachment in attachments:
            filename = str(
                getattr(attachment, "filename", None) or "discord_attachment"
            )
            content_type = self._attachment_content_type(attachment)
            data = await attachment.read()
            stored = self.attachments.store_bytes(
                data=bytes(data),
                filename=filename,
                content_type=content_type,
                session_key=session_key,
                conversation_id=conversation_id,
                origin="discord",
                caption=caption,
            )
            stored_attachments.append(stored.attachment)
        return stored_attachments

    def _render_outbound_text(self, outbound: ChannelOutboundMessage) -> str:
        return outbound.text

    async def _send_outbound(
        self,
        outbound: ChannelOutboundMessage,
        *,
        interaction=None,
    ) -> None:
        if outbound.suppress_delivery:
            return
        rendered_text = self._render_outbound_text(outbound)
        controls = self.review_controls_for(outbound)
        review_view = self._review_view(controls, outbound.review_url) if controls else None
        if interaction is not None:
            await self._send_interaction_text(
                interaction,
                rendered_text,
                view=review_view,
            )
        else:
            await self._send_text(
                chat_id=outbound.chat_id,
                thread_id=outbound.thread_id,
                text=rendered_text,
                reply_to_message_id=outbound.in_reply_to_message_id,
                view=review_view,
            )
        logger.info(
            "Discord outbound to user: chat_id=%s thread_id=%s reply_to=%s session=%s conversation=%s preview=%r",
            outbound.chat_id,
            outbound.thread_id or "-",
            outbound.in_reply_to_message_id or "-",
            outbound.session_key or "-",
            outbound.conversation_id or "-",
            preview_text(outbound.text),
        )

    async def _send_outbound_audio(self, outbound: ChannelOutboundMessage) -> None:
        speech_text, trimmed_history = speech_text_for_outbound_reply(outbound.text)
        if trimmed_history:
            logger.warning(
                "Discord voice outbound text looked like copied conversation history; speaking latest final answer only "
                "(chat_id=%s thread_id=%s session=%s conversation=%s).",
                outbound.chat_id,
                outbound.thread_id or "-",
                outbound.session_key or "-",
                outbound.conversation_id or "-",
            )
        speech = await self._voice_client.synthesize(SpeechRequest(text=speech_text))
        rendered_text = (
            self._render_outbound_text(outbound) if not speech.audio else None
        )
        await self._send_audio(
            chat_id=outbound.chat_id,
            thread_id=outbound.thread_id,
            audio=speech.audio,
            content_type=speech.content_type,
            filename=self._audio_filename(speech.content_type),
            reply_to_message_id=outbound.in_reply_to_message_id,
            fallback_content=rendered_text,
        )
        logger.info(
            "Discord voice outbound to user: chat_id=%s thread_id=%s reply_to=%s session=%s conversation=%s preview=%r",
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
                "Discord voice reply synthesis failed after text delivery: chat_id=%s thread_id=%s session=%s conversation=%s.",
                outbound.chat_id,
                outbound.thread_id or "-",
                outbound.session_key or "-",
                outbound.conversation_id or "-",
                exc_info=True,
            )
            await self._send_text(
                chat_id=outbound.chat_id,
                thread_id=outbound.thread_id,
                text=f"Voice reply failed: {exc}",
                reply_to_message_id=outbound.in_reply_to_message_id,
            )

    async def _send_audio(
        self,
        *,
        chat_id: str,
        thread_id: Optional[str],
        audio: bytes,
        content_type: str,
        filename: str,
        reply_to_message_id: Optional[str],
        fallback_content: Optional[str] = None,
    ) -> None:
        if self._bot is None:
            raise RuntimeError("Discord app is not running.")
        target = await self._resolve_target_channel(
            chat_id=chat_id, thread_id=thread_id
        )
        if target is None:
            logger.warning(
                "Discord audio outbound target was not found for chat_id=%s thread_id=%s.",
                chat_id,
                thread_id or "-",
            )
            return
        reference = None
        if self.config.reply_to_message and reply_to_message_id:
            try:
                message_id = int(reply_to_message_id)
            except ValueError:
                message_id = None
            if message_id is not None and hasattr(target, "get_partial_message"):
                reference = target.get_partial_message(message_id)
        kwargs = {"content": fallback_content or ""}
        if reference is not None:
            kwargs["reference"] = reference
            kwargs["mention_author"] = False
        discord_file_cls = (
            getattr(discord, "File", None) if discord is not None else None
        )
        if discord_file_cls is not None and audio:
            kwargs["file"] = discord_file_cls(io.BytesIO(audio), filename=filename)
        await target.send(**kwargs)

    def _audio_filename(self, content_type: str) -> str:
        normalized = str(content_type or "").split(";")[0].strip().lower()
        if normalized == "audio/wav":
            return "reply.wav"
        if normalized == "audio/ogg":
            return "reply.ogg"
        return "reply.mp3"

    async def _send_interaction_text(
        self, interaction, text: str, *, view=None
    ) -> None:
        chunks = _split_discord_text(text)
        if not chunks:
            chunks = [""]
        response = getattr(interaction, "response", None)
        followup = getattr(interaction, "followup", None)
        if response is None:
            raise RuntimeError("Discord interaction is missing a response handle.")
        if hasattr(response, "is_done") and response.is_done():
            for chunk_index, chunk in enumerate(chunks):
                if followup is None:
                    raise RuntimeError(
                        "Discord interaction followup handle is not available."
                    )
                if view is not None and chunk_index == len(chunks) - 1:
                    await followup.send(chunk, view=view)
                else:
                    await followup.send(chunk)
            return
        if view is not None and len(chunks) == 1:
            await response.send_message(chunks[0], view=view)
        else:
            await response.send_message(chunks[0])
        if followup is None:
            return
        for chunk_index, chunk in enumerate(chunks[1:], start=1):
            if view is not None and chunk_index == len(chunks) - 1:
                await followup.send(chunk, view=view)
            else:
                await followup.send(chunk)

    async def _send_text(
        self,
        *,
        chat_id: str,
        thread_id: Optional[str],
        text: str,
        reply_to_message_id: Optional[str],
        view=None,
    ) -> None:
        if self._bot is None:
            raise RuntimeError("Discord app is not running.")
        target = await self._resolve_target_channel(
            chat_id=chat_id, thread_id=thread_id
        )
        if target is None:
            logger.warning(
                "Discord outbound target was not found for chat_id=%s thread_id=%s.",
                chat_id,
                thread_id or "-",
            )
            return
        reference = None
        if self.config.reply_to_message and reply_to_message_id:
            try:
                message_id = int(reply_to_message_id)
            except ValueError:
                message_id = None
            if message_id is not None and hasattr(target, "get_partial_message"):
                reference = target.get_partial_message(message_id)
        first_chunk = True
        chunks = _split_discord_text(text)
        for chunk_index, chunk in enumerate(chunks):
            kwargs = {"content": chunk}
            if first_chunk and reference is not None:
                kwargs["reference"] = reference
                kwargs["mention_author"] = False
            if view is not None and chunk_index == len(chunks) - 1:
                kwargs["view"] = view
            await target.send(**kwargs)
            first_chunk = False

    def _review_view(self, controls: tuple[ReviewControl, ...], review_url: str | None):
        """Buttons for an access review; presses reach ``on_interaction`` (no timeout)."""

        if discord is None or not hasattr(discord, "ui"):
            return None
        styles = {
            "approve": discord.ButtonStyle.success,
            "approve_read": discord.ButtonStyle.primary,
            "approve_task": discord.ButtonStyle.secondary,
            "deny": discord.ButtonStyle.danger,
        }
        view = discord.ui.View(timeout=None)
        for index, control in enumerate(controls):
            view.add_item(
                discord.ui.Button(
                    label=control.label,
                    style=styles.get(control.choice, discord.ButtonStyle.secondary),
                    custom_id=control.data,
                    row=min(index // 2, 3),
                )
            )
        if review_url and review_url.startswith("https://"):
            view.add_item(discord.ui.Button(label="Open Inbox", url=review_url, row=4))
        return view

    async def _on_component_interaction(self, interaction) -> None:
        """An access review button: decide as the pressing person's linked gateway user.

        The button data holds only the request id and the choice; the gateway
        looks up the person from this Discord account and checks their
        current roles. The announcement is then updated with the outcome.
        """

        data = getattr(interaction, "data", None)
        custom_id = str(data.get("custom_id") or "") if isinstance(data, dict) else ""
        if not custom_id.startswith(ACCESS_REVIEW_CONTROL_PREFIX):
            return
        await interaction.response.defer(ephemeral=True)
        chat_id, thread_id = self._resolve_channel_ids(getattr(interaction, "channel", None))
        result = await self.decide_review_control(
            custom_id,
            user_id=str(getattr(getattr(interaction, "user", None), "id", "") or ""),
            chat_id=chat_id,
            thread_id=thread_id,
        )
        if result is None:
            return
        if result.closes_controls:
            await self._close_review_message(getattr(interaction, "message", None), result.text)
        await interaction.followup.send(result.text, ephemeral=True)

    async def _close_review_message(self, message, outcome: str) -> None:
        """Replace the buttons with the outcome line; keep the announcement text."""

        edit = getattr(message, "edit", None)
        if not callable(edit):
            return
        original = str(getattr(message, "content", "") or "").strip()
        updated = f"{original}\n\n{outcome}" if original else outcome
        try:
            await edit(content=updated[:DISCORD_MAX_MESSAGE_LEN], view=None)
        except Exception:
            logger.warning(
                "Could not add the outcome to a Discord access review. "
                "event=discord_access_review_edit_failed message_id=%s",
                getattr(message, "id", "-"),
                exc_info=True,
            )

    async def _send_event_attachments(self, event: GatewayDeliveryEvent) -> None:
        if self._bot is None:
            raise RuntimeError("Discord app is not running.")
        target = await self._resolve_target_channel(
            chat_id=event.chat_id or "", thread_id=event.thread_id
        )
        if target is None:
            logger.warning(
                "Discord attachment target was not found for chat_id=%s thread_id=%s.",
                event.chat_id or "-",
                event.thread_id or "-",
            )
            return
        reference = None
        if self.config.reply_to_message and event.original_message_id:
            try:
                message_id = int(event.original_message_id)
            except ValueError:
                message_id = None
            if message_id is not None and hasattr(target, "get_partial_message"):
                reference = target.get_partial_message(message_id)
        discord_file_cls = (
            getattr(discord, "File", None) if discord is not None else None
        )
        if discord_file_cls is None:
            logger.warning(
                "Discord attachment delivery skipped because discord.File is unavailable."
            )
            return
        first = True
        # Only the checked bytes of the gateway's staged copy are sent; a copy
        # that changed or became a link after staging is skipped (and logged).
        for attachment, data in self.outgoing_attachments.readable(event.attachments):
            filename = attachment.filename or Path(attachment.path).name
            file_obj = discord_file_cls(io.BytesIO(data), filename=filename)
            kwargs = {"content": attachment.caption or "", "file": file_obj}
            if first and reference is not None:
                kwargs["reference"] = reference
                kwargs["mention_author"] = False
            await target.send(**kwargs)
            first = False

    async def _resolve_target_channel(self, *, chat_id: str, thread_id: Optional[str]):
        if self._bot is None:
            raise RuntimeError("Discord app is not running.")
        target_id_text = thread_id or chat_id
        if not target_id_text:
            return None
        try:
            target_id = int(target_id_text)
        except ValueError:
            logger.warning(
                "Discord outbound target has non-numeric id=%s", target_id_text
            )
            return None
        target = self._bot.get_channel(target_id)
        if target is not None:
            return target
        try:
            return await self._bot.fetch_channel(target_id)
        except Exception:
            logger.warning(
                "Discord outbound target lookup failed for id=%s.",
                target_id,
                exc_info=True,
            )
            return None

    async def emit_native_activity_indicator(self, context: ActivityContext) -> None:
        if context.interaction is not None:
            await self._defer_interaction(context.interaction)
            return
        channel = await self._resolve_target_channel(
            chat_id=context.chat_id or "",
            thread_id=context.thread_id,
        )
        if channel is None:
            return
        await self._trigger_typing(channel)

    async def emit_text_activity_update(
        self, context: ActivityContext, text: str
    ) -> None:
        if context.interaction is not None:
            await self._send_interaction_text(context.interaction, text)
            return
        await self._send_text(
            chat_id=context.chat_id or "",
            thread_id=context.thread_id,
            text=text,
            reply_to_message_id=context.message_id,
        )

    async def _defer_interaction(self, interaction) -> None:
        response = getattr(interaction, "response", None)
        if response is None:
            return
        if hasattr(response, "is_done") and response.is_done():
            return
        defer = getattr(response, "defer", None)
        if defer is None:
            return
        try:
            result = defer(thinking=True)
            if inspect.isawaitable(result):
                await result
        except Exception:
            logger.debug("Discord interaction defer failed.", exc_info=True)

    async def _trigger_typing(self, channel) -> None:
        trigger = getattr(channel, "trigger_typing", None)
        if trigger is None:
            return
        try:
            result = trigger()
            if inspect.isawaitable(result):
                await result
        except Exception:
            logger.debug(
                "Discord typing indicator failed for channel_id=%s.",
                getattr(channel, "id", "-"),
                exc_info=True,
            )
