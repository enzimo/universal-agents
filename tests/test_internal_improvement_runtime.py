import logging
from types import SimpleNamespace
from custom_agents.task_operation_runtime import TaskOperationRuntime

import pytest

from custom_messages.contracts.runtime_v1 import (
    INTERNAL_IMPROVEMENT_AGENT_TYPE,
    TEAM_ARCHITECT_AGENT_TYPE,
)
from custom_agents.internal_improvement_runtime import (
    INTERNAL_IMPROVEMENT_DELIVERY_MODE,
    build_internal_improvement_schedule,
    enqueue_internal_improvement_job,
    internal_improvement_schedule_id,
    prepare_internal_improvement_schedule_for_manager,
)
from custom_agents.schedule_store import (
    ScheduleStore,
    owner_canonical_user_id_for_schedule,
)
from custom_agents.schedule_management import (
    ScheduleMutationAuthorityMode,
    ScheduleMutationOperation,
    build_schedule_mutation_permit,
)
from custom_agents.scheduled_runtime import handle_scheduled_message
from custom_messages.enums import RequesterType
from access_control.identity import ISSUER_SCHEDULER, ContextVerifier
from tests.broker_access_support import forget_task_context_keys, use_scheduler_key


@pytest.fixture
def scheduler_key(monkeypatch):
    use_scheduler_key(monkeypatch)
    yield
    forget_task_context_keys()


class _Wake:
    def __init__(self) -> None:
        self.was_set = False

    def set(self) -> None:
        self.was_set = True


class _Comms:
    def __init__(self) -> None:
        self.sent = []

    async def send_message(self, *, message, routing_key, **kwargs) -> None:
        self.sent.append((message, routing_key, kwargs))


class _Worker:
    named_agent_type = INTERNAL_IMPROVEMENT_AGENT_TYPE
    team_id = "team_internal"

    def __init__(self, agent_id: str, interval_seconds: float = 21600) -> None:
        self.id = agent_id
        self.name = f"worker-{agent_id}"
        self.schedule_maintenance = {
            "internal_improvement_review_interval_seconds": interval_seconds,
        }
        self.team_comms = _Comms()
        self._logger = logging.getLogger(f"test.internal.worker.{agent_id}")


class _Manager:
    id = "manager_1"
    name = "manager"
    team_id = "team_internal"
    named_agent_type = TEAM_ARCHITECT_AGENT_TYPE
    is_team_lead = True
    default_request_timezone = "America/Chicago"

    def __init__(self, db_path=None) -> None:
        self.task_manager = SimpleNamespace(operations_runtime=TaskOperationRuntime())
        self.bootstrap_config = {
            "instance_name": "nova",
            "canonical_user_id": "user_123",
            "timezone": "America/Chicago",
        }
        self._logger = logging.getLogger("test.internal.manager")
        self._schedule_store = ScheduleStore(db_path=str(db_path)) if db_path else None
        self._scheduler_wake = _Wake()
        self._conversation_store = None
        self.team_comms = _Comms()
        self.named_agent_type_queue_map = {
            INTERNAL_IMPROVEMENT_AGENT_TYPE: f"{self.team_id}_{INTERNAL_IMPROVEMENT_AGENT_TYPE}"
        }
        self.availability_requests = []
        self.availability_result = True

    async def _probe_live_named_agent_type(
        self,
        named_agent_type,
    ):
        self.availability_requests.append(named_agent_type)
        return SimpleNamespace() if self.availability_result else None


def test_schedule_builder_uses_stable_team_singleton_and_configured_interval() -> None:
    first = build_internal_improvement_schedule(
        _Worker("worker_a", interval_seconds=30)
    )
    second = build_internal_improvement_schedule(
        _Worker("worker_b", interval_seconds=30)
    )

    assert first is not None
    assert second is not None
    assert (
        first.schedule_id
        == second.schedule_id
        == internal_improvement_schedule_id("team_internal")
    )
    assert first.schedule_id.startswith("int_imp_review_")
    assert first.payload.task_id.startswith("int_imp_launch_")
    assert first.install_mode == "ensure"
    assert first.interval_ms == 30_000
    assert first.repeat_count is None
    assert first.schedule_memory is not None
    assert first.schedule_memory.policy.notify == "never"


