#!/usr/bin/env python3
"""Preview local memory timers, or upgrade them with all team runtimes stopped."""

# ruff: noqa: E402
from __future__ import annotations

import argparse
import json
import logging
import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from dotenv import load_dotenv
from assistant_gateway.runtime_config import load_runtime_config_file
from config_loader import resolve_agent_type_settings, set_config
from custom_agents.agent_catalog import load_agent_type_map
from custom_agents.local_schedule_upgrade import (
    apply_local_schedule_upgrade,
    plan_local_schedule_upgrade,
)
from custom_agents.schedule_store import ScheduleStore
from custom_messages.contracts.runtime_v1 import MEM_REVIEW_AGENT_TYPE
from storage.config import StorageSettings, resolve_storage_settings
from runtime_settings.review_cleanup import ReviewCleanupSettings
from storage.memory_review_cleanup_repository import MemoryReviewCleanupRepository
from storage.schedule_worker_repository import binding_from_json, schedule_from_row
from storage.runtime_snapshot_maintenance import (
    _connection,
    _has_table,
    inspect_runtime_snapshots,
)

LOGGER = logging.getLogger("upgrade_local_schedules")


def inspect_review_work(storage: StorageSettings, team_id: str) -> dict[str, Any]:
    """Describe backlog and execution blockers without changing saved work.

    A queued database job is not an execution claim. Local review preparation
    can reuse matching jobs after the operator drains old NATS deliveries.
    Claimed jobs, including expired claims with uncertain effects, and saved
    worker tasks still require resolution before transferring the timers.
    Never include review content or task payloads in the command's output.
    """
    tasks = []
    jobs = []
    awaiting_cleanup = 0
    with _connection(storage) as conn:
        p = conn.dialect.bind
        postgres = storage.backend == "postgres"
        if _has_table(conn, "active_task_runtime_records", postgres=postgres):
            tasks = [
                dict(row)
                for row in conn.execute(
                    "SELECT o.owner_agent_id, r.record_id AS task_id, o.updated_at "
                    "FROM active_task_runtime_records r JOIN active_task_runtime o "
                    "ON o.owner_agent_id=r.owner_agent_id "
                    f"WHERE o.team_id={p} AND o.owner_named_agent_type={p} "
                    "AND r.section='tasks' ORDER BY o.owner_agent_id, r.record_id",
                    (team_id, MEM_REVIEW_AGENT_TYPE),
                ).fetchall()
            ]
        if _has_table(conn, "memory_review_jobs", postgres=postgres):
            awaiting_cleanup = conn.execute(
                f"SELECT COUNT(*) FROM memory_review_jobs WHERE owner_team_id={p} AND status='expired'",
                (team_id,),
            ).fetchone()[0]
            jobs = [
                dict(row)
                for row in conn.execute(
                    "SELECT review_job_id, task_id AS source_task_id, status, "
                    "claim_owner_agent_id, claim_expires_at, updated_at "
                    f"FROM memory_review_jobs WHERE owner_team_id={p} "
                    "AND status IN ('queued', 'claimed') ORDER BY review_job_id",
                    (team_id,),
                ).fetchall()
            ]
    queued = [job for job in jobs if job["status"] == "queued"]
    claimed = [job for job in jobs if job["status"] == "claimed"]
    # Obsolete or damaged snapshots may have no current task-record rows.
    # Reuse the runtime's strict validation rather than treating that as idle.
    unsupported = [
        row
        for row in inspect_runtime_snapshots(storage, team_id=team_id)
        if row["owner_named_agent_type"] == MEM_REVIEW_AGENT_TYPE
    ]
    return {
        "queued_review_jobs": queued,
        "claimed_review_jobs": claimed,
        "unfinished_review_tasks": tasks,
        "unsupported_review_runtimes": unsupported,
        "awaiting_cleanup_review": awaiting_cleanup,
        "blocks_upgrade": bool(claimed or tasks or unsupported),
    }


def read_upgrade_state(
    storage: StorageSettings, team_id: str
) -> tuple[dict[str, Any], SimpleNamespace]:
    """Read old tables without constructing repositories that initialize schema."""
    with _connection(storage) as conn:
        p = conn.dialect.bind
        postgres = storage.backend == "postgres"
        schedules = (
            [
                dict(row)
                for row in conn.execute(
                    f"SELECT * FROM scheduled_messages WHERE claim_team_id={p}",
                    (team_id,),
                ).fetchall()
            ]
            if _has_table(conn, "scheduled_messages", postgres=postgres)
            else []
        )
        bindings = (
            [
                binding_from_json(row[0])
                for row in conn.execute(
                    f"SELECT payload_json FROM schedule_worker_bindings WHERE team_id={p}",
                    (team_id,),
                ).fetchall()
            ]
            if _has_table(conn, "schedule_worker_bindings", postgres=postgres)
            else []
        )
    by_id = {row["schedule_id"]: schedule_from_row(row) for row in schedules}
    policies = {binding.schedule_id: binding for binding in bindings}
    view = SimpleNamespace(
        get_all=lambda: schedules,
        get_scheduled_message=by_id.get,
        worker_records=SimpleNamespace(
            get_binding=lambda schedule_id, **kwargs: policies.get(schedule_id),
        ),
    )
    backup = {
        "team_id": team_id,
        "schedules": schedules,
        "policies": [item.model_dump(mode="json") for item in bindings],
    }
    return backup, view


