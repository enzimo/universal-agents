from __future__ import annotations

import json
import logging
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from custom_agents.conversation_store import ConversationStore
from custom_agents.memory_review_runtime import (
    enqueue_pinned_memory_audit_job,
    enqueue_conversation_window_review_job,
    enqueue_stale_conversation_review_jobs,
)
from custom_agents.memory_review_store import MemoryReviewStore
from custom_agents.scheduled_runtime import (
    prepare_memory_review_schedule as run_memory_review_schedule,
)
from custom_messages.contracts.routing_v1 import GatewayContext, MessageMeta
from custom_messages.contracts.schedule_v1 import ScheduledMessage
from custom_messages.contracts.task_v1 import Task
from custom_messages.enums import (
    MessageMetaType,
    RequesterType,
    ScheduleManagementClass,
)
from custom_messages.system_schedules import (
    COREMEM_AUDIT_SCHEDULE_NAME_PREFIX,
    STALE_CONVERSATION_SCHEDULE_NAME_PREFIX,
    USERMEM_AUDIT_SCHEDULE_NAME_PREFIX,
)
from custom_tools.providers.memory import resolve_memory_review_job
from tests.task_work_support import set_agent_ref
from storage.config import resolve_storage_settings
from storage.conversation_repository import (
    MEMORY_REVIEW_PROMPT_LIKE_PATTERN,
    TURN_MEMORY_REVIEW_IGNORED,
    TURN_MEMORY_REVIEW_QUEUED,
    TURN_MEMORY_REVIEW_UNREVIEWED,
    SqliteConversationRepository,
)
from storage.pinned_memory_repository import SqlitePinnedMemoryRepository
from tests.change_approval_support import SignedTaskManager, use_signed_contexts

TEST_REQUESTER_NAME = "Test User"


REVIEW_PERSON = "user_review"


@pytest.fixture
def signed_contexts(monkeypatch: pytest.MonkeyPatch) -> None:
    use_signed_contexts(monkeypatch)


def _reviewer(memory_store: MemoryReviewStore, tmp_path: Path) -> SimpleNamespace:
    """MemReviewAgent on a task for REVIEW_PERSON (resolving is limited to that person's jobs)."""

    storage = resolve_storage_settings({"backend": "sqlite", "sqlite": {"path": str(tmp_path / "manager.db")}})
    return SimpleNamespace(
        id="memreview_current",
        team_id="team_test",
        named_agent_type="MemReviewAgent",
        _memory_review_store=memory_store,
        _conversation_store=None,
        task_manager=SignedTaskManager(team_id="team_test", person_id=REVIEW_PERSON, storage_settings=storage),
        authorization_config={"enabled": False},
    )


def test_memory_review_eligibility_binds_like_pattern() -> None:
    captured: dict[str, object] = {}

    class _Cursor:
        def fetchall(self):
            return []

    class _Connection:
        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return None

        def execute(self, query, params):
            captured["query"] = query
            captured["params"] = tuple(params)
            return _Cursor()

    repository = object.__new__(SqliteConversationRepository)
    repository._connect = _Connection

    assert repository.list_conversation_memory_review_candidates(team_id="team_test") == []
    assert "NOT LIKE ?" in str(captured["query"])
    assert "%" not in str(captured["query"])
    assert captured["params"] == (
        TURN_MEMORY_REVIEW_UNREVIEWED,
        MEMORY_REVIEW_PROMPT_LIKE_PATTERN,
        "team_test",
        50,
    )


def _stores(tmp_path: Path) -> tuple[ConversationStore, MemoryReviewStore, SqlitePinnedMemoryRepository]:
    db_path = str(tmp_path / "memory_review.db")
    logger = logging.getLogger("memory_review_test")
    return (
        ConversationStore(db_path=db_path, logger=logger, artifacts_dir=Path(db_path).parent / "conversation_artifacts"),
        MemoryReviewStore(db_path=db_path, logger=logger),
        SqlitePinnedMemoryRepository(db_path, logger=logger),
    )


