"""Removed tool-credential variables stop every start, naming what replaces them.

GitHub and Google run under each person's own sign-in (/link cli:github,
/link cli:google) and Hugging Face under account links (/link cli:huggingface), all
kept in the credential store. A token still set in the environment would be one
team-wide credential for everyone, so the supervisor, each process's settings
load and the broker refuse it (runtime_settings.security).
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from runtime_settings.environment import _apply_env_overrides
from runtime_settings.security import (
    REMOVED_TOOL_CREDENTIAL_ENV_VARS,
    reject_removed_tool_credential_env_vars,
    removed_tool_credential_env_problems,
)
from secure_tool_broker.service import run_secure_tool_broker
from tests.broker_access_support import BROKER_ROOT, open_gate
from tests.test_docker_launch_headless import _make_config
from docker.launch_headless import build_process_env


@pytest.mark.parametrize(
    ("name", "next_step"),
    [
        ("GH_TOKEN", "/link cli:github"),
        ("GITHUB_TOKEN", "/link cli:github"),
        ("GH_ENTERPRISE_TOKEN", "/link cli:github"),
        ("GITHUB_ENTERPRISE_TOKEN", "/link cli:github"),
        ("GOG_CLIENT_SECRET", "/auth google team_client"),
        ("GOG_REFRESH_TOKEN", "/link cli:google"),
        ("GOOGLE_OAUTH_ACCESS_TOKEN", "/link cli:google"),
        ("GOOGLE_OAUTH_REFRESH_TOKEN", "/link cli:google"),
        ("GOG_TOKEN_FILE", "/link cli:google"),
        ("HF_TOKEN", "/link cli:huggingface"),
        # A Google service-account key file: one Google identity for the team.
        ("GOOGLE_APPLICATION_CREDENTIALS", "each person signs in with /link cli:google"),
    ],
)
def test_each_removed_variable_and_its_file_form_is_refused_with_the_next_step(name: str, next_step: str) -> None:
    for key in (name, f"{name}_FILE"):
        problems = removed_tool_credential_env_problems({"PATH": "/usr/bin", key: "secret-canary"})
        assert len(problems) == 1
        assert key in problems[0] and next_step in problems[0]
        assert "secret-canary" not in problems[0]
        with pytest.raises(ValueError, match=key):
            reject_removed_tool_credential_env_vars({key: "secret-canary"})


def test_messages_never_contain_the_value() -> None:
    problems = removed_tool_credential_env_problems({name: "secret-canary" for name in REMOVED_TOOL_CREDENTIAL_ENV_VARS})
    assert len(problems) == len(REMOVED_TOOL_CREDENTIAL_ENV_VARS)
    assert all("secret-canary" not in problem for problem in problems)


def test_the_kept_broker_secrets_still_start() -> None:
    reject_removed_tool_credential_env_vars(
        {"GOG_KEYRING_PASSWORD": "x", "BW_SESSION": "x", "SEALBOX_TOKEN_FILE": "/x", "VAULT_TOKEN": "x", "PATH": "/usr/bin"}
    )


def test_the_supervisor_refuses_a_removed_variable_in_team_env(tmp_path: Path) -> None:
    config = _make_config(tmp_path)
    env_file = tmp_path / "team.env"
    env_file.write_text("GH_TOKEN=ghp_canary\n", encoding="utf-8")

    with pytest.raises(ValueError, match="GH_TOKEN is set") as refused:
        build_process_env(config, env_path=env_file, base_env={})
    assert "ghp_canary" not in str(refused.value)


@pytest.mark.parametrize("key", ["GOOGLE_APPLICATION_CREDENTIALS", "GOOGLE_APPLICATION_CREDENTIALS_FILE"])
def test_every_start_refuses_a_google_service_account_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, key: str
) -> None:
    key_file = tmp_path / "service-account.json"
    key_file.write_text('{"private_key": "canary"}', encoding="utf-8")

    with pytest.raises(ValueError, match=f"{key} is set.*/link cli:google") as refused:
        build_process_env(_make_config(tmp_path), env_path=None, base_env={key: str(key_file)})
    assert "canary" not in str(refused.value)

    monkeypatch.setenv(key, str(key_file))
    with pytest.raises(ValueError, match=f"{key} is set"):
        _apply_env_overrides({})
    with pytest.raises(ValueError, match=f"{key} is set"):
        asyncio.run(run_secure_tool_broker(host="127.0.0.1", port=1, token=BROKER_ROOT, access_gate=open_gate()))


def test_each_process_settings_load_refuses_a_removed_variable(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HF_TOKEN", "hf_canary")
    with pytest.raises(ValueError, match="HF_TOKEN is set"):
        _apply_env_overrides({})


def test_the_broker_refuses_to_start_with_a_removed_variable(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("GOOGLE_OAUTH_REFRESH_TOKEN", "canary")
    with pytest.raises(ValueError, match="GOOGLE_OAUTH_REFRESH_TOKEN is set"):
        asyncio.run(run_secure_tool_broker(host="127.0.0.1", port=1, token=BROKER_ROOT, access_gate=open_gate()))
