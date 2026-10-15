from __future__ import annotations

import argparse
import asyncio
import contextlib
import json
import logging
import os
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Optional

import yaml

try:
    from dotenv import load_dotenv
except ImportError:
    load_dotenv = None

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from runtime_settings.values import deep_merge_dicts
from config_loader import load_config, resolve_agent_type_settings
from custom_agents.conversation_compaction import (
    ConversationCompactionConfig,
    build_conversation_compactor,
)
from custom_agents.conversation_store import ConversationStore
from custom_agents.context_engine.thread_renderer import BudgetedThreadContextRenderer
from custom_messages.contracts.control_v1 import AgentStatus, ControlInstruction, Instruction
from custom_messages.contracts.delivery_v1 import DeliveryReceipt
from custom_messages.contracts.envelope_v1 import MessageEnvelope
from custom_messages.contracts.routing_v1 import GatewayContext, MessageHeader, MessageMeta
from custom_messages.contracts.schedule_v1 import ScheduledMessage
from custom_messages.contracts.task_v1 import Task, TaskQuery
from custom_messages.enums import ControlType, MessageMetaType, RequesterType, RoutingType
import live_nats_client
from live_db_fixtures import provision_db, suite_default_db_path
from live_storage_utils import (
    apply_storage_env,
    query_storage,
    resolve_live_storage_settings,
    reset_postgres_storage,
    storage_identifier,
    storage_bind,
    coerce_storage_settings,
)
from storage.config import StorageSettings
from storage.runtime_state_repository import create_runtime_state_repository
from id_generation import new_id, new_id_suffix


logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger(__name__)


ADVANCED_TESTS = {
    8: "Correlation And Clarification Routing",
    10: "Subtask Lifecycle",
    11: "Scheduled Message Flow",
    12: "Multiple Specialist Queue Routing",
    13: "Direct Agent Targeting",
    14: "Specialist Pause/Resume Deferral",
    15: "No Response Requested",
    16: "Tool Failure Containment",
    17: "Operational Discovery Freshness",
    18: "Large Context Isolation",
    19: "Conversation Follow-Up Threading",
    20: "Conversation Artifact Offloading",
    21: "Conversation Compaction Replay",
    22: "LLM Conversation Compaction Replay",
    23: "All Compactors Side-By-Side Replay",
    24: "Worker Crash Takeover Recovery",
    25: "Waiting Queue Advancement During Worker Outage",
    26: "Worker Crash Watchdog Failure Recovery",
    27: "TaskQuery Timeout Recovery",
    28: "Manager Replacement Takeover Recovery",
    29: "Single Claim On Same-Type Worker Takeover",
    30: "Team Mismatch Discards Persisted Work",
    31: "Manager Instantiation With Operator-Configured Noop MCP",
    32: "Manager Team Template Instantiation",
    45: "Pinned Usermem Recall Across Conversations",
    46: "Pinned Usermem Survives Manager Restart",
    47: "Pinned Usermem Latest Preference Recall",
    48: "Pinned Memory Explicit Tool Write And Inspect",
    49: "LLM Turn Summaries And Context Engine Live Verification",
    50: "Context Epoch Boundary Live Verification",
    51: "Recurring Schedule Conversation Continuation",
    52: "Shell Execution Boundary",
}


def get_repo_python_executable() -> str:
    """Prefer the repo-local virtualenv interpreter for managed live processes."""
    repo_python = live_nats_client.PROJECT_ROOT / ".venv" / "bin" / "python"
    if repo_python.exists():
        return str(repo_python)
    return sys.executable


def prepare_result_dir(test_number: int) -> Path:
    result_dir = live_nats_client.TEST_RESULTS_ROOT / f"test_{test_number}"
    result_dir.mkdir(parents=True, exist_ok=True)
    return result_dir


def persist_summary(
    result_dir: Path,
    test_number: int,
    title: str,
    summary: str,
    details: dict,
) -> None:
    payload = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "test_number": test_number,
        "title": title,
        "summary": summary,
        "details": details,
    }
    live_nats_client.write_json_file(result_dir / "final_answer.json", payload)
    (result_dir / "final_answer.txt").write_text(summary, encoding="utf-8")
    live_nats_client.write_json_file(result_dir / "scenario_report.json", details)


def _query_db(db_path: Path | StorageSettings | str, query: str, params: tuple = ()) -> list:
    return query_storage(db_path, query, params)


def get_agent_rows(db_path: Path | StorageSettings | str, team_id: str, agent_type: str) -> list[dict]:
    rows = _query_db(
        db_path,
        f"""
        SELECT agent_id, agent_name, named_agent_type, team_id, is_busy, is_paused, is_exiting, last_seen_at
        FROM agent_states
        WHERE team_id = {storage_bind(db_path)} AND named_agent_type = {storage_bind(db_path)}
        ORDER BY last_seen_at DESC
        """,
        (team_id, agent_type),
    )
    return [
        {
            "agent_id": row[0],
            "agent_name": row[1],
            "named_agent_type": row[2],
            "team_id": row[3],
            "is_busy": bool(row[4]),
            "is_paused": bool(row[5]),
            "is_exiting": bool(row[6]),
            "last_seen_at": row[7],
        }
        for row in rows
    ]


def get_archived_task_row(
    db_path: Path | StorageSettings | str,
    task_id: str,
    owner_agent_id: Optional[str] = None,
) -> Optional[dict]:
    if owner_agent_id:
        rows = _query_db(
            db_path,
            f"""
            SELECT task_data, status, timestamp, owner_agent_id, owner_agent_name, team_id
            FROM completed_tasks
            WHERE task_id = {storage_bind(db_path)} AND owner_agent_id = {storage_bind(db_path)}
            ORDER BY timestamp DESC
            LIMIT 1
            """,
            (task_id, owner_agent_id),
        )
    else:
        rows = _query_db(
            db_path,
            f"""
            SELECT task_data, status, timestamp, owner_agent_id, owner_agent_name, team_id
            FROM completed_tasks
            WHERE task_id = {storage_bind(db_path)}
            ORDER BY timestamp DESC
            LIMIT 1
            """,
            (task_id,),
        )
    if not rows:
        return None
    row = rows[0]
    task_data = row[0]
    status = row[1]
    timestamp = row[2]
    row_owner_agent_id = row[3]
    row_owner_agent_name = row[4]
    row_team_id = row[5]
    return {
        "task_data": json.loads(task_data) if isinstance(task_data, str) else task_data,
        "status": status,
        "timestamp": timestamp,
        "owner_agent_id": row_owner_agent_id,
        "owner_agent_name": row_owner_agent_name,
        "team_id": row_team_id,
    }


def get_archived_task_rows(
    db_path: Path | StorageSettings | str,
    *,
    owner_agent_id: Optional[str] = None,
    team_id: Optional[str] = None,
) -> list[dict]:
    query = """
        SELECT task_id, task_data, status, timestamp, owner_agent_id, owner_agent_name, team_id
        FROM completed_tasks
    """
    params = []
    where = []
    if owner_agent_id:
        where.append(f"owner_agent_id = {storage_bind(db_path)}")
        params.append(owner_agent_id)
    if team_id:
        where.append(f"team_id = {storage_bind(db_path)}")
        params.append(team_id)
    if where:
        query += " WHERE " + " AND ".join(where)
    query += " ORDER BY timestamp DESC"
    rows = _query_db(db_path, query, tuple(params))
    return [
        {
            "task_id": row[0],
            "task_data": json.loads(row[1]) if isinstance(row[1], str) else row[1],
            "status": row[2],
            "timestamp": row[3],
            "owner_agent_id": row[4],
            "owner_agent_name": row[5],
            "team_id": row[6],
        }
        for row in rows
    ]


def get_active_runtime_rows(
    db_path: Path | StorageSettings | str,
    *,
    team_id: Optional[str] = None,
    owner_named_agent_type: Optional[str] = None,
    owner_agent_id: Optional[str] = None,
) -> list[dict]:
    query = """
        SELECT owner_agent_id, owner_agent_name, team_id, owner_named_agent_type, runtime_state_json, updated_at
        FROM active_task_runtime
    """
    params = []
    where = []
    if team_id:
        where.append(f"team_id = {storage_bind(db_path)}")
        params.append(team_id)
    if owner_named_agent_type:
        where.append(f"owner_named_agent_type = {storage_bind(db_path)}")
        params.append(owner_named_agent_type)
    if owner_agent_id:
        where.append(f"owner_agent_id = {storage_bind(db_path)}")
        params.append(owner_agent_id)
    if where:
        query += " WHERE " + " AND ".join(where)
    query += " ORDER BY updated_at DESC"
    repository = create_runtime_state_repository(coerce_storage_settings(db_path))
    rows = repository._load_task_runtime_rows(query, tuple(params))
    return [
        {
            "owner_agent_id": row.owner_agent_id,
            "owner_agent_name": row.owner_agent_name,
            "team_id": row.team_id,
            "owner_named_agent_type": row.owner_named_agent_type,
            "runtime_state": json.loads(row.runtime_state_json),
            "updated_at": row.updated_at,
        }
        for row in rows
    ]


def get_schedule_row(db_path: Path | StorageSettings | str, schedule_id: str) -> Optional[dict]:
    rows = _query_db(
        db_path,
        f"""
        SELECT schedule_id, next_fire_at, enabled, remaining_count
        FROM scheduled_messages
        WHERE schedule_id = {storage_bind(db_path)}
        """,
        (schedule_id,),
    )
    if not rows:
        return None
    row = rows[0]
    return {
        "schedule_id": row[0],
        "next_fire_at": row[1],
        "enabled": bool(row[2]),
        "remaining_count": row[3],
    }


def get_conversation_thread_row(db_path: Path | StorageSettings | str, conversation_id: str) -> Optional[dict]:
    rows = _query_db(
        db_path,
        f"""
        SELECT conversation_id, root_task_id, latest_task_id, status,
               synopsis_markdown, summarized_through_turn_index, last_token_estimate,
               compactor_type, compaction_state_json
        FROM conversation_threads
        WHERE conversation_id = {storage_bind(db_path)}
        """,
        (conversation_id,),
    )
    if not rows:
        return None
    row = rows[0]
    return {
        "conversation_id": row[0],
        "root_task_id": row[1],
        "latest_task_id": row[2],
        "status": row[3],
        "synopsis_markdown": row[4] or "",
        "summarized_through_turn_index": row[5] or 0,
        "last_token_estimate": row[6] or 0,
        "compactor_type": row[7],
        "compaction_state": json.loads(row[8] or "{}"),
    }


def get_conversation_turn_rows(db_path: Path | StorageSettings | str, conversation_id: str) -> list[dict]:
    rows = _query_db(
        db_path,
        f"""
        SELECT conversation_id, task_id, followup_to_task_id, turn_index, task_status,
               delivery_state, delivery_via, problem_text, final_answer_text, execution_history_text, turn_summary_text,
               user_request_summary, assistant_response_summary, context_summary_text,
               artifact_summary_json, summary_generator, summary_generated_at,
               tool_artifact_manifest_json
        FROM conversation_task_links
        WHERE conversation_id = {storage_bind(db_path)}
        ORDER BY turn_index ASC
        """,
        (conversation_id,),
    )
    result = []
    for row in rows:
        try:
            artifact_summary = json.loads(row[14] or "[]")
        except json.JSONDecodeError:
            artifact_summary = []
        try:
            manifest = json.loads(row[17] or "[]")
        except json.JSONDecodeError:
            manifest = []
        result.append(
            {
                "conversation_id": row[0],
                "task_id": row[1],
                "followup_to_task_id": row[2],
                "turn_index": row[3],
                "task_status": row[4],
                "delivery_state": row[5] or "",
                "delivery_via": row[6] or "",
                "problem_text": row[7] or "",
                "final_answer_text": row[8] or "",
                "execution_history_text": row[9] or "",
                "turn_summary_text": row[10] or "",
                "user_request_summary": row[11] or "",
                "assistant_response_summary": row[12] or "",
                "context_summary_text": row[13] or "",
                "artifact_summary": artifact_summary,
                "summary_generator": row[15] or "",
                "summary_generated_at": row[16],
                "tool_artifact_manifest": manifest,
            }
        )
    return result


def get_conversation_artifacts(
    db_path: Path | StorageSettings | str,
    *,
    conversation_id: Optional[str] = None,
    task_id: Optional[str] = None,
) -> list[dict]:
    query = """
        SELECT artifact_id, conversation_id, task_id, artifact_type, storage_path, summary_text, created_at
        FROM conversation_artifacts
    """
    params = []
    where = []
    if conversation_id:
        where.append(f"conversation_id = {storage_bind(db_path)}")
        params.append(conversation_id)
    if task_id:
        where.append(f"task_id = {storage_bind(db_path)}")
        params.append(task_id)
    if where:
        query += " WHERE " + " AND ".join(where)
    query += " ORDER BY created_at ASC"
    rows = _query_db(db_path, query, tuple(params))
    return [
        {
            "artifact_id": row[0],
            "conversation_id": row[1],
            "task_id": row[2],
            "artifact_type": row[3],
            "storage_path": row[4],
            "summary_text": row[5],
            "created_at": row[6],
        }
        for row in rows
    ]


def get_context_epoch_rows(db_path: Path | StorageSettings | str, conversation_id: str) -> list[dict]:
    rows = _query_db(
        db_path,
        f"""
        SELECT context_epoch_id, conversation_id, epoch_index, trigger, status,
               through_turn_index, preserved_suffix_start_turn, latest_task_id,
               summary_json, summary_markdown, rehydration_manifest_json,
               source_lengths_before_json, source_lengths_after_json,
               pre_compact_estimated_tokens, post_compact_estimated_tokens,
               compactor_type, created_by_agent_id, created_at, updated_at, error_text
        FROM conversation_context_epochs
        WHERE conversation_id = {storage_bind(db_path)}
        ORDER BY epoch_index ASC
        """,
        (conversation_id,),
    )
    epochs = []
    for row in rows:
        def loads(index: int, default):
            try:
                return json.loads(row[index] or json.dumps(default))
            except json.JSONDecodeError:
                return default

        epochs.append(
            {
                "context_epoch_id": row[0],
                "conversation_id": row[1],
                "epoch_index": row[2],
                "trigger": row[3],
                "status": row[4],
                "through_turn_index": row[5] or 0,
                "preserved_suffix_start_turn": row[6] or 1,
                "latest_task_id": row[7],
                "summary_json": loads(8, {}),
                "summary_markdown": row[9] or "",
                "rehydration_manifest": loads(10, {}),
                "source_lengths_before": loads(11, []),
                "source_lengths_after": loads(12, []),
                "pre_compact_estimated_tokens": row[13],
                "post_compact_estimated_tokens": row[14],
                "compactor_type": row[15],
                "created_by_agent_id": row[16],
                "created_at": row[17],
                "updated_at": row[18],
                "error_text": row[19],
            }
        )
    return epochs


def get_pinned_memory_rows(
    db_path: Path | StorageSettings | str,
    *,
    bank: str,
    canonical_user_id: str = "",
    team_id: str = "",
) -> list[dict]:
    rows = _query_db(
        db_path,
        f"""
        SELECT bank, team_id, canonical_user_id, memory_id, fact_uuid, fact_text,
               valid_from_utc, invalid_after_utc, tags_json, metadata_json,
               source_conversation_id, source_task_id, created_at_utc, updated_at_utc
        FROM pinned_memory_facts_active
        WHERE bank = {storage_bind(db_path)} AND team_id = {storage_bind(db_path)} AND canonical_user_id = {storage_bind(db_path)}
        ORDER BY memory_id ASC
        """,
        (bank, team_id, canonical_user_id),
    )
    return [
        {
            "bank": row[0],
            "team_id": row[1],
            "canonical_user_id": row[2],
            "memory_id": row[3],
            "fact_uuid": row[4],
            "fact_text": row[5],
            "valid_from_utc": row[6],
            "invalid_after_utc": row[7],
            "tags": json.loads(row[8] or "[]"),
            "metadata": json.loads(row[9] or "{}"),
            "source_conversation_id": row[10],
            "source_task_id": row[11],
            "created_at_utc": row[12],
            "updated_at_utc": row[13],
        }
        for row in rows
    ]


def _resolved_compaction_settings() -> dict:
    config = load_config()
    return resolve_agent_type_settings(config, "TeamArchitect")


def _build_compaction_config(settings: dict) -> ConversationCompactionConfig:
    context_window_tokens = settings.get("context_window_tokens") or 128000
    return ConversationCompactionConfig(
        context_window_tokens=int(context_window_tokens),
        trigger_ratio=float(settings.get("context_compaction_trigger_ratio", 0.75)),
        target_ratio=float(settings.get("context_compaction_target_ratio", 0.50)),
        min_recent_turns=int(settings.get("context_compaction_min_recent_turns", 3)),
        max_recent_turns=int(settings.get("context_compaction_max_recent_turns", 8)),
        trigger_turn_count=settings.get("context_compaction_trigger_turn_count"),
        trigger_message_count=settings.get("context_compaction_trigger_message_count"),
        compaction_model_provider=settings.get("compaction_model_provider"),
        compaction_model_id=settings.get("compaction_model_id"),
        default_model_provider=settings.get("model_provider", "openai"),
        default_model_id=settings.get("model_id", "gpt-4o"),
        temperature=float(settings.get("temperature", 0.0) or 0.0),
        litellm_api_base=settings.get("litellm_api_base"),
    )


def _prompt_stats(text: str) -> dict:
    stripped = text or ""
    return {
        "character_count": len(stripped),
        "line_count": len(stripped.splitlines()) if stripped else 0,
        "estimated_tokens": max(1, int(len(stripped) / 4)) if stripped else 0,
    }


def _read_jsonl(path: Path) -> list[dict]:
    if not path.exists():
        return []
    records = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        records.append(json.loads(line))
    return records


def _clone_conversation_into_store(
    *,
    target_db_path: Path,
    thread: dict,
    turns: list[dict],
) -> tuple[ConversationStore, str]:
    if target_db_path.exists():
        target_db_path.unlink()
    store = ConversationStore(db_path=str(target_db_path), artifacts_dir=Path(target_db_path).parent / "conversation_artifacts")
    clone_conversation_id = new_id("clone_conv")
    store.ensure_thread(
        conversation_id=clone_conversation_id,
        team_id=thread.get("team_id"),
        owner_agent_id=thread.get("owner_agent_id"),
        owner_agent_name=thread.get("owner_agent_name"),
        requester_name=thread.get("requester_name"),
        requester_id=thread.get("requester_id"),
        root_task_id=thread.get("root_task_id") or (turns[0]["task_id"] if turns else "task_id_root"),
        subject_preview=thread.get("subject_preview"),
    )
    task_id_map: dict[str, str] = {}
    for turn in turns:
        cloned_task_id = new_id("clone_task")
        task_id_map[turn["task_id"]] = cloned_task_id
        followup_to = task_id_map.get(turn.get("followup_to_task_id"))
        store.append_turn(
            conversation_id=clone_conversation_id,
            task_id=cloned_task_id,
            followup_to_task_id=followup_to,
            owner_agent_id=turn.get("owner_agent_id"),
            requester_name=turn.get("requester_name"),
            requester_id=turn.get("requester_id"),
            correlation_id=turn.get("correlation_id"),
            problem_text=turn.get("problem_text", ""),
            task_status=turn.get("task_status", "completed"),
        )
        store.update_turn_completion(
            task_id=cloned_task_id,
            task_status=turn.get("task_status", "completed"),
            final_answer_text=turn.get("final_answer_text", ""),
            clarification_summary_text=turn.get("clarification_summary_text", ""),
            turn_summary_text=turn.get("turn_summary_text", ""),
            tool_artifact_manifest=turn.get("tool_artifact_manifest") or [],
        )
    return store, clone_conversation_id


