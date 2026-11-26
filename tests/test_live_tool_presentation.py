"""Live comparison of tool presentations (ua-architecture plans/tool_index_and_scripts_plan.md, phase 3).

A real TeamArchitect, built from its definition with authorization on, works
on a person's signed task on the configured endpoint. The same fixed tasks
run once per model, per ``agent.tool_presentation`` (full definitions, index,
index and scripts) and per repetition. Each run records input and output
tokens, model calls, the tools that ran (a script's calls included), refused
or unknown tool names, how many calls went through ``call_tool``,
``use_tools`` and ``describe_tools``, and whether the task succeeded: its
required tool calls succeeded and, where the answer is fixed, the answer
contains it. One task is skill-driven: a test skill under
``tests/data/tool_presentation_skills`` declares ``ua-tools`` for a read-level
group the index setups hide. Rows go to a JSON Lines report;
``scripts/summarize_tool_presentation_report.py`` turns it into the
comparison table.

Run with ``RUN_LIVE_TOOL_PRESENTATION=1``, ``TEAM_NATS_PROVIDER_SMOKE_API_BASE``,
``TEAM_NATS_LIVE_ENV`` (an env file with ``LITELLM_API_KEY``, read by the test
process only), ``TOOL_PRESENTATION_MODELS`` (comma-separated model ids),
``TOOL_PRESENTATION_REPEATS`` (default 1) and ``TOOL_PRESENTATION_REPORT``
(the report path). ``TOOL_PRESENTATION_TASKS`` limits the run to some task ids.
"""

from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable
from unittest.mock import patch

import pytest
from dotenv import dotenv_values
from strands.hooks import AfterToolCallEvent, BeforeModelCallEvent, BeforeToolCallEvent, HookProvider, HookRegistry

from custom_agents.tool_presentation import CALL_TOOL_NAME
from runtime_settings.tool_presentation_contract import TOOL_PRESENTATIONS

PROJECT_ROOT = Path(__file__).resolve().parents[1]
CEDAR_BINARY = PROJECT_ROOT / "target" / "debug" / "ua-cedar-authorizer"
MAX_MODEL_CALLS = 14
TEST_SKILLS = PROJECT_ROOT / "tests" / "data" / "tool_presentation_skills"
# Before CallToolHook (custom_agents.tool_calling.CALL_TOOL_HOOK_ORDER, -200), which renames call_tool to its target.
WATCH_BEFORE_TOOL_ORDER = -1000.0

pytestmark = [
    pytest.mark.live,
    pytest.mark.external,
    pytest.mark.skipif(
        os.getenv("RUN_LIVE_TOOL_PRESENTATION") != "1",
        reason="Set RUN_LIVE_TOOL_PRESENTATION=1 to call the configured model provider.",
    ),
]


# -- the tasks ------------------------------------------------------------------------------


@dataclass(frozen=True)
class Required:
    """A tool call the task needs, succeeded; ``where`` checks its inputs."""

    tool: str
    where: Callable[[dict], bool] = lambda _inputs: True


@dataclass(frozen=True)
class LiveTask:
    task_id: str
    prompt: str
    required: tuple[Required, ...]
    answer_has: tuple[str, ...] = ()
    seed: Callable[[Any], None] | None = None
    # Extra skill folders the team lead gets (custom_agents.strands_team.build_standard_skill_directories).
    skill_directories: tuple[str, ...] = ()


TASKS = (
    LiveTask(
        "calculate",
        "What is 17% of 2,340, plus 15.5? Use your calculator tool to compute it exactly, then give the number.",
        (Required("calculator"),),
        answer_has=("413.3",),
    ),
    LiveTask(
        "agent_types",
        "Which agent types can you start that are meant for web research or for comparing research? "
        "Check with your tools and give their exact type names.",
        (Required("get_available_agent_types"),),
        answer_has=("WebResearchAgent", "ResearchComparisonAgent"),
    ),
    LiveTask(
        "own_model",
        "Use your tools to check your own status or runtime configuration: which model id are you running on? "
        "Give the exact id.",
        (),
        answer_has=("{model_id}",),
    ),
    LiveTask(
        "three_counts",
        "Check each of these with your tools and give me the three numbers: how many tools or tool sets you hold "
        "access to, how many conversation threads you know of, and how many archived tasks there are.",
        (Required("list_my_access"), Required("list_conversations"), Required("list_archived_tasks")),
    ),
    LiveTask(
        "settings_options",
        "Which runtime settings could you stage a change for? Look them up with your tools and name three.",
        (Required("list_runtime_settings_options"),),
    ),
    LiveTask(
        "remember_fact",
        "Remember this for the whole team as a team memory fact: the office closes at 6pm on Fridays.",
        (Required("memory", lambda inputs: inputs.get("action") == "create" and inputs.get("bank") == "coremem"),),
    ),
    LiveTask(
        "skill_heartbeat_audit",
        "Use the heartbeat-checklist-audit skill to audit the team's heartbeat checklist, and tell me how many items it has.",
        (
            Required("skills", lambda inputs: inputs.get("skill_name") == "heartbeat-checklist-audit"),
            Required("list_heartbeat_items"),
        ),
        skill_directories=(str(TEST_SKILLS),),
    ),
)


