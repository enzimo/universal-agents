"""Access control (A1, C03) and authorization-off (A7, C04) tests for ``wait`` and ``long_wait``.

Uses the real Cedar evaluator (``target/debug/ua-cedar-authorizer``), real
SQLite access/schedule stores, real ``AccessDecisionHook``, real
``ScheduleWorkerService``, and real catalog hydration / ``startup_tools``.
"""

from __future__ import annotations

import asyncio
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from strands.interrupt import _InterruptState
from strands.types.tools import ToolContext

from access_catalog.catalog import load_catalog
from access_control.cedar import CedarAccessClient
from access_control.decisions import AccessService, HandDownRefused
from access_control.entities import AgentPrincipal, PersonPrincipal, agent_role_resolver
from access_control.inbox import StoredAccessInbox
from access_control.requests import AccessRequests
from access_control.starter_profiles import apply_starter_profile
from config_loader import Config
from custom_agents import access_runtime, timed_waits as timed_waits_rules
from custom_agents.access_runtime import AccessDecisionHook, decide_call
from custom_agents.agent_catalog import _hydrate_tools, load_agent_catalog
from custom_agents.agent_instructions import shared_agent_base_prompt, strip_access_prompt
from custom_agents.schedule_changes import WorkerConfigurationRequestV1
from custom_agents.schedule_store import ScheduleStore
from custom_agents.schedule_worker_service import ScheduleWorkerService
from custom_agents.system_schedule_changes import configure_worker
from custom_agents.task_manager import TaskManager
from custom_agents.timed_waits import (
    LONG_WAIT_TOOL_ID,
    LONG_WAIT_TOOL_NAME,
    TIMED_WAIT_STARTED_EVENT,
    WAIT_TOOL_ID,
    WAIT_TOOL_NAME,
)
from custom_agents.tool_sync import startup_tools
from custom_messages.contracts.access_v2 import (
    AccessLevel,
    AccessRequestStatus,
    AccessReviewChoice,
    GrantLifetime,
    GrantV1,
    SubjectKind,
    SubjectRefV1,
    TargetKind,
    TargetRefV1,
)
from custom_messages.contracts.routing_v1 import GatewayContext, MessageHeader, MessageMeta
from custom_messages.contracts.schedule_v1 import ScheduledMessage
from custom_messages.contracts.schedule_worker_v1 import (
    ScheduleCapabilityRequirementsV1,
    ScheduleWorkerPolicySpecV1,
    utc_now as schedule_utc_now,
)
from custom_messages.contracts.task_v1 import Task
from custom_messages.enums import (
    MessageMetaType,
    ProgressState,
    ScheduleManagementClass,
    TaskHistoryImportance,
    TaskHistoryType,
)
from custom_tools.provider_contract import tool_name
from custom_tools.providers.timed_waits import long_wait, wait
from custom_tools.runtime_context import set_agent_ref, task_work
from runtime_settings.timed_waits import TimedWaitLimits
from storage.access_repository import AccessRepository
from storage.config import resolve_storage_settings
from tests.broker_access_support import (
    forget_task_context_keys,
    signed_scheduler_token,
    signed_task_token,
    use_broker_authority_verifier,
    use_scheduler_key,
)
from tests.task_work_support import bind_task

ROOT = Path(__file__).resolve().parents[1]
BINARY = ROOT / "target" / "debug" / "ua-cedar-authorizer"
TEAM = "team-timed-wait"
OWNER = PersonPrincipal("usr_owner", TEAM, ("owner", "admin"))
MEMBER = PersonPrincipal("usr_member", TEAM, ("member",))
BASE_TIME = datetime(2026, 11, 26, 12, 0, 0, tzinfo=timezone.utc)

needs_cedar = pytest.mark.skipif(
    not BINARY.is_file(),
    reason="build ua-cedar-authorizer first (cargo build)",
)


@pytest.fixture
def storage(tmp_path: Path):
    forget_task_context_keys()
    use_broker_authority_verifier()
    access_runtime._runtime_access.cache_clear()
    yield resolve_storage_settings({"backend": "sqlite", "sqlite": {"path": str(tmp_path / "manager.db")}})
    access_runtime._runtime_access.cache_clear()
    forget_task_context_keys()


