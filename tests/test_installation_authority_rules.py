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
from custom_messages.wire.kinds import CLOCK_SKEW_SECONDS
from custom_messages.wire.refusals import FrameRefused
from custom_messages.wire.trust import kept_for
from custom_messages.wire.values import parse_timestamp
from custom_transport.protocol1 import host_keys
from custom_transport.protocol1.bootstrap import BootstrapBundle, read_bundle, write_bundle
from custom_transport.protocol1.bucket_reads import BucketReadTimedOut
from custom_transport.protocol1.live import utc_now
from custom_transport.protocol1.nats_jwt import NatsSigningKey
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
    valid_nats_key = NatsSigningKey.generate("user").public_key
    verified = SimpleNamespace(raw={"body": join_body(nats_public_key=valid_nats_key, profile_sha256="")})
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
    valid_nats_key = NatsSigningKey.generate("user").public_key
    held = certificate(key_id=listed.key_id, public_key=public_key, certificate_id="certificate-1", membership_epoch=1,
                       nats_public_key=valid_nats_key)
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
    """C11, C12, C17: ledger persists per-certificate granted partition sequences and compares confirmation timestamps."""

    from custom_transport.protocol1.nats_jwt import NatsSigningKey

    now = utc_now()
    k1 = NatsSigningKey.generate("user").public_key
    ledger = IssueLedger.open(tmp_path, "install_one")
    ledger.record_certificate_partitions("cert.worker-1.1", {})
    ledger.record_certificate_partitions("cert.worker-1.2", {3: 300, 1: 100})
    ledger.record_confirmed_nats_revocation(k1, 1000)
    assert ledger.is_nats_revocation_confirmed(k1, min_revoke_until=1000)
    assert not ledger.is_nats_revocation_confirmed(k1, min_revoke_until=2000)

    # Raising revoke_until invalidates earlier lower confirmation until re-confirmed.
    ledger.record_revoked_nats_key(k1, 2000)
    assert not ledger.is_nats_revocation_confirmed(k1)
    ledger.save(now)

    reopened = IssueLedger.open(tmp_path, "install_one")
    assert reopened.certificate_partitions("cert.worker-1.1") == {}
    assert reopened.certificate_partitions("cert.worker-1.2") == {1: 100, 3: 300}
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

    # C13 + C12 + F15: default reason='decommissioned' validates, key_expires_at is cert2's expires_at for BOTH
    # entries, and NATS key revoke_until is bounded to min(cert2.expires_at, now + credential_lifetime_seconds + skew).
    outcomes = await authority.retire_host("worker-1", now=now)
    assert len(outcomes) == 1 and outcomes[0].revoked is True
    expected_exp = cert2["body"]["expires_at"]
    expected_exp_ts = int(parse_timestamp(expected_exp).timestamp())
    expected_bounded_ts = min(
        expected_exp_ts,
        int(now.timestamp()) + authority.credential_lifetime_seconds + CLOCK_SKEW_SECONDS,
    )
    entries = authority._revocations["body"]["entries"]
    assert len(entries) == 2
    assert all(e["reason"] == "decommissioned" for e in entries)
    assert all(e["key_expires_at"] == expected_exp for e in entries)
    assert outcomes[0].revoked_at == expected_bounded_ts

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


@pytest.mark.asyncio
async def test_c17_host_lost_spares_fast_reassignment_within_clock_skew_window(tmp_path: Path) -> None:
    """C17: post-recovery reassignment + renewal within CLOCK_SKEW_SECONDS is spared using owner_sequence < release_sequence."""

    from datetime import timedelta

    from custom_transport.protocol1 import host_keys
    from custom_transport.protocol1.bootstrap import BootstrapBundle, write_bundle
    from custom_transport.protocol1.nats_jwt import NatsSigningKey
    from custom_transport.protocol1.trust_state import PersistentTrustStore
    from installation_authority.service import AUTHORITY_RUNTIME, CertificateRequest, InstallationAuthority

    layout = InstanceLayout.at(tmp_path / "instance")
    auth_keys, _ = host_keys.ensure_authority_keys(layout, "install-c17")
    auth_host_keys, _ = host_keys.ensure_host_keys(layout, AUTHORITY_RUNTIME)
    w_keys, _ = host_keys.ensure_host_keys(layout, "worker-1")
    t0 = datetime(2026, 11, 24, 12, 0, tzinfo=timezone.utc)
    k1 = NatsSigningKey.generate("user").public_key
    k1_pre_renew = NatsSigningKey.generate("user").public_key
    k2_recovered = NatsSigningKey.generate("user").public_key

    bundle = BootstrapBundle("install-c17", ((auth_keys.key_id, auth_keys.public_key_text),), partitions=8)
    write_bundle(layout.trust_bootstrap_path, bundle)
    authority = InstallationAuthority(
        layout,
        auth_keys,
        auth_host_keys,
        partitions=8,
        queues=TEST_QUEUE_LIMITS,
        clock=lambda: t0 + timedelta(seconds=5),
    )
    authority._trust = PersistentTrustStore.open(bundle, layout.host_trust_dir(AUTHORITY_RUNTIME), t0)
    authority._teams = ("team_a",)
    authority._started = True

    def _add_cert(epoch: int, nats_key: str, issued: datetime, part_seqs: dict[int, int]) -> None:
        req = CertificateRequest(
            runtime="worker-1",
            teams=("team_a",),
            roles=("worker_host",),
            agent_types=("researcher",),
            session_limit=4,
            connection_class="connected",
            key_id=w_keys.key_id,
            public_key=w_keys.public_key_text,
            nats_public_key=nats_key,
        )
        rec = authority.certificate_record(req, epoch=epoch, revocation_epoch=1, issued=issued)
        authority._trust.add(json.dumps(rec).encode(), now=issued)
        authority._ledger.take_epoch("worker-1", epoch)
        authority._ledger.record_certificate_partitions(str(rec["body"]["certificate_id"]), part_seqs)

    # Epoch 1: original cut-off cert (key K1) owning partition 1 at owner_sequence=100.
    _add_cert(1, k1, t0, {1: 100})
    # Epoch 2: a pre-release renewal (key k1_pre_renew) still carrying partition 1 from owner_sequence=100.
    _add_cert(2, k1_pre_renew, t0 + timedelta(seconds=1), {1: 100})
    # Epoch 3: fast recovery rejoin with fresh key K2 (partitions={}) 2 seconds later.
    _add_cert(3, k2_recovered, t0 + timedelta(seconds=2), {})
    # Epoch 4: fast reassignment of partition 1 at higher owner_sequence=300 and renewal 3 seconds later
    # (well within CLOCK_SKEW_SECONDS of released_at = t0 + 1s).
    _add_cert(4, k2_recovered, t0 + timedelta(seconds=3), {1: 300})

    # Delayed host_lost release for partition 1 arrives at sequence=200, host_epoch=1, released_at=t0+1s.
    await authority.revoke_cut_off_host_nats_key(
        "worker-1",
        host_epoch=1,
        partition=1,
        team="team_a",
        release_sequence=200,
        released_at=t0 + timedelta(seconds=1),
        now=t0 + timedelta(seconds=5),
    )

    # Original epoch-1 key and pre-release epoch-2 key (owner_seq 100 < release_seq 200) are revoked.
    assert authority._ledger.is_nats_key_revoked(k1)
    assert authority._ledger.is_nats_key_revoked(k1_pre_renew)
    # Recovered host's K2 (reassigned at owner_seq 300 >= release_seq 200) is deterministically spared.
    assert not authority._ledger.is_nats_key_revoked(k2_recovered)


