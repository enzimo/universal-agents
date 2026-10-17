"""Guardrails between rerunning an interrupted scheduled run and the next fire."""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

import custom_agents.worker_handback_runtime as runtime
from custom_agents import scheduled_runtime
from custom_agents.schedule_store import ScheduleStore
from custom_agents.task_operation_runtime import TaskOperationRuntime
from custom_messages.contracts.redispatch_v1 import TaskRedispatchNoticeV1
from custom_messages.contracts.schedule_worker_v1 import ScheduleOccurrenceV1
from storage.config import resolve_storage_settings
from storage.schedule_worker_repository import ScheduleWorkerRepository
from storage.worker_handback_repository import (
    HANDBACK_STATUS_SUPERSEDED,
)

TEAM = "team"


def _now() -> datetime:
    return datetime.now(timezone.utc)


@pytest.fixture
def records(tmp_path) -> ScheduleWorkerRepository:
    settings = resolve_storage_settings(
        {"backend": "sqlite", "sqlite": {"path": str(tmp_path / "runtime.db")}}
    )
    store = ScheduleStore(storage_settings=settings)
    with store.worker_records._connect() as conn:
        ScheduleWorkerRepository.ensure_occurrence_schema(conn)
        conn.commit()
    return store.worker_records


def _occurrence(
    occurrence_id: str,
    *,
    due: datetime,
    agent_id: str = "worker_a",
    state: str = "admitted",
    coalesced_into: str | None = None,
    finished: bool = False,
) -> ScheduleOccurrenceV1:
    return ScheduleOccurrenceV1(
        occurrence_id=occurrence_id,
        schedule_id="sched_1",
        due_at=due.isoformat(),
        task_id=f"task_{occurrence_id}",
        team_id=TEAM,
        owner_canonical_user_id="alice",
        named_agent_type="WebResearchAgent",
        execution_digest="digest",
        agent_id=agent_id,
        claim_token="claim",
        state=state,
        admitted_at_utc=due,
        finished_at_utc=_now() if finished else None,
        coalesced_into_task_id=coalesced_into,
    )


def _insert(records: ScheduleWorkerRepository, record: ScheduleOccurrenceV1) -> None:
    with records._connect() as conn:
        conn.execute(
            "INSERT INTO schedule_occurrences(occurrence_id,schedule_id,task_id,team_id,"
            "owner_id,agent_id,payload_json) VALUES (?,?,?,?,?,?,?)",
            (
                record.occurrence_id,
                record.schedule_id,
                record.task_id,
                record.team_id,
                record.owner_canonical_user_id,
                record.agent_id,
                record.model_dump_json(),
            ),
        )
        conn.commit()


# --- repository -------------------------------------------------------------


def test_run_moves_only_from_its_current_owner(records):
    base = _now()
    _insert(records, _occurrence("o1", due=base))
    _insert(records, _occurrence("o2", due=base, state="coalesced", coalesced_into="task_o1"))

    assert records.reassign_run("task_o1", TEAM, from_agent_id="someone_else", to_agent_id="worker_b", launch_id="l2") == 0
    assert records.reassign_run("task_o1", TEAM, from_agent_id="worker_a", to_agent_id="worker_b", launch_id="l2") == 2
    moved = records.for_task("task_o1", TEAM)
    assert (moved.agent_id, moved.launch_id) == ("worker_b", "l2")
    assert records.unfinished_occurrences("sched_1", TEAM)[0].agent_id == "worker_b"


# --- lead: rerun decision ---------------------------------------------------


def _lead_store(records, *, row=None, ran_again=False, window=900.0, monkeypatch=None):
    monkeypatch.setattr(
        "custom_agents.agent_instantiation_service.effective_worker_rules",
        lambda agent, agent_type, overrides: SimpleNamespace(
            interrupted_run_skip_window_seconds=window
        ),
    )
    worker_records = SimpleNamespace(
        ran_again_since=lambda schedule_id, team_id, due_at: ran_again,
        get_binding=lambda schedule_id, team_id: None,
        for_task=records.for_task,
        supersede_run=records.supersede_run,
    )
    return SimpleNamespace(worker_records=worker_records, get_row=lambda schedule_id: row)


def _row(*, enabled=True, next_in=None, paused_for=None) -> dict:
    now = _now()
    return {
        "schedule_id": "sched_1",
        "enabled": enabled,
        "next_fire_at": (now + next_in).isoformat() if next_in is not None else None,
        "paused_until": (now + paused_for).isoformat() if paused_for is not None else None,
    }


@pytest.mark.asyncio
async def test_skipped_rerun_closes_the_run(records, monkeypatch):
    _insert(records, _occurrence("o1", due=_now()))
    store = _lead_store(records, row=_row(next_in=timedelta(minutes=5)), monkeypatch=monkeypatch)
    agent = SimpleNamespace(
        id="lead",
        team_id=TEAM,
        _schedule_store=store,
        task_manager=SimpleNamespace(operations_runtime=TaskOperationRuntime()),
        _logger=logging.getLogger("test.guard"),
    )
    status = await runtime._scheduled_run_status(agent, "task_o1")
    assert status == HANDBACK_STATUS_SUPERSEDED
    assert records.for_task("task_o1", TEAM).state == "superseded"


