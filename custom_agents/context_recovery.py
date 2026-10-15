"""Shared accounting for bounded context-overflow recovery."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping, MutableMapping


DEFAULT_MAX_TOTAL_RECOVERY_ATTEMPTS = 3
MAX_TOTAL_RECOVERY_ATTEMPTS_CEILING = 10
CONTEXT_RECOVERY_STATE_KEY = "universal_agents_context_recovery"


@dataclass(frozen=True)
class ContextRecoveryAttempt:
    """Result of claiming one attempt from a request-scoped recovery budget."""

    allowed: bool
    attempt: int
    max_attempts: int
    phase: str


@dataclass(frozen=True)
class ProviderContextRetryDecision:
    """Whether UA may restart a rejected provider call from a fresh transcript."""

    handled: bool
    allowed: bool
    reason: str
    recovery_attempt: ContextRecoveryAttempt | None = None


def reactive_overflow_config(config: Mapping[str, Any] | None) -> dict[str, Any]:
    """Return the normalized reactive-overflow section."""

    value = (config or {}).get("reactive_overflow")
    return dict(value) if isinstance(value, Mapping) else {}


def max_total_recovery_attempts(config: Mapping[str, Any] | None) -> int:
    """Resolve the request-wide ceiling shared by inner and outer recovery."""

    value = reactive_overflow_config(config).get(
        "max_total_recovery_attempts",
        DEFAULT_MAX_TOTAL_RECOVERY_ATTEMPTS,
    )
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        parsed = DEFAULT_MAX_TOTAL_RECOVERY_ATTEMPTS
    return max(1, min(parsed, MAX_TOTAL_RECOVERY_ATTEMPTS_CEILING))


def min_reduction_tokens(config: Mapping[str, Any] | None) -> int:
    """Resolve the minimum measured reduction that permits an inner retry."""

    value = reactive_overflow_config(config).get("min_reduction_tokens", 1)
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        parsed = 1
    return max(1, parsed)


def invocation_request_state(
    invocation_state: MutableMapping[str, Any],
) -> MutableMapping[str, Any]:
    """Return Strands request state without replacing a valid caller mapping."""

    request_state = invocation_state.get("request_state")
    if isinstance(request_state, MutableMapping):
        return request_state
    request_state = {}
    invocation_state["request_state"] = request_state
    return request_state


def claim_context_recovery_attempt(
    invocation_state: MutableMapping[str, Any],
    *,
    max_attempts: int,
    phase: str,
) -> ContextRecoveryAttempt:
    """Claim one bounded attempt for this request and recovery phase."""

    request_state = invocation_request_state(invocation_state)
    recovery_state = request_state.get(CONTEXT_RECOVERY_STATE_KEY)
    if not isinstance(recovery_state, MutableMapping):
        recovery_state = {}
        request_state[CONTEXT_RECOVERY_STATE_KEY] = recovery_state

    try:
        completed_attempts = max(0, int(recovery_state.get("attempts") or 0))
    except (TypeError, ValueError):
        completed_attempts = 0
    if completed_attempts >= max_attempts:
        return ContextRecoveryAttempt(
            allowed=False,
            attempt=completed_attempts,
            max_attempts=max_attempts,
            phase=phase,
        )

    attempt = completed_attempts + 1
    recovery_state["attempts"] = attempt
    phase_attempts = recovery_state.get("phase_attempts")
    if not isinstance(phase_attempts, MutableMapping):
        phase_attempts = {}
        recovery_state["phase_attempts"] = phase_attempts
    try:
        phase_count = max(0, int(phase_attempts.get(phase) or 0))
    except (TypeError, ValueError):
        phase_count = 0
    phase_attempts[phase] = phase_count + 1
    return ContextRecoveryAttempt(
        allowed=True,
        attempt=attempt,
        max_attempts=max_attempts,
        phase=phase,
    )


def decide_provider_context_retry(
    invocation_state: MutableMapping[str, Any],
    *,
    recovery_enabled: bool,
    is_context_overflow: bool,
    tool_calls_started: bool,
    provider_retry_attempt: int,
    max_provider_retry_attempts: int,
    max_total_attempts: int,
) -> ProviderContextRetryDecision:
    """Decide whether a fresh outer invocation is safe and still budgeted."""

    if not recovery_enabled or not is_context_overflow:
        return ProviderContextRetryDecision(
            handled=False,
            allowed=False,
            reason="not_context_recovery",
        )
    if tool_calls_started:
        return ProviderContextRetryDecision(
            handled=True,
            allowed=False,
            reason="tool_activity_started",
        )
    if provider_retry_attempt >= max_provider_retry_attempts:
        return ProviderContextRetryDecision(
            handled=True,
            allowed=False,
            reason="provider_retry_budget_exhausted",
        )

    attempt = claim_context_recovery_attempt(
        invocation_state,
        max_attempts=max_total_attempts,
        phase="provider_reinvoke",
    )
    return ProviderContextRetryDecision(
        handled=True,
        allowed=attempt.allowed,
        reason="retry" if attempt.allowed else "total_recovery_budget_exhausted",
        recovery_attempt=attempt,
    )