@pytest.mark.asyncio
async def test_multiple_worker_starts_share_one_manager_schedule_and_preserve_operator_state(
    tmp_path,
) -> None:
    manager = _Manager(tmp_path / "schedules.db")
    first = build_internal_improvement_schedule(
        _Worker("worker_first", interval_seconds=60)
    )
    second = build_internal_improvement_schedule(
        _Worker("worker_second", interval_seconds=5)
    )
    assert first is not None and second is not None

    await handle_scheduled_message(manager, first)
    original = manager._schedule_store.get_scheduled_message(first.schedule_id)
    assert original is not None
    original_next_fire_at = original.next_fire_at
    disabled = original.model_copy(deep=True)
    disabled.enabled = False
    disable_permit = build_schedule_mutation_permit(
        schedule=original,
        operation=ScheduleMutationOperation.DISABLE,
        authority_mode=ScheduleMutationAuthorityMode.AUTHENTICATED_GATEWAY_USER,
        actor_principal_id="user_123",
        owner_canonical_user_id="user_123",
        team_id=manager.team_id,
        candidate=disabled,
    )
    assert manager._schedule_store.set_enabled(
        first.schedule_id,
        False,
        system_mutation_permit=disable_permit,
    )
    paused_until = "2026-07-20T12:00:00+00:00"
    disabled = manager._schedule_store.get_scheduled_message(first.schedule_id)
    assert disabled is not None
    paused = disabled.model_copy(deep=True)
    paused.paused_until = paused_until
    pause_permit = build_schedule_mutation_permit(
        schedule=disabled,
        operation=ScheduleMutationOperation.PAUSE,
        authority_mode=ScheduleMutationAuthorityMode.AUTHENTICATED_GATEWAY_USER,
        actor_principal_id="user_123",
        owner_canonical_user_id="user_123",
        team_id=manager.team_id,
        candidate=paused,
    )
    assert manager._schedule_store.set_paused_until(
        first.schedule_id,
        paused_until,
        system_mutation_permit=pause_permit,
    )

    await handle_scheduled_message(manager, second)

    rows = manager._schedule_store.get_all()
    restored = manager._schedule_store.get_scheduled_message(first.schedule_id)
    assert len(rows) == 1
    assert restored is not None
    assert restored.interval_ms == 60_000
    assert restored.next_fire_at == original_next_fire_at
    assert restored.enabled is False
    assert restored.paused_until == "2026-07-20T12:00:00+00:00"
    assert restored.claim_team_id == manager.team_id
    assert restored.claim_named_agent_type == TEAM_ARCHITECT_AGENT_TYPE
    assert owner_canonical_user_id_for_schedule(restored) == "user_123"
    assert restored.payload_meta is not None
    assert restored.payload_meta.gateway_context is not None
    assert (
        restored.payload_meta.gateway_context.delivery_mode
        == INTERNAL_IMPROVEMENT_DELIVERY_MODE
    )
    assert manager._scheduler_wake.was_set is True


def test_manager_rejects_non_singleton_internal_schedule() -> None:
    manager = _Manager()
    schedule = build_internal_improvement_schedule(_Worker("worker_invalid"))
    assert schedule is not None
    schedule.schedule_id = "attacker_selected_id"

    assert prepare_internal_improvement_schedule_for_manager(manager, schedule) is None


@pytest.mark.asyncio
async def test_due_schedule_ensures_worker_and_dispatches_stable_occurrence() -> None:
    manager = _Manager()
    schedule = build_internal_improvement_schedule(_Worker("worker_dispatch"))
    assert schedule is not None
    schedule = prepare_internal_improvement_schedule_for_manager(manager, schedule)
    assert schedule is not None
    schedule.next_fire_at = "2026-07-15T12:00:00+00:00"

    assert (
        await enqueue_internal_improvement_job(manager, schedule.schedule_id, schedule)
        is True
    )
    assert (
        await enqueue_internal_improvement_job(manager, schedule.schedule_id, schedule)
        is True
    )

    assert len(manager.availability_requests) == 2
    assert all(
        item == INTERNAL_IMPROVEMENT_AGENT_TYPE
        for item in manager.availability_requests
    )
    assert len(manager.team_comms.sent) == 2
    first_envelope = manager.team_comms.sent[0][0]
    second_envelope = manager.team_comms.sent[1][0]
    assert first_envelope.msg_content.task_id == second_envelope.msg_content.task_id
    assert first_envelope.msg_content.task_id.startswith("int_imp_")
    assert (
        first_envelope.msg_meta.msg_correlation_id == first_envelope.msg_content.task_id
    )
    assert (
        first_envelope.msg_header.idempotency_key
        == second_envelope.msg_header.idempotency_key
    )
    assert (
        first_envelope.msg_meta.to_named_agent_type == INTERNAL_IMPROVEMENT_AGENT_TYPE
    )
    assert first_envelope.msg_meta.requester_type == RequesterType.TIMER_REQUESTER
    assert (
        first_envelope.msg_meta.gateway_context.scheduled_job_id == schedule.schedule_id
    )
    # The run is system work: it names no person (the schedule still records
    # the first user, whom a worker launch names).
    assert first_envelope.msg_meta.gateway_context.canonical_user_id is None
    assert owner_canonical_user_id_for_schedule(schedule) == "user_123"
    assert "record_scheduled_run" in first_envelope.msg_content.problem_statement
    # Trusted scratch cleanup runs first and its outcome is part of the run's
    # instructions (this fake manager has no instance folders, so it says so).
    assert "Scratch cleanup did not run" in first_envelope.msg_content.problem_statement
    assert "inspect_memory_review_cleanup" in first_envelope.msg_content.problem_statement
    assert "maintain_memory_review_jobs" in first_envelope.msg_content.problem_statement
    assert (
        "suppress_outbound_dispatch=true"
        in first_envelope.msg_content.problem_statement
    )


