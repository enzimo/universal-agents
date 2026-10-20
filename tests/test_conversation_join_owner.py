"""A new task continues a conversation only for the person the conversation belongs to.

``custom_agents.conversation_runtime`` refuses a top-level task that names a
conversation of someone else (``access_control.ownership.
conversation_continuation``), because joining shows the earlier turns to the
task's model. Trusted code keeps work from naming such a conversation:

- a run for no person (the knowledge-curation team run, installed by the
  gateway with an administrator's proactive session) starts its own
  conversation, owned by nobody;
- a team schedule moved from a departed person continues in a new
  conversation, and a later restore puts the person's conversation back
  untouched;
- a gateway refresh that names another person gives the schedule that
  person's conversation; the same person keeps theirs;
- a heartbeat item whose conversation belongs to someone other than the
  person its run works for continues in a new conversation.

Real SQLite task, schedule, conversation, access and heartbeat stores, real
Ed25519 signed contexts, the real gateway installer, the real admission path
and the context engine's thread renderer.
"""

from __future__ import annotations

import asyncio
import logging
import sqlite3
from unittest.mock import patch
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

from access_control.identity import ContextVerifier
from access_control.ownership import conversation_continuation
from assistant_gateway.gateway import _knowledge_curation_schedules
from custom_agents import task_message_runtime
from custom_agents.context_engine.thread_renderer import BudgetedThreadContextRenderer
from custom_agents.conversation_store import ConversationStore
from custom_agents.heartbeat_store import HeartbeatStore
from custom_agents.person_departure_runtime import _clear_schedules, _move_team_schedule, _ScheduleMoves
from custom_agents.schedule_context_refresh import refresh_schedule_gateway_context
from custom_agents.schedule_store import ScheduleStore
from custom_agents.scheduled_runtime import _enqueue_owner_run, _prepared_heartbeat_run, _signed_heartbeat_runs
from custom_agents.task_context_runtime import run_owner
from custom_agents.task_manager import TaskManager
from custom_messages.contracts.access_v2 import PersonStanding, TeamPersonV1
from custom_messages.contracts.control_v1 import PersonWorkOutcomeV1
from custom_messages.contracts.routing_v1 import GatewayContext, MessageHeader, MessageMeta
from custom_messages.contracts.schedule_v1 import ScheduledMessage
from custom_messages.contracts.task_v1 import Task
from custom_messages.enums import MessageMetaType, RequesterType, ScheduleManagementClass, SystemScheduleKind
from custom_messages.system_schedules import COREMEM_AUDIT_SCHEDULE_NAME_PREFIX, MEMORY_REVIEW_DELIVERY_MODE
from storage.access_repository import AccessRepository
from storage.person_departure_repository import PersonDepartureRepository
from tests import test_person_memory_review_schedules as _gateway_schedules
from tests.broker_access_support import context_signer, forget_task_context_keys, use_scheduler_key
from tests.test_conversation_owner_verified_person import ANN, BOB, _admit, _lead
from tests.test_knowledge_curation_schedules import TEAM_ID as CURATION_TEAM_ID
from tests.test_knowledge_curation_schedules import _config
from tests.test_person_memory_review_schedules import OLIVE, TEAM, _deliver, _gateway, _stored
from tests.test_turn_action_outbox import _make_agent

# The gateway schedule tests' fixture: a gateway with people and a manager's schedule store.
world = _gateway_schedules.world

OTTO = "otto"
NOW = datetime(2026, 10, 11, 12, 0, tzinfo=timezone.utc)
SECRET = "Olive's medical leave is approved until November 3."
RUNTIME_LOGGER = "custom_agents.conversation_runtime"
HEARTBEAT_LOGGER = "custom_agents.heartbeat_store"
ADMISSION_TEAM = "test_team"  # the admission harness's team (tests.test_turn_action_outbox._make_agent)


# -- shared setup ------------------------------------------------------------------------


@pytest.fixture
def signing_keys(monkeypatch: pytest.MonkeyPatch):
    """Trust the gateway's and the scheduler's test keys (the admission tests do not use ``world``)."""

    use_scheduler_key(monkeypatch)
    yield
    forget_task_context_keys()


