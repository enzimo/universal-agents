"""Filesystem sandbox for agent-run shell and Python commands on the host.

Why this exists: agent-run code executes as the runtime's operating-system
user (root in the default container). Without a sandbox it can read every
secret file, the instance configuration, the task and permission database,
and other processes' environments, whatever the file tools' path policy says.
Path checks on command text cannot contain code that builds paths at run time.

The sandbox uses Linux Landlock, which an unprivileged process can apply to
itself. Each command runs through ``code_sandbox_exec.py``, which grants:

* read and execute access to operating-system and interpreter directories;
* the folders the agent and its person may use (``custom_agents.code_access``):
  read-write where both may write, read-only where they may only read, and a
  private per-command scratch directory used as ``HOME`` and ``TMPDIR``;
* read and write access to existing device files under ``/dev``;
* operator-configured extra read-only or read-write paths.

Nothing else is reachable. Landlock also prevents the command from inspecting
processes outside the sandbox. TCP connections and listening sockets are
denied unless the agent and the person it works for hold internet access
(``fn:internet``: standard for every role, closed only by a block, decision
D14). Closing the network needs Landlock ABI 4 (Linux 6.7); without it a
command that must stay off the network is refused rather than run with the
network open. UDP is not covered. Internal services still require
credentials that sandboxed code cannot read. OpenShell mode uses its own
sandbox and does not use this module.

``required`` (the default) refuses to run a command when the sandbox cannot be
applied. ``off`` runs every command unconfined and is logged as a warning. A
single command runs unconfined only on an approved ``code:unconfined`` grant.
"""

from __future__ import annotations

import ctypes
import json
import logging
import os
import platform
import shutil
import sys
import tempfile
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping, Sequence

from runtime_settings.instance_layout import InstanceLayout
from runtime_settings.code_sandbox_contract import (
    CODE_SANDBOX_MODES,
    CODE_SANDBOX_OFF,
    CODE_SANDBOX_REQUIRED,
    ENV_CODE_SANDBOX_EXTRA_READ_PATHS,
    ENV_CODE_SANDBOX_EXTRA_WRITE_PATHS,
    ENV_CODE_SANDBOX_MODE,
)

logger = logging.getLogger(__name__)

CODE_SANDBOX_KIND_LANDLOCK = "landlock"

REASON_CODE_SANDBOX_UNAVAILABLE = "code_sandbox_unavailable"
# One command run outside the sandbox on an approved code:unconfined grant.
CODE_SANDBOX_UNCONFINED = "unconfined"
# Landlock can limit TCP from this ABI on (code_sandbox_exec.NETWORK_ABI).
NETWORK_LANDLOCK_ABI = 4

# Operating-system locations code needs to run programs. None of these hold
# Universal Agents secrets. /proc is readable, but Landlock denies access to
# processes outside the sandbox, so other processes' environments stay hidden.
SYSTEM_READ_PATHS: tuple[str, ...] = (
    "/usr",
    "/bin",
    "/sbin",
    "/lib",
    "/lib32",
    "/lib64",
    "/libx32",
    "/etc",
    "/opt",
    "/proc",
    "/sys",
    "/run/systemd/resolve",
    "/run/resolvconf",
)
DEVICE_PATHS: tuple[str, ...] = ("/dev",)
# Per-user locations programs read and write. They all point at the private
# scratch directory: a runtime-provided XDG_CONFIG_HOME outside the sandbox
# would otherwise make tools such as git fail on "Permission denied".
SCRATCH_ENV_KEYS: tuple[str, ...] = (
    "HOME",
    "TMPDIR",
    "TMP",
    "TEMP",
    "XDG_CONFIG_HOME",
    "XDG_CACHE_HOME",
    "XDG_DATA_HOME",
    "XDG_STATE_HOME",
    "XDG_RUNTIME_DIR",
)

_SYS_LANDLOCK_CREATE_RULESET = 444
_LANDLOCK_CREATE_RULESET_VERSION = 1 << 0
_SUPPORTED_MACHINES = {"x86_64", "amd64", "aarch64", "arm64"}
_HELPER_NAME = "code_sandbox_exec"