def _seed_turns(
    store: ConversationStore,
    *,
    conversation_id: str = "conversation_review",
    count: int = 15,
    owner_canonical_user_id: str | None = "canonical_user_1",
) -> None:
    store.ensure_thread(
        conversation_id=conversation_id,
        team_id="team_test",
        owner_agent_id="agent_team",
        owner_agent_name="TeamArchitect",
        requester_name=TEST_REQUESTER_NAME,
        requester_id="user_1",
        root_task_id="task_1",
        subject_preview="Memory review test",
        owner_canonical_user_id=owner_canonical_user_id,
    )
    for index in range(1, count + 1):
        task_id = f"task_{index}"
        store.append_turn(
            conversation_id=conversation_id,
            task_id=task_id,
            followup_to_task_id=f"task_{index - 1}" if index > 1 else None,
            owner_agent_id="agent_team",
            requester_name=TEST_REQUESTER_NAME,
            requester_id="user_1",
            requester_type=RequesterType.USER_REQUESTER.value,
            correlation_id=f"corr_{index}",
            problem_text=f"Question {index} about car repair.",
            gateway_context=GatewayContext(
                gateway_instance_name="test_gateway",
                session_key="session_1",
                channel_name="cli",
                user_id="user_1",
                canonical_user_id=owner_canonical_user_id,
                chat_id="chat_1",
                delivery_mode="interactive",
            ).model_dump(mode="json"),
            task_status="accepted",
        )
        store.update_turn_completion(
            task_id=task_id,
            task_status="completed",
            final_answer_text=f"Answer {index}",
            turn_summary_text=f"Turn {index} summary",
            user_request_summary=f"User asked question {index}",
            assistant_response_summary=f"Assistant answered question {index}",
        )


def _append_completed_turn(
    store: ConversationStore,
    *,
    conversation_id: str,
    task_id: str,
    followup_to_task_id: str | None,
    requester_type: str,
    problem_text: str,
    owner_agent_id: str = "agent_team",
    owner_named_agent_type: str = "TeamArchitect",
    correlation_id: str = "corr_extra",
) -> None:
    store.append_turn(
        conversation_id=conversation_id,
        task_id=task_id,
        followup_to_task_id=followup_to_task_id,
        owner_agent_id=owner_agent_id,
        owner_named_agent_type=owner_named_agent_type,
        requester_name=TEST_REQUESTER_NAME if requester_type == RequesterType.USER_REQUESTER.value else owner_named_agent_type,
        requester_id="user_1" if requester_type == RequesterType.USER_REQUESTER.value else owner_agent_id,
        requester_type=requester_type,
        correlation_id=correlation_id,
        problem_text=problem_text,
        gateway_context=(
            GatewayContext(
                gateway_instance_name="test_gateway",
                session_key="session_1",
                channel_name="cli",
                user_id="user_1",
                canonical_user_id="canonical_user_1",
                chat_id="chat_1",
                delivery_mode="interactive",
            ).model_dump(mode="json")
            if requester_type == RequesterType.USER_REQUESTER.value
            else None
        ),
        task_status="accepted",
    )
    store.update_turn_completion(
        task_id=task_id,
        task_status="completed",
        final_answer_text=f"Answer for {task_id}",
        turn_summary_text=f"Summary for {task_id}",
        user_request_summary=problem_text,
        assistant_response_summary=f"Answer for {task_id}",
    )


def test_memory_review_windows_ignore_internal_memreview_turns(tmp_path: Path) -> None:
    conversation_store, _, _ = _stores(tmp_path)
    _seed_turns(conversation_store, count=1)
    _append_completed_turn(
        conversation_store,
        conversation_id="conversation_review",
        task_id="memory_review_task_1",
        followup_to_task_id="task_1",
        requester_type=RequesterType.AGENT_REQUESTER.value,
        problem_text="Review the queued memory job below.\nPrior review details.",
        owner_agent_id="memreview_1",
        owner_named_agent_type="MemReviewAgent",
        correlation_id="memory_review_1",
    )
    _append_completed_turn(
        conversation_store,
        conversation_id="conversation_review",
        task_id="task_3",
        followup_to_task_id="memory_review_task_1",
        requester_type=RequesterType.USER_REQUESTER.value,
        problem_text="Question 3 about car repair.",
        correlation_id="corr_3",
    )

    internal_turn = conversation_store.get_turn_by_task_id("memory_review_task_1")
    assert internal_turn is not None
    assert internal_turn["memory_review_status"] == TURN_MEMORY_REVIEW_IGNORED

    window = conversation_store.get_memory_review_window(
        conversation_id="conversation_review",
        max_unreviewed_turns=2,
    )

    assert window is not None
    assert window["unreviewed_turn_indexes"] == [1, 3]
    assert [turn["turn_index"] for turn in window["turns"]] == [1, 3]

    queued = conversation_store.mark_turns_memory_review_queued(
        conversation_id="conversation_review",
        start_turn=1,
        end_turn=3,
        review_job_id="review_job_mixed",
    )

    assert queued == 2
    assert conversation_store.get_turn_by_task_id("memory_review_task_1")["memory_review_status"] == TURN_MEMORY_REVIEW_IGNORED


