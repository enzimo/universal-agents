"""The installation authority as a process the launcher supervises (``assistant_cli run-installation-authority``).

It connects to NATS as host ``authority`` with its own reply inbox, sets up the
topology (every team's streams with the instance's ``transport.queues`` limits, and the
buckets) and its records (``service.InstallationAuthority.start``), answers joins and
renewals, keeps its records current (``InstallationAuthority.keep_current``) and each
placed agent's consumers in step with the placements
(``InstallationAuthority.follow_placements``) until it is stopped. The launcher starts it once NATS and the team's message streams are
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

from access_control.people import PERSON_ROLE_ADMIN
from assistant_gateway.config import GatewayInstanceConfig
from custom_messages.contracts.access_v2 import AccessNoticeKind, AccessNoticeV1
from custom_messages.wire.queues import QueueLimits
from custom_transport.nats_adapter import NatsTransportAdapter, NatsTransportConfig
from custom_transport.protocol1.dead_letters import NearExpiryMessage, format_near_expiry_notice
from custom_transport.protocol1.joining import certificate_agent_types
from custom_transport.protocol1.host_keys import load_authority_keys, load_host_keys
from custom_transport.protocol1.live import utc_now
from custom_transport.subjects import NatsSubjectMapper
from installation_authority.refusals import AuthorityStartRefused
from installation_authority.service import AUTHORITY_RUNTIME, InstallationAuthority
from storage.access_repository import AccessRepository

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


def catalog_agent_types(config: GatewayInstanceConfig) -> frozenset[str]:
    """The instance's agent types a certificate can name, read again at each join (``expected_hosts``)."""

    from custom_agents.agent_catalog import instance_agent_catalog

    catalog = instance_agent_catalog(str(config.instance_root_path), config.agent.agent_definition_paths)
    return certificate_agent_types(catalog.agent_types, _LOGGER)


def queue_limits(config: GatewayInstanceConfig) -> QueueLimits:
    """What sizes every team's streams: ``transport.queues`` and the operator's replica count."""

    queues = config.transport.queues
    return QueueLimits(max_bytes=queues.max_bytes.model_dump(), max_messages_per_subject=queues.max_messages_per_subject,
                       replicas=config.transport.replicas)


async def run_installation_authority(config: GatewayInstanceConfig) -> None:
    layout = config.layout
    nats_settings = getattr(config.transport, "nats", None)
    credential_lifetime_seconds = (
        nats_settings.credential_lifetime_seconds
        if nats_settings is not None
        else None
    )
    storage_settings = getattr(config, "manager_storage_settings", None)
    authority_kwargs: dict[str, Any] = {
        "partitions": config.transport.queues.partitions,
        "queues": queue_limits(config),
        "logger": _LOGGER,
        "catalog_agent_types": lambda: catalog_agent_types(config),
    }
    if storage_settings is not None:
        access_repo = AccessRepository(storage_settings)

        async def notify_near_expiry(team: str, item: NearExpiryMessage) -> None:
            notice_id, title, summary = format_near_expiry_notice(team, item)
            notice = AccessNoticeV1(
                notice_id=notice_id,
                team_id=team,
                kind=AccessNoticeKind.QUEUED_MESSAGE_EXPIRING,
                recipient_roles=(PERSON_ROLE_ADMIN,),
                title=title,
                summary=summary,
                created_at_utc=utc_now(),
            )
            created = await asyncio.to_thread(access_repo.create_notice_if_absent, notice)
            if created:
                _LOGGER.warning(
                    "Told administrators about a queued message nearing expiry in UA1_TEAM_WORK. "
                    "event=authority_near_expiry_notice team=%s notice_id=%s stream=%s first_seq=%s",
                    team,
                    notice_id,
                    item.stream,
                    item.first_seq,
                )

        authority_kwargs["on_near_expiry"] = notify_near_expiry
    if credential_lifetime_seconds is not None:
        authority_kwargs["credential_lifetime_seconds"] = credential_lifetime_seconds
    authority = InstallationAuthority(layout, load_authority_keys(layout), load_host_keys(layout, AUTHORITY_RUNTIME),
                                      **authority_kwargs)
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
        placements = asyncio.create_task(authority.follow_placements(stop))
        advisories = asyncio.create_task(authority.watch_delivery_advisories(stop))
        stopping = asyncio.create_task(stop.wait())
        tasks = [upkeep, placements, advisories, stopping]
        await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
        if upkeep.done():
            _LOGGER.error("The installation authority stopped keeping its records current and exits. "
                          "event=installation_authority_upkeep_stopped error=%s", upkeep.exception())
            raise SystemExit(1)
        if placements.done() and not stop.is_set():
            # It opens failed watches again itself, so it ends early only on an error of its own.
            _LOGGER.error("The installation authority stopped following placements and exits. "
                          "event=installation_authority_placements_stopped error=%s",
                          placements.exception() if not placements.cancelled() else "cancelled")
            raise SystemExit(1)
        if advisories.done() and not stop.is_set():
            _LOGGER.error("The installation authority stopped watching delivery advisories and exits. "
                          "event=installation_authority_advisories_stopped error=%s",
                          advisories.exception() if not advisories.cancelled() else "cancelled")
            raise SystemExit(1)
        _LOGGER.info("The installation authority is stopping. event=installation_authority_stopping")
    finally:
        for task in tasks:
            task.cancel()
        # The upkeep's own error was logged above; gathering keeps it from replacing the exit.
        await asyncio.gather(*tasks, return_exceptions=True)
        await adapter.close()
