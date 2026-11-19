"""Stream limits for tests that start an installation authority (``custom_messages.wire.queues.QueueLimits``).

The authority creates every team's work and dead-letter streams before it serves, and NATS
sets each stream's whole byte limit aside when it creates it. Tests use 1 MiB per stream, so a
team's 100 streams set aside 100 MiB on a shared live server rather than the defaults' 2 GiB.
"""

from __future__ import annotations

from custom_messages.wire.queues import QueueLimits
from custom_transport.protocol1.contract_files import transport

SIZE_KINDS = tuple(transport()["queues"]["size_kinds"])
TEST_STREAM_BYTES = 1 << 20


def queue_limits(*, cap: int = 8, replicas: int = 1, **max_bytes: int) -> QueueLimits:
    """1 MiB per size kind unless ``max_bytes`` names another limit; ``cap`` is the per-subject cap."""

    unknown = set(max_bytes) - set(SIZE_KINDS)
    if unknown:
        raise ValueError(f"not size kinds: {sorted(unknown)}")
    return QueueLimits(max_bytes={kind: max_bytes.get(kind, TEST_STREAM_BYTES) for kind in SIZE_KINDS},
                       max_messages_per_subject=cap, replicas=replicas)


TEST_QUEUE_LIMITS = queue_limits()
