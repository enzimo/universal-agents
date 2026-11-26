"""People's schedule commands at the team services (team_services.schedules), and through the gateway.

Real SQLite stores (and PostgreSQL with ``LIVE_TEST_POSTGRES_DSN``); frames are stand-ins with the contract's retry keys
(tests/team_services_support.py), and every body and reply is checked against the
contract's models. tests/test_live_team_memory.py covers signing and verifying over NATS.

The person comes only from the command token's reference and sees and changes only the
schedules they own; a chat's list names the chat by its delivery reference; a change names
the version the person listed and is checked with the store's permit in its write
transaction; a system schedule changes only for people the access rule allows; a retried
change gets its stored reply and is carried out once.
"""

from __future__ import annotations

import logging
import re
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Callable, Iterator, Optional
from uuid import uuid4

import pytest

from access_control.people_directory import PeopleDirectory
from assistant_gateway.application.commands.context import GatewayActorContext
from assistant_gateway.application.commands.manager_control import ManagerControlCommandRequest
from assistant_gateway.application.manager_control_command_service import GatewayManagerControlCommandService
from assistant_gateway.application.request_context import GatewayCommandPerson
from assistant_gateway.application.schedule_management import GatewayScheduleApplication, GatewayScheduleSettings
from assistant_gateway.application.team_commands import GatewayTeamCommandService
from assistant_gateway.domain.sessions import SessionRecord
from assistant_gateway.team_services_client import AccessStorePersonReferences
from custom_agents.person_schedule_controls import apply_person_schedule_change
from custom_agents.schedule_store import ScheduleStore, schedule_record_digest
from custom_messages.contracts.access_v2 import PersonStanding, TeamPersonV1
from custom_messages.contracts.routing_v1 import GatewayContext, MessageMeta
from custom_messages.contracts.schedule_v1 import ScheduledMessage
from custom_messages.contracts.task_v1 import Task
from custom_messages.enums import MessageMetaType, ScheduleManagementClass, SystemScheduleKind
from custom_transport.protocol1.delivery_refs import DeliveryRefs, ensure_delivery_ref_key
from runtime_settings.instance_layout import InstanceLayout
from team_services.people import CommandPerson
from team_services.schedules import ScheduleOperations
from tests.people_directory_support import access_store_at
from tests.team_services_support import checked_reply, stand_in_frame
from tests.test_message_contract import body_schema_file, validator
from tests.test_pinned_memory_stale_edits import BACKENDS
from tests.test_pinned_memory_stale_edits import _storage as backend_storage

TEAM = "team_schedules"
PEOPLE = {"ada": ("member",), "bo": ("member",), "olu": ("admin",)}
LATER = "2030-01-01T00:00:00.000Z"


def reference_of(person: str) -> str:
    return f"p-{person:_<22}"[:24]


class _Ids:
    def new_correlation_id(self) -> str:
        return f"cmd-{uuid4().hex}"


class _InProcess:
    """The gateway's team services port, answered by the schedule handlers in process."""

    def __init__(self, operations: ScheduleOperations) -> None:
        self.operations = operations

    async def command(self, kind: str, body: dict, *, person_ref: str, request_id: str) -> dict:
        return checked_reply(kind, self.operations.answer(
            stand_in_frame(TEAM, kind, body, person_ref=person_ref, request_id=request_id)))


