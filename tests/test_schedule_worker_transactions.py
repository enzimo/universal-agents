"""Real storage contention, admission rollback and policy revision fencing."""

from concurrent.futures import ThreadPoolExecutor
import json
import os
from dataclasses import replace
from datetime import timedelta
from threading import Barrier
from uuid import uuid4

import pytest

from custom_agents.schedule_management import schedule_control_state_digest
from custom_agents.schedule_store import ScheduleStore
from custom_messages.contracts.routing_v1 import GatewayContext, MessageMeta
from custom_messages.contracts.schedule_v1 import ScheduledMessage
from custom_messages.contracts.schedule_worker_v1 import (
    AgentLaunchState,
    AgentLaunchV1,
    ScheduleOccurrenceV1,
    ScheduleWorkerBindingV1,
    ScheduleWorkerPolicySpecV1,
    occurrence_identity,
    utc_now,
)
from custom_messages.contracts.task_v1 import Task
from custom_messages.enums import MessageMetaType, ScheduleManagementClass

from storage.agent_launch_repository import AgentLaunchRepository, LaunchCapacity, LaunchConflict
from storage.config import resolve_storage_settings
from storage.runtime_state_repository import RuntimeCustodyLost, create_runtime_state_repository
from storage.schedule_worker_repository import ScheduleWorkerConflict
from tests.runtime_snapshot_fixtures import snapshot_json


@pytest.fixture(
    params=[
        "sqlite",
        pytest.param("postgres", marks=[pytest.mark.live, pytest.mark.integration]),
    ]
)
def stores(tmp_path, request):
    if request.param == "postgres":
        from psycopg.conninfo import make_conninfo
        from storage.postgres_connection import connect_postgres

        dsn = os.environ.get("LIVE_TEST_POSTGRES_DSN")
        if not dsn:
            pytest.skip(
                "LIVE_TEST_POSTGRES_DSN is required for real PostgreSQL contention tests"
            )
        schema = "scheduled_worker_test_" + uuid4().hex
        with connect_postgres(dsn) as conn:
            conn.execute(f"CREATE SCHEMA {schema}")
            conn.commit()
        scoped_dsn = make_conninfo(dsn, options=f"-csearch_path={schema}")
        settings = resolve_storage_settings(
            {"backend": "postgres", "postgres": {"dsn": scoped_dsn}}
        )
    else:
        settings = resolve_storage_settings(
            {"backend": "sqlite", "sqlite": {"path": str(tmp_path / "runtime.db")}}
        )
    try:
        yield (
            ScheduleStore(storage_settings=settings),
            create_runtime_state_repository(settings),
            AgentLaunchRepository(settings),
        )
    finally:
        if request.param == "postgres":
            with connect_postgres(dsn) as conn:
                conn.execute(f"DROP SCHEMA {schema} CASCADE")
                conn.commit()


def make_launch(**changes):
    fields = dict(
        launch_id=uuid4().hex,
        agent_id=uuid4().hex,
        team_id="team",
        owner_canonical_user_id="owner",
        named_agent_type="Worker",
        definition_digest="a" * 64,
        host_id="test:123:boot",
        source="schedule",
        lease_expires_at_utc=utc_now() + timedelta(minutes=2),
    )
    return AgentLaunchV1(**{**fields, **changes})


def seed_schedule(store):
    schedule = ScheduledMessage(
        schedule_id="schedule",
        management_class=ScheduleManagementClass.USER,
        payload=Task(
            task_type=MessageMetaType.REQUEST, problem_statement="Read report"
        ),
        payload_meta=MessageMeta(
            gateway_context=GatewayContext(canonical_user_id="owner")
        ),
        claim_team_id="team",
        claim_named_agent_type="Worker",
        interval_ms=60_000,
        repeat_count=None,
        next_fire_at=(utc_now() - timedelta(minutes=1)).isoformat(),
    )
    store.add(schedule)
    return schedule


def claim_occurrence(store, schedule, agent_id="worker"):
    claim = store.claim_next_due(
        claimer_agent_id=agent_id,
        claimer_named_agent_type="Worker",
        claimer_team_id="team",
    )
    key = occurrence_identity(schedule.schedule_id, schedule.next_fire_at)
    return ScheduleOccurrenceV1(
        occurrence_id=key,
        schedule_id=schedule.schedule_id,
        task_id="task_" + key,
        due_at=schedule.next_fire_at,
        team_id="team",
        owner_canonical_user_id="owner",
        named_agent_type="Worker",
        execution_digest="a" * 64,
        agent_id=agent_id,
        claim_token=claim["claim_token"],
    )


