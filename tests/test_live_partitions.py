"""End-to-end live nats-server 2.15.1 tests for phase 3 step 2b (A9):

- Partition ownership assignment, balance, ``partition_not_owned`` refusal, and
  partition-scoped agent ID generation across real ``InstallationAuthority``,
  ``PartitionService`` + ``SessionService``, and ``AgentProcessHost`` runtimes.
- Connected four-step partition handover with real operator-mode ``nats-server``
  revocation (``OperatorModeRevoker``) closing an active old-key connection
  immediately and blocking reconnect while the renewed key connects and the
  receiving host adopts the partition and moved agent.
- Cut-off and retired host bulk release and group reassignment (both operator-mode
  immediate revocation and external-server ``credential_lifetime_seconds +
  clock_skew_seconds`` fallback).
- Several gateways per installation joining and connecting with distinct and
  overlapping team/route credentials alongside the default single-gateway setup.
"""

from __future__ import annotations

import asyncio
from contextlib import AsyncExitStack
from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import nats
import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from assistant_gateway.message_host import GATEWAY_RUNTIME, GatewayMessageHost, gateway_profile
from custom_agents.agent_host import (
    LEAD_HOST_RUNTIME,
    AgentProcessHost,
    definition_sha256,
    lead_host_profile,
    startup_agent_host_profile,
)
from custom_messages.wire.frame import Receiver
from custom_messages.wire.kinds import CLOCK_SKEW_SECONDS
from custom_messages.wire.partitions import OWNED, RELEASED, agent_partition, partition_owner_key
from custom_messages.wire.values import parse_timestamp
from custom_transport.protocol1 import host_keys
from custom_transport.protocol1.host_partitions import draw_agent_id
from custom_transport.protocol1.host_runtime import MessageHost
from custom_transport.protocol1.live import utc_now
from custom_transport.protocol1.nats_jwt import NatsSigningKey, decode_jwt_payload
from docker import message_hosts
from installation_authority.revocation import LifetimeWaitRevoker, OperatorModeRevoker
from installation_authority.service import AUTHORITY_RUNTIME, InstallationAuthority
from runtime_settings.instance_layout import InstanceLayout
from team_services.partitions import PartitionService
from team_services.process import TEAM_SERVICES_RUNTIME, serve_team, team_services_profile
from team_services.service import TeamServices
from team_services.sessions import SessionService
from tests.live_nats_support import flush_fully
from tests.own_nats_server import own_nats_server
from tests.queue_limits_support import TEST_QUEUE_LIMITS
from tests.test_live_message_contract import nats_server
from tests.test_live_nats_revocation import PARTITIONS, _OperatorServer, _operator_nats_server

pytestmark = [pytest.mark.integration, pytest.mark.live, pytest.mark.nats]

TEAM_A = "team_alpha"
TEAM_B = "team_beta"
DEFINITION = definition_sha256("cd" * 32)


class _Clock:
    def __init__(self, now: datetime) -> None:
        self.now = now

    def __call__(self) -> datetime:
        return self.now


async def _until(condition, *, timeout: float = 20.0) -> None:
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while not condition():
        if loop.time() >= deadline:
            raise AssertionError("timed out waiting for condition")
        await asyncio.sleep(0.05)


async def _connect(stack: AsyncExitStack, url: str, runtime: str):
    nc = await nats.connect(url, inbox_prefix=f"_INBOX.{runtime}".encode(), allow_reconnect=False)
    stack.push_async_callback(nc.close)
    return nc


async def _connect_operator_user(
    stack: AsyncExitStack,
    server: _OperatorServer,
    runtime: str,
):
    key, jwt_token = server.issue_host_user(runtime)
    nc = await nats.connect(
        server.url,
        **key.login_options(jwt_token),
        inbox_prefix=f"_INBOX.{runtime}".encode(),
        allow_reconnect=False,
    )
    stack.push_async_callback(nc.close)
    return nc


def _signing_key_from_seed(seed: bytes) -> NatsSigningKey:
    return NatsSigningKey.from_private_key("user", Ed25519PrivateKey.from_private_bytes(seed))


def _stand_in_agent(agent_id: str, agent_type: str = "Researcher") -> SimpleNamespace:
    return SimpleNamespace(
        id=agent_id,
        name=f"{agent_type}-{agent_id[:8]}",
        named_agent_type=agent_type,
        _runtime_loop=asyncio.get_running_loop(),
    )


async def _read_all_owners(host: MessageHost, team: str, count: int, *, now: datetime | None = None) -> dict[int, Any]:
    assert host.records is not None
    owners: dict[int, Any] = {}
    read_at = host.clock() if now is None else now
    for p in range(count):
        stored = await host.records.read_report("partition_owner", partition_owner_key(team, p, count), now=read_at)
        if stored is not None:
            owners[p] = stored.record.body
    return owners


