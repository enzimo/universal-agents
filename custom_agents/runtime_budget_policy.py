"""Runtime budget policy helpers for Strands agents."""

from __future__ import annotations

import copy
import logging
from collections import Counter
from typing import Any, Dict, Iterable, Optional

from default_values import AGENT_DEFAULTS
from custom_messages.contracts.runtime_v1 import WEB_RESEARCH_AGENT_TYPE
from custom_messages.enums import RecoverablePauseTrigger, RuntimeBudgetPolicyMode, TaskQueryDecision


def runtime_budget_policy_config(
    configured_policy: Optional[Dict[str, Any]],
) -> Dict[str, Any]:
    """Deep-merge a configured runtime-budget policy over defaults."""
    defaults = copy.deepcopy(AGENT_DEFAULTS.runtime_budget_policy.value)
    configured = configured_policy if isinstance(configured_policy, dict) else {}

    def merge(base: Dict[str, Any], override: Dict[str, Any]) -> Dict[str, Any]:
        result = dict(base)
        for key, value in override.items():
            if isinstance(value, dict) and isinstance(result.get(key), dict):
                result[key] = merge(dict(result[key]), value)
            else:
                result[key] = value
        return result

    return merge(defaults, configured)


def runtime_budget_policy_mode(
    configured_policy: Optional[Dict[str, Any]],
    *,
    logger: Optional[logging.Logger] = None,
    agent_id: str = "unknown",
) -> str:
    """Return a validated runtime-budget policy mode."""
    policy = runtime_budget_policy_config(configured_policy)
    raw = str(policy.get("mode") or RuntimeBudgetPolicyMode.HARD_STOP.value)
    normalized = raw.strip().lower()
    if normalized not in {
        RuntimeBudgetPolicyMode.HARD_STOP.value,
        RuntimeBudgetPolicyMode.USER_GUIDED.value,
    }:
        if logger is not None:
            logger.warning(
                "[%s] Invalid runtime budget policy mode %r; falling back to hard_stop.",
                agent_id,
                raw,
            )
        return RuntimeBudgetPolicyMode.HARD_STOP.value
    return normalized


def runtime_budget_trigger_enabled(
    configured_policy: Optional[Dict[str, Any]],
    trigger: RecoverablePauseTrigger,
) -> bool:
    """Return whether a recoverable pause trigger is enabled."""
    policy = runtime_budget_policy_config(configured_policy)
    triggers = (
        policy.get("triggers") if isinstance(policy.get("triggers"), dict) else {}
    )
    return bool(triggers.get(trigger.value, True))


def max_user_guided_pauses_per_task(configured_policy: Optional[Dict[str, Any]]) -> int:
    raw = runtime_budget_policy_config(configured_policy).get(
        "max_user_guided_pauses_per_task", 3
    )
    try:
        return max(0, int(raw))
    except (TypeError, ValueError):
        return 3


def partial_tool_call_budget_multiplier(
    configured_policy: Optional[Dict[str, Any]],
) -> float:
    raw = runtime_budget_policy_config(configured_policy).get(
        "partial_tool_call_budget_multiplier", 1.5
    )
    try:
        value = float(raw)
    except (TypeError, ValueError):
        return 1.5
    return max(1.0, value)


def partial_tool_call_limit(
    configured_policy: Optional[Dict[str, Any]], base_limit: Optional[int]
) -> Optional[int]:
    if base_limit is None or base_limit <= 0:
        return None
    multiplier = partial_tool_call_budget_multiplier(configured_policy)
    return max(base_limit + 1, int(base_limit * multiplier + 0.9999))


def _decision_values(allowed_decisions: Optional[Iterable[Any]]) -> list[str]:
    if allowed_decisions is None:
        return [
            TaskQueryDecision.RESUME.value,
            TaskQueryDecision.PARTIAL.value,
            TaskQueryDecision.CANCEL.value,
        ]
    values: list[str] = []
    for decision in allowed_decisions:
        value = (
            decision.value if isinstance(decision, TaskQueryDecision) else str(decision)
        )
        value = value.strip()
        if value:
            values.append(value)
    return values


