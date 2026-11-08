"""Schedule commands against real stores: failures, retries, paging and recoverable old pause values."""

from __future__ import annotations

import asyncio
import logging
from types import SimpleNamespace
from threading import Barrier, Event
from concurrent.futures import ThreadPoolExecutor

import pytest

from assistant_gateway.application.commands.context import GatewayActorContext
from assistant_gateway.application.commands.manager_control import ManagerControlCommandRequest
from assistant_gateway.application.manager_control_command_service import GatewayManagerControlCommandService
from assistant_gateway.application.schedule_management import ScheduleControlRefused
from assistant_gateway.application.route_context import GatewayRouteContextV1
from assistant_gateway.application.website_services import WebsiteOperationsApplication
from assistant_gateway.application.team_commands import TeamServicesUnavailable
from assistant_gateway.config import GatewayInstanceConfig
from access_control.people_directory import PeopleDirectory
from custom_messages.wire.sizes import REPLY_BODY_MAX_BYTES, encoded_size
from storage.operation_outcomes import CommandOutcome
from storage.schedule_repository import RowWrite
from tests.team_services_support import stand_in_frame
from tests.test_team_schedules import LATER, TEAM, Team, _gateway, reference_of
from team_services.schedules import SystemScheduleCheck
from tests.change_approval_support import CEDAR_BINARY, needs_cedar

pytest_plugins = ["tests.test_team_schedules"]


def _fail(*args, **kwargs):
    raise OSError("injected storage failure")


@pytest.mark.parametrize("fault", ["read", "write", "outcome_save"])
def test_storage_failure_is_unstored_and_the_same_command_succeeds_after_recovery(team: Team, monkeypatch, fault) -> None:
    team.add("sched_a", "ada")
    body = {"schedule_id": "sched_a", "seen_version": team.version("sched_a")}
    with monkeypatch.context() as patch:
        if fault == "read":
            patch.setattr(team.store, "read_scheduled_message", _fail)
        elif fault == "write":
            patch.setattr(team.store._repository, "_apply_write", _fail)
        else:
            patch.setattr(CommandOutcome, "save", _fail)
        refused = team.ask("schedules.disable", body)
    assert refused["outcome"] == "runtime_error"
    assert team.store.read_scheduled_message("sched_a").enabled is True
    assert team.ask("schedules.disable", body)["outcome"] == "ok"
    assert team.store.read_scheduled_message("sched_a").enabled is False


def test_a_list_read_error_is_not_an_empty_success(team: Team, monkeypatch) -> None:
    team.add("sched_a", "ada")
    monkeypatch.setattr(team.store._repository, "get_all", _fail)
    assert team.listed()["outcome"] == "runtime_error"


@pytest.mark.parametrize("value", ["tomorrow", "2030-01-01T09:30:00"])
def test_new_pause_values_must_be_valid_times_with_an_offset(team: Team, value: str) -> None:
    team.add("sched_a", "ada")
    with pytest.raises(ValueError):
        team.store.set_paused_until("sched_a", value)
    assert team.store.read_scheduled_message("sched_a").paused_until is None


def test_a_failed_refusal_save_asks_for_a_retry(team: Team, monkeypatch) -> None:
    with monkeypatch.context() as patch:
        patch.setattr(team.store.command_outcomes, "save_command_refusal", _fail)
        reply = team.ask("schedules.remove", {"schedule_id": "missing", "seen_version": "a" * 64})
    assert reply["outcome"] == "runtime_error"
    assert team.ask("schedules.remove", {"schedule_id": "missing", "seen_version": "a" * 64})["outcome"] == "not_found"


def test_a_lost_commit_acknowledgement_returns_the_stored_success(team: Team, monkeypatch) -> None:
    team.add("sched_a", "ada")
    body = {"schedule_id": "sched_a", "seen_version": team.version("sched_a")}
    original = team.store._repository.write_locked

    def written_then_lost(*args, **kwargs):
        original(*args, **kwargs)
        raise OSError("commit acknowledgement lost")

    with monkeypatch.context() as patch:
        patch.setattr(team.store._repository, "write_locked", written_then_lost)
        reply = team.ask("schedules.remove", body)
    assert reply["outcome"] == "ok"
    assert team.ask("schedules.remove", body) == reply


