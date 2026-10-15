"""Web search and crawling keys are linked accounts now; the old settings are refused by name.

``EXA_API_KEY``, ``BRAVE_SEARCH_API_KEY`` and ``CRAWL4AI_API_TOKEN`` (and their
``*_FILE`` forms) moved into the credential store, so startup refuses them in
the environment and in team.env, an MCP server header that still reads one is
refused, and the Crawl4AI settings that no longer have an effect are refused.
Each message names the variable or key and the ``/link`` command to use.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from runtime_settings.common import _normalize_external_mcp_server_list
from runtime_settings.services import (
    Crawl4AIServiceSettings,
    crawl4ai_base_url,
    reject_removed_web_service_env_vars,
    web_service_urls,
    WebServicesSettings,
)


@pytest.mark.parametrize(
    ("name", "command"),
    [
        ("EXA_API_KEY", "/link http:exa team"),
        ("BRAVE_SEARCH_API_KEY_FILE", "/link http:brave-search team"),
        ("CRAWL4AI_API_TOKEN", "/link mcp:crawl4ai team"),
    ],
)
def test_startup_refuses_a_key_that_is_a_linked_account_now(name: str, command: str) -> None:
    with pytest.raises(ValueError) as refused:
        reject_removed_web_service_env_vars({name: "value-not-shown", "PATH": "/usr/bin"})

    message = str(refused.value)
    assert f"Removed environment variable(s) are set: {name}." in message
    assert command in message
    assert "value-not-shown" not in message


def test_startup_refuses_the_removed_crawl4ai_switches() -> None:
    with pytest.raises(ValueError, match="CRAWL4AI_EXECUTE_JS_ENABLED.*no effect"):
        reject_removed_web_service_env_vars({"CRAWL4AI_EXECUTE_JS_ENABLED": "false"})
    reject_removed_web_service_env_vars({"CRAWL4AI_BASE_URL": "http://crawl4ai:11235", "SEARXNG_BASE_URL": "x"})


def test_the_launcher_refuses_a_key_left_in_team_env(tmp_path: Path) -> None:
    from docker.launch_headless import build_process_env
    from tests.test_docker_launch_headless import _make_config

    env_file = tmp_path / "team.env"
    env_file.write_text("EXA_API_KEY=left-behind\n", encoding="utf-8")

    with pytest.raises(ValueError, match="EXA_API_KEY.*/link http:exa team"):
        build_process_env(_make_config(tmp_path), env_path=env_file, base_env={})


def test_an_mcp_header_that_reads_a_removed_key_is_refused_with_the_fix() -> None:
    servers = [{"name": "crawl4ai", "headers": {"Authorization": "Bearer ${CRAWL4AI_API_TOKEN}"}}]

    with pytest.raises(ValueError) as refused:
        _normalize_external_mcp_server_list(servers, preserve_none=False)

    message = str(refused.value)
    assert "Authorization: ${CRAWL4AI_API_TOKEN}" in message
    assert "credential_injections" in message and "/link mcp:crawl4ai team" in message


@pytest.mark.parametrize(
    ("key", "value"),
    [("verify_tls", False), ("allow_execute_js", True), ("expected_version", "0.9.3"), ("job_timeout_seconds", 60)],
)
def test_removed_crawl4ai_settings_with_a_changed_value_are_refused_by_name(key: str, value: object) -> None:
    with pytest.raises(ValueError, match=f"web_services.crawl4ai.{key}={value}: this setting has been removed"):
        Crawl4AIServiceSettings.model_validate({"base_url": "http://crawl4ai:11235", key: value})


def test_removed_crawl4ai_settings_that_hold_their_former_defaults_are_dropped() -> None:
    """Saved instance files hold every setting; the former defaults changed nothing, so they go quietly."""

    saved = {
        "base_url": "http://crawl4ai:11235",
        "verify_tls": True,
        "timeout_seconds": 120,
        "expected_version": "0.9.2",
        "allow_execute_js": False,
        "job_poll_seconds": 2,
        "job_timeout_seconds": 300.0,
    }

    settings = Crawl4AIServiceSettings.model_validate(saved)

    assert settings.model_dump() == {"base_url": "http://crawl4ai:11235", "timeout_seconds": 120.0}


def test_the_crawl4ai_address_is_the_same_for_the_tool_and_the_broker() -> None:
    settings = WebServicesSettings.model_validate({"crawl4ai": {"base_url": "http://crawl4ai:11235/"}})

    assert web_service_urls(settings, env={}) == {"web_services.crawl4ai.base_url": "http://crawl4ai:11235"}
    assert crawl4ai_base_url("http://ignored:1", {"CRAWL4AI_BASE_URL": "http://override:2"}) == "http://override:2"
