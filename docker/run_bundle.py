#!/usr/bin/env python3
"""Start the standalone bundled runtime with local NATS/JetStream and optional Postgres."""

# ruff: noqa: E402

from __future__ import annotations

import argparse
from dataclasses import dataclass
import logging
import os
from pathlib import Path
import shlex
import shutil
import signal
import subprocess
import sys
import time
from typing import Callable, Mapping, Sequence
from urllib.parse import unquote, urlsplit

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from assistant_gateway.config import GatewayInstanceConfig, InstanceConfigFileError
from default_values import TRANSPORT_DEFAULTS
from docker.nats_service import (
    apply_local_nats_env,
    local_nats_enabled_from_env,
    local_nats_url,
    start_local_nats,
)
from docker.python_entrypoints import resolve_python_entrypoint
from docker.launch_headless import (
    RuntimeLayoutError,
    build_process_env,
    ensure_instance_config_ready_for_headless_launch,
    resolve_env_file_path,
    terminate_process,
    use_process_timezone,
    validate_runtime_layout,
)
from env_secrets import PROCESS_POSTGRES, environment_for, materialize_secret_file_envs
from storage.postgres_connection import (
    BUNDLED_POSTGRES_ADDRESS_ENV,
    postgres_dsn_at,
    redact_postgres_dsn,
)


LOGGER = logging.getLogger("docker.run_bundle")
INTERNAL_POSTGRES_HOST = "127.0.0.1"
INTERNAL_POSTGRES_PORT = 5432
POSTGRES_USER = "postgres"
POSTGRES_PM_STATUS_LINE_INDEX = 7
POSTGRES_PM_STATUS_READY = "ready"


@dataclass(frozen=True)
class BundlePostgresTarget:
    dsn: str
    database_name: str
    username: str
    password: str | None


@dataclass(frozen=True)
class BundlePaths:
    service_root: Path
    postgres_root: Path
    postgres_data: Path
    nats_root: Path
    nats_data: Path
    nats_logs: Path


def _bundle_paths() -> BundlePaths:
    service_root = Path(os.getenv("TEAM_BUNDLE_SERVICE_ROOT", "/data/services")).expanduser().resolve()
    postgres_root = service_root / "postgres"
    nats_root = service_root / "nats"
    return BundlePaths(
        service_root=service_root,
        postgres_root=postgres_root,
        postgres_data=postgres_root / "data",
        nats_root=nats_root,
        nats_data=nats_root / "data",
        nats_logs=nats_root / "log",
    )


# Earlier releases ran every process from a copy of instance_config.yaml written
# here at container start. Settings saved while the runtime ran went to the
# saved file, so the copy went stale until the container restarted. Nothing
# reads or writes it any more (owner's decision 2026-09-29).
REMOVED_RUNTIME_CONFIG_COPY = Path("runtime") / "instance_config.bundle.yaml"


def report_removed_runtime_config_copy(service_root: Path) -> None:
    """Tell the operator once per start that an old copy is ignored and may be deleted."""

    old_copy = service_root / REMOVED_RUNTIME_CONFIG_COPY
    if old_copy.exists():
        LOGGER.warning(
            "%s is no longer used: every process reads the saved instance_config.yaml and the "
            "bundle's own NATS and PostgreSQL details are applied in memory. Delete the file; "
            "hand edits in it never reached the saved settings. "
            "event=bundle_runtime_config_copy_ignored path=%s",
            old_copy,
            old_copy,
        )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Launch bundled or external messaging, optional Postgres, manager, and gateway for an onboarded instance."
        ),
    )
    parser.add_argument("--config", required=True, help="Path to the onboarded instance_config.yaml")
    parser.add_argument(
        "--env",
        default=None,
        help="Optional env file to merge before launch. Overrides config.launch.env_file_path.",
    )
    parser.add_argument(
        "--refresh-external-skills",
        action="store_true",
        help="Refresh configured external skill repositories before any agents start.",
    )
    parser.add_argument(
        "--skills-repository",
        action="append",
        default=None,
        help=(
            "Public GitHub repository containing a skills/ directory at any depth. Repeat to override "
            "workspace.external_skill_repositories for this launch."
        ),
    )
    parser.add_argument(
        "gateway_args",
        nargs=argparse.REMAINDER,
        help="Additional arguments forwarded to assistant_cli.py run-gateway",
    )
    return parser


