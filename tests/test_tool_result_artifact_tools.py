from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from runtime_settings.instance_layout import InstanceLayout
from tests.broker_access_support import context_signer
from tests.change_approval_support import use_signed_contexts


@pytest.fixture(autouse=True)
def own_agent(tmp_path, monkeypatch):
    from custom_tools import tool_result_artifact_tools as tools
    from custom_tools import workspace_scope

    # The tool finds state/tool_results through the agent's instance root
    # (runtime_layout), so both lookups must return the same fake agent.
    # Tasks name their person only through a gateway-signed context.
    use_signed_contexts(monkeypatch)
    agent = SimpleNamespace(
        id="agent-1",
        team_id="team-1",
        instance_root=str(tmp_path),
        authorization_config={"enabled": False},
        task_manager=SimpleNamespace(current_task_id="task-1"),
    )
    monkeypatch.setattr(tools, "get_agent_ref", lambda: agent)
    monkeypatch.setattr(workspace_scope, "get_agent_ref", lambda: agent)
    return agent


def _tool_results_dir(tmp_path: Path) -> Path:
    return InstanceLayout.at(tmp_path).tool_results_dir


def _payload(result):
    return json.loads(result["content"][0]["text"])


def _write_manifest(tmp_path: Path, *, binary: bool = False) -> Path:
    artifact_dir = _tool_results_dir(tmp_path) / "task-1" / "artifact-1"
    artifact_dir.mkdir(parents=True)
    text_block = artifact_dir / "block_0.txt"
    text_block.write_text("alpha\nneedle one\nbeta\nneedle two\ngamma\n", encoding="utf-8")
    blocks = [
        {
            "index": 0,
            "content_type": "text/plain",
            "path": str(text_block),
            "bytes": text_block.stat().st_size,
            "chars": len(text_block.read_text(encoding="utf-8")),
        }
    ]
    if binary:
        binary_block = artifact_dir / "block_1.png"
        binary_block.write_bytes(b"abc")
        blocks.append(
            {
                "index": 1,
                "content_type": "image/png",
                "path": str(binary_block),
                "bytes": binary_block.stat().st_size,
                "chars": 0,
            }
        )
    manifest = {
        "manifest_version": 1,
        "created_at": "2026-05-06T00:00:00+00:00",
        "reason": "max_tool_result_tokens_per_call_exceeded",
        "task_id": "task-1",
        "agent_id": "agent-1",
        "team_id": "team-1",
        "conversation_id": "conversation-1",
        "tool_name": "large_tool",
        "tool_use_id": "tool_large",
        "original_tokens": 123,
        "original_chars": 456,
        "max_tool_result_tokens_per_call": 100,
        "blocks": blocks,
    }
    manifest_path = artifact_dir / "manifest.json"
    manifest_path.write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
    return manifest_path


def test_inspect_tool_result_artifact_manifest_and_bounded_reads(tmp_path, monkeypatch):
    from custom_tools.tool_result_artifact_tools import inspect_tool_result_artifact

    manifest = _write_manifest(tmp_path)

    summary = _payload(inspect_tool_result_artifact(str(manifest), action="manifest"))
    assert summary["status"] == "success"
    assert summary["blocks"][0]["content_type"] == "text/plain"

    head = _payload(inspect_tool_result_artifact(str(manifest), action="head", max_lines=2, max_chars=100))
    assert [line["text"] for line in head["lines"]] == ["alpha", "needle one"]

    tail = _payload(inspect_tool_result_artifact(str(manifest), action="tail", max_lines=2, max_chars=100))
    assert [line["text"] for line in tail["lines"]] == ["needle two", "gamma"]

    range_payload = _payload(inspect_tool_result_artifact(str(manifest), action="read_range", start_line=2, max_lines=1))
    assert range_payload["lines"][0]["text"] == "needle one"

    grep = _payload(inspect_tool_result_artifact(str(manifest), action="grep", query="needle", max_lines=2, context_lines=0))
    assert grep["match_line_count"] == 2


def test_inspect_tool_result_artifact_rejects_non_artifact_paths(tmp_path, monkeypatch):
    from custom_tools.tool_result_artifact_tools import inspect_tool_result_artifact

    outside = tmp_path / "outside.manifest.json"
    outside.write_text("{}", encoding="utf-8")

    payload = _payload(inspect_tool_result_artifact(str(outside), action="manifest"))

    assert payload["status"] == "failed"
    assert payload["failure_reason"] == "path_outside_artifacts"


def test_inspect_tool_result_artifact_binary_blocks_return_metadata_only(tmp_path, monkeypatch):
    from custom_tools.tool_result_artifact_tools import inspect_tool_result_artifact

    manifest = _write_manifest(tmp_path, binary=True)

    payload = _payload(inspect_tool_result_artifact(str(manifest), block_index=1, action="head"))

    assert payload["status"] == "success"
    assert payload["is_binary"] is True
    assert "lines" not in payload


def test_inspect_tool_result_artifact_rejects_unowned_manifest(tmp_path, monkeypatch):
    from custom_tools.tool_result_artifact_tools import inspect_tool_result_artifact

    artifact_root = _tool_results_dir(tmp_path) / "task-1"
    artifact_root.mkdir(parents=True)
    raw_path = artifact_root / "legacy.json"
    raw_path.write_text('{"ok": true}\n', encoding="utf-8")
    manifest = artifact_root / "manifest.json"
    manifest.write_text(
        json.dumps(
            {
                "created_at": "2026-05-06T00:00:00+00:00",
                "task_id": "task-1",
                "tool_name": "large_tool",
                "tool_use_id": "tool_large",
                "raw_path": str(raw_path),
            }
        ),
        encoding="utf-8",
    )

    payload = _payload(inspect_tool_result_artifact(str(manifest), action="manifest"))

    assert payload["status"] == "failed"
    assert payload["failure_reason"] == "artifact_owner_required"


