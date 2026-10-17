"""Run real shell commands inside real Strands turns, for the tool-stop tests.

A scripted model asks for the real ``shell`` tool; Strands runs it in a worker
thread exactly as in production (``asyncio.to_thread``), and the commands go
through the real sandbox and process supervisor. Each command writes its
process id to a file first, so a test can check whether it still runs.
"""

from __future__ import annotations

import asyncio
import json
import os
import shlex
from pathlib import Path
from typing import Any

from strands import Agent
from strands.models.model import Model

from custom_tools.non_interactive_shell import shell
from runtime_settings.instance_layout import InstanceLayout


def agents_folder(root: Path, monkeypatch: Any) -> Path:
    """An instance whose shell commands run in ``agents/`` (work for no person)."""

    layout = InstanceLayout.at(root / "instance")
    layout.ensure_directories()
    monkeypatch.setenv("AGENT_WORKSPACE_DIR", str(layout.workspaces_dir))
    monkeypatch.setenv("TEAM_INSTANCE_ROOT", str(layout.root))
    return layout.agents_dir


def long_command(pid_file: Path, seconds: int = 60) -> str:
    """A shell command that records its process id, then sleeps in that same process."""

    return f"echo $$ > {shlex.quote(pid_file.name)}; exec sleep {seconds}"


def process_alive(pid: int) -> bool:
    """Whether a process runs; a zombie waiting for its parent counts as ended."""

    try:
        fields = Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()
    except (FileNotFoundError, ProcessLookupError, IndexError):
        return False
    return fields[0] != "Z"


async def wait_for_pid(pid_file: Path, timeout: float = 20.0) -> int:
    """The process id a long_command wrote, once the command has started."""

    deadline = asyncio.get_running_loop().time() + timeout
    while asyncio.get_running_loop().time() < deadline:
        text = pid_file.read_text().strip() if pid_file.exists() else ""
        if text.isdigit() and process_alive(int(text)):
            return int(text)
        await asyncio.sleep(0.05)
    raise AssertionError(f"The command never started: {pid_file}")


def kill_if_alive(pid_file: Path) -> None:
    """Test cleanup: never leave a command behind when an assertion failed."""

    text = pid_file.read_text().strip() if pid_file.exists() else ""
    if text.isdigit() and process_alive(int(text)):
        os.kill(int(text), 9)


class ScriptedShellModel(Model):
    """A model whose first reply asks for one ``shell`` call per command, in one message.

    Several calls in one message run side by side (Strands' default
    ConcurrentToolExecutor). Later replies end the turn with text.
    """

    def __init__(self, commands: list[str], work_dir: Path) -> None:
        self.commands = commands
        self.work_dir = work_dir
        self.calls = 0

    def update_config(self, **config: Any) -> None:
        pass

    def get_config(self) -> dict[str, Any]:
        return {"model_id": "scripted-shell"}

    async def structured_output(self, *args: Any, **kwargs: Any):
        raise AssertionError("No structured output expected")
        yield

    async def stream(self, *args: Any, **kwargs: Any):
        self.calls += 1
        yield {"messageStart": {"role": "assistant"}}
        if self.calls == 1:
            for index, command in enumerate(self.commands):
                tool_input = {"command": command, "work_dir": str(self.work_dir)}
                yield {"contentBlockStart": {"start": {"toolUse": {"toolUseId": f"shell-{index}", "name": "shell"}}}}
                yield {"contentBlockDelta": {"delta": {"toolUse": {"input": json.dumps(tool_input)}}}}
                yield {"contentBlockStop": {}}
            yield {"messageStop": {"stopReason": "tool_use"}}
            return
        yield {"contentBlockDelta": {"contentBlockIndex": 0, "delta": {"text": "done"}}}
        yield {"contentBlockStop": {"contentBlockIndex": 0}}
        yield {"messageStop": {"stopReason": "end_turn"}}


def shell_agent(commands: list[str], work_dir: Path) -> Agent:
    """A real Strands agent with the real shell tool and a scripted model."""

    return Agent(model=ScriptedShellModel(commands, work_dir), tools=[shell], callback_handler=None)