def _agent(tmp_path: Path, agent_type: str = "TeamArchitect") -> SimpleNamespace:
    """An agent that fires schedules in a process holding the scheduler key (the world fixture sets it)."""

    db_path = str(tmp_path / "agent_tasks.db")
    tasks = TaskManager(db_path=db_path)
    tasks.set_owner_identity("agent_1", agent_type, TEAM, agent_type)
    return SimpleNamespace(
        id="agent_1",
        name=agent_type,
        team_id=TEAM,
        is_team_lead=agent_type == "TeamArchitect",
        named_agent_type=agent_type,
        task_manager=tasks,
        _schedule_store=ScheduleStore(db_path=db_path, storage_settings=tasks.storage_settings),
        _conversation_store=ConversationStore(db_path, artifacts_dir=tmp_path / "artifacts"),
        paused=False,
        is_executing=True,  # queue the run instead of starting a model turn
        _ensure_request_timezone=lambda meta, top_level_request: meta,
        _logger=logging.getLogger("test.conversation_join"),
    )


def _people(agent: SimpleNamespace, *people: tuple[str, PersonStanding, tuple[str, ...]]) -> None:
    """The gateway's copy of the team's people, which scheduled runs read their owner from."""

    AccessRepository(agent.task_manager.storage_settings).replace_people(
        TEAM,
        [
            TeamPersonV1(team_id=TEAM, person_id=person, standing=standing, roles=roles, changed_at_utc=NOW)
            for person, standing, roles in people
        ],
        at=NOW,
    )


def _say(store: ConversationStore, conversation_id: str, owner: str | None, task_id: str, answer: str) -> None:
    """One finished turn of ``owner`` in the conversation (its thread is made for them when missing)."""

    store.ensure_thread(
        conversation_id=conversation_id,
        team_id=TEAM,
        owner_agent_id="agent_1",
        owner_agent_name="TeamArchitect",
        requester_name="gateway",
        requester_id="gateway",
        root_task_id=task_id,
        owner_canonical_user_id=owner,
    )
    store.append_turn(
        conversation_id=conversation_id,
        task_id=task_id,
        followup_to_task_id=None,
        owner_agent_id="agent_1",
        requester_name="gateway",
        requester_id="gateway",
        correlation_id=None,
        problem_text="What did HR say?",
        owner_canonical_user_id=owner,
    )
    store.update_turn_completion(
        task_id=task_id, task_status="completed", final_answer_text=answer, turn_summary_text=answer
    )


def _shown(store: ConversationStore, run) -> str:
    """The conversation history the context engine puts into this run's prompt."""

    return BudgetedThreadContextRenderer().render(
        store=store, conversation_id=run.meta.conversation_id, exclude_task_id=run.task.task_id
    ).text


def _team_schedule(
    schedule_id: str,
    owner: str,
    conversation_id: str,
    *,
    name: str | None = None,
    kind: SystemScheduleKind = SystemScheduleKind.MEMORY_REVIEW_COREMEM,
    heartbeat_source_id: str | None = None,
) -> ScheduledMessage:
    """A team schedule as the gateway installs it: the owner's proactive session and its conversation."""

    return ScheduledMessage(
        schedule_id=schedule_id,
        schedule_name=name or f"{COREMEM_AUDIT_SCHEDULE_NAME_PREFIX}test",
        management_class=ScheduleManagementClass.SYSTEM,
        system_schedule_kind=kind,
        payload=Task(task_type=MessageMetaType.REQUEST, problem_statement="Audit the team's core facts."),
        payload_header=MessageHeader(from_agent_id="gateway", to_routing_path="agent_1", response_routing_key="gateway"),
        payload_meta=MessageMeta(
            conversation_id=conversation_id,
            requester_type=RequesterType.TIMER_REQUESTER,
            is_response_requested=False,
            gateway_context=GatewayContext(
                canonical_user_id=owner,
                session_key=f"proactive:{owner}",
                delivery_mode=MEMORY_REVIEW_DELIVERY_MODE,
                scheduled_job_id=schedule_id,
                heartbeat_source_id=heartbeat_source_id,
            ),
        ),
        interval_ms=3_600_000,
        repeat_count=None,
    )


def _store_schedule(agent: SimpleNamespace, schedule: ScheduledMessage) -> ScheduledMessage:
    agent._schedule_store.add_if_absent(schedule, claim_team_id=TEAM, claim_named_agent_type=agent.named_agent_type)
    stored = agent._schedule_store.get_scheduled_message(schedule.schedule_id)
    assert stored is not None
    return stored


