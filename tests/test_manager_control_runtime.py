from __future__ import annotations

from custom_agents.task_operation_runtime import TaskOperationRuntime

import asyncio
import json
import logging
import sqlite3
import threading
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from assistant_gateway.config import GatewayInstanceConfig
from custom_agents.conversation_store import ConversationStore
from custom_agents.manager_bootstrap_runtime import run_manager_bootstrap
from custom_agents.manager_control_runtime import (
    build_task_progress_response,
    handle_manager_memory_command,
    handle_manager_runtime_work_command,
    handle_manager_schedule_command,
)
from custom_agents.pinned_memory_runtime import (
    PinnedMemoryCommand,
    PinnedMemoryRequestContext,
    PinnedMemoryService,
)
from custom_agents.schedule_store import ScheduleStore
from custom_agents.task_manager import TaskManager
from custom_messages.contracts.routing_v1 import PERSON_GATEWAY_FIELDS
from custom_messages.contracts.access_v2 import PersonStanding
from custom_messages.contracts.control_v1 import (
    ControlInstruction,
    ManagerMemoryCommand,
    ManagerRuntimeWorkCommand,
    PinnedMemoryCommandPayload,
)
from custom_messages.contracts.envelope_v1 import MessageEnvelope
from custom_messages.contracts.routing_v1 import (
    GatewayContext,
    MessageHeader,
    MessageMeta,
)
from custom_messages.contracts.schedule_v1 import (
    SYSTEM_SCHEDULE_CHANGE_NOT_ALLOWED,
    ManagerScheduleCommand,
    ScheduledMessage,
)
from custom_messages.contracts.task_v1 import Task, TaskProgressStatus
from custom_messages.enums import (
    MessageMetaType,
    ObjectiveHorizon,
    ObjectiveStatus,
    ProgressState,
    ScheduleManagementClass,
    SystemScheduleKind,
)
from custom_messages.contracts.common_v1 import contract_sha256
from custom_tools.schedule_inspection import ScheduleView, list_schedule_payloads
from storage.pinned_memory_repository import (
    USERMEM_BANK,
    create_pinned_memory_repository,
)
from storage.config import resolve_storage_settings
from storage.sqlite_connection import connect_sqlite
from storage.task_archive_repository import SqliteTaskArchiveRepository
from tests.broker_access_support import (
    context_signer,
    context_verify_keys,
    forget_task_context_keys,
)
from custom_agents import manager_control_runtime
from tests.change_approval_support import CEDAR_BINARY, needs_cedar
from tests import scheduled_run_support as scheduled_runs


@pytest.fixture(autouse=True)
def _gateway_signing_keys(monkeypatch: pytest.MonkeyPatch):
    """Gateway commands name their person only through a gateway-signed context."""

    monkeypatch.setenv("ACCESS_CONTEXT_VERIFY_KEYS", context_verify_keys())
    forget_task_context_keys()
    yield
    forget_task_context_keys()


def _signed(context: GatewayContext, *, team_id: str, roles: list[str]) -> GatewayContext:
    """The gateway context with the signed-in person signed, as the gateway sends it."""

    signed = context_signer().issue(
        team_id=team_id, person_id=context.canonical_user_id, person_roles=tuple(roles)
    )
    return context.model_copy(update={"task_context": signed})


class _CaptureComms:
    def __init__(self) -> None:
        self.messages: list[MessageEnvelope] = []

    async def send_message(self, *, message: MessageEnvelope, routing_key: str) -> None:
        self.messages.append(message)


def _config(tmp_path: Path) -> GatewayInstanceConfig:
    config = GatewayInstanceConfig.create(
        instance_name="manager_control",
        instance_root=str(tmp_path / "instance"),
        team_id="team_control",
        timezone="America/Chicago",
        display_name="Owner",
        initial_user_context="Prefers concise status updates.",
    )
    config.state.storage.sqlite.path = str(tmp_path / "manager.db")
    config.layout.ensure_directories()
    return config


def _bootstrap_agent(config: GatewayInstanceConfig) -> SimpleNamespace:
    repository = create_pinned_memory_repository(config.manager_storage_settings)
    return SimpleNamespace(
        id="manager_1",
        name="Manager",
        is_team_lead=True,
        team_id=config.gateway.team_id,
        instance_root=str(config.instance_root_path),
        workspace_dir=str(config.workspace_path),
        layout=config.layout,
        memory_config=dict(config.agent.memory or {}),
        task_manager=SimpleNamespace(
            operations_runtime=TaskOperationRuntime(),
            storage_settings=config.manager_storage_settings,
            _db_path=config.manager_storage_settings.sqlite_path or "",
        ),
        _pinned_memory_repository=repository,
        _logger=logging.getLogger("test.manager_bootstrap"),
        bootstrap_config={
            "instance_name": config.instance_name,
            "instance_root": str(config.instance_root_path),
            "workspace_dir": str(config.workspace_path),
            "team_id": config.gateway.team_id,
            "canonical_user_id": config.user.canonical_user_id,
            "timezone": config.user.timezone,
            "initial_user_context": config.user.initial_context,
            "heartbeat_enabled": False,
            "heartbeat_source_id": "hb_source_test",
        },
    )


@pytest.mark.parametrize("embedded_owner", [None, "usr_stale_person"])
def test_manager_bootstrap_imports_legacy_files_once_and_gives_away_no_records(
    tmp_path: Path,
    embedded_owner: str | None,
) -> None:
    """Records with no owner are work for no person; no start makes them the first user's."""

    config = _config(tmp_path)
    # The layout move commands put the old single workspace's files in team/.
    legacy_dir = config.layout.team_dir
    (legacy_dir / "USER.md").write_text(
        "Legacy user profile.\n", encoding="utf-8"
    )
    (legacy_dir / "HEARTBEAT.md").write_text(
        "# Heartbeat\n\n## Active Tasks\n\n- Check the inbox.\n",
        encoding="utf-8",
    )
    store = ScheduleStore(storage_settings=config.manager_storage_settings)
    store.add(
        ScheduledMessage(
            schedule_id="legacy_schedule",
            payload=Task(
                task_type=MessageMetaType.REQUEST, problem_statement="Legacy task"
            ),
            payload_meta=MessageMeta(
                gateway_context=GatewayContext(
                    session_key="legacy_session", canonical_user_id=embedded_owner
                )
            ),
            interval_ms=60_000,
            repeat_count=None,
        )
    )
    # Adding the nullable owner column to an old row leaves it unassigned,
    # even when its saved JSON still names the former single user's person.
    with connect_sqlite(config.manager_storage_settings.sqlite_path or "") as conn:
        conn.execute(
            "UPDATE scheduled_messages SET owner_canonical_user_id = NULL WHERE schedule_id = 'legacy_schedule'"
        )
    # A finished run for no person, such as the knowledge curator's team run.
    SqliteTaskArchiveRepository(config.manager_storage_settings.sqlite_path or "").save_completed_task(
        archive_id="archive_system_run",
        task_id="task_system_run",
        owner_agent_id="manager_1",
        owner_agent_name="Manager",
        team_id=config.gateway.team_id,
        task_json="{}",
        status="completed",
    )
    agent = _bootstrap_agent(config)

    first = run_manager_bootstrap(agent)
    second = run_manager_bootstrap(agent)

    rows = create_pinned_memory_repository(
        config.manager_storage_settings
    ).list_usermem(canonical_user_id=config.user.canonical_user_id)
    assert first.onboarding_memory_id is not None
    assert "migrated" in {result.status for result in first.workspace_migrations}
    assert {result.status for result in second.workspace_migrations} == {"not_present"}
    assert {row.fact_text for row in rows} == {
        "Prefers concise status updates.",
        "Legacy user profile.",
    }
    restored = store.get_scheduled_message("legacy_schedule")
    assert restored is not None
    assert restored.payload_meta.gateway_context.canonical_user_id is None
    with connect_sqlite(config.manager_storage_settings.sqlite_path or "") as conn:
        owners = conn.execute(
            "SELECT owner_canonical_user_id FROM completed_tasks WHERE task_id = 'task_system_run' "
            "UNION ALL SELECT owner_canonical_user_id FROM scheduled_messages WHERE schedule_id = 'legacy_schedule'"
        ).fetchall()
    assert [row[0] for row in owners] == [None, None]
    assert not (legacy_dir / "USER.md").exists()
    assert not (legacy_dir / "HEARTBEAT.md").exists()


