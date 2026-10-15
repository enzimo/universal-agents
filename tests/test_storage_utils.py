from __future__ import annotations


from live_storage_utils import resolve_live_storage_settings


def test_resolve_live_storage_settings_defaults_to_sqlite(tmp_path, monkeypatch) -> None:
    monkeypatch.delenv("STORAGE_BACKEND", raising=False)
    monkeypatch.delenv("POSTGRES_DSN", raising=False)

    settings = resolve_live_storage_settings(
        sqlite_fallback_path=tmp_path / "live.sqlite",
        workspace_dir=tmp_path,
    )

    assert settings.backend == "sqlite"
    assert settings.sqlite_path == str((tmp_path / "live.sqlite").resolve())


def test_resolve_live_storage_settings_uses_postgres_env(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("STORAGE_BACKEND", "postgres")
    monkeypatch.setenv("POSTGRES_DSN", "postgresql://team:secret@db/team_agents")

    settings = resolve_live_storage_settings(
        sqlite_fallback_path=tmp_path / "ignored.sqlite",
        workspace_dir=tmp_path,
    )

    assert settings.backend == "postgres"
    assert settings.postgres_dsn == "postgresql://team:secret@db/team_agents_live_tests"


def test_resolve_live_storage_settings_uses_config_backend_with_env_dsn(tmp_path, monkeypatch) -> None:
    monkeypatch.delenv("STORAGE_BACKEND", raising=False)
    monkeypatch.setenv("POSTGRES_DSN", "postgresql://team:secret@db/team_agents")
    config_path = tmp_path / "config.yaml"
    config_path.write_text(
        """
schema_version: 1
storage:
  backend: "postgres"
  postgres:
    dsn: ""
""".strip()
        + "\n",
        encoding="utf-8",
    )

    settings = resolve_live_storage_settings(
        sqlite_fallback_path=tmp_path / "ignored.sqlite",
        workspace_dir=tmp_path,
        config_path=config_path,
    )

    assert settings.backend == "postgres"
    assert settings.postgres_dsn == "postgresql://team:secret@db/team_agents_live_tests"


def test_resolve_live_storage_settings_prefers_explicit_live_test_postgres_dsn(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("STORAGE_BACKEND", "postgres")
    monkeypatch.setenv("POSTGRES_DSN", "postgresql://team:secret@db/team_agents")
    monkeypatch.setenv("LIVE_TEST_POSTGRES_DSN", "postgresql://team:secret@db/team_agents_ci")

    settings = resolve_live_storage_settings(
        sqlite_fallback_path=tmp_path / "ignored.sqlite",
        workspace_dir=tmp_path,
    )

    assert settings.backend == "postgres"
    assert settings.postgres_dsn == "postgresql://team:secret@db/team_agents_ci"
