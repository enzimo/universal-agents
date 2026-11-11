"""The ``operator_secrets`` transfer section carries operator-supplied secrets, encrypted."""

from __future__ import annotations

import logging
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


# -- what the export tells the operator about the NATS login ------------------------

SHELL_NATS_USER = "shell-nats-user"
SHELL_NATS_PASSWORD = "shell-nats-password"
# A URL may hold a user and password, so no notice or log may repeat one.
SHELL_EXTERNAL_NATS = f"nats://{SHELL_NATS_USER}:{SHELL_NATS_PASSWORD}@shell-nats.example.invalid:4222"
MALFORMED_NATS = "nats://[::1"
INSTANCE_NATS_LOGIN = "NATS_USER=nats-user-value\nNATS_PASSWORD=nats-password-value\n"
NOTICE_EVENTS = ("team_transfer_nats_environment_differs", "team_transfer_nats_environment_unreadable")


def _cli_export(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    team_env: str,
    shell: dict[str, str],
    saved_nats_url: str | None = None,
    secrets_source: str | None = None,
) -> Path:
    """Runs ``assistant_cli team export`` for the settings and operator secrets sections."""

    import team_transfer.registry as registry
    from team_transfer import crypto
    from tests.test_team_transfer import _owner_instance, _run_cli

    monkeypatch.setattr(registry, "TRANSFER_SECTIONS", SECTIONS)
    monkeypatch.setattr(crypto, "DEFAULT_OPSLIMIT", FAST_KEYS["opslimit"])
    monkeypatch.setattr(crypto, "DEFAULT_MEMLIMIT", FAST_KEYS["memlimit"])
    path, config = _owner_instance(tmp_path)
    if saved_nats_url:
        config.transport.nats_url = saved_nats_url
        config.transport.nats_servers = [saved_nats_url]
    if secrets_source:
        config.launch.secrets_source = secrets_source
    config.save(path)
    config.layout.team_env_path.write_text(f"TELEGRAM_BOT_TOKEN={BOT_TOKEN}\n{INSTANCE_NATS_LOGIN}{team_env}")
    for name, value in shell.items():
        monkeypatch.setenv(name, value)
    secret = tmp_path / "passphrase.txt"
    secret.write_text(PASSPHRASE + "\n")
    out = tmp_path / "team.uatransfer"
    _run_cli("export", "--config", str(path), "--owner", "usr_owner", "--out", str(out), "--passphrase-file", str(secret))
    return out


@pytest.mark.parametrize(
    ("team_env", "shell", "carried"),
    [
        pytest.param("", {"NATS_URL": SHELL_EXTERNAL_NATS}, False, id="local-instance-external-shell-url"),
        pytest.param(
            "", {"NATS_URL": SHELL_EXTERNAL_NATS, "NATS_SERVERS": SHELL_EXTERNAL_NATS}, False,
            id="local-instance-external-shell-servers-and-url",
        ),
        pytest.param("", {"NATS_CREDENTIALS_FILE": "/run/secrets/shell-nats.creds"}, False, id="local-instance-shell-credentials-file"),
        pytest.param(f"NATS_URL={EXTERNAL_NATS}\n", {"NATS_URL": LOCAL_NATS}, True, id="external-in-team-env-local-shell"),
    ],
)
def test_the_export_says_when_the_commands_nats_variables_would_have_decided_otherwise(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    caplog: pytest.LogCaptureFixture,
    team_env: str,
    shell: dict[str, str],
    carried: bool,
) -> None:
    caplog.set_level(logging.INFO)
    out = _cli_export(tmp_path, monkeypatch, team_env=team_env, shell=shell)

    printed = capsys.readouterr()
    lines = printed.out.splitlines()
    notices = [line for line in lines if "NATS" in line]
    assert len(notices) == 1, printed.out
    notice = notices[0]
    assert lines.index(notice) < next(i for i, line in enumerate(lines) if line.startswith("Stop this instance now"))
    for name in shell:
        assert f"{name}" in notice
    assert "this instance's own settings (instance_config.yaml and team.env)" in notice
    warnings = [record.getMessage() for record in caplog.records if "event=team_transfer_nats_environment_differs" in record.getMessage()]
    assert len(warnings) == 1 and "decided_by=instance_settings" in warnings[0]
    assert f"command_names={','.join(name for name in NATS_DECISION_ENV_NAMES if name in shell)}" in warnings[0]
    if carried:
        assert notice.startswith("NATS_USER and NATS_PASSWORD were carried: ")
        assert "would mean a local server" in notice and "the import says how" in notice
        assert "instance_nats=external carried_names=NATS_USER,NATS_PASSWORD" in warnings[0]
        assert set(NATS_CREDENTIAL_ENV_NAMES) <= set(_carried(out))
    else:
        assert notice.startswith("NATS_USER and NATS_PASSWORD were left out: ")
        assert "would mean an external server" in notice
        # The step is conditional: a stray shell value must not be copied into team.env.
        assert "If this instance really runs with" in notice
        assert ("add it to" if len(shell) == 1 else "add them to") in notice
        assert "TEAM_LOCAL_NATS_ENABLED=false" in notice and "left over from another setup, nothing was lost" in notice
        assert "instance_nats=local carried_names=none" in warnings[0]
        assert not set(NATS_CREDENTIAL_ENV_NAMES) & set(_carried(out))
    _assert_no_values(printed.out + printed.err + caplog.text, team_env, shell)


