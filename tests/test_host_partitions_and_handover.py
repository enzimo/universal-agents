"""Unit tests for host partition tracking, partition-scoped agent IDs, and host handover steps 2-3 (A3, A4, CR-4)."""

from __future__ import annotations

import asyncio
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Optional, Sequence

import pytest

from custom_agents.agent_host import (
    REFUSAL_CODES,
    AgentProcessHost,
    _HostedAgent,
    assign_partition_agent_id,
    draw_launch_agent_id,
)
from custom_agents.agent_requests import configure_process_host
from custom_messages.wire.encoding import format_timestamp
from custom_messages.wire.kinds import PROTOCOL
from custom_messages.wire.partitions import OWNED, RELEASED, agent_partition, partition_owner_key
from custom_messages.wire.trust import AuthorityKey, TrustStore, sign_state
from custom_transport.protocol1.host_keys import ensure_authority_keys, ensure_host_keys
from custom_transport.protocol1.host_partitions import (
    AGENT_ID_PATTERN,
    PARTITION_COMPONENT_PREFIX,
    PARTITION_CREDENTIAL_TOO_LARGE_CODE,
    HostPartitionTracker,
    draw_agent_id,
    draw_any_agent_id,
)
from custom_transport.protocol1.host_runtime import RenewalOutcome
from custom_transport.protocol1.joining import HostProfile
from custom_transport.protocol1.trust_state import PersistentTrustStore
from runtime_settings.instance_layout import InstanceLayout

INSTALLATION = "inst_host_part"
TEAM = "team_alpha"
PARTITIONS = 8
DEFINITION = "DQ0NDQ0NDQ0NDQ0NDQ0NDQ0NDQ0NDQ0NDQ0NDQ0NDQ0"


def _ts(dt: datetime) -> str:
    return format_timestamp(dt)


class _SignedCluster:
    """Minimal signed-record builder for host partition tracker and handover tests."""

    def __init__(self, tmp_path: Path) -> None:
        self.layout = InstanceLayout.at(tmp_path / "inst")
        self.now = datetime(2026, 11, 24, 12, 0, tzinfo=timezone.utc)
        self.authority = ensure_authority_keys(self.layout, INSTALLATION)[0]
        self.trust = PersistentTrustStore(
            TrustStore(
                INSTALLATION,
                [AuthorityKey(INSTALLATION, self.authority.key_id, self.authority.private_key.public_key())],
                partition_count=PARTITIONS,
            ),
            self.layout.host_trust_dir("worker-1") / "trust.json",
        )
        # Seed a current revocations record so state records verify.
        rev_doc = sign_state(
            {
                "record": "revocations",
                "protocol": PROTOCOL,
                "key": f"revocations.{INSTALLATION}",
                "writer": {"type": "authority", "installation": INSTALLATION, "key_id": self.authority.key_id},
                "sequence": 1,
                "issued_at": _ts(self.now),
                "expires_at": _ts(self.now + timedelta(hours=24)),
                "body": {
                    "installation": INSTALLATION,
                    "revocation_epoch": 1,
                    "entries": [],
                    "expires_at": _ts(self.now + timedelta(hours=24)),
                },
            },
            self.authority.private_key,
        )
        self.trust.add(json.dumps(rev_doc).encode(), now=self.now)
        self.keys: dict[str, Any] = {}
        self.certs: dict[str, Any] = {}
        self.cert_docs: dict[str, dict[str, Any]] = {}
        self.epochs: dict[str, int] = {}
        self.seq = 100

        self.issue_host("team-services", roles=("team_services",), agent_types=(), session_limit=0)
        self.issue_host("worker-1", roles=("worker_host",), agent_types=("researcher",), session_limit=4)
        self.issue_host("worker-2", roles=("worker_host",), agent_types=("researcher",), session_limit=4)

    def issue_host(
        self,
        runtime: str,
        *,
        roles: tuple[str, ...],
        agent_types: tuple[str, ...],
        session_limit: int,
    ) -> Any:
        keys = self.keys.get(runtime) or ensure_host_keys(self.layout, runtime)[0]
        self.keys[runtime] = keys
        epoch = self.epochs.get(runtime, 0) + 1
        self.epochs[runtime] = epoch
        cert_doc = sign_state(
            {
                "record": "certificate",
                "protocol": PROTOCOL,
                "key": f"host.{INSTALLATION}.{runtime}.{epoch}",
                "writer": {"type": "authority", "installation": INSTALLATION, "key_id": self.authority.key_id},
                "sequence": epoch,
                "issued_at": _ts(self.now),
                "expires_at": _ts(self.now + timedelta(days=30)),
                "body": {
                    "certificate_id": f"cert-{runtime}-{epoch}",
                    "installation": INSTALLATION,
                    "runtime": runtime,
                    "teams": [TEAM],
                    "roles": list(roles),
                    "agent_types": list(agent_types),
                    "session_limit": session_limit,
                    "connection_class": "connected",
                    "protocol": PROTOCOL,
                    "deployment": "local",
                    "application_key": {"key_id": keys.key_id, "public_key": keys.public_key_text},
                    "nats_public_key": keys.nats_public_key,
                    "membership_epoch": epoch,
                    "revocation_epoch": 1,
                    "profile_sha256": "A" * 43,
                    "issued_at": _ts(self.now),
                    "expires_at": _ts(self.now + timedelta(days=30)),
                },
            },
            self.authority.private_key,
        )
        self.trust.add(json.dumps(cert_doc).encode(), now=self.now)
        cert = self.trust.store.host_certificate(INSTALLATION, runtime, epoch)
        assert cert is not None
        self.certs[runtime] = cert
        self.cert_docs[runtime] = cert_doc
        return cert

    def sign_partition_owner(
        self,
        partition: int,
        *,
        state: str,
        host: str,
        reason: str = "initial",
        signer_runtime: str = "team-services",
        sequence: Optional[int] = None,
        assigned_at: Optional[datetime] = None,
        host_epoch: Optional[int] = None,
    ) -> tuple[str, bytes]:
        if sequence is None:
            self.seq += 1
            sequence = self.seq
        key = partition_owner_key(TEAM, partition, PARTITIONS)
        signer_keys = self.keys[signer_runtime]
        signer_cert = self.certs[signer_runtime]
        doc = sign_state(
            {
                "record": "partition_owner",
                "protocol": PROTOCOL,
                "key": key,
                "writer": {
                    "type": "host",
                    "installation": INSTALLATION,
                    "runtime": signer_runtime,
                    "membership_epoch": signer_cert.membership_epoch,
                    "key_id": signer_keys.key_id,
                },
                "sequence": sequence,
                "issued_at": _ts(self.now),
                "body": {
                    "team": TEAM,
                    "partition": partition,
                    "state": state,
                    "host": host,
                    "host_epoch": self.certs[host].membership_epoch if host_epoch is None else host_epoch,
                    "assigned_at": _ts(assigned_at or self.now),
                    "reason": reason,
                },
            },
            signer_keys.application_key,
        )
        return key, json.dumps(doc).encode()


class _FakeSubscription:
    def __init__(self) -> None:
        self.unsubscribed = 0

    async def unsubscribe(self) -> None:
        self.unsubscribed += 1


class _FakeRecords:
    def __init__(self) -> None:
        self.written: list[dict[str, Any]] = []
        self.removed: list[str] = []
        self.values: dict[str, dict[str, Any]] = {}

    def signed(self, record: str, key: str, body: dict[str, Any], *, sequence: int, issued_at: datetime,
               expires_at: Optional[datetime] = None) -> dict[str, Any]:
        return {"record": record, "key": key, "body": body}

    async def put(self, record: str, key: str, document: dict[str, Any]) -> int:
        self.written.append(document)
        self.values[key] = document
        return 1

    async def read_report(self, record: str, key: str, *, now: datetime) -> Any:
        if key not in self.values:
            return None
        return SimpleNamespace(revision=1, record=SimpleNamespace(signer=SimpleNamespace(runtime="worker-1")))

    async def delete(self, record: str, key: str, *, last_revision: int) -> bool:
        self.removed.append(key)
        self.values.pop(key, None)
        return True


