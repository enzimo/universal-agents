from __future__ import annotations

# ruff: noqa: E402

import argparse
import json
import logging
import os
import shutil
import sys
import threading
from dataclasses import replace
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

try:
    import psycopg
except ImportError:  # pragma: no cover - exercised in live runtime only
    psycopg = None

try:
    from dotenv import load_dotenv
except ImportError:  # pragma: no cover - exercised in live runtime only
    load_dotenv = None



PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from assistant_gateway.store import GatewayStateStore
from assistant_gateway.domain.transcript import TranscriptAppendRequest
from assistant_gateway.adapter_host.domain import (
    AdapterInboxRecord,
    AdapterRegistrationRecord,
)
from custom_agents.agent_state_store import AgentStateStore
from custom_agents.conversation_store import ConversationStore
from custom_agents.heartbeat_store import HeartbeatStore
from custom_agents.memory_review_store import MemoryReviewStore
from custom_agents.runtime_state_store import RuntimeStateStore
from custom_agents.schedule_management import (
    ScheduleMutationAuthorityMode,
    ScheduleMutationOperation,
    SystemScheduleMutationDenied,
    build_schedule_mutation_permit,
)
from custom_agents.schedule_store import AdvanceOutcome, ScheduleStore
from custom_messages.contracts.routing_v1 import PERSON_GATEWAY_FIELDS
from custom_agents.task_manager import TaskManager
from custom_messages.contracts.control_v1 import AgentStatus
from custom_messages.contracts.routing_v1 import GatewayContext, MessageHeader, MessageMeta
from custom_messages.contracts.schedule_v1 import ScheduledMessage
from custom_messages.contracts.task_v1 import Task
from custom_messages.enums import ControlType, MessageMetaType, PlanActionType, PlanStatus, RequesterType
from custom_messages.solution_variation import (
    SolutionCandidate,
    SolutionEvaluation,
    SolutionEvaluationOutcome,
    SolutionVariationRun,
)
from storage.config import StorageSettings
from storage.adapter_host_repository import create_adapter_host_repository
from storage.after_action_report_repository import create_after_action_report_repository
from storage.memory_graph_repository import create_memory_graph_repository
from storage.multi_user_owner_migration import migrate_legacy_null_owner_rows
from storage.postgres_connection import connect_postgres, redact_postgres_dsn
from storage.solution_variation_repository import create_solution_variation_repository
from live_storage_utils import resolve_live_storage_settings
from tests.after_action_report_repository_support import assert_report_person_scoping


