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
    nats_login: str = INSTANCE_NATS_LOGIN,
) -> Path:
    """Runs ``assistant_cli team export`` for the settings and operator secrets sections.

    The passphrase file it writes, ``tmp_path / "passphrase.txt"``, serves the import too.
    """

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
    config.layout.team_env_path.write_text(f"TELEGRAM_BOT_TOKEN={BOT_TOKEN}\n{nats_login}{team_env}")
    for name, value in shell.items():
        monkeypatch.setenv(name, value)
    secret = tmp_path / "passphrase.txt"
    secret.write_text(PASSPHRASE + "\n")
    out = tmp_path / "team.uatransfer"
    _run_cli("export", "--config", str(path), "--owner", "usr_owner", "--out", str(out), "--passphrase-file", str(secret))
    return out


# What the export did with the NATS login, in the notice test below.
LEFT_OUT, CARRIED, NONE_CARRIED = "left out", "carried", "none carried"
# How the export's NATS line ends whenever it carries a login: the switch does not travel.
KEEP_THE_SERVER = (
    "For the new instance to use that server, set {names}TEAM_LOCAL_NATS_ENABLED=false in its team.env "
    "(or its environment, if it takes secrets from there) before importing; otherwise the image starts its "
    "own NATS server."
)
ADDRESS_IN_THE_SETTINGS = (
    "The server's address travels in instance_config.yaml, but TEAM_LOCAL_NATS_ENABLED does not."
)


def _neither(names: str, where: str = "team.env") -> str:
    """The middle of the line when the instance's environment names the server, with its tail."""

    verb = "does" if " and " not in names else "do"
    value = "value" if " and " not in names else "values"
    return (
        f"TEAM_LOCAL_NATS_ENABLED does not travel, and neither {verb} the {names} in this instance's {where}. "
        + KEEP_THE_SERVER.format(names=f"{names} to the same {value} and ")
        + (CREDENTIALS_FILE_STAYS if "NATS_CREDENTIALS_FILE" in names else "")
    )


# The variable alone is not enough: the file it names stays on the old host too.
CREDENTIALS_FILE_STAYS = (
    " The file NATS_CREDENTIALS_FILE names does not travel either: put it at that path on the new instance "
    "(for Docker, the same secret mount)."
)


@pytest.mark.parametrize(
    ("team_env", "shell", "outcome", "options"),
    [
        pytest.param("", {"NATS_URL": SHELL_EXTERNAL_NATS}, LEFT_OUT, {}, id="local-instance-external-shell-url"),
        pytest.param(
            "", {"NATS_URL": SHELL_EXTERNAL_NATS, "NATS_SERVERS": SHELL_EXTERNAL_NATS}, LEFT_OUT, {},
            id="local-instance-external-shell-servers-and-url",
        ),
        pytest.param(
            "", {"NATS_CREDENTIALS_FILE": "/run/secrets/shell-nats.creds"}, LEFT_OUT, {},
            id="local-instance-shell-credentials-file",
        ),
        pytest.param(f"NATS_URL={EXTERNAL_NATS}\n", {"NATS_URL": LOCAL_NATS}, CARRIED, {}, id="external-in-team-env-local-shell"),
        pytest.param(
            "", {"NATS_URL": LOCAL_NATS}, NONE_CARRIED, {"saved_nats_url": EXTERNAL_NATS, "nats_login": ""},
            id="external-instance-without-a-login-local-shell",
        ),
    ],
)
def test_the_export_says_when_the_commands_nats_variables_would_have_decided_otherwise(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    caplog: pytest.LogCaptureFixture,
    team_env: str,
    shell: dict[str, str],
    outcome: str,
    options: dict[str, str],
) -> None:
    caplog.set_level(logging.INFO)
    out = _cli_export(tmp_path, monkeypatch, team_env=team_env, shell=shell, **options)

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
    if outcome == CARRIED:
        assert notice.startswith("NATS_USER and NATS_PASSWORD were carried: ")
        # One line about one login: the command's variable, then what the new instance needs.
        assert notice.endswith("would mean a local server; it was not used. " + _neither("NATS_URL"))
        assert "instance_nats=external carried_names=NATS_USER,NATS_PASSWORD" in warnings[0]
        assert set(NATS_CREDENTIAL_ENV_NAMES) <= set(_carried(out))
    elif outcome == NONE_CARRIED:
        # No login travels, so the import has nothing to refuse, but the switch
        # still does not travel; the line promises no login (C04).
        assert notice.startswith("No NATS_USER or NATS_PASSWORD was carried: ")
        assert notice.endswith(
            "This command's own NATS_URL would mean a local server; it was not used. "
            + ADDRESS_IN_THE_SETTINGS + " " + KEEP_THE_SERVER.format(names="")
        )
        assert "keeps the login" not in notice
        assert "instance_nats=external carried_names=none" in warnings[0]
        assert not set(NATS_CREDENTIAL_ENV_NAMES) & set(_carried(out))
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