@pytest.mark.asyncio
async def test_live_partition_ownership_balance_not_owned_refusal_and_scoped_agent_ids(tmp_path: Path) -> None:
    """A1, A2, A3, A9 on real nats-server 2.15.1: balanced partition assignment, partition_not_owned refusal,
    and <= 32-char partition-scoped agent IDs drawn across least-filled owned partitions."""

    layout = InstanceLayout.at(tmp_path / "instance")
    lead_profile = lead_host_profile(TEAM_A, ("TeamArchitect", "Researcher"), 8)
    worker_profile = startup_agent_host_profile(TEAM_A, "agent-worker-1", "Researcher")
    svc_profile = team_services_profile(TEAM_A)
    stop = asyncio.Event()

    nats_dir = tmp_path / "nats"
    nats_dir.mkdir()
    with own_nats_server(nats_server(), nats_dir, partitions=PARTITIONS) as url:
        async with AsyncExitStack() as stack:
            expected = {
                profile.runtime: message_hosts.expected_host(layout, profile)
                for profile in (svc_profile, lead_profile, worker_profile)
            }
            message_hosts.prepare_message_host_trust(layout, expected, partitions=PARTITIONS)
            authority = InstallationAuthority(
                layout,
                host_keys.load_authority_keys(layout),
                host_keys.load_host_keys(layout, AUTHORITY_RUNTIME),
                partitions=PARTITIONS,
                queues=TEST_QUEUE_LIMITS,
            )
            await authority.start(await _connect(stack, url, AUTHORITY_RUNTIME), teams=[TEAM_A])
            services = MessageHost(layout, svc_profile, await _connect(stack, url, TEAM_SERVICES_RUNTIME))
            serving = asyncio.create_task(serve_team(services, TEAM_A, stop))

            lead_host = AgentProcessHost(layout, lead_profile, TEAM_A)
            worker_host = AgentProcessHost(layout, worker_profile, TEAM_A)
            try:
                lead_host.ensure_started(await _connect(stack, url, LEAD_HOST_RUNTIME))
                worker_host.ensure_started(await _connect(stack, url, worker_profile.runtime))
                await lead_host.wait_for_usable_partition(timeout=15)
                await worker_host.wait_for_usable_partition(timeout=15)

                # Wait until all PARTITIONS are balanced evenly across both hosts.
                await _until(
                    lambda: len(lead_host.usable_partitions()) == PARTITIONS // 2
                    and len(worker_host.usable_partitions()) == PARTITIONS // 2
                    and lead_host.usable_partitions().isdisjoint(worker_host.usable_partitions())
                )
                owners = await _read_all_owners(services, TEAM_A, PARTITIONS)
                assert len(owners) == PARTITIONS
                assert all(body.state == OWNED for body in owners.values())
                lead_parts = {p for p, body in owners.items() if body.host == LEAD_HOST_RUNTIME}
                worker_parts = {p for p, body in owners.items() if body.host == worker_profile.runtime}
                assert lead_parts == set(lead_host.usable_partitions())
                assert worker_parts == set(worker_host.usable_partitions())

                # Draw one agent ID per worker partition: each <= 32 chars, inside worker_parts, spread evenly.
                drawn_ids = [worker_host.draw_agent_id(prefix="worker") for _ in range(len(worker_parts))]
                assert all(len(aid) <= 32 for aid in drawn_ids)
                drawn_parts = [agent_partition(TEAM_A, aid, PARTITIONS) for aid in drawn_ids]
                assert set(drawn_parts) == worker_parts

                # Attach one drawn agent on each host and confirm sessions open cleanly.
                lead_id = lead_host.draw_agent_id(prefix="lead")
                worker_id = drawn_ids[0]
                lead_host.attach(_stand_in_agent(lead_id, "TeamArchitect"), agent_type="TeamArchitect",
                                 definition=DEFINITION, nc=None, state=("idle", True))
                worker_host.attach(_stand_in_agent(worker_id, "Researcher"), agent_type="Researcher",
                                   definition=DEFINITION, nc=None, state=("idle", True))
                await _until(
                    lambda: lead_id in lead_host.agents and lead_host.agents[lead_id].session is not None
                    and worker_id in worker_host.agents and worker_host.agents[worker_id].session is not None
                )

                # sessions.open for an agent outside the asking host's owned partitions is refused partition_not_owned.
                foreign_id = draw_agent_id(TEAM_A, sorted(worker_parts), prefix="foreign", partition_count=PARTITIONS)
                assert lead_host.host is not None and lead_host.host.live is not None
                refused = await lead_host.host.live.request(
                    "sessions.open",
                    {"request_id": "open-foreign-1", "agent": foreign_id, "agent_type": "Researcher",
                     "definition_sha256": DEFINITION},
                    {"installation": lead_host.host.identity.installation, "team": TEAM_A,
                     "target": "team_services", "id": TEAM_A},
                    team=TEAM_A,
                )
                assert refused.raw["body"]["outcome"] == "partition_not_owned"
                assert refused.raw["body"]["partition"] == agent_partition(TEAM_A, foreign_id, PARTITIONS)
            finally:
                await worker_host.stop()
                await lead_host.stop()
                stop.set()
                await asyncio.wait_for(serving, timeout=10)


