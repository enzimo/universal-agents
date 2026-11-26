"""Shared setup for tests of scheduled runs using protocol 1 scheduler permits and ``svc.scheduler``.

A lead, a worker, and ``team_services.scheduler.SchedulerOperations`` share one test
process and one SQLite database (schedule rows, claims, the team's people, the task store).
Neither the lead nor the worker holds any signing key; only ``SchedulerOperations`` holds
the scheduler certificate. Requests to ``svc.scheduler`` (``run_context.sign``) go through
an in-memory process-host double that forwards a ``VerifiedFrame`` naming the asking agent
to ``SchedulerOperations.handle``.

Used by ``tests/test_scheduled_run_contexts.py``, ``tests/test_schedule_permits.py``,
``tests/test_schedule_final_runs.py``, and related scheduled-run identity tests.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
from datetime import datetime, timedelta, timezone
from functools import partial
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Callable, Mapping, Optional
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest

from access_control.authority_verifier import AuthorityTokenVerifier, set_process_verifier
from access_control.people_directory import PeopleDirectory
from access_control.task_identity import ScheduleRunLookup, resolve_task_identity
from custom_agents import access_runtime, agent_requests
from custom_agents.agent_requests import configure_process_host
from custom_agents.heartbeat_store import HeartbeatStore
from custom_agents.schedule_store import ScheduleStore
from custom_agents.scheduled_runtime import _fire_claimed_schedule, enqueue_fired_scheduled_task
from custom_agents.task_manager import TaskManager
from custom_messages.contracts.access_v2 import PersonStanding, TeamPersonV1
from custom_messages.contracts.routing_v1 import GatewayContext, MessageHeader, MessageMeta
from custom_messages.contracts.schedule_v1 import ScheduledMessage
from custom_messages.contracts.task_v1 import Task
from custom_messages.enums import MessageMetaType, ScheduleManagementClass, SystemScheduleKind
from custom_messages.wire.authority_bindings import AuthorityTokenRecord
from custom_messages.wire.encoding import b64url, canonical_bytes
from custom_messages.wire.frame import body_model, verify_authority_token
from custom_messages.wire.models import AuthorityToken
from custom_transport.protocol1.authority_tokens import root_task_token
from storage.access_repository import AccessRepository
from team_services.scheduler import PermitPassResult, SchedulerOperations
from team_services.schedules import ScheduleInstallDefaults, ScheduleOperations
from tests.broker_access_support import (
    broker_gateway_identity,
    broker_people_store,
    broker_person_ref,
    broker_scheduler_identity,
    broker_trust_store,
    forget_task_context_keys,
)
from tests.task_work_support import bind_task

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


def fake_verified_frame(
    kind: str,
    body: Mapping[str, Any],
    *,
    sender_agent: Optional[str],
    team: str = TEAM,
) -> Any:
    """Build a minimal VerifiedFrame double carrying ``issuer.agent`` and ``raw['body']``."""

    return SimpleNamespace(
        frame=SimpleNamespace(
            kind=kind,
            issuer=SimpleNamespace(agent=sender_agent, team=team),
        ),
        raw={"body": dict(body)},
    )


class _InMemoryBlobStore:
    """Minimal in-memory ``BlobStore`` for unit tests exercising >32KB ``schedules.install`` instructions."""

    def __init__(self) -> None:
        self._items: dict[str, bytes] = {}

    async def put(
        self,
        data: bytes,
        *,
        media_type: str,
        owner: Any,
        name: Optional[str] = None,
    ) -> dict[str, Any]:
        payload = bytes(data)
        digest = b64url(hashlib.sha256(payload).digest())
        self._items[digest] = payload
        ref: dict[str, Any] = {
            "sha256": digest,
            "size": len(payload),
            "media_type": media_type,
        }
        if name is not None:
            ref["name"] = name
        return ref

    async def get(self, ref: Mapping[str, Any], *, max_bytes: Optional[int] = None) -> bytes:
        data = self._items[str(ref["sha256"])]
        if max_bytes is not None and len(data) > max_bytes:
            raise ValueError(f"Blob exceeds max_bytes={max_bytes}")
        return data


class _FallbackPeopleStore:
    """Looks up people in the test's AccessRepository first, then falls back to ``broker_people_store()``."""

    def __init__(self, primary: Any, fallback: Any) -> None:
        self._primary = primary
        self._fallback = fallback

    def team_person(self, team_id: str, person_id: str) -> Any:
        found = self._primary.team_person(team_id, person_id)
        return found if found is not None else self._fallback.team_person(team_id, person_id)

    def team_person_by_ref(self, team_id: str, person_ref: str) -> Any:
        found = self._primary.team_person_by_ref(team_id, person_ref)
        return found if found is not None else self._fallback.team_person_by_ref(team_id, person_ref)

    def team_people(self, team_id: str) -> list[Any]:
        primary = list(self._primary.team_people(team_id)) if hasattr(self._primary, "team_people") else []
        seen = {getattr(p, "person_id", None) for p in primary}
        extra = [
            p
            for p in (self._fallback.team_people(team_id) if hasattr(self._fallback, "team_people") else [])
            if getattr(p, "person_id", None) not in seen
        ]
        return primary + extra

    def people_synced_at(self, team_id: str) -> Any:
        synced = self._primary.people_synced_at(team_id) if hasattr(self._primary, "people_synced_at") else None
        if synced is not None:
            return synced
        return self._fallback.people_synced_at(team_id) if hasattr(self._fallback, "people_synced_at") else None


