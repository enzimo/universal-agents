"""The team services as a process the launcher supervises (``assistant_cli run-team-services``).

It runs as host ``team-services``: it joins the installation, waits for the team's
single-instance lock, serves the team's address, and keeps its host upkeep (liveness,
trust, renewal) and the lock's lease until it is stopped. Losing the lock (another
instance holds it, or its lease ran out without a successful renewal) stops the process
with an error, so the launcher stops the team rather than letting two instances serve. A
renewal that only failed, such as NATS not answering for a few seconds, is tried again
while the lease lasts and does not stop anything. An attempt to take the lock that failed
that way is tried again too, until it succeeds or the process is stopped
(``team_services.lock``).
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import signal

from assistant_gateway.config import GatewayInstanceConfig
from custom_messages.wire.frame import Receiver
from custom_transport.nats_adapter import NatsTransportAdapter, NatsTransportConfig
from custom_transport.protocol1.host_runtime import MessageHost
from custom_transport.protocol1.joining import HostProfile
from custom_transport.subjects import NatsSubjectMapper
from release_version import release_version
from team_services.lock import SingleInstanceLock
from team_services.service import TeamServices
from team_services.serving import mark_serving
from team_services.sessions import SessionService

TEAM_SERVICES_RUNTIME = "team-services"
# Plan decision 58: the scheduler role (signing scheduled runs) runs in this process.
TEAM_SERVICES_ROLES = ("team_services", "scheduler")
_LOGGER = logging.getLogger("team_services")


def team_services_profile(team: str) -> HostProfile:
    return HostProfile(TEAM_SERVICES_RUNTIME, (team,), TEAM_SERVICES_ROLES,
                       software={"name": "universal-agents", "version": release_version()})


def transport_config(config: GatewayInstanceConfig) -> NatsTransportConfig:
    transport = config.transport
    servers = list(transport.nats_servers or ([transport.nats_url] if transport.nats_url else []))
    return NatsTransportConfig(servers=servers, name=f"{config.instance_name}-team-services",
                               subject_prefix=transport.subject_prefix, jetstream_enabled=False,
                               user=transport.nats_user, password=transport.nats_password,
                               credentials_file=transport.credentials_file, reply_inbox_runtime=TEAM_SERVICES_RUNTIME)


async def serve_team(host: MessageHost, team: str, stop: asyncio.Event) -> None:
    """Hold the lock, serve the team's address and keep the host and the lock up until ``stop``."""

    live = await host.start()
    lock = SingleInstanceLock(host.nc.jetstream(), team, TEAM_SERVICES_RUNTIME, host.clock, host.logger)
    if not await lock.acquire(stop):
        return
    assert host.records is not None  # start() made them
    sessions = SessionService(host.nc.jetstream(), host.records, live.trust, live.identity.installation, host.clock,
                              host.logger)
    services = TeamServices(sessions)
    subscription = await live.serve("svc.team", Receiver(live.identity.installation, team, "team_services", team,
                                                         TEAM_SERVICES_RUNTIME), services.handle)
    await host.nc.flush()
    host.logger.info("The team services are serving. event=team_services_ready team=%s operations=%s", team,
                     ",".join(sorted(services.served)))
    mark_serving(host.layout, lock.instance, host.clock())  # the launcher waits for this (team_services.serving)
    upkeep = asyncio.create_task(host.keep_up(stop))
    try:
        await lock.keep(stop)
    finally:
        await subscription.unsubscribe()
        stop.set()
        await upkeep
        await lock.release()


async def run_team_services(config: GatewayInstanceConfig) -> None:
    team = config.manager.team_id
    adapter = NatsTransportAdapter(config=transport_config(config), mapper=NatsSubjectMapper(team_id=team), logger=_LOGGER)
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for signum in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(signum, stop.set)
    await adapter.connect()
    try:
        host = MessageHost(config.layout, team_services_profile(team), adapter.nats_connection, logger=_LOGGER)
        await serve_team(host, team, stop)
    finally:
        with contextlib.suppress(Exception):
            await adapter.close()
