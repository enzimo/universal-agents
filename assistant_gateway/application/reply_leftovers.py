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

Order. Passes of one request run one after another: a pass started by a later
close waits for the earlier one. A message that arrives after the close and
that no wait takes waits for its request's leftovers before it is handled
(``GatewayManagerClient.handle_response``), so it cannot overtake them. The
gateway's response route is handled one message at a time, so while such a
message waits, replies for every other chat wait behind it, as they do while a
message is handled on arrival. The wait therefore gives up after
``LEFTOVER_ORDER_WAIT_SECONDS`` with a warning, and the message goes on out of
order rather than hold every person's replies. Later messages for the request
then do not wait for that pass again: one has already gone ahead of it. Nor do
they wait while a leftover is tried again after a failure (see "Failures"): it
has lost its place, as a message that fails on arrival does. So one pass holds
the route at most once, for at most the bound. Two cases are not ordered,
because nothing a person sees depends on it:

- a message that a newly opened wait of the same request takes goes to that
  wait at once (another consumer);
- an operational message that no wait takes only updates the team lead's
  status (ordered by its own timestamp) or is dropped.

Waiting on a pass never links the tasks (``asyncio.wait``): cancelling a held
message leaves the pass running, and a cancelled or failed pass raises nothing
into the message's handling.

Failures. A leftover whose handling raises is tried again the way the
response receiver retries a message on arrival: up to the transport's
``max_deliver`` tries, ``HANDLING_RETRY_DELAY_SECONDS`` apart
(``assistant_gateway.nats_manager_runtime``, passed in through
``GatewayManagerClientSettings``). The pass holds the request's later
leftovers meanwhile, so their order among themselves is kept; later messages
that no wait takes do not wait for the retries ("Order"). After the last try an error is
logged, and the task is told through the route's lost handler: a setup, form
or signup request is refused to its task, unless its setup page already has a
record (``GatewayResponseCoordinator.fail_lost_credential_request``). The
arrival path instead gives a message that fails on its last delivery to the
dead-letter store; a leftover was already acknowledged, so it has no message
there to set aside.

Shutdown. ``stop`` cancels the running passes and refuses later ones. Every
leftover not passed on, whether its pass was running or had not started, is
logged, and its task is told the same way, while the connection to NATS is
still open: ``UserGateway.close`` stops the passes first, and
``GatewayManagerClient.close`` stops them again, for waits that closed in
between, before it closes the connection. A leftover was acknowledged to NATS
when it was first queued, so one lost to a crash is not delivered again.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass, field
from enum import StrEnum
from functools import partial

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
# The gateway is stopping (ReplyLeftovers.stop).
NOT_PASSED_ON_GATEWAY_STOPPING = "gateway_stopping"

# Why a later message did not wait for its request's leftovers
# (event=gateway_reply_leftover_order_wait_skipped reason=...): one of them is
# being tried again after a failure, or a message already went ahead of them.
ORDER_WAIT_SKIPPED_RETRYING = "retrying"
ORDER_WAIT_SKIPPED_ALREADY_OVERTAKEN = "already_overtaken"

# How long a message that no wait takes waits for its request's leftovers
# before it goes on out of order (module docstring, "Order"). While it waits,
# the response route waits too, so it is kept short. The longest broker call a
# leftover makes is opening a setup page (an account link page allows 15 s; a
# sign-in page 10 s). A leftover signup or form request fails closed without
# calling the broker. The 5 s beyond that cover storage and chat delivery
# around the call.
LEFTOVER_ORDER_WAIT_SECONDS = 20.0


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


@dataclass
class _Pass:
    """One closed wait's leftovers, oldest first, until each is passed on.

    Kept outside the pass's coroutine, so ``stop`` can report the leftovers of a
    pass it cancelled, whether or not the pass had started.
    """

    correlation_id: str
    task_id: str | None
    remaining: list[Leftover]
    # True while its leftover in hand is tried again after a failure: later
    # messages for the request do not wait for it (ReplyLeftovers.passed_on).
    retrying: bool = False
    # True once a later message stopped waiting for this pass and went ahead:
    # its order is already broken, so later messages do not wait for it again.
    overtaken: bool = False


