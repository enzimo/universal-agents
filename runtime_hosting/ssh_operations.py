"""Broker-side SSH operational primitives: host-key probing, key bootstrap, sudo recipes, and bundles.

All remote commands that touch Sealbox secrets are constructed strictly by the
broker (never by agents):

- ``probe_ssh_host_key`` validates the target against ``SSHNetworkPolicy`` and
  runs ``ssh-keyscan -T <timeout> -t ed25519`` without authenticating.
- Key bootstrap installs ``ssh_public_key`` via stdin (never argv) into the
  platform-appropriate ``authorized_keys`` file and verifies key-only login.
- Separated ``sudo`` handling checks ``sudo -n true`` first (never resolving or
  sending ``admin_password`` on NOPASSWD hosts), validates ``admin_password``
  alone via ``<sudo_bin> -S -p '' -v`` with no retries when needed (without
  ``-k`` so ``sudo`` caches its ``TS_PPID`` timestamp for the outer shell),
  verifies that a sudo timestamp persists, and executes the watched root shell
  as a child of that same shell. The stdin-EOF watcher runs inside the root
  shell with the command in the foreground (stdin ``/dev/null``), so revoking
  or timing out signals the remote process group directly as root without any
  second ``sudo`` call (even after ``timestamp_timeout`` expires or under
  ``sudo`` ``use_pty``) and cleans up leftover background jobs in the group.
- Secret-consuming sudo steps are restricted to broker-written named recipes
  (``SSHSecretRecipe.TAILSCALE_UP``) with strictly validated non-secret flags;
  no Sealbox secret is ever piped to an arbitrary agent-written command.
- Workspace bundle transfers enforce size and count ceilings plus
  ``access_control.sensitive_paths`` defense-in-depth before streaming a
  deterministic ``tar.gz`` archive over ``ssh -T``.
"""

from __future__ import annotations

import base64
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
import io
from pathlib import PurePosixPath
import re
import shlex
import subprocess
import tarfile
from typing import Any

from access_control.sensitive_paths import is_sensitive_path
from runtime_hosting.execution_contracts import (
    SSHSecretRecipe,
    SSHTargetPlatform,
    ssh_public_key_sha256_fingerprint,
)
from runtime_hosting.ssh_credentials import build_ssh_subprocess_env
from runtime_hosting.ssh_network import (
    LocalAddressChecker,
    SSHHostResolver,
    SSHNetworkPolicy,
    validate_ssh_host,
)

DEFAULT_COMMAND_OUTPUT_LIMIT_BYTES = 262_144
MAX_BUNDLE_TOTAL_BYTES = 16 * 1024 * 1024
MAX_BUNDLE_ARCHIVE_BYTES = 16 * 1024 * 1024
MAX_BUNDLE_SINGLE_FILE_BYTES = 8 * 1024 * 1024
MAX_BUNDLE_FILES = 500
KEY_AUTH_VERIFY_MARKER = "UA_KEY_AUTH_OK"
SUDO_AUTH_FAILED_MARKER = "UA_SUDO_AUTH_FAILED"
SUDO_NO_TIMESTAMP_MARKER = "UA_SUDO_NO_TIMESTAMP"
TAILSCALE_BIN_MISSING_MARKER = "UA_TAILSCALE_NOT_FOUND"

DEFAULT_SUDO_SEARCH_PATHS: tuple[str, ...] = (
    "/run/wrappers/bin/sudo",
    "/usr/bin/sudo",
)
DEFAULT_TAILSCALE_SEARCH_PATHS: tuple[str, ...] = (
    "/run/current-system/sw/bin/tailscale",
    "/usr/bin/tailscale",
    "/usr/local/bin/tailscale",
    "/opt/homebrew/bin/tailscale",
)

_SAFE_ABSOLUTE_BIN_PATH = re.compile(r"^/[A-Za-z0-9_./+-]{1,255}\Z")
_TAILSCALE_HOSTNAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9-]{0,62}\Z")
_TAILSCALE_TAG_RE = re.compile(r"^tag:[A-Za-z0-9][A-Za-z0-9_-]{0,62}\Z")
_ALLOWED_TAILSCALE_PARAM_KEYS = frozenset(
    {"hostname", "advertise_tags", "ssh", "accept_routes", "accept_dns", "reset"}
)


class SSHCommandStatus(StrEnum):
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    TIMED_OUT = "timed_out"
    REVOKED = "revoked"


