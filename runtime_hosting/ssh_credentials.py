"""Broker-owned Sealbox SSH credential material, ephemeral ssh-agent, and secret scrubbing.

Secrets are resolved only in the trusted broker process (never in agent
processes) within the verified owner scope, loaded into a private ephemeral
``ssh-agent`` via standard input so private keys never touch disk, kept under
``InstanceLayout.ssh_ephemeral_dir`` (never under ``/dev`` or ``workspaces/``),
and scrubbed across both streaming reads and complete transcripts.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable, Iterator
from contextlib import AbstractContextManager, contextmanager, nullcontext
import ctypes
from dataclasses import dataclass, field
from datetime import datetime, timezone
import logging
import os
from pathlib import Path
import secrets
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
_PR_SET_PDEATHSIG = 1
_REDACTED_TEXT = "[REDACTED]"
_REDACTED_BYTES = b"[REDACTED]"
_AGENT_SOCKET_RELATIVE = "./s"
_AGENT_PID_FILENAME = "agent.pid"
_PEM_HEADER_PREFIX = "-----BEGIN "
_PEM_FOOTER_PREFIX = "-----END "

SecretFieldResolver = Callable[[str, str, OwnerScopeV1], str | None]
KnownHostsLineProvider = Callable[[InteractiveSessionV1], tuple[str, str]]


@dataclass(frozen=True)
class SSHCredentialMaterial:
    """Broker-prepared SSH identity and known-hosts material for one session."""

    identity_file: Path
    known_hosts_file: Path
    ssh_auth_sock: str | None = None
    working_dir: Path | None = None
    public_key: str | None = None
    touched_secrets: tuple[str, ...] = ()


@dataclass(frozen=True)
class SSHAskpassContext:
    """Environment updates for one broker-controlled SSH_ASKPASS invocation."""

    env: dict[str, str]
    touched_secrets: tuple[str, ...] = ()


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

    Holds back only the longest trailing suffix of the buffer that is a strict
    prefix of any registered exact secret, so ordinary output (such as an
    interactive shell prompt) is emitted with zero unnecessary latency while any
    secret split across arbitrary chunk boundaries is caught before emission.
    """

    def __init__(self, scrubber: SessionSecretScrubber) -> None:
        self._scrubber = scrubber
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

        holdback = _trailing_prefix_holdback(buf, patterns)
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
    if not directory.exists():
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


def _set_pdeathsig_term() -> None:
    """Ask Linux to send SIGTERM to the child if the broker parent dies unexpectedly."""

    if sys.platform != "linux":
        return
    try:
        libc = ctypes.CDLL("libc.so.6")
        libc.prctl(_PR_SET_PDEATHSIG, int(signal.SIGTERM), 0, 0, 0)
    except OSError:
        return


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
    if sys.platform != "linux":
        return False
    try:
        stat_text = Path(f"/proc/{pid}/stat").read_text(encoding="utf-8", errors="ignore")
    except OSError:
        return False
    _, _, rest = stat_text.rpartition(")")
    parts = rest.split()
    return bool(parts) and parts[0] == "Z"


def _terminate_pid(pid: int) -> bool:
    if not _pid_is_ssh_agent(pid):
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
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return True
        except OSError:
            return True
        time.sleep(0.05)
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
        if entry.resolve() in active_resolved:
            continue
        killed_agent = False
        pid_file = entry / _AGENT_PID_FILENAME
        if pid_file.is_file() and not pid_file.is_symlink():
            try:
                recorded_pid = int(pid_file.read_text(encoding="utf-8").strip())
                killed_agent = _terminate_pid(recorded_pid)
            except (ValueError, OSError):
                pass
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


def _build_pipe_askpass_script(python_executable: str) -> bytes:
    """Small executable helper that reads one secret from an inherited pipe FD."""

    script = (
        f"#!{python_executable}\n"
        "import os, sys\n"
        "fd = int(os.environ['UA_SSH_ASKPASS_FD'])\n"
        "with os.fdopen(fd, 'r', encoding='utf-8') as handle:\n"
        "    sys.stdout.write(handle.read())\n"
    )
    return script.encode("utf-8")


