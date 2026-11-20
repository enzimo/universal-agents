"""The people's requests an agent's record names (custom_agents.agent_host, Web Chat agent trail).

The host writes the ids its agent's turns run for into the signed record,
including those of turns that started and ended between two writes (C06),
never more or other than the contract takes (C11), and without moving the
state's ``since`` (C01). A failure to read the ids still reports the state.
"""

from __future__ import annotations

import asyncio
import logging
from types import SimpleNamespace
from typing import Any

import pytest

from custom_agents import agent_host
from custom_messages.wire.kinds import STATE_RECORDS
from tests.test_agent_host_sessions import _hosted, _process


def _validating(process) -> None:
    """Sign like ``StateRecords.signed``: the body must fit the contract's model."""

    signed = process.host.records.signed

    def checked(record: str, key: str, body: dict[str, Any], **kwargs: Any) -> dict:
        STATE_RECORDS[record].body_model.validate_python(dict(body))
        return signed(record, key, body, **kwargs)

    process.host.records.signed = checked


def _running(agent_id: str = "a1"):
    process, _ = _process()
    _validating(process)
    hosted = _hosted(agent_id)
    hosted.session, hosted.state = f"s-{agent_id}-1", "idle"
    process.agents[agent_id] = hosted
    return process, hosted


def _requests(process) -> list[Any]:
    return [record["body"].get("requests") for record in process.host.records.written]


@pytest.mark.asyncio
async def test_the_record_names_the_requests_and_a_changed_set_is_written_again(monkeypatch):
    monkeypatch.setattr(agent_host, "AGENT_RECORD_INTERVAL_SECONDS", 0.02)
    process, hosted = _running()
    process._noted("a1", "busy", True, ("task_r1",))
    await asyncio.sleep(0.05)
    process._noted("a1", "busy", True, ("task_r1", "task_r2"))
    await asyncio.sleep(0.05)
    process._noted("a1", "idle", True, ())
    await asyncio.sleep(0.1)
    assert _requests(process)[-3:] == [["task_r1"], ["task_r1", "task_r2"], None]
    assert [record["body"]["state"] for record in process.host.records.written][-1] == "idle"


@pytest.mark.asyncio
async def test_a_changed_request_set_does_not_move_since(monkeypatch):
    monkeypatch.setattr(agent_host, "AGENT_RECORD_INTERVAL_SECONDS", 0.01)
    process, hosted = _running()
    process._noted("a1", "busy", True, ())
    since = hosted.since
    clock = process.host.clock
    process.host.clock = lambda: clock().replace(year=2030)
    process._noted("a1", "busy", True, ("task_r1",))
    await asyncio.sleep(0.05)
    assert hosted.since == since and hosted.requests == ("task_r1",)
    assert process.host.records.written[-1]["body"]["since"] == agent_host.format_timestamp(since)


@pytest.mark.asyncio
async def test_a_turn_that_starts_and_ends_between_two_writes_still_reaches_one_record(monkeypatch):
    monkeypatch.setattr(agent_host, "AGENT_RECORD_INTERVAL_SECONDS", 0.1)
    process, hosted = _running()
    process._noted("a1", "busy", True, ())  # lane entry: written at once, no turn yet
    await asyncio.sleep(0.01)
    process._noted("a1", "busy", True, ("task_quick",))  # the turn starts ...
    process._noted("a1", "busy", True, ())  # ... and ends before the next write is due
    await asyncio.sleep(0.15)
    assert _requests(process) == [None, ["task_quick"]]
    await asyncio.sleep(0.15)
    # Named once: the next record, without a running turn, names it no more.
    assert _requests(process) == [None, ["task_quick"], None]
    assert hosted.requests_seen == {}


@pytest.mark.asyncio
async def test_ids_the_contract_cannot_carry_are_left_out_and_the_record_is_still_written(caplog):
    process, hosted = _running()
    too_many = tuple(f"task_{index:02d}" for index in range(17))
    hosted.state, hosted.requests = "busy", ("x" * 200, "task/with/slash", *too_many)
    hosted.requests_seen = dict.fromkeys(("task_gone", "y" * 200))
    with caplog.at_level(logging.WARNING):
        await process._write_record(hosted)
    [written] = _requests(process)
    assert written == list(too_many[:16])
    assert "event=agent_record_requests_left_out" in caplog.text and "left_out=5" in caplog.text
    # Ids left out cannot keep a later record from being written.
    assert "task_gone" not in hosted.requests_seen and "y" * 200 not in hosted.requests_seen


@pytest.mark.asyncio
async def test_an_agent_with_no_requests_writes_no_requests_field():
    process, hosted = _running()
    await process._write_record(hosted)
    assert "requests" not in process.host.records.written[0]["body"]


@pytest.mark.asyncio
async def test_an_exiting_record_names_no_requests():
    process, hosted = _running()
    hosted.state, hosted.requests, hosted.requests_seen = "exiting", ("task_r1",), {"task_r1": None}
    await process._write_record(hosted)
    assert "requests" not in process.host.records.written[0]["body"]


@pytest.mark.asyncio
async def test_after_a_failed_write_the_next_write_names_the_requests(monkeypatch):
    monkeypatch.setattr(agent_host, "AGENT_RECORD_INTERVAL_SECONDS", 0.01)
    process, hosted = _running()
    put = process.host.records.put
    failures = [OSError("bucket unavailable")]

    async def flaky(*args):
        if failures:
            raise failures.pop()
        return await put(*args)

    process.host.records.put = flaky
    hosted.state, hosted.requests, hosted.requests_seen = "busy", ("task_r1",), {"task_r1": None}
    await process._write_record(hosted)
    assert process.host.records.written == [] and hosted.write_failures == 1
    await asyncio.sleep(0.1)  # the host's retry
    assert _requests(process) == [["task_r1"]] and hosted.write_failures == 0


def test_a_failure_to_read_the_requests_still_reports_the_state(monkeypatch, caplog):
    reported: list[tuple] = []
    host = SimpleNamespace(report=lambda *args, **kwargs: reported.append((args, kwargs)),
                           logger=logging.getLogger("test.agent_host"))
    monkeypatch.setattr(agent_host, "process_host", lambda: host)

    def broken(agent: Any) -> tuple[str, ...]:
        raise RuntimeError("turn registry unreadable")

    monkeypatch.setattr("custom_agents.working_requests.working_request_ids", broken)
    agent = SimpleNamespace(id="a1", paused=False, is_executing=True, _worker_ready=True)
    with caplog.at_level(logging.WARNING):
        agent_host.report_agent_state(agent)
    assert reported == [(("a1", "busy", True, ()), {})]
    assert "event=agent_state_requests_failed agent=a1" in caplog.text


def test_the_limit_is_the_contracts():
    import json
    from pathlib import Path

    schema = json.loads(Path("contracts/messages/v1/schemas/state/agent.schema.json").read_text())
    assert schema["properties"]["requests"]["maxItems"] == agent_host.AGENT_RECORD_REQUESTS_MAX
