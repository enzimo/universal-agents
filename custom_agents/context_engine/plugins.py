from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from custom_agents.context_engine.base import (
    AssembledContext,
    ContextAssemblyRequest,
    ContextCompactionReason,
    TurnFinalizationRequest,
)
from custom_agents.conversation_compaction.base import CompactionResult


class ContextLifecycleRecorderPlugin:
    """Record context lifecycle hook calls to JSONL for diagnostics and live tests."""

    def __init__(
        self,
        *,
        output_path: str,
        diagnostics_dir: str | Path,
        logger: logging.Logger | None = None,
    ) -> None:
        # Diagnostics are runtime records (state/diagnostics); agents cannot
        # open them. A configured absolute path stays as given.
        raw_path = Path(output_path).expanduser()
        self.output_path = (
            raw_path
            if raw_path.is_absolute()
            else Path(diagnostics_dir).expanduser().resolve() / raw_path
        )
        self.logger = logger or logging.getLogger(__name__)
        self.output_path.parent.mkdir(parents=True, exist_ok=True)

    def ingest(self, request: ContextAssemblyRequest) -> None:
        self._append(
            "ingest",
            {
                "task_id": getattr(request.incoming_content, "task_id", None),
                "conversation_id": getattr(request.incoming_meta, "conversation_id", None),
                "message_chars": len(request.incoming_message_json or ""),
            },
        )

    def assemble(self, request: ContextAssemblyRequest, assembly: AssembledContext) -> None:
        self._append(
            "assemble",
            {
                "task_id": getattr(request.incoming_content, "task_id", None),
                "conversation_id": getattr(request.incoming_meta, "conversation_id", None),
                "estimated_tokens": assembly.estimated_tokens,
                "soft_limit_tokens": assembly.soft_limit_tokens,
                "hard_limit_tokens": assembly.hard_limit_tokens,
                "warning_count": len(assembly.warnings),
                "section_names": [section.name for section in assembly.sections],
            },
        )

    def compact(
        self,
        reason: ContextCompactionReason,
        request: ContextAssemblyRequest | None = None,
    ) -> CompactionResult | None:
        self._append(
            "compact",
            {
                "reason": reason.value,
                "task_id": getattr(getattr(request, "incoming_content", None), "task_id", None),
                "conversation_id": getattr(getattr(request, "incoming_meta", None), "conversation_id", None),
            },
        )
        return None

    def after_turn(self, request: TurnFinalizationRequest) -> None:
        self._append(
            "after_turn",
            {
                "task_id": getattr(getattr(request, "task_info", None).task, "task_id", None)
                if getattr(request, "task_info", None) is not None
                else None,
                "task_status": request.task_status,
                "strands_message_count": len(request.strands_messages or []),
            },
        )

    def _append(self, event: str, payload: dict[str, Any]) -> None:
        record = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "event": event,
            **payload,
        }
        try:
            with self.output_path.open("a", encoding="utf-8") as handle:
                handle.write(json.dumps(record, sort_keys=True, default=str) + "\n")
        except Exception as exc:
            self.logger.warning("Failed to record context lifecycle event %s to %s: %s", event, self.output_path, exc)


def build_context_engine_plugins(
    plugin_configs: list[dict[str, Any]] | None,
    *,
    diagnostics_dir: str | Path,
    logger: logging.Logger | None = None,
) -> list[Any]:
    plugins: list[Any] = []
    for plugin_config in plugin_configs or []:
        if not isinstance(plugin_config, dict):
            continue
        plugin_type = str(plugin_config.get("type") or "").strip().lower()
        if plugin_type in {"lifecycle_recorder", "context_lifecycle_recorder"}:
            output_path = str(plugin_config.get("output_path") or "context_lifecycle.jsonl")
            plugins.append(
                ContextLifecycleRecorderPlugin(
                    output_path=output_path,
                    diagnostics_dir=diagnostics_dir,
                    logger=logger,
                )
            )
            continue
        raise ValueError(f"Unsupported context engine plugin type: {plugin_type!r}")
    return plugins
