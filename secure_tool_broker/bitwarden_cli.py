"""Broker-side Bitwarden CLI configuration helpers."""

from __future__ import annotations

import json
import os
import pty
import re
import select
import shutil
import subprocess
import threading
import time
from dataclasses import dataclass
from typing import Any, Mapping
from urllib.parse import urlparse

from secure_tool_broker.credential_state import credential_owner_environment
from secure_tool_broker.redaction import parse_and_redact_stdout, redact_text
from secure_tools.providers import ToolResult


BITWARDEN_SERVER_URL_ENV_VARS = ("BITWARDEN_SERVER_URL", "BW_SERVER_URL")

_CONFIG_LOCK = threading.Lock()
_LAST_CONFIGURED_SERVER_URL: str | None = None
BW_SESSION_ASSIGNMENT_RE = re.compile(
    r"""(?x)
    (?:export\s+BW_SESSION|\$env:BW_SESSION|env:BW_SESSION)\s*=\s*
    (?P<quote>['"]?)
    (?P<session>[^'"\s;]+)
    (?P=quote)
    """
)


@dataclass(frozen=True)
class BitwardenSessionResult:
    result: ToolResult
    session: str | None = None


def _command_preview(command: list[str]) -> list[str]:
    return [command[0], *command[1:3], "..."] if len(command) > 3 else list(command)


def normalize_bitwarden_server_url(server_url: str) -> str:
    normalized = str(server_url or "").strip().rstrip("/")
    parsed = urlparse(normalized)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise ValueError("Bitwarden server URL must be an absolute http(s) URL.")
    return normalized


def _normalize_bitwarden_server_url_if_present(server_url: object) -> str | None:
    value = str(server_url or "").strip()
    if not value or value.lower() in {"null", "none"}:
        return None
    try:
        return normalize_bitwarden_server_url(value)
    except ValueError:
        return None


def bitwarden_server_url_from_env(env: Mapping[str, str] | None = None) -> str | None:
    env_mapping = env if env is not None else os.environ
    for name in BITWARDEN_SERVER_URL_ENV_VARS:
        value = str(env_mapping.get(name) or "").strip()
        if value:
            return normalize_bitwarden_server_url(value)
    return None


def configure_bitwarden_server_url(
    server_url: str,
    *,
    env: Mapping[str, str] | None = None,
    executable_path: str | None = None,
    timeout_seconds: int = 15,
) -> ToolResult:
    try:
        normalized_url = normalize_bitwarden_server_url(server_url)
    except ValueError as exc:
        return ToolResult(status="error", error=str(exc))

    executable = executable_path or shutil.which("bw")
    if not executable:
        return ToolResult(status="error", error="Required CLI is not installed or not on PATH: bw")

    current_status = read_bitwarden_cli_server_status(executable_path=executable, env=env, timeout_seconds=timeout_seconds)
    if not isinstance(current_status, ToolResult):
        current_url = current_status.get("server_url")
        current_vault_status = str(current_status.get("status") or "").strip().lower()
        if current_url == normalized_url:
            return ToolResult(
                status="success",
                content={
                    "provider": "bitwarden",
                    "action": "config.server",
                    "server_url": normalized_url,
                    "already_configured": True,
                    "current_status": current_vault_status or None,
                },
            )
        if current_url and current_vault_status in {"locked", "unlocked"}:
            return ToolResult(
                status="error",
                content={
                    "provider": "bitwarden",
                    "action": "config.server",
                    "server_url": normalized_url,
                    "current_server_url": current_url,
                    "current_status": current_vault_status,
                    "error_code": "bitwarden_server_mismatch_logged_in",
                },
                error=(
                    "Bitwarden CLI is already logged in to a different server. "
                    f"Configured server URL is {normalized_url}, but bw is logged in to {current_url}. "
                    "Log out of the broker-side Bitwarden CLI before changing servers, or update "
                    "security_layer.bitwarden_server_url to match the persisted CLI state."
                ),
            )

    command = [executable, "config", "server", normalized_url]
    try:
        completed = subprocess.run(
            command,
            cwd=os.getcwd(),
            env=credential_owner_environment(env=env),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout_seconds,
            check=False,
        )
    except subprocess.TimeoutExpired:
        return ToolResult(status="error", error=f"bw config server timed out after {timeout_seconds}s")
    except Exception as exc:
        return ToolResult(status="error", error=f"bw config server failed to start: {exc}")

    parsed_json, redacted_stdout = parse_and_redact_stdout(completed.stdout or "")
    content: dict[str, object] = {
        "provider": "bitwarden",
        "action": "config.server",
        "exit_code": completed.returncode,
        "command": _command_preview(command),
        "server_url": normalized_url,
    }
    if parsed_json is not None:
        content["json"] = parsed_json
    elif redacted_stdout:
        content["stdout"] = redacted_stdout
    stderr = redact_text(completed.stderr or "").strip()
    if stderr:
        content["stderr"] = stderr
    if completed.returncode != 0:
        return ToolResult(status="error", content=content, error=f"bw config server exited with code {completed.returncode}")
    return ToolResult(status="success", content=content)


