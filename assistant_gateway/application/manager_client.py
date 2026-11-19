"""Manager-facing gateway client with transport-neutral lifecycle and routing.

A reply wait (``response_channel``) takes the messages for its correlation and
task while it is open. A message that no wait takes goes through
``_nothing_waiting_for_response`` or ``_nothing_waiting_for_operational``. What
is still queued when a wait closes is passed on through the same path, as if
it had arrived after the close (``reply_leftovers``).
"""

from __future__ import annotations

import asyncio
import inspect
import logging
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import AsyncIterator, Awaitable, Callable

from assistant_gateway.application.ports import (
    GatewayIdGenerator,
    GatewayManagerRuntime,
    GatewayManagerTransport,
)
from assistant_gateway.application.reply_leftovers import ArrivalRoute, ReplyLeftovers, ReplyWait
from custom_messages.contracts.control_v1 import AgentStatus, SystemStatusMessage
from custom_messages.contracts.credentials_v1 import (
    SecureCredentialCollectionRequest,
    SignupAutomationRequest,
    ToolSetupRequestV1,
)
from custom_messages.contracts.delivery_v1 import UserNotification
from custom_messages.contracts.envelope_v1 import GATEWAY_MANAGER_CONTROL_TYPES, MessageEnvelope
from custom_messages.contracts.task_v1 import Task, TaskQuery, TaskSteering
from custom_messages.enums import RoutingType


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
    # A message left on a closed reply wait whose handling fails is tried again
    # the way the response receiver retries a message on arrival: up to this
    # many tries in all, this many seconds apart (reply_leftovers). UserGateway
    # passes the transport's values; the defaults try once.
    leftover_attempts: int = 1
    leftover_retry_seconds: float = 0.0


@dataclass(frozen=True)
class GatewayManagerTransition:
    """One manager-route availability transition."""

    manager_id: str
    available: bool
    source: str
    reason: str
    became_available: bool = False
    # Waiting replies this unavailable transition must not fail: a lead whose process runs another message
    # contract still finishes the tasks it holds, and these are its people's replies. The lead follower
    # (assistant_gateway.team_view.TeamLeadFollower) names them and fails them once that lead leaves the records.
    kept_replies: frozenset[str] = frozenset()