@pytest.mark.asyncio
async def test_live_connected_four_step_handover_with_operator_mode_revocation(tmp_path: Path) -> None:
    """A4, A5, A7, A9 on real operator-mode nats-server 2.15.1:

    Connected four-step handover driven end-to-end by PartitionService + AgentProcessHost +
    InstallationAuthority + OperatorModeRevoker:
    - Step 1 writes ``released`` and blocks new sessions in ``p`` (``partition_not_owned``).
    - Step 2 drains held work on ``host-a`` and closes the agent's session as ``handed_over``.
    - Step 3 renews ``host-a`` with a new NATS key omitting ``p``; authority immediately revokes
      the old NATS key via ``$SYS.REQ.CLAIMS.UPDATE``, closing an active old-key connection and
      blocking reconnect while the new key connects cleanly.
    - Step 4 writes ``owned`` for ``host-b``, moves the agent's placement to ``host-b``, and
      ``host-b`` renews its credential naming ``p`` and opens a session for the moved agent.

    Note: ``PartitionService(..., is_nats_key_revoked=authority._ledger.is_nats_revocation_confirmed)``
    uses an in-process test seam to exercise step 2b's operator-mode revocation contract before
    step 11 introduces the cross-process revocation signal between separate OS processes.
    """

    nats_dir = tmp_path / "nats"
    nats_dir.mkdir()
    with _operator_nats_server(nats_dir) as server:
        layout = InstanceLayout.at(tmp_path / "instance")
        svc_profile = team_services_profile(TEAM_A)
        host_a_profile = startup_agent_host_profile(TEAM_A, "host-a", "Researcher")
        host_b_profile = startup_agent_host_profile(TEAM_A, "host-b", "Researcher")
        expected = {
            profile.runtime: message_hosts.expected_host(layout, profile)
            for profile in (svc_profile, host_a_profile, host_b_profile)
        }
        message_hosts.prepare_message_host_trust(layout, expected, partitions=PARTITIONS)

        async with AsyncExitStack() as stack:
            sys_key, sys_jwt = server.issue_system_user()
            sys_nc = await nats.connect(server.url, **sys_key.login_options(sys_jwt), allow_reconnect=False)
            stack.push_async_callback(sys_nc.close)

            auth_nc = await _connect_operator_user(stack, server, AUTHORITY_RUNTIME)
            svc_nc = await _connect_operator_user(stack, server, TEAM_SERVICES_RUNTIME)
            host_a_nc = await _connect_operator_user(stack, server, host_a_profile.runtime)
            host_b_nc = await _connect_operator_user(stack, server, host_b_profile.runtime)

            revoker = OperatorModeRevoker(server.operator_signing, server.base_claims, sys_nc)
            authority = InstallationAuthority(
                layout,
                host_keys.load_authority_keys(layout),
                host_keys.load_host_keys(layout, AUTHORITY_RUNTIME),
                partitions=PARTITIONS,
                queues=TEST_QUEUE_LIMITS,
                nats_account_signing_key=server.installation_signing,
                nats_account_public_key=server.installation_account.public_key,
                revoker=revoker,
                credential_lifetime_seconds=300,
            )
            await authority.start(auth_nc, teams=[TEAM_A])

            services_host = MessageHost(layout, svc_profile, svc_nc)
            live = await services_host.start()
            assert services_host.records is not None
            sessions = SessionService(services_host.records, live.trust, live.identity.installation, services_host.clock)
            partitions = PartitionService(
                services_host.records,
                live.trust,
                live.identity.installation,
                TEAM_A,
                sessions._changing,
                services_host.clock,
                credential_lifetime_seconds=300,
                credentials_enforced=True,
                is_nats_key_revoked=authority._ledger.is_nats_revocation_confirmed,
                reconcile_interval_seconds=0.2,
            )
            sessions.ensure_partitions = partitions.ensure_unowned_assigned
            team_svc = TeamServices(sessions)
            sub = await live.serve(
                "svc.team",
                Receiver(live.identity.installation, TEAM_A, "team_services", TEAM_A, TEAM_SERVICES_RUNTIME),
                team_svc.handle,
            )
            await flush_fully(svc_nc)

            stop = asyncio.Event()
            part_task = asyncio.create_task(partitions.run(stop))

            # Stand-in drain hook on host-a: holds one task until step-1 assertions finish
            # and handover drain releases it, preventing steps 2-4 from racing ahead of step-1 checks.
            allow_drain = asyncio.Event()
            held_work = True
            drain_reasons: list[str] = []

            async def drain_agent(_agent: Any, reason: str) -> None:
                nonlocal held_work
                drain_reasons.append(reason)
                held_work = False

            async def holds_work(_agent: Any) -> bool:
                return held_work or not allow_drain.is_set()

            host_a = AgentProcessHost(
                layout, host_a_profile, TEAM_A,
                drain_agent=drain_agent, holds_work=holds_work,
                drain_poll_seconds=0.05, partition_retry_seconds=0.1,
            )
            host_b = AgentProcessHost(
                layout, host_b_profile, TEAM_A,
                drain_poll_seconds=0.05, partition_retry_seconds=0.1,
            )
            old_jwt_nc = None
            new_jwt_nc = None
            try:
                host_a.ensure_started(host_a_nc)
                host_b.ensure_started(host_b_nc)
                await host_a.wait_for_usable_partition(timeout=20)
                await host_b.wait_for_usable_partition(timeout=20)
                # Wait until initial partition balancing across host-a and host-b has settled so
                # background rebalance does not release target_p while attaching the mover agent.
                await _until(
                    lambda: len(host_a.usable_partitions()) == PARTITIONS // 2
                    and len(host_b.usable_partitions()) == PARTITIONS // 2
                    and host_a.usable_partitions().isdisjoint(host_b.usable_partitions()),
                    timeout=20,
                )

                assert host_a.host is not None and host_b.host is not None
                assert host_a.host.connection is not None and host_b.host.connection is not None
                target_p = min(host_a.usable_partitions())
                aid = draw_agent_id(TEAM_A, [target_p], prefix="mover", partition_count=PARTITIONS)
                host_a.attach(_stand_in_agent(aid, "Researcher"), agent_type="Researcher",
                              definition=DEFINITION, nc=None, state=("idle", True))
                await _until(lambda: aid in host_a.agents and host_a.agents[aid].session is not None)

                # Capture host-a's pre-handover NATS key and open an actual operator-mode connection with it.
                old_nats_pub = host_a.host.identity.certificate.nats_public_key
                old_nats_signing = _signing_key_from_seed(host_a.host.identity.keys.nats_seed)
                old_jwt = host_a.host.connection["jwt"]
                old_disconnected = asyncio.Event()

                async def on_old_disconnect() -> None:
                    old_disconnected.set()

                old_jwt_nc = await nats.connect(
                    server.url,
                    **old_nats_signing.login_options(old_jwt),
                    inbox_prefix=f"_INBOX.{host_a_profile.runtime}".encode(),
                    allow_reconnect=False,
                    disconnected_cb=on_old_disconnect,
                )
                await flush_fully(old_jwt_nc)
                assert old_jwt_nc.is_connected

                # Step 1: team services initiate a connected four-step handover of target_p -> host-b.
                started = await partitions.release_partition(target_p, target_host=host_b_profile.runtime)
                assert started is True
                owner_after_s1 = (await _read_all_owners(services_host, TEAM_A, PARTITIONS))[target_p]
                assert owner_after_s1.state == RELEASED and owner_after_s1.host == host_a_profile.runtime

                # While released, sessions.open for a new agent in target_p is refused partition_not_owned.
                blocked_id = draw_agent_id(TEAM_A, [target_p], prefix="blocked", partition_count=PARTITIONS,
                                           exclude_ids={aid})
                refused = await host_a.host.live.request(
                    "sessions.open",
                    {"request_id": "blocked-during-release", "agent": blocked_id, "agent_type": "Researcher",
                     "definition_sha256": DEFINITION},
                    {"installation": host_a.host.identity.installation, "team": TEAM_A,
                     "target": "team_services", "id": TEAM_A},
                    team=TEAM_A,
                )
                assert refused.raw["body"]["outcome"] == "partition_not_owned"

                # Release the step-2 drain gate now that step-1 RELEASED and refusal assertions passed.
                allow_drain.set()

                # Step 2 + Step 3: host-a drains, hands back, closes session handed_over, rotates NATS key,
                # and authority immediately revokes old_nats_pub, closing old_jwt_nc!
                await asyncio.wait_for(old_disconnected.wait(), timeout=15)
                # $SYS.REQ.CLAIMS.UPDATE drops old_jwt_nc before ACCOUNTZ confirmation completes and
                # before host-a finishes promoting its renewed NATS key/JWT; wait for both to settle.
                await _until(
                    lambda: host_a.host.identity.certificate.nats_public_key != old_nats_pub
                    and host_a.host.connection is not None
                    and host_a.host.connection["jwt"] != old_jwt
                    and authority._ledger.is_nats_revocation_confirmed(old_nats_pub),
                    timeout=15,
                )
                assert any("handed over" in r for r in drain_reasons), drain_reasons
                assert host_a.host.identity.certificate.nats_public_key != old_nats_pub
                assert authority._ledger.is_nats_revocation_confirmed(old_nats_pub) is True
                # Revoked old key cannot reconnect to operator-mode NATS.
                with pytest.raises(Exception):
                    await nats.connect(
                        server.url,
                        **old_nats_signing.login_options(old_jwt),
                        allow_reconnect=False,
                    )
                # Renewed key on host-a connects cleanly!
                assert host_a.host.connection is not None
                new_nats_signing = _signing_key_from_seed(host_a.host.identity.keys.nats_seed)
                new_jwt_nc = await nats.connect(
                    server.url,
                    **new_nats_signing.login_options(host_a.host.connection["jwt"]),
                    inbox_prefix=f"_INBOX.{host_a_profile.runtime}".encode(),
                    allow_reconnect=False,
                )
                await flush_fully(new_jwt_nc)
                assert new_jwt_nc.is_connected

                # Step 4: PartitionService assigns target_p as owned by host-b and moves aid's placement to host-b.
                await _until(lambda: target_p in host_b.usable_partitions(), timeout=20)
                owners_final = await _read_all_owners(services_host, TEAM_A, PARTITIONS)
                assert owners_final[target_p].state == OWNED
                assert owners_final[target_p].host == host_b_profile.runtime
                assert owners_final[target_p].previous_host == host_a_profile.runtime
                moved_placement = await services_host.records.read_report(
                    "placement", f"{TEAM_A}.{aid}", now=services_host.clock()
                )
                assert moved_placement is not None
                assert moved_placement.record.body.host == host_b_profile.runtime

                # host-b's new credential names target_p, and host-b opens a session for the moved agent.
                host_b.attach(_stand_in_agent(aid, "Researcher"), agent_type="Researcher",
                              definition=DEFINITION, nc=None, state=("idle", True))
                await _until(lambda: aid in host_b.agents and host_b.agents[aid].session is not None, timeout=15)
            finally:
                if new_jwt_nc is not None and not new_jwt_nc.is_closed:
                    await new_jwt_nc.close()
                if old_jwt_nc is not None and not old_jwt_nc.is_closed:
                    await old_jwt_nc.close()
                await host_a.stop()
                await host_b.stop()
                stop.set()
                await asyncio.wait_for(part_task, timeout=10)
                await sub.unsubscribe()