@pytest.mark.asyncio
async def test_due_schedule_remains_pending_when_worker_cannot_be_started(monkeypatch) -> None:
    monkeypatch.setattr(
        "custom_agents.agent_instantiation_service.AgentInstantiationService",
        lambda agent: SimpleNamespace(default_idle_ttl=lambda name: 600, launch=lambda **kwargs: SimpleNamespace(status="denied", reason="not_allowed")),
    )
    manager = _Manager()
    manager.availability_result = False
    schedule = build_internal_improvement_schedule(_Worker("worker_missing"))
    assert schedule is not None
    schedule = prepare_internal_improvement_schedule_for_manager(manager, schedule)
    assert schedule is not None

    assert (
        await enqueue_internal_improvement_job(manager, schedule.schedule_id, schedule)
        is False
    )
    assert manager.team_comms.sent == []


@pytest.mark.asyncio
async def test_trusted_cleanups_run_once_per_occurrence_even_when_the_worker_cannot_start(monkeypatch) -> None:
    """The scheduler retries an occurrence whose worker did not start every 30 seconds; clean up once."""

    from custom_agents import internal_improvement_runtime as runtime

    monkeypatch.setattr(
        "custom_agents.agent_instantiation_service.AgentInstantiationService",
        lambda agent: SimpleNamespace(default_idle_ttl=lambda name: 600, launch=lambda **kwargs: SimpleNamespace(status="denied", reason="not_allowed")),
    )
    ran: list[str] = []

    async def scratch(agent, schedule_id):
        ran.append("scratch")
        return "scratch sentence"

    async def outputs(agent, schedule_id):
        ran.append("outputs")
        return "outputs sentence"

    monkeypatch.setattr(runtime, "prune_scratch_for_maintenance", scratch)
    monkeypatch.setattr(runtime, "remove_old_outputs_for_maintenance", outputs)
    manager = _Manager()
    manager.availability_result = False
    schedule = prepare_internal_improvement_schedule_for_manager(
        manager, build_internal_improvement_schedule(_Worker("worker_missing"))
    )
    schedule.next_fire_at = "2026-10-28T06:00:00+00:00"

    assert await enqueue_internal_improvement_job(manager, schedule.schedule_id, schedule) is False
    assert await enqueue_internal_improvement_job(manager, schedule.schedule_id, schedule) is False
    assert ran == ["scratch", "outputs"]
    assert manager.team_comms.sent == []

    # The next occurrence cleans up again, and its dispatch carries the sentences.
    manager.availability_result = True
    schedule.next_fire_at = "2026-10-28T12:00:00+00:00"
    assert await enqueue_internal_improvement_job(manager, schedule.schedule_id, schedule) is True
    assert ran == ["scratch", "outputs", "scratch", "outputs"]
    problem = manager.team_comms.sent[0][0].msg_content.problem_statement
    assert "scratch sentence" in problem and "outputs sentence" in problem


def _due_maintenance_schedule(manager):
    schedule = build_internal_improvement_schedule(_Worker("worker_signed"))
    assert schedule is not None
    schedule = prepare_internal_improvement_schedule_for_manager(manager, schedule)
    assert schedule is not None
    schedule.next_fire_at = "2026-09-25T12:00:00+00:00"
    return schedule


@pytest.mark.asyncio
async def test_a_maintenance_run_is_signed_by_the_scheduler_as_work_for_no_person(scheduler_key) -> None:
    manager = _Manager()
    schedule = _due_maintenance_schedule(manager)

    assert await enqueue_internal_improvement_job(manager, schedule.schedule_id, schedule) is True

    gateway_context = manager.team_comms.sent[0][0].msg_meta.gateway_context
    context = ContextVerifier.from_env().verify(gateway_context.task_context, team_id=manager.team_id)
    assert context.issuer == ISSUER_SCHEDULER
    assert context.person_id is None and context.person_roles == ()
    # The context names the schedule, so the run may record that schedule's run.
    assert context.schedule_id == schedule.schedule_id
    # No part of the run names a person it does not work for.
    assert gateway_context.canonical_user_id is None


@pytest.mark.asyncio
async def test_without_a_scheduler_key_the_run_is_unsigned_and_the_log_says_why(monkeypatch, caplog) -> None:
    monkeypatch.delenv("ACCESS_SCHEDULER_SIGNING_KEY", raising=False)
    forget_task_context_keys()
    manager = _Manager()
    schedule = _due_maintenance_schedule(manager)

    with caplog.at_level(logging.WARNING):
        assert await enqueue_internal_improvement_job(manager, schedule.schedule_id, schedule) is True

    assert manager.team_comms.sent[0][0].msg_meta.gateway_context.task_context is None
    assert "event=task_context_signer_missing" in caplog.text
    assert schedule.schedule_id in caplog.text
    forget_task_context_keys()
