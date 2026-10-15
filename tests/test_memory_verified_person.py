"""A turn's personal memory follows the task's verified person (``custom_agents.memory_plugins``).

Personal hydration (``usermem`` facts and ``domain_topic_recall`` thread
hints) and the conversation memory review job use only the person the task's
signed context or schedule run identity names
(``access_runtime.verified_meta_person_id``), never the ``canonical_user_id``
a message's gateway context claims. Real Ed25519 keys, real SQLite stores and
the real identity resolver.

Covers:
- a task signed for Ann that claims Bob loads Ann's memories and threads,
  never Bob's, and logs the mismatch, with authorization on and off;
- a claim without a signed context, and system work for no person, load no
  personal memory (team memory still loads);
- a scheduled run, signed by the scheduler or carrying its schedule's permit,
  loads its owner's memories even when its message claims someone else;
- the conversation review job is made for the verified person, and none is
  made for an unverified claim;
- the agent connects its memory manager to the verified person;
- the tools' ``verified_person_id`` logs the same mismatch.
"""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from types import SimpleNamespace

import pytest

from access_control.identity import ENV_VERIFY_KEYS, ISSUER_SCHEDULER, ContextSigner
from custom_agents import access_runtime
from custom_agents.access_runtime import verified_meta_person_id, verified_person_id
from custom_agents.context_engine import ContextAssemblyRequest, TurnFinalizationRequest
from custom_agents.conversation_store import ConversationStore
from custom_agents.memory_plugins.hydration import (
    CorememHydrationPlugin,
    DomainTopicRecallPlugin,
    UsermemHydrationPlugin,
)
from custom_agents.memory_plugins.manager import MemoryPluginManager
from custom_messages.contracts.access_v2 import PersonStanding
from custom_messages.contracts.routing_v1 import GatewayContext, MessageHeader, MessageMeta
from custom_messages.contracts.task_v1 import Task
from custom_messages.enums import MessageMetaType, RequesterType
from storage.conversation_repository import TURN_MEMORY_REVIEW_QUEUED, TURN_MEMORY_REVIEW_UNREVIEWED
from storage.pinned_memory_repository import SqlitePinnedMemoryRepository
from tests.broker_access_support import (
    context_signer,
    context_verify_keys,
    forget_task_context_keys,
    use_scheduler_key,
)

# The review seeds (tests.test_memory_review_runtime) use this team and person.
TEAM = "team_test"
ANN = "canonical_user_1"
BOB = "usr_bob"
ACCESS_LOGGER = "custom_agents.access_runtime"
MISMATCH = "event=claimed_person_mismatch"
UNVERIFIED = "event=unverified_person_ignored"


