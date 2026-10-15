"""Disabling, removing and restoring a person: their work, records and held data.

Real SQLite gateway, access, task, schedule, conversation and memory stores,
real Cedar decisions for shares, the real identity service and person
lifecycle on the gateway side, and the real manager handler
(``custom_agents.person_departure_runtime``) reached in process with a
gateway-signed context. Covers:

- disable stops work and deletes schedules but keeps data; enable reinstalls
  memory review;
- remove deletes what is listed at once and holds the folder, personal memory
  and conversations; a link planted in the folder is moved as a link, never
  followed;
- restore within the window; purge after it; adding back afterwards starts fresh;
- team schedules move to another administrator;
- after-action report reviews stay with their person: the review schedule is
  deleted, never moved, and removal deletes the report records;
- a repeat changes nothing more;
- the safety net at manager start;
- a command that is not from the gateway for an administrator is refused.
"""

from __future__ import annotations

import asyncio
import logging
import os
import sqlite3
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock

import pytest

from access_catalog.catalog import load_catalog
from access_control import safe_open
from access_control.cedar import CedarAccessClient
from access_control.decisions import AccessService
from access_control.entities import PersonPrincipal, agent_role_resolver
from access_control.identity import ContextSigner
from access_control.requests import AccessRequests
from assistant_gateway.application.commands.context import GatewayActorContext
from assistant_gateway.application.commands.identity import IdentityCommandHandler, IdentityCommandRequest
from assistant_gateway.application.identity_service import GatewayIdentityService, GatewayIdentitySettings
from assistant_gateway.application.person_lifecycle import GatewayPersonLifecycle, GatewayPersonLifecycleSettings
from assistant_gateway.file_sharing import GatewayFileSharing
from assistant_gateway.people_directory_sync import PeopleDirectorySync
from assistant_gateway.person_departure import GatewayPersonDeparture
from assistant_gateway.runtime_ports import MembershipRoleIdentityAuthorizer
from assistant_gateway.store import GatewayStateStore
from custom_agents import task_context_runtime
from custom_agents.conversation_store import ConversationStore
from custom_agents.heartbeat_store import HeartbeatStore
from custom_agents.memory_review_store import MemoryReviewStore
from custom_agents.person_departure_runtime import (
    handle_person_standing_command,
    purge_expired_holds,
    settle_departed_people_safely,
)
from custom_agents.schedule_store import ScheduleStore
from custom_agents.task_manager import TaskManager
from custom_agents.task_records import ReportedWorkIdentity
from custom_messages.contracts.access_v2 import (
    AccessLevel,
    AccessNoticeKind,
    AccessNoticeV1,
    AccessRequestStatus,
    AccessRequestV1,
    GrantStatus,
    GrantV1,
    LinkOwnerKind,
    PersonStanding,
    SubjectKind,
    SubjectRefV1,
    TargetKind,
    TargetRefV1,
)
from custom_messages.contracts.control_v1 import PersonStandingCommandV1
from custom_messages.contracts.envelope_v1 import GATEWAY_MANAGER_CONTROL_TYPES
from custom_messages.contracts.routing_v1 import GatewayContext, MessageHeader, MessageMeta
from custom_messages.contracts.schedule_v1 import ScheduledMessage
from custom_messages.contracts.task_v1 import AfterActionReport, Task
from custom_messages.enums import (
    MessageMetaType,
    ObjectiveHorizon,
    ObjectiveStatus,
    ProgressState,
    ScheduleManagementClass,
    SystemScheduleKind,
)
from runtime_settings.instance_layout import InstanceLayout, person_folder_name
from storage.access_repository import AccessRepository
from storage.config import resolve_storage_settings
from storage.person_departure_repository import PersonDepartureRepository
from knowledge_base.records import KnowledgeActor
from knowledge_base.service import DraftInput, KnowledgeService
from knowledge_base.vocabulary import ActorKind
from runtime_settings.knowledge import KnowledgeSettings
from storage.knowledge_repository import create_knowledge_repository
from storage.pinned_memory_repository import create_pinned_memory_repository
from tests.system_schedule_kind_support import PERSON_KIND, STAND_IN_KIND, use_kind
from tests.broker_access_support import (
    context_signer,
    forget_task_context_keys,
    store_account_link,
    use_scheduler_key,
)

ROOT = Path(__file__).resolve().parents[1]
BINARY = ROOT / "target" / "debug" / "ua-cedar-authorizer"
TEAM = "team_departure"
LEAD = "TeamArchitect"
OLIVE, OTTO, ADAM, MIA = "olive", "otto", "adam", "mia"
ROLES = {OLIVE: "owner", OTTO: "owner", ADAM: "admin", MIA: "member"}
# The current hour, not a fixed date: the gateway reads this clock to set how
# long a removed person's data is held, while the manager's purge
# (settle_departed_people_safely) reads the real time, so a fixed start made the
# hold look expired once its seven days had passed on the calendar.
START = datetime.now(timezone.utc).replace(minute=0, second=0, microsecond=0)

pytestmark = pytest.mark.skipif(not BINARY.is_file(), reason="build ua-cedar-authorizer first (cargo build)")


class _Clock:
    def __init__(self) -> None:
        self.now = START

    def now_utc(self) -> datetime:
        return self.now


