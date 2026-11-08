"""Writes and interrupted exits retain enough state to finish while the host stays up."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from custom_agents import agent_host
from tests.test_agent_host_sessions import _Answers, _hosted, _process


@pytest.mark.asyncio
async def test_a_change_during_a_write_waits_for_the_next_interval(monkeypatch):
    process, _ = _process()
    hosted = _hosted("a1")
    hosted.session, hosted.state = "s-a1-1", "idle"
    process.agents["a1"] = hosted
    put = process.host.records.put
    entered, release = asyncio.Event(), asyncio.Event()
    monkeypatch.setattr(agent_host, "AGENT_RECORD_INTERVAL_SECONDS", 0.05)

    async def blocked(*args):
        entered.set()
        await release.wait()
        return await put(*args)

    process.host.records.put = blocked
    writing = asyncio.create_task(process._write_record(hosted))
    await entered.wait()
    process._noted("a1", "busy", True)
    release.set()
    await writing
    await asyncio.sleep(0.01)
    assert [item["body"]["state"] for item in process.host.records.written] == ["idle"]
    await asyncio.sleep(0.08)
    assert [item["body"]["state"] for item in process.host.records.written] == ["idle", "busy"]


@pytest.mark.asyncio
async def test_a_session_change_during_a_write_is_not_lost(monkeypatch):
    process, _ = _process()
    hosted = _hosted("a1")
    hosted.session = "s-a1-1"
    process.agents["a1"] = hosted
    put = process.host.records.put
    monkeypatch.setattr(agent_host, "AGENT_RECORD_INTERVAL_SECONDS", 0.01)

    async def renewing(*args):
        hosted.session = "s-a1-2"
        return await put(*args)

    process.host.records.put = renewing
    await process._write_record(hosted)
    await asyncio.sleep(0.05)
    assert [item["body"]["session"] for item in process.host.records.written] == ["s-a1-1", "s-a1-2"]


@pytest.mark.asyncio
@pytest.mark.parametrize("failure_at", ["write", "close", "delete"])
async def test_cancelled_detach_retains_the_exit_until_upkeep_can_finish(failure_at):
    process, _ = _process()
    hosted = _hosted("a1")
    hosted.session = "s-a1-1"
    process.agents["a1"] = hosted
    calls = _Answers(process, [{"outcome": "ok"}])
    process._request = calls
    owner = process if failure_at == "close" else process.host.records
    method = {"write": "put", "close": "_request", "delete": "delete"}[failure_at]
    original = getattr(owner, method)
    entered = asyncio.Event()

    async def blocked(*args, **kwargs):
        entered.set()
        await asyncio.Event().wait()

    setattr(owner, method, blocked)
    closing = asyncio.create_task(process._close("a1", "exited"))
    await asyncio.wait_for(entered.wait(), 1)
    closing.cancel()
    await asyncio.gather(closing, return_exceptions=True)
    assert "a1" in process._pending_departures and hosted.state == "exiting"
    setattr(owner, method, original)
    await process._finish_departure("a1")
    assert process._pending_departures == {} and "a1" not in process.agents
    assert calls.ids("sessions.close") == ["close-s-a1-1"]
    assert process.host.records.removed == ["team_one.a1"]
    assert process.host.records.written[0]["body"]["state"] == "exiting"


@pytest.mark.asyncio
async def test_record_listing_failure_is_logged_and_retried(caplog):
    process, _ = _process()
    calls = 0

    async def keys(record, prefix):
        nonlocal calls
        calls += 1
        if calls == 1:
            raise TimeoutError("listing timed out")
        return []

    process.host.records.keys = keys
    await process._remove_earlier_records()
    assert process._record_cleanup_needed
    await process._remove_earlier_records()
    assert not process._record_cleanup_needed and calls == 2
    assert "event=agent_record_listing_failed" in caplog.text


@pytest.mark.asyncio
async def test_unchanged_records_are_refreshed_with_a_new_signature():
    process, _ = _process()
    hosted = _hosted("a1")
    hosted.session = "s-a1-1"
    process.agents["a1"] = hosted
    await process._write_record(hosted)
    first = hosted.record_sequence
    hosted.written_at -= agent_host.AGENT_RECORD_REFRESH_SECONDS
    await process._write_record(hosted)
    assert len(process.host.records.written) == 2 and hosted.record_sequence > first


def test_agent_availability_follows_startup_pause_and_drain():
    agent = SimpleNamespace(paused=False, is_executing=False, _worker_ready=False)
    assert agent_host.agent_record_state(agent) == ("starting", False)
    agent._worker_ready = True
    assert agent_host.agent_record_state(agent) == ("idle", True)
    agent.paused = True
    assert agent_host.agent_record_state(agent) == ("paused", False)
    agent.paused, agent.is_executing = False, True
    assert agent_host.agent_record_state(agent) == ("busy", True)
    agent._contract_drain = SimpleNamespace(holds_back_unstarted=False)
    assert agent_host.agent_record_state(agent) == ("busy", False)


@pytest.mark.asyncio
async def test_detach_while_the_address_is_subscribing_removes_the_late_subscription():
    process, _ = _process()
    hosted = _hosted("a1")
    process._request = _Answers(process, [
        {"outcome": "ok", "session": {"record": "agent_session", "body": {"agent": "a1", "session": "s-a1-1"}}},
        {"outcome": "ok"},
    ])
    entered, release = asyncio.Event(), asyncio.Event()
    unsubscribed = []

    async def unsubscribe():
        unsubscribed.append(True)

    async def serve(*args, **kwargs):
        entered.set()
        await release.wait()
        return SimpleNamespace(unsubscribe=unsubscribe)

    process.host.live.serve = serve
    opening = asyncio.create_task(process._open(hosted))
    await asyncio.wait_for(entered.wait(), 1)
    await process._close("a1", "exited")
    release.set()
    await asyncio.wait_for(opening, 1)
    assert unsubscribed == [True] and hosted.subscription is None
    assert process.host.records.removed == ["team_one.a1"]
    assert [record["body"]["state"] for record in process.host.records.written] == ["exiting"]


@pytest.mark.asyncio
async def test_conditional_removal_keeps_a_replacement_and_retries():
    process, _ = _process()
    hosted = _hosted("a1")
    hosted.session = "s-a1-1"
    process.agents["a1"] = hosted
    process._request = _Answers(process, [{"outcome": "ok"}])
    original = process.host.records.delete
    attempts = []

    async def changed(record, key, *, last_revision):
        attempts.append(last_revision)
        return False

    process.host.records.delete = changed
    await process._close("a1", "exited")
    assert attempts == [1] and not process.host.records.removed
    assert "a1" in process._pending_departures and hosted.session_closed
    process.host.records.delete = original
    await process._finish_departure("a1")
    assert not process._pending_departures and process.host.records.removed == ["team_one.a1"]


@pytest.mark.asyncio
async def test_the_real_universal_agent_refresh_reports_its_state(monkeypatch):
    from custom_agents.strands_agent import UniversalAgent

    reports = []
    host = SimpleNamespace(report=lambda *args: reports.append(args))
    monkeypatch.setattr(agent_host, "_PROCESS_HOST", host)
    agent = UniversalAgent.model_construct(id="a1", named_agent_type="Researcher", name="Researcher", paused=True)
    agent._worker_ready = True
    agent._agent_state_store = None
    await agent._refresh_self_agent_state()
    assert reports == [("a1", "paused", False)]