def test_conversation_store_tracks_memory_review_windows_and_status(tmp_path: Path) -> None:
    conversation_store, _, _ = _stores(tmp_path)
    _seed_turns(conversation_store, count=20)

    marked = conversation_store.mark_turns_memory_reviewed(
        conversation_id="conversation_review",
        start_turn=1,
        end_turn=10,
        review_job_id="review_job_old",
        reviewed_by_agent_id="memreview_1",
    )
    assert marked == 10

    window = conversation_store.get_memory_review_window(
        conversation_id="conversation_review",
        max_unreviewed_turns=5,
        overlap_turns=3,
    )
    assert window is not None
    assert window["window_start_turn"] == 8
    assert window["window_end_turn"] == 15
    assert window["unreviewed_turn_indexes"] == [11, 12, 13, 14, 15]
    assert [turn["turn_index"] for turn in window["turns"]] == list(range(8, 16))

    queued = conversation_store.mark_turns_memory_review_queued(
        conversation_id="conversation_review",
        start_turn=11,
        end_turn=15,
        review_job_id="review_job_new",
    )
    assert queued == 5
    turn_11 = conversation_store.get_conversation_turns(
        conversation_id="conversation_review",
        start_turn=11,
        limit=1,
    )[0]
    assert turn_11["memory_review_status"] == TURN_MEMORY_REVIEW_QUEUED
    assert turn_11["memory_review_job_id"] == "review_job_new"

    candidates = conversation_store.list_conversation_memory_review_candidates(team_id="team_test")
    assert candidates[0]["unreviewed_count"] == 5
    assert candidates[0]["first_unreviewed_turn"] == 16


def test_conversation_window_review_enqueues_at_configured_unreviewed_turn_count(tmp_path: Path) -> None:
    conversation_store, memory_store, _ = _stores(tmp_path)
    _seed_turns(conversation_store, count=15)
    task = Task(
        task_id="task_15",
        task_type=MessageMetaType.REQUEST,
        problem_statement="Question 15 about car repair.",
    )
    meta = MessageMeta(
        conversation_id="conversation_review",
        requester_type=RequesterType.USER_REQUESTER,
        gateway_context=GatewayContext(canonical_user_id="canonical_user_1"),
    )

    job_ref = enqueue_conversation_window_review_job(
        memory_review_store=memory_store,
        conversation_store=conversation_store,
        team_id="team_test",
        named_agent_type="TeamArchitect",
        agent_id="agent_team",
        task=task,
        msg_meta=meta,
        canonical_user_id="canonical_user_1",
        task_status="completed",
        memory_config={"review": {"periodic_turn_interval": 15, "sliding_window_overlap_turns": 5}},
        trigger="turn_cadence",
    )

    assert job_ref is not None
    job = memory_store.get_review_job(job_ref.job_id)
    assert job is not None
    assert job.owner_named_agent_type == "MemReviewAgent"
    assert job.source_payload["review_kind"] == "conversation_window"
    assert job.source_payload["reviewed_turn_range"]["start"] == 1
    assert job.source_payload["reviewed_turn_range"]["end"] == 15
    assert len(job.source_payload["turns"]) == 15
    assert conversation_store.get_conversation_turns(
        conversation_id="conversation_review",
        start_turn=1,
        limit=1,
    )[0]["memory_review_status"] == TURN_MEMORY_REVIEW_QUEUED

    duplicate = enqueue_conversation_window_review_job(
        memory_review_store=memory_store,
        conversation_store=conversation_store,
        team_id="team_test",
        named_agent_type="TeamArchitect",
        agent_id="agent_team",
        task=task,
        msg_meta=meta,
        canonical_user_id="canonical_user_1",
        task_status="completed",
        memory_config={"review": {"periodic_turn_interval": 15, "sliding_window_overlap_turns": 5}},
        trigger="turn_cadence",
    )
    assert duplicate is None


