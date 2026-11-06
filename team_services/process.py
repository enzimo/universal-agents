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
from datetime import datetime
from typing import Callable, Mapping, Sequence

from access_control.people_directory import PeopleDirectory
from assistant_gateway.config import GatewayInstanceConfig
from custom_messages.wire.frame import Receiver
from custom_transport.nats_adapter import NatsTransportAdapter, NatsTransportConfig
from custom_transport.protocol1.host_runtime import MessageHost
from custom_transport.protocol1.joining import HostProfile
from custom_transport.subjects import NatsSubjectMapper
from release_version import release_version
from storage.access_repository import AccessRepository
from runtime_settings.knowledge import knowledge_settings
from storage.knowledge_repository import create_knowledge_repository
from storage.pinned_memory_repository import create_pinned_memory_repository
from team_services.lock import SingleInstanceLock
from team_services.knowledge import KnowledgeOperations
from team_services.memory import MemoryOperations
from team_services.service import Handler, TeamServices
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


async def serve_team(host: MessageHost, team: str, stop: asyncio.Event,
                     families: Sequence[Mapping[str, Handler]] = ()) -> None:
    """Hold the lock, serve the team's address and keep the host and the lock up until ``stop``.

    ``families`` are the handlers of people's commands (``team_operations``).
    """

    live = await host.start()
    lock = SingleInstanceLock(host.nc.jetstream(), team, TEAM_SERVICES_RUNTIME, host.clock, host.logger)
    if not await lock.acquire(stop):
        return
    assert host.records is not None  # start() made them
    sessions = SessionService(host.nc.jetstream(), host.records, live.trust, live.identity.installation, host.clock,
                              host.logger)
    services = TeamServices(sessions, families)
    subscription = await live.serve("svc.team", Receiver(live.identity.installation, team, "team_services", team,
                                                         TEAM_SERVICES_RUNTIME), services.handle)
    await host.nc.flush()
    host.logger.info("The team services are serving. event=team_services_ready team=%s operations=%s", team,
                     ",".join(sorted(services.served)))
    upkeep = asyncio.create_task(host.keep_up(stop))
    try:
        await lock.keep(stop)
    finally:
        await subscription.unsubscribe()
        stop.set()
        await upkeep
        await lock.release()


def team_operations(config: GatewayInstanceConfig, clock: Callable[[], datetime]) -> list[Mapping[str, Handler]]:
    """The families of people's commands this process answers, on the team's database."""

    storage = config.manager_storage_settings
    people = PeopleDirectory(AccessRepository(storage), config.manager.team_id)
    memory_store = create_pinned_memory_repository(storage, logger=_LOGGER)
    memory = MemoryOperations(memory_store, people, dict(config.agent.memory), clock, _LOGGER)
    settings = knowledge_settings(config.agent.knowledge)
    # With the knowledge base off no store is opened, and its commands are answered feature_off.
    knowledge_store = create_knowledge_repository(storage, logger=_LOGGER) if settings.enabled else None
    knowledge = KnowledgeOperations(knowledge_store, people, settings, clock, _LOGGER, personal_memory=memory_store)
    return [memory.handlers(), knowledge.handlers()]


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
    finally:
        with contextlib.suppress(Exception):
            await adapter.close()