@pytest.mark.asyncio
async def test_f7_invalid_nkey_checksum_refused_and_corrupt_entries_skipped(tmp_path: Path) -> None:
    """F7/SEC-02: CRC-invalid NKeys are refused at join/renew/expected-hosts and skipped cleanly by OperatorModeRevoker."""

    from installation_authority.revocation import (
        ACCOUNTZ_SUBJECT,
        CLAIMS_UPDATE_SUBJECT,
        AccountBaseClaims,
        OperatorModeRevoker,
    )

    bad_nkey = "U" + "A" * 55
    with pytest.raises(ValueError, match="checksum|NKey"):
        ExpectedHost(
            "worker-bad",
            "k1",
            "pub1",
            frozenset({"worker_host"}),
            frozenset({"team_a"}),
            bootstrap_nats_public_key=bad_nkey,
        )

    authority = authority_at(tmp_path)
    write_expected_hosts(authority.layout.trust_expected_hosts_path, {LISTED.runtime: LISTED})
    verified_join = SimpleNamespace(raw={"body": join_body(nats_public_key=bad_nkey, profile_sha256="")})
    with pytest.raises(FrameRefused) as bad_join:
        await authority.answer_join(verified_join)  # type: ignore[arg-type]
    assert bad_join.value.code == "invalid_body" and bad_join.value.at == "/body/nats_public_key"

    public_key = Ed25519PrivateKey.generate().public_key()
    authority.live = SimpleNamespace()  # type: ignore[assignment]
    held = certificate(
        key_id=LISTED.key_id,
        public_key=public_key,
        certificate_id="cert.worker.1",
        membership_epoch=1,
        nats_public_key=NatsSigningKey.generate("user").public_key,
    )
    verified_renew = SimpleNamespace(
        certificate=held,
        frame=SimpleNamespace(idempotency_key="renew-bad-nkey"),
        raw={"body": {"profile_sha256": "", "new_nats_public_key": bad_nkey}},
    )
    with pytest.raises(FrameRefused) as bad_renew:
        await authority.answer_renew(verified_renew)  # type: ignore[arg-type]
    assert bad_renew.value.code == "invalid_body" and bad_renew.value.at == "/body/new_nats_public_key"

    # OperatorModeRevoker skips legacy corrupt keys in all_revocations instead of failing all future revocations.
    op = NatsSigningKey.generate("operator")
    acc = NatsSigningKey.generate("account")
    good_u = NatsSigningKey.generate("user").public_key
    last_jwt = ""

    async def responder(subject: str, payload: bytes, timeout: float = 5.0) -> Any:
        del timeout
        nonlocal last_jwt
        if subject == CLAIMS_UPDATE_SUBJECT:
            last_jwt = payload.decode("ascii")
            return SimpleNamespace(
                data=json.dumps({"data": {"account": acc.public_key, "code": 200, "message": "jwt updated"}}).encode()
            )
        assert subject == ACCOUNTZ_SUBJECT
        return SimpleNamespace(
            data=json.dumps(
                {
                    "data": {
                        "account_detail": {
                            "account_name": acc.public_key,
                            "jwt": last_jwt,
                            "revoked_user": {good_u: 2000},
                        }
                    }
                }
            ).encode()
        )

    revoker = OperatorModeRevoker(
        op,
        AccountBaseClaims(account_public_key=acc.public_key),
        SimpleNamespace(request=responder),
    )
    out = await revoker.revoke(good_u, revoke_until=2000, all_revocations={bad_nkey: 9999})
    assert out.revoked is True
    assert good_u in revoker.applied_revocations and bad_nkey not in revoker.applied_revocations


@pytest.mark.asyncio
async def test_f10_cross_host_bootstrap_key_banned_and_forget_verifies_records(tmp_path: Path) -> None:
    """F10/SEC-05/DI-6: All bootstrap keys from expected-hosts.json are banned across hosts & saved on refusal,
    and forget() verifies UA1_CERTS records before deleting or touching certificate_partitions."""

    from custom_transport.protocol1.host_keys import ensure_authority_keys, ensure_host_keys
    from custom_transport.protocol1.trust_state import PersistentTrustStore

    layout = InstanceLayout.at(tmp_path / "inst-f10")
    auth_keys, _ = ensure_authority_keys(layout, "install-f10")
    auth_host_keys, _ = ensure_host_keys(layout, AUTHORITY_RUNTIME)
    host_a_keys, _ = ensure_host_keys(layout, "host-a")
    host_b_keys, _ = ensure_host_keys(layout, "host-b")
    boot_b = NatsSigningKey.generate("user").public_key
    write_expected_hosts(
        layout.trust_expected_hosts_path,
        {
            "host-a": ExpectedHost(
                "host-a", host_a_keys.key_id, host_a_keys.public_key_text, frozenset({"worker_host"}), frozenset({"team_a"})
            ),
            "host-b": ExpectedHost(
                "host-b",
                host_b_keys.key_id,
                host_b_keys.public_key_text,
                frozenset({"worker_host"}),
                frozenset({"team_a"}),
                bootstrap_nats_public_key=boot_b,
            ),
        },
    )
    now = datetime(2026, 11, 24, 12, 0, tzinfo=timezone.utc)
    bundle = BootstrapBundle("install-f10", ((auth_keys.key_id, auth_keys.public_key_text),), partitions=8)
    write_bundle(layout.trust_bootstrap_path, bundle)
    authority = InstallationAuthority(
        layout, auth_keys, auth_host_keys, partitions=8, queues=authority_at(tmp_path / "q").queues, clock=lambda: now
    )
    authority._trust = PersistentTrustStore.open(bundle, layout.host_trust_dir(AUTHORITY_RUNTIME), now)

    # host-a presents host-b's bootstrap key before host-b ever joins -> refused and persisted in issued.json!
    verified_join = SimpleNamespace(
        raw={
            "body": join_body(
                runtime="host-a",
                application_key={"key_id": host_a_keys.key_id, "public_key": host_a_keys.public_key_text},
                teams=["team_a"],
                nats_public_key=boot_b,
                profile_sha256="",
            )
        }
    )
    with pytest.raises(FrameRefused) as refused:
        await authority.answer_join(verified_join)  # type: ignore[arg-type]
    assert refused.value.code == "invalid_body" and refused.value.at == "/body/nats_public_key"
    reopened = IssueLedger.open(layout.host_trust_dir(AUTHORITY_RUNTIME), "install-f10")
    assert reopened.is_bootstrap_nats_key(boot_b)

    # Forged UA1_CERTS record with old issued_at and active certificate_id is skipped by forget().
    active_cert_id = "cert.host-a.2"
    authority._ledger.record_certificate_partitions(active_cert_id, {0: 123})
    forged_key = "host.install-f10.host-a.99"
    forged_payload = json.dumps(
        {
            "record": "certificate",
            "key": forged_key,
            "issued_at": format_timestamp(now - timedelta(days=90)),
            "body": {"certificate_id": active_cert_id, "runtime": "host-a"},
        }
    ).encode()
    bucket = _Bucket()
    bucket.values[forged_key] = forged_payload
    authority._records = AsyncMock(return_value=bucket)  # type: ignore[method-assign]
    authority._bucket_keys = AsyncMock(return_value=[forged_key])  # type: ignore[method-assign]
    deleted = await authority.forget(now)
    assert deleted == 0
    assert bucket.deleted == []
    assert authority._ledger.certificate_partitions(active_cert_id) == {0: 123}


