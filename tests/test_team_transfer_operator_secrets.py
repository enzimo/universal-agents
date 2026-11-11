"""The ``operator_secrets`` transfer section carries operator-supplied secrets, encrypted."""

from __future__ import annotations

import stat
from pathlib import Path

import pytest

from assistant_gateway.instance_secrets import NATS_CREDENTIAL_ENV_NAMES, NATS_DECISION_ENV_NAMES
from internal_credentials import parse_env_file
from team_transfer.container import TransferReader
from team_transfer.crypto import new_key_derivation
from team_transfer.operator_secrets import OperatorSecretsSection
from team_transfer.sections import RecordOutcome, read_section_records
from team_transfer.service import export_source, export_team, import_team
from team_transfer.settings import SettingsSection
from tests.pinned_memory_support import new_instance

PASSPHRASE = "correct horse battery staple"
FAST_KEYS = {"opslimit": 1, "memlimit": 8192}
SECTIONS = (SettingsSection(), OperatorSecretsSection())
BOT_TOKEN = "123456:telegram-bot-token-value"
MODEL_KEY = "sk-operator-model-key-value"
OLD_SIGNING_KEY = "gateway-old:old-signing-seed"
CODEX_AUTH = '{"tokens": {"refresh_token": "codex-refresh-value"}}'


@pytest.fixture(autouse=True)
def _no_nats_variables_from_the_shell(monkeypatch: pytest.MonkeyPatch) -> None:
    # The shell running the tests may set any of these; a test that needs one sets it.
    for name in (*NATS_DECISION_ENV_NAMES, *NATS_CREDENTIAL_ENV_NAMES):
        monkeypatch.delenv(name, raising=False)


def _export(config_path: Path, out: Path) -> None:
    export_team(
        export_source(config_path), out, PASSPHRASE, owner_person_id="usr_owner", sections=SECTIONS,
        key_derivation=new_key_derivation(**FAST_KEYS),
    )


def _carried(out: Path) -> dict[str, str]:
    """The operator secrets an export file carries, by name."""

    with TransferReader.open(out, PASSPHRASE) as reader:
        records = read_section_records(reader, "operator_secrets", OperatorSecretsSection.schema_version)
    return {record["name"]: record["value"] for record in records}


def _old_instance(tmp_path: Path) -> Path:
    path, config = new_instance(tmp_path, "alpha", "team_a")
    config.layout.team_env_path.write_text(
        f"TELEGRAM_BOT_TOKEN={BOT_TOKEN}\nOPENAI_API_KEY={MODEL_KEY}\nACCESS_GATEWAY_SIGNING_KEY={OLD_SIGNING_KEY}\n"
    )
    config.layout.openai_codex_auth_path.parent.mkdir(parents=True)
    config.layout.openai_codex_auth_path.write_text(CODEX_AUTH)
    return path