logger = logging.getLogger(__name__)
TEST_RESULTS_ROOT = PROJECT_ROOT / "test_results"
RESULT_DIR = TEST_RESULTS_ROOT / "live_postgres_validation"


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def write_json_file(path: Path, payload: dict[str, Any] | list[Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(payload, indent=2, sort_keys=True, default=str), encoding="utf-8"
    )


def require_postgres_settings() -> StorageSettings:
    backend = str(os.getenv("STORAGE_BACKEND", "")).strip().lower()
    if backend != "postgres":
        raise RuntimeError(
            f"Live Postgres validation requires STORAGE_BACKEND=postgres in the loaded env, got {backend!r}."
        )
    settings = resolve_live_storage_settings(
        sqlite_fallback_path=PROJECT_ROOT / "test_results" / "ignored.sqlite",
        workspace_dir=str(PROJECT_ROOT),
    )
    if settings.backend != "postgres" or not settings.postgres_dsn:
        raise RuntimeError(
            "Live Postgres validation requires POSTGRES_DSN or LIVE_TEST_POSTGRES_DSN in the loaded env."
        )
    return settings


def reset_postgres_database(dsn: str) -> None:
    if psycopg is None:
        raise RuntimeError("psycopg is required to run live Postgres validation tests.")
    with psycopg.connect(dsn, autocommit=True) as conn:
        with conn.cursor() as cur:
            cur.execute("DROP SCHEMA IF EXISTS public CASCADE")
            cur.execute("CREATE SCHEMA public")
            cur.execute("GRANT ALL ON SCHEMA public TO CURRENT_USER")
            cur.execute("GRANT ALL ON SCHEMA public TO PUBLIC")


def record_check(report: dict[str, Any], name: str, details: dict[str, Any]) -> None:
    logger.info("Validated %s", name)
    report.setdefault("checks", []).append({"name": name, "details": details})


def build_live_schedule_mutation_permit(
    store: ScheduleStore,
    *,
    schedule_id: str,
    operation: ScheduleMutationOperation,
    candidate: ScheduledMessage | None = None,
    request_binding: dict[str, Any] | None = None,
):
    """Build exact trusted-runtime authority for one live-fixture transition."""

    schedule = store.get_scheduled_message(schedule_id)
    assert schedule is not None
    permit = build_schedule_mutation_permit(
        schedule=schedule,
        operation=operation,
        authority_mode=ScheduleMutationAuthorityMode.TRUSTED_RUNTIME,
        actor_principal_id="manager_live_pg",
        owner_canonical_user_id="usr_live_pg_admin",
        team_id="team_live_pg",
        candidate=candidate,
        request_binding=request_binding,
    )
    assert permit is not None
    return permit


def exercise_agent_state_store(
    settings: StorageSettings, report: dict[str, Any]
) -> None:
    store = AgentStateStore(
        db_path="ignored.db", storage_settings=settings, logger=logger
    )
    assert store.get_all() == []

    first = AgentStatus(
        status_type=MessageMetaType.RESPONSE,
        info_type=ControlType.STATUS_REQUEST_BASIC,
        named_agent_type="TeamArchitect",
        self_id="agent_live_1",
        is_paused=False,
        is_executing=False,
        has_execution_capacity=True,
        is_busy=False,
        name="Live Manager",
        tools=["search"],
        team_id="team_live_pg",
        agent_role="Manager",
        last_seen_at="2026-10-17T00:00:00+00:00",
    )
    second = AgentStatus(
        status_type=MessageMetaType.RESPONSE,
        info_type=ControlType.STATUS_REQUEST_BASIC,
        named_agent_type="CodeWritingAgent",
        self_id="agent_live_2",
        is_paused=False,
        is_executing=True,
        has_execution_capacity=False,
        is_busy=True,
        name="Live Writer",
        tools=["shell"],
        team_id="team_live_pg",
        agent_role="Writer",
    )
    store.upsert(first)
    store.upsert(second)

    all_rows = store.get_all()
    assert len(all_rows) == 2
    assert store.get_by_id("agent_live_1") is not None
    assert store.remove("agent_live_2") is True
    # A row is pruned only while it still holds the heartbeat that was read.
    assert store.remove_unseen("agent_live_1", "2026-10-16T00:00:00+00:00") is False
    assert store.remove_unseen("agent_live_1", first.last_seen_at) is True
    assert store.get_all() == []
    record_check(report, "agent_state_store", {"inserted": 2, "removed": 2})


def exercise_runtime_state_store(
    settings: StorageSettings, report: dict[str, Any]
) -> None:
    store = RuntimeStateStore(
        db_path="ignored.db", storage_settings=settings, logger=logger
    )
    assert store.peek_task_runtime_by_role("team_live_pg", "TeamArchitect") == []

    from tests.runtime_snapshot_fixtures import snapshot_json

    runtime_payload = snapshot_json("task_live_1")
    store.save_task_runtime(
        expect_existing=False,
        owner_agent_id="agent_runtime_1",
        owner_agent_name="Live Runtime",
        team_id="team_live_pg",
        owner_named_agent_type="TeamArchitect",
        runtime_state_json=runtime_payload,
    )
    assert store.get_task_runtime_team_id("agent_runtime_1") == "team_live_pg"
    peeked = store.peek_task_runtime_by_role("team_live_pg", "TeamArchitect")
    assert len(peeked) == 1
    assert store.load_task_runtime_by_owner("agent_runtime_1") == peeked
    store.commit_task_runtime_recovery(
        sources=peeked,
        replacement=replace(peeked[0], owner_agent_id="agent_runtime_replacement"),
    )
    assert store.load_task_runtime_by_owner("agent_runtime_1") == []
    assert len(store.load_task_runtime_by_owner("agent_runtime_replacement")) == 1

    deferred_message = {"msg_header": {"message_id": "msg_1"}, "content": "deferred"}
    deferred_payload = json.dumps([deferred_message])
    store.add_deferred_message(
        owner_agent_id="agent_runtime_2",
        owner_agent_name="Live Deferred",
        team_id="team_live_pg",
        owner_named_agent_type="TeamArchitect",
        message_id="msg_1",
        envelope_json=json.dumps(deferred_message),
        first=False,
        expect_existing=False,
    )
    assert store.get_deferred_messages_team_id("agent_runtime_2") == "team_live_pg"
    deferred_rows = store.load_deferred_messages_by_owner("agent_runtime_2")
    assert len(deferred_rows) == 1
    assert deferred_rows[0].messages_json == deferred_payload
    store.commit_deferred_messages_recovery(
        sources=deferred_rows,
        replacement=replace(deferred_rows[0], owner_agent_id="agent_deferred_replacement"),
    )
    assert store.load_deferred_messages_by_owner("agent_runtime_2") == []
    assert len(store.load_deferred_messages_by_owner("agent_deferred_replacement")) == 1
    record_check(report, "runtime_state_store", {"runtime_rows": 1, "deferred_rows": 1})


def exercise_task_manager_and_conversation_store(
    settings: StorageSettings,
    result_dir: Path,
    report: dict[str, Any],
) -> None:
    artifacts_dir = result_dir / "conversation_artifacts"
    conversation_store = ConversationStore(
        db_path="ignored.db",
        storage_settings=settings,
        logger=logger,
        artifacts_dir=str(artifacts_dir),
    )
    task_manager = TaskManager(
        db_path="ignored.db",
        storage_settings=settings,
        logger=logger,
    )
    task_manager.set_owner_identity(
        agent_id="manager_live_pg",
        agent_name="Live Manager",
        team_id="team_live_pg",
        named_agent_type="TeamArchitect",
    )

    conversation_id = "conversation_live_pg"
    task_id = "task_live_pg_archive"
    conversation_store.ensure_thread(
        conversation_id=conversation_id,
        team_id="team_live_pg",
        owner_agent_id="manager_live_pg",
        owner_agent_name="Live Manager",
        requester_name="live_user",
        requester_id="user_1",
        root_task_id=task_id,
        subject_preview="Validate Postgres archive storage",
    )
    conversation_store.append_turn(
        conversation_id=conversation_id,
        task_id=task_id,
        followup_to_task_id=None,
        owner_agent_id="manager_live_pg",
        requester_name="live_user",
        requester_id="user_1",
        correlation_id="corr_live_pg_1",
        problem_text="Validate Postgres archive storage",
        requester_type=RequesterType.USER_REQUESTER,
        delivery_mode="immediate",
        delivery_state="pending",
        task_status="accepted",
    )

    task = Task(
        task_id=task_id,
        task_type=MessageMetaType.REQUEST,
        problem_statement="Validate Postgres archive storage",
    )
    header = MessageHeader(
        from_agent_id="user_queue",
        to_routing_path="manager_live_pg",
        response_routing_key="user_queue",
        agent_signature="",
    )
    meta = MessageMeta(
        from_agent_name="live_user",
        msg_correlation_id="corr_live_pg_1",
        conversation_id=conversation_id,
        requester_type=RequesterType.USER_REQUESTER,
        is_response_requested=True,
    )

    assert task_manager.add_task(task, header, meta, owner_person_id=None) is True
    assert (
        task_manager.set_task_plan(
            task_id,
            "Validate live Postgres plan persistence.",
            rationale="Live storage validation explicitly promotes this task into plan state.",
            agent_name="Live Manager",
            agent_id="manager_live_pg",
        )
        is not None
    )
    plan_id = task_manager.get_task_plan_id(task_id)
    assert plan_id
    assert "plan_id" not in task.model_dump()
    plan_action = task_manager.record_plan_action(
        task_id,
        PlanActionType.CHECKPOINT,
        "Live Postgres validation checkpoint recorded.",
        details={"validation": "postgres_plan_actions"},
        agent_name="Live Manager",
        agent_id="manager_live_pg",
    )
    assert plan_action is not None
    task_manager.append_history_entry(
        task_id, "Validated archive persistence against a live Postgres instance."
    )
    task_info = task_manager.get_task_info(task_id)
    assert task_info is not None
    task_info.task.answer = "Postgres archive validated live."
    task_manager.complete_task(task_id, utc_now_iso())

    conversation_store.update_turn_completion(
        task_id=task_id,
        task_status="completed",
        final_answer_text="Postgres archive validated live.",
        turn_summary_text="Validated archive persistence against live Postgres.",
        execution_history_text="Validated archive persistence against a live Postgres instance.",
    )
    conversation_store.update_turn_delivery(
        task_id=task_id,
        delivery_state="delivered",
        delivery_via="live_postgres_validation",
        delivered_at=utc_now_iso(),
    )
    artifact = conversation_store.offload_large_text(
        conversation_id=conversation_id,
        task_id=task_id,
        artifact_type="tool_output",
        raw_text="x" * 4096,
        summary_text="Large tool output was offloaded.",
    )

    archived = task_manager.get_task_from_db(task_id)
    assert archived is not None
    assert (
        archived["main_task"].problem_statement == "Validate Postgres archive storage"
    )
    assert "plan_id" not in archived["main_task"].model_dump()
    plan = task_manager.get_plan(plan_id)
    assert plan is not None
    assert plan.status == PlanStatus.COMPLETED
    assert plan.root_task_id == task_id
    assert any(
        action.action_id == plan_action.action_id for action in plan.plan_actions
    )
    listed = task_manager.list_archived_tasks(limit=5, owner_agent_id="manager_live_pg")
    assert listed
    assert listed[0]["conversation_id"] == conversation_id
    summary = conversation_store.get_conversation_summary(conversation_id)
    assert summary["turn_count"] == 1
    assert artifact["storage_path"]
    record_check(
        report,
        "task_manager_and_conversation_store",
        {
            "archived_task_id": task_id,
            "plan_id": plan_id,
            "conversation_id": conversation_id,
            "artifact_path": artifact["storage_path"],
        },
    )


def exercise_solution_variation_repository(
    settings: StorageSettings,
    report: dict[str, Any],
) -> None:
    repository = create_solution_variation_repository(settings, logger=logger)
    task_run, created = repository.start_run(
        SolutionVariationRun(
            run_id="solvar_live_pg",
            purpose="Validate atomic Task-to-Plan promotion on PostgreSQL.",
            evaluation_criteria=["preserve run identity and lineage"],
            owner_agent_id="manager_scope:team_live_pg:TeamArchitect",
            owner_access_scope="user:usr_live_pg_admin",
            team_id="team_live_pg",
            source_task_id="task_solvar_live_pg",
            scope_key="task:task_solvar_live_pg",
        )
    )
    assert created is True
    candidate, candidate_created = repository.record_candidate(
        SolutionCandidate(
            run_id=task_run.run_id,
            candidate_key="atomic-promotion",
            title="Atomic promotion",
            hypothesis="A row lock and revision check preserve one active run.",
            approach="Promote the existing Task row to the first Plan scope.",
        ),
        owner_agent_id=task_run.owner_agent_id,
        owner_access_scope=task_run.owner_access_scope,
    )
    assert candidate_created is True
    evaluation, evaluation_created = repository.record_evaluation(
        SolutionEvaluation(
            run_id=task_run.run_id,
            candidate_key=candidate.candidate_key,
            evaluation_key="live-postgres",
            outcome=SolutionEvaluationOutcome.SUPPORTS,
            summary="The real PostgreSQL repository retained the candidate lineage.",
            hard_constraints_passed=True,
        ),
        owner_agent_id=task_run.owner_agent_id,
        owner_access_scope=task_run.owner_access_scope,
    )
    assert evaluation_created is True

    barrier = threading.Barrier(2)

    def promote():
        barrier.wait()
        return repository.promote_active_task_run_to_plan(
            owner_agent_id=task_run.owner_agent_id,
            owner_access_scope=task_run.owner_access_scope,
            task_scope_key=task_run.scope_key,
            plan_scope_key="plan:plan_solvar_live_pg",
            plan_id="plan_solvar_live_pg",
        )

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(lambda _: promote(), range(2)))

    assert sorted(promoted for _, promoted in results) == [False, True]
    promoted_runs = [run for run, _ in results if run is not None]
    assert {run.run_id for run in promoted_runs} == {task_run.run_id}
    assert (
        repository.get_active_run(
            owner_agent_id=task_run.owner_agent_id,
            owner_access_scope=task_run.owner_access_scope,
            scope_key=task_run.scope_key,
        )
        is None
    )
    promoted = repository.get_active_run(
        owner_agent_id=task_run.owner_agent_id,
        owner_access_scope=task_run.owner_access_scope,
        scope_key="plan:plan_solvar_live_pg",
    )
    assert promoted is not None
    assert promoted.run_id == task_run.run_id
    assert promoted.plan_id == "plan_solvar_live_pg"
    bundle = repository.get_bundle(
        promoted.run_id,
        owner_agent_id=promoted.owner_agent_id,
        owner_access_scope=promoted.owner_access_scope,
    )
    assert bundle is not None
    assert [item.candidate_key for item in bundle.candidates] == [
        candidate.candidate_key
    ]
    assert [item.evaluation_key for item in bundle.evaluations] == [
        evaluation.evaluation_key
    ]
    record_check(
        report,
        "solution_variation_repository",
        {
            "run_id": promoted.run_id,
            "plan_id": promoted.plan_id,
            "revision": promoted.revision,
            "concurrent_promoters": len(results),
        },
    )