def _make_access_agent(
    storage: dict[str, Any],
    *,
    agent_type: str = "WebResearchAgent",
    lead: bool = False,
    role: str = "worker",
    person: str = OWNER.person_id,
    person_roles: tuple[str, ...] = ("owner", "admin"),
    enabled: bool = True,
    task_id: str = "task-1",
    signed_context: Any | None = None,
) -> Any:
    if signed_context is None:
        signed_context = signed_task_token(person, team_id=TEAM, roles=person_roles, root_task_id=task_id)
    meta = SimpleNamespace(gateway_context=SimpleNamespace(task_context=signed_context), conversation_id="conv-1")
    task_manager = SimpleNamespace(
        storage_settings=storage,
        current_task_id=task_id,
        get_task_info=lambda _task_id: SimpleNamespace(meta=meta),
        terminal_callbacks=[],
        release_callbacks=[],
    )
    task_manager.add_task_terminal_callback = task_manager.terminal_callbacks.append
    task_manager.add_task_release_callback = task_manager.release_callbacks.append
    bind_task(task_id)
    return SimpleNamespace(
        id=f"agent-{agent_type}",
        named_agent_type=agent_type,
        team_id=TEAM,
        is_team_lead=lead,
        access_role=role,
        authorization_config={"enabled": enabled, "cedar_binary_path": str(BINARY)},
        task_manager=task_manager,
        instance_root=None,
    )


def _service(storage: dict[str, Any]) -> AccessService:
    return AccessService(
        store=AccessRepository(storage),
        catalog=load_catalog(None),
        cedar=CedarAccessClient(binary=str(BINARY)),
        team_id=TEAM,
    )


# -- A1: real Cedar decisions & AccessDecisionHook ------------------------------------------


@needs_cedar
def test_a1_worker_may_call_wait_and_hook_refuses_long_wait_until_granted(storage: dict[str, Any]) -> None:
    worker = _make_access_agent(storage)

    wait_decision = decide_call(worker, WAIT_TOOL_NAME, {"minutes": 5, "reason": "redeploy"})
    assert wait_decision is not None and wait_decision.allowed
    assert wait_decision.tool_id == WAIT_TOOL_ID and wait_decision.level is AccessLevel.READ

    refused = decide_call(worker, LONG_WAIT_TOOL_NAME, {"minutes": 60, "reason": "redeploy"})
    assert refused is not None and not refused.allowed
    assert refused.tool_id == LONG_WAIT_TOOL_ID and refused.level is AccessLevel.WRITE

    hook = AccessDecisionHook(worker)
    registered = object()
    refused_event = SimpleNamespace(
        tool_use={"name": LONG_WAIT_TOOL_NAME, "input": {"minutes": 60, "reason": "redeploy"}},
        cancel_tool=False,
        selected_tool=registered,
    )
    allowed_event = SimpleNamespace(
        tool_use={"name": WAIT_TOOL_NAME, "input": {"minutes": 5, "reason": "redeploy"}},
        cancel_tool=False,
        selected_tool=registered,
    )
    asyncio.run(hook.before_tool_call(refused_event))
    asyncio.run(hook.before_tool_call(allowed_event))

    payload = json.loads(refused_event.cancel_tool)
    assert payload["status"] == "access_denied"
    assert payload["tool_id"] == LONG_WAIT_TOOL_ID
    assert payload["level"] == "write"
    assert "request_access" in payload["next_step"]
    assert f"tool_id='{LONG_WAIT_TOOL_ID}'" in payload["next_step"]
    assert "level='write'" in payload["next_step"]
    assert allowed_event.cancel_tool is False

    # Grant fn:long_wait for this task; both decide_call and the hook now allow it.
    work_id = worker.task_manager.get_task_info("task-1").meta.gateway_context.task_context.bound_to.request_id
    service = _service(storage)
    try:
        service.grant_by_person(
            OWNER,
            GrantV1(
                grant_id="grant-long-wait-task",
                team_id=TEAM,
                subject=SubjectRefV1(kind=SubjectKind.AGENT_TYPE, id=f"{TEAM}/WebResearchAgent"),
                target=TargetRefV1(kind=TargetKind.TOOL, id=LONG_WAIT_TOOL_ID),
                level=AccessLevel.WRITE,
                lifetime=GrantLifetime.TASK,
                task_id=work_id,
                granted_by=SubjectRefV1(kind=SubjectKind.USER, id=OWNER.person_id),
                created_at_utc=BASE_TIME,
                for_person_id=OWNER.person_id,
            ),
        )
    finally:
        service.cedar.close()

    after_grant = decide_call(worker, LONG_WAIT_TOOL_NAME, {"minutes": 60, "reason": "redeploy"})
    assert after_grant is not None and after_grant.allowed
    granted_event = SimpleNamespace(
        tool_use={"name": LONG_WAIT_TOOL_NAME, "input": {"minutes": 60, "reason": "redeploy"}},
        cancel_tool=False,
        selected_tool=registered,
    )
    asyncio.run(hook.before_tool_call(granted_event))
    assert granted_event.cancel_tool is False


