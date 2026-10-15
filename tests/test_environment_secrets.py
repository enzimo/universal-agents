"""Secrets from the environment (launch.secrets_source: environment) and the tolerant settings import.

Covers the Docker Swarm install where every secret reaches the container as an
environment variable or Docker secret: onboarding writes no team.env and
generates nothing, lists missing secrets by name only, and the launcher reads
no env file. Also covers importing an old instance_config.yaml that the full
loaders would refuse.
"""

from __future__ import annotations

import logging
from pathlib import Path

import pytest
from aiohttp.test_utils import TestClient, TestServer

from assistant_cli import GatewayOnboardingSeed, _onboard, _resolve_onboarding_seed, build_parser
from assistant_gateway.config import GatewayInstanceConfig, LaunchSettings
from assistant_gateway.instance_secrets import (
    internal_credential_request,
    missing_secrets,
    required_secrets,
)
from assistant_gateway.onboarding_account_keys import read_handover
from assistant_gateway.onboarding_engine import (
    OnboardingTemplate,
    OnboardingWriteOptions,
    normalize_onboarding_answers,
    write_onboarded_instance,
)
from assistant_gateway.onboarding_web.server import OnboardingWebService, OnboardingWebSettings
from assistant_gateway.onboarding_web.settings_import import (
    OnboardingImportError,
    read_onboarding_import,
)
from docker.launch_headless import resolve_env_file_path, task_context_keys_message
from internal_credentials import (
    InternalCredentialRequest,
    generate_internal_credentials,
    internal_credential_names,
)
from runtime_settings.secrets_source import SECRETS_SOURCE_ENV_FILE, SECRETS_SOURCE_ENVIRONMENT

# Every name onboarding may require or read; cleared so the host running the
# tests cannot supply them by accident.
SECRET_NAMES = (
    "ACCESS_GATEWAY_SIGNING_KEY",
    "ACCESS_SCHEDULER_SIGNING_KEY",
    "ACCESS_CONTEXT_VERIFY_KEYS",
    "ACTIVITY_COLLECTOR_USER_TOKEN",
    "ACTIVITY_COLLECTOR_ADMIN_TOKEN",
    "ACTIVITY_COLLECTOR_INGEST_TOKEN",
    "WEB_CHAT_AUTH_TOKEN",
    "NATS_USER",
    "NATS_PASSWORD",
    "NATS_URL",
    "NATS_SERVERS",
    "NATS_CREDENTIALS_FILE",
    "POSTGRES_DSN",
    "SECURE_TOOL_BROKER_TOKEN",
    "GOG_KEYRING_PASSWORD",
    "OPENAI_API_KEY",
    "LITELLM_API_KEY",
    "TELEGRAM_BOT_TOKEN",
    "EXA_API_KEY",
    "NETWORK_PUBLIC_ORIGIN",
    "INTERNAL_WEBSITE_PUBLIC_PORT",
)
PRESENT_VALUE = "value-that-must-never-be-shown"


def _clear_secret_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in SECRET_NAMES:
        monkeypatch.delenv(name, raising=False)
        monkeypatch.delenv(f"{name}_FILE", raising=False)


def _answers(tmp_path: Path, **overrides: object) -> dict[str, object]:
    raw = {
        "instance_name": "swarm",
        "instance_root": str(tmp_path / "instance"),
        "team_id": "team_swarm",
        "assistant_name": "Nova",
        "user_display_name": "Owner",
        "model_provider": "OpenAI",
        "model_id": "gpt-test",
        "secrets_source": SECRETS_SOURCE_ENVIRONMENT,
        **overrides,
    }
    return normalize_onboarding_answers(raw, OnboardingTemplate(team_id="team_swarm"))


