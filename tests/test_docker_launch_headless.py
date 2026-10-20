from __future__ import annotations

import os
from pathlib import Path
import signal
import sys
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from assistant_gateway.config import (
    GatewayInstanceConfig,
    INSTANCE_CONFIG_SCHEMA_VERSION,
    StartupAgentSettings,
)
from docker.launch_headless import (
    append_headless_startup_notice,
    build_parser,
    build_instance_env_defaults,
    build_manager_command,
    build_startup_agent_commands,
    build_openshell_gateway_command,
    build_process_env,
    build_service_process_environments,
    cleanup_openshell_sandbox,
    ensure_managed_sealbox_service,
    ensure_openshell_gateway,
    ensure_openshell_sandbox,
    ensure_instance_config_ready_for_headless_launch,
    resolve_env_file_path,
    refresh_external_skills_before_launch,
    run_supervised,
    scoped_environment,
    should_start_managed_sealbox,
)


def _make_config(tmp_path: Path) -> GatewayInstanceConfig:
    return GatewayInstanceConfig.create(
        instance_name="dockertest",
        instance_root=str(tmp_path / "instance"),
        team_id="team_test",
        timezone="America/Phoenix",
    )


def test_headless_parser_accepts_external_skill_refresh_overrides() -> None:
    args = build_parser().parse_args(
        [
            "--config",
            "/data/instance_config.yaml",
            "--refresh-external-skills",
            "--skills-repository",
            "https://github.com/example/one.git",
            "--skills-repository",
            "https://github.com/example/two.git",
        ]
    )

    assert args.refresh_external_skills is True
    assert args.skills_repository == [
        "https://github.com/example/one.git",
        "https://github.com/example/two.git",
    ]


def test_refresh_external_skills_before_launch_uses_cli_repository_override(
    tmp_path: Path,
    monkeypatch,
) -> None:
    config = _make_config(tmp_path)
    config.workspace.external_skill_repositories = [
        "https://github.com/example/configured.git"
    ]
    calls: list[tuple[list[str], Path]] = []

    def fake_sync(repositories, destination):
        calls.append((list(repositories), Path(destination)))
        return SimpleNamespace(
            collisions=(),
            refreshed_count=1,
            destination=Path(destination),
            removed_skills=(),
            repositories=(object(),),
        )

    monkeypatch.setattr(
        "docker.launch_headless.sync_external_skill_repositories", fake_sync
    )

    result = refresh_external_skills_before_launch(
        config,
        env={},
        cli_refresh_requested=True,
        cli_repositories=["https://github.com/example/override.git"],
    )

    assert result is not None
    assert calls == [
        (
            ["https://github.com/example/override.git"],
            config.layout.skills_dir,
        )
    ]


def test_refresh_external_skills_before_launch_honors_config_and_env_toggle(
    tmp_path: Path,
    monkeypatch,
) -> None:
    config = _make_config(tmp_path)
    config.workspace.external_skill_repositories = [
        "https://github.com/example/configured.git"
    ]
    config.workspace.refresh_external_skills_on_start = True
    calls: list[list[str]] = []

    def fake_sync(repositories, destination):
        calls.append(list(repositories))
        return SimpleNamespace(
            collisions=(),
            refreshed_count=1,
            destination=Path(destination),
            removed_skills=(),
            repositories=(object(),),
        )

    monkeypatch.setattr(
        "docker.launch_headless.sync_external_skill_repositories", fake_sync
    )

    assert refresh_external_skills_before_launch(config, env={}) is not None
    assert (
        refresh_external_skills_before_launch(
            config,
            env={"TEAM_REFRESH_EXTERNAL_SKILLS": "false"},
        )
        is None
    )
    assert calls == [["https://github.com/example/configured.git"]]


def test_build_instance_env_defaults_uses_instance_runtime_paths(
    tmp_path: Path,
) -> None:
    config = _make_config(tmp_path)
    config.transport.nats_url = "nats://nats.internal:4222"
    config.transport.nats_servers = ["nats://nats.internal:4222"]
    config.transport.nats_name = "team-test"
    config.llm.xai_oauth.auth_store_path = "auth/custom_xai.json"
    config.llm.xai_oauth.refresh_skew_seconds = 2400
    config.llm.xai_oauth.refresh_timeout_seconds = 35

    env_defaults = build_instance_env_defaults(config)

    assert env_defaults["TEAM_INSTANCE_CONFIG"] == str(config.config_path)
    assert env_defaults["TEAM_INSTANCE_ROOT"] == str(config.instance_root_path)
    assert env_defaults["RUNTIME_RESTART_REQUEST_DIR"] == str(
        config.instance_root_path / "runtime" / "restart_requests"
    )
    assert env_defaults["AGENT_WORKSPACE_DIR"] == str(config.workspace_path)
    assert config.workspace_path == config.instance_root_path / "workspaces"
    # Every other location is derived from TEAM_INSTANCE_ROOT, so the launcher
    # no longer exports per-location overrides.
    for removed in (
        "SKILLS_DIR",
        "PYTHON_ENVS_DIR",
        "LLM_OVERRIDE_PATH",
        "TEAM_TEMPLATES_PATH",
        "TEAM_COMPOSITION_PATH",
    ):
        assert removed not in env_defaults
    assert env_defaults["TASK_HISTORY_DB_PATH"] == str(config.layout.task_database_path)
    assert env_defaults["TRANSPORT_TYPE"] == "nats"
    assert env_defaults["NATS_URL"] == "nats://nats.internal:4222"
    assert env_defaults["NATS_SERVERS"] == "nats://nats.internal:4222"
    assert env_defaults["NATS_NAME"] == "team-test"
    assert env_defaults["DEFAULT_TEAM_ID"] == "team_test"
    assert env_defaults["DEFAULT_REQUEST_TIMEZONE"] == "America/Phoenix"
    assert env_defaults["MEMORY_GRAPH_ENABLED"] == "false"
    assert env_defaults["ACTIVITY_COLLECTOR_BASE_URL"] == "http://127.0.0.1:8765"
    assert env_defaults["XAI_OAUTH_BASE_URL"] == "https://api.x.ai/v1"
    assert env_defaults["XAI_OAUTH_AUTH_STORE_PATH"] == "auth/custom_xai.json"
    assert env_defaults["XAI_OAUTH_REFRESH_SKEW_SECONDS"] == "2400"
    assert env_defaults["XAI_OAUTH_REFRESH_TIMEOUT_SECONDS"] == "35"
    assert env_defaults["LLM_PROVIDER"] == "litellm"
    assert env_defaults["LLM_MODEL_ID"] == "deepseek/deepseek-v4.1-flash"
    assert env_defaults["LITELLM_API_BASE"] == "https://openrouter.ai/api/v1"


def test_service_process_environments_bind_one_manifest_per_runtime(
    tmp_path: Path,
) -> None:
    manifests = {
        service: tmp_path / f"{service}.runtime-manifest.v1.json"
        for service in (
            "gateway",
            "activity_collector",
                "secure_broker",
                "execution_controller",
                "adapter_host",
        )
    }

    environments = build_service_process_environments(
        runtime_env={"SHARED": "runtime", "BW_SESSION": "broker-secret"},
        inherited_env={"SHARED": "inherited", "BW_SESSION": "broker-secret"},
        manifest_paths_by_service=manifests,
    )

    assert environments.gateway["UA_RUNTIME_MANIFEST"] == str(manifests["gateway"])
    assert environments.activity_collector["UA_RUNTIME_MANIFEST"] == str(
        manifests["activity_collector"]
    )
    assert environments.secure_broker["UA_RUNTIME_MANIFEST"] == str(
        manifests["secure_broker"]
    )
    assert environments.execution_controller["UA_RUNTIME_MANIFEST"] == str(
        manifests["execution_controller"]
    )
    assert environments.adapter_host["UA_RUNTIME_MANIFEST"] == str(
        manifests["adapter_host"]
    )
    assert environments.gateway["SHARED"] == "runtime"
    assert environments.secure_broker["SHARED"] == "inherited"
    assert "BW_SESSION" not in environments.gateway
    assert environments.secure_broker["BW_SESSION"] == "broker-secret"
    assert "BW_SESSION" not in environments.execution_controller
    assert "BW_SESSION" not in environments.adapter_host