@dataclass(frozen=True)
class SSHHostKeyProbeResult:
    """Verified ed25519 host key returned by ``ssh-keyscan`` without authenticating."""

    host: str
    port: int
    resolved_ip: str
    host_key_algorithm: str
    host_key_fingerprint: str
    host_key_public_key: str
    verification_commands: dict[str, str]

    def to_payload(self) -> dict[str, Any]:
        return {
            "host": self.host,
            "port": self.port,
            "resolved_ip": self.resolved_ip,
            "host_key_algorithm": self.host_key_algorithm,
            "host_key_fingerprint": self.host_key_fingerprint,
            "host_key_public_key": self.host_key_public_key,
            "verification_commands": dict(self.verification_commands),
        }


@dataclass(frozen=True)
class SSHCommandResult:
    """Scrubbed state and output of an async or completed remote SSH command."""

    command_id: str
    session_id: str
    status: SSHCommandStatus
    exit_code: int | None
    stdout: str
    stderr: str
    stdout_truncated: bool
    stderr_truncated: bool
    started_at_utc: datetime
    finished_at_utc: datetime | None = None
    error: str | None = None

    def to_payload(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "command_id": self.command_id,
            "session_id": self.session_id,
            "status": self.status.value,
            "exit_code": self.exit_code,
            "stdout": self.stdout,
            "stderr": self.stderr,
            "stdout_truncated": self.stdout_truncated,
            "stderr_truncated": self.stderr_truncated,
            "started_at_utc": self.started_at_utc.isoformat(),
            "finished_at_utc": self.finished_at_utc.isoformat() if self.finished_at_utc else None,
        }
        if self.error:
            payload["error"] = self.error
        return payload


@dataclass(frozen=True)
class SSHKeyEnrollResult:
    """Outcome of broker-executed one-time password SSH key enrollment."""

    session_id: str
    target_id: str
    host: str
    port: int
    username: str
    platform: SSHTargetPlatform
    public_key_fingerprint: str
    verified_key_login: bool
    password_bootstrap_latched_at_utc: datetime

    def to_payload(self) -> dict[str, Any]:
        return {
            "session_id": self.session_id,
            "target_id": self.target_id,
            "host": self.host,
            "port": self.port,
            "username": self.username,
            "platform": self.platform.value,
            "public_key_fingerprint": self.public_key_fingerprint,
            "verified_key_login": self.verified_key_login,
            "password_bootstrap_latched_at_utc": self.password_bootstrap_latched_at_utc.isoformat(),
        }


@dataclass(frozen=True)
class SSHBundleUploadResult:
    """Outcome of a verified non-secret workspace archive transfer."""

    session_id: str
    remote_destination: str
    file_count: int
    archive_bytes: int
    total_file_bytes: int
    stdout: str
    stderr: str

    def to_payload(self) -> dict[str, Any]:
        return {
            "session_id": self.session_id,
            "remote_destination": self.remote_destination,
            "file_count": self.file_count,
            "archive_bytes": self.archive_bytes,
            "total_file_bytes": self.total_file_bytes,
            "stdout": self.stdout,
            "stderr": self.stderr,
        }


def host_key_verification_commands() -> dict[str, str]:
    """Commands an administrator can run directly on the target host to confirm its ed25519 key."""

    return {
        SSHTargetPlatform.LINUX.value: "ssh-keygen -lf /etc/ssh/ssh_host_ed25519_key.pub",
        SSHTargetPlatform.MACOS.value: "ssh-keygen -lf /etc/ssh/ssh_host_ed25519_key.pub",
        SSHTargetPlatform.WINDOWS.value: 'ssh-keygen -lf "$env:ProgramData\\ssh\\ssh_host_ed25519_key.pub"',
    }


