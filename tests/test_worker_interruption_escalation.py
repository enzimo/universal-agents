"""Work interrupted too many times in a row is stopped and reviewed by the lead."""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

import custom_agents.worker_handback_runtime as runtime
import custom_agents.worker_interruption_escalation as escalation
from custom_agents.schedule_store import ScheduleStore
from custom_agents.task_manager import TaskManager
from custom_messages.contracts.routing_v1 import GatewayContext, MessageHeader, MessageMeta
from custom_messages.contracts.task_v1 import Task
from custom_messages.contracts.worker_handback_v1 import (
    HandedBackTaskV1,
    WorkerLeaseLossEvidenceV1,
    WorkerTaskHandbackV1,
    handback_identity,
)
from custom_messages.enums import MessageMetaType
from storage.config import resolve_storage_settings
from storage.schedule_occurrences import ensure_occurrence_schema
from storage.worker_handback_repository import (
    HANDBACK_STATUS_ESCALATED,
    HANDBACK_STATUS_REDISPATCHED,
    WorkerHandbackRepository,
)

TEAM = "team"
WORKER_TYPE = "WebResearchAgent"


def _handback(task_id: str, attempt: int, *, parent_task_id=None) -> WorkerTaskHandbackV1:
    launch_id = f"launch_{attempt}"
    return WorkerTaskHandbackV1(
        handback_id=handback_identity(
            worker_agent_id=f"worker_{attempt}", launch_id=launch_id, task_id=task_id
        ),
        team_id=TEAM,
        worker_agent_id=f"worker_{attempt}",
        worker_named_agent_type=WORKER_TYPE,
        evidence=WorkerLeaseLossEvidenceV1(
            launch_id=launch_id,
            lease_lost_at_utc=datetime.now(timezone.utc),
            reason="stale_launch_lease",
            recent_scheduler_errors=(f"OperationalError: database is locked ({attempt})",),
        ),
        handed_back=HandedBackTaskV1(
            task_id=task_id,
            disposition="interrupted",
            prior_state="in_progress",
            task=Task(
                task_id=task_id,
                parent_task_id=parent_task_id,
                task_type=MessageMetaType.REQUEST,
                problem_statement="Compile the report",
            ),
            header=MessageHeader(from_agent_id="gateway", response_routing_key="gateway"),
            meta=MessageMeta(
                conversation_id="conv_user",
                gateway_context=GatewayContext(
                    canonical_user_id="alice", scheduled_job_id="sched_job"
                ),
            ),
        ),
    )


@pytest.fixture
def lead(tmp_path, monkeypatch):
    manager = TaskManager(db_path=str(tmp_path / "lead.db"))
    manager.set_owner_identity("lead", "Lead", TEAM, "TeamArchitect")
    monkeypatch.setattr(
        runtime,
        "AgentLaunchRepository",
        lambda _settings: SimpleNamespace(
            get=lambda launch_id, team_id: SimpleNamespace(
                agent_id="worker_" + launch_id.split("_")[1], owner_canonical_user_id="alice"
            )
        ),
    )
    monkeypatch.setattr(runtime, "interruption_limit", lambda agent: 3)
    cancel_responses = AsyncMock()
    monkeypatch.setattr(escalation, "send_task_cancel_response", cancel_responses)
    settings = resolve_storage_settings(
        {"backend": "sqlite", "sqlite": {"path": str(tmp_path / "schedules.db")}}
    )
    store = ScheduleStore(storage_settings=settings)
    with store.worker_records._connect() as conn:
        ensure_occurrence_schema(conn)
        conn.commit()
    return SimpleNamespace(
        id="lead",
        name="Lead",
        team_id=TEAM,
        is_team_lead=True,
        task_manager=manager,
        team_comms=SimpleNamespace(send_message=AsyncMock()),
        named_agent_type_queue_map={WORKER_TYPE: "team_WebResearchAgent"},
        _logger=logging.getLogger("test.escalation"),
        _probe_live_named_agent_type=AsyncMock(return_value=None),
        _schedule_store=store,
        _handle_task_request=AsyncMock(),
        _fail_subtask_and_reconcile_parent=AsyncMock(),
        cancel_responses=cancel_responses,
    )


async def _receive(lead, handback):
    await runtime.handle_worker_task_handback(
        lead,
        handback=handback,
        msg_header=MessageHeader(from_agent_id=handback.worker_agent_id),
        msg_meta=MessageMeta(),
    )
    return WorkerHandbackRepository(lead.task_manager.storage_settings).status(
        handback.handback_id, TEAM
    )


@pytest.mark.asyncio
async def test_third_interruption_cancels_the_task_and_opens_a_review(lead):
    statuses = [await _receive(lead, _handback("t1", attempt)) for attempt in (1, 2, 3)]

    assert statuses == [
        HANDBACK_STATUS_REDISPATCHED,
        HANDBACK_STATUS_REDISPATCHED,
        HANDBACK_STATUS_ESCALATED,
    ]
    assert lead.team_comms.send_message.await_count == 2
    assert lead.task_manager.is_execution_cancelled("t1")
    lead.cancel_responses.assert_awaited_once()
    assert "interrupted 3 times in a row" in lead.cancel_responses.await_args.args[4]

    lead._handle_task_request.assert_awaited_once()
    kwargs = lead._handle_task_request.await_args.kwargs
    review, meta = kwargs["task_request"], kwargs["msg_meta"]
    assert review.task_id.startswith(escalation.INTERRUPTION_REVIEW_TASK_PREFIX)
    assert meta.manager_escalation.escalation_type == "manager_review"
    assert meta.manager_escalation.source_task_id == "t1"
    assert meta.manager_escalation.original_requester == "worker_task"
    assert meta.conversation_id is None
    assert meta.gateway_context.canonical_user_id == "alice"
    assert meta.gateway_context.scheduled_job_id is None
    evidence = json.loads(review.problem_statement.split("\n\n", 2)[-1])
    assert evidence["interruptions_in_a_row"] == 3
    assert [item["worker_agent_id"] for item in evidence["interruptions"]] == [
        "worker_1",
        "worker_2",
        "worker_3",
    ]
    assert "notify_user" in review.problem_statement


@pytest.mark.parametrize(("count", "expected"), [(2, False), (3, True), (4, False), (5, False), (6, True)])
def test_reviews_repeat_every_limit_interruptions(count, expected):
    streak = escalation.InterruptionStreak("task", "t", count, 3, ())
    assert streak.should_escalate is expected
