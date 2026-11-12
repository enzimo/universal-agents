"""Files from a chat app run in the adapter host are saved where every chat's files are.

A hosted chat sends each file into the adapter host's transfer store, then
sends the message naming it. The gateway checks the file with the rules every
upload follows and saves it under ``state/uploads``, like a chat app running in
the gateway, so agents can open it by id later and the upload retention,
history clean-up and person departure cover it. These tests drive the real
adapter host service (its SQLite store and transfer folder), the real request
handler and the real ``UserGateway`` answering a waiting question; only the
lead is a recorder (``tests.test_gateway_question_queue``).
"""

from __future__ import annotations

import hashlib
from pathlib import Path

import pytest

from assistant_gateway.adapter_host.gateway_application import GatewayAdapterRequestHandler
from assistant_gateway.contracts.adapter_host_v1 import (
    ADAPTER_HOST_PROTOCOL_VERSION,
    AdapterAttachmentUploadRequestV1,
    AdapterAttachmentV1,
    ChannelInboundMessageV1,
)
from assistant_gateway.inbound_attachments import AttachmentValidationError, gateway_inbound_attachment_policy
from assistant_gateway.interactive_apps.host_ports import LocalInteractiveAppAttachmentStore
from custom_agents.conversation_attachments import UPLOAD_AVAILABLE, upload_state
from tests.test_gateway_adapter_host_phase4 import ADAPTER_ID, NOW, PLUGIN_ID, _peer, _registration, _service
from tests.test_gateway_question_queue import TELEGRAM_ID, _Chat, _two_requests_ask

PHOTO = b"\x89PNG\r\n\x1a\n" + b"receipt"


class _HostedChat:
    """A Telegram chat run in the adapter host, talking to the real gateway."""

    def __init__(self, tmp_path: Path) -> None:
        self.chat = _Chat(tmp_path)
        self.service, _repository, _clock = _service(tmp_path)
        self.transfer_dir = tmp_path / "uploads"
        self.epoch = self.service.register(_peer(), _registration()).session_epoch
        application = self.chat.gateway.application
        # The message comes from the person's own chat session; linking a
        # Telegram account to a person is covered elsewhere (test_gateway_phase1).
        application.resolve_session = lambda **_route: self.chat.session  # type: ignore[method-assign]
        self.handler = GatewayAdapterRequestHandler(
            application,
            lambda: self.service,
            attachments=LocalInteractiveAppAttachmentStore(gateway_inbound_attachment_policy(self.chat.config), application),
        )

    def upload(self, filename: str, data: bytes, content_type: str = "image/png") -> AdapterAttachmentV1:
        """The hosted chat sends a file into the transfer store, as ``AdapterHostRuntime.upload_attachment`` does."""

        digest = hashlib.sha256(data).hexdigest()
        ticket = self.service.create_upload_ticket(
            _peer(),
            AdapterAttachmentUploadRequestV1(
                protocol_version=ADAPTER_HOST_PROTOCOL_VERSION,
                message_type="adapter.attachment_upload_request.v1",
                upload_request_id=f"upload-{filename}",
                idempotency_key=f"upload-idem-{filename}",
                plugin_id=PLUGIN_ID,
                adapter_instance_id=ADAPTER_ID,
                session_epoch=self.epoch,
                filename=filename,
                content_type=content_type,
                kind="image",
                size_bytes=len(data),
                sha256=digest,
                requested_at_utc=NOW,
            ),
        )
        self.service.accept_upload_bytes(_peer(), upload_id=ticket.upload_id, payload=data)
        return AdapterAttachmentV1(
            attachment_id=f"att_hosted_{ticket.upload_id}",
            filename=filename,
            content_type=content_type,
            kind="image",
            size_bytes=len(data),
            sha256=digest,
            storage_reference=ticket.storage_reference,
        )

    async def person_sends(self, text: str, files: list[AdapterAttachmentV1]) -> None:
        await self.handler.handle_inbound(
            ChannelInboundMessageV1(
                protocol_version=ADAPTER_HOST_PROTOCOL_VERSION,
                message_type="channel.inbound.v1",
                request_id="request-1",
                idempotency_key="inbound-idem-1",
                plugin_id=PLUGIN_ID,
                adapter_instance_id=ADAPTER_ID,
                session_epoch=self.epoch,
                stream_position=1,
                channel_name="telegram",
                external_user_id=TELEGRAM_ID,
                chat_id=TELEGRAM_ID,
                text=text,
                attachments=tuple(files),
                received_at_utc=NOW,
            )
        )

    def transfer_copies(self) -> list[Path]:
        return sorted(self.transfer_dir.iterdir())

    def saved_uploads(self) -> list[Path]:
        return sorted(path for path in self.chat.config.layout.uploads_dir.rglob("*") if path.is_file())


@pytest.mark.asyncio
async def test_a_hosted_chats_file_is_saved_in_the_gateways_uploads_and_sent_with_the_answer(tmp_path: Path) -> None:
    hosted = _HostedChat(tmp_path)
    first, _second = await _two_requests_ask(hosted.chat)
    hosted.chat.finish_each_answered_task()
    sent = hosted.upload("receipt.png", PHOTO)
    assert len(hosted.transfer_copies()) == 1

    await hosted.person_sends("Here is the receipt", [sent])

    [answer] = hosted.chat.lead.answers()
    assert (answer.task_query_id, answer.message) == (first.task_query_id, "Here is the receipt")
    [file] = answer.attachments
    uploads_dir = hosted.chat.config.layout.uploads_dir
    saved = Path(file.local_path() or "")
    # Saved in the chat session's folder of state/uploads, under an id the gateway gave it.
    assert saved.parent.parent == uploads_dir.resolve()
    assert saved.read_bytes() == PHOTO
    assert (file.filename, file.content_type, file.kind, file.origin) == ("receipt.png", "image/png", "image", "telegram")
    assert file.attachment_id.startswith("att_") and file.attachment_id != sent.attachment_id
    assert upload_state(file, uploads_dir) == UPLOAD_AVAILABLE
    # The gateway knows it as its own upload (retention, clean-up and departure use this record).
    record = hosted.chat.store.get_attachment(file.attachment_id)
    assert record is not None and Path(record.local_path) == saved
    # The transfer copy is gone: the file is kept once, for the retention period.
    assert hosted.transfer_copies() == []


@pytest.mark.asyncio
async def test_a_file_the_upload_rules_refuse_saves_none_of_the_messages_files(tmp_path: Path) -> None:
    hosted = _HostedChat(tmp_path)
    await _two_requests_ask(hosted.chat)
    photo = hosted.upload("receipt.png", PHOTO)
    script = hosted.upload("setup.sh", b"echo hi", content_type="text/plain")

    with pytest.raises(AttachmentValidationError, match="blocked executable"):
        await hosted.person_sends("Two files", [photo, script])

    # Checked before any was saved: nothing in state/uploads, nothing sent,
    # and the transfer copies are gone (the message is never handled again).
    assert hosted.saved_uploads() == []
    assert hosted.chat.lead.answers() == []
    assert hosted.transfer_copies() == []
