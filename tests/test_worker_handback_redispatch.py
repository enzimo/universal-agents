"""The TeamLead sends returned tasks out again for another worker to run."""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

import custom_agents.worker_handback_runtime as runtime
from custom_agents.delegated_work_runtime import delegated_tasks, record_publication
from custom_agents.task_manager import TaskManager
from custom_messages.contracts.redispatch_v1 import (
    DispatchedActionNoteV1,
    TaskRedispatchNoticeV1,
    render_redispatch_directive,
)
from custom_messages.contracts.routing_v1 import GatewayContext, MessageHeader, MessageMeta
from custom_messages.contracts.task_v1 import Task
from custom_messages.contracts.worker_handback_v1 import (
    HandedBackTaskV1,
    WorkerLeaseLossEvidenceV1,
    WorkerTaskHandbackV1,
    handback_identity,
)
from custom_messages.enums import MessageMetaType, ProgressState
from storage.worker_handback_repository import (
    HANDBACK_STATUS_CLOSED_CANCELLED,
    HANDBACK_STATUS_CLOSED_FINISHED,
    HANDBACK_STATUS_REDISPATCHED,
    WorkerHandbackRepository,
)

WORKER_TYPE = "WebResearchAgent"
NOW = lambda: datetime.now(timezone.utc).isoformat()  # noqa: E731


class _Comms:
    def __init__(self, failures: int = 0) -> None:
        self.sent = []
        self.failures = failures

    async def send_message(self, *, message, routing_key):
        if self.failures:
            self.failures -= 1
            raise RuntimeError("nats unavailable")
        self.sent.append((routing_key, message))
        return 100 + len(self.sent)  # the stream sequence of the published message


@pytest.fixture
def lead(tmp_path, monkeypatch):
    manager = TaskManager(db_path=str(tmp_path / "lead.db"))
    manager.set_owner_identity("lead", "Lead", "team", "TeamArchitect")
    launches = SimpleNamespace(
        get=lambda launch_id, team_id: (
            SimpleNamespace(agent_id="worker_1", owner_canonical_user_id="alice")
            if launch_id == "launch_1"
            else None
        )
    )
    monkeypatch.setattr(runtime, "AgentLaunchRepository", lambda _settings: launches)
    monkeypatch.setattr(runtime, "interruption_limit", lambda agent: 3)
    occurrences: dict[str, object] = {}
    agent = SimpleNamespace(
        id="lead",
        team_id="team",
        is_team_lead=True,
        task_manager=manager,
        team_comms=_Comms(),
        named_agent_type_queue_map={WORKER_TYPE: "team_WebResearchAgent"},
        _logger=logging.getLogger("test.redispatch"),
        _schedule_store=SimpleNamespace(
            worker_records=SimpleNamespace(
                for_task=lambda task_id, team_id: occurrences.get(task_id)
            )
        ),
        occurrences=occurrences,
    )
    return agent


def _handback(
    task_id="t1",
    *,
    disposition="interrupted",
    parent_task_id=None,
    route="worker_1",
    launch_id="launch_1",
    actions=(),
) -> WorkerTaskHandbackV1:
    return WorkerTaskHandbackV1(
        handback_id=handback_identity(
            worker_agent_id="worker_1", launch_id=launch_id, task_id=task_id
        ),
        team_id="team",
        worker_agent_id="worker_1",
        worker_named_agent_type=WORKER_TYPE,
        evidence=WorkerLeaseLossEvidenceV1(
            launch_id=launch_id,
            lease_lost_at_utc=datetime.now(timezone.utc),
            reason="stale_launch_lease",
        ),
        handed_back=HandedBackTaskV1(
            task_id=task_id,
            disposition=disposition,
            prior_state="in_progress" if disposition == "interrupted" else "accepted",
            task=Task(
                task_id=task_id,
                parent_task_id=parent_task_id,
                task_type=MessageMetaType.REQUEST,
                problem_statement="Summarize the weekly report",
            ),
            header=MessageHeader(
                from_agent_id="gateway",
                to_routing_path=route,
                response_routing_key="gateway",
                idempotency_key="original_key",
            ),
            meta=MessageMeta(
                to_agent_name="Worker",
                gateway_context=GatewayContext(canonical_user_id="alice"),
            ),
            dispatched_actions=actions,
        ),
    )


