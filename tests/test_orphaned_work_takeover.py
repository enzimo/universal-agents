"""A dead agent's unfinished work is handed to a new agent of its type, and only a dead one's.

Real SQLite launch and snapshot stores with the scheduled-worker test lead (its
launcher records starts instead of running threads). Before 2026-10-04 such
work waited until some unrelated start of that type happened, and takeover
trusted a 30-second heartbeat alone. Startup agents and the one shared rule are
covered in tests/test_agent_liveness.py.
"""
# ruff: noqa: F811 -- imported pytest fixture is injected by name

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from custom_agents.agent_liveness import OwnerEvidence, gone_owner_ids, owner_liveness
from custom_agents.agent_state_store import AgentStateStore
from custom_agents.liveness_observation import observe_liveness
from custom_agents.orphaned_work_takeover import take_over_orphaned_work
from custom_messages.contracts.routing_v1 import GatewayContext, MessageHeader, MessageMeta
from custom_messages.contracts.runtime_snapshot_v3 import RuntimeSnapshotV3, TaskSnapshotRecord
from custom_messages.contracts.schedule_worker_v1 import AgentLaunchState
from custom_messages.contracts.task_v1 import Task
from custom_messages.enums import MessageMetaType, ProgressState
from storage.agent_launch_repository import AgentLaunchRepository
from storage.agent_launch_repository import LaunchCapacity
from tests.test_schedule_worker_service import lead  # noqa: F401
from tests.test_schedule_worker_transactions import make_launch
from tests.test_stuck_tasks import seen
from tests.broker_access_support import context_signer
from tests.change_approval_support import use_signed_contexts

DEAD = "agent_dead_worker"


@pytest.mark.asyncio
@pytest.mark.parametrize("reports_after_wake", [True, False])
async def test_resume_does_not_replace_a_startup_worker_with_an_old_heartbeat(lead, monkeypatch, reports_after_wake):
    use_signed_contexts(monkeypatch)
    clock = [100.0]
    monkeypatch.setattr("custom_agents.liveness_observation.time", SimpleNamespace(monotonic=lambda: clock[0]))
    lead._agent_state_store = AgentStateStore(storage_settings=lead.task_manager.storage_settings)
    lead.is_team_lead = True
    lead.schedule_maintenance = {}
    held_work(lead, signed=True)
    before = datetime.now(timezone.utc) - timedelta(minutes=18)
    woke = before + timedelta(minutes=18)
    seen(lead, DEAD, "Worker")
    status = lead._agent_state_store.get_by_id(DEAD)
    status.last_seen_at = before.isoformat()
    lead._agent_state_store.upsert(status)
    observe_liveness(lead, now=before)
    clock[0] += 1
    observe_liveness(lead, now=woke)
    assert await take_over_orphaned_work(lead, now=woke, clock=1.0) == 0
    assert await gone_owner_ids(lead, [DEAD], purpose="test") == []
    assert lead.starts == []

    # After the wait, actual heartbeat evidence decides whether to replace it.
    if reports_after_wake:
        status.last_seen_at = (woke + timedelta(seconds=5)).isoformat()
        lead._agent_state_store.upsert(status)
    clock[0] += 60
    observe_liveness(lead, now=woke + timedelta(seconds=60))
    replacements = 0 if reports_after_wake else 1
    assert await take_over_orphaned_work(lead, now=woke + timedelta(seconds=60), clock=62.0) == replacements
    assert len(lead.starts) == replacements
    assert await gone_owner_ids(lead, [DEAD], purpose="test") == ([] if reports_after_wake else [DEAD])


@pytest.mark.asyncio
async def test_a_proven_worker_exit_is_replaced_during_resume_grace(lead, monkeypatch):
    clock = [100.0]
    monkeypatch.setattr("custom_agents.liveness_observation.time", SimpleNamespace(monotonic=lambda: clock[0]))
    lead.is_team_lead = True
    lead.schedule_maintenance = {}
    launch_record(lead, AgentLaunchState.FAILED)
    held_work(lead)
    before = datetime.now(timezone.utc)
    observe_liveness(lead, now=before)
    clock[0] += 1
    observe_liveness(lead, now=before + timedelta(minutes=18))
    assert await take_over_orphaned_work(lead, clock=1.0) == 1
    assert await gone_owner_ids(lead, [DEAD], purpose="test") == [DEAD]


def launch_record(lead, state: AgentLaunchState, agent_id: str = DEAD):
    repository = AgentLaunchRepository(lead.task_manager.storage_settings)
    record, _ = repository.reserve(make_launch(agent_id=agent_id), LaunchCapacity.defaults(), join=False)
    starting = repository.update(record, state=AgentLaunchState.STARTING)
    if state == AgentLaunchState.STARTING:
        return starting
    return repository.update(starting, state=state)