def probe_ssh_host_key(
    host: str,
    port: int = 22,
    *,
    network_policy: SSHNetworkPolicy | None = None,
    ssh_keyscan_executable: str = "ssh-keyscan",
    resolver: SSHHostResolver | None = None,
    local_address_checker: LocalAddressChecker | None = None,
    timeout_seconds: int = 10,
) -> SSHHostKeyProbeResult:
    """Validate ``(host, port)`` against network policy and scan its ed25519 host key."""

    policy = network_policy or SSHNetworkPolicy.default()
    validated = validate_ssh_host(
        host,
        port,
        policy,
        resolver=resolver,
        local_address_checker=local_address_checker,
    )
    scan_timeout = max(1, min(int(timeout_seconds), 30))
    env = build_ssh_subprocess_env()
    for candidate_ip in validated.resolved_ips:
        try:
            completed = subprocess.run(
                [
                    ssh_keyscan_executable,
                    "-T",
                    str(scan_timeout),
                    "-t",
                    "ed25519",
                    "-p",
                    str(validated.port),
                    "--",
                    candidate_ip,
                ],
                stdin=subprocess.DEVNULL,
                capture_output=True,
                text=True,
                env=env,
                timeout=scan_timeout + 5,
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired):
            continue
        parsed = _parse_ed25519_keyscan_output(completed.stdout)
        if parsed is not None:
            algorithm, fingerprint, public_key = parsed
            return SSHHostKeyProbeResult(
                host=validated.host,
                port=validated.port,
                resolved_ip=candidate_ip,
                host_key_algorithm=algorithm,
                host_key_fingerprint=fingerprint,
                host_key_public_key=public_key,
                verification_commands=host_key_verification_commands(),
            )
    raise RuntimeError("Could not probe an ssh-ed25519 host key from the target host.")


def _parse_ed25519_keyscan_output(stdout: str) -> tuple[str, str, str] | None:
    for raw_line in stdout.splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        fields = line.split()
        if len(fields) < 3:
            continue
        algorithm, encoded_key = fields[1], fields[2]
        if algorithm != "ssh-ed25519":
            continue
        public_key = f"{algorithm} {encoded_key}"
        try:
            parsed_algo, fingerprint = ssh_public_key_sha256_fingerprint(public_key)
        except ValueError:
            continue
        if parsed_algo == "ssh-ed25519":
            return parsed_algo, fingerprint, public_key
    return None


def _posix_sh_command(script: str) -> tuple[str, ...]:
    """Wrap a POSIX sh script as a single OpenSSH remote command string.

    OpenSSH concatenates trailing argv entries with spaces before passing them
    to the remote login shell; passing one pre-quoted ``sh -c <script>`` string
    preserves exact quoting across bash, zsh, dash, and fish login shells.
    """

    return (f"sh -c {shlex.quote(script)}",)


def _windows_powershell_command(script: str) -> tuple[str, ...]:
    """Wrap a PowerShell script with ``-EncodedCommand`` so no remote shell quoting can alter it."""

    encoded = base64.b64encode(script.encode("utf-16-le")).decode("ascii")
    return (f"powershell -NoProfile -NonInteractive -EncodedCommand {encoded}",)


def _wrap_posix_watched_command(
    child_command: str,
    *,
    cleanup_path_var: str | None = None,
) -> str:
    """Run ``child_command`` in the foreground while a watcher stops the process group on stdin EOF.

    - Running ``child_command`` in the foreground preserves default ``SIGINT`` /
      ``SIGQUIT`` dispositions (avoiding POSIX background-job signal ignoring)
      and returns its exact exit code.
    - The background watcher drains SSH stdin on fd 3 until true EOF with
      stdout/stderr redirected to ``/dev/null`` so normal commands with no
      leftover background jobs return immediately.
    - On stdin EOF, the watcher checks ``kill -0 $$`` to distinguish an early
      stop (revoke, timeout, or connection loss while the wrapper shell is
      still running) from a normal exit (the wrapper shell has already exited
      and been reaped before ``sshd`` closes stdin). Early stops send
      ``SIGTERM`` immediately; normal exits wait 2 seconds first so output
      helpers (for example ``exec > >(tee ...)`` or timestamping pipes) can
      flush and close cleanly before any lingering background job is stopped.
    - For ``sudo`` commands, this wrapper runs inside the root shell spawned by
      ``sudo -n -- sh -c ...`` so stopping signals the root process group
      directly as root without a second ``sudo`` call.
    """

    cleanup_snippet = (
        f"[ -n \"${{{cleanup_path_var}:-}}\" ] && rm -f -- \"${cleanup_path_var}\"; "
        if cleanup_path_var
        else ""
    )
    # Keep a trailing command after child_command so shells that implicitly
    # exec the last simple command in ``sh -c`` (bash 5.1+, ksh93, FreeBSD sh)
    # keep the wrapper shell PID ``$$`` alive until child_command finishes.
    return (
        "exec 3<&0; "
        "( "
        "trap '' TERM; "
        "trap '_ua_hup=1' HUP; "
        "while IFS= read -r _ua_eof <&3; do :; done; "
        "if ! kill -0 $$ 2>/dev/null; then sleep 2; fi; "
        "kill -TERM 0 2>/dev/null; "
        "sleep 1.5 2>/dev/null || sleep 2; "
        f"{cleanup_snippet}"
        "kill -KILL 0 2>/dev/null "
        ") </dev/null >/dev/null 2>&1 & "
        "exec 3<&-; "
        f"{child_command} </dev/null; "
        '_ua_rc=$?; exit "$_ua_rc"'
    )


