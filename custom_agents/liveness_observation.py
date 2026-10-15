"""Keep a pause of the monitor from being mistaken for a stopped worker.

Heartbeat age is wall time, so it includes host suspension. The scheduler
observes its own checks and, after a gap longer than its normal cadence,
allows three heartbeat intervals before using old heartbeats as evidence.
Launch records and explicit exits remain authoritative during this window.
This state is local to the observer and grants no task or launch authority.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

NATS_PRESENCE_HEARTBEAT_SECONDS = 20.0
MAX_SCHEDULER_SLEEP_SECONDS = 60.0
HEARTBEAT_REFRESH_SECONDS = 3 * NATS_PRESENCE_HEARTBEAT_SECONDS
_MAX_CHECK_GAP_SECONDS = MAX_SCHEDULER_SLEEP_SECONDS + NATS_PRESENCE_HEARTBEAT_SECONDS


@dataclass
class LivenessObservation:
    """One scheduler's last check and bounded grace after an observation gap."""

    checked_at: datetime | None = None
    checked_clock: float | None = None
    defer_until: float = 0.0

    def observe(self, now: datetime, clock: float) -> float | None:
        gap = None
        if self.checked_at is not None and self.checked_clock is not None:
            # Linux monotonic time excludes host suspend; a VM may count it.
            # Wall time and monotonic gaps therefore both matter here.
            elapsed = max((now - self.checked_at).total_seconds(), clock - self.checked_clock)
            if elapsed > _MAX_CHECK_GAP_SECONDS:
                self.defer_until = clock + HEARTBEAT_REFRESH_SECONDS
                gap = elapsed
        self.checked_at, self.checked_clock = now, clock
        return gap


def observe_liveness(agent: Any, *, now: datetime | None = None) -> None:
    """Record an active check, logging any pause that requires fresh heartbeats."""
    observation = getattr(agent, "_liveness_observation", None)
    if observation is None:
        observation = LivenessObservation()
        agent._liveness_observation = observation
    gap = observation.observe(now or datetime.now(timezone.utc), time.monotonic())
    if gap is not None:
        agent._logger.warning(
            "The task monitor paused; allowing workers to refresh their heartbeats. "
            "event=liveness_monitor_paused agent_id=%s gap_seconds=%.3f refresh_seconds=%.0f",
            agent.id, gap, HEARTBEAT_REFRESH_SECONDS,
        )


def heartbeat_checks_deferred(agent: Any) -> bool:
    """Whether this observer must wait before trusting a stale heartbeat."""
    observation = getattr(agent, "_liveness_observation", None)
    return observation is not None and time.monotonic() < observation.defer_until
