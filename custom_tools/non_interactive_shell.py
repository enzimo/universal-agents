"""
Project-local shell policy and agent-tool adapter.

Process supervision and RTK planning live in the standard-library-only
``custom_tools.shell_execution`` module. This wrapper keeps Strands at the
outermost boundary while enforcing workspace, credential, network, and
OpenShell policy before any command is launched. What each command may reach
(folders, the network, and running outside the sandbox) comes from the access
service through ``custom_agents.code_access``.
"""

from __future__ import annotations

import json
import logging
import os
import re
import shlex
import shutil
import subprocess
import threading
import uuid
from concurrent.futures import CancelledError, ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Any, Dict, List, Literal, Tuple, Union
from urllib.parse import urlparse

from strands import tool

from access_control.sensitive_paths import (
    ENV_FILE_SUFFIX,
    PRIVATE_KEY_FILE_NAMES,
    SENSITIVE_SUFFIXES,
    is_sensitive_path,
)
from custom_tools.path_policy import (
    PROTECTED_RUNTIME_CONTROL_DENIED,
    PathPolicyError,
    contains_protected_runtime_control_reference,
    ensure_allowed_path,
    is_protected_runtime_control_path,
)
from custom_tools.workspace_scope import (
    configured_data_folders,
    current_instance_layout,
    current_workspace_scope,
    ensure_scope_home,
    workspaces_root,
)
from custom_tools.code_sandbox import (
    CodeSandboxUnavailable,
    prepare_sandboxed_command,
    release_sandboxed_command,
    sandbox_denial_note,
)
from custom_tools.openshell_sandboxes import OpenShellSandboxError, ensure_sandbox, workspace_mount
from custom_tools.runtime_context import get_agent_ref
from runtime_settings.security import SECURITY_LAYER_MODE_BROKER, SECURITY_LAYER_MODE_OPENSHELL
from custom_tools.shell_execution import (
    ProcessRequest,
    RtkCommandPlan,
    plan_rtk_command,
    run_process,
    validate_environment_keys,
)


logger = logging.getLogger(__name__)

NON_INTERACTIVE_ENV_OVERRIDES = {
    "CI": "1",
    "DEBIAN_FRONTEND": "noninteractive",
    "GIT_TERMINAL_PROMPT": "0",
    "PIP_NO_INPUT": "1",
    "POETRY_NO_INTERACTION": "1",
    "TERM": "dumb",
    "PAGER": "cat",
    "MANPAGER": "cat",
    "GIT_PAGER": "cat",
    "NO_COLOR": "1",
    "CLICOLOR": "0",
    "FORCE_COLOR": "0",
}

RTK_FALLBACK_WARNING = "rtk is not available on PATH; running command through the normal shell without RTK filtering"
OPENSHELL_RTK_FALLBACK_WARNING = "rtk is not available inside the OpenShell sandbox; running command without RTK filtering"
BLOCKED_CREDENTIAL_COMMANDS = {
    "bw",
    "gh",
    "gog",
    "keepass",
    "keepassxc",
    "keepassxc-cli",
    "op",
    "vault",
}
SECRET_ENV_MARKERS = ("KEY", "TOKEN", "SECRET", "PASSWORD", "PASS", "DSN", "CREDENTIAL")
SECRET_ENV_EXACT = {
    "AWS_ACCESS_KEY_ID",
    "AWS_SECRET_ACCESS_KEY",
    "AWS_SESSION_TOKEN",
    "BW_SESSION",
    "GH_TOKEN",
    "GITHUB_TOKEN",
}
URL_TOKEN_RE = re.compile(r"^[A-Za-z][A-Za-z0-9+.-]*://")
ANY_URL_RE = re.compile(r"\b[A-Za-z][A-Za-z0-9+.-]*://[^\s\"'<>]+")
LOOPBACK_HOSTS = {
    "localhost",
    "ip6-localhost",
    "ip6-loopback",
    "::",
    "::1",
    "0.0.0.0",
    "0:0:0:0:0:0:0:0",
    "0:0:0:0:0:0:0:1",
}
LOOPBACK_NETWORK_COMMANDS = {
    "curl",
    "http",
    "https",
    "wget",
    "nc",
    "ncat",
    "netcat",
    "telnet",
    "ssh",
    "socat",
    "nmap",
    "openssl",
    "python",
    "python3",
    "node",
}

SAFE_SHELL_DEVICE_PATHS = frozenset({Path("/dev/null")})