def build_remote_shell_command(command: str, platform: SSHTargetPlatform) -> tuple[str, ...]:
    """Wrap an agent-supplied remote command safely for the target platform."""

    if not command or not command.strip():
        raise ValueError("SSH command must not be empty.")
    if platform == SSHTargetPlatform.WINDOWS:
        return _windows_powershell_command(command)
    return _posix_sh_command(_wrap_posix_watched_command(f"sh -c {shlex.quote(command)}"))


def build_key_install_command(platform: SSHTargetPlatform) -> tuple[str, ...]:
    """Return a remote command that reads one OpenSSH public key line on stdin and installs it idempotently."""

    if platform == SSHTargetPlatform.WINDOWS:
        ps_script = (
            "$ErrorActionPreference='Stop'; "
            "$key=[Console]::In.ReadLine(); "
            "if ([string]::IsNullOrWhiteSpace($key)) { throw 'Missing public key on stdin' }; "
            "$key=$key.Trim(); "
            "$id=[Security.Principal.WindowsIdentity]::GetCurrent(); "
            "$pr=New-Object Security.Principal.WindowsPrincipal($id); "
            "$isAdmin=$pr.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator); "
            "if ($isAdmin) { "
            "  $dir=Join-Path $env:ProgramData 'ssh'; "
            "  New-Item -ItemType Directory -Force -Path $dir | Out-Null; "
            "  $file=Join-Path $dir 'administrators_authorized_keys'; "
            "} else { "
            "  $dir=Join-Path $env:USERPROFILE '.ssh'; "
            "  New-Item -ItemType Directory -Force -Path $dir | Out-Null; "
            "  $file=Join-Path $dir 'authorized_keys'; "
            "}; "
            "$existing=@(); "
            "if (Test-Path -LiteralPath $file) { $existing=Get-Content -LiteralPath $file }; "
            "if ($existing -notcontains $key) { Add-Content -LiteralPath $file -Value $key -Encoding ascii }; "
            "if ($isAdmin) { "
            "  icacls $file /inheritance:r /grant '*S-1-5-32-544:F' /grant '*S-1-5-18:F' | Out-Null; "
            "}"
        )
        return _windows_powershell_command(ps_script)
    posix_script = (
        "umask 077; "
        "IFS= read -r _ua_pub || exit 1; "
        "[ -n \"$_ua_pub\" ] || exit 1; "
        "mkdir -p \"$HOME/.ssh\" && chmod 700 \"$HOME/.ssh\"; "
        "touch \"$HOME/.ssh/authorized_keys\" && chmod 600 \"$HOME/.ssh/authorized_keys\"; "
        "grep -Fxq -- \"$_ua_pub\" \"$HOME/.ssh/authorized_keys\" "
        "|| printf '%s\\n' \"$_ua_pub\" >> \"$HOME/.ssh/authorized_keys\""
    )
    return _posix_sh_command(posix_script)


def build_key_verify_command(platform: SSHTargetPlatform) -> tuple[str, ...]:
    """Return a harmless remote command proving key-only authentication works."""

    if platform == SSHTargetPlatform.WINDOWS:
        return (f"cmd /c echo {KEY_AUTH_VERIFY_MARKER}",)
    return _posix_sh_command(f"printf '{KEY_AUTH_VERIFY_MARKER}\\n'")


def validate_trusted_bin_paths(paths: Sequence[str], *, label: str) -> tuple[str, ...]:
    """Ensure broker-configured remote binary search paths are safe absolute paths."""

    cleaned: list[str] = []
    for raw in paths:
        item = str(raw or "").strip()
        if not _SAFE_ABSOLUTE_BIN_PATH.fullmatch(item):
            raise ValueError(f"Unsafe {label} binary path: {item!r}.")
        cleaned.append(item)
    if not cleaned:
        raise ValueError(f"At least one trusted {label} path is required.")
    return tuple(cleaned)


