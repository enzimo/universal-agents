"""Protocol 1 for live tests that run real agents in this process, as the launcher sets it up.

The installation authority, the team services and this process's message host (the team
lead's host, ``custom_agents.agent_host``) run on the shared live NATS server; agents this
test starts afterwards attach to the host on their own, as under the launcher
(``strands_main.py --host-runtime``). Use it as ``async with protocol1_team(...) as host``.

When it ends, also after a failure, it removes what the test's installation and team left on
the shared server, by their exact ids: the team's streams, its keys in the protocol 1 buckets
and its dead-letter stream (``_remove_what_the_team_left``).
"""

from __future__ import annotations

import asyncio
import contextlib
from contextlib import AsyncExitStack, asynccontextmanager
from pathlib import Path
from typing import AsyncIterator, Iterable

import nats
from nats.js.errors import NotFoundError

from custom_agents.agent_host import AgentProcessHost, lead_host_profile
from custom_agents.agent_requests import configure_process_host
from custom_agents.live_controls import EVENT_HANDLERS, LIVE_CONTROL_HANDLERS
from custom_agents.relayed_task_controls import RELAYED_HANDLERS
from custom_transport.protocol1.host_keys import load_authority_keys, load_host_keys
from custom_transport.protocol1.host_runtime import MessageHost
from docker import message_hosts
from installation_authority.service import AUTHORITY_RUNTIME, InstallationAuthority
from live_nats_client import delete_team_streams, nats_transport_config_from_env
from runtime_settings.instance_layout import InstanceLayout
from team_services.process import TEAM_SERVICES_RUNTIME, serve_team, team_services_profile


async def _connect(stack: AsyncExitStack, runtime: str):
    config = nats_transport_config_from_env()
    connection = await nats.connect(servers=list(config.servers), user=config.user, password=config.password,
                                    inbox_prefix=f"_INBOX.{runtime}".encode(), allow_reconnect=False)
    stack.push_async_callback(connection.close)
    return connection


async def _remove_what_the_team_left(nc, installation: str, team: str) -> None:
    """Remove what this test's installation and team wrote on the shared live server, by exact keys and names.

    The ``UA1_*`` buckets are shared by every run on the server, so they stay; only the keys under this
    test's own installation and team ids go, and the team's dead-letter stream and its other streams.
    Host records (``UA1_HOSTS``, keyed by host id alone, which other runs share) expire on their own
    within a minute.
    """

    jetstream = nc.jetstream()
    owned = {"UA1_CERTS": (f"host.{installation}.>", f"revocations.{installation}", f"session.{installation}.{team}.>"),
             "UA1_AGENTS": (f"{team}.>",), "UA1_PLACEMENT": (f"{team}.>",), "UA1_TEAM_VIEW": (f"{team}.>",),
             "UA1_BLOCK_LIST": (team,), "UA1_LOCKS": (f"team_services.{team}",)}
    for bucket, keys in owned.items():
        for key in keys:
            with contextlib.suppress(NotFoundError):
                await jetstream.purge_stream(f"KV_{bucket}", subject=f"$KV.{bucket}.{key}")
    with contextlib.suppress(NotFoundError):
        await jetstream.delete_stream(f"UA1_DLQ_{team}")
    await delete_team_streams(team)


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
        installation = message_hosts.prepare_message_host_trust(layout, hosts)
        authority = InstallationAuthority(layout, load_authority_keys(layout), load_host_keys(layout, AUTHORITY_RUNTIME))
        authority_nc = await _connect(stack, AUTHORITY_RUNTIME)
        await authority.start(authority_nc, teams=[team_id])
        services = MessageHost(layout, services_profile, await _connect(stack, TEAM_SERVICES_RUNTIME))
        serving = asyncio.create_task(serve_team(services, team_id, stop))
        host = AgentProcessHost(layout, lead_profile, team_id, handlers={**RELAYED_HANDLERS, **LIVE_CONTROL_HANDLERS},
                                event_handlers=dict(EVENT_HANDLERS))
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
            await _remove_what_the_team_left(authority_nc, installation, team_id)
