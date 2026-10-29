from __future__ import annotations


import ast
import logging
import sys
import time
from pathlib import Path

import pytest

from custom_tools.non_interactive_shell import (
    OPENSHELL_RTK_FALLBACK_WARNING,
    RTK_FALLBACK_WARNING,
    _execute_commands,
    _execute_single_command,
    _extract_openshell_rtk_mode,
    _openshell_rtk_wrapped_command,
    _validate_command_path_tokens,
)
from custom_agents.code_access import REASON_NO_AGENT, embedded_commands
from custom_tools.path_policy import WORKSPACE_PATH_DENIED, PathPolicyError
from runtime_hosting.process_run import PROCESS_RUN_ID_ENV
from tests.task_work_support import set_agent_ref
from runtime_settings.instance_layout import InstanceLayout
from custom_tools.shell_execution import (
    ProcessRequest,
    run_process,
    validate_environment_keys,
)


@pytest.fixture
def agents_workspace(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, commands_without_agent) -> Path:
    """The default folder of a task that works for no person.

    Agent shell commands may only run inside the task's folders under
    ``<instance_root>/workspaces``; with no agent reference the policy reads
    ``TEAM_INSTANCE_ROOT`` and ``AGENT_WORKSPACE_DIR``.
    """

    layout = InstanceLayout.at(tmp_path / "instance")
    layout.ensure_directories()
    monkeypatch.setenv("AGENT_WORKSPACE_DIR", str(layout.workspaces_dir))
    monkeypatch.setenv("TEAM_INSTANCE_ROOT", str(layout.root))
    set_agent_ref(None)
    yield layout.agents_dir
    set_agent_ref(None)


