#!/usr/bin/env python3
"""
Main entry point for starting NATS-backed Strands agents.

This module provides the entry point for launching agents using the
Strands Agents SDK.

Usage:
    python strands_main.py [--agent-type TYPE] [--team-id ID] [--is-lead]

Example:
    python strands_main.py --agent-type TeamArchitect --team-id team_303 --is-lead

Configuration:
    Settings are loaded from config.yaml (or specified via --config).
    Environment variables override config file values.
"""

import argparse
import asyncio
import json
import logging
import os
import signal
import sys
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

import yaml

try:
    from dotenv import load_dotenv
except ImportError:
    load_dotenv = None

from assistant_gateway.config import GatewayInstanceConfig
from assistant_gateway.runtime_config import (
    build_runtime_config_from_instance_config as _build_runtime_config_from_instance_config,
    is_instance_config_document,
    non_empty_env as _non_empty_env,
)
from storage.backends import (
    STORAGE_BACKEND_POSTGRES,
    STORAGE_BACKEND_SQLITE,
)
from default_values import AGENT_DEFAULTS, STORAGE_DEFAULTS
from config_loader import (
    apply_runtime_default_overrides,
    inspect_config_version,
    migrate_config_file,
    reload_config,
    resolve_config_file_path,
    resolve_task_history_db_path,
    resolve_agent_type_settings,
    set_config,
)
from runtime_settings.instance_layout import InstanceLayout
from access_control.caller_tokens import CALLER_MANAGER, agent_caller_label, with_own_caller_token
from runtime_settings.access_cutover import obsolete_binding_data, obsolete_binding_message
from runtime_settings.legacy_layout import legacy_layout_message, legacy_layout_moves
from env_secrets import materialize_secret_file_envs
from runtime_hosting.process_run import adopt_process_run
from storage.config import StorageSettings, resolve_storage_settings
from storage.runtime_state_repository import RuntimeRecoveryConflict

# Set up logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - pid=%(process)d - thread=%(threadName)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)


async def run_agent_with_signal_handlers(agent) -> None:
    """Run an agent while translating process signals into graceful exit."""

    loop = asyncio.get_running_loop()
    installed_signals: list[signal.Signals] = []

    def _request_exit(received_signal: signal.Signals) -> None:
        logger.info(
            "Received %s; requesting graceful agent shutdown. "
            "event=agent_shutdown_signal_received agent_id=%s",
            received_signal.name,
            getattr(agent, "id", "unknown"),
        )
        agent.set_exit_flag()

    for handled_signal in (signal.SIGTERM, signal.SIGINT):
        try:
            loop.add_signal_handler(
                handled_signal,
                _request_exit,
                handled_signal,
            )
        except (NotImplementedError, RuntimeError, ValueError) as exc:
            logger.warning(
                "Could not install graceful shutdown handler. "
                "event=agent_shutdown_signal_handler_unavailable signal=%s error=%s",
                handled_signal.name,
                exc,
            )
        else:
            installed_signals.append(handled_signal)
    try:
        await agent.connect()
    finally:
        for handled_signal in installed_signals:
            loop.remove_signal_handler(handled_signal)


@dataclass(frozen=True)
class OperatorContext:
    storage_settings: StorageSettings
    db_path: str
    team_id: Optional[str]
    named_agent_type: Optional[str]
    is_team_lead: bool
    owner_agent_name: Optional[str]


def _collect_descendant_task_ids(task_manager, task_id: str) -> list[str]:
    descendant_ids: list[str] = []
    for subtask_info in task_manager.get_subtasks(task_id):
        child_id = subtask_info.task.task_id
        descendant_ids.extend(_collect_descendant_task_ids(task_manager, child_id))
        descendant_ids.append(child_id)
    return descendant_ids






def _parse_task_state_timestamp(task_payload: dict, fallback_timestamp: str | None) -> datetime:
    task_state = task_payload.get("task_state") or []
    if task_state:
        latest_entry = task_state[-1]
        if isinstance(latest_entry, (list, tuple)) and len(latest_entry) >= 2 and latest_entry[1]:
            try:
                return datetime.fromisoformat(latest_entry[1])
            except ValueError:
                pass
    if fallback_timestamp:
        try:
            return datetime.fromisoformat(fallback_timestamp)
        except ValueError:
            pass
    return datetime.min.replace(tzinfo=timezone.utc)


def _resolve_operator_target_task_id(rows, requested_task_id: str | None) -> str:
    if requested_task_id:
        return requested_task_id

    current_task_ids: list[str] = []
    executing_candidates: list[tuple[datetime, str]] = []
    all_candidates: list[tuple[datetime, str]] = []

    for row in rows:
        snapshot = json.loads(row.runtime_state_json)
        tasks_by_id = {
            item.get("task_id"): item
            for item in snapshot.get("tasks", [])
            if item.get("task_id")
        }
        current_task_id = snapshot.get("current_in_progress_task_id")
        if current_task_id and current_task_id in tasks_by_id and current_task_id not in current_task_ids:
            current_task_ids.append(current_task_id)

        for task_id, item in tasks_by_id.items():
            task_timestamp = _parse_task_state_timestamp(item.get("task") or {}, row.updated_at)
            all_candidates.append((task_timestamp, task_id))
            state = item.get("state")
            if state in {"preparing", "in_progress", "retrying"}:
                executing_candidates.append((task_timestamp, task_id))

    if current_task_ids:
        return current_task_ids[-1]
    if executing_candidates:
        executing_candidates.sort()
        return executing_candidates[-1][1]
    if all_candidates:
        all_candidates.sort()
        return all_candidates[-1][1]
    raise RuntimeError("No tasks were present in the persisted active runtime snapshot.")


def _load_yaml_mapping(path: str | Path) -> dict:
    config_path = Path(path).expanduser().resolve()
    with config_path.open("r", encoding="utf-8") as handle:
        data = yaml.safe_load(handle) or {}
    if not isinstance(data, dict):
        raise ValueError(f"Expected a mapping config document in {config_path}.")
    return data


