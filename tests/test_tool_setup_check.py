"""The setup check behind ``set_up_tools``: is everything set up for the tools a request needs?

Real Cedar, SQLite, TaskManager, the real broker app and its access gate, and
the broker's file-backed credential state. Covered:

- the broker's ``tool_setup.status`` action reports the person's sign-ins
  (with the account name), their own and the team's account links, and the
  team's sign-in apps, and never a token, client secret or credential key;
  a worker cannot call it (it is the team lead's);
- TeamLead's tool combines that with access for itself, a worker and the
  person, and gives each gap's next step and who can do it: a sign-in or
  account link the person can do, the team's sign-in app (a setup request for
  an administrator, a plain explanation for anyone else), and access to ask
  for;
- with authorization off access is not needed, and without a broker the
  account part is reported as unknown.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from aiohttp.test_utils import TestClient, TestServer

from access_catalog.catalog import CatalogSource
from access_control.cedar import CedarAccessClient
from access_control.decisions import AccessService
from access_control.entities import agent_role_resolver
from access_control.identity import ContextVerifier
from custom_agents import access_runtime
from custom_agents.task_manager import TaskManager
from custom_agents.tool_setup_runs import needs_workflow, plan_run
from custom_messages.contracts.access_v2 import LinkOwnerKind
from custom_messages.contracts.routing_v1 import MessageHeader, MessageMeta
from custom_messages.contracts.task_v1 import Task
from custom_messages.enums import MessageMetaType
from access_control.identity import IdentityRejected
from access_control.people import is_administrator
from custom_tools.tool_setup_check import check_tools
from tests.task_work_support import set_agent_ref
from secure_tool_broker import executors, google_workspace, x_sign_in
from secure_tool_broker.access_gate import BrokerAccessGate
from secure_tool_broker.auth_models import AuthStatus
from secure_tool_broker.service import SecureToolBrokerService
from storage.access_repository import AccessRepository
from storage.config import resolve_storage_settings
from tests.broker_access_support import (
    BROKER_ROOT,
    agent_context,
    caller_headers,
    caller_token,
    context_signer,
    context_verify_keys,
)
from tests.test_account_links import _isolated_credentials, link  # noqa: F401 - autouse fixture

ROOT = Path(__file__).resolve().parents[1]
BINARY = ROOT / "target" / "debug" / "ua-cedar-authorizer"
TEAM = "team-1"
PERSON = "user-1"
GOOGLE_TEAM_APP = json.dumps({"installed": {"client_id": "team-app.apps.googleusercontent.com", "client_secret": "s1"}})
SECRETS = ("x-access-secret", "x-refresh-secret", "x-app-secret", "s1", "gho_secret_token")

pytestmark = pytest.mark.skipif(not BINARY.is_file(), reason="build ua-cedar-authorizer first (cargo build)")


@pytest.fixture
def world(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """The manager's storage, the broker with its gate (authorization on), and account states."""

    monkeypatch.setenv("ACCESS_CONTEXT_VERIFY_KEYS", context_verify_keys())
    access_runtime._runtime_access.cache_clear()
    storage = resolve_storage_settings({"backend": "sqlite", "sqlite": {"path": str(tmp_path / "manager.db")}})
    store = AccessRepository(storage)
    catalogs = CatalogSource(None)
    service = AccessService(
        store=store,
        catalog=catalogs.current(),
        cedar=CedarAccessClient(binary=str(BINARY)),
        team_id=TEAM,
        agent_role_of=agent_role_resolver("TeamArchitect"),
    )
    gate = BrokerAccessGate(
        catalogs=catalogs,
        verifier=ContextVerifier.from_value(context_verify_keys()),
        team_id=TEAM,
        access=service,
        links=store,
    )
    real_status = executors.get_auth_status

    def auth_status(provider: str, *, team_id: str | None = None) -> AuthStatus:
        # gh and gog check with GitHub and Google; stand in for those checks only.
        if provider == "github":
            return AuthStatus(
                provider="github",
                status="authenticated",
                authenticated=True,
                message="GitHub CLI authentication is available to the secure tool broker.",
                details={"stdout": "github.com\n  ✓ Logged in to github.com account exampleuser (GH_TOKEN)"},
            )
        if provider == "google":
            return AuthStatus(provider="google", status="auth_required", authenticated=False, message="Sign in.")
        return real_status(provider, team_id=team_id)

    monkeypatch.setattr(executors, "get_auth_status", auth_status)
    yield SimpleNamespace(storage=storage, store=store, gate=gate, service=service, tmp_path=tmp_path)
    service.cedar.close()
    access_runtime._runtime_access.cache_clear()
    set_agent_ref(None)


