"""The agent access tools and the pause and resume around a request.

Real Cedar, SQLite and TaskManager. Covers: a request that waits pauses the
tool with an access interrupt, a resumed call finds its own request instead of
asking again, an automatic hand-down returns at once, access already held,
authorization off, giving/restricting/revoking and listing; the interrupt
waits locally with a blocker instead of asking the requester; a decision
resumes a live turn through its interrupt or, after a restart, from a
directive; and a request is withdrawn when its task ends first.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from strands.interrupt import Interrupt, InterruptException

from access_catalog.catalog import load_catalog
from access_control.cedar import CedarAccessClient
from access_control.decisions import AccessService
from access_control.entities import PersonPrincipal
from access_control.starter_profiles import apply_starter_profile
from custom_agents import access_request_runtime, access_runtime
from custom_agents.task_human_interaction_blockers import (
    ACCESS_REQUEST_BLOCKER,
    get_human_interaction_blocker,
)
from custom_agents.task_manager import TaskManager
from custom_agents.task_steering_runtime import send_interrupt_request_for_result
from custom_messages.contracts.access_v2 import (
    AccessRequestResolvedV1,
    AccessRequestStatus,
    AccessReviewChoice,
    SubjectKind,
    SubjectRefV1,
    TargetKind,
    TargetRefV1,
)
from custom_messages.contracts.routing_v1 import MessageHeader, MessageMeta
from custom_messages.contracts.task_v1 import ACCESS_REQUEST_INTERRUPT_KIND, Task
from custom_messages.enums import MessageMetaType, ProgressState
from custom_tools.providers import access as tools
from custom_tools.providers import tool_loading
from custom_tools.runtime_context import set_agent_ref
from storage.access_repository import AccessRepository
from storage.config import resolve_storage_settings
from tests.broker_access_support import context_signer, context_verify_keys

ROOT = Path(__file__).resolve().parents[1]
BINARY = ROOT / "target" / "debug" / "ua-cedar-authorizer"
TEAM = "team-1"
OWNER = PersonPrincipal("user-1", TEAM, ("owner",))

pytestmark = pytest.mark.skipif(not BINARY.is_file(), reason="build ua-cedar-authorizer first (cargo build)")


class Context:
    """A Strands tool context: one tool call, answered interrupts by name."""

    def __init__(self, tool_use_id: str = "tooluse-1") -> None:
        self.tool_use = {"toolUseId": tool_use_id, "name": "request_access"}
        self.responses: dict[str, object] = {}
        self.raised: list[dict] = []

    def interrupt(self, name: str, reason=None):
        if name in self.responses:
            return self.responses[name]
        self.raised.append(reason)
        raise InterruptException(Interrupt(id=f"interrupt-{name}", name=name, reason=reason))


@pytest.fixture
def storage(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("ACCESS_CONTEXT_VERIFY_KEYS", context_verify_keys())
    access_runtime._runtime_access.cache_clear()
    yield resolve_storage_settings({"backend": "sqlite", "sqlite": {"path": str(tmp_path / "manager.db")}})
    access_runtime._runtime_access.cache_clear()
    set_agent_ref(None)


def make_agent(storage, tmp_path: Path, *, agent_type="WebResearchAgent", lead=False, enabled=True):
    # Every agent shares the manager database, as in production.
    manager = TaskManager(db_path=str(tmp_path / "manager.db"))
    manager.set_owner_identity(f"agent-{agent_type}", agent_type, TEAM, agent_type)
    signed = context_signer().issue(team_id=TEAM, person_id=OWNER.person_id, person_roles=("owner",))
    meta = MessageMeta(conversation_id="conv-1")
    meta.gateway_context = SimpleNamespace(task_context=signed)
    task_id = f"task-{agent_type}"
    manager.add_task(Task(task_id=task_id, task_type=MessageMetaType.REQUEST), MessageHeader(), meta, owner_person_id=None)
    agent = SimpleNamespace(
        id=f"agent-{agent_type}",
        name=agent_type,
        named_agent_type=agent_type,
        team_id=TEAM,
        is_team_lead=lead,
        access_role="worker",
        authorization_config={"enabled": enabled, "cedar_binary_path": str(BINARY)},
        task_manager=manager,
        instance_root=None,
        _active_execution_task_id=task_id,  # the task the agent is running now
        _interrupted_turn_sessions={},
        paused=False,
        is_executing=False,
        _should_exit=asyncio.Event(),
        _pending_work_task=None,
        _continue_pending_work=AsyncMock(),
    )
    return agent


def owner_service(storage) -> AccessService:
    return AccessService(
        store=AccessRepository(storage), catalog=load_catalog(None), cedar=CedarAccessClient(binary=str(BINARY)), team_id=TEAM
    )


def call(tool, **kwargs) -> dict:
    return json.loads(tool(**kwargs))


def test_a_waiting_request_pauses_and_the_resumed_call_returns_the_decision(storage, tmp_path) -> None:
    worker = make_agent(storage, tmp_path)
    set_agent_ref(worker)
    context = Context()

    with pytest.raises(InterruptException):
        tools.request_access(tool_id="fn:schedules", level="write", reason="Track the report", tool_context=context)
    [reason] = context.raised
    assert reason["kind"] == ACCESS_REQUEST_INTERRUPT_KIND and "Schedules" in reason["summary"]
    requests = access_runtime.runtime_requests(worker)
    request = requests.service.store.get_request(reason["access_request_id"])
    assert request.status is AccessRequestStatus.WAITING and request.conversation_id == "conv-1"

    requests.decide(request.request_id, OWNER, AccessReviewChoice.APPROVE_TASK)
    context.responses[tools.ACCESS_REQUEST_INTERRUPT_NAME] = access_request_runtime.decision_response(
        requests.service.store.get_request(request.request_id)
    )
    result = call(tools.request_access, tool_id="fn:schedules", level="write", reason="Track the report", tool_context=context)

    assert result["status"] == "granted" and "this task only" in result["message"]
    assert len(requests.service.store.list_requests(TEAM)) == 1, "the resumed call did not ask again"
    assert access_runtime.decide_call(worker, "create_scheduled_job", {}).allowed


def test_automatic_hand_down_already_held_and_authorization_off(storage, tmp_path) -> None:
    service = owner_service(storage)
    apply_starter_profile(service, OWNER, "trusted", lead_agent_type="TeamArchitect")
    service.cedar.close()
    worker = make_agent(storage, tmp_path)
    set_agent_ref(worker)

    # Web research is standard internet access (decision D14): nothing to ask for.
    web = call(tools.request_access, tool_id="fn:web_research", level="read", reason="Research", tool_context=Context("t0"))
    assert web["status"] == "already_held"

    granted = call(tools.request_access, tool_id="fn:publishing", level="write", reason="Publish", tool_context=Context())
    assert granted["status"] == "granted" and "Retry the call" in granted["message"]
    held = call(tools.request_access, tool_id="fn:publishing", level="write", reason="Publish", tool_context=Context("t2"))
    assert held["status"] == "already_held"
    found = call(tool_loading.find_tools, query="publish")
    assert [t["your_level"] for t in found["tools"] if t["tool_id"] == "fn:publishing"] == ["write"]
    # Discovery recognizes automatic hand-on without creating another grant.
    before = access_runtime.runtime_requests(worker).service.store.version(TEAM)
    [schedules] = [t for t in call(tool_loading.find_tools, query="create_scheduled_job")["tools"]]
    assert schedules["tool_id"] == "fn:schedules" and "create_scheduled_job" in schedules["functions"]
    assert schedules["your_level"] == "full" and schedules["how_to_get"] is None
    assert access_runtime.runtime_requests(worker).service.store.version(TEAM) == before
    mine = call(tools.list_my_access)
    [published] = [a for a in mine["access"] if a["target"] == "fn:publishing"]
    assert published["from"] == "user-1 → TeamArchitect → WebResearchAgent"

    set_agent_ref(make_agent(storage, tmp_path, agent_type="OtherAgent", enabled=False))
    off = call(tools.request_access, tool_id="fn:schedules", level="read", reason="x", tool_context=Context("t3"))
    assert off["status"] == "not_needed"


def test_a_workers_task_gets_what_the_team_lead_may_hand_on_and_restrictions_win(storage, tmp_path) -> None:
    service = owner_service(storage)
    apply_starter_profile(service, OWNER, "trusted", lead_agent_type="TeamArchitect")
    service.cedar.close()
    worker = make_agent(storage, tmp_path)
    set_agent_ref(worker)

    # The first call adds the access to the worker's task: no request, no turn ("access follows the work").
    assert access_runtime.decide_call(worker, "create_scheduled_job", {}).allowed
    store = access_runtime.runtime_requests(worker).service.store
    worker_type = SubjectRefV1(kind=SubjectKind.AGENT_TYPE, id=f"{TEAM}/WebResearchAgent")
    [handed] = [grant for grant in store.for_subjects(TEAM, [worker_type]) if grant.target.id == "fn:schedules"]
    assert handed.lifetime.value == "task" and handed.task_id == access_runtime.verified_context_id(worker)
    assert store.notices_for(TEAM, person_id=OWNER.person_id, roles=OWNER.roles) == [], "added to one task: no notice"

    lead = make_agent(storage, tmp_path, agent_type="TeamArchitect", lead=True)
    set_agent_ref(lead)
    restricted = call(
        tools.restrict_access,
        agent_type="WebResearchAgent",
        tool_id="fn:schedules",
        operation="create_scheduled_job",
        reason="Only change existing schedules",
    )
    assert restricted["status"] == "restricted"
    assert not access_runtime.decide_call(worker, "create_scheduled_job", {}).allowed
    assert access_runtime.decide_call(worker, "update_scheduled_job", {}).allowed
    assert call(tools.revoke_access, grant_id=restricted["grant_id"])["status"] == "revoked"
    assert access_runtime.decide_call(worker, "create_scheduled_job", {}).allowed
    assert "give_access" not in tools.ACCESS_TOOL_NAMES


@pytest.mark.asyncio
async def test_an_access_interrupt_waits_here_and_a_decision_resumes_the_task(storage, tmp_path) -> None:
    worker = make_agent(storage, tmp_path)
    worker.mid_turn_steering = {}
    worker._agent_id_by_name = {}
    worker.named_agent_type_queue_map = {}
    worker._logger = SimpleNamespace(warning=lambda *a, **k: None, info=lambda *a, **k: None)
    worker.team_comms = SimpleNamespace(send_message=AsyncMock())
    set_agent_ref(worker)
    context = Context()
    with pytest.raises(InterruptException) as raised:
        tools.request_access(tool_id="fn:schedules", level="write", reason="Track", tool_context=context)
    request_id = context.raised[0]["access_request_id"]
    manager = worker.task_manager
    task_id = worker._active_execution_task_id
    info = manager.get_task_info(task_id)

    # Scheduled work has no requester route; an access wait needs none.
    await send_interrupt_request_for_result(
        worker,
        result=SimpleNamespace(interrupts=[raised.value.interrupt]),
        task=info.task,
        msg_header=info.header,
        msg_meta=info.meta,
        strands_agent=object(),
        invoke_kwargs={},
        full_prompt="",
    )

    worker.team_comms.send_message.assert_not_awaited()
    blocker = get_human_interaction_blocker(manager, task_id)
    assert blocker["kind"] == ACCESS_REQUEST_BLOCKER and blocker["access_request_id"] == request_id
    assert manager.get_task_info(task_id).state == ProgressState.WAITING

    # A live turn is answered through its interrupt.
    requests = access_runtime.runtime_requests(worker)
    requests.decide(request_id, OWNER, AccessReviewChoice.APPROVE)
    steered = AsyncMock()
    from custom_agents import task_steering_runtime

    original = task_steering_runtime.handle_task_steering_message
    task_steering_runtime.handle_task_steering_message = steered
    try:
        message = AccessRequestResolvedV1(message_id="m1", team_id=TEAM, request_id=request_id)
        await access_request_runtime.handle_access_request_resolved(
            worker, message=message, msg_header=MessageHeader(), msg_meta=MessageMeta()
        )
    finally:
        task_steering_runtime.handle_task_steering_message = original
    steering = steered.await_args.kwargs["steering"]
    assert steering.interrupt_responses[0]["response"]["status"] == "granted"

    # After a restart there is no live turn: the task resumes from a directive.
    worker._interrupted_turn_sessions.clear()
    await access_request_runtime.handle_access_request_resolved(
        worker, message=message, msg_header=MessageHeader(), msg_meta=MessageMeta()
    )
    assert get_human_interaction_blocker(manager, task_id) is None
    assert manager.get_task_info(task_id).state == ProgressState.ACCEPTED
    # The decision's handler returned before the turn; the retained drain runs it.
    worker._continue_pending_work.assert_not_awaited()
    await asyncio.wait_for(worker._pending_work_task, 2)
    worker._continue_pending_work.assert_awaited()


def test_a_request_is_withdrawn_when_its_task_ends_first(storage, tmp_path) -> None:
    worker = make_agent(storage, tmp_path)
    set_agent_ref(worker)
    access_runtime.AccessDecisionHook(worker)  # registers the task-ended cleanup
    context = Context()
    with pytest.raises(InterruptException):
        tools.request_access(tool_id="fn:schedules", level="write", reason="Track", tool_context=context)

    manager = worker.task_manager
    manager.update_main_task_state(worker._active_execution_task_id, ProgressState.CANCELLED, "2026-09-25T00:00:00+00:00")

    request = access_runtime.runtime_requests(worker).service.store.get_request(context.raised[0]["access_request_id"])
    assert request.status is AccessRequestStatus.WITHDRAWN and "cancelled" in request.decision_note


def approved_for_the_task(worker) -> str:
    """The worker asks for access for its task only and the owner approves; returns the grant id."""

    context = Context()
    with pytest.raises(InterruptException):
        tools.request_access(tool_id="fn:schedules", level="write", reason="Track", tool_context=context)
    requests = access_runtime.runtime_requests(worker)
    decided = requests.decide(context.raised[0]["access_request_id"], OWNER, AccessReviewChoice.APPROVE_TASK)
    [grant_id] = decided.grant_ids
    assert requests.service.store.get(grant_id).status.value == "active"
    return grant_id


def test_access_for_one_task_ends_with_the_task(storage, tmp_path) -> None:
    worker = make_agent(storage, tmp_path)
    set_agent_ref(worker)
    access_runtime.AccessDecisionHook(worker)  # registers the task-ended cleanup
    grant_id = approved_for_the_task(worker)

    manager = worker.task_manager
    manager.update_main_task_state(worker._active_execution_task_id, ProgressState.COMPLETED, "2026-09-25T00:00:00+00:00")

    store = access_runtime.runtime_requests(worker).service.store
    assert store.get(grant_id).status.value == "revoked"
    [ended] = [event for event in store.events(TEAM, grant_id=grant_id) if event.kind == "grant_revoked"]
    # Bound to the request (its signed context), which ends with the request's first task.
    assert ended.detail == {"ended_with_task": access_runtime.verified_context_id(worker)}
    assert ended.actor.kind.value == "team"


def test_the_team_lead_ends_task_access_that_no_task_can_use_any_more(storage, tmp_path, caplog) -> None:
    from datetime import datetime, timedelta, timezone

    from custom_messages.contracts.access_v2 import GrantLifetime, GrantV1

    worker = make_agent(storage, tmp_path)
    set_agent_ref(worker)
    running = approved_for_the_task(worker)  # bound to the worker's request, which has not ended
    store = access_runtime.runtime_requests(worker).service.store
    now = datetime.now(timezone.utc)

    def one_task_grant(grant_id: str, task_id: str, created: datetime) -> str:
        store.create(
            GrantV1(
                grant_id=grant_id,
                team_id=TEAM,
                subject=SubjectRefV1(kind=SubjectKind.AGENT_TYPE, id=f"{TEAM}/WebResearchAgent"),
                target=TargetRefV1(kind=TargetKind.TOOL, id="fn:publishing"),
                lifetime=GrantLifetime.TASK,
                task_id=task_id,
                granted_by=SubjectRefV1(kind=SubjectKind.USER, id=OWNER.person_id),
                created_at_utc=created,
                for_person_id=OWNER.person_id,
            )
        )
        return grant_id

    # Bound to one agent task (before access covered the whole request) that the archive has as finished.
    worker.task_manager.complete_task(worker._active_execution_task_id, "2026-09-25T00:00:00+00:00")
    finished = one_task_grant("grant_finished_task", worker._active_execution_task_id, now)
    # Older than any task context lives, so no task can use it.
    too_old = one_task_grant("grant_too_old", "task_context_gone", now - timedelta(days=8))

    lead = make_agent(storage, tmp_path, agent_type="TeamArchitect", lead=True)
    with caplog.at_level("WARNING", logger="custom_agents.access_runtime"):
        access_runtime.AccessDecisionHook(lead)

    assert store.get(finished).status.value == "revoked" and store.get(too_old).status.value == "revoked"
    assert store.get(running).status.value == "active"
    assert "event=task_grant_outlived_task" in caplog.text and "older_than_any_task_context" in caplog.text
    # The check runs once per team lead, not on every hook.
    access_runtime.AccessDecisionHook(lead)
    assert lead._finished_task_grants_checked is True


def test_asking_again_for_more_leaves_one_entry_that_remove_takes_whole(storage, tmp_path) -> None:
    worker = make_agent(storage, tmp_path)
    set_agent_ref(worker)
    requests = access_runtime.runtime_requests(worker)

    def ask_and_approve(tool_use_id: str, level: str, lifetime: str, choice: AccessReviewChoice) -> list[str]:
        context = Context(tool_use_id)
        with pytest.raises(InterruptException):
            tools.request_access(
                tool_id="fn:publishing", level=level, lifetime=lifetime, reason="Publish the report", tool_context=context
            )
        return list(requests.decide(context.raised[0]["access_request_id"], OWNER, choice).grant_ids)

    [for_the_task] = ask_and_approve("tooluse-1", "read", "task", AccessReviewChoice.APPROVE_TASK)
    # Kept: the team lead keeps it (and may hand it on); the worker's task gets it from there.
    lead_grant, task_grant = ask_and_approve("tooluse-2", "write", "until_revoked", AccessReviewChoice.APPROVE)

    store = requests.service.store
    assert store.get(for_the_task).status.value == "revoked", "read-and-change for the task replaced read"
    assert store.get(task_grant).parent_grant_id == lead_grant
    assert [grant.grant_id for grant in requests.service.entry_grants(store.get(task_grant))] == [task_grant]
    assert access_runtime.decide_call(worker, "publish_single_page_site", {}).allowed

    # Removing the team lead's entry ends the worker's access too, even in the running task.
    requests.service.revoke_by_person(OWNER, lead_grant)

    assert store.get(task_grant).status.value == "revoked"
    assert not access_runtime.decide_call(worker, "publish_single_page_site", {}).allowed


def test_release_and_team_skill_changes_leave_silent_notices(storage, tmp_path) -> None:
    from datetime import datetime, timezone

    from custom_agents.schedule_definition_identity import _notice_release_change
    from custom_messages.contracts.access_v2 import AccessNoticeKind
    from custom_messages.contracts.schedule_worker_v1 import ReleaseDefinitionChangeV1
    from custom_agents.team_skill_changes import notice_team_skill_change

    lead = make_agent(storage, tmp_path, agent_type="TeamArchitect", lead=True)
    binding = SimpleNamespace(
        target_agent_type="BackgroundOperationsAgent", schedule_id="sched-1", owner_canonical_user_id="user-1"
    )
    change = ReleaseDefinitionChangeV1(
        changed_at_utc=datetime.now(timezone.utc),
        tools_added=("exa_search",),
        tools_removed=(),
        prompt_changed=True,
        declaration_changed=False,
    )

    _notice_release_change(lead, binding, change)
    notice_team_skill_change(lead, {"skill_name": "weekly-report"}, "installed")

    store = access_runtime.runtime_requests(lead).service.store
    owner_notices = store.notices_for(TEAM, person_id="user-1", roles=("admin", "owner"))
    by_kind = {notice.kind: notice for notice, _read in owner_notices}
    assert set(by_kind) == {AccessNoticeKind.RELEASE_CHANGED, AccessNoticeKind.TEAM_SKILL_CHANGED}
    assert "tools added: exa_search" in by_kind[AccessNoticeKind.RELEASE_CHANGED].summary
    assert by_kind[AccessNoticeKind.TEAM_SKILL_CHANGED].title == "TeamArchitect installed the team skill weekly-report"
    assert store.notices_for(TEAM, person_id="member-9", roles=("member",)) == []


async def _waiting_on_request(storage, tmp_path):
    """A worker whose task waits on its access request, as request_access leaves it."""
    worker = make_agent(storage, tmp_path)
    worker.mid_turn_steering = {}
    worker._agent_id_by_name = {}
    worker.named_agent_type_queue_map = {}
    worker._logger = SimpleNamespace(warning=lambda *a, **k: None, info=lambda *a, **k: None)
    worker.team_comms = SimpleNamespace(send_message=AsyncMock())
    set_agent_ref(worker)
    context = Context()
    with pytest.raises(InterruptException) as raised:
        tools.request_access(tool_id="fn:schedules", level="write", reason="Track", tool_context=context)
    task_id = worker._active_execution_task_id
    info = worker.task_manager.get_task_info(task_id)
    await send_interrupt_request_for_result(
        worker, result=SimpleNamespace(interrupts=[raised.value.interrupt]), task=info.task,
        msg_header=info.header, msg_meta=info.meta, strands_agent=object(), invoke_kwargs={}, full_prompt="",
    )
    return worker, context.raised[0]["access_request_id"], task_id


def _restarted(worker):
    """The same worker type after a restart: a new agent id holding the same tasks."""
    import asyncio

    restarted = SimpleNamespace(**vars(worker))
    restarted.id = "agent-WebResearchAgent-after-restart"
    restarted._interrupted_turn_sessions = {}
    restarted._should_exit = asyncio.Event()
    restarted._pending_work_task = None
    restarted._continue_pending_work = AsyncMock()
    return restarted


@pytest.mark.asyncio
async def test_a_decision_made_while_the_agent_restarted_still_resumes_its_task(storage, tmp_path) -> None:
    """2026-10-04: the notice went to the old agent id and was never resent, so
    the taken-over task waited forever while the reviewer thought it approved."""
    import asyncio

    worker, request_id, task_id = await _waiting_on_request(storage, tmp_path)
    restarted = _restarted(worker)
    store = access_runtime.runtime_requests(restarted).service.store

    # Still undecided: the request is pointed at the new agent, the task keeps waiting.
    assert await access_request_runtime.resume_decided_access_waits(restarted, now=1.0) == 0
    assert store.get_request(request_id).requester_agent_id == restarted.id
    assert worker.task_manager.get_task_info(task_id).state == ProgressState.WAITING
    assert await access_request_runtime.resume_decided_access_waits(restarted, now=30.0) == 0  # paced

    access_runtime.runtime_requests(restarted).decide(request_id, OWNER, AccessReviewChoice.APPROVE)
    assert await access_request_runtime.resume_decided_access_waits(restarted, now=100.0) == 1
    assert get_human_interaction_blocker(worker.task_manager, task_id) is None
    assert worker.task_manager.get_task_info(task_id).state == ProgressState.ACCEPTED
    await asyncio.sleep(0)
    restarted._continue_pending_work.assert_awaited()  # queued, not run inside the scheduler pass
    assert await access_request_runtime.resume_decided_access_waits(restarted, now=200.0) == 0


@pytest.mark.asyncio
async def test_a_notice_reaches_the_restarted_agent_of_the_same_type(storage, tmp_path) -> None:
    worker, request_id, task_id = await _waiting_on_request(storage, tmp_path)
    restarted = _restarted(worker)
    access_runtime.runtime_requests(restarted).decide(request_id, OWNER, AccessReviewChoice.APPROVE)
    message = AccessRequestResolvedV1(message_id="m2", team_id=TEAM, request_id=request_id)
    await access_request_runtime.handle_access_request_resolved(
        restarted, message=message, msg_header=MessageHeader(), msg_meta=MessageMeta()
    )
    assert worker.task_manager.get_task_info(task_id).state == ProgressState.ACCEPTED

    other_type = _restarted(worker)
    other_type.named_agent_type = "PersonalAdminAgent"
    assert access_request_runtime._decided_request(other_type, message) is None