def _write_compaction_views(
    *,
    result_dir: Path,
    source_thread: dict,
    source_turns: list[dict],
    source_artifacts: list[dict],
    latest_request_text: str,
    compactor_types: list[str],
) -> dict:
    settings = _resolved_compaction_settings()
    config = _build_compaction_config(settings)
    report_dir = result_dir / "compaction_views"
    report_dir.mkdir(parents=True, exist_ok=True)

    live_nats_client.write_json_file(report_dir / "source_thread.json", source_thread)
    live_nats_client.write_json_file(report_dir / "source_turns.json", source_turns)
    live_nats_client.write_json_file(report_dir / "source_artifacts.json", source_artifacts)

    noop_compactor = build_conversation_compactor(
        "noop",
        config=config,
        logger=logger,
    )
    noop_db_path = report_dir / "source_raw.db"
    noop_store, noop_conversation_id = _clone_conversation_into_store(
        target_db_path=noop_db_path,
        thread=source_thread,
        turns=source_turns,
    )
    full_raw_context = noop_compactor.build_prompt_context(
        store=noop_store,
        conversation_id=noop_conversation_id,
        latest_request_text=latest_request_text,
    )
    (report_dir / "full_raw_context.txt").write_text(full_raw_context, encoding="utf-8")
    full_turn_render = "\n\n".join(
        [
            (
                f"### Turn {turn['turn_index']}\n"
                f"task_id={turn['task_id']}\n"
                f"status={turn['task_status']}\n\n"
                f"User Request:\n{turn['problem_text']}\n\n"
                f"Final Answer:\n{turn['final_answer_text']}\n\n"
                f"Execution History:\n{turn.get('execution_history_text', '')}\n\n"
                f"Turn Summary:\n{turn['turn_summary_text']}"
            )
            for turn in source_turns
        ]
    )
    (report_dir / "full_conversation_turns.txt").write_text(full_turn_render, encoding="utf-8")

    outputs = {
        "source_thread_path": str(report_dir / "source_thread.json"),
        "source_turns_path": str(report_dir / "source_turns.json"),
        "source_artifacts_path": str(report_dir / "source_artifacts.json"),
        "full_raw_context_path": str(report_dir / "full_raw_context.txt"),
        "full_conversation_turns_path": str(report_dir / "full_conversation_turns.txt"),
        "full_raw_context_stats": _prompt_stats(full_raw_context),
        "views": [],
    }

    for compactor_type in compactor_types:
        temp_db_path = report_dir / f"{compactor_type}.db"
        store, clone_conversation_id = _clone_conversation_into_store(
            target_db_path=temp_db_path,
            thread=source_thread,
            turns=source_turns,
        )
        compactor = build_conversation_compactor(
            compactor_type,
            config=config,
            logger=logger,
        )
        prompt_context = compactor.build_prompt_context(
            store=store,
            conversation_id=clone_conversation_id,
            latest_request_text=latest_request_text,
        )
        thread_after = store.get_thread(clone_conversation_id) or {}
        prompt_path = report_dir / f"{compactor_type}_prompt_context.txt"
        thread_path = report_dir / f"{compactor_type}_thread.json"
        prompt_path.write_text(prompt_context, encoding="utf-8")
        live_nats_client.write_json_file(thread_path, thread_after)
        prompt_stats = _prompt_stats(prompt_context)
        outputs["views"].append(
            {
                "compactor_type": compactor_type,
                "prompt_context_path": str(prompt_path),
                "thread_path": str(thread_path),
                "summarized_through_turn_index": thread_after.get("summarized_through_turn_index"),
                "last_token_estimate": thread_after.get("last_token_estimate"),
                "has_synopsis": bool(thread_after.get("synopsis_markdown")),
                "prompt_stats": prompt_stats,
            }
        )

    comparison_lines = [
        "## Full Raw Context",
        f"stats={json.dumps(outputs['full_raw_context_stats'], indent=2)}",
        full_raw_context,
        "## Full Conversation Turns",
        full_turn_render,
    ]
    for view in outputs["views"]:
        comparison_lines.append(
            (
                f"## {view['compactor_type']}\n"
                f"prompt_context_path={view['prompt_context_path']}\n"
                f"thread_path={view['thread_path']}\n"
                f"summarized_through_turn_index={view['summarized_through_turn_index']}\n"
                f"last_token_estimate={view['last_token_estimate']}\n"
                f"prompt_stats={json.dumps(view['prompt_stats'], indent=2)}\n"
            )
        )
        comparison_lines.append(Path(view["prompt_context_path"]).read_text(encoding="utf-8"))
    (report_dir / "compaction_comparison.txt").write_text("\n\n".join(comparison_lines), encoding="utf-8")
    outputs["comparison_path"] = str(report_dir / "compaction_comparison.txt")
    live_nats_client.write_json_file(report_dir / "compaction_manifest.json", outputs)
    outputs["manifest_path"] = str(report_dir / "compaction_manifest.json")
    return outputs


def _write_live_summary_views(
    *,
    result_dir: Path,
    thread: dict,
    turns: list[dict],
    lifecycle_events: list[dict],
) -> dict:
    report_dir = result_dir / "llm_turn_summary_live"
    report_dir.mkdir(parents=True, exist_ok=True)

    full_history_path = report_dir / "full_turn_history.json"
    full_history_text_path = report_dir / "full_turn_history.txt"
    generated_summary_path = report_dir / "generated_conversation_summary.json"
    generated_summary_text_path = report_dir / "generated_conversation_summary.md"
    lifecycle_path = report_dir / "context_lifecycle_events.json"

    live_nats_client.write_json_file(full_history_path, {"thread": thread, "turns": turns})
    full_turn_lines = []
    for turn in turns:
        full_turn_lines.append(
            "\n".join(
                [
                    f"### Turn {turn['turn_index']} task_id={turn['task_id']}",
                    f"followup_to_task_id={turn.get('followup_to_task_id')}",
                    f"status={turn.get('task_status')}",
                    f"summary_generator={turn.get('summary_generator')}",
                    f"summary_generated_at={turn.get('summary_generated_at')}",
                    "",
                    "User Request:",
                    turn.get("problem_text", ""),
                    "",
                    "Final Answer:",
                    turn.get("final_answer_text", ""),
                    "",
                    "Execution History:",
                    turn.get("execution_history_text", ""),
                    "",
                    "Legacy Turn Summary:",
                    turn.get("turn_summary_text", ""),
                    "",
                    "Generated User Summary:",
                    turn.get("user_request_summary", ""),
                    "",
                    "Generated Assistant Summary:",
                    turn.get("assistant_response_summary", ""),
                    "",
                    "Generated Context Summary:",
                    turn.get("context_summary_text", ""),
                ]
            )
        )
    full_history_text_path.write_text("\n\n".join(full_turn_lines), encoding="utf-8")

    generated = {
        "conversation_id": thread.get("conversation_id"),
        "thread": thread,
        "turn_summaries": [
            {
                "turn_index": turn.get("turn_index"),
                "task_id": turn.get("task_id"),
                "followup_to_task_id": turn.get("followup_to_task_id"),
                "summary_generator": turn.get("summary_generator"),
                "summary_generated_at": turn.get("summary_generated_at"),
                "user_request_summary": turn.get("user_request_summary"),
                "assistant_response_summary": turn.get("assistant_response_summary"),
                "context_summary_text": turn.get("context_summary_text"),
                "artifact_summary": turn.get("artifact_summary") or [],
            }
            for turn in turns
        ],
        "lifecycle_event_counts": {
            event_name: sum(1 for event in lifecycle_events if event.get("event") == event_name)
            for event_name in ["ingest", "assemble", "compact", "after_turn"]
        },
    }
    live_nats_client.write_json_file(generated_summary_path, generated)
    summary_lines = [
        f"# Generated Conversation Summary: {thread.get('conversation_id')}",
        "",
    ]
    for summary in generated["turn_summaries"]:
        summary_lines.extend(
            [
                f"## Turn {summary['turn_index']} ({summary['summary_generator']})",
                f"- task_id: `{summary['task_id']}`",
                f"- followup_to_task_id: `{summary['followup_to_task_id']}`",
                f"- generated_at: `{summary['summary_generated_at']}`",
                "",
                "### User Request Summary",
                summary.get("user_request_summary") or "",
                "",
                "### Assistant Response Summary",
                summary.get("assistant_response_summary") or "",
                "",
                "### Context Summary",
                summary.get("context_summary_text") or "",
                "",
            ]
        )
    generated_summary_text_path.write_text("\n".join(summary_lines), encoding="utf-8")
    live_nats_client.write_json_file(lifecycle_path, lifecycle_events)

    return {
        "report_dir": str(report_dir),
        "full_turn_history_json": str(full_history_path),
        "full_turn_history_text": str(full_history_text_path),
        "generated_conversation_summary_json": str(generated_summary_path),
        "generated_conversation_summary_markdown": str(generated_summary_text_path),
        "context_lifecycle_events_json": str(lifecycle_path),
        "lifecycle_event_counts": generated["lifecycle_event_counts"],
    }


async def _build_dense_live_conversation(
    session: ReliabilitySession,
    *,
    prefix: str,
    turn_count: int = 4,
    repeated_lines: int = 180,
    first_turn_tail_sentinel: str = "",
    target_named_agent_type: str = "TeamArchitect",
) -> dict:
    token = f"{prefix}-token-{new_id_suffix()}"
    conversation_id: Optional[str] = None
    prior_task_id: Optional[str] = None
    responses = []
    for turn_index in range(1, turn_count + 1):
        repeated_block = "\n".join(
            [
                f"{prefix.upper()} background turn {turn_index} line {line_index} token {token}"
                for line_index in range(repeated_lines)
            ]
        )
        if turn_index == 1 and first_turn_tail_sentinel:
            repeated_block = f"{repeated_block}\nTAIL RAW ONLY SENTINEL: {first_turn_tail_sentinel}"
        prompt = (
            f"This is turn {turn_index} in an ongoing conversation.\n"
            f"Background block:\n{repeated_block}\n\n"
            "Reply in one short sentence acknowledging the token and turn number."
        )
        envelope, corr = session.build_task_envelope(
            prompt,
            routing_key=session.manager_id,
            to_named_agent_type=target_named_agent_type,
            response_routing_key=session.user_queue_name,
            conversation_id=conversation_id,
            followup_to_task_id=prior_task_id,
        )
        await session.send_envelope(envelope, routing_key=session.manager_id, label=f"{prefix}-turn-{turn_index}")
        events = await session.receive_matching(
            predicate=lambda env, corr=corr: env.msg_meta and env.msg_meta.msg_correlation_id == corr and isinstance(env.msg_content, Task),
            expected_count=1,
            timeout=max(session.timeout, 180),
        )
        if len(events) != 1:
            raise AssertionError(f"Did not receive turn {turn_index} response for {prefix}.")
        response_envelope = events[0].envelope
        conversation_id = response_envelope.msg_meta.conversation_id
        prior_task_id = response_envelope.msg_content.task_id
        responses.append(
            {
                "turn_index": turn_index,
                "correlation_id": corr,
                "task_id": response_envelope.msg_content.task_id,
                "answer": str(response_envelope.msg_content.answer or ""),
            }
        )

    if not conversation_id or not prior_task_id:
        raise AssertionError("Dense live conversation did not produce a conversation_id.")

    followup_prompt = (
        "In this same conversation, what token has been repeated across the earlier turns? "
        "Reply with just the token."
    )
    envelope, corr = session.build_task_envelope(
        followup_prompt,
        routing_key=session.manager_id,
        to_named_agent_type=target_named_agent_type,
        response_routing_key=session.user_queue_name,
        conversation_id=conversation_id,
        followup_to_task_id=prior_task_id,
    )
    await session.send_envelope(envelope, routing_key=session.manager_id, label=f"{prefix}-followup")
    events = await session.receive_matching(
        predicate=lambda env: env.msg_meta and env.msg_meta.msg_correlation_id == corr and isinstance(env.msg_content, Task),
        expected_count=1,
        timeout=max(session.timeout, 180),
    )
    if len(events) != 1:
        raise AssertionError(f"Did not receive final follow-up response for {prefix}.")
    final_response = events[0].envelope
    final_answer = str(final_response.msg_content.answer or "")
    if token not in final_answer:
        raise AssertionError(f"Follow-up reply did not recover the token for {prefix}: {final_answer}")

    thread = get_conversation_thread_row(session.db_path, conversation_id)
    turns = get_conversation_turn_rows(session.db_path, conversation_id)
    artifacts = get_conversation_artifacts(session.db_path, conversation_id=conversation_id)
    if thread is None:
        raise AssertionError(f"Conversation thread {conversation_id} was not persisted.")
    return {
        "conversation_id": conversation_id,
        "token": token,
        "responses": responses,
        "final_task_id": final_response.msg_content.task_id,
        "final_answer": final_answer,
        "thread": thread,
        "turns": turns,
        "artifacts": artifacts,
        "latest_request_text": "What should we do next in this conversation?",
    }


async def wait_for_condition(
    description: str,
    predicate: Callable[[], bool],
    timeout: float,
    interval: float = 0.5,
) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        await asyncio.sleep(interval)
    raise TimeoutError(f"Timed out waiting for: {description}")


@dataclass
class ReceivedEvent:
    envelope: MessageEnvelope
    received_at: str


@dataclass
class ManagedAgentProcess:
    agent_type: str
    team_id: str
    agent_id: str
    result_dir: Path
    env_path: Optional[str]
    storage_settings: StorageSettings
    is_lead: bool = False
    agent_result_transformer: Optional[str] = None
    config_path: Optional[Path] = None
    extra_env: Optional[dict[str, str]] = None
    process: Optional[asyncio.subprocess.Process] = field(default=None, init=False, repr=False)
    stdout_log_path: Path = field(init=False)
    stderr_log_path: Path = field(init=False)
    _stdout_handle: Optional[object] = field(default=None, init=False, repr=False)
    _stderr_handle: Optional[object] = field(default=None, init=False, repr=False)

    def __post_init__(self) -> None:
        safe_prefix = f"{self.agent_type}_{self.agent_id}".replace(os.sep, "_")
        self.stdout_log_path = self.result_dir / f"{safe_prefix}_stdout.log"
        self.stderr_log_path = self.result_dir / f"{safe_prefix}_stderr.log"

    @property
    def is_running(self) -> bool:
        return self.process is not None and self.process.returncode is None

    async def start(self) -> None:
        if self.process is not None:
            raise RuntimeError(f"Managed agent process {self.agent_id} has already been started.")

        cmd = self.build_command()
        env = self.build_env()

        self._stdout_handle = self.stdout_log_path.open("w", encoding="utf-8")
        self._stderr_handle = self.stderr_log_path.open("w", encoding="utf-8")
        self.process = await asyncio.create_subprocess_exec(
            *cmd,
            cwd=str(live_nats_client.PROJECT_ROOT),
            env=env,
            stdout=self._stdout_handle,
            stderr=self._stderr_handle,
        )

    def build_command(self) -> list[str]:
        cmd = [
            get_repo_python_executable(),
            "strands_main.py",
            "--agent-type",
            self.agent_type,
            "--team-id",
            self.team_id,
            "--agent-id",
            self.agent_id,
        ]
        if self.storage_settings.backend == "sqlite" and self.storage_settings.sqlite_path:
            cmd.extend(["--task-history-db-path", self.storage_settings.sqlite_path])
        if self.is_lead:
            cmd.append("--is-lead")
        if self.env_path:
            cmd.extend(["--env", self.env_path])
        if self.config_path:
            cmd.extend(["--config", str(self.config_path)])
        if self.agent_result_transformer:
            cmd.extend(["--agent-result-transformer", self.agent_result_transformer])
        return cmd

    def build_env(self) -> dict[str, str]:
        env = os.environ.copy()
        env.setdefault("PYTHONUNBUFFERED", "1")
        for key, value in (self.extra_env or {}).items():
            env[key] = str(value)
        return apply_storage_env(env, self.storage_settings)

    async def wait_for_exit(self, timeout: float) -> int:
        if self.process is None:
            raise RuntimeError(f"Managed agent process {self.agent_id} has not been started.")
        return await asyncio.wait_for(self.process.wait(), timeout=timeout)

    async def stop(self, timeout: float = 10.0) -> Optional[int]:
        if self.process is None:
            self._close_handles()
            return None
        if self.process.returncode is not None:
            self._close_handles()
            return self.process.returncode

        self.process.terminate()
        try:
            returncode = await asyncio.wait_for(self.process.wait(), timeout=timeout)
        except asyncio.TimeoutError:
            self.process.kill()
            returncode = await self.process.wait()
        self._close_handles()
        return returncode

    def snapshot(self) -> dict:
        return {
            "agent_type": self.agent_type,
            "agent_id": self.agent_id,
            "team_id": self.team_id,
            "env_path": self.env_path,
            "storage_backend": self.storage_settings.backend,
            "storage_locator": storage_identifier(self.storage_settings),
            "is_lead": self.is_lead,
            "agent_result_transformer": self.agent_result_transformer,
            "config_path": None if self.config_path is None else str(self.config_path),
            "extra_env": dict(self.extra_env or {}),
            "stdout_log_path": str(self.stdout_log_path),
            "stderr_log_path": str(self.stderr_log_path),
            "returncode": None if self.process is None else self.process.returncode,
        }

    def _close_handles(self) -> None:
        for handle in (self._stdout_handle, self._stderr_handle):
            if handle is not None and not handle.closed:
                handle.flush()
                handle.close()


