"""The installation authority's rules that need no NATS server.

installation_authority.service (the launcher's list at join and renewal, reuse),
installation_authority.ledger (what the authority keeps on disk),
installation_authority.expected_hosts, installation_authority.process (how it exits),
and custom_messages.wire.trust.kept_for. The authority on a real server is in
tests/test_live_installation_authority.py.
"""

from __future__ import annotations

import asyncio
import dataclasses
import json
import logging
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat
from nats.js.errors import KeyNotFoundError, NotFoundError

from custom_messages.wire.encoding import b64url, format_timestamp
from custom_messages.wire.trust import kept_for
from custom_messages.wire.values import parse_timestamp
from custom_transport.protocol1 import host_keys
from custom_transport.protocol1.bootstrap import BootstrapBundle, read_bundle, write_bundle
from custom_transport.protocol1.bucket_reads import BucketReadTimedOut
from custom_transport.protocol1.live import utc_now
from installation_authority import process as authority_process
from installation_authority import service as authority_service
from installation_authority.expected_hosts import ExpectedHost, read_expected_hosts, write_expected_hosts
from installation_authority.ledger import ISSUED_FILENAME, OUTCOME_RETENTION, IssueLedger
from installation_authority.service import (
    AUTHORITY_RUNTIME,
    AuthorityDisconnected,
    CertificateRequest,
    InstallationAuthority,
    PartitionCountMismatch,
    RevocationsUnverified,
    _denial,
    _renewal_refusal,
)
from runtime_settings.instance_layout import InstanceLayout
from runtime_settings.transport import QueueSettings
from custom_transport.protocol1.contract_files import transport
from tests.queue_limits_support import TEST_QUEUE_LIMITS

LISTED = ExpectedHost("worker-host-1", "worker.key1", "PUBLIC1", frozenset({"worker_host"}), frozenset({"team_one"}),
                      frozenset({"researcher"}), 4)


def join_body(**changes: Any) -> dict[str, Any]:
    body: dict[str, Any] = {"runtime": "worker-host-1", "application_key": {"key_id": "worker.key1", "public_key": "PUBLIC1"},
                            "teams": ["team_one"], "roles": ["worker_host"], "agent_types": ["researcher"],
                            "session_limit": 4, "connection_class": "connected", "deployment": "local"}
    body.update(changes)
    return body


@pytest.mark.parametrize(("changes", "reason"), [
    ({}, None),
    ({"application_key": {"key_id": "worker.key2", "public_key": "PUBLIC2"}}, "key_mismatch"),
    ({"application_key": {"key_id": "worker.key1", "public_key": "PUBLIC2"}}, "key_mismatch"),
    ({"teams": ["team_one", "team_two"]}, "team_not_allowed"),
    ({"roles": ["worker_host", "lead_host"]}, "role_not_allowed"),
    ({"agent_types": ["researcher", "analyst"]}, "role_not_allowed"),
    ({"session_limit": 5}, "over_limit"),
    ({"connection_class": "intermittent"}, "not_approved"),
    ({"deployment": "third_party"}, "not_approved"),
    # Asking for less than listed is approved.
    ({"agent_types": [], "session_limit": 0}, None),
])
def test_a_join_gets_no_more_than_the_launcher_listed(changes: dict[str, Any], reason: str | None) -> None:
    assert _denial(LISTED, join_body(**changes), LISTED.agent_types) == reason


def test_every_runtime_but_the_gateway_serves_one_team_even_when_listed_for_more() -> None:
    """transport.yaml single_team_roles: every role but the gateway and the authority serves one team per runtime."""

    teams = frozenset({"team_one", "team_two"})
    for role in ("worker_host", "lead_host", "team_services", "edge_adapter", "scheduler"):
        listed = ExpectedHost("host-1", "worker.key1", "PUBLIC1", frozenset({role}), teams, frozenset({"researcher"}), 4)
        body = join_body(runtime="host-1", roles=[role])
        assert _denial(listed, dict(body, teams=["team_one", "team_two"]), listed.agent_types) == "team_not_allowed"
        assert _denial(listed, dict(body, teams=["team_two"]), listed.agent_types) is None
    gateway = ExpectedHost("gateway-1", "worker.key1", "PUBLIC1", frozenset({"gateway"}), teams, frozenset(), 0)
    body = join_body(runtime="gateway-1", roles=["gateway"], agent_types=[], session_limit=0)
    assert _denial(gateway, dict(body, teams=["team_one", "team_two"]), frozenset()) is None


def test_a_host_nobody_listed_is_not_approved() -> None:
    assert _denial(None, join_body(), frozenset()) == "not_approved"


def certificate(**changes: Any) -> Any:
    values: dict[str, Any] = {"runtime": "worker-host-1", "teams": frozenset({"team_one"}),
                              "roles": frozenset({"worker_host"}), "agent_types": frozenset({"researcher"}),
                              "session_limit": 4, "connection_class": "connected"}
    values.update(changes)
    return SimpleNamespace(**values)


KEPT_KEY = {"key_id": "worker.key1", "public_key": "PUBLIC1"}


def test_a_renewal_is_checked_against_the_launchers_list() -> None:
    assert _renewal_refusal(LISTED, certificate(), KEPT_KEY, LISTED.agent_types) is None
    assert _renewal_refusal(LISTED, certificate(agent_types=frozenset()), KEPT_KEY, LISTED.agent_types) is None
    assert "no longer lists" in str(_renewal_refusal(None, certificate(), KEPT_KEY, frozenset()))
    assert "another key" in str(_renewal_refusal(LISTED, certificate(), {"key_id": "worker.key2",
                                                                           "public_key": "PUBLIC2"}, LISTED.agent_types))
    for wider in ({"agent_types": frozenset({"researcher", "analyst"})}, {"session_limit": 5},
                  {"teams": frozenset({"team_one", "team_two"})}, {"roles": frozenset({"worker_host", "lead_host"})},
                  {"connection_class": "intermittent"}):
        assert "join again" in str(_renewal_refusal(LISTED, certificate(**wider), KEPT_KEY, LISTED.agent_types)), wider