def _normalize_commands(
    command: Union[str, List[Union[str, Dict[str, Any]]], Dict[str, Any]],
) -> List[Union[str, Dict[str, Any]]]:
    if isinstance(command, str):
        stripped = command.strip()
        if stripped.startswith("[") and stripped.endswith("]"):
            try:
                parsed = json.loads(stripped)
            except json.JSONDecodeError:
                return [command]
            if isinstance(parsed, list):
                return parsed
        return [command]
    if isinstance(command, list):
        return command
    return [command]


def _validate_command(
    command: Union[str, Dict[str, Any]],
) -> Tuple[str, Dict[str, Any]]:
    if isinstance(command, str):
        cmd = command.strip()
        if not cmd:
            raise ValueError("Command is required")
        return cmd, {}
    if isinstance(command, dict):
        normalized = dict(command)
        cmd = str(normalized.get("command") or "").strip()
        if not cmd:
            raise ValueError("Command object must contain a non-empty 'command' string")
        normalized["command"] = cmd
        return cmd, normalized
    raise ValueError("Command must be a string, dict, or list of those values")


def _resolve_work_dir(base_dir: str, override: str | None) -> str:
    raw = str(override or "").strip()
    if not raw:
        return str(ensure_allowed_path(str(Path(base_dir).expanduser().resolve())))
    candidate = Path(raw).expanduser()
    if not candidate.is_absolute():
        candidate = Path(base_dir).expanduser().resolve() / candidate
    return str(ensure_allowed_path(str(candidate)))


def _build_env() -> Dict[str, str]:
    allowlist = {
        value.strip()
        for value in os.getenv("SHELL_PASSTHROUGH_ENV_ALLOWLIST", "").split(",")
        if value.strip()
    }
    env = {}
    for key, value in os.environ.items():
        upper = key.upper()
        if key in allowlist:
            env[key] = value
            continue
        if upper in SECRET_ENV_EXACT or any(
            marker in upper for marker in SECRET_ENV_MARKERS
        ):
            continue
        env[key] = value
    env.update(NON_INTERACTIVE_ENV_OVERRIDES)
    return env


def _command_program(command: str) -> str:
    try:
        tokens = shlex.split(command, posix=True)
    except ValueError:
        return ""
    if not tokens:
        return ""
    index = 0
    while index < len(tokens) and tokens[index] in {"env", "command", "rtk"}:
        index += 1
        if index < len(tokens) and tokens[index] == "proxy":
            index += 1
    while (
        index < len(tokens)
        and "=" in tokens[index]
        and not tokens[index].startswith("=")
    ):
        index += 1
    return Path(tokens[index]).name if index < len(tokens) else ""


def _canonical_command(command: str) -> tuple[str, tuple[str, ...]]:
    tokens = tuple(shlex.split(command, posix=True))
    if not tokens:
        raise ValueError("Command is required")
    return shlex.join(tokens), tokens


def _contains_blocked_credential_command(command: str) -> str:
    program = _command_program(command)
    if program in BLOCKED_CREDENTIAL_COMMANDS:
        return program
    try:
        tokens = shlex.split(command, posix=True)
    except ValueError:
        return ""
    for index, token in enumerate(tokens[:-2]):
        if Path(token).name in {"sh", "bash", "zsh"} and tokens[index + 1] == "-c":
            nested = _contains_blocked_credential_command(tokens[index + 2])
            if nested:
                return nested
    return ""


def _normalize_host_candidate(raw: str) -> str:
    token = raw.strip().strip("[]")
    if "@" in token:
        token = token.rsplit("@", 1)[1]
    if token.startswith("[") and "]" in token:
        return token[1 : token.index("]")].lower()
    if ":" in token and token.count(":") == 1:
        token = token.split(":", 1)[0]
    return token.strip(".,;").lower()


def _is_loopback_host(raw: str) -> bool:
    host = _normalize_host_candidate(raw)
    if not host:
        return False
    if host in LOOPBACK_HOSTS:
        return True
    return bool(re.fullmatch(r"127(?:\.\d{1,3}){0,3}", host))


def _loopback_url(command: str) -> str:
    for match in ANY_URL_RE.finditer(command):
        url = match.group(0)
        parsed = urlparse(url)
        if parsed.hostname and _is_loopback_host(parsed.hostname):
            return url
    return ""


def _contains_loopback_target(command: str) -> str:
    url = _loopback_url(command)
    if url:
        return url
    try:
        tokens = shlex.split(command, posix=True)
    except ValueError:
        return ""
    program = _command_program(command)
    network_context = program in LOOPBACK_NETWORK_COMMANDS
    for token in tokens:
        if "=" in token and not token.startswith(("http://", "https://")):
            _name, value = token.split("=", 1)
            if _loopback_url(value):
                return value
            continue
        candidate = token
        if candidate.startswith("-"):
            continue
        if ":" in candidate or network_context:
            if _is_loopback_host(candidate):
                return token
    for index, token in enumerate(tokens[:-2]):
        if Path(token).name in {"sh", "bash", "zsh"} and tokens[index + 1] == "-c":
            nested = _contains_loopback_target(tokens[index + 2])
            if nested:
                return nested
    return ""


