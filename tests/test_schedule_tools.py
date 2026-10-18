"""Schedule tools and the scheduler loop.

The tools work on the schedules of the person the task works for, taken from
the task's gateway-signed context. Changes to schedules the runtime manages
are covered with real Cedar in tests/test_system_schedule_changes.py.
"""

from __future__ import annotations

import asyncio
import json
import logging
import sqlite3
import pytest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock


from custom_agents.scheduled_runtime import (
    enqueue_fired_scheduled_task,
    enqueue_isolated_recovery_checkpoint,
    scheduler_loop,
)
from custom_agents.conversation_store import ConversationStore
from custom_agents.schedule_store import AdvanceOutcome, ScheduleStore
from custom_agents.task_manager import TaskManager
from custom_agents.task_records import ReportedWorkIdentity
from custom_messages.contracts.routing_v1 import (
    GatewayContext,
    MessageHeader,
    MessageMeta,
)
from custom_messages.contracts.schedule_v1 import ScheduledMessage, ScheduleTaskMemory
from custom_messages.contracts.task_v1 import AfterActionReport, Task
from custom_messages.enums import (
    AfterActionReportReviewStatus,
    MessageMetaType,
    PlanActionType,
    ProgressState,
    RequesterType,
    ScheduleManagementClass,
    SystemScheduleKind,
    TaskHistoryImportance,
)
from storage.config import resolve_storage_settings
from custom_tools.providers.communication import send_message
from custom_tools.providers.schedules import (
    create_scheduled_job,
    disable_scheduled_job,
    get_schedule_telemetry,
    list_scheduled_jobs,
    pause_scheduled_job,
    remove_scheduled_job,
    record_scheduled_run,
    resume_scheduled_job,
    update_scheduled_job,
)
from custom_tools.runtime_context import set_agent_ref
from tests.pending_work_support import call_tool_on_agent_loop
from tests.broker_access_support import context_signer
from tests.change_approval_support import use_signed_contexts


@pytest.fixture(autouse=True)
def signed_contexts(monkeypatch: pytest.MonkeyPatch) -> None:
    """Trust the test gateway's signature on task contexts."""

    use_signed_contexts(monkeypatch)


def signed_gateway_context(canonical_user_id: str, **fields) -> GatewayContext:
    """A gateway context signed for the person, as the gateway sends it."""

    return GatewayContext(
        canonical_user_id=canonical_user_id,
        task_context=context_signer().issue(
            team_id="team_1", person_id=canonical_user_id, person_roles=("owner",)
        ),
        **fields,
    )


def test_schedule_inspection_pages_metadata_and_exact_large_detail(tmp_path):
    agent = _FakeAgent(str(tmp_path / "schedule.db"))
    set_agent_ref(agent)
    ids = []
    for index in range(23):
        result = json.loads(
            create_scheduled_job(
                instruction=f"Task {index}: " + "large work description " * 5000,
                schedule_name=f"schedule {index}",
                interval_seconds=60,
            )
        )
        ids.append(result["schedule_id"])
    first_raw = list_scheduled_jobs()
    first = json.loads(first_raw)
    assert first["returned"] == 20
    assert first["next_offset"] == 20
    assert len(first_raw) < 25000
    second = json.loads(list_scheduled_jobs(offset=first["next_offset"]))
    assert second["returned"] == 3
    assert second["next_offset"] is None
    assert {
        item["schedule_id"] for item in first["schedules"] + second["schedules"]
    } == set(ids)
    detail = json.loads(
        list_scheduled_jobs(schedule_id=ids[0], details_max_chars=999999)
    )["schedules"][0]["detail"]
    assert len(detail["text"]) == 4000
    assert detail["next_offset"] == 4000
    next_detail = json.loads(
        list_scheduled_jobs(schedule_id=ids[0], details_offset=4000)
    )["schedules"][0]["detail"]
    assert next_detail["sha256"] == detail["sha256"]
    counts = json.loads(get_schedule_telemetry())
    assert counts["schedules"] is None
    assert counts["summary"]["total"] == 23
    assert len(json.dumps(counts)) < 1500
    targeted = json.loads(
        get_schedule_telemetry(include_schedules=True, schedule_id=ids[0])
    )
    assert targeted["summary"]["total"] == 1
    assert targeted["schedules"][0]["schedule_id"] == ids[0]
    assert "schedule_memory" not in targeted["schedules"][0]
    other_agent = _FakeAgent(
        str(tmp_path / "schedule.db"), canonical_user_id="someone_else"
    )
    set_agent_ref(other_agent)
    assert json.loads(list_scheduled_jobs(schedule_id=ids[0]))["schedules"] == []
    assert (
        json.loads(get_schedule_telemetry(schedule_id=ids[0]))["summary"]["total"] == 0
    )


@pytest.fixture(autouse=True)
def configured_tool_registry():
    # These tests exercise real policy normalization. Compose the trusted
    # registry explicitly instead of relying on another test module's imports.
    import custom_tools.registry  # noqa: F401


class _FakeTaskManager:
    def __init__(self, db_path: str, task_info):
        from custom_agents.task_operation_runtime import TaskOperationRuntime

        self.operations_runtime = TaskOperationRuntime()
        self._db_path = db_path
        self.storage_settings = resolve_storage_settings(
            {"backend": "sqlite", "sqlite": {"path": db_path}},
            base_dir=str(Path(db_path).parent),
        )
        self.current_task_id = "task_current"
        self._task_info = task_info
        self.history_entries = []

    def get_task_info(self, task_id: str):
        if task_id != "task_current":
            return None
        return self._task_info

    def append_history_entry(self, task_id: str, summary: str, **kwargs):
        self.history_entries.append((task_id, summary, kwargs))


class _FakeWake:
    def __init__(self):
        self.was_set = False

    def set(self):
        self.was_set = True


