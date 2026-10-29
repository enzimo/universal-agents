from __future__ import annotations

import argparse
import json
import logging
import os
import shutil
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from custom_agents.heartbeat_store import HeartbeatStore
from custom_agents.schedule_store import ScheduleStore
from custom_agents.task_manager import TaskManager
from config_loader import BUILTIN_OBJECTIVE_EXECUTION_POLICY_PATH
from custom_messages.contracts.routing_v1 import GatewayContext, MessageHeader, MessageMeta
from custom_messages.contracts.schedule_v1 import ScheduledMessage
from custom_messages.contracts.task_v1 import Task
from custom_messages.enums import MessageMetaType, ObjectiveEventType, ObjectiveStatus, PlanStatus, RequesterType
from custom_messages.records.plans import PlanRecord
from custom_messages.enums import ScheduleManagementClass
from custom_tools.providers.objective_work import objective_work_profiles, propose_objective_work_profile
from custom_tools.providers.objectives import objectives
from tests.task_work_support import bind_task, set_agent_ref
from live_storage_utils import (
    query_storage,
    storage_bind,
    reset_postgres_storage,
    resolve_live_storage_settings,
)
from id_generation import new_id_suffix
from storage.postgres_connection import redact_postgres_dsn
from tests.broker_access_support import context_signer, context_verify_keys, forget_task_context_keys

try:
    from dotenv import load_dotenv
except ImportError:  # pragma: no cover
    load_dotenv = None


RESULT_DIR = Path("test_results/live_objective_validation")
logger = logging.getLogger("live_objective_storage_validation")


class _FakeWake:
    def __init__(self) -> None:
        self.was_set = False

    def set(self) -> None:
        self.was_set = True


def _write_report(report: dict[str, Any]) -> None:
    RESULT_DIR.mkdir(parents=True, exist_ok=True)
    (RESULT_DIR / "report.json").write_text(json.dumps(report, indent=2, default=str), encoding="utf-8")


def _reset_storage(settings: Any) -> None:
    if settings.backend == "postgres":
        reset_postgres_storage(settings)
        return
    sqlite_path = Path(settings.sqlite_path or "test_results/live_objective_validation/task_history.db")
    if sqlite_path.exists():
        sqlite_path.unlink()
    sqlite_path.parent.mkdir(parents=True, exist_ok=True)


def run_validation(*, config_path: str | None = None) -> dict[str, Any]:
    """Exercise person-owned tools with a gateway-signed fixture task.

    Authorization-off mode still verifies who the task works for. Trust only
    this canary's public key while it runs, then retire the cached verifier
    before restoring the caller's environment.
    """
    with patch.dict(os.environ, {"ACCESS_CONTEXT_VERIFY_KEYS": context_verify_keys()}):
        forget_task_context_keys()
        try:
            return _run_validation(config_path=config_path)
        finally:
            set_agent_ref(None)
            forget_task_context_keys()