class ReplyLeftovers:
    """Pass on what closed reply waits left behind (module docstring)."""

    def __init__(
        self,
        *,
        handlers: Mapping[ArrivalRoute, LeftoverHandler],
        lost_handlers: Mapping[ArrivalRoute, LeftoverHandler],
        attempts: int,
        retry_seconds: float,
        logger: logging.Logger,
    ) -> None:
        # The "nothing is waiting for this" path per route; a route without one is not passed on.
        self._handlers = dict(handlers)
        # What tells the task about a leftover given up on or cut off by shutdown, per route.
        self._lost_handlers = dict(lost_handlers)
        # The response receiver's own retry policy for a message on arrival.
        self._attempts = max(1, attempts)
        self._retry_seconds = retry_seconds
        self._logger = logger
        # Running passes. Also the strong references asyncio needs: it keeps only weak ones.
        self._passes: dict[asyncio.Task[None], _Pass] = {}
        # Per request (correlation id): its latest pass, which runs after the earlier ones.
        self._latest: dict[str, asyncio.Task[None]] = {}
        # Leftovers of waits that closed after stop(); the next stop() reports them.
        self._refused: list[_Pass] = []
        self._stopped = False

    def pass_on(
        self,
        *,
        correlation_id: str,
        task_id: str | None,
        leftovers: Sequence[Leftover],
    ) -> None:
        """Start passing on one closed wait's leftovers in a task of their own, after the request's earlier ones."""

        if not leftovers:
            return
        batch = _Pass(correlation_id, task_id, list(leftovers))
        if self._stopped:
            for leftover in batch.remaining:
                self._log_stopping(batch, leftover)
            # Their tasks are told by the next stop(), while the connection is still open.
            self._refused.append(batch)
            return
        earlier = self._latest.get(correlation_id)
        task = asyncio.create_task(self._run(batch, earlier), name=f"gateway_reply_leftovers:{correlation_id}")
        self._passes[task] = batch
        self._latest[correlation_id] = task
        task.add_done_callback(partial(self._finished, correlation_id))

    async def passed_on(self, correlation_id: str | None) -> None:
        """Wait until the request's leftovers have been passed on, at most ``LEFTOVER_ORDER_WAIT_SECONDS``.

        Its latest pass ends only after the earlier ones. ``asyncio.wait`` never
        links the tasks: cancelling this caller leaves the pass running, and a
        cancelled or failed pass raises nothing here.
        """

        if not correlation_id or (latest := self._latest.get(correlation_id)) is None:
            return
        passes = [batch for batch in self._passes.values() if batch.correlation_id == correlation_id]
        if (skipped := _order_wait_skipped(passes)) is not None:
            self._logger.debug(
                "Not waiting for a request's leftover messages. "
                "event=gateway_reply_leftover_order_wait_skipped correlation_id=%s reason=%s",
                correlation_id,
                skipped,
            )
            return
        done, _ = await asyncio.wait({latest}, timeout=LEFTOVER_ORDER_WAIT_SECONDS)
        if not done:
            for batch in passes:
                batch.overtaken = True
            self._logger.warning(
                "Stopped waiting for a request's leftover messages; the next message goes on out of order. "
                "event=gateway_reply_leftover_order_wait_expired correlation_id=%s pending=%s waited_seconds=%s",
                correlation_id,
                sum(len(batch.remaining) for batch in passes),
                LEFTOVER_ORDER_WAIT_SECONDS,
            )

    async def stop(self) -> None:
        """Cancel the running passes and refuse later ones for the life of this client (the gateway is stopping).

        Every leftover not passed on, in a pass that was running, had not started
        yet, or was refused since the last call, is logged, and its task is told
        through the lost handlers. The caller still has its connection open. Called
        again before the connection closes, for waits that closed in between.
        """

        self._stopped = True
        cancelled = dict(self._passes)
        for task in cancelled:
            task.cancel()
        if cancelled:
            await asyncio.wait(cancelled)
        refused, self._refused = self._refused, []
        for batch in cancelled.values():
            for leftover in batch.remaining:
                self._log_stopping(batch, leftover)
                await self._report_lost(batch, leftover)
        for batch in refused:
            for leftover in batch.remaining:
                await self._report_lost(batch, leftover)

    async def _run(self, batch: _Pass, earlier: asyncio.Task[None] | None) -> None:
        if earlier is not None:
            await asyncio.wait({earlier})
        # Oldest first, one at a time: the order the team lead sent them. A
        # leftover leaves the list only once handled, so stop() sees the one in hand.
        while batch.remaining:
            await self._pass_one(batch, batch.remaining[0])
            batch.remaining.pop(0)

    def _finished(self, correlation_id: str, task: asyncio.Task[None]) -> None:
        batch = self._passes.pop(task, None)
        if self._latest.get(correlation_id) is task:
            del self._latest[correlation_id]
        if task.cancelled():
            if not self._stopped and batch is not None and batch.remaining:
                # Only stop() cancels a pass; anything else is an anomaly.
                self._logger.error(
                    "Passing on leftover messages was cancelled outside shutdown. "
                    "event=gateway_reply_leftovers_stopped_with_error correlation_id=%s error_type=CancelledError "
                    "remaining=%s",
                    correlation_id,
                    len(batch.remaining),
                )
            return
        # Nothing awaits a pass's result (asyncio.wait does not read it), so an
        # unexpected error is reported here. Handler errors never get this far.
        if (error := task.exception()) is not None:
            self._logger.error(
                "Passing on leftover messages stopped with an error. "
                "event=gateway_reply_leftovers_stopped_with_error correlation_id=%s error_type=%s",
                correlation_id,
                type(error).__name__,
                exc_info=error,
            )

    def _log_stopping(self, batch: _Pass, leftover: Leftover) -> None:
        self._logger.warning(
            "Did not pass on a message left on a closed reply wait. "
            "event=gateway_reply_leftover_not_passed_on reason=%s " + _FIELDS,
            NOT_PASSED_ON_GATEWAY_STOPPING,
            *_fields(batch, leftover),
        )

    async def _pass_one(self, batch: _Pass, leftover: Leftover) -> None:
        handler = self._handlers.get(leftover.route) if leftover.route is not None else None
        if handler is None:
            reason = NOT_PASSED_ON_ARRIVAL_UNKNOWN if leftover.route is None else NOT_PASSED_ON_GATEWAY_NOTICE
            self._logger.log(
                logging.WARNING if leftover.route is None else logging.INFO,
                "Did not pass on a message left on a closed reply wait. "
                "event=gateway_reply_leftover_not_passed_on reason=%s " + _FIELDS,
                reason,
                *_fields(batch, leftover),
            )
            return
        # Every agent answers the gateway's discovery request and only the first
        # answer ends that wait, so a leftover agent status is routine.
        routine = isinstance(leftover.envelope.msg_content, AgentStatus)
        self._logger.log(
            logging.DEBUG if routine else logging.INFO,
            "Passing on a message left on a closed reply wait. "
            "event=gateway_reply_leftover_passed_on " + _FIELDS,
            *_fields(batch, leftover),
        )
        if not await self._handle_with_retries(handler, batch, leftover):
            await self._report_lost(batch, leftover)

    async def _handle_with_retries(self, handler: LeftoverHandler, batch: _Pass, leftover: Leftover) -> bool:
        """Try the handler as the response receiver tries a message on arrival; False once it gave up."""

        error: Exception | None = None
        try:
            for attempt in range(1, self._attempts + 1):
                try:
                    await handler(leftover.envelope)
                    return True
                except Exception as exc:
                    error = exc
                if attempt < self._attempts:
                    batch.retrying = True
                    self._logger.warning(
                        "Could not pass on a message left on a closed reply wait; trying again. "
                        "event=gateway_reply_leftover_failed attempt=%s attempts=%s retry_in_seconds=%s " + _FIELDS,
                        attempt,
                        self._attempts,
                        self._retry_seconds,
                        *_fields(batch, leftover),
                        exc_info=error,
                    )
                    await asyncio.sleep(self._retry_seconds)
        finally:
            batch.retrying = False
        self._logger.error(
            "Gave up passing on a message left on a closed reply wait. "
            "event=gateway_reply_leftover_gave_up attempts=%s " + _FIELDS,
            self._attempts,
            *_fields(batch, leftover),
            exc_info=error,
        )
        return False

    async def _report_lost(self, batch: _Pass, leftover: Leftover) -> None:
        """Tell the task about a leftover that was not passed on, where its route has a way to."""

        handler = self._lost_handlers.get(leftover.route) if leftover.route is not None else None
        if handler is None:
            return
        try:
            await handler(leftover.envelope)
        except Exception:
            self._logger.exception(
                "Could not tell the task about a message that was not passed on. "
                "event=gateway_reply_leftover_lost_report_failed " + _FIELDS,
                *_fields(batch, leftover),
            )


