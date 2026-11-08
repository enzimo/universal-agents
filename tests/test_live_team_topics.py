"""Signed topic commands and retries over real NATS and the real conversation store."""

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
from custom_agents.conversation_store import ConversationStore
from custom_messages.contracts.access_v2 import PersonStanding, TeamPersonV1
from custom_transport.protocol1.host_runtime import MessageHost
from runtime_settings.instance_layout import InstanceLayout
from team_services.process import TEAM_SERVICES_RUNTIME, serve_team, team_services_profile
from team_services.topics import TopicOperations
from tests.people_directory_support import access_store_at
from tests.test_live_team_services import TEAM, broker, connect, installation  # noqa: F401 - shared fixture

pytestmark = [pytest.mark.integration, pytest.mark.live, pytest.mark.nats]
ADA = "p-AdaAdaAdaAdaAdaAdaAdaA"


@pytest.mark.asyncio
async def test_signed_topic_commands_and_retries(broker: str, tmp_path: Path) -> None:  # noqa: F811
    layout = InstanceLayout.at(tmp_path / "instance")
    stop = asyncio.Event()
    async with AsyncExitStack() as stack:
        await installation(stack, broker, layout, team_services_profile(TEAM), gateway_profile(TEAM))
        access = access_store_at(tmp_path)
        access.record_person(TeamPersonV1(team_id=TEAM, person_id="ada", person_ref=ADA,
                                          standing=PersonStanding.ACTIVE, roles=("member",),
                                          changed_at_utc=datetime.now(timezone.utc)))
        store = ConversationStore(str(tmp_path / "manager.db"), artifacts_dir=layout.conversation_artifacts_dir)
        store.ensure_thread(conversation_id="conv_ada", team_id=TEAM, owner_canonical_user_id="ada",
                            owner_agent_id="lead_1", owner_agent_name="TeamArchitect", requester_name="Ada",
                            requester_id="ada", root_task_id="task_ada", subject_preview="Taxes")
        host = MessageHost(layout, team_services_profile(TEAM), await connect(stack, broker, TEAM_SERVICES_RUNTIME))
        topics = TopicOperations(store, PeopleDirectory(access, TEAM), host.clock, logging.getLogger("test"))
        serving = asyncio.create_task(serve_team(host, TEAM, stop, [topics.family()]))
        stack.push_async_callback(lambda: serving)
        stack.callback(stop.set)
        gateway = GatewayMessageHost(layout, TEAM)
        gateway.start(await connect(stack, broker, GATEWAY_RUNTIME))
        stack.push_async_callback(gateway.close)
        await gateway.ready(timeout=20)
        client = MessageHostTeamServices(gateway, timeout_seconds=10)
        body = {"request_id": "cmd-tag", "conversation_id": "conv_ada", "tags": ["taxes"], "private": True}
        saved = await client.command("topics.tag_thread", body, person_ref=ADA, request_id="cmd-tag")
        store.untag_thread(conversation_id="conv_ada", tag="taxes")
        assert await client.command("topics.tag_thread", body, person_ref=ADA, request_id="cmd-tag") == saved
        assert store.list_thread_tags("conv_ada") == []
        assert saved == {"outcome": "ok", "applied_tags": ["taxes"]}
        await client.command("topics.tag_thread", {**body, "request_id": "cmd-new"}, person_ref=ADA, request_id="cmd-new")
        listed = await client.command("topics.list_tags", {"request_id": "cmd-list"}, person_ref=ADA, request_id="cmd-list")
        found = await client.command("topics.find_threads", {"request_id": "cmd-find", "tags": ["taxes"]},
                                     person_ref=ADA, request_id="cmd-find")
        assert [row["tag"] for row in listed["tags"]] == ["taxes"]
        assert [row["conversation_id"] for row in found["matches"]] == ["conv_ada"]
