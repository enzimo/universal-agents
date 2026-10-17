"""Strands-independent process supervision and RTK command planning.

This module deliberately depends only on the Python standard library.  Agent
SDK integration, workspace policy, credential policy, and OpenShell routing
belong in the caller.  Keeping the process boundary small and typed makes it
straightforward to replace with a Rust implementation later.
"""

from __future__ import annotations

import codecs
import os
import queue
import re
import shlex
import shutil
import signal
import subprocess
import threading
import time
from dataclasses import dataclass
from typing import Callable, Literal, Mapping, Protocol


StreamName = Literal["stdout", "stderr"]
TerminationReason = Literal["exited", "timeout", "cancelled", "spawn_error"]
RtkMode = Literal["rewrite", "proxy", "missing"]

_ENVIRONMENT_KEY_PATTERN = re.compile(r"[A-Za-z_][A-Za-z0-9_]*\Z")
_READ_CHUNK_BYTES = 65_536


@dataclass(frozen=True)
class ProcessRequest:
    """A complete, SDK-neutral process execution request."""

    argv: tuple[str, ...]
    cwd: str
    env: Mapping[str, str]
    timeout_seconds: float | None
    terminate_grace_seconds: float = 1.0


@dataclass(frozen=True)
class ProcessStreamChunk:
    """One decoded chunk emitted by a child process stream."""

    stream: StreamName
    data: str


@dataclass(frozen=True)
class ProcessResult:
    """The normalized result of one supervised child process."""

    exit_code: int
    stdout: str
    stderr: str
    termination_reason: TerminationReason
    duration_seconds: float


@dataclass(frozen=True)
class RtkCommandPlan:
    """The executable command selected for one requested shell command."""

    requested_command: str
    executed_command: str
    mode: RtkMode
    warning: str = ""
    diagnostic: str = ""
    cancelled: bool = False


class CancelSignal(Protocol):
    """Anything that reports whether a command must stop, such as ``threading.Event``."""

    def is_set(self) -> bool: ...


class AnyCancel:
    """A stop signal that is set as soon as any of its signals is set.

    The parallel shell mode stops a command both when its task stops and when
    a sibling command fails; this lets one command watch both without either
    signal setting the other.
    """

    def __init__(self, *signals: CancelSignal | None) -> None:
        self._signals = tuple(item for item in signals if item is not None)

    def is_set(self) -> bool:
        return any(item.is_set() for item in self._signals)


class ProcessRunner(Protocol):
    """Structural contract for a replaceable process supervisor."""

    def __call__(
        self,
        request: ProcessRequest,
        *,
        cancel_event: CancelSignal | None = None,
        on_chunk: Callable[[ProcessStreamChunk], None] | None = None,
        on_spawn: Callable[[int], None] | None = None,
    ) -> ProcessResult: ...


def validate_environment_keys(env: Mapping[str, str]) -> None:
    """Reject environment names that cannot be represented safely by POSIX shells."""

    for key in env:
        if not _ENVIRONMENT_KEY_PATTERN.fullmatch(key):
            raise ValueError(f"Invalid environment variable name: {key}")


def _normalized_exit_code(returncode: int | None) -> int:
    if returncode is None:
        return 1
    if returncode < 0:
        return 128 + abs(returncode)
    return returncode


def _signal_process_tree(process: subprocess.Popen[bytes], sig: signal.Signals) -> None:
    """Signal the process group on POSIX, or the direct child elsewhere."""

    try:
        if os.name == "posix":
            os.killpg(process.pid, sig)
        elif sig == signal.SIGTERM:
            process.terminate()
        else:
            process.kill()
    except (ProcessLookupError, PermissionError):
        return


def _stop_process_tree(process: subprocess.Popen[bytes], grace_seconds: float) -> None:
    """Terminate then kill a child process tree without trusting the shell parent."""

    _signal_process_tree(process, signal.SIGTERM)
    if process.poll() is None:
        try:
            process.wait(timeout=max(0.0, grace_seconds))
        except subprocess.TimeoutExpired:
            pass
    # Descendants can survive after the shell parent exits, so signal the group
    # even when the direct child already has a return code.
    _signal_process_tree(process, signal.SIGKILL)
    if process.poll() is None:
        try:
            process.wait(timeout=max(1.0, grace_seconds))
        except subprocess.TimeoutExpired:
            pass


