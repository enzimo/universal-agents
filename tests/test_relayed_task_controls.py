"""What an agent answers for relayed people's commands (custom_agents.relayed_task_controls).

The relay must come from the team services with a gateway's command token, and the
token's person must be active in the gateway's copy of the people; the context dump
setting is the team lead's. tests/test_task_control_responsiveness.py and
tests/test_live_agent_hosts.py cover the task controls and the relay itself.
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

from custom_agents.relayed_task_controls import RELAYED_HANDLERS
from custom_messages.contracts.access_v2 import PersonStanding, TeamPersonV1
from tests.people_directory_support import access_store_at
from tests.team_services_support import checked_reply, relayed_frame, stand_in_frame

TEAM = "team_relay"
OWNER, GONE = "p-OwnerOwnerOwnerOwnerOw", "p-GoneGoneGoneGoneGoneGo"


@pytest.fixture
def agent(tmp_path: Path):
    access = access_store_at(tmp_path)
    now = datetime.now(timezone.utc)
    access.record_person(TeamPersonV1(team_id=TEAM, person_id="owner", person_ref=OWNER,
                                      standing=PersonStanding.ACTIVE, roles=("member",), changed_at_utc=now))
    access.record_person(TeamPersonV1(team_id=TEAM, person_id="gone", person_ref=GONE,
                                      standing=PersonStanding.REMOVED, changed_at_utc=now))
    applied = []
    return SimpleNamespace(
        id="lead_1", team_id=TEAM, is_team_lead=True, applied=applied,
        task_manager=SimpleNamespace(storage_settings=access.settings),
        apply_runtime_context_dump_settings=lambda **values: applied.append(values) or
        {"enabled": values["enabled"], "mode": values["mode"]},
    )


async def _ask(agent, kind: str, body: dict, frame) -> dict:
    return checked_reply(kind, await RELAYED_HANDLERS[kind](agent, frame))


@pytest.mark.asyncio
async def test_only_the_team_services_relay_and_only_for_an_active_person(agent) -> None:
    body = {"agent": "lead_1", "offset": 0, "limit": 20}
    straight = stand_in_frame(TEAM, "tasks.list", {"request_id": "cmd-1", **body}, person_ref=OWNER,
                              request_id="cmd-1")  # from the gateway itself, not relayed
    assert (await _ask(agent, "tasks.list", body, straight))["outcome"] == "not_allowed"
    for person in (GONE, "p-NobodyNobodyNobodyNob"):
        refused = await _ask(agent, "tasks.list", body, relayed_frame(TEAM, "tasks.list", body, person_ref=person))
        assert refused["outcome"] == "not_allowed"


@pytest.mark.asyncio
async def test_the_context_dump_is_the_team_leads_setting(agent) -> None:
    body = {"enabled": True, "mode": "always"}
    applied = await _ask(agent, "debug.dump", body, relayed_frame(TEAM, "debug.dump", body, person_ref=OWNER))
    assert applied == {"outcome": "ok", "enabled": True, "mode": "always"}
    assert agent.applied == [{"enabled": True, "mode": "always", "source": "person:owner"}]
    agent.is_team_lead = False
    refused = await _ask(agent, "debug.dump", body, relayed_frame(TEAM, "debug.dump", body, person_ref=OWNER))
    assert refused["outcome"] == "not_allowed"