def _looks_like_instance_config(data: dict) -> bool:
    return isinstance(data, dict) and is_instance_config_document(data)


def _resolve_explicit_config_path(config_path: Optional[str]) -> Optional[Path]:
    if not config_path:
        return None
    return Path(config_path).expanduser().resolve()


def _discover_instance_config_path(args: argparse.Namespace) -> Optional[Path]:
    candidates: list[Path] = []

    explicit_instance_config = _non_empty_env("TEAM_INSTANCE_CONFIG")
    if explicit_instance_config:
        candidates.append(Path(explicit_instance_config).expanduser().resolve())

    team_instance_root = _non_empty_env("TEAM_INSTANCE_ROOT")
    if team_instance_root:
        candidates.append((Path(team_instance_root).expanduser() / "instance_config.yaml").resolve())

    instance_root = getattr(args, "instance_root", None)
    if instance_root:
        candidates.append((Path(instance_root).expanduser() / "instance_config.yaml").resolve())

    for candidate in candidates:
        if candidate.exists():
            return candidate
    return None


def _resolve_operator_storage_context(args: argparse.Namespace) -> OperatorContext:
    resolved_path = _resolve_explicit_config_path(args.config) or _discover_instance_config_path(args)
    if resolved_path is not None:
        raw_config = _load_yaml_mapping(resolved_path)
        if _looks_like_instance_config(raw_config):
            instance_config = GatewayInstanceConfig.load_for_process(resolved_path)
            storage_settings = instance_config.resolve_manager_storage_settings(env=os.environ)
            if args.task_history_db_path and storage_settings.backend == STORAGE_BACKEND_SQLITE:
                db_path = resolve_task_history_db_path(
                    args.task_history_db_path,
                    instance_root=str(instance_config.instance_root_path),
                )
                storage_settings = resolve_storage_settings(
                    {
                        "backend": STORAGE_DEFAULTS.backend.value,
                        STORAGE_BACKEND_SQLITE: {"path": db_path},
                    },
                    base_dir=str(instance_config.instance_root_path),
                    sqlite_fallback_path=db_path,
                )
            else:
                db_path = (
                    args.task_history_db_path
                    or storage_settings.sqlite_path
                    or resolve_task_history_db_path(
                        None,
                        instance_root=str(instance_config.instance_root_path),
                    )
                )
            return OperatorContext(
                storage_settings=storage_settings,
                db_path=db_path,
                team_id=instance_config.manager.team_id,
                named_agent_type=instance_config.manager.named_agent_type,
                is_team_lead=(instance_config.manager.named_agent_type == AGENT_DEFAULTS.named_agent_type.value),
                owner_agent_name=instance_config.manager.named_agent_type,
            )

    config_path = _ensure_config_ready(args.config)
    if config_path is not None:
        cfg = reload_config(config_path, search_default_paths=False)
    else:
        cfg = reload_config(None, search_default_paths=False)
    agent_runtime_settings = resolve_agent_type_settings(cfg, args.agent_type)
    storage_backend = agent_runtime_settings.get('storage_backend', STORAGE_DEFAULTS.backend.value)
    storage_sqlite_path = agent_runtime_settings.get('storage_sqlite_path')
    storage_postgres_dsn = agent_runtime_settings.get('storage_postgres_dsn')
    storage_sqlite_vector = {
        "enabled": agent_runtime_settings.get(
            "storage_sqlite_vector_enabled",
            STORAGE_DEFAULTS.sqlite_vector_enabled.value,
        ),
        "provider": agent_runtime_settings.get(
            "storage_sqlite_vector_provider",
            STORAGE_DEFAULTS.sqlite_vector_provider.value,
        ),
        "require_extension": agent_runtime_settings.get(
            "storage_sqlite_vector_require_extension",
            STORAGE_DEFAULTS.sqlite_vector_require_extension.value,
        ),
    }
    instance_root = getattr(args, 'instance_root', None) or agent_runtime_settings.get('instance_root')
    task_history_db_path = resolve_task_history_db_path(
        args.task_history_db_path or storage_sqlite_path or agent_runtime_settings['task_history_db_path'],
        instance_root=instance_root,
    )
    storage_settings = resolve_storage_settings(
        {
            "backend": storage_backend,
            STORAGE_BACKEND_SQLITE: {"path": task_history_db_path, "vector": storage_sqlite_vector},
            STORAGE_BACKEND_POSTGRES: {"dsn": storage_postgres_dsn},
        },
        base_dir=instance_root,
        sqlite_fallback_path=task_history_db_path,
    )
    return OperatorContext(
        storage_settings=storage_settings,
        db_path=task_history_db_path,
        team_id=args.team_id or cfg.get('agent.default_team_id', AGENT_DEFAULTS.default_team_id.value),
        named_agent_type=args.agent_type,
        is_team_lead=bool(args.is_lead),
        owner_agent_name=args.agent_type,
    )


def _verified_deferred_task_owner(context: OperatorContext, task_id: str, msg_meta: Any) -> Optional[str]:
    """The person a deferred task verifiably works for, or None (logged).

    No agent admitted this task, so nothing recorded its owner. The operator
    decides it here like admission does: only from the task's signed context
    or schedule run identity, checked by the one resolver
    (``access_control.task_identity.resolve_task_identity``) with the
    verifying keys in this process's environment. The person the message
    names is never used; an expired or unverifiable identity archives the
    task for nobody.
    """

    from access_control.identity import ContextVerifier, IdentityRejected
    from access_control.task_identity import ScheduleRunLookup, resolve_task_identity

    gateway_context = getattr(msg_meta, "gateway_context", None)
    try:
        return resolve_task_identity(
            verifier=ContextVerifier.from_env(),
            # An empty team id refuses every context instead of skipping the team check.
            team_id=str(context.team_id or ""),
            task_context=getattr(gateway_context, "task_context", None),
            schedule_run=getattr(gateway_context, "schedule_run", None),
            schedule_runs=ScheduleRunLookup.from_storage(context.storage_settings),
        ).person_id
    except (IdentityRejected, ValueError) as exc:
        logger.warning(
            "A deferred task the operator failed has no verifiable person; it is archived for nobody. "
            "event=operator_failed_task_owner_unverified task_id=%s claimed_person_id=%s reason=%s",
            task_id,
            getattr(gateway_context, "canonical_user_id", None),
            exc,
        )
        return None


