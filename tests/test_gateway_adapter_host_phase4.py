from __future__ import annotations

import ipaddress
import asyncio
import hashlib
from datetime import datetime, timedelta, timezone

import pytest

from assistant_gateway.adapter_host.client_state import AdapterHostClientStateStore
from assistant_gateway.adapter_host.service import (
    AdapterHostApplicationService,
    AdapterHostAuthenticatedPeer,
    AdapterHostPolicy,
    AdapterHostServiceError,
    AdapterPluginPolicy,
    canonical_configuration_digest,
)
from assistant_gateway.adapter_host.runtime import (
    AdapterDeliveryResult,
    AdapterHostRuntime,
    HostedAdapterSpec,
)
from assistant_gateway.adapter_host.remote_host import build_python_adapter_host_runtime
from assistant_gateway.adapter_host.transport import (
    AdapterHostClient,
    AdapterHostServer,
    AdapterHostTransportError,
)
from runtime_settings.interactive_apps import AdapterHostSettings
from assistant_gateway.config import GatewayInstanceConfig
from runtime_settings.interactive_apps import AdapterHostTLSSettings
from assistant_gateway.contracts.adapter_host_v1 import (
    ADAPTER_HOST_PROTOCOL_VERSION,
    AdapterCapabilityV1,
    AdapterAttachmentUploadRequestV1,
    AdapterDeliveryAcknowledgementV1,
    AdapterHeartbeatV1,
    AdapterHealthStateV1,
    AdapterProtocolLimitsV1,
    AdapterRegistrationV1,
    AdapterResumePositionV1,
    ChannelInboundMessageV1,
    DeliveryAcknowledgementStageV1,
    DeliveryOutcomeV1,
    GatewayDeliveryEventV1,
)
from id_generation import new_id_suffix
from storage.adapter_host_repository import SqliteAdapterHostRepository


NOW = datetime(2026, 8, 18, 12, 0, tzinfo=timezone.utc)
PLUGIN_ID = "test.echo"
ADAPTER_ID = "instance:test.echo"
HOST_ID = "test-adapter-host"
CONFIGURATION = {"enabled": True, "prefix": "echo"}
SECOND_PLUGIN_ID = "test.second"
SECOND_ADAPTER_ID = "instance:test.second"
SECOND_CONFIGURATION = {"enabled": True, "prefix": "second"}


class _Clock:
    def __init__(self) -> None:
        self.now = NOW

    def __call__(self) -> datetime:
        return self.now


class _Handler:
    def __init__(self) -> None:
        self.inbound_calls = 0

    async def handle_inbound(self, message):
        self.inbound_calls += 1
        return (
            GatewayDeliveryEventV1(
                protocol_version=1,
                message_type="gateway.delivery.v1",
                event_id=f"event-{message.request_id}",
                idempotency_key=f"event-idem-{message.request_id}",
                plugin_id=message.plugin_id,
                adapter_instance_id=message.adapter_instance_id,
                session_epoch=message.session_epoch,
                stream_position=1,
                channel_name=message.channel_name,
                external_user_id=message.external_user_id,
                chat_id=message.chat_id,
                session_key="session-1",
                correlation_id="correlation-1",
                text=f"echo: {message.text}",
                emitted_at_utc=NOW,
            ),
        )

    async def handle_command(self, _message):
        return ()


def _limits(*, pending: int = 10) -> AdapterProtocolLimitsV1:
    return AdapterProtocolLimitsV1(
        max_in_flight_events=2,
        max_payload_bytes=100_000,
        max_attachment_bytes=10_000,
        max_pending_events=pending,
        heartbeat_interval_seconds=5,
        acknowledgement_timeout_seconds=30,
        retention_seconds=60,
    )


def _service(tmp_path, *, pending: int = 10, handler=None):
    repository = SqliteAdapterHostRepository(str(tmp_path / "gateway.db"))
    clock = _Clock()
    policy = AdapterHostPolicy(
        gateway_instance_id="gateway-test",
        plugins={
            PLUGIN_ID: AdapterPluginPolicy(
                plugin_id=PLUGIN_ID,
                adapter_instance_id=ADAPTER_ID,
                allowed_host_ids=frozenset({HOST_ID}),
                configuration_digest=canonical_configuration_digest(CONFIGURATION),
                plugin_api_version="1",
                channel_names=frozenset({"echo"}),
                capabilities=frozenset(
                    {
                        AdapterCapabilityV1.INBOUND_TEXT,
                        AdapterCapabilityV1.OUTBOUND_TEXT,
                        AdapterCapabilityV1.ATTACHMENTS,
                    }
                ),
            )
        },
        limits=_limits(pending=pending),
        lease_seconds=20,
        upload_ticket_ttl_seconds=30,
        upload_dir=tmp_path / "uploads",
    )
    service = AdapterHostApplicationService(
        repository,
        policy,
        request_handler=handler,
        clock=clock,
    )
    return service, repository, clock


