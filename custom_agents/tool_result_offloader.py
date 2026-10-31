"""Task-aware tool-result offloading for oversized Strands tool outputs."""

from __future__ import annotations

from custom_tools.runtime_context import bound_task_id
import hashlib
import json
import logging
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Literal, Optional, Union

from strands.hooks import AfterToolCallEvent, HookRegistry

from custom_agents.structured_llm_messages import ModelWorkerTurnDecision
from custom_agents.token_estimation import count_message_tokens, truncate_to_token_budget

logger = logging.getLogger(__name__)

# Saved results live in state/tool_results, which file tools and the sandbox
# cannot reach; inspect_tool_result_artifact is the one way an agent reads them.
SAVED_RESULT_MESSAGE = (
    "The full result is saved. Read the parts you need with inspect_tool_result_artifact("
    "manifest_path=<artifact_manifest_path>, action=\"head\", \"grep\" or \"read_range\"); "
    "do not read all of it back into context."
)

STRUCTURED_OUTPUT_TOOL_NAMES = {
    "ModelAgentTurnDecision",
    ModelWorkerTurnDecision.__name__,
    "StructuredAgentTurnDecision",
    "StructuredLLMResponse",
}


@dataclass
class ToolResultOffloaderConfig:
    enabled: bool = True
    max_tokens_per_call: Optional[int] = 6250
    preview_tokens: int = 1000
    preserve_original_status: bool = True
    offload_text_blocks: bool = True
    offload_json_blocks: bool = True
    offload_binary_blocks: bool = True