def test_a_certificate_that_names_two_teams_for_a_one_team_role_is_not_renewed() -> None:
    """A certificate issued before the one-team rule, or from a hand-edited list, must join again (C41)."""

    teams = frozenset({"team_one", "team_two"})
    for role in ("worker_host", "lead_host", "team_services", "edge_adapter", "scheduler"):
        listed = ExpectedHost("host-1", "worker.key1", "PUBLIC1", frozenset({role}), teams, frozenset({"researcher"}), 4)
        refusal = _renewal_refusal(listed, certificate(roles=frozenset({role}), teams=teams), KEPT_KEY,
                                   listed.agent_types)
        assert "join again, once for each team" in str(refusal), role
        assert _renewal_refusal(listed, certificate(roles=frozenset({role})), KEPT_KEY, listed.agent_types) is None
    gateway = ExpectedHost("gateway-1", "worker.key1", "PUBLIC1", frozenset({"gateway"}), teams, frozenset(), 0)
    assert _renewal_refusal(gateway, certificate(roles=frozenset({"gateway"}), teams=teams, agent_types=frozenset(),
                                                 session_limit=0), KEPT_KEY, frozenset()) is None


def test_the_list_keeps_connection_class_and_deployment(tmp_path: Path) -> None:
    path = tmp_path / "expected-hosts.json"
    edge = ExpectedHost("edge-1", "edge.key", "PUBLIC", frozenset({"edge_adapter"}), frozenset({"team_one"}),
                        connection_class="intermittent", deployment="operator_managed")
    write_expected_hosts(path, {"edge-1": edge, LISTED.runtime: LISTED})
    hosts = read_expected_hosts(path)
    assert hosts == {"edge-1": edge, LISTED.runtime: LISTED}
    assert (hosts[LISTED.runtime].connection_class, hosts[LISTED.runtime].deployment) == ("connected", "local")
    # A list written before these fields existed reads as the launcher's own processes.
    document = json.loads(path.read_text())
    for entry in document["hosts"].values():
        del entry["connection_class"], entry["deployment"]
    path.write_text(json.dumps(document))
    assert read_expected_hosts(path)["edge-1"].connection_class == "connected"


def test_a_certificate_states_the_profile_it_grants() -> None:
    request = CertificateRequest("worker-host-1", ("team_one",), ("worker_host",), ("researcher",), 4, "connected",
                                 "worker.key1", "PUBLIC1", "NATS1")
    narrower = CertificateRequest("worker-host-1", ("team_one",), ("worker_host",), (), 0, "connected",
                                  "worker.key1", "PUBLIC1", "NATS1")
    assert request.profile_sha256() != narrower.profile_sha256()


# -- the issue record ------------------------------------------------------------------------------


def test_the_issue_record_keeps_epochs_renewal_answers_and_revocations(tmp_path: Path) -> None:
    now = utc_now()
    ledger = IssueLedger.open(tmp_path, "install_one")
    assert ledger.highest_epoch("worker-host-1") == 0 and ledger.revocations is None
    ledger.take_epoch("worker-host-1", 3)
    ledger.take_epoch("worker-host-1", 2)  # never lowers
    ledger.store_renewal("worker-host-1|renew-1", {"key": "host.install_one.worker-host-1.3"}, "digest", now)
    ledger.keep_revocations({"record": "revocations", "sequence": 9})
    ledger.save(now)
    assert (tmp_path / ISSUED_FILENAME).stat().st_mode & 0o777 == 0o600

    again = IssueLedger.open(tmp_path, "install_one")
    assert again.highest_epoch("worker-host-1") == 3
    stored = again.renewal("worker-host-1|renew-1", now)
    assert stored is not None and stored.body_sha256 == "digest"
    assert again.revocations == {"record": "revocations", "sequence": 9}
    # README "Stored outcomes": kept 7 days, then dropped at the next save.
    later = now + OUTCOME_RETENTION
    assert again.renewal("worker-host-1|renew-1", later) is None
    again.save(later)
    assert IssueLedger.open(tmp_path, "install_one").renewal("worker-host-1|renew-1", now) is None


def test_an_issue_record_of_another_installation_or_unreadable_is_not_used(tmp_path: Path, caplog) -> None:
    now = utc_now()
    ledger = IssueLedger.open(tmp_path, "install_one")
    ledger.take_epoch("worker-host-1", 5)
    ledger.save(now)
    assert IssueLedger.open(tmp_path, "install_two").highest_epoch("worker-host-1") == 0
    assert "event=authority_ledger_other_installation" in caplog.text

    (tmp_path / ISSUED_FILENAME).write_text("{not json")
    assert IssueLedger.open(tmp_path, "install_one").highest_epoch("worker-host-1") == 0
    assert "event=authority_ledger_unreadable" in caplog.text
    # The damaged file is kept for an operator, not overwritten by the next save.
    assert [path.name for path in tmp_path.iterdir() if path.name.startswith(f"{ISSUED_FILENAME}.unreadable-")]


def test_receivers_and_writers_forget_a_certificate_at_the_same_time() -> None:
    kept = kept_for("certificate")
    assert kept is not None and kept > timedelta(days=90)
    assert kept_for("revocations") is None


# -- the authority without a server ----------------------------------------------------------------


def authority_at(tmp_path: Path, *, partitions: int = 96) -> InstallationAuthority:
    layout = InstanceLayout.at(tmp_path / "instance")
    authority, _ = host_keys.ensure_authority_keys(layout, "install_one")
    keys, _ = host_keys.ensure_host_keys(layout, AUTHORITY_RUNTIME)
    return InstallationAuthority(layout, authority, keys, partitions=partitions, queues=TEST_QUEUE_LIMITS)


class _NoServer:
    """A NATS connection the authority must not touch: the count check comes before anything else."""

    def __getattr__(self, name: str) -> Any:
        raise AssertionError(f"the authority used its NATS connection ({name}) before refusing")


