"""Gateway-side translation between protocol requests and application use cases."""

from __future__ import annotations

import hashlib
import logging
from collections.abc import Callable, Sequence
from datetime import datetime, timezone
from pathlib import Path

from assistant_gateway.adapter_host.domain import AdapterUploadRecord
from assistant_gateway.adapter_host.replied_messages import replied_message_from_wire
from assistant_gateway.adapter_host.service import (
    AdapterHostApplicationService,
    AdapterHostRequestHandler,
)
from assistant_gateway.interactive_apps.host_ports import InteractiveAppAttachmentStore
from assistant_gateway.application.gateway_application import (
    GatewayApplication,
    GatewayDeliveryEvent,
    GatewaySession,
)
from assistant_gateway.application.access_reviews import AccessReviewDecisionRequestV1
from assistant_gateway.channel_bridge import ChannelBridge
from assistant_gateway.contracts.adapter_host_v1 import (
    ACCESS_REVIEW_DECISION_COMMAND,
    ADAPTER_HOST_PROTOCOL_VERSION,
    AdapterAttachmentV1,
    ChannelCommandV1,
    ChannelInboundMessageV1,
    DeliveryMessageLevelV1,
    GatewayDeliveryEventV1,
)
from assistant_gateway.translation import ChannelInboundMessage, ChannelOutboundMessage
from custom_messages.contracts.attachments_v1 import AttachmentOrigin, UserTaskAttachment
from id_generation import new_id


LOGGER = logging.getLogger(__name__)
PluginRouteResolver = Callable[[str], tuple[str, str]]
_CHAT_APP_ORIGINS = frozenset({"telegram", "discord", "whatsapp", "signal", "photon"})


