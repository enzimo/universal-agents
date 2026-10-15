"""Transport-neutral contracts for structured agent messages."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from typing import Awaitable, Callable, Optional

from custom_messages.contracts.envelope_v1 import MessageEnvelope


DeliveryHandler = Callable[["InboundDelivery"], Awaitable[None]]


class SubscriptionDelivery(StrEnum):
    """Transport delivery mode for a subscription."""

    CORE = "core"
    JETSTREAM_EPHEMERAL = "jetstream_ephemeral"
    JETSTREAM_DURABLE = "jetstream_durable"


class ConsumerScope(StrEnum):
    """Owner whose lifetime controls a JetStream consumer."""

    PROCESS = "process"
    ROUTE = "route"


class FetchPolicy(StrEnum):
    """When a durable (pull) subscription asks NATS for its next message.

    Durable work is pulled, never pushed (ADR-019): the receiver asks when it
    can act, so NATS never hands a message to a receiver that is busy.
    """

    # One owner's mailbox: always keep a request open and handle each message.
    CONTINUOUS = "continuous"
    # A shared worker queue: ask for one message only while the receiver can
    # start it at once, so unstarted work stays where any free worker takes it.
    WHEN_READY = "when_ready"


@dataclass(frozen=True)
class TransportPublishOptions:
    """Delivery hints used by concrete transport adapters."""

    durable: bool = True
    operational: bool = False
    broadcast: bool = False
    presence: bool = False
    reply: bool = False
    ttl_seconds: Optional[float] = None
    idempotency_key: Optional[str] = None
    headers: dict[str, str] = field(default_factory=dict)


@dataclass
class SubscriptionHandle:
    """Subscription plus the JetStream identity needed for owned cleanup."""

    name: str
    subject: str
    raw_subscription: object
    delivery: SubscriptionDelivery = SubscriptionDelivery.CORE
    consumer_scope: Optional[ConsumerScope] = None
    consumer_stream_name: Optional[str] = None
    consumer_name: Optional[str] = None
    consumer_role: Optional[str] = None
    owner_agent_id: Optional[str] = None

    async def unsubscribe(self) -> None:
        unsubscribe = getattr(self.raw_subscription, "unsubscribe", None)
        if unsubscribe is not None:
            result = unsubscribe()
            if hasattr(result, "__await__"):
                await result


@dataclass
class InboundDelivery:
    """A decoded inbound envelope plus transport-level acknowledgement hooks."""

    envelope: MessageEnvelope
    logical_route: str
    transport_subject: str
    transport_message_id: Optional[str] = None
    redelivery_count: Optional[int] = None
    source_queue_name: Optional[str] = None
    # The consumer's delivery limits: the last attempt is ``max_deliver`` and an
    # unacknowledged message returns to the queue after ``ack_wait_seconds``.
    max_deliver: Optional[int] = None
    ack_wait_seconds: Optional[float] = None
    _ack: Optional[Callable[[], Awaitable[None]]] = None
    _nak: Optional[Callable[[Optional[float]], Awaitable[None]]] = None
    _term: Optional[Callable[[Optional[str]], Awaitable[None]]] = None
    _in_progress: Optional[Callable[[], Awaitable[None]]] = None

    async def ack(self) -> None:
        if self._ack is not None:
            await self._ack()

    async def nak(self, delay_seconds: Optional[float] = None) -> None:
        if self._nak is not None:
            await self._nak(delay_seconds)

    async def term(self, reason: Optional[str] = None) -> None:
        if self._term is not None:
            await self._term(reason)

    async def in_progress(self) -> None:
        if self._in_progress is not None:
            await self._in_progress()
