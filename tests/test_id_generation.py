from __future__ import annotations

import ast
import shutil
import subprocess
from pathlib import Path

import pytest

from custom_messages.contracts.routing_v1 import MessageHeader
from custom_messages.contracts.task_v1 import Task
from custom_messages.enums import MessageMetaType
from id_generation import (
    GENERATED_OPERATIONAL_ID_PREFIXES,
    MAX_OPERATIONAL_ID_PREFIX_LENGTH,
    new_id,
    operational_id,
)

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
RUNTIME_PYTHON_ROOTS = (
    "activity_collector",
    "assistant_gateway",
    "custom_agents",
    "custom_messages",
    "custom_tools",
    "custom_transport",
    "docker",
    "mcp",
    "scripts",
    "secure_tool_broker",
    "secure_tools",
    "storage",
    "tests",
)
ALLOWED_DIRECT_UUID4_CALLERS = {
    (
        "assistant_gateway/interactive_apps/website_handlers/configuration.py",
        "_write_text_atomic",
    ),
    ("assistant_gateway/onboarding_web/server.py", "_handle_xai_device_login_start"),
    ("custom_agents/agent_profile_service.py", "update_team_lead_soul"),
    ("custom_agents/external_skills.py", "_apply_staged_skills"),
    ("custom_agents/external_skills.py", "_write_manifest"),
    ("custom_tools/non_interactive_shell.py", "_execute_with_openshell"),
    (
        "tests/live_gateway_scenarios.py",
        "run_test_54",
    ),
    (
        "tests/test_live_nats_replacement_semantics.py",
        "test_live_duplicate_nats_message_id_suppresses_duplicate_work",
    ),
    (
        "tests/test_live_nats_replacement_semantics.py",
        "test_live_task_steering_routes_to_active_agent",
    ),
    (
        "tests/test_nats_integration.py",
        "test_nats_object_store_round_trips_attachment_bytes",
    ),
}


@pytest.mark.parametrize("prefix", ["", None, 123])
def test_operational_id_prefix_must_be_a_non_empty_string(prefix: object) -> None:
    with pytest.raises(ValueError, match="non-empty string"):
        new_id(prefix)  # type: ignore[arg-type]


def test_existing_short_operational_ids_remain_accepted() -> None:
    header = MessageHeader(message_id="msg_id_ff0ec88e")
    task = Task(
        task_id="task_id_ff0ec88e",
        task_type=MessageMetaType.REQUEST,
        problem_statement="Load a persisted task that predates the 12-character suffix.",
    )

    assert header.message_id == "msg_id_ff0ec88e"
    assert task.task_id == "task_id_ff0ec88e"


def test_operational_id_prefix_length_is_bounded() -> None:
    accepted_prefix = "x" * MAX_OPERATIONAL_ID_PREFIX_LENGTH

    assert new_id(accepted_prefix).startswith(f"{accepted_prefix}_")
    with pytest.raises(ValueError, match="at most 15 characters"):
        new_id("x" * (MAX_OPERATIONAL_ID_PREFIX_LENGTH + 1))


def test_registered_generated_id_prefixes_respect_the_shared_bound() -> None:
    assert GENERATED_OPERATIONAL_ID_PREFIXES
    assert all(
        0 < len(prefix) <= MAX_OPERATIONAL_ID_PREFIX_LENGTH
        for prefix in GENERATED_OPERATIONAL_ID_PREFIXES
    )


def test_deterministic_operational_id_uses_the_same_function_level_boundary() -> None:
    accepted_prefix = "x" * MAX_OPERATIONAL_ID_PREFIX_LENGTH

    assert operational_id(accepted_prefix, "stable") == f"{accepted_prefix}_stable"
    assert operational_id("index", 0) == "index_0"
    with pytest.raises(ValueError, match="at most 15 characters"):
        operational_id("x" * (MAX_OPERATIONAL_ID_PREFIX_LENGTH + 1), "stable")
    with pytest.raises(ValueError, match="suffix must be non-empty"):
        operational_id("stable", " ")


