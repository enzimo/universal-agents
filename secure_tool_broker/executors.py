"""Broker-side command executors for registered secure tool providers."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Mapping

from secure_tool_broker.auth_models import AuthStatus
from access_catalog.cli_commands import argv_from_params
from secure_tool_broker.cli_passthrough import (
    CheckedCommand,
    CommandRefused,
    check_command_line,
)
from secure_tool_broker.task_files import TaskFileRejected
from secure_tool_broker.bitwarden_cli import ensure_bitwarden_server_configured_from_env, login_and_unlock_bitwarden_with_api_key
from secure_tool_broker.credential_catalog import execute_credential_catalog_action, execute_secure_requests_action
from secure_tool_broker.credential_state import (
    USERS_DIRNAME,
    active_credential_owner_id,
    broker_credential_file,
    credential_owner_environment,
    credential_owner_context,
    write_private_broker_file,
)
from secure_tool_broker.credential_state_store import (
    credential_state_backend_mode,
    read_credential_state,
    write_credential_state,
)
from secure_tool_broker.credential_automations import supported_automation_dicts, use_credential_for_action
from secure_tool_broker.credential_sources import (
    CredentialRef,
    CredentialSearchRequest,
    get_credential_source,
    normalize_credential_source_name,
)
from secure_tool_broker.redaction import parse_and_redact_stdout, redact_text
from secure_tool_broker.google_workspace import (
    ERROR_SIGN_IN_REQUIRED as GOOGLE_SIGN_IN_REQUIRED,
    GoogleNotReady,
    classify_failure as classify_google_failure,
    google_auth_status,
    not_ready as google_not_ready,
    signed_in_google,
)
from secure_tool_broker.x_sign_in import x_auth_status
from secure_tools.auth_methods import SIGN_IN_TOOLS, X_SIGN_IN_PROVIDER
from secure_tools.providers import ToolContext, ToolResult, ValidatedAction


CommandBuilder = Callable[[Mapping[str, Any]], list[str]]


@dataclass(frozen=True)
class BrokerAction:
    executable: str
    build_command: CommandBuilder
    timeout_seconds: int = 30


AUTH_REQUIRED_PATTERNS: dict[str, tuple[str, ...]] = {
    "github": (
        "gh auth login",
        "not logged in",
        "authentication required",
        "requires authentication",
        "bad credentials",
        "http 401",
        "status code 401",
    ),
    "google": (
        "not authenticated",
        "authentication required",
        "login required",
        "no credentials",
        "unauthorized",
        "status code 401",
    ),
    "bitwarden": (
        "not logged in",
        "unauthenticated",
        "vault is locked",
        "invalid session",
        "authentication required",
        "unauthorized",
    ),
}

# How a person signs in to GitHub (/link cli:github web, /link cli:github).
# There is no environment-variable token: each person uses their own sign-in.
GITHUB_SIGN_IN = SIGN_IN_TOOLS["cli:github"]
GITHUB_AUTH_OPTIONS = ("web", "token")
GOOGLE_SIGN_IN_AGAIN_MESSAGE = f"Google says you need to sign in again. Run {SIGN_IN_TOOLS['cli:google'].command}."

BITWARDEN_PENDING_RESTART_UNLOCK_FILENAME = "pending_api_key_unlock.json"
BITWARDEN_PENDING_RESTART_UNLOCK_TTL_SECONDS = 900


def _string(params: Mapping[str, Any], key: str, *, required: bool = True, default: str = "") -> str:
    value = str(params.get(key, default) or "").strip()
    if required and not value:
        raise ValueError(f"Missing required parameter: {key}")
    return value


def _int(params: Mapping[str, Any], key: str, *, required: bool = True, default: int = 0) -> int:
    if required and key not in params:
        raise ValueError(f"Missing required parameter: {key}")
    raw = params.get(key, default)
    if raw in (None, ""):
        if required:
            raise ValueError(f"Missing required parameter: {key}")
        return default
    return int(raw)


def _number(params: Mapping[str, Any], *aliases: str) -> int:
    for key in ("number", *aliases):
        if key in params and params.get(key) not in (None, ""):
            return int(params[key])
    raise ValueError("Missing required parameter: number")


def _bool(params: Mapping[str, Any], key: str, *, default: bool = False) -> bool:
    raw = params.get(key)
    if raw in (None, ""):
        return default
    if isinstance(raw, bool):
        return raw
    return str(raw).strip().lower() in {"1", "true", "yes", "on"}


def _limit(params: Mapping[str, Any], default: int = 30, maximum: int = 100) -> str:
    value = _int(params, "limit", required=False, default=default)
    return str(max(1, min(value, maximum)))


def _json_fields(fields: str) -> list[str]:
    return ["--json", fields]


def _github_repo(params: Mapping[str, Any]) -> str:
    return _string(params, "repo")


def _optional_csv(params: Mapping[str, Any], key: str) -> str:
    value = params.get(key)
    if isinstance(value, list):
        return ",".join(str(item).strip() for item in value if str(item).strip())
    return str(value or "").strip()


def _gh_repo_view(params: Mapping[str, Any]) -> list[str]:
    return [
        "gh",
        "repo",
        "view",
        _github_repo(params),
        *_json_fields("name,owner,description,url,isPrivate,defaultBranchRef"),
    ]


def _gh_auth_status(params: Mapping[str, Any]) -> list[str]:
    command = ["gh", "auth", "status"]
    hostname = _string(params, "hostname", required=False)
    if hostname:
        command.extend(["--hostname", hostname])
    return command


def _gh_auth_login_with_token(params: Mapping[str, Any]) -> list[str]:
    command = ["gh", "auth", "login", "--with-token"]
    hostname = _string(params, "hostname", required=False)
    if hostname:
        command.extend(["--hostname", hostname])
    return command


def _gh_auth_logout(params: Mapping[str, Any]) -> list[str]:
    command = ["gh", "auth", "logout"]
    hostname = _string(params, "hostname", required=False, default="github.com")
    if hostname:
        command.extend(["--hostname", hostname])
    user = _string(params, "user", required=False)
    if user:
        command.extend(["--user", user])
    return command


def _gh_issue_list(params: Mapping[str, Any]) -> list[str]:
    return [
        "gh",
        "issue",
        "list",
        "--repo",
        _github_repo(params),
        "--state",
        _string(params, "state", required=False, default="open"),
        "--limit",
        _limit(params),
        *_json_fields("number,title,state,url,author,labels,updatedAt"),
    ]


def _gh_issue_view(params: Mapping[str, Any]) -> list[str]:
    return [
        "gh",
        "issue",
        "view",
        str(_number(params, "issue_number")),
        "--repo",
        _github_repo(params),
        *_json_fields("number,title,state,url,author,labels,body,comments"),
    ]


def _gh_issue_create(params: Mapping[str, Any]) -> list[str]:
    command = [
        "gh",
        "issue",
        "create",
        "--repo",
        _github_repo(params),
        "--title",
        _string(params, "title"),
        "--body",
        _string(params, "body", required=False, default=""),
    ]
    labels = _optional_csv(params, "labels")
    if labels:
        command.extend(["--label", labels])
    return command


def _gh_issue_comment(params: Mapping[str, Any]) -> list[str]:
    return [
        "gh",
        "issue",
        "comment",
        str(_number(params, "issue_number")),
        "--repo",
        _github_repo(params),
        "--body",
        _string(params, "body"),
    ]


def _gh_pr_list(params: Mapping[str, Any]) -> list[str]:
    return [
        "gh",
        "pr",
        "list",
        "--repo",
        _github_repo(params),
        "--state",
        _string(params, "state", required=False, default="open"),
        "--limit",
        _limit(params),
        *_json_fields("number,title,state,url,author,headRefName,baseRefName,updatedAt"),
    ]


def _gh_pr_view(params: Mapping[str, Any]) -> list[str]:
    return [
        "gh",
        "pr",
        "view",
        str(_number(params, "pr_number", "pull_request_number")),
        "--repo",
        _github_repo(params),
        *_json_fields("number,title,state,url,author,headRefName,baseRefName,body,comments"),
    ]


def _gh_pr_create(params: Mapping[str, Any]) -> list[str]:
    command = [
        "gh",
        "pr",
        "create",
        "--repo",
        _github_repo(params),
        "--title",
        _string(params, "title"),
        "--body",
        _string(params, "body", required=False, default=""),
    ]
    head = _string(params, "head", required=False)
    base = _string(params, "base", required=False)
    if head:
        command.extend(["--head", head])
    if base:
        command.extend(["--base", base])
    return command


def _gh_pr_comment(params: Mapping[str, Any]) -> list[str]:
    return [
        "gh",
        "pr",
        "comment",
        str(_number(params, "pr_number", "pull_request_number")),
        "--repo",
        _github_repo(params),
        "--body",
        _string(params, "body"),
    ]


def _gh_workflow_run_list(params: Mapping[str, Any]) -> list[str]:
    command = [
        "gh",
        "run",
        "list",
        "--repo",
        _github_repo(params),
        "--limit",
        _limit(params),
        *_json_fields("databaseId,displayTitle,event,status,conclusion,workflowName,url,createdAt,updatedAt"),
    ]
    workflow = _string(params, "workflow", required=False)
    if workflow:
        command.extend(["--workflow", workflow])
    return command


def _project_number(params: Mapping[str, Any]) -> int:
    for key in ("number", "project_number", "project"):
        if key in params and params.get(key) not in (None, ""):
            return int(params[key])
    raise ValueError("Missing required parameter: number")


def _gh_project_item_list(params: Mapping[str, Any]) -> list[str]:
    return [
        "gh",
        "project",
        "item-list",
        str(_project_number(params)),
        "--owner",
        _string(params, "owner"),
        "--format",
        "json",
        "--limit",
        _limit(params, default=100, maximum=500),
    ]


def _gh_api_get(params: Mapping[str, Any]) -> list[str]:
    return ["gh", "api", "--method", "GET", _string(params, "path")]


def _gh_api_post(params: Mapping[str, Any]) -> list[str]:
    command = ["gh", "api", "--method", "POST", _string(params, "path")]
    body = params.get("body")
    if body is not None:
        command.extend(["--input", "-"])
    return command


def _gh_exec(params: Mapping[str, Any]) -> list[str]:
    # The command line is checked by secure_tool_broker.cli_passthrough before it runs.
    return ["gh", *argv_from_params(params, cli="gh")]


def _gh_exec_stdin(params: Mapping[str, Any]) -> str | None:
    if "stdin" not in params:
        return None
    raw = params.get("stdin")
    if raw is None:
        return None
    value = str(raw)
    if "\x00" in value:
        raise ValueError("gh.exec stdin contains a NUL byte")
    if len(value) > 1_000_000:
        raise ValueError("gh.exec stdin is too large")
    return value


def _gog_base(*parts: str) -> list[str]:
    return ["gog", "--json", "--no-input", *parts]


def _gog_limit_flag(params: Mapping[str, Any], *, default: int = 20) -> str:
    return str(max(1, min(_int(params, "limit", required=False, default=default), 100)))


def _gog_has_flag(argv: list[str], flag: str) -> bool:
    return any(value == flag or value.startswith(f"{flag}=") for value in argv)


def _gog_exec(params: Mapping[str, Any]) -> list[str]:
    # Every gog command line, typed or passthrough, is checked by
    # secure_tool_broker.cli_passthrough before it runs.
    argv = argv_from_params(params, cli="gog")
    command = ["gog"]
    if _bool(params, "json", default=True) and not (
        _gog_has_flag(argv, "--json") or _gog_has_flag(argv, "--plain")
    ):
        command.append("--json")
    if not _gog_has_flag(argv, "--no-input"):
        command.append("--no-input")
    command.extend(argv)
    return command


def _gog_gmail_search(params: Mapping[str, Any]) -> list[str]:
    command = _gog_base("gmail", "search", _string(params, "query"))
    command.extend(["--max", _gog_limit_flag(params, default=10)])
    return command


def _gog_gmail_read(params: Mapping[str, Any]) -> list[str]:
    command = _gog_base("gmail", "get", _string(params, "id"))
    message_format = _string(params, "format", required=False)
    if message_format:
        command.extend(["--format", message_format])
    return command


def _gog_gmail_send(params: Mapping[str, Any]) -> list[str]:
    command = _gog_base("gmail", "send")
    for key, flag in (("to", "--to"), ("subject", "--subject"), ("body", "--body")):
        value = _string(params, key)
        command.extend([flag, value])
    return command


def _gog_calendar_list(params: Mapping[str, Any]) -> list[str]:
    command = _gog_base("calendar", "events")
    calendar_id = _string(params, "calendar_id", required=False)
    if calendar_id:
        command.append(calendar_id)
    query = _string(params, "query", required=False)
    if query:
        command.extend(["--query", query])
    command.extend(["--max", _gog_limit_flag(params, default=10)])
    return command


def _gog_calendar_create(params: Mapping[str, Any]) -> list[str]:
    command = _gog_base(
        "calendar",
        "create",
        _string(params, "calendar_id", required=False, default="primary"),
    )
    summary = _string(params, "subject", required=False) or _string(params, "summary")
    command.extend(["--summary", summary])
    for key, flag in (
        ("from", "--from"),
        ("to", "--to"),
        ("location", "--location"),
        ("attendees", "--attendees"),
    ):
        value = _string(params, key, required=False)
        if value:
            command.extend([flag, value])
    description = _string(params, "description", required=False) or _string(
        params,
        "body",
        required=False,
    )
    if description:
        command.extend(["--description", description])
    return command


def _gog_drive_list(params: Mapping[str, Any]) -> list[str]:
    command = _gog_base("drive", "ls")
    parent = _string(params, "parent", required=False) or _string(params, "path", required=False)
    if parent:
        command.extend(["--parent", parent])
    query = _string(params, "query", required=False)
    if query:
        command.extend(["--query", query])
    command.extend(["--max", _gog_limit_flag(params, default=20)])
    return command


def _gog_drive_export(params: Mapping[str, Any]) -> list[str]:
    command = _gog_base("drive", "download", _string(params, "id"))
    output_path = _string(params, "path", required=False)
    if output_path:
        command.extend(["--out", output_path])
    export_format = _string(params, "format", required=False) or _string(params, "mime_type", required=False)
    if export_format:
        command.extend(["--format", _gog_export_format(export_format)])
    return command


def _gog_docs_export(params: Mapping[str, Any]) -> list[str]:
    command = _gog_base("docs", "export", _string(params, "id"))
    output_path = _string(params, "path", required=False)
    if output_path:
        command.extend(["--out", output_path])
    export_format = _string(params, "format", required=False) or _string(params, "mime_type", required=False)
    if export_format:
        command.extend(["--format", _gog_export_format(export_format)])
    return command


def _gog_sheets_read(params: Mapping[str, Any]) -> list[str]:
    range_value = _string(params, "range", required=False) or _string(
        params,
        "path",
        required=False,
        default="Sheet1!A1:Z100",
    )
    return _gog_base(
        "sheets",
        "get",
        _string(params, "id"),
        range_value,
    )


def _gog_export_format(value: str) -> str:
    normalized = value.strip().lower()
    mime_map = {
        "text/plain": "txt",
        "text/markdown": "txt",
        "application/pdf": "pdf",
        "application/vnd.openxmlformats-officedocument.wordprocessingml.document": "docx",
        "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet": "xlsx",
        "text/csv": "csv",
    }
    return mime_map.get(normalized, normalized)


def _bw_list_items(params: Mapping[str, Any]) -> list[str]:
    command = ["bw", "list", "items"]
    query = _string(params, "query", required=False)
    if query:
        command.extend(["--search", query])
    return command


def _bw_get_item(params: Mapping[str, Any]) -> list[str]:
    return ["bw", "get", "item", _string(params, "id")]


def _bw_credential_list(params: Mapping[str, Any]) -> list[str]:
    return _bw_list_items(params)


GITHUB_ACTIONS: dict[str, BrokerAction] = {
    "gh.exec": BrokerAction("gh", _gh_exec, timeout_seconds=120),
    "auth.status": BrokerAction("gh", _gh_auth_status, timeout_seconds=15),
    "auth.logout": BrokerAction("gh", _gh_auth_logout, timeout_seconds=15),
    "auth.login.credential": BrokerAction("gh", _gh_auth_login_with_token, timeout_seconds=30),
    "repo.view": BrokerAction("gh", _gh_repo_view),
    "issue.list": BrokerAction("gh", _gh_issue_list),
    "issue.view": BrokerAction("gh", _gh_issue_view),
    "issue.create": BrokerAction("gh", _gh_issue_create),
    "issue.comment": BrokerAction("gh", _gh_issue_comment),
    "pr.list": BrokerAction("gh", _gh_pr_list),
    "pr.view": BrokerAction("gh", _gh_pr_view),
    "pr.create": BrokerAction("gh", _gh_pr_create),
    "pr.comment": BrokerAction("gh", _gh_pr_comment),
    "workflow.run.list": BrokerAction("gh", _gh_workflow_run_list),
    "project.item.list": BrokerAction("gh", _gh_project_item_list, timeout_seconds=60),
    "api.get": BrokerAction("gh", _gh_api_get),
    "api.post": BrokerAction("gh", _gh_api_post),
}

GITHUB_AUTH_LOGIN_ACTION = BrokerAction("gh", _gh_auth_login_with_token, timeout_seconds=30)

GOOGLE_ACTIONS: dict[str, BrokerAction] = {
    "gog.exec": BrokerAction("gog", _gog_exec, timeout_seconds=60),
    "gmail_search": BrokerAction("gog", _gog_gmail_search),
    "gmail_read": BrokerAction("gog", _gog_gmail_read),
    "gmail_send": BrokerAction("gog", _gog_gmail_send),
    "calendar_list": BrokerAction("gog", _gog_calendar_list),
    "calendar_create": BrokerAction("gog", _gog_calendar_create),
    "drive_list": BrokerAction("gog", _gog_drive_list),
    "drive_export": BrokerAction("gog", _gog_drive_export),
    "docs_export": BrokerAction("gog", _gog_docs_export),
    "sheets_read": BrokerAction("gog", _gog_sheets_read),
}

BITWARDEN_ACTIONS: dict[str, BrokerAction] = {
    "item_search_metadata": BrokerAction("bw", _bw_list_items),
    "item_get_metadata": BrokerAction("bw", _bw_get_item),
    "credential.search": BrokerAction("bw", _bw_list_items),
    "credential.get_metadata": BrokerAction("bw", _bw_get_item),
    "credential.list": BrokerAction("bw", _bw_credential_list),
}

BROKER_ACTIONS: dict[str, dict[str, BrokerAction]] = {
    "github": GITHUB_ACTIONS,
    "google": GOOGLE_ACTIONS,
    "bitwarden": BITWARDEN_ACTIONS,
}

def _run_command(
    command: list[str],
    *,
    executable_name: str,
    input_text: str | None = None,
    env: Mapping[str, str] | None = None,
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
            env=credential_owner_environment(env=env),
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
        return ToolResult(status="error", error=f"{executable_name} command timed out after {timeout_seconds}s")
    except Exception as exc:
        return ToolResult(status="error", error=f"{executable_name} command failed to start: {exc}")


def _is_auth_required(provider: str, *, stdout: str = "", stderr: str = "", error: str = "") -> bool:
    haystack = "\n".join([stdout or "", stderr or "", error or ""]).lower()
    return any(pattern in haystack for pattern in AUTH_REQUIRED_PATTERNS.get(provider, ()))


def _auth_required_result(provider: str, content: Mapping[str, Any] | None = None) -> ToolResult:
    command = f"/auth {provider}"
    if provider == "github":
        command = GITHUB_SIGN_IN.command
        message = (
            "GitHub authentication is required in the secure tool broker. GitHub uses the broker-owned "
            f"`gh` CLI with each person's own sign-in. Ask the person to run {command} web for browser/MFA "
            f"login, {command} to paste a token, or {command} web-all only when high-privilege scope is approved."
        )
        auth_options = [f"{command} web", command, f"{command} web-all"]
    else:
        message = (
            f"{provider} authentication is required in the secure tool broker. "
            f"Ask the user to run {command}."
        )
        auth_options = [command]
    result_content = dict(content or {})
    result_content.update(
        {
            "error_code": "auth_required",
            "provider": provider,
            "auth_required": True,
            "auth_message": message,
            "user_action": command,
            "auth_options": auth_options,
        }
    )
    return ToolResult(status="error", content=result_content, error=message)


def _command_preview(command: list[str]) -> list[str]:
    return [command[0], *command[1:3], "..."] if len(command) > 3 else command


def get_auth_status(provider: str, *, team_id: str | None = None) -> AuthStatus:
    """The active person's status for one provider; ``team_id`` finds the team's Google client."""

    provider_name = str(provider or "").strip().lower()
    if provider_name == "github":
        owner_id = active_credential_owner_id()
        if not owner_id:
            raise ValueError("A registered canonical user is required for credential auth status.")
        result = execute_validated_action(
            ValidatedAction(provider="github", action="auth.status", params={}),
            ToolContext(
                reason="broker auth status check",
                canonical_user_id=owner_id,
                principal_type="user",
            ),
        )
        if result.status == "success":
            return AuthStatus(
                provider="github",
                status="authenticated",
                authenticated=True,
                message="GitHub CLI authentication is available to the secure tool broker.",
                auth_options=GITHUB_AUTH_OPTIONS,
                details=result.content,
            )
        if result.content.get("error_code") == "auth_required":
            return AuthStatus(
                provider="github",
                status="auth_required",
                authenticated=False,
                message=f"GitHub authentication is required. Sign in with {GITHUB_SIGN_IN.command}.",
                auth_options=GITHUB_AUTH_OPTIONS,
                details=result.content,
            )
        return AuthStatus(
            provider="github",
            status="unavailable",
            authenticated=False,
            message=str(result.error or "GitHub auth status could not be checked."),
            auth_options=GITHUB_AUTH_OPTIONS,
            details=result.content,
        )
    if provider_name == "google":
        return google_auth_status(active_credential_owner_id(), team_id)
    if provider_name == X_SIGN_IN_PROVIDER:
        return x_auth_status(active_credential_owner_id(), team_id)
    source_provider = normalize_credential_source_name(provider_name)
    if source_provider in {"bitwarden", "onepassword", "keepass", "vault", "sealbox"}:
        source = get_credential_source(source_provider)
        if source is not None:
            return source.auth_status()
    return AuthStatus(
        provider=provider_name or "unknown",
        status="unknown_provider",
        authenticated=False,
        message=f"Unknown auth provider: {provider}",
    )


def _prepare_github_env_from_state(env: dict[str, str]) -> ToolResult | None:
    """Give gh the person's stored token; ``credential_owner_environment`` removed any other.

    Without a stored token gh uses the sign-in in the person's own
    ``GH_CONFIG_DIR`` (file-backed state), which is equally the person's.
    """

    stored = read_credential_state("github.token", env=env)
    if isinstance(stored, ToolResult):
        return stored
    if not stored:
        return None
    env["GH_TOKEN"] = str(stored).strip()
    env["GH_ENTERPRISE_TOKEN"] = str(stored).strip()
    return None


def _github_auth_status_with_token(executable_path: str, token: str, *, hostname: str | None = None) -> ToolResult:
    command = _gh_auth_status({"hostname": hostname or ""})
    command[0] = executable_path
    env = credential_owner_environment()
    env["GH_TOKEN"] = token
    env["GH_ENTERPRISE_TOKEN"] = token
    try:
        completed = subprocess.run(
            command,
            cwd=os.getcwd(),
            env=env,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=15,
            check=False,
        )
    except subprocess.TimeoutExpired:
        return ToolResult(status="error", error="gh auth status timed out after 15s")
    except Exception as exc:
        return ToolResult(status="error", error=f"gh auth status failed to start: {exc}")
    content = {
        "provider": "github",
        "action": "auth.status",
        "exit_code": completed.returncode,
        "command": _command_preview(command),
        "credential_state_backend": credential_state_backend_mode(),
    }
    stdout = redact_text(completed.stdout or "").strip()
    stderr = redact_text(completed.stderr or "").strip()
    if stdout:
        content["stdout"] = stdout
    if stderr:
        content["stderr"] = stderr
    if completed.returncode != 0:
        return ToolResult(status="error", content=content, error=f"gh auth status exited with code {completed.returncode}")
    return ToolResult(status="success", content=content)


def login_github_with_token(token: str, *, hostname: str | None = None) -> ToolResult:
    token_value = str(token or "").strip()
    if not token_value:
        return ToolResult(status="error", error="GitHub token is required.")
    spec = GITHUB_AUTH_LOGIN_ACTION
    executable_path = shutil.which(spec.executable)
    if not executable_path:
        return ToolResult(status="error", error=f"Required CLI is not installed or not on PATH: {spec.executable}")
    if credential_state_backend_mode() == "credential_source":
        verification = _github_auth_status_with_token(executable_path, token_value, hostname=hostname)
        if verification.status != "success":
            return verification
        store_result = write_credential_state("github.token", token_value)
        if store_result.status != "success":
            return store_result
        verification.content = {
            **dict(verification.content or {}),
            "credential_state_persisted": True,
            "credential_state_backend": "credential_source",
            "credential_state": dict(store_result.content or {}),
        }
        return verification
    return _gh_login_with_token(spec, executable_path, token_value, hostname=hostname)


def _gh_login_with_token(
    spec: BrokerAction, executable_path: str, token_value: str, *, hostname: str | None
) -> ToolResult:
    """``gh auth login --with-token`` in the person's own GH_CONFIG_DIR (file-backed state)."""

    params = {"hostname": hostname or ""}
    try:
        command = spec.build_command(params)
    except Exception as exc:
        return ToolResult(status="error", error=f"Invalid parameters: {exc}")
    command[0] = executable_path
    try:
        completed = subprocess.run(
            command,
            cwd=os.getcwd(),
            env=credential_owner_environment(),
            input=f"{token_value}\n",
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=spec.timeout_seconds,
            check=False,
        )
    except subprocess.TimeoutExpired:
        return ToolResult(status="error", error=f"{spec.executable} command timed out after {spec.timeout_seconds}s")
    except Exception as exc:
        return ToolResult(status="error", error=f"{spec.executable} command failed to start: {exc}")
    content = {
        "provider": "github",
        "action": "auth.login",
        "exit_code": completed.returncode,
        "command": _command_preview(command),
    }
    stdout = redact_text(completed.stdout or "").strip()
    stderr = redact_text(completed.stderr or "").strip()
    if stdout:
        content["stdout"] = stdout
    if stderr:
        content["stderr"] = stderr
    if completed.returncode != 0:
        return ToolResult(status="error", content=content, error=f"{spec.executable} exited with code {completed.returncode}")
    return ToolResult(status="success", content=content)