def _verified_team_frame(
    kind: str,
    body: Mapping[str, Any],
    *,
    sender_agent: str,
    authority: Mapping[str, Any],
    team: str,
) -> Any:
    """Validate ``body`` and ``authority`` with the real contract models and trust store."""

    body_model(kind)[0].validate_python(dict(body))
    now = datetime.now(timezone.utc)
    token = AuthorityToken.model_validate(dict(authority))
    verified_authority = verify_authority_token(
        token,
        dict(authority),
        broker_trust_store(),
        valid_at=now,
        now=now,
    )
    request_id = str(body.get("request_id") or uuid4().hex)
    digest = b64url(hashlib.sha256(canonical_bytes(dict(body))).digest())
    sender_cert = SimpleNamespace(
        installation=verified_authority.certificate.installation,
        runtime="lead",
        roles=frozenset({"lead_host", "worker_host"}),
    )
    return SimpleNamespace(
        frame=SimpleNamespace(
            kind=kind,
            issuer=SimpleNamespace(agent=sender_agent, team=team, runtime="lead"),
        ),
        authority=verified_authority,
        certificate=sender_cert,
        raw={
            "kind": kind,
            "body": dict(body),
            "idempotency_key": f"{kind}:{request_id}",
            "body_digest": digest,
        },
    )


