"""Deterministic unit tests for team services partition ownership, CR-2 lock, and four-step handovers (A1, A2, A4)."""

from __future__ import annotations

import asyncio
import json
import secrets
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import Any, AsyncIterator, Mapping, Optional

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat
from nats.js.errors import KeyWrongLastSequenceError

from custom_messages.contracts.runtime_v1 import MANAGER_AGENT_TYPE
from custom_messages.wire.encoding import b64url, format_timestamp
from custom_messages.wire.kinds import CLOCK_SKEW_SECONDS, CONTRACT_SHA256, PROTOCOL
from custom_messages.wire.models import SessionsOpenReplyAdapter
from custom_messages.wire.partitions import OWNED, RELEASED, agent_partition, partition_owner_key
from custom_messages.wire.refusals import FrameRefused
from custom_messages.wire.trust import sign_state
from custom_transport.protocol1.bootstrap import BootstrapBundle
from custom_transport.protocol1.host_keys import HostKeys
from custom_transport.protocol1.identity import HostIdentity
from custom_transport.protocol1.state_records import StateRecords, StoredRecord, require_record_key
from custom_transport.protocol1.trust_state import PersistentTrustStore
from team_services.partitions import (
    PARTITION_CREDENTIAL_TOO_LARGE_CODE,
    PARTITION_OWNER,
    PartitionService,
)
from team_services.sessions import PLACEMENT, SessionService, placement_key

INSTALLATION = "install_part_test"
TEAM = "team_alpha"
PARTITIONS = 8
DEFINITION_SHA = "DQ0NDQ0NDQ0NDQ0NDQ0NDQ0NDQ0NDQ0NDQ0NDQ0NDQ0"


@dataclass
class _Entry:
    revision: int
    value: bytes


