"""
Project-local file tools with direct, non-interactive behavior.

The stock Strands file helpers are exposed through module-style tool specs and
some include approval-oriented wording that makes agent behavior less
deterministic in live automation. These wrappers read and write UTF-8 text
files through the agent file library (``custom_tools.file_access``), which
checks the path for the task and never follows a link.
"""

import logging
from typing import Any, Dict

from strands import tool

from custom_tools import file_access
from custom_tools.path_policy import PathPolicyError

logger = logging.getLogger(__name__)


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