@pytest.mark.asyncio
async def test_f5_pending_flush_revocations_withheld_from_upkeep_and_concurrent_pushes(tmp_path: Path) -> None:
    """F5/CR-2: Outgoing keys in ReplyWithAfterFlush are banned in IssueLedger but excluded from upkeep
    and unrelated concurrent pushes until their own after_flush runs."""

    from installation_authority.revocation import RevocationOutcome

    pushed_batches: list[tuple[str, set[str]]] = []

    class _TrackingRevoker:
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
            pushed = set(all_revocations) | {nats_public_key}
            pushed_batches.append((nats_public_key, pushed))
            return RevocationOutcome(
                revoked=True,
                revoked_at=revoke_until,
                confirmed_revocations={k: revoke_until for k in pushed},
            )

    now = datetime(2026, 11, 24, 12, 0, tzinfo=timezone.utc)
    authority = authority_at(tmp_path)
    authority._revoker = _TrackingRevoker()
    k_inflight = NatsSigningKey.generate("user").public_key
    k_other = NatsSigningKey.generate("user").public_key
    deferred: list[Any] = []
    immediate: list[tuple[str, int]] = []
    exp_ts = int(now.timestamp()) + 900

    async with authority._issue_lock:
        authority._defer_nats_key_revocation(
            k_inflight, revoke_until=exp_ts, now=now, deferred=deferred, immediate=immediate
        )
    assert immediate == [] and len(deferred) == 1
    assert authority._ledger.is_nats_key_revoked(k_inflight)
    # Upkeep must not pick up k_inflight before after_flush.
    assert k_inflight not in authority._unconfirmed_nats_revocations(now)
    # Concurrent unrelated revocation must not piggyback k_inflight before after_flush.
    await authority._revoke_nats_key(k_other, revoke_until=exp_ts, now=now)
    assert pushed_batches == [(k_other, {k_other})]
    assert not authority.is_nats_key_revocation_confirmed(k_inflight)

    # Once after_flush runs, k_inflight is pushed and confirmed.
    await deferred[0]()
    assert authority.is_nats_key_revocation_confirmed(k_inflight)
    assert k_inflight in pushed_batches[-1][1]


@pytest.mark.asyncio
async def test_f15_unloaded_account_accountz_500_confirms_via_claims_lookup() -> None:
    """F15/FA-9: OperatorModeRevoker confirms via CLAIMS.LOOKUP when ACCOUNTZ returns error 500 (unloaded account)."""

    from installation_authority.revocation import (
        ACCOUNTZ_SUBJECT,
        CLAIMS_LOOKUP_SUBJECT_TEMPLATE,
        CLAIMS_UPDATE_SUBJECT,
        AccountBaseClaims,
        OperatorModeRevoker,
    )

    op = NatsSigningKey.generate("operator")
    acc = NatsSigningKey.generate("account")
    u1 = NatsSigningKey.generate("user").public_key
    stored_jwt = ""
    return_mismatch = False

    async def responder(subject: str, payload: bytes, timeout: float = 5.0) -> Any:
        del timeout
        nonlocal stored_jwt
        if subject == CLAIMS_UPDATE_SUBJECT:
            stored_jwt = payload.decode("ascii")
            return SimpleNamespace(
                data=json.dumps({"data": {"account": acc.public_key, "code": 200, "message": "jwt updated"}}).encode()
            )
        if subject == ACCOUNTZ_SUBJECT:
            return SimpleNamespace(
                data=json.dumps(
                    {"error": {"code": 500, "description": f"Account {acc.public_key} does not exist"}}
                ).encode()
            )
        assert subject == CLAIMS_LOOKUP_SUBJECT_TEMPLATE.format(account=acc.public_key)
        return SimpleNamespace(data=(b"wrong.jwt" if return_mismatch else stored_jwt.encode("ascii")))

    revoker = OperatorModeRevoker(
        op, AccountBaseClaims(account_public_key=acc.public_key), SimpleNamespace(request=responder)
    )
    ok_out = await revoker.revoke(u1, revoke_until=2000)
    assert ok_out.revoked is True

    return_mismatch = True
    u2 = NatsSigningKey.generate("user").public_key
    bad_out = await revoker.revoke(u2, revoke_until=2000)
    assert bad_out.revoked is False
    assert u2 not in revoker.applied_revocations


@pytest.mark.asyncio
async def test_f4_authority_dekker_revokes_and_refuses_when_partition_released_during_issue(tmp_path: Path) -> None:
    """F4/CR-1/DI-5/SEC-03: If UA1_PLACEMENT releases a partition while _issue() is in flight,
    the post-issue Dekker re-read revokes the minted NATS key immediately and returns not_allowed."""

    from custom_messages.wire.frame import VerifiedFrame
    from custom_transport.protocol1.host_keys import ensure_authority_keys, ensure_host_keys
    from custom_transport.protocol1.trust_state import PersistentTrustStore
    from installation_authority.service import CertificateRequest

    layout = InstanceLayout.at(tmp_path / "inst-f4")
    auth_keys, _ = ensure_authority_keys(layout, "install-f4")
    auth_host_keys, _ = ensure_host_keys(layout, AUTHORITY_RUNTIME)
    w_keys, _ = ensure_host_keys(layout, "worker-1")
    now = datetime(2026, 11, 24, 12, 0, tzinfo=timezone.utc)
    k1 = NatsSigningKey.generate("user").public_key
    k2 = NatsSigningKey.generate("user").public_key
    write_expected_hosts(
        layout.trust_expected_hosts_path,
        {
            "worker-1": ExpectedHost(
                "worker-1",
                w_keys.key_id,
                w_keys.public_key_text,
                frozenset({"worker_host"}),
                frozenset({"team_a"}),
                agent_types=frozenset({"researcher"}),
                session_limit=4,
            )
        },
    )
    bundle = BootstrapBundle("install-f4", ((auth_keys.key_id, auth_keys.public_key_text),), partitions=8)
    write_bundle(layout.trust_bootstrap_path, bundle)
    authority = InstallationAuthority(
        layout,
        auth_keys,
        auth_host_keys,
        partitions=8,
        queues=TEST_QUEUE_LIMITS,
        clock=lambda: now,
        credential_lifetime_seconds=300,
    )
    authority._trust = PersistentTrustStore.open(bundle, layout.host_trust_dir(AUTHORITY_RUNTIME), now)
    authority._revocations = {"body": {"revocation_epoch": 1, "entries": []}}
    authority._teams = ("team_a",)
    authority._started = True
    authority.live = SimpleNamespace()  # type: ignore[assignment]
    authority._records = AsyncMock(return_value=_Bucket())  # type: ignore[method-assign]

    req = CertificateRequest(
        runtime="worker-1",
        teams=("team_a",),
        roles=("worker_host",),
        agent_types=("researcher",),
        session_limit=4,
        connection_class="connected",
        key_id=w_keys.key_id,
        public_key=w_keys.public_key_text,
        nats_public_key=k1,
    )
    rec = authority.certificate_record(req, epoch=1, revocation_epoch=1, issued=now)
    authority._trust.add(json.dumps(rec).encode(), now=now)
    authority._ledger.take_epoch("worker-1", 1)
    cert = authority._trust.store.host_certificate("install-f4", "worker-1", 1)
    assert cert is not None
    authority._latest_certificate = AsyncMock(return_value=rec)  # type: ignore[method-assign]

    # Simulate UA1_PLACEMENT transitioning partition 0 from OWNED to RELEASED right as _issue publishes the cert.
    reads = 0

    async def changing_ownership(_runtime: str, _teams: tuple[str, ...], _now: datetime) -> Any:
        nonlocal reads
        reads += 1
        if reads <= 2:
            return {"team_a": {0: 100}}, {}
        return {}, {"team_a": {0: (1, "host_lost")}}

    authority._read_host_partition_ownership = changing_ownership  # type: ignore[method-assign]
    revoked_keys: list[str] = []

    async def track_revoke(key: str, *, revoke_until: int, now: datetime) -> Any:
        del revoke_until, now
        revoked_keys.append(key)
        return SimpleNamespace(revoked=True)

    authority._revoke_nats_key = track_revoke  # type: ignore[method-assign]

    body = {"profile_sha256": str(rec["body"]["profile_sha256"]), "partitions": [0], "new_nats_public_key": k2}
    verified = VerifiedFrame(
        frame=SimpleNamespace(idempotency_key="worker-1|renew-f4", kind="host.renew"),  # type: ignore[arg-type]
        body=body,
        raw={"body": body},
        certificate=cert,
        session=None,
        authority=None,
        replay_key="rk",
        frame_digest="fd",
    )
    reply = await authority._renew(verified, give_up=False)
    assert isinstance(reply, dict) and reply["outcome"] == "not_allowed"
    assert k2 in revoked_keys
    assert authority._ledger.is_nats_key_revoked(k2)