class _Secrets:
    def hash_secret(self, raw_secret: str) -> str:
        return f"hashed:{raw_secret}"

    def new_web_user_token(self) -> str:
        return "uauser_x"

    def new_web_user_invite(self) -> str:
        return "uainvite_x"

    def new_link_code(self) -> str:
        return "LINKCODE1"


class _Comms:
    def __init__(self) -> None:
        self.messages: list[Any] = []

    async def send_message(self, *, message: Any, routing_key: str) -> None:
        self.messages.append(message)


@dataclass
class _Inbox:
    finished: list[AccessRequestV1] = field(default_factory=list)

    def open_review(self, request: AccessRequestV1) -> None:
        return None

    def send_notice(self, notice: AccessNoticeV1) -> None:
        return None

    def request_finished(self, request: AccessRequestV1) -> None:
        self.finished.append(request)


class _ManagerInProcess:
    """The gateway's manager transport, delivering to the real manager handler with a gateway-signed context."""

    def __init__(self, agent: Any, signer: ContextSigner) -> None:
        self.agent = agent
        self.signer = signer
        self.available = True

    async def request_person_standing(
        self, command: PersonStandingCommandV1, *, actor_person_id: str
    ) -> PersonStandingCommandV1:
        if not self.available:
            raise RuntimeError("The manager is not available.")
        # The gateway signs the acting administrator with their current roles.
        signed = self.signer.issue(
            team_id=TEAM, person_id=actor_person_id, person_roles=(ROLES[actor_person_id],)
        )
        await handle_person_standing_command(
            self.agent,
            command=command,
            msg_header=MessageHeader(from_agent_id="gateway", response_routing_key="gateway"),
            msg_meta=MessageMeta(
                gateway_context=GatewayContext(canonical_user_id=actor_person_id, task_context=signed)
            ),
        )
        return self.agent.team_comms.messages[-1].msg_content


class _Broker:
    available = True

    def __init__(self) -> None:
        self.deletions: list[Any] = []
        # What the broker reports beyond the linked credentials (secure_tool_broker.departed_person_credentials).
        self.sign_ins: dict[str, Any] = {"sign_ins_deleted": ["github", "x"], "folder": "deleted"}

    async def delete_person_credentials(self, request: Any) -> dict[str, Any]:
        self.deletions.append(request)
        return {"deleted": list(request.credential_keys), "failed": [], **self.sign_ins}


class _PersonSchedules:
    def __init__(self) -> None:
        self.installed: list[str] = []

    async def ensure_person_schedules(self, person_id: str) -> None:
        self.installed.append(person_id)


@dataclass
class World:
    tmp: Path
    clock: _Clock
    layout: InstanceLayout
    gateway: GatewayStateStore
    access: AccessRepository
    departures: PersonDepartureRepository
    identity: GatewayIdentityService
    lifecycle: GatewayPersonLifecycle
    manager: _ManagerInProcess
    broker: _Broker
    person_schedules: _PersonSchedules
    agent: Any
    tasks: TaskManager
    schedules: ScheduleStore
    conversations: ConversationStore
    sharing: GatewayFileSharing
    service: AccessService

    def actor(self, person_id: str):
        return self.identity.resolve_user_by_id(person_id)

    def run(self, coroutine):
        return asyncio.run(coroutine)

    def disable(self, actor: str, target: str) -> str:
        return self.run(self.lifecycle.set_enabled(actor=self.actor(actor), target=target, enabled=False, channel_name=None, chat_scope=""))

    def enable(self, actor: str, target: str) -> str:
        return self.run(self.lifecycle.set_enabled(actor=self.actor(actor), target=target, enabled=True, channel_name=None, chat_scope=""))

    def remove(self, actor: str, target: str) -> str:
        return self.run(self.lifecycle.remove(actor=self.actor(actor), target=target, channel_name=None, chat_scope=""))

    def restore(self, actor: str, target: str) -> str:
        return self.run(self.lifecycle.restore(actor=self.actor(actor), target=target, channel_name=None, chat_scope=""))