def test_the_authority_refuses_to_start_when_the_bundles_partition_count_is_not_its_own(tmp_path: Path) -> None:
    """Hosts compute agents' partitions with the bundle's count, the authority with its setting."""

    authority = authority_at(tmp_path, partitions=96)
    path = authority.layout.trust_bootstrap_path
    write_bundle(path, BootstrapBundle("install_one", ((authority.authority.key_id, authority.authority.public_key_text),),
                                       partitions=7))
    before = path.read_bytes()

    with pytest.raises(PartitionCountMismatch) as refused:
        asyncio.run(authority.start(_NoServer(), teams=["team_one"]))

    assert "holds partition count 7, but this authority's transport.queues.partitions is 96" in str(refused.value)
    assert "restart the instance" in str(refused.value)
    assert path.read_bytes() == before
    assert not (authority.layout.host_trust_dir(AUTHORITY_RUNTIME) / ISSUED_FILENAME).exists()


def test_the_authority_keeps_the_bundles_partition_count_when_it_writes_its_records(tmp_path: Path) -> None:
    authority = authority_at(tmp_path, partitions=7)
    path = authority.layout.trust_bootstrap_path
    write_bundle(path, BootstrapBundle("install_one", ((authority.authority.key_id, authority.authority.public_key_text),),
                                       partitions=7))
    authority._own, authority._revocations = {"record": "certificate"}, {"record": "revocations"}

    authority._publish_bundle()

    assert read_bundle(path).partitions == 7
    assert [record["record"] for record in read_bundle(path).records] == ["certificate", "revocations"]


def test_a_stored_certificate_is_handed_back_only_for_the_same_keys_while_newest(tmp_path: Path) -> None:
    authority = authority_at(tmp_path)
    now = utc_now()
    request = CertificateRequest("worker-host-1", ("team_one",), ("worker_host",), ("researcher",), 4, "connected",
                                 "worker.key1", "PUBLIC1", "NATS1")
    record = authority.certificate_record(request, epoch=2, revocation_epoch=1, issued=now)
    assert authority._reusable(record, "worker.key1", "PUBLIC1", "NATS1", now)
    # Another NATS key: the host would refuse the certificate (README "Replies").
    assert not authority._reusable(record, "worker.key1", "PUBLIC1", "NATS2", now)
    # Its last third: a new one.
    assert not authority._reusable(record, "worker.key1", "PUBLIC1", "NATS1", now + timedelta(days=21))
    # A later epoch was issued: this one was replaced.
    authority._ledger.take_epoch("worker-host-1", 3)
    assert not authority._reusable(record, "worker.key1", "PUBLIC1", "NATS1", now)


class _Closed:
    is_closed = True


def test_upkeep_stops_once_the_nats_connection_closed_for_good(tmp_path: Path) -> None:
    authority = authority_at(tmp_path)
    authority._nc = _Closed()
    with pytest.raises(AuthorityDisconnected):
        asyncio.run(authority.upkeep(utc_now()))


def test_a_failed_upkeep_step_is_logged_and_tried_again(tmp_path: Path, caplog) -> None:
    authority = authority_at(tmp_path)
    attempts: list[object] = []

    async def failing(now):
        attempts.append(now)
        raise RuntimeError("bucket unavailable")

    async def forget(now):
        authority._forgotten_at = now

    authority._sign_revocations = failing  # type: ignore[method-assign]
    authority.forget = forget  # type: ignore[method-assign]
    now = utc_now()
    asyncio.run(authority.upkeep(now))
    asyncio.run(authority.upkeep(now + timedelta(minutes=1)))
    assert len(attempts) == 2
    assert caplog.text.count("it is tried again in 60 seconds. event=revocations_resign_failed") == 2


# -- a listing of UA1_CERTS that fails (bucket_reads.list_keys) ------------------------------------
# A failed listing is never read as an empty bucket: the authority does not start, issues no
# certificate and deletes none.


def failing_listings(monkeypatch: pytest.MonkeyPatch, *, fail_from: int = 1) -> list[str]:
    """The authority's listings raise from call ``fail_from`` on (earlier ones find nothing); returns the prefixes asked."""

    asked: list[str] = []

    async def list_keys(_js: Any, bucket: str, prefix: str = "") -> list[str]:
        asked.append(prefix)
        if len(asked) >= fail_from:
            raise BucketReadTimedOut(bucket, f"$KV.{bucket}.{prefix}>", [f"{prefix}1"], 0)
        return []

    monkeypatch.setattr(authority_service, "list_keys", list_keys)
    return asked


def issued_by(authority: InstallationAuthority) -> list[Any]:
    """The certificate requests the authority signs from now on (recorded, not signed)."""

    issued: list[Any] = []

    async def issue(request: Any, **_options: Any) -> dict[str, Any]:
        issued.append(request)
        return {}

    authority._issue = issue  # type: ignore[method-assign]
    authority._revocations = {"body": {"revocation_epoch": 1}}
    return issued


class _Bucket:
    """UA1_CERTS without a server: values by key, and the keys deleted."""

    def __init__(self) -> None:
        self.values: dict[str, bytes] = {}
        self.deleted: list[str] = []

    async def get(self, key: str) -> Any:
        if key not in self.values:
            raise KeyNotFoundError
        return SimpleNamespace(value=self.values[key])

    async def put(self, key: str, value: bytes) -> None:
        self.values[key] = value

    async def delete(self, key: str) -> None:
        self.deleted.append(key)


class _JetStream:
    def __init__(self) -> None:
        self.buckets: dict[str, _Bucket] = {}

    async def stream_info(self, _name: str) -> Any:
        raise NotFoundError

    async def add_stream(self, config: Any) -> None:
        pass

    async def key_value(self, bucket: str) -> _Bucket:
        return self.buckets.setdefault(bucket, _Bucket())