class Team:
    def __init__(self, tmp_path: Path, storage: Any) -> None:
        self.access = access_store_at(tmp_path)
        now = datetime.now(timezone.utc)
        for person, roles in PEOPLE.items():
            self.access.record_person(TeamPersonV1(team_id=TEAM, person_id=person, person_ref=reference_of(person),
                                                   standing=PersonStanding.ACTIVE, roles=roles, changed_at_utc=now))
        self.store = ScheduleStore(storage_settings=storage)
        self.refs = DeliveryRefs(TEAM, b"k" * 32)
        # Administrators may change system schedules (the real rule: test_gateway_system_schedule_controls).
        self.may_change_system: Callable[[CommandPerson], bool] = lambda person: "admin" in person.roles
        self.operations = ScheduleOperations(self.store, PeopleDirectory(self.access, TEAM), self.refs,
                                             lambda person: self.may_change_system(person), lambda: datetime.now(timezone.utc),
                                             logging.getLogger("test.team_schedules"))

    def add(self, schedule_id: str, owner: str, *, session_key: str = "session_ada_a", system: bool = False) -> ScheduledMessage:
        self.store.add(ScheduledMessage(
            schedule_id=schedule_id,
            schedule_name=f"heartbeat:{schedule_id}" if system else f"name of {schedule_id}",
            management_class=ScheduleManagementClass.SYSTEM if system else ScheduleManagementClass.USER,
            system_schedule_kind=SystemScheduleKind.HEARTBEAT_LAUNCHER if system else None,
            claim_team_id=TEAM,
            payload=Task(task_type=MessageMetaType.REQUEST, problem_statement=f"Check   the\nfeed for {schedule_id}"),
            payload_meta=MessageMeta(gateway_context=GatewayContext(canonical_user_id=owner, session_key=session_key)),
            interval_ms=60_000,
        ))
        schedule = self.store.get_scheduled_message(schedule_id)
        assert schedule is not None
        return schedule

    def version(self, schedule_id: str) -> str:
        schedule = self.store.get_scheduled_message(schedule_id)
        assert schedule is not None
        return schedule_record_digest(schedule)

    def ask(self, kind: str, body: dict, *, person: str = "ada", request_id: str = "cmd-1") -> dict:
        frame = stand_in_frame(TEAM, kind, {"request_id": request_id, **body}, person_ref=reference_of(person),
                               request_id=request_id)
        return checked_reply(kind, self.operations.answer(frame))

    def listed(self, *, person: str = "ada", request_id: str = "cmd-list", **body: Any) -> dict:
        return self.ask("schedules.list", {"offset": 0, "limit": 50, **body}, person=person, request_id=request_id)


@pytest.fixture(params=BACKENDS)
def team(request, tmp_path: Path) -> Iterator[Team]:
    """On SQLite, and on PostgreSQL with ``LIVE_TEST_POSTGRES_DSN`` (its own schema per test)."""

    with backend_storage(request.param, tmp_path) as storage:
        yield Team(tmp_path, storage)


def test_a_person_lists_their_own_schedules_and_a_chat_only_its_own(team: Team) -> None:
    team.add("sched_a", "ada", session_key="session_ada_a")
    team.add("sched_b", "ada", session_key="session_ada_b")
    team.add("sched_bo", "bo", session_key="session_bo")

    everything = team.listed()
    chat_a = team.listed(delivery_ref=team.refs.ref("session_ada_a"), request_id="cmd-chat")
    bos_chat = team.listed(delivery_ref=team.refs.ref("session_bo"), request_id="cmd-other")
    first_page = team.listed(limit=1, request_id="cmd-page")

    assert [row["schedule_id"] for row in everything["schedules"]] == ["sched_a", "sched_b"]
    assert everything["total"] == 2 and "next_offset" not in everything
    row = everything["schedules"][0]
    assert row["timing"] == {"kind": "interval", "interval_ms": 60_000}
    assert (row["name"], row["instruction_preview"]) == ("name of sched_a", "Check the feed for sched_a")
    assert row["seen_version"] == team.version("sched_a")
    assert [row["schedule_id"] for row in chat_a["schedules"]] == ["sched_a"]
    # Another person's chat lists nothing of theirs for Ada.
    assert bos_chat == {"outcome": "ok", "schedules": [], "total": 0}
    assert (len(first_page["schedules"]), first_page["next_offset"]) == (1, 1)


def test_listed_rows_name_their_action_and_every_reply_meets_the_contracts_schema(team: Team) -> None:
    """Each schedules.list row says what a run does (``action``, required by the contract); every stored
    schedule runs a task. The list and change replies pass the contract's JSON Schemas as well as its models."""

    team.add("sched_a", "ada")
    team.add("hb_sched_team", "ada", system=True)

    listed = team.listed()
    paused = team.ask("schedules.pause", {"schedule_id": "sched_a", "seen_version": team.version("sched_a"),
                                          "paused_until": LATER}, request_id="cmd-2")
    stale = team.ask("schedules.resume", {"schedule_id": "sched_a", "seen_version": "b" * 64}, request_id="cmd-3")
    refused = team.ask("schedules.remove", {"schedule_id": "hb_sched_team", "seen_version": team.version("hb_sched_team")},
                       request_id="cmd-4")
    missing = team.ask("schedules.disable", {"schedule_id": "sched_none", "seen_version": "b" * 64}, request_id="cmd-5")

    assert [(row["schedule_id"], row["action"]) for row in listed["schedules"]] == [
        ("hb_sched_team", {"type": "task"}), ("sched_a", {"type": "task"})]
    replies = {"schedules.list": [listed], "schedules.pause": [paused], "schedules.resume": [stale],
               "schedules.remove": [refused], "schedules.disable": [missing]}
    for kind, bodies in replies.items():
        for body in bodies:
            validator(body_schema_file(f"{kind}.reply")).validate(body)
    assert [paused["outcome"], stale["outcome"], refused["outcome"], missing["outcome"]] == [
        "ok", "changed_since_read", "not_allowed", "not_found"]


