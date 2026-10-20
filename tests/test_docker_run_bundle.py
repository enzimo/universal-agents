from __future__ import annotations

import os
import shlex
import logging
from pathlib import Path
import subprocess
import sys
import types

import pytest
import yaml

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import docker.run_bundle as run_bundle_module
from assistant_gateway.config import GatewayInstanceConfig
from storage.postgres_connection import BUNDLED_POSTGRES_ADDRESS_ENV
from docker.nats_service import (
    INTERNAL_NATS_HOST,
    INTERNAL_NATS_PORT,
)
from docker.run_bundle import (
    INTERNAL_POSTGRES_HOST,
    INTERNAL_POSTGRES_PORT,
    BundlePostgresTarget,
    _wait_for_ready,
    build_bundle_runtime_env,
    build_launch_headless_command,
    collect_bundle_postgres_targets,
    normalize_postgres_dsn_for_bundle,
    run_supervised_bundle,
    seed_postgres_from_targets,
    start_postgres,
)


def _make_config(tmp_path: Path, **kwargs: object) -> GatewayInstanceConfig:
    defaults: dict[str, object] = {
        "instance_name": "bundle-test",
        "instance_root": str(tmp_path / "instance"),
        "team_id": "team_bundle",
        "timezone": "America/Chicago",
    }
    defaults.update(kwargs)
    return GatewayInstanceConfig.create(**defaults)


def test_normalize_postgres_dsn_for_bundle_rewrites_host_and_port() -> None:
    raw = "postgresql://team:secret@example.com:6543/team_agents?sslmode=require"

    normalized = normalize_postgres_dsn_for_bundle(raw)

    assert normalized == (
        f"postgresql://team:secret@{INTERNAL_POSTGRES_HOST}:{INTERNAL_POSTGRES_PORT}/team_agents"
        "?sslmode=require"
    )


@pytest.mark.parametrize("dsn_source", ["environment", "hand_edited_file"])
def test_bundle_env_points_every_process_at_the_bundled_nats_and_postgres(
    tmp_path: Path, dsn_source: str
) -> None:
    operator_dsn = "postgresql://team:secret@db.example:5544/team_agents"
    config = _make_config(tmp_path, manager_storage_backend="postgres")
    saved_path = config.save()
    effective_env = {
        "NATS_URL": "nats://nats.external:4223",
        "NATS_SERVERS": "nats://nats.external:4223",
    }
    if dsn_source == "environment":
        effective_env["POSTGRES_DSN"] = operator_dsn
    else:
        # Connection strings are never saved, but an operator may type one in.
        raw = yaml.safe_load(saved_path.read_text(encoding="utf-8"))
        raw["state"]["storage"]["postgres"] = {"dsn": operator_dsn}
        saved_path.write_text(yaml.safe_dump(raw, sort_keys=False), encoding="utf-8")
    saved_bytes = saved_path.read_bytes()
    config = GatewayInstanceConfig.load_saved(saved_path)

    runtime_env = build_bundle_runtime_env(config, base_env=effective_env, ambient_env={})
    loaded = GatewayInstanceConfig.load_for_process(saved_path, env=runtime_env)

    local = f"nats://{INTERNAL_NATS_HOST}:{INTERNAL_NATS_PORT}"
    bundled_dsn = f"postgresql://team:secret@{INTERNAL_POSTGRES_HOST}:{INTERNAL_POSTGRES_PORT}/team_agents"
    assert runtime_env["NATS_URL"] == local and runtime_env["NATS_SERVERS"] == local
    assert runtime_env["NATS_JETSTREAM_ENABLED"] == "true"
    assert runtime_env[BUNDLED_POSTGRES_ADDRESS_ENV] == f"{INTERNAL_POSTGRES_HOST}:{INTERNAL_POSTGRES_PORT}"
    assert runtime_env["POSTGRES_DSN"] == bundled_dsn
    assert loaded.transport.type == "nats"
    assert loaded.transport.nats_servers == [local]
    assert loaded.resolve_manager_storage_settings(env=runtime_env).postgres_dsn == bundled_dsn
    assert loaded.resolve_state_storage_settings(env=runtime_env).postgres_dsn == bundled_dsn
    # Nothing is written: the saved file keeps the operator's own values.
    assert saved_path.read_bytes() == saved_bytes


def test_bundle_env_keeps_external_nats_when_bundled_nats_disabled(
    tmp_path: Path,
) -> None:
    config = _make_config(tmp_path)
    config.transport.type = "nats"
    effective_env = {
        "TRANSPORT_TYPE": "nats",
        "NATS_URL": "nats://nats.operator:4333",
        "NATS_SERVERS": "nats://nats-a.operator:4333,nats://nats-b.operator:4333",
    }

    runtime_env = build_bundle_runtime_env(
        config, base_env=effective_env, bundled_nats_enabled=False
    )

    assert runtime_env["NATS_URL"] == "nats://nats-a.operator:4333"
    assert (
        runtime_env["NATS_SERVERS"]
        == "nats://nats-a.operator:4333,nats://nats-b.operator:4333"
    )
    assert "NATS_JETSTREAM_ENABLED" not in runtime_env
    assert BUNDLED_POSTGRES_ADDRESS_ENV not in runtime_env


def test_bundle_env_adds_only_its_own_details_to_the_container_environment(tmp_path: Path) -> None:
    """Copies of saved settings stay out, so the launcher can recompute them on a restart."""

    from docker.launch_headless import build_process_env

    config = _make_config(tmp_path)
    config.llm.provider = "litellm"
    config.llm.model_id = "old/model"
    ambient = {"PATH": "/usr/bin", "OPERATOR_SETTING": "kept"}
    effective_env = build_process_env(config, env_path=None, base_env=ambient)
    assert effective_env["LLM_MODEL_ID"] == "old/model"

    runtime_env = build_bundle_runtime_env(config, base_env=effective_env, ambient_env=ambient)

    assert "LLM_MODEL_ID" not in runtime_env
    assert runtime_env["OPERATOR_SETTING"] == "kept"


