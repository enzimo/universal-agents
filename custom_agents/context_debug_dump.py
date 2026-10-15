"""Debug artifact writer for model-visible context and runtime traces."""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import shutil
import time
from collections.abc import AsyncGenerator
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Optional

from custom_agents.context_engine.base import AssembledContext, ContextAssemblyRequest


DEFAULT_CONTEXT_DUMP_CONFIG: dict[str, Any] = {
    "enabled": False,
    "mode": "on_error",
    "include_system_prompt": True,
    "include_full_prompt": True,
    "include_context_sections": True,
    "include_source_lengths": True,
    "include_incoming_envelope_json": False,
    "include_strands_messages": True,
    "include_tool_results": True,
    "include_result": True,
    "include_reasoning_artifacts": True,
    "capture_model_calls": False,
    "include_model_call_inputs": False,
    "include_tool_specs": False,
    "redact_secrets": True,
    "retention_days": 14,
    "web_chat_controls_enabled": False,
}

_SENSITIVE_ASSIGNMENT_RE = re.compile(
    r"(?P<key>[A-Za-z][A-Za-z0-9_.-]{0,80})\s*[:=]\s*"
    r"(?P<value>\"[^\"\n]*\"|'[^'\n]*'|[^\s,;]+)",
    re.IGNORECASE,
)
_TOKEN_ACCOUNTING_PARTS = {
    "budget",
    "cache",
    "cached",
    "completion",
    "context",
    "count",
    "counts",
    "estimated",
    "hard",
    "input",
    "limit",
    "max",
    "maximum",
    "min",
    "minimum",
    "output",
    "projected",
    "prompt",
    "reserved",
    "soft",
    "total",
    "usage",
    "window",
}
_TOKEN_SECRET_PARTS = {
    "access",
    "activity",
    "admin",
    "auth",
    "bearer",
    "bot",
    "broker",
    "collector",
    "id",
    "refresh",
    "secure",
    "session",
    "user",
}


def _normalized_key_parts(key: Any) -> tuple[str, ...]:
    rendered = re.sub(r"(?<=[a-z0-9])(?=[A-Z])", "_", str(key or ""))
    return tuple(part for part in re.split(r"[^a-z0-9]+", rendered.casefold()) if part)


def is_sensitive_context_debug_key(key: Any) -> bool:
    """Classify secret-bearing keys without hiding token accounting metrics."""
    parts = _normalized_key_parts(key)
    if not parts:
        return False
    if parts[-1] in {"file", "path", "dir", "directory", "name"}:
        return False
    part_set = set(parts)
    if "password" in part_set or "secret" in part_set or "authorization" in part_set:
        return True
    if "private" in part_set and "key" in part_set:
        return True
    if "api" in part_set and "key" in part_set:
        return True
    if "credential" in part_set or "credentials" in part_set:
        return bool(part_set & {"data", "payload", "secret", "token", "value"}) or len(parts) == 1
    if "tokens" in part_set:
        return bool(part_set & _TOKEN_SECRET_PARTS) and not bool(part_set & _TOKEN_ACCOUNTING_PARTS)
    if "token" in part_set:
        if len(parts) == 1 or part_set & _TOKEN_SECRET_PARTS:
            return True
        return not bool(part_set & _TOKEN_ACCOUNTING_PARTS)
    return False