def _bitwarden_pending_restart_unlock_file() -> Path | None:
    return broker_credential_file("bitwarden", BITWARDEN_PENDING_RESTART_UNLOCK_FILENAME)


def save_bitwarden_restart_unlock_request(
    *,
    master_password: str,
    client_id: str = "",
    client_secret: str = "",
    server_url: str = "",
) -> ToolResult:
    password = str(master_password or "")
    if not password:
        return ToolResult(status="error", error="Bitwarden master password is required.")
    pending_path = _bitwarden_pending_restart_unlock_file()
    if pending_path is None:
        return ToolResult(
            status="error",
            error="SECURE_TOOL_BROKER_CREDENTIALS_DIR is required to save a restart-based Bitwarden unlock request.",
        )

    now = int(time.time())
    client_id_value = str(client_id or "").strip()
    client_secret_value = str(client_secret or "").strip()
    owner_id = active_credential_owner_id()
    owner_env = credential_owner_environment()
    env_client_id_present = bool(owner_env.get("BW_CLIENTID"))
    env_client_secret_present = bool(owner_env.get("BW_CLIENTSECRET"))
    if not (client_id_value or env_client_id_present) or not (client_secret_value or env_client_secret_present):
        return ToolResult(
            status="error",
            content={
                "provider": "bitwarden",
                "action": "pending_restart_unlock.saved",
                "error_code": "bitwarden_pending_restart_unlock_missing_api_key",
                "client_id_present": bool(client_id_value or env_client_id_present),
                "client_secret_present": bool(client_secret_value or env_client_secret_present),
                "client_id_source": "form" if client_id_value else ("env" if env_client_id_present else "missing"),
                "client_secret_source": "form" if client_secret_value else ("env" if env_client_secret_present else "missing"),
            },
            error=(
                "Bitwarden API client ID and client secret are required for restart-based broker-side login. "
                "Paste them into the form or configure BW_CLIENTID and BW_CLIENTSECRET in the broker environment."
            ),
        )
    effective_server_url = str(server_url or os.getenv("BITWARDEN_SERVER_URL") or os.getenv("BW_SERVER_URL") or "").strip()
    payload = {
        "version": 1,
        "created_at_epoch": now,
        "expires_at_epoch": now + BITWARDEN_PENDING_RESTART_UNLOCK_TTL_SECONDS,
        "server_url": effective_server_url,
        "client_id": client_id_value,
        "client_secret": client_secret_value,
        "master_password": password,
        "owner_canonical_user_id": owner_id,
    }
    write_private_broker_file(pending_path, json.dumps(payload, indent=2, sort_keys=True))
    return ToolResult(
        status="success",
        content={
            "provider": "bitwarden",
            "action": "pending_restart_unlock.saved",
            "pending_file": str(pending_path),
            "expires_at_epoch": payload["expires_at_epoch"],
            "ttl_seconds": BITWARDEN_PENDING_RESTART_UNLOCK_TTL_SECONDS,
            "server_url_present": bool(effective_server_url),
            "client_id_present": bool(client_id_value or env_client_id_present),
            "client_secret_present": bool(client_secret_value or env_client_secret_present),
            "client_id_source": "form" if client_id_value else "env",
            "client_secret_source": "form" if client_secret_value else "env",
            "master_password_present": True,
        },
    )