async def _receive(lead, handback):
    return await runtime.handle_worker_task_handback(
        lead,
        handback=handback,
        msg_header=MessageHeader(from_agent_id="worker_1"),
        msg_meta=MessageMeta(),
    )


def _status(lead, handback):
    return WorkerHandbackRepository(lead.task_manager.storage_settings).status(
        handback.handback_id, "team"
    )


@pytest.mark.asyncio
async def test_returned_task_goes_to_the_type_queue_with_its_reply_route(lead):
    note = DispatchedActionNoteV1(
        action_id="a1", action_type="notify_user", status="dispatched", summary="Sent draft"
    )
    handback = _handback(actions=(note,))

    assert await _receive(lead, handback)

    # No worker is started here: the pool controller starts one for the waiting task.
    [(route, envelope)] = lead.team_comms.sent
    assert route == "team_WebResearchAgent"
    header, meta = envelope.msg_header, envelope.msg_meta
    assert header.to_routing_path == "team_WebResearchAgent"
    assert header.response_routing_key == "gateway"
    assert header.from_agent_id == "gateway"
    assert header.idempotency_key == f"redispatch:{handback.handback_id}"
    assert header.message_id != handback.handed_back.header.message_id
    assert meta.to_named_agent_type == WORKER_TYPE
    assert meta.to_agent_name is None
    assert meta.redispatch_notice.prior_disposition == "interrupted"
    assert meta.redispatch_notice.interruption_count == 1
    assert meta.redispatch_notice.dispatched_actions == (note,)
    assert envelope.msg_content.task_id == "t1"
    assert _status(lead, handback) == HANDBACK_STATUS_REDISPATCHED


@pytest.mark.asyncio
async def test_cancelled_or_finished_tasks_are_not_sent_out_again(lead):
    manager = lead.task_manager
    manager.add_task(
        Task(task_id="done", task_type=MessageMetaType.REQUEST),
        MessageHeader(),
        MessageMeta(),
        owner_person_id=None,
    )
    manager.update_main_task_state("done", ProgressState.IN_PROGRESS, NOW())
    manager.task_state.tasks["done"].update_state(ProgressState.COMPLETED, NOW())
    cancelled = _handback("stopped")
    finished = _handback("done")
    manager.is_execution_cancelled = lambda task_id: task_id == "stopped"

    await _receive(lead, cancelled)
    await _receive(lead, finished)

    assert lead.team_comms.sent == []
    assert _status(lead, cancelled) == HANDBACK_STATUS_CLOSED_CANCELLED
    assert _status(lead, finished) == HANDBACK_STATUS_CLOSED_FINISHED