def test_stale_conversation_review_enqueues_partial_final_window(tmp_path: Path) -> None:
    conversation_store, memory_store, _ = _stores(tmp_path)
    _seed_turns(conversation_store, count=3)
    conversation_store.update_thread_compaction(
        "conversation_review",
        latest_task_id="task_3",
        status="completed",
    )
    with conversation_store._repository._connect() as conn:
        conn.execute(
            "UPDATE conversation_threads SET updated_at = ? WHERE conversation_id = ?",
            ("2026-01-01T00:00:00+00:00", "conversation_review"),
        )
        conn.commit()

    jobs = enqueue_stale_conversation_review_jobs(
        memory_review_store=memory_store,
        conversation_store=conversation_store,
        team_id="team_test",
        named_agent_type="TeamArchitect",
        agent_id="agent_team",
        memory_config={"review": {"periodic_turn_interval": 15, "stale_conversation_after_seconds": 86400}},
        canonical_user_id="canonical_user_1",
        now_utc="2026-01-03T00:00:00+00:00",
    )

    assert len(jobs) == 1
    job = memory_store.get_review_job(jobs[0].job_id)
    assert job is not None
    assert job.source_payload["trigger"] == "stale_conversation"
    assert job.source_payload["canonical_user_id"] == "canonical_user_1"
    assert job.source_payload["gateway_context"]["canonical_user_id"] == "canonical_user_1"
    assert job.source_payload["reviewed_turn_range"]["unreviewed_turn_indexes"] == [1, 2, 3]


def test_stale_conversation_review_anchors_to_latest_reviewable_turn(tmp_path: Path) -> None:
    conversation_store, memory_store, _ = _stores(tmp_path)
    _seed_turns(conversation_store, count=2)
    _append_completed_turn(
        conversation_store,
        conversation_id="conversation_review",
        task_id="memory_review_task_latest",
        followup_to_task_id="task_2",
        requester_type=RequesterType.AGENT_REQUESTER.value,
        problem_text="Review the queued memory job below.\nPrevious memory review prompt.",
        owner_agent_id="memreview_1",
        owner_named_agent_type="MemReviewAgent",
        correlation_id="memory_review_latest",
    )
    conversation_store.update_thread_compaction(
        "conversation_review",
        latest_task_id="memory_review_task_latest",
        status="completed",
    )
    with conversation_store._repository._connect() as conn:
        conn.execute(
            "UPDATE conversation_threads SET updated_at = ? WHERE conversation_id = ?",
            ("2026-01-01T00:00:00+00:00", "conversation_review"),
        )
        conn.commit()

    jobs = enqueue_stale_conversation_review_jobs(
        memory_review_store=memory_store,
        conversation_store=conversation_store,
        team_id="team_test",
        named_agent_type="TeamArchitect",
        agent_id="agent_team",
        memory_config={"review": {"periodic_turn_interval": 15, "stale_conversation_after_seconds": 86400}},
        canonical_user_id="canonical_user_1",
        now_utc="2026-01-03T00:00:00+00:00",
    )

    assert len(jobs) == 1
    job = memory_store.get_review_job(jobs[0].job_id)
    assert job is not None
    assert job.task_id == "task_2"
    assert job.source_payload["task_id"] == "task_2"
    assert job.source_payload["reviewed_turn_range"]["unreviewed_turn_indexes"] == [1, 2]
    assert [turn["turn_index"] for turn in job.source_payload["turns"]] == [1, 2]
    assert conversation_store.get_turn_by_task_id("memory_review_task_latest")["memory_review_status"] == TURN_MEMORY_REVIEW_IGNORED