def build_sudo_locate_command(sudo_search_paths: Sequence[str]) -> tuple[str, ...]:
    """Locate the first executable trusted absolute sudo binary on the remote host."""

    paths = validate_trusted_bin_paths(sudo_search_paths, label="sudo")
    quoted = " ".join(shlex.quote(path) for path in paths)
    return _posix_sh_command(
        f"for _ua_p in {quoted}; do [ -x \"$_ua_p\" ] && printf '%s\\n' \"$_ua_p\" && exit 0; done; exit 127"
    )


def build_sudo_nopasswd_check_command(sudo_path: str) -> tuple[str, ...]:
    """Check whether ``sudo -n true`` already succeeds without sending any password."""

    validate_trusted_bin_paths((sudo_path,), label="sudo")
    return _posix_sh_command(f"exec {shlex.quote(sudo_path)} -n true </dev/null")


def build_sudo_free_command(
    sudo_path: str,
    command: str,
    *,
    needs_password: bool,
) -> tuple[str, ...]:
    """Build a broker-controlled sudo command that NEVER shares stdin with the agent command.

    When ``needs_password`` is True, line 1 of SSH stdin is read into a shell
    variable, piped alone to ``<sudo_path> -S -p '' -v`` (one attempt, no
    retries, without ``-k`` so ``sudo`` records its ``TS_PPID`` timestamp for
    this outer shell), unset immediately, and verified with
    ``<sudo_path> -n true``. The outer shell then spawns
    ``<sudo_path> -n -- sh -c <watched_inner>`` passing the now-secret-free SSH
    stdin to the root shell, where the stdin-EOF watcher runs as root and the
    agent command runs in the foreground with stdin ``/dev/null``.
    """

    validate_trusted_bin_paths((sudo_path,), label="sudo")
    if not command or not command.strip():
        raise ValueError("Sudo command must not be empty.")
    quoted_sudo = shlex.quote(sudo_path)
    quoted_cmd = shlex.quote(command)
    watched_inner = _wrap_posix_watched_command(f"sh -c {quoted_cmd}")
    sudo_exec = f"{quoted_sudo} -n -- sh -c {shlex.quote(watched_inner)}"
    if not needs_password:
        return _posix_sh_command(sudo_exec)
    # Never leave sudo as the last command of the outer ``sh -c`` script:
    # bash 5.1+, ksh93, and FreeBSD sh implicitly exec the final simple command
    # when no trap is set, replacing the outer shell PID and breaking sudo's
    # TS_PPID timestamp check.
    script = (
        "IFS= read -r _ua_pw || exit 71; "
        f"printf '%s\\n' \"$_ua_pw\" | {quoted_sudo} -S -p '' -v >/dev/null 2>&1; "
        "_ua_rc=$?; _ua_pw=''; unset _ua_pw; "
        f"if [ \"$_ua_rc\" -ne 0 ]; then printf '{SUDO_AUTH_FAILED_MARKER}\\n' >&2; exit 72; fi; "
        f"if ! {quoted_sudo} -n true </dev/null >/dev/null 2>&1; then "
        f"printf '{SUDO_NO_TIMESTAMP_MARKER}\\n' >&2; exit 73; fi; "
        f"{sudo_exec}; "
        '_ua_rc=$?; exit "$_ua_rc"'
    )
    return _posix_sh_command(script)