class _FakeMessageHost:
    def __init__(self, cluster: _SignedCluster, runtime: str = "worker-1", *, credentials_issued: bool = False) -> None:
        self.cluster = cluster
        self.profile = HostProfile(runtime, (TEAM,), ("worker_host",), ("researcher",), 4)
        self.credentials_issued = credentials_issued
        self.credential_partitions: frozenset[int] = frozenset()
        self.connection: Optional[dict[str, Any]] = (
            {"jwt": "fake-jwt", "expires_at": _ts(cluster.now + timedelta(seconds=600))}
            if credentials_issued
            else None
        )
        self._connection_issued_at: Optional[datetime] = cluster.now if credentials_issued else None
        self.records = _FakeRecords()
        self.reports = 0
        self.renew_calls: list[tuple[tuple[int, ...], bool]] = []
        self.reject_partitions_on_renew: set[int] = set()
        self.max_renew_partitions: Optional[int] = None
        self.live = SimpleNamespace(
            identity=SimpleNamespace(
                installation=INSTALLATION,
                certificate=cluster.certs[runtime],
                sessions={},
                with_session=lambda a, s: self.live.identity,
                without_session=lambda a: self.live.identity,
            ),
            trust=cluster.trust,
        )

    @property
    def identity(self) -> Any:
        return self.live.identity

    def clock(self) -> datetime:
        return self.cluster.now

    async def report_now(self) -> None:
        self.reports += 1

    async def renew(
        self,
        *,
        partitions: Optional[Sequence[int]] = None,
        rotate_nats_key: bool = False,
    ) -> RenewalOutcome:
        req = tuple(sorted(set(partitions or ())))
        self.renew_calls.append((req, rotate_nats_key))
        if (
            any(p in self.reject_partitions_on_renew for p in req)
            or (self.max_renew_partitions is not None and len(req) > self.max_renew_partitions)
        ):
            return RenewalOutcome(
                ok=False,
                outcome="not_allowed",
                message="This host's NATS credential (70000 bytes) exceeds the 65536-byte limit (max_credential_bytes).",
                credential_too_large=True,
            )
        self.credential_partitions = frozenset(req)
        self.credentials_issued = True
        self.connection = {"jwt": f"jwt-{len(self.renew_calls)}", "expires_at": _ts(self.cluster.now + timedelta(seconds=600))}
        self._connection_issued_at = self.cluster.now
        return RenewalOutcome(
            ok=True,
            outcome="ok",
            certificate=self.live.identity.certificate,
            connection=self.connection,
            partitions=self.credential_partitions,
            rotated_nats_key=rotate_nats_key,
        )


def test_draw_agent_id_respects_contract_length_and_spreads_across_least_filled_partitions() -> None:
    """A3: generated IDs are <= 32 chars, match agent_id regex, and target least-filled owned partitions."""

    usable = [1, 4, 6]
    loads = {1: 2, 4: 1, 6: 0}

    first = draw_agent_id(TEAM, usable, PARTITIONS, prefix="agent", partition_loads=loads)
    assert len(first) <= 32
    assert AGENT_ID_PATTERN.fullmatch(first)
    assert agent_partition(TEAM, first, PARTITIONS) == 6

    # After partition 6 reaches load 1, both 4 and 6 tie for minimum load (1) while 1 (load 2) is skipped.
    loads[6] = 1
    seen = {
        agent_partition(TEAM, draw_agent_id(TEAM, usable, PARTITIONS, prefix="very_long_agent_prefix_name", partition_loads=loads), PARTITIONS)
        for _ in range(30)
    }
    assert seen <= {4, 6}
    assert 1 not in seen

    # Unconstrained fallback ID is also <= 32 chars and contract-valid.
    fallback = draw_any_agent_id(prefix="agent")
    assert len(fallback) <= 32
    assert AGENT_ID_PATTERN.fullmatch(fallback)

    with pytest.raises(ValueError):
        draw_agent_id(TEAM, [], PARTITIONS)


@pytest.mark.asyncio
async def test_tracker_verifies_signatures_enforces_replay_and_gates_usability_on_credentials(tmp_path: Path) -> None:
    """A3: tracker ignores forged/replayed records and gates usable_partitions on credentials when issued."""

    cluster = _SignedCluster(tmp_path)
    host = _FakeMessageHost(cluster, "worker-1", credentials_issued=False)
    tracker = HostPartitionTracker(host, TEAM)

    # Forged partition_owner signed by worker-2 (not team_services) MUST be rejected.
    key0, forged_raw = cluster.sign_partition_owner(0, state=OWNED, host="worker-1", signer_runtime="worker-2")
    assert await tracker.apply_event(key0, forged_raw) is False
    assert tracker.owned_by_host == frozenset()
    assert tracker.usable_partitions == frozenset()

    # Valid record from team-services under shared NATS login -> immediately usable.
    key0, raw_owned = cluster.sign_partition_owner(0, state=OWNED, host="worker-1", sequence=200)
    assert await tracker.apply_event(key0, raw_owned) is True
    assert tracker.owned_by_host == frozenset({0})
    assert tracker.usable_partitions == frozenset({0})
    assert tracker.has_usable.is_set()

    # Delete partition 0, then attempt replay of older sequence 199 -> MUST be refused!
    assert await tracker.apply_event(key0, None) is True
    assert tracker.owned_by_host == frozenset()
    key0, old_raw = cluster.sign_partition_owner(0, state=OWNED, host="worker-1", sequence=199)
    assert await tracker.apply_event(key0, old_raw) is False
    assert tracker.owned_by_host == frozenset()

    # Switch to issued-credentials mode: verified owned partition is NOT usable until credential names it.
    host.credentials_issued = True
    host.credential_partitions = frozenset()
    key2, raw2 = cluster.sign_partition_owner(2, state=OWNED, host="worker-1", sequence=205)
    assert await tracker.apply_event(key2, raw2) is True
    assert tracker.owned_by_host == frozenset({2})
    assert tracker.usable_partitions == frozenset()
    assert not tracker.has_usable.is_set()

    # Once host credential names partition 2, it becomes usable.
    host.credential_partitions = frozenset({2})
    tracker.refresh_usable()
    assert tracker.usable_partitions == frozenset({2})
    assert tracker.has_usable.is_set()


@pytest.mark.asyncio
async def test_agent_host_handover_step2_and_step3_with_credentials_and_shared_login(tmp_path: Path) -> None:
    """A4: host unsubscribes control, drains, closes sessions as handed_over, and renews+rotates key on step 3."""

    cluster = _SignedCluster(tmp_path)
    host = _FakeMessageHost(cluster, "worker-1", credentials_issued=True)
    host.credential_partitions = frozenset({1, 3})

    drained: list[tuple[str, str]] = []
    holding: set[str] = set()
    closed_requests: list[dict[str, Any]] = []

    async def fake_drain(agent: Any, reason: str) -> None:
        drained.append((str(agent.id), reason))

    async def fake_holds_work(agent: Any) -> bool:
        return str(agent.id) in holding

    process = AgentProcessHost(
        cluster.layout,
        host.profile,
        TEAM,
        drain_agent=fake_drain,
        holds_work=fake_holds_work,
        drain_poll_seconds=0.01,
        handover_seconds=60.0,
    )
    process.host = host  # type: ignore[assignment]
    process.loop = asyncio.get_running_loop()
    process._joined, process._stop = asyncio.Event(), asyncio.Event()
    process._joined.set()
    process._partition_wake = asyncio.Event()
    process.partitions = HostPartitionTracker(host, TEAM)

    async def fake_request(kind: str, body: dict[str, Any]) -> dict[str, Any]:
        if kind == "sessions.close":
            closed_requests.append(body)
        return {"outcome": "ok"}

    process._request = fake_request  # type: ignore[method-assign]

    # Seed partitions 1 and 3 as owned; attach one agent in partition 1 and one in partition 3.
    for p in (1, 3):
        k, raw = cluster.sign_partition_owner(p, state=OWNED, host="worker-1")
        await process.partitions.apply_event(k, raw)

    a_p1 = draw_agent_id(TEAM, [1], PARTITIONS, prefix="wrk")
    a_p3 = draw_agent_id(TEAM, [3], PARTITIONS, prefix="wrk")
    sub_p1, sub_p3 = _FakeSubscription(), _FakeSubscription()
    process.agents[a_p1] = _HostedAgent(SimpleNamespace(id=a_p1), "researcher", DEFINITION, session="s-1", subscription=sub_p1)
    process.agents[a_p3] = _HostedAgent(SimpleNamespace(id=a_p3), "researcher", DEFINITION, session="s-3", subscription=sub_p3)
    holding.add(a_p1)

    # Team services release partition 1 (step 1) AND also assign new partition 5 while step 2 runs.
    k1, rel1 = cluster.sign_partition_owner(1, state=RELEASED, host="worker-1", reason="operator")
    k5, own5 = cluster.sign_partition_owner(5, state=OWNED, host="worker-1", reason="rebalanced")
    await process.partitions.apply_event(k1, rel1)
    await process.partitions.apply_event(k5, own5)

    # Run handover in background: a_p1 holds work, so step 2 must unsubscribe + drain and wait before closing/renewing.
    task = asyncio.create_task(process._sync_partitions_once())
    await asyncio.sleep(0.05)
    assert sub_p1.unsubscribed == 1
    assert sub_p3.unsubscribed == 0
    assert [aid for aid, _ in drained] == [a_p1]
    assert process._handover_active is True  # routine renewals paused during step 2
    assert host.renew_calls == []
    assert closed_requests == []

    # Release work on a_p1 -> step 2 closes s-1 as handed_over, step 3 renews with [3, 5] and rotate_nats_key=True!
    holding.clear()
    needs_retry = await asyncio.wait_for(task, timeout=2.0)
    assert needs_retry is False
    assert process._handover_active is False
    assert a_p1 not in process.agents
    assert a_p3 in process.agents  # unaffected partition stays running!
    assert [(r["session"], r["reason"]) for r in closed_requests] == [("s-1", "handed_over")]
    assert host.renew_calls == [((3, 5), True)]
    assert process.partitions.usable_partitions == frozenset({3, 5})

    # Re-running sync while partition 1 is still released (waiting for team services step 4) must NOT repeat step 3!
    assert await process._sync_partitions_once() is False
    assert len(host.renew_calls) == 1

    # Under shared NATS login (credentials_issued=False), step 3 renews nothing!
    host.credentials_issued = False
    k3, rel3 = cluster.sign_partition_owner(3, state=RELEASED, host="worker-1", reason="operator")
    await process.partitions.apply_event(k3, rel3)
    assert await process._sync_partitions_once() is False
    assert [(r["session"], r["reason"]) for r in closed_requests] == [("s-1", "handed_over"), ("s-3", "handed_over")]
    assert len(host.renew_calls) == 1  # no renewal under shared login