class ReliabilitySession:
    def __init__(
        self,
        team_id: str,
        result_dir: Path,
        timeout: int,
        env_path: Optional[str] = None,
        *,
        auto_manager: Optional[dict] = None,
        storage_settings: Optional[StorageSettings] = None,
        db_path: Optional[Path] = None,
    ):
        self.team_id = team_id
        self.result_dir = result_dir
        self.timeout = timeout
        self.env_path = env_path
        self.auto_manager = auto_manager or {}
        self.connection: Optional[live_nats_client.LiveNatsClient] = None
        self.user_queue: Optional[live_nats_client.LiveNatsQueue] = None
        self.user_queue_name = new_id("usr_int_queue")
        self.manager_id: Optional[str] = None
        self.event_log_path = result_dir / "message_events.jsonl"
        sqlite_fallback = db_path or live_nats_client.get_live_test_task_history_db_path()
        self.storage_settings = storage_settings or resolve_live_storage_settings(
            sqlite_fallback_path=sqlite_fallback,
            workspace_dir=live_nats_client.PROJECT_ROOT,
        )
        self.db_path: Path | StorageSettings = (
            Path(self.storage_settings.sqlite_path)
            if self.storage_settings.backend == "sqlite" and self.storage_settings.sqlite_path
            else self.storage_settings
        )
        self._managed_processes: list[ManagedAgentProcess] = []
        self._received_event_buffer: list[ReceivedEvent] = []

    async def __aenter__(self):
        if self.auto_manager:
            await self.launch_agent_process(
                agent_type=self.auto_manager.get("agent_type", "TeamArchitect"),
                agent_id=self.auto_manager.get("agent_id"),
                is_lead=True,
                agent_result_transformer=self.auto_manager.get("agent_result_transformer"),
                config_path=self.auto_manager.get("config_path"),
                extra_env=self.auto_manager.get("extra_env"),
            )

        self.connection = live_nats_client.LiveNatsClient(self.team_id)
        await self.connection.__aenter__()
        self.user_queue = await self.connection.subscribe_route(self.user_queue_name)
        await self.user_queue.purge()
        if self.auto_manager and self.auto_manager.get("agent_id"):
            await wait_for_agent_row(
                self.db_path,
                team_id=self.team_id,
                agent_type=self.auto_manager.get("agent_type", "TeamArchitect"),
                agent_id=self.auto_manager["agent_id"],
                timeout=45,
            )
            self.manager_id = self.auto_manager["agent_id"]
        else:
            self.manager_id = await live_nats_client.discover_manager_id(self.connection, self.team_id, self.user_queue)
        live_nats_client.write_json_file(
            self.result_dir / "run_metadata.json",
            {
                "team_id": self.team_id,
                "result_dir": str(self.result_dir),
                "manager_id": self.manager_id,
                "task_history_db_path": str(self.db_path) if isinstance(self.db_path, Path) else None,
                "storage_backend": self.storage_settings.backend,
                "storage_locator": storage_identifier(self.storage_settings),
                "env_path": self.env_path,
                "started_at": datetime.now(timezone.utc).isoformat(),
            },
        )
        return self

    async def __aexit__(self, exc_type, exc, tb):
        for managed in reversed(self._managed_processes):
            with contextlib.suppress(Exception):
                await managed.stop()
        if self.user_queue is not None:
            await self.user_queue.purge()
        if self.connection is not None:
            await self.connection.__aexit__(exc_type, exc, tb)

    async def launch_agent_process(
        self,
        *,
        agent_type: str,
        agent_id: Optional[str] = None,
        team_id: Optional[str] = None,
        is_lead: bool = False,
        agent_result_transformer: Optional[str] = None,
        config_path: Optional[Path] = None,
        extra_env: Optional[dict[str, str]] = None,
    ) -> ManagedAgentProcess:
        effective_config_path = config_path
        if effective_config_path is None and self.auto_manager:
            effective_config_path = self.auto_manager.get("config_path")
        managed = ManagedAgentProcess(
            agent_type=agent_type,
            team_id=team_id or self.team_id,
            agent_id=agent_id or new_id("live_agent"),
            result_dir=self.result_dir,
            env_path=self.env_path,
            storage_settings=self.storage_settings,
            is_lead=is_lead,
            agent_result_transformer=agent_result_transformer,
            config_path=effective_config_path,
            extra_env=extra_env,
        )
        await managed.start()
        self._managed_processes.append(managed)
        return managed

    def _record_sent(self, envelope: MessageEnvelope, routing_key: str, label: str) -> None:
        live_nats_client.append_jsonl_file(
            self.event_log_path,
            {
                "timestamp": datetime.now(timezone.utc).isoformat(),
                "event": "sent_message",
                "label": label,
                "routing_key": routing_key,
                "envelope": envelope.model_dump(mode="json"),
            },
        )

    def _record_received(self, envelope: MessageEnvelope) -> None:
        live_nats_client.append_jsonl_file(
            self.event_log_path,
            {
                "timestamp": datetime.now(timezone.utc).isoformat(),
                "event": "received_message",
                "envelope": envelope.model_dump(mode="json"),
            },
        )

    def build_task_envelope(
        self,
        problem_statement: str,
        *,
        routing_key: str,
        to_named_agent_type: Optional[str] = None,
        to_agent_name: Optional[str] = None,
        from_agent_id: str = "user",
        from_agent_name: str = "user",
        response_requested: bool = True,
        correlation_id: Optional[str] = None,
        response_routing_key: Optional[str] = None,
        conversation_id: Optional[str] = None,
        followup_to_task_id: Optional[str] = None,
    ) -> tuple[MessageEnvelope, str]:
        corr = correlation_id or new_id("corr_id")
        envelope = MessageEnvelope(
            msg_content=Task(
                task_type=MessageMetaType.REQUEST,
                problem_statement=problem_statement,
            ),
            msg_meta=MessageMeta(
                from_agent_name=from_agent_name,
                to_named_agent_type=to_named_agent_type,
                to_agent_name=to_agent_name,
                is_response_requested=response_requested,
                msg_correlation_id=corr,
                conversation_id=conversation_id,
                followup_to_task_id=followup_to_task_id,
            ),
            msg_header=MessageHeader(
                from_agent_id=from_agent_id,
                to_routing_path=routing_key,
                response_routing_key=response_routing_key,
                agent_signature="",
            ),
        )
        envelope = live_nats_client._ensure_response_metadata(envelope, default_from_agent_name=from_agent_name)
        if not response_requested:
            envelope.msg_meta.is_response_requested = False
        if response_routing_key is None:
            envelope.msg_header.response_routing_key = None
        return envelope, corr

    def build_control_envelope(self, target_agent_id: str, instruction: str, *, from_agent_id: str = "user_client") -> MessageEnvelope:
        return MessageEnvelope(
            msg_content=ControlInstruction(
                type=ControlType.CONTROL_MSG,
                instruction=Instruction(instruction=instruction),
            ),
            msg_meta=MessageMeta(from_agent_name="user_client"),
            msg_header=MessageHeader(
                from_agent_id=from_agent_id,
                to_routing_path=target_agent_id,
                agent_signature="",
            ),
        )

    def build_status_request_envelope(self, target_routing_path: str, *, correlation_id: Optional[str] = None) -> tuple[MessageEnvelope, str]:
        corr = correlation_id or new_id("corr_id")
        envelope = MessageEnvelope(
            msg_content=AgentStatus(
                self_id="user_client",
                team_id=self.team_id,
                status_type=MessageMetaType.REQUEST,
                info_type=ControlType.STATUS_REQUEST_BASIC,
                named_agent_type="user_client",
                is_paused=False,
                is_busy=False,
                name="user_client",
                agent_role="user_client",
            ),
            msg_meta=MessageMeta(from_agent_name="user_client", msg_correlation_id=corr),
            msg_header=MessageHeader(
                from_agent_id="user_client",
                to_routing_path=target_routing_path,
                response_routing_key=self.user_queue_name,
                agent_signature="",
            ),
        )
        return envelope, corr

    def build_scheduled_message_envelope(
        self,
        *,
        routing_key: str,
        payload_problem_statement: str,
        interval_ms: int,
        repeat_count: int = 1,
    ) -> MessageEnvelope:
        return MessageEnvelope(
            msg_content=ScheduledMessage(
                payload=Task(
                    task_type=MessageMetaType.REQUEST,
                    problem_statement=payload_problem_statement,
                ),
                interval_ms=interval_ms,
                repeat_count=repeat_count,
            ),
            msg_meta=MessageMeta(from_agent_name="user"),
            msg_header=MessageHeader(
                from_agent_id="user",
                to_routing_path=routing_key,
                agent_signature="",
            ),
        )

    async def send_envelope(self, envelope: MessageEnvelope, *, routing_key: Optional[str] = None, label: str = "") -> None:
        actual_routing_key = routing_key or str(envelope.msg_header.to_routing_path)
        self._record_sent(envelope, actual_routing_key, label)
        if self.connection is None:
            raise RuntimeError("ReliabilitySession is not connected to NATS.")
        await self.connection.publish(
            actual_routing_key,
            envelope,
            durable=True,
            operational=actual_routing_key == RoutingType.ALL_AGENTS_ROUTE.value,
        )

    async def send_operational_envelope(self, envelope: MessageEnvelope, *, label: str = "") -> None:
        self._record_sent(envelope, f"{self.team_id}_operational", label)
        if self.connection is None:
            raise RuntimeError("ReliabilitySession is not connected to NATS.")
        await self.connection.publish(
            str(envelope.msg_header.to_routing_path or RoutingType.ALL_AGENTS_ROUTE.value),
            envelope,
            operational=True,
            durable=False,
        )

    async def _receive_next_event(self, *, timeout: float) -> Optional[ReceivedEvent]:
        if self.user_queue is None:
            raise RuntimeError("ReliabilitySession has no NATS response subscription.")
        try:
            envelope = await self.user_queue.get(timeout=timeout)
        except asyncio.TimeoutError:
            return None
        self._record_received(envelope)
        return ReceivedEvent(
            envelope=envelope,
            received_at=datetime.now(timezone.utc).isoformat(),
        )

    async def receive_matching(
        self,
        *,
        predicate: Callable[[MessageEnvelope], bool],
        expected_count: int,
        timeout: float,
    ) -> list[ReceivedEvent]:
        matched: list[ReceivedEvent] = []
        still_buffered: list[ReceivedEvent] = []
        for event in self._received_event_buffer:
            if len(matched) < expected_count and predicate(event.envelope):
                matched.append(event)
            else:
                still_buffered.append(event)
        self._received_event_buffer = still_buffered
        if len(matched) >= expected_count:
            return matched
        deadline = time.monotonic() + timeout
        while len(matched) < expected_count:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            event = await self._receive_next_event(timeout=remaining)
            if event is None:
                break
            if predicate(event.envelope):
                matched.append(event)
            else:
                self._received_event_buffer.append(event)
        return matched

    async def expect_no_matching(self, *, predicate: Callable[[MessageEnvelope], bool], timeout: float) -> bool:
        remaining_buffer: list[ReceivedEvent] = []
        for event in self._received_event_buffer:
            if predicate(event.envelope):
                return False
            remaining_buffer.append(event)
        self._received_event_buffer = remaining_buffer
        deadline = time.monotonic() + timeout
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return True
            event = await self._receive_next_event(timeout=remaining)
            if event is None:
                return True
            if predicate(event.envelope):
                return False
            self._received_event_buffer.append(event)

    async def collect_matching_for_duration(
        self,
        *,
        predicate: Callable[[MessageEnvelope], bool],
        duration: float,
    ) -> list[ReceivedEvent]:
        collected: list[ReceivedEvent] = []
        retained_buffer: list[ReceivedEvent] = []
        for event in self._received_event_buffer:
            if predicate(event.envelope):
                collected.append(event)
            else:
                retained_buffer.append(event)
        self._received_event_buffer = retained_buffer
        deadline = time.monotonic() + duration
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            event = await self._receive_next_event(timeout=remaining)
            if event is None:
                break
            if predicate(event.envelope):
                collected.append(event)
            else:
                self._received_event_buffer.append(event)
        return collected


async def instantiate_new_agents(
    session: ReliabilitySession,
    agent_type: str,
    count: int,
    baseline_ids: set[str],
    *,
    storage_target: Path | StorageSettings | str,
) -> list[dict]:
    team_id = session.team_id
    for _ in range(count):
        await session.launch_agent_process(agent_type=agent_type)

    def _have_new_agents() -> bool:
        rows = get_agent_rows(storage_target, team_id, agent_type)
        new_ids = [row["agent_id"] for row in rows if row["agent_id"] not in baseline_ids and not row["is_exiting"]]
        return len(set(new_ids)) >= count

    await wait_for_condition(f"{count} new {agent_type} agents", _have_new_agents, timeout=45)
    rows = get_agent_rows(storage_target, team_id, agent_type)
    new_rows = [row for row in rows if row["agent_id"] not in baseline_ids and not row["is_exiting"]]
    return new_rows[:count]


async def send_exit_to_agents(session: ReliabilitySession, agent_ids: list[str]) -> None:
    for agent_id in agent_ids:
        await session.send_envelope(
            session.build_control_envelope(agent_id, "exit"),
            routing_key=agent_id,
            label=f"exit:{agent_id}",
        )


async def wait_for_agent_row(
    db_path: Path,
    *,
    team_id: str,
    agent_type: str,
    agent_id: str,
    timeout: float,
) -> dict:
    await wait_for_condition(
        f"{agent_type} agent row for {agent_id}",
        lambda: any(
            row["agent_id"] == agent_id and not row["is_exiting"]
            for row in get_agent_rows(db_path, team_id, agent_type)
        ),
        timeout=timeout,
    )
    rows = get_agent_rows(db_path, team_id, agent_type)
    for row in rows:
        if row["agent_id"] == agent_id:
            return row
    raise AssertionError(f"Did not find persisted agent state for {agent_type} {agent_id}.")


async def send_gateway_style_task(
    session: ReliabilitySession,
    *,
    canonical_user_id: str,
    prompt: str,
    conversation_id: Optional[str] = None,
    followup_to_task_id: Optional[str] = None,
    correlation_suffix: str = "",
) -> MessageEnvelope:
    envelope, corr = session.build_task_envelope(
        prompt,
        routing_key=session.manager_id,
        to_named_agent_type="TeamArchitect",
        from_agent_id=f"gateway_user_{canonical_user_id}",
        from_agent_name="live_gateway",
        response_routing_key=session.user_queue_name,
        conversation_id=conversation_id,
        followup_to_task_id=followup_to_task_id,
        correlation_id=(new_id("corr_live") if correlation_suffix else None),
    )
    envelope.msg_meta.requester_type = RequesterType.USER_REQUESTER
    envelope.msg_meta.gateway_context = GatewayContext(
        gateway_instance_name="live_gateway",
        session_key=f"session_{canonical_user_id}",
        channel_name="local",
        user_id=canonical_user_id,
        canonical_user_id=canonical_user_id,
        chat_id=f"chat_{canonical_user_id}",
        delivery_mode="interactive",
    )
    await session.send_envelope(envelope, routing_key=session.manager_id, label=f"memory:{canonical_user_id}")
    events = await session.receive_matching(
        predicate=lambda env, expected_corr=corr: env.msg_meta and env.msg_meta.msg_correlation_id == expected_corr and isinstance(env.msg_content, Task),
        expected_count=1,
        timeout=max(session.timeout, 180),
    )
    if len(events) != 1:
        raise AssertionError(f"Expected one Task response for canonical_user_id={canonical_user_id}.")
    return events[0].envelope


async def stop_managed_agents(session: ReliabilitySession, processes: list[ManagedAgentProcess]) -> list[dict]:
    running = [proc for proc in processes if proc.is_running]
    if running:
        await send_exit_to_agents(session, [proc.agent_id for proc in running])

    outcomes = []
    for proc in processes:
        try:
            if proc.process is not None and proc.process.returncode is None:
                returncode = await proc.wait_for_exit(timeout=20)
            else:
                returncode = None if proc.process is None else proc.process.returncode
        except asyncio.TimeoutError:
            returncode = await proc.stop(timeout=5)
        outcomes.append(
            {
                **proc.snapshot(),
                "returncode": returncode,
            }
        )
    return outcomes


def write_scenario_config(
    result_dir: Path,
    filename: str,
    overrides: dict,
    *,
    base_config_path: str | Path = "config.yaml",
) -> Path:
    base_config = load_config(str(base_config_path)).to_dict()
    merged = deep_merge_dicts(base_config, overrides)
    config_path = result_dir / filename
    config_path.write_text(yaml.safe_dump(merged, sort_keys=False), encoding="utf-8")
    return config_path


def assert_no_preexisting_specialists(
    db_path: Path,
    *,
    team_id: str,
    agent_type: str,
) -> None:
    active_rows = [row for row in get_agent_rows(db_path, team_id, agent_type) if not row["is_exiting"]]
    if active_rows:
        raise AssertionError(
            f"Live recovery scenario requires an isolated {agent_type} queue, but found existing specialists: "
            f"{[row['agent_id'] for row in active_rows]}"
        )


def _build_manager_web_delegate_prompt(result_dir: Path, topic: str) -> str:
    return (
        "Delegate this request to an already-running WebResearchAgent specialist. "
        "Do not use your own web-search tools and do not instantiate any new agents. "
        f"Ask the WebResearchAgent to browse the web for {topic}. "
        "Have the specialist return a concise summary with the source name, three core ingredients, and one short note "
        f"about why the recipe is straightforward. If the specialist writes any scratch files, use `{result_dir}`. "
        "Wait for the delegated specialist reply and then send me the final Task response."
    )


def _build_direct_worker_web_prompt(topic: str, result_dir: Path) -> str:
    return (
        f"Browse the web for {topic}. Return a concise summary with a source name, three ingredients, and one short "
        f"note about why the recipe is easy. If you write any scratch files, place them under `{result_dir}`."
    )


def _managed_recovery_overrides(
    *,
    subtask_timeout_seconds: int = 12,
    subtask_watchdog_interval_seconds: int = 2,
    max_queued_top_level_tasks: int = 6,
    max_concurrent_top_level_tasks: int = 1,
) -> dict:
    return {
        "agent": {
            "subtask_timeout_seconds": subtask_timeout_seconds,
            "subtask_watchdog_interval_seconds": subtask_watchdog_interval_seconds,
            "max_queued_top_level_tasks": max_queued_top_level_tasks,
            "max_concurrent_top_level_tasks": max_concurrent_top_level_tasks,
        }
    }


def _noop_mcp_profile_overrides() -> dict:
    return {
        "agent_types": {
            "InternalImprovementAgent": {
                "agent": {
                    "external_mcp_servers": [
                        {
                            "name": "noop-test",
                            "transport": "stdio",
                            "command": get_repo_python_executable(),
                            "args": ["mcp/noop_mcp.py"],
                            "tool_name_prefix": "noop_test",
                        }
                    ]
                }
            }
        }
    }


async def scenario_8(session: ReliabilitySession) -> tuple[str, dict]:
    prompt_a = f"What is 2 + 2? If you create any files, place them under `{session.result_dir}`."
    prompt_b = (
        "Before doing any implementation work, ask me exactly one clarifying question as a TaskQuery "
        "response and do not solve the request yet. The request is: build me something helpful."
    )
    envelope_a, corr_a = session.build_task_envelope(
        prompt_a,
        routing_key=session.manager_id,
        to_named_agent_type="TeamArchitect",
        response_routing_key=session.user_queue_name,
    )
    envelope_b, corr_b = session.build_task_envelope(
        prompt_b,
        routing_key=session.manager_id,
        to_named_agent_type="TeamArchitect",
        response_routing_key=session.user_queue_name,
    )
    await asyncio.gather(
        session.send_envelope(envelope_a, routing_key=session.manager_id, label="simple-response"),
        session.send_envelope(envelope_b, routing_key=session.manager_id, label="clarification-request"),
    )
    events = await session.receive_matching(
        predicate=lambda env: env.msg_meta and env.msg_meta.msg_correlation_id in {corr_a, corr_b},
        expected_count=2,
        timeout=session.timeout,
    )
    if len(events) != 2:
        raise AssertionError(f"Expected 2 correlated replies, received {len(events)}")

    by_corr = {event.envelope.msg_meta.msg_correlation_id: event.envelope for event in events}
    env_a = by_corr.get(corr_a)
    env_b = by_corr.get(corr_b)
    if env_a is None or not isinstance(env_a.msg_content, Task):
        raise AssertionError("Did not receive a Task response for the simple request.")
    if env_a.msg_content.task_type != MessageMetaType.RESPONSE:
        raise AssertionError("Simple request did not produce a Task response.")
    if env_b is None or not isinstance(env_b.msg_content, TaskQuery):
        raise AssertionError("Clarification request did not produce a TaskQuery.")
    if env_b.msg_content.query_type != MessageMetaType.REQUEST:
        raise AssertionError("Clarification message was not a TaskQuery request.")

    return (
        "Received two independent replies with preserved correlation IDs; the simple request returned a Task response and the ambiguous request returned a TaskQuery on the user queue.",
        {
            "simple_correlation_id": corr_a,
            "clarification_correlation_id": corr_b,
            "simple_reply_type": type(env_a.msg_content).__name__,
            "clarification_reply_type": type(env_b.msg_content).__name__,
        },
    )


async def scenario_10(session: ReliabilitySession) -> tuple[str, dict]:
    target_file = session.result_dir / "subtask_lifecycle_hello.txt"
    prompt = (
        "Instantiate a CodeWritingAgent and delegate this implementation as a subtask. "
        f"Have the specialist create the file `{target_file}` containing the exact text `subtask lifecycle ok`. "
        "Return a final Task response only after the delegated subtask completes successfully."
    )
    envelope, corr = session.build_task_envelope(
        prompt,
        routing_key=session.manager_id,
        to_named_agent_type="TeamArchitect",
        response_routing_key=session.user_queue_name,
    )
    await session.send_envelope(envelope, routing_key=session.manager_id, label="subtask-lifecycle")
    events = await session.receive_matching(
        predicate=lambda env: env.msg_meta and env.msg_meta.msg_correlation_id == corr and isinstance(env.msg_content, Task),
        expected_count=1,
        timeout=session.timeout,
    )
    if len(events) != 1:
        raise AssertionError("Did not receive the subtask lifecycle response.")
    if not target_file.exists():
        raise AssertionError(f"Expected delegated output file was not created: {target_file}")
    response = events[0].envelope
    try:
        await wait_for_condition(
            "Subtask parent task archived row",
            lambda: get_archived_task_row(
                session.db_path,
                response.msg_content.task_id,
                owner_agent_id=response.msg_header.from_agent_id,
            ) is not None,
            timeout=10,
        )
    except TimeoutError:
        raise AssertionError("Parent task was not archived.")

    archived = get_archived_task_row(
        session.db_path,
        response.msg_content.task_id,
        owner_agent_id=response.msg_header.from_agent_id,
    )
    history = archived["task_data"].get("execution_history") or []
    if not any("subtask" in (entry.get("summary", "").lower()) for entry in history):
        raise AssertionError("Parent task history does not show subtask activity.")

    return (
        "The manager delegated work to a specialist, resumed after the child completed, and returned the final response.",
        {
            "task_id": response.msg_content.task_id,
            "delegated_file": str(target_file),
            "history_entries": len(history),
        },
    )


async def scenario_11(session: ReliabilitySession) -> tuple[str, dict]:
    fire_file = session.result_dir / "scheduled_fire.txt"
    payload_prompt = (
        f"When this scheduled task fires, use shell to write the exact text `scheduled ok` to `{fire_file}`. "
        "Do not ask clarifying questions."
    )
    envelope = session.build_scheduled_message_envelope(
        routing_key=session.manager_id,
        payload_problem_statement=payload_prompt,
        interval_ms=2000,
        repeat_count=1,
    )
    schedule_id = envelope.msg_content.schedule_id
    await session.send_envelope(envelope, routing_key=session.manager_id, label="scheduled-message")
    await wait_for_condition(
        f"scheduled task file {fire_file}",
        lambda: fire_file.exists(),
        timeout=session.timeout,
    )
    if get_schedule_row(session.db_path, schedule_id) is not None:
        raise AssertionError("One-shot scheduled message still exists after it should have fired.")

    return (
        "A one-shot ScheduledMessage fired successfully and produced its expected file output.",
        {
            "schedule_id": schedule_id,
            "output_file": str(fire_file),
        },
    )