def validate_tailscale_up_params(params: Mapping[str, Any] | None) -> tuple[str, ...]:
    """Validate non-secret flags for the broker-written ``tailscale_up`` recipe."""

    if not params:
        return ()
    unknown = sorted(set(params) - _ALLOWED_TAILSCALE_PARAM_KEYS)
    if unknown:
        raise ValueError(
            "Unsupported tailscale_up recipe parameters: " + ", ".join(unknown)
        )
    flags: list[str] = []
    hostname = params.get("hostname")
    if hostname is not None:
        cleaned_host = str(hostname).strip()
        if not _TAILSCALE_HOSTNAME_RE.fullmatch(cleaned_host):
            raise ValueError(
                "tailscale_up hostname must be 1-63 letters, digits, or hyphens (starting with letter/digit)."
            )
        flags.extend(("--hostname", cleaned_host))
    tags = params.get("advertise_tags")
    if tags is not None:
        if not isinstance(tags, (list, tuple)) or not tags:
            raise ValueError("tailscale_up advertise_tags must be a non-empty list of tag:<name> strings.")
        cleaned_tags: list[str] = []
        for raw_tag in tags:
            tag = str(raw_tag or "").strip()
            if not _TAILSCALE_TAG_RE.fullmatch(tag):
                raise ValueError(f"Invalid Tailscale tag: {tag!r}.")
            cleaned_tags.append(tag)
        flags.extend(("--advertise-tags", ",".join(cleaned_tags)))
    for key, flag in (
        ("ssh", "--ssh"),
        ("accept_routes", "--accept-routes"),
        ("reset", "--reset"),
    ):
        value = params.get(key)
        if value is not None:
            if not isinstance(value, bool):
                raise ValueError(f"tailscale_up parameter {key!r} must be a boolean.")
            if value:
                flags.append(flag)
    accept_dns = params.get("accept_dns")
    if accept_dns is not None:
        if not isinstance(accept_dns, bool):
            raise ValueError("tailscale_up parameter 'accept_dns' must be a boolean.")
        flags.append("--accept-dns=true" if accept_dns else "--accept-dns=false")
    return tuple(flags)


def build_sudo_secret_recipe_command(
    *,
    recipe: SSHSecretRecipe,
    sudo_path: str,
    needs_password: bool,
    recipe_params: Mapping[str, Any] | None = None,
    tailscale_search_paths: Sequence[str] = DEFAULT_TAILSCALE_SEARCH_PATHS,
) -> tuple[str, ...]:
    """Build a broker-written remote command for an admin-approved secret recipe.

    Secrets flow exclusively over stdin (never argv) into an ephemeral ``0600``
    temp file wiped by ``trap`` on exit.
    """

    validate_trusted_bin_paths((sudo_path,), label="sudo")
    if recipe != SSHSecretRecipe.TAILSCALE_UP:
        raise ValueError(f"Unsupported SSH secret recipe: {recipe!r}.")
    ts_paths = validate_trusted_bin_paths(tailscale_search_paths, label="tailscale")
    flags = validate_tailscale_up_params(recipe_params)
    quoted_sudo = shlex.quote(sudo_path)
    quoted_ts_paths = " ".join(shlex.quote(path) for path in ts_paths)
    quoted_flags = (" " + " ".join(shlex.quote(item) for item in flags)) if flags else ""
    parts: list[str] = ["umask 077"]
    if needs_password:
        parts.extend(
            [
                "IFS= read -r _ua_pw || exit 71",
                f"printf '%s\\n' \"$_ua_pw\" | {quoted_sudo} -S -p '' -v >/dev/null 2>&1",
                "_ua_rc=$?; _ua_pw=''; unset _ua_pw",
                f"if [ \"$_ua_rc\" -ne 0 ]; then printf '{SUDO_AUTH_FAILED_MARKER}\\n' >&2; exit 72; fi",
                f"if ! {quoted_sudo} -n true </dev/null >/dev/null 2>&1; then "
                f"printf '{SUDO_NO_TIMESTAMP_MARKER}\\n' >&2; exit 73; fi",
            ]
        )
    watched_tailscale = _wrap_posix_watched_command(
        f'"$1" up --auth-key="file:$2"{quoted_flags}',
        cleanup_path_var="_ua_kf",
    )
    root_tailscale_script = f"_ua_kf=$2; {watched_tailscale}"
    sudo_tailscale_exec = (
        f"{quoted_sudo} -n -- sh -c {shlex.quote(root_tailscale_script)} "
        '_ "$_ua_ts" "$_ua_keyfile"'
    )
    parts.extend(
        [
            "IFS= read -r _ua_tskey || exit 74",
            "[ -n \"$_ua_tskey\" ] || exit 74",
            f"_ua_ts=''; for _ua_p in {quoted_ts_paths}; do [ -x \"$_ua_p\" ] && _ua_ts=\"$_ua_p\" && break; done",
            f"if [ -z \"$_ua_ts\" ]; then _ua_tskey=''; unset _ua_tskey; "
            f"printf '{TAILSCALE_BIN_MISSING_MARKER}\\n' >&2; exit 75; fi",
            "_ua_keyfile=$(mktemp \"${TMPDIR:-/tmp}/ua-ts-XXXXXX\") || exit 76",
            "trap 'rm -f -- \"$_ua_keyfile\"' EXIT HUP INT TERM",
            "chmod 600 \"$_ua_keyfile\" || exit 76",
            "printf '%s\\n' \"$_ua_tskey\" > \"$_ua_keyfile\"",
            "_ua_tskey=''; unset _ua_tskey",
            sudo_tailscale_exec,
            # Never leave sudo as the last command of the outer ``sh -c`` script
            # (bash 5.1+, ksh93, FreeBSD sh implicitly exec it if no trap holds
            # the shell, which would break sudo's TS_PPID ticket).
            '_ua_rc=$?; exit "$_ua_rc"',
        ]
    )
    return _posix_sh_command("; ".join(parts))