@pytest.mark.asyncio
async def test_live_cutoff_and_retired_host_bulk_release_and_lifetime_wait_fallback(tmp_path: Path) -> None:
    """A4, A7, A9, F14 (EVA-03) on real nats-server 2.15.1:

    1. Retired host (authority.retire_host with OperatorModeRevoker): disconnects host-a's active
       operator-mode JWT connection immediately, writes intermediate ``released`` (``host_lost``)
       records for all of host-a's partitions, and reassigns partitions + placed agents to host-b
       as a group (``previous_host``, ``handed_over=True``, old session closed).
    2. External-server fallback (real InstallationAuthority wired with LifetimeWaitRevoker +
       follow_placements): a cut-off host's partitions stay ``released`` until
       ``credential_lifetime_seconds + clock_skew_seconds`` passes, then reassign as a group to
       host-c along with its placed agents.

    Note: Part 1 passes ``is_nats_key_revoked=authority._ledger.is_nats_revocation_confirmed`` as
    an in-process test seam verifying step 2b's operator-mode revocation contract pending step 11's
    cross-process revocation signal.
    """

    import json

    # Part 1: OperatorModeRevoker + SessionService with placed agents + active JWT disconnect.
    nats_dir = tmp_path / "nats-part1"
    nats_dir.mkdir()
    with _operator_nats_server(nats_dir) as server:
        layout = InstanceLayout.at(tmp_path / "instance-part1")
        svc_profile = team_services_profile(TEAM_A)
        host_a_profile = startup_agent_host_profile(TEAM_A, "host-a", "Researcher")
        host_b_profile = startup_agent_host_profile(TEAM_A, "host-b", "Researcher")
        expected = {
            profile.runtime: message_hosts.expected_host(layout, profile)
            for profile in (svc_profile, host_a_profile, host_b_profile)
        }
        message_hosts.prepare_message_host_trust(layout, expected, partitions=PARTITIONS)

        async with AsyncExitStack() as stack:
            sys_key, sys_jwt = server.issue_system_user()
            sys_nc = await nats.connect(server.url, **sys_key.login_options(sys_jwt), allow_reconnect=False)
            stack.push_async_callback(sys_nc.close)

            auth_nc = await _connect_operator_user(stack, server, AUTHORITY_RUNTIME)
            svc_nc = await _connect_operator_user(stack, server, TEAM_SERVICES_RUNTIME)
            host_a_nc = await _connect_operator_user(stack, server, host_a_profile.runtime)
            host_b_nc = await _connect_operator_user(stack, server, host_b_profile.runtime)

            revoker = OperatorModeRevoker(server.operator_signing, server.base_claims, sys_nc)
            authority = InstallationAuthority(
                layout,
                host_keys.load_authority_keys(layout),
                host_keys.load_host_keys(layout, AUTHORITY_RUNTIME),
                partitions=PARTITIONS,
                queues=TEST_QUEUE_LIMITS,
                nats_account_signing_key=server.installation_signing,
                nats_account_public_key=server.installation_account.public_key,
                revoker=revoker,
                credential_lifetime_seconds=300,
            )
            await authority.start(auth_nc, teams=[TEAM_A])

            services_host = MessageHost(layout, svc_profile, svc_nc)
            live = await services_host.start()
            assert services_host.records is not None
            sessions = SessionService(services_host.records, live.trust, live.identity.installation, services_host.clock)
            partitions = PartitionService(
                services_host.records,
                live.trust,
                live.identity.installation,
                TEAM_A,
                sessions._changing,
                services_host.clock,
                credential_lifetime_seconds=300,
                credentials_enforced=True,
                is_nats_key_revoked=authority._ledger.is_nats_revocation_confirmed,
                cut_off_grace_seconds=0.0,
            )
            sessions.ensure_partitions = partitions.ensure_unowned_assigned
            team_svc = TeamServices(sessions)
            sub = await live.serve(
                "svc.team",
                Receiver(live.identity.installation, TEAM_A, "team_services", TEAM_A, TEAM_SERVICES_RUNTIME),
                team_svc.handle,
            )
            await flush_fully(svc_nc)

            host_a = MessageHost(layout, host_a_profile, host_a_nc)
            host_b = MessageHost(layout, host_b_profile, host_b_nc)
            await host_a.start()
            await host_b.start()
            await partitions.reconcile()

            owners_before = await _read_all_owners(services_host, TEAM_A, PARTITIONS)
            assert len(owners_before) == PARTITIONS
            host_a_parts = sorted(p for p, b in owners_before.items() if b.host == host_a_profile.runtime)
            host_b_parts = sorted(p for p, b in owners_before.items() if b.host == host_b_profile.runtime)
            assert len(host_a_parts) == PARTITIONS // 2 and len(host_b_parts) == PARTITIONS // 2

            # Place a real agent on host-a via SessionService so Part 1 verifies group placement handover.
            agent_a_id = draw_agent_id(TEAM_A, [host_a_parts[0]], prefix="reta", partition_count=PARTITIONS)
            assert host_a.live is not None
            open_a = await host_a.live.request(
                "sessions.open",
                {
                    "request_id": "open-retire-a",
                    "agent": agent_a_id,
                    "agent_type": "Researcher",
                    "definition_sha256": DEFINITION,
                },
                {"installation": live.identity.installation, "team": TEAM_A, "target": "team_services", "id": TEAM_A},
                team=TEAM_A,
            )
            assert open_a.raw["body"]["outcome"] == "ok"
            session_a_id = str(open_a.raw["body"]["session"]["body"]["session"])

            # Open an active operator-mode JWT connection for host-a and assert retire_host drops it immediately.
            assert host_a.connection is not None
            old_a_nats_pub = host_a.identity.certificate.nats_public_key
            old_a_signing = _signing_key_from_seed(host_a.identity.keys.nats_seed)
            host_a_disconnected = asyncio.Event()

            async def _on_a_disconnect() -> None:
                host_a_disconnected.set()

            host_a_jwt_nc = await nats.connect(
                server.url,
                **old_a_signing.login_options(host_a.connection["jwt"]),
                inbox_prefix=f"_INBOX.{host_a_profile.runtime}".encode(),
                allow_reconnect=False,
                disconnected_cb=_on_a_disconnect,
            )
            stack.push_async_callback(host_a_jwt_nc.close)
            await flush_fully(host_a_jwt_nc)
            assert host_a_jwt_nc.is_connected

            # Watch UA1_PLACEMENT to prove intermediate RELEASED/host_lost records are written before OWNED.
            seen_released: set[int] = set()
            watch_stop = asyncio.Event()

            async def _watch_releases() -> None:
                assert services_host.records is not None
                async for key, raw in services_host.records.watch(
                    "partition_owner", f"{TEAM_A}.partition.>", team=TEAM_A
                ):
                    if watch_stop.is_set():
                        return
                    if raw:
                        body = json.loads(raw).get("body", {})
                        if body.get("state") == RELEASED and body.get("reason") == "host_lost":
                            seen_released.add(int(body["partition"]))

            watch_task = asyncio.create_task(_watch_releases())
            try:
                await asyncio.sleep(0.05)
                await authority.retire_host(host_a_profile.runtime, reason="decommissioned")
                await asyncio.wait_for(host_a_disconnected.wait(), timeout=5.0)
                assert authority._ledger.is_nats_revocation_confirmed(old_a_nats_pub) is True

                await partitions.reconcile()
                await _until(lambda: set(host_a_parts) <= seen_released, timeout=5.0)
                owners_after_retire = await _read_all_owners(services_host, TEAM_A, PARTITIONS)
                assert all(
                    body.state == OWNED and body.host == host_b_profile.runtime
                    for body in owners_after_retire.values()
                )
                for p in host_a_parts:
                    assert owners_after_retire[p].previous_host == host_a_profile.runtime

                # Placed agent moved as a group to host-b with previous_host, handed_over=True, and closed old session.
                moved_a = await services_host.records.read_report(
                    "placement", f"{TEAM_A}.{agent_a_id}", now=services_host.clock()
                )
                assert moved_a is not None
                assert moved_a.record.body.host == host_b_profile.runtime
                assert moved_a.record.body.previous_host == host_a_profile.runtime
                assert moved_a.record.body.handed_over is True
                closed_a = live.trust.store.agent_session(live.identity.installation, TEAM_A, session_a_id)
                assert closed_a is not None and closed_a.ends_at <= services_host.clock()
            finally:
                watch_stop.set()
                watch_task.cancel()
                await asyncio.gather(watch_task, return_exceptions=True)
                await sub.unsubscribe()

    # Part 2: Real InstallationAuthority wired with LifetimeWaitRevoker + follow_placements,
    # real placed agent on host-b, natural host liveness discovery (no private _seen_live mutation).
    nats_dir2 = tmp_path / "nats-part2"
    nats_dir2.mkdir()
    with _operator_nats_server(nats_dir2) as server2:
        layout2 = InstanceLayout.at(tmp_path / "instance-part2")
        svc2_profile = team_services_profile(TEAM_A)
        host_b2_profile = startup_agent_host_profile(TEAM_A, "host-b", "Researcher")
        host_c2_profile = startup_agent_host_profile(TEAM_A, "host-c", "Researcher")
        expected2 = {
            profile.runtime: message_hosts.expected_host(layout2, profile)
            for profile in (svc2_profile, host_b2_profile, host_c2_profile)
        }
        message_hosts.prepare_message_host_trust(layout2, expected2, partitions=PARTITIONS)
        fake_clock = _Clock(utc_now())

        async with AsyncExitStack() as stack2:
            auth2_nc = await _connect_operator_user(stack2, server2, AUTHORITY_RUNTIME)
            svc2_nc = await _connect_operator_user(stack2, server2, TEAM_SERVICES_RUNTIME)
            host_b2_nc = await _connect_operator_user(stack2, server2, host_b2_profile.runtime)
            host_c2_nc = await _connect_operator_user(stack2, server2, host_c2_profile.runtime)

            wait_revoker = LifetimeWaitRevoker()
            assert wait_revoker.supports_instant_revocation is False
            ext_authority = InstallationAuthority(
                layout2,
                host_keys.load_authority_keys(layout2),
                host_keys.load_host_keys(layout2, AUTHORITY_RUNTIME),
                partitions=PARTITIONS,
                queues=TEST_QUEUE_LIMITS,
                clock=fake_clock,
                nats_account_signing_key=server2.installation_signing,
                nats_account_public_key=server2.installation_account.public_key,
                revoker=wait_revoker,
                credential_lifetime_seconds=60,
            )
            await ext_authority.start(auth2_nc, teams=[TEAM_A])

            follow_stop = asyncio.Event()
            follow_task = asyncio.create_task(ext_authority.follow_placements(follow_stop))

            svc2_host = MessageHost(layout2, svc2_profile, svc2_nc, clock=fake_clock)
            live2 = await svc2_host.start()
            assert svc2_host.records is not None
            sessions2 = SessionService(svc2_host.records, live2.trust, live2.identity.installation, fake_clock)
            ext_partitions = PartitionService(
                svc2_host.records,
                live2.trust,
                live2.identity.installation,
                TEAM_A,
                sessions2._changing,
                fake_clock,
                credential_lifetime_seconds=60,
                credentials_enforced=True,
                is_nats_key_revoked=None,
                cut_off_grace_seconds=0.0,
            )
            sessions2.ensure_partitions = ext_partitions.ensure_unowned_assigned
            sub2 = await live2.serve(
                "svc.team",
                Receiver(live2.identity.installation, TEAM_A, "team_services", TEAM_A, TEAM_SERVICES_RUNTIME),
                TeamServices(sessions2).handle,
            )
            await flush_fully(svc2_nc)

            try:
                host_b2 = MessageHost(layout2, host_b2_profile, host_b2_nc, clock=fake_clock)
                await host_b2.start()
                # Normal reconcile discovers host-b live and assigns all PARTITIONS to it.
                await ext_partitions.reconcile()
                b2_owners = await _read_all_owners(svc2_host, TEAM_A, PARTITIONS, now=fake_clock())
                assert len(b2_owners) == PARTITIONS and all(b.host == host_b2_profile.runtime for b in b2_owners.values())

                # Place a real agent on host-b so Part 2 verifies group placement move after lifetime wait.
                agent_b_id = draw_agent_id(TEAM_A, [0], prefix="cutb", partition_count=PARTITIONS)
                assert host_b2.live is not None
                open_b = await host_b2.live.request(
                    "sessions.open",
                    {
                        "request_id": "open-cutoff-b",
                        "agent": agent_b_id,
                        "agent_type": "Researcher",
                        "definition_sha256": DEFINITION,
                    },
                    {"installation": live2.identity.installation, "team": TEAM_A, "target": "team_services", "id": TEAM_A},
                    team=TEAM_A,
                )
                assert open_b.raw["body"]["outcome"] == "ok"
                session_b_id = str(open_b.raw["body"]["session"]["body"]["session"])

                host_c2 = MessageHost(layout2, host_c2_profile, host_c2_nc, clock=fake_clock)
                await host_c2.start()

                # Cut off host-b: stop its heartbeats and advance clock past its liveness expires_at + skew
                # (respecting C29 forged-delete resistance) while keeping host-c live.
                fake_clock.now = fake_clock.now + timedelta(seconds=120)
                await host_c2.publish_liveness("accepting")

                await ext_partitions.reconcile()
                owners_released = await _read_all_owners(svc2_host, TEAM_A, PARTITIONS, now=fake_clock())
                assert all(
                    body.state == RELEASED and body.host == host_b2_profile.runtime and body.reason == "host_lost"
                    for body in owners_released.values()
                )
                # Let ext_authority.follow_placements process the host_lost release with LifetimeWaitRevoker:
                # key is recorded revoked in ledger, but instant confirmation remains False (external server).
                b2_nats_pub = host_b2.identity.certificate.nats_public_key
                await _until(lambda: ext_authority._ledger.is_nats_key_revoked(b2_nats_pub), timeout=5.0)
                assert ext_authority._ledger.is_nats_revocation_confirmed(b2_nats_pub) is False

                # Before credential_lifetime_seconds + clock_skew_seconds, nothing reassigns.
                fake_clock.now = fake_clock.now + timedelta(seconds=30)
                await host_c2.publish_liveness("accepting")
                await ext_partitions.reconcile()
                owners_still_waiting = await _read_all_owners(svc2_host, TEAM_A, PARTITIONS, now=fake_clock())
                assert all(body.state == RELEASED for body in owners_still_waiting.values())

                # After credential_lifetime_seconds + clock_skew_seconds, all partitions and placed agent move to host-c.
                fake_clock.now = fake_clock.now + timedelta(seconds=60 + CLOCK_SKEW_SECONDS + 5)
                await host_c2.publish_liveness("accepting")
                await ext_partitions.reconcile()
                owners_moved = await _read_all_owners(svc2_host, TEAM_A, PARTITIONS, now=fake_clock())
                assert all(
                    body.state == OWNED and body.host == host_c2_profile.runtime
                    for body in owners_moved.values()
                )
                moved_b = await svc2_host.records.read_report(
                    "placement", f"{TEAM_A}.{agent_b_id}", now=fake_clock()
                )
                assert moved_b is not None
                assert moved_b.record.body.host == host_c2_profile.runtime
                assert moved_b.record.body.previous_host == host_b2_profile.runtime
                assert moved_b.record.body.handed_over is True
                closed_b = live2.trust.store.agent_session(live2.identity.installation, TEAM_A, session_b_id)
                assert closed_b is not None and closed_b.ends_at <= fake_clock()
            finally:
                follow_stop.set()
                follow_task.cancel()
                await asyncio.gather(follow_task, return_exceptions=True)
                await sub2.unsubscribe()