@pytest.mark.parametrize("embedded_owner", [None, scheduled_runs.ANN])
def test_legacy_ownerless_schedule_stays_unassigned_through_startup_and_firing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, embedded_owner: str | None
) -> None:
    """A nullable column upgrade must not turn saved personal identity into a run owner."""

    world = scheduled_runs.build_world(tmp_path, monkeypatch)
    scheduled_runs.record_ann(world, PersonStanding.ACTIVE)
    schedule = scheduled_runs.save_schedule(world)
    schedule.payload_meta.conversation_id = "old_personal_conversation"
    schedule.payload_meta.followup_to_task_id = "old_personal_task"
    context = schedule.payload_meta.gateway_context
    context.canonical_user_id = embedded_owner
    context.user_id = scheduled_runs.ANN
    context.session_key = "old_personal_session"
    context.channel_name = "telegram"
    context.chat_id = "old_personal_chat"
    context.scheduled_job_id = schedule.schedule_id
    world.store.add(schedule)
    raw_json = schedule.model_dump_json()
    # Reproduce the real schema upgrade: old JSON has identity, but there was
    # no owner column. Initializing the repository adds it without an owner.
    with connect_sqlite(world.db_path) as conn:
        conn.execute("DROP INDEX idx_scheduled_messages_owner_user")
        conn.execute("ALTER TABLE scheduled_messages DROP COLUMN owner_canonical_user_id")
    world.store = ScheduleStore(storage_settings=world.tasks.storage_settings)
    world.manager._schedule_store = world.store
    world.worker._schedule_store = world.store
    world.worker._conversation_store = ConversationStore(
        world.db_path, artifacts_dir=tmp_path / "conversation_artifacts"
    )
    bootstrap = _bootstrap_agent(_config(tmp_path))
    run_manager_bootstrap(bootstrap)
    run_manager_bootstrap(bootstrap)

    loaded = world.store.get_scheduled_message(schedule.schedule_id)
    assert loaded is not None
    assert all(getattr(loaded.payload_meta.gateway_context, name) is None for name in PERSON_GATEWAY_FIELDS)
    assert loaded.payload_meta.conversation_id is None
    assert loaded.payload_meta.followup_to_task_id is None
    assert loaded.payload_meta.gateway_context.scheduled_job_id == schedule.schedule_id
    assert loaded.payload_header.to_routing_path == schedule.payload_header.to_routing_path
    assert list_schedule_payloads(world.store, view=ScheduleView(owner=scheduled_runs.ANN)) == []
    with connect_sqlite(world.db_path) as conn:
        row = conn.execute(
            "SELECT owner_canonical_user_id, schedule_json FROM scheduled_messages WHERE schedule_id = ?",
            (schedule.schedule_id,),
        ).fetchone()
    assert row[0] is None
    assert json.loads(row[1]) == json.loads(raw_json), "Loading changes no stored record."

    async def scenario():
        await scheduled_runs.subscribe(world)
        return await scheduled_runs.fire(world, scheduled_runs.claim(world))

    assert asyncio.run(scenario()) == {}, "The ownerless occurrence commits without a scope mismatch."
    run = scheduled_runs.queued_run(world)
    assert run.owner_person_id is None
    signed = scheduled_runs.run_context(run)
    assert signed.person_id is None and signed.person_roles == ()
    assert all(
        getattr(run.meta.gateway_context, name) is None
        for name in PERSON_GATEWAY_FIELDS
        if name != "task_context"
    )
    assert scheduled_runs.claim_row(world) == (None, None)
    conversations = world.worker._conversation_store
    assert conversations.get_turn_by_task_id(run.task.task_id)["owner_canonical_user_id"] is None
    assert conversations.list_conversations(owner_canonical_user_id=scheduled_runs.ANN) == []


@pytest.mark.asyncio
async def test_manager_memory_command_is_owner_scoped_and_returns_over_nats_contract(
    tmp_path: Path,
) -> None:
    config = _config(tmp_path)
    comms = _CaptureComms()
    agent = _bootstrap_agent(config)
    agent.team_comms = comms
    command = ManagerMemoryCommand(
        command=PinnedMemoryCommandPayload(
            bank="usermem",
            action="create",
            fact_text="Owner-only preference.",
        ),
    )
    meta = MessageMeta(
        msg_correlation_id="corr_1",
        conversation_id="conv_1",
        gateway_context=_signed(
            GatewayContext(canonical_user_id="Owner", session_key="session_1"),
            team_id=config.gateway.team_id,
            roles=["admin"],
        ),
    )
    await handle_manager_memory_command(
        agent,
        command=command,
        msg_header=MessageHeader(
            from_agent_id="gateway", response_routing_key="gateway"
        ),
        msg_meta=meta,
    )

    response = comms.messages[-1].msg_content
    assert isinstance(response, ManagerMemoryCommand)
    assert response.success is True
    assert response.result is not None
    assert response.result.fact is not None
    assert response.result.fact.fact_text == "Owner-only preference."
    assert len(response.result.fact.record_digest) == 64
    assert "metadata" not in response.result.fact.model_dump()
    assert "created_by_id" not in response.result.fact.model_dump()
    assert "User memory saved" in response.rendered_result
    service = PinnedMemoryService(agent._pinned_memory_repository)
    other = service.execute(
        PinnedMemoryCommand(bank=USERMEM_BANK, action="list"),
        context=PinnedMemoryRequestContext(
            team_id=config.gateway.team_id, canonical_user_id="Other"
        ),
    )
    assert other["facts"] == []