def test_resolve_memory_review_job_reports_claimed_by_other_agent(tmp_path: Path, signed_contexts: None) -> None:
    _, memory_store, _ = _stores(tmp_path)
    job = memory_store.enqueue_review_job(
        owner_team_id="team_test",
        owner_named_agent_type="MemReviewAgent",
        task_id="task_1",
        conversation_id="conversation_review",
        turn_index=1,
        source_payload={"review_kind": "conversation_window", "canonical_user_id": REVIEW_PERSON},
    )
    memory_store.claim_review_jobs_for_role(
        owner_team_id="team_test",
        owner_named_agent_type="MemReviewAgent",
        claimer_agent_id="memreview_other",
        limit=1,
    )
    set_agent_ref(_reviewer(memory_store, tmp_path))

    try:
        result = json.loads(resolve_memory_review_job(job.review_job_id))
    finally:
        set_agent_ref(None)

    assert result["error_code"] == "claimed_by_other_agent"
    assert result["status"] == "claimed"
    assert result["claim_owner_agent_id"] == "memreview_other"
    assert result["claim_expires_at"]


def test_resolve_memory_review_job_claims_addressed_job_behind_large_backlog(tmp_path: Path, signed_contexts: None) -> None:
    _, memory_store, _ = _stores(tmp_path)
    older_jobs = [
        memory_store.enqueue_review_job(
            owner_team_id="team_test",
            owner_named_agent_type="MemReviewAgent",
            task_id=f"task_backlog_{index}",
            conversation_id="conversation_review",
            turn_index=index,
            source_payload={"review_kind": "coremem_audit", "canonical_user_id": REVIEW_PERSON},
        )
        for index in range(51)
    ]
    target = memory_store.enqueue_review_job(
        owner_team_id="team_test",
        owner_named_agent_type="MemReviewAgent",
        task_id="task_target",
        conversation_id="conversation_review",
        turn_index=52,
        source_payload={"review_kind": "coremem_audit", "canonical_user_id": REVIEW_PERSON},
    )
    set_agent_ref(_reviewer(memory_store, tmp_path))

    try:
        result = json.loads(resolve_memory_review_job(target.review_job_id))
    finally:
        set_agent_ref(None)

    assert result["status"] == "completed"
    assert all(memory_store.get_review_job(job.review_job_id).status == "queued" for job in older_jobs)


def test_resolve_memory_review_job_rejects_invalid_status_without_claiming(tmp_path: Path, signed_contexts: None) -> None:
    _, memory_store, _ = _stores(tmp_path)
    job = memory_store.enqueue_review_job(
        owner_team_id="team_test",
        owner_named_agent_type="MemReviewAgent",
        task_id="task_1",
        conversation_id="conversation_review",
        turn_index=1,
        source_payload={"review_kind": "conversation_window", "canonical_user_id": REVIEW_PERSON},
    )
    set_agent_ref(_reviewer(memory_store, tmp_path))

    try:
        result = json.loads(resolve_memory_review_job(job.review_job_id, status="unknown"))
    finally:
        set_agent_ref(None)

    assert result["allowed_statuses"] == ["completed", "skipped", "failed"]
    unchanged = memory_store.get_review_job(job.review_job_id)
    assert unchanged is not None
    assert unchanged.status == "queued"
    assert unchanged.claim_owner_agent_id is None


@pytest.mark.asyncio
async def test_memory_review_dispatch_is_fire_and_forget() -> None:
    from custom_agents.strands_agent import UniversalAgent

    team_comms = SimpleNamespace(send_message=AsyncMock())
    agent = SimpleNamespace(
        id="agent_team",
        name="manager",
        memory_config={"review": {"review_task_ttl_seconds": 600}},
        team_comms=team_comms,
        _logger=logging.getLogger("memory_review_dispatch_test"),
        _verify_delegation_target=AsyncMock(return_value=True),
        _memory_review_task_ttl_seconds=lambda: 600.0,
    )
    review_job = SimpleNamespace(job_id="memory_review_target", plugin_id="pinned_memory_review")
    payload = {
        "review_job_id": "memory_review_target",
        "task_id": "task_source",
        "conversation_id": "conversation_review",
    }

    await UniversalAgent._publish_memory_review_task(
        agent,
        review_job,
        payload,
        "team_test_MemReviewAgent",
    )

    team_comms.send_message.assert_awaited_once()
    call = team_comms.send_message.await_args
    envelope = call.kwargs["message"]
    assert call.kwargs["routing_key"] == "team_test_MemReviewAgent"
    assert call.kwargs["ttl_seconds"] == 600.0
    assert envelope.msg_content.parent_task_id is None
    assert envelope.msg_meta.requester_type == RequesterType.AGENT_REQUESTER
    assert envelope.msg_meta.is_response_requested is False
    assert envelope.msg_header.from_agent_id == "agent_team"
    assert envelope.msg_header.response_routing_key is None