def test_a_person_the_team_does_not_know_gets_nothing(team: Team) -> None:
    team.add("sched_a", "ada")
    frame = stand_in_frame(TEAM, "schedules.list", {"request_id": "cmd-x", "offset": 0, "limit": 50},
                           person_ref="p-Nobody_______________x", request_id="cmd-x")

    reply = checked_reply("schedules.list", team.operations.answer(frame))

    assert reply["outcome"] == "not_allowed"


def test_another_persons_schedule_is_not_found_and_unchanged(team: Team) -> None:
    team.add("sched_bo", "bo", session_key="session_bo")

    reply = team.ask("schedules.disable", {"schedule_id": "sched_bo", "seen_version": team.version("sched_bo")})

    assert reply["outcome"] == "not_found"
    assert team.store.get_scheduled_message("sched_bo").enabled is True


def test_a_change_names_the_listed_version_and_returns_the_new_one(team: Team) -> None:
    team.add("sched_a", "ada")
    listed = team.version("sched_a")

    paused = team.ask("schedules.pause", {"schedule_id": "sched_a", "seen_version": listed, "paused_until": LATER})
    stale = team.ask("schedules.resume", {"schedule_id": "sched_a", "seen_version": listed}, request_id="cmd-2")
    resumed = team.ask("schedules.resume", {"schedule_id": "sched_a", "seen_version": paused["version"]["seen_version"]},
                       request_id="cmd-3")

    saved = team.store.get_scheduled_message("sched_a")
    assert paused["outcome"] == "ok" and paused["state"] == "paused"
    assert stale["outcome"] == "changed_since_read"
    assert resumed["outcome"] == "ok" and saved.paused_until is None
    # The version a change returns is the one the next list shows.
    assert resumed["version"] == {"state": "available", "seen_version": team.version("sched_a")}


def test_a_member_cannot_change_a_system_schedule_and_an_administrator_must_name_its_version(team: Team) -> None:
    team.add("hb_sched_team", "ada", system=True)
    team.add("hb_sched_olu", "olu", system=True)
    current = team.version("hb_sched_team")

    refused = team.ask("schedules.remove", {"schedule_id": "hb_sched_team", "seen_version": current})
    listed = team.listed()

    assert refused["outcome"] == "not_allowed"
    assert team.store.get_scheduled_message("hb_sched_team") is not None
    # The member still sees their system schedule, with its kind.
    assert listed["schedules"][0]["system_schedule_kind"] == "heartbeat_launcher"

    stale = team.ask("schedules.remove", {"schedule_id": "hb_sched_olu", "seen_version": "b" * 64}, person="olu",
                     request_id="cmd-2")
    removed = team.ask("schedules.remove", {"schedule_id": "hb_sched_olu", "seen_version": team.version("hb_sched_olu")},
                       person="olu", request_id="cmd-3")

    assert stale == {"outcome": "changed_since_read", "current_version": stale["current_version"],
                     "message": "The schedule changed since you listed it. Refresh and try again."}
    assert removed == {"outcome": "ok", "state": "removed", "schedule_id": "hb_sched_olu"}
    assert team.store.get_scheduled_message("hb_sched_olu") is None