def normalize_postgres_dsn_for_bundle(
    raw_dsn: str,
    *,
    host: str = INTERNAL_POSTGRES_HOST,
    port: int = INTERNAL_POSTGRES_PORT,
) -> str:
    return postgres_dsn_at(raw_dsn, host=host, port=port)


def _bundle_postgres_target(raw_dsn: str) -> BundlePostgresTarget:
    normalized = normalize_postgres_dsn_for_bundle(raw_dsn)
    parts = urlsplit(normalized)
    db_name = parts.path.lstrip("/")
    username = unquote(parts.username or "").strip()
    password = unquote(parts.password or "") if parts.password is not None else None
    if not username:
        raise ValueError(f"Bundle Postgres DSN must include a username: {redact_postgres_dsn(normalized)}")
    if not db_name:
        raise ValueError(f"Bundle Postgres DSN must include a database name: {redact_postgres_dsn(normalized)}")
    return BundlePostgresTarget(
        dsn=normalized,
        database_name=db_name,
        username=username,
        password=password,
    )


def collect_bundle_postgres_targets(
    config: GatewayInstanceConfig,
    *,
    env: Mapping[str, str],
) -> list[BundlePostgresTarget]:
    targets: list[BundlePostgresTarget] = []
    seen: set[str] = set()
    for settings in (
        config.resolve_manager_storage_settings(env=env),
        config.resolve_state_storage_settings(env=env),
    ):
        if settings.backend != "postgres" or not settings.postgres_dsn:
            continue
        target = _bundle_postgres_target(settings.postgres_dsn)
        if target.dsn in seen:
            continue
        seen.add(target.dsn)
        targets.append(target)
    return targets


def bundle_nats_servers(
    config: GatewayInstanceConfig,
    *,
    env: Mapping[str, str],
    bundled_nats_enabled: bool = True,
) -> list[str]:
    """The NATS servers every process uses: the bundle's own, or the operator's external ones."""

    configured = str(env.get("NATS_SERVERS") or "").strip()
    original_servers = (
        [item.strip() for item in configured.split(",") if item.strip()]
        if configured
        else list(config.transport.nats_servers or [])
    )
    original_url = str(
        env.get("NATS_URL") or config.transport.nats_url or TRANSPORT_DEFAULTS.nats_url.value
    ).strip()
    if bundled_nats_enabled:
        if original_servers and original_servers != [local_nats_url()]:
            LOGGER.warning(
                "Bundle runtime normalized NATS servers from %s to %s.",
                original_servers,
                [local_nats_url()],
            )
        elif original_url and original_url != local_nats_url():
            LOGGER.warning("Bundle runtime normalized NATS URL from %s to %s.", original_url, local_nats_url())
        return [local_nats_url()]
    servers = original_servers or [original_url or TRANSPORT_DEFAULTS.nats_url.value]
    if any("127.0.0.1" in server or "localhost" in server for server in servers):
        LOGGER.warning(
            "Local NATS is disabled but the effective NATS endpoint is local (%s). "
            "Ensure an external NATS server is reachable from inside the container or set NATS_SERVERS/NATS_URL.",
            servers,
        )
    LOGGER.info("Bundle runtime is using external NATS endpoint(s) %s.", servers)
    return servers


def _voice_summary(config: GatewayInstanceConfig) -> str:
    return (
        f"enabled={config.voice.enabled} input_enabled={config.voice.input_enabled} "
        f"output_enabled={config.voice.output_enabled} provider={config.voice.provider}"
    )