def _run_bw(
    command: list[str],
    *,
    env: Mapping[str, str],
    input_text: str | None = None,
    timeout_seconds: int = 30,
) -> subprocess.CompletedProcess[str] | ToolResult:
    stdin_kwargs: dict[str, Any]
    if input_text is None:
        stdin_kwargs = {"stdin": subprocess.DEVNULL}
    else:
        stdin_kwargs = {"input": input_text}
    try:
        return subprocess.run(
            command,
            cwd=os.getcwd(),
            env=dict(env),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout_seconds,
            check=False,
            **stdin_kwargs,
        )
    except subprocess.TimeoutExpired:
        return ToolResult(status="error", error=f"bw command timed out after {timeout_seconds}s")
    except Exception as exc:
        return ToolResult(status="error", error=f"bw command failed to start: {exc}")


def _run_bw_with_pty(
    command: list[str],
    *,
    env: Mapping[str, str],
    timeout_seconds: int = 30,
) -> subprocess.CompletedProcess[str] | ToolResult:
    """Run bw attached to a pseudo-terminal for CLI builds that suppress piped output."""

    try:
        master_fd, slave_fd = pty.openpty()
    except Exception as exc:
        return ToolResult(status="error", error=f"bw pty could not be opened: {exc}")

    process: subprocess.Popen[bytes] | None = None
    output_chunks: list[bytes] = []
    slave_open = True
    try:
        process = subprocess.Popen(
            command,
            cwd=os.getcwd(),
            env=dict(env),
            stdin=slave_fd,
            stdout=slave_fd,
            stderr=slave_fd,
            close_fds=True,
        )
        os.close(slave_fd)
        slave_open = False
        deadline = time.monotonic() + timeout_seconds
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                process.kill()
                process.wait(timeout=5)
                return ToolResult(status="error", error=f"bw command timed out after {timeout_seconds}s")

            readable, _, _ = select.select([master_fd], [], [], min(0.1, remaining))
            if readable:
                try:
                    chunk = os.read(master_fd, 4096)
                except OSError:
                    chunk = b""
                if chunk:
                    output_chunks.append(chunk)

            if process.poll() is not None:
                while True:
                    readable, _, _ = select.select([master_fd], [], [], 0)
                    if not readable:
                        break
                    try:
                        chunk = os.read(master_fd, 4096)
                    except OSError:
                        break
                    if not chunk:
                        break
                    output_chunks.append(chunk)
                stdout = b"".join(output_chunks).decode("utf-8", errors="replace")
                return subprocess.CompletedProcess(command, process.returncode, stdout=stdout, stderr="")
    except Exception as exc:
        if process is not None and process.poll() is None:
            process.kill()
            process.wait(timeout=5)
        return ToolResult(status="error", error=f"bw pty command failed: {exc}")
    finally:
        if slave_open:
            try:
                os.close(slave_fd)
            except OSError:
                pass
        try:
            os.close(master_fd)
        except OSError:
            pass


