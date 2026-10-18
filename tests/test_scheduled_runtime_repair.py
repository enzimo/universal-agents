from custom_agents.task_operation_runtime import TaskOperationRuntime
import logging
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from custom_agents.conversation_store import ConversationStore
from custom_agents.heartbeat_store import HeartbeatStore
from custom_agents.schedule_store import ScheduleStore
from custom_agents.scheduled_runtime import (
    enqueue_heartbeat_items,
    handle_scheduled_message,
)
from custom_messages.contracts.routing_v1 import (
    GatewayContext,
    MessageHeader,
    MessageMeta,
)
from custom_messages.contracts.schedule_v1 import ScheduledMessage
from custom_messages.contracts.task_v1 import Task
from custom_messages.enums import (
    MessageMetaType,
    RequesterType,
    ScheduleManagementClass,
    SystemScheduleKind,
)
from storage.config import resolve_storage_settings


@pytest.mark.asyncio
async def test_provider_retry_cannot_be_claimed_by_sibling_worker(tmp_path):
    from datetime import datetime, timedelta, timezone
    from custom_agents.strands_agent import UniversalAgent

    agent = _FakeAgent()
    agent.named_agent_type = "WebResearchAgent"
    agent.instance_timezone = "UTC"
    agent._schedule_store = ScheduleStore(db_path=str(tmp_path / "timers.db"))
    agent._ensure_schedule_store = lambda: agent._schedule_store
    info = SimpleNamespace(
        task=Task(task_id="paused_task", task_type=MessageMetaType.REQUEST),
        header=MessageHeader(from_agent_id="requester"),
        meta=MessageMeta(),
    )
    schedule = await UniversalAgent._schedule_provider_cooldown_retry(
        agent, task_info=info, retry_at=datetime.now(timezone.utc) - timedelta(seconds=1)
    )
    assert agent._schedule_store.claim_next_due(
        claimer_agent_id="sibling_worker", claimer_named_agent_type=agent.named_agent_type,
        claimer_team_id=agent.team_id,
    ) is None
    claimed = agent._schedule_store.claim_next_due(
        claimer_agent_id=agent.id, claimer_named_agent_type=agent.named_agent_type,
        claimer_team_id=agent.team_id,
    )
    assert claimed is not None and claimed["schedule_id"] == schedule.schedule_id


@pytest.mark.parametrize("management,kind", [
    (ScheduleManagementClass.USER, None),
    (ScheduleManagementClass.SYSTEM, SystemScheduleKind.UNCLASSIFIED),
])
def test_heartbeat_name_does_not_grant_launcher_behavior(management, kind):
    from custom_agents.heartbeat_store import is_heartbeat_launcher

    schedule = ScheduledMessage(
        schedule_name="heartbeat:personal", management_class=management, system_schedule_kind=kind,
        payload=Task(task_type=MessageMetaType.REQUEST),
        payload_meta=MessageMeta(gateway_context=GatewayContext(heartbeat_source_id="source")),
        interval_ms=60_000,
    )
    assert not is_heartbeat_launcher(schedule)


class _FakeScheduleStore:
    def __init__(self):
        self.added = None

    def add(self, schedule, **kwargs):
        self.added = schedule
        self.kwargs = kwargs

    def get_scheduled_message(self, schedule_id):
        return None


class _FakeSchedulerWake:
    def __init__(self):
        self.was_set = False

    def set(self):
        self.was_set = True


class _FakeAgent:
    id = "agent_1"
    name = "manager"
    team_id = "team_1"
    named_agent_type = "TeamArchitect"

    def __init__(self, tmp_path=None):
        self.task_manager = SimpleNamespace(operations_runtime=TaskOperationRuntime())
        self._logger = logging.getLogger("test_scheduled_runtime_repair")
        self._schedule_store = _FakeScheduleStore()
        self._scheduler_wake = _FakeSchedulerWake()
        self._conversation_store = (
            ConversationStore(db_path=str(tmp_path / "conversation.db"), artifacts_dir=tmp_path / "conversation_artifacts")
            if tmp_path
            else None
        )
        self.records = []

    async def _record_runtime_repair_anomaly(self, event, summary, **kwargs):
        self.records.append((event, summary, kwargs))