def apply_with_backup(
    agent: Any,
    storage: StorageSettings,
    backup: dict[str, Any],
    backup_path: str,
    candidate_ids: set[str],
    *,
    cutoff: datetime,
    now: datetime,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Back up first, retire exact reviewed candidates, then move the timers."""
    if candidate_ids:
        # This private backup contains original content; the report never does.
        with _connection(storage) as conn:
            backup["memory_review_jobs"] = [
                dict(row)
                for row in conn.execute(
                    f"SELECT * FROM memory_review_jobs WHERE owner_team_id={conn.dialect.bind} AND status='claimed'",
                    (agent.team_id,),
                ).fetchall()
            ]
    fd = os.open(
        Path(backup_path).expanduser(), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600
    )
    with os.fdopen(fd, "w") as stream:
        json.dump(backup, stream, indent=2, default=str)
        stream.flush()
        os.fsync(stream.fileno())
    if candidate_ids:
        MemoryReviewCleanupRepository(storage).retire_expired(
            agent.team_id,
            before=cutoff,
            now=now,
            actor_id="operator:local_schedule_upgrade",
            limit=len(candidate_ids),
            expected_ids=candidate_ids,
        )
    agent._schedule_store = ScheduleStore(storage_settings=storage)
    return apply_local_schedule_upgrade(agent), inspect_review_work(
        storage, agent.team_id
    )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--config", required=True, help="config.yaml or instance_config.yaml"
    )
    parser.add_argument("--team-id", required=True)
    parser.add_argument(
        "--env", help="Optional environment file, loaded without printing values"
    )
    parser.add_argument("--apply", action="store_true")
    parser.add_argument(
        "--retire-expired-review-claims",
        action="store_true",
        help="Set abandoned expired review claims aside for InternalImprovementAgent, preserving their content and unknown outcome",
    )
    parser.add_argument(
        "--runtime-stopped",
        action="store_true",
        help="Confirm all team runtimes are stopped and old review deliveries have drained",
    )
    parser.add_argument(
        "--backup",
        help="New path for the pre-upgrade schedule and policy backup, required with --apply",
    )
    args = parser.parse_args()
    if args.apply and (not args.runtime_stopped or not args.backup):
        parser.error("--apply requires --runtime-stopped and --backup")
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    if args.env:
        load_dotenv(args.env, override=True)
    config = load_runtime_config_file(args.config)
    set_config(config)
    runtime = resolve_agent_type_settings(config, "TeamArchitect")
    storage = resolve_storage_settings(
        {
            "backend": runtime["storage_backend"],
            "sqlite": {"path": runtime["storage_sqlite_path"]},
            "postgres": {"dsn": runtime["storage_postgres_dsn"]},
        },
        base_dir=runtime["instance_root"],
    )
    # Check the existing DB before constructing stores that initialize schemas.
    review_work = inspect_review_work(storage, args.team_id)
    cleanup = MemoryReviewCleanupRepository(storage)
    cleanup_config = ReviewCleanupSettings.model_validate(
        (runtime.get("schedule_maintenance") or {}).get("memory_review_cleanup") or {}
    )
    now = datetime.now(timezone.utc)
    cutoff = now - timedelta(seconds=cleanup_config.expired_claim_grace_seconds)
    candidates = (
        cleanup.candidates(args.team_id, before=cutoff)
        if args.retire_expired_review_claims
        else []
    )
    candidate_ids = {row["review_job_id"] for row in candidates}
    if args.retire_expired_review_claims:
        remaining_claims = [
            row
            for row in review_work["claimed_review_jobs"]
            if row["review_job_id"] not in candidate_ids
        ]
        review_work["blocks_upgrade"] = bool(
            remaining_claims
            or review_work["unfinished_review_tasks"]
            or review_work["unsupported_review_runtimes"]
        )
    catalog = load_agent_type_map()
    backup, store = read_upgrade_state(storage, args.team_id)
    agent = SimpleNamespace(
        team_id=args.team_id,
        _schedule_store=store,
        _logger=LOGGER,
        schedule_workers=config.get("agent.schedule_workers") or {},
        _local_team_runtime=SimpleNamespace(
            agent_types=lambda: tuple(catalog), resolve_definition=catalog.get
        ),
    )
    report = plan_local_schedule_upgrade(agent)
    blocked = review_work["blocks_upgrade"] or any(
        item["reason"] == "active_schedule_claim" for item in report
    )
    if args.apply and not blocked:
        report, review_work = apply_with_backup(
            agent,
            storage,
            backup,
            args.backup,
            candidate_ids,
            cutoff=cutoff,
            now=now,
        )
    LOGGER.info(
        "%s",
        json.dumps(
            {
                "team_id": args.team_id,
                "review_work": review_work,
                "schedules": report,
                "expired_claim_retirement": {
                    "requested": args.retire_expired_review_claims,
                    "eligible_count": len(candidate_ids),
                    "applied": bool(args.apply and not blocked),
                },
            },
            indent=2,
            default=str,
        ),
    )
    if blocked:
        LOGGER.error(
            "Upgrade blocked; no changes were made. Resolve the listed claimed "
            "review jobs, unfinished or unsupported MemReviewAgent runtimes, or active schedule "
            "claims first. An expired claim does not prove the review had no "
            "effects. Preview with --retire-expired-review-claims to set abandoned "
            "claims aside for cleanup review without replaying or deleting them."
        )
        if review_work["unsupported_review_runtimes"]:
            LOGGER.error(
                "Use the configuration guide's active runtime snapshot upgrade "
                "procedure for unsupported saved runtimes. The schedule upgrade "
                "does not convert or discard those records."
            )
        return 1
    if review_work["queued_review_jobs"]:
        LOGGER.info(
            "%d queued review job(s) kept unchanged. Queued records do not block "
            "the upgrade. Matching jobs can be reused by local reviews. Before "
            "applying, stop all team runtimes and finish old NATS deliveries; "
            "this command cannot check the broker.",
            len(review_work["queued_review_jobs"]),
        )
    return 1 if any(item["action"] == "blocked" for item in report) else 0


if __name__ == "__main__":
    raise SystemExit(main())
