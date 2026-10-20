"""Which chat session is a chat's current one after an import, a late reply and live chat traffic.

A chat's current session (``gateway_channel_sessions``) is where the person's
next message goes. Only the person's own chat traffic changes it: starting a
new session, ``/switch_session``, or a message that names a session. A team
import restores the session that was current when the team was exported, and
a reply that arrives after the person moved to another session leaves the chat
where the person is.

Every scenario runs through the real services on SQLite and on PostgreSQL
(``LIVE_TEST_POSTGRES_DSN``, a server where the test may create databases):
the export and import round trip with the real sections, and the next message
through the real gateway and channel bridge, as Telegram sends it.
"""

from __future__ import annotations

import asyncio
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterator
from uuid import uuid4

import pytest
import yaml

from assistant_gateway.channel_bridge import ChannelBridge
from assistant_gateway.config import GatewayInstanceConfig
from assistant_gateway.gateway import UserGateway
from assistant_gateway.store import GatewayStateStore
from assistant_gateway.translation import ChannelInboundMessage
from custom_agents.schedule_store import ScheduleStore
from custom_messages.contracts.envelope_v1 import MessageEnvelope
from custom_messages.contracts.routing_v1 import GatewayContext, MessageHeader, MessageMeta
from custom_messages.contracts.schedule_v1 import ScheduledMessage
from custom_messages.contracts.task_v1 import Task, TaskAnswer
from custom_messages.enums import MessageMetaType, ScheduleManagementClass
from storage.config import StorageSettings, resolve_storage_settings
from storage.gateway_state_repository import create_gateway_state_repository
from team_transfer.container import TransferReader
from team_transfer.crypto import new_key_derivation
from team_transfer.people import PeopleSection
from team_transfer.schedules import SchedulesSection
from team_transfer.sections import RecordOutcome
from team_transfer.service import export_source, export_team, import_team
from team_transfer.settings import SettingsSection
from tests.pinned_memory_support import new_instance

TEAM = "team_a"
PERSON = "usr_ana"
TELEGRAM = {"channel_name": "telegram", "user_id": "tg-42", "chat_id": "42"}
# Session keys are random, so an older session's key sorts after a newer one's
# about half the time. The import writes sessions in key order.
OLDER = "session_f9e8d7c6b5"
NEWER = "session_0a1b2c3d4e"
PASSPHRASE = "correct horse battery staple"
FAST_KEYS = {"opslimit": 1, "memlimit": 8192}
SECTIONS = (SettingsSection(), PeopleSection(), SchedulesSection())
WAIT_SECONDS = 10


@dataclass
class Backend:
    """SQLite, or a PostgreSQL server where each test creates its own empty databases."""

    name: str
    admin_dsn: str = ""
    created: list[str] = field(default_factory=list)

    @property
    def postgres(self) -> bool:
        return self.name == "postgres"

    def new_database(self) -> str:
        import psycopg
        from psycopg import sql
        from psycopg.conninfo import make_conninfo

        name = "chat_sessions_" + uuid4().hex
        # CREATE DATABASE cannot run inside a transaction.
        with psycopg.connect(self.admin_dsn, autocommit=True) as conn:
            conn.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(name)))
        self.created.append(name)
        return make_conninfo(self.admin_dsn, dbname=name)

    def gateway_storage(self, tmp_path: Path) -> StorageSettings:
        """A new, empty gateway state store."""

        if not self.postgres:
            return resolve_storage_settings({"backend": "sqlite", "sqlite": {"path": str(tmp_path / "gateway_state.db")}})
        return resolve_storage_settings({"backend": "postgres", "postgres": {"dsn": self.new_database()}})

    def drop_databases(self) -> None:
        import psycopg
        from psycopg import sql

        with psycopg.connect(self.admin_dsn, autocommit=True) as conn:
            for name in self.created:
                conn.execute(sql.SQL("DROP DATABASE IF EXISTS {} WITH (FORCE)").format(sql.Identifier(name)))


@pytest.fixture(params=["sqlite", pytest.param("postgres", marks=[pytest.mark.live, pytest.mark.postgres])])
def backend(request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch) -> Iterator[Backend]:
    # Neither database may come from the developer's or the bundle's own settings.
    for name in ("POSTGRES_DSN", "POSTGRES_DSN_FILE", "TEAM_BUNDLED_POSTGRES_ADDRESS"):
        monkeypatch.delenv(name, raising=False)
    if request.param == "sqlite":
        yield Backend("sqlite")
        return
    dsn = os.environ.get("LIVE_TEST_POSTGRES_DSN")
    if not dsn:
        pytest.skip("Set LIVE_TEST_POSTGRES_DSN to a PostgreSQL server where the test may create databases.")
    found = Backend("postgres", admin_dsn=dsn)
    try:
        yield found
    finally:
        found.drop_databases()