def exercise_schedule_store(settings: StorageSettings, report: dict[str, Any]) -> None:
    store = ScheduleStore(
        db_path="ignored.db", storage_settings=settings, logger=logger
    )
    msg = ScheduledMessage(
        schedule_id="schedule_live_pg_1",
        payload=Task(
            task_type=MessageMetaType.REQUEST,
            problem_statement="Run the validation reminder",
        ),
        payload_meta=MessageMeta(
            gateway_context=GatewayContext(
                session_key="session_live_pg_owner",
                canonical_user_id="usr_live_pg_admin",
            )
        ),
        interval_ms=1000,
        repeat_count=2,
    )
    msg.next_fire_at = (datetime.now(timezone.utc) - timedelta(seconds=5)).isoformat()
    store.add(msg)

    assert store.count_enabled() == 1
    due = store.get_next_due()
    assert due is not None
    assert due["schedule_id"] == msg.schedule_id
    current = store.get_scheduled_message(msg.schedule_id)
    assert current is not None
    resumed = current.model_copy(deep=True)
    resumed.paused_until = None
    resume_permit = build_live_schedule_mutation_permit(
        store,
        schedule_id=msg.schedule_id,
        operation=ScheduleMutationOperation.RESUME,
        candidate=resumed,
    )
    assert (
        store.set_paused_until(
            msg.schedule_id,
            None,
            operation=ScheduleMutationOperation.RESUME,
            system_mutation_permit=resume_permit,
        )
        is True
    )
    assert store.update_after_fire(msg.schedule_id) == AdvanceOutcome.ADVANCED
    restored = store.get_scheduled_message(msg.schedule_id)
    assert restored is not None
    assert restored.repeat_count == 2

    # An exact pause of the system-managed schedule, then the same permit
    # again: it no longer matches the schedule, so Postgres refuses it.
    paused = restored.model_copy(deep=True)
    paused.paused_until = (datetime.now(timezone.utc) + timedelta(hours=2)).isoformat()
    pause_permit = build_live_schedule_mutation_permit(
        store,
        schedule_id=msg.schedule_id,
        operation=ScheduleMutationOperation.PAUSE,
        candidate=paused,
    )
    assert store.set_paused_until(
        msg.schedule_id,
        paused.paused_until,
        operation=ScheduleMutationOperation.PAUSE,
        system_mutation_permit=pause_permit,
    )
    try:
        store.set_paused_until(
            msg.schedule_id,
            paused.paused_until,
            operation=ScheduleMutationOperation.PAUSE,
            system_mutation_permit=pause_permit,
        )
    except SystemScheduleMutationDenied:
        pass
    else:
        raise AssertionError("Postgres accepted a stale system schedule permit")
    restarted_store = ScheduleStore(
        db_path="ignored.db",
        storage_settings=settings,
        logger=logger,
    )
    reloaded = restarted_store.get_scheduled_message(msg.schedule_id)
    assert reloaded is not None and reloaded.paused_until == paused.paused_until

    current = store.get_scheduled_message(msg.schedule_id)
    assert current is not None
    disabled = current.model_copy(deep=True)
    disabled.enabled = False
    disable_permit = build_live_schedule_mutation_permit(
        store,
        schedule_id=msg.schedule_id,
        operation=ScheduleMutationOperation.DISABLE,
        candidate=disabled,
    )
    assert (
        store.set_enabled(
            msg.schedule_id,
            False,
            operation=ScheduleMutationOperation.DISABLE,
            system_mutation_permit=disable_permit,
        )
        is True
    )
    assert store.count_enabled() == 0

    legacy = ScheduledMessage(
        schedule_id="schedule_live_pg_ownerless",
        management_class="user",
        payload=Task(task_type=MessageMetaType.REQUEST, problem_statement="Preserve missing ownership."),
        payload_meta=MessageMeta(
            conversation_id="old_personal_conversation",
            gateway_context=GatewayContext(
                canonical_user_id="usr_live_pg_admin",
                session_key="old_personal_session",
                channel_name="telegram",
                chat_id="old_personal_chat",
            ),
        ),
        interval_ms=60_000,
        repeat_count=None,
    )
    store.add(legacy)
    with connect_postgres(settings.postgres_dsn or "") as conn:
        conn.execute(
            "UPDATE scheduled_messages SET owner_canonical_user_id = NULL WHERE schedule_id = %s",
            (legacy.schedule_id,),
        )
        conn.commit()
    restarted = ScheduleStore(storage_settings=settings, logger=logger)
    for _ in range(2):
        unassigned = restarted.get_scheduled_message(legacy.schedule_id)
        assert unassigned is not None and unassigned.payload_meta is not None
        assert unassigned.payload_meta.gateway_context is not None
        assert all(getattr(unassigned.payload_meta.gateway_context, name) is None for name in PERSON_GATEWAY_FIELDS)
        assert unassigned.payload_meta.conversation_id is None
    with connect_postgres(settings.postgres_dsn or "") as conn:
        owner = conn.execute(
            "SELECT owner_canonical_user_id FROM scheduled_messages WHERE schedule_id = %s",
            (legacy.schedule_id,),
        ).fetchone()
    assert owner[0] is None

    record_check(
        report,
        "schedule_store",
        {
            "schedule_id": msg.schedule_id,
            "stale_system_schedule_permit_refused": True,
            "ownerless_schedule_context_cleared": True,
        },
    )