def _strip_redirection_prefix(token: str) -> str:
    candidate = re.sub(r"^\d*(?:>>?|<)", "", token, count=1)
    # ``shlex.split`` keeps adjacent command separators on redirect targets,
    # for example ``2>/dev/null;``. They are shell syntax, not filename bytes.
    return candidate.rstrip(";&|")


def _looks_like_path_token(token: str) -> bool:
    if not token or URL_TOKEN_RE.match(token) or token.startswith("-"):
        return False
    candidate = _strip_redirection_prefix(token)
    if not candidate or candidate in {".", ".."}:
        return bool(candidate)
    if candidate.startswith(("~", "/", "./", "../")):
        return True
    if "/" in candidate and ":" not in candidate and "%" not in candidate:
        return True
    # A bare credential file name (``.env``, ``team.env``, ``id_rsa``,
    # ``key.pem``) has no "/", so it is treated as a path here and the path
    # rules refuse it (the names come from access_control.sensitive_paths).
    # Words that only look like one are left alone: a folder name such as
    # ``secrets`` and the environment object of a code runtime such as
    # ``process.env`` are usually words in a search, and the sandbox is what
    # bounds the command anyway.
    return _bare_credential_file_name(candidate)


# ``<object>.env`` in code, not a file: process.env (Node), import.meta.env
# (Vite), Deno.env, Bun.env.
_CODE_ENV_OBJECTS = frozenset({"process", "import.meta", "deno", "bun"})


def _bare_credential_file_name(word: str) -> bool:
    lowered = word.lower()
    if lowered.startswith(".") and is_sensitive_path(Path(lowered)):
        return True
    if lowered.endswith(ENV_FILE_SUFFIX) and lowered[: -len(ENV_FILE_SUFFIX)] not in _CODE_ENV_OBJECTS:
        return True
    return lowered in PRIVATE_KEY_FILE_NAMES or Path(lowered).suffix in SENSITIVE_SUFFIXES


def _protected_runtime_control_shell_error(value: str) -> str:
    del value
    return (
        "Team control files, databases, and runtime records cannot be accessed through "
        "shell. Read them through get_runtime_configuration_context, get_team_composition "
        "and get_available_agent_types; change them through the typed runtime-settings, "
        "team composition, agent provisioning, and skill installation tools."
    )


def _validate_command_path_tokens(command: str, *, cwd: str) -> None:
    try:
        tokens = shlex.split(command, posix=True)
    except ValueError:
        return
    for token in tokens:
        if Path(token).name in {"sh", "bash", "zsh"}:
            continue
        candidate = _strip_redirection_prefix(token)
        if candidate:
            candidate_path = Path(candidate).expanduser()
            if not candidate_path.is_absolute():
                candidate_path = Path(cwd).expanduser().resolve() / candidate_path
        else:
            candidate_path = None
        if candidate_path is not None and is_protected_runtime_control_path(
            candidate_path
        ):
            raise PathPolicyError(
                _protected_runtime_control_shell_error(candidate),
                reason_code=PROTECTED_RUNTIME_CONTROL_DENIED,
            )
        if not _looks_like_path_token(candidate):
            continue
        path = Path(candidate).expanduser()
        if path in SAFE_SHELL_DEVICE_PATHS:
            continue
        if not path.is_absolute():
            path = Path(cwd).expanduser().resolve() / path
        ensure_allowed_path(str(path))
    for index, token in enumerate(tokens[:-2]):
        if Path(token).name in {"sh", "bash", "zsh"} and tokens[index + 1] == "-c":
            _validate_command_path_tokens(tokens[index + 2], cwd=cwd)

    # Shell snippets can hide writes behind interpreters or redirection. Treat
    # any remaining literal runtime-control reference conservatively here.
    if contains_protected_runtime_control_reference(command):
        raise PathPolicyError(
            _protected_runtime_control_shell_error(command),
            reason_code=PROTECTED_RUNTIME_CONTROL_DENIED,
        )