CARRIED_FROM_TEAM_ENV = (
    "NATS_USER and NATS_PASSWORD were carried: this instance's own settings (instance_config.yaml and team.env) "
    "say it uses an external NATS server. "
)


@pytest.mark.parametrize(
    ("team_env", "shell", "saved_nats_url", "secrets_source", "line"),
    [
        pytest.param(
            f"NATS_URL={EXTERNAL_NATS}\n", {}, None, None, CARRIED_FROM_TEAM_ENV + _neither("NATS_URL"),
            id="clean-shell",
        ),
        pytest.param("", {"NATS_URL": "nats://localhost:4222"}, None, None, None, id="both-local"),
        pytest.param(
            "", {"NATS_URL": SHELL_EXTERNAL_NATS}, EXTERNAL_NATS, None,
            CARRIED_FROM_TEAM_ENV + ADDRESS_IN_THE_SETTINGS + " " + KEEP_THE_SERVER.format(names=""),
            id="both-external",
        ),
        pytest.param(
            f"NATS_SERVERS={EXTERNAL_NATS}\n", {"NATS_URL": LOCAL_NATS}, None, None,
            CARRIED_FROM_TEAM_ENV + _neither("NATS_SERVERS"),
            id="team-env-servers-outrank-the-shells-url",
        ),
        pytest.param(
            "NATS_CREDENTIALS_FILE=/run/secrets/nats.creds\n", {"NATS_URL": LOCAL_NATS}, None, None,
            CARRIED_FROM_TEAM_ENV + _neither("NATS_CREDENTIALS_FILE"),
            id="team-env-credentials-file-outranks-the-shells-url",
        ),
        pytest.param(
            "", {"NATS_URL": SHELL_EXTERNAL_NATS}, None, "environment",
            # The login is only in team.env, which this instance never read.
            "No NATS_USER or NATS_PASSWORD was carried: this instance's own settings (instance_config.yaml and its "
            "environment) say it uses an external NATS server, but they hold no such login. "
            + _neither("NATS_URL", "environment"),
            id="secrets-from-the-environment",
        ),
        pytest.param(
            "",
            {"NATS_SERVERS": SHELL_EXTERNAL_NATS, "NATS_URL": SHELL_EXTERNAL_NATS, "NATS_USER": "env-nats-user"},
            None,
            "environment",
            "NATS_USER was carried: this instance's own settings (instance_config.yaml and its environment) say it "
            "uses an external NATS server. " + _neither("NATS_SERVERS and NATS_URL", "environment"),
            id="secrets-from-the-environment-with-a-login",
        ),
    ],
)
def test_the_export_says_nothing_about_the_commands_nats_variables_when_they_would_decide_the_same(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    caplog: pytest.LogCaptureFixture,
    team_env: str,
    shell: dict[str, str],
    saved_nats_url: str | None,
    secrets_source: str | None,
    line: str | None,
) -> None:
    # The launcher lays the container's environment over team.env name by name,
    # and the rule reads NATS_CREDENTIALS_FILE, then NATS_SERVERS, then
    # NATS_URL; a command value that would not change the decision is not news.
    # With secrets from the environment, the command's environment is the instance's.
    # An external instance still gets its line: the local-server switch does not travel (F8).
    caplog.set_level(logging.INFO)
    out = _cli_export(
        tmp_path, monkeypatch, team_env=team_env, shell=shell, saved_nats_url=saved_nats_url, secrets_source=secrets_source
    )

    printed = capsys.readouterr()
    notices = [text for text in printed.out.splitlines() if "NATS" in text]
    assert notices == ([line] if line else [])
    carried = bool(set(NATS_CREDENTIAL_ENV_NAMES) & set(_carried(out)))
    assert carried == bool(line and line.startswith("NATS_USER")), "the line says what was carried"
    assert not any(event in caplog.text for event in NOTICE_EVENTS)
    _assert_no_values(printed.out + printed.err + caplog.text, team_env, shell)