def exercise_heartbeat_store(settings: StorageSettings, report: dict[str, Any]) -> None:
    store = HeartbeatStore(
        db_path="ignored.db", storage_settings=settings, logger_=logger
    )
    source = store.ensure_source(
        source_id="hb_source_live_pg", title="Live Heartbeat", enabled=True
    )
    assert source.source_id == "hb_source_live_pg"
    item = store.add_item(
        source_id=source.source_id,
        title="Check the Postgres-backed heartbeat path",
        instruction="Confirm that heartbeat items persist in Postgres.",
        conversation_id="conversation_live_pg",
        target_agent_type="TeamArchitect",
    )
    assert store.get_item(item.heartbeat_item_id) is not None
    store.mark_item_run_started(item.heartbeat_item_id)
    store.mark_item_run_finished(item.heartbeat_item_id, success=True)
    updated = store.set_item_enabled(item.heartbeat_item_id, False)
    assert updated is not None
    assert updated.enabled is False
    record_check(
        report,
        "heartbeat_store",
        {"source_id": source.source_id, "item_id": item.heartbeat_item_id},
    )


def exercise_memory_review_store(
    settings: StorageSettings, report: dict[str, Any]
) -> None:
    store = MemoryReviewStore(
        db_path="ignored.db", storage_settings=settings, logger=logger
    )
    backlog_job = store.enqueue_review_job(
        owner_team_id="team_live_pg",
        owner_named_agent_type="MemReviewAgent",
        task_id="task_live_pg_backlog",
        conversation_id="conversation_live_pg",
        turn_index=0,
        source_payload={"turn_summary": "Unrelated queued review job."},
    )
    job = store.enqueue_review_job(
        owner_team_id="team_live_pg",
        owner_named_agent_type="MemReviewAgent",
        task_id="task_live_pg_archive",
        conversation_id="conversation_live_pg",
        turn_index=1,
        source_payload={
            "turn_summary": "Validate live Postgres-backed memory review jobs."
        },
    )
    claimed = store.claim_review_job(
        job.review_job_id,
        owner_team_id="team_live_pg",
        owner_named_agent_type="MemReviewAgent",
        claimer_agent_id="manager_live_pg",
        claim_ttl_seconds=60,
    )
    assert claimed is not None
    assert claimed.review_job_id == job.review_job_id
    untouched_backlog = store.get_review_job(backlog_job.review_job_id)
    assert untouched_backlog is not None
    assert untouched_backlog.status == "queued"
    store.mark_review_job_completed(
        job.review_job_id, claimer_agent_id="manager_live_pg"
    )
    completed = store.get_review_job(job.review_job_id)
    assert completed is not None
    assert completed.status == "completed"
    record_check(report, "memory_review_store", {"review_job_id": job.review_job_id})