def parse_runtime_pause_decision(
    text: str,
    *,
    allowed_decisions: Optional[Iterable[Any]] = None,
) -> Optional[str]:
    normalized = str(text or "").strip().lower()
    first = normalized.split(maxsplit=1)[0] if normalized else ""
    allowed = _decision_values(allowed_decisions)
    if first.isdigit():
        index = int(first) - 1
        if 0 <= index < len(allowed):
            return allowed[index]
    if normalized in {"continue", "resume", "yes"}:
        return (
            TaskQueryDecision.RESUME.value
            if TaskQueryDecision.RESUME.value in allowed
            else None
        )
    if normalized in {"retry", "try again", "try now", "retry now"}:
        if TaskQueryDecision.RETRY.value in allowed:
            return TaskQueryDecision.RETRY.value
        return (
            TaskQueryDecision.RESUME.value
            if TaskQueryDecision.RESUME.value in allowed
            else None
        )
    if normalized in {
        "try in 5",
        "try in 5 min",
        "try in 5 mins",
        "try in 5 minutes",
        "retry in 5",
        "retry in 5 min",
        "retry in 5 mins",
        "retry in 5 minutes",
    }:
        return (
            TaskQueryDecision.RETRY_IN_5_MINUTES.value
            if TaskQueryDecision.RETRY_IN_5_MINUTES.value in allowed
            else None
        )
    if normalized in {
        "try in 10",
        "try in 10 min",
        "try in 10 mins",
        "try in 10 minutes",
        "retry in 10",
        "retry in 10 min",
        "retry in 10 mins",
        "retry in 10 minutes",
    }:
        return (
            TaskQueryDecision.RETRY_IN_10_MINUTES.value
            if TaskQueryDecision.RETRY_IN_10_MINUTES.value in allowed
            else None
        )
    if normalized in {"partial", "best partial", "summarize", "summary"}:
        return (
            TaskQueryDecision.PARTIAL.value
            if TaskQueryDecision.PARTIAL.value in allowed
            else None
        )
    if normalized in {"stop", "cancel", "abort", "give up", "giveup"}:
        return (
            TaskQueryDecision.CANCEL.value
            if TaskQueryDecision.CANCEL.value in allowed
            else None
        )
    return None


def runtime_pause_identity(*, task_id: str, description: str, subtask_id: Optional[str] = None, worker: Optional[str] = None) -> str:
    """Keep pause decisions identifiable in chat and detached notifications."""
    text = f"Task: {str(description or 'Untitled task')[:500]}\nTask ID: {task_id}\n"
    if subtask_id:
        text += f"Delegated task: {subtask_id} ({worker or 'worker'})\n"
    return text + "\n"


def runtime_pause_prompt(friendly_reason: str) -> str:
    return (
        f"This task has been paused because {friendly_reason.rstrip('. ')}.\n\n"
        "Reply with one of these options:\n"
        "- 1 - continue working on the task\n"
        "- 2 - return the best partial result(s) now\n"
        "- 3 - stop the task"
    )


def provider_overload_pause_prompt(friendly_reason: str) -> str:
    return (
        f"The model endpoint reported a recoverable overload error:\n{friendly_reason}\n\n"
        "Reply with one of these options:\n"
        "- 1 - retry now\n"
        "- 2 - try in 5 mins\n"
        "- 3 - try in 10 mins\n"
        "- 4 - give up"
    )


def friendly_budget_reason(
    *,
    trigger: RecoverablePauseTrigger,
    named_agent_type: Optional[str],
    limit_error: Optional[Any] = None,
    detail: Optional[str] = None,
) -> str:
    """Build a user-safe explanation for a recoverable budget pause."""
    named_type = named_agent_type or "this agent"
    if (
        trigger == RecoverablePauseTrigger.TOOL_CALL_BUDGET_EXCEEDED
        and limit_error is not None
    ):
        counts = Counter(getattr(limit_error, "tool_sequence", []) or [])
        research_heavy = any(
            name
            in {
                "exa_search",
                "brave_web_search_artifact",
                "brave_llm_context_artifact",
                "capture_web_screenshot",
                "web_search",
                "searxng",
                "crawl",
                "search",
            }
            or name.startswith("crawl4ai_")
            for name in counts
        )
        if research_heavy or named_type == WEB_RESEARCH_AGENT_TYPE:
            return (
                "web searches and source checks are taking longer than expected, "
                "and the research worker reached its tool-call budget before finishing"
            )
        if named_type == AGENT_DEFAULTS.named_agent_type.value:
            return (
                "the manager is still processing information relevant to the task "
                "and reached its tool-call budget before producing a final result"
            )
        return f"{named_type} reached its tool-call budget before finishing its current work"
    if trigger == RecoverablePauseTrigger.CONTEXT_BUDGET_EXCEEDED:
        return "the task has accumulated more relevant context than the model can safely process in one turn"
    if trigger == RecoverablePauseTrigger.DELEGATED_SUBTASK_TIMEOUT:
        return (
            detail
            or "a delegated worker has not returned a result within the configured wait window"
        )
    if trigger == RecoverablePauseTrigger.SPECIALIST_RUNTIME_BUDGET_PAUSE:
        return (
            detail
            or f"{named_type} reached a runtime budget while working on a delegated part of the task"
        )
    if trigger == RecoverablePauseTrigger.PROVIDER_OVERLOADED:
        return (
            detail
            or "the model endpoint is currently overloaded and asked us to try again later"
        )
    return (
        detail
        or "a recoverable runtime budget was reached before the task could finish"
    )
