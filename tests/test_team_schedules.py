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


@pytest.mark.asyncio
async def test_schedules_install_13c5_vouching_rules_for_gateway_run_and_owner_permits(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """C29: only gateway-signed tokens vouch; scheduler run tokens, schedule permits, and admin
    owner permits install user schedules unvouched, drop vouching on replace, and cannot install
    system-managed schedules or use stopped source schedules."""

    from custom_messages.contracts.schedule_v1 import ScheduledMessage
    from custom_messages.contracts.task_v1 import Task
    from custom_messages.enums import MessageMetaType, ScheduleManagementClass, SystemScheduleKind
    from custom_transport.protocol1.authority_tokens import root_task_token, schedule_permit_token
    from tests.broker_access_support import (
        broker_gateway_identity,
        broker_people_store,
        broker_person_ref,
        broker_scheduler_identity,
        forget_task_context_keys,
    )
    from tests.scheduled_run_support import _verified_team_frame, bind_test_scheduler

    forget_task_context_keys()
    try:
        people_store = broker_people_store()
        people_store.register(TEAM, "ada", roles=("owner",))
        people_store.register(TEAM, "bo", roles=("member",))
        store = ScheduleStore(db_path=str(tmp_path / "vouching_13c5.db"))
        bound = bind_test_scheduler(monkeypatch, store=store, team_id=TEAM, people_store=people_store)

        now = datetime.now(timezone.utc)
        gw_id = broker_gateway_identity(TEAM)
        sched_id = broker_scheduler_identity(TEAM)
        ada_gw_token = root_task_token(
            gw_id, team=TEAM, root_task_id="task_ada_gw", request_id="req_ada_gw", person_ref=broker_person_ref("ada"), issued=now
        )
        bo_gw_token = root_task_token(
            gw_id, team=TEAM, root_task_id="task_bo_gw", request_id="req_bo_gw", person_ref=broker_person_ref("bo"), issued=now
        )

        async def _send(body: dict[str, Any], token: dict[str, Any]) -> dict[str, Any]:
            frame = _verified_team_frame("schedules.install", body, sender_agent="lead_1", authority=token, team=TEAM)
            return checked_reply("schedules.install", await bound.schedule_ops.handle(frame))

        def _user_body(req_id: str, sid: str, *, mode: str = "create", seen_version: str | None = None) -> dict[str, Any]:
            body: dict[str, Any] = {
                "request_id": req_id,
                "schedule_id": sid,
                "mode": mode,
                "management_class": "user",
                "timing": {"kind": "interval", "interval_ms": 600_000},
                "repeat_count": 0,
                "enabled": True,
                "action": {"type": "task", "instruction": {"type": "inline", "text": "Check updates."}},
            }
            if seen_version is not None:
                body["seen_version"] = seen_version
            return body

        # 1. Gateway-signed root-task token vouches for the schedule and issues a permit.
        gw_created = await _send(_user_body("req-gw-1", "sched_gw_vouched"), bo_gw_token)
        assert gw_created["outcome"] == "ok"
        assert store.is_owner_vouched("sched_gw_vouched") is True
        bo_permit = store.run_permit("sched_gw_vouched")
        assert bo_permit is not None

        # 2. Permit-backed scheduled run creates an UNVOUCHED schedule with no permit.
        from_permit = await _send(
            _user_body("req-permit-1", "sched_from_permit"),
            bo_permit.model_dump(mode="json", exclude_none=True),
        )
        assert from_permit["outcome"] == "ok"
        assert store.is_owner_vouched("sched_from_permit") is False
        assert store.run_permit("sched_from_permit") is None

        # 3. Scheduler-signed per-run token (run_context.sign) creates an UNVOUCHED schedule with no permit.
        bo_run_token = root_task_token(
            sched_id,
            team=TEAM,
            root_task_id="run_bo_1",
            request_id="schedule_run_bo_1",
            person_ref=broker_person_ref("bo"),
            schedule_id="sched_gw_vouched",
            issued=now,
        )
        from_run = await _send(_user_body("req-run-1", "sched_from_run"), bo_run_token)
        assert from_run["outcome"] == "ok"
        assert store.is_owner_vouched("sched_from_run") is False
        assert store.run_permit("sched_from_run") is None

        # 4. Administrator's internal_improvement owner permit creates user schedules UNVOUCHED and
        #    is refused for system-managed schedules (cannot bypass vouching via is_system_managed_schedule).
        maint = ScheduledMessage(
            schedule_id="sched_internal_imp",
            schedule_name="internal_improvement:team",
            management_class=ScheduleManagementClass.SYSTEM,
            system_schedule_kind=SystemScheduleKind.INTERNAL_IMPROVEMENT,
            payload=Task(task_type=MessageMetaType.REQUEST, problem_statement="Maintain team."),
            payload_meta=MessageMeta(gateway_context=GatewayContext(canonical_user_id="ada")),
            interval_ms=3_600_000,
            repeat_count=None,
        )
        store.add(maint, claim_team_id=TEAM)
        owner_permit = schedule_permit_token(
            sched_id,
            team=TEAM,
            schedule_id="sched_internal_imp",
            person_ref=broker_person_ref("ada"),
            owner_permit=True,
            issued=now,
        )
        from_owner_permit = await _send(_user_body("req-op-user", "sched_from_owner_permit"), owner_permit)
        assert from_owner_permit["outcome"] == "ok"
        assert store.is_owner_vouched("sched_from_owner_permit") is False
        assert store.run_permit("sched_from_owner_permit") is None

        sys_body = {
            "request_id": "req-op-sys",
            "schedule_id": "sched_sys_from_owner_permit",
            "mode": "create",
            "management_class": "system",
            "system_schedule_kind": "heartbeat_launcher",
            "timing": {"kind": "interval", "interval_ms": 600_000},
            "repeat_count": 0,
            "enabled": True,
            "action": {"type": "task", "instruction": {"type": "inline", "text": "Sneaky system schedule."}},
        }
        sys_refused = await _send(sys_body, owner_permit)
        assert sys_refused["outcome"] == "not_allowed"
        assert store.get_scheduled_message("sched_sys_from_owner_permit") is None

        # 5. Scheduler-signed replace on an existing vouched schedule drops vouching and clears its permit.
        vouched_ada = await _send(_user_body("req-ada-1", "sched_ada_target"), ada_gw_token)
        assert store.is_owner_vouched("sched_ada_target") is True
        assert store.run_permit("sched_ada_target") is not None
        replaced = await _send(
            _user_body(
                "req-ada-rep",
                "sched_ada_target",
                mode="replace",
                seen_version=vouched_ada["version"]["seen_version"],
            ),
            owner_permit,
        )
        assert replaced["outcome"] == "ok"
        assert store.is_owner_vouched("sched_ada_target") is False
        assert store.run_permit("sched_ada_target") is None

        # 6. Once the source schedule of a per-run token is disabled, further installs are refused.
        store.set_enabled("sched_gw_vouched", False)
        stopped_run = await _send(_user_body("req-run-stopped", "sched_after_stop"), bo_run_token)
        assert stopped_run["outcome"] == "not_allowed"
        assert store.get_scheduled_message("sched_after_stop") is None
    finally:
        forget_task_context_keys()


def test_agent_created_schedules_from_member_chat_route_replies_and_chat_list(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """C27: create_scheduled_job (one-shot & recurring) and turn-action schedule_message in a
    member's chat carry delivery_ref + conversation_id through schedules.install, appear in that
    chat's schedules.list, and project fired run replies to the member's exact chat session."""

    import asyncio
    from assistant_gateway.application.delivery_presentation import GatewayDeliveryPresentation
    from assistant_gateway.application.delivery_projection import (
        DeliveryPersonMismatch,
        GatewayDeliveryProjection,
        GatewayDeliveryProjectionSettings,
    )
    from assistant_gateway.application.delivery_service import GatewayDeliveryService
    from assistant_gateway.application.request_context import (
        GatewayRequestContextService,
        GatewayRequestContextSettings,
        GatewayRequestRoute,
    )
    from assistant_gateway.store import GatewayStateStore
    from custom_agents.conversation_store import ConversationStore
    from custom_agents.schedule_installation import create_schedule_from_turn_action
    from custom_messages.contracts.access_v2 import PersonStanding, TeamPersonV1
    from custom_messages.contracts.envelope_v1 import MessageEnvelope
    from custom_messages.contracts.routing_v1 import MessageHeader, MessageMeta
    from custom_messages.contracts.schedule_v1 import ScheduledMessage
    from custom_messages.contracts.task_v1 import Task
    from custom_messages.enums import MessageMetaType, RequesterType, ScheduleManagementClass
    from custom_tools.providers.schedules import create_scheduled_job
    from storage.access_repository import AccessRepository
    from storage.config import resolve_storage_settings
    from team_services.schedules import ScheduleInstallDefaults
    from tests.broker_access_support import (
        BrokerGatewayTokenSigner,
        broker_people_store,
        broker_person_ref,
        forget_task_context_keys,
    )
    from tests.scheduled_run_support import bind_test_scheduler
    from tests.task_work_support import set_agent_ref
    from tests.test_schedule_tools import _FakeTaskManager, _FakeWake
    from tests.tool_result_support import tool_json

    forget_task_context_keys()
    try:
        refs = DeliveryRefs(TEAM, b"c27-delivery-ref-secret-key")
        gw_store = GatewayStateStore(str(tmp_path / "gateway.db"))
        gw_store.create_session(
            session_key="session_ada_default",
            conversation_id="conv_ada_default",
            manager_agent_id="manager_1",
            channel_name="web",
            user_id="ada_web",
            chat_id="web_1",
            owner_canonical_user_id="ada",
        )
        gw_store.create_session(
            session_key="session_ada_work",
            conversation_id="conv_ada_work",
            manager_agent_id="manager_1",
            channel_name="telegram",
            user_id="ada_tg",
            chat_id="tg_ada_work",
            owner_canonical_user_id="ada",
        )
        gw_store.create_session(
            session_key="session_bo_telegram",
            conversation_id="conv_bo_1",
            manager_agent_id="manager_1",
            channel_name="telegram",
            user_id="bo_tg",
            chat_id="tg_bo_99",
            owner_canonical_user_id="bo",
        )
        delivery = GatewayDeliveryService(repository=gw_store.delivery_repository, unit_of_work_factory=gw_store)

        people_store = broker_people_store()
        people_store.register(TEAM, "ada", roles=("owner",))
        people_store.register(TEAM, "bo", roles=("member",))
        db_path = str(tmp_path / "team_schedules.db")
        storage_settings = resolve_storage_settings({"backend": "sqlite", "sqlite": {"path": db_path}})
        now = datetime.now(timezone.utc)
        AccessRepository(storage_settings).replace_people(
            TEAM,
            [
                TeamPersonV1(
                    person_ref=broker_person_ref("ada"),
                    team_id=TEAM,
                    person_id="ada",
                    standing=PersonStanding.ACTIVE,
                    roles=("owner",),
                    changed_at_utc=now,
                ),
                TeamPersonV1(
                    person_ref=broker_person_ref("bo"),
                    team_id=TEAM,
                    person_id="bo",
                    standing=PersonStanding.ACTIVE,
                    roles=("member",),
                    changed_at_utc=now,
                ),
            ],
            at=now,
        )
        store = ScheduleStore(db_path=db_path, storage_settings=storage_settings)
        conversations = ConversationStore(
            db_path=db_path,
            storage_settings=storage_settings,
            artifacts_dir=str(tmp_path / "artifacts"),
        )
        bound = bind_test_scheduler(
            monkeypatch,
            store=store,
            team_id=TEAM,
            storage_settings=storage_settings,
        )
        bound.schedule_ops._delivery_refs = refs
        bound.schedule_ops._conversations = conversations
        bound.schedule_ops._install_defaults = ScheduleInstallDefaults(
            lead_agent_type="TeamArchitect",
            response_route="gateway_queue",
            instance_name="team_c27",
            default_timezone="UTC",
        )

        # Gateway admits a chat task for member "bo" in session_bo_telegram and owner "ada" in session_ada_work.
        def _build_chat_ctx(person_id: str, session_key: str, chat_id: str, user_id: str, conv_id: str) -> GatewayContext:
            req_ctx = GatewayRequestContextService(
                settings=GatewayRequestContextSettings(
                    instance_name="team_c27",
                    team_id=TEAM,
                    default_canonical_user_id="ada",
                    default_display_name="Ada",
                    default_timezone="UTC",
                    heartbeat_source_id=None,
                    users_enabled=False,
                ),
                identity=SimpleNamespace(),  # type: ignore[arg-type]
                token_signer=BrokerGatewayTokenSigner(team_id=TEAM),
                person_ref_resolver=broker_person_ref,
                delivery_refs=refs,
            )
            return req_ctx.build_gateway_context(
                session_key=session_key,
                delivery_mode="interactive",
                route=GatewayRequestRoute(
                    channel_name="telegram",
                    user_id=user_id,
                    chat_id=chat_id,
                    canonical_user_id=person_id,
                ),
                manager_agent_id="manager_1",
                root_task_id=f"task_{person_id}_chat",
                conversation_id=conv_id,
            )

        bo_gc = _build_chat_ctx("bo", "session_bo_telegram", "tg_bo_99", "bo_tg", "conv_bo_1")
        assert bo_gc.delivery_ref == refs.ref("session_bo_telegram")

        bo_header = MessageHeader(
            from_agent_id="gateway_queue",
            to_routing_path="manager_1",
            response_routing_key="gateway_queue",
            agent_signature="",
        )
        bo_meta = MessageMeta(
            from_agent_name="Bo",
            conversation_id="conv_bo_1",
            request_timezone="Europe/Berlin",
            requester_type=RequesterType.USER_REQUESTER,
            gateway_context=bo_gc,
        )
        bo_task_info = SimpleNamespace(
            task=Task(task_id="task_bo_chat", task_type=MessageMetaType.REQUEST, problem_statement="Remind me"),
            header=bo_header,
            meta=bo_meta,
        )
        bo_tm = _FakeTaskManager(db_path, bo_task_info)
        bo_tm.get_task_objective_id = lambda _tid: None  # type: ignore[attr-defined]
        bo_agent = SimpleNamespace(
            id="manager_1",
            name="TeamArchitect",
            named_agent_type="TeamArchitect",
            team_id=TEAM,
            is_team_lead=True,
            instance_timezone="UTC",
            _schedule_store=store,
            _conversation_store=conversations,
            _scheduler_wake=_FakeWake(),
            task_manager=bo_tm,
            test_task_id="task_current",
            _logger=logging.getLogger("test.c27_bo"),
        )
        set_agent_ref(bo_agent)

        # 1. One-shot create_scheduled_job from member's chat.
        one_shot = tool_json(create_scheduled_job(instruction="Remind me to stretch", delay_seconds=600))
        assert "schedule_id" in one_shot, one_shot
        one_shot_id = one_shot["schedule_id"]
        saved_one_shot = store.get_scheduled_message(one_shot_id)
        assert saved_one_shot is not None
        assert store.delivery_ref_of(one_shot_id) == refs.ref("session_bo_telegram")
        assert saved_one_shot.payload_meta.gateway_context.delivery_ref == refs.ref("session_bo_telegram")
        assert saved_one_shot.payload_meta.conversation_id == "conv_bo_1"
        assert saved_one_shot.payload_header.response_routing_key == "gateway_queue"

        # 2. Recurring create_scheduled_job (forked into a dedicated conversation unknown to gateway).
        recurring = tool_json(create_scheduled_job(instruction="Daily standup", interval_seconds=86400))
        recurring_id = recurring["schedule_id"]
        saved_recurring = store.get_scheduled_message(recurring_id)
        assert saved_recurring is not None
        assert store.delivery_ref_of(recurring_id) == refs.ref("session_bo_telegram")
        assert saved_recurring.payload_meta.gateway_context.delivery_ref == refs.ref("session_bo_telegram")
        assert saved_recurring.payload_meta.conversation_id is not None
        assert saved_recurring.payload_meta.conversation_id != "conv_bo_1"
        assert saved_recurring.payload_meta.followup_to_task_id is not None
        assert saved_recurring.payload_meta.followup_to_task_id.startswith("sched_conv_seed_")

        # 3. Turn-action schedule_message from member's chat.
        turn_action_sched = ScheduledMessage(
            schedule_id="sched_bo_turn_action",
            management_class=ScheduleManagementClass.USER,
            payload=Task(task_type=MessageMetaType.REQUEST, problem_statement="Follow up from turn action."),
            interval_ms=3_600_000,
        )
        asyncio.run(
            create_schedule_from_turn_action(
                bo_agent,
                turn_action_sched,
                msg_header=bo_header,
                msg_meta=bo_meta,
            )
        )
        saved_turn_action = store.get_scheduled_message("sched_bo_turn_action")
        assert saved_turn_action is not None
        assert store.delivery_ref_of("sched_bo_turn_action") == refs.ref("session_bo_telegram")
        assert saved_turn_action.payload_meta.gateway_context.delivery_ref == refs.ref("session_bo_telegram")

        # 4. Chat's /schedules list (schedules.list with delivery_ref) shows all three for bo's chat only.
        list_bo_chat = stand_in_frame(
            TEAM,
            "schedules.list",
            {"request_id": "list-bo-chat", "delivery_ref": refs.ref("session_bo_telegram"), "offset": 0, "limit": 50},
            person_ref=broker_person_ref("bo"),
            request_id="list-bo-chat",
        )
        listed = checked_reply("schedules.list", asyncio.run(bound.schedule_ops.handle(list_bo_chat)))
        assert {row["schedule_id"] for row in listed["schedules"]} == {
            one_shot_id,
            recurring_id,
            "sched_bo_turn_action",
        }
        list_other_chat = stand_in_frame(
            TEAM,
            "schedules.list",
            {"request_id": "list-bo-other", "delivery_ref": refs.ref("session_bo_other"), "offset": 0, "limit": 50},
            person_ref=broker_person_ref("bo"),
            request_id="list-bo-other",
        )
        assert checked_reply("schedules.list", asyncio.run(bound.schedule_ops.handle(list_other_chat)))["schedules"] == []

        # 5. Fired run replies project to bo's exact Telegram session (not default session / no DeliveryPersonMismatch).
        projection = GatewayDeliveryProjection(
            settings=GatewayDeliveryProjectionSettings(default_session_key="session_ada_default"),
            sessions=gw_store.session_repository,
            delivery=delivery,
            presentation=GatewayDeliveryPresentation(),
            attachments=SimpleNamespace(resolve=lambda *_a, **_kw: ()),  # type: ignore[arg-type]
            delivery_refs=refs,
        )
        for saved in (saved_one_shot, saved_recurring, saved_turn_action):
            reply_env = MessageEnvelope(
                msg_content=Task(task_type=MessageMetaType.RESPONSE, problem_statement="run", answer="Time to stretch!"),
                msg_header=MessageHeader(from_agent_id="manager_1", to_routing_path="gateway_queue"),
                msg_meta=saved.payload_meta.model_copy(deep=True),
            )
            event = projection.project(envelope=reply_env, rendered_text="Time to stretch!", request_record=None)
            assert event.session_key == "session_bo_telegram"
            assert event.channel_name == "telegram"
            assert event.chat_id == "tg_bo_99"
            assert event.user_id == "bo_tg"
            assert event.scheduled_job_id == saved.schedule_id
            # C31: first delivery_ref match records a gateway_scheduled_targets row so later runs never scan.
            recorded_target = delivery.get_scheduled_target(saved.schedule_id)
            assert recorded_target is not None
            assert recorded_target.session_key == "session_bo_telegram"
            assert recorded_target.owner_canonical_user_id == "bo"

        # C31: subsequent runs of an agent-made recurring schedule hit scheduled_targets with zero session scans.
        scan_calls: list[str] = []
        orig_list_sessions = gw_store.session_repository.list_sessions
        orig_list_unowned = gw_store.session_repository.list_unowned_sessions

        def _counting_list_sessions(*args: Any, **kwargs: Any) -> Any:
            scan_calls.append("list_sessions")
            return orig_list_sessions(*args, **kwargs)

        def _counting_list_unowned(*args: Any, **kwargs: Any) -> Any:
            scan_calls.append("list_unowned_sessions")
            return orig_list_unowned(*args, **kwargs)

        monkeypatch.setattr(gw_store.session_repository, "list_sessions", _counting_list_sessions)
        monkeypatch.setattr(gw_store.session_repository, "list_unowned_sessions", _counting_list_unowned)
        second_run = projection.project(
            envelope=MessageEnvelope(
                msg_content=Task(task_type=MessageMetaType.RESPONSE, problem_statement="run 2", answer="Standup!"),
                msg_header=MessageHeader(from_agent_id="manager_1", to_routing_path="gateway_queue"),
                msg_meta=saved_recurring.payload_meta.model_copy(deep=True),
            ),
            rendered_text="Standup!",
            request_record=None,
        )
        assert second_run.session_key == "session_bo_telegram"
        assert scan_calls == []
        monkeypatch.setattr(gw_store.session_repository, "list_sessions", orig_list_sessions)
        monkeypatch.setattr(gw_store.session_repository, "list_unowned_sessions", orig_list_unowned)

        # 6. Owner "ada" creating a reminder in her non-default chat routes back to session_ada_work.
        ada_gc = _build_chat_ctx("ada", "session_ada_work", "tg_ada_work", "ada_tg", "conv_ada_work")
        ada_meta = MessageMeta(
            from_agent_name="Ada",
            conversation_id="conv_ada_work",
            requester_type=RequesterType.USER_REQUESTER,
            gateway_context=ada_gc,
        )
        ada_tm = _FakeTaskManager(
            db_path,
            SimpleNamespace(
                task=Task(task_id="task_ada_work", task_type=MessageMetaType.REQUEST, problem_statement="Remind"),
                header=bo_header,
                meta=ada_meta,
            ),
        )
        ada_tm.get_task_objective_id = lambda _tid: None  # type: ignore[attr-defined]
        ada_agent = SimpleNamespace(
            id="manager_1",
            name="TeamArchitect",
            named_agent_type="TeamArchitect",
            team_id=TEAM,
            is_team_lead=True,
            instance_timezone="UTC",
            _schedule_store=store,
            _conversation_store=conversations,
            _scheduler_wake=_FakeWake(),
            task_manager=ada_tm,
            test_task_id="task_current",
            _logger=logging.getLogger("test.c27_ada"),
        )
        set_agent_ref(ada_agent)
        ada_sched_id = tool_json(create_scheduled_job(instruction="Check deploy", delay_seconds=300))["schedule_id"]
        ada_saved = store.get_scheduled_message(ada_sched_id)
        assert ada_saved is not None
        ada_event = projection.project(
            envelope=MessageEnvelope(
                msg_content=Task(task_type=MessageMetaType.RESPONSE, problem_statement="run", answer="Deploy check"),
                msg_header=MessageHeader(from_agent_id="manager_1", to_routing_path="gateway_queue"),
                msg_meta=ada_saved.payload_meta.model_copy(deep=True),
            ),
            rendered_text="Deploy check",
            request_record=None,
        )
        assert ada_event.session_key == "session_ada_work"
        assert ada_event.chat_id == "tg_ada_work"

        # 7. Cross-person guard: bo's reply carrying ada's delivery_ref never resolves to ada's session.
        spoofed_meta = saved_one_shot.payload_meta.model_copy(deep=True)
        spoofed_meta.conversation_id = "conv_unknown"
        spoofed_meta.gateway_context.scheduled_job_id = "sched_spoofed_untracked"
        spoofed_meta.gateway_context.delivery_ref = refs.ref("session_ada_work")
        with pytest.raises(DeliveryPersonMismatch):
            projection.project(
                envelope=MessageEnvelope(
                    msg_content=Task(task_type=MessageMetaType.RESPONSE, problem_statement="run", answer="Nope"),
                    msg_header=MessageHeader(from_agent_id="manager_1", to_routing_path="gateway_queue"),
                    msg_meta=spoofed_meta,
                ),
                rendered_text="Nope",
                request_record=None,
            )
    finally:
        forget_task_context_keys()


@pytest.mark.asyncio
async def test_scheduled_runs_install_agent_person_system_kinds_and_refuse_owner_permits_and_missing_source(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """C32: permit-backed and per-run scheduled tasks install agent person system schedules
    (isolated_recovery_checkpoint, objective_work_session) when their backing schedule is active
    and vouched/system-managed, while owner permits, team-scope kinds, and missing source schedules
    are refused."""

    from custom_messages.contracts.schedule_v1 import ScheduledMessage
    from custom_messages.contracts.task_v1 import Task
    from custom_messages.enums import MessageMetaType, ScheduleManagementClass, SystemScheduleKind
    from custom_transport.protocol1.authority_tokens import root_task_token, schedule_permit_token
    from tests.broker_access_support import (
        broker_gateway_identity,
        broker_people_store,
        broker_person_ref,
        broker_scheduler_identity,
        forget_task_context_keys,
    )
    from tests.scheduled_run_support import _verified_team_frame, bind_test_scheduler

    forget_task_context_keys()
    try:
        people_store = broker_people_store()
        people_store.register(TEAM, "ada", roles=("owner",))
        store = ScheduleStore(db_path=str(tmp_path / "c32_system_kinds.db"))
        bound = bind_test_scheduler(monkeypatch, store=store, team_id=TEAM, people_store=people_store)

        now = datetime.now(timezone.utc)
        gw_id = broker_gateway_identity(TEAM)
        sched_id = broker_scheduler_identity(TEAM)
        ada_gw_token = root_task_token(
            gw_id, team=TEAM, root_task_id="task_ada_gw", request_id="req_ada_gw", person_ref=broker_person_ref("ada"), issued=now
        )

        async def _send(body: dict[str, Any], token: dict[str, Any]) -> dict[str, Any]:
            frame = _verified_team_frame("schedules.install", body, sender_agent="lead_1", authority=token, team=TEAM)
            return checked_reply("schedules.install", await bound.schedule_ops.handle(frame))

        def _ckpt_body(req_id: str, sid: str) -> dict[str, Any]:
            return {
                "request_id": req_id,
                "schedule_id": sid,
                "mode": "create",
                "name": f"isolated_recovery_checkpoint:task_orig:{sid}",
                "management_class": "system",
                "system_schedule_kind": "isolated_recovery_checkpoint",
                "timing": {"kind": "once", "at": "2030-01-01T00:10:00.000Z"},
                "repeat_count": 1,
                "enabled": True,
                "action": {"type": "task", "instruction": {"type": "inline", "text": "Checkpoint."}},
            }

        # Install a vouched objective_work_session from a gateway-signed request and obtain its standing permit.
        obj_body = {
            "request_id": "req-obj-1",
            "schedule_id": "objwork_sched_1",
            "mode": "create",
            "name": "objective_work_session:obj_1",
            "management_class": "system",
            "system_schedule_kind": "objective_work_session",
            "objective_id": "obj_1",
            "timing": {"kind": "interval", "interval_ms": 3_600_000},
            "repeat_count": 0,
            "enabled": True,
            "action": {"type": "task", "instruction": {"type": "inline", "text": "Resume objective."}},
        }
        created_obj = await _send(obj_body, ada_gw_token)
        assert created_obj["outcome"] == "ok"
        obj_permit = store.run_permit("objwork_sched_1")
        assert obj_permit is not None

        # 1. Permit-backed run of objective_work_session installs an isolated_recovery_checkpoint.
        ckpt_from_permit = await _send(
            _ckpt_body("req-ckpt-permit", "recov_ckpt_from_permit"),
            obj_permit.model_dump(mode="json", exclude_none=True),
        )
        assert ckpt_from_permit["outcome"] == "ok", ckpt_from_permit
        assert store.get_scheduled_message("recov_ckpt_from_permit") is not None
        assert store.run_permit("recov_ckpt_from_permit") is not None

        # 2. Scheduler-signed per-run token (run_context.sign) for an active schedule installs a checkpoint.
        run_token = root_task_token(
            sched_id,
            team=TEAM,
            root_task_id="run_obj_1",
            request_id="schedule_run_obj_1",
            person_ref=broker_person_ref("ada"),
            schedule_id="objwork_sched_1",
            issued=now,
        )
        ckpt_from_run = await _send(_ckpt_body("req-ckpt-run", "recov_ckpt_from_run"), run_token)
        assert ckpt_from_run["outcome"] == "ok", ckpt_from_run
        assert store.get_scheduled_message("recov_ckpt_from_run") is not None

        # 3. Permit-backed run CANNOT install team/maintenance system kinds (heartbeat_launcher).
        hb_refused = await _send(
            {
                "request_id": "req-hb-from-permit",
                "schedule_id": "sched_hb_from_permit",
                "mode": "create",
                "management_class": "system",
                "system_schedule_kind": "heartbeat_launcher",
                "timing": {"kind": "interval", "interval_ms": 600_000},
                "repeat_count": 0,
                "enabled": True,
                "action": {"type": "task", "instruction": {"type": "inline", "text": "Heartbeat."}},
            },
            obj_permit.model_dump(mode="json", exclude_none=True),
        )
        assert hb_refused["outcome"] == "not_allowed"
        assert store.get_scheduled_message("sched_hb_from_permit") is None

        # 4. Administrator owner permit (schedule_owner_permit:*) CANNOT install isolated_recovery_checkpoint
        #    or objective_work_session.
        maint = ScheduledMessage(
            schedule_id="sched_internal_imp_c32",
            schedule_name="internal_improvement:team",
            management_class=ScheduleManagementClass.SYSTEM,
            system_schedule_kind=SystemScheduleKind.INTERNAL_IMPROVEMENT,
            payload=Task(task_type=MessageMetaType.REQUEST, problem_statement="Maintain team."),
            payload_meta=MessageMeta(gateway_context=GatewayContext(canonical_user_id="ada")),
            interval_ms=3_600_000,
            repeat_count=None,
        )
        store.add(maint, claim_team_id=TEAM)
        owner_permit = schedule_permit_token(
            sched_id,
            team=TEAM,
            schedule_id="sched_internal_imp_c32",
            person_ref=broker_person_ref("ada"),
            owner_permit=True,
            issued=now,
        )
        owner_permit_ckpt = await _send(_ckpt_body("req-op-ckpt", "recov_ckpt_from_op"), owner_permit)
        assert owner_permit_ckpt["outcome"] == "not_allowed"
        assert store.get_scheduled_message("recov_ckpt_from_op") is None

        owner_permit_obj = await _send(
            {**obj_body, "request_id": "req-op-obj", "schedule_id": "objwork_from_op"},
            owner_permit,
        )
        assert owner_permit_obj["outcome"] == "not_allowed"
        assert store.get_scheduled_message("objwork_from_op") is None

        # 5. Per-run token whose source_schedule_id does not exist in the store is REFUSED.
        missing_source_token = root_task_token(
            sched_id,
            team=TEAM,
            root_task_id="run_missing_1",
            request_id="schedule_run_missing_1",
            person_ref=broker_person_ref("ada"),
            schedule_id="sched_does_not_exist",
            issued=now,
        )
        missing_refused = await _send(_ckpt_body("req-missing-src", "recov_ckpt_missing_src"), missing_source_token)
        assert missing_refused["outcome"] == "not_allowed"
        assert store.get_scheduled_message("recov_ckpt_missing_src") is None
    finally:
        forget_task_context_keys()