def test_environment_mode_writes_no_team_env_and_generates_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _clear_secret_env(monkeypatch)
    monkeypatch.setenv("ACTIVITY_COLLECTOR_USER_TOKEN", PRESENT_VALUE)

    def refuse_generation(*_args: object, **_kwargs: object) -> dict[str, str]:
        raise AssertionError("nothing may be generated when secrets come from the environment")

    monkeypatch.setattr("assistant_gateway.instance_secrets.generate_internal_credentials", refuse_generation)
    seeded_envs: list[dict[str, str]] = []
    monkeypatch.setattr(
        "assistant_gateway.onboarding_engine.seed_default_gateway_user",
        lambda _config, *, env: seeded_envs.append(dict(env)) or True,
    )
    answers = _answers(tmp_path, exa_api_key="typed-in-form")

    result = write_onboarded_instance(
        answers=answers,
        template=OnboardingTemplate(team_id="team_swarm"),
        options=OnboardingWriteOptions(generate_internal_credentials=True),
    )

    instance_root = tmp_path / "instance"
    assert not (instance_root / "team.env").exists()
    assert result.env_path is None
    assert answers["web_chat_auth_token"] == "", "no Web Chat token is generated either"
    config = GatewayInstanceConfig.load(instance_root / "instance_config.yaml")
    assert config.launch.secrets_source == SECRETS_SOURCE_ENVIRONMENT
    assert config.launch.env_file_path is None
    # The first user is created with the process environment's view.
    assert seeded_envs and seeded_envs[0]["ACTIVITY_COLLECTOR_USER_TOKEN"] == PRESENT_VALUE
    missing = {item.name for item in result.missing_secrets}
    assert {
        "ACCESS_GATEWAY_SIGNING_KEY",
        "ACCESS_SCHEDULER_SIGNING_KEY",
        "ACTIVITY_COLLECTOR_ADMIN_TOKEN",
        "WEB_CHAT_AUTH_TOKEN",
    } <= missing
    # Model keys are not environment secrets: they are saved with /auth model.
    assert "OPENAI_API_KEY" not in missing
    assert "ACTIVITY_COLLECTOR_USER_TOKEN" not in missing, "present in the environment"
    assert "ACCESS_CONTEXT_VERIFY_KEYS" not in missing, "derived from the signing keys"
    # The typed Exa key is not an environment secret: it waits for the gateway,
    # which links it for the team in the credential store at its first start.
    assert "EXA_API_KEY" not in missing
    assert result.unsaved_secret_names == ()
    handover = read_handover(instance_root)
    assert handover is not None and handover.keys == {"http:exa": "typed-in-form"}
    assert handover.person_id == config.user.canonical_user_id
    assert (config.layout.onboarding_account_keys_path.stat().st_mode & 0o777) == 0o600