class _Connection:
    is_closed = False

    def __init__(self) -> None:
        self.js = _JetStream()
        self.subscribed: list[str] = []

    def jetstream(self) -> _JetStream:
        return self.js

    async def subscribe(self, subject: str, **_options: Any) -> None:
        self.subscribed.append(subject)

    async def flush(self) -> None:
        pass


@pytest.mark.parametrize("fail_from", [1, 2])  # its own newest certificate; the certificates it issued
def test_an_authority_whose_certificate_listing_fails_does_not_start(tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
                                                                     fail_from: int) -> None:
    authority = authority_at(tmp_path)
    write_bundle(authority.layout.trust_bootstrap_path,
                 BootstrapBundle("install_one", ((authority.authority.key_id, authority.authority.public_key_text),),
                                 partitions=authority.partitions))

    async def nothing(*_args: Any, **_options: Any) -> None:
        return None

    # The partition mapping check and the teams' streams come before the listings and need a
    # real server; their own tests cover them (test_partition_mapping_check, the live queue tests).
    monkeypatch.setattr(authority_service, "check_partition_mapping", nothing)
    monkeypatch.setattr(authority, "ensure_topology", nothing)
    asked = failing_listings(monkeypatch, fail_from=fail_from)
    nc = _Connection()
    with pytest.raises(BucketReadTimedOut):
        asyncio.run(authority.start(nc, teams=["team_one"]))
    assert asked == [f"host.install_one.{AUTHORITY_RUNTIME}.", "host.install_one."][:fail_from]
    assert nc.subscribed == []  # it never answered joins


def test_a_join_whose_certificate_listing_fails_gets_no_certificate(tmp_path: Path,
                                                                    monkeypatch: pytest.MonkeyPatch) -> None:
    authority = authority_at(tmp_path)
    write_expected_hosts(authority.layout.trust_expected_hosts_path, {LISTED.runtime: LISTED})
    failing_listings(monkeypatch)
    issued = issued_by(authority)
    verified = SimpleNamespace(raw={"body": join_body(nats_public_key="NATS1", profile_sha256="")})
    with pytest.raises(BucketReadTimedOut):
        asyncio.run(authority.answer_join(verified))  # type: ignore[arg-type]
    assert issued == []


def test_a_renewal_whose_certificate_listing_fails_gets_no_certificate(tmp_path: Path,
                                                                       monkeypatch: pytest.MonkeyPatch) -> None:
    authority = authority_at(tmp_path)
    public_key = Ed25519PrivateKey.generate().public_key()
    listed = dataclasses.replace(LISTED, public_key=b64url(public_key.public_bytes(Encoding.Raw, PublicFormat.Raw)))
    write_expected_hosts(authority.layout.trust_expected_hosts_path, {listed.runtime: listed})
    failing_listings(monkeypatch)
    issued = issued_by(authority)
    authority.live = SimpleNamespace()  # type: ignore[assignment]  # serving: renewals reach it only then
    held = certificate(key_id=listed.key_id, public_key=public_key, certificate_id="certificate-1", membership_epoch=1,
                       nats_public_key="NATS1")
    verified = SimpleNamespace(certificate=held, frame=SimpleNamespace(idempotency_key="renew-1"),
                               raw={"body": {"profile_sha256": ""}})
    with pytest.raises(BucketReadTimedOut):
        asyncio.run(authority.answer_renew(verified))  # type: ignore[arg-type]
    assert issued == []


def test_a_failed_listing_deletes_no_certificate_and_the_forgetting_runs_again_the_next_day(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture) -> None:
    authority = authority_at(tmp_path)
    asked = failing_listings(monkeypatch)
    nc = _Connection()
    authority._nc, authority._js = nc, nc.js
    authority._trust = SimpleNamespace(store=SimpleNamespace(forget=lambda _now: 0))  # type: ignore[assignment]
    now = utc_now()
    authority._revocations = {"issued_at": format_timestamp(now)}  # signed just now: only the forgetting is due
    asyncio.run(authority.upkeep(now))
    asyncio.run(authority.upkeep(now + timedelta(minutes=1)))
    assert asked == ["host.install_one."]
    assert nc.js.buckets["UA1_CERTS"].deleted == []
    assert "it is tried again in 24 hours. event=authority_forget_failed" in caplog.text
    later = now + timedelta(days=1)
    authority._revocations = {"issued_at": format_timestamp(later)}
    asyncio.run(authority.upkeep(later))
    assert len(asked) == 2


# -- the process -----------------------------------------------------------------------------------


class _Adapter:
    def __init__(self, **_kwargs: Any) -> None:
        self.nats_connection = object()
        self.closed = False

    async def connect(self) -> None:
        pass

    async def close(self) -> None:
        self.closed = True


def _config(tmp_path: Path) -> Any:
    return SimpleNamespace(layout=InstanceLayout.at(tmp_path), instance_name="test",
                           manager=SimpleNamespace(team_id="team_one"),
                           transport=SimpleNamespace(replicas=3, nats_servers=["nats://127.0.0.1:1"], nats_url=None,
                                                     subject_prefix="team", nats_user=None, nats_password=None,
                                                     credentials_file=None,
                                                     queues=QueueSettings(partitions=7, max_bytes={"pool": 5 << 20})))


def _run_with(monkeypatch, tmp_path: Path, authority: Any, made: dict[str, Any] | None = None) -> None:
    def make(*_args: Any, **kwargs: Any) -> Any:
        (made if made is not None else {}).update(kwargs)
        return authority

    monkeypatch.setattr(authority_process, "NatsTransportAdapter", _Adapter)
    monkeypatch.setattr(authority_process, "InstallationAuthority", make)
    monkeypatch.setattr(authority_process, "load_authority_keys", lambda _layout: None)
    monkeypatch.setattr(authority_process, "load_host_keys", lambda _layout, _runtime: None)
    asyncio.run(authority_process.run_installation_authority(_config(tmp_path)))