@pytest.mark.asyncio
async def test_scheduled_memory_review_jobs_prepare_for_local_worker(tmp_path: Path) -> None:
    conversation_store, memory_store, pinned_repo = _stores(tmp_path)
    pinned_repo.create_coremem(team_id="team_test", fact_text="Team prefers smoke tests.", capacity=10)
    pinned_repo.create_usermem(canonical_user_id="user_1", fact_text="User prefers concise replies.", capacity=10)
    pinned_repo.create_usermem(canonical_user_id="other_user", fact_text="Private fact for another user.", capacity=10)
    dispatched: list[object] = []
    agent = SimpleNamespace(
        id="agent_team",
        team_id="team_test",
        named_agent_type="MemReviewAgent",
        memory_config={"review": {"enabled": True}},
        _memory_review_store=memory_store,
        _conversation_store=conversation_store,
        _pinned_memory_repository=pinned_repo,
        _logger=logging.getLogger("memory_review_schedule_test"),
        _dispatch_memory_review_job=lambda job: dispatched.append(job),
    )

    for schedule_name in (
        f"{COREMEM_AUDIT_SCHEDULE_NAME_PREFIX}test",
        f"{USERMEM_AUDIT_SCHEDULE_NAME_PREFIX}test",
    ):
        accepted = run_memory_review_schedule(
            agent,
            f"sched_{schedule_name.split(':')[1]}",
            ScheduledMessage(
                schedule_id=f"sched_{schedule_name.split(':')[1]}",
                schedule_name=schedule_name,
                management_class=ScheduleManagementClass.SYSTEM,
                claim_named_agent_type="MemReviewAgent",
                payload_meta=MessageMeta(gateway_context=GatewayContext(canonical_user_id="user_1")),
                payload=Task(task_type=MessageMetaType.REQUEST, problem_statement="maintenance"),
                interval_ms=1000,
            ),
        )
        assert "Saved review job IDs:" in accepted.payload.problem_statement
        assert "Private fact for another user" not in accepted.payload.problem_statement

    assert dispatched == []
    assert {job.source_payload["review_kind"] for job in memory_store.list_review_jobs()} == {"coremem_audit", "usermem_audit"}


def _personal_facts_audit(memory_store, pinned_repo, person: str):
    return enqueue_pinned_memory_audit_job(
        memory_review_store=memory_store,
        pinned_memory_repository=pinned_repo,
        team_id="team_test",
        named_agent_type="MemReviewAgent",
        agent_id="agent_team",
        memory_config={"review": {"enabled": True}},
        bank="usermem",
        canonical_user_id=person,
    )


def test_a_personal_facts_audit_holds_only_its_persons_facts(tmp_path: Path) -> None:
    """The job is made for the review schedule's owner; there is no audit of everyone's facts."""

    _conversations, memory_store, pinned_repo = _stores(tmp_path)
    pinned_repo.create_usermem(canonical_user_id="user_1", fact_text="User prefers concise replies.", capacity=10)
    pinned_repo.create_usermem(canonical_user_id="other_user", fact_text="Private fact for another user.", capacity=10)

    job = _personal_facts_audit(memory_store, pinned_repo, "user_1")

    assert job is not None
    stored = memory_store.get_review_job(job.job_id)
    assert stored is not None and stored.source_payload["canonical_user_id"] == "user_1"
    assert [group["canonical_user_id"] for group in stored.source_payload["facts"]] == ["user_1"]
    assert "Private fact for another user" not in json.dumps(stored.source_payload)