@pytest.mark.asyncio
async def test_credential_too_large_renewal_marks_unhealthy_component_renews_without_it_and_clears_on_move(
    tmp_path: Path,
) -> None:
    """A4 / CR-4: if renewal hits credential_too_large, host drops rejected partition, reports partition-<p>, and clears on move."""

    cluster = _SignedCluster(tmp_path)
    host = _FakeMessageHost(cluster, "worker-1", credentials_issued=True)
    host.credential_partitions = frozenset({0})
    host.reject_partitions_on_renew.add(4)

    process = AgentProcessHost(cluster.layout, host.profile, TEAM, drain_poll_seconds=0.01)
    process.host = host  # type: ignore[assignment]
    process.loop = asyncio.get_running_loop()
    process._joined, process._stop = asyncio.Event(), asyncio.Event()
    process._joined.set()
    process._partition_wake = asyncio.Event()
    process.partitions = HostPartitionTracker(host, TEAM)
    process.partitions.on_change(process._wake_partitions)

    # worker-1 owns partitions 0, 2, 4; authority refuses 4 as max_credential_bytes.
    for p in (0, 2, 4):
        k, raw = cluster.sign_partition_owner(p, state=OWNED, host="worker-1")
        await process.partitions.apply_event(k, raw)

    assert await process._sync_partitions_once() is False
    # First tried (0, 2, 4) -> refused; automatically rejected 4 and renewed cleanly with (0, 2)!
    assert host.renew_calls == [((0, 2, 4), False), ((0, 2), False)]
    assert host.credential_partitions == frozenset({0, 2})
    assert process.partitions.rejected_partitions == frozenset({4})
    assert process.partitions.usable_partitions == frozenset({0, 2})
    assert process._components() == [
        {
            "component": f"{PARTITION_COMPONENT_PREFIX}4",
            "state": "unhealthy",
            "code": PARTITION_CREDENTIAL_TOO_LARGE_CODE,
        }
    ]

    # Team services recover by reassigning partition 4 to worker-2 -> rejection & unhealthy component clear automatically!
    k4, moved4 = cluster.sign_partition_owner(4, state=OWNED, host="worker-2", reason="rebalanced")
    await process.partitions.apply_event(k4, moved4)
    assert process.partitions.rejected_partitions == frozenset()
    assert process._components() == []


@pytest.mark.asyncio
async def test_launch_and_startup_agent_ids_draw_in_owned_partitions_and_spread_batch_launches(tmp_path: Path) -> None:
    """A3: dynamic worker launches and startup agents draw <= 32-char IDs inside least-filled usable owned partitions."""

    cluster = _SignedCluster(tmp_path)
    host = _FakeMessageHost(cluster, "worker-1", credentials_issued=False)
    process = AgentProcessHost(cluster.layout, host.profile, TEAM, partition_wait_seconds=1.0)
    process.host = host  # type: ignore[assignment]
    process.loop = asyncio.get_running_loop()
    process._joined, process._stop = asyncio.Event(), asyncio.Event()
    process._joined.set()
    process.partitions = HostPartitionTracker(host, TEAM)

    owned = (1, 3, 5)
    for p in owned:
        k, raw = cluster.sign_partition_owner(p, state=OWNED, host="worker-1")
        await process.partitions.apply_event(k, raw)

    configure_process_host(process)
    try:
        # Launching 3 workers synchronously back-to-back spreads 1 per owned partition (not all in one)!
        launched_ids = [draw_launch_agent_id(TEAM) for _ in range(3)]
        assert all(len(aid) <= 32 and AGENT_ID_PATTERN.fullmatch(aid) for aid in launched_ids)
        assert {agent_partition(TEAM, aid, PARTITIONS) for aid in launched_ids} == set(owned)

        # Auto-generated startup agent whose initial ID falls outside owned partitions adopts a valid owned ID.
        outside_id = draw_agent_id(TEAM, [0], PARTITIONS, prefix="agent")
        adopted: list[str] = []
        fake_agent = SimpleNamespace(
            id=outside_id,
            team_id=TEAM,
            model_fields_set=set(),
            _worker_launch=None,
            _adopt_agent_id=lambda new_id: adopted.append(new_id),
        )
        await assign_partition_agent_id(fake_agent, SimpleNamespace(nats_connection=None))
        assert len(adopted) == 1
        assert len(adopted[0]) <= 32
        assert agent_partition(TEAM, adopted[0], PARTITIONS) in set(owned)

        # Operator-pinned ID ("id" in model_fields_set) is preserved untouched.
        adopted_pinned: list[str] = []
        pinned_agent = SimpleNamespace(
            id=outside_id,
            team_id=TEAM,
            model_fields_set={"id"},
            _worker_launch=None,
            _adopt_agent_id=lambda new_id: adopted_pinned.append(new_id),
        )
        await assign_partition_agent_id(pinned_agent, SimpleNamespace(nats_connection=None))
        assert adopted_pinned == []
    finally:
        configure_process_host(None)


def test_partition_not_owned_refusal_code_is_registered() -> None:
    """A2 / A3: partition_not_owned maps to a documented host refusal code."""

    assert REFUSAL_CODES["partition_not_owned"] == "session_partition_not_owned"