def _peer(host_id: str = HOST_ID) -> AdapterHostAuthenticatedPeer:
    return AdapterHostAuthenticatedPeer(
        host_id=host_id,
        authentication_mechanism="test",
        credential_fingerprint="f" * 64,
    )


def _write_mtls_material(tmp_path):
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import rsa
    from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID

    now = datetime.now(timezone.utc)

    def key():
        return rsa.generate_private_key(public_exponent=65537, key_size=2048)

    ca_key = key()
    ca_name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "adapter-test-ca")])
    ca_cert = (
        x509.CertificateBuilder()
        .subject_name(ca_name)
        .issuer_name(ca_name)
        .public_key(ca_key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - timedelta(minutes=1))
        .not_valid_after(now + timedelta(days=1))
        .add_extension(x509.BasicConstraints(ca=True, path_length=None), critical=True)
        .add_extension(
            x509.KeyUsage(
                digital_signature=True,
                content_commitment=False,
                key_encipherment=False,
                data_encipherment=False,
                key_agreement=False,
                key_cert_sign=True,
                crl_sign=True,
                encipher_only=None,
                decipher_only=None,
            ),
            critical=True,
        )
        .add_extension(
            x509.SubjectKeyIdentifier.from_public_key(ca_key.public_key()),
            critical=False,
        )
        .sign(ca_key, hashes.SHA256())
    )

    def issue(common_name, *, server):
        issued_key = key()
        identities = (
            [
                x509.DNSName("adapter-gateway.test"),
                x509.IPAddress(ipaddress.ip_address("127.0.0.1")),
            ]
            if server
            else [x509.DNSName(HOST_ID)]
        )
        usage = (
            ExtendedKeyUsageOID.SERVER_AUTH
            if server
            else ExtendedKeyUsageOID.CLIENT_AUTH
        )
        issued_cert = (
            x509.CertificateBuilder()
            .subject_name(
                x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, common_name)])
            )
            .issuer_name(ca_name)
            .public_key(issued_key.public_key())
            .serial_number(x509.random_serial_number())
            .not_valid_before(now - timedelta(minutes=1))
            .not_valid_after(now + timedelta(days=1))
            .add_extension(x509.SubjectAlternativeName(identities), critical=False)
            .add_extension(x509.ExtendedKeyUsage([usage]), critical=False)
            .add_extension(
                x509.KeyUsage(
                    digital_signature=True,
                    content_commitment=False,
                    key_encipherment=True,
                    data_encipherment=False,
                    key_agreement=False,
                    key_cert_sign=False,
                    crl_sign=False,
                    encipher_only=None,
                    decipher_only=None,
                ),
                critical=True,
            )
            .add_extension(
                x509.AuthorityKeyIdentifier.from_issuer_public_key(
                    ca_key.public_key()
                ),
                critical=False,
            )
            .sign(ca_key, hashes.SHA256())
        )
        return issued_key, issued_cert

    server_key, server_cert = issue("adapter-gateway.test", server=True)
    client_key, client_cert = issue(HOST_ID, server=False)
    paths = {
        "ca": tmp_path / "ca.pem",
        "server_cert": tmp_path / "server-cert.pem",
        "server_key": tmp_path / "server-key.pem",
        "client_cert": tmp_path / "client-cert.pem",
        "client_key": tmp_path / "client-key.pem",
    }
    paths["ca"].write_bytes(ca_cert.public_bytes(serialization.Encoding.PEM))
    paths["server_cert"].write_bytes(
        server_cert.public_bytes(serialization.Encoding.PEM)
    )
    paths["client_cert"].write_bytes(
        client_cert.public_bytes(serialization.Encoding.PEM)
    )
    paths["server_key"].write_bytes(
        server_key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        )
    )
    paths["client_key"].write_bytes(
        client_key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        )
    )
    return paths