def _plan_command_with_rtk(
    command: str,
    *,
    cwd: str,
    env: Dict[str, str],
    timeout: int,
    cancel_event: threading.Event | None = None,
) -> RtkCommandPlan:
    plan = plan_rtk_command(
        command,
        cwd=cwd,
        env=env,
        timeout_seconds=timeout,
        missing_warning=RTK_FALLBACK_WARNING,
        cancel_event=cancel_event,
    )
    if plan.mode == "missing":
        logger.warning("%s: command=%r cwd=%s", RTK_FALLBACK_WARNING, command, cwd)
    elif plan.diagnostic:
        logger.warning(
            "command=%r, cwd=%s, diagnostic=%s | rtk rewrite unavailable; using tracked proxy",
            command,
            cwd,
            plan.diagnostic,
        )
    return plan


def _security_layer_mode() -> str:
    return os.getenv("SECURITY_LAYER_MODE", SECURITY_LAYER_MODE_BROKER).strip().lower()


def _openshell_workdir(host_work_dir: str) -> str:
    sandbox_mount = workspace_mount(os.environ)
    try:
        # Each person's sandbox holds their folder and team/ and agents/ at the
        # same places below the mount as under workspaces/ (openshell_sandboxes).
        relative = (
            Path(host_work_dir).expanduser().resolve().relative_to(workspaces_root())
        )
    except ValueError:
        configured = os.getenv("OPENSHELL_WORKDIR")
        return configured or sandbox_mount
    if relative.as_posix() == ".":
        return sandbox_mount
    return f"{sandbox_mount}/{relative.as_posix()}"


def _openshell_rtk_wrapped_command(command: str, marker: str) -> str:
    quoted_command = shlex.quote(command)
    quoted_warning = shlex.quote(OPENSHELL_RTK_FALLBACK_WARNING)
    quoted_marker = shlex.quote(marker)
    return (
        "if command -v rtk >/dev/null 2>&1; then "
        f"ua_rtk_output=$(rtk rewrite {quoted_command} 2>/dev/null || true); "
        "ua_rtk_command=$(printf '%s\\n' \"$ua_rtk_output\" | "
        "sed '/^[[:space:]]*$/d; /^[[:space:]]*\\[rtk\\]/d' | tail -n 1); "
        'if [ -n "$ua_rtk_command" ]; then '
        f"printf '%s%s\\n' {quoted_marker} rewrite >&2; "
        'exec sh -c "$ua_rtk_command"; '
        "fi; "
        f"printf '%s%s\\n' {quoted_marker} proxy >&2; "
        f"exec rtk proxy sh -c {quoted_command}; "
        "else "
        f"printf '%s%s\\n' {quoted_marker} missing >&2; "
        f"printf '%s\\n' {quoted_warning} >&2; "
        f"exec sh -c {quoted_command}; "
        "fi"
    )


def _extract_openshell_rtk_mode(text: str, marker: str) -> tuple[str, str]:
    mode = ""
    retained: list[str] = []
    for line in text.splitlines(keepends=True):
        stripped = line.strip()
        if stripped.startswith(marker):
            candidate = stripped[len(marker) :]
            if candidate in {"rewrite", "proxy", "missing"}:
                mode = candidate
                continue
        if stripped == OPENSHELL_RTK_FALLBACK_WARNING:
            continue
        retained.append(line)
    return mode, "".join(retained)


