"""Runs one tool script inside the code sandbox; its only way out is two pipes to the agent's process.

Trusted code (``custom_agents.tool_script_runtime``) copies this file and the
script into a private scratch folder and starts it as
``python -I -S tool_script_runner.py <request fd> <reply fd> <script file>``
inside the Landlock sandbox with no folders and no network. It uses only the
standard library and imports nothing from Universal Agents.

The script is the body of a function: ``tools.<name>(**inputs)`` calls a tool
and ``return`` gives the result. Each call is one JSON line on the request
pipe; the agent's process checks and runs it and answers with one JSON line on
the reply pipe. A refused or failed call raises ``ToolError`` in the script,
which may catch it. When the script ends, the runner sends a ``done`` line
with the returned value, or an ``error`` line with the exception (line numbers
counted from the script's first line).

Limits the runner sets on itself before the script runs: address space and
CPU seconds (the agent's process also enforces the wall-clock limit and kills
the process group).
"""

from __future__ import annotations

import ast
import json
import os
import resource
import sys
import traceback
from collections.abc import Callable
from types import CodeType
from typing import Any

SCRIPT_FILENAME = "<tool script>"
FUNCTION_NAME = "__tool_script__"


class ToolError(Exception):
    """A tool call the agent's process refused or that failed; the message says why."""


class _Bridge:
    def __init__(self, request_fd: int, reply_fd: int) -> None:
        self._requests = os.fdopen(request_fd, "w", encoding="utf-8", buffering=1)
        self._replies = os.fdopen(reply_fd, "r", encoding="utf-8")
        self._next_id = 0

    def send(self, message: dict[str, Any]) -> None:
        self._requests.write(json.dumps(message, default=str) + "\n")
        self._requests.flush()

    def call(self, name: str, inputs: dict[str, Any]) -> object:
        self._next_id += 1
        self.send({"kind": "call", "id": self._next_id, "tool": name, "input": inputs})
        line = self._replies.readline()
        if not line:
            raise ToolError("The agent's process stopped answering tool calls.")
        reply = json.loads(line)
        if reply.get("ok"):
            return reply.get("result")
        raise ToolError(str(reply.get("error") or "The tool call failed."))


class _Tools:
    """``tools.<name>(**inputs)`` calls the agent's tool by name, keyword inputs only."""

    def __init__(self, bridge: _Bridge) -> None:
        self._bridge = bridge

    def __getattr__(self, name: str) -> Callable[..., object]:
        if name.startswith("_"):
            raise AttributeError(name)

        def call(*args: object, **inputs: Any) -> object:
            if args:
                raise ToolError(f"Give {name}'s inputs by name, for example tools.{name}(query='...').")
            return self._bridge.call(name, inputs)

        call.__name__ = name
        return call


def _limit(memory_bytes: int, cpu_seconds: int) -> None:
    resource.setrlimit(resource.RLIMIT_AS, (memory_bytes, memory_bytes))
    resource.setrlimit(resource.RLIMIT_CPU, (cpu_seconds, cpu_seconds))


def _compiled(source: str) -> CodeType:
    """The script as the body of a function, keeping the script's own line numbers.

    ``return`` at the top level parses (only compiling rejects it outside a
    function), so the parsed statements become the body of a function with no
    re-indenting, which would change multi-line strings.
    """

    module = ast.parse(source or "pass", filename=SCRIPT_FILENAME)
    arguments = ast.arguments(
        posonlyargs=[], args=[ast.arg("tools"), ast.arg("ToolError")], kwonlyargs=[], kw_defaults=[], defaults=[]
    )
    function = ast.FunctionDef(
        name=FUNCTION_NAME, args=arguments, body=module.body or [ast.Pass()], decorator_list=[], returns=None, type_params=[]
    )
    wrapped = ast.Module(body=[function], type_ignores=[])
    return compile(ast.fix_missing_locations(wrapped), SCRIPT_FILENAME, "exec")


def _error_text(exc: BaseException) -> str:
    """The exception with the script's line numbers and no runner frames."""

    frames = [frame for frame in traceback.extract_tb(exc.__traceback__) if frame.filename == SCRIPT_FILENAME]
    where = [f"script line {frame.lineno}" for frame in frames if frame.lineno]
    message = "".join(traceback.format_exception_only(type(exc), exc)).strip()
    return "\n".join([*where[-3:], message])


def main(argv: list[str]) -> int:
    request_fd, reply_fd, script_path = int(argv[1]), int(argv[2]), argv[3]
    memory_bytes, cpu_seconds = int(argv[4]), int(argv[5])
    bridge = _Bridge(request_fd, reply_fd)
    try:
        _limit(memory_bytes, cpu_seconds)
        with open(script_path, encoding="utf-8") as handle:
            code = _compiled(handle.read())
        namespace: dict[str, Any] = {"__name__": "__tool_script__", "__builtins__": __builtins__}
        exec(code, namespace)
        result = namespace[FUNCTION_NAME](_Tools(bridge), ToolError)
        sys.stdout.flush()
        sys.stderr.flush()
        bridge.send({"kind": "done", "result": result})
    except BaseException as exc:  # noqa: BLE001 - every failure goes back to the agent as text
        sys.stdout.flush()
        sys.stderr.flush()
        bridge.send({"kind": "error", "error": _error_text(exc)})
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
