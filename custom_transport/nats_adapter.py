"""NATS/JetStream transport adapter for structured agent messages."""

from __future__ import annotations

import asyncio
import contextlib
import errno
import json
import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from hashlib import sha256
from typing import Any, Callable, Optional

from pydantic import ValidationError

from custom_messages.contracts.envelope_v1 import MessageEnvelope
from custom_messages.transport_codec import decode_envelope, encode_envelope
from custom_transport.base import (
    ConsumerScope,
    DeliveryHandler,
    FetchPolicy,
    InboundDelivery,
    SubscriptionDelivery,
    SubscriptionHandle,
    TransportPublishOptions,
)
from custom_transport.dead_letters import (
    HEADER_COPY_ID,
    HEADER_DELIVERY_COUNT,
    HEADER_ERROR,
    HEADER_MESSAGE_CLASS,
    HEADER_MESSAGE_ID,
    HEADER_ORIGINAL_SEQUENCE,
    HEADER_ORIGINAL_STREAM,
    HEADER_ORIGINAL_SUBJECT,
    HEADER_REASON,
    HEADER_RAW_SUBJECT,
    HEADER_RECEIVER,
    HEADER_RELEASE,
    HEADER_SET_ASIDE_AT,
    REASON_UNDECODABLE,
    DeadLetterRecord,
    StoppedReceiver,
)
from custom_transport.nats_pull import (
    CONSUMER_METADATA_SCHEMA,
    POOL_ACK_WAIT_SECONDS,
    POOL_MAX_DELIVER,
    PullRunner,
    delivery_count,
    ensure_pull_consumer,
    return_or_end,
)
from custom_transport.nats_topology import NatsStreamTopology
from custom_transport.subjects import NatsSubjectMapper
from release_version import release_version


_CONSUMER_DELETE_TIMEOUT_SECONDS = 3.0
# Consumer roles. A worker queue is shared by every worker of one type.
NAMED_WORKER_CONSUMER_ROLE = "named_worker"
# What happened to an unreadable Core NATS message: nothing can give it back.
_DROPPED = "dropped"
# Longest text taken from an unreadable message into a log line.
_LOGGED_TEXT_LIMIT = 120
_LOGGED_ERROR_LIMIT = 5


def _undecodable_identity(data: Any) -> tuple[Optional[str], Optional[str]]:
    """The message id and type named in an unreadable message, when it has them.

    Only these two identifiers are logged, shortened; message content is not.
    """

    try:
        document = json.loads(data)
    except Exception:
        return None, None
    if not isinstance(document, dict):
        return None, None
    header = document.get("msg_header")
    content = document.get("msg_content")
    message_id = header.get("message_id") if isinstance(header, dict) else None
    message_class = content.get("message_class") if isinstance(content, dict) else None
    return (
        str(message_id)[:_LOGGED_TEXT_LIMIT] if message_id is not None else None,
        str(message_class)[:_LOGGED_TEXT_LIMIT] if message_class is not None else None,
    )


def _decode_error_summary(exc: ValueError) -> str:
    """Where and why decoding failed, without the rejected values."""

    if isinstance(exc, ValidationError):
        parts = [
            f"{'.'.join(str(part) for part in error['loc']) or '<root>'}: {error['type']}"
            for error in exc.errors(include_input=False, include_url=False)[:_LOGGED_ERROR_LIMIT]
        ]
        return "; ".join(parts) or type(exc).__name__
    if isinstance(exc, json.JSONDecodeError):
        # The message names the position only, never the document.
        return f"JSONDecodeError: {exc.msg} at position {exc.pos}"
    if isinstance(exc, UnicodeDecodeError):
        return f"UnicodeDecodeError at position {exc.start}"
    return f"{type(exc).__name__}: {str(exc)[:_LOGGED_TEXT_LIMIT]}"


@dataclass(frozen=True)
class _DeadLetterCopy:
    """What ``_set_aside`` publishes for one message, and the identifiers it logs."""

    original_subject: str
    stream_sequence: str
    message_id: Optional[str]
    message_class: Optional[str]
    copy_id: str
    original_headers: dict[str, str]
    headers: dict[str, str]


def _dead_letter_copy(
    msg: Any, *, reason: str, error: str, receiver: str
) -> _DeadLetterCopy:
    """Separate accepted original headers from the copy's small diagnostics."""

    original_subject = str(getattr(msg, "subject", "") or "")
    stream = sequence = published_at = ""
    with contextlib.suppress(Exception):
        stream = str(getattr(msg.metadata, "stream", "") or "")
        sequence = str(
            getattr(getattr(msg.metadata, "sequence", None), "stream", "") or ""
        )
        published_at = str(getattr(msg.metadata, "timestamp", "") or "")
    message_id, message_class = _undecodable_identity(getattr(msg, "data", b""))
    original_headers = {
        str(key): str(value)
        for key, value in dict(getattr(msg, "headers", None) or {}).items()
        if not str(key).lower().startswith("nats-")
    }
    # Source timestamps survive redelivery and distinguish stream incarnations
    # after a memory-backed broker restarts. Content also distinguishes copies
    # lacking a decoded message id. Receiver/current time are deliberately out.
    identity = json.dumps(
        [original_subject, stream, sequence, published_at, original_headers],
        sort_keys=True,
    )
    digest = sha256(identity.encode())
    digest.update(bytes(getattr(msg, "data", b"") or b""))
    copy_id = digest.hexdigest()
    headers = {
        HEADER_COPY_ID: copy_id,
        HEADER_REASON: reason,
        HEADER_ERROR: str(error)[: _LOGGED_TEXT_LIMIT * 4],
        HEADER_ORIGINAL_SUBJECT: original_subject,
        HEADER_ORIGINAL_STREAM: stream,
        HEADER_ORIGINAL_SEQUENCE: sequence,
        HEADER_DELIVERY_COUNT: str(delivery_count(msg) or ""),
        HEADER_RECEIVER: receiver,
        HEADER_RELEASE: release_version(),
        HEADER_SET_ASIDE_AT: datetime.now(timezone.utc).isoformat(),
        HEADER_MESSAGE_CLASS: message_class or "",
    }
    message_id = (
        message_id
        or original_headers.get(HEADER_MESSAGE_ID, "")[:_LOGGED_TEXT_LIMIT]
        or None
    )
    if message_id:
        headers[HEADER_MESSAGE_ID] = message_id
    headers["Nats-Msg-Id"] = f"dead-letter:{copy_id}"
    return _DeadLetterCopy(
        original_subject,
        sequence,
        message_id,
        message_class,
        copy_id,
        original_headers,
        headers,
    )


