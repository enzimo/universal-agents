"""A task record belongs to the person its task verifiably works for (``TaskInfo.owner_person_id``).

The owner is decided once, when the task is admitted, from the task's signed
context or schedule run identity (``access_runtime.verified_meta_person_id``),
and kept with the task, its runtime snapshot included. The ``canonical_user_id``
a message's gateway context names is never used. The archived task row,
Objectives, solution variations, a person's task list and a person's departure
read this one owner. Real Ed25519 keys, real SQLite stores, the real admission
path and the real identity resolver.

Covers:
- a task signed for Ann that claims Bob is Ann's while active and once
  archived, and the mismatch is logged;
- a claim without a signed context makes a task for nobody;
- a task restored from its runtime snapshot keeps its owner;
- a subtask works for its parent's verified person, not the person its own
  message names;
- a worker's permit-backed scheduled run is archived as its schedule owner's;
- an Objective, its spawned root task and a solution variation belong to the
  verified person;
- a departure takes the verified person's active and archived tasks, not the
  claimed person's;
- the operator archives a failed deferred task for its verified person only.
"""

from __future__ import annotations

import asyncio
import json
import logging
from pathlib import Path
from unittest.mock import AsyncMock

import pytest

from access_control.identity import ENV_VERIFY_KEYS
from custom_agents import task_message_runtime
from custom_agents.conversation_store import ConversationStore
from custom_agents.manager_control_runtime import _runtime_work_roots
from custom_agents.task_manager import TaskManager
from custom_messages.contracts.access_v2 import PersonStanding
from custom_messages.contracts.routing_v1 import GatewayContext, MessageHeader, MessageMeta
from custom_messages.contracts.task_v1 import Task
from custom_messages.enums import MessageMetaType, RequesterType
from storage.config import resolve_storage_settings
from storage.person_departure_repository import PersonDepartureRepository
from tests.broker_access_support import context_signer, context_verify_keys, forget_task_context_keys

# The admission harness (tests.test_turn_action_outbox._make_agent) runs in this team.
TEAM = "test_team"
ANN = "usr_ann"
BOB = "usr_bob"
CAROL = "usr_carol"
ACCESS_LOGGER = "custom_agents.access_runtime"
NOW = "2026-10-11T00:00:00+00:00"


