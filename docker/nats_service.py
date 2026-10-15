"""Helpers for supervising the local Docker NATS/JetStream service.

The local server always requires a user and password. NATS carries structured
agent, gateway, and control messages, so any process that can connect without
credentials could impersonate a runtime component. Agent-run code executes in
a sandbox that cannot read the runtime's environment or files outside the
workspace; authentication makes that isolation extend to the message bus.

When the operator configures no NATS_USER/NATS_PASSWORD, the first start
generates credentials and stores them in ``<service_root>/auth`` (mode 0600),
which later starts and maintenance commands reuse. The password reaches
``nats-server`` through a private configuration file, never its command line,
because every process can read another process's command line. For the same
reason ``nats-server`` receives none of the runtime's secrets in its
environment (``env_secrets.environment_for``, process ``nats``).
"""

from __future__ import annotations

import json
import logging
import os
from pathlib import Path
import secrets
import shutil
import socket
import subprocess
import time
from typing import Mapping, MutableMapping

from env_secrets import PROCESS_NATS, environment_for


INTERNAL_NATS_HOST = "127.0.0.1"
INTERNAL_NATS_PORT = 4222
INTERNAL_NATS_MONITOR_PORT = 8222
LOCAL_NATS_ENABLED_ENV = "TEAM_LOCAL_NATS_ENABLED"
NATS_USER_ENV = "NATS_USER"
NATS_PASSWORD_ENV = "NATS_PASSWORD"
GENERATED_NATS_USER = "ua-runtime"
_AUTH_DIR_NAME = "auth"
_CREDENTIALS_FILE_NAME = "credentials.json"
_SERVER_CONFIG_FILE_NAME = "nats-server.conf"


def local_nats_url(host: str = INTERNAL_NATS_HOST, port: int = INTERNAL_NATS_PORT) -> str:
    return f"nats://{host}:{port}"


def env_bool(env: Mapping[str, str], key: str, default: bool) -> bool:
    raw = str(env.get(key, "")).strip().lower()
    if not raw:
        return default
    return raw in {"1", "true", "yes", "on"}


def local_nats_enabled_from_env(env: Mapping[str, str] | None = None, *, default: bool = True) -> bool:
    values = dict(env or os.environ)
    if LOCAL_NATS_ENABLED_ENV in values:
        return env_bool(values, LOCAL_NATS_ENABLED_ENV, default)
    return default


def apply_local_nats_env(env: dict[str, str]) -> None:
    env["NATS_URL"] = local_nats_url()
    env["NATS_SERVERS"] = local_nats_url()
    env["NATS_JETSTREAM_ENABLED"] = "true"


def resolve_nats_service_root(env: Mapping[str, str] | None = None) -> Path:
    values = dict(env or os.environ)
    return Path(values.get("TEAM_NATS_SERVICE_ROOT") or "/data/services/nats").expanduser().resolve()


def nats_log_tail(service_root: Path, *, max_files: int = 3, max_lines_per_file: int = 80) -> str:
    log_dir = service_root / "log"
    if not log_dir.exists():
        return f"NATS log directory does not exist: {log_dir}"
    candidates = [path for path in log_dir.iterdir() if path.is_file()]
    if not candidates:
        return f"NATS log directory is empty: {log_dir}"
    candidates.sort(key=lambda path: path.stat().st_mtime, reverse=True)
    sections = []
    for path in candidates[:max_files]:
        try:
            lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
        except OSError as exc:
            rendered = f"Could not read {path}: {exc}"
        else:
            rendered = "\n".join(lines[-max_lines_per_file:])
        sections.append(f"--- NATS log tail: {path} ---\n{rendered}")
    return "\n".join(sections)


def _wait_for_nats_ready(
    process: subprocess.Popen[bytes],
    *,
    logger: logging.Logger,
    service_root: Path,
    timeout_seconds: float = 30.0,
) -> None:
    deadline = time.time() + timeout_seconds
    while time.time() < deadline:
        code = process.poll()
        if code is not None:
            raise RuntimeError(
                f"NATS process exited before becoming ready with code {code}.\n"
                f"{nats_log_tail(service_root)}"
            )
        try:
            with socket.create_connection((INTERNAL_NATS_HOST, INTERNAL_NATS_PORT), timeout=1.0):
                return
        except OSError:
            time.sleep(0.25)
    logger.warning("NATS did not become ready before timeout; diagnostics follow.\n%s", nats_log_tail(service_root))
    raise RuntimeError("Timed out waiting for NATS to become ready.")


def _write_private_file(path: Path, content: str) -> None:
    """Atomically write a file readable only by the service owner."""

    path.parent.mkdir(parents=True, exist_ok=True)
    os.chmod(path.parent, 0o700)
    temporary = path.with_name(f".{path.name}.tmp")
    descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
        handle.write(content)
    os.replace(temporary, path)


