"""Broker-owned process supervisor for reviewed declarative MCP sidecars."""

from __future__ import annotations

import asyncio
import logging
import os
from pathlib import Path
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
    ) -> None:
        deadline = asyncio.get_running_loop().time() + 20.0
        while asyncio.get_running_loop().time() < deadline:
            if process.returncode is not None:
                raise RuntimeError(
                    f"Managed MCP sidecar exited during startup with status {process.returncode}."
                )
            try:
                reader, writer = await asyncio.open_connection(
                    definition.bind_host,
                    definition.port,
                )
                del reader
                writer.close()
                await writer.wait_closed()
                return
            except OSError:
                await asyncio.sleep(0.1)
        raise TimeoutError("Managed MCP sidecar did not become ready within 20 seconds.")

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
                await self._wait_ready(definition, process)
            except Exception:
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