CREDENTIALS_FILE_ENV = "NATS_CREDENTIALS_FILE=/run/secrets/nats.creds\n"
NONE_CARRIED_FROM_TEAM_ENV = (
    "No NATS_USER or NATS_PASSWORD was carried: this instance's own settings (instance_config.yaml and team.env) "
    "say it uses an external NATS server, "
)


@pytest.mark.parametrize(
    ("saved_nats_url", "line"),
    [
        pytest.param(
            None,
            NONE_CARRIED_FROM_TEAM_ENV + "and it signs in with the file NATS_CREDENTIALS_FILE names. "
            + _neither("NATS_CREDENTIALS_FILE"),
            id="credentials-file-in-team-env",
        ),
        pytest.param(
            EXTERNAL_NATS,
            NONE_CARRIED_FROM_TEAM_ENV + "and it signs in with the file NATS_CREDENTIALS_FILE names. "
            + _neither("NATS_CREDENTIALS_FILE"),
            id="credentials-file-in-team-env-server-in-the-settings",
        ),
        pytest.param(
            EXTERNAL_NATS,
            NONE_CARRIED_FROM_TEAM_ENV + "but they hold no such login. "
            + ADDRESS_IN_THE_SETTINGS + " " + KEEP_THE_SERVER.format(names=""),
            id="server-in-the-settings-without-a-login",
        ),
    ],
)
def test_an_external_instance_without_a_nats_login_to_carry_still_says_what_the_new_instance_needs(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    caplog: pytest.LogCaptureFixture,
    saved_nats_url: str | None,
    line: str,
) -> None:
    # Nothing NATS travels, so the import has nothing to refuse and says
    # nothing; without the switch the new instance would start its own server.
    caplog.set_level(logging.INFO)
    credentials_file = CREDENTIALS_FILE_ENV if "NATS_CREDENTIALS_FILE" in line else ""
    out = _cli_export(
        tmp_path, monkeypatch, team_env=credentials_file + "TEAM_LOCAL_NATS_ENABLED=false\n", shell={},
        saved_nats_url=saved_nats_url, nats_login="",
    )

    printed = capsys.readouterr()
    assert [text for text in printed.out.splitlines() if "NATS" in text] == [line]
    assert not {"NATS_CREDENTIALS_FILE", "TEAM_LOCAL_NATS_ENABLED", *NATS_CREDENTIAL_ENV_NAMES} & set(_carried(out))
    assert not any(event in caplog.text for event in NOTICE_EVENTS)
    # The switch's value appears in the line as advice, so only the file's path is checked.
    _assert_no_values(printed.out + printed.err + caplog.text, credentials_file, {})


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


# -- round trips: what the new instance receives -------------------------------------

NATS_KEYS = {f"environment/{name}" for name in NATS_CREDENTIAL_ENV_NAMES}
IN_THE_ENVIRONMENT = "this instance takes secrets from the environment: set it there or as a Docker secret"


def _new_root(tmp_path: Path, team_env: str) -> Path:
    """A fresh root; ``team_env`` is what the operator wrote into its team.env before importing."""

    target = tmp_path / "new"
    target.mkdir()
    if team_env:
        (target / "team.env").write_text(team_env)
    return target


def _prepare_review(tmp_path: Path, out: Path, target: Path) -> Path:
    from team_transfer.review import prepare_review

    with TransferReader.open(out, PASSPHRASE) as reader:
        prepare_review(reader, target, tmp_path / "review")
    return tmp_path / "review"


def _cli_import(tmp_path: Path, out: Path, target: Path, review: Path, *extra: str) -> None:
    """``assistant_cli team import --review``, the host command's reviewed import."""

    from tests.test_team_transfer import _run_cli

    _run_cli(
        "import", "--in", str(out), "--instance-root", str(target), "--review", str(review),
        "--passphrase-file", str(tmp_path / "passphrase.txt"), *extra,
    )


def _unselect(out: Path, review: Path, keys: set[str]) -> None:
    from team_transfer.review import ReviewFiles

    with TransferReader.open(out, PASSPHRASE) as reader:
        files = ReviewFiles.open(reader, review)
        records = [
            record.model_copy(update={"selected": False}) if record.key in keys else record
            for record in files.review.records
        ]
        files.save(files.review.model_copy(update={"records": records}))


def _new_team_env(target: Path) -> dict[str, str]:
    path = target / "team.env"
    return parse_env_file(path) if path.is_file() else {}