def test_each_process_gets_only_its_own_broker_caller_token(tmp_path: Path) -> None:
    from access_control.caller_tokens import verify_caller_token
    from docker.launch_headless import (
        BROKER_TOKEN_ENV,
        build_startup_agent_environments,
    )

    root = "stb_root_secret"
    manager_env = scoped_environment(
        "manager", {"SHARED": "x", BROKER_TOKEN_ENV: root}, broker_root=root, caller_label="manager"
    )
    environments = build_service_process_environments(
        runtime_env={"SHARED": "x", BROKER_TOKEN_ENV: root},
        inherited_env={BROKER_TOKEN_ENV: root},
        manifest_paths_by_service={
            service: tmp_path / f"{service}.json"
            for service in ("gateway", "activity_collector", "secure_broker", "execution_controller", "adapter_host")
        },
    )
    agents = build_startup_agent_environments(
        (("ResearchAgent#1", ["run"]), ("ResearchAgent#2", ["run"]), ("Coder", ["run"])),
        base_env={"SHARED": "x", BROKER_TOKEN_ENV: "must-not-leak"},
        broker_root=root,
    )

    def label(env: dict[str, str]) -> str | None:
        caller = verify_caller_token(root, env.get(BROKER_TOKEN_ENV))
        return caller.label if caller else None

    assert label(manager_env) == "manager"
    assert label(environments.gateway) == "gateway"
    assert environments.secure_broker[BROKER_TOKEN_ENV] == root, "only the broker holds the root"
    for service_env in (environments.activity_collector, environments.execution_controller, environments.adapter_host):
        assert BROKER_TOKEN_ENV not in service_env
    assert label(agents["ResearchAgent#1"]) == label(agents["ResearchAgent#2"]) == "agent/ResearchAgent"
    assert label(agents["Coder"]) == "agent/Coder"
    assert root not in manager_env.values() and root not in environments.gateway.values()


def test_build_process_env_prefers_env_file_then_process_env(tmp_path: Path) -> None:
    config = _make_config(tmp_path)
    env_file = tmp_path / "team.env"
    env_file.write_text(
        "NATS_URL=nats://env-file-nats:4222\n"
        "DEFAULT_TEAM_ID=env-file-team\n"
        "EXTRA_TOKEN=from-file\n"
        "GOG_KEYRING_PASSWORD=from-file\n"
        "INTERNAL_WEBSITE_BASE_URL=http://docs-from-file.example.test\n",
        encoding="utf-8",
    )

    process_env = build_process_env(
        config,
        env_path=env_file,
        base_env={
            "DEFAULT_TEAM_ID": "process-team",
            "EXTRA_TOKEN": "from-process",
            "INTERNAL_WEBSITE_BASE_URL": "http://docs-from-process.example.test",
        },
    )

    assert process_env["NATS_URL"] == "nats://env-file-nats:4222"
    assert process_env["DEFAULT_TEAM_ID"] == "process-team"
    assert process_env["EXTRA_TOKEN"] == "from-process"
    assert process_env["GOG_KEYRING_PASSWORD"] == "from-file"
    assert (
        process_env["INTERNAL_WEBSITE_BASE_URL"]
        == "http://docs-from-process.example.test"
    )


def test_build_process_env_rejects_removed_public_url_env_vars(tmp_path: Path) -> None:
    config = _make_config(tmp_path)
    env_file = tmp_path / "team.env"
    env_file.write_text(
        "WEBSITE_APP_BASE_URL=http://old.example.test\n", encoding="utf-8"
    )

    try:
        build_process_env(config, env_path=env_file, base_env={})
    except ValueError as exc:
        assert "Removed environment variable" in str(exc)
        assert "WEBSITE_APP_BASE_URL" in str(exc)
    else:  # pragma: no cover - assertion aid
        raise AssertionError("removed public URL env var did not fail fast")


@pytest.mark.parametrize(
    "variable",
    ["SECURE_TOOL_BROKER_CREDENTIAL_CATALOG_PATH", "SECURE_TOOL_BROKER_LEGACY_OWNER_CANONICAL_USER_ID"],
)
def test_build_process_env_refuses_removed_catalog_import_env_vars(tmp_path: Path, variable: str) -> None:
    config = _make_config(tmp_path)
    env_file = tmp_path / "team.env"
    env_file.write_text(f"{variable}=old-value\n", encoding="utf-8")

    with pytest.raises(ValueError, match=rf"Removed environment variable\(s\) are set: {variable}"):
        build_process_env(config, env_path=env_file, base_env={})


def test_build_process_env_passes_only_the_catalog_database(tmp_path: Path) -> None:
    config = _make_config(tmp_path)

    process_env = build_process_env(config, env_path=None, base_env={})

    assert process_env["SECURE_TOOL_BROKER_CREDENTIAL_CATALOG_DB_PATH"] == str(
        config.credential_catalog_database_path
    )
    assert "SECURE_TOOL_BROKER_CREDENTIAL_CATALOG_PATH" not in process_env
    assert "SECURE_TOOL_BROKER_LEGACY_OWNER_CANONICAL_USER_ID" not in process_env


def test_resolve_env_file_path_uses_configured_env_file(tmp_path: Path) -> None:
    config = _make_config(tmp_path)
    config.launch.env_file_path = str(
        (config.instance_root_path / "team.env").resolve()
    )
    env_file = config.instance_root_path / "team.env"
    env_file.parent.mkdir(parents=True, exist_ok=True)
    env_file.write_text("OPENAI_API_KEY=configured\n", encoding="utf-8")

    assert resolve_env_file_path(config, None) == env_file.resolve()


def test_resolve_env_file_path_without_configured_env_returns_none(
    tmp_path: Path,
) -> None:
    config = _make_config(tmp_path)
    config.launch.env_file_path = ""
    env_file = config.instance_root_path / "team.env"
    env_file.parent.mkdir(parents=True, exist_ok=True)
    env_file.write_text("OPENAI_API_KEY=team\n", encoding="utf-8")

    assert resolve_env_file_path(config, None) is None


def test_build_process_env_materializes_gog_keyring_password_file(
    tmp_path: Path,
) -> None:
    config = _make_config(tmp_path)
    secret_file = tmp_path / "gog-keyring-password"
    secret_file.write_text("from-secret-file\n", encoding="utf-8")
    env_file = tmp_path / "team.env"
    env_file.write_text(f"GOG_KEYRING_PASSWORD_FILE={secret_file}\n", encoding="utf-8")

    process_env = build_process_env(config, env_path=env_file, base_env={})

    assert process_env["GOG_KEYRING_PASSWORD"] == "from-secret-file"
    assert process_env["GOG_KEYRING_PASSWORD_FILE"] == str(secret_file)


def test_build_startup_agent_commands_use_non_lead_processes(tmp_path: Path) -> None:
    config = _make_config(tmp_path)
    config.gateway.startup_agents = [
        StartupAgentSettings(named_agent_type="WebResearchAgent", count=2),
        StartupAgentSettings(named_agent_type="CodeWritingAgent", count=1),
        StartupAgentSettings(named_agent_type="TeamArchitect", count=1),
    ]

    commands = build_startup_agent_commands(
        config, python_executable="/usr/local/bin/python"
    )

    assert [label for label, _command in commands] == [
        "WebResearchAgent#1",
        "WebResearchAgent#2",
        "CodeWritingAgent",
        "MemReviewAgent",
    ]
    for label, command in commands:
        assert command[:3] == [
            "/usr/local/bin/python",
            "-u",
            str(Path.cwd() / "strands_main.py"),
        ]
        assert "--config" in command
        assert str(config.config_path) in command
        assert "--team-id" in command
        assert "team_test" in command
        assert "--workspace-dir" not in command
        assert "--task-history-db-path" in command
        assert "--is-lead" not in command
    assert (
        commands[0][1][commands[0][1].index("--agent-type") + 1] == "WebResearchAgent"
    )
    assert (
        commands[2][1][commands[2][1].index("--agent-type") + 1] == "CodeWritingAgent"
    )


def test_build_instance_env_defaults_uses_postgres_manager_storage(
    tmp_path: Path,
) -> None:
    config = GatewayInstanceConfig.create(
        instance_name="dockertest",
        instance_root=str(tmp_path / "instance"),
        team_id="team_test",
        timezone="America/Phoenix",
        manager_storage_backend="postgres",
        manager_storage_postgres_dsn="postgresql://team:secret@db/team_agents",
    )

    env_defaults = build_instance_env_defaults(config)
    command = build_manager_command(config, python_executable="/usr/local/bin/python")

    assert env_defaults["STORAGE_BACKEND"] == "postgres"
    assert env_defaults["POSTGRES_DSN"] == "postgresql://team:secret@db/team_agents"
    assert "TASK_HISTORY_DB_PATH" not in env_defaults
    assert "--task-history-db-path" not in command