@pytest.mark.asyncio
async def test_manager_memory_command_allows_team_read_but_requires_admin_for_mutation(
    tmp_path: Path,
) -> None:
    config = _config(tmp_path)
    comms = _CaptureComms()
    agent = _bootstrap_agent(config)
    agent.team_comms = comms
    header = MessageHeader(
        from_agent_id="gateway",
        response_routing_key="gateway",
    )

    async def invoke(
        *,
        canonical_user_id: str,
        roles: list[str],
        payload: PinnedMemoryCommandPayload,
    ) -> ManagerMemoryCommand:
        request = ManagerMemoryCommand(command=payload)
        await handle_manager_memory_command(
            agent,
            command=request,
            msg_header=header,
            msg_meta=MessageMeta(
                gateway_context=_signed(
                    GatewayContext(
                        canonical_user_id=canonical_user_id,
                        session_key=f"session_{canonical_user_id}",
                    ),
                    team_id=config.gateway.team_id,
                    roles=roles,
                )
            ),
        )
        response = comms.messages[-1].msg_content
        assert isinstance(response, ManagerMemoryCommand)
        return response

    denied_create = await invoke(
        canonical_user_id="Member",
        roles=["member"],
        payload=PinnedMemoryCommandPayload(
            bank="coremem",
            action="create",
            fact_text="Team deploys after smoke tests.",
        ),
    )
    created = await invoke(
        canonical_user_id="Admin",
        roles=["admin"],
        payload=PinnedMemoryCommandPayload(
            bank="coremem",
            action="create",
            fact_text="Team deploys after smoke tests.",
        ),
    )
    listed = await invoke(
        canonical_user_id="Member",
        roles=["member"],
        payload=PinnedMemoryCommandPayload(bank="coremem", action="list"),
    )
    denied_delete = await invoke(
        canonical_user_id="Member",
        roles=["member"],
        payload=PinnedMemoryCommandPayload(
            bank="coremem",
            action="delete",
            memory_id=1,
        ),
    )

    assert denied_create.success is False
    assert denied_create.error_code == "authorization_denied"
    assert denied_create.rendered_result == "team_admin_required"
    assert created.success is True
    assert created.result is not None and created.result.fact is not None
    assert listed.success is True
    assert listed.result is not None
    assert [fact.fact_text for fact in listed.result.facts] == [
        "Team deploys after smoke tests."
    ]
    assert denied_delete.success is False
    assert denied_delete.error_code == "authorization_denied"
    assert denied_delete.rendered_result == "team_admin_required"


