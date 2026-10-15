import hashlib
import json
from typing import Any, Dict, Iterable, List, Optional, Protocol

from custom_agents.conversation_store import ConversationStore


class ConversationArtifactOffloader(Protocol):
    def extract_large_text_artifacts(
        self,
        *,
        store: ConversationStore,
        conversation_id: str,
        task_id: str,
        messages: List[Dict[str, Any]],
    ) -> List[Dict[str, Any]]:
        ...


class RecursiveLargeTextArtifactOffloader:
    def __init__(self, min_chars: int = 4000) -> None:
        self.min_chars = min_chars

    @staticmethod
    def _prune_text(text: str) -> str:
        return " ".join((text or "").strip().split())

    @staticmethod
    def _truncate_text(text: str, limit: int = 280) -> str:
        if len(text) <= limit:
            return text
        return text[: limit - 3].rstrip() + "..."

    def extract_large_text_artifacts(
        self,
        *,
        store: ConversationStore,
        conversation_id: str,
        task_id: str,
        messages: List[Dict[str, Any]],
    ) -> List[Dict[str, Any]]:
        artifacts = []
        seen_hashes = set()

        for path, text in self._iter_tool_result_texts(messages):
            text = text.strip()
            marker_artifact = self._runtime_context_marker_artifact(
                text,
                path=path,
                conversation_id=conversation_id,
                task_id=task_id,
            )
            if marker_artifact:
                marker_key = marker_artifact.get("storage_path") or marker_artifact.get("summary_text")
                if marker_key and marker_key not in seen_hashes:
                    seen_hashes.add(str(marker_key))
                    artifacts.append(marker_artifact)
                continue
            if len(text) < self.min_chars or self._is_runtime_context_marker(text):
                continue
            digest = hashlib.sha256(text.encode("utf-8")).hexdigest()
            if digest in seen_hashes:
                continue
            seen_hashes.add(digest)
            summary = self._truncate_text(self._prune_text(text), 280)
            artifact = store.offload_large_text(
                conversation_id=conversation_id,
                task_id=task_id,
                artifact_type="tool_output",
                raw_text=text,
                summary_text=f"{path}: {summary}",
            )
            artifacts.append(artifact)
        return artifacts

    @staticmethod
    def _iter_tool_result_texts(messages: List[Dict[str, Any]]) -> Iterable[tuple[str, str]]:
        for message_index, message in enumerate(messages or []):
            if not isinstance(message, dict):
                continue
            for content_index, content in enumerate(message.get("content", []) or []):
                if not isinstance(content, dict):
                    continue
                tool_result = content.get("toolResult")
                if not isinstance(tool_result, dict):
                    continue
                for item_index, item in enumerate(tool_result.get("content", []) or []):
                    if isinstance(item, dict) and isinstance(item.get("text"), str):
                        yield (
                            f"messages[{message_index}].content[{content_index}].toolResult.content[{item_index}].text",
                            item["text"],
                        )

    @staticmethod
    def _is_runtime_context_marker(text: str) -> bool:
        try:
            payload = json.loads(text)
        except (TypeError, ValueError):
            return False
        if not isinstance(payload, dict):
            return False
        return bool(
            payload.get("filtered_tool_result")
            or payload.get("offloaded_tool_result")
            or payload.get("inner_loop_context_warning")
        )

    @staticmethod
    def _runtime_context_marker_artifact(
        text: str,
        *,
        path: str,
        conversation_id: str,
        task_id: str,
    ) -> Optional[Dict[str, Any]]:
        try:
            payload = json.loads(text)
        except (TypeError, ValueError):
            return None
        if not isinstance(payload, dict) or not payload.get("offloaded_tool_result"):
            return None
        manifest_path = payload.get("artifact_manifest_path")
        if not manifest_path:
            return None
        tool_name = payload.get("tool_name") or "unknown_tool"
        tool_use_id = payload.get("tool_use_id") or "unknown_tool_use"
        original_tokens = payload.get("original_tokens")
        summary = f"{path}: offloaded tool result for {tool_name} ({tool_use_id})"
        if original_tokens is not None:
            summary = f"{summary}; original_tokens={original_tokens}"
        return {
            "artifact_id": f"tool_result_manifest:{manifest_path}",
            "conversation_id": conversation_id,
            "task_id": task_id,
            "artifact_type": "tool_result_manifest",
            "storage_path": str(manifest_path),
            "summary_text": summary,
            "created_at": payload.get("created_at"),
            "source": "offloaded_tool_result",
            "tool_name": tool_name,
            "tool_use_id": tool_use_id,
            "original_tokens": original_tokens,
            "original_chars": payload.get("original_chars"),
        }
