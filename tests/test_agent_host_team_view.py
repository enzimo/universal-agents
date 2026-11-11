"""Several agents in one process: the host hands each team event and the team's view to every agent it runs.

The team lead's process runs the lead and its thread workers, each on its own loop, behind one
process host (``custom_agents.agent_host``). A ``skills.changed`` event arrives once per process
and must reach every agent there, through the real handler (``custom_agents.live_controls``) and
the real catalog refresh (``apply_managed_skill_catalog_change``); an agent attached after the
team's view was read must get the current directory at once. The review's mutations
``event_first_only`` and ``no_sync_directory`` each make a test here fail.
"""

from __future__ import annotations

import asyncio
import logging
import threading
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from types import MappingProxyType, SimpleNamespace
from typing import Iterator
from unittest.mock import AsyncMock

import pytest

from custom_agents.live_controls import EVENT_HANDLERS
from custom_agents.sdk.strands.skills import list_agent_skill_names
from custom_messages.contracts.control_v1 import AgentStatusMap
from custom_transport.protocol1.team_agents import TeamAgent
from tests.test_agent_host_sessions import _hosted, _process
from tests.test_managed_skill_catalog_runtime import (
    INSTALL_CHANGE_ID,
    TEAM_ID,
    _instance,
    _specialist_runtime,
    _write_attestation,
    _write_receipt,
    _write_skill,
)

NOW = datetime(2026, 11, 9, 9, 0, tzinfo=timezone.utc)


@contextmanager
def _own_loop() -> Iterator[asyncio.AbstractEventLoop]:
    """A thread worker's loop, as each worker in the team lead's process has."""

    loop = asyncio.new_event_loop()
    thread = threading.Thread(target=loop.run_forever, daemon=True)
    thread.start()
    try:
        yield loop
    finally:
        loop.call_soon_threadsafe(loop.stop)
        thread.join(2)
        loop.close()


async def _until(check, timeout: float = 3.0) -> None:
    async with asyncio.timeout(timeout):
        while not check():
            await asyncio.sleep(0.01)


@pytest.mark.asyncio
async def test_a_team_event_reaches_every_agent_the_process_runs(tmp_path: Path) -> None:
    # Each stand-in agent reads its own copy of the instance; the skill and its receipt are the same.
    first, first_plugin = await _specialist_runtime(*_instance(tmp_path / "first"))
    second, second_plugin = await _specialist_runtime(*_instance(tmp_path / "second"))
    second.id, second.name = "agent_writer", "WriterAgent-0001"
    for agent in (first, second):
        _write_skill(Path(agent.additional_skill_directories[0]), "grill-me")
        tree_digest = _write_attestation(agent, "grill-me")
        _write_receipt(agent, operation="install", change_id=INSTALL_CHANGE_ID, skill_name="grill-me",
                       installed_tree_sha256=tree_digest)
    body = {"operation": "install", "change_id": INSTALL_CHANGE_ID, "skill_name": "grill-me",
            "installed_tree_sha256": tree_digest}
    process, _ = _process()
    process.event_handlers = dict(EVENT_HANDLERS)

    with _own_loop() as worker_loop:
        first._runtime_loop = asyncio.get_running_loop()  # the agent the host joined with
        second._runtime_loop = worker_loop  # a thread worker
        for agent in (first, second):
            hosted = _hosted(agent.id)
            hosted.agent = agent
            process.agents[agent.id] = hosted
        event = SimpleNamespace(frame=SimpleNamespace(kind="skills.changed"), raw={"body": body},
                                session=SimpleNamespace(agent_role="lead", team=TEAM_ID, agent="agent_team_architect"))

        assert await process._event(event) == {}

        await _until(lambda: "grill-me" in list_agent_skill_names(second_plugin, second._strands_agent)
                     and "grill-me" in list_agent_skill_names(first_plugin, first._strands_agent)
                     and first._refreshed_state and second._refreshed_state)
        assert first._refreshed_state == [True]
        assert second._refreshed_state == [True]


class _View:
    """Stands in for the process's ``TeamAgents`` once it has read the team."""

    def __init__(self, *members: TeamAgent) -> None:
        self._live = MappingProxyType({item.agent: item for item in members})
        self.ready = SimpleNamespace(is_set=lambda: True)

    def live(self):
        return self._live


def _member(agent: str, name: str, *, lead: bool = False) -> TeamAgent:
    return TeamAgent(agent=agent, agent_type="TeamArchitect" if lead else "Researcher", display_name=name,
                     state="idle", accepting_work=True, since=NOW, host="lead", session=f"s-{agent}", is_lead=lead)


@pytest.mark.asyncio
async def test_an_agent_attached_after_the_view_was_read_gets_the_current_directory() -> None:
    process, _ = _process()
    process.team_agents = _View(_member("lead_1", "TeamLead", lead=True), _member("worker_1", "Researcher-1"))
    process._hold_session = AsyncMock()  # its session opens as in tests/test_agent_host_sessions.py

    with _own_loop() as worker_loop:
        late = SimpleNamespace(id="worker_2", named_agent_type="Researcher", team_id="team", _runtime_loop=worker_loop,
                               known_agents=AgentStatusMap(), _agent_id_by_name={},
                               _logger=logging.getLogger("test.late_worker"))
        hosted = _hosted("worker_2")
        hosted.agent = late

        await process._open(hosted)

        await _until(lambda: set(late.known_agents.agent_map) == {"lead_1", "worker_1"})
        assert late._agent_id_by_name == {"TeamLead": "lead_1", "Researcher-1": "worker_1"}
        assert late.known_agents.agent_map["lead_1"].is_team_lead is True
        process._hold_session.assert_awaited_once_with(hosted)
