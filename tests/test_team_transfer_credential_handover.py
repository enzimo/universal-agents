"""The credentials onboarding's Import leaves for the first start (``team_transfer.credential_handover``).

The archive is a real transfer file whose credential section a stand-in
broker wrote; storing goes through a replaced ``import_credential_bundle``, so
these tests cover waiting for the broker, refusals and the file's lifetime.
The whole path through a real broker is in ``test_onboarding_team_import``.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from storage.access_repository import AccessRepository
from team_transfer import broker_credentials, credential_handover
from team_transfer.broker_credentials import BrokerCredentialsSection, BrokerUnavailable, CredentialImportReport
from team_transfer.container import TransferReader
from team_transfer.credential_handover import (
    import_handed_over_credentials,
    read_credential_handover,
    write_credential_handover,
)
from team_transfer.crypto import new_key_derivation
from team_transfer.errors import TransferError
from team_transfer.service import export_source, export_team
from tests.pinned_memory_support import new_instance

PASSPHRASE = "correct horse battery staple"
TEAM = "team_a"
KEY = "person:usr_ana/stored_value/github.sign_in"


@pytest.fixture
def handed_over(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """A destination instance holding a hand-over file written from a real export."""

    def export_reply(_endpoint, _route, _payload):
        return {
            "schema_version": BrokerCredentialsSection.schema_version,
            "ciphertext_b64": "c2VhbGVkIGNyZWRlbnRpYWxz",
            "record_count": 1,
            "index": [{"key": KEY, "owner": {"kind": "person", "owner_id": "usr_ana"}}],
        }

    monkeypatch.setattr(broker_credentials, "call_broker", export_reply)
    monkeypatch.setattr(broker_credentials, "broker_endpoint", lambda *_args: None)
    monkeypatch.setattr(broker_credentials, "_people", lambda _source: ["usr_ana"])
    old_path, _old = new_instance(tmp_path, "alpha", TEAM)
    out = tmp_path / "team.uatransfer"
    export_team(
        export_source(old_path),
        out,
        PASSPHRASE,
        owner_person_id="usr_ana",
        sections=(BrokerCredentialsSection(),),
        key_derivation=new_key_derivation(opslimit=1, memlimit=8192),
    )
    config_path, config = new_instance(tmp_path, "new", TEAM)
    with TransferReader.open(out, PASSPHRASE) as reader:
        path = write_credential_handover(reader, config.layout, [KEY])
    return config_path, config, path


def _report(*outcomes: dict) -> CredentialImportReport:
    return CredentialImportReport(outcomes=list(outcomes), notified=[])


def test_the_file_is_private_and_holds_no_passphrase(handed_over) -> None:
    _config_path, _config, path = handed_over

    assert oct(path.stat().st_mode & 0o777) == "0o600"
    assert PASSPHRASE not in path.read_text()
    bundle, selected = read_credential_handover(path)
    assert selected == [KEY] and bundle.ciphertext == b"sealed credentials"


def test_a_changed_file_is_refused(handed_over) -> None:
    _config_path, _config, path = handed_over
    value = json.loads(path.read_text())
    value["ciphertext_b64"] = "b3RoZXIgYnl0ZXM="
    path.write_text(json.dumps(value))

    with pytest.raises(TransferError, match="do not match their export"):
        read_credential_handover(path)


def test_nothing_is_selected_means_no_file(tmp_path: Path) -> None:
    _path, config = new_instance(tmp_path, "new", TEAM)

    assert write_credential_handover(object(), config.layout, []) is None
    assert not config.layout.team_import_credentials_path.exists()


def test_the_step_waits_for_the_broker_then_stores_and_deletes_the_file(handed_over, monkeypatch) -> None:
    config_path, config, path = handed_over
    attempts, waits = [], []

    def store(bundle, _config_path, *, selected_keys):
        attempts.append(selected_keys)
        if len(attempts) < 3:
            raise BrokerUnavailable("not yet")
        return _report({"key": KEY, "imported": True, "owner": {"kind": "person", "owner_id": "usr_ana"}})

    monkeypatch.setattr(credential_handover, "import_credential_bundle", store)
    report = import_handed_over_credentials(str(config_path), retry_delays=(0.0, 5.0, 10.0), sleep=waits.append)

    assert report is not None and attempts == [[KEY]] * 3 and waits == [5.0, 10.0]
    assert not path.exists()
    [written] = config.layout.team_import_dir.glob("credentials-*.json")
    assert json.loads(written.read_text())["outcomes"][0] == {
        "key": KEY, "imported": True, "owner": {"kind": "person", "owner_id": "usr_ana"},
    }


def test_a_broker_that_never_answers_keeps_the_file_for_the_next_start(handed_over, monkeypatch) -> None:
    config_path, _config, path = handed_over

    def store(*_args, **_kwargs):
        raise BrokerUnavailable("connection refused")

    monkeypatch.setattr(credential_handover, "import_credential_bundle", store)
    with pytest.raises(TransferError, match="the next start tries again"):
        import_handed_over_credentials(str(config_path), retry_delays=(0.0, 0.0), sleep=lambda _s: None)
    assert path.exists()


def test_a_broker_refusal_keeps_the_file(handed_over, monkeypatch) -> None:
    config_path, _config, path = handed_over

    def store(*_args, **_kwargs):
        raise TransferError("The broker refused the request (400)")

    monkeypatch.setattr(credential_handover, "import_credential_bundle", store)
    with pytest.raises(TransferError, match="refused"):
        import_handed_over_credentials(str(config_path), retry_delays=(0.0,), sleep=lambda _s: None)
    assert path.exists()


def test_credentials_the_broker_did_not_store_are_reported_to_the_exporting_owner(handed_over, monkeypatch) -> None:
    config_path, config, path = handed_over
    monkeypatch.setattr(
        credential_handover,
        "import_credential_bundle",
        lambda *_args, **_kwargs: _report(
            {"key": KEY, "imported": False, "reason": "kept the current credential",
             "owner": {"kind": "person", "owner_id": "usr_ana"}}
        ),
    )

    import_handed_over_credentials(str(config_path), retry_delays=(0.0,), sleep=lambda _s: None)

    assert not path.exists(), "retrying cannot change the broker's answer"
    notices = AccessRepository(config.manager_storage_settings).notices_for(TEAM, person_id="usr_ana", roles=())
    [(notice, _read)] = notices
    assert notice.title == "Some sign-ins and keys did not move"
    assert f"{KEY}: kept the current credential" in notice.summary


def test_without_a_file_the_step_does_nothing(tmp_path: Path) -> None:
    config_path, _config = new_instance(tmp_path, "new", TEAM)

    assert import_handed_over_credentials(str(config_path)) is None


def test_retry_preserves_completed_outcomes_and_only_resends_retryable_records(handed_over, monkeypatch) -> None:
    config_path, _config, path = handed_over
    completed = "team/model_key/https://api.example/v1"
    value = json.loads(path.read_text())
    value["selected_keys"] = [KEY, completed]
    path.write_text(json.dumps(value))
    attempts = []

    def store(_bundle, _config_path, *, selected_keys):
        attempts.append(selected_keys)
        if len(attempts) == 1:
            return _report(
                {"key": completed, "imported": True, "owner": {"kind": "team", "owner_id": TEAM}},
                {"key": KEY, "imported": False, "retryable": True, "reason": "temporary store failure",
                 "owner": {"kind": "person", "owner_id": "usr_ana"}},
            )
        return _report({"key": KEY, "imported": True, "owner": {"kind": "person", "owner_id": "usr_ana"}})

    monkeypatch.setattr(credential_handover, "import_credential_bundle", store)
    report = import_handed_over_credentials(str(config_path), retry_delays=(0.0, 0.0))

    assert report is not None and {r["key"] for r in report.outcomes} == {completed, KEY}
    assert all(r["imported"] for r in report.outcomes)
    assert attempts == [[KEY, completed], [KEY]]
    assert not path.exists()


def test_finish_import_cli_completes_when_the_destination_credential_is_kept(handed_over, monkeypatch, capsys) -> None:
    from argparse import Namespace
    from team_transfer.cli import _finish_import

    config_path, _config, path = handed_over
    monkeypatch.setattr(
        credential_handover,
        "import_credential_bundle",
        lambda *_args, **_kwargs: _report(
            {"key": KEY, "imported": False, "retryable": False,
             "reason": "already stored in this instance; kept the current credential",
             "owner": {"kind": "person", "owner_id": "usr_ana"}}
        ),
    )

    _finish_import(Namespace(config=str(config_path)))

    assert not path.exists()
    assert "Stored 0 of 1" in capsys.readouterr().out