@pytest.mark.parametrize(
    ("team_env", "shell", "saved_nats_url", "secrets_source"),
    [
        pytest.param(f"NATS_URL={EXTERNAL_NATS}\n", {}, None, None, id="clean-shell"),
        pytest.param("", {"NATS_URL": "nats://localhost:4222"}, None, None, id="both-local"),
        pytest.param("", {"NATS_URL": SHELL_EXTERNAL_NATS}, EXTERNAL_NATS, None, id="both-external"),
        pytest.param(
            f"NATS_SERVERS={EXTERNAL_NATS}\n", {"NATS_URL": LOCAL_NATS}, None, None,
            id="team-env-servers-outrank-the-shells-url",
        ),
        pytest.param(
            "NATS_CREDENTIALS_FILE=/run/secrets/nats.creds\n", {"NATS_URL": LOCAL_NATS}, None, None,
            id="team-env-credentials-file-outranks-the-shells-url",
        ),
        pytest.param("", {"NATS_URL": SHELL_EXTERNAL_NATS}, None, "environment", id="secrets-from-the-environment"),
    ],
)
def test_the_export_says_nothing_about_nats_when_the_command_would_decide_the_same(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    caplog: pytest.LogCaptureFixture,
    team_env: str,
    shell: dict[str, str],
    saved_nats_url: str | None,
    secrets_source: str | None,
) -> None:
    # The launcher lays the container's environment over team.env name by name,
    # and the rule reads NATS_CREDENTIALS_FILE, then NATS_SERVERS, then
    # NATS_URL; a command value that would not change the decision is not news.
    # With secrets from the environment, the command's environment is the instance's.
    caplog.set_level(logging.INFO)
    _cli_export(
        tmp_path, monkeypatch, team_env=team_env, shell=shell, saved_nats_url=saved_nats_url, secrets_source=secrets_source
    )

    printed = capsys.readouterr()
    assert "NATS" not in printed.out
    assert not any(event in caplog.text for event in NOTICE_EVENTS)
    _assert_no_values(printed.out + printed.err + caplog.text, team_env, shell)


def test_a_nats_address_the_command_cannot_read_is_reported_and_does_not_stop_the_export(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], caplog: pytest.LogCaptureFixture
) -> None:
    # A typo in the operator's shell is not the instance's setting; it must not
    # stop the export (should_generate_internal_nats_credentials raises on it).
    caplog.set_level(logging.INFO)
    out = _cli_export(tmp_path, monkeypatch, team_env="", shell={"NATS_URL": MALFORMED_NATS})

    printed = capsys.readouterr()
    assert out.is_file() and _carried(out)["TELEGRAM_BOT_TOKEN"] == BOT_TOKEN
    notices = [line for line in printed.out.splitlines() if "NATS" in line]
    assert notices == [
        "NATS_USER and NATS_PASSWORD were left out: this instance's own settings (instance_config.yaml and "
        "team.env) say it uses a local NATS server, and the new instance makes its own login for that. "
        "This command's own NATS_URL could not be read; it was not used."
    ]
    assert "event=team_transfer_nats_environment_unreadable" in caplog.text
    assert "command_names=NATS_URL instance_nats=local" in caplog.text
    _assert_no_values(printed.out + printed.err + caplog.text, "", {"NATS_URL": MALFORMED_NATS})


def _assert_no_values(text: str, team_env: str, shell: dict[str, str]) -> None:
    """No address, user, password or carried value in what the operator sees or the logs keep."""

    values = [
        BOT_TOKEN, "nats-user-value", "nats-password-value", SHELL_NATS_USER, SHELL_NATS_PASSWORD,
        "shell-nats.example.invalid", "external.example", *shell.values(),
    ]
    values += [line.split("=", 1)[1] for line in team_env.splitlines() if "=" in line]
    for value in values:
        assert value not in text, value