def _sign_in_to_x(person_id: str = PERSON) -> None:
    client = x_sign_in.XClient(
        client_id="x-app-client", redirect_uri="https://broker.example.com/auth/x/callback", client_secret="x-app-secret"
    )
    assert x_sign_in.store_team_client(TEAM, client, stored_by="admin-1").status == "success"
    record = x_sign_in.sign_in_record(
        client,
        {"access_token": "x-access-secret", "refresh_token": "x-refresh-secret", "expires_in": 7200},
        "exampleuser",
        now=0,
    )
    assert x_sign_in.store_person_sign_in(person_id, record).status == "success"


def _status_body(tool_ids: list[str], *, agent_type: str = "TeamArchitect", roles=("owner",)) -> dict:
    return {
        "provider": "tool_setup",
        "action": "status",
        "params": {"tool_ids": tool_ids},
        "reason": "Check what the request needs.",
        "context": agent_context(PERSON, named_agent_type=agent_type, agent_id=f"agent-{agent_type}", roles=roles),
    }


@pytest.mark.asyncio
async def test_the_broker_reports_sign_ins_links_and_team_apps_without_secrets(world) -> None:
    _sign_in_to_x()
    link(world.store, LinkOwnerKind.TEAM, TEAM, "team_hf", tool_id="cli:huggingface")
    service = SecureToolBrokerService(token=BROKER_ROOT, access_gate=world.gate)
    async with TestClient(TestServer(service.build_app())) as client:
        response = await client.post(
            "/v1/tools/tool_setup",
            headers=caller_headers(),
            json=_status_body(["cli:x", "cli:github", "cli:google", "cli:huggingface", "http:nowhere"]),
        )
        payload = await response.json()
        worker = await client.post(
            "/v1/tools/tool_setup",
            headers=caller_headers(),
            json=_status_body(["cli:x"], agent_type="WebResearchAgent"),
        )
        worker_text = await worker.text()

    assert response.status == 200 and payload["status"] == "success", payload
    tools = {tool["tool_id"]: tool for tool in payload["content"]["tools"]}
    assert tools["cli:x"]["sign_in"] == {"state": "signed_in", "account": "exampleuser"}
    assert tools["cli:x"]["team_sign_in_app"] == "configured"
    assert tools["cli:x"]["link"] == {"own": False, "team": False}
    assert tools["cli:github"]["sign_in"] == {"state": "signed_in", "account": "exampleuser"}
    assert tools["cli:github"]["team_sign_in_app"] == "not_needed" and "link" not in tools["cli:github"]
    assert tools["cli:google"]["sign_in"] == {"state": "missing"}
    assert tools["cli:google"]["team_sign_in_app"] == "missing"
    assert tools["cli:huggingface"]["link"] == {"own": False, "team": True}
    assert tools["http:nowhere"] == {"tool_id": "http:nowhere", "known": False}
    text = json.dumps(payload)
    assert not any(secret in text for secret in (*SECRETS, "team_hf")), "states and names only"
    # The check is the team lead's: a worker does not hold it.
    assert worker.status == 403 and "tool_setup" in worker_text


def check_tool_setup(tool_ids: list[str], level: str = "read", agent_type: str = "") -> str:
    """The check's answer as TeamLead's tools used to see it (the checks now feed set_up_tools)."""

    try:
        context, reports, problem = check_tools(tool_ids, level, agent_type)
    except (IdentityRejected, ValueError) as exc:
        return json.dumps({"status": "refused", "reason": str(exc)})
    return json.dumps({
        "status": "checked",
        "person_is_administrator": is_administrator(context["person"].roles),
        "tools": reports,
        **({"note": problem} if problem else {}),
    })


