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
from custom_messages.contracts.control_v1 import AgentStatus
from custom_messages.contracts.envelope_v1 import GATEWAY_MANAGER_CONTROL_TYPES, MessageEnvelope
from custom_messages.contracts.routing_v1 import MessageHeader, MessageMeta
from custom_messages.enums import ControlType, MessageMetaType, RequesterType, RoutingType


EnvelopeHandler = Callable[[MessageEnvelope], Awaitable[None]]
ManagerRouteWriter = Callable[[str | None], Awaitable[None] | None]
ManagerTransitionHandler = Callable[
    ["GatewayManagerTransition"],
    Awaitable[None] | None,
]
TraceEnabled = Callable[[], bool]
AgentPresenceHandler = Callable[
    [AgentStatus, datetime | None, str],
    Awaitable[None] | None,
]
_MANAGER_CONTROL_TYPES = GATEWAY_MANAGER_CONTROL_TYPES
# How often manager discovery repeats its status request (discover_manager_id).
DISCOVERY_RESEND_SECONDS = 1.0


@dataclass(frozen=True)
class GatewayManagerClientSettings:
    """Stable settings needed by the manager-client state machine."""

    instance_name: str
    user_display_name: str
    team_id: str
    manager_named_agent_type: str
    response_route: str
    initial_manager_id: str | None = None
    liveness_poll_seconds: float = 5.0
    presence_stale_seconds: float = 30.0
    recovery_discovery_timeout_seconds: float = 2.0


@dataclass(frozen=True)
class GatewayManagerTransition:
    """One manager-route availability transition."""

    manager_id: str
    available: bool
    source: str
    reason: str
    became_available: bool = False