# -- the people, chats and sessions ----------------------------------------------------


def _person(repository: Any) -> None:
    """Ana, the team's owner, whose Telegram account is linked."""

    repository.create_gateway_user(canonical_user_id=PERSON, display_name="Ana", timezone="UTC")
    repository.create_team_membership(canonical_user_id=PERSON, team_id=TEAM, role="owner")
    repository.create_channel_binding(
        canonical_user_id=PERSON, channel_name=TELEGRAM["channel_name"], external_user_id=TELEGRAM["user_id"]
    )


def _two_sessions_of_one_chat(repository: Any) -> None:
    """The older session, then ``/new`` in the same chat: the newer one is current."""

    for key in (OLDER, NEWER):
        repository.create_session(
            session_key=key, conversation_id=f"conv_{key}", manager_agent_id="mgr_old",
            owner_canonical_user_id=PERSON, **TELEGRAM,
        )
    assert _current(repository) == NEWER


def _schedule_in_the_older_session(config: Any) -> None:
    """A daily report set up before ``/new`` keeps delivering through the older session."""

    context = GatewayContext(
        gateway_instance_name="alpha", canonical_user_id=PERSON, session_key=OLDER,
        delivery_mode="proactive", **TELEGRAM,
    )
    ScheduleStore(storage_settings=config.manager_storage_settings, default_cron_timezone="UTC").add(
        ScheduledMessage(
            schedule_id="sched_daily_report", schedule_name="Daily report",
            management_class=ScheduleManagementClass.USER,
            payload=Task(task_type=MessageMetaType.REQUEST, problem_statement="Send the daily report"),
            payload_meta=MessageMeta(gateway_context=context),
            claim_team_id=TEAM, claim_named_agent_type="TeamArchitect",
            interval_ms=86_400_000, repeat_count=None, next_fire_at="2020-01-01T00:00:00+00:00",
        )
    )


def _current(repository: Any) -> str | None:
    found = repository.get_active_session_for_channel(**TELEGRAM)
    return found.session_key if found is not None else None


# -- the old and the new instance ------------------------------------------------------


def _instance(tmp_path: Path, backend: Backend, monkeypatch: pytest.MonkeyPatch) -> tuple[Path, Any]:
    """A saved old instance; on PostgreSQL its stores are in their own database (``POSTGRES_DSN``)."""

    path, config = new_instance(tmp_path, "alpha", TEAM)
    if not backend.postgres:
        return path, config
    saved = config.to_yaml_dict()
    for section in ("state", "gateway"):
        saved[section]["storage"]["backend"] = "postgres"
    path.write_text(yaml.safe_dump(saved), encoding="utf-8")
    monkeypatch.setenv("POSTGRES_DSN", backend.new_database())
    return path, GatewayInstanceConfig.load(path)


def _export(tmp_path: Path, backend: Backend, monkeypatch: pytest.MonkeyPatch, config_path: Path) -> Path:
    """Export the team; the import that follows writes a fresh root (a new, empty database on PostgreSQL)."""

    archive = tmp_path / "team.uatransfer"
    export_team(
        export_source(config_path), archive, PASSPHRASE, owner_person_id=PERSON, sections=SECTIONS,
        key_derivation=new_key_derivation(**FAST_KEYS),
    )
    if backend.postgres:
        monkeypatch.setenv("POSTGRES_DSN", backend.new_database())
    return archive


def _move(tmp_path: Path, backend: Backend, monkeypatch: pytest.MonkeyPatch, config_path: Path) -> tuple[Any, Any]:
    """Export the team and import all of it into a fresh root."""

    archive = _export(tmp_path, backend, monkeypatch, config_path)
    with TransferReader.open(archive, PASSPHRASE) as reader:
        plan = import_team(reader, tmp_path / "new", dry_run=False, sections=SECTIONS)
    return plan, GatewayInstanceConfig.load(tmp_path / "new" / "instance_config.yaml")


# -- the gateway, as a chat app uses it ------------------------------------------------


def _reply(request: MessageEnvelope, task_id: str) -> MessageEnvelope:
    return MessageEnvelope(
        msg_content=Task(task_id=task_id, task_type=MessageMetaType.RESPONSE, answer=TaskAnswer(answer="Done.")),
        msg_meta=MessageMeta(
            msg_correlation_id=request.msg_meta.msg_correlation_id,
            conversation_id=request.msg_meta.conversation_id,
        ),
        msg_header=MessageHeader(from_agent_id="manager_1234", to_routing_path="user_queue", agent_signature=""),
    )