class TaskAwareWorkspaceStorage:
    """Storage for offloaded tool-result blocks and manifests in state/tool_results."""

    def __init__(
        self,
        *,
        artifact_root: Union[str, Path],
        task_manager: Any = None,
        team_id: Optional[str] = None,
        agent_id: Optional[str] = None,
        named_agent_type: Optional[str] = None,
        logger_: Optional[logging.Logger] = None,
    ) -> None:
        self.artifact_root = Path(artifact_root).expanduser().resolve()
        self.task_manager = task_manager
        self.team_id = team_id
        self.agent_id = agent_id
        self.named_agent_type = named_agent_type
        self.logger = logger_ or logger

    def store_result(
        self,
        *,
        result: Any,
        event: AfterToolCallEvent,
        tool_name: str,
        result_tokens: int,
        result_text: str,
        max_tokens_per_call: int,
        preview_tokens: int,
        replacement_status: Literal["success", "error"],
    ) -> Dict[str, Any]:
        tool_use_id = str(event.tool_use.get("toolUseId") or self._value_from_result(result, "toolUseId") or "tool")
        original_status = self._value_from_result(result, "status")
        stored = self.write_artifact(
            result=result,
            identity=self._identity(event),
            tool_name=tool_name,
            tool_use_id=tool_use_id,
            reason="max_tool_result_tokens_per_call_exceeded",
            details={
                "original_status": original_status,
                "replacement_status": replacement_status,
                "original_tokens": result_tokens,
                "original_chars": len(result_text),
                "max_tool_result_tokens_per_call": max_tokens_per_call,
                "preview_tokens": preview_tokens,
            },
        )
        return {**stored, "original_status": original_status}

    def write_artifact(
        self,
        *,
        result: Any,
        identity: Dict[str, Any],
        tool_name: str,
        tool_use_id: str,
        reason: str,
        details: Dict[str, Any],
    ) -> Dict[str, Any]:
        """Save one tool result as a manifest folder that inspect_tool_result_artifact opens.

        Every saved tool result uses this one format (the per-call offloader
        and the inner-loop context manager alike): a folder per result with
        ``manifest.json`` naming its agent, team and task (the inspect tool's
        ownership check) and one file per content block.
        """

        digest = hashlib.sha256(_safe_json_dumps(result).encode("utf-8")).hexdigest()[:16]
        timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        task_slug = _safe_slug(str(identity.get("task_id") or "no_active_task"))
        stem = f"{timestamp}_{_safe_slug(tool_use_id)}_{_safe_slug(tool_name)}_{digest}"
        artifact_root = self.artifact_root / task_slug / stem
        artifact_root.mkdir(parents=True, exist_ok=True)

        blocks = self._write_blocks(artifact_root, result)
        manifest_path = artifact_root / "manifest.json"
        manifest = {
            "manifest_version": 1,
            "created_at": datetime.now(timezone.utc).isoformat(),
            "reason": reason,
            "task_id": identity.get("task_id"),
            "conversation_id": identity.get("conversation_id"),
            "team_id": identity.get("team_id"),
            "agent_id": identity.get("agent_id"),
            "named_agent_type": identity.get("named_agent_type"),
            "tool_name": tool_name,
            "tool_use_id": tool_use_id,
            **details,
            "blocks": blocks,
        }
        manifest_path.write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        return {
            "artifact_dir": str(artifact_root),
            "artifact_manifest_path": str(manifest_path),
            "blocks": blocks,
            "identity": identity,
        }

    def _write_blocks(self, artifact_root: Path, result: Any) -> list[Dict[str, Any]]:
        content = result.get("content") if isinstance(result, dict) else None
        source_blocks = content if isinstance(content, list) and content else [{"json": result}]
        blocks: list[Dict[str, Any]] = []
        for index, block in enumerate(source_blocks):
            content_bytes, content_type, suffix, chars = self._block_payload(block)
            path = artifact_root / f"block_{index}{suffix}"
            path.write_bytes(content_bytes)
            blocks.append(
                {
                    "index": index,
                    "content_type": content_type,
                    "path": str(path),
                    "bytes": path.stat().st_size,
                    "chars": chars,
                    "sha256": hashlib.sha256(content_bytes).hexdigest(),
                }
            )
        return blocks

    @staticmethod
    def _block_payload(block: Any) -> tuple[bytes, str, str, int]:
        if isinstance(block, dict) and "text" in block:
            text = str(block.get("text") or "")
            data = text.encode("utf-8")
            return data, "text/plain", ".txt", len(text)
        if isinstance(block, dict) and "json" in block:
            text = json.dumps(block.get("json"), indent=2, sort_keys=True, ensure_ascii=False, default=str)
            data = (text + "\n").encode("utf-8")
            return data, "application/json", ".json", len(text)
        if isinstance(block, dict) and "image" in block:
            image = block.get("image") if isinstance(block.get("image"), dict) else {}
            image_format = str(image.get("format") or "unknown").lower()
            data = _bytes_from_source(image.get("source"))
            return data, f"image/{image_format}", f".{_safe_slug(image_format, fallback='bin')}", 0
        if isinstance(block, dict) and "document" in block:
            document = block.get("document") if isinstance(block.get("document"), dict) else {}
            document_format = str(document.get("format") or "unknown").lower()
            data = _bytes_from_source(document.get("source"))
            return data, f"application/{document_format}", f".{_safe_slug(document_format, fallback='bin')}", 0
        text = _safe_json_dumps(block)
        data = (text + "\n").encode("utf-8")
        return data, "application/json", ".json", len(text)

    def _identity(self, event: AfterToolCallEvent) -> Dict[str, Any]:
        return self.identity_for(getattr(event, "agent", None))

    def identity_for(self, agent: Any) -> Dict[str, Any]:
        """Who a saved result belongs to: the agent, its team and its current task."""

        task_manager = getattr(agent, "task_manager", None) or self.task_manager
        task_id = bound_task_id()
        task_info = None
        if task_manager is not None and task_id:
            try:
                task_info = task_manager.get_task_info(task_id)
            except Exception:
                task_info = None
        task_meta = getattr(task_info, "meta", None)
        return {
            "task_id": task_id,
            "conversation_id": getattr(task_meta, "conversation_id", None),
            "team_id": getattr(agent, "team_id", None) or self.team_id,
            "agent_id": getattr(agent, "id", None) or self.agent_id,
            "named_agent_type": getattr(agent, "named_agent_type", None) or self.named_agent_type,
        }

    @staticmethod
    def _value_from_result(result: Any, key: str) -> Any:
        return result.get(key) if isinstance(result, dict) else None


