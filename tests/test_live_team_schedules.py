"""Signed schedules.* requests over NATS, real paging and atomic stored changes."""

from __future__ import annotations

import asyncio
import logging
from contextlib import AsyncExitStack
from datetime import datetime, timezone
from pathlib import Path

import pytest

from access_control.people_directory import PeopleDirectory
from assistant_gateway.message_host import GATEWAY_RUNTIME, GatewayMessageHost, gateway_profile
from assistant_gateway.team_services_client import MessageHostTeamServices
from custom_agents.schedule_store import ScheduleStore
from custom_messages.contracts.access_v2 import PersonStanding, TeamPersonV1
from custom_messages.contracts.routing_v1 import GatewayContext, MessageMeta
from custom_messages.contracts.schedule_v1 import ScheduledMessage, ScheduleTaskMemory
from custom_messages.contracts.task_v1 import Task
from custom_messages.enums import MessageMetaType, ScheduleManagementClass
from custom_messages.wire.kinds import MAX_FRAME_BYTES
from custom_messages.wire.sizes import encoded_size
from custom_transport.protocol1.delivery_refs import DeliveryRefs
from custom_transport.protocol1.host_runtime import MessageHost
from runtime_settings.instance_layout import InstanceLayout
from storage.schedule_repository import RowWrite
from team_services.process import TEAM_SERVICES_RUNTIME, serve_team, team_services_profile
from team_services.schedules import ScheduleOperations
from tests.people_directory_support import access_store_at
from tests.test_live_team_services import TEAM, broker, connect, installation  # noqa: F401

pytestmark = [pytest.mark.integration, pytest.mark.live, pytest.mark.nats]
ADA = "p-AdaAdaAdaAdaAdaAdaAdaA"
BO = "p-" + "b" * 22


@pytest.mark.asyncio
async def test_signed_schedule_paging_and_controls(broker: str, tmp_path: Path) -> None:  # noqa: F811
    layout = InstanceLayout.at(tmp_path / "instance")
    stop = asyncio.Event()
    async with AsyncExitStack() as stack:
        await installation(stack, broker, layout, team_services_profile(TEAM), gateway_profile(TEAM))
        access = access_store_at(tmp_path)
        for person, reference in (("ada", ADA), ("bo", BO)):
            access.record_person(TeamPersonV1(team_id=TEAM, person_id=person, person_ref=reference,
                                              standing=PersonStanding.ACTIVE, roles=("member",),
                                              changed_at_utc=datetime.now(timezone.utc)))
        store = ScheduleStore(str(tmp_path / "manager.db"))
        for number in range(8):
            store.add(ScheduledMessage(
                schedule_id=f"sched_{number}", schedule_name="🙂" * 17_000,
                management_class=ScheduleManagementClass.USER, claim_team_id=TEAM, interval_ms=60_000,
                payload=Task(task_type=MessageMetaType.REQUEST, problem_statement="Check the feed."),
                payload_meta=MessageMeta(gateway_context=GatewayContext(canonical_user_id="ada", session_key="private-chat")),
                schedule_memory=ScheduleTaskMemory(),
            ))
        # A stored pause accepted by the old gateway remains listable and diagnosable on the wire.
        store._repository.write_locked("sched_1", lambda row: RowWrite(columns={"paused_until": "tomorrow"}))
        refs = DeliveryRefs.load(layout, TEAM)
        host = MessageHost(layout, team_services_profile(TEAM), await connect(stack, broker, TEAM_SERVICES_RUNTIME))
        schedules = ScheduleOperations(store, PeopleDirectory(access, TEAM), refs, lambda person: False,
                                        host.clock, logging.getLogger("test"))
        serving = asyncio.create_task(serve_team(host, TEAM, stop, [schedules.family()]))
        stack.push_async_callback(lambda: serving)
        stack.callback(stop.set)
        gateway = GatewayMessageHost(layout, TEAM)
        gateway.start(await connect(stack, broker, GATEWAY_RUNTIME))
        stack.push_async_callback(gateway.close)
        await gateway.ready(timeout=20)
        rows = []
        offset = 0
        while True:
            verified = await gateway.command("schedules.list", {"request_id": f"cmd-list-{offset}", "offset": offset,
                                               "limit": 50, "delivery_ref": refs.ref("private-chat")},
                                               person_ref=ADA, timeout=10)
            assert encoded_size(verified.raw) <= MAX_FRAME_BYTES
            assert "private-chat" not in str(verified.raw)
            reply = verified.raw["body"]
            assert reply["outcome"] == "ok"
            rows.extend(reply["schedules"])
            if "next_offset" not in reply:
                break
            offset = reply["next_offset"]
        assert [row["schedule_id"] for row in rows] == [f"sched_{n}" for n in range(8)]
        assert len(rows[0]["name"]) == 16_384 and "run_notes" in rows[0]
        assert rows[1]["invalid_time_fields"] == ["paused_until"]
        client = MessageHostTeamServices(gateway, timeout_seconds=10)

        async def command(action, seen, request_id, **extra):
            return await client.command(f"schedules.{action}", {"request_id": request_id, "schedule_id": "sched_0",
                                        "seen_version": seen, **extra}, person_ref=ADA, request_id=request_id)

        first = await command("pause", rows[0]["seen_version"], "cmd-pause", paused_until="2030-01-01T00:00:00.000Z")
        resumed = await command("resume", first["version"]["seen_version"], "cmd-resume")
        assert await command("pause", rows[0]["seen_version"], "cmd-pause", paused_until="2030-01-01T00:00:00.000Z") == first
        assert store.read_scheduled_message("sched_0").paused_until is None
        disabled = await command("disable", resumed["version"]["seen_version"], "cmd-disable")
        assert (await command("resume", rows[0]["seen_version"], "cmd-stale"))["outcome"] == "changed_since_read"
        removed = await command("remove", disabled["version"]["seen_version"], "cmd-remove")
        assert removed["outcome"] == "ok" and store.read_scheduled_message("sched_0") is None
        assert await command("remove", "a" * 64, "cmd-remove") == removed
        assert (await client.command("schedules.list", {"request_id": "cmd-bo", "offset": 0, "limit": 50},
                                     person_ref=BO, request_id="cmd-bo"))["schedules"] == []
