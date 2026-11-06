"""Broker-owned process supervisor for reviewed declarative MCP sidecars."""

from __future__ import annotations

import asyncio
import logging
import os
from pathlib import Path
import re
import secrets
import sys
from typing import Any, BinaryIO

from mcp_control.hosting import (
    HostedMCPLifecycle,
    MCPHostedServerDefinitionV1,
    MCPHostedServerStore,
    resolve_mcp_hosting_paths,
)


LOGGER = logging.getLogger(__name__)

# uvicorn's line once the sidecar listens (mcp_control.managed_server), and the
# error it prints when the port is taken.
_LISTENING = re.compile(r"Uvicorn running on \S+:(\d+)")
_PORT_TAKEN = "address already in use"


class HostedMCPStartError(RuntimeError):
    """A hosted MCP server's sidecar did not start; the message says why and what to do."""


class HostedMCPPortInUse(HostedMCPStartError):
    """Another process already listens on the sidecar's port."""


def _sidecar_output(log_path: Path, offset: int) -> str:
    """What the sidecar of this start wrote: its log is appended to on every start."""

    with log_path.open("rb") as log:
        log.seek(offset)
        return log.read().decode("utf-8", errors="replace")


class MCPHostedProcessSupervisor:
    def __init__(self, config: Any) -> None:
        self.config = config
        self.paths = resolve_mcp_hosting_paths(config)
        self.store = MCPHostedServerStore(self.paths)
        self._processes: dict[str, asyncio.subprocess.Process] = {}
        self._log_handles: dict[str, BinaryIO] = {}
        self._lock = asyncio.Lock()

    def _definition_path(self, hosted_server_id: str) -> Path:
        return self.paths.definitions / f"{hosted_server_id}.json"

    def _token_path(self, hosted_server_id: str) -> Path:
        return self.paths.tokens / f"{hosted_server_id}.token"

    def _log_path(self, hosted_server_id: str) -> Path:
        return self.paths.logs / f"{hosted_server_id}.log"

    def _close_log(self, hosted_server_id: str) -> None:
        handle = self._log_handles.pop(hosted_server_id, None)
        if handle is not None:
            handle.close()

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
        timeout_seconds: float = 20.0,
    ) -> None:
        """Return once the sidecar this start launched says it listens on its port, while it still runs.

        Another process answering on the port is not readiness. Sidecars run in
        their own session, so one left over from an earlier broker run can keep
        the port; this start's sidecar then stops with "address already in
        use", and the start must say so instead of reporting that one as running.
        """

        address = f"{definition.bind_host}:{definition.port}"
        deadline = asyncio.get_running_loop().time() + timeout_seconds
        while True:
            returncode = process.returncode
            output = _sidecar_output(log_path, offset)
            if returncode is not None:
                if _PORT_TAKEN in output:
                    raise HostedMCPPortInUse(
                        f"The hosted MCP server {definition.server_name} cannot start: {address} is already "
                        "in use, most likely by its sidecar left over from an earlier broker run. Stop that "
                        "process (`ps -ef | grep mcp_control.managed_server` shows it), then start the "
                        "server again."
                    )
                raise HostedMCPStartError(
                    f"The hosted MCP server {definition.server_name} stopped while starting, with status "
                    f"{returncode}. Its log is {log_path}."
                )
            listening = _LISTENING.search(output)
            if listening is not None and int(listening.group(1)) == definition.port:
                return
            if asyncio.get_running_loop().time() >= deadline:
                raise TimeoutError(
                    f"Managed MCP sidecar did not become ready within {timeout_seconds:g} seconds."
                )
            await asyncio.sleep(0.1)

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
            token_path = self._ensure_token(hosted_server_id)
            log_path = self._log_path(hosted_server_id)
            offset = log_path.stat().st_size if log_path.exists() else 0
            log_handle = log_path.open("ab", buffering=0)
            log_path.chmod(0o600)
            try:
                process = await asyncio.create_subprocess_exec(
                    sys.executable,
                    "-m",
                    "mcp_control.managed_server",
                    "--definition",
                    str(self._definition_path(hosted_server_id)),
                    "--token-file",
                    str(token_path),
                    stdin=asyncio.subprocess.DEVNULL,
                    stdout=log_handle,
                    stderr=asyncio.subprocess.STDOUT,
                    start_new_session=True,
                )
            except Exception:
                log_handle.close()
                raise
            self._log_handles[hosted_server_id] = log_handle
            try:
                await self._wait_ready(definition, process, log_path=log_path, offset=offset)
            except Exception:
                if process.returncode is None:
                    process.terminate()
                await process.wait()
                self._close_log(hosted_server_id)
                raise
            self._processes[hosted_server_id] = process
            LOGGER.info(
                "Managed MCP sidecar started. event=mcp_host_sidecar_started "
                "hosted_server_id=%s pid=%s port=%s",
                hosted_server_id,
                process.pid,
                definition.port,
                extra={"mcp_host_log_path": str(log_path)},
            )
            return {
                "status": "running",
                "hosted_server_id": hosted_server_id,
                "pid": process.pid,
                "already_running": False,
            }

    async def stop(self, hosted_server_id: str) -> dict[str, Any]:
        async with self._lock:
            process = self._processes.pop(hosted_server_id, None)
            if process is None or process.returncode is not None:
                self._close_log(hosted_server_id)
                return {
                    "status": "stopped",
                    "hosted_server_id": hosted_server_id,
                    "already_stopped": True,
                }
            process.terminate()
            try:
                await asyncio.wait_for(process.wait(), timeout=10.0)
            except TimeoutError:
                process.kill()
                await process.wait()
            self._close_log(hosted_server_id)
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
                except Exception:
                    LOGGER.exception(
                        "Managed MCP sidecar restore failed. event=mcp_host_restore_failed "
                        "hosted_server_id=%s",
                        definition.hosted_server_id,
                    )

    async def close(self) -> None:
        for hosted_server_id in list(self._processes):
            await self.stop(hosted_server_id)
