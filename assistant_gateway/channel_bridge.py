"""Phase 2 channel-agnostic bridge built on top of the Phase 1 gateway."""

from __future__ import annotations

import json
import logging

from assistant_gateway.application.gateway_application import GatewayApplication
from assistant_gateway.inbound_attachments import attachment_debug_summary
from assistant_gateway.translation import (
    ChannelInboundMessage,
    ChannelOutboundMessage,
    DeterministicTextTranslator,
)
from time_presentation import format_user_timestamp

ATTACHMENT_ONLY_TASK_TEXT = "User sent attachment(s)."
logger = logging.getLogger(__name__)


class ChannelBridge:
    """Translate channel text into gateway actions and back into channel text."""

    def __init__(
        self,
        application: GatewayApplication,
        *,
        translator: DeterministicTextTranslator | None = None,
    ):
        self.application = application
        self.translator = translator or DeterministicTextTranslator()

    async def handle_inbound(
        self, inbound: ChannelInboundMessage
    ) -> list[ChannelOutboundMessage]:
        inbound_attachments = list(getattr(inbound, "attachments", None) or [])
        inbound_text = (inbound.text or "").strip()
        resolved_session_key = inbound.session_key
        if resolved_session_key is None:
            active_session = self.application.get_active_session(
                channel_name=inbound.channel_name,
                user_id=inbound.user_id,
                chat_id=inbound.chat_id,
                thread_id=inbound.thread_id,
            )
            resolved_session_key = (
                active_session.session_key if active_session else None
            )

        if self.application.is_task_progress_status_request(
            inbound_text, inbound_attachments
        ):
            response = await self.application.request_task_progress_status(
                session_key=resolved_session_key,
                channel_name=inbound.channel_name,
                user_id=inbound.user_id,
                chat_id=inbound.chat_id,
                thread_id=inbound.thread_id,
            )
            return [
                self.translator.render_outbound(
                    inbound=inbound,
                    text=response.text,
                    session_key=resolved_session_key,
                    conversation_id=response.conversation_id,
                    correlation_id=response.correlation_id,
                    message_type=response.message_type,
                )
            ]

        waiting_question_id = (
            await self.application.waiting_question_id(resolved_session_key)
            if resolved_session_key
            else None
        )
        if inbound_attachments and waiting_question_id:
            return [
                self.translator.render_outbound(
                    inbound=inbound,
                    text="Attachments cannot be used as a reply to a pending question. Send a text answer first, then send the attachment.",
                    session_key=resolved_session_key,
                )
            ]
        if inbound_attachments and inbound_text.startswith("/"):
            return [
                self.translator.render_outbound(
                    inbound=inbound,
                    text="Slash commands cannot include attachments.",
                    session_key=resolved_session_key,
                )
            ]
        if inbound_attachments and not inbound_text:
            inbound = ChannelInboundMessage(
                channel_name=inbound.channel_name,
                user_id=inbound.user_id,
                chat_id=inbound.chat_id,
                text=ATTACHMENT_ONLY_TASK_TEXT,
                thread_id=inbound.thread_id,
                session_key=inbound.session_key,
                message_id=inbound.message_id,
                reply_to_message_id=inbound.reply_to_message_id,
                replied_message=inbound.replied_message,
                attachments=inbound_attachments,
            )
        action = self.translator.translate_inbound(
            inbound,
            has_pending_query=waiting_question_id is not None,
        )
        if inbound_attachments:
            logger.info(
                "Channel bridge routed inbound message. channel=%s session=%s action=%s attachment_count=%d attachment_summary=%s",
                inbound.channel_name,
                resolved_session_key or inbound.session_key or "-",
                action.action_type,
                len(inbound_attachments),
                json.dumps(
                    attachment_debug_summary(inbound_attachments), sort_keys=True
                ),
            )

        if action.action_type == "new_session":
            session = self.application.start_new_session(
                channel_name=inbound.channel_name,
                user_id=inbound.user_id,
                chat_id=inbound.chat_id,
                thread_id=inbound.thread_id,
            )
            return [
                self.translator.render_outbound(
                    inbound=inbound,
                    text=f"Started new session: {session.session_key}",
                    session_key=session.session_key,
                    conversation_id=session.conversation_id,
                )
            ]

        if (
            action.action_type == "reply_to_query"
            and resolved_session_key
            and waiting_question_id
        ):
            # The answer names the question found above, so a question shown
            # after this message arrived is never answered by it.
            response = await self.application.reply_to_pending_query(
                session_key=resolved_session_key,
                task_query_id=waiting_question_id,
                response_text=action.text or "",
                replied_message=inbound.replied_message,
            )
            return [
                self.translator.render_outbound(
                    inbound=inbound,
                    text=response.text,
                    session_key=action.session_key or resolved_session_key,
                    conversation_id=response.conversation_id,
                    correlation_id=response.correlation_id,
                    message_type=response.message_type,
                )
            ]

        if action.action_type == "list_cron_jobs":
            jobs = await self.application.list_scheduled_jobs(
                session_key=action.session_key or resolved_session_key,
                channel_name=inbound.channel_name,
                user_id=inbound.user_id,
                chat_id=inbound.chat_id,
                thread_id=inbound.thread_id,
            )
            if not jobs:
                text = "No scheduled jobs."
            else:
                timezone_name = self.application.requester_timezone_for_context(
                    channel_name=inbound.channel_name,
                    user_id=inbound.user_id,
                    chat_id=inbound.chat_id,
                )
                lines = ["Scheduled jobs:"]
                for job in jobs:
                    next_fire_at = format_user_timestamp(
                        job.next_fire_at,
                        timezone_name=timezone_name,
                    )
                    lines.append(
                        f"- {job.scheduled_job_id} next={next_fire_at} "
                        f"task={job.task_summary}"
                    )
                text = "\n".join(lines)
            return [
                self.translator.render_outbound(
                    inbound=inbound,
                    text=text,
                    session_key=action.session_key or resolved_session_key,
                )
            ]

        if action.action_type == "deliver_pending":
            releases = await self.application.release_due_deliveries()
            text = "\n\n".join(releases) if releases else "No due deferred deliveries."
            return [
                self.translator.render_outbound(
                    inbound=inbound,
                    text=text,
                    session_key=action.session_key or resolved_session_key,
                )
            ]

        if action.action_type == "add_cron_job":
            job_id = await self.application.add_cron_job(
                session_key=action.session_key or resolved_session_key,
                name=action.cron_name or "scheduled_job",
                instruction=action.cron_instruction or "",
                interval_seconds=action.interval_seconds,
                cron_expression=action.cron_expression,
                channel_name=inbound.channel_name,
                user_id=inbound.user_id,
                chat_id=inbound.chat_id,
                thread_id=inbound.thread_id,
                original_message_id=inbound.message_id,
            )
            return [
                self.translator.render_outbound(
                    inbound=inbound,
                    text=f"Scheduled reminder created: {job_id}",
                    session_key=action.session_key or resolved_session_key,
                    conversation_id=self.application.resolve_session(
                        session_key=action.session_key or resolved_session_key,
                        channel_name=inbound.channel_name,
                        user_id=inbound.user_id,
                        chat_id=inbound.chat_id,
                        thread_id=inbound.thread_id,
                    ).conversation_id,
                )
            ]

        return await self._submit_inbound(
            inbound, action.session_key or resolved_session_key, action.text or ""
        )

    async def _submit_inbound(
        self,
        inbound: ChannelInboundMessage,
        session_key: str | None,
        text: str,
    ) -> list[ChannelOutboundMessage]:
        """Submit a task through the authenticated application path."""
        response = await self.application.send_user_message(
            text,
            session_key=session_key,
            channel_name=inbound.channel_name,
            user_id=inbound.user_id,
            chat_id=inbound.chat_id,
            thread_id=inbound.thread_id,
            original_message_id=inbound.message_id,
            attachments=list(inbound.attachments) or None,
            replied_message=inbound.replied_message,
        )
        return [
            self.translator.render_outbound(
                inbound=inbound,
                text=response.text,
                session_key=session_key,
                conversation_id=response.conversation_id,
                correlation_id=response.correlation_id,
                message_type=response.message_type,
            )
        ]
