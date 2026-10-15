"""Sealbox CLI helpers for broker-owned credential storage."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path
from typing import Any, Callable, Mapping

from secure_tool_broker.redaction import redact_json, redact_text
from secure_tools.providers import ToolResult


SEALBOX_AUTH_REQUIRED_PATTERNS = (
    "authentication token not configured",
    "unauthorized",
    "401",
    "forbidden",
    "server authentication token",
)


def sealbox_executable() -> str | None:
    configured = str(os.getenv("SEALBOX_CLI_PATH") or "").strip()
    if configured:
        return configured
    return shutil.which("sealbox-cli") or shutil.which("sealbox")


def run_sealbox(
    args: list[str],
    *,
    input_text: str | None = None,
    env: Mapping[str, str] | None = None,
    timeout_seconds: int = 30,
) -> subprocess.CompletedProcess[str] | ToolResult:
    executable = sealbox_executable()
    if not executable:
        return ToolResult(status="error", error="Required CLI is not installed or not on PATH: sealbox-cli")
    command = [executable, *args]
    stdin_kwargs: dict[str, Any] = {"stdin": subprocess.DEVNULL} if input_text is None else {"input": input_text}
    try:
        return subprocess.run(
            command,
            cwd=os.getcwd(),
            env=dict(env or os.environ),
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
        return ToolResult(status="error", error=f"sealbox-cli command timed out after {timeout_seconds}s")
    except Exception as exc:
        return ToolResult(status="error", error=f"sealbox-cli command failed to start: {exc}")


def command_preview(command: list[str]) -> list[str]:
    return [command[0], *command[1:3], "..."] if len(command) > 3 else list(command)


def parse_json_values(stdout: str) -> list[Any]:
    """Parse JSON objects/arrays from Sealbox stdout, skipping progress lines."""

    decoder = json.JSONDecoder()
    values: list[Any] = []
    index = 0
    text = stdout or ""
    while index < len(text):
        while index < len(text) and text[index] not in "[{":
            index += 1
        if index >= len(text):
            break
        try:
            value, end = decoder.raw_decode(text[index:])
        except json.JSONDecodeError:
            index += 1
            continue
        values.append(value)
        index += end
    return values


def first_json_value(stdout: str, predicate: Callable[[Any], bool] | None = None) -> Any | None:
    for value in parse_json_values(stdout):
        if predicate is None or predicate(value):
            return value
    return None


def is_auth_required(*, stdout: str = "", stderr: str = "", error: str = "") -> bool:
    haystack = "\n".join([stdout or "", stderr or "", error or ""]).lower()
    return any(pattern in haystack for pattern in SEALBOX_AUTH_REQUIRED_PATTERNS)


def redacted_completed_content(
    completed: subprocess.CompletedProcess[str],
    *,
    action: str,
    command: list[str],
    include_stdout: bool = True,
) -> dict[str, Any]:
    content: dict[str, Any] = {
        "provider": "sealbox",
        "action": action,
        "exit_code": completed.returncode,
        "command": command_preview(command),
    }
    if include_stdout:
        values = parse_json_values(completed.stdout or "")
        if values:
            content["json"] = redact_json(values[0] if len(values) == 1 else values)
        else:
            stdout = redact_text(completed.stdout or "").strip()
            if stdout:
                content["stdout"] = stdout
    stderr = redact_text(completed.stderr or "").strip()
    if stderr:
        content["stderr"] = stderr
    return content


def auth_required_result(content: Mapping[str, Any] | None = None) -> ToolResult:
    message = (
        "Sealbox authentication is required in the secure tool broker. Configure SEALBOX_URL, "
        "SEALBOX_TOKEN_FILE or SEALBOX_TOKEN, and the matching Sealbox key files for the broker."
    )
    result_content = dict(content or {})
    result_content.update(
        {
            "error_code": "auth_required",
            "provider": "sealbox",
            "auth_required": True,
            "auth_message": message,
            "user_action": "configure security_layer.sealbox or broker-only SEALBOX_* environment variables",
        }
    )
    return ToolResult(status="error", content=result_content, error=message)


def safe_key_component(value: object, *, fallback: str = "value") -> str:
    text = str(value or "").strip()
    safe = "".join(char if char.isascii() and (char.isalnum() or char in "_.:-") else "-" for char in text)
    safe = safe.strip("_.:-")
    return safe or fallback


def reject_unsupported_key(key: str) -> ToolResult | None:
    if "/" not in key:
        return None
    return ToolResult(
        status="error",
        error="Sealbox CLI keys containing '/' are not supported by the current API route shape.",
        content={"error_code": "sealbox_key_contains_slash", "key": key},
    )


def chmod_private_file(path: Path) -> None:
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass
