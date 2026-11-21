"""Interpret JetStream consumer state returned by nats-py."""

from __future__ import annotations

import logging
from dataclasses import dataclass
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


# -- how full a stream is ------------------------------------------------------
# NATS 2.15 refuses a stream's 1,001st consumer when neither the stream nor its
# account sets a consumer limit, although both fields then read -1 (no limit);
# a limit set on the stream or the account replaces this default (checked on
# NATS 2.15.3). Servers before 2.15 have no default. This is the limit the
# 2026-11-19 incident hit. The server setting
# ``jetstream { limits { default_max_consumers: N } }`` changes the default, but
# no client can read it (the fields above still read -1), so the checks assume
# 1,000 whatever it is: early warnings when it is higher, none when lower.
NATS_DEFAULT_STREAM_CONSUMER_LIMIT = 1000
NATS_DEFAULT_STREAM_LIMIT_SINCE = (2, 15)
# Warn once a stream holds this share of its limit (a tenth left).
CONSUMER_CAPACITY_WARNING_RATIO = 0.9
# Where a stream's consumer limit came from (``StreamConsumerCapacity.limit_source``).
LIMIT_FROM_STREAM = "stream"
LIMIT_FROM_ACCOUNT = "account"
LIMIT_FROM_NATS_DEFAULT = "nats_default"


@dataclass(frozen=True)
class StreamConsumerCapacity:
    """How many consumers a stream holds and the limit NATS enforces on it."""

    stream_name: str
    consumer_count: int | None
    # None when no limit applies (no configured limit on a server before 2.15).
    consumer_limit: int | None
    limit_source: str | None
    # (major, minor) of the server, or None when it could not be read.
    server_version: tuple[int, int] | None

    @property
    def near_limit(self) -> bool:
        return (
            self.consumer_count is not None
            and self.consumer_limit is not None
            and self.consumer_count >= self.consumer_limit * CONSUMER_CAPACITY_WARNING_RATIO
        )


def server_version_of(connection: Any) -> tuple[int, int] | None:
    """(major, minor) of the NATS server a nats-py client is connected to, or None when unknown.

    nats-py reports ``0.0.0-unknown`` when the server sent no version and
    raises ValueError for one that is not a semantic version.
    """

    try:
        version = connection.connected_server_version
        found = (int(version.major), int(version.minor))
    except (AttributeError, TypeError, ValueError):
        return None
    return None if found == (0, 0) else found


def version_label(version: tuple[int, int] | None) -> str:
    """For log lines: ``2.15`` or ``unknown``."""

    return "unknown" if version is None else f"{version[0]}.{version[1]}"


def stream_consumer_capacity(
    stream_name: str, stream_info: Any, account_info: Any, server_version: tuple[int, int] | None
) -> StreamConsumerCapacity:
    """The stream's consumer count and enforced limit (constants above).

    A limit set on the stream or the account applies (the smaller one when
    both are set); only when neither is set does a server at 2.15 or newer
    apply its default.
    """

    count = getattr(getattr(stream_info, "state", None), "consumer_count", None)
    configured = [
        (int(limit), source)
        for limit, source in (
            (getattr(getattr(stream_info, "config", None), "max_consumers", None), LIMIT_FROM_STREAM),
            (getattr(getattr(account_info, "limits", None), "max_consumers", None), LIMIT_FROM_ACCOUNT),
        )
        if limit is not None and int(limit) > 0
    ]
    limit: int | None = None
    source: str | None = None
    if configured:
        limit, source = min(configured, key=lambda item: item[0])
    elif server_version is not None and server_version >= NATS_DEFAULT_STREAM_LIMIT_SINCE:
        limit, source = NATS_DEFAULT_STREAM_CONSUMER_LIMIT, LIMIT_FROM_NATS_DEFAULT
    return StreamConsumerCapacity(
        stream_name=stream_name,
        consumer_count=None if count is None else int(count),
        consumer_limit=limit,
        limit_source=source,
        server_version=server_version,
    )


def log_capacity_if_high(logger: logging.Logger, capacity: StreamConsumerCapacity | None, *, reporter: str) -> bool:
    """Warn when a stream nears its consumer limit; returns whether it warned."""

    if capacity is None or not capacity.near_limit:
        return False
    logger.warning(
        "A NATS work stream is near its consumer limit; new agents and workers will fail to start once it is "
        "reached. event=nats_stream_consumer_capacity_high stream_name=%s stream_consumer_count=%s "
        "stream_consumer_limit=%s stream_limit_source=%s nats_server_version=%s reporter=%s",
        capacity.stream_name,
        capacity.consumer_count,
        capacity.consumer_limit,
        capacity.limit_source,
        version_label(capacity.server_version),
        reporter,
    )
    return True