@pytest.mark.asyncio
async def test_handle_scheduled_message_records_payload_context_repairs():
    agent = _FakeAgent()
    schedule = ScheduledMessage(
        schedule_id="sched_repair",
        schedule_name="repair-test",
        payload=Task(
            task_id="task_scheduled",
            task_type=MessageMetaType.REQUEST,
            problem_statement="run later",
        ),
        payload_header=None,
        payload_meta=None,
        interval_ms=60_000,
    )

    await handle_scheduled_message(
        agent,
        schedule,
        msg_header=MessageHeader(
            from_agent_id="manager",
            to_routing_path="agent_1",
            response_routing_key="gateway_response",
        ),
        msg_meta=MessageMeta(
            msg_correlation_id="corr_schedule",
            conversation_id="conversation_schedule",
        ),
    )

    assert agent._schedule_store.added is not None
    assert agent._schedule_store.added.payload_header is not None
    assert agent._schedule_store.added.payload_meta is not None
    assert agent._schedule_store.kwargs == {
        "claim_team_id": "team_1",
        "claim_named_agent_type": "TeamArchitect",
        "claim_agent_id": None,
    }
    assert agent._scheduler_wake.was_set is True
    assert [record[0] for record in agent.records] == [
        "scheduled_message_payload_header_preserved",
        "scheduled_message_payload_meta_preserved",
    ]
    assert agent.records[0][2]["related_task_id"] == "task_scheduled"
    assert agent.records[1][2]["details"]["correlation_id"] == "corr_schedule"


@pytest.mark.asyncio
async def test_trusted_runtime_replacement_normalizes_before_issuing_system_permit(
    tmp_path,
):
    agent = _FakeAgent()
    agent._schedule_store = ScheduleStore(db_path=str(tmp_path / "schedules.db"))
    schedule = ScheduledMessage(
        schedule_id="hb_sched_runtime_replace",
        schedule_name="heartbeat:runtime-replace",
        management_class=ScheduleManagementClass.SYSTEM,
        system_schedule_kind=SystemScheduleKind.HEARTBEAT_LAUNCHER,
        payload=Task(
            task_type=MessageMetaType.REQUEST,
            problem_statement="Launch heartbeat work.",
        ),
        payload_header=MessageHeader(
            from_agent_id="gateway",
            response_routing_key="gateway",
        ),
        payload_meta=MessageMeta(
            requester_type=RequesterType.TIMER_REQUESTER,
            gateway_context=GatewayContext(canonical_user_id="usr_owner"),
        ),
        interval_ms=60_000,
        repeat_count=None,
    )

    await handle_scheduled_message(agent, schedule)
    replacement = schedule.model_copy(deep=True)
    replacement.interval_ms = 120_000
    replacement.next_fire_at = None
    await handle_scheduled_message(agent, replacement)

    restored = agent._schedule_store.get_scheduled_message(schedule.schedule_id)
    assert restored is not None
    assert restored.interval_ms == 120_000
    assert restored.claim_team_id == agent.team_id
    assert restored.claim_named_agent_type == agent.named_agent_type


@pytest.mark.asyncio
async def test_heartbeat_does_not_dispatch_work_to_unavailable_worker_role(
    tmp_path,
) -> None:
    settings = resolve_storage_settings(
        {"backend": "sqlite", "sqlite": {"path": "runtime/task_history.db"}},
        base_dir=str(tmp_path),
    )
    heartbeat_store = HeartbeatStore(storage_settings=settings)
    heartbeat_store.ensure_source(source_id="heartbeat_source", title="Heartbeat")
    item = heartbeat_store.add_item(
        source_id="heartbeat_source",
        title="Unavailable worker check",
        instruction="Run only if the worker is live.",
        conversation_id="conversation_heartbeat",
        target_agent_type="MissingWorkerAgent",
    )
    availability = AsyncMock(return_value=False)
    send_message = AsyncMock()
    agent = SimpleNamespace(
        id="manager_1",
        name="Manager",
        team_id="team_1",
        paused=False,
        is_executing=True,
        task_manager=SimpleNamespace(
            operations_runtime=TaskOperationRuntime(),
            _db_path=settings.sqlite_path,
            storage_settings=settings,
        ),
        named_agent_type_queue_map={},
        team_comms=SimpleNamespace(send_message=send_message),
        _verify_delegation_target=availability,
        _logger=logging.getLogger("test.heartbeat.worker_liveness"),
    )
    schedule = ScheduledMessage(
        schedule_id="heartbeat_launcher",
        schedule_name="heartbeat:test",
        payload=Task(
            task_type=MessageMetaType.REQUEST,
            problem_statement="Launch heartbeat items.",
        ),
        payload_meta=MessageMeta(
            gateway_context=GatewayContext(heartbeat_source_id="heartbeat_source")
        ),
        interval_ms=60_000,
    )

    assert await enqueue_heartbeat_items(agent, schedule) is True

    availability.assert_awaited_once_with("MissingWorkerAgent")
    send_message.assert_not_awaited()
    refreshed = heartbeat_store.get_item(item.heartbeat_item_id)
    assert refreshed is not None
    assert refreshed.last_run_at is not None
    assert refreshed.last_failure_at is not None
    assert refreshed.last_success_at is None


