"""Protocol 1 for live tests that run real agents in this process, as the launcher sets it up.

The installation authority, the team services and this process's message host (the team
lead's host, ``custom_agents.agent_host``) run on the shared live NATS server; agents this
test starts afterwards attach to the host on their own, as under the launcher
(``strands_main.py --host-runtime``). Use it as ``async with protocol1_team(...) as host``.
"""

from __future__ import annotations

import asyncio
import contextlib
from contextlib import AsyncExitStack, asynccontextmanager
from pathlib import Path
from typing import AsyncIterator, Iterable

import nats

from custom_agents.agent_host import AgentProcessHost, configure_process_host, lead_host_profile
from custom_agents.relayed_task_controls import RELAYED_HANDLERS
from custom_transport.protocol1.host_keys import load_authority_keys, load_host_keys
from custom_transport.protocol1.host_runtime import MessageHost
from docker import message_hosts
from installation_authority.service import AUTHORITY_RUNTIME, InstallationAuthority
from live_nats_client import nats_transport_config_from_env
from runtime_settings.instance_layout import InstanceLayout
from team_services.process import TEAM_SERVICES_RUNTIME, serve_team, team_services_profile


async def _connect(stack: AsyncExitStack, runtime: str):
    config = nats_transport_config_from_env()
    connection = await nats.connect(servers=list(config.servers), user=config.user, password=config.password,
                                    inbox_prefix=f"_INBOX.{runtime}".encode(), allow_reconnect=False)
    stack.push_async_callback(connection.close)
    return connection


@asynccontextmanager
async def protocol1_team(tmp_path: Path, team_id: str, agent_types: Iterable[str], *,
                         session_limit: int = 8) -> AsyncIterator[AgentProcessHost]:
    layout = InstanceLayout.at(tmp_path / "protocol1")
    lead_profile = lead_host_profile(team_id, agent_types, session_limit)
    services_profile = team_services_profile(team_id)
    stop = asyncio.Event()
    async with AsyncExitStack() as stack:
        hosts = {profile.runtime: message_hosts.expected_host(layout, profile)
                 for profile in (services_profile, lead_profile)}
        message_hosts.prepare_message_host_trust(layout, hosts)
        authority = InstallationAuthority(layout, load_authority_keys(layout), load_host_keys(layout, AUTHORITY_RUNTIME))
        await authority.start(await _connect(stack, AUTHORITY_RUNTIME), teams=[team_id])
        services = MessageHost(layout, services_profile, await _connect(stack, TEAM_SERVICES_RUNTIME))
        serving = asyncio.create_task(serve_team(services, team_id, stop))
        host = AgentProcessHost(layout, lead_profile, team_id, handlers=dict(RELAYED_HANDLERS))
        configure_process_host(host)
        try:
            yield host
        finally:
            configure_process_host(None)
            with contextlib.suppress(Exception):
                await asyncio.wait_for(host.stop(), timeout=10)
            stop.set()
            with contextlib.suppress(Exception):
                await asyncio.wait_for(serving, timeout=10)