def test_build_instance_env_defaults_exports_instance_embedding_settings(
    tmp_path: Path,
) -> None:
    config = _make_config(tmp_path)
    config.agent.memory_graph_enabled = True
    config.llm.provider = "litellm"
    config.llm.model_id = "gemini-3.1-flash-lite-preview"
    config.llm.temperature = 0.0
    config.llm.stream = False
    config.llm.request_timeout_seconds = 45.0
    config.llm.reasoning_trace_retention_messages = 6
    config.llm.litellm_api_base = "https://litellm.example.test"
    config.embedding.provider = "openai_compatible"
    config.embedding.model_id = "pplx-embed-v1-0.6b"
    config.embedding.api_base = "http://localhost:4000/v1"
    config.embedding.timeout_seconds = 20.0

    env_defaults = build_instance_env_defaults(config)

    assert env_defaults["MEMORY_GRAPH_ENABLED"] == "true"
    assert env_defaults["LLM_PROVIDER"] == "litellm"
    assert env_defaults["LLM_MODEL_ID"] == "gemini-3.1-flash-lite-preview"
    assert env_defaults["LLM_TEMPERATURE"] == "0.0"
    assert env_defaults["LLM_STREAM"] == "false"
    assert env_defaults["LLM_REQUEST_TIMEOUT_SECONDS"] == "45.0"
    assert env_defaults["LLM_REASONING_TRACE_RETENTION_MESSAGES"] == "6"
    assert env_defaults["LITELLM_API_BASE"] == "https://litellm.example.test"
    assert env_defaults["EMBEDDING_PROVIDER"] == "openai_compatible"
    assert env_defaults["EMBEDDING_MODEL_ID"] == "pplx-embed-v1-0.6b"
    assert env_defaults["EMBEDDING_API_BASE"] == "http://localhost:4000/v1"
    assert env_defaults["EMBEDDING_TIMEOUT_SECONDS"] == "20.0"
    assert "EMBEDDING_DIMENSIONS" not in env_defaults


def test_build_process_env_resolves_postgres_dsn_from_env_file(
    monkeypatch, tmp_path: Path
) -> None:
    monkeypatch.delenv("POSTGRES_DSN", raising=False)
    monkeypatch.delenv("POSTGRES_DSN_FILE", raising=False)
    config = GatewayInstanceConfig.create(
        instance_name="dockertest",
        instance_root=str(tmp_path / "instance"),
        team_id="team_test",
        timezone="America/Phoenix",
        manager_storage_backend="postgres",
    )
    secret_path = tmp_path / "postgres.dsn"
    secret_path.write_text(
        "postgresql://team:secret@db/from_env_file\n", encoding="utf-8"
    )
    env_file = tmp_path / "team.env"
    env_file.write_text(f"POSTGRES_DSN_FILE={secret_path}\n", encoding="utf-8")

    process_env = build_process_env(config, env_path=env_file, base_env={})

    assert process_env["STORAGE_BACKEND"] == "postgres"
    assert process_env["POSTGRES_DSN"] == "postgresql://team:secret@db/from_env_file"


def test_build_process_env_materializes_secret_file_variables(
    monkeypatch, tmp_path: Path
) -> None:
    monkeypatch.delenv("ACTIVITY_COLLECTOR_USER_TOKEN", raising=False)
    monkeypatch.delenv("ACTIVITY_COLLECTOR_USER_TOKEN_FILE", raising=False)
    monkeypatch.delenv("ACTIVITY_COLLECTOR_INGEST_TOKEN", raising=False)
    monkeypatch.delenv("ACTIVITY_COLLECTOR_INGEST_TOKEN_FILE", raising=False)
    config = _make_config(tmp_path)
    secret_path = tmp_path / "openai.key"
    secret_path.write_text("sk-from-secret-file\n", encoding="utf-8")
    collector_token_path = tmp_path / "collector_user_token"
    collector_token_path.write_text("collector-user-secret\n", encoding="utf-8")
    ingest_token_path = tmp_path / "collector_ingest_token"
    ingest_token_path.write_text("collector-ingest-secret\n", encoding="utf-8")
    env_file = tmp_path / "team.env"
    env_file.write_text(
        f"OPENAI_API_KEY_FILE={secret_path}\n"
        f"ACTIVITY_COLLECTOR_USER_TOKEN_FILE={collector_token_path}\n"
        f"ACTIVITY_COLLECTOR_INGEST_TOKEN_FILE={ingest_token_path}\n",
        encoding="utf-8",
    )

    process_env = build_process_env(config, env_path=env_file, base_env={})

    assert process_env["OPENAI_API_KEY"] == "sk-from-secret-file"
    assert process_env["ACTIVITY_COLLECTOR_USER_TOKEN"] == "collector-user-secret"
    assert process_env["ACTIVITY_COLLECTOR_INGEST_TOKEN"] == "collector-ingest-secret"


def test_build_process_env_secret_file_overrides_env_file_value(
    monkeypatch, tmp_path: Path
) -> None:
    monkeypatch.delenv("NATS_USER", raising=False)
    monkeypatch.delenv("NATS_USER_FILE", raising=False)
    monkeypatch.delenv("NATS_PASSWORD", raising=False)
    monkeypatch.delenv("NATS_PASSWORD_FILE", raising=False)
    config = _make_config(tmp_path)
    user_secret_path = tmp_path / "nats.user"
    password_secret_path = tmp_path / "nats.password"
    user_secret_path.write_text("user-from-secret-file\n", encoding="utf-8")
    password_secret_path.write_text("from-secret-file\n", encoding="utf-8")
    env_file = tmp_path / "team.env"
    env_file.write_text(
        "NATS_USER=user-from-team-env\nNATS_PASSWORD=from-team-env\n", encoding="utf-8"
    )

    process_env = build_process_env(
        config,
        env_path=env_file,
        base_env={
            "NATS_USER_FILE": str(user_secret_path),
            "NATS_PASSWORD_FILE": str(password_secret_path),
        },
    )

    assert process_env["NATS_USER"] == "user-from-secret-file"
    assert process_env["NATS_PASSWORD"] == "from-secret-file"


def test_web_chat_secret_file_overrides_the_token_onboarding_wrote_to_team_env(
    monkeypatch, tmp_path: Path
) -> None:
    """The gateway reads the token with a direct-value-first lookup, so the
    launcher must replace a team.env value with the Docker secret's contents."""

    monkeypatch.delenv("WEB_CHAT_AUTH_TOKEN", raising=False)
    monkeypatch.delenv("WEB_CHAT_AUTH_TOKEN_FILE", raising=False)
    config = _make_config(tmp_path)
    secret_path = tmp_path / "web_chat_token"
    secret_path.write_text("from-secret-file\n", encoding="utf-8")
    env_file = tmp_path / "team.env"
    env_file.write_text("WEB_CHAT_AUTH_TOKEN=from-team-env\n", encoding="utf-8")

    process_env = build_process_env(
        config,
        env_path=env_file,
        base_env={"WEB_CHAT_AUTH_TOKEN_FILE": str(secret_path)},
    )

    assert process_env["WEB_CHAT_AUTH_TOKEN"] == "from-secret-file"


def test_ensure_instance_config_ready_for_headless_launch_accepts_public_baseline(
    tmp_path: Path,
) -> None:
    config = _make_config(tmp_path)
    config.save()
    config_path = config.config_path

    notice = ensure_instance_config_ready_for_headless_launch(config_path)

    assert notice is None
    assert f"schema_version: {INSTANCE_CONFIG_SCHEMA_VERSION}" in config_path.read_text(
        encoding="utf-8"
    )
    assert list(config_path.parent.glob("instance_config.yaml.bak.*")) == []


def test_append_headless_startup_notice_appends_to_existing_payload() -> None:
    env = {"TEAM_HEADLESS_STARTUP_NOTICE": '["first notice"]'}

    append_headless_startup_notice(env, "second notice")

    assert env["TEAM_HEADLESS_STARTUP_NOTICE"] == '["first notice", "second notice"]'


def test_build_instance_env_defaults_includes_internal_website_defaults(
    tmp_path: Path,
) -> None:
    config = _make_config(tmp_path)

    env_defaults = build_instance_env_defaults(config)

    assert env_defaults["NETWORK_PUBLIC_ORIGIN"] == "http://127.0.0.1"
    assert env_defaults["INTERNAL_WEBSITE_ENABLED"] == "true"
    assert env_defaults["INTERNAL_WEBSITE_BIND_HOST"] == "0.0.0.0"
    assert env_defaults["INTERNAL_WEBSITE_BIND_PORT"] == "18090"
    assert env_defaults["INTERNAL_WEBSITE_PUBLIC_PORT"] == "18090"
    assert env_defaults["INTERNAL_WEBSITE_BASE_URL"] == "http://127.0.0.1:18090"
    # Published sites always live in state/published_sites; there is no override.
    assert "INTERNAL_WEBSITE_SITES_DIR" not in env_defaults
    assert "USER_DOCS_BIND_HOST" not in env_defaults
    assert "WEBSITE_APP_BASE_URL" not in env_defaults


