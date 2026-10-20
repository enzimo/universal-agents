"""Supervised runtime restarts applied through apply_change, with real Cedar and storage.

The manager is a real ``TaskManager`` on SQLite and, with
``LIVE_TEST_POSTGRES_DSN``, PostgreSQL; the access store shares its database. It
holds the request that asks for the restart (running), another person's
request (waiting) and work the first one delegated to a specialist, who
reports it in their status.

Covers: a restart pauses until a person approves the exact plan, then the
supervisor accepts the request it writes, and its receipt tells the task that
asked for it what happened (with the approver's note); a denial returns the
note; the approval covers the tasks the restart interrupts, so other state
changes and tasks finishing keep it valid, while a new task the restart would
interrupt is refused, writes nothing, returns the note, and a person is asked
again; the supervisor refuses agent restart requests that record no approval.
"""

from __future__ import annotations

import asyncio
import json
import logging
import subprocess
import sys
from collections.abc import Iterator
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest
from tests.tool_result_support import tool_json
from strands.interrupt import Interrupt, InterruptException

from access_control.entities import PersonPrincipal
from assistant_gateway.config import GatewayInstanceConfig
from custom_agents import access_runtime
from custom_agents.runtime_restart_service import (
    RuntimeRestartService,
    resolve_runtime_restart_paths,
    restart_resume_directive,
    restarts_dispatched_for_task,
)
from custom_agents.task_manager import TaskManager
from custom_messages.contracts.access_v2 import CHANGE_EFFECT_MAX_CHARS, AccessRequestStatus, AccessReviewChoice
from custom_messages.contracts.control_v1 import AgentStatus
from custom_messages.contracts.routing_v1 import GatewayContext, MessageHeader, MessageMeta
from custom_messages.contracts.runtime_v1 import RUNTIME_RESTART_REQUEST_SCHEMA_VERSION
from custom_messages.contracts.task_v1 import Task
from custom_messages.enums import MessageMetaType, ProgressState
from custom_tools.providers.changes import apply_change
from custom_tools.runtime_context import set_agent_ref
from docker.launch_headless import pop_runtime_restart_request
from storage.config import StorageSettings
from tests.broker_access_support import context_signer, context_verify_keys
from tests.knowledge_support import knowledge_storage

ROOT = Path(__file__).resolve().parents[1]
CEDAR_BINARY = ROOT / "target" / "debug" / "ua-cedar-authorizer"
TEAM = "team_restart"
LEAD_ID = "agent_team_lead"
SPECIALIST_ID = "agent_specialist"
# PostgreSQL runs when LIVE_TEST_POSTGRES_DSN names a server (knowledge_storage).
BACKENDS = ("sqlite", "postgres")

pytestmark = pytest.mark.skipif(not CEDAR_BINARY.is_file(), reason="build ua-cedar-authorizer first (cargo build)")


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _meta(owner_id: str, task_id: str) -> MessageMeta:
    signed = context_signer().issue(team_id=TEAM, person_id=owner_id, person_roles=("owner",))
    return MessageMeta(
        conversation_id=f"conversation_{task_id}",
        gateway_context=GatewayContext(source="test", task_context=signed, canonical_user_id=owner_id),
    )


def _admit(agent, task_id: str) -> None:
    """A request reaches the manager: a person's message, a scheduled run or a heartbeat item."""

    task = Task(task_id=task_id, task_type=MessageMetaType.REQUEST, problem_statement=f"Work for {task_id}")
    header = MessageHeader(from_agent_id="gateway")
    assert agent.task_manager.add_task(task, header, _meta(agent.owner_id, task_id), owner_person_id=agent.owner_id)


def _delegate(agent, parent_task_id: str, subtask_id: str) -> None:
    """The manager hands part of a request to the specialist."""

    subtask = Task(
        task_id=subtask_id, parent_task_id=parent_task_id, task_type=MessageMetaType.REQUEST, problem_statement="Part"
    )
    header = MessageHeader(from_agent_id=LEAD_ID, to_routing_path="team_CodeWritingAgent")
    assert agent.task_manager.add_subtask(parent_task_id, subtask, header, _meta(agent.owner_id, subtask_id))