def _registration(
    *,
    registration_id: str = "registration-1",
    capabilities: tuple[AdapterCapabilityV1, ...] = (
        AdapterCapabilityV1.INBOUND_TEXT,
        AdapterCapabilityV1.OUTBOUND_TEXT,
        AdapterCapabilityV1.ATTACHMENTS,
    ),
    resume: AdapterResumePositionV1 | None = None,
) -> AdapterRegistrationV1:
    return AdapterRegistrationV1(
        protocol_version=ADAPTER_HOST_PROTOCOL_VERSION,
        message_type="adapter.registration.v1",
        registration_id=registration_id,
        idempotency_key=f"idem-{registration_id}",
        plugin_id=PLUGIN_ID,
        adapter_instance_id=ADAPTER_ID,
        plugin_api_version="1",
        implementation_version="test-1",
        configuration_digest=canonical_configuration_digest(CONFIGURATION),
        channel_names=("echo",),
        capabilities=capabilities,
        supported_limits=_limits(),
        resume=resume or AdapterResumePositionV1(),
        registered_at_utc=NOW,
    )


def _inbound(epoch: str, position: int = 1, *, idem: str = "inbound-idem"):
    return ChannelInboundMessageV1(
        protocol_version=1,
        message_type="channel.inbound.v1",
        request_id=f"request-{position}",
        idempotency_key=idem,
        plugin_id=PLUGIN_ID,
        adapter_instance_id=ADAPTER_ID,
        session_epoch=epoch,
        stream_position=position,
        channel_name="echo",
        external_user_id="external-user",
        chat_id="chat-1",
        text="hello",
        received_at_utc=NOW,
    )


def test_registration_fails_closed_for_host_and_capability_escalation(tmp_path):
    service, _repository, _clock = _service(tmp_path)
    with pytest.raises(AdapterHostServiceError, match="may not register") as wrong_host:
        service.register(_peer("other-host"), _registration())
    assert wrong_host.value.error_code == "authenticated_host_not_allowed"

    escalated = _registration(
        capabilities=(
            AdapterCapabilityV1.INBOUND_TEXT,
            AdapterCapabilityV1.NATIVE_COMMANDS,
        )
    )
    with pytest.raises(AdapterHostServiceError) as denied:
        service.register(_peer(), escalated)
    assert denied.value.error_code == "capability_escalation"

    accepted = service.register(_peer(), _registration(registration_id="valid"))
    assert accepted.session_epoch
    cross_plugin = _inbound(accepted.session_epoch).model_copy(
        update={
            "plugin_id": SECOND_PLUGIN_ID,
            "adapter_instance_id": SECOND_ADAPTER_ID,
        }
    )
    with pytest.raises(AdapterHostServiceError) as cross_plugin_denied:
        service.accept_request(_peer(), cross_plugin)
    assert cross_plugin_denied.value.error_code == "adapter_not_registered"


def test_stale_epoch_and_inbound_sequence_gap_fail_closed(tmp_path):
    service, _repository, _clock = _service(tmp_path)
    first = service.register(_peer(), _registration())
    assert first.session_epoch
    second = service.register(_peer(), _registration(registration_id="registration-2"))
    assert second.session_epoch and second.session_epoch != first.session_epoch

    with pytest.raises(AdapterHostServiceError) as stale:
        service.accept_request(_peer(), _inbound(first.session_epoch))
    assert stale.value.error_code == "stale_registration_epoch"

    with pytest.raises(AdapterHostServiceError) as gap:
        service.accept_request(_peer(), _inbound(second.session_epoch, position=2))
    assert gap.value.error_code == "inbound_sequence_gap"
    assert gap.value.expected_stream_position == 1


@pytest.mark.asyncio
async def test_outbox_backpressure_is_bounded_and_observable(tmp_path):
    service, _repository, _clock = _service(tmp_path, pending=1)
    registration = service.register(_peer(), _registration())
    assert registration.session_epoch

    def event(number: int) -> GatewayDeliveryEventV1:
        return GatewayDeliveryEventV1(
            protocol_version=1,
            message_type="gateway.delivery.v1",
            event_id=f"event-{number}",
            idempotency_key=f"event-idem-{number}",
            plugin_id=PLUGIN_ID,
            adapter_instance_id=ADAPTER_ID,
            session_epoch=registration.session_epoch,
            stream_position=1,
            channel_name="echo",
            chat_id="chat-1",
            session_key="session-1",
            text="hello",
            emitted_at_utc=NOW,
        )

    await service.enqueue_event(event(1))
    with pytest.raises(AdapterHostServiceError) as overloaded:
        await service.enqueue_event(event(2))
    assert overloaded.value.error_code == "outbox_backpressure"
    assert overloaded.value.http_status == 429
    assert overloaded.value.retryable is True


