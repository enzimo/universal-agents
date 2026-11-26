"""The live comparison's call counts and their summary, without a model.

``tests/test_live_tool_presentation.py`` runs only with the live environment;
these checks cover its watcher's call_tool / use_tools / describe_tools counts
and ``scripts/summarize_tool_presentation_report.py`` (new rows and rows from
reports written before the counts existed).
"""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

from strands import Skill

from custom_agents.tool_loading import declared_skill_tool_ids
from scripts.summarize_tool_presentation_report import COUNTED, main, summarize
from tests.test_live_tool_presentation import TASKS, TEST_SKILLS, Watch, call_counts

SUCCESS = {"status": "success", "content": [{"text": "{}"}]}


def run_tool(watch: Watch, sent: dict, ran: dict, result: dict = SUCCESS) -> None:
    """One tool call as the hooks see it: ``sent`` before CallToolHook, ``ran`` after it."""

    watch._before_tool(SimpleNamespace(tool_use=sent))
    watch._after_tool(SimpleNamespace(tool_use=ran, result=result, selected_tool=object()))


def test_the_watch_counts_calls_through_call_tool_and_loading_and_describing_calls() -> None:
    watch = Watch()
    through = {"toolUseId": "t1", "name": "list_heartbeat_items", "input": {}}
    run_tool(watch, {"toolUseId": "t1", "name": "call_tool", "input": {"name": "list_heartbeat_items"}}, through)
    run_tool(watch, {"toolUseId": "t2", "name": "use_tools", "input": {"tool_ids": ["fn:heartbeat"]}}, {"toolUseId": "t2", "name": "use_tools", "input": {"tool_ids": ["fn:heartbeat"]}})
    run_tool(watch, {"toolUseId": "t3", "name": "describe_tools", "input": {"names": ["x"]}}, {"toolUseId": "t3", "name": "describe_tools", "input": {"names": ["x"]}})
    # Cancelled before the rewrite: still named call_tool; recorded as its target, failed.
    cancelled = {"toolUseId": "t4", "name": "call_tool", "input": {"name": "memory", "inputs": {"action": "list"}}}
    run_tool(watch, cancelled, cancelled, {"status": "error", "content": [{"text": "refused"}]})
    # The same id reused by a later direct call is not counted again.
    run_tool(watch, {"toolUseId": "t1", "name": "calculator", "input": {}}, {"toolUseId": "t1", "name": "calculator", "input": {}})

    assert call_counts(watch.calls) == {"call_tool_calls": 2, "use_tools_calls": 1, "describe_tools_calls": 1}
    assert [call["tool"] for call in watch.calls] == ["list_heartbeat_items", "use_tools", "describe_tools", "memory", "calculator"]
    assert watch.calls[3]["input"] == {"action": "list"} and watch.calls[3]["ok"] is False


def test_the_summary_adds_the_new_counts_and_reads_older_rows_as_zero(tmp_path: Path, capsys) -> None:
    rows = [
        {"model": "m", "presentation": "full_definitions", "task": "a", "success": True, "inputTokens": 1000, "outputTokens": 10},
        {"model": "m", "presentation": "index", "task": "a", "success": True, "inputTokens": 600, "outputTokens": 12,
         "call_tool_calls": 2, "use_tools_calls": 0, "describe_tools_calls": 1},
        {"model": "m", "presentation": "index", "task": "b", "success": False, "inputTokens": 800, "outputTokens": 8,
         "call_tool_calls": 1, "use_tools_calls": 1},
    ]

    full, index = summarize(rows)

    assert {field: full[field] for field in COUNTED} == dict.fromkeys(COUNTED, 0)
    assert (index["call_tool_calls"], index["use_tools_calls"], index["describe_tools_calls"]) == (3, 1, 1)
    assert index["input_vs_full"] == "-30%"
    report = tmp_path / "report.jsonl"
    report.write_text("\n".join(json.dumps(row) for row in rows), encoding="utf-8")
    assert main(["summarize", str(report), "--markdown"]) == 0
    assert "| call_tool_calls | use_tools_calls | describe_tools_calls |" in capsys.readouterr().out


def test_the_skill_task_uses_a_test_skill_that_declares_a_group_the_index_hides() -> None:
    [task] = [task for task in TASKS if task.task_id == "skill_heartbeat_audit"]
    skill = Skill.from_file(TEST_SKILLS / "heartbeat-checklist-audit" / "SKILL.md")

    assert task.skill_directories == (str(TEST_SKILLS),)
    assert declared_skill_tool_ids(skill.metadata) == ["fn:heartbeat"]
    assert {need.tool for need in task.required} == {"skills", "list_heartbeat_items"}