def _nats_payload_size(payload: bytes, headers: Optional[dict[str, str]]) -> int:
    """Conservative wire size, including the UTF-8 NATS header block.

    nats-py's own limit check counts only the body. Header normalization can
    shorten these values, so counting them before normalization is safe.
    ``None`` sends no header block; an empty dict would still add twelve bytes.
    """

    if headers is None:
        return len(payload)
    return (
        len(payload)
        + len(b"NATS/1.0\r\n\r\n")
        + sum(
            len(key.encode()) + len(value.encode()) + 4
            for key, value in headers.items()
        )
    )


@dataclass(frozen=True)
class ConsumerQueueState:
    """What a durable consumer reports about its queue (JetStream consumer info).

    ``waiting``: messages not yet handed to any receiver. ``asking``: open pull
    requests, which for a worker queue is the number of idle workers asking for
    work. ``unacknowledged``: messages handed out but not yet acknowledged.
    """

    waiting: int
    asking: int
    unacknowledged: int


@dataclass(frozen=True)
class NatsTransportConfig:
    servers: list[str] = field(default_factory=lambda: ["nats://127.0.0.1:4222"])
    name: Optional[str] = None
    subject_prefix: str = ""
    jetstream_enabled: bool = True
    storage: str = "file"
    replicas: int = 1
    ack_wait_seconds: float = 3600.0
    max_deliver: int = 5
    # Worker queues acknowledge at admission (custom_transport.nats_pull). Not
    # an operator setting; tests shorten it to observe redelivery quickly.
    pool_ack_wait_seconds: float = POOL_ACK_WAIT_SECONDS
    duplicate_window_seconds: float = 600.0
    work_max_age_seconds: Optional[float] = 604800.0
    presence_max_age_seconds: Optional[float] = 600.0
    process_consumer_inactive_threshold_seconds: float = 300.0
    user: Optional[str] = None
    password: Optional[str] = None
    credentials_file: Optional[str] = None


