"""Live check that the managed local NATS server requires authentication.

Agent-run code executes in a sandbox that cannot read runtime credentials, but
it can open network connections. The local message bus must therefore reject
connections that do not present the generated credentials.
"""

from __future__ import annotations

import asyncio
import logging
import os
import shutil
import socket
from pathlib import Path

import nats
import pytest

import docker.nats_service as nats_service


def _free_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return int(probe.getsockname()[1])


@pytest.mark.skipif(shutil.which("nats-server") is None, reason="nats-server binary is not installed")
def test_managed_nats_rejects_unauthenticated_clients(tmp_path: Path, monkeypatch) -> None:
    port = _free_port()
    monkeypatch.setattr(nats_service, "INTERNAL_NATS_PORT", port)
    monkeypatch.setattr(nats_service, "INTERNAL_NATS_MONITOR_PORT", _free_port())
    # Like the launcher, pass a full environment without NATS credentials.
    env = {key: value for key, value in os.environ.items() if not key.startswith("NATS_")}
    process = nats_service.start_local_nats(
        env=env,
        logger=logging.getLogger("test_local_nats_auth_live"),
        cwd=tmp_path,
        service_root=tmp_path / "nats",
    )
    try:
        cmdline = Path(f"/proc/{process.pid}/cmdline").read_bytes().decode()
        assert env["NATS_PASSWORD"] not in cmdline

        async def probe() -> None:
            url = f"nats://127.0.0.1:{port}"
            with pytest.raises(Exception) as denied:
                await nats.connect(url, connect_timeout=2, max_reconnect_attempts=0, allow_reconnect=False)
            assert "authorization" in str(denied.value).lower()
            client = await nats.connect(
                url,
                user=env["NATS_USER"],
                password=env["NATS_PASSWORD"],
                connect_timeout=2,
                allow_reconnect=False,
            )
            await client.close()

        asyncio.run(probe())
    finally:
        process.terminate()
        process.wait(timeout=10)