class _FakeAgent:
    def __init__(self, db_path: str, canonical_user_id: str = "usr_sched_a"):
        header = MessageHeader(
            from_agent_id="gateway_queue",
            to_routing_path="manager_id",
            response_routing_key="gateway_queue",
            agent_signature="",
        )
        meta = MessageMeta(
            from_agent_name="user",
            conversation_id="conversation_id_sched",
            requester_type=RequesterType.USER_REQUESTER,
            gateway_context=signed_gateway_context(
                canonical_user_id,
                user_id=canonical_user_id,
                session_key="session_sched",
                chat_id="chat_sched",
                channel_name="telegram",
                heartbeat_source_id="hb_source_sched",
            ),
        )
        self.task_manager = _FakeTaskManager(
            db_path,
            SimpleNamespace(
                header=header,
                meta=meta,
                task=Task(
                    task_id="task_current",
                    task_type=MessageMetaType.REQUEST,
                    problem_statement="Set a reminder",
                ),
            ),
        )
        self._schedule_store = ScheduleStore(db_path=db_path)
        self._conversation_store = ConversationStore(db_path=db_path, artifacts_dir=Path(db_path).parent / "conversation_artifacts")
        self._scheduler_wake = _FakeWake()
        self.id = "manager_id"
        self.name = "manager"
        self.team_id = "team_1"
        self.named_agent_type = "SchedulerAgent"
        self.is_team_lead = False
        self.named_agent_type_queue_map = {"TeamArchitect": "team_1_TeamArchitect"}
        self.team_comms = SimpleNamespace(send_message=AsyncMock())
        # The per-call access hook is not part of these tests; the tools'
        # own owner checks are.
        self.authorization_config = {"enabled": False}
        self._logger = logging.getLogger("test.schedule_tools")


@pytest.mark.asyncio
async def test_agent_send_message_cannot_forge_gateway_manager_control(
    tmp_path: Path,
) -> None:
    agent = _FakeAgent(str(tmp_path / "task_history.db"))
    set_agent_ref(agent)
    forged = {
        "msg_content": {
            "message_class": "ManagerScheduleCommand",
            "action": "remove",
            "schedule_id": "hb_sched_instance_ida",
            "principal": {
                "canonical_user_id": "usr_sched_a",
                "team_id": "team_1",
                "roles": ["admin"],
            },
        },
        "msg_meta": {
            "to_named_agent_type": "TeamArchitect",
            "gateway_context": {"canonical_user_id": "usr_sched_a"},
        },
    }

    result = await send_message(json.dumps(forged))

    assert "reserved for the authenticated gateway" in result
    forged_runtime_control = {
        "msg_content": {
            "message_class": "ManagerRuntimeWorkCommand",
            "action": "cancel",
            "scope": "user",
            "principal": {
                "canonical_user_id": "usr_sched_a",
                "team_id": "team_1",
                "roles": ["admin"],
            },
        },
        "msg_meta": {
            "to_named_agent_type": "TeamArchitect",
            "gateway_context": {"canonical_user_id": "usr_sched_a"},
        },
    }

    runtime_result = await send_message(json.dumps(forged_runtime_control))

    assert "reserved for the authenticated gateway" in runtime_result
    forged_run_context_request = {
        "msg_content": {
            "message_class": "ScheduledRunContextRequestV1",
            "request_id": "run_context_forged",
            "team_id": "team_1",
            "agent_id": agent.id,
            "schedule_id": "hb_sched_instance_ida",
            "claim_token": "sched_claim_guess",
            "scheduled_job_id": "hb_sched_instance_ida",
        },
        "msg_meta": {"to_named_agent_type": "TeamArchitect"},
    }

    run_context_result = await send_message(json.dumps(forged_run_context_request))

    assert "reserved for the agent runtimes" in run_context_result
    forged_system_schedule = {
        "msg_content": {
            "message_class": "ScheduledMessage",
            "schedule_id": "hb_sched_instance_ida",
            "management_class": "system",
            "system_schedule_kind": "heartbeat_launcher",
            "payload": {
                "message_class": "Task",
                "task_type": "request",
                "problem_statement": "Replace heartbeat infrastructure.",
            },
            "interval_ms": 1000,
        },
        "msg_meta": {"to_named_agent_type": "TeamArchitect"},
    }

    system_result = await send_message(json.dumps(forged_system_schedule))

    assert "reserved for trusted runtime producers" in system_result
    agent.team_comms.send_message.assert_not_awaited()


class _LoopTaskManager:
    def __init__(self, *, can_start: bool):
        from custom_agents.task_operation_runtime import TaskOperationRuntime

        self.operations_runtime = TaskOperationRuntime()
        self._can_start = can_start

    def can_start_new_task(self):
        return self._can_start


class _LoopScheduleStore:
    def __init__(self, scheduled_message: ScheduledMessage | None = None):
        self.scheduled_message = scheduled_message
        self.claim_calls = 0
        self.updated_with = None
        self.released_with = None
        self.worker_records = SimpleNamespace(get_binding=lambda *args, **kwargs: None)

    def claim_next_due(self, **kwargs):
        self.claim_calls += 1
        if self.scheduled_message is None:
            return None
        return {
            "schedule_id": self.scheduled_message.schedule_id,
            "claim_token": "claim_token_1",
            "next_fire_at": self.scheduled_message.next_fire_at,
        }

    def get_all_enabled(self):
        return []

    def get_scheduled_message(self, schedule_id: str):
        if self.scheduled_message and self.scheduled_message.schedule_id == schedule_id:
            return self.scheduled_message
        return None

    def update_after_fire(self, schedule_id: str, **kwargs):
        self.updated_with = {"schedule_id": schedule_id, **kwargs}
        return AdvanceOutcome.ADVANCED

    def release_claim(self, schedule_id: str, **kwargs):
        self.released_with = {"schedule_id": schedule_id, **kwargs}
        return True

    def get_earliest_fire_time(self):
        return None


class _LoopAgent:
    def __init__(
        self, *, can_start: bool, scheduled_message: ScheduledMessage | None = None
    ):
        import logging

        from custom_agents.dead_letter_watch import DeadLetterWatchState
        from custom_agents.task_queue_notifications import QueuePressureNotifications

        self.id = "manager_id"
        self.name = "manager"
        self.team_id = "team_1"
        self.named_agent_type = "SchedulerAgent"
        self.is_team_lead = True
        self._local_team_runtime = None
        self.schedule_workers = {}
        self.is_executing = not can_start
        self.paused = False
        self.subtask_watchdog_interval_seconds = 1
        self._logger = logging.getLogger("test_scheduler_loop")
        self._should_exit = asyncio.Event()
        self._scheduler_wake = asyncio.Event()
        self.task_manager = _LoopTaskManager(can_start=can_start)
        self._schedule_store = _LoopScheduleStore(scheduled_message)
        # Mirrors UniversalAgent's private state; scheduler_loop runs the
        # lead's queue-pressure check on every pass before claiming work.
        self._queue_pressure_notifications = QueuePressureNotifications()
        # ...and the lead's set-aside message report, here with no transport connected.
        self._dead_letter_watch = DeadLetterWatchState()
        self.enqueued = []

    def _ensure_nats_transport_adapter(self):
        return SimpleNamespace(connected=False)

    async def _refresh_self_agent_state(self):
        return None

    async def _recover_stalled_subtasks(self):
        return None

    async def _enqueue_fired_scheduled_task(self, schedule_id, scheduled_message, **_fire_details):
        self.enqueued.append((schedule_id, scheduled_message.schedule_id))
        self._should_exit.set()
        return True