def _fire(agent: SimpleNamespace, schedule: ScheduledMessage) -> tuple[bool, object]:
    """One fire of the schedule as the scheduler admits it; the queued run, if any."""

    before = {info.task.task_id for info in agent.task_manager.list_task_infos()}
    # The queued run is inspected here, not started.
    with patch("custom_agents.scheduled_runtime.schedule_pending_work"):
        accepted = asyncio.run(
            _enqueue_owner_run(
                agent,
                schedule.schedule_id,
                schedule,
                owner=run_owner(agent, schedule.payload_meta),
                is_aar_review_schedule=False,
                schedule_admission=None,
            )
        )
    queued = [info for info in agent.task_manager.list_task_infos() if info.task.task_id not in before]
    assert len(queued) <= 1
    return accepted, (queued[0] if queued else None)


def _signed(run) -> object:
    return ContextVerifier.from_env().verify(run.meta.gateway_context.task_context, team_id=TEAM)


# -- the rule ------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("person", "owner", "allowed"),
    [
        (ANN, ANN, True),
        (None, None, True),
        (BOB, ANN, False),
        (None, ANN, False),
        (ANN, None, False),
        (" ", None, True),  # blank is no person
    ],
)
def test_only_the_owner_continues_a_conversation(person, owner, allowed) -> None:
    decision = conversation_continuation(person_id=person, owner_id=owner, conversation_id="c1")
    assert decision.allowed is allowed


@pytest.mark.asyncio
async def test_a_task_for_someone_else_is_refused_and_the_owners_follow_up_joins(tmp_path, caplog, signing_keys) -> None:
    agent = _lead(tmp_path)
    store = agent._conversation_store
    for_ann = context_signer().issue(team_id=ADMISSION_TEAM, person_id=ANN, person_roles=("member",))
    for_bob = context_signer().issue(team_id=ADMISSION_TEAM, person_id=BOB, person_roles=("member",))
    for_nobody = context_signer().issue(team_id=ADMISSION_TEAM, person_id=None)
    await _admit(agent, GatewayContext(canonical_user_id=ANN, task_context=for_ann), task_id="t1", conversation_id="c1")

    with caplog.at_level(logging.WARNING, logger=RUNTIME_LOGGER):
        await _admit(agent, GatewayContext(canonical_user_id=BOB, task_context=for_bob), task_id="t2", conversation_id="c1")
        await _admit(agent, GatewayContext(task_context=for_nobody), task_id="t3", conversation_id="c1")

    assert agent.task_manager.get_task_info("t2") is None and agent.task_manager.get_task_info("t3") is None
    assert [turn["task_id"] for turn in store.get_conversation_turns(conversation_id="c1", start_turn=1, limit=10)] == ["t1"]
    assert "event=conversation_owner_mismatch_refused conversation_id=c1 task_id=t2" in caplog.text
    assert f"conversation_owner={ANN} task_person={BOB}" in caplog.text
    assert f"task_id=t3 conversation_owner={ANN} task_person=-" in caplog.text
    answers = [call.kwargs["message"].msg_content for call in agent.team_comms.send_message.await_args_list]
    refusals = [content for content in answers if getattr(content, "task_id", None) in {"t2", "t3"}]
    assert len(refusals) == 2 and all("cannot continue it" in content.answer for content in refusals)

    await _admit(agent, GatewayContext(canonical_user_id=ANN, task_context=for_ann), task_id="t4", conversation_id="c1")
    assert [turn["task_id"] for turn in store.get_conversation_turns(conversation_id="c1", start_turn=1, limit=10)] == ["t1", "t4"]


@pytest.mark.asyncio
async def test_work_for_no_person_continues_a_conversation_of_no_person(tmp_path, signing_keys) -> None:
    agent = _lead(tmp_path)
    await _admit(agent, GatewayContext(), task_id="t1", conversation_id="c_system")
    await _admit(agent, GatewayContext(), task_id="t2", conversation_id="c_system")

    store = agent._conversation_store
    assert store.get_thread("c_system")["owner_canonical_user_id"] is None
    assert [turn["task_id"] for turn in store.get_conversation_turns(conversation_id="c_system", start_turn=1, limit=10)] == ["t1", "t2"]


# -- runs for no person ----------------------------------------------------------------------