class GatewayManagerClient:
    """Own manager transport lifecycle, correlation, discovery, and liveness."""

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
        on_agent_presence: AgentPresenceHandler | None = None,
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
        self._on_agent_presence = on_agent_presence
        self._write_manager_route = write_manager_route
        self._trace_enabled = trace_enabled or (lambda: False)
        self._manager_id = settings.initial_manager_id
        self._lifecycle_connected = False
        self._manager_status_updated_at: datetime | None = None
        self._manager_status_lock = asyncio.Lock()
        self._response_queues: dict[str, asyncio.Queue[MessageEnvelope]] = {}
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

        return tuple(self._response_queues)

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
        on_agent_presence: AgentPresenceHandler,
    ) -> None:
        """Bind the composed application handlers before connecting the runtime."""

        if self._lifecycle_connected:
            raise RuntimeError(
                "Cannot replace handlers on a connected manager runtime."
            )
        self._on_unsolicited = on_unsolicited
        self._on_manager_available = on_manager_available
        self._on_manager_unavailable = on_manager_unavailable
        self._on_agent_presence = on_agent_presence

    async def connect(self) -> None:
        """Connect the runtime and install transport-neutral inbound handlers."""

        if self._runtime is None:
            raise RuntimeError("Gateway manager runtime is not configured.")
        handlers: dict[str, EnvelopeHandler] = {
            "on_response": self.handle_response,
            "on_operational": self.handle_operational,
            "on_presence": self.handle_presence,
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
    ) -> AsyncIterator[asyncio.Queue[MessageEnvelope]]:
        """Register one terminal-response correlation for a bounded operation."""

        queue = self.register_response_channel(correlation_id)
        try:
            yield queue
        finally:
            self.unregister_response_channel(correlation_id, queue=queue)

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
        queue: asyncio.Queue[MessageEnvelope] | None = None,
    ) -> asyncio.Queue[MessageEnvelope]:
        """Register and return a response queue for a correlation."""

        if correlation_id in self._response_queues:
            raise RuntimeError(
                f"Response correlation {correlation_id!r} is already registered."
            )
        registered = queue or asyncio.Queue()
        self._response_queues[correlation_id] = registered
        return registered

    def unregister_response_channel(
        self,
        correlation_id: str,
        *,
        queue: asyncio.Queue[MessageEnvelope] | None = None,
    ) -> None:
        """Remove a response queue without deleting a newer replacement."""

        current = self._response_queues.get(correlation_id)
        if queue is None or current is queue:
            self._response_queues.pop(correlation_id, None)

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

        return correlation_id in self._response_queues

    def pending_response_correlation_ids(self) -> tuple[str, ...]:
        """Requests the gateway is still waiting on the agent to answer."""

        return tuple(self._response_queues)

    def get_response_channel(
        self,
        correlation_id: str,
    ) -> asyncio.Queue[MessageEnvelope]:
        """Return the registered response queue for a correlation."""

        try:
            return self._response_queues[correlation_id]
        except KeyError as exc:
            raise RuntimeError(
                f"Response correlation {correlation_id!r} is not registered."
            ) from exc

    async def queue_response(
        self,
        correlation_id: str,
        envelope: MessageEnvelope,
    ) -> bool:
        """Queue a synthetic or inbound response for an active correlation."""

        queue = self._response_queues.get(correlation_id)
        if queue is None:
            return False
        await queue.put(envelope)
        return True

    async def handle_response(self, envelope: MessageEnvelope) -> None:
        """Route one inbound response envelope by correlation."""

        await self._notify_agent_presence(envelope, source="response")
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
            correlation_id and correlation_id in self._response_queues
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
        if correlation_id and correlation_id in self._response_queues:
            await self._response_queues[correlation_id].put(envelope)
            return
        if await self._handle_unmatched_status(envelope, source="response"):
            return
        if self._on_unsolicited is not None:
            await self._on_unsolicited(envelope)

    async def handle_operational(self, envelope: MessageEnvelope) -> None:
        """Route one inbound operational envelope by correlation."""

        await self._notify_agent_presence(envelope, source="operational")
        header = getattr(envelope, "msg_header", None)
        if getattr(header, "from_agent_id", None) == self.settings.response_route:
            return
        meta = getattr(envelope, "msg_meta", None)
        correlation_id = getattr(meta, "msg_correlation_id", None)
        if correlation_id and correlation_id in self._control_queues:
            await self._control_queues[correlation_id].put(envelope)
            return
        if correlation_id and correlation_id in self._response_queues:
            await self._response_queues[correlation_id].put(envelope)
            return
        if await self._handle_unmatched_status(envelope, source="operational"):
            return
        self._logger.debug(
            "Gateway manager client dropped unmatched operational response "
            "type=%s correlation_id=%s.",
            type(getattr(envelope, "msg_content", None)).__name__,
            correlation_id,
        )

    async def handle_presence(self, envelope: MessageEnvelope) -> None:
        """Apply one replayable manager presence envelope."""

        await self._notify_agent_presence(envelope, source="presence")
        content = getattr(envelope, "msg_content", None)
        if isinstance(content, AgentStatus):
            await self.apply_manager_status(
                content,
                fallback_timestamp=getattr(
                    getattr(envelope, "msg_header", None),
                    "timestamp",
                    None,
                ),
                source="presence",
            )

    async def _notify_agent_presence(
        self,
        envelope: MessageEnvelope,
        *,
        source: str,
    ) -> None:
        """Publish one validated status observation to the gateway registry."""

        if self._on_agent_presence is None:
            return
        content = getattr(envelope, "msg_content", None)
        if not isinstance(content, AgentStatus):
            return
        fallback_timestamp = getattr(
            getattr(envelope, "msg_header", None),
            "timestamp",
            None,
        )
        status_timestamp = self._parse_status_timestamp(
            content,
            fallback_timestamp=fallback_timestamp,
        )
        try:
            result = self._on_agent_presence(content, status_timestamp, source)
            if inspect.isawaitable(result):
                await result
        except Exception:
            self._logger.exception(
                "Gateway agent-presence observer failed. source=%s agent_id=%s",
                source,
                content.self_id,
            )

    async def _handle_unmatched_status(
        self,
        envelope: MessageEnvelope,
        *,
        source: str,
    ) -> bool:
        content = getattr(envelope, "msg_content", None)
        if not isinstance(content, AgentStatus):
            return False
        await self.apply_manager_status(
            content,
            fallback_timestamp=getattr(
                getattr(envelope, "msg_header", None),
                "timestamp",
                None,
            ),
            source=f"{source}_unmatched",
        )
        meta = getattr(envelope, "msg_meta", None)
        log_fn = self._logger.info if self._trace_enabled() else self._logger.debug
        log_fn(
            "event=gateway_operational_agent_status_unmatched_pending_dropped "
            "correlation_id=%s agent_id=%s named_agent_type=%s status_type=%s.",
            getattr(meta, "msg_correlation_id", None),
            content.self_id,
            content.named_agent_type,
            getattr(content.status_type, "value", content.status_type),
        )
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

    async def refresh_manager_id(self) -> None:
        """Revalidate the cached manager route during connection startup."""

        configured_manager_id = self._manager_id
        configured_status_updated_at = self._manager_status_updated_at
        try:
            discovered_manager_id = await self.discover_manager_id()
        except TimeoutError:
            async with self._manager_status_lock:
                current_manager_id = self._manager_id
                current_status_updated_at = self._manager_status_updated_at
                live_status_arrived_during_discovery = (
                    current_manager_id is not None
                    and (
                        current_manager_id != configured_manager_id
                        or current_status_updated_at != configured_status_updated_at
                    )
                )
                if live_status_arrived_during_discovery:
                    self._logger.info(
                        "Gateway discovery timed out during connect, but manager "
                        "route %s was updated by live status; keeping that route.",
                        current_manager_id,
                    )
                    return
                if configured_manager_id is not None:
                    await self._set_manager_id_locked(None)
                    self._manager_status_updated_at = self._clock.now_utc()
                    self._logger.warning(
                        "Gateway did not find a live manager during connect; "
                        "cleared stale cached route %s.",
                        configured_manager_id,
                    )
                else:
                    self._logger.info(
                        "Gateway did not find a live manager during connect; "
                        "starting without a manager route."
                    )
            return

        if configured_manager_id == discovered_manager_id:
            self._logger.info(
                "Gateway manager routing confirmed for %s.",
                discovered_manager_id,
            )
            return
        async with self._manager_status_lock:
            current_manager_id = self._manager_id
            if current_manager_id == discovered_manager_id:
                return
            await self._set_manager_id_locked(discovered_manager_id)
        if configured_manager_id:
            self._logger.warning(
                "Gateway manager routing refreshed from stale agent id %s to "
                "live agent id %s.",
                configured_manager_id,
                discovered_manager_id,
            )
        else:
            self._logger.info(
                "Gateway discovered live manager agent id %s.",
                discovered_manager_id,
            )

    async def discover_manager_id(self, timeout_seconds: float = 15.0) -> str:
        """Discover the live team manager through an operational status request.

        The request is sent again every ``DISCOVERY_RESEND_SECONDS`` until the
        timeout: at startup the manager starts together with the gateway and
        misses the first request, which made every restart wait the whole
        timeout (about 13 seconds) even after the manager was up. A manager
        that announced itself through its live status meanwhile
        (``apply_manager_status``) ends the wait as well.
        """

        correlation_id = self._id_generator.new_correlation_id()
        known_at_start = (self._manager_id, self._manager_status_updated_at)
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout_seconds
        async with self.response_channel(correlation_id) as discovery_queue:
            envelope = self._discovery_request(correlation_id)
            while True:
                await self.publish_operational(envelope)
                answered = await self._discovery_answer(
                    discovery_queue, until=min(deadline, loop.time() + DISCOVERY_RESEND_SECONDS)
                )
                if answered is not None:
                    return answered
                announced = self._manager_id
                if announced is not None and (announced, self._manager_status_updated_at) != known_at_start:
                    return announced
                if loop.time() >= deadline:
                    raise TimeoutError(f"Timed out waiting for {self.settings.manager_named_agent_type} discovery.")

    def _discovery_request(self, correlation_id: str) -> MessageEnvelope:
        status_msg = AgentStatus(
            self_id=self.settings.response_route,
            status_type=MessageMetaType.REQUEST,
            info_type=ControlType.STATUS_REQUEST_BASIC,
            named_agent_type="user_gateway",
            is_paused=False,
            is_executing=False,
            has_execution_capacity=True,
            is_busy=False,
            name="user_gateway",
            agent_role="user_gateway",
        )
        return MessageEnvelope(
            msg_content=status_msg,
            msg_meta=MessageMeta(
                from_agent_name=self.settings.user_display_name,
                msg_correlation_id=correlation_id,
                requester_type=RequesterType.USER_REQUESTER,
                is_response_requested=True,
            ),
            msg_header=MessageHeader(
                from_agent_id=self.settings.response_route,
                to_routing_path=RoutingType.ALL_AGENTS_ROUTE,
                response_routing_key=self.settings.response_route,
                agent_signature="",
            ),
        )

    async def _discovery_answer(self, queue: asyncio.Queue[MessageEnvelope], *, until: float) -> str | None:
        """The manager id from the first matching status answer before ``until``, or None."""

        loop = asyncio.get_running_loop()
        while (remaining := until - loop.time()) > 0:
            try:
                response = await asyncio.wait_for(queue.get(), timeout=remaining)
            except asyncio.TimeoutError:
                return None
            content = response.msg_content
            if (
                not isinstance(content, AgentStatus)
                or content.named_agent_type != self.settings.manager_named_agent_type
                or content.team_id != self.settings.team_id
                or not content.self_id
            ):
                continue
            status_timestamp = self._parse_status_timestamp(
                content,
                fallback_timestamp=getattr(response.msg_header, "timestamp", None),
            )
            if status_timestamp is not None:
                self._manager_status_updated_at = status_timestamp
            return content.self_id
        return None

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

    async def discover_manager_recovery_once(self) -> None:
        """Attempt one bounded recovery discovery when no manager is known."""

        if not self.connected:
            return
        try:
            discovered_manager_id = await self.discover_manager_id(
                timeout_seconds=self.settings.recovery_discovery_timeout_seconds
            )
        except TimeoutError:
            self._logger.debug(
                "Gateway manager recovery discovery timed out; manager route "
                "remains unavailable."
            )
            return
        except Exception as exc:
            self._logger.warning(
                "Gateway manager recovery discovery failed: %s",
                exc,
            )
            return

        async with self._manager_status_lock:
            if self._manager_id is not None:
                return
            await self._set_manager_id_locked(discovered_manager_id)
            self._manager_status_updated_at = self._clock.now_utc()

        self._logger.info(
            "Gateway rediscovered live manager %s via recovery discovery.",
            discovered_manager_id,
        )
        await self._run_transition_handler(
            self._on_manager_available,
            GatewayManagerTransition(
                manager_id=discovered_manager_id,
                available=True,
                source="recovery_discovery",
                reason="manager recovered",
                became_available=True,
            ),
        )

    async def manager_has_live_consumer(
        self,
        manager_id: str,
    ) -> bool | None:
        """Probe manager route liveness through the runtime when supported."""

        if self._runtime is None:
            return None
        return await self._runtime.manager_has_live_consumer(manager_id)

    async def check_manager_liveness_once(self) -> None:
        """Run one manager liveness/recovery iteration."""

        manager_id = self._manager_id
        if not manager_id:
            await self.discover_manager_recovery_once()
            return
        has_live_consumer = await self.manager_has_live_consumer(manager_id)
        if has_live_consumer is False:
            await self.mark_manager_unavailable(
                expected_manager_id=manager_id,
                reason="broker liveness probe observed no active consumer",
                source="consumer_probe",
            )
            return
        if has_live_consumer is None and self._manager_status_updated_at is not None:
            age_seconds = (
                self._clock.now_utc() - self._manager_status_updated_at
            ).total_seconds()
            if age_seconds > self.settings.presence_stale_seconds:
                await self.mark_manager_unavailable(
                    expected_manager_id=manager_id,
                    reason=(f"manager presence heartbeat stale for {age_seconds:.1f}s"),
                    source="presence_timeout",
                )

    async def manager_liveness_loop(
        self,
        stop_event: asyncio.Event,
    ) -> None:
        """Run manager liveness checks until the composition root stops."""

        while not stop_event.is_set():
            try:
                await self.check_manager_liveness_once()
                await asyncio.sleep(self.settings.liveness_poll_seconds)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self._logger.warning(
                    "Gateway manager-client liveness loop error: %s",
                    exc,
                )
                await asyncio.sleep(self.settings.liveness_poll_seconds)

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
