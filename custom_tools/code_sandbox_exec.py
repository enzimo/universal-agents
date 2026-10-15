"""Apply a Linux Landlock filesystem sandbox, then exec one command.

This file is executed directly as ``python -I -S code_sandbox_exec.py`` in the
child process, before the agent-authored command replaces it. It must depend
only on the Python standard library and must never import project modules:
the runtime launches it with isolated mode, and container images may strip
project sources down to compiled ``.pyc`` files.

Landlock is an allowlist. Everything below a granted directory receives the
granted rights and nothing else is reachable, including secret files outside
the workspace. Landlock also stops the sandboxed process tree from inspecting
processes outside it (``/proc/<pid>/environ``, ``/proc/<pid>/mem``), which is
why removing secrets from the child's environment is not the only safeguard.

Usage: code_sandbox_exec.py <policy-json> <command> [args...]

``policy-json`` holds ``read`` (read and execute), ``write`` (full access),
and ``device`` (read/write existing device files) path lists, and ``network``:
when false, TCP connect and bind are denied (Landlock ABI 4 and later; the
launcher refuses to start such a command on an older kernel). Missing paths
are skipped. Any failure to apply the sandbox exits with status 126 before the
command runs, so the command never executes unconfined.
"""

from __future__ import annotations

import ctypes
import json
import os
import struct
import sys
from typing import NoReturn

# Generic syscall numbers shared by x86_64 and aarch64 since Linux 5.13.
_SYS_LANDLOCK_CREATE_RULESET = 444
_SYS_LANDLOCK_ADD_RULE = 445
_SYS_LANDLOCK_RESTRICT_SELF = 446
_LANDLOCK_CREATE_RULESET_VERSION = 1 << 0
_LANDLOCK_RULE_PATH_BENEATH = 1
_PR_SET_NO_NEW_PRIVS = 38

_EXECUTE = 1 << 0
_WRITE_FILE = 1 << 1
_READ_FILE = 1 << 2
_READ_DIR = 1 << 3
_MAKE_CHAR = 1 << 6
_MAKE_BLOCK = 1 << 11
_REFER = 1 << 13
_TRUNCATE = 1 << 14
_IOCTL_DEV = 1 << 15
# ABI 6 (Linux 6.12+) scopes: block signals to, and abstract-socket
# connections with, processes outside the sandbox.
_SCOPE_ABSTRACT_UNIX_SOCKET = 1 << 0
_SCOPE_SIGNAL = 1 << 1
# ABI 4 (Linux 6.7+) network rights. Handling both with no port rule denies
# every TCP connection and listening socket; UDP is not covered by Landlock.
_NET_BIND_TCP = 1 << 0
_NET_CONNECT_TCP = 1 << 1
NETWORK_ABI = 4

# Rights that make sense on a regular file; directory-only rights are invalid
# on file rules and the kernel rejects them.
_FILE_RIGHTS = _EXECUTE | _WRITE_FILE | _READ_FILE | _TRUNCATE | _IOCTL_DEV

SANDBOX_FAILURE_EXIT_CODE = 126


def _fail(message: str) -> NoReturn:
    sys.stderr.write(f"code sandbox: {message}\n")
    sys.stderr.flush()
    os._exit(SANDBOX_FAILURE_EXIT_CODE)


def _handled_rights(abi: int) -> int:
    """All filesystem rights this kernel's Landlock ABI understands."""

    rights = (1 << 13) - 1  # ABI 1: execute .. make_sym
    if abi >= 2:
        rights |= _REFER
    if abi >= 3:
        rights |= _TRUNCATE
    if abi >= 5:
        rights |= _IOCTL_DEV
    return rights


def ruleset_attr(abi: int, handled_fs: int, handled_net: int = 0) -> bytes:
    """Pack struct landlock_ruleset_attr for this ABI.

    Fields: handled_access_fs, handled_access_net (ABI 4+), scoped (ABI 6+).
    ``handled_net`` is zero when the command may use the network. Passing a
    shorter struct is valid on every ABI; the kernel treats omitted trailing
    fields as zero.
    """

    if abi >= 6:
        return struct.pack("=QQQ", handled_fs, handled_net, _SCOPE_ABSTRACT_UNIX_SOCKET | _SCOPE_SIGNAL)
    if abi >= NETWORK_ABI:
        return struct.pack("=QQ", handled_fs, handled_net)
    return struct.pack("=Q", handled_fs)


