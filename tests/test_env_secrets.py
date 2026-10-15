from pathlib import Path

import pytest
import yaml

from env_secrets import (
    DEFAULT_SECRET_FILE_ENV_VARS,
    FORM_NONE,
    FORM_VALUE,
    PROCESS_NAMES,
    SECRET_ENV_LIST,
    ListedVariable,
    container_secret_file_problems,
    environment_for,
    get_env_with_file_fallback,
    is_withheld_from,
    load_listed_variables,
    main,
    materialize_secret_file_envs,
)
from scripts.export_secret_env_docs import DOC_PATH, REGENERATE_COMMAND, secret_env_doc_is_stale


def _processes_yaml() -> str:
    """A ``processes`` section naming every known process, as the loader requires."""

    return yaml.safe_dump({"processes": {name: f"The {name} process." for name in sorted(PROCESS_NAMES)}})


def _write_list(tmp_path: Path, groups_yaml: str, *, processes_yaml: str | None = None) -> Path:
    list_path = tmp_path / "secret_env_vars.yaml"
    list_path.write_text((processes_yaml if processes_yaml is not None else _processes_yaml()) + groups_yaml, encoding="utf-8")
    return list_path


def test_get_env_with_file_fallback_reads_secret_file(tmp_path: Path) -> None:
    secret_path = tmp_path / "openai.key"
    secret_path.write_text("sk-test-value\n", encoding="utf-8")

    value = get_env_with_file_fallback(
        "OPENAI_API_KEY",
        env={"OPENAI_API_KEY_FILE": str(secret_path)},
    )

    assert value == "sk-test-value"


def test_get_env_with_file_fallback_keeps_a_value_the_process_set(tmp_path: Path) -> None:
    """After startup the direct value wins: the broker stores a fresh Bitwarden
    session in the environment, and a stale mounted file must not replace it."""

    secret_path = tmp_path / "bw.session"
    secret_path.write_text("stale-from-file\n", encoding="utf-8")

    value = get_env_with_file_fallback(
        "BW_SESSION",
        env={"BW_SESSION": "fresh-from-sign-in", "BW_SESSION_FILE": str(secret_path)},
    )

    assert value == "fresh-from-sign-in"


def test_materialize_secret_file_envs_lets_a_secret_file_beat_a_direct_value(tmp_path: Path) -> None:
    user_secret_path = tmp_path / "nats.user"
    password_secret_path = tmp_path / "nats.password"
    web_chat_secret_path = tmp_path / "web_chat.token"
    user_secret_path.write_text("user-from-file\n", encoding="utf-8")
    password_secret_path.write_text("from-file\n", encoding="utf-8")
    web_chat_secret_path.write_text("web-chat-from-file\n", encoding="utf-8")
    env = {
        "NATS_USER": "user-from-env",
        "NATS_USER_FILE": str(user_secret_path),
        "NATS_PASSWORD": "from-env",
        "NATS_PASSWORD_FILE": str(password_secret_path),
        "WEB_CHAT_AUTH_TOKEN": "web-chat-from-team-env",
        "WEB_CHAT_AUTH_TOKEN_FILE": str(web_chat_secret_path),
    }

    resolved = materialize_secret_file_envs(env)

    assert resolved["NATS_USER"] == env["NATS_USER"] == "user-from-file"
    assert resolved["NATS_PASSWORD"] == env["NATS_PASSWORD"] == "from-file"
    assert resolved["WEB_CHAT_AUTH_TOKEN"] == env["WEB_CHAT_AUTH_TOKEN"] == "web-chat-from-file"


def test_materialize_secret_file_envs_keeps_direct_values_without_a_file() -> None:
    env = {"OPENAI_API_KEY": "sk-from-env"}

    resolved = materialize_secret_file_envs(env)

    assert resolved == {"OPENAI_API_KEY": "sk-from-env"}


def test_materialize_secret_file_envs_fails_on_a_missing_secret_file(tmp_path: Path) -> None:
    env = {"WEB_CHAT_AUTH_TOKEN_FILE": str(tmp_path / "missing")}

    with pytest.raises(FileNotFoundError, match="Secret file does not exist"):
        materialize_secret_file_envs(env)