@needs_cedar
def test_a1_trusted_starter_profile_never_grants_long_wait(storage: dict[str, Any]) -> None:
    service = _service(storage)
    try:
        apply_starter_profile(service, OWNER, "trusted", lead_agent_type="TeamArchitect")
    finally:
        service.cedar.close()

    lead = _make_access_agent(storage, agent_type="TeamArchitect", lead=True, role="team_lead")
    worker = _make_access_agent(storage, agent_type="WebResearchAgent", role="worker")
    assert decide_call(lead, WAIT_TOOL_NAME, {"minutes": 5, "reason": "check"}).allowed
    assert decide_call(worker, WAIT_TOOL_NAME, {"minutes": 5, "reason": "check"}).allowed
    assert not decide_call(lead, LONG_WAIT_TOOL_NAME, {"minutes": 60, "reason": "check"}).allowed
    assert not decide_call(worker, LONG_WAIT_TOOL_NAME, {"minutes": 60, "reason": "check"}).allowed


@needs_cedar
def test_a1_non_admin_member_needs_person_grant_for_long_wait_but_not_wait(storage: dict[str, Any]) -> None:
    worker = _make_access_agent(
        storage,
        person=MEMBER.person_id,
        person_roles=("member",),
    )
    assert decide_call(worker, WAIT_TOOL_NAME, {"minutes": 5, "reason": "check"}).allowed
    assert not decide_call(worker, LONG_WAIT_TOOL_NAME, {"minutes": 60, "reason": "check"}).allowed

    work_id = worker.task_manager.get_task_info("task-1").meta.gateway_context.task_context.bound_to.request_id
    service = _service(storage)
    try:
        # Even with a task grant on the worker, the member side still refuses until an admin grants the member.
        service.grant_by_person(
            OWNER,
            GrantV1(
                grant_id="grant-worker-for-member",
                team_id=TEAM,
                subject=SubjectRefV1(kind=SubjectKind.AGENT_TYPE, id=f"{TEAM}/WebResearchAgent"),
                target=TargetRefV1(kind=TargetKind.TOOL, id=LONG_WAIT_TOOL_ID),
                level=AccessLevel.WRITE,
                lifetime=GrantLifetime.TASK,
                task_id=work_id,
                granted_by=SubjectRefV1(kind=SubjectKind.USER, id=OWNER.person_id),
                created_at_utc=BASE_TIME,
                for_person_id=MEMBER.person_id,
            ),
        )
        assert not decide_call(worker, LONG_WAIT_TOOL_NAME, {"minutes": 60, "reason": "check"}).allowed

        service.grant_by_person(
            OWNER,
            GrantV1(
                grant_id="grant-member-person",
                team_id=TEAM,
                subject=SubjectRefV1(kind=SubjectKind.USER, id=MEMBER.person_id),
                target=TargetRefV1(kind=TargetKind.TOOL, id=LONG_WAIT_TOOL_ID),
                level=AccessLevel.WRITE,
                granted_by=SubjectRefV1(kind=SubjectKind.USER, id=OWNER.person_id),
                created_at_utc=BASE_TIME,
            ),
        )
        assert decide_call(worker, LONG_WAIT_TOOL_NAME, {"minutes": 60, "reason": "check"}).allowed
    finally:
        service.cedar.close()