class GatewayManagerClient:
    """Own manager transport lifecycle, correlation, and the team lead's route.

    Which agent leads, and whether it is live, comes from the team's agent records: the
    team view's lead follower (``assistant_gateway.team_view.TeamLeadFollower``) is the
    only caller of ``set_lead`` and ``mark_manager_unavailable`` while the gateway runs.
    Before message contract step 7c this client discovered the lead on the operational
    subject, ordered presence reports by their timestamps and polled the lead's liveness.
    """

    def __init__(
        self,
        *,
        settings: GatewayManagerClientSettings,
        id_generator: GatewayIdGenerator,
        logger: logging.Logger,
        runtime: GatewayManagerRuntime | None = None,
        outbound_transport: GatewayManagerTransport | None = None,
        on_unsolicited: EnvelopeHandler | None = None,
        on_manager_available: ManagerTransitionHandler | None = None,
        on_manager_unavailable: ManagerTransitionHandler | None = None,
        on_leftover_lost: EnvelopeHandler | None = None,
        write_manager_route: ManagerRouteWriter | None = None,
        trace_enabled: TraceEnabled | None = None,
    ) -> None:
        self.settings = settings
        self._id_generator = id_generator
        self._logger = logger
        self._runtime = runtime
        self._outbound_transport = outbound_transport or runtime
        self._outbound_runtime = runtime if outbound_transport is None else None
        self._on_unsolicited = on_unsolicited
        self._on_manager_available = on_manager_available
        self._on_manager_unavailable = on_manager_unavailable
        self._on_leftover_lost = on_leftover_lost
        self._write_manager_route = write_manager_route
        self._trace_enabled = trace_enabled or (lambda: False)
        self._manager_id = settings.initial_manager_id
        self._lifecycle_connected = False
        self._manager_status_lock = asyncio.Lock()
        # A schedule action keeps its originating request's correlation.
        # Distinct scheduled tasks can therefore wait for replies at once.
        self._response_queues: dict[tuple[str, str | None], ReplyWait] = {}
        # What a closing wait leaves on its queue gets the handling it would
        # have had arriving after the close, by the way it came in.
        self._leftovers = ReplyLeftovers(
            handlers={
                ArrivalRoute.RESPONSE: self._nothing_waiting_for_response,
                ArrivalRoute.OPERATIONAL: self._nothing_waiting_for_operational,
            },
            # Only a response-route message can be a request a task waits on;
            # an operational one is never shown, so nothing waits on it.
            lost_handlers={ArrivalRoute.RESPONSE: self._response_leftover_lost},
            attempts=settings.leftover_attempts,
            retry_seconds=settings.leftover_retry_seconds,
            logger=logger,
        )
        self._control_queues: dict[str, asyncio.Queue[MessageEnvelope]] = {}

    @property
    def manager_id(self) -> str | None:
        """Return the current live manager route, if one is known."""

        return self._manager_id

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
        on_leftover_lost: EnvelopeHandler,
    ) -> None:
        """Bind the composed application handlers before connecting the runtime."""

        if self._lifecycle_connected:
            raise RuntimeError(
                "Cannot replace handlers on a connected manager runtime."
            )
        self._on_unsolicited = on_unsolicited
        self._on_manager_available = on_manager_available
        self._on_manager_unavailable = on_manager_unavailable
        self._on_leftover_lost = on_leftover_lost

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
        """Close the runtime connection, after stopping the passing on of leftovers."""

        await self.stop_leftover_passes()
        if self._runtime is not None:
            await self._runtime.close()
        self._lifecycle_connected = False

    async def stop_leftover_passes(self) -> None:
        """Cancel passing on leftovers and refuse later ones (shutdown).

        Each leftover not passed on is logged, and a setup, form or signup request
        among them is refused to its task while the connection is still open.
        """

        await self._leftovers.stop()

    @asynccontextmanager
    async def response_channel(
        self,
        correlation_id: str,
        *,
        task_id: str | None = None,
    ) -> AsyncIterator[asyncio.Queue[MessageEnvelope]]:
        """Register one terminal-response correlation for a bounded operation.

        However the operation ends, messages still queued when it closes are
        passed on (``unregister_response_channel``, ``reply_leftovers``).
        """

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
        registered = ReplyWait(queue or asyncio.Queue())
        self._response_queues[key] = registered
        return registered.queue

    def unregister_response_channel(
        self,
        correlation_id: str,
        *,
        task_id: str | None = None,
        queue: asyncio.Queue[MessageEnvelope] | None = None,
    ) -> None:
        """Remove a response queue without deleting a newer replacement, and pass on what it still holds.

        This is the one place a reply wait is removed, and ``response_channel``
        always calls it, so the leftovers are passed on however the wait ended.
        """

        key = (correlation_id, task_id)
        current = self._response_queues.get(key)
        if current is None or (queue is not None and current.queue is not queue):
            return
        del self._response_queues[key]
        # Drained in the same step as the removal: a message is either drained
        # here or arrives after it and finds no wait.
        self._leftovers.pass_on(
            correlation_id=correlation_id, task_id=task_id, leftovers=current.drain()
        )

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
            return self._response_queues[(correlation_id, task_id)].queue
        except KeyError as exc:
            raise RuntimeError(
                f"Response correlation {correlation_id!r} for task {task_id!r} is not registered."
            ) from exc

    def _response_targets(
        self, correlation_id: str, envelope: MessageEnvelope
    ) -> tuple[ReplyWait, ...]:
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
            # Every wait of the correlation gets it, and each wait passes its own
            # unread copy on when it closes (reply_leftovers). No production
            # sender reaches two waits today: control answers use a fresh correlation.
            return tuple(wait for (correlation, _), wait in self._response_queues.items() if correlation == correlation_id)
        wait = self._response_queues.get((correlation_id, task_id))
        if wait is None and task_id is not None:
            wait = self._response_queues.get((correlation_id, None))
        return (wait,) if wait is not None else ()

    async def queue_response(
        self,
        correlation_id: str,
        envelope: MessageEnvelope,
        *,
        route: ArrivalRoute,
    ) -> bool:
        """Queue a synthetic or inbound response for an active correlation.

        ``route`` is how the message came in. It has no default: it decides what
        happens to the message if its wait closes before reading it
        (``reply_leftovers``), and a wrong default would drop a team lead's
        message as the gateway's own notice.
        """

        waits = self._response_targets(correlation_id, envelope)
        for wait in waits:
            await wait.put(envelope, route)
        return bool(waits)

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
        if correlation_id and await self.queue_response(correlation_id, envelope, route=ArrivalRoute.RESPONSE):
            return
        # Go after the request's leftovers, so this message cannot overtake them.
        # This route is handled one message at a time, so meanwhile every chat's
        # replies wait too, at most LEFTOVER_ORDER_WAIT_SECONDS (reply_leftovers).
        await self._leftovers.passed_on(correlation_id)
        await self._nothing_waiting_for_response(envelope)

    async def _nothing_waiting_for_response(self, envelope: MessageEnvelope) -> None:
        """A response-route message no reply wait takes: on arrival, or left on a closed wait (reply_leftovers)."""

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
        if correlation_id and await self.queue_response(correlation_id, envelope, route=ArrivalRoute.OPERATIONAL):
            return
        await self._nothing_waiting_for_operational(envelope)

    async def _response_leftover_lost(self, envelope: MessageEnvelope) -> None:
        """A response-route leftover given up on or cut off by shutdown: its task is told (reply_leftovers)."""

        if self._on_leftover_lost is not None:
            await self._on_leftover_lost(envelope)

    async def _nothing_waiting_for_operational(self, envelope: MessageEnvelope) -> None:
        """An operational message no reply wait takes: on arrival, or left on a closed wait (reply_leftovers)."""

        if self._drop_agent_status(envelope, source="operational"):
            return
        self._logger.debug(
            "Gateway manager client dropped unmatched operational response "
            "type=%s correlation_id=%s.",
            type(getattr(envelope, "msg_content", None)).__name__,
            getattr(getattr(envelope, "msg_meta", None), "msg_correlation_id", None),
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

    async def set_lead(self, lead_id: str, *, source: str) -> None:
        """Route people's chats to ``lead_id``, the team lead the team's records name, and emit one available
        transition (``became_available`` when there was no route). The team view's lead follower is its caller
        (``assistant_gateway.team_view.TeamLeadFollower``)."""

        async with self._manager_status_lock:
            previous = self._manager_id
            if previous == lead_id:
                return
            await self._set_manager_id_locked(lead_id)
        # Info, not a warning: after the start the follower marks a lead unavailable before it sets another,
        # so a previous route here is the one saved by the previous run, replaced at every normal team start
        # (agent ids are new at each start; critique C08).
        self._logger.info(
            "The gateway routes chats to the team lead. event=gateway_team_lead_followed manager_id=%s "
            "previous_manager_id=%s source=%s",
            lead_id,
            previous,
            source,
        )
        await self._run_transition_handler(
            self._on_manager_available,
            GatewayManagerTransition(
                manager_id=lead_id,
                available=True,
                source=source,
                reason="the team lead is live in the team's records",
                became_available=previous is None,
            ),
        )

    async def mark_manager_unavailable(
        self,
        *,
        expected_manager_id: str | None = None,
        reason: str,
        source: str = "liveness",
        kept_replies: frozenset[str] = frozenset(),
    ) -> None:
        """Clear the current route and emit one unavailable transition.

        ``kept_replies``: waiting replies a lead on another contract still answers, so this
        transition does not fail them (``GatewayManagerTransition.kept_replies``).
        """

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

        self._logger.warning(
            "The gateway marked the team lead unavailable. event=gateway_manager_unavailable manager_id=%s "
            "reason=%s source=%s kept_replies=%d",
            current_manager_id,
            reason,
            source,
            len(kept_replies),
        )
        await self._run_transition_handler(
            self._on_manager_unavailable,
            GatewayManagerTransition(
                manager_id=current_manager_id,
                available=False,
                source=source,
                reason=reason,
                kept_replies=kept_replies,
            ),
        )

    async def _set_manager_id_locked(self, manager_id: str | None) -> None:
        """Change the route, then save it. The route in memory is the one the gateway uses while it runs, so a
        save that fails (a full disk, a read-only mount, a saved file another writer left invalid) is logged and
        the transition still runs: the lead follower would see no difference to repair (critique C03)."""

        self._manager_id = manager_id
        if self._write_manager_route is None:
            return
        try:
            result = self._write_manager_route(manager_id)
            if inspect.isawaitable(result):
                await result
        except Exception as error:
            self._logger.error(
                "The route to the team lead could not be saved; the gateway uses it while it runs and saves it "
                "again at the next change. event=gateway_manager_route_save_failed manager_id=%s error_type=%s",
                manager_id,
                type(error).__name__,
                exc_info=error,
            )

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
