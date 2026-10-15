import json
import os
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from assistant_gateway.config import GatewayInstanceConfig
from assistant_gateway.runtime_config import build_runtime_config_from_instance_config
from config_loader import get_config, set_config
from custom_agents.runtime_configuration_context import build_runtime_configuration_context


@pytest.fixture(autouse=True)
def restore_global_config():
    previous = get_config()
    yield
    set_config(previous)


def _fake_agent(**overrides):
    values = {
        "id": "agent_id_test",
        "name": "TeamArchitect",
        "named_agent_type": "TeamArchitect",
        "team_id": "test_team",
        "is_team_lead": True,
        "instance_root": None,
        "workspace_dir": None,
        "skills_dir": None,
        "additional_skill_directories": [],
        "skill_exclusions": [],
        "llm_override_path": None,
        "team_templates_path": None,
        "team_composition_path": None,
        "instance_config_path": None,
        "model_provider": "openai",
        "model_id": "gpt-test",
        "supports_image_input": None,
        "supports_audio_input": None,
        "supports_file_input": None,
        "storage_backend": "sqlite",
        "storage_sqlite_path": None,
        "storage_postgres_dsn": None,
        "activity_collector_base_url": None,
    }
    values.update(overrides)
    return SimpleNamespace(**values)


def test_runtime_configuration_context_rereads_instance_config_on_explicit_call(tmp_path: Path) -> None:
    instance_root = tmp_path / "instance"
    instance_config = GatewayInstanceConfig.create(
        instance_name="test-instance",
        instance_root=str(instance_root),
        team_id="test_team",
        timezone="UTC",
    )
    layout = instance_config.layout
    instance_config.workspace.external_skill_repositories = [
        "https://github.com/example/skills.git"
    ]
    instance_config.workspace.refresh_external_skills_on_start = True
    instance_config.network.public_origin = "http://127.0.0.1"
    instance_config.security_layer.broker_internal_base_url = "http://127.0.0.1:8766"
    instance_config.security_layer.broker_public_port = 8766
    instance_config.security_layer.broker_bind_host = "0.0.0.0"
    instance_config.security_layer.broker_bind_port = 8766
    instance_config.interactive_apps.telegram.enabled = True
    instance_config.llm.provider = "litellm"
    instance_config.llm.model_id = "fireworks/deepseek-v4-flash"
    instance_config.llm.supports_image_input = False
    instance_config.llm.supports_audio_input = False
    instance_config.llm.supports_file_input = True
    instance_config.delivery.default_target.channel_name = "telegram"
    instance_config.delivery.default_target.chat_id = "-100123"
    instance_config.delivery.default_target.thread_id = "42"
    instance_config.delivery.notification_routing.mode = "default_target"
    config_path = instance_config.save(instance_root / "instance_config.yaml")
    set_config(build_runtime_config_from_instance_config(instance_config))

    agent = _fake_agent(
        instance_root=str(layout.root),
        workspace_dir=str(layout.workspaces_dir),
        skills_dir=str(layout.skills_dir),
        instance_config_path=str(config_path),
        model_provider="litellm",
        model_id="fireworks/deepseek-v4-flash",
        supports_image_input=False,
        supports_audio_input=False,
        supports_file_input=True,
    )

    first = build_runtime_configuration_context(agent)
    assert first["live_agent"]["model_provider"] == "litellm"
    assert first["live_agent"]["model_id"] == "fireworks/deepseek-v4-flash"
    assert first["live_agent"]["supports_image_input"] is False
    assert first["live_agent"]["supports_audio_input"] is False
    assert first["live_agent"]["supports_file_input"] is True
    assert first["config_file_snapshot"]["llm"]["supports_image_input"] is False
    assert first["config_file_snapshot"]["llm"]["supports_audio_input"] is False
    assert first["config_file_snapshot"]["llm"]["supports_file_input"] is True
    assert first["live_agent"]["instance_root"] == str(layout.root)
    assert first["live_agent"]["workspaces_dir"] == str(layout.workspaces_dir)
    assert first["live_agent"]["skills_dir"] == str(layout.skills_dir)
    assert first["config_file_snapshot"]["workspace"]["instance_root"] == str(layout.root)
    assert first["config_file_snapshot"]["workspace"]["workspaces_dir"] == str(layout.workspaces_dir)
    assert first["config_file_snapshot"]["workspace"]["skills_dir"] == str(layout.skills_dir)
    assert first["config_file_snapshot"]["workspace"]["external_skill_repositories"] == [
        "https://github.com/example/skills.git"
    ]
    assert first["config_file_snapshot"]["workspace"]["refresh_external_skills_on_start"] is True
    assert first["config_file_snapshot"]["interactive_apps"]["telegram"] is True
    assert first["config_file_snapshot"]["user_communication"]["preferred_channel"] == "telegram"
    assert first["config_file_snapshot"]["user_communication"]["preferred_chat_id"] == "-100123"
    assert first["config_file_snapshot"]["user_communication"]["preferred_thread_id"] == "42"
    assert first["config_file_snapshot"]["user_communication"]["notification_routing_mode"] == "default_target"
    assert first["config_file_snapshot"]["user_communication"]["tool"] == "notify_user"
    # One view of scheduled-worker settings: the launcher's, read from the saved file.
    assert "schedule_workers" not in first["config_file_snapshot"]
    assert first["schedule_workers"]["source"] == str(config_path.resolve())
    assert first["schedule_workers"]["limits"]["eligible_agent_types"] == [
        "WebResearchAgent", "PersonalAdminAgent",
    ]

    edited = GatewayInstanceConfig.load(config_path)
    edited.workspace.external_skill_repositories = [
        "https://github.com/example/other-skills.git"
    ]
    edited.agent.schedule_workers.eligible_agent_types.append("CustomResearcher")
    edited.save(config_path)

    second = build_runtime_configuration_context(agent)
    assert second["live_agent"]["skills_dir"] == str(layout.skills_dir)
    assert second["config_file_snapshot"]["workspace"]["external_skill_repositories"] == [
        "https://github.com/example/other-skills.git"
    ]
    assert second["runtime_config_cache"]["workspace"]["external_skill_repositories"] == [
        "https://github.com/example/skills.git"
    ]
    assert second["schedule_workers"]["limits"]["eligible_agent_types"] == [
        "WebResearchAgent", "PersonalAdminAgent", "CustomResearcher",
    ]
    # The process's startup copy never offers a second, stale list.
    assert "schedule_workers" not in second["runtime_config_cache"]
    assert "needs no restart" in second["runtime_note"]