def test_schedule_tools_create_pause_resume_disable_and_remove(tmp_path: Path):
    db_path = str(tmp_path / "task_history.db")
    agent = _FakeAgent(db_path)
    set_agent_ref(agent)

    created = json.loads(
        create_scheduled_job(
            instruction="Reply with the exact phrase scheduled tool ok.",
            schedule_name="tool schedule",
            interval_seconds=60,
        )
    )
    schedule_id = created["schedule_id"]
    assert created["schedule_name"] == "tool schedule"
    assert agent._scheduler_wake.was_set is True

    listing = json.loads(list_scheduled_jobs())
    assert listing["returned"] == 1
    assert listing["schedules"][0]["schedule_id"] == schedule_id
    assert listing["schedules"][0]["schedule_name"] == "tool schedule"
    assert listing["schedules"][0]["notify_policy"] == "on_change"
    assert "schedule_memory" not in listing["schedules"][0]
    detail = json.loads(list_scheduled_jobs(schedule_id=schedule_id))["schedules"][0]["detail"]
    assert "session_sched" in detail["text"]
    stored = agent._schedule_store.get_scheduled_message(schedule_id)
    assert stored is not None
    assert stored.schedule_memory is not None
    assert stored.schedule_memory.policy.notify == "on_change"
    assert stored.schedule_memory.ledger.total_runs == 0
    assert stored.claim_team_id == "team_1"
    assert stored.claim_named_agent_type == "SchedulerAgent"
    assert stored.payload_meta is not None
    assert stored.payload_meta.conversation_id != "conversation_id_sched"
    assert stored.payload_meta.followup_to_task_id.startswith("sched_conv_seed_")
    seed_turn = agent._conversation_store.get_turn_by_task_id(
        stored.payload_meta.followup_to_task_id
    )
    assert seed_turn is not None
    assert seed_turn["conversation_id"] == stored.payload_meta.conversation_id
    assert "Set a reminder" in seed_turn["problem_text"]
    assert agent.task_manager.history_entries[-1][0] == "task_current"
    assert "Created dedicated conversation" in agent.task_manager.history_entries[-1][1]

    paused = json.loads(call_tool_on_agent_loop(agent, pause_scheduled_job, schedule_id, duration_minutes=30))
    assert paused["schedule_id"] == schedule_id
    assert paused["paused"] is True

    resumed = json.loads(call_tool_on_agent_loop(agent, resume_scheduled_job, schedule_id))
    assert resumed["resumed"] is True

    disabled = json.loads(call_tool_on_agent_loop(agent, disable_scheduled_job, schedule_id))
    assert disabled["disabled"] is True

    removed = json.loads(call_tool_on_agent_loop(agent, remove_scheduled_job, schedule_id))
    assert removed["removed"] is True


def test_schedule_tools_hide_jobs_owned_by_another_canonical_user(tmp_path: Path):
    db_path = str(tmp_path / "task_history.db")
    owner_agent = _FakeAgent(db_path, canonical_user_id="usr_sched_a")
    set_agent_ref(owner_agent)

    created = json.loads(
        create_scheduled_job(
            instruction="Private schedule.",
            schedule_name="private schedule",
            interval_seconds=60,
        )
    )
    schedule_id = created["schedule_id"]

    other_agent = _FakeAgent(db_path, canonical_user_id="usr_sched_b")
    set_agent_ref(other_agent)

    listed = json.loads(list_scheduled_jobs())
    disabled = json.loads(disable_scheduled_job(schedule_id))

    assert listed == {"total": 0, "returned": 0, "offset": 0, "next_offset": None, "schedules": []}
    assert disabled == {
        "schedule_id": schedule_id,
        "disabled": False,
        "error": "schedule not found",
    }
    assert (
        owner_agent._schedule_store.get_scheduled_message(schedule_id).enabled is True
    )


def test_update_scheduled_job_mutates_fields_and_reports_to_manager(tmp_path: Path):
    db_path = str(tmp_path / "task_history.db")
    agent = _FakeAgent(db_path)
    set_agent_ref(agent)
    created = json.loads(
        create_scheduled_job(
            instruction="Original scheduled instruction.",
            schedule_name="original schedule",
            interval_seconds=60,
        )
    )
    schedule_id = created["schedule_id"]
    expires_at = (datetime.now(timezone.utc) + timedelta(days=1)).isoformat()

    updated = json.loads(
        call_tool_on_agent_loop(
            agent,
            update_scheduled_job,
            schedule_id,
            schedule_name="updated schedule",
            instruction="Updated scheduled instruction.",
            interval_seconds=120,
            repeat_count=3,
            expires_at_utc=expires_at,
            enabled="false",
        )
    )

    assert updated["updated"] is True
    assert updated["changes"]["interval_seconds"] == 120
    assert updated["changes"]["repeat_count"] == 3
    assert updated["changes"]["enabled"] is False
    assert updated["changes"]["expires_at_utc"] == expires_at
    assert updated["manager_report"]["reported"] is True
    stored = agent._schedule_store.get_scheduled_message(schedule_id)
    assert stored is not None
    assert stored.schedule_name == "updated schedule"
    assert stored.payload.problem_statement == "Updated scheduled instruction."
    assert stored.interval_ms == 120000
    assert stored.cron_expression is None
    assert stored.repeat_count == 3
    assert stored.enabled is False
    assert stored.expires_at_utc == expires_at
    assert stored.schedule_memory is not None
    assert stored.schedule_memory.policy.notify == "on_change"

    agent.team_comms.send_message.assert_awaited()
    envelope = agent.team_comms.send_message.await_args.kwargs["message"]
    assert envelope.msg_meta.manager_escalation is not None
    assert envelope.msg_meta.manager_escalation.escalation_type == "manager_review"
    assert envelope.msg_meta.manager_escalation.source_schedule_id == schedule_id
    assert (
        agent.team_comms.send_message.await_args.kwargs["routing_key"]
        == "team_1_TeamArchitect"
    )


