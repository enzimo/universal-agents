"""Supervised runtime restarts applied through apply_change, with real Cedar and SQLite.

Covers: a restart pauses until a person approves the exact plan, then the
supervisor accepts the request it writes, and its receipt tells the task that
asked for it what happened (with the approver's note); a denial returns the
note; a restart whose running state changed after approval is refused, writes
nothing and still returns the note; the supervisor refuses agent restart
requests that record no approval.
"""

from __future__ import annotations

import asyncio
import json
import logging
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest
from strands.interrupt import Interrupt, InterruptException

from access_control.entities import PersonPrincipal
from assistant_gateway.config import GatewayInstanceConfig
from custom_agents import access_runtime
from custom_agents.runtime_restart_service import (
    RuntimeRestartService,
    restart_resume_directive,
    restarts_dispatched_for_task,
)
from custom_messages.contracts.access_v2 import AccessReviewChoice
from custom_messages.contracts.runtime_v1 import RUNTIME_RESTART_REQUEST_SCHEMA_VERSION
from custom_tools.providers.changes import apply_change
from custom_tools.runtime_context import set_agent_ref
from docker.launch_headless import pop_runtime_restart_request
from tests.broker_access_support import context_signer, context_verify_keys

ROOT = Path(__file__).resolve().parents[1]
CEDAR_BINARY = ROOT / "target" / "debug" / "ua-cedar-authorizer"
TEAM = "team_restart"

pytestmark = pytest.mark.skipif(not CEDAR_BINARY.is_file(), reason="build ua-cedar-authorizer first (cargo build)")


class _TaskManager:
    """One running top-level task, a waiting one, and a delegated child."""

    def __init__(self, *, owner_id: str, storage_settings) -> None:
        self.current_task_id = "task_restart"
        self.owner_id = owner_id
        self.storage_settings = storage_settings
        self.queue_depth = 2
        signed = context_signer().issue(team_id=TEAM, person_id=owner_id, person_roles=("owner",))
        meta = SimpleNamespace(
            gateway_context=SimpleNamespace(task_context=signed), conversation_id="conversation_restart"
        )
        self.root = SimpleNamespace(task=SimpleNamespace(task_id="task_restart"), state="in_progress", meta=meta)
        self.waiting = SimpleNamespace(task=SimpleNamespace(task_id="task_waiting"), state="waiting", meta=meta)
        self.child = SimpleNamespace(task=SimpleNamespace(task_id="task_child"), state="in_progress", meta=meta)

    def get_task_info(self, task_id: str):
        return self.root if task_id == "task_restart" else None

    def get_top_level_task_infos(self):
        return [self.root, self.waiting]

    def get_subtasks(self, task_id: str):
        return [self.child] if task_id == "task_restart" else []

    def get_tracked_top_level_task_count(self) -> int:
        return 2

    def get_runnable_top_level_queue_depth(self) -> int:
        return self.queue_depth

    def get_waiting_top_level_task_count(self) -> int:
        return 1

    def add_task_terminal_callback(self, _callback) -> None:
        return None


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


@pytest.fixture
def agent(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("ACCESS_CONTEXT_VERIFY_KEYS", context_verify_keys())
    access_runtime._runtime_access.cache_clear()
    root = tmp_path / "instance"
    config = GatewayInstanceConfig.create(
        instance_name="runtime-restart-test", instance_root=str(root), team_id=TEAM, timezone="UTC"
    )
    config_path = config.save(root / "instance_config.yaml")
    known = SimpleNamespace(
        self_id="agent_specialist",
        named_agent_type="CodeWritingAgent",
        name="Ada",
        is_executing=True,
        current_task_id="task_child",
        current_task_age_seconds=42,
        top_level_queue_depth=0,
        waiting_top_level_task_count=0,
        deferred_message_count=1,
        last_seen_at="2026-08-06T22:00:00+00:00",
    )
    lead = SimpleNamespace(
        id="agent_team_lead",
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
        task_manager=_TaskManager(owner_id=config.user.canonical_user_id, storage_settings=config.manager_storage_settings),
        known_agents=SimpleNamespace(agent_map={"agent_specialist": known}),
    )
    set_agent_ref(lead)
    yield lead
    set_agent_ref(None)
    access_runtime._runtime_access.cache_clear()


def _requests_dir(agent) -> Path:
    return Path(agent.instance_root) / "runtime" / "restart_requests"


def _decide(agent, call: _Call, choice: AccessReviewChoice = AccessReviewChoice.APPROVE, note: str = "") -> None:
    requests = access_runtime.runtime_requests(agent)
    owner = PersonPrincipal(agent.task_manager.owner_id, TEAM, ("owner",))
    requests.decide(call.reasons[0]["access_request_id"], owner, choice, note=note)
    call.response = {"kind": "access_request_decision", "status": choice.value}


def _approve(agent, call: _Call, note: str = "") -> None:
    _decide(agent, call, AccessReviewChoice.APPROVE, note)


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
    applied = json.loads(asyncio.run(apply_change._tool_func(plan["change_id"], "Load the new model settings", call)))

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

    result = json.loads(asyncio.run(apply_change._tool_func(plan["change_id"], "Reload", call)))

    assert result["status"] == "denied" and result["note"] == "Not during office hours"
    assert not list(_requests_dir(agent).glob("*.json"))
    assert restarts_dispatched_for_task(agent, "task_restart") == []


def test_a_restart_whose_running_state_changed_is_refused(agent) -> None:
    plan = RuntimeRestartService(agent).stage(scope="manager", reason="Reload")
    call = _Call()
    with pytest.raises(InterruptException):
        asyncio.run(apply_change._tool_func(plan["change_id"], "Reload", call))
    _approve(agent, call, note="Only if nothing else is running")
    agent.task_manager.queue_depth = 5

    result = json.loads(asyncio.run(apply_change._tool_func(plan["change_id"], "Reload", call)))

    assert result["status"] == "refused" and "state changed" in result["reason"]
    assert result["note"] == "Only if nothing else is running"
    assert not list(_requests_dir(agent).glob("*.json"))
    stored = access_runtime.runtime_requests(agent).service.store.get_request(call.reasons[0]["access_request_id"])
    assert stored.applied_at_utc is not None and "state changed" in stored.apply_error


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
    result = json.loads(asyncio.run(apply_change._tool_func(plan["change_id"], "Reload saved settings", call)))
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
from tests.test_runtime_restart_service import agent as fixture, _dispatch_approved, _Call, _approve
root, death_at = Path(sys.argv[1]), sys.argv[2]
with pytest.MonkeyPatch.context() as patch:
    generator = fixture.__wrapped__(root, patch)
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