@pytest.mark.parametrize(
    ("old_team_env", "shell", "saved_nats_url", "new_team_env", "kept", "export_says"),
    [
        pytest.param(
            "", {"NATS_URL": LOCAL_NATS}, EXTERNAL_NATS, "", True,
            ADDRESS_IN_THE_SETTINGS + " " + KEEP_THE_SERVER.format(names=""),
            id="server-in-the-saved-settings-local-shell",
        ),
        pytest.param(
            "", {"NATS_URL": SHELL_EXTERNAL_NATS, "NATS_SERVERS": SHELL_EXTERNAL_NATS}, None, "", False,
            "NATS_USER and NATS_PASSWORD were left out: ",
            id="local-instance-external-shell",
        ),
        pytest.param(
            f"NATS_URL={EXTERNAL_NATS}\n", {"NATS_URL": LOCAL_NATS}, None,
            f"NATS_URL={EXTERNAL_NATS}\nTEAM_LOCAL_NATS_ENABLED=false\n", True, _neither("NATS_URL"),
            id="server-in-the-old-team-env-named-again-by-url",
        ),
        pytest.param(
            f"NATS_SERVERS={EXTERNAL_NATS}\n", {}, None,
            f"NATS_SERVERS={EXTERNAL_NATS}\nTEAM_LOCAL_NATS_ENABLED=false\n", True, _neither("NATS_SERVERS"),
            id="server-in-the-old-team-env-named-again-by-servers",
        ),
    ],
)
def test_an_external_servers_login_reaches_the_new_team_env_when_the_new_instance_names_the_server(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    caplog: pytest.LogCaptureFixture,
    old_team_env: str,
    shell: dict[str, str],
    saved_nats_url: str | None,
    new_team_env: str,
    kept: bool,
    export_says: str,
) -> None:
    # The shell stays set during the import too: with env_file secrets the
    # import decides from the new team.env, not from the command's environment.
    caplog.set_level(logging.INFO)
    out = _cli_export(tmp_path, monkeypatch, team_env=old_team_env, shell=shell, saved_nats_url=saved_nats_url)
    exported = capsys.readouterr().out
    # The import accepts a carried login without a word about the switch (F8), so the export says it.
    assert len([line for line in exported.splitlines() if export_says in line]) == 1, exported
    target = _new_root(tmp_path, new_team_env)
    review = _prepare_review(tmp_path, out, target)
    capsys.readouterr()

    _cli_import(tmp_path, out, target, review)

    printed = capsys.readouterr()
    assert "refused" in printed.out and "; 0 refused." in printed.out
    new = _new_team_env(target)
    assert new["TELEGRAM_BOT_TOKEN"] == BOT_TOKEN
    if kept:
        assert (new["NATS_USER"], new["NATS_PASSWORD"]) == ("nats-user-value", "nats-password-value")
    else:
        assert not set(NATS_CREDENTIAL_ENV_NAMES) & set(_carried(out))
        assert new["NATS_PASSWORD"] and new["NATS_PASSWORD"] != "nats-password-value", "a fresh login for the local server"
    for line in new_team_env.splitlines():
        name, value = line.split("=", 1)
        assert new[name] == value, "what the operator wrote is kept"
    assert "TEAM_LOCAL_NATS_ENABLED" in new_team_env or "TEAM_LOCAL_NATS_ENABLED" not in new, "the switch does not travel"
    _assert_no_values(exported + printed.out + printed.err + caplog.text, old_team_env, shell)