def test_update_scheduled_job_mutates_schedule_memory_policy(tmp_path: Path):
    db_path = str(tmp_path / "task_history.db")
    agent = _FakeAgent(db_path)
    set_agent_ref(agent)
    created = json.loads(
        create_scheduled_job(
            instruction="Watch source for updates.",
            schedule_name="watch source",
            interval_seconds=60,
            schedule_kind="source_monitor",
            notify="always",
            evidence_required="fresh_source",
        )
    )
    schedule_id = created["schedule_id"]

    updated = json.loads(
        call_tool_on_agent_loop(
            agent,
            update_scheduled_job,
            schedule_id,
            notify="never",
            evidence_required="authoritative_source",
            notify_on_failure="true",
        )
    )

    assert updated["updated"] is True
    assert updated["changes"]["schedule_memory"]["policy.notify"] == "never"
    stored = agent._schedule_store.get_scheduled_message(schedule_id)
    assert stored is not None
    assert stored.schedule_memory is not None
    assert stored.schedule_memory.schedule_kind == "source_monitor"
    assert stored.schedule_memory.policy.notify == "never"
    assert stored.schedule_memory.policy.evidence_required == "authoritative_source"
    assert stored.schedule_memory.policy.notify_on_failure is True


def test_record_scheduled_run_updates_bounded_memory_and_notification_decision(
    tmp_path: Path,
):
    db_path = str(tmp_path / "task_history.db")
    agent = _FakeAgent(db_path)
    set_agent_ref(agent)
    created = json.loads(
        create_scheduled_job(
            instruction="Check match state.",
            schedule_name="match monitor",
            interval_seconds=60,
            schedule_kind="live_sports_monitor",
            notify="on_change",
        )
    )
    schedule_id = created["schedule_id"]
    task_info = agent.task_manager._task_info
    task_info.meta.requester_type = RequesterType.TIMER_REQUESTER
    task_info.meta.gateway_context.scheduled_job_id = schedule_id

    no_change = json.loads(
        record_scheduled_run(
            outcome="no_change",
            state_summary="Score unchanged at 2-0.",
            domain_state_json='{"score":"2-0","period":"1H"}',
        )
    )
    assert no_change["recorded"] is True
    assert no_change["should_notify"] is False

    changed = json.loads(
        record_scheduled_run(
            outcome="changed",
            state_summary="Netherlands extended lead to 3-0.",
            domain_state_json='{"score":"3-0","period":"2H","last_event_id":"goal:gakpo:47"}',
            evidence_json='{"source":"official","observed_clock":"47"}',
        )
    )
    assert changed["should_notify"] is True
    stored = agent._schedule_store.get_scheduled_message(schedule_id)
    assert stored is not None
    assert stored.schedule_memory is not None
    memory = stored.schedule_memory
    assert memory.ledger.total_runs == 2
    assert memory.ledger.consecutive_failures == 0
    assert len(memory.ledger.recent_runs) == 2
    assert memory.state.summary == "Netherlands extended lead to 3-0."
    assert memory.state.domain["score"] == "3-0"
    assert memory.state.domain["last_event_id"] == "goal:gakpo:47"


@pytest.mark.parametrize("concurrent", ["remove", "disable"])
def test_record_scheduled_run_never_undoes_a_change_made_after_its_read(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, concurrent: str
):
    """The person deletes or turns off the schedule while a run records its notes (2026-10-17).

    The tool used to save the whole schedule it had read, which recreated a
    deleted schedule and turned a turned-off one back on.
    """
    import custom_tools.providers.schedules as schedule_tools

    agent = _FakeAgent(str(tmp_path / "task_history.db"))
    set_agent_ref(agent)
    schedule_id = json.loads(
        create_scheduled_job(instruction="Check match state.", schedule_name="match monitor", interval_seconds=60)
    )["schedule_id"]
    task_info = agent.task_manager._task_info
    task_info.meta.requester_type = RequesterType.TIMER_REQUESTER
    task_info.meta.gateway_context.scheduled_job_id = schedule_id
    store = agent._schedule_store
    read_schedule = schedule_tools._owned_or_denial

    def read_then_person_acts(*args, **kwargs):
        found = read_schedule(*args, **kwargs)
        if concurrent == "remove":
            assert store.remove(schedule_id)
        else:
            assert store.set_enabled(schedule_id, False)
        return found

    monkeypatch.setattr(schedule_tools, "_owned_or_denial", read_then_person_acts)
    result = json.loads(record_scheduled_run(outcome="no_change", state_summary="Score unchanged."))

    if concurrent == "remove":
        assert result == {"schedule_id": schedule_id, "recorded": False, "error": schedule_tools.SCHEDULE_NOT_FOUND}
        assert store.get_row(schedule_id) is None
    else:
        assert result["recorded"] is True
        saved = store.get_scheduled_message(schedule_id)
        assert saved.enabled is False
        assert saved.schedule_memory.state.summary == "Score unchanged."


def test_scheduler_loop_updates_schedule_with_claim_token_after_acceptance():
    scheduled_message = ScheduledMessage(
        schedule_id="sched_loop",
        next_fire_at=(datetime.now(timezone.utc) - timedelta(minutes=1)).isoformat(),
        payload=Task(
            task_id="task_loop",
            task_type=MessageMetaType.REQUEST,
            problem_statement="run",
        ),
        interval_ms=1000,
    )
    agent = _LoopAgent(can_start=True, scheduled_message=scheduled_message)

    asyncio.run(scheduler_loop(agent))

    assert agent._schedule_store.claim_calls == 1
    assert agent.enqueued == [("sched_loop", "sched_loop")]
    assert agent._schedule_store.updated_with == {
        "schedule_id": "sched_loop",
        "claim_token": "claim_token_1",
        "claimer_agent_id": "manager_id",
        "due_at": scheduled_message.next_fire_at,
    }


def test_schedule_telemetry_reports_counts_timing_and_scheduler_identity(
    tmp_path: Path,
):
    db_path = str(tmp_path / "task_history.db")
    agent = _FakeAgent(db_path)
    set_agent_ref(agent)

    created = json.loads(
        create_scheduled_job(
            instruction="Check the project status and summarize the next action.",
            schedule_name="status poll",
            interval_seconds=120,
        )
    )
    schedule_id = created["schedule_id"]

    telemetry = json.loads(get_schedule_telemetry(include_schedules=True))

    assert telemetry["agent"]["agent_id"] == "manager_id"
    assert telemetry["summary"]["total"] == 1
    assert telemetry["summary"]["enabled"] == 1
    assert telemetry["summary"]["with_interval"] == 1
    assert telemetry["summary"]["recurring"] == 1
    schedule = telemetry["schedules"][0]
    assert schedule["schedule_id"] == schedule_id
    assert schedule["schedule_name"] == "status poll"
    assert schedule["interval_ms"] == 120000
    assert schedule["interval_seconds"] == 120
    assert schedule["scheduled_by"]["from_agent_id"] == "gateway_queue"
    assert schedule["scheduled_by"]["from_agent_name"] == "user"
    assert schedule["scheduled_by"]["followup_to_task_id"].startswith(
        "sched_conv_seed_"
    )
    assert schedule["target"]["to_routing_path"] == "manager_id"
    assert "gateway_context" not in schedule
    assert "schedule_memory" not in schedule
    assert "project status" in schedule["problem_preview"]