# -- watching a run ---------------------------------------------------------------------------


class Watch(HookProvider):
    """Model calls with their tool counts, and every tool call with its inputs and outcome."""

    def __init__(self) -> None:
        self.request_sizes: list[int] = []
        self.calls: list[dict[str, Any]] = []
        self._via_call_tool: set[str] = set()  # toolUseIds the model sent as call_tool

    def register_hooks(self, registry: HookRegistry, **_kw) -> None:
        registry.add_callback(BeforeModelCallEvent, self._before_model, order=1000)
        registry.add_callback(BeforeToolCallEvent, self._before_tool, order=WATCH_BEFORE_TOOL_ORDER)
        registry.add_callback(AfterToolCallEvent, self._after_tool)

    def _before_tool(self, event: BeforeToolCallEvent) -> None:
        if event.tool_use.get("name") == CALL_TOOL_NAME:
            self._via_call_tool.add(str(event.tool_use.get("toolUseId") or ""))

    def _before_model(self, event: BeforeModelCallEvent) -> None:
        self.request_sizes.append(len(event.agent.tool_registry.get_all_tool_specs()))
        if len(self.request_sizes) > MAX_MODEL_CALLS:
            event.cancel = "Live comparison exceeded its model-call bound."

    def _after_tool(self, event: AfterToolCallEvent) -> None:
        from custom_tools.tool_script_context import in_tool_script

        from custom_agents.tool_use_unwrap import resolve_tool_use

        result = event.result or {}
        ok = _call_ok(result)
        via_call_tool = str(event.tool_use.get("toolUseId") or "") in self._via_call_tool
        self._via_call_tool.discard(str(event.tool_use.get("toolUseId") or ""))
        # A call_tool that was cancelled before the rewrite still carries call_tool; record its target.
        resolved = resolve_tool_use(event.tool_use)
        self.calls.append({
            "tool": resolved.name or str(event.tool_use.get("name") or ""),
            "input": resolved.inputs,
            "ok": ok,
            "via_call_tool": via_call_tool,
            # What a failed call said and was given, kept short, to explain failures in the report.
            "sample": "" if ok else json.dumps({"input": event.tool_use.get("input"), "result": result.get("content")}, default=str)[:700],
            "unknown": event.selected_tool is None,
            "from_script": in_tool_script(),
        })


def call_counts(calls: list[dict[str, Any]]) -> dict[str, int]:
    """How many calls went through call_tool, and how many were use_tools and describe_tools (report fields)."""

    return {
        "call_tool_calls": sum(1 for call in calls if call["via_call_tool"]),
        "use_tools_calls": sum(1 for call in calls if call["tool"] == "use_tools"),
        "describe_tools_calls": sum(1 for call in calls if call["tool"] == "describe_tools"),
    }


def _call_ok(result: dict) -> bool:
    """A call succeeded: success status and no error or refusal in its JSON answer."""

    if result.get("status") != "success":
        return False
    for block in result.get("content", []):
        text = block.get("text") if isinstance(block, dict) else None
        try:
            value = json.loads(text) if text else None
        except ValueError:
            continue
        if isinstance(value, dict) and (value.get("error") or value.get("status") in {"error", "refused"}):
            return False
    return True


# -- one live agent -----------------------------------------------------------------------------