def _first_value(*values: str | None) -> str:
    return next((str(value).strip() for value in values if str(value or "").strip()), "")


def _bundle_postgres_env(
    config: GatewayInstanceConfig, env: dict[str, str], base_env: Mapping[str, str]
) -> None:
    """Point every process at the bundle's own PostgreSQL, when the settings use PostgreSQL.

    The address travels as ``BUNDLED_POSTGRES_ADDRESS_ENV`` (no secret), which
    the storage resolvers apply in memory to the saved connection strings.
    ``POSTGRES_DSN`` carries the same connection for code that reads it
    directly; ``POSTGRES_DSN_FILE`` is dropped, or the launcher would read the
    operator's original address back from the file (a secret file wins over
    a direct value, env_secrets).
    """

    env.pop("POSTGRES_DSN_FILE", None)
    env.pop(BUNDLED_POSTGRES_ADDRESS_ENV, None)
    if not collect_bundle_postgres_targets(config, env=base_env):
        env.pop("POSTGRES_DSN", None)
        return
    resolving = {**base_env, BUNDLED_POSTGRES_ADDRESS_ENV: f"{INTERNAL_POSTGRES_HOST}:{INTERNAL_POSTGRES_PORT}"}
    manager = config.resolve_manager_storage_settings(env=resolving)
    state = config.resolve_state_storage_settings(env=resolving)
    env[BUNDLED_POSTGRES_ADDRESS_ENV] = resolving[BUNDLED_POSTGRES_ADDRESS_ENV]
    env["POSTGRES_DSN"] = str(
        manager.postgres_dsn if manager.backend == "postgres" else state.postgres_dsn
    )


def build_bundle_runtime_env(
    config: GatewayInstanceConfig,
    *,
    base_env: Mapping[str, str],
    ambient_env: Mapping[str, str] | None = None,
    bundled_nats_enabled: bool = True,
) -> dict[str, str]:
    """The launcher's environment: the container's own plus the bundle's services.

    ``base_env`` is this process's full view (``build_process_env``) and
    decides the bundle's NATS and PostgreSQL details; they are added to
    ``ambient_env``, the environment the container started with. The launcher
    derives every other variable from the saved instance_config.yaml itself,
    so those stay settings it can recompute on an in-app restart
    (``docker.settings_environment``); copying them here would freeze them.
    Every process applies the bundle's details in memory
    (``assistant_gateway.deployment_environment``); no file is written.
    """

    env = dict(base_env if ambient_env is None else ambient_env)
    servers = bundle_nats_servers(config, env=base_env, bundled_nats_enabled=bundled_nats_enabled)
    env["TRANSPORT_TYPE"] = TRANSPORT_DEFAULTS.type.value
    env["NATS_URL"] = servers[0]
    env["NATS_SERVERS"] = ",".join(servers)
    if bundled_nats_enabled:
        env["NATS_JETSTREAM_ENABLED"] = "true"
    # Credentials the operator provides (environment, team.env or a Docker
    # secret file, already read into base_env) win over the config file. Blanking
    # them made the bundled NATS server generate its own password while every
    # child re-read the operator's secret files, so no process could connect.
    env["NATS_USER"] = _first_value(base_env.get("NATS_USER"), config.transport.nats_user)
    env["NATS_PASSWORD"] = _first_value(base_env.get("NATS_PASSWORD"), config.transport.nats_password)
    env["NATS_CREDENTIALS_FILE"] = _first_value(
        base_env.get("NATS_CREDENTIALS_FILE"), config.transport.credentials_file
    )
    _bundle_postgres_env(config, env, base_env)
    return env


def _ensure_directory(path: Path, *, owner: str | None = None) -> None:
    path.mkdir(parents=True, exist_ok=True)
    if owner:
        shutil.chown(path, user=owner, group=owner)