def _lead(world, *, roles: tuple[str, ...] = ("owner",), enabled: bool = True, lead: bool = True):
    """TeamLead running a task for PERSON, whose signed context names ``roles``."""

    manager = TaskManager(db_path=str(world.tmp_path / "manager.db"))
    manager.set_owner_identity("agent-TeamArchitect", "TeamArchitect", TEAM, "TeamArchitect")
    signed = context_signer().issue(team_id=TEAM, person_id=PERSON, person_roles=roles)
    meta = MessageMeta(conversation_id="conv-1")
    meta.gateway_context = SimpleNamespace(task_context=signed, canonical_user_id=PERSON)
    manager.add_task(Task(task_id="task-lead", task_type=MessageMetaType.REQUEST), MessageHeader(), meta, owner_person_id=None)
    agent = SimpleNamespace(
        id="agent-TeamArchitect",
        name="TeamArchitect",
        named_agent_type="TeamArchitect",
        team_id=TEAM,
        is_team_lead=lead,
        access_role="team_lead",
        authorization_config={"enabled": enabled, "cedar_binary_path": str(BINARY)},
        task_manager=manager,
        instance_root=None,
        test_task_id="task-lead",
        _interrupted_turn_sessions={},
        paused=False,
        is_executing=False,
        _continue_pending_work=AsyncMock(),
    )
    set_agent_ref(agent)
    return agent


async def _check(world, monkeypatch, **kwargs) -> dict:
    """Call the tool from a thread (it blocks on HTTP) while the broker serves on this loop."""

    service = SecureToolBrokerService(token=BROKER_ROOT, access_gate=world.gate)
    async with TestClient(TestServer(service.build_app())) as client:
        monkeypatch.setenv("SECURE_TOOL_BROKER_INTERNAL_BASE_URL", str(client.make_url("")).rstrip("/"))
        monkeypatch.setenv("SECURE_TOOL_BROKER_TOKEN", caller_token())
        return json.loads(await asyncio.to_thread(check_tool_setup, **kwargs))


def _steps(report: dict) -> dict[str, dict]:
    return {step["step"]: step for step in report["next_steps"]}


@pytest.mark.asyncio
async def test_the_team_lead_sees_every_gap_with_who_can_fix_it(world, monkeypatch) -> None:
    _lead(world)
    link(world.store, LinkOwnerKind.TEAM, TEAM, "team_hf", tool_id="cli:huggingface")

    result = await _check(
        world, monkeypatch, tool_ids=["cli:github", "cli:google", "cli:huggingface"], agent_type="WebResearchAgent"
    )

    assert result["status"] == "checked" and result["person_is_administrator"] is True
    reports = {report["tool_id"]: report for report in result["tools"]}
    github = reports["cli:github"]
    # Signed in (the stand-in gh check says exampleuser), but nobody gave the agents GitHub yet.
    assert github["sign_in"] == {"state": "signed_in", "account": "exampleuser"}
    assert github["access"] == {
        "team_lead": "needs_request",
        "person": "allowed",
        "worker": {"agent_type": "WebResearchAgent", "state": "needs_request"},
    }
    steps = _steps(github)
    # One request: TeamLead's access, which it may hand on to the worker's tasks (workers keep none).
    assert set(steps) == {"team_lead_access"}
    assert steps["team_lead_access"]["hand_on"] is True
    assert steps["team_lead_access"]["call"].startswith("request_access(tool_id='cli:github', level='read', hand_on=True")
    assert github["ready"] is False

    google = _steps(reports["cli:google"])
    assert google["team_sign_in_app"] == {
        "step": "team_sign_in_app",
        "who": "administrator",
        "fix_with": "setup_page",
        "kind": "team_sign_in_app",
        "owner": "team",
        "say": "First the team's Google sign-in app needs setting up once; you're an administrator, so I can "
        "open that page for you.",
    }
    assert google["sign_in"]["fix_with"] == "setup_page" and google["sign_in"]["who"] == "person"

    # The team's Hugging Face account is linked, so only access is missing there.
    huggingface = reports["cli:huggingface"]
    assert huggingface["link"] == {"own": False, "team": True}
    assert "account_link" not in _steps(huggingface)
    # The check refreshed the stored last known states and returns them.
    assert github["known_state"]["state"] == "ready" and github["known_state"]["account_label"] == "exampleuser"
    assert reports["cli:google"]["known_state"]["missing_step"] == "team_sign_in_app"
    assert huggingface["known_state"]["how"] == "team_link"


