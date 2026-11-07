"""The installation authority as a process the launcher supervises (``assistant_cli run-installation-authority``).

It connects to NATS as host ``authority`` with its own reply inbox, sets up the
topology and its records (``service.InstallationAuthority.start``), answers joins and
renewals, and keeps its records current (``InstallationAuthority.keep_current``) until
it is stopped. The launcher starts it once NATS and the team's message streams are
ready, and waits for its certificate in the bootstrap bundle before it starts any other
host.

It exits with code 1, which the launcher's supervisor sees, when it cannot start safely
(``AuthorityStartRefused``: the log line says what to do) and when its NATS connection
closed for good: alive but unreachable, it would answer no join or renewal and let the
revocations record expire.
"""

from __future__ import annotations

import asyncio
import logging
import signal
from typing import Any

from assistant_gateway.config import GatewayInstanceConfig
from custom_transport.nats_adapter import NatsTransportAdapter, NatsTransportConfig
from custom_transport.protocol1.host_keys import load_authority_keys, load_host_keys
from custom_transport.subjects import NatsSubjectMapper
from installation_authority.service import AUTHORITY_RUNTIME, AuthorityStartRefused, InstallationAuthority

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
                                      logger=_LOGGER, replicas=config.transport.replicas)
    adapter = NatsTransportAdapter(config=transport_config(config),
                                   mapper=NatsSubjectMapper(team_id=config.manager.team_id), logger=_LOGGER)
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for signum in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(signum, stop.set)
    await adapter.connect()
    tasks: list[asyncio.Task[Any]] = []
    try:
        try:
            await authority.start(adapter.nats_connection, teams=[config.manager.team_id])
        except AuthorityStartRefused as refused:
            _LOGGER.error("The installation authority cannot start. event=installation_authority_start_refused "
                          "reason=%s %s", type(refused).__name__, refused)
            raise SystemExit(1) from None
        upkeep = asyncio.create_task(authority.keep_current())
        stopping = asyncio.create_task(stop.wait())
        tasks = [upkeep, stopping]
        await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
        if upkeep.done():
            _LOGGER.error("The installation authority stopped keeping its records current and exits. "
                          "event=installation_authority_upkeep_stopped error=%s", upkeep.exception())
            raise SystemExit(1)
        _LOGGER.info("The installation authority is stopping. event=installation_authority_stopping")
    finally:
        for task in tasks:
            task.cancel()
        # The upkeep's own error was logged above; gathering keeps it from replacing the exit.
        await asyncio.gather(*tasks, return_exceptions=True)
        await adapter.close()