def admit(runtime, occurrence):
    runtime.save_task_runtime(
        expect_existing=False,
        owner_agent_id=occurrence.agent_id,
        owner_agent_name="worker",
        team_id="team",
        owner_named_agent_type="Worker",
        runtime_state_json=snapshot_json(
            occurrence.coalesced_into_task_id or occurrence.task_id
        ),
        schedule_admission=occurrence,
    )


def test_parallel_controllers_join_one_launch(stores):
    _, _, repository = stores
    barrier = Barrier(8)

    def reserve(_):
        barrier.wait()
        return repository.reserve(make_launch(), LaunchCapacity.defaults(), join=True)

    with ThreadPoolExecutor(max_workers=8) as executor:
        results = list(executor.map(reserve, range(8)))
    assert sum(created for _, created in results) == 1
    assert len({launch.launch_id for launch, _ in results}) == 1


def test_unknown_worker_holds_capacity_but_is_never_joined(stores):
    """A start whose process cannot be checked keeps its slot, and new runs
    are refused instead of being tied to it (2026-10-03: one dead start
    absorbed three schedules' runs)."""
    _, _, repository = stores
    one_per_type = replace(LaunchCapacity.defaults(), max_workers_of_type=1)
    launch, _ = repository.reserve(make_launch(), one_per_type, join=True)
    repository.update(launch, state=AgentLaunchState.UNKNOWN)
    for join in (True, False):
        with pytest.raises(LaunchConflict, match="automatic_worker_capacity_limit"):
            repository.reserve(make_launch(), one_per_type, join=join)
    with pytest.raises(LaunchConflict, match="stale_launch_fence"):
        repository.update(launch, state=AgentLaunchState.FAILED)


def test_a_start_in_progress_is_joined(stores):
    _, _, repository = stores
    launch, _ = repository.reserve(make_launch(), LaunchCapacity.defaults(), join=True)
    starting = repository.update(launch, state=AgentLaunchState.STARTING)
    joined, created = repository.reserve(
        make_launch(), LaunchCapacity.defaults(), join=True
    )
    assert joined == starting and not created


def test_expired_worker_cannot_renew_its_lease(stores):
    _, _, repository = stores
    record, _ = repository.reserve(make_launch(), LaunchCapacity.defaults(), join=False)
    starting = repository.update(record, state=AgentLaunchState.STARTING)
    ready = repository.update(
        starting,
        state=AgentLaunchState.READY,
        lease_expires_at_utc=utc_now() - timedelta(seconds=5),
    )
    with pytest.raises(LaunchConflict, match="stale_launch_lease"):
        repository.renew(ready, lease_seconds=120)


def test_occurrence_receipt_and_runtime_snapshot_commit_together(stores):
    store, runtime, _ = stores
    schedule = seed_schedule(store)
    occurrence = claim_occurrence(store, schedule)
    admit(runtime, occurrence)
    assert (
        store.get_scheduled_message(schedule.schedule_id).next_fire_at
        == schedule.next_fire_at
    )
    assert (
        store.worker_records.get_occurrence(occurrence.occurrence_id, "team").task_id
        == occurrence.task_id
    )
    # Simulate death before advancing the schedule. A second owner can acquire
    # the claim, but cannot create a second executable copy of this occurrence.
    store.release_claim(
        schedule.schedule_id,
        claim_token=occurrence.claim_token,
        claimer_agent_id="worker",
    )
    second = claim_occurrence(store, schedule, "replacement")
    with pytest.raises(ScheduleWorkerConflict, match="already_admitted"):
        admit(runtime, second)
    assert runtime.load_task_runtime_by_owner("replacement") == []
    assert len(runtime.load_task_runtime_by_owner("worker")) == 1


def test_stale_claim_rolls_back_new_runtime_custody(stores):
    store, runtime, _ = stores
    schedule = seed_schedule(store)
    occurrence = claim_occurrence(store, schedule)
    store.release_claim(
        schedule.schedule_id,
        claim_token=occurrence.claim_token,
        claimer_agent_id="worker",
    )
    with pytest.raises(ScheduleWorkerConflict, match="stale_schedule_admission_claim"):
        admit(runtime, occurrence)
    assert runtime.load_task_runtime_by_owner("worker") == []
    assert store.worker_records.for_task(occurrence.task_id, "team") is None


