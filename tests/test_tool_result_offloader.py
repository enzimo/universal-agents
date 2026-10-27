from __future__ import annotations

from tests.task_work_support import bind_task
from custom_agents.task_operation_runtime import TaskOperationRuntime
import json
from types import SimpleNamespace

from strands.hooks import AfterToolCallEvent

from runtime_settings.instance_layout import InstanceLayout


def _artifact_root(tmp_path):
    """Offloaded tool results live in the instance's state/tool_results."""
    return InstanceLayout.at(tmp_path).tool_results_dir


def _event(result, invocation_state=None, agent=None):
    return AfterToolCallEvent(
        agent=agent or SimpleNamespace(),
        selected_tool=SimpleNamespace(tool_name="large_tool"),
        tool_use={"name": "large_tool", "toolUseId": "tool_large", "input": {}},
        invocation_state=invocation_state
        if invocation_state is not None
        else {"request_state": {}},
        result=result,
    )


class _CountingModel:
    def __init__(self, value: int) -> None:
        self.value = value
        self.calls = []

    async def count_tokens(
        self, messages, tool_specs=None, system_prompt=None, system_prompt_content=None
    ):
        self.calls.append(messages)
        return self.value


def test_tool_result_offloader_replaces_oversized_single_result_with_preview_and_manifest(
    tmp_path,
):
    from custom_agents.tool_result_offloader import TaskAwareToolResultOffloader

    offloader = TaskAwareToolResultOffloader(
        max_tokens_per_call=100,
        model=_CountingModel(value=101),
        artifact_root=_artifact_root(tmp_path),
    )
    invocation_state = {"request_state": {}}
    task_info = SimpleNamespace(meta=SimpleNamespace(conversation_id="conversation-1"))
    task_manager = SimpleNamespace(
        operations_runtime=TaskOperationRuntime(),
        current_task_id="task_budget_test",
        get_task_info=lambda task_id: task_info,
    )
    agent = SimpleNamespace(
        model=_CountingModel(value=101),
        task_manager=task_manager,
        team_id="team-1",
        id="agent-1",
        named_agent_type="CodeWritingAgent",
    )
    event = _event(
        {
            "toolUseId": "tool_large",
            "status": "success",
            "content": [{"text": "x" * 500}],
        },
        invocation_state,
        agent=agent,
    )

    bind_task("task_budget_test")  # the hook runs in the turn of this task (tests.task_work_support)
    offloader._after_tool_call(event)

    assert event.result["status"] == "success"
    assert event.result["toolUseId"] == "tool_large"
    payload = json.loads(event.result["content"][0]["text"])
    assert payload["offloaded_tool_result"] is True
    assert "inspect_tool_result_artifact" in payload["message"]
    assert payload["task_id"] == "task_budget_test"
    assert payload["conversation_id"] == "conversation-1"
    assert payload["artifact_manifest_path"].startswith(str(_artifact_root(tmp_path)))
    assert payload["blocks"][0]["content_type"] == "text/plain"
    manifest = json.loads(
        open(payload["artifact_manifest_path"], encoding="utf-8").read()
    )
    assert manifest["task_id"] == "task_budget_test"
    assert manifest["conversation_id"] == "conversation-1"
    assert manifest["team_id"] == "team-1"
    assert manifest["agent_id"] == "agent-1"
    assert manifest["named_agent_type"] == "CodeWritingAgent"
    assert manifest["tool_name"] == "large_tool"
    assert manifest["tool_use_id"] == "tool_large"
    assert manifest["original_status"] == "success"
    events = invocation_state["request_state"][TaskAwareToolResultOffloader._EVENTS_KEY]
    assert events[0]["artifact_manifest_path"] == payload["artifact_manifest_path"]


def test_tool_result_offloader_writes_json_and_binary_blocks(tmp_path):
    from custom_agents.tool_result_offloader import TaskAwareToolResultOffloader

    model = _CountingModel(value=101)
    offloader = TaskAwareToolResultOffloader(
        max_tokens_per_call=100, model=model, artifact_root=_artifact_root(tmp_path)
    )
    result = {
        "toolUseId": "tool_large",
        "status": "error",
        "content": [
            {"json": {"items": [1, 2, 3]}},
            {"image": {"format": "png", "source": {"bytes": b"abc"}}},
        ],
    }
    event = _event(
        result,
        {"request_state": {}},
        agent=SimpleNamespace(model=model),
    )

    offloader._after_tool_call(event)

    assert event.result["status"] == "error"
    payload = json.loads(event.result["content"][0]["text"])
    manifest = json.loads(
        open(payload["artifact_manifest_path"], encoding="utf-8").read()
    )
    assert manifest["blocks"][0]["content_type"] == "application/json"
    assert manifest["blocks"][1]["content_type"] == "image/png"
    assert open(manifest["blocks"][1]["path"], "rb").read() == b"abc"


def test_tool_result_offloader_rejects_invalid_preserved_status(tmp_path, caplog):
    from custom_agents.tool_result_offloader import TaskAwareToolResultOffloader

    model = _CountingModel(value=101)
    offloader = TaskAwareToolResultOffloader(
        max_tokens_per_call=100,
        model=model,
        artifact_root=_artifact_root(tmp_path),
    )
    event = _event(
        {
            "toolUseId": "tool_large",
            "status": "partial",
            "content": [{"text": "x" * 500}],
        },
        {"request_state": {}},
        agent=SimpleNamespace(model=model),
    )

    with caplog.at_level("WARNING"):
        offloader._after_tool_call(event)

    assert event.result["status"] == "error"
    payload = json.loads(event.result["content"][0]["text"])
    manifest = json.loads(
        open(payload["artifact_manifest_path"], encoding="utf-8").read()
    )
    assert manifest["original_status"] == "partial"
    assert manifest["replacement_status"] == "error"
    assert "event=tool_result_invalid_status" in caplog.text


def test_tool_result_offloader_skips_already_offloaded_payload(tmp_path):
    from custom_agents.tool_result_offloader import TaskAwareToolResultOffloader

    model = _CountingModel(value=101)
    result = {
        "toolUseId": "tool_large",
        "status": "success",
        "content": [
            {
                "text": json.dumps(
                    {"offloaded_tool_result": True, "artifact_manifest_path": "x"}
                )
            }
        ],
    }
    offloader = TaskAwareToolResultOffloader(
        max_tokens_per_call=100, model=model, artifact_root=_artifact_root(tmp_path)
    )
    event = _event(result, {"request_state": {}})

    offloader._after_tool_call(event)

    assert event.result is result
    assert not model.calls