def test_shared_list_covers_every_gateway_and_channel_secret() -> None:
    for name in (
        "WEB_CHAT_AUTH_TOKEN",
        "ADAPTER_HOST_AUTH_TOKEN",
        "DISCORD_BOT_TOKEN",
        "TWILIO_ACCOUNT_SID",
        "PHOTON_PROJECT_SECRET",
        "OPENSHELL_SSH_HANDSHAKE_SECRET",
        "OP_CONNECT_TOKEN",
    ):
        assert name in DEFAULT_SECRET_FILE_ENV_VARS


def test_secret_list_parser_reads_processes_and_forms(tmp_path: Path) -> None:
    list_path = _write_list(
        tmp_path,
        """
groups:
  - name: First
    variables:
      FIRST_TOKEN: {receivers: [gateway], about: First token.}
  - name: Second
    variables:
      LAST_KEY: {receivers: [manager, agent], file: value, about: Last key.}
      OP_SESSION_*: {receivers: [secure_broker], file: none, about: Sessions.}
""",
    )

    assert load_listed_variables(list_path) == (
        ListedVariable("FIRST_TOKEN", frozenset({"gateway"}), FORM_VALUE),
        ListedVariable("LAST_KEY", frozenset({"manager", "agent"}), FORM_VALUE),
        ListedVariable("OP_SESSION_*", frozenset({"secure_broker"}), FORM_NONE),
    )


@pytest.mark.parametrize(
    ("variables", "message"),
    [
        ("BAD_FILE: {receivers: [gateway], file: nofile, about: x}", "BAD_FILE > file"),
        # The path form (NAME_FILE's path becomes the value) was removed with its
        # only user, GOOGLE_APPLICATION_CREDENTIALS.
        ("PATH_FORM: {receivers: [secure_broker], file: path, about: x}", "PATH_FORM > file"),
        ("EXTRA_KEY: {receivers: [gateway], about: x, secret: true}", "EXTRA_KEY > secret"),
        ("NO_ABOUT: {receivers: [gateway]}", "NO_ABOUT > about"),
        ("NO_RECEIVERS: {receivers: [], about: x}", "NO_RECEIVERS > receivers"),
        ("UNKNOWN_PROCESS: {receivers: [web_server], about: x}", "UNKNOWN_PROCESS: unknown process web_server"),
        ("lower_case: {receivers: [gateway], about: x}", "lower_case: not an upper-case"),
        ("PREFIX_*: {receivers: [secure_broker], about: x}", "PREFIX_*: a name ending in '*' must use file: none"),
        ("SAME_TOKEN: {receivers: [gateway], about: x}\n      SAME_TOKEN: {receivers: [agent], about: y}", "SAME_TOKEN is listed more than once"),
    ],
)
def test_secret_list_parser_refuses_bad_entries_and_names_them(tmp_path: Path, variables: str, message: str) -> None:
    list_path = _write_list(tmp_path, f"groups:\n  - name: Group\n    variables:\n      {variables}\n")

    with pytest.raises(ValueError, match="Invalid secret variable list") as exc_info:
        load_listed_variables(list_path)
    assert message in str(exc_info.value)


def test_secret_list_parser_refuses_a_name_in_two_groups(tmp_path: Path) -> None:
    list_path = _write_list(
        tmp_path,
        """
groups:
  - name: One
    variables:
      SAME_TOKEN: {receivers: [gateway], about: x}
  - name: Two
    variables:
      SAME_TOKEN: {receivers: [agent], about: y}
""",
    )

    with pytest.raises(ValueError, match="SAME_TOKEN: listed more than once"):
        load_listed_variables(list_path)


def test_secret_list_parser_refuses_processes_that_differ_from_the_code(tmp_path: Path) -> None:
    list_path = _write_list(
        tmp_path,
        "groups:\n  - name: G\n    variables:\n      A_TOKEN: {receivers: [gateway], about: x}\n",
        processes_yaml="processes:\n  gateway: The gateway.\n  web_server: Not a process.\n",
    )

    with pytest.raises(ValueError, match="processes must name exactly") as exc_info:
        load_listed_variables(list_path)
    assert "web_server" in str(exc_info.value)
    assert "manager" in str(exc_info.value)


def test_shipped_list_describes_every_process_and_entry() -> None:
    assert set(SECRET_ENV_LIST.processes) == PROCESS_NAMES
    assert all(entry.about.strip() for _name, entry in SECRET_ENV_LIST.entries())


