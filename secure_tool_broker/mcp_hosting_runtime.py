"""Broker-owned process supervisor for reviewed declarative MCP sidecars.

A start either leaves the server's own sidecar running and listening, or
raises and leaves nothing running: whatever ends a start early (a start
failure, a cancellation, an unexpected error) stops and reaps the sidecar it
launched. Failures people can act on are ``HostedMCPStartError`` with a reason
that names no host path (``MCPHostingPaths.shown``); the broker answers them
as "could not start", which agents try again. Anything else is a defect and
keeps its traceback.
"""

from __future__ import annotations

import asyncio
from contextlib import contextmanager, suppress
from dataclasses import dataclass
from datetime import datetime, timezone
from functools import partial
import logging
import os
from pathlib import Path
import re
import secrets
import sys
import time
from typing import Any, BinaryIO, Iterator

from mcp_control.hosting import (
    HostedMCPLifecycle,
    HostedMCPStartError,
    MCPHostedServerDefinitionV1,
    MCPHostedServerStore,
    resolve_mcp_hosting_paths,
)


LOGGER = logging.getLogger(__name__)

# uvicorn's line once the sidecar listens (mcp_control.managed_server), and the
# error it prints when the port is taken.
_LISTENING = re.compile(r"Uvicorn running on \S+:(\d+)")
_PORT_TAKEN = "address already in use"

# How long a start waits for its sidecar to say it listens, and how long a
# sidecar gets to exit after SIGTERM before SIGKILL: after a failed start, and
# when stopped. A failed start answers within READY_TIMEOUT_SECONDS +
# FAILED_START_STOP_SECONDS (25 seconds), inside the manager's 30-second wait
# for the broker. A start of a server set to run repeats its last failure
# instead of launching again for FAILED_START_REPEAT_SECONDS, which is shorter
# than the agents' first one-minute retry. Read at call time, so tests can
# shorten them.
READY_TIMEOUT_SECONDS = 20.0
FAILED_START_STOP_SECONDS = 5.0
STOP_GRACE_SECONDS = 10.0
FAILED_START_REPEAT_SECONDS = 30.0


class HostedMCPPortInUse(HostedMCPStartError):
    """Another process already listens on the sidecar's port."""


@dataclass(frozen=True)
class _FailedStart:
    reason: str
    at: float  # time.monotonic()


def _port_in_use_reason(server_name: str, address: str) -> str:
    """Why a start failed on a port another process holds, and what to do about it.

    Agents read it in ``load_status`` cut to ``custom_agents.mcp_server_tools.MAX_ERROR_TEXT``
    (300) characters, so it is short enough that its advice, at the end, stays
    whole for server names up to 32 characters on a loopback address. The advice
    never says to stop and start a server set to run: the broker refuses an agent
    that tries while it is stopped, and that agent then waits for its own
    restart. A person whose start of a stopped server failed starts it again.
    """

    return (
        f"The hosted MCP server {server_name} cannot start: {address} is already in use, likely by its "
        "sidecar from an earlier broker run. Stop that process (`ps -ef | grep mcp_control.managed_server`). "
        "A server set to run then starts on an agent's next try; start a stopped one again."
    )


def _sidecar_output(log_path: Path, offset: int) -> str:
    """What the sidecar of this start wrote: its log is appended to on every start."""

    with log_path.open("rb") as log:
        log.seek(offset)
        return log.read().decode("utf-8", errors="replace")


def _open_log(log_path: Path) -> tuple[BinaryIO, int]:
    """The sidecar's log, opened to append and readable only by the broker, and where this start's output begins."""

    offset = log_path.stat().st_size if log_path.exists() else 0
    log = log_path.open("ab", buffering=0)
    try:
        log_path.chmod(0o600)
    except BaseException:
        log.close()
        raise
    return log, offset


@contextmanager
def _start_step(definition: MCPHostedServerDefinitionV1, step: str) -> Iterator[None]:
    """An OS error during one step of a start is a start failure that names the step.

    The reason gives the step and the error's own words (``strerror``), not
    its file names; the broker's log gets the whole error.
    """

    try:
        yield
    except OSError as error:
        LOGGER.warning(
            "A hosted MCP server's start failed on an OS error. event=mcp_host_sidecar_start_os_error "
            "hosted_server_id=%s step=%s error=%s",
            definition.hosted_server_id,
            step,
            error,
        )
        raise HostedMCPStartError(
            f"The hosted MCP server {definition.server_name} could not start: {step} failed "
            f"({error.strerror or type(error).__name__}). The broker's log has the details."
        ) from error