@pytest.mark.asyncio
async def test_handover_deadline_closes_still_holding_agent_and_runs_step3(tmp_path: Path) -> None:
    """A4: when an agent still holds work past handover_seconds (or near credential expiry), step 2 closes and step 3 runs."""

    cluster = _SignedCluster(tmp_path)
    host = _FakeMessageHost(cluster, "worker-1", credentials_issued=True)
    host.credential_partitions = frozenset({2, 6})

    closed_requests: list[dict[str, Any]] = []

    async def fake_drain(agent: Any, reason: str) -> None:
        return None

    async def always_holding(agent: Any) -> bool:
        return True

    process = AgentProcessHost(
        cluster.layout,
        host.profile,
        TEAM,
        drain_agent=fake_drain,
        holds_work=always_holding,
        drain_poll_seconds=0.01,
        handover_seconds=30.0,
    )
    process.host = host  # type: ignore[assignment]
    process.loop = asyncio.get_running_loop()
    process._joined, process._stop = asyncio.Event(), asyncio.Event()
    process._joined.set()
    process._partition_wake = asyncio.Event()
    process.partitions = HostPartitionTracker(host, TEAM)

    async def fake_request(kind: str, body: dict[str, Any]) -> dict[str, Any]:
        if kind == "sessions.close":
            closed_requests.append(body)
        return {"outcome": "ok"}

    process._request = fake_request  # type: ignore[method-assign]

    for p in (2, 6):
        k, raw = cluster.sign_partition_owner(p, state=OWNED, host="worker-1")
        await process.partitions.apply_event(k, raw)
    a_p2 = draw_agent_id(TEAM, [2], PARTITIONS, prefix="wrk")
    process.agents[a_p2] = _HostedAgent(SimpleNamespace(id=a_p2), "researcher", DEFINITION, session="s-2",
                                        subscription=_FakeSubscription())

    # Release partition 2 at t0; advance clock past handover_seconds (31s) so step 2 hits deadline immediately.
    k2, rel2 = cluster.sign_partition_owner(2, state=RELEASED, host="worker-1", reason="operator", assigned_at=cluster.now)
    await process.partitions.apply_event(k2, rel2)
    cluster.now = cluster.now + timedelta(seconds=31)

    assert await process._sync_partitions_once() is False
    assert a_p2 not in process.agents
    assert [(r["session"], r["reason"]) for r in closed_requests] == [("s-2", "handed_over")]
    assert host.renew_calls == [((6,), True)]


def test_message_host_renewal_due_tracks_connection_lifetime_and_respects_pause(tmp_path: Path) -> None:
    """A4 / A7: MessageHost renews at 2/3 of connection credential lifetime and pauses during handover step 2."""

    from custom_transport.protocol1.host_runtime import MessageHost

    cluster = _SignedCluster(tmp_path)
    now = [cluster.now]
    paused = [False]
    mhost = MessageHost(
        cluster.layout,
        HostProfile("worker-1", (TEAM,), ("worker_host",), ("researcher",), 4),
        nc=None,
        clock=lambda: now[0],
        renewal_paused=lambda: paused[0],
    )
    mhost.live = SimpleNamespace(identity=SimpleNamespace(certificate=cluster.certs["worker-1"]))  # type: ignore[assignment]
    # 30-day cert is nowhere near 2/3, but 600s connection credential reaches 2/3 at 400s!
    mhost._record_join_connection({"jwt": "j", "expires_at": _ts(cluster.now + timedelta(seconds=600))})
    assert mhost.credentials_issued is True
    assert mhost.credential_partitions == frozenset()
    assert mhost._renewal_due() is False

    now[0] = cluster.now + timedelta(seconds=401)
    assert mhost._renewal_due() is True

    # Paused during handover step 2 -> must NOT run routine renewal!
    paused[0] = True
    assert mhost._renewal_due() is False


def test_universal_agent_adopt_agent_id_preserves_process_scope_and_updates_subsystems() -> None:
    """A3: _adopt_agent_id updates id, name suffix, task_manager, and context engine without marking id explicit."""

    from custom_agents.strands_agent import UniversalAgent, _agent_consumer_scope
    from custom_transport.base import ConsumerScope

    updated_owner: list[tuple[Any, ...]] = []
    maint_plugin = SimpleNamespace(agent_id="old_id_1234")
    mpm = SimpleNamespace(agent_id="old_id_1234", maintenance_plugins=[maint_plugin])
    ctx = SimpleNamespace(agent_id="old_id_1234", memory_plugin_manager=mpm)
    tm = SimpleNamespace(set_owner_identity=lambda *args: updated_owner.append(args))

    fake = SimpleNamespace(
        id="agent_id_abcd1234",
        name="clever-fox-1234",
        team_id=TEAM,
        named_agent_type="Researcher",
        model_fields_set=set(),
        _worker_launch=None,
        task_manager=tm,
        _context_engine=ctx,
        _logger=None,
    )

    UniversalAgent._adopt_agent_id(fake, "agent_998877665544")  # type: ignore[arg-type]

    assert fake.id == "agent_998877665544"
    assert fake.name == "clever-fox-5544"
    assert "id" not in fake.model_fields_set
    assert _agent_consumer_scope(fake) == ConsumerScope.PROCESS
    assert updated_owner == [("agent_998877665544", "clever-fox-5544", TEAM, "Researcher")]
    assert ctx.agent_id == "agent_998877665544"
    assert mpm.agent_id == "agent_998877665544"
    assert maint_plugin.agent_id == "agent_998877665544"


def test_adopt_agent_id_rebuilds_strands_offloader_and_passes_owns_artifact(tmp_path: Path) -> None:
    """C25: _adopt_agent_id rebuilds _strands_agent so offloaded tool results record adopted agent_id and pass _owns_artifact."""

    from custom_agents.strands_agent import UniversalAgent
    from custom_agents.strands_agent_builder import build_strands_parts
    from custom_agents.tool_result_offloader import TaskAwareToolResultOffloader
    from custom_tools.runtime_context import set_agent_ref, task_work
    from custom_tools.tool_result_artifact_tools import _owns_artifact
    from storage.config import resolve_storage_settings

    class _DummyStrandsAgent:
        def __init__(self, *, system_prompt: str, tools: list[Any], model: Any,
                     conversation_manager: Any, callback_handler: Any, hooks: list[Any],
                     tool_executor: Any, **kwargs: Any) -> None:
            self.system_prompt = system_prompt
            self.tools = tools
            self.model = model
            self.conversation_manager = conversation_manager
            self.hooks = hooks
            self.tool_executor = tool_executor

    class _DummyContextEngine:
        def __init__(self, agent_id: str) -> None:
            self.agent_id = agent_id
            self.memory_plugin_manager = None

        def build_inner_loop_conversation_manager(self, storage: Any) -> Any:
            return SimpleNamespace(storage=storage)

    layout = InstanceLayout.at(tmp_path / "inst")
    layout.tool_results_dir.mkdir(parents=True, exist_ok=True)
    tm = SimpleNamespace(
        set_owner_identity=lambda *args: None,
        add_task_terminal_callback=lambda cb: None,
        add_task_release_callback=lambda cb: None,
        storage_settings=resolve_storage_settings(None, sqlite_fallback_path=str(layout.root / "tasks.db")),
        get_task_info=lambda tid: SimpleNamespace(meta=SimpleNamespace(conversation_id="conv-1")),
    )
    fake = SimpleNamespace(
        id="agent_old00001",
        name="clever-fox-0001",
        team_id=TEAM,
        named_agent_type="Researcher",
        is_team_lead=False,
        authorization_config={"enabled": False},
        model_fields_set=set(),
        _worker_launch=None,
        task_manager=tm,
        _context_engine=_DummyContextEngine("agent_old00001"),
        _context_debug_dump_plugin=None,
        _logger=None,
        _strands_model=object(),
        _strands_generation=1,
        _strands_parts=None,
        _strands_agent=None,
        system_prompt="prompt",
        structured_output_failure_threshold=2,
        max_tool_calls_per_task=10,
        max_tool_result_tokens_per_call=20,
        tools=[],
        plugins=[],
        instance_config_path=str(layout.root / "instance_config.yaml"),
        agent_definition_path=None,
        _refresh_skills_plugins=lambda: None,
        _model_failover_retry_strategy_config=lambda: None,
        _configure_model=lambda: object(),
    )

    def _create_strands_agent(*, keep_loaded_tools: bool = False) -> None:
        parts = build_strands_parts(fake, agent_class=_DummyStrandsAgent, loaded=None)
        fake._strands_parts = parts
        fake._strands_agent = parts.agent

    fake._create_strands_agent = _create_strands_agent
    fake._create_strands_agent()

    initial_offloader = next(h for h in fake._strands_agent.hooks if isinstance(h, TaskAwareToolResultOffloader))
    assert initial_offloader.storage.agent_id == "agent_old00001"
    assert fake._strands_agent.conversation_manager.storage.agent_id == "agent_old00001"

    UniversalAgent._adopt_agent_id(fake, "agent_new99999")  # type: ignore[arg-type]

    rebuilt_offloader = next(h for h in fake._strands_agent.hooks if isinstance(h, TaskAwareToolResultOffloader))
    assert rebuilt_offloader is not initial_offloader
    assert rebuilt_offloader.storage.agent_id == "agent_new99999"
    assert fake._strands_agent.conversation_manager.storage is rebuilt_offloader.storage

    # Offload an oversized result during a turn (event.agent is the Strands SDK Agent with no .id) and verify _owns_artifact.
    fake_event = SimpleNamespace(
        agent=fake._strands_agent,
        tool_use={"name": "big_tool", "toolUseId": "tu-1"},
        result={"status": "success", "content": [{"text": "x" * 2000}]},
    )
    set_agent_ref(fake)  # type: ignore[arg-type]
    try:
        with task_work("task-123"):
            stored = rebuilt_offloader.storage.store_result(
                result=fake_event.result,
                event=fake_event,  # type: ignore[arg-type]
                tool_name="big_tool",
                result_tokens=500,
                result_text="x" * 2000,
                max_tokens_per_call=20,
                preview_tokens=10,
                replacement_status="success",
            )
            manifest = json.loads(Path(stored["artifact_manifest_path"]).read_text(encoding="utf-8"))
            assert manifest["agent_id"] == "agent_new99999"
            assert _owns_artifact(manifest) is True
    finally:
        set_agent_ref(None)


