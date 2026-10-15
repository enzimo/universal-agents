"""The secure tool broker always runs, so its token always exists.

Every credentialed tool call (web research, GitHub, Google, Hugging Face, X
and MCP servers) goes through the broker, so no security mode or switch turns
it off. Onboarding (web and CLI) always creates ``SECURE_TOOL_BROKER_TOKEN``
(and the gog keyring password) in team.env, the environment-mode list of
required secrets always names the token, and the launcher refuses to start
with a plain message when it is missing (the broker itself refuses to start
with no token). The removed ways of turning the broker off
(``security_layer.mode: disabled``, ``security_layer.secure_cli_enabled`` and
``SECURE_CLI_ENABLED``) are refused at startup by name.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml
from pydantic import ValidationError

from assistant_gateway.config import GatewayInstanceConfig
from assistant_gateway.instance_secrets import internal_credential_request, required_secrets
from assistant_gateway.onboarding_engine import (
    OnboardingTemplate,
    OnboardingWriteOptions,
    normalize_onboarding_answers,
    write_onboarded_instance,
)
from assistant_gateway.onboarding_web.settings_import import read_onboarding_import
from config_loader import load_config, resolve_agent_type_settings
from docker.launch_headless import build_instance_env_defaults, missing_secret_refusal
from internal_credentials import internal_credential_names, parse_env_file
from runtime_settings.environment import _apply_env_overrides
from runtime_settings.secrets_source import SECRETS_SOURCE_ENV_FILE, SECRETS_SOURCE_ENVIRONMENT
from runtime_settings.security import SecurityLayerSettings, reject_removed_secure_cli_env_var
from secure_tool_broker.defaults import DEFAULT_INTERNAL_BASE_URL

BROKER_TOKEN = "SECURE_TOOL_BROKER_TOKEN"
KEYRING_PASSWORD = "GOG_KEYRING_PASSWORD"
SIGNING_KEYS = {"ACCESS_GATEWAY_SIGNING_KEY": "gateway-key", "ACCESS_SCHEDULER_SIGNING_KEY": "scheduler-key"}
MODES = ("broker", "openshell")


@pytest.fixture(autouse=True)
def _no_host_secrets(monkeypatch: pytest.MonkeyPatch) -> None:
    # The host running the tests must not supply the names under test.
    for name in (BROKER_TOKEN, KEYRING_PASSWORD, *SIGNING_KEYS):
        monkeypatch.delenv(name, raising=False)
        monkeypatch.delenv(f"{name}_FILE", raising=False)
    for name in ("SECURE_CLI_ENABLED", "SECURITY_LAYER_MODE"):
        monkeypatch.delenv(name, raising=False)


def _config(tmp_path: Path, *, mode: str = "broker") -> GatewayInstanceConfig:
    config = GatewayInstanceConfig.create(
        instance_name="tokens", instance_root=str(tmp_path / "instance"), team_id="team_tokens", timezone="UTC"
    )
    config.security_layer.mode = mode
    # MCP used to be one of the reasons the broker started; off here to show
    # the broker no longer depends on it.
    config.mcp.enabled = False
    return config


def _environment_mode(config: GatewayInstanceConfig) -> GatewayInstanceConfig:
    config.launch.env_file_path = None
    config.launch.secrets_source = SECRETS_SOURCE_ENVIRONMENT  # type: ignore[assignment]
    return config


@pytest.mark.parametrize("mode", MODES)
def test_credential_request_always_includes_the_broker_token(tmp_path: Path, mode: str) -> None:
    request = internal_credential_request(_config(tmp_path, mode=mode), env={})

    assert {BROKER_TOKEN, KEYRING_PASSWORD} <= set(internal_credential_names(request))


def test_default_web_install_gets_a_broker_token(tmp_path: Path) -> None:
    raw = {
        "instance_name": "tokens",
        "instance_root": str(tmp_path / "instance"),
        "team_id": "team_tokens",
        "assistant_name": "Nova",
        "user_display_name": "Owner",
        "model_provider": "OpenAI",
        "model_id": "gpt-test",
        "secrets_source": SECRETS_SOURCE_ENV_FILE,
    }
    template = OnboardingTemplate(team_id="team_tokens")

    write_onboarded_instance(
        answers=normalize_onboarding_answers(raw, template), template=template, options=OnboardingWriteOptions()
    )

    config = GatewayInstanceConfig.load(tmp_path / "instance" / "instance_config.yaml")
    values = parse_env_file(tmp_path / "instance" / "team.env")
    assert config.security_layer.mode == "broker"
    assert values[BROKER_TOKEN].startswith("stb_")
    assert values[KEYRING_PASSWORD].startswith("gog_")


@pytest.mark.parametrize("mode", MODES)
def test_onboarding_team_env_always_has_the_broker_token(tmp_path: Path, mode: str) -> None:
    # Web onboarding, ``assistant_cli onboard`` and the team import all use this one writer.
    from assistant_gateway.instance_secrets import ensure_internal_credential_env_file

    config = _config(tmp_path, mode=mode)

    values = parse_env_file(ensure_internal_credential_env_file(config))

    assert values[BROKER_TOKEN].startswith("stb_")
    assert values[KEYRING_PASSWORD].startswith("gog_")
    assert missing_secret_refusal(config, None, {**SIGNING_KEYS, **values}) is None


def test_environment_mode_always_lists_the_broker_token(tmp_path: Path) -> None:
    config = _environment_mode(_config(tmp_path))

    by_name = {item.name: item for item in required_secrets(config, env={})}

    assert BROKER_TOKEN in by_name
    assert KEYRING_PASSWORD not in by_name, "only Google Workspace sign-in needs it"
    assert "-m scripts.generate_internal_credentials --instance-name tokens" in by_name[BROKER_TOKEN].how_to_create
    assert "--include-broker-token" not in by_name[BROKER_TOKEN].how_to_create


def test_launcher_refuses_to_start_without_the_broker_token_and_names_the_fix(tmp_path: Path) -> None:
    config = _config(tmp_path)
    env_path = tmp_path / "instance" / "team.env"

    message = missing_secret_refusal(config, env_path, dict(SIGNING_KEYS))

    assert message is not None
    assert "No SECURE_TOOL_BROKER_TOKEN is set (reason_code=broker_token_missing)" in message
    assert "always starts" in message
    assert (
        f"python3 -m scripts.generate_internal_credentials --instance-name tokens --env-file {env_path} --write"
    ) in message
    assert missing_secret_refusal(config, env_path, {**SIGNING_KEYS, BROKER_TOKEN: "stb_x"}) is None


def test_launcher_refusal_points_to_a_docker_secret_in_environment_mode(tmp_path: Path) -> None:
    config = _environment_mode(_config(tmp_path))

    message = missing_secret_refusal(config, None, dict(SIGNING_KEYS))

    assert message is not None
    assert "SECURE_TOOL_BROKER_TOKEN_FILE" in message
    assert "--instance-name tokens --print" in message
    assert "team.env" not in message


def test_missing_signing_keys_are_reported_before_the_broker_token(tmp_path: Path) -> None:
    message = missing_secret_refusal(_config(tmp_path), None, {})

    assert message is not None and "task_context_keys_missing" in message


def test_every_process_gets_the_broker_address_and_no_secure_cli_switch(tmp_path: Path) -> None:
    env = build_instance_env_defaults(_config(tmp_path))

    assert env["SECURE_TOOL_BROKER_INTERNAL_BASE_URL"] == DEFAULT_INTERNAL_BASE_URL
    assert "SECURE_CLI_ENABLED" not in env


@pytest.mark.parametrize(
    ("payload", "names"),
    [
        pytest.param({"mode": "disabled"}, ["security_layer.mode 'disabled'"], id="mode-disabled"),
        pytest.param({"mode": " Disabled "}, ["security_layer.mode 'disabled'"], id="mode-disabled-spelled-loosely"),
        pytest.param({"secure_cli_enabled": True}, ["security_layer.secure_cli_enabled"], id="secure-cli-on"),
        pytest.param({"secure_cli_enabled": False}, ["security_layer.secure_cli_enabled"], id="secure-cli-off"),
        pytest.param(
            {"mode": "disabled", "secure_cli_enabled": False},
            ["security_layer.mode 'disabled'", "security_layer.secure_cli_enabled"],
            id="both",
        ),
    ],
)
def test_instance_config_refuses_the_removed_broker_switches(payload: dict, names: list[str]) -> None:
    with pytest.raises(ValidationError) as refused:
        SecurityLayerSettings.model_validate(payload)

    message = str(refused.value)
    for name in names:
        assert name in message
    assert "the secure tool broker now always runs" in message


def test_instance_config_file_with_a_removed_switch_does_not_load(tmp_path: Path) -> None:
    config = _config(tmp_path)
    config.save()
    path = config.config_path
    raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    raw["security_layer"]["secure_cli_enabled"] = True
    path.write_text(yaml.safe_dump(raw), encoding="utf-8")

    with pytest.raises(ValidationError, match="security_layer.secure_cli_enabled has been removed"):
        GatewayInstanceConfig.load(path)


@pytest.mark.parametrize(
    ("security_layer", "expected"),
    [
        pytest.param('  mode: "disabled"\n', "security_layer.mode 'disabled'", id="mode-disabled"),
        pytest.param("  secure_cli_enabled: false\n", "security_layer.secure_cli_enabled", id="secure-cli"),
    ],
)
def test_agent_config_yaml_refuses_the_removed_broker_switches(
    tmp_path: Path, security_layer: str, expected: str
) -> None:
    config_path = tmp_path / "config.yaml"
    config_path.write_text(
        f"schema_version: 1\nagent:\n  workspace_dir: \"{tmp_path / 'workspaces'}\"\n"
        f"security_layer:\n{security_layer}",
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match=expected):
        resolve_agent_type_settings(load_config(str(config_path)), "TeamArchitect")


def test_security_mode_environment_override_refuses_disabled(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SECURITY_LAYER_MODE", "disabled")
    config: dict = {"security_layer": {}}

    _apply_env_overrides(config)

    with pytest.raises(ValidationError, match="also set by SECURITY_LAYER_MODE"):
        SecurityLayerSettings.model_validate(config["security_layer"])


@pytest.mark.parametrize("value", ["true", "false", ""])
def test_secure_cli_environment_variable_is_refused_at_startup(value: str, monkeypatch: pytest.MonkeyPatch) -> None:
    reject_removed_secure_cli_env_var({})
    with pytest.raises(ValueError, match="Removed environment variable\\(s\\) are set: SECURE_CLI_ENABLED"):
        reject_removed_secure_cli_env_var({"SECURE_CLI_ENABLED": value})

    monkeypatch.setenv("SECURE_CLI_ENABLED", value)
    with pytest.raises(ValueError, match="SECURE_CLI_ENABLED"):
        _apply_env_overrides({})


def test_settings_import_leaves_out_the_removed_broker_switches() -> None:
    imported = read_onboarding_import(
        "instance_config.yaml",
        "instance_name: old\nsecurity_layer:\n  mode: disabled\n  secure_cli_enabled: false\n  broker_bind_port: 18601\n",
    )

    assert imported.values["security_layer"] == {"broker_bind_port": 18601}
    skipped = {item.path: item.reason for item in imported.skipped}
    assert "always runs" in skipped["security_layer.mode"]
    assert "Removed" in skipped["security_layer.secure_cli_enabled"]


def test_onboarding_refuses_a_disabled_security_mode(tmp_path: Path) -> None:
    raw = {
        "instance_name": "tokens",
        "instance_root": str(tmp_path / "instance"),
        "team_id": "team_tokens",
        "model_provider": "OpenAI",
        "model_id": "gpt-test",
        "security_layer_mode": "disabled",
    }

    with pytest.raises(ValueError, match="security_layer.mode 'disabled'"):
        normalize_onboarding_answers(raw, OnboardingTemplate(team_id="team_tokens"))