def _bw_content(
    command: list[str],
    completed: subprocess.CompletedProcess[str],
    *,
    action: str,
    transport: str = "pipe",
) -> dict[str, object]:
    stdout = completed.stdout or ""
    stderr_raw = completed.stderr or ""
    parsed_json, redacted_stdout = parse_and_redact_stdout(stdout)
    content: dict[str, object] = {
        "provider": "bitwarden",
        "action": action,
        "exit_code": completed.returncode,
        "command": command,
        "transport": transport,
        "stdout_bytes": len(stdout.encode("utf-8", errors="replace")),
        "stderr_bytes": len(stderr_raw.encode("utf-8", errors="replace")),
        "stdout_lines": len(stdout.splitlines()),
        "stderr_lines": len(stderr_raw.splitlines()),
    }
    if parsed_json is not None:
        content["json"] = parsed_json
    elif redacted_stdout:
        content["stdout"] = redacted_stdout
    stderr = redact_text(stderr_raw).strip()
    if stderr:
        content["stderr"] = stderr
    return content


def _bw_auth_diagnostics(
    *,
    executable: str,
    command_env: Mapping[str, str],
    master_password: str,
    server_url_param: str,
    client_id_param: str,
    client_secret_param: str,
    force_login: bool = False,
) -> dict[str, object]:
    configured_server_url = str(command_env.get("BITWARDEN_SERVER_URL") or command_env.get("BW_SERVER_URL") or "").strip()
    return {
        "executable": executable,
        "cwd": os.getcwd(),
        "uid": os.getuid() if hasattr(os, "getuid") else None,
        "home": str(command_env.get("HOME") or ""),
        "bitwardencli_appdata_dir": str(command_env.get("BITWARDENCLI_APPDATA_DIR") or ""),
        "secure_tool_broker_credentials_dir": str(command_env.get("SECURE_TOOL_BROKER_CREDENTIALS_DIR") or ""),
        "bw_session_file": str(command_env.get("BW_SESSION_FILE") or ""),
        "bw_session_file_present": bool(command_env.get("BW_SESSION_FILE")),
        "server_url_param_present": bool(str(server_url_param or "").strip()),
        "configured_server_url_present": bool(configured_server_url),
        "configured_server_url": configured_server_url or None,
        "client_id_source": "form" if client_id_param else ("env" if command_env.get("BW_CLIENTID") else "missing"),
        "client_secret_source": "form" if client_secret_param else ("env" if command_env.get("BW_CLIENTSECRET") else "missing"),
        "force_login": bool(force_login),
        "master_password_present": bool(master_password),
        "master_password_chars": len(master_password),
        "master_password_has_surrounding_whitespace": master_password != master_password.strip(),
        "bw_session_env_present": bool(command_env.get("BW_SESSION")),
        "subprocess_stdin": "DEVNULL",
        "subprocess_stdout": "PIPE",
        "subprocess_stderr": "PIPE",
    }


def _bw_status_snapshot_from_completed(
    command: list[str],
    completed: subprocess.CompletedProcess[str],
    *,
    action: str,
) -> dict[str, object]:
    return _bw_content(command, completed, action=action)


def _read_bw_status_snapshot(
    *,
    executable: str,
    env: Mapping[str, str],
    action: str,
) -> dict[str, object]:
    command = [executable, "status"]
    completed = _run_bw(command, env=env, timeout_seconds=15)
    if isinstance(completed, ToolResult):
        content = dict(completed.content or {})
        content.setdefault("provider", "bitwarden")
        content["action"] = action
        if completed.error:
            content["error"] = redact_text(completed.error).strip()
        return content
    return _bw_status_snapshot_from_completed(command, completed, action=action)


def _attach_bw_status_diagnostics(
    content: dict[str, object],
    *,
    executable: str,
    env: Mapping[str, str],
    status_before: dict[str, object],
    auth_diagnostics: dict[str, object] | None = None,
) -> dict[str, object]:
    content["bw_status_before"] = status_before
    content["bw_status_after"] = _read_bw_status_snapshot(
        executable=executable,
        env=env,
        action="status.after_auth",
    )
    if auth_diagnostics is not None:
        content["bw_auth_diagnostics"] = auth_diagnostics
    return content