def test_literal_operational_id_prefixes_respect_the_shared_bound() -> None:
    violations: list[str] = []

    for root_name in RUNTIME_PYTHON_ROOTS:
        for path in (REPOSITORY_ROOT / root_name).rglob("*.py"):
            tree = ast.parse(path.read_text(encoding="utf-8"))
            for node in ast.walk(tree):
                if not isinstance(node, ast.Call) or not node.args:
                    continue
                function = node.func
                function_name = (
                    function.id
                    if isinstance(function, ast.Name)
                    else function.attr
                    if isinstance(function, ast.Attribute)
                    else None
                )
                if function_name not in {"new_id", "operational_id"}:
                    continue
                prefix = node.args[0]
                if not (
                    isinstance(prefix, ast.Constant)
                    and isinstance(prefix.value, str)
                    and len(prefix.value) > MAX_OPERATIONAL_ID_PREFIX_LENGTH
                ):
                    continue
                violations.append(
                    f"{path.relative_to(REPOSITORY_ROOT)}:{node.lineno}:{prefix.value}"
                )

    assert violations == []


def test_runtime_uuid4_calls_are_limited_to_non_operational_exceptions() -> None:
    direct_callers: set[tuple[str, str]] = set()

    for root_name in RUNTIME_PYTHON_ROOTS:
        for path in (REPOSITORY_ROOT / root_name).rglob("*.py"):
            tree = ast.parse(path.read_text(encoding="utf-8"))
            parents: dict[ast.AST, ast.AST] = {}
            for parent in ast.walk(tree):
                for child in ast.iter_child_nodes(parent):
                    parents[child] = parent
            for node in ast.walk(tree):
                if not _is_uuid4_call(node):
                    continue
                owner = _enclosing_function_name(node, parents)
                relative_path = path.relative_to(REPOSITORY_ROOT).as_posix()
                direct_callers.add((relative_path, owner))

    assert direct_callers == ALLOWED_DIRECT_UUID4_CALLERS


def test_browser_operational_ids_use_the_shared_secure_helper() -> None:
    chat_source = (
        REPOSITORY_ROOT / "assistant_gateway/onboarding_web/assets/chat.js"
    ).read_text(encoding="utf-8")
    site_tool_source = (
        REPOSITORY_ROOT / "custom_tools/single_page_site_tools.py"
    ).read_text(encoding="utf-8")
    browser_id_source = (
        REPOSITORY_ROOT / "assistant_gateway/onboarding_web/assets/operational_ids.js"
    ).read_text(encoding="utf-8")

    assert "Math.random" not in chat_source
    assert "Math.random" not in site_tool_source
    assert 'newId("browser")' in chat_source
    assert 'newId("thread")' in chat_source
    assert 'newId("browser")' in site_tool_source
    assert "function newId(prefix)" in browser_id_source
    assert "MAX_OPERATIONAL_ID_PREFIX_LENGTH = 15" in browser_id_source
    assert "var bytes = new Uint8Array(6)" in browser_id_source


def test_browser_operational_id_helper_emits_twelve_lowercase_hex_characters() -> None:
    node = shutil.which("node")
    if node is None:
        pytest.skip("node is required to execute the browser identifier helper")
    asset_path = (
        REPOSITORY_ROOT / "assistant_gateway/onboarding_web/assets/operational_ids.js"
    )
    script = """
const fs = require("fs");
const vm = require("vm");
const source = fs.readFileSync(process.argv[1], "utf8");
const sandbox = {
  window: {
    crypto: {
      getRandomValues(bytes) {
        for (let index = 0; index < bytes.length; index += 1) bytes[index] = index + 1;
        return bytes;
      },
    },
  },
};
vm.createContext(sandbox);
vm.runInContext(source, sandbox, { filename: "operational_ids.js" });
process.stdout.write(sandbox.window.UAOperationalIds.newId("thread"));
"""

    result = subprocess.run(
        [node, "-e", script, str(asset_path)],
        text=True,
        capture_output=True,
        check=True,
    )

    assert result.stdout == "thread_010203040506"


def _is_uuid4_call(node: ast.AST) -> bool:
    return (
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "uuid4"
        and isinstance(node.func.value, ast.Name)
        and node.func.value.id == "uuid"
    )


def _enclosing_function_name(
    node: ast.AST,
    parents: dict[ast.AST, ast.AST],
) -> str:
    current = node
    while current in parents:
        current = parents[current]
        if isinstance(current, (ast.FunctionDef, ast.AsyncFunctionDef)):
            return current.name
    return "<module>"