class FakeSchedulerHost:
    """In-memory ``AgentProcessHost`` double routing ``svc.scheduler`` and ``svc.team`` (``schedules.install``)."""

    def __init__(
        self,
        scheduler: Optional[SchedulerOperations] = None,
        *,
        schedule_ops: Optional[ScheduleOperations] = None,
        team: str = TEAM,
    ) -> None:
        self.scheduler = scheduler
        self.schedule_ops = schedule_ops
        self.team = team
        self.requests: list[tuple[str, dict[str, Any]]] = []
        self.team_requests: list[tuple[str, str, dict[str, Any]]] = []
        self.custom_handler: Optional[Callable[[str, dict[str, Any], str], Any]] = None
        self.team_agents = None
        self.loop = None
        self.blob_store = _InMemoryBlobStore()
        self.logger = logging.getLogger("test.fake_scheduler_host")

    def report(self, *_args: Any, **_kwargs: Any) -> None:
        """No-op state report hook for tests where ``_PROCESS_HOST`` is a ``FakeSchedulerHost``."""

    def usable_partitions(self) -> tuple[int, ...]:
        return ()

    def cannot_ask(self, _agent_id: str) -> Any:
        return None

    async def ask_scheduler(
        self,
        kind: str,
        body: Mapping[str, Any],
        *,
        sender_agent: str,
        timeout: Optional[float] = None,
    ) -> dict[str, Any]:
        payload = dict(body)
        self.requests.append((sender_agent, payload))
        if self.custom_handler is not None:
            result = self.custom_handler(kind, payload, sender_agent)
            if asyncio.iscoroutine(result):
                return await result
            return result
        if self.scheduler is None:
            raise RuntimeError("No test scheduler is bound on this FakeSchedulerHost.")
        verified = fake_verified_frame(kind, payload, sender_agent=sender_agent, team=self.team)
        return await self.scheduler.handle(verified)

    def _resolve_schedule_ops(self, authority: Mapping[str, Any]) -> tuple[ScheduleOperations, str]:
        if self.schedule_ops is not None:
            return self.schedule_ops, self.team
        from custom_tools.runtime_context import get_agent_ref

        agent = get_agent_ref()
        if agent is None:
            raise RuntimeError("No active agent or bound ScheduleOperations for in-process schedules.install.")
        store = getattr(agent, "_schedule_store", None)
        if store is None:
            raise RuntimeError("Active test agent has no _schedule_store.")
        team_id = str(getattr(agent, "team_id", "") or authority.get("audience", {}).get("team") or self.team)
        storage = getattr(getattr(agent, "task_manager", None), "storage_settings", None)
        people_store: Any = broker_people_store()
        if storage is not None:
            people_store = _FallbackPeopleStore(AccessRepository(storage), people_store)
        people = PeopleDirectory(people_store, team_id)
        ops = ScheduleOperations(
            store,
            people,
            None,
            lambda person: bool(set(person.roles) & {"owner", "admin"}),
            lambda: datetime.now(timezone.utc),
            logging.getLogger("test.team_schedules"),
            blobs=self.blob_store,
            conversations=getattr(agent, "_conversation_store", None),
            install_defaults=ScheduleInstallDefaults(
                lead_agent_type=str(getattr(agent, "named_agent_type", None) or "TeamArchitect"),
                default_timezone=getattr(agent, "instance_timezone", None),
            ),
        )
        return ops, team_id

    async def ask_team(
        self,
        kind: str,
        body: Mapping[str, Any],
        *,
        sender_agent: str,
        authority: Optional[Mapping[str, Any]] = None,
        timeout: Optional[float] = None,
    ) -> dict[str, Any]:
        payload = dict(body)
        self.team_requests.append((sender_agent, kind, payload))
        if kind != "schedules.install":
            raise RuntimeError(f"FakeSchedulerHost does not handle svc.team kind {kind!r}")
        if authority is None:
            raise RuntimeError("schedules.install requires an authority token")
        ops, team_id = self._resolve_schedule_ops(authority)
        verified = _verified_team_frame(
            kind, payload, sender_agent=sender_agent, authority=authority, team=team_id
        )
        reply = await ops.handle(verified)
        body_model(f"{kind}.reply")[0].validate_python(reply)
        return reply


