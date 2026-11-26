"""Running a tool script: a short sandboxed Python script that calls the agent's read-level tools.

Why: with a tool index (``custom_agents.tool_index``) most tools wait outside
the model's tool list, and work like "list these, filter them, read the
details of three" costs one model call per step, each carrying every earlier
result. A tool script does those steps in one call and returns only what the
model needs. The idea follows Cloudflare's Code Mode, without MCP: UA's tools
run inside the agent's process with the task's signed context, so the script
reaches them through a pipe to that process instead of a network service that
would need its own credential and a second place where access is decided.

What a script can reach (ua-architecture ``plans/tool_index_and_scripts_plan.md``):

- **Nothing directly.** It runs in the Landlock sandbox
  (``custom_tools.code_sandbox``) with no task folders, no granted folders and
  the network closed; its only way out is two inherited pipes. No credential
  exists for it to read. When the operator turned the sandbox off, or the
  kernel cannot close the network, scripts are refused: running one
  unconfined would give every agent arbitrary code.
- **Only read-level calls, checked here first** (``check_call``): the function
  must be one the agent holds for this work, offered by the operator's tool
  settings, callable from scripts (``custom_agents.tool_index.script_inputs``:
  read level for these inputs; dispatchers and tool loading never), within
  the call limit.
- **Then decided like a direct call.** An allowed call runs through Strands'
  tool executor on the turn's own Strands agent, the path a model's call
  takes: the access hook decides it, the activity trail, call budget and other
  hooks see it. It is marked as a script's call
  (``custom_tools.tool_script_context``) so that its group is not loaded into
  the model's list and its result goes to the script whole. A call that would
  pause for a person cannot pause inside a script; it fails and the model
  makes it directly.

Limits: wall-clock seconds, inner calls, each inner result's size, printed
output, memory and CPU (set by the runner). The result lists every inner call
and how it ended, and a stopped task kills the script's process group
(``custom_tools.tool_stop``).
"""

from __future__ import annotations

import json
import logging
import os
import queue
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, cast

logger = logging.getLogger(__name__)

TOOL_SCRIPT_NAME = "run_tool_script"
RUNNER_FILE = "tool_script_runner.py"
SCRIPT_FILE = "script.py"


@dataclass(frozen=True)
class ScriptLimits:
    """How far one tool script may go."""

    wall_seconds: float = 120.0
    max_calls: int = 50
    max_script_chars: int = 20_000
    max_result_chars: int = 200_000  # one inner result handed to the script
    max_output_chars: int = 8_000  # printed output kept for the model
    memory_bytes: int = 1 << 30


class ScriptRefused(Exception):
    """The script cannot run here; the message says why and what to do."""


@dataclass
class ScriptRun:
    """What one script did: its calls, its result or error, and what it printed."""

    calls: list[dict[str, Any]] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    result: Any = None
    error: str | None = None
    output: str = ""
    stopped: str | None = None

    def payload(self) -> dict[str, Any]:
        body: dict[str, Any] = {"status": "error" if self.error or self.stopped else "success", "calls": self.calls}
        if self.error:
            body["error"] = self.error
        if self.stopped:
            body["stopped"] = self.stopped
        if not self.error and not self.stopped:
            body["result"] = self.result
        if self.output:
            body["printed"] = self.output
        if self.notes:
            body["notes"] = self.notes
        return body


# -- refusing before anything runs -----------------------------------------------------------


def sandbox_refusal() -> str | None:
    """Why the sandbox cannot hold a tool script here, or None when it can."""

    from custom_tools.code_sandbox import NETWORK_LANDLOCK_ABI, code_sandbox_status
    from runtime_settings.code_sandbox_contract import CODE_SANDBOX_REQUIRED

    status = code_sandbox_status()
    if status.mode != CODE_SANDBOX_REQUIRED:
        return (
            "Tool scripts need the code sandbox, and the operator turned it off "
            "(security_layer.code_execution.sandbox). Call the tools directly instead."
        )
    if not status.available or status.landlock_abi < NETWORK_LANDLOCK_ABI:
        return (
            "Tool scripts need a sandbox that also closes the network (Landlock ABI "
            f"{NETWORK_LANDLOCK_ABI}, Linux 6.7 or later); this system has ABI {status.landlock_abi}. "
            "Call the tools directly instead."
        )
    return None


