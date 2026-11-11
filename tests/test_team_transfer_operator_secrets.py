"""The ``operator_secrets`` transfer section carries operator-supplied secrets, encrypted."""

from __future__ import annotations

import logging
import re
import stat
from pathlib import Path
from typing import NamedTuple

import pytest

from assistant_gateway.instance_secrets import NATS_CREDENTIAL_ENV_NAMES, NATS_DECISION_ENV_NAMES
from internal_credentials import parse_env_file
from team_transfer.container import TransferReader
from team_transfer.crypto import new_key_derivation
from team_transfer.operator_secrets import EXTERNAL_NATS_DOCS, OperatorSecretsSection
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
    for name in (*NATS_DECISION_ENV_NAMES, *NATS_CREDENTIAL_ENV_NAMES, "TEAM_LOCAL_NATS_ENABLED"):
        for form in (name, f"{name}_FILE"):
            monkeypatch.delenv(form, raising=False)


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


# -- what the export tells the operator about NATS ---------------------------------

SHELL_NATS_USER = "shell-nats-user"
SHELL_NATS_PASSWORD = "shell-nats-password"
# A URL may hold a user and password, so no line or log may repeat one.
SHELL_EXTERNAL_NATS = f"nats://{SHELL_NATS_USER}:{SHELL_NATS_PASSWORD}@shell-nats.example.invalid:4222"
MALFORMED_NATS = "nats://[::1"
INSTANCE_NATS_LOGIN = "NATS_USER=nats-user-value\nNATS_PASSWORD=nats-password-value\n"
CREDENTIALS_FILE_ENV = "NATS_CREDENTIALS_FILE=/run/secrets/nats.creds\n"
# A row's "{password_file}" is replaced by a file holding FILE_PASSWORD.
PASSWORD_FILE, FILE_PASSWORD = "{password_file}", "file-nats-password"
DIFFERS, UNREADABLE = "team_transfer_nats_environment_differs", "team_transfer_nats_environment_unreadable"
SWITCH, LOGIN = "TEAM_LOCAL_NATS_ENABLED=false", "NATS_USER, NATS_PASSWORD"
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



# The names and decision each fixed line gives; the words around them may change.
NATS_LINES = (
    re.compile(r"NATS: .* would use (?P<unused>.+), which this export did not use\. .* is (?P<decided>local|external)\. "),
    re.compile(r"NATS: .* Login carried: (?P<carried>.+?)\. .* before importing: (?P<needed>.+?)\. "),
)


def _nats_said(out: str) -> dict[str, str]:
    """The fields the export's NATS lines give; each line has one fixed form and ends with the guide link."""

    said: dict[str, str] = {}
    for line in [line for line in out.splitlines() if "NATS" in line]:
        match = next(filter(None, (form.match(line) for form in NATS_LINES)), None)
        assert match and line.endswith(f"({EXTERNAL_NATS_DOCS}).") and not said.keys() & match.groupdict().keys(), line
        said.update(match.groupdict())
    return said


class Says(NamedTuple):
    """What the export's NATS lines name; a field left None belongs to a line that is not printed."""

    unused: str | None = None  # line 1: what the instance would use and the export did not
    decided: str | None = None  # line 1: what the instance's own settings decided
    carried: str | None = None  # line 2: the login carried, or "none"
    needed: str | None = None  # line 2: what the new instance needs
    event: str | None = None  # the warning logged with line 1


EXTERNAL_ENV = f"NATS_URL={EXTERNAL_NATS}\n"
WITH_PASSWORD_FILE = {"team_env": f"{EXTERNAL_ENV}NATS_PASSWORD_FILE={PASSWORD_FILE}\n"}
NEEDS_URL = f"{SWITCH}, NATS_URL"