def test_a_change_made_while_the_permission_is_decided_is_caught_in_the_write(team: Team) -> None:
    schedule = team.add("hb_sched_olu", "olu", system=True)
    listed = team.version("hb_sched_olu")

    def decided_after_a_pause(person: CommandPerson) -> bool:
        # Something else pauses the schedule after the team services read it.
        assert apply_person_schedule_change(team.store, "pause", schedule, person_id="olu", team_id=TEAM,
                                            expected_record_digest=None, paused_until=LATER)
        return True

    team.may_change_system = decided_after_a_pause
    reply = team.ask("schedules.remove", {"schedule_id": "hb_sched_olu", "seen_version": listed}, person="olu")

    assert reply["outcome"] == "changed_since_read"
    assert reply["current_version"] == team.version("hb_sched_olu")
    assert team.store.get_scheduled_message("hb_sched_olu").paused_until == LATER


def test_a_failed_decision_changes_nothing_and_is_not_kept_for_retries(team: Team, caplog) -> None:
    team.add("hb_sched_olu", "olu", system=True)
    listed = team.version("hb_sched_olu")
    body = {"schedule_id": "hb_sched_olu", "seen_version": listed}

    def failing(person: CommandPerson) -> bool:
        raise RuntimeError("Permission evaluator failed")

    team.may_change_system = failing
    with caplog.at_level(logging.ERROR):
        failed = team.ask("schedules.remove", body, person="olu", request_id="cmd-retry")
    team.may_change_system = lambda person: True
    retried = team.ask("schedules.remove", body, person="olu", request_id="cmd-retry")

    assert failed["outcome"] == "runtime_error"
    assert "event=team_schedule_change_failed" in caplog.text
    assert retried["outcome"] == "ok"
    assert team.store.get_scheduled_message("hb_sched_olu") is None


def test_a_retried_change_gets_its_stored_reply_and_is_carried_out_once(team: Team, caplog) -> None:
    team.add("sched_a", "ada")
    listed = team.version("sched_a")
    pause = {"schedule_id": "sched_a", "seen_version": listed, "paused_until": LATER}

    first = team.ask("schedules.pause", pause, request_id="cmd-pause")
    team.ask("schedules.resume", {"schedule_id": "sched_a", "seen_version": first["version"]["seen_version"]},
             request_id="cmd-resume")
    again = team.ask("schedules.pause", pause, request_id="cmd-pause")
    removed = team.ask("schedules.remove", {"schedule_id": "sched_a", "seen_version": team.version("sched_a")},
                       request_id="cmd-remove")
    removed_again = team.ask("schedules.remove", {"schedule_id": "sched_a", "seen_version": "c" * 64},
                             request_id="cmd-remove")

    # The retry is answered from the stored reply: the later resume stands.
    assert again == first
    assert removed_again == removed == {"outcome": "ok", "state": "removed", "schedule_id": "sched_a"}
    assert "event=operation_outcome_body_mismatch" in caplog.text


def test_a_refusal_is_kept_for_retries(team: Team) -> None:
    team.add("hb_sched_team", "ada", system=True)
    body = {"schedule_id": "hb_sched_team", "seen_version": team.version("hb_sched_team")}

    refused = team.ask("schedules.disable", body, request_id="cmd-disable")
    team.may_change_system = lambda person: True
    again = team.ask("schedules.disable", body, request_id="cmd-disable")

    assert again == refused and refused["outcome"] == "not_allowed"
    assert team.store.get_scheduled_message("hb_sched_team").enabled is True


# -- through the gateway --------------------------------------------------------------


def _session(session_key: str, owner: str, chat_id: str) -> SessionRecord:
    return SessionRecord(
        session_key=session_key, session_alias=None, conversation_id=f"conversation_{session_key}", last_task_id=None,
        manager_agent_id="manager_1", channel_name="telegram", user_id=owner, chat_id=chat_id, thread_id=None,
        status="active", created_at="2026-11-04T00:00:00+00:00", updated_at="2026-11-04T00:00:00+00:00",
        owner_canonical_user_id=owner,
    )


