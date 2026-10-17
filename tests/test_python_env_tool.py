from __future__ import annotations

import json
import threading
import time
import os
import sys
from pathlib import Path

import pytest


from custom_tools.python_env_tool import inspect_python_source, python_env
from tests.tool_stop_support import tool_context
from custom_tools.runtime_context import set_agent_ref
from runtime_settings.instance_layout import InstanceLayout


def _payload(result: dict) -> dict:
    return json.loads(result["content"][1]["text"])


def _python_wrapper(workspace: Path) -> Path:
    wrapper = workspace / "bin" / "python"
    wrapper.parent.mkdir(parents=True, exist_ok=True)
    wrapper.write_text(f"#!/bin/sh\nexec {sys.executable!r} \"$@\"\n", encoding="utf-8")
    wrapper.chmod(0o755)
    return wrapper


@pytest.fixture(autouse=True)
def _no_agent_reference(commands_without_agent):
    set_agent_ref(None)
    yield
    set_agent_ref(None)


def _instance_layout(tmp_path: Path, monkeypatch) -> InstanceLayout:
    """An instance whose ``workspaces/`` area is the agent's working area.

    With no agent reference the task works for no person, so its default
    folder, and the home of its Python environments, is ``workspaces/agents``.
    """

    layout = InstanceLayout.at(tmp_path / "instance")
    layout.ensure_directories()
    monkeypatch.setenv("AGENT_WORKSPACE_DIR", str(layout.workspaces_dir))
    monkeypatch.setenv("TEAM_INSTANCE_ROOT", str(layout.root))
    return layout


def _configure_workspace(tmp_path: Path, monkeypatch) -> Path:
    workspace = _instance_layout(tmp_path, monkeypatch).agents_dir
    python_wrapper = _python_wrapper(workspace)
    monkeypatch.setenv("PYTHON_ENV_PYTHON", str(python_wrapper))
    monkeypatch.setenv("SECURITY_LAYER_MODE", "broker")
    return workspace


def test_python_env_run_command_rejects_non_python_shell(tmp_path: Path, monkeypatch) -> None:
    _configure_workspace(tmp_path, monkeypatch)
    assert python_env._tool_func(tool_context=tool_context(), action="create", env_name="demo")["status"] == "success"

    run_result = python_env._tool_func(tool_context=tool_context(), action="run", env_name="demo", command="echo unsafe")
    run_payload = _payload(run_result)

    assert run_result["status"] == "error"
    assert run_payload["command_result"]["exit_code"] == 126
    assert "only supports Python interpreter commands" in run_payload["command_result"]["error"]


def test_python_env_run_command_rejects_inline_python(tmp_path: Path, monkeypatch) -> None:
    _configure_workspace(tmp_path, monkeypatch)
    assert python_env._tool_func(tool_context=tool_context(), action="create", env_name="demo")["status"] == "success"

    run_result = python_env._tool_func(tool_context=tool_context(), action="run", env_name="demo", command=".venv/bin/python -c 'print(42)'")
    run_payload = _payload(run_result)

    assert run_result["status"] == "error"
    assert run_payload["command_result"]["exit_code"] == 126
    assert "blocks inline and module execution" in run_payload["command_result"]["error"]


def test_python_env_run_command_inspects_python_script(tmp_path: Path, monkeypatch) -> None:
    _configure_workspace(tmp_path, monkeypatch)
    assert python_env._tool_func(tool_context=tool_context(), action="create", env_name="demo")["status"] == "success"
    write_result = python_env._tool_func(
        tool_context=tool_context(),
        action="write_file",
        env_name="demo",
        path="src/main.py",
        code="print('command mode ok')\n",
    )
    assert write_result["status"] == "success"

    run_result = python_env._tool_func(
        tool_context=tool_context(),
        action="run",
        env_name="demo",
        command=".venv/bin/python src/main.py '--literal && shell separator'",
    )
    run_payload = _payload(run_result)

    assert run_result["status"] == "success"
    assert run_payload["inspection"]["status"] == "ok"
    assert "command mode ok" in run_payload["command_result"]["output"]
    assert "'--literal && shell separator'" in run_payload["command_result"]["command"]


def test_python_env_inspection_reports_syntax_errors_and_risky_calls() -> None:
    syntax = inspect_python_source("def broken(:\n")
    assert syntax["status"] == "error"
    assert "SyntaxError" in syntax["errors"][0]

    risky = inspect_python_source("import subprocess\nsubprocess.run(['echo', 'ok'])\n")
    assert risky["status"] == "ok"
    assert any("subprocess" in warning for warning in risky["warnings"])