@pytest.mark.asyncio
async def test_terminal_retention_advances_resume_floor(tmp_path):
    service, repository, clock = _service(tmp_path)
    accepted = service.register(_peer(), _registration())
    assert accepted.session_epoch
    event = GatewayDeliveryEventV1(
        protocol_version=1,
        message_type="gateway.delivery.v1",
        event_id="retained-event",
        idempotency_key="retained-event-idem",
        plugin_id=PLUGIN_ID,
        adapter_instance_id=ADAPTER_ID,
        session_epoch=accepted.session_epoch,
        stream_position=1,
        channel_name="echo",
        chat_id="chat-1",
        session_key="session-1",
        text="hello",
        emitted_at_utc=NOW,
    )
    await service.enqueue_event(event)
    receipt = AdapterDeliveryAcknowledgementV1(
        protocol_version=1,
        message_type="adapter.delivery_ack.v1",
        acknowledgement_id="retention-receipt",
        idempotency_key="retention-receipt-idem",
        plugin_id=PLUGIN_ID,
        adapter_instance_id=ADAPTER_ID,
        session_epoch=accepted.session_epoch,
        event_id=event.event_id,
        stream_position=1,
        stage=DeliveryAcknowledgementStageV1.RECEIVED,
        observed_at_utc=NOW,
    )
    service.acknowledge(_peer(), receipt)
    service.acknowledge(
        _peer(),
        receipt.model_copy(
            update={
                "acknowledgement_id": "retention-complete",
                "idempotency_key": "retention-complete-idem",
                "stage": DeliveryAcknowledgementStageV1.COMPLETED,
                "outcome": DeliveryOutcomeV1.DELIVERED,
            }
        ),
    )
    clock.now = NOW + timedelta(seconds=121)

    removed = service._prune_terminal_records()

    assert removed["outbox"] == 1
    assert repository.get_stream(PLUGIN_ID, ADAPTER_ID).retained_from_position == 2
    with pytest.raises(AdapterHostServiceError) as exhausted:
        service.register(
            _peer(),
            _registration(
                registration_id="registration-after-retention",
                resume=AdapterResumePositionV1(
                    outbound_received_position=0,
                    outbound_completed_position=0,
                ),
            ),
        )
    assert exhausted.value.error_code == "resume_retention_exhausted"


@pytest.mark.asyncio
async def test_unix_transport_authenticates_and_delivers_end_to_end(
    tmp_path,
    monkeypatch,
):
    token_env = "TEST_ADAPTER_HOST_AUTH_TOKEN"
    monkeypatch.setenv(token_env, "a" * 48)
    settings = AdapterHostSettings(
        enabled=True,
        host_id=HOST_ID,
        allowed_host_ids=[HOST_ID],
        token_env_var=token_env,
        unix_socket_path=f"/tmp/ua-adapter-{new_id_suffix()}.sock",
        upload_dir="uploads",
        client_state_path="client.db",
        limits=_limits().model_dump(mode="json"),
    )
    handler = _Handler()
    service, _repository, _clock = _service(tmp_path, handler=handler)
    server = AdapterHostServer(service, settings, instance_root=tmp_path)
    client = AdapterHostClient(settings, instance_root=tmp_path)
    await server.start()
    await client.start()
    try:
        registration = await client.register(_registration())
        assert registration.accepted is True
        assert registration.session_epoch
        accepted = await client.submit(_inbound(registration.session_epoch))
        assert accepted.status == "accepted"

        events = ()
        for _ in range(20):
            events = await client.poll_events(
                plugin_id=PLUGIN_ID,
                adapter_instance_id=ADAPTER_ID,
                session_epoch=registration.session_epoch,
                after_position=0,
                limit=2,
                wait_seconds=0.05,
            )
            if events:
                break
        assert [event.text for event in events] == ["echo: hello"]

        inbound_payload = b"opaque inbound attachment bytes"
        upload = await client.create_upload_ticket(
            AdapterAttachmentUploadRequestV1(
                protocol_version=ADAPTER_HOST_PROTOCOL_VERSION,
                message_type="adapter.attachment_upload_request.v1",
                upload_request_id="upload-request-1",
                idempotency_key="upload-idem-1",
                plugin_id=PLUGIN_ID,
                adapter_instance_id=ADAPTER_ID,
                session_epoch=registration.session_epoch,
                filename="inbound.txt",
                content_type="text/plain",
                kind="document",
                size_bytes=len(inbound_payload),
                sha256=hashlib.sha256(inbound_payload).hexdigest(),
                requested_at_utc=NOW,
            )
        )
        await client.upload(upload, inbound_payload)
        resolved = service.resolve_upload_reference(
            plugin_id=PLUGIN_ID,
            adapter_instance_id=ADAPTER_ID,
            session_epoch=registration.session_epoch,
            storage_reference=upload.storage_reference,
        )
        assert resolved.status == "completed"
        assert resolved.size_bytes == len(inbound_payload)
        assert resolved.sha256 == hashlib.sha256(inbound_payload).hexdigest()

        # The gateway passes bytes it already checked against the staged
        # copy's SHA-256 (assistant_gateway.attachments), never a path.
        outbound_payload = b"opaque outbound attachment bytes"
        outbound = service.stage_outbound_attachment(
            plugin_id=PLUGIN_ID,
            adapter_instance_id=ADAPTER_ID,
            payload=outbound_payload,
            filename="outbound.txt",
            content_type="text/plain",
            kind="document",
        )
        downloaded = await client.download_attachment(
            plugin_id=PLUGIN_ID,
            adapter_instance_id=ADAPTER_ID,
            session_epoch=registration.session_epoch,
            storage_reference=outbound.storage_reference,
            expected_size_bytes=outbound.size_bytes,
            expected_sha256=outbound.sha256,
        )
        assert downloaded == outbound_payload

        intruder_settings = settings.model_copy(update={"host_id": "intruder"})
        intruder = AdapterHostClient(intruder_settings, instance_root=tmp_path)
        await intruder.start()
        try:
            with pytest.raises(AdapterHostTransportError) as denied:
                await intruder.register(_registration(registration_id="intruder"))
            assert denied.value.error_code == "unauthenticated"
        finally:
            await intruder.close()

        wrong_token_env = "TEST_ADAPTER_HOST_WRONG_TOKEN"
        monkeypatch.setenv(wrong_token_env, "z" * 48)
        wrong_token_client = AdapterHostClient(
            settings.model_copy(update={"token_env_var": wrong_token_env}),
            instance_root=tmp_path,
        )
        await wrong_token_client.start()
        try:
            with pytest.raises(AdapterHostTransportError) as denied:
                await wrong_token_client.register(
                    _registration(registration_id="wrong-token")
                )
            assert denied.value.error_code == "unauthenticated"
        finally:
            await wrong_token_client.close()
    finally:
        await client.close()
        await server.stop()