def test_a_run_for_no_person_starts_its_own_conversation(world) -> None:
    asyncio.run(_gateway(world, more_schedules=(_knowledge_curation_schedules(_config()),)).ensure_all())
    _deliver(world)
    team_run = _stored(world)[CURATION_TEAM_ID]
    proactive = team_run.payload_meta.conversation_id
    assert proactive and team_run.payload_meta.gateway_context.canonical_user_id == OLIVE
    agent = _agent(world.tmp_path, "KnowledgeCuratorAgent")
    _people(agent, (OLIVE, PersonStanding.ACTIVE, ("owner",)))
    _say(agent._conversation_store, proactive, OLIVE, "olive_turn", SECRET)

    accepted, run = _fire(agent, team_run)

    assert accepted and run is not None
    store = agent._conversation_store
    assert run.meta.conversation_id not in (None, proactive)
    assert store.get_thread(run.meta.conversation_id)["owner_canonical_user_id"] is None
    assert SECRET not in _shown(store, run)
    context = _signed(run)
    assert context.person_id is None and context.conversation_id == run.meta.conversation_id
    assert [turn["task_id"] for turn in store.get_conversation_turns(conversation_id=proactive, start_turn=1, limit=10)] == ["olive_turn"]


# -- schedules that change hands -------------------------------------------------------------


def test_a_moved_team_schedule_continues_in_a_new_conversation_of_its_new_owner(world, caplog) -> None:
    agent = _agent(world.tmp_path)
    _people(agent, (OLIVE, PersonStanding.DISABLED, ()), (OTTO, PersonStanding.ACTIVE, ("admin",)))
    store = agent._conversation_store
    _say(store, "conv_olive_proactive", OLIVE, "olive_turn", SECRET)
    schedule = _team_schedule("sched_coremem", OLIVE, "conv_olive_proactive")
    schedule.payload_meta.followup_to_task_id = "olive_turn"
    stored = _store_schedule(agent, schedule)
    outcome = PersonWorkOutcomeV1()

    _move_team_schedule(
        agent, agent._schedule_store, stored, OLIVE,
        _ScheduleMoves(new_owner=OTTO), outcome,
    )

    moved = agent._schedule_store.get_scheduled_message("sched_coremem")
    assert outcome.problems == [] and moved.payload_meta.gateway_context.canonical_user_id == OTTO
    assert moved.payload_meta.conversation_id not in (None, "conv_olive_proactive")
    assert moved.payload_meta.followup_to_task_id is None
    with caplog.at_level(logging.WARNING, logger=RUNTIME_LOGGER):
        accepted, run = _fire(agent, moved)
    assert accepted and run.meta.conversation_id == moved.payload_meta.conversation_id
    assert store.get_thread(run.meta.conversation_id)["owner_canonical_user_id"] == OTTO
    assert SECRET not in _shown(store, run)
    assert "conversation_owner_mismatch_refused" not in caplog.text


def test_a_restored_persons_conversation_comes_back_untouched_after_a_move(world) -> None:
    agent = _agent(world.tmp_path)
    _people(agent, (OLIVE, PersonStanding.REMOVED, ()), (OTTO, PersonStanding.ACTIVE, ("admin",)))
    store = agent._conversation_store
    _say(store, "conv_olive_proactive", OLIVE, "olive_turn", SECRET)
    stored = _store_schedule(agent, _team_schedule("sched_coremem", OLIVE, "conv_olive_proactive"))
    _move_team_schedule(
        agent, agent._schedule_store, stored, OLIVE,
        _ScheduleMoves(new_owner=OTTO), PersonWorkOutcomeV1(),
    )
    departures = PersonDepartureRepository(agent.task_manager.storage_settings)
    departures.hold_person_records(TEAM, OLIVE, at=NOW)
    assert store.get_thread("conv_olive_proactive") is None

    accepted, run = _fire(agent, agent._schedule_store.get_scheduled_message("sched_coremem"))
    assert accepted
    store.update_turn_completion(task_id=run.task.task_id, task_status="completed", final_answer_text="Otto's audit.", turn_summary_text="Otto's audit.")
    agent.task_manager.remove_task(run.task.task_id)  # finished, so the next fire is not merged into it
    departures.restore_person_records(TEAM, OLIVE)

    assert store.get_thread("conv_olive_proactive")["owner_canonical_user_id"] == OLIVE
    olive_turns = store.get_conversation_turns(conversation_id="conv_olive_proactive", start_turn=1, limit=10)
    assert [turn["task_id"] for turn in olive_turns] == ["olive_turn"]
    assert store.get_thread(run.meta.conversation_id)["owner_canonical_user_id"] == OTTO
    later_accepted, later = _fire(agent, agent._schedule_store.get_scheduled_message("sched_coremem"))
    assert later_accepted and SECRET not in _shown(store, later)