def exercise_memory_graph_repository(
    settings: StorageSettings, report: dict[str, Any]
) -> None:
    repo = create_memory_graph_repository(settings, logger=logger)
    delta = repo.upsert_task_turn_delta(
        task_id="task_live_pg_archive",
        conversation_id="conversation_live_pg",
        turn_id="task_live_pg_archive:turn:1",
        turn_index=1,
        source_kind="conversation_turn",
        actor_id="manager_live_pg",
        actor_name="TeamArchitect",
        actor_type="agent_role",
        turn_kind="status_update",
        delta={
            "status": "completed",
            "next_actions": ["Ship the Postgres-backed memory graph."],
        },
        delta_summary="Task completed and the next action is to ship the graph.",
        extractor_version="live_validation",
        batch_id="live_validation_batch",
    )
    snapshot = repo.create_task_state_snapshot(
        task_id="task_live_pg_archive",
        conversation_id="conversation_live_pg",
        through_turn_index=1,
        status="completed",
        assignee_id="TeamArchitect",
        priority=None,
        due_at=None,
        active_blockers=[],
        active_dependencies=[],
        open_questions=[],
        next_actions=["Ship the Postgres-backed memory graph."],
        current_summary="Live Postgres validation of the graph path completed.",
        state={"status": "completed"},
    )
    episode = repo.ensure_episode(
        subject_id="memory_graph_subject_live_pg",
        conversation_id="conversation_live_pg",
        task_id="task_live_pg_archive",
        turn_index=1,
        source_kind="conversation_turn",
        request_text="Prefer Postgres for shared persistence.",
        event_time=utc_now_iso(),
    )
    owner = repo.ensure_entity(
        subject_id=episode.subject_id,
        entity_type="person",
        canonical_name="Live PG User",
        aliases=["user:live_pg_user"],
        source_episode_id=episode.episode_id,
    )
    backend = repo.ensure_entity(
        subject_id=episode.subject_id,
        entity_type="concept",
        canonical_name="Postgres",
        aliases=["postgresql"],
        source_episode_id=episode.episode_id,
    )
    claim = repo.create_claim(
        subject_id=episode.subject_id,
        subject_entity_id=owner.entity_id,
        predicate="prefers_storage_backend",
        object_entity_id=backend.entity_id,
        object_literal=None,
        fact_text="Prefer Postgres for shared persistence.",
        claim_type="decision",
        confidence=1.0,
        valid_from_utc=utc_now_iso(),
        metadata={"section": "accepted_decisions"},
    )
    evidence = repo.add_claim_evidence(
        claim_id=claim.claim_id,
        episode_id=episode.episode_id,
        evidence_text=claim.fact_text,
        evidence_kind="request",
        span={"section": "accepted_decisions"},
    )
    case_memory = repo.upsert_case_memory(
        task_id="task_live_pg_archive",
        conversation_id="conversation_live_pg",
        project_key=None,
        case_type="completed_task",
        summary="Validated the Postgres-backed memory graph path live.",
        resolution="Storage validation passed.",
        outcome_quality="successful",
        key_blockers=[],
        dependencies=[],
        tools_used=["psycopg"],
        lessons_learned=["Validate the task-native path against a real Postgres DSN."],
    )
    searched = repo.search_claims(
        subject_id=episode.subject_id,
        query_text="postgres persistence",
        limit=5,
    )
    assert searched and searched[0].claim_id == claim.claim_id
    assert (
        repo.list_evidence_for_claims([claim.claim_id])[0].claim_evidence_id
        == evidence.claim_evidence_id
    )
    record_check(
        report,
        "memory_graph_repository",
        {
            "delta_id": delta.delta_id,
            "snapshot_id": snapshot.snapshot_id,
            "episode_id": episode.episode_id,
            "claim_id": claim.claim_id,
            "evidence_id": evidence.claim_evidence_id,
            "case_memory_id": case_memory.case_memory_id,
        },
    )


