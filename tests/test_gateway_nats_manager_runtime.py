from __future__ import annotations

from types import SimpleNamespace

import pytest

from assistant_gateway.config import GatewayInstanceConfig
from assistant_gateway.nats_manager_runtime import NatsGatewayManagerRuntime
from custom_messages.contracts.envelope_v1 import MessageEnvelope
from custom_messages.contracts.routing_v1 import MessageHeader, MessageMeta
from custom_messages.contracts.task_v1 import Task
from custom_messages.enums import MessageMetaType
from custom_transport.base import InboundDelivery


class _CaptureNatsAdapter:
    def __init__(self) -> None:
        self.connect_count = 0
        self.close_count = 0
        self.route_subscription = None
        self.subject_subscriptions: list[dict] = []
        self.publish_calls: list[dict] = []
        self.consumer_calls: list[dict] = []
        self.put_calls: list[dict] = []

    async def connect(self, *, reconcile_topology=False) -> None:
        assert reconcile_topology is True
        self.connect_count += 1

    async def close(self) -> None:
        self.close_count += 1

    async def subscribe_route(self, **kwargs):
        self.route_subscription = dict(kwargs)

    async def subscribe_subject(self, **kwargs):
        self.subject_subscriptions.append(dict(kwargs))

    async def publish(self, **kwargs) -> None:
        self.publish_calls.append(dict(kwargs))

    async def consumer_is_listening(self, **kwargs) -> bool:
        self.consumer_calls.append(dict(kwargs))
        return True

    async def put_object(self, **kwargs):
        self.put_calls.append(dict(kwargs))
        return SimpleNamespace(name=kwargs["object_name"])


def _config(tmp_path) -> GatewayInstanceConfig:
    return GatewayInstanceConfig.create(
        instance_name="runtime_test",
        instance_root=str(tmp_path / "instance"),
        team_id="team_test",
        timezone="America/Chicago",
        manager_agent_id="manager_1",
    )


def _envelope() -> MessageEnvelope:
    return MessageEnvelope(
        msg_content=Task(
            task_type=MessageMetaType.REQUEST,
            problem_statement="hello",
        ),
        msg_header=MessageHeader(
            from_agent_id="gateway",
            to_routing_path="manager_1",
            idempotency_key="idem_1",
        ),
        msg_meta=MessageMeta(msg_correlation_id="corr_1"),
    )


@pytest.mark.asyncio
async def test_nats_manager_runtime_owns_subscriptions_ack_and_publish_modes(
    tmp_path,
) -> None:
    adapter = _CaptureNatsAdapter()
    runtime = NatsGatewayManagerRuntime.from_gateway_config(
        _config(tmp_path),
        adapter=adapter,  # type: ignore[arg-type]
    )
    responses: list[MessageEnvelope] = []
    operational: list[MessageEnvelope] = []
    presence: list[MessageEnvelope] = []

    await runtime.connect(
        on_response=lambda envelope: _append_async(responses, envelope),
        on_operational=lambda envelope: _append_async(operational, envelope),
        on_presence=lambda envelope: _append_async(presence, envelope),
    )

    assert runtime.connected is True
    assert adapter.connect_count == 1
    assert adapter.route_subscription is not None
    # Operational and presence only: the gateway no longer follows the old
    # authorization snapshot stream.
    assert len(adapter.subject_subscriptions) == 2
    assert not any(item["name"].endswith(":authorization") for item in adapter.subject_subscriptions)

    acked: list[str] = []

    async def ack() -> None:
        acked.append("ack")

    envelope = _envelope()
    await adapter.route_subscription["handler"](
        InboundDelivery(
            envelope=envelope,
            logical_route="gateway",
            transport_subject="team_test.gateway.response",
            _ack=ack,
        )
    )
    assert responses == [envelope]
    assert acked == ["ack"]

    await runtime.publish_durable(envelope, routing_key="manager_1")
    await runtime.publish_operational(envelope, routing_key="broadcast")
    assert adapter.publish_calls[0]["options"].durable is True
    assert adapter.publish_calls[0]["options"].operational is False
    assert adapter.publish_calls[1]["options"].durable is False
    assert adapter.publish_calls[1]["options"].operational is True
    assert len(adapter.publish_calls) == 2

    assert await runtime.manager_has_live_consumer("manager_1") is True
    assert adapter.consumer_calls == [
        {
            "stream_name": "TEAM_TEST_WORK",
            "durable_name": "team_test_agent_manager_1",
        }
    ]
    await runtime.close()
    assert runtime.connected is False
    assert adapter.close_count == 1


@pytest.mark.asyncio
async def test_nats_manager_runtime_closes_partial_connection_on_subscribe_failure(
    tmp_path,
) -> None:
    adapter = _CaptureNatsAdapter()

    async def fail_subscribe(**_kwargs):
        raise RuntimeError("subscription failed")

    adapter.subscribe_route = fail_subscribe  # type: ignore[method-assign]
    runtime = NatsGatewayManagerRuntime.from_gateway_config(
        _config(tmp_path),
        adapter=adapter,  # type: ignore[arg-type]
    )

    with pytest.raises(RuntimeError, match="subscription failed"):
        await runtime.connect(
            on_response=_discard_async,
            on_operational=_discard_async,
            on_presence=_discard_async,
        )

    assert runtime.connected is False
    assert adapter.close_count == 1


@pytest.mark.asyncio
async def test_stream_setup_before_the_gateway_starts_sets_up_and_disconnects(tmp_path) -> None:
    adapter = _CaptureNatsAdapter()
    adapter.stream_names = lambda: ("TEAM_TEST_WORK",)  # type: ignore[attr-defined]
    runtime = NatsGatewayManagerRuntime.from_gateway_config(
        _config(tmp_path),
        adapter=adapter,  # type: ignore[arg-type]
    )

    assert await runtime.prepare_topology() == ("TEAM_TEST_WORK",)
    assert (adapter.connect_count, adapter.close_count) == (1, 1)
    assert adapter.route_subscription is None and not adapter.subject_subscriptions
    assert runtime.connected is False

    await runtime.connect(on_response=_discard_async, on_operational=_discard_async, on_presence=_discard_async)
    with pytest.raises(RuntimeError, match="before it connects"):
        await runtime.prepare_topology()


@pytest.mark.asyncio
async def test_nats_manager_runtime_does_not_ack_failed_application_handling(
    tmp_path,
) -> None:
    adapter = _CaptureNatsAdapter()
    runtime = NatsGatewayManagerRuntime.from_gateway_config(
        _config(tmp_path),
        adapter=adapter,  # type: ignore[arg-type]
    )

    async def fail_handling(_envelope: MessageEnvelope) -> None:
        raise RuntimeError("application handling failed")

    await runtime.connect(
        on_response=fail_handling,
        on_operational=_discard_async,
        on_presence=_discard_async,
    )
    acked: list[str] = []

    async def ack() -> None:
        acked.append("ack")

    with pytest.raises(RuntimeError, match="application handling failed"):
        await adapter.route_subscription["handler"](
            InboundDelivery(
                envelope=_envelope(),
                logical_route="gateway",
                transport_subject="team_test.gateway.response",
                _ack=ack,
            )
        )

    assert acked == []
    await runtime.close()


async def _append_async(items: list, item) -> None:
    items.append(item)


async def _discard_async(_item) -> None:
    return None