def test_broker_tool_credentials_and_settings_reach_only_the_broker() -> None:
    inherited = (
        {
            "BW_CLIENTSECRET": "secret",
            "BW_CLIENTSECRET_FILE": "/run/secrets/bw-client-secret",
            "GH_CONFIG_DIR": "/data/broker_credentials/gh",
            "SECURE_TOOL_BROKER_CREDENTIALS_DIR": "/data/broker_credentials",
            "BW_SESSION": "secret",
            "BITWARDEN_SERVER_URL": "https://vault.example.test",
            "GOG_KEYRING_PASSWORD": "secret",
            "GOG_KEYRING_PASSWORD_FILE": "/run/secrets/gog-keyring-password",
            "OP_SERVICE_ACCOUNT_TOKEN": "secret",
            "OP_CONNECT_HOST": "https://op-connect.example.test",
            "OP_CONNECT_TOKEN": "secret",
            "OP_CONNECT_TOKEN_FILE": "/run/secrets/op-connect-token",
            "OP_CONFIG_DIR": "/data/broker_credentials/op",
            "OP_SESSION_example": "secret",
            "KEEPASSXC_PASSWORD_FILE": "/run/secrets/keepass-password",
            "VAULT_TOKEN": "secret",
            "SEALBOX_URL": "http://127.0.0.1:8767",
            "SEALBOX_TOKEN_FILE": "/run/secrets/sealbox-token",
            "SEALBOX_PUBLIC_KEY_FILE": "/run/secrets/sealbox-public.pem",
            "SEALBOX_PRIVATE_KEY_FILE": "/run/secrets/sealbox-private.pem",
            "SEALBOX_SERVER_PATH": "/usr/local/bin/sealbox-server",
            "SECURE_TOOL_BROKER_SEALBOX_MANAGE_SERVICE": "true",
            "SECURE_TOOL_BROKER_CREDENTIAL_STATE_BACKEND_MODE": "credential_source",
            "SECURE_TOOL_BROKER_CREDENTIAL_STATE_BACKEND_SOURCE": "vault",
            "SECURE_TOOL_BROKER_CREDENTIAL_STATE_BACKEND_NAMESPACE": "agent-credential-store",
            "SECURE_TOOL_BROKER_CREDENTIAL_STATE_BACKEND_FALLBACK_TO_FILES": "false",
            "OPENAI_API_KEY": "model-secret",
            "NORMAL": "value",
        }
    )
    broker_only = set(inherited) - {"OPENAI_API_KEY", "NORMAL"}

    for process in ("manager", "agent", "gateway", "activity_collector", "execution_controller", "adapter_host"):
        env = scoped_environment(process, inherited)
        assert not broker_only & set(env), process
        assert env["NORMAL"] == "value"
    assert scoped_environment("manager", inherited)["OPENAI_API_KEY"] == "model-secret"
    assert "OPENAI_API_KEY" not in scoped_environment("gateway", inherited)
    assert scoped_environment("secure_broker", inherited).keys() == inherited.keys() - {"OPENAI_API_KEY"}


def test_build_instance_env_defaults_sets_broker_credentials_dir(
    tmp_path: Path,
) -> None:
    config = _make_config(tmp_path)
    config.security_layer.mode = "broker"
    config.security_layer.broker_credentials_dir = "broker_credentials"
    config.security_layer.bitwarden_server_url = "https://vault.example.test"

    env_defaults = build_instance_env_defaults(config)
    expected_dir = str((config.instance_root_path / "broker_credentials").resolve())

    assert env_defaults["SECURE_TOOL_BROKER_CREDENTIALS_DIR"] == expected_dir
    assert (
        env_defaults["SECURE_TOOL_BROKER_CREDENTIAL_STATE_BACKEND_MODE"]
        == "credential_source"
    )
    assert (
        env_defaults["SECURE_TOOL_BROKER_CREDENTIAL_STATE_BACKEND_SOURCE"] == "sealbox"
    )
    assert (
        env_defaults["SECURE_TOOL_BROKER_CREDENTIAL_STATE_BACKEND_NAMESPACE"]
        == "team-cred-store"
    )
    assert env_defaults["SECURE_TOOL_BROKER_CREDENTIAL_TEMPLATE_OVERRIDES_PATH"] == str(
        (config.instance_root_path / "credential_template_overrides.yaml").resolve()
    )
    assert env_defaults["SECURE_TOOL_BROKER_SECRET_CACHE_TTL_SECONDS"] == "300"
    # File tools reach only registered folders, so the launcher no longer
    # passes deny lists or a workspace switch to agent processes.
    assert "TOOL_DENY_PATHS" not in env_defaults
    assert "TOOL_RESTRICT_TO_WORKSPACE" not in env_defaults
    assert env_defaults["BITWARDEN_SERVER_URL"] == "https://vault.example.test"
    assert env_defaults["SEALBOX_URL"] == "http://127.0.0.1:8767"
    assert env_defaults["SEALBOX_ROOT_TOKEN_FILE"] == str(
        Path(expected_dir) / "sealbox" / "root_token"
    )
    assert env_defaults["SECURE_TOOL_BROKER_SEALBOX_TENANT_MATERIAL_DIR"] == str(
        Path(expected_dir) / "sealbox" / "tenants"
    )
    assert env_defaults["SEALBOX_API_VERSION"] == "v2"
    assert "SEALBOX_TOKEN_FILE" not in env_defaults
    assert env_defaults["SEALBOX_OUTPUT_FORMAT"] == "json"


def test_build_process_env_resolves_broker_credentials_dir_env_override(
    tmp_path: Path,
) -> None:
    config = _make_config(tmp_path)
    config.security_layer.mode = "broker"
    override_dir = tmp_path / "external-broker-creds"

    env = build_process_env(
        config,
        env_path=None,
        base_env={"SECURE_TOOL_BROKER_CREDENTIALS_DIR": str(override_dir)},
    )

    assert env["SECURE_TOOL_BROKER_CREDENTIALS_DIR"] == str(override_dir.resolve())
    assert "TOOL_DENY_PATHS" not in env
    assert "TOOL_RESTRICT_TO_WORKSPACE" not in env


def test_ensure_managed_sealbox_service_initializes_root_only(
    tmp_path: Path, monkeypatch
) -> None:
    config = _make_config(tmp_path)
    config.security_layer.mode = "broker"
    env: dict[str, str] = {
        "SEALBOX_BIND_PORT": "9876",
        "SEALBOX_STORE_PATH": "custom/sealbox.db",
        "SEALBOX_ROOT_TOKEN_FILE": str(
            config.instance_root_path / "broker_credentials" / "custom" / "root_token"
        ),
    }
    started: list[tuple[list[str], dict[str, str]]] = []

    class FakeProcess:
        pid = 123
        returncode = None

        def poll(self):
            return self.returncode

        def terminate(self) -> None:
            self.returncode = 0

        def kill(self) -> None:
            self.returncode = 0

        def wait(self, timeout=None):
            return 0

    def fake_popen(command, *, env, **_kwargs):
        started.append((list(command), dict(env)))
        return FakeProcess()

    monkeypatch.setattr(
        "docker.launch_headless._sealbox_server_executable",
        lambda: "/usr/local/bin/sealbox-server",
    )
    monkeypatch.setattr("docker.launch_headless.subprocess.Popen", fake_popen)
    monkeypatch.setattr(
        "docker.launch_headless.wait_for_sealbox_tcp", lambda *_args, **_kwargs: None
    )

    assert should_start_managed_sealbox(config) is True
    assert (
        should_start_managed_sealbox(
            config,
            env={"SECURE_TOOL_BROKER_SEALBOX_MANAGE_SERVICE": "false"},
        )
        is False
    )

    service = ensure_managed_sealbox_service(config, env=env)

    assert service is not None
    assert started[0][0] == ["/usr/local/bin/sealbox-server"]
    assert started[0][1]["STORE_PATH"].endswith("broker_credentials/custom/sealbox.db")
    assert started[0][1]["AUTH_TOKEN_FILE"] == env["SEALBOX_ROOT_TOKEN_FILE"]
    assert "LEGACY_V1_ENABLED" not in started[0][1], "Sealbox no longer has the v1 API to switch off"
    assert started[0][1]["LISTEN_ADDR"] == "127.0.0.1:9876"
    assert Path(env["SEALBOX_ROOT_TOKEN_FILE"]).is_file()
    assert not list((config.instance_root_path / "broker_credentials").glob("**/*.pem"))
    assert env["SEALBOX_URL"] == "http://127.0.0.1:9876"


def test_build_openshell_gateway_command_uses_instance_state_and_plaintext_loopback(
    tmp_path: Path,
) -> None:
    config = _make_config(tmp_path)
    config.security_layer.mode = "openshell"
    config.security_layer.openshell.sandbox_image = "team-runtime:latest"
    env = {
        "OPENSHELL_GATEWAY_BIN": "/usr/local/bin/openshell-gateway",
        "XDG_STATE_HOME": str(tmp_path / "state"),
    }

    command = build_openshell_gateway_command(config, env=env)

    assert command[:7] == [
        "/usr/local/bin/openshell-gateway",
        "--bind-address",
        "127.0.0.1",
        "--port",
        "18080",
        "--drivers",
        "docker",
    ]
    assert "--disable-tls" in command
    assert ["--sandbox-image", "team-runtime:latest"] == command[
        command.index("--sandbox-image") : command.index("--sandbox-image") + 2
    ]
    db_url = command[command.index("--db-url") + 1]
    assert db_url.startswith(
        f"sqlite:{tmp_path / 'state' / 'openshell' / 'gateway.db'}"
    )
    assert not any("handshake" in part for part in command), "the secret stays off the command line"