@pytest.mark.asyncio
async def test_authenticated_gateway_can_remove_owned_system_schedule(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = _config(tmp_path)
    comms = _CaptureComms()
    agent = _bootstrap_agent(config)
    store = ScheduleStore(storage_settings=config.manager_storage_settings)
    owner = config.user.canonical_user_id
    store.add(
        ScheduledMessage(
            schedule_id="hb_sched_manager_control",
            schedule_name="heartbeat:manager-control",
            management_class=ScheduleManagementClass.SYSTEM,
            system_schedule_kind=SystemScheduleKind.HEARTBEAT_LAUNCHER,
            claim_team_id=config.gateway.team_id,
            payload=Task(
                task_type=MessageMetaType.REQUEST,
                problem_statement="Launch heartbeat work.",
            ),
            payload_meta=MessageMeta(
                gateway_context=GatewayContext(
                    canonical_user_id=owner,
                    session_key="session_owner",
                )
            ),
            interval_ms=60_000,
        )
    )
    agent.team_comms = comms
    agent._ensure_schedule_store = lambda: store
    agent._scheduler_wake = SimpleNamespace(set=lambda: None)
    header = MessageHeader(from_agent_id="gateway", response_routing_key="gateway")
    # The manager decides with the verified roles who may change schedules the
    # runtime manages (the real decision is tested in
    # test_gateway_system_schedule_controls); administrators here.
    monkeypatch.setattr(
        manager_control_runtime,
        "_may_change_system_schedules",
        lambda _agent, person: "admin" in person.roles,
    )
    meta = MessageMeta(
        gateway_context=_signed(
            GatewayContext(canonical_user_id=owner, session_key="session_owner"),
            team_id=config.gateway.team_id,
            roles=["member"],
        )
    )
    # The signed person is a member, not an administrator: the manager refuses,
    # even though the person owns the schedule and the record is current.
    current = store.get_scheduled_message("hb_sched_manager_control")
    refused_command = ManagerScheduleCommand(
        action="remove",
        schedule_id="hb_sched_manager_control",
        expected_record_digest=contract_sha256(current.model_dump(mode="json")),
    )
    await handle_manager_schedule_command(
        agent, command=refused_command, msg_header=header, msg_meta=meta
    )
    refused = comms.messages[-1].msg_content
    assert refused.success is False
    assert refused.message == SYSTEM_SCHEDULE_CHANGE_NOT_ALLOWED
    assert store.get_scheduled_message("hb_sched_manager_control") is not None

    command = ManagerScheduleCommand(
        action="remove",
        schedule_id="hb_sched_manager_control",
        expected_record_digest="b" * 64,
    )
    admin_meta = MessageMeta(
        gateway_context=_signed(
            GatewayContext(canonical_user_id=owner, session_key="session_owner"),
            team_id=config.gateway.team_id,
            roles=["admin"],
        )
    )
    await handle_manager_schedule_command(
        agent,
        command=command,
        msg_header=MessageHeader(
            from_agent_id="gateway",
            response_routing_key="gateway",
        ),
        msg_meta=admin_meta,
    )

    stale_response = comms.messages[-1].msg_content
    assert isinstance(stale_response, ManagerScheduleCommand)
    assert stale_response.success is False
    assert stale_response.message == "schedule_changed"
    unchanged = store.get_scheduled_message("hb_sched_manager_control")
    assert unchanged is not None
    command.expected_record_digest = contract_sha256(unchanged.model_dump(mode="json"))
    await handle_manager_schedule_command(
        agent,
        command=command,
        msg_header=MessageHeader(
            from_agent_id="gateway",
            response_routing_key="gateway",
        ),
        msg_meta=admin_meta,
    )

    response = comms.messages[-1].msg_content
    assert isinstance(response, ManagerScheduleCommand)
    assert response.success is True
    assert response.message == "schedule_removed"
    assert store.get_scheduled_message("hb_sched_manager_control") is None


@needs_cedar
@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("authorization_enabled", "roles", "interruption", "allowed"),
    [
        (True, ["admin"], None, True),
        (True, ["member"], None, False),
        (False, ["member"], None, True),
        (True, ["admin"], "cancel", True),
        (True, ["admin"], "schedule_changed", True),
        (True, ["admin"], "decision_failed", True),
    ],
)
async def test_slow_schedule_permission_keeps_loop_responsive_and_precedes_mutation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    authorization_enabled: bool,
    roles: list[str],
    interruption: str | None,
    allowed: bool,
) -> None:
    from custom_agents.access_runtime import _runtime_access, runtime_access

    config = _config(tmp_path)
    agent = _bootstrap_agent(config)
    agent.authorization_config = {
        "enabled": authorization_enabled,
        "cedar_binary_path": str(CEDAR_BINARY),
    }
    store = ScheduleStore(storage_settings=config.manager_storage_settings)
    owner = config.user.canonical_user_id
    schedule = ScheduledMessage(
        schedule_id="slow_permission_schedule",
        management_class=ScheduleManagementClass.SYSTEM,
        system_schedule_kind=SystemScheduleKind.HEARTBEAT_LAUNCHER,
        claim_team_id=config.gateway.team_id,
        payload=Task(task_type=MessageMetaType.REQUEST, problem_statement="Heartbeat"),
        payload_meta=MessageMeta(gateway_context=GatewayContext(canonical_user_id=owner)),
        interval_ms=60_000,
    )
    store.add(schedule)
    agent.team_comms = _CaptureComms()
    agent._ensure_schedule_store = lambda: store
    agent._scheduler_wake = SimpleNamespace(set=lambda: None)
    person = manager_control_runtime.CommandPerson(owner, config.gateway.team_id, tuple(roles))
    decision = manager_control_runtime._may_change_system_schedules
    entered = threading.Event()
    release = threading.Event()

    def slow_decision(live_agent, verified_person):
        # Keep the real Cedar decision; hold its start to simulate slow storage
        # or evaluation and prove no mutation can precede its answer.
        assert live_agent is agent
        assert verified_person == person
        entered.set()
        assert release.wait(timeout=2), "Permission lookup blocked the manager loop"
        if interruption == "decision_failed":
            raise RuntimeError("Permission evaluator failed")
        return decision(live_agent, verified_person)

    task = None
    _runtime_access.cache_clear()
    try:
        assert decision(agent, person) is allowed
        monkeypatch.setattr(manager_control_runtime, "_may_change_system_schedules", slow_decision)
        task = asyncio.create_task(
            handle_manager_schedule_command(
                agent,
                command=ManagerScheduleCommand(
                    action="remove",
                    schedule_id=schedule.schedule_id,
                    expected_record_digest=contract_sha256(schedule.model_dump(mode="json")),
                ),
                msg_header=MessageHeader(from_agent_id="gateway", response_routing_key="gateway"),
                msg_meta=MessageMeta(gateway_context=_signed(
                    GatewayContext(canonical_user_id=owner),
                    team_id=config.gateway.team_id,
                    roles=roles,
                )),
            )
        )
        assert await asyncio.to_thread(entered.wait, 3)
        # Reaching this callback while the decision is held proves the loop
        # can continue handling timers and other incoming commands.
        assert not task.done()
        assert store.get_scheduled_message(schedule.schedule_id) is not None
        assert not agent.team_comms.messages
        if interruption == "cancel":
            task.cancel()
            await asyncio.sleep(0)
            assert not task.done(), "Cancellation must join the in-flight lookup"
        elif interruption == "schedule_changed":
            candidate = schedule.model_copy(update={"paused_until": "2030-01-01T00:00:00+00:00"})
            operation = manager_control_runtime.ScheduleMutationOperation.PAUSE
            permit = manager_control_runtime._authenticated_gateway_schedule_permit(
                schedule, principal=person, operation=operation, candidate=candidate
            )
            store.set_paused_until(
                schedule.schedule_id, candidate.paused_until,
                operation=operation, system_mutation_permit=permit,
            )
        release.set()
        if interruption == "cancel":
            with pytest.raises(asyncio.CancelledError):
                await task
            assert store.get_scheduled_message(schedule.schedule_id) is not None
            assert not agent.team_comms.messages
        elif interruption == "schedule_changed":
            from custom_agents.schedule_management import SystemScheduleMutationDenied

            with pytest.raises(SystemScheduleMutationDenied, match="current state"):
                await task
            assert store.get_scheduled_message(schedule.schedule_id).paused_until == candidate.paused_until
            assert not agent.team_comms.messages
        elif interruption == "decision_failed":
            with pytest.raises(RuntimeError, match="Permission evaluator failed"):
                await task
            assert store.get_scheduled_message(schedule.schedule_id) is not None
            assert not agent.team_comms.messages
        else:
            await task
            response = agent.team_comms.messages[-1].msg_content
            assert response.success is allowed
            assert response.message == (
                "schedule_removed" if allowed else SYSTEM_SCHEDULE_CHANGE_NOT_ALLOWED
            )
            assert (store.get_scheduled_message(schedule.schedule_id) is None) is allowed
    finally:
        release.set()
        if task is not None:
            await asyncio.gather(task, return_exceptions=True)
        access = runtime_access(agent)
        if access.service is not None:
            access.service.cedar.close()
        _runtime_access.cache_clear()


@pytest.mark.asyncio
async def test_task_progress_projection_is_owner_session_and_conversation_scoped(
    tmp_path,
) -> None:
    task = Task(
        task_id="task_root",
        task_type=MessageMetaType.REQUEST,
        problem_statement="Compare cars",
    )
    task.task_state = [(ProgressState.IN_PROGRESS, "2026-07-14T12:00:00+00:00")]
    info = SimpleNamespace(
        task=task,
        state=ProgressState.IN_PROGRESS,
        owner_person_id="Owner",  # recorded at admission (TaskInfo)
        meta=MessageMeta(
            conversation_id="conv_owner",
            gateway_context=GatewayContext(
                canonical_user_id="Owner", session_key="session_owner"
            ),
        ),
    )
    manager = SimpleNamespace(
        id="manager_1",
        name="Manager",
        team_id="team_1",
        task_manager=SimpleNamespace(
            operations_runtime=TaskOperationRuntime(),
            storage_settings=resolve_storage_settings(
                {"backend": "sqlite", "sqlite": {"path": str(tmp_path / "progress.db")}}
            ),
            get_top_level_task_infos=lambda: [info],
            get_task_plan_id=lambda _task_id: None,
            get_plan=lambda _plan_id: None,
            get_subtasks=lambda _task_id: [],
        ),
    )
    request = TaskProgressStatus(
        status_type=MessageMetaType.REQUEST,
        canonical_user_id="Owner",
        session_key="session_owner",
        conversation_id="conv_owner",
    )
    meta = MessageMeta(
        conversation_id="conv_owner",
        gateway_context=GatewayContext(
            canonical_user_id="Owner", session_key="session_owner"
        ),
    )

    matching = await build_task_progress_response(manager, request, meta)
    other_session = await build_task_progress_response(
        manager,
        request.model_copy(update={"session_key": "session_other"}),
        meta.model_copy(
            update={
                "gateway_context": GatewayContext(
                    canonical_user_id="Owner",
                    session_key="session_other",
                )
            }
        ),
    )

    assert matching.status == "active"
    assert matching.root_task_id == "task_root"
    assert other_session.status == "idle"