class GatewayAdapterRequestHandler(AdapterHostRequestHandler):
    """Execute admitted adapter requests through existing gateway application ports."""

    def __init__(
        self,
        application: GatewayApplication,
        service_resolver: Callable[[], AdapterHostApplicationService],
        *,
        attachments: InteractiveAppAttachmentStore,
    ) -> None:
        self._application = application
        self._bridge = ChannelBridge(application)
        self._service_resolver = service_resolver
        # The gateway's own upload store (state/uploads), the one in-process
        # chat apps save to (gateway_inbound_attachment_policy).
        self._attachments = attachments

    async def handle_inbound(
        self, message: ChannelInboundMessageV1
    ) -> Sequence[GatewayDeliveryEventV1]:
        session = self._application.resolve_session(
            channel_name=message.channel_name,
            user_id=message.external_user_id,
            chat_id=message.chat_id,
            thread_id=message.thread_id,
        )
        service = self._service_resolver()
        uploads = [
            service.resolve_upload_reference(
                plugin_id=message.plugin_id,
                adapter_instance_id=message.adapter_instance_id,
                session_epoch=message.session_epoch,
                storage_reference=attachment.storage_reference,
            )
            for attachment in message.attachments
        ]
        attachments = self._save_uploads(
            uploads, session=session, origin=_attachment_origin(message.channel_name)
        )
        outbound = await self._bridge.handle_inbound(
            ChannelInboundMessage(
                channel_name=message.channel_name,
                user_id=message.external_user_id,
                chat_id=message.chat_id,
                thread_id=message.thread_id,
                text=message.text,
                message_id=message.external_message_id,
                reply_to_message_id=message.reply_to_external_message_id,
                replied_message=replied_message_from_wire(message.replied_message),
                session_key=session.session_key,
                attachments=attachments,
            )
        )
        return tuple(
            _channel_outbound_to_wire(
                item,
                plugin_id=message.plugin_id,
                adapter_instance_id=message.adapter_instance_id,
                session_epoch=message.session_epoch,
            )
            for item in outbound
        )

    def _save_uploads(
        self,
        uploads: Sequence[AdapterUploadRecord],
        *,
        session: GatewaySession,
        origin: AttachmentOrigin,
    ) -> list[UserTaskAttachment]:
        """Save a hosted chat's files where in-process chats save theirs, then drop the transfer copies.

        A hosted chat sends each file into the adapter host's transfer store
        (``interactive_apps.adapter_host.upload_dir``). The gateway checks it
        with the same rules as any file people send and saves it under
        ``state/uploads`` (``self._attachments``), so agents open it by id in
        later turns and the upload retention, history clean-up and person
        departure cover it. Every file is checked before any is saved, so a
        refused file leaves none behind. An inbound message is handled once
        (``AdapterHostApplicationService._process_record`` marks it failed
        rather than trying again), so its transfer copies are removed whether
        or not its files were saved: nothing reads them again.
        """

        try:
            received = [(upload, _transfer_bytes(upload)) for upload in uploads]
            for upload, data in received:
                self._attachments.check_bytes(
                    data=data, filename=upload.filename, content_type=upload.content_type
                )
            return [
                self._attachments.store_bytes(
                    data=data,
                    filename=upload.filename,
                    content_type=upload.content_type,
                    session_key=session.session_key,
                    conversation_id=session.conversation_id,
                    origin=origin,
                ).attachment
                for upload, data in received
            ]
        finally:
            for upload in uploads:
                _remove_transfer_copy(upload)

    async def handle_command(
        self, message: ChannelCommandV1
    ) -> Sequence[GatewayDeliveryEventV1]:
        if message.command_name == ACCESS_REVIEW_DECISION_COMMAND:
            return await self._decide_access_review(message)
        if message.command_name != "interactive.raw":
            raise ValueError(f"Unsupported adapter command {message.command_name!r}.")
        allowed_arguments = {
            "raw_text",
            "current_session_key",
            "help_text",
            "enable_debug_commands",
            "enable_reply_command",
            "enable_channel_binding_commands",
        }
        unexpected = set(message.arguments) - allowed_arguments
        if unexpected:
            raise ValueError(
                "Unsupported interactive command arguments: "
                + ", ".join(sorted(unexpected))
            )
        raw_text = str(message.arguments.get("raw_text") or "")
        result = await self._application.execute_interactive_command(
            raw_text=raw_text,
            channel_name=message.channel_name,
            user_id=message.external_user_id,
            chat_id=message.chat_id,
            thread_id=message.thread_id,
            current_session_key=(
                str(message.arguments["current_session_key"])
                if message.arguments.get("current_session_key")
                else None
            ),
            help_text=(
                str(message.arguments["help_text"])
                if message.arguments.get("help_text")
                else None
            ),
            enable_debug_commands=bool(
                message.arguments.get("enable_debug_commands", True)
            ),
            enable_reply_command=bool(
                message.arguments.get("enable_reply_command", True)
            ),
            enable_channel_binding_commands=bool(
                message.arguments.get("enable_channel_binding_commands", True)
            ),
        )
        if not result.handled or result.text is None:
            return ()
        return (
            GatewayDeliveryEventV1(
                protocol_version=ADAPTER_HOST_PROTOCOL_VERSION,
                message_type="gateway.delivery.v1",
                event_id=new_id("adapter_event"),
                idempotency_key=new_id("adapter_eidem"),
                plugin_id=message.plugin_id,
                adapter_instance_id=message.adapter_instance_id,
                session_epoch=message.session_epoch,
                stream_position=1,
                channel_name=message.channel_name,
                external_user_id=message.external_user_id,
                chat_id=message.chat_id,
                thread_id=message.thread_id,
                in_reply_to_external_message_id=message.external_message_id,
                session_key=result.session_key or "default",
                text=result.text,
                emitted_at_utc=datetime.now(timezone.utc),
            ),
        )

    async def _decide_access_review(self, message: ChannelCommandV1) -> Sequence[GatewayDeliveryEventV1]:
        """A review button pressed in a hosted adapter; the outcome is posted to the chat."""

        if set(message.arguments) != {"request_id", "choice"}:
            raise ValueError(f"{ACCESS_REVIEW_DECISION_COMMAND} requires exactly request_id and choice.")
        result = await self._application.decide_access_review(
            AccessReviewDecisionRequestV1(
                request_id=str(message.arguments["request_id"]),
                choice=str(message.arguments["choice"]),
                channel_name=message.channel_name,
                user_id=message.external_user_id,
                chat_id=message.chat_id,
                thread_id=message.thread_id,
            )
        )
        session = self._application.resolve_session(
            channel_name=message.channel_name,
            user_id=message.external_user_id,
            chat_id=message.chat_id,
            thread_id=message.thread_id,
        )
        return (
            GatewayDeliveryEventV1(
                protocol_version=ADAPTER_HOST_PROTOCOL_VERSION,
                message_type="gateway.delivery.v1",
                event_id=new_id("adapter_event"),
                idempotency_key=new_id("adapter_eidem"),
                plugin_id=message.plugin_id,
                adapter_instance_id=message.adapter_instance_id,
                session_epoch=message.session_epoch,
                stream_position=1,
                channel_name=message.channel_name,
                external_user_id=message.external_user_id,
                chat_id=message.chat_id,
                thread_id=message.thread_id,
                in_reply_to_external_message_id=message.external_message_id,
                session_key=session.session_key,
                text=result.text,
                emitted_at_utc=datetime.now(timezone.utc),
            ),
        )