@pytest.mark.asyncio
async def test_replayed_heartbeat_occurrence_keeps_worker_task_identity(tmp_path):
    from datetime import datetime, timedelta, timezone

    settings = resolve_storage_settings({"backend": "sqlite", "sqlite": {"path": str(tmp_path / "heartbeat.db")}})
    store = HeartbeatStore(storage_settings=settings)
    store.ensure_source(source_id="source")
    store.add_item(source_id="source", title="Alerts", instruction="Check alerts", conversation_id="conversation_heartbeat", target_agent_type="WebResearchAgent")
    sends = AsyncMock()
    agent = SimpleNamespace(
        id="manager", name="Manager", team_id="team", named_agent_type="TeamArchitect", paused=True,
        task_manager=SimpleNamespace(operations_runtime=TaskOperationRuntime(), _db_path=settings.sqlite_path, storage_settings=settings),
        named_agent_type_queue_map={}, team_comms=SimpleNamespace(send_message=sends),
        _verify_delegation_target=AsyncMock(return_value=True), _logger=logging.getLogger("heartbeat.replay"),
    )
    schedule = ScheduledMessage(
        schedule_id="launcher", schedule_name="heartbeat:test", management_class=ScheduleManagementClass.SYSTEM,
        system_schedule_kind=SystemScheduleKind.HEARTBEAT_LAUNCHER,
        payload=Task(task_type=MessageMetaType.REQUEST),
        payload_meta=MessageMeta(gateway_context=GatewayContext(heartbeat_source_id="source")),
        interval_ms=60_000, next_fire_at=datetime.now(timezone.utc).isoformat(),
    )
    # The launcher was dispatched but its due time was not advanced before restart.
    assert await enqueue_heartbeat_items(agent, schedule)
    agent.id = "replacement_manager"
    assert await enqueue_heartbeat_items(agent, schedule.model_copy(deep=True))
    first, replay = [call.kwargs["message"] for call in sends.await_args_list]
    assert first.msg_content.task_id == replay.msg_content.task_id
    assert first.msg_header.idempotency_key == replay.msg_header.idempotency_key
    assert first.msg_header.idempotency_key
    # The broker's duplicate window may have elapsed. A different worker must
    # still refuse this occurrence after the first worker completed it.
    from custom_agents.task_manager import TaskManager
    from custom_agents.task_message_runtime import _custody_for_new_task

    original_worker = TaskManager(storage_settings=settings)
    original_worker.set_owner_identity("worker_one", "First worker", "team", "WebResearchAgent")
    assert original_worker.add_task(first.msg_content, first.msg_header, first.msg_meta, owner_person_id=None)
    original_worker.complete_task(first.msg_content.task_id, datetime.now(timezone.utc).isoformat())
    sibling = TaskManager(storage_settings=settings)
    sibling.set_owner_identity("worker_two", "Second worker", "team", "WebResearchAgent")
    sibling_agent = SimpleNamespace(
        id="worker_two", team_id="team", task_manager=sibling, _logger=agent._logger,
    )
    fresh_header = replay.msg_header.model_copy(update={"message_id": "fresh_transport_delivery", "idempotency_key": None})
    assert await _custody_for_new_task(sibling_agent, replay.msg_content, fresh_header, replay.msg_meta, None) is None
    assert not sibling.task_state.tasks
    schedule.next_fire_at = (datetime.fromisoformat(schedule.next_fire_at) + timedelta(minutes=1)).isoformat()
    assert await enqueue_heartbeat_items(agent, schedule)
    assert sends.await_args.kwargs["message"].msg_content.task_id != first.msg_content.task_id