@pytest.mark.asyncio
async def test_runtime_work_command_cancels_the_complete_owned_execution_graph(
    tmp_path: Path,
) -> None:
    config = _config(tmp_path)
    task_manager = TaskManager(
        storage_settings=config.manager_storage_settings,
        logger=logging.getLogger("test.runtime_work_control"),
    )
    task_manager.set_owner_identity(
        "manager_1",
        "Manager",
        config.gateway.team_id,
        config.gateway.named_agent_type,
    )
    owner_context = GatewayContext(
        canonical_user_id="Owner",
        session_key="session_owner",
    )
    root = Task(
        task_id="task_root",
        task_type=MessageMetaType.REQUEST,
        problem_statement="Complete the owned work.",
    )
    assert task_manager.add_task(
        root,
        MessageHeader(from_agent_id="gateway", response_routing_key="gateway"),
        MessageMeta(
            conversation_id="conv_owner",
            gateway_context=owner_context,
        ),
        owner_person_id="Owner",
    )
    task_manager.update_main_task_state(
        root.task_id,
        ProgressState.IN_PROGRESS,
        "2026-08-23T12:00:00+00:00",
    )
    child = Task(
        task_id="task_child",
        parent_task_id=root.task_id,
        task_type=MessageMetaType.REQUEST,
        problem_statement="Delegated work.",
    )
    assert task_manager.add_subtask(
        root.task_id,
        child,
        MessageHeader(
            from_agent_id="manager_1",
            to_routing_path="worker_exact",
        ),
        MessageMeta(
            to_agent_name="worker_exact",
            to_named_agent_type="WebResearchAgent",
            conversation_id="conv_owner",
            gateway_context=owner_context,
        ),
    )
    other = Task(
        task_id="task_other_user",
        task_type=MessageMetaType.REQUEST,
        problem_statement="Other user's work.",
    )
    assert task_manager.add_task(
        other,
        MessageHeader(from_agent_id="gateway", response_routing_key="gateway"),
        MessageMeta(
            conversation_id="conv_other",
            gateway_context=GatewayContext(
                canonical_user_id="Other",
                session_key="session_other",
            ),
        ),
        owner_person_id="Other",
    )

    comms = _CaptureComms()
    agent = SimpleNamespace(
        id="manager_1",
        name="Manager",
        is_team_lead=True,
        team_id=config.gateway.team_id,
        task_manager=task_manager,
        team_comms=comms,
        named_agent_type_queue_map={
            "WebResearchAgent": "team_control_WebResearchAgent"
        },
        _agent_id_by_name={"worker_exact": "worker_exact"},
        _stop_reasons_by_task_id={},
        _active_execution_task=None,
        _active_execution_task_id=None,
        _strands_agent=SimpleNamespace(),
        _logger=logging.getLogger("test.runtime_work_control"),
        _ensure_schedule_store=lambda: ScheduleStore(
            storage_settings=config.manager_storage_settings
        ),
        _finalize_conversation_turn=AsyncMock(return_value=None),
    )
    command = ManagerRuntimeWorkCommand(
        action="cancel",
        scope="task",
        target_task_id=child.task_id,
        reason="Operator recovery stop.",
    )

    await handle_manager_runtime_work_command(
        agent,
        command=command,
        msg_header=MessageHeader(
            from_agent_id="gateway",
            response_routing_key="gateway",
        ),
        msg_meta=MessageMeta(
            conversation_id="conv_owner",
            gateway_context=_signed(owner_context, team_id=config.gateway.team_id, roles=["admin"]),
        ),
    )

    response = comms.messages[-1].msg_content
    assert isinstance(response, ManagerRuntimeWorkCommand)
    assert response.success is True
    assert response.message == "runtime_work_cancelled"
    assert len(response.cancellation_receipts) == 1
    receipt = response.cancellation_receipts[0]
    assert receipt.root_task_id == root.task_id
    assert receipt.cancelled_task_count == 2
    assert receipt.stop_signals_sent == 1
    assert receipt.unconfirmed_worker_count == 1
    assert task_manager.get_task_info(root.task_id) is None
    assert task_manager.get_task_info(child.task_id) is None
    assert task_manager.get_task_info(other.task_id) is not None
    assert task_manager.is_execution_cancelled(root.task_id)
    assert task_manager.is_execution_cancelled(child.task_id)
    stop_signals = [
        message.msg_content
        for message in comms.messages
        if isinstance(message.msg_content, ControlInstruction)
    ]
    assert [signal.target_task_id for signal in stop_signals] == [child.task_id]


@pytest.mark.asyncio
async def test_authenticated_objective_pause_disables_future_system_schedule(
    tmp_path: Path,
) -> None:
    config = _config(tmp_path)
    task_manager = TaskManager(
        storage_settings=config.manager_storage_settings,
        logger=logging.getLogger("test.objective_operator_control"),
    )
    task_manager.set_owner_identity(
        "manager_1",
        "Manager",
        config.gateway.team_id,
        config.gateway.named_agent_type,
    )
    owner_context = GatewayContext(
        canonical_user_id="Owner",
        session_key="session_owner",
    )
    source = Task(
        task_id="task_objective_source",
        task_type=MessageMetaType.REQUEST,
        problem_statement="Create durable work.",
    )
    assert task_manager.add_task(
        source,
        MessageHeader(from_agent_id="gateway"),
        MessageMeta(
            conversation_id="conv_owner",
            gateway_context=owner_context,
        ),
        owner_person_id="Owner",
    )
    objective = task_manager.create_objective(
        task_id=source.task_id,
        title="Durable operator-controlled work",
        desired_outcome="Run until explicitly paused.",
        why="This work is scheduled over time.",
        horizon=ObjectiveHorizon.RECURRING,
    )
    assert objective is not None
    assert objective.owner_canonical_user_id == "Owner"
    schedule_store = ScheduleStore(storage_settings=config.manager_storage_settings)
    schedule_store.add(
        ScheduledMessage(
            schedule_id="objective_system_schedule",
            schedule_name="objective:operator-control",
            objective_id=objective.objective_id,
            management_class=ScheduleManagementClass.SYSTEM,
            system_schedule_kind=SystemScheduleKind.OBJECTIVE_WORK_SESSION,
            claim_team_id=config.gateway.team_id,
            claim_named_agent_type=config.gateway.named_agent_type,
            payload=Task(
                task_type=MessageMetaType.REQUEST,
                problem_statement="Advance the Objective.",
            ),
            payload_meta=MessageMeta(gateway_context=owner_context),
            interval_ms=60_000,
        )
    )
    task_manager.link_schedule_to_objective(
        objective.objective_id,
        "objective_system_schedule",
        managed_by_objective=True,
    )
    comms = _CaptureComms()
    wake = SimpleNamespace(set=lambda: None)
    agent = SimpleNamespace(
        id="manager_1",
        name="Manager",
        is_team_lead=True,
        team_id=config.gateway.team_id,
        task_manager=task_manager,
        team_comms=comms,
        _logger=logging.getLogger("test.objective_operator_control"),
        _ensure_schedule_store=lambda: schedule_store,
        _scheduler_wake=wake,
    )
    command = ManagerRuntimeWorkCommand(
        action="pause_objective",
        scope="objective",
        target_objective_id=objective.objective_id,
        expected_objective_digest="b" * 64,
    )

    await handle_manager_runtime_work_command(
        agent,
        command=command,
        msg_header=MessageHeader(
            from_agent_id="gateway",
            response_routing_key="gateway",
        ),
        msg_meta=MessageMeta(gateway_context=_signed(owner_context, team_id=config.gateway.team_id, roles=["admin"])),
    )

    stale_response = comms.messages[-1].msg_content
    assert isinstance(stale_response, ManagerRuntimeWorkCommand)
    assert stale_response.success is False
    assert stale_response.message == "objective_changed"
    unchanged_schedule = schedule_store.get_scheduled_message(
        "objective_system_schedule"
    )
    assert unchanged_schedule is not None
    assert unchanged_schedule.enabled is True

    command.expected_objective_digest = contract_sha256(
        task_manager.get_objective(objective.objective_id).model_dump(mode="json")
    )
    await handle_manager_runtime_work_command(
        agent,
        command=command,
        msg_header=MessageHeader(
            from_agent_id="gateway",
            response_routing_key="gateway",
        ),
        msg_meta=MessageMeta(gateway_context=_signed(owner_context, team_id=config.gateway.team_id, roles=["admin"])),
    )

    response = comms.messages[-1].msg_content
    assert isinstance(response, ManagerRuntimeWorkCommand)
    assert response.success is True
    assert response.objective_status == ObjectiveStatus.PAUSED
    refreshed_objective = task_manager.get_objective(objective.objective_id)
    assert refreshed_objective is not None
    assert refreshed_objective.status == ObjectiveStatus.PAUSED
    assert refreshed_objective.schedule_links[0].disabled_by_objective_pause is True
    refreshed_schedule = schedule_store.get_scheduled_message(
        "objective_system_schedule"
    )
    assert refreshed_schedule is not None
    assert refreshed_schedule.enabled is False


