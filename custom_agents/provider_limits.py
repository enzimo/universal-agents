"""A worker whose model provider refuses calls waits and retries; it does not ask anyone.

Before worker pools, a delegated task whose provider stayed overloaded after
Strands retries and model failover was paused and its requester asked the
person what to do, once per task; with dozens of workers that floods the
person with questions, and every waiting worker went on taking new tasks that
hit the same limit. Now, for delegated work (owner's decision 2026-10-04):

1. The worker records a provider-limit event (``storage.worker_pool_repository``),
   which the worker pool controller counts per worker type: it starts no more
   workers of the type and, past ``provider_limit.review_events`` events in
   ``provider_limit.review_window_minutes``, gives TeamArchitect a review (it may
   choose fewer workers or to let tasks wait).
2. The worker pauses the task, schedules a retry after
   ``provider_limit.cooldown_seconds`` (doubling each time, at most
   ``max_cooldown_seconds``), and takes no new work until then
   (``pool_intake.may_take_pool_task``). The failed model call made no tool
   call, so retrying it repeats nothing.
3. After ``provider_limit.max_retries`` waits the task fails with a typed
   failure (``TaskFailureV1``, category ``provider_limited``), so the requester's
   trusted code knows why without reading error text.

TeamArchitect's own tasks keep the person-guided pause.
"""

from __future__ import annotations

import time
from datetime import datetime, timedelta, timezone
from functools import partial
from typing import Any, Optional

from custom_agents.model_failover_runtime import ProviderFailoverRequeued
from custom_agents.provider_error_runtime import provider_error_status_code
from custom_messages.contracts.delegated_work_v1 import TaskFailureCategory, TaskFailureV1
from custom_messages.enums import (
    ProgressState,
    TaskHistoryImportance,
    TaskHistorySource,
    TaskHistoryType,
)
from storage.worker_pool_repository import ProviderLimitEventV1

PROVIDER_LIMIT_COOLDOWN_EVENT = "provider_limit_cooldown"


class ProviderCooldownScheduled(ProviderFailoverRequeued):
    """The task was paused to retry after the provider's limit; it is not failed."""


class ProviderLimitExhausted(RuntimeError):
    """Retries after a provider limit ran out; the task fails with ``failure``."""

    def __init__(self, message: str, failure: TaskFailureV1) -> None:
        super().__init__(message)
        self.failure = failure


def _cooldowns_so_far(task_info: Any) -> int:
    return sum(
        1
        for entry in (task_info.task.execution_history or [])
        if (entry.details or {}).get("event") == PROVIDER_LIMIT_COOLDOWN_EVENT
    )


def _record_event(agent: Any, *, task_id: str, status_code: Optional[int]) -> None:
    from custom_agents.worker_pool_limits import worker_pool_repository

    worker_pool_repository(agent).record_provider_limit(
        ProviderLimitEventV1(
            team_id=str(agent.team_id),
            agent_type=str(agent.named_agent_type or ""),
            agent_id=str(agent.id),
            task_id=task_id,
            provider=getattr(agent, "model_provider", None),
            model_id=getattr(agent, "model_id", None),
            status_code=status_code,
            at_utc=datetime.now(timezone.utc),
        )
    )


async def handle_delegated_provider_limit(agent: Any, *, task_info: Any, error: BaseException) -> None:
    """Wait out a provider limit on a delegated task; always raises.

    Raises ``ProviderCooldownScheduled`` after pausing the task with a retry
    timer, or ``ProviderLimitExhausted`` once retries are used up.
    """

    from custom_agents.worker_pool_settings import current_worker_pool_settings

    settings = current_worker_pool_settings(agent).provider_limit
    task_id = task_info.task.task_id
    run = agent.task_manager.operations_runtime.run
    status_code = provider_error_status_code(error)
    try:
        await run(partial(_record_event, agent, task_id=task_id, status_code=status_code))
    except Exception:
        agent._logger.exception(
            "Could not record a provider-limit event. event=provider_limit_event_failed "
            "agent_id=%s task_id=%s",
            agent.id,
            task_id,
        )
    waited = _cooldowns_so_far(task_info)
    if waited >= settings.max_retries:
        failure = TaskFailureV1(
            category=TaskFailureCategory.PROVIDER_LIMITED,
            provider=getattr(agent, "model_provider", None),
            model_id=getattr(agent, "model_id", None),
            status_code=status_code,
            attempts=waited,
        )
        agent._logger.error(
            "The model provider kept refusing calls; the delegated task fails as provider-limited. "
            "event=provider_limit_retries_exhausted agent_id=%s task_id=%s retries=%s provider=%s "
            "model=%s status_code=%s",
            agent.id,
            task_id,
            waited,
            failure.provider,
            failure.model_id,
            status_code,
        )
        raise ProviderLimitExhausted(
            "The model provider kept refusing calls (rate limit, quota or overload) after "
            f"{waited} retries, so this task could not be finished now.",
            failure,
        ) from error
    delay = min(settings.cooldown_seconds * (2**waited), settings.max_cooldown_seconds)
    retry_at = datetime.now(timezone.utc) + timedelta(seconds=delay)
    agent._provider_cooldown_until = time.monotonic() + delay
    agent._provider_cooldown = {
        "provider": getattr(agent, "model_provider", None),
        "model_id": getattr(agent, "model_id", None),
        "until_utc": retry_at.isoformat(),
        "task_id": task_id,
    }
    now_ts = datetime.now(timezone.utc).isoformat()
    await run(
        agent.task_manager.append_history_entry,
        task_id,
        f"The model provider refused calls; retrying in {int(delay)} seconds "
        f"(retry {waited + 1} of {settings.max_retries}). This worker takes no new work meanwhile.",
        source=TaskHistorySource.SYSTEM,
        entry_type=TaskHistoryType.ERROR_EVENT,
        importance=TaskHistoryImportance.HIGH,
        timestamp=now_ts,
        details={
            "event": PROVIDER_LIMIT_COOLDOWN_EVENT,
            "retry": waited + 1,
            "max_retries": settings.max_retries,
            "delay_seconds": delay,
            "retry_at": retry_at.isoformat(),
            "status_code": status_code,
        },
    )
    await run(agent.task_manager.update_main_task_state, task_id, ProgressState.PAUSED, now_ts)
    await agent._schedule_provider_cooldown_retry(task_info=task_info, retry_at=retry_at)
    agent._logger.warning(
        "The model provider refused calls; the worker waits before retrying its task and "
        "takes no new work. event=provider_limit_cooldown agent_id=%s agent_type=%s task_id=%s "
        "retry=%s delay_seconds=%s status_code=%s",
        agent.id,
        agent.named_agent_type,
        task_id,
        waited + 1,
        delay,
        status_code,
    )
    raise ProviderCooldownScheduled(f"Provider limit: retry scheduled at {retry_at.isoformat()}.") from error


__all__ = [
    "PROVIDER_LIMIT_COOLDOWN_EVENT",
    "ProviderCooldownScheduled",
    "ProviderLimitExhausted",
    "handle_delegated_provider_limit",
]
