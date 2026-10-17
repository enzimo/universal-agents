"""A turn's ToolStop ends the commands its tools run (custom_tools.tool_stop).

Real process supervisor, real sandbox, real shell and python_env tools; the
tools run in a worker thread, the way Strands runs synchronous tools.
"""

from __future__ import annotations

import asyncio
import logging
import threading
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from custom_tools import non_interactive_shell
from custom_tools.non_interactive_shell import shell
from custom_tools.runtime_context import set_agent_ref
from custom_tools.shell_execution import AnyCancel, ProcessRequest, run_process
from custom_tools.tool_stop import ToolStop, tracked_tool_call
from runtime_settings.security import SECURITY_LAYER_MODE_OPENSHELL
from tests.tool_stop_support import tool_context
from tests.turn_tool_stop_support import (
    agents_folder,
    kill_if_alive,
    long_command,
    process_alive,
    wait_for_pid,
)


@pytest.fixture
def work_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    set_agent_ref(None)
    folder = agents_folder(tmp_path, monkeypatch)
    yield folder
    for pid_file in folder.glob("*.pid"):
        kill_if_alive(pid_file)
    set_agent_ref(None)


def _results(result: dict) -> list[dict[str, str]]:
    """Each command's fields from the shell tool's text result."""

    parsed = []
    for block in result["content"][1:]:
        lines = block["text"].split("\n")
        parsed.append(dict(line.split(": ", 1) for line in lines if ": " in line and not line.startswith(" ")))
    return parsed


# --- the process supervisor -------------------------------------------------


def test_run_process_never_starts_a_command_after_a_stop(tmp_path: Path) -> None:
    stopped = threading.Event()
    stopped.set()
    marker = tmp_path / "ran"

    result = run_process(
        ProcessRequest(argv=("touch", str(marker)), cwd=str(tmp_path), env={"PATH": "/usr/bin:/bin"}, timeout_seconds=5),
        cancel_event=stopped,
    )

    assert result.termination_reason == "cancelled"
    assert result.exit_code == 130
    assert not marker.exists()


def test_run_process_ends_the_whole_process_group_when_stopped(tmp_path: Path) -> None:
    stop = threading.Event()
    child_pid_file = tmp_path / "child.pid"
    command = f"sleep 60 & echo $! > {child_pid_file}; wait"

    def stop_once_started() -> None:
        deadline = time.monotonic() + 10
        while not child_pid_file.exists() and time.monotonic() < deadline:
            time.sleep(0.02)
        stop.set()

    threading.Thread(target=stop_once_started, daemon=True).start()
    started = time.monotonic()
    result = run_process(
        ProcessRequest(argv=("sh", "-c", command), cwd=str(tmp_path), env={"PATH": "/usr/bin:/bin"}, timeout_seconds=30),
        cancel_event=stop,
    )

    assert result.termination_reason == "cancelled"
    assert time.monotonic() - started < 6
    assert not process_alive(int(child_pid_file.read_text()))


def test_any_cancel_is_set_when_either_signal_is() -> None:
    first, second = threading.Event(), threading.Event()
    combined = AnyCancel(first, None, second)
    assert not combined.is_set()
    second.set()
    assert combined.is_set()


# --- ToolStop ---------------------------------------------------------------


def test_a_tool_call_counts_as_running_until_it_returns() -> None:
    stop = ToolStop(agent_id="agent", task_id="task")
    with stop.tool_call("shell", "use-1") as event:
        assert event is stop.event
        [call] = stop.running()
        assert (call.tool_name, call.tool_use_id) == ("shell", "use-1")
    assert stop.running() == ()


def test_a_tool_call_that_starts_after_the_stop_sees_it() -> None:
    stop = ToolStop(agent_id="agent", task_id="task")
    stop.request("task_stopped")
    with stop.tool_call("shell", "use-1") as event:
        assert event.is_set()


def test_the_first_stop_reason_is_kept() -> None:
    stop = ToolStop(agent_id="agent", task_id="task")
    assert stop.request("lease_loss_handback") is True
    assert stop.request("turn_cancelled") is False
    assert stop.reason == "lease_loss_handback"