def _gateway(team: Team, *, owner: str = "ada") -> GatewayScheduleApplication:
    sessions = {key: _session(key, owner, chat) for key, chat in (("session_ada_a", "41"), ("session_ada_b", "42"))}

    def resolve_session(*, requested_session_key: Optional[str], **_kwargs: Any) -> SessionRecord:
        return sessions[requested_session_key or "session_ada_a"]

    references = AccessStorePersonReferences(team.access.settings, TEAM)
    return GatewayScheduleApplication(
        settings=GatewayScheduleSettings(
            instance_name="team_schedules", response_route="gateway", user_display_name="Ada", request_timezone="UTC",
            default_session_key="session_ada_a", default_owner_canonical_user_id=owner,
            manager_unavailable_text="unavailable",
        ),
        sessions=SimpleNamespace(resolve_session=resolve_session),  # type: ignore[arg-type]
        request_context=SimpleNamespace(  # type: ignore[arg-type]
            canonical_user_id=lambda route: owner,
            manager_principal=lambda route: GatewayCommandPerson(canonical_user_id=owner, team_id=TEAM,
                                                                 roles=PEOPLE[owner]),
            users_enabled=True,
        ),
        delivery=SimpleNamespace(remove_scheduled_target=lambda schedule_id: None),  # type: ignore[arg-type]
        manager=SimpleNamespace(manager_id="manager_1"),  # type: ignore[arg-type]
        team_commands=GatewayTeamCommandService(team_services=_InProcess(team.operations), references=references,
                                                request_ids=_Ids().new_correlation_id),
        id_generator=SimpleNamespace(),  # type: ignore[arg-type]
        delivery_refs=team.refs,
    )


@pytest.mark.asyncio
async def test_the_gateway_lists_one_chats_schedules_by_its_reference(team: Team) -> None:
    team.add("sched_a", "ada", session_key="session_ada_a")
    team.add("sched_b", "ada", session_key="session_ada_b")
    gateway = _gateway(team)

    chat_b = await gateway.list(session_key="session_ada_b")
    everywhere = await gateway.list(session_key="session_ada_b", owner_wide=True)

    assert [(item.schedule_id, item.session_key) for item in chat_b] == [("sched_b", "session_ada_b")]
    assert [item.schedule_id for item in everywhere] == ["sched_a", "sched_b"]


@pytest.mark.asyncio
@pytest.mark.parametrize("command", ["/pause schedule {id} 2030-01-01T00:00:00+00:00", "/disable schedule {id}"])
async def test_chat_schedule_commands_reach_the_stores_version_check(team: Team, command: str) -> None:
    """Chat and CLI commands name only the schedule: the gateway acts on its current listed version."""

    schedule = team.add("sched_chat_control", "ada")
    service = GatewayManagerControlCommandService(SimpleNamespace(), schedules=_gateway(team))  # type: ignore[arg-type]

    result = await service.execute(ManagerControlCommandRequest(
        command=command.format(id=schedule.schedule_id), current_session_key="session_ada_a",
        actor=GatewayActorContext(channel_name="telegram", user_id="ada", chat_id="41", canonical_user_id="ada"),
    ))

    saved = team.store.get_scheduled_message(schedule.schedule_id)
    if command.startswith("/pause"):
        assert result.text == f"Schedule {schedule.schedule_id} paused."
        assert saved.paused_until is not None and saved.paused_until.startswith("2030-01-01T00:00:00")
    else:
        assert result.text == f"Schedule {schedule.schedule_id} disabled."
        assert saved.enabled is False


# -- delivery references -------------------------------------------------------------


def test_delivery_references_are_keyed_per_instance_and_team(tmp_path: Path) -> None:
    layout = InstanceLayout.at(tmp_path)

    assert ensure_delivery_ref_key(layout) is True
    assert ensure_delivery_ref_key(layout) is False
    refs = DeliveryRefs.load(layout, TEAM)
    other_instance = InstanceLayout.at(tmp_path / "other")
    ensure_delivery_ref_key(other_instance)

    reference = refs.ref("session_ada_a")
    assert re.fullmatch(r"dr_[A-Za-z0-9_-]{43}", reference)
    assert DeliveryRefs.load(layout, TEAM).ref("session_ada_a") == reference
    assert refs.ref("session_ada_b") != reference
    assert DeliveryRefs.load(layout, "another_team").ref("session_ada_a") != reference
    assert DeliveryRefs.load(other_instance, TEAM).ref("session_ada_a") != reference


# -- schedules.install (Unit 7) -------------------------------------------------------