def test_build_launch_headless_command_forwards_external_skill_refresh_flags(
    tmp_path: Path,
    monkeypatch,
) -> None:
    docker_dir = tmp_path / "docker"
    docker_dir.mkdir()
    (docker_dir / "launch_headless.py").write_text("", encoding="utf-8")
    monkeypatch.setattr(run_bundle_module, "REPO_ROOT", tmp_path)

    command = build_launch_headless_command(
        tmp_path / "runtime.yaml",
        [],
        env_path=None,
        refresh_external_skills=True,
        skills_repositories=(
            "https://example.test/one.git",
            "https://example.test/two.git",
        ),
    )

    assert command[-5:] == [
        "--refresh-external-skills",
        "--skills-repository",
        "https://example.test/one.git",
        "--skills-repository",
        "https://example.test/two.git",
    ]


def test_an_old_runtime_config_copy_is_ignored_and_reported(tmp_path: Path, caplog) -> None:
    old_copy = tmp_path / "services" / "runtime" / "instance_config.bundle.yaml"
    old_copy.parent.mkdir(parents=True)
    old_copy.write_text("stale: true\n", encoding="utf-8")

    with caplog.at_level(logging.WARNING, logger="docker.run_bundle"):
        run_bundle_module.report_removed_runtime_config_copy(tmp_path / "services")
        run_bundle_module.report_removed_runtime_config_copy(tmp_path / "elsewhere")

    assert caplog.text.count("event=bundle_runtime_config_copy_ignored") == 1
    assert old_copy.read_text(encoding="utf-8") == "stale: true\n"


def test_collect_bundle_postgres_targets_deduplicates_manager_and_state(
    tmp_path: Path,
) -> None:
    config = _make_config(
        tmp_path,
        manager_storage_backend="postgres",
        manager_storage_postgres_dsn="postgresql://team:secret@db.example:5439/team_agents",
        state_storage_backend="postgres",
        state_storage_postgres_dsn="postgresql://team:secret@db.example:5439/team_agents",
    )

    targets = collect_bundle_postgres_targets(config, env={})

    assert len(targets) == 1
    assert targets[0].database_name == "team_agents"
    assert targets[0].username == "team"
    assert targets[0].password == "secret"
    assert targets[0].dsn == (
        f"postgresql://team:secret@{INTERNAL_POSTGRES_HOST}:{INTERNAL_POSTGRES_PORT}/team_agents"
    )


def test_wait_for_ready_retries_transient_service_startup(monkeypatch) -> None:
    attempts: list[list[str]] = []

    def fake_run_as_user(
        user: str, command: list[str], *, env=None, capture_output=True
    ):
        attempts.append(list(command))
        if len(attempts) == 1:
            return subprocess.CompletedProcess(
                command,
                69,
                stdout="Still booting, will check again in 10 seconds...",
                stderr="Error:\nnats_not_ready",
            )
        return subprocess.CompletedProcess(command, 0, stdout="", stderr="")

    monkeypatch.setattr("docker.run_bundle._run_as_user", fake_run_as_user)
    monkeypatch.setattr("docker.run_bundle.time.sleep", lambda _seconds: None)

    _wait_for_ready(
        "NATS startup",
        ["nats-server", "--signal", "reload"],
        user="nats",
        timeout_seconds=1.0,
    )

    assert attempts == [
        ["nats-server", "--signal", "reload"],
        ["nats-server", "--signal", "reload"],
    ]


def test_wait_for_ready_reports_process_exit_with_diagnostics(monkeypatch) -> None:
    class ExitedProcess:
        def poll(self):
            return 1

    with pytest.raises(RuntimeError) as excinfo:
        _wait_for_ready(
            "NATS",
            ["nats", "server", "check"],
            user=None,
            process=ExitedProcess(),
            diagnostics=lambda: "nats log tail",
            timeout_seconds=1.0,
        )

    message = str(excinfo.value)
    assert "NATS process exited before becoming ready with code 1" in message
    assert "nats log tail" in message


def test_start_postgres_waits_for_postmaster_before_pg_isready(
    tmp_path: Path, monkeypatch
) -> None:
    events: list[str] = []

    class FakePaths:
        postgres_root = tmp_path / "postgres"
        postgres_data = tmp_path / "postgres" / "data"

    class FakeProcess:
        def poll(self):
            return None

    def fake_resolve_binary(name: str) -> str:
        return f"/usr/lib/postgresql/18/bin/{name}"

    def fake_wait_for_postmaster(
        *, process, data_dir: Path, timeout_seconds: float
    ) -> None:
        assert isinstance(process, FakeProcess)
        assert data_dir == FakePaths.postgres_data
        assert timeout_seconds == 90.0
        events.append("postmaster-ready")

    def fake_wait_for_ready(label, command, **kwargs) -> None:
        assert label == "Postgres"
        assert "postmaster-ready" in events
        events.append("pg-isready")

    monkeypatch.setattr("docker.run_bundle.initialize_postgres_data_dir", lambda: None)
    monkeypatch.setattr("docker.run_bundle._bundle_paths", lambda: FakePaths())
    monkeypatch.setattr(
        "docker.run_bundle._resolve_postgres_binary", fake_resolve_binary
    )
    popen_envs: list[dict[str, str]] = []

    def fake_popen(*args, **kwargs):
        popen_envs.append(dict(kwargs["env"]))
        return FakeProcess()

    monkeypatch.setattr("docker.run_bundle.subprocess.Popen", fake_popen)
    monkeypatch.setattr(
        "docker.run_bundle._wait_for_postgres_pm_ready", fake_wait_for_postmaster
    )
    monkeypatch.setattr("docker.run_bundle._wait_for_ready", fake_wait_for_ready)
    monkeypatch.setenv("WEB_CHAT_AUTH_TOKEN", "web")
    monkeypatch.setenv("BW_SESSION_FILE", "/run/secrets/bw")

    process = start_postgres()

    assert isinstance(process, FakeProcess)
    assert events == ["postmaster-ready", "pg-isready"]
    # Postgres receives none of the runtime's secrets.
    assert not {"WEB_CHAT_AUTH_TOKEN", "BW_SESSION_FILE"} & set(popen_envs[0])