def test_ensure_openshell_gateway_starts_registers_and_marks_env(
    tmp_path: Path, monkeypatch
) -> None:
    config = _make_config(tmp_path)
    config.security_layer.mode = "openshell"
    env = {
        "OPENSHELL_GATEWAY_BIN": "/usr/local/bin/openshell-gateway",
        "OPENSHELL_BIN": "/usr/local/bin/openshell",
        "BW_SESSION": "broker-secret",
        "ACCESS_GATEWAY_SIGNING_KEY": "gateway-signing-key",
    }
    started: list[list[str]] = []
    started_envs: list[dict[str, str]] = []
    openshell_calls: list[list[str]] = []

    class FakeProcess:
        pid = 123
        returncode = None

        def poll(self):
            return None

        def terminate(self) -> None:
            self.returncode = 0

        def kill(self) -> None:
            self.returncode = 0

        def wait(self, timeout=None):
            return 0

    def fake_popen(command, **kwargs):
        started.append(list(command))
        started_envs.append(dict(kwargs["env"]))
        return FakeProcess()

    def fake_run(args, *, env, timeout_seconds):
        openshell_calls.append(list(args))
        return type("Completed", (), {"returncode": 0, "stdout": ""})()

    monkeypatch.setattr("docker.launch_headless.subprocess.Popen", fake_popen)
    monkeypatch.setattr("docker.launch_headless._run_openshell", fake_run)

    gateway = ensure_openshell_gateway(config, env=env)

    assert gateway is not None
    assert gateway.name == "team-dockertest"
    assert gateway.endpoint == "http://127.0.0.1:18080"
    assert env["OPENSHELL_GATEWAY"] == "team-dockertest"
    assert env["OPENSHELL_GATEWAY_ENDPOINT"] == "http://127.0.0.1:18080"
    assert env["XDG_CONFIG_HOME"].endswith("runtime/openshell/config")
    assert started[0][0] == "/usr/local/bin/openshell-gateway"
    # The handshake secret travels in the gateway's environment, never on its
    # command line, and the gateway gets none of the runtime's other secrets.
    secret = started_envs[0]["OPENSHELL_SSH_HANDSHAKE_SECRET"]
    assert len(secret) == 64
    assert not any(secret in part for part in started[0])
    assert "BW_SESSION" not in started_envs[0]
    assert "ACCESS_GATEWAY_SIGNING_KEY" not in started_envs[0]
    stored = config.instance_root_path / "runtime" / "openshell" / "ssh_handshake_secret"
    assert stored.read_text(encoding="utf-8").strip() == secret
    assert stored.stat().st_mode & 0o777 == 0o600
    assert openshell_calls[:3] == [
        ["gateway", "remove", "team-dockertest"],
        [
            "gateway",
            "add",
            "http://127.0.0.1:18080",
            "--local",
            "--name",
            "team-dockertest",
        ],
        ["status"],
    ]


def test_ensure_openshell_gateway_can_use_external_gateway(
    tmp_path: Path, monkeypatch
) -> None:
    config = _make_config(tmp_path)
    config.security_layer.mode = "openshell"
    config.security_layer.openshell.manage_gateway = False
    env = {"OPENSHELL_GATEWAY_ENDPOINT": "http://openshell.example.test:18080"}

    def fail_popen(*_args, **_kwargs):
        raise AssertionError("external OpenShell gateway must not be started")

    monkeypatch.setattr("docker.launch_headless.subprocess.Popen", fail_popen)

    gateway = ensure_openshell_gateway(config, env=env)

    assert gateway is None
    assert env["OPENSHELL_GATEWAY"] == "team-dockertest"
    assert env["OPENSHELL_GATEWAY_ENDPOINT"] == "http://openshell.example.test:18080"


def test_openshell_sandboxes_are_prepared_per_person_not_created_at_start(
    tmp_path: Path, monkeypatch
) -> None:
    """The launcher only passes settings; each person's sandbox is created on first use."""

    config = _make_config(tmp_path)
    config.security_layer.mode = "openshell"
    config.security_layer.openshell.sandbox_image = "team-runtime:latest"
    config.security_layer.openshell.policy_file = "/policies/team.yaml"
    env = {"PATH": "/usr/local/bin"}
    calls: list[list[str]] = []
    monkeypatch.setattr(
        "docker.launch_headless._run_openshell",
        lambda args, **_kwargs: calls.append(list(args)),
    )

    sandbox = ensure_openshell_sandbox(config, env=env)

    assert sandbox is not None and sandbox.prefix == "team-dockertest"
    assert calls == [], "no sandbox holding every person's files is created"
    assert env["OPENSHELL_SANDBOX_NAME"] == "team-dockertest"
    assert env["OPENSHELL_SANDBOX_IMAGE"] == "team-runtime:latest"
    assert env["OPENSHELL_SANDBOX_POLICY_FILE"] == "/policies/team.yaml"
    assert env["OPENSHELL_WORKSPACE_MOUNT"] == "/workspace"
    assert env["OPENSHELL_INSTANCE_LABEL"] == "dockertest"
    assert env["OPENSHELL_CREATE_IF_MISSING"] == "true"


def test_cleanup_deletes_the_instances_per_person_sandboxes_when_configured(monkeypatch) -> None:
    from docker.launch_headless import ManagedOpenShellSandbox

    calls: list[list[str]] = []

    def fake_run(args, *, env, timeout_seconds):
        calls.append(list(args))
        return type("Completed", (), {"returncode": 0, "stdout": ""})()

    monkeypatch.setattr("docker.launch_headless._run_openshell", fake_run)
    monkeypatch.setattr("docker.launch_headless._openshell_command", lambda _env: "openshell")
    monkeypatch.setattr(
        "custom_tools.openshell_sandboxes.existing_sandboxes",
        lambda _openshell, _env: {
            "team-p-alice-0123456789",
            "team-shared",
            "team",  # the one sandbox for everyone that earlier versions created
            "team-staging-shared",  # another instance whose prefix starts the same
            "other-shared",
        },
    )
    env = {"OPENSHELL_SANDBOX_NAME": "team"}

    cleanup_openshell_sandbox(ManagedOpenShellSandbox(prefix="team", delete_on_shutdown=False), env=env)
    cleanup_openshell_sandbox(ManagedOpenShellSandbox(prefix="team", delete_on_shutdown=True), env=env)

    assert calls == [
        ["sandbox", "delete", "team"],
        ["sandbox", "delete", "team-p-alice-0123456789"],
        ["sandbox", "delete", "team-shared"],
    ]


def test_run_supervised_starts_secure_tool_broker_with_other_children(
    monkeypatch,
) -> None:
    started: list[list[str]] = []

    class FakeProcess:
        def __init__(self, command: list[str]):
            self.command = command
            self.pid = len(started)
            self.terminated = False

        def poll(self):
            if self.terminated:
                return 0
            if self.command == ["manager"]:
                return 0
            return None

        def terminate(self) -> None:
            self.terminated = True

        def kill(self) -> None:
            self.terminated = True

        def wait(self, timeout=None):
            return 0

    def fake_popen(command, **_kwargs):
        started.append(list(command))
        return FakeProcess(list(command))

    monkeypatch.setattr("docker.launch_headless.subprocess.Popen", fake_popen)
    monkeypatch.setattr("docker.launch_headless.time.sleep", lambda _seconds: None)

    exit_code = run_supervised(
        manager_command=["manager"],
        gateway_command=["gateway"],
        activity_collector_command=["activity"],
        secure_tool_broker_command=["broker"],
        manager_env={},
        gateway_env={},
        activity_collector_env={},
        secure_tool_broker_env={},
    )

    assert exit_code == 0
    assert started == [["broker"], ["manager"], ["activity"], ["gateway"]]


def test_run_supervised_starts_startup_agents_with_their_own_env(monkeypatch) -> None:
    started: list[tuple[list[str], str | None]] = []

    class FakeProcess:
        def __init__(self, command: list[str]):
            self.command = command
            self.pid = len(started)
            self.terminated = False

        def poll(self):
            if self.terminated:
                return 0
            if self.command == ["manager"]:
                return 0
            return None

        def terminate(self) -> None:
            self.terminated = True

        def kill(self) -> None:
            self.terminated = True

        def wait(self, timeout=None):
            return 0

    def fake_popen(command, **kwargs):
        started.append((list(command), dict(kwargs.get("env") or {}).get("ROLE")))
        return FakeProcess(list(command))

    monkeypatch.setattr("docker.launch_headless.subprocess.Popen", fake_popen)
    monkeypatch.setattr("docker.launch_headless.time.sleep", lambda _seconds: None)

    exit_code = run_supervised(
        manager_command=["manager"],
        startup_agent_commands=(
            ("WebResearchAgent", ["web-agent"]),
            ("CodeWritingAgent", ["code-agent"]),
        ),
        gateway_command=["gateway"],
        activity_collector_command=["activity"],
        manager_env={"ROLE": "manager"},
        gateway_env={"ROLE": "gateway"},
        activity_collector_env={"ROLE": "activity"},
        startup_agent_envs={
            "WebResearchAgent": {"ROLE": "agent"},
            "CodeWritingAgent": {"ROLE": "agent"},
        },
    )

    assert exit_code == 0
    assert started == [
        (["manager"], "manager"),
        (["web-agent"], "agent"),
        (["code-agent"], "agent"),
        (["activity"], "activity"),
        (["gateway"], "gateway"),
    ]