def test_operator_secrets_travel_encrypted_and_generated_keys_are_made_fresh(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("OPENAI_API_KEY", "the-operators-personal-shell-key")
    out = tmp_path / "team.uatransfer"
    export_team(
        export_source(_old_instance(tmp_path)), out, PASSPHRASE, owner_person_id="usr_owner", sections=SECTIONS,
        key_derivation=new_key_derivation(**FAST_KEYS),
    )
    raw = out.read_bytes()
    for secret in (BOT_TOKEN, MODEL_KEY, "codex-refresh-value"):
        assert secret.encode() not in raw

    with TransferReader.open(out, PASSPHRASE) as reader:
        from team_transfer.sections import read_section_records

        carried = {r["name"]: r["value"] for r in read_section_records(reader, "operator_secrets", OperatorSecretsSection.schema_version)}
    assert carried["OPENAI_API_KEY"] == MODEL_KEY, "the instance's team.env, not the operator's own shell"
    assert "ACCESS_GATEWAY_SIGNING_KEY" not in carried

    target = tmp_path / "new"
    target.mkdir()
    # A value the operator already put in the new team.env wins.
    (target / "team.env").write_text("OPENAI_API_KEY=new-instance-own-key\n")
    with TransferReader.open(out, PASSPHRASE) as reader:
        plan = import_team(reader, target, dry_run=False, sections=SECTIONS)

    outcomes = {r.key: r for r in plan.records if r.section == "operator_secrets"}
    assert set(outcomes) == {"environment/TELEGRAM_BOT_TOKEN", "environment/OPENAI_API_KEY", "auth_file/openai_codex_auth.json"}
    assert outcomes["environment/OPENAI_API_KEY"].outcome is RecordOutcome.ATTENTION
    for record in plan.records:
        assert BOT_TOKEN not in record.reason and MODEL_KEY not in record.reason

    env = parse_env_file(target / "team.env")
    assert env["TELEGRAM_BOT_TOKEN"] == BOT_TOKEN
    assert env["OPENAI_API_KEY"] == "new-instance-own-key"
    assert env["ACCESS_GATEWAY_SIGNING_KEY"] and env["ACCESS_GATEWAY_SIGNING_KEY"] != OLD_SIGNING_KEY
    codex = target / "auth" / "openai_codex_auth.json"
    assert codex.read_text() == CODEX_AUTH and stat.S_IMODE(codex.stat().st_mode) == 0o600
    report = next((target / "runtime" / "team-import").glob("report-*.json")).read_text()
    assert BOT_TOKEN not in report and MODEL_KEY not in report and "codex-refresh-value" not in report


EXTERNAL_NATS = "nats://external.example:4222"
LOCAL_NATS = "nats://127.0.0.1:4222"


@pytest.mark.parametrize(
    ("saved_nats_url", "team_env_nats_url", "shell_nats_url", "carried"),
    [
        pytest.param(EXTERNAL_NATS, None, LOCAL_NATS, True, id="external-instance-local-shell"),
        pytest.param(LOCAL_NATS, EXTERNAL_NATS, LOCAL_NATS, True, id="external-in-team-env-local-shell"),
        pytest.param(LOCAL_NATS, None, EXTERNAL_NATS, False, id="local-instance-external-shell"),
    ],
)
def test_the_instance_not_the_commands_environment_decides_whether_nats_credentials_move(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, saved_nats_url: str, team_env_nats_url: str | None, shell_nats_url: str, carried: bool
) -> None:
    # The operator's shell may name a NATS server the instance does not use.
    # Credentials for an external server move; the ones generated for a local
    # server are made fresh by the new instance.
    monkeypatch.setenv("NATS_URL", shell_nats_url)
    monkeypatch.setenv("NATS_SERVERS", shell_nats_url)
    path, config = new_instance(tmp_path, "alpha", "team_a")
    config.transport.nats_url = saved_nats_url
    config.transport.nats_servers = [saved_nats_url]
    config.save(path)
    team_env = "NATS_USER=nats-user-value\nNATS_PASSWORD=nats-password-value\n"
    if team_env_nats_url:
        team_env += f"NATS_URL={team_env_nats_url}\n"
    config.layout.team_env_path.write_text(team_env)
    out = tmp_path / "team.uatransfer"
    export_team(
        export_source(path), out, PASSPHRASE, owner_person_id="usr_owner", sections=SECTIONS,
        key_derivation=new_key_derivation(**FAST_KEYS),
    )

    with TransferReader.open(out, PASSPHRASE) as reader:
        from team_transfer.sections import read_section_records

        names = {r["name"] for r in read_section_records(reader, "operator_secrets", OperatorSecretsSection.schema_version)}
    assert names & {"NATS_USER", "NATS_PASSWORD"} == ({"NATS_USER", "NATS_PASSWORD"} if carried else set())


def test_with_secrets_from_the_environment_the_commands_environment_is_the_instances(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Run inside the instance's container, the export sees its environment.
    for name, value in (
        ("NATS_URL", EXTERNAL_NATS), ("NATS_SERVERS", EXTERNAL_NATS),
        ("NATS_USER", "nats-user-value"), ("NATS_PASSWORD", "nats-password-value"),
    ):
        monkeypatch.setenv(name, value)
    path, config = new_instance(tmp_path, "alpha", "team_a")
    config.launch.secrets_source = "environment"
    config.save(path)
    out = tmp_path / "team.uatransfer"
    export_team(
        export_source(path), out, PASSPHRASE, owner_person_id="usr_owner", sections=SECTIONS,
        key_derivation=new_key_derivation(**FAST_KEYS),
    )

    with TransferReader.open(out, PASSPHRASE) as reader:
        from team_transfer.sections import read_section_records

        carried = {r["name"]: r["value"] for r in read_section_records(reader, "operator_secrets", OperatorSecretsSection.schema_version)}
    assert carried["NATS_USER"] == "nats-user-value" and carried["NATS_PASSWORD"] == "nats-password-value"


@pytest.mark.parametrize(
    "unread_line",
    [
        pytest.param("NATS_ACK_WAIT_SECONDS=thirty", id="malformed-ack-wait"),
        pytest.param("NATS_PASSWORD_FILE=/run/secrets/only-inside-the-container", id="password-file-elsewhere"),
        pytest.param("USER_DOCS_BASE_URL=https://docs.example.invalid", id="removed-variable"),
    ],
)
def test_a_local_instance_exports_team_env_values_the_nats_decision_never_reads(
    tmp_path: Path, unread_line: str
) -> None:
    # The decision reads only the NATS addresses and the credentials file from
    # team.env. Applying all of team.env to the saved settings made the export
    # fail on lines like these, which the instance's export accepted before.
    path, config = new_instance(tmp_path, "alpha", "team_a")
    config.layout.team_env_path.write_text(
        f"TELEGRAM_BOT_TOKEN={BOT_TOKEN}\nNATS_USER=nats-user-value\n{unread_line}\n"
    )
    out = tmp_path / "team.uatransfer"
    _export(path, out)

    carried = _carried(out)
    assert carried["TELEGRAM_BOT_TOKEN"] == BOT_TOKEN
    assert not set(carried) & set(NATS_CREDENTIAL_ENV_NAMES), "a local server's login is generated anew"
