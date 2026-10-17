"""A turn's ToolStop ends the commands its tools run (custom_tools.tool_stop).

Real process supervisor, real sandbox, real shell and python_env tools; the
tools run in a worker thread, the way Strands runs synchronous tools.
"""

from __future__ import annotations

import asyncio
import logging
import os
import signal
import subprocess
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import SimpleNamespace

import pytest

from custom_tools import non_interactive_shell
from custom_tools.non_interactive_shell import shell
from custom_tools.runtime_context import set_agent_ref
from custom_tools.shell_execution import AnyCancel, ProcessRequest, run_process
from custom_agents.sdk.strands.turn_tool_stop import stop_tool_calls
from custom_tools.tool_stop import (
    TOOL_STOP_STATE_KEY,
    ToolStop,
    _read_stat,
    _StartedGroup,
    tool_stop_for,
    tracked_tool_call,
)
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
def work_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, commands_without_agent):
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
    assert "event=tool_stop_missing agent_id=None tool_name=shell tool_use_id=use-9" in caplog.text


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


def test_an_openshell_sandbox_is_not_prepared_for_a_command_after_the_stop(work_dir: Path, monkeypatch) -> None:
    prepared: list = []
    monkeypatch.setenv("SECURITY_LAYER_MODE", SECURITY_LAYER_MODE_OPENSHELL)
    monkeypatch.setattr(non_interactive_shell.shutil, "which", lambda *args, **kwargs: "/usr/bin/openshell")
    monkeypatch.setattr(non_interactive_shell, "ensure_sandbox", lambda *args: prepared.append(args) or "sandbox")
    stopped = threading.Event()
    stopped.set()

    result = non_interactive_shell._execute_single_command(
        "true", default_work_dir=str(work_dir), default_timeout=5, cancel_event=stopped
    )

    assert result["termination_reason"] == "cancelled"
    assert result["error"] == "Command not started because the task was stopped"
    assert prepared == []


def test_a_shell_call_that_starts_after_the_stop_runs_nothing(work_dir: Path) -> None:
    marker = work_dir / "ran.txt"
    stop = ToolStop(agent_id="agent", task_id="task")
    stop.request("task_stopped")

    result = shell._tool_func(command=f"touch {marker.name}", tool_context=tool_context(stop), work_dir=str(work_dir))

    assert result == {"status": "error", "content": [{"text": "Command not started because the task was stopped"}]}
    assert not marker.exists()


# --- what production hands tools -------------------------------------------


def test_a_tool_finds_its_turn_stop_in_both_shapes_strands_uses() -> None:
    """Strands nests the turn's invocation_state when the call has extra keywords (every turn)."""

    stop = ToolStop(agent_id="agent", task_id="task")
    flat = SimpleNamespace(invocation_state={TOOL_STOP_STATE_KEY: stop, "agent": object()})
    nested = SimpleNamespace(
        invocation_state={"callback_handler": print, "agent": object(), "invocation_state": {TOOL_STOP_STATE_KEY: stop}}
    )
    assert tool_stop_for(flat) is stop
    assert tool_stop_for(nested) is stop
    assert tool_stop_for(tool_context(stop)) is stop


# --- processes a command leaves behind -------------------------------------


BACKGROUND = "sh -c 'echo $$ > {pid}; exec sleep 60' > /dev/null 2>&1 & sleep 0.3; echo started"


@pytest.mark.asyncio
async def test_a_stop_ends_processes_a_finished_command_left_running(work_dir: Path, caplog) -> None:
    caplog.set_level(logging.INFO, logger="tests.tool_stop")
    stop = ToolStop(agent_id="agent", task_id="task")
    pid_file = work_dir / "bg.pid"
    result = await asyncio.to_thread(
        shell._tool_func, command=BACKGROUND.format(pid=pid_file.name), tool_context=tool_context(stop), work_dir=str(work_dir)
    )
    assert _results(result)[0]["Status"] == "success"
    pid = await wait_for_pid(pid_file)
    assert stop.running() == ()  # the call has returned; only its background process runs

    await stop_tool_calls(stop, logging.getLogger("tests.tool_stop"), reason="task_stopped")

    assert not process_alive(pid)
    assert "event=tool_background_processes_ended agent_id=agent task_id=task reason=task_stopped" in caplog.text
    assert f"pids={pid}" in caplog.text


def test_a_reused_process_group_number_is_never_signalled(tmp_path: Path) -> None:
    other = subprocess.Popen(["sleep", "60"], start_new_session=True)
    try:
        stop = ToolStop(agent_id="agent", task_id="task")
        started = _read_stat(other.pid)
        assert started is not None
        # A group recorded earlier whose number now belongs to this unrelated process.
        stop._groups.append(_StartedGroup(pgid=other.pid, leader_start=started.start - 1, tool_name="shell"))
        assert stop.signal_process_groups(signal.SIGKILL) == ()
        assert other.poll() is None

        stop.process_group_recorder("shell")(other.pid)
        [ended] = stop.signal_process_groups(signal.SIGKILL)
        assert ended.pgid == other.pid and other.pid in ended.pids
        assert other.wait(5) == -signal.SIGKILL
    finally:
        if other.poll() is None:
            other.kill()