def test_recovery_transfers_occurrence_and_runtime_custody(stores):
    store, runtime, _ = stores
    occurrence = claim_occurrence(store, seed_schedule(store))
    admit(runtime, occurrence)
    sources = runtime.load_task_runtime_by_owner("worker")
    runtime.commit_task_runtime_recovery(
        sources=sources, replacement=replace(sources[0], owner_agent_id="replacement")
    )
    assert (
        store.worker_records.for_task(occurrence.task_id, "team").agent_id
        == "replacement"
    )
    assert runtime.load_task_runtime_by_owner("worker") == []


def test_task_manager_admission_preserves_the_transaction_boundary(stores):
    from custom_agents.task_manager import TaskManager
    from custom_messages.contracts.routing_v1 import MessageHeader

    store, _, repository = stores
    occurrence = claim_occurrence(store, seed_schedule(store))
    manager = TaskManager(storage_settings=repository.settings)
    manager.set_owner_identity(
        agent_id="worker",
        agent_name="worker",
        team_id="team",
        named_agent_type="Worker",
    )
    assert manager.add_task(
        Task(
            task_id=occurrence.task_id,
            task_type=MessageMetaType.REQUEST,
            problem_statement="Read report",
        ),
        MessageHeader(),
        MessageMeta(),
        schedule_admission=occurrence,
        owner_person_id=None,
    )
    assert store.worker_records.for_task(occurrence.task_id, "team") is not None
    assert manager.runtime_state_store.load_task_runtime_by_owner("worker")


def test_retarget_defers_behind_live_claim_and_preserves_due_count(stores):
    store, _, _ = stores
    schedule = seed_schedule(store)
    occurrence = claim_occurrence(store, schedule)
    binding = ScheduleWorkerBindingV1(
        schedule_id=schedule.schedule_id,
        team_id="team",
        owner_canonical_user_id="owner",
        target_agent_type="Replacement",
        definition_digest="b" * 64,
        execution_digest="c" * 64,
        revision=1,
        policy=ScheduleWorkerPolicySpecV1(),
        actor_principal_id="lead",
        authority_mode="agreement",
    )
    candidate = schedule.model_copy(update={"claim_named_agent_type": "Replacement"})
    pending = store.worker_records.configure(
        binding,
        candidate,
        expected_revision=0,
        expected_control_digest=schedule_control_state_digest(schedule),
    )
    assert pending
    assert (
        store.get_scheduled_message(schedule.schedule_id).claim_named_agent_type
        == "Worker"
    )
    assert not store.worker_records.apply_pending(schedule.schedule_id, "team")
    store.release_claim(
        schedule.schedule_id,
        claim_token=occurrence.claim_token,
        claimer_agent_id="worker",
    )
    assert store.worker_records.apply_pending(schedule.schedule_id, "team")
    updated = store.get_scheduled_message(schedule.schedule_id)
    assert updated.claim_named_agent_type == "Replacement"
    assert updated.next_fire_at == schedule.next_fire_at
    with pytest.raises(ScheduleWorkerConflict):
        store.worker_records.configure(
            binding,
            candidate,
            expected_revision=0,
            expected_control_digest=schedule_control_state_digest(updated),
        )


def test_retargeted_live_claim_keeps_its_prior_policy_until_admission(stores):
    store, runtime, _ = stores
    schedule = seed_schedule(store)
    binding = ScheduleWorkerBindingV1(
        schedule_id=schedule.schedule_id,
        team_id="team",
        owner_canonical_user_id="owner",
        target_agent_type="Worker",
        definition_digest="b" * 64,
        execution_digest="a" * 64,
        revision=1,
        policy=ScheduleWorkerPolicySpecV1(),
        actor_principal_id="lead",
        authority_mode="agreement",
    )
    store.worker_records.configure(
        binding,
        schedule,
        expected_revision=0,
        expected_control_digest=schedule_control_state_digest(schedule),
    )
    occurrence = claim_occurrence(store, schedule).model_copy(
        update={"policy_revision": 1}
    )
    candidate = schedule.model_copy(update={"claim_named_agent_type": "Replacement"})
    updated = binding.model_copy(
        update={
            "target_agent_type": "Replacement",
            "revision": 2,
            "execution_digest": "c" * 64,
        }
    )
    store.worker_records.configure(
        updated,
        candidate,
        expected_revision=1,
        expected_control_digest=schedule_control_state_digest(schedule),
    )
    assert (
        store.worker_records.get_binding(
            schedule.schedule_id, team_id="team", active=True
        )
        == binding
    )
    admit(runtime, occurrence)
    store.update_after_fire(
        schedule.schedule_id,
        claim_token=occurrence.claim_token,
        claimer_agent_id="worker",
    )
    assert store.worker_records.apply_pending(schedule.schedule_id, "team")
    assert (
        store.worker_records.get_binding(
            schedule.schedule_id, team_id="team", active=True
        )
        == updated
    )