def test_docker_guide_tables_match_the_list() -> None:
    assert not secret_env_doc_is_stale(), f"{DOC_PATH.name} is stale; regenerate with: {REGENERATE_COMMAND}"


def test_container_check_refuses_a_secret_set_twice_or_a_missing_file(tmp_path: Path) -> None:
    secret = tmp_path / "web_chat_token"
    secret.write_text("from-file\n", encoding="utf-8")
    env = {
        "WEB_CHAT_AUTH_TOKEN": "direct-value",
        "WEB_CHAT_AUTH_TOKEN_FILE": str(secret),
        "DISCORD_BOT_TOKEN_FILE": str(tmp_path / "missing.json"),
        "OPENAI_API_KEY_FILE": str(secret),
        # Sealbox reads its own file; a "none" entry has no NAME_FILE form to check.
        "SEALBOX_TOKEN": "sealbox",
        "SEALBOX_TOKEN_FILE": str(tmp_path / "missing"),
    }

    problems = container_secret_file_problems(env)

    assert sorted(problems) == sorted([
        "Both WEB_CHAT_AUTH_TOKEN and WEB_CHAT_AUTH_TOKEN_FILE are set; use only one.",
        f"DISCORD_BOT_TOKEN_FILE points to a missing file: {tmp_path / 'missing.json'}",
    ])
    assert not any("direct-value" in problem for problem in problems)


def test_container_check_command_exits_with_2_and_names_the_problem(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    secret = tmp_path / "token"
    secret.write_text("x", encoding="utf-8")
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "direct")
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN_FILE", str(secret))

    assert main(["check-container-env"]) == 2
    assert "Both TELEGRAM_BOT_TOKEN and TELEGRAM_BOT_TOKEN_FILE are set" in caplog.text

    monkeypatch.delenv("TELEGRAM_BOT_TOKEN")
    assert main(["check-container-env"]) == 0


def test_environment_for_keeps_each_secret_and_its_file_form_with_its_processes() -> None:
    env = {
        "PATH": "/usr/bin",
        "WEB_CHAT_AUTH_TOKEN": "web",
        "WEB_CHAT_AUTH_TOKEN_FILE": "/run/secrets/web",
        "BW_SESSION_FILE": "/run/secrets/bw",
        "OP_SESSION_team": "session",
        "SEALBOX_TOKEN_FILE": "/broker/sealbox-token",
        "OPENAI_API_KEY": "sk",
    }

    gateway = environment_for("gateway", env)
    broker = environment_for("secure_broker", env)
    manager = environment_for("manager", env)

    assert gateway == {
        "PATH": "/usr/bin",
        "WEB_CHAT_AUTH_TOKEN": "web",
        "WEB_CHAT_AUTH_TOKEN_FILE": "/run/secrets/web",
    }
    assert set(broker) == {"PATH", "BW_SESSION_FILE", "OP_SESSION_team", "SEALBOX_TOKEN_FILE"}
    assert set(manager) == {"PATH", "OPENAI_API_KEY"}


def test_environment_for_routes_a_configured_name_like_the_name_it_replaces() -> None:
    env = {"TEAM_WEB_TOKEN": "web", "TEAM_WEB_TOKEN_FILE": "/run/secrets/web", "OTHER": "x"}
    renamed = {"TEAM_WEB_TOKEN": "WEB_CHAT_AUTH_TOKEN"}

    assert environment_for("gateway", env, renamed=renamed) == env
    assert environment_for("manager", env, renamed=renamed) == {"OTHER": "x"}
    with pytest.raises(ValueError, match="not listed"):
        environment_for("manager", env, renamed={"TEAM_WEB_TOKEN": "NOT_A_LISTED_NAME"})


def test_environment_for_refuses_an_unknown_process() -> None:
    with pytest.raises(ValueError, match="Unknown process"):
        environment_for("web_server", {"PATH": "/usr/bin"})


def test_is_withheld_from_names_secrets_the_broker_does_not_receive() -> None:
    assert is_withheld_from("secure_broker", "XAI_API_KEY")
    assert not is_withheld_from("secure_broker", "NATS_PASSWORD")
    assert not is_withheld_from("secure_broker", "UNLISTED_NAME")