_POWERSHELL_UNICODE_SINGLE_QUOTES: tuple[str, ...] = (
    "\u2018",  # LEFT SINGLE QUOTATION MARK
    "\u2019",  # RIGHT SINGLE QUOTATION MARK
    "\u201a",  # SINGLE LOW-9 QUOTATION MARK
    "\u201b",  # SINGLE HIGH-REVERSED-9 QUOTATION MARK
)


def validate_remote_destination(destination: str) -> str:
    """Reject control characters, PowerShell Unicode single quotes, leading dashes, ``~user`` paths, and empty remote destinations."""

    cleaned = str(destination or "").strip()
    if not cleaned or len(cleaned) > 512:
        raise ValueError("Remote destination path must be between 1 and 512 characters.")
    if cleaned.startswith("-") or any(
        ord(ch) < 32 or ord(ch) == 127 or 0x80 <= ord(ch) <= 0x9F for ch in cleaned
    ):
        raise ValueError(
            "Remote destination path must not start with '-' or contain control characters."
        )
    if any(ch in _POWERSHELL_UNICODE_SINGLE_QUOTES for ch in cleaned):
        raise ValueError(
            "Remote destination path must not contain Unicode single-quote characters."
        )
    if cleaned.startswith("~") and cleaned != "~" and not cleaned.startswith(("~/", "~\\")):
        raise ValueError(
            "Remote destination path must not use '~user' syntax; use '~/', an absolute path, or a relative path."
        )
    return cleaned


def _powershell_utf8_string_expr(value: str) -> str:
    """Encode a string as ASCII base64 decoded inside PowerShell without quoting hazards."""

    encoded = base64.b64encode(value.encode("utf-8")).decode("ascii")
    return f"[Text.Encoding]::UTF8.GetString([Convert]::FromBase64String('{encoded}'))"


def build_bundle_extract_command(destination: str, platform: SSHTargetPlatform) -> tuple[str, ...]:
    """Build a safe remote command to extract a streamed ``tar.gz`` archive into ``destination``."""

    cleaned = validate_remote_destination(destination)
    if platform == SSHTargetPlatform.WINDOWS:
        if cleaned == "~":
            dest_expr = "$env:USERPROFILE"
        elif cleaned.startswith(("~/", "~\\")):
            sub = cleaned[2:]
            dest_expr = (
                f"(Join-Path $env:USERPROFILE ({_powershell_utf8_string_expr(sub)}))"
                if sub
                else "$env:USERPROFILE"
            )
        else:
            dest_expr = _powershell_utf8_string_expr(cleaned)
        ps_script = (
            "$ErrorActionPreference='Stop'; "
            f"$dest={dest_expr}; "
            "New-Item -ItemType Directory -Force -Path $dest | Out-Null; "
            "tar -xzf - -C $dest"
        )
        return _windows_powershell_command(ps_script)
    if cleaned == "~":
        dest_expr = '"$HOME"'
    elif cleaned.startswith("~/"):
        sub = cleaned[2:]
        dest_expr = f'"$HOME"/{shlex.quote(sub)}' if sub else '"$HOME"'
    elif cleaned.startswith("~\\"):
        raise ValueError("POSIX remote destinations must use '~/' rather than '~\\'.")
    else:
        dest_expr = shlex.quote(cleaned)
    return _posix_sh_command(f"mkdir -p -- {dest_expr} && tar -xzf - --no-same-owner -C {dest_expr}")


