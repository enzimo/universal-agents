"""Schedule contracts preserve real SQLite reads and committed tool outcomes.

The mappings here specify the future dispatcher without changing today's tools.
Only independent follow-ups fail; every mutation still reaches the real store.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import sqlite3
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from jsonschema import ValidationError as SchemaError

from custom_agents.schedule_store import schedule_record_digest
from custom_messages.contracts.schedule_v1 import ScheduleTaskMemory
from custom_messages.wire.frame import body_model
from custom_tools.providers.schedules import (
    create_scheduled_job,
    disable_scheduled_job,
    list_scheduled_jobs,
    pause_scheduled_job,
    remove_scheduled_job,
    resume_scheduled_job,
    update_scheduled_job,
)
from custom_tools.result_pages import text_page
from tests.change_approval_support import use_signed_contexts
from tests.test_message_contract import body_schema_file, validator
from tests.task_work_support import set_agent_ref
from tests.test_schedule_tools import _FakeAgent
from tests.tool_result_support import sdk_tool_result, tool_json

DETAIL_FIELDS = {
    "schedule_id", "record_digest", "schedule_name", "management_class", "system_schedule_kind",
    "assigned_agent_type", "enabled", "paused_until", "expires_at_utc", "next_fire_at", "interval_ms",
    "cron_expression", "cron_timezone", "repeat_count", "objective_id", "owner_canonical_user_id",
    "problem_statement", "schedule_memory",
}
TOOLS = {
    "install": create_scheduled_job, "update": update_scheduled_job, "pause": pause_scheduled_job,
    "resume": resume_scheduled_job, "disable": disable_scheduled_job, "remove": remove_scheduled_job,
}
STATES = {
    "install": "installed", "update": "applied", "pause": "paused", "resume": "resumed",
    "disable": "disabled", "remove": "removed",
}


@pytest.fixture
def agent(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Any]:
    import custom_tools.registry  # noqa: F401

    use_signed_contexts(monkeypatch)
    instance: Any = _FakeAgent(str(tmp_path / "schedules.db"))
    set_agent_ref(instance)
    yield instance
    set_agent_ref(None)


def check_body(kind: str, body: dict[str, Any]) -> dict[str, Any]:
    validator(body_schema_file(kind)).validate(body)
    adapter, _ = body_model(kind)
    parsed = adapter.validate_python(body)
    assert adapter.dump_python(parsed, mode="json", exclude_none=True, by_alias=True) == body
    return body


def unavailable(*_args: Any, **_kwargs: Any) -> None:
    raise sqlite3.OperationalError("database is locked")


def run_tool(agent: Any, tool: Any, inputs: dict[str, Any]) -> dict[str, Any]:
    async def scenario() -> dict[str, Any]:
        agent._runtime_loop = asyncio.get_running_loop()
        agent._should_exit = asyncio.Event()
        agent._owned_tasks = set()
        try:
            result = await sdk_tool_result(tool, inputs)
            await asyncio.sleep(0)
            return result
        finally:
            agent._runtime_loop = None

    result = asyncio.run(scenario())
    assert result["status"] == "success", result
    payload = tool_json(result)
    assert isinstance(payload, dict)
    return payload


def saved_body(operation: str, payload: dict[str, Any]) -> dict[str, Any]:
    """Use only what the successful tool returned, never invent a digest."""
    body = {"outcome": "ok", "state": STATES[operation], "schedule_id": payload["schedule_id"]}
    if operation != "remove":
        digest = payload.get("record_digest")
        body["version"] = {"state": "available", "seen_version": digest} if digest else {"state": "unavailable"}
        if payload.get("removed_by_expiration"):
            body["version"] = {"state": "removed_by_expiration"}
    warnings = list(payload.get("warnings", []))
    report_warning = payload.get("manager_report", {}).get("warning")
    if report_warning:
        warnings.append(report_warning)
    if warnings:
        body["warnings"] = {"type": "inline", "text": "\n".join(warnings)}
    return check_body(f"schedules.{operation}.reply", body)


def read_runtime_detail(schedule_id: str) -> dict[str, Any]:
    pages = []
    offset = 0
    digest = None
    while True:
        response = tool_json(list_scheduled_jobs(schedule_id=schedule_id, details_offset=offset))
        detail = response["schedules"][0]["detail"]
        if digest is not None:
            assert detail["sha256"] == digest
        digest = detail["sha256"]
        pages.append(detail["text"])
        if detail["next_offset"] is None:
            break
        assert detail["next_offset"] > offset
        offset = detail["next_offset"]
    document = json.loads("".join(pages))
    assert isinstance(document, dict)
    return document


def test_exact_detail_preserves_instruction_ledger_and_owner_scope(agent: Any, tmp_path: Path) -> None:
    instruction = "Review the build. " * 600 + "Finish with the saved result."
    memory: dict[str, Any] = {
        "ledger": {"total_runs": 9, "recent_runs": [{"task_id": "run_r2", "result": "Saved build R2. " * 1400}],
                   "total_notifications": 1, "recent_notifications": [{"result": "Build R2 is saved."}]},
        "state": {"summary": "Build R2 is saved.", "domain": {"build": "R2"}},
    }
    created = run_tool(agent, create_scheduled_job, {
        "instruction": instruction, "interval_seconds": 60, "schedule_memory_json": json.dumps(memory),
    })
    raw = read_runtime_detail(created["schedule_id"])
    assert set(raw) == DETAIL_FIELDS | {"gateway_context"}, "Review new runtime fields before allowing them into detail."
    assert raw["gateway_context"]["chat_id"], "The raw runtime context must exercise the privacy exclusion."
    safe = {key: raw[key] for key in DETAIL_FIELDS}
    text = json.dumps(safe, ensure_ascii=False, sort_keys=True)
    assert len(text) > 16384
    assert "gateway_context" not in text and "chat_sched" not in text and "session_sched" not in text
    parts = []
    offset = 0
    while True:
        check_body("schedules.get", {"request_id": "inspect_build", "schedule_id": created["schedule_id"],
                                    "offset": offset, "max_chars": 4000})
        page = text_page(text, offset=offset, max_chars=4000)
        if page["next_offset"] is None:
            del page["next_offset"]
        body = check_body("schedules.get.reply", {
            "outcome": "ok", "schedule_id": created["schedule_id"], "seen_version": raw["record_digest"],
            "detail": page, "delivery_ref": "dr_Wm9vbVJvb21XZWJDaGF0",
        })
        assert body["detail"]["sha256"] == hashlib.sha256(text.encode()).hexdigest()
        parts.append(body["detail"]["text"])
        if "next_offset" not in page:
            break
        assert page["next_offset"] > offset
        offset = page["next_offset"]
    restored = json.loads("".join(parts))
    assert restored == safe
    assert restored["problem_statement"] == instruction
    assert restored["schedule_memory"]["ledger"] == raw["schedule_memory"]["ledger"]
    assert restored["schedule_memory"]["ledger"]["total_runs"] == 9
    assert restored["schedule_memory"]["ledger"]["recent_runs"][0]["result"] == memory["ledger"]["recent_runs"][0]["result"]
    # Another signed person's exact-ID read stays invisible in the unchanged tool.
    other: Any = _FakeAgent(str(tmp_path / "schedules.db"), canonical_user_id="person_other")
    set_agent_ref(other)
    assert tool_json(list_scheduled_jobs(schedule_id=created["schedule_id"]))["schedules"] == []


def test_run_history_changes_detail_identity_without_changing_listing_version(agent: Any) -> None:
    created = run_tool(agent, create_scheduled_job, {"instruction": "Read saved build results.", "interval_seconds": 60})
    before = read_runtime_detail(created["schedule_id"])

    def record_run(memory: ScheduleTaskMemory) -> bool:
        memory.ledger.total_runs += 1
        memory.ledger.recent_runs.append({"task_id": "run_next", "result": "Build R3 is saved."})
        return True

    agent._schedule_store.edit_schedule_memory(created["schedule_id"], record_run)
    after = read_runtime_detail(created["schedule_id"])
    assert before["record_digest"] == after["record_digest"]
    before_text = json.dumps({key: before[key] for key in DETAIL_FIELDS}, ensure_ascii=False, sort_keys=True)
    after_text = json.dumps({key: after[key] for key in DETAIL_FIELDS}, ensure_ascii=False, sort_keys=True)
    assert text_page(before_text)["sha256"] != text_page(after_text)["sha256"]
    assert after["schedule_memory"]["ledger"]["recent_runs"][-1]["result"] == "Build R3 is saved."


@pytest.mark.parametrize("operation", list(TOOLS))
def test_committed_changes_keep_wake_warnings(agent: Any, monkeypatch: pytest.MonkeyPatch, operation: str) -> None:
    if operation == "install":
        inputs = {"instruction": "Saved instruction.", "interval_seconds": 60}
    else:
        created = run_tool(agent, create_scheduled_job, {"instruction": "Before the edit.", "interval_seconds": 60})
        inputs = {"schedule_id": created["schedule_id"], "expected_record_digest": created["record_digest"]}
        if operation == "update":
            inputs["instruction"] = "Saved instruction."
        if operation == "pause":
            inputs["duration_minutes"] = 60
    monkeypatch.setattr("custom_tools.providers.schedules.wake_scheduler", unavailable)
    payload = run_tool(agent, TOOLS[operation], inputs)
    body = saved_body(operation, payload)
    assert body["outcome"] == "ok"
    assert "Do not repeat the change" in body["warnings"]["text"]
    assert "waking the scheduler failed" in body["warnings"]["text"]
    saved = agent._schedule_store.get_scheduled_message(payload["schedule_id"])
    if operation == "remove":
        assert saved is None
        assert "version" not in body
    else:
        assert saved is not None
        assert body["version"] == {"state": "available", "seen_version": schedule_record_digest(saved)}
        if operation in {"install", "update"}:
            assert saved.payload.problem_statement == "Saved instruction."
        if operation == "pause":
            assert saved.paused_until is not None
        if operation == "resume":
            assert saved.enabled and saved.paused_until is None
        if operation == "disable":
            assert not saved.enabled


@pytest.mark.parametrize("operation", ["install", "update"])
def test_saved_change_without_readable_version_is_successful(
    agent: Any, monkeypatch: pytest.MonkeyPatch, operation: str,
) -> None:
    store = agent._schedule_store
    if operation == "install":
        inputs = {"instruction": "Saved despite read failure.", "interval_seconds": 60}
        method = "add"
    else:
        created = run_tool(agent, create_scheduled_job, {"instruction": "Before the edit.", "interval_seconds": 60})
        inputs = {"schedule_id": created["schedule_id"], "expected_record_digest": created["record_digest"],
                  "instruction": "Saved despite read failure."}
        method = "update"
    original_commit = getattr(store, method)
    with monkeypatch.context() as patch:
        def save_then_fail_read(*args: Any, **kwargs: Any) -> Any:
            result = original_commit(*args, **kwargs)
            patch.setattr(store, "get_scheduled_message", unavailable)
            return result

        patch.setattr(store, method, save_then_fail_read)
        payload = run_tool(agent, TOOLS[operation], inputs)
    assert not payload.get("record_digest")
    body = saved_body(operation, payload)
    assert body["version"] == {"state": "unavailable"}
    assert "removed_by_expiration" not in body
    assert "Do not repeat the change" in body["warnings"]["text"]
    saved = store.get_scheduled_message(payload["schedule_id"])
    assert saved.payload.problem_statement == "Saved despite read failure."
    assert len(store.get_all()) == 1
    contradictory = {**body, "version": {"state": "unavailable", "seen_version": schedule_record_digest(saved)}}
    with pytest.raises(SchemaError):
        validator(f"bodies/schedules.{operation}.reply.schema.json").validate(contradictory)


def test_expired_update_reports_successful_removal_without_transient_digest(agent: Any) -> None:
    created = run_tool(agent, create_scheduled_job, {"instruction": "Expire this reminder.", "interval_seconds": 60})
    payload = run_tool(agent, update_scheduled_job, {
        "schedule_id": created["schedule_id"], "expected_record_digest": created["record_digest"],
        "expires_at_utc": "2001-01-01T00:00:00+00:00",
    })
    assert payload["updated"] and payload["removed_by_expiration"]
    assert payload["record_digest"] is None
    assert agent._schedule_store.get_scheduled_message(created["schedule_id"]) is None
    body = saved_body("update", payload)
    assert body["version"] == {"state": "removed_by_expiration"}
    with pytest.raises(SchemaError):
        validator("bodies/schedules.update.reply.schema.json").validate({
            **body, "version": {"state": "removed_by_expiration", "seen_version": created["record_digest"]},
        })


def test_sent_report_history_warning_is_preserved(agent: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    created = run_tool(agent, create_scheduled_job, {"instruction": "Before the edit.", "interval_seconds": 60})
    monkeypatch.setattr(agent.task_manager, "append_history_entry", unavailable)
    payload = run_tool(agent, update_scheduled_job, {
        "schedule_id": created["schedule_id"], "expected_record_digest": created["record_digest"],
        "instruction": "The report was sent.",
    })
    assert payload["manager_report"]["reported"] is True
    body = saved_body("update", payload)
    assert body["warnings"]["text"] == payload["manager_report"]["warning"]
    assert agent._schedule_store.get_scheduled_message(created["schedule_id"]).payload.problem_statement == "The report was sent."


def test_independent_followup_failures_preserve_every_warning_in_order(agent: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    created = run_tool(agent, create_scheduled_job, {"instruction": "Before the edit.", "interval_seconds": 60})
    store = agent._schedule_store
    original_update = store.update
    with monkeypatch.context() as patch:
        def save_then_fail_read(*args: Any, **kwargs: Any) -> Any:
            result = original_update(*args, **kwargs)
            patch.setattr(store, "get_scheduled_message", unavailable)
            return result

        patch.setattr(store, "update", save_then_fail_read)
        patch.setattr(store, "prune_expired", unavailable)
        patch.setattr("custom_tools.providers.schedules.wake_scheduler", unavailable)
        payload = run_tool(agent, update_scheduled_job, {
            "schedule_id": created["schedule_id"], "expected_record_digest": created["record_digest"],
            "instruction": "Saved once despite three failed follow-ups.",
        })
    assert len(payload["warnings"]) == 3
    body = saved_body("update", payload)
    assert body["warnings"]["text"] == "\n".join(payload["warnings"])
    assert body["version"] == {"state": "unavailable"}
    assert store.get_scheduled_message(created["schedule_id"]).payload.problem_statement == "Saved once despite three failed follow-ups."