def check_call(agent: Any, catalog: Any, name: Any, inputs: Any, held: set[str]) -> str | None:
    """Why a script may not make this call, or None when it may (module docstring)."""

    import difflib

    from custom_agents.tool_index import script_inputs
    from custom_agents.tool_loading import offered_names
    from custom_agents.tool_sync import provider_tools

    if not isinstance(name, str) or not name:
        return "A tool call needs a function name."
    if not isinstance(inputs, dict):
        return f"{name}'s inputs must be given by name."
    tools = provider_tools()
    if name not in tools:
        closest = difflib.get_close_matches(name, sorted(tools), n=3, cutoff=0.5)
        hint = f" The closest names are: {', '.join(closest)}." if closest else ""
        return f"No tool function is named {name!r}.{hint}"
    allowed = script_inputs(catalog, name)
    if allowed is None:
        return f"{name} changes things or cannot run from a script; call it directly."
    if name not in held:
        return f"You do not hold {name} for this work; find_tools shows whether you may use it and how to ask."
    if name not in offered_names(agent, {name}):
        return f"The operator's tool settings do not offer {name}."
    if not allowed.allows(inputs):
        return (
            f"From a script, {name} runs only with {allowed.argument} set to one of {', '.join(allowed.values)}; "
            "call it directly for anything else."
        )
    return None


# -- running one inner call ------------------------------------------------------------------


def _block_value(block: dict[str, Any]) -> Any:
    if "json" in block:
        return block["json"]
    if "text" in block:
        text = str(block["text"])
        try:
            return json.loads(text)
        except (TypeError, ValueError):
            return text
    return {key: "<not text>" for key in block}


def _result_value(result: dict[str, Any]) -> tuple[Any, list[str]]:
    """A tool result as the script sees it, and any notes added to it.

    The tool's own answer is its first content block (parsed JSON when the
    text is JSON). Blocks after it are notes hooks added, such as the task's
    tool-call budget warning (``custom_agents.runtime_guardrails``): the
    script gets the answer's own shape, and the notes go to the model with
    the script's result.
    """

    blocks = list(result.get("content") or [])
    if not blocks:
        return None, []
    notes = [str(block.get("text")) for block in blocks[1:] if "text" in block]
    return _block_value(blocks[0]), notes


def _error_text(result: dict[str, Any]) -> str:
    texts = [str(block.get("text")) for block in result.get("content") or [] if "text" in block]
    return " ".join(texts).strip() or "The tool call failed."


def execute_call(
    strands_agent: Any, invocation_state: dict[str, Any], name: str, inputs: dict[str, Any]
) -> tuple[bool, Any, list[str]]:
    """Run one call through Strands' tool executor, as a script's call; (succeeded, value or error text, notes).

    Strands' own direct calls (``agent.tool.<name>``) use the same executor
    stream, but only for tools already in the registry; a script's tool may be
    one the index lists without a definition, which ``OnRequestToolHook`` gives
    the call without loading its group. The executor is private Strands API,
    pinned by ``tests/test_tool_scripts.py``.
    """

    from strands._async import run_async
    from strands.tools.executors._executor import ToolExecutor
    from strands.types._events import ToolInterruptEvent

    from custom_tools.tool_script_context import tool_script_call

    from strands.types.tools import ToolResult, ToolUse

    tool_use = cast(ToolUse, {"toolUseId": f"tooluse_script_{uuid.uuid4().hex[:12]}", "name": name, "input": dict(inputs)})
    results: list[ToolResult] = []
    # A copy shares the turn's request state (call budget) and ToolStop, but Strands' updates stay here.
    state = dict(invocation_state or {})
    paused: list[bool] = []

    async def run() -> None:
        async for event in ToolExecutor._stream(strands_agent, tool_use, results, state):
            if isinstance(event, ToolInterruptEvent):
                paused.append(True)
                strands_agent._interrupt_state.deactivate()
                return

    with tool_script_call():
        run_async(run)
    if paused:
        return False, f"{name} needs a person's decision, which cannot happen inside a script; call it directly.", []
    if not results:
        return False, f"{name} returned no result.", []
    result = cast(dict[str, Any], results[0])
    if result.get("status") == "error":
        return False, _error_text(result), []
    value, notes = _result_value(result)
    return True, value, notes


# -- the script process ----------------------------------------------------------------------