def test_attachment_upload_rejects_tampered_bytes(tmp_path):
    payload = b"expected bytes"
    service, _repository, _clock = _service(tmp_path)
    registration = service.register(_peer(), _registration())
    ticket = service.create_upload_ticket(
        _peer(),
        AdapterAttachmentUploadRequestV1(
            protocol_version=ADAPTER_HOST_PROTOCOL_VERSION,
            message_type="adapter.attachment_upload_request.v1",
            upload_request_id="upload-request-tampered",
            idempotency_key="upload-idem-tampered",
            plugin_id=PLUGIN_ID,
            adapter_instance_id=ADAPTER_ID,
            session_epoch=registration.session_epoch,
            filename="payload.bin",
            content_type="application/octet-stream",
            kind="other",
            size_bytes=len(payload),
            sha256=hashlib.sha256(payload).hexdigest(),
            requested_at_utc=NOW,
        ),
    )

    with pytest.raises(AdapterHostServiceError) as exc_info:
        service.accept_upload_bytes(
            _peer(), upload_id=ticket.upload_id, payload=b"tampered bytes"
        )
    assert exc_info.value.error_code == "attachment_digest_mismatch"
    assert not (tmp_path / "uploads" / ticket.upload_id).exists()


@pytest.mark.asyncio
async def test_mtls_transport_binds_certificate_identity_and_connect_address(
    tmp_path,
    monkeypatch,
    unused_tcp_port,
):
    token_env = "TEST_ADAPTER_HOST_MTLS_TOKEN"
    monkeypatch.setenv(token_env, "m" * 48)
    paths = _write_mtls_material(tmp_path)
    settings = AdapterHostSettings(
        enabled=True,
        transport="mtls",
        host_id=HOST_ID,
        allowed_host_ids=[HOST_ID, "intruder"],
        token_env_var=token_env,
        bind_host="127.0.0.1",
        bind_port=unused_tcp_port,
        connect_host="127.0.0.1",
        tls=AdapterHostTLSSettings(
            server_certificate_path=str(paths["server_cert"]),
            server_private_key_path=str(paths["server_key"]),
            client_ca_path=str(paths["ca"]),
            client_certificate_path=str(paths["client_cert"]),
            client_private_key_path=str(paths["client_key"]),
            server_ca_path=str(paths["ca"]),
            server_name="adapter-gateway.test",
        ),
        upload_dir="uploads",
        client_state_path="client.db",
        limits=_limits().model_dump(mode="json"),
    )
    service, _repository, _clock = _service(tmp_path)
    server = AdapterHostServer(service, settings, instance_root=tmp_path)
    client = AdapterHostClient(settings, instance_root=tmp_path)
    await server.start()
    await client.start()
    try:
        accepted = await client.register(_registration())
        assert accepted.accepted is True
        intruder = AdapterHostClient(
            settings.model_copy(update={"host_id": "intruder"}),
            instance_root=tmp_path,
        )
        await intruder.start()
        try:
            with pytest.raises(AdapterHostTransportError) as denied:
                await intruder.register(_registration(registration_id="mtls-intruder"))
            assert denied.value.error_code == "unauthenticated"
        finally:
            await intruder.close()
    finally:
        await client.close()
        await server.stop()