def test_python_env_rejects_protected_runtime_control_references(
    tmp_path: Path, monkeypatch
) -> None:
    _configure_workspace(tmp_path, monkeypatch)
    layout = InstanceLayout.at(tmp_path / "instance")
    assert python_env._tool_func(tool_context=tool_context(), action="create", env_name="demo")["status"] == "success"
    # Inspection refuses literal references to the instance's private areas
    # (here the startup roster in control/); the code sandbox is the boundary
    # for paths built at run time.
    source = (
        "from pathlib import Path\n"
        f"Path({str(layout.team_composition_path)!r}).write_text('changed', encoding='utf-8')\n"
    )
    write_result = python_env._tool_func(
        tool_context=tool_context(),
        action="write_file",
        env_name="demo",
        path="src/main.py",
        code=source,
    )
    run_result = python_env._tool_func(
        tool_context=tool_context(),
        action="run",
        env_name="demo",
        path="src/main.py",
    )

    assert write_result["status"] == "error"
    assert _payload(write_result)["inspection"]["status"] == "error"
    assert run_result["status"] == "error"
    assert "inspection failed" in _payload(run_result)["command_result"]["error"]
    assert not layout.team_composition_path.exists()


def test_python_env_resolves_relative_path_literals_from_the_env_folder(
    tmp_path: Path, monkeypatch
) -> None:
    _configure_workspace(tmp_path, monkeypatch)
    layout = InstanceLayout.at(tmp_path / "instance")
    created = python_env._tool_func(tool_context=tool_context(), action="create", env_name="demo")
    assert created["status"] == "success"
    env_dir = layout.agents_dir / "python_envs" / "demo"
    relative = os.path.relpath(layout.soul_path, env_dir)
    assert relative.startswith("..")

    closed = python_env._tool_func(
        tool_context=tool_context(),
        action="inspect", env_name="demo", code=f"open({relative!r}).read()\n"
    )
    shared = python_env._tool_func(
        tool_context=tool_context(),
        action="inspect", env_name="demo", code="open('../../notes.txt').read()\n"
    )

    assert _payload(closed)["inspection"]["status"] == "error"
    assert _payload(shared)["inspection"]["status"] == "ok"


def test_python_env_tool_schema_exposes_string_or_list_arguments() -> None:
    schema = python_env.tool_spec["inputSchema"]["json"]["properties"]

    assert schema["packages"]["anyOf"][0] == {"items": {"type": "string"}, "type": "array"}
    assert schema["packages"]["anyOf"][1] == {"type": "string"}
    assert schema["args"]["anyOf"][0] == {"items": {"type": "string"}, "type": "array"}
    assert schema["args"]["anyOf"][1] == {"type": "string"}


def test_stopping_the_turn_ends_a_running_python_script(tmp_path: Path, monkeypatch) -> None:
    """python_env runs scripts in a worker thread too; its turn's stop must end them."""

    from custom_tools.tool_stop import ToolStop
    from tests.turn_tool_stop_support import process_alive

    workspace = _configure_workspace(tmp_path, monkeypatch)
    assert python_env._tool_func(tool_context=tool_context(), action="create", env_name="demo")["status"] == "success"
    pid_file = workspace / "python_envs" / "demo" / "script.pid"
    script = "import os, time\nopen('script.pid', 'w').write(str(os.getpid()))\ntime.sleep(60)\n"
    written = python_env._tool_func(
        tool_context=tool_context(), action="write_file", env_name="demo", path="src/main.py", code=script
    )
    assert written["status"] == "success"
    stop = ToolStop(agent_id="agent", task_id="task")
    outcome: dict = {}
    runner = threading.Thread(
        target=lambda: outcome.update(
            python_env._tool_func(tool_context=tool_context(stop), action="run", env_name="demo", path="src/main.py")
        )
    )
    runner.start()
    deadline = time.monotonic() + 20
    while not (pid_file.exists() and pid_file.read_text().isdigit()) and time.monotonic() < deadline:
        time.sleep(0.05)
    pid = int(pid_file.read_text())

    stop.request("task_stopped")
    runner.join(6)

    assert not runner.is_alive()
    command_result = _payload(outcome)["command_result"]
    assert command_result["exit_code"] == 130
    assert "Command cancelled" in command_result["error"]
    assert not process_alive(pid)
    assert stop.running() == ()