@pytest.mark.asyncio
async def test_c31_retire_host_serializes_with_concurrent_key_rotating_renew(tmp_path: Path) -> None:
    """C31/F4: retire_host holds _issue_lock across cert discovery, revocations publish, and ledger updates
    so a concurrent key-rotating renew cannot slip an unrevoked NATS key past retirement."""

    from custom_messages.wire.frame import VerifiedFrame
    from custom_transport.protocol1.host_keys import ensure_authority_keys, ensure_host_keys
    from custom_transport.protocol1.trust_state import PersistentTrustStore
    from installation_authority.service import CertificateRequest

    def _make_authority(sub: str) -> tuple[InstallationAuthority, Any, Any, datetime, str]:
        layout = InstanceLayout.at(tmp_path / sub)
        auth_keys, _ = ensure_authority_keys(layout, f"install-{sub}")
        auth_host_keys, _ = ensure_host_keys(layout, AUTHORITY_RUNTIME)
        w_keys, _ = ensure_host_keys(layout, "worker-1")
        now = datetime(2026, 11, 24, 12, 0, tzinfo=timezone.utc)
        k1 = NatsSigningKey.generate("user").public_key
        write_expected_hosts(
            layout.trust_expected_hosts_path,
            {
                "worker-1": ExpectedHost(
                    "worker-1",
                    w_keys.key_id,
                    w_keys.public_key_text,
                    frozenset({"worker_host"}),
                    frozenset({"team_a"}),
                    agent_types=frozenset({"researcher"}),
                    session_limit=4,
                )
            },
        )
        bundle = BootstrapBundle(f"install-{sub}", ((auth_keys.key_id, auth_keys.public_key_text),), partitions=8)
        write_bundle(layout.trust_bootstrap_path, bundle)
        authority = InstallationAuthority(
            layout,
            auth_keys,
            auth_host_keys,
            partitions=8,
            queues=TEST_QUEUE_LIMITS,
            clock=lambda: now,
            credential_lifetime_seconds=300,
        )
        authority._trust = PersistentTrustStore.open(bundle, layout.host_trust_dir(AUTHORITY_RUNTIME), now)
        rev0 = authority.revocations_record(sequence=1, revocation_epoch=1, entries=[], issued=now)
        authority._trust.add(json.dumps(rev0).encode(), now=now)
        authority._revocations = rev0
        authority._teams = ("team_a",)
        authority._started = True
        authority.live = SimpleNamespace()  # type: ignore[assignment]
        bucket = _Bucket()
        authority._records = AsyncMock(return_value=bucket)  # type: ignore[method-assign]

        req = CertificateRequest(
            runtime="worker-1",
            teams=("team_a",),
            roles=("worker_host",),
            agent_types=("researcher",),
            session_limit=4,
            connection_class="connected",
            key_id=w_keys.key_id,
            public_key=w_keys.public_key_text,
            nats_public_key=k1,
        )
        rec = authority.certificate_record(req, epoch=1, revocation_epoch=1, issued=now)
        authority._trust.add(json.dumps(rec).encode(), now=now)
        authority._ledger.take_epoch("worker-1", 1)
        cert = authority._trust.store.host_certificate(f"install-{sub}", "worker-1", 1)
        assert cert is not None
        authority._latest_certificate = AsyncMock(return_value=rec)  # type: ignore[method-assign]
        return authority, cert, rec, now, k1

    # Interleaving 1: retire_host enters _issue_lock first and pauses during cert discovery;
    # concurrent key-rotating renew must wait for _issue_lock and then see outcome == "revoked".
    auth1, cert1, rec1, now1, k1_a = _make_authority("c31-retire-first")
    k2_a = NatsSigningKey.generate("user").public_key
    retire_in_discovery = asyncio.Event()
    release_retire = asyncio.Event()
    orig_discover = auth1._host_certificates_for_revocation

    async def slow_discover(runtime: str, now: datetime, *, min_epoch: int = 1, include_epoch: int | None = None) -> Any:
        retire_in_discovery.set()
        await release_retire.wait()
        return await orig_discover(runtime, now, min_epoch=min_epoch, include_epoch=include_epoch)

    auth1._host_certificates_for_revocation = slow_discover  # type: ignore[method-assign]
    revoked_a: list[str] = []

    async def track_a(key: str, *, revoke_until: int, now: datetime) -> Any:
        del revoke_until, now
        revoked_a.append(key)
        return SimpleNamespace(revoked=True, revoked_at=0, confirmed_revocations={})

    auth1._revoke_nats_key = track_a  # type: ignore[method-assign]
    body_a = {"profile_sha256": str(rec1["body"]["profile_sha256"]), "new_nats_public_key": k2_a}
    verified_a = VerifiedFrame(
        frame=SimpleNamespace(idempotency_key="worker-1|renew-c31-a", kind="host.renew"),  # type: ignore[arg-type]
        body=body_a,
        raw={"body": body_a},
        certificate=cert1,
        session=None,
        authority=None,
        replay_key="rk-a",
        frame_digest="fd-a",
    )

    retire_task = asyncio.create_task(auth1.retire_host("worker-1", now=now1))
    await retire_in_discovery.wait()
    renew_task = asyncio.create_task(auth1._renew(verified_a, give_up=False))
    await asyncio.sleep(0.02)
    assert not renew_task.done()  # Blocked on _issue_lock held by retire_host!
    release_retire.set()
    await retire_task
    renew_reply_a = await renew_task
    assert renew_reply_a["outcome"] == "revoked"
    assert revoked_a == [k1_a]
    assert auth1._ledger.highest_epoch("worker-1") == 1
    assert not auth1._ledger.is_nats_key_revoked(k2_a)

    # Interleaving 2: key-rotating renew enters _issue_lock first and pauses before issuing cert2;
    # concurrent retire_host waits for _issue_lock and then revokes BOTH cert1/K1 AND cert2/K2.
    auth2, cert2, rec2, now2, k1_b = _make_authority("c31-renew-first")
    k2_b = NatsSigningKey.generate("user").public_key
    renew_in_flight = asyncio.Event()
    release_renew = asyncio.Event()

    async def slow_latest(runtime: str, now: datetime) -> Any:
        del runtime, now
        renew_in_flight.set()
        await release_renew.wait()
        return rec2

    auth2._latest_certificate = slow_latest  # type: ignore[method-assign]
    revoked_b: list[str] = []

    async def track_b(key: str, *, revoke_until: int, now: datetime) -> Any:
        del revoke_until, now
        revoked_b.append(key)
        return SimpleNamespace(revoked=True, revoked_at=0, confirmed_revocations={})

    auth2._revoke_nats_key = track_b  # type: ignore[method-assign]
    body_b = {"profile_sha256": str(rec2["body"]["profile_sha256"]), "new_nats_public_key": k2_b}
    verified_b = VerifiedFrame(
        frame=SimpleNamespace(idempotency_key="worker-1|renew-c31-b", kind="host.renew"),  # type: ignore[arg-type]
        body=body_b,
        raw={"body": body_b},
        certificate=cert2,
        session=None,
        authority=None,
        replay_key="rk-b",
        frame_digest="fd-b",
    )

    renew_task_b = asyncio.create_task(auth2._renew(verified_b, give_up=False))
    await renew_in_flight.wait()
    retire_task_b = asyncio.create_task(auth2.retire_host("worker-1", now=now2))
    await asyncio.sleep(0.02)
    assert not retire_task_b.done()  # Blocked on _issue_lock held by _renew!
    release_renew.set()
    renew_reply_b = await renew_task_b
    assert renew_reply_b["outcome"] == "ok"
    await retire_task_b
    assert set(revoked_b) == {k1_b, k2_b}
    assert auth2._ledger.is_nats_key_revoked(k1_b) and auth2._ledger.is_nats_key_revoked(k2_b)
    revoked_epochs = {
        e["membership_epoch"] for e in auth2._revocations["body"]["entries"] if e["runtime"] == "worker-1"
    }
    assert revoked_epochs == {1, 2}


