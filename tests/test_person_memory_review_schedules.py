"""Per-person memory review schedules (``assistant_gateway.application.maintenance_schedules``).

Real SQLite gateway store (people, sessions, scheduled targets), the manager's
schedule handling and SQLite schedule store, signed task contexts, and the
MemReviewAgent run preparation over real conversation, memory-review and
pinned-memory stores. Covers:

- at gateway start every active person gets their own usermem audit and
  stale-conversation pair, owned by them; disabled and removed people get none;
  the coremem audit stays one team schedule;
- each pair is delivered to that person's own chat: their default chat, else
  their most recent chat session, else no route (installed anyway, logged);
- ``ensure_person_schedules`` is safe to call again and keeps a pause;
- each run prepares jobs only from its own person's facts and conversations;
- with gateway users off, the configured user gets the pair on the team's
  proactive session;
- a new per-person kind in the system schedule table (a stand-in,
  ``tests/system_schedule_kind_support.py``) is installed for each active
  person the same way, with only its row and its gateway settings.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from access_control.identity import ContextVerifier
from assistant_gateway.application.delivery_service import GatewayDeliveryService
from assistant_gateway.application.identity_service import (
    GatewayIdentityService,
    GatewayIdentitySettings,
)
from assistant_gateway.application.maintenance_schedules import (
    GatewayHeartbeatScheduleSettings,
    GatewayInternalImprovementScheduleSettings,
    GatewayMaintenanceScheduleApplication,
    GatewayMaintenanceScheduleSettings,
    GatewayPersonSystemScheduleDefinition,
    GatewaySpecialistScheduleSettings,
    GatewaySystemScheduleDefinition,
)
from assistant_gateway.application.person_maintenance import (
    GatewayPersonMaintenanceSessions,
    GatewayPersonMaintenanceSettings,
    person_maintenance_session_key,
)
from assistant_gateway.application.ports import GatewayPersonSchedules
from assistant_gateway.application.request_context import (
    GatewayRequestContextService,
    GatewayRequestContextSettings,
)
from assistant_gateway.application.session_service import GatewaySessionService
from assistant_gateway.runtime_ports import (
    MembershipRoleIdentityAuthorizer,
    OwnershipPrivateResourceAuthorizer,
    UuidGatewayIdGenerator,
)
from assistant_gateway.store import GatewayStateStore
from custom_agents.conversation_store import ConversationStore
from custom_agents.memory_review_runtime import memory_review_schedule_kind
from custom_agents.memory_review_store import MemoryReviewStore
from custom_agents.schedule_management import (
    ScheduleMutationAuthorityMode,
    ScheduleMutationOperation,
    build_schedule_mutation_permit,
)
from custom_agents.scheduled_runtime import (
    handle_scheduled_message,
    prepare_memory_review_schedule,
)
from custom_messages.contracts.routing_v1 import GatewayContext
from custom_messages.contracts.schedule_v1 import ScheduledMessage
from custom_messages.contracts.task_v1 import Task
from custom_messages.enums import (
    MessageMetaType,
    RequesterType,
    ScheduleManagementClass,
    SystemScheduleKind,
)
from custom_messages.system_schedules import (
    COREMEM_AUDIT_SCHEDULE_NAME_PREFIX,
    is_person_system_schedule,
    person_system_schedule_id,
)
from id_generation import (
    MEMORY_REVIEW_CORE_ID_PREFIX,
    MEMORY_REVIEW_STALE_ID_PREFIX,
    MEMORY_REVIEW_USER_ID_PREFIX,
)
from storage.pinned_memory_repository import SqlitePinnedMemoryRepository
from tests.broker_access_support import (
    context_signer,
    forget_task_context_keys,
    use_scheduler_key,
)
from tests.system_schedule_kind_support import (
    CURATOR_AGENT_TYPE,
    CURATOR_DELIVERY_MODE,
    PERSON_KIND,
    PERSON_NAME_PREFIX,
    STAND_IN_KIND,
    use_kind,
)
from tests.test_internal_improvement_runtime import _Manager

INSTANCE = "test"
TEAM = _Manager.team_id
CORE_ID = f"{MEMORY_REVIEW_CORE_ID_PREFIX}_{INSTANCE}"
OLIVE, MIA, NOAH, DORA, REMY = "olive", "mia", "noah", "dora", "remy"
ACTIVE = (OLIVE, MIA, NOAH)
PROACTIVE_SESSION = "heartbeat_session"
UNREACHABLE_EVENT = "event=gateway_person_maintenance_unreachable"
CURATION_ID_PREFIX = "kc_person"


def _usermem_id(person: str) -> str:
    return person_system_schedule_id(MEMORY_REVIEW_USER_ID_PREFIX, INSTANCE, person)


def _stale_id(person: str) -> str:
    return person_system_schedule_id(MEMORY_REVIEW_STALE_ID_PREFIX, INSTANCE, person)


class _ManagerRoute:
    """The gateway's manager client: what it publishes goes to the manager's inbox."""

    def __init__(self) -> None:
        self.manager_id: str | None = "manager_1"
        self.published: list[Any] = []

    async def publish_durable(self, envelope: Any, *, routing_key: str) -> None:
        self.published.append(envelope)


def _seed_people(store: GatewayStateStore) -> None:
    """Olive (owner, default chat), Mia and Noah (members), Dora (disabled), Remy (removed)."""

    people = {OLIVE: ("owner", "active"), MIA: ("member", "active"), NOAH: ("member", "active"),
              DORA: ("member", "active"), REMY: ("member", "removed")}
    with store.identity_unit_of_work() as unit_of_work:
        for person_id, (role, status) in people.items():
            unit_of_work.identity.create_gateway_user(canonical_user_id=person_id, display_name=person_id.title(), timezone="UTC")
            unit_of_work.identity.create_team_membership(canonical_user_id=person_id, team_id=TEAM, role=role, status=status)
        unit_of_work.commit()
    identity = store.identity_repository
    identity.set_gateway_user_status(DORA, "disabled")
    identity.set_gateway_user_default_channel(canonical_user_id=OLIVE, channel_name="telegram", chat_id="chat_olive", thread_id="topic_1")
    # Mia has no default chat; she last used Web Chat (an older Telegram chat before that).
    store.create_session(session_key="telegram_mia", conversation_id="conv_mia_tg", manager_agent_id=None,
                         channel_name="telegram", user_id="tg_mia", chat_id="chat_mia", owner_canonical_user_id=MIA)
    store.create_session(session_key="website:site:client_mia", conversation_id="conv_mia_web", manager_agent_id=None,
                         channel_name="website", user_id="client_mia", chat_id="site", thread_id="site",
                         owner_canonical_user_id=MIA)
    store.upsert_session(session_key="website:site:client_mia", conversation_id="conv_mia_web", last_task_id="t_mia",
                         manager_agent_id=None, channel_name="website", user_id="client_mia", chat_id="site",
                         thread_id="site", owner_canonical_user_id=MIA)
    # Noah only ever used the local console, which reaches nobody on its own.
    store.create_session(session_key="cli_noah", conversation_id="conv_noah_cli", manager_agent_id=None,
                         channel_name="cli", user_id=NOAH, chat_id="cli:test", owner_canonical_user_id=NOAH)


def _identity_service(store: GatewayStateStore) -> GatewayIdentityService:
    class _Secrets:
        def hash_secret(self, raw_secret: str) -> str:
            return f"hashed:{raw_secret}"

    return GatewayIdentityService(
        repository=store.identity_repository,
        unit_of_work_factory=store,
        authorizer=MembershipRoleIdentityAuthorizer(),
        secrets=_Secrets(),  # type: ignore[arg-type]
        clock=SimpleNamespace(now_utc=lambda: datetime(2026, 9, 26, tzinfo=timezone.utc)),
        settings=GatewayIdentitySettings(
            enabled=True,
            allow_newuser_registration=True,
            web_user_tokens_enabled=True,
            web_user_token_ttl_days=7,
            web_user_invite_ttl_seconds=900,
            link_code_ttl_seconds=600,
            default_timezone="UTC",
            team_id=TEAM,
            web_chat_url="https://agents.example.test/webchat",
        ),
        people_directory=SimpleNamespace(record=lambda _person: True),  # type: ignore[arg-type]
    )


def _memory_review_schedules(enabled: bool = True) -> GatewaySpecialistScheduleSettings:
    return GatewaySpecialistScheduleSettings(
        name="memory review",
        enabled=enabled,
        instruction_frame="Memory review tick for schedule_id={schedule_id}. {instruction}",
        definitions=(
            GatewaySystemScheduleDefinition(
                schedule_id=CORE_ID,
                schedule_name=f"{COREMEM_AUDIT_SCHEDULE_NAME_PREFIX}{INSTANCE}",
                interval_seconds=86_400,
                instruction="Review active coremem facts.",
                system_schedule_kind=SystemScheduleKind.MEMORY_REVIEW_COREMEM,
            ),
        ),
        person_definitions=(
            GatewayPersonSystemScheduleDefinition(
                schedule_id_prefix=MEMORY_REVIEW_USER_ID_PREFIX,
                interval_seconds=86_400,
                instruction="Review this person's usermem facts.",
                system_schedule_kind=SystemScheduleKind.MEMORY_REVIEW_USERMEM,
            ),
            GatewayPersonSystemScheduleDefinition(
                schedule_id_prefix=MEMORY_REVIEW_STALE_ID_PREFIX,
                interval_seconds=3_600,
                instruction="Review this person's stale conversations.",
                system_schedule_kind=SystemScheduleKind.MEMORY_REVIEW_STALE_CONVERSATIONS,
            ),
        ),
    )


def _gateway(
    world: SimpleNamespace,
    *,
    users_enabled: bool = True,
    memory_review: bool = True,
    more_schedules: tuple[GatewaySpecialistScheduleSettings, ...] = (),
) -> GatewayMaintenanceScheduleApplication:
    store: GatewayStateStore = world.store
    sessions = GatewaySessionService(
        repository=store.session_repository,
        id_generator=UuidGatewayIdGenerator(),
        authorizer=OwnershipPrivateResourceAuthorizer(),
        team_id=TEAM,
    )
    proactive = SimpleNamespace(
        resolve_session=lambda: sessions.resolve_session(
            requested_session_key=PROACTIVE_SESSION,
            default_session_key=PROACTIVE_SESSION,
            manager_agent_id="manager_1",
            owner_canonical_user_id=OLIVE,
            default_owner_canonical_user_id=OLIVE,
        )
    )
    return GatewayMaintenanceScheduleApplication(
        settings=GatewayMaintenanceScheduleSettings(
            instance_name=INSTANCE,
            response_route="gateway_responses",
            user_display_name="Olive",
            request_timezone="UTC",
            default_owner_canonical_user_id=OLIVE,
            heartbeat=GatewayHeartbeatScheduleSettings(enabled=False, source_id="hb", schedule_id="hb", interval_seconds=60),
            specialist_schedules=(_memory_review_schedules(memory_review), *more_schedules),
            internal_improvement=GatewayInternalImprovementScheduleSettings(
                schedule_id="int_imp_review_x", schedule_name="internal_improvement:x", interval_seconds=0
            ),
        ),
        proactive_sessions=proactive,  # type: ignore[arg-type]
        person_sessions=GatewayPersonMaintenanceSessions(
            settings=GatewayPersonMaintenanceSettings(team_id=TEAM, users_enabled=users_enabled, default_owner_canonical_user_id=OLIVE),
            identity=store.identity_repository,
            sessions=sessions,
            session_repository=store.session_repository,
            proactive_sessions=proactive,  # type: ignore[arg-type]
        ),
        request_context=GatewayRequestContextService(
            settings=GatewayRequestContextSettings(
                instance_name=INSTANCE,
                team_id=TEAM,
                default_canonical_user_id=OLIVE,
                default_display_name="Olive",
                default_timezone="UTC",
                heartbeat_source_id=None,
                users_enabled=users_enabled,
            ),
            identity=_identity_service(store),
            context_signer=context_signer(),
        ),
        delivery=GatewayDeliveryService(repository=store.delivery_repository, unit_of_work_factory=store),
        manager=world.route,  # type: ignore[arg-type]
        id_generator=UuidGatewayIdGenerator(),
    )


def _deliver(world: SimpleNamespace) -> None:
    """The manager handles what the gateway sent, in order."""

    for envelope in world.route.published:
        asyncio.run(handle_scheduled_message(world.manager, envelope.msg_content))
    world.route.published.clear()


def _stored(world: SimpleNamespace) -> dict[str, ScheduledMessage]:
    store = world.manager._schedule_store
    return {row["schedule_id"]: store.get_scheduled_message(row["schedule_id"]) for row in store.get_all()}


@pytest.fixture
def world(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    use_scheduler_key(monkeypatch)
    store = GatewayStateStore(str(tmp_path / "gateway_state.db"))
    _seed_people(store)
    yield SimpleNamespace(tmp_path=tmp_path, store=store, route=_ManagerRoute(), manager=_Manager(tmp_path / "schedules.db"))
    forget_task_context_keys()


def test_gateway_start_installs_a_pair_owned_by_each_active_person_only(world) -> None:
    asyncio.run(_gateway(world).ensure_all())
    _deliver(world)
    stored = _stored(world)

    expected = {CORE_ID} | {_usermem_id(p) for p in ACTIVE} | {_stale_id(p) for p in ACTIVE}
    assert set(stored) == expected  # nothing for Dora (disabled) or Remy (removed)
    verifier = ContextVerifier.from_env()
    for person in ACTIVE:
        for schedule_id in (_usermem_id(person), _stale_id(person)):
            schedule = stored[schedule_id]
            context = schedule.payload_meta.gateway_context
            assert context.canonical_user_id == person
            assert verifier.verify(context.task_context, team_id=TEAM).person_id == person
            assert schedule.management_class == ScheduleManagementClass.SYSTEM
            assert schedule.claim_named_agent_type == "MemReviewAgent"
            assert is_person_system_schedule(schedule)
            assert memory_review_schedule_kind(schedule.schedule_name) is not None
            assert person in schedule.schedule_name
    # The coremem audit stays one team schedule, owned by the first user.
    assert stored[CORE_ID].payload_meta.gateway_context.canonical_user_id == OLIVE
    assert not is_person_system_schedule(stored[CORE_ID])


def test_each_persons_pair_is_delivered_to_their_own_chat(world, caplog) -> None:
    with caplog.at_level(logging.INFO):
        asyncio.run(_gateway(world).ensure_all())
    _deliver(world)
    stored = _stored(world)

    def route(person: str) -> tuple[Any, ...]:
        context = stored[_usermem_id(person)].payload_meta.gateway_context
        return context.session_key, context.channel_name, context.chat_id, context.thread_id

    # Olive: her default chat. Mia: her most recent chat (Web Chat). Noah: none.
    assert route(OLIVE) == (person_maintenance_session_key(OLIVE), "telegram", "chat_olive", "topic_1")
    assert route(MIA) == (person_maintenance_session_key(MIA), "website", "site", "site")
    assert route(NOAH) == (person_maintenance_session_key(NOAH), None, None, None)
    assert UNREACHABLE_EVENT in caplog.text and "person_id=noah" in caplog.text
    for person in ACTIVE:
        target = world.store.get_scheduled_target(_stale_id(person))
        session = world.store.get_session(person_maintenance_session_key(person))
        assert (target.session_key, target.owner_canonical_user_id) == (session.session_key, person)
        # The dedicated session keeps review runs out of the person's own chats.
        assert session.owner_canonical_user_id == person
        assert target.conversation_id == session.conversation_id not in {"conv_mia_tg", "conv_mia_web", "conv_noah_cli"}
    assert world.store.get_scheduled_target(CORE_ID).session_key == PROACTIVE_SESSION


def test_a_person_without_a_chat_gets_one_at_the_next_install(world) -> None:
    gateway = _gateway(world)
    asyncio.run(gateway.ensure_person_schedules(NOAH))
    world.store.identity_repository.set_gateway_user_default_channel(
        canonical_user_id=NOAH, channel_name="signal", chat_id="chat_noah", thread_id=None
    )
    asyncio.run(gateway.ensure_person_schedules(NOAH))
    _deliver(world)

    context = _stored(world)[_usermem_id(NOAH)].payload_meta.gateway_context
    assert (context.channel_name, context.chat_id) == ("signal", "chat_noah")


def _pause(world: SimpleNamespace, schedule_id: str, person: str) -> None:
    store = world.manager._schedule_store
    stored = store.get_scheduled_message(schedule_id)
    paused = stored.model_copy(deep=True)
    paused.paused_until = "2030-01-01T00:00:00+00:00"
    permit = build_schedule_mutation_permit(
        schedule=stored,
        operation=ScheduleMutationOperation.PAUSE,
        authority_mode=ScheduleMutationAuthorityMode.AUTHENTICATED_GATEWAY_USER,
        actor_principal_id=person,
        owner_canonical_user_id=person,
        team_id=TEAM,
        candidate=paused,
    )
    assert store.set_paused_until(schedule_id, paused.paused_until, system_mutation_permit=permit)


def test_ensure_person_memory_review_is_safe_to_repeat_and_keeps_a_pause(world) -> None:
    gateway = _gateway(world)
    assert isinstance(gateway, GatewayPersonSchedules)
    asyncio.run(gateway.ensure_person_schedules(MIA))
    _deliver(world)
    assert set(_stored(world)) == {_usermem_id(MIA), _stale_id(MIA)}
    _pause(world, _usermem_id(MIA), MIA)
    before = world.manager._schedule_store.get_scheduled_message(_usermem_id(MIA))

    asyncio.run(gateway.ensure_person_schedules(MIA))
    asyncio.run(gateway.ensure_person_schedules(MIA))
    _deliver(world)

    after = world.manager._schedule_store.get_scheduled_message(_usermem_id(MIA))
    assert set(_stored(world)) == {_usermem_id(MIA), _stale_id(MIA)}
    assert (after.paused_until, after.next_fire_at, after.interval_ms) == (
        before.paused_until,
        before.next_fire_at,
        before.interval_ms,
    )


def test_ensure_person_memory_review_does_nothing_when_it_cannot(world, caplog) -> None:
    with caplog.at_level(logging.INFO):
        asyncio.run(_gateway(world).ensure_person_schedules(DORA))
        asyncio.run(_gateway(world).ensure_person_schedules(REMY))
        asyncio.run(_gateway(world, memory_review=False).ensure_person_schedules(MIA))
        world.route.manager_id = None
        asyncio.run(_gateway(world).ensure_person_schedules(MIA))

    assert world.route.published == []
    assert caplog.text.count("event=gateway_person_memory_review_not_active") == 2
    assert "event=gateway_memory_review_schedule_disabled" in caplog.text
    assert "event=gateway_person_memory_review_manager_unavailable" in caplog.text


def test_with_gateway_users_off_the_configured_user_gets_the_pair_on_the_proactive_session(world) -> None:
    asyncio.run(_gateway(world, users_enabled=False).ensure_all())
    _deliver(world)
    stored = _stored(world)

    assert set(stored) == {CORE_ID, _usermem_id(OLIVE), _stale_id(OLIVE)}
    assert stored[_usermem_id(OLIVE)].payload_meta.gateway_context.session_key == PROACTIVE_SESSION


def _seed_stale_conversation(conversations: ConversationStore, person: str) -> None:
    conversation_id = f"conv_{person}"
    conversations.ensure_thread(
        conversation_id=conversation_id,
        team_id=TEAM,
        owner_agent_id="agent_team",
        owner_agent_name="TeamArchitect",
        requester_name=person,
        requester_id=person,
        root_task_id=f"{person}_task_1",
        subject_preview=f"{person}'s chat",
        owner_canonical_user_id=person,
    )
    for index in (1, 2):
        task_id = f"{person}_task_{index}"
        conversations.append_turn(
            conversation_id=conversation_id,
            task_id=task_id,
            followup_to_task_id=None,
            owner_agent_id="agent_team",
            requester_name=person,
            requester_id=person,
            requester_type=RequesterType.USER_REQUESTER.value,
            correlation_id=f"corr_{task_id}",
            problem_text=f"{person} question {index}",
            gateway_context=GatewayContext(
                session_key=f"session_{person}", channel_name="telegram", canonical_user_id=person, chat_id=f"chat_{person}"
            ).model_dump(mode="json"),
            task_status="accepted",
        )
        conversations.update_turn_completion(
            task_id=task_id, task_status="completed", final_answer_text="ok", turn_summary_text=f"{person} turn {index}"
        )
    with conversations._repository._connect() as conn:
        conn.execute(
            "UPDATE conversation_threads SET updated_at = ? WHERE conversation_id = ?",
            ("2026-01-01T00:00:00+00:00", conversation_id),
        )
        conn.commit()


def test_each_run_prepares_jobs_only_for_its_own_person(world) -> None:
    asyncio.run(_gateway(world).ensure_all())
    _deliver(world)
    stored = _stored(world)
    db_path = str(world.tmp_path / "memory.db")
    conversations = ConversationStore(db_path=db_path, logger=logging.getLogger("test"), artifacts_dir=Path(db_path).parent / "conversation_artifacts")
    reviews = MemoryReviewStore(db_path=db_path, logger=logging.getLogger("test"))
    pinned = SqlitePinnedMemoryRepository(db_path, logger=logging.getLogger("test"))
    for person in (OLIVE, MIA):
        pinned.create_usermem(canonical_user_id=person, fact_text=f"{person} private fact", capacity=10)
        _seed_stale_conversation(conversations, person)
    reviewer = SimpleNamespace(
        id="memreview_1",
        team_id=TEAM,
        named_agent_type="MemReviewAgent",
        memory_config={"review": {"enabled": True, "stale_conversation_after_seconds": 60}},
        _memory_review_store=reviews,
        _conversation_store=conversations,
        _pinned_memory_repository=pinned,
        _logger=logging.getLogger("test"),
    )

    for person in (OLIVE, MIA):
        for schedule_id in (_usermem_id(person), _stale_id(person)):
            prepare_memory_review_schedule(reviewer, schedule_id, stored[schedule_id])

    jobs = reviews.list_review_jobs()
    assert {(job.source_payload["review_kind"], job.source_payload["canonical_user_id"]) for job in jobs} == {
        ("usermem_audit", OLIVE),
        ("usermem_audit", MIA),
        ("conversation_window", OLIVE),
        ("conversation_window", MIA),
    }
    for job in jobs:
        person = job.source_payload["canonical_user_id"]
        other = MIA if person == OLIVE else OLIVE
        assert other not in str(job.source_payload.get("facts") or "")
        assert job.conversation_id != f"conv_{other}"


def test_older_team_wide_and_unclassified_schedules_are_recognized_as_per_person() -> None:
    def schedule(name: str, kind: SystemScheduleKind | None, management: ScheduleManagementClass) -> ScheduledMessage:
        return ScheduledMessage(
            schedule_name=name,
            management_class=management,
            system_schedule_kind=kind,
            payload=Task(task_type=MessageMetaType.REQUEST, problem_statement="tick"),
            interval_ms=1000,
        )

    system = ScheduleManagementClass.SYSTEM
    # The team-wide pair installed before per-person reviews (owned by the first user).
    assert is_person_system_schedule(schedule("memory_review:usermem:test", SystemScheduleKind.MEMORY_REVIEW_USERMEM, system))
    assert is_person_system_schedule(
        schedule("memory_review:stale_conversations:test", SystemScheduleKind.MEMORY_REVIEW_STALE_CONVERSATIONS, system)
    )
    assert is_person_system_schedule(schedule("memory_review:usermem:test", SystemScheduleKind.UNCLASSIFIED, system))
    assert not is_person_system_schedule(schedule("memory_review:coremem:test", SystemScheduleKind.MEMORY_REVIEW_COREMEM, system))
    assert not is_person_system_schedule(schedule("memory_review:usermem:test", None, ScheduleManagementClass.USER))


def test_schedule_ids_are_stable_and_never_shared_between_people() -> None:
    assert _usermem_id(OLIVE) == "mem_rev_user_test_olive" == _usermem_id(OLIVE)
    assert _usermem_id(OLIVE) != f"{MEMORY_REVIEW_USER_ID_PREFIX}_{INSTANCE}"  # the old team-wide id
    spaced, underscored = _usermem_id("Ann Lee"), _usermem_id("Ann_Lee")
    assert " " not in spaced and spaced != underscored


def _curation_schedules(enabled: bool = True) -> GatewaySpecialistScheduleSettings:
    """A second set with one per-person schedule of a new kind (tests/system_schedule_kind_support.py)."""

    return GatewaySpecialistScheduleSettings(
        name="knowledge review",
        enabled=enabled,
        instruction_frame="Knowledge review tick for schedule_id={schedule_id}. {instruction}",
        definitions=(),
        person_definitions=(
            GatewayPersonSystemScheduleDefinition(
                schedule_id_prefix=CURATION_ID_PREFIX,
                interval_seconds=86_400,
                instruction="Curate this person's notes.",
                system_schedule_kind=STAND_IN_KIND,
            ),
        ),
    )


def test_a_new_per_person_kind_is_installed_for_each_active_person_like_memory_review(world, monkeypatch) -> None:
    """A new per-person row in the system schedule table and its gateway
    settings are all it takes: each active person gets their own schedule,
    signed for them and claimed by the kind's agent type."""

    use_kind(monkeypatch, PERSON_KIND)
    asyncio.run(_gateway(world, more_schedules=(_curation_schedules(),)).ensure_all())
    _deliver(world)
    stored = _stored(world)

    curation = {person_system_schedule_id(CURATION_ID_PREFIX, INSTANCE, person) for person in ACTIVE}
    assert set(stored) == {CORE_ID} | {_usermem_id(p) for p in ACTIVE} | {_stale_id(p) for p in ACTIVE} | curation
    verifier = ContextVerifier.from_env()
    for person in ACTIVE:
        schedule = stored[person_system_schedule_id(CURATION_ID_PREFIX, INSTANCE, person)]
        context = schedule.payload_meta.gateway_context
        assert schedule.schedule_name == f"{PERSON_NAME_PREFIX}{INSTANCE}:{person}"
        assert (schedule.management_class, schedule.system_schedule_kind) == (ScheduleManagementClass.SYSTEM, STAND_IN_KIND)
        assert schedule.claim_named_agent_type == schedule.payload_meta.to_named_agent_type == CURATOR_AGENT_TYPE
        assert (context.canonical_user_id, context.delivery_mode) == (person, CURATOR_DELIVERY_MODE)
        assert verifier.verify(context.task_context, team_id=TEAM).person_id == person
        assert context.session_key == person_maintenance_session_key(person)
        assert is_person_system_schedule(schedule)