def test_recovery_checkpoint_schedule_sends_task_query(tmp_path: Path):
    task_manager = TaskManager(db_path=str(tmp_path / "task_history.db"))
    task_manager.set_owner_identity(
        agent_id="manager_id",
        agent_name="manager",
        team_id="team_1",
        named_agent_type="TeamArchitect",
    )
    original = Task(
        task_id="task_original",
        task_type=MessageMetaType.REQUEST,
        problem_statement="Original task waiting on recovery.",
    )
    assert task_manager.add_task(
        original,
        MessageHeader(
            from_agent_id="gateway_queue",
            response_routing_key="gateway_queue",
            agent_signature="",
        ),
        MessageMeta(
            from_agent_name="user",
            conversation_id="conversation_recovery",
            requester_type=RequesterType.USER_REQUESTER,
        ),
        owner_person_id=None,
    )
    recovery = Task(
        task_id="recovery_task_1",
        task_type=MessageMetaType.REQUEST,
        problem_statement="Recovery still running.",
    )
    task_manager.set_task_plan(original.task_id, "Recover the original task")
    task_manager.plan_state.task_to_plan_id[recovery.task_id] = (
        task_manager.get_task_plan_id(original.task_id)
    )
    assert task_manager.add_task(
        recovery,
        MessageHeader(
            from_agent_id="manager_id",
            response_routing_key="gateway_queue",
            agent_signature="",
        ),
        MessageMeta(
            from_agent_name="manager", conversation_id="conversation_recovery_isolated"
        ),
        owner_person_id=None,
    )
    task_manager.update_main_task_state(
        recovery.task_id,
        ProgressState.IN_PROGRESS,
        "2026-04-29T12:00:00+00:00",
    )

    agent = SimpleNamespace(
        id="manager_id",
        name="manager",
        task_manager=task_manager,
        team_comms=SimpleNamespace(send_message=AsyncMock()),
        _logger=SimpleNamespace(
            info=lambda *args, **kwargs: None, warning=lambda *args, **kwargs: None
        ),
    )
    scheduled = ScheduledMessage(
        schedule_id="recovery_checkpoint_1",
        schedule_name="isolated_recovery_checkpoint:task_original:recovery_task_1",
        payload=Task(task_type=MessageMetaType.REQUEST, problem_statement="checkpoint"),
    )

    accepted = asyncio.run(
        enqueue_isolated_recovery_checkpoint(agent, scheduled.schedule_id, scheduled)
    )

    assert accepted is True
    queries = task_manager.get_task_queries(original.task_id)
    assert len(queries) == 1
    assert queries[0].query_type == MessageMetaType.REQUEST
    assert "continue investigating" in queries[0].message
    agent.team_comms.send_message.assert_awaited_once()
    envelope = agent.team_comms.send_message.await_args.kwargs["message"]
    assert envelope.msg_content.task_query_id == queries[0].task_query_id
    assert (
        agent.team_comms.send_message.await_args.kwargs["routing_key"]
        == "gateway_queue"
    )
    plan_actions = task_manager.get_plan_actions(original.task_id)
    assert any(action.action_type == PlanActionType.ASK_USER for action in plan_actions)


def test_aar_batch_review_schedule_queues_hydrated_manager_task(tmp_path: Path):
    task_manager = TaskManager(db_path=str(tmp_path / "task_history.db"))
    task_manager.set_owner_identity(
        agent_id="manager_id",
        agent_name="manager",
        team_id="team_1",
        named_agent_type="TeamArchitect",
    )
    parent = Task(
        task_id="task_parent_aar_schedule",
        task_type=MessageMetaType.REQUEST,
        problem_statement="Coordinate work.",
    )
    task_manager.add_task(
        parent,
        MessageHeader(
            from_agent_id="user", response_routing_key="user_queue", agent_signature=""
        ),
        MessageMeta(from_agent_name="user"),
        owner_person_id=None,
    )
    task_manager.update_main_task_state(
        parent.task_id, ProgressState.IN_PROGRESS, "2026-03-18T00:00:00+00:00"
    )
    task_manager.complete_task(parent.task_id, "2026-03-18T00:01:00+00:00")

    recorded = task_manager.record_after_action_report(
        AfterActionReport(
            report_id="aar_schedule_reviewed",
            source_task_id="task_child_aar_schedule",
            parent_task_id=parent.task_id,
            source_agent_id="specialist_id",
            source_agent_name="specialist",
            summary="Late scheduled review lesson.",
            recommended_importance=TaskHistoryImportance.HIGH,
        ),
        identity=ReportedWorkIdentity(person_id=None),
        agent_name="specialist",
        agent_id="specialist_id",
        timestamp="2026-03-18T00:01:05+00:00",
    )
    assert recorded is not None
    assert recorded.batch is not None
    review_task = task_manager.build_after_action_report_batch_review_task(
        recorded.batch.batch_id,
        task_id="task_internal_aar_review",
    )
    assert review_task is not None

    schedule = ScheduledMessage(
        schedule_id="sched_aar_batch_test",
        schedule_name=f"after_action_report_batch_review:{recorded.batch.batch_id}",
        payload=review_task,
        repeat_count=1,
        next_fire_at=recorded.batch.scheduled_review_at,
    )
    agent = SimpleNamespace(
        id="manager_id",
        name="manager",
        team_id="team_1",
        task_manager=task_manager,
        paused=False,
        is_executing=True,
        # The run's wake-up is recorded; with is_executing=True the drain finds the slot busy, so the run stays queued.
        _should_exit=asyncio.Event(),
        _pending_work_task=None,
        _continue_pending_work=AsyncMock(),
        _conversation_store=None,
        _ensure_request_timezone=lambda meta, top_level_request: meta,
        _logger=SimpleNamespace(
            info=lambda *args, **kwargs: None,
            warning=lambda *args, **kwargs: None,
            error=lambda *args, **kwargs: None,
        ),
    )

    assert (
        asyncio.run(enqueue_fired_scheduled_task(agent, schedule.schedule_id, schedule))
        is True
    )

    queued = task_manager.get_task_info("task_internal_aar_review")
    assert queued is not None
    assert "report_id: aar_schedule_reviewed" in queued.task.problem_statement
    assert (
        "Use resolve_after_action_report_review with the report_id shown above"
        in queued.task.problem_statement
    )
    archived = task_manager.get_task_from_db(parent.task_id)
    assert not [
        entry
        for entry in archived["main_task"].execution_history
        if (entry.details or {}).get("batch_id") == recorded.batch.batch_id
    ]