class TaskAwareToolResultOffloader:
    """Replace oversized tool results with bounded previews and artifact references."""

    _EVENTS_KEY = "tool_result_budget_events"

    def __init__(
        self,
        *,
        config: Optional[ToolResultOffloaderConfig] = None,
        max_tokens_per_call: Optional[int] = None,
        model: Any = None,
        artifact_root: Union[str, Path],
        task_manager: Any = None,
        team_id: Optional[str] = None,
        agent_id: Optional[str] = None,
        named_agent_type: Optional[str] = None,
        logger_: Optional[logging.Logger] = None,
    ) -> None:
        self.config = config or ToolResultOffloaderConfig(max_tokens_per_call=max_tokens_per_call)
        if max_tokens_per_call is not None:
            self.config.max_tokens_per_call = max_tokens_per_call
        self.model = model
        self.task_manager = task_manager
        self.logger = logger_ or logger
        self.storage = TaskAwareWorkspaceStorage(
            artifact_root=artifact_root,
            task_manager=task_manager,
            team_id=team_id,
            agent_id=agent_id,
            named_agent_type=named_agent_type,
            logger_=self.logger,
        )

    def register_hooks(self, registry: HookRegistry) -> None:
        registry.add_callback(AfterToolCallEvent, self._after_tool_call)

    def _after_tool_call(self, event: AfterToolCallEvent) -> None:
        if not self.config.enabled:
            return
        per_call_limit = _resolved_positive_int(self.config.max_tokens_per_call)
        if per_call_limit is None:
            return
        if _is_structured_output_tool(event):
            return
        if self._is_already_offloaded(event.result):
            return

        result_text = _safe_json_dumps(event.result)
        result_tokens = self._estimate_tool_result_tokens(event)
        if result_tokens <= per_call_limit:
            return

        tool_name = _tool_name(event)
        original_status = self._result_value(event.result, "status")
        replacement_status = self._normalized_replacement_status(
            original_status,
            tool_name=tool_name,
            tool_use_id=event.tool_use.get("toolUseId"),
        )
        try:
            stored = self.storage.store_result(
                result=event.result,
                event=event,
                tool_name=tool_name,
                result_tokens=result_tokens,
                result_text=result_text,
                max_tokens_per_call=per_call_limit,
                preview_tokens=self.config.preview_tokens,
                replacement_status=replacement_status,
            )
        except Exception as exc:
            self.logger.warning(
                "Failed to offload oversized tool result: tool=%s toolUseId=%s error=%s",
                tool_name,
                event.tool_use.get("toolUseId"),
                exc,
            )
            return

        payload = self._replacement_payload(
            event=event,
            tool_name=tool_name,
            result_tokens=result_tokens,
            result_text=result_text,
            per_call_limit=per_call_limit,
            stored=stored,
        )
        request_state = event.invocation_state.setdefault("request_state", {})
        event_details = {
            "reason": "per_call_limit_exceeded",
            "tool_name": tool_name,
            "tool_use_id": event.tool_use.get("toolUseId"),
            "result_tokens": result_tokens,
            "result_chars": len(result_text),
            "max_tool_result_tokens_per_call": per_call_limit,
            "original_status": stored.get("original_status"),
            "artifact_path": stored.get("artifact_manifest_path"),
            "artifact_manifest_path": stored.get("artifact_manifest_path"),
            "artifact_dir": stored.get("artifact_dir"),
            "original_result_preview": _compact_preview(event.result, max_chars=1200),
        }
        request_state.setdefault(self._EVENTS_KEY, []).append(event_details)
        self.logger.warning(
            "Tool result offloaded: tool=%s toolUseId=%s result_tokens=%s max_tokens=%s manifest=%s",
            tool_name,
            event.tool_use.get("toolUseId"),
            result_tokens,
            per_call_limit,
            stored.get("artifact_manifest_path"),
        )

        event.result = {
            "toolUseId": event.tool_use.get("toolUseId") or self._result_value(event.result, "toolUseId"),
            "status": replacement_status,
            "content": [{"text": json.dumps(payload, indent=2, sort_keys=True)}],
        }

    def _normalized_replacement_status(
        self,
        original_status: object,
        *,
        tool_name: str,
        tool_use_id: object,
    ) -> Literal["success", "error"]:
        if not self.config.preserve_original_status:
            return "success"
        if original_status == "error":
            return "error"
        if original_status in {None, "", "success"}:
            return "success"
        self.logger.warning(
            "Oversized tool result had an invalid status; replacement will fail closed. "
            "event=tool_result_invalid_status tool=%s toolUseId=%s original_status=%r",
            tool_name,
            tool_use_id,
            original_status,
        )
        return "error"

    def _estimate_tool_result_tokens(self, event: AfterToolCallEvent) -> int:
        model = getattr(getattr(event, "agent", None), "model", None) or self.model
        return count_message_tokens(model, [{"role": "user", "content": [{"toolResult": event.result}]}])

    def _replacement_payload(
        self,
        *,
        event: AfterToolCallEvent,
        tool_name: str,
        result_tokens: int,
        result_text: str,
        per_call_limit: int,
        stored: Dict[str, Any],
    ) -> Dict[str, Any]:
        identity = stored.get("identity") if isinstance(stored.get("identity"), dict) else {}
        return {
            "offloaded_tool_result": True,
            "status": "offloaded",
            "message": f"This tool result was larger than the per-call limit. {SAVED_RESULT_MESSAGE}",
            "tool_name": tool_name,
            "tool_use_id": event.tool_use.get("toolUseId") or self._result_value(event.result, "toolUseId"),
            "task_id": identity.get("task_id"),
            "conversation_id": identity.get("conversation_id"),
            "artifact_manifest_path": stored.get("artifact_manifest_path"),
            "artifact_dir": stored.get("artifact_dir"),
            "original_tokens": result_tokens,
            "original_chars": len(result_text),
            "max_tool_result_tokens_per_call": per_call_limit,
            "preview_tokens": self.config.preview_tokens,
            "preview": truncate_to_token_budget(
                self._preview_source(event.result),
                max_tokens=max(0, int(self.config.preview_tokens)),
                estimate=lambda text: count_message_tokens(
                    getattr(getattr(event, "agent", None), "model", None) or self.model,
                    [{"role": "user", "content": [{"text": text}]}],
                ),
            ),
            "blocks": [
                {
                    "index": block.get("index"),
                    "content_type": block.get("content_type"),
                    "path": block.get("path"),
                    "bytes": block.get("bytes"),
                }
                for block in stored.get("blocks", [])
                if isinstance(block, dict)
            ],
            "mitigation_hints": [
                "Read only bounded excerpts with inspect_tool_result_artifact.",
                "Rerun the prior tool with narrower parameters when possible.",
            ],
        }

    @staticmethod
    def _preview_source(result: Any) -> str:
        if isinstance(result, dict) and isinstance(result.get("content"), list):
            parts: list[str] = []
            for block in result["content"]:
                if isinstance(block, dict) and "text" in block:
                    parts.append(str(block.get("text") or ""))
                elif isinstance(block, dict) and "json" in block:
                    parts.append(json.dumps(block.get("json"), indent=2, sort_keys=True, ensure_ascii=False, default=str))
            if parts:
                return "\n".join(parts)
        return _safe_json_dumps(result)

    @staticmethod
    def _is_already_offloaded(result: Any) -> bool:
        if not isinstance(result, dict):
            return False
        for block in result.get("content") or []:
            if not isinstance(block, dict) or "text" not in block:
                continue
            try:
                payload = json.loads(block.get("text") or "")
            except (TypeError, ValueError):
                continue
            if isinstance(payload, dict) and payload.get("offloaded_tool_result"):
                return True
        return False

    @staticmethod
    def _result_value(result: Any, key: str) -> Any:
        return result.get(key) if isinstance(result, dict) else None