def test_run_supervised_starts_and_monitors_adapter_host_with_scoped_env(
    monkeypatch,
) -> None:
    started: list[tuple[list[str], str | None]] = []

    class FakeProcess:
        def __init__(self, command: list[str]):
            self.command = command
            self.pid = len(started)
            self.terminated = False

        def poll(self):
            if self.terminated:
                return 0
            if self.command == ["manager"]:
                return 0
            return None

        def terminate(self) -> None:
            self.terminated = True

        def kill(self) -> None:
            self.terminated = True

        def wait(self, timeout=None):
            return 0

    def fake_popen(command, **kwargs):
        started.append((list(command), dict(kwargs.get("env") or {}).get("ROLE")))
        return FakeProcess(list(command))

    monkeypatch.setattr("docker.launch_headless.subprocess.Popen", fake_popen)
    monkeypatch.setattr("docker.launch_headless.time.sleep", lambda _seconds: None)

    exit_code = run_supervised(
        manager_command=["manager"],
        gateway_command=["gateway"],
        activity_collector_command=["activity"],
        adapter_host_command=["adapter-host"],
        manager_env={"ROLE": "manager"},
        gateway_env={"ROLE": "gateway"},
        activity_collector_env={"ROLE": "activity"},
        adapter_host_env={"ROLE": "adapter-host"},
    )

    assert exit_code == 0
    assert started == [
        (["manager"], "manager"),
        (["activity"], "activity"),
        (["gateway"], "gateway"),
        (["adapter-host"], "adapter-host"),
    ]


def test_run_supervised_monitors_external_openshell_gateway_and_cleans_before_shutdown(
    monkeypatch,
) -> None:
    events: list[str] = []

    class FakeProcess:
        def __init__(self, name: str):
            self.name = name
            self.pid = len(events)
            self.terminated = False

        def poll(self):
            if self.terminated:
                return 0
            if self.name == "openshell":
                return 7
            return None

        def terminate(self) -> None:
            events.append(f"terminate:{self.name}")
            self.terminated = True

        def kill(self) -> None:
            self.terminated = True

        def wait(self, timeout=None):
            return 0

    def fake_popen(command, **_kwargs):
        return FakeProcess(command[0])

    openshell_process = FakeProcess("openshell")

    monkeypatch.setattr("docker.launch_headless.subprocess.Popen", fake_popen)
    monkeypatch.setattr("docker.launch_headless.time.sleep", lambda _seconds: None)

    exit_code = run_supervised(
        manager_command=["manager"],
        gateway_command=["gateway"],
        activity_collector_command=["activity"],
        external_processes=(("OpenShell gateway", openshell_process),),
        before_external_shutdown=lambda: events.append("cleanup:sandbox"),
        manager_env={},
        gateway_env={},
        activity_collector_env={},
    )

    assert exit_code == 7
    assert "cleanup:sandbox" in events


def test_run_supervised_cleans_sandbox_before_stopping_running_external_gateway(
    monkeypatch,
) -> None:
    events: list[str] = []

    class FakeProcess:
        def __init__(self, name: str):
            self.name = name
            self.pid = len(events)
            self.terminated = False

        def poll(self):
            if self.terminated:
                return 0
            if self.name == "manager":
                return 0
            return None

        def terminate(self) -> None:
            events.append(f"terminate:{self.name}")
            self.terminated = True

        def kill(self) -> None:
            self.terminated = True

        def wait(self, timeout=None):
            return 0

    def fake_popen(command, **_kwargs):
        return FakeProcess(command[0])

    openshell_process = FakeProcess("openshell")

    monkeypatch.setattr("docker.launch_headless.subprocess.Popen", fake_popen)
    monkeypatch.setattr("docker.launch_headless.time.sleep", lambda _seconds: None)

    exit_code = run_supervised(
        manager_command=["manager"],
        gateway_command=["gateway"],
        activity_collector_command=["activity"],
        external_processes=(("OpenShell gateway", openshell_process),),
        before_external_shutdown=lambda: events.append("cleanup:sandbox"),
        manager_env={},
        gateway_env={},
        activity_collector_env={},
    )

    assert exit_code == 0
    assert events[-2:] == ["cleanup:sandbox", "terminate:openshell"]


def test_launch_refuses_without_task_context_keys_and_names_the_command(tmp_path: Path) -> None:
    from docker.launch_headless import missing_task_context_keys, task_context_keys_message

    config = _make_config(tmp_path)
    assert missing_task_context_keys({"ACCESS_GATEWAY_SIGNING_KEY": "k", "ACCESS_SCHEDULER_SIGNING_KEY": "k"}) == []
    missing = missing_task_context_keys({"ACCESS_GATEWAY_SIGNING_KEY": "k"})
    assert missing == ["ACCESS_SCHEDULER_SIGNING_KEY"]

    message = task_context_keys_message(config, tmp_path / "team.env", missing)

    assert "task_context_keys_missing" in message
    assert f"--env-file {tmp_path / 'team.env'} --write" in message
    assert "python3 -m scripts.generate_internal_credentials" in message
    assert "ACCESS_GATEWAY_SIGNING_KEY_FILE" in message, "Docker secrets are offered too"


def test_processes_started_by_hand_derive_their_own_broker_token(monkeypatch) -> None:
    from access_control.caller_tokens import derive_caller_token, verify_caller_token
    from assistant_cli import _use_gateway_broker_token

    monkeypatch.setenv("SECURE_TOOL_BROKER_TOKEN", "root-secret")
    _use_gateway_broker_token()
    caller = verify_caller_token("root-secret", os.environ["SECURE_TOOL_BROKER_TOKEN"])
    assert caller is not None and caller.is_gateway

    manager_token = derive_caller_token("root-secret", "manager")
    monkeypatch.setenv("SECURE_TOOL_BROKER_TOKEN", manager_token)
    _use_gateway_broker_token()
    assert os.environ["SECURE_TOOL_BROKER_TOKEN"] == manager_token, "a supervisor-given token is kept"


def test_signing_keys_from_docker_secret_files_reach_only_their_process(tmp_path: Path, caplog) -> None:
    from access_control.identity import ContextVerifier, generate_signing_key
    from docker.launch_headless import (
        BROKER_TOKEN_ENV,
        with_verify_keys,
    )
    from env_secrets import materialize_secret_file_envs

    gateway_value, _ = generate_signing_key("gateway")
    scheduler_value, _ = generate_signing_key("scheduler")
    (tmp_path / "gateway_key").write_text(gateway_value + "\n", encoding="utf-8")
    (tmp_path / "scheduler_key").write_text(scheduler_value, encoding="utf-8")
    (tmp_path / "broker_root").write_text("root-secret", encoding="utf-8")
    env = {
        "ACCESS_GATEWAY_SIGNING_KEY_FILE": str(tmp_path / "gateway_key"),
        "ACCESS_SCHEDULER_SIGNING_KEY_FILE": str(tmp_path / "scheduler_key"),
        "ACCESS_CONTEXT_VERIFY_KEYS": "[]",
        f"{BROKER_TOKEN_ENV}_FILE": str(tmp_path / "broker_root"),
    }
    materialize_secret_file_envs(env)
    assert env["ACCESS_GATEWAY_SIGNING_KEY"] == gateway_value

    with caplog.at_level("WARNING"):
        env = with_verify_keys(env)
    assert "task_context_verify_keys_mismatch" in caplog.text
    assert set(ContextVerifier.from_value(env["ACCESS_CONTEXT_VERIFY_KEYS"]).keys) == {
        gateway_value.split(":")[0],
        scheduler_value.split(":")[0],
    }

    manager = scoped_environment("manager", env)
    assert "ACCESS_GATEWAY_SIGNING_KEY" not in manager and "ACCESS_GATEWAY_SIGNING_KEY_FILE" not in manager
    assert manager["ACCESS_SCHEDULER_SIGNING_KEY"] == scheduler_value
    gateway = scoped_environment("gateway", env)
    assert gateway["ACCESS_GATEWAY_SIGNING_KEY"] == gateway_value
    assert "ACCESS_SCHEDULER_SIGNING_KEY_FILE" not in gateway
    worker = scoped_environment("agent", env, broker_root="root-secret", caller_label="agent/Coder")
    assert not {key for key in worker if key.startswith(("ACCESS_GATEWAY", "ACCESS_SCHEDULER"))}
    assert f"{BROKER_TOKEN_ENV}_FILE" not in worker and worker[BROKER_TOKEN_ENV] != "root-secret"
    assert worker["ACCESS_CONTEXT_VERIFY_KEYS"] == env["ACCESS_CONTEXT_VERIFY_KEYS"]