async def _stop_process(process: asyncio.subprocess.Process, *, grace_seconds: float) -> None:
    """Stop a sidecar and reap it: SIGTERM, then SIGKILL if it has not exited after ``grace_seconds``."""

    if process.returncode is None:
        with suppress(ProcessLookupError):
            process.terminate()
    try:
        await asyncio.wait_for(process.wait(), timeout=grace_seconds)
    except TimeoutError:
        with suppress(ProcessLookupError):
            process.kill()
        await process.wait()


class MCPHostedProcessSupervisor:
    def __init__(self, config: Any) -> None:
        self.config = config
        self.paths = resolve_mcp_hosting_paths(config)
        self.store = MCPHostedServerStore(self.paths)
        self._processes: dict[str, asyncio.subprocess.Process] = {}
        # Sidecars being stopped (``_stop_sidecar``), until they have exited.
        self._stopping: dict[str, asyncio.Task[None]] = {}
        # The last failed start of each server (``_repeat_recent_failure``).
        self._failed_starts: dict[str, _FailedStart] = {}
        self._lock = asyncio.Lock()

    def _definition_path(self, hosted_server_id: str) -> Path:
        return self.paths.definitions / f"{hosted_server_id}.json"

    def _token_path(self, hosted_server_id: str) -> Path:
        return self.paths.tokens / f"{hosted_server_id}.token"

    def _log_path(self, hosted_server_id: str) -> Path:
        return self.paths.logs / f"{hosted_server_id}.log"

    def _ensure_token(self, hosted_server_id: str) -> Path:
        path = self._token_path(hosted_server_id)
        if path.is_file() and path.read_text(encoding="utf-8").strip():
            return path
        temporary = path.with_name(f".{path.name}.{secrets.token_hex(8)}.tmp")
        try:
            temporary.write_text(secrets.token_urlsafe(48) + "\n", encoding="utf-8")
            temporary.chmod(0o600)
            os.replace(temporary, path)
        finally:
            temporary.unlink(missing_ok=True)
        return path

    async def _wait_ready(
        self,
        definition: MCPHostedServerDefinitionV1,
        process: asyncio.subprocess.Process,
        *,
        log_path: Path,
        offset: int,
    ) -> None:
        """Return once the sidecar this start launched says it listens on its port, while it still runs.

        Another process answering on the port is not readiness. Sidecars run in
        their own session, so one left over from an earlier broker run can keep
        the port; this start's sidecar then stops with "address already in
        use", and the start must say so instead of reporting that one as running.
        A sidecar that has not said so within ``READY_TIMEOUT_SECONDS`` did not
        start either.
        """

        address = f"{definition.bind_host}:{definition.port}"
        shown_log = self.paths.shown(log_path)
        timeout_seconds = READY_TIMEOUT_SECONDS
        deadline = asyncio.get_running_loop().time() + timeout_seconds
        while True:
            returncode = process.returncode
            output = _sidecar_output(log_path, offset)
            if returncode is not None:
                if _PORT_TAKEN in output:
                    raise HostedMCPPortInUse(_port_in_use_reason(definition.server_name, address))
                raise HostedMCPStartError(
                    f"The hosted MCP server {definition.server_name} stopped while starting, with status "
                    f"{returncode}. Its log is {shown_log}."
                )
            listening = _LISTENING.search(output)
            if listening is not None and int(listening.group(1)) == definition.port:
                return
            if asyncio.get_running_loop().time() >= deadline:
                raise HostedMCPStartError(
                    f"The hosted MCP server {definition.server_name} did not report listening on {address} "
                    f"within {timeout_seconds:g} seconds, so the broker stopped it. Its log is {shown_log}."
                )
            await asyncio.sleep(0.1)

    def _remember_start_failure(self, definition: MCPHostedServerDefinitionV1, error: HostedMCPStartError) -> None:
        """A server set to run keeps why its sidecar did not start, for every list of hosted servers.

        A server being started from stopped is not set to run yet; whoever
        started it gets the reason in the answer instead. A note the broker
        cannot write is logged, and the start's own reason still reaches
        whoever asked.
        """

        if definition.lifecycle != HostedMCPLifecycle.RUNNING:
            return
        reason = str(error).strip() or type(error).__name__
        try:
            self.store.record_start_failure(definition.hosted_server_id, reason, at=datetime.now(timezone.utc))
        except OSError as note_error:
            LOGGER.warning(
                "Could not record why a hosted MCP server set to run did not start. "
                "event=mcp_host_start_failure_not_recorded hosted_server_id=%s reason=%s error=%s",
                definition.hosted_server_id,
                reason,
                note_error,
            )
            return
        LOGGER.warning(
            "A hosted MCP server set to run could not start. event=mcp_host_start_failure_recorded "
            "hosted_server_id=%s reason=%s",
            definition.hosted_server_id,
            reason,
        )

    def _forget_start_failure(self, hosted_server_id: str) -> None:
        """Delete the server's start-failure note; one the broker cannot delete does not undo a start or stop."""

        try:
            self.store.clear_start_failure(hosted_server_id)
        except OSError as error:
            LOGGER.warning(
                "Could not delete the start-failure note of a hosted MCP server. "
                "event=mcp_host_start_failure_not_cleared hosted_server_id=%s error=%s",
                hosted_server_id,
                error,
            )

    def _repeat_recent_failure(self, definition: MCPHostedServerDefinitionV1) -> None:
        """Answer a start of a server set to run with its last failure while that failure is recent.

        Every agent that uses the server's tools asks the broker to start it,
        and those asks queue on the one lock; without this, each would launch
        a sidecar that fails the same way, and a person's stop would wait
        behind all of them. A person's start (from stopped) always launches.
        """

        failed = self._failed_starts.get(definition.hosted_server_id)
        if (
            definition.lifecycle != HostedMCPLifecycle.RUNNING
            or failed is None
            or time.monotonic() - failed.at >= FAILED_START_REPEAT_SECONDS
        ):
            return
        LOGGER.info(
            "A hosted MCP server's start failed moments ago; not launching it again yet. "
            "event=mcp_host_sidecar_start_repeated hosted_server_id=%s",
            definition.hosted_server_id,
        )
        raise HostedMCPStartError(failed.reason)

    async def _stop_sidecar(
        self, hosted_server_id: str, process: asyncio.subprocess.Process, *, grace_seconds: float
    ) -> None:
        """Stop and reap a sidecar, even if whoever asked is cancelled meanwhile.

        The stop runs as its own task, awaited through ``asyncio.shield`` (as
        asyncio does for a spawn it abandons): a second cancellation, such as
        a shutdown cancelling its handlers again, ends this wait but not the
        stop. Until the task ends, the next start or stop of the server waits
        for it (``_finish_stopping``), so nothing launches onto a port the old
        sidecar still holds.
        """

        task = asyncio.get_running_loop().create_task(_stop_process(process, grace_seconds=grace_seconds))
        self._stopping[hosted_server_id] = task
        task.add_done_callback(partial(self._stopped, hosted_server_id))
        await asyncio.shield(task)

    def _stopped(self, hosted_server_id: str, task: asyncio.Task[None]) -> None:
        if self._stopping.get(hosted_server_id) is task:
            del self._stopping[hosted_server_id]

    async def _finish_stopping(self, hosted_server_id: str) -> None:
        """Wait until a sidecar of this server that is being stopped has exited (see ``_stop_sidecar``)."""

        task = self._stopping.get(hosted_server_id)
        if task is not None:
            await asyncio.shield(task)

    async def _launch(self, definition: MCPHostedServerDefinitionV1) -> asyncio.subprocess.Process:
        """Launch the server's sidecar and return it once it listens; otherwise leave nothing running.

        An OS error in any step becomes a ``HostedMCPStartError`` that names
        the step (``_start_step``). Whatever ends the start early also stops
        and reaps the sidecar it launched before it is raised again. The
        broker keeps no handle on the log: the sidecar writes to its own copy
        of the descriptor.
        """

        hosted_server_id = definition.hosted_server_id
        log_path = self._log_path(hosted_server_id)
        process: asyncio.subprocess.Process | None = None
        try:
            with _start_step(definition, "preparing its access token"):
                token_path = self._ensure_token(hosted_server_id)
            with _start_step(definition, "opening its log"):
                log, offset = _open_log(log_path)
            with log, _start_step(definition, "starting its process"):
                process = await asyncio.create_subprocess_exec(
                    sys.executable,
                    "-m",
                    "mcp_control.managed_server",
                    "--definition",
                    str(self._definition_path(hosted_server_id)),
                    "--token-file",
                    str(token_path),
                    stdin=asyncio.subprocess.DEVNULL,
                    stdout=log,
                    stderr=asyncio.subprocess.STDOUT,
                    start_new_session=True,
                )
            with _start_step(definition, "reading its log"):
                await self._wait_ready(definition, process, log_path=log_path, offset=offset)
        except (Exception, asyncio.CancelledError) as error:
            if process is not None:
                if isinstance(error, asyncio.CancelledError):
                    LOGGER.warning(
                        "A hosted MCP server's start was cancelled; stopping its sidecar. "
                        "event=mcp_host_sidecar_start_cancelled hosted_server_id=%s pid=%s",
                        hosted_server_id,
                        process.pid,
                    )
                await self._stop_sidecar(hosted_server_id, process, grace_seconds=FAILED_START_STOP_SECONDS)
            raise
        return process

    async def start(self, hosted_server_id: str) -> dict[str, Any]:
        definition = self.store.require(hosted_server_id)
        if definition.lifecycle not in {
            HostedMCPLifecycle.STOPPED,
            HostedMCPLifecycle.RUNNING,
        }:
            raise PermissionError("Hosted MCP definition must be installed before start.")
        async with self._lock:
            existing = self._processes.get(hosted_server_id)
            if existing is not None and existing.returncode is None:
                return {
                    "status": "running",
                    "hosted_server_id": hosted_server_id,
                    "pid": existing.pid,
                    "already_running": True,
                }
            # A sidecar that exited after it started is not this broker's to stop
            # any more; a later stop() (also at shutdown) must not clear the note
            # of a start that failed after it.
            self._processes.pop(hosted_server_id, None)
            await self._finish_stopping(hosted_server_id)
            self._repeat_recent_failure(definition)
            try:
                process = await self._launch(definition)
            except HostedMCPStartError as error:
                self._failed_starts[hosted_server_id] = _FailedStart(str(error), time.monotonic())
                self._remember_start_failure(definition, error)
                raise
            self._failed_starts.pop(hosted_server_id, None)
            self._processes[hosted_server_id] = process
            self._forget_start_failure(hosted_server_id)
            LOGGER.info(
                "Managed MCP sidecar started. event=mcp_host_sidecar_started "
                "hosted_server_id=%s pid=%s port=%s",
                hosted_server_id,
                process.pid,
                definition.port,
                extra={"mcp_host_log_path": str(self._log_path(hosted_server_id))},
            )
            return {
                "status": "running",
                "hosted_server_id": hosted_server_id,
                "pid": process.pid,
                "already_running": False,
            }

    async def stop(self, hosted_server_id: str) -> dict[str, Any]:
        async with self._lock:
            await self._finish_stopping(hosted_server_id)
            # Stopped on purpose: a start that failed earlier no longer matters.
            self._failed_starts.pop(hosted_server_id, None)
            self._forget_start_failure(hosted_server_id)
            process = self._processes.pop(hosted_server_id, None)
            if process is None or process.returncode is not None:
                return {
                    "status": "stopped",
                    "hosted_server_id": hosted_server_id,
                    "already_stopped": True,
                }
            await self._stop_sidecar(hosted_server_id, process, grace_seconds=STOP_GRACE_SECONDS)
            LOGGER.info(
                "Managed MCP sidecar stopped. event=mcp_host_sidecar_stopped "
                "hosted_server_id=%s pid=%s",
                hosted_server_id,
                process.pid,
            )
            return {
                "status": "stopped",
                "hosted_server_id": hosted_server_id,
                "already_stopped": False,
            }

    async def ensure_running(self, hosted_server_id: str) -> None:
        definition = self.store.require(hosted_server_id)
        if definition.lifecycle != HostedMCPLifecycle.RUNNING:
            raise PermissionError("Hosted MCP server is not in running lifecycle state.")
        await self.start(hosted_server_id)

    async def restore(self) -> None:
        for definition in self.store.list_definitions():
            if definition.lifecycle == HostedMCPLifecycle.RUNNING:
                try:
                    await self.start(definition.hosted_server_id)
                except HostedMCPStartError as error:
                    # Every list of hosted servers shows why (_remember_start_failure);
                    # nothing in the broker failed, so no traceback.
                    LOGGER.warning(
                        "Managed MCP sidecar restore failed. event=mcp_host_restore_failed "
                        "hosted_server_id=%s reason=%s",
                        definition.hosted_server_id,
                        error,
                    )
                except Exception:
                    LOGGER.exception(
                        "Managed MCP sidecar restore failed. event=mcp_host_restore_failed "
                        "hosted_server_id=%s",
                        definition.hosted_server_id,
                    )

    async def close(self) -> None:
        """Stop every sidecar this broker runs, and wait for those of unfinished starts to exit."""

        for hosted_server_id in list(self._processes):
            await self.stop(hosted_server_id)
        if self._stopping:
            await asyncio.wait(list(self._stopping.values()))