@pytest.mark.asyncio
async def test_rejoin_resets_usable_partitions_and_wakes_coordinator_to_reacquire_them(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """C26(1): rejoin resets credential_partitions, notifies on_credential to refresh usable_partitions, and wakes coordinator."""

    import custom_transport.protocol1.host_runtime as host_runtime_mod
    from custom_transport.protocol1.host_runtime import MessageHost
    from custom_transport.protocol1.identity import HostIdentity

    cluster = _SignedCluster(tmp_path)
    mhost = MessageHost(
        cluster.layout,
        HostProfile("worker-1", (TEAM,), ("worker_host",), ("researcher",), 4),
        nc=None,
        clock=lambda: cluster.now,
    )
    mhost.live = SimpleNamespace(
        identity=HostIdentity(cluster.keys["worker-1"], cluster.certs["worker-1"]),
        trust=cluster.trust,
    )  # type: ignore[assignment]
    mhost.credentials_issued = True
    mhost.credential_partitions = frozenset({1, 3})

    process = AgentProcessHost(cluster.layout, mhost.profile, TEAM)
    process.host = mhost
    process.loop = asyncio.get_running_loop()
    process._joined, process._stop = asyncio.Event(), asyncio.Event()
    process._joined.set()
    process._partition_wake = asyncio.Event()
    process.partitions = HostPartitionTracker(mhost, TEAM)
    mhost.renewal_partitions = process._renewal_partitions
    mhost.renewal_paused = process._is_renewal_paused
    mhost.on_credential = process._on_credential_changed

    for p in (1, 3):
        k, raw = cluster.sign_partition_owner(p, state=OWNED, host="worker-1")
        await process.partitions.apply_event(k, raw)
    assert process.partitions.usable_partitions == frozenset({1, 3})
    assert process.partitions.has_usable.is_set()
    process._partition_wake.clear()

    new_cert = cluster.issue_host(
        "worker-1", roles=("worker_host",), agent_types=("researcher", "specialist"), session_limit=4
    )

    async def fake_join(nc: Any, keys: Any, trust: Any, profile: Any, **kwargs: Any) -> Any:
        on_connection = kwargs.get("on_connection")
        if on_connection is not None:
            on_connection({"jwt": "join-jwt", "expires_at": _ts(cluster.now + timedelta(seconds=600))})
        return new_cert

    monkeypatch.setattr(host_runtime_mod, "join", fake_join)

    rejoined = await mhost.rejoin(["specialist"])
    assert "specialist" in rejoined.granted
    assert mhost.credential_partitions == frozenset()
    assert process.partitions.usable_partitions == frozenset()
    assert not process.partitions.has_usable.is_set()
    assert process._partition_wake.is_set()

    # Coordinator wakes up and renews to restore owned partitions {1, 3}.
    renew_requests: list[dict[str, Any]] = []
    renewed_cert = cluster.issue_host(
        "worker-1", roles=("worker_host",), agent_types=("researcher", "specialist"), session_limit=4
    )
    renewed_doc = cluster.cert_docs["worker-1"]

    async def fake_live_request(kind: str, body: dict[str, Any], audience: Any, *, team: str) -> Any:
        renew_requests.append(body)
        return SimpleNamespace(
            raw={
                "body": {
                    "outcome": "ok",
                    "certificate": renewed_doc,
                    "connection": {"jwt": "renew-jwt", "expires_at": _ts(cluster.now + timedelta(seconds=600))},
                }
            }
        )

    mhost.live.request = fake_live_request  # type: ignore[attr-defined]
    assert await process._sync_partitions_once() is False
    assert renew_requests[0]["partitions"] == [1, 3]
    assert mhost.live.identity.certificate.membership_epoch == renewed_cert.membership_epoch
    assert process.partitions.usable_partitions == frozenset({1, 3})
    assert process.partitions.has_usable.is_set()


@pytest.mark.asyncio
async def test_routine_renew_if_due_updates_usable_partitions_and_sync_refreshes_on_match(tmp_path: Path) -> None:
    """C26(2): routine renew_if_due picking up a new owned partition refreshes usable_partitions; sync also refreshes when equal."""

    from custom_transport.protocol1.host_runtime import MessageHost
    from custom_transport.protocol1.identity import HostIdentity

    cluster = _SignedCluster(tmp_path)
    mhost = MessageHost(
        cluster.layout,
        HostProfile("worker-1", (TEAM,), ("worker_host",), ("researcher",), 4),
        nc=None,
        clock=lambda: cluster.now,
    )
    mhost.live = SimpleNamespace(
        identity=HostIdentity(cluster.keys["worker-1"], cluster.certs["worker-1"]),
        trust=cluster.trust,
    )  # type: ignore[assignment]
    mhost._record_join_connection({"jwt": "j0", "expires_at": _ts(cluster.now + timedelta(seconds=600))})
    mhost.credential_partitions = frozenset({1})

    process = AgentProcessHost(cluster.layout, mhost.profile, TEAM)
    process.host = mhost
    process.loop = asyncio.get_running_loop()
    process._joined, process._stop = asyncio.Event(), asyncio.Event()
    process._joined.set()
    process._partition_wake = asyncio.Event()
    process.partitions = HostPartitionTracker(mhost, TEAM)
    mhost.renewal_partitions = process._renewal_partitions
    mhost.renewal_paused = process._is_renewal_paused
    mhost.on_credential = process._on_credential_changed

    # Assign partition 1 (already in credential) and new partition 3 (not yet in credential).
    for p in (1, 3):
        k, raw = cluster.sign_partition_owner(p, state=OWNED, host="worker-1")
        await process.partitions.apply_event(k, raw)
    assert process.partitions.usable_partitions == frozenset({1})

    # Routine 2/3-lifetime renewal fires before _sync_partitions_once and picks up (1, 3).
    cluster.now = cluster.now + timedelta(seconds=450)
    cluster.issue_host("worker-1", roles=("worker_host",), agent_types=("researcher",), session_limit=4)
    renewed_doc = cluster.cert_docs["worker-1"]

    async def fake_live_request(kind: str, body: dict[str, Any], audience: Any, *, team: str) -> Any:
        assert body["partitions"] == [1, 3]
        assert "new_nats_public_key" not in body
        return SimpleNamespace(
            raw={
                "body": {
                    "outcome": "ok",
                    "certificate": renewed_doc,
                    "connection": {"jwt": "j1", "expires_at": _ts(cluster.now + timedelta(seconds=600))},
                }
            }
        )

    mhost.live.request = fake_live_request  # type: ignore[attr-defined]
    await mhost.renew_if_due()
    assert mhost.credential_partitions == frozenset({1, 3})
    assert process.partitions.usable_partitions == frozenset({1, 3})
    assert process.partitions.has_usable.is_set()

    # Even if on_credential were absent and credential_partitions already matched desired, _sync_partitions_once refreshes usable!
    mhost.on_credential = None
    k5, raw5 = cluster.sign_partition_owner(5, state=OWNED, host="worker-1")
    await process.partitions.apply_event(k5, raw5)
    mhost.credential_partitions = frozenset({1, 3, 5})
    assert 5 not in process.partitions.usable_partitions
    assert await process._sync_partitions_once() is False
    assert process.partitions.usable_partitions == frozenset({1, 3, 5})


@pytest.mark.asyncio
async def test_renewal_paused_covers_pending_release_and_step3_retry_backoff_and_drop_rotates_key(
    tmp_path: Path,
) -> None:
    """C26(3-4): renewal_paused stays true from release arrival through step-3 retry backoff; dropping any held partition rotates key."""

    from custom_transport.protocol1.host_runtime import MessageHost
    from custom_transport.protocol1.identity import HostIdentity

    cluster = _SignedCluster(tmp_path)
    host = _FakeMessageHost(cluster, "worker-1", credentials_issued=True)
    host.credential_partitions = frozenset({1, 3})

    process = AgentProcessHost(cluster.layout, host.profile, TEAM, drain_poll_seconds=0.01)
    process.host = host  # type: ignore[assignment]
    process.loop = asyncio.get_running_loop()
    process._joined, process._stop = asyncio.Event(), asyncio.Event()
    process._joined.set()
    process._partition_wake = asyncio.Event()
    process.partitions = HostPartitionTracker(host, TEAM)

    for p in (1, 3):
        k, raw = cluster.sign_partition_owner(p, state=OWNED, host="worker-1")
        await process.partitions.apply_event(k, raw)
    assert process._is_renewal_paused() is False

    # As soon as watch records a released partition (before _execute_handover starts), routine renewals are paused!
    k1, rel1 = cluster.sign_partition_owner(1, state=RELEASED, host="worker-1", reason="operator")
    await process.partitions.apply_event(k1, rel1)
    assert process._handover_active is False
    assert process._is_renewal_paused() is True

    # Simulate transient step-3 renewal failure -> _handover_active resets in finally, yet renewal_paused MUST stay True!
    orig_renew = host.renew

    async def failing_renew(**kwargs: Any) -> RenewalOutcome:
        return RenewalOutcome(ok=False, outcome="temporarily_unavailable", message="retry")

    host.renew = failing_renew  # type: ignore[method-assign]
    assert await process._sync_partitions_once() is True  # needs_retry
    assert process._handover_active is False
    assert process._is_renewal_paused() is True  # still paused during partition_retry_seconds sleep!

    # Once step 3 succeeds, handover is recorded in _completed_handovers and renewal_paused clears.
    host.renew = orig_renew  # type: ignore[method-assign]
    assert await process._sync_partitions_once() is False
    assert process._is_renewal_paused() is False

    # MessageHost._renew dropping an already-held partition (non-drained, e.g. CR-4 shrink) MUST rotate the NATS key!
    mhost = MessageHost(
        cluster.layout,
        HostProfile("worker-1", (TEAM,), ("worker_host",), ("researcher",), 4),
        nc=None,
        clock=lambda: cluster.now,
    )
    mhost.live = SimpleNamespace(
        identity=HostIdentity(cluster.keys["worker-1"], cluster.certs["worker-1"]),
        trust=cluster.trust,
    )  # type: ignore[assignment]
    mhost.credentials_issued = True
    mhost.credential_partitions = frozenset({0, 2})

    seen_bodies: list[dict[str, Any]] = []
    cluster.issue_host("worker-1", roles=("worker_host",), agent_types=("researcher",), session_limit=4)
    renewed_doc = cluster.cert_docs["worker-1"]

    async def fake_live_request(kind: str, body: dict[str, Any], audience: Any, *, team: str) -> Any:
        seen_bodies.append(body)
        return SimpleNamespace(
            raw={
                "body": {
                    "outcome": "ok",
                    "certificate": renewed_doc,
                    "connection": {"jwt": "j-shrink", "expires_at": _ts(cluster.now + timedelta(seconds=600))},
                }
            }
        )

    mhost.live.request = fake_live_request  # type: ignore[attr-defined]
    outcome = await mhost.renew(partitions=[0], rotate_nats_key=False)
    assert outcome.ok is True
    assert outcome.rotated_nats_key is True
    assert "new_nats_public_key" in seen_bodies[0]
    assert seen_bodies[0]["partitions"] == [0]


@pytest.mark.asyncio
async def test_execute_handover_recomputes_still_released_before_closing_and_marking_completed(tmp_path: Path) -> None:
    """C27: _execute_handover closes sessions and marks _completed_handovers only for still-released partitions."""

    cluster = _SignedCluster(tmp_path)
    host = _FakeMessageHost(cluster, "worker-1", credentials_issued=True)
    host.credential_partitions = frozenset({1, 2})
    closed_requests: list[dict[str, Any]] = []

    process = AgentProcessHost(cluster.layout, host.profile, TEAM, drain_poll_seconds=0.01, handover_seconds=60.0)
    process.host = host  # type: ignore[assignment]
    process.loop = asyncio.get_running_loop()
    process._joined, process._stop = asyncio.Event(), asyncio.Event()
    process._joined.set()
    process._partition_wake = asyncio.Event()
    process.partitions = HostPartitionTracker(host, TEAM)

    async def fake_request(kind: str, body: dict[str, Any]) -> dict[str, Any]:
        if kind == "sessions.close":
            closed_requests.append(body)
        return {"outcome": "ok"}

    process._request = fake_request  # type: ignore[method-assign]

    a_p1 = draw_agent_id(TEAM, [1], PARTITIONS, prefix="wrk")
    a_p2 = draw_agent_id(TEAM, [2], PARTITIONS, prefix="wrk")
    process.agents[a_p1] = _HostedAgent(SimpleNamespace(id=a_p1), "researcher", DEFINITION, session="s-1",
                                        subscription=_FakeSubscription())
    process.agents[a_p2] = _HostedAgent(SimpleNamespace(id=a_p2), "researcher", DEFINITION, session="s-2",
                                        subscription=_FakeSubscription())

    # Release both partitions 1 and 2; while _still_holds runs, partition 2 returns to owned!
    k1, rel1 = cluster.sign_partition_owner(1, state=RELEASED, host="worker-1", reason="operator")
    k2, rel2 = cluster.sign_partition_owner(2, state=RELEASED, host="worker-1", reason="operator")
    await process.partitions.apply_event(k1, rel1)
    await process.partitions.apply_event(k2, rel2)

    reclaimed = False

    async def fake_drain(agent: Any, reason: str) -> None:
        return None

    async def holds_and_reclaim_p2(agent: Any) -> bool:
        nonlocal reclaimed
        if not reclaimed:
            reclaimed = True
            k2_own, own2 = cluster.sign_partition_owner(2, state=OWNED, host="worker-1", reason="rebalanced")
            await process.partitions.apply_event(k2_own, own2)
        return False

    process.drain_agent = fake_drain
    process.holds_work = holds_and_reclaim_p2

    assert await process._sync_partitions_once() is False
    # Only partition 1's agent was closed; partition 2's agent stayed untouched and partition 2 is NOT in _completed_handovers!
    assert a_p1 not in process.agents
    assert a_p2 in process.agents
    assert [(r["session"], r["reason"]) for r in closed_requests] == [("s-1", "handed_over")]
    assert set(process._completed_handovers) == {1}
    assert host.renew_calls == [((2,), True)]


@pytest.mark.asyncio
async def test_tracker_run_sets_ready_immediately_on_quiet_catch_up_and_prunes_missing_on_reconnect(
    tmp_path: Path,
) -> None:
    """C28: HostPartitionTracker.run sets ready when caught_up fires on a quiet bucket and prunes deleted keys on reconnect."""

    cluster = _SignedCluster(tmp_path)
    host = _FakeMessageHost(cluster, "worker-1", credentials_issued=False)
    k0, raw0 = cluster.sign_partition_owner(0, state=OWNED, host="worker-1", sequence=300)

    watch_attempt = 0
    release_first_watch = asyncio.Event()

    async def fake_watch(record: str, pattern: str, *, team: Optional[str] = None,
                         caught_up: Optional[asyncio.Event] = None):
        nonlocal watch_attempt
        watch_attempt += 1
        if watch_attempt == 1:
            yield k0, raw0
            if caught_up is not None:
                caught_up.set()
            # Quiet bucket: stay blocked without yielding anything else until test releases us.
            await release_first_watch.wait()
            return
        # Reconnect: bucket is now empty (partition 0 was deleted while watch was down), quiet after catch-up.
        if caught_up is not None:
            caught_up.set()
        await asyncio.Future()

    host.records.watch = fake_watch  # type: ignore[attr-defined]
    tracker = HostPartitionTracker(host, TEAM, watch_retry_seconds=0.01)
    stop = asyncio.Event()
    task = asyncio.create_task(tracker.run(stop))
    try:
        # Must become ready immediately on catch-up even though no further KV mutations arrive!
        await asyncio.wait_for(tracker.ready.wait(), timeout=1.0)
        assert tracker.owned_by_host == frozenset({0})
        assert tracker.usable_partitions == frozenset({0})

        # Simulate watch reconnect where partition 0 disappeared while disconnected -> stale entry pruned on catch-up.
        reconnected = asyncio.Event()
        tracker.on_change(lambda: reconnected.set() if not tracker.owned_by_host else None)
        release_first_watch.set()
        await asyncio.wait_for(reconnected.wait(), timeout=1.0)
        assert tracker.owned_by_host == frozenset()
        assert tracker.usable_partitions == frozenset()
    finally:
        stop.set()
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)