def test_aar_batch_review_schedule_skips_when_no_pending_reports(tmp_path: Path):
    task_manager = TaskManager(db_path=str(tmp_path / "task_history.db"))
    task_manager.set_owner_identity(
        agent_id="manager_id",
        agent_name="manager",
        team_id="team_1",
        named_agent_type="TeamArchitect",
    )
    parent = Task(
        task_id="task_parent_aar_skip",
        task_type=MessageMetaType.REQUEST,
        problem_statement="Coordinate work.",
    )
    task_manager.add_task(
        parent,
        MessageHeader(
            from_agent_id="user", response_routing_key="user_queue", agent_signature=""
        ),
        MessageMeta(from_agent_name="user"),
        owner_person_id=None,
    )
    task_manager.update_main_task_state(
        parent.task_id, ProgressState.IN_PROGRESS, "2026-03-18T00:00:00+00:00"
    )
    task_manager.complete_task(parent.task_id, "2026-03-18T00:01:00+00:00")

    recorded = task_manager.record_after_action_report(
        AfterActionReport(
            report_id="aar_schedule_skip",
            source_task_id="task_child_aar_skip",
            parent_task_id=parent.task_id,
            source_agent_id="specialist_id",
            source_agent_name="specialist",
            summary="This report is resolved before the scheduled review fires.",
        ),
        identity=ReportedWorkIdentity(person_id=None),
        agent_name="specialist",
        agent_id="specialist_id",
        timestamp="2026-03-18T00:01:05+00:00",
    )
    assert recorded is not None
    assert recorded.batch is not None
    assert recorded.review is not None
    resolved = task_manager.resolve_after_action_report_review(
        recorded.review.review_id,
        person_id=None,
        status=AfterActionReportReviewStatus.IGNORED,
        decision_summary="Resolved before the scheduled review fired.",
        agent_name="manager",
        agent_id="manager_id",
    )
    assert resolved is not None

    review_task = task_manager.build_after_action_report_batch_review_task(
        recorded.batch.batch_id,
        task_id="task_internal_aar_skip",
    )
    schedule = ScheduledMessage(
        schedule_id="sched_aar_batch_skip",
        schedule_name=f"after_action_report_batch_review:{recorded.batch.batch_id}",
        payload=review_task,
        repeat_count=1,
        next_fire_at=recorded.batch.scheduled_review_at,
    )
    agent = SimpleNamespace(
        id="manager_id",
        name="manager",
        team_id="team_1",
        task_manager=task_manager,
        paused=False,
        is_executing=False,
        _conversation_store=None,
        _ensure_request_timezone=lambda meta, top_level_request: meta,
        _logger=SimpleNamespace(
            info=lambda *args, **kwargs: None,
            warning=lambda *args, **kwargs: None,
            error=lambda *args, **kwargs: None,
        ),
    )

    assert (
        asyncio.run(enqueue_fired_scheduled_task(agent, schedule.schedule_id, schedule))
        is True
    )
    assert task_manager.get_task_info("task_internal_aar_skip") is None


def test_fired_scheduled_task_carries_objective_link(tmp_path: Path):
    task_manager = TaskManager(db_path=str(tmp_path / "task_history.db"))
    task_manager.set_owner_identity(
        agent_id="manager_id",
        agent_name="manager",
        team_id="team_1",
        named_agent_type="TeamArchitect",
    )
    root = Task(
        task_id="task_root_objective_schedule",
        task_type=MessageMetaType.REQUEST,
        problem_statement="Create objective-linked schedule.",
    )
    assert task_manager.add_task(
        root,
        MessageHeader(from_agent_id="user", agent_signature=""),
        MessageMeta(
            from_agent_name="user", conversation_id="conversation_objective_schedule"
        ),
        owner_person_id=None,
    )
    objective = task_manager.create_objective(
        task_id=root.task_id,
        title="Scheduled objective",
        desired_outcome="Scheduled work remains attached to this objective.",
        why="Recurring task launches must not lose the durable root.",
    )
    assert objective is not None

    schedule = ScheduledMessage(
        schedule_id="sched_objective_fire",
        schedule_name="objective fire",
        objective_id=objective.objective_id,
        payload=Task(
            task_id="task_scheduled_objective",
            parent_task_id=root.task_id,
            task_type=MessageMetaType.REQUEST,
            problem_statement="Run scheduled objective check.",
        ),
        payload_header=MessageHeader(from_agent_id="manager_id", agent_signature=""),
        payload_meta=MessageMeta(
            from_agent_name="manager",
            conversation_id="conversation_objective_schedule",
            objective_id=objective.objective_id,
        ),
        repeat_count=1,
        next_fire_at="2026-03-18T00:00:00+00:00",
    )
    agent = SimpleNamespace(
        id="manager_id",
        name="manager",
        team_id="team_1",
        task_manager=task_manager,
        paused=False,
        is_executing=True,
        # The run's wake-up is recorded; with is_executing=True the drain finds the slot busy, so the run stays queued.
        _should_exit=asyncio.Event(),
        _pending_work_task=None,
        _continue_pending_work=AsyncMock(),
        _conversation_store=None,
        _ensure_request_timezone=lambda meta, top_level_request: meta,
        _logger=SimpleNamespace(
            info=lambda *args, **kwargs: None,
            warning=lambda *args, **kwargs: None,
            error=lambda *args, **kwargs: None,
        ),
    )

    assert (
        asyncio.run(
            enqueue_fired_scheduled_task(
                agent,
                schedule.schedule_id,
                schedule,
                from_runtime_mailbox=True,
            )
        )
        is True
    )
    queued = task_manager.get_task_info("task_scheduled_objective")
    assert queued is not None
    assert (
        task_manager.get_task_objective_id(queued.task.task_id)
        == objective.objective_id
    )
    assert queued.task.execution_history[-1].objective_id == objective.objective_id