@pytest.mark.parametrize(
    ("instance", "shell", "says"),
    [
        # (a) The command's address would decide otherwise, or cannot be read.
        pytest.param({}, {"NATS_URL": SHELL_EXTERNAL_NATS}, Says("NATS_URL", "local", event=DIFFERS), id="local-external-shell-url"),
        pytest.param(
            {}, {"NATS_URL": SHELL_EXTERNAL_NATS, "NATS_SERVERS": SHELL_EXTERNAL_NATS},
            Says("NATS_SERVERS, NATS_URL", "local", event=DIFFERS), id="local-external-shell-servers-and-url",
        ),
        pytest.param(
            {}, {"NATS_CREDENTIALS_FILE": "/run/secrets/shell-nats.creds"},
            Says("NATS_CREDENTIALS_FILE", "local", event=DIFFERS), id="local-shell-credentials-file",
        ),
        pytest.param({}, {"NATS_URL": MALFORMED_NATS}, Says("NATS_URL", "local", event=UNREADABLE), id="unreadable-shell-url"),
        pytest.param(
            {"team_env": EXTERNAL_ENV}, {"NATS_URL": LOCAL_NATS}, Says("NATS_URL", "external", LOGIN, NEEDS_URL, DIFFERS),
            id="external-team-env-local-shell",
        ),
        pytest.param(
            {"saved_nats_url": EXTERNAL_NATS, "nats_login": ""}, {"NATS_URL": LOCAL_NATS},
            Says("NATS_URL", "external", "none", SWITCH, DIFFERS), id="external-without-a-login-local-shell",
        ),
        # (b) The server is external and an address or login the instance would use differs (C03, C02).
        pytest.param(
            {"saved_nats_url": EXTERNAL_NATS}, {"NATS_URL": SHELL_EXTERNAL_NATS},
            Says("NATS_URL", "external", LOGIN, SWITCH, DIFFERS), id="both-external-address-only-in-the-shell",
        ),
        pytest.param(
            {"team_env": EXTERNAL_ENV}, {"NATS_URL": SHELL_EXTERNAL_NATS},
            Says("NATS_URL", "external", LOGIN, NEEDS_URL, DIFFERS), id="both-external-different-addresses",
        ),
        pytest.param(
            {"team_env": f"NATS_SERVERS={EXTERNAL_NATS}\n"}, {"NATS_URL": LOCAL_NATS},
            Says("NATS_URL", "external", LOGIN, f"{SWITCH}, NATS_SERVERS", DIFFERS), id="team-env-servers-outrank-the-shells-url",
        ),
        pytest.param(
            {"team_env": CREDENTIALS_FILE_ENV}, {"NATS_URL": LOCAL_NATS},
            Says("NATS_URL", "external", LOGIN, f"{SWITCH}, NATS_CREDENTIALS_FILE", DIFFERS),
            id="team-env-credentials-file-outranks-the-shells-url",
        ),
        pytest.param(
            {"team_env": EXTERNAL_ENV}, {"NATS_USER": SHELL_NATS_USER, "NATS_PASSWORD": SHELL_NATS_PASSWORD},
            Says(LOGIN, "external", LOGIN, NEEDS_URL, DIFFERS), id="external-shell-login",
        ),
        pytest.param(
            WITH_PASSWORD_FILE, {"NATS_PASSWORD_FILE": PASSWORD_FILE},
            Says("NATS_PASSWORD_FILE", "external", LOGIN, NEEDS_URL, DIFFERS), id="shell-password-file-team-env-both-forms",
        ),
        pytest.param(
            WITH_PASSWORD_FILE, {}, Says("NATS_PASSWORD_FILE", "external", LOGIN, NEEDS_URL, DIFFERS),
            id="team-env-both-password-forms",
        ),
        # No line 1: both sides use the same values, or the instance is local either way.
        pytest.param({"team_env": EXTERNAL_ENV}, {}, Says(carried=LOGIN, needed=NEEDS_URL), id="clean-shell"),
        pytest.param(
            {"team_env": EXTERNAL_ENV}, {"NATS_USER": "nats-user-value", "NATS_PASSWORD": "nats-password-value"},
            Says(carried=LOGIN, needed=NEEDS_URL), id="shell-login-equals-team-env",
        ),
        pytest.param(
            {**WITH_PASSWORD_FILE, "nats_login": "NATS_USER=nats-user-value\n"}, {"NATS_PASSWORD": SHELL_NATS_PASSWORD},
            Says(carried=LOGIN, needed=NEEDS_URL), id="shell-plain-password-team-env-password-file",
        ),
        pytest.param(
            {**WITH_PASSWORD_FILE, "nats_login": "NATS_USER=nats-user-value\n"}, {},
            Says(carried=LOGIN, needed=NEEDS_URL), id="team-env-password-file",
        ),
        pytest.param({}, {"NATS_URL": "nats://localhost:4222"}, Says(), id="both-local"),
        pytest.param({}, {"NATS_USER": SHELL_NATS_USER, "NATS_PASSWORD": SHELL_NATS_PASSWORD}, Says(), id="local-shell-login"),
        # Line 2 without a login to carry: the switch still does not travel.
        pytest.param(
            {"team_env": f"{CREDENTIALS_FILE_ENV}{SWITCH}\n", "nats_login": ""}, {},
            Says(carried="none", needed=f"{SWITCH}, NATS_CREDENTIALS_FILE"), id="credentials-file",
        ),
        pytest.param(
            {"team_env": CREDENTIALS_FILE_ENV, "nats_login": "", "saved_nats_url": EXTERNAL_NATS}, {},
            Says(carried="none", needed=f"{SWITCH}, NATS_CREDENTIALS_FILE"), id="credentials-file-server-in-the-settings",
        ),
        pytest.param(
            {"saved_nats_url": EXTERNAL_NATS, "nats_login": ""}, {}, Says(carried="none", needed=SWITCH),
            id="server-in-the-settings-without-a-login",
        ),
        # Secrets from the environment: the command's environment is the instance's; team.env is not read.
        pytest.param(
            {"secrets_source": "environment"}, {"NATS_URL": SHELL_EXTERNAL_NATS},
            Says(carried="none", needed=NEEDS_URL), id="secrets-from-the-environment",
        ),
        pytest.param(
            {"secrets_source": "environment"},
            {"NATS_SERVERS": SHELL_EXTERNAL_NATS, "NATS_URL": SHELL_EXTERNAL_NATS, "NATS_USER": "env-nats-user"},
            Says(carried="NATS_USER", needed=f"{SWITCH}, NATS_SERVERS, NATS_URL"), id="secrets-from-the-environment-with-a-login",
        ),
    ],
)
def test_the_exports_nats_lines_name_the_variables_and_the_decision(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    caplog: pytest.LogCaptureFixture,
    instance: dict[str, str],
    shell: dict[str, str],
    says: Says,
) -> None:
    # Line 1 compares the command's NATS variables, laid over team.env as the
    # launcher does, with what the export used; line 2 says what an external
    # server needs. Only names and the decision are checked, never wording.
    caplog.set_level(logging.INFO)
    (tmp_path / "nats-password").write_text(FILE_PASSWORD + "\n")
    instance, shell = (
        {name: value.replace(PASSWORD_FILE, str(tmp_path / "nats-password")) for name, value in values.items()}
        for values in (instance, shell)
    )
    out = _cli_export(tmp_path, monkeypatch, shell=shell, **{"team_env": "", **instance})

    printed = capsys.readouterr()
    assert _nats_said(printed.out) == {
        field: value for field, value in says._asdict().items() if value is not None and field != "event"
    }
    lines = printed.out.splitlines()
    stop = next(index for index, line in enumerate(lines) if line.startswith("Stop this instance now"))
    assert all(index < stop for index, line in enumerate(lines) if "NATS" in line)
    warnings = [record.getMessage() for record in caplog.records if "event=team_transfer_nats_environment" in record.getMessage()]
    if says.event:
        assert len(warnings) == 1 and f"event={says.event} " in warnings[0], warnings
        assert "decided_by=instance_settings" in warnings[0]
        carried_names = (says.carried or "none").replace(", ", ",")
        assert (
            f"command_names={str(says.unused).replace(', ', ',')} instance_nats={says.decided} carried_names={carried_names}"
            in warnings[0]
        )
    else:
        assert not warnings
    carried = _carried(out)
    login = [name for name in NATS_CREDENTIAL_ENV_NAMES if name in carried]
    assert (", ".join(login) or "none") == (says.carried or "none")
    assert not {*NATS_DECISION_ENV_NAMES, "TEAM_LOCAL_NATS_ENABLED"} & set(carried), "the address and switch stay"
    _assert_no_values(printed.out + printed.err + caplog.text, instance.get("team_env", ""), shell)