@needs_cedar
def test_c22_member_long_wait_request_approved_by_admin_in_one_step(storage: dict[str, Any]) -> None:
    """When a member does not hold fn:long_wait, one admin approval in AccessRequests grants both sides."""

    worker = _make_access_agent(
        storage,
        person=MEMBER.person_id,
        person_roles=("member",),
    )
    work_id = worker.task_manager.get_task_info("task-1").meta.gateway_context.task_context.bound_to.request_id
    assert not decide_call(worker, LONG_WAIT_TOOL_NAME, {"minutes": 60, "reason": "redeploy"}).allowed

    service = AccessService(
        store=AccessRepository(storage),
        catalog=load_catalog(None),
        cedar=CedarAccessClient(binary=str(BINARY)),
        team_id=TEAM,
        agent_role_of=agent_role_resolver("TeamArchitect"),
    )
    try:
        requests = AccessRequests(
            service=service,
            inbox=StoredAccessInbox(service.store),
            lead_agent_type="TeamArchitect",
        )
        requester = AgentPrincipal(
            agent_id=worker.id,
            agent_type=worker.named_agent_type,
            role="worker",
            team_id=TEAM,
            work_id=work_id,
        )
        outcome = requests.submit(
            requester=requester,
            person=MEMBER,
            target=TargetRefV1(kind=TargetKind.TOOL, id=LONG_WAIT_TOOL_ID),
            level=AccessLevel.WRITE,
            reason="Wait 60 minutes for maintenance window",
            task_id="task-1",
        )
        assert outcome.waiting and outcome.request is not None
        assert outcome.request.person_lacks_access is True
        assert not requests.may_decide(MEMBER, outcome.request)
        assert requests.may_decide(OWNER, outcome.request)
        with pytest.raises(HandDownRefused):
            requests.decide(outcome.request.request_id, MEMBER, AccessReviewChoice.APPROVE_TASK)

        decided = requests.decide(outcome.request.request_id, OWNER, AccessReviewChoice.APPROVE_TASK)
        assert decided.status is AccessRequestStatus.GRANTED
        # One administrator approval grants both the member and the worker's task; no separate grant step needed.
        assert decide_call(worker, LONG_WAIT_TOOL_NAME, {"minutes": 60, "reason": "redeploy"}).allowed
    finally:
        service.cedar.close()


def test_c21_shared_prompt_and_tool_docstrings_do_not_leak_request_access_when_auth_off() -> None:
    assert "request_access" not in strip_access_prompt(shared_agent_base_prompt)
    assert "request_access" not in (wait.__doc__ or "")
    assert "request_access" not in (long_wait.__doc__ or "")


# -- C03: scheduled job access path (SCHEDULE grants + authorize_scheduled_job_access) ------