async def scenario_12(session: ReliabilitySession) -> tuple[str, dict]:
    baseline_ids = {row["agent_id"] for row in get_agent_rows(session.db_path, session.team_id, "CodeWritingAgent")}
    new_agents = await instantiate_new_agents(
        session,
        "CodeWritingAgent",
        3,
        baseline_ids,
        storage_target=session.db_path,
    )
    new_agent_ids = [row["agent_id"] for row in new_agents]

    correlations = []
    for index in range(4):
        prompt = (
            f"Queue-routing live test task {index + 1}. Use shell to `sleep 4`, then reply with your own name and id. "
            f"Also use shell to write the same information to `{session.result_dir / f'queue_task_{index + 1}.txt'}`."
        )
        envelope, corr = session.build_task_envelope(
            prompt,
            routing_key=f"{session.team_id}_CodeWritingAgent",
            to_named_agent_type="CodeWritingAgent",
            response_routing_key=session.user_queue_name,
        )
        correlations.append(corr)
        await session.send_envelope(envelope, routing_key=f"{session.team_id}_CodeWritingAgent", label=f"queue-task-{index + 1}")

    events = await session.receive_matching(
        predicate=lambda env: env.msg_meta and env.msg_meta.msg_correlation_id in set(correlations) and isinstance(env.msg_content, Task),
        expected_count=4,
        timeout=max(session.timeout, 90),
    )
    await send_exit_to_agents(session, new_agent_ids)
    if len(events) != 4:
        raise AssertionError(f"Expected 4 specialist replies, received {len(events)}")

    responding_ids = [event.envelope.msg_header.from_agent_id for event in events]
    distinct_ids = set(responding_ids)
    if len(distinct_ids) < 2:
        raise AssertionError(f"Expected at least 2 distinct specialists, saw {len(distinct_ids)}: {responding_ids}")
    counts = {agent_id: responding_ids.count(agent_id) for agent_id in distinct_ids}
    if max(counts.values()) < 2:
        raise AssertionError("Expected at least one specialist to handle multiple queued tasks from the worker pool.")

    return (
        "The NATS worker pool delivered each named-queue task to exactly one specialist, and at least one specialist handled additional work from the pool.",
        {
            "new_agent_ids": new_agent_ids,
            "response_agent_ids": responding_ids,
            "counts": counts,
        },
    )


async def scenario_13(session: ReliabilitySession) -> tuple[str, dict]:
    baseline_ids = {row["agent_id"] for row in get_agent_rows(session.db_path, session.team_id, "CodeWritingAgent")}
    new_agents = await instantiate_new_agents(
        session,
        "CodeWritingAgent",
        2,
        baseline_ids,
        storage_target=session.db_path,
    )
    target = new_agents[0]
    other = new_agents[1]
    target_file = session.result_dir / "direct_target.txt"
    prompt = (
        f"Direct-targeting live test. Write your own agent id and name to `{target_file}` and reply with the same information."
    )
    envelope, corr = session.build_task_envelope(
        prompt,
        routing_key=target["agent_id"],
        to_agent_name=target["agent_name"],
        response_routing_key=session.user_queue_name,
    )
    await session.send_envelope(envelope, routing_key=target["agent_id"], label="direct-target")
    events = await session.receive_matching(
        predicate=lambda env: env.msg_meta and env.msg_meta.msg_correlation_id == corr and isinstance(env.msg_content, Task),
        expected_count=1,
        timeout=session.timeout,
    )
    await send_exit_to_agents(session, [target["agent_id"], other["agent_id"]])
    if len(events) != 1:
        raise AssertionError("Did not receive the direct-target response.")
    response = events[0].envelope
    if response.msg_header.from_agent_id != target["agent_id"]:
        raise AssertionError(
            f"Direct-targeted response came from {response.msg_header.from_agent_id}, expected {target['agent_id']}"
        )
    if not target_file.exists():
        raise AssertionError(f"Direct target file was not created: {target_file}")

    return (
        "A directly routed task bypassed the named-agent-type queue and was answered only by the targeted specialist.",
        {
            "target_agent_id": target["agent_id"],
            "non_target_agent_id": other["agent_id"],
            "response_agent_id": response.msg_header.from_agent_id,
            "output_file": str(target_file),
        },
    )


async def scenario_14(session: ReliabilitySession) -> tuple[str, dict]:
    baseline_ids = {row["agent_id"] for row in get_agent_rows(session.db_path, session.team_id, "CodeWritingAgent")}
    new_agents = await instantiate_new_agents(
        session,
        "CodeWritingAgent",
        1,
        baseline_ids,
        storage_target=session.db_path,
    )
    target = new_agents[0]
    paused_file = session.result_dir / "paused_specialist_probe.txt"

    await session.send_envelope(
        session.build_control_envelope(target["agent_id"], "pause"),
        routing_key=target["agent_id"],
        label="pause-specialist",
    )
    await wait_for_condition(
        f"agent {target['agent_id']} paused",
        lambda: any(
            row["agent_id"] == target["agent_id"] and row["is_paused"]
            for row in get_agent_rows(session.db_path, session.team_id, "CodeWritingAgent")
        ),
        timeout=20,
    )

    prompt = f"Create the file `{paused_file}` with the exact text `processed after resume`."
    envelope, corr = session.build_task_envelope(
        prompt,
        routing_key=target["agent_id"],
        to_agent_name=target["agent_name"],
        response_routing_key=session.user_queue_name,
    )
    await session.send_envelope(envelope, routing_key=target["agent_id"], label="paused-specialist-task")
    no_early_response = await session.expect_no_matching(
        predicate=lambda env: env.msg_meta and env.msg_meta.msg_correlation_id == corr,
        timeout=4,
    )
    if not no_early_response:
        raise AssertionError("Paused specialist replied before it was resumed.")
    if paused_file.exists():
        raise AssertionError("Paused specialist processed the direct task before resume.")

    await session.send_envelope(
        session.build_control_envelope(target["agent_id"], "resume"),
        routing_key=target["agent_id"],
        label="resume-specialist",
    )
    events = await session.receive_matching(
        predicate=lambda env: env.msg_meta and env.msg_meta.msg_correlation_id == corr and isinstance(env.msg_content, Task),
        expected_count=1,
        timeout=max(session.timeout, 120),
    )
    await send_exit_to_agents(session, [target["agent_id"]])
    if len(events) != 1:
        raise AssertionError("Did not receive the specialist response after resume.")
    if not paused_file.exists():
        raise AssertionError("Paused specialist did not process the deferred task after resume.")

    return (
        "A specialist was paused, direct work stayed deferred while paused, then the queued work completed after resume.",
        {
            "specialist_agent_id": target["agent_id"],
            "specialist_agent_name": target["agent_name"],
            "output_file": str(paused_file),
        },
    )


async def scenario_15(session: ReliabilitySession) -> tuple[str, dict]:
    prompt = "This request intentionally does not ask for a reply. Do not create any files."
    envelope, corr = session.build_task_envelope(
        prompt,
        routing_key=session.manager_id,
        to_named_agent_type="TeamArchitect",
        from_agent_id="user_silent",
        from_agent_name="user_silent",
        response_requested=False,
        response_routing_key=None,
    )
    await session.send_envelope(envelope, routing_key=session.manager_id, label="no-response-requested")
    no_message = await session.expect_no_matching(
        predicate=lambda env: env.msg_meta and env.msg_meta.msg_correlation_id == corr,
        timeout=10,
    )
    if not no_message:
        raise AssertionError("Observed a correlated reply even though no response was requested.")

    return (
        "No correlated reply arrived on the user interaction queue when the request omitted both response routing and response intent.",
        {
            "correlation_id": corr,
        },
    )


async def scenario_16(session: ReliabilitySession) -> tuple[str, dict]:
    missing_path = session.result_dir / "does_not_exist.txt"
    prompt = (
        f"Use the shell tool to read the file `{missing_path}` even though it does not exist. "
        "Handle the failure cleanly, do not retry forever, and return a concise error report."
    )
    envelope, corr = session.build_task_envelope(
        prompt,
        routing_key=session.manager_id,
        to_named_agent_type="TeamArchitect",
        response_routing_key=session.user_queue_name,
    )
    await session.send_envelope(envelope, routing_key=session.manager_id, label="tool-failure-containment")
    events = await session.receive_matching(
        predicate=lambda env: env.msg_meta and env.msg_meta.msg_correlation_id == corr and isinstance(env.msg_content, Task),
        expected_count=1,
        timeout=session.timeout,
    )
    if len(events) != 1:
        raise AssertionError("Did not receive the tool-failure response.")
    answer = str(events[0].envelope.msg_content.answer)
    if "not exist" not in answer.lower() and "no such file" not in answer.lower() and "error" not in answer.lower():
        raise AssertionError(f"Final answer did not clearly report the missing-file failure: {answer}")

    return (
        "The manager handled a shell-tool missing-file failure without stalling and returned a clean error summary.",
        {
            "missing_path": str(missing_path),
            "answer_excerpt": answer[:240],
        },
    )


async def scenario_52(session: ReliabilitySession) -> tuple[str, dict]:
    prompt = (
        "Use the shell tool exactly once with this command: "
        "`printf ua-shell-live-stdout; printf ua-shell-live-stderr >&2`. "
        "Then report the exact stdout marker, exact stderr marker, exit code, and RTK mode shown by the tool."
    )
    envelope, corr = session.build_task_envelope(
        prompt,
        routing_key=session.manager_id,
        to_named_agent_type="TeamArchitect",
        response_routing_key=session.user_queue_name,
    )
    await session.send_envelope(envelope, routing_key=session.manager_id, label="shell-execution-boundary")
    events = await session.receive_matching(
        predicate=lambda env: env.msg_meta and env.msg_meta.msg_correlation_id == corr and isinstance(env.msg_content, Task),
        expected_count=1,
        timeout=session.timeout,
    )
    if len(events) != 1:
        raise AssertionError("Did not receive the shell execution boundary response.")
    answer = str(events[0].envelope.msg_content.answer or "")
    lower_answer = answer.lower()
    for expected in ("ua-shell-live-stdout", "ua-shell-live-stderr", "proxy"):
        if expected not in lower_answer:
            raise AssertionError(f"Shell execution response omitted {expected!r}: {answer}")
    if "exit code" not in lower_answer and "exit_code" not in lower_answer:
        raise AssertionError(f"Shell execution response omitted the exit code: {answer}")

    return (
        "The manager executed a real non-interactive shell command and reported separated stdout/stderr plus RTK mode.",
        {
            "answer_excerpt": answer[:400],
            "rtk_mode": "proxy",
            "expected_exit_code": 0,
        },
    )


async def scenario_17(session: ReliabilitySession) -> tuple[str, dict]:
    baseline_ids = {row["agent_id"] for row in get_agent_rows(session.db_path, session.team_id, "CodeWritingAgent")}
    first_agent = (
        await instantiate_new_agents(
            session,
            "CodeWritingAgent",
            1,
            baseline_ids,
            storage_target=session.db_path,
        )
    )[0]
    await session.send_envelope(
        session.build_control_envelope(first_agent["agent_id"], "exit"),
        routing_key=first_agent["agent_id"],
        label="exit-first-agent",
    )
    await wait_for_condition(
        f"agent {first_agent['agent_id']} exiting status",
        lambda: any(
            row["agent_id"] == first_agent["agent_id"] and row["is_exiting"]
            for row in get_agent_rows(session.db_path, session.team_id, "CodeWritingAgent")
        ),
        timeout=20,
    )
    second_agent = (
        await instantiate_new_agents(
            session,
            "CodeWritingAgent",
            1,
            baseline_ids | {first_agent["agent_id"]},
            storage_target=session.db_path,
        )
    )[0]
    status_request, corr = session.build_status_request_envelope(RoutingType.ALL_AGENTS_ROUTE.value)
    await session.send_operational_envelope(status_request, label="operational-status-request")
    events = await session.collect_matching_for_duration(
        predicate=lambda env: env.msg_meta and env.msg_meta.msg_correlation_id == corr and isinstance(env.msg_content, AgentStatus),
        duration=8,
    )
    await send_exit_to_agents(session, [second_agent["agent_id"]])
    responders = [event.envelope.msg_content.self_id for event in events]
    if second_agent["agent_id"] not in responders:
        raise AssertionError("Freshly started specialist did not respond to the operational status request.")
    if first_agent["agent_id"] in responders:
        raise AssertionError("Exited specialist still responded after restart.")

    return (
        "After one specialist exited and another of the same type was started, operational discovery returned the fresh agent and not the exited one.",
        {
            "exited_agent_id": first_agent["agent_id"],
            "fresh_agent_id": second_agent["agent_id"],
            "responders": responders,
        },
    )


async def scenario_18(session: ReliabilitySession) -> tuple[str, dict]:
    large_file = session.result_dir / "large_context.txt"
    token = "LARGE_CONTEXT_SENTINEL"
    large_text = "\n".join([f"{token} line {i}" for i in range(6000)])
    large_file.write_text(large_text, encoding="utf-8")
    prompt_one = (
        f"Use file_read to inspect the file `{large_file}` and tell me the repeated token used throughout it. "
        "If the file_read result is too large for the active context, recover with targeted shell commands such as "
        "head, grep, or sed instead of reading the whole file again. A short answer is enough."
    )
    envelope_one, corr_one = session.build_task_envelope(
        prompt_one,
        routing_key=session.manager_id,
        to_named_agent_type="TeamArchitect",
        response_routing_key=session.user_queue_name,
    )
    await session.send_envelope(envelope_one, routing_key=session.manager_id, label="large-context-step-1")
    first = await session.receive_matching(
        predicate=lambda env: env.msg_meta and env.msg_meta.msg_correlation_id == corr_one and isinstance(env.msg_content, Task),
        expected_count=1,
        timeout=max(session.timeout, 120),
    )
    if len(first) != 1:
        raise AssertionError("Did not receive the first large-context response.")

    prompt_two = "What is 2 + 2? Reply with just the answer. Do not mention LARGE_CONTEXT_SENTINEL."
    envelope_two, corr_two = session.build_task_envelope(
        prompt_two,
        routing_key=session.manager_id,
        to_named_agent_type="TeamArchitect",
        response_routing_key=session.user_queue_name,
    )
    await session.send_envelope(envelope_two, routing_key=session.manager_id, label="large-context-step-2")
    second = await session.receive_matching(
        predicate=lambda env: env.msg_meta and env.msg_meta.msg_correlation_id == corr_two and isinstance(env.msg_content, Task),
        expected_count=1,
        timeout=session.timeout,
    )
    if len(second) != 1:
        raise AssertionError("Did not receive the second post-large-context response.")
    answer_text = str(second[0].envelope.msg_content.answer)
    if "4" not in answer_text:
        raise AssertionError(f"Second answer did not contain the expected result: {answer_text}")
    if token in answer_text:
        raise AssertionError("Second answer was contaminated by the previous large-context token.")

    return (
        "A large-context task completed, and the immediately following simple request returned a clean answer without contamination from the previous turn.",
        {
            "large_file": str(large_file),
            "step_one_task_id": first[0].envelope.msg_content.task_id,
            "step_two_task_id": second[0].envelope.msg_content.task_id,
            "step_two_answer": answer_text,
        },
    )


async def scenario_19(session: ReliabilitySession) -> tuple[str, dict]:
    token = f"thread-token-{new_id_suffix()}"
    prompt_one = (
        f"Remember this exact token for a follow-up question later: `{token}`. "
        "For this turn, just confirm that you stored it."
    )
    envelope_one, corr_one = session.build_task_envelope(
        prompt_one,
        routing_key=session.manager_id,
        to_named_agent_type="TeamArchitect",
        response_routing_key=session.user_queue_name,
    )
    await session.send_envelope(envelope_one, routing_key=session.manager_id, label="conversation-thread-step-1")
    first = await session.receive_matching(
        predicate=lambda env: env.msg_meta and env.msg_meta.msg_correlation_id == corr_one and isinstance(env.msg_content, Task),
        expected_count=1,
        timeout=session.timeout,
    )
    if len(first) != 1:
        raise AssertionError("Did not receive the initial conversation-thread response.")

    first_env = first[0].envelope
    conversation_id = getattr(first_env.msg_meta, "conversation_id", None)
    first_task_id = first_env.msg_content.task_id
    if not conversation_id:
        raise AssertionError("Initial response did not include a conversation_id.")

    prompt_two = "What exact token did I ask you to remember earlier in this conversation? Reply with just the token."
    envelope_two, corr_two = session.build_task_envelope(
        prompt_two,
        routing_key=session.manager_id,
        to_named_agent_type="TeamArchitect",
        response_routing_key=session.user_queue_name,
        conversation_id=conversation_id,
        followup_to_task_id=first_task_id,
    )
    await session.send_envelope(envelope_two, routing_key=session.manager_id, label="conversation-thread-step-2")
    second = await session.receive_matching(
        predicate=lambda env: env.msg_meta and env.msg_meta.msg_correlation_id == corr_two and isinstance(env.msg_content, Task),
        expected_count=1,
        timeout=session.timeout,
    )
    if len(second) != 1:
        raise AssertionError("Did not receive the follow-up conversation-thread response.")

    second_env = second[0].envelope
    answer_text = str(second_env.msg_content.answer or "")
    if token not in answer_text:
        raise AssertionError(f"Follow-up reply did not recover the earlier token from thread context: {answer_text}")
    if second_env.msg_meta.conversation_id != conversation_id:
        raise AssertionError("Follow-up response did not preserve the conversation_id.")

    thread = get_conversation_thread_row(session.db_path, conversation_id)
    turns = get_conversation_turn_rows(session.db_path, conversation_id)
    if thread is None:
        raise AssertionError("Conversation thread row was not persisted.")
    if len(turns) < 2:
        raise AssertionError(f"Expected at least 2 persisted conversation turns, found {len(turns)}.")
    if turns[1]["followup_to_task_id"] != first_task_id:
        raise AssertionError("Second turn was not linked to the first task in the conversation store.")

    return (
        "A follow-up request reused the returned conversation_id, resolved against the prior top-level turn, and the manager answered using earlier thread context.",
        {
            "conversation_id": conversation_id,
            "first_task_id": first_task_id,
            "second_task_id": second_env.msg_content.task_id,
            "turn_count": len(turns),
            "last_token_estimate": thread["last_token_estimate"],
        },
    )