def _secret_env() -> dict[str, str]:
    """One value for each kind of secret, plus Docker secret file companions."""

    return {
        "PATH": "/usr/bin",
        "NATS_PASSWORD": "nats",
        "OPENAI_API_KEY": "model",
        "EMBEDDING_API_KEY": "embedding",
        "XAI_API_KEY": "voice",
        "WEB_CHAT_AUTH_TOKEN": "web-chat",
        "WEB_CHAT_AUTH_TOKEN_FILE": "/run/secrets/web_chat",
        "TELEGRAM_BOT_TOKEN": "telegram",
        "PHOTON_PROJECT_SECRET": "photon",
        "ADAPTER_HOST_AUTH_TOKEN": "adapter",
        "ACTIVITY_COLLECTOR_ADMIN_TOKEN": "activity-admin",
        "ACTIVITY_COLLECTOR_INGEST_TOKEN": "activity-ingest",
        "BW_SESSION_FILE": "/run/secrets/gh",
        "VAULT_TOKEN": "vault",
        "ACCESS_GATEWAY_SIGNING_KEY": "gateway-key",
        "ACCESS_SCHEDULER_SIGNING_KEY": "scheduler-key",
        "ACCESS_SCHEDULER_SIGNING_KEY_FILE": "/run/secrets/scheduler",
        "OPENSHELL_SSH_HANDSHAKE_SECRET": "handshake",
        "SECURE_TOOL_BROKER_TOKEN": "root",
    }


def test_manager_and_agents_hold_no_gateway_channel_or_broker_tool_secret(tmp_path: Path) -> None:
    from docker.launch_headless import build_startup_agent_environments

    env = _secret_env()
    manager = scoped_environment("manager", env, broker_root="root", caller_label="manager")
    agents = build_startup_agent_environments((("Coder", ["run"]),), base_env=env, broker_root="root")

    for process_env in (manager, agents["Coder"]):
        assert not {
            "WEB_CHAT_AUTH_TOKEN",
            "WEB_CHAT_AUTH_TOKEN_FILE",
            "TELEGRAM_BOT_TOKEN",
            "PHOTON_PROJECT_SECRET",
            "ADAPTER_HOST_AUTH_TOKEN",
            "ACTIVITY_COLLECTOR_ADMIN_TOKEN",
            "BW_SESSION_FILE",
            "VAULT_TOKEN",
            "ACCESS_GATEWAY_SIGNING_KEY",
            "OPENSHELL_SSH_HANDSHAKE_SECRET",
        } & set(process_env)
        assert process_env["OPENAI_API_KEY"] == "model"
        assert process_env["ACTIVITY_COLLECTOR_INGEST_TOKEN"] == "activity-ingest"
        assert process_env["SECURE_TOOL_BROKER_TOKEN"] != "root"
    assert manager["ACCESS_SCHEDULER_SIGNING_KEY"] == "scheduler-key"
    assert "ACCESS_SCHEDULER_SIGNING_KEY" not in agents["Coder"]
    assert "ACCESS_SCHEDULER_SIGNING_KEY_FILE" not in agents["Coder"]


def test_gateway_and_services_get_only_their_own_secrets(tmp_path: Path) -> None:
    environments = build_service_process_environments(
        runtime_env=_secret_env(),
        inherited_env=_secret_env(),
        manifest_paths_by_service={
            service: tmp_path / f"{service}.json"
            for service in ("gateway", "activity_collector", "secure_broker", "execution_controller", "adapter_host")
        },
    )

    gateway = environments.gateway
    assert gateway["WEB_CHAT_AUTH_TOKEN"] == "web-chat" and gateway["TELEGRAM_BOT_TOKEN"] == "telegram"
    assert gateway["ACCESS_GATEWAY_SIGNING_KEY"] == "gateway-key" and gateway["XAI_API_KEY"] == "voice"
    assert not {"BW_SESSION_FILE", "VAULT_TOKEN", "OPENAI_API_KEY", "EMBEDDING_API_KEY", "ACCESS_SCHEDULER_SIGNING_KEY"} & set(gateway)
    adapter_host = environments.adapter_host
    assert adapter_host["TELEGRAM_BOT_TOKEN"] == "telegram" and adapter_host["ADAPTER_HOST_AUTH_TOKEN"] == "adapter"
    assert not {"WEB_CHAT_AUTH_TOKEN", "ACCESS_GATEWAY_SIGNING_KEY", "OPENAI_API_KEY"} & set(adapter_host)
    broker = environments.secure_broker
    assert broker["VAULT_TOKEN"] == "vault" and broker["SECURE_TOOL_BROKER_TOKEN"] == "root"
    assert not {"WEB_CHAT_AUTH_TOKEN", "TELEGRAM_BOT_TOKEN", "OPENAI_API_KEY", "ACCESS_GATEWAY_SIGNING_KEY"} & set(broker)
    assert set(environments.execution_controller) - {"UA_RUNTIME_MANIFEST"} == {"PATH", "NATS_PASSWORD"}
    collector = environments.activity_collector
    assert collector["ACTIVITY_COLLECTOR_ADMIN_TOKEN"] == "activity-admin"
    assert "WEB_CHAT_AUTH_TOKEN" not in collector


def test_a_configured_secret_name_reaches_the_same_processes_as_the_default(tmp_path: Path) -> None:
    from docker.launch_headless import configured_secret_names

    config = _make_config(tmp_path)
    config.interactive_apps.internal_website.web_chat_token_env_var = "TEAM_WEB_CHAT_TOKEN"
    renamed = configured_secret_names(config)
    env = {"TEAM_WEB_CHAT_TOKEN": "web", "TEAM_WEB_CHAT_TOKEN_FILE": "/run/secrets/web"}

    assert renamed == {"TEAM_WEB_CHAT_TOKEN": "WEB_CHAT_AUTH_TOKEN"}
    assert scoped_environment("gateway", env, renamed=renamed) == env
    assert scoped_environment("manager", env, renamed=renamed) == {}


def test_a_runtime_restart_reuses_the_scoped_environments(monkeypatch) -> None:
    """A restart must not hand a process more secrets than it started with."""

    launched: list[tuple[str, dict[str, str]]] = []
    requests = iter([{"scope": "all", "reason": "test", "request_path": "r"}])

    class FakeProcess:
        pid = 1

        def __init__(self, name: str):
            self.name = name
            self.terminated = False

        def poll(self):
            if self.terminated:
                return 0
            restarted = sum(1 for launched_name, _ in launched if launched_name == "manager") > 1
            return 0 if self.name == "manager" and restarted else None

        def terminate(self) -> None:
            self.terminated = True

        def kill(self) -> None:
            self.terminated = True

        def wait(self, timeout=None):
            return 0

    def fake_popen(command, **kwargs):
        launched.append((command[0], dict(kwargs["env"])))
        return FakeProcess(command[0])

    monkeypatch.setattr("docker.launch_headless.subprocess.Popen", fake_popen)
    monkeypatch.setattr("docker.launch_headless.time.sleep", lambda _seconds: None)
    monkeypatch.setattr(
        "docker.launch_headless.pop_runtime_restart_request", lambda _path: next(requests, None)
    )
    envs = {
        "manager": scoped_environment("manager", _secret_env()),
        "gateway": scoped_environment("gateway", _secret_env()),
        "activity": scoped_environment("activity_collector", _secret_env()),
        "adapter": scoped_environment("adapter_host", _secret_env()),
        "agent": scoped_environment("agent", _secret_env()),
    }

    run_supervised(
        manager_command=["manager"],
        startup_agent_commands=(("Coder", ["agent"]),),
        gateway_command=["gateway"],
        activity_collector_command=["activity"],
        adapter_host_command=["adapter"],
        manager_env=envs["manager"],
        gateway_env=envs["gateway"],
        activity_collector_env=envs["activity"],
        adapter_host_env=envs["adapter"],
        startup_agent_envs={"Coder": envs["agent"]},
    )

    assert [name for name, _ in launched].count("manager") == 2, "the restart ran"
    for name, env in launched:
        assert env == envs[name], name


def test_finish_import_step_runs_only_when_onboarding_left_credentials(tmp_path: Path) -> None:
    from docker.launch_headless import build_finish_import_task

    config = _make_config(tmp_path)
    config_path = tmp_path / "instance" / "instance_config.yaml"
    env = {"SECURE_TOOL_BROKER_TOKEN": "root"}

    assert build_finish_import_task(config, config_path, env) is None
    handover = config.layout.team_import_credentials_path
    handover.parent.mkdir(parents=True)
    handover.write_text("{}")
    name, command, step_env = build_finish_import_task(config, config_path, env)

    assert name == "first-start credential import"
    assert command[-4:] == ["team", "finish-import", "--config", str(config_path)]
    assert step_env is env