def run_process(
    request: ProcessRequest,
    *,
    cancel_event: CancelSignal | None = None,
    on_chunk: Callable[[ProcessStreamChunk], None] | None = None,
    on_spawn: Callable[[int], None] | None = None,
) -> ProcessResult:
    """Run a child with closed stdin, streamed pipes, and process-tree cleanup.

    ``cancel_event`` is checked before the child starts, so a command asked to
    stop before it began never runs, and every 50 ms while it runs, when the
    whole process group is stopped. ``on_spawn`` gets the child's process id,
    which on POSIX is also its new process group's id, right after it starts.
    """

    if not request.argv:
        raise ValueError("Process argv is required")
    if request.timeout_seconds is not None and request.timeout_seconds <= 0:
        raise ValueError("Process timeout must be greater than zero")
    validate_environment_keys(request.env)

    if cancel_event is not None and cancel_event.is_set():
        return ProcessResult(
            exit_code=130,
            stdout="",
            stderr="",
            termination_reason="cancelled",
            duration_seconds=0.0,
        )
    started = time.monotonic()
    try:
        process = subprocess.Popen(
            request.argv,
            cwd=request.cwd,
            env=dict(request.env),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            start_new_session=os.name == "posix",
        )
    except FileNotFoundError as exc:
        return ProcessResult(
            exit_code=127,
            stdout="",
            stderr=str(exc),
            termination_reason="spawn_error",
            duration_seconds=time.monotonic() - started,
        )
    except OSError as exc:
        return ProcessResult(
            exit_code=1,
            stdout="",
            stderr=str(exc),
            termination_reason="spawn_error",
            duration_seconds=time.monotonic() - started,
        )

    assert process.stdout is not None
    assert process.stderr is not None
    stream_queue: queue.Queue[tuple[StreamName, bytes | None]] = queue.Queue()

    def pump(stream_name: StreamName, pipe: object) -> None:
        reader = pipe
        try:
            while data := reader.read(_READ_CHUNK_BYTES):  # type: ignore[attr-defined]
                stream_queue.put((stream_name, data))
        finally:
            stream_queue.put((stream_name, None))

    threads = [
        threading.Thread(target=pump, args=("stdout", process.stdout), daemon=True),
        threading.Thread(target=pump, args=("stderr", process.stderr), daemon=True),
    ]
    for thread in threads:
        thread.start()

    decoders = {
        "stdout": codecs.getincrementaldecoder("utf-8")(errors="replace"),
        "stderr": codecs.getincrementaldecoder("utf-8")(errors="replace"),
    }
    buffers: dict[StreamName, list[str]] = {"stdout": [], "stderr": []}
    open_streams: set[StreamName] = {"stdout", "stderr"}
    deadline = (
        started + request.timeout_seconds
        if request.timeout_seconds is not None
        else None
    )
    termination_reason: TerminationReason = "exited"
    callback_error: BaseException | None = None

    def consume(stream_name: StreamName, data: bytes | None) -> None:
        nonlocal callback_error
        if data is None:
            if stream_name not in open_streams:
                return
            text = decoders[stream_name].decode(b"", final=True)
            open_streams.remove(stream_name)
        else:
            text = decoders[stream_name].decode(data, final=False)
        if not text:
            return
        buffers[stream_name].append(text)
        if on_chunk is not None and callback_error is None:
            try:
                on_chunk(ProcessStreamChunk(stream=stream_name, data=text))
            except BaseException as exc:  # Preserve callback failures after cleanup.
                callback_error = exc

    try:
        if on_spawn is not None:
            # Inside the try, so the child is stopped if the callback fails.
            on_spawn(process.pid)
        while open_streams or process.poll() is None:
            try:
                stream_name, data = stream_queue.get(timeout=0.05)
                consume(stream_name, data)
            except queue.Empty:
                pass

            if termination_reason == "exited":
                if callback_error is not None or (
                    cancel_event is not None and cancel_event.is_set()
                ):
                    termination_reason = "cancelled"
                    _stop_process_tree(process, request.terminate_grace_seconds)
                elif deadline is not None and time.monotonic() >= deadline:
                    termination_reason = "timeout"
                    _stop_process_tree(process, request.terminate_grace_seconds)

        while True:
            try:
                stream_name, data = stream_queue.get_nowait()
            except queue.Empty:
                break
            consume(stream_name, data)
    finally:
        if process.poll() is None:
            _stop_process_tree(process, request.terminate_grace_seconds)
        for thread in threads:
            thread.join(timeout=max(1.0, request.terminate_grace_seconds))
        process.stdout.close()
        process.stderr.close()

    if callback_error is not None:
        raise callback_error

    exit_code = _normalized_exit_code(process.returncode)
    if termination_reason == "timeout":
        exit_code = 124
    elif termination_reason == "cancelled":
        exit_code = 130
    return ProcessResult(
        exit_code=exit_code,
        stdout="".join(buffers["stdout"]),
        stderr="".join(buffers["stderr"]),
        termination_reason=termination_reason,
        duration_seconds=time.monotonic() - started,
    )


def plan_rtk_command(
    command: str,
    *,
    cwd: str,
    env: Mapping[str, str],
    timeout_seconds: float,
    missing_warning: str,
    cancel_event: CancelSignal | None = None,
) -> RtkCommandPlan:
    """Choose an RTK rewrite, tracked proxy, or observable raw fallback."""

    rtk_path = shutil.which("rtk", path=env.get("PATH"))
    if not rtk_path:
        return RtkCommandPlan(
            requested_command=command,
            executed_command=command,
            mode="missing",
            warning=missing_warning,
        )

    rewrite = run_process(
        ProcessRequest(
            argv=(rtk_path, "rewrite", command),
            cwd=cwd,
            env=env,
            timeout_seconds=min(max(timeout_seconds, 0.1), 10.0),
        ),
        cancel_event=cancel_event,
    )
    if rewrite.termination_reason == "cancelled":
        return RtkCommandPlan(
            requested_command=command,
            executed_command="",
            mode="proxy",
            cancelled=True,
        )

    rewritten_lines = [
        line.strip()
        for line in rewrite.stdout.splitlines()
        if line.strip() and not line.strip().startswith("[rtk]")
    ]
    if rewritten_lines:
        return RtkCommandPlan(
            requested_command=command,
            executed_command=rewritten_lines[-1],
            mode="rewrite",
        )

    diagnostic = rewrite.stderr.strip()
    if rewrite.termination_reason == "timeout":
        diagnostic = "rtk rewrite timed out"
    elif rewrite.exit_code not in {0, 1, 3} and not diagnostic:
        diagnostic = f"rtk rewrite exited with code {rewrite.exit_code}"
    return RtkCommandPlan(
        requested_command=command,
        executed_command=f"{shlex.quote(rtk_path)} proxy sh -c {shlex.quote(command)}",
        mode="proxy",
        diagnostic=diagnostic,
    )


__all__ = [
    "AnyCancel",
    "CancelSignal",
    "ProcessRequest",
    "ProcessResult",
    "ProcessRunner",
    "ProcessStreamChunk",
    "RtkCommandPlan",
    "plan_rtk_command",
    "run_process",
    "validate_environment_keys",
]