@pytest.mark.asyncio
async def test_gateway_schedules_install_creates_vouches_permits_and_filters_by_delivery_ref(team: Team) -> None:
    """Gateway schedules.install validates against the contract, sets owner_vouched=1, triggers initial
    permit issuance, stores idempotent replies, and supports delivery_ref filtering in schedules.list."""

    permitted: list[str] = []
    team.operations._on_schedule_installed = permitted.append
    delivery_ref_a = team.refs.ref("session_ada_a")
    delivery_ref_b = team.refs.ref("session_ada_b")

    create_body = {
        "request_id": "cmd-install-1",
        "schedule_id": "sched_gw_1",
        "mode": "create",
        "management_class": "user",
        "name": "Morning brief",
        "timing": {"kind": "interval", "interval_ms": 300_000},
        "repeat_count": 0,
        "enabled": True,
        "action": {"type": "task", "instruction": {"type": "inline", "text": "Summarize overnight mail."}},
        "deliver_to": {"delivery_ref": delivery_ref_a, "conversation_id": "conv_ada_a", "time_zone": "UTC"},
    }
    validator(body_schema_file("schedules.install")).validate(create_body)
    frame = stand_in_frame(TEAM, "schedules.install", create_body, person_ref=reference_of("ada"), request_id="cmd-install-1")
    created = checked_reply("schedules.install", await team.operations.handle(frame))
    validator(body_schema_file("schedules.install.reply")).validate(created)

    assert created["outcome"] == "ok"
    assert created["state"] == "installed"
    assert created["schedule_id"] == "sched_gw_1"
    assert created["version"]["state"] == "available"
    assert team.store.is_owner_vouched("sched_gw_1") is True
    assert team.store.delivery_ref_of("sched_gw_1") == delivery_ref_a
    assert permitted == ["sched_gw_1"]

    # Idempotent retry returns the exact stored reply without re-running the install.
    retried = checked_reply("schedules.install", await team.operations.handle(frame))
    assert retried == created
    assert permitted == ["sched_gw_1"]

    # ensure on an existing schedule keeps it untouched.
    ensure_body = {**create_body, "request_id": "cmd-install-2", "mode": "ensure"}
    ensure_frame = stand_in_frame(TEAM, "schedules.install", ensure_body, person_ref=reference_of("ada"), request_id="cmd-install-2")
    kept = checked_reply("schedules.install", await team.operations.handle(ensure_frame))
    assert kept["outcome"] == "ok" and kept["state"] == "kept"

    # schedules.list filters by the stored delivery_ref column even without gateway_context.session_key.
    chat_a = team.listed(delivery_ref=delivery_ref_a, request_id="cmd-list-a")
    chat_b = team.listed(delivery_ref=delivery_ref_b, request_id="cmd-list-b")
    assert [row["schedule_id"] for row in chat_a["schedules"]] == ["sched_gw_1"]
    assert chat_b["schedules"] == []


