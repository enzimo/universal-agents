from __future__ import annotations

from custom_agents.context_recovery import (
    claim_context_recovery_attempt,
    decide_provider_context_retry,
)


def test_context_recovery_budget_is_shared_across_inner_and_outer_phases() -> None:
    invocation_state: dict[str, object] = {}

    first = claim_context_recovery_attempt(
        invocation_state,
        max_attempts=2,
        phase="inner_reduce",
    )
    second = decide_provider_context_retry(
        invocation_state,
        recovery_enabled=True,
        is_context_overflow=True,
        tool_calls_started=False,
        provider_retry_attempt=0,
        max_provider_retry_attempts=1,
        max_total_attempts=2,
    )
    exhausted = claim_context_recovery_attempt(
        invocation_state,
        max_attempts=2,
        phase="inner_reduce",
    )

    assert first.allowed is True
    assert second.allowed is True
    assert second.recovery_attempt is not None
    assert second.recovery_attempt.attempt == 2
    assert exhausted.allowed is False
    assert exhausted.attempt == 2


def test_provider_context_retry_is_blocked_after_tool_activity_without_consuming_budget() -> (
    None
):
    invocation_state: dict[str, object] = {}

    decision = decide_provider_context_retry(
        invocation_state,
        recovery_enabled=True,
        is_context_overflow=True,
        tool_calls_started=True,
        provider_retry_attempt=0,
        max_provider_retry_attempts=1,
        max_total_attempts=3,
    )

    assert decision.handled is True
    assert decision.allowed is False
    assert decision.reason == "tool_activity_started"
    assert invocation_state == {}
