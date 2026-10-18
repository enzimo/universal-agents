"""Shared setup for tests of scheduled runs started by a worker without the scheduler key.

A manager and a worker share one test process and one SQLite database
(schedule rows, claims, the team's people, the task store). The environment is
the worker's: it trusts the scheduler key but holds none. The manager's
signer is handed to the manager's code explicitly, as the manager process
would read it from its own environment. Messages cross an in-memory bus that
serializes them like the wire.

Used by ``tests/test_scheduled_run_contexts.py`` (the manager signs each run on
request) and ``tests/test_schedule_permits.py`` (one permit per schedule).
"""

from __future__ import annotations

import asyncio
import logging
import os
from datetime import datetime, timedelta, timezone
from functools import partial
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from access_control.identity import ENV_SCHEDULER_SIGNING_KEY, ISSUER_SCHEDULER, ContextSigner, ContextVerifier
from custom_agents import access_runtime, scheduled_run_contexts
from custom_agents.schedule_store import ScheduleStore
from custom_agents.scheduled_run_contexts import answer_run_context_request, subscribe_run_context_routes
from custom_agents.scheduled_runtime import _fire_claimed_schedule, enqueue_fired_scheduled_task
from custom_agents.task_manager import TaskManager
from custom_messages.contracts.access_v2 import PersonStanding, TeamPersonV1
from custom_messages.contracts.routing_v1 import GatewayContext, MessageHeader, MessageMeta
from custom_messages.contracts.schedule_v1 import ScheduledMessage
from custom_messages.contracts.task_v1 import Task
from custom_messages.enums import MessageMetaType, ScheduleManagementClass
from custom_messages.transport_codec import decode_envelope, encode_envelope
from custom_transport.base import InboundDelivery
from custom_transport.subjects import NatsSubjectMapper
from storage.access_repository import AccessRepository
from tests.broker_access_support import context_signer, forget_task_context_keys, use_scheduler_key

TEAM = "team_1"
ANN = "usr_ann"
WORKER_TYPE = "WebResearchAgent"
WORKER_ID = "worker_1"
MANAGER_ID = "manager_1"
SCHEDULE_ID = "sched_launch_check"
NOW = datetime(2026, 9, 25, 12, 0, tzinfo=timezone.utc)
ROOT = Path(__file__).resolve().parents[1]
CEDAR = ROOT / "target" / "debug" / "ua-cedar-authorizer"
needs_cedar = pytest.mark.skipif(not CEDAR.is_file(), reason="build ua-cedar-authorizer first (cargo build)")


# -- an in-memory bus that behaves like Core NATS subjects ----------------------------


class Bus:
    def __init__(self) -> None:
        self.handlers: dict[str, list] = {}
        self.published: list[tuple[str, object]] = []
        self.deliveries: list[asyncio.Task] = []


class BusAdapter:
    """What an agent's NATS adapter offers these helpers: subscribe and publish by subject."""

    def __init__(self, bus: Bus) -> None:
        self.bus = bus
        self.mapper = NatsSubjectMapper(team_id=TEAM)

    async def subscribe_subject(self, *, name: str, subject: str, handler, **_options) -> None:
        self.bus.handlers.setdefault(subject, []).append(handler)

    async def publish_subject(self, *, subject: str, envelope, options=None) -> None:
        self.bus.published.append((subject, envelope.msg_content))
        # Serialize as on the wire; one subscriber answers, as in a queue group.
        wire = decode_envelope(encode_envelope(envelope))
        for handler in self.bus.handlers.get(subject, [])[:1]:
            delivery = InboundDelivery(envelope=wire, logical_route=subject, transport_subject=subject)
            self.bus.deliveries.append(asyncio.create_task(handler(delivery)))


# -- the world ----------------------------------------------------------------------