def _set_state(agent, task_id: str, state: ProgressState) -> None:
    assert agent.task_manager.update_main_task_state(task_id, state, _now()), (task_id, state)


def _specialist_status(*, current_task_id: str | None, deferred: int = 1) -> AgentStatus:
    """The specialist's presence report, as the manager keeps it (``known_agents``)."""

    return AgentStatus(
        status_type=MessageMetaType.RESPONSE,
        team_id=TEAM,
        self_id=SPECIALIST_ID,
        named_agent_type="CodeWritingAgent",
        agent_role="Specialist",
        name="Ada",
        is_paused=False,
        is_executing=current_task_id is not None,
        current_task_id=current_task_id,
        current_task_age_seconds=42,
        top_level_queue_depth=0,
        waiting_top_level_task_count=0,
        deferred_message_count=deferred,
        last_seen_at=_now(),
    )


def _report(agent, status: AgentStatus) -> None:
    agent.known_agents.agent_map[SPECIALIST_ID] = status


class _Call:
    """A Strands tool context for one apply_change call; pauses until answered."""

    def __init__(self) -> None:
        self.tool_use = {"toolUseId": "tooluse_restart", "name": "apply_change"}
        self.response = None
        self.reasons: list[dict] = []

    def interrupt(self, name, reason=None):
        if self.response is not None:
            return self.response
        self.reasons.append(reason)
        raise InterruptException(Interrupt(id=f"interrupt-{name}", name=name, reason=reason))


def _make_agent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, storage: StorageSettings | None = None
) -> Iterator[SimpleNamespace]:
    """TeamArchitect with its manager and access store on ``storage`` (the instance's SQLite by default)."""

    monkeypatch.setenv("ACCESS_CONTEXT_VERIFY_KEYS", context_verify_keys())
    access_runtime._runtime_access.cache_clear()
    root = tmp_path / "instance"
    config = GatewayInstanceConfig.create(
        instance_name="runtime-restart-test", instance_root=str(root), team_id=TEAM, timezone="UTC"
    )
    config_path = config.save(root / "instance_config.yaml")
    manager = TaskManager(
        max_queued_top_level_tasks=50,  # as in config.example.yaml
        storage_settings=storage or config.manager_storage_settings,
        logger=logging.getLogger("test.restart"),
    )
    manager.set_owner_identity(LEAD_ID, "TeamArchitect", TEAM, "TeamArchitect")
    lead = SimpleNamespace(
        id=LEAD_ID,
        owner_id=config.user.canonical_user_id,
        team_id=TEAM,
        is_team_lead=True,
        name="TeamArchitect",
        named_agent_type="TeamArchitect",
        access_role="worker",
        authorization_config={"enabled": True, "cedar_binary_path": str(CEDAR_BINARY)},
        instance_config_path=str(config_path),
        instance_root=str(root),
        bootstrap_config={"instance_root": str(root)},
        workspace_dir=str(config.workspace_path),
        task_manager=manager,
        known_agents=SimpleNamespace(agent_map={}),
    )
    # The task that asks for the restart runs; another request waits for its
    # person; the first delegated part of its work to the specialist.
    _admit(lead, "task_restart")
    assert manager.get_next_task_to_process() == "task_restart"
    _set_state(lead, "task_restart", ProgressState.IN_PROGRESS)
    _admit(lead, "task_waiting")
    _set_state(lead, "task_waiting", ProgressState.WAITING)
    _delegate(lead, "task_restart", "task_child")
    _report(lead, _specialist_status(current_task_id="task_child"))
    assert manager.current_task_id == "task_restart"
    set_agent_ref(lead)
    yield lead
    set_agent_ref(None)
    access_runtime._runtime_access.cache_clear()