def test_start_local_nats_captures_early_server_output(
    tmp_path: Path, monkeypatch
) -> None:
    opened_paths: list[Path] = []
    popen_kwargs: dict[str, object] = {}

    class FakeProcess:
        def poll(self):
            return None

    def fake_popen(command, **kwargs):
        popen_kwargs.update(kwargs)
        stdout = kwargs["stdout"]
        opened_paths.append(Path(stdout.name))
        stdout.write(b"early nats failure\n")
        return FakeProcess()

    monkeypatch.setattr(
        "docker.nats_service.shutil.which", lambda name: "/usr/local/bin/nats-server"
    )
    monkeypatch.setattr(
        "docker.nats_service.subprocess.run",
        lambda *args, **kwargs: subprocess.CompletedProcess(
            args[0], 0, stdout="nats-server: v2.15.1\n", stderr=""
        ),
    )
    monkeypatch.setattr("docker.nats_service.subprocess.Popen", fake_popen)
    monkeypatch.setattr(
        "docker.nats_service._wait_for_nats_ready", lambda *args, **kwargs: None
    )

    from docker.nats_service import start_local_nats

    process = start_local_nats(
        env={},
        logger=types.SimpleNamespace(info=lambda *args: None),
        cwd=Path.cwd(),
        service_root=tmp_path,
    )

    assert isinstance(process, FakeProcess)
    assert opened_paths == [tmp_path / "log" / "nats-server-startup.log"]
    assert popen_kwargs["stderr"] == subprocess.STDOUT
    assert popen_kwargs["start_new_session"] is True
    assert (tmp_path / "log" / "nats-server-startup.log").read_text(
        encoding="utf-8"
    ) == "early nats failure\n"


def test_start_local_nats_uses_configured_auth_without_logging_password(
    tmp_path: Path, monkeypatch
) -> None:
    popen_command: list[str] = []
    popen_env: dict[str, str] = {}
    log_messages: list[str] = []

    class FakeProcess:
        def poll(self):
            return None

    def fake_popen(command, **kwargs):
        popen_command.extend(command)
        popen_env.update(kwargs["env"])
        return FakeProcess()

    class FakeLogger:
        def info(self, message, *args):
            log_messages.append(message % args if args else message)

        def warning(self, message, *args):
            log_messages.append(message % args if args else message)

    monkeypatch.setattr(
        "docker.nats_service.shutil.which", lambda name: "/usr/local/bin/nats-server"
    )
    monkeypatch.setattr(
        "docker.nats_service.subprocess.run",
        lambda *args, **kwargs: subprocess.CompletedProcess(
            args[0], 0, stdout="nats-server: v2.15.1\n", stderr=""
        ),
    )
    monkeypatch.setattr("docker.nats_service.subprocess.Popen", fake_popen)
    monkeypatch.setattr(
        "docker.nats_service._wait_for_nats_ready", lambda *args, **kwargs: None
    )

    from docker.nats_service import start_local_nats

    process = start_local_nats(
        env={"NATS_USER": "team", "NATS_PASSWORD": "super-secret", "WEB_CHAT_AUTH_TOKEN": "web", "PATH": "/usr/bin"},
        logger=FakeLogger(),
        cwd=Path.cwd(),
        service_root=tmp_path,
    )

    assert isinstance(process, FakeProcess)
    # Any process can read another process's command line, so the password
    # travels only through the private server configuration file.
    assert "super-secret" not in popen_command
    # nats-server reads the password from its file and gets no runtime secret.
    assert popen_env == {"PATH": "/usr/bin"}
    config_path = Path(popen_command[popen_command.index("-c") + 1])
    assert config_path == tmp_path / "auth" / "nats-server.conf"
    assert '"super-secret"' in config_path.read_text(encoding="utf-8")
    assert config_path.stat().st_mode & 0o777 == 0o600
    assert (tmp_path / "auth").stat().st_mode & 0o777 == 0o700
    rendered_logs = "\n".join(log_messages)
    assert "super-secret" not in rendered_logs


def test_start_local_nats_generates_and_reuses_credentials(tmp_path: Path, monkeypatch) -> None:
    popen_commands: list[list[str]] = []

    class FakeProcess:
        def poll(self):
            return None

    monkeypatch.setattr(
        "docker.nats_service.shutil.which", lambda name: "/usr/local/bin/nats-server"
    )
    monkeypatch.setattr(
        "docker.nats_service.subprocess.run",
        lambda *args, **kwargs: subprocess.CompletedProcess(args[0], 0, stdout="", stderr=""),
    )
    monkeypatch.setattr(
        "docker.nats_service.subprocess.Popen",
        lambda command, **kwargs: popen_commands.append(list(command)) or FakeProcess(),
    )
    monkeypatch.setattr("docker.nats_service._wait_for_nats_ready", lambda *args, **kwargs: None)

    from docker.nats_service import ensure_local_nats_credentials, start_local_nats

    first_env = {"NATS_USER": "", "NATS_PASSWORD": ""}
    start_local_nats(env=first_env, logger=logging.getLogger("test"), cwd=Path.cwd(), service_root=tmp_path)
    assert first_env["NATS_USER"] == "ua-runtime"
    assert len(first_env["NATS_PASSWORD"]) >= 32
    credentials = tmp_path / "auth" / "credentials.json"
    assert credentials.stat().st_mode & 0o777 == 0o600
    assert first_env["NATS_PASSWORD"] not in popen_commands[0]

    second_env: dict[str, str] = {}
    start_local_nats(env=second_env, logger=logging.getLogger("test"), cwd=Path.cwd(), service_root=tmp_path)
    assert second_env == {"NATS_USER": "ua-runtime", "NATS_PASSWORD": first_env["NATS_PASSWORD"]}

    client_env: dict[str, str] = {}
    ensure_local_nats_credentials(client_env, service_root=tmp_path, create=False)
    assert client_env["NATS_PASSWORD"] == first_env["NATS_PASSWORD"]

    missing_env: dict[str, str] = {}
    ensure_local_nats_credentials(missing_env, service_root=tmp_path / "other", create=False)
    assert missing_env == {}
    assert not (tmp_path / "other").exists()