def build_world(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> SimpleNamespace:
    """A worker process's view: it trusts the scheduler key but holds none.

    Call ``forget_task_context_keys`` after the test.
    """

    use_scheduler_key(monkeypatch)
    manager_signer = ContextSigner.from_value(ISSUER_SCHEDULER, os.environ[ENV_SCHEDULER_SIGNING_KEY])
    monkeypatch.delenv(ENV_SCHEDULER_SIGNING_KEY)
    forget_task_context_keys()
    # The manager process reads the key from its own environment; here it is given it.
    monkeypatch.setattr(
        scheduled_run_contexts,
        "answer_run_context_request",
        partial(answer_run_context_request, signer=manager_signer),
    )
    db_path = str(tmp_path / "manager.db")
    tasks = TaskManager(db_path=db_path)
    tasks.set_owner_identity(agent_id=WORKER_ID, agent_name="researcher", team_id=TEAM, named_agent_type=WORKER_TYPE)
    store = ScheduleStore(db_path=db_path, storage_settings=tasks.storage_settings)
    bus = Bus()
    manager = SimpleNamespace(
        id=MANAGER_ID,
        team_id=TEAM,
        is_team_lead=True,
        task_manager=tasks,
        _schedule_store=store,
    )
    manager._ensure_nats_transport_adapter = lambda adapter=BusAdapter(bus): adapter
    worker = worker_agent(tasks, store, bus, WORKER_ID)
    return SimpleNamespace(
        tasks=tasks, store=store, bus=bus, manager=manager, worker=worker, signer=manager_signer, db_path=db_path
    )


def worker_agent(tasks: TaskManager, store: ScheduleStore, bus: Bus, agent_id: str) -> SimpleNamespace:
    worker = SimpleNamespace(
        id=agent_id,
        name="researcher",
        team_id=TEAM,
        is_team_lead=False,
        named_agent_type=WORKER_TYPE,
        task_manager=tasks,
        _schedule_store=store,
        schedule_workers={},
        paused=False,
        is_executing=True,  # queue the run instead of starting a model turn
        # The run's wake-up is recorded; with is_executing=True the drain finds the slot busy, so the run stays queued.
        _should_exit=asyncio.Event(),
        _pending_work_task=None,
        _continue_pending_work=AsyncMock(),
        _conversation_store=None,
        _ensure_request_timezone=lambda meta, top_level_request: meta,
        _logger=logging.getLogger("test.worker"),
    )
    worker._ensure_nats_transport_adapter = lambda adapter=BusAdapter(bus): adapter
    worker._enqueue_fired_scheduled_task = partial(enqueue_fired_scheduled_task, worker)
    return worker


def record_people(world, *people: tuple[str, PersonStanding, tuple[str, ...]]) -> None:
    """What the gateway keeps in the access store: each person's standing and current roles."""

    AccessRepository(world.tasks.storage_settings).replace_people(
        TEAM,
        [
            TeamPersonV1(team_id=TEAM, person_id=person_id, standing=standing, roles=roles, changed_at_utc=NOW)
            for person_id, standing, roles in people
        ],
        at=NOW,
    )


def record_ann(world, standing: PersonStanding, roles: tuple[str, ...] = ("member",)) -> None:
    record_people(world, (ANN, standing, roles))


def save_schedule(
    world,
    *,
    schedule_id: str = SCHEDULE_ID,
    owner: str | None = ANN,
    vouched: bool = True,
    heartbeat_source_id: str | None = None,
) -> ScheduledMessage:
    """A daily check for the research worker, saved from its owner's request.

    ``vouched``: the gateway signed the request's context for the owner.
    """

    signed = context_signer().issue(team_id=TEAM, person_id=owner, person_roles=("admin",)) if vouched else None
    schedule = ScheduledMessage(
        schedule_id=schedule_id,
        schedule_name=f"heartbeat:{heartbeat_source_id}" if heartbeat_source_id else "Starship launch check",
        management_class=ScheduleManagementClass.USER,
        payload=Task(task_id=f"task_{schedule_id}", task_type=MessageMetaType.REQUEST, problem_statement="Check launches."),
        payload_header=MessageHeader(from_agent_id="gateway", to_routing_path=MANAGER_ID),
        payload_meta=MessageMeta(
            gateway_context=GatewayContext(
                canonical_user_id=owner, task_context=signed, heartbeat_source_id=heartbeat_source_id
            )
        ),
        interval_ms=86_400_000,  # daily, until removed
        repeat_count=None,
        next_fire_at=(datetime.now(timezone.utc) - timedelta(minutes=1)).isoformat(),  # due now
    )
    world.store.add(schedule, claim_team_id=TEAM, claim_named_agent_type=WORKER_TYPE)
    return schedule


def claim(world, agent_id: str = WORKER_ID, schedule_id: str = SCHEDULE_ID) -> dict:
    claimed = world.store.claim_next_due(
        claimer_agent_id=agent_id, claimer_named_agent_type=WORKER_TYPE, claimer_team_id=TEAM
    )
    assert claimed is not None and claimed["schedule_id"] == schedule_id
    return claimed


def transport(agent) -> tuple:
    adapter = agent._ensure_nats_transport_adapter()
    return adapter, adapter.mapper


async def subscribe(world) -> None:
    await subscribe_run_context_routes(world.manager, *transport(world.manager))
    await subscribe_run_context_routes(world.worker, *transport(world.worker))


async def fire(world, claimed: dict, agent=None) -> dict:
    deadlines: dict = {}

    async def no_sleep(_seconds: float) -> None:
        return None

    await _fire_claimed_schedule(agent or world.worker, claimed, deadlines, no_sleep)
    return deadlines


def queued(world) -> list:
    return world.tasks.list_task_infos()


def queued_run(world):
    runs = queued(world)
    assert len(runs) == 1
    return runs[0]


def run_context(task_info):
    return ContextVerifier.from_env().verify(task_info.meta.gateway_context.task_context, team_id=TEAM)


def claim_row(world) -> tuple:
    row = world.store._repository.get_schedule_row(SCHEDULE_ID)
    return row[15], row[16]


def access_agent(world, run) -> SimpleNamespace:
    """The worker as the access hook sees it while it runs ``run``."""

    task_manager = SimpleNamespace(
        storage_settings=world.tasks.storage_settings,
        current_task_id=run.task.task_id,
        get_task_info=lambda _task_id: run,
        terminal_callbacks=[],
    )
    task_manager.add_task_terminal_callback = task_manager.terminal_callbacks.append
    access_runtime._runtime_access.cache_clear()
    return SimpleNamespace(
        id=WORKER_ID,
        named_agent_type=WORKER_TYPE,
        team_id=TEAM,
        is_team_lead=False,
        access_role="worker",
        authorization_config={"enabled": True, "cedar_binary_path": str(CEDAR)},
        task_manager=task_manager,
        instance_root=None,
    )