def test_gateway_cron_schedule_fire_keeps_stored_conversation_id(tmp_path: Path):
    task_manager = TaskManager(db_path=str(tmp_path / "task_history.db"))
    task_manager.set_owner_identity(
        agent_id="manager_id",
        agent_name="manager",
        team_id="team_1",
        named_agent_type="TeamArchitect",
    )
    conversation_store = ConversationStore(db_path=str(tmp_path / "conversation.db"), artifacts_dir=tmp_path / "conversation_artifacts")
    conversation_id = "conversation_gateway_cron"
    schedule = ScheduledMessage(
        schedule_id="sched_gateway_cron_context",
        schedule_name="gateway cron context",
        payload=Task(
            task_type=MessageMetaType.REQUEST,
            problem_statement="Run gateway-created cron check.",
        ),
        payload_header=MessageHeader(
            from_agent_id="gateway_queue",
            to_routing_path="manager_id",
            response_routing_key="gateway_queue",
            agent_signature="",
        ),
        payload_meta=MessageMeta(
            from_agent_name="gateway",
            conversation_id=conversation_id,
            msg_correlation_id="corr_gateway_cron",
            requester_type=RequesterType.TIMER_REQUESTER,
            gateway_context=GatewayContext(
                session_key="session_gateway_cron",
                delivery_mode="cron",
                scheduled_job_id="sched_gateway_cron_context",
            ),
        ),
        cron_expression="*/5 * * * *",
        next_fire_at="2026-03-18T00:00:00+00:00",
    )
    agent = SimpleNamespace(
        id="manager_id",
        name="manager",
        team_id="team_1",
        task_manager=task_manager,
        paused=False,
        is_executing=True,
        # The run's wake-up is recorded; with is_executing=True the drain finds the slot busy, so the run stays queued.
        _should_exit=asyncio.Event(),
        _pending_work_task=None,
        _continue_pending_work=AsyncMock(),
        _conversation_store=conversation_store,
        _ensure_request_timezone=lambda meta, top_level_request: meta,
        _logger=SimpleNamespace(
            info=lambda *args, **kwargs: None,
            warning=lambda *args, **kwargs: None,
            error=lambda *args, **kwargs: None,
        ),
    )

    assert (
        asyncio.run(
            enqueue_fired_scheduled_task(
                agent,
                schedule.schedule_id,
                schedule,
                from_runtime_mailbox=True,
            )
        )
        is True
    )

    queued_infos = [
        task_info
        for task_info in task_manager.task_state.tasks.values()
        if task_info.task.problem_statement.startswith(
            "Run gateway-created cron check."
        )
    ]
    assert len(queued_infos) == 1
    queued = queued_infos[0]
    assert "suppress_outbound_dispatch=true" in queued.task.problem_statement
    assert queued.meta.conversation_id == conversation_id
    assert queued.meta.msg_correlation_id == "corr_gateway_cron"
    turn = conversation_store.get_turn_by_task_id(queued.task.task_id)
    assert turn is not None
    assert turn["conversation_id"] == conversation_id
    assert turn["delivery_state"] == "pending_gateway"


def test_legacy_schedule_row_owner_reaches_fired_task_runtime_context(tmp_path: Path):
    db_path = str(tmp_path / "task_history.db")
    task_manager = TaskManager(db_path=db_path)
    task_manager.set_owner_identity(
        agent_id="manager_id",
        agent_name="manager",
        team_id="team_1",
        named_agent_type="TeamArchitect",
    )
    schedule_store = ScheduleStore(db_path=db_path)
    schedule = ScheduledMessage(
        schedule_id="sched_legacy_owner_context",
        payload=Task(
            task_id="task_legacy_owner_context",
            task_type=MessageMetaType.REQUEST,
            problem_statement="Run a credential-backed scheduled check.",
        ),
        payload_header=MessageHeader(
            from_agent_id="gateway_queue",
            to_routing_path="manager_id",
            response_routing_key="gateway_queue",
            agent_signature="",
        ),
        payload_meta=MessageMeta(
            from_agent_name="gateway",
            conversation_id="conversation_legacy_owner_context",
            gateway_context=GatewayContext(
                session_key="session_legacy_owner_context",
                delivery_mode="cron",
            ),
        ),
        repeat_count=1,
        next_fire_at="2026-07-13T00:00:00+00:00",
    )
    schedule_store.add(schedule)
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            "UPDATE scheduled_messages SET owner_canonical_user_id = ? WHERE schedule_id = ?",
            ("usr_admin", schedule.schedule_id),
        )
        conn.commit()

    restored = schedule_store.get_scheduled_message(schedule.schedule_id)
    assert restored is not None
    agent = SimpleNamespace(
        id="manager_id",
        name="manager",
        team_id="team_1",
        named_agent_type="TeamArchitect",
        task_manager=task_manager,
        paused=False,
        is_executing=True,
        # The run's wake-up is recorded; with is_executing=True the drain finds the slot busy, so the run stays queued.
        _should_exit=asyncio.Event(),
        _pending_work_task=None,
        _continue_pending_work=AsyncMock(),
        _conversation_store=ConversationStore(db_path=db_path, artifacts_dir=Path(db_path).parent / "conversation_artifacts"),
        _ensure_request_timezone=lambda meta, top_level_request: meta,
        _logger=SimpleNamespace(
            info=lambda *args, **kwargs: None,
            warning=lambda *args, **kwargs: None,
            error=lambda *args, **kwargs: None,
        ),
    )

    accepted = asyncio.run(
        enqueue_fired_scheduled_task(
            agent,
            restored.schedule_id,
            restored,
            from_runtime_mailbox=True,
        )
    )

    assert accepted is True
    queued = task_manager.get_task_info("task_legacy_owner_context")
    assert queued is not None
    assert queued.meta.gateway_context is not None
    assert queued.meta.gateway_context.canonical_user_id == "usr_admin"