def _ensure_tree_owner(path: Path, *, owner: str) -> None:
    if not path.exists():
        return
    shutil.chown(path, user=owner, group=owner)
    for current_root, dirnames, filenames in os.walk(path):
        current = Path(current_root)
        shutil.chown(current, user=owner, group=owner)
        for name in dirnames:
            shutil.chown(current / name, user=owner, group=owner)
        for name in filenames:
            try:
                shutil.chown(current / name, user=owner, group=owner)
            except FileNotFoundError:
                continue


def _resolve_postgres_binary(name: str) -> str:
    resolved = shutil.which(name)
    if resolved:
        return resolved
    candidates = sorted(Path("/usr/lib/postgresql").glob(f"*/bin/{name}"), reverse=True)
    if candidates:
        return str(candidates[0])
    raise FileNotFoundError(f"Unable to locate PostgreSQL binary: {name}")


def _postgres_environment() -> dict[str, str]:
    """Bundled Postgres and its tools receive none of the runtime's secrets.

    This launcher's own environment holds every secret (it starts the
    headless supervisor with them); docker/secret_env_vars.yaml names no
    secret for the ``postgres`` process.
    """

    return environment_for(PROCESS_POSTGRES, os.environ)


def _run_as_user(
    user: str,
    command: Sequence[str],
    *,
    env: Mapping[str, str] | None = None,
    capture_output: bool = True,
) -> subprocess.CompletedProcess[str]:
    shell_command = " ".join(shlex.quote(part) for part in command)
    return subprocess.run(
        ["su", "-s", "/bin/sh", user, "-c", shell_command],
        check=False,
        cwd=REPO_ROOT,
        env=dict(env) if env is not None else _postgres_environment(),
        capture_output=capture_output,
        text=True,
    )


def _run_checked_as_user(
    user: str,
    command: Sequence[str],
    *,
    env: Mapping[str, str] | None = None,
) -> subprocess.CompletedProcess[str]:
    result = _run_as_user(user, command, env=env)
    if result.returncode == 0:
        return result
    output = "\n".join(part for part in (result.stdout.strip(), result.stderr.strip()) if part).strip()
    rendered = f"\n{output}" if output else ""
    raise RuntimeError(f"Command failed for user {user}: {' '.join(command)}{rendered}")


def _wait_for_ready(
    label: str,
    command: Sequence[str],
    *,
    env: Mapping[str, str] | None = None,
    user: str | None = None,
    process: subprocess.Popen[bytes] | None = None,
    diagnostics: Callable[[], str] | None = None,
    timeout_seconds: float = 60.0,
) -> None:
    deadline = time.time() + timeout_seconds
    next_log_at = time.time() + 10.0
    last_output = ""
    while time.time() < deadline:
        if process is not None:
            code = process.poll()
            if code is not None:
                suffix = f"\n{last_output}" if last_output else ""
                diagnostic_text = diagnostics() if diagnostics is not None else ""
                if diagnostic_text:
                    suffix = f"{suffix}\n{diagnostic_text}"
                raise RuntimeError(f"{label} process exited before becoming ready with code {code}.{suffix}")
        if user:
            result = _run_as_user(user, command, env=env)
        else:
            result = subprocess.run(
                list(command),
                check=False,
                cwd=REPO_ROOT,
                env=dict(env) if env is not None else _postgres_environment(),
                capture_output=True,
                text=True,
            )
        if result.returncode == 0:
            return
        last_output = "\n".join(part for part in (result.stdout.strip(), result.stderr.strip()) if part).strip()
        if time.time() >= next_log_at:
            LOGGER.info("Still waiting for %s to become ready.", label)
            next_log_at = time.time() + 10.0
        time.sleep(1.0)
    suffix = f"\n{last_output}" if last_output else ""
    diagnostic_text = diagnostics() if diagnostics is not None else ""
    if diagnostic_text:
        suffix = f"{suffix}\n{diagnostic_text}"
    raise RuntimeError(f"Timed out waiting for {label} to become ready.{suffix}")