class NatsTransportAdapter:
    """Small adapter around nats-py that preserves the project message protocol."""

    def __init__(
        self,
        *,
        config: NatsTransportConfig,
        mapper: NatsSubjectMapper,
        logger: Optional[logging.Logger] = None,
    ) -> None:
        self.config = config
        self.mapper = mapper
        self._logger = logger or logging.getLogger(__name__)
        self._nc: Any = None
        self._js: Any = None
        self._subscriptions: list[SubscriptionHandle] = []
        self._delivery_tasks: set[asyncio.Task[Any]] = set()
        self._closing = False
        # Receive loops that ended without being asked to (custom_transport.nats_pull).
        self._stopped_receivers: dict[str, StoppedReceiver] = {}
        # The team lead's binding to the dead-letter stream (fetch_dead_letters).
        self._dead_letter_subscriptions: dict[str, Any] = {}

    @property
    def connected(self) -> bool:
        return bool(self._nc)

    async def connect(self, *, reconcile_topology: bool = False) -> None:
        try:
            import nats
        except (
            ImportError
        ) as exc:  # pragma: no cover - exercised only without optional dependency
            raise RuntimeError(
                "NATS transport selected, but nats-py is not installed. Install requirements.txt first."
            ) from exc

        kwargs: dict[str, Any] = {
            "servers": self.config.servers,
            "error_cb": self._on_nats_error,
        }
        if self.config.name:
            kwargs["name"] = self.config.name
        if self.config.user:
            kwargs["user"] = self.config.user
        if self.config.password:
            kwargs["password"] = self.config.password
        if self.config.credentials_file:
            kwargs["user_credentials"] = self.config.credentials_file
        self._closing = False
        self._nc = await nats.connect(**kwargs)
        try:
            if self.config.jetstream_enabled:
                self._js = self._nc.jetstream()
                if reconcile_topology:
                    await self.reconcile_streams()
                else:
                    await self.validate_streams()
        except Exception:
            await self.close()
            raise

    async def close(self) -> None:
        nc = self._nc
        if nc is None:
            self._subscriptions.clear()
            self._js = None
            return
        self._closing = True
        try:
            subscriptions = list(self._subscriptions)
            for subscription in subscriptions:
                try:
                    await subscription.unsubscribe()
                except Exception as exc:
                    self._logger.debug(
                        "Failed to unsubscribe NATS subscription %s: %s",
                        subscription.name,
                        exc,
                    )
            # Unsubscribe cancels the SDK subscription loop but does not join
            # its running callback. Retain resources until admitted writes and
            # callback cleanup have reached a disposition.
            callbacks = self._delivery_tasks - {asyncio.current_task()}
            for callback in callbacks:
                callback.cancel()
            await asyncio.gather(*callbacks, return_exceptions=True)
            await self._delete_process_consumers(subscriptions)
            self._subscriptions.clear()
            with contextlib.suppress(Exception):
                nc.options["allow_reconnect"] = False
            if bool(getattr(nc, "is_closed", False)):
                return
            try:
                if bool(getattr(nc, "is_reconnecting", False)) or bool(
                    getattr(nc, "is_connecting", False)
                ):
                    await nc.close()
                else:
                    await nc.drain()
            except Exception as exc:
                if self._is_expected_shutdown_error(exc):
                    self._logger.debug(
                        "NATS connection closed while transport was shutting down: %s",
                        exc,
                    )
                else:
                    self._logger.warning(
                        "NATS drain failed during shutdown; forcing close: %s", exc
                    )
                with contextlib.suppress(Exception):
                    await nc.close()
        finally:
            self._subscriptions.clear()
            self._nc = None
            self._js = None
            self._closing = False

    async def unsubscribe_consumer_role(self, consumer_role: str) -> int:
        """Stop consuming every subscription with this role; keep the others.

        Shared route consumers (for example a named-type queue group) belong to
        every worker of that type, so this only removes this process's
        subscriptions and never deletes the durable consumer. In-flight
        delivery callbacks are left to finish and acknowledge normally.
        """
        stopped = 0
        for handle in [
            handle
            for handle in self._subscriptions
            if handle.consumer_role == consumer_role
        ]:
            try:
                await handle.unsubscribe()
            except Exception:
                self._logger.warning(
                    "NATS subscription could not be stopped; its messages may "
                    "still arrive. event=nats_unsubscribe_failed subscription=%s "
                    "consumer_role=%s",
                    handle.name,
                    consumer_role,
                    exc_info=True,
                )
                continue
            self._subscriptions.remove(handle)
            stopped += 1
        return stopped

    async def _delete_process_consumers(
        self,
        subscriptions: list[SubscriptionHandle],
    ) -> None:
        """Delete consumers whose identity ends with this process."""

        if self._js is None:
            return
        targets = {
            (
                subscription.consumer_stream_name,
                subscription.consumer_name,
            ): subscription
            for subscription in subscriptions
            if subscription.consumer_scope == ConsumerScope.PROCESS
            and subscription.consumer_stream_name
            and subscription.consumer_name
        }
        if not targets:
            return
        await asyncio.gather(
            *(
                self._delete_process_consumer(
                    stream_name=stream_name,
                    consumer_name=consumer_name,
                    subscription=subscription,
                )
                for (stream_name, consumer_name), subscription in targets.items()
            )
        )

    async def _delete_process_consumer(
        self,
        *,
        stream_name: str,
        consumer_name: str,
        subscription: SubscriptionHandle,
    ) -> None:
        pending = None
        ack_pending = None
        with contextlib.suppress(Exception):
            info = await asyncio.wait_for(
                self._js.consumer_info(stream_name, consumer_name),
                timeout=_CONSUMER_DELETE_TIMEOUT_SECONDS,
            )
            pending = getattr(info, "num_pending", None)
            ack_pending = getattr(info, "num_ack_pending", None)
        try:
            await asyncio.wait_for(
                self._js.delete_consumer(stream_name, consumer_name),
                timeout=_CONSUMER_DELETE_TIMEOUT_SECONDS,
            )
        except Exception as exc:
            if self._is_consumer_not_found(exc):
                return
            self._logger.warning(
                "Failed to delete process-scoped NATS consumer. "
                "event=nats_process_consumer_delete_failed stream_name=%s "
                "consumer_name=%s consumer_role=%s owner_agent_id=%s "
                "num_pending=%s num_ack_pending=%s error=%s",
                stream_name,
                consumer_name,
                subscription.consumer_role,
                subscription.owner_agent_id,
                pending,
                ack_pending,
                exc,
            )
            return
        self._logger.info(
            "Deleted process-scoped NATS consumer. "
            "event=nats_process_consumer_deleted stream_name=%s "
            "consumer_name=%s consumer_role=%s owner_agent_id=%s "
            "num_pending=%s num_ack_pending=%s",
            stream_name,
            consumer_name,
            subscription.consumer_role,
            subscription.owner_agent_id,
            pending,
            ack_pending,
        )

    @staticmethod
    def _is_consumer_not_found(exc: Exception) -> bool:
        if exc.__class__.__name__ in {"NotFoundError", "ConsumerNotFoundError"}:
            return True
        return "consumer not found" in str(exc).lower()

    async def _on_nats_error(self, exc: Exception) -> None:
        if self._is_expected_shutdown_error(exc):
            self._logger.debug(
                "NATS client reported expected shutdown connection loss: %s", exc
            )
            return
        self._logger.error("NATS client error: %s", exc, exc_info=exc)

    def _is_expected_shutdown_error(self, exc: Exception) -> bool:
        if not self._closing:
            return False
        exc_name = exc.__class__.__name__
        if exc_name in {
            "UnexpectedEOF",
            "ConnectionClosedError",
            "ConnectionReconnectingError",
        }:
            return True
        if isinstance(
            exc, (BrokenPipeError, ConnectionResetError, ConnectionRefusedError)
        ):
            return True
        if isinstance(exc, OSError) and getattr(exc, "errno", None) in {
            errno.ECONNRESET,
            errno.EPIPE,
            errno.ECONNREFUSED,
        }:
            return True
        return False

    def _topology(self) -> NatsStreamTopology:
        return NatsStreamTopology(self.config, self.mapper)

    def stream_names(self) -> tuple[str, ...]:
        """The JetStream streams this team's topology defines, for log lines."""

        return tuple(spec.name for spec in self._topology().specifications())

    async def reconcile_streams(self) -> None:
        if self._js is not None:
            await self._topology().reconcile(self._js, self._logger)

    async def validate_streams(self) -> None:
        if self._js is not None:
            await self._topology().validate(self._js)

    async def publish(
        self,
        *,
        envelope: MessageEnvelope,
        routing_key: str,
        options: Optional[TransportPublishOptions] = None,
    ) -> Any:
        opts = options or TransportPublishOptions()
        if self._nc is None:
            await self.connect()
        if opts.operational:
            subject = self.mapper.operational_subject()
        elif opts.broadcast or opts.presence:
            subject = self.mapper.broadcast_subject(envelope=envelope)
        else:
            subject = self.mapper.route_subject(routing_key, envelope=envelope)
        return await self.publish_subject(
            subject=subject, envelope=envelope, options=opts
        )

    async def publish_subject(
        self,
        *,
        subject: str,
        envelope: MessageEnvelope,
        options: Optional[TransportPublishOptions] = None,
    ) -> Any:
        if self._nc is None:
            await self.connect()
        opts = options or TransportPublishOptions()
        body = encode_envelope(envelope)
        headers = self._headers_for(envelope, opts)
        if (
            self.config.jetstream_enabled
            and self._js is not None
            and opts.durable
            and not opts.operational
        ):
            return await self._js.publish(subject, body, headers=headers)
        await self._nc.publish(subject, body, headers=headers)
        return None

    # -- worker queues ------------------------------------------------------

    def worker_queue_consumer_config(self, named_agent_type: str) -> Any:
        """The shared pull consumer of one worker type's queue."""

        return self._consumer_config(
            durable_name=self.worker_queue_durable_name(named_agent_type),
            filter_subject=self.mapper.named_type_work_subject(named_agent_type),
            deliver_last_per_subject=False,
            consumer_scope=ConsumerScope.ROUTE,
            consumer_role=NAMED_WORKER_CONSUMER_ROLE,
            owner_agent_id=None,
            ack_wait_seconds=float(self.config.pool_ack_wait_seconds),
            max_deliver=POOL_MAX_DELIVER,
        )

    def worker_queue_durable_name(self, named_agent_type: str) -> str:
        return self.mapper.durable_name(f"workers_{named_agent_type}")

    async def ensure_worker_queue(self, named_agent_type: str) -> None:
        """Create a worker type's queue consumer before any worker of it runs.

        The pool controller counts waiting tasks from this consumer, so tasks
        for a type with no worker are counted too.
        """

        if self._nc is None:
            await self.connect()
        if self._js is None:
            raise RuntimeError("Worker queues require JetStream.")
        await ensure_pull_consumer(
            self._js,
            stream_name=self.mapper.work_stream_name(),
            config=self.worker_queue_consumer_config(named_agent_type),
            logger=self._logger,
        )

    async def worker_queue_state(self, named_agent_type: str) -> Optional[ConsumerQueueState]:
        """Waiting tasks, idle workers asking, and handed-out tasks; None if absent."""

        if self._nc is None:
            await self.connect()
        if self._js is None:
            return None
        from nats.js.errors import NotFoundError

        try:
            info = await self._js.consumer_info(
                self.mapper.work_stream_name(),
                self.worker_queue_durable_name(named_agent_type),
            )
        except NotFoundError:
            return None
        return ConsumerQueueState(
            waiting=int(getattr(info, "num_pending", 0) or 0),
            asking=int(getattr(info, "num_waiting", 0) or 0),
            unacknowledged=int(getattr(info, "num_ack_pending", 0) or 0),
        )

    async def oldest_worker_queue_message_at(
        self, named_agent_type: str
    ) -> Optional[datetime]:
        """When the oldest task still in a worker type's queue was published.

        A work-queue stream deletes a message once it is acknowledged, so the
        first message on the type's subject is the one that has waited longest
        (it may be handed out but not yet acknowledged).
        """

        if self._nc is None:
            await self.connect()
        if self._js is None:
            return None
        from nats.js.errors import NotFoundError

        try:
            message = await self._js.get_msg(
                self.mapper.work_stream_name(),
                seq=1,
                subject=self.mapper.named_type_work_subject(named_agent_type),
                next=True,
            )
        except NotFoundError:
            return None
        published = getattr(message, "time", None)
        if isinstance(published, datetime):
            return published if published.tzinfo else published.replace(tzinfo=timezone.utc)
        return None

    async def delete_work_message(self, sequence: int) -> bool:
        """Remove one message from the work stream; False if it was already gone.

        Used to withdraw a delegated task that no worker has taken yet.
        """

        if self._nc is None:
            await self.connect()
        if self._js is None:
            return False
        from nats.js.errors import NotFoundError

        try:
            return bool(
                await self._js.delete_msg(self.mapper.work_stream_name(), int(sequence))
            )
        except NotFoundError:
            return False
        except Exception as exc:
            if "no message found" in str(exc).lower() or "not found" in str(exc).lower():
                return False
            raise

    async def put_object(
        self,
        *,
        bucket: str,
        object_name: str,
        path: str,
        ttl_seconds: Optional[float] = None,
        max_bytes: Optional[int] = None,
        headers: Optional[dict[str, str]] = None,
    ) -> Any:
        """Store a local file in a JetStream Object Store bucket."""

        if self._nc is None:
            await self.connect()
        if self._js is None:
            raise RuntimeError("NATS Object Store requires JetStream.")
        try:
            from nats.js.api import ObjectMeta, ObjectStoreConfig, StorageType
            from nats.js.errors import BucketNotFoundError
        except ImportError as exc:  # pragma: no cover
            raise RuntimeError(
                "Installed nats-py does not expose Object Store API classes."
            ) from exc

        try:
            store = await self._js.object_store(bucket)
        except BucketNotFoundError:
            config = ObjectStoreConfig(
                bucket=bucket,
                ttl=float(ttl_seconds) if ttl_seconds else None,
                max_bytes=int(max_bytes) if max_bytes else None,
                storage=StorageType.FILE
                if str(self.config.storage).lower() == "file"
                else StorageType.MEMORY,
                replicas=int(self.config.replicas or 1),
            )
            store = await self._js.create_object_store(bucket=bucket, config=config)
        with open(path, "rb") as handle:
            return await store.put(
                object_name,
                handle,
                meta=ObjectMeta(name=object_name, headers=dict(headers or {})),
            )

    async def get_object(
        self, *, bucket: str, object_name: str, destination_path: str
    ) -> Any:
        """Fetch a JetStream Object Store object to a local file path."""

        if self._nc is None:
            await self.connect()
        if self._js is None:
            raise RuntimeError("NATS Object Store requires JetStream.")
        store = await self._js.object_store(bucket)
        with open(destination_path, "wb") as handle:
            return await store.get(object_name, writeinto=handle)

    def _headers_for(
        self, envelope: MessageEnvelope, options: TransportPublishOptions
    ) -> dict[str, str]:
        headers = {
            "content-type": "application/json",
            "to_routing_path": str(envelope.msg_header.to_routing_path or ""),
            "from_agent_id": str(envelope.msg_header.from_agent_id or ""),
            "message_id": str(envelope.msg_header.message_id or ""),
        }
        idempotency_key = (
            options.idempotency_key
            or envelope.msg_header.idempotency_key
            or envelope.msg_header.outbox_action_id
            or envelope.msg_header.message_id
        )
        if idempotency_key:
            headers["Nats-Msg-Id"] = str(idempotency_key)
        if options.ttl_seconds is not None:
            headers["ttl_seconds"] = str(options.ttl_seconds)
        headers.update({str(k): str(v) for k, v in dict(options.headers or {}).items()})
        return headers

    async def subscribe_route(
        self,
        *,
        name: str,
        routing_key: str,
        handler: DeliveryHandler,
        durable_name: Optional[str] = None,
        deliver_last_per_subject: bool = False,
        delivery: SubscriptionDelivery = SubscriptionDelivery.JETSTREAM_DURABLE,
        consumer_scope: ConsumerScope = ConsumerScope.ROUTE,
        consumer_role: str = "route",
        owner_agent_id: Optional[str] = None,
        fetch_policy: FetchPolicy = FetchPolicy.CONTINUOUS,
        ready: Optional[Callable[[], bool]] = None,
    ) -> SubscriptionHandle:
        subject = self.mapper.route_subject(routing_key)
        return await self.subscribe_subject(
            name=name,
            subject=subject,
            handler=handler,
            durable_name=durable_name,
            logical_route=routing_key,
            deliver_last_per_subject=deliver_last_per_subject,
            delivery=delivery,
            consumer_scope=consumer_scope,
            consumer_role=consumer_role,
            owner_agent_id=owner_agent_id,
            fetch_policy=fetch_policy,
            ready=ready,
        )

    async def consumer_is_listening(
        self, *, stream_name: str, durable_name: str
    ) -> Optional[bool]:
        """Whether a durable pull consumer has a receiver asking for messages.

        False when the consumer does not exist (its owner never started, or a
        process-scoped consumer was removed after its process stopped asking).
        True when a pull request is open. None otherwise: a receiver that is
        handling a message has no request open for a moment, so the caller
        falls back to presence heartbeats.
        """

        if self._nc is None:
            await self.connect()
        if self._js is None:
            return None
        try:
            info = await self._js.consumer_info(stream_name, durable_name)
        except Exception as exc:
            message = str(exc).lower()
            if "not found" in message:
                return False
            self._logger.debug(
                "NATS consumer liveness probe failed for stream=%s durable=%s: %s",
                stream_name,
                durable_name,
                exc,
            )
            return None
        return True if int(getattr(info, "num_waiting", 0) or 0) > 0 else None

    async def subscribe_subject(
        self,
        *,
        name: str,
        subject: str,
        handler: DeliveryHandler,
        durable_name: Optional[str] = None,
        logical_route: Optional[str] = None,
        delivery: SubscriptionDelivery = SubscriptionDelivery.JETSTREAM_DURABLE,
        consumer_scope: ConsumerScope = ConsumerScope.ROUTE,
        consumer_role: str = "subject",
        owner_agent_id: Optional[str] = None,
        deliver_last_per_subject: bool = False,
        fetch_policy: FetchPolicy = FetchPolicy.CONTINUOUS,
        ready: Optional[Callable[[], bool]] = None,
        core_queue_group: Optional[str] = None,
    ) -> SubscriptionHandle:
        """Subscribe one handler.

        Durable JetStream subscriptions are pull consumers (ADR-019, see
        ``custom_transport.nats_pull``). Core NATS and ephemeral JetStream
        subscriptions (presence replay) are delivered as they arrive.
        ``core_queue_group`` lets one of several Core NATS listeners answer a
        live request (nothing is stored, so there is nothing to pull);
        JetStream subscriptions never use deliver groups.
        """

        if self._nc is None:
            await self.connect()
        is_pool = fetch_policy == FetchPolicy.WHEN_READY
        max_deliver = POOL_MAX_DELIVER if is_pool else int(self.config.max_deliver or 1)
        ack_wait = (
            float(self.config.pool_ack_wait_seconds)
            if is_pool
            else float(self.config.ack_wait_seconds)
        )

        async def _deliver(msg: Any) -> None:
            await self._deliver_received(
                msg,
                handler=handler,
                subscription_name=name,
                logical_route=logical_route
                or self.mapper.logical_route_for_subject(
                    getattr(msg, "subject", subject)
                ),
                max_deliver=max_deliver,
                ack_wait_seconds=ack_wait,
                jetstream=self._uses_jetstream(delivery),
            )

        async def _callback(msg: Any) -> None:
            owner = asyncio.current_task()
            assert owner is not None
            if self._closing:
                raise asyncio.CancelledError
            self._delivery_tasks.add(owner)
            try:
                await _deliver(msg)
            finally:
                self._delivery_tasks.discard(owner)
                # A domain callback may consume a child-turn stop signal. It
                # cannot consume cancellation of the transport's own loop.
                if owner.cancelling():
                    raise asyncio.CancelledError

        uses_jetstream = self._uses_jetstream(delivery)
        self._validate_subscription_request(
            name=name,
            subject=subject,
            delivery=delivery,
            durable_name=durable_name,
            fetch_policy=fetch_policy,
            ready=ready,
        )
        if core_queue_group and delivery != SubscriptionDelivery.CORE:
            raise ValueError(
                "Only Core NATS request listeners may share a queue group; "
                "durable work is pulled (ADR-019)."
            )
        if delivery == SubscriptionDelivery.JETSTREAM_DURABLE:
            stream_name = self._stream_name_for_subject(subject)
            runner = PullRunner(
                name=name,
                jetstream=self._js,
                stream_name=stream_name,
                consumer_config=self._consumer_config(
                    durable_name=durable_name,
                    filter_subject=subject,
                    deliver_last_per_subject=deliver_last_per_subject,
                    consumer_scope=consumer_scope,
                    consumer_role=consumer_role,
                    owner_agent_id=owner_agent_id,
                    ack_wait_seconds=ack_wait,
                    max_deliver=max_deliver,
                ),
                policy=fetch_policy,
                ready=ready,
                callback=_callback,
                is_closing=lambda: self._closing,
                logger=self._logger,
                set_aside=lambda message, reason, error: self._set_aside(
                    message, reason=reason, error=error, subscription_name=name
                ),
                on_stopped=self._record_stopped_receiver,
            )
            await runner.start()
            subscription: Any = runner
            consumer_stream_name: Optional[str] = stream_name
            consumer_name: Optional[str] = durable_name
        else:
            (
                subscription,
                consumer_stream_name,
                consumer_name,
            ) = await self._create_push_subscription(
                subject=subject,
                callback=_callback,
                delivery=delivery,
                core_queue_group=core_queue_group,
                deliver_last_per_subject=deliver_last_per_subject,
                consumer_scope=consumer_scope,
                consumer_role=consumer_role,
                owner_agent_id=owner_agent_id,
            )
        handle = SubscriptionHandle(
            name=name,
            subject=subject,
            raw_subscription=subscription,
            delivery=delivery,
            consumer_scope=consumer_scope if uses_jetstream else None,
            consumer_stream_name=consumer_stream_name,
            consumer_name=consumer_name,
            consumer_role=consumer_role if uses_jetstream else None,
            owner_agent_id=owner_agent_id if uses_jetstream else None,
        )
        self._subscriptions.append(handle)
        return handle

    @staticmethod
    def _uses_jetstream(delivery: SubscriptionDelivery) -> bool:
        return delivery in {
            SubscriptionDelivery.JETSTREAM_DURABLE,
            SubscriptionDelivery.JETSTREAM_EPHEMERAL,
        }

    def _validate_subscription_request(
        self,
        *,
        name: str,
        subject: str,
        delivery: SubscriptionDelivery,
        durable_name: Optional[str],
        fetch_policy: FetchPolicy,
        ready: Optional[Callable[[], bool]],
    ) -> None:
        uses_jetstream = self._uses_jetstream(delivery)
        if uses_jetstream and not self.config.jetstream_enabled:
            raise RuntimeError(
                f"Subscription {name!r} requires JetStream, but JetStream is disabled."
            )
        if uses_jetstream and self._js is None:
            raise RuntimeError(
                f"Subscription {name!r} requires an initialized JetStream context."
            )
        if delivery == SubscriptionDelivery.JETSTREAM_EPHEMERAL and durable_name:
            raise ValueError(
                "Ephemeral JetStream subscriptions cannot declare a durable name."
            )
        if delivery == SubscriptionDelivery.JETSTREAM_DURABLE and not durable_name:
            raise ValueError("Durable JetStream subscriptions require a durable name.")
        if (
            delivery == SubscriptionDelivery.JETSTREAM_EPHEMERAL
            and self._stream_name_for_subject(subject) == self.mapper.work_stream_name()
        ):
            # ADR-019: work is pulled by the receiver that can act on it. An
            # ephemeral push listener on the work stream would take work
            # without asking; only presence replay uses one.
            raise ValueError(
                f"Subscription {name!r} would push work-stream messages; "
                "work is pulled through a durable subscription (ADR-019)."
            )
        if fetch_policy == FetchPolicy.WHEN_READY and (
            delivery != SubscriptionDelivery.JETSTREAM_DURABLE or ready is None
        ):
            raise ValueError(
                "A worker queue subscription must be durable and say when it is ready."
            )

    async def _create_push_subscription(
        self,
        *,
        subject: str,
        callback: Any,
        delivery: SubscriptionDelivery,
        core_queue_group: Optional[str],
        deliver_last_per_subject: bool,
        consumer_scope: ConsumerScope,
        consumer_role: str,
        owner_agent_id: Optional[str],
    ) -> tuple[Any, Optional[str], Optional[str]]:
        """Core NATS, or an ephemeral JetStream replay listener (presence).

        Every listener must see every message here, so there is no shared work
        to hand out and nothing to pull.
        """

        if not self._uses_jetstream(delivery):
            if core_queue_group:
                subscription = await self._nc.subscribe(
                    subject, queue=core_queue_group, cb=callback
                )
            else:
                subscription = await self._nc.subscribe(subject, cb=callback)
            return subscription, None, None
        config = self._consumer_config(
            durable_name=None,
            filter_subject=None,
            deliver_last_per_subject=deliver_last_per_subject,
            consumer_scope=consumer_scope,
            consumer_role=consumer_role,
            owner_agent_id=owner_agent_id,
            ack_wait_seconds=float(self.config.ack_wait_seconds),
            max_deliver=int(self.config.max_deliver or 1),
        )
        subscription = await self._js.subscribe(
            subject,
            cb=callback,
            manual_ack=True,
            config=config,
        )
        stream_name, consumer_name = await self._consumer_identity(
            subscription,
            subject=subject,
            fallback_consumer_name=None,
        )
        return subscription, stream_name, consumer_name

    async def _consumer_identity(
        self,
        subscription: Any,
        *,
        subject: str,
        fallback_consumer_name: Optional[str],
    ) -> tuple[Optional[str], Optional[str]]:
        stream_name = self._stream_name_for_subject(subject)
        consumer_name = fallback_consumer_name
        consumer_info = getattr(subscription, "consumer_info", None)
        if consumer_info is None:
            return stream_name, consumer_name
        try:
            info = await consumer_info()
        except Exception as exc:
            self._logger.warning(
                "Could not resolve NATS consumer identity after subscription. "
                "event=nats_consumer_identity_unavailable subject=%s error=%s",
                subject,
                exc,
            )
            return stream_name, consumer_name
        return (
            getattr(info, "stream_name", None) or stream_name,
            getattr(info, "name", None) or consumer_name,
        )

    def _stream_name_for_subject(self, subject: str) -> str:
        if subject.startswith(f"{self.mapper.prefix}.presence."):
            return self.mapper.presence_stream_name()
        return self.mapper.work_stream_name()

    def _ttl_seconds_from_msg(self, msg: Any) -> Optional[float]:
        headers = getattr(msg, "headers", None) or {}
        raw_value = None
        try:
            header_items = dict(headers).items()
        except Exception:
            header_items = []
        for key, value in header_items:
            if str(key).lower() == "ttl_seconds":
                raw_value = value
                break
        if raw_value is None:
            return None
        if isinstance(raw_value, (list, tuple)):
            raw_value = raw_value[0] if raw_value else None
        try:
            ttl_seconds = float(raw_value)
        except (TypeError, ValueError):
            self._logger.warning(
                "Ignoring invalid NATS ttl_seconds header: %r", raw_value
            )
            return None
        if ttl_seconds <= 0:
            return None
        return ttl_seconds

    def _delivery_ttl_expired(
        self, delivery: InboundDelivery, ttl_seconds: float
    ) -> bool:
        raw_timestamp = getattr(delivery.envelope.msg_header, "timestamp", None)
        if not raw_timestamp:
            return False
        try:
            created_at = datetime.fromisoformat(
                str(raw_timestamp).replace("Z", "+00:00")
            )
        except ValueError:
            self._logger.debug(
                "Could not parse message timestamp for TTL enforcement: %r",
                raw_timestamp,
            )
            return False
        if created_at.tzinfo is None:
            created_at = created_at.replace(tzinfo=timezone.utc)
        return datetime.now(timezone.utc) - created_at > timedelta(seconds=ttl_seconds)

    def _consumer_config(
        self,
        *,
        durable_name: Optional[str],
        filter_subject: Optional[str],
        deliver_last_per_subject: bool,
        consumer_scope: ConsumerScope,
        consumer_role: str,
        owner_agent_id: Optional[str],
        ack_wait_seconds: float,
        max_deliver: int,
    ) -> Any:
        try:
            from nats.js.api import AckPolicy, ConsumerConfig, DeliverPolicy
        except ImportError as exc:  # pragma: no cover
            raise RuntimeError(
                "Installed nats-py does not expose JetStream consumer API classes."
            ) from exc
        kwargs: dict[str, Any] = {
            "ack_policy": AckPolicy.EXPLICIT,
            "ack_wait": float(ack_wait_seconds),
            "max_deliver": int(max_deliver or 1),
        }
        if durable_name:
            kwargs["durable_name"] = durable_name
            kwargs["name"] = durable_name
        if filter_subject:
            kwargs["filter_subject"] = filter_subject
        if deliver_last_per_subject:
            kwargs["deliver_policy"] = DeliverPolicy.LAST_PER_SUBJECT
        if consumer_scope == ConsumerScope.PROCESS:
            threshold = float(self.config.process_consumer_inactive_threshold_seconds)
            if threshold <= 0:
                raise ValueError(
                    "process_consumer_inactive_threshold_seconds must be positive."
                )
            kwargs["inactive_threshold"] = threshold
        kwargs["metadata"] = {
            "ua.managed": "true",
            "ua.schema": CONSUMER_METADATA_SCHEMA,
            "ua.team_id": self.mapper.team_id,
            "ua.consumer_scope": consumer_scope.value,
            "ua.consumer_role": str(consumer_role or "subject"),
            "ua.owner_agent_id": str(owner_agent_id or ""),
        }
        return ConsumerConfig(**kwargs)

    async def _deliver_received(
        self,
        msg: Any,
        *,
        handler: DeliveryHandler,
        subscription_name: str,
        logical_route: str,
        max_deliver: int,
        ack_wait_seconds: float,
        jetstream: bool,
    ) -> None:
        """Decode one received message, drop it if expired, and hand it to its handler."""

        delivery_record = await self._decode_or_settle(
            msg,
            subscription_name=subscription_name,
            logical_route=logical_route,
            max_deliver=max_deliver,
            ack_wait_seconds=ack_wait_seconds,
            jetstream=jetstream,
        )
        if delivery_record is None:
            return
        ttl_seconds = self._ttl_seconds_from_msg(msg)
        if ttl_seconds is not None and self._delivery_ttl_expired(
            delivery_record, ttl_seconds
        ):
            await delivery_record.term(f"expired ttl_seconds={ttl_seconds:g}")
            self._logger.info(
                "Terminated expired NATS message subject=%s route=%s ttl_seconds=%s",
                delivery_record.transport_subject,
                delivery_record.logical_route,
                ttl_seconds,
            )
            return
        await handler(delivery_record)

    async def _decode_or_settle(
        self,
        msg: Any,
        *,
        subscription_name: str,
        logical_route: str,
        max_deliver: int,
        ack_wait_seconds: float,
        jetstream: bool,
    ) -> Optional[InboundDelivery]:
        """Decode one received message, or settle one this release cannot read.

        A message that fails to decode (malformed JSON, or a message type or
        field value another release added or removed) is a property of that
        message, not of this receiver: it is logged without its content and
        given back with a delay so a receiver that can read it may take it,
        until its last allowed delivery ends it. Returns None for such a
        message. Core NATS messages cannot be given back; they are dropped.
        """

        try:
            return self._delivery_from_msg(
                msg,
                logical_route=logical_route,
                max_deliver=max_deliver if jetstream else None,
                ack_wait_seconds=ack_wait_seconds if jetstream else None,
            )
        except ValueError as exc:
            # ValueError covers JSON errors, Pydantic validation errors and the
            # codec's missing-identity check.
            summary = _decode_error_summary(exc)
            outcome = (
                await return_or_end(
                    msg,
                    max_deliver=max_deliver,
                    set_aside=lambda: self._set_aside(
                        msg, reason=REASON_UNDECODABLE, error=summary, subscription_name=subscription_name
                    ),
                )
                if jetstream
                else _DROPPED
            )
            message_id, message_class = _undecodable_identity(getattr(msg, "data", b""))
            self._logger.error(
                "Received a message this release cannot read. "
                "event=nats_message_undecodable subscription=%s subject=%s "
                "message_id=%s message_class=%s delivery=%s max_deliver=%s "
                "outcome=%s error=%s",
                subscription_name,
                getattr(msg, "subject", ""),
                message_id,
                message_class,
                delivery_count(msg) if jetstream else None,
                max_deliver if jetstream else None,
                outcome,
                summary,
            )
            return None

    async def _set_aside(
        self, msg: Any, *, reason: str, error: str, subscription_name: str
    ) -> bool:
        """Copy a message about to be ended to the dead-letter stream; True when stored.

        The raw record keeps original bytes/non-reserved headers without added
        overhead. A separate diagnostic record points to it, so an original
        already at the broker size limit cannot close the receiver connection.
        Stable subjects retain one raw record per copy; diagnostic publications
        deduplicate by that copy's identity, including across stream restarts.
        """

        copy = _dead_letter_copy(
            msg, reason=reason, error=error, receiver=subscription_name
        )
        raw_subject = self.mapper.dead_letter_raw_subject(copy.copy_id)
        target = self.mapper.dead_letter_notice_subject(copy.copy_id)
        copy.headers[HEADER_RAW_SUBJECT] = raw_subject
        try:
            if self._js is None:
                raise RuntimeError("JetStream is not connected.")
            await self._publish_dead_letter(
                raw_subject,
                bytes(getattr(msg, "data", b"") or b""),
                headers=copy.original_headers or None,
            )
            # Only report a copy after its unchanged original is stored.
            await self._publish_dead_letter(target, b"", headers=copy.headers)
        except Exception as exc:
            self._logger.error(
                "Could not finish setting a message aside before ending it. "
                "event=nats_dead_letter_failed subscription=%s subject=%s stream_sequence=%s "
                "message_id=%s message_class=%s reason=%s error_type=%s raw_subject=%s",
                subscription_name,
                copy.original_subject,
                copy.stream_sequence or None,
                copy.message_id,
                copy.message_class,
                reason,
                type(exc).__name__,
                raw_subject,
            )
            return False
        self._logger.warning(
            "Set a message aside before ending it; administrators are told. "
            "event=nats_message_dead_lettered subscription=%s subject=%s stream_sequence=%s "
            "message_id=%s message_class=%s reason=%s dead_letter_subject=%s",
            subscription_name,
            copy.original_subject,
            copy.stream_sequence or None,
            copy.message_id,
            copy.message_class,
            reason,
            target,
        )
        return True

    async def _publish_dead_letter(
        self, subject: str, payload: bytes, *, headers: Optional[dict[str, str]]
    ) -> None:
        from nats.errors import MaxPayloadError

        if _nats_payload_size(payload, headers) > self._nc.max_payload:
            # Reject locally. An oversized HPUB closes the shared connection
            # before nats-py's body-only size check can protect the receivers.
            raise MaxPayloadError
        await self._js.publish(subject, payload, headers=headers)

    def _record_stopped_receiver(self, subscription: str, error: Optional[BaseException]) -> None:
        self._stopped_receivers[subscription] = StoppedReceiver(
            subscription=subscription,
            stopped_at=datetime.now(timezone.utc),
            error_type=type(error).__name__ if error is not None else None,
        )

    def stopped_receivers(self) -> tuple[StoppedReceiver, ...]:
        """Receive loops of this adapter that ended without being asked to."""

        return tuple(self._stopped_receivers.values())

    async def fetch_dead_letters(
        self, *, consumer_route: str, batch: int = 20, timeout_seconds: float = 0.5
    ) -> list[DeadLetterRecord]:
        """New set-aside messages for one durable reader (the team lead's watch).

        The reader acknowledges each record after it told people, so a record
        whose report failed comes back on the next call. Records carry
        identifiers only (``DeadLetterRecord``). It never connects: the agent's
        start does, and until then there is nothing to read.
        """

        if self._js is None:
            return []
        stream = self.mapper.dead_letter_stream_name()
        durable = self.mapper.durable_name(consumer_route)
        subscription = self._dead_letter_subscriptions.get(durable)
        if subscription is None:
            config = self._consumer_config(
                durable_name=durable,
                filter_subject=self.mapper.dead_letter_notice_subjects(),
                deliver_last_per_subject=False,
                consumer_scope=ConsumerScope.ROUTE,
                consumer_role=consumer_route,
                owner_agent_id=None,
                ack_wait_seconds=float(self.config.pool_ack_wait_seconds),
                # A report that keeps failing must still be retried.
                max_deliver=-1,
            )
            await ensure_pull_consumer(self._js, stream_name=stream, config=config, logger=self._logger)
            subscription = await self._js.pull_subscribe_bind(durable=durable, stream=stream)
            self._dead_letter_subscriptions[durable] = subscription
        try:
            # The reader runs on every scheduler pass, and a fetch from an empty
            # stream waits its whole timeout; one quick question avoids that.
            info = await self._js.consumer_info(stream, durable)
        except Exception:
            # Bind again next time (the consumer may have been deleted).
            self._dead_letter_subscriptions.pop(durable, None)
            raise
        if not (info.num_pending or info.num_ack_pending):
            return []
        try:
            messages = await subscription.fetch(batch, timeout=timeout_seconds)
        except TimeoutError:
            return []
        records = []
        for message in messages:
            try:
                sequence = int(message.metadata.sequence.stream)
                waiting_after = int(message.metadata.num_pending or 0)
            except Exception:
                sequence, waiting_after = 0, 0
            records.append(
                DeadLetterRecord.from_headers(
                    dict(getattr(message, "headers", None) or {}),
                    stream_sequence=sequence,
                    ack=message.ack,
                    waiting_after=waiting_after,
                )
            )
        return records

    def _delivery_from_msg(
        self,
        msg: Any,
        *,
        logical_route: str,
        max_deliver: Optional[int] = None,
        ack_wait_seconds: Optional[float] = None,
    ) -> InboundDelivery:
        envelope = decode_envelope(msg.data)
        metadata = None
        try:
            metadata = msg.metadata
        except Exception:
            metadata = None
        redelivery_count = None
        if metadata is not None:
            redelivery_count = max(
                0, int(getattr(metadata, "num_delivered", 1) or 1) - 1
            )

        async def _ack() -> None:
            ack = getattr(msg, "ack", None)
            if ack is not None:
                try:
                    await ack()
                except Exception as exc:
                    if exc.__class__.__name__ != "NotJSMessageError":
                        raise

        async def _nak(delay_seconds: Optional[float] = None) -> None:
            nak = getattr(msg, "nak", None)
            if nak is None:
                return
            try:
                if delay_seconds is None:
                    await nak()
                else:
                    await nak(delay=delay_seconds)
            except Exception as exc:
                if exc.__class__.__name__ != "NotJSMessageError":
                    raise

        async def _term(reason: Optional[str] = None) -> None:
            term = getattr(msg, "term", None)
            if term is None:
                await _ack()
                return
            try:
                await term(reason=reason)
            except TypeError:
                await term()
            except Exception as exc:
                if exc.__class__.__name__ != "NotJSMessageError":
                    raise

        async def _in_progress() -> None:
            progress = getattr(msg, "in_progress", None)
            if progress is not None:
                try:
                    await progress()
                except Exception as exc:
                    if exc.__class__.__name__ != "NotJSMessageError":
                        raise

        return InboundDelivery(
            envelope=envelope,
            logical_route=logical_route,
            transport_subject=getattr(msg, "subject", ""),
            transport_message_id=getattr(metadata, "sequence", None)
            if metadata is not None
            else None,
            redelivery_count=redelivery_count,
            source_queue_name=logical_route,
            max_deliver=max_deliver,
            ack_wait_seconds=ack_wait_seconds,
            _ack=_ack,
            _nak=_nak,
            _term=_term,
            _in_progress=_in_progress,
        )

    async def keepalive_while(
        self, delivery: InboundDelivery, task: asyncio.Task[Any]
    ) -> None:
        ack_wait = delivery.ack_wait_seconds or float(self.config.ack_wait_seconds)
        interval = max(1.0, min(30.0, float(ack_wait) / 2.0))
        while not task.done():
            await asyncio.sleep(interval)
            if not task.done():
                try:
                    await delivery.in_progress()
                except Exception as exc:
                    self._logger.debug(
                        "Failed to send NATS in-progress ack for %s: %s",
                        delivery.transport_subject,
                        exc,
                    )