def _is_structured_output_tool(event: AfterToolCallEvent) -> bool:
    selected_tool = event.selected_tool
    if getattr(selected_tool, "tool_type", None) == "structured_output":
        return True
    if getattr(selected_tool, "tool_name", None) in STRUCTURED_OUTPUT_TOOL_NAMES:
        return True
    return event.tool_use.get("name") in STRUCTURED_OUTPUT_TOOL_NAMES


def _tool_name(event: AfterToolCallEvent) -> str:
    return str(event.tool_use.get("name") or getattr(event.selected_tool, "tool_name", None) or "unknown")


def _resolved_positive_int(raw_value: Optional[int]) -> Optional[int]:
    if raw_value is None:
        return None
    try:
        value = int(raw_value)
    except (TypeError, ValueError):
        return None
    return value if value > 0 else None


def _safe_slug(value: str, *, fallback: str = "unknown") -> str:
    slug = re.sub(r"[^a-zA-Z0-9._-]+", "-", str(value or "").strip()).strip("-._")
    return (slug or fallback)[:80]


def _safe_json_dumps(value: Any) -> str:
    return json.dumps(value, indent=2, sort_keys=True, ensure_ascii=False, default=str)


def _compact_preview(value: Any, *, max_chars: int) -> str:
    text = _safe_json_dumps(value)
    if len(text) <= max_chars:
        return text
    return text[:max_chars] + f"...<truncated {len(text) - max_chars} chars>"


def _bytes_from_source(source: Any) -> bytes:
    if isinstance(source, dict):
        data = source.get("bytes")
    else:
        data = source
    if isinstance(data, bytes):
        return data
    if isinstance(data, bytearray):
        return bytes(data)
    if data is None:
        return b""
    return str(data).encode("utf-8", errors="replace")
