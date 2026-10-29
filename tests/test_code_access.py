"""What agent code may reach, decided per command (custom_agents.code_access).

Real Cedar, SQLite, TaskManager and Linux Landlock: folders are mounted from
the agent's and person's access, the network is open by default and closed by
a block on internet access, a folder with a blocked subfolder is left out, and running
outside the sandbox needs an approved code:unconfined request, which changes
the next command.
"""

from __future__ import annotations

import contextvars

import json
import socket
import threading
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from access_control.folders import DataFolder
from access_control.standard_grants import INTERNET_SET, INTERNET_TOOL_ID
from custom_agents import access_request_runtime, access_runtime, folder_access
from custom_agents.code_access import (
    CODE_SHELL_TOOL_ID,
    REASON_UNCONFINED_UNAVAILABLE,
    CodeAccessRefused,
    code_access,
)
from custom_agents.task_manager import TaskManager
from custom_messages.contracts.access_v2 import (
    AccessLevel,
    AccessReviewChoice,
    GrantKind,
    TargetKind,
    TargetRefV1,
)
from custom_messages.contracts.routing_v1 import MessageHeader, MessageMeta
from custom_messages.contracts.task_v1 import Task
from custom_messages.enums import MessageMetaType
from custom_tools import code_sandbox
from custom_tools.code_sandbox import landlock_abi
from custom_tools.non_interactive_shell import _execute_single_command
from custom_tools.providers import access as access_tools
from tests.task_work_support import bind_task, set_agent_ref
from custom_tools.workspace_scope import current_workspace_scope
from runtime_settings.code_sandbox_contract import ENV_CODE_SANDBOX_MODE
from runtime_settings.instance_layout import InstanceLayout
from tests.broker_access_support import context_signer, context_verify_keys
from tests.data_folder_support import store_data_folder
from tests.test_access_request_tools import BINARY, OWNER, TEAM, Context, owner_service
from tests.test_access_service import spec

WORKER_TYPE = "WebResearchAgent"
WORKER_SUBJECT = ("agent_type", f"{TEAM}/{WORKER_TYPE}")

pytestmark = pytest.mark.skipif(not BINARY.is_file(), reason="build ua-cedar-authorizer first (cargo build)")
requires_network_landlock = pytest.mark.skipif(
    landlock_abi() < code_sandbox.NETWORK_LANDLOCK_ABI, reason="Landlock network limits need ABI 4"
)