def initialize_postgres_data_dir() -> None:
    paths = _bundle_paths()
    _ensure_directory(paths.postgres_root)
    _ensure_directory(paths.postgres_data)
    _ensure_tree_owner(paths.postgres_root, owner=POSTGRES_USER)
    if any(paths.postgres_data.iterdir()):
        return
    initdb = _resolve_postgres_binary("initdb")
    _run_checked_as_user(
        POSTGRES_USER,
        [
            initdb,
            "-D",
            str(paths.postgres_data),
            "--username",
            POSTGRES_USER,
            "--auth-local",
            "trust",
            "--auth-host",
            "trust",
        ],
    )


def _read_postgres_pm_status(data_dir: Path) -> str | None:
    try:
        lines = (data_dir / "postmaster.pid").read_text(encoding="utf-8", errors="replace").splitlines()
    except FileNotFoundError:
        return None
    except OSError as exc:
        return f"unreadable: {exc}"
    if len(lines) <= POSTGRES_PM_STATUS_LINE_INDEX:
        return None
    return lines[POSTGRES_PM_STATUS_LINE_INDEX].strip() or None


def _wait_for_postgres_pm_ready(
    *,
    process: subprocess.Popen[bytes],
    data_dir: Path,
    timeout_seconds: float = 90.0,
) -> None:
    deadline = time.time() + timeout_seconds
    next_log_at = time.time() + 10.0
    last_status = "postmaster.pid is not present yet"
    while time.time() < deadline:
        code = process.poll()
        if code is not None:
            raise RuntimeError(
                "Postgres process exited before postmaster reported ready "
                f"with code {code}; last postmaster status: {last_status}"
            )
        status = _read_postgres_pm_status(data_dir)
        if status == POSTGRES_PM_STATUS_READY:
            return
        if status:
            last_status = status
        if time.time() >= next_log_at:
            LOGGER.info("Still waiting for Postgres postmaster readiness; status=%s.", last_status)
            next_log_at = time.time() + 10.0
        time.sleep(1.0)
    raise RuntimeError(f"Timed out waiting for Postgres postmaster readiness; last status: {last_status}")


def seed_postgres_from_targets(targets: Sequence[BundlePostgresTarget]) -> None:
    if not targets:
        LOGGER.info("Bundle runtime has no Postgres storage targets; skipping Postgres database seeding.")
        return
    try:
        import psycopg
        from psycopg import sql
    except ImportError as exc:  # pragma: no cover - dependency is expected in normal runtime
        raise RuntimeError("psycopg is required to seed bundled Postgres.") from exc

    admin_dsn = f"postgresql://{POSTGRES_USER}@{INTERNAL_POSTGRES_HOST}:{INTERNAL_POSTGRES_PORT}/postgres"
    with psycopg.connect(admin_dsn, autocommit=True) as conn:
        with conn.cursor() as cur:
            for target in targets:
                cur.execute("SELECT 1 FROM pg_roles WHERE rolname = %s", (target.username,))
                if cur.fetchone() is None:
                    if target.password is None:
                        cur.execute(sql.SQL("CREATE ROLE {} LOGIN").format(sql.Identifier(target.username)))
                    else:
                        cur.execute(
                            sql.SQL("CREATE ROLE {} LOGIN PASSWORD {}").format(
                                sql.Identifier(target.username),
                                sql.Literal(target.password),
                            )
                        )
                elif target.password is not None:
                    cur.execute(
                        sql.SQL("ALTER ROLE {} WITH LOGIN PASSWORD {}").format(
                            sql.Identifier(target.username),
                            sql.Literal(target.password),
                        )
                    )
                cur.execute("SELECT 1 FROM pg_database WHERE datname = %s", (target.database_name,))
                if cur.fetchone() is None:
                    cur.execute(
                        sql.SQL("CREATE DATABASE {} OWNER {}").format(
                            sql.Identifier(target.database_name),
                            sql.Identifier(target.username),
                        )
                    )
                LOGGER.info("Ensuring pgvector extension in %s", redact_postgres_dsn(target.dsn))
                admin_target_dsn = (
                    f"postgresql://{POSTGRES_USER}@{INTERNAL_POSTGRES_HOST}:{INTERNAL_POSTGRES_PORT}/{target.database_name}"
                )
                with psycopg.connect(admin_target_dsn, autocommit=True) as conn:
                    with conn.cursor() as cur:
                        cur.execute("CREATE EXTENSION IF NOT EXISTS vector")
    for target in targets:
        LOGGER.info("Verified bundled Postgres target %s", redact_postgres_dsn(target.dsn))
        with psycopg.connect(target.dsn, autocommit=True) as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT 1")


