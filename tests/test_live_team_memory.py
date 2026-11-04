"""A person's memory commands over NATS: gateway host → team services, signed and verified both ways.

The installation authority, the team services (with their memory handlers on a real
SQLite database) and the gateway's host run on nats-server 2.15.1. The gateway's command
token names the person by reference; the team services read who that is from the access
store. team_services.memory, assistant_gateway.message_host and
assistant_gateway.team_services_client.
"""

from __future__ import annotations

import asyncio
import logging
from contextlib import AsyncExitStack
from datetime import datetime, timezone
from pathlib import Path

import pytest

from access_control.people_directory import PeopleDirectory
from assistant_gateway.application.team_commands import TeamCommandRefused
from assistant_gateway.message_host import GATEWAY_RUNTIME, GatewayMessageHost, gateway_profile
from assistant_gateway.team_services_client import MessageHostTeamServices
from custom_messages.contracts.access_v2 import PersonStanding, TeamPersonV1
from custom_transport.protocol1.host_runtime import MessageHost
from storage.config import resolve_storage_settings
from storage.pinned_memory_repository import create_pinned_memory_repository
from team_services.memory import MemoryOperations
from team_services.process import TEAM_SERVICES_RUNTIME, serve_team, team_services_profile
from tests.people_directory_support import access_store_at
from tests.test_live_team_services import TEAM, broker, connect, installation  # noqa: F401 - broker is a fixture

pytestmark = [pytest.mark.integration, pytest.mark.live, pytest.mark.nats]

ADA = "p-AdaAdaAdaAdaAdaAdaAdaA"


async def started_team(stack: AsyncExitStack, url: str, tmp_path: Path, stop: asyncio.Event) -> MessageHostTeamServices:
    layout_root = tmp_path / "instance"
    from runtime_settings.instance_layout import InstanceLayout

    layout = InstanceLayout.at(layout_root)
    await installation(stack, url, layout, team_services_profile(TEAM), gateway_profile(TEAM))
    access = access_store_at(tmp_path)
    access.record_person(TeamPersonV1(team_id=TEAM, person_id="Ada Admin", person_ref=ADA,
                                      standing=PersonStanding.ACTIVE, roles=("admin",),
                                      changed_at_utc=datetime.now(timezone.utc)))
    repository = create_pinned_memory_repository(
        resolve_storage_settings({"backend": "sqlite", "sqlite": {"path": str(tmp_path / "manager.db")}}))
    host = MessageHost(layout, team_services_profile(TEAM), await connect(stack, url, TEAM_SERVICES_RUNTIME))
    memory = MemoryOperations(repository, PeopleDirectory(access, TEAM), {}, host.clock, logging.getLogger("test"))
    serving = asyncio.create_task(serve_team(host, TEAM, stop, [memory.handlers()]))
    stack.push_async_callback(lambda: serving)
    stack.callback(stop.set)
    gateway = GatewayMessageHost(layout, TEAM)
    gateway.start(await connect(stack, url, GATEWAY_RUNTIME))
    stack.push_async_callback(gateway.close)
    await gateway.ready(timeout=20)
    return MessageHostTeamServices(gateway, timeout_seconds=10)


@pytest.mark.asyncio
async def test_a_persons_memory_commands_run_at_the_team_services(
    broker: str, tmp_path: Path,  # noqa: F811 - the shared live NATS fixture
) -> None:
    stop = asyncio.Event()
    async with AsyncExitStack() as stack:
        team = await started_team(stack, broker, tmp_path, stop)
        body = {"request_id": "cmd-1", "bank": "usermem", "fact_text": {"type": "inline", "text": "Likes tea."}}
        saved = await team.command("memory.create", body, person_ref=ADA, request_id="cmd-1")
        # The gateway retries the same command: the team services answer it again and change nothing.
        again = await team.command("memory.create", body, person_ref=ADA, request_id="cmd-1")
        listed = await team.command("memory.list", {"request_id": "cmd-2", "bank": "usermem", "sort": "memory_id",
                                                    "offset": 0, "limit": 10}, person_ref=ADA, request_id="cmd-2")
        stranger = await team.command("memory.list", {"request_id": "cmd-3", "bank": "usermem", "sort": "newest",
                                                      "offset": 0, "limit": 10},
                                      person_ref="p-NobodyNobodyNobodyNob", request_id="cmd-3")
        with pytest.raises(TeamCommandRefused) as refused:
            await team.command("knowledge.list", {"request_id": "cmd-4", "space": "team", "status": "active",
                                                  "offset": 0, "limit": 10}, person_ref=ADA, request_id="cmd-4")

    assert saved["outcome"] == "ok" and saved["fact"]["fact_text"]["text"] == "Likes tea."
    assert again == saved
    assert [fact["fact_text"]["text"] for fact in listed["facts"]] == ["Likes tea."] and listed["total"] == 1
    assert stranger["outcome"] == "not_allowed"
    # Families not moved yet are answered at once, not left to time out.
    assert refused.value.code == "unsupported"
