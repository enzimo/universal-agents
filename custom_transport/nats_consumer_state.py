"""Interpret JetStream consumer state returned by nats-py."""

from __future__ import annotations

from typing import Any


def consumer_listening_state(consumer_info: Any) -> bool | None:
    """Whether a process is still receiving from this consumer.

    Pull consumers (ADR-019) are live while a receiver has a pull request open
    (``num_waiting``) or holds delivered messages it has not acknowledged yet
    (``num_ack_pending``). A receiver that died stops asking, and its held
    messages return to the queue after the consumer's ack wait, so both
    counts fall to zero.

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
