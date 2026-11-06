"""The team services as a process the launcher supervises (``assistant_cli run-team-services``).

It runs as host ``team-services``: it joins the installation, waits for the team's
single-instance lock, serves the team's address, then writes its host record, and keeps
its host upkeep (liveness, trust, renewal) and the lock's lease until it is stopped. Losing the lock (another
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
from typing import Callable, Mapping, Optional, Sequence

from access_control.people_directory import PeopleDirectory
from assistant_gateway.config import GatewayInstanceConfig
from custom_messages.wire.frame import Receiver
from custom_transport.nats_adapter import NatsTransportAdapter, NatsTransportConfig
from custom_transport.protocol1.host_runtime import MessageHost
from custom_transport.protocol1.joining import HostProfile
from custom_transport.protocol1.live import LiveHost
from custom_transport.subjects import NatsSubjectMapper
from release_version import release_version
from storage.access_repository import AccessRepository
from runtime_settings.knowledge import knowledge_settings
from custom_agents.conversation_store import ConversationStore
from custom_agents.schedule_store import ScheduleStore
from custom_transport.protocol1.delivery_refs import DeliveryRefs
from storage.knowledge_repository import create_knowledge_repository
from storage.operation_outcomes import OperationOutcomeStore
from storage.pinned_memory_repository import create_pinned_memory_repository
from team_services.lock import LockLost, SingleInstanceLock
from team_services.knowledge import KnowledgeOperations
from team_services.memory import MemoryOperations
from team_services.relays import Relays
from team_services.schedules import ScheduleOperations, SystemScheduleCheck
from team_services.topics import TopicOperations
from team_services.service import Handler, TeamServices
from team_services.serving import mark_serving
from team_services.sessions import SessionService

TEAM_SERVICES_RUNTIME = "team-services"
TEAM_SERVICES_ROLES = ("team_services",)
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
    lost = False
    upkeep: Optional[asyncio.Task[None]] = None
    try:
        await host.publish_liveness("accepting")
        host.logger.info("The team services are serving. event=team_services_ready team=%s operations=%s", team,
                         ",".join(sorted(services.served)))
        # The launcher waits for this (team_services.serving): it comes with the host record, once they serve.
        mark_serving(host.layout, lock.instance, host.clock())
        upkeep = asyncio.create_task(host.keep_up(stop, drained=lambda: not lost))
        await lock.keep(stop)
    except LockLost:
        lost = True  # another instance may serve now and write the record; this one writes no more
        raise
    finally:
        await subscription.unsubscribe()
        stop.set()
        if upkeep is not None:
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
    conversations = ConversationStore(db_path=storage.sqlite_path or "", storage_settings=storage, logger=_LOGGER,
                                      artifacts_dir=config.layout.conversation_artifacts_dir)
    topics = TopicOperations(conversations, OperationOutcomeStore(storage, logger=_LOGGER), people, clock, _LOGGER)
    schedules = ScheduleOperations(ScheduleStore(storage_settings=storage), people, _delivery_refs(config),
                                   SystemScheduleCheck(config, people), clock, _LOGGER)
    return [memory.handlers(), knowledge.handlers(), topics.handlers(), schedules.handlers()]


def _delivery_refs(config: GatewayInstanceConfig) -> DeliveryRefs | None:
    """The instance's delivery reference key, which the launcher creates; without it chat lists are refused."""

    try:
        return DeliveryRefs.load(config.layout, config.manager.team_id)
    except Exception:
        _LOGGER.warning("The delivery reference key cannot be read; a chat's own schedule list is refused until the "
                        "launcher creates it. event=team_services_delivery_ref_key_missing", exc_info=True)
        return None


def _started(host: MessageHost) -> LiveHost:
    assert host.live is not None, "the team services serve only after joining"
    return host.live


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
        relays = Relays(lambda: _started(host), host.nc.jetstream(), team, config.manager.named_agent_type, _LOGGER)
        await serve_team(host, team, stop, [*team_operations(config, host.clock), relays.handlers()])
    finally:
        with contextlib.suppress(Exception):
            await adapter.close()