@pytest.mark.asyncio
async def test_lead_subtask_goes_back_to_waiting_for_pickup(lead):
    manager = lead.task_manager
    manager.add_task(
        Task(task_id="parent", task_type=MessageMetaType.REQUEST),
        MessageHeader(),
        MessageMeta(),
        owner_person_id=None,
    )
    manager.add_subtask(
        "parent",
        Task(task_id="child", parent_task_id="parent", task_type=MessageMetaType.REQUEST),
        MessageHeader(to_routing_path="team_WebResearchAgent"),
        MessageMeta(to_named_agent_type=WORKER_TYPE),
    )
    old = (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat()
    manager.update_main_task_state("child", ProgressState.IN_PROGRESS, old)
    record_publication(
        lead,
        subtask_id="child",
        parent_task_id="parent",
        named_agent_type=WORKER_TYPE,
        target_agent_name=None,
        publish_sequence=7,
    )
    delegated_tasks(lead).record_claim(
        team_id="team",
        task_id="child",
        worker_agent_id="worker_1",
        worker_agent_name="worker_1",
        claimed_at=datetime.now(timezone.utc),
    )

    await _receive(lead, _handback("child", parent_task_id="parent"))

    child = manager.get_task_info("child")
    assert child.state == ProgressState.ACCEPTED
    assert datetime.fromisoformat(child.task.task_state[-1][1]) > datetime.fromisoformat(old)
    # Waiting in the queue again under the new message, so a stop withdraws it
    # and nothing probes it until a worker claims it.
    queued = delegated_tasks(lead).get("team", "child")
    assert (queued.status, queued.holder_agent_id, queued.publish_sequence) == ("queued", None, 101)


@pytest.mark.asyncio
async def test_a_claim_during_the_redispatch_survives_and_a_retry_keeps_it(lead):
    """Regression (2026-10-17): the lead requeued its subtask after publishing the
    re-sent task, so a claim from the new worker that arrived first was turned back
    into "queued" with no holder; a retried handback did the same later."""

    manager = lead.task_manager
    manager.add_task(
        Task(task_id="parent", task_type=MessageMetaType.REQUEST), MessageHeader(), MessageMeta(),
        owner_person_id=None,
    )
    manager.add_subtask(
        "parent",
        Task(task_id="child", parent_task_id="parent", task_type=MessageMetaType.REQUEST),
        MessageHeader(to_routing_path="team_WebResearchAgent"),
        MessageMeta(to_named_agent_type=WORKER_TYPE),
    )
    record_publication(
        lead, subtask_id="child", parent_task_id="parent", named_agent_type=WORKER_TYPE,
        target_agent_name=None, publish_sequence=7,
    )
    claims = delegated_tasks(lead)
    claims.record_claim(
        team_id="team", task_id="child", worker_agent_id="worker_1", worker_agent_name="worker_1",
        claimed_at=datetime.now(timezone.utc),
    )
    comms = lead.team_comms
    real_send = comms.send_message

    async def send_and_get_claimed(*, message, routing_key):
        sequence = await real_send(message=message, routing_key=routing_key)
        # worker_2 takes the re-sent task and its claim is handled before publish returns.
        claims.record_claim(
            team_id="team", task_id="child", worker_agent_id="worker_2", worker_agent_name="worker_2",
            claimed_at=datetime.now(timezone.utc),
        )
        return sequence

    comms.send_message = send_and_get_claimed
    handback = _handback("child", parent_task_id="parent")
    await _receive(lead, handback)
    record = claims.get("team", "child")
    assert (record.status, record.holder_agent_id) == ("running", "worker_2")

    # The handback is processed again (its first attempt could not settle).
    comms.send_message = real_send
    repository = WorkerHandbackRepository(manager.storage_settings)
    await runtime._process_handback(lead, repository, handback)
    record = claims.get("team", "child")
    assert (record.status, record.holder_agent_id) == ("running", "worker_2")


def test_interruptions_count_only_interrupted_handbacks(lead, tmp_path):
    repository = WorkerHandbackRepository(lead.task_manager.storage_settings)
    repository.record(_handback(launch_id="launch_1"))
    repository.record(_handback(launch_id="launch_2"))
    repository.record(_handback(disposition="not_started", launch_id="launch_3"))
    assert repository.interruption_count("team", "t1") == 2
    assert repository.interruption_count("team", "other") == 0
    with pytest.raises(ValueError):
        repository.set_status("x", "team", "bogus")


# --- receiving worker -------------------------------------------------------


def _notice(disposition="interrupted", actions=()):
    return TaskRedispatchNoticeV1(
        handback_id="handback_1",
        prior_disposition=disposition,
        prior_worker_agent_id="worker_1",
        interruption_count=2,
        interrupted_at_utc=datetime.now(timezone.utc),
        dispatched_actions=actions,
    )


def test_directive_lists_actions_already_sent_by_the_interrupted_run():
    note = DispatchedActionNoteV1(
        action_id="a1", action_type="notify_user", status="dispatched", summary="Sent draft"
    )
    text = render_redispatch_directive(_notice(actions=(note,)))
    assert "interruption 2" in text
    assert "- notify_user (dispatched): Sent draft" in text
    assert "Check whether each action above already took effect" in text
    assert render_redispatch_directive(_notice(disposition="not_started")) is None