def bind_test_scheduler(
    monkeypatch: pytest.MonkeyPatch,
    *,
    store: ScheduleStore,
    team_id: str,
    storage_settings: Any | None = None,
    people_store: Any | None = None,
    identity: Any | None = None,
    heartbeat_store: HeartbeatStore | None = None,
) -> SimpleNamespace:
    """Wire protocol 1 ``SchedulerOperations`` and ``ScheduleOperations`` onto an existing store."""

    if storage_settings is not None:
        access_repo: Any = AccessRepository(storage_settings)
        merged_people: Any = _FallbackPeopleStore(access_repo, broker_people_store())
    elif people_store is not None:
        access_repo = people_store
        merged_people = people_store
    else:
        access_repo = broker_people_store()
        merged_people = access_repo
    people = PeopleDirectory(merged_people, team_id)
    verifier = AuthorityTokenVerifier(broker_trust_store(), people=merged_people)
    set_process_verifier(verifier)

    scheduler_state: dict[str, Any] = {
        "identity": identity if identity is not None else broker_scheduler_identity(team_id)
    }

    def _heartbeat_store() -> HeartbeatStore:
        if heartbeat_store is not None:
            return heartbeat_store
        if storage_settings is not None:
            return HeartbeatStore(storage_settings=storage_settings)
        from storage.config import resolve_storage_settings

        settings = resolve_storage_settings({"backend": "sqlite", "sqlite": {"path": str(store._repository._db_path)}})
        return HeartbeatStore(storage_settings=settings)

    scheduler = SchedulerOperations(
        store,
        people,
        team_id,
        identity_fn=lambda: scheduler_state["identity"],
        verifier_fn=lambda: verifier,
        heartbeat_store_fn=_heartbeat_store,
        clock=lambda: datetime.now(timezone.utc),
        logger=logging.getLogger("test.scheduler"),
    )
    blob_store = _InMemoryBlobStore()
    schedule_ops = ScheduleOperations(
        store,
        people,
        None,
        lambda person: bool(set(person.roles) & {"owner", "admin"}),
        lambda: datetime.now(timezone.utc),
        logging.getLogger("test.team_schedules"),
        blobs=blob_store,
        on_schedule_installed=scheduler.issue_permit_for_schedule,
    )
    host = FakeSchedulerHost(scheduler, schedule_ops=schedule_ops, team=team_id)
    host.blob_store = blob_store
    monkeypatch.setattr(agent_requests, "_PROCESS_HOST", host)
    return SimpleNamespace(
        scheduler=scheduler,
        schedule_ops=schedule_ops,
        scheduler_state=scheduler_state,
        host=host,
        verifier=verifier,
        people=people,
        access_repo=access_repo,
    )


def build_world(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> SimpleNamespace:
    """Build a test world with protocol 1 trust, ``SchedulerOperations``, lead, and worker.

    Call ``cleanup_world(world)`` or ``forget_task_context_keys()`` after the test.
    """

    forget_task_context_keys()
    db_path = str(tmp_path / "manager.db")
    tasks = TaskManager(db_path=db_path)
    tasks.set_owner_identity(agent_id=WORKER_ID, agent_name="researcher", team_id=TEAM, named_agent_type=WORKER_TYPE)
    store = ScheduleStore(db_path=db_path, storage_settings=tasks.storage_settings)
    access_repo = AccessRepository(tasks.storage_settings)
    people = PeopleDirectory(access_repo, TEAM)
    verifier = AuthorityTokenVerifier(broker_trust_store(), people=access_repo)
    set_process_verifier(verifier)

    scheduler_state: dict[str, Any] = {"identity": broker_scheduler_identity(TEAM)}
    scheduler = SchedulerOperations(
        store,
        people,
        TEAM,
        identity_fn=lambda: scheduler_state["identity"],
        verifier_fn=lambda: verifier,
        heartbeat_store_fn=lambda: HeartbeatStore(db_path=db_path, storage_settings=tasks.storage_settings),
        clock=lambda: datetime.now(timezone.utc),
        logger=logging.getLogger("test.scheduler"),
    )
    host = FakeSchedulerHost(scheduler, team=TEAM)
    monkeypatch.setattr(agent_requests, "_PROCESS_HOST", host)

    manager = SimpleNamespace(
        id=MANAGER_ID,
        team_id=TEAM,
        is_team_lead=True,
        task_manager=tasks,
        _schedule_store=store,
    )
    worker = worker_agent(tasks, store, WORKER_ID)
    return SimpleNamespace(
        tasks=tasks,
        store=store,
        access_repo=access_repo,
        people=people,
        verifier=verifier,
        scheduler=scheduler,
        scheduler_state=scheduler_state,
        host=host,
        bus=SimpleNamespace(published=host.requests),
        manager=manager,
        worker=worker,
        db_path=db_path,
    )


def worker_agent(tasks: TaskManager, store: ScheduleStore, agent_id: str) -> SimpleNamespace:
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
        main_lane_busy=True,  # the main lane (runtime_hosting.agent_execution)
        _should_exit=asyncio.Event(),
        _pending_work_task=None,
        _continue_pending_work=AsyncMock(),
        _conversation_store=None,
        _ensure_request_timezone=lambda meta, top_level_request: meta,
        _logger=logging.getLogger("test.worker"),
    )
    worker._enqueue_fired_scheduled_task = partial(enqueue_fired_scheduled_task, worker)
    return worker