async def scenario_20(session: ReliabilitySession) -> tuple[str, dict]:
    token = f"artifact-token-{new_id_suffix()}"
    large_file = session.result_dir / "conversation_artifact_source.txt"
    large_text = "\n".join([f"{token} line {i}" for i in range(900)])
    large_file.write_text(large_text, encoding="utf-8")

    prompt_one = (
        f"First call file_read on the full file `{large_file}` and tell me the repeated token used throughout it. "
        "Do not use shell, head, grep, sed, or any other file-inspection tool before that first file_read attempt. "
        "If the file_read result is too large for the active context, then recover with one bounded shell command like "
        f"`grep -m 1 -Eo 'artifact-token-[0-9a-f]+' {large_file}` against the original file or a similarly narrow "
        "grep against the saved tool-result artifact. Do not read the whole artifact back into context. Reply with the "
        "exact artifact-token value."
    )
    envelope_one, corr_one = session.build_task_envelope(
        prompt_one,
        routing_key=session.manager_id,
        to_named_agent_type="TeamArchitect",
        response_routing_key=session.user_queue_name,
    )
    await session.send_envelope(envelope_one, routing_key=session.manager_id, label="conversation-artifact-step-1")
    first = await session.receive_matching(
        predicate=lambda env: env.msg_meta and env.msg_meta.msg_correlation_id == corr_one and isinstance(env.msg_content, Task),
        expected_count=1,
        timeout=max(session.timeout, 120),
    )
    if len(first) != 1:
        raise AssertionError("Did not receive the first artifact-offload response.")

    first_env = first[0].envelope
    conversation_id = getattr(first_env.msg_meta, "conversation_id", None)
    first_task_id = first_env.msg_content.task_id
    if not conversation_id:
        raise AssertionError("Artifact-offload response did not include a conversation_id.")

    answer_one = str(first_env.msg_content.answer or "")
    if token not in answer_one:
        raise AssertionError(f"Initial large-output response did not recover the repeated token: {answer_one}")

    artifact_root = session.result_dir / "manager_workspace" / "artifacts" / "tool_results"
    await wait_for_condition(
        f"tool-result artifacts under {artifact_root}",
        lambda: artifact_root.exists() and any(artifact_root.rglob("*.json")),
        timeout=30,
    )
    artifact_paths = [path for path in artifact_root.rglob("*.json") if not path.name.endswith(".manifest.json")]
    if not artifact_paths:
        raise AssertionError("No tool-result artifact files were persisted for the oversized tool result.")
    missing_paths = [str(path) for path in artifact_paths if not path.exists()]
    if missing_paths:
        raise AssertionError(f"Tool-result artifact files were missing on disk: {missing_paths}")

    prompt_two = "In this same conversation, what repeated token did you inspect earlier? Reply with just the token."
    envelope_two, corr_two = session.build_task_envelope(
        prompt_two,
        routing_key=session.manager_id,
        to_named_agent_type="TeamArchitect",
        response_routing_key=session.user_queue_name,
        conversation_id=conversation_id,
        followup_to_task_id=first_task_id,
    )
    await session.send_envelope(envelope_two, routing_key=session.manager_id, label="conversation-artifact-step-2")
    second = await session.receive_matching(
        predicate=lambda env: env.msg_meta and env.msg_meta.msg_correlation_id == corr_two and isinstance(env.msg_content, Task),
        expected_count=1,
        timeout=session.timeout,
    )
    if len(second) != 1:
        raise AssertionError("Did not receive the follow-up artifact-offload response.")

    second_env = second[0].envelope
    answer_text = str(second_env.msg_content.answer or "")
    if token not in answer_text:
        raise AssertionError(f"Follow-up reply did not preserve the earlier large-output fact: {answer_text}")

    turns = get_conversation_turn_rows(session.db_path, conversation_id)
    if not any(turn["task_id"] == first_task_id for turn in turns):
        raise AssertionError("The first conversation turn was not persisted.")

    return (
        "A large-output tool result was persisted as a tool-result artifact, and a later follow-up still recovered the key fact from the same conversation thread.",
        {
            "conversation_id": conversation_id,
            "first_task_id": first_task_id,
            "second_task_id": second_env.msg_content.task_id,
            "artifact_count": len(artifact_paths),
            "artifact_paths": [str(path) for path in artifact_paths],
        },
    )


async def scenario_21(session: ReliabilitySession) -> tuple[str, dict]:
    dense = await _build_dense_live_conversation(
        session,
        prefix="compaction_replay",
        turn_count=4,
        repeated_lines=220,
    )
    compaction_outputs = _write_compaction_views(
        result_dir=session.result_dir,
        source_thread=dense["thread"],
        source_turns=dense["turns"],
        source_artifacts=dense["artifacts"],
        latest_request_text=dense["latest_request_text"],
        compactor_types=["noop", "heuristic_sliding_window"],
    )

    return (
        "Built a dense live conversation thread and wrote the full raw context plus replayed noop and heuristic compaction views to disk for inspection.",
        {
            "conversation_id": dense["conversation_id"],
            "token": dense["token"],
            "persisted_thread": dense["thread"],
            "turn_count": len(dense["turns"]),
            "compaction_outputs": compaction_outputs,
        },
    )


async def scenario_22(session: ReliabilitySession) -> tuple[str, dict]:
    dense = await _build_dense_live_conversation(
        session,
        prefix="llm_compaction_replay",
        turn_count=4,
        repeated_lines=220,
    )
    compaction_outputs = _write_compaction_views(
        result_dir=session.result_dir,
        source_thread=dense["thread"],
        source_turns=dense["turns"],
        source_artifacts=dense["artifacts"],
        latest_request_text=dense["latest_request_text"],
        compactor_types=["heuristic_sliding_window", "llm_summarizing"],
    )

    llm_view = next(
        (view for view in compaction_outputs["views"] if view["compactor_type"] == "llm_summarizing"),
        None,
    )
    if llm_view is None:
        raise AssertionError("LLM compaction replay did not produce an llm_summarizing view.")
    llm_thread = json.loads(Path(llm_view["thread_path"]).read_text(encoding="utf-8"))
    if llm_thread.get("compactor_type") != "llm_summarizing":
        raise AssertionError("LLM compaction replay thread did not record llm_summarizing as the compactor_type.")

    return (
        "Built a dense live conversation thread and replayed heuristic plus LLM-backed compaction, writing the full raw context and both compacted prompt views to disk.",
        {
            "conversation_id": dense["conversation_id"],
            "token": dense["token"],
            "persisted_thread": dense["thread"],
            "turn_count": len(dense["turns"]),
            "compaction_outputs": compaction_outputs,
            "llm_thread": llm_thread,
        },
    )


async def scenario_23(session: ReliabilitySession) -> tuple[str, dict]:
    dense = await _build_dense_live_conversation(
        session,
        prefix="all_compactors_replay",
        turn_count=4,
        repeated_lines=220,
    )
    compaction_outputs = _write_compaction_views(
        result_dir=session.result_dir,
        source_thread=dense["thread"],
        source_turns=dense["turns"],
        source_artifacts=dense["artifacts"],
        latest_request_text=dense["latest_request_text"],
        compactor_types=["noop", "heuristic_sliding_window", "llm_summarizing"],
    )

    llm_view = next(
        (view for view in compaction_outputs["views"] if view["compactor_type"] == "llm_summarizing"),
        None,
    )
    if llm_view is None:
        raise AssertionError("Combined compaction replay did not produce an llm_summarizing view.")

    return (
        "Built a dense live conversation thread and wrote the full conversation, raw prompt context, and side-by-side noop, heuristic, and llm-backed compaction outputs to disk.",
        {
            "conversation_id": dense["conversation_id"],
            "token": dense["token"],
            "persisted_thread": dense["thread"],
            "turn_count": len(dense["turns"]),
            "compaction_outputs": compaction_outputs,
        },
    )


async def scenario_24(session: ReliabilitySession) -> tuple[str, dict]:
    assert_no_preexisting_specialists(
        session.db_path,
        team_id=session.team_id,
        agent_type="WebResearchAgent",
    )

    first_worker = await session.launch_agent_process(
        agent_type="WebResearchAgent",
        agent_id=f"webresearch-crash-{new_id_suffix()}",
        agent_result_transformer="tests.live_result_transformers:crash_first_task_before_reply",
    )
    replacement_worker: Optional[ManagedAgentProcess] = None

    try:
        first_worker_row = await wait_for_agent_row(
            session.db_path,
            team_id=session.team_id,
            agent_type="WebResearchAgent",
            agent_id=first_worker.agent_id,
            timeout=45,
        )

        prompt = _build_manager_web_delegate_prompt(session.result_dir, "a simple pancake recipe")
        envelope, corr = session.build_task_envelope(
            prompt,
            routing_key=session.manager_id,
            to_named_agent_type="TeamArchitect",
            response_routing_key=session.user_queue_name,
        )
        await session.send_envelope(envelope, routing_key=session.manager_id, label="worker-crash-takeover")

        crash_returncode = await first_worker.wait_for_exit(timeout=max(session.timeout, 120))
        if crash_returncode == 0:
            raise AssertionError("Crash-test worker exited cleanly instead of failing mid-task.")

        await wait_for_condition(
            f"runtime snapshot for crashed worker {first_worker.agent_id}",
            lambda: bool(
                get_active_runtime_rows(
                    session.db_path,
                    team_id=session.team_id,
                    owner_named_agent_type="WebResearchAgent",
                    owner_agent_id=first_worker.agent_id,
                )
            ),
            timeout=30,
        )
        crashed_runtime_rows = get_active_runtime_rows(
            session.db_path,
            team_id=session.team_id,
            owner_named_agent_type="WebResearchAgent",
            owner_agent_id=first_worker.agent_id,
        )

        replacement_worker = await session.launch_agent_process(
            agent_type="WebResearchAgent",
            agent_id=f"webresearch-restart-{new_id_suffix()}",
        )
        replacement_worker_row = await wait_for_agent_row(
            session.db_path,
            team_id=session.team_id,
            agent_type="WebResearchAgent",
            agent_id=replacement_worker.agent_id,
            timeout=45,
        )

        events = await session.receive_matching(
            predicate=lambda env: env.msg_meta and env.msg_meta.msg_correlation_id == corr and isinstance(env.msg_content, Task),
            expected_count=1,
            timeout=max(session.timeout, 180),
        )
        if len(events) != 1:
            raise AssertionError("Did not receive the recovered manager reply after worker takeover.")

        response = events[0].envelope
        answer_text = str(response.msg_content.answer or "")
        if "ingredient" not in answer_text.lower() and "pancake" not in answer_text.lower():
            raise AssertionError(f"Recovered response did not look like a recipe summary: {answer_text}")

        replacement_archives = get_archived_task_rows(
            session.db_path,
            owner_agent_id=replacement_worker.agent_id,
            team_id=session.team_id,
        )
        resumed_subtasks = [
            row
            for row in replacement_archives
            if row["task_data"].get("parent_task_id")
            and "pancake recipe" in str(row["task_data"].get("problem_statement", "")).lower()
        ]
        if not resumed_subtasks:
            raise AssertionError(
                "Replacement WebResearchAgent did not archive any resumed delegated subtask for the recipe request."
            )

        return (
            "A WebResearchAgent crashed after finishing its delegated work but before replying, and a restarted WebResearchAgent with a different agent_id restored the persisted task and returned the result back through the manager.",
            {
                "correlation_id": corr,
                "manager_id": session.manager_id,
                "initial_worker": {
                    **first_worker.snapshot(),
                    "agent_row": first_worker_row,
                    "crash_returncode": crash_returncode,
                },
                "replacement_worker": {
                    **replacement_worker.snapshot(),
                    "agent_row": replacement_worker_row,
                },
                "crashed_runtime_rows": crashed_runtime_rows,
                "response_task_id": response.msg_content.task_id,
                "response_answer_excerpt": answer_text[:300],
                "replacement_archived_subtask_ids": [row["task_id"] for row in resumed_subtasks],
            },
        )
    finally:
        cleanup = await stop_managed_agents(
            session,
            [proc for proc in [first_worker, replacement_worker] if proc is not None],
        )
        live_nats_client.write_json_file(session.result_dir / "scenario_24_cleanup.json", cleanup)


async def scenario_25(session: ReliabilitySession) -> tuple[str, dict]:
    assert_no_preexisting_specialists(
        session.db_path,
        team_id=session.team_id,
        agent_type="WebResearchAgent",
    )

    first_worker = await session.launch_agent_process(
        agent_type="WebResearchAgent",
        agent_id=f"webresearch-queue-crash-{new_id_suffix()}",
        agent_result_transformer="tests.live_result_transformers:crash_first_task_before_reply",
    )
    replacement_worker: Optional[ManagedAgentProcess] = None

    try:
        await wait_for_agent_row(
            session.db_path,
            team_id=session.team_id,
            agent_type="WebResearchAgent",
            agent_id=first_worker.agent_id,
            timeout=45,
        )

        waiting_prompt = _build_manager_web_delegate_prompt(session.result_dir, "a quick tomato soup recipe")
        waiting_envelope, waiting_corr = session.build_task_envelope(
            waiting_prompt,
            routing_key=session.manager_id,
            to_named_agent_type="TeamArchitect",
            response_routing_key=session.user_queue_name,
        )
        await session.send_envelope(waiting_envelope, routing_key=session.manager_id, label="waiting-task-before-crash")

        crash_returncode = await first_worker.wait_for_exit(timeout=max(session.timeout, 120))
        if crash_returncode == 0:
            raise AssertionError("Queue-advancement worker exited cleanly instead of failing mid-task.")

        await wait_for_condition(
            f"persisted waiting runtime for crashed worker {first_worker.agent_id}",
            lambda: bool(
                get_active_runtime_rows(
                    session.db_path,
                    team_id=session.team_id,
                    owner_named_agent_type="WebResearchAgent",
                    owner_agent_id=first_worker.agent_id,
                )
            ),
            timeout=30,
        )

        fast_prompt = "Reply with exactly `queue advancement ok`. Do not delegate and do not use tools."
        fast_envelope, fast_corr = session.build_task_envelope(
            fast_prompt,
            routing_key=session.manager_id,
            to_named_agent_type="TeamArchitect",
            response_routing_key=session.user_queue_name,
        )
        await session.send_envelope(fast_envelope, routing_key=session.manager_id, label="queue-advance-fast-task")

        fast_events = await session.receive_matching(
            predicate=lambda env: env.msg_meta and env.msg_meta.msg_correlation_id == fast_corr and isinstance(env.msg_content, Task),
            expected_count=1,
            timeout=max(session.timeout, 120),
        )
        if len(fast_events) != 1:
            raise AssertionError("Did not receive the fast follow-on task while the delegated task was waiting.")

        fast_response = fast_events[0].envelope
        fast_answer = str(fast_response.msg_content.answer or "").strip().lower()
        if "queue advancement ok" not in fast_answer:
            raise AssertionError(f"Unexpected fast-task answer while the first task was waiting: {fast_answer}")

        replacement_worker = await session.launch_agent_process(
            agent_type="WebResearchAgent",
            agent_id=f"webresearch-queue-restart-{new_id_suffix()}",
        )
        replacement_row = await wait_for_agent_row(
            session.db_path,
            team_id=session.team_id,
            agent_type="WebResearchAgent",
            agent_id=replacement_worker.agent_id,
            timeout=45,
        )

        waiting_events = await session.receive_matching(
            predicate=lambda env: env.msg_meta and env.msg_meta.msg_correlation_id == waiting_corr and isinstance(env.msg_content, Task),
            expected_count=1,
            timeout=max(session.timeout, 180),
        )
        if len(waiting_events) != 1:
            raise AssertionError("Did not receive the original waiting task after replacement worker takeover.")

        waiting_response = waiting_events[0].envelope
        waiting_answer = str(waiting_response.msg_content.answer or "")
        if "tomato" not in waiting_answer.lower() and "soup" not in waiting_answer.lower():
            raise AssertionError(f"Recovered waiting-task answer did not match the delegated request: {waiting_answer}")

        fast_received_at = datetime.fromisoformat(fast_events[0].received_at)
        waiting_received_at = datetime.fromisoformat(waiting_events[0].received_at)
        if fast_received_at >= waiting_received_at:
            raise AssertionError("The queued fast task did not complete before the resumed waiting task.")

        await wait_for_condition(
            "recovered waiting parent task archived row",
            lambda: get_archived_task_row(
                session.db_path,
                waiting_response.msg_content.task_id,
                owner_agent_id=waiting_response.msg_header.from_agent_id,
            )
            is not None,
            timeout=15,
        )
        waiting_parent = get_archived_task_row(
            session.db_path,
            waiting_response.msg_content.task_id,
            owner_agent_id=waiting_response.msg_header.from_agent_id,
        )
        if waiting_parent is None:
            raise AssertionError("Waiting parent task was not archived after recovery.")
        waiting_history = waiting_parent["task_data"].get("execution_history") or []
        if not any("subtask" in str(entry.get("summary", "")).lower() for entry in waiting_history):
            raise AssertionError("Recovered waiting task history did not capture delegated subtask activity.")

        return (
            "While a delegated recipe task was waiting on a crashed worker, the manager accepted and completed a later queued request, then resumed the original task after a replacement worker took over the persisted work.",
            {
                "manager_id": session.manager_id,
                "initial_worker": {
                    **first_worker.snapshot(),
                    "crash_returncode": crash_returncode,
                },
                "replacement_worker": {
                    **replacement_worker.snapshot(),
                    "agent_row": replacement_row,
                },
                "fast_task": {
                    "correlation_id": fast_corr,
                    "task_id": fast_response.msg_content.task_id,
                    "received_at": fast_events[0].received_at,
                    "answer": str(fast_response.msg_content.answer or ""),
                },
                "waiting_task": {
                    "correlation_id": waiting_corr,
                    "task_id": waiting_response.msg_content.task_id,
                    "received_at": waiting_events[0].received_at,
                    "answer_excerpt": waiting_answer[:300],
                },
                "waiting_history_entries": len(waiting_history),
            },
        )
    finally:
        cleanup = await stop_managed_agents(
            session,
            [proc for proc in [first_worker, replacement_worker] if proc is not None],
        )
        live_nats_client.write_json_file(session.result_dir / "scenario_25_cleanup.json", cleanup)


async def scenario_26(session: ReliabilitySession) -> tuple[str, dict]:
    assert_no_preexisting_specialists(
        session.db_path,
        team_id=session.team_id,
        agent_type="WebResearchAgent",
    )

    worker = await session.launch_agent_process(
        agent_type="WebResearchAgent",
        agent_id=f"webresearch-timeout-{new_id_suffix()}",
        agent_result_transformer="tests.live_result_transformers:crash_first_task_before_reply",
    )
    try:
        await wait_for_agent_row(
            session.db_path,
            team_id=session.team_id,
            agent_type="WebResearchAgent",
            agent_id=worker.agent_id,
            timeout=45,
        )

        waiting_prompt = (
            _build_manager_web_delegate_prompt(session.result_dir, "an easy scrambled egg recipe")
            + " If the delegated WebResearchAgent does not respond or the delegated subtask times out, "
            "do not retry and do not delegate again; send the final Task response explaining that the "
            "delegated WebResearchAgent subtask timed out."
        )
        waiting_envelope, waiting_corr = session.build_task_envelope(
            waiting_prompt,
            routing_key=session.manager_id,
            to_named_agent_type="TeamArchitect",
            response_routing_key=session.user_queue_name,
        )
        await session.send_envelope(waiting_envelope, routing_key=session.manager_id, label="worker-timeout-primary")

        crash_returncode = await worker.wait_for_exit(timeout=max(session.timeout, 120))
        if crash_returncode == 0:
            raise AssertionError("Timeout-test worker exited cleanly instead of crashing.")

        fast_prompt = "Reply with exactly `watchdog queue ok`. Do not delegate and do not ask a question."
        fast_envelope, fast_corr = session.build_task_envelope(
            fast_prompt,
            routing_key=session.manager_id,
            to_named_agent_type="TeamArchitect",
            response_routing_key=session.user_queue_name,
        )
        await session.send_envelope(fast_envelope, routing_key=session.manager_id, label="worker-timeout-fast-followup")

        fast_events = await session.receive_matching(
            predicate=lambda env: env.msg_meta and env.msg_meta.msg_correlation_id == fast_corr and isinstance(env.msg_content, Task),
            expected_count=1,
            timeout=max(session.timeout, 90),
        )
        if len(fast_events) != 1:
            raise AssertionError("Manager did not continue queued work while the crashed subtask was waiting.")

        failure_events = await session.receive_matching(
            predicate=lambda env: env.msg_meta and env.msg_meta.msg_correlation_id == waiting_corr and isinstance(env.msg_content, Task),
            expected_count=1,
            timeout=max(session.timeout, 90),
        )
        if len(failure_events) != 1:
            raise AssertionError("Did not receive the watchdog failure response after worker crash with no restart.")

        fast_answer = str(fast_events[0].envelope.msg_content.answer or "").lower()
        if "watchdog queue ok" not in fast_answer:
            raise AssertionError(f"Unexpected fast follow-on answer while waiting task was blocked: {fast_answer}")

        failure_response = failure_events[0].envelope
        failure_answer = str(failure_response.msg_content.answer or "")
        failure_errors = failure_response.msg_content.errors or []
        if "timed out" not in failure_answer.lower() and not any("timed out" in err.lower() for err in failure_errors):
            raise AssertionError(f"Watchdog failure response did not mention timeout: {failure_answer}")

        await wait_for_condition(
            "timed-out parent task archived row",
            lambda: get_archived_task_row(
                session.db_path,
                failure_response.msg_content.task_id,
                owner_agent_id=failure_response.msg_header.from_agent_id,
            )
            is not None,
            timeout=15,
        )
        failed_parent = get_archived_task_row(
            session.db_path,
            failure_response.msg_content.task_id,
            owner_agent_id=failure_response.msg_header.from_agent_id,
        )
        if failed_parent is None:
            raise AssertionError("Timed-out parent task was not archived.")
        history = failed_parent["task_data"].get("execution_history") or []
        if not any("timed out" in str(entry.get("summary", "")).lower() for entry in history):
            raise AssertionError("Archived timed-out parent task history did not record the timeout.")

        return (
            "After a delegated worker crashed and never returned, the manager kept draining queued work and then cleanly failed the blocked task when the subtask watchdog timeout elapsed.",
            {
                "manager_id": session.manager_id,
                "worker": {
                    **worker.snapshot(),
                    "crash_returncode": crash_returncode,
                },
                "fast_task_id": fast_events[0].envelope.msg_content.task_id,
                "timed_out_task_id": failure_response.msg_content.task_id,
                "timed_out_errors": failure_errors,
                "timed_out_history_entries": len(history),
            },
        )
    finally:
        cleanup = await stop_managed_agents(session, [worker])
        live_nats_client.write_json_file(session.result_dir / "scenario_26_cleanup.json", cleanup)