def test_start_local_nats_rejects_partial_auth(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(
        "docker.nats_service.shutil.which", lambda name: "/usr/local/bin/nats-server"
    )
    monkeypatch.setattr(
        "docker.nats_service.subprocess.run",
        lambda *args, **kwargs: subprocess.CompletedProcess(
            args[0], 0, stdout="nats-server: v2.15.1\n", stderr=""
        ),
    )

    from docker.nats_service import start_local_nats

    with pytest.raises(RuntimeError, match="requires both NATS_USER and NATS_PASSWORD"):
        start_local_nats(
            env={"NATS_PASSWORD": "super-secret"},
            logger=types.SimpleNamespace(
                info=lambda *args: None, warning=lambda *args: None
            ),
            cwd=Path.cwd(),
            service_root=tmp_path,
        )


def test_seed_postgres_from_targets_creates_role_with_literal_password(
    monkeypatch,
    caplog,
) -> None:
    import psycopg

    admin_queries: list[tuple[object, object]] = []
    target_queries: list[tuple[object, object]] = []
    connect_dsns: list[str] = []

    class FakeSqlFragment:
        def __init__(self, template: str):
            self.template = template

        def format(self, *parts):
            rendered = self.template
            for part in parts:
                rendered = rendered.replace("{}", str(part), 1)
            return rendered

    class FakeSqlModule:
        @staticmethod
        def SQL(template: str):
            return FakeSqlFragment(template)

        @staticmethod
        def Identifier(value: str):
            return f'"{value}"'

        @staticmethod
        def Literal(value: str):
            escaped = str(value).replace("'", "''")
            return f"'{escaped}'"

    class FakeCursor:
        def __init__(self, store: list[tuple[object, object]]):
            self._store = store
            self._last_query = None
            self._last_params = None

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def execute(self, query, params=None):
            self._store.append((query, params))
            self._last_query = query
            self._last_params = params

        def fetchone(self):
            if self._last_params and self._last_params[0] in {"team", "team_agents"}:
                return None
            return None

    class FakeConnection:
        def __init__(self, store: list[tuple[object, object]]):
            self._store = store

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def cursor(self):
            return FakeCursor(self._store)

    def fake_connect(dsn, autocommit=True):
        connect_dsns.append(dsn)
        if dsn.endswith("/postgres"):
            return FakeConnection(admin_queries)
        return FakeConnection(target_queries)

    # Keep the real connection-string parser in the seeding log path.
    monkeypatch.setattr(psycopg, "connect", fake_connect)
    monkeypatch.setattr(psycopg, "sql", FakeSqlModule())
    caplog.set_level(logging.INFO, logger="docker.run_bundle")

    seed_postgres_from_targets(
        [
            BundlePostgresTarget(
                dsn=f"postgresql://team:secret@{INTERNAL_POSTGRES_HOST}:{INTERNAL_POSTGRES_PORT}/team_agents",
                database_name="team_agents",
                username="team",
                password="secret",
            )
        ]
    )

    rendered_admin_queries = [str(query) for query, _params in admin_queries]
    assert "CREATE ROLE \"team\" LOGIN PASSWORD 'secret'" in rendered_admin_queries
    assert 'CREATE DATABASE "team_agents" OWNER "team"' in rendered_admin_queries
    assert "postgresql://postgres@127.0.0.1:5432/team_agents" in connect_dsns
    rendered_admin_target_queries = [str(query) for query, _params in target_queries]
    assert "CREATE EXTENSION IF NOT EXISTS vector" in rendered_admin_target_queries
    assert all(
        "ALTER EXTENSION" not in query for query in rendered_admin_target_queries
    )
    assert "secret" not in caplog.text
    assert "password=***" in caplog.text


def test_run_supervised_bundle_skips_postgres_for_sqlite_runtime(
    tmp_path: Path, monkeypatch
) -> None:
    config = _make_config(tmp_path)
    events: list[str] = []
    monkeypatch.setenv("TEAM_BUNDLE_SERVICE_ROOT", str(tmp_path / "services"))

    class FakeProcess:
        def __init__(self, name: str):
            self.name = name
            self.terminated = False

        def poll(self):
            if self.terminated:
                return 0
            if self.name == "headless":
                return 0
            return None

        def terminate(self) -> None:
            events.append(f"terminate:{self.name}")
            self.terminated = True

        def kill(self) -> None:
            self.terminated = True

        def wait(self, timeout=None):
            return 0

    def fail_start_postgres():
        raise AssertionError(
            "Postgres should not start for an all-sqlite bundle runtime"
        )

    def fake_popen(command, **_kwargs):
        assert command == ["headless"]
        return FakeProcess("headless")

    seeded_targets: list[list[BundlePostgresTarget]] = []

    monkeypatch.setattr("docker.run_bundle.start_postgres", fail_start_postgres)
    monkeypatch.setattr(
        "docker.run_bundle.start_local_nats", lambda **kwargs: FakeProcess("nats")
    )
    monkeypatch.setattr(
        "docker.run_bundle.seed_postgres_from_targets",
        lambda targets: seeded_targets.append(list(targets)),
    )
    monkeypatch.setattr(
        "docker.run_bundle.build_launch_headless_command",
        lambda *args, **kwargs: ["headless"],
    )
    monkeypatch.setattr("docker.run_bundle.subprocess.Popen", fake_popen)
    monkeypatch.setattr(
        "docker.run_bundle.terminate_process", lambda process, name: process.terminate()
    )
    monkeypatch.setattr("docker.run_bundle.time.sleep", lambda _seconds: None)

    exit_code = run_supervised_bundle(
        config=config,
        runtime_env={},
        env_path=None,
        gateway_args=[],
    )

    assert exit_code == 0
    assert seeded_targets == [[]]
    assert "terminate:nats" in events


def test_run_supervised_bundle_skips_local_nats_when_disabled(
    tmp_path: Path, monkeypatch
) -> None:
    config = _make_config(tmp_path)
    events: list[str] = []

    class FakeProcess:
        def __init__(self, name: str):
            self.name = name
            self.terminated = False

        def poll(self):
            if self.terminated:
                return 0
            if self.name == "headless":
                return 0
            return None

        def terminate(self) -> None:
            events.append(f"terminate:{self.name}")
            self.terminated = True

        def kill(self) -> None:
            self.terminated = True

        def wait(self, timeout=None):
            return 0

    def fail_start_local_nats(**kwargs):
        raise AssertionError("NATS should not start when local NATS is disabled")

    seeded_targets: list[list[BundlePostgresTarget]] = []

    monkeypatch.setattr("docker.run_bundle.start_postgres", lambda: None)
    monkeypatch.setattr("docker.run_bundle.start_local_nats", fail_start_local_nats)
    monkeypatch.setattr(
        "docker.run_bundle.seed_postgres_from_targets",
        lambda targets: seeded_targets.append(list(targets)),
    )
    monkeypatch.setattr(
        "docker.run_bundle.build_launch_headless_command",
        lambda *args, **kwargs: ["headless"],
    )
    monkeypatch.setattr(
        "docker.run_bundle.subprocess.Popen",
        lambda command, **_kwargs: FakeProcess("headless"),
    )
    monkeypatch.setattr(
        "docker.run_bundle.terminate_process", lambda process, name: process.terminate()
    )
    monkeypatch.setattr("docker.run_bundle.time.sleep", lambda _seconds: None)

    exit_code = run_supervised_bundle(
        config=config,
        runtime_env={},
        env_path=None,
        gateway_args=[],
        bundled_nats_enabled=False,
    )

    assert exit_code == 0
    assert seeded_targets == [[]]


def test_run_supervised_bundle_starts_local_nats(tmp_path: Path, monkeypatch) -> None:
    config = _make_config(tmp_path)
    config.transport.type = "nats"
    events: list[str] = []
    headless_popen_kwargs: dict[str, object] = {}

    class FakeProcess:
        def __init__(self, name: str):
            self.name = name
            self.terminated = False

        def poll(self):
            if self.terminated:
                return 0
            if self.name == "headless":
                return 0
            return None

        def terminate(self) -> None:
            events.append(f"terminate:{self.name}")
            self.terminated = True

        def kill(self) -> None:
            self.terminated = True

        def wait(self, timeout=None):
            return 0

    monkeypatch.setattr("docker.run_bundle.start_postgres", lambda: None)
    monkeypatch.setattr(
        "docker.run_bundle.start_local_nats", lambda **kwargs: FakeProcess("nats")
    )
    monkeypatch.setattr(
        "docker.run_bundle.seed_postgres_from_targets",
        lambda targets: events.append("seed:postgres"),
    )
    monkeypatch.setattr(
        "docker.run_bundle.build_launch_headless_command",
        lambda *args, **kwargs: ["headless"],
    )

    def fake_popen(command, **kwargs):
        headless_popen_kwargs.update(kwargs)
        return FakeProcess("headless")

    monkeypatch.setattr("docker.run_bundle.subprocess.Popen", fake_popen)
    monkeypatch.setattr(
        "docker.run_bundle.terminate_process", lambda process, name: process.terminate()
    )
    monkeypatch.setattr("docker.run_bundle.time.sleep", lambda _seconds: None)

    exit_code = run_supervised_bundle(
        config=config,
        runtime_env={},
        env_path=None,
        gateway_args=[],
    )

    assert exit_code == 0
    assert "seed:postgres" in events
    assert "terminate:nats" in events
    assert headless_popen_kwargs["start_new_session"] is True


def test_run_supervised_bundle_uses_external_nats_when_bundled_nats_disabled(
    tmp_path: Path, monkeypatch
) -> None:
    config = _make_config(tmp_path)
    config.transport.type = "nats"
    events: list[str] = []

    class FakeProcess:
        def __init__(self, name: str):
            self.name = name
            self.terminated = False

        def poll(self):
            if self.terminated:
                return 0
            if self.name == "headless":
                return 0
            return None

        def terminate(self) -> None:
            events.append(f"terminate:{self.name}")
            self.terminated = True

        def kill(self) -> None:
            self.terminated = True

        def wait(self, timeout=None):
            return 0

    def fail_start_local_nats(**kwargs):
        raise AssertionError("NATS should not start when bundled NATS is disabled")

    monkeypatch.setattr("docker.run_bundle.start_postgres", lambda: None)
    monkeypatch.setattr("docker.run_bundle.start_local_nats", fail_start_local_nats)
    monkeypatch.setattr(
        "docker.run_bundle.seed_postgres_from_targets",
        lambda targets: events.append("seed:postgres"),
    )
    monkeypatch.setattr(
        "docker.run_bundle.build_launch_headless_command",
        lambda *args, **kwargs: ["headless"],
    )
    monkeypatch.setattr(
        "docker.run_bundle.subprocess.Popen",
        lambda command, **_kwargs: FakeProcess("headless"),
    )
    monkeypatch.setattr(
        "docker.run_bundle.terminate_process", lambda process, name: process.terminate()
    )
    monkeypatch.setattr("docker.run_bundle.time.sleep", lambda _seconds: None)

    exit_code = run_supervised_bundle(
        config=config,
        runtime_env={},
        env_path=None,
        gateway_args=[],
        bundled_nats_enabled=False,
    )

    assert exit_code == 0
    assert "seed:postgres" in events


def test_entrypoint_routes_bundle_run_to_run_bundle(tmp_path: Path) -> None:
    app_root = tmp_path / "app"
    (app_root / "docker").mkdir(parents=True)
    (app_root / "assistant_cli.py").write_text(
        "print('assistant stub')\n", encoding="utf-8"
    )
    (app_root / "docker" / "run_bundle.py").write_text(
        "print('bundle stub')\n", encoding="utf-8"
    )
    capture_path = tmp_path / "python_args.txt"
    python_dir = tmp_path / "bin"
    python_dir.mkdir()
    (python_dir / "python").write_text(
        '#!/bin/sh\nprintf \'%s\n\' "$@" > "$CAPTURE_FILE"\n',
        encoding="utf-8",
    )
    subprocess.run(["chmod", "+x", str(python_dir / "python")], check=True)

    instance_root = tmp_path / "instance"
    instance_root.mkdir()
    (instance_root / "instance_config.yaml").write_text(
        "instance_name: test\nschema_version: 1\n", encoding="utf-8"
    )

    result = subprocess.run(
        ["sh", str(Path.cwd() / "docker" / "entrypoint.sh"), "run", "--no-apps"],
        cwd=Path.cwd(),
        env={
            "PATH": f"{python_dir}:{os.environ['PATH']}",
            "CAPTURE_FILE": str(capture_path),
            "TEAM_APP_ROOT": str(app_root),
            "TEAM_DOCKER_IMAGE_MODE": "bundle",
            "TEAM_INSTANCE_ROOT": str(instance_root),
        },
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0
    captured_args = capture_path.read_text(encoding="utf-8").splitlines()
    assert captured_args[0] == str(app_root / "docker" / "run_bundle.py")
    assert captured_args[1] == "--config"
    assert captured_args[2] == str((instance_root / "instance_config.yaml").resolve())
    assert captured_args[-1] == "--no-apps"


def test_entrypoint_routes_bundle_run_to_compiled_run_bundle_when_source_is_stripped(
    tmp_path: Path,
) -> None:
    app_root = tmp_path / "app"
    (app_root / "docker").mkdir(parents=True)
    (app_root / "docker" / "run_bundle.pyc").write_bytes(b"compiled bundle")
    capture_path = tmp_path / "python_args.txt"
    python_dir = tmp_path / "bin"
    python_dir.mkdir()
    (python_dir / "python").write_text(
        '#!/bin/sh\nprintf \'%s\n\' "$@" > "$CAPTURE_FILE"\n',
        encoding="utf-8",
    )
    subprocess.run(["chmod", "+x", str(python_dir / "python")], check=True)

    instance_root = tmp_path / "instance"
    instance_root.mkdir()
    (instance_root / "instance_config.yaml").write_text(
        "instance_name: test\nschema_version: 1\n", encoding="utf-8"
    )

    result = subprocess.run(
        ["sh", str(Path.cwd() / "docker" / "entrypoint.sh"), "run", "--no-apps"],
        cwd=Path.cwd(),
        env={
            "PATH": f"{python_dir}:{os.environ['PATH']}",
            "CAPTURE_FILE": str(capture_path),
            "TEAM_APP_ROOT": str(app_root),
            "TEAM_DOCKER_IMAGE_MODE": "bundle",
            "TEAM_INSTANCE_ROOT": str(instance_root),
        },
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0
    captured_args = capture_path.read_text(encoding="utf-8").splitlines()
    assert captured_args[0] == str(app_root / "docker" / "run_bundle.pyc")
    assert captured_args[-1] == "--no-apps"


def _run_entrypoint_with_secret_env(tmp_path: Path, secret_env: dict[str, str]):
    """Run the entrypoint with a stub launcher that records the secret variables it received.

    The stub ``python`` hands the entrypoint's secret check to the real
    interpreter, so the check runs for real, and records the environment of
    the launcher call that follows.
    """

    app_root = tmp_path / "app"
    (app_root / "docker").mkdir(parents=True)
    (app_root / "docker" / "launch_headless.py").write_text("", encoding="utf-8")
    python_dir = tmp_path / "bin"
    python_dir.mkdir()
    (python_dir / "python").write_text(
        "#!/bin/sh\n"
        'case "$1" in */env_secrets.py) exec ' + shlex.quote(sys.executable) + ' "$@" ;; esac\n'
        'printf \'%s|%s|%s|%s\' "${WEB_CHAT_AUTH_TOKEN-unset}" "${WEB_CHAT_AUTH_TOKEN_FILE-unset}" '
        '"${OPENAI_API_KEY-unset}" "${OPENAI_API_KEY_FILE-unset}" '
        '> "$CAPTURE_FILE"\n',
        encoding="utf-8",
    )
    subprocess.run(["chmod", "+x", str(python_dir / "python")], check=True)
    instance_root = tmp_path / "instance"
    instance_root.mkdir()
    (instance_root / "instance_config.yaml").write_text(
        "instance_name: test\nschema_version: 1\n", encoding="utf-8"
    )
    capture_path = tmp_path / "captured.txt"
    result = subprocess.run(
        ["sh", str(Path.cwd() / "docker" / "entrypoint.sh"), "run", "--no-apps"],
        cwd=Path.cwd(),
        env={
            "PATH": f"{python_dir}:{os.environ['PATH']}",
            "CAPTURE_FILE": str(capture_path),
            "TEAM_APP_ROOT": str(app_root),
            "TEAM_DOCKER_IMAGE_MODE": "standard",
            "TEAM_INSTANCE_ROOT": str(instance_root),
            **secret_env,
        },
        capture_output=True,
        text=True,
        check=False,
    )
    return result, capture_path


def test_entrypoint_leaves_secret_files_to_the_python_launcher(tmp_path: Path) -> None:
    """The launcher reads the files itself (env_secrets.materialize_secret_file_envs),
    so the entrypoint passes every <NAME>_FILE through unchanged."""

    web_chat_secret = tmp_path / "web_chat_token"
    web_chat_secret.write_text("web-chat-from-secret\n", encoding="utf-8")
    model_key_secret = tmp_path / "openai_api_key"
    model_key_secret.write_text("sk-from-secret\n", encoding="utf-8")

    result, capture_path = _run_entrypoint_with_secret_env(
        tmp_path,
        {
            "WEB_CHAT_AUTH_TOKEN_FILE": str(web_chat_secret),
            "OPENAI_API_KEY_FILE": str(model_key_secret),
        },
    )

    assert result.returncode == 0, result.stderr
    assert capture_path.read_text(encoding="utf-8") == (
        f"unset|{web_chat_secret}|unset|{model_key_secret}"
    )


def test_entrypoint_refuses_a_secret_set_both_directly_and_as_a_file(tmp_path: Path) -> None:
    web_chat_secret = tmp_path / "web_chat_token"
    web_chat_secret.write_text("from-secret\n", encoding="utf-8")

    result, capture_path = _run_entrypoint_with_secret_env(
        tmp_path,
        {
            "WEB_CHAT_AUTH_TOKEN": "direct",
            "WEB_CHAT_AUTH_TOKEN_FILE": str(web_chat_secret),
        },
    )

    assert result.returncode == 2
    assert "Both WEB_CHAT_AUTH_TOKEN and WEB_CHAT_AUTH_TOKEN_FILE are set" in result.stderr
    assert "direct" not in result.stderr
    assert not capture_path.exists()


def test_entrypoint_refuses_a_secret_file_that_does_not_exist(tmp_path: Path) -> None:
    missing = tmp_path / "missing.json"

    result, capture_path = _run_entrypoint_with_secret_env(
        tmp_path, {"OPENAI_API_KEY_FILE": str(missing)}
    )

    assert result.returncode == 2
    assert f"OPENAI_API_KEY_FILE points to a missing file: {missing}" in result.stderr
    assert not capture_path.exists()


def test_entrypoint_defaults_to_data_root_instance_config(tmp_path: Path) -> None:
    app_root = tmp_path / "app"
    (app_root / "docker").mkdir(parents=True)
    (app_root / "docker" / "run_bundle.py").write_text(
        "print('bundle stub')\n", encoding="utf-8"
    )
    capture_path = tmp_path / "python_args.txt"
    python_dir = tmp_path / "bin"
    python_dir.mkdir()
    (python_dir / "python").write_text(
        '#!/bin/sh\nprintf \'%s\n\' "$@" > "$CAPTURE_FILE"\n',
        encoding="utf-8",
    )
    subprocess.run(["chmod", "+x", str(python_dir / "python")], check=True)

    data_root = tmp_path / "data"
    data_root.mkdir(parents=True)
    (data_root / "instance_config.yaml").write_text(
        "instance_name: default\nschema_version: 1\n",
        encoding="utf-8",
    )

    result = subprocess.run(
        ["sh", str(Path.cwd() / "docker" / "entrypoint.sh"), "run", "--no-apps"],
        cwd=Path.cwd(),
        env={
            "PATH": f"{python_dir}:{os.environ['PATH']}",
            "CAPTURE_FILE": str(capture_path),
            "TEAM_APP_ROOT": str(app_root),
            "TEAM_DOCKER_IMAGE_MODE": "bundle",
            "TEAM_DATA_ROOT": str(data_root),
        },
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0
    captured_args = capture_path.read_text(encoding="utf-8").splitlines()
    assert captured_args[0] == str(app_root / "docker" / "run_bundle.py")
    assert captured_args[1] == "--config"
    assert captured_args[2] == str((data_root / "instance_config.yaml").resolve())
    assert (
        f"Using instance config: {data_root / 'instance_config.yaml'}" in result.stderr
    )


def test_dockerfiles_do_not_bake_legacy_instance_root() -> None:
    assert "TEAM_INSTANCE_ROOT=/data/instance" not in Path("Dockerfile").read_text(
        encoding="utf-8"
    )
    assert "TEAM_INSTANCE_ROOT=/data/instance" not in Path(
        "Dockerfile.bundle"
    ).read_text(encoding="utf-8")
    assert "TEAM_INSTANCE_ROOT=/data/instance" not in Path(
        "docker-compose.yml"
    ).read_text(encoding="utf-8")


def test_docker_compose_forwards_external_skill_refresh_override() -> None:
    compose = Path("docker-compose.yml").read_text(encoding="utf-8")
    assert "TEAM_REFRESH_EXTERNAL_SKILLS=${TEAM_REFRESH_EXTERNAL_SKILLS:-}" in compose


def test_docker_compose_pins_and_secures_web_service_data_planes() -> None:
    compose = yaml.safe_load(Path("docker-compose.yml").read_text(encoding="utf-8"))
    crawl4ai = compose["services"]["crawl4ai"]
    ua = compose["services"]["ua"]

    assert crawl4ai["image"] == "unclecode/crawl4ai:0.9.2"
    assert crawl4ai["secrets"] == [
        {"source": "crawl4ai_api_token", "target": "api_token"}
    ]
    # Universal Agents keeps the token in its credential store (/link), so the
    # ua service gets neither the secret file nor the removed switches.
    assert "secrets" not in ua
    assert not [item for item in ua["environment"] if item.startswith(("CRAWL4AI_API_TOKEN", "CRAWL4AI_EXECUTE_JS"))]
    assert "CRAWL4AI_MCP_SERVER_URL=http://crawl4ai:11235/mcp/sse" in ua["environment"]
    assert "command" not in crawl4ai

    searxng_settings = yaml.safe_load(
        Path("deploy/searxng/settings.yml").read_text(encoding="utf-8")
    )
    assert searxng_settings["search"]["formats"] == ["html", "json", "csv", "rss"]


def test_entrypoint_routes_bundle_run_via_instance_root_hint(tmp_path: Path) -> None:
    app_root = tmp_path / "app"
    (app_root / "docker").mkdir(parents=True)
    (app_root / "assistant_cli.py").write_text(
        "print('assistant stub')\n", encoding="utf-8"
    )
    (app_root / "docker" / "run_bundle.py").write_text(
        "print('bundle stub')\n", encoding="utf-8"
    )
    capture_path = tmp_path / "python_args.txt"
    python_dir = tmp_path / "bin"
    python_dir.mkdir()
    (python_dir / "python").write_text(
        '#!/bin/sh\nprintf \'%s\n\' "$@" > "$CAPTURE_FILE"\n',
        encoding="utf-8",
    )
    subprocess.run(["chmod", "+x", str(python_dir / "python")], check=True)

    data_root = tmp_path / "data"
    instance_root = data_root / "custom-instance"
    instance_root.mkdir(parents=True)
    (instance_root / "instance_config.yaml").write_text(
        "instance_name: test\nschema_version: 1\n", encoding="utf-8"
    )
    (data_root / ".team-instance-root").write_text(
        f"{instance_root}\n", encoding="utf-8"
    )

    result = subprocess.run(
        ["sh", str(Path.cwd() / "docker" / "entrypoint.sh"), "run", "--no-apps"],
        cwd=Path.cwd(),
        env={
            "PATH": f"{python_dir}:{os.environ['PATH']}",
            "CAPTURE_FILE": str(capture_path),
            "TEAM_APP_ROOT": str(app_root),
            "TEAM_DOCKER_IMAGE_MODE": "bundle",
            "TEAM_DATA_ROOT": str(data_root),
        },
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0
    captured_args = capture_path.read_text(encoding="utf-8").splitlines()
    assert captured_args[0] == str(app_root / "docker" / "run_bundle.py")
    assert captured_args[1] == "--config"
    assert captured_args[2] == str((instance_root / "instance_config.yaml").resolve())
    assert captured_args[-1] == "--no-apps"


@pytest.mark.parametrize("how", ["secret_files", "direct", "none"])
def test_bundled_nats_server_and_runtime_processes_use_the_same_credentials(tmp_path: Path, how: str) -> None:
    """Operator credentials (secret files or plain variables) reach both the bundled NATS server and
    every process the headless launcher starts; without any, both use the generated pair."""

    from docker.launch_headless import build_process_env
    from docker.nats_service import apply_local_nats_env, ensure_local_nats_credentials

    container_env: dict[str, str] = {}
    if how == "secret_files":
        (tmp_path / "user").write_text("operator-user\n", encoding="utf-8")
        (tmp_path / "pass").write_text("operator-pass\n", encoding="utf-8")
        container_env = {"NATS_USER_FILE": str(tmp_path / "user"), "NATS_PASSWORD_FILE": str(tmp_path / "pass")}
    elif how == "direct":
        container_env = {"NATS_USER": "operator-user", "NATS_PASSWORD": "operator-pass"}
    config = _make_config(tmp_path)

    effective_env = build_process_env(config, env_path=None, base_env=container_env)
    apply_local_nats_env(effective_env)
    runtime_env = build_bundle_runtime_env(config, base_env=effective_env, ambient_env=container_env)
    ensure_local_nats_credentials(runtime_env, service_root=tmp_path / "nats")  # what start_local_nats does
    child_env = build_process_env(config, env_path=None, base_env=dict(runtime_env))

    server = (runtime_env["NATS_USER"], runtime_env["NATS_PASSWORD"])
    assert (child_env["NATS_USER"], child_env["NATS_PASSWORD"]) == server
    assert server[0] == ("ua-runtime" if how == "none" else "operator-user")


def test_bundled_postgres_address_is_not_replaced_by_a_secret_file(tmp_path: Path) -> None:
    """The bundle's own PostgreSQL address reaches the launcher's processes even when the operator's
    connection string came from POSTGRES_DSN_FILE."""

    from docker.launch_headless import build_process_env

    (tmp_path / "dsn").write_text("postgresql://team:secret@db.example:5544/team_agents\n", encoding="utf-8")
    config = _make_config(
        tmp_path,
        manager_storage_backend="postgres",
        manager_storage_postgres_dsn="postgresql://team:secret@db.example:5544/team_agents",
    )
    effective_env = build_process_env(config, env_path=None, base_env={"POSTGRES_DSN_FILE": str(tmp_path / "dsn")})
    runtime_env = build_bundle_runtime_env(
        config, base_env=effective_env, ambient_env={"POSTGRES_DSN_FILE": str(tmp_path / "dsn")}
    )
    child_env = build_process_env(config, env_path=None, base_env=dict(runtime_env))

    assert "POSTGRES_DSN_FILE" not in runtime_env
    assert f"{INTERNAL_POSTGRES_HOST}:{INTERNAL_POSTGRES_PORT}" in child_env["POSTGRES_DSN"]