@needs_cedar
def test_c03_schedule_grant_allows_long_wait_only_in_that_schedules_run(
    storage: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    use_scheduler_key(monkeypatch)
    # Real schedule rows in the same database so task_identity.job_schedule_id recognizes the job runs.
    store = ScheduleStore(db_path=str(storage.sqlite.path))
    for schedule_id in ("sched_upgrade", "sched_other"):
        store.add(
            ScheduledMessage(
                schedule_id=schedule_id,
                schedule_name=f"Job {schedule_id}",
                management_class=ScheduleManagementClass.USER,
                payload=Task(task_id=f"t-{schedule_id}", task_type=MessageMetaType.REQUEST, problem_statement="Run."),
                payload_header=MessageHeader(from_agent_id="lead", to_routing_path="lead"),
                payload_meta=MessageMeta(
                    to_named_agent_type="WebResearchAgent",
                    gateway_context=GatewayContext(canonical_user_id=OWNER.person_id),
                ),
                interval_ms=86_400_000,
                next_fire_at=(schedule_utc_now() + timedelta(hours=1)).isoformat(),
            ),
            claim_team_id=TEAM,
            claim_named_agent_type="WebResearchAgent",
        )
    service = _service(storage)
    try:
        service.grant_by_person(
            OWNER,
            GrantV1(
                grant_id="grant-sched-long-wait",
                team_id=TEAM,
                subject=SubjectRefV1(kind=SubjectKind.SCHEDULE, id=f"{TEAM}/sched_upgrade"),
                target=TargetRefV1(kind=TargetKind.TOOL, id=LONG_WAIT_TOOL_ID),
                level=AccessLevel.WRITE,
                granted_by=SubjectRefV1(kind=SubjectKind.USER, id=OWNER.person_id),
                created_at_utc=BASE_TIME,
                for_person_id=OWNER.person_id,
            ),
        )
    finally:
        service.cedar.close()

    sched_ctx = signed_scheduler_token(
        OWNER.person_id,
        team_id=TEAM,
        roles=("owner", "admin"),
        root_task_id="run-1",
        schedule_id="sched_upgrade",
    )
    other_sched_ctx = signed_scheduler_token(
        OWNER.person_id,
        team_id=TEAM,
        roles=("owner", "admin"),
        root_task_id="run-2",
        schedule_id="sched_other",
    )
    chat_ctx = signed_task_token(
        OWNER.person_id,
        team_id=TEAM,
        roles=("owner", "admin"),
        root_task_id="chat-1",
    )

    in_job = _make_access_agent(storage, task_id="run-1", signed_context=sched_ctx)
    assert decide_call(in_job, LONG_WAIT_TOOL_NAME, {"minutes": 60, "reason": "redeploy"}).allowed

    in_other_job = _make_access_agent(storage, task_id="run-2", signed_context=other_sched_ctx)
    assert not decide_call(in_other_job, LONG_WAIT_TOOL_NAME, {"minutes": 60, "reason": "redeploy"}).allowed

    in_chat = _make_access_agent(storage, task_id="chat-1", signed_context=chat_ctx)
    assert not decide_call(in_chat, LONG_WAIT_TOOL_NAME, {"minutes": 60, "reason": "redeploy"}).allowed


@needs_cedar
def test_c03_authorize_scheduled_job_access_lists_long_wait_under_ask_a_person_even_with_trusted_profile(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import custom_tools.registry  # noqa: F401
    from tests.change_approval_support import use_signed_contexts

    use_signed_contexts(monkeypatch)
    access_runtime._runtime_access.cache_clear()
    try:
        config = Config({"agent": {"schedule_workers": {"eligible_agent_types": ["Worker"]}}})
        monkeypatch.setattr("config_loader._config", config)

        manager = TaskManager(db_path=str(tmp_path / "runtime.db"))
        manager.set_owner_identity(agent_id="lead", agent_name="lead", team_id=TEAM, named_agent_type="TeamArchitect")
        owner_ctx = GatewayContext(
            canonical_user_id=OWNER.person_id,
            task_context=signed_task_token(
                OWNER.person_id, team_id=TEAM, roles=("owner", "admin"), root_task_id="request"
            ),
        )
        manager.add_task(
            Task(task_id="request", task_type=MessageMetaType.REQUEST, problem_statement="Configure daily job"),
            MessageHeader(),
            MessageMeta(gateway_context=owner_ctx),
            owner_person_id=OWNER.person_id,
        )
        manager.update_main_task_state("request", ProgressState.IN_PROGRESS, schedule_utc_now().isoformat())
        bind_task("request")

        store = ScheduleStore(db_path=str(tmp_path / "runtime.db"))
        store.add(
            ScheduledMessage(
                schedule_id="job-long-wait",
                schedule_name="Daily upgrade",
                management_class=ScheduleManagementClass.USER,
                payload=Task(task_id="t1", task_type=MessageMetaType.REQUEST, problem_statement="Upgrade and wait."),
                payload_header=MessageHeader(from_agent_id="lead", to_routing_path="lead"),
                payload_meta=MessageMeta(to_named_agent_type="Worker", gateway_context=owner_ctx),
                interval_ms=86_400_000,
                next_fire_at=(schedule_utc_now() + timedelta(hours=1)).isoformat(),
            ),
            claim_team_id=TEAM,
            claim_named_agent_type="Worker",
        )

        shipped = load_agent_catalog(include_configured=False).agent_types["ResearchComparisonAgent"]
        worker_def = {
            "named_agent_type": "Worker",
            "role": "worker",
            "goal": "Run daily upgrade",
            "tools": list(shipped.tools),
            "agent_definition_source": "team",
            "agent_definition_source_path": "/data/control/agent_bundles/Worker/agent.yaml",
            "declared_definition": {
                "role": "worker",
                "own_prompt": "Run daily upgrade",
                "include_shared_prompt": False,
                "backstory": "hub_and_spoke",
                "default_tools": [],
                "skills_enabled": True,
            },
        }
        definitions = {"Worker": worker_def}
        lead = SimpleNamespace(
            id="lead",
            name="lead",
            team_id=TEAM,
            is_team_lead=True,
            named_agent_type="TeamArchitect",
            workspace_dir=str(tmp_path),
            team_composition_path=None,
            instance_config_path=None,
            mcp_profiles={},
            task_manager=manager,
            _schedule_store=store,
            _scheduler_wake=asyncio.Event(),
            schedule_workers={},
            authorization_config={"enabled": True, "cedar_binary_path": str(BINARY)},
            instance_root=None,
            _local_team_runtime=SimpleNamespace(
                agent_types=lambda: tuple(definitions),
                local_workers=lambda team_id: (),
                resolve_definition=definitions.get,
            ),
            definitions=definitions,
        )

        # Apply Trusted starter profile for the owner: fn:long_wait is still outside it.
        apply_starter_profile(
            access_runtime.runtime_access(lead).service,
            OWNER,
            "trusted",
            lead_agent_type="TeamArchitect",
        )

        configure_worker(
            lead,
            WorkerConfigurationRequestV1(
                schedule_id="job-long-wait",
                expected_revision=0,
                target_agent_type="Worker",
                policy=ScheduleWorkerPolicySpecV1(enabled=True),
                capabilities=ScheduleCapabilityRequirementsV1.model_validate(
                    {"requirements": [{"capability_id": LONG_WAIT_TOOL_ID, "operation": LONG_WAIT_TOOL_NAME}]}
                ),
                activate_schedule=False,
            ),
        )
        result = ScheduleWorkerService(lead).authorize_access("job-long-wait", expected_revision=1)
        assert result["given"] == []
        assert len(result["ask_a_person"]) == 1
        asked = result["ask_a_person"][0]
        assert asked["tool_id"] == LONG_WAIT_TOOL_ID
        assert asked["level"] == "write"
        assert asked["operations"] == [LONG_WAIT_TOOL_NAME]
        assert f"request_access(tool_id='{LONG_WAIT_TOOL_ID}', level='write'" in asked["message"]
    finally:
        access_runtime._runtime_access.cache_clear()
        forget_task_context_keys()


# -- A7 & C04: authorization off path -------------------------------------------------------


def test_a7_auth_off_hydrates_wait_for_every_role_and_long_wait_only_from_default_tools(
    storage: dict[str, Any],
) -> None:
    catalog = load_agent_catalog(include_configured=False)
    assert catalog.agent_types, "shipped catalog must define built-in agent types"
    for agent_type, definition in catalog.agent_types.items():
        names = {tool_name(item) for item in definition.tools}
        assert WAIT_TOOL_NAME in names, f"{agent_type} should have wait in hydrated tools"
        assert LONG_WAIT_TOOL_NAME not in names, f"{agent_type} must not include long_wait by default"

    # Non-standard tool path when authorization is off: operator or specialist definition adds fn:long_wait to default_tools.
    without_default = {tool_name(t) for t in _hydrate_tools("CustomWorker", "worker", ())}
    with_default = {tool_name(t) for t in _hydrate_tools("CustomWorker", "worker", (LONG_WAIT_TOOL_ID,))}
    assert WAIT_TOOL_NAME in without_default and LONG_WAIT_TOOL_NAME not in without_default
    assert {WAIT_TOOL_NAME, LONG_WAIT_TOOL_NAME} <= with_default

    # startup_tools with authorization disabled preserves those hydrated tools.
    auth_off_agent = _make_access_agent(storage, enabled=False)
    hydrated_with = list(_hydrate_tools("CustomWorker", "worker", (LONG_WAIT_TOOL_ID,)))
    shown = {tool_name(t) for t in startup_tools(auth_off_agent, hydrated_with)}
    assert {WAIT_TOOL_NAME, LONG_WAIT_TOOL_NAME} <= shown

    # Access checks are bypassed cleanly when authorization is off.
    assert decide_call(auth_off_agent, WAIT_TOOL_NAME, {"minutes": 5, "reason": "check"}) is None
    assert decide_call(auth_off_agent, LONG_WAIT_TOOL_NAME, {"minutes": 60, "reason": "check"}) is None
    hook = AccessDecisionHook(auth_off_agent)
    event = SimpleNamespace(
        tool_use={"name": LONG_WAIT_TOOL_NAME, "input": {"minutes": 60, "reason": "check"}},
        cancel_tool=False,
        selected_tool=object(),
    )
    asyncio.run(hook.before_tool_call(event))
    assert event.cancel_tool is False


@pytest.mark.asyncio
async def test_a7_c04_auth_off_budget_refusal_matches_whether_agent_has_long_wait(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(timed_waits_rules, "utc_now", lambda: BASE_TIME)
    manager = TaskManager(db_path=str(tmp_path / "manager.db"))
    manager.set_owner_identity(agent_id="worker-1", agent_name="worker-1", team_id=TEAM, named_agent_type="Worker")
    manager.add_task(
        Task(task_id="task-auth-off", task_type=MessageMetaType.REQUEST, problem_statement="Wait a while"),
        MessageHeader(),
        MessageMeta(),
        owner_person_id=OWNER.person_id,
    )
    manager.append_history_entry(
        "task-auth-off",
        "Used 15 minutes",
        entry_type=TaskHistoryType.QUERY_EVENT,
        importance=TaskHistoryImportance.HIGH,
        details={
            "event": TIMED_WAIT_STARTED_EVENT,
            "call_id": "call-prior",
            "tool_use_id": "tu-prior",
            "tool": WAIT_TOOL_NAME,
            "minutes": 15,
            "requested_at": BASE_TIME.isoformat(),
            "resume_at": (BASE_TIME + timedelta(minutes=15)).isoformat(),
            "reason": "prior wait",
        },
    )

    def _ctx(tool_use_id: str) -> ToolContext:
        fake_agent = SimpleNamespace(_interrupt_state=_InterruptState(), state=SimpleNamespace())
        return ToolContext(
            tool_use={"toolUseId": tool_use_id, "name": WAIT_TOOL_NAME, "input": {}},
            agent=fake_agent,  # type: ignore[arg-type]
            invocation_state={},
        )

    # Without long_wait in default_tools: refusal tells operator to add fn:long_wait to default_tools.
    without_agent = SimpleNamespace(
        id="worker-1",
        team_id=TEAM,
        task_manager=manager,
        timed_wait_limits=TimedWaitLimits(),
        authorization_config={"enabled": False},
        tools=list(_hydrate_tools("Worker", "worker", ())),
    )
    set_agent_ref(without_agent)  # type: ignore[arg-type]
    try:
        with task_work("task-auth-off"):
            res_without = json.loads(await wait(10, "need more time", _ctx("tu-1")))
    finally:
        set_agent_ref(None)
    assert res_without["status"] == "refused"
    assert "default_tools" in res_without["reason"]
    assert LONG_WAIT_TOOL_ID in res_without["reason"]
    assert "request_access" not in res_without["reason"]

    # With long_wait in default_tools: refusal directs straight to long_wait.
    with_agent = SimpleNamespace(
        id="worker-1",
        team_id=TEAM,
        task_manager=manager,
        timed_wait_limits=TimedWaitLimits(),
        authorization_config={"enabled": False},
        tools=list(_hydrate_tools("Worker", "worker", (LONG_WAIT_TOOL_ID,))),
    )
    set_agent_ref(with_agent)  # type: ignore[arg-type]
    try:
        with task_work("task-auth-off"):
            res_with = json.loads(await wait(10, "need more time", _ctx("tu-2")))
    finally:
        set_agent_ref(None)
    assert res_with["status"] == "refused"
    assert "Use long_wait instead" in res_with["reason"]
    assert "default_tools" not in res_with["reason"]