def read_bitwarden_cli_server_status(
    *,
    executable_path: str | None = None,
    env: Mapping[str, str] | None = None,
    timeout_seconds: int = 15,
) -> dict[str, Any] | ToolResult:
    """Read the configured bw server without mutating CLI state."""

    executable = executable_path or shutil.which("bw")
    if not executable:
        return ToolResult(status="error", error="Required CLI is not installed or not on PATH: bw")
    command_env = credential_owner_environment(env=env)

    status_command = [executable, "status"]
    status_completed = _run_bw(status_command, env=command_env, timeout_seconds=timeout_seconds)
    if isinstance(status_completed, ToolResult):
        return status_completed

    parsed_json, _redacted_stdout = parse_and_redact_stdout(status_completed.stdout or "")
    current_status = ""
    status_server_url = None
    if isinstance(parsed_json, dict):
        current_status = str(parsed_json.get("status") or "").strip().lower()
        status_server_url = _normalize_bitwarden_server_url_if_present(parsed_json.get("serverUrl"))
    if status_server_url:
        return {
            "server_url": status_server_url,
            "status": current_status,
            "source": "status",
        }

    config_command = [executable, "config", "server"]
    config_completed = _run_bw(config_command, env=command_env, timeout_seconds=timeout_seconds)
    if isinstance(config_completed, ToolResult):
        return config_completed
    if config_completed.returncode == 0:
        config_server_url = _normalize_bitwarden_server_url_if_present((config_completed.stdout or "").strip())
        if config_server_url:
            return {
                "server_url": config_server_url,
                "status": current_status,
                "source": "config.server",
            }

    if status_completed.returncode != 0:
        return ToolResult(
            status="error",
            content=_bw_content(status_command, status_completed, action="status"),
            error=f"bw status exited with code {status_completed.returncode}",
        )
    if config_completed.returncode != 0:
        return ToolResult(
            status="error",
            content=_bw_content(config_command, config_completed, action="config.server.read"),
            error=f"bw config server exited with code {config_completed.returncode}",
        )
    return {
        "server_url": None,
        "status": current_status,
        "source": "status",
    }


def _status_value(completed: subprocess.CompletedProcess[str]) -> str:
    parsed_json, _redacted_stdout = parse_and_redact_stdout(completed.stdout or "")
    if isinstance(parsed_json, dict):
        return str(parsed_json.get("status") or "").strip().lower()
    return ""


def _existing_session_from_env(env: Mapping[str, str]) -> str | None:
    session = str(env.get("BW_SESSION") or "").strip()
    return session or None


def _session_from_unlock_json(value: Any) -> str | None:
    if isinstance(value, str):
        return _extract_session_from_unlock_output(value)
    if isinstance(value, list):
        for child in value:
            session = _session_from_unlock_json(child)
            if session:
                return session
    if isinstance(value, dict):
        for key in ("raw", "session", "sessionKey", "session_key", "token"):
            candidate = str(value.get(key) or "").strip()
            if candidate:
                return candidate
        for key in ("data", "result", "response"):
            session = _session_from_unlock_json(value.get(key))
            if session:
                return session
        for child in value.values():
            session = _session_from_unlock_json(child)
            if session:
                return session
    return None


def _extract_session_from_unlock_output(stdout: str) -> str | None:
    output = str(stdout or "").strip()
    if not output:
        return None
    try:
        parsed_json = json.loads(output)
    except json.JSONDecodeError:
        parsed_json = None
    if parsed_json is not None:
        session = _session_from_unlock_json(parsed_json)
        if session:
            return session
    match = BW_SESSION_ASSIGNMENT_RE.search(output)
    if match:
        return match.group("session").strip()
    if "\n" not in output and " " not in output and "=" in output:
        return output
    return None