def _execute_with_openshell(
    command: str,
    *,
    command_work_dir: str,
    command_timeout: int,
    env: Dict[str, str],
    scope: Any,
    cancel_event: threading.Event | None = None,
) -> Dict[str, Any]:
    openshell_path = shutil.which("openshell", path=env.get("PATH"))
    if not openshell_path:
        return {
            "command": command,
            "executed_command": "",
            "exit_code": 127,
            "output": "",
            "error": "SECURITY_LAYER_MODE=openshell requires the openshell CLI on PATH.",
            "status": "error",
        }

    try:
        # One sandbox per person (custom_tools.openshell_sandboxes), created on
        # first use with copies of only that person's folders.
        sandbox_name = ensure_sandbox(openshell_path, scope, os.environ)
    except (OpenShellSandboxError, OSError, subprocess.SubprocessError) as exc:
        logger.error(
            "The person's OpenShell sandbox is unavailable. event=openshell_sandbox_unavailable "
            "person_id=%s error=%s",
            getattr(scope, "person_id", None),
            exc,
        )
        return {
            "command": command,
            "executed_command": "",
            "exit_code": 126,
            "output": "",
            "error": f"The OpenShell sandbox for this person is unavailable: {exc}",
            "status": "error",
            "reason_code": REASON_OPENSHELL_UNAVAILABLE,
        }
    marker = f"__UA_RTK_MODE_{uuid.uuid4().hex}__="
    sandbox_command = [
        openshell_path,
        "sandbox",
        "exec",
        "--no-tty",
        "--timeout",
        str(command_timeout),
        "--workdir",
        _openshell_workdir(command_work_dir),
    ]
    sandbox_command.extend(["-n", sandbox_name])
    sandbox_command.extend(
        ["--", "sh", "-lc", _openshell_rtk_wrapped_command(command, marker)]
    )

    completed = run_process(
        ProcessRequest(
            argv=tuple(sandbox_command),
            cwd=command_work_dir,
            env=env,
            timeout_seconds=command_timeout + 15,
        ),
        cancel_event=cancel_event,
    )
    stdout_mode, output = _extract_openshell_rtk_mode(completed.stdout, marker)
    stderr_mode, error = _extract_openshell_rtk_mode(completed.stderr, marker)
    rtk_mode = stderr_mode or stdout_mode
    if completed.termination_reason == "timeout":
        error = f"{error}\nOpenShell command timed out after {command_timeout} seconds".strip()
    elif completed.termination_reason == "cancelled":
        error = f"{error}\nOpenShell command cancelled".strip()

    result: Dict[str, Any] = {
        "command": command,
        "executed_command": shlex.join(sandbox_command),
        "exit_code": completed.exit_code,
        "output": output,
        "error": error,
        "status": "success" if completed.exit_code == 0 else "error",
        "termination_reason": completed.termination_reason,
        "duration_seconds": completed.duration_seconds,
    }
    if rtk_mode:
        result["rtk_mode"] = rtk_mode
    if rtk_mode == "missing":
        result["warning"] = OPENSHELL_RTK_FALLBACK_WARNING
        logger.warning(
            "%s: command=%r cwd=%s",
            OPENSHELL_RTK_FALLBACK_WARNING,
            command,
            command_work_dir,
        )
    return result


def _update_context_dir(current_dir: str, command: str) -> str:
    stripped = command.strip()
    if not stripped.startswith("cd "):
        return current_dir
    target = stripped[3:].strip()
    if not target:
        return current_dir
    expanded = Path(target).expanduser()
    if expanded.is_absolute():
        return str(expanded.resolve())
    return str((Path(current_dir).expanduser().resolve() / expanded).resolve())


CODE_SHELL_TOOL_ID = "code:shell"
REASON_OPENSHELL_UNAVAILABLE = "openshell_sandbox_unavailable"


def _code_access(scope: Any, *, tool_id: str, unconfined: bool) -> Any:
    """What this command may reach (custom_agents.code_access).

    Commands always run for an agent; embedded use without one (and tests)
    gets the task's folders as in single-person mode, which is logged.
    """

    from custom_agents.code_access import code_access, code_access_without_agent

    agent = get_agent_ref()
    if agent is None:
        logger.warning(
            "A command runs with no agent; using the task's folders as in single-person mode. "
            "event=code_access_without_agent tool_id=%s",
            tool_id,
        )
        return code_access_without_agent(scope, unconfined=unconfined)
    return code_access(
        agent, scope, tool_id=tool_id, unconfined=unconfined, data_folders=configured_data_folders(agent)
    )


def _planned_access(cmd_str: str, *, code_tool: str, unconfined: bool) -> tuple[Any, Any] | Dict[str, Any]:
    """The task's folders and what this command may reach, or the refusal result."""

    from custom_agents.code_access import CodeAccessRefused

    scope = current_workspace_scope()
    ensure_scope_home(scope)
    try:
        return scope, _code_access(scope, tool_id=code_tool, unconfined=unconfined)
    except CodeAccessRefused as exc:
        return _refused(cmd_str, str(exc), reason_code=exc.reason_code, termination_reason="spawn_error")


def _sandbox_note(planned_notes: tuple[str, ...], sandboxed: Any, completed: Any) -> str:
    """Folders left out of the sandbox, and what the sandbox allows when the command hit it."""

    notes = list(planned_notes)
    denial = sandbox_denial_note(sandboxed, completed.stderr) if completed.exit_code != 0 else None
    return " ".join([*notes, denial] if denial else notes)


def _refused(cmd_str: str, error: str, **extra: Any) -> Dict[str, Any]:
    """A command that never ran (exit code 126), with the reason and any extra fields."""

    return {
        "command": cmd_str,
        "executed_command": "",
        "exit_code": 126,
        "output": "",
        "error": error,
        "status": "error",
        **extra,
    }