def test_recurring_fired_schedule_keeps_history_without_followup_delivery_obligation(tmp_path: Path):
    task_manager = TaskManager(db_path=str(tmp_path / "task_history.db"))
    task_manager.set_owner_identity(
        agent_id="manager_id",
        agent_name="manager",
        team_id="team_1",
        named_agent_type="TeamArchitect",
    )
    conversation_store = ConversationStore(db_path=str(tmp_path / "conversation.db"), artifacts_dir=tmp_path / "conversation_artifacts")
    conversation_id = "conversation_schedule_thread"
    conversation_store.ensure_thread(
        conversation_id=conversation_id,
        team_id="team_1",
        owner_agent_id="manager_id",
        owner_agent_name="manager",
        requester_name="user",
        requester_id="gateway",
        root_task_id="task_creator",
        subject_preview="Create recurring schedule.",
    )
    conversation_store.append_turn(
        conversation_id=conversation_id,
        task_id="task_creator",
        followup_to_task_id=None,
        owner_agent_id="manager_id",
        requester_name="user",
        requester_id="gateway",
        correlation_id="corr_creator",
        problem_text="Create recurring schedule.",
        requester_type=RequesterType.USER_REQUESTER.value,
        delivery_state="not_applicable",
        task_status=ProgressState.COMPLETED.value,
    )
    conversation_store.append_turn(
        conversation_id=conversation_id,
        task_id="task_prior_fire",
        followup_to_task_id="task_creator",
        owner_agent_id="manager_id",
        requester_name="timer",
        requester_id="gateway",
        correlation_id="corr_prior_fire",
        problem_text="Run recurring check.",
        requester_type=RequesterType.TIMER_REQUESTER.value,
        delivery_state="delivered",
        task_status=ProgressState.COMPLETED.value,
    )
    schedule = ScheduledMessage(
        schedule_id="sched_recurring_relink",
        schedule_name="recurring relink",
        payload=Task(
            task_type=MessageMetaType.REQUEST,
            problem_statement="Run recurring check.",
        ),
        payload_header=MessageHeader(from_agent_id="manager_id", agent_signature=""),
        payload_meta=MessageMeta(
            from_agent_name="manager",
            conversation_id=conversation_id,
            followup_to_task_id="task_creator",
            requester_type=RequesterType.TIMER_REQUESTER,
            gateway_context=GatewayContext(scheduled_job_id="sched_recurring_relink"),
        ),
        interval_ms=60_000,
        repeat_count=None,
        next_fire_at="2026-03-18T00:00:00+00:00",
        schedule_memory=ScheduleTaskMemory(schedule_kind="source_monitor"),
    )
    agent = SimpleNamespace(
        id="manager_id",
        name="manager",
        team_id="team_1",
        task_manager=task_manager,
        paused=False,
        is_executing=True,
        # The run's wake-up is recorded; with is_executing=True the drain finds the slot busy, so the run stays queued.
        _should_exit=asyncio.Event(),
        _pending_work_task=None,
        _continue_pending_work=AsyncMock(),
        _conversation_store=conversation_store,
        _ensure_request_timezone=lambda meta, top_level_request: meta,
        _logger=SimpleNamespace(
            info=lambda *args, **kwargs: None,
            warning=lambda *args, **kwargs: None,
            error=lambda *args, **kwargs: None,
        ),
    )

    assert (
        asyncio.run(
            enqueue_fired_scheduled_task(
                agent,
                schedule.schedule_id,
                schedule,
                from_runtime_mailbox=True,
            )
        )
        is True
    )

    queued_infos = [
        task_info
        for task_info in task_manager.task_state.tasks.values()
        if task_info.task.problem_statement.startswith("Run recurring check.")
    ]
    assert len(queued_infos) == 1
    queued = queued_infos[0]
    assert "suppress_outbound_dispatch=true" in queued.task.problem_statement
    assert "Scheduled memory contract:" in queued.task.problem_statement
    assert '"schedule_kind": "source_monitor"' in queued.task.problem_statement
    assert "record_scheduled_run" in queued.task.problem_statement
    assert queued.meta.conversation_id == conversation_id
    assert queued.meta.followup_to_task_id is None
    turn = conversation_store.get_turn_by_task_id(queued.task.task_id)
    assert turn is not None
    assert turn["followup_to_task_id"] is None
    assert conversation_store.get_turn_by_task_id("task_prior_fire") is not None


def test_notify_user_records_run_notes_only_on_a_schedule_the_task_may_record(tmp_path: Path):
    """notify_user wrote notes on whatever schedule the task's metadata named, unlike record_scheduled_run."""
    from custom_tools.providers.communication import _record_schedule_notification_if_active

    db_path = str(tmp_path / "task_history.db")
    owner = _FakeAgent(db_path)
    set_agent_ref(owner)
    schedule_id = json.loads(
        create_scheduled_job(instruction="Check match state.", schedule_name="match monitor", interval_seconds=60)
    )["schedule_id"]
    someone_else = _FakeAgent(db_path, canonical_user_id="usr_sched_b")
    someone_else.task_manager._task_info.meta.gateway_context.scheduled_job_id = schedule_id
    set_agent_ref(someone_else)

    refused = _record_schedule_notification_if_active(someone_else, message="Hello", notification_id="note_1", reason="")

    assert refused == {"recorded": False, "reason": "schedule_not_recordable", "schedule_id": schedule_id}
    assert someone_else._schedule_store.get_scheduled_message(schedule_id).schedule_memory.ledger.total_notifications == 0

    owner.task_manager._task_info.meta.gateway_context.scheduled_job_id = schedule_id
    set_agent_ref(owner)
    recorded = _record_schedule_notification_if_active(owner, message="Hello", notification_id="note_2", reason="")

    assert recorded == {"recorded": True, "schedule_id": schedule_id}
    memory = owner._schedule_store.get_scheduled_message(schedule_id).schedule_memory
    assert memory.ledger.total_notifications == 1


def test_a_persons_system_timer_records_notes_with_both_tools(tmp_path: Path):
    """A system timer signed for its owner (such as MemReviewAgent's memory review) works like the person's request."""
    from custom_tools.providers.communication import _record_schedule_notification_if_active

    agent = _FakeAgent(str(tmp_path / "task_history.db"))
    set_agent_ref(agent)
    timer = ScheduledMessage(
        schedule_id="mem_rev_user_test",
        schedule_name="memory_review:usermem:test",
        management_class=ScheduleManagementClass.SYSTEM,
        system_schedule_kind=SystemScheduleKind.MEMORY_REVIEW_USERMEM,
        payload=Task(task_type=MessageMetaType.REQUEST, problem_statement="Review memory."),
        payload_meta=MessageMeta(gateway_context=GatewayContext(canonical_user_id="usr_sched_a")),
        interval_ms=86_400_000,
        repeat_count=None,
    )
    agent._schedule_store.add(timer)
    agent.task_manager._task_info.meta.gateway_context.scheduled_job_id = timer.schedule_id

    noted = _record_schedule_notification_if_active(agent, message="Memory reviewed.", notification_id="note_timer", reason="")
    recorded = json.loads(record_scheduled_run(outcome="no_change", state_summary="Nothing to change."))

    assert noted == {"recorded": True, "schedule_id": timer.schedule_id}
    assert recorded["recorded"] is True
    memory = agent._schedule_store.get_scheduled_message(timer.schedule_id).schedule_memory
    assert (memory.ledger.total_notifications, memory.ledger.total_runs) == (1, 1)
