"""Committed SQLite changes stay successful when a later independent step fails.

The tools run through the actual Strands decorator with gateway-signed task
contexts. Only the operation after the commit is fault-injected; assertions
check the result status, the saved record, the warning, and the log.
"""

from __future__ import annotations

import asyncio
import sqlite3
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from custom_tools.providers.heartbeat import _heartbeat_store, add_heartbeat_item
from custom_tools.providers.changes import _Applied, apply_change
from custom_tools.providers.schedules import create_scheduled_job, update_scheduled_job
from tests.task_work_support import set_agent_ref
from tests.change_approval_support import ToolCall, agreement, use_signed_contexts
from tests.test_schedule_tools import _FakeAgent
from tests.tool_result_support import sdk_tool_result, tool_json


@pytest.fixture(autouse=True)
def signed_contexts(monkeypatch):
    use_signed_contexts(monkeypatch)
    yield
    set_agent_ref(None)


def unavailable(*_args, **_kwargs):
    raise sqlite3.OperationalError("database is locked")


def run_tool(agent, tool, inputs):
    async def scenario():
        agent._runtime_loop = asyncio.get_running_loop()
        agent._should_exit = asyncio.Event()
        agent._owned_tasks = set()
        try:
            result = await sdk_tool_result(tool, inputs)
            await asyncio.sleep(0)
            return result
        finally:
            agent._runtime_loop = None

    return asyncio.run(scenario())


@pytest.mark.parametrize("step", ["history", "worker", "wake"])
def test_saved_schedule_creation_returns_its_id_and_warning(
    tmp_path, monkeypatch, caplog, step
):
    agent = _FakeAgent(str(tmp_path / "schedules.db"))
    inputs = {"instruction": "Check the build.", "interval_seconds": 60}
    if step == "history":
        monkeypatch.setattr(agent.task_manager, "append_history_entry", unavailable)
    elif step == "worker":
        inputs["worker_policy_json"] = "{}"
        monkeypatch.setattr(
            "custom_agents.system_schedule_changes.configure_worker", unavailable
        )
    else:
        monkeypatch.setattr(
            "custom_tools.providers.schedules.wake_scheduler", unavailable
        )
    set_agent_ref(agent)

    result = run_tool(agent, create_scheduled_job, inputs)

    assert result["status"] == "success", result
    payload = tool_json(result)
    saved = agent._schedule_store.get_scheduled_message(payload["schedule_id"])
    assert saved is not None and saved.payload.problem_statement == "Check the build."
    assert len(agent._schedule_store.get_all()) == 1
    assert saved.enabled is (step != "worker")
    assert payload["schedule_id"] in payload["warnings"][0]
    assert "Do not repeat the change" in payload["warnings"][0]
    assert "event=change_followup_failed" in caplog.text
    assert f"record_id={payload['schedule_id']}" in caplog.text
    if step == "history":
        assert agent._scheduler_wake.was_set, (
            "failed history must not skip the scheduler wake"
        )


@pytest.mark.parametrize(
    "step", ["cleanup", "read", "worker_approval", "send", "report_history", "wake"]
)
def test_saved_schedule_update_stays_successful_when_a_followup_fails(
    tmp_path, monkeypatch, caplog, step
):
    agent = _FakeAgent(str(tmp_path / "schedules.db"))
    set_agent_ref(agent)
    created = tool_json(
        create_scheduled_job(instruction="Old instruction.", interval_seconds=60)
    )
    store = agent._schedule_store
    original_read = store.get_scheduled_message
    if step == "cleanup":
        monkeypatch.setattr(store, "prune_expired", unavailable)
    elif step == "read":
        original_update = store.update

        def update_then_break_read(*args, **kwargs):
            original_update(*args, **kwargs)
            monkeypatch.setattr(store, "get_scheduled_message", unavailable)

        monkeypatch.setattr(store, "update", update_then_break_read)
    elif step == "worker_approval":
        monkeypatch.setattr(
            "custom_agents.schedule_changes.follow_authorized_change", unavailable
        )
    elif step == "send":
        agent.team_comms.send_message = AsyncMock(
            side_effect=sqlite3.OperationalError("database is locked")
        )
    elif step == "report_history":
        monkeypatch.setattr(agent.task_manager, "append_history_entry", unavailable)
    else:
        monkeypatch.setattr(
            "custom_tools.providers.schedules.wake_scheduler", unavailable
        )

    result = run_tool(
        agent,
        update_scheduled_job,
        {
            "schedule_id": created["schedule_id"],
            "expected_record_digest": created["record_digest"],
            "instruction": "Saved instruction.",
        },
    )

    assert result["status"] == "success", result
    payload = tool_json(result)
    assert (
        payload["updated"] is True and payload["schedule_id"] == created["schedule_id"]
    )
    assert (
        original_read(created["schedule_id"]).payload.problem_statement
        == "Saved instruction."
    )
    if step == "report_history":
        assert payload["manager_report"]["reported"] is True
        assert "history" in payload["manager_report"]["warning"]
        assert "event=schedule_report_history_failed" in caplog.text
    else:
        assert payload["warnings"]
        assert "event=change_followup_failed" in caplog.text