def _create_ruleset(libc: ctypes.CDLL, *, network: bool) -> tuple[int, int]:
    """Create a ruleset handling every filesystem right this kernel knows.

    Without ``network`` it also handles TCP connect and bind, so both are denied.
    """

    abi = libc.syscall(
        _SYS_LANDLOCK_CREATE_RULESET,
        None,
        ctypes.c_size_t(0),
        ctypes.c_uint32(_LANDLOCK_CREATE_RULESET_VERSION),
    )
    if abi < 1:
        _fail(f"Linux Landlock is unavailable (errno {ctypes.get_errno()})")
    handled = _handled_rights(abi)
    if not network and abi < NETWORK_ABI:
        _fail(f"this kernel's Landlock (ABI {abi}) cannot limit the network; ABI {NETWORK_ABI} is needed")
    attr_bytes = ruleset_attr(abi, handled, 0 if network else _NET_BIND_TCP | _NET_CONNECT_TCP)
    attr = ctypes.create_string_buffer(attr_bytes, len(attr_bytes))
    ruleset_fd = libc.syscall(
        _SYS_LANDLOCK_CREATE_RULESET, attr, ctypes.c_size_t(len(attr_bytes)), ctypes.c_uint32(0)
    )
    if ruleset_fd < 0:
        _fail(f"could not create the Landlock ruleset (errno {ctypes.get_errno()})")
    return ruleset_fd, handled


def _policy_grants(policy: dict, handled: int) -> list[tuple[str, int]]:
    read_rights = _EXECUTE | _READ_FILE | _READ_DIR
    device_rights = (_READ_FILE | _WRITE_FILE | _READ_DIR | _TRUNCATE | _IOCTL_DEV) & handled
    grants = [(path, read_rights) for path in policy.get("read", [])]
    grants += [(path, device_rights) for path in policy.get("device", [])]
    # Writable folders get every right except making device files, which code
    # never needs there. Links may be made (Python environments need them):
    # trusted code opens files without following links (access_control.safe_open).
    write_rights = handled & ~(_MAKE_CHAR | _MAKE_BLOCK)
    grants += [(path, write_rights) for path in policy.get("write", [])]
    return grants


def _add_rule(libc: ctypes.CDLL, ruleset_fd: int, path: str, rights: int) -> None:
    """Grant ``rights`` below ``path``; missing paths are skipped."""

    try:
        path_fd = os.open(path, os.O_PATH | os.O_CLOEXEC)
    except FileNotFoundError:
        return
    except OSError as exc:
        _fail(f"could not open sandbox path {path!r}: {exc}")
    try:
        allowed = rights if os.path.isdir(path) else rights & _FILE_RIGHTS
        # struct landlock_path_beneath_attr is packed: __u64 then __s32.
        rule = ctypes.create_string_buffer(struct.pack("=Qi", allowed, path_fd), 12)
        result = libc.syscall(
            _SYS_LANDLOCK_ADD_RULE,
            ctypes.c_int(ruleset_fd),
            ctypes.c_int(_LANDLOCK_RULE_PATH_BENEATH),
            rule,
            ctypes.c_uint32(0),
        )
        if result != 0:
            _fail(f"could not add sandbox rule for {path!r} (errno {ctypes.get_errno()})")
    finally:
        os.close(path_fd)


def _restrict_self(libc: ctypes.CDLL, ruleset_fd: int) -> None:
    if libc.prctl(_PR_SET_NO_NEW_PRIVS, 1, 0, 0, 0) != 0:
        _fail(f"could not set no_new_privs (errno {ctypes.get_errno()})")
    if libc.syscall(_SYS_LANDLOCK_RESTRICT_SELF, ctypes.c_int(ruleset_fd), ctypes.c_uint32(0)) != 0:
        _fail(f"could not apply the Landlock ruleset (errno {ctypes.get_errno()})")
    os.close(ruleset_fd)


def _main(argv: list[str]) -> None:
    if len(argv) < 3:
        _fail("usage: code_sandbox_exec.py <policy-json> <command> [args...]")
    try:
        policy = json.loads(argv[1])
    except json.JSONDecodeError as exc:
        _fail(f"invalid policy: {exc}")
    command = argv[2:]
    libc = ctypes.CDLL(None, use_errno=True)
    ruleset_fd, handled = _create_ruleset(libc, network=bool(policy.get("network", False)))
    for path, rights in _policy_grants(policy, handled):
        _add_rule(libc, ruleset_fd, path, rights & handled)
    _restrict_self(libc, ruleset_fd)
    try:
        os.execvp(command[0], command)
    except OSError as exc:
        sys.stderr.write(f"{command[0]}: {exc}\n")
        os._exit(127)


if __name__ == "__main__":
    _main(sys.argv)
