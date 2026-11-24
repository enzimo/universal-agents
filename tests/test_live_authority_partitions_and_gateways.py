"""Live nats-server 2.15.1 tests for Checkpoint 2: authority partition enforcement,
credential limits, FA-6, CR-1/S1 key lifecycle, operator-mode revocation wiring,
cut-off/retired host revocation, StoredRenewal refresh, and several gateways (A5, A6, A7).
"""

from __future__ import annotations

import asyncio
import json
from contextlib import AsyncExitStack
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

import nats
import pytest

from assistant_gateway.message_host import GATEWAY_RUNTIME, gateway_profile
from custom_messages.wire.encoding import format_timestamp
from custom_messages.wire.kinds import CONTRACT_SHA256, PROTOCOL
from custom_messages.wire.partitions import OWNED, RELEASED, partition_owner_key
from custom_messages.wire.trust import sign_state
from custom_messages.wire.values import parse_timestamp
from custom_transport.protocol1 import host_keys
from custom_transport.protocol1.bootstrap import BootstrapBundle, read_bundle, write_bundle
from custom_transport.protocol1.contract_files import role_permissions
from custom_transport.protocol1.identity import HostIdentity
from custom_transport.protocol1.joining import HostProfile, join
from custom_transport.protocol1.live import LiveHost, RequestRefused, utc_now
from custom_transport.protocol1.nats_jwt import (
    NatsSigningKey,
    decode_jwt_payload,
    encode_user_jwt,
)
from custom_transport.protocol1.trust_state import PersistentTrustStore
from custom_messages.wire.permissions import HostPlacement, roles_permissions
from docker import message_hosts
from installation_authority.expected_hosts import write_expected_hosts
from installation_authority.revocation import OperatorModeRevoker
from installation_authority.service import AUTHORITY_RUNTIME, InstallationAuthority
from runtime_settings.instance_layout import InstanceLayout
from team_services.process import team_services_profile
from tests.live_nats_support import flush_fully
from tests.own_nats_server import own_nats_server
from tests.queue_limits_support import TEST_QUEUE_LIMITS
from tests.test_live_message_contract import nats_server
from tests.test_live_nats_revocation import PARTITIONS, _operator_nats_server

pytestmark = [pytest.mark.integration, pytest.mark.live, pytest.mark.nats]
INSTALLATION = "install_p2"
TEAM_A = "team_a"
TEAM_B = "team_b"
AUTHORITY_AUDIENCE = {
    "installation": INSTALLATION,
    "team": "_install",
    "target": "installation_service",
    "id": "authority",
}


class _Clock:
    def __init__(self, now: datetime) -> None:
        self.now = now

    def __call__(self) -> datetime:
        return self.now


async def _connect_shared(stack: AsyncExitStack, url: str, runtime: str):
    nc = await nats.connect(url, inbox_prefix=f"_INBOX.{runtime}".encode(), allow_reconnect=False)
    stack.push_async_callback(nc.close)
    return nc


async def _write_partition_owner(
    nc: Any,
    signer_identity: HostIdentity,
    *,
    team: str,
    partition: int,
    state: str,
    host: str,
    host_epoch: int,
    reason: str,
    now: datetime,
    sequence: int = 1,
    sign_key: Any = None,
) -> None:
    key = partition_owner_key(team, partition, PARTITIONS)
    cert = signer_identity.certificate
    document = {
        "record": "partition_owner",
        "protocol": PROTOCOL,
        "key": key,
        "writer": {
            "type": "host",
            "installation": cert.installation,
            "runtime": cert.runtime,
            "membership_epoch": cert.membership_epoch,
            "key_id": cert.key_id,
        },
        "sequence": sequence,
        "issued_at": format_timestamp(now),
        "body": {
            "team": team,
            "partition": partition,
            "state": state,
            "host": host,
            "host_epoch": host_epoch,
            "assigned_at": format_timestamp(now),
            "reason": reason,
        },
    }
    signed = sign_state(document, sign_key or signer_identity.keys.application_key)
    bucket = await nc.jetstream().key_value("UA1_PLACEMENT")
    await bucket.put(key, json.dumps(signed).encode())


def _host_trust(layout: InstanceLayout, runtime: str, now: datetime) -> PersistentTrustStore:
    return PersistentTrustStore.open(
        read_bundle(layout.trust_bootstrap_path), layout.host_trust_dir(runtime), now
    )


async def _join_raw(
    nc: Any,
    keys: host_keys.HostKeys,
    trust: PersistentTrustStore,
    profile: HostProfile,
    *,
    clock: Any,
    expected_outcome: str = "ok",
) -> dict[str, Any]:
    """Send a single host.join frame and return the verified reply body (including connection credential)."""

    from custom_messages.wire.frame import Receiver, reply_audience, verify_frame
    from custom_messages.wire.models import Frame
    from custom_transport.protocol1.ids import new_message_id
    from custom_transport.protocol1.joining import sign_join
    from custom_transport.protocol1.live import answer_headers, encode, raise_if_refused
    from custom_transport.protocol1.replay import LiveReplayRecord

    frame = sign_join(
        keys,
        trust.store.installation,
        profile,
        join_id=f"join-{new_message_id()}",
        issued=clock(),
        contract_sha256=trust.store.contract_sha256,
    )
    data, headers = encode(frame)
    msg = await nc.request(frame["subject"], data, timeout=15, headers=headers)
    raise_if_refused("host.join", frame["subject"], msg, __import__("logging").getLogger(__name__))
    req = Frame.model_validate(frame)
    named = reply_audience(req)
    reply = verify_frame(
        msg.data,
        trust=trust.store,
        receiver=Receiver(named["installation"], named["team"], named["target"], named["id"], named["runtime"]),
        arrived_on=msg.subject,
        now=clock(),
        replay=LiveReplayRecord().lookup,
        headers=answer_headers(msg),
        answering=req,
    )
    body = reply.raw["body"]
    assert body["outcome"] == expected_outcome, body
    if body["outcome"] == "ok":
        trust.add(json.dumps(body["certificate"]).encode(), now=clock())
    return body