_status_logged = False
_status_lock = threading.Lock()


class CodeSandboxUnavailable(RuntimeError):
    """The sandbox is required but cannot be applied on this system."""

    reason_code = REASON_CODE_SANDBOX_UNAVAILABLE


@dataclass(frozen=True)
class CodeSandboxStatus:
    mode: str
    available: bool
    landlock_abi: int
    detail: str


@dataclass(frozen=True)
class SandboxedCommand:
    """The argv and environment that actually execute one command."""

    argv: tuple[str, ...]
    env: dict[str, str]
    sandbox: str
    scratch_dir: str | None = None


def configured_mode(env: Mapping[str, str] | None = None) -> str:
    """Return the configured mode; unknown values fail closed to ``required``."""

    source = os.environ if env is None else env
    raw = str(source.get(ENV_CODE_SANDBOX_MODE, "") or "").strip().lower()
    if not raw:
        return CODE_SANDBOX_REQUIRED
    if raw not in CODE_SANDBOX_MODES:
        logger.warning(
            "Unknown code sandbox mode; requiring the sandbox. "
            "event=code_sandbox_mode_invalid value=%s allowed=%s",
            raw,
            ",".join(CODE_SANDBOX_MODES),
        )
        return CODE_SANDBOX_REQUIRED
    return raw


def landlock_abi() -> int:
    """Return the kernel's Landlock ABI version, or 0 when unavailable."""

    if sys.platform != "linux" or platform.machine().lower() not in _SUPPORTED_MACHINES:
        return 0
    libc = ctypes.CDLL(None, use_errno=True)
    abi = libc.syscall(
        _SYS_LANDLOCK_CREATE_RULESET,
        None,
        ctypes.c_size_t(0),
        ctypes.c_uint32(_LANDLOCK_CREATE_RULESET_VERSION),
    )
    return abi if abi > 0 else 0


def code_sandbox_status(env: Mapping[str, str] | None = None) -> CodeSandboxStatus:
    mode = configured_mode(env)
    if mode == CODE_SANDBOX_OFF:
        return CodeSandboxStatus(
            mode=mode,
            available=False,
            landlock_abi=0,
            detail="Agent-run shell and Python commands run unconfined.",
        )
    abi = landlock_abi()
    if abi < 1:
        return CodeSandboxStatus(
            mode=mode,
            available=False,
            landlock_abi=0,
            detail=(
                "Linux Landlock is unavailable (it needs Linux 5.13 or later with Landlock "
                "enabled, and a container security profile that allows it)."
            ),
        )
    return CodeSandboxStatus(
        mode=mode,
        available=True,
        landlock_abi=abi,
        detail="Agent-run shell and Python commands reach only the folders their agent and person may use.",
    )


def log_code_sandbox_status_once(env: Mapping[str, str] | None = None) -> CodeSandboxStatus:
    """Log the effective sandbox state the first time code runs in a process."""

    global _status_logged
    status = code_sandbox_status(env)
    with _status_lock:
        if _status_logged:
            return status
        _status_logged = True
    if status.mode == CODE_SANDBOX_OFF:
        logger.warning(
            "Agent code execution is not sandboxed; shell and Python commands can read "
            "anything this process can. event=code_sandbox_disabled mode=%s",
            status.mode,
        )
    elif not status.available:
        logger.error(
            "Agent code execution is refused because the required sandbox is unavailable. "
            "event=code_sandbox_unavailable mode=%s detail=%s",
            status.mode,
            status.detail,
        )
    else:
        logger.info(
            "Agent code execution is sandboxed. event=code_sandbox_ready kind=%s abi=%s",
            CODE_SANDBOX_KIND_LANDLOCK,
            status.landlock_abi,
        )
    return status


def _helper_path() -> Path:
    here = Path(__file__)
    for candidate in (here.with_name(f"{_HELPER_NAME}.py"), here.with_name(f"{_HELPER_NAME}.pyc")):
        if candidate.is_file():
            return candidate
    raise CodeSandboxUnavailable(
        f"The sandbox launcher {_HELPER_NAME}.py is missing from {here.parent}."
    )