async def scenario_27(session: ReliabilitySession) -> tuple[str, dict]:
    clarification_prompt = (
        "Before doing any work, ask exactly one clarifying question as a TaskQuery and then wait for my answer. "
        "Do not solve the request yet. The request is: prepare a practical plan for building a small internal dashboard."
    )
    clarification_envelope, clarification_corr = session.build_task_envelope(
        clarification_prompt,
        routing_key=session.manager_id,
        to_named_agent_type="TeamArchitect",
        response_routing_key=session.user_queue_name,
    )
    await session.send_envelope(clarification_envelope, routing_key=session.manager_id, label="taskquery-timeout-primary")

    query_events = await session.receive_matching(
        predicate=lambda env: env.msg_meta and env.msg_meta.msg_correlation_id == clarification_corr and isinstance(env.msg_content, TaskQuery),
        expected_count=1,
        timeout=max(session.timeout, 90),
    )
    if len(query_events) != 1:
        raise AssertionError("Did not receive the expected TaskQuery clarification request.")

    fast_prompt = "Reply with exactly `taskquery timeout queue ok`. Do not ask a question."
    fast_envelope, fast_corr = session.build_task_envelope(
        fast_prompt,
        routing_key=session.manager_id,
        to_named_agent_type="TeamArchitect",
        response_routing_key=session.user_queue_name,
    )
    await session.send_envelope(fast_envelope, routing_key=session.manager_id, label="taskquery-timeout-fast-followup")

    fast_events = await session.receive_matching(
        predicate=lambda env: env.msg_meta and env.msg_meta.msg_correlation_id == fast_corr and isinstance(env.msg_content, Task),
        expected_count=1,
        timeout=max(session.timeout, 90),
    )
    if len(fast_events) != 1:
        raise AssertionError("Manager did not continue queued work while waiting on a TaskQuery response.")

    timeout_events = await session.receive_matching(
        predicate=lambda env: env.msg_meta and env.msg_meta.msg_correlation_id == clarification_corr and isinstance(env.msg_content, Task),
        expected_count=1,
        timeout=max(session.timeout, 90),
    )
    if len(timeout_events) != 1:
        raise AssertionError("Did not receive the timeout failure response for the unanswered TaskQuery.")

    timeout_response = timeout_events[0].envelope
    timeout_answer = str(timeout_response.msg_content.answer or "")
    timeout_errors = timeout_response.msg_content.errors or []
    if "taskquery" not in timeout_answer.lower() and not any("taskquery" in err.lower() for err in timeout_errors):
        raise AssertionError(f"TaskQuery timeout response did not clearly mention the TaskQuery wait: {timeout_answer}")

    await wait_for_condition(
        "timed-out TaskQuery parent task archived row",
        lambda: get_archived_task_row(
            session.db_path,
            timeout_response.msg_content.task_id,
            owner_agent_id=timeout_response.msg_header.from_agent_id,
        )
        is not None,
        timeout=15,
    )
    archived = get_archived_task_row(
        session.db_path,
        timeout_response.msg_content.task_id,
        owner_agent_id=timeout_response.msg_header.from_agent_id,
    )
    if archived is None:
        raise AssertionError("Timed-out TaskQuery parent task was not archived.")
    history = archived["task_data"].get("execution_history") or []
    if not any("taskquery" in str(entry.get("summary", "")).lower() and "timed out" in str(entry.get("summary", "")).lower() for entry in history):
        raise AssertionError("Archived history did not show the unanswered TaskQuery timeout.")

    return (
        "An unanswered TaskQuery left the original task waiting while a later queued task completed, and the watchdog then failed the blocked task with a timeout response instead of leaving it stuck.",
        {
            "manager_id": session.manager_id,
            "taskquery_id": query_events[0].envelope.msg_content.task_query_id,
            "fast_task_id": fast_events[0].envelope.msg_content.task_id,
            "timed_out_task_id": timeout_response.msg_content.task_id,
            "timed_out_errors": timeout_errors,
            "timed_out_history_entries": len(history),
        },
    )


async def scenario_28(session: ReliabilitySession) -> tuple[str, dict]:
    assert_no_preexisting_specialists(
        session.db_path,
        team_id=session.team_id,
        agent_type="WebResearchAgent",
    )

    worker = await session.launch_agent_process(
        agent_type="WebResearchAgent",
        agent_id=f"webresearch-manager-recovery-{new_id_suffix()}",
        agent_result_transformer="tests.live_result_transformers:crash_first_task_before_reply",
    )
    replacement_manager: Optional[ManagedAgentProcess] = None
    original_manager_id = session.manager_id

    try:
        await wait_for_agent_row(
            session.db_path,
            team_id=session.team_id,
            agent_type="WebResearchAgent",
            agent_id=worker.agent_id,
            timeout=45,
        )

        waiting_prompt = (
            _build_manager_web_delegate_prompt(session.result_dir, "a weeknight chili recipe")
            + " If the delegated WebResearchAgent does not return or the delegated subtask times out, "
            "do not retry and do not redelegate. Send the final Task response explaining the timeout."
        )
        waiting_envelope, waiting_corr = session.build_task_envelope(
            waiting_prompt,
            routing_key=session.manager_id,
            to_named_agent_type="TeamArchitect",
            response_routing_key=session.user_queue_name,
        )
        fast_prompt = "Reply with exactly `manager takeover queue ok`. Do not delegate and do not ask questions."
        fast_envelope, fast_corr = session.build_task_envelope(
            fast_prompt,
            routing_key=session.manager_id,
            to_named_agent_type="TeamArchitect",
            response_routing_key=session.user_queue_name,
        )
        await session.send_envelope(waiting_envelope, routing_key=session.manager_id, label="manager-takeover-primary")
        await session.send_envelope(fast_envelope, routing_key=session.manager_id, label="manager-takeover-fast-followup")

        crash_returncode = await worker.wait_for_exit(timeout=max(session.timeout, 120))
        if crash_returncode == 0:
            raise AssertionError("Manager-takeover worker exited cleanly instead of crashing.")

        original_manager_rows = get_agent_rows(session.db_path, session.team_id, "TeamArchitect")
        if not any(row["agent_id"] == original_manager_id for row in original_manager_rows):
            raise AssertionError(f"Original manager {original_manager_id} was not present in agent state.")

        original_manager_proc = next(
            (proc for proc in session._managed_processes if proc.agent_id == original_manager_id),
            None,
        )
        if original_manager_proc is None:
            raise AssertionError("Managed original manager process was not tracked by the live session.")

        manager_exit_code = await original_manager_proc.stop(timeout=5)
        if manager_exit_code is None:
            raise AssertionError("Failed to stop the original manager process.")
        await asyncio.sleep(5.0)

        await wait_for_condition(
            f"persisted manager runtime snapshot for {original_manager_id}",
            lambda: bool(
                get_active_runtime_rows(
                    session.db_path,
                    team_id=session.team_id,
                    owner_named_agent_type="TeamArchitect",
                    owner_agent_id=original_manager_id,
                )
            ),
            timeout=30,
        )
        persisted_manager_runtime = get_active_runtime_rows(
            session.db_path,
            team_id=session.team_id,
            owner_named_agent_type="TeamArchitect",
            owner_agent_id=original_manager_id,
        )

        replacement_manager = await session.launch_agent_process(
            agent_type="TeamArchitect",
            agent_id=f"manager-replacement-{new_id_suffix()}",
            is_lead=True,
            config_path=session.auto_manager.get("config_path"),
        )
        session.manager_id = replacement_manager.agent_id
        replacement_row = await wait_for_agent_row(
            session.db_path,
            team_id=session.team_id,
            agent_type="TeamArchitect",
            agent_id=replacement_manager.agent_id,
            timeout=45,
        )

        fast_events = await session.receive_matching(
            predicate=lambda env: env.msg_meta and env.msg_meta.msg_correlation_id == fast_corr and isinstance(env.msg_content, Task),
            expected_count=1,
            timeout=max(session.timeout, 90),
        )
        if len(fast_events) != 1:
            raise AssertionError("Replacement manager did not restore and complete the queued fast task.")

        waiting_events = await session.receive_matching(
            predicate=lambda env: env.msg_meta and env.msg_meta.msg_correlation_id == waiting_corr and isinstance(env.msg_content, Task),
            expected_count=1,
            timeout=max(session.timeout, 90),
        )
        if len(waiting_events) != 1:
            raise AssertionError("Replacement manager did not complete or fail the restored waiting task.")

        fast_answer = str(fast_events[0].envelope.msg_content.answer or "").lower()
        if "manager takeover queue ok" not in fast_answer:
            raise AssertionError(f"Unexpected fast-task answer after manager takeover: {fast_answer}")

        waiting_response = waiting_events[0].envelope
        waiting_text = (
            str(waiting_response.msg_content.answer or "")
            + " "
            + " ".join(waiting_response.msg_content.errors or [])
        ).lower()
        if "timed out" not in waiting_text and "chili" not in waiting_text:
            raise AssertionError(f"Replacement manager returned an unexpected waiting-task result: {waiting_text}")

        fast_received_at = datetime.fromisoformat(fast_events[0].received_at)
        waiting_received_at = datetime.fromisoformat(waiting_events[0].received_at)
        if fast_received_at >= waiting_received_at:
            raise AssertionError("Replacement manager did not resume the queued fast task before the old waiting task resolved.")

        return (
            "A replacement TeamArchitect with a different agent_id restored persisted manager runtime after the original manager died, completed queued follow-on work, and then resolved the older waiting task.",
            {
                "original_manager_id": original_manager_id,
                "original_manager_exit_code": manager_exit_code,
                "replacement_manager": {
                    **replacement_manager.snapshot(),
                    "agent_row": replacement_row,
                },
                "worker": {
                    **worker.snapshot(),
                    "crash_returncode": crash_returncode,
                },
                "persisted_manager_runtime": persisted_manager_runtime,
                "fast_task_id": fast_events[0].envelope.msg_content.task_id,
                "waiting_task_id": waiting_response.msg_content.task_id,
            },
        )
    finally:
        cleanup = await stop_managed_agents(
            session,
            [proc for proc in [worker, replacement_manager] if proc is not None],
        )
        live_nats_client.write_json_file(session.result_dir / "scenario_28_cleanup.json", cleanup)


async def scenario_29(session: ReliabilitySession) -> tuple[str, dict]:
    assert_no_preexisting_specialists(
        session.db_path,
        team_id=session.team_id,
        agent_type="WebResearchAgent",
    )

    crashed_worker = await session.launch_agent_process(
        agent_type="WebResearchAgent",
        agent_id=f"webresearch-single-claim-{new_id_suffix()}",
        agent_result_transformer="tests.live_result_transformers:crash_first_task_before_reply",
    )
    replacement_one: Optional[ManagedAgentProcess] = None
    replacement_two: Optional[ManagedAgentProcess] = None

    try:
        await wait_for_agent_row(
            session.db_path,
            team_id=session.team_id,
            agent_type="WebResearchAgent",
            agent_id=crashed_worker.agent_id,
            timeout=45,
        )

        prompt = _build_direct_worker_web_prompt("a simple banana bread recipe", session.result_dir)
        envelope, corr = session.build_task_envelope(
            prompt,
            routing_key=f"{session.team_id}_WebResearchAgent",
            to_named_agent_type="WebResearchAgent",
            response_routing_key=session.user_queue_name,
        )
        await session.send_envelope(envelope, routing_key=f"{session.team_id}_WebResearchAgent", label="single-claim-primary")

        crash_returncode = await crashed_worker.wait_for_exit(timeout=max(session.timeout, 120))
        if crash_returncode == 0:
            raise AssertionError("Single-claim worker exited cleanly instead of crashing.")

        replacement_one = await session.launch_agent_process(
            agent_type="WebResearchAgent",
            agent_id=f"webresearch-claim-a-{new_id_suffix()}",
        )
        replacement_two = await session.launch_agent_process(
            agent_type="WebResearchAgent",
            agent_id=f"webresearch-claim-b-{new_id_suffix()}",
        )
        await wait_for_agent_row(
            session.db_path,
            team_id=session.team_id,
            agent_type="WebResearchAgent",
            agent_id=replacement_one.agent_id,
            timeout=45,
        )
        await wait_for_agent_row(
            session.db_path,
            team_id=session.team_id,
            agent_type="WebResearchAgent",
            agent_id=replacement_two.agent_id,
            timeout=45,
        )

        events = await session.receive_matching(
            predicate=lambda env: env.msg_meta and env.msg_meta.msg_correlation_id == corr and isinstance(env.msg_content, Task),
            expected_count=1,
            timeout=max(session.timeout, 180),
        )
        if len(events) != 1:
            raise AssertionError("Did not receive the recovered direct worker reply after launching two replacements.")

        response = events[0].envelope
        answer_text = str(response.msg_content.answer or "")
        if "banana" not in answer_text.lower() and "bread" not in answer_text.lower():
            raise AssertionError(f"Recovered direct-worker answer did not match the banana-bread request: {answer_text}")

        await wait_for_condition(
            "exactly one replacement worker archived the recovered task",
            lambda: (
                sum(
                    1
                    for owner_agent_id in [replacement_one.agent_id, replacement_two.agent_id]
                    for row in get_archived_task_rows(
                        session.db_path,
                        owner_agent_id=owner_agent_id,
                        team_id=session.team_id,
                    )
                    if row["task_id"] == response.msg_content.task_id
                )
                >= 1
            ),
            timeout=15,
        )
        await asyncio.sleep(1.5)
        archived_one = get_archived_task_rows(
            session.db_path,
            owner_agent_id=replacement_one.agent_id,
            team_id=session.team_id,
        )
        archived_two = get_archived_task_rows(
            session.db_path,
            owner_agent_id=replacement_two.agent_id,
            team_id=session.team_id,
        )
        matched_one = [row for row in archived_one if row["task_id"] == response.msg_content.task_id]
        matched_two = [row for row in archived_two if row["task_id"] == response.msg_content.task_id]
        if bool(matched_one) == bool(matched_two):
            raise AssertionError("Expected exactly one replacement worker to claim and archive the recovered task.")

        return (
            "When two same-type replacement workers started after one crashed, only one of them claimed the persisted task and returned the recovered reply.",
            {
                "crashed_worker": {
                    **crashed_worker.snapshot(),
                    "crash_returncode": crash_returncode,
                },
                "replacement_one": replacement_one.snapshot(),
                "replacement_two": replacement_two.snapshot(),
                "response_task_id": response.msg_content.task_id,
                "response_from_agent_id": response.msg_header.from_agent_id,
                "claiming_agent_id": replacement_one.agent_id if matched_one else replacement_two.agent_id,
            },
        )
    finally:
        cleanup = await stop_managed_agents(
            session,
            [proc for proc in [crashed_worker, replacement_one, replacement_two] if proc is not None],
        )
        live_nats_client.write_json_file(session.result_dir / "scenario_29_cleanup.json", cleanup)


async def scenario_30(session: ReliabilitySession) -> tuple[str, dict]:
    assert_no_preexisting_specialists(
        session.db_path,
        team_id=session.team_id,
        agent_type="WebResearchAgent",
    )
    mismatched_team_id = f"{session.team_id}_team_mismatch"

    crashed_worker = await session.launch_agent_process(
        agent_type="WebResearchAgent",
        agent_id=f"webresearch-team-mismatch-{new_id_suffix()}",
        agent_result_transformer="tests.live_result_transformers:crash_first_task_before_reply",
    )

    try:
        await wait_for_agent_row(
            session.db_path,
            team_id=session.team_id,
            agent_type="WebResearchAgent",
            agent_id=crashed_worker.agent_id,
            timeout=45,
        )

        prompt = _build_direct_worker_web_prompt("a simple lentil soup recipe", session.result_dir)
        envelope, corr = session.build_task_envelope(
            prompt,
            routing_key=f"{session.team_id}_WebResearchAgent",
            to_named_agent_type="WebResearchAgent",
            response_routing_key=session.user_queue_name,
        )
        await session.send_envelope(envelope, routing_key=f"{session.team_id}_WebResearchAgent", label="team-mismatch-primary")

        crash_returncode = await crashed_worker.wait_for_exit(timeout=max(session.timeout, 120))
        if crash_returncode == 0:
            raise AssertionError("Team-mismatch worker exited cleanly instead of crashing.")

        mismatched_worker = await session.launch_agent_process(
            agent_type="WebResearchAgent",
            agent_id=crashed_worker.agent_id,
            team_id=mismatched_team_id,
        )
        await wait_for_agent_row(
            session.db_path,
            team_id=mismatched_team_id,
            agent_type="WebResearchAgent",
            agent_id=mismatched_worker.agent_id,
            timeout=45,
        )

        events = await session.receive_matching(
            predicate=lambda env: env.msg_meta and env.msg_meta.msg_correlation_id == corr and isinstance(env.msg_content, Task),
            expected_count=1,
            timeout=max(session.timeout, 90),
        )
        if len(events) != 1:
            raise AssertionError("Did not receive the team-mismatch discard error response.")

        response = events[0].envelope
        answer_text = str(response.msg_content.answer or "")
        if "team_id changed" not in answer_text.lower() and not any(
            "team_id changed" in err.lower() for err in (response.msg_content.errors or [])
        ):
            raise AssertionError(f"Team-mismatch response did not explain the discard reason: {answer_text}")

        archived_rows = get_archived_task_rows(
            session.db_path,
            owner_agent_id=mismatched_worker.agent_id,
            team_id=mismatched_team_id,
        )
        matching_archive = [row for row in archived_rows if row["task_id"] == response.msg_content.task_id]
        if not matching_archive:
            raise AssertionError("Team-mismatch discard task was not archived under the replacement worker.")

        return (
            "When the same worker agent_id restarted under a different team_id, it did not resume the old work. Instead it failed and archived the restored task with a team-mismatch error response.",
            {
                "original_team_id": session.team_id,
                "mismatched_team_id": mismatched_team_id,
                "worker_agent_id": crashed_worker.agent_id,
                "crash_returncode": crash_returncode,
                "response_task_id": response.msg_content.task_id,
                "archived_owner_agent_id": matching_archive[0]["owner_agent_id"],
                "archived_owner_team_id": matching_archive[0]["team_id"],
            },
        )
    finally:
        active_mismatch_rows = [
            row
            for row in get_agent_rows(session.db_path, mismatched_team_id, "WebResearchAgent")
            if row["agent_id"] == crashed_worker.agent_id and not row["is_exiting"]
        ]
        if active_mismatch_rows:
            await send_exit_to_agents(session, [crashed_worker.agent_id])
        cleanup = await stop_managed_agents(session, [crashed_worker])
        live_nats_client.write_json_file(session.result_dir / "scenario_30_cleanup.json", cleanup)