def _archive_failed_envelope_task(
    *,
    context: OperatorContext,
    owner_agent_id: str,
    owner_agent_name: Optional[str],
    task_request,
    msg_header,
    msg_meta,
    reason: str,
) -> str:
    from custom_agents.task_manager import TaskManager

    task_manager = TaskManager(
        db_path=context.db_path,
        logger=logging.getLogger("TaskRuntimeOperator"),
        storage_settings=context.storage_settings,
    )
    task_manager.set_owner_identity(
        owner_agent_id,
        owner_agent_name or context.owner_agent_name,
        context.team_id,
        context.named_agent_type,
    )
    added = task_manager.add_task(
        task_request,
        msg_header,
        msg_meta,
        owner_person_id=_verified_deferred_task_owner(context, task_request.task_id, msg_meta),
    )
    if not added:
        raise RuntimeError(f"Failed to admit operator-selected task {task_request.task_id} into the archive path.")
    now_ts = datetime.now(timezone.utc).isoformat()
    task_manager.fail_task(task_request.task_id, reason, now_ts)
    return task_request.task_id


def _fail_latest_deferred_task(
    *,
    context: OperatorContext,
    owner_agent_id: str,
    reason: str,
) -> dict[str, object]:
    from custom_messages.contracts.envelope_v1 import MessageEnvelope
    from custom_messages.contracts.routing_v1 import MessageMeta
    from custom_messages.contracts.task_v1 import Task
    from custom_messages.enums import MessageMetaType
    from custom_agents.task_manager import TaskManager

    task_manager = TaskManager(
        db_path=context.db_path,
        logger=logging.getLogger("TaskRuntimeOperator"),
        storage_settings=context.storage_settings,
    )
    rows = task_manager.runtime_state_store.load_deferred_messages_by_owner(owner_agent_id)
    if not rows:
        raise RuntimeError(f"No persisted deferred message snapshot found for owner_agent_id={owner_agent_id}.")

    row = rows[0]
    try:
        restored = json.loads(row.messages_json)
        envelopes = [MessageEnvelope.model_validate(item) for item in restored]
    except Exception as exc:
        raise RuntimeError(f"Failed to parse deferred message snapshot for owner_agent_id={owner_agent_id}: {exc}") from exc

    selected_index = None
    selected_envelope = None
    for idx in range(len(envelopes) - 1, -1, -1):
        envelope = envelopes[idx]
        if isinstance(envelope.msg_content, Task) and envelope.msg_content.task_type == MessageMetaType.REQUEST:
            selected_index = idx
            selected_envelope = envelope
            break

    if selected_index is None or selected_envelope is None:
        raise RuntimeError(
            f"No deferred Task request was present for owner_agent_id={owner_agent_id}; nothing to fail."
        )

    remaining = [envelope for idx, envelope in enumerate(envelopes) if idx != selected_index]
    failed_task_id = _archive_failed_envelope_task(
        context=context,
        owner_agent_id=owner_agent_id,
        owner_agent_name=row.owner_agent_name,
        task_request=selected_envelope.msg_content,
        msg_header=selected_envelope.msg_header,
        msg_meta=selected_envelope.msg_meta or MessageMeta(),
        reason=reason,
    )
    # Archive the terminal outcome before releasing the durable envelope.
    # A crash or conflicting update leaves the source available for inspection.
    task_manager.runtime_state_store.commit_deferred_messages_recovery(
        sources=rows,
        replacement=replace(
            row,
            messages_json=json.dumps([envelope.model_dump(mode="json") for envelope in remaining]),
        ),
    )
    return {
        "source": "deferred",
        "owner_agent_id": owner_agent_id,
        "task_id": failed_task_id,
        "queue_name": None,
        "reason": reason,
    }


def _fail_latest_task(
    *,
    context: OperatorContext,
    owner_agent_id: str,
    reason: str,
) -> dict[str, object]:
    try:
        result = _fail_persisted_runtime_task(
            storage_settings=context.storage_settings,
            db_path=context.db_path,
            owner_agent_id=owner_agent_id,
            task_id=None,
            reason=reason,
        )
        result["source"] = "runtime"
        result["queue_name"] = None
        return result
    except RuntimeRecoveryConflict:
        raise
    except RuntimeError as exc:
        logger.info("No persisted runtime task to fail for %s: %s", owner_agent_id, exc)

    try:
        return _fail_latest_deferred_task(
            context=context,
            owner_agent_id=owner_agent_id,
            reason=reason,
        )
    except RuntimeRecoveryConflict:
        raise
    except RuntimeError as exc:
        logger.info("No deferred task snapshot to fail for %s: %s", owner_agent_id, exc)

    raise RuntimeError(
        "No pending Task request was found in persisted runtime or deferred snapshots "
        f"for owner_agent_id={owner_agent_id}."
    )