@pytest.mark.asyncio
async def test_one_host_runs_multiple_plugins_with_isolated_streams(
    tmp_path,
    monkeypatch,
):
    token_env = "TEST_MULTI_ADAPTER_HOST_TOKEN"
    monkeypatch.setenv(token_env, "p" * 48)
    settings = AdapterHostSettings(
        enabled=True,
        host_id=HOST_ID,
        allowed_host_ids=[HOST_ID],
        token_env_var=token_env,
        unix_socket_path=f"/tmp/ua-adapter-{new_id_suffix()}.sock",
        upload_dir="uploads",
        client_state_path="client.db",
        reconnect_initial_delay_seconds=0.05,
        reconnect_max_delay_seconds=0.1,
        limits=_limits().model_dump(mode="json"),
    )
    repository = SqliteAdapterHostRepository(str(tmp_path / "gateway.db"))
    plugin_capabilities = frozenset(
        {
            AdapterCapabilityV1.INBOUND_TEXT,
            AdapterCapabilityV1.OUTBOUND_TEXT,
        }
    )
    policy = AdapterHostPolicy(
        gateway_instance_id="gateway-test",
        plugins={
            PLUGIN_ID: AdapterPluginPolicy(
                plugin_id=PLUGIN_ID,
                adapter_instance_id=ADAPTER_ID,
                allowed_host_ids=frozenset({HOST_ID}),
                configuration_digest=canonical_configuration_digest(CONFIGURATION),
                plugin_api_version="1",
                channel_names=frozenset({"echo"}),
                capabilities=plugin_capabilities,
            ),
            SECOND_PLUGIN_ID: AdapterPluginPolicy(
                plugin_id=SECOND_PLUGIN_ID,
                adapter_instance_id=SECOND_ADAPTER_ID,
                allowed_host_ids=frozenset({HOST_ID}),
                configuration_digest=canonical_configuration_digest(
                    SECOND_CONFIGURATION
                ),
                plugin_api_version="1",
                channel_names=frozenset({"second"}),
                capabilities=plugin_capabilities,
            ),
        },
        limits=_limits(),
        lease_seconds=20,
        upload_ticket_ttl_seconds=30,
        upload_dir=tmp_path / "uploads",
    )
    service = AdapterHostApplicationService(repository, policy)
    server = AdapterHostServer(service, settings, instance_root=tmp_path)
    client = AdapterHostClient(settings, instance_root=tmp_path)
    state = AdapterHostClientStateStore(tmp_path / "client.db")
    delivered: list[tuple[str, str]] = []
    lifecycle: list[str] = []

    def spec(plugin_id, adapter_id, channel, configuration):
        async def deliver(event):
            delivered.append((plugin_id, event.text))
            return AdapterDeliveryResult(outcome=DeliveryOutcomeV1.DELIVERED)

        async def start():
            lifecycle.append(f"start:{plugin_id}")

        async def stop():
            lifecycle.append(f"stop:{plugin_id}")

        return HostedAdapterSpec(
            plugin_id=plugin_id,
            adapter_instance_id=adapter_id,
            plugin_api_version="1",
            implementation_version="test-1",
            channel_names=(channel,),
            capabilities=tuple(
                sorted(plugin_capabilities, key=lambda item: item.value)
            ),
            configuration=configuration,
            deliver_event=deliver,
            start=start,
            stop=stop,
        )

    runtime = AdapterHostRuntime(
        client,
        state,
        settings,
        (
            spec(PLUGIN_ID, ADAPTER_ID, "echo", CONFIGURATION),
            spec(
                SECOND_PLUGIN_ID,
                SECOND_ADAPTER_ID,
                "second",
                SECOND_CONFIGURATION,
            ),
        ),
    )
    await server.start()
    await runtime.start()
    try:
        for _ in range(100):
            if (
                repository.get_registration(PLUGIN_ID, ADAPTER_ID) is not None
                and repository.get_registration(SECOND_PLUGIN_ID, SECOND_ADAPTER_ID)
                is not None
            ):
                break
            await asyncio.sleep(0.02)
        assert lifecycle[:2] == [f"start:{PLUGIN_ID}", f"start:{SECOND_PLUGIN_ID}"]

        async def enqueue(plugin_id, adapter_id, channel, text):
            await service.enqueue_event(
                GatewayDeliveryEventV1(
                    protocol_version=1,
                    message_type="gateway.delivery.v1",
                    event_id=f"event-{plugin_id}",
                    idempotency_key=f"event-idem-{plugin_id}",
                    plugin_id=plugin_id,
                    adapter_instance_id=adapter_id,
                    session_epoch="pending",
                    stream_position=1,
                    channel_name=channel,
                    chat_id="chat-1",
                    session_key="session-1",
                    text=text,
                    emitted_at_utc=NOW,
                )
            )

        await enqueue(PLUGIN_ID, ADAPTER_ID, "echo", "first")
        await enqueue(SECOND_PLUGIN_ID, SECOND_ADAPTER_ID, "second", "second")
        # The callback records delivery before its acknowledgement reaches the
        # gateway. Wait for persisted completion, which the assertions require.
        for _ in range(100):
            first_health = repository.health_snapshot(PLUGIN_ID, ADAPTER_ID)
            second_health = repository.health_snapshot(SECOND_PLUGIN_ID, SECOND_ADAPTER_ID)
            if (
                len(delivered) == 2
                and first_health
                and first_health.last_outbound_completed_position == 1
                and second_health
                and second_health.last_outbound_completed_position == 1
            ):
                break
            await asyncio.sleep(0.02)

        assert set(delivered) == {
            (PLUGIN_ID, "first"),
            (SECOND_PLUGIN_ID, "second"),
        }
        first_health = repository.health_snapshot(PLUGIN_ID, ADAPTER_ID)
        second_health = repository.health_snapshot(SECOND_PLUGIN_ID, SECOND_ADAPTER_ID)
        assert first_health and first_health.last_outbound_completed_position == 1
        assert second_health and second_health.last_outbound_completed_position == 1

        service.heartbeat(
            _peer(),
            AdapterHeartbeatV1(
                protocol_version=ADAPTER_HOST_PROTOCOL_VERSION,
                message_type="adapter.heartbeat.v1",
                heartbeat_id="heartbeat-second-ready",
                idempotency_key="heartbeat-second-ready-idem",
                plugin_id=SECOND_PLUGIN_ID,
                adapter_instance_id=SECOND_ADAPTER_ID,
                session_epoch=second_health.registration_epoch,
                state=AdapterHealthStateV1.READY,
                inbound_committed_position=second_health.last_inbound_position,
                outbound_received_position=second_health.last_outbound_received_position,
                outbound_completed_position=second_health.last_outbound_completed_position,
                in_flight_events=0,
                local_queue_depth=0,
                observed_at_utc=datetime.now(timezone.utc),
            ),
        )
        service.heartbeat(
            _peer(),
            AdapterHeartbeatV1(
                protocol_version=ADAPTER_HOST_PROTOCOL_VERSION,
                message_type="adapter.heartbeat.v1",
                heartbeat_id="heartbeat-first-degraded",
                idempotency_key="heartbeat-first-degraded-idem",
                plugin_id=PLUGIN_ID,
                adapter_instance_id=ADAPTER_ID,
                session_epoch=first_health.registration_epoch,
                state=AdapterHealthStateV1.DEGRADED,
                inbound_committed_position=first_health.last_inbound_position,
                outbound_received_position=first_health.last_outbound_received_position,
                outbound_completed_position=first_health.last_outbound_completed_position,
                in_flight_events=0,
                local_queue_depth=0,
                observed_at_utc=datetime.now(timezone.utc),
                error_code="test_dependency_unavailable",
                detail="redacted dependency diagnostic",
            ),
        )
        assert service.health(PLUGIN_ID, ADAPTER_ID)["degraded"] is True
        assert service.health(SECOND_PLUGIN_ID, SECOND_ADAPTER_ID)["degraded"] is False
        assert (
            service.health(SECOND_PLUGIN_ID, SECOND_ADAPTER_ID)["readiness"] == "ready"
        )
    finally:
        await runtime.stop()
        await server.stop()
    assert lifecycle[-2:] == [
        f"stop:{SECOND_PLUGIN_ID}",
        f"stop:{PLUGIN_ID}",
    ]


