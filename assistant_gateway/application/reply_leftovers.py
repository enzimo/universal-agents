"""Pass on the messages still queued when a reply wait closes.

The gateway waits for the team lead's answer to a request on a reply wait
(``GatewayManagerClient.response_channel``). The wait ends at the first
message that answers it (``GatewayResponseCoordinator``: a reply, a question,
a setup page, a form, a notification or a status), but the team lead may
already have sent more for the same request. ``set_up_tools`` sends all of a
task's setup pages at once, and while the gateway opens the first one (broker
calls of up to 15 seconds) the others are queued on the same wait. Before,
closing the wait discarded them: no page record, no card, no result to the
task and no log line, so the setup run waited forever (review of 2026-11-16).

Now every message still queued when a wait closes gets the handling it would
have had if it had arrived just after the close, whichever way the wait ended
(answered, timed out, cancelled or failed). That handling depends on how the
message came in (``ArrivalRoute``):

- the response route: the gateway's "nothing is waiting for this" path, the
  same code a message that matched no wait takes. A late setup page is opened
  and its card listed where the chat shows late pages, otherwise it is
  refused and the task is sent a failure. Credential forms fail closed.
  Replies, questions, notifications and statuses are delivered, and a late
  guidance answer settles its held copy.
- the operational subject: an agent status is applied, and anything else is
  dropped, as on arrival. Agents' messages to each other carry the request's
  correlation too, and are never shown to a person.
- the gateway's own notice (``queue_response`` with ``ArrivalRoute.GATEWAY``):
  not passed on. Had it come after the close, nothing would have queued it.

What already happened when the message first arrived (the agent presence
observation and the trace line in ``handle_response``) is not repeated.

The leftovers are passed on oldest first, in a task of their own that starts
when the wait closes. Running there means a cancelled or timed-out waiter is
not held up by broker calls in its cleanup, and a second cancellation cannot
cut a half-opened page short. It also means the waiter's caller handles the
reply that ended the wait first. Interactive submission records that reply in
the chat history before it next yields to the event loop, so the history
keeps the order the team lead sent.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass, field
from enum import StrEnum

from custom_messages.contracts.control_v1 import AgentStatus
from custom_messages.contracts.envelope_v1 import MessageEnvelope


class ArrivalRoute(StrEnum):
    """How a message reached a reply wait; it decides what happens to it if it is left over."""

    # The gateway's response route (GatewayManagerClient.handle_response).
    RESPONSE = "response"
    # The operational subject (GatewayManagerClient.handle_operational).
    OPERATIONAL = "operational"
    # The gateway's own message for the wait, such as the "team lead unavailable" notice.
    GATEWAY = "gateway"


LeftoverHandler = Callable[[MessageEnvelope], Awaitable[None]]

# Why a leftover was not passed on (event=gateway_reply_leftover_not_passed_on reason=...).
NOT_PASSED_ON_GATEWAY_NOTICE = "gateway_notice"
# Anomaly: a message put into the queue without recording how it came in (only
# a test putting into the queue directly does this).
NOT_PASSED_ON_ARRIVAL_UNKNOWN = "arrival_unknown"


@dataclass(frozen=True)
class Leftover:
    """One message still queued when its wait closed, and how it came in (None: not recorded)."""

    envelope: MessageEnvelope
    route: ArrivalRoute | None


@dataclass
class ReplyWait:
    """One open reply wait: the queue its caller reads, and how each queued message came in."""

    queue: asyncio.Queue[MessageEnvelope]
    # id(envelope) -> (envelope, route). Holding the envelope keeps its id from
    # being reused while it is recorded. The record lives only as long as the
    # wait, which its caller bounds (request, guidance or discovery timeout).
    _arrivals: dict[int, tuple[MessageEnvelope, ArrivalRoute]] = field(
        default_factory=dict, init=False, repr=False
    )

    async def put(self, envelope: MessageEnvelope, route: ArrivalRoute) -> None:
        """Queue a message for the wait; the route is recorded before the message can be read."""

        self._arrivals[id(envelope)] = (envelope, route)
        await self.queue.put(envelope)

    def drain(self) -> tuple[Leftover, ...]:
        """Empty the queue, oldest first, pairing each message with how it came in.

        Called in the same synchronous step that removes the wait, so a message is
        either drained here or arrives after the removal and finds no wait.
        """

        leftovers: list[Leftover] = []
        while not self.queue.empty():
            envelope = self.queue.get_nowait()
            recorded = self._arrivals.get(id(envelope))
            leftovers.append(Leftover(envelope, recorded[1] if recorded is not None else None))
        self._arrivals.clear()
        return tuple(leftovers)


class ReplyLeftovers:
    """Pass on what closed reply waits left behind (module docstring)."""

    def __init__(
        self,
        *,
        handlers: Mapping[ArrivalRoute, LeftoverHandler],
        logger: logging.Logger,
    ) -> None:
        # The "nothing is waiting for this" path per route; a route without one is not passed on.
        self._handlers = dict(handlers)
        self._logger = logger
        # Strong references: asyncio keeps only weak references to running tasks.
        self._running: set[asyncio.Task[None]] = set()

    def pass_on(
        self,
        *,
        correlation_id: str,
        task_id: str | None,
        leftovers: Sequence[Leftover],
    ) -> None:
        """Start passing on one closed wait's leftovers in a task of their own."""

        if not leftovers:
            return
        task = asyncio.create_task(
            self._run(correlation_id, task_id, tuple(leftovers)),
            name=f"gateway_reply_leftovers:{correlation_id}",
        )
        self._running.add(task)
        task.add_done_callback(self._running.discard)

    async def _run(
        self,
        correlation_id: str,
        task_id: str | None,
        leftovers: tuple[Leftover, ...],
    ) -> None:
        # Oldest first, one at a time: the order the team lead sent them.
        for leftover in leftovers:
            await self._pass_one(correlation_id, task_id, leftover)

    async def _pass_one(
        self,
        correlation_id: str,
        task_id: str | None,
        leftover: Leftover,
    ) -> None:
        handler = self._handlers.get(leftover.route) if leftover.route is not None else None
        if handler is None:
            reason = NOT_PASSED_ON_ARRIVAL_UNKNOWN if leftover.route is None else NOT_PASSED_ON_GATEWAY_NOTICE
            self._logger.log(
                logging.WARNING if leftover.route is None else logging.INFO,
                "Did not pass on a message left on a closed reply wait. "
                "event=gateway_reply_leftover_not_passed_on reason=%s " + _FIELDS,
                reason,
                *_fields(correlation_id, task_id, leftover),
            )
            return
        # Every agent answers the gateway's discovery request and only the first
        # answer ends that wait, so a leftover agent status is routine.
        routine = isinstance(leftover.envelope.msg_content, AgentStatus)
        self._logger.log(
            logging.DEBUG if routine else logging.INFO,
            "Passing on a message left on a closed reply wait. "
            "event=gateway_reply_leftover_passed_on " + _FIELDS,
            *_fields(correlation_id, task_id, leftover),
        )
        try:
            await handler(leftover.envelope)
        except Exception:
            # One failure must not lose the leftovers after it.
            self._logger.exception(
                "Could not pass on a message left on a closed reply wait. "
                "event=gateway_reply_leftover_failed " + _FIELDS,
                *_fields(correlation_id, task_id, leftover),
            )


_FIELDS = "correlation_id=%s task_id=%s message_id=%s content_type=%s content_id=%s route=%s"


def _fields(correlation_id: str, task_id: str | None, leftover: Leftover) -> tuple[str, ...]:
    """The identifiers each leftover log line carries; never the message's text or a page link."""

    content = leftover.envelope.msg_content
    return (
        correlation_id,
        task_id or "-",
        leftover.envelope.msg_header.message_id,
        type(content).__name__,
        _content_id(content),
        leftover.route.value if leftover.route is not None else "-",
    )


def _content_id(content: object) -> str:
    """The content's own id (for a setup page its ``setup_request_id``), or "-" when it has none."""

    get_id = getattr(content, "get_id", None)
    return str(get_id()) if callable(get_id) else "-"