def test_execution_core_imports_only_the_python_standard_library() -> None:
    module_path = (
        Path(__file__).resolve().parent.parent / "custom_tools" / "shell_execution.py"
    )
    tree = ast.parse(module_path.read_text(encoding="utf-8"))
    imported_roots: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported_roots.update(alias.name.split(".", 1)[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported_roots.add(node.module.split(".", 1)[0])

    assert imported_roots <= sys.stdlib_module_names | {"__future__"}


def test_process_supervisor_rejects_invalid_environment_names() -> None:
    with pytest.raises(ValueError, match="Invalid environment variable name"):
        validate_environment_keys({"INVALID-NAME": "value"})


def test_openshell_wrapper_reports_missing_rtk(tmp_path: Path) -> None:
    marker = "__UA_TEST_RTK_MODE__="
    result = run_process(
        ProcessRequest(
            argv=(
                "/bin/sh",
                "-lc",
                "PATH=/usr/bin:/bin; "
                + _openshell_rtk_wrapped_command("printf raw-output", marker),
            ),
            cwd=str(tmp_path),
            env={"HOME": str(tmp_path), "PATH": "/usr/bin:/bin"},
            timeout_seconds=5,
        )
    )
    _stdout_mode, stdout = _extract_openshell_rtk_mode(result.stdout, marker)
    stderr_mode, stderr = _extract_openshell_rtk_mode(result.stderr, marker)

    assert result.exit_code == 0
    assert stdout == "raw-output"
    assert stderr_mode == "missing"
    assert OPENSHELL_RTK_FALLBACK_WARNING not in stderr


def test_shell_result_separates_stderr_when_rtk_is_missing(
    agents_workspace: Path, monkeypatch
) -> None:
    monkeypatch.setenv("PATH", "/usr/bin:/bin")

    result = _execute_single_command(
        "printf stdout-value; printf stderr-value >&2; exit 9",
        default_work_dir=str(agents_workspace),
        default_timeout=5,
    )

    assert result["exit_code"] == 9
    assert result["output"] == "stdout-value"
    assert result["error"] == "stderr-value"
    assert result["rtk_mode"] == "missing"
    assert result["warning"] == RTK_FALLBACK_WARNING
    assert result["termination_reason"] == "exited"


def test_shell_path_validation_allows_exact_dev_null_redirect_with_separator(
    tmp_path: Path,
) -> None:
    _validate_command_path_tokens(
        "ls missing-path 2>/dev/null; printf done",
        cwd=str(tmp_path),
    )


def test_shell_path_validation_still_rejects_other_outside_paths(
    agents_workspace: Path,
) -> None:
    with pytest.raises(PathPolicyError, match="Path is outside this task's folders") as raised:
        _validate_command_path_tokens(
            "sed -n 1,20p /app/internal/SKILL.md",
            cwd=str(agents_workspace),
        )
    assert raised.value.reason_code == WORKSPACE_PATH_DENIED


def test_parallel_shell_failure_cancels_siblings_and_keeps_input_order(
    agents_workspace: Path,
) -> None:
    started = time.monotonic()

    results = _execute_commands(
        ["sleep 60", "exit 7"],
        parallel=True,
        ignore_errors=False,
        work_dir=str(agents_workspace),
        timeout=10,
    )

    assert [result["command"] for result in results] == ["sleep 60", "exit 7"]
    assert results[0]["termination_reason"] == "cancelled"
    assert results[1]["exit_code"] == 7
    assert time.monotonic() - started < 3


# --- commands with no agent behind them ---------------------------------------


def _no_agent_instance(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    layout = InstanceLayout.at(tmp_path / "instance")
    layout.ensure_directories()
    monkeypatch.setenv("AGENT_WORKSPACE_DIR", str(layout.workspaces_dir))
    monkeypatch.setenv("TEAM_INSTANCE_ROOT", str(layout.root))
    monkeypatch.delenv(PROCESS_RUN_ID_ENV, raising=False)
    set_agent_ref(None)
    return layout.agents_dir


def test_a_command_with_no_agent_behind_it_is_refused(tmp_path: Path, monkeypatch, caplog) -> None:
    """Without an agent nothing decides what a command may reach, so it must not run."""

    folder = _no_agent_instance(tmp_path, monkeypatch)
    caplog.set_level(logging.ERROR, logger="custom_tools.non_interactive_shell")

    result = _execute_single_command("touch ran.txt", default_work_dir=str(folder), default_timeout=5)

    assert result["exit_code"] == 126 and result["reason_code"] == REASON_NO_AGENT
    assert not (folder / "ran.txt").exists()
    assert "event=code_refused_without_agent tool_id=code:shell reason=not_embedded" in caplog.text


def test_embedded_use_may_run_commands_without_an_agent(tmp_path: Path, monkeypatch, caplog) -> None:
    folder = _no_agent_instance(tmp_path, monkeypatch)
    caplog.set_level(logging.WARNING, logger="custom_tools.non_interactive_shell")

    with embedded_commands():
        result = _execute_single_command("touch ran.txt", default_work_dir=str(folder), default_timeout=5)

    assert result["status"] == "success", result
    assert (folder / "ran.txt").exists()
    assert "event=code_access_without_agent tool_id=code:shell" in caplog.text


def test_a_supervised_runtime_process_refuses_commands_without_an_agent_even_when_embedded(
    tmp_path: Path, monkeypatch, caplog
) -> None:
    folder = _no_agent_instance(tmp_path, monkeypatch)
    monkeypatch.setenv(PROCESS_RUN_ID_ENV, "run-under-the-supervisor")
    caplog.set_level(logging.ERROR, logger="custom_tools.non_interactive_shell")

    with embedded_commands():
        result = _execute_single_command("touch ran.txt", default_work_dir=str(folder), default_timeout=5)

    assert result["reason_code"] == REASON_NO_AGENT
    assert not (folder / "ran.txt").exists()
    assert "event=code_refused_without_agent tool_id=code:shell reason=runtime_process" in caplog.text


def test_commands_never_get_the_agent_process_run_variables(monkeypatch) -> None:
    """Only the agent process reads them; a command with them would look like a supervised agent process."""

    from custom_tools.non_interactive_shell import _build_env

    monkeypatch.setenv(PROCESS_RUN_ID_ENV, "run-1")
    monkeypatch.setenv("UA_PROCESS_RUN_PROVEN", "1")
    monkeypatch.setenv("UA_PROCESS_RUN_LOCK_FD", "9")
    monkeypatch.setenv("SHELL_PASSTHROUGH_ENV_ALLOWLIST", f"{PROCESS_RUN_ID_ENV},UA_PROCESS_RUN_PROVEN")
    monkeypatch.setenv("UA_KEPT_FOR_COMMANDS", "yes")

    env = _build_env()

    assert not [key for key in env if key.startswith("UA_PROCESS_RUN_")]
    assert env["UA_KEPT_FOR_COMMANDS"] == "yes"


def test_a_command_does_not_see_the_process_run_variables(agents_workspace: Path, monkeypatch) -> None:
    monkeypatch.setenv("UA_PROCESS_RUN_PROVEN", "1")
    monkeypatch.setenv("UA_PROCESS_RUN_LOCK_FD", "9")

    result = _execute_single_command("env", default_work_dir=str(agents_workspace), default_timeout=5)

    assert result["status"] == "success", result
    assert "UA_PROCESS_RUN_" not in result["output"]