@pytest.mark.asyncio
async def test_wait_until_idle_returns_the_calls_still_running_at_the_timeout() -> None:
    stop = ToolStop(agent_id="agent", task_id="task")
    release = threading.Event()

    def busy() -> None:
        with stop.tool_call("slow_tool", "use-1"):
            release.wait(5)

    thread = threading.Thread(target=busy)
    thread.start()
    while not stop.running():
        await asyncio.sleep(0.01)
    still_running = await stop.wait_until_idle(0.2)
    assert [call.tool_name for call in still_running] == ["slow_tool"]
    release.set()
    assert await stop.wait_until_idle(5) == ()
    thread.join()


def test_a_command_tool_without_a_turn_stop_is_logged(caplog) -> None:
    caplog.set_level(logging.WARNING, logger="custom_tools.tool_stop")
    with tracked_tool_call(SimpleNamespace(invocation_state={}, tool_use={"toolUseId": "use-9"}), "shell") as event:
        assert event is None
    assert "event=tool_stop_missing tool_name=shell tool_use_id=use-9" in caplog.text


# --- the shell tool ---------------------------------------------------------


@pytest.mark.asyncio
async def test_stopping_the_turn_ends_a_running_shell_command(work_dir: Path) -> None:
    stop = ToolStop(agent_id="agent", task_id="task")
    pid_file = work_dir / "cmd.pid"
    call = asyncio.create_task(
        asyncio.to_thread(
            shell._tool_func, command=long_command(pid_file), tool_context=tool_context(stop), work_dir=str(work_dir)
        )
    )
    pid = await wait_for_pid(pid_file)
    assert len(stop.running()) == 1

    stop.request("task_stopped")
    result = await asyncio.wait_for(call, 6)

    [command] = _results(result)
    assert command["Termination Reason"] == "cancelled"
    assert command["Exit Code"] == "130"
    assert not process_alive(pid)
    assert stop.running() == ()


@pytest.mark.asyncio
async def test_parallel_shell_commands_stop_with_the_turn(work_dir: Path) -> None:
    stop = ToolStop(agent_id="agent", task_id="task")
    pid_files = [work_dir / "first.pid", work_dir / "second.pid"]
    call = asyncio.create_task(
        asyncio.to_thread(
            shell._tool_func,
            command=[long_command(path) for path in pid_files],
            parallel=True,
            ignore_errors=True,
            tool_context=tool_context(stop),
            work_dir=str(work_dir),
        )
    )
    pids = [await wait_for_pid(path) for path in pid_files]

    stop.request("task_stopped")
    result = await asyncio.wait_for(call, 6)

    assert [item["Termination Reason"] for item in _results(result)] == ["cancelled", "cancelled"]
    assert not any(process_alive(pid) for pid in pids)


@pytest.mark.asyncio
async def test_a_stopped_turn_starts_none_of_its_remaining_commands(work_dir: Path) -> None:
    stop = ToolStop(agent_id="agent", task_id="task")
    pid_file = work_dir / "cmd.pid"
    later = work_dir / "later.txt"
    call = asyncio.create_task(
        asyncio.to_thread(
            shell._tool_func,
            command=[long_command(pid_file), f"touch {later.name}"],
            ignore_errors=True,
            tool_context=tool_context(stop),
            work_dir=str(work_dir),
        )
    )
    await wait_for_pid(pid_file)

    stop.request("task_stopped")
    result = await asyncio.wait_for(call, 6)

    first, second = _results(result)
    assert first["Termination Reason"] == "cancelled"
    assert second["Termination Reason"] == "cancelled"
    assert second["Error"] == "Command not started because the task was stopped"
    assert not later.exists()


def test_an_openshell_sandbox_is_not_prepared_after_the_stop(work_dir: Path, monkeypatch) -> None:
    prepared: list = []
    monkeypatch.setenv("SECURITY_LAYER_MODE", SECURITY_LAYER_MODE_OPENSHELL)
    monkeypatch.setattr(non_interactive_shell.shutil, "which", lambda *args, **kwargs: "/usr/bin/openshell")
    monkeypatch.setattr(non_interactive_shell, "ensure_sandbox", lambda *args: prepared.append(args) or "sandbox")
    stop = ToolStop(agent_id="agent", task_id="task")
    stop.request("task_stopped")

    result = shell._tool_func(command="true", tool_context=tool_context(stop), work_dir=str(work_dir))

    assert _results(result)[0]["Termination Reason"] == "cancelled"
    assert prepared == []