def _configured_paths(env: Mapping[str, str], key: str) -> list[str]:
    raw = str(env.get(key, "") or "").strip()
    if not raw:
        return []
    try:
        values = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise CodeSandboxUnavailable(f"{key} must be a JSON list of absolute paths: {exc}") from exc
    if not isinstance(values, list) or not all(isinstance(item, str) for item in values):
        raise CodeSandboxUnavailable(f"{key} must be a JSON list of absolute paths.")
    paths = []
    for item in values:
        if not os.path.isabs(item):
            raise CodeSandboxUnavailable(f"{key} entries must be absolute paths: {item!r}")
        paths.append(os.path.realpath(item))
    return paths


def _is_same_or_ancestor(candidate: str, path: str) -> bool:
    candidate = candidate.rstrip("/") or "/"
    return path == candidate or path.startswith(candidate if candidate == "/" else candidate + "/")


def _search_path_dirs(env: Mapping[str, str]) -> list[str]:
    return [
        os.path.realpath(entry)
        for entry in str(env.get("PATH", "") or "").split(os.pathsep)
        if entry and os.path.isabs(entry)
    ]


def _protected_roots(env: Mapping[str, str], task_roots: Sequence[str]) -> list[str]:
    """Directories whose parents must never be granted to sandboxed code.

    Granting a parent would expose everything below it: the instance root holds
    configuration, secrets files, control files, databases, and every person's
    folder; the broker credentials directory may live elsewhere. The task's own
    folders are listed so that their parents are never granted either.
    """

    roots = list(task_roots)
    for key in ("TEAM_INSTANCE_ROOT", "SECURE_TOOL_BROKER_CREDENTIALS_DIR"):
        value = str(env.get(key, "") or "").strip()
        if value and os.path.isabs(value):
            roots.append(os.path.realpath(value))
    return roots


def _instance_layout(env: Mapping[str, str]) -> InstanceLayout | None:
    value = str(env.get("TEAM_INSTANCE_ROOT", "") or "").strip()
    return InstanceLayout.at(value) if value and os.path.isabs(value) else None


def _without_protected_parents(
    paths: list[str],
    protected: list[str],
    *,
    kind: str,
    layout: InstanceLayout | None = None,
) -> list[str]:
    kept = []
    for path in dict.fromkeys(paths):
        exposed = [item for item in protected if _is_same_or_ancestor(path, item)]
        if layout is not None and layout.is_instance_private(Path(path)):
            # An operator extra path inside control/, dbs/, state/ or another
            # runtime folder would hand agent code the files it must not reach.
            exposed.append(str(layout.root))
        if exposed:
            logger.warning(
                "Not granting a sandbox path that contains protected runtime files. "
                "event=code_sandbox_path_skipped kind=%s path=%s protected=%s",
                kind,
                path,
                ",".join(exposed),
            )
            continue
        kept.append(path)
    return kept