class _BoundedReader(threading.Thread):
    """Keeps at most ``limit`` characters of a stream so a chatty script cannot block on a full pipe."""

    def __init__(self, stream: Any, limit: int) -> None:
        super().__init__(daemon=True)
        self.stream, self.limit = stream, limit
        self.parts: list[str] = []
        self.size = self.dropped = 0

    def run(self) -> None:
        for chunk in iter(lambda: self.stream.read(4096), b""):
            text = chunk.decode("utf-8", errors="replace")
            room = self.limit - self.size
            if room > 0:
                self.parts.append(text[:room])
                self.size += min(room, len(text))
            self.dropped += max(0, len(text) - max(room, 0))

    def text(self) -> str:
        body = "".join(self.parts)
        return body + (f"\n... {self.dropped} more characters not kept" if self.dropped else "")


def _line_reader(stream: Any, lines: queue.Queue[str | None]) -> None:
    for line in stream:
        lines.put(line)
    lines.put(None)


def _end_process(process: subprocess.Popen[bytes]) -> None:
    if process.poll() is not None:
        return
    try:
        os.killpg(process.pid, signal.SIGKILL)
    except (ProcessLookupError, PermissionError):
        process.kill()
    try:
        process.wait(timeout=5)
    except subprocess.TimeoutExpired:
        logger.error("A tool script did not end after SIGKILL. event=tool_script_kill_failed pid=%s", process.pid)


def run_script(
    agent: Any,
    strands_agent: Any,
    invocation_state: dict[str, Any],
    code: str,
    *,
    tool_stop: Any = None,
    limits: ScriptLimits = ScriptLimits(),
) -> ScriptRun:
    """Run one tool script for the agent's current turn; raises ``ScriptRefused`` when it cannot start."""

    from custom_agents.access_runtime import runtime_access
    from custom_agents.strands_parts import current_parts
    from custom_agents.tool_index import held_functions
    from custom_agents.tool_presentation import scripts_enabled
    from custom_tools.code_sandbox import prepare_sandboxed_command, release_sandboxed_command
    from custom_tools import tool_script_runner

    if not scripts_enabled(agent):
        raise ScriptRefused("Tool scripts are not on for this team (agent.tool_presentation). Call the tools directly.")
    if not str(code or "").strip():
        raise ScriptRefused("The script is empty. Write a function body that calls tools.<name>(...) and returns a value.")
    if len(code) > limits.max_script_chars:
        raise ScriptRefused(f"The script is longer than {limits.max_script_chars} characters; keep it short.")
    refusal = sandbox_refusal()
    if refusal:
        raise ScriptRefused(refusal)

    catalog = runtime_access(agent).catalog
    held = held_functions(agent, current_parts(agent))
    scratch = Path(tempfile.mkdtemp(prefix="ua-tool-script-"))
    run = ScriptRun()
    process: subprocess.Popen[bytes] | None = None
    request_read, request_write = os.pipe()
    reply_read, reply_write = os.pipe()
    try:
        shutil.copyfile(tool_script_runner.__file__, scratch / RUNNER_FILE)
        (scratch / SCRIPT_FILE).write_text(code, encoding="utf-8")
        command = prepare_sandboxed_command(
            (
                sys.executable, "-I", "-S", str(scratch / RUNNER_FILE),
                str(request_write), str(reply_read), str(scratch / SCRIPT_FILE),
                str(limits.memory_bytes), str(int(limits.wall_seconds) + 5),
            ),
            task_roots=(),
            env={"PATH": os.defpath, "LANG": "C.UTF-8"},
            network=False,
            scratch_dir=str(scratch),
        )
        process = subprocess.Popen(
            command.argv,
            env=command.env,
            cwd=str(scratch),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            pass_fds=(request_write, reply_read),
            start_new_session=True,
        )
        if tool_stop is not None:
            tool_stop.process_group_recorder(TOOL_SCRIPT_NAME)(process.pid)
        os.close(request_write)
        os.close(reply_read)
        request_write = reply_read = -1
        printed = _BoundedReader(process.stdout, limits.max_output_chars)
        printed.start()
        # _serve owns and closes these two from here on.
        served = (request_read, reply_write)
        request_read = reply_write = -1
        _serve(agent, strands_agent, invocation_state, catalog, held, process, *served, run, limits, tool_stop)
        _end_process(process)
        printed.join(timeout=2)
        run.output = printed.text().strip()
        release_sandboxed_command(command)
    finally:
        for fd in (request_read, request_write, reply_read, reply_write):
            if fd >= 0:
                try:
                    os.close(fd)
                except OSError:
                    pass
        if process is not None:
            _end_process(process)
        shutil.rmtree(scratch, ignore_errors=True)
    logger.info(
        "A tool script ended. event=tool_script_finished agent_id=%s calls=%d refused=%d status=%s stopped=%s",
        getattr(agent, "id", None),
        len(run.calls),
        sum(1 for call in run.calls if call.get("refused")),
        "error" if run.error else "success",
        run.stopped,
    )
    return run