@pytest.mark.asyncio
async def test_live_several_gateways_join_and_connect_with_scoped_credentials(tmp_path: Path) -> None:
    """A6, A9, F14 (EVA-04) on real operator-mode nats-server 2.15.1:

    Several gateway runtimes join one installation serving two teams with strictly disjoint
    (gateway-alpha on TEAM_A vs gateway-beta-only on TEAM_B) and overlapping (gateway-multi on
    TEAM_A + TEAM_B) team/route grants, alongside the default single-gateway profile. Verifies
    scoped JWT permissions, live NATS connections, and end-to-end GatewayMessageHost.command(..., team=TEAM_B).
    """

    import json

    nats_dir = tmp_path / "nats"
    nats_dir.mkdir()
    with _operator_nats_server(nats_dir) as server:
        layout = InstanceLayout.at(tmp_path / "instance")
        default_gw = gateway_profile(TEAM_A)
        alpha_gw = gateway_profile(TEAM_A, runtime="gateway-alpha")
        beta_only_gw = gateway_profile(TEAM_B, runtime="gateway-beta-only")
        multi_gw = gateway_profile((TEAM_A, TEAM_B), runtime="gateway-multi")
        ts_b_profile = team_services_profile(TEAM_B)
        expected = {
            default_gw.runtime: message_hosts.expected_host(layout, default_gw),
            alpha_gw.runtime: message_hosts.expected_host(layout, alpha_gw, edges={TEAM_A: ("web",)}),
            beta_only_gw.runtime: message_hosts.expected_host(layout, beta_only_gw, edges={TEAM_B: ("web_b",)}),
            multi_gw.runtime: message_hosts.expected_host(
                layout, multi_gw, edges={TEAM_A: ("slack",), TEAM_B: ("tg",)}
            ),
            ts_b_profile.runtime: message_hosts.expected_host(layout, ts_b_profile),
        }
        message_hosts.prepare_message_host_trust(layout, expected, partitions=PARTITIONS)

        async with AsyncExitStack() as stack:
            auth_nc = await _connect_operator_user(stack, server, AUTHORITY_RUNTIME)
            authority = InstallationAuthority(
                layout,
                host_keys.load_authority_keys(layout),
                host_keys.load_host_keys(layout, AUTHORITY_RUNTIME),
                partitions=PARTITIONS,
                queues=TEST_QUEUE_LIMITS,
                nats_account_signing_key=server.installation_signing,
                nats_account_public_key=server.installation_account.public_key,
                credential_lifetime_seconds=900,
            )
            await authority.start(auth_nc, teams=[TEAM_A, TEAM_B])

            # Serve TEAM_B's svc.team so gw_multi.command(..., team=TEAM_B) runs end-to-end.
            ts_b_nc = await _connect_operator_user(stack, server, ts_b_profile.runtime)
            ts_b_host = MessageHost(layout, ts_b_profile, ts_b_nc)
            ts_b_live = await ts_b_host.start()
            reply_body = json.loads(
                (Path(__file__).resolve().parents[1] / "contracts/messages/v1/examples/valid/memory.list.reply/last-page.json")
                .read_text(encoding="utf-8")
            )["body"]
            received_b: list[Any] = []

            async def answer_b(verified: Any) -> dict[str, Any]:
                received_b.append(verified)
                return reply_body

            sub_b = await ts_b_live.serve(
                "svc.team",
                Receiver(ts_b_live.identity.installation, TEAM_B, "team_services", TEAM_B, ts_b_profile.runtime),
                answer_b,
            )
            await flush_fully(ts_b_nc)

            # Start all four gateways (default + disjoint alpha & beta-only + overlapping multi).
            gw_default = GatewayMessageHost(layout, TEAM_A)
            gw_alpha = GatewayMessageHost(layout, TEAM_A, runtime_id="gateway-alpha", edges={TEAM_A: ("web",)})
            gw_beta_only = GatewayMessageHost(
                layout, TEAM_B, runtime_id="gateway-beta-only", teams=(TEAM_B,), edges={TEAM_B: ("web_b",)}
            )
            gw_multi = GatewayMessageHost(
                layout, TEAM_A, runtime_id="gateway-multi",
                teams=(TEAM_A, TEAM_B),
                edges={TEAM_A: ("slack",), TEAM_B: ("tg",)},
            )
            try:
                gw_default.start(await _connect_operator_user(stack, server, GATEWAY_RUNTIME))
                gw_alpha.start(await _connect_operator_user(stack, server, "gateway-alpha"))
                gw_beta_only.start(await _connect_operator_user(stack, server, "gateway-beta-only"))
                gw_multi.start(await _connect_operator_user(stack, server, "gateway-multi"))
                await gw_default.ready(timeout=15)
                await gw_alpha.ready(timeout=15)
                await gw_beta_only.ready(timeout=15)
                await gw_multi.ready(timeout=15)

                assert gw_default.host is not None and gw_default.host.connection is not None
                assert gw_alpha.host is not None and gw_alpha.host.connection is not None
                assert gw_beta_only.host is not None and gw_beta_only.host.connection is not None
                assert gw_multi.host is not None and gw_multi.host.connection is not None

                # Inspect issued JWT permissions: alpha sees TEAM_A + web only; beta-only sees TEAM_B + web_b only.
                alpha_claims = decode_jwt_payload(gw_alpha.host.connection["jwt"])["nats"]
                alpha_all = set(alpha_claims["pub"]["allow"]) | set(alpha_claims["sub"]["allow"])
                assert any(TEAM_A in s for s in alpha_all)
                assert not any(TEAM_B in s for s in alpha_all)
                assert any(f"edge~{TEAM_A}~web" in s for s in alpha_all)
                assert not any("slack" in s or "tg" in s or "web_b" in s for s in alpha_all)

                beta_only_claims = decode_jwt_payload(gw_beta_only.host.connection["jwt"])["nats"]
                beta_only_all = set(beta_only_claims["pub"]["allow"]) | set(beta_only_claims["sub"]["allow"])
                assert any(TEAM_B in s for s in beta_only_all)
                assert not any(TEAM_A in s for s in beta_only_all)
                assert any(f"edge~{TEAM_B}~web_b" in s for s in beta_only_all)
                assert not any("tg" in s or "slack" in s for s in beta_only_all)

                # Multi-gateway sees both TEAM_A (slack) and TEAM_B (tg), not web or web_b.
                multi_claims = decode_jwt_payload(gw_multi.host.connection["jwt"])["nats"]
                multi_all = set(multi_claims["pub"]["allow"]) | set(multi_claims["sub"]["allow"])
                assert any(TEAM_A in s for s in multi_all) and any(TEAM_B in s for s in multi_all)
                assert any(f"edge~{TEAM_A}~slack" in s for s in multi_all)
                assert any(f"edge~{TEAM_B}~tg" in s for s in multi_all)
                assert not any(f"edge~{TEAM_A}~web" in s or f"edge~{TEAM_B}~web_b" in s for s in multi_all)

                # Multi-team GatewayMessageHost.command(..., team=TEAM_B) succeeds end-to-end against TEAM_B.
                person_ref = "p-AbCdEfGhIjKlMnOpQrStUv"
                list_body = {"request_id": "cmd-team-b-1", "bank": "usermem", "sort": "memory_id", "offset": 0, "limit": 10}
                reply = await gw_multi.command("memory.list", list_body, person_ref=person_ref, team=TEAM_B, timeout=10)
                assert reply.raw["body"] == reply_body
                assert len(received_b) == 1
                assert received_b[0].frame.issuer.runtime == "gateway-multi"
                assert received_b[0].authority is not None and received_b[0].authority.token.person == person_ref

                # Every gateway's issued user JWT actually connects to operator-mode NATS.
                for gw, runtime in (
                    (gw_default, GATEWAY_RUNTIME),
                    (gw_alpha, "gateway-alpha"),
                    (gw_beta_only, "gateway-beta-only"),
                    (gw_multi, "gateway-multi"),
                ):
                    assert gw.host is not None and gw.host.connection is not None
                    gw_signing = _signing_key_from_seed(gw.host.identity.keys.nats_seed)
                    nc_jwt = await nats.connect(
                        server.url,
                        **gw_signing.login_options(gw.host.connection["jwt"]),
                        inbox_prefix=f"_INBOX.{runtime}".encode(),
                        allow_reconnect=False,
                    )
                    stack.push_async_callback(nc_jwt.close)
                    await flush_fully(nc_jwt)
                    assert nc_jwt.is_connected
                    exp = parse_timestamp(gw.host.connection["expires_at"])
                    assert 840 <= (exp - utc_now()).total_seconds() <= 910
            finally:
                await sub_b.unsubscribe()
                await gw_default.close()
                await gw_alpha.close()
                await gw_beta_only.close()
                await gw_multi.close()