@pytest.mark.parametrize("field,value", [("agent_id", "other"), ("team_id", "other"), ("task_id", "other")])
def test_artifact_owner_is_checked(tmp_path, monkeypatch, own_agent, field, value):
    from custom_tools import tool_result_artifact_tools as tools
    manifest = _write_manifest(tmp_path)
    data = json.loads(manifest.read_text())
    data[field] = value
    manifest.write_text(json.dumps(data))
    assert _payload(tools.inspect_tool_result_artifact(str(manifest)))["failure_reason"] == "artifact_owner_required"


def test_artifact_cannot_read_block_in_another_manifest_directory(tmp_path, monkeypatch):
    from custom_tools import tool_result_artifact_tools as tools
    manifest = _write_manifest(tmp_path)
    other = manifest.parent.parent / "other.txt"
    other.write_text("private")
    data = json.loads(manifest.read_text())
    data["blocks"][0]["path"] = str(other)
    manifest.write_text(json.dumps(data))
    assert _payload(tools.inspect_tool_result_artifact(str(manifest), action="head"))["failure_reason"] == "block_path_invalid"


@pytest.mark.parametrize("owner,allowed", [("alice", True), ("bob", False)])
def test_prior_task_artifacts_are_isolated_by_user(tmp_path, monkeypatch, own_agent, owner, allowed):
    from custom_agents.task_manager import TaskManager
    from custom_messages.contracts.task_v1 import Task
    from custom_messages.contracts.routing_v1 import GatewayContext, MessageHeader, MessageMeta
    from custom_messages.enums import MessageMetaType, ProgressState
    from custom_tools import tool_result_artifact_tools as tools
    manager = TaskManager(db_path=str(tmp_path / "tasks.db"))
    manager.set_owner_identity("agent-1", "Worker", "team-1", "Worker")
    own_agent.task_manager = manager
    for task_id, task_owner in [("past", owner), ("task-1", "alice")]:
        signed = context_signer().issue(team_id="team-1", person_id=task_owner, person_roles=("member",))
        manager.add_task(Task(task_id=task_id, task_type=MessageMetaType.REQUEST, problem_statement="Work"), MessageHeader(), MessageMeta(gateway_context=GatewayContext(canonical_user_id=task_owner, task_context=signed)), owner_person_id=task_owner)
    manager.update_main_task_state("task-1", ProgressState.IN_PROGRESS, "2026-09-21T00:00:00Z")
    manifest = _write_manifest(tmp_path)
    data = json.loads(manifest.read_text())
    data["task_id"] = "past"
    manifest.write_text(json.dumps(data))
    assert (_payload(tools.inspect_tool_result_artifact(str(manifest)))["status"] == "success") == allowed
    manager._save_completed_task("past", "completed")
    manager.remove_task("past")
    assert (_payload(tools.inspect_tool_result_artifact(str(manifest)))["status"] == "success") == allowed


class _CharCountModel:
    async def count_tokens(self, messages, tool_specs=None, system_prompt=None, system_prompt_content=None):
        return max(1, len(json.dumps(messages, default=str)) // 4)


def test_a_result_the_context_manager_saved_opens_with_the_inspect_tool(tmp_path, own_agent):
    """Strands hands hooks its own Agent (no id, team or task), as in the reported log."""

    from strands.types.exceptions import ContextWindowOverflowException

    from custom_agents.filtered_tool_result_context import FilteredToolResultConversationManager
    from custom_agents.tool_result_offloader import TaskAwareWorkspaceStorage
    from custom_tools.tool_result_artifact_tools import inspect_tool_result_artifact

    staged = json.dumps({"change_id": "settings_change_01KSTAGED", "instructions": "ask them to confirm " * 200})
    messages = [
        {"role": "assistant", "content": [{"toolUse": {"toolUseId": "stage_1", "name": "plan_runtime_settings_change", "input": {}}}]},
        {"role": "user", "content": [{"toolResult": {"toolUseId": "stage_1", "status": "success", "content": [{"text": staged}]}}]},
    ]
    strands_agent = SimpleNamespace(messages=messages)  # what the hook receives
    storage = TaskAwareWorkspaceStorage(
        artifact_root=_tool_results_dir(tmp_path),
        task_manager=own_agent.task_manager,
        team_id=own_agent.team_id,
        agent_id=own_agent.id,
        named_agent_type="TeamArchitect",
    )
    model = _CharCountModel()
    manager = FilteredToolResultConversationManager(storage=storage, config={"max_total_tokens": 200}, model=model)

    manager.reduce_context(strands_agent, e=ContextWindowOverflowException("too long"), invocation_state={})

    stub = json.loads(messages[1]["content"][0]["toolResult"]["content"][0]["text"])
    assert stub["artifact_only"] is True
    assert "inspect_tool_result_artifact" in stub["message"]
    manifest = _payload(inspect_tool_result_artifact(stub["artifact_manifest_path"], action="manifest"))
    assert manifest["status"] == "success"
    assert manifest["task_id"] == "task-1"
    assert manifest["tool_name"] == "plan_runtime_settings_change"
    head = _payload(inspect_tool_result_artifact(stub["artifact_manifest_path"], action="grep", query="change_id"))
    assert head["status"] == "success"
    assert "settings_change_01KSTAGED" in json.dumps(head["matches"])
