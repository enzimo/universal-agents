"""Manager-facing gateway client with transport-neutral lifecycle and routing."""

from __future__ import annotations

import asyncio
import inspect
import logging
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import AsyncIterator, Awaitable, Callable

from assistant_gateway.application.ports import (
    GatewayClock,
    GatewayIdGenerator,
    GatewayManagerRuntime,
    GatewayManagerTransport,
)
from custom_messages.contracts.control_v1 import AgentStatus, SystemStatusMessage
from custom_messages.contracts.credentials_v1 import (
    SecureCredentialCollectionRequest,
    SignupAutomationRequest,
    ToolSetupRequestV1,
)
from custom_messages.contracts.delivery_v1 import UserNotification
from custom_messages.contracts.envelope_v1 import GATEWAY_MANAGER_CONTROL_TYPES, MessageEnvelope
from custom_messages.contracts.task_v1 import Task, TaskQuery, TaskSteering
from custom_messages.enums import MessageMetaType, RoutingType


EnvelopeHandler = Callable[[MessageEnvelope], Awaitable[None]]
ManagerRouteWriter = Callable[[str | None], Awaitable[None] | None]
ManagerTransitionHandler = Callable[
    ["GatewayManagerTransition"],
    Awaitable[None] | None,
]
TraceEnabled = Callable[[], bool]
_MANAGER_CONTROL_TYPES = GATEWAY_MANAGER_CONTROL_TYPES


@dataclass(frozen=True)
class GatewayManagerClientSettings:
    """Stable settings needed by the manager-client state machine."""

    instance_name: str
    user_display_name: str
    team_id: str
    manager_named_agent_type: str
    response_route: str
    initial_manager_id: str | None = None


@dataclass(frozen=True)
class GatewayManagerTransition:
    """One manager-route availability transition."""

    manager_id: str
    available: bool
    source: str
    reason: str
    became_available: bool = False


