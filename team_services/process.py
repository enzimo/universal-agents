"""The team services as a process the launcher supervises (``assistant_cli run-team-services``).

It runs as host ``team-services``: it joins the installation, waits for the team's
single-instance lock, and keeps the lock's lease from the moment it holds it. Before it
serves it signs with the host's newest certificate (every instance runs as this one host,
and the serving instance may have renewed their certificate while this one waited,
``MessageHost.take_newest_certificate``) and reads the current revocations record (a
standby may have waited longer than the one it read at start lasts). Then it serves the
team's address, writes its host record, and keeps its host upkeep (liveness, trust,
renewal), its own records' upkeep (``SessionService.maintain``: placements signed again
under the current certificate, forgotten sessions deleted) and the lock's lease until it
is stopped.

Stopping releases the lock even when unsubscribing or writing the last host record
failed, so the next instance does not wait out the lease. Losing the lock (another
instance holds it, or its lease ran out without a successful renewal), also while it
prepares to serve, stops the process with exit code 1, so the launcher stops the team
rather than letting two instances serve; so does a NATS connection that closed for good.
A renewal that only failed, such as NATS not answering for a few seconds, is tried again
while the lease lasts and does not stop anything. An attempt to take the lock that failed
that way is tried again too, until it succeeds or the process is stopped
(``team_services.lock``).
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import signal
from datetime import datetime
from typing import Callable, Mapping, Optional, Sequence

from access_control.people_directory import PeopleDirectory
from assistant_gateway.config import GatewayInstanceConfig
from custom_messages.wire.frame import Receiver
from custom_transport.nats_adapter import NatsTransportAdapter, NatsTransportConfig
from custom_transport.protocol1.host_runtime import TEAM_SERVICES_RUNTIME, TRUST_REFRESH_INTERVAL, MessageHost
from custom_transport.protocol1.joining import HostProfile
from custom_transport.protocol1.live import LiveHost
from custom_transport.subjects import NatsSubjectMapper
from release_version import release_version
from storage.access_repository import AccessRepository
from storage.pinned_memory_repository import create_pinned_memory_repository
from team_services.lock import LockLost, SingleInstanceLock
from team_services.memory import MemoryOperations
from team_services.service import Handler, TeamServices
from team_services.serving import mark_serving
from team_services.sessions import SessionService

TEAM_SERVICES_ROLES = ("team_services",)
# How often the team services look after their own records (SessionService.maintain).
MAINTENANCE_INTERVAL = TRUST_REFRESH_INTERVAL
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


async def serve_team(host: MessageHost, team: str, stop: asyncio.Event,
                     families: Sequence[Mapping[str, Handler]] = ()) -> None:
    """Hold the lock, serve the team's address and keep the host and the lock up until ``stop``.

    ``families`` are the handlers of people's commands (``team_operations``).
    """

    # Every instance writes the same host record, so it is written only by the one that holds the
    # lock and serves (plan 13B.4): a standby never looks like the serving instance, and the
    # record appearing means the team services answer.
    live = await host.start(report=False)
    lock = SingleInstanceLock.on_connection(host.nc, team, TEAM_SERVICES_RUNTIME, host.clock, host.logger)
    if not await lock.acquire(stop):
        return
    # The lease is kept from now on, through the preparation before serving: a slow catch-up must not let
    # it run out while a standby waits (module docstring).
    keeper = asyncio.create_task(lock.keep(stop))
    try:
        await _serve_holding_lock(host, live, lock, keeper, team, stop, families)
    finally:
        if not keeper.done():
            keeper.cancel()
        await asyncio.gather(keeper, return_exceptions=True)
        # Released whatever happened while serving or stopping, so the next instance (a restart of every
        # service) takes over at once instead of waiting out the lease; an error raised there is still raised.
        await lock.release()


async def _prepare(host: MessageHost) -> None:
    # All instances share one host certificate, and a standby may have waited long enough for the serving
    # instance to renew it, and for the revocations record it read at start to end.
    await host.take_newest_certificate()
    await host.refresh_trust()


async def _serve_holding_lock(host: MessageHost, live: LiveHost, lock: SingleInstanceLock,
                              keeper: asyncio.Task[None], team: str, stop: asyncio.Event,
                              families: Sequence[Mapping[str, Handler]]) -> None:
    preparing = asyncio.create_task(_prepare(host))
    await asyncio.wait({preparing, keeper}, return_when=asyncio.FIRST_COMPLETED)
    if keeper.done():
        # The lock was lost (or the process stopped) before serving: serve nothing.
        preparing.cancel()
        await asyncio.gather(preparing, return_exceptions=True)
        await keeper
        return
    await preparing
    assert host.records is not None  # start() made them
    sessions = SessionService(host.records, live.trust, live.identity.installation, host.clock, host.logger)
    services = TeamServices(sessions, families)
    subscription = await live.serve("svc.team", Receiver(live.identity.installation, team, "team_services", team,
                                                         TEAM_SERVICES_RUNTIME), services.handle)
    await host.nc.flush()
    lost = False
    upkeep: Optional[asyncio.Task[None]] = None
    maintenance: Optional[asyncio.Task[None]] = None
    try:
        await host.publish_liveness("accepting")
        host.logger.info("The team services are serving. event=team_services_ready team=%s operations=%s", team,
                         ",".join(sorted(services.served)))
        # The launcher waits for this (team_services.serving): it comes with the host record, once they serve.
        mark_serving(host.layout, lock.instance, host.clock())
        upkeep = asyncio.create_task(host.keep_up(stop, drained=lambda: not lost))
        maintenance = asyncio.create_task(_maintain(sessions, team, stop, host.logger))
        await keeper
    except LockLost:
        lost = True  # another instance may serve now and write the record; this one writes no more
        raise
    finally:
        try:
            await subscription.unsubscribe()
        finally:
            # The upkeep ends and writes its last record (drained) even when unsubscribing failed, and before
            # the caller releases the lock, so it never overwrites the record of the instance that takes over.
            stop.set()
            if maintenance is not None:
                maintenance.cancel()
                await asyncio.gather(maintenance, return_exceptions=True)
            if upkeep is not None:
                await upkeep


async def _maintain(sessions: SessionService, team: str, stop: asyncio.Event, logger: logging.Logger) -> None:
    """``SessionService.maintain`` now and every ``MAINTENANCE_INTERVAL`` until ``stop``; a failed pass is logged."""

    while not stop.is_set():
        try:
            await sessions.maintain(team)
        except Exception:
            logger.exception("The team services could not look after their records; trying again later. "
                             "event=team_services_maintenance_failed team=%s", team)
        with contextlib.suppress(asyncio.TimeoutError):
            await asyncio.wait_for(stop.wait(), timeout=MAINTENANCE_INTERVAL.total_seconds())


def team_operations(config: GatewayInstanceConfig, clock: Callable[[], datetime]) -> list[Mapping[str, Handler]]:
    """The families of people's commands this process answers, on the team's database."""

    storage = config.manager_storage_settings
    people = PeopleDirectory(AccessRepository(storage), config.manager.team_id)
    memory = MemoryOperations(create_pinned_memory_repository(storage, logger=_LOGGER), people,
                              dict(config.agent.memory), clock, _LOGGER)
    return [memory.handlers()]


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
        await serve_team(host, team, stop, team_operations(config, host.clock))
    except LockLost as lost:
        # Exit code 1 for the launcher's supervisor, which stops the team (module docstring).
        _LOGGER.error("The team services stop. event=team_services_stopped reason=%s", lost)
        raise SystemExit(1) from None
    finally:
        with contextlib.suppress(Exception):
            await adapter.close()