def _bitwarden_status_with_session(session: str) -> ToolResult:
    session_value = str(session or "").strip()
    if not session_value:
        return ToolResult(status="error", error="Bitwarden session is required.")
    executable_path = shutil.which("bw")
    if not executable_path:
        return ToolResult(status="error", error="Required CLI is not installed or not on PATH: bw")
    env = credential_owner_environment()
    env["BW_SESSION"] = session_value
    config_result = ensure_bitwarden_server_configured_from_env(env)
    if config_result is not None:
        return config_result
    completed = _run_command([executable_path, "status"], executable_name="bw", env=env, timeout_seconds=15)
    if isinstance(completed, ToolResult):
        return completed
    parsed_json, redacted_stdout = parse_and_redact_stdout(completed.stdout or "")
    content = {
        "provider": "bitwarden",
        "action": "status",
        "exit_code": completed.returncode,
        "command": [executable_path, "status"],
    }
    if parsed_json is not None:
        content["json"] = parsed_json
    elif redacted_stdout:
        content["stdout"] = redacted_stdout
    stderr = redact_text(completed.stderr or "").strip()
    if stderr:
        content["stderr"] = stderr
    if completed.returncode != 0:
        return ToolResult(status="error", content=content, error=f"bw exited with code {completed.returncode}")
    status = ""
    if isinstance(parsed_json, dict):
        status = str(parsed_json.get("status") or "").strip().lower()
    if status and status != "unlocked":
        if status == "unauthenticated":
            return ToolResult(
                status="error",
                content=content,
                error=(
                    "Bitwarden vault is unauthenticated in the broker. A BW_SESSION value is not portable unless "
                    "the broker-side bw CLI is already logged in to the same server; use the broker-side API-key "
                    "login/unlock form or run bw login inside the broker environment."
                ),
            )
        return ToolResult(status="error", content=content, error=f"Bitwarden vault is {status}, not unlocked.")
    return ToolResult(status="success", content=content)


