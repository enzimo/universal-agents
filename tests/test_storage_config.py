from pathlib import Path

import pytest
import yaml

from config_loader import CONFIG_SCHEMA_VERSION, load_config, resolve_agent_type_settings
from storage.config import resolve_storage_settings


def _clear_config_env_overrides(monkeypatch: pytest.MonkeyPatch) -> None:
    for env_name in (
        "TASK_HISTORY_DB_PATH",
        "PYTHON_ENVS_DIR",
        "STORAGE_BACKEND",
        "SQLITE_DB_PATH",
        "POSTGRES_DSN",
        "POSTGRES_DSN_FILE",
        "LLM_STREAM",
        "LLM_REQUEST_TIMEOUT_SECONDS",
        "AGENT_DEBUG_TERMINAL_REASONING_ENABLED",
        "SECURITY_LAYER_MODE",
        "SECURE_CLI_ENABLED",
        "NETWORK_PUBLIC_ORIGIN",
        "SECURE_TOOL_BROKER_INTERNAL_BASE_URL",
        "SECURE_TOOL_BROKER_PUBLIC_BASE_URL",
        "SECURE_TOOL_BROKER_PUBLIC_PORT",
        "SECURE_TOOL_BROKER_HOST",
        "SECURE_TOOL_BROKER_PORT",
        "SECURE_TOOL_BROKER_CREDENTIALS_DIR",
        "USER_DOCS_BIND_HOST",
        "USER_DOCS_BIND_PORT",
        "USER_DOCS_BASE_URL",
        "USER_DOCS_ROOT",
        "USER_DOCS_SKILLS_DIR",
        "WEBSITE_APP_ENABLED",
        "WEBSITE_APP_BIND_HOST",
        "WEBSITE_APP_BIND_PORT",
        "WEBSITE_APP_BASE_URL",
        "WEBSITE_APP_SITES_DIR",
        "INTERNAL_WEBSITE_ENABLED",
        "INTERNAL_WEBSITE_PORT",
        "INTERNAL_WEBSITE_BIND_HOST",
        "INTERNAL_WEBSITE_BIND_PORT",
        "INTERNAL_WEBSITE_BASE_URL",
        "INTERNAL_WEBSITE_PUBLIC_PORT",
        "INTERNAL_WEBSITE_DOCS_ROOT",
        "INTERNAL_WEBSITE_SKILLS_DIR",
        "INTERNAL_WEBSITE_SITES_DIR",
        "SEALBOX_TOKEN_FILE",
        "SEALBOX_AUTH_TOKEN_FILE",
        "SEALBOX_PUBLIC_KEY_FILE",
        "SEALBOX_PRIVATE_KEY_FILE",
        "SEALBOX_ROOT_TOKEN_FILE",
        "SEALBOX_URL",
        "SECURE_TOOL_BROKER_SEALBOX_TENANT_MATERIAL_DIR",
        "SEALBOX_API_VERSION",
    ):
        monkeypatch.delenv(env_name, raising=False)