_BLOCKED_CREDENTIAL_COMMAND_ERROR = (
    "Direct `{program}` shell usage is blocked because credentials are broker-owned. "
    "Use the secure_cli tool for credentialed GitHub, Google, Bitwarden, or credential_catalog actions. "
    "For GitHub, use first-class secure_cli actions when available, or provider='github' "
    "action='gh.exec' with argv=[...] for broker-policy-checked gh commands. "
    "For GitHub Projects v2 item listing, use action='project.item.list'; use action='api.post' "
    "with params.path='graphql' for other Projects operations when gh.exec is not suitable. "
    "raw `gh project ...` is not available; raw shell `gh ...` is not available."
)


def _policy_refusal(cmd_str: str) -> Dict[str, Any] | None:
    """Commands refused before anything runs: credential programs and local services."""

    program = _contains_blocked_credential_command(cmd_str)
    if program in BLOCKED_CREDENTIAL_COMMANDS:
        return _refused(cmd_str, _BLOCKED_CREDENTIAL_COMMAND_ERROR.format(program=program))
    loopback_target = _contains_loopback_target(cmd_str)
    if loopback_target:
        return _refused(
            cmd_str,
            "Direct shell access to localhost or loopback services is blocked. "
            "Use secure_cli or another broker-owned tool for broker, Sealbox, NATS, Postgres, "
            f"or other local-service interactions. Blocked target: {loopback_target}",
        )
    return None


def _command_work_dir(cmd_str: str, default_work_dir: str, requested: Any) -> str | Dict[str, Any]:
    """The command's working folder after the path checks, or the refusal."""

    try:
        work_dir = _resolve_work_dir(default_work_dir, requested)
        _validate_command_path_tokens(cmd_str, cwd=work_dir)
    except PermissionError as exc:
        extra = {"reason_code": exc.reason_code} if isinstance(exc, PathPolicyError) else {}
        return _refused(cmd_str, str(exc), **extra)
    return work_dir


def _command_environment(
    cmd_str: str, extra_env: Dict[str, str] | None
) -> tuple[Dict[str, str], Dict[str, Any] | None]:
    """The child's environment (with SKILLS_DIR and the caller's additions), and the refusal if it is unsafe."""

    env = _build_env()
    layout = current_instance_layout()
    if layout is not None:
        # Skills run bundled scripts as "${SKILLS_DIR}/<skill>/scripts/...".
        env["SKILLS_DIR"] = str(layout.skills_dir)
    if extra_env:
        env.update({str(key): str(value) for key, value in extra_env.items()})
    try:
        validate_environment_keys(env)
    except ValueError as exc:
        return env, _refused(cmd_str, str(exc), termination_reason="spawn_error")
    return env, None


def _run_in_sandbox(
    cmd_str: str,
    plan: Any,
    grants: Any,
    *,
    env: Dict[str, str],
    work_dir: str,
    timeout: int,
    cancel_event: threading.Event | None,
) -> Dict[str, Any]:
    """Run the planned command in the code sandbox and describe what happened."""

    layout = current_instance_layout()
    try:
        sandboxed = prepare_sandboxed_command(
            ("/bin/sh", "-c", plan.executed_command),
            task_roots=[str(root) for root in grants.read_write],
            env=env,
            network=grants.network,
            read_only_roots=[str(path) for path in (layout.agent_readable_skill_dirs if layout else ())],
            granted_read_roots=[str(path) for path in grants.read_only],
            unconfined=grants.unconfined,
        )
    except CodeSandboxUnavailable as exc:
        logger.error(
            "Refusing agent command because the code sandbox cannot be applied. "
            "event=code_sandbox_refused reason=%s",
            exc,
        )
        return _refused(cmd_str, str(exc), reason_code=exc.reason_code, termination_reason="spawn_error")
    try:
        completed = run_process(
            ProcessRequest(argv=sandboxed.argv, cwd=work_dir, env=sandboxed.env, timeout_seconds=timeout),
            cancel_event=cancel_event,
        )
    finally:
        release_sandboxed_command(sandboxed)
    error = completed.stderr
    if completed.termination_reason == "timeout":
        error = f"{error}\nCommand timed out after {timeout} seconds".strip()
    elif completed.termination_reason == "cancelled":
        error = f"{error}\nCommand cancelled".strip()
    result = {
        "command": cmd_str,
        "executed_command": plan.executed_command,
        "exit_code": completed.exit_code,
        "output": completed.stdout,
        "error": error,
        "status": "success" if completed.exit_code == 0 else "error",
        "rtk_mode": plan.mode,
        "termination_reason": completed.termination_reason,
        "duration_seconds": completed.duration_seconds,
        "sandbox": sandboxed.sandbox,
    }
    note = _sandbox_note(grants.notes, sandboxed, completed)
    if note:
        result["sandbox_note"] = note
    return result