def login_bitwarden_with_session(session: str) -> ToolResult:
    result = _bitwarden_status_with_session(session)
    if result.status != "success":
        return result
    session_value = str(session or "").strip()
    persisted_path = broker_credential_file("bitwarden", "session")
    if persisted_path is not None:
        write_private_broker_file(persisted_path, session_value)
        result.content = {**dict(result.content or {}), "credential_state_persisted": True}
        if not active_credential_owner_id():
            os.environ["BW_SESSION_FILE"] = str(persisted_path)
    if not active_credential_owner_id():
        os.environ["BW_SESSION"] = session_value
    return result


def login_bitwarden_with_api_key(
    *,
    master_password: str,
    client_id: str = "",
    client_secret: str = "",
    server_url: str = "",
    force_login: bool = False,
) -> ToolResult:
    unlock = login_and_unlock_bitwarden_with_api_key(
        master_password=master_password,
        client_id=client_id,
        client_secret=client_secret,
        server_url=server_url,
        force_login=force_login,
    )
    if unlock.result.status != "success" or not unlock.session:
        return unlock.result
    result = _bitwarden_status_with_session(unlock.session)
    if result.status != "success":
        return result
    persisted_path = broker_credential_file("bitwarden", "session")
    if persisted_path is not None:
        write_private_broker_file(persisted_path, unlock.session)
        result.content = {**dict(result.content or {}), "credential_state_persisted": True}
        if not active_credential_owner_id():
            os.environ["BW_SESSION_FILE"] = str(persisted_path)
    if not active_credential_owner_id():
        os.environ["BW_SESSION"] = unlock.session
    result.content = {**dict(result.content or {}), "broker_side_login": True}
    return result


