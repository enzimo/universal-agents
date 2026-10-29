"""Managed Python environment tool for agent-generated code."""

from __future__ import annotations

import ast
import json
import logging
import os
import re
import stat
import shlex
from contextlib import nullcontext
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Mapping, Optional

from strands import tool
from strands.types.tools import ToolContext

from custom_tools import file_access
from custom_tools.non_interactive_shell import execute_non_interactive_command
from custom_tools.tool_stop import ToolStop, tracked_tool_call
from custom_tools.path_policy import (
    contains_protected_runtime_control_reference,
    ensure_allowed_path,
    is_protected_runtime_control_path,
)
from custom_tools.runtime_context import get_agent_ref
from custom_tools.workspace_scope import current_instance_layout, current_workspace_scope
from python_env_lifecycle import (
    LAST_USED_FIELD, REQUEST_DEFAULT_FIELD, environment_lock, request_environment_name,
)
from runtime_settings.instance_layout import PERSON_PYTHON_ENVS_DIRNAME


logger = logging.getLogger(__name__)

PYTHON_ENV_TOOL_NAME = "python_env"

# Commands run as this code tool (named in access logs). The network comes
# from internet access (custom_agents.code_access), which installing needs.
CODE_PYTHON_TOOL_ID = "code:python"

MANIFEST_SCHEMA_VERSION = 1
DEFAULT_CODE_PATH = "src/main.py"
DEFAULT_CREATE_TIMEOUT_SECONDS = 120
DEFAULT_INSTALL_TIMEOUT_SECONDS = 900
DEFAULT_RUN_TIMEOUT_SECONDS = 300
MAX_TEXT_RESULT_CHARS = 20000
PYTHON_COMMAND_NAMES = {"python", "python3", "python.exe"}
PYTHON_RUN_OPTIONS = {"-u", "-B", "-I", "-S", "-s", "-E", "-O", "-OO"}
SAFE_NAME_RE = re.compile(r"[^A-Za-z0-9_.-]+")
SECRET_LITERAL_PATTERNS = (
    re.compile(r"\b(?:sk-[A-Za-z0-9_-]{16,}|gh[pousr]_[A-Za-z0-9_]{20,}|AKIA[0-9A-Z]{16})\b"),
    re.compile(r"(?i)\b(?:api[_-]?key|token|secret|password)\s*[:=]\s*['\"][^'\"]{8,}['\"]"),
)
RISKY_IMPORTS = {
    "ftplib",
    "http",
    "httpx",
    "os",
    "pathlib",
    "requests",
    "shlex",
    "shutil",
    "smtplib",
    "socket",
    "subprocess",
    "urllib",
}
RISKY_CALL_NAMES = {
    "compile",
    "eval",
    "exec",
    "open",
    "__import__",
}
RISKY_ATTR_CALLS = {
    "os.system",
    "os.popen",
    "subprocess.call",
    "subprocess.check_call",
    "subprocess.check_output",
    "subprocess.Popen",
    "subprocess.run",
    "shutil.rmtree",
    "socket.socket",
}


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _tool_result(status: str, text: str, *, payload: Mapping[str, Any] | None = None) -> dict[str, Any]:
    content = [{"text": text[:MAX_TEXT_RESULT_CHARS]}]
    if payload is not None:
        content.append({"text": json.dumps(payload, sort_keys=True, default=str)[:MAX_TEXT_RESULT_CHARS]})
    return {"status": status, "content": content}


def _configured_python_envs_dir() -> Path:
    """Environments live in the task's own folder: one person's are not another's.

    Work that runs for no person keeps its environments in the shared agents/
    folder.
    """

    home = current_workspace_scope().home
    return file_access.locate(home / PERSON_PYTHON_ENVS_DIRNAME, for_write=True).path


def _exists(path: Path) -> bool:
    """Whether a path in the task's folders exists (checked without following links)."""

    return file_access.status(path)[1] is not None


def _safe_env_name(env_name: str | None) -> str:
    raw = str(env_name or "").strip()
    if not raw:
        from custom_agents.access_runtime import verified_context_id

        agent = get_agent_ref()
        context_id = verified_context_id(agent) if agent is not None else None
        if context_id is None:
            raise ValueError("A default Python environment needs a verified request. Provide env_name for deliberate reuse.")
        return request_environment_name(context_id)
    slug = SAFE_NAME_RE.sub("-", raw).strip(".-")
    if not slug:
        raise ValueError("env_name must contain a letter or number")
    return slug[:80]