def test_a_refresh_moves_a_schedule_an_older_release_handed_over_into_its_owners_conversation(world) -> None:
    """The gateway start after an upgrade: the person is unchanged, the conversation was the departed person's."""

    agent = _agent(world.tmp_path)
    _people(agent, (OLIVE, PersonStanding.DISABLED, ()), (OTTO, PersonStanding.ACTIVE, ("admin",)))
    _say(agent._conversation_store, "conv_olive_proactive", OLIVE, "olive_turn", SECRET)
    stored = _team_schedule("sched_coremem", OTTO, "conv_olive_proactive")
    stored.payload_meta.followup_to_task_id = "olive_turn"
    _store_schedule(agent, stored)

    refreshed = _refresh(agent, OTTO, "conv_otto_proactive")
    assert refreshed.payload_meta.conversation_id == "conv_otto_proactive"
    assert refreshed.payload_meta.followup_to_task_id is None
    accepted, run = _fire(agent, refreshed)
    assert accepted and run.meta.conversation_id == "conv_otto_proactive"
    assert SECRET not in _shown(agent._conversation_store, run)


def test_a_schedule_still_naming_someone_elses_conversation_is_not_started(world, caplog) -> None:
    """A schedule an older release moved keeps its old owner's conversation; its runs are refused (logged)."""

    agent = _agent(world.tmp_path)
    _people(agent, (OLIVE, PersonStanding.DISABLED, ()), (OTTO, PersonStanding.ACTIVE, ("admin",)))
    _say(agent._conversation_store, "conv_olive_proactive", OLIVE, "olive_turn", SECRET)
    legacy = _store_schedule(agent, _team_schedule("sched_coremem", OTTO, "conv_olive_proactive"))

    with caplog.at_level(logging.WARNING):
        accepted, run = _fire(agent, legacy)

    assert accepted is False and run is None
    assert "event=conversation_owner_mismatch_refused conversation_id=conv_olive_proactive" in caplog.text
    assert "Could not admit fired schedule sched_coremem" in caplog.text


def _refresh(agent: SimpleNamespace, owner: str, conversation_id: str | None) -> ScheduledMessage:
    incoming = _team_schedule("sched_coremem", owner, conversation_id or "")
    incoming.payload_meta.conversation_id = conversation_id
    incoming.install_mode = "refresh"
    assert refresh_schedule_gateway_context(agent, incoming)
    return agent._schedule_store.get_scheduled_message("sched_coremem")


def test_a_refresh_for_another_person_takes_their_conversation(world) -> None:
    agent = _agent(world.tmp_path)
    stored = _team_schedule("sched_coremem", OTTO, "conv_otto_proactive")
    stored.payload_meta.followup_to_task_id = "otto_turn"
    _store_schedule(agent, stored)

    back_to_olive = _refresh(agent, OLIVE, "conv_olive_proactive")
    assert back_to_olive.payload_meta.gateway_context.canonical_user_id == OLIVE
    assert back_to_olive.payload_meta.conversation_id == "conv_olive_proactive"
    assert back_to_olive.payload_meta.followup_to_task_id is None

    same_conversation = _refresh(agent, OLIVE, "conv_olive_proactive")
    assert same_conversation.payload_meta.conversation_id == "conv_olive_proactive"

    without_conversation = _refresh(agent, OTTO, None)
    assert without_conversation.payload_meta.conversation_id not in (None, "conv_olive_proactive", "conv_otto_proactive")


# -- heartbeat items -----------------------------------------------------------------------


