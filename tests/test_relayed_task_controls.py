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
    refused = await _ask(agent, "debug.dump", body, relayed_frame(TEAM, "debug.dump", body, person_ref=OWNER,
                                                                request_id="other-command"))
    assert refused["outcome"] == "not_allowed"


@pytest.mark.asyncio
async def test_relay_checks_reuse_the_access_store_but_read_current_person_standing(agent, monkeypatch) -> None:
    from custom_agents import task_context_runtime

    task_context_runtime._access_store.cache_clear()
    original = task_context_runtime.AccessRepository
    opened = []

    def access_store(settings):
        store = original(settings)
        opened.append(store)
        return store

    monkeypatch.setattr(task_context_runtime, "AccessRepository", access_store)
    body = {"enabled": True, "mode": "always"}
    try:
        assert (await RELAYED_HANDLERS["debug.dump"](
            agent, relayed_frame(TEAM, "debug.dump", body, person_ref=OWNER)))["outcome"] == "ok"
        opened[0].record_person(TeamPersonV1(team_id=TEAM, person_id="owner", person_ref=OWNER,
            standing=PersonStanding.DISABLED, changed_at_utc=datetime.now(timezone.utc)))
        refused = await RELAYED_HANDLERS["debug.dump"](
            agent, relayed_frame(TEAM, "debug.dump", body, person_ref=OWNER, request_id="after-disable"))
        assert refused["outcome"] == "not_allowed"
        assert len(opened) == len(agent.applied) == 1
    finally:
        task_context_runtime._access_store.cache_clear()


@pytest.mark.asyncio
async def test_finished_history_pages_fit_encoded_frames_without_skipping_multibyte_results(agent, tmp_path) -> None:
    from custom_agents.task_manager import TaskManager
    from custom_messages.contracts.task_v1 import Task
    from custom_messages.enums import MessageMetaType
    from tests.team_services_support import checked_reply
    from custom_messages.wire.sizes import encoded_size, REPLY_BODY_MAX_BYTES

    manager = TaskManager(storage_settings=agent.task_manager.storage_settings)
    agent.task_manager = manager
    for i in range(12):
        manager.task_archive_repository.save_completed_task(
            archive_id=f"finished-{i:02}", task_id=f"task-{i}", owner_agent_id="stopped",
            owner_agent_name="Worker", team_id=TEAM, owner_canonical_user_id="owner", status="completed",
            task_json=Task(task_id=f"task-{i}", task_type=MessageMetaType.RESPONSE,
                           problem_statement="😀" * 2000, answer="😀" * 12000).model_dump_json())
    offset, ids, sizes = 0, [], []
    while offset < 12:
        body = {"agent": agent.id, "offset": offset, "limit": 10}
        reply = checked_reply("tasks.list_finished", await RELAYED_HANDLERS["tasks.list_finished"](
            agent, relayed_frame(TEAM, "tasks.list_finished", body, person_ref=OWNER)))
        assert reply["tasks"]
        ids.extend(row["task_id"] for row in reply["tasks"])
        sizes.append(encoded_size(reply))
        assert all(row["answer"] == "😀" * 12000 for row in reply["tasks"])
        offset = reply.get("next_offset", 12)
    assert len(ids) == len(set(ids)) == 12
    assert len(sizes) >= 3 and max(sizes) <= REPLY_BODY_MAX_BYTES


@pytest.mark.asyncio
async def test_repeating_an_older_context_dump_command_never_overrides_a_newer_one(agent) -> None:
    on = relayed_frame(TEAM, "debug.dump", {"enabled": True, "mode": "always"}, person_ref=OWNER, request_id="turn-on")
    off = relayed_frame(TEAM, "debug.dump", {"enabled": False, "mode": "always"}, person_ref=OWNER, request_id="turn-off")
    first = await RELAYED_HANDLERS["debug.dump"](agent, on)
    await RELAYED_HANDLERS["debug.dump"](agent, off)
    del agent._relayed_command_outcomes  # reopen from storage, as after process recovery
    repeated = await RELAYED_HANDLERS["debug.dump"](agent, on)
    assert first == repeated == {"outcome": "ok", "enabled": True, "mode": "always"}
    assert [change["enabled"] for change in agent.applied] == [True, False]


@pytest.mark.asyncio
async def test_an_admitted_command_without_a_final_reply_is_not_run_twice(agent, monkeypatch) -> None:
    from storage.relayed_command_outcomes import RelayedCommandOutcomes

    frame = relayed_frame(TEAM, "debug.dump", {"enabled": True, "mode": "always"}, person_ref=OWNER, request_id="lost-answer")
    real_complete = RelayedCommandOutcomes.complete
    monkeypatch.setattr(RelayedCommandOutcomes, "complete", lambda *args: (_ for _ in ()).throw(RuntimeError("commit failed")))
    with pytest.raises(RuntimeError, match="commit failed"):
        await RELAYED_HANDLERS["debug.dump"](agent, frame)
    monkeypatch.setattr(RelayedCommandOutcomes, "complete", real_complete)
    reply = checked_reply("debug.dump", await RELAYED_HANDLERS["debug.dump"](agent, frame))
    assert reply["outcome"] == "runtime_error" and "may have finished" in reply["message"]
    assert len(agent.applied) == 1