def start_postgres() -> subprocess.Popen[bytes]:
    initialize_postgres_data_dir()
    paths = _bundle_paths()
    postgres = _resolve_postgres_binary("postgres")
    command = [
        "su",
        "-s",
        "/bin/sh",
        POSTGRES_USER,
        "-c",
        "exec "
        + " ".join(
            shlex.quote(part)
            for part in (
                postgres,
                "-D",
                str(paths.postgres_data),
                "-h",
                INTERNAL_POSTGRES_HOST,
                "-p",
                str(INTERNAL_POSTGRES_PORT),
                "-k",
                "/tmp",
            )
        ),
    ]
    LOGGER.info("Starting bundled Postgres: %s", " ".join(command))
    process = subprocess.Popen(command, cwd=REPO_ROOT, env=_postgres_environment())
    _wait_for_postgres_pm_ready(process=process, data_dir=paths.postgres_data, timeout_seconds=90.0)
    _wait_for_ready(
        "Postgres",
        [
            _resolve_postgres_binary("pg_isready"),
            "-h",
            INTERNAL_POSTGRES_HOST,
            "-p",
            str(INTERNAL_POSTGRES_PORT),
            "-U",
            POSTGRES_USER,
            "-d",
            "postgres",
        ],
        timeout_seconds=90.0,
    )
    return process


def build_launch_headless_command(
    runtime_config_path: Path,
    gateway_args: Sequence[str],
    *,
    env_path: Path | None,
    python_executable: str | None = None,
    refresh_external_skills: bool = False,
    skills_repositories: Sequence[str] = (),
) -> list[str]:
    command = [
        python_executable or sys.executable,
        "-u",
        str(resolve_python_entrypoint(REPO_ROOT / "docker" / "launch_headless.py")),
        "--config",
        str(runtime_config_path),
    ]
    if env_path is not None:
        command.extend(["--env", str(env_path)])
    if refresh_external_skills:
        command.append("--refresh-external-skills")
    for repository in skills_repositories:
        command.extend(["--skills-repository", repository])
    if gateway_args:
        command.extend(["--", *gateway_args])
    return command