def sandbox_policy(
    *,
    task_roots: Sequence[str],
    scratch_dir: str,
    env: Mapping[str, str],
    read_only_roots: Sequence[str] = (),
    granted_read_roots: Sequence[str] = (),
    network: bool = False,
) -> dict[str, Any]:
    """Paths granted to one sandboxed command, and whether it may use the network.

    ``task_roots`` are the folders the command may write
    (``custom_agents.code_access``); nothing else under the instance root is
    writable, so code for one person cannot see another person's folder,
    control files, databases or runtime records. ``read_only_roots`` are the
    team skill folders, readable so a skill's bundled scripts can run.
    ``granted_read_roots`` are the folders the agent and its person may only
    read; they may hold a writable granted folder (read on ``data/reports``,
    write on ``data/reports/2026``), so only the instance's own protected
    locations are checked for them, not the task's folders.
    """

    roots = [os.path.realpath(root) for root in task_roots]
    protected = _protected_roots(env, roots)
    instance_protected = _protected_roots(env, [])
    granted_read = [os.path.realpath(root) for root in granted_read_roots]
    layout = _instance_layout(env)
    interpreter = sorted({os.path.realpath(p) for p in (sys.base_prefix, sys.prefix, sys.exec_prefix)})
    read = list(SYSTEM_READ_PATHS) + interpreter + _search_path_dirs(env)
    read += _configured_paths(env, ENV_CODE_SANDBOX_EXTRA_READ_PATHS)
    write = [os.path.realpath(scratch_dir)] + _configured_paths(env, ENV_CODE_SANDBOX_EXTRA_WRITE_PATHS)
    skills = [os.path.realpath(root) for root in read_only_roots]
    return {
        # Skill folders sit inside the instance, so they skip the instance-private
        # check applied to operator paths, but never a protected-parent check.
        "read": _without_protected_parents(read, protected, kind="read", layout=layout)
        + _without_protected_parents(skills, protected, kind="skill")
        + _without_protected_parents(granted_read, instance_protected, kind="granted_read"),
        # The task's folders are granted; only their parents are protected.
        "write": roots
        + _without_protected_parents(write, protected, kind="write", layout=layout),
        "device": list(DEVICE_PATHS),
        "network": bool(network),
    }


def _within_granted_paths(value: str, policy: Mapping[str, Any]) -> bool:
    resolved = os.path.realpath(value)
    granted = [*policy["read"], *policy["write"], *policy["device"]]
    return any(_is_same_or_ancestor(root, resolved) for root in granted)


def _without_ungranted_path_variables(
    env: Mapping[str, str], policy: Mapping[str, Any]
) -> tuple[dict[str, str], list[str]]:
    """Drop variables that name a single path the sandbox cannot reach.

    Runtimes commonly export per-user locations such as GIT_CONFIG_GLOBAL,
    CARGO_HOME, or a secret file path. Inside the sandbox those paths are
    unreadable: some tools treat that as fatal (git aborts on an unreadable
    global config), and the value still discloses where secrets live. Without
    the variable, tools fall back to their defaults under the scratch HOME.
    """

    kept: dict[str, str] = {}
    dropped: list[str] = []
    for key, value in env.items():
        if (
            key not in SCRATCH_ENV_KEYS
            and value.startswith("/")
            and os.pathsep not in value
            and not _within_granted_paths(value, policy)
        ):
            dropped.append(key)
            continue
        kept[key] = value
    return kept, sorted(dropped)