@pytest.mark.asyncio
async def test_f1_f2_message_host_renew_recovers_on_new_nats_public_key_refusal(tmp_path: Path) -> None:
    """F1/F2: MessageHost._renew retries with a rotated NATS key when authority refuses without one."""

    from custom_transport.protocol1.host_runtime import MessageHost
    from custom_transport.protocol1.live import HostIdentity, RequestRefused

    cluster = _SignedCluster(tmp_path)
    cert1 = cluster.certs["worker-1"]
    keys1 = cluster.keys["worker-1"]
    profile = HostProfile("worker-1", (TEAM,), ("worker_host",), agent_types=("researcher",), session_limit=4)
    host = MessageHost(cluster.layout, profile, SimpleNamespace(), clock=lambda: cluster.now)
    host.live = SimpleNamespace(
        identity=HostIdentity(keys1, cert1),
        trust=cluster.trust,
    )  # type: ignore[assignment]

    seen_bodies: list[dict[str, Any]] = []

    async def fake_request(kind: str, body: dict[str, Any], _aud: Any, **_kwargs: Any) -> Any:
        seen_bodies.append(dict(body))
        if "new_nats_public_key" not in body:
            raise RequestRefused(kind, "invalid_body", "/body/new_nats_public_key")
        new_key = body["new_nats_public_key"]
        cert_doc = sign_state(
            {
                "record": "certificate",
                "protocol": PROTOCOL,
                "key": f"host.{INSTALLATION}.worker-1.2",
                "writer": {"type": "authority", "installation": INSTALLATION, "key_id": cluster.authority.key_id},
                "sequence": 2,
                "issued_at": _ts(cluster.now),
                "expires_at": _ts(cluster.now + timedelta(days=30)),
                "body": {
                    "certificate_id": "cert-worker-1-2",
                    "installation": INSTALLATION,
                    "runtime": "worker-1",
                    "teams": [TEAM],
                    "roles": ["worker_host"],
                    "agent_types": ["researcher"],
                    "session_limit": 4,
                    "connection_class": "connected",
                    "protocol": PROTOCOL,
                    "deployment": "local",
                    "application_key": {"key_id": keys1.key_id, "public_key": keys1.public_key_text},
                    "nats_public_key": new_key,
                    "membership_epoch": 2,
                    "revocation_epoch": 1,
                    "profile_sha256": profile.digest(),
                    "issued_at": _ts(cluster.now),
                    "expires_at": _ts(cluster.now + timedelta(days=30)),
                },
            },
            cluster.authority.private_key,
        )
        return SimpleNamespace(raw={"body": {"outcome": "ok", "certificate": cert_doc}})

    host.live.request = fake_request  # type: ignore[attr-defined]
    outcome = await host.renew(partitions=[0], rotate_nats_key=False)
    assert outcome.ok is True and outcome.rotated_nats_key is True
    assert len(seen_bodies) == 2
    assert "new_nats_public_key" not in seen_bodies[0]
    assert "new_nats_public_key" in seen_bodies[1]