@pytest.mark.asyncio
async def test_agent_schedules_install_enforces_seen_version_ownership_blobs_and_permits(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Agent schedules.install uses root_task tokens, requires seen_version on replace, checks live
    source schedule permits, protects other people's schedules, and resolves >32KB blob instructions."""

    from custom_transport.protocol1.authority_tokens import root_task_token
    from custom_transport.protocol1.knowledge_blobs import BlobOwner, encode_text_content
    from tests.broker_access_support import (
        broker_gateway_identity,
        broker_people_store,
        broker_person_ref,
        broker_scheduler_identity,
        forget_task_context_keys,
    )
    from tests.scheduled_run_support import _InMemoryBlobStore, _verified_team_frame, bind_test_scheduler

    forget_task_context_keys()
    try:
        people_store = broker_people_store()
        people_store.register(TEAM, "ada", roles=("member",))
        people_store.register(TEAM, "bo", roles=("member",))
        store = ScheduleStore(db_path=str(tmp_path / "agent_install.db"))
        bound = bind_test_scheduler(monkeypatch, store=store, team_id=TEAM, people_store=people_store)
        blobs = _InMemoryBlobStore()
        bound.schedule_ops._blobs = blobs

        now = datetime.now(timezone.utc)
        gw_id = broker_gateway_identity(TEAM)
        sched_id = broker_scheduler_identity(TEAM)
        ada_token = root_task_token(
            gw_id, team=TEAM, root_task_id="task_ada_1", request_id="req_ada_1", person_ref=broker_person_ref("ada"), issued=now
        )
        bo_token = root_task_token(
            gw_id, team=TEAM, root_task_id="task_bo_1", request_id="req_bo_1", person_ref=broker_person_ref("bo"), issued=now
        )

        async def _send(body: dict[str, Any], token: dict[str, Any]) -> dict[str, Any]:
            frame = _verified_team_frame("schedules.install", body, sender_agent="lead_1", authority=token, team=TEAM)
            return checked_reply("schedules.install", await bound.schedule_ops.handle(frame))

        # 1. Create with >32KB blob-backed instruction.
        large_text = "Detailed research checklist: " + ("check status and summarize " * 1500)
        assert len(large_text) > 32_768
        blob_content = await encode_text_content(
            large_text, blobs, BlobOwner(request_id="req-blob-1", person_ref=broker_person_ref("ada")), inline_limit=32_768
        )
        assert blob_content["type"] == "blob"
        create_body = {
            "request_id": "req-blob-1",
            "schedule_id": "sched_agent_1",
            "mode": "create",
            "management_class": "user",
            "name": "Large job",
            "timing": {"kind": "interval", "interval_ms": 600_000},
            "repeat_count": 0,
            "enabled": True,
            "action": {"type": "task", "instruction": blob_content},
        }
        created = await _send(create_body, ada_token)
        assert created["outcome"] == "ok" and created["state"] == "installed"
        saved = store.get_scheduled_message("sched_agent_1")
        assert saved is not None and saved.payload.problem_statement == large_text.strip()
        assert store.is_owner_vouched("sched_agent_1") is True
        assert store.run_permit("sched_agent_1") is not None

        # 2. Duplicate create is refused with id_in_use.
        dup = await _send({**create_body, "request_id": "req-dup-1"}, ada_token)
        assert dup["outcome"] == "id_in_use"

        # 3. Agent replace requires seen_version and rejects stale digests.
        replace_base = {
            **create_body,
            "mode": "replace",
            "action": {"type": "task", "instruction": {"type": "inline", "text": "Updated checklist."}},
        }
        missing_ver = await _send({**replace_base, "request_id": "req-rep-1"}, ada_token)
        assert missing_ver["outcome"] == "invalid_request"
        stale_ver = await _send({**replace_base, "request_id": "req-rep-2", "seen_version": "a" * 64}, ada_token)
        assert stale_ver["outcome"] == "changed_since_read"
        replaced = await _send(
            {**replace_base, "request_id": "req-rep-3", "seen_version": created["version"]["seen_version"]},
            ada_token,
        )
        assert replaced["outcome"] == "ok" and replaced["state"] == "replaced"
        assert store.get_scheduled_message("sched_agent_1").payload.problem_statement == "Updated checklist."

        # 4. Another person cannot overwrite Ada's schedule.
        other_person = await _send(
            {**replace_base, "request_id": "req-rep-4", "seen_version": replaced["version"]["seen_version"]},
            bo_token,
        )
        assert other_person["outcome"] == "not_allowed"

        # 5. System-work root_task token without person is refused.
        system_token = root_task_token(
            sched_id, team=TEAM, root_task_id="sched_run_sys", request_id="sched_run_sys", schedule_id="sched_sys_1", issued=now
        )
        sys_refused = await _send(
            {**create_body, "request_id": "req-sys-1", "schedule_id": "sched_from_sys", "action": {"type": "task", "instruction": {"type": "inline", "text": "Sys"}}},
            system_token,
        )
        assert sys_refused["outcome"] == "not_allowed"

        # 6. Permit-bound token from a disabled source schedule is refused live.
        permit_token = store.run_permit("sched_agent_1")
        assert permit_token is not None
        store.set_enabled("sched_agent_1", False)
        stopped_permit = await _send(
            {
                "request_id": "req-permit-stopped",
                "schedule_id": "sched_from_stopped_permit",
                "mode": "create",
                "management_class": "user",
                "timing": {"kind": "interval", "interval_ms": 60_000},
                "repeat_count": 0,
                "enabled": True,
                "action": {"type": "task", "instruction": {"type": "inline", "text": "Should fail."}},
            },
            permit_token.model_dump(mode="json", exclude_none=True),
        )
        assert stopped_permit["outcome"] == "not_allowed"
        assert store.get_scheduled_message("sched_from_stopped_permit") is None
    finally:
        forget_task_context_keys()


@pytest.mark.asyncio
async def test_gateway_schedule_add_and_delivery_projection_resolve_session_via_scheduled_target(
    tmp_path: Path,
) -> None:
    """GatewayScheduleApplication.add installs via schedules.install (no raw session_key stored on the
    schedule), and GatewayDeliveryProjection resolves the target session from scheduled_targets."""

    from assistant_gateway.application.delivery_projection import (
        GatewayDeliveryProjection,
        GatewayDeliveryProjectionSettings,
        _DeliveryMetadata,
    )
    from assistant_gateway.application.delivery_service import GatewayDeliveryService
    from assistant_gateway.application.schedule_management import GatewayScheduleCreateRequest
    from assistant_gateway.runtime_ports import UuidGatewayIdGenerator
    from assistant_gateway.store import GatewayStateStore
    from storage.config import resolve_storage_settings

    gw_store = GatewayStateStore(str(tmp_path / "gateway.db"))
    gw_store.create_session(
        session_key="session_ada_a",
        conversation_id="conv_ada_a",
        manager_agent_id="manager_1",
        channel_name="telegram",
        user_id="ada",
        chat_id="41",
        owner_canonical_user_id="ada",
    )
    delivery = GatewayDeliveryService(repository=gw_store.delivery_repository, unit_of_work_factory=gw_store)
    team = Team(
        tmp_path,
        resolve_storage_settings({"backend": "sqlite", "sqlite": {"path": str(tmp_path / "schedules.db")}}),
    )
    references = AccessStorePersonReferences(team.access.settings, TEAM)
    session = gw_store.get_session("session_ada_a")
    assert session is not None
    gateway = GatewayScheduleApplication(
        settings=GatewayScheduleSettings(
            instance_name="team_schedules",
            response_route="gateway",
            user_display_name="Ada",
            request_timezone="UTC",
            default_session_key="session_ada_a",
            default_owner_canonical_user_id="ada",
            manager_unavailable_text="unavailable",
        ),
        sessions=SimpleNamespace(resolve_session=lambda **_kw: session),  # type: ignore[arg-type]
        request_context=SimpleNamespace(  # type: ignore[arg-type]
            canonical_user_id=lambda _route: "ada",
            manager_principal=lambda _route: GatewayCommandPerson(canonical_user_id="ada", team_id=TEAM, roles=("member",)),
            requester_presentation_for_session=lambda _session, _route: SimpleNamespace(timezone="UTC"),
            users_enabled=True,
        ),
        delivery=delivery,
        manager=SimpleNamespace(manager_id="manager_1"),  # type: ignore[arg-type]
        team_commands=GatewayTeamCommandService(
            team_services=_InProcess(team.operations),
            references=references,
            request_ids=_Ids().new_correlation_id,
        ),
        id_generator=UuidGatewayIdGenerator(),
        delivery_refs=team.refs,
    )

    schedule_id = await gateway.add(
        GatewayScheduleCreateRequest(
            name="Flights",
            instruction="Check flights",
            interval_seconds=300,
            session_key="session_ada_a",
        )
    )
    assert schedule_id
    stored = team.store.get_scheduled_message(schedule_id)
    assert stored is not None
    assert stored.payload_meta.gateway_context.session_key is None
    assert team.store.is_owner_vouched(schedule_id) is True
    assert team.store.delivery_ref_of(schedule_id) == team.refs.ref("session_ada_a")

    # GatewayDeliveryProjection resolves session_key from scheduled_targets when metadata.session_key is None.
    projection = GatewayDeliveryProjection(
        settings=GatewayDeliveryProjectionSettings(default_session_key="session_ada_a"),
        sessions=gw_store.session_repository,
        delivery=delivery,
        presentation=SimpleNamespace(),  # type: ignore[arg-type]
        attachments=SimpleNamespace(),  # type: ignore[arg-type]
    )
    resolved_key, resolved_session = projection._resolve_session(
        _DeliveryMetadata(
            gateway_context=GatewayContext(
                canonical_user_id="ada",
                scheduled_job_id=schedule_id,
                delivery_mode="cron",
            ),
            conversation_id=stored.payload_meta.conversation_id,
            scheduled_job_id=schedule_id,
            correlation_id=None,
            session_key=None,
        ),
        request_record=None,
    )
    assert resolved_key == "session_ada_a"
    assert resolved_session is not None and resolved_session.session_key == "session_ada_a"
