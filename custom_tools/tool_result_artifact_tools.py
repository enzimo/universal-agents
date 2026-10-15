"""Bounded inspection tools for offloaded tool-result artifacts."""

from __future__ import annotations

from access_control.ownership import (
    PRIVATE_READ,
    RESOURCE_TASK,
    private_access,
)

import json
from collections import deque
from pathlib import Path
from typing import Any, Dict, Optional

from strands import tool

from custom_tools.runtime_context import get_agent_ref
from custom_tools.workspace_scope import runtime_layout

DEFAULT_MAX_CHARS = 6000
HARD_MAX_CHARS = 12000
DEFAULT_MAX_LINES = 80
HARD_MAX_LINES = 200
HARD_CONTEXT_LINES = 10


def _owns_artifact(manifest: Dict[str, Any]) -> bool:
    """Check runtime identity and task ownership, including archived own tasks."""
    from custom_tools.runtime_context import active_task_context

    agent = get_agent_ref()
    if agent is None or manifest.get("agent_id") != agent.id or manifest.get("team_id") != agent.team_id:
        return False
    task_id = manifest.get("task_id")
    current_task_id, _meta = active_task_context(agent)
    if not task_id or not current_task_id:
        return False
    if task_id == current_task_id:
        return True
    from custom_agents.access_runtime import verified_person_id

    owner = verified_person_id(agent)  # the signed context's person, never a message field
    if not owner:
        return False
    manager = agent.task_manager
    info = manager.get_task_info(task_id)
    if info is not None:
        target_owner = info.owner_person_id  # recorded at admission, like the archived row's
    else:
        archived = manager.task_archive_repository.get_archived_task(
            task_id, owner_agent_id=agent.id, include_all_agents=False,
            owner_canonical_user_id=owner,
        )
        target_owner = archived.owner_canonical_user_id if archived else None
    return private_access(
        person_id=owner, owner_id=target_owner,
        resource_type=RESOURCE_TASK, resource_id=task_id, action=PRIVATE_READ,
        team_id=agent.team_id,
    ).allowed


def _json_result(status: str, payload: Dict[str, Any]) -> Dict[str, Any]:
    return {"status": status, "content": [{"text": json.dumps(payload, indent=2, sort_keys=True)}]}


def _safe_int(value: Any, *, default: int, minimum: int, maximum: int) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        parsed = default
    return max(minimum, min(maximum, parsed))


def _artifact_root() -> Path:
    """state/tool_results; this tool is the only agent-facing way to read it."""

    return runtime_layout().tool_results_dir


def _resolve_manifest(path: str) -> tuple[Optional[Path], Optional[str]]:
    raw = Path(path).expanduser()
    resolved = raw.resolve()
    if resolved.is_dir():
        resolved = (resolved / "manifest.json").resolve()
    root = _artifact_root()
    try:
        resolved.relative_to(root)
    except ValueError:
        return None, f"Path must be under {root}: {resolved}"
    if resolved.name != "manifest.json":
        return None, "Provide a manifest.json path or its containing artifact directory."
    return resolved, None


def _load_manifest(path: Path) -> Dict[str, Any]:
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValueError("The artifact manifest must be a JSON object.")
    return data


def _manifest_summary(manifest: Dict[str, Any]) -> Dict[str, Any]:
    blocks = [
        {
            "index": block.get("index"),
            "content_type": block.get("content_type"),
            "path": block.get("path"),
            "bytes": block.get("bytes"),
            "chars": block.get("chars"),
        }
        for block in list(manifest.get("blocks") or [])[:80]
        if isinstance(block, dict)
    ]
    return {
        "manifest_version": manifest.get("manifest_version"),
        "created_at": manifest.get("created_at"),
        "reason": manifest.get("reason"),
        "task_id": manifest.get("task_id"),
        "conversation_id": manifest.get("conversation_id"),
        "tool_name": manifest.get("tool_name"),
        "tool_use_id": manifest.get("tool_use_id"),
        "original_tokens": manifest.get("original_tokens"),
        "original_chars": manifest.get("original_chars"),
        "max_tool_result_tokens_per_call": manifest.get("max_tool_result_tokens_per_call"),
        "blocks": blocks,
        "truncated_blocks": len(manifest.get("blocks") or []) > 80,
    }