def test_missing_secrets_are_named_with_a_fix_and_never_show_values(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    _clear_secret_env(monkeypatch)
    secret_file = tmp_path / "gateway_key"
    secret_file.write_text(PRESENT_VALUE + "\n", encoding="utf-8")
    monkeypatch.setenv("ACCESS_GATEWAY_SIGNING_KEY_FILE", str(secret_file))
    caplog.set_level(logging.INFO)

    result = write_onboarded_instance(
        answers=_answers(tmp_path),
        template=OnboardingTemplate(team_id="team_swarm"),
        options=OnboardingWriteOptions(),
    )

    by_name = {item.name: item for item in result.missing_secrets}
    assert "ACCESS_GATEWAY_SIGNING_KEY" not in by_name, "a Docker secret file counts as present"
    scheduler = by_name["ACCESS_SCHEDULER_SIGNING_KEY"]
    assert "-m scripts.generate_internal_credentials --instance-name swarm" in scheduler.how_to_create
    assert "--print" in scheduler.how_to_create
    assert "one-line command" in scheduler.how_to_create
    rendered = repr(result.missing_secrets) + caplog.text
    assert PRESENT_VALUE not in rendered
    assert "event=onboarding_secrets_from_environment" in caplog.text


def test_required_secrets_follow_the_configuration(tmp_path: Path) -> None:
    config = GatewayInstanceConfig.create(
        instance_name="swarm",
        instance_root=str(tmp_path / "instance"),
        team_id="team_swarm",
        timezone="UTC",
        manager_storage_backend="postgres",
    )
    config.llm.provider = "openai-codex"
    config.security_layer.mode = "broker"
    config.interactive_apps.telegram.enabled = True

    names = [item.name for item in required_secrets(config, env={})]

    assert "POSTGRES_DSN" in names
    assert "SECURE_TOOL_BROKER_TOKEN" in names
    assert "TELEGRAM_BOT_TOKEN" in names
    assert not {"OPENAI_API_KEY", "OPENAI_CODEX_ACCESS_TOKEN"} & set(names), "OAuth keeps its own store"
    assert not {"NATS_USER", "NATS_PASSWORD"} & set(names), "the local broker makes its own"
    assert "GOG_KEYRING_PASSWORD" not in names
    assert missing_secrets(config, env={name: "x" for name in names}) == ()


@pytest.mark.parametrize(
    "request_kwargs",
    [
        {},
        {"include_nats_credentials": True, "storage_backend": "postgres"},
        {"include_web_chat_token": False},
    ],
)
def test_internal_credential_names_match_what_generation_creates(request_kwargs: dict[str, object]) -> None:
    request = InternalCredentialRequest(instance_name="swarm", **request_kwargs)  # type: ignore[arg-type]

    assert set(internal_credential_names(request)) == set(generate_internal_credentials(request))


def test_env_file_mode_still_writes_team_env_with_generated_credentials(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _clear_secret_env(monkeypatch)

    result = write_onboarded_instance(
        answers=_answers(tmp_path, secrets_source=SECRETS_SOURCE_ENV_FILE),
        template=OnboardingTemplate(team_id="team_swarm"),
        options=OnboardingWriteOptions(),
    )

    env_path = tmp_path / "instance" / "team.env"
    assert result.env_path == env_path.resolve()
    env_text = env_path.read_text(encoding="utf-8")
    assert "ACCESS_GATEWAY_SIGNING_KEY=" in env_text
    assert "WEB_CHAT_AUTH_TOKEN=uaweb_" in env_text
    assert result.missing_secrets == ()
    config = GatewayInstanceConfig.load(tmp_path / "instance" / "instance_config.yaml")
    assert config.launch.secrets_source == SECRETS_SOURCE_ENV_FILE
    assert config.launch.env_file_path == str(env_path.resolve())


def test_launch_settings_refuse_an_env_file_with_secrets_from_the_environment() -> None:
    with pytest.raises(ValueError, match="launch.env_file_path must be empty"):
        LaunchSettings(secrets_source=SECRETS_SOURCE_ENVIRONMENT, env_file_path="/data/team.env")
    with pytest.raises(ValueError, match="launch.secrets_source must be one of"):
        LaunchSettings(secrets_source="vault")


def _environment_config(tmp_path: Path) -> GatewayInstanceConfig:
    config = GatewayInstanceConfig.create(
        instance_name="swarm", instance_root=str(tmp_path / "instance"), team_id="team_swarm", timezone="UTC"
    )
    config.launch.env_file_path = None
    config.launch.secrets_source = SECRETS_SOURCE_ENVIRONMENT  # type: ignore[assignment]
    return config


def test_launcher_ignores_an_existing_team_env_with_one_warning(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    config = _environment_config(tmp_path)
    team_env = config.instance_root_path / "team.env"
    team_env.parent.mkdir(parents=True, exist_ok=True)
    team_env.write_text("OPENAI_API_KEY=stale\n", encoding="utf-8")
    caplog.set_level(logging.WARNING)

    assert resolve_env_file_path(config, None) is None
    assert resolve_env_file_path(config, None, report_ignored_env_file=False) is None

    warnings = [record for record in caplog.records if "event=launch_team_env_ignored" in record.getMessage()]
    assert len(warnings) == 1
    assert str(team_env) in warnings[0].getMessage()
    assert "stale" not in caplog.text


def test_launcher_without_team_env_in_environment_mode_stays_quiet(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.WARNING)

    assert resolve_env_file_path(_environment_config(tmp_path), None) is None
    assert caplog.text == ""


def test_launcher_refuses_an_explicit_env_file_in_environment_mode(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="launch.secrets_source is environment"):
        resolve_env_file_path(_environment_config(tmp_path), str(tmp_path / "other.env"))


def test_missing_signing_keys_refusal_points_to_docker_secrets_in_environment_mode(tmp_path: Path) -> None:
    message = task_context_keys_message(_environment_config(tmp_path), None, ["ACCESS_SCHEDULER_SIGNING_KEY"])

    assert "task_context_keys_missing" in message
    assert "ACCESS_SCHEDULER_SIGNING_KEY_FILE" in message
    assert "Docker secret" in message
    assert "--print" in message
    assert "team.env" not in message


OLD_INSTANCE_CONFIG = """
schema_version: 9
instance_name: old_team
instance_root: /srv/old-instance
gateway:
  team_id: team_old
  storage:
    backend: sqlite
user:
  display_name: Riley
  timezone: Europe/Berlin
assistant:
  display_name: Nova
authorization:
  enabled: true
  engine: cedar
human_interactions:
  max_steps: 4
tool_policy:
  data_folders:
    - /srv/data
agent:
  memory_graph_enabled: true
  external_mcp_servers:
    - name: search
      continue_on_error: true
workspace:
  python_envs_dir: python_envs
llm:
  provider: litellm
  model_id: old-model
  litellm_api_base: https://llm.example.test/v1
  failover:
    enabled: true
    state_path: failover.json
interactive_apps:
  telegram:
    enabled: true
    allowed_user_ids: ["42"]
mcp_profiles:
  research: {}
agent_types:
  Researcher: {}
activity_collector:
  user_token: not-for-the-browser
  bind_port: 18600
"""


def test_import_of_an_old_instance_config_lists_what_it_skipped() -> None:
    imported = read_onboarding_import("instance_config.yaml", OLD_INSTANCE_CONFIG)

    assert imported.kind == "instance_config"
    assert imported.values["team_id"] == "team_old"
    assert imported.values["timezone"] == "Europe/Berlin"
    assert imported.values["telegram_enabled"] is True, "the pre-plugin channel shape is read too"
    assert imported.values["telegram_allowed_user_ids"] == ["42"]
    assert imported.values["llm_failover"] == {"enabled": True}, "the removed state_path is dropped"
    assert imported.values["activity_collector"] == {"bind_port": 18600}, "tokens never reach the form"
    assert "instance_root" not in imported.values
    skipped = {item.path: item.reason for item in imported.skipped}
    for removed in (
        "authorization.engine",
        "human_interactions",
        "tool_policy.data_folders",
        "workspace.python_envs_dir",
        "llm.failover.state_path",
        "agent.external_mcp_servers.search.continue_on_error",
    ):
        assert "Removed" in skipped[removed], removed
    assert "own folder" in skipped["instance_root"]
    assert "MCP servers" in skipped["mcp_profiles"]
    assert "Agent type" in skipped["agent_types"]
    assert "Access" in skipped["authorization"]
    assert "external_mcp_servers" in skipped["agent"]
    assert "human_interactions" not in {path for path, reason in skipped.items() if "Removed" not in reason}


@pytest.mark.parametrize(
    ("content", "expected"),
    [
        ("", "is empty"),
        ("llm: [unclosed", "is not valid YAML near line"),
        ("- just\n- a list\n", "does not hold settings"),
    ],
)
def test_import_of_an_unreadable_file_explains_the_problem(content: str, expected: str) -> None:
    with pytest.raises(OnboardingImportError, match=expected):
        read_onboarding_import("instance_config.yaml", content)


@pytest.mark.asyncio
async def test_web_import_never_returns_a_server_error(tmp_path: Path) -> None:
    service = OnboardingWebService(
        OnboardingWebSettings(host="127.0.0.1", port=18090, instance_root=str(tmp_path / "new"), restart_on_complete=False)
    )

    async with TestClient(TestServer(service.build_app())) as client:
        good = await client.post(
            "/api/onboarding/import-config",
            json={"filename": "instance_config.yaml", "content": OLD_INSTANCE_CONFIG},
        )
        good_payload = await good.json()
        bad = await client.post(
            "/api/onboarding/import-config",
            json={"filename": "instance_config.yaml", "content": "llm: [unclosed"},
        )
        bad_payload = await bad.json()

    assert good.status == 200
    assert good_payload["settings"]["team_id"] == "team_old"
    assert good_payload["settings"]["model_provider"] == "OpenAI Compatible"
    assert good_payload["settings"]["model_endpoint"] == "https://llm.example.test/v1"
    assert "instance_root" not in good_payload["settings"]
    assert "/srv/old-instance" not in str(good_payload["settings"])
    assert any(item["path"] == "authorization.engine" for item in good_payload["skipped"])
    assert bad.status == 400
    assert bad_payload["status"] == "error"
    assert "is not valid YAML" in bad_payload["message"]


@pytest.mark.asyncio
async def test_web_onboarding_in_environment_mode_saves_no_uploaded_env_and_blocks_launch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _clear_secret_env(monkeypatch)
    instance_root = tmp_path / "instance"
    service = OnboardingWebService(
        OnboardingWebSettings(host="127.0.0.1", port=18090, instance_root=str(instance_root), restart_on_complete=True)
    )

    async with TestClient(TestServer(service.build_app())) as client:
        complete = await client.post(
            "/api/onboarding/complete",
            json={
                "instance_name": "swarm",
                "instance_root": "/srv/old-instance",
                "team_id": "team_swarm",
                "assistant_name": "Nova",
                "model_provider": "OpenAI Compatible",
                "model_id": "test-model",
                "secrets_source": SECRETS_SOURCE_ENVIRONMENT,
                "source_env_filename": "old.env",
                "source_env_content": "OPENAI_API_KEY=from-upload\n",
                "finalize_only": True,
            },
        )
        payload = await complete.json()
        restart = await client.post("/api/onboarding/restart", json={})
        bootstrap = await (await client.get("/api/bootstrap")).json()

    assert complete.status == 200
    assert payload["env_path"] is None
    assert payload["source_env_ignored"] is True
    assert "ACCESS_GATEWAY_SIGNING_KEY" in {item["name"] for item in payload["missing_secrets"]}
    assert "from-upload" not in str(payload)
    assert not (instance_root / "team.env").exists()
    assert (instance_root / "instance_config.yaml").exists(), "the page's remembered folder never moves the instance"
    assert restart.status == 409
    assert bootstrap["secrets_source"] == SECRETS_SOURCE_ENVIRONMENT, "a rerun keeps the instance's choice"


def _cli_args(tmp_path: Path, *extra: str):
    return build_parser().parse_args(
        ["onboard", "--instance-name", "swarm", "--instance-root", str(tmp_path / "instance"), "--non-interactive", *extra]
    )


def test_cli_onboard_from_environment_writes_no_team_env(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _clear_secret_env(monkeypatch)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(
        "assistant_cli._resolve_onboarding_seed",
        lambda _args: GatewayOnboardingSeed(template=OnboardingTemplate(team_id="team_swarm"), source_env_file_path=None),
    )

    _onboard(_cli_args(tmp_path, "--secrets-from-environment"))

    output = capsys.readouterr().out
    assert not (tmp_path / "instance" / "team.env").exists()
    config = GatewayInstanceConfig.load(tmp_path / "instance" / "instance_config.yaml")
    assert config.launch.secrets_source == SECRETS_SOURCE_ENVIRONMENT
    assert "no team.env was written" in output
    assert "ACCESS_GATEWAY_SIGNING_KEY" in output

    # A rerun without the flag keeps the stored choice.
    _onboard(_cli_args(tmp_path))
    assert not (tmp_path / "instance" / "team.env").exists()
    assert GatewayInstanceConfig.load(tmp_path / "instance" / "instance_config.yaml").launch.secrets_from_environment


def test_cli_refuses_team_env_options_with_secrets_from_the_environment(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _clear_secret_env(monkeypatch)
    monkeypatch.chdir(tmp_path)
    source_env = tmp_path / "old.env"
    source_env.write_text("OPENAI_API_KEY=x\n", encoding="utf-8")
    monkeypatch.setattr(
        "assistant_cli._resolve_onboarding_seed",
        lambda _args: GatewayOnboardingSeed(
            template=OnboardingTemplate(team_id="team_swarm"), source_env_file_path=str(source_env)
        ),
    )

    with pytest.raises(SystemExit, match="--base-env-file writes team.env"):
        _onboard(_cli_args(tmp_path, "--secrets-from-environment"))
    assert not (tmp_path / "instance" / "instance_config.yaml").exists()


def test_cli_base_config_names_an_instance_config_plainly(tmp_path: Path) -> None:
    instance_config = tmp_path / "instance_config.yaml"
    instance_config.write_text(OLD_INSTANCE_CONFIG, encoding="utf-8")

    with pytest.raises(SystemExit, match="--base-config takes a config.yaml"):
        _resolve_onboarding_seed(_cli_args(tmp_path, "--base-config", str(instance_config)))


def test_internal_credential_request_is_shared_by_generation_and_requirements(tmp_path: Path) -> None:
    config = _environment_config(tmp_path)
    request = internal_credential_request(config, env={})

    # The broker always runs (tests/test_secure_broker_token.py), so its token
    # and the gog keyring password are always generated.
    assert {"SECURE_TOOL_BROKER_TOKEN", "GOG_KEYRING_PASSWORD"} <= set(internal_credential_names(request))
    assert request.include_nats_credentials, "the default NATS endpoint is local"