def test_the_transaction_itself_answers_a_duplicate_before_checking_a_stale_version(team: Team) -> None:
    schedule = team.add("sched_a", "ada")
    body = {"schedule_id": "sched_a", "seen_version": team.version("sched_a")}
    frame = stand_in_frame(TEAM, "schedules.disable", {"request_id": "cmd-1", **body},
                           person_ref=reference_of("ada"), request_id="cmd-1")
    first = team.operations.answer(frame)
    # Skip only CommandOnce's optimistic first lookup; the transaction must still guard the command.
    original = team.store.command_outcomes.stored_command_outcome
    team.store.command_outcomes.stored_command_outcome = lambda outcome: None
    try:
        assert team.operations.answer(frame) == first
    finally:
        team.store.command_outcomes.stored_command_outcome = original
    assert schedule.enabled is True and team.store.read_scheduled_message("sched_a").enabled is False


def test_another_person_cannot_read_a_stored_reply_under_a_reused_id(team: Team) -> None:
    team.add("sched_a", "ada")
    body = {"schedule_id": "sched_a", "seen_version": team.version("sched_a")}
    assert team.ask("schedules.remove", body)["outcome"] == "ok"
    assert team.ask("schedules.remove", body, person="bo")["outcome"] == "not_allowed"


def test_concurrent_reuse_of_one_request_id_cannot_change_two_schedules(team: Team) -> None:
    team.add("sched_a", "ada")
    team.add("sched_b", "ada")
    bodies = [{"schedule_id": name, "seen_version": team.version(name)} for name in ("sched_a", "sched_b")]
    start = Barrier(2)

    def disable(body):
        start.wait(timeout=5)
        return team.ask("schedules.disable", body, request_id="cmd-raced")

    with ThreadPoolExecutor(max_workers=2) as pool:
        replies = list(pool.map(disable, bodies))
    assert replies[0] == replies[1]
    assert sum(not team.store.read_scheduled_message(name).enabled for name in ("sched_a", "sched_b")) == 1


def test_a_scheduler_advance_during_a_system_decision_is_a_logged_permit_refusal(team: Team, caplog) -> None:
    team.add("hb_sched_olu", "olu", system=True)
    seen = team.version("hb_sched_olu")

    def advance_then_allow(person):
        team.store._repository.write_locked("hb_sched_olu", lambda row: RowWrite(columns={"next_fire_at": LATER}))
        return True

    team.may_change_system = advance_then_allow
    with caplog.at_level(logging.WARNING):
        reply = team.ask("schedules.disable", {"schedule_id": "hb_sched_olu", "seen_version": seen}, person="olu")
    assert team.version("hb_sched_olu") == seen
    assert reply["outcome"] == "not_allowed"
    assert "event=team_schedule_change_refused" in caplog.text
    assert "Refresh and try again" in reply["message"] and "digest" not in reply["message"]
    assert team.store.read_scheduled_message("hb_sched_olu").enabled is True


def test_scheduler_advances_do_not_move_schedules_between_pages(team: Team) -> None:
    for number in range(51):
        team.add(f"sched_{number:02}", "ada")
    first = team.listed()
    team.store._repository.write_locked("sched_00", lambda row: RowWrite(columns={"next_fire_at": LATER}))
    second = team.listed(offset=first["next_offset"])
    assert [row["schedule_id"] for row in first["schedules"] + second["schedules"]] == [f"sched_{n:02}" for n in range(51)]