@pytest.fixture(params=BACKENDS)
def agent(request: pytest.FixtureRequest, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    with knowledge_storage(request.param, tmp_path) as storage:
        yield from _make_agent(tmp_path, monkeypatch, storage)


def _requests_dir(agent) -> Path:
    return Path(agent.instance_root) / "runtime" / "restart_requests"


def _decide(agent, call: _Call, choice: AccessReviewChoice = AccessReviewChoice.APPROVE, note: str = "") -> None:
    requests = access_runtime.runtime_requests(agent)
    owner = PersonPrincipal(agent.owner_id, TEAM, ("owner",))
    requests.decide(call.reasons[0]["access_request_id"], owner, choice, note=note)
    call.response = {"kind": "access_request_decision", "status": choice.value}


def _approve(agent, call: _Call, note: str = "") -> None:
    _decide(agent, call, AccessReviewChoice.APPROVE, note)


def test_restart_approval_keeps_work_delegated_below_an_approved_child(agent) -> None:
    service = RuntimeRestartService(agent)
    plan = service.stage(scope="all", reason="Reload")
    _delegate(agent, "task_child", "task_grandchild")
    assert agent.task_manager.resolve_root_task_id("task_grandchild") == "task_restart"
    _report(agent, _specialist_status(current_task_id="task_grandchild"))
    assert service.describe(plan["change_id"]).change_id == plan["change_id"]


def test_restart_reason_fits_the_review_before_a_plan_is_saved(agent) -> None:
    service = RuntimeRestartService(agent)
    limit = CHANGE_EFFECT_MAX_CHARS - len("Reason: ")
    reason = "r" * limit
    plan = service.stage(reason=reason)
    proposal = service.describe(plan["change_id"])
    assert f"Reason: {reason}" in proposal.effect
    assert len(next(line for line in proposal.effect if line.startswith("Reason: "))) == CHANGE_EFFECT_MAX_CHARS
    plans = resolve_runtime_restart_paths(agent).plans
    before = sorted(plans.rglob("*.json"))
    with pytest.raises(ValueError, match=rf"at most {limit} characters"):
        service.stage(reason=reason + "r")
    assert sorted(plans.rglob("*.json")) == before


def test_a_restart_waits_for_a_person_and_the_supervisor_accepts_it(agent) -> None:
    plan = RuntimeRestartService(agent).stage(scope="manager", reason="Load the new model settings")
    call = _Call()

    with pytest.raises(InterruptException):
        asyncio.run(apply_change._tool_func(plan["change_id"], "Load the new model settings", call))
    assert not list(_requests_dir(agent).glob("*.json")), "nothing restarts before approval"
    request = access_runtime.runtime_requests(agent).service.store.get_request(call.reasons[0]["access_request_id"])
    assert request.change.kind == "runtime_restart" and "manager" in request.change.title
    assert request.change.summary.startswith("Restart ") and "manager" in request.change.summary
    _approve(agent, call, note="Go ahead, nobody is using it now")
    applied = tool_json(asyncio.run(apply_change._tool_func(plan["change_id"], "Load the new model settings", call)))

    assert applied["status"] == "applied", applied
    assert applied["note"] == "Go ahead, nobody is using it now"
    service = RuntimeRestartService(agent)
    assert service.status(plan["change_id"])["status"] == "requested"
    assert restarts_dispatched_for_task(agent, "task_restart") == []
    assert not list((Path(agent.instance_root) / "runtime/restart-authorization/receipts").glob("*.json"))
    supervisor_request = pop_runtime_restart_request(_requests_dir(agent))
    assert supervisor_request is not None and supervisor_request["scope"] == "manager"
    assert supervisor_request["authority"]["mode"] == "person_approval"
    assert supervisor_request["authority"]["approval"]["access_request_id"] == request.request_id
    assert service.status(plan["change_id"])["status"] == "dispatched"

    # The restart ends this task before the agent reads the result above; the
    # receipt is what restart recovery tells the resumed task.
    [restart] = restarts_dispatched_for_task(agent, "task_restart")
    assert restart.change_id == plan["change_id"] and restart.scope == "manager"
    assert restart.approval_note == "Go ahead, nobody is using it now"
    assert restart.approved_at_utc is not None
    assert restarts_dispatched_for_task(agent, "task_waiting") == []
    directive = restart_resume_directive([restart])
    assert plan["change_id"] in directive and "Go ahead, nobody is using it now" in directive
    assert "Do not stage or apply another restart" in directive


def test_a_denied_restart_returns_the_note(agent) -> None:
    plan = RuntimeRestartService(agent).stage(scope="manager", reason="Reload")
    call = _Call()
    with pytest.raises(InterruptException):
        asyncio.run(apply_change._tool_func(plan["change_id"], "Reload", call))
    _decide(agent, call, AccessReviewChoice.DENY, note="Not during office hours")

    result = tool_json(asyncio.run(apply_change._tool_func(plan["change_id"], "Reload", call)))

    assert result["status"] == "denied" and result["note"] == "Not during office hours"
    assert not list(_requests_dir(agent).glob("*.json"))
    assert restarts_dispatched_for_task(agent, "task_restart") == []


def _ask(agent, change_id: str, *, call_id: str = "tooluse_restart") -> _Call:
    """apply_change asks a person; the task waits for them, as the runtime records it."""

    call = _Call()
    call.tool_use["toolUseId"] = call_id
    with pytest.raises(InterruptException):
        asyncio.run(apply_change._tool_func(change_id, "Reload", call))
    _set_state(agent, "task_restart", ProgressState.WAITING)
    return call


def _resume(agent, change_id: str, call: _Call) -> dict:
    """The person decided; the task runs again and the call returns."""

    _set_state(agent, "task_restart", ProgressState.IN_PROGRESS)
    return tool_json(asyncio.run(apply_change._tool_func(change_id, "Reload", call)))


def _published(agent) -> list[dict]:
    return [json.loads(path.read_text(encoding="utf-8")) for path in sorted(_requests_dir(agent).glob("*.json"))]


def test_other_task_changes_keep_an_approved_restart(agent) -> None:
    plan = RuntimeRestartService(agent).stage(scope="all", reason="Reload")
    call = _ask(agent, plan["change_id"])
    _approve(agent, call)
    # While the person decided: the waiting request's person answered, so it
    # is queued again; the specialist finished its part of the restart's
    # request, picked up the next part, and deferred another message.
    _set_state(agent, "task_waiting", ProgressState.ACCEPTED)
    _set_state(agent, "task_child", ProgressState.COMPLETED)
    _delegate(agent, "task_restart", "task_child_next")
    _report(agent, _specialist_status(current_task_id="task_child_next", deferred=3))

    result = _resume(agent, plan["change_id"], call)

    assert result["status"] == "applied", result
    [request] = _published(agent)
    assert request["authority"]["change_id"] == plan["change_id"]


def test_a_task_finishing_keeps_an_approved_restart(agent) -> None:
    plan = RuntimeRestartService(agent).stage(scope="manager", reason="Reload")
    call = _ask(agent, plan["change_id"])
    _approve(agent, call)
    agent.task_manager.complete_task("task_waiting", _now())
    assert agent.task_manager.get_task_info("task_waiting") is None, "a finished task leaves the manager"

    result = _resume(agent, plan["change_id"], call)

    assert result["status"] == "applied", result
    [request] = _published(agent)
    # The request records what the person approved, the finished task included.
    assert request["impact_snapshot"] == plan["impact_snapshot"]


def test_a_new_task_the_restart_would_interrupt_needs_a_new_approval(agent) -> None:
    service = RuntimeRestartService(agent)
    plan = service.stage(scope="manager", reason="Reload")
    call = _ask(agent, plan["change_id"])
    _approve(agent, call, note="Only if nothing else is running")
    _admit(agent, "task_scheduled_run")  # a scheduled run arrives and is queued

    status = service.status(plan["change_id"])
    assert status["still_applies"] is False
    assert status["new_interrupted_tasks"] == ["task_scheduled_run"]
    result = _resume(agent, plan["change_id"], call)

    assert result["status"] == "refused", result
    assert "task_scheduled_run" in result["reason"]
    assert result["note"] == "Only if nothing else is running"
    assert _published(agent) == []
    stored = access_runtime.runtime_requests(agent).service.store.get_request(call.reasons[0]["access_request_id"])
    assert stored.applied_at_utc is not None and "task_scheduled_run" in stored.apply_error

    # TeamArchitect stages the restart again; a person is asked again and sees the new task.
    fresh = service.stage(scope="manager", reason="Reload")
    again = _ask(agent, fresh["change_id"], call_id="tooluse_restart_again")
    new_request = access_runtime.runtime_requests(agent).service.store.get_request(
        again.reasons[0]["access_request_id"]
    )
    assert new_request.request_id != stored.request_id
    assert new_request.status is AccessRequestStatus.WAITING
    assert any("task_scheduled_run" in line for line in new_request.change.effect)
    _approve(agent, again)
    assert _resume(agent, fresh["change_id"], again)["status"] == "applied"
    [request] = _published(agent)
    assert request["authority"]["change_id"] == fresh["change_id"]


@pytest.mark.parametrize(
    ("next_task", "applies"),
    [
        ("task_child_next", True),  # the next part of a request the person agreed to interrupt
        ("task_specialist_own", False),  # the specialist's own work, which nobody agreed to
    ],
)
def test_an_all_restart_covers_work_delegated_by_approved_tasks_only(agent, next_task: str, applies: bool) -> None:
    plan = RuntimeRestartService(agent).stage(scope="all", reason="Reload")
    call = _ask(agent, plan["change_id"])
    _approve(agent, call)
    _set_state(agent, "task_child", ProgressState.COMPLETED)
    if next_task == "task_child_next":
        _delegate(agent, "task_restart", next_task)
    _report(agent, _specialist_status(current_task_id=next_task))

    result = _resume(agent, plan["change_id"], call)

    if applies:
        assert result["status"] == "applied", result
    else:
        assert result["status"] == "refused" and next_task in result["reason"], result
        assert _published(agent) == []


def test_with_authorization_off_the_persons_agreement_covers_the_same_tasks(agent) -> None:
    agent.authorization_config = {"enabled": False}
    access_runtime._runtime_access.cache_clear()
    service = RuntimeRestartService(agent)
    plan = service.stage(scope="manager", reason="Reload")

    def apply(change_id: str, *, confirmed: bool) -> dict:
        return tool_json(asyncio.run(apply_change._tool_func(change_id, "Reload", _Call(), confirmed=confirmed)))

    asked = apply(plan["change_id"], confirmed=False)
    assert asked["status"] == "agreement_required"
    assert any("finish first" in line for line in asked["effect"])
    agent.task_manager.complete_task("task_waiting", _now())
    _admit(agent, "task_new_message")

    refused = apply(plan["change_id"], confirmed=True)

    assert refused["status"] == "refused" and "task_new_message" in refused["reason"]
    assert _published(agent) == []
    fresh = service.stage(scope="manager", reason="Reload")
    assert apply(fresh["change_id"], confirmed=True)["status"] == "applied"
    [request] = _published(agent)
    assert request["authority"]["mode"] == "authorization_disabled_agreement"
    assert request["authority"]["approval"]["mode"] == "agreement"


def test_the_person_sees_which_tasks_the_restart_interrupts(agent) -> None:
    plan = RuntimeRestartService(agent).stage(scope="all", reason="Load the new model settings")

    proposal = RuntimeRestartService(agent).describe(plan["change_id"])

    effect = "\n".join(proposal.effect)
    # The specialist's work is part of the task that delegated it, not a third task.
    assert "Tasks it may interrupt or delay (2)" in effect
    assert "task_restart (in_progress, Ada on task_child), task_waiting (waiting)." in effect
    assert "finish first, the restart still goes ahead" in effect and "approve it again" in effect
    assert proposal.summary == (
        "Restart manager, configured_startup_agents, activity_collector, gateway, "
        "execution_controller_if_enabled; 2 tasks may be interrupted or delayed."
    )


def test_a_long_task_list_stays_within_one_review_line(agent) -> None:
    """A busy manager (the example configuration allows 50 tasks) can still be restarted."""

    for index in range(40):
        _admit(agent, f"task_id_{index:012x}")
    plan = RuntimeRestartService(agent).stage(scope="manager", reason="Reload")

    proposal = RuntimeRestartService(agent).describe(plan["change_id"])

    [line] = [line for line in proposal.effect if line.startswith("Tasks it may interrupt")]
    assert line.startswith("Tasks it may interrupt or delay (42)") and line.endswith(" more.")
    assert len(line) <= 1000


def test_the_restart_message_and_events_are_unchanged(agent, caplog) -> None:
    with caplog.at_level(logging.INFO, logger="custom_agents.governance.runtime_adapters"):
        plan = _dispatch_approved(agent, scope="all")
    [request] = _published(agent)

    assert set(request) == {
        "schema_version", "request_id", "created_at", "scope", "reason", "requested_by",
        "authority", "impact_snapshot", "source_task_id", "approval_note",
    }
    assert request["schema_version"] == RUNTIME_RESTART_REQUEST_SCHEMA_VERSION
    assert request["scope"] == "all" and request["requested_by"] == LEAD_ID
    assert request["source_task_id"] == "task_restart"
    assert set(request["authority"]) == {"mode", "change_id", "plan_digest", "runtime_state_digest", "approval"}
    assert request["authority"]["plan_digest"] == plan["plan_digest"]
    assert request["authority"]["runtime_state_digest"] == plan["runtime_state_digest"]
    assert request["impact_snapshot"] == plan["impact_snapshot"]
    events = [record.getMessage() for record in caplog.records if "authorized_operation_lifecycle" in record.getMessage()]
    assert [event.split("phase=")[1].split()[0] for event in events] == ["staged", "applied"]
    assert all(f"change_id={plan['change_id']}" in event and plan["plan_digest"] in event for event in events)
    assert pop_runtime_restart_request(_requests_dir(agent)) is not None
    [restart] = restarts_dispatched_for_task(agent, "task_restart")
    assert restart.change_id == plan["change_id"] and restart.scope == "all"


def test_the_supervisor_refuses_agent_restarts_without_an_approval(tmp_path: Path) -> None:
    request_dir = tmp_path / "runtime" / "restart_requests"
    request_dir.mkdir(parents=True)
    base = {
        "schema_version": RUNTIME_RESTART_REQUEST_SCHEMA_VERSION,
        "request_id": "restart_0123456789ab",
        "source_task_id": "task_restart",
        "approval_note": "",
        "scope": "manager",
        "reason": "reload",
        "requested_by": "agent_team_lead",
        "authority": {
            "mode": "person_approval",
            "change_id": "runtime_restart_0123456789ab",
            "plan_digest": "a" * 64,
            "runtime_state_digest": "b" * 64,
            "approval": {"mode": "person_approval", "approved_by": "user_1"},
        },
    }
    (request_dir / "restart_a.json").write_text(json.dumps(base), encoding="utf-8")
    agreement_for_person_mode = json.loads(json.dumps(base))
    agreement_for_person_mode["authority"]["approval"] = {"mode": "agreement", "approved_by": "agreement"}
    (request_dir / "restart_b.json").write_text(json.dumps(agreement_for_person_mode), encoding="utf-8")

    assert pop_runtime_restart_request(request_dir) is None, "no access request id, and a mismatched mode"
    assert not list(request_dir.glob("*.json"))


def _dispatch_approved(agent, *, scope: str = "manager", call_id: str = "tooluse_restart") -> dict:
    plan = RuntimeRestartService(agent).stage(scope=scope, reason="Reload saved settings")
    call = _Call()
    call.tool_use["toolUseId"] = call_id
    with pytest.raises(InterruptException):
        asyncio.run(apply_change._tool_func(plan["change_id"], "Reload saved settings", call))
    _approve(agent, call, note="Check the saved settings")
    result = tool_json(asyncio.run(apply_change._tool_func(plan["change_id"], "Reload saved settings", call)))
    assert result["status"] == "applied", result
    return plan


def test_pending_restart_cannot_publish_twice(agent) -> None:
    plan = _dispatch_approved(agent)
    service = RuntimeRestartService(agent)
    from access_control.changes import ChangeApproval
    from custom_messages.contracts.access_v2 import ChangeApprovalMode
    from custom_messages.contracts.common_v1 import utc_now

    with pytest.raises(FileExistsError, match="awaits the supervisor"):
        service.apply(plan["change_id"], ChangeApproval(ChangeApprovalMode.PERSON_APPROVAL, "owner", utc_now()))
    assert len(list(_requests_dir(agent).glob("*.json"))) == 1
    assert restarts_dispatched_for_task(agent, "task_restart") == []


def test_failed_request_publication_leaves_no_dispatch_receipt(agent, monkeypatch) -> None:
    import custom_agents.runtime_restart_service as restart_service
    from access_control.changes import ChangeApproval
    from custom_messages.contracts.access_v2 import ChangeApprovalMode
    from custom_messages.contracts.common_v1 import utc_now

    service = RuntimeRestartService(agent)
    plan = service.stage(scope="all", reason="Reload all processes")
    def fail_publication(*_args, **_kwargs):
        raise OSError("disk write failed")
    monkeypatch.setattr(restart_service, "atomic_write_json", fail_publication)
    with pytest.raises(OSError, match="disk write failed"):
        service.apply(plan["change_id"], ChangeApproval(ChangeApprovalMode.PERSON_APPROVAL, "owner", utc_now()))
    assert not list(_requests_dir(agent).glob("*.json"))
    assert not list((Path(agent.instance_root) / "runtime/restart-authorization/receipts").glob("*.json"))
    assert service.status(plan["change_id"])["status"] == "pending"


def test_supervisor_records_dispatch_before_removing_request_and_does_not_replay(agent, monkeypatch) -> None:
    plan = _dispatch_approved(agent)
    request_path, = _requests_dir(agent).glob("*.json")
    original_payload = request_path.read_text()
    real_unlink = Path.unlink
    def check_receipt_before_unlink(path, *args, **kwargs):
        if path == request_path:
            assert RuntimeRestartService(agent).status(plan["change_id"])["status"] == "dispatched"
            assert restarts_dispatched_for_task(agent, "task_restart")[0].approval_note == "Check the saved settings"
        return real_unlink(path, *args, **kwargs)
    monkeypatch.setattr(Path, "unlink", check_receipt_before_unlink)
    assert pop_runtime_restart_request(_requests_dir(agent)) is not None
    # A supervisor interruption after its receipt write leaves the same
    # request. On startup it must not cause another restart.
    request_path.write_text(original_payload)
    assert pop_runtime_restart_request(_requests_dir(agent)) is None
    assert not request_path.exists()


def test_supervisor_retains_request_if_dispatch_receipt_cannot_be_written(agent, monkeypatch, caplog) -> None:
    import docker.launch_headless as launcher
    plan = _dispatch_approved(agent)
    real_write = launcher.atomic_write_json
    def fail_receipt(*_args, **_kwargs):
        raise OSError("receipt disk write failed")
    monkeypatch.setattr(launcher, "atomic_write_json", fail_receipt)
    with caplog.at_level(logging.ERROR):
        assert pop_runtime_restart_request(_requests_dir(agent)) is None
    assert RuntimeRestartService(agent).status(plan["change_id"])["status"] == "requested"
    assert "event=runtime_restart_dispatch_failed" in caplog.text
    monkeypatch.setattr(launcher, "atomic_write_json", real_write)
    assert pop_runtime_restart_request(_requests_dir(agent)) is not None
    assert RuntimeRestartService(agent).status(plan["change_id"])["status"] == "dispatched"


def test_failed_receipt_permissions_leave_request_retryable(agent, monkeypatch) -> None:
    plan = _dispatch_approved(agent, scope="all")
    request_path, = _requests_dir(agent).glob("*.json")
    real_chmod = Path.chmod
    failed = False
    def fail_first_receipt_chmod(path, mode, *args, **kwargs):
        nonlocal failed
        if path.parent.name == "receipts" and not failed:
            failed = True
            raise OSError("receipt permission change failed")
        return real_chmod(path, mode, *args, **kwargs)
    monkeypatch.setattr(Path, "chmod", fail_first_receipt_chmod)
    assert pop_runtime_restart_request(_requests_dir(agent)) is None
    assert failed and request_path.exists()
    assert RuntimeRestartService(agent).status(plan["change_id"])["status"] == "requested"
    accepted = pop_runtime_restart_request(_requests_dir(agent))
    assert accepted is not None and accepted["scope"] == "all"
    assert not request_path.exists()
    assert RuntimeRestartService(agent).status(plan["change_id"])["status"] == "dispatched"


@pytest.mark.parametrize("death_at", ["before_publication", "after_publication"])
def test_manager_process_death_cannot_record_an_unpublished_all_restart(tmp_path: Path, death_at: str) -> None:
    """Another manager restart may stop the publisher on either side of rename."""
    script = """
import asyncio, json, os, sys
from pathlib import Path
import pytest
from strands.interrupt import InterruptException
from custom_agents import runtime_restart_service
from custom_agents.runtime_restart_service import RuntimeRestartService
from custom_tools.providers.changes import apply_change
from tests.test_runtime_restart_service import _make_agent, _dispatch_approved, _Call, _approve
root, death_at = Path(sys.argv[1]), sys.argv[2]
with pytest.MonkeyPatch.context() as patch:
    generator = _make_agent(root, patch)
    agent = next(generator)
    first = _dispatch_approved(agent, call_id='first_manager')
    second = RuntimeRestartService(agent).stage(scope='all', reason='Reload all processes')
    call = _Call()
    call.tool_use['toolUseId'] = 'second_all'
    try:
        asyncio.run(apply_change._tool_func(second['change_id'], 'Reload all', call))
    except InterruptException:
        pass
    _approve(agent, call, note='Reload the gateway too')
    (root / 'plans.json').write_text(json.dumps({'first': first['change_id'], 'second': second['change_id']}))
    write = runtime_restart_service.atomic_write_json
    def interrupted_publication(path, payload, **kwargs):
        if death_at == 'after_publication':
            write(path, payload, **kwargs)
        os._exit(73)
    patch.setattr(runtime_restart_service, 'atomic_write_json', interrupted_publication)
    asyncio.run(apply_change._tool_func(second['change_id'], 'Reload all', call))
"""
    child = subprocess.run([sys.executable, "-c", script, str(tmp_path), death_at], cwd=ROOT,
                           capture_output=True, text=True, timeout=30)
    assert child.returncode == 73, child.stderr
    plans = json.loads((tmp_path / "plans.json").read_text())
    instance_root = tmp_path / "instance"
    holder = SimpleNamespace(instance_root=str(instance_root))
    service = RuntimeRestartService(holder)
    # Before the supervisor accepts anything, neither plan has a receipt.
    assert restarts_dispatched_for_task(holder, "task_restart") == []
    accepted = []
    while request := pop_runtime_restart_request(instance_root / "runtime/restart_requests"):
        accepted.append(request["scope"])
    assert service.status(plans["first"])["status"] == "dispatched"
    if death_at == "before_publication":
        assert accepted == ["manager"]
        assert service.status(plans["second"])["status"] == "pending"
    else:
        assert sorted(accepted) == ["all", "manager"]
        assert service.status(plans["second"])["status"] == "dispatched"
        assert any(item.approval_note == "Reload the gateway too"
                   for item in restarts_dispatched_for_task(holder, "task_restart"))