def _heartbeat(agent: SimpleNamespace) -> tuple[HeartbeatStore, object, ScheduledMessage]:
    """A heartbeat source with one item that ran for Olive, and its launcher, now Otto's."""

    store = HeartbeatStore(storage_settings=agent.task_manager.storage_settings)
    store.ensure_source(source_id="hb_main")
    item = store.add_item(source_id="hb_main", title="Inbox", instruction="Summarize my inbox.", conversation_id="conv_item")
    _say(agent._conversation_store, "conv_item", OLIVE, "olive_item_run", SECRET)
    launcher = _team_schedule(
        "sched_heartbeat", OTTO, "conv_otto_proactive", name="heartbeat:test",
        kind=SystemScheduleKind.HEARTBEAT_LAUNCHER, heartbeat_source_id="hb_main",
    )
    return store, item, launcher


def test_a_heartbeat_item_run_for_another_person_continues_in_a_new_conversation(world, caplog) -> None:
    agent = _agent(world.tmp_path)
    _people(agent, (OLIVE, PersonStanding.ACTIVE, ("owner",)), (OTTO, PersonStanding.ACTIVE, ("admin",)))
    store, item, launcher = _heartbeat(agent)

    with caplog.at_level(logging.WARNING, logger=HEARTBEAT_LOGGER):
        run = asyncio.run(_prepared_heartbeat_run(agent, launcher, item, store))

    assert run is not None and run.meta.conversation_id not in (None, "conv_item")
    assert store.get_item(item.heartbeat_item_id).conversation_id == run.meta.conversation_id
    assert "event=heartbeat_item_conversation_handed_over" in caplog.text
    assert f"previous_conversation_id=conv_item conversation_owner={OLIVE} person_id={OTTO}" in caplog.text
    # The new conversation has no history yet; the item's later runs for Otto stay in it.
    assert SECRET not in _shown(agent._conversation_store, run)
    again = asyncio.run(_prepared_heartbeat_run(agent, launcher, store.get_item(item.heartbeat_item_id), store))
    assert again.meta.conversation_id == run.meta.conversation_id


def test_a_heartbeat_item_run_for_its_conversations_owner_keeps_it(world) -> None:
    agent = _agent(world.tmp_path)
    _people(agent, (OLIVE, PersonStanding.ACTIVE, ("owner",)))
    store, item, launcher = _heartbeat(agent)
    launcher.payload_meta.gateway_context.canonical_user_id = OLIVE

    run = asyncio.run(_prepared_heartbeat_run(agent, launcher, item, store))

    assert run.meta.conversation_id == "conv_item"
    assert store.get_item(item.heartbeat_item_id).conversation_id == "conv_item"


def test_moving_a_heartbeat_launcher_gives_its_items_new_conversations(world) -> None:
    agent = _agent(world.tmp_path)
    store, item, launcher = _heartbeat(agent)
    launcher.payload_meta.gateway_context.canonical_user_id = OLIVE
    other = store.add_item(source_id="hb_other", title="Other", instruction="Other source.", conversation_id="conv_other")
    stored = _store_schedule(agent, launcher)

    _move_team_schedule(
        agent, agent._schedule_store, stored, OLIVE,
        _ScheduleMoves(new_owner=OTTO), PersonWorkOutcomeV1(),
    )

    assert store.get_item(item.heartbeat_item_id).conversation_id not in ("conv_item", None)
    assert store.get_item(other.heartbeat_item_id).conversation_id == "conv_other"  # another source's item


