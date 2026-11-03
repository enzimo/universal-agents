"""The installation authority as a process the launcher supervises (``assistant_cli run-installation-authority``).

It connects to NATS as host ``authority`` with its own reply inbox, sets up the
topology and its records (``service.InstallationAuthority.start``), answers joins and
renewals, and signs the revocations record again every six hours until it is stopped.
The launcher starts it right after NATS and waits for its certificate in the
bootstrap bundle before it starts any other host.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import signal

from assistant_gateway.config import GatewayInstanceConfig
from custom_transport.nats_adapter import NatsTransportAdapter, NatsTransportConfig
from custom_transport.protocol1.host_keys import load_authority_keys, load_host_keys
from custom_transport.subjects import NatsSubjectMapper
from installation_authority.service import AUTHORITY_RUNTIME, InstallationAuthority

_LOGGER = logging.getLogger("installation_authority")


def transport_config(config: GatewayInstanceConfig) -> NatsTransportConfig:
    """The authority's NATS connection: the instance's servers and login, and its own reply inbox."""

    transport = config.transport
    servers = list(transport.nats_servers or ([transport.nats_url] if transport.nats_url else []))
    return NatsTransportConfig(
        servers=servers,
        name=f"{config.instance_name}-installation-authority",
        subject_prefix=transport.subject_prefix,
        # It uses protocol 1's streams and buckets only, not the team's old work streams.
        jetstream_enabled=False,
        user=transport.nats_user,
        password=transport.nats_password,
        credentials_file=transport.credentials_file,
        reply_inbox_runtime=AUTHORITY_RUNTIME,
    )


async def run_installation_authority(config: GatewayInstanceConfig) -> None:
    layout = config.layout
    authority = InstallationAuthority(layout, load_authority_keys(layout), load_host_keys(layout, AUTHORITY_RUNTIME),
                                      logger=_LOGGER)
    adapter = NatsTransportAdapter(config=transport_config(config),
                                   mapper=NatsSubjectMapper(team_id=config.manager.team_id), logger=_LOGGER)
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for signum in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(signum, stop.set)
    await adapter.connect()
    resign = None
    try:
        await authority.start(adapter.nats_connection, teams=[config.manager.team_id])
        resign = asyncio.create_task(authority.resign_revocations_forever())
        await stop.wait()
        _LOGGER.info("The installation authority is stopping. event=installation_authority_stopping")
    finally:
        if resign is not None:
            resign.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await resign
        await adapter.close()