@pytest.mark.asyncio
async def test_a_members_missing_team_app_is_explained_not_requested(world, monkeypatch) -> None:
    _lead(world, roles=("member",))

    result = await _check(world, monkeypatch, tool_ids=["cli:google", "cli:huggingface"])

    assert result["person_is_administrator"] is False
    reports = {report["tool_id"]: report for report in result["tools"]}
    google = _steps(reports["cli:google"])
    assert google["team_sign_in_app"]["fix_with"] == "tell_person_and_stop"
    assert "/auth google team_client" in google["team_sign_in_app"]["say"]
    # Members do not hold Google Workspace unless someone gives it to them.
    assert google["person_access"]["who"] == "administrator"
    assert reports["cli:google"]["known_state"]["missing_step"] == "access"
    huggingface = _steps(reports["cli:huggingface"])
    assert huggingface["account_link"]["fix_with"] == "setup_page"
    assert huggingface["team_account_link"]["fix_with"] == "tell_person"


@pytest.mark.asyncio
async def test_with_authorization_off_only_the_account_part_is_checked(world, monkeypatch) -> None:
    _lead(world, enabled=False)
    world.gate.access = None  # the broker's gate follows the same switch

    result = await _check(world, monkeypatch, tool_ids=["cli:x"])

    [report] = result["tools"]
    assert report["access"]["team_lead"] == "not_needed"
    assert _steps(report)["sign_in"]["kind"] == "sign_in"
    assert report["team_sign_in_app"] == "missing"


def test_without_a_broker_the_account_part_is_unknown(world, monkeypatch) -> None:
    _lead(world)
    monkeypatch.delenv("SECURE_TOOL_BROKER_INTERNAL_BASE_URL", raising=False)

    result = json.loads(check_tool_setup(tool_ids=["cli:github", "fn:schedules"]))

    assert "cannot be reached" in result["note"]
    reports = {report["tool_id"]: report for report in result["tools"]}
    assert _steps(reports["cli:github"])["account_unknown"]["fix_with"] == "try_the_tool"
    assert "account_unknown" not in _steps(reports["fn:schedules"]), "a function tool needs no account"


def test_a_task_for_nobody_is_refused(world) -> None:
    agent = _lead(world)
    agent.task_manager.get_task_info("task-lead").meta.gateway_context = SimpleNamespace(task_context=None)

    result = json.loads(check_tool_setup(tool_ids=["cli:github"]))

    assert result["status"] == "refused"


def test_google_team_app_is_set_up_by_the_teams_client(world) -> None:
    # A person's own Google client also counts; here the team's is stored.
    from secure_tool_broker.tool_setup_status import _team_app_state
    from secure_tools.auth_methods import tool_sign_in

    assert _team_app_state(tool_sign_in("cli:google"), PERSON, TEAM) == "missing"
    assert google_workspace.store_team_client(TEAM, GOOGLE_TEAM_APP, stored_by="admin-1").status == "success"
    assert _team_app_state(tool_sign_in("cli:google"), PERSON, TEAM) == "configured"


@pytest.mark.asyncio
async def test_two_missing_sign_ins_are_two_pieces_of_one_workflow(world, monkeypatch) -> None:
    """\"Check my GitHub and my Gmail for anything from Acme\" with neither signed in."""

    _lead(world, enabled=False)
    world.gate.access = None
    assert google_workspace.store_team_client(TEAM, GOOGLE_TEAM_APP, stored_by="admin-1").status == "success"
    signed_out = AuthStatus(provider="github", status="auth_required", authenticated=False, message="Sign in.")
    real_status = executors.get_auth_status
    monkeypatch.setattr(
        executors,
        "get_auth_status",
        lambda provider, *, team_id=None: signed_out if provider == "github" else real_status(provider, team_id=team_id),
    )

    result = await _check(world, monkeypatch, tool_ids=["cli:github", "cli:google"])

    plan = plan_run(result["tools"], CatalogSource(None).current(), is_admin=True)
    assert [(piece.tool_id, piece.kind) for piece in plan.pieces] == [("cli:github", "sign_in"), ("cli:google", "sign_in")]
    assert needs_workflow(plan.pieces) and not plan.stops
    # One sign-in alone needs no workflow.
    single = await _check(world, monkeypatch, tool_ids=["cli:github"])
    assert not needs_workflow(plan_run(single["tools"], CatalogSource(None).current(), is_admin=True).pieces)