def _unlock_fallback_session(
    *,
    executable: str,
    unlock_env: Mapping[str, str],
) -> tuple[str | None, list[dict[str, object]] | ToolResult]:
    attempts: list[dict[str, object]] = []
    for action, command, transport in (
        ("unlock.response", [executable, "unlock", "--passwordenv", "BW_PASSWORD", "--response"], "pipe"),
        ("unlock.plain", [executable, "unlock", "--passwordenv", "BW_PASSWORD"], "pipe"),
        ("unlock.raw_pty", [executable, "unlock", "--passwordenv", "BW_PASSWORD", "--raw"], "pty"),
        ("unlock.response_pty", [executable, "unlock", "--passwordenv", "BW_PASSWORD", "--response"], "pty"),
        ("unlock.plain_pty", [executable, "unlock", "--passwordenv", "BW_PASSWORD"], "pty"),
    ):
        completed = (
            _run_bw_with_pty(command, env=unlock_env, timeout_seconds=30)
            if transport == "pty"
            else _run_bw(command, env=unlock_env, timeout_seconds=30)
        )
        if isinstance(completed, ToolResult):
            return None, completed
        attempts.append(_bw_content(command, completed, action=action, transport=transport))
        session = _extract_session_from_unlock_output(completed.stdout or "")
        if completed.returncode == 0 and session:
            return session, attempts
    return None, attempts