def _serve(
    agent: Any,
    strands_agent: Any,
    invocation_state: dict[str, Any],
    catalog: Any,
    held: set[str],
    process: subprocess.Popen[bytes],
    request_read: int,
    reply_write: int,
    run: ScriptRun,
    limits: ScriptLimits,
    tool_stop: Any,
) -> None:
    """Answer the script's calls until it ends, is stopped, or runs out of time."""

    requests = os.fdopen(request_read, "r", encoding="utf-8")
    replies = os.fdopen(reply_write, "w", encoding="utf-8", buffering=1)
    lines: queue.Queue[str | None] = queue.Queue()
    threading.Thread(target=_line_reader, args=(requests, lines), daemon=True).start()
    deadline = time.monotonic() + limits.wall_seconds
    try:
        while True:
            if tool_stop is not None and tool_stop.is_requested():
                run.stopped = "The task was stopped, so the script was ended."
                return
            if time.monotonic() > deadline:
                run.stopped = f"The script ran longer than {int(limits.wall_seconds)} seconds and was ended."
                return
            try:
                line = lines.get(timeout=0.2)
            except queue.Empty:
                continue
            if line is None:
                if run.result is None and run.error is None:
                    run.error = "The script process ended without a result."
                return
            message = _parsed(line)
            kind = message.get("kind")
            if kind == "done":
                run.result = message.get("result")
                return
            if kind == "error":
                run.error = str(message.get("error") or "The script failed.")
                return
            if kind == "call":
                replies.write(json.dumps(_answer(agent, strands_agent, invocation_state, catalog, held, message, run, limits), default=str) + "\n")
                replies.flush()
                continue
            run.error = "The script sent something that is not a tool call."
            return
    finally:
        for stream in (replies, requests):
            try:
                stream.close()
            except OSError:
                pass


def _parsed(line: str) -> dict[str, Any]:
    try:
        value = json.loads(line)
    except ValueError:
        return {}
    return value if isinstance(value, dict) else {}


def _answer(
    agent: Any,
    strands_agent: Any,
    invocation_state: dict[str, Any],
    catalog: Any,
    held: set[str],
    message: dict[str, Any],
    run: ScriptRun,
    limits: ScriptLimits,
) -> dict[str, Any]:
    name, inputs = message.get("tool"), message.get("input")
    if len(run.calls) >= limits.max_calls:
        refusal: str | None = f"A script may make at most {limits.max_calls} tool calls."
    else:
        refusal = check_call(agent, catalog, name, inputs, held)
    if refusal:
        run.calls.append({"tool": str(name), "ok": False, "refused": True, "error": refusal})
        logger.info(
            "A tool script's call was refused. event=tool_script_call_refused agent_id=%s tool=%s reason=%s",
            getattr(agent, "id", None),
            name,
            refusal,
        )
        return {"ok": False, "error": refusal}
    try:
        ok, value, notes = execute_call(strands_agent, invocation_state, str(name), dict(inputs or {}))
    except Exception as exc:  # noqa: BLE001 - one failed call fails in the script, not the turn
        logger.exception(
            "A tool script's call failed. event=tool_script_call_failed agent_id=%s tool=%s", getattr(agent, "id", None), name
        )
        ok, value, notes = False, f"{name} failed: {exc}", []
    run.notes.extend(note for note in notes if note not in run.notes)
    if ok:
        size = len(json.dumps(value, default=str))
        if size > limits.max_result_chars:
            ok, value = False, (
                f"{name}'s result has {size} characters, more than a script takes ({limits.max_result_chars}); "
                "ask for less (a smaller limit, a narrower query)."
            )
    run.calls.append({"tool": str(name), "ok": ok, **({} if ok else {"error": value})})
    return {"ok": True, "result": value} if ok else {"ok": False, "error": value}


__all__ = [
    "ScriptLimits",
    "ScriptRefused",
    "ScriptRun",
    "TOOL_SCRIPT_NAME",
    "check_call",
    "execute_call",
    "run_script",
    "sandbox_refusal",
]