@pytest.fixture(autouse=True)
def _verify_keys(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv(ENV_VERIFY_KEYS, context_verify_keys())
    forget_task_context_keys()
    yield
    forget_task_context_keys()


def _db(tmp_path: Path) -> str:
    return str(tmp_path / "tasks.db")


def _tasks(tmp_path: Path) -> TaskManager:
    tasks = TaskManager(db_path=_db(tmp_path))
    tasks.set_owner_identity("lead", "Lead", TEAM, "TeamArchitect")
    return tasks


def _lead(tmp_path: Path):
    """The team lead as the admission path sees it; admitted tasks stay queued instead of running."""

    from tests.test_turn_action_outbox import _make_agent

    agent = _make_agent(tmp_path, task_manager=_tasks(tmp_path))
    agent._conversation_store = ConversationStore(_db(tmp_path), artifacts_dir=tmp_path / "conversation_artifacts")
    object.__setattr__(agent, "_continue_pending_work", AsyncMock())
    return agent


def _signed_for(person: str | None, *, claims: str | None) -> GatewayContext:
    signed = context_signer().issue(team_id=TEAM, person_id=person, person_roles=("member",)) if person else None
    return GatewayContext(canonical_user_id=claims, task_context=signed)


async def _admit(agent, gateway_context: GatewayContext, *, task_id: str = "t1") -> None:
    await task_message_runtime._handle_task_request(
        agent,
        Task(task_id=task_id, task_type=MessageMetaType.REQUEST, problem_statement="Ann's private question"),
        MessageHeader(from_agent_id="gateway", response_routing_key="test_team_gateway_response"),
        MessageMeta(
            conversation_id=f"c_{task_id}",
            msg_correlation_id=f"corr_{task_id}",
            requester_type=RequesterType.USER_REQUESTER,
            gateway_context=gateway_context,
        ),
    )


def _archived_owner(tasks: TaskManager, task_id: str) -> str | None:
    row = tasks.task_archive_repository.get_archived_task(task_id, owner_agent_id=None, include_all_agents=True)
    assert row is not None, f"{task_id} was archived"
    return row.owner_canonical_user_id


def _finished(tasks: TaskManager, person: str) -> list[str]:
    rows, _total = tasks.task_archive_repository.list_finished_tasks(
        team_id=TEAM, owner_canonical_user_id=person, offset=0, limit=20
    )
    return [row.task_id for row in rows]


@pytest.mark.asyncio
async def test_a_task_signed_for_ann_that_claims_bob_is_archived_as_anns(tmp_path, caplog) -> None:
    agent = _lead(tmp_path)

    with caplog.at_level(logging.WARNING, logger=ACCESS_LOGGER):
        await _admit(agent, _signed_for(ANN, claims=BOB))

    tasks = agent.task_manager
    assert tasks.get_task_info("t1").owner_person_id == ANN
    tasks.complete_task("t1", NOW)
    assert _archived_owner(tasks, "t1") == ANN
    assert _finished(tasks, ANN) == ["t1"] and _finished(tasks, BOB) == []
    assert f"claimed_person_id={BOB} verified_person_id={ANN}" in caplog.text


@pytest.mark.asyncio
async def test_a_claim_without_a_signed_context_makes_a_task_for_nobody(tmp_path, caplog) -> None:
    agent = _lead(tmp_path)

    with caplog.at_level(logging.WARNING, logger=ACCESS_LOGGER):
        await _admit(agent, _signed_for(None, claims=BOB))

    tasks = agent.task_manager
    assert tasks.get_task_info("t1").owner_person_id is None
    tasks.complete_task("t1", NOW)
    assert _archived_owner(tasks, "t1") is None
    assert _finished(tasks, BOB) == []
    assert "event=unverified_person_ignored" in caplog.text


@pytest.mark.asyncio
async def test_a_restored_task_keeps_its_owner(tmp_path) -> None:
    agent = _lead(tmp_path)
    await _admit(agent, _signed_for(ANN, claims=BOB))

    successor = _tasks(tmp_path)
    successor.restore_runtime_state()

    restored = successor.get_task_info("t1")
    assert restored is not None
    assert restored.meta.gateway_context.canonical_user_id == BOB, "the claim is restored unchanged"
    assert restored.owner_person_id == ANN, "the owner comes from the snapshot, not from the claim"
    successor.complete_task("t1", NOW)
    assert _archived_owner(successor, "t1") == ANN


@pytest.mark.asyncio
async def test_a_subtask_works_for_its_parents_verified_person(tmp_path) -> None:
    agent = _lead(tmp_path)
    await _admit(agent, _signed_for(ANN, claims=BOB))
    tasks = agent.task_manager

    subtask = Task(task_id="t1_sub", parent_task_id="t1", task_type=MessageMetaType.REQUEST, problem_statement="Look it up")
    info = tasks.add_subtask(
        "t1",
        subtask,
        MessageHeader(from_agent_id="lead"),
        MessageMeta(gateway_context=_signed_for(CAROL, claims=CAROL)),
    )

    assert info is not None and info.owner_person_id == ANN
    tasks.complete_task("t1_sub", NOW)
    assert _archived_owner(tasks, "t1_sub") == ANN


def test_a_permit_backed_scheduled_run_is_archived_as_its_schedule_owners(tmp_path, monkeypatch) -> None:
    """A worker without the scheduler key starts the run with its schedule's permit (``schedule_permits``)."""

    from custom_agents.schedule_permits import renew_schedule_permits
    from tests import scheduled_run_support as runs

    world = runs.build_world(tmp_path, monkeypatch)
    runs.record_people(world, (runs.ANN, PersonStanding.ACTIVE, ("member",)))
    runs.save_schedule(world)
    renew_schedule_permits(world.manager, signer=world.signer)

    async def start_run() -> dict:
        await runs.subscribe(world)
        return await runs.fire(world, runs.claim(world))

    assert asyncio.run(start_run()) == {}, "the run was accepted"
    run = runs.queued_run(world)
    assert run.meta.gateway_context.schedule_run is not None
    assert run.owner_person_id == runs.ANN
    world.tasks.complete_task(run.task.task_id, NOW)
    assert _archived_owner(world.tasks, run.task.task_id) == runs.ANN


@pytest.mark.asyncio
async def test_objectives_and_solution_variations_belong_to_the_verified_person(tmp_path) -> None:
    agent = _lead(tmp_path)
    await _admit(agent, _signed_for(ANN, claims=BOB))
    tasks = agent.task_manager

    objective = tasks.create_objective(
        task_id="t1",
        title="Weekly research",
        desired_outcome="Research stays current.",
        why="The need outlives one task.",
        creation_mode="spawn_task",
    )
    variation, created = tasks.solution_variation_coordinator.start(
        "t1", purpose="Compare two approaches.", evaluation_criteria=["works"]
    )

    assert objective is not None and objective.owner_canonical_user_id == ANN
    spawned = [task_id for task_id in objective.related_task_ids if task_id != "t1"]
    assert spawned and tasks.get_task_info(spawned[0]).owner_person_id == ANN
    assert created and variation.owner_access_scope == f"user:{ANN}"


@pytest.mark.asyncio
async def test_a_departure_takes_the_verified_persons_tasks(tmp_path) -> None:
    agent = _lead(tmp_path)
    await _admit(agent, _signed_for(ANN, claims=BOB))
    tasks = agent.task_manager

    assert [root.task.task_id for root in await _runtime_work_roots(agent, ANN)] == ["t1"]
    assert await _runtime_work_roots(agent, BOB) == []

    tasks.complete_task("t1", NOW)
    # A removed person's task records are deleted (storage.person_departure_repository).
    departures = PersonDepartureRepository(resolve_storage_settings({"backend": "sqlite", "sqlite": {"path": _db(tmp_path)}}))
    departures.delete_person_records(BOB)
    assert _finished(tasks, ANN) == ["t1"], "Bob's removal leaves Ann's task"
    departures.delete_person_records(ANN)
    assert _finished(tasks, ANN) == [], "Ann's task leaves with her"


@pytest.mark.parametrize(("person", "expected"), [(ANN, ANN), (None, None)])
def test_the_operator_archives_a_failed_deferred_task_for_its_verified_person(
    tmp_path, caplog, person, expected
) -> None:
    import strands_main

    storage = resolve_storage_settings({"backend": "sqlite", "sqlite": {"path": _db(tmp_path)}})
    context = strands_main.OperatorContext(
        storage_settings=storage, db_path=_db(tmp_path), team_id=TEAM,
        named_agent_type="TeamArchitect", is_team_lead=True, owner_agent_name="Lead",
    )

    with caplog.at_level(logging.WARNING, logger="strands_main"):
        strands_main._archive_failed_envelope_task(
            context=context,
            owner_agent_id="lead",
            owner_agent_name="Lead",
            task_request=Task(task_id="deferred", task_type=MessageMetaType.REQUEST, problem_statement="Stuck"),
            msg_header=MessageHeader(from_agent_id="gateway"),
            msg_meta=MessageMeta(gateway_context=_signed_for(person, claims=BOB)),
            reason="Operator cleared stuck work.",
        )

    assert _archived_owner(_tasks(tmp_path), "deferred") == expected
    unverified = "event=operator_failed_task_owner_unverified" in caplog.text
    assert unverified is (person is None)


def test_a_snapshot_task_without_an_owner_is_refused() -> None:
    """The owner is a required field: a restored task never re-derives it from its message."""

    from custom_messages.contracts.runtime_snapshot_v3 import decode_runtime_snapshot
    from tests.runtime_snapshot_fixtures import snapshot_json

    document = json.loads(snapshot_json())
    del document["tasks"][0]["owner_person_id"]
    with pytest.raises(ValueError, match="owner_person_id"):
        decode_runtime_snapshot(json.dumps(document))