def build_tar_gz_bundle(
    entries: Sequence[tuple[str, bytes]],
    *,
    max_files: int = MAX_BUNDLE_FILES,
    max_single_file_bytes: int = MAX_BUNDLE_SINGLE_FILE_BYTES,
    max_total_bytes: int = MAX_BUNDLE_TOTAL_BYTES,
    max_archive_bytes: int = MAX_BUNDLE_ARCHIVE_BYTES,
) -> tuple[bytes, int]:
    """Package verified regular files into a deterministic ``tar.gz`` archive.

    Enforces relative non-traversing paths, ``access_control.sensitive_paths``
    filtering (defense in depth), file count limits, and uncompressed/compressed
    byte ceilings. Returns ``(archive_bytes, total_uncompressed_bytes)``.
    """

    if not entries:
        raise ValueError("Bundle upload requires at least one file.")
    if len(entries) > max_files:
        raise ValueError(
            f"Bundle upload contains {len(entries)} files, exceeding the limit of {max_files}."
        )
    normalized_entries: list[tuple[str, bytes]] = []
    seen_paths: set[str] = set()
    total_bytes = 0
    for raw_path, payload in entries:
        posix = PurePosixPath(str(raw_path).replace("\\", "/"))
        if posix.is_absolute() or not posix.parts or any(part in {"", ".", ".."} for part in posix.parts):
            raise ValueError(f"Unsafe archive entry path: {raw_path!r}.")
        if any("\x00" in part or "\n" in part or "\r" in part for part in posix.parts):
            raise ValueError(f"Archive entry path contains disallowed characters: {raw_path!r}.")
        if is_sensitive_path(posix):
            raise PermissionError(
                f"Refused to upload sensitive credential or key file: {posix.as_posix()!r}."
            )
        rel = posix.as_posix()
        if rel in seen_paths:
            raise ValueError(f"Duplicate archive entry path: {rel!r}.")
        seen_paths.add(rel)
        size = len(payload)
        if size > max_single_file_bytes:
            raise ValueError(
                f"File {rel!r} ({size} bytes) exceeds maximum single file size ({max_single_file_bytes} bytes)."
            )
        total_bytes += size
        if total_bytes > max_total_bytes:
            raise ValueError(
                f"Total bundle size ({total_bytes} bytes) exceeds limit ({max_total_bytes} bytes)."
            )
        normalized_entries.append((rel, payload))

    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w:gz", compresslevel=6) as archive:
        for rel, payload in sorted(normalized_entries, key=lambda item: item[0]):
            info = tarfile.TarInfo(name=rel)
            info.size = len(payload)
            info.mode = 0o644
            info.mtime = 0
            info.uid = 0
            info.gid = 0
            info.uname = ""
            info.gname = ""
            archive.addfile(info, io.BytesIO(payload))
    archive_bytes = buffer.getvalue()
    if len(archive_bytes) > max_archive_bytes:
        raise ValueError(
            f"Compressed bundle size ({len(archive_bytes)} bytes) exceeds limit ({max_archive_bytes} bytes)."
        )
    return archive_bytes, total_bytes


__all__ = [
    "DEFAULT_COMMAND_OUTPUT_LIMIT_BYTES",
    "DEFAULT_SUDO_SEARCH_PATHS",
    "DEFAULT_TAILSCALE_SEARCH_PATHS",
    "KEY_AUTH_VERIFY_MARKER",
    "MAX_BUNDLE_ARCHIVE_BYTES",
    "MAX_BUNDLE_FILES",
    "MAX_BUNDLE_SINGLE_FILE_BYTES",
    "MAX_BUNDLE_TOTAL_BYTES",
    "SUDO_AUTH_FAILED_MARKER",
    "SUDO_NO_TIMESTAMP_MARKER",
    "TAILSCALE_BIN_MISSING_MARKER",
    "SSHBundleUploadResult",
    "SSHCommandResult",
    "SSHCommandStatus",
    "SSHHostKeyProbeResult",
    "SSHKeyEnrollResult",
    "build_bundle_extract_command",
    "build_key_install_command",
    "build_key_verify_command",
    "build_remote_shell_command",
    "build_sudo_free_command",
    "build_sudo_locate_command",
    "build_sudo_nopasswd_check_command",
    "build_sudo_secret_recipe_command",
    "build_tar_gz_bundle",
    "host_key_verification_commands",
    "probe_ssh_host_key",
    "validate_remote_destination",
    "validate_tailscale_up_params",
    "validate_trusted_bin_paths",
]