@pytest.mark.asyncio
async def test_c32_dekker_abort_preserves_live_key_for_connected_handover_and_drops_stored_renewal(
    tmp_path: Path,
) -> None:
    """C32/F4/F5: Post-issue Dekker abort in _renew_new_locked:
    - Same-key renew racing with connected handover (operator/rebalanced/drained) returns not_allowed,
      keeps K1 alive, and drops StoredRenewal so retries cannot leak the unreturned JWT;
    - Key-rotating renew racing with connected handover revokes ONLY K2 immediately and keeps K1 alive;
    - Same-key renew racing with host_lost or partition move defers K1 post-flush instead of immediate revoke."""

    from custom_messages.wire.frame import VerifiedFrame
    from custom_transport.protocol1.host_keys import ensure_authority_keys, ensure_host_keys
    from custom_transport.protocol1.trust_state import PersistentTrustStore
    from installation_authority.service import CertificateRequest

    def _setup(sub: str) -> tuple[InstallationAuthority, InstanceLayout, Any, Any, datetime, str]:
        layout = InstanceLayout.at(tmp_path / sub)
        auth_keys, _ = ensure_authority_keys(layout, f"install-{sub}")
        auth_host_keys, _ = ensure_host_keys(layout, AUTHORITY_RUNTIME)
        w_keys, _ = ensure_host_keys(layout, "worker-1")
        acct = NatsSigningKey.generate("account")
        now = datetime(2026, 11, 24, 12, 0, tzinfo=timezone.utc)
        k1 = NatsSigningKey.generate("user").public_key
        write_expected_hosts(
            layout.trust_expected_hosts_path,
            {
                "worker-1": ExpectedHost(
                    "worker-1",
                    w_keys.key_id,
                    w_keys.public_key_text,
                    frozenset({"worker_host"}),
                    frozenset({"team_a"}),
                    agent_types=frozenset({"researcher"}),
                    session_limit=4,
                )
            },
        )
        bundle = BootstrapBundle(f"install-{sub}", ((auth_keys.key_id, auth_keys.public_key_text),), partitions=8)
        write_bundle(layout.trust_bootstrap_path, bundle)
        authority = InstallationAuthority(
            layout,
            auth_keys,
            auth_host_keys,
            partitions=8,
            queues=TEST_QUEUE_LIMITS,
            clock=lambda: now,
            credential_lifetime_seconds=300,
            nats_account_public_key=acct.public_key,
            nats_account_signing_key=acct,
        )
        authority._trust = PersistentTrustStore.open(bundle, layout.host_trust_dir(AUTHORITY_RUNTIME), now)
        authority._revocations = {"body": {"revocation_epoch": 1, "entries": []}}
        authority._teams = ("team_a",)
        authority._started = True
        authority.live = SimpleNamespace()  # type: ignore[assignment]
        authority._records = AsyncMock(return_value=_Bucket())  # type: ignore[method-assign]
        req = CertificateRequest(
            runtime="worker-1",
            teams=("team_a",),
            roles=("worker_host",),
            agent_types=("researcher",),
            session_limit=4,
            connection_class="connected",
            key_id=w_keys.key_id,
            public_key=w_keys.public_key_text,
            nats_public_key=k1,
        )
        rec = authority.certificate_record(req, epoch=1, revocation_epoch=1, issued=now)
        authority._trust.add(json.dumps(rec).encode(), now=now)
        authority._ledger.take_epoch("worker-1", 1)
        cert = authority._trust.store.host_certificate(f"install-{sub}", "worker-1", 1)
        assert cert is not None
        authority._latest_certificate = AsyncMock(return_value=rec)  # type: ignore[method-assign]
        return authority, layout, cert, rec, now, k1

    # Case 1: Same-key renew (K1 -> K1) racing with connected handover (reason="operator").
    auth1, layout1, cert1, rec1, now1, k1_1 = _setup("c32-same-key-connected")
    reads1 = 0

    async def own_then_connected_release(_rt: str, _teams: Any, _now: datetime) -> Any:
        nonlocal reads1
        reads1 += 1
        if reads1 <= 2:
            return {"team_a": {0: 100}}, {}
        return {}, {"team_a": {0: (1, "operator")}}

    auth1._read_host_partition_ownership = own_then_connected_release  # type: ignore[method-assign]
    revoked1: list[str] = []

    async def track1(key: str, *, revoke_until: int, now: datetime) -> Any:
        del revoke_until, now
        revoked1.append(key)
        return SimpleNamespace(revoked=True)

    auth1._revoke_nats_key = track1  # type: ignore[method-assign]
    body1 = {"profile_sha256": str(rec1["body"]["profile_sha256"]), "partitions": [0]}
    vf1 = VerifiedFrame(
        frame=SimpleNamespace(idempotency_key="worker-1|renew-c32-1", kind="host.renew"),  # type: ignore[arg-type]
        body=body1,
        raw={"body": body1},
        certificate=cert1,
        session=None,
        authority=None,
        replay_key="rk1",
        frame_digest="fd1",
    )
    deferred1: list[Any] = []
    reply1 = await auth1._renew(vf1, give_up=False, deferred=deferred1)
    assert reply1["outcome"] == "not_allowed"
    assert revoked1 == []
    assert deferred1 == []
    assert not auth1._ledger.is_nats_key_revoked(k1_1)
    # Aborted StoredRenewal must be dropped both in memory and on disk so retries cannot receive the unreturned JWT.
    assert auth1._ledger.renewal("worker-1|renew-c32-1", now1) is None
    reopened1 = IssueLedger.open(layout1.host_trust_dir(AUTHORITY_RUNTIME), "install-c32-same-key-connected")
    assert reopened1.renewal("worker-1|renew-c32-1", now1) is None
    with pytest.raises(FrameRefused) as retry_refused:
        await auth1._renew(vf1, give_up=False, deferred=[])
    assert retry_refused.value.code == "invalid_body" and retry_refused.value.at == "/body/partitions"

    # Case 2: Key-rotating renew (K1 -> K2) racing with connected handover (reason="rebalanced").
    auth2, _layout2, cert2, rec2, now2, k1_2 = _setup("c32-rotating-connected")
    k2_2 = NatsSigningKey.generate("user").public_key
    reads2 = 0

    async def own_then_rebalanced(_rt: str, _teams: Any, _now: datetime) -> Any:
        nonlocal reads2
        reads2 += 1
        if reads2 <= 2:
            return {"team_a": {0: 100}}, {}
        return {}, {"team_a": {0: (1, "rebalanced")}}

    auth2._read_host_partition_ownership = own_then_rebalanced  # type: ignore[method-assign]
    revoked2: list[str] = []

    async def track2(key: str, *, revoke_until: int, now: datetime) -> Any:
        del revoke_until, now
        revoked2.append(key)
        return SimpleNamespace(revoked=True)

    auth2._revoke_nats_key = track2  # type: ignore[method-assign]
    body2 = {"profile_sha256": str(rec2["body"]["profile_sha256"]), "partitions": [0], "new_nats_public_key": k2_2}
    vf2 = VerifiedFrame(
        frame=SimpleNamespace(idempotency_key="worker-1|renew-c32-2", kind="host.renew"),  # type: ignore[arg-type]
        body=body2,
        raw={"body": body2},
        certificate=cert2,
        session=None,
        authority=None,
        replay_key="rk2",
        frame_digest="fd2",
    )
    deferred2: list[Any] = []
    reply2 = await auth2._renew(vf2, give_up=False, deferred=deferred2)
    assert reply2["outcome"] == "not_allowed"
    assert revoked2 == [k2_2]
    assert deferred2 == []
    assert auth2._ledger.is_nats_key_revoked(k2_2)
    assert not auth2._ledger.is_nats_key_revoked(k1_2)
    assert auth2._ledger.renewal("worker-1|renew-c32-2", now2) is None

    # Case 3: Same-key renew (K1 -> K1) racing with partition moved/lost -> defer K1 post-flush (never immediate).
    auth3, _layout3, cert3, rec3, now3, k1_3 = _setup("c32-same-key-moved")
    reads3 = 0

    async def own_then_moved(_rt: str, _teams: Any, _now: datetime) -> Any:
        nonlocal reads3
        reads3 += 1
        if reads3 <= 2:
            return {"team_a": {0: 100}}, {}
        return {}, {}  # Partition moved to another host!

    auth3._read_host_partition_ownership = own_then_moved  # type: ignore[method-assign]
    revoked3: list[str] = []

    async def track3(key: str, *, revoke_until: int, now: datetime) -> Any:
        del revoke_until, now
        revoked3.append(key)
        return SimpleNamespace(revoked=True)

    auth3._revoke_nats_key = track3  # type: ignore[method-assign]
    body3 = {"profile_sha256": str(rec3["body"]["profile_sha256"]), "partitions": [0]}
    vf3 = VerifiedFrame(
        frame=SimpleNamespace(idempotency_key="worker-1|renew-c32-3", kind="host.renew"),  # type: ignore[arg-type]
        body=body3,
        raw={"body": body3},
        certificate=cert3,
        session=None,
        authority=None,
        replay_key="rk3",
        frame_digest="fd3",
    )
    deferred3: list[Any] = []
    reply3 = await auth3._renew(vf3, give_up=False, deferred=deferred3)
    assert reply3["outcome"] == "not_allowed"
    assert revoked3 == []  # Not pushed before reply flush!
    assert len(deferred3) == 1
    assert auth3._ledger.is_nats_key_revoked(k1_3)
    assert auth3._ledger.renewal("worker-1|renew-c32-3", now3) is None
    await deferred3[0]()
    assert revoked3 == [k1_3]