def _run_validation(*, config_path: str | None = None) -> dict[str, Any]:
    settings = resolve_live_storage_settings(
        config_path=config_path,
        sqlite_fallback_path=RESULT_DIR / "task_history.db",
        workspace_dir=Path.cwd(),
    )
    _reset_storage(settings)
    db_path = settings.sqlite_path or str(RESULT_DIR / "task_history.db")
    run_id = new_id_suffix()
    owner_agent_id = f"manager_live_objective_{run_id}"

    manager = TaskManager(
        db_path=db_path,
        logger=logger,
        storage_settings=settings,
        objective_execution_paths=[str(BUILTIN_OBJECTIVE_EXECUTION_POLICY_PATH)],
    )
    manager.set_owner_identity(
        agent_id=owner_agent_id,
        agent_name="Live Objective Manager",
        team_id="team_live_objective",
        named_agent_type="TeamArchitect",
    )

    task = Task(
        task_id=f"task_live_objective_{run_id}",
        task_type=MessageMetaType.REQUEST,
        problem_statement="Validate live Objective persistence and lifecycle cascade.",
    )
    assert manager.add_task(
        task,
        MessageHeader(from_agent_id="user", agent_signature=""),
        MessageMeta(
            from_agent_name="user",
            conversation_id=f"conversation_live_objective_{run_id}",
            requester_type=RequesterType.USER_REQUESTER,
            gateway_context=GatewayContext(
                canonical_user_id="live-objective-user",
                session_key=f"session_live_objective_{run_id}",
                heartbeat_source_id=f"hb_source_live_objective_{run_id}",
                task_context=context_signer().issue(
                    team_id="team_live_objective",
                    person_id="live-objective-user",
                    person_roles=("owner",),
                ),
            ),
        ),
        owner_person_id="live-objective-user",
    )
    objective = manager.create_objective(
        task_id=task.task_id,
        title="Live Objective validation",
        desired_outcome="Objective, schedule, and heartbeat state persist in live storage.",
        why="Objective is a top-level primitive that must work with the configured storage backend.",
        horizon="recurring",
        success_criteria=["Objective can pause and resume managed recurring work."],
    )
    assert objective is not None

    schedule_store = ScheduleStore(db_path=db_path, logger=logger, storage_settings=settings)
    schedule_id = f"sched_live_objective_{run_id}"
    scheduled = ScheduledMessage(
        schedule_id=schedule_id,
        schedule_name="live objective validation",
        management_class=ScheduleManagementClass.USER,
        objective_id=objective.objective_id,
        payload=Task(
            task_id=f"task_live_objective_scheduled_{run_id}",
            task_type=MessageMetaType.REQUEST,
            problem_statement="Run a live objective validation schedule occurrence.",
        ),
        payload_header=MessageHeader(from_agent_id=owner_agent_id, agent_signature=""),
        payload_meta=MessageMeta(
            from_agent_name="Live Objective Manager",
            conversation_id=f"conversation_live_objective_{run_id}",
            objective_id=objective.objective_id,
        ),
        interval_ms=60_000,
        next_fire_at=(datetime.now(timezone.utc) - timedelta(seconds=1)).isoformat(),
    )
    schedule_store.add(
        scheduled,
        claim_team_id="team_live_objective",
        claim_named_agent_type="TeamArchitect",
    )
    manager.link_schedule_to_objective(objective.objective_id, schedule_id)

    expired_schedule_id = f"sched_live_expired_{run_id}"
    now = datetime.now(timezone.utc)
    schedule_store.add(
        ScheduledMessage(
            schedule_id=expired_schedule_id,
            schedule_name="live expired schedule validation",
            payload=Task(
                task_type=MessageMetaType.REQUEST,
                problem_statement="This expired schedule must be pruned before it can fire.",
            ),
            interval_ms=60_000,
            repeat_count=None,
            next_fire_at=(now - timedelta(minutes=5)).isoformat(),
            expires_at_utc=(now - timedelta(minutes=1)).isoformat(),
        )
    )
    assert schedule_store.get_scheduled_message(expired_schedule_id) is not None
    assert schedule_store.prune_expired(now=now) == 1
    assert schedule_store.get_scheduled_message(expired_schedule_id) is None

    heartbeat_store = HeartbeatStore(db_path=db_path, logger_=logger, storage_settings=settings)
    heartbeat_source_id = f"hb_source_live_objective_{run_id}"
    heartbeat_store.ensure_source(source_id=heartbeat_source_id, title="Live Objective Heartbeat")
    heartbeat_item = heartbeat_store.add_item(
        source_id=heartbeat_source_id,
        title="Live Objective heartbeat",
        instruction="Confirm the live Objective heartbeat path.",
        conversation_id=f"conversation_live_objective_heartbeat_{run_id}",
        objective_id=objective.objective_id,
    )
    manager.link_heartbeat_to_objective(objective.objective_id, heartbeat_item.heartbeat_item_id)

    agent = SimpleNamespace(
        id=owner_agent_id,
        name="Live Objective Manager",
        team_id="team_live_objective",
        named_agent_type="TeamArchitect",
        authorization_config={"enabled": False},
        task_manager=manager,
        _schedule_store=schedule_store,
        _scheduler_wake=_FakeWake(),
    )
    manager.task_state.current_in_progress_task_id = task.task_id
    bind_task(manager.task_state.current_in_progress_task_id)
    set_agent_ref(agent)

    profile_proposal = json.loads(
        propose_objective_work_profile(
            profile_name="live-storage-profile",
            cadence_cron="0 */6 * * *",
            active_seconds=2_700,
            checkpoint_reserve_seconds=300,
            checkpoint_reserve_turns=2,
            checkpoint_reserve_tokens=50_000,
            checkpoint_reserve_tool_calls=3,
            turns=18,
            total_tokens=250_000,
            tool_calls=35,
            rationale="Verify owner-scoped reusable profile persistence in live storage.",
            scope="reusable",
        )
    )
    assert "error" not in profile_proposal, profile_proposal
    profile_application = json.loads(
        objective_work_profiles(action="apply", proposal_id=profile_proposal["proposal_id"], proposal_digest=profile_proposal["proposal_digest"], confirmation_summary="Install these exact reusable bounds for the live storage canary.", confirmed=True)
    )
    installed_profile = profile_application["installed_profile"]

    version = json.loads(objectives(action="get", objective_id=objective.objective_id))["version"]
    paused = json.loads(
        objectives(action="update_status", expected_version=version, status="paused", objective_id=objective.objective_id)
    )
    assert paused["objective"]["status"] == "paused"
    assert schedule_store.get_scheduled_message(schedule_id).enabled is False
    assert heartbeat_store.get_item(heartbeat_item.heartbeat_item_id).enabled is False

    resumed = json.loads(
        objectives(action="update_status", expected_version=version, status="active", objective_id=objective.objective_id)
    )
    assert resumed["objective"]["status"] == "active"
    assert schedule_store.get_scheduled_message(schedule_id).enabled is True
    assert heartbeat_store.get_item(heartbeat_item.heartbeat_item_id).enabled is True

    persisted = manager.get_objective(objective.objective_id)
    assert persisted is not None
    assert persisted.current_plan_id
    assert persisted.related_task_ids == [task.task_id]
    persisted_plan = manager.get_plan(persisted.current_plan_id)
    assert persisted_plan is not None
    assert persisted_plan.objective_id == objective.objective_id
    assert persisted_plan.root_task_id == task.task_id
    assert persisted.schedule_links[0].schedule_id == schedule_id
    assert persisted.heartbeat_links[0].heartbeat_item_id == heartbeat_item.heartbeat_item_id

    manager.complete_task(task.task_id, "2026-05-23T10:00:00+00:00")
    reviewed = manager.get_objective(objective.objective_id)
    assert reviewed is not None
    assert reviewed.status == ObjectiveStatus.ACTIVE
    surviving_plan = manager.get_plan(persisted.current_plan_id)
    assert surviving_plan is not None
    assert surviving_plan.status.value == "active"
    review_events = [event for event in reviewed.event_log if event.event_type == ObjectiveEventType.REVIEW_COMPLETED]
    assert review_events
    assert review_events[-1].details["conclusion"] == "plan_work_remains"

    restored_manager = TaskManager(
        db_path=db_path,
        logger=logger,
        storage_settings=settings,
        objective_execution_paths=[str(BUILTIN_OBJECTIVE_EXECUTION_POLICY_PATH)],
    )
    replacement_owner_agent_id = f"manager_live_objective_replacement_{run_id}"
    restored_manager.set_owner_identity(
        agent_id=replacement_owner_agent_id,
        agent_name="Live Objective Manager",
        team_id="team_live_objective",
        named_agent_type="TeamArchitect",
    )
    durable_owner_agent_id = restored_manager.durable_manager_scope_id()
    restored_objective = restored_manager.get_objective(objective.objective_id)
    assert restored_objective is not None
    assert restored_objective.owner_agent_id == durable_owner_agent_id
    restored_profile = restored_manager.objective_work_repository.get_installed_profile(
        "live-storage-profile",
        owner_agent_id=durable_owner_agent_id,
        owner_canonical_user_id="live-objective-user",
    )
    assert restored_profile is not None
    assert restored_profile.installed_profile_id == installed_profile["installed_profile_id"]
    restored_schedule_store = ScheduleStore(
        db_path=db_path,
        logger=logger,
        storage_settings=settings,
    )
    claimed_schedule = restored_schedule_store.claim_next_due(
        claimer_agent_id=replacement_owner_agent_id,
        claimer_named_agent_type="TeamArchitect",
        claimer_team_id="team_live_objective",
    )
    assert claimed_schedule is not None
    assert claimed_schedule["schedule_id"] == schedule_id
    assert restored_schedule_store.release_claim(
        schedule_id,
        claim_token=claimed_schedule["claim_token"],
        claimer_agent_id=replacement_owner_agent_id,
    )

    collision_plan_id = f"plan_live_owner_collision_{run_id}"
    shared_collision_updated_at = "2026-06-10T00:02:37.660873+00:00"
    older_collision_plan = PlanRecord(
        plan_id=collision_plan_id,
        root_task_id=f"task_live_owner_collision_{run_id}",
        owner_agent_id=f"manager_live_collision_older_{run_id}",
        owner_agent_name="Older live manager",
        team_id="team_live_objective",
        owner_named_agent_type="TeamArchitect",
        status=PlanStatus.ACTIVE,
        created_at="2026-06-10T00:02:37.668792+00:00",
        updated_at=shared_collision_updated_at,
    )
    newer_collision_plan = older_collision_plan.model_copy(deep=True)
    newer_collision_plan.owner_agent_id = f"manager_live_collision_newer_{run_id}"
    newer_collision_plan.owner_agent_name = "Newer live manager"
    newer_collision_plan.status = PlanStatus.FAILED
    newer_collision_plan.created_at = "2026-06-10T00:13:31.978202+00:00"
    restored_manager.plan_repository.save_plan(older_collision_plan)
    restored_manager.plan_repository.save_plan(newer_collision_plan)

    collision_replacement = TaskManager(
        db_path=db_path,
        logger=logger,
        storage_settings=settings,
        objective_execution_paths=[str(BUILTIN_OBJECTIVE_EXECUTION_POLICY_PATH)],
    )
    collision_replacement.set_owner_identity(
        agent_id=f"manager_live_collision_replacement_{run_id}",
        agent_name="Live collision replacement manager",
        team_id="team_live_objective",
        named_agent_type="TeamArchitect",
    )
    reconciled_collision_plan = collision_replacement.get_plan(collision_plan_id)
    assert reconciled_collision_plan is not None
    assert reconciled_collision_plan.owner_agent_id == durable_owner_agent_id
    assert reconciled_collision_plan.status == PlanStatus.FAILED
    collision_archive_rows = query_storage(
        settings,
        f"""
        SELECT source_record_id, target_record_id, reason, source_data
        FROM durable_owner_scope_reconciliation_archive
        WHERE source_table='plans' AND target_record_id={storage_bind(settings)}
        """,
        (f"{durable_owner_agent_id}::{collision_plan_id}",),
    )
    assert len(collision_archive_rows) == 1
    assert collision_archive_rows[0][0] == (
        f"{older_collision_plan.owner_agent_id}::{collision_plan_id}"
    )
    assert collision_archive_rows[0][1] == (
        f"{durable_owner_agent_id}::{collision_plan_id}"
    )
    assert collision_archive_rows[0][2] == "superseded_legacy_snapshot"
    archived_collision_plan = PlanRecord.model_validate_json(collision_archive_rows[0][3])
    assert archived_collision_plan.status == PlanStatus.ACTIVE

    return {
        "status": "passed",
        "storage_backend": settings.backend,
        "storage": redact_postgres_dsn(settings.postgres_dsn or "") if settings.backend == "postgres" else settings.sqlite_path,
        "owner_agent_id": owner_agent_id,
        "replacement_owner_agent_id": replacement_owner_agent_id,
        "durable_owner_agent_id": durable_owner_agent_id,
        "objective_id": objective.objective_id,
        "plan_id": persisted.current_plan_id,
        "objective_task_id": task.task_id,
        "schedule_id": schedule_id,
        "replacement_claimed_schedule": True,
        "owner_collision_plan_id": collision_plan_id,
        "owner_collision_archived_snapshots": len(collision_archive_rows),
        "expired_schedule_pruned": True,
        "heartbeat_item_id": heartbeat_item.heartbeat_item_id,
        "installed_profile_id": installed_profile["installed_profile_id"],
        "profile_proposal_id": profile_proposal["proposal_id"],
        "terminal_review_conclusion": review_events[-1].details["conclusion"],
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="Validate Objective persistence against live configured storage.")
    parser.add_argument("--config", default="config.yaml", help="Config file used to resolve live storage settings.")
    parser.add_argument("--env", default=None, help="Optional env file to load before validation.")
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
    if args.env and load_dotenv:
        load_dotenv(args.env, override=True)
    if RESULT_DIR.exists():
        shutil.rmtree(RESULT_DIR)
    RESULT_DIR.mkdir(parents=True, exist_ok=True)

    try:
        report = run_validation(config_path=args.config)
    except Exception as exc:
        report = {"status": "failed", "error": {"type": type(exc).__name__, "message": str(exc)}}
        _write_report(report)
        logger.exception("Live Objective validation failed.")
        raise
    _write_report(report)
    logger.info("Live Objective validation passed. Report written to %s", RESULT_DIR / "report.json")


if __name__ == "__main__":
    main()