def test_load_config_carries_authorization(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _clear_config_env_overrides(monkeypatch)
    config_path = tmp_path / "config.yaml"
    config_path.write_text(
        f"""
schema_version: {CONFIG_SCHEMA_VERSION}
authorization:
  enabled: true
  starter_profile: careful
""".strip()
        + "\n",
        encoding="utf-8",
    )

    cfg = load_config(str(config_path))

    assert cfg.get("authorization.enabled") is True
    assert cfg.get("authorization.starter_profile") == "careful"


def test_config_example_uses_current_token_budget_names_and_no_secret_fields(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from runtime_settings.telemetry import ActivityCollectorSettings
    from runtime_settings.interactive_apps import InteractiveAppsSettings
    from runtime_settings.network import NetworkSettings
    from runtime_settings.voice import VoiceSettings

    example_path = Path(__file__).resolve().parents[1] / "config.example.yaml"
    raw = yaml.safe_load(example_path.read_text(encoding="utf-8"))

    assert raw["agent"]["prompt_skill_description_limit"] == 50
    assert raw["agent"]["context_engine"]["thread_window"]["recent_full_turns"] == 4
    assert raw["agent"]["debug"]["context_dump"]["capture_model_calls"] is False

    seen_keys: set[str] = set()

    def collect_keys(value) -> None:
        if isinstance(value, dict):
            for key, child in value.items():
                seen_keys.add(str(key))
                collect_keys(child)
        elif isinstance(value, list):
            for child in value:
                collect_keys(child)

    collect_keys(raw)

    forbidden_secret_keys = {
        "api_key",
        "openai_api_key",
        "gemini_api_key",
        "litellm_api_key",
        "password",
        "user_token",
        "admin_token",
        "bot_token_env_var",
        "dsn",
    }
    assert seen_keys.isdisjoint(forbidden_secret_keys)

    forbidden_legacy_budget_keys = {
        "max_tool_result_chars_per_call",
        "max_tool_result_chars_per_invocation",
        "max_total_char_len",
        "preserve_head_chars",
        "preserve_tail_chars",
        "trigger_chars",
        "comms_json_trigger_chars",
        "per_turn_summary_max_chars",
        "broker_base_url",
        "broker_public_base_url",
    }
    assert seen_keys.isdisjoint(forbidden_legacy_budget_keys)

    for env_name in (
        "STORAGE_BACKEND",
        "POSTGRES_DSN",
        "POSTGRES_DSN_FILE",
        "NATS_USER",
        "NATS_USER_FILE",
        "NATS_PASSWORD",
        "NATS_PASSWORD_FILE",
        "OPENAI_API_KEY",
        "OPENAI_API_KEY_FILE",
        "GEMINI_API_KEY",
        "GEMINI_API_KEY_FILE",
        "LITELLM_API_KEY",
        "LITELLM_API_KEY_FILE",
        "LLM_STREAM",
        "LLM_REQUEST_TIMEOUT_SECONDS",
        "EMBEDDING_API_KEY",
        "EMBEDDING_API_KEY_FILE",
        "VOICE_ENABLED",
        "NETWORK_PUBLIC_ORIGIN",
        "SECURE_TOOL_BROKER_PUBLIC_PORT",
        "INTERNAL_WEBSITE_ENABLED",
        "INTERNAL_WEBSITE_PORT",
        "INTERNAL_WEBSITE_BIND_HOST",
        "INTERNAL_WEBSITE_BIND_PORT",
        "INTERNAL_WEBSITE_BASE_URL",
        "INTERNAL_WEBSITE_PUBLIC_PORT",
        "INTERNAL_WEBSITE_DOCS_ROOT",
        "INTERNAL_WEBSITE_SKILLS_DIR",
        "INTERNAL_WEBSITE_SITES_DIR",
    ):
        monkeypatch.delenv(env_name, raising=False)

    cfg = load_config(str(example_path))
    settings = resolve_agent_type_settings(cfg, "TeamArchitect")

    assert cfg.get("agent.max_tool_result_tokens_per_call") == 20000
    assert cfg.get("agent.inner_loop_context.max_total_tokens") == 300000
    assert cfg.get("agent.inner_loop_context.filters.html_document.trigger_tokens") == 2000
    assert cfg.get("agent.context_engine.thread_window.per_turn_summary_max_tokens") == 400
    assert cfg.get("agent.debug.context_dump.enabled") is False
    assert cfg.get("agent.debug.context_dump.mode") == "on_error"
    assert cfg.get("agent.debug.context_dump.include_reasoning_artifacts") is True
    assert cfg.get("agent.debug.context_dump.web_chat_controls_enabled") is False
    assert cfg.get("agent.debug.terminal_reasoning.enabled") is False
    assert cfg.get("llm.stream") is None
    assert cfg.get("llm.request_timeout_seconds") == 120
    assert cfg.get("llm.reasoning_trace_retention_messages") == 96
    assert cfg.get("voice.enabled") is raw["voice"]["enabled"]
    assert cfg.get("voice.provider") == "grok"
    assert raw["schema_version"] == CONFIG_SCHEMA_VERSION
    assert raw["agent"]["memory"]["plugin_manager"]["enabled"] is True
    assert raw["agent"]["memory"]["plugins"]["coremem"]["enabled"] is True
    assert raw["network"]["public_origin"] == "http://127.0.0.1"
    assert raw["gateway"]["users"]["web_user_invite_ttl_seconds"] == 900
    assert "user_docs" not in raw
    assert set(raw["interactive_apps"]) == {"adapter_host", "plugins"}
    assert raw["interactive_apps"]["adapter_host"]["enabled"] is False
    assert raw["interactive_apps"]["adapter_host"]["transport"] == "unix"
    assert "broker_public_base_url" not in raw["security_layer"]
    assert cfg.get("network.public_origin") == "http://127.0.0.1"
    assert cfg.get("gateway.users.web_user_invite_ttl_seconds") == 900
    assert cfg.get("security_layer.broker_public_port") == 18600
    website_path = "interactive_apps.plugins.builtin.internal_website.settings"
    assert cfg.get(f"{website_path}.enabled") is True
    assert cfg.get(f"{website_path}.bind_host") == "0.0.0.0"
    assert cfg.get(f"{website_path}.bind_port") == 18090
    assert cfg.get(f"{website_path}.public_port") == 18090
    assert cfg.get(f"{website_path}.docs_root") is None
    assert cfg.get(f"{website_path}.skills_dir") is None
    # Published sites always live in state/published_sites; not configurable.
    assert cfg.get(f"{website_path}.sites_dir") is None
    assert settings["max_tool_result_tokens_per_call"] == 20000
    assert settings["max_output_tokens"] == 55296
    assert settings["stream"] is None
    assert settings["request_timeout_seconds"] == 120
    assert settings["reasoning_trace_retention_messages"] == 96
    assert settings["task_queue_warning_threshold"] == 5
    assert settings["subtask_status_probe_timeout_seconds"] == 5
    assert settings["deferred_message_warning_threshold"] == 10
    assert settings["tool_allowlist"] is None
    assert settings["tool_blocklist"] == []
    assert settings["network_public_origin"] == "http://127.0.0.1"
    assert settings["secure_tool_broker_public_port"] == 18600
    assert settings["internal_website_base_url"] == "http://127.0.0.1:18090"
    assert VoiceSettings.model_validate(raw["voice"]).provider == "grok"
    assert NetworkSettings.model_validate(raw["network"]).public_origin == "http://127.0.0.1"
    assert ActivityCollectorSettings.model_validate(raw["activity_collector"]).bind_port == 8765
    assert InteractiveAppsSettings.model_validate(raw["interactive_apps"]).internal_website.bind_port == 18090


@pytest.mark.parametrize(
    ("field", "value", "match"),
    [
        ("bind_port", 70000, "activity_collector.bind_port"),
        ("connect_timeout_seconds", 0, "activity_collector.connect_timeout_seconds"),
        ("request_timeout_seconds", -1, "activity_collector.request_timeout_seconds"),
        ("flush_interval_seconds", 0, "activity_collector.flush_interval_seconds"),
        ("batch_max_events", 0, "activity_collector.batch_max_events"),
        ("batch_max_events", 1001, "activity_collector.batch_max_events"),
        ("max_buffered_events", 0, "activity_collector.max_buffered_events"),
        ("max_buffered_events", 100001, "activity_collector.max_buffered_events"),
    ],
)
def test_activity_collector_settings_reject_invalid_numeric_values(
    field: str,
    value: object,
    match: str,
) -> None:
    from runtime_settings.telemetry import ActivityCollectorSettings

    with pytest.raises(ValueError, match=match):
        ActivityCollectorSettings.model_validate({field: value})


@pytest.mark.parametrize(
    ("field", "value", "match"),
    [
        ("connect_timeout_seconds", 0, "activity_collector.connect_timeout_seconds"),
        ("request_timeout_seconds", -1, "activity_collector.request_timeout_seconds"),
        ("flush_interval_seconds", 0, "activity_collector.flush_interval_seconds"),
        ("batch_max_events", 0, "activity_collector.batch_max_events"),
        ("batch_max_events", 1001, "activity_collector.batch_max_events"),
        ("max_buffered_events", 0, "activity_collector.max_buffered_events"),
        ("max_buffered_events", 100001, "activity_collector.max_buffered_events"),
    ],
)
def test_generic_config_rejects_invalid_activity_collector_numeric_values(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    field: str,
    value: object,
    match: str,
) -> None:
    _clear_config_env_overrides(monkeypatch)
    config_path = tmp_path / "config.yaml"
    config_path.write_text(
        f"""
schema_version: 1
activity_collector:
  {field}: {value}
""".strip()
        + "\n",
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match=match):
        resolve_agent_type_settings(load_config(str(config_path)), "TeamArchitect")


def test_compose_public_base_url_derives_service_urls_and_rejects_non_origins() -> None:
    from runtime_settings.network import compose_public_base_url

    assert compose_public_base_url("http://127.0.0.1", 18500) == "http://127.0.0.1:18500"
    assert compose_public_base_url("https://example.com", 443) == "https://example.com:443"
    assert compose_public_base_url("https://example.com", None) == "https://example.com"
    assert compose_public_base_url("https://example.com/", None) == "https://example.com"

    for origin in (
        "https://example.com/path",
        "https://example.com?query=1",
        "https://example.com#fragment",
        "https://user@example.com",
        "ftp://example.com",
        "example.com",
    ):
        with pytest.raises(ValueError):
            compose_public_base_url(origin, 18500)


@pytest.mark.parametrize(
    "config_text,match",
    [
        ("schema_version: 1\nuser_docs: {}\n", "user_docs has been removed"),
        (
            "schema_version: 1\ninteractive_apps:\n  website: {}\n",
            "interactive_apps.website has been removed",
        ),
        (
            "schema_version: 1\nsecurity_layer:\n  broker_public_base_url: http://old.example.test\n",
            "broker_public_base_url has been removed",
        ),
    ],
)
def test_current_config_rejects_removed_public_url_paths(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    config_text: str,
    match: str,
) -> None:
    _clear_config_env_overrides(monkeypatch)
    config_path = tmp_path / "config.yaml"
    config_path.write_text(config_text, encoding="utf-8")

    with pytest.raises(ValueError, match=match):
        load_config(str(config_path))


def test_removed_public_url_env_vars_fail_fast(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _clear_config_env_overrides(monkeypatch)
    config_path = tmp_path / "config.yaml"
    config_path.write_text("schema_version: 1\n", encoding="utf-8")
    monkeypatch.setenv("WEBSITE_APP_BASE_URL", "http://old.example.test")

    with pytest.raises(ValueError, match="Removed environment variable"):
        load_config(str(config_path))


def test_resolve_storage_settings_requires_postgres_dsn(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="storage.postgres.dsn is required"):
        resolve_storage_settings(
            {"backend": "postgres"},
            base_dir=str(tmp_path),
        )


def test_resolve_storage_settings_marks_postgres_runtime_supported(tmp_path: Path) -> None:
    settings = resolve_storage_settings(
        {"backend": "postgres", "postgres": {"dsn": "postgresql://team:secret@db/team_agents"}},
        base_dir=str(tmp_path),
    )

    assert settings.backend == "postgres"
    assert settings.postgres_dsn == "postgresql://team:secret@db/team_agents"
    assert settings.current_runtime_supported is True


def test_resolve_agent_type_settings_exposes_postgres_storage_config(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _clear_config_env_overrides(monkeypatch)
    workspace_dir = tmp_path / "workspace"
    config_path = tmp_path / "config.yaml"
    config_path.write_text(
        f"""
schema_version: 1
agent:
  workspace_dir: "{workspace_dir}"
storage:
  backend: "postgres"
  postgres:
    dsn: "postgresql+psycopg://team:secret@db:5432/team_agents"
""".strip()
        + "\n",
        encoding="utf-8",
    )

    cfg = load_config(str(config_path))
    settings = resolve_agent_type_settings(cfg, "TeamArchitect")

    assert settings["storage_backend"] == "postgres"
    assert settings["storage_postgres_dsn"] == "postgresql+psycopg://team:secret@db:5432/team_agents"
    assert settings["storage_sqlite_path"] is None
    assert settings["storage_sqlite_vector_enabled"] is True
    assert settings["storage_sqlite_vector_provider"] == "sqlite_vec"
    assert settings["storage_sqlite_vector_require_extension"] is True


def test_legacy_python_envs_dir_is_dropped_at_default_and_refused_when_customized(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Python environments now live in each person's folder (workspaces/users/<person>).

    Upgrading a config.yaml drops workspace.python_envs_dir while it holds its
    old default and refuses a custom value, whose files must be moved by hand.
    """

    _clear_config_env_overrides(monkeypatch)
    instance_root = tmp_path / "instance"
    config_path = tmp_path / "config.yaml"

    def write(python_envs_dir: str) -> None:
        config_path.write_text(
            f"""
schema_version: 1
agent:
  workspace_dir: "{instance_root}"
workspace:
  python_envs_dir: "{python_envs_dir}"
""".strip()
            + "\n",
            encoding="utf-8",
        )

    write("python_envs")
    cfg = load_config(str(config_path))
    settings = resolve_agent_type_settings(cfg, "TeamArchitect")
    assert cfg.get("workspace.python_envs_dir") is None
    assert "python_envs_dir" not in settings
    assert settings["instance_root"] == str(instance_root.resolve())

    write("runtime/python_envs")
    with pytest.raises(ValueError, match=r"workspace\.python_envs_dir=runtime/python_envs"):
        load_config(str(config_path))


def test_broker_security_layer_enables_secure_cli_by_mode(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _clear_config_env_overrides(monkeypatch)
    workspace_dir = tmp_path / "workspace"
    config_path = tmp_path / "config.yaml"
    config_path.write_text(
        f"""
schema_version: 1
agent:
  workspace_dir: "{workspace_dir}"
network:
  public_origin: "http://broker.public"
security_layer:
  mode: "broker"
  broker_internal_base_url: "http://broker.internal"
  broker_bind_host: "0.0.0.0"
  broker_bind_port: "8767"
  broker_credentials_dir: "broker_credentials"
""".strip()
        + "\n",
        encoding="utf-8",
    )

    settings = resolve_agent_type_settings(load_config(str(config_path)), "TeamArchitect")

    assert settings["security_layer_mode"] == "broker"
    assert settings["secure_tool_broker_internal_base_url"] == "http://broker.internal"
    assert settings["secure_tool_broker_public_base_url"] == "http://broker.public:18600"
    assert settings["secure_tool_broker_bind_host"] == "0.0.0.0"
    assert settings["secure_tool_broker_bind_port"] == 8767
    assert settings["secure_tool_broker_credentials_dir"] == "broker_credentials"


@pytest.mark.parametrize("removed_key", ["legacy_public_key_file", "legacy_private_key_file"])
def test_removed_single_tenant_sealbox_key_files_are_refused_by_name(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    removed_key: str,
) -> None:
    _clear_config_env_overrides(monkeypatch)
    config_path = tmp_path / "config.yaml"
    config_path.write_text(
        f"""
schema_version: 1
agent:
  workspace_dir: "{tmp_path / 'workspace'}"
security_layer:
  mode: "broker"
  sealbox:
    root_token_file: "mounted/sealbox-token"
    {removed_key}: "mounted/sealbox-key.pem"
""".strip()
        + "\n",
        encoding="utf-8",
    )

    # Refused where the Sealbox settings are read, like the older removed
    # single-tenant fields (runtime_settings.security.REMOVED_SEALBOX_KEYS).
    with pytest.raises(ValueError, match=f"security_layer.sealbox.{removed_key} has been removed"):
        resolve_agent_type_settings(load_config(str(config_path)), "TeamArchitect")


@pytest.mark.parametrize("removed_key", ["path", "legacy_owner_canonical_user_id"])
def test_removed_catalog_import_settings_are_refused_by_name(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    removed_key: str,
) -> None:
    _clear_config_env_overrides(monkeypatch)
    config_path = tmp_path / "config.yaml"
    config_path.write_text(
        f"""
schema_version: 1
security_layer:
  mode: "broker"
  credential_catalog:
    database_path: "credential_catalog.db"
    {removed_key}: "old-value"
""".strip()
        + "\n",
        encoding="utf-8",
    )

    # The one-time credential_bindings.yaml import is gone
    # (runtime_settings.security.REMOVED_CREDENTIAL_CATALOG_KEYS).
    with pytest.raises(
        ValueError, match=f"security_layer.credential_catalog.{removed_key} has been removed"
    ):
        resolve_agent_type_settings(load_config(str(config_path)), "TeamArchitect")


@pytest.mark.parametrize(
    "variable",
    ["SECURE_TOOL_BROKER_CREDENTIAL_CATALOG_PATH", "SECURE_TOOL_BROKER_LEGACY_OWNER_CANONICAL_USER_ID"],
)
def test_removed_catalog_import_env_vars_are_refused(variable: str) -> None:
    from runtime_settings.security import reject_removed_credential_catalog_env_vars

    reject_removed_credential_catalog_env_vars({"PATH": "/usr/bin"})
    with pytest.raises(ValueError, match=rf"Removed environment variable\(s\) are set: {variable}"):
        reject_removed_credential_catalog_env_vars({variable: "old-value"})


@pytest.mark.parametrize(
    "variable",
    ["SECURE_TOOL_BROKER_SEALBOX_LEGACY_PUBLIC_KEY_FILE", "SECURE_TOOL_BROKER_SEALBOX_LEGACY_PRIVATE_KEY_FILE"],
)
def test_removed_single_tenant_sealbox_env_vars_are_refused(variable: str) -> None:
    from runtime_settings.security import reject_removed_sealbox_env_vars

    reject_removed_sealbox_env_vars({"PATH": "/usr/bin"})
    with pytest.raises(ValueError, match=rf"Removed environment variable\(s\) are set: {variable}"):
        reject_removed_sealbox_env_vars({variable: "/keys/sealbox.pem"})


def test_broker_security_layer_accepts_multi_tenant_sealbox_env_overrides(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _clear_config_env_overrides(monkeypatch)
    monkeypatch.setenv("SEALBOX_ROOT_TOKEN_FILE", "env/sealbox-root-token")
    monkeypatch.setenv("SEALBOX_URL", "https://sealbox.example.test")
    monkeypatch.setenv("SECURE_TOOL_BROKER_SEALBOX_TENANT_MATERIAL_DIR", "env/sealbox-tenants")
    workspace_dir = tmp_path / "workspace"
    config_path = tmp_path / "config.yaml"
    config_path.write_text(
        f"""
schema_version: 1
agent:
  workspace_dir: "{workspace_dir}"
security_layer:
  mode: "broker"
  sealbox:
    root_token_file: "configured/sealbox-token"
""".strip()
        + "\n",
        encoding="utf-8",
    )

    settings = resolve_agent_type_settings(load_config(str(config_path)), "TeamArchitect")

    assert settings["secure_tool_broker_sealbox"]["root_token_file"] == "env/sealbox-root-token"
    assert settings["secure_tool_broker_sealbox"]["service_url"] == "https://sealbox.example.test"
    assert settings["secure_tool_broker_sealbox"]["tenant_material_dir"] == "env/sealbox-tenants"
    assert "public_key_file" not in settings["secure_tool_broker_sealbox"]
    assert "private_key_file" not in settings["secure_tool_broker_sealbox"]


def test_broker_security_layer_defaults_broker_urls_when_omitted(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _clear_config_env_overrides(monkeypatch)
    workspace_dir = tmp_path / "workspace"
    config_path = tmp_path / "config.yaml"
    config_path.write_text(
        f"""
schema_version: 1
agent:
  workspace_dir: "{workspace_dir}"
security_layer:
  mode: "broker"
""".strip()
        + "\n",
        encoding="utf-8",
    )

    settings = resolve_agent_type_settings(load_config(str(config_path)), "TeamArchitect")

    assert settings["secure_tool_broker_internal_base_url"] == "http://127.0.0.1:18600"
    assert settings["secure_tool_broker_public_base_url"] == "http://127.0.0.1:18600"
    assert settings["secure_tool_broker_bind_host"] == "0.0.0.0"
    assert settings["secure_tool_broker_bind_port"] == 18600
    assert settings["secure_tool_broker_credentials_dir"] == "broker_credentials"


def test_security_layer_rejects_removed_broker_base_url(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _clear_config_env_overrides(monkeypatch)
    workspace_dir = tmp_path / "workspace"
    config_path = tmp_path / "config.yaml"
    config_path.write_text(
        f"""
schema_version: 1
agent:
  workspace_dir: "{workspace_dir}"
security_layer:
  mode: "broker"
  broker_base_url: "http://broker.local"
""".strip()
        + "\n",
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="broker_base_url has been removed"):
        resolve_agent_type_settings(load_config(str(config_path)), "TeamArchitect")


def test_security_layer_rejects_invalid_broker_bind_port(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _clear_config_env_overrides(monkeypatch)
    workspace_dir = tmp_path / "workspace"
    config_path = tmp_path / "config.yaml"
    config_path.write_text(
        f"""
schema_version: 1
agent:
  workspace_dir: "{workspace_dir}"
security_layer:
  mode: "broker"
  broker_bind_port: 70000
""".strip()
        + "\n",
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="security_layer.broker_bind_port"):
        resolve_agent_type_settings(load_config(str(config_path)), "TeamArchitect")


def test_load_config_applies_storage_env_overrides(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    workspace_dir = tmp_path / "workspace"
    config_path = tmp_path / "config.yaml"
    config_path.write_text(
        f"""
schema_version: 1
agent:
  workspace_dir: "{workspace_dir}"
storage:
  backend: "sqlite"
  sqlite:
    path: "runtime/from-config.db"
""".strip()
        + "\n",
        encoding="utf-8",
    )

    monkeypatch.setenv("STORAGE_BACKEND", "postgres")
    monkeypatch.setenv("POSTGRES_DSN", "postgresql://team:secret@db/team_agents")

    cfg = load_config(str(config_path))
    settings = resolve_agent_type_settings(cfg, "TeamArchitect")

    assert settings["storage_backend"] == "postgres"
    assert settings["storage_postgres_dsn"] == "postgresql://team:secret@db/team_agents"


def test_load_config_applies_nats_user_file_override(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    config_path = tmp_path / "config.yaml"
    config_path.write_text("schema_version: 1\ntransport: {}\nagent: {}\nworkspace: {}\n", encoding="utf-8")
    user_secret_path = tmp_path / "nats.user"
    user_secret_path.write_text("file-user\n", encoding="utf-8")

    monkeypatch.delenv("NATS_USER", raising=False)
    monkeypatch.setenv("NATS_USER_FILE", str(user_secret_path))

    cfg = load_config(str(config_path))
    settings = resolve_agent_type_settings(cfg, "TeamArchitect")

    assert settings["nats_user"] == "file-user"


def test_load_config_applies_postgres_dsn_file_override(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    workspace_dir = tmp_path / "workspace"
    config_path = tmp_path / "config.yaml"
    config_path.write_text(
        f"""
schema_version: 1
agent:
  workspace_dir: "{workspace_dir}"
storage:
  backend: "postgres"
  postgres:
    dsn: "postgresql://team:secret@db/from_config"
""".strip()
        + "\n",
        encoding="utf-8",
    )
    secret_path = tmp_path / "postgres.dsn"
    secret_path.write_text("postgresql://team:secret@db/from_secret_file\n", encoding="utf-8")

    monkeypatch.delenv("POSTGRES_DSN", raising=False)
    monkeypatch.setenv("POSTGRES_DSN_FILE", str(secret_path))

    cfg = load_config(str(config_path))
    settings = resolve_agent_type_settings(cfg, "TeamArchitect")

    assert settings["storage_backend"] == "postgres"
    assert settings["storage_postgres_dsn"] == "postgresql://team:secret@db/from_secret_file"


def test_resolve_agent_type_settings_exposes_embedding_service_config(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    workspace_dir = tmp_path / "workspace"
    config_path = tmp_path / "config.yaml"
    config_path.write_text(
        f"""
schema_version: 1
agent:
  workspace_dir: "{workspace_dir}"
embedding:
  provider: "openai_compatible"
  model_id: "text-embedding-3-large"
  api_base: "https://embeddings.example.com/v1"
  api_key: "config-secret"
  dimensions: 256
  timeout_seconds: 9
""".strip()
        + "\n",
        encoding="utf-8",
    )

    monkeypatch.setenv("EMBEDDING_MODEL_ID", "text-embedding-3-small")

    cfg = load_config(str(config_path))
    settings = resolve_agent_type_settings(cfg, "TeamArchitect")

    assert cfg.get("embedding.api_key") is None
    assert settings["embedding_provider"] == "openai_compatible"
    assert settings["embedding_model_id"] == "text-embedding-3-small"
    assert settings["embedding_api_base"] == "https://embeddings.example.com/v1"
    # The key is not a setting: custom_agents.model_keys looks it up per request.
    assert "embedding_api_key" not in settings
    assert settings["embedding_dimensions"] == 256
    assert settings["embedding_timeout_seconds"] == 9


def test_load_config_ignores_yaml_api_key_fields(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    config_path = tmp_path / "config.yaml"
    config_path.write_text(
        """
schema_version: 1
llm:
  provider: "openai"
  openai_api_key: "yaml-openai-secret"
  gemini_api_key: "yaml-gemini-secret"
  litellm_api_key: "yaml-litellm-secret"
embedding:
  provider: "openai_compatible"
  model_id: "pplx-embed-v1-0.6b"
  api_base: "https://litellm.example.com/v1"
  api_key: "yaml-embedding-secret"
agent_types:
  WebResearchAgent:
    llm:
      openai_api_key: "yaml-agent-openai-secret"
    embedding:
      api_key: "yaml-agent-embedding-secret"
""".strip()
        + "\n",
        encoding="utf-8",
    )

    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    monkeypatch.delenv("LITELLM_API_KEY", raising=False)
    monkeypatch.delenv("EMBEDDING_API_KEY", raising=False)

    cfg = load_config(str(config_path))
    manager_settings = resolve_agent_type_settings(cfg, "TeamArchitect")
    web_settings = resolve_agent_type_settings(cfg, "WebResearchAgent")

    assert cfg.get("llm.openai_api_key") is None
    assert cfg.get("llm.gemini_api_key") is None
    assert cfg.get("llm.litellm_api_key") is None
    assert cfg.get("embedding.api_key") is None
    assert cfg.get("agent_types.WebResearchAgent.llm.openai_api_key") is None
    assert cfg.get("agent_types.WebResearchAgent.embedding.api_key") is None
    for key_field in ("openai_api_key", "gemini_api_key", "litellm_api_key", "embedding_api_key"):
        assert key_field not in manager_settings
        assert key_field not in web_settings