def test_no_personal_facts_audit_without_facts_or_a_person(tmp_path: Path) -> None:
    _conversations, memory_store, pinned_repo = _stores(tmp_path)
    pinned_repo.create_usermem(canonical_user_id="other_user", fact_text="Private fact for another user.", capacity=10)

    assert _personal_facts_audit(memory_store, pinned_repo, "user_1") is None, "nothing to audit"
    assert _personal_facts_audit(memory_store, pinned_repo, "") is None, "nobody could review it"
    assert memory_store.list_review_jobs() == []


@pytest.mark.asyncio
async def test_scheduled_memory_review_audit_skips_bank_when_automatic_creation_disabled(tmp_path: Path) -> None:
    conversation_store, memory_store, pinned_repo = _stores(tmp_path)
    pinned_repo.create_coremem(team_id="team_test", fact_text="Team prefers smoke tests.", capacity=10)
    dispatched: list[object] = []
    agent = SimpleNamespace(
        id="agent_team",
        team_id="team_test",
        named_agent_type="MemReviewAgent",
        memory_config={"review": {"enabled": True}, "coremem": {"automatic_creation_enabled": False}},
        _memory_review_store=memory_store,
        _conversation_store=conversation_store,
        _pinned_memory_repository=pinned_repo,
        _logger=logging.getLogger("memory_review_schedule_test"),
        _dispatch_memory_review_job=lambda job: dispatched.append(job),
    )

    accepted = run_memory_review_schedule(
        agent,
        "sched_coremem",
        ScheduledMessage(
            schedule_id="sched_coremem",
            schedule_name=f"{COREMEM_AUDIT_SCHEDULE_NAME_PREFIX}test",
            payload_meta=MessageMeta(gateway_context=GatewayContext(canonical_user_id="user_1")),
            payload=Task(task_type=MessageMetaType.REQUEST, problem_statement="maintenance"),
            interval_ms=1000,
        ),
    )

    assert "Saved review job IDs: []" in accepted.payload.problem_statement
    assert dispatched == []
    assert memory_store.list_review_jobs() == []


@pytest.mark.asyncio
async def test_scheduled_stale_conversation_review_prepares_local_task(tmp_path: Path) -> None:
    conversation_store, memory_store, pinned_repo = _stores(tmp_path)
    _seed_turns(conversation_store, count=2)
    with conversation_store._repository._connect() as conn:
        conn.execute(
            "UPDATE conversation_threads SET updated_at = ? WHERE conversation_id = ?",
            ("2026-01-01T00:00:00+00:00", "conversation_review"),
        )
        conn.commit()
    dispatched: list[object] = []
    agent = SimpleNamespace(
        id="agent_team",
        team_id="team_test",
        named_agent_type="MemReviewAgent",
        memory_config={"review": {"periodic_turn_interval": 15, "stale_conversation_after_seconds": 60}},
        _memory_review_store=memory_store,
        _conversation_store=conversation_store,
        _pinned_memory_repository=pinned_repo,
        _logger=logging.getLogger("memory_review_schedule_test"),
        _dispatch_memory_review_job=lambda job: dispatched.append(job),
    )

    accepted = run_memory_review_schedule(
        agent,
        "mem_rev_stale_test",
        ScheduledMessage(
            schedule_id="mem_rev_stale_test",
            schedule_name=f"{STALE_CONVERSATION_SCHEDULE_NAME_PREFIX}test",
            payload_meta=MessageMeta(gateway_context=GatewayContext(canonical_user_id="canonical_user_1")),
            payload=Task(task_type=MessageMetaType.REQUEST, problem_statement="maintenance"),
            interval_ms=1000,
        ),
    )

    assert "Saved review job IDs:" in accepted.payload.problem_statement
    assert dispatched == []
    job = memory_store.list_review_jobs()[0]
    assert job is not None
    assert job.source_payload["trigger"] == "stale_conversation"


def test_stale_review_candidates_apply_owner_filter_before_limit(tmp_path: Path) -> None:
    conversations, _, _ = _stores(tmp_path)
    _seed_turns(conversations, count=2)
    assert not conversations.list_conversation_memory_review_candidates(team_id="team_test", canonical_user_id="other_user", limit=1)
    own = conversations.list_conversation_memory_review_candidates(team_id="team_test", canonical_user_id="canonical_user_1", limit=1)
    assert [row["conversation_id"] for row in own] == ["conversation_review"]