def _fail_persisted_runtime_task(
    *,
    storage_settings,
    db_path: str,
    owner_agent_id: str,
    task_id: str | None,
    reason: str,
) -> dict[str, object]:
    from custom_agents.task_manager import TaskManager

    task_manager = TaskManager(
        db_path=db_path,
        logger=logging.getLogger("TaskRuntimeOperator"),
        storage_settings=storage_settings,
    )
    rows = task_manager.runtime_state_store.load_task_runtime_by_owner(owner_agent_id)
    if not rows:
        raise RuntimeError(f"No persisted active runtime snapshot found for owner_agent_id={owner_agent_id}.")

    resolved_task_id = _resolve_operator_target_task_id(rows, task_id)
    task_found = False
    row_owner_agent_name = rows[0].owner_agent_name
    row_team_id = rows[0].team_id
    row_owner_named_agent_type = rows[0].owner_named_agent_type
    for row in rows:
        try:
            snapshot = json.loads(row.runtime_state_json)
        except json.JSONDecodeError as exc:
            raise RuntimeError(f"Failed to parse runtime snapshot for owner_agent_id={owner_agent_id}: {exc}") from exc
        if any(item.get("task_id") == resolved_task_id for item in snapshot.get("tasks", [])):
            task_found = True
            break

    if not task_found:
        raise RuntimeError(
            f"Task {resolved_task_id} was not present in the persisted active runtime snapshot for owner_agent_id={owner_agent_id}."
        )

    task_manager.set_owner_identity(
        owner_agent_id,
        row_owner_agent_name,
        row_team_id,
        row_owner_named_agent_type,
    )
    restored = task_manager.restore_runtime_snapshot_rows(rows)
    if not restored:
        raise RuntimeError(
            f"Failed to restore persisted runtime snapshot for owner_agent_id={owner_agent_id} before operator failure."
        )

    task_info = task_manager.get_task_info(resolved_task_id)
    if task_info is None:
        raise RuntimeError(
            f"Task {resolved_task_id} disappeared during restore for owner_agent_id={owner_agent_id}; runtime snapshot may be inconsistent."
        )

    now_ts = datetime.now(timezone.utc).isoformat()
    descendant_ids = _collect_descendant_task_ids(task_manager, resolved_task_id)
    descendant_reason = (
        f"Operator failed ancestor task {resolved_task_id}. Descendant task removed from active runtime state. Root reason: {reason}"
    )
    for descendant_id in descendant_ids:
        if task_manager.get_task_info(descendant_id) is not None:
            task_manager.fail_task(descendant_id, descendant_reason, now_ts)

    task_manager.fail_task(resolved_task_id, reason, now_ts)

    return {
        "owner_agent_id": owner_agent_id,
        "owner_agent_name": row_owner_agent_name,
        "team_id": row_team_id,
        "owner_named_agent_type": row_owner_named_agent_type,
        "task_id": resolved_task_id,
        "failed_descendant_task_ids": descendant_ids,
        "reason": reason,
        "requested_task_id": task_id,
    }


def build_parser() -> argparse.ArgumentParser:
    """Build the shared CLI parser for Strands agent launch commands."""
    parser = argparse.ArgumentParser(
        description='Start a NATS-backed Strands agent'
    )
    parser.add_argument(
        '--agent-type',
        type=str,
        default=AGENT_DEFAULTS.named_agent_type.value,
        help='Named agent type to instantiate (default: TeamArchitect)'
    )
    parser.add_argument(
        '--team-id',
        type=str,
        default=None,
        help=f'Team ID (default: from config or {AGENT_DEFAULTS.default_team_id.value})'
    )
    parser.add_argument(
        '--is-lead',
        action='store_true',
        help='Whether this agent is the team leader'
    )
    parser.add_argument(
        '--model-provider',
        type=str,
        default=None,
        choices=['openai', 'gemini', 'bedrock', 'anthropic', 'xai', 'litellm'],
        help='LLM provider to use (default: from config)'
    )
    parser.add_argument(
        '--model-id',
        type=str,
        default=None,
        help='Model ID to use (default: from config)'
    )
    parser.add_argument(
        '--config',
        type=str,
        default=None,
        help='Path to configuration file (default: config.yaml)'
    )
    parser.add_argument(
        '--agent-id',
        type=str,
        default=None,
        help='Agent ID for NATS routing (default: from config or auto-generated)'
    )
    parser.add_argument(
        '--env',
        type=str,
        default=None,
        help='Path to a .env file to load (e.g. debug.env)'
    )
    parser.add_argument(
        '--task-history-db-path',
        type=str,
        default=None,
        help='Override SQLite task history DB path (default: from config, env, or dbs/task_history.db)'
    )
    parser.add_argument(
        '--instance-root',
        type=str,
        default=None,
        help='Instance root; control/, dbs/, state/ and workspaces/ are derived from it (default: from config)'
    )
    parser.add_argument(
        '--agent-result-transformer',
        type=str,
        default=None,
        help="Optional module:function hook to post-process AgentResult objects before parsing.",
    )
    parser.add_argument(
        '--migrate-config',
        action='store_true',
        help='Inspect or migrate config.yaml to the latest supported schema and exit.',
    )
    parser.add_argument(
        '--fail-runtime-task',
        nargs='?',
        const='',
        default=None,
        help=(
            'Operator action: fail one persisted active runtime task and exit. '
            'Provide a task_id to target a specific task, or omit the value to fail the current persisted runtime task. '
            'Use this only while the owning agent process is stopped.'
        ),
    )
    parser.add_argument(
        '--fail-latest-task',
        action='store_true',
        help=(
            'Operator action: fail the latest recoverable task for this agent and exit. '
            'This checks active runtime state first, then deferred-message snapshots, '
            'then the next pending broker task from the queues the agent would consume on startup. '
            'Use this only while the owning agent process is stopped.'
        ),
    )
    parser.add_argument(
        '--owner-agent-id',
        type=str,
        default=None,
        help='Owner agent_id for --fail-runtime-task / --fail-latest-task. Required because persisted runtime state is stored per owner agent.',
    )
    parser.add_argument(
        '--fail-reason',
        type=str,
        default=None,
        help='Optional operator-visible failure reason for --fail-runtime-task / --fail-latest-task.',
    )
    parser.add_argument(
        '--yes',
        action='store_true',
        help='Apply config migration without prompting when used with --migrate-config.',
    )
    return parser