def login_and_unlock_bitwarden_with_api_key(
    *,
    master_password: str,
    client_id: str = "",
    client_secret: str = "",
    server_url: str = "",
    env: Mapping[str, str] | None = None,
    force_login: bool = False,
) -> BitwardenSessionResult:
    password = str(master_password or "")
    if not password:
        return BitwardenSessionResult(ToolResult(status="error", error="Bitwarden master password is required."))

    executable = shutil.which("bw")
    if not executable:
        return BitwardenSessionResult(ToolResult(status="error", error="Required CLI is not installed or not on PATH: bw"))

    command_env = credential_owner_environment(env=env)
    if client_id:
        command_env["BW_CLIENTID"] = str(client_id).strip()
    if client_secret:
        command_env["BW_CLIENTSECRET"] = str(client_secret).strip()
    force_login_cleared_bw_session_env = False
    if force_login and command_env.get("BW_SESSION"):
        command_env.pop("BW_SESSION", None)
        force_login_cleared_bw_session_env = True
    auth_diagnostics = _bw_auth_diagnostics(
        executable=executable,
        command_env=command_env,
        master_password=password,
        server_url_param=server_url,
        client_id_param=client_id,
        client_secret_param=client_secret,
        force_login=force_login,
    )
    auth_diagnostics["force_login_cleared_bw_session_env"] = force_login_cleared_bw_session_env
    if server_url:
        normalized_url = normalize_bitwarden_server_url(server_url)
        config_result = configure_bitwarden_server_url(normalized_url, env=command_env, executable_path=executable)
        if config_result.status != "success":
            content = dict(config_result.content or {})
            content["bw_auth_diagnostics"] = auth_diagnostics
            return BitwardenSessionResult(
                ToolResult(status=config_result.status, content=content, error=config_result.error)
            )
        command_env["BITWARDEN_SERVER_URL"] = normalized_url
        auth_diagnostics["configured_server_url_present"] = True
        auth_diagnostics["configured_server_url"] = normalized_url

    status_command = [executable, "status"]
    status_completed = _run_bw(status_command, env=command_env, timeout_seconds=15)
    if isinstance(status_completed, ToolResult):
        return BitwardenSessionResult(status_completed)
    status_before = _bw_status_snapshot_from_completed(
        status_command,
        status_completed,
        action="status.before_auth",
    )
    status = _status_value(status_completed)
    auth_diagnostics["initial_status"] = status or "unknown"

    if force_login:
        auth_diagnostics["forced_relogin_attempted"] = True
        if not command_env.get("BW_CLIENTID") or not command_env.get("BW_CLIENTSECRET"):
            content = _bw_content(status_command, status_completed, action="status")
            content["bw_status_before"] = status_before
            content["bw_auth_diagnostics"] = auth_diagnostics
            content["error_code"] = "bitwarden_force_login_missing_api_key"
            return BitwardenSessionResult(
                ToolResult(
                    status="error",
                    content=content,
                    error="Bitwarden API client ID and client secret are required for forced broker-side login.",
                )
            )
        logout_command = [executable, "logout"]
        logout_completed = _run_bw(logout_command, env=command_env, timeout_seconds=30)
        if isinstance(logout_completed, ToolResult):
            auth_diagnostics["force_logout_error"] = redact_text(str(logout_completed.error or "")).strip()
        else:
            logout_content = _bw_content(logout_command, logout_completed, action="logout")
            auth_diagnostics["force_logout_exit_code"] = logout_completed.returncode
            auth_diagnostics["force_logout_stdout_bytes"] = logout_content.get("stdout_bytes", 0)
            auth_diagnostics["force_logout_stderr_bytes"] = logout_content.get("stderr_bytes", 0)
        status = "unauthenticated"

    if status == "unlocked":
        existing_session = _existing_session_from_env(command_env)
        if existing_session:
            return BitwardenSessionResult(
                ToolResult(
                    status="success",
                    content={
                        "provider": "bitwarden",
                        "action": "login.reuse_unlocked_session",
                        "server_url": command_env.get("BITWARDEN_SERVER_URL") or command_env.get("BW_SERVER_URL"),
                    },
                ),
                session=existing_session,
            )

    if status == "unauthenticated":
        auth_diagnostics["login_attempted"] = True
        if not command_env.get("BW_CLIENTID") or not command_env.get("BW_CLIENTSECRET"):
            content = _bw_content(status_command, status_completed, action="status")
            content["bw_status_before"] = status_before
            content["bw_auth_diagnostics"] = auth_diagnostics
            return BitwardenSessionResult(
                ToolResult(
                    status="error",
                    content=content,
                    error="Bitwarden API client ID and client secret are required for broker-side login.",
                )
            )
        login_command = [executable, "login", "--apikey"]
        login_completed = _run_bw(login_command, env=command_env, timeout_seconds=30)
        if isinstance(login_completed, ToolResult):
            return BitwardenSessionResult(login_completed)
        auth_diagnostics["login_exit_code"] = login_completed.returncode
        auth_diagnostics["login_stdout_bytes"] = len((login_completed.stdout or "").encode("utf-8", errors="replace"))
        auth_diagnostics["login_stderr_bytes"] = len((login_completed.stderr or "").encode("utf-8", errors="replace"))
        if login_completed.returncode != 0:
            content = _attach_bw_status_diagnostics(
                _bw_content(login_command, login_completed, action="login.apikey"),
                executable=executable,
                env=command_env,
                status_before=status_before,
                auth_diagnostics=auth_diagnostics,
            )
            return BitwardenSessionResult(
                ToolResult(
                    status="error",
                    content=content,
                    error=f"bw login --apikey exited with code {login_completed.returncode}",
                )
            )
    else:
        auth_diagnostics["login_attempted"] = False
        auth_diagnostics["login_skip_reason"] = f"initial_status:{status or 'unknown'}"

    unlock_env = dict(command_env)
    unlock_env["BW_PASSWORD"] = password
    unlock_command = [executable, "unlock", "--passwordenv", "BW_PASSWORD", "--raw"]
    unlock_completed = _run_bw(unlock_command, env=unlock_env, timeout_seconds=30)
    if isinstance(unlock_completed, ToolResult):
        return BitwardenSessionResult(unlock_completed)
    auth_diagnostics["unlock_raw_exit_code"] = unlock_completed.returncode
    auth_diagnostics["unlock_raw_stdout_bytes"] = len((unlock_completed.stdout or "").encode("utf-8", errors="replace"))
    auth_diagnostics["unlock_raw_stderr_bytes"] = len((unlock_completed.stderr or "").encode("utf-8", errors="replace"))
    session = str(unlock_completed.stdout or "").strip()
    auth_diagnostics["unlock_raw_session_extracted"] = bool(session)
    if not session:
        existing_session = _existing_session_from_env(unlock_env)
        if unlock_completed.returncode == 0 and existing_session:
            return BitwardenSessionResult(
                ToolResult(
                    status="success",
                    content={
                        "provider": "bitwarden",
                        "action": "login.reuse_unlocked_session",
                        "server_url": command_env.get("BITWARDEN_SERVER_URL") or command_env.get("BW_SERVER_URL"),
                    },
                ),
                session=existing_session,
            )
        if unlock_completed.returncode == 0:
            fallback_session, fallback_attempts = _unlock_fallback_session(executable=executable, unlock_env=unlock_env)
            if isinstance(fallback_attempts, ToolResult):
                content = _attach_bw_status_diagnostics(
                    dict(fallback_attempts.content or {}),
                    executable=executable,
                    env=command_env,
                    status_before=status_before,
                    auth_diagnostics=auth_diagnostics,
                )
                return BitwardenSessionResult(
                    ToolResult(status=fallback_attempts.status, content=content, error=fallback_attempts.error)
                )
            if fallback_session:
                return BitwardenSessionResult(
                    ToolResult(
                        status="success",
                        content={
                            "provider": "bitwarden",
                            "action": "login.unlock_fallback",
                            "server_url": command_env.get("BITWARDEN_SERVER_URL") or command_env.get("BW_SERVER_URL"),
                            "fallback_actions": [str(attempt.get("action") or "") for attempt in fallback_attempts],
                        },
                    ),
                    session=fallback_session,
                )
    if unlock_completed.returncode != 0 or not session:
        content = _bw_content(unlock_command, unlock_completed, action="unlock")
        content["bw_auth_diagnostics"] = auth_diagnostics
        _attach_bw_status_diagnostics(
            content,
            executable=executable,
            env=command_env,
            status_before=status_before,
            auth_diagnostics=auth_diagnostics,
        )
        if unlock_completed.returncode == 0:
            content["error_code"] = "bitwarden_unlock_empty_session"
            if "fallback_attempts" not in content:
                content["fallback_attempts"] = fallback_attempts if "fallback_attempts" in locals() else []
        return BitwardenSessionResult(
            ToolResult(
                status="error",
                content=content,
                error=(
                    "bw unlock exited with code 0 but did not print a session."
                    if unlock_completed.returncode == 0
                    else f"bw unlock exited with code {unlock_completed.returncode}"
                ),
            )
        )
    return BitwardenSessionResult(
        ToolResult(
            status="success",
            content={
                "provider": "bitwarden",
                "action": "login.unlock",
                "server_url": command_env.get("BITWARDEN_SERVER_URL") or command_env.get("BW_SERVER_URL"),
            },
        ),
        session=session,
    )


def ensure_bitwarden_server_configured_from_env(env: Mapping[str, str] | None = None) -> ToolResult | None:
    global _LAST_CONFIGURED_SERVER_URL

    try:
        server_url = bitwarden_server_url_from_env(env)
    except ValueError as exc:
        return ToolResult(status="error", error=str(exc))
    if not server_url:
        return None

    with _CONFIG_LOCK:
        if _LAST_CONFIGURED_SERVER_URL == server_url:
            return None
        result = configure_bitwarden_server_url(server_url, env=env)
        if result.status == "success":
            _LAST_CONFIGURED_SERVER_URL = server_url
        return result if result.status != "success" else None


def set_bitwarden_server_url(server_url: str, *, env: dict[str, str] | None = None) -> ToolResult:
    global _LAST_CONFIGURED_SERVER_URL

    env_mapping = env if env is not None else os.environ
    result = configure_bitwarden_server_url(server_url, env=env_mapping)
    if result.status != "success":
        return result
    normalized_url = str((result.content or {}).get("server_url") or "").strip()
    if normalized_url:
        env_mapping["BITWARDEN_SERVER_URL"] = normalized_url
        _LAST_CONFIGURED_SERVER_URL = normalized_url
    return result
