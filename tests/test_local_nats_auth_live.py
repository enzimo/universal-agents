"""Live check that the managed local NATS server requires authentication.

Agent-run code executes in a sandbox that cannot read runtime credentials, but
it can open network connections. The local message bus must therefore reject
connections that do not present the generated credentials.
"""

from __future__ import annotations

import asyncio
import logging
import os
import re
import shutil
from pathlib import Path

import nats
import pytest

import docker.nats_service as nats_service


_LISTENING = re.compile(r"Listening for client connections on 127\.0\.0\.1:(\d+)")


@pytest.mark.skipif(shutil.which("nats-server") is None, reason="nats-server binary is not installed")
def test_managed_nats_rejects_unauthenticated_clients(tmp_path: Path, monkeypatch) -> None:
    # The server picks both its ports itself: a port the test picked and closed first could be
    # taken by another test's server. The start waits until this server says it is ready, and
    # its startup log names the port it listens on.
    monkeypatch.setattr(nats_service, "INTERNAL_NATS_PORT", -1)
    monkeypatch.setattr(nats_service, "INTERNAL_NATS_MONITOR_PORT", -1)
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

        listening = _LISTENING.search((tmp_path / "nats" / "log" / "nats-server-startup.log").read_text())
        assert listening is not None
        url = f"nats://127.0.0.1:{listening.group(1)}"

        async def probe() -> None:
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