def _env_dir(env_name: str | None) -> Path:
    root = _configured_python_envs_dir()
    return file_access.locate(root / _safe_env_name(env_name), for_write=True).path


def _same_or_child(path: Path, parent: Path) -> bool:
    return path == parent or parent in path.parents


def _resolve_env_path(env_dir: Path, raw_path: str | None, *, default: str, for_write: bool = False) -> Path:
    rendered = str(raw_path or default).strip() or default
    candidate = Path(rendered).expanduser()
    if not candidate.is_absolute():
        candidate = env_dir / candidate
    resolved = ensure_allowed_path(str(candidate), for_write=for_write)
    if not _same_or_child(resolved, env_dir):
        raise PermissionError(f"Python environment path is outside env root: {resolved}")
    return resolved


def _relative_to_env(env_dir: Path, path: Path) -> str:
    return path.resolve().relative_to(env_dir.resolve()).as_posix()


def _relative_between(from_dir: Path, target: Path) -> str:
    return os.path.relpath(target.resolve(), from_dir.resolve())


def _venv_python_rel() -> str:
    return ".venv/bin/python"


def _is_python_command_name(raw_command: str) -> bool:
    name = Path(raw_command).name.lower()
    return name in PYTHON_COMMAND_NAMES or re.fullmatch(r"python\d+(?:\.\d+)?(?:\.exe)?", name) is not None


def _resolve_command_executable(env_dir: Path, run_work_dir: Path, executable: str) -> str:
    if "/" not in executable and "\\" not in executable:
        return executable
    candidate = Path(executable).expanduser()
    if not candidate.is_absolute():
        candidate = run_work_dir / candidate
    resolved = ensure_allowed_path(str(candidate), for_write=False)
    if not _same_or_child(resolved, env_dir):
        raise PermissionError(f"Python command executable is outside env root: {resolved}")
    return _relative_between(run_work_dir, resolved)


def _resolve_command_script(env_dir: Path, run_work_dir: Path, script: str) -> Path:
    if not script.endswith(".py"):
        raise ValueError("python_env command mode only supports Python script files.")
    candidate = Path(script).expanduser()
    if not candidate.is_absolute():
        candidate = run_work_dir / candidate
    resolved = ensure_allowed_path(str(candidate), for_write=False)
    if not _same_or_child(resolved, env_dir):
        raise PermissionError(f"Python command script is outside env root: {resolved}")
    if not _exists(resolved):
        raise FileNotFoundError(f"Python file not found: {resolved}")
    return resolved


def _prepare_python_command(env_dir: Path, *, command: str, run_work_dir: Path) -> tuple[str, dict[str, Any] | None]:
    try:
        tokens = shlex.split(command)
    except ValueError as exc:
        raise ValueError(f"Invalid python_env command syntax: {exc}") from exc
    if not tokens:
        raise ValueError("python_env command mode requires a Python script command.")
    executable = tokens[0]
    if not _is_python_command_name(executable):
        raise ValueError(
            "python_env command mode only supports Python interpreter commands "
            "that execute a .py script."
        )

    script_index: int | None = None
    for index, token in enumerate(tokens[1:], start=1):
        if token == "--":
            continue
        if token in {"-c", "-m"} or token.startswith("-c") or token.startswith("-m"):
            raise ValueError("python_env command mode blocks inline and module execution.")
        if token.startswith("-"):
            if token not in PYTHON_RUN_OPTIONS:
                raise ValueError(f"python_env command mode does not allow Python option {token!r}.")
            continue
        script_index = index
        break

    if script_index is None:
        raise ValueError("python_env command mode requires a .py script file.")

    script_path = _resolve_command_script(env_dir, run_work_dir, tokens[script_index])
    inspection = _inspect_file_if_python(script_path, env_dir)
    tokens[0] = _resolve_command_executable(env_dir, run_work_dir, executable)
    tokens[script_index] = _relative_between(run_work_dir, script_path)
    return " ".join(shlex.quote(item) for item in tokens), inspection


def _default_python_executable(explicit: str | None = None) -> str:
    if explicit and str(explicit).strip():
        return str(explicit).strip()
    configured = os.getenv("PYTHON_ENV_PYTHON")
    if configured and configured.strip():
        return configured.strip()
    return "python3"