def process_pending_bitwarden_restart_unlock() -> ToolResult | None:
    owner_id = active_credential_owner_id()
    if not owner_id:
        raw_root = str(os.getenv("SECURE_TOOL_BROKER_CREDENTIALS_DIR") or "").strip()
        if raw_root:
            pending_files = sorted(
                (Path(raw_root).expanduser().resolve() / USERS_DIRNAME).glob(
                    f"owner-*/bitwarden/{BITWARDEN_PENDING_RESTART_UNLOCK_FILENAME}"
                )
            )
            if pending_files:
                results: list[ToolResult] = []
                for candidate in pending_files:
                    try:
                        payload = json.loads(candidate.read_text(encoding="utf-8"))
                        candidate_owner = str(payload.get("owner_canonical_user_id") or "").strip()
                    except Exception as exc:
                        candidate.unlink(missing_ok=True)
                        results.append(
                            ToolResult(
                                status="error",
                                error=f"Pending Bitwarden restart unlock file could not be read: {exc}",
                            )
                        )
                        continue
                    if not candidate_owner:
                        candidate.unlink(missing_ok=True)
                        results.append(
                            ToolResult(
                                status="error",
                                error="Owner-scoped pending Bitwarden unlock has no canonical user owner.",
                            )
                        )
                        continue
                    with credential_owner_context(candidate_owner):
                        result = process_pending_bitwarden_restart_unlock()
                    if result is not None:
                        results.append(result)
                if not results:
                    return None
                failures = [result for result in results if result.status != "success"]
                return ToolResult(
                    status="error" if failures else "success",
                    content={
                        "provider": "bitwarden",
                        "action": "pending_restart_unlock.process_all",
                        "processed_count": len(results),
                        "failed_count": len(failures),
                    },
                    error=f"{len(failures)} pending Bitwarden unlock request(s) failed." if failures else None,
                )
    pending_path = _bitwarden_pending_restart_unlock_file()
    if pending_path is None or not pending_path.is_file():
        return None

    base_content: dict[str, Any] = {
        "provider": "bitwarden",
        "action": "pending_restart_unlock.process",
        "pending_file": str(pending_path),
        "ttl_seconds": BITWARDEN_PENDING_RESTART_UNLOCK_TTL_SECONDS,
    }
    try:
        raw_payload = pending_path.read_text(encoding="utf-8")
        payload = json.loads(raw_payload)
    except Exception as exc:
        try:
            pending_path.unlink()
            base_content["pending_file_deleted"] = True
        except OSError:
            base_content["pending_file_deleted"] = False
        base_content["error_code"] = "bitwarden_pending_restart_unlock_invalid"
        return ToolResult(
            status="error",
            content=base_content,
            error=f"Pending Bitwarden restart unlock file could not be read: {exc}",
        )

    if not isinstance(payload, dict):
        payload = {}
    now = int(time.time())
    created_at = int(payload.get("created_at_epoch") or 0)
    expires_at = int(payload.get("expires_at_epoch") or 0)
    base_content["pending_age_seconds"] = max(0, now - created_at) if created_at else None
    base_content["expires_at_epoch"] = expires_at or None
    if expires_at and now > expires_at:
        try:
            pending_path.unlink()
            base_content["pending_file_deleted"] = True
        except OSError:
            base_content["pending_file_deleted"] = False
        base_content["error_code"] = "bitwarden_pending_restart_unlock_expired"
        return ToolResult(status="error", content=base_content, error="Pending Bitwarden restart unlock request expired.")

    master_password = str(payload.get("master_password") or "")
    client_id = str(payload.get("client_id") or "").strip()
    client_secret = str(payload.get("client_secret") or "").strip()
    server_url = str(payload.get("server_url") or "").strip()
    owner_env = credential_owner_environment()
    env_client_id_present = bool(owner_env.get("BW_CLIENTID"))
    env_client_secret_present = bool(owner_env.get("BW_CLIENTSECRET"))
    base_content.update(
        {
            "server_url_present": bool(server_url),
            "client_id_present": bool(client_id or env_client_id_present),
            "client_secret_present": bool(client_secret or env_client_secret_present),
            "client_id_source": "pending_file" if client_id else ("env" if env_client_id_present else "missing"),
            "client_secret_source": "pending_file" if client_secret else ("env" if env_client_secret_present else "missing"),
            "master_password_present": bool(master_password),
            "master_password_chars": len(master_password),
        }
    )
    unlock = login_and_unlock_bitwarden_with_api_key(
        master_password=master_password,
        client_id=client_id,
        client_secret=client_secret,
        server_url=server_url,
        force_login=True,
    )
    if unlock.result.status != "success" or not unlock.session:
        base_content["pending_file_retained"] = True
        base_content["unlock_result"] = dict(unlock.result.content or {})
        return ToolResult(
            status="error",
            content=base_content,
            error=unlock.result.error or "Pending Bitwarden restart unlock did not produce a session.",
        )

    result = _bitwarden_status_with_session(unlock.session)
    if result.status != "success":
        base_content["pending_file_retained"] = True
        base_content["verification_result"] = dict(result.content or {})
        return ToolResult(status="error", content=base_content, error=result.error or "Pending Bitwarden session verification failed.")

    session_path = broker_credential_file("bitwarden", "session")
    if session_path is not None:
        write_private_broker_file(session_path, unlock.session)
        base_content["session_file"] = str(session_path)
        base_content["credential_state_persisted"] = True
        if not active_credential_owner_id():
            os.environ["BW_SESSION_FILE"] = str(session_path)
    if not active_credential_owner_id():
        os.environ["BW_SESSION"] = unlock.session

    try:
        pending_path.unlink()
        base_content["pending_file_deleted"] = True
    except OSError as exc:
        base_content["pending_file_deleted"] = False
        base_content["pending_file_delete_error"] = redact_text(str(exc))

    return ToolResult(
        status="success",
        content={
            **base_content,
            "broker_side_login": True,
            "restart_pending_unlock": True,
            "verification": dict(result.content or {}),
        },
    )