@pytest.mark.asyncio
async def test_authority_enforces_partition_ownership_revoked_keys_bootstrap_keys_and_fa6(tmp_path: Path) -> None:
    """A5, CR-1, S1, FA-6 on live nats-server 2.15.1 (shared login)."""

    nats_dir = tmp_path / "nats"
    nats_dir.mkdir()
    with own_nats_server(nats_server(), nats_dir, partitions=PARTITIONS) as url:
        layout = InstanceLayout.at(tmp_path / "instance")
        clock = _Clock(utc_now())
        ts_profile = team_services_profile(TEAM_A)
        worker_profile = HostProfile("worker-1", (TEAM_A,), ("worker_host",), ("researcher",), 4)
        no_streams_profile = HostProfile("worker-nostream", (TEAM_B,), ("worker_host",), ("researcher",), 2)
        bootstrap_key = NatsSigningKey.generate("user")

        auth_keys, _ = host_keys.ensure_authority_keys(layout, INSTALLATION)
        host_keys.ensure_host_keys(layout, AUTHORITY_RUNTIME)
        ts_keys, _ = host_keys.ensure_host_keys(layout, ts_profile.runtime)
        w_keys, _ = host_keys.ensure_host_keys(layout, worker_profile.runtime)
        ns_keys, _ = host_keys.ensure_host_keys(layout, no_streams_profile.runtime)

        write_expected_hosts(
            layout.trust_expected_hosts_path,
            {
                ts_profile.runtime: message_hosts.expected_host(layout, ts_profile),
                worker_profile.runtime: message_hosts.expected_host(
                    layout, worker_profile, bootstrap_nats_public_key=bootstrap_key.public_key
                ),
                no_streams_profile.runtime: message_hosts.expected_host(layout, no_streams_profile),
            },
        )
        write_bundle(
            layout.trust_bootstrap_path,
            BootstrapBundle(
                INSTALLATION, ((auth_keys.key_id, auth_keys.public_key_text),), partitions=PARTITIONS
            ),
        )

        async with AsyncExitStack() as stack:
            authority = InstallationAuthority(
                layout,
                auth_keys,
                host_keys.load_host_keys(layout, AUTHORITY_RUNTIME),
                partitions=PARTITIONS,
                queues=TEST_QUEUE_LIMITS,
                clock=clock,
            )
            auth_nc = await _connect_shared(stack, url, AUTHORITY_RUNTIME)
            await authority.start(auth_nc, teams=[TEAM_A])

            # FA-6: team_b has no streams in self._teams -> denied with team_not_allowed.
            ns_nc = await _connect_shared(stack, url, no_streams_profile.runtime)
            ns_reply = await _join_raw(
                ns_nc,
                ns_keys,
                _host_trust(layout, no_streams_profile.runtime, clock()),
                no_streams_profile,
                clock=clock,
                expected_outcome="denied",
            )
            assert ns_reply["reason"] == "team_not_allowed"

            # S1: presenting the bootstrap NATS key in host.join is refused with invalid_body at /body/nats_public_key.
            w_nc = await _connect_shared(stack, url, worker_profile.runtime)
            w_trust = _host_trust(layout, worker_profile.runtime, clock())
            reusing_bootstrap = w_keys.with_nats_seed(
                NatsSigningKey.from_seed(bootstrap_key.seed()).private_key.private_bytes_raw()
            )
            with pytest.raises(RequestRefused) as refused_boot:
                await _join_raw(w_nc, reusing_bootstrap, w_trust, worker_profile, clock=clock)
            assert refused_boot.value.code == "invalid_body" and refused_boot.value.at == "/body/nats_public_key"

            # Normal joins for team_services and worker-1.
            ts_nc = await _connect_shared(stack, url, ts_profile.runtime)
            ts_trust = _host_trust(layout, ts_profile.runtime, clock())
            ts_cert = await join(ts_nc, ts_keys, ts_trust, ts_profile, clock=clock)
            ts_identity = HostIdentity(ts_keys, ts_cert)

            w_cert = await join(w_nc, w_keys, w_trust, worker_profile, clock=clock)
            worker = LiveHost(w_nc, HostIdentity(w_keys, w_cert), w_trust, clock=clock)

            # Write partition_owner records: 0 & 1 owned by worker-1, 2 released from worker-1, 3 forged.
            await _write_partition_owner(
                ts_nc, ts_identity, team=TEAM_A, partition=0, state=OWNED,
                host=worker_profile.runtime, host_epoch=w_cert.membership_epoch, reason="initial", now=clock()
            )
            await _write_partition_owner(
                ts_nc, ts_identity, team=TEAM_A, partition=1, state=OWNED,
                host=worker_profile.runtime, host_epoch=w_cert.membership_epoch, reason="initial", now=clock()
            )
            await _write_partition_owner(
                ts_nc, ts_identity, team=TEAM_A, partition=2, state=RELEASED,
                host=worker_profile.runtime, host_epoch=w_cert.membership_epoch, reason="rebalanced", now=clock()
            )
            from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

            await _write_partition_owner(
                ts_nc, ts_identity, team=TEAM_A, partition=3, state=OWNED,
                host=worker_profile.runtime, host_epoch=w_cert.membership_epoch, reason="initial", now=clock(),
                sign_key=Ed25519PrivateKey.generate(),
            )

            def _renew_body(renew_id: str, **extra: Any) -> dict[str, Any]:
                c = worker.identity.certificate
                return {
                    "renew_id": renew_id,
                    "certificate_id": c.certificate_id,
                    "membership_epoch": c.membership_epoch,
                    "profile_sha256": worker_profile.digest(),
                    "contract_sha256": CONTRACT_SHA256,
                    **extra,
                }

            # Unowned, released, forged, and out-of-range partitions are all refused at /body/partitions.
            for idx, bad_parts in enumerate(([5], [2], [3], [PARTITIONS])):
                with pytest.raises(RequestRefused) as bad_p:
                    await worker.request(
                        "host.renew", _renew_body(f"bad-part-{idx}", partitions=bad_parts),
                        AUTHORITY_AUDIENCE, team="_install"
                    )
                assert bad_p.value.code == "invalid_body" and bad_p.value.at == "/body/partitions"

            # Because partition 2 is released for worker-1, renewing without new_nats_public_key is refused.
            with pytest.raises(RequestRefused) as no_new_key:
                await worker.request(
                    "host.renew", _renew_body("no-new-key", partitions=[0, 1]),
                    AUTHORITY_AUDIENCE, team="_install"
                )
            assert no_new_key.value.code == "invalid_body" and no_new_key.value.at == "/body/new_nats_public_key"

            # Supplying bootstrap key as new_nats_public_key is refused at /body/new_nats_public_key.
            with pytest.raises(RequestRefused) as boot_renew:
                await worker.request(
                    "host.renew",
                    _renew_body("boot-new-key", partitions=[0, 1], new_nats_public_key=bootstrap_key.public_key),
                    AUTHORITY_AUDIENCE,
                    team="_install",
                )
            assert boot_renew.value.code == "invalid_body" and boot_renew.value.at == "/body/new_nats_public_key"

            # Supplying a fresh new_nats_public_key succeeds and bans the old NATS key permanently (CR-1).
            old_nats_key = w_cert.nats_public_key
            new_nats_key_2 = NatsSigningKey.generate("user")
            ok_reply = await worker.request(
                "host.renew",
                _renew_body("step3-ok", partitions=[0, 1], new_nats_public_key=new_nats_key_2.public_key),
                AUTHORITY_AUDIENCE,
                team="_install",
            )
            assert ok_reply.raw["body"]["outcome"] == "ok"
            cert2_doc = ok_reply.raw["body"]["certificate"]
            assert cert2_doc["body"]["nats_public_key"] == new_nats_key_2.public_key
            w_trust.add(json.dumps(cert2_doc).encode(), now=clock())
            cert2 = w_trust.store.host_certificate(INSTALLATION, worker_profile.runtime, 2)
            assert cert2 is not None
            worker.identity = worker.identity.with_certificate(cert2)

            # Restart authority: permanent ban on old_nats_key and bootstrap_key persists in IssueLedger.
            await auth_nc.close()
            authority2 = InstallationAuthority(
                layout,
                auth_keys,
                host_keys.load_host_keys(layout, AUTHORITY_RUNTIME),
                partitions=PARTITIONS,
                queues=TEST_QUEUE_LIMITS,
                clock=clock,
            )
            await authority2.start(await _connect_shared(stack, url, AUTHORITY_RUNTIME), teams=[TEAM_A])

            with pytest.raises(RequestRefused) as reuse_old_join:
                await _join_raw(w_nc, w_keys, w_trust, worker_profile, clock=clock)
            assert reuse_old_join.value.code == "invalid_body" and reuse_old_join.value.at == "/body/nats_public_key"

            with pytest.raises(RequestRefused) as reuse_old_renew:
                await worker.request(
                    "host.renew",
                    _renew_body("reuse-old", partitions=[0], new_nats_public_key=old_nats_key),
                    AUTHORITY_AUDIENCE,
                    team="_install",
                )
            assert reuse_old_renew.value.code == "invalid_body" and reuse_old_renew.value.at == "/body/new_nats_public_key"


@pytest.mark.asyncio
async def test_authority_enforces_credential_size_limits_on_join_and_renewal(tmp_path: Path) -> None:
    """A5: oversized credentials are denied with credential_too_large on join and not_allowed on renewal."""

    nats_dir = tmp_path / "nats"
    nats_dir.mkdir()
    with own_nats_server(nats_server(), nats_dir, partitions=96) as url:
        layout = InstanceLayout.at(tmp_path / "instance")
        clock = _Clock(utc_now())
        long_team = "team_" + "a" * 19  # 24 chars
        # 180 long pools exceed max_credential_bytes (65,536) even with 0 partitions on join.
        huge_pools = tuple(f"pool_{i:03d}_" + "p" * 15 for i in range(180))
        # 100 long pools fit on join (0 partitions, ~49 KB), but exceed 65,536 once 96 partitions are added on renewal.
        borderline_pools = tuple(f"pool_{i:03d}_" + "p" * 15 for i in range(100))

        ts_profile = team_services_profile(long_team)
        huge_worker = HostProfile("worker-huge", (long_team,), ("worker_host",), huge_pools, 4)
        border_worker = HostProfile("worker-border", (long_team,), ("worker_host",), borderline_pools, 4)

        auth_keys, _ = host_keys.ensure_authority_keys(layout, INSTALLATION)
        host_keys.ensure_host_keys(layout, AUTHORITY_RUNTIME)
        ts_keys, _ = host_keys.ensure_host_keys(layout, ts_profile.runtime)
        huge_keys, _ = host_keys.ensure_host_keys(layout, huge_worker.runtime)
        border_keys, _ = host_keys.ensure_host_keys(layout, border_worker.runtime)

        write_expected_hosts(
            layout.trust_expected_hosts_path,
            {
                ts_profile.runtime: message_hosts.expected_host(layout, ts_profile),
                huge_worker.runtime: message_hosts.expected_host(layout, huge_worker),
                border_worker.runtime: message_hosts.expected_host(layout, border_worker),
            },
        )
        write_bundle(
            layout.trust_bootstrap_path,
            BootstrapBundle(INSTALLATION, ((auth_keys.key_id, auth_keys.public_key_text),), partitions=96),
        )

        async with AsyncExitStack() as stack:
            authority = InstallationAuthority(
                layout,
                auth_keys,
                host_keys.load_host_keys(layout, AUTHORITY_RUNTIME),
                partitions=96,
                queues=TEST_QUEUE_LIMITS,
                clock=clock,
            )
            await authority.start(await _connect_shared(stack, url, AUTHORITY_RUNTIME), teams=[long_team])

            # Huge worker is denied on join with reason=credential_too_large.
            huge_nc = await _connect_shared(stack, url, huge_worker.runtime)
            denied = await _join_raw(
                huge_nc,
                huge_keys,
                _host_trust(layout, huge_worker.runtime, clock()),
                huge_worker,
                clock=clock,
                expected_outcome="denied",
            )
            assert denied["reason"] == "credential_too_large"
            assert "max_credential_bytes" in denied["message"]

            # Borderline worker joins cleanly (0 partitions on join).
            ts_nc = await _connect_shared(stack, url, ts_profile.runtime)
            ts_cert = await join(ts_nc, ts_keys, _host_trust(layout, ts_profile.runtime, clock()), ts_profile, clock=clock)
            ts_identity = HostIdentity(ts_keys, ts_cert)

            border_nc = await _connect_shared(stack, url, border_worker.runtime)
            border_trust = _host_trust(layout, border_worker.runtime, clock())
            border_cert = await join(border_nc, border_keys, border_trust, border_worker, clock=clock)
            border_live = LiveHost(border_nc, HostIdentity(border_keys, border_cert), border_trust, clock=clock)

            # Assign all 96 partitions to border_worker in UA1_PLACEMENT.
            bucket = await ts_nc.jetstream().key_value("UA1_PLACEMENT")
            for p in range(96):
                key = partition_owner_key(long_team, p, 96)
                doc = sign_state(
                    {
                        "record": "partition_owner",
                        "protocol": PROTOCOL,
                        "key": key,
                        "writer": {
                            "type": "host",
                            "installation": ts_cert.installation,
                            "runtime": ts_cert.runtime,
                            "membership_epoch": ts_cert.membership_epoch,
                            "key_id": ts_cert.key_id,
                        },
                        "sequence": 1,
                        "issued_at": format_timestamp(clock()),
                        "body": {
                            "team": long_team,
                            "partition": p,
                            "state": OWNED,
                            "host": border_worker.runtime,
                            "host_epoch": border_cert.membership_epoch,
                            "assigned_at": format_timestamp(clock()),
                            "reason": "initial",
                        },
                    },
                    ts_identity.keys.application_key,
                )
                await bucket.put(key, json.dumps(doc).encode())

            # Renewing with all 96 partitions exceeds 65,536 -> not_allowed naming max_credential_bytes.
            too_big = await border_live.request(
                "host.renew",
                {
                    "renew_id": "renew-too-big",
                    "certificate_id": border_cert.certificate_id,
                    "membership_epoch": border_cert.membership_epoch,
                    "profile_sha256": border_worker.digest(),
                    "contract_sha256": CONTRACT_SHA256,
                    "partitions": list(range(96)),
                },
                AUTHORITY_AUDIENCE,
                team="_install",
            )
            assert too_big.raw["body"]["outcome"] == "not_allowed"
            assert "max_credential_bytes" in too_big.raw["body"]["message"]

            # Renewing with just 2 partitions succeeds.
            fits = await border_live.request(
                "host.renew",
                {
                    "renew_id": "renew-fits",
                    "certificate_id": border_cert.certificate_id,
                    "membership_epoch": border_cert.membership_epoch,
                    "profile_sha256": border_worker.digest(),
                    "contract_sha256": CONTRACT_SHA256,
                    "partitions": [0, 1],
                },
                AUTHORITY_AUDIENCE,
                team="_install",
            )
            assert fits.raw["body"]["outcome"] == "ok"


