from __future__ import annotations

from live_nats_client import nats_transport_config_from_env
from live_reliability_scenarios import ManagedAgentProcess
from live_storage_utils import resolve_live_storage_settings


def test_live_nats_transport_config_uses_standard_auth_environment(monkeypatch) -> None:
    monkeypatch.delenv("NATS_SERVERS", raising=False)
    monkeypatch.setenv("NATS_URL", "nats://nats.example.test:4222")
    monkeypatch.setenv("NATS_USER", "live-user")
    monkeypatch.setenv("NATS_PASSWORD", "live-password")
    monkeypatch.setenv("NATS_CREDENTIALS_FILE", "/run/secrets/live-nats.creds")

    config = nats_transport_config_from_env()

    assert config.servers == ["nats://nats.example.test:4222"]
    assert config.user == "live-user"
    assert config.password == "live-password"
    assert config.credentials_file == "/run/secrets/live-nats.creds"


def test_managed_agent_process_build_command_and_env_use_postgres(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("STORAGE_BACKEND", "postgres")
    monkeypatch.setenv("POSTGRES_DSN", "postgresql://team:secret@db/team_agents")
    settings = resolve_live_storage_settings(
        sqlite_fallback_path=tmp_path / "ignored.sqlite",
        workspace_dir=tmp_path,
    )
    managed = ManagedAgentProcess(
        agent_type="TeamArchitect",
        team_id="team_live",
        agent_id="manager_1",
        result_dir=tmp_path,
        env_path="debug.env",
        storage_settings=settings,
        is_lead=True,
    )

    command = managed.build_command()
    env = managed.build_env()

    assert "--task-history-db-path" not in command
    assert env["STORAGE_BACKEND"] == "postgres"
    assert env["POSTGRES_DSN"] == "postgresql://team:secret@db/team_agents_live_tests"
    assert "TASK_HISTORY_DB_PATH" not in env