async def scenario_31(session: ReliabilitySession) -> tuple[str, dict]:
    spawned_agent_type = "InternalImprovementAgent"
    assert_no_preexisting_specialists(
        session.db_path,
        team_id=session.team_id,
        agent_type=spawned_agent_type,
    )

    baseline_ids = {
        row["agent_id"]
        for row in get_agent_rows(session.db_path, session.team_id, spawned_agent_type)
        if not row["is_exiting"]
    }

    prompt = (
        "Use your `agent-instantiation-skill` to create exactly one InternalImprovementAgent on this team without "
        "passing any `mcp_profile_names`; the operator has statically configured that type with the `noop-test` MCP "
        "server for this test. After the new agent exists, delegate a task to that child telling it to call its noop "
        "MCP echo tool with the exact text `live-mcp-ok` and return the tool output. Wait for "
        "the delegated child result and then send me a final Task response that includes the exact strings "
        "`noop-test` and `live-mcp-ok`. Do not create any other agents."
    )
    envelope, corr = session.build_task_envelope(
        prompt,
        routing_key=session.manager_id,
        to_named_agent_type="TeamArchitect",
        response_routing_key=session.user_queue_name,
    )
    await session.send_envelope(envelope, routing_key=session.manager_id, label="manager-skill-noop-mcp")

    response_events = await session.receive_matching(
        predicate=lambda env: env.msg_meta and env.msg_meta.msg_correlation_id == corr and isinstance(env.msg_content, Task),
        expected_count=1,
        timeout=max(session.timeout, 180),
    )
    if len(response_events) != 1:
        raise AssertionError("Did not receive the final manager response for the noop MCP instantiation scenario.")

    final_response = response_events[0].envelope
    final_answer = str(final_response.msg_content.answer or "")
    if "noop-test" not in final_answer or "live-mcp-ok" not in final_answer:
        raise AssertionError(
            "Manager response did not confirm both the noop MCP profile and echoed payload: "
            f"{final_answer}"
        )

    await wait_for_condition(
        "new InternalImprovementAgent spawned by manager with noop MCP profile",
        lambda: any(
            row["agent_id"] not in baseline_ids and not row["is_exiting"]
            for row in get_agent_rows(session.db_path, session.team_id, spawned_agent_type)
        ),
        timeout=45,
    )
    new_rows = [
        row
        for row in get_agent_rows(session.db_path, session.team_id, spawned_agent_type)
        if row["agent_id"] not in baseline_ids and not row["is_exiting"]
    ]
    if len(new_rows) != 1:
        raise AssertionError(f"Expected exactly one new {spawned_agent_type}, found {[row['agent_id'] for row in new_rows]}")

    spawned_agent_id = new_rows[0]["agent_id"]
    await send_exit_to_agents(session, [spawned_agent_id])
    await wait_for_condition(
        "spawned InternalImprovementAgent exit acknowledgement",
        lambda: any(
            row["agent_id"] == spawned_agent_id and row["is_exiting"]
            for row in get_agent_rows(session.db_path, session.team_id, spawned_agent_type)
        ),
        timeout=30,
    )

    return (
        "An isolated TeamArchitect used its startup-generated agent-instantiation-skill to create an InternalImprovementAgent "
        "whose type had the operator-configured `noop-test` MCP server, delegated a noop echo call through that child, and returned the echoed "
        "payload in the final response.",
        {
            "manager_id": session.manager_id,
            "spawned_agent_id": spawned_agent_id,
            "final_task_id": final_response.msg_content.task_id,
            "final_answer": final_answer,
            "noop_profile_name": "noop-test",
        },
    )


async def scenario_32(session: ReliabilitySession) -> tuple[str, dict]:
    required_types = [
        "PersonalAdminAgent",
        "ResearchComparisonAgent",
        "HomeErrandsAgent",
    ]
    for agent_type in required_types:
        assert_no_preexisting_specialists(
            session.db_path,
            team_id=session.team_id,
            agent_type=agent_type,
        )

    prompt = (
        "Use your workspace team templates to inspect the available templates and instantiate exactly the "
        "`balanced_delivery_team` template on this team. Do not create any other agents outside that template. "
        "Wait until the template is instantiated, then send a final Task response that includes the exact string "
        "`balanced_delivery_team` and also explicitly names `PersonalAdminAgent`, `ResearchComparisonAgent`, and "
        "`HomeErrandsAgent`."
    )
    envelope, corr = session.build_task_envelope(
        prompt,
        routing_key=session.manager_id,
        to_named_agent_type="TeamArchitect",
        response_routing_key=session.user_queue_name,
    )
    await session.send_envelope(envelope, routing_key=session.manager_id, label="manager-team-template-create")

    response_events = await session.receive_matching(
        predicate=lambda env: env.msg_meta and env.msg_meta.msg_correlation_id == corr and isinstance(env.msg_content, Task),
        expected_count=1,
        timeout=max(session.timeout, 180),
    )
    if len(response_events) != 1:
        raise AssertionError("Did not receive the final manager response for the team-template instantiation scenario.")

    final_response = response_events[0].envelope
    final_answer = str(final_response.msg_content.answer or "")
    for expected in ["balanced_delivery_team", "PersonalAdminAgent", "ResearchComparisonAgent", "HomeErrandsAgent"]:
        if expected not in final_answer:
            raise AssertionError(f"Manager response did not mention `{expected}`: {final_answer}")

    for agent_type in required_types:
        await wait_for_condition(
            f"{agent_type} spawned from balanced_delivery_team",
            lambda agent_type=agent_type: len(
                [row for row in get_agent_rows(session.db_path, session.team_id, agent_type) if not row["is_exiting"]]
            ) == 1,
            timeout=45,
        )

    baseline_counts = {
        agent_type: len([row for row in get_agent_rows(session.db_path, session.team_id, agent_type) if not row["is_exiting"]])
        for agent_type in required_types
    }

    second_prompt = (
        "Instantiate the `balanced_delivery_team` workspace team template again using the default missing-only behavior. "
        "Do not create duplicates. Send a final Task response that includes the exact phrase `missing-only ok`."
    )
    second_envelope, second_corr = session.build_task_envelope(
        second_prompt,
        routing_key=session.manager_id,
        to_named_agent_type="TeamArchitect",
        response_routing_key=session.user_queue_name,
    )
    await session.send_envelope(second_envelope, routing_key=session.manager_id, label="manager-team-template-repeat")

    second_events = await session.receive_matching(
        predicate=lambda env: env.msg_meta and env.msg_meta.msg_correlation_id == second_corr and isinstance(env.msg_content, Task),
        expected_count=1,
        timeout=max(session.timeout, 180),
    )
    if len(second_events) != 1:
        raise AssertionError("Did not receive the second final manager response for the team-template scenario.")

    second_response = second_events[0].envelope
    second_answer = str(second_response.msg_content.answer or "")
    if "missing-only ok" not in second_answer:
        raise AssertionError(f"Manager response did not confirm missing-only behavior: {second_answer}")

    final_counts = {
        agent_type: len([row for row in get_agent_rows(session.db_path, session.team_id, agent_type) if not row["is_exiting"]])
        for agent_type in required_types
    }
    if final_counts != baseline_counts:
        raise AssertionError(f"Template re-instantiation created unexpected duplicates: before={baseline_counts}, after={final_counts}")

    spawned_agent_ids = []
    for agent_type in required_types:
        rows = [row for row in get_agent_rows(session.db_path, session.team_id, agent_type) if not row["is_exiting"]]
        spawned_agent_ids.extend(row["agent_id"] for row in rows)

    await send_exit_to_agents(session, spawned_agent_ids)
    for agent_type in required_types:
        await wait_for_condition(
            f"{agent_type} exit acknowledgement after team template scenario",
            lambda agent_type=agent_type: all(
                row["is_exiting"] for row in get_agent_rows(session.db_path, session.team_id, agent_type)
            ),
            timeout=30,
        )

    return (
        "An isolated TeamArchitect loaded the workspace `balanced_delivery_team` team template, instantiated the expected analysis, coding, and verification specialists, and a second template-instantiation call did not create duplicates because missing-only behavior held.",
        {
            "manager_id": session.manager_id,
            "required_types": required_types,
            "first_final_task_id": final_response.msg_content.task_id,
            "second_final_task_id": second_response.msg_content.task_id,
            "first_final_answer": final_answer,
            "second_final_answer": second_answer,
            "counts_after_first_call": baseline_counts,
            "counts_after_second_call": final_counts,
            "spawned_agent_ids": spawned_agent_ids,
        },
    )


async def scenario_45(session: ReliabilitySession) -> tuple[str, dict]:
    canonical_user_id = f"memory-user-{new_id_suffix()}"

    first_response = await send_gateway_style_task(
        session,
        canonical_user_id=canonical_user_id,
        prompt=(
            "Use usermem to remember this for future conversations: call me LiveNick and I prefer concise replies. "
            "Reply with exactly `memory stored` after you have handled it."
        ),
        correlation_suffix="memory_store",
    )
    first_answer = str(first_response.msg_content.answer or "")
    if "memory stored" not in first_answer.lower():
        raise AssertionError(f"Initial memory-seeding reply did not confirm storage: {first_answer}")

    await wait_for_condition(
        "usermem fact persisted",
        lambda: bool(
            get_pinned_memory_rows(
                session.db_path,
                bank="usermem",
                canonical_user_id=canonical_user_id,
            )
        ),
        timeout=30,
    )
    usermem_rows = get_pinned_memory_rows(
        session.db_path,
        bank="usermem",
        canonical_user_id=canonical_user_id,
    )
    if not usermem_rows:
        raise AssertionError("Usermem fact was not created.")

    second_response = await send_gateway_style_task(
        session,
        canonical_user_id=canonical_user_id,
        prompt=(
            "Inspect your usermem before answering. "
            "Tell me the nickname you should use for me and whether I prefer concise or detailed replies."
        ),
        correlation_suffix="memory_recall",
    )
    second_answer = str(second_response.msg_content.answer or "")
    lowered = second_answer.lower()
    if "livenick" not in lowered or "concise" not in lowered:
        raise AssertionError(f"Memory recall response did not include the stored user memory: {second_answer}")

    flattened_usermem = " ".join(row["fact_text"] for row in usermem_rows).lower()
    if "livenick" not in flattened_usermem or "concise" not in flattened_usermem:
        raise AssertionError(f"Usermem facts missing nickname or concise preference: {usermem_rows}")

    return (
        "A live gateway-style user request seeded pinned usermem and a later top-level conversation recalled the same nickname and reply preference from persisted usermem.",
        {
            "manager_id": session.manager_id,
            "canonical_user_id": canonical_user_id,
            "first_task_id": first_response.msg_content.task_id,
            "second_task_id": second_response.msg_content.task_id,
            "first_answer": first_answer,
            "second_answer": second_answer,
            "usermem_rows": usermem_rows,
        },
    )


async def scenario_46(session: ReliabilitySession) -> tuple[str, dict]:
    canonical_user_id = f"memory-restart-{new_id_suffix()}"
    original_manager_id = session.manager_id

    seed_response = await send_gateway_style_task(
        session,
        canonical_user_id=canonical_user_id,
        prompt=(
            "Use usermem to remember this for future conversations: call me RestartNick and I prefer bullet points. "
            "Reply with exactly `restart memory stored`."
        ),
        correlation_suffix="restart_seed",
    )
    seed_answer = str(seed_response.msg_content.answer or "")
    if "restart memory stored" not in seed_answer.lower():
        raise AssertionError(f"Manager did not confirm seeded memory before restart: {seed_answer}")

    original_proc = next(
        (proc for proc in session._managed_processes if proc.agent_id == original_manager_id),
        None,
    )
    if original_proc is None:
        raise AssertionError("Original managed manager process was not tracked.")
    original_exit_code = await original_proc.stop(timeout=10)
    if original_exit_code is None:
        raise AssertionError("Original manager did not stop cleanly enough for restart test.")

    replacement_manager = await session.launch_agent_process(
        agent_type="TeamArchitect",
        agent_id=f"memory-manager-replacement-{new_id_suffix()}",
        is_lead=True,
        config_path=session.auto_manager.get("config_path"),
    )
    session.manager_id = replacement_manager.agent_id
    await wait_for_agent_row(
        session.db_path,
        team_id=session.team_id,
        agent_type="TeamArchitect",
        agent_id=replacement_manager.agent_id,
        timeout=45,
    )

    recall_response = await send_gateway_style_task(
        session,
        canonical_user_id=canonical_user_id,
        prompt=(
            "Inspect your usermem and reply with the nickname you should use for me "
            "and whether you should use bullet points."
        ),
        correlation_suffix="restart_recall",
    )
    recall_answer = str(recall_response.msg_content.answer or "")
    lowered = recall_answer.lower()
    if "restartnick" not in lowered or "bullet" not in lowered:
        raise AssertionError(f"Replacement manager did not recall persisted user memory: {recall_answer}")

    usermem_rows = get_pinned_memory_rows(
        session.db_path,
        bank="usermem",
        canonical_user_id=canonical_user_id,
    )
    if not usermem_rows:
        raise AssertionError("Restart scenario could not find persisted usermem fact.")

    return (
        "After the original TeamArchitect exited, a replacement same-role manager with a new agent_id still recalled the persisted user nickname and formatting preference from pinned usermem.",
        {
            "original_manager_id": original_manager_id,
            "replacement_manager_id": replacement_manager.agent_id,
            "original_exit_code": original_exit_code,
            "canonical_user_id": canonical_user_id,
            "seed_task_id": seed_response.msg_content.task_id,
            "recall_task_id": recall_response.msg_content.task_id,
            "seed_answer": seed_answer,
            "recall_answer": recall_answer,
            "usermem_rows": usermem_rows,
        },
    )


async def scenario_47(session: ReliabilitySession) -> tuple[str, dict]:
    canonical_user_id = f"memory-conflict-{new_id_suffix()}"

    concise_response = await send_gateway_style_task(
        session,
        canonical_user_id=canonical_user_id,
        prompt="Use usermem to remember this for future conversations: I prefer concise replies. Reply with exactly `concise stored`.",
        correlation_suffix="conflict_first",
    )
    if "concise stored" not in str(concise_response.msg_content.answer or "").lower():
        raise AssertionError("Initial concise-preference request did not confirm storage.")

    detailed_response = await send_gateway_style_task(
        session,
        canonical_user_id=canonical_user_id,
        prompt=(
            "Update usermem: actually I prefer detailed step-by-step replies now. "
            "Reply with exactly `detailed stored`."
        ),
        correlation_suffix="conflict_second",
    )
    if "detailed stored" not in str(detailed_response.msg_content.answer or "").lower():
        raise AssertionError("Updated detailed-preference request did not confirm storage.")

    recall_response = await send_gateway_style_task(
        session,
        canonical_user_id=canonical_user_id,
        prompt=(
            "Inspect your usermem and answer with exactly one word: "
            "`concise` or `detailed`, depending on the latest stored preference."
        ),
        correlation_suffix="conflict_recall",
    )
    recall_answer = str(recall_response.msg_content.answer or "").strip().lower()
    if "detailed" not in recall_answer:
        raise AssertionError(f"Contradiction resolution did not preserve the latest preference: {recall_answer}")

    usermem_rows = get_pinned_memory_rows(
        session.db_path,
        bank="usermem",
        canonical_user_id=canonical_user_id,
    )
    if not any("detailed" in row["fact_text"].lower() for row in usermem_rows):
        raise AssertionError(f"Expected usermem to contain the latest detailed preference, got: {usermem_rows}")

    return (
        "A later explicit user preference was recalled as the current preference, and persisted usermem contained the detailed-reply preference.",
        {
            "manager_id": session.manager_id,
            "canonical_user_id": canonical_user_id,
            "concise_task_id": concise_response.msg_content.task_id,
            "detailed_task_id": detailed_response.msg_content.task_id,
            "recall_task_id": recall_response.msg_content.task_id,
            "recall_answer": recall_answer,
            "usermem_rows": usermem_rows,
        },
    )


async def scenario_48(session: ReliabilitySession) -> tuple[str, dict]:
    canonical_user_id = f"memory-tools-{new_id_suffix()}"

    response = await send_gateway_style_task(
        session,
        canonical_user_id=canonical_user_id,
        prompt=(
            "Use your pinned memory tools right now. "
            "Call memory with action create and bank usermem to store that you should call me ToolNick. "
            "Call memory with action create and bank coremem to store that this team standardizes on SQLite. "
            "Then inspect both scopes and reply with a sentence that includes `ToolNick` and `SQLite`."
        ),
        correlation_suffix="tool_write",
    )
    answer = str(response.msg_content.answer or "")
    lowered = answer.lower()
    if "toolnick" not in lowered or "sqlite" not in lowered:
        raise AssertionError(f"Explicit tool-path scenario did not confirm stored memory: {answer}")

    usermem_rows = get_pinned_memory_rows(
        session.db_path,
        bank="usermem",
        canonical_user_id=canonical_user_id,
    )
    coremem_rows = get_pinned_memory_rows(
        session.db_path,
        bank="coremem",
        team_id=session.team_id,
    )
    if not any("toolnick" in row["fact_text"].lower() for row in usermem_rows):
        raise AssertionError(f"Usermem did not store ToolNick: {usermem_rows}")
    if not any("sqlite" in row["fact_text"].lower() for row in coremem_rows):
        raise AssertionError(f"Coremem did not store SQLite convention: {coremem_rows}")

    return (
        "A live manager task explicitly exercised the pinned-memory tool path, stored both usermem and coremem, and then read them back in the same response.",
        {
            "manager_id": session.manager_id,
            "canonical_user_id": canonical_user_id,
            "task_id": response.msg_content.task_id,
            "answer": answer,
            "usermem_rows": usermem_rows,
            "coremem_rows": coremem_rows,
        },
    )


async def scenario_49(session: ReliabilitySession) -> tuple[str, dict]:
    token = f"llm-summary-token-{new_id_suffix()}"
    lifecycle_jsonl = session.result_dir / "context_lifecycle.jsonl"
    if lifecycle_jsonl.exists():
        lifecycle_jsonl.unlink()
    prompts = [
        (
            f"Remember this exact token for this conversation: `{token}`. "
            "Reply with one short sentence confirming the token."
        ),
        (
            "Use the prior conversation context. What exact token did I ask you to remember? "
            "Reply with the token and the phrase context-engine-live-check."
        ),
        (
            "Summarize the two earlier requests in this same conversation in two bullets, "
            "and include the exact token."
        ),
    ]

    conversation_id: Optional[str] = None
    prior_task_id: Optional[str] = None
    responses = []
    for index, prompt in enumerate(prompts, start=1):
        envelope, corr = session.build_task_envelope(
            prompt,
            routing_key=session.manager_id,
            to_named_agent_type="TeamArchitect",
            response_routing_key=session.user_queue_name,
            conversation_id=conversation_id,
            followup_to_task_id=prior_task_id,
        )
        await session.send_envelope(envelope, routing_key=session.manager_id, label=f"llm-summary-live-turn-{index}")
        events = await session.receive_matching(
            predicate=lambda env, expected_corr=corr: (
                env.msg_meta
                and env.msg_meta.msg_correlation_id == expected_corr
                and isinstance(env.msg_content, Task)
            ),
            expected_count=1,
            timeout=max(session.timeout, 240),
        )
        if len(events) != 1:
            raise AssertionError(f"Did not receive LLM summary live response for turn {index}.")
        response = events[0].envelope
        conversation_id = response.msg_meta.conversation_id
        prior_task_id = response.msg_content.task_id
        answer = str(response.msg_content.answer or "")
        responses.append(
            {
                "turn_index": index,
                "correlation_id": corr,
                "task_id": prior_task_id,
                "answer": answer,
            }
        )

    if not conversation_id:
        raise AssertionError("LLM summary live scenario did not produce a conversation_id.")
    final_answer = responses[-1]["answer"]
    if token not in final_answer:
        raise AssertionError(f"Final turn did not preserve the conversation token: {final_answer}")

    thread = get_conversation_thread_row(session.db_path, conversation_id)
    turns = get_conversation_turn_rows(session.db_path, conversation_id)
    if thread is None:
        raise AssertionError(f"Conversation thread {conversation_id} was not persisted.")
    if len(turns) < 3:
        raise AssertionError(f"Expected at least 3 persisted turns, found {len(turns)}.")

    missing_summary_fields = []
    non_llm_generators = []
    for turn in turns[:3]:
        for field_name in [
            "user_request_summary",
            "assistant_response_summary",
            "context_summary_text",
            "summary_generated_at",
        ]:
            if not turn.get(field_name):
                missing_summary_fields.append({"turn_index": turn["turn_index"], "field": field_name})
        if turn.get("summary_generator") != "llm_summarizing":
            non_llm_generators.append(
                {
                    "turn_index": turn["turn_index"],
                    "summary_generator": turn.get("summary_generator"),
                }
            )
    if missing_summary_fields:
        raise AssertionError(f"LLM summary fields were missing: {missing_summary_fields}")
    if non_llm_generators:
        raise AssertionError(f"Expected llm_summarizing summaries, got: {non_llm_generators}")

    await wait_for_condition(
        "context lifecycle recorder events",
        lambda: lifecycle_jsonl.exists() and len(_read_jsonl(lifecycle_jsonl)) >= 6,
        timeout=20,
    )
    lifecycle_events = _read_jsonl(lifecycle_jsonl)
    event_counts = {
        event_name: sum(1 for event in lifecycle_events if event.get("event") == event_name)
        for event_name in ["ingest", "assemble", "compact", "after_turn"]
    }
    if event_counts["ingest"] < 3 or event_counts["assemble"] < 3 or event_counts["after_turn"] < 3:
        raise AssertionError(f"Context lifecycle recorder did not observe all expected hooks: {event_counts}")
    if not any((event.get("warning_count") or 0) > 0 for event in lifecycle_events if event.get("event") == "assemble"):
        raise AssertionError("Expected at least one context preflight warning during live assembly.")

    summary_outputs = _write_live_summary_views(
        result_dir=session.result_dir,
        thread=thread,
        turns=turns,
        lifecycle_events=lifecycle_events,
    )

    manager_process = session._managed_processes[0] if session._managed_processes else None

    return (
        "Ran a three-turn live conversation with LLM-generated per-turn summaries, context preflight warnings, and context lifecycle hook recording; wrote full turn history and generated summaries to disk.",
        {
            "conversation_id": conversation_id,
            "token": token,
            "responses": responses,
            "turn_count": len(turns),
            "event_counts": event_counts,
            "summary_outputs": summary_outputs,
            "manager_process": manager_process.snapshot() if manager_process else {},
        },
    )