def test_expiring_a_schedule_is_a_successful_change(tmp_path):
    agent = _FakeAgent(str(tmp_path / "schedules.db"))
    set_agent_ref(agent)
    created = tool_json(
        create_scheduled_job(instruction="Check once.", interval_seconds=60)
    )

    result = run_tool(
        agent,
        update_scheduled_job,
        {
            "schedule_id": created["schedule_id"],
            "expected_record_digest": created["record_digest"],
            "expires_at_utc": "2000-01-01T00:00:00+00:00",
        },
    )

    assert result["status"] == "success", result
    payload = tool_json(result)
    assert payload["updated"] is True and payload["removed_by_expiration"] is True
    assert agent._schedule_store.get_scheduled_message(created["schedule_id"]) is None


def test_saved_heartbeat_creation_survives_a_failed_runtime_check(
    tmp_path, monkeypatch, caplog
):
    agent = _FakeAgent(str(tmp_path / "heartbeat.db"))
    monkeypatch.setattr(
        "custom_tools.providers.heartbeat._heartbeat_runtime_state", unavailable
    )
    set_agent_ref(agent)

    result = run_tool(agent, add_heartbeat_item, {"instruction": "Check the build."})

    assert result["status"] == "success", result
    payload = tool_json(result)
    saved = _heartbeat_store(agent).get_item(payload["heartbeat_item_id"])
    assert saved is not None and saved.instruction == "Check the build."
    assert payload["warnings"] and "event=change_followup_failed" in caplog.text


@pytest.mark.parametrize("failure", ["raised", "returned"])
def test_applied_change_keeps_its_receipt_when_its_followup_fails(monkeypatch, caplog, failure):
    result = {"change_id": "change_saved", "installed": True}
    followup = AsyncMock(side_effect=RuntimeError("notice unavailable")) if failure == "raised" else AsyncMock(
        return_value={"error": "reload unavailable"}
    )
    outcome = _Applied(SimpleNamespace(), SimpleNamespace(after_apply=followup), agreement(), result)
    monkeypatch.setattr("custom_tools.providers.changes._decide_and_apply", lambda *_args: outcome)

    actual = asyncio.run(sdk_tool_result(apply_change, {
        "change_id": "change_saved", "reason": "Apply the approved install.", "confirmed": True,
    }, context=ToolCall()))

    assert actual["status"] == "success", actual
    payload = tool_json(actual)
    assert payload["status"] == "applied" and payload["result"]["installed"] is True
    assert payload["result"]["change_id"] == "change_saved"
    assert payload["result"]["warnings"] and "event=change_followup_failed" in caplog.text


def test_adapter_reported_failure_is_a_failed_apply_change_result(monkeypatch):
    outcome = _Applied(SimpleNamespace(), SimpleNamespace(), agreement(), {"error": "The plan was not applied."})
    monkeypatch.setattr("custom_tools.providers.changes._decide_and_apply", lambda *_args: outcome)

    actual = asyncio.run(sdk_tool_result(apply_change, {
        "change_id": "change_failed", "reason": "Apply the approved install.", "confirmed": True,
    }, context=ToolCall()))

    assert actual["status"] == "error", actual
    assert tool_json(actual)["status"] == "failed"
