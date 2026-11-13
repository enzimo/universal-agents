"""Files from a chat app run in the adapter host are saved where every chat's files are.

A hosted chat sends each file into the adapter host's transfer store, then
sends the message naming it. The gateway checks the file with the rules every
upload follows and saves it under ``state/uploads``, like a chat app running in
the gateway, so agents can open it by id later and the upload retention,
history clean-up and person departure cover it. The adapter host removes the
copy the chat app's own store saved once the file is sent on, and the person
is told when the rules refuse a file. These tests drive the real adapter host
service (its SQLite store and transfer folder), the real request handler and
the real ``UserGateway`` answering a waiting question; only the lead is a
recorder (``tests.test_gateway_question_queue``). The adapter host side runs
the real ``RemoteChannelBridge`` and plugin attachment store against a
recording runtime.
"""

from __future__ import annotations

import hashlib
from collections.abc import Sequence
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from assistant_gateway.adapter_host.gateway_application import GatewayAdapterRequestHandler
from assistant_gateway.adapter_host.remote_host import RemoteChannelBridge
from assistant_gateway.adapter_host.service import AdapterHostServiceError
from assistant_gateway.contracts.adapter_host_v1 import (
    ADAPTER_HOST_PROTOCOL_VERSION,
    AdapterAttachmentUploadRequestV1,
    AdapterAttachmentV1,
    ChannelInboundMessageV1,
    GatewayDeliveryEventV1,
)
from assistant_gateway.inbound_attachments import InboundAttachmentStoragePolicy, gateway_inbound_attachment_policy
from assistant_gateway.interactive_apps.host_ports import LocalInteractiveAppAttachmentStore
from assistant_gateway.translation import ChannelInboundMessage
from custom_messages.contracts.attachments_v1 import AttachmentStorageRef, UserTaskAttachment
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

    async def person_sends(self, text: str, files: list[AdapterAttachmentV1]) -> Sequence[GatewayDeliveryEventV1]:
        return await self.handler.handle_inbound(
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
async def test_a_file_the_upload_rules_refuse_is_explained_to_the_person_and_none_of_the_files_is_saved(
    tmp_path: Path,
) -> None:
    hosted = _HostedChat(tmp_path)
    await _two_requests_ask(hosted.chat)
    photo = hosted.upload("receipt.png", PHOTO)
    script = hosted.upload("setup.sh", b"echo hi", content_type="text/plain")

    [reply] = await hosted.person_sends("Two files", [photo, script])

    # Told why in their chat, as the chat apps the gateway runs do.
    assert "blocked executable" in reply.text
    assert (reply.chat_id, reply.session_key) == (TELEGRAM_ID, hosted.chat.session.session_key)
    # Checked before any was saved: nothing in state/uploads, nothing sent,
    # and the transfer copies are gone (the message is never handled again).
    assert hosted.saved_uploads() == []
    assert hosted.chat.lead.answers() == []
    assert hosted.transfer_copies() == []


@pytest.mark.asyncio
async def test_a_file_that_cannot_be_found_fails_the_message_and_removes_the_other_files_transfer_copies(
    tmp_path: Path,
) -> None:
    hosted = _HostedChat(tmp_path)
    await _two_requests_ask(hosted.chat)
    photo = hosted.upload("receipt.png", PHOTO)
    missing = photo.model_copy(update={"attachment_id": "att_hosted_missing", "storage_reference": "adapter-upload:missing"})

    with pytest.raises(AdapterHostServiceError, match="missing"):
        await hosted.person_sends("Two files", [photo, missing])

    assert hosted.saved_uploads() == []
    assert hosted.chat.lead.answers() == []
    assert hosted.transfer_copies() == []


class _RecordingRuntime:
    """The adapter host's connection to the gateway, recording what the bridge sends."""

    def __init__(self, *, upload_fails: bool = False) -> None:
        self.uploads: list[bytes] = []
        self.submitted: list[Any] = []
        self.upload_fails = upload_fails

    async def upload_attachment(self, **upload: Any) -> AdapterAttachmentV1:
        if self.upload_fails:
            raise ConnectionError("gateway unavailable")
        self.uploads.append(upload["payload"])
        return AdapterAttachmentV1(
            attachment_id=upload["attachment_id"], filename=upload["filename"], content_type=upload["content_type"],
            kind=upload["kind"], size_bytes=len(upload["payload"]), sha256=hashlib.sha256(upload["payload"]).hexdigest(),
            storage_reference="adapter-upload:upload-1",
        )

    async def submit(self, message: Any) -> None:
        self.submitted.append(message)


def _plugin_store(files_dir: Path) -> LocalInteractiveAppAttachmentStore:
    """The attachment store a hosted chat app saves incoming files with (``remote_host``)."""

    return LocalInteractiveAppAttachmentStore(
        InboundAttachmentStoragePolicy(cache_dir=files_dir), SimpleNamespace(), register_with_application=False
    )


def _sent(attachments: list[UserTaskAttachment]) -> ChannelInboundMessage:
    return ChannelInboundMessage(
        channel_name="telegram", user_id=TELEGRAM_ID, chat_id=TELEGRAM_ID, text="Here", message_id="m1",
        attachments=attachments,
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("upload_fails", [False, True])
async def test_the_adapter_host_removes_its_own_copy_of_a_persons_file_once_it_is_sent_on(
    tmp_path: Path, upload_fails: bool
) -> None:
    files_dir = tmp_path / "dbs" / "adapter-host-attachments"
    stored = _plugin_store(files_dir).store_bytes(
        data=PHOTO, filename="receipt.png", content_type="image/png", session_key="telegram:1:1",
        conversation_id=None, origin="telegram",
    )
    # A channel's own input file elsewhere (signal-cli keeps its own) is not the store's.
    channel_file = tmp_path / "signal-cli" / "attachments" / "photo.png"
    channel_file.parent.mkdir(parents=True)
    channel_file.write_bytes(PHOTO)
    channel_attachment = stored.attachment.model_copy(
        update={"attachment_id": "att_signal", "storage_refs": [AttachmentStorageRef(storage_type="local", path=str(channel_file))]}
    )
    # Neither is a file the store wrote for that attachment, though one is in its folder.
    outgoing = files_dir / "att_outgoing"
    outgoing.write_bytes(PHOTO)
    misnamed = stored.attachment.model_copy(
        update={"attachment_id": "att_other", "storage_refs": [AttachmentStorageRef(storage_type="local", path=str(outgoing))]}
    )
    runtime = _RecordingRuntime(upload_fails=upload_fails)
    bridge = RemoteChannelBridge(lambda: runtime, plugin_id=PLUGIN_ID, adapter_instance_id=ADAPTER_ID, inbound_files_dir=files_dir)
    assert stored.local_path.is_file()

    if upload_fails:
        with pytest.raises(ConnectionError):
            await bridge.handle_inbound(_sent([stored.attachment, channel_attachment, misnamed]))
    else:
        await bridge.handle_inbound(_sent([stored.attachment, channel_attachment, misnamed]))
        assert runtime.uploads == [PHOTO, PHOTO, PHOTO] and len(runtime.submitted) == 1

    # The gateway keeps the person's file; the adapter host keeps no copy of it.
    assert not stored.local_path.exists()
    assert channel_file.read_bytes() == PHOTO
    assert outgoing.read_bytes() == PHOTO