def test_a_person_added_later_gets_every_per_person_kind_and_a_set_that_is_off_none(world, monkeypatch, caplog) -> None:
    use_kind(monkeypatch, PERSON_KIND)
    gateway = _gateway(world, more_schedules=(_curation_schedules(),))
    assert isinstance(gateway, GatewayPersonSchedules)

    asyncio.run(gateway.ensure_person_schedules(MIA))
    _deliver(world)
    assert set(_stored(world)) == {
        _usermem_id(MIA),
        _stale_id(MIA),
        person_system_schedule_id(CURATION_ID_PREFIX, INSTANCE, MIA),
    }

    with caplog.at_level(logging.INFO):
        asyncio.run(_gateway(world, more_schedules=(_curation_schedules(enabled=False),)).ensure_person_schedules(NOAH))
    _deliver(world)
    assert person_system_schedule_id(CURATION_ID_PREFIX, INSTANCE, NOAH) not in _stored(world)
    assert _usermem_id(NOAH) in _stored(world)
    assert "event=gateway_knowledge_review_schedule_disabled" in caplog.text


def test_a_set_refuses_a_kind_the_table_does_not_give_a_local_specialist_or_of_the_wrong_scope() -> None:
    with pytest.raises(ValueError, match="not a system schedule a local specialist runs"):
        GatewaySpecialistScheduleSettings(
            name="maintenance",
            enabled=True,
            instruction_frame="{instruction}",
            definitions=(
                GatewaySystemScheduleDefinition(
                    schedule_id="x", schedule_name="x", interval_seconds=60, instruction="x",
                    system_schedule_kind=SystemScheduleKind.INTERNAL_IMPROVEMENT,
                ),
            ),
        )
    with pytest.raises(ValueError, match="is a team schedule"):
        GatewaySpecialistScheduleSettings(
            name="memory review",
            enabled=True,
            instruction_frame="{instruction}",
            definitions=(),
            person_definitions=(
                GatewayPersonSystemScheduleDefinition(
                    schedule_id_prefix="x", interval_seconds=60, instruction="x",
                    system_schedule_kind=SystemScheduleKind.MEMORY_REVIEW_COREMEM,
                ),
            ),
        )