def test_imported_keys_exist_when_real_model_processes_initialize(tmp_path: Path) -> None:
    """Exercise child process scheduling, rather than treating Popen as completion."""

    import subprocess

    child_script = tmp_path / "startup_child.py"
    child_script.write_text(
        """import sys
import time
from pathlib import Path

role, folder = sys.argv[1:]
folder = Path(folder)
if role == "finish-import":
    time.sleep(0.2)
    (folder / "stored-key").write_text("restored")
    sys.exit(0)
if role in {"manager", "agent", "gateway"}:
    assert (folder / "stored-key").read_text() == "restored"
    (folder / f"{role}.initialized").touch()
if role == "manager":
    deadline = time.monotonic() + 5
    while not all((folder / f"{name}.initialized").exists() for name in ("agent", "gateway")):
        if time.monotonic() > deadline:
            sys.exit(8)
        time.sleep(0.02)
    sys.exit(0)
time.sleep(30)
""",
        encoding="utf-8",
    )

    def command(role: str) -> list[str]:
        return [sys.executable, str(child_script), role, str(tmp_path)]

    processes: list[subprocess.Popen] = []
    original_popen = subprocess.Popen

    def recorded_popen(*args, **kwargs):
        process = original_popen(*args, **kwargs)
        processes.append(process)
        return process

    # Keep real child processes and record them so shutdown is part of the check.
    from unittest.mock import patch

    with patch("docker.launch_headless.subprocess.Popen", recorded_popen):
        exit_code = run_supervised(
            manager_command=command("manager"),
            startup_agent_commands=(("Coder", command("agent")),),
            gateway_command=command("gateway"),
            activity_collector_command=command("activity"),
            secure_tool_broker_command=command("broker"),
            manager_env=os.environ,
            startup_agent_envs={"Coder": os.environ},
            gateway_env=os.environ,
            activity_collector_env=os.environ,
            secure_tool_broker_env=os.environ,
            first_start_tasks=(("first-start credential import", command("finish-import"), os.environ),),
        )

    assert exit_code == 0
    assert all((tmp_path / f"{role}.initialized").exists() for role in ("manager", "agent", "gateway"))
    assert all(process.poll() is not None for process in processes), "all children stopped"


@pytest.mark.parametrize(
    ("failure", "expected_code"),
    [
        ("step_exit", 7),
        ("step_spawn", 1),
        ("broker_exit", 9),
        ("broker_clean_exit", 1),
        ("external_exit", 11),
        ("sigterm", 0),
        ("sigint", 0),
        ("manager_spawn", None),
        ("agent_spawn", None),
    ],
)
def test_startup_failures_and_signals_clean_up_first_start_services(
    monkeypatch, failure: str, expected_code: int | None, caplog,
) -> None:
    events: list[str] = []
    handlers = {}
    processes = []

    class FakeProcess:
        def __init__(self, name: str):
            self.name = name
            self.pid = len(events)
            self.terminated = False
            processes.append(self)

        def poll(self):
            if self.terminated:
                return 0
            if self.name == "finish-import":
                if failure == "step_exit":
                    return 7
                if failure in {"manager_spawn", "agent_spawn"}:
                    events.append("import:complete")
                    return 0
            if self.name == "broker" and failure == "broker_exit":
                return 9
            if self.name == "broker" and failure == "broker_clean_exit":
                return 0
            if self.name == "external" and failure == "external_exit":
                return 11
            return None

        def terminate(self) -> None:
            events.append(f"terminate:{self.name}")
            self.terminated = True

        def kill(self) -> None:
            self.terminated = True

        def wait(self, timeout=None):
            return 0

    def fake_popen(command, **kwargs):
        name = command[0]
        events.append(f"start:{name}")
        assert kwargs["env"] == {"ROLE": name}, "startup retains the scoped environment"
        if (name, failure) in {
            ("finish-import", "step_spawn"),
            ("manager", "manager_spawn"),
            ("agent", "agent_spawn"),
        }:
            raise OSError("fixture spawn failure")
        return FakeProcess(name)

    def sleep(_seconds):
        assert failure in {"sigterm", "sigint"}, "this case should stop without waiting"
        signum = signal.SIGTERM if failure == "sigterm" else signal.SIGINT
        handlers[signum](signum, None)

    monkeypatch.setattr("docker.launch_headless.subprocess.Popen", fake_popen)
    monkeypatch.setattr("docker.launch_headless.time.sleep", sleep)
    monkeypatch.setattr("docker.launch_headless.signal.signal", handlers.__setitem__)

    arguments = dict(
        manager_command=["manager"],
        startup_agent_commands=(("Coder", ["agent"]),),
        gateway_command=["gateway"],
        activity_collector_command=["activity"],
        secure_tool_broker_command=["broker"],
        external_processes=(("external", FakeProcess("external")),),
        before_external_shutdown=lambda: events.append("cleanup:sandbox"),
        manager_env={"ROLE": "manager"},
        startup_agent_envs={"Coder": {"ROLE": "agent"}},
        gateway_env={"ROLE": "gateway"},
        activity_collector_env={"ROLE": "activity"},
        secure_tool_broker_env={"ROLE": "broker"},
        first_start_tasks=(("first-start credential import", ["finish-import"], {"ROLE": "finish-import"}),),
    )
    if expected_code is None:
        with pytest.raises(OSError, match="fixture spawn failure"):
            run_supervised(**arguments)
        assert events.index("import:complete") < events.index("start:manager")
    else:
        assert run_supervised(**arguments) == expected_code
        assert "start:manager" not in events
    assert "start:gateway" not in events
    assert "cleanup:sandbox" in events
    if failure not in {"broker_exit", "broker_clean_exit"}:
        assert "terminate:broker" in events
    if failure != "external_exit":
        assert events[-2:] == ["cleanup:sandbox", "terminate:external"]
    assert all(process.poll() is not None for process in processes)
    if failure in {"step_exit", "step_spawn"}:
        assert "event=first_start_step_failed" in caplog.text


# --- the manager database is set up before any service opens it -------------


def test_the_manager_database_is_primed_before_each_start(tmp_path: Path) -> None:
    import docker.launch_headless as lh

    config = _make_config(tmp_path)
    path = lh.prepare_manager_database(config)
    assert path == lh.resolve_manager_db_path(config)
    # In WAL mode with its first write done, so no service creates the file.
    assert Path(f"{path}-wal").stat().st_size > 0
    assert lh.prepare_manager_database(config) == path
    assert Path(f"{path}-wal").stat().st_size > 0


def test_a_postgres_manager_store_has_no_file_to_set_up(tmp_path: Path) -> None:
    import docker.launch_headless as lh

    config = _make_config(tmp_path)
    config.state.storage.backend = "postgres"
    assert lh.prepare_manager_database(config) is None
    assert not lh.resolve_manager_db_path(config).exists()


class _StopAfterPreparation(Exception):
    pass


def _launch_until_after_preparation(tmp_path: Path, monkeypatch, prepare) -> tuple[int | None, list[str]]:
    """Run the launcher's main up to the step after the database setup."""

    import docker.launch_headless as lh

    config_path = _make_config(tmp_path).save(tmp_path / "instance" / "instance_config.yaml")
    steps: list[str] = []
    monkeypatch.setattr(lh, "acquire_process_run", lambda _path: steps.append("run lock") or SimpleNamespace())
    monkeypatch.setattr(lh, "prepare_manager_database", lambda config: steps.append("prepare") or prepare(config))

    def next_step(*_args, **_kwargs):
        steps.append("manifests")
        raise _StopAfterPreparation

    monkeypatch.setattr(lh, "write_runtime_manifests", next_step)
    monkeypatch.setattr(lh, "validate_runtime_layout", lambda config: None)
    monkeypatch.setattr(sys, "argv", ["launch_headless.py", "--config", str(config_path)])
    try:
        return lh.main(), steps
    except _StopAfterPreparation:
        return None, steps


def test_launch_sets_up_the_manager_database_right_after_taking_the_run_lock(tmp_path: Path, monkeypatch) -> None:
    code, steps = _launch_until_after_preparation(tmp_path, monkeypatch, lambda config: None)
    assert code is None and steps == ["run lock", "prepare", "manifests"]


def test_launch_refuses_to_start_when_the_manager_database_cannot_be_set_up(
    tmp_path: Path, monkeypatch, caplog
) -> None:
    import sqlite3

    def locked(_config):
        raise sqlite3.OperationalError("database is locked")

    with caplog.at_level("ERROR", logger="docker.launch_headless"):
        code, steps = _launch_until_after_preparation(tmp_path, monkeypatch, locked)
    assert code == 2 and steps == ["run lock", "prepare"]
    assert "event=manager_database_prepare_failed" in caplog.text and "database is locked" in caplog.text
