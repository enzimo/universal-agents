"""Cron schedules run in their owner's time zone (owner's decision 2026-09-27).

Covers: next run times computed in the schedule's zone, including across a
daylight saving change; a cron schedule without a zone is refused; the store
fills the owner's zone (the schedule's request time zone), else the instance
zone; old cron schedules get their zone once at scheduler start and move to
local time; updates keep, change or validate the zone; objective cadences
default to the owner's zone; and the supervisor gives every process ``TZ``
from ``user.timezone`` unless the environment sets it.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from custom_agents.schedule_changes import ScheduleUpdateV1, _updated_candidate
from custom_agents.schedule_store import ScheduleStore, compute_next_fire_at, fill_cron_timezone
from custom_messages.contracts.routing_v1 import MessageMeta
from custom_messages.contracts.schedule_v1 import ScheduledMessage
from custom_messages.contracts.task_v1 import Task
from custom_messages.enums import MessageMetaType, ScheduleManagementClass
from storage.schedule_repository import RowWrite
from custom_messages.records.objectives import OWNER_CADENCE_TIMEZONE, ObjectiveWorkBudget, ObjectiveWorkProfile

CHICAGO = "America/Chicago"
BERLIN = "Europe/Berlin"


def _cron_schedule(*, request_timezone: str | None = None, cron_timezone: str | None = None) -> ScheduledMessage:
    return ScheduledMessage(
        management_class=ScheduleManagementClass.USER,
        payload=Task(task_type=MessageMetaType.REQUEST, problem_statement="Morning summary"),
        payload_meta=MessageMeta(request_timezone=request_timezone) if request_timezone else None,
        cron_expression="0 9 * * *",
        cron_timezone=cron_timezone,
        repeat_count=None,
    )


def test_cron_runs_at_the_local_time_of_its_zone_across_daylight_saving() -> None:
    summer = datetime(2026, 9, 27, 12, 0, tzinfo=timezone.utc)
    after_the_change = datetime(2026, 11, 1, 12, 0, tzinfo=timezone.utc)

    assert compute_next_fire_at(cron_expression="0 9 * * *", from_time=summer, cron_timezone=CHICAGO) == (
        "2026-09-27T14:00:00+00:00"
    )
    assert compute_next_fire_at(
        cron_expression="0 9 * * *", from_time=after_the_change, cron_timezone=CHICAGO
    ) == ("2026-11-01T15:00:00+00:00")
    assert compute_next_fire_at(cron_expression="0 9 * * *", from_time=summer, cron_timezone="UTC") == (
        "2026-09-28T09:00:00+00:00"
    )


def test_a_cron_schedule_without_a_time_zone_is_refused() -> None:
    with pytest.raises(ValueError, match="has no time zone"):
        compute_next_fire_at(cron_expression="0 9 * * *")
    with pytest.raises(ValueError, match="IANA time zone"):
        _cron_schedule(cron_timezone="Mars/Olympus")


def test_the_owners_zone_comes_first_then_the_instance_zone() -> None:
    owned = _cron_schedule(request_timezone=BERLIN)
    system = _cron_schedule()
    explicit = _cron_schedule(request_timezone=BERLIN, cron_timezone="Asia/Tokyo")

    assert fill_cron_timezone(owned, CHICAGO) == BERLIN
    assert fill_cron_timezone(system, CHICAGO) == CHICAGO
    assert fill_cron_timezone(explicit, CHICAGO) is None
    assert explicit.cron_timezone == "Asia/Tokyo"
    with pytest.raises(ValueError, match="neither its owner nor the instance"):
        fill_cron_timezone(_cron_schedule(), None)


def test_the_store_saves_the_owners_zone_and_uses_it_after_each_run(tmp_path: Path) -> None:
    store = ScheduleStore(db_path=str(tmp_path / "schedules.db"), default_cron_timezone=CHICAGO)
    schedule = _cron_schedule(request_timezone=BERLIN)

    store.add(schedule)
    store.update_after_fire(schedule.schedule_id)

    saved = store.get_scheduled_message(schedule.schedule_id)
    assert saved is not None and saved.cron_timezone == BERLIN
    next_run = datetime.fromisoformat(store.get_row(schedule.schedule_id)["next_fire_at"])
    assert next_run.astimezone(ZoneInfo(BERLIN)).hour == 9


def test_old_cron_schedules_get_their_owners_zone_once_at_scheduler_start(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    store = ScheduleStore(db_path=str(tmp_path / "schedules.db"), default_cron_timezone=CHICAGO)
    old = _cron_schedule(request_timezone=BERLIN, cron_timezone="UTC")
    store.add(old)
    # Saved as before this change: no time zone, run time in UTC.
    row = store.get_row(old.schedule_id)
    legacy = json.loads(row["schedule_json"])
    legacy.pop("cron_timezone")
    store._repository.write_locked(
        old.schedule_id, lambda _row: RowWrite(columns={"schedule_json": json.dumps(legacy)})
    )
    interval = ScheduledMessage(
        management_class=ScheduleManagementClass.USER,
        payload=Task(task_type=MessageMetaType.REQUEST, problem_statement="Every hour"),
        interval_ms=3_600_000,
    )
    store.add(interval)

    with caplog.at_level(logging.WARNING):
        filled = store.fill_missing_cron_timezones()
    again = store.fill_missing_cron_timezones()

    assert filled == [(old.schedule_id, BERLIN)]
    assert again == []
    saved = store.get_scheduled_message(old.schedule_id)
    assert saved is not None and saved.cron_timezone == BERLIN
    next_run = datetime.fromisoformat(store.get_row(old.schedule_id)["next_fire_at"])
    assert next_run.astimezone(ZoneInfo(BERLIN)).hour == 9
    assert "event=schedule_cron_timezone_filled" in caplog.text


def test_an_update_keeps_changes_or_refuses_the_zone() -> None:
    schedule = _cron_schedule(cron_timezone=CHICAGO)

    new_cron, _ = _updated_candidate(schedule, ScheduleUpdateV1(cron_expression="30 7 * * 1-5"), BERLIN)
    new_zone, changes = _updated_candidate(schedule, ScheduleUpdateV1(cron_timezone=BERLIN), CHICAGO)

    assert new_cron.cron_timezone == CHICAGO
    assert new_zone.cron_timezone == BERLIN and changes["cron_timezone"] == BERLIN
    assert datetime.fromisoformat(new_zone.next_fire_at).astimezone(ZoneInfo(BERLIN)).hour == 9
    with pytest.raises(ValueError, match="IANA time zone"):
        ScheduleUpdateV1(cron_timezone="Nowhere/Land")
    with pytest.raises(ValueError, match="not interval ones"):
        ScheduleUpdateV1(cron_timezone=BERLIN, interval_seconds=60)


def test_objective_cadences_default_to_the_owners_zone_and_accept_an_iana_name() -> None:
    budget = ObjectiveWorkBudget(
        active_seconds=600,
        checkpoint_reserve_seconds=60,
        checkpoint_reserve_turns=1,
        checkpoint_reserve_tokens=1000,
        checkpoint_reserve_tool_calls=1,
        turns=10,
        total_tokens=10000,
        tool_calls=10,
    )

    assert ObjectiveWorkProfile(cadence_cron="0 3 * * *", budget=budget).cadence_timezone == OWNER_CADENCE_TIMEZONE
    assert ObjectiveWorkProfile(cadence_cron="0 3 * * *", cadence_timezone="Owner", budget=budget).cadence_timezone == (
        OWNER_CADENCE_TIMEZONE
    )
    assert ObjectiveWorkProfile(cadence_cron="0 3 * * *", cadence_timezone=BERLIN, budget=budget).cadence_timezone == BERLIN
    with pytest.raises(ValueError, match="'owner' or an IANA time zone"):
        ObjectiveWorkProfile(cadence_cron="0 3 * * *", cadence_timezone="local", budget=budget)


def test_every_process_gets_the_instance_zone_unless_the_environment_sets_tz(tmp_path: Path) -> None:
    from assistant_gateway.config import GatewayInstanceConfig
    from docker.launch_headless import build_process_env

    root = tmp_path / "instance"
    config = GatewayInstanceConfig.create(
        instance_name="tz-test", instance_root=str(root), team_id="team_tz", timezone=BERLIN
    )
    config.save(root / "instance_config.yaml")

    assert build_process_env(config, env_path=None, base_env={})["TZ"] == BERLIN
    assert build_process_env(config, env_path=None, base_env={"TZ": CHICAGO})["TZ"] == CHICAGO


@pytest.mark.asyncio
async def test_a_schedule_made_in_chat_carries_the_persons_own_zone() -> None:
    from types import SimpleNamespace

    from assistant_gateway.application.schedule_management import (
        GatewayScheduleApplication,
        GatewayScheduleCreateRequest,
        GatewayScheduleSettings,
    )
    from custom_messages.contracts.routing_v1 import GatewayContext

    session = SimpleNamespace(
        session_key="session_ann", conversation_id="conv_ann", channel_name="web", user_id="ann",
        chat_id=None, thread_id=None, owner_canonical_user_id="usr_ann",
    )
    published = []

    async def publish_durable(envelope, *, routing_key):
        published.append(envelope)

    ids = iter(["sched_1", "corr_1", "conv_sched_1"])
    application = GatewayScheduleApplication(
        settings=GatewayScheduleSettings(
            instance_name="tz", response_route="gateway", user_display_name="Ann", request_timezone=CHICAGO,
            default_session_key="session_ann", default_owner_canonical_user_id="usr_ann",
            manager_unavailable_text="unavailable",
        ),
        sessions=SimpleNamespace(resolve_session=lambda **_kwargs: session),
        request_context=SimpleNamespace(
            canonical_user_id=lambda route: "usr_ann",
            users_enabled=True,
            requester_presentation_for_session=lambda session, route: SimpleNamespace(timezone=BERLIN),
            build_gateway_context=lambda **_kwargs: GatewayContext(canonical_user_id="usr_ann"),
        ),
        delivery=SimpleNamespace(register_scheduled_request=lambda **_kwargs: None),
        manager=SimpleNamespace(manager_id="manager", publish_durable=publish_durable),
        manager_commands=None,
        id_generator=SimpleNamespace(
            new_scheduled_job_id=lambda: next(ids),
            new_correlation_id=lambda: next(ids),
            new_conversation_id=lambda: next(ids),
        ),
    )

    await application.add(GatewayScheduleCreateRequest(name="standup", instruction="Standup notes", cron_expression="0 9 * * 1-5"))

    [envelope] = published
    schedule = envelope.msg_content
    assert schedule.payload_meta.request_timezone == BERLIN
    assert fill_cron_timezone(schedule, CHICAGO) == BERLIN