def _execute_single_command(
    command: Union[str, Dict[str, Any]],
    *,
    default_work_dir: str,
    default_timeout: int,
    extra_env: Dict[str, str] | None = None,
    cancel_event: threading.Event | None = None,
    code_tool: str = CODE_SHELL_TOOL_ID,
    unconfined: bool = False,
) -> Dict[str, Any]:
    """Check, plan and run one command; every refusal comes back as a result, never an exception."""

    cmd_str, cmd_opts = _validate_command(command)
    refusal = _policy_refusal(cmd_str)
    if refusal is not None:
        return refusal
    command_timeout = int(cmd_opts.get("timeout") or default_timeout)
    command_work_dir = _command_work_dir(cmd_str, default_work_dir, cmd_opts.get("work_dir"))
    if isinstance(command_work_dir, dict):
        return command_work_dir
    env, env_refusal = _command_environment(cmd_str, extra_env)
    if env_refusal is not None:
        return env_refusal
    planned = _planned_access(cmd_str, code_tool=code_tool, unconfined=unconfined)
    if isinstance(planned, dict):
        return planned
    scope, grants = planned
    if _security_layer_mode() == SECURITY_LAYER_MODE_OPENSHELL and not grants.unconfined:
        return _execute_with_openshell(
            cmd_str,
            command_work_dir=command_work_dir,
            command_timeout=command_timeout,
            env=env,
            scope=scope,
            cancel_event=cancel_event,
        )
    plan = _plan_command_with_rtk(
        cmd_str, cwd=command_work_dir, env=env, timeout=command_timeout, cancel_event=cancel_event
    )
    if plan.cancelled:
        return {
            "command": cmd_str,
            "executed_command": "",
            "exit_code": 130,
            "output": "",
            "error": "Command cancelled before execution",
            "status": "error",
            "rtk_mode": plan.mode,
            "termination_reason": "cancelled",
        }
    result = _run_in_sandbox(
        cmd_str,
        plan,
        grants,
        env=env,
        work_dir=command_work_dir,
        timeout=command_timeout,
        cancel_event=cancel_event,
    )
    if plan.warning:
        result["warning"] = plan.warning
    if cmd_opts:
        result["options"] = cmd_opts
    return result


def execute_non_interactive_command(
    command: Union[str, Dict[str, Any]],
    *,
    default_work_dir: str,
    default_timeout: int,
    code_tool: str,
    extra_env: Dict[str, str] | None = None,
    unconfined: bool = False,
) -> Dict[str, Any]:
    """Execute one validated non-interactive command for another code tool.

    ``code_tool`` is the calling tool (``code:python`` for python_env), named
    in access logs. The public `shell` tool does not expose `extra_env` to
    agents.
    """
    return _execute_single_command(
        command,
        default_work_dir=default_work_dir,
        default_timeout=default_timeout,
        extra_env=extra_env,
        code_tool=code_tool,
        unconfined=unconfined,
    )


def _execute_commands(
    commands: List[Union[str, Dict[str, Any]]],
    *,
    parallel: bool,
    ignore_errors: bool,
    work_dir: str,
    timeout: int,
    unconfined: bool = False,
) -> List[Dict[str, Any]]:
    def execute_one(
        item: Union[str, Dict[str, Any]],
        current_work_dir: str,
        cancel_event: threading.Event | None = None,
    ) -> Dict[str, Any]:
        return _execute_single_command(
            item,
            default_work_dir=current_work_dir,
            default_timeout=timeout,
            cancel_event=cancel_event,
            unconfined=unconfined,
        )

    if parallel:
        cancel_event = threading.Event()
        ordered_results: list[Dict[str, Any] | None] = [None] * len(commands)
        with ThreadPoolExecutor() as executor:
            futures = {
                executor.submit(
                    execute_one,
                    command,
                    work_dir,
                    cancel_event,
                ): index
                for index, command in enumerate(commands)
            }
            for future in as_completed(futures):
                index = futures[future]
                try:
                    result = future.result()
                except CancelledError:
                    cmd_str, _ = _validate_command(commands[index])
                    result = {
                        "command": cmd_str,
                        "executed_command": "",
                        "exit_code": 130,
                        "output": "",
                        "error": "Command cancelled because another parallel command failed",
                        "status": "error",
                        "termination_reason": "cancelled",
                    }
                ordered_results[index] = result
                if not ignore_errors and result["status"] == "error":
                    cancel_event.set()
                    for pending in futures.keys():
                        pending.cancel()
        results: List[Dict[str, Any]] = []
        for index, result in enumerate(ordered_results):
            if result is None:
                cmd_str, _ = _validate_command(commands[index])
                result = {
                    "command": cmd_str,
                    "executed_command": "",
                    "exit_code": 130,
                    "output": "",
                    "error": "Command cancelled because another parallel command failed",
                    "status": "error",
                    "termination_reason": "cancelled",
                }
            results.append(result)
        return results

    results = []
    current_dir = work_dir
    for command in commands:
        cmd_str, _ = _validate_command(command)
        result = execute_one(command, current_dir)
        results.append(result)
        if result["status"] == "success":
            current_dir = _update_context_dir(current_dir, cmd_str)
        if not ignore_errors and result["status"] == "error":
            break
    return results