def _credential_search_request(params: Mapping[str, Any], context: ToolContext) -> CredentialSearchRequest:
    owner_id = str(context.canonical_user_id or "").strip() or None
    return CredentialSearchRequest(
        query=_string(params, "query", required=False),
        limit=max(1, min(_int(params, "limit", required=False, default=50), 100)),
        vault=_string(params, "vault", required=False) or None,
        folder=_string(params, "folder", required=False) or None,
        key=_string(params, "key", required=False) or None,
        name=_string(params, "name", required=False) or None,
        username=_string(params, "username", required=False) or None,
        include_archived=_bool(params, "include_archived", default=False),
        owner_scope_kind="user" if owner_id else None,
        owner_id=owner_id,
    )


def _credential_ref(provider: str, params: Mapping[str, Any], context: ToolContext) -> CredentialRef:
    owner_id = str(context.canonical_user_id or "").strip() or None
    return CredentialRef(
        source_provider=normalize_credential_source_name(provider),
        item_id=_string(params, "item_id", required=False)
        or _string(params, "id", required=False)
        or _string(params, "path", required=False)
        or None,
        item_name=_string(params, "item_name", required=False)
        or _string(params, "credential_name", required=False)
        or None,
        vault=_string(params, "vault", required=False) or None,
        folder=_string(params, "folder", required=False) or None,
        owner_scope_kind="user" if owner_id else None,
        owner_id=owner_id,
    )


