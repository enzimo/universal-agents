"""A conversation belongs to the person its task verifiably works for (``custom_agents.conversation_runtime``).

The thread and each turn record the person named by the task's signed context
(or, for a scheduled run, the schedule's owner the run is signed for), never
the ``canonical_user_id`` a message's gateway context claims. The owner decides
who may read the conversation, which threads topic recall offers, whose stale
conversations are reviewed and what leaves with a departing person. Real
Ed25519 keys, real SQLite stores, the real admission path and the real
identity resolver.

Covers:
- a gateway task signed for Ann that claims Bob makes Ann's conversation, in
  Ann's list and not Bob's, and logs the mismatch;
- a claim without a signed context makes a conversation with no owner;
- repeated manager startup preserves unsigned, system and personal ownership,
  including compacted context, without assigning conversations to the bootstrap person;
- a worker's permit-backed scheduled run records its schedule owner's
  conversation;
- the store records no owner from a gateway context's claim;
- a schedule's dedicated conversation and its seed turn belong to the
  schedule's owner;
- a conversation leaves with the person it belongs to, not with the person a
  message claimed.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

from access_control.identity import ENV_VERIFY_KEYS
from custom_agents import task_message_runtime
from custom_agents.conversation_store import ConversationStore
from custom_agents.task_manager import TaskManager
from custom_messages.contracts.access_v2 import PersonStanding
from custom_messages.contracts.routing_v1 import GatewayContext, MessageHeader, MessageMeta
from custom_messages.contracts.schedule_v1 import ScheduledMessage
from custom_messages.contracts.task_v1 import Task
from custom_messages.enums import MessageMetaType, RequesterType, ScheduleManagementClass
from storage.config import resolve_storage_settings
from storage.person_departure_repository import PersonDepartureRepository
from tests.broker_access_support import context_signer, context_verify_keys, forget_task_context_keys

# The admission harness (tests.test_turn_action_outbox._make_agent) runs in this team.
TEAM = "test_team"
ANN = "usr_ann"
BOB = "usr_bob"
ACCESS_LOGGER = "custom_agents.access_runtime"


@pytest.fixture(autouse=True)
def _verify_keys(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv(ENV_VERIFY_KEYS, context_verify_keys())
    forget_task_context_keys()
    yield
    forget_task_context_keys()


def _conversations(tmp_path: Path) -> ConversationStore:
    return ConversationStore(str(tmp_path / "tasks.db"), artifacts_dir=tmp_path / "conversation_artifacts")


def _lead(tmp_path: Path):
    """The team lead as the admission path sees it, with its conversations in the task database."""

    from tests.test_turn_action_outbox import _make_agent

    tasks = TaskManager(db_path=str(tmp_path / "tasks.db"))
    tasks.set_owner_identity("lead", "Lead", TEAM, "TeamArchitect")
    agent = _make_agent(tmp_path, task_manager=tasks)
    agent._conversation_store = _conversations(tmp_path)
    return agent


async def _admit(agent, gateway_context: GatewayContext, *, task_id: str, conversation_id: str) -> None:
    await task_message_runtime._handle_task_request(
        agent,
        Task(task_id=task_id, task_type=MessageMetaType.REQUEST, problem_statement="Ann's private question"),
        MessageHeader(from_agent_id="gateway", response_routing_key="test_team_gateway_response"),
        MessageMeta(
            conversation_id=conversation_id,
            msg_correlation_id=f"corr_{task_id}",
            requester_type=RequesterType.USER_REQUESTER,
            gateway_context=gateway_context,
        ),
    )


def _owners(store: ConversationStore, conversation_id: str, task_id: str) -> tuple[str | None, str | None]:
    thread = store.get_thread(conversation_id) or {}
    turn = store.get_turn_by_task_id(task_id) or {}
    return thread.get("owner_canonical_user_id"), turn.get("owner_canonical_user_id")


def _listed(store: ConversationStore, person: str) -> list[str]:
    return [item["conversation_id"] for item in store.list_conversations(limit=20, owner_canonical_user_id=person)]


@pytest.mark.asyncio
async def test_a_task_signed_for_ann_that_claims_bob_makes_anns_conversation(tmp_path, caplog) -> None:
    agent = _lead(tmp_path)
    signed = context_signer().issue(team_id=TEAM, person_id=ANN, person_roles=("member",))

    with caplog.at_level(logging.WARNING, logger=ACCESS_LOGGER):
        await _admit(agent, GatewayContext(canonical_user_id=BOB, task_context=signed), task_id="t1", conversation_id="c1")

    store = agent._conversation_store
    assert _owners(store, "c1", "t1") == (ANN, ANN)
    assert _listed(store, ANN) == ["c1"] and _listed(store, BOB) == []
    assert "event=claimed_person_mismatch" in caplog.text
    assert f"claimed_person_id={BOB} verified_person_id={ANN}" in caplog.text


@pytest.mark.asyncio
async def test_a_claim_without_a_signed_context_makes_a_conversation_with_no_owner(tmp_path, caplog) -> None:
    agent = _lead(tmp_path)

    with caplog.at_level(logging.WARNING, logger=ACCESS_LOGGER):
        await _admit(agent, GatewayContext(canonical_user_id=BOB), task_id="t1", conversation_id="c1")

    store = agent._conversation_store
    assert _owners(store, "c1", "t1") == (None, None)
    assert _listed(store, BOB) == []
    assert "event=unverified_person_ignored" in caplog.text


@pytest.mark.asyncio
@pytest.mark.parametrize("identity_kind", ["unsigned", "system", "person"])
async def test_manager_startup_preserves_conversation_ownership(tmp_path, identity_kind) -> None:
    from assistant_gateway.config import GatewayInstanceConfig
    from custom_agents.manager_bootstrap_runtime import run_manager_bootstrap
    from tests.test_manager_control_runtime import _bootstrap_agent

    agent = _lead(tmp_path)
    owner = ANN if identity_kind == "person" else None
    signed = (
        context_signer().issue(team_id=TEAM, person_id=owner, person_roles=("member",) if owner else ())
        if identity_kind != "unsigned"
        else None
    )
    await _admit(agent, GatewayContext(canonical_user_id=BOB, task_context=signed), task_id="t1", conversation_id="c1")
    epoch = agent._conversation_store.create_context_epoch(
        conversation_id="c1", trigger="test", through_turn_index=1,
        preserved_suffix_start_turn=2, latest_task_id="t1",
    )
    assert _owners(agent._conversation_store, "c1", "t1") == (owner, owner)
    assert epoch["owner_canonical_user_id"] == owner

    config = GatewayInstanceConfig.create(
        instance_name="owner_restart", instance_root=str(tmp_path / "instance"),
        team_id=TEAM, timezone="UTC", display_name="Bootstrap",
        initial_user_context="Bootstrap person's own profile.",
    )
    config.state.storage.sqlite.path = str(tmp_path / "tasks.db")
    config.layout.ensure_directories()
    bootstrap = _bootstrap_agent(config)

    for _ in range(2):
        run_manager_bootstrap(bootstrap)
        # Reopen the store to check persisted ownership after each startup.
        store = _conversations(tmp_path)
        assert _owners(store, "c1", "t1") == (owner, owner)
        assert store.get_context_epoch(epoch["context_epoch_id"])["owner_canonical_user_id"] == owner
        assert _listed(store, config.user.canonical_user_id) == []
        assert _listed(store, BOB) == []
        assert _listed(store, ANN) == (["c1"] if owner else [])


def test_a_permit_backed_scheduled_run_records_its_owners_conversation(tmp_path, monkeypatch) -> None:
    """A worker without the scheduler key starts the run with its schedule's permit (``schedule_permits``)."""

    from custom_agents.schedule_permits import renew_schedule_permits
    from tests import scheduled_run_support as runs

    world = runs.build_world(tmp_path, monkeypatch)
    world.worker._conversation_store = ConversationStore(world.db_path, artifacts_dir=tmp_path / "artifacts")
    runs.record_people(world, (runs.ANN, PersonStanding.ACTIVE, ("member",)))
    runs.save_schedule(world)
    renew_schedule_permits(world.manager, signer=world.signer)

    async def start_run() -> dict:
        await runs.subscribe(world)
        return await runs.fire(world, runs.claim(world))

    assert asyncio.run(start_run()) == {}, "the run was accepted"
    run = runs.queued_run(world)
    assert run.meta.gateway_context.schedule_run is not None
    store = world.worker._conversation_store
    assert _owners(store, run.meta.conversation_id, run.task.task_id) == (runs.ANN, runs.ANN)