@pytest.mark.asyncio
async def test_operator_mode_authority_full_revocation_lifecycle_and_stored_renewal_refresh(tmp_path: Path) -> None:
    """A7, C01, C03, C08 on real operator-mode nats-server 2.15.1:
    - issues real NATS user JWTs ending at credential_lifetime_seconds;
    - flushes join/renew replies BEFORE revoking bootstrap/old NATS keys;
    - confirms revocation via ACCOUNTZ;
    - refreshes expired StoredRenewal JWTs on retry;
    - watches host_lost released records to revoke cut-off hosts;
    - retires a host revoking both its certificate and its NATS key."""

    nats_dir = tmp_path / "nats"
    nats_dir.mkdir()
    with _operator_nats_server(nats_dir) as server:
        layout = InstanceLayout.at(tmp_path / "instance")
        clock = _Clock(utc_now())

        ts_profile = team_services_profile(TEAM_A)
        w1_profile = HostProfile("worker-1", (TEAM_A,), ("worker_host",), ("researcher",), 4)
        w2_profile = HostProfile("worker-2", (TEAM_A,), ("worker_host",), ("researcher",), 4)
        w3_profile = HostProfile("worker-3", (TEAM_A,), ("worker_host",), ("researcher",), 4)

        auth_keys, _ = host_keys.ensure_authority_keys(layout, INSTALLATION)
        host_keys.ensure_host_keys(layout, AUTHORITY_RUNTIME)
        ts_keys, _ = host_keys.ensure_host_keys(layout, ts_profile.runtime)
        w1_keys, _ = host_keys.ensure_host_keys(layout, w1_profile.runtime)
        w2_keys, _ = host_keys.ensure_host_keys(layout, w2_profile.runtime)
        w3_keys, _ = host_keys.ensure_host_keys(layout, w3_profile.runtime)

        # Bootstrap user for worker-1's join (pre-join permissions on _INBOX.worker-1.join.>).
        boot_key = NatsSigningKey.generate("user")
        boot_perms = roles_permissions(
            [role_permissions("joining_host")],
            HostPlacement(w1_profile.runtime, (), installation=INSTALLATION),
            partition_count=PARTITIONS,
        )
        boot_jwt = encode_user_jwt(
            server.installation_signing,
            server.installation_account.public_key,
            boot_key.public_key,
            f"{w1_profile.runtime}-bootstrap",
            boot_perms,
            issued_at=int(clock().timestamp()),
            expires_at=int(clock().timestamp()) + 300,
        )

        write_expected_hosts(
            layout.trust_expected_hosts_path,
            {
                ts_profile.runtime: message_hosts.expected_host(layout, ts_profile),
                w1_profile.runtime: message_hosts.expected_host(
                    layout, w1_profile, bootstrap_nats_public_key=boot_key.public_key
                ),
                w2_profile.runtime: message_hosts.expected_host(layout, w2_profile),
                w3_profile.runtime: message_hosts.expected_host(layout, w3_profile),
            },
        )
        write_bundle(
            layout.trust_bootstrap_path,
            BootstrapBundle(
                INSTALLATION, ((auth_keys.key_id, auth_keys.public_key_text),), partitions=PARTITIONS
            ),
        )

        async with AsyncExitStack() as stack:
            sys_key, sys_jwt = server.issue_system_user()
            sys_nc = await nats.connect(server.url, **sys_key.login_options(sys_jwt), allow_reconnect=False)
            stack.push_async_callback(sys_nc.close)

            # Authority connects with installation_authority permissions on _INBOX.authority.
            auth_nats_key, auth_nats_jwt = server.issue_host_user(AUTHORITY_RUNTIME)
            auth_nc = await nats.connect(
                server.url,
                **auth_nats_key.login_options(auth_nats_jwt),
                inbox_prefix=f"_INBOX.{AUTHORITY_RUNTIME}".encode(),
                allow_reconnect=False,
            )
            stack.push_async_callback(auth_nc.close)

            revoker = OperatorModeRevoker(server.operator_signing, server.base_claims, sys_nc)
            authority = InstallationAuthority(
                layout,
                auth_keys,
                host_keys.load_host_keys(layout, AUTHORITY_RUNTIME),
                partitions=PARTITIONS,
                queues=TEST_QUEUE_LIMITS,
                clock=clock,
                credential_lifetime_seconds=300,
                nats_account_public_key=server.installation_account.public_key,
                nats_account_signing_key=server.installation_signing,
                revoker=revoker,
            )
            await authority.start(auth_nc, teams=[TEAM_A])
            stop_follow = asyncio.Event()
            follow_task = asyncio.create_task(authority.follow_placements(stop_follow))

            try:
                # 1. Worker-1 joins over its bootstrap NATS user connection; reply arrives BEFORE bootstrap key is revoked!
                boot_disconnected = asyncio.Event()

                async def on_boot_disc() -> None:
                    boot_disconnected.set()

                boot_nc = await nats.connect(
                    server.url,
                    **boot_key.login_options(boot_jwt),
                    inbox_prefix=f"_INBOX.{w1_profile.runtime}.join".encode(),
                    allow_reconnect=False,
                    disconnected_cb=on_boot_disc,
                )
                stack.push_async_callback(boot_nc.close)
                w1_trust = _host_trust(layout, w1_profile.runtime, clock())
                join_reply = await _join_raw(boot_nc, w1_keys, w1_trust, w1_profile, clock=clock)
                assert "connection" in join_reply and join_reply["connection"]["type"] == "nats_user_jwt"
                conn1 = join_reply["connection"]
                assert parse_timestamp(conn1["expires_at"]) <= clock() + timedelta(seconds=300)
                await asyncio.wait_for(boot_disconnected.wait(), timeout=3.0)
                assert authority.live is not None
                await asyncio.wait_for(authority.live.drain(), timeout=3.0)
                assert authority.is_nats_key_revocation_confirmed(boot_key.public_key) is True
                with pytest.raises(Exception, match="Authorization Violation"):
                    await nats.connect(server.url, **boot_key.login_options(boot_jwt), allow_reconnect=False)

                # 2. Worker-1 connects using its issued NATS user JWT!
                w1_user_key = NatsSigningKey.from_private_key(
                    "user",
                    __import__(
                        "cryptography.hazmat.primitives.asymmetric.ed25519", fromlist=["Ed25519PrivateKey"]
                    ).Ed25519PrivateKey.from_private_bytes(w1_keys.nats_seed),
                )
                w1_disconnected = asyncio.Event()

                async def on_w1_disc() -> None:
                    w1_disconnected.set()

                w1_nc = await nats.connect(
                    server.url,
                    **w1_user_key.login_options(conn1["jwt"]),
                    inbox_prefix=f"_INBOX.{w1_profile.runtime}".encode(),
                    allow_reconnect=False,
                    disconnected_cb=on_w1_disc,
                )
                stack.push_async_callback(w1_nc.close)
                await flush_fully(w1_nc)

                # Join team_services so it can write verified partition_owner records.
                ts_uk, ts_uj = server.issue_host_user(ts_profile.runtime)
                ts_nc = await nats.connect(
                    server.url,
                    **ts_uk.login_options(ts_uj),
                    inbox_prefix=f"_INBOX.{ts_profile.runtime}".encode(),
                    allow_reconnect=False,
                )
                stack.push_async_callback(ts_nc.close)
                ts_trust = _host_trust(layout, ts_profile.runtime, clock())
                ts_cert = await join(ts_nc, ts_keys, ts_trust, ts_profile, clock=clock)
                ts_identity = HostIdentity(ts_keys, ts_cert)

                w1_cert = w1_trust.store.host_certificate(INSTALLATION, w1_profile.runtime, 1)
                assert w1_cert is not None
                w1_live = LiveHost(w1_nc, HostIdentity(w1_keys, w1_cert), w1_trust, clock=clock)

                # Assign partition 0 to worker-1; worker-1 renews with partitions=[0] and gets partition 0 in its JWT.
                await _write_partition_owner(
                    ts_nc, ts_identity, team=TEAM_A, partition=0, state=OWNED,
                    host=w1_profile.runtime, host_epoch=1, reason="initial", now=clock()
                )
                r_part0 = await w1_live.request(
                    "host.renew",
                    {
                        "renew_id": "renew-part0",
                        "certificate_id": w1_cert.certificate_id,
                        "membership_epoch": 1,
                        "profile_sha256": w1_profile.digest(),
                        "contract_sha256": CONTRACT_SHA256,
                        "partitions": [0],
                    },
                    AUTHORITY_AUDIENCE,
                    team="_install",
                )
                part0_body = r_part0.raw["body"]
                assert part0_body["outcome"] == "ok" and "connection" in part0_body
                part0_jwt_payload = decode_jwt_payload(part0_body["connection"]["jwt"])
                assert f"ua.v1.{TEAM_A}.*.ctl.agent.*.0" in part0_jwt_payload["nats"]["sub"]["allow"]
                w1_trust.add(json.dumps(part0_body["certificate"]).encode(), now=clock())
                w1_cert2 = w1_trust.store.host_certificate(INSTALLATION, w1_profile.runtime, 2)
                assert w1_cert2 is not None

                # C03: retrying renew-part0 after the cached JWT expired mints a fresh JWT for cert2 & partition 0.
                clock.now = clock.now + timedelta(seconds=280)
                r_part0_retry = await w1_live.request(
                    "host.renew",
                    {
                        "renew_id": "renew-part0",
                        "certificate_id": w1_cert.certificate_id,
                        "membership_epoch": 1,
                        "profile_sha256": w1_profile.digest(),
                        "contract_sha256": CONTRACT_SHA256,
                        "partitions": [0],
                    },
                    AUTHORITY_AUDIENCE,
                    team="_install",
                )
                retry_body = r_part0_retry.raw["body"]
                assert retry_body["outcome"] == "ok"
                assert retry_body["certificate"] == part0_body["certificate"]
                assert retry_body["connection"]["jwt"] != part0_body["connection"]["jwt"]
                assert parse_timestamp(retry_body["connection"]["expires_at"]) > parse_timestamp(
                    part0_body["connection"]["expires_at"]
                )

                # Reset clock back to wall clock so live nats-server exp matches.
                clock.now = utc_now()
                w1_live.identity = w1_live.identity.with_certificate(w1_cert2)

                # 3. Handover step 3: release partition 0, worker-1 renews with new_nats_public_key and partitions=[].
                await _write_partition_owner(
                    ts_nc, ts_identity, team=TEAM_A, partition=0, state=RELEASED,
                    host=w1_profile.runtime, host_epoch=2, reason="rebalanced", now=clock(), sequence=2
                )
                w1_new_nats = NatsSigningKey.generate("user")
                step3_reply = await w1_live.request(
                    "host.renew",
                    {
                        "renew_id": "renew-step3",
                        "certificate_id": w1_cert2.certificate_id,
                        "membership_epoch": 2,
                        "profile_sha256": w1_profile.digest(),
                        "contract_sha256": CONTRACT_SHA256,
                        "partitions": [],
                        "new_nats_public_key": w1_new_nats.public_key,
                    },
                    AUTHORITY_AUDIENCE,
                    team="_install",
                )
                step3_body = step3_reply.raw["body"]
                assert step3_body["outcome"] == "ok" and "connection" in step3_body
                # Reply arrived BEFORE disconnect, and now old connection closes & revocation is confirmed!
                await asyncio.wait_for(w1_disconnected.wait(), timeout=3.0)
                await asyncio.wait_for(authority.live.drain(), timeout=3.0)
                assert authority.is_nats_key_revocation_confirmed(w1_user_key.public_key) is True
                with pytest.raises(Exception, match="Authorization Violation"):
                    await nats.connect(server.url, **w1_user_key.login_options(conn1["jwt"]), allow_reconnect=False)
                # New NATS key connects cleanly with its new JWT (without partition 0)!
                new_jwt = step3_body["connection"]["jwt"]
                assert f"ua.v1.{TEAM_A}.*.ctl.agent.*.0" not in decode_jwt_payload(new_jwt)["nats"]["sub"]["allow"]
                w1_nc2 = await nats.connect(server.url, **w1_new_nats.login_options(new_jwt), allow_reconnect=False)
                await flush_fully(w1_nc2)
                await w1_nc2.close()

                # 4. Cut-off host (host_lost): follow_placements watches released record & revokes worker-2's NATS key.
                # Join worker-2 over a temporary installation-account connection, then connect as worker-2's own JWT.
                w2_tmp_k, w2_tmp_j = server.issue_host_user("w2-bootstrap")
                w2_tmp_nc = await nats.connect(
                    server.url,
                    **w2_tmp_k.login_options(w2_tmp_j),
                    inbox_prefix=f"_INBOX.{w2_profile.runtime}".encode(),
                    allow_reconnect=False,
                )
                stack.push_async_callback(w2_tmp_nc.close)
                w2_reply = await _join_raw(
                    w2_tmp_nc, w2_keys, _host_trust(layout, w2_profile.runtime, clock()), w2_profile, clock=clock
                )
                w2_user_key = NatsSigningKey.from_private_key(
                    "user",
                    __import__(
                        "cryptography.hazmat.primitives.asymmetric.ed25519", fromlist=["Ed25519PrivateKey"]
                    ).Ed25519PrivateKey.from_private_bytes(w2_keys.nats_seed),
                )
                w2_disconnected = asyncio.Event()

                async def on_w2_disc() -> None:
                    w2_disconnected.set()

                w2_nc = await nats.connect(
                    server.url,
                    **w2_user_key.login_options(w2_reply["connection"]["jwt"]),
                    allow_reconnect=False,
                    disconnected_cb=on_w2_disc,
                )
                stack.push_async_callback(w2_nc.close)
                await flush_fully(w2_nc)

                await _write_partition_owner(
                    ts_nc, ts_identity, team=TEAM_A, partition=4, state=RELEASED,
                    host=w2_profile.runtime, host_epoch=1, reason="host_lost", now=clock(), sequence=1
                )
                await asyncio.wait_for(w2_disconnected.wait(), timeout=5.0)
                for _ in range(50):
                    if authority.is_nats_key_revocation_confirmed(w2_user_key.public_key):
                        break
                    await asyncio.sleep(0.05)
                assert authority.is_nats_key_revocation_confirmed(w2_user_key.public_key) is True

                # 5. retire_host revokes both certificate in UA1_CERTS and NATS key on the server.
                w3_tmp_k, w3_tmp_j = server.issue_host_user("w3-bootstrap")
                w3_tmp_nc = await nats.connect(
                    server.url,
                    **w3_tmp_k.login_options(w3_tmp_j),
                    inbox_prefix=f"_INBOX.{w3_profile.runtime}".encode(),
                    allow_reconnect=False,
                )
                stack.push_async_callback(w3_tmp_nc.close)
                w3_reply = await _join_raw(
                    w3_tmp_nc, w3_keys, _host_trust(layout, w3_profile.runtime, clock()), w3_profile, clock=clock
                )
                w3_user_key = NatsSigningKey.from_private_key(
                    "user",
                    __import__(
                        "cryptography.hazmat.primitives.asymmetric.ed25519", fromlist=["Ed25519PrivateKey"]
                    ).Ed25519PrivateKey.from_private_bytes(w3_keys.nats_seed),
                )
                w3_disconnected = asyncio.Event()

                async def on_w3_disc() -> None:
                    w3_disconnected.set()

                w3_nc = await nats.connect(
                    server.url,
                    **w3_user_key.login_options(w3_reply["connection"]["jwt"]),
                    allow_reconnect=False,
                    disconnected_cb=on_w3_disc,
                )
                stack.push_async_callback(w3_nc.close)
                await flush_fully(w3_nc)

                outcomes = await authority.retire_host(w3_profile.runtime, reason="decommissioned")
                assert len(outcomes) == 1 and outcomes[0].revoked is True
                await asyncio.wait_for(w3_disconnected.wait(), timeout=3.0)
                assert authority.is_nats_key_revocation_confirmed(w3_user_key.public_key) is True
                assert any(e["runtime"] == w3_profile.runtime for e in authority._revocations["body"]["entries"])
            finally:
                stop_follow.set()
                await asyncio.gather(follow_task, return_exceptions=True)