def test_run_process_kills_a_command_that_ignores_sigterm_after_the_grace_period(tmp_path: Path) -> None:
    ready = tmp_path / "ready"
    script = (
        "import signal, time; signal.signal(signal.SIGTERM, signal.SIG_IGN); "
        f"open({str(ready)!r}, 'w').close(); time.sleep(60)"
    )
    stop = threading.Event()
    stopped_at: list[float] = []
    pids: list[int] = []

    def stop_when_ready() -> None:
        deadline = time.monotonic() + 10
        while not ready.exists() and time.monotonic() < deadline:
            time.sleep(0.02)
        stopped_at.append(time.monotonic())
        stop.set()

    threading.Thread(target=stop_when_ready, daemon=True).start()
    result = run_process(
        ProcessRequest(
            argv=(sys.executable, "-c", script),
            cwd=str(tmp_path),
            env={"PATH": "/usr/bin:/bin"},
            timeout_seconds=30,
            terminate_grace_seconds=1.0,
        ),
        cancel_event=stop,
        on_spawn=pids.append,
    )
    took = time.monotonic() - stopped_at[0]

    assert result.termination_reason == "cancelled"
    assert 0.9 <= took < 4, took  # SIGTERM was ignored; SIGKILL came after the one-second grace
    assert not process_alive(pids[0])


def test_python_env_does_nothing_once_its_turn_was_stopped(work_dir: Path) -> None:
    from custom_tools.python_env_tool import python_env

    stop = ToolStop(agent_id="agent", task_id="task")
    stop.request("task_stopped")

    result = python_env._tool_func(
        tool_context=tool_context(stop), action="write_file", env_name="demo", path="src/main.py", code="print(1)\n"
    )

    assert result["status"] == "error"
    assert "task was stopped" in result["content"][0]["text"]
    assert not (work_dir / "python_envs" / "demo").exists()


@pytest.mark.asyncio
async def test_parallel_commands_not_started_before_a_turn_stop_say_why(work_dir: Path, monkeypatch) -> None:
    # One worker, so the later commands are still waiting when the stop comes.
    monkeypatch.setattr(non_interactive_shell, "ThreadPoolExecutor", lambda: ThreadPoolExecutor(max_workers=1))
    stop = ToolStop(agent_id="agent", task_id="task")
    pid_file = work_dir / "first.pid"
    call = asyncio.create_task(
        asyncio.to_thread(
            shell._tool_func,
            command=[long_command(pid_file), "true", "true"],
            parallel=True,
            tool_context=tool_context(stop),
            work_dir=str(work_dir),
        )
    )
    await wait_for_pid(pid_file)

    stop.request("task_stopped")
    first, second, third = _results(await asyncio.wait_for(call, 6))

    assert first["Termination Reason"] == "cancelled"
    assert second["Error"] == third["Error"] == "Command not started because the task was stopped"


def test_a_command_that_already_exited_still_has_its_group_recorded(tmp_path: Path) -> None:
    """Its leader is a zombie until the supervisor collects it, with the real group and start time."""

    leader = subprocess.Popen(
        ["sh", "-c", "sleep 60 >/dev/null 2>&1 & echo $!"], stdout=subprocess.PIPE, start_new_session=True
    )
    background = int(leader.stdout.readline())
    try:
        deadline = time.monotonic() + 5
        while (info := _read_stat(leader.pid)) is not None and not info.zombie and time.monotonic() < deadline:
            time.sleep(0.02)
        assert info is not None and info.zombie  # exited, not yet collected
        stop = ToolStop(agent_id="agent", task_id="task")

        stop.process_group_recorder("shell")(leader.pid)
        [ended] = stop.signal_process_groups(signal.SIGKILL)

        assert ended.pids == (background,)
        deadline = time.monotonic() + 5
        while process_alive(background) and time.monotonic() < deadline:
            time.sleep(0.02)
        assert not process_alive(background)
    finally:
        if process_alive(background):
            os.kill(background, signal.SIGKILL)
        leader.wait()


def test_each_pass_reads_proc_once_however_many_groups_were_recorded(monkeypatch) -> None:
    from custom_tools import tool_stop as tool_stop_module

    scans: list[int] = []
    real_scan = tool_stop_module._scan_processes
    monkeypatch.setattr(tool_stop_module, "_scan_processes", lambda: scans.append(1) or real_scan())
    stop = ToolStop(agent_id="agent", task_id="task")
    for number in range(300):
        stop._groups.append(_StartedGroup(pgid=4_000_000 + number, leader_start=1, tool_name="shell"))

    assert stop.live_process_groups() == ()
    assert stop.signal_process_groups(signal.SIGTERM) == ()
    assert len(scans) == 2


@pytest.mark.asyncio
async def test_ending_leftovers_reads_proc_off_the_event_loop_and_kills_what_ignores_sigterm(monkeypatch) -> None:
    from custom_tools import tool_stop as tool_stop_module

    scans_on_loop: list[bool] = []
    real_scan = tool_stop_module._scan_processes

    def scan():
        scans_on_loop.append(threading.current_thread() is threading.main_thread())
        return real_scan()

    monkeypatch.setattr(tool_stop_module, "_scan_processes", scan)
    stubborn = subprocess.Popen(
        [sys.executable, "-c", "import signal, time; signal.signal(signal.SIGTERM, signal.SIG_IGN); time.sleep(60)"],
        start_new_session=True,
    )
    try:
        await asyncio.sleep(0.3)  # let it ignore SIGTERM before the stop
        stop = ToolStop(agent_id="agent", task_id="task")
        for number in range(200):
            stop._groups.append(_StartedGroup(pgid=4_000_000 + number, leader_start=1, tool_name="shell"))
        stop.process_group_recorder("shell")(stubborn.pid)

        await stop_tool_calls(stop, logging.getLogger("tests.tool_stop"), reason="task_stopped")

        assert stubborn.wait(5) == -signal.SIGKILL
        assert scans_on_loop and not any(scans_on_loop)
        # SIGTERM pass, about one check per 50 ms during the one-second grace, SIGKILL pass.
        assert len(scans_on_loop) <= 2 + 1.0 / 0.05 + 2
    finally:
        if stubborn.poll() is None:
            stubborn.kill()