def test_the_process_exits_with_a_reason_when_it_cannot_start_safely(tmp_path: Path, monkeypatch, caplog) -> None:
    class Refusing:
        async def start(self, _nc: Any, *, teams: Any) -> None:
            raise RevocationsUnverified("Restore the instance's trust folder.")

    caplog.set_level(logging.ERROR)
    made: dict[str, Any] = {}
    with pytest.raises(SystemExit) as exited:
        _run_with(monkeypatch, tmp_path, Refusing(), made)
    assert exited.value.code == 1
    assert "event=installation_authority_start_refused reason=RevocationsUnverified" in caplog.text
    assert "Restore the instance's trust folder." in caplog.text
    assert made["partitions"] == 7  # the saved transport.queues.partitions
    # The streams' limits are the saved transport.queues and transport.replicas.
    queues = made["queues"]
    assert (queues.max_bytes["pool"], queues.max_bytes["partition"], queues.max_messages_per_subject, queues.replicas) \
        == (5 << 20, QueueSettings().max_bytes.partition, QueueSettings().max_messages_per_subject, 3)
    assert set(queues.max_bytes) == set(transport()["queues"]["size_kinds"])


def test_the_process_exits_when_its_upkeep_stops(tmp_path: Path, monkeypatch, caplog) -> None:
    class Disconnecting:
        async def start(self, _nc: Any, *, teams: Any) -> None:
            pass

        async def keep_current(self) -> None:
            raise AuthorityDisconnected("closed")

        async def follow_placements(self, stop: asyncio.Event) -> None:
            await stop.wait()

    with pytest.raises(SystemExit) as exited:
        _run_with(monkeypatch, tmp_path, Disconnecting())
    assert exited.value.code == 1
    assert "event=installation_authority_upkeep_stopped" in caplog.text


def test_the_process_exits_when_it_stops_following_placements(tmp_path: Path, monkeypatch, caplog) -> None:
    """It opens failed watches again itself, so ending before the stop is an error of its own."""

    class LosingPlacements:
        async def start(self, _nc: Any, *, teams: Any) -> None:
            pass

        async def keep_current(self) -> None:
            await asyncio.Event().wait()

        async def follow_placements(self, stop: asyncio.Event) -> None:
            raise RuntimeError("the consumers could not be listed")

    with pytest.raises(SystemExit) as exited:
        _run_with(monkeypatch, tmp_path, LosingPlacements())
    assert exited.value.code == 1
    assert "event=installation_authority_placements_stopped error=the consumers could not be listed" in caplog.text


@pytest.mark.parametrize("changed", ["key_id", "public_key", "expired", "absent"])
def test_held_agent_types_require_the_same_live_certificate_key(changed) -> None:
    from installation_authority.service import _held_agent_types
    from custom_messages.wire.encoding import format_timestamp

    now = utc_now()
    stored = {"body": {"application_key": dict(KEPT_KEY), "agent_types": ["Researcher"],
                        "expires_at": format_timestamp(now + timedelta(days=1))}}
    assert _held_agent_types(stored, KEPT_KEY, now) == frozenset({"Researcher"})
    key = dict(KEPT_KEY)
    if changed in key:
        key[changed] = "OTHER"
    elif changed == "expired":
        stored["body"]["expires_at"] = format_timestamp(now)
    else:
        stored = None
    assert not _held_agent_types(stored, key, now)


def test_expected_hosts_persists_gateway_edges_and_bootstrap_key(tmp_path: Path) -> None:
    from custom_transport.protocol1.nats_jwt import NatsSigningKey

    path = tmp_path / "expected-hosts.json"
    boot = NatsSigningKey.generate("user").public_key
    gw = ExpectedHost(
        "gateway-east",
        "gw.key1",
        "PUBLIC1",
        frozenset({"gateway"}),
        frozenset({"team_a", "team_b"}),
        edges={"team_a": ("web_a", "tg_a"), "team_b": ("web_b",)},
        bootstrap_nats_public_key=boot,
    )
    write_expected_hosts(path, {gw.runtime: gw})
    loaded = read_expected_hosts(path)[gw.runtime]
    assert loaded.edges == {"team_a": ("tg_a", "web_a"), "team_b": ("web_b",)}
    assert loaded.bootstrap_nats_public_key == boot


def test_denial_and_renewal_refusal_enforce_known_teams_and_gateway_edges() -> None:
    """FA-6 and C07: refuse teams without streams and misconfigured edges outside expected teams."""

    gw = ExpectedHost(
        "gateway-1",
        "worker.key1",
        "PUBLIC1",
        frozenset({"gateway"}),
        frozenset({"team_one", "team_two"}),
        edges={"team_one": ("web",)},
    )
    body = join_body(runtime="gateway-1", roles=["gateway"], teams=["team_one", "team_two"], agent_types=[], session_limit=0)
    assert _denial(gw, body, frozenset(), known_teams=frozenset({"team_one"})) == "team_not_allowed"
    assert _denial(gw, body, frozenset(), known_teams=frozenset({"team_one", "team_two"})) is None

    gw_cert = certificate(
        runtime="gateway-1",
        roles=frozenset({"gateway"}),
        teams=frozenset({"team_one", "team_two"}),
        agent_types=frozenset(),
        session_limit=0,
    )
    assert "team_not_allowed" in str(
        _renewal_refusal(gw, gw_cert, KEPT_KEY, frozenset(), known_teams=frozenset({"team_one"}))
    )
    assert _renewal_refusal(gw, gw_cert, KEPT_KEY, frozenset(), known_teams=frozenset({"team_one", "team_two"})) is None

    bad_edges = ExpectedHost(
        "gateway-1",
        "worker.key1",
        "PUBLIC1",
        frozenset({"gateway"}),
        frozenset({"team_one"}),
        edges={"team_other": ("web",)},
    )
    body_one = join_body(runtime="gateway-1", roles=["gateway"], teams=["team_one"], agent_types=[], session_limit=0)
    assert _denial(bad_edges, body_one, frozenset()) == "team_not_allowed"
    assert "gateway routes" in str(
        _renewal_refusal(
            bad_edges,
            certificate(runtime="gateway-1", roles=frozenset({"gateway"}), agent_types=frozenset(), session_limit=0),
            KEPT_KEY,
            frozenset(),
        )
    )