def _load_manifest(env_dir: Path) -> dict[str, Any]:
    manifest_path = env_dir / "manifest.json"
    try:
        loaded = json.loads(file_access.read_text(manifest_path)[1])
    except FileNotFoundError:
        return {}
    except Exception as exc:
        logger.warning("Failed to load python env manifest. env_dir=%s error=%s", env_dir, exc)
        return {}
    return loaded if isinstance(loaded, dict) else {}


def _write_manifest(env_dir: Path, manifest: Mapping[str, Any]) -> None:
    payload = dict(manifest)
    payload["schema_version"] = MANIFEST_SCHEMA_VERSION
    payload["updated_at"] = _utc_now()
    file_access.write_text(env_dir / "manifest.json", json.dumps(payload, indent=2, sort_keys=True) + "\n")


def _base_manifest(env_name: str, env_dir: Path, *, python_executable: str | None = None) -> dict[str, Any]:
    existing = _load_manifest(env_dir)
    now = _utc_now()
    return {
        "schema_version": MANIFEST_SCHEMA_VERSION,
        "env_name": env_name,
        "env_dir": str(env_dir),
        "venv_dir": str(env_dir / ".venv"),
        "python_executable": python_executable or existing.get("python_executable") or _default_python_executable(),
        "created_at": existing.get("created_at") or now,
        "updated_at": now,
        "last_action": existing.get("last_action"),
    }


def _normalize_packages(packages: Any) -> list[str]:
    if packages is None or packages == "":
        return []
    if isinstance(packages, str):
        return shlex.split(packages)
    if isinstance(packages, Mapping):
        raise ValueError("packages must be a string or list of package specifiers")
    if isinstance(packages, Iterable):
        return [str(item).strip() for item in packages if str(item).strip()]
    raise ValueError("packages must be a string or list of package specifiers")


def _normalize_args(args: Any) -> list[str]:
    if args is None or args == "":
        return []
    if isinstance(args, str):
        return shlex.split(args)
    if isinstance(args, Mapping):
        raise ValueError("args must be a string or list")
    if isinstance(args, Iterable):
        return [str(item) for item in args]
    raise ValueError("args must be a string or list")


def _command_result_payload(result: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "command": result.get("command"),
        "executed_command": result.get("executed_command"),
        "exit_code": result.get("exit_code"),
        "status": result.get("status"),
        "warning": result.get("warning"),
        "output": str(result.get("output") or "")[:MAX_TEXT_RESULT_CHARS],
        "error": str(result.get("error") or "")[:MAX_TEXT_RESULT_CHARS],
        "sandbox": result.get("sandbox"),
        "sandbox_note": result.get("sandbox_note"),
    }


def _attribute_name(node: ast.AST) -> str:
    parts: list[str] = []
    current: ast.AST | None = node
    while isinstance(current, ast.Attribute):
        parts.append(current.attr)
        current = current.value
    if isinstance(current, ast.Name):
        parts.append(current.id)
    return ".".join(reversed(parts))


def _names_closed_area(literal: str, base_dir: Path | None) -> bool:
    """Whether a path-like string literal resolves into an area closed to agents.

    Defense in depth only: the code sandbox is the boundary, and code can build
    paths at run time. Relative literals resolve from ``base_dir``, the
    folder the code runs in.
    """

    text = literal.strip()
    if base_dir is None or "/" not in text or "\n" in text or len(text) > 4096:
        return False
    candidate = Path(text).expanduser()
    if not candidate.is_absolute():
        candidate = base_dir / candidate
    try:
        return is_protected_runtime_control_path(candidate)
    except (OSError, ValueError):
        return False