@pytest.mark.asyncio
async def test_c35_rotation_already_issued_skips_same_key_dekker_aborted_cert(tmp_path: Path) -> None:
    """C35: A same-key renewal carrying application_key_rotation that aborts on post-write Dekker
    during a connected handover leaves K1 unrevoked; a subsequent step-3 renewal carrying the same
    application_key_rotation and new_nats_public_key=K2 must rotate to K2 (and return connection),
    and a same-key retry must never receive the aborted certificate."""

    from custom_messages.wire.frame import VerifiedFrame
    from custom_transport.protocol1.host_keys import ensure_authority_keys, ensure_host_keys
    from custom_transport.protocol1.trust_state import PersistentTrustStore
    from installation_authority.ledger import IssueLedger
    from installation_authority.service import CertificateRequest

    layout = InstanceLayout.at(tmp_path / "inst-c35")
    auth_keys, _ = ensure_authority_keys(layout, "install-c35")
    auth_host_keys, _ = ensure_host_keys(layout, AUTHORITY_RUNTIME)
    w_keys, _ = ensure_host_keys(layout, "worker-1")
    rotated_app, _ = ensure_host_keys(layout, "worker-1-rotated")
    now = datetime(2026, 11, 24, 12, 0, tzinfo=timezone.utc)
    k1 = NatsSigningKey.generate("user").public_key
    k2 = NatsSigningKey.generate("user").public_key
    op_acc = NatsSigningKey.generate("account")

    write_expected_hosts(
        layout.trust_expected_hosts_path,
        {
            "worker-1": ExpectedHost(
                "worker-1",
                rotated_app.key_id,
                rotated_app.public_key_text,
                frozenset({"worker_host"}),
                frozenset({"team_a"}),
                agent_types=frozenset({"researcher"}),
                session_limit=4,
            )
        },
    )
    bundle = BootstrapBundle("install-c35", ((auth_keys.key_id, auth_keys.public_key_text),), partitions=8)
    write_bundle(layout.trust_bootstrap_path, bundle)
    authority = InstallationAuthority(
        layout,
        auth_keys,
        auth_host_keys,
        partitions=8,
        queues=TEST_QUEUE_LIMITS,
        clock=lambda: now,
        credential_lifetime_seconds=300,
        nats_account_public_key=op_acc.public_key,
        nats_account_signing_key=op_acc,
    )
    authority._trust = PersistentTrustStore.open(bundle, layout.host_trust_dir(AUTHORITY_RUNTIME), now)
    authority._revocations = {"body": {"revocation_epoch": 1, "entries": []}}
    authority._teams = ("team_a",)
    authority._started = True
    authority.live = SimpleNamespace()  # type: ignore[assignment]
    bucket = _Bucket()
    authority._records = AsyncMock(return_value=bucket)  # type: ignore[method-assign]

    req = CertificateRequest(
        runtime="worker-1",
        teams=("team_a",),
        roles=("worker_host",),
        agent_types=("researcher",),
        session_limit=4,
        connection_class="connected",
        key_id=w_keys.key_id,
        public_key=w_keys.public_key_text,
        nats_public_key=k1,
    )
    rec = authority.certificate_record(req, epoch=1, revocation_epoch=1, issued=now)
    authority._trust.add(json.dumps(rec).encode(), now=now)
    authority._ledger.take_epoch("worker-1", 1)
    authority._ledger.record_certificate_partitions(str(rec["body"]["certificate_id"]), {0: 100})
    cert = authority._trust.store.host_certificate("install-c35", "worker-1", 1)
    assert cert is not None
    authority._latest_certificate = AsyncMock(return_value=rec)  # type: ignore[method-assign]

    reads = 0

    async def own_then_connected_release(_rt: str, _teams: Any, _now: datetime) -> Any:
        nonlocal reads
        reads += 1
        if reads <= 2:
            return {"team_a": {0: 100}}, {}
        return {}, {"team_a": {0: (1, "operator")}}

    authority._read_host_partition_ownership = own_then_connected_release  # type: ignore[method-assign]
    revoked_keys: list[str] = []

    async def track_revoke(key: str, *, revoke_until: int, now: datetime) -> Any:
        del revoke_until, now
        revoked_keys.append(key)
        return SimpleNamespace(revoked=True)

    authority._revoke_nats_key = track_revoke  # type: ignore[method-assign]

    rotation_payload = {"key": {"key_id": rotated_app.key_id, "public_key": rotated_app.public_key_text}}
    body_aborted = {
        "profile_sha256": str(rec["body"]["profile_sha256"]),
        "partitions": [0],
        "application_key_rotation": rotation_payload,
    }
    vf_aborted = VerifiedFrame(
        frame=SimpleNamespace(idempotency_key="worker-1|renew-c35-1", kind="host.renew"),  # type: ignore[arg-type]
        body=body_aborted,
        raw={"body": body_aborted},
        certificate=cert,
        session=None,
        authority=None,
        replay_key="rk-c35-1",
        frame_digest="fd-c35-1",
    )
    reply1 = await authority._renew(vf_aborted, give_up=False, deferred=[])
    assert reply1["outcome"] == "not_allowed"
    assert not authority._ledger.is_nats_key_revoked(k1)
    # The aborted epoch-2 cert is persisted as aborted in issued.json.
    reopened = IssueLedger.open(layout.host_trust_dir(AUTHORITY_RUNTIME), "install-c35")
    assert reopened.is_aborted_certificate("cert.worker-1.2")

    # Same-key retry with same application_key_rotation (new renew_id) must NOT get aborted cert back.
    vf_same_key_retry = VerifiedFrame(
        frame=SimpleNamespace(idempotency_key="worker-1|renew-c35-same", kind="host.renew"),  # type: ignore[arg-type]
        body={
            "profile_sha256": str(rec["body"]["profile_sha256"]),
            "partitions": [],
            "application_key_rotation": rotation_payload,
        },
        raw={
            "body": {
                "profile_sha256": str(rec["body"]["profile_sha256"]),
                "partitions": [],
                "application_key_rotation": rotation_payload,
            }
        },
        certificate=cert,
        session=None,
        authority=None,
        replay_key="rk-c35-same",
        frame_digest="fd-c35-same",
    )
    with pytest.raises(FrameRefused) as same_key_refused:
        await authority._renew(vf_same_key_retry, give_up=False, deferred=[])
    assert same_key_refused.value.code == "invalid_body" and same_key_refused.value.at == "/body/new_nats_public_key"

    # Step 3 arrives with the same application_key_rotation and new_nats_public_key=K2:
    # must NOT return aborted K1 cert; must mint epoch 3 with K2 + connection JWT and defer-revoke K1.
    body_step3 = {
        "profile_sha256": str(rec["body"]["profile_sha256"]),
        "partitions": [],
        "new_nats_public_key": k2,
        "application_key_rotation": rotation_payload,
    }
    vf_step3 = VerifiedFrame(
        frame=SimpleNamespace(idempotency_key="worker-1|renew-c35-step3", kind="host.renew"),  # type: ignore[arg-type]
        body=body_step3,
        raw={"body": body_step3},
        certificate=cert,
        session=None,
        authority=None,
        replay_key="rk-c35-step3",
        frame_digest="fd-c35-step3",
    )
    deferred_step3: list[Any] = []
    reply_step3 = await authority._renew(vf_step3, give_up=False, deferred=deferred_step3)
    assert reply_step3["outcome"] == "ok"
    assert reply_step3["certificate"]["body"]["membership_epoch"] == 3
    assert reply_step3["certificate"]["body"]["nats_public_key"] == k2
    assert "connection" in reply_step3 and "jwt" in reply_step3["connection"]
    assert len(deferred_step3) == 1
    await deferred_step3[0]()
    assert revoked_keys == [k1]