def test_ledger_persists_nats_revocations_bootstrap_keys_and_renewal_connection(tmp_path: Path) -> None:
    from custom_transport.protocol1.nats_jwt import NatsSigningKey

    now = utc_now()
    k1 = NatsSigningKey.generate("user").public_key
    k2 = NatsSigningKey.generate("user").public_key
    boot = NatsSigningKey.generate("user").public_key
    ledger = IssueLedger.open(tmp_path, "install_one")
    ledger.record_revoked_nats_key(k1, 1000)
    ledger.record_confirmed_nats_revocation(k2, 2000)
    ledger.record_bootstrap_nats_key(boot)
    ledger.store_renewal(
        "host-1|renew-1",
        {"key": "host.install_one.host-1.2"},
        "digest",
        now,
        partitions=(3, 1),
        connection={"type": "nats_user_jwt", "jwt": "a.b.c", "expires_at": format_timestamp(now)},
    )
    ledger.save(now)

    reopened = IssueLedger.open(tmp_path, "install_one")
    assert reopened.is_nats_key_revoked(k1) and not reopened.is_nats_revocation_confirmed(k1)
    assert reopened.is_nats_key_revoked(k2) and reopened.is_nats_revocation_confirmed(k2)
    assert reopened.is_bootstrap_nats_key(boot)
    stored = reopened.renewal("host-1|renew-1", now)
    assert stored is not None
    assert stored.partitions == (1, 3)
    assert stored.connection == {"type": "nats_user_jwt", "jwt": "a.b.c", "expires_at": format_timestamp(now)}


def test_host_keys_stage_promote_and_discard_pending_nats_key(tmp_path: Path) -> None:
    """C03: atomic staging reuses the same pending NATS key across retries and promotes cleanly."""

    layout = InstanceLayout.at(tmp_path / "instance")
    original, _ = host_keys.ensure_host_keys(layout, "worker-1")
    staged1 = host_keys.stage_pending_nats_key(layout, "worker-1")
    assert staged1.nats_public_key == original.nats_public_key
    assert staged1.pending_nats_public_key is not None
    assert staged1.pending_nats_public_key != original.nats_public_key

    # Retrying staging before promotion returns the exact same pending key.
    staged2 = host_keys.stage_pending_nats_key(layout, "worker-1")
    assert staged2.pending_nats_public_key == staged1.pending_nats_public_key

    promoted = host_keys.promote_pending_nats_key(
        layout, "worker-1", expected_public_key=staged1.pending_nats_public_key
    )
    assert promoted.nats_public_key == staged1.pending_nats_public_key
    assert promoted.pending_nats_public_key is None
    assert host_keys.load_host_keys(layout, "worker-1").nats_public_key == promoted.nats_public_key

    # Idempotent re-promotion after already promoted.
    again = host_keys.promote_pending_nats_key(layout, "worker-1", expected_public_key=promoted.nats_public_key)
    assert again.nats_public_key == promoted.nats_public_key

    # Discard clears a staged key.
    staged3 = host_keys.stage_pending_nats_key(layout, "worker-1")
    assert staged3.pending_nats_public_key is not None
    cleared = host_keys.discard_pending_nats_key(layout, "worker-1")
    assert cleared.pending_nats_public_key is None
    assert cleared.nats_public_key == promoted.nats_public_key


def test_expected_hosts_persists_bootstrap_nats_expires_at(tmp_path: Path) -> None:
    """C12: bootstrap_nats_expires_at persists through expected-hosts.json round-trips."""

    from custom_transport.protocol1.nats_jwt import NatsSigningKey

    path = tmp_path / "expected-hosts.json"
    boot = NatsSigningKey.generate("user").public_key
    boot_exp = datetime(2026, 11, 25, 12, 0, tzinfo=timezone.utc)
    host = ExpectedHost(
        "worker-boot",
        "worker.key1",
        "PUBLIC1",
        frozenset({"worker_host"}),
        frozenset({"team_a"}),
        bootstrap_nats_public_key=boot,
        bootstrap_nats_expires_at=boot_exp,
    )
    write_expected_hosts(path, {host.runtime: host})
    loaded = read_expected_hosts(path)[host.runtime]
    assert loaded.bootstrap_nats_public_key == boot
    assert loaded.bootstrap_nats_expires_at == boot_exp


def test_ledger_persists_certificate_partitions_and_min_revoke_until(tmp_path: Path) -> None:
    """C11, C12: ledger persists per-certificate granted partitions and compares confirmation timestamps."""

    from custom_transport.protocol1.nats_jwt import NatsSigningKey

    now = utc_now()
    k1 = NatsSigningKey.generate("user").public_key
    ledger = IssueLedger.open(tmp_path, "install_one")
    ledger.record_certificate_partitions("cert.worker-1.1", ())
    ledger.record_certificate_partitions("cert.worker-1.2", (3, 1))
    ledger.record_confirmed_nats_revocation(k1, 1000)
    assert ledger.is_nats_revocation_confirmed(k1, min_revoke_until=1000)
    assert not ledger.is_nats_revocation_confirmed(k1, min_revoke_until=2000)

    # Raising revoke_until invalidates earlier lower confirmation until re-confirmed.
    ledger.record_revoked_nats_key(k1, 2000)
    assert not ledger.is_nats_revocation_confirmed(k1)
    ledger.save(now)

    reopened = IssueLedger.open(tmp_path, "install_one")
    assert reopened.certificate_partitions("cert.worker-1.1") == ()
    assert reopened.certificate_partitions("cert.worker-1.2") == (1, 3)
    assert reopened.certificate_partitions("cert.worker-1.99") is None
    assert not reopened.is_nats_revocation_confirmed(k1)
    reopened.record_confirmed_nats_revocation(k1, 2000)
    assert reopened.is_nats_revocation_confirmed(k1)
    reopened.forget_certificate_partitions("cert.worker-1.2")
    assert reopened.certificate_partitions("cert.worker-1.2") is None