@pytest.mark.parametrize("include_snapshot", [False, True])
def test_agent_type_models_reads_saved_baseline_without_refreshing_process_config(
    tmp_path: Path, include_snapshot: bool,
) -> None:
    instance = GatewayInstanceConfig.create(
        instance_name="test-instance", instance_root=str(tmp_path / "instance"),
        team_id="test_team", timezone="UTC",
    )
    instance.llm.context_window_tokens = 800_000
    instance.agent_types = {}
    config_path = instance.save()
    set_config(build_runtime_config_from_instance_config(instance))
    agent = _fake_agent(instance_config_path=str(config_path))

    edited = GatewayInstanceConfig.load_saved(config_path)
    edited.llm.context_window_tokens = 1_050_000
    edited.save(config_path)
    context = build_runtime_configuration_context(
        agent, include_config_file_snapshot=include_snapshot,
        agent_types=["WebResearchAgent"], known_agent_types={"WebResearchAgent"},
    )

    assert context["runtime_config_cache"]["llm"]["context_window_tokens"] == 800_000
    assert context["agent_type_models"]["WebResearchAgent"]["context_window_tokens"] == 1_050_000
    assert (context["config_file_snapshot"] is not None) is include_snapshot


def test_agent_type_models_reports_unreadable_saved_config_instead_of_cached_values(tmp_path: Path) -> None:
    agent = _fake_agent(instance_config_path=str(tmp_path / "missing.yaml"))
    context = build_runtime_configuration_context(
        agent, include_config_file_snapshot=False,
        agent_types=["WebResearchAgent"], known_agent_types={"WebResearchAgent"},
    )
    assert "settings_error" in context["agent_type_models"]["WebResearchAgent"]
    assert "model_id" not in context["agent_type_models"]["WebResearchAgent"]


def test_runtime_configuration_context_redacts_secrets_and_paths(tmp_path: Path) -> None:
    set_config(
        {
            "agent": {"instance_root": str(tmp_path)},
            "storage": {
                "backend": "postgres",
                "postgres": {"dsn": "postgres://user:dbpassword@localhost/app"},
            },
            "security_layer": {
                "mode": "broker",
                "broker_internal_base_url": "http://localhost:8766",
                "broker_public_port": 8766,
                "broker_credentials_dir": "/private/credential-vault",
                "credential_catalog": {
                    "enabled": True,
                    "default_write_source": "sealbox",
                    "path": "/private/credential-vault/catalog.yaml",
                },
                "credential_state_backend": {
                    "mode": "credential_source",
                    "source": "sealbox",
                    "namespace": "private-namespace",
                },
                "sealbox": {
                    "enabled": True,
                    "manage_service": True,
                    "auto_initialize": True,
                    "private_key_file": "/private/credential-vault/sealbox/private.pem",
                },
            },
        }
    )
    agent = _fake_agent(
        instance_root=str(tmp_path),
        workspace_dir=str(tmp_path / "workspaces"),
        storage_backend="postgres",
        storage_postgres_dsn="postgres://user:livepassword@localhost/app",
    )

    payload = build_runtime_configuration_context(agent, include_config_file_snapshot=False)
    rendered = json.dumps(payload, sort_keys=True)

    assert "dbpassword" not in rendered
    assert "livepassword" not in rendered
    assert "/private/credential-vault" not in rendered
    assert payload["live_agent"]["storage_postgres_dsn_configured"] is True
    assert payload["runtime_config_cache"]["storage"]["postgres_dsn_configured"] is True
    assert payload["runtime_config_cache"]["security_layer"]["broker_credentials_dir_configured"] is True
    assert payload["runtime_config_cache"]["security_layer"]["credential_catalog_enabled"] is True
    assert payload["runtime_config_cache"]["security_layer"]["credential_catalog_default_write_source"] == "sealbox"
    assert payload["runtime_config_cache"]["security_layer"]["credential_state_backend_mode"] == "credential_source"
    assert payload["runtime_config_cache"]["security_layer"]["credential_state_backend_source"] == "sealbox"
    assert payload["runtime_config_cache"]["security_layer"]["sealbox_enabled"] is True
    assert payload["runtime_config_cache"]["security_layer"]["sealbox_manage_service"] is True
    assert payload["runtime_config_cache"]["security_layer"]["sealbox_auto_initialize"] is True
    assert "private-namespace" not in rendered
    assert "private.pem" not in rendered