def _select_block(manifest: Dict[str, Any], block_index: Optional[int]) -> tuple[Optional[Dict[str, Any]], Optional[str]]:
    blocks = [block for block in manifest.get("blocks") or [] if isinstance(block, dict)]
    if not blocks:
        return None, "Manifest does not contain inspectable blocks."
    index = 0 if block_index is None else block_index
    for block in blocks:
        if int(block.get("index") or 0) == index:
            return block, None
    return None, f"Block index {index} not found."


def _resolve_block_path(block: Dict[str, Any]) -> tuple[Optional[Path], Optional[str]]:
    raw_path = block.get("path")
    if not raw_path:
        return None, "Selected block does not include a path."
    resolved = Path(str(raw_path)).expanduser().resolve()
    try:
        resolved.relative_to(_artifact_root())
    except ValueError:
        return None, f"Block path is outside tool-result artifacts: {resolved}"
    if not resolved.exists():
        return None, f"Block file not found: {resolved}"
    return resolved, None


def _is_binary_block(block: Dict[str, Any]) -> bool:
    content_type = str(block.get("content_type") or "")
    return not (content_type.startswith("text/") or content_type == "application/json")


def _line_count(path: Path) -> int:
    count = 0
    with path.open("r", encoding="utf-8", errors="replace") as handle:
        for count, _ in enumerate(handle, start=1):
            pass
    return count


def _bounded_lines(path: Path, *, start_line: int, max_lines: int, max_chars: int) -> Dict[str, Any]:
    lines = []
    chars = 0
    next_line = None
    with path.open("r", encoding="utf-8", errors="replace") as handle:
        for line_number, line in enumerate(handle, start=1):
            if line_number < start_line:
                continue
            if len(lines) >= max_lines or chars >= max_chars:
                next_line = line_number
                break
            remaining = max_chars - chars
            chunk = line[:remaining]
            lines.append({"line": line_number, "text": chunk.rstrip("\n")})
            chars += len(chunk)
    return {
        "start_line": start_line,
        "line_count_returned": len(lines),
        "max_lines": max_lines,
        "max_chars": max_chars,
        "next_line": next_line,
        "truncated": next_line is not None,
        "lines": lines,
    }


def _grep(path: Path, *, query: str, max_lines: int, max_chars: int, context_lines: int) -> Dict[str, Any]:
    if not query:
        return {"matches": [], "truncated": False, "message": "query is required for grep"}
    selected: list[Dict[str, Any]] = []
    chars = 0
    truncated = False
    query_lower = query.lower()
    before: deque[tuple[int, str]] = deque(maxlen=context_lines)
    after_remaining = 0
    with path.open("r", encoding="utf-8", errors="replace") as handle:
        for line_number, raw_line in enumerate(handle, start=1):
            line = raw_line.rstrip("\n")
            is_match = query_lower in line.lower()
            rows: list[tuple[int, str, bool]] = []
            if is_match:
                rows.extend((before_line, before_text, False) for before_line, before_text in before)
                rows.append((line_number, line, True))
                after_remaining = context_lines
            elif after_remaining > 0:
                rows.append((line_number, line, False))
                after_remaining -= 1
            for row_line, row_text, row_match in rows:
                if selected and selected[-1]["line"] == row_line:
                    if row_match:
                        selected[-1]["match"] = True
                    continue
                if len(selected) >= max_lines or chars >= max_chars:
                    truncated = True
                    break
                remaining = max_chars - chars
                selected.append({"line": row_line, "text": row_text[:remaining], "match": row_match})
                chars += min(len(row_text), remaining)
            if truncated:
                break
            before.append((line_number, line))
    return {"query": query, "matches": selected, "match_line_count": sum(1 for item in selected if item["match"]), "truncated": truncated}


