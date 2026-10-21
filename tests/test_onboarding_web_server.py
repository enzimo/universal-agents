from __future__ import annotations

import asyncio
import io
import json
from pathlib import Path
import subprocess
from urllib import error as urllib_error

import pytest
import yaml
from aiohttp.test_utils import TestClient, TestServer

from assistant_gateway.config import GatewayInstanceConfig, INSTANCE_CONFIG_SCHEMA_VERSION
from assistant_gateway.onboarding_web import provider_test
from model_providers import connection_test
from assistant_gateway.onboarding_web import server as onboarding_server
from assistant_gateway.onboarding_web.server import OnboardingWebService, OnboardingWebSettings
from assistant_gateway.store import GatewayStateStore
from runtime_settings.governance import REMOVED_TOOL_POLICY_KEYS


@pytest.mark.parametrize(
    ("version", "expected"),
    [
        ("v20.19.2", False),
        ("v22.22.0", False),
        ("v23.11.1", False),
        ("v24.0.0", False),
        ("v24.4.9", False),
        ("v24.5.0", True),
        ("v25.1.0", True),
        ("v24", False),
        ("invalid", False),
    ],
)
def test_nodejs_available_requires_node_24_5(
    monkeypatch: pytest.MonkeyPatch,
    version: str,
    expected: bool,
) -> None:
    monkeypatch.setattr(onboarding_server.shutil, "which", lambda _name: "/usr/bin/node")
    monkeypatch.setattr(
        onboarding_server.subprocess,
        "run",
        lambda *_args, **_kwargs: subprocess.CompletedProcess(
            args=["/usr/bin/node", "--version"],
            returncode=0,
            stdout=f"{version}\n",
            stderr="",
        ),
    )

    assert onboarding_server.nodejs_available() is expected


def test_nodejs_available_fails_closed_on_probe_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(onboarding_server.shutil, "which", lambda _name: "/usr/bin/node")
    monkeypatch.setattr(
        onboarding_server.subprocess,
        "run",
        lambda *_args, **_kwargs: subprocess.CompletedProcess(
            args=["/usr/bin/node", "--version"],
            returncode=1,
            stdout="",
            stderr="probe failed",
        ),
    )

    assert onboarding_server.nodejs_available() is False


@pytest.mark.parametrize("empty_value", ["", "null", "{}", "[]"])
def test_onboarding_base_config_accepts_empty_external_mcp_block(
    tmp_path: Path,
    empty_value: str,
) -> None:
    base_config = tmp_path / "config.yaml"
    rendered_value = f" {empty_value}" if empty_value else ""
    base_config.write_text(
        "schema_version: 1\n"
        "agent:\n"
        f"  external_mcp_servers:{rendered_value}\n",
        encoding="utf-8",
    )

    service = OnboardingWebService(
        OnboardingWebSettings(
            host="127.0.0.1",
            port=18090,
            instance_root=str(tmp_path / "instance"),
            base_config=str(base_config),
            restart_on_complete=False,
        )
    )

    assert service.template.agent.external_mcp_servers == []