def test_materialize_secret_file_envs_includes_xai_telegram_and_twilio_defaults(tmp_path: Path) -> None:
    xai_secret_path = tmp_path / "xai.key"
    xai_oauth_secret_path = tmp_path / "xai-oauth.token"
    telegram_secret_path = tmp_path / "telegram.key"
    twilio_secret_path = tmp_path / "twilio.key"
    xai_secret_path.write_text("xai-secret\n", encoding="utf-8")
    xai_oauth_secret_path.write_text("xai-oauth-secret\n", encoding="utf-8")
    telegram_secret_path.write_text("telegram-secret\n", encoding="utf-8")
    twilio_secret_path.write_text("twilio-secret\n", encoding="utf-8")
    env = {
        "XAI_API_KEY_FILE": str(xai_secret_path),
        "XAI_OAUTH_ACCESS_TOKEN_FILE": str(xai_oauth_secret_path),
        "TELEGRAM_BOT_TOKEN_FILE": str(telegram_secret_path),
        "TWILIO_AUTH_TOKEN_FILE": str(twilio_secret_path),
    }

    resolved = materialize_secret_file_envs(env)

    assert resolved["XAI_API_KEY"] == "xai-secret"
    assert resolved["XAI_OAUTH_ACCESS_TOKEN"] == "xai-oauth-secret"
    assert resolved["TELEGRAM_BOT_TOKEN"] == "telegram-secret"
    assert resolved["TWILIO_AUTH_TOKEN"] == "twilio-secret"
    assert env["XAI_API_KEY"] == "xai-secret"
    assert env["XAI_OAUTH_ACCESS_TOKEN"] == "xai-oauth-secret"
    assert env["TELEGRAM_BOT_TOKEN"] == "telegram-secret"
    assert env["TWILIO_AUTH_TOKEN"] == "twilio-secret"


def test_materialize_secret_file_envs_includes_broker_cli_tokens(tmp_path: Path) -> None:
    bitwarden_secret_path = tmp_path / "bw.session"
    onepassword_secret_path = tmp_path / "op.token"
    keepass_secret_path = tmp_path / "keepass.password"
    vault_secret_path = tmp_path / "vault.token"
    bitwarden_secret_path.write_text("bw-secret\n", encoding="utf-8")
    onepassword_secret_path.write_text("ops_secret\n", encoding="utf-8")
    keepass_secret_path.write_text("kdbx-secret\n", encoding="utf-8")
    vault_secret_path.write_text("hvs_secret\n", encoding="utf-8")
    env = {
        "BW_SESSION_FILE": str(bitwarden_secret_path),
        "OP_SERVICE_ACCOUNT_TOKEN_FILE": str(onepassword_secret_path),
        "KEEPASSXC_PASSWORD_FILE": str(keepass_secret_path),
        "VAULT_TOKEN_FILE": str(vault_secret_path),
    }

    resolved = materialize_secret_file_envs(env)

    assert resolved["BW_SESSION"] == "bw-secret"
    assert resolved["OP_SERVICE_ACCOUNT_TOKEN"] == "ops_secret"
    assert resolved["KEEPASSXC_PASSWORD"] == "kdbx-secret"
    assert resolved["VAULT_TOKEN"] == "hvs_secret"
    assert env["BW_SESSION"] == "bw-secret"
    assert env["OP_SERVICE_ACCOUNT_TOKEN"] == "ops_secret"
    assert env["KEEPASSXC_PASSWORD"] == "kdbx-secret"
    assert env["VAULT_TOKEN"] == "hvs_secret"


def test_google_application_credentials_is_no_longer_a_listed_secret(tmp_path: Path) -> None:
    # Google tools run with each person's own sign-in; startup refuses the
    # variable (tests/test_google_per_person.py), so the list no longer carries it.
    google_secret_path = tmp_path / "service-account.json"
    google_secret_path.write_text('{"private_key":"secret"}\n', encoding="utf-8")
    env = {"GOOGLE_APPLICATION_CREDENTIALS_FILE": str(google_secret_path)}

    resolved = materialize_secret_file_envs(env)

    assert "GOOGLE_APPLICATION_CREDENTIALS" not in resolved
    assert "GOOGLE_APPLICATION_CREDENTIALS" not in env
    assert "GOOGLE_APPLICATION_CREDENTIALS" not in {name for name, _entry in SECRET_ENV_LIST.entries()}