def record_people(world, *people: tuple[str, PersonStanding, tuple[str, ...]]) -> None:
    """What the gateway keeps in the access store: each person's standing and current roles."""

    AccessRepository(world.tasks.storage_settings).replace_people(
        TEAM,
        [
            TeamPersonV1(
                person_ref=broker_person_ref(person_id),
                team_id=TEAM,
                person_id=person_id,
                standing=standing,
                roles=roles,
                changed_at_utc=NOW,
            )
            for person_id, standing, roles in people
        ],
        at=NOW,
    )


def record_ann(world, standing: PersonStanding, roles: tuple[str, ...] = ("member",)) -> None:
    record_people(world, (ANN, standing, roles))


def issue_permits(world, now: datetime | None = None) -> PermitPassResult:
    """Run one ``SchedulerOperations.renew_permits`` pass."""

    return world.scheduler.renew_permits(now=now)


def save_schedule(
    world,
    *,
    schedule_id: str = SCHEDULE_ID,
    owner: str | None = ANN,
    vouched: bool = True,
    heartbeat_source_id: str | None = None,
) -> ScheduledMessage:
    """A daily check for the research worker, saved from its owner's request.

    ``vouched``: the gateway signed the request's protocol 1 root-task token for the owner.
    """

    signed = None
    if vouched and owner:
        signed = AuthorityTokenRecord.model_validate(
            root_task_token(
                broker_gateway_identity(TEAM),
                team=TEAM,
                root_task_id=f"task_{schedule_id}",
                request_id=f"task_context_{schedule_id}",
                person_ref=broker_person_ref(owner),
                issued=datetime.now(timezone.utc),
            )
        )
    schedule = ScheduledMessage(
        schedule_id=schedule_id,
        schedule_name=f"heartbeat:{heartbeat_source_id}" if heartbeat_source_id else "Starship launch check",
        management_class=ScheduleManagementClass.SYSTEM if heartbeat_source_id else ScheduleManagementClass.USER,
        system_schedule_kind=SystemScheduleKind.HEARTBEAT_LAUNCHER if heartbeat_source_id else None,
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


async def subscribe(world) -> None:
    """Ensure the test process host is configured for ``svc.scheduler`` requests."""

    configure_process_host(world.host)


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


def run_context(task_info, world=None):
    gw = task_info.meta.gateway_context
    verifier = world.verifier if world is not None else access_runtime.context_verifier()
    lookup = ScheduleRunLookup.from_storage(world.tasks.storage_settings) if world is not None else None
    return resolve_task_identity(
        verifier=verifier,
        team_id=TEAM,
        task_context=gw.task_context,
        schedule_run=gw.schedule_run,
        schedule_runs=lookup,
    )


def claim_row(world) -> tuple:
    row = world.store._repository.get_schedule_row(SCHEDULE_ID)
    return row[15], row[16]


def access_agent(world, run) -> SimpleNamespace:
    """The worker as the access hook sees it while it runs ``run``."""

    access_runtime._runtime_access.cache_clear()
    bind_task(run.task.task_id)
    return SimpleNamespace(
        id=WORKER_ID,
        name="researcher",
        team_id=TEAM,
        named_agent_type=WORKER_TYPE,
        is_team_lead=False,
        access_role="worker",
        authorization_config={"enabled": True, "cedar_binary_path": str(CEDAR)},
        task_manager=world.tasks,
        _schedule_store=world.store,
        instance_root=None,
        test_task_id=run.task.task_id,
    )