@pytest.mark.asyncio
async def test_heartbeat_item_admission_survives_restart_before_launcher_advances(tmp_path):
    from datetime import datetime, timedelta, timezone
    from custom_agents.task_manager import TaskManager
    from custom_messages.contracts.schedule_worker_v1 import occurrence_identity

    db_path = str(tmp_path / "heartbeat_admission.db")
    manager = TaskManager(db_path=db_path)
    manager.set_owner_identity("manager", "Manager", "team", "TeamArchitect")
    heartbeat = HeartbeatStore(storage_settings=manager.storage_settings)
    heartbeat.ensure_source(source_id="source")
    item = heartbeat.add_item(source_id="source", title="Alerts", instruction="Check alerts", conversation_id="conversation_item")
    schedules = ScheduleStore(storage_settings=manager.storage_settings)
    schedules.add(ScheduledMessage(
        schedule_id="launcher", schedule_name="heartbeat:test", management_class=ScheduleManagementClass.SYSTEM,
        system_schedule_kind=SystemScheduleKind.HEARTBEAT_LAUNCHER,
        payload=Task(task_type=MessageMetaType.REQUEST),
        payload_meta=MessageMeta(gateway_context=GatewayContext(heartbeat_source_id="source")),
        interval_ms=60_000, repeat_count=None,
        next_fire_at=(datetime.now(timezone.utc) - timedelta(seconds=1)).isoformat(),
    ), claim_team_id="team", claim_named_agent_type="TeamArchitect")
    claim = schedules.claim_next_due(claimer_agent_id="manager", claimer_named_agent_type="TeamArchitect", claimer_team_id="team")
    schedule = schedules.get_scheduled_message("launcher")
    agent = SimpleNamespace(
        id="manager", name="Manager", team_id="team", named_agent_type="TeamArchitect", paused=True,
        task_manager=manager, _schedule_store=schedules, _logger=logging.getLogger("heartbeat.admission"),
    )
    assert await enqueue_heartbeat_items(agent, schedule, claim_token=claim["claim_token"])
    occurrence_id = occurrence_identity(f"launcher:{item.heartbeat_item_id}", schedule.next_fire_at)
    receipt = schedules.worker_records.get_occurrence(occurrence_id, "team")
    assert receipt is not None
    # Restart with the launcher still due. Its task and receipt committed together.
    restarted = TaskManager(db_path=db_path)
    restarted.set_owner_identity("manager", "Manager", "team", "TeamArchitect")
    restarted.restore_runtime_state()
    agent.task_manager = restarted
    assert await enqueue_heartbeat_items(agent, schedule, claim_token=claim["claim_token"])
    assert list(restarted.task_state.tasks) == [receipt.task_id]
    assert schedules.get_scheduled_message("launcher").next_fire_at == schedule.next_fire_at


@pytest.mark.parametrize("explicit_followup", [True])
def test_timer_conversation_history_does_not_invent_reply_obligation(
    tmp_path, explicit_followup
):
    from custom_agents.conversation_runtime import ensure_task_conversation_context
    from custom_agents.turn_action_outbox_runtime import (
        _explicit_suppressed_task_response_invalid_reason,
    )

    store = ConversationStore(db_path=str(tmp_path / "conversation.db"), artifacts_dir=tmp_path / "conversation_artifacts")
    header = MessageHeader(from_agent_id="gateway", response_routing_key="gateway")
    first = Task(
        task_id="prior_run",
        task_type=MessageMetaType.REQUEST,
        problem_statement="Check Gmail",
    )
    meta = ensure_task_conversation_context(
        conversation_store=store,
        team_id="team_1",
        owner_agent_id="worker",
        owner_agent_name="PersonalAdminAgent",
        task_request=first,
        msg_header=header,
        msg_meta=MessageMeta(requester_type=RequesterType.TIMER_REQUESTER),
        owner_person_id=None,
    )
    later = Task(
        task_id="next_run",
        task_type=MessageMetaType.REQUEST,
        problem_statement="Check Gmail",
    )
    meta.followup_to_task_id = first.task_id if explicit_followup else None
    admitted = ensure_task_conversation_context(
        conversation_store=store,
        team_id="team_1",
        owner_agent_id="worker",
        owner_agent_name="PersonalAdminAgent",
        task_request=later,
        msg_header=header,
        msg_meta=meta,
        owner_person_id=None,
    )
    assert admitted.conversation_id == meta.conversation_id
    assert store.get_turn_by_task_id(first.task_id) is not None
    reason, _ = _explicit_suppressed_task_response_invalid_reason(
        incoming_content=later,
        incoming_meta=admitted,
        outgoing_content=later.model_copy(
            update={"task_type": MessageMetaType.RESPONSE, "answer": "No new mail"}
        ),
    )
    assert reason == ("followup_work" if explicit_followup else None)
    assert store.get_turn_by_task_id(later.task_id)["followup_to_task_id"] == (
        first.task_id if explicit_followup else None
    )