def ensure_local_nats_credentials(
    env: MutableMapping[str, str],
    *,
    service_root: Path | None = None,
    logger: logging.Logger | None = None,
    create: bool = True,
) -> None:
    """Make sure ``env`` carries credentials for the local NATS server.

    Operator-provided NATS_USER and NATS_PASSWORD win. Otherwise the stored
    generated pair is loaded, or created on first start. Clients of an already
    running server pass ``create=False`` so they never invent credentials the
    server does not know; ``env`` is left unchanged when none are stored.
    """

    log = logger or logging.getLogger(__name__)
    user = str(env.get(NATS_USER_ENV) or "").strip()
    password = str(env.get(NATS_PASSWORD_ENV) or "").strip()
    if bool(user) != bool(password):
        raise RuntimeError(
            "Local NATS authentication requires both NATS_USER and NATS_PASSWORD. "
            "Set both variables, or unset both to use generated credentials."
        )
    if user and password:
        return
    root = service_root or resolve_nats_service_root(env)
    credentials_path = root / _AUTH_DIR_NAME / _CREDENTIALS_FILE_NAME
    if credentials_path.is_file():
        try:
            stored = json.loads(credentials_path.read_text(encoding="utf-8"))
            user = str(stored["user"]).strip()
            password = str(stored["password"]).strip()
        except (OSError, ValueError, KeyError, TypeError) as exc:
            raise RuntimeError(
                f"Stored local NATS credentials at {credentials_path} are unreadable. "
                "Delete the file to generate new credentials while every runtime process is stopped."
            ) from exc
        if not user or not password:
            raise RuntimeError(
                f"Stored local NATS credentials at {credentials_path} are incomplete. "
                "Delete the file to generate new credentials while every runtime process is stopped."
            )
    elif not create:
        return
    else:
        user = GENERATED_NATS_USER
        password = secrets.token_urlsafe(32)
        _write_private_file(
            credentials_path,
            json.dumps({"user": user, "password": password}) + "\n",
        )
        log.info(
            "Generated local NATS credentials. event=local_nats_credentials_generated path=%s",
            credentials_path,
        )
    env[NATS_USER_ENV] = user
    env[NATS_PASSWORD_ENV] = password


def write_local_nats_server_config(service_root: Path, *, user: str, password: str) -> Path:
    """Write the private server configuration that carries the password."""

    config_path = service_root / _AUTH_DIR_NAME / _SERVER_CONFIG_FILE_NAME
    # JSON string escaping is valid NATS configuration string syntax.
    _write_private_file(
        config_path,
        "authorization {\n"
        f"  user: {json.dumps(user)}\n"
        f"  password: {json.dumps(password)}\n"
        "}\n",
    )
    return config_path


def start_local_nats(
    *,
    env: MutableMapping[str, str],
    logger: logging.Logger,
    cwd: Path,
    service_root: Path | None = None,
) -> subprocess.Popen[bytes]:
    """Start the local server with required authentication.

    ``env`` gains NATS_USER/NATS_PASSWORD when they were unset, so callers must
    pass the same mapping they later give to runtime processes.
    """
    if shutil.which("nats-server") is None:
        raise RuntimeError(
            "Local NATS is enabled, but nats-server is not installed. "
            "Rebuild the image with INSTALL_NATS=true or set TEAM_LOCAL_NATS_ENABLED=false "
            "and configure NATS_URL/NATS_SERVERS for an external server."
        )
    root = service_root or resolve_nats_service_root(env)
    data_dir = root / "data"
    log_dir = root / "log"
    data_dir.mkdir(parents=True, exist_ok=True)
    log_dir.mkdir(parents=True, exist_ok=True)

    server_env = environment_for(PROCESS_NATS, env)
    version_check = subprocess.run(
        ["nats-server", "--version"],
        check=False,
        cwd=cwd,
        env=server_env,
        capture_output=True,
        text=True,
    )
    version_output = "\n".join(
        part for part in (version_check.stdout.strip(), version_check.stderr.strip()) if part
    ).strip()
    if version_output:
        log = logger.info if version_check.returncode == 0 else logger.warning
        log("Bundled NATS server version: %s", version_output)

    ensure_local_nats_credentials(env, service_root=root, logger=logger)
    server_config = write_local_nats_server_config(
        root,
        user=str(env[NATS_USER_ENV]),
        password=str(env[NATS_PASSWORD_ENV]),
    )

    command = [
        "nats-server",
        "-js",
        "-sd",
        str(data_dir),
        "-a",
        INTERNAL_NATS_HOST,
        "-p",
        str(INTERNAL_NATS_PORT),
        "-m",
        str(INTERNAL_NATS_MONITOR_PORT),
        "-c",
        str(server_config),
    ]
    log_command = list(command)
    startup_log_path = log_dir / "nats-server-startup.log"
    logger.info("Starting local NATS/JetStream: %s", " ".join(log_command))
    logger.info("Writing local NATS startup output to %s", startup_log_path)
    startup_log = startup_log_path.open("ab")
    try:
        process = subprocess.Popen(
            command,
            cwd=cwd,
            env=server_env,
            stdout=startup_log,
            stderr=subprocess.STDOUT,
            start_new_session=True,
        )
    finally:
        startup_log.close()
    _wait_for_nats_ready(process, logger=logger, service_root=root)
    return process