def test_host_keys_rotate_nats_key_prefers_pending_then_generates_fresh(tmp_path: Path) -> None:
    """C16: rotate_nats_key promotes a staged pending key first or generates a fresh key, keeping application_key."""

    layout = InstanceLayout.at(tmp_path / "instance")
    original, _ = host_keys.ensure_host_keys(layout, "worker-1")
    staged = host_keys.stage_pending_nats_key(layout, "worker-1")
    assert staged.pending_nats_public_key is not None

    promoted = host_keys.rotate_nats_key(layout, "worker-1", prefer_pending=True)
    assert promoted.key_id == original.key_id
    assert promoted.public_key_text == original.public_key_text
    assert promoted.nats_public_key == staged.pending_nats_public_key
    assert promoted.pending_nats_public_key is None

    # Without a staged pending key, rotate_nats_key generates a brand-new NATS user key.
    fresh = host_keys.rotate_nats_key(layout, "worker-1", prefer_pending=True)
    assert fresh.key_id == original.key_id
    assert fresh.public_key_text == original.public_key_text
    assert fresh.nats_public_key != promoted.nats_public_key
    assert fresh.pending_nats_public_key is None
    assert host_keys.load_host_keys(layout, "worker-1").nats_public_key == fresh.nats_public_key


@pytest.mark.asyncio
async def test_retire_host_validation_max_expires_and_upkeep_revocation_reconciliation(tmp_path: Path) -> None:
    """C12, C13, C14: retire_host default reason & validation, max key_expires_at/revoke_until, and upkeep retry."""

    from custom_transport.protocol1.bootstrap import BootstrapBundle, write_bundle
    from custom_transport.protocol1.nats_jwt import NatsSigningKey
    from custom_transport.protocol1.trust_state import PersistentTrustStore
    from installation_authority.revocation import RevocationOutcome
    from installation_authority.service import AUTHORITY_RUNTIME, CertificateRequest, InstallationAuthority

    class _FakeRevoker:
        def __init__(self) -> None:
            self.fail_next = False
            self.calls: list[tuple[str, int]] = []

        @property
        def supports_instant_revocation(self) -> bool:
            return True

        async def revoke(
            self,
            nats_public_key: str,
            *,
            revoke_until: int,
            all_revocations: dict[str, int] = {},  # noqa: B006
            prune_before: int | None = None,
        ) -> RevocationOutcome:
            self.calls.append((nats_public_key, revoke_until))
            if self.fail_next:
                self.fail_next = False
                return RevocationOutcome(revoked=False, revoked_at=revoke_until, detail="transient error")
            merged = dict(all_revocations)
            merged[nats_public_key] = max(merged.get(nats_public_key, 0), revoke_until)
            if prune_before is not None:
                merged = {k: v for k, v in merged.items() if k == nats_public_key or v >= prune_before}
            return RevocationOutcome(
                revoked=True,
                revoked_at=merged[nats_public_key],
                detail="ok",
                confirmed_revocations=merged,
            )

    class _FakeBucket:
        def __init__(self) -> None:
            self.data: dict[str, bytes] = {}

        async def get(self, key: str) -> Any:
            from nats.js.errors import KeyNotFoundError

            if key not in self.data:
                raise KeyNotFoundError()
            return type("Entry", (), {"value": self.data[key]})()

        async def put(self, key: str, value: bytes) -> None:
            self.data[key] = value

    layout = InstanceLayout.at(tmp_path / "instance")
    auth_keys, _ = host_keys.ensure_authority_keys(layout, "install-c13")
    auth_host_keys, _ = host_keys.ensure_host_keys(layout, AUTHORITY_RUNTIME)
    worker_keys, _ = host_keys.ensure_host_keys(layout, "worker-1")
    now = datetime(2026, 11, 24, 12, 0, tzinfo=timezone.utc)
    revoker = _FakeRevoker()
    authority = InstallationAuthority(
        layout,
        auth_keys,
        auth_host_keys,
        partitions=8,
        queues=TEST_QUEUE_LIMITS,
        clock=lambda: now,
        revoker=revoker,
    )
    bundle = BootstrapBundle("install-c13", ((auth_keys.key_id, auth_keys.public_key_text),), partitions=8)
    write_bundle(layout.trust_bootstrap_path, bundle)
    authority._trust = PersistentTrustStore.open(bundle, layout.host_trust_dir(AUTHORITY_RUNTIME), now)
    bucket = _FakeBucket()
    authority._records = AsyncMock(return_value=bucket)  # type: ignore[method-assign]

    # Invalid reason is rejected before touching state.
    with pytest.raises(ValueError, match="revocation reason"):
        await authority.retire_host("worker-1", reason="retired", now=now)

    # Issue two epochs sharing both application key and NATS key, 10 days apart.
    req = CertificateRequest(
        runtime="worker-1",
        teams=("team_a",),
        roles=("worker_host",),
        agent_types=("helper",),
        session_limit=4,
        connection_class="connected",
        key_id=worker_keys.key_id,
        public_key=worker_keys.public_key_text,
        nats_public_key=worker_keys.nats_public_key,
    )
    t1 = now - timedelta(days=10)
    cert1 = authority.certificate_record(req, epoch=1, revocation_epoch=1, issued=t1)
    cert2 = authority.certificate_record(req, epoch=2, revocation_epoch=1, issued=now, replaces=cert1["body"]["certificate_id"])
    authority._trust.add(json.dumps(cert1).encode(), now=now)
    authority._trust.add(json.dumps(cert2).encode(), now=now)
    authority._revocations = authority.revocations_record(sequence=1, revocation_epoch=1, entries=[], issued=now)

    # C13 + C12: default reason='decommissioned' validates, key_expires_at is cert2's expires_at for BOTH entries,
    # and NATS key revoke_until is cert2's expires_at (not cert1's).
    outcomes = await authority.retire_host("worker-1", now=now)
    assert len(outcomes) == 1 and outcomes[0].revoked is True
    expected_exp = cert2["body"]["expires_at"]
    expected_exp_ts = int(parse_timestamp(expected_exp).timestamp())
    entries = authority._revocations["body"]["entries"]
    assert len(entries) == 2
    assert all(e["reason"] == "decommissioned" for e in entries)
    assert all(e["key_expires_at"] == expected_exp for e in entries)
    assert outcomes[0].revoked_at == expected_exp_ts

    # C14: failed revocation stays unconfirmed; a later push confirms all pushed keys; upkeep retries remaining unconfirmed.
    k_fail1 = NatsSigningKey.generate("user").public_key
    k_ok = NatsSigningKey.generate("user").public_key
    k_fail2 = NatsSigningKey.generate("user").public_key
    revoker.fail_next = True
    out1 = await authority._revoke_nats_key(k_fail1, revoke_until=int(now.timestamp()) + 600, now=now)
    assert out1.revoked is False
    assert not authority.is_nats_key_revocation_confirmed(k_fail1)

    # Later successful push for k_ok includes k_fail1 in all_revocations and confirms both.
    out2 = await authority._revoke_nats_key(k_ok, revoke_until=int(now.timestamp()) + 600, now=now)
    assert out2.revoked is True
    assert authority.is_nats_key_revocation_confirmed(k_ok)
    assert authority.is_nats_key_revocation_confirmed(k_fail1)

    # Another failed revocation is retried and confirmed by upkeep(now).
    revoker.fail_next = True
    out3 = await authority._revoke_nats_key(k_fail2, revoke_until=int(now.timestamp()) + 600, now=now)
    assert out3.revoked is False
    assert not authority.is_nats_key_revocation_confirmed(k_fail2)
    authority._forgotten_at = now
    await authority.upkeep(now)
    assert authority.is_nats_key_revocation_confirmed(k_fail2)