def _format_tool_content(
    results: List[Dict[str, Any]],
) -> Tuple[Literal["success", "error"], List[Dict[str, str]]]:
    success_count = sum(1 for result in results if result["status"] == "success")
    error_count = len(results) - success_count
    content: List[Dict[str, str]] = [
        {
            "text": (
                "Execution Summary:\n"
                f"Total commands: {len(results)}\n"
                f"Successful: {success_count}\n"
                f"Failed: {error_count}"
            )
        }
    ]
    for result in results:
        content.append(
            {
                "text": (
                    f"Command: {result['command']}\n"
                    f"Executed Command: {result.get('executed_command', result['command'])}\n"
                    f"Status: {result['status']}\n"
                    f"Exit Code: {result['exit_code']}\n"
                    f"RTK Mode: {result.get('rtk_mode', '')}\n"
                    f"Termination Reason: {result.get('termination_reason', '')}\n"
                    f"Duration Seconds: {result.get('duration_seconds', '')}\n"
                    f"Warning: {result.get('warning', '')}\n"
                    f"Output: {result['output']}\n"
                    f"Error: {result['error']}"
                    + (f"\nSandbox: {result['sandbox_note']}" if result.get("sandbox_note") else "")
                )
            }
        )
    status: Literal["success", "error"] = "success" if error_count == 0 else "error"
    return status, content


@tool
def shell(
    command: str | list[str | dict[str, Any]],
    parallel: bool = False,
    ignore_errors: bool = False,
    timeout: int | None = None,
    work_dir: str | None = None,
    unconfined: bool = False,
) -> dict[str, Any]:
    """
    Execute shell commands in a fail-fast non-interactive mode without a PTY.

    Runtime rules:
    - never rely on stdin, editors, pagers, TUI selectors, or confirmation prompts
    - add non-interactive flags such as `--yes`, `--no-input`, or `--batch` when
      the underlying command supports them
    - set a timeout for commands that might wait on external services
    - `Executed Command` may show an `rtk` or `rtk proxy sh -c` wrapper; that
      wrapper is expected and should not be treated as a failure unless the
      command status, exit code, or output reports an actual error

    Commands run through the project-owned process supervisor with stdin closed,
    separate output streams, process-tree timeout cleanup, and common
    non-interactive environment overrides, so TTY-aware flows fail fast instead
    of hanging an agent on an option screen.

    Commands run in a sandbox: they reach the folders you and the person you
    work for may use (read-only where you may only read) and operating-system
    files, with a private temporary HOME. Instance configuration, credentials,
    databases, other people's folders and other processes are not reachable.
    The network is open unless a person blocked internet access for this
    work; a request cannot lift a block, so tell the person.

    If a task needs more, ask with request_access and say exactly why:
    folder='<folder>' for a folder, or tool_id='code:unconfined' with
    level='full' to run outside the sandbox. With that approved, pass unconfined=true for the commands that
    need it; every such command is logged.
    """
    if command is None:
        return {
            "status": "error",
            "content": [{"text": "Command is required"}],
        }

    commands = _normalize_commands(command)
    timeout_seconds = (
        int(os.environ.get("SHELL_DEFAULT_TIMEOUT", "900"))
        if timeout is None
        else int(timeout)
    )
    home = ensure_scope_home(current_workspace_scope())
    resolved_work_dir = _resolve_work_dir(work_dir or str(home), None)

    try:
        # Whether this agent may run code at all is decided before the call
        # (custom_agents.access_runtime); path rules and the sandbox apply here.
        results = _execute_commands(
            commands,
            parallel=parallel,
            ignore_errors=ignore_errors,
            work_dir=resolved_work_dir,
            timeout=timeout_seconds,
            unconfined=bool(unconfined),
        )
        status, content = _format_tool_content(results)
        return {"status": status, "content": content}
    except Exception as exc:
        return {
            "status": "error",
            "content": [{"text": f"Non-interactive shell error: {exc}"}],
        }
