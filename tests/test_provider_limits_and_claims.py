"""Provider limits on delegated work, and claim-based retry counting, with real SQLite stores."""

from __future__ import annotations

import logging
import time
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from custom_agents.provider_limits import (
    PROVIDER_LIMIT_COOLDOWN_EVENT,
    ProviderCooldownScheduled,
    ProviderLimitExhausted,
    handle_delegated_provider_limit,
)
from custom_messages.contracts.delegated_work_v1 import DelegatedTaskClaimedV1, TaskFailureCategory
from custom_messages.contracts.routing_v1 import MessageHeader
from custom_messages.contracts.task_v1 import Task, TaskHistoryEntry
from custom_messages.enums import MessageMetaType, ProgressState
from runtime_settings.worker_pools import WorkerPoolsConfig
from storage import resolve_storage_settings
from storage.worker_pool_repository import WorkerPoolRepository


class _Runtime:
    async def run(self, fn, *args, **kwargs):
        return fn(*args, **kwargs)


class _Overloaded(Exception):
    status_code = 429


def _worker(tmp_path, history_events: int = 0):
    storage = resolve_storage_settings({"backend": "sqlite", "sqlite": {"path": str(tmp_path / "w.db")}})
    history = [
        TaskHistoryEntry(summary="waited", details={"event": PROVIDER_LIMIT_COOLDOWN_EVENT})
        for _ in range(history_events)
    ]
    task = Task(task_id="sub_1", parent_task_id="parent_1", task_type=MessageMetaType.REQUEST, execution_history=history)
    states: list = []
    entries: list = []
    manager = SimpleNamespace(
        storage_settings=storage,
        operations_runtime=_Runtime(),
        append_history_entry=lambda task_id, summary, **kwargs: entries.append(kwargs.get("details")),
        update_main_task_state=lambda task_id, state, ts: states.append(state) or True,
    )
    agent = SimpleNamespace(
        id="worker_1",
        team_id="team_t",
        named_agent_type="WebResearchAgent",
        model_provider="litellm",
        model_id="model-x",
        task_manager=manager,
        _logger=logging.getLogger("provider-test"),
        _provider_cooldown_until=None,
        _provider_cooldown=None,
        _schedule_provider_cooldown_retry=AsyncMock(),
    )
    return agent, SimpleNamespace(task=task), states, entries, storage


@pytest.fixture(autouse=True)
def _settings(monkeypatch):
    monkeypatch.setattr(
        "custom_agents.worker_pool_settings.current_worker_pool_settings",
        lambda _agent: WorkerPoolsConfig(provider_limit={"cooldown_seconds": 10, "max_cooldown_seconds": 25, "max_retries": 2}),
    )


@pytest.mark.asyncio
async def test_a_provider_limit_pauses_the_task_and_holds_new_work(tmp_path):
    agent, task_info, states, entries, storage = _worker(tmp_path)
    with pytest.raises(ProviderCooldownScheduled):
        await handle_delegated_provider_limit(agent, task_info=task_info, error=_Overloaded("rate limited"))
    assert states == [ProgressState.PAUSED]
    assert entries[0]["event"] == PROVIDER_LIMIT_COOLDOWN_EVENT and entries[0]["delay_seconds"] == 10
    assert agent._provider_cooldown_until > time.monotonic()
    agent._schedule_provider_cooldown_retry.assert_awaited_once()
    events = WorkerPoolRepository(storage).provider_limit_events("team_t", since=datetime(2000, 1, 1, tzinfo=timezone.utc))
    assert [(e.agent_type, e.status_code) for e in events] == [("WebResearchAgent", 429)]


@pytest.mark.asyncio
async def test_the_wait_doubles_up_to_its_maximum(tmp_path):
    agent, task_info, _states, entries, _ = _worker(tmp_path, history_events=1)
    with pytest.raises(ProviderCooldownScheduled):
        await handle_delegated_provider_limit(agent, task_info=task_info, error=_Overloaded("x"))
    assert entries[0]["delay_seconds"] == 20


@pytest.mark.asyncio
async def test_retries_used_up_fail_the_task_with_a_typed_reason(tmp_path):
    agent, task_info, states, _entries, _ = _worker(tmp_path, history_events=2)
    with pytest.raises(ProviderLimitExhausted) as raised:
        await handle_delegated_provider_limit(agent, task_info=task_info, error=_Overloaded("x"))
    failure = raised.value.failure
    assert failure.category == TaskFailureCategory.PROVIDER_LIMITED
    assert (failure.provider, failure.model_id, failure.status_code, failure.attempts) == ("litellm", "model-x", 429, 2)
    assert states == []