@pytest.mark.parametrize("failure_stage", ["first_item", "later_item", "schedule_update"])
def test_a_failed_heartbeat_handover_stays_stopped_until_retry_and_restore_keeps_history_private(
    world, monkeypatch, caplog, failure_stage: str,
) -> None:
    """Partial item writes and a failed launcher write must keep the old owner for a safe retry."""

    agent = _agent(world.tmp_path)
    agent._ensure_schedule_store = lambda: agent._schedule_store
    _people(agent, (OLIVE, PersonStanding.REMOVED, ()), (OTTO, PersonStanding.ACTIVE, ("admin",)))
    store, item, launcher = _heartbeat(agent)
    store.update_item(item.heartbeat_item_id, target_agent_type="WebResearchAgent")
    disabled = store.add_item(
        source_id=item.source_id, title="Disabled", instruction="Later check.",
        conversation_id="conv_disabled", enabled=False,
    )
    launcher.payload_meta.gateway_context.canonical_user_id = OLIVE
    _store_schedule(agent, launcher)
    outcome = PersonWorkOutcomeV1()
    reset_item = HeartbeatStore.start_new_item_conversation
    resets = 0
    conversations_at_failed_update: list[str] = []

    def fail_item_reset(heartbeat: HeartbeatStore, item_id: str) -> str | None:
        nonlocal resets
        if resets == (0 if failure_stage == "first_item" else 1):
            raise sqlite3.OperationalError("Injected heartbeat handover write failure")
        resets += 1
        return reset_item(heartbeat, item_id)

    def fail_schedule_update(*args, **kwargs) -> None:
        conversations_at_failed_update.extend(
            stored.conversation_id for stored in store.list_items(item.source_id)
        )
        raise sqlite3.OperationalError("Injected schedule handover write failure")

    with monkeypatch.context() as failure, caplog.at_level(logging.ERROR):
        if failure_stage == "schedule_update":
            failure.setattr(agent._schedule_store, "update", fail_schedule_update)
        else:
            failure.setattr(HeartbeatStore, "start_new_item_conversation", fail_item_reset)
        _clear_schedules(agent, OLIVE, outcome)

    pending = agent._schedule_store.get_scheduled_message(launcher.schedule_id)
    assert outcome.problems and not outcome.team_schedules_moved
    assert pending.payload_meta.gateway_context.canonical_user_id == OLIVE
    assert asyncio.run(_prepared_heartbeat_run(agent, pending, store.get_item(item.heartbeat_item_id), store)) is None
    if failure_stage != "schedule_update":
        assert "event=heartbeat_items_handover_failed" in caplog.text
    else:
        assert len(conversations_at_failed_update) == 2
        assert not {"conv_item", "conv_disabled"}.intersection(conversations_at_failed_update)

    # Removal may hold the old threads even while the launcher handover is incomplete.
    departures = PersonDepartureRepository(agent.task_manager.storage_settings)
    departures.hold_person_records(TEAM, OLIVE, at=NOW)
    assert agent._conversation_store.get_thread("conv_item") is None

    # Reopening the stores proves that the incomplete move stays retryable after restart.
    agent._schedule_store = ScheduleStore(storage_settings=agent.task_manager.storage_settings)
    store = HeartbeatStore(storage_settings=agent.task_manager.storage_settings)
    retry = PersonWorkOutcomeV1()
    _clear_schedules(agent, OLIVE, retry)
    moved = agent._schedule_store.get_scheduled_message(launcher.schedule_id)
    assert not retry.problems and len(retry.team_schedules_moved) == 1
    assert moved.payload_meta.gateway_context.canonical_user_id == OTTO
    assert store.get_item(item.heartbeat_item_id).conversation_id != "conv_item"
    assert store.get_item(disabled.heartbeat_item_id).conversation_id != "conv_disabled"
    assert not store.get_item(disabled.heartbeat_item_id).enabled

    # Use actual signed worker admission, which creates a thread for the new owner.
    worker = _make_agent(world.tmp_path, task_manager=agent.task_manager)
    worker.team_id = TEAM
    worker.is_team_lead = False
    worker.named_agent_type = "WebResearchAgent"
    # UniversalAgent's generic admission harness resets the manager's team.
    # Both sides must name the team that signed this scheduled run.
    worker.task_manager.set_owner_identity(worker.id, worker.name, TEAM, worker.named_agent_type)
    worker._conversation_store = agent._conversation_store
    worker.paused = True

    async def admit_run():
        runs = await _signed_heartbeat_runs(agent, moved, store.list_items(item.source_id, enabled_only=True), None, store)
        assert runs and len(runs) == 1
        run = runs[0]
        await task_message_runtime._handle_task_request(worker, run.task, run.header, run.meta)
        assert worker.task_manager.get_task_info(run.task.task_id) is not None
        assert _signed(run).person_id == OTTO
        assert SECRET not in _shown(agent._conversation_store, run)
        return run

    run = asyncio.run(admit_run())
    assert agent._conversation_store.get_thread(run.meta.conversation_id)["owner_canonical_user_id"] == OTTO
    departures.restore_person_records(TEAM, OLIVE)
    original = agent._conversation_store.get_thread("conv_item")
    assert original["owner_canonical_user_id"] == OLIVE
    assert [turn["task_id"] for turn in agent._conversation_store.get_conversation_turns(
        conversation_id="conv_item", start_turn=1, limit=10,
    )] == ["olive_item_run"]
    assert SECRET not in _shown(agent._conversation_store, run)
    assert asyncio.run(admit_run()).meta.conversation_id == run.meta.conversation_id