def _migrate_config_interactive(config_path: str | None, *, assume_yes: bool = False) -> None:
    resolved_path = resolve_config_file_path(config_path, search_default_paths=bool(config_path))
    if resolved_path is None:
        raise SystemExit("No config.yaml file found to migrate.")
    status = inspect_config_version(resolved_path)
    print(f"Config: {status.path}")
    print(f"Detected schema_version: {status.detected_version}")
    print(f"Latest schema_version: {status.latest_version}")
    print(
        "Directly compatible schema versions: "
        + ", ".join(str(version) for version in status.directly_compatible_versions)
    )
    if not status.needs_migration:
        print("No migration required.")
        return
    if not assume_yes:
        if not sys.stdin.isatty() or not sys.stdout.isatty():
            raise SystemExit("Migration required. Re-run with --migrate-config --yes to apply it non-interactively.")
        raw = input("Apply migration now? [Y/n]: ").strip().lower()
        if raw not in {"", "y", "yes"}:
            raise SystemExit("Migration declined.")
    result = migrate_config_file(status.path)
    print(f"Migrated to schema_version {result.migrated_version}")
    if result.backup_path is not None:
        print(f"Backup written to {result.backup_path}")
    for step in result.applied_steps:
        print(f"- {step}")


def _ensure_config_ready(config_path: str | None) -> Optional[str]:
    if config_path is None:
        return None
    resolved_path = resolve_config_file_path(config_path, search_default_paths=False)
    if resolved_path is None:
        return config_path
    status = inspect_config_version(resolved_path)
    if not status.needs_migration:
        return str(resolved_path)
    if not sys.stdin.isatty() or not sys.stdout.isatty():
        raise SystemExit(
            f"{status.path} is schema_version {status.detected_version}, but latest is "
            f"{status.latest_version}. Run `strands_main.py --config {status.path} --migrate-config --yes` first."
        )
    raw = input(
        f"Config {status.path} is schema_version {status.detected_version}; latest is "
        f"{status.latest_version}. Migrate now? [Y/n]: "
    ).strip().lower()
    if raw not in {"", "y", "yes"}:
        raise SystemExit("Config migration declined.")
    result = migrate_config_file(status.path)
    print(f"Migrated {result.path} from schema_version {result.original_version} to {result.migrated_version}.")
    if result.backup_path is not None:
        print(f"Backup written to {result.backup_path}")
    for step in result.applied_steps:
        print(f"- {step}")
    return str(status.path)