def run_supervised_bundle(
    *,
    config: GatewayInstanceConfig,
    runtime_env: Mapping[str, str],
    env_path: Path | None,
    gateway_args: Sequence[str],
    bundled_nats_enabled: bool = True,
    refresh_external_skills: bool = False,
    skills_repositories: Sequence[str] = (),
) -> int:
    postgres_targets = collect_bundle_postgres_targets(config, env=runtime_env)
    postgres = start_postgres() if postgres_targets else None
    nats: subprocess.Popen[bytes] | None = None
    launcher: subprocess.Popen[bytes] | None = None
    stop_requested = False

    def _handle_signal(signum: int, _frame: object) -> None:
        nonlocal stop_requested
        stop_requested = True
        LOGGER.info("Received signal %s; shutting down bundled services.", signum)

    signal.signal(signal.SIGTERM, _handle_signal)
    signal.signal(signal.SIGINT, _handle_signal)

    try:
        if bundled_nats_enabled:
            nats = start_local_nats(env=runtime_env, logger=LOGGER, cwd=REPO_ROOT, service_root=_bundle_paths().nats_root)
        else:
            LOGGER.info("Local NATS is disabled; using configured external NATS endpoint(s).")
        seed_postgres_from_targets(postgres_targets)

        # The launcher and every process read the saved settings file itself.
        launch_command = build_launch_headless_command(
            config.config_path,
            gateway_args,
            env_path=env_path,
            refresh_external_skills=refresh_external_skills,
            skills_repositories=skills_repositories,
        )
        LOGGER.info("Starting bundled headless launcher: %s", " ".join(launch_command))
        launcher = subprocess.Popen(
            launch_command,
            cwd=REPO_ROOT,
            env=dict(runtime_env),
            start_new_session=True,
        )

        process_list: list[tuple[str, subprocess.Popen[bytes]]] = []
        if postgres is not None:
            process_list.append(("postgres", postgres))
        if nats is not None:
            process_list.append(("nats", nats))
        process_list.append(("headless", launcher))
        processes = tuple(process_list)
        exit_name: str | None = None
        exit_code = 0
        while True:
            if stop_requested:
                break
            for name, process in processes:
                code = process.poll()
                if code is not None:
                    exit_name = name
                    exit_code = int(code)
                    LOGGER.warning("%s exited with code %s; stopping bundled runtime.", name, code)
                    stop_requested = True
                    break
            if stop_requested:
                break
            time.sleep(1.0)
        return exit_code if exit_name is not None else 0
    finally:
        if launcher is not None:
            terminate_process(launcher, "bundle-headless")
        if nats is not None:
            terminate_process(nats, "nats")
        if postgres is not None:
            terminate_process(postgres, "postgres")


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    materialize_secret_file_envs()
    args = build_parser().parse_args()

    config_path = Path(args.config).expanduser().resolve()
    if not config_path.exists():
        raise SystemExit(f"No such instance config: {config_path}")

    try:
        ensure_instance_config_ready_for_headless_launch(config_path)
        # Only the saved file: the bundle adds its own details to the launcher's
        # environment below and never writes them into any file.
        config = GatewayInstanceConfig.load_saved(config_path)
    except InstanceConfigFileError as exc:
        LOGGER.error("Bundle launch refused. %s", exc)
        return 2
    try:
        validate_runtime_layout(config)
    except RuntimeLayoutError as exc:
        LOGGER.error("Bundle launch refused. %s", exc)
        return 2
    LOGGER.info("Loaded the saved instance config %s with voice %s.", config.config_path, _voice_summary(config))
    try:
        # The headless launcher this starts reports an ignored team.env once.
        env_path = resolve_env_file_path(config, args.env, report_ignored_env_file=False)
    except ValueError as exc:
        LOGGER.error("Bundle launch refused. %s", exc)
        return 2
    gateway_args = list(args.gateway_args)
    if gateway_args and gateway_args[0] == "--":
        gateway_args = gateway_args[1:]

    ambient_env = dict(os.environ)
    effective_env = build_process_env(config, env_path=env_path)
    use_process_timezone(effective_env)
    bundled_nats_enabled = local_nats_enabled_from_env(effective_env, default=True)
    if bundled_nats_enabled:
        apply_local_nats_env(effective_env)
    report_removed_runtime_config_copy(_bundle_paths().service_root)
    runtime_env = build_bundle_runtime_env(
        config,
        base_env=effective_env,
        ambient_env=ambient_env,
        bundled_nats_enabled=bundled_nats_enabled,
    )
    if bundled_nats_enabled:
        runtime_env["TEAM_LOCAL_NATS_ENABLED"] = "false"
    return run_supervised_bundle(
        config=config,
        runtime_env=runtime_env,
        env_path=env_path,
        gateway_args=gateway_args,
        bundled_nats_enabled=bundled_nats_enabled,
        refresh_external_skills=bool(args.refresh_external_skills),
        skills_repositories=tuple(args.skills_repository or ()),
    )


if __name__ == "__main__":
    raise SystemExit(main())