def _settings() -> tuple[str, dict[str, str]]:
    base = str(os.getenv("TEAM_NATS_PROVIDER_SMOKE_API_BASE") or "").strip()
    env_path = Path(os.getenv("TEAM_NATS_LIVE_ENV", str(PROJECT_ROOT / "debug.env"))).expanduser()
    values = {k: str(v or "") for k, v in dotenv_values(env_path).items()} if env_path.is_file() else {}
    if not base or not values.get("LITELLM_API_KEY"):
        pytest.skip("Set TEAM_NATS_PROVIDER_SMOKE_API_BASE and TEAM_NATS_LIVE_ENV with LITELLM_API_KEY.")
    if not CEDAR_BINARY.is_file():
        pytest.skip("build ua-cedar-authorizer first (cargo build)")
    return base, values


def team_lead(tmp_path: Path, monkeypatch, model_id: str, presentation: str, skill_directories: tuple[str, ...] = ()):
    from custom_agents import access_runtime
    from custom_agents.model_keys import ModelKeys
    from custom_agents.strands_model_factory import StrandsModelFactoryConfig, build_strands_model
    from custom_agents.strands_team import AGENT_TYPE_MAP, create_strands_agent
    from custom_messages.contracts.routing_v1 import MessageHeader, MessageMeta
    from custom_messages.contracts.task_v1 import Task
    from custom_messages.enums import MessageMetaType
    from tests.broker_access_support import context_signer, context_verify_keys
    from tests.task_work_support import bind_task, set_agent_ref

    base, values = _settings()
    monkeypatch.setenv("ACCESS_CONTEXT_VERIFY_KEYS", context_verify_keys())
    access_runtime._runtime_access.cache_clear()
    model = build_strands_model(
        StrandsModelFactoryConfig(
            provider="litellm", model_id=model_id, litellm_api_base=base, max_output_tokens=2_000,
            model_keys=ModelKeys(None, env_lookup=values.get),
            request_timeout_seconds=120, stream_idle_timeout_seconds=90, model_call_timeout_seconds=180,
        )
    )
    monkeypatch.setenv("TEAM_INSTANCE_ROOT", str(tmp_path / "instance"))
    config = dict(AGENT_TYPE_MAP["TeamArchitect"])
    config.update({
        "team_id": "team_live", "is_team_lead": True, "named_agent_type": "TeamArchitect",
        "authorization_config": {"enabled": True, "cedar_binary_path": str(CEDAR_BINARY)},
        "task_history_db_path": str(tmp_path / "t.db"), "storage_sqlite_path": str(tmp_path / "t.db"),
        "instance_root": str(tmp_path / "instance"),
        "model_provider": "litellm", "model_id": model_id, "litellm_api_base": base,
        "tool_presentation": presentation,
        "additional_skill_directories": [*(config.get("additional_skill_directories") or []), *skill_directories],
    })
    with patch("custom_agents.strands_agent.UniversalAgent._configure_model", return_value=model):
        agent = create_strands_agent(config)
    set_agent_ref(agent)
    meta = MessageMeta(conversation_id="conv-live")
    meta.gateway_context = type("Gateway", (), {})()
    meta.gateway_context.task_context = context_signer().issue(
        team_id="team_live", person_id="usr_live_owner", person_roles=("owner",)
    )
    # The task's owner is the signed person, as the gateway admits it, so the change ledger records the
    # request and its writes (a team memory fact) are accepted (storage.change_ledger).
    agent.task_manager.add_task(
        Task(task_id="task-live", task_type=MessageMetaType.REQUEST), MessageHeader(), meta, owner_person_id="usr_live_owner"
    )
    bind_task("task-live")
    agent._conversation_store.ensure_thread(
        conversation_id="conv-live", team_id="team_live", owner_agent_id=agent.id, owner_agent_name=agent.name,
        requester_name="Live test owner", requester_id="usr_live_owner", root_task_id="task-live",
        owner_canonical_user_id="usr_live_owner",
    )
    watch = Watch()
    agent._strands_agent.hooks.add_hook(watch)
    return agent, watch


def ask(agent, text: str):
    from custom_agents.sdk.strands.turn import _turn_decision_initial_guidance

    decision_model = agent.turn_executor._turn_decision_structured_output_model()
    return agent._strands_agent(
        "This is task task-live in conversation conv-live for usr_live_owner. "
        + text
        + " "
        + _turn_decision_initial_guidance(decision_model.__name__),
        structured_output_model=decision_model,
        structured_output_prompt=agent.turn_executor._turn_decision_structured_output_prompt(),
    )