def held_work(lead, owner: str = DEAD, state: ProgressState = ProgressState.WAITING, *, signed=False):
    task = Task(task_id="work-1", task_type=MessageMetaType.REQUEST, problem_statement="Fetch candles")
    snapshot = RuntimeSnapshotV3(
        schema_version=3,
        tasks=[
            TaskSnapshotRecord(
                task_id="work-1", state=state, task=task,
                header=MessageHeader(response_routing_key="gateway"),
                meta=MessageMeta(gateway_context=GatewayContext(
                    canonical_user_id="owner",
                    task_context=context_signer().issue(team_id="team", person_id="owner") if signed else None,
                )),
                owner_person_id="owner" if signed else None,
            )
        ],
    )
    lead.task_manager.runtime_state_store.save_task_runtime(
        expect_existing=False,
        owner_agent_id=owner, owner_agent_name=owner, team_id="team",
        owner_named_agent_type="Worker", runtime_state_json=snapshot.model_dump_json(),
    )


def test_a_launch_record_decides_whether_its_worker_is_gone(lead):
    assert owner_liveness(lead, "agent_without_launch").launch is None
    launch_record(lead, AgentLaunchState.FAILED)
    ended = owner_liveness(lead, DEAD)
    assert (ended.evidence, ended.gone) == (OwnerEvidence.LAUNCH_ENDED, True)
    launch_record(lead, AgentLaunchState.READY, agent_id="agent_alive")
    lead._local_team_runtime.inspect_launch = lambda record: "ready"
    running = owner_liveness(lead, "agent_alive")
    assert (running.evidence, running.gone) == (OwnerEvidence.LAUNCH_ACTIVE, False)
    lead._local_team_runtime.inspect_launch = lambda record: "absent"
    absent = owner_liveness(lead, "agent_alive")
    assert (absent.evidence, absent.gone) == (OwnerEvidence.LAUNCH_PROCESS_ABSENT, True)


@pytest.mark.asyncio
async def test_a_slow_worker_with_a_stale_heartbeat_keeps_its_work(lead):
    """Takeover used to trust a 30-second heartbeat alone."""
    launch_record(lead, AgentLaunchState.READY, agent_id="agent_slow")
    lead._local_team_runtime.inspect_launch = lambda record: "ready"
    lead._agent_state_store = None
    assert await gone_owner_ids(lead, ["agent_slow"], purpose="test") == []
    lead._local_team_runtime.inspect_launch = lambda record: "absent"
    assert await gone_owner_ids(lead, ["agent_slow"], purpose="test") == ["agent_slow"]


@pytest.mark.asyncio
async def test_the_lead_starts_one_replacement_for_a_dead_workers_work(lead):
    launch_record(lead, AgentLaunchState.FAILED)
    held_work(lead)
    lead.is_team_lead = True
    lead.same_role_runtime_takeover_enabled = True
    lead.schedule_maintenance = {}
    assert await take_over_orphaned_work(lead, clock=1.0) == 1
    (started,) = [s for s in lead.starts if s.named_agent_type == "Worker"]
    assert started.owner_canonical_user_id == "owner"
    assert await take_over_orphaned_work(lead, clock=2.0) == 0  # paced
    assert await take_over_orphaned_work(lead, clock=100.0) == 0  # the same start, not a second one
    assert len([s for s in lead.starts if s.named_agent_type == "Worker"]) == 1


@pytest.mark.asyncio
async def test_a_live_or_finished_owner_gets_no_replacement(lead):
    launch_record(lead, AgentLaunchState.READY)
    lead._local_team_runtime.inspect_launch = lambda record: "ready"
    held_work(lead)
    lead.is_team_lead = True
    lead.schedule_maintenance = {}
    assert await take_over_orphaned_work(lead, clock=1.0) == 0
    held_work(lead, state=ProgressState.COMPLETED)
    lead._local_team_runtime.inspect_launch = lambda record: "absent"
    assert await take_over_orphaned_work(lead, clock=100.0) == 0
    assert not [s for s in lead.starts if s.named_agent_type == "Worker"]


@pytest.mark.asyncio
async def test_takeover_can_be_turned_off(lead):
    launch_record(lead, AgentLaunchState.FAILED)
    held_work(lead)
    lead.is_team_lead = True
    lead.same_role_runtime_takeover_enabled = False
    assert await take_over_orphaned_work(lead, clock=1.0) == 0