def test_an_external_server_named_only_in_the_old_team_env_is_refused_with_the_steps_until_unselected(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    # The address does not travel, so the new instance's settings name the
    # local server and it makes its own login; the reason says how to keep the old one.
    out = _cli_export(tmp_path, monkeypatch, team_env=f"NATS_URL={EXTERNAL_NATS}\n", shell={})
    assert set(NATS_CREDENTIAL_ENV_NAMES) <= set(_carried(out))
    target = _new_root(tmp_path, "")
    review = _prepare_review(tmp_path, out, target)
    capsys.readouterr()

    with pytest.raises(SystemExit) as exited:
        _cli_import(tmp_path, out, target, review, "--dry-run")
    assert exited.value.code == 1
    reason = (
        "the new instance's settings name a local NATS server, and it makes its own login for that. "
        "If the old instance used an external NATS server, set NATS_URL or NATS_SERVERS to that server, "
        "and TEAM_LOCAL_NATS_ENABLED=false so the image does not start its own server, "
        f"in the new instance's env file, {target / 'team.env'}; then validate the selected records again. "
        "Otherwise unselect this record."
    )
    refused = [line for line in capsys.readouterr().out.splitlines() if line.startswith("- operator_secrets")]
    assert refused == [f"- operator_secrets environment/{name}: {reason}" for name in ("NATS_PASSWORD", "NATS_USER")]

    with pytest.raises(SystemExit, match="Selected records were refused"):
        _cli_import(tmp_path, out, target, review)
    assert not (target / "instance_config.yaml").exists(), "nothing is written"

    _unselect(out, review, NATS_KEYS)
    _cli_import(tmp_path, out, target, review)
    new = _new_team_env(target)
    assert new["TELEGRAM_BOT_TOKEN"] == BOT_TOKEN
    assert new["NATS_PASSWORD"] and new["NATS_PASSWORD"] != "nats-password-value"


@pytest.mark.parametrize(
    ("import_environment", "new_team_env", "kept"),
    [
        pytest.param({"NATS_URL": EXTERNAL_NATS}, "", True, id="the-environment-names-the-server"),
        pytest.param({}, "", False, id="the-environment-names-no-server"),
        pytest.param({}, f"NATS_URL={EXTERNAL_NATS}\n", False, id="only-a-team-env-names-the-server"),
    ],
)
def test_with_secrets_from_the_environment_the_import_decides_from_the_environment_it_runs_in(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    import_environment: dict[str, str],
    new_team_env: str,
    kept: bool,
) -> None:
    # Onboarding's Import of an env_file instance with "Secrets come from the
    # environment" chosen: it runs in the new container, whose environment is
    # the new instance's. A team.env is not where that instance reads secrets.
    import asyncio

    from team_transfer.review import prepare_review
    from team_transfer.review_session import ReviewApplyV1, ReviewSession
    from team_transfer.sections import DestinationChoices

    out = _cli_export(tmp_path, monkeypatch, team_env=f"NATS_URL={EXTERNAL_NATS}\n", shell={})
    for name, value in import_environment.items():
        monkeypatch.setenv(name, value)
    target = _new_root(tmp_path, new_team_env)
    with TransferReader.open(out, PASSPHRASE) as reader:
        session = ReviewSession(
            prepare_review(reader, target, tmp_path / "review"),
            destination=DestinationChoices(secrets_source="environment"),
        )
        result = asyncio.run(session.validate())
        nats = [record for record in result["records"] if record["key"] in NATS_KEYS]
        assert len(nats) == 2
        if not kept:
            assert not result["valid"]
            for record in nats:
                assert record["outcome"] == RecordOutcome.REFUSED.value
                assert "TEAM_LOCAL_NATS_ENABLED=false so the image does not start its own server, in the new instance's environment." in record["reason"]
                assert "This import reads NATS_URL and NATS_SERVERS from the environment it runs in" in record["reason"]
                assert "where a value already set wins over --env), so set them there too;" in record["reason"]
            return
        assert result["valid"]
        assert [(record["outcome"], record["reason"]) for record in nats] == [
            (RecordOutcome.ATTENTION.value, IN_THE_ENVIRONMENT)
        ] * 2
        asyncio.run(session.apply(ReviewApplyV1(digest=result["digest"])))
    assert (target / "instance_config.yaml").is_file()
    assert not set(NATS_CREDENTIAL_ENV_NAMES) & set(_new_team_env(target)), "nothing is written in this mode"


def test_an_environment_mode_instance_lists_its_external_servers_login_for_the_new_environment(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    # Both commands run where their instance runs, so each reads its own
    # instance's environment; the old team.env, which the instance never used, is ignored.
    environment = {"NATS_URL": EXTERNAL_NATS, "NATS_USER": "env-nats-user", "NATS_PASSWORD": "env-nats-password"}
    out = _cli_export(tmp_path, monkeypatch, team_env="", shell=environment, secrets_source="environment")
    carried = _carried(out)
    assert (carried["NATS_USER"], carried["NATS_PASSWORD"]) == ("env-nats-user", "env-nats-password")
    exported = capsys.readouterr().out
    assert _neither("NATS_URL", "environment") in exported
    assert "env-nats-user" not in exported and "env-nats-password" not in exported and "external.example" not in exported
    target = _new_root(tmp_path, "")
    review = _prepare_review(tmp_path, out, target)
    capsys.readouterr()

    _cli_import(tmp_path, out, target, review)

    printed = capsys.readouterr().out
    for name in NATS_CREDENTIAL_ENV_NAMES:
        assert f"- operator_secrets environment/{name}: {IN_THE_ENVIRONMENT}" in printed
    assert "env-nats-user" not in printed and "env-nats-password" not in printed
    assert not set(NATS_CREDENTIAL_ENV_NAMES) & set(_new_team_env(target))