class GatewayManagerClient:
    """Own manager transport lifecycle, correlation, and the team lead's route.

    Which agent leads, and whether it is live, comes from the team's agent records
    (``assistant_gateway.team_view`` calls ``apply_manager_status``); before message
    contract step 7c this client discovered the lead on the operational subject and
    polled its liveness.
    """

    def __init__(
        self,
        *,
        settings: GatewayManagerClientSettings,
        clock: GatewayClock,
        id_generator: GatewayIdGenerator,
        logger: logging.Logger,
        runtime: GatewayManagerRuntime | None = None,
        outbound_transport: GatewayManagerTransport | None = None,
        on_unsolicited: EnvelopeHandler | None = None,
        on_manager_available: ManagerTransitionHandler | None = None,
        on_manager_unavailable: ManagerTransitionHandler | None = None,
        write_manager_route: ManagerRouteWriter | None = None,
        trace_enabled: TraceEnabled | None = None,
    ) -> None:
        self.settings = settings
        self._clock = clock
        self._id_generator = id_generator
        self._logger = logger
        self._runtime = runtime
        self._outbound_transport = outbound_transport or runtime
        self._outbound_runtime = runtime if outbound_transport is None else None
        self._on_unsolicited = on_unsolicited
        self._on_manager_available = on_manager_available
        self._on_manager_unavailable = on_manager_unavailable
        self._write_manager_route = write_manager_route
        self._trace_enabled = trace_enabled or (lambda: False)
        self._manager_id = settings.initial_manager_id
        self._lifecycle_connected = False
        self._manager_status_updated_at: datetime | None = None
        self._manager_status_lock = asyncio.Lock()
        # A schedule action keeps its originating request's correlation.
        # Distinct scheduled tasks can therefore wait for replies at once.
        self._response_queues: dict[tuple[str, str | None], asyncio.Queue[MessageEnvelope]] = {}
        self._control_queues: dict[str, asyncio.Queue[MessageEnvelope]] = {}

    @property
    def manager_id(self) -> str | None:
        """Return the current live manager route, if one is known."""

        return self._manager_id

    @property
    def manager_status_updated_at(self) -> datetime | None:
        """Return the timestamp of the last accepted manager-status transition."""

        return self._manager_status_updated_at

    @manager_status_updated_at.setter
    def manager_status_updated_at(self, value: datetime | None) -> None:
        self._manager_status_updated_at = value

    @property
    def connected(self) -> bool:
        """Return whether manager I/O is available."""

        if self._runtime is not None:
            return self._runtime.connected
        return self._outbound_transport is not None

    @property
    def pending_response_ids(self) -> tuple[str, ...]:
        """Return a snapshot of correlations awaiting manager responses."""

        return tuple(dict.fromkeys(correlation_id for correlation_id, _ in self._response_queues))

    def set_runtime(
        self,
        runtime: GatewayManagerRuntime,
        *,
        use_for_outbound: bool,
    ) -> None:
        """Install the runtime before connection."""

        if self._runtime is not None and self._runtime.connected:
            raise RuntimeError("Cannot replace a connected gateway manager runtime.")
        self._runtime = runtime
        if use_for_outbound:
            self._outbound_transport = runtime
            self._outbound_runtime = runtime

    def bind_inbound_handlers(
        self,
        *,
        on_unsolicited: EnvelopeHandler,
        on_manager_available: ManagerTransitionHandler,
        on_manager_unavailable: ManagerTransitionHandler,
    ) -> None:
        """Bind the composed application handlers before connecting the runtime."""

        if self._lifecycle_connected:
            raise RuntimeError(
                "Cannot replace handlers on a connected manager runtime."
            )
        self._on_unsolicited = on_unsolicited
        self._on_manager_available = on_manager_available
        self._on_manager_unavailable = on_manager_unavailable

    async def connect(self) -> None:
        """Connect the runtime and install transport-neutral inbound handlers."""

        if self._runtime is None:
            raise RuntimeError("Gateway manager runtime is not configured.")
        handlers: dict[str, EnvelopeHandler] = {
            "on_response": self.handle_response,
            "on_operational": self.handle_operational,
        }
        await self._runtime.connect(**handlers)
        self._lifecycle_connected = True

    async def close(self) -> None:
        """Close the runtime connection."""

        if self._runtime is not None:
            await self._runtime.close()
        self._lifecycle_connected = False

    @asynccontextmanager
    async def response_channel(
        self,
        correlation_id: str,
        *,
        task_id: str | None = None,
    ) -> AsyncIterator[asyncio.Queue[MessageEnvelope]]:
        """Register one terminal-response correlation for a bounded operation."""

        queue = self.register_response_channel(correlation_id, task_id=task_id)
        try:
            yield queue
        finally:
            self.unregister_response_channel(correlation_id, task_id=task_id, queue=queue)

    @asynccontextmanager
    async def control_channel(
        self,
        correlation_id: str,
    ) -> AsyncIterator[asyncio.Queue[MessageEnvelope]]:
        """Register one control-response correlation for a bounded operation."""

        queue = self.register_control_channel(correlation_id)
        try:
            yield queue
        finally:
            self.unregister_control_channel(correlation_id, queue=queue)

    def register_response_channel(
        self,
        correlation_id: str,
        *,
        task_id: str | None = None,
        queue: asyncio.Queue[MessageEnvelope] | None = None,
    ) -> asyncio.Queue[MessageEnvelope]:
        """Register and return a response queue for a correlation."""

        key = (correlation_id, task_id)
        if key in self._response_queues:
            raise RuntimeError(
                f"Response correlation {correlation_id!r} for task {task_id!r} is already registered."
            )
        registered = queue or asyncio.Queue()
        self._response_queues[key] = registered
        return registered

    def unregister_response_channel(
        self,
        correlation_id: str,
        *,
        task_id: str | None = None,
        queue: asyncio.Queue[MessageEnvelope] | None = None,
    ) -> None:
        """Remove a response queue without deleting a newer replacement."""

        key = (correlation_id, task_id)
        current = self._response_queues.get(key)
        if queue is None or current is queue:
            self._response_queues.pop(key, None)

    def register_control_channel(
        self,
        correlation_id: str,
        *,
        queue: asyncio.Queue[MessageEnvelope] | None = None,
    ) -> asyncio.Queue[MessageEnvelope]:
        """Register and return a control queue for a correlation."""

        if correlation_id in self._control_queues:
            raise RuntimeError(
                f"Control correlation {correlation_id!r} is already registered."
            )
        registered = queue or asyncio.Queue()
        self._control_queues[correlation_id] = registered
        return registered

    def unregister_control_channel(
        self,
        correlation_id: str,
        *,
        queue: asyncio.Queue[MessageEnvelope] | None = None,
    ) -> None:
        """Remove a control queue without deleting a newer replacement."""

        current = self._control_queues.get(correlation_id)
        if queue is None or current is queue:
            self._control_queues.pop(correlation_id, None)

    def get_control_channel(
        self,
        correlation_id: str,
    ) -> asyncio.Queue[MessageEnvelope]:
        """Return the registered control queue for a correlation."""

        try:
            return self._control_queues[correlation_id]
        except KeyError as exc:
            raise RuntimeError(
                f"Control correlation {correlation_id!r} is not registered."
            ) from exc

    def has_pending_response(self, correlation_id: str) -> bool:
        """Return whether a terminal response correlation is active."""

        return correlation_id in self.pending_response_ids

    def pending_response_correlation_ids(self) -> tuple[str, ...]:
        """Requests the gateway is still waiting on the agent to answer."""

        return self.pending_response_ids

    def get_response_channel(
        self,
        correlation_id: str,
        *,
        task_id: str | None = None,
    ) -> asyncio.Queue[MessageEnvelope]:
        """Return the registered response queue for a correlation."""

        try:
            return self._response_queues[(correlation_id, task_id)]
        except KeyError as exc:
            raise RuntimeError(
                f"Response correlation {correlation_id!r} for task {task_id!r} is not registered."
            ) from exc

    def _response_targets(
        self, correlation_id: str, envelope: MessageEnvelope
    ) -> tuple[asyncio.Queue[MessageEnvelope], ...]:
        """Match a task's own waiter before its request's general reply wait.

        An unmatched task must never use a sibling task's queue. Untargeted
        service availability notices wake every waiter for the correlation.
        """

        content = envelope.msg_content
        task_id = None
        if isinstance(content, Task):
            task_id = content.task_id
        elif isinstance(content, TaskSteering):
            task_id = content.target_task_id
        elif isinstance(content, (TaskQuery, SecureCredentialCollectionRequest, ToolSetupRequestV1, SignupAutomationRequest)):
            task_id = content.parent_task_id
        elif isinstance(content, UserNotification):
            task_id = content.source_task_id
        if isinstance(content, SystemStatusMessage):
            return tuple(queue for (correlation, _), queue in self._response_queues.items() if correlation == correlation_id)
        queue = self._response_queues.get((correlation_id, task_id))
        if queue is None and task_id is not None:
            queue = self._response_queues.get((correlation_id, None))
        return (queue,) if queue is not None else ()

    async def queue_response(
        self,
        correlation_id: str,
        envelope: MessageEnvelope,
    ) -> bool:
        """Queue a synthetic or inbound response for an active correlation."""

        queues = self._response_targets(correlation_id, envelope)
        for queue in queues:
            await queue.put(envelope)
        return bool(queues)

    async def handle_response(self, envelope: MessageEnvelope) -> None:
        """Route one inbound response envelope by correlation."""

        meta = getattr(envelope, "msg_meta", None)
        header = getattr(envelope, "msg_header", None)
        content = getattr(envelope, "msg_content", None)
        correlation_id = getattr(meta, "msg_correlation_id", None)
        if correlation_id and correlation_id in self._control_queues:
            await self._control_queues[correlation_id].put(envelope)
            return
        if isinstance(content, _MANAGER_CONTROL_TYPES):
            self._logger.warning(
                "Dropped an unmatched manager control response. "
                "event=gateway_unmatched_manager_control_response_dropped "
                "correlation_id=%s conversation_id=%s command_type=%s "
                "command_id=%s from_agent_id=%s",
                correlation_id,
                getattr(meta, "conversation_id", None),
                type(content).__name__,
                content.get_id(),
                getattr(header, "from_agent_id", None),
            )
            return

        matched_pending = bool(
            correlation_id and self._response_targets(correlation_id, envelope)
        )
        if self._trace_enabled():
            self._logger.info(
                "event=%s correlation_id=%s conversation_id=%s matched_pending=%s "
                "pending_correlation_count=%s header_to=%s "
                "header_response_routing_key=%s message_id=%s content_type=%s",
                (
                    "gateway_response_matched_pending_queue"
                    if matched_pending
                    else "gateway_response_unmatched_pending_queue"
                ),
                correlation_id,
                getattr(meta, "conversation_id", None),
                matched_pending,
                len(self._response_queues),
                getattr(header, "to_routing_path", None),
                getattr(header, "response_routing_key", None),
                getattr(header, "message_id", None),
                type(content).__name__,
            )
        if correlation_id and await self.queue_response(correlation_id, envelope):
            return
        if self._drop_agent_status(envelope, source="response"):
            return
        if self._on_unsolicited is not None:
            await self._on_unsolicited(envelope)

    async def handle_operational(self, envelope: MessageEnvelope) -> None:
        """Route one inbound operational envelope by correlation."""

        header = getattr(envelope, "msg_header", None)
        if getattr(header, "from_agent_id", None) == self.settings.response_route:
            return
        meta = getattr(envelope, "msg_meta", None)
        correlation_id = getattr(meta, "msg_correlation_id", None)
        if correlation_id and correlation_id in self._control_queues:
            await self._control_queues[correlation_id].put(envelope)
            return
        if correlation_id and await self.queue_response(correlation_id, envelope):
            return
        if self._drop_agent_status(envelope, source="operational"):
            return
        self._logger.debug(
            "Gateway manager client dropped unmatched operational response "
            "type=%s correlation_id=%s.",
            type(getattr(envelope, "msg_content", None)).__name__,
            correlation_id,
        )

    def _drop_agent_status(self, envelope: MessageEnvelope, *, source: str) -> bool:
        """An agent status message is never shown to anyone: who is live comes from the team's records
        (``assistant_gateway.team_view``)."""

        content = getattr(envelope, "msg_content", None)
        if not isinstance(content, AgentStatus):
            return False
        self._logger.debug("Ignored an agent status message. event=gateway_agent_status_message_ignored source=%s "
                           "agent_id=%s", source, content.self_id)
        return True

    async def publish_durable(
        self,
        envelope: MessageEnvelope,
        *,
        routing_key: str,
    ) -> None:
        """Publish one durable manager envelope."""

        if self._outbound_transport is None:
            raise RuntimeError("Gateway is not connected.")
        if self._outbound_runtime is not None and not self._outbound_runtime.connected:
            raise RuntimeError("Gateway is not connected.")
        await self._outbound_transport.publish_durable(
            envelope,
            routing_key=routing_key,
        )

    async def publish_operational(self, envelope: MessageEnvelope) -> None:
        """Publish one operational envelope using its logical target."""

        if self._outbound_transport is None:
            raise RuntimeError("Gateway is not connected.")
        if self._outbound_runtime is not None and not self._outbound_runtime.connected:
            raise RuntimeError("Gateway is not connected.")
        target = (
            envelope.msg_header.to_routing_path or RoutingType.ALL_AGENTS_ROUTE.value
        )
        routing_key = str(getattr(target, "value", target))
        await self._outbound_transport.publish_operational(
            envelope,
            routing_key=routing_key,
        )

    async def put_object(
        self,
        *,
        bucket: str,
        object_name: str,
        path: str,
        ttl_seconds: float | None = None,
        max_bytes: int | None = None,
        headers: dict[str, str] | None = None,
    ) -> object:
        """Store one attachment object through the runtime capability."""

        if self._runtime is None:
            raise RuntimeError(
                "Gateway manager runtime does not provide object storage."
            )
        if not self._runtime.connected:
            await self.connect()
        return await self._runtime.put_object(
            bucket=bucket,
            object_name=object_name,
            path=path,
            ttl_seconds=ttl_seconds,
            max_bytes=max_bytes,
            headers=headers,
        )

    def _parse_status_timestamp(
        self,
        status: AgentStatus,
        *,
        fallback_timestamp: str | None = None,
    ) -> datetime | None:
        raw_timestamp = status.last_seen_at or fallback_timestamp
        if not raw_timestamp:
            return None
        try:
            parsed = datetime.fromisoformat(raw_timestamp)
        except ValueError:
            self._logger.warning(
                "Gateway could not parse manager status timestamp %r.",
                raw_timestamp,
            )
            return None
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed

    async def apply_manager_status(
        self,
        status: AgentStatus,
        *,
        fallback_timestamp: str | None = None,
        source: str,
    ) -> None:
        """Apply one validated manager status update."""

        if (
            status.status_type != MessageMetaType.RESPONSE
            or status.named_agent_type != self.settings.manager_named_agent_type
            or status.team_id != self.settings.team_id
        ):
            return
        if not status.self_id:
            self._logger.warning(
                "Gateway ignored manager status from %s without self_id.",
                source,
            )
            return

        status_timestamp = self._parse_status_timestamp(
            status,
            fallback_timestamp=fallback_timestamp,
        )
        should_mark_unavailable = False
        transition: GatewayManagerTransition | None = None
        async with self._manager_status_lock:
            if (
                status_timestamp is not None
                and self._manager_status_updated_at is not None
                and status_timestamp < self._manager_status_updated_at
            ):
                self._logger.info(
                    "Gateway ignored stale manager status from %s for %s at %s; "
                    "newer status at %s is already applied.",
                    source,
                    status.self_id,
                    status_timestamp.isoformat(),
                    self._manager_status_updated_at.isoformat(),
                )
                return

            current_manager_id = self._manager_id
            if status.is_exiting:
                if current_manager_id and status.self_id != current_manager_id:
                    self._logger.info(
                        "Gateway ignored shutdown status from non-current manager "
                        "%s; current manager is %s.",
                        status.self_id,
                        current_manager_id,
                    )
                    return
                if status_timestamp is not None:
                    self._manager_status_updated_at = status_timestamp
                should_mark_unavailable = True
            else:
                changed = current_manager_id != status.self_id
                became_available = current_manager_id is None
                if changed:
                    await self._set_manager_id_locked(status.self_id)
                    transition = GatewayManagerTransition(
                        manager_id=status.self_id,
                        available=True,
                        source=source,
                        reason="live manager status",
                        became_available=became_available,
                    )
                    if current_manager_id:
                        self._logger.warning(
                            "Gateway manager routing refreshed from %s to %s via %s.",
                            current_manager_id,
                            status.self_id,
                            source,
                        )
                    else:
                        self._logger.info(
                            "Gateway discovered live manager %s via %s.",
                            status.self_id,
                            source,
                        )
                if status_timestamp is not None:
                    self._manager_status_updated_at = status_timestamp
                elif changed:
                    self._manager_status_updated_at = self._clock.now_utc()

        if transition is not None:
            await self._run_transition_handler(
                self._on_manager_available,
                transition,
            )
        if should_mark_unavailable:
            await self.mark_manager_unavailable(
                expected_manager_id=status.self_id,
                status_timestamp=status_timestamp,
                reason=f"shutdown status from {source}",
                source=source,
            )

    async def mark_manager_unavailable(
        self,
        *,
        expected_manager_id: str | None = None,
        status_timestamp: datetime | None = None,
        reason: str,
        source: str = "liveness",
    ) -> None:
        """Clear the current route and emit one unavailable transition."""

        async with self._manager_status_lock:
            current_manager_id = self._manager_id
            if (
                expected_manager_id is not None
                and current_manager_id != expected_manager_id
            ):
                self._logger.info(
                    "Gateway skipped manager-unavailable transition for %s "
                    "because current manager is %s (%s).",
                    expected_manager_id,
                    current_manager_id,
                    reason,
                )
                return
            if current_manager_id is None:
                return
            await self._set_manager_id_locked(None)
            self._manager_status_updated_at = status_timestamp or self._clock.now_utc()

        self._logger.warning(
            "Gateway marked manager %s unavailable (%s).",
            current_manager_id,
            reason,
        )
        await self._run_transition_handler(
            self._on_manager_unavailable,
            GatewayManagerTransition(
                manager_id=current_manager_id,
                available=False,
                source=source,
                reason=reason,
            ),
        )

    async def _set_manager_id_locked(self, manager_id: str | None) -> None:
        self._manager_id = manager_id
        if self._write_manager_route is None:
            return
        result = self._write_manager_route(manager_id)
        if inspect.isawaitable(result):
            await result

    @staticmethod
    async def _run_transition_handler(
        handler: ManagerTransitionHandler | None,
        transition: GatewayManagerTransition,
    ) -> None:
        if handler is None:
            return
        result = handler(transition)
        if inspect.isawaitable(result):
            await result