@pytest.mark.asyncio
async def test_f8_handover_skips_exiting_record_and_preserves_handed_over_on_late_open(tmp_path: Path) -> None:
    """F8/GEN-4/FA-7/CR-8: handed_over close writes no state=exiting record, TeamAgents skips left_for_good
    for kept handed_over placements, and late-opening sessions in _hold_session close with handed_over."""

    from custom_transport.protocol1.team_agents import TeamAgent, TeamAgents, _Held

    cluster = _SignedCluster(tmp_path)
    host = _FakeMessageHost(cluster, "worker-1", credentials_issued=False)
    written_agent_states: list[str] = []
    closed_reasons: list[tuple[str, str]] = []

    orig_put = host.records.put

    async def tracking_put(record: str, key: str, document: dict[str, Any], **kwargs: Any) -> int:
        if record == "agent":
            written_agent_states.append(str(document["body"]["state"]))
        return await orig_put(record, key, document, **kwargs)

    host.records.put = tracking_put  # type: ignore[method-assign]
    process = AgentProcessHost(cluster.layout, host.profile, TEAM, host=host, loop=asyncio.get_running_loop())
    process._stop = asyncio.Event()
    process._joined = asyncio.Event()
    process._joined.set()
    process.partitions = HostPartitionTracker(host, TEAM)

    async def fake_request(kind: str, body: dict[str, Any]) -> dict[str, Any]:
        if kind == "sessions.close":
            closed_reasons.append((str(body["session"]), str(body["reason"])))
        return {"outcome": "ok"}

    process._request = fake_request  # type: ignore[method-assign]

    # 1. handed_over close must NOT write state="exiting" to UA1_AGENTS.
    a1 = draw_agent_id(TEAM, [1], PARTITIONS, prefix="wrk")
    hosted1 = _HostedAgent(SimpleNamespace(id=a1), "researcher", DEFINITION, session="s-1",
                           subscription=_FakeSubscription())
    process.agents[a1] = hosted1
    await process._close(a1, "handed_over", hosted=hosted1)
    assert written_agent_states == []
    assert closed_reasons == [("s-1", "handed_over")]

    # 2. CR-8: late-opening session in _hold_session preserves close_reason="handed_over".
    a2 = draw_agent_id(TEAM, [1], PARTITIONS, prefix="wrk")
    hosted2 = _HostedAgent(SimpleNamespace(id=a2), "researcher", DEFINITION)
    process.agents[a2] = hosted2

    async def fake_open_session(h: _HostedAgent) -> bool:
        await process._close(a2, "handed_over", hosted=h)
        h.session = "s-late-2"
        return True

    process._open_session = fake_open_session  # type: ignore[method-assign]
    await process._hold_session(hosted2)
    assert ("s-late-2", "handed_over") in closed_reasons

    # 3. TeamAgents._left_for_good_evidence returns None when placement has handed_over=True.
    ta = TeamAgents(host, TEAM)
    ta._placements[a1] = _Held(
        1,
        "d",
        SimpleNamespace(body=SimpleNamespace(handed_over=True)),  # type: ignore[arg-type]
    )
    last_agent = TeamAgent(
        agent=a1,
        agent_type="researcher",
        display_name=None,
        state="idle",
        accepting_work=True,
        since=cluster.now,
        host="worker-1",
        session="s-1",
        is_lead=False,
    )
    assert ta._left_for_good_evidence(a1, last_agent, cluster.now) is None