def test_list_pages_fit_in_a_frame_with_unicode_and_oversized_names(team: Team, caplog) -> None:
    for number in range(8):
        team.add(f"sched_{number}", "ada")
        team.store._repository.write_locked(f"sched_{number}", lambda row: RowWrite(columns={"schedule_name": "🙂" * 17_000}))
    seen = []
    offset = 0
    with caplog.at_level(logging.WARNING):
        while True:
            reply = team.listed(offset=offset)
            assert reply["outcome"] == "ok" and encoded_size(reply) <= REPLY_BODY_MAX_BYTES
            assert all(len(row["name"]) == 16_384 for row in reply["schedules"])
            seen.extend(row["schedule_id"] for row in reply["schedules"])
            if "next_offset" not in reply:
                break
            offset = reply["next_offset"]
    assert seen == [f"sched_{n}" for n in range(8)]
    assert "event=team_schedule_text_shortened" in caplog.text


@pytest.mark.asyncio
async def test_a_saved_invalid_pause_can_be_listed_and_resumed_without_repairing_storage_on_read(team: Team, caplog) -> None:
    team.add("sched_a", "ada")
    team.store._repository.write_locked("sched_a", lambda row: RowWrite(columns={"paused_until": "tomorrow"}))
    with caplog.at_level(logging.WARNING):
        row, = team.listed()["schedules"]
    assert "paused_until" not in row
    assert team.store.read_scheduled_message("sched_a").paused_until == "tomorrow"
    assert "event=team_schedule_invalid_time" in caplog.text
    gateway = _gateway(team)
    website = WebsiteOperationsApplication(team=None, mcp=None, event_output=None, operational_controls=None,
                                           schedules=gateway, memory_operations=None, workspace_browser=None,
                                           team_commands=None)
    card, = (await website.list_web_chat_schedules(
        GatewayRouteContextV1(session_key="session_ada_a", canonical_user_id="ada"),
    )).schedules
    assert card.available_actions == ("resume", "remove")
    assert team.ask("schedules.resume", {"schedule_id": "sched_a", "seen_version": row["seen_version"]})["outcome"] == "ok"
    assert team.store.read_scheduled_message("sched_a").paused_until is None


@pytest.mark.asyncio
@pytest.mark.parametrize("cancellations", [1, 2, 3])
async def test_every_cancellation_joins_an_already_started_write(team: Team, monkeypatch, cancellations: int) -> None:
    team.add("sched_a", "ada")
    body = {"schedule_id": "sched_a", "seen_version": team.version("sched_a")}
    frame = stand_in_frame(TEAM, "schedules.disable", {"request_id": "cmd-cancel-write", **body},
                           person_ref=reference_of("ada"), request_id="cmd-cancel-write")
    entered, release = Event(), Event()
    original = team.store._repository.write_locked

    def held_write(*args, **kwargs):
        entered.set()
        assert release.wait(5)
        return original(*args, **kwargs)

    monkeypatch.setattr(team.store._repository, "write_locked", held_write)
    handling = asyncio.create_task(team.operations.handle(frame))
    try:
        assert await asyncio.to_thread(entered.wait, 5)
        for _ in range(cancellations):
            handling.cancel()
            await asyncio.sleep(0)
            await asyncio.sleep(0)
            assert not handling.done(), "the handler ended while its write could still commit"
    finally:
        release.set()
        await asyncio.gather(handling, return_exceptions=True)
    assert handling.cancelled()
    assert team.store.read_scheduled_message("sched_a").enabled is False
    assert team.ask("schedules.disable", body, request_id="cmd-cancel-write")["outcome"] == "ok"


def test_a_command_never_lists_or_changes_another_teams_schedule(team: Team) -> None:
    team.add("sched_other", "ada")
    team.store._repository.write_locked("sched_other", lambda row: RowWrite(columns={"claim_team_id": "team_other"}))
    assert team.listed()["schedules"] == []
    assert team.ask("schedules.remove", {"schedule_id": "sched_other", "seen_version": team.version("sched_other")})["outcome"] == "not_found"


