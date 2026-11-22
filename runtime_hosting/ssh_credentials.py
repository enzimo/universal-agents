"""Broker-owned Sealbox SSH credential material, ephemeral ssh-agent, and secret scrubbing.

Secrets are resolved only in the trusted broker process (never in agent
processes) within the verified owner scope, loaded into a private ephemeral
``ssh-agent`` via standard input so private keys never touch disk, kept under
``InstanceLayout.ssh_ephemeral_dir`` (never under ``/dev`` or ``workspaces/``),
and scrubbed across both streaming reads and complete transcripts.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable, Iterator, Mapping
from contextlib import AbstractContextManager, contextmanager, nullcontext
from dataclasses import dataclass, field
from datetime import datetime, timezone
import errno
import logging
import os
from pathlib import Path
import secrets
import select
import shutil
import signal
import stat
import subprocess
import sys
import threading
import time
from typing import Protocol

from runtime_hosting.execution_contracts import InteractiveSessionV1, OwnerScopeKind, OwnerScopeV1
from runtime_hosting.ssh_network import ssh_host_key_alias
from secure_tool_broker.credential_catalog import ResolvedCredentialField, resolve_secret_field
from secure_tool_broker.credential_catalog_store import CredentialOwnerScope
from secure_tool_broker.redaction import redact_text
from secure_tools.providers import ToolResult

CATALOG_BINDING_PREFIX = "catalog:"
SSH_EPHEMERAL_DIR_PREFIX = "ua-ssh-"
MIN_EXACT_SCRUB_SECRET_LENGTH = 6
FORBIDDEN_EXTRA_SECRET_FIELDS: frozenset[str] = frozenset(
    {"admin_password", "ssh_private_key", "ssh_passphrase"}
)

_LOGGER = logging.getLogger(__name__)
_REDACTED_TEXT = "[REDACTED]"
_REDACTED_BYTES = b"[REDACTED]"
_AGENT_SOCKET_RELATIVE = "./s"
_AGENT_PID_FILENAME = "agent.pid"
_PASSWORD_FIFO_RELATIVE = "./pw.fifo"
_PEM_HEADER_PREFIX = "-----BEGIN "
_PEM_FOOTER_PREFIX = "-----END "
_ASKPASS_HELPER_PATH = Path(__file__).resolve().with_name("_ssh_askpass_helper.py")
_ALLOWED_SUBPROCESS_ENV_KEYS = ("PATH", "LANG", "LC_ALL", "LC_CTYPE", "TZ")
_TOKEN_BYTES = frozenset(
    b"abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_.=-"
)
_MAX_PATTERN_HOLDBACK_BYTES = 1024
_AGENT_KEEPER_CODE = (
    "import os, sys; "
    "print(os.environ['SSH_AGENT_PID'], flush=True); "
    "sys.stdin.buffer.read()"
)

SecretFieldResolver = Callable[[str, str, OwnerScopeV1], str | None]
KnownHostsLineProvider = Callable[[InteractiveSessionV1], tuple[str, str]]


def build_ssh_subprocess_env(
    *,
    extra: Mapping[str, str] | None = None,
    python_executable: str | None = None,
    include_term: bool = False,
) -> dict[str, str]:
    """Build a short allowlisted environment for OpenSSH binaries and helpers.

    Never passes the broker's full process environment (which holds Sealbox
    tokens, broker tokens, and other tool secrets) into ``ssh``, ``ssh-agent``,
    ``ssh-add``, or ``SSH_ASKPASS`` subprocesses.
    """

    keys = (*_ALLOWED_SUBPROCESS_ENV_KEYS, "TERM") if include_term else _ALLOWED_SUBPROCESS_ENV_KEYS
    env: dict[str, str] = {}
    for key in keys:
        value = os.environ.get(key)
        if value is not None:
            env[key] = value
    path_value = env.get("PATH") or os.defpath
    if python_executable:
        py_dir = str(Path(python_executable).resolve().parent)
        path_parts = [part for part in path_value.split(os.pathsep) if part]
        if py_dir and py_dir not in path_parts:
            path_value = os.pathsep.join([py_dir, *path_parts])
    env["PATH"] = path_value
    if extra:
        for key, value in extra.items():
            env[key] = value
    return env


def _resolve_material_path(path: Path, working_dir: Path | None) -> Path:
    if path.is_absolute() or working_dir is None:
        return path
    if ".." in path.parts:
        raise PermissionError("Relative SSH material path must not traverse parent directories.")
    return working_dir / path


def _format_relative_ssh_path(path: Path, working_dir: Path | None) -> str:
    if working_dir is not None and not path.is_absolute():
        if ".." in path.parts:
            raise PermissionError("Relative SSH material path must not traverse parent directories.")
        clean = path.as_posix().lstrip("./")
        if not clean:
            raise PermissionError("Relative SSH material path must not be empty.")
        return f"./{clean}"
    return str(path)


@dataclass(frozen=True)
class SSHCredentialMaterial:
    """Broker-prepared SSH identity and known-hosts material for one session."""

    identity_file: Path
    known_hosts_file: Path
    ssh_auth_sock: str | None = None
    working_dir: Path | None = None
    public_key: str | None = None
    touched_secrets: tuple[str, ...] = field(default=(), repr=False)

    def resolved_identity_file(self) -> Path:
        return _resolve_material_path(self.identity_file, self.working_dir)

    def resolved_known_hosts_file(self) -> Path:
        return _resolve_material_path(self.known_hosts_file, self.working_dir)

    def argv_identity_file(self) -> str:
        return _format_relative_ssh_path(self.identity_file, self.working_dir)

    def argv_known_hosts_file(self) -> str:
        return _format_relative_ssh_path(self.known_hosts_file, self.working_dir)


@dataclass(frozen=True)
class SSHAskpassContext:
    """Environment updates and private working dir for one SSH_ASKPASS invocation."""

    env: dict[str, str]
    working_dir: Path | None = None
    touched_secrets: tuple[str, ...] = field(default=(), repr=False)


@dataclass
class _EphemeralAgentHandle:
    keeper_proc: subprocess.Popen[str]
    agent_pid: int
    agent_start_time: int | None


class SSHCredentialMaterialProvider(Protocol):
    def prepare(
        self, session: InteractiveSessionV1
    ) -> AbstractContextManager[SSHCredentialMaterial]: ...


@dataclass(frozen=True)
class ConfiguredSSHCredentialMaterialProvider:
    """Resolve only operator-configured binding ids to broker-owned key paths."""

    materials: dict[str, SSHCredentialMaterial]

    def prepare(
        self, session: InteractiveSessionV1
    ) -> AbstractContextManager[SSHCredentialMaterial]:
        material = self.materials.get(session.target.credential_binding_id)
        if material is None:
            raise PermissionError("SSH credential binding is not installed in this broker.")
        return nullcontext(material)


def is_short_secret(value: str) -> bool:
    """Return True when a secret is shorter than the exact-match scrubbing threshold."""

    return len(value.strip()) < MIN_EXACT_SCRUB_SECRET_LENGTH


def _extract_scrub_variants(secret: str) -> tuple[str, ...]:
    """Return exact strings (full secret plus non-header PEM body lines) to scrub."""

    stripped = secret.strip()
    if len(stripped) < MIN_EXACT_SCRUB_SECRET_LENGTH:
        return ()
    variants: list[str] = [stripped]
    if "\n" in stripped:
        for raw_line in stripped.splitlines():
            line = raw_line.strip()
            if (
                len(line) >= MIN_EXACT_SCRUB_SECRET_LENGTH
                and not line.startswith(_PEM_HEADER_PREFIX)
                and not line.startswith(_PEM_FOOTER_PREFIX)
            ):
                variants.append(line)
    # Deduplicate while ordering longest-first so full values replace before substrings.
    return tuple(sorted(set(variants), key=len, reverse=True))


class SessionSecretScrubber:
    """Tracks exact Sealbox secrets touched by one SSH session and scrubs all outputs.

    Combines exact longest-first matching (with streaming prefix holdback via
    ``StreamSecretScrubber``) and ``secure_tool_broker.redaction.redact_text``.
    """

    def __init__(
        self,
        initial_secrets: Iterable[str] = (),
        *,
        logger: logging.Logger | None = None,
    ) -> None:
        self._logger = logger or _LOGGER
        self._lock = threading.Lock()
        self._text_secrets: tuple[str, ...] = ()
        self._byte_secrets: tuple[bytes, ...] = ()
        self._short_secret_warned = False
        for item in initial_secrets:
            self.register(item)

    def register(self, secret: str | None) -> bool:
        """Register a touched secret for exact scrubbing.

        Returns False and logs a warning (without logging the secret) when the
        value is shorter than ``MIN_EXACT_SCRUB_SECRET_LENGTH``.
        """

        if not secret:
            return False
        variants = _extract_scrub_variants(secret)
        if not variants:
            if not self._short_secret_warned:
                self._short_secret_warned = True
                self._logger.warning(
                    "Stored SSH secret is shorter than the exact-match scrubbing threshold; "
                    "rotate it to at least %d characters. "
                    "event=ssh_short_secret_skipped_from_exact_scrubbing min_length=%d",
                    MIN_EXACT_SCRUB_SECRET_LENGTH,
                    MIN_EXACT_SCRUB_SECRET_LENGTH,
                )
            return False
        with self._lock:
            merged_text = set(self._text_secrets)
            merged_text.update(variants)
            ordered_text = tuple(sorted(merged_text, key=len, reverse=True))
            self._text_secrets = ordered_text
            self._byte_secrets = tuple(item.encode("utf-8") for item in ordered_text)
        return True

    def register_many(self, secrets_iter: Iterable[str | None]) -> None:
        for item in secrets_iter:
            self.register(item)

    @property
    def byte_patterns(self) -> tuple[bytes, ...]:
        with self._lock:
            return self._byte_secrets

    @property
    def text_patterns(self) -> tuple[str, ...]:
        with self._lock:
            return self._text_secrets

    def scrub_text(self, value: str) -> str:
        """Scrub all exact registered secrets and standard broker token patterns."""

        if not value:
            return value
        result = value
        for pattern in self.text_patterns:
            if pattern in result:
                result = result.replace(pattern, _REDACTED_TEXT)
        return redact_text(result)

    def scrub_bytes(self, data: bytes) -> bytes:
        """Scrub exact UTF-8 byte secrets before decoding, plus standard token patterns."""

        if not data:
            return data
        result = data
        for pattern in self.byte_patterns:
            if pattern in result:
                result = result.replace(pattern, _REDACTED_BYTES)
        # Preserve non-UTF-8 binary bytes via surrogateescape while redacting token regexes.
        decoded = result.decode("utf-8", errors="surrogateescape")
        redacted = redact_text(decoded)
        if redacted != decoded:
            return redacted.encode("utf-8", errors="surrogateescape")
        return result

    def new_stream_scrubber(self) -> "StreamSecretScrubber":
        """Create a chunk-safe streaming scrubber for PTY or command output."""

        return StreamSecretScrubber(self)


class StreamSecretScrubber:
    """Streaming byte scrubber that prevents secrets split across chunks from leaking.

    Holds back the larger of:
    1. the longest trailing suffix that strictly prefixes any registered exact
       secret, and
    2. the trailing run of token characters (``[A-Za-z0-9_.=-]``, capped at
       ``max_pattern_holdback`` bytes) so token patterns recognized by
       ``redact_text`` (GitHub/GitLab/Slack/broker tokens, API keys, JWTs)
       are scrubbed even when split across arbitrary read boundaries.

    Ordinary interactive prompts ending in whitespace, newlines, or prompt
    punctuation (such as ``deploy@box:~$ `` or ``Password: ``) are emitted
    immediately with zero extra holdback; any held tail is released when a
    non-token boundary arrives or on ``flush()``.
    """

    def __init__(
        self,
        scrubber: SessionSecretScrubber,
        *,
        max_pattern_holdback: int = _MAX_PATTERN_HOLDBACK_BYTES,
    ) -> None:
        self._scrubber = scrubber
        self._max_pattern_holdback = max(0, int(max_pattern_holdback))
        self._pending = bytearray()

    def feed(self, chunk: bytes) -> bytes:
        if not chunk:
            return b""
        self._pending.extend(chunk)
        # Replace any complete exact matches already present in the pending buffer.
        buf = bytes(self._pending)
        patterns = self._scrubber.byte_patterns
        for pattern in patterns:
            if pattern in buf:
                buf = buf.replace(pattern, _REDACTED_BYTES)

        holdback = max(
            _trailing_prefix_holdback(buf, patterns),
            _trailing_token_holdback(buf, max_holdback=self._max_pattern_holdback),
        )
        if holdback > 0:
            emit_raw = buf[:-holdback]
            self._pending = bytearray(buf[-holdback:])
        else:
            emit_raw = buf
            self._pending.clear()
        return self._scrubber.scrub_bytes(emit_raw) if emit_raw else b""

    def flush(self) -> bytes:
        if not self._pending:
            return b""
        remaining = bytes(self._pending)
        self._pending.clear()
        return self._scrubber.scrub_bytes(remaining)


def _trailing_prefix_holdback(buf: bytes, patterns: tuple[bytes, ...]) -> int:
    """Return the length of the longest suffix of ``buf`` that strictly prefixes a pattern."""

    if not buf or not patterns:
        return 0
    buf_len = len(buf)
    holdback = 0
    for pattern in patterns:
        max_prefix = min(buf_len, len(pattern) - 1)
        if max_prefix <= holdback:
            continue
        for length in range(max_prefix, holdback, -1):
            if buf.endswith(pattern[:length]):
                holdback = length
                break
    return holdback


def _trailing_token_holdback(buf: bytes, *, max_holdback: int) -> int:
    """Return the length of a trailing token-character run, capped at ``max_holdback``."""

    if not buf or max_holdback <= 0:
        return 0
    limit = min(len(buf), max_holdback)
    held = 0
    for idx in range(len(buf) - 1, len(buf) - 1 - limit, -1):
        if buf[idx] in _TOKEN_BYTES:
            held += 1
        else:
            break
    return held


def _assert_safe_ephemeral_base_dir(base_dir: Path) -> Path:
    resolved = base_dir.expanduser().resolve()
    dev_root = Path("/dev").resolve()
    if resolved == dev_root or dev_root in resolved.parents:
        raise ValueError(
            "SSH ephemeral directory must not live under /dev (including /dev/shm), "
            "because the agent code sandbox permits /dev access. Use InstanceLayout.ssh_ephemeral_dir."
        )
    return resolved


def _ensure_private_dir(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(path, 0o700)


def _create_ephemeral_session_dir(base_dir: Path) -> Path:
    _ensure_private_dir(base_dir)
    for _ in range(16):
        candidate = base_dir / f"{SSH_EPHEMERAL_DIR_PREFIX}{secrets.token_hex(16)}"
        try:
            os.mkdir(candidate, 0o700)
            return candidate
        except FileExistsError:
            continue
    raise RuntimeError("Could not allocate a unique ephemeral SSH directory.")


def _write_private_file(path: Path, payload: bytes, *, mode: int = 0o600) -> None:
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    fd = os.open(path, flags, mode)
    try:
        os.fchmod(fd, mode)
        view = memoryview(payload)
        while view:
            written = os.write(fd, view)
            view = view[written:]
    finally:
        os.close(fd)


def _wipe_and_unlink(path: Path) -> None:
    try:
        st = os.lstat(path)
    except FileNotFoundError:
        return
    except OSError:
        return
    if stat.S_ISREG(st.st_mode) and st.st_size > 0:
        flags = os.O_WRONLY
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        try:
            fd = os.open(path, flags)
            try:
                remaining = st.st_size
                zeros = b"\x00" * min(remaining, 65_536)
                while remaining > 0:
                    chunk = zeros if remaining >= len(zeros) else b"\x00" * remaining
                    written = os.write(fd, chunk)
                    remaining -= written
                os.fsync(fd)
            finally:
                os.close(fd)
        except OSError:
            pass
    try:
        os.unlink(path)
    except FileNotFoundError:
        pass
    except OSError:
        pass


def _wipe_tree(directory: Path) -> None:
    try:
        dir_stat = os.lstat(directory)
    except OSError:
        return
    if not stat.S_ISDIR(dir_stat.st_mode):
        return
    for root, dirs, files in os.walk(directory, topdown=False, followlinks=False):
        root_path = Path(root)
        for name in files:
            _wipe_and_unlink(root_path / name)
        for name in dirs:
            child = root_path / name
            try:
                if child.is_symlink():
                    child.unlink(missing_ok=True)
                else:
                    child.rmdir()
            except OSError:
                pass
    shutil.rmtree(directory, ignore_errors=True)


def _read_proc_stat_fields(pid: int) -> list[str] | None:
    if sys.platform != "linux":
        return None
    try:
        stat_text = Path(f"/proc/{pid}/stat").read_text(encoding="utf-8", errors="ignore")
    except OSError:
        return None
    _, sep, rest = stat_text.rpartition(")")
    if not sep:
        return None
    return rest.split()


def _read_proc_start_time(pid: int) -> int | None:
    """Return the kernel start-time tick (field 22 of ``/proc/<pid>/stat``) on Linux."""

    fields = _read_proc_stat_fields(pid)
    if fields is None or len(fields) <= 19:
        return None
    try:
        return int(fields[19])
    except ValueError:
        return None


def _pid_is_ssh_agent(pid: int) -> bool:
    """Confirm a recorded PID still belongs to an ssh-agent process before signalling."""

    if pid <= 1 or pid == os.getpid():
        return False
    if sys.platform == "linux":
        cmdline_path = Path(f"/proc/{pid}/cmdline")
        try:
            raw = cmdline_path.read_bytes()
            if raw:
                argv0 = raw.split(b"\x00", 1)[0].decode("utf-8", errors="ignore")
                return Path(argv0).name == "ssh-agent"
        except OSError:
            pass
        try:
            comm = Path(f"/proc/{pid}/comm").read_text(encoding="utf-8", errors="ignore").strip()
            return comm == "ssh-agent"
        except OSError:
            return False
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


def _pid_is_zombie(pid: int) -> bool:
    fields = _read_proc_stat_fields(pid)
    return fields is not None and len(fields) > 0 and fields[0] == "Z"


def _pid_matches_recorded_agent(pid: int, *, expected_start_time: int | None) -> bool:
    if not _pid_is_ssh_agent(pid):
        return False
    if sys.platform == "linux":
        if expected_start_time is None:
            return False
        actual_start_time = _read_proc_start_time(pid)
        if actual_start_time != expected_start_time:
            return False
    return True


def _parse_agent_pid_file(pid_file: Path) -> tuple[int, int | None] | None:
    try:
        st = os.lstat(pid_file)
    except OSError:
        return None
    if not stat.S_ISREG(st.st_mode):
        return None
    try:
        parts = pid_file.read_text(encoding="utf-8").strip().split()
    except OSError:
        return None
    if not parts:
        return None
    try:
        pid = int(parts[0])
        start_time = int(parts[1]) if len(parts) >= 2 else None
    except ValueError:
        return None
    return pid, start_time


def _terminate_pid(pid: int, *, expected_start_time: int | None) -> bool:
    if not _pid_matches_recorded_agent(pid, expected_start_time=expected_start_time):
        return False
    try:
        os.kill(pid, signal.SIGTERM)
    except ProcessLookupError:
        return False
    except OSError:
        return False
    deadline = time.monotonic() + 2.0
    while time.monotonic() < deadline:
        if _pid_is_zombie(pid):
            return True
        if sys.platform == "linux" and expected_start_time is not None:
            if _read_proc_start_time(pid) != expected_start_time:
                return True
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return True
        except OSError:
            return True
        time.sleep(0.05)
    if _pid_matches_recorded_agent(pid, expected_start_time=expected_start_time):
        try:
            os.kill(pid, signal.SIGKILL)
        except OSError:
            pass
    return True


def sweep_stale_ssh_ephemeral_dirs(
    base_dir: Path,
    *,
    active_dirs: Iterable[Path] = (),
    logger: logging.Logger | None = None,
) -> int:
    """Remove leftover broker SSH ephemeral directories and orphaned ssh-agents after a crash."""

    log = logger or _LOGGER
    resolved_base = base_dir.expanduser().resolve()
    if not resolved_base.exists():
        return 0
    active_resolved = {Path(item).resolve() for item in active_dirs}
    swept = 0
    for entry in sorted(resolved_base.iterdir()):
        if not entry.name.startswith(SSH_EPHEMERAL_DIR_PREFIX):
            continue
        try:
            entry_stat = os.lstat(entry)
        except OSError:
            continue
        if not stat.S_ISDIR(entry_stat.st_mode):
            log.warning(
                "Skipping non-directory entry under SSH ephemeral base directory. "
                "event=ssh_ephemeral_non_dir_skipped entry=%s",
                entry.name,
            )
            continue
        if entry.resolve() in active_resolved:
            continue
        killed_agent = False
        parsed_pid = _parse_agent_pid_file(entry / _AGENT_PID_FILENAME)
        if parsed_pid is not None:
            recorded_pid, recorded_start = parsed_pid
            killed_agent = _terminate_pid(recorded_pid, expected_start_time=recorded_start)
        _wipe_tree(entry)
        swept += 1
        log.info(
            "Swept stale ephemeral SSH session directory. "
            "event=ssh_ephemeral_dir_swept directory=%s killed_agent=%s",
            entry.name,
            killed_agent,
        )
    return swept


def _default_catalog_secret_resolver(
    credential_key: str,
    field_name: str,
    owner: OwnerScopeV1,
) -> str | None:
    scope = (
        CredentialOwnerScope.user(owner.owner_id)
        if owner.scope_kind == OwnerScopeKind.USER
        else CredentialOwnerScope.team(owner.team_id)
    )
    resolved = resolve_secret_field(credential_key, field_name, scope=scope)
    if isinstance(resolved, ToolResult):
        error_code = (resolved.content or {}).get("error_code") if isinstance(resolved.content, dict) else None
        if error_code == "credential_field_missing":
            return None
        raise PermissionError(
            f"Could not resolve SSH credential {credential_key!r}.{field_name}: {resolved.error or error_code}"
        )
    assert isinstance(resolved, ResolvedCredentialField)
    return resolved.value or None


def _serve_oneshot_fifo(
    fifo_path: Path,
    payload: bytes,
    stop_event: threading.Event,
) -> None:
    """Serve one secret over a named pipe and immediately unlink it so a second read fails."""

    flags = os.O_WRONLY | os.O_NONBLOCK | getattr(os, "O_NOFOLLOW", 0)
    while not stop_event.is_set():
        try:
            fd = os.open(fifo_path, flags)
        except OSError as exc:
            if exc.errno == errno.ENXIO:
                stop_event.wait(0.01)
                continue
            return
        try:
            info = os.fstat(fd)
            if not stat.S_ISFIFO(info.st_mode):
                return
            try:
                os.unlink(fifo_path)
            except OSError:
                pass
            if stop_event.is_set():
                return
            os.set_blocking(fd, True)
            view = memoryview(payload)
            while view:
                written = os.write(fd, view)
                view = view[written:]
        except OSError:
            return
        finally:
            os.close(fd)
        return


def _extract_catalog_credential_key(binding_id: str) -> str:
    if not binding_id.startswith(CATALOG_BINDING_PREFIX):
        raise PermissionError(
            f"Credential binding {binding_id!r} is not a Sealbox catalog SSH credential."
        )
    key = binding_id[len(CATALOG_BINDING_PREFIX) :].strip()
    if not key:
        raise PermissionError("SSH catalog credential binding key must not be empty.")
    return key


def _compute_key_lifetime_seconds(
    session: InteractiveSessionV1,
    *,
    requested_lifetime_seconds: int | None,
    default_lifetime_seconds: int,
) -> int:
    candidates: list[int] = [max(1, int(default_lifetime_seconds))]
    if requested_lifetime_seconds is not None:
        if requested_lifetime_seconds <= 0:
            raise PermissionError("SSH key lifetime must be positive.")
        candidates.append(int(requested_lifetime_seconds))
    activation = session.connect_activation
    if activation is not None and activation.expires_at_utc is not None:
        remaining = int(
            (activation.expires_at_utc - datetime.now(timezone.utc)).total_seconds()
        )
        if remaining <= 0:
            raise PermissionError("SSH session activation has already expired.")
        candidates.append(remaining)
    return max(1, min(candidates))


def _sanitize_public_key_line(raw: str) -> str:
    line = raw.strip()
    if not line or "\n" in line or "\r" in line:
        raise PermissionError("SSH public key must be a single OpenSSH public key line.")
    parts = line.split()
    if len(parts) < 2:
        raise PermissionError("SSH public key must include key algorithm and encoded key data.")
    return line


@dataclass
class CatalogSSHCredentialMaterialProvider:
    """Resolve owner-scoped Sealbox SSH credentials into an isolated private ssh-agent.

    Security invariants:
    - Ephemeral session artifacts live under ``ephemeral_base_dir`` (derived from
      ``InstanceLayout.ssh_ephemeral_dir``), never under ``/dev`` or ``workspaces/``.
    - Raw private keys, passphrases, and bootstrap passwords are never written to
      disk: private keys stream over stdin to ``ssh-add -t <ttl> -``, passphrases
      flow over an inherited pipe FD, and bootstrap passwords flow one-shot over a
      ``0600`` named pipe (FIFO) inside the private session directory.
    - ``SSH_ASKPASS`` points to the tracked package helper script
      ``runtime_hosting/_ssh_askpass_helper.py``, so nothing executable is ever
      written under the instance directory (``noexec`` mounts keep working) and no
      session-folder path ever appears on any command line.
    - ``ssh-agent`` runs in command mode (``ssh-agent -a ./s <keeper>``) bound to a
      pipe held by the broker so it exits automatically even if the broker dies
      with ``SIGKILL`` on setgid-``_ssh`` hosts where ``PR_SET_PDEATHSIG`` is
      cleared, and all SSH subprocesses receive a short allowlisted environment.
    - Material paths returned to ``ssh`` are relative (``./id.pub``,
      ``./known_hosts``, ``./s``) with ``cwd`` set to the private session folder,
      keeping the session folder path off ``/proc/<pid>/cmdline`` for every process.
    """

    ephemeral_base_dir: Path
    secret_resolver: SecretFieldResolver = _default_catalog_secret_resolver
    known_hosts_line_provider: KnownHostsLineProvider | None = None
    ssh_agent_executable: str = "ssh-agent"
    ssh_add_executable: str = "ssh-add"
    python_executable: str = field(default_factory=lambda: sys.executable)
    default_key_lifetime_seconds: int = 60
    logger: logging.Logger = field(default_factory=lambda: _LOGGER)
    _active_dirs: set[Path] = field(default_factory=set, init=False, repr=False)
    _active_lock: threading.Lock = field(default_factory=threading.Lock, init=False, repr=False)

    def __post_init__(self) -> None:
        self.ephemeral_base_dir = _assert_safe_ephemeral_base_dir(self.ephemeral_base_dir)

    def sweep_stale(self) -> int:
        with self._active_lock:
            active = tuple(self._active_dirs)
        return sweep_stale_ssh_ephemeral_dirs(
            self.ephemeral_base_dir,
            active_dirs=active,
            logger=self.logger,
        )

    @contextmanager
    def prepare(
        self,
        session: InteractiveSessionV1,
        *,
        key_lifetime_seconds: int | None = None,
        scrubber: SessionSecretScrubber | None = None,
    ) -> Iterator[SSHCredentialMaterial]:
        credential_key = _extract_catalog_credential_key(session.target.credential_binding_id)
        lifetime = _compute_key_lifetime_seconds(
            session,
            requested_lifetime_seconds=key_lifetime_seconds,
            default_lifetime_seconds=self.default_key_lifetime_seconds,
        )

        private_key = self.secret_resolver(credential_key, "ssh_private_key", session.owner)
        if not private_key or not private_key.strip():
            raise PermissionError(
                f"SSH credential {credential_key!r} is missing required field 'ssh_private_key'."
            )
        passphrase = self.secret_resolver(credential_key, "ssh_passphrase", session.owner)
        stored_public_key = self.secret_resolver(credential_key, "ssh_public_key", session.owner)

        touched: list[str] = [private_key]
        if passphrase:
            touched.append(passphrase)
        active_scrubber = scrubber or SessionSecretScrubber(logger=self.logger)
        active_scrubber.register_many(touched)

        known_hosts_line = self._resolve_known_hosts_line(session)
        session_dir = _create_ephemeral_session_dir(self.ephemeral_base_dir)
        with self._active_lock:
            self._active_dirs.add(session_dir)

        agent_handle: _EphemeralAgentHandle | None = None
        try:
            known_hosts_path = session_dir / "known_hosts"
            _write_private_file(known_hosts_path, known_hosts_line.encode("utf-8"), mode=0o600)

            agent_handle = self._start_ephemeral_agent(session_dir)
            self._load_key_into_agent(
                session_dir=session_dir,
                private_key=private_key,
                passphrase=passphrase,
                lifetime_seconds=lifetime,
                scrubber=active_scrubber,
            )
            agent_public_key = self._read_agent_public_key(session_dir, active_scrubber)
            if stored_public_key and stored_public_key.strip():
                sanitized_stored = _sanitize_public_key_line(stored_public_key)
                if sanitized_stored.split()[:2] != agent_public_key.split()[:2]:
                    raise PermissionError(
                        "Stored ssh_public_key metadata does not match the unlocked ssh_private_key."
                    )
                public_key_line = sanitized_stored
            else:
                public_key_line = agent_public_key

            pub_path = session_dir / "id.pub"
            _write_private_file(pub_path, f"{public_key_line}\n".encode("utf-8"), mode=0o600)

            yield SSHCredentialMaterial(
                identity_file=Path("id.pub"),
                known_hosts_file=Path("known_hosts"),
                ssh_auth_sock=_AGENT_SOCKET_RELATIVE,
                working_dir=session_dir,
                public_key=public_key_line,
                touched_secrets=tuple(touched),
            )
        finally:
            self._stop_ephemeral_agent(agent_handle)
            _wipe_tree(session_dir)
            with self._active_lock:
                self._active_dirs.discard(session_dir)

    @contextmanager
    def prepare_password_askpass(
        self,
        *,
        owner: OwnerScopeV1,
        credential_key: str,
        scrubber: SessionSecretScrubber | None = None,
    ) -> Iterator[SSHAskpassContext]:
        """Prepare a one-shot FIFO-backed SSH_ASKPASS context for password bootstrap."""

        password = self.resolve_admin_password(
            owner=owner,
            credential_key=credential_key,
            scrubber=scrubber,
        )
        session_dir = _create_ephemeral_session_dir(self.ephemeral_base_dir)
        with self._active_lock:
            self._active_dirs.add(session_dir)
        fifo_path = session_dir / "pw.fifo"
        stop_event = threading.Event()
        writer_thread: threading.Thread | None = None
        try:
            os.mkfifo(fifo_path, 0o600)
            os.chmod(fifo_path, 0o600)
            writer_thread = threading.Thread(
                target=_serve_oneshot_fifo,
                args=(fifo_path, f"{password}\n".encode("utf-8"), stop_event),
                name="ssh-password-askpass-fifo",
                daemon=True,
            )
            writer_thread.start()
            yield SSHAskpassContext(
                env={
                    "SSH_ASKPASS": str(_ASKPASS_HELPER_PATH),
                    "SSH_ASKPASS_REQUIRE": "force",
                    "DISPLAY": "none",
                    "UA_SSH_ASKPASS_FIFO": _PASSWORD_FIFO_RELATIVE,
                },
                working_dir=session_dir,
                touched_secrets=(password,),
            )
        finally:
            stop_event.set()
            if writer_thread is not None:
                writer_thread.join(timeout=2.0)
            _wipe_tree(session_dir)
            with self._active_lock:
                self._active_dirs.discard(session_dir)

    def resolve_admin_password(
        self,
        *,
        owner: OwnerScopeV1,
        credential_key: str,
        scrubber: SessionSecretScrubber | None = None,
    ) -> str:
        """Resolve ``admin_password`` strictly within ``owner`` scope for broker-controlled use."""

        password = self.secret_resolver(credential_key, "admin_password", owner)
        if not password:
            raise PermissionError(
                f"SSH credential {credential_key!r} is missing required field 'admin_password'."
            )
        if scrubber is not None:
            scrubber.register(password)
        return password

    def resolve_named_secret(
        self,
        *,
        owner: OwnerScopeV1,
        credential_key: str,
        field_name: str,
        scrubber: SessionSecretScrubber | None = None,
    ) -> str:
        """Resolve a non-forbidden named secret field (for example ``tailscale_auth_key``)."""

        normalized = str(field_name or "").strip()
        if not normalized or normalized in FORBIDDEN_EXTRA_SECRET_FIELDS:
            raise PermissionError(
                f"Credential field {normalized!r} is not permitted as an extra secret field."
            )
        value = self.secret_resolver(credential_key, normalized, owner)
        if not value:
            raise PermissionError(
                f"SSH credential {credential_key!r} is missing secret field {normalized!r}."
            )
        if scrubber is not None:
            scrubber.register(value)
        return value

    def _resolve_known_hosts_line(self, session: InteractiveSessionV1) -> str:
        if self.known_hosts_line_provider is None:
            raise PermissionError(
                "Catalog SSH credential provider requires a pinned host public key."
            )
        algorithm, encoded_key = self.known_hosts_line_provider(session)
        alias = ssh_host_key_alias(session.target.host, session.target.port)
        return f"{alias} {algorithm} {encoded_key}\n"

    def _start_ephemeral_agent(self, session_dir: Path) -> _EphemeralAgentHandle:
        proc = subprocess.Popen(
            [
                self.ssh_agent_executable,
                "-a",
                _AGENT_SOCKET_RELATIVE,
                self.python_executable,
                "-c",
                _AGENT_KEEPER_CODE,
            ],
            cwd=str(session_dir),
            env=build_ssh_subprocess_env(python_executable=self.python_executable),
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            close_fds=True,
        )
        deadline = time.monotonic() + 5.0
        agent_pid: int | None = None
        try:
            assert proc.stdout is not None
            remaining = max(0.0, deadline - time.monotonic())
            ready, _, _ = select.select([proc.stdout], [], [], remaining)
            if ready:
                line = proc.stdout.readline().strip()
                if line:
                    agent_pid = int(line)
            if agent_pid is None or agent_pid <= 1:
                stderr = proc.stderr.read() if proc.stderr else ""
                raise RuntimeError(
                    f"Private ssh-agent failed to start (exit {proc.poll()}): {redact_text(stderr).strip()}"
                )
            socket_path = session_dir / "s"
            while time.monotonic() < deadline and not socket_path.exists():
                time.sleep(0.02)
            if not socket_path.exists():
                raise RuntimeError("Timed out waiting for private ssh-agent socket.")
            start_time = _read_proc_start_time(agent_pid)
            pid_payload = (
                f"{agent_pid} {start_time}\n"
                if start_time is not None
                else f"{agent_pid}\n"
            )
            _write_private_file(
                session_dir / _AGENT_PID_FILENAME,
                pid_payload.encode("ascii"),
                mode=0o600,
            )
            return _EphemeralAgentHandle(
                keeper_proc=proc,
                agent_pid=agent_pid,
                agent_start_time=start_time,
            )
        except Exception:
            self._stop_ephemeral_agent(
                _EphemeralAgentHandle(
                    keeper_proc=proc,
                    agent_pid=agent_pid or 0,
                    agent_start_time=_read_proc_start_time(agent_pid) if agent_pid else None,
                )
            )
            raise

    def _stop_ephemeral_agent(self, handle: _EphemeralAgentHandle | None) -> None:
        if handle is None:
            return
        proc = handle.keeper_proc
        if proc.stdin is not None:
            try:
                proc.stdin.close()
            except OSError:
                pass
        if handle.agent_pid > 1:
            _terminate_pid(handle.agent_pid, expected_start_time=handle.agent_start_time)
        if proc.poll() is None:
            try:
                proc.wait(timeout=2)
            except subprocess.TimeoutExpired:
                proc.terminate()
                try:
                    proc.wait(timeout=2)
                except subprocess.TimeoutExpired:
                    proc.kill()
                    proc.wait(timeout=2)
        for stream in (proc.stdout, proc.stderr):
            if stream is not None:
                try:
                    stream.close()
                except OSError:
                    pass

    def _load_key_into_agent(
        self,
        *,
        session_dir: Path,
        private_key: str,
        passphrase: str | None,
        lifetime_seconds: int,
        scrubber: SessionSecretScrubber,
    ) -> None:
        key_payload = private_key if private_key.endswith("\n") else f"{private_key}\n"
        extra_env: dict[str, str] = {"SSH_AUTH_SOCK": _AGENT_SOCKET_RELATIVE}
        r_fd: int | None = None
        pass_fds: tuple[int, ...] = ()
        try:
            if passphrase:
                r_fd, w_fd = os.pipe()
                try:
                    os.write(w_fd, f"{passphrase}\n".encode("utf-8"))
                finally:
                    os.close(w_fd)
                extra_env["SSH_ASKPASS"] = str(_ASKPASS_HELPER_PATH)
                extra_env["SSH_ASKPASS_REQUIRE"] = "force"
                extra_env["DISPLAY"] = "none"
                extra_env["UA_SSH_ASKPASS_FD"] = str(r_fd)
                pass_fds = (r_fd,)
            else:
                extra_env["SSH_ASKPASS_REQUIRE"] = "never"
                extra_env["DISPLAY"] = ""

            env = build_ssh_subprocess_env(
                extra=extra_env,
                python_executable=self.python_executable,
            )
            result = subprocess.run(
                [self.ssh_add_executable, "-t", str(lifetime_seconds), "-"],
                input=key_payload,
                text=True,
                capture_output=True,
                cwd=str(session_dir),
                env=env,
                pass_fds=pass_fds,
                timeout=15,
                check=False,
            )
            if result.returncode != 0:
                detail = scrubber.scrub_text(result.stderr or result.stdout).strip()
                raise PermissionError(
                    f"Could not unlock SSH private key in broker ssh-agent: {detail or 'ssh-add failed'}"
                )
        finally:
            if r_fd is not None:
                try:
                    os.close(r_fd)
                except OSError:
                    pass

    def _read_agent_public_key(
        self,
        session_dir: Path,
        scrubber: SessionSecretScrubber,
    ) -> str:
        env = build_ssh_subprocess_env(
            extra={"SSH_AUTH_SOCK": _AGENT_SOCKET_RELATIVE},
            python_executable=self.python_executable,
        )
        result = subprocess.run(
            [self.ssh_add_executable, "-L"],
            text=True,
            capture_output=True,
            cwd=str(session_dir),
            env=env,
            timeout=10,
            check=False,
        )
        if result.returncode != 0:
            detail = scrubber.scrub_text(result.stderr).strip()
            raise PermissionError(f"Could not inspect unlocked SSH key in broker ssh-agent: {detail}")
        lines = [line.strip() for line in result.stdout.splitlines() if line.strip()]
        if not lines:
            raise PermissionError("Private ssh-agent holds no unlocked SSH key.")
        return _sanitize_public_key_line(lines[0])


@dataclass(frozen=True)
class CompositeSSHCredentialMaterialProvider:
    """Dispatch catalog:<key> bindings to Sealbox and static bindings to configured disk keys."""

    configured_provider: SSHCredentialMaterialProvider
    catalog_provider: CatalogSSHCredentialMaterialProvider | None = None

    def sweep_stale(self) -> int:
        if self.catalog_provider is None:
            return 0
        return self.catalog_provider.sweep_stale()

    def prepare(
        self, session: InteractiveSessionV1
    ) -> AbstractContextManager[SSHCredentialMaterial]:
        if session.target.credential_binding_id.startswith(CATALOG_BINDING_PREFIX):
            if self.catalog_provider is None:
                raise PermissionError(
                    "Sealbox SSH credential provider is not enabled in this broker."
                )
            return self.catalog_provider.prepare(session)
        return self.configured_provider.prepare(session)


__all__ = [
    "CATALOG_BINDING_PREFIX",
    "FORBIDDEN_EXTRA_SECRET_FIELDS",
    "MIN_EXACT_SCRUB_SECRET_LENGTH",
    "SSH_EPHEMERAL_DIR_PREFIX",
    "CatalogSSHCredentialMaterialProvider",
    "CompositeSSHCredentialMaterialProvider",
    "ConfiguredSSHCredentialMaterialProvider",
    "KnownHostsLineProvider",
    "SSHAskpassContext",
    "SSHCredentialMaterial",
    "SSHCredentialMaterialProvider",
    "SecretFieldResolver",
    "SessionSecretScrubber",
    "StreamSecretScrubber",
    "build_ssh_subprocess_env",
    "is_short_secret",
    "sweep_stale_ssh_ephemeral_dirs",
]