def exercise_gateway_state_store(
    settings: StorageSettings, report: dict[str, Any]
) -> None:
    store = GatewayStateStore(
        db_path="ignored.db", storage_settings=settings, logger_=logger
    )
    assert store.list_sessions() == []
    session = store.create_session(
        session_key="session_live_pg",
        session_alias="live",
        conversation_id="conversation_live_pg",
        manager_agent_id="manager_live_pg",
        channel_name="console",
        user_id="user_1",
        chat_id="chat_1",
    )
    assert session.session_key == "session_live_pg"
    store.register_conversation_session(
        session_key="session_live_pg",
        conversation_id="conversation_live_pg",
        last_task_id="task_live_pg_archive",
    )
    store.record_request(
        correlation_id="corr_live_pg_1",
        session_key="session_live_pg",
        request_kind="user_message",
        request_text="Validate gateway state persistence.",
        conversation_id="conversation_live_pg",
        followup_to_task_id=None,
        status="pending",
    )
    request = store.get_request("corr_live_pg_1")
    assert request is not None
    assert request.request_text == "Validate gateway state persistence."
    delivery_id = store.add_deferred_delivery(
        session_key="session_live_pg",
        origin_kind="manager_reply",
        payload={"text": "Deferred reply"},
        rendered_text="Deferred reply",
        available_after=(datetime.now(timezone.utc) - timedelta(seconds=1)).isoformat(),
        correlation_id="corr_live_pg_1",
    )
    due = store.get_due_deliveries()
    assert [row.delivery_id for row in due] == [delivery_id]
    store.mark_delivery_delivered(delivery_id)
    assert store.get_due_deliveries() == []
    store.upsert_pending_query(
        session_key="session_live_pg",
        correlation_id="corr_live_pg_query",
        task_query_id="task_query_live_pg",
        parent_task_id="task_live_pg_archive",
        conversation_id="conversation_live_pg",
        query_text="Do you want a follow-up validation?",
        status="pending",
    )
    assert store.get_pending_query("session_live_pg") is not None
    store.clear_pending_query("session_live_pg")
    assert store.get_pending_query("session_live_pg") is None
    store.upsert_scheduled_target(
        scheduled_job_id="scheduled_live_pg",
        session_key="session_live_pg",
        conversation_id="conversation_live_pg",
    )
    assert store.get_scheduled_target("scheduled_live_pg") is not None
    assert store.remove_scheduled_target("scheduled_live_pg") is True
    transcript_session = store.create_session(
        session_key="website:web-chat:live_pg",
        conversation_id="conversation_web_chat_live_pg",
        manager_agent_id="manager_live_pg",
        channel_name="website",
        user_id="usr_live_pg_admin",
        chat_id="web-chat",
        owner_canonical_user_id="usr_live_pg_admin",
    )
    transcript_request = TranscriptAppendRequest(
        message_id="message_web_chat_live_pg",
        session_key=transcript_session.session_key,
        conversation_id=transcript_session.conversation_id,
        owner_canonical_user_id="usr_live_pg_admin",
        channel_name="website",
        chat_id="web-chat",
        role="user",
        content="Validate durable Web Chat history in Postgres.",
        message_type="user_message",
        message_level="normal",
        correlation_id="corr_web_chat_live_pg",
        source_kind="client_request",
        source_id="client_web_chat_live_pg",
        idempotency_key="web_chat_live_pg_request_1",
    )
    transcript_message = store.transcript_repository.append_transcript_message(
        transcript_request
    )
    duplicate_message = store.transcript_repository.append_transcript_message(
        transcript_request
    )
    restarted_store = GatewayStateStore(
        db_path="ignored.db", storage_settings=settings, logger_=logger
    )
    restarted_messages = restarted_store.transcript_repository.list_transcript_messages(
        owner_canonical_user_id="usr_live_pg_admin",
        session_key=transcript_session.session_key,
        before_session_sequence=None,
        limit=10,
    )
    assert duplicate_message.message_id == transcript_message.message_id
    assert [message.message_id for message in restarted_messages] == [
        transcript_message.message_id
    ]
    record_check(
        report,
        "gateway_state_store",
        {
            "session_key": session.session_key,
            "delivery_id": delivery_id,
            "transcript_message_id": transcript_message.message_id,
        },
    )


