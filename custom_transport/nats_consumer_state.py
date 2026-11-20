"""Interpret JetStream consumer state returned by nats-py."""

from __future__ import annotations

from typing import Any


def consumer_listening_state(consumer_info: Any) -> bool | None:
    """Whether a process is still receiving from this consumer.

    Pull consumers (ADR-019) count as live while a receiver has a pull request
    open (``num_waiting``) or holds delivered messages it has not acknowledged
    yet (``num_ack_pending``). A receiver that died stops asking, but the
    messages it held stay pending until someone asks again (NATS 2.15.3), so a
    dead receiver's consumer can keep ``num_ack_pending`` above zero for good.
    Where that matters, ``consumer_has_open_request`` asks the narrower
    question.

    Push consumers left from releases before pull delivery report
    ``push_bound``. NATS serializes that field with ``omitempty``, so an
    unbound push consumer arrives through nats-py as ``None``; the delivery
    subject tells it apart from a pull consumer.

    Returns None when the server reported neither shape.
    """

    push_bound = getattr(consumer_info, "push_bound", None)
    if push_bound is not None:
        return bool(push_bound)
    config = getattr(consumer_info, "config", None)
    if str(getattr(config, "deliver_subject", "") or "").strip():
        return False
    waiting = getattr(consumer_info, "num_waiting", None)
    held = getattr(consumer_info, "num_ack_pending", None)
    if waiting is None and held is None:
        return None
    return int(waiting or 0) > 0 or int(held or 0) > 0


def consumer_has_open_request(consumer_info: Any) -> bool:
    """Whether a receiver is asking this consumer for messages right now.

    Only a running process can have a pull request open (``num_waiting``) or
    a push-era consumer bound (``push_bound``), so this is proof that the
    receiver is alive. A dead receiver's request ends within its fetch timeout.
    Messages it held (``num_ack_pending``) are not counted: they stay pending
    after the receiver died (``consumer_listening_state``).
    """

    if getattr(consumer_info, "push_bound", None):
        return True
    return int(getattr(consumer_info, "num_waiting", None) or 0) > 0