# --- worker: taking over a rerun --------------------------------------------


def _notice() -> TaskRedispatchNoticeV1:
    return TaskRedispatchNoticeV1(
        handback_id="h1",
        prior_disposition="interrupted",
        prior_worker_agent_id="worker_a",
        interruption_count=1,
        interrupted_at_utc=_now(),
    )


def _worker(records):
    removed = []
    manager = SimpleNamespace(
        operations_runtime=TaskOperationRuntime(),
        remove_task=lambda task_id: removed.append(task_id) or True,
    )
    return SimpleNamespace(
        id="worker_b",
        team_id=TEAM,
        _schedule_store=SimpleNamespace(worker_records=records),
        _worker_launch=SimpleNamespace(launch_id="launch_b"),
        task_manager=manager,
        _logger=logging.getLogger("test.takeover"),
        removed=removed,
    )


@pytest.mark.asyncio
async def test_rerun_takes_over_the_run_receipt(records):
    _insert(records, _occurrence("o1", due=_now()))
    worker = _worker(records)
    assert await runtime._take_over_scheduled_run(worker, "task_o1", _notice())
    assert records.for_task("task_o1", TEAM).agent_id == "worker_b"
    assert worker.removed == []


@pytest.mark.asyncio
async def test_rerun_overtaken_by_a_newer_run_is_dropped(records):
    base = _now() - timedelta(hours=1)
    _insert(records, _occurrence("o1", due=base))
    _insert(records, _occurrence("o2", due=base + timedelta(minutes=30), agent_id="worker_c"))
    worker = _worker(records)
    assert not await runtime._take_over_scheduled_run(worker, "task_o1", _notice())
    assert records.for_task("task_o1", TEAM).state == "superseded"
    assert worker.removed == ["task_o1"]


# --- firing time: overlap guard ---------------------------------------------


def _firing_agent(records, *, stale=()):
    persisted = []
    manager = SimpleNamespace(
        operations_runtime=TaskOperationRuntime(),
        _persist_runtime_state=lambda **kwargs: persisted.append(kwargs["schedule_admission"]),
    )
    return SimpleNamespace(
        id="worker_c",
        team_id=TEAM,
        _schedule_store=SimpleNamespace(worker_records=records),
        task_manager=manager,
        _logger=logging.getLogger("test.fire"),
        persisted=persisted,
        stale=list(stale),
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("owner_live", [True, False])
async def test_fire_merges_into_an_unfinished_run_on_a_live_worker(
    records, monkeypatch, owner_live
):
    _insert(records, _occurrence("o1", due=_now() - timedelta(minutes=30)))
    agent = _firing_agent(records)

    async def stale_owners(_agent, owner_ids, *, purpose):
        assert purpose == "schedule_coalescing"
        return [] if owner_live else list(owner_ids)

    monkeypatch.setattr("custom_agents.agent_liveness.gone_owner_ids", stale_owners)
    new_fire = _occurrence("o2", due=_now(), agent_id="worker_c", state="reserved")

    merged = await scheduled_runtime._coalesce_into_run_elsewhere(
        agent, schedule_id="sched_1", schedule_admission=new_fire
    )

    assert merged is owner_live
    if owner_live:
        [receipt] = agent.persisted
        assert receipt.occurrence_id == "o2"
        assert receipt.coalesced_into_task_id == "task_o1"
    else:
        assert agent.persisted == []


@pytest.mark.asyncio
async def test_local_merge_notifies_the_user_once(monkeypatch):
    from custom_agents.task_manager import TaskInfo
    from custom_messages.contracts.routing_v1 import GatewayContext, MessageHeader, MessageMeta
    from custom_messages.contracts.task_v1 import Task
    from custom_messages.enums import MessageMetaType

    active = TaskInfo(
        Task(task_id="running", task_type=MessageMetaType.REQUEST),
        MessageHeader(),
        MessageMeta(gateway_context=GatewayContext(scheduled_job_id="sched_1")),
        owner_person_id=None,
    )
    history = []
    send = AsyncMock()
    agent = SimpleNamespace(
        id="worker_a",
        name="Worker",
        task_manager=SimpleNamespace(
            operations_runtime=TaskOperationRuntime(),
            list_task_infos=lambda: [active],
            is_terminal_state=lambda state: False,
            append_history_entry=lambda task_id, text, **kwargs: history.append(kwargs["details"]),
        ),
        team_comms=SimpleNamespace(send_message=send),
        _logger=logging.getLogger("test.local"),
    )
    header = MessageHeader(response_routing_key="gateway_route")
    meta = MessageMeta(gateway_context=GatewayContext(canonical_user_id="alice"))

    merged = await scheduled_runtime._coalesce_scheduled_fire_if_active(
        agent,
        "sched_1",
        schedule_id="sched_1",
        schedule_name="Weekly report",
        notification_header=header,
        notification_meta=meta,
    )

    assert merged
    send.assert_awaited_once()
    assert send.await_args.kwargs["routing_key"] == "gateway_route"
    assert any(item.get("event") == "scheduled_fire_coalesced_user_notified" for item in history)