@pytest.mark.asyncio
async def test_chat_list_errors_and_change_refusals_keep_their_explanations(team: Team, monkeypatch) -> None:
    team.add("sched_a", "ada")
    gateway = _gateway(team)
    service = GatewayManagerControlCommandService(None, schedules=gateway)

    async def request(kind, body, **kwargs):
        return {"outcome": "runtime_error", "message": "The schedule store is unavailable."}

    monkeypatch.setattr(gateway._team_commands, "request", request)
    with pytest.raises(TeamServicesUnavailable, match="store is unavailable"):
        await gateway.list(owner_wide=True)
    actor = GatewayActorContext(channel_name="cli", user_id="ada", canonical_user_id="ada")
    result = await service.execute(ManagerControlCommandRequest(command="/schedules", current_session_key="session_ada_a", actor=actor))
    assert result.text == "The schedule store is unavailable."
    with pytest.raises(ScheduleControlRefused, match="store is unavailable") as refused:
        await gateway.disable("sched_a", expected_record_digest=team.version("sched_a"))
    assert refused.value.outcome == "runtime_error"


@pytest.mark.asyncio
async def test_invalid_pause_text_is_answered_before_a_command_is_sent_and_local_time_uses_the_persons_zone(team: Team) -> None:
    team.add("sched_a", "ada")
    gateway = _gateway(team)
    with pytest.raises(ScheduleControlRefused, match="Use a pause time"):
        await gateway.pause("sched_a", paused_until="tomorrow")
    gateway._request_context.requester_presentation_for_session = lambda *args: SimpleNamespace(timezone="America/Chicago")
    assert await gateway.pause("sched_a", paused_until="2030-01-01T09:30:00")
    assert team.store.read_scheduled_message("sched_a").paused_until == "2030-01-01T15:30:00.000Z"


@pytest.mark.asyncio
async def test_cancelling_during_a_slow_permission_lookup_joins_it_without_a_write(team: Team) -> None:
    team.add("hb_sched_olu", "olu", system=True)
    entered, release = Event(), Event()

    def slow(person):
        entered.set()
        assert release.wait(5)
        return True

    team.may_change_system = slow
    frame = stand_in_frame(TEAM, "schedules.disable", {"request_id": "cmd-cancel", "schedule_id": "hb_sched_olu",
                           "seen_version": team.version("hb_sched_olu")}, person_ref=reference_of("olu"), request_id="cmd-cancel")
    handling = asyncio.create_task(team.operations.handle(frame))
    try:
        assert await asyncio.to_thread(entered.wait, 5)
        handling.cancel()
        await asyncio.sleep(0)
        assert not handling.done()
    finally:
        release.set()
    with pytest.raises(asyncio.CancelledError):
        await handling
    assert team.store.read_scheduled_message("hb_sched_olu").enabled is True


@needs_cedar
@pytest.mark.asyncio
async def test_real_cedar_decides_the_handlers_concurrent_system_changes_and_is_closed(team: Team, tmp_path, monkeypatch) -> None:
    from assistant_gateway import access_setup

    config = GatewayInstanceConfig.create(instance_name="schedules", instance_root=str(tmp_path / "instance"),
                                           team_id=TEAM, timezone="UTC")
    config.authorization.enabled = True
    config.authorization.cedar_binary_path = str(CEDAR_BINARY)
    opened = []
    original = access_setup.open_access_service

    def open_once(**kwargs):
        service = original(**kwargs)
        opened.append(service)
        return service

    monkeypatch.setattr(access_setup, "open_access_service", open_once)
    check = SystemScheduleCheck(config, PeopleDirectory(team.access, TEAM))
    team.operations._may_change_system = check
    team.add("hb_member", "ada", system=True)
    team.add("hb_admin", "olu", system=True)
    frames = [stand_in_frame(TEAM, "schedules.disable", {"request_id": f"cmd-{name}", "schedule_id": name,
              "seen_version": team.version(name)}, person_ref=reference_of(person), request_id=f"cmd-{name}")
              for name, person in (("hb_member", "ada"), ("hb_admin", "olu"))]
    try:
        refused, allowed = await asyncio.gather(*(team.operations.handle(frame) for frame in frames))
        assert refused["outcome"] == "not_allowed" and allowed["outcome"] == "ok"
        assert len(opened) == 1
    finally:
        team.operations.close()
    assert check._check._service is None