def test_f14_validate_requested_partitions_rejection_branches(tmp_path: Path) -> None:
    """F14/EVA-05: Cover every error branch of InstallationAuthority._validate_requested_partitions."""

    authority = authority_at(tmp_path)
    worker_cert = SimpleNamespace(runtime="worker-1", roles=frozenset({"worker_host"}), teams=frozenset({"team_a"}))
    gw_cert = SimpleNamespace(runtime="gateway", roles=frozenset({"gateway"}), teams=frozenset({"team_a"}))
    multi_team_cert = SimpleNamespace(
        runtime="worker-bad", roles=frozenset({"worker_host"}), teams=frozenset({"team_a", "team_b"})
    )

    assert authority._validate_requested_partitions(worker_cert, None, give_up=False, owned_by_team={}, released_by_team={}) == ()  # type: ignore[arg-type]
    assert authority._validate_requested_partitions(worker_cert, [], give_up=False, owned_by_team={}, released_by_team={}) == ()  # type: ignore[arg-type]

    for bad_asked in ("0", 0, {0: 1}):
        with pytest.raises(FrameRefused) as refused:
            authority._validate_requested_partitions(
                worker_cert, bad_asked, give_up=False, owned_by_team={"team_a": {0: 1}}, released_by_team={}  # type: ignore[arg-type]
            )
        assert refused.value.code == "invalid_body" and refused.value.at == "/body/partitions"

    with pytest.raises(FrameRefused, match="repeat") as dup:
        authority._validate_requested_partitions(
            worker_cert, [0, 0], give_up=False, owned_by_team={"team_a": {0: 1}}, released_by_team={}  # type: ignore[arg-type]
        )
    assert dup.value.at == "/body/partitions"

    for bad_num in ([authority.partitions], [-1], [True], ["0"]):
        with pytest.raises(FrameRefused, match="outside") as oob:
            authority._validate_requested_partitions(
                worker_cert, bad_num, give_up=False, owned_by_team={"team_a": {0: 1}}, released_by_team={}  # type: ignore[arg-type]
            )
        assert oob.value.at == "/body/partitions"

    with pytest.raises(FrameRefused, match="giving up") as gu:
        authority._validate_requested_partitions(
            worker_cert, [0], give_up=True, owned_by_team={"team_a": {0: 1}}, released_by_team={}  # type: ignore[arg-type]
        )
    assert gu.value.at == "/body/partitions"

    with pytest.raises(FrameRefused, match="lead_host or worker_host") as role_err:
        authority._validate_requested_partitions(
            gw_cert, [0], give_up=False, owned_by_team={"team_a": {0: 1}}, released_by_team={}  # type: ignore[arg-type]
        )
    assert role_err.value.at == "/body/partitions"

    with pytest.raises(FrameRefused, match="exactly one team") as multi_err:
        authority._validate_requested_partitions(
            multi_team_cert, [0], give_up=False, owned_by_team={"team_a": {0: 1}}, released_by_team={}  # type: ignore[arg-type]
        )
    assert multi_err.value.at == "/body/partitions"

    with pytest.raises(FrameRefused, match="owned partition record") as rel_err:
        authority._validate_requested_partitions(
            worker_cert, [0], give_up=False, owned_by_team={"team_a": {0: 1}}, released_by_team={"team_a": {0: (1, "operator")}}  # type: ignore[arg-type]
        )
    assert rel_err.value.at == "/body/partitions"

    assert authority._validate_requested_partitions(
        worker_cert, [2, 0], give_up=False, owned_by_team={"team_a": {0: 10, 2: 11}}, released_by_team={}  # type: ignore[arg-type]
    ) == (0, 2)