def _answer(result) -> str:
    try:
        decision = result.structured_output.to_runtime_decision()
        return str(decision.actions[-1].task.answer or "")
    except Exception:  # noqa: BLE001 - a missing answer is recorded as a failed run
        return str(result)


def _usage(result) -> dict[str, int]:
    usage = dict(getattr(getattr(result, "metrics", None), "accumulated_usage", {}) or {})
    return {key: int(usage.get(key, 0) or 0) for key in ("inputTokens", "outputTokens", "totalTokens", "cacheReadInputTokens")}


def _succeeded(task: LiveTask, calls: list[dict[str, Any]], answer: str, model_id: str) -> tuple[bool, list[str]]:
    missing = [
        need.tool for need in task.required
        if not any(call["tool"] == need.tool and call["ok"] and need.where(call["input"]) for call in calls)
    ]
    absent = [text for text in (item.replace("{model_id}", model_id) for item in task.answer_has) if text.lower() not in answer.lower()]
    return not missing and not absent, [*(f"call:{name}" for name in missing), *(f"answer:{text}" for text in absent)]


def _models() -> list[str]:
    return [item.strip() for item in str(os.getenv("TOOL_PRESENTATION_MODELS") or "").split(",") if item.strip()]


def _tasks() -> list[LiveTask]:
    wanted = {item.strip() for item in str(os.getenv("TOOL_PRESENTATION_TASKS") or "").split(",") if item.strip()}
    return [task for task in TASKS if not wanted or task.task_id in wanted]


@pytest.mark.parametrize("presentation", TOOL_PRESENTATIONS)
def test_live_tool_presentation_comparison(tmp_path, monkeypatch, presentation: str) -> None:
    from custom_agents import access_runtime

    models = _models()
    report = Path(os.getenv("TOOL_PRESENTATION_REPORT") or "").expanduser()
    if not models or not str(report):
        pytest.skip("Set TOOL_PRESENTATION_MODELS and TOOL_PRESENTATION_REPORT.")
    repeats = int(os.getenv("TOOL_PRESENTATION_REPEATS") or "1")
    report.parent.mkdir(parents=True, exist_ok=True)
    finished = 0
    for model_id in models:
        for task in _tasks():
            for repeat in range(repeats):
                run_dir = tmp_path / f"{model_id.replace('/', '_')}-{task.task_id}-{repeat}"
                run_dir.mkdir(parents=True)
                row: dict[str, Any] = {
                    "model": model_id, "presentation": presentation, "task": task.task_id, "repeat": repeat,
                }
                started = time.monotonic()
                watch = None
                try:
                    agent, watch = team_lead(run_dir, monkeypatch, model_id, presentation, task.skill_directories)
                    if task.seed is not None:
                        task.seed(agent)
                    row["system_prompt_chars"] = len(agent._strands_agent.system_prompt or "")
                    result = ask(agent, task.prompt)
                    answer = _answer(result)
                    ok, misses = _succeeded(task, watch.calls, answer, model_id)
                    row.update({"success": ok, "misses": misses, "answer": answer[:400], **_usage(result)})
                except Exception as exc:  # noqa: BLE001 - one failed run is a row, not the end of the comparison
                    row.update({"success": False, "misses": ["run_failed"], "error": f"{type(exc).__name__}: {exc}"[:400]})
                finally:
                    access_runtime._runtime_access.cache_clear()
                row["seconds"] = round(time.monotonic() - started, 1)
                if watch is not None:
                    row.update({
                        "model_calls": len(watch.request_sizes),
                        "first_request_tools": watch.request_sizes[0] if watch.request_sizes else None,
                        "tools": [call["tool"] for call in watch.calls],
                        "script_calls": sum(1 for call in watch.calls if call["from_script"]),
                        **call_counts(watch.calls),
                        "failed_calls": sum(1 for call in watch.calls if not call["ok"]),
                        "unknown_calls": sum(1 for call in watch.calls if call["unknown"]),
                        "failure_samples": [call["sample"] for call in watch.calls if call["sample"]][:3],
                    })
                with report.open("a", encoding="utf-8") as handle:
                    handle.write(json.dumps(row) + "\n")
                finished += 1
    assert finished, "no runs"