def merge_context_debug_dump_config(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return {**DEFAULT_CONTEXT_DUMP_CONFIG, **value}
    return dict(DEFAULT_CONTEXT_DUMP_CONFIG)


class ContextDebugDumpWriter:
    """Write opt-in diagnostic artifacts for one model invocation."""

    def __init__(
        self,
        *,
        config: dict[str, Any] | None,
        output_dir: str | Path,
        logger: Optional[logging.Logger] = None,
    ) -> None:
        self.config = merge_context_debug_dump_config(config)
        self.logger = logger or logging.getLogger(__name__)
        # state/diagnostics/context_dumps: runtime records agents cannot open.
        self.output_dir = Path(output_dir).expanduser().resolve()
        self._secret_values = self._collect_secret_values()
        self._cleanup_done = False

    @property
    def enabled(self) -> bool:
        return bool(self.config.get("enabled", False))

    def should_dump_pre(self, assembly: AssembledContext) -> bool:
        if not self.enabled:
            return False
        mode = self._mode()
        if mode == "always":
            return True
        if mode == "on_budget_warning":
            return bool(assembly.warnings)
        return False

    def should_dump_post(self, assembly: AssembledContext, *, error: BaseException | None = None) -> bool:
        if not self.enabled:
            return False
        mode = self._mode()
        if mode == "always":
            return True
        if mode == "on_error":
            return error is not None
        if mode == "on_budget_warning":
            return bool(assembly.warnings)
        return False

    def dump_pre_invocation(
        self,
        *,
        request: ContextAssemblyRequest,
        assembly: AssembledContext,
        agent_metadata: dict[str, Any],
    ) -> Path | None:
        if not self.should_dump_pre(assembly):
            return None
        dump_dir = self._create_dump_dir(request=request, phase="pre")
        self._write_pre_files(dump_dir=dump_dir, request=request, assembly=assembly, agent_metadata=agent_metadata)
        return dump_dir

    def dump_post_turn(
        self,
        *,
        request: ContextAssemblyRequest,
        assembly: AssembledContext,
        agent_metadata: dict[str, Any],
        dump_dir: Path | None = None,
        result: Any = None,
        parsed_content: Any = None,
        strands_messages: list[dict[str, Any]] | None = None,
        reasoning_artifacts: dict[str, Any] | None = None,
        model_calls: list[dict[str, Any]] | None = None,
        task_history_summary: str | None = None,
        error: BaseException | None = None,
    ) -> Path | None:
        if not self.should_dump_post(assembly, error=error):
            return dump_dir
        dump_dir = dump_dir or self._create_dump_dir(request=request, phase="post")
        if not (dump_dir / "pre_metadata.json").exists():
            self._write_pre_files(dump_dir=dump_dir, request=request, assembly=assembly, agent_metadata=agent_metadata)
        self._write_json(
            dump_dir / "post_metadata.json",
            {
                "phase": "post",
                "created_at": datetime.now(timezone.utc).isoformat(),
                "agent": self._agent_metadata_summary(agent_metadata),
                "task": self._request_metadata(request),
                "task_history_summary": task_history_summary,
                "error": self._error_payload(error) if error is not None else None,
            },
        )
        if self._include("include_strands_messages") and strands_messages is not None:
            self._write_json(
                dump_dir / "strands_messages.json",
                self._filter_strands_messages(strands_messages),
            )
        if self._include("include_result") and result is not None:
            self._write_json(dump_dir / "result.json", self._jsonable(result))
        if self._include("include_reasoning_artifacts") and reasoning_artifacts is not None:
            self._write_json(dump_dir / "reasoning_artifacts.json", self._jsonable(reasoning_artifacts))
        if parsed_content is not None:
            self._write_json(dump_dir / "parsed_content.json", self._jsonable(parsed_content))
        if model_calls:
            self._write_model_calls(dump_dir, model_calls)
        if error is not None:
            self._write_json(dump_dir / "error.json", self._error_payload(error))
        self.logger.warning("Context debug dump written: %s", dump_dir)
        return dump_dir

    def dump_strands_prompt_input(
        self,
        *,
        request: ContextAssemblyRequest,
        assembly: AssembledContext,
        agent_metadata: dict[str, Any],
        strands_prompt_input: Any,
        attempt: int,
        dump_dir: Path | None = None,
    ) -> Path | None:
        if not self.enabled:
            return dump_dir
        if dump_dir is None:
            if not self.should_dump_pre(assembly):
                return None
            dump_dir = self._create_dump_dir(request=request, phase="pre")
        if not (dump_dir / "pre_metadata.json").exists():
            self._write_pre_files(dump_dir=dump_dir, request=request, assembly=assembly, agent_metadata=agent_metadata)
        payload = {
            "attempt": attempt,
            "created_at": datetime.now(timezone.utc).isoformat(),
            "summary": self._summarize_strands_prompt_input(strands_prompt_input),
            "input": self._jsonable_model_input(strands_prompt_input),
        }
        self._write_json(dump_dir / f"strands_prompt_input_attempt_{attempt}.json", payload)
        self._write_json(dump_dir / "strands_prompt_input_latest.json", payload)
        return dump_dir

    def build_model_call_record(
        self,
        *,
        context: Any,
        last_event: Any,
        error: BaseException | None,
        elapsed_seconds: float,
        call_index: int,
        invocation_metadata: dict[str, Any],
    ) -> dict[str, Any]:
        messages = getattr(context, "messages", None)
        system_prompt = getattr(context, "system_prompt", None)
        tool_specs = list(getattr(context, "tool_specs", None) or [])
        tool_choice = getattr(context, "tool_choice", None)
        stop_reason, usage, metrics = self._model_stop_payload(last_event)
        record: dict[str, Any] = {
            "call_index": call_index,
            "captured_at": datetime.now(timezone.utc).isoformat(),
            "elapsed_seconds": round(max(0.0, elapsed_seconds), 6),
            "invocation": self._jsonable(invocation_metadata),
            "input_accounting": {
                "projected_input_tokens": getattr(context, "projected_input_tokens", None),
                "messages": self._model_value_accounting(messages),
                "system_prompt": self._model_value_accounting(system_prompt),
                "tool_specs": self._tool_specs_accounting(tool_specs),
                "tool_choice": self._model_value_accounting(tool_choice),
            },
            "result": {
                "stop_reason": stop_reason,
                "usage": self._jsonable(usage),
                "metrics": self._jsonable(metrics),
                "error": self._jsonable(self._error_payload(error)),
            },
        }
        captured_inputs: dict[str, Any] = {}
        if self._include("include_model_call_inputs"):
            captured_inputs.update(
                {
                    "messages": self._jsonable(messages),
                    "system_prompt": self._jsonable(system_prompt),
                    "tool_choice": self._jsonable(tool_choice),
                }
            )
        if self._include("include_tool_specs"):
            captured_inputs["tool_specs"] = self._jsonable_tool_specs(tool_specs)
        if captured_inputs:
            record["captured_inputs"] = captured_inputs
        return record

    def _write_model_calls(self, dump_dir: Path, records: list[dict[str, Any]]) -> None:
        summary_calls: list[dict[str, Any]] = []
        usage_totals: dict[str, float | int] = {}
        detail_dir = dump_dir / "model_calls"
        write_details = self._include("include_model_call_inputs") or self._include("include_tool_specs")
        if write_details:
            detail_dir.mkdir(parents=True, exist_ok=True)
        for record in records:
            compact_record = {key: value for key, value in record.items() if key != "captured_inputs"}
            if write_details:
                detail_name = f"call_{int(record.get('call_index') or 0):03d}.json"
                compact_record["input_artifact"] = f"model_calls/{detail_name}"
                self._write_json(detail_dir / detail_name, record, already_redacted=True)
            summary_calls.append(compact_record)
            usage = ((record.get("result") or {}).get("usage") or {})
            if isinstance(usage, dict):
                for key, value in usage.items():
                    if isinstance(value, (int, float)) and not isinstance(value, bool):
                        usage_totals[str(key)] = usage_totals.get(str(key), 0) + value
        self._write_json(
            dump_dir / "model_calls.json",
            {
                "schema_version": 1,
                "measurement_scope": "Each Strands InvokeModelStage provider call, including inner-loop and forced structured-output calls.",
                "call_count": len(records),
                "usage_totals": usage_totals,
                "calls": summary_calls,
            },
            already_redacted=True,
        )

    def _model_value_accounting(self, value: Any) -> dict[str, Any]:
        sanitized = self._jsonable(value)
        encoded = json.dumps(sanitized, sort_keys=True, separators=(",", ":"), ensure_ascii=False, default=str)
        summary: dict[str, Any] = {
            "compact_json_chars": len(encoded),
            "text_chars": self._nested_text_chars(value),
            "sanitized_sha256": hashlib.sha256(encoded.encode("utf-8")).hexdigest(),
        }
        if isinstance(value, (list, tuple, dict)):
            summary["item_count"] = len(value)
        return summary

    def _tool_specs_accounting(self, tool_specs: list[Any]) -> dict[str, Any]:
        tools: list[dict[str, Any]] = []
        total_chars = 0
        for raw_spec in tool_specs:
            spec = raw_spec if isinstance(raw_spec, dict) else self._jsonable_tool_specs(raw_spec)
            spec = spec if isinstance(spec, dict) else {"value": spec}
            description = str(spec.get("description") or "")
            input_schema = spec.get("inputSchema") or spec.get("input_schema") or {}
            schema_json = json.dumps(
                self._jsonable_tool_specs(input_schema),
                sort_keys=True,
                separators=(",", ":"),
                ensure_ascii=False,
                default=str,
            )
            spec_json = json.dumps(
                self._jsonable_tool_specs(spec),
                sort_keys=True,
                separators=(",", ":"),
                ensure_ascii=False,
                default=str,
            )
            total_chars += len(spec_json)
            tools.append(
                {
                    "name": spec.get("name"),
                    "description_chars": len(description),
                    "input_schema_chars": len(schema_json),
                    "compact_json_chars": len(spec_json),
                }
            )
        encoded = json.dumps(
            self._jsonable_tool_specs(tool_specs),
            sort_keys=True,
            separators=(",", ":"),
            default=str,
        )
        return {
            "tool_count": len(tool_specs),
            "compact_json_chars": total_chars,
            "sanitized_sha256": hashlib.sha256(encoded.encode("utf-8")).hexdigest(),
            "tools": tools,
        }

    def _jsonable_tool_specs(self, value: Any) -> Any:
        """Serialize static schemas without mistaking parameter names for secret values."""
        if isinstance(value, dict):
            return {str(key): self._jsonable_tool_specs(child) for key, child in value.items()}
        if isinstance(value, (list, tuple)):
            return [self._jsonable_tool_specs(child) for child in value]
        if isinstance(value, (bytes, bytearray)):
            data = bytes(value)
            return {"omitted_binary_bytes": len(data), "sha256": hashlib.sha256(data).hexdigest()}
        if isinstance(value, str):
            return self._redact_string(value)
        if isinstance(value, (int, float, bool)) or value is None:
            return value
        if hasattr(value, "model_dump"):
            try:
                return self._jsonable_tool_specs(value.model_dump(mode="json"))
            except Exception:
                pass
        return self._redact_string(repr(value))

    @staticmethod
    def _nested_text_chars(value: Any) -> int:
        if isinstance(value, str):
            return len(value)
        if isinstance(value, dict):
            return sum(ContextDebugDumpWriter._nested_text_chars(child) for child in value.values())
        if isinstance(value, (list, tuple)):
            return sum(ContextDebugDumpWriter._nested_text_chars(child) for child in value)
        return 0

    @staticmethod
    def _model_stop_payload(last_event: Any) -> tuple[Any, Any, Any]:
        try:
            stop = last_event.get("stop") if isinstance(last_event, dict) else last_event["stop"]
            if isinstance(stop, (list, tuple)) and len(stop) == 4:
                stop_reason, _message, usage, metrics = stop
                return stop_reason, usage, metrics
        except Exception:
            pass
        return None, None, None

    def _write_pre_files(
        self,
        *,
        dump_dir: Path,
        request: ContextAssemblyRequest,
        assembly: AssembledContext,
        agent_metadata: dict[str, Any],
    ) -> None:
        self._write_json(
            dump_dir / "pre_metadata.json",
            {
                "phase": "pre",
                "created_at": datetime.now(timezone.utc).isoformat(),
                "agent": self._agent_metadata_summary(agent_metadata),
                "task": self._request_metadata(request),
                "context": {
                    "measurement_scope": "Outer context-engine prompt plus the base system prompt.",
                    "excluded_from_estimate": [
                        "AgentSkills XML injected after outer assembly",
                        "Strands message history and model-cycle additions",
                        "tool specifications",
                        "terminal structured-output schema/tool",
                        "provider-specific serialization and cache metadata",
                    ],
                    "char_count": assembly.char_count,
                    "estimated_tokens": assembly.estimated_tokens,
                    "max_context_tokens": assembly.max_context_tokens,
                    "soft_limit_tokens": assembly.soft_limit_tokens,
                    "hard_limit_tokens": assembly.hard_limit_tokens,
                    "reserved_output_tokens": assembly.reserved_output_tokens,
                    "context_fill_ratio": assembly.context_fill_ratio,
                    "warnings": assembly.warnings,
                },
            },
        )
        if self._include("include_full_prompt"):
            self._write_text(dump_dir / "prompt.md", assembly.prompt)
        if self._include("include_system_prompt"):
            self._write_text(dump_dir / "system_prompt.md", agent_metadata.get("system_prompt") or "")
        if self._include("include_context_sections"):
            self._write_json(
                dump_dir / "sections.json",
                [
                    {
                        "name": section.name,
                        "priority": section.priority,
                        "pinned": section.pinned,
                        "source": section.source,
                        "char_count": section.char_count,
                        "estimated_tokens": section.estimated_tokens,
                        "plugin_id": section.plugin_id,
                        "scope_kind": section.scope_kind,
                        "degradable": section.degradable,
                        "warning": section.warning,
                        "text": section.text,
                    }
                    for section in assembly.sections
                ],
            )
        if self._include("include_source_lengths"):
            self._write_json(dump_dir / "source_lengths.json", assembly.source_lengths)
        if self._include("include_incoming_envelope_json"):
            try:
                incoming_payload = json.loads(request.incoming_message_json or "{}")
            except (TypeError, ValueError):
                self._write_text(dump_dir / "incoming_envelope.json", request.incoming_message_json or "")
            else:
                self._write_json(dump_dir / "incoming_envelope.json", incoming_payload)

    def _create_dump_dir(self, *, request: ContextAssemblyRequest, phase: str) -> Path:
        self._cleanup_old_dumps()
        timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S.%fZ")
        task_id = self._safe_slug(str(getattr(request.incoming_content, "task_id", None) or "no_task"))
        correlation_id = self._safe_slug(str(getattr(request.incoming_meta, "msg_correlation_id", None) or "no_corr"))
        dump_dir = self.output_dir / task_id / f"{timestamp}_{phase}_{correlation_id}"
        dump_dir.mkdir(parents=True, exist_ok=True)
        return dump_dir

    def _cleanup_old_dumps(self) -> None:
        if self._cleanup_done:
            return
        self._cleanup_done = True
        try:
            retention_days = int(self.config.get("retention_days") or 0)
        except (TypeError, ValueError):
            retention_days = 0
        if retention_days <= 0 or not self.output_dir.exists():
            return
        cutoff = time.time() - (retention_days * 86400)
        try:
            for task_dir in self.output_dir.iterdir():
                if not task_dir.is_dir():
                    continue
                for dump_dir in task_dir.iterdir():
                    try:
                        if dump_dir.stat().st_mtime < cutoff:
                            if dump_dir.is_dir():
                                shutil.rmtree(dump_dir)
                            else:
                                dump_dir.unlink()
                    except FileNotFoundError:
                        continue
                try:
                    next(task_dir.iterdir())
                except StopIteration:
                    task_dir.rmdir()
        except Exception as exc:
            self.logger.warning("Failed to clean old context debug dumps in %s: %s", self.output_dir, exc)

    def _request_metadata(self, request: ContextAssemblyRequest) -> dict[str, Any]:
        return {
            "content_type": type(request.incoming_content).__name__ if request.incoming_content is not None else None,
            "task_id": getattr(request.incoming_content, "task_id", None),
            "parent_task_id": getattr(request.incoming_content, "parent_task_id", None),
            "task_type": str(getattr(request.incoming_content, "task_type", "")) or None,
            "from_agent_id": getattr(request.incoming_header, "from_agent_id", None),
            "from_agent_name": getattr(request.incoming_meta, "from_agent_name", None),
            "correlation_id": getattr(request.incoming_meta, "msg_correlation_id", None),
            "conversation_id": getattr(request.incoming_meta, "conversation_id", None),
            "followup_to_task_id": getattr(request.incoming_meta, "followup_to_task_id", None),
            "request_timezone": getattr(request.incoming_meta, "request_timezone", None),
            "isolated_recovery_task": request.isolated_recovery_task,
            "plan_step_task": request.plan_step_task,
        }

    def _agent_metadata_summary(self, agent_metadata: dict[str, Any]) -> dict[str, Any]:
        payload = dict(agent_metadata or {})
        system_prompt = str(payload.pop("system_prompt", "") or "")
        sanitized_prompt = self._redact_string(system_prompt)
        payload.update(
            {
                "system_prompt_chars": len(system_prompt),
                "system_prompt_sanitized_sha256": hashlib.sha256(
                    sanitized_prompt.encode("utf-8")
                ).hexdigest(),
                "system_prompt_artifact": (
                    "system_prompt.md" if self._include("include_system_prompt") else None
                ),
            }
        )
        return payload

    def _filter_strands_messages(self, messages: list[dict[str, Any]]) -> Any:
        if self._include("include_tool_results"):
            return self._jsonable(messages)
        filtered = []
        for message in messages:
            filtered_message = self._jsonable(message)
            self._strip_tool_results(filtered_message)
            filtered.append(filtered_message)
        return filtered

    def _strip_tool_results(self, value: Any) -> None:
        if isinstance(value, dict):
            if "toolResult" in value:
                value["toolResult"] = {"omitted": "tool results excluded by debug.context_dump.include_tool_results=false"}
            for child in value.values():
                self._strip_tool_results(child)
        elif isinstance(value, list):
            for child in value:
                self._strip_tool_results(child)

    def _summarize_strands_prompt_input(self, value: Any) -> dict[str, Any]:
        if isinstance(value, str):
            return {"input_type": "text", "text_chars": len(value), "image_block_count": 0}
        if not isinstance(value, list):
            return {"input_type": type(value).__name__, "image_block_count": 0}
        block_summaries: list[dict[str, Any]] = []
        image_count = 0
        text_chars = 0
        for index, block in enumerate(value):
            if not isinstance(block, dict):
                block_summaries.append({"index": index, "type": type(block).__name__})
                continue
            if "text" in block:
                text = str(block.get("text") or "")
                text_chars += len(text)
                block_summaries.append({"index": index, "type": "text", "text_chars": len(text)})
                continue
            if "image" in block:
                image_count += 1
                image = block.get("image") if isinstance(block.get("image"), dict) else {}
                source = image.get("source") if isinstance(image, dict) and isinstance(image.get("source"), dict) else {}
                data = source.get("bytes") if isinstance(source, dict) else None
                block_summaries.append(
                    {
                        "index": index,
                        "type": "image",
                        "format": image.get("format") if isinstance(image, dict) else None,
                        "bytes": len(data) if isinstance(data, (bytes, bytearray)) else None,
                        "sha256": hashlib.sha256(bytes(data)).hexdigest() if isinstance(data, (bytes, bytearray)) else None,
                    }
                )
                continue
            block_summaries.append({"index": index, "type": "unknown", "keys": sorted(str(key) for key in block)})
        return {
            "input_type": "content_blocks",
            "block_count": len(value),
            "image_block_count": image_count,
            "text_chars": text_chars,
            "blocks": block_summaries,
        }

    def _jsonable_model_input(self, value: Any) -> Any:
        if isinstance(value, dict):
            redacted: dict[str, Any] = {}
            for key, child in value.items():
                if key == "text" and isinstance(child, str):
                    redacted[key] = {
                        "omitted_text_chars": len(child),
                        "preview": self._redact_string(child[:500]),
                    }
                elif key == "bytes" and isinstance(child, (bytes, bytearray)):
                    data = bytes(child)
                    redacted[key] = {
                        "omitted_binary_bytes": len(data),
                        "sha256": hashlib.sha256(data).hexdigest(),
                    }
                else:
                    redacted[str(key)] = self._jsonable_model_input(child)
            return self._redact(redacted)
        if isinstance(value, list):
            return self._redact([self._jsonable_model_input(child) for child in value])
        if isinstance(value, tuple):
            return self._redact([self._jsonable_model_input(child) for child in value])
        if isinstance(value, (bytes, bytearray)):
            data = bytes(value)
            return self._redact({"omitted_binary_bytes": len(data), "sha256": hashlib.sha256(data).hexdigest()})
        return self._jsonable(value)

    def _jsonable(self, value: Any) -> Any:
        if hasattr(value, "model_dump"):
            try:
                return self._redact(value.model_dump(mode="json"))
            except Exception:
                pass
        if isinstance(value, dict):
            return self._redact({str(key): self._jsonable(child) for key, child in value.items()})
        if isinstance(value, list):
            return self._redact([self._jsonable(child) for child in value])
        if isinstance(value, tuple):
            return self._redact([self._jsonable(child) for child in value])
        if isinstance(value, (bytes, bytearray)):
            data = bytes(value)
            return self._redact(
                {
                    "omitted_binary_bytes": len(data),
                    "sha256": hashlib.sha256(data).hexdigest(),
                }
            )
        if isinstance(value, (str, int, float, bool)) or value is None:
            return self._redact(value)
        if hasattr(value, "__dict__"):
            try:
                return self._redact({str(key): self._jsonable(child) for key, child in vars(value).items()})
            except Exception:
                pass
        return self._redact(repr(value))

    def _redact(self, value: Any) -> Any:
        if not self._include("redact_secrets"):
            return value
        if isinstance(value, dict):
            redacted = {}
            for key, child in value.items():
                if is_sensitive_context_debug_key(key):
                    redacted[key] = "[REDACTED]"
                else:
                    redacted[key] = self._redact(child)
            return redacted
        if isinstance(value, list):
            return [self._redact(child) for child in value]
        if isinstance(value, str):
            return self._redact_string(value)
        return value

    def _redact_string(self, text: str) -> str:
        def replace_assignment(match: re.Match[str]) -> str:
            key = match.group("key")
            if not is_sensitive_context_debug_key(key):
                return match.group(0)
            return f"{key}=[REDACTED]"

        redacted = _SENSITIVE_ASSIGNMENT_RE.sub(replace_assignment, text)
        for secret in self._secret_values:
            redacted = redacted.replace(secret, "[REDACTED]")
        return redacted

    def _collect_secret_values(self) -> list[str]:
        secrets = []
        for key, value in os.environ.items():
            if not value or len(value) < 6:
                continue
            parts = _normalized_key_parts(key)
            if parts and parts[-1] in {"file", "path", "dir", "directory", "name"}:
                continue
            if is_sensitive_context_debug_key(key):
                secrets.append(value)
        return sorted(set(secrets), key=len, reverse=True)

    def _write_json(self, path: Path, payload: Any, *, already_redacted: bool = False) -> None:
        try:
            path.write_text(
                json.dumps(
                    payload if already_redacted else self._redact(payload),
                    indent=2,
                    sort_keys=True,
                    ensure_ascii=False,
                    default=str,
                )
                + "\n",
                encoding="utf-8",
            )
        except Exception as exc:
            self.logger.warning("Failed to write context debug JSON artifact %s: %s", path, exc)

    def _write_text(self, path: Path, text: str) -> None:
        try:
            path.write_text(self._redact_string(str(text or "")) + "\n", encoding="utf-8")
        except Exception as exc:
            self.logger.warning("Failed to write context debug text artifact %s: %s", path, exc)

    def _error_payload(self, error: BaseException | None) -> dict[str, Any] | None:
        if error is None:
            return None
        return {
            "type": type(error).__name__,
            "message": str(error),
        }

    def _include(self, key: str) -> bool:
        return bool(self.config.get(key, DEFAULT_CONTEXT_DUMP_CONFIG.get(key)))

    def _mode(self) -> str:
        mode = str(self.config.get("mode") or "on_error").strip().lower()
        return mode if mode in {"always", "on_error", "on_budget_warning"} else "on_error"

    @staticmethod
    def _safe_slug(value: str) -> str:
        slug = re.sub(r"[^a-zA-Z0-9._-]+", "-", str(value or "").strip()).strip("-._")
        return (slug or "unknown")[:100]


class ContextDebugDumpPlugin:
    """Context-engine plugin that owns context debug dump lifecycle calls."""

    INVOCATION_CONTEXT_KEY = "universal_agents.context_debug"

    def __init__(
        self,
        *,
        config: dict[str, Any] | None,
        output_dir: str | Path,
        logger: Optional[logging.Logger] = None,
        agent_metadata_provider: Callable[[], dict[str, Any]],
        strands_messages_provider: Callable[[], list[dict[str, Any]]] | None = None,
        reasoning_artifacts_builder: Callable[..., dict[str, Any]] | None = None,
        task_history_summary_provider: Callable[[Any], str | None] | None = None,
    ) -> None:
        self.writer = ContextDebugDumpWriter(
            config=config,
            output_dir=output_dir,
            logger=logger,
        )
        self.agent_metadata_provider = agent_metadata_provider
        self.strands_messages_provider = strands_messages_provider
        self.reasoning_artifacts_builder = reasoning_artifacts_builder
        self.task_history_summary_provider = task_history_summary_provider
        self._dump_dirs: dict[tuple[str, str], Path | None] = {}
        self._model_calls: dict[tuple[str, str], list[dict[str, Any]]] = {}
        self.model_call_observer_status: dict[str, Any] = {
            "registered": False,
            "api": "strands._middleware.InvokeModelStage.Wrap",
            "reason": "not_registered",
        }

    @property
    def enabled(self) -> bool:
        return self.writer.enabled

    @property
    def model_call_capture_enabled(self) -> bool:
        return self.enabled and self.model_call_capture_configured

    @property
    def model_call_capture_configured(self) -> bool:
        return self.writer._include("capture_model_calls")

    def assemble(
        self,
        request: ContextAssemblyRequest,
        assembly: AssembledContext,
    ) -> AssembledContext | None:
        if not self.enabled:
            return None
        try:
            dump_dir = self.writer.dump_pre_invocation(
                request=request,
                assembly=assembly,
                agent_metadata=self.agent_metadata_provider(),
            )
            self._dump_dirs[self._key(request)] = dump_dir
        except Exception as exc:
            self.writer.logger.warning(
                "Failed to write pre-invocation context debug dump: %s",
                exc,
            )
        return None

    def post_invocation(
        self,
        request: ContextAssemblyRequest,
        assembly: AssembledContext,
        *,
        result: Any = None,
        parsed_content: Any = None,
        after_action_report: Any = None,
        error: BaseException | None = None,
    ) -> None:
        if not self.enabled:
            return
        key = self._key(request)
        dump_dir = self._dump_dirs.get(key)
        model_calls = self._model_calls.pop(key, [])
        try:
            strands_messages = self.strands_messages_provider() if self.strands_messages_provider else None
            reasoning_artifacts = (
                self.reasoning_artifacts_builder(
                    request=request,
                    assembly=assembly,
                    strands_messages=strands_messages,
                    after_action_report=after_action_report,
                )
                if self.reasoning_artifacts_builder
                else None
            )
            task_history_summary = (
                self.task_history_summary_provider(request.incoming_content)
                if self.task_history_summary_provider
                else None
            )
            self.writer.dump_post_turn(
                request=request,
                assembly=assembly,
                agent_metadata=self.agent_metadata_provider(),
                dump_dir=dump_dir,
                result=result,
                parsed_content=parsed_content,
                strands_messages=strands_messages,
                reasoning_artifacts=reasoning_artifacts,
                model_calls=model_calls,
                task_history_summary=task_history_summary,
                error=error,
            )
        except Exception as exc:
            self.writer.logger.warning(
                "Failed to write post-turn context debug dump: %s",
                exc,
            )
        finally:
            self._dump_dirs.pop(key, None)

    def dump_strands_prompt_input(
        self,
        request: ContextAssemblyRequest,
        assembly: AssembledContext,
        *,
        strands_prompt_input: Any,
        attempt: int,
    ) -> None:
        if not self.enabled:
            return
        try:
            key = self._key(request)
            dump_dir = self.writer.dump_strands_prompt_input(
                request=request,
                assembly=assembly,
                agent_metadata=self.agent_metadata_provider(),
                strands_prompt_input=strands_prompt_input,
                attempt=attempt,
                dump_dir=self._dump_dirs.get(key),
            )
            if dump_dir is not None:
                self._dump_dirs[key] = dump_dir
        except Exception as exc:
            self.writer.logger.warning(
                "Failed to write Strands prompt input debug dump: %s",
                exc,
            )

    def capture_model_call(
        self,
        *,
        context: Any,
        last_event: Any,
        error: BaseException | None,
        elapsed_seconds: float,
    ) -> None:
        if not self.model_call_capture_enabled:
            return
        try:
            invocation_state = getattr(context, "invocation_state", None) or {}
            invocation_metadata = invocation_state.get(self.INVOCATION_CONTEXT_KEY) or {}
            key = (
                str(invocation_metadata.get("task_id") or "no_task"),
                str(invocation_metadata.get("correlation_id") or "no_corr"),
            )
            records = self._model_calls.setdefault(key, [])
            records.append(
                self.writer.build_model_call_record(
                    context=context,
                    last_event=last_event,
                    error=error,
                    elapsed_seconds=elapsed_seconds,
                    call_index=len(records) + 1,
                    invocation_metadata=dict(invocation_metadata),
                )
            )
        except Exception as exc:
            self.writer.logger.warning("Failed to capture Strands model-call diagnostics: %s", exc)

    @staticmethod
    def _key(request: ContextAssemblyRequest) -> tuple[str, str]:
        return (
            str(getattr(request.incoming_content, "task_id", None) or "no_task"),
            str(getattr(request.incoming_meta, "msg_correlation_id", None) or "no_corr"),
        )


def register_context_debug_model_observer(
    agent: Any,
    plugin: ContextDebugDumpPlugin,
    *,
    logger: logging.Logger | None = None,
) -> bool:
    """Register opt-in model-call observation against Strands' private middleware API."""
    logger = logger or logging.getLogger(__name__)
    if not plugin.model_call_capture_configured:
        plugin.model_call_observer_status = {
            "registered": False,
            "api": "strands._middleware.InvokeModelStage.Wrap",
            "reason": "capture_model_calls_disabled",
        }
        return False
    if getattr(agent, "_universal_agents_context_debug_observer_registered", False):
        return True
    try:
        from strands._middleware.stages import InvokeModelStage

        registry = getattr(agent, "_middleware_registry")
        add_middleware = getattr(registry, "add_middleware")
    except (AttributeError, ImportError) as exc:
        plugin.model_call_observer_status = {
            "registered": False,
            "api": "strands._middleware.InvokeModelStage.Wrap",
            "reason": f"unavailable: {type(exc).__name__}: {exc}",
        }
        logger.warning(
            "Exact context-debug model-call instrumentation is unavailable; continuing without it: %s",
            exc,
        )
        return False

    async def observe_model_call(context: Any, next_fn: Callable[[Any], AsyncGenerator[Any, None]]):
        if not plugin.model_call_capture_enabled:
            async for event in next_fn(context):
                yield event
            return

        started_at = time.monotonic()
        last_event: Any = None
        try:
            async for event in next_fn(context):
                last_event = event
                yield event
        except BaseException as exc:
            plugin.capture_model_call(
                context=context,
                last_event=last_event,
                error=exc,
                elapsed_seconds=time.monotonic() - started_at,
            )
            raise
        else:
            plugin.capture_model_call(
                context=context,
                last_event=last_event,
                error=None,
                elapsed_seconds=time.monotonic() - started_at,
            )

    try:
        add_middleware(InvokeModelStage.Wrap, observe_model_call)
        setattr(agent, "_universal_agents_context_debug_observer_registered", True)
    except Exception as exc:
        plugin.model_call_observer_status = {
            "registered": False,
            "api": "strands._middleware.InvokeModelStage.Wrap",
            "reason": f"registration_failed: {type(exc).__name__}: {exc}",
        }
        logger.warning(
            "Exact context-debug model-call instrumentation registration failed; continuing without it: %s",
            exc,
        )
        return False

    plugin.model_call_observer_status = {
        "registered": True,
        "api": "strands._middleware.InvokeModelStage.Wrap",
        "reason": None,
        "private_api": True,
    }
    return True
