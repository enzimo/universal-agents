"""File-path block enforcement across chat, operator CLI, restrict_access, and Cedar decisions.

Verifies against the real Cedar evaluator and SQLite store that:
- a FOLDER-kind block on a file path (as created by ``/access block`` and
  ``assistant_cli access block --folder``) refuses ``file_read``, ``file_write``,
  ``file_access.status``, and ``file_access.move_file``, while siblings stay
  readable, the parent folder listing stays allowed, and ``code_access`` omits
  the containing folder (B4);
- normalized ``blocking_policy_ids`` carry a single ``block:<grant_id>`` entry
  with no duplicate, preventing ``hand_on_for_task`` from running on blocked
  files and working for person-side blocks and ``blocks_on`` (C01);
- ``restrict_access(folder=<file path>)`` isolates to the request it was created
  in even when the folder cache is warmed in both requests (C05c, C08);
- pre-blocking a non-existent file path refuses creating it while siblings write
  normally (C05d).
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from access_control.data_folders import DataFolderRegistry, protected_locations
from access_control.entities import AgentPrincipal, PersonPrincipal
from access_control.inbox import StoredAccessInbox
from access_control.operator_commands import list_grants
from access_control.requests import AccessRequests
from assistant_gateway.access_commands import GatewayAccessCommands
from assistant_gateway.access_inbox import GatewayAccessInbox, GatewayAccessInboxSettings
from assistant_gateway.access_setup import MembershipRoles
from assistant_gateway.application.commands.access import AccessCommandHandler, AccessCommandRequest
from assistant_gateway.application.commands.context import GatewayActorContext
from custom_agents import access_runtime, code_access, folder_access
from custom_messages.contracts.access_v2 import (
    AccessLevel,
    DelegationMode,
    GrantKind,
    GrantLifetime,
    GrantV1,
    SubjectKind,
    SubjectRefV1,
    TargetKind,
    TargetRefV1,
)
from custom_tools import file_access
from custom_tools.non_interactive_file_tools import file_read, file_write
from custom_tools.path_policy import PathPolicyError
from custom_tools.providers import access as access_tools
from custom_tools.workspace_scope import current_workspace_scope
from runtime_settings.instance_layout import InstanceLayout
from storage.config import resolve_storage_settings
from tests.broker_access_support import context_signer
from tests.task_work_support import set_agent_ref
from tests.test_workspace_scope_policy import TEAM
from tests.test_workspace_scope_policy import layout as _scope_layout

layout = _scope_layout
BINARY = Path(__file__).resolve().parents[1] / "target" / "debug" / "ua-cedar-authorizer"
pytestmark = pytest.mark.skipif(not BINARY.is_file(), reason="build ua-cedar-authorizer first (cargo build)")

OWNER = PersonPrincipal("owner-1", TEAM, ("owner",))
ALICE = PersonPrincipal("alice", TEAM, ("member",))


def _text(result: dict[str, Any]) -> str:
    return result["content"][0]["text"]


_active_services: list[Any] = []


@pytest.fixture(autouse=True)
def _close_services() -> Any:
    yield
    while _active_services:
        _active_services.pop().cedar.close()


def _make_agent(
    layout: InstanceLayout,
    person: str = "alice",
    *,
    roles: tuple[str, ...] = ("member",),
    agent_type: str = "WebResearchAgent",
    lead: bool = False,
    signed_context: Any | None = None,
    task_id: str = "task-1",
    agent_id: str | None = None,
) -> SimpleNamespace:
    context = signed_context or context_signer().issue(team_id=TEAM, person_id=person, person_roles=roles)
    meta = SimpleNamespace(gateway_context=SimpleNamespace(canonical_user_id=person, task_context=context))
    storage = resolve_storage_settings({"backend": "sqlite", "sqlite": {"path": str(layout.root / "manager.db")}})
    task_manager = SimpleNamespace(
        current_task_id=task_id,
        get_task_info=lambda _tid: SimpleNamespace(meta=meta),
        storage_settings=storage,
    )
    agent = SimpleNamespace(
        id=agent_id or f"agent-{agent_type}-{task_id}",
        name=agent_type,
        named_agent_type=agent_type,
        team_id=TEAM,
        is_team_lead=lead,
        access_role="team_lead" if lead else "worker",
        instance_root=str(layout.root),
        workspace_dir=str(layout.workspaces_dir),
        authorization_config={"enabled": True, "cedar_binary_path": str(BINARY)},
        task_manager=task_manager,
        test_task_id=task_id,
    )
    service = access_runtime.runtime_access(agent).service
    if service is not None and service not in _active_services:
        _active_services.append(service)
    return agent


def _grant(
    *,
    subject: SubjectRefV1,
    target: TargetRefV1,
    level: AccessLevel = AccessLevel.FULL,
    kind: GrantKind = GrantKind.ACCESS,
    lifetime: GrantLifetime = GrantLifetime.UNTIL_REVOKED,
    for_person_id: str | None = None,
    delegation_depth: int = 0,
    delegation_mode: DelegationMode = DelegationMode.ASK_OWNER,
) -> GrantV1:
    return GrantV1(
        grant_id="new",
        team_id=TEAM,
        kind=kind,
        subject=subject,
        target=target,
        level=level,
        lifetime=lifetime,
        for_person_id=for_person_id,
        delegation_depth=delegation_depth,
        delegation_mode=delegation_mode,
        granted_by=SubjectRefV1(kind=SubjectKind.USER, id=OWNER.person_id),
        reason="Test access setup.",
        created_at_utc=datetime.now(timezone.utc),
    )


class _Identity:
    def __init__(self, roles: dict[str, str]) -> None:
        self._roles = roles

    def get_gateway_user(self, person_id: str) -> Any:
        return SimpleNamespace(status="active") if person_id in self._roles else None

    def list_team_memberships(self, person_id: str, status: str = "active") -> list[Any]:
        role = self._roles.get(person_id)
        return [SimpleNamespace(team_id=TEAM, role=role)] if role else []


class _NoChannelIdentities:
    def canonical_user_id_for_context(self, **_context: Any) -> str | None:
        return None


# -- B4: /access block chat command on a file path ---------------------------


@pytest.mark.asyncio
async def test_slash_access_block_on_file_path_refuses_file_operations_and_sandbox_mount(
    layout: InstanceLayout,
) -> None:
    finance_dir = layout.team_dir / "finance"
    finance_dir.mkdir(parents=True, exist_ok=True)
    payroll = finance_dir / "payroll.csv"
    public = finance_dir / "public.csv"
    payroll.write_text("salary,100000\n", encoding="utf-8")
    public.write_text("quarter,Q4\n", encoding="utf-8")

    storage = resolve_storage_settings({"backend": "sqlite", "sqlite": {"path": str(layout.root / "manager.db")}})

    async def emit(_event: Any) -> None:
        return None

    inbox = GatewayAccessInbox(
        settings=GatewayAccessInboxSettings(
            team_id=TEAM,
            instance_name="test",
            response_route="gateway-route",
            lead_agent_type="TeamArchitect",
            inbox_url="https://ua.example/webchat",
            instance_root=layout.root,
            storage_settings=storage,
            cedar_binary=str(BINARY),
            authorization_enabled=True,
        ),
        memberships=MembershipRoles(_Identity({"owner-1": "owner", "alice": "member"})),
        manager_client=SimpleNamespace(connected=False),
        session_repository=SimpleNamespace(get_session_by_conversation_id=lambda _id: None),
        proactive_session=lambda: None,
        emit_delivery_event=emit,
    )
    try:
        handler = AccessCommandHandler(
            identity=_NoChannelIdentities(),
            access=GatewayAccessCommands(
                requests=inbox.requests,
                person=inbox.person,
                review_url=inbox.review_url,
                authorization_enabled=True,
            ),
        )
        reply = await handler.execute(
            AccessCommandRequest(
                command='/access block agent-type:WebResearchAgent team/finance/payroll.csv --reason "Payroll is private"',
                actor=GatewayActorContext(channel_name="website", canonical_user_id="owner-1"),
            )
        )
        assert reply.startswith("Blocked.")
        assert "the path team/finance/payroll.csv" in reply
        assert "payroll.csv/ folder" not in reply

        agent = _make_agent(layout, "alice")
        set_agent_ref(agent)
        folder_access.forget_kept_answers()

        # Warm the folder cache with both a sibling read and a parent directory listing first.
        sibling_read = file_read(path=str(public))
        assert sibling_read["status"] == "success"
        assert "quarter,Q4" in _text(sibling_read)
        _loc, entries = file_access.list_directory(finance_dir)
        assert any(name == "payroll.csv" for name, _stat in entries)

        # file_read on the blocked file is refused and names the blocked file path.
        blocked_read = file_read(path=str(payroll))
        assert (blocked_read["status"], blocked_read["reason_code"]) == ("error", file_access.ACCESS_DENIED)
        assert "salary,100000" not in _text(blocked_read)
        assert "team/finance/payroll.csv" in _text(blocked_read)
        assert "only a person can remove the block" in _text(blocked_read)

        # file_write on the blocked file is refused and preserves content.
        blocked_write = file_write(path=str(payroll), content="overwritten\n")
        assert (blocked_write["status"], blocked_write["reason_code"]) == ("error", file_access.ACCESS_DENIED)
        assert payroll.read_text(encoding="utf-8") == "salary,100000\n"

        # file_access.status (is_file=None) and file_access.move_file (blocked source) are refused.
        with pytest.raises(PathPolicyError) as status_err:
            file_access.status(payroll)
        assert status_err.value.reason_code == file_access.ACCESS_DENIED

        with pytest.raises(PathPolicyError) as move_err:
            file_access.move_file(payroll, finance_dir / "copy.csv", keep_original=True)
        assert move_err.value.reason_code == file_access.ACCESS_DENIED
        assert not (finance_dir / "copy.csv").exists()

        # Sibling stays readable and parent folder listing stays allowed.
        assert file_read(path=str(public))["status"] == "success"
        file_access.list_directory(finance_dir)

        # code_access omits team/ from sandbox mounts because a block sits inside it.
        plan = code_access.code_access(agent, current_workspace_scope(agent), tool_id=code_access.CODE_SHELL_TOOL_ID)
        mounted = {*plan.read_write, *plan.read_only}
        assert layout.team_dir.resolve() not in {p.resolve() for p in mounted}
        assert finance_dir.resolve() not in {p.resolve() for p in mounted}
        assert any("team/finance/payroll.csv" in note for note in plan.notes)
    finally:
        inbox.close()


# -- B4: assistant_cli access block --folder on a file path ------------------


def test_operator_cli_block_folder_on_file_path_refuses_file_read_until_unblocked(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    import assistant_cli
    from assistant_gateway.bootstrap import seed_default_gateway_user
    from assistant_gateway.config import GatewayInstanceConfig
    from assistant_gateway.store import GatewayStateStore
    from tests.broker_access_support import context_verify_keys

    monkeypatch.setenv("ACCESS_CONTEXT_VERIFY_KEYS", context_verify_keys())
    access_runtime._runtime_access.cache_clear()
    try:
        root = tmp_path / "instance"
        config = GatewayInstanceConfig.create(
            instance_name="cli-file-blocks", instance_root=str(root), team_id="team_cli", timezone="UTC"
        )
        config.authorization.cedar_binary_path = str(BINARY)
        config_path = root / "instance_config.yaml"
        config.save(config_path)
        settings = config.state_storage_settings
        seed_default_gateway_user(
            config, state_store=GatewayStateStore(settings.sqlite_path or "", storage_settings=settings)
        )

        layout = InstanceLayout.at(root)
        layout.ensure_directories()
        layout.person_dir("alice").mkdir(parents=True, exist_ok=True)
        finance_dir = layout.team_dir / "finance"
        finance_dir.mkdir(parents=True, exist_ok=True)
        payroll = finance_dir / "payroll.csv"
        public = finance_dir / "public.csv"
        payroll.write_text("secret-payroll\n", encoding="utf-8")
        public.write_text("public-info\n", encoding="utf-8")

        def run_cli(*argv: str) -> str:
            args = assistant_cli.build_parser().parse_args(["access", *argv, "--config", str(config_path)])
            assistant_cli._access_command(args)
            return capsys.readouterr().out

        out = run_cli(
            "block",
            "--agent-type",
            "WebResearchAgent",
            "--folder",
            "team/finance/payroll.csv",
            "--reason",
            "Payroll is private",
        )
        assert "Blocked " in out

        context = context_signer().issue(team_id="team_cli", person_id="alice", person_roles=("member",))
        meta = SimpleNamespace(gateway_context=SimpleNamespace(canonical_user_id="alice", task_context=context))
        task_manager = SimpleNamespace(
            current_task_id="task-cli",
            get_task_info=lambda _tid: SimpleNamespace(meta=meta),
            storage_settings=config.manager_storage_settings,
        )
        agent = SimpleNamespace(
            id="agent-cli",
            name="WebResearchAgent",
            named_agent_type="WebResearchAgent",
            team_id="team_cli",
            is_team_lead=False,
            access_role="worker",
            instance_root=str(root),
            workspace_dir=str(layout.workspaces_dir),
            authorization_config={"enabled": True, "cedar_binary_path": str(BINARY)},
            task_manager=task_manager,
            test_task_id="task-cli",
        )
        service = access_runtime.runtime_access(agent).service
        assert service is not None
        _active_services.append(service)
        set_agent_ref(agent)
        folder_access.forget_kept_answers()

        assert file_read(path=str(public))["status"] == "success"
        blocked = file_read(path=str(payroll))
        assert (blocked["status"], blocked["reason_code"]) == ("error", file_access.ACCESS_DENIED)
        assert "secret-payroll" not in _text(blocked)

        (block_grant,) = list_grants(service.store, "team_cli")
        assert "Removed " in run_cli("unblock", block_grant.grant_id)
        restored = file_read(path=str(payroll))
        assert restored["status"] == "success"
        assert "secret-payroll" in _text(restored)
    finally:
        set_agent_ref(None)
        access_runtime._runtime_access.cache_clear()


# -- C01: Worker hand-on, person-side block, and blocks_on normalization -----


def test_file_path_block_stops_worker_hand_on_normalizes_policy_id_and_covers_blocks_on(
    layout: InstanceLayout, tmp_path: Path
) -> None:
    agent = _make_agent(layout, "alice")
    set_agent_ref(agent)
    access = access_runtime.runtime_access(agent)
    service = access.service
    assert service is not None
    principal, person, _task_id = access_runtime.task_access(agent, access)

    # Register data/reports, grant Alice read and TeamArchitect automatic delegable read.
    reports_dir = tmp_path / "data_reports"
    reports_dir.mkdir(parents=True, exist_ok=True)
    registry = DataFolderRegistry(service.store, TEAM)
    registry.register(
        OWNER,
        name="reports",
        path=str(reports_dir),
        writable=True,
        reason="Test reports.",
        protected=protected_locations(layout.root),
        now=datetime.now(timezone.utc),
    )
    service.grant_by_person(
        OWNER,
        _grant(
            subject=SubjectRefV1(kind=SubjectKind.USER, id="alice"),
            target=TargetRefV1(kind=TargetKind.FOLDER, id="data/reports"),
            level=AccessLevel.READ,
        ),
    )
    service.grant_by_person(
        OWNER,
        _grant(
            subject=SubjectRefV1(kind=SubjectKind.AGENT_TYPE, id=f"{TEAM}/TeamArchitect"),
            target=TargetRefV1(kind=TargetKind.FOLDER, id="data/reports"),
            level=AccessLevel.READ,
            for_person_id="alice",
            delegation_depth=1,
            delegation_mode=DelegationMode.AUTOMATIC,
        ),
    )

    worker_subject = SubjectRefV1(kind=SubjectKind.AGENT_TYPE, id=f"{TEAM}/WebResearchAgent")
    requests = AccessRequests(service=service, inbox=StoredAccessInbox(service.store), lead_agent_type="TeamArchitect")
    block = requests.block(
        OWNER,
        subject=worker_subject,
        target=TargetRefV1(kind=TargetKind.FOLDER, id="data/reports/payroll.csv"),
        reason="Blocked payroll file",
    )

    version_before = service.store.version(TEAM)
    worker_grants_before = service.store.for_subjects(TEAM, [worker_subject])

    decision = service.decide_folder(
        agent=principal,
        person=person,
        target_id="data/reports/payroll.csv",
        is_file=True,
        level=AccessLevel.READ,
        task_id=principal.work_id,
        hand_on_for_task=True,
    )
    assert not decision.allowed
    assert decision.blocking_policy_ids == (f"block:{block.grant_id}",)
    assert "data/reports/payroll.csv" in decision.reason
    assert "only a person can remove the block" in decision.next_step
    assert "data/reports/payroll.csv" in decision.next_step
    # Because blocking_policy_ids was populated, hand_on_for_task must not have run.
    assert service.store.version(TEAM) == version_before
    assert service.store.for_subjects(TEAM, [worker_subject]) == worker_grants_before

    # blocks_on reports the grant id for both FILE and FOLDER targets without duplicates.
    assert service.blocks_on(
        principal, TargetRefV1(kind=TargetKind.FILE, id="data/reports/payroll.csv"), person_id="alice"
    ) == (block.grant_id,)
    assert service.blocks_on(
        principal, TargetRefV1(kind=TargetKind.FOLDER, id="data/reports/payroll.csv"), person_id="alice"
    ) == (block.grant_id,)


def test_person_and_team_file_path_blocks_refuse_file_with_normalized_id_and_exact_path(
    layout: InstanceLayout,
) -> None:
    agent = _make_agent(layout, "alice")
    set_agent_ref(agent)
    access = access_runtime.runtime_access(agent)
    service = access.service
    assert service is not None
    principal, person, _task_id = access_runtime.task_access(agent, access)
    requests = AccessRequests(service=service, inbox=StoredAccessInbox(service.store), lead_agent_type="TeamArchitect")

    # Person-side block on User::alice targeting a file path stored as TargetKind.FOLDER.
    person_block = requests.block(
        OWNER,
        subject=SubjectRefV1(kind=SubjectKind.USER, id="alice"),
        target=TargetRefV1(kind=TargetKind.FOLDER, id="team/finance/payroll.csv"),
        reason="Alice cannot view payroll",
    )
    refused_person = service.decide_folder(
        agent=principal,
        person=person,
        target_id="team/finance/payroll.csv",
        is_file=True,
        level=AccessLevel.READ,
        task_id=principal.work_id,
        hand_on_for_task=True,
    )
    assert not refused_person.allowed
    assert refused_person.blocking_policy_ids == (f"block:{person_block.grant_id}",)
    assert "is blocked from team/finance/payroll.csv" in refused_person.reason
    assert "remove the block on team/finance/payroll.csv" in refused_person.next_step
    assert service.blocks_on(person, TargetRefV1(kind=TargetKind.FILE, id="team/finance/payroll.csv")) == (
        person_block.grant_id,
    )
    # Sibling file stays allowed.
    sibling = service.decide_folder(
        agent=principal,
        person=person,
        target_id="team/finance/public.csv",
        is_file=True,
        level=AccessLevel.READ,
        task_id=principal.work_id,
    )
    assert sibling.allowed

    # Team-wide block on Team::TEAM targeting another file path.
    requests.unblock(OWNER, person_block.grant_id)
    team_block = requests.block(
        OWNER,
        subject=SubjectRefV1(kind=SubjectKind.TEAM, id=TEAM),
        target=TargetRefV1(kind=TargetKind.FOLDER, id="team/finance/board.csv"),
        reason="Board minutes restricted",
    )
    refused_team = service.decide_folder(
        agent=principal,
        person=person,
        target_id="team/finance/board.csv",
        is_file=True,
        level=AccessLevel.READ,
        task_id=principal.work_id,
    )
    assert not refused_team.allowed
    assert refused_team.blocking_policy_ids == (f"block:{team_block.grant_id}",)
    assert "team/finance/board.csv" in refused_team.reason


# -- C05c + C08: restrict_access on a file path + warm-cache work_id isolation


def test_restrict_access_on_file_path_isolates_to_its_request_even_with_warm_cache(
    layout: InstanceLayout,
) -> None:
    finance_dir = layout.team_dir / "finance"
    finance_dir.mkdir(parents=True, exist_ok=True)
    payroll = finance_dir / "payroll.csv"
    public = finance_dir / "public.csv"
    payroll.write_text("restricted-payroll\n", encoding="utf-8")
    public.write_text("open-public\n", encoding="utf-8")

    # Two separate requests for Alice (distinct signed context_ids -> distinct principal.work_id).
    req_first = context_signer().issue(team_id=TEAM, person_id="alice", person_roles=("member",))
    req_second = context_signer().issue(team_id=TEAM, person_id="alice", person_roles=("member",))

    lead_first = _make_agent(
        layout, "alice", agent_type="TeamArchitect", lead=True, signed_context=req_first, task_id="task-lead-1"
    )
    # Same worker process (shared agent_id) serving two different requests sequentially so cache isolation
    # depends on the request-identifying key fields (work_id / task_id), not on distinct agent ids.
    shared_worker_id = "agent-WebResearchAgent-shared"
    worker_first = _make_agent(
        layout,
        "alice",
        agent_type="WebResearchAgent",
        signed_context=req_first,
        task_id="task-worker-1",
        agent_id=shared_worker_id,
    )
    worker_second = _make_agent(
        layout,
        "alice",
        agent_type="WebResearchAgent",
        signed_context=req_second,
        task_id="task-worker-2",
        agent_id=shared_worker_id,
    )

    # Give TeamArchitect delegable write on team/finance for Alice so it may restrict the worker there.
    service = access_runtime.runtime_access(lead_first).service
    assert service is not None
    service.grant_by_person(
        OWNER,
        _grant(
            subject=SubjectRefV1(kind=SubjectKind.AGENT_TYPE, id=f"{TEAM}/TeamArchitect"),
            target=TargetRefV1(kind=TargetKind.FOLDER, id="team/finance"),
            level=AccessLevel.WRITE,
            for_person_id="alice",
            delegation_depth=1,
            delegation_mode=DelegationMode.AUTOMATIC,
        ),
    )

    set_agent_ref(lead_first)
    restricted = json.loads(
        access_tools.restrict_access(
            agent_type="WebResearchAgent",
            folder="team/finance/payroll.csv",
            reason="Do not read payroll in request 1",
        )
    )
    assert restricted["status"] == "restricted"
    assert "the path team/finance/payroll.csv" in restricted["message"]
    assert "payroll.csv/ folder" not in restricted["message"]
    # The worker's own listing words the restriction the same way (C13).
    set_agent_ref(worker_first)
    restrictions = json.loads(access_tools.list_my_access())["restrictions"]
    assert [row["name"] for row in restrictions if row["target"] == "team/finance/payroll.csv"] == [
        "the path team/finance/payroll.csv"
    ]

    folder_access.forget_kept_answers()

    # Request 1: warm the folder cache with a sibling read, then attempt the blocked file -> refused.
    set_agent_ref(worker_first)
    assert file_read(path=str(public))["status"] == "success"
    blocked_first = file_read(path=str(payroll))
    assert (blocked_first["status"], blocked_first["reason_code"]) == ("error", file_access.ACCESS_DENIED)
    assert "restricted-payroll" not in _text(blocked_first)

    # Request 2: warm the folder cache with a sibling read, then read payroll -> allowed.
    set_agent_ref(worker_second)
    assert file_read(path=str(public))["status"] == "success"
    allowed_second = file_read(path=str(payroll))
    assert allowed_second["status"] == "success"
    assert "restricted-payroll" in _text(allowed_second)

    # Back in Request 1 on the same worker (same agent_id) with Request 2's unrestricted folder entry still warm ->
    # still refused (proves the request-identifying key fields work_id/task_id keep the cache entries apart).
    set_agent_ref(worker_first)
    still_blocked = file_read(path=str(payroll))
    assert (still_blocked["status"], still_blocked["reason_code"]) == ("error", file_access.ACCESS_DENIED)
    assert "restricted-payroll" not in _text(still_blocked)


# -- C05d: Pre-block on a non-existent file path -----------------------------


def test_pre_block_on_missing_file_path_refuses_write_while_sibling_succeeds(layout: InstanceLayout) -> None:
    finance_dir = layout.team_dir / "finance"
    finance_dir.mkdir(parents=True, exist_ok=True)
    future_blocked = finance_dir / "future_secret.csv"
    allowed_sibling = finance_dir / "allowed.csv"
    assert not future_blocked.exists()

    agent = _make_agent(layout, "alice")
    service = access_runtime.runtime_access(agent).service
    assert service is not None
    requests = AccessRequests(service=service, inbox=StoredAccessInbox(service.store), lead_agent_type="TeamArchitect")
    requests.block(
        OWNER,
        subject=SubjectRefV1(kind=SubjectKind.AGENT_TYPE, id=f"{TEAM}/WebResearchAgent"),
        target=TargetRefV1(kind=TargetKind.FOLDER, id="team/finance/future_secret.csv"),
        reason="Pre-block sensitive output file",
    )

    set_agent_ref(agent)
    folder_access.forget_kept_answers()

    # Warm folder cache by writing a sibling first.
    wrote_sibling = file_write(path=str(allowed_sibling), content="ok\n")
    assert wrote_sibling["status"] == "success"
    assert allowed_sibling.read_text(encoding="utf-8") == "ok\n"

    # Attempting to create the pre-blocked file is refused and nothing is written to disk.
    refused = file_write(path=str(future_blocked), content="should-not-write\n")
    assert (refused["status"], refused["reason_code"]) == ("error", file_access.ACCESS_DENIED)
    assert not future_blocked.exists()


# -- C12: Parent-folder block refusal names the blocking folder --------------


def test_parent_folder_block_refusal_names_the_blocking_folder(layout: InstanceLayout) -> None:
    q1_dir = layout.team_dir / "finance" / "q1"
    q1_dir.mkdir(parents=True, exist_ok=True)
    report = q1_dir / "report.csv"
    report.write_text("q1-numbers\n", encoding="utf-8")

    worker_agent = _make_agent(layout, "alice", agent_type="WebResearchAgent")
    service = access_runtime.runtime_access(worker_agent).service
    assert service is not None
    requests = AccessRequests(service=service, inbox=StoredAccessInbox(service.store), lead_agent_type="TeamArchitect")

    # Agent-side block on team/finance: reading team/finance/q1/report.csv names both the file and the block target.
    agent_block = requests.block(
        OWNER,
        subject=SubjectRefV1(kind=SubjectKind.AGENT_TYPE, id=f"{TEAM}/WebResearchAgent"),
        target=TargetRefV1(kind=TargetKind.FOLDER, id="team/finance"),
        reason="Finance folder is restricted",
    )
    worker = AgentPrincipal("agent-1", "WebResearchAgent", "worker", TEAM, work_id="req-parent-1")
    alice = PersonPrincipal("alice", TEAM, ("member",))
    refused_agent = service.decide_folder(
        agent=worker,
        person=alice,
        target_id="team/finance/q1/report.csv",
        is_file=True,
        level=AccessLevel.READ,
        task_id="req-parent-1",
    )
    assert refused_agent.allowed is False
    assert refused_agent.blocking_policy_ids == (f"block:{agent_block.grant_id}",)
    assert "team/finance/q1/report.csv" in refused_agent.reason
    assert "by a block on team/finance" in refused_agent.reason
    assert "A block on team/finance stops WebResearchAgent from using team/finance/q1/report.csv" in refused_agent.next_step

    # Person-side block on team/finance: tells administrators to remove the block on team/finance (not the file).
    requests.unblock(OWNER, agent_block.grant_id)
    person_block = requests.block(
        OWNER,
        subject=SubjectRefV1(kind=SubjectKind.USER, id="alice"),
        target=TargetRefV1(kind=TargetKind.FOLDER, id="team/finance"),
        reason="Alice cannot read finance",
    )
    refused_person = service.decide_folder(
        agent=worker,
        person=alice,
        target_id="team/finance/q1/report.csv",
        is_file=True,
        level=AccessLevel.READ,
        task_id="req-parent-2",
    )
    assert refused_person.allowed is False
    assert refused_person.blocking_policy_ids == (f"block:{person_block.grant_id}",)
    assert "blocked from team/finance/q1/report.csv by a block on team/finance" in refused_person.reason
    assert "remove the block on team/finance for them" in refused_person.next_step
    assert "remove the block on team/finance/q1/report.csv" not in refused_person.next_step