@pytest.mark.asyncio
async def test_several_gateways_join_with_scoped_team_and_route_credentials(tmp_path: Path) -> None:
    """Decision 102 / A6 / C07 on live operator-mode nats-server 2.15.1:
    Two distinct gateways plus the default single gateway join with distinct team and route grants,
    and nats-server enforces that each gateway cannot touch teams outside its own grants."""

    nats_dir = tmp_path / "nats"
    nats_dir.mkdir()
    with _operator_nats_server(nats_dir) as server:
        layout = InstanceLayout.at(tmp_path / "instance")
        clock = _Clock(utc_now())

        gw_default = gateway_profile(TEAM_A)
        gw_alpha = gateway_profile(TEAM_A, runtime="gateway-alpha")
        gw_beta = gateway_profile((TEAM_A, TEAM_B), runtime="gateway-beta")

        auth_keys, _ = host_keys.ensure_authority_keys(layout, INSTALLATION)
        host_keys.ensure_host_keys(layout, AUTHORITY_RUNTIME)
        default_keys, _ = host_keys.ensure_host_keys(layout, gw_default.runtime)
        alpha_keys, _ = host_keys.ensure_host_keys(layout, gw_alpha.runtime)
        beta_keys, _ = host_keys.ensure_host_keys(layout, gw_beta.runtime)

        write_expected_hosts(
            layout.trust_expected_hosts_path,
            {
                GATEWAY_RUNTIME: message_hosts.expected_host(
                    layout, gw_default, edges={TEAM_A: ("web_default",)}
                ),
                gw_alpha.runtime: message_hosts.expected_host(
                    layout, gw_alpha, edges={TEAM_A: ("web_alpha",)}
                ),
                gw_beta.runtime: message_hosts.expected_host(
                    layout, gw_beta, edges={TEAM_A: ("tg_beta",), TEAM_B: ("web_beta",)}
                ),
            },
        )
        write_bundle(
            layout.trust_bootstrap_path,
            BootstrapBundle(
                INSTALLATION, ((auth_keys.key_id, auth_keys.public_key_text),), partitions=PARTITIONS
            ),
        )

        async with AsyncExitStack() as stack:
            auth_nk, auth_nj = server.issue_host_user(AUTHORITY_RUNTIME)
            auth_nc = await nats.connect(
                server.url,
                **auth_nk.login_options(auth_nj),
                inbox_prefix=f"_INBOX.{AUTHORITY_RUNTIME}".encode(),
                allow_reconnect=False,
            )
            stack.push_async_callback(auth_nc.close)

            authority = InstallationAuthority(
                layout,
                auth_keys,
                host_keys.load_host_keys(layout, AUTHORITY_RUNTIME),
                partitions=PARTITIONS,
                queues=TEST_QUEUE_LIMITS,
                clock=clock,
                credential_lifetime_seconds=300,
                nats_account_public_key=server.installation_account.public_key,
                nats_account_signing_key=server.installation_signing,
            )
            await authority.start(auth_nc, teams=[TEAM_A, TEAM_B])

            replies: dict[str, dict[str, Any]] = {}
            for profile, keys in (
                (gw_default, default_keys),
                (gw_alpha, alpha_keys),
                (gw_beta, beta_keys),
            ):
                tmp_k, tmp_j = server.issue_host_user(f"join-{profile.runtime}")
                nc = await nats.connect(
                    server.url,
                    **tmp_k.login_options(tmp_j),
                    inbox_prefix=f"_INBOX.{profile.runtime}".encode(),
                    allow_reconnect=False,
                )
                stack.push_async_callback(nc.close)
                replies[profile.runtime] = await _join_raw(
                    nc, keys, _host_trust(layout, profile.runtime, clock()), profile, clock=clock
                )

            alpha_jwt = replies[gw_alpha.runtime]["connection"]["jwt"]
            beta_jwt = replies[gw_beta.runtime]["connection"]["jwt"]
            default_jwt = replies[GATEWAY_RUNTIME]["connection"]["jwt"]

            alpha_pub = set(decode_jwt_payload(alpha_jwt)["nats"]["pub"]["allow"])
            beta_pub = set(decode_jwt_payload(beta_jwt)["nats"]["pub"]["allow"])
            default_pub = set(decode_jwt_payload(default_jwt)["nats"]["pub"]["allow"])

            # Each gateway gets only its own teams and edges!
            assert any(TEAM_A in s for s in alpha_pub) and not any(TEAM_B in s for s in alpha_pub)
            assert any("web_alpha" in s for s in alpha_pub) and not any("tg_beta" in s for s in alpha_pub)

            assert any(TEAM_A in s for s in beta_pub) and any(TEAM_B in s for s in beta_pub)
            assert any("tg_beta" in s for s in beta_pub) and any("web_beta" in s for s in beta_pub)
            assert not any("web_alpha" in s for s in beta_pub)

            assert any("web_default" in s for s in default_pub) and not any(TEAM_B in s for s in default_pub)

            # Live NATS enforcement: gateway-alpha connects with its issued JWT and is denied publishing to team_b.
            alpha_user_key = NatsSigningKey.from_private_key(
                "user",
                __import__(
                    "cryptography.hazmat.primitives.asymmetric.ed25519", fromlist=["Ed25519PrivateKey"]
                ).Ed25519PrivateKey.from_private_bytes(alpha_keys.nats_seed),
            )
            errors: list[Exception] = []

            async def on_error(exc: Exception) -> None:
                errors.append(exc)

            alpha_nc = await nats.connect(
                server.url,
                **alpha_user_key.login_options(alpha_jwt),
                inbox_prefix=f"_INBOX.{gw_alpha.runtime}".encode(),
                allow_reconnect=False,
                error_cb=on_error,
            )
            stack.push_async_callback(alpha_nc.close)
            await alpha_nc.publish(f"ua.v1.{TEAM_B}.{gw_alpha.runtime}.svc.team", b"forbidden")
            await flush_fully(alpha_nc)
            await asyncio.sleep(0.1)
            assert any("permissions violation" in str(err).lower() for err in errors), errors