@pytest.mark.asyncio
async def test_authority_sweep_delivery_limits_and_install_dead_letter_routing(tmp_path: Path) -> None:
    """A4 & A5: Authority sweeps each served team's UA1_TEAM_WORK_<team> into that team's UA1_DLQ_<team>,
    routes _install traffic to its first served team, and isolates sweep failures in upkeep."""

    from custom_transport.protocol1.dead_letters import dlq_notice_subject, dlq_raw_subject
    from custom_transport.protocol1.identity import HostIdentity
    from custom_transport.protocol1.live import LiveHost
    from custom_transport.protocol1.trust_state import PersistentTrustStore
    from tests import message_contract_support as fixtures
    from tests.test_protocol1_dead_letters import FakeNatsConnection

    authority = authority_at(tmp_path, partitions=2)
    nc = FakeNatsConnection()
    nc.is_closed = False  # type: ignore[attr-defined]
    bundle = BootstrapBundle(
        authority.installation,
        ((authority.authority.key_id, authority.authority.public_key_text),),
        partitions=2,
    )
    trust = authority._trust = PersistentTrustStore.open(
        bundle, authority.layout.host_trust_dir(AUTHORITY_RUNTIME), fixtures.NOW
    )
    own_req = authority._own_request(("team_beta", "team_alpha"))
    own_rec = authority.certificate_record(own_req, epoch=1, revocation_epoch=1, issued=fixtures.NOW)
    trust.add(json.dumps(own_rec).encode("utf-8"), now=fixtures.NOW)
    ident = HostIdentity(authority.host_keys, authority._held_certificate(own_rec))
    live = LiveHost(nc, ident, trust, clock=lambda: fixtures.NOW)
    authority.live = live
    authority._nc = nc
    authority._teams = ("team_alpha", "team_beta")
    authority._own = own_rec
    authority._revocations = {"issued_at": format_timestamp(fixtures.NOW), "body": {"revocation_epoch": 1, "entries": []}}
    authority._forgotten_at = fixtures.NOW

    # 1. dead_letter_setter routes _install traffic to sorted(certificate.teams)[0] ("team_alpha").
    setter = authority.dead_letter_setter()
    assert setter.resolve_notice_team("_install") == "team_alpha"
    assert setter.resolve_notice_team(None) == "team_alpha"

    # 2. Populate UA1_TEAM_WORK_team_alpha and UA1_TEAM_WORK_team_beta with stranded exhausted messages.
    for team_name, payload in (("team_alpha", b"alpha-stranded"), ("team_beta", b"beta-stranded")):
        stream = f"UA1_TEAM_WORK_{team_name}"
        nc.streams[stream] = {"created": "2026-10-22T04:00:00Z"}
        nc.messages[stream] = {
            1: {"subject": f"ua.v1.{team_name}.lead-host-1.work.team", "data": payload, "time": "2026-10-22T04:30:00Z"},
        }
        nc.consumers[stream] = [
            {
                "name": f"team~{team_name}",
                "config": {"filter_subject": f"ua.v1.{team_name}.*.work.team", "max_deliver": 20},
                "delivered": {"stream_seq": 1},
                "ack_floor": {"stream_seq": 0},
                "num_ack_pending": 0,
            }
        ]

    summary = await authority.sweep_delivery_limits()
    assert summary.streams_checked == 2
    assert summary.streams_failed == 0
    assert summary.messages_swept == 2
    assert nc.messages["UA1_TEAM_WORK_team_alpha"] == {}
    assert nc.messages["UA1_TEAM_WORK_team_beta"] == {}

    published_subjects = [subj for subj, _data, _hdrs in nc.jetstream().published]
    assert any(subj.startswith("ua.v1.team_alpha.authority.dlq.raw.") for subj in published_subjects)
    assert any(subj.startswith("ua.v1.team_alpha.authority.dlq.notice.") for subj in published_subjects)
    assert any(subj.startswith("ua.v1.team_beta.authority.dlq.raw.") for subj in published_subjects)
    assert any(subj.startswith("ua.v1.team_beta.authority.dlq.notice.") for subj in published_subjects)
    # Silence unused import check when verifying subject prefixes directly.
    assert dlq_raw_subject("team_alpha", "authority", "0" * 64).startswith("ua.v1.team_alpha.authority.dlq.raw.")
    assert dlq_notice_subject("team_beta", "authority", "0" * 64).startswith("ua.v1.team_beta.authority.dlq.notice.")

    # 3. upkeep() schedules sweep_delivery_limits when due, isolates failures, and retries on next minute after failure.
    calls: list[str] = []
    fail_first = {"flag": True}

    async def fake_sweep() -> Any:
        calls.append("sweep")
        if fail_first["flag"]:
            fail_first["flag"] = False
            raise RuntimeError("transient nats hiccup")
        return summary

    authority.sweep_delivery_limits = fake_sweep  # type: ignore[method-assign,assignment]
    await authority.upkeep(fixtures.NOW)
    assert calls == ["sweep"]
    assert authority._dlq_swept_at is None  # failed step does not advance timestamp -> retries next minute!
    await authority.upkeep(fixtures.NOW + timedelta(minutes=1))
    assert calls == ["sweep", "sweep"]
    assert authority._dlq_swept_at == fixtures.NOW + timedelta(minutes=1)
    # Within DLQ_SWEEP_INTERVAL (5 min), upkeep does not re-sweep; after 5 min, it sweeps again.
    await authority.upkeep(fixtures.NOW + timedelta(minutes=3))
    assert len(calls) == 2
    await authority.upkeep(fixtures.NOW + timedelta(minutes=6))
    assert len(calls) == 3