_FIELDS = "correlation_id=%s task_id=%s message_id=%s content_type=%s content_id=%s route=%s"


def _fields(batch: _Pass, leftover: Leftover) -> tuple[str, ...]:
    """The identifiers each leftover log line carries; never the message's text or a page link."""

    content = leftover.envelope.msg_content
    return (
        batch.correlation_id,
        batch.task_id or "-",
        leftover.envelope.msg_header.message_id,
        type(content).__name__,
        _content_id(content),
        leftover.route.value if leftover.route is not None else "-",
    )


def _order_wait_skipped(passes: Sequence[_Pass]) -> str | None:
    """Why a later message need not wait for these passes, or None when it waits.

    A leftover being tried again has lost its place, as a message that fails on
    arrival does; and once a message went ahead of a pass, its order is already
    broken. Either way, waiting would only hold the response route again, so a
    pass holds it at most once, for at most ``LEFTOVER_ORDER_WAIT_SECONDS``.
    """

    if any(batch.retrying for batch in passes):
        return ORDER_WAIT_SKIPPED_RETRYING
    if any(batch.overtaken for batch in passes):
        return ORDER_WAIT_SKIPPED_ALREADY_OVERTAKEN
    return None


def _content_id(content: object) -> str:
    """The content's own id (for a setup page its ``setup_request_id``), or "-" when it has none."""

    get_id = getattr(content, "get_id", None)
    return str(get_id()) if callable(get_id) else "-"