def _field_ref(params: Mapping[str, Any]) -> str:
    return _string(params, "field_ref", required=False) or _string(params, "field", required=False) or "password"


def _bitwarden_source() -> Any | ToolResult:
    source = get_credential_source("bitwarden")
    if source is None:
        return ToolResult(status="error", error="Bitwarden credential source is not registered.")
    return source


def _credential_source(provider: str) -> Any | ToolResult:
    source_provider = normalize_credential_source_name(provider)
    source = get_credential_source(source_provider)
    if source is None:
        return ToolResult(status="error", error=f"{source_provider} credential source is not registered.")
    return source


def _checked_cli_name(action: ValidatedAction) -> str | None:
    """The CLI whose command lines must pass cli_passthrough before running."""

    if action.provider == "google":
        return "gog"  # typed actions and gog.exec alike
    if action.provider == "github" and action.action == "gh.exec":
        return "gh"
    return None


def _run_broker_process(
    command: list[str],
    *,
    spec: BrokerAction,
    env: Mapping[str, str],
    stdin: str | None,
    private_cwd: bool,
) -> subprocess.CompletedProcess[str] | ToolResult:
    """Run one CLI; checked CLIs run in an empty private folder, so any file they
    write by default (for example attachments without --out-dir) is discarded."""

    subprocess_kwargs: dict[str, Any] = {"input": stdin} if stdin is not None else {"stdin": subprocess.DEVNULL}
    with tempfile.TemporaryDirectory(prefix="ua-broker-run-") as run_dir:
        try:
            return subprocess.run(
                command,
                cwd=run_dir if private_cwd else os.getcwd(),
                env=env,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=spec.timeout_seconds,
                check=False,
                **subprocess_kwargs,
            )
        except subprocess.TimeoutExpired:
            return ToolResult(status="error", error=f"{spec.executable} command timed out after {spec.timeout_seconds}s")
        except Exception as exc:
            return ToolResult(status="error", error=f"{spec.executable} command failed to start: {exc}")


def _publish_staged_files(checked: CheckedCommand | None, *, succeeded: bool) -> list[str]:
    if checked is None or checked.stage is None or not succeeded:
        return []
    return checked.stage.publish()


# Tools that run under the task person's own sign-in (catalog account mode
# personal_login): never without a person, and never from ambient tokens.
PERSONAL_LOGIN_PROVIDERS = frozenset({"github", "google"})


def execute_validated_action(action: ValidatedAction, context: ToolContext) -> ToolResult:
    if context.canonical_user_id:
        with credential_owner_context(context.require_user()):
            return _execute_validated_action_for_owner(action, context)
    if action.provider in PERSONAL_LOGIN_PROVIDERS:
        return ToolResult(
            status="error",
            error=f"{action.provider} runs only with a person's own sign-in, and this request names no person.",
            content={"provider": action.provider, "action": action.action, "error_code": "person_required"},
        )
    # The authenticated HTTP service rejects missing owners; direct helpers for
    # the credential sources (Bitwarden and the like) still run here.
    return _execute_validated_action_for_owner(action, context)


def _execute_validated_action_for_owner(action: ValidatedAction, context: ToolContext) -> ToolResult:
    from secure_tools.hub_providers import HF_PROVIDER, X_PROVIDER
    from secure_tools.installed_clis import InstalledCliProvider
    from secure_tools.registry import get_provider

    installed = get_provider(action.provider)
    if isinstance(installed, InstalledCliProvider):
        # A CLI the team installed: its checked copy runs in the sandbox (secure_tool_broker.installed_clis).
        return installed.execute(action, context)

    if action.provider in {X_PROVIDER, HF_PROVIDER}:
        from secure_tool_broker.hub_cli_executor import execute_hub_cli

        return execute_hub_cli(action, context)
    if action.provider == "credential_catalog":
        return execute_credential_catalog_action(action, context)
    if action.provider == "secure_requests":
        return execute_secure_requests_action(action, context)
    if action.provider == "github" and action.action in {"auth.login.credential", "auth.login.bitwarden"}:
        return use_credential_for_action(
            credential_provider=_string(action.params, "credential_provider", required=False) or "bitwarden",
            target_provider="github",
            target_action="auth.login",
            item_id=_string(action.params, "item_id", required=False)
            or _string(action.params, "id", required=False)
            or _string(action.params, "path", required=False)
            or None,
            item_name=_string(action.params, "item_name", required=False)
            or _string(action.params, "credential_name", required=False)
            or None,
            field_ref=_field_ref(action.params),
            hostname=_string(action.params, "hostname", required=False) or "github.com",
            vault=_string(action.params, "vault", required=False) or None,
            folder=_string(action.params, "folder", required=False) or None,
            context=context,
            github_token_login=login_github_with_token,
        )
    if get_credential_source(action.provider) is not None:
        return _execute_credential_source_action(action, context)
    return _execute_cli_action(action, context)


def _execute_credential_source_action(action: ValidatedAction, context: ToolContext) -> ToolResult:
    """Search, describe or use an item of a password manager (Bitwarden, Vault, ...)."""

    credential_source = get_credential_source(action.provider)
    if credential_source is not None and action.action in {"credential.search", "credential.list", "item_search_metadata"}:
        source = _credential_source(action.provider)
        if isinstance(source, ToolResult):
            return source
        return source.search(_credential_search_request(action.params, context))
    if credential_source is not None and action.action in {"credential.get_metadata", "item_get_metadata"}:
        source = _credential_source(action.provider)
        if isinstance(source, ToolResult):
            return source
        return source.get_metadata(_credential_ref(action.provider, action.params, context))
    if credential_source is not None and action.action in {"credential.use_for_action", "use_secret_for_action"}:
        source_provider = normalize_credential_source_name(action.provider)
        result = use_credential_for_action(
            credential_provider=source_provider,
            target_provider=_string(action.params, "target_provider", required=False)
            or _string(action.params, "target_service", required=False)
            or _string(action.params, "service", required=False)
            or _string(action.params, "provider", required=False),
            target_action=_string(action.params, "target_action", required=False)
            or _string(action.params, "target", required=False)
            or _string(action.params, "action", required=False),
            item_id=_string(action.params, "item_id", required=False)
            or _string(action.params, "id", required=False)
            or _string(action.params, "path", required=False)
            or None,
            item_name=_string(action.params, "item_name", required=False)
            or _string(action.params, "credential_name", required=False)
            or None,
            field_ref=_field_ref(action.params),
            hostname=_string(action.params, "hostname", required=False) or "github.com",
            vault=_string(action.params, "vault", required=False) or None,
            folder=_string(action.params, "folder", required=False) or None,
            context=context,
            github_token_login=login_github_with_token,
        )
        content = dict(result.content)
        content["provider"] = source_provider
        content["action"] = "credential.use_for_action"
        if result.error == "Unsupported credential automation target.":
            content.setdefault("available_secret_automations", supported_automation_dicts())
        result.content = content
        return result
    # bw's own typed actions (item_search_metadata and the like) run as a CLI.
    return _execute_cli_action(action, context)