@pytest.mark.asyncio
async def test_a_typed_failure_reaches_the_requester_in_the_error_response(tmp_path):
    from custom_agents.turn_failure_runtime import task_runtime_error_message

    agent, task_info, *_ = _worker(tmp_path, history_events=2)
    with pytest.raises(ProviderLimitExhausted) as raised:
        await handle_delegated_provider_limit(agent, task_info=task_info, error=_Overloaded("x"))
    assert task_runtime_error_message(raised.value).startswith("The model provider kept refusing calls")


# -- claims on the requester ---------------------------------------------------------


def _claim(worker: str, task_id: str = "sub_1") -> DelegatedTaskClaimedV1:
    return DelegatedTaskClaimedV1(
        claim_id=f"claim:{task_id}:{worker}",
        team_id="team_t",
        task_id=task_id,
        parent_task_id="parent_1",
        worker_agent_id=worker,
        worker_agent_name=worker,
        claimed_at_utc=datetime.now(timezone.utc),
    )


def _requester(tmp_path, monkeypatch, *, retries: int = 1):
    from custom_agents.delegated_work_runtime import record_publication

    storage = resolve_storage_settings({"backend": "sqlite", "sqlite": {"path": str(tmp_path / "r.db")}})
    subtask = SimpleNamespace(task=Task(task_id="sub_1", parent_task_id="parent_1", task_type=MessageMetaType.REQUEST), state=ProgressState.ACCEPTED)
    manager = SimpleNamespace(
        storage_settings=storage,
        operations_runtime=_Runtime(),
        get_task_info=lambda task_id: subtask if task_id == "sub_1" else None,
        get_parent_task_id=lambda task_id: "parent_1" if task_id == "sub_1" else None,
        is_terminal_state=lambda state: state in {ProgressState.COMPLETED, ProgressState.FAILED, ProgressState.CANCELLED},
        is_execution_cancelled=lambda task_id: False,
        append_history_entry=lambda *args, **kwargs: None,
        resolve_root_task_id=lambda task_id: "parent_1",
        update_task_result=lambda task_id, update: subtask,
    )
    agent = SimpleNamespace(
        id="lead",
        name="lead",
        team_id="team_t",
        task_manager=manager,
        _logger=logging.getLogger("claim-test"),
        team_comms=SimpleNamespace(send_message=AsyncMock()),
        _fail_subtask_and_reconcile_parent=AsyncMock(),
    )
    monkeypatch.setattr(
        "custom_agents.worker_pool_settings.current_worker_pool_settings",
        lambda _agent: WorkerPoolsConfig(max_retries_after_worker_loss=retries),
    )
    record_publication(agent, subtask_id="sub_1", parent_task_id="parent_1", named_agent_type="WebResearchAgent", target_agent_name=None, publish_sequence=3)
    return agent


@pytest.mark.asyncio
async def test_claims_record_the_holder_and_moves_until_the_retry_limit(tmp_path, monkeypatch):
    from custom_agents.delegated_work_runtime import delegated_tasks, handle_delegated_task_claimed

    agent = _requester(tmp_path, monkeypatch, retries=1)
    for worker in ("w1", "w2"):
        await handle_delegated_task_claimed(agent, claim=_claim(worker), msg_header=MessageHeader(from_agent_id=worker), msg_meta=None)
    record = delegated_tasks(agent).get("team_t", "sub_1")
    assert (record.holder_agent_id, record.interruptions) == ("w2", 1)
    agent._fail_subtask_and_reconcile_parent.assert_not_awaited()
    # A second move exceeds one automatic retry: stop the holder and fail the task.
    await handle_delegated_task_claimed(agent, claim=_claim("w3"), msg_header=MessageHeader(from_agent_id="w3"), msg_meta=None)
    agent._fail_subtask_and_reconcile_parent.assert_awaited_once()
    stop = agent.team_comms.send_message.await_args.kwargs
    assert stop["routing_key"] == "w3"
    assert stop["message"].msg_content.target_task_id == "sub_1"


@pytest.mark.asyncio
async def test_a_claim_not_sent_by_its_worker_is_ignored(tmp_path, monkeypatch):
    from custom_agents.delegated_work_runtime import delegated_tasks, handle_delegated_task_claimed

    agent = _requester(tmp_path, monkeypatch)
    await handle_delegated_task_claimed(agent, claim=_claim("w1"), msg_header=MessageHeader(from_agent_id="someone_else"), msg_meta=None)
    assert delegated_tasks(agent).get("team_t", "sub_1").holder_agent_id is None


@pytest.mark.asyncio
async def test_a_worker_that_took_an_ended_task_is_told_to_stop(tmp_path, monkeypatch):
    from custom_agents.delegated_work_runtime import handle_delegated_task_claimed

    agent = _requester(tmp_path, monkeypatch)
    agent.task_manager.get_task_info("sub_1").state = ProgressState.CANCELLED
    await handle_delegated_task_claimed(agent, claim=_claim("w9"), msg_header=MessageHeader(from_agent_id="w9"), msg_meta=None)
    sent = agent.team_comms.send_message.await_args.kwargs
    assert sent["routing_key"] == "w9"
    assert sent["message"].msg_content.instruction.instruction == "stop"


# -- tasks sent to one named worker --------------------------------------------------


def _publish(agent, task_id: str, *, worker_name=None, sequence=5):
    from custom_agents.delegated_work_runtime import record_publication

    return record_publication(
        agent,
        subtask_id=task_id,
        parent_task_id="parent_1",
        named_agent_type="WebResearchAgent",
        target_agent_name=worker_name,
        publish_sequence=sequence,
    )


def _subtask_info(task_id: str, *, route: str, worker_name=None):
    from custom_messages.contracts.routing_v1 import MessageMeta

    return SimpleNamespace(
        task=Task(task_id=task_id, parent_task_id="parent_1", task_type=MessageMetaType.REQUEST),
        header=MessageHeader(from_agent_id="lead", to_routing_path=route),
        meta=MessageMeta(to_named_agent_type="WebResearchAgent", to_agent_name=worker_name),
    )


@pytest.mark.asyncio
async def test_only_tasks_waiting_in_a_shared_queue_skip_the_watchdog(tmp_path, monkeypatch):
    """A task sent to one named worker is probed and timed out while it waits:
    only that worker can take it (fixed 2026-10-05)."""
    from custom_agents.delegated_work_runtime import delegated_tasks
    from custom_agents.subtask_watchdog_runtime import _delegated_holders, _queue_tracking

    agent = _requester(tmp_path, monkeypatch)
    agent._untracked_subtask_warnings = set()
    _publish(agent, "direct_1", worker_name="research-7")
    holders = await _delegated_holders(agent)
    shared = _subtask_info("sub_1", route="team_t_WebResearchAgent")
    direct = _subtask_info("direct_1", route="worker_7", worker_name="research-7")
    assert await _queue_tracking(agent, holders, shared) == (True, None)
    assert await _queue_tracking(agent, holders, direct) == (False, None)
    claimed_at = datetime.now(timezone.utc)
    delegated_tasks(agent).record_claim(
        team_id="team_t", task_id="sub_1", worker_agent_id="w1", worker_agent_name="w1", claimed_at=claimed_at
    )
    holders = await _delegated_holders(agent)
    assert await _queue_tracking(agent, holders, shared) == (False, claimed_at.isoformat())


@pytest.mark.asyncio
async def test_a_stop_for_a_named_worker_task_it_already_took_goes_to_that_worker(tmp_path, monkeypatch):
    from custom_agents.delegated_work_runtime import stop_delegated_task

    agent = _requester(tmp_path, monkeypatch)
    agent.team_comms.delete_work_message = AsyncMock(return_value=False)  # already in its inbox
    _publish(agent, "direct_1", worker_name="research-7", sequence=9)
    reached = await stop_delegated_task(
        agent,
        parent_task_id="parent_1",
        subtask_info=_subtask_info("direct_1", route="worker_7", worker_name="research-7"),
        reason="no longer needed",
    )
    assert reached
    agent.team_comms.delete_work_message.assert_awaited_once_with(9)
    sent = agent.team_comms.send_message.await_args.kwargs
    assert sent["routing_key"] == "worker_7"


@pytest.mark.asyncio
async def test_a_stop_for_a_shared_queue_task_is_never_published_into_the_queue(tmp_path, monkeypatch):
    from custom_agents.delegated_work_runtime import stop_delegated_task

    agent = _requester(tmp_path, monkeypatch)
    agent.team_comms.delete_work_message = AsyncMock(return_value=False)  # a worker took it just now
    reached = await stop_delegated_task(
        agent,
        parent_task_id="parent_1",
        subtask_info=_subtask_info("sub_1", route="team_t_WebResearchAgent"),
        reason="no longer needed",
    )
    # Its worker is told when it claims the ended task.
    assert reached
    agent.team_comms.send_message.assert_not_awaited()


@pytest.mark.asyncio
async def test_a_retried_publish_follows_a_named_worker_to_its_new_mailbox(tmp_path, monkeypatch):
    from custom_agents.turn_action_outbox_runtime import _current_exact_route

    agent = _requester(tmp_path, monkeypatch)
    agent.is_team_lead = True
    agent._probe_live_named_agent_type = AsyncMock(return_value=SimpleNamespace(self_id="worker_8"))
    header = MessageHeader(from_agent_id="lead", to_routing_path="worker_7")
    route = await _current_exact_route(agent, header, "worker_7", "WebResearchAgent", "research-7")
    assert route == header.to_routing_path == "worker_8"
    agent._probe_live_named_agent_type = AsyncMock(return_value=None)
    with pytest.raises(RuntimeError, match="not live"):
        await _current_exact_route(agent, header, "worker_8", "WebResearchAgent", "research-7")