def inspect_python_source(
    source: str, *, path: str | None = None, base_dir: Path | None = None
) -> dict[str, Any]:
    """Inspect generated Python source with deterministic lightweight checks.

    ``base_dir`` is the folder the code runs in; relative path literals are
    resolved from it.
    """
    warnings: list[str] = []
    errors: list[str] = []
    try:
        tree = ast.parse(source, filename=path or "<python_env>")
    except SyntaxError as exc:
        return {
            "path": path,
            "status": "error",
            "errors": [f"SyntaxError line {exc.lineno}: {exc.msg}"],
            "warnings": [],
        }

    imports: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imports.update(alias.name.split(".", 1)[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imports.add(node.module.split(".", 1)[0])
        elif isinstance(node, ast.Call):
            call_name = _attribute_name(node.func)
            if isinstance(node.func, ast.Name) and node.func.id in RISKY_CALL_NAMES:
                warnings.append(f"Uses dynamic or filesystem call `{node.func.id}`.")
            elif call_name in RISKY_ATTR_CALLS:
                warnings.append(f"Uses process/filesystem/network call `{call_name}`.")
        elif isinstance(node, ast.Constant) and isinstance(node.value, str):
            literal = node.value
            if contains_protected_runtime_control_reference(literal) or _names_closed_area(
                literal, base_dir
            ):
                errors.append(
                    "Python source references a protected runtime control path; "
                    "use the typed staged runtime-settings workflow instead."
                )
            for pattern in SECRET_LITERAL_PATTERNS:
                if pattern.search(literal):
                    warnings.append("Contains a string literal that resembles a secret or credential.")
                    break

    risky_imports = sorted(imports.intersection(RISKY_IMPORTS))
    if risky_imports:
        warnings.append(f"Imports modules with filesystem, process, or network capability: {', '.join(risky_imports)}.")

    deduped_warnings = list(dict.fromkeys(warnings))
    return {
        "path": path,
        "status": "ok" if not errors else "error",
        "errors": errors,
        "warnings": deduped_warnings,
    }


def _inspect_file_if_python(path: Path, env_dir: Path) -> dict[str, Any] | None:
    if path.suffix != ".py":
        return None
    _location, source = file_access.read_text(path)
    return inspect_python_source(
        source, path=_relative_to_env(env_dir, path), base_dir=env_dir
    )


def _ensure_env_dirs(env_dir: Path) -> None:
    for folder in (env_dir / "src", env_dir / "artifacts"):
        file_access.make_directories(folder)


def _create_env(
    env_name: str,
    env_dir: Path,
    *,
    python_executable: str | None,
    recreate: bool,
    tool_stop: ToolStop | None,
) -> dict[str, Any]:
    if recreate:
        file_access.remove_tree(env_dir)
    _ensure_env_dirs(env_dir)
    manifest = _base_manifest(env_name, env_dir, python_executable=_default_python_executable(python_executable))
    if not _exists(env_dir / ".venv"):
        create_command = f"{shlex.quote(manifest['python_executable'])} -m venv --copies .venv"
        result = execute_non_interactive_command(
            {"command": create_command, "timeout": DEFAULT_CREATE_TIMEOUT_SECONDS},
            default_work_dir=str(env_dir),
            default_timeout=DEFAULT_CREATE_TIMEOUT_SECONDS,
            code_tool=CODE_PYTHON_TOOL_ID,
            tool_stop=tool_stop,
            tool_name=PYTHON_ENV_TOOL_NAME,
        )
        manifest["last_create_result"] = _command_result_payload(result)
        if result.get("status") != "success":
            _write_manifest(env_dir, {**manifest, "last_action": "create_failed"})
            return {"manifest": manifest, "command_result": _command_result_payload(result)}
    manifest["last_action"] = "create"
    _write_manifest(env_dir, manifest)
    return {"manifest": manifest, "command_result": manifest.get("last_create_result")}


def _install_packages(
    env_dir: Path, packages: Any, *, timeout: int | None, unconfined: bool, tool_stop: ToolStop | None
) -> dict[str, Any]:
    package_args = _normalize_packages(packages)
    if not package_args:
        raise ValueError("install action requires at least one package")
    quoted = " ".join(shlex.quote(item) for item in package_args)
    command = f"{_venv_python_rel()} -m pip install {quoted}"
    result = execute_non_interactive_command(
        {"command": command, "timeout": timeout or DEFAULT_INSTALL_TIMEOUT_SECONDS},
        default_work_dir=str(env_dir),
        default_timeout=timeout or DEFAULT_INSTALL_TIMEOUT_SECONDS,
        code_tool=CODE_PYTHON_TOOL_ID,
        unconfined=unconfined,
        tool_stop=tool_stop,
        tool_name=PYTHON_ENV_TOOL_NAME,
    )
    return {"packages": package_args, "command_result": _command_result_payload(result)}


def _write_file(env_dir: Path, *, raw_path: str | None, content: str) -> dict[str, Any]:
    target = _resolve_env_path(env_dir, raw_path, default=DEFAULT_CODE_PATH, for_write=True)
    file_access.write_text(target, content)
    inspection = _inspect_file_if_python(target, env_dir)
    return {
        "path": _relative_to_env(env_dir, target),
        "bytes_written": len(content.encode("utf-8")),
        "inspection": inspection,
    }


def _run(
    env_dir: Path,
    *,
    raw_path: str | None,
    command: str | None,
    args: Any,
    work_dir: str | None,
    timeout: int | None,
    unconfined: bool,
    tool_stop: ToolStop | None,
) -> dict[str, Any]:
    run_work_dir = _resolve_env_path(env_dir, work_dir, default=".", for_write=False)
    inspection = None
    if command and str(command).strip():
        try:
            run_command, inspection = _prepare_python_command(env_dir, command=str(command).strip(), run_work_dir=run_work_dir)
        except (FileNotFoundError, PermissionError, ValueError) as exc:
            return {
                "inspection": None,
                "command_result": {
                    "status": "error",
                    "exit_code": 126,
                    "output": "",
                    "error": str(exc),
                },
            }
        if inspection and inspection.get("errors"):
            return {
                "inspection": inspection,
                "command_result": {
                    "status": "error",
                    "exit_code": 126,
                    "output": "",
                    "error": "Python source inspection failed; fix syntax errors before execution.",
                },
            }
    else:
        target = _resolve_env_path(env_dir, raw_path, default=DEFAULT_CODE_PATH, for_write=False)
        if not _exists(target):
            raise FileNotFoundError(f"Python file not found: {target}")
        inspection = _inspect_file_if_python(target, env_dir)
        if inspection and inspection.get("errors"):
            return {
                "inspection": inspection,
                "command_result": {
                    "status": "error",
                    "exit_code": 126,
                    "output": "",
                    "error": "Python source inspection failed; fix syntax errors before execution.",
                },
            }
        rendered_args = " ".join(shlex.quote(item) for item in _normalize_args(args))
        venv_python = shlex.quote(_relative_between(run_work_dir, env_dir / _venv_python_rel()))
        script = shlex.quote(_relative_between(run_work_dir, target))
        run_command = f"{venv_python} {script}{(' ' + rendered_args) if rendered_args else ''}"

    result = execute_non_interactive_command(
        {"command": run_command, "timeout": timeout or DEFAULT_RUN_TIMEOUT_SECONDS, "work_dir": str(run_work_dir)},
        default_work_dir=str(env_dir),
        default_timeout=timeout or DEFAULT_RUN_TIMEOUT_SECONDS,
        code_tool=CODE_PYTHON_TOOL_ID,
        unconfined=unconfined,
        tool_stop=tool_stop,
        tool_name=PYTHON_ENV_TOOL_NAME,
    )
    return {"inspection": inspection, "command_result": _command_result_payload(result)}


def _list_envs() -> dict[str, Any]:
    root = _configured_python_envs_dir()
    try:
        _location, entries = file_access.list_directory(root)
    except FileNotFoundError:
        return {"root": str(root), "envs": []}
    envs: list[dict[str, Any]] = []
    for name, entry in sorted(entries):
        if not stat.S_ISDIR(entry.st_mode):
            continue  # links are listed as links and skipped
        child = root / name
        manifest = _load_manifest(child)
        envs.append(
            {
                "env_name": manifest.get("env_name") or child.name,
                "env_dir": str(child),
                "created_at": manifest.get("created_at"),
                "updated_at": manifest.get("updated_at"),
                REQUEST_DEFAULT_FIELD: manifest.get(REQUEST_DEFAULT_FIELD, False),
                LAST_USED_FIELD: manifest.get(LAST_USED_FIELD),
                "last_action": manifest.get("last_action"),
                "venv_exists": _exists(child / ".venv"),
            }
        )
    return {"root": str(root), "envs": envs}


@tool(context=True)
def python_env(
    action: str,
    tool_context: ToolContext,
    env_name: Optional[str] = None,
    path: Optional[str] = None,
    content: Optional[str] = None,
    code: Optional[str] = None,
    command: Optional[str] = None,
    packages: Optional[list[str] | str] = None,
    args: Optional[list[str] | str] = None,
    work_dir: Optional[str] = None,
    timeout: Optional[int] = None,
    python_executable: Optional[str] = None,
    recreate: bool = False,
    reason: Optional[str] = None,
    unconfined: bool = False,
) -> dict[str, Any]:
    """
    Create and use managed Python virtual environments for generated code.

    Omit env_name to use this request's default environment, shared with its
    helpers. Each request and each scheduled run gets its own default. Trusted
    maintenance deletes a default environment as a whole after 7 days without
    use. Give env_name explicitly to reuse an environment across requests and
    keep it until you delete it. Save lasting results outside default envs.

    Actions:
    - `create`: create `python_envs/<env_name>/.venv` plus `src/` and `artifacts/` in
      this task's folder (the person's own folder, or agents/ for work with no person).
    - `write_file`: write UTF-8 text under the env directory; `.py` files are inspected.
    - `inspect`: inspect supplied `code` or a Python file without executing it.
    - `install`: install packages into the env venv with pip. It changes the env, so it
      needs write access to Python environments, and it reaches the package index
      through internet access.
    - `run`: run a Python file with the env interpreter, or a Python script command in the env.
    - `list`: list managed envs.
    - `delete`: delete one managed env.

    Generated files and script commands stay in this task's folders. Commands run
    in the same sandbox as the shell tool: the folders you and the person you work
    for may use, read-only operating-system files and team skills, with no access
    to control files, credentials, databases, other people's folders, or other
    processes. The network is open unless a person blocked internet access for this
    work; a request cannot lift a block, so tell the person.
    OpenShell mode uses its own sandbox. If a task needs more, ask with
    request_access and say exactly why; with tool_id='code:unconfined' approved,
    `install` and `run` accept unconfined=true to run outside the sandbox.
    """
    # The call counts as running until its commands end, keeps its task (and
    # person) throughout, and its commands stop when the task stops
    # (custom_tools.tool_stop). Nothing runs once the task was stopped.
    with tracked_tool_call(tool_context, PYTHON_ENV_TOOL_NAME) as tool_stop:
        if tool_stop is not None and tool_stop.is_requested():
            return _tool_result("error", "python_env did not run because the task was stopped.")
        return _python_env_action(
            action,
            env_name=env_name,
            path=path,
            content=content,
            code=code,
            command=command,
            packages=packages,
            args=args,
            work_dir=work_dir,
            timeout=timeout,
            python_executable=python_executable,
            recreate=recreate,
            reason=reason,
            unconfined=unconfined,
            tool_stop=tool_stop,
        )


def _python_env_action(
    action: str,
    *,
    env_name: Optional[str],
    path: Optional[str],
    content: Optional[str],
    code: Optional[str],
    command: Optional[str],
    packages: Optional[list[str] | str],
    args: Optional[list[str] | str],
    work_dir: Optional[str],
    timeout: Optional[int],
    python_executable: Optional[str],
    recreate: bool,
    reason: Optional[str],
    unconfined: bool,
    tool_stop: ToolStop | None,
) -> dict[str, Any]:
    """Carry out one python_env action; its commands stop with ``tool_stop``."""

    normalized_action = str(action or "").strip().lower()
    if normalized_action not in {"create", "write_file", "inspect", "install", "run", "list", "delete"}:
        return _tool_result("error", f"Unsupported python_env action: {action}")

    try:
        if normalized_action == "list":
            payload = _list_envs()
            return _tool_result("success", f"Found {len(payload['envs'])} managed Python env(s).", payload=payload)

        safe_name = _safe_env_name(env_name)
        env_dir = _env_dir(safe_name)
        layout = current_instance_layout()
        # Embedded callers with an explicit name have no instance maintenance.
        # Runtime environments share one lock with whole-environment cleanup.
        lock = environment_lock(
            layout, env_dir, cancelled=tool_stop.is_requested if tool_stop is not None else None,
        ) if layout is not None else nullcontext()
        with lock:
            if tool_stop is not None and tool_stop.is_requested():
                return _tool_result("error", "python_env did not run because the task was stopped.")
            try:
                return _python_env_in_directory(
                    normalized_action, safe_name, env_dir, path=path, content=content, code=code,
                    command=command, packages=packages, args=args, work_dir=work_dir, timeout=timeout,
                    python_executable=python_executable, recreate=recreate, reason=reason,
                    unconfined=unconfined, tool_stop=tool_stop,
                )
            finally:
                if normalized_action != "delete" and _exists(env_dir):
                    manifest = _load_manifest(env_dir)
                    manifest[REQUEST_DEFAULT_FIELD] = not bool(str(env_name or "").strip())
                    manifest[LAST_USED_FIELD] = _utc_now()
                    _write_manifest(env_dir, manifest)
    except Exception as exc:
        logger.warning(
            "Managed Python env action failed. event=python_env_action_failed action=%s env_name=%s error=%s",
            normalized_action, env_name, exc,
        )
        return _tool_result("error", f"python_env {normalized_action or '<empty>'} failed: {exc}")


def _python_env_in_directory(
    normalized_action: str, safe_name: str, env_dir: Path, *, path: Optional[str],
    content: Optional[str], code: Optional[str], command: Optional[str],
    packages: Optional[list[str] | str], args: Optional[list[str] | str], work_dir: Optional[str],
    timeout: Optional[int], python_executable: Optional[str], recreate: bool, reason: Optional[str],
    unconfined: bool, tool_stop: ToolStop | None,
) -> dict[str, Any]:
    """One action while the environment's use/cleanup lock is held."""

    if normalized_action == "delete":
        file_access.remove_tree(env_dir)
        logger.info("Managed Python env deleted. event=python_env_deleted env_name=%s reason=%s", safe_name, reason)
        return _tool_result("success", f"Deleted Python env `{safe_name}`.", payload={"env_name": safe_name})

    if normalized_action == "create":
        payload = _create_env(
            safe_name, env_dir, python_executable=python_executable, recreate=recreate, tool_stop=tool_stop
        )
        command_result = payload.get("command_result") or {}
        status = "success" if not command_result or command_result.get("status") == "success" else "error"
        logger.info("Managed Python env create completed. event=python_env_create env_name=%s status=%s", safe_name, status)
        return _tool_result(status, f"Python env `{safe_name}` create status: {status}.", payload=payload)

    if not _exists(env_dir / ".venv"):
        create_payload = _create_env(
            safe_name, env_dir, python_executable=python_executable, recreate=False, tool_stop=tool_stop
        )
        command_result = create_payload.get("command_result") or {}
        if command_result and command_result.get("status") != "success":
            return _tool_result("error", f"Failed to create Python env `{safe_name}`.", payload=create_payload)

    manifest = _base_manifest(safe_name, env_dir, python_executable=python_executable)
    if normalized_action == "install":
        payload = _install_packages(
            env_dir, packages, timeout=timeout, unconfined=bool(unconfined), tool_stop=tool_stop
        )
    elif normalized_action == "write_file":
        file_content = content if content is not None else code
        if file_content is None:
            raise ValueError("write_file action requires content or code")
        payload = _write_file(env_dir, raw_path=path, content=str(file_content))
    elif normalized_action == "inspect":
        if code is not None:
            payload = {
                "inspection": inspect_python_source(
                    str(code), path=path, base_dir=env_dir
                )
            }
        else:
            target = _resolve_env_path(env_dir, path, default=DEFAULT_CODE_PATH, for_write=False)
            payload = {"inspection": _inspect_file_if_python(target, env_dir)}
            if payload["inspection"] is None:
                raise ValueError("inspect action requires Python code or a .py path")
    else:
        payload = _run(
            env_dir,
            raw_path=path,
            command=command,
            args=args,
            work_dir=work_dir,
            timeout=timeout,
            unconfined=bool(unconfined),
            tool_stop=tool_stop,
        )

    manifest["last_action"] = normalized_action
    _write_manifest(env_dir, manifest)
    status = "success"
    command_result = payload.get("command_result") if isinstance(payload, dict) else None
    inspection = payload.get("inspection") if isinstance(payload, dict) else None
    if command_result and command_result.get("status") != "success":
        status = "error"
    if inspection and inspection.get("errors"):
        status = "error"
    logger.info(
        "Managed Python env action completed. event=python_env_action env_name=%s action=%s status=%s reason=%s",
        safe_name,
        normalized_action,
        status,
        reason,
    )
    return _tool_result(status, f"Python env `{safe_name}` action `{normalized_action}` completed with status {status}.", payload=payload)
