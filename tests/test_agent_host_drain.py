"""A process on another message contract drains its agents and then gives them up (custom_agents.agent_host).

Contract README "Versions": while the label is a pre-release, hosts of one installation
refuse a peer whose contract digest differs. The process's host and the team services are
stand-ins here, as in tests/test_agent_host_sessions.py; each agent's own drain
(custom_agents.contract_drain) is a stand-in too, and tests/test_contract_drain.py checks it.
tests/test_live_agent_hosts.py drains a real host on a real NATS server.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any, Iterable

import pytest

from custom_agents import agent_host
from custom_agents.agent_host import AgentProcessHost
from custom_transport.protocol1.joining import HostProfile, JoinRefused
from tests.test_agent_host_sessions import TEAM, _Answers, _events, _Host, _hosted, _process, _session

OURS, THEIRS = "1" * 64, "2" * 64


class _DrainingHost(_Host):
    """A joined host stand-in that also records its drain."""

    def __init__(self, agent_types: Iterable[str]) -> None:
        super().__init__(agent_types)
        self.contract_sha256 = OURS
        self.installation_contract: str | None = None
        self.steps: list[str] = []

    async def drain(self) -> None:
        self.steps.append("draining")

    async def finish_drain(self) -> None:
        self.steps.append("drained")


class _Agents:
    """Each agent's drain and whether it still holds work, as the process asks them."""

    def __init__(self) -> None:
        self.drained: list[tuple[str, str]] = []
        self.holding: set[str] = set()

    async def drain(self, agent: Any, reason: str) -> None:
        self.drained.append((str(agent.id), reason))

    async def holds_work(self, agent: Any) -> bool:
        return str(agent.id) in self.holding


def _draining_process(answers: list[Any]) -> tuple[AgentProcessHost, _DrainingHost, _Answers, _Agents]:
    process, _pauses = _process()
    host = _DrainingHost(("Researcher",))
    process.host = host  # type: ignore[assignment]
    agents = _Agents()
    process.drain_agent = agents.drain  # type: ignore[assignment]
    process.holds_work = agents.holds_work  # type: ignore[assignment]
    process.drain_poll_seconds = 0.01
    requests = _Answers(process, answers)
    process._request = requests  # type: ignore[method-assign]
    return process, host, requests, agents


async def _until(condition, timeout: float = 5.0) -> None:
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while not condition():
        assert loop.time() < deadline, "timed out"
        await asyncio.sleep(0.01)


@pytest.mark.asyncio
async def test_a_process_on_another_contract_drains_its_agents_and_gives_them_up_once_they_hold_nothing(caplog) -> None:
    process, host, requests, agents = _draining_process([
        lambda body: {"outcome": "ok", "session": _session("a1", "s-a1-1")},
        lambda body: {"outcome": "ok", "session": _session("a2", "s-a2-1")},
        {"outcome": "ok"}, {"outcome": "ok"},
    ])
    await process._open(_hosted("a1"))
    await process._open(_hosted("a2"))
    agents.holding = {"a1"}

    with caplog.at_level(logging.INFO, logger=agent_host.__name__):
        process._other_contract(THEIRS)
        await _until(lambda: len(agents.drained) == 2)
        assert host.steps == ["draining"]
        assert all(OURS in reason and THEIRS in reason for _agent, reason in agents.drained)

        # A new agent gets no session while the process drains, and drains at once.
        await process._open(_hosted("a3"))
        await _until(lambda: len(agents.drained) == 3)
        assert requests.ids() == ["open-a1-1", "open-a2-1"]
        assert [agent for agent, _reason in agents.drained] == ["a1", "a2", "a3"]
        # Sessions are still renewed (control for what they hold), but a refused one is not opened again.
        requests.answers = [{"outcome": "not_allowed", "message": "no"}]
        await process._renew(process.agents["a2"])
        await asyncio.sleep(0.05)
        assert requests.ids() == ["open-a1-1", "open-a2-1"]

        # a1 still holds a task: nothing is given up yet.
        await asyncio.sleep(0.05)
        assert host.steps == ["draining"] and not [kind for kind, _ in requests.asked if kind == "sessions.close"]
        requests.answers = [{"outcome": "ok"}, {"outcome": "ok"}]
        agents.holding = set()
        await _until(lambda: host.steps == ["draining", "drained"])

    closes = [body for kind, body in requests.asked if kind == "sessions.close"]
    assert [(body["session"], body["reason"]) for body in closes] == [("s-a1-1", "exited"),
                                                                     ("s-a2-1", "exited")]
    assert process.agents == {}  # given up; a3 never held a session
    assert len(_events(caplog, "agent_host_drained")) == 1
    process._other_contract(THEIRS)  # learning it again drains nothing again
    assert process._draining is not None and process._draining.done()


@pytest.mark.asyncio
async def test_a_process_whose_join_is_refused_for_another_contract_does_not_ask_again_and_drains(caplog) -> None:
    profile = HostProfile("lead", (TEAM,), ("lead_host", "worker_host"), ("Researcher",), 4)
    process = AgentProcessHost(None, profile, TEAM)  # type: ignore[arg-type]
    agents = _Agents()
    process.drain_agent = agents.drain  # type: ignore[assignment]
    process.holds_work = agents.holds_work  # type: ignore[assignment]
    process.drain_poll_seconds = 0.01
    process.loop = asyncio.get_running_loop()
    process._joined, process._stop = asyncio.Event(), asyncio.Event()

    class _Refused:
        live = None
        contract_sha256 = OURS
        installation_contract: str | None = None
        joins = 0

        async def start(self) -> None:
            self.joins += 1
            self.installation_contract = THEIRS
            process._other_contract(THEIRS)  # what MessageHost.learned_other_contract tells its listeners
            raise JoinRefused("protocol_mismatch", "install the same release", THEIRS)

    host = _Refused()
    process.host = host  # type: ignore[assignment]
    opening = asyncio.ensure_future(process._open(_hosted("a1")))
    await asyncio.sleep(0)
    with caplog.at_level(logging.INFO, logger=agent_host.__name__):
        await asyncio.wait_for(process._run(), timeout=5)
        await _until(lambda: process._draining is not None and process._draining.done())
    await asyncio.wait_for(opening, timeout=5)
    assert host.joins == 1 and not _events(caplog, "agent_host_retry")
    assert [agent for agent, _reason in agents.drained] == ["a1"]
    assert len(_events(caplog, "agent_host_drained")) == 1
    assert "a1" in process.agents  # no session to give up: it never joined


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", [RuntimeError("record write failed"), asyncio.CancelledError()])
async def test_a_drain_step_failure_is_logged_and_retried(failure, caplog) -> None:
    process, host, requests, agents = _draining_process([])
    process.agents["a1"] = _hosted("a1")
    attempts = []

    async def fail_once():
        attempts.append(1)
        if len(attempts) == 1:
            assert [agent for agent, reason in agents.drained] == ["a1"]
            raise failure
        host.steps.append("draining")

    host.drain = fail_once
    with caplog.at_level(logging.WARNING):
        process._other_contract(THEIRS)
        await asyncio.wait_for(process._draining, timeout=5)
    assert len(attempts) == 2
    assert [agent for agent, reason in agents.drained] == ["a1"]
    assert host.steps == ["draining", "drained"]
    assert "trying again" in caplog.text