GENERATED_LOGIN = "NATS_USER=ua-generated-user\nNATS_PASSWORD=ua-generated-password\n"
CONTAINER_LOGIN = {"NATS_USER": "container-nats-user", "NATS_PASSWORD": "container-nats-password"}


def test_following_the_exports_nats_advice_carries_the_login_the_instance_runs_with(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    # The review's scenario (FIN-1): an env_file instance in Docker whose
    # compose environment holds the external server and its login. Onboarding
    # decided from team.env, which named no server, and wrote a generated login
    # there that the instance never uses. Each step follows the guide.
    secret = tmp_path / "container-nats-password"
    secret.write_text(CONTAINER_LOGIN["NATS_PASSWORD"] + "\n")
    container = {"NATS_URL": EXTERNAL_NATS, **CONTAINER_LOGIN, "TEAM_LOCAL_NATS_ENABLED": "false"}
    container_user = f"NATS_USER={CONTAINER_LOGIN['NATS_USER']}\n"

    def export(step: str, team_env: str, nats_login: str) -> tuple[str | None, dict[str, str]]:
        (tmp_path / step).mkdir()
        out = _cli_export(tmp_path / step, monkeypatch, team_env=team_env, shell=container, nats_login=nats_login)
        carried = _carried(out)
        return _nats_said(capsys.readouterr().out).get("unused"), {
            name: carried[name] for name in NATS_CREDENTIAL_ENV_NAMES if name in carried
        }

    # Decided local from team.env: the line names the address and the login together.
    assert export("first", "", GENERATED_LOGIN) == ("NATS_URL, NATS_USER, NATS_PASSWORD", {})
    # Moving only the address carries onboarding's login, and the line still names the container's.
    assert export("address-only", EXTERNAL_ENV, GENERATED_LOGIN) == (
        LOGIN, {"NATS_USER": "ua-generated-user", "NATS_PASSWORD": "ua-generated-password"}
    )
    # Replacing onboarding's lines with the container's login, as the guide says, carries it, with no line 1.
    container_login = "".join(f"{name}={value}\n" for name, value in CONTAINER_LOGIN.items())
    assert export("advice-followed", EXTERNAL_ENV, container_login) == (None, CONTAINER_LOGIN)

    # The password as a Docker secret (C02): the instance reads the file before
    # a plain value, the export the other way round.
    monkeypatch.delenv("NATS_PASSWORD")
    container.pop("NATS_PASSWORD")
    container["NATS_PASSWORD_FILE"] = str(secret)
    with_file = f"{EXTERNAL_ENV}NATS_PASSWORD_FILE={secret}\n"
    # Onboarding's plain NATS_PASSWORD left beside the file form is named.
    assert export("file-beside-plain", with_file, container_user + "NATS_PASSWORD=ua-generated-password\n") == (
        "NATS_PASSWORD_FILE", {"NATS_USER": "container-nats-user", "NATS_PASSWORD": "ua-generated-password"}
    )
    # Deleting it, as the guide says, carries the file's password.
    assert export("file-only", with_file, container_user) == (None, CONTAINER_LOGIN)


def test_the_nats_lines_point_to_a_section_of_the_team_transfer_guide() -> None:
    # Every NATS line and reason ends with this link and names the section;
    # renaming the section must update both.
    from markdown.extensions.toc import slugify

    from team_transfer.operator_secrets import _SEE_THE_GUIDE

    page, anchor = EXTERNAL_NATS_DOCS.removeprefix("/docs/").split("/#")
    guide = (Path(__file__).resolve().parents[1] / "user_docs_source" / f"{page}.md").read_text(encoding="utf-8")
    headings = [line.lstrip("#").strip() for line in guide.splitlines() if line.startswith("## ")]
    title = re.search(r'See "(.+?)"', _SEE_THE_GUIDE)
    assert title and title.group(1) in headings and slugify(title.group(1), "-") == anchor


def _assert_no_values(text: str, team_env: str, shell: dict[str, str]) -> None:
    """No address, user, password or carried value in what the operator sees or the logs keep."""

    values = [
        BOT_TOKEN, "nats-user-value", "nats-password-value", SHELL_NATS_USER, SHELL_NATS_PASSWORD, FILE_PASSWORD,
        "shell-nats.example.invalid", "external.example", *CONTAINER_LOGIN.values(), *shell.values(),
    ]
    # The local-server switch is advice, not a secret: its value appears in the lines.
    values += [line.split("=", 1)[1] for line in team_env.splitlines() if "=" in line and not line.startswith("TEAM_LOCAL")]
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
    ("old_team_env", "shell", "saved_nats_url", "new_team_env", "kept", "needed"),
    [
        pytest.param(
            "", {"NATS_URL": LOCAL_NATS}, EXTERNAL_NATS, "", True, SWITCH,
            id="server-in-the-saved-settings-local-shell",
        ),
        pytest.param(
            "", {"NATS_URL": SHELL_EXTERNAL_NATS, "NATS_SERVERS": SHELL_EXTERNAL_NATS}, None, "", False, None,
            id="local-instance-external-shell",
        ),
        pytest.param(
            EXTERNAL_ENV, {"NATS_URL": LOCAL_NATS}, None, f"{EXTERNAL_ENV}{SWITCH}\n", True, NEEDS_URL,
            id="server-in-the-old-team-env-named-again-by-url",
        ),
        pytest.param(
            f"NATS_SERVERS={EXTERNAL_NATS}\n", {}, None,
            f"NATS_SERVERS={EXTERNAL_NATS}\n{SWITCH}\n", True, f"{SWITCH}, NATS_SERVERS",
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
    needed: str | None,
) -> None:
    # The shell stays set during the import too: with env_file secrets the
    # import decides from the new team.env, not from the command's environment.
    caplog.set_level(logging.INFO)
    out = _cli_export(tmp_path, monkeypatch, team_env=old_team_env, shell=shell, saved_nats_url=saved_nats_url)
    exported = capsys.readouterr().out
    # The import accepts a carried login without a word about the switch (F8), so the export says it.
    assert _nats_said(exported).get("needed") == needed, exported
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
    assert _nats_said(exported) == {"carried": LOGIN, "needed": NEEDS_URL}
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