class _Manager:
    """Stands in for the manager: records each published request; answers at once unless held."""

    def __init__(self, gateway: UserGateway) -> None:
        self.gateway = gateway
        self.published: list[MessageEnvelope] = []
        self.hold = False
        gateway._manager_client.publish_durable = self.publish  # type: ignore[method-assign]

    async def publish(self, envelope: MessageEnvelope, routing_key: str) -> None:
        self.published.append(envelope)
        if not self.hold:
            await self.answer(envelope)

    async def answer(self, envelope: MessageEnvelope) -> None:
        correlation_id = envelope.msg_meta.msg_correlation_id
        await self.gateway._manager_client.get_response_channel(correlation_id).put(
            _reply(envelope, f"task_{len(self.published)}")
        )

    async def wait_for(self, count: int) -> MessageEnvelope:
        async with asyncio.timeout(WAIT_SECONDS):
            while len(self.published) < count:
                await asyncio.sleep(0.01)
        return self.published[count - 1]


def _message(text: str) -> ChannelInboundMessage:
    return ChannelInboundMessage(text=text, **TELEGRAM)


def _session_of(envelope: MessageEnvelope) -> str:
    return envelope.msg_meta.gateway_context.session_key


# -- import -----------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_import_keeps_the_exported_current_session_and_the_next_message_goes_there(
    tmp_path: Path, backend: Backend, monkeypatch: pytest.MonkeyPatch
) -> None:
    config_path, old = _instance(tmp_path, backend, monkeypatch)
    old_repository = create_gateway_state_repository(old.state_storage_settings)
    _person(old_repository)
    _two_sessions_of_one_chat(old_repository)
    _schedule_in_the_older_session(old)

    plan, new = _move(tmp_path, backend, monkeypatch, config_path)

    outcomes = {record.key: record.outcome for record in plan.records if record.section == "schedules"}
    assert outcomes[f"session/{OLDER}"] is RecordOutcome.IMPORT, "the schedule's session travels"
    assert outcomes[f"session/{NEWER}"] is RecordOutcome.IMPORT, "the chat's current session travels"
    repository = create_gateway_state_repository(new.state_storage_settings)
    assert {repository.get_session(OLDER).chat_id, repository.get_session(NEWER).chat_id} == {"42"}
    assert _current(repository) == NEWER, "the import made the older session current"

    new.manager.manager_agent_id = "manager_1234"  # as the new instance's started manager records it
    gateway = UserGateway(
        new, output_sink=None,
        state_store=GatewayStateStore(new.state_storage_settings.sqlite_path or "", storage_settings=new.state_storage_settings),
    )
    manager = _Manager(gateway)
    await ChannelBridge(gateway.application).handle_inbound(_message("Is the garden watered?"))
    assert _session_of(await manager.wait_for(1)) == NEWER