@pytest.mark.asyncio
async def test_f12_handover_deadline_margin_rejoin_pause_and_tracker_caught_up(tmp_path: Path) -> None:
    """F12/FA-6/CR-5/CR-6: Handover reserves a step-3 safety margin, rejoin respects renewal_paused & lock
    ordering for async on_credential, and HostPartitionTracker defers notifications until caught_up."""

    from custom_transport.protocol1.host_partitions import OwnedPartition
    from custom_transport.protocol1.host_runtime import MessageHost
    from custom_transport.protocol1.live import HostIdentity

    cluster = _SignedCluster(tmp_path)
    host_fake = _FakeMessageHost(cluster, "worker-1", credentials_issued=True)
    process = AgentProcessHost(
        cluster.layout, host_fake.profile, TEAM, host=host_fake, loop=asyncio.get_running_loop(), handover_seconds=60.0
    )
    rec = OwnedPartition(
        partition=1,
        state=RELEASED,
        host="worker-1",
        host_epoch=1,
        assigned_at=cluster.now,
        issued_at=cluster.now,
        reason="operator",
        sequence=10,
        digest="d",
    )
    deadline = process._handover_deadline([rec])
    assert deadline == cluster.now + timedelta(seconds=50.0)

    # CR-5: rejoin defers while renewal_paused() is True.
    cert1 = cluster.certs["worker-1"]
    keys1 = cluster.keys["worker-1"]
    profile = HostProfile("worker-1", (TEAM,), ("worker_host",), agent_types=("researcher",), session_limit=4)
    real_host = MessageHost(
        cluster.layout, profile, SimpleNamespace(), clock=lambda: cluster.now, renewal_paused=lambda: True
    )
    real_host.live = SimpleNamespace(identity=HostIdentity(keys1, cert1), trust=cluster.trust)  # type: ignore[assignment]
    rejoined = await real_host.rejoin(["analyst"])
    assert rejoined.granted == frozenset() and rejoined.refused == frozenset({"analyst"})
    assert rejoined.refusal and "handover" in rejoined.refusal

    # CR-6: HostPartitionTracker.run holds notifications and has_usable until caught_up fires.
    k0, raw0 = cluster.sign_partition_owner(0, state=OWNED, host="worker-1", sequence=100)
    k1, raw1 = cluster.sign_partition_owner(1, state=RELEASED, host="worker-1", sequence=101)
    proceed_catchup = asyncio.Event()
    notifications: list[tuple[bool, frozenset[int], set[int]]] = []

    async def gated_watch(record: str, pattern: str, *, team: Optional[str] = None, caught_up: Optional[asyncio.Event] = None):
        yield k0, raw0
        await proceed_catchup.wait()
        yield k1, raw1
        if caught_up is not None:
            caught_up.set()
        await asyncio.Future()

    host_watch = _FakeMessageHost(cluster, "worker-1", credentials_issued=False)
    host_watch.records.watch = gated_watch  # type: ignore[attr-defined]
    tracker = HostPartitionTracker(host_watch, TEAM)
    tracker.on_change(
        lambda: notifications.append((tracker.ready.is_set(), tracker.owned_by_host, set(tracker.released_by_host)))
    )
    stop = asyncio.Event()
    task = asyncio.create_task(tracker.run(stop))
    try:
        await asyncio.sleep(0.03)
        assert notifications == []
        assert not tracker.has_usable.is_set()
        assert not tracker.ready.is_set()
        proceed_catchup.set()
        await asyncio.wait_for(tracker.ready.wait(), timeout=1.0)
        assert len(notifications) == 1
        assert notifications[0][1] == frozenset({0}) and notifications[0][2] == {1}
        assert tracker.has_usable.is_set()
    finally:
        stop.set()
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)


@pytest.mark.asyncio
async def test_c36_host_epoch_bump_at_same_assigned_at_reruns_step3(tmp_path: Path) -> None:
    """C36: Updating a RELEASED record from host_epoch=1 to host_epoch=2 at unchanged assigned_at
    (from a C33 post-write Dekker sweep) clears the prior completion match, keeps _is_renewal_paused()
    true, and drives a fresh step-3 renewal."""

    cluster = _SignedCluster(tmp_path)
    host = _FakeMessageHost(cluster, "worker-1", credentials_issued=True)
    host.credential_partitions = frozenset({0, 1})
    process = AgentProcessHost(
        cluster.layout, host.profile, TEAM, host=host, loop=asyncio.get_running_loop(), drain_poll_seconds=0.01
    )
    process._stop = asyncio.Event()
    process._joined = asyncio.Event()
    process._joined.set()
    process._partition_wake = asyncio.Event()
    process.partitions = HostPartitionTracker(host, TEAM)

    k0, raw0 = cluster.sign_partition_owner(0, state=OWNED, host="worker-1", host_epoch=1, sequence=100)
    await process.partitions.apply_event(k0, raw0)
    t0 = cluster.now
    k1, raw1 = cluster.sign_partition_owner(
        1, state=RELEASED, host="worker-1", host_epoch=1, assigned_at=t0, reason="operator", sequence=101
    )
    await process.partitions.apply_event(k1, raw1)
    assert process._is_renewal_paused() is True

    # First step-3 completes for (t0, host_epoch=1).
    assert await process._sync_partitions_once() is False
    assert len(host.renew_calls) == 1
    assert host.renew_calls[0][1] is True
    assert process._completed_handovers[1] == (t0, 1)
    assert process._is_renewal_paused() is False

    # Post-write Dekker sweep bumps RELEASED.host_epoch from 1 -> 2 at the exact same assigned_at=t0.
    k1_swept, raw1_swept = cluster.sign_partition_owner(
        1, state=RELEASED, host="worker-1", host_epoch=2, assigned_at=t0, reason="operator", sequence=102
    )
    await process.partitions.apply_event(k1_swept, raw1_swept)
    assert process._is_renewal_paused() is True

    # Coordinator clears (t0, 1), runs a fresh step-3 renewal, and records (t0, 2).
    assert await process._sync_partitions_once() is False
    assert len(host.renew_calls) == 2
    assert host.renew_calls[1][1] is True
    assert process._completed_handovers[1] == (t0, 2)
    assert process._is_renewal_paused() is False


from tests.test_schedule_worker_service import lead  # noqa: E402, F401


@pytest.mark.asyncio
async def test_f14_agent_instantiation_service_and_universal_agent_partition_id_wiring(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, lead: Any  # noqa: F811
) -> None:
    """F14/EVA-05: Real AgentInstantiationService.launch and real UniversalAgent.__init__ /
    assign_partition_agent_id draw partition-scoped IDs inside owned partitions and keep
    auto-generated IDs out of UniversalAgent.model_fields_set."""

    from custom_agents.agent_instantiation_service import AgentInstantiationService
    from custom_agents.strands_agent import UniversalAgent

    cluster = _SignedCluster(tmp_path)
    host = _FakeMessageHost(cluster, "worker-1", credentials_issued=False)
    process = AgentProcessHost(cluster.layout, host.profile, TEAM, host=host, loop=asyncio.get_running_loop())
    process._joined = asyncio.Event()
    process._joined.set()
    process._stop = asyncio.Event()
    process.partitions = HostPartitionTracker(host, TEAM)
    owned = frozenset({2, 5})
    for p in owned:
        k, raw = cluster.sign_partition_owner(p, state=OWNED, host="worker-1")
        await process.partitions.apply_event(k, raw)

    # 1. Real AgentInstantiationService.launch draws a partition-scoped worker ID inside owned partitions.
    lead.team_id = TEAM
    configure_process_host(process)
    try:
        launched = AgentInstantiationService(lead).launch(
            agent_type="Worker", owner_id="owner-1", request_id="f14-launch-1", source="manual"
        )
        assert launched.status in {"starting", "started"}
        assert launched.agent_id is not None
        assert len(launched.agent_id) <= 32 and AGENT_ID_PATTERN.fullmatch(launched.agent_id)
        assert agent_partition(TEAM, launched.agent_id, PARTITIONS) in owned

        # 2. Real UniversalAgent.__init__ auto_partition_id draws an owned-partition ID and keeps "id"
        #    out of model_fields_set (so consumer scope stays PROCESS, not operator-pinned ROUTE).
        agent = UniversalAgent(
            role="partition wiring check",
            tools=[],
            team_id=TEAM,
            workspace_dir=str(tmp_path / "ws1"),
        )
        assert len(agent.id) <= 32 and AGENT_ID_PATTERN.fullmatch(agent.id)
        assert agent_partition(TEAM, agent.id, PARTITIONS) in owned
        assert "id" not in agent.model_fields_set

        # 3. When UniversalAgent is constructed before partitions are usable, assign_partition_agent_id
        #    (called by UniversalAgent._connect_nats) adopts an owned-partition ID on the real agent.
        configure_process_host(None)
        early_agent = UniversalAgent(
            role="late partition wiring check",
            tools=[],
            team_id=TEAM,
            workspace_dir=str(tmp_path / "ws2"),
        )
        assert "id" not in early_agent.model_fields_set
        configure_process_host(process)
        # Ensure early_agent starts in a non-owned partition so assign_partition_agent_id must adopt a new ID.
        if agent_partition(TEAM, early_agent.id, PARTITIONS) in owned:
            other_p = next(p for p in range(PARTITIONS) if p not in owned)
            early_agent._adopt_agent_id(draw_agent_id(TEAM, [other_p], PARTITIONS, prefix="agent"))
            early_agent.model_fields_set.discard("id")
        fake_adapter = SimpleNamespace(nats_connection=SimpleNamespace())
        monkeypatch.setattr(process, "ensure_started", lambda nc: None)
        await assign_partition_agent_id(early_agent, fake_adapter)
        assert agent_partition(TEAM, early_agent.id, PARTITIONS) in owned
        assert "id" not in early_agent.model_fields_set
    finally:
        configure_process_host(None)
