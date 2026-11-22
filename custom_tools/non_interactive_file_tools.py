"""
Project-local file tools with direct, non-interactive behavior.

The stock Strands file helpers are exposed through module-style tool specs and
some include approval-oriented wording that makes agent behavior less
deterministic in live automation. These wrappers read and write UTF-8 text
files through the agent file library (``custom_tools.file_access``), which
checks the path for the task and never follows a link.
"""

import json
import logging
import os
from pathlib import Path, PurePosixPath
from typing import Any, Dict
from urllib.parse import quote

from strands import tool

from access_control import safe_open
from access_control.entities import AGENT_ROLE_SYSTEM
from access_control.ownership import RESOURCE_FILE_PATH, WORKSPACE_FILE_READ, private_access
from access_control.safe_open import UnsafePath
from access_control.sensitive_paths import is_sensitive_path
from custom_tools import file_access
from custom_tools.path_policy import PathPolicyError, SENSITIVE_PATH_DENIED
from custom_tools.runtime_context import get_agent_ref
from custom_tools.single_page_site_tools import website_base_url
from custom_tools.workspace_scope import current_workspace_scope
from runtime_settings.instance_layout import (
    AGENTS_DIRNAME,
    TEAM_DIRNAME,
    USERS_DIRNAME,
    WorkspaceScope,
    is_hidden_workspace_tooling_path,
    workspace_download_content_type,
)

logger = logging.getLogger(__name__)

_SAFE_OPEN_REASON_CODES = {
    safe_open.REASON_LINK: file_access.LINK_NOT_FOLLOWED,
    safe_open.REASON_SEVERAL_LINKS: file_access.SEVERAL_HARD_LINKS,
    safe_open.REASON_NOT_REGULAR_FILE: file_access.NOT_A_REGULAR_FILE,
    safe_open.REASON_NOT_DIRECTORY: file_access.NOT_A_FOLDER,
}


class _WorkspaceLinkRefused(RuntimeError):
    """Refusal specific to workspace download-link eligibility."""

    def __init__(self, reason_code: str, message: str) -> None:
        super().__init__(message)
        self.reason_code = reason_code


def _tool_result(status: str, text: str) -> Dict[str, Any]:
    return {
        "status": status,
        "content": [{"text": text}],
    }


def _path_policy_result(requested_path: str, exc: PathPolicyError) -> Dict[str, Any]:
    """Report a path-policy denial with its reason code."""

    logger.warning(
        "File tool path denied. event=file_tool_path_denied reason_code=%s path=%s",
        exc.reason_code,
        requested_path,
    )
    result = _tool_result("error", f"Access to {requested_path} is not allowed: {exc}")
    result["reason_code"] = exc.reason_code
    return result


@tool
def file_read(path: str) -> Dict[str, Any]:
    """
    Read a UTF-8 text file and return its contents.

    Use this to inspect source files, templates, configs, or generated output.
    """
    try:
        location, content = file_access.read_text(path)
    except PathPolicyError as exc:
        return _path_policy_result(path, exc)
    except FileNotFoundError:
        return _tool_result("error", f"File not found: {path}")
    except Exception as exc:
        return _tool_result("error", f"Failed to read {path}: {exc}")

    return _tool_result("success", f"Path: {location.path}\n\n{content}")


@tool
def file_write(path: str, content: str, create_directories: bool = True) -> Dict[str, Any]:
    """
    Create or overwrite a UTF-8 text file without interactive confirmation.

    Use this for source code, templates, configs, and README files. Prefer this
    over shell redirection when you need to create or update file contents.
    """
    try:
        location = file_access.write_text(path, content, create_directories=create_directories)
    except PathPolicyError as exc:
        return _path_policy_result(path, exc)
    except Exception as exc:
        return _tool_result("error", f"Failed to write {path}: {exc}")

    bytes_written = len(content.encode("utf-8"))
    return _tool_result("success", f"Wrote {bytes_written} bytes to {location.path}")