def test_a_later_batch_leaves_a_chat_that_already_has_a_current_session_where_it_is(
    tmp_path: Path, backend: Backend, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    config_path, old = _instance(tmp_path, backend, monkeypatch)
    old_repository = create_gateway_state_repository(old.state_storage_settings)
    _person(old_repository)
    _two_sessions_of_one_chat(old_repository)
    _schedule_in_the_older_session(old)
    archive = _export(tmp_path, backend, monkeypatch, config_path)
    target = tmp_path / "new"
    with TransferReader.open(archive, PASSPHRASE) as reader:
        # The first batch brings the daily report and its session, and leaves Ana's current session behind.
        import_team(
            reader, target, dry_run=False, sections=SECTIONS, require_valid=True,
            choices={"people": {PERSON}, "schedules": {f"session/{OLDER}", "schedule/sched_daily_report"}},
        )
        new = GatewayInstanceConfig.load(target / "instance_config.yaml")
        repository = create_gateway_state_repository(new.state_storage_settings)
        assert _current(repository) is None, "the older session was not its chat's current one"
        # Ana uses the new instance: her next message starts a session there.
        repository.create_session(
            session_key="session_live", conversation_id="conv_live", manager_agent_id="mgr_new",
            owner_canonical_user_id=PERSON, **TELEGRAM,
        )
        plan = import_team(
            reader, target, dry_run=False, sections=SECTIONS,
            choices={"schedules": {f"session/{NEWER}"}}, allow_existing=True, require_valid=True,
        )

    record = next(record for record in plan.records if record.key == f"session/{NEWER}")
    assert record.outcome is RecordOutcome.ATTENTION
    assert "already continues in session_live here, which stays current" in record.reason
    assert repository.get_session(NEWER).chat_id == "42"
    assert _current(repository) == "session_live"
    kept = [record for record in caplog.records if "event=team_transfer_session_current_kept" in record.getMessage()]
    assert len(kept) == 1 and f"session_key={NEWER} current_session_key=session_live" in kept[0].getMessage()


# -- a reply that arrives after the person moved on ------------------------------------


def _gateway(tmp_path: Path, backend: Backend) -> tuple[UserGateway, Any]:
    """A gateway for Ana's team over a new, empty state store, and that store's repository."""

    config = GatewayInstanceConfig.create(
        instance_name="alpha", instance_root=str(tmp_path / "alpha"), team_id=TEAM, timezone="UTC",
        manager_agent_id="manager_1234",
    )
    config.save()
    settings = backend.gateway_storage(tmp_path)
    repository = create_gateway_state_repository(settings)
    _person(repository)
    store = GatewayStateStore(settings.sqlite_path or "", storage_settings=settings)
    return UserGateway(config, output_sink=None, state_store=store), repository


async def _command(gateway: UserGateway, text: str) -> str:
    """A command typed in the chat, as the Telegram app hands it to the gateway."""

    result = await gateway.application.execute_interactive_command(raw_text=text, enable_reply_command=False, **TELEGRAM)
    assert result.handled, text
    return result.text or ""


@pytest.mark.asyncio
async def test_a_reply_that_arrives_after_new_leaves_the_chat_in_the_new_session(
    tmp_path: Path, backend: Backend
) -> None:
    gateway, repository = _gateway(tmp_path, backend)
    manager = _Manager(gateway)
    bridge = ChannelBridge(gateway.application)
    manager.hold = True
    first = asyncio.create_task(bridge.handle_inbound(_message("Compare these three laptops.")))
    try:
        request = await manager.wait_for(1)
        earlier = _session_of(request)
        # While the comparison runs, Ana starts a new session in the same chat.
        await _command(gateway, "/new")
        newer = _current(repository)
        assert newer not in (None, earlier)

        await manager.answer(request)
        async with asyncio.timeout(WAIT_SECONDS):
            await first
    finally:
        first.cancel()
    assert repository.get_session(earlier).last_task_id == "task_1", "the reply is recorded with its own session"
    assert _current(repository) == newer, "the late reply moved the chat back to the earlier session"

    manager.hold = False
    await bridge.handle_inbound(_message("Now a new topic."))
    assert _session_of(await manager.wait_for(2)) == newer


# -- live chat traffic still chooses the current session -------------------------------


@pytest.mark.asyncio
async def test_new_and_switch_session_still_choose_where_the_next_message_goes(
    tmp_path: Path, backend: Backend
) -> None:
    gateway, repository = _gateway(tmp_path, backend)
    manager = _Manager(gateway)
    bridge = ChannelBridge(gateway.application)

    await bridge.handle_inbound(_message("First question."))
    first = _session_of(await manager.wait_for(1))
    assert _current(repository) == first, "a chat's first message starts its current session"

    await _command(gateway, "/new")
    second = _current(repository)
    assert second not in (None, first)
    await bridge.handle_inbound(_message("Second question."))
    assert _session_of(await manager.wait_for(2)) == second

    assert "Switched to session" in await _command(gateway, f"/switch_session {first}")
    assert _current(repository) == first
    await bridge.handle_inbound(_message("Back to the first question."))
    assert _session_of(await manager.wait_for(3)) == first


def test_only_a_chat_write_makes_a_session_current(tmp_path: Path, backend: Backend) -> None:
    """``upsert_session`` is the person's chat traffic; ``upsert_session_without_switching_chat`` is everything else."""

    repository = create_gateway_state_repository(backend.gateway_storage(tmp_path))
    _person(repository)
    _two_sessions_of_one_chat(repository)

    # An import or a late reply writes the older session with its full chat route.
    repository.upsert_session_without_switching_chat(
        session_key=OLDER, session_alias="garden", conversation_id=None, last_task_id="task_late",
        manager_agent_id="mgr_new", status="active", owner_canonical_user_id=PERSON, **TELEGRAM,
    )
    older = repository.get_session(OLDER)
    assert (older.session_alias, older.last_task_id, older.manager_agent_id, older.chat_id) == (
        "garden", "task_late", "mgr_new", "42",
    )
    assert older.conversation_id == f"conv_{OLDER}", "an unset field keeps its saved value"
    assert repository.get_session_by_conversation_id(f"conv_{OLDER}").last_task_id == "task_late"
    assert _current(repository) == NEWER

    # A session the store has not seen yet, on a chat with no current session.
    repository.upsert_session_without_switching_chat(
        session_key="session_imported", conversation_id=None, last_task_id=None, manager_agent_id=None,
        owner_canonical_user_id=PERSON, channel_name="telegram", user_id="tg-42", chat_id="other",
    )
    assert repository.get_session("session_imported").status == "active"
    assert repository.get_active_session_for_channel(channel_name="telegram", user_id="tg-42", chat_id="other") is None

    # The person's own chat traffic still moves the chat, as before.
    repository.upsert_session(
        session_key=OLDER, conversation_id=None, last_task_id=None, manager_agent_id=None, **TELEGRAM,
    )
    assert _current(repository) == OLDER