def _channel_outbound_to_wire(
    outbound: ChannelOutboundMessage,
    *,
    plugin_id: str,
    adapter_instance_id: str,
    session_epoch: str,
) -> GatewayDeliveryEventV1:
    return GatewayDeliveryEventV1(
        protocol_version=ADAPTER_HOST_PROTOCOL_VERSION,
        message_type="gateway.delivery.v1",
        event_id=new_id("adapter_event"),
        idempotency_key=new_id("adapter_eidem"),
        plugin_id=plugin_id,
        adapter_instance_id=adapter_instance_id,
        session_epoch=session_epoch,
        stream_position=1,
        channel_name=outbound.channel_name,
        chat_id=outbound.chat_id,
        thread_id=outbound.thread_id,
        in_reply_to_external_message_id=outbound.in_reply_to_message_id,
        session_key=outbound.session_key or "default",
        conversation_id=outbound.conversation_id,
        correlation_id=outbound.correlation_id,
        content_message_type=outbound.message_type,
        review_url=outbound.review_url,
        access_request_id=outbound.access_request_id,
        review_choices=outbound.review_choices,
        text=outbound.text,
        emitted_at_utc=datetime.now(timezone.utc),
    )


def gateway_delivery_to_wire(
    event: GatewayDeliveryEvent,
    *,
    plugin_id: str,
    adapter_instance_id: str,
    attachments: tuple[AdapterAttachmentV1, ...] = (),
) -> GatewayDeliveryEventV1:
    """Remove local paths and construct a durable external-host delivery."""

    return GatewayDeliveryEventV1(
        protocol_version=ADAPTER_HOST_PROTOCOL_VERSION,
        message_type="gateway.delivery.v1",
        event_id=event.event_id,
        idempotency_key=f"gateway:{event.event_id}",
        plugin_id=plugin_id,
        adapter_instance_id=adapter_instance_id,
        session_epoch="pending",
        stream_position=1,
        channel_name=event.channel_name or "unknown",
        external_user_id=event.user_id,
        chat_id=event.chat_id or "",
        thread_id=event.thread_id,
        in_reply_to_external_message_id=event.original_message_id,
        session_key=event.session_key,
        conversation_id=event.conversation_id,
        correlation_id=event.correlation_id,
        content_message_type=event.message_type,
        review_url=event.review_url,
        access_request_id=event.access_request_id,
        review_choices=event.review_choices,
        text=event.text,
        message_level=(
            DeliveryMessageLevelV1.ATTENTION
            if event.message_level == "attention"
            else DeliveryMessageLevelV1.NORMAL
        ),
        attachments=attachments,
        emitted_at_utc=datetime.now(timezone.utc),
    )


def _attachment_origin(channel_name: str) -> AttachmentOrigin:
    return channel_name if channel_name in _CHAT_APP_ORIGINS else "other"  # type: ignore[return-value]


def _transfer_bytes(upload: AdapterUploadRecord) -> bytes:
    """The bytes of a completed upload, checked again against the digest accepted for it."""

    data = Path(upload.local_path).read_bytes()
    if len(data) != upload.size_bytes or hashlib.sha256(data).hexdigest() != upload.sha256:
        raise ValueError(
            f"Adapter upload {upload.upload_id} changed after it was accepted; it is not saved."
        )
    return data


def _remove_transfer_copy(upload: AdapterUploadRecord) -> None:
    try:
        Path(upload.local_path).unlink(missing_ok=True)
    except OSError as exc:
        LOGGER.warning(
            "A hosted chat's file was saved but its transfer copy could not be removed. "
            "event=adapter_upload_transfer_copy_not_removed upload_id=%s error=%s",
            upload.upload_id,
            type(exc).__name__,
        )


__all__ = ["GatewayAdapterRequestHandler", "gateway_delivery_to_wire"]
