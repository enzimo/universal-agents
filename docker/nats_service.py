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
import re
import secrets
import shutil
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
# The JetStream store directory under the service root, passed to nats-server as ``-sd``.
_STORE_DIR_NAME = "data"
# The message contract's server requirements (transport.yaml "server" and "limits"),
# which the bundled server's generated configuration applies.
CONTRACT_TRANSPORT_PATH = Path(__file__).resolve().parents[1] / "contracts" / "messages" / "v1" / "generated" / "transport.json"
# NATS reads a client's credential in its CONNECT line; the rest of that line (name,
# version, options) needs room beside the largest credential the contract allows.
CONNECT_LINE_ROOM_BYTES = 4096
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


_LISTENING = "Listening for client connections on"
_READY = "Server is ready"
_PORT_TAKEN = re.compile(r"listen tcp (\S+): bind: address already in use")


def _startup_output(startup_log_path: Path, offset: int, pid: int) -> str:
    """The lines the server of this start wrote to the startup log every start appends to.

    The offset drops what earlier starts wrote. A start of the same instance that overlaps
    this one appends to the file at the same time, so its server's ready or "address
    already in use" lines can follow the offset too. nats-server begins every log line
    with ``[<pid>] ``, so only the lines of this start's server count. Lines without the
    prefix (an error printed before the server's logger starts) matter only once the
    server has exited, and that message carries the whole log tail.
    """

    prefix = f"[{pid}] "
    with startup_log_path.open("rb") as log:
        log.seek(offset)
        text = log.read().decode("utf-8", errors="replace")
    return "\n".join(line for line in text.splitlines() if line.startswith(prefix))


def _port_taken_message(address: str, store_dir: Path) -> str:
    """The refusal when this start's server found its address taken, with advice that is safe to follow.

    The ports are fixed, so the holder can be this team's own server (the team is still
    running, or a start ended abruptly and left it), or the server or program of another
    team or service on the same machine. Only this team's own server can safely be
    stopped, and only when the team is not running; its command line carries this team's
    store directory (``-sd``), which tells the cases apart. This start never stops or
    adopts the holder itself.
    """

    return (
        f"The local NATS server cannot start: {address} is already in use. Run `ps -ef | grep nats-server` "
        f"to list the running nats-server processes. One started with `-sd {store_dir}` belongs to this team. "
        "If this team is running, stop the team first, and do not stop that nats-server by hand; a command that "
        "starts its own NATS server, such as the consumer reconciler with --start-local-nats, runs only while the "
        "team is stopped. If nothing of this team is running, that nats-server was left over from a start that "
        "ended abruptly: stop it, then start again. If no nats-server shows that directory, another team or "
        "program holds the address, so do not stop it from here: free the port deliberately, or set "
        "TEAM_LOCAL_NATS_ENABLED=false and configure NATS_URL/NATS_SERVERS for this team's own external NATS server."
    )


def _wait_for_nats_ready(
    process: subprocess.Popen[bytes],
    *,
    logger: logging.Logger,
    service_root: Path,
    startup_log_path: Path,
    offset: int,
    timeout_seconds: float = 30.0,
) -> None:
    """Return once the server this start launched says it listens and is ready, while it still runs.

    Another server answering on the port is not readiness. The server runs in its own
    session, so one left over from an earlier start (a launcher that crashed, or a team
    still running) can keep the port, and so can another team's server on the same
    machine; this start's server then stops with "address already in use", and the start
    must refuse with the address its server reported (``_port_taken_message``) instead of
    going on with the old one. Both the ready lines and the "address already in use" line
    count only when this start's server wrote them (``_startup_output``).
    """

    deadline = time.time() + timeout_seconds
    while True:
        code = process.poll()
        output = _startup_output(startup_log_path, offset, process.pid)
        if code is not None:
            taken = _PORT_TAKEN.search(output)
            if taken is not None:
                raise RuntimeError(_port_taken_message(taken.group(1), service_root / _STORE_DIR_NAME))
            raise RuntimeError(
                f"NATS process exited before becoming ready with code {code}.\n{nats_log_tail(service_root)}"
            )
        if _LISTENING in output and _READY in output and process.poll() is None:
            return
        if time.time() >= deadline:
            logger.warning(
                "NATS did not become ready before timeout; diagnostics follow.\n%s", nats_log_tail(service_root)
            )
            raise RuntimeError(
                f"The local NATS server did not say it was ready within {timeout_seconds:g} seconds."
            )
        time.sleep(0.25)