@pytest.mark.asyncio
async def test_user_lists_and_deletes_objective_orphaned_by_legacy_manager(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = _config(tmp_path)
    legacy_owner = "rosy-stem-ee07"

    class LegacyTaskManager(TaskManager):
        def durable_manager_scope_id(self) -> str:
            return legacy_owner

    original = LegacyTaskManager(
        storage_settings=config.manager_storage_settings,
        logger=logging.getLogger("test.objective_orphan_original"),
    )
    original.set_owner_identity(
        legacy_owner,
        "Original Manager",
        config.gateway.team_id,
        config.gateway.named_agent_type,
    )
    owner_context = GatewayContext(
        canonical_user_id="Owner",
        session_key="session_owner",
    )
    source = Task(
        task_id="task_legacy_objective",
        task_type=MessageMetaType.REQUEST,
        problem_statement="Create work that survives manager replacement.",
    )
    assert original.add_task(
        source,
        MessageHeader(from_agent_id="gateway"),
        MessageMeta(
            conversation_id="conv_owner",
            gateway_context=owner_context,
        ),
        owner_person_id="Owner",
    )
    objective = original.create_objective(
        task_id=source.task_id,
        title="Recover legacy Objective custody",
        desired_outcome="The authenticated owner can delete it after restart.",
        why="Manager process identity must not strand user-owned work.",
        horizon=ObjectiveHorizon.LONG_HORIZON,
    )
    assert objective is not None
    objective = original.link_schedule_to_objective(
        objective.objective_id,
        "legacy_schedule_already_removed",
    )
    assert objective is not None
    objective = original.link_heartbeat_to_objective(
        objective.objective_id,
        "legacy_heartbeat_already_removed",
    )
    assert objective is not None
    plan = original.get_plan(objective.current_plan_id or "")
    assert plan is not None
    objective.team_id = None
    plan.team_id = None
    with connect_sqlite(config.manager_storage_settings.sqlite_path or "") as conn:
        conn.execute(
            "UPDATE objectives SET team_id=NULL, objective_data=? WHERE objective_id=?",
            (objective.model_dump_json(), objective.objective_id),
        )
        conn.execute(
            "UPDATE plans SET team_id=NULL, plan_data=? WHERE plan_id=?",
            (plan.model_dump_json(), plan.plan_id),
        )

    replacement = TaskManager(
        storage_settings=config.manager_storage_settings,
        logger=logging.getLogger("test.objective_orphan_replacement"),
    )
    replacement.set_owner_identity(
        "manager_after_restart",
        "Replacement Manager",
        config.gateway.team_id,
        config.gateway.named_agent_type,
    )
    assert replacement.get_objective(objective.objective_id) is None
    replacement.update_main_task_state(
        source.task_id,
        ProgressState.COMPLETED,
        "2026-08-24T12:00:00+00:00",
    )
    comms = _CaptureComms()
    schedule_store = ScheduleStore(storage_settings=config.manager_storage_settings)
    agent = SimpleNamespace(
        id="manager_after_restart",
        name="Replacement Manager",
        is_team_lead=True,
        team_id=config.gateway.team_id,
        task_manager=replacement,
        team_comms=comms,
        _logger=logging.getLogger("test.objective_orphan_control"),
        _ensure_schedule_store=lambda: schedule_store,
        _scheduler_wake=SimpleNamespace(set=lambda: None),
    )
    header = MessageHeader(from_agent_id="gateway", response_routing_key="gateway")
    meta = MessageMeta(gateway_context=_signed(owner_context, team_id=config.gateway.team_id, roles=["admin"]))

    await handle_manager_runtime_work_command(
        agent,
        command=ManagerRuntimeWorkCommand(
            action="list_objectives",
            scope="objective",
        ),
        msg_header=header,
        msg_meta=meta,
    )

    listed = comms.messages[-1].msg_content
    assert isinstance(listed, ManagerRuntimeWorkCommand)
    assert [item.objective_id for item in listed.objectives] == [objective.objective_id]
    durable_owner = replacement.durable_manager_scope_id()
    adopted = replacement.get_objective(objective.objective_id)
    adopted_plan = replacement.get_plan(plan.plan_id)
    assert adopted is not None
    assert adopted.owner_agent_id == durable_owner
    assert adopted.team_id == config.gateway.team_id
    assert adopted_plan is not None
    assert adopted_plan.owner_agent_id == durable_owner

    await handle_manager_runtime_work_command(
        agent,
        command=ManagerRuntimeWorkCommand(
            action="delete_objective",
            scope="objective",
            target_objective_id=objective.objective_id,
            expected_objective_digest=listed.objectives[0].record_digest,
        ),
        msg_header=header,
        msg_meta=meta,
    )

    deleted = comms.messages[-1].msg_content
    assert isinstance(deleted, ManagerRuntimeWorkCommand)
    assert deleted.success is True
    assert deleted.objective_status == ObjectiveStatus.DELETED
    tombstone = replacement.get_objective(objective.objective_id)
    assert tombstone is not None
    assert tombstone.deleted_at is not None
    assert tombstone.deletion_summary is not None
    assert tombstone.schedule_links[0].disabled_by_objective_pause is True
    assert tombstone.heartbeat_links[0].disabled_by_objective_pause is True
    assert replacement.get_plan(plan.plan_id) is not None


def test_gateway_runtime_has_no_manager_repository_imports() -> None:
    gateway_root = Path(__file__).parents[1] / "assistant_gateway"
    gateway_sources = {
        path.relative_to(gateway_root): path.read_text(encoding="utf-8")
        for path in gateway_root.rglob("*.py")
    }
    forbidden = (
        "custom_agents.conversation_store",
        "custom_agents.heartbeat_store",
        "custom_agents.schedule_store",
        "storage.pinned_memory_repository",
        "storage.runtime_state_repository",
        "storage.task_archive_repository",
        "storage.plan_repository",
    )
    violations = {
        str(path): module
        for path, source in gateway_sources.items()
        for module in forbidden
        if module in source
    }
    assert violations == {}


@pytest.mark.asyncio
@pytest.mark.parametrize("fail_on_metadata", [False, True], ids=["deleted-status", "deletion-metadata"])
async def test_objective_deletion_that_was_not_saved_is_not_reported_done(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, fail_on_metadata: bool
) -> None:
    config = _config(tmp_path)
    task_manager = TaskManager(
        storage_settings=config.manager_storage_settings,
        logger=logging.getLogger("test.objective_deletion_not_saved"),
    )
    task_manager.set_owner_identity("manager_1", "Manager", config.gateway.team_id, config.gateway.named_agent_type)
    owner_context = GatewayContext(canonical_user_id="Owner", session_key="session_owner")
    source = Task(task_id="task_deletion_source", task_type=MessageMetaType.REQUEST, problem_statement="Work.")
    assert task_manager.add_task(
        source,
        MessageHeader(from_agent_id="gateway"),
        MessageMeta(conversation_id="conv_owner", gateway_context=owner_context),
        owner_person_id="Owner",
    )
    objective = task_manager.create_objective(
        task_id=source.task_id, title="Delete me", desired_outcome="Deleted.", why="Asked."
    )
    assert objective is not None
    # The deletion starts (DELETING is saved), but its final save fails.
    save = task_manager.objective_repository.save_objective

    def fail_when_deleted(record) -> None:
        if record.status == ObjectiveStatus.DELETED and (not fail_on_metadata or record.deleted_at is not None):
            raise sqlite3.OperationalError("disk I/O error")
        save(record)

    monkeypatch.setattr(task_manager.objective_repository, "save_objective", fail_when_deleted)
    comms = _CaptureComms()
    schedule_store = ScheduleStore(storage_settings=config.manager_storage_settings)
    agent = SimpleNamespace(
        id="manager_1",
        name="Manager",
        is_team_lead=True,
        team_id=config.gateway.team_id,
        task_manager=task_manager,
        team_comms=comms,
        _logger=logging.getLogger("test.objective_deletion_not_saved"),
        _ensure_schedule_store=lambda: schedule_store,
        _scheduler_wake=SimpleNamespace(set=lambda: None),
    )

    await handle_manager_runtime_work_command(
        agent,
        command=ManagerRuntimeWorkCommand(
            action="delete_objective",
            scope="objective",
            target_objective_id=objective.objective_id,
            expected_objective_digest=contract_sha256(
                task_manager.get_objective(objective.objective_id).model_dump(mode="json")
            ),
        ),
        msg_header=MessageHeader(from_agent_id="gateway", response_routing_key="gateway"),
        msg_meta=MessageMeta(gateway_context=_signed(owner_context, team_id=config.gateway.team_id, roles=["admin"])),
    )

    response = comms.messages[-1].msg_content
    assert isinstance(response, ManagerRuntimeWorkCommand)
    assert (response.success, response.message) == (False, "objective_update_failed")
    assert response.objective_status == ObjectiveStatus.DELETING
    assert task_manager.get_objective(objective.objective_id).status == ObjectiveStatus.DELETING

    # A failed completion stays retryable, including when the write containing
    # its completion metadata fails after earlier deletion steps succeeded.
    monkeypatch.setattr(task_manager.objective_repository, "save_objective", save)
    retry = response.model_copy(
        update={
            "command_type": MessageMetaType.REQUEST,
            "expected_objective_digest": contract_sha256(
                task_manager.get_objective(objective.objective_id).model_dump(mode="json")
            ),
        }
    )
    await handle_manager_runtime_work_command(
        agent,
        command=retry,
        msg_header=MessageHeader(from_agent_id="gateway", response_routing_key="gateway"),
        msg_meta=MessageMeta(gateway_context=_signed(owner_context, team_id=config.gateway.team_id, roles=["admin"])),
    )
    completed = comms.messages[-1].msg_content
    assert (completed.success, completed.message, completed.objective_status) == (
        True, "objective_deleted", ObjectiveStatus.DELETED
    )
    stored = task_manager.get_objective(objective.objective_id)
    assert stored.status == ObjectiveStatus.DELETED
    assert stored.deleted_at is not None
    assert "Future execution is disabled" in stored.deletion_summary


# -- each Objective control is one task operation on the stored record ---------------


def _owned_objective_with_schedule(tmp_path: Path):
    config = _config(tmp_path)
    task_manager = TaskManager(
        storage_settings=config.manager_storage_settings,
        logger=logging.getLogger("test.objective_control_operation"),
    )
    task_manager.set_owner_identity("manager_1", "Manager", config.gateway.team_id, config.gateway.named_agent_type)
    owner_context = GatewayContext(canonical_user_id="Owner", session_key="session_owner")
    source = Task(task_id="task_control_source", task_type=MessageMetaType.REQUEST, problem_statement="Work.")
    assert task_manager.add_task(
        source,
        MessageHeader(from_agent_id="gateway"),
        MessageMeta(conversation_id="conv_owner", gateway_context=owner_context),
        owner_person_id="Owner",
    )
    objective = task_manager.create_objective(
        task_id=source.task_id,
        title="Check every minute",
        desired_outcome="Checked.",
        why="Asked.",
        horizon=ObjectiveHorizon.RECURRING,
    )
    schedule_store = ScheduleStore(storage_settings=config.manager_storage_settings)
    schedule_store.add(
        ScheduledMessage(
            schedule_id="objective_owner_schedule",
            schedule_name="objective:owner",
            objective_id=objective.objective_id,
            payload=Task(task_type=MessageMetaType.REQUEST, problem_statement="Check."),
            payload_meta=MessageMeta(gateway_context=owner_context),
            interval_ms=60_000,
        )
    )
    task_manager.link_schedule_to_objective(objective.objective_id, "objective_owner_schedule")
    agent = SimpleNamespace(
        id="manager_1",
        task_manager=task_manager,
        _logger=logging.getLogger("test.objective_control_operation"),
        _ensure_schedule_store=lambda: schedule_store,
        _scheduler_wake=SimpleNamespace(set=lambda: None),
    )
    return task_manager, schedule_store, objective, agent


async def _owner_control(
    agent, objective_id: str, action: str, *, digest: str | None = None, command_id: str | None = None
):
    command = ManagerRuntimeWorkCommand(
        action=action, scope="objective", target_objective_id=objective_id, expected_objective_digest=digest
    )
    if command_id:
        command.command_id = command_id
    response = command.model_copy(deep=True)
    await manager_control_runtime._apply_objective_control(
        agent,
        command=command,
        person=manager_control_runtime.CommandPerson("Owner", agent.task_manager.identity.team_id, ("owner",)),
        response=response,
    )
    return response


@pytest.mark.asyncio
async def test_objective_control_refuses_an_action_the_status_does_not_offer(tmp_path: Path) -> None:
    task_manager, schedule_store, objective, agent = _owned_objective_with_schedule(tmp_path)
    task_manager.update_objective_status(objective.objective_id, ObjectiveStatus.SATISFIED, summary="Done.")
    before = task_manager.get_objective(objective.objective_id)

    response = await _owner_control(agent, objective.objective_id, "resume_objective")

    assert (response.success, response.message) == (False, "objective_action_unavailable:satisfied")
    # Nothing changed, so the reply names no status the gateway would report as new.
    assert response.objective_status is None
    assert task_manager.get_objective(objective.objective_id) == before


@pytest.mark.asyncio
async def test_objective_control_keeps_changes_stored_while_its_links_switch(tmp_path: Path) -> None:
    task_manager, schedule_store, objective, agent = _owned_objective_with_schedule(tmp_path)
    switch = schedule_store.set_enabled

    def switch_while_another_change_lands(*args, **kwargs):
        task_manager.update_objective_progress(objective.objective_id, progress_summary="Half checked.")
        return switch(*args, **kwargs)

    schedule_store.set_enabled = switch_while_another_change_lands

    response = await _owner_control(agent, objective.objective_id, "pause_objective")

    assert (response.success, response.message) == (True, "objective_paused")
    stored = task_manager.get_objective(objective.objective_id)
    assert stored.status == ObjectiveStatus.PAUSED
    assert stored.progress_summary == "Half checked."
    assert stored.schedule_links[0].disabled_by_objective_pause is True
    assert schedule_store.get_scheduled_message("objective_owner_schedule").enabled is False


@pytest.mark.asyncio
async def test_objective_control_checks_and_changes_in_one_task_operation(tmp_path: Path) -> None:
    """The digest and allowed-action checks see the record the change starts from."""
    task_manager, _schedule_store, objective, agent = _owned_objective_with_schedule(tmp_path)
    digest = contract_sha256(task_manager.get_objective(objective.objective_id).model_dump(mode="json"))
    operations: list[str] = []
    run = task_manager.operations_runtime.run

    async def record(operation, *args, **kwargs):
        target = getattr(operation, "func", operation)
        operations.append(getattr(target, "__name__", str(target)))
        return await run(operation, *args, **kwargs)

    task_manager.operations_runtime.run = record

    response = await _owner_control(agent, objective.objective_id, "pause_objective", digest=digest)

    assert (response.success, response.message) == (True, "objective_paused")
    assert operations == ["_control_objective_in_operation"]


def _fail_first_schedule_switch(schedule_store) -> None:
    """The first schedule switch fails (as when the database is busy); later ones work."""
    switch = schedule_store.set_enabled
    calls: list[str] = []

    def fail_first(schedule_id, *args, **kwargs):
        calls.append(schedule_id)
        if len(calls) == 1:
            return False
        return switch(schedule_id, *args, **kwargs)

    schedule_store.set_enabled = fail_first


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("action", "stored_status"),
    [("pause_objective", ObjectiveStatus.PAUSED), ("abandon_objective", ObjectiveStatus.ABANDONED)],
)
async def test_an_incomplete_pause_or_abandon_can_be_retried(
    tmp_path: Path, action: str, stored_status: ObjectiveStatus
) -> None:
    """The status is stored before a schedule fails to switch, so the retry finds it set.

    Paused and abandoned Objectives do not offer pause or abandon, and the
    retry used to be refused while the schedule kept running.
    """
    task_manager, schedule_store, objective, agent = _owned_objective_with_schedule(tmp_path)
    task_manager.complete_task("task_control_source", "2026-10-17T09:00:00+00:00")
    _fail_first_schedule_switch(schedule_store)

    first = await _owner_control(agent, objective.objective_id, action)

    assert (first.success, first.message) == (
        False,
        "objective_control_incomplete:schedule:objective_owner_schedule",
    )
    assert first.objective_status == stored_status
    assert schedule_store.get_scheduled_message("objective_owner_schedule").enabled is True
    stored = task_manager.get_objective(objective.objective_id)
    assert stored.schedule_links[0].disabled_by_objective_pause is False
    events_before = len(stored.event_log)

    retried = await _owner_control(agent, objective.objective_id, action)

    assert (retried.success, retried.message) == (True, f"objective_{stored_status.value}")
    assert schedule_store.get_scheduled_message("objective_owner_schedule").enabled is False
    stored = task_manager.get_objective(objective.objective_id)
    assert stored.status == stored_status
    assert stored.schedule_links[0].disabled_by_objective_pause is True
    assert len(stored.event_log) == events_before