@pytest.fixture(autouse=True)
def _verify_keys(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv(ENV_VERIFY_KEYS, context_verify_keys())
    forget_task_context_keys()
    yield
    forget_task_context_keys()


def _agent(tmp_path: Path, *, authorization: bool = True, team_id: str = TEAM) -> SimpleNamespace:
    """What the identity check reads from an agent: its team, task storage and authorization switch."""

    from custom_agents.task_manager import TaskManager

    return SimpleNamespace(
        id="agent-lead",
        team_id=team_id,
        task_manager=TaskManager(db_path=str(tmp_path / "manager.db")),
        authorization_config={"enabled": authorization},
        instance_root=None,
    )


def _memories(tmp_path: Path, *, team_id: str = TEAM) -> tuple[SqlitePinnedMemoryRepository, ConversationStore]:
    """Team memory, Ann's and Bob's private facts, and one private thread each with a team-level topic tag."""

    pinned = SqlitePinnedMemoryRepository(str(tmp_path / "memory.db"))
    pinned.create_coremem(team_id=team_id, fact_text="Team fact", capacity=10)
    pinned.create_usermem(canonical_user_id=ANN, fact_text="Ann private fact", capacity=10)
    pinned.create_usermem(canonical_user_id=BOB, fact_text="Bob private fact", capacity=10)
    conversations = ConversationStore(str(tmp_path / "conversations.db"), artifacts_dir=tmp_path / "artifacts")
    for person, preview in ((ANN, "Ann salary review notes"), (BOB, "Bob salary negotiation notes")):
        conversation_id = f"conversation_{person}"
        conversations.ensure_thread(
            conversation_id=conversation_id,
            team_id=team_id,
            owner_canonical_user_id=person,
            owner_agent_id="agent-lead",
            owner_agent_name="TeamArchitect",
            requester_name=person,
            requester_id=person,
            root_task_id=f"task_{person}",
            subject_preview=preview,
        )
        # A team-level tag, as the after-turn topic tagger writes it.
        conversations.tag_thread(conversation_id=conversation_id, tag="salary", team_id=team_id)
    return pinned, conversations


def _manager(agent, pinned=None, conversations=None, **kwargs) -> MemoryPluginManager:
    """The memory manager as the agent builds it (``UniversalAgent._create_context_engine``)."""

    return MemoryPluginManager(
        agent_id=agent.id,
        team_id=agent.team_id,
        named_agent_type="TeamArchitect",
        verified_person_of=lambda meta: verified_meta_person_id(agent, meta),
        pinned_memory_repository=pinned,
        conversation_store=conversations,
        **kwargs,
    )


def _hydration_manager(agent, tmp_path: Path) -> MemoryPluginManager:
    return _manager(
        agent,
        *_memories(tmp_path, team_id=agent.team_id),
        hydration_plugins=[CorememHydrationPlugin(), UsermemHydrationPlugin(), DomainTopicRecallPlugin()],
    )


def _hydrated(manager: MemoryPluginManager, meta: MessageMeta) -> str:
    task = Task(task_id="task_now", task_type=MessageMetaType.REQUEST, problem_statement="Where are we on salary?")
    request = ContextAssemblyRequest(
        incoming_content=task, incoming_header=MessageHeader(), incoming_meta=meta,
        execution_task_id=task.task_id,
        identity_meta=meta, incoming_message_json="{}"
    )
    return "\n".join(section.text for section in manager.hydrate(request))


def _meta(gateway_context: GatewayContext) -> MessageMeta:
    return MessageMeta(conversation_id="conversation_now", gateway_context=gateway_context)


def _assert_only_anns(text: str) -> None:
    assert "Team fact" in text
    assert "Ann private fact" in text and "Ann salary review notes" in text
    assert "Bob private fact" not in text and "Bob salary" not in text


def _assert_no_personal_memory(text: str) -> None:
    assert "Team fact" in text, "team memory still loads"
    assert "private fact" not in text and "salary" not in text and "Related Topic Thread Hints" not in text


# -- hydration ----------------------------------------------------------------------


@pytest.mark.parametrize("authorization", [True, False], ids=["authorization_on", "authorization_off"])
def test_a_task_signed_for_ann_that_claims_bob_loads_only_anns_memories(tmp_path, caplog, authorization) -> None:
    manager = _hydration_manager(_agent(tmp_path, authorization=authorization), tmp_path)
    signed = context_signer().issue(team_id=TEAM, person_id=ANN, person_roles=("member",))

    with caplog.at_level(logging.WARNING, logger=ACCESS_LOGGER):
        text = _hydrated(manager, _meta(GatewayContext(canonical_user_id=BOB, task_context=signed)))

    _assert_only_anns(text)
    assert MISMATCH in caplog.text
    assert f"claimed_person_id={BOB} verified_person_id={ANN} context_id={signed.context.context_id}" in caplog.text


def test_a_claim_without_a_signed_context_loads_no_personal_memory(tmp_path, caplog) -> None:
    manager = _hydration_manager(_agent(tmp_path), tmp_path)

    with caplog.at_level(logging.WARNING, logger=ACCESS_LOGGER):
        text = _hydrated(manager, _meta(GatewayContext(canonical_user_id=BOB)))

    _assert_no_personal_memory(text)
    assert UNVERIFIED in caplog.text and f"claimed_person_id={BOB}" in caplog.text


def test_system_work_loads_no_personal_memory(tmp_path, monkeypatch, caplog) -> None:
    use_scheduler_key(monkeypatch)
    scheduler = ContextSigner.from_env(ISSUER_SCHEDULER)
    assert scheduler is not None
    signed = scheduler.issue(team_id=TEAM, person_id=None, schedule_id="sched_internal_improvement")
    manager = _hydration_manager(_agent(tmp_path), tmp_path)

    with caplog.at_level(logging.WARNING, logger=ACCESS_LOGGER):
        text = _hydrated(manager, _meta(GatewayContext(task_context=signed)))

    # The store's no-person query would return team-level tags on anyone's threads.
    _assert_no_personal_memory(text)
    assert MISMATCH not in caplog.text and UNVERIFIED not in caplog.text, "system work names nobody"


def test_a_scheduler_signed_run_loads_its_owners_memories(tmp_path, monkeypatch, caplog) -> None:
    use_scheduler_key(monkeypatch)
    scheduler = ContextSigner.from_env(ISSUER_SCHEDULER)
    assert scheduler is not None
    signed = scheduler.issue(team_id=TEAM, person_id=ANN, person_roles=("member",), schedule_id="sched_ann_daily")
    manager = _hydration_manager(_agent(tmp_path), tmp_path)

    _assert_only_anns(_hydrated(manager, _meta(GatewayContext(canonical_user_id=ANN, task_context=signed))))

    # A message that still names a previous owner does not change whose memories load.
    with caplog.at_level(logging.WARNING, logger=ACCESS_LOGGER):
        _assert_only_anns(_hydrated(manager, _meta(GatewayContext(canonical_user_id=BOB, task_context=signed))))
    assert MISMATCH in caplog.text


def test_a_permit_backed_scheduled_run_loads_its_owners_memories(tmp_path, monkeypatch, caplog) -> None:
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
    gateway_context = run.meta.gateway_context
    assert gateway_context.task_context is None and gateway_context.schedule_run is not None

    pinned = SqlitePinnedMemoryRepository(str(tmp_path / "memory.db"))
    pinned.create_usermem(canonical_user_id=runs.ANN, fact_text="Owner private fact", capacity=10)
    pinned.create_usermem(canonical_user_id=BOB, fact_text="Bob private fact", capacity=10)
    agent = runs.access_agent(world, run)
    manager = _manager(agent, pinned, hydration_plugins=[UsermemHydrationPlugin()])

    assert "Owner private fact" in _hydrated(manager, run.meta)
    gateway_context.canonical_user_id = BOB
    with caplog.at_level(logging.WARNING, logger=ACCESS_LOGGER):
        text = _hydrated(manager, run.meta)
    assert "Owner private fact" in text and "Bob private fact" not in text
    assert MISMATCH in caplog.text and f"verified_person_id={runs.ANN}" in caplog.text


# -- the conversation review job ------------------------------------------------------


def _review_setup(tmp_path: Path, *, authorization: bool = True, owner_canonical_user_id: str | None = ANN):
    from tests.test_memory_review_runtime import _seed_turns, _stores

    conversations, reviews, _pinned = _stores(tmp_path)
    _seed_turns(conversations, count=15, owner_canonical_user_id=owner_canonical_user_id)
    manager = _manager(
        _agent(tmp_path, authorization=authorization),
        conversations=conversations,
        memory_review_store=reviews,
        hydration_plugins=[],
    )
    return manager, conversations, reviews


def _finish_turn(manager: MemoryPluginManager, gateway_context: GatewayContext) -> list:
    meta = MessageMeta(
        conversation_id="conversation_review",
        requester_type=RequesterType.USER_REQUESTER,
        gateway_context=gateway_context,
    )
    task = Task(task_id="task_15", task_type=MessageMetaType.REQUEST, problem_statement="Question 15 about car repair.")
    finished = TurnFinalizationRequest(
        task_info=SimpleNamespace(task=task, meta=meta), task_status="completed", strands_messages=[]
    )
    return manager.enqueue_review_jobs(finished)


def test_the_conversation_review_job_is_made_for_the_verified_person(tmp_path) -> None:
    manager, _conversations, reviews = _review_setup(tmp_path)
    signed = context_signer().issue(team_id=TEAM, person_id=ANN, person_roles=("member",))

    jobs = _finish_turn(manager, GatewayContext(canonical_user_id=BOB, task_context=signed))

    assert [job.payload["canonical_user_id"] for job in jobs] == [ANN]
    stored = reviews.get_review_job(jobs[0].job_id)
    assert stored is not None and stored.source_payload["canonical_user_id"] == ANN


def test_no_review_job_is_made_for_an_unverified_claim(tmp_path) -> None:
    manager, conversations, _reviews = _review_setup(tmp_path)

    assert _finish_turn(manager, GatewayContext(canonical_user_id=ANN)) == []
    first_turn = conversations.get_conversation_turns(conversation_id="conversation_review", start_turn=1, limit=1)[0]
    assert first_turn["memory_review_status"] != TURN_MEMORY_REVIEW_QUEUED, "the turns stay for a later review"


@pytest.mark.parametrize("authorization", [True, False], ids=["authorization_on", "authorization_off"])
@pytest.mark.parametrize("source_owner", [ANN, None], ids=["another_person", "no_owner"])
def test_review_jobs_refuse_a_conversation_the_verified_person_does_not_own(
    tmp_path, caplog, authorization, source_owner
) -> None:
    manager, conversations, reviews = _review_setup(
        tmp_path, authorization=authorization, owner_canonical_user_id=source_owner
    )
    person = BOB if source_owner == ANN else ANN
    signed = context_signer().issue(
        team_id=TEAM, person_id=person, person_roles=("member",), conversation_id=f"conversation_{person}"
    )

    # The unsigned message points to Ann's conversation; its signed task works
    # for Bob. A person claim cannot assign a conversation with no stored owner.
    with caplog.at_level(logging.INFO, logger="access_control.ownership"):
        jobs = _finish_turn(manager, GatewayContext(canonical_user_id=ANN, task_context=signed))

    assert jobs == []
    assert reviews.list_review_jobs(limit=20) == []
    turns = conversations.get_conversation_turns(conversation_id="conversation_review", start_turn=1, limit=20)
    assert len(turns) == 15
    assert {turn["memory_review_status"] for turn in turns} == {TURN_MEMORY_REVIEW_UNREVIEWED}
    assert "event=private_access_refused" in caplog.text
    assert f"person_id={person} resource_type=Conversation resource_id=conversation_review" in caplog.text

    if source_owner == ANN:
        # Refusal must leave the window available for its actual owner, even
        # when that owner's unsigned message claims a different person.
        owner_signed = context_signer().issue(team_id=TEAM, person_id=ANN, person_roles=("member",))
        owner_jobs = _finish_turn(manager, GatewayContext(canonical_user_id=BOB, task_context=owner_signed))
        assert [job.payload["canonical_user_id"] for job in owner_jobs] == [ANN]
        assert len(owner_jobs[0].payload["turns"]) == 15


# -- the agent and its tools ----------------------------------------------------------


def test_the_agent_gives_its_memory_manager_the_verified_person(tmp_path, monkeypatch) -> None:
    from custom_agents.strands_agent import UniversalAgent

    monkeypatch.chdir(tmp_path)
    agent = UniversalAgent(role="memory identity check", tools=[], team_id=TEAM, workspace_dir=str(tmp_path))
    manager = agent._context_engine.memory_plugin_manager
    signed = context_signer().issue(team_id=TEAM, person_id=ANN, person_roles=("member",))

    assert manager.verified_person_of(_meta(GatewayContext(canonical_user_id=BOB, task_context=signed))) == ANN
    assert manager.verified_person_of(_meta(GatewayContext(canonical_user_id=BOB))) is None


def test_the_tools_verified_person_logs_the_same_mismatch(tmp_path, caplog) -> None:
    signed = context_signer().issue(team_id=TEAM, person_id=ANN, person_roles=("member",))
    meta = _meta(GatewayContext(canonical_user_id=BOB, task_context=signed))
    agent = _agent(tmp_path)
    agent._active_execution_task_id = "task_now"
    agent.task_manager = SimpleNamespace(
        storage_settings=agent.task_manager.storage_settings,
        get_task_info=lambda _task_id: SimpleNamespace(meta=meta),
    )
    access_runtime._runtime_access.cache_clear()

    with caplog.at_level(logging.WARNING, logger=ACCESS_LOGGER):
        assert verified_person_id(agent) == ANN

    assert MISMATCH in caplog.text and f"claimed_person_id={BOB} verified_person_id={ANN}" in caplog.text