@pytest.mark.asyncio
async def test_c11_c12_c13_c16_live_recovery_pruning_default_retire_and_host_start(tmp_path: Path) -> None:
    """Live operator-mode nats-server 2.15.1 verification for C11, C12, C13, and C16:
    - C11: Cut-off host with two host_lost partitions recovers to K2; second host_lost event and watch
      replay do NOT revoke K2.
    - C12 + C13: Multi-epoch shared NATS key uses max expires_at on default retire_host(runtime)
      (reason='decommissioned', max key_expires_at) and survives prune_before past epoch 1's expiry;
      longer-lived bootstrap key with bootstrap_nats_expires_at survives prune_before past
      credential_lifetime_seconds.
    - C16: MessageHost.start() recovers from a revoked stored nats_key both by promoting a staged
      pending_nats_key and by generating a fresh NATS user key when none is staged.
    """

    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
    from custom_transport.protocol1.host_runtime import MessageHost
    from custom_transport.protocol1.state_records import StateRecords
    from installation_authority.placements import ReleasedPartitionWatcher

    nats_dir = tmp_path / "nats"
    nats_dir.mkdir()
    with _operator_nats_server(nats_dir) as server:
        layout = InstanceLayout.at(tmp_path / "instance")
        clock = _Clock(utc_now())

        ts_profile = HostProfile("team-services", (TEAM_A,), ("team_services",), (), 0)
        w1_profile = HostProfile("worker-recover", (TEAM_A,), ("worker_host",), ("helper",), 4)
        w2_profile = HostProfile("worker-shared-key", (TEAM_A,), ("worker_host",), ("helper",), 4)
        w3_profile = HostProfile("worker-long-boot", (TEAM_A,), ("worker_host",), ("helper",), 4)
        w4_profile = HostProfile("worker-crash-pending", (TEAM_A,), ("worker_host",), ("helper",), 4)
        w5_profile = HostProfile("worker-offline-revoked", (TEAM_A,), ("worker_host",), ("helper",), 4)

        auth_keys, _ = host_keys.ensure_authority_keys(layout, INSTALLATION)
        host_keys.ensure_host_keys(layout, AUTHORITY_RUNTIME)
        ts_keys, _ = host_keys.ensure_host_keys(layout, ts_profile.runtime)
        w1_keys, _ = host_keys.ensure_host_keys(layout, w1_profile.runtime)
        w2_keys, _ = host_keys.ensure_host_keys(layout, w2_profile.runtime)
        w3_keys, _ = host_keys.ensure_host_keys(layout, w3_profile.runtime)
        w4_keys, _ = host_keys.ensure_host_keys(layout, w4_profile.runtime)
        w5_keys, _ = host_keys.ensure_host_keys(layout, w5_profile.runtime)

        long_boot_key = NatsSigningKey.generate("user")
        long_boot_exp = clock() + timedelta(seconds=3600)
        long_boot_perms = roles_permissions(
            [role_permissions("joining_host")],
            HostPlacement(w3_profile.runtime, (), installation=INSTALLATION),
            partition_count=PARTITIONS,
        )
        long_boot_jwt = encode_user_jwt(
            server.installation_signing,
            server.installation_account.public_key,
            long_boot_key.public_key,
            f"{w3_profile.runtime}-bootstrap",
            long_boot_perms,
            issued_at=int(clock().timestamp()),
            expires_at=int(long_boot_exp.timestamp()),
        )

        write_expected_hosts(
            layout.trust_expected_hosts_path,
            {
                ts_profile.runtime: message_hosts.expected_host(layout, ts_profile),
                w1_profile.runtime: message_hosts.expected_host(layout, w1_profile),
                w2_profile.runtime: message_hosts.expected_host(layout, w2_profile),
                w3_profile.runtime: message_hosts.expected_host(
                    layout,
                    w3_profile,
                    bootstrap_nats_public_key=long_boot_key.public_key,
                    bootstrap_nats_expires_at=long_boot_exp,
                ),
                w4_profile.runtime: message_hosts.expected_host(layout, w4_profile),
                w5_profile.runtime: message_hosts.expected_host(layout, w5_profile),
            },
        )
        write_bundle(
            layout.trust_bootstrap_path,
            BootstrapBundle(
                INSTALLATION, ((auth_keys.key_id, auth_keys.public_key_text),), partitions=PARTITIONS
            ),
        )

        async with AsyncExitStack() as stack:
            sys_key, sys_jwt = server.issue_system_user()
            sys_nc = await nats.connect(server.url, **sys_key.login_options(sys_jwt), allow_reconnect=False)
            stack.push_async_callback(sys_nc.close)

            auth_nk, auth_nj = server.issue_host_user(AUTHORITY_RUNTIME)
            auth_nc = await nats.connect(
                server.url,
                **auth_nk.login_options(auth_nj),
                inbox_prefix=f"_INBOX.{AUTHORITY_RUNTIME}".encode(),
                allow_reconnect=False,
            )
            stack.push_async_callback(auth_nc.close)

            revoker = OperatorModeRevoker(server.operator_signing, server.base_claims, sys_nc)
            authority = InstallationAuthority(
                layout,
                auth_keys,
                host_keys.load_host_keys(layout, AUTHORITY_RUNTIME),
                partitions=PARTITIONS,
                queues=TEST_QUEUE_LIMITS,
                clock=clock,
                credential_lifetime_seconds=300,
                nats_account_public_key=server.installation_account.public_key,
                nats_account_signing_key=server.installation_signing,
                revoker=revoker,
            )
            await authority.start(auth_nc, teams=[TEAM_A])
            stop_follow = asyncio.Event()
            follow_task = asyncio.create_task(authority.follow_placements(stop_follow))

            try:
                # Join team_services so it can write verified partition_owner records.
                ts_uk, ts_uj = server.issue_host_user(ts_profile.runtime)
                ts_nc = await nats.connect(
                    server.url,
                    **ts_uk.login_options(ts_uj),
                    inbox_prefix=f"_INBOX.{ts_profile.runtime}".encode(),
                    allow_reconnect=False,
                )
                stack.push_async_callback(ts_nc.close)
                ts_trust = _host_trust(layout, ts_profile.runtime, clock())
                ts_cert = await join(ts_nc, ts_keys, ts_trust, ts_profile, clock=clock)
                ts_identity = HostIdentity(ts_keys, ts_cert)

                # --- C11: cut-off host with two partitions recovers to K2; second host_lost & replay spare K2 ---
                w1_join_k, w1_join_j = server.issue_host_user("w1-join")
                w1_join_nc = await nats.connect(
                    server.url,
                    **w1_join_k.login_options(w1_join_j),
                    inbox_prefix=f"_INBOX.{w1_profile.runtime}".encode(),
                    allow_reconnect=False,
                )
                stack.push_async_callback(w1_join_nc.close)
                w1_trust = _host_trust(layout, w1_profile.runtime, clock())
                w1_r1 = await _join_raw(w1_join_nc, w1_keys, w1_trust, w1_profile, clock=clock)
                k1_signer = NatsSigningKey.from_private_key(
                    "user", Ed25519PrivateKey.from_private_bytes(w1_keys.nats_seed)
                )
                k1_disconnected = asyncio.Event()

                async def on_k1_disc() -> None:
                    k1_disconnected.set()

                k1_nc = await nats.connect(
                    server.url,
                    **k1_signer.login_options(w1_r1["connection"]["jwt"]),
                    allow_reconnect=False,
                    disconnected_cb=on_k1_disc,
                )
                stack.push_async_callback(k1_nc.close)
                await flush_fully(k1_nc)

                # Release partition 0 as host_lost at host_epoch=1 -> revokes K1.
                await _write_partition_owner(
                    ts_nc, ts_identity, team=TEAM_A, partition=0, state=RELEASED,
                    host=w1_profile.runtime, host_epoch=1, reason="host_lost", now=clock(), sequence=1
                )
                await asyncio.wait_for(k1_disconnected.wait(), timeout=5.0)
                for _ in range(50):
                    if authority.is_nats_key_revocation_confirmed(k1_signer.public_key):
                        break
                    await asyncio.sleep(0.05)
                assert authority.is_nats_key_revocation_confirmed(k1_signer.public_key)

                # Worker-1 recovers with a fresh NATS key K2 -> epoch 2 with partitions=().
                w1_keys_k2 = host_keys.rotate_nats_key(layout, w1_profile.runtime, prefer_pending=False)
                w1_r2 = await _join_raw(w1_join_nc, w1_keys_k2, w1_trust, w1_profile, clock=clock)
                assert int(w1_r2["certificate"]["body"]["membership_epoch"]) == 2
                k2_signer = NatsSigningKey.from_private_key(
                    "user", Ed25519PrivateKey.from_private_bytes(w1_keys_k2.nats_seed)
                )
                k2_disconnected = asyncio.Event()

                async def on_k2_disc() -> None:
                    k2_disconnected.set()

                k2_nc = await nats.connect(
                    server.url,
                    **k2_signer.login_options(w1_r2["connection"]["jwt"]),
                    allow_reconnect=False,
                    disconnected_cb=on_k2_disc,
                )
                stack.push_async_callback(k2_nc.close)
                await flush_fully(k2_nc)

                # Now partition 1's host_lost record arrives with host_epoch=1!
                await _write_partition_owner(
                    ts_nc, ts_identity, team=TEAM_A, partition=1, state=RELEASED,
                    host=w1_profile.runtime, host_epoch=1, reason="host_lost", now=clock(), sequence=1
                )
                await asyncio.sleep(0.3)

                # Also simulate a fresh watcher replaying partition 0's still-released record after restart/reconnect.
                assert authority._trust is not None and authority.live is not None
                replay_watcher = ReleasedPartitionWatcher(
                    StateRecords(authority._js, lambda: authority.live.identity, authority._trust),
                    verify=lambda raw: authority._trust.store.verify(raw, record="partition_owner", now=clock()),
                    on_host_lost=lambda rel: authority.revoke_cut_off_host_nats_key(
                        rel.host, host_epoch=rel.host_epoch, partition=rel.partition, team=rel.team, released_at=rel.issued_at
                    ),
                    teams=[TEAM_A],
                )
                bucket = await auth_nc.jetstream().key_value("UA1_PLACEMENT")
                p0_entry = await bucket.get(f"{TEAM_A}.partition.0")
                await replay_watcher._handle_record(TEAM_A, f"{TEAM_A}.partition.0", p0_entry.value)
                await flush_fully(k2_nc)
                assert not k2_disconnected.is_set()
                assert not authority._ledger.is_nats_key_revoked(k2_signer.public_key)

                # --- C16: MessageHost.start() recovers when stored nats_key is revoked ---
                # Case 1: pending_nats_key staged before crash, old nats_key already revoked in ledger.
                w4_staged = host_keys.stage_pending_nats_key(layout, w4_profile.runtime)
                assert w4_staged.pending_nats_public_key is not None
                await authority._revoke_nats_key(
                    w4_keys.nats_public_key, revoke_until=int(clock().timestamp()) + 600, now=clock()
                )
                w4_nc_k, w4_nc_j = server.issue_host_user("w4-start")
                w4_nc = await nats.connect(
                    server.url,
                    **w4_nc_k.login_options(w4_nc_j),
                    inbox_prefix=f"_INBOX.{w4_profile.runtime}".encode(),
                    allow_reconnect=False,
                )
                stack.push_async_callback(w4_nc.close)
                w4_host = MessageHost(layout=layout, profile=w4_profile, nc=w4_nc, clock=clock)
                w4_live = await w4_host.start(report=False)
                assert w4_live.identity.certificate.nats_public_key == w4_staged.pending_nats_public_key
                assert w4_live.identity.keys.key_id == w4_keys.key_id
                assert host_keys.load_host_keys(layout, w4_profile.runtime).pending_nats_public_key is None

                # Case 2: no pending_nats_key staged, stored nats_key was revoked while offline.
                await authority._revoke_nats_key(
                    w5_keys.nats_public_key, revoke_until=int(clock().timestamp()) + 600, now=clock()
                )
                w5_nc_k, w5_nc_j = server.issue_host_user("w5-start")
                w5_nc = await nats.connect(
                    server.url,
                    **w5_nc_k.login_options(w5_nc_j),
                    inbox_prefix=f"_INBOX.{w5_profile.runtime}".encode(),
                    allow_reconnect=False,
                )
                stack.push_async_callback(w5_nc.close)
                w5_host = MessageHost(layout=layout, profile=w5_profile, nc=w5_nc, clock=clock)
                w5_live = await w5_host.start(report=False)
                assert w5_live.identity.certificate.nats_public_key != w5_keys.nats_public_key
                assert w5_live.identity.keys.key_id == w5_keys.key_id
                assert host_keys.load_host_keys(layout, w5_profile.runtime).nats_public_key == (
                    w5_live.identity.certificate.nats_public_key
                )

                # --- C12 + C13: multi-epoch shared key & longer-lived bootstrap key survive prune_before ---
                # Worker-3 joins over long_boot_key (expires +3600s > credential_lifetime_seconds=300s).
                w3_boot_disc = asyncio.Event()

                async def on_w3_boot_disc() -> None:
                    w3_boot_disc.set()

                w3_boot_nc = await nats.connect(
                    server.url,
                    **long_boot_key.login_options(long_boot_jwt),
                    inbox_prefix=f"_INBOX.{w3_profile.runtime}.join".encode(),
                    allow_reconnect=False,
                    disconnected_cb=on_w3_boot_disc,
                )
                stack.push_async_callback(w3_boot_nc.close)
                await _join_raw(w3_boot_nc, w3_keys, _host_trust(layout, w3_profile.runtime, clock()), w3_profile, clock=clock)
                await asyncio.wait_for(w3_boot_disc.wait(), timeout=3.0)
                await asyncio.wait_for(authority.live.drain(), timeout=3.0)
                assert authority.is_nats_key_revocation_confirmed(long_boot_key.public_key)

                # Worker-2 joins at epoch 1 with K_w2, then renews at epoch 2 keeping K_w2 (later expires_at).
                w2_join_k, w2_join_j = server.issue_host_user("w2-join")
                w2_join_nc = await nats.connect(
                    server.url,
                    **w2_join_k.login_options(w2_join_j),
                    inbox_prefix=f"_INBOX.{w2_profile.runtime}".encode(),
                    allow_reconnect=False,
                )
                stack.push_async_callback(w2_join_nc.close)
                w2_trust = _host_trust(layout, w2_profile.runtime, clock())
                await _join_raw(w2_join_nc, w2_keys, w2_trust, w2_profile, clock=clock)
                w2_cert1 = w2_trust.store.host_certificate(INSTALLATION, w2_profile.runtime, 1)
                assert w2_cert1 is not None
                w2_live = LiveHost(w2_join_nc, HostIdentity(w2_keys, w2_cert1), w2_trust, clock=clock)

                # Advance authority clock by 500s (> credential_lifetime_seconds + 2*CLOCK_SKEW_SECONDS) and renew w2 keeping K_w2.
                clock.now = clock.now + timedelta(seconds=500)
                w2_r2 = await w2_live.request(
                    "host.renew",
                    {
                        "renew_id": "renew-w2-same-key",
                        "certificate_id": w2_cert1.certificate_id,
                        "membership_epoch": 1,
                        "profile_sha256": w2_profile.digest(),
                        "contract_sha256": CONTRACT_SHA256,
                    },
                    AUTHORITY_AUDIENCE,
                    team="_install",
                )
                assert w2_r2.raw["body"]["outcome"] == "ok"
                w2_cert2_exp = parse_timestamp(w2_r2.raw["body"]["certificate"]["body"]["expires_at"])
                w2_cert2_jwt = w2_r2.raw["body"]["connection"]["jwt"]

                # C13: retire_host(runtime) with DEFAULT reason='decommissioned' succeeds and revokes w2's key through cert2's expires_at.
                retire_outcomes = await authority.retire_host(w2_profile.runtime)
                assert len(retire_outcomes) == 1 and retire_outcomes[0].revoked is True
                assert retire_outcomes[0].revoked_at == int(w2_cert2_exp.timestamp())
                w2_entries = [e for e in authority._revocations["body"]["entries"] if e["runtime"] == w2_profile.runtime]
                assert len(w2_entries) == 2
                assert all(e["reason"] == "decommissioned" for e in w2_entries)
                assert all(e["key_expires_at"] == w2_r2.raw["body"]["certificate"]["body"]["expires_at"] for e in w2_entries)

                # Because clock is now +500s (> 300 + 2*CLOCK_SKEW), prune_before ran during retire_host;
                # long_boot_key (expires +3600s) must STILL be revoked on nats-server!
                assert long_boot_key.public_key in revoker.applied_revocations
                with pytest.raises(Exception, match="Authorization Violation"):
                    await nats.connect(server.url, **long_boot_key.login_options(long_boot_jwt), allow_reconnect=False)

                # Trigger another pruning revocation at cert1.expires_at + 120s (< cert2.expires_at):
                # w2's shared NATS key must STILL remain in revoker.applied_revocations and blocked on nats-server!
                dummy_key = NatsSigningKey.generate("user").public_key
                prune_time = w2_cert1.expires_at + timedelta(seconds=120)
                await authority._revoke_nats_key(dummy_key, revoke_until=int(prune_time.timestamp()) + 300, now=prune_time)
                assert w2_keys.nats_public_key in revoker.applied_revocations
                w2_user_key = NatsSigningKey.from_private_key(
                    "user", Ed25519PrivateKey.from_private_bytes(w2_keys.nats_seed)
                )
                with pytest.raises(Exception, match="Authorization Violation"):
                    await nats.connect(server.url, **w2_user_key.login_options(w2_cert2_jwt), allow_reconnect=False)
            finally:
                stop_follow.set()
                await asyncio.gather(follow_task, return_exceptions=True)