def exercise_adapter_host_repository(
    settings: StorageSettings, report: dict[str, Any]
) -> None:
    """Exercise the durable adapter stream on the real Postgres compatibility layer."""

    repository = create_adapter_host_repository(settings, logger=logger)
    now = utc_now_iso()
    plugin_id = "live.adapter.echo"
    adapter_instance_id = "live:adapter:echo"
    registration_epoch = "epoch-live-adapter-1"
    registration = AdapterRegistrationRecord(
        authenticated_host_id="live-adapter-host",
        plugin_id=plugin_id,
        adapter_instance_id=adapter_instance_id,
        registration_id="registration-live-adapter-1",
        registration_epoch=registration_epoch,
        configuration_digest="a" * 64,
        plugin_api_version="1",
        implementation_version="live-test",
        channel_names_json='["echo"]',
        offered_capabilities_json='["inbound_text","outbound_text"]',
        negotiated_capabilities_json='["inbound_text","outbound_text"]',
        negotiated_limits_json=json.dumps(
            {
                "max_in_flight_events": 2,
                "max_payload_bytes": 100000,
                "max_attachment_bytes": 10000,
                "max_pending_events": 10,
                "heartbeat_interval_seconds": 5,
                "acknowledgement_timeout_seconds": 30,
                "retention_seconds": 60,
            },
            separators=(",", ":"),
        ),
        lease_expires_at_utc=(
            datetime.now(timezone.utc) + timedelta(minutes=5)
        ).isoformat(),
        last_heartbeat_at_utc=now,
        state="ready",
        last_inbound_position=0,
        last_outbound_received_position=0,
        last_outbound_completed_position=0,
        reconnect_count=0,
        error_code=None,
        detail=None,
        created_at_utc=now,
        updated_at_utc=now,
    )
    repository.save_registration(registration)
    assert repository.get_registration(plugin_id, adapter_instance_id) == registration

    inbox = AdapterInboxRecord(
        authenticated_host_id="live-adapter-host",
        plugin_id=plugin_id,
        adapter_instance_id=adapter_instance_id,
        registration_epoch=registration_epoch,
        idempotency_key="live-inbound-idem-1",
        request_id="live-inbound-request-1",
        stream_position=1,
        message_type="channel.inbound.v1",
        payload_json='{"text":"hello"}',
        correlation_id="live-correlation-1",
        admission_json='{"status":"accepted"}',
        status="accepted",
        result_json=None,
        error_code=None,
        detail=None,
        created_at_utc=now,
        updated_at_utc=now,
    )
    admitted = repository.admit_inbox(inbox, expected_previous_position=0)
    duplicate = repository.admit_inbox(inbox, expected_previous_position=0)
    assert admitted.request_id == duplicate.request_id
    assert repository.mark_inbox_processing(inbox.request_id)
    repository.complete_inbox(inbox.request_id, status="completed", result_json="{}")

    queued = repository.enqueue_outbox(
        event_id="live-outbound-event-1",
        idempotency_key="live-outbound-idem-1",
        plugin_id=plugin_id,
        adapter_instance_id=adapter_instance_id,
        payload_json='{"text":"reply"}',
        max_pending_events=10,
    )
    duplicate_queued = repository.enqueue_outbox(
        event_id="live-outbound-event-duplicate",
        idempotency_key="live-outbound-idem-1",
        plugin_id=plugin_id,
        adapter_instance_id=adapter_instance_id,
        payload_json='{"text":"reply"}',
        max_pending_events=10,
    )
    assert queued.event_id == duplicate_queued.event_id
    repository.acknowledge_outbox(
        plugin_id=plugin_id,
        adapter_instance_id=adapter_instance_id,
        registration_epoch=registration_epoch,
        event_id=queued.event_id,
        stream_position=queued.stream_position,
        stage="received",
        outcome=None,
        provider_message_id=None,
        error_code=None,
        detail=None,
        observed_at_utc=now,
    )
    completed = repository.acknowledge_outbox(
        plugin_id=plugin_id,
        adapter_instance_id=adapter_instance_id,
        registration_epoch=registration_epoch,
        event_id=queued.event_id,
        stream_position=queued.stream_position,
        stage="completed",
        outcome="delivered",
        provider_message_id="live-provider-message-1",
        error_code=None,
        detail=None,
        observed_at_utc=now,
    )
    assert completed.status == "delivered"
    health = repository.health_snapshot(plugin_id, adapter_instance_id)
    assert health is not None
    assert health.last_inbound_position == 1
    assert health.last_outbound_completed_position == 1
    assert health.pending_event_count == 0
    record_check(
        report,
        "adapter_host_repository",
        {
            "plugin_id": plugin_id,
            "inbound_position": health.last_inbound_position,
            "outbound_position": health.last_outbound_completed_position,
            "duplicate_admission_idempotent": True,
            "duplicate_delivery_idempotent": True,
        },
    )