def test_coalesced_occurrence_commits_against_existing_task_custody(stores):
    store, runtime, _ = stores
    occurrence = claim_occurrence(store, seed_schedule(store)).model_copy(
        update={"coalesced_into_task_id": "active-task"}
    )
    runtime.save_task_runtime(
        expect_existing=False,
        owner_agent_id="worker",
        owner_agent_name="worker",
        team_id="team",
        owner_named_agent_type="Worker",
        runtime_state_json=snapshot_json("active-task"),
        schedule_admission=occurrence,
    )
    receipt = store.worker_records.get_occurrence(occurrence.occurrence_id, "team")
    assert (
        receipt.state == "coalesced" and receipt.coalesced_into_task_id == "active-task"
    )
    with pytest.raises(ScheduleWorkerConflict, match="already_admitted"):
        admit(runtime, occurrence)

    sources = runtime.load_task_runtime_by_owner("worker")
    runtime.commit_task_runtime_recovery(
        sources=sources, replacement=replace(sources[0], owner_agent_id="replacement")
    )
    assert (
        store.worker_records.get_occurrence(occurrence.occurrence_id, "team").agent_id
        == "replacement"
    )


def test_lost_launch_fence_blocks_admission_even_with_valid_schedule_claim(stores):
    store, runtime, repository = stores
    occurrence = claim_occurrence(store, seed_schedule(store))
    launch, _ = repository.reserve(
        make_launch(agent_id="worker"), LaunchCapacity.defaults(), join=True
    )
    ready = repository.update(launch, state=AgentLaunchState.READY)
    repository.update(ready, state=AgentLaunchState.UNKNOWN)
    with pytest.raises(ScheduleWorkerConflict, match="worker_launch_custody_lost"):
        admit(runtime, occurrence.model_copy(update={"launch_id": launch.launch_id}))
    assert runtime.load_task_runtime_by_owner("worker") == []


def test_excluding_blocked_first_schedule_allows_later_due_work(stores):
    store, _, _ = stores
    first = seed_schedule(store)
    second = first.model_copy(
        update={
            "schedule_id": "second",
            "next_fire_at": (utc_now() - timedelta(seconds=5)).isoformat(),
        }
    )
    store.add(second)
    claim = store.claim_next_due(
        claimer_agent_id="worker",
        claimer_named_agent_type="Worker",
        claimer_team_id="team",
        exclude_schedule_ids=(first.schedule_id,),
    )
    assert claim["schedule_id"] == "second"


def test_a_refused_save_records_no_outcome_and_keeps_the_receipt_with_the_new_owner(stores):
    """A previous owner still running after a takeover finishes the run: nothing is written."""
    store, runtime, _ = stores
    occurrence = claim_occurrence(store, seed_schedule(store))
    admit(runtime, occurrence)
    sources = runtime.load_task_runtime_by_owner("worker")
    runtime.commit_task_runtime_recovery(
        sources=sources, replacement=replace(sources[0], owner_agent_id="replacement")
    )
    finished = json.loads(snapshot_json(occurrence.task_id))
    finished["tasks"][0]["state"] = "completed"
    with pytest.raises(RuntimeCustodyLost):
        runtime.save_task_runtime(
            expect_existing=True,
            owner_agent_id="worker",
            owner_agent_name="worker",
            team_id="team",
            owner_named_agent_type="Worker",
            runtime_state_json=json.dumps(finished),
        )
    receipt = store.worker_records.get_occurrence(occurrence.occurrence_id, "team")
    assert (receipt.agent_id, receipt.state, receipt.finished_at_utc) == ("replacement", "admitted", None)
    assert runtime.load_task_runtime_by_owner("worker") == []