async def scenario_50(session: ReliabilitySession) -> tuple[str, dict]:
    raw_tail_sentinel = f"raw-epoch-only-{new_id_suffix()}"
    dense = await _build_dense_live_conversation(
        session,
        prefix="epoch-live",
        turn_count=5,
        repeated_lines=4,
        first_turn_tail_sentinel=raw_tail_sentinel,
        target_named_agent_type="CodeWritingAgent",
    )
    conversation_id = dense["conversation_id"]
    thread = get_conversation_thread_row(session.db_path, conversation_id)
    turns = get_conversation_turn_rows(session.db_path, conversation_id)
    epochs = get_context_epoch_rows(session.db_path, conversation_id)
    if thread is None:
        raise AssertionError(f"Conversation thread {conversation_id} was not persisted.")
    if len(turns) < 6:
        raise AssertionError(f"Expected at least 6 persisted turns including follow-up, found {len(turns)}.")
    if not epochs:
        raise AssertionError("Expected at least one context epoch to be created by live compaction.")

    missing_summary_fields = []
    for turn in turns:
        for field_name in ["user_request_summary", "assistant_response_summary", "context_summary_text"]:
            if not turn.get(field_name):
                missing_summary_fields.append({"turn_index": turn["turn_index"], "field": field_name})
    if missing_summary_fields:
        raise AssertionError(f"Per-turn summaries were missing after epoch creation: {missing_summary_fields}")

    latest_epoch = epochs[-1]
    manifest = latest_epoch.get("rehydration_manifest") or {}
    required_summary_fields = {
        "user_request_summary",
        "assistant_response_summary",
        "context_summary_text",
        "artifact_summary_json",
        "tool_artifact_manifest_json",
    }
    observed_summary_fields = set(manifest.get("source_summary_fields") or [])
    if not required_summary_fields.issubset(observed_summary_fields):
        raise AssertionError(
            f"Latest epoch did not record source summary fields. observed={sorted(observed_summary_fields)}"
        )
    if "summary_layering" not in manifest:
        raise AssertionError("Latest epoch did not record the summary layering policy.")
    if len(epochs) > 1 and not any(epoch.get("rehydration_manifest", {}).get("source_epoch_ids") for epoch in epochs[1:]):
        raise AssertionError("Later epochs did not record prior epoch lineage in source_epoch_ids.")

    renderer_store = ConversationStore(
        db_path=str(session.db_path) if isinstance(session.db_path, Path) else str(session.result_dir / "renderer_unused.db"),
        storage_settings=session.storage_settings,
        artifacts_dir=session.result_dir / "conversation_artifacts",
    )
    rendered = BudgetedThreadContextRenderer().render(
        store=renderer_store,
        conversation_id=conversation_id,
        exclude_task_id=dense["final_task_id"],
        available_tokens=20_000,
    )
    if latest_epoch["context_epoch_id"] not in rendered.text:
        raise AssertionError("Epoch-aware renderer did not include the latest context epoch boundary.")
    if raw_tail_sentinel in rendered.text:
        raise AssertionError("Epoch-aware renderer replayed pre-epoch raw turn text instead of using summaries.")
    if dense["token"] not in dense["final_answer"]:
        raise AssertionError("Live follow-up no longer recovered the repeated token from compacted context.")

    live_nats_client.write_json_file(
        session.result_dir / "context_epoch_live_report.json",
        {
            "conversation_id": conversation_id,
            "thread": thread,
            "turns": turns,
            "epochs": epochs,
            "rendered_context_metadata": rendered.metadata,
            "rendered_context_path": str(session.result_dir / "epoch_rendered_context.txt"),
            "raw_tail_sentinel": raw_tail_sentinel,
        },
    )
    (session.result_dir / "epoch_rendered_context.txt").write_text(rendered.text, encoding="utf-8")

    return (
        "Ran a live dense conversation that created durable context epochs, preserved per-turn summaries, recorded summary-of-summary lineage, and answered a follow-up through epoch-aware context.",
        {
            "conversation_id": conversation_id,
            "token": dense["token"],
            "epoch_count": len(epochs),
            "latest_epoch": latest_epoch,
            "turn_count": len(turns),
            "rendered_context_metadata": rendered.metadata,
            "final_answer": dense["final_answer"],
        },
    )


async def scenario_51(session: ReliabilitySession) -> tuple[str, dict]:
    token = f"sched-context-{new_id_suffix()}"
    first_phrase = f"goal scored live context token {token}"
    second_phrase = f"scheduled context carried {token}"
    conversation_id = new_id("conv_live_sched")
    schedule_id = new_id("sched_live_ctx")
    correlation_id = new_id("corr_live_sched")
    prompt = (
        "Recurring scheduled context live test. "
        "Look only at prior rendered Conversation Thread Context, not this current instruction. "
        f"If an earlier Assistant response in that context already contains exactly `{first_phrase}`, "
        f"reply with exactly `{second_phrase}`. "
        f"If no earlier Assistant response contains that exact phrase, reply with exactly `{first_phrase}`. "
        "Do not call tools and do not add any other text."
    )
    schedule = ScheduledMessage(
        schedule_id=schedule_id,
        schedule_name="live recurring schedule context",
        payload=Task(
            task_type=MessageMetaType.REQUEST,
            problem_statement=prompt,
        ),
        payload_header=MessageHeader(
            from_agent_id=session.user_queue_name,
            to_routing_path=session.manager_id,
            response_routing_key=session.user_queue_name,
            agent_signature="",
        ),
        payload_meta=MessageMeta(
            from_agent_name="live_gateway",
            msg_correlation_id=correlation_id,
            conversation_id=conversation_id,
            requester_type=RequesterType.TIMER_REQUESTER,
            is_response_requested=True,
            gateway_context=GatewayContext(
                session_key="live_schedule_context",
                delivery_mode="cron",
                scheduled_job_id=schedule_id,
            ),
        ),
        interval_ms=8000,
        repeat_count=2,
    )
    await session.send_envelope(
        MessageEnvelope(
            msg_content=schedule,
            msg_meta=MessageMeta(
                from_agent_name="live_gateway",
                msg_correlation_id=correlation_id,
                conversation_id=conversation_id,
                requester_type=RequesterType.USER_REQUESTER,
            ),
            msg_header=MessageHeader(
                from_agent_id=session.user_queue_name,
                to_routing_path=session.manager_id,
                response_routing_key=session.user_queue_name,
                agent_signature="",
            ),
        ),
        routing_key=session.manager_id,
        label="recurring-schedule-context",
    )

    first = await session.receive_matching(
        predicate=lambda env: (
            env.msg_meta
            and env.msg_meta.msg_correlation_id == correlation_id
            and isinstance(env.msg_content, Task)
            and str(env.msg_content.answer or "").strip()
        ),
        expected_count=1,
        timeout=session.timeout,
    )
    if len(first) != 1:
        raise AssertionError("Did not receive the first recurring scheduled response.")
    first_env = first[0].envelope
    first_answer = str(first_env.msg_content.answer or "")
    if first_phrase not in first_answer:
        raise AssertionError(f"First scheduled response did not emit the baseline phrase: {first_answer}")
    schedule_conversation_id = getattr(first_env.msg_meta, "conversation_id", None)
    if not schedule_conversation_id:
        raise AssertionError("First scheduled response did not carry a schedule conversation_id.")
    if schedule_conversation_id == conversation_id:
        raise AssertionError("Recurring schedule did not fork into a dedicated conversation_id.")

    delivered_at = datetime.now(timezone.utc).isoformat()
    await session.send_envelope(
        MessageEnvelope(
            msg_content=DeliveryReceipt(
                task_id=first_env.msg_content.task_id,
                conversation_id=schedule_conversation_id,
                correlation_id=correlation_id,
                scheduled_job_id=schedule_id,
                session_key="live_schedule_context",
                delivery_state="delivered",
                delivery_via="live_test",
                delivered_at=delivered_at,
            ),
            msg_meta=MessageMeta(
                from_agent_name="live_gateway",
                msg_correlation_id=correlation_id,
                conversation_id=schedule_conversation_id,
                requester_type=RequesterType.AGENT_REQUESTER,
                is_response_requested=False,
            ),
            msg_header=MessageHeader(
                from_agent_id=session.user_queue_name,
                to_routing_path=session.manager_id,
                response_routing_key=session.user_queue_name,
                agent_signature="",
            ),
        ),
        routing_key=session.manager_id,
        label="recurring-schedule-context-receipt-1",
    )

    second = await session.receive_matching(
        predicate=lambda env: (
            env.msg_meta
            and env.msg_meta.msg_correlation_id == correlation_id
            and isinstance(env.msg_content, Task)
            and env.msg_content.task_id != first_env.msg_content.task_id
            and str(env.msg_content.answer or "").strip()
        ),
        expected_count=1,
        timeout=max(session.timeout, 90),
    )
    if len(second) != 1:
        raise AssertionError("Did not receive the second recurring scheduled response.")
    second_env = second[0].envelope
    second_answer = str(second_env.msg_content.answer or "")
    if second_phrase not in second_answer:
        raise AssertionError(
            "Second scheduled response did not appear to use the prior delivered timer turn. "
            f"Expected {second_phrase!r}, got: {second_answer}"
        )

    await session.send_envelope(
        MessageEnvelope(
            msg_content=DeliveryReceipt(
                task_id=second_env.msg_content.task_id,
                conversation_id=schedule_conversation_id,
                correlation_id=correlation_id,
                scheduled_job_id=schedule_id,
                session_key="live_schedule_context",
                delivery_state="delivered",
                delivery_via="live_test",
                delivered_at=datetime.now(timezone.utc).isoformat(),
            ),
            msg_meta=MessageMeta(
                from_agent_name="live_gateway",
                msg_correlation_id=correlation_id,
                conversation_id=schedule_conversation_id,
                requester_type=RequesterType.AGENT_REQUESTER,
                is_response_requested=False,
            ),
            msg_header=MessageHeader(
                from_agent_id=session.user_queue_name,
                to_routing_path=session.manager_id,
                response_routing_key=session.user_queue_name,
                agent_signature="",
            ),
        ),
        routing_key=session.manager_id,
        label="recurring-schedule-context-receipt-2",
    )

    await wait_for_condition(
        "recurring schedule context turns persisted",
        lambda: len(get_conversation_turn_rows(session.db_path, schedule_conversation_id)) >= 3,
        timeout=15,
    )
    turns = get_conversation_turn_rows(session.db_path, schedule_conversation_id)
    fired_turns = [turn for turn in turns if turn["task_id"] in {first_env.msg_content.task_id, second_env.msg_content.task_id}]
    if len(fired_turns) != 2:
        raise AssertionError(f"Did not find both fired scheduled turns in dedicated conversation: {turns}")
    if fired_turns[0]["delivery_state"] != "delivered":
        raise AssertionError(f"First scheduled turn was not marked delivered by receipt: {fired_turns[0]}")
    if fired_turns[1]["followup_to_task_id"] != first_env.msg_content.task_id:
        raise AssertionError(
            "Second scheduled turn did not link to the prior scheduled fire. "
            f"turn={fired_turns[1]} first_task_id={first_env.msg_content.task_id}"
        )

    return (
        "A recurring scheduled task forked a dedicated conversation_id, accepted a delivery receipt for the first fire, and the second fire answered using prior schedule conversation context.",
        {
            "schedule_id": schedule_id,
            "source_conversation_id": conversation_id,
            "schedule_conversation_id": schedule_conversation_id,
            "first_task_id": first_env.msg_content.task_id,
            "second_task_id": second_env.msg_content.task_id,
            "first_answer": first_answer,
            "second_answer": second_answer,
            "turns": turns,
        },
    )


SCENARIO_MAP = {
    8: scenario_8,
    10: scenario_10,
    11: scenario_11,
    12: scenario_12,
    13: scenario_13,
    14: scenario_14,
    15: scenario_15,
    16: scenario_16,
    17: scenario_17,
    18: scenario_18,
    19: scenario_19,
    20: scenario_20,
    21: scenario_21,
    22: scenario_22,
    23: scenario_23,
    24: scenario_24,
    25: scenario_25,
    26: scenario_26,
    27: scenario_27,
    28: scenario_28,
    29: scenario_29,
    30: scenario_30,
    31: scenario_31,
    32: scenario_32,
    45: scenario_45,
    46: scenario_46,
    47: scenario_47,
    48: scenario_48,
    49: scenario_49,
    50: scenario_50,
    51: scenario_51,
    52: scenario_52,
}


async def main() -> None:
    parser = argparse.ArgumentParser(description="Advanced live reliability scenarios")
    parser.add_argument("--test", type=int, required=True, choices=sorted(SCENARIO_MAP.keys()))
    parser.add_argument("--team-id", required=True)
    parser.add_argument("--timeout", type=int, default=120)
    parser.add_argument("--env", type=str, default=None)
    parser.add_argument("--config", type=str, default="config.yaml")
    args = parser.parse_args()

    if args.env and load_dotenv:
        load_dotenv(args.env)
        logger.info(f"Loaded environment variables from {args.env}")

    result_dir = prepare_result_dir(args.test)
    scenario_name = ADVANCED_TESTS[args.test]
    logger.info(f"Running advanced live test #{args.test}: {scenario_name}")

    sqlite_fallback_path = suite_default_db_path("reliability", args.test)
    storage_settings = resolve_live_storage_settings(
        sqlite_fallback_path=sqlite_fallback_path,
        workspace_dir=live_nats_client.PROJECT_ROOT,
        config_path=args.config,
    )
    if storage_settings.backend == "sqlite":
        db_path = provision_db(
            suite="reliability",
            test_id=args.test,
            db_path=sqlite_fallback_path,
        )
    else:
        reset_postgres_storage(storage_settings)
        db_path = None
    apply_storage_env(os.environ, storage_settings)
    logger.info("Live storage backend: %s (%s)", storage_settings.backend, storage_identifier(storage_settings))

    managed_manager_tests = set(SCENARIO_MAP.keys())
    session_kwargs = {}
    if args.test in managed_manager_tests:
        recovery_subtask_timeout = 12
        if args.test in {24, 25, 28}:
            recovery_subtask_timeout = 180
        elif args.test == 31:
            recovery_subtask_timeout = 45
        recovery_overrides = _managed_recovery_overrides(
            subtask_timeout_seconds=recovery_subtask_timeout,
        )
        if args.test in {26, 28}:
            recovery_overrides = deep_merge_dicts(
                recovery_overrides,
                {
                    "agent": {
                        "runtime_budget_policy": {
                            "mode": "hard_stop",
                        },
                    },
                },
            )
        if args.test == 31:
            recovery_overrides = deep_merge_dicts(
                recovery_overrides,
                _noop_mcp_profile_overrides(),
            )
        if args.test == 32:
            workspace_dir = result_dir / "manager_workspace"
            workspace_dir.mkdir(parents=True, exist_ok=True)
            (workspace_dir / "TEAM_TEMPLATES.yaml").write_text(
                yaml.safe_dump(
                    {
                        "version": 1,
                        "templates": {
                            "balanced_delivery_team": {
                                "description": "Balanced personal operations team.",
                                "members": [
                                    {"agent_type": "PersonalAdminAgent", "count": 1},
                                    {"agent_type": "ResearchComparisonAgent", "count": 1},
                                    {"agent_type": "HomeErrandsAgent", "count": 1},
                                ],
                            }
                        },
                    },
                    sort_keys=False,
                ),
                encoding="utf-8",
            )
            recovery_overrides = deep_merge_dicts(
                recovery_overrides,
                {
                    "agent": {
                        "workspace_dir": str(workspace_dir),
                    }
                },
            )
        if args.test in {18, 20}:
            workspace_dir = result_dir / "manager_workspace"
            workspace_dir.mkdir(parents=True, exist_ok=True)
            recovery_overrides = deep_merge_dicts(
                recovery_overrides,
                {
                    "llm": {
                        "context_window_tokens": 16000,
                        "max_output_tokens": 1024 if args.test == 20 else 128,
                    },
                    "agent": {
                        "workspace_dir": str(workspace_dir),
                        "max_tool_result_tokens_per_call": 1200,
                        "context_engine": {
                            "max_context_ratio": 0.85,
                            "reserved_output_tokens": 2000,
                            "hard_limit_behavior": "error",
                        },
                        "inner_loop_context": {
                            "max_total_tokens": 8000,
                            "warning_ratio": 0.50,
                            "aggressive_ratio": 0.70,
                            "preserve_recent_messages": 4,
                            "preserve_head_tokens": 120,
                            "preserve_tail_tokens": 80,
                            "offload_dir": "artifacts/tool_results",
                        },
                    },
                },
            )
        if args.test == 49:
            recovery_overrides = deep_merge_dicts(
                recovery_overrides,
                {
                    "llm": {
                        "context_window_tokens": 12000,
                    },
                    "agent": {
                        "conversation_summary_generator_type": "llm_summarizing",
                        "context_engine": {
                            "max_context_ratio": 0.20,
                            "reserved_output_tokens": 2000,
                            "hard_limit_behavior": "error",
                            "plugins": [
                                {
                                    "type": "lifecycle_recorder",
                                    "output_path": str(result_dir / "context_lifecycle.jsonl"),
                                }
                            ],
                            "thread_window": {
                                "recent_full_turns": 2,
                                "per_turn_summary_max_tokens": 225,
                                "max_turns_to_load": 50,
                                "omitted_notice_enabled": True,
                                "include_turn_ids": True,
                                "retrieval_tool_hint": True,
                            },
                        },
                    },
                },
            )
        if args.test == 50:
            workspace_dir = result_dir / "manager_workspace"
            workspace_dir.mkdir(parents=True, exist_ok=True)
            recovery_overrides = deep_merge_dicts(
                recovery_overrides,
                {
                    "llm": {
                        "context_window_tokens": 16000,
                    },
                    "agent": {
                        "workspace_dir": str(workspace_dir),
                        "tool_allowlist": [],
                        "conversation_summary_generator_type": "heuristic",
                        "conversation_compactor": {
                            "type": "heuristic_sliding_window",
                            "options": {
                                "trigger_ratio": 0.05,
                                "target_ratio": 0.03,
                                "min_recent_turns": 1,
                                "max_recent_turns": 1,
                                "trigger_turn_count": 2,
                            },
                        },
                        "context_engine": {
                            "max_context_ratio": 0.65,
                            "reserved_output_tokens": 3000,
                            "hard_limit_behavior": "recover_then_error",
                            "overflow_recovery": {
                                "enabled": True,
                                "max_preflight_attempts": 2,
                                "max_provider_retry_attempts": 1,
                            },
                            "thread_window": {
                                "recent_full_turns": 2,
                                "per_turn_summary_max_tokens": 250,
                                "max_turns_to_load": 60,
                                "omitted_notice_enabled": True,
                                "include_turn_ids": True,
                                "retrieval_tool_hint": True,
                            },
                        },
                    },
                },
            )
        manager_config_path = write_scenario_config(
            result_dir,
            f"scenario_{args.test}_managed_manager.yaml",
            recovery_overrides,
            base_config_path=args.config,
        )
        session_kwargs["auto_manager"] = {
            "agent_type": "CodeWritingAgent" if args.test == 50 else "TeamArchitect",
            "agent_id": f"managed-team-architect-{args.test}-{new_id_suffix()}",
            "config_path": manager_config_path,
        }
    async with ReliabilitySession(
        args.team_id,
        result_dir,
        args.timeout,
        env_path=args.env,
        storage_settings=storage_settings,
        db_path=db_path,
        **session_kwargs,
    ) as session:
        summary, details = await SCENARIO_MAP[args.test](session)
        persist_summary(
            result_dir=result_dir,
            test_number=args.test,
            title=scenario_name,
            summary=summary,
            details=details,
        )
        logger.info(summary)


if __name__ == "__main__":
    asyncio.run(main())
