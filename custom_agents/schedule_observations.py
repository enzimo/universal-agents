"""Shared observation accounting for tools and deterministic runtime completion."""

from typing import Any

from custom_messages.contracts.schedule_v1 import ScheduleTaskMemory

SCHEDULE_MEMORY_RECENT_RUN_LIMIT = 20
SCHEDULE_FAILURE_OUTCOMES = {"failure", "failed", "error", "conflict", "inconclusive"}


def record_run_observation(
    memory: ScheduleTaskMemory,
    *,
    outcome: str,
    state_changed: bool,
    summary: str,
    evidence: dict[str, Any],
    error: str,
    at: str,
) -> bool:
    """Record observed work, without changing authority or schedule control state."""
    failure = outcome in SCHEDULE_FAILURE_OUTCOMES
    memory.ledger.total_runs += 1
    memory.ledger.last_run_at = at
    if failure:
        memory.ledger.consecutive_failures += 1
    else:
        memory.ledger.consecutive_failures = 0
        memory.ledger.last_success_at = at
    record: dict[str, Any] = {
        "at": at,
        "outcome": outcome,
        "state_changed": state_changed,
        "summary": summary.strip() or memory.state.summary or None,
    }
    if evidence:
        record["evidence"] = evidence
    if error.strip():
        record["error"] = error.strip()
    should_notify = (
        bool(memory.policy.notify_on_failure)
        if failure
        else (
            memory.policy.notify == "always"
            or (memory.policy.notify == "on_change" and state_changed)
        )
    )
    record["should_notify"] = should_notify
    memory.ledger.recent_runs.append(record)
    memory.ledger.recent_runs = memory.ledger.recent_runs[
        -SCHEDULE_MEMORY_RECENT_RUN_LIMIT:
    ]
    return should_notify