class _MemoryStateRecords:
    """In-memory signed state record store with real CAS revisions and real TrustStore verification."""

    def __init__(self, identity_fn: Any, trust: PersistentTrustStore) -> None:
        self._identity = identity_fn
        self._trust = trust
        self._buckets: dict[str, dict[str, _Entry]] = {}
        self._rev = 0
        self.before_put_hook: Optional[Any] = None

    def _bucket_name(self, record: str) -> str:
        return StateRecords._bucket_name(record, team=TEAM)

    def _bucket(self, record: str) -> dict[str, _Entry]:
        return self._buckets.setdefault(self._bucket_name(record), {})

    def writer(self) -> HostIdentity:
        return self._identity()

    def signed(
        self,
        record: str,
        key: str,
        body: Mapping[str, Any],
        *,
        sequence: int,
        issued_at: datetime,
        expires_at: Optional[datetime] = None,
    ) -> dict[str, Any]:
        identity = self._identity()
        cert = identity.certificate
        doc: dict[str, Any] = {
            "record": record,
            "protocol": PROTOCOL,
            "key": key,
            "writer": {
                "type": "host",
                "installation": cert.installation,
                "runtime": cert.runtime,
                "membership_epoch": cert.membership_epoch,
                "key_id": identity.keys.key_id,
            },
            "sequence": sequence,
            "issued_at": format_timestamp(issued_at),
            "body": dict(body),
        }
        if expires_at is not None:
            doc["expires_at"] = format_timestamp(expires_at)
        return sign_state(doc, identity.keys.application_key)

    async def put(
        self,
        record: str,
        key: str,
        document: Mapping[str, Any],
        *,
        last_revision: Optional[int] = None,
    ) -> int:
        if self.before_put_hook is not None:
            await self.before_put_hook(record, key, document)
        bucket = self._bucket(record)
        existing = bucket.get(key)
        current_rev = 0 if existing is None else existing.revision
        if last_revision is not None and current_rev != last_revision:
            raise KeyWrongLastSequenceError(f"expected revision {last_revision}, got {current_rev}")
        self._rev += 1
        data = json.dumps(document, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
        bucket[key] = _Entry(self._rev, data)
        return self._rev

    async def delete(self, record: str, key: str, *, last_revision: int) -> bool:
        bucket = self._bucket(record)
        existing = bucket.get(key)
        if existing is None or existing.revision != last_revision:
            return False
        del bucket[key]
        return True

    async def raw(self, record: str, key: str) -> Optional[tuple[int, bytes]]:
        entry = self._bucket(record).get(key)
        return None if entry is None else (entry.revision, entry.value)

    async def revision(self, record: str, key: str) -> int:
        entry = self._bucket(record).get(key)
        return 0 if entry is None else entry.revision

    async def keys(self, record: str, prefix: str, *, team: Optional[str] = None) -> list[str]:
        return sorted(k for k in self._bucket(record) if k.startswith(prefix))

    async def read_report(self, record: str, key: str, *, now: datetime, held: Optional[tuple[int, str]] = None) -> Optional[StoredRecord]:
        entry = self._bucket(record).get(key)
        if entry is None:
            return None
        verified = self._trust.store.verify(entry.value, record=record, now=now, held=held)
        require_record_key(verified, key)
        return StoredRecord(entry.revision, verified)

    async def take_trust_record(self, record: str, key: str, *, now: datetime) -> bool:
        entry = self._bucket(record).get(key)
        if entry is None:
            return False
        doc = json.loads(entry.value)
        if doc.get("key") != key:
            raise FrameRefused("invalid_body", "stored under another key", at="/key")
        return self._trust.add(entry.value, now=now)

    async def watch(self, record: str, keys: str = ">", *, team: Optional[str] = None,
                    caught_up: Optional[asyncio.Event] = None) -> AsyncIterator[tuple[str, Optional[bytes]]]:
        if caught_up is not None:
            caught_up.set()
        if False:
            yield ("", None)


class _ClusterFixture:
    """Real cryptographic cluster fixture (authority, team_services, and host identities) over in-memory KV."""

    def __init__(self, tmp_path: Path, *, partitions: int = PARTITIONS) -> None:
        self.now = datetime(2026, 11, 24, 10, 0, tzinfo=timezone.utc)
        self.partitions = partitions
        self.auth_priv = Ed25519PrivateKey.generate()
        self.auth_pub = b64url(self.auth_priv.public_key().public_bytes(Encoding.Raw, PublicFormat.Raw))
        self.auth_key_id = "auth-key-1"
        bundle = BootstrapBundle(INSTALLATION, ((self.auth_key_id, self.auth_pub),), partitions=partitions)
        self.trust = PersistentTrustStore.open(bundle, tmp_path / "trust", self.now)
        self.host_keys: dict[str, HostKeys] = {}
        self.host_epochs: dict[str, int] = {}
        self.host_certs: dict[str, dict[str, Any]] = {}
        self.liveness_seq: dict[str, int] = {}
        self.revocation_seq = 0
        self.revocation_epoch = 0
        self.revoked_entries: list[dict[str, Any]] = []

        self.ts_identity: Optional[HostIdentity] = None
        self.records = _MemoryStateRecords(lambda: self.ts_identity, self.trust)
        self._publish_revocations()
        self.ts_identity = self.issue_host(
            "team-services",
            roles=("team_services",),
            agent_types=(),
            session_limit=0,
        )

    def clock(self) -> datetime:
        return self.now

    def advance(self, seconds: float) -> datetime:
        self.now += timedelta(seconds=seconds)
        return self.now

    def _publish_revocations(self) -> None:
        self.revocation_seq += 1
        self.revocation_epoch += 1
        key = f"revocations.{INSTALLATION}"
        doc = sign_state(
            {
                "record": "revocations",
                "protocol": PROTOCOL,
                "key": key,
                "writer": {"type": "authority", "installation": INSTALLATION, "key_id": self.auth_key_id},
                "sequence": self.revocation_seq,
                "issued_at": format_timestamp(self.now),
                "expires_at": format_timestamp(self.now + timedelta(hours=24)),
                "body": {
                    "installation": INSTALLATION,
                    "revocation_epoch": self.revocation_epoch,
                    "expires_at": format_timestamp(self.now + timedelta(hours=24)),
                    "entries": list(self.revoked_entries),
                },
            },
            self.auth_priv,
        )
        raw = json.dumps(doc).encode()
        self.trust.add(raw, now=self.now)
        self.records._bucket("revocations")[key] = _Entry(self.revocation_seq, raw)

    def revoke_host(self, runtime: str) -> None:
        cert_doc = self.host_certs[runtime]
        body = cert_doc["body"]
        self.revoked_entries.append(
            {
                "certificate_id": body["certificate_id"],
                "runtime": runtime,
                "key_id": body["application_key"]["key_id"],
                "membership_epoch": body["membership_epoch"],
                "revoked_at": format_timestamp(self.now),
                "key_expires_at": body["expires_at"],
                "reason": "decommissioned",
            }
        )
        self._publish_revocations()

    def issue_host(
        self,
        runtime: str,
        *,
        roles: tuple[str, ...] = ("worker_host",),
        agent_types: tuple[str, ...] = ("researcher",),
        session_limit: int = 8,
        connection_class: str = "connected",
        new_nats_key: bool = False,
        cert_lifetime_days: int = 30,
    ) -> HostIdentity:
        if runtime not in self.host_keys:
            app_priv = Ed25519PrivateKey.generate()
            self.host_keys[runtime] = HostKeys(
                runtime=runtime,
                key_id=f"key-{runtime}",
                application_key=app_priv,
                nats_seed=secrets.token_bytes(32),
            )
        elif new_nats_key:
            old = self.host_keys[runtime]
            self.host_keys[runtime] = HostKeys(
                runtime=runtime,
                key_id=old.key_id,
                application_key=old.application_key,
                nats_seed=secrets.token_bytes(32),
            )
        keys = self.host_keys[runtime]
        epoch = self.host_epochs.get(runtime, 0) + 1
        self.host_epochs[runtime] = epoch
        prev = self.host_certs.get(runtime)
        cert_id = f"cert.{runtime}.{epoch}"
        expires = self.now + timedelta(days=cert_lifetime_days)
        body: dict[str, Any] = {
            "certificate_id": cert_id,
            "installation": INSTALLATION,
            "runtime": runtime,
            "membership_epoch": epoch,
            "revocation_epoch": self.revocation_epoch,
            "teams": [TEAM],
            "roles": list(roles),
            "agent_types": list(agent_types),
            "session_limit": session_limit,
            "connection_class": connection_class,
            "protocol": PROTOCOL,
            "deployment": "local",
            "application_key": {
                "key_id": keys.key_id,
                "public_key": keys.public_key_text,
            },
            "nats_public_key": keys.nats_public_key,
            "profile_sha256": DEFINITION_SHA,
            "issued_at": format_timestamp(self.now),
            "expires_at": format_timestamp(expires),
        }
        if prev is not None:
            body["replaces"] = prev["body"]["certificate_id"]
        key = f"host.{INSTALLATION}.{runtime}.{epoch}"
        doc = sign_state(
            {
                "record": "certificate",
                "protocol": PROTOCOL,
                "key": key,
                "writer": {"type": "authority", "installation": INSTALLATION, "key_id": self.auth_key_id},
                "sequence": epoch,
                "issued_at": format_timestamp(self.now),
                "expires_at": format_timestamp(expires),
                "body": body,
            },
            self.auth_priv,
        )
        raw = json.dumps(doc).encode()
        self.trust.add(raw, now=self.now)
        self.records._bucket("certificate")[key] = _Entry(epoch, raw)
        self.host_certs[runtime] = doc
        cert = self.trust.store.host_certificate(INSTALLATION, runtime, epoch)
        assert cert is not None
        ident = HostIdentity(keys, cert)
        if runtime == "team-services":
            self.ts_identity = ident
        return ident

    def write_liveness(
        self,
        runtime: str,
        *,
        state: str = "accepting",
        components: list[dict[str, str]] = (),  # type: ignore[assignment]
        contract_sha256: str = CONTRACT_SHA256,
        ttl_seconds: int = 30,
    ) -> None:
        epoch = self.host_epochs[runtime]
        cert = self.trust.store.host_certificate(INSTALLATION, runtime, epoch)
        assert cert is not None
        held = self.trust.store.held_record("certificate", f"host.{INSTALLATION}.{runtime}.{epoch}")
        assert held is not None
        keys = self.host_keys[runtime]
        seq = self.liveness_seq.get(runtime, 0) + 1
        self.liveness_seq[runtime] = seq
        body: dict[str, Any] = {
            "runtime": runtime,
            "certificate": {
                "certificate_id": cert.certificate_id,
                "membership_epoch": cert.membership_epoch,
                "sha256": held[1],
            },
            "session_limit": cert.session_limit,
            "sessions_active": 0,
            "protocol": PROTOCOL,
            "contract_sha256": contract_sha256,
            "state": state,
            "health": "healthy" if not components else "degraded",
            "components": list(components),
            "placement_labels": [],
            "outbox_depth": 0,
            "revocation_epoch_seen": cert.revocation_epoch,
            "started_at": format_timestamp(self.now),
            "heartbeat_at": format_timestamp(self.now),
        }
        doc = sign_state(
            {
                "record": "host",
                "protocol": PROTOCOL,
                "key": runtime,
                "writer": {
                    "type": "host",
                    "installation": INSTALLATION,
                    "runtime": runtime,
                    "membership_epoch": cert.membership_epoch,
                    "key_id": keys.key_id,
                },
                "sequence": seq,
                "issued_at": format_timestamp(self.now),
                "expires_at": format_timestamp(self.now + timedelta(seconds=ttl_seconds)),
                "body": body,
            },
            keys.application_key,
        )
        self.records._rev += 1
        self.records._bucket("host")[runtime] = _Entry(self.records._rev, json.dumps(doc).encode())

    def remove_liveness(self, runtime: str) -> None:
        self.records._bucket("host").pop(runtime, None)

    def make_services(
        self,
        *,
        credentials_enforced: bool = False,
        credential_lifetime_seconds: int = 3600,
        handover_seconds: int = 600,
        is_nats_key_revoked: Any = None,
        cut_off_grace_seconds: float = 30.0,
        max_credential_bytes: Optional[int] = None,
    ) -> tuple[SessionService, PartitionService]:
        sessions = SessionService(self.records, self.trust, INSTALLATION, self.clock)  # type: ignore[arg-type]
        partitions = PartitionService(
            self.records,  # type: ignore[arg-type]
            self.trust,
            INSTALLATION,
            TEAM,
            sessions._changing,
            self.clock,
            credentials_enforced=credentials_enforced,
            credential_lifetime_seconds=credential_lifetime_seconds,
            handover_seconds=handover_seconds,
            is_nats_key_revoked=is_nats_key_revoked,
            cut_off_grace_seconds=cut_off_grace_seconds,
            max_credential_bytes=max_credential_bytes,
        )
        return sessions, partitions

    def open_frame(self, runtime: str, agent: str, agent_type: str = "researcher") -> Any:
        cert = self.trust.store.host_certificate(INSTALLATION, runtime, self.host_epochs[runtime])
        assert cert is not None
        return SimpleNamespace(
            certificate=cert,
            frame=SimpleNamespace(kind="sessions.open", issuer=SimpleNamespace(team=TEAM)),
            raw={"body": {"agent": agent, "agent_type": agent_type, "definition_sha256": DEFINITION_SHA}},
        )

    def close_frame(self, runtime: str, session_id: str, reason: str = "handed_over") -> Any:
        cert = self.trust.store.host_certificate(INSTALLATION, runtime, self.host_epochs[runtime])
        assert cert is not None
        return SimpleNamespace(
            certificate=cert,
            frame=SimpleNamespace(kind="sessions.close", issuer=SimpleNamespace(team=TEAM)),
            raw={"body": {"session": session_id, "reason": reason}},
        )

    def agent_in_partition(self, target_partition: int, prefix: str = "ag") -> str:
        for idx in range(5000):
            candidate = f"{prefix}-{idx}"
            if agent_partition(TEAM, candidate, self.partitions) == target_partition:
                return candidate
        raise AssertionError(f"could not find agent id in partition {target_partition}")

    async def owners(self) -> dict[int, tuple[str, str]]:
        """Map partition -> (state, host) for verified records in UA1_PLACEMENT."""

        result: dict[int, tuple[str, str]] = {}
        for p in range(self.partitions):
            key = partition_owner_key(TEAM, p, self.partitions)
            stored = await self.records.read_report(PARTITION_OWNER, key, now=self.now)
            if stored is not None:
                result[p] = (str(stored.record.body.state), str(stored.record.body.host))
        return result


@pytest.mark.asyncio
async def test_balanced_initial_assignment_ignores_non_owning_roles_and_persists_across_renewals(tmp_path: Path) -> None:
    """A1: assigns 0..N-1 balanced across live lead/worker hosts, ignores gateways, persists across renewals."""

    cluster = _ClusterFixture(tmp_path)
    cluster.issue_host("lead-host", roles=("lead_host", "worker_host"), agent_types=(MANAGER_AGENT_TYPE, "researcher"))
    cluster.issue_host("worker-1", roles=("worker_host",), agent_types=("researcher",))
    cluster.issue_host("gateway", roles=("gateway",), agent_types=(), session_limit=0)
    cluster.write_liveness("lead-host")
    cluster.write_liveness("worker-1")
    cluster.write_liveness("gateway")

    _sessions, partitions = cluster.make_services()
    await partitions.reconcile()

    owners = await cluster.owners()
    assert set(owners) == set(range(PARTITIONS))
    assert all(state == OWNED for state, _host in owners.values())
    assert sum(1 for _s, h in owners.values() if h == "lead-host") == PARTITIONS // 2
    assert sum(1 for _s, h in owners.values() if h == "worker-1") == PARTITIONS // 2
    assert all(h != "gateway" for _s, h in owners.values())

    # Renewing worker-1 to a higher membership epoch keeps its exact partitions without moving them.
    before = dict(owners)
    cluster.advance(10)
    cluster.issue_host("worker-1", roles=("worker_host",), agent_types=("researcher",))
    cluster.write_liveness("lead-host")
    cluster.write_liveness("worker-1")
    await partitions.reconcile()
    assert await cluster.owners() == before


@pytest.mark.asyncio
async def test_credential_fit_check_limits_partitions_assigned_to_host(tmp_path: Path) -> None:
    """A1: PartitionService verifies credential fit via fits_credential_limit before writing owned."""

    cluster = _ClusterFixture(tmp_path)
    cluster.issue_host("worker-tight", roles=("worker_host",), agent_types=("researcher",))
    cluster.write_liveness("worker-tight")

    # Compute exact credential size with 2 partitions so a 3rd partition would not fit.
    from custom_messages.wire.permissions import HostPlacement
    from custom_transport.protocol1.nats_jwt import user_credential_bytes

    cert = cluster.trust.store.host_certificate(INSTALLATION, "worker-tight", 1)
    assert cert is not None
    size_for_2 = user_credential_bytes(
        ("worker_host",),
        HostPlacement(
            runtime="worker-tight",
            teams=(TEAM,),
            partitions={TEAM: (0, 1)},
            pools={TEAM: ("researcher",)},
            installation=INSTALLATION,
        ),
        partition_count=PARTITIONS,
    )
    _sessions, partitions = cluster.make_services(max_credential_bytes=size_for_2)
    await partitions.reconcile()

    owners = await cluster.owners()
    # Exactly 2 partitions fit; the remaining 6 stay unowned rather than exceeding the credential limit.
    assert len(owners) == 2
    assert all(state == OWNED and host == "worker-tight" for state, host in owners.values())


@pytest.mark.asyncio
async def test_rebalancing_moves_only_empty_partitions_and_never_silently_moves_occupied_ones(tmp_path: Path) -> None:
    """A1: newly joined host gets empty partitions balanced; partitions holding placed agents never move silently."""

    cluster = _ClusterFixture(tmp_path)
    cluster.issue_host("worker-1", roles=("worker_host",), agent_types=("researcher",), session_limit=16)
    cluster.write_liveness("worker-1")

    sessions, partitions = cluster.make_services()
    await partitions.reconcile()
    assert all(h == "worker-1" for _s, h in (await cluster.owners()).values())

    # Place an agent in partitions 0..5 (leaving only 6 and 7 empty).
    for p in range(6):
        agent_id = cluster.agent_in_partition(p, prefix=f"res{p}")
        reply = await sessions.open(cluster.open_frame("worker-1", agent_id))
        assert reply["outcome"] == "ok"

    # Second host joins. Only empty partitions (6 and 7) may be rebalanced; 0..5 stay on worker-1.
    cluster.advance(5)
    cluster.issue_host("worker-2", roles=("worker_host",), agent_types=("researcher",), session_limit=16)
    cluster.write_liveness("worker-1")
    cluster.write_liveness("worker-2")
    await partitions.reconcile()

    owners = await cluster.owners()
    for p in range(6):
        assert owners[p] == (OWNED, "worker-1")
    assert owners[6] == (OWNED, "worker-2")
    assert owners[7] == (OWNED, "worker-2")


@pytest.mark.asyncio
async def test_shared_lock_serializes_concurrent_release_and_sessions_open_cr2(tmp_path: Path) -> None:
    """A2 / CR-2: PartitionService.release_partition and SessionService.open share the exact same lock."""

    cluster = _ClusterFixture(tmp_path)
    cluster.issue_host("worker-1", roles=("worker_host",), agent_types=("researcher",))
    cluster.issue_host("worker-2", roles=("worker_host",), agent_types=("researcher",))
    cluster.write_liveness("worker-1")
    cluster.write_liveness("worker-2")

    sessions, partitions = cluster.make_services(credentials_enforced=True)
    await partitions.reconcile()
    owners = await cluster.owners()
    p_w1 = next(p for p, (_s, h) in owners.items() if h == "worker-1")
    agent_id = cluster.agent_in_partition(p_w1)

    release_writing = asyncio.Event()
    allow_finish = asyncio.Event()

    async def pause_on_release(record: str, key: str, document: Mapping[str, Any]) -> None:
        if record == PARTITION_OWNER and document["body"]["state"] == RELEASED:
            release_writing.set()
            await allow_finish.wait()

    cluster.records.before_put_hook = pause_on_release
    release_task = asyncio.create_task(partitions.release_partition(p_w1, reason="operator", target_host="worker-2"))
    await release_writing.wait()

    # sessions.open starts while release_partition holds the shared lock mid-write.
    open_task = asyncio.create_task(sessions.open(cluster.open_frame("worker-1", agent_id)))
    await asyncio.sleep(0.02)
    assert not open_task.done()

    allow_finish.set()
    assert await release_task is True
    reply = await open_task
    SessionsOpenReplyAdapter.validate_python(reply)
    assert reply["outcome"] == "partition_not_owned"
    assert reply["partition"] == p_w1
    # Placement must not exist in UA1_PLACEMENT.
    assert await cluster.records.raw(PLACEMENT, placement_key(TEAM, agent_id)) is None


@pytest.mark.asyncio
async def test_four_step_connected_handover_with_instant_revocation_and_group_move(tmp_path: Path) -> None:
    """A4: connected handover enforces no overlapping release, waits for step-3 new NATS key + revocation, moves agents as a group."""

    cluster = _ClusterFixture(tmp_path)
    cluster.issue_host("lead-1", roles=("lead_host", "worker_host"), agent_types=(MANAGER_AGENT_TYPE, "researcher"))
    cluster.issue_host("lead-2", roles=("lead_host", "worker_host"), agent_types=(MANAGER_AGENT_TYPE, "researcher"))
    cluster.write_liveness("lead-1")
    cluster.write_liveness("lead-2")

    revoked_nats_keys: set[str] = set()
    sessions, partitions = cluster.make_services(
        credentials_enforced=True,
        credential_lifetime_seconds=3600,
        is_nats_key_revoked=lambda key: key in revoked_nats_keys,
    )
    await partitions.reconcile()
    owners = await cluster.owners()
    lead1_parts = sorted(p for p, (_s, h) in owners.items() if h == "lead-1")
    p_move, p_second = lead1_parts[0], lead1_parts[1]

    # Place BOTH a lead agent and a worker agent in p_move on lead-1.
    lead_agent = cluster.agent_in_partition(p_move, prefix="lead")
    worker_agent = cluster.agent_in_partition(p_move, prefix="wrk")
    lead_open = await sessions.open(cluster.open_frame("lead-1", lead_agent, MANAGER_AGENT_TYPE))
    worker_open = await sessions.open(cluster.open_frame("lead-1", worker_agent, "researcher"))
    assert lead_open["outcome"] == "ok" and worker_open["outcome"] == "ok"
    old_nats_key = cluster.host_keys["lead-1"].nats_public_key

    # Step 1: release p_move; overlapping release on lead-1 must be refused.
    assert await partitions.release_partition(p_move, reason="operator", target_host="lead-2") is True
    assert await partitions.release_partition(p_second, reason="operator", target_host="lead-2") is False
    assert (await cluster.owners())[p_move] == (RELEASED, "lead-1")

    # Before step 3 renewal, reconcile keeps p_move in released state.
    cluster.advance(5)
    cluster.write_liveness("lead-1")
    cluster.write_liveness("lead-2")
    await partitions.reconcile()
    assert (await cluster.owners())[p_move] == (RELEASED, "lead-1")

    # Step 2: lead-1 closes worker session as handed_over (leave lead session open to verify step 4 ends it too).
    closed_w = await sessions.close(cluster.close_frame("lead-1", worker_open["session"]["body"]["session"], "handed_over"))
    assert closed_w["outcome"] == "ok"

    # Step 3: lead-1 renews with a new NATS key, but revocation not confirmed yet -> still waiting!
    cluster.advance(5)
    cluster.issue_host("lead-1", roles=("lead_host", "worker_host"), agent_types=(MANAGER_AGENT_TYPE, "researcher"), new_nats_key=True)
    cluster.write_liveness("lead-1")
    cluster.write_liveness("lead-2")
    await partitions.reconcile()
    assert (await cluster.owners())[p_move] == (RELEASED, "lead-1")

    # Authority confirms revocation of old_nats_key -> Step 4 proceeds immediately!
    revoked_nats_keys.add(old_nats_key)
    await partitions.reconcile()
    assert (await cluster.owners())[p_move] == (OWNED, "lead-2")

    # Both agents in p_move moved as a group to lead-2 with handed_over=True so orphan cleanup preserves them.
    for ag in (lead_agent, worker_agent):
        pl = await cluster.records.read_report(PLACEMENT, placement_key(TEAM, ag), now=cluster.now)
        assert pl is not None
        assert pl.record.body.host == "lead-2"
        assert pl.record.body.previous_host == "lead-1"
        assert pl.record.body.handed_over is True

    # Running SessionService.maintain must NOT delete the moved placements as orphans!
    await sessions.maintain(TEAM)
    assert await cluster.records.read_report(PLACEMENT, placement_key(TEAM, worker_agent), now=cluster.now) is not None

    # And lead-2 can immediately open the moved lead agent's session (old lead session on lead-1 was ended in step 4)!
    reopened_lead = await sessions.open(cluster.open_frame("lead-2", lead_agent, MANAGER_AGENT_TYPE))
    assert reopened_lead["outcome"] == "ok"
    assert reopened_lead["session"]["body"]["agent_role"] == "lead"


@pytest.mark.asyncio
async def test_external_server_lifetime_wait_fallback_waits_for_lifetime_plus_clock_skew(tmp_path: Path) -> None:
    """A4 / A7: without instant revocation, step 4 waits credential_lifetime_seconds + CLOCK_SKEW_SECONDS after step 3."""

    cluster = _ClusterFixture(tmp_path)
    cluster.issue_host("worker-1", roles=("worker_host",), agent_types=("researcher",))
    cluster.issue_host("worker-2", roles=("worker_host",), agent_types=("researcher",))
    cluster.write_liveness("worker-1")
    cluster.write_liveness("worker-2")

    lifetime = 120
    _sessions, partitions = cluster.make_services(
        credentials_enforced=True,
        credential_lifetime_seconds=lifetime,
        is_nats_key_revoked=None,  # external server: no operator revocation
    )
    await partitions.reconcile()
    p_move = next(p for p, (_s, h) in (await cluster.owners()).items() if h == "worker-1")
    assert await partitions.release_partition(p_move, reason="operator", target_host="worker-2") is True

    # Step 3 renewal with new NATS key at t0 + 10s.
    cluster.advance(10)
    cluster.issue_host("worker-1", roles=("worker_host",), agent_types=("researcher",), new_nats_key=True)
    cluster.write_liveness("worker-1")
    cluster.write_liveness("worker-2")
    await partitions.reconcile()
    assert (await cluster.owners())[p_move] == (RELEASED, "worker-1")

    # 1 second before lifetime + CLOCK_SKEW_SECONDS -> still released!
    cluster.advance(lifetime + CLOCK_SKEW_SECONDS - 1)
    cluster.write_liveness("worker-1")
    cluster.write_liveness("worker-2")
    await partitions.reconcile()
    assert (await cluster.owners())[p_move] == (RELEASED, "worker-1")

    # At lifetime + CLOCK_SKEW_SECONDS -> reassigned to worker-2!
    cluster.advance(1)
    cluster.write_liveness("worker-1")
    cluster.write_liveness("worker-2")
    await partitions.reconcile()
    assert (await cluster.owners())[p_move] == (OWNED, "worker-2")


@pytest.mark.asyncio
async def test_shared_login_handover_waits_for_handed_over_or_deadline_and_recovers_after_restart(tmp_path: Path) -> None:
    """A4: under shared NATS login, step 4 waits for handed_over closes (or handover_seconds) and survives restart."""

    cluster = _ClusterFixture(tmp_path)
    cluster.issue_host("worker-1", roles=("worker_host",), agent_types=("researcher",))
    cluster.issue_host("worker-2", roles=("worker_host",), agent_types=("researcher",))
    cluster.write_liveness("worker-1")
    cluster.write_liveness("worker-2")

    sessions, partitions = cluster.make_services(credentials_enforced=False, handover_seconds=60)
    await partitions.reconcile()
    p_move = next(p for p, (_s, h) in (await cluster.owners()).items() if h == "worker-1")
    ag = cluster.agent_in_partition(p_move)
    opened = await sessions.open(cluster.open_frame("worker-1", ag))
    assert opened["outcome"] == "ok"

    assert await partitions.release_partition(p_move, reason="operator", target_host="worker-2") is True
    # Still not handed over -> stays released.
    cluster.advance(10)
    cluster.write_liveness("worker-1")
    cluster.write_liveness("worker-2")
    await partitions.reconcile()
    assert (await cluster.owners())[p_move] == (RELEASED, "worker-1")

    # Simulate team services restart (brand new PartitionService with empty in-memory _planned_targets).
    _sessions2, partitions_restarted = cluster.make_services(credentials_enforced=False, handover_seconds=60)
    # After handover_seconds elapses (even without handed_over close), restarted service finishes step 4!
    cluster.advance(50)
    cluster.write_liveness("worker-1")
    cluster.write_liveness("worker-2")
    await partitions_restarted.reconcile()
    assert (await cluster.owners())[p_move] == (OWNED, "worker-2")
    pl = await cluster.records.read_report(PLACEMENT, placement_key(TEAM, ag), now=cluster.now)
    assert pl is not None and pl.record.body.host == "worker-2"


@pytest.mark.asyncio
async def test_cut_off_and_retired_hosts_release_all_partitions_while_intermittent_stays_owned(tmp_path: Path) -> None:
    """A4: cut-off connected hosts and retired hosts release all partitions at once; intermittent hosts do not."""

    cluster = _ClusterFixture(tmp_path)
    cluster.issue_host("worker-conn", roles=("worker_host",), agent_types=("researcher",), connection_class="connected")
    cluster.issue_host("worker-edge", roles=("worker_host",), agent_types=("researcher",), connection_class="intermittent")
    cluster.issue_host("worker-live", roles=("worker_host",), agent_types=("researcher",), connection_class="connected")
    cluster.write_liveness("worker-conn")
    cluster.write_liveness("worker-edge")
    cluster.write_liveness("worker-live")

    revoked_nats: set[str] = set()
    _sessions, partitions = cluster.make_services(
        credentials_enforced=True,
        is_nats_key_revoked=lambda k: k in revoked_nats,
    )
    await partitions.reconcile()
    initial = await cluster.owners()
    conn_parts = sorted(p for p, (_s, h) in initial.items() if h == "worker-conn")
    edge_parts = sorted(p for p, (_s, h) in initial.items() if h == "worker-edge")
    assert len(conn_parts) >= 2 and len(edge_parts) >= 2
    conn_k1 = cluster.host_keys["worker-conn"].nats_public_key

    # Both worker-conn and worker-edge go offline (liveness gone).
    cluster.remove_liveness("worker-conn")
    cluster.remove_liveness("worker-edge")
    cluster.advance(5)
    cluster.write_liveness("worker-live")
    await partitions.reconcile()

    after_drop = await cluster.owners()
    # worker-conn (connected) had ALL its partitions released at once!
    for p in conn_parts:
        assert after_drop[p] == (RELEASED, "worker-conn")
    # worker-edge (intermittent) kept ALL its partitions owned!
    for p in edge_parts:
        assert after_drop[p] == (OWNED, "worker-edge")

    # Fast recovery: worker-conn's old key K1 is revoked AND worker-conn immediately rejoins with fresh K2
    # within 2 seconds (< CLOCK_SKEW_SECONDS). Step 4 must NOT stall waiting for K2!
    revoked_nats.add(conn_k1)
    cluster.advance(2)
    cluster.issue_host("worker-conn", roles=("worker_host",), agent_types=("researcher",), new_nats_key=True)
    cluster.write_liveness("worker-live")
    await partitions.reconcile()
    after_revoke = await cluster.owners()
    for p in conn_parts:
        assert after_revoke[p] == (OWNED, "worker-live")

    # Retiring worker-edge (revoking its certificate) releases ALL its partitions immediately even though intermittent.
    cluster.revoke_host("worker-edge")
    cluster.advance(1)
    cluster.write_liveness("worker-live")
    await partitions.reconcile()
    after_retire = await cluster.owners()
    for p in edge_parts:
        assert after_retire[p] == (RELEASED, "worker-edge")


@pytest.mark.asyncio
async def test_credential_too_large_refusal_recovery_reassigns_or_leaves_unowned(tmp_path: Path) -> None:
    """A4 / CR-4: host reporting partition-<p> credential_too_large triggers reassignment (preferring previous_host) or unowned."""

    cluster = _ClusterFixture(tmp_path)
    cluster.issue_host("worker-1", roles=("worker_host",), agent_types=("researcher",))
    cluster.issue_host("worker-2", roles=("worker_host",), agent_types=("researcher",))
    cluster.write_liveness("worker-1")
    cluster.write_liveness("worker-2")

    _sessions, partitions = cluster.make_services(credentials_enforced=False)
    await partitions.reconcile()
    p_w1 = next(p for p, (_s, h) in (await cluster.owners()).items() if h == "worker-1")

    # Move p_w1 to worker-2 so previous_host is worker-1.
    assert await partitions.release_partition(p_w1, reason="operator", target_host="worker-2") is True
    await partitions.reconcile()
    assert (await cluster.owners())[p_w1] == (OWNED, "worker-2")

    # worker-2 reports unhealthy partition-<p_w1> with code=partition_credential_too_large.
    cluster.advance(2)
    cluster.write_liveness("worker-1")
    cluster.write_liveness(
        "worker-2",
        components=[
            {
                "component": f"partition-{p_w1}",
                "state": "unhealthy",
                "code": PARTITION_CREDENTIAL_TOO_LARGE_CODE,
            }
        ],
    )
    await partitions.reconcile()
    # Recovered back to previous_host (worker-1)!
    assert (await cluster.owners())[p_w1] == (OWNED, "worker-1")

    # Now BOTH hosts report refusing p_w1 -> no host can fit it, so team services leave p_w1 unowned!
    cluster.advance(2)
    refuse = [{"component": f"partition-{p_w1}", "state": "unhealthy", "code": PARTITION_CREDENTIAL_TOO_LARGE_CODE}]
    cluster.write_liveness("worker-1", components=refuse)
    cluster.write_liveness("worker-2", components=refuse)
    await partitions.reconcile()
    assert p_w1 not in await cluster.owners()


@pytest.mark.asyncio
async def test_draining_and_other_contract_host_releases_partitions_and_maintain_resigns_records(tmp_path: Path) -> None:
    """A4 / README: draining or other-contract host gives up partitions; maintain re-signs under renewed team_services cert."""

    cluster = _ClusterFixture(tmp_path)
    cluster.issue_host("worker-1", roles=("worker_host",), agent_types=("researcher",))
    cluster.issue_host("worker-2", roles=("worker_host",), agent_types=("researcher",))
    cluster.write_liveness("worker-1")
    cluster.write_liveness("worker-2")

    _sessions, partitions = cluster.make_services(credentials_enforced=False)
    await partitions.reconcile()
    w1_parts = sorted(p for p, (_s, h) in (await cluster.owners()).items() if h == "worker-1")
    assert w1_parts

    # worker-1 switches to another contract -> team services drain its partitions to worker-2.
    cluster.advance(2)
    cluster.write_liveness("worker-1", contract_sha256="f" * 64)
    cluster.write_liveness("worker-2")
    for _ in range(len(w1_parts) + 1):
        await partitions.reconcile()
    assert all(h == "worker-2" for _s, h in (await cluster.owners()).values())

    # Team services renew their certificate to epoch 2 -> maintain() re-signs every partition_owner under epoch 2.
    cluster.advance(10)
    cluster.issue_host("team-services", roles=("team_services",), agent_types=(), session_limit=0)
    await partitions.maintain()
    for p in range(PARTITIONS):
        rec = await cluster.records.read_report(PARTITION_OWNER, partition_owner_key(TEAM, p, PARTITIONS), now=cluster.now)
        assert rec is not None and rec.record.signer is not None
        assert rec.record.signer.membership_epoch == 2