@pytest.mark.asyncio
async def test_host_reconnects_after_gateway_restart_and_flushes_durable_input(
    tmp_path,
    monkeypatch,
):
    token_env = "TEST_RECONNECT_ADAPTER_HOST_TOKEN"
    monkeypatch.setenv(token_env, "r" * 48)
    limits = _limits().model_copy(update={"heartbeat_interval_seconds": 1.0})
    settings = AdapterHostSettings(
        enabled=True,
        host_id=HOST_ID,
        allowed_host_ids=[HOST_ID],
        token_env_var=token_env,
        unix_socket_path=f"/tmp/ua-adapter-{new_id_suffix()}.sock",
        upload_dir="uploads",
        client_state_path="client.db",
        reconnect_initial_delay_seconds=0.05,
        reconnect_max_delay_seconds=0.1,
        limits=limits.model_dump(mode="json"),
    )
    first_service, first_repository, _clock = _service(tmp_path)
    first_server = AdapterHostServer(first_service, settings, instance_root=tmp_path)
    state = AdapterHostClientStateStore(tmp_path / "client.db")
    lifecycle: list[str] = []
    delivered: list[str] = []

    async def deliver(event):
        delivered.append(event.text)
        return AdapterDeliveryResult(outcome=DeliveryOutcomeV1.DELIVERED)

    async def start():
        lifecycle.append("start")

    async def stop():
        lifecycle.append("stop")

    runtime = AdapterHostRuntime(
        AdapterHostClient(settings, instance_root=tmp_path),
        state,
        settings,
        (
            HostedAdapterSpec(
                plugin_id=PLUGIN_ID,
                adapter_instance_id=ADAPTER_ID,
                plugin_api_version="1",
                implementation_version="test-1",
                channel_names=("echo",),
                capabilities=(
                    AdapterCapabilityV1.INBOUND_TEXT,
                    AdapterCapabilityV1.OUTBOUND_TEXT,
                    AdapterCapabilityV1.ATTACHMENTS,
                ),
                configuration=CONFIGURATION,
                deliver_event=deliver,
                start=start,
                stop=stop,
            ),
        ),
    )
    await first_server.start()
    await runtime.start()
    second_server = None
    try:
        for _ in range(100):
            if first_repository.get_registration(PLUGIN_ID, ADAPTER_ID):
                break
            await asyncio.sleep(0.02)
        assert runtime.registration_epoch(PLUGIN_ID)
        await first_server.stop()
        for _ in range(100):
            if runtime.registration_epoch(PLUGIN_ID) is None:
                break
            await asyncio.sleep(0.02)
        assert runtime.registration_epoch(PLUGIN_ID) is None

        queued = await runtime.submit(_inbound("pending"))
        assert queued is None
        handler = _Handler()
        second_service, second_repository, _clock = _service(tmp_path, handler=handler)
        second_server = AdapterHostServer(
            second_service, settings, instance_root=tmp_path
        )
        await second_server.start()
        for _ in range(200):
            registration = second_repository.get_registration(PLUGIN_ID, ADAPTER_ID)
            if handler.inbound_calls == 1 and delivered and registration:
                break
            await asyncio.sleep(0.02)

        registration = second_repository.get_registration(PLUGIN_ID, ADAPTER_ID)
        assert registration and registration.reconnect_count >= 1
        assert handler.inbound_calls == 1
        assert delivered == ["echo: hello"]
        assert lifecycle == ["start"]
    finally:
        await runtime.stop()
        if second_server is not None:
            await second_server.stop()
        elif first_server is not None:
            await first_server.stop()
    assert lifecycle == ["start", "stop"]


def test_external_builtin_host_composes_multiple_plugins_without_aggregate_ports(
    tmp_path,
    monkeypatch,
):
    monkeypatch.setenv("ADAPTER_HOST_AUTH_TOKEN", "h" * 48)
    config = GatewayInstanceConfig.create(
        instance_name="phase4-host",
        instance_root=str(tmp_path / "instance"),
        team_id="team_phase4",
        timezone="UTC",
    )
    raw = config.model_dump(mode="python")
    raw["interactive_apps"]["adapter_host"]["enabled"] = True
    raw["interactive_apps"]["plugins"]["builtin.internal_website"]["settings"][
        "enabled"
    ] = False
    for plugin_id in ("builtin.discord", "builtin.telegram"):
        plugin = raw["interactive_apps"]["plugins"][plugin_id]
        plugin["execution"] = "adapter_host"
        plugin["settings"]["enabled"] = True
    validated = GatewayInstanceConfig.model_validate(raw)

    built = build_python_adapter_host_runtime(validated)

    assert built.plugin_ids == ("builtin.discord", "builtin.telegram")