def test_the_store_records_no_owner_from_a_claim(tmp_path) -> None:
    store = _conversations(tmp_path)
    store.ensure_thread(
        conversation_id="c1",
        team_id=TEAM,
        owner_agent_id="lead",
        owner_agent_name="TeamArchitect",
        requester_name="someone",
        requester_id="someone",
        root_task_id="t1",
    )
    store.append_turn(
        conversation_id="c1",
        task_id="t1",
        followup_to_task_id=None,
        owner_agent_id="lead",
        requester_name="someone",
        requester_id="someone",
        correlation_id=None,
        problem_text="hello",
        gateway_context=GatewayContext(canonical_user_id=BOB).model_dump(mode="json"),
    )

    assert _owners(store, "c1", "t1") == (None, None)


def test_a_schedules_dedicated_conversation_belongs_to_its_owner(tmp_path) -> None:
    from custom_agents.schedule_conversation_context import ensure_dedicated_schedule_conversation

    store = _conversations(tmp_path)
    agent = SimpleNamespace(id="lead", name="Lead", named_agent_type="TeamArchitect", team_id=TEAM, _conversation_store=store)
    schedule = ScheduledMessage(
        schedule_id="sched_daily",
        management_class=ScheduleManagementClass.USER,
        payload=Task(task_type=MessageMetaType.REQUEST, problem_statement="Send the daily report"),
        payload_meta=MessageMeta(gateway_context=GatewayContext(canonical_user_id=ANN)),
        interval_ms=86_400_000,
    )

    prepared, conversation = ensure_dedicated_schedule_conversation(agent, schedule)

    assert conversation is not None
    meta = prepared.payload_meta
    assert _owners(store, meta.conversation_id, meta.followup_to_task_id) == (ANN, ANN)


@pytest.mark.asyncio
async def test_a_conversation_leaves_with_its_owner_not_with_the_claimed_person(tmp_path) -> None:
    agent = _lead(tmp_path)
    signed = context_signer().issue(team_id=TEAM, person_id=ANN, person_roles=("member",))
    await _admit(agent, GatewayContext(canonical_user_id=BOB, task_context=signed), task_id="t1", conversation_id="c1")
    departures = PersonDepartureRepository(resolve_storage_settings({"backend": "sqlite", "sqlite": {"path": str(tmp_path / "tasks.db")}}))
    now = datetime.now(timezone.utc)

    departures.hold_person_records(TEAM, BOB, at=now)
    assert agent._conversation_store.get_thread("c1") is not None, "Bob's departure leaves Ann's conversation"

    departures.hold_person_records(TEAM, ANN, at=now)
    assert agent._conversation_store.get_thread("c1") is None, "Ann's conversation leaves with her"
    assert agent._conversation_store.get_turn_by_task_id("t1") is None