@pytest.fixture
def world(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    use_scheduler_key(monkeypatch)
    task_context_runtime._verifier.cache_clear()
    monkeypatch.delenv("SECURITY_LAYER_MODE", raising=False)
    clock = _Clock()
    layout = InstanceLayout.at(tmp_path / "instance")
    layout.ensure_directories()
    storage = resolve_storage_settings({"backend": "sqlite", "sqlite": {"path": str(layout.task_database_path)}})
    gateway = GatewayStateStore(str(tmp_path / "gateway_state.db"))
    with gateway.identity_unit_of_work() as unit_of_work:
        for person_id, role in ROLES.items():
            unit_of_work.identity.create_gateway_user(canonical_user_id=person_id, display_name=person_id.title(), timezone="UTC")
            unit_of_work.identity.create_team_membership(canonical_user_id=person_id, team_id=TEAM, role=role)
            unit_of_work.identity.create_channel_binding(
                canonical_user_id=person_id, channel_name="telegram", external_user_id=f"tg_{person_id}"
            )
        unit_of_work.commit()
    access = AccessRepository(storage)
    directory = PeopleDirectorySync(identity=gateway.identity_repository, open_store=lambda: access, team_id=TEAM, clock=clock)
    directory.sync_all()
    identity = GatewayIdentityService(
        repository=gateway.identity_repository,
        unit_of_work_factory=gateway,
        authorizer=MembershipRoleIdentityAuthorizer(),
        secrets=_Secrets(),
        clock=clock,
        settings=GatewayIdentitySettings(
            enabled=True,
            allow_newuser_registration=True,
            web_user_tokens_enabled=True,
            web_user_token_ttl_days=0,
            web_user_invite_ttl_seconds=900,
            link_code_ttl_seconds=600,
            default_timezone="UTC",
            team_id=TEAM,
            web_chat_url="https://agents.example.test/webchat",
        ),
        people_directory=directory,
    )
    service = AccessService(
        store=access,
        catalog=load_catalog(None),
        cedar=CedarAccessClient(binary=str(BINARY)),
        team_id=TEAM,
        agent_role_of=agent_role_resolver(LEAD),
    )
    inbox = _Inbox()
    requests = lambda: AccessRequests(service=service, inbox=inbox, lead_agent_type=LEAD)  # noqa: E731
    people = {person_id: PersonPrincipal(person_id, TEAM, (role,)) for person_id, role in ROLES.items()}
    sharing = GatewayFileSharing(
        requests=requests,
        person=people.get,
        workspaces_dir=layout.workspaces_dir,
        team_people=lambda: [(person_id, person_id.title()) for person_id in ROLES],
        display_name=lambda person_id: person_id.title(),
        authorization_enabled=True,
    )
    tasks = TaskManager(storage_settings=storage, logger=logging.getLogger("test.departure.tasks"))
    tasks.set_owner_identity("manager_1", "Manager", TEAM, LEAD)
    schedules = ScheduleStore(storage_settings=storage)
    conversations = ConversationStore(db_path=str(layout.task_database_path), storage_settings=storage, artifacts_dir=layout.conversation_artifacts_dir)
    agent = SimpleNamespace(
        id="manager_1",
        name="Manager",
        is_team_lead=True,
        team_id=TEAM,
        task_manager=tasks,
        team_comms=_Comms(),
        instance_root=str(layout.root),
        workspace_dir=str(layout.workspaces_dir),
        named_agent_type_queue_map={},
        _agent_id_by_name={},
        _stop_reasons_by_task_id={},
        _active_execution_task=None,
        _active_execution_task_id=None,
        _strands_agent=SimpleNamespace(),
        _conversation_store=conversations,
        _logger=logging.getLogger("test.departure.manager"),
        _ensure_schedule_store=lambda: schedules,
        _scheduler_wake=SimpleNamespace(set=lambda: None),
        _finalize_conversation_turn=AsyncMock(return_value=None),
    )
    manager = _ManagerInProcess(agent, context_signer())
    departures = PersonDepartureRepository(storage)
    records = GatewayPersonDeparture(
        state_store=gateway,
        access=lambda: access,
        requests=requests,
        departures=lambda: departures,
        layout=layout,
        team_id=TEAM,
        clock=clock,
    )
    broker = _Broker()
    person_schedules = _PersonSchedules()
    lifecycle = GatewayPersonLifecycle(
        settings=GatewayPersonLifecycleSettings(team_id=TEAM, removed_data_retention_days=7),
        identity=identity,
        records=records,
        work=manager,
        broker=broker,  # type: ignore[arg-type]
        person_schedules=person_schedules,
        clock=clock,
    )
    yield World(
        tmp_path, clock, layout, gateway, access, departures, identity, lifecycle, manager, broker, person_schedules,
        agent, tasks, schedules, conversations, sharing, service,
    )
    service.cedar.close()
    forget_task_context_keys()
    task_context_runtime._verifier.cache_clear()


# -- what a person has ------------------------------------------------------------------


def _context(person_id: str) -> GatewayContext:
    return GatewayContext(canonical_user_id=person_id, session_key=f"session_{person_id}")


def _schedule(schedule_id: str, owner: str, *, kind: SystemScheduleKind | None = None) -> ScheduledMessage:
    return ScheduledMessage(
        schedule_id=schedule_id,
        schedule_name=f"name of {schedule_id}",
        management_class=ScheduleManagementClass.SYSTEM if kind else ScheduleManagementClass.USER,
        system_schedule_kind=kind,
        claim_team_id=TEAM,
        payload=Task(task_type=MessageMetaType.REQUEST, problem_statement="Do it."),
        payload_header=MessageHeader(from_agent_id="gateway", to_routing_path="manager_1"),
        payload_meta=MessageMeta(gateway_context=_context(owner)),
        interval_ms=86_400_000,
    )


def _give_work(world: World, person_id: str) -> dict[str, str]:
    """A running task tree, an objective, schedules, memory, a conversation, a folder, access records."""

    root = Task(task_id=f"task_{person_id}", task_type=MessageMetaType.REQUEST, problem_statement="Work.")
    assert world.tasks.add_task(root, MessageHeader(from_agent_id="gateway"), MessageMeta(conversation_id=f"conv_{person_id}", gateway_context=_context(person_id)), owner_person_id=person_id)
    world.tasks.update_main_task_state(root.task_id, ProgressState.IN_PROGRESS, START.isoformat())
    objective = world.tasks.create_objective(
        task_id=root.task_id, title="Keep going", desired_outcome="Done", why="Needed", horizon=ObjectiveHorizon.RECURRING
    )
    assert objective is not None and objective.owner_canonical_user_id == person_id
    world.tasks.update_objective_status(objective.objective_id, ObjectiveStatus.ACTIVE, summary="Started.")
    world.schedules.add(_schedule(f"sched_{person_id}", person_id))
    world.schedules.add(_schedule(f"sched_{person_id}_usermem", person_id, kind=SystemScheduleKind.MEMORY_REVIEW_USERMEM))
    create_pinned_memory_repository(world.tasks.storage_settings).create_usermem(
        canonical_user_id=person_id, fact_text=f"{person_id} likes tea.", capacity=50
    )
    world.conversations.ensure_thread(
        conversation_id=f"conv_{person_id}", team_id=TEAM, owner_agent_id="manager_1", owner_agent_name="Manager",
        requester_name=person_id, requester_id=person_id, root_task_id=root.task_id, owner_canonical_user_id=person_id,
    )
    world.conversations.append_turn(
        conversation_id=f"conv_{person_id}", task_id=root.task_id, followup_to_task_id=None, owner_agent_id="manager_1",
        requester_name=person_id, requester_id=person_id, correlation_id=None, problem_text="Hello.",
        owner_canonical_user_id=person_id,
    )
    _give_knowledge(world, person_id)
    MemoryReviewStore(storage_settings=world.tasks.storage_settings).enqueue_review_job(
        owner_team_id=TEAM, owner_named_agent_type="MemReviewAgent", task_id=root.task_id,
        conversation_id=f"conv_{person_id}", turn_index=0, source_payload={"canonical_user_id": person_id},
    )
    home = world.layout.person_dir(person_id)
    (home / "python_envs" / "tools" / ".venv").mkdir(parents=True)
    (home / "notes.md").write_text("mine", encoding="utf-8")
    world.gateway.create_session(
        session_key=f"session_{person_id}", conversation_id=f"conv_{person_id}", manager_agent_id="manager_1",
        owner_canonical_user_id=person_id,
    )
    share = world.sharing.share(person_id, "notes.md", "Olive", "read")
    for_work = world.access.create(
        GrantV1(
            grant_id=f"grant_{person_id}_work", team_id=TEAM,
            subject=SubjectRefV1(kind=SubjectKind.AGENT_TYPE, id="WebResearchAgent"),
            target=TargetRefV1(kind=TargetKind.TOOL, id="internet"), level=AccessLevel.READ,
            granted_by=SubjectRefV1(kind=SubjectKind.USER, id=person_id), created_at_utc=START, for_person_id=person_id,
        )
    )
    link = store_account_link(world.access, "cli:gh", f"{person_id}_github", owner_id=person_id, team_id=TEAM)
    world.access.create_request(
        AccessRequestV1(
            request_id=f"request_{person_id}", team_id=TEAM, requester_agent_id="agent_web",
            requester_agent_type="WebResearchAgent", person_id=person_id,
            subject=SubjectRefV1(kind=SubjectKind.AGENT_TYPE, id="WebResearchAgent"),
            target=TargetRefV1(kind=TargetKind.TOOL, id="internet"), level=AccessLevel.READ,
            reason="Look something up.", task_id=root.task_id, created_at_utc=START,
        )
    )
    world.access.create_notice(
        AccessNoticeV1(
            notice_id=f"notice_{person_id}", team_id=TEAM, kind=AccessNoticeKind.SHARED,
            recipient_person_id=person_id, title="Hello", summary="Something.", created_at_utc=START,
        )
    )
    world.access.record_profile_choice(TEAM, person_id, "standard", source="test", at=START)
    return {"task": root.task_id, "objective": objective.objective_id, "share": share.grant_id,
            "for_work": for_work.grant_id, "link": link.link_id}


def _knowledge(world: World) -> KnowledgeService:
    return KnowledgeService(create_knowledge_repository(world.tasks.storage_settings), settings=KnowledgeSettings())


def _knowledge_actor(person_id: str = "") -> KnowledgeActor:
    return KnowledgeActor(kind=ActorKind.AGENT, actor_id=f"agent_for_{person_id or 'nobody'}", team_id=TEAM,
                          work_person_id=person_id, task_id=f"task_{person_id}", work_id=f"ctx_{person_id}")


def _give_knowledge(world: World, person_id: str) -> None:
    """A personal page with a merged page's name leading to it, a pending team contribution, and a team page."""

    knowledge = _knowledge(world)
    actor = _knowledge_actor(person_id)
    tea = knowledge.write(actor, "personal/tea", DraftInput(title="Tea", body=f"{person_id} keeps green tea notes.")).page
    more = knowledge.write(actor, "personal/tea-notes", DraftInput(title="Tea notes", body="Sencha first.")).page
    knowledge.merge(
        actor, "personal/tea-notes", "personal/tea", DraftInput(body=f"{person_id} keeps green tea notes. Sencha first."),
        base_revision=tea.revision, source_revision=more.revision,
    )
    knowledge.write(actor, f"team/idea-{person_id}", DraftInput(title="Idea", body="A team idea."))
    knowledge.write(_knowledge_actor(), "team/handbook", DraftInput(title="Handbook", body="How the team works."))


def _rows(world: World, table: str, where: str, *params: str) -> int:
    with sqlite3.connect(world.layout.task_database_path) as conn:
        return conn.execute(f"SELECT COUNT(*) FROM {table} WHERE {where}", params).fetchone()[0]


def _usermem(world: World, person_id: str) -> list:
    return create_pinned_memory_repository(world.tasks.storage_settings).list_usermem(canonical_user_id=person_id)


def _status(world: World, grant_id: str) -> GrantStatus:
    grant = world.access.get(grant_id)
    assert grant is not None
    return grant.status


# -- disable and enable -------------------------------------------------------------------


def test_disable_stops_work_and_deletes_schedules_but_keeps_data(world: World) -> None:
    ids = _give_work(world, MIA)

    reply = world.disable(ADAM, MIA)

    assert reply.startswith("Disabled Mia (mia). They cannot use the team")
    assert "Stopped: 1 running or queued task(s) and 1 objective(s) (stopped; kept, paused). Deleted: 2 schedule(s)." in reply
    assert "Withdrawn: 1 waiting access request(s)." in reply and "Not done" not in reply
    # Work ended.
    assert world.tasks.is_execution_cancelled(ids["task"])
    assert world.schedules.get_scheduled_message(f"sched_{MIA}") is None
    assert world.schedules.get_scheduled_message(f"sched_{MIA}_usermem") is None
    assert world.tasks.get_objective(ids["objective"]).status is ObjectiveStatus.PAUSED
    assert world.access.get_request(f"request_{MIA}").status is AccessRequestStatus.WITHDRAWN
    # Data kept.
    assert _usermem(world, MIA) and world.conversations.get_thread(f"conv_{MIA}") is not None
    assert (world.layout.person_dir(MIA) / "notes.md").is_file()
    assert _status(world, ids["share"]) is GrantStatus.ACTIVE and _status(world, ids["for_work"]) is GrantStatus.ACTIVE
    assert world.access.get_link(ids["link"]).status.value == "active"
    assert world.departures.hold_of(TEAM, MIA) is None and world.person_schedules.installed == []
    # Sign-ins are kept: disabling never asks the broker to delete anything.
    assert "linked accounts and sign-ins" in reply and world.broker.deletions == []


def test_enabling_again_reinstalls_memory_review_but_not_the_deleted_schedules(world: World) -> None:
    _give_work(world, MIA)
    world.disable(ADAM, MIA)

    reply = world.enable(ADAM, MIA)

    assert reply.startswith("Enabled Mia (mia).")
    assert "Their memory-review schedules are installed." in reply and "do not come back" in reply
    assert world.person_schedules.installed == [MIA]
    assert world.schedules.get_scheduled_message(f"sched_{MIA}") is None


# -- remove, restore, purge ---------------------------------------------------------------


def test_remove_deletes_at_once_and_holds_the_folder_memory_and_conversations(world: World) -> None:
    ids = _give_work(world, MIA)
    outside = world.tmp / "outside.txt"
    outside.write_text("not hers", encoding="utf-8")
    os.symlink(outside, world.layout.person_dir(MIA) / "planted")

    reply = world.remove(ADAM, MIA)

    assert reply.startswith("Removed Mia (mia) from the team.")
    assert f"Held until {START + timedelta(days=7):%Y-%m-%d %H:%M} UTC, then deleted: their folder, personal memory, own knowledge pages and conversations." in reply
    assert "/user restore mia" in reply and "Not done" not in reply
    assert "Deleted by the broker: their GitHub and X sign-in(s) and their private credentials folder." in reply
    # Deleted at once: gateway records, access, credentials, manager records.
    assert world.gateway.get_session(f"session_{MIA}") is None
    assert _status(world, ids["share"]) is GrantStatus.REVOKED and _status(world, ids["for_work"]) is GrantStatus.REVOKED
    assert world.access.get_link(ids["link"]).status.value == "revoked"
    assert [request.credential_keys for request in world.broker.deletions] == [(f"{MIA}_github",)]
    assert world.access.get_request(f"request_{MIA}") is None
    assert world.access.notices_for(TEAM, person_id=MIA, roles=()) == []
    assert world.access.profile_choice(TEAM, MIA) is None
    assert world.tasks.get_objective(ids["objective"]) is None
    assert _rows(world, "memory_review_jobs", "1=1") == 0
    assert _rows(world, "completed_tasks", "owner_canonical_user_id = ?", MIA) == 0
    # Held: out of the tables agents read, and the folder out of workspaces/.
    assert _usermem(world, MIA) == [] and world.conversations.get_thread(f"conv_{MIA}") is None
    assert world.departures.held_row_count(TEAM, MIA) > 0
    knowledge = _knowledge(world)
    assert knowledge.search(_knowledge_actor(MIA), query="green tea") == []
    assert knowledge.list_contributions(_knowledge_actor(MIA)) == []
    assert _rows(world, "knowledge_write_events", "work_person_id = ?", MIA) == 0
    assert _rows(world, "knowledge_redirects", "owner_person_id = ?", MIA) == 0
    assert [hit.page.reference() for hit in knowledge.search(_knowledge_actor(), query="handbook")] == ["team/handbook"]
    held = world.layout.held_people_dir / person_folder_name(MIA)
    assert not world.layout.person_dir(MIA).exists() and (held / "notes.md").is_file()
    # The planted link moved as a link; the file it points at was not touched.
    assert (held / "planted").is_symlink() and outside.read_text(encoding="utf-8") == "not hers"
    # Kept: the audit record.
    assert any(event.kind == "grant_revoked" for event in world.access.events(TEAM))


def test_restore_within_the_window_brings_the_held_data_back(world: World) -> None:
    _give_work(world, MIA)
    world.remove(ADAM, MIA)
    world.clock.now = START + timedelta(days=3)

    reply = world.restore(ADAM, MIA)

    assert reply.startswith("Added Mia (mia) back to the team as a member.")
    assert "Restored their folder." in reply and "record(s) of their personal memory, own knowledge pages and conversations" in reply
    assert "do not come back" in reply and "they sign in again with /link" in reply
    assert world.identity.resolve_user_by_id(MIA) is not None
    assert (world.layout.person_dir(MIA) / "notes.md").is_file()
    assert _usermem(world, MIA) and world.conversations.get_thread(f"conv_{MIA}") is not None
    knowledge = _knowledge(world)
    assert [hit.page.reference() for hit in knowledge.search(_knowledge_actor(MIA), query="green tea")] == ["personal/tea"]
    assert knowledge.read(_knowledge_actor(MIA), "personal/tea-notes").page.slug == "tea", "the merged name came back"
    assert [c.slug for c in knowledge.list_contributions(_knowledge_actor(MIA))] == [f"idea-{MIA}"]
    assert world.departures.held_row_count(TEAM, MIA) == 0
    assert world.person_schedules.installed == [MIA]
    assert world.schedules.get_scheduled_message(f"sched_{MIA}") is None  # schedules do not return
    assert "was not removed from the team" in world.restore(ADAM, MIA)


def test_after_the_window_restore_explains_and_a_maintenance_run_purges(world: World) -> None:
    _give_work(world, MIA)
    outside = world.tmp / "outside.txt"
    outside.write_text("not hers", encoding="utf-8")
    os.symlink(outside, world.layout.person_dir(MIA) / "planted")
    world.remove(ADAM, MIA)
    world.clock.now = START + timedelta(days=8)

    reply = world.restore(ADAM, MIA)
    purged = purge_expired_holds(world.agent, now=world.clock.now)

    assert f"held until {START + timedelta(days=7):%Y-%m-%d %H:%M} UTC; they can no longer be restored" in reply and "starts fresh" in reply
    assert world.identity.team_standing(MIA) is PersonStanding.REMOVED  # nothing changed
    assert purged == 1
    assert not (world.layout.held_people_dir / person_folder_name(MIA)).exists()
    assert outside.read_text(encoding="utf-8") == "not hers"  # the purge removed the link, not its target
    assert world.departures.held_row_count(TEAM, MIA) == 0
    assert world.departures.hold_of(TEAM, MIA).purged_at is not None
    assert purge_expired_holds(world.agent, now=world.clock.now) == 0
    # Adding her back now starts fresh.
    added = world.run(
        world.lifecycle.after_added(
            world.actor(ADAM),
            world.identity.register_user_for_channel(
                actor=world.actor(ADAM), display_name="Mia", channel_name="telegram", external_user_id="tg_mia",
                chat_scope="", thread_id=None,
            ),
        )
    )
    assert added.startswith("Added Mia (mia) back") and "Restored" not in added
    assert not world.layout.person_dir(MIA).exists() and _usermem(world, MIA) == []


def test_sign_ins_the_broker_could_not_delete_are_reported(world: World) -> None:
    _give_work(world, MIA)
    world.broker.sign_ins = {"sign_ins_failed": ["google"], "folder": "kept"}

    reply = world.remove(ADAM, MIA)

    assert "Deleted by the broker" not in reply
    assert (
        "Not done (see the gateway and manager logs): Their Google sign-in(s) could not be deleted, so their "
        "private credentials folder is kept for now; run /user remove mia again."
    ) in reply


def test_repeating_remove_changes_nothing_more(world: World) -> None:
    _give_work(world, MIA)
    world.remove(ADAM, MIA)
    hold = world.departures.hold_of(TEAM, MIA)
    world.clock.now = START + timedelta(hours=1)

    reply = world.remove(ADAM, MIA)

    assert reply.startswith("Mia (mia) was already removed from the team.")
    assert "Stopped: 0 running or queued task(s) and 0 objective(s)" in reply and "Deleted: 0 schedule(s)" in reply
    assert "Not done" not in reply
    assert world.departures.hold_of(TEAM, MIA).hold_until == hold.hold_until  # the running hold is kept
    assert (world.layout.held_people_dir / person_folder_name(MIA) / "notes.md").is_file()


def test_team_schedules_move_to_another_administrator(world: World) -> None:
    world.schedules.add(_schedule("sched_team_maintenance", OLIVE, kind=SystemScheduleKind.INTERNAL_IMPROVEMENT))
    world.schedules.add(_schedule("sched_olive_usermem", OLIVE, kind=SystemScheduleKind.MEMORY_REVIEW_USERMEM))

    reply = world.remove(OTTO, OLIVE)

    moved = world.schedules.get_scheduled_message("sched_team_maintenance")
    assert moved is not None and moved.payload_meta.gateway_context.canonical_user_id == OTTO
    assert moved.payload_meta.gateway_context.session_key is None  # not the departed person's chat
    assert world.schedules.get_scheduled_message("sched_olive_usermem") is None  # her own memory review
    assert "The team schedules they owned now run for otto: name of sched_team_maintenance." in reply
    notices = [notice for notice, _read in world.access.notices_for(TEAM, person_id=ADAM, roles=("admin",))]
    assert any("now belong to otto" in notice.title for notice in notices)
    # The gateway's refresh chooses the same person for the team's schedules.
    assert world.identity.team_work_owner(OLIVE) == OTTO


def _turn(world: World, conversation_id: str, person_id: str, task_id: str) -> None:
    """A finished turn of the person in the conversation, which is made for them when missing."""

    world.conversations.ensure_thread(
        conversation_id=conversation_id, team_id=TEAM, owner_agent_id="manager_1", owner_agent_name=LEAD,
        requester_name="gateway", requester_id="gateway", root_task_id=task_id, owner_canonical_user_id=person_id,
    )
    world.conversations.append_turn(
        conversation_id=conversation_id, task_id=task_id, followup_to_task_id=None, owner_agent_id="manager_1",
        requester_name="gateway", requester_id="gateway", correlation_id=None, problem_text="Private question.",
        owner_canonical_user_id=person_id,
    )


def _turn_ids(world: World, conversation_id: str) -> list[str]:
    turns = world.conversations.get_conversation_turns(conversation_id=conversation_id, start_turn=1, limit=20)
    return [turn["task_id"] for turn in turns]


def test_moved_team_work_continues_in_new_conversations_and_a_restore_keeps_hers(world: World, caplog) -> None:
    """A conversation belongs to one person (``custom_agents.conversation_runtime``): what moves to another
    administrator continues in new conversations, so a restore puts the person's own back unmixed."""

    audit = _schedule("sched_team_audit", OLIVE, kind=SystemScheduleKind.MEMORY_REVIEW_COREMEM)
    audit.payload_meta.conversation_id = "conv_olive_proactive"
    world.schedules.add(audit)
    launcher = _schedule("sched_heartbeat", OLIVE, kind=SystemScheduleKind.HEARTBEAT_LAUNCHER)
    launcher.schedule_name = "heartbeat:main"
    launcher.payload_meta.gateway_context.heartbeat_source_id = "hb_main"
    world.schedules.add(launcher)
    heartbeat = HeartbeatStore(storage_settings=world.tasks.storage_settings)
    heartbeat.ensure_source(source_id="hb_main")
    item = heartbeat.add_item(source_id="hb_main", title="Inbox", instruction="Summarize my inbox.", conversation_id="conv_item")
    _turn(world, "conv_olive_proactive", OLIVE, "olive_audit")
    _turn(world, "conv_item", OLIVE, "olive_item_run")

    world.remove(OTTO, OLIVE)

    moved = world.schedules.get_scheduled_message("sched_team_audit")
    assert moved.payload_meta.gateway_context.canonical_user_id == OTTO
    assert moved.payload_meta.conversation_id not in (None, "conv_olive_proactive")
    assert heartbeat.get_item(item.heartbeat_item_id).conversation_id not in (None, "conv_item")
    assert world.conversations.get_thread("conv_olive_proactive") is None  # held with her records
    # Otto's runs start the new conversations as his.
    for conversation_id, task_id in (
        (moved.payload_meta.conversation_id, "otto_audit"),
        (heartbeat.get_item(item.heartbeat_item_id).conversation_id, "otto_item_run"),
    ):
        _turn(world, conversation_id, OTTO, task_id)

    with caplog.at_level(logging.WARNING):
        world.restore(OTTO, OLIVE)

    assert "person_held_row_conflict" not in caplog.text
    for conversation_id, task_id in (("conv_olive_proactive", "olive_audit"), ("conv_item", "olive_item_run")):
        assert world.conversations.get_thread(conversation_id)["owner_canonical_user_id"] == OLIVE
        assert _turn_ids(world, conversation_id) == [task_id]
    assert _turn_ids(world, moved.payload_meta.conversation_id) == ["otto_audit"]


def _report_on_finished_work(world: World, person_id: str) -> None:
    """A finished task whose specialist reported back afterwards, about this person's work."""

    parent = Task(task_id=f"task_aar_{person_id}", task_type=MessageMetaType.REQUEST, problem_statement="Research.")
    assert world.tasks.add_task(parent, MessageHeader(from_agent_id="gateway"), MessageMeta(), owner_person_id=None)
    world.tasks.update_main_task_state(parent.task_id, ProgressState.IN_PROGRESS, START.isoformat())
    world.tasks.complete_task(parent.task_id, START.isoformat())
    recorded = world.tasks.record_after_action_report(
        AfterActionReport(report_id=f"aar_{person_id}", parent_task_id=parent.task_id, summary="A lesson."),
        identity=ReportedWorkIdentity(person_id=person_id),
        timestamp=(START + timedelta(seconds=5)).isoformat(),
    )
    assert recorded is not None and recorded.batch is not None
    world.schedules.add(_schedule(f"sched_aar_{person_id}", person_id, kind=SystemScheduleKind.AFTER_ACTION_REPORT_REVIEW))


def test_after_action_reviews_stay_with_their_person(world: World) -> None:
    _report_on_finished_work(world, MIA)
    _report_on_finished_work(world, OTTO)

    reply = world.disable(ADAM, MIA)

    # Her review reads reports about her work, so it is deleted, never moved to an administrator.
    assert world.schedules.get_scheduled_message(f"sched_aar_{MIA}") is None
    assert "now run for" not in reply
    assert [review.report_id for review in world.tasks.list_after_action_report_reviews(person_id=MIA)] == [f"aar_{MIA}"]

    world.remove(ADAM, MIA)

    assert _rows(world, "after_action_report_reviews", "person_id = ?", MIA) == 0
    assert _rows(world, "after_action_report_review_batches", "person_id = ?", MIA) == 0
    # Someone else's are untouched.
    assert _rows(world, "after_action_report_reviews", "person_id = ?", OTTO) == 1
    assert world.schedules.get_scheduled_message(f"sched_aar_{OTTO}") is not None


def test_a_new_per_person_system_kind_is_deleted_with_its_person_and_never_moved(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Whose a system schedule is comes from the system schedule table: a new
    per-person row is deleted like memory review's pair, with no code change."""

    use_kind(monkeypatch, PERSON_KIND)
    world.schedules.add(_schedule("sched_olive_curation", OLIVE, kind=STAND_IN_KIND))
    world.schedules.add(_schedule("sched_team_maintenance", OLIVE, kind=SystemScheduleKind.INTERNAL_IMPROVEMENT))

    reply = world.remove(OTTO, OLIVE)

    assert world.schedules.get_scheduled_message("sched_olive_curation") is None
    moved = world.schedules.get_scheduled_message("sched_team_maintenance")
    assert moved is not None and moved.payload_meta.gateway_context.canonical_user_id == OTTO
    assert "now run for otto: name of sched_team_maintenance." in reply


# -- the manager's side on its own ----------------------------------------------------------


def test_the_safety_net_at_manager_start_ends_work_the_manager_missed(world: World) -> None:
    ids = _give_work(world, MIA)
    world.manager.available = False

    reply = world.remove(ADAM, MIA)

    assert "The manager has not confirmed yet" in reply
    assert not world.tasks.is_execution_cancelled(ids["task"]) and _usermem(world, MIA)
    world.run(settle_departed_people_safely(world.agent, when="manager_start"))
    assert world.tasks.is_execution_cancelled(ids["task"])
    assert world.schedules.get_scheduled_message(f"sched_{MIA}") is None
    assert _usermem(world, MIA) == [] and world.departures.held_row_count(TEAM, MIA) > 0


def _send(world: World, *, signed: Any, principal: str = ADAM, roles: tuple[str, ...] = ("admin",)) -> PersonStandingCommandV1:
    command = PersonStandingCommandV1(
        person_id=MIA,
        standing=PersonStanding.REMOVED,
    )
    world.run(
        handle_person_standing_command(
            world.agent,
            command=command,
            msg_header=MessageHeader(from_agent_id="somebody", response_routing_key="somebody"),
            msg_meta=MessageMeta(gateway_context=GatewayContext(canonical_user_id=principal, task_context=signed)),
        )
    )
    return world.agent.team_comms.messages[-1].msg_content


def test_a_command_not_from_the_gateway_for_an_administrator_is_refused(world: World) -> None:
    ids = _give_work(world, MIA)
    world.gateway.remove_team_membership(canonical_user_id=MIA, team_id=TEAM)
    world.identity._people_directory.record(MIA)  # the copy says removed; only the command is in question
    scheduler = ContextSigner.from_env("scheduler")
    assert scheduler is not None

    unsigned = _send(world, signed=None)
    by_scheduler = _send(world, signed=scheduler.issue(team_id=TEAM, person_id=ADAM, person_roles=("admin",)))
    by_member = _send(world, signed=context_signer().issue(team_id=TEAM, person_id=OTTO, person_roles=("member",)), principal=OTTO, roles=("member",))

    assert unsigned.success is False and unsigned.message.startswith("unsigned_sender")
    assert (by_scheduler.success, by_scheduler.message) == (False, "sender_not_gateway")
    assert (by_member.success, by_member.message) == (False, "administrator_required")
    assert not world.tasks.is_execution_cancelled(ids["task"])
    assert world.schedules.get_scheduled_message(f"sched_{MIA}") is not None
    # Agents cannot send it at all (custom_tools.providers.communication).
    assert PersonStandingCommandV1 in GATEWAY_MANAGER_CONTROL_TYPES


# -- pieces ----------------------------------------------------------------------------------


def test_a_link_in_place_of_the_folder_is_not_moved(tmp_path: Path) -> None:
    root = tmp_path / "root"
    (root / "workspaces" / "users").mkdir(parents=True)
    (tmp_path / "elsewhere").mkdir()
    os.symlink(tmp_path / "elsewhere", root / "workspaces" / "users" / "mia")

    with pytest.raises(safe_open.UnsafePath) as refused:
        safe_open.move_directory(root, ("workspaces", "users", "mia"), root, ("state", "held_people", "mia"))

    assert refused.value.reason == safe_open.REASON_LINK and (tmp_path / "elsewhere").is_dir()


@pytest.mark.asyncio
async def test_the_user_restore_command_reaches_the_restore_operation() -> None:
    calls: list[dict[str, object]] = []

    class Operations:
        async def restore_gateway_user(self, **request: object) -> str:
            calls.append(request)
            return "restored"

    handler = IdentityCommandHandler(Operations())  # type: ignore[arg-type]
    actor = GatewayActorContext(channel_name="web", canonical_user_id="adam")

    assert await handler.execute(IdentityCommandRequest(command="/user restore mia", actor=actor)) == "restored"
    assert (await handler.execute(IdentityCommandRequest(command="/user restore", actor=actor))).startswith(
        "Usage: /user restore"
    )
    assert calls[0]["target"] == "mia" and calls[0]["canonical_user_id"] == "adam"


def test_the_teams_account_link_is_kept(world: World) -> None:
    link = store_account_link(world.access, "cli:gh", "team_github", owner_kind=LinkOwnerKind.TEAM, owner_id=TEAM, team_id=TEAM)

    world.remove(ADAM, MIA)

    assert world.access.get_link(link.link_id).status.value == "active"