def prepare_sandboxed_command(
    argv: tuple[str, ...],
    *,
    task_roots: Sequence[str],
    env: Mapping[str, str],
    settings_env: Mapping[str, str] | None = None,
    network: bool,
    read_only_roots: Sequence[str] = (),
    granted_read_roots: Sequence[str] = (),
    unconfined: bool = False,
    scratch_dir: str | None = None,
) -> SandboxedCommand:
    """Wrap one command in the sandbox, or return it unchanged when disabled.

    ``env`` is the child's environment. ``settings_env`` supplies the sandbox
    settings and defaults to the runtime process environment.
    ``read_only_roots`` are folders the command may read but not write.
    ``network`` allows TCP; ``unconfined`` (an approved ``code:unconfined``
    grant) runs this one command outside the sandbox. ``scratch_dir`` is a
    private folder the caller made and prepared (the broker's run folder for
    an installed CLI, holding the checked program and its credential file);
    it becomes the command's home and only writable folder, and the caller
    removes it.
    """

    settings = os.environ if settings_env is None else settings_env
    if unconfined:
        return SandboxedCommand(argv=argv, env=dict(env), sandbox=CODE_SANDBOX_UNCONFINED)
    status = log_code_sandbox_status_once(settings)
    if status.mode == CODE_SANDBOX_OFF:
        return SandboxedCommand(argv=argv, env=dict(env), sandbox=CODE_SANDBOX_OFF)
    if not status.available:
        raise CodeSandboxUnavailable(
            "Code execution is sandboxed, but the sandbox cannot run on this system. "
            f"{status.detail} The operator can enable Landlock, use security_layer.mode "
            "'openshell', or set security_layer.code_execution.sandbox to 'off' to run "
            "agent code unconfined."
        )
    if not network and status.landlock_abi < NETWORK_LANDLOCK_ABI:
        raise CodeSandboxUnavailable(
            f"This kernel's Landlock (ABI {status.landlock_abi}) cannot keep sandboxed code off the "
            f"network; that needs ABI {NETWORK_LANDLOCK_ABI} (Linux 6.7 or later). A person has blocked "
            "internet access for this work, so code cannot run here: tell the person, who can lift "
            "the block, or ask the operator to run a newer kernel."
        )
    helper = _helper_path()
    owned = scratch_dir is None
    scratch_dir = tempfile.mkdtemp(prefix="ua-code-sandbox-") if scratch_dir is None else scratch_dir
    try:
        merged = {**dict(settings), **dict(env)}
        policy = sandbox_policy(
            task_roots=task_roots,
            scratch_dir=scratch_dir,
            env=merged,
            read_only_roots=read_only_roots,
            granted_read_roots=granted_read_roots,
            network=network,
        )
        child_env, dropped = _without_ungranted_path_variables(env, policy)
        if dropped:
            logger.info(
                "Removed environment variables naming paths outside the code sandbox. "
                "event=code_sandbox_env_paths_removed names=%s",
                ",".join(dropped),
            )
        for key in SCRATCH_ENV_KEYS:
            child_env[key] = scratch_dir
        wrapped = (
            sys.executable,
            "-I",
            "-S",
            str(helper),
            json.dumps(policy, separators=(",", ":")),
            *argv,
        )
    except BaseException:
        if owned:
            shutil.rmtree(scratch_dir, ignore_errors=True)
        raise
    return SandboxedCommand(
        argv=wrapped,
        env=child_env,
        sandbox=CODE_SANDBOX_KIND_LANDLOCK,
        scratch_dir=scratch_dir if owned else None,
    )


def release_sandboxed_command(command: SandboxedCommand) -> None:
    """Delete the per-command scratch directory."""

    if command.scratch_dir:
        shutil.rmtree(command.scratch_dir, ignore_errors=True)


def sandbox_denial_note(command: SandboxedCommand, stderr: str) -> str | None:
    """Explain sandbox limits when a sandboxed command hit a permission error."""

    if command.sandbox != CODE_SANDBOX_KIND_LANDLOCK:
        return None
    lowered = stderr.lower()
    if "permission denied" not in lowered and "operation not permitted" not in lowered:
        return None
    return (
        "Commands run in a sandbox that reaches only the folders you and the person you work "
        "for may use (read-only where you may only read), operating-system files and the "
        "team's skills. Team control files, credentials, databases, other people's folders "
        "and other processes are not reachable. The network is open unless a person blocked "
        "internet access for this work; a request cannot lift a block, so tell the person. "
        "If the task needs more, ask with request_access and say exactly why: "
        "folder='<folder>' for a folder, or tool_id='code:unconfined' with level='full' to "
        "run one command outside the sandbox."
    )


__all__ = [
    "CODE_SANDBOX_KIND_LANDLOCK",
    "CODE_SANDBOX_UNCONFINED",
    "NETWORK_LANDLOCK_ABI",
    "CODE_SANDBOX_MODES",
    "CODE_SANDBOX_OFF",
    "CODE_SANDBOX_REQUIRED",
    "CodeSandboxStatus",
    "CodeSandboxUnavailable",
    "ENV_CODE_SANDBOX_EXTRA_READ_PATHS",
    "ENV_CODE_SANDBOX_EXTRA_WRITE_PATHS",
    "ENV_CODE_SANDBOX_MODE",
    "REASON_CODE_SANDBOX_UNAVAILABLE",
    "SandboxedCommand",
    "code_sandbox_status",
    "configured_mode",
    "landlock_abi",
    "log_code_sandbox_status_once",
    "prepare_sandboxed_command",
    "release_sandboxed_command",
    "sandbox_denial_note",
    "sandbox_policy",
]
