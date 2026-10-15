"""Pull delivery for every durable JetStream consumer (ADR-019).

Why pull: with push delivery NATS chooses a receiver for each message whether or
not it can act on it. Shared worker queues used push consumers with a deliver
group, so a busy worker received tasks, stored them in its private inbox and
acknowledged them, while other workers sat idle (reproduced 2026-10-04). Here a
receiver asks for its next message only when it can act on it:

- ``FetchPolicy.CONTINUOUS`` (one owner's mailbox) keeps a request open while
  ``ready()`` allows it, so the owner stops asking, and the broker keeps the
  messages, while its local admission is full.
- ``FetchPolicy.WHEN_READY`` (a shared worker queue) asks for one message only
  while ``ready()`` says the worker can start it at once. A message that
  arrives after the worker became busy goes straight back to the queue
  (``nak``), so another worker takes it.

``ensure_pull_consumer`` creates or updates the durable consumer. A consumer left
by a release that used push delivery cannot be changed into a pull consumer in
place, and a work-queue stream refuses two consumers with the same filter, so it
is deleted first (logged). Stored messages stay in the stream and the new
consumer delivers them.

One message must never stop a receiver. The loop awaits each message's handling
before it asks for the next one, so an error that escaped the handling used to
end the loop: the receiver never asked again, and on a shared worker queue the
redelivered message stopped every worker in turn (a message of a type another
release added or removed is enough). ``PullRunner`` therefore contains every
handling error, and ``return_or_end`` gives the message back with a delay, so a
receiver that can handle it (another worker, another release) may take it, until
its last allowed delivery ends it. Before ending it, the receiver copies it to the
dead-letter stream (``set_aside``, kept three days, ``nats_topology``), and the
team lead tells administrators (``custom_agents.dead_letter_watch``).

A loop that ends anyway (it no longer can on one message; this would be a defect
in the loop itself) is reported, not restarted: ``on_stopped`` records it, the
agent shows it on administrators' Status page, and the log names it.
Restarting silently would hide the defect.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from typing import Any, Awaitable, Callable, Optional

from custom_transport.base import FetchPolicy
from custom_transport.dead_letters import REASON_HANDLER_FAILED

# The metadata schema of consumers this release creates. Consumers of the
# push-delivery release carried "consumer-lifecycle-v1".
CONSUMER_METADATA_SCHEMA = "consumer-lifecycle-v2"

# Worker queues are acknowledged at admission, seconds after a worker takes a
# task, so a worker that dies before admitting it returns the task to the queue
# within a minute. Fixed: it follows from acknowledging at admission.
POOL_ACK_WAIT_SECONDS = 60.0
# A worker that became busy while its request was open returns the task it was
# handed (nak), and each return counts as a delivery, so worker queues allow
# more deliveries than mailboxes before a message is given up.
POOL_MAX_DELIVER = 20

# A mailbox keeps one request open this long; a request that expires is renewed.
MAILBOX_FETCH_SECONDS = 10.0
# A worker queue request is short, so a worker that became busy for another
# reason (a scheduled run, a resumed task) seldom receives a task it must return.
POOL_FETCH_SECONDS = 5.0
# How often a receiver that may not ask checks again.
NOT_READY_POLL_SECONDS = 0.25
_ERROR_BACKOFF_SECONDS = (1.0, 2.0, 5.0, 10.0)
# A message this receiver could not handle goes back to its queue after this
# delay, so it does not return at once to the receiver that just failed it and
# another receiver (another worker, or one of another release) may take it.
UNHANDLED_RETURN_DELAY_SECONDS = 10.0

# What ``return_or_end`` did with a message.
RETURNED = "returned"
ENDED = "ended"
ALREADY_SETTLED = "already_settled"


def delivery_count(message: Any) -> Optional[int]:
    """How many times JetStream has delivered this message, or None if unknown."""

    try:
        return int(message.metadata.num_delivered)
    except Exception:
        # Core NATS messages and test doubles carry no JetStream metadata.
        return None


async def return_or_end(
    message: Any,
    *,
    max_deliver: int,
    set_aside: Optional[Callable[[], Awaitable[bool]]] = None,
) -> str:
    """Give back a message that could not be handled, or end it on its last delivery.

    Returns ``RETURNED`` (back to the queue after ``UNHANDLED_RETURN_DELAY_SECONDS``),
    ``ENDED`` (its last allowed delivery: JetStream stops delivering it, after
    ``set_aside`` copied it to the dead-letter stream) or ``ALREADY_SETTLED``
    (the handler had acknowledged, returned or ended it). The caller logs what
    happened with the message's identity; ``set_aside`` logs the copy.
    """

    if getattr(message, "is_acked", False):
        return ALREADY_SETTLED
    delivered = delivery_count(message)
    if max_deliver > 0 and delivered is not None and delivered >= max_deliver:
        if set_aside is not None:
            # A failed copy is logged by set_aside; the message still ends, as
            # JetStream would stop delivering it anyway.
            with contextlib.suppress(Exception):
                await set_aside()
        with contextlib.suppress(Exception):
            await message.term()
        return ENDED
    with contextlib.suppress(Exception):
        await message.nak(delay=UNHANDLED_RETURN_DELAY_SECONDS)
    return RETURNED


def is_push_consumer(consumer_config: Any) -> bool:
    """Whether a stored consumer is push-style (it has a deliver subject)."""

    return bool(getattr(consumer_config, "deliver_subject", None))


def consumer_needs_replacement(consumer_config: Any) -> str | None:
    """Why an existing consumer must be deleted before this release uses it, or None."""

    if is_push_consumer(consumer_config):
        return "push_consumer"
    metadata = dict(getattr(consumer_config, "metadata", None) or {})
    if metadata.get("ua.managed") == "true" and metadata.get("ua.schema") != CONSUMER_METADATA_SCHEMA:
        return "old_consumer_schema"
    return None


async def ensure_pull_consumer(
    jetstream: Any,
    *,
    stream_name: str,
    config: Any,
    logger: logging.Logger,
) -> None:
    """Create or update one durable pull consumer, replacing an old push consumer."""

    from nats.js.errors import NotFoundError

    durable = str(config.durable_name)
    try:
        info = await jetstream.consumer_info(stream_name, durable)
    except NotFoundError:
        info = None
    if info is not None:
        reason = consumer_needs_replacement(info.config)
        if reason is not None:
            logger.warning(
                "Replacing a NATS consumer left by an older release; its stored "
                "messages stay in the stream. event=nats_consumer_replaced "
                "stream_name=%s consumer_name=%s reason=%s num_pending=%s num_ack_pending=%s",
                stream_name,
                durable,
                reason,
                getattr(info, "num_pending", None),
                getattr(info, "num_ack_pending", None),
            )
            with contextlib.suppress(NotFoundError):
                await jetstream.delete_consumer(stream_name, durable)
    # Create, or update the editable fields (acknowledgement wait, delivery
    # limit, metadata) of an existing pull consumer in place.
    await jetstream.add_consumer(stream_name, config=config)


class PullRunner:
    """One durable pull subscription and the loop that asks for its messages."""

    def __init__(
        self,
        *,
        name: str,
        jetstream: Any,
        stream_name: str,
        consumer_config: Any,
        policy: FetchPolicy,
        ready: Optional[Callable[[], bool]],
        callback: Callable[[Any], Awaitable[None]],
        is_closing: Callable[[], bool],
        logger: logging.Logger,
        set_aside: Optional[Callable[[Any, str, str], Awaitable[bool]]] = None,
        on_stopped: Optional[Callable[[str, Optional[BaseException]], None]] = None,
    ) -> None:
        self.name = name
        self._js = jetstream
        self._stream_name = stream_name
        self._config = consumer_config
        self._policy = policy
        self._ready = ready
        self._callback = callback
        self._is_closing = is_closing
        self._logger = logger
        self._subscription: Any = None
        self._task: Optional[asyncio.Task[Any]] = None
        self._stopping = False
        # True while a message is being handled; unsubscribe leaves that to finish.
        self._handling = False
        # Copies a message to the dead-letter stream before it is ended
        # (message, reason, error summary -> stored).
        self._set_aside = set_aside
        # Told when the loop ends without being asked to (subscription name, error).
        self._on_stopped = on_stopped

    @property
    def durable_name(self) -> str:
        return str(self._config.durable_name)

    async def start(self) -> None:
        """Make sure the consumer exists, bind to it and start asking."""

        await ensure_pull_consumer(
            self._js, stream_name=self._stream_name, config=self._config, logger=self._logger
        )
        self._subscription = await self._js.pull_subscribe_bind(
            durable=self.durable_name, stream=self._stream_name
        )
        self._task = asyncio.create_task(self._run(), name=f"nats-pull:{self.name}")
        self._task.add_done_callback(self._loop_done)

    def _loop_done(self, task: "asyncio.Task[Any]") -> None:
        """Report a loop that ended without being asked to stop."""

        if self._stopping or self._is_closing():
            return
        error: Optional[BaseException] = None if task.cancelled() else task.exception()
        if error is not None:
            self._logger.error(
                "A receiver stopped asking for messages; it is not restarted. "
                "event=nats_pull_loop_stopped subscription=%s consumer_name=%s error_type=%s",
                self.name,
                self.durable_name,
                type(error).__name__,
                exc_info=error,
            )
        else:
            self._logger.warning(
                "A receiver stopped asking for messages without being asked to; it is not restarted. "
                "event=nats_pull_loop_stopped subscription=%s consumer_name=%s cancelled=%s",
                self.name,
                self.durable_name,
                task.cancelled(),
            )
        if self._on_stopped is not None:
            try:
                self._on_stopped(self.name, error)
            except Exception:
                self._logger.exception(
                    "Could not record a stopped receiver. event=nats_pull_stop_record_failed subscription=%s",
                    self.name,
                )

    def _may_ask(self) -> bool:
        if self._ready is None:
            return True
        try:
            return bool(self._ready())
        except Exception:
            self._logger.exception(
                "Could not tell whether a subscription may ask for work; not asking. "
                "event=nats_pull_ready_check_failed subscription=%s",
                self.name,
            )
            return False

    def _fetch_seconds(self) -> float:
        return POOL_FETCH_SECONDS if self._policy == FetchPolicy.WHEN_READY else MAILBOX_FETCH_SECONDS

    async def _run(self) -> None:
        failures = 0
        while not self._stopping and not self._is_closing():
            if not self._may_ask():
                await self._return_buffered_messages()
                await asyncio.sleep(NOT_READY_POLL_SECONDS)
                continue
            try:
                messages = await self._subscription.fetch(1, timeout=self._fetch_seconds())
            except asyncio.CancelledError:
                raise
            except TimeoutError:
                # nats.errors.TimeoutError subclasses TimeoutError: nothing arrived.
                failures = 0
                continue
            except Exception as exc:
                failures += 1
                await self._recover_from_fetch_error(exc, failures)
                continue
            failures = 0
            for message in messages:
                await self._deliver(message)

    async def _deliver(self, message: Any) -> None:
        """Hand one fetched message to the callback, or give it back to the queue."""

        if self._stopping or self._is_closing():
            await self._nak(message)
            return
        if self._policy == FetchPolicy.WHEN_READY and not self._may_ask():
            # The worker became busy while its request was open.
            self._logger.debug(
                "Returned a task to the shared queue because this worker became "
                "busy. event=nats_pull_returned_to_queue subscription=%s",
                self.name,
            )
            await self._nak(message)
            return
        self._handling = True
        try:
            await self._callback(message)
        except Exception as exc:
            # The handling failed outside its own error handling. Keep asking for
            # messages; give this one back (or end it on its last delivery).
            set_aside = self._set_aside
            error_type = type(exc).__name__
            outcome = await return_or_end(
                message,
                max_deliver=self._max_deliver,
                set_aside=(
                    (lambda: set_aside(message, REASON_HANDLER_FAILED, error_type))
                    if set_aside is not None
                    else None
                ),
            )
            self._logger.error(
                "A received message could not be handled; this receiver keeps "
                "asking for messages. event=nats_delivery_handler_failed "
                "subscription=%s consumer_name=%s delivery=%s max_deliver=%s "
                "outcome=%s error_type=%s",
                self.name,
                self.durable_name,
                delivery_count(message),
                self._max_deliver,
                outcome,
                type(exc).__name__,
                exc_info=exc,
            )
        finally:
            self._handling = False

    @property
    def _max_deliver(self) -> int:
        return int(getattr(self._config, "max_deliver", 0) or 0)

    async def _return_buffered_messages(self) -> None:
        """Give back messages that arrived after a request expired on this side.

        The server may deliver a message just after the client stopped waiting;
        it then sits in the client buffer. A receiver that may not ask now must
        not hold it until the acknowledgement wait runs out.
        """

        subscription = self._subscription
        if subscription is None or not getattr(subscription, "pending_msgs", 0):
            return
        with contextlib.suppress(Exception):
            for message in await subscription.fetch(1, timeout=0.05):
                await self._nak(message)

    async def _recover_from_fetch_error(self, exc: Exception, failures: int) -> None:
        delay = _ERROR_BACKOFF_SECONDS[min(failures, len(_ERROR_BACKOFF_SECONDS)) - 1]
        if self._stopping or self._is_closing():
            return
        name = type(exc).__name__
        consumer_gone = name in {"NotFoundError", "ServiceUnavailableError", "NoRespondersError"}
        self._logger.warning(
            "Asking NATS for the next message failed; retrying. event=nats_pull_fetch_failed "
            "subscription=%s consumer_name=%s error_type=%s retry_in_seconds=%s "
            "rebinding=%s error=%s",
            self.name,
            self.durable_name,
            name,
            delay,
            consumer_gone,
            exc,
        )
        await asyncio.sleep(delay)
        if not consumer_gone:
            return
        # Someone deleted the consumer (an operator, or an older runtime during
        # an upgrade): recreate it and bind again; its stored messages remain.
        try:
            await ensure_pull_consumer(
                self._js, stream_name=self._stream_name, config=self._config, logger=self._logger
            )
            old = self._subscription
            self._subscription = await self._js.pull_subscribe_bind(
                durable=self.durable_name, stream=self._stream_name
            )
            with contextlib.suppress(Exception):
                await old.unsubscribe()
            self._logger.warning(
                "Recreated a NATS consumer that had disappeared. "
                "event=nats_pull_consumer_recreated subscription=%s consumer_name=%s",
                self.name,
                self.durable_name,
            )
        except Exception:
            self._logger.exception(
                "Could not recreate a NATS consumer; retrying. "
                "event=nats_pull_consumer_recreate_failed subscription=%s consumer_name=%s",
                self.name,
                self.durable_name,
            )

    async def _nak(self, message: Any) -> None:
        with contextlib.suppress(Exception):
            await message.nak()

    async def unsubscribe(self) -> None:
        """Stop asking for messages; a message being handled is left to finish.

        Adapter close cancels a running delivery callback separately, so this
        returns without waiting for one.
        """

        self._stopping = True
        task = self._task
        if task is not None and not task.done() and not self._handling:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await task
        subscription = self._subscription
        if subscription is not None:
            with contextlib.suppress(Exception):
                await subscription.unsubscribe()

    @property
    def task(self) -> Optional[asyncio.Task[Any]]:
        return self._task