@tool
def file_move(source: str, destination: str, keep_original: bool = False) -> Dict[str, Any]:
    """
    Move (or with keep_original=True, copy) one file to another folder you may write.

    Use it to hand a finished file to the shared team/ folder, or to put a file
    into a folder someone shared with the person you work for. destination is
    the new file's full path; an existing file there is never overwritten.
    Moving to another person's own folder needs them to share a folder with
    the person you work for (or the person sends it with /send in chat).
    """
    try:
        origin, target = file_access.move_file(source, destination, keep_original=keep_original)
    except PathPolicyError as exc:
        return _path_policy_result(f"{source} -> {destination}", exc)
    except FileNotFoundError:
        return _tool_result("error", f"File not found: {source}")
    except Exception as exc:
        return _tool_result("error", f"Failed to move {source} to {destination}: {exc}")
    action = "Copied" if keep_original else "Moved"
    return _tool_result("success", f"{action} {origin.path} to {target.path}")


def _verified_task_identity(agent: Any) -> tuple[bool, Any, Any]:
    """Return ``(verified, agent_principal, person_principal)`` for the active task."""

    if agent is None:
        return False, None, None
    from access_control.identity import IdentityRejected
    from custom_agents.access_runtime import runtime_access, task_access

    try:
        principal, person, _task_id = task_access(agent, runtime_access(agent))
    except (IdentityRejected, RuntimeError) as exc:
        logger.warning(
            "Workspace file link task identity could not be verified. "
            "event=workspace_file_link_identity_unverified agent_id=%s error=%s",
            getattr(agent, "id", None),
            exc,
        )
        return False, None, None
    return True, principal, person


def _enter_workspaces(
    candidate: Path, real_workspaces: Path, raw: str
) -> tuple[Path, tuple[str, ...]]:
    """Find the first prefix of an absolute path that lands on ``real_workspaces``."""

    acc = Path(candidate.anchor)
    parts = candidate.parts[1:]
    if Path(os.path.realpath(acc)) == real_workspaces and not acc.is_symlink():
        return real_workspaces, parts
    for index, part in enumerate(parts):
        acc = acc / part
        if Path(os.path.realpath(acc)) != real_workspaces:
            continue
        if acc.is_symlink():
            break
        return real_workspaces, parts[index + 1 :]
    raise PathPolicyError(
        f"Symbolic links cannot be shared as workspace download links: {raw}",
        reason_code=file_access.LINK_NOT_FOLLOWED,
    )


def _check_no_workspace_symlink(
    raw: str, scope: WorkspaceScope, real_workspaces: Path
) -> None:
    """Refuse any symbolic link traversed inside (or entering) ``workspaces/``."""

    candidate = Path(raw).expanduser()
    if not candidate.is_absolute():
        current = scope.home
        rel_parts = candidate.parts
    else:
        current, rel_parts = _enter_workspaces(candidate, real_workspaces, raw)
    for part in rel_parts:
        if part == ".":
            continue
        if part == "..":
            if current.is_symlink():
                raise PathPolicyError(
                    f"Symbolic links cannot be shared as workspace download links: {raw}",
                    reason_code=file_access.LINK_NOT_FOLLOWED,
                )
            current = current.parent
            continue
        current = current / part
        if current.is_symlink():
            raise PathPolicyError(
                f"Symbolic links cannot be shared as workspace download links: {raw}",
                reason_code=file_access.LINK_NOT_FOLLOWED,
            )


def _check_no_person_area(top: str, *, verified: bool, principal: Any) -> None:
    """Enforce area eligibility when the task has no verified person."""

    if top == TEAM_DIRNAME:
        return
    if not verified:
        raise _WorkspaceLinkRefused(
            "not_downloadable_location",
            "Download links for personal folders or agents/ require a verified task identity.",
        )
    if top == AGENTS_DIRNAME and getattr(principal, "role", None) == AGENT_ROLE_SYSTEM:
        return
    logger.warning(
        "Workspace file link refused for task without a verified person. "
        "event=workspace_file_link_unverified_person agent_role=%s area=%s",
        getattr(principal, "role", None),
        top,
    )
    raise _WorkspaceLinkRefused(
        "not_downloadable_location",
        "Only verified system tasks can create agents/ download links without a person; "
        "personal folder links require a task running for that person.",
    )