def stop_local_nats(
    process: subprocess.Popen[bytes],
    *,
    logger: logging.Logger,
    terminate_seconds: float = 10.0,
    kill_seconds: float = 5.0,
) -> None:
    """Stop and reap a local NATS server this process started; a slow stop is logged, never raised.

    SIGTERM lets the server finish its JetStream writes; one still running after
    ``terminate_seconds`` is killed. A server that has not exited even ``kill_seconds``
    after SIGKILL is logged as an error with its pid instead of raising: callers stop it
    on their way out of another error (a failed start, a failed maintenance run), and
    that error is the one the operator needs to see. Only the given child process is
    signalled, never a pid read from a log or found on a port, so a port holder this
    process did not start is never stopped here.
    """

    if process.poll() is not None:
        return
    logger.info("Stopping the local NATS server. event=local_nats_stop pid=%s", process.pid)
    # Popen skips the signal when the process has exited meanwhile, so a reused pid is never signalled.
    process.terminate()
    try:
        process.wait(timeout=terminate_seconds)
        return
    except subprocess.TimeoutExpired:
        logger.warning(
            "The local NATS server did not stop within %g seconds of SIGTERM; killing it. "
            "event=local_nats_stop_killed pid=%s",
            terminate_seconds,
            process.pid,
        )
    process.kill()
    try:
        process.wait(timeout=kill_seconds)
    except subprocess.TimeoutExpired:
        logger.error(
            "The local NATS server did not exit within %g seconds of SIGKILL and may still hold its ports. "
            "event=local_nats_stop_failed pid=%s",
            kill_seconds,
            process.pid,
        )


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


def contract_server_settings(path: Path = CONTRACT_TRANSPORT_PATH) -> tuple[dict[str, object], int, str]:
    """What the message contract asks of every NATS server: JetStream settings, control line and minimum version.

    ``sync_interval: always`` makes a confirmed JetStream write reach the disk
    before the server confirms it, so "record before publish" and "admit before
    acknowledge" hold through a power loss. ``max_control_line`` fits the largest
    host credential (``max_credential_bytes``) and the rest of the CONNECT line.
    """

    transport = json.loads(path.read_text(encoding="utf-8"))
    server = transport["server"]
    control_line = int(transport["limits"]["max_credential_bytes"]) + CONNECT_LINE_ROOM_BYTES
    return dict(server["jetstream"]), control_line, str(server["minimum_version"])


def write_local_nats_server_config(service_root: Path, *, user: str, password: str) -> Path:
    """Write the private server configuration: the password and the contract's server settings."""

    jetstream, control_line, _minimum = contract_server_settings()
    config_path = service_root / _AUTH_DIR_NAME / _SERVER_CONFIG_FILE_NAME
    # JSON string escaping is valid NATS configuration string syntax. The store
    # directory stays on the command line (-sd), which the server combines with
    # this jetstream block.
    _write_private_file(
        config_path,
        "authorization {\n"
        f"  user: {json.dumps(user)}\n"
        f"  password: {json.dumps(password)}\n"
        "}\n"
        f"max_control_line: {control_line}\n"
        "jetstream {\n"
        + "".join(f"  {key}: {json.dumps(value)}\n" for key, value in sorted(jetstream.items()))
        + "}\n",
    )
    return config_path


def _version_tuple(text: str) -> tuple[int, ...] | None:
    match = re.search(r"v?(\d+)\.(\d+)\.(\d+)", text)
    return tuple(int(part) for part in match.groups()) if match else None


def report_server_below_contract(version_output: str, logger: logging.Logger) -> None:
    """Log an error when the bundled server is older than the contract's minimum, or its version is unreadable.

    The images and install.sh pin a server that meets it; this names an older
    hand-installed one.
    """

    _jetstream, _control_line, minimum = contract_server_settings()
    found, needed = _version_tuple(version_output), _version_tuple(minimum)
    if found is None or needed is None or found < needed:
        logger.error(
            "The bundled NATS server is older than the message contract requires, or its version could not be read. "
            "event=nats_server_below_contract_minimum found=%r minimum=%s",
            version_output,
            minimum,
        )


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

    A start that fails after launching the server (it timed out, its startup log
    could not be read, or it was interrupted) stops and reaps that server before
    raising. The caller never receives the process when the start fails, and the
    server runs in its own session, so nothing else would stop it: it would keep the
    ports, and the next start would be refused with "already in use".
    """
    if shutil.which("nats-server") is None:
        raise RuntimeError(
            "Local NATS is enabled, but nats-server is not installed. "
            "Rebuild the image with INSTALL_NATS=true or set TEAM_LOCAL_NATS_ENABLED=false "
            "and configure NATS_URL/NATS_SERVERS for an external server."
        )
    root = service_root or resolve_nats_service_root(env)
    data_dir = root / _STORE_DIR_NAME
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
    report_server_below_contract(version_output, logger)

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
    offset = startup_log_path.stat().st_size if startup_log_path.exists() else 0
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
    try:
        _wait_for_nats_ready(
            process, logger=logger, service_root=root, startup_log_path=startup_log_path, offset=offset
        )
    except BaseException:
        # BaseException: an interrupt (Ctrl-C in a maintenance run) must not leave the server running either.
        if process.poll() is None:
            logger.warning(
                "The local NATS start failed; stopping the server it launched. "
                "event=local_nats_start_cleanup pid=%s",
                process.pid,
            )
            stop_local_nats(process, logger=logger)
        raise
    return process