def run_with_args(args: argparse.Namespace) -> None:
    """Start a NATS-backed Strands agent from parsed CLI args."""
    if args.env and load_dotenv:
        load_dotenv(args.env)
        logger.info(f"Loaded environment variables from {args.env}")
    materialize_secret_file_envs()
    # Started by hand with the broker's root secret, this process derives its
    # own caller token; under the supervisor the derived token is kept.
    with_own_caller_token(
        os.environ,  # type: ignore[arg-type]
        CALLER_MANAGER if getattr(args, "is_lead", False) else agent_caller_label(str(args.agent_type)),
    )

    if getattr(args, 'migrate_config', False):
        _migrate_config_interactive(args.config, assume_yes=getattr(args, 'yes', False))
        return

    if getattr(args, 'fail_runtime_task', None) is not None and getattr(args, 'fail_latest_task', False):
        raise SystemExit("--fail-runtime-task and --fail-latest-task are mutually exclusive.")

    if getattr(args, 'fail_runtime_task', None) is not None:
        if not getattr(args, 'owner_agent_id', None):
            raise SystemExit("--owner-agent-id is required with --fail-runtime-task.")
        operator_context = _resolve_operator_storage_context(args)
        reason = (
            args.fail_reason
            or (
                f"Operator manually failed persisted runtime task {args.fail_runtime_task}."
                if args.fail_runtime_task
                else "Operator manually failed the current persisted runtime task."
            )
        )
        result = _fail_persisted_runtime_task(
            storage_settings=operator_context.storage_settings,
            db_path=operator_context.db_path,
            owner_agent_id=args.owner_agent_id,
            task_id=(args.fail_runtime_task or None),
            reason=reason,
        )
        print(f"owner_agent_id={result['owner_agent_id']}")
        print(f"task_id={result['task_id']}")
        print(f"task_id_inferred={result['requested_task_id'] is None}")
        print(f"failed_descendants={len(result['failed_descendant_task_ids'])}")
        print(f"reason={result['reason']}")
        return

    if getattr(args, 'fail_latest_task', False):
        if not getattr(args, 'owner_agent_id', None):
            raise SystemExit("--owner-agent-id is required with --fail-latest-task.")
        operator_context = _resolve_operator_storage_context(args)
        reason = (
            args.fail_reason
            or f"Operator manually failed the latest recoverable task for owner_agent_id={args.owner_agent_id}."
        )
        result = _fail_latest_task(
            context=operator_context,
            owner_agent_id=args.owner_agent_id,
            reason=reason,
        )
        print(f"owner_agent_id={result['owner_agent_id']}")
        print(f"task_id={result['task_id']}")
        print(f"source={result['source']}")
        if result.get("queue_name"):
            print(f"queue_name={result['queue_name']}")
        print(f"reason={result['reason']}")
        return

    resolved_path = _resolve_explicit_config_path(args.config)
    if resolved_path is None:
        resolved_path = _discover_instance_config_path(args)
        if resolved_path is not None:
            logger.info("Auto-discovered onboarded instance config: %s", resolved_path)

    if resolved_path is not None and resolved_path.exists():
        raw_config = _load_yaml_mapping(resolved_path)
        if _looks_like_instance_config(raw_config):
            logger.info("Loading manager runtime settings from instance_config.yaml: %s", resolved_path)
            cfg = _build_runtime_config_from_instance_config(GatewayInstanceConfig.load_for_process(resolved_path))
            set_config(cfg)
        else:
            config_path = _ensure_config_ready(args.config)
            cfg = reload_config(config_path, search_default_paths=False)
    else:
        config_path = _ensure_config_ready(args.config)
        if config_path is not None:
            cfg = reload_config(config_path, search_default_paths=False)
        else:
            logger.info("No explicit config or onboarded instance_config.yaml discovered; using environment defaults only.")
            cfg = reload_config(None, search_default_paths=False)

    agent_runtime_settings = resolve_agent_type_settings(cfg, args.agent_type)
    storage_backend = agent_runtime_settings.get('storage_backend', STORAGE_DEFAULTS.backend.value)
    storage_sqlite_path = agent_runtime_settings.get('storage_sqlite_path')
    storage_postgres_dsn = agent_runtime_settings.get('storage_postgres_dsn')
    storage_sqlite_vector = {
        "enabled": agent_runtime_settings.get(
            "storage_sqlite_vector_enabled",
            STORAGE_DEFAULTS.sqlite_vector_enabled.value,
        ),
        "provider": agent_runtime_settings.get(
            "storage_sqlite_vector_provider",
            STORAGE_DEFAULTS.sqlite_vector_provider.value,
        ),
        "require_extension": agent_runtime_settings.get(
            "storage_sqlite_vector_require_extension",
            STORAGE_DEFAULTS.sqlite_vector_require_extension.value,
        ),
    }

    # Get LLM settings from config/env (can be overridden by CLI for the explicitly started agent)
    from custom_agents.strands_model_factory import normalize_model_provider
    from custom_agents.xai_oauth_auth import resolve_xai_oauth_model_id

    configured_model_provider = normalize_model_provider(agent_runtime_settings['model_provider'])
    model_provider = normalize_model_provider(args.model_provider or configured_model_provider)
    model_id = resolve_xai_oauth_model_id(
        model_provider,
        args.model_id,
        inherited_provider=configured_model_provider,
        inherited_model_id=agent_runtime_settings['model_id'],
    )
    temperature = agent_runtime_settings['temperature']

    # Get team ID from CLI or config
    team_id = args.team_id or cfg.get('agent.default_team_id', AGENT_DEFAULTS.default_team_id.value)
    layout = InstanceLayout.at(
        getattr(args, 'instance_root', None) or agent_runtime_settings['instance_root']
    )
    instance_root = str(layout.root)
    workspace_dir = str(layout.workspaces_dir)
    legacy_moves = legacy_layout_moves(layout.root)
    if legacy_moves:
        logger.error("Agent start refused. %s", legacy_layout_message(layout.root, legacy_moves))
        sys.exit(2)
    obsolete_bindings = obsolete_binding_data(layout.root)
    if obsolete_bindings.present:
        logger.error("Agent start refused. %s", obsolete_binding_message(obsolete_bindings))
        sys.exit(2)
    skills_dir = str(layout.skills_dir)
    task_history_db_path = resolve_task_history_db_path(
        args.task_history_db_path or storage_sqlite_path or agent_runtime_settings['task_history_db_path'],
        instance_root=instance_root,
    )
    # Validate backend/path combinations before constructing the agent config.
    _ = resolve_storage_settings(
        {
            "backend": storage_backend,
            STORAGE_BACKEND_SQLITE: {"path": task_history_db_path, "vector": storage_sqlite_vector},
            STORAGE_BACKEND_POSTGRES: {"dsn": storage_postgres_dsn},
        },
        base_dir=instance_root,
        sqlite_fallback_path=task_history_db_path,
    )
    llm_override_path = str(layout.llm_overrides_path)
    team_templates_path = str(layout.team_templates_path)
    agent_result_transformer = (
        getattr(args, 'agent_result_transformer', None)
        or agent_runtime_settings['agent_result_transformer_spec']
    )
    litellm_api_base = agent_runtime_settings.get('litellm_api_base')

    apply_runtime_default_overrides(
        {
            'transport': {
                'type': 'nats',
                'nats_url': agent_runtime_settings.get('nats_url'),
                'nats_servers': agent_runtime_settings.get('nats_servers'),
                'nats_name': agent_runtime_settings.get('nats_name'),
                'jetstream_enabled': agent_runtime_settings.get('nats_jetstream_enabled'),
                'subject_prefix': agent_runtime_settings.get('nats_subject_prefix'),
                'storage': agent_runtime_settings.get('nats_storage'),
                'replicas': agent_runtime_settings.get('nats_replicas'),
                'ack_wait_seconds': agent_runtime_settings.get('nats_ack_wait_seconds'),
                'max_deliver': agent_runtime_settings.get('nats_max_deliver'),
                'duplicate_window_seconds': agent_runtime_settings.get('nats_duplicate_window_seconds'),
                'work_max_age_seconds': agent_runtime_settings.get('nats_work_max_age_seconds'),
                'presence_max_age_seconds': agent_runtime_settings.get('nats_presence_max_age_seconds'),
                'process_consumer_inactive_threshold_seconds': agent_runtime_settings.get(
                    'nats_process_consumer_inactive_threshold_seconds'
                ),
                'nats_user': agent_runtime_settings.get('nats_user'),
                'nats_password': agent_runtime_settings.get('nats_password'),
                'credentials_file': agent_runtime_settings.get('nats_credentials_file'),
            },
            'llm': {
                'provider': model_provider,
                'model_id': model_id,
                'temperature': temperature,
                'reasoning_effort': agent_runtime_settings.get('reasoning_effort'),
                'reasoning_summary': agent_runtime_settings.get('reasoning_summary'),
                'max_output_tokens': agent_runtime_settings.get('max_output_tokens'),
                'context_window_tokens': agent_runtime_settings.get('context_window_tokens'),
                'stream': agent_runtime_settings.get('stream'),
                'litellm_api_mode': agent_runtime_settings.get('litellm_api_mode'),
                'supports_image_input': agent_runtime_settings.get('supports_image_input'),
                'supports_audio_input': agent_runtime_settings.get('supports_audio_input'),
                'supports_file_input': agent_runtime_settings.get('supports_file_input'),
                'reasoning_trace_retention_messages': agent_runtime_settings.get(
                    'reasoning_trace_retention_messages'
                ),
                'compaction_model_provider': agent_runtime_settings.get('compaction_model_provider'),
                'compaction_model_id': agent_runtime_settings.get('compaction_model_id'),
                'litellm_api_base': litellm_api_base,
                'openai_codex': agent_runtime_settings.get('openai_codex'),
                'xai_oauth': agent_runtime_settings.get('xai_oauth'),
            },
            'embedding': {
                'provider': agent_runtime_settings.get('embedding_provider'),
                'model_id': agent_runtime_settings.get('embedding_model_id'),
                'api_base': agent_runtime_settings.get('embedding_api_base'),
                'dimensions': agent_runtime_settings.get('embedding_dimensions'),
                'timeout_seconds': agent_runtime_settings.get('embedding_timeout_seconds'),
                'organization': agent_runtime_settings.get('embedding_organization'),
                'extra_headers': agent_runtime_settings.get('embedding_extra_headers'),
            },
            'agent': {
                'default_team_id': team_id,
                'default_request_timezone': agent_runtime_settings.get('default_request_timezone'),
                'instance_root': instance_root,
                'instance_config_path': agent_runtime_settings.get('instance_config_path'),
                'bootstrap': agent_runtime_settings.get('bootstrap_config'),
                'result_transformer': agent_result_transformer,
                'memory_graph_enabled': agent_runtime_settings.get('memory_graph_enabled', False),
                'max_queued_top_level_tasks': agent_runtime_settings['max_queued_top_level_tasks'],
                'max_concurrent_top_level_tasks': agent_runtime_settings['max_concurrent_top_level_tasks'],
                'task_queue_warning_threshold': agent_runtime_settings.get('task_queue_warning_threshold'),
                'subtask_timeout_seconds': agent_runtime_settings.get('subtask_timeout_seconds'),
                'subtask_watchdog_interval_seconds': agent_runtime_settings.get('subtask_watchdog_interval_seconds'),
                'subtask_status_probe_timeout_seconds': agent_runtime_settings.get('subtask_status_probe_timeout_seconds'),
                'restart_executing_task_grace_seconds': agent_runtime_settings.get('restart_executing_task_grace_seconds'),
                'same_role_runtime_takeover_enabled': agent_runtime_settings.get('same_role_runtime_takeover_enabled'),
                'max_tool_calls_per_task': agent_runtime_settings['max_tool_calls_per_task'],
                'max_tool_result_tokens_per_call': agent_runtime_settings['max_tool_result_tokens_per_call'],
                'runtime_budget_policy': agent_runtime_settings.get('runtime_budget_policy'),
                'turn_action_outbox': agent_runtime_settings.get('turn_action_outbox'),
                'runtime_coordinator': agent_runtime_settings.get('runtime_coordinator'),
                'mid_turn_steering': agent_runtime_settings.get('mid_turn_steering'),
                'inner_loop_context': agent_runtime_settings.get('inner_loop_context'),
                'context_engine': agent_runtime_settings.get('context_engine'),
                'debug': agent_runtime_settings.get('debug'),
                'structured_output_failure_threshold': agent_runtime_settings.get('structured_output_failure_threshold'),
                'deferred_message_warning_threshold': agent_runtime_settings.get('deferred_message_warning_threshold'),
                'conversation_compactor': agent_runtime_settings.get('conversation_compactor'),
                'conversation_summary_generator_type': agent_runtime_settings.get('conversation_summary_generator_type'),
                'tool_allowlist': agent_runtime_settings.get('tool_allowlist'),
                'tool_blocklist': agent_runtime_settings.get('tool_blocklist'),
                'external_mcp_servers': agent_runtime_settings.get('external_mcp_servers'),
                'prompt_skill_description_exclusions': agent_runtime_settings.get('prompt_skill_description_exclusions'),
                'prompt_skill_description_limit': agent_runtime_settings.get('prompt_skill_description_limit'),
            },
            'storage': {
                'backend': storage_backend,
                'sqlite': {
                    'path': task_history_db_path,
                    'vector': storage_sqlite_vector,
                },
                'postgres': {
                    'dsn': storage_postgres_dsn,
                },
            },
            'security_layer': {
                'mode': agent_runtime_settings.get('security_layer_mode'),
                'broker_internal_base_url': agent_runtime_settings.get('secure_tool_broker_internal_base_url'),
                'broker_public_port': agent_runtime_settings.get('secure_tool_broker_public_port'),
            },
            'network': {
                'public_origin': agent_runtime_settings.get('network_public_origin'),
            },
            'mcp_profiles': cfg.get('mcp_profiles', {}),
            'mcp': agent_runtime_settings.get('mcp_config', {}),
        }
    )

    logger.info('Configuration loaded:')
    logger.info("  Transport: nats")
    logger.info(f"  NATS Servers: {', '.join(agent_runtime_settings.get('nats_servers') or [])}")

    # Model keys are looked up at the first model call (custom_agents.model_keys):
    # the key saved with /auth model, or an environment override. The broker
    # starts after the agents, so a missing key cannot be checked here; the
    # first request says plainly which key is missing, and the Status page
    # lists configured endpoints without one.
    if model_provider == 'bedrock':
        logger.info('Using AWS credentials for Bedrock')
    elif model_provider == 'litellm':
        # The endpoint's key comes from LITELLM_API_KEY or the broker's stored
        # key, looked up at the first model call (custom_agents.model_keys):
        # the broker starts after the agents, so it cannot be checked here.
        effective_litellm_api_base = litellm_api_base or os.getenv('LITELLM_API_BASE')
        if not effective_litellm_api_base:
            logger.warning('LITELLM_API_BASE not set - LiteLLM will use default endpoints')
        else:
            logger.info(f'Using the OpenAI-compatible endpoint at: {effective_litellm_api_base}')
    elif model_provider == 'openai-codex':
        from custom_agents.openai_codex_auth import OpenAICodexAuthError, resolve_openai_codex_credentials

        try:
            credentials = resolve_openai_codex_credentials(
                agent_runtime_settings.get('openai_codex'),
                instance_root=instance_root,
            )
            logger.info("Using OpenAI Codex OAuth credentials from %s", credentials.source)
        except OpenAICodexAuthError as exc:
            logger.error("%s", exc)
            sys.exit(1)
    elif model_provider == 'xai-oauth':
        from custom_agents.xai_oauth_auth import XAIOAuthError, resolve_xai_oauth_credentials

        try:
            credentials = resolve_xai_oauth_credentials(
                agent_runtime_settings.get('xai_oauth'),
                instance_root=instance_root,
            )
            logger.info("Using xAI OAuth credentials from %s", credentials.source)
        except XAIOAuthError as exc:
            logger.error("%s", exc)
            sys.exit(1)

    # Import here to avoid circular imports
    from custom_agents.strands_team import (
        AGENT_TYPE_MAP,
        create_strands_agent,
    )
    from custom_tools.runtime_context import set_agent_ref

    # Validate agent type
    if args.agent_type not in AGENT_TYPE_MAP:
        logger.error(f'Unknown agent type: {args.agent_type}')
        logger.error(f'Available types: {list(AGENT_TYPE_MAP.keys())}')
        sys.exit(1)

    # Get configuration for this agent type
    agent_config = AGENT_TYPE_MAP[args.agent_type].copy()
    agent_config['team_id'] = team_id
    agent_config['is_team_lead'] = args.is_lead

    # Apply runtime settings resolved from generic defaults + optional per-agent overrides
    agent_config.update(agent_runtime_settings)
    if not args.is_lead:
        agent_config["bootstrap_config"] = {}
    agent_config['model_provider'] = model_provider
    agent_config['model_id'] = model_id
    agent_config['temperature'] = temperature
    agent_config['reasoning_effort'] = agent_runtime_settings.get('reasoning_effort')
    agent_config['reasoning_summary'] = agent_runtime_settings.get('reasoning_summary')
    agent_config['max_output_tokens'] = agent_runtime_settings.get('max_output_tokens')
    agent_config['stream'] = agent_runtime_settings.get('stream')
    agent_config['litellm_api_mode'] = agent_runtime_settings.get('litellm_api_mode')
    agent_config['reasoning_trace_retention_messages'] = agent_runtime_settings.get(
        'reasoning_trace_retention_messages'
    )

    # Apply LiteLLM proxy settings
    if litellm_api_base:
        agent_config['litellm_api_base'] = litellm_api_base
    if agent_runtime_settings.get('openai_codex'):
        agent_config['openai_codex'] = agent_runtime_settings.get('openai_codex')
    if agent_runtime_settings.get('xai_oauth'):
        agent_config['xai_oauth'] = agent_runtime_settings.get('xai_oauth')

    # Apply agent ID from CLI, config, or let it auto-generate
    agent_id = args.agent_id or cfg.get('agent.agent_id')
    if agent_id:
        agent_config['id'] = agent_id
    agent_config['task_history_db_path'] = task_history_db_path
    agent_config['storage_backend'] = storage_backend
    agent_config['storage_sqlite_path'] = task_history_db_path
    agent_config['storage_sqlite_vector_enabled'] = storage_sqlite_vector["enabled"]
    agent_config['storage_sqlite_vector_provider'] = storage_sqlite_vector["provider"]
    agent_config['storage_sqlite_vector_require_extension'] = storage_sqlite_vector["require_extension"]
    agent_config['storage_postgres_dsn'] = storage_postgres_dsn
    agent_config['instance_config_path'] = agent_runtime_settings.get('instance_config_path')
    agent_config['skills_dir'] = skills_dir
    agent_config['instance_root'] = instance_root
    agent_config['workspace_dir'] = workspace_dir
    agent_config['llm_override_path'] = llm_override_path
    agent_config['team_templates_path'] = team_templates_path
    agent_config['agent_result_transformer_spec'] = agent_result_transformer
    agent_config['debug'] = agent_runtime_settings.get('debug')

    logger.info(f'Starting {args.agent_type} agent...')
    logger.info(f'  Team ID: {team_id}')
    logger.info(f'  Is Leader: {args.is_lead}')
    logger.info(f'  Agent ID: {agent_id or "(auto-generated)"}')
    logger.info(f'  Storage Backend: {storage_backend}')
    logger.info(f'  Task History DB: {task_history_db_path}')
    logger.info(f'  Agent Result Transformer: {agent_result_transformer or "(none)"}')
    logger.info(f'  Model Provider: {model_provider}')
    logger.info(f'  Model ID: {model_id}')

    try:
        # Create the agent
        agent = create_strands_agent(agent_config)

        # Set agent reference for tools
        set_agent_ref(agent)

        logger.info(f'Agent created with ID: {agent.id}')
        logger.info(f'Agent name: {agent.name}')
        logger.info(f'Tools: {[t.__name__ if hasattr(t, "__name__") else str(t) for t in agent.tools]}')

        # Run the agent (blocks until exit)
        asyncio.run(run_agent_with_signal_handlers(agent))

    except KeyboardInterrupt:
        logger.info('Received interrupt signal, shutting down...')
    except Exception as e:
        if e.__class__.__name__ == "EmbeddingProfileMismatchError":
            logger.error(str(e))
            sys.exit(1)
        logger.exception(f'Error running agent: {e}')
        sys.exit(1)

    logger.info('Agent shut down successfully')


def main():
    """Main entry point for Strands agent."""
    # Keep the supervisor's run lock to this process (runtime_hosting.process_run).
    adopt_process_run()
    parser = build_parser()
    args = parser.parse_args()
    run_with_args(args)


if __name__ == '__main__':
    main()