@pytest.mark.asyncio
async def test_renew_skips_placement_scan_for_non_partition_owning_roles(tmp_path: Path) -> None:
    """C15: gateway/team_services renewals do not scan UA1_PLACEMENT."""

    from custom_messages.wire.frame import VerifiedFrame
    from custom_transport.protocol1.bootstrap import BootstrapBundle, write_bundle
    from custom_transport.protocol1.joining import HostProfile
    from custom_transport.protocol1.trust_state import PersistentTrustStore
    from installation_authority.service import AUTHORITY_RUNTIME, CertificateRequest, InstallationAuthority

    layout = InstanceLayout.at(tmp_path / "instance")
    auth_keys, _ = host_keys.ensure_authority_keys(layout, "install-c15")
    auth_host_keys, _ = host_keys.ensure_host_keys(layout, AUTHORITY_RUNTIME)
    gw_keys, _ = host_keys.ensure_host_keys(layout, "gateway-1")
    now = datetime(2026, 11, 24, 12, 0, tzinfo=timezone.utc)

    gw_profile = HostProfile("gateway-1", ("team_a", "team_b"), ("gateway",), (), 0)
    write_expected_hosts(
        layout.trust_expected_hosts_path,
        {
            "gateway-1": ExpectedHost(
                "gateway-1",
                gw_keys.key_id,
                gw_keys.public_key_text,
                frozenset({"gateway"}),
                frozenset({"team_a", "team_b"}),
            )
        },
    )
    bundle = BootstrapBundle("install-c15", ((auth_keys.key_id, auth_keys.public_key_text),), partitions=8)
    write_bundle(layout.trust_bootstrap_path, bundle)

    authority = InstallationAuthority(
        layout,
        auth_keys,
        auth_host_keys,
        partitions=8,
        queues=TEST_QUEUE_LIMITS,
        clock=lambda: now,
    )
    authority._trust = PersistentTrustStore.open(bundle, layout.host_trust_dir(AUTHORITY_RUNTIME), now)
    authority._teams = ("team_a", "team_b")
    authority._started = True
    authority.live = object()  # type: ignore[assignment]
    authority._revocations = authority.revocations_record(sequence=1, revocation_epoch=1, entries=[], issued=now)

    req = CertificateRequest(
        runtime="gateway-1",
        teams=("team_a", "team_b"),
        roles=("gateway",),
        agent_types=(),
        session_limit=0,
        connection_class="connected",
        key_id=gw_keys.key_id,
        public_key=gw_keys.public_key_text,
        nats_public_key=gw_keys.nats_public_key,
    )
    cert_rec = authority.certificate_record(req, epoch=1, revocation_epoch=1, issued=now)
    authority._trust.add(json.dumps(cert_rec).encode(), now=now)
    gw_cert = authority._trust.store.host_certificate("install-c15", "gateway-1", 1)
    assert gw_cert is not None

    scan_mock = AsyncMock(side_effect=AssertionError("UA1_PLACEMENT must not be scanned for gateway renewals"))
    authority._read_host_partition_ownership = scan_mock  # type: ignore[method-assign]
    authority._latest_certificate = AsyncMock(return_value=cert_rec)  # type: ignore[method-assign]
    fake_bucket = AsyncMock()
    authority._records = AsyncMock(return_value=fake_bucket)  # type: ignore[method-assign]

    body = {
        "renew_id": "renew-gw-1",
        "certificate_id": gw_cert.certificate_id,
        "membership_epoch": 1,
        "profile_sha256": gw_profile.digest(),
        "contract_sha256": authority.contract_sha256,
    }
    fake_frame = type("Frame", (), {"idempotency_key": "gateway-1|renew-gw-1", "kind": "host.renew"})()
    verified = VerifiedFrame(
        frame=fake_frame,  # type: ignore[arg-type]
        body=body,
        raw={"body": body},
        certificate=gw_cert,
        session=None,
        authority=None,
        replay_key="rk",
        frame_digest="fd",
    )

    reply = await authority._renew(verified, give_up=False)
    assert reply["outcome"] == "ok"
    scan_mock.assert_not_awaited()