def _check_downloadable_area(
    workspace_parts: tuple[str, ...],
    workspace_path: str,
    agent: Any,
) -> None:
    """Confirm the gateway download endpoint will serve ``workspace_path`` to the task's person."""

    top = workspace_parts[0]
    min_depth = 3 if top == USERS_DIRNAME else 2
    if top not in {USERS_DIRNAME, TEAM_DIRNAME, AGENTS_DIRNAME} or len(workspace_parts) < min_depth:
        raise _WorkspaceLinkRefused(
            "not_downloadable_location",
            "Only files inside your own folder, team/, or agents/ can have workspace download links.",
        )
    verified, principal, person = _verified_task_identity(agent)
    if person is None:
        _check_no_person_area(top, verified=verified, principal=principal)
        return
    decision = private_access(
        person_id=person.person_id,
        owner_id=None,
        resource_type=RESOURCE_FILE_PATH,
        resource_id=f"workspace/{workspace_path}",
        action=WORKSPACE_FILE_READ,
        team_id=person.team_id,
        roles=person.roles,
    )
    if not decision.allowed:
        if top == AGENTS_DIRNAME:
            raise _WorkspaceLinkRefused(
                "not_downloadable_location",
                "Files in agents/ can only be downloaded by administrators. "
                "Copy the file to the person's folder or team/ to share a download link.",
            )
        raise _WorkspaceLinkRefused(
            "not_downloadable_location",
            "Download links are only available for files in the person's own folder, team/, "
            "or (for administrators) agents/. Files in another person's folder cannot be "
            "downloaded through workspace links yet.",
        )


def _reopen_in_workspace(
    real_workspaces: Path,
    workspace_parts: tuple[str, ...],
    expected_stat: os.stat_result,
) -> os.stat_result:
    """Re-verify link-safety from ``workspaces/`` down through ``workspace_parts``."""

    try:
        with safe_open.opened_file(real_workspaces, workspace_parts) as (_fd, opened_stat):
            if (
                opened_stat.st_dev != expected_stat.st_dev
                or opened_stat.st_ino != expected_stat.st_ino
            ):
                raise PathPolicyError(
                    "The target file changed while verifying its download link.",
                    reason_code=file_access.UNSAFE_PATH,
                )
            return opened_stat
    except UnsafePath as exc:
        if exc.reason == safe_open.REASON_NOT_FOUND:
            raise FileNotFoundError(f"Not found: {'/'.join(workspace_parts)}") from exc
        raise PathPolicyError(
            str(exc),
            reason_code=_SAFE_OPEN_REASON_CODES.get(exc.reason, file_access.UNSAFE_PATH),
        ) from exc