@pytest.mark.asyncio
async def test_an_incomplete_deletion_waits_and_keeps_its_first_operation_on_retry(tmp_path: Path) -> None:
    task_manager, schedule_store, objective, agent = _owned_objective_with_schedule(tmp_path)
    task_manager.complete_task("task_control_source", "2026-10-17T09:00:00+00:00")
    _fail_first_schedule_switch(schedule_store)

    first = await _owner_control(agent, objective.objective_id, "delete_objective", command_id="cmd_delete_first")

    assert (first.success, first.message) == (
        False,
        "objective_deletion_incomplete:schedule:objective_owner_schedule",
    )
    assert first.objective_status == ObjectiveStatus.DELETING
    waiting = task_manager.get_objective(objective.objective_id)
    assert waiting.status == ObjectiveStatus.DELETING
    assert waiting.deletion_operation_id == "cmd_delete_first"
    assert waiting.deletion_requested_at is not None
    assert waiting.deletion_summary == (
        "Deletion is waiting for controls to finish: schedule:objective_owner_schedule"
    )

    retried = await _owner_control(agent, objective.objective_id, "delete_objective", command_id="cmd_delete_retry")

    assert (retried.success, retried.message) == (True, "objective_deleted")
    deleted = task_manager.get_objective(objective.objective_id)
    assert deleted.status == ObjectiveStatus.DELETED
    assert deleted.deletion_operation_id == "cmd_delete_first"
    assert deleted.deletion_requested_at == waiting.deletion_requested_at
    assert deleted.deleted_at is not None
    assert schedule_store.get_scheduled_message("objective_owner_schedule").enabled is False