def _cli_command(action: ValidatedAction) -> tuple[BrokerAction, list[str]] | ToolResult:
    """The action's CLI and command line, with the executable resolved on PATH."""

    spec = BROKER_ACTIONS.get(action.provider, {}).get(action.action)
    if spec is None:
        return ToolResult(status="error", error=f"Broker execution is not implemented for {action.provider}.{action.action}")
    executable_path = shutil.which(spec.executable)
    if not executable_path:
        return ToolResult(status="error", error=f"Required CLI is not installed or not on PATH: {spec.executable}")
    try:
        command = spec.build_command(action.params)
    except Exception as exc:
        return ToolResult(status="error", error=f"Invalid parameters: {exc}")
    return spec, [executable_path, *command[1:]]


def _checked_command(
    action: ValidatedAction, command: list[str], context: ToolContext
) -> tuple[list[str], CheckedCommand | None] | ToolResult:
    """Pass gh/gog command lines through cli_passthrough (other actions are typed)."""

    checked_cli = _checked_cli_name(action)
    if checked_cli is None:
        return command, None
    try:
        checked = check_command_line(
            checked_cli,
            command[1:],
            instance_root=os.getenv("TEAM_INSTANCE_ROOT"),
            person_id=context.canonical_user_id,
        )
    except CommandRefused as exc:
        return ToolResult(
            status="error",
            error=str(exc),
            content={"provider": action.provider, "action": action.action, "error_code": exc.reason_code},
        )
    return [command[0], *checked.argv], checked


def _cli_stdin(action: ValidatedAction) -> str | None:
    if action.provider == "github" and action.action == "api.post" and action.params.get("body") is not None:
        return json.dumps(action.params["body"])
    if action.provider == "github" and action.action == "gh.exec":
        return _gh_exec_stdin(action.params)
    return None


def _execute_cli_action(action: ValidatedAction, context: ToolContext) -> ToolResult:
    """Run one gh, gog or bw action for the active person."""

    prepared = _cli_command(action)
    if isinstance(prepared, ToolResult):
        return prepared
    spec, command = prepared
    try:
        stdin = _cli_stdin(action)
    except Exception as exc:
        return ToolResult(status="error", error=f"Invalid parameters: {exc}")
    checked_result = _checked_command(action, command, context)
    if isinstance(checked_result, ToolResult):
        return checked_result
    command, checked = checked_result
    run = _CliRun(action=action, context=context, spec=spec, command=command, checked=checked, stdin=stdin)
    try:
        if action.provider == "google":
            # Only the person's own Google sign-in, in a fresh private gog home.
            with signed_in_google(command[0], person_id=context.canonical_user_id, team_id=context.team_id) as env:
                return run.execute(env)
        env = credential_owner_environment()
        if action.provider == "github":
            env["GH_PROMPT_DISABLED"] = "1"
            github_state_result = _prepare_github_env_from_state(env)
            if github_state_result is not None:
                return github_state_result
        return run.execute(env)
    except GoogleNotReady as exc:
        return exc.result
    finally:
        if checked is not None and checked.stage is not None:
            checked.stage.cleanup()


@dataclass(frozen=True)
class _CliRun:
    """One prepared CLI invocation and how to report its outcome."""

    action: ValidatedAction
    context: ToolContext
    spec: BrokerAction
    command: list[str]
    checked: CheckedCommand | None
    stdin: str | None

    def execute(self, env: Mapping[str, str]) -> ToolResult:
        try:
            completed = _run_broker_process(
                self.command, spec=self.spec, env=env, stdin=self.stdin, private_cwd=self.checked is not None
            )
            if isinstance(completed, ToolResult):
                return completed
            local_files_written = _publish_staged_files(self.checked, succeeded=completed.returncode == 0)
        except TaskFileRejected as exc:
            return ToolResult(
                status="error",
                error=f"The command ran, but its output files could not be saved: {exc}",
                content={"provider": self.action.provider, "action": self.action.action, "error_code": "cli_local_file_rejected"},
            )
        content = self._content(completed, local_files_written)
        if completed.returncode != 0:
            return self._failure(completed, content)
        return ToolResult(status="success", content=content)

    def _content(self, completed: subprocess.CompletedProcess[str], local_files_written: list[str]) -> dict[str, Any]:
        parsed_json, redacted_stdout = parse_and_redact_stdout(completed.stdout or "")
        content: dict[str, Any] = {
            "provider": self.action.provider,
            "action": self.action.action,
            "exit_code": completed.returncode,
            "command": _command_preview(self.command),
        }
        if self.checked is not None:
            content["command_path"] = self.checked.command_path
            content["access_level"] = self.checked.level.value
            if local_files_written:
                content["local_files_written"] = local_files_written
        if parsed_json is not None:
            content["json"] = parsed_json
        elif redacted_stdout:
            content["stdout"] = redacted_stdout
        stderr = redact_text(completed.stderr or "").strip()
        if stderr:
            content["stderr"] = stderr
        if self.context.reason:
            content["reason"] = self.context.reason
        return content

    def _failure(self, completed: subprocess.CompletedProcess[str], content: dict[str, Any]) -> ToolResult:
        stdout, stderr = completed.stdout or "", completed.stderr or ""
        provider = self.action.provider
        if provider == "google":
            known = classify_google_failure(content, output=f"{stdout}\n{stderr}")
            if known is not None:
                return known
            if _is_auth_required(provider, stdout=stdout, stderr=stderr):
                return google_not_ready(GOOGLE_SIGN_IN_REQUIRED, GOOGLE_SIGN_IN_AGAIN_MESSAGE, content=content)
        elif _is_auth_required(provider, stdout=stdout, stderr=stderr):
            return _auth_required_result(provider, content)
        return ToolResult(status="error", content=content, error=f"{self.spec.executable} exited with code {completed.returncode}")