@tool
def inspect_tool_result_artifact(
    manifest_path: str,
    block_index: Optional[int] = None,
    action: str = "manifest",
    start_line: int = 1,
    max_lines: int = DEFAULT_MAX_LINES,
    max_chars: int = DEFAULT_MAX_CHARS,
    query: str = "",
    context_lines: int = 3,
) -> Dict[str, Any]:
    """
    Safely inspect an offloaded tool-result artifact using bounded reads.

    Use this for manifests produced by oversized tool-result offloading. This
    tool never returns binary block content and enforces hard line/character
    limits for text blocks.
    """
    manifest, error = _resolve_manifest(manifest_path)
    if error:
        return _json_result("error", {"status": "failed", "failure_stage": "input_validation", "failure_reason": "path_outside_artifacts", "message": error, "retryable": False})
    assert manifest is not None
    if not manifest.exists():
        return _json_result("error", {"status": "failed", "failure_stage": "input_validation", "failure_reason": "file_not_found", "message": f"Manifest not found: {manifest}", "retryable": True})

    action = (action or "manifest").strip().lower()
    max_lines = _safe_int(max_lines, default=DEFAULT_MAX_LINES, minimum=1, maximum=HARD_MAX_LINES)
    max_chars = _safe_int(max_chars, default=DEFAULT_MAX_CHARS, minimum=200, maximum=HARD_MAX_CHARS)
    start_line = _safe_int(start_line, default=1, minimum=1, maximum=10_000_000)
    context_lines = _safe_int(context_lines, default=3, minimum=0, maximum=HARD_CONTEXT_LINES)

    try:
        data = _load_manifest(manifest)
    except Exception as exc:
        return _json_result("error", {"status": "failed", "failure_stage": "manifest_read", "failure_reason": "invalid_manifest", "message": str(exc), "retryable": False})

    if not _owns_artifact(data):
        return _json_result("error", {"status": "failed", "failure_reason": "artifact_owner_required", "message": "Only this agent's tool results for the current task owner are available.", "retryable": False})

    if action == "manifest":
        return _json_result("success", {"status": "success", "action": action, "manifest_path": str(manifest), **_manifest_summary(data)})

    block, error = _select_block(data, block_index)
    if error:
        return _json_result("error", {"status": "failed", "failure_stage": "input_validation", "failure_reason": "block_not_found", "message": error, "retryable": True})
    assert block is not None
    block_path, error = _resolve_block_path(block)
    if error:
        return _json_result("error", {"status": "failed", "failure_stage": "input_validation", "failure_reason": "block_path_invalid", "message": error, "retryable": True})
    assert block_path is not None
    if not block_path.is_relative_to(manifest.parent):
        return _json_result("error", {"status": "failed", "failure_reason": "block_path_invalid", "message": "Artifact blocks must remain in their manifest directory.", "retryable": False})

    stat = {
        "block_index": block.get("index"),
        "content_type": block.get("content_type"),
        "path": str(block_path),
        "file_bytes": block_path.stat().st_size,
        "is_binary": _is_binary_block(block),
    }
    if _is_binary_block(block):
        return _json_result("success", {"status": "success", "action": action, **stat, "message": "Binary block content is not returned inline."})
    stat["line_count"] = _line_count(block_path)

    if action == "stat":
        payload = {"status": "success", "action": action, **stat, "truncated": False}
    elif action == "head":
        payload = {"status": "success", "action": action, **stat, **_bounded_lines(block_path, start_line=1, max_lines=max_lines, max_chars=max_chars)}
    elif action == "tail":
        payload = {
            "status": "success",
            "action": action,
            **stat,
            **_bounded_lines(block_path, start_line=max(1, int(stat["line_count"]) - max_lines + 1), max_lines=max_lines, max_chars=max_chars),
        }
    elif action == "read_range":
        payload = {"status": "success", "action": action, **stat, **_bounded_lines(block_path, start_line=start_line, max_lines=max_lines, max_chars=max_chars)}
    elif action == "grep":
        payload = {"status": "success", "action": action, **stat, **_grep(block_path, query=query, max_lines=max_lines, max_chars=max_chars, context_lines=context_lines)}
    else:
        return _json_result(
            "error",
            {
                "status": "failed",
                "failure_stage": "input_validation",
                "failure_reason": "unsupported_action",
                "message": "action must be one of: manifest, stat, head, tail, read_range, grep",
                "retryable": True,
            },
        )
    return _json_result("success", payload)