def _resolve_download_target(
    raw_path: str,
    *,
    agent: Any,
) -> tuple[tuple[str, ...], os.stat_result]:
    """Validate ``raw_path`` through ``file_access`` and gateway download rules."""

    location, file_stat = file_access.inspect_regular_file(raw_path, agent=agent)
    scope = current_workspace_scope(agent)
    real_workspaces = Path(os.path.realpath(scope.workspaces_dir))
    try:
        workspace_parts = location.path.relative_to(real_workspaces).parts
    except ValueError as exc:
        raise _WorkspaceLinkRefused(
            "not_downloadable_location",
            "Only files inside the workspace (the person's folder, team/, or agents/) can "
            "have download links. Host data folders and skill folders are not served by the "
            "workspace download endpoint.",
        ) from exc
    if not workspace_parts:
        raise _WorkspaceLinkRefused(
            "not_downloadable_location",
            "Name a file inside the workspace, not the workspace root folder.",
        )
    if any("\\" in part for part in workspace_parts):
        raise _WorkspaceLinkRefused(
            "unsafe_workspace_path",
            "Workspace file names containing backslashes cannot be downloaded through workspace links.",
        )
    _check_no_workspace_symlink(raw_path, scope, real_workspaces)
    if is_hidden_workspace_tooling_path(workspace_parts):
        raise _WorkspaceLinkRefused(
            "hidden_workspace_path",
            "Files inside hidden tooling folders (.git, .hg, .svn, .venv, __pycache__, "
            "node_modules) cannot be shared as workspace download links.",
        )
    if is_sensitive_path(PurePosixPath(*workspace_parts)):
        raise PathPolicyError(
            f"Sensitive path is blocked: {'/'.join(workspace_parts)}",
            reason_code=SENSITIVE_PATH_DENIED,
        )
    workspace_path = "/".join(workspace_parts)
    _check_downloadable_area(workspace_parts, workspace_path, agent)
    verified_stat = _reopen_in_workspace(real_workspaces, workspace_parts, file_stat)
    return workspace_parts, verified_stat


@tool
def workspace_file_link(path: str) -> Dict[str, Any]:
    """
    Build authenticated download and Workspace Files view links for a workspace file.

    Use this whenever the person asks for a link to a file you saved or downloaded
    in their workspace, or when a file is large (for example audio, video, archives,
    or multi-megabyte documents) instead of attaching the whole file in chat.

    Relative paths resolve against the current task's working folder (the person's
    private folder); pass the full path for files in team/ or (for administrators)
    agents/. Returns JSON with download_url, view_url, workspace_path, filename,
    size_bytes, and content_type.
    """
    cleaned_path = str(path or "").strip()
    if not cleaned_path or "\x00" in cleaned_path:
        logger.warning(
            "Workspace file link refused. event=workspace_file_link_refused "
            "reason_code=invalid_path path=%r",
            path,
        )
        result = _tool_result("error", "Provide a valid path to a file inside the workspace.")
        result["reason_code"] = "invalid_path"
        return result

    agent = get_agent_ref()
    try:
        workspace_parts, verified_stat = _resolve_download_target(cleaned_path, agent=agent)
    except PathPolicyError as exc:
        logger.warning(
            "Workspace file link refused. event=workspace_file_link_refused "
            "reason_code=%s path=%s",
            exc.reason_code,
            cleaned_path,
        )
        return _path_policy_result(cleaned_path, exc)
    except _WorkspaceLinkRefused as exc:
        logger.warning(
            "Workspace file link refused. event=workspace_file_link_refused "
            "reason_code=%s path=%s",
            exc.reason_code,
            cleaned_path,
        )
        result = _tool_result("error", str(exc))
        result["reason_code"] = exc.reason_code
        return result
    except FileNotFoundError:
        logger.warning(
            "Workspace file link refused. event=workspace_file_link_refused "
            "reason_code=not_found path=%s",
            cleaned_path,
        )
        result = _tool_result("error", f"File not found: {cleaned_path}")
        result["reason_code"] = "not_found"
        return result
    except Exception as exc:
        logger.warning(
            "Workspace file link failed. event=workspace_file_link_failed path=%s error=%s",
            cleaned_path,
            exc,
        )
        return _tool_result("error", f"Could not build a download link for {cleaned_path}: {exc}")

    workspace_path = "/".join(workspace_parts)
    filename = workspace_parts[-1]
    encoded_path = quote(workspace_path, safe="-_.~")
    base_url = website_base_url()
    payload = {
        "content_type": workspace_download_content_type(filename),
        "download_url": f"{base_url}/api/web-chat/workspace/download?path={encoded_path}",
        "filename": filename,
        "size_bytes": verified_stat.st_size,
        "view_url": f"{base_url}/webchat?view=files&path={encoded_path}",
        "workspace_path": workspace_path,
    }
    return _tool_result("success", json.dumps(payload, indent=2, sort_keys=True))