def exercise_gateway_owner_assignment(settings: StorageSettings, report: dict[str, Any]) -> None:
    """Gateway assignment must preserve manager rows even in a shared database."""

    store = GatewayStateStore(db_path="ignored.db", storage_settings=settings)
    for label, owner in (("unassigned", None), ("member", "usr_live_pg_member")):
        store.create_session(
            session_key=f"session_owner_assignment_{label}",
            conversation_id=f"conversation_owner_assignment_{label}",
            manager_agent_id="manager_live_pg",
            owner_canonical_user_id=owner,
        )
    with connect_postgres(settings.postgres_dsn or "") as conn:
        manager_before = conn.execute(
            "SELECT owner_canonical_user_id, schedule_json FROM scheduled_messages "
            "WHERE schedule_id = %s", ("schedule_live_pg_ownerless",),
        ).fetchone()
    assignment = migrate_legacy_null_owner_rows(
        owner_canonical_user_id="usr_live_pg_admin", gateway_storage_settings=settings
    )
    with connect_postgres(settings.postgres_dsn or "") as conn:
        owners = conn.execute(
            "SELECT session_key, owner_canonical_user_id FROM gateway_sessions "
            "WHERE session_key IN (%s, %s)",
            ("session_owner_assignment_unassigned", "session_owner_assignment_member"),
        ).fetchall()
        manager_after = conn.execute(
            "SELECT owner_canonical_user_id, schedule_json FROM scheduled_messages "
            "WHERE schedule_id = %s", ("schedule_live_pg_ownerless",),
        ).fetchone()
    assert {row[0]: row[1] for row in owners} == {
        "session_owner_assignment_unassigned": "usr_live_pg_admin",
        "session_owner_assignment_member": "usr_live_pg_member",
    }
    assert manager_before == manager_after and manager_after[0] is None
    assert assignment.updated_rows > 0
    repeated = migrate_legacy_null_owner_rows(
        owner_canonical_user_id="usr_live_pg_admin", gateway_storage_settings=settings
    )
    assert repeated.updated_rows == 0
    record_check(report, "gateway_owner_assignment", {"manager_unchanged": True, "repeat_updated_rows": 0})


def exercise_old_access_table_drop(settings: StorageSettings, report: dict[str, Any]) -> None:
    """``assistant_cli access drop-old-tables`` on PostgreSQL: finds and drops only old tables."""

    from storage.old_access_tables import OLD_STATE_TABLES, OldTableDatabase, drop_old_tables, find_old_tables
    from storage.postgres_connection import connect_postgres

    dsn = settings.postgres_dsn or ""
    with connect_postgres(dsn) as conn:
        conn.execute("CREATE TABLE permission_requests (request_id TEXT PRIMARY KEY)")
        conn.execute(
            "CREATE TABLE permission_grants (grant_id TEXT PRIMARY KEY, "
            "request_id TEXT REFERENCES permission_requests(request_id))"
        )
        conn.execute("CREATE TABLE human_interactions (interaction_id TEXT PRIMARY KEY)")
        conn.execute("INSERT INTO permission_requests VALUES ('req_live_pg')")
        conn.execute("INSERT INTO permission_grants VALUES ('grant_live_pg', 'req_live_pg')")
    database = OldTableDatabase("manager database", settings, OLD_STATE_TABLES)
    found = find_old_tables(database)
    assert [table.name for table in found] == ["human_interactions", "permission_grants", "permission_requests"]
    assert {table.name: table.rows for table in found}["permission_grants"] == 1
    dropped = drop_old_tables(database, found)
    assert find_old_tables(database) == []
    # Current tables created by the exercises above are untouched.
    with connect_postgres(dsn) as conn:
        remaining = {
            row["table_name"]
            for row in conn.execute(
                "SELECT table_name FROM information_schema.tables WHERE table_schema = current_schema()"
            ).fetchall()
        }
    assert "gateway_users" in remaining and "scheduled_messages" in remaining
    record_check(report, "old_access_table_drop", {"dropped": dropped, "observation_only": True})


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Run live storage validation against the configured Postgres instance."
    )
    parser.add_argument(
        "--env", default=None, help="Optional env file to load before validation."
    )
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s"
    )
    if args.env and load_dotenv:
        load_dotenv(args.env, override=True)

    if RESULT_DIR.exists():
        shutil.rmtree(RESULT_DIR)
    RESULT_DIR.mkdir(parents=True, exist_ok=True)

    report: dict[str, Any] = {
        "started_at": utc_now_iso(),
        "status": "running",
        "checks": [],
    }

    try:
        settings = require_postgres_settings()
        report["storage"] = {
            "backend": settings.backend,
            "dsn": redact_postgres_dsn(settings.postgres_dsn or ""),
        }
        logger.info(
            "Resetting Postgres validation database: %s",
            redact_postgres_dsn(settings.postgres_dsn or ""),
        )
        reset_postgres_database(settings.postgres_dsn or "")
        record_check(
            report, "baseline_reset", {"dsn": redact_postgres_dsn(settings.postgres_dsn or "")}
        )

        exercise_agent_state_store(settings, report)
        exercise_runtime_state_store(settings, report)
        exercise_task_manager_and_conversation_store(settings, RESULT_DIR, report)
        exercise_solution_variation_repository(settings, report)
        exercise_schedule_store(settings, report)
        exercise_heartbeat_store(settings, report)
        exercise_memory_review_store(settings, report)
        assert_report_person_scoping(create_after_action_report_repository(settings))
        record_check(report, "after_action_report_person_scoping", {"people_and_system_work": True, "owner_changes_refused": True})
        exercise_memory_graph_repository(settings, report)
        exercise_gateway_state_store(settings, report)
        exercise_gateway_owner_assignment(settings, report)
        exercise_adapter_host_repository(settings, report)
        exercise_old_access_table_drop(settings, report)

        report["status"] = "passed"
        report["finished_at"] = utc_now_iso()
        write_json_file(RESULT_DIR / "report.json", report)
        logger.info(
            "Live Postgres validation passed. Report written to %s",
            RESULT_DIR / "report.json",
        )
    except Exception as exc:
        report["status"] = "failed"
        report["finished_at"] = utc_now_iso()
        report["error"] = {"type": type(exc).__name__, "message": str(exc)}
        write_json_file(RESULT_DIR / "report.json", report)
        logger.exception("Live Postgres validation failed.")
        raise


if __name__ == "__main__":
    main()
