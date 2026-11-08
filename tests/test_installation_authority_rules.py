"""The installation authority's rules that need no NATS server.

installation_authority.service (the launcher's list at join and renewal, reuse),
installation_authority.ledger (what the authority keeps on disk),
installation_authority.expected_hosts, installation_authority.process (how it exits),
and custom_messages.wire.trust.kept_for. The authority on a real server is in
tests/test_live_installation_authority.py.
"""

from __future__ import annotations

import asyncio
import json
import logging
from datetime import timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from custom_messages.wire.trust import kept_for
from custom_transport.protocol1 import host_keys
from custom_transport.protocol1.live import utc_now
from installation_authority import process as authority_process
from installation_authority.expected_hosts import ExpectedHost, read_expected_hosts, write_expected_hosts
from installation_authority.ledger import ISSUED_FILENAME, OUTCOME_RETENTION, IssueLedger
from installation_authority.service import (
    AUTHORITY_RUNTIME,
    AuthorityDisconnected,
    CertificateRequest,
    InstallationAuthority,
    RevocationsUnverified,
    _denial,
    _renewal_refusal,
)
from runtime_settings.instance_layout import InstanceLayout

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
    assert _denial(LISTED, join_body(**changes)) == reason


def test_a_host_nobody_listed_is_not_approved() -> None:
    assert _denial(None, join_body()) == "not_approved"


def certificate(**changes: Any) -> Any:
    values: dict[str, Any] = {"runtime": "worker-host-1", "teams": frozenset({"team_one"}),
                              "roles": frozenset({"worker_host"}), "agent_types": frozenset({"researcher"}),
                              "session_limit": 4, "connection_class": "connected"}
    values.update(changes)
    return SimpleNamespace(**values)


KEPT_KEY = {"key_id": "worker.key1", "public_key": "PUBLIC1"}


def test_a_renewal_is_checked_against_the_launchers_list() -> None:
    assert _renewal_refusal(LISTED, certificate(), KEPT_KEY) is None
    assert _renewal_refusal(LISTED, certificate(agent_types=frozenset()), KEPT_KEY) is None
    assert "no longer lists" in str(_renewal_refusal(None, certificate(), KEPT_KEY))
    assert "another key" in str(_renewal_refusal(LISTED, certificate(), {"key_id": "worker.key2",
                                                                           "public_key": "PUBLIC2"}))
    for wider in ({"agent_types": frozenset({"researcher", "analyst"})}, {"session_limit": 5},
                  {"teams": frozenset({"team_one", "team_two"})}, {"roles": frozenset({"worker_host", "lead_host"})},
                  {"connection_class": "intermittent"}):
        assert "join again" in str(_renewal_refusal(LISTED, certificate(**wider), KEPT_KEY)), wider


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


def authority_at(tmp_path: Path) -> InstallationAuthority:
    layout = InstanceLayout.at(tmp_path / "instance")
    authority, _ = host_keys.ensure_authority_keys(layout, "install_one")
    keys, _ = host_keys.ensure_host_keys(layout, AUTHORITY_RUNTIME)
    return InstallationAuthority(layout, authority, keys)


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
    assert caplog.text.count("event=revocations_resign_failed") == 2


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
                           transport=SimpleNamespace(replicas=1, nats_servers=["nats://127.0.0.1:1"], nats_url=None,
                                                     subject_prefix="team", nats_user=None, nats_password=None,
                                                     credentials_file=None))


def _run_with(monkeypatch, tmp_path: Path, authority: Any) -> None:
    monkeypatch.setattr(authority_process, "NatsTransportAdapter", _Adapter)
    monkeypatch.setattr(authority_process, "InstallationAuthority", lambda *_args, **_kwargs: authority)
    monkeypatch.setattr(authority_process, "load_authority_keys", lambda _layout: None)
    monkeypatch.setattr(authority_process, "load_host_keys", lambda _layout, _runtime: None)
    asyncio.run(authority_process.run_installation_authority(_config(tmp_path)))


def test_the_process_exits_with_a_reason_when_it_cannot_start_safely(tmp_path: Path, monkeypatch, caplog) -> None:
    class Refusing:
        async def start(self, _nc: Any, *, teams: Any) -> None:
            raise RevocationsUnverified("Restore the instance's trust folder.")

    caplog.set_level(logging.ERROR)
    with pytest.raises(SystemExit) as exited:
        _run_with(monkeypatch, tmp_path, Refusing())
    assert exited.value.code == 1
    assert "event=installation_authority_start_refused reason=RevocationsUnverified" in caplog.text
    assert "Restore the instance's trust folder." in caplog.text


def test_the_process_exits_when_its_upkeep_stops(tmp_path: Path, monkeypatch, caplog) -> None:
    class Disconnecting:
        async def start(self, _nc: Any, *, teams: Any) -> None:
            pass

        async def keep_current(self) -> None:
            raise AuthorityDisconnected("closed")

    with pytest.raises(SystemExit) as exited:
        _run_with(monkeypatch, tmp_path, Disconnecting())
    assert exited.value.code == 1
    assert "event=installation_authority_upkeep_stopped" in caplog.text
