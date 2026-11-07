"""Live check that the managed local NATS server requires authentication.

Agent-run code executes in a sandbox that cannot read runtime credentials, but
it can open network connections. The local message bus must therefore reject
connections that do not present the generated credentials.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import shutil
import socket
import urllib.request
from pathlib import Path

import nats
import pytest

import docker.nats_service as nats_service


def _free_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return int(probe.getsockname()[1])


def _connect_reply(port: int, env: dict[str, str], *, padding: int) -> str:
    """Send one CONNECT line carrying ``padding`` bytes (as a host's credential would) and PING."""

    line = json.dumps(
        {"user": env["NATS_USER"], "pass": env["NATS_PASSWORD"], "verbose": False, "pedantic": False,
         "name": "x" * padding}
    )
    with socket.create_connection(("127.0.0.1", port), timeout=5) as conn:
        conn.recv(4096)  # the server's INFO line
        conn.sendall(f"CONNECT {line}\r\nPING\r\n".encode())
        reply = b""
        while b"\r\n" not in reply:
            chunk = conn.recv(4096)
            if not chunk:
                break
            reply += chunk
    return reply.decode(errors="replace")


@pytest.mark.skipif(shutil.which("nats-server") is None, reason="nats-server binary is not installed")
def test_managed_nats_rejects_unauthenticated_clients(tmp_path: Path, monkeypatch) -> None:
    port = _free_port()
    monkeypatch.setattr(nats_service, "INTERNAL_NATS_PORT", port)
    monitor_port = _free_port()
    monkeypatch.setattr(nats_service, "INTERNAL_NATS_MONITOR_PORT", monitor_port)
    # Like the launcher, pass a full environment without NATS credentials.
    env = {key: value for key, value in os.environ.items() if not key.startswith("NATS_")}
    process = nats_service.start_local_nats(
        env=env,
        logger=logging.getLogger("test_local_nats_auth_live"),
        cwd=tmp_path,
        service_root=tmp_path / "nats",
    )
    jetstream, control_line, _minimum = nats_service.contract_server_settings()
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
            # The contract's server settings are in force (docker.nats_service.contract_server_settings).
            # Straight to the local server, whatever HTTP proxy the environment names.
            local = urllib.request.build_opener(urllib.request.ProxyHandler({}))
            varz = json.loads(local.open(f"http://127.0.0.1:{monitor_port}/varz", timeout=2).read())
            assert varz["max_control_line"] == control_line
            assert varz["jetstream"]["config"]["sync_always"] is (jetstream["sync_interval"] == "always")
            # The configuration's jetstream block keeps the store folder the command line names (-sd),
            # so an upgraded instance finds its queued work.
            assert varz["jetstream"]["config"]["store_dir"] == str(tmp_path / "nats" / "data" / "jetstream")

        asyncio.run(probe())
        # A connection line as long as the largest credential the contract allows, plus the rest
        # of a CONNECT line, is accepted; a longer one is refused.
        credential_bytes = control_line - nats_service.CONNECT_LINE_ROOM_BYTES
        assert _connect_reply(port, env, padding=credential_bytes).startswith("PONG")
        assert "maximum control line exceeded" in _connect_reply(port, env, padding=control_line).lower()
    finally:
        process.terminate()
        process.wait(timeout=10)