def _build_oneshot_file_askpass_script(python_executable: str, secret_path: Path) -> bytes:
    """Executable helper for ssh password bootstrap (ssh closes FDs > 2 before askpass).

    Reads the 0600 file inside the broker-private 0700 directory, immediately
    overwrites it with zeros, unlinks it, and emits the password on stdout.
    """

    script = (
        f"#!{python_executable}\n"
        "import os, sys\n"
        f"path = {str(secret_path)!r}\n"
        "flags = os.O_RDONLY | getattr(os, 'O_NOFOLLOW', 0)\n"
        "fd = os.open(path, flags)\n"
        "try:\n"
        "    data = os.fdopen(fd, 'rb').read()\n"
        "finally:\n"
        "    try:\n"
        "        wfd = os.open(path, os.O_WRONLY | getattr(os, 'O_NOFOLLOW', 0))\n"
        "        try:\n"
        "            os.write(wfd, b'\\x00' * max(len(data), 1))\n"
        "        finally:\n"
        "            os.close(wfd)\n"
        "    except OSError:\n"
        "        pass\n"
        "    try:\n"
        "        os.unlink(path)\n"
        "    except OSError:\n"
        "        pass\n"
        "sys.stdout.write(data.decode('utf-8'))\n"
    )
    return script.encode("utf-8")


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
    - Raw private keys and passphrases are never written to disk: private keys
      stream over stdin to ``ssh-add -t <ttl> -`` and passphrases flow over an
      inherited pipe FD to ``SSH_ASKPASS``.
    - The ``ssh-agent`` runs in the foreground (``-D``) with ``PR_SET_PDEATHSIG``
      and binds relative socket ``./s`` inside the 0700 session directory so no
      path or secret ever appears in ``/proc/<pid>/cmdline``.
    """

    ephemeral_base_dir: Path
    secret_resolver: SecretFieldResolver = _default_catalog_secret_resolver
    known_hosts_line_provider: KnownHostsLineProvider | None = None
    ssh_agent_executable: str = "ssh-agent"
    ssh_add_executable: str = "ssh-add"
    python_executable: str = field(default_factory=lambda: sys.executable)
    default_key_lifetime_seconds: int = 7200
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

        agent_proc: subprocess.Popen[str] | None = None
        try:
            known_hosts_path = session_dir / "known_hosts"
            _write_private_file(known_hosts_path, known_hosts_line.encode("utf-8"), mode=0o600)

            agent_proc = self._start_ephemeral_agent(session_dir)
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
                identity_file=pub_path,
                known_hosts_file=known_hosts_path,
                ssh_auth_sock=_AGENT_SOCKET_RELATIVE,
                working_dir=session_dir,
                public_key=public_key_line,
                touched_secrets=tuple(touched),
            )
        finally:
            self._stop_ephemeral_agent(agent_proc)
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
        """Prepare a one-shot broker-owned SSH_ASKPASS helper for password bootstrap."""

        password = self.resolve_admin_password(
            owner=owner,
            credential_key=credential_key,
            scrubber=scrubber,
        )
        session_dir = _create_ephemeral_session_dir(self.ephemeral_base_dir)
        with self._active_lock:
            self._active_dirs.add(session_dir)
        try:
            secret_path = session_dir / "bootstrap.pw"
            askpass_path = session_dir / "askpass.py"
            _write_private_file(secret_path, f"{password}\n".encode("utf-8"), mode=0o600)
            _write_private_file(
                askpass_path,
                _build_oneshot_file_askpass_script(self.python_executable, secret_path),
                mode=0o700,
            )
            yield SSHAskpassContext(
                env={
                    "SSH_ASKPASS": str(askpass_path),
                    "SSH_ASKPASS_REQUIRE": "force",
                    "DISPLAY": "none",
                },
                touched_secrets=(password,),
            )
        finally:
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

    def _start_ephemeral_agent(self, session_dir: Path) -> subprocess.Popen[str]:
        proc = subprocess.Popen(
            [self.ssh_agent_executable, "-D", "-s", "-a", _AGENT_SOCKET_RELATIVE],
            cwd=str(session_dir),
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            text=True,
            close_fds=True,
            preexec_fn=_set_pdeathsig_term if sys.platform == "linux" else None,
        )
        _write_private_file(
            session_dir / _AGENT_PID_FILENAME,
            f"{proc.pid}\n".encode("ascii"),
            mode=0o600,
        )
        socket_path = session_dir / "s"
        deadline = time.monotonic() + 5.0
        while time.monotonic() < deadline:
            if socket_path.exists():
                return proc
            if proc.poll() is not None:
                stderr = proc.stderr.read() if proc.stderr else ""
                raise RuntimeError(
                    f"Private ssh-agent failed to start (exit {proc.returncode}): {redact_text(stderr).strip()}"
                )
            time.sleep(0.02)
        self._stop_ephemeral_agent(proc)
        raise RuntimeError("Timed out waiting for private ssh-agent socket.")

    def _stop_ephemeral_agent(self, proc: subprocess.Popen[str] | None) -> None:
        if proc is None:
            return
        if proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(timeout=3)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait(timeout=3)
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
        env = {
            key: value
            for key, value in os.environ.items()
            if key not in {"SSH_AUTH_SOCK", "SSH_ASKPASS", "SSH_ASKPASS_REQUIRE", "UA_SSH_ASKPASS_FD"}
        }
        env["SSH_AUTH_SOCK"] = _AGENT_SOCKET_RELATIVE
        askpass_path: Path | None = None
        r_fd: int | None = None
        pass_fds: tuple[int, ...] = ()
        try:
            if passphrase:
                askpass_path = session_dir / "unlock_askpass.py"
                _write_private_file(
                    askpass_path,
                    _build_pipe_askpass_script(self.python_executable),
                    mode=0o700,
                )
                r_fd, w_fd = os.pipe()
                try:
                    os.write(w_fd, f"{passphrase}\n".encode("utf-8"))
                finally:
                    os.close(w_fd)
                env["SSH_ASKPASS"] = str(askpass_path)
                env["SSH_ASKPASS_REQUIRE"] = "force"
                env["DISPLAY"] = "none"
                env["UA_SSH_ASKPASS_FD"] = str(r_fd)
                pass_fds = (r_fd,)
            else:
                env["SSH_ASKPASS_REQUIRE"] = "never"
                env["DISPLAY"] = ""

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
            if askpass_path is not None:
                _wipe_and_unlink(askpass_path)

    def _read_agent_public_key(
        self,
        session_dir: Path,
        scrubber: SessionSecretScrubber,
    ) -> str:
        env = dict(os.environ)
        env["SSH_AUTH_SOCK"] = _AGENT_SOCKET_RELATIVE
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
    "is_short_secret",
    "sweep_stale_ssh_ephemeral_dirs",
]