def test_onboarding_browser_url_allows_origin_port_when_public_port_is_empty(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("NETWORK_PUBLIC_ORIGIN", "https://agents.example.test:8443")
    monkeypatch.setenv("INTERNAL_WEBSITE_PUBLIC_PORT", "")
    service = OnboardingWebService(
        OnboardingWebSettings(
            host="0.0.0.0",
            port=18090,
            instance_root=str(tmp_path),
            restart_on_complete=False,
        )
    )

    assert service.onboarding_browser_url() == "https://agents.example.test:8443/"
    bootstrap = service._settings_from_template(instance_root=str(tmp_path))
    assert bootstrap["internal_website_public_port"] is None
    assert bootstrap["internal_website_base_url"] == "https://agents.example.test:8443"
    catalog = bootstrap["interactive_app_catalog"]
    assert [entry["plugin_id"] for entry in catalog] == [
        "builtin.internal_website",
        "builtin.telegram",
        "builtin.discord",
        "builtin.signal",
        "builtin.photon",
        "builtin.whatsapp",
    ]
    telegram = next(
        entry for entry in catalog if entry["plugin_id"] == "builtin.telegram"
    )
    assert telegram["origin"] == "bundled"
    assert telegram["configuration_schema"]["title"] == "TelegramAppSettings"
    assert telegram["secret_reference_names"] == ["TELEGRAM_BOT_TOKEN"]


@pytest.mark.asyncio
async def test_bootstrap_uses_live_public_port_without_changing_runtime_bind_port(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    instance_root = tmp_path / "instance"
    monkeypatch.setenv("NETWORK_PUBLIC_ORIGIN", "http://192.0.2.20")
    monkeypatch.setenv("INTERNAL_WEBSITE_PUBLIC_PORT", "19443")
    service = OnboardingWebService(
        OnboardingWebSettings(
            host="0.0.0.0",
            port=18090,
            instance_root=str(instance_root),
            restart_on_complete=False,
        )
    )

    async with TestClient(TestServer(service.build_app())) as client:
        bootstrap_response = await client.get("/api/bootstrap")
        bootstrap = await bootstrap_response.json()
        complete_response = await client.post(
            "/api/onboarding/complete",
            json={
                "instance_name": "instance_ava",
                "team_id": "team_ava",
                "assistant_name": "Ava",
                "model_endpoint": "https://openrouter.ai/api/v1",
                "model_token": "provider-token",
                "model_id": "deepseek/deepseek-v4-flash",
                "generate_internal_credentials": False,
                "internal_website_bind_port": bootstrap["internal_website_bind_port"],
                "internal_website_public_port": bootstrap["internal_website_public_port"],
            },
        )

    config = GatewayInstanceConfig.load(instance_root / "instance_config.yaml")
    assert bootstrap_response.status == 200
    assert bootstrap["network_public_origin"] == "http://192.0.2.20"
    assert bootstrap["documentation_port"] == 19443
    assert bootstrap["internal_website_bind_port"] == 18090
    assert bootstrap["internal_website_public_port"] == 19443
    assert bootstrap["internal_website_base_url"] == "http://192.0.2.20:19443"
    assert bootstrap["model_provider"] == "OpenRouter"
    assert bootstrap["model_endpoint"] == "https://openrouter.ai/api/v1"
    assert bootstrap["model_id"] == "deepseek/deepseek-v4.1-flash"
    assert complete_response.status == 200
    assert config.interactive_apps.internal_website.bind_port == 18090
    assert config.interactive_apps.internal_website.public_port == 19443


@pytest.mark.asyncio
async def test_onboarding_web_service_validates_team_skills_repositories(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fake_validate_skill_repositories(repositories: list[str]) -> list[dict[str, object]]:
        return [
            {"url": repositories[0], "ok": True, "message": "Repository is reachable."},
            {"url": repositories[1], "ok": False, "message": "Repository could not be reached."},
        ]

    monkeypatch.setattr(
        "assistant_gateway.onboarding_web.server.validate_skill_repositories",
        fake_validate_skill_repositories,
    )
    service = OnboardingWebService(
        OnboardingWebSettings(
            host="127.0.0.1",
            port=18090,
            instance_root=str(tmp_path / "instance"),
            restart_on_complete=False,
        )
    )

    async with TestClient(TestServer(service.build_app())) as client:
        response = await client.post(
            "/api/onboarding/skills-repos/validate",
            json={
                "repositories": [
                    "https://github.com/enzimo/ua-skills",
                    "https://github.com/example/missing-skills",
                ]
            },
        )
        payload = await response.json()

    assert response.status == 400
    assert payload["status"] == "warning"
    assert payload["repositories"][0]["ok"] is True
    assert payload["repositories"][1]["message"] == "Repository could not be reached."


@pytest.mark.asyncio
async def test_onboarding_web_service_writes_runtime_files(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    instance_root = tmp_path / "instance"
    merged_skill_repos: list[tuple[list[str], Path]] = []

    def fake_merge_team_skills_repositories(
        repositories: list[str],
        config: GatewayInstanceConfig,
    ) -> list[dict[str, object]]:
        merged_skill_repos.append((list(repositories), config.layout.skills_dir))
        return [
            {
                "url": repositories[0],
                "ok": True,
                "copied": 1,
                "message": "Merged 1 skill item.",
                "skills_dir": str(config.layout.skills_dir),
            }
        ]

    monkeypatch.setattr(
        "assistant_gateway.onboarding_web.server.merge_team_skills_repositories",
        fake_merge_team_skills_repositories,
    )
    monkeypatch.setenv("NETWORK_PUBLIC_ORIGIN", "http://10.0.0.5")
    service = OnboardingWebService(
        OnboardingWebSettings(
            host="127.0.0.1",
            port=18090,
            instance_root=str(instance_root),
            restart_on_complete=False,
        )
    )

    async with TestClient(TestServer(service.build_app())) as client:
        bootstrap_response = await client.get("/api/bootstrap")
        complete_response = await client.post(
            "/api/onboarding/complete",
            json={
                "instance_name": "instance_ava",
                "team_id": "team_ava",
                "assistant_name": "Ava",
                "user_display_name": "Ada",
                "user_role": "Platform engineer",
                "timezone": "Europe/Paris",
                "network_public_origin": "http://10.0.0.5",
                "documentation_port": 18090,
                "model_provider": "OpenAI Compatible",
                "model_endpoint": "https://openrouter.ai/api/v1",
                "model_token": "provider-token",
                "model_id": "deepseek/deepseek-v4-flash",
                "reasoning_effort": "high",
                "reasoning_summary": None,
                "litellm_api_mode": "chat_completions",
                "context_window_tokens": 123456,
                "stream": False,
                "supports_image_input": True,
                "llm_failover": {
                    "enabled": True,
                    "switch_for": {
                        "min_turns": 5,
                        "min_seconds": 120,
                    },
                    "demotion": {
                        "failure_threshold": 2,
                        "failure_window_seconds": 300,
                        "demotion_seconds": 0,
                    },
                    "notify_user": False,
                    "fallbacks": [
                        {"provider": "openai", "model_id": "gpt-4.1-mini"},
                    ],
                },
                "embedding_enabled": True,
                "embedding_provider": "openai_compatible",
                "embedding_model_id": "text-embedding-3-small",
                "embedding_api_base": "https://embeddings.example.test/v1",
                "embedding_api_key": "embedding-secret",
                "embedding_dimensions": 1536,
                "embedding_timeout_seconds": 12.5,
                "embedding_organization": "org-test",
                "web_chat_auth_token": "uaweb_test_token",
                "appearance": {
                    "light_colors": ["#0000ff", "#ffff00", "#00ff00", "#ff0000"],
                    "light_diffusion": 10,
                    "polygon_opacity_low": 25,
                    "polygon_opacity_high": 65,
                    "polygon_line_color": "#123456",
                    "polygon_line_opacity": 21,
                    "dynamic_background": False,
                },
                "quiet_enabled": True,
                "quiet_start": 21,
                "quiet_end": 7,
                "generate_internal_credentials": True,
                "security_layer": {
                    "mode": "broker",
                    "broker_internal_base_url": "http://127.0.0.1:9876",
                    "broker_public_port": 9876,
                    "broker_bind_host": "0.0.0.0",
                    "broker_bind_port": 9876,
                    "broker_credentials_dir": "secure-creds",
                    "credential_state_backend": {
                        "mode": "credential_source",
                        "source": "sealbox",
                        "namespace": "test-namespace",
                        "fallback_to_files": True,
                    },
                    "credential_catalog": {
                        "enabled": True,
                        "template_overrides_path": "catalog_overrides.yaml",
                        "default_write_source": "bitwarden",
                        "collection_session_ttl_seconds": 321,
                        "secret_cache_ttl_seconds": 0,
                    },
                    "bitwarden_server_url": "https://vault.example.test",
                    "sealbox": {
                        "enabled": True,
                        "manage_service": False,
                        "auto_initialize": False,
                        "bind_host": "127.0.0.1",
                        "bind_port": 9988,
                        "service_url": "https://sealbox.example.test",
                        "store_path": "sealbox/custom.db",
                        "root_token_file": "sealbox/root-token",
                        "tenant_material_dir": "sealbox/tenant-material",
                        "api_version": "v2",
                        "ready_timeout_seconds": 45,
                    },
                },
                "activity_collector": {
                    "base_url": "http://collector.example.test",
                    "connect_timeout_seconds": 0.5,
                    "request_timeout_seconds": 4.5,
                    "batch_max_events": 7,
                    "flush_interval_seconds": 0.5,
                    "max_buffered_events": 77,
                    "drop_when_unavailable": False,
                    "bind_host": "0.0.0.0",
                    "bind_port": 9875,
                    "user_url_path": "events",
                    "admin_url_path": "admin-events",
                    "otel_trace_base_url": "http://traces.example.test",
                    "storage": {
                        "backend": "sqlite",
                        "sqlite": {"path": "dbs/custom_activity.db"},
                        "postgres": {},
                    },
                },
                "internal_website_docs_root": "user_docs/site",
                "internal_website_skills_dir": "skills",
                "source_env_filename": "uploaded.env",
                "source_env_content": "ACTIVITY_COLLECTOR_USER_TOKEN=acu_uploaded\n",
                "brave_search_api_key": "brave-secret",
                "crawl4ai_mcp_server_url": "http://crawl4ai.example.test/mcp/sse",
                "postgres_dsn": "postgresql://ua:secret@postgres.example.test:5432/ua",
                "searxng_base_url": "https://searxng.example.test",
                "searxng_verify_tls": "false",
                "searxng_timeout_seconds": "9",
                "searxng_default_num_results": "12",
                "team_skills_repositories": ["https://github.com/enzimo/ua-skills"],
                "selected_agents": {
                    "TeamArchitect": True,
                    "PersonalAdminAgent": True,
                    "ResearchComparisonAgent": True,
                    "HomeErrandsAgent": True,
                    "MemReviewAgent": True,
                    "LearningCoachingAgent": False,
                    "InternalImprovementAgent": False,
                },
                "selected_agent_counts": {
                    "PersonalAdminAgent": 1,
                    "ResearchComparisonAgent": 2,
                    "HomeErrandsAgent": 1,
                    "MemReviewAgent": 2,
                },
                "voice_enabled": True,
                "voice_response_mode": "text_only",
                "website_enabled": True,
                "default_delivery_channel": "website",
                "telegram_enabled": True,
                "telegram_allowed_user_ids": ["111", "222"],
                "telegram_allowed_chat_ids": ["-100123", "-100456"],
                "telegram_rich_messages": False,
                "discord_enabled": True,
                "discord_allowed_user_ids": ["333", "444"],
                "discord_allowed_channel_ids": ["555", "666"],
                "signal_enabled": True,
                "signal_base_url": "http://signal.example.test:8080",
                "signal_account": "+15550001111",
                "signal_allowed_sender_ids": ["+15551234567"],
                "signal_allowed_group_ids": ["group.signal"],
                "signal_group_policy": "mention",
                "signal_mention_markers": ["@Ava"],
                "signal_poll_timeout_seconds": 3.5,
                "signal_max_messages_per_poll": 25,
                "signal_drop_pending_messages_on_startup": True,
                "photon_enabled": True,
                "photon_project_id": "photon-project",
                "photon_project_secret": "photon-secret",
                "photon_allowed_sender_ids": ["imessage:+15551234567"],
                "photon_allowed_group_ids": ["photon-group"],
                "photon_group_policy": "mention",
                "photon_mention_markers": ["@Ava"],
            },
        )
        bootstrap_payload = await bootstrap_response.json()
        complete_payload = await complete_response.json()

    config_path = instance_root / "instance_config.yaml"
    env_path = instance_root / "team.env"
    config_text = config_path.read_text(encoding="utf-8")
    raw_config = yaml.safe_load(config_text)
    loaded = GatewayInstanceConfig.load(config_path)
    env_text = env_path.read_text(encoding="utf-8")

    assert bootstrap_response.status == 200
    assert bootstrap_payload["network_public_origin"] == "http://10.0.0.5"
    assert "llm_failover" in bootstrap_payload
    assert bootstrap_payload["llm_failover"]["enabled"] is False
    assert bootstrap_payload["llm_failover"]["fallbacks"] == []
    assert "security_layer" in bootstrap_payload
    assert "activity_collector" in bootstrap_payload
    assert "nodejs_available" in bootstrap_payload
    bootstrap_agent_types = {agent["type"] for agent in bootstrap_payload["agent_catalog"]}
    assert "PersonalAdminAgent" in bootstrap_agent_types
    assert "WebResearchAgent" in bootstrap_agent_types
    assert "CodebaseAnalysisAgent" not in bootstrap_agent_types
    assert "IntegrationAgent" not in bootstrap_agent_types
    assert "TestVerificationAgent" not in bootstrap_agent_types
    assert complete_response.status == 200
    assert complete_payload["restart_required"] is False
    assert complete_payload["skills_repos"][0]["copied"] == 1
    assert merged_skill_repos == [
        (["https://github.com/enzimo/ua-skills.git"], loaded.layout.skills_dir)
    ]
    assert loaded.workspace.external_skill_repositories == [
        "https://github.com/enzimo/ua-skills.git"
    ]
    assert config_path.is_file()
    assert env_path.is_file()
    assert "# Instance config schema version" in config_text
    assert "# Gateway-local SQLite DB. Relative paths resolve under instance_root." in config_text
    assert "# Specialist agent processes supervised at headless startup." in config_text
    assert "# Activity collector SQLite DB. Relative paths resolve under instance_root." in config_text
    assert raw_config["schema_version"] == INSTANCE_CONFIG_SCHEMA_VERSION
    assert raw_config["ui"]["onboarding"]["appearance"]["polygon_line_color"] == "#123456"
    assert loaded.interactive_apps.internal_website.web_chat_enabled is True
    assert loaded.interactive_apps.internal_website.public_port == 18090
    assert loaded.network.public_origin == "http://10.0.0.5"
    assert loaded.user.role == "Platform engineer"
    assert loaded.llm.reasoning_effort == "high"
    assert loaded.llm.reasoning_summary is None
    assert loaded.llm.litellm_api_mode == "chat_completions"
    assert loaded.llm.context_window_tokens == 123456
    assert loaded.llm.stream is False
    assert loaded.llm.supports_image_input is True
    assert loaded.llm.failover["enabled"] is True
    assert loaded.llm.failover["switch_for"]["min_turns"] == 5
    assert loaded.llm.failover["switch_for"]["min_seconds"] == 120
    assert loaded.llm.failover["demotion"]["failure_threshold"] == 2
    assert loaded.llm.failover["demotion"]["failure_window_seconds"] == 300
    assert loaded.llm.failover["demotion"]["demotion_seconds"] == 0
    assert loaded.llm.failover["notify_user"] is False
    assert loaded.llm.failover["fallbacks"] == [{"provider": "openai", "model_id": "gpt-4.1-mini"}]
    assert loaded.agent.memory_graph_enabled is True
    assert loaded.embedding.provider == "openai_compatible"
    assert loaded.embedding.model_id == "text-embedding-3-small"
    assert loaded.embedding.api_base == "https://embeddings.example.test/v1"
    assert loaded.embedding.dimensions == 1536
    assert loaded.embedding.timeout_seconds == 12.5
    assert loaded.embedding.organization == "org-test"
    assert loaded.security_layer.mode == "broker"
    assert loaded.security_layer.broker_internal_base_url == "http://127.0.0.1:9876"
    assert loaded.security_layer.broker_public_port == 9876
    assert loaded.security_layer.broker_bind_host == "0.0.0.0"
    assert loaded.security_layer.broker_bind_port == 9876
    assert loaded.security_layer.broker_credentials_dir == "secure-creds"
    assert loaded.security_layer.credential_state_backend.namespace == "test-namespace"
    assert loaded.security_layer.credential_state_backend.fallback_to_files is True
    assert loaded.security_layer.credential_catalog.default_write_source == "bitwarden"
    assert loaded.security_layer.credential_catalog.secret_cache_ttl_seconds == 0
    assert loaded.security_layer.bitwarden_server_url == "https://vault.example.test"
    assert loaded.security_layer.sealbox.manage_service is False
    assert loaded.security_layer.sealbox.service_url == "https://sealbox.example.test"
    assert loaded.security_layer.sealbox.store_path == "sealbox/custom.db"
    assert loaded.security_layer.sealbox.root_token_file == "sealbox/root-token"
    assert loaded.security_layer.sealbox.tenant_material_dir == "sealbox/tenant-material"
    assert loaded.activity_collector.base_url == "http://collector.example.test"
    assert loaded.activity_collector.connect_timeout_seconds == 0.5
    assert loaded.activity_collector.request_timeout_seconds == 4.5
    assert loaded.activity_collector.batch_max_events == 7
    assert loaded.activity_collector.flush_interval_seconds == 0.5
    assert loaded.activity_collector.max_buffered_events == 77
    assert loaded.activity_collector.drop_when_unavailable is False
    assert loaded.activity_collector.bind_host == "0.0.0.0"
    assert loaded.activity_collector.bind_port == 9875
    assert loaded.activity_collector.user_url_path == "/events"
    assert loaded.activity_collector.admin_url_path == "/admin-events"
    assert loaded.activity_collector.otel_trace_base_url == "http://traces.example.test"
    assert loaded.activity_collector.storage.sqlite.path == str(instance_root / "dbs" / "custom_activity.db")
    assert loaded.gateway.storage.sqlite.path == str(instance_root / "dbs" / "gateway_state.db")
    assert {
        agent.named_agent_type: agent.count
        for agent in loaded.gateway.startup_agents
    } == {
        "PersonalAdminAgent": 1,
        "ResearchComparisonAgent": 2,
        "HomeErrandsAgent": 1,
        "MemReviewAgent": 2,
    }
    assert loaded.voice.enabled is True
    assert loaded.voice.response_mode == "text_only"
    assert loaded.voice.input_enabled is True
    assert loaded.voice.output_enabled is False
    assert loaded.interactive_apps.telegram.allowed_user_ids == ["111", "222"]
    assert loaded.interactive_apps.telegram.allowed_chat_ids == ["-100123", "-100456"]
    assert loaded.interactive_apps.telegram.rich_messages is False
    assert loaded.interactive_apps.discord.allowed_user_ids == ["333", "444"]
    assert loaded.interactive_apps.discord.allowed_channel_ids == ["555", "666"]
    assert loaded.interactive_apps.signal.enabled is True
    assert loaded.interactive_apps.signal.base_url == "http://signal.example.test:8080"
    assert loaded.interactive_apps.signal.account == "+15550001111"
    assert loaded.interactive_apps.signal.allowed_sender_ids == ["+15551234567"]
    assert loaded.interactive_apps.signal.allowed_group_ids == ["group.signal"]
    assert loaded.interactive_apps.signal.group_policy == "mention"
    assert loaded.interactive_apps.signal.mention_markers == ["@Ava"]
    assert loaded.interactive_apps.signal.poll_timeout_seconds == 3.5
    assert loaded.interactive_apps.signal.max_messages_per_poll == 25
    assert loaded.interactive_apps.signal.drop_pending_messages_on_startup is True
    assert loaded.interactive_apps.photon.enabled is True
    assert loaded.interactive_apps.photon.project_id == "photon-project"
    assert loaded.interactive_apps.photon.allowed_sender_ids == ["imessage:+15551234567"]
    assert loaded.interactive_apps.photon.allowed_group_ids == ["photon-group"]
    assert loaded.interactive_apps.photon.group_policy == "mention"
    assert loaded.interactive_apps.photon.mention_markers == ["@Ava"]
    assert loaded.interactive_apps.internal_website.docs_root == "user_docs/site"
    assert loaded.interactive_apps.internal_website.skills_dir == "skills"
    assert loaded.layout.published_sites_dir == instance_root.resolve() / "state" / "published_sites"
    assert "WEB_CHAT_AUTH_TOKEN=uaweb_test_token" in env_text
    # Model keys and the Brave key are never written to team.env (a variable
    # there would override every key saved later): they wait in the private
    # hand-over file until the gateway hands them to the broker.
    for secret in ("BRAVE_SEARCH_API_KEY", "brave-secret", "EMBEDDING_API_KEY", "embedding-secret", "provider-token"):
        assert secret not in env_text
    handover_path = loaded.layout.onboarding_account_keys_path
    handover = json.loads(handover_path.read_text(encoding="utf-8"))
    assert handover["keys"] == {"http:brave-search": "brave-secret"}
    model_keys = {address: (entry["kind"], entry["value"]) for address, entry in handover["model_keys"].items()}
    assert model_keys["https://embeddings.example.test"] == ("openai_compatible", "embedding-secret")
    assert "provider-token" in {value for _kind, value in model_keys.values()}
    assert (handover_path.stat().st_mode & 0o777) == 0o600
    assert "CRAWL4AI_MCP_SERVER_URL=http://crawl4ai.example.test/mcp/sse" in env_text
    assert "CRAWL4AI_BASE_URL=" not in env_text
    assert "POSTGRES_DSN=postgresql://ua:secret@postgres.example.test:5432/ua" in env_text
    assert "SEARXNG_BASE_URL=https://searxng.example.test" in env_text
    assert "SEARXNG_VERIFY_TLS=false" in env_text
    assert "SEARXNG_TIMEOUT_SECONDS=9" in env_text
    assert "SEARXNG_DEFAULT_NUM_RESULTS=12" in env_text
    assert "PHOTON_PROJECT_ID=photon-project" in env_text
    assert "PHOTON_PROJECT_SECRET=photon-secret" in env_text
    assert "ACTIVITY_COLLECTOR_USER_TOKEN=acu_uploaded" in env_text
    assert "ACTIVITY_COLLECTOR_ADMIN_TOKEN=" in env_text
    assert "ACTIVITY_COLLECTOR_INGEST_TOKEN=" in env_text


@pytest.mark.asyncio
async def test_onboarding_web_service_preserves_base_config_runtime_defaults(tmp_path: Path) -> None:
    base_config = tmp_path / "config.example.yaml"
    base_config.write_text(
        "schema_version: 1\n"
        "agent:\n"
        "  default_team_id: team_template\n"
        "  max_queued_top_level_tasks: 50\n"
        "  subtask_timeout_seconds: 1200\n"
        "  max_tool_result_tokens_per_call: 20000\n"
        "  inner_loop_context:\n"
        "    max_total_tokens: 180000\n"
        "    preserve_recent_messages: 8\n"
        "    preserve_head_tokens: 500\n"
        "    preserve_tail_tokens: 500\n"
        "    filters:\n"
        "      html_document: {enabled: true, trigger_tokens: 2000}\n"
        "      javascript_or_bundle: {enabled: true, trigger_tokens: 1000}\n"
        "      css_or_style_blob: {enabled: true, trigger_tokens: 2000}\n"
        "      base64_or_binary_like: {enabled: true, trigger_tokens: 500}\n"
        "      json_large: {enabled: true, trigger_tokens: 3000, comms_json_lenient: true, comms_json_trigger_tokens: 20000}\n"
        "      xml_or_svg: {enabled: true, trigger_tokens: 2000}\n"
        "      log_or_trace: {enabled: true, trigger_tokens: 750}\n"
        "      csv_or_table: {enabled: true, trigger_tokens: 2500}\n"
        "      html_boilerplate_text: {enabled: true, trigger_tokens: 1000}\n"
        "      agent_transcript: {enabled: true, trigger_tokens: 1500}\n"
        "      code_file: {enabled: true, trigger_tokens: 30000}\n"
        "  context_engine:\n"
        "    reserved_output_tokens: 55296\n"
        "  debug:\n"
        "    context_dump:\n"
        "      include_reasoning_artifacts: true\n"
        "interactive_apps:\n"
        "  plugins:\n"
        "    builtin.telegram:\n"
        "      settings:\n"
        "        drop_pending_updates: false\n"
        "gateway:\n"
        "  users:\n"
        "    web_user_invite_ttl_seconds: 300\n",
        encoding="utf-8",
    )
    instance_root = tmp_path / "instance"
    service = OnboardingWebService(
        OnboardingWebSettings(
            host="127.0.0.1",
            port=18090,
            instance_root=str(instance_root),
            base_config=str(base_config),
            restart_on_complete=False,
        )
    )

    async with TestClient(TestServer(service.build_app())) as client:
        response = await client.post(
            "/api/onboarding/complete",
            json={
                "instance_name": "instance_ava",
                "team_id": "team_ava",
                "assistant_name": "Ava",
                "user_display_name": "Ada",
                "model_provider": "OpenAI Compatible",
                "model_id": "deepseek/deepseek-v4-flash",
                "web_chat_auth_token": "uaweb_test_token",
                "generate_internal_credentials": False,
            },
        )

    assert response.status == 200
    loaded = GatewayInstanceConfig.load(instance_root / "instance_config.yaml")
    assert loaded.agent.max_queued_top_level_tasks == 50
    assert loaded.agent.subtask_timeout_seconds == 1200
    assert loaded.agent.max_tool_result_tokens_per_call == 20000
    assert loaded.agent.inner_loop_context["max_total_tokens"] == 180000
    assert loaded.agent.inner_loop_context["preserve_recent_messages"] == 8
    assert loaded.agent.inner_loop_context["preserve_head_tokens"] == 500
    assert loaded.agent.inner_loop_context["preserve_tail_tokens"] == 500
    assert loaded.agent.inner_loop_context["filters"]["html_document"]["trigger_tokens"] == 2000
    assert loaded.agent.inner_loop_context["filters"]["javascript_or_bundle"]["trigger_tokens"] == 1000
    assert loaded.agent.inner_loop_context["filters"]["css_or_style_blob"]["trigger_tokens"] == 2000
    assert loaded.agent.inner_loop_context["filters"]["base64_or_binary_like"]["trigger_tokens"] == 500
    assert loaded.agent.inner_loop_context["filters"]["json_large"]["trigger_tokens"] == 3000
    assert loaded.agent.inner_loop_context["filters"]["json_large"]["comms_json_trigger_tokens"] == 20000
    assert loaded.agent.inner_loop_context["filters"]["xml_or_svg"]["trigger_tokens"] == 2000
    assert loaded.agent.inner_loop_context["filters"]["log_or_trace"]["trigger_tokens"] == 750
    assert loaded.agent.inner_loop_context["filters"]["csv_or_table"]["trigger_tokens"] == 2500
    assert loaded.agent.inner_loop_context["filters"]["html_boilerplate_text"]["trigger_tokens"] == 1000
    assert loaded.agent.inner_loop_context["filters"]["agent_transcript"]["trigger_tokens"] == 1500
    assert loaded.agent.inner_loop_context["filters"]["code_file"]["trigger_tokens"] == 30000
    assert loaded.agent.context_engine["reserved_output_tokens"] == 55296
    assert loaded.agent.debug["context_dump"]["include_reasoning_artifacts"] is True
    assert loaded.interactive_apps.telegram.drop_pending_updates is False
    assert loaded.interactive_apps.telegram.rich_messages is True
    assert loaded.gateway.users.web_user_invite_ttl_seconds == 300


@pytest.mark.asyncio
async def test_onboarding_web_service_can_skip_internal_credential_generation(tmp_path: Path) -> None:
    instance_root = tmp_path / "instance"
    service = OnboardingWebService(
        OnboardingWebSettings(
            host="127.0.0.1",
            port=18090,
            instance_root=str(instance_root),
            restart_on_complete=False,
        )
    )

    async with TestClient(TestServer(service.build_app())) as client:
        response = await client.post(
            "/api/onboarding/complete",
            json={
                "instance_name": "instance_ava",
                "team_id": "team_ava",
                "assistant_name": "Ava",
                "model_endpoint": "https://openrouter.ai/api/v1",
                "model_token": "provider-token",
                "model_id": "deepseek/deepseek-v4-flash",
                "generate_internal_credentials": False,
                "source_env_filename": "uploaded.env",
                "source_env_content": "ACTIVITY_COLLECTOR_USER_TOKEN=acu_uploaded\n",
                "website_enabled": True,
                "default_delivery_channel": "website",
            },
        )

    env_text = (instance_root / "team.env").read_text(encoding="utf-8")
    assert response.status == 200
    assert "ACTIVITY_COLLECTOR_USER_TOKEN=acu_uploaded" in env_text
    assert "ACTIVITY_COLLECTOR_ADMIN_TOKEN=" not in env_text
    assert "ACTIVITY_COLLECTOR_INGEST_TOKEN=" not in env_text


@pytest.mark.asyncio
async def test_onboarding_web_service_finalizes_before_restart(tmp_path: Path) -> None:
    instance_root = tmp_path / "instance"
    service = OnboardingWebService(
        OnboardingWebSettings(
            host="127.0.0.1",
            port=18090,
            instance_root=str(instance_root),
            restart_on_complete=True,
        )
    )
    service.completion_event = asyncio.Event()

    async with TestClient(TestServer(service.build_app())) as client:
        finalize_response = await client.post(
            "/api/onboarding/complete",
            json={
                "instance_name": "instance_ava",
                "team_id": "team_ava",
                "assistant_name": "Ava",
                "model_endpoint": "https://openrouter.ai/api/v1",
                "model_token": "provider-token",
                "model_id": "deepseek/deepseek-v4-flash",
                "web_chat_auth_token": "uaweb_test_token",
                "finalize_only": True,
                "website_enabled": True,
                "default_delivery_channel": "website",
            },
        )
        finalize_payload = await finalize_response.json()
        restart_response = await client.post("/api/onboarding/restart", json={})
        restart_payload = await restart_response.json()
        await asyncio.sleep(0.6)

    assert finalize_response.status == 200
    assert finalize_payload["restart_required"] is False
    assert (instance_root / "instance_config.yaml").is_file()
    assert (instance_root / "team.env").is_file()
    assert restart_response.status == 200
    assert restart_payload["restart_required"] is True
    assert service.completion_event.is_set()


@pytest.mark.asyncio
async def test_openrouter_finalization_ignores_inactive_xai_oauth_url_with_voice_enabled(
    tmp_path: Path,
) -> None:
    instance_root = tmp_path / "instance"
    service = OnboardingWebService(
        OnboardingWebSettings(
            host="127.0.0.1",
            port=18090,
            instance_root=str(instance_root),
            restart_on_complete=False,
        )
    )

    async with TestClient(TestServer(service.build_app())) as client:
        response = await client.post(
            "/api/onboarding/complete",
            json={
                "instance_name": "instance_ruth",
                "team_id": "team_ua",
                "assistant_name": "Ruth",
                "model_provider": "OpenRouter",
                "model_endpoint": "https://openrouter.ai/api/v1",
                "model_token": "provider-token",
                "model_id": "minimax/minimax-m3",
                "xai_oauth": {"base_url": "https://openrouter.ai/api/v1"},
                "xai_api_key": "xai-voice-key",
                "voice_enabled": True,
                "voice_response_mode": "text_only",
                "website_enabled": True,
                "default_delivery_channel": "website",
                "generate_internal_credentials": False,
                "finalize_only": True,
            },
        )
        payload = await response.json()

    assert response.status == 200
    assert payload["status"] == "ok"
    config = GatewayInstanceConfig.load(instance_root / "instance_config.yaml")
    assert config.llm.provider == "litellm"
    assert config.llm.litellm_api_base == "https://openrouter.ai/api/v1"
    assert config.llm.xai_oauth.base_url == "https://api.x.ai/v1"
    assert config.voice.enabled is True
    assert config.voice.input_enabled is True
    assert config.voice.output_enabled is False
    assert "xai-voice-key" not in (instance_root / "team.env").read_text(encoding="utf-8")
    handover = json.loads(config.layout.onboarding_account_keys_path.read_text(encoding="utf-8"))
    assert handover["model_keys"]["https://api.x.ai"]["value"] == "xai-voice-key"


@pytest.mark.asyncio
async def test_xai_oauth_finalization_reports_invalid_active_base_url(tmp_path: Path) -> None:
    service = OnboardingWebService(
        OnboardingWebSettings(
            host="127.0.0.1",
            port=18090,
            instance_root=str(tmp_path / "instance"),
            restart_on_complete=False,
        )
    )

    async with TestClient(TestServer(service.build_app())) as client:
        response = await client.post(
            "/api/onboarding/complete",
            json={
                "instance_name": "instance_ruth",
                "team_id": "team_ua",
                "assistant_name": "Ruth",
                "model_provider": "xAI Grok OAuth",
                "model_endpoint": "https://openrouter.ai/api/v1",
                "model_id": "grok-4.5",
                "xai_oauth": {"base_url": "https://openrouter.ai/api/v1"},
                "website_enabled": True,
                "default_delivery_channel": "website",
                "generate_internal_credentials": False,
                "finalize_only": True,
            },
        )
        payload = await response.json()

    assert response.status == 400
    assert payload["message"] == (
        "Configuration could not be finalized. Review the validation details below."
    )
    assert payload["details"] == [
        "base_url: xai_oauth.base_url must use HTTPS on x.ai or an x.ai subdomain."
    ]


@pytest.mark.asyncio
async def test_onboarding_web_service_imports_generic_config_yaml(tmp_path: Path) -> None:
    service = OnboardingWebService(
        OnboardingWebSettings(
            host="127.0.0.1",
            port=18090,
            instance_root=str(tmp_path / "instance"),
            restart_on_complete=False,
        )
    )
    config_yaml = """
schema_version: 1
llm:
  provider: litellm
  model_id: openrouter/deepseek/deepseek-v4-flash
  supports_image_input: true
  supports_audio_input: false
  supports_file_input: true
  model_capabilities:
    litellm:
      openrouter/deepseek/deepseek-v4-flash:
        supports_image_input: true
        supports_audio_input: false
        supports_file_input: true
  litellm_api_base: https://openrouter.ai/api/v1
agent:
  default_team_id: team_from_config
  default_request_timezone: Europe/Berlin
delivery:
  quiet_hours:
    enabled: true
    start_hour_local: 20
    end_hour_local: 6
voice:
  enabled: true
  response_mode: voice_only
interactive_apps:
  plugins:
    builtin.telegram:
      settings:
        rich_messages: false
ui:
  onboarding:
    appearance:
      polygon_line_color: "#445566"
"""

    async with TestClient(TestServer(service.build_app())) as client:
        response = await client.post(
            "/api/onboarding/import-config",
            json={"filename": "config.yaml", "content": config_yaml},
        )
        payload = await response.json()

    assert response.status == 200
    assert payload["kind"] == "config"
    settings = payload["settings"]
    assert settings["team_id"] == "team_from_config"
    assert settings["timezone"] == "Europe/Berlin"
    assert settings["model_provider"] == "OpenRouter"
    assert settings["model_endpoint"] == "https://openrouter.ai/api/v1"
    assert settings["supports_image_input"] is True
    assert settings["supports_audio_input"] is False
    assert settings["supports_file_input"] is True
    assert settings["model_capabilities"]["litellm"]["openrouter/deepseek/deepseek-v4-flash"] == {
        "supports_image_input": True,
        "supports_audio_input": False,
        "supports_file_input": True,
    }
    assert settings["quiet_start"] == 20
    assert settings["quiet_end"] == 6
    assert settings["voice_enabled"] is True
    assert settings["voice_response_mode"] == "voice_only"
    assert not set(REMOVED_TOOL_POLICY_KEYS) & settings.keys()
    assert settings["telegram_rich_messages"] is False
    assert settings["appearance"]["polygon_line_color"] == "#445566"


@pytest.mark.asyncio
async def test_fresh_onboarding_imports_current_sealbox_config_and_seeds_owner(tmp_path: Path) -> None:
    """Current Sealbox settings are imported; the removed single-tenant key files are listed, not kept."""
    instance_root = tmp_path / "instance"
    service = OnboardingWebService(
        OnboardingWebSettings(
            host="127.0.0.1",
            port=18090,
            instance_root=str(instance_root),
            restart_on_complete=False,
        )
    )
    config_yaml = """
schema_version: 1
agent:
  default_team_id: team_legacy
  default_request_timezone: America/Chicago
security_layer:
  mode: broker
  sealbox:
    enabled: true
    manage_service: true
    auto_initialize: true
    root_token_file: migrated/root-token
    legacy_public_key_file: migrated/public-key.pem
    legacy_private_key_file: migrated/private-key.pem
  credential_catalog:
    database_path: migrated/credential_catalog.db
    path: credential_bindings.yaml
    legacy_owner_canonical_user_id: first-admin
"""

    async with TestClient(TestServer(service.build_app())) as client:
        import_response = await client.post(
            "/api/onboarding/import-config",
            json={"filename": "config.yaml", "content": config_yaml},
        )
        imported = await import_response.json()
        settings = imported["settings"]
        complete_response = await client.post(
            "/api/onboarding/complete",
            json={
                "instance_name": "instance_legacy",
                "team_id": settings["team_id"],
                "assistant_name": "Nova",
                "user_display_name": "First Admin",
                "timezone": settings["timezone"],
                "model_provider": "OpenAI Compatible",
                "model_id": "test-model",
                "web_chat_auth_token": "uaweb_test_token",
                "generate_internal_credentials": False,
                "security_layer": settings["security_layer"],
            },
        )

    assert import_response.status == 200
    sealbox_settings = settings["security_layer"]["sealbox"]
    assert sealbox_settings["root_token_file"] == "migrated/root-token"
    assert not {"legacy_public_key_file", "legacy_private_key_file"} & sealbox_settings.keys()
    skipped = {item["path"]: item["reason"] for item in imported["skipped"]}
    for removed in ("legacy_public_key_file", "legacy_private_key_file"):
        assert "Removed in this version" in skipped[f"security_layer.sealbox.{removed}"]
    catalog_settings = settings["security_layer"]["credential_catalog"]
    assert catalog_settings["database_path"] == "migrated/credential_catalog.db"
    assert not {"path", "legacy_owner_canonical_user_id"} & catalog_settings.keys()
    for removed in ("path", "legacy_owner_canonical_user_id"):
        assert "Removed in this version" in skipped[f"security_layer.credential_catalog.{removed}"]
    assert sealbox_settings["api_version"] == "v2"
    assert complete_response.status == 200

    config = GatewayInstanceConfig.load(instance_root / "instance_config.yaml")
    assert config.security_layer.sealbox.root_token_file == "migrated/root-token"
    assert config.security_layer.sealbox.api_version == "v2"
    assert config.security_layer.credential_catalog.database_path == "migrated/credential_catalog.db"

    storage_settings = config.resolve_state_storage_settings(env={})
    store = GatewayStateStore(
        storage_settings.sqlite_path or "",
        storage_settings=storage_settings,
    )
    memberships = store.list_team_memberships(config.user.canonical_user_id, status="active")
    assert [(membership.team_id, membership.role) for membership in memberships] == [
        ("team_legacy", "owner")
    ]


@pytest.mark.asyncio
async def test_onboarding_web_service_imports_generic_openai_codex_config_yaml(tmp_path: Path) -> None:
    service = OnboardingWebService(
        OnboardingWebSettings(
            host="127.0.0.1",
            port=18090,
            instance_root=str(tmp_path / "instance"),
            restart_on_complete=False,
        )
    )
    config_yaml = """
schema_version: 1
llm:
  provider: openai-codex
  model_id: gpt-5.5
  openai_codex:
    base_url: https://codex.example.test/backend-api/codex
    auth_store_path: auth/imported_openai_codex_auth.json
    shared_codex_home: bootstrap-codex-home
    allow_shared_codex_auth_read: false
    refresh_skew_seconds: 180
    refresh_timeout_seconds: 25
    model_discovery:
      enabled: true
      cache_path: cache/imported_codex_models.json
      cache_ttl_seconds: 7200
      client_version: 1.0.0
agent:
  default_team_id: team_codex_config
  default_request_timezone: America/New_York
"""

    async with TestClient(TestServer(service.build_app())) as client:
        response = await client.post(
            "/api/onboarding/import-config",
            json={"filename": "config.yaml", "content": config_yaml},
        )
        payload = await response.json()

    assert response.status == 200
    assert payload["kind"] == "config"
    settings = payload["settings"]
    assert settings["team_id"] == "team_codex_config"
    assert settings["timezone"] == "America/New_York"
    assert settings["model_provider"] == "OpenAI Codex OAuth"
    assert settings["model_endpoint"] == "https://codex.example.test/backend-api/codex"
    assert settings["model_id"] == "gpt-5.5"
    assert settings["openai_codex"]["auth_store_path"] == "auth/imported_openai_codex_auth.json"
    assert settings["openai_codex"]["shared_codex_home"] == "bootstrap-codex-home"
    assert settings["openai_codex"]["allow_shared_codex_auth_read"] is False
    assert settings["openai_codex"]["model_discovery"]["cache_path"] == "cache/imported_codex_models.json"


@pytest.mark.asyncio
async def test_onboarding_web_service_imports_instance_config_yaml(tmp_path: Path) -> None:
    instance_root = tmp_path / "source"
    config = GatewayInstanceConfig.create(
        instance_name="imported",
        instance_root=str(instance_root),
        team_id="team_imported",
        timezone="Asia/Tokyo",
        display_name="Riley",
        assistant_name="Nova",
    )
    config.user.role = "Engineering lead"
    config.user.initial_context = "Prefers detailed vehicle comparisons."
    config.llm.provider = "litellm"
    config.llm.model_id = "custom-model"
    config.llm.litellm_api_base = "https://llm.example.test/v1"
    config.llm.supports_image_input = True
    config.llm.supports_audio_input = False
    config.llm.supports_file_input = True
    config.llm.model_capabilities = {
        "litellm": {
            "custom-model": {
                "supports_image_input": True,
                "supports_audio_input": False,
                "supports_file_input": True,
            }
        }
    }
    config.delivery.quiet_hours.enabled = False
    config.voice.enabled = True
    config.voice.response_mode = "text_only"
    config.interactive_apps.telegram.enabled = True
    config.interactive_apps.telegram.allowed_user_ids = ["67890"]
    config.interactive_apps.telegram.allowed_chat_ids = ["12345"]
    config.interactive_apps.telegram.rich_messages = False
    config.interactive_apps.discord.enabled = True
    config.interactive_apps.discord.allowed_user_ids = ["24680"]
    config.interactive_apps.discord.allowed_channel_ids = ["13579"]
    config.interactive_apps.signal.enabled = True
    config.interactive_apps.signal.base_url = "http://signal.example.test:8080"
    config.interactive_apps.signal.account = "+15550001111"
    config.interactive_apps.signal.allowed_sender_ids = ["+15551234567"]
    config.interactive_apps.signal.allowed_group_ids = ["group.signal"]
    config.interactive_apps.signal.mention_markers = ["@Nova"]
    config_path = tmp_path / "instance_config.yaml"
    config.save(config_path)
    service = OnboardingWebService(
        OnboardingWebSettings(
            host="127.0.0.1",
            port=18090,
            instance_root=str(tmp_path / "target"),
            restart_on_complete=False,
        )
    )

    async with TestClient(TestServer(service.build_app())) as client:
        response = await client.post(
            "/api/onboarding/import-config",
            json={"filename": "instance_config.yaml", "content": config_path.read_text(encoding="utf-8")},
        )
        payload = await response.json()

    assert response.status == 200
    assert payload["kind"] == "instance_config"
    settings = payload["settings"]
    # The new instance keeps its own folder; the old one is listed as not imported.
    assert "instance_root" not in settings
    assert "workspace_dir" not in settings
    skipped = {item["path"]: item["reason"] for item in payload["skipped"]}
    assert "own folder" in skipped["instance_root"]
    assert settings["team_id"] == "team_imported"
    assert settings["timezone"] == "Asia/Tokyo"
    assert settings["user_display_name"] == "Riley"
    assert settings["user_role"] == "Engineering lead"
    assert settings["user_context"] == "Prefers detailed vehicle comparisons."
    assert settings["assistant_name"] == "Nova"
    assert settings["model_provider"] == "OpenAI Compatible"
    assert settings["model_endpoint"] == "https://llm.example.test/v1"
    assert settings["supports_image_input"] is True
    assert settings["supports_audio_input"] is False
    assert settings["supports_file_input"] is True
    assert settings["model_capabilities"]["litellm"]["custom-model"] == {
        "supports_image_input": True,
        "supports_audio_input": False,
        "supports_file_input": True,
    }
    assert settings["quiet_enabled"] is False
    assert settings["voice_response_mode"] == "text_only"
    assert not set(REMOVED_TOOL_POLICY_KEYS) & settings.keys()
    assert settings["telegram_enabled"] is True
    assert settings["telegram_allowed_user_ids"] == ["67890"]
    assert settings["telegram_allowed_chat_ids"] == ["12345"]
    assert settings["telegram_rich_messages"] is False
    assert settings["discord_enabled"] is True
    assert settings["discord_allowed_user_ids"] == ["24680"]
    assert settings["discord_allowed_channel_ids"] == ["13579"]
    assert settings["signal_enabled"] is True
    assert settings["signal_base_url"] == "http://signal.example.test:8080"
    assert settings["signal_account"] == "+15550001111"
    assert settings["signal_allowed_sender_ids"] == ["+15551234567"]
    assert settings["signal_allowed_group_ids"] == ["group.signal"]
    assert settings["signal_group_policy"] == "mention"
    assert settings["signal_mention_markers"] == ["@Nova"]


@pytest.mark.asyncio
async def test_onboarding_web_service_imports_openai_codex_instance_config_yaml(tmp_path: Path) -> None:
    instance_root = tmp_path / "source"
    config = GatewayInstanceConfig.create(
        instance_name="imported-codex",
        instance_root=str(instance_root),
        team_id="team_imported_codex",
        timezone="America/Los_Angeles",
        display_name="Riley",
        assistant_name="Nova",
    )
    config.llm.provider = "openai-codex"
    config.llm.model_id = "gpt-5.5"
    config.llm.openai_codex.base_url = "https://codex.instance.test/backend-api/codex"
    config.llm.openai_codex.auth_store_path = "auth/instance_codex_auth.json"
    config.llm.openai_codex.shared_codex_home = "instance-codex-home"
    config.llm.openai_codex.allow_shared_codex_auth_read = False
    config.llm.openai_codex.refresh_skew_seconds = 240
    config.llm.openai_codex.model_discovery.cache_path = "cache/instance_codex_models.json"
    config_path = tmp_path / "instance_config.yaml"
    config.save(config_path)
    service = OnboardingWebService(
        OnboardingWebSettings(
            host="127.0.0.1",
            port=18090,
            instance_root=str(tmp_path / "target"),
            restart_on_complete=False,
        )
    )

    async with TestClient(TestServer(service.build_app())) as client:
        response = await client.post(
            "/api/onboarding/import-config",
            json={"filename": "instance_config.yaml", "content": config_path.read_text(encoding="utf-8")},
        )
        payload = await response.json()

    assert response.status == 200
    assert payload["kind"] == "instance_config"
    settings = payload["settings"]
    assert settings["team_id"] == "team_imported_codex"
    assert settings["model_provider"] == "OpenAI Codex OAuth"
    assert settings["model_endpoint"] == "https://codex.instance.test/backend-api/codex"
    assert settings["model_id"] == "gpt-5.5"
    assert settings["openai_codex"]["auth_store_path"] == "auth/instance_codex_auth.json"
    assert settings["openai_codex"]["shared_codex_home"] == "instance-codex-home"
    assert settings["openai_codex"]["allow_shared_codex_auth_read"] is False
    assert settings["openai_codex"]["refresh_skew_seconds"] == 240
    assert settings["openai_codex"]["model_discovery"]["cache_path"] == "cache/instance_codex_models.json"


@pytest.mark.asyncio
async def test_onboarding_web_completion_preserves_openai_codex_import_payload(tmp_path: Path) -> None:
    instance_root = tmp_path / "target"
    service = OnboardingWebService(
        OnboardingWebSettings(
            host="127.0.0.1",
            port=18090,
            instance_root=str(instance_root),
            restart_on_complete=False,
        )
    )
    openai_codex = {
        "base_url": "https://codex.imported.test/backend-api/codex",
        "auth_store_path": "auth/imported_codex_auth.json",
        "shared_codex_home": "imported-codex-home",
        "allow_shared_codex_auth_read": False,
        "refresh_skew_seconds": 300,
        "refresh_timeout_seconds": 30,
        "model_discovery": {
            "enabled": True,
            "cache_path": "cache/imported_codex_models.json",
            "cache_ttl_seconds": 5400,
            "client_version": "1.0.0",
        },
    }

    async with TestClient(TestServer(service.build_app())) as client:
        response = await client.post(
            "/api/onboarding/complete",
            json={
                "instance_name": "target",
                "instance_root": str(instance_root),
                "team_id": "team_codex_complete",
                "timezone": "America/Chicago",
                "user_display_name": "Riley",
                "assistant_name": "Nova",
                "model_provider": "OpenAI Codex OAuth",
                "model_endpoint": "https://codex.final.test/backend-api/codex",
                "model_id": "gpt-5.5",
                "openai_codex": openai_codex,
                "default_delivery_channel": "website",
                "quiet_enabled": True,
                "quiet_start": 22,
                "quiet_end": 8,
            },
        )
        payload = await response.json()

    assert response.status == 200
    assert payload["status"] == "ok"
    loaded = GatewayInstanceConfig.load(instance_root / "instance_config.yaml")
    assert loaded.llm.provider == "openai-codex"
    assert loaded.llm.model_id == "gpt-5.5"
    assert loaded.llm.openai_codex.base_url == "https://codex.final.test/backend-api/codex"
    assert loaded.llm.openai_codex.auth_store_path == "auth/imported_codex_auth.json"
    assert loaded.llm.openai_codex.shared_codex_home == "imported-codex-home"
    assert loaded.llm.openai_codex.allow_shared_codex_auth_read is False
    assert loaded.llm.openai_codex.refresh_skew_seconds == 300
    assert loaded.llm.openai_codex.refresh_timeout_seconds == 30
    assert loaded.llm.openai_codex.model_discovery.cache_path == "cache/imported_codex_models.json"
    assert loaded.llm.openai_codex.model_discovery.cache_ttl_seconds == 5400


def test_provider_connection_uses_onboarding_prompt(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: dict[str, object] = {}

    class FakeResponse:
        def __enter__(self) -> "FakeResponse":
            return self

        def __exit__(self, exc_type, exc, traceback) -> None:  # type: ignore[no-untyped-def]
            return None

        def read(self, _limit: int = -1) -> bytes:
            return json.dumps(
                {"choices": [{"message": {"content": "I'm connected and ready to go!"}}]}
            ).encode("utf-8")

    def fake_urlopen(request, timeout):  # type: ignore[no-untyped-def]
        captured["url"] = request.full_url
        captured["timeout"] = timeout
        captured["body"] = json.loads(request.data.decode("utf-8"))
        return FakeResponse()

    monkeypatch.setattr(connection_test.urllib_request, "urlopen", fake_urlopen)

    result = provider_test.test_provider_connection(
        provider="OpenAI Compatible",
        endpoint="https://example.test/v1",
        token="secret-token",
        model="demo-model",
        reasoning_effort="medium",
    )

    assert result.ok is True
    assert captured["url"] == "https://example.test/v1/chat/completions"
    assert captured["body"] == {
        "model": "demo-model",
        "messages": [{"role": "user", "content": "Reply to me: 'I'm connected and ready to go!'"}],
        "max_tokens": 10,
        "tools": [connection_test.CONNECTION_TEST_CHAT_TOOL],
        "reasoning_effort": "medium",
    }


@pytest.mark.asyncio
async def test_provider_test_handler_forwards_reasoning_request_options(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    captured: dict[str, object] = {}

    async def fake_test_provider_connection(check) -> provider_test.ProviderTestResult:
        captured.update(check.model_dump())
        return provider_test.ProviderTestResult(
            ok=True,
            provider=check.provider,
            message="Connection test succeeded.",
        )

    monkeypatch.setattr(
        "assistant_gateway.onboarding_web.provider_process.run_provider_check",
        fake_test_provider_connection,
    )
    service = OnboardingWebService(
        OnboardingWebSettings(instance_root=str(tmp_path / "instance"), restart_on_complete=False)
    )

    async with TestClient(TestServer(service.build_app())) as client:
        response = await client.post(
            "/api/provider-test",
            json={
                "provider": "OpenAI",
                "endpoint": "https://api.openai.test/v1",
                "token": "test-token",
                "model": "gpt-5.6-luna",
                "reasoning_effort": "medium",
                "reasoning_summary": "auto",
                "litellm_api_mode": "responses",
                "stream": True,
            },
        )

    assert response.status == 200
    assert captured["reasoning_effort"] == "medium"
    assert captured["reasoning_summary"] == "auto"
    assert captured["litellm_api_mode"] == "responses"
    assert captured["stream"] is True


@pytest.mark.asyncio
async def test_provider_test_handler_logs_sanitized_failure(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    async def fake_test_provider_connection(check) -> provider_test.ProviderTestResult:
        return provider_test.ProviderTestResult(
            ok=False,
            provider=check.provider,
            message="Could not reach the configured API Base URL: [Errno 111] Connection refused",
        )

    monkeypatch.setattr(
        "assistant_gateway.onboarding_web.provider_process.run_provider_check",
        fake_test_provider_connection,
    )
    service = OnboardingWebService(
        OnboardingWebSettings(
            instance_root=str(tmp_path / "instance"), restart_on_complete=False
        )
    )
    caplog.set_level("WARNING", logger="assistant_gateway.onboarding_web.server")

    async with TestClient(TestServer(service.build_app())) as client:
        response = await client.post(
            "/api/provider-test",
            json={
                "provider": "OpenAI Compatible",
                "endpoint": "https://url-user:url-secret@example.test/v1?key=query-secret",
                "token": "token-secret",
                "model": "openrouter/deepseek/deepseek-v4.1-flash",
            },
        )

    assert response.status == 502
    assert "event=onboarding_provider_test_failed" in caplog.text
    assert "provider=OpenAI Compatible" in caplog.text
    assert "endpoint_host=example.test" in caplog.text
    assert "endpoint_path=/v1" in caplog.text
    assert "Connection refused" in caplog.text
    assert "url-secret" not in caplog.text
    assert "query-secret" not in caplog.text
    assert "token-secret" not in caplog.text


def test_openai_provider_connection_uses_responses_reasoning_and_tools(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, object] = {}

    class FakeResponse:
        def __enter__(self) -> "FakeResponse":
            return self

        def __exit__(self, exc_type, exc, traceback) -> None:  # type: ignore[no-untyped-def]
            return None

        def read(self, _limit: int = -1) -> bytes:
            return (
                'event: response.output_text.delta\n'
                'data: {"type":"response.output_text.delta","delta":"I\'m connected and ready to go!"}\n\n'
                'event: response.completed\n'
                'data: {"type":"response.completed","response":{"id":"response-test"}}\n\n'
            ).encode("utf-8")

    def fake_urlopen(request, timeout):  # type: ignore[no-untyped-def]
        captured["url"] = request.full_url
        captured["body"] = json.loads(request.data.decode("utf-8"))
        return FakeResponse()

    monkeypatch.setattr(connection_test.urllib_request, "urlopen", fake_urlopen)

    result = provider_test.test_provider_connection(
        provider="OpenAI",
        endpoint="https://example.test/v1",
        token="secret-token",
        model="gpt-5.6-luna",
        reasoning_effort="medium",
        reasoning_summary="auto",
    )

    assert result.ok is True
    assert result.sample == "I'm connected and ready to go!"
    assert captured["url"] == "https://example.test/v1/responses"
    assert captured["body"] == {
        "model": "gpt-5.6-luna",
        "input": [
            {
                "role": "user",
                "content": [
                    {
                        "type": "input_text",
                        "text": "Reply to me: 'I'm connected and ready to go!'",
                    }
                ],
            }
        ],
        "max_output_tokens": connection_test.CONNECTION_TEST_MAX_OUTPUT_TOKENS,
        "tools": [connection_test.CONNECTION_TEST_RESPONSES_TOOL],
        "reasoning": {"effort": "medium", "summary": "auto"},
        "stream": True,
        "store": False,
    }


def test_compatible_provider_connection_can_use_responses_reasoning_summary(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, object] = {}

    class FakeResponse:
        def __enter__(self) -> "FakeResponse":
            return self

        def __exit__(self, exc_type, exc, traceback) -> None:  # type: ignore[no-untyped-def]
            return None

        def read(self, _limit: int = -1) -> bytes:
            return (
                'data: {"type":"response.output_text.delta","delta":"ready"}\n\n'
                'data: {"type":"response.completed","response":{"id":"response-test"}}\n\n'
            ).encode("utf-8")

    def fake_urlopen(request, timeout):  # type: ignore[no-untyped-def]
        captured["url"] = request.full_url
        captured["body"] = json.loads(request.data.decode("utf-8"))
        return FakeResponse()

    monkeypatch.setattr(connection_test.urllib_request, "urlopen", fake_urlopen)

    result = provider_test.test_provider_connection(
        provider="OpenAI Compatible",
        endpoint="https://proxy.example.test/v1",
        token="secret-token",
        model="openai/gpt-5.6-luna",
        reasoning_effort="xhigh",
        reasoning_summary="auto",
        litellm_api_mode="responses",
        stream=True,
    )

    assert result.ok is True
    assert result.sample == "ready"
    assert captured["url"] == "https://proxy.example.test/v1/responses"
    assert captured["body"] == {
        "model": "openai/gpt-5.6-luna",
        "input": [
            {
                "role": "user",
                "content": [
                    {
                        "type": "input_text",
                        "text": "Reply to me: 'I'm connected and ready to go!'",
                    }
                ],
            }
        ],
        "max_output_tokens": connection_test.CONNECTION_TEST_MAX_OUTPUT_TOKENS,
        "tools": [connection_test.CONNECTION_TEST_RESPONSES_TOOL],
        "reasoning": {"effort": "xhigh", "summary": "auto"},
        "stream": True,
        "store": False,
    }


def test_compatible_responses_provider_test_rejects_disabled_streaming() -> None:
    result = provider_test.test_provider_connection(
        provider="OpenAI Compatible",
        endpoint="https://proxy.example.test/v1",
        token="secret-token",
        model="openai/gpt-5.6-luna",
        reasoning_summary="auto",
        litellm_api_mode="responses",
        stream=False,
    )

    assert result.ok is False
    assert "needs streaming" in result.message


def test_openrouter_provider_connection_keeps_chat_reasoning_and_tools(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, object] = {}

    class FakeResponse:
        def __enter__(self) -> "FakeResponse":
            return self

        def __exit__(self, exc_type, exc, traceback) -> None:  # type: ignore[no-untyped-def]
            return None

        def read(self, _limit: int = -1) -> bytes:
            return json.dumps(
                {"choices": [{"message": {"content": "I'm connected and ready to go!"}}]}
            ).encode("utf-8")

    def fake_urlopen(request, timeout):  # type: ignore[no-untyped-def]
        captured["url"] = request.full_url
        captured["body"] = json.loads(request.data.decode("utf-8"))
        return FakeResponse()

    monkeypatch.setattr(connection_test.urllib_request, "urlopen", fake_urlopen)

    result = provider_test.test_provider_connection(
        provider="OpenRouter",
        endpoint="https://openrouter.example.test/api/v1",
        token="secret-token",
        model="openai/gpt-5.6-luna",
        reasoning_effort="medium",
    )

    assert result.ok is True
    assert captured["url"] == "https://openrouter.example.test/api/v1/chat/completions"
    assert captured["body"] == {
        "model": "openai/gpt-5.6-luna",
        "messages": [{"role": "user", "content": "Reply to me: 'I'm connected and ready to go!'"}],
        "max_completion_tokens": connection_test.CONNECTION_TEST_MAX_COMPLETION_TOKENS,
        "tools": [connection_test.CONNECTION_TEST_CHAT_TOOL],
        "reasoning_effort": "medium",
    }


def test_provider_connection_formats_structured_provider_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    body = json.dumps(
        {
            "error": {
                "message": "Unsupported parameter: max_tokens",
                "param": "max_tokens",
                "code": "unsupported_parameter",
            }
        }
    ).encode("utf-8")

    def fake_urlopen(request, timeout):  # type: ignore[no-untyped-def]
        raise urllib_error.HTTPError(
            request.full_url,
            400,
            "Bad Request",
            {},
            io.BytesIO(body),
        )

    monkeypatch.setattr(connection_test.urllib_request, "urlopen", fake_urlopen)

    result = provider_test.test_provider_connection(
        provider="OpenAI",
        endpoint="https://api.openai.test/v1",
        token="secret-token",
        model="gpt-5.6-luna",
    )

    assert result.ok is False
    assert result.message.startswith("OpenAI answered HTTP 400 after ")
    assert result.message.endswith(
        "seconds: Unsupported parameter: max_tokens (parameter: max_tokens, code: unsupported_parameter)"
    )


def test_openai_codex_provider_connection_uses_native_stream_contract(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, object] = {}

    class FakeResponse:
        def __enter__(self) -> "FakeResponse":
            return self

        def __exit__(self, exc_type, exc, traceback) -> None:  # type: ignore[no-untyped-def]
            return None

        def read(self, _limit: int = -1) -> bytes:
            return (
                'event: response.output_text.delta\n'
                'data: {"type":"response.output_text.delta","delta":"I\'m connected "}\n\n'
                'event: response.output_text.delta\n'
                'data: {"type":"response.output_text.delta","delta":"and ready to go!"}\n\n'
                'event: response.completed\n'
                'data: {"type":"response.completed","response":{"id":"response-test"}}\n\n'
            ).encode("utf-8")

    def fake_urlopen(request, timeout):  # type: ignore[no-untyped-def]
        captured["url"] = request.full_url
        captured["timeout"] = timeout
        captured["headers"] = dict(request.header_items())
        captured["body"] = json.loads(request.data.decode("utf-8"))
        return FakeResponse()

    monkeypatch.setattr(connection_test.urllib_request, "urlopen", fake_urlopen)

    result = provider_test.test_provider_connection(
        provider="OpenAI Codex OAuth",
        endpoint="https://chatgpt.example.test/backend-api/codex",
        token="codex-test-token",
        model="gpt-5.6-luna",
        reasoning_effort="xhigh",
        reasoning_summary="detailed",
    )

    assert result.ok is True
    assert result.sample == "I'm connected and ready to go!"
    assert captured["url"] == "https://chatgpt.example.test/backend-api/codex/responses"
    assert captured["headers"]["Accept"] == "text/event-stream"
    assert captured["body"] == {
        "model": "gpt-5.6-luna",
        "input": [
            {
                "role": "user",
                "content": [
                    {
                        "type": "input_text",
                        "text": "Reply to me: 'I'm connected and ready to go!'",
                    }
                ],
            }
        ],
        "tools": [connection_test.CONNECTION_TEST_RESPONSES_TOOL],
        "reasoning": {"effort": "xhigh", "summary": "detailed"},
        "stream": True,
        "store": False,
    }


def test_openai_codex_provider_connection_reports_stream_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class FakeResponse:
        def __enter__(self) -> "FakeResponse":
            return self

        def __exit__(self, exc_type, exc, traceback) -> None:  # type: ignore[no-untyped-def]
            return None

        def read(self, _limit: int = -1) -> bytes:
            return (
                'event: response.failed\n'
                'data: {"type":"response.failed","response":'
                '{"error":{"message":"model is unavailable"}}}\n\n'
            ).encode("utf-8")

    monkeypatch.setattr(connection_test.urllib_request, "urlopen", lambda request, timeout: FakeResponse())

    result = provider_test.test_provider_connection(
        provider="openai-codex",
        endpoint="https://chatgpt.example.test/backend-api/codex",
        token="codex-test-token",
        model="gpt-5.5",
    )

    assert result.ok is False
    assert result.message == "Codex stream failed: model is unavailable"


def _sharing_service(tmp_path: Path) -> OnboardingWebService:
    return OnboardingWebService(
        OnboardingWebSettings(host="127.0.0.1", port=18090, instance_root=str(tmp_path / "instance"), restart_on_complete=False)
    )


def _finalize_answers(**extra: object) -> dict[str, object]:
    return {
        "instance_name": "instance_ava",
        "team_id": "team_ava",
        "assistant_name": "Ava",
        "model_endpoint": "https://openrouter.ai/api/v1",
        "model_token": "provider-token",
        "model_id": "deepseek/deepseek-v4-flash",
        "web_chat_auth_token": "uaweb_test_token",
        "finalize_only": True,
        "website_enabled": True,
        "default_delivery_channel": "website",
        **extra,
    }


@pytest.mark.asyncio
async def test_bootstrap_lists_the_services_whose_sharing_the_secrets_page_shows(tmp_path: Path) -> None:
    async with TestClient(TestServer(_sharing_service(tmp_path).build_app())) as client:
        payload = await (await client.get("/api/bootstrap")).json()

    services = {item["subject"]: item for item in payload["account_sharing_services"]}
    assert services["http:exa"]["sharing"] == "shared" and services["http:exa"]["fixed"] is False
    assert services["cli:x"]["sharing"] == "private" and services["cli:x"]["fixed"] is False
    assert services["cli:github"]["fixed"] is True and "always private" in services["cli:github"]["note"]
    assert services["http:github"]["fixed"] is True
    assert all(item["display_name"] and item["description"] for item in services.values())


@pytest.mark.asyncio
async def test_finalize_leaves_the_sharing_choices_with_the_keys_for_the_gateway(tmp_path: Path) -> None:
    from assistant_gateway.onboarding_account_keys import read_handover
    from custom_messages.contracts.access_v2 import AccountSharing

    answers = _finalize_answers(
        exa_api_key="exa-typed-key",
        account_sharing={"share_with_team": True, "choices": {"http:exa": "private", "model_keys": "shared"}},
    )
    async with TestClient(TestServer(_sharing_service(tmp_path).build_app())) as client:
        response = await client.post("/api/onboarding/complete", json=answers)

    assert response.status == 200, await response.text()
    handover = read_handover(tmp_path / "instance")
    assert handover is not None and handover.keys == {"http:exa": "exa-typed-key"}
    assert handover.sharing["http:exa"] is AccountSharing.PRIVATE
    assert handover.sharing["model_keys"] is AccountSharing.SHARED
    # Services nobody changed keep where they start; GitHub and Google are never in the choices.
    assert handover.sharing["cli:x"] is AccountSharing.PRIVATE and handover.sharing["http:brave-search"] is AccountSharing.SHARED
    assert "cli:github" not in handover.sharing


@pytest.mark.asyncio
async def test_sharing_off_makes_everything_private_and_a_fixed_service_is_refused(tmp_path: Path) -> None:
    from assistant_gateway.onboarding_account_keys import read_handover

    async with TestClient(TestServer(_sharing_service(tmp_path).build_app())) as client:
        off = await client.post(
            "/api/onboarding/complete", json=_finalize_answers(account_sharing={"share_with_team": False, "choices": {}})
        )
        handover = read_handover(tmp_path / "instance")
        refused = await client.post(
            "/api/onboarding/complete",
            json=_finalize_answers(account_sharing={"share_with_team": True, "choices": {"cli:google": "shared"}}),
        )
        refused_text = await refused.text()

    assert off.status == 200
    assert handover is not None and set(handover.sharing.values()) == {"private"}
    assert refused.status == 400
    assert "not services the team can share or keep private: cli:google" in refused_text


@pytest.mark.parametrize("signal_name", ["SIGTERM", "SIGINT"])
@pytest.mark.parametrize("pending_checks", [0, 2], ids=["idle", "checking-providers"])
def test_the_onboarding_page_stops_cleanly_when_the_container_stops(
    tmp_path: Path, signal_name: str, pending_checks: int
) -> None:
    """Stop before Docker's 10 s grace even with several providers that never answer."""

    import concurrent.futures
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
    import os
    import queue
    import re
    import signal
    import socket
    import sys
    import threading
    import time
    import urllib.request

    arrived: queue.Queue[None] = queue.Queue()
    release_provider = threading.Event()

    class Provider(BaseHTTPRequestHandler):
        def do_POST(self):
            arrived.put(None)
            release_provider.wait(45)
            self.close_connection = True

        def log_message(self, *args):
            pass

    provider = ThreadingHTTPServer(("127.0.0.1", 0), Provider)
    provider_thread = threading.Thread(target=provider.serve_forever, daemon=True)
    provider_thread.start()
    clients = concurrent.futures.ThreadPoolExecutor(max_workers=max(1, pending_checks))

    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
    repo = Path(__file__).resolve().parents[1]
    process = subprocess.Popen(
        [
            sys.executable, str(repo / "assistant_cli.py"), "onboard-web",
            "--instance-root", str(tmp_path / "instance"), "--host", "127.0.0.1", "--port", str(port),
        ],
        cwd=repo,
        env={**os.environ, "TMPDIR": str(tmp_path)},
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )

    def request_check() -> None:
        request = urllib.request.Request(
            f"http://127.0.0.1:{port}/api/provider-test",
            data=json.dumps({
                "provider": "OpenAI Compatible", "model": "test-model", "token": "test-provider-key",
                "endpoint": f"http://127.0.0.1:{provider.server_port}/v1",
            }).encode(),
            headers={"Content-Type": "application/json"},
        )
        # Shutdown closes the request, so either a response or a disconnect is expected.
        try:
            with urllib.request.urlopen(request, timeout=45) as response:
                response.read()
        except OSError:
            pass

    try:
        deadline = time.monotonic() + 90
        while True:
            try:
                with urllib.request.urlopen(f"http://127.0.0.1:{port}/health", timeout=2) as response:
                    if response.status == 200:
                        break
            except OSError:
                pass
            assert process.poll() is None, "the onboarding page stopped before it was ready"
            assert time.monotonic() < deadline, "the onboarding page did not become ready"
            time.sleep(0.2)
        checks = [clients.submit(request_check) for _ in range(pending_checks)]
        for _ in checks:
            arrived.get(timeout=30)
        process.send_signal(getattr(signal, signal_name))
        output, _ = process.communicate(timeout=5)
        for check in checks:
            check.result(timeout=5)
    finally:
        if process.poll() is None:
            process.kill()
            process.communicate()
        release_provider.set()
        provider.shutdown()
        provider.server_close()
        provider_thread.join(timeout=5)
        clients.shutdown(wait=True)
    assert process.returncode == 0, output[-3000:]
    assert f"event=onboarding_web_stop_signal_received signal={signal_name}" in output
    stopped = re.findall(r"event=onboarding_provider_process_stopped pid=(\d+)", output)
    assert len(stopped) == pending_checks
    for child_pid in stopped:
        with pytest.raises(ProcessLookupError):
            os.kill(int(child_pid), 0)