@pytest.fixture
def world(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    layout = InstanceLayout.at(tmp_path / "instance")
    layout.ensure_directories()
    (layout.root / "team.env").write_text("SECRET=runtime-secret\n", encoding="utf-8")
    monkeypatch.setenv("AGENT_WORKSPACE_DIR", str(layout.workspaces_dir))
    monkeypatch.setenv("TEAM_INSTANCE_ROOT", str(layout.root))
    monkeypatch.setenv("ACCESS_CONTEXT_VERIFY_KEYS", context_verify_keys())
    monkeypatch.delenv(ENV_CODE_SANDBOX_MODE, raising=False)
    monkeypatch.setattr(code_sandbox, "_status_logged", False)
    access_runtime._runtime_access.cache_clear()
    folder_access.forget_kept_answers()
    manager = TaskManager(db_path=str(layout.root / "manager.db"))
    manager.set_owner_identity(f"agent-{WORKER_TYPE}", WORKER_TYPE, TEAM, WORKER_TYPE)
    meta = MessageMeta(conversation_id="conv-1")
    meta.gateway_context = SimpleNamespace(
        task_context=context_signer().issue(team_id=TEAM, person_id=OWNER.person_id, person_roles=("owner",))
    )
    manager.add_task(Task(task_id="task-1", task_type=MessageMetaType.REQUEST), MessageHeader(), meta, owner_person_id=None)
    agent = SimpleNamespace(
        id=f"agent-{WORKER_TYPE}",
        name=WORKER_TYPE,
        named_agent_type=WORKER_TYPE,
        team_id=TEAM,
        is_team_lead=False,
        access_role="worker",
        authorization_config={"enabled": True, "cedar_binary_path": str(BINARY)},
        task_manager=manager,
        instance_root=str(layout.root),
        workspace_dir=str(layout.workspaces_dir),
        test_task_id="task-1",
        _interrupted_turn_sessions={},
        paused=False,
        is_executing=False,
        main_lane_busy=False,  # the main lane (runtime_hosting.agent_execution)
        _continue_pending_work=AsyncMock(),
    )
    set_agent_ref(agent)
    service = owner_service(manager.storage_settings)
    yield SimpleNamespace(layout=layout, agent=agent, service=service)
    service.cedar.close()
    set_agent_ref(None)
    access_runtime._runtime_access.cache_clear()


def _give_worker(world, target: TargetRefV1, level: AccessLevel, **fields) -> None:
    from custom_messages.contracts.access_v2 import SubjectKind, SubjectRefV1

    world.service.grant_by_person(
        OWNER, spec(SubjectRefV1(kind=SubjectKind.AGENT_TYPE, id=WORKER_SUBJECT[1]), target, level, **fields)
    )


def _run(world, command: str, **options) -> dict:
    home = current_workspace_scope(world.agent).home
    home.mkdir(parents=True, exist_ok=True)
    return _execute_single_command(command, default_work_dir=str(home), default_timeout=30, **options)


@pytest.fixture
def listener():
    server = socket.create_server(("127.0.0.1", 0))
    server.settimeout(5)

    def accept() -> None:
        try:
            connection, _ = server.accept()
            connection.close()
        except OSError:
            pass

    thread = threading.Thread(target=accept, daemon=True)
    thread.start()
    yield server.getsockname()[1]
    server.close()


CONNECT = (
    "python3 -c \"import os,socket; socket.create_connection((os.environ['T_HOST'], int(os.environ['T_PORT'])), "
    "timeout=3); print('connected')\""
)


@requires_network_landlock
def test_the_network_is_open_by_default_and_closed_by_a_block_on_internet_access(world, listener) -> None:
    """Decision D14: internet access is standard; a block on it closes the sandbox's network."""

    from custom_messages.contracts.access_v2 import SubjectKind, SubjectRefV1

    env = {"T_HOST": "127.0.0.1", "T_PORT": str(listener)}

    def assert_closed() -> None:
        closed = _run(world, CONNECT, extra_env=env)
        assert closed["status"] == "error" and "connected" not in closed["output"]
        assert "Permission denied" in closed["error"] or "PermissionError" in closed["error"]

    opened = _run(world, CONNECT, extra_env=env)
    assert opened["status"] == "success", opened
    assert "connected" in opened["output"]

    requests = access_runtime.runtime_requests(world.agent)
    worker_block = requests.block(
        OWNER,
        subject=SubjectRefV1(kind=SubjectKind.AGENT_TYPE, id=WORKER_SUBJECT[1]),
        target=TargetRefV1(kind=TargetKind.TOOL, id=INTERNET_TOOL_ID),
        reason="No downloads for this worker",
    )
    assert_closed()

    requests.unblock(OWNER, worker_block.grant_id)
    assert _run(world, CONNECT, extra_env=env)["status"] == "success"

    requests.block(
        OWNER,
        subject=SubjectRefV1(kind=SubjectKind.TEAM, id=TEAM),
        target=TargetRefV1(kind=TargetKind.TOOL_SET, id=INTERNET_SET),
        reason="Internet off for the whole team",
    )
    assert_closed()


@requires_network_landlock
def test_parallel_commands_get_the_same_access_as_sequential_ones(world, listener, monkeypatch) -> None:
    """shell(parallel=true) runs commands in pool threads, which do not inherit context
    variables. They used to run without the agent: internet on despite a person's block
    and no person folder. Each parallel command must get the access a sequential one gets."""

    from custom_messages.contracts.access_v2 import SubjectKind, SubjectRefV1
    from custom_tools import non_interactive_shell

    access_runtime.runtime_requests(world.agent).block(
        OWNER,
        subject=SubjectRefV1(kind=SubjectKind.AGENT_TYPE, id=WORKER_SUBJECT[1]),
        target=TargetRefV1(kind=TargetKind.TOOL, id=INTERNET_TOOL_ID),
        reason="No downloads for this worker",
    )
    # The loopback policy refuses a literal 127.0.0.1 in a command, so the target
    # travels in the environment the shell passes on.
    monkeypatch.setenv("T_HOST", "127.0.0.1")
    monkeypatch.setenv("T_PORT", str(listener))
    planned: list = []
    run_in_sandbox = non_interactive_shell._run_in_sandbox

    def recording(cmd_str, plan, grants, **options):
        planned.append(grants)
        return run_in_sandbox(cmd_str, plan, grants, **options)

    monkeypatch.setattr(non_interactive_shell, "_run_in_sandbox", recording)
    home = current_workspace_scope(world.agent).home
    home.mkdir(parents=True, exist_ok=True)

    def run(parallel: bool) -> list[dict]:
        return non_interactive_shell._execute_commands(
            [CONNECT, "pwd"], parallel=parallel, ignore_errors=True, work_dir=str(home), timeout=30
        )

    sequential = run(parallel=False)
    sequential_access = list(planned)
    planned.clear()
    parallel = run(parallel=True)

    for results in (sequential, parallel):
        connect, pwd = results
        assert connect["status"] == "error" and "connected" not in connect["output"], connect
        assert pwd["status"] == "success" and pwd["output"].strip() == str(home), pwd
    assert len(sequential_access) == 2 and planned == sequential_access
    assert all(access.network is False for access in planned)
    assert all(home in access.read_write for access in planned)


def test_a_command_list_keeps_the_person_it_started_with(world, monkeypatch) -> None:
    """A shell call keeps the task of the turn it belongs to (runtime_context.task_work).

    Each further command of a list used to read the agent's current task, so a
    call still running when the agent moved on ran its later commands for the
    next task's person and folders. Another turn now binds its own task in its
    own context, which the running call never sees.
    """

    from custom_tools import non_interactive_shell
    from custom_tools.non_interactive_shell import shell
    from custom_tools.tool_stop import ToolStop
    from tests.tool_stop_support import tool_context

    world.agent.task_manager.add_task(
        Task(task_id="task-2", task_type=MessageMetaType.REQUEST), MessageHeader(), MessageMeta(), owner_person_id=None
    )
    planned: list = []
    run_in_sandbox = non_interactive_shell._run_in_sandbox

    def recording(cmd_str, plan, grants, **options):
        planned.append(grants)
        result = run_in_sandbox(cmd_str, plan, grants, **options)
        # Another turn of the agent starts its next task, in its own context.
        contextvars.copy_context().run(bind_task, "task-2")
        return result

    monkeypatch.setattr(non_interactive_shell, "_run_in_sandbox", recording)
    home = current_workspace_scope(world.agent).home
    home.mkdir(parents=True, exist_ok=True)
    stop = ToolStop(agent_id=world.agent.id, task_id="task-1")

    result = shell._tool_func(command=["pwd", "pwd"], tool_context=tool_context(stop), work_dir=str(home))

    assert result["status"] == "success", result
    assert len(planned) == 2 and planned[0] == planned[1]
    assert home in planned[1].read_write


def test_a_folder_with_a_blocked_subfolder_stays_out_of_the_sandbox(world) -> None:
    finance = world.layout.team_dir / "finance"
    finance.mkdir()
    (finance / "budget.md").write_text("numbers", encoding="utf-8")
    scope = current_workspace_scope(world.agent)
    assert world.layout.team_dir in code_access(world.agent, scope, tool_id=CODE_SHELL_TOOL_ID).read_write

    _give_worker(world, TargetRefV1(kind=TargetKind.FOLDER, id="team/finance"), AccessLevel.FULL, kind=GrantKind.BLOCK)
    planned = code_access(world.agent, scope, tool_id=CODE_SHELL_TOOL_ID)

    assert world.layout.team_dir not in planned.read_write + planned.read_only
    assert world.layout.agents_dir in planned.read_write
    assert any("team/finance" in note for note in planned.notes)
    if landlock_abi() >= 1:
        result = _run(world, f"cat {finance / 'budget.md'}")
        assert result["status"] == "error" and "numbers" not in result["output"]
        assert "team/finance" in result["sandbox_note"]


def test_a_read_grant_on_a_data_folder_mounts_it_read_only(world, tmp_path: Path) -> None:
    reports = tmp_path / "reports"
    (reports / "2026").mkdir(parents=True)
    (reports / "2026" / "q1.md").write_text("quarter", encoding="utf-8")
    folders = (DataFolder("reports", reports),)
    scope = current_workspace_scope(world.agent)
    assert reports not in code_access(world.agent, scope, tool_id=CODE_SHELL_TOOL_ID, data_folders=folders).read_only

    _give_worker(world, TargetRefV1(kind=TargetKind.FOLDER, id="data/reports/2026"), AccessLevel.READ)
    planned = code_access(world.agent, scope, tool_id=CODE_SHELL_TOOL_ID, data_folders=folders)

    assert planned.read_only == (reports / "2026",)
    assert reports not in planned.read_write


@pytest.mark.skipif(landlock_abi() < 1, reason="Linux Landlock is not available on this kernel")
def test_running_outside_the_sandbox_needs_an_approved_request_which_changes_the_next_command(world) -> None:
    secret = world.layout.root / "team.env"
    # Built at run time so the command-text checks cannot see the path.
    read_secret = f"python3 -c \"print(open('{str(secret)[:8]}' + '{str(secret)[8:]}').read())\""

    refused = _run(world, read_secret, unconfined=True)
    assert refused["status"] == "error" and refused["reason_code"] == "access_denied"
    denial = json.loads(refused["error"])
    assert denial["tool_id"] == "code:unconfined" and "request_access" in denial["next_step"]
    sandboxed = _run(world, read_secret)
    assert "runtime-secret" not in sandboxed["output"]

    context = Context()
    with pytest.raises(Exception):  # the task pauses until a person decides
        access_tools.request_access(
            tool_id="code:unconfined", level="full", reason="Install a system package", tool_context=context
        )
    requests = access_runtime.runtime_requests(world.agent)
    request_id = context.raised[0]["access_request_id"]
    requests.decide(request_id, OWNER, AccessReviewChoice.APPROVE_TASK)
    context.responses[access_tools.ACCESS_REQUEST_INTERRUPT_NAME] = access_request_runtime.decision_response(
        requests.service.store.get_request(request_id)
    )
    resumed = json.loads(
        access_tools.request_access(
            tool_id="code:unconfined", level="full", reason="Install a system package", tool_context=context
        )
    )
    assert resumed["status"] == "granted"

    unconfined = _run(world, read_secret, unconfined=True)
    assert unconfined["status"] == "success", unconfined
    assert "runtime-secret" in unconfined["output"]
    assert unconfined["sandbox"] == code_sandbox.CODE_SANDBOX_UNCONFINED
    # Only the command that asks runs outside the sandbox.
    assert "runtime-secret" not in _run(world, read_secret)["output"]


def test_with_authorization_off_only_the_operator_can_turn_the_sandbox_off(world) -> None:
    world.agent.authorization_config = {"enabled": False}
    scope = current_workspace_scope(world.agent)

    with pytest.raises(CodeAccessRefused) as refused:
        code_access(world.agent, scope, tool_id=CODE_SHELL_TOOL_ID, unconfined=True)
    planned = code_access(world.agent, scope, tool_id=CODE_SHELL_TOOL_ID)

    assert refused.value.reason_code == REASON_UNCONFINED_UNAVAILABLE
    assert "security_layer.code_execution.sandbox" in str(refused.value)
    assert planned.network is True and set(planned.read_write) == set(scope.roots)


def test_file_tools_reach_a_data_folder_only_as_far_as_the_grant_goes(world, tmp_path: Path) -> None:
    from custom_tools.non_interactive_file_tools import file_read, file_write

    reports = tmp_path / "reports"
    (reports / "2026").mkdir(parents=True)
    (reports / "2026" / "q1.md").write_text("quarter", encoding="utf-8")
    store_data_folder(world.agent.task_manager.storage_settings, "reports", reports, team_id=TEAM)

    refused = file_read(path=str(reports / "2026" / "q1.md"))
    assert refused["reason_code"] == "access_denied"
    assert "folder='data/reports/2026'" in refused["content"][0]["text"]

    _give_worker(world, TargetRefV1(kind=TargetKind.FOLDER, id="data/reports/2026"), AccessLevel.READ)
    assert file_read(path=str(reports / "2026" / "q1.md"))["status"] == "success"
    written = file_write(path=str(reports / "2026" / "q2.md"), content="next")
    assert written["reason_code"] == "access_denied"
    assert not (reports / "2026" / "q2.md").exists()


def test_what_another_person_shared_reaches_the_agent_through_file_tools_and_the_sandbox(world) -> None:
    """Decision D15: a share from Alice to the task's person is usable at the shared level only."""

    from access_control.entities import PersonPrincipal
    from custom_messages.contracts.access_v2 import GrantV1, SubjectKind, SubjectRefV1
    from custom_tools.non_interactive_file_tools import file_read, file_write
    from runtime_settings.instance_layout import person_folder_name

    alice = PersonPrincipal("alice", TEAM, ("member",))
    reports = world.layout.person_dir("alice") / "reports"
    reports.mkdir(parents=True)
    (reports / "q1.md").write_text("quarter one", encoding="utf-8")
    (world.layout.person_dir("alice") / "diary.md").write_text("private", encoding="utf-8")
    shared_id = f"users/{person_folder_name('alice')}/reports"

    assert file_read(path=str(reports / "q1.md"))["reason_code"] == "access_denied"
    world.service.grant_by_person(
        alice,
        GrantV1(
            grant_id="new",
            team_id=TEAM,
            subject=SubjectRefV1(kind=SubjectKind.USER, id=OWNER.person_id),
            target=TargetRefV1(kind=TargetKind.FOLDER, id=shared_id),
            level=AccessLevel.READ,
            granted_by=SubjectRefV1(kind=SubjectKind.USER, id="alice"),
            created_at_utc=world.service.clock(),
        ),
    )
    folder_access.forget_kept_answers()

    assert file_read(path=str(reports / "q1.md"))["status"] == "success"
    assert file_write(path=str(reports / "q2.md"), content="x")["reason_code"] == "access_denied"
    assert file_read(path=str(world.layout.person_dir("alice") / "diary.md"))["reason_code"] == "access_denied"
    planned = code_access(world.agent, current_workspace_scope(world.agent), tool_id=CODE_SHELL_TOOL_ID)
    assert reports in planned.read_only and reports not in planned.read_write
