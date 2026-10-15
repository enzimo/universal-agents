"""The broker's access gate against the real Cedar evaluator and SQLite store.

Covers: per-process caller tokens, signed task contexts (forged, expired,
unsigned, wrong person, wrong team), agent-type binding for agent processes,
unlisted web hosts in both modes, the agent-plus-person decision with grants,
and the real broker app running ``gog.exec`` with the exact argument list and
gog's own command filter.
"""

from __future__ import annotations

import contextlib
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from aiohttp.test_utils import TestClient, TestServer

from access_catalog.catalog import CatalogSource
from access_control.caller_tokens import (
    CALLER_GATEWAY,
    CALLER_MANAGER,
    BrokerCaller,
    agent_caller_label,
    derive_caller_token,
    verify_caller_token,
)
from access_control.cedar import CedarAccessClient
from access_control.decisions import AccessService
from access_control.entities import PersonPrincipal
from access_control.identity import ISSUER_GATEWAY, ContextSigner, ContextVerifier, generate_signing_key
from custom_messages.contracts.access_v2 import (
    AccessLevel,
    GrantV1,
    SubjectKind,
    SubjectRefV1,
    TargetKind,
    TargetRefV1,
)
from secure_tool_broker.access_gate import (
    REASON_ACCESS_DENIED,
    REASON_IDENTITY_REJECTED,
    REASON_UNLISTED_HOST,
    BrokerAccessGate,
)
from secure_tool_broker.service import SecureToolBrokerService
from storage.access_repository import AccessRepository
from storage.config import resolve_storage_settings
from tests.broker_access_support import (
    BROKER_ROOT,
    TEST_TEAM,
    agent_context,
    caller_headers,
    context_signer,
    context_verify_keys,
    gateway_headers,
    open_gate,
)

ROOT = Path(__file__).resolve().parents[1]
BINARY = ROOT / "target" / "debug" / "ua-cedar-authorizer"
needs_cedar = pytest.mark.skipif(not BINARY.is_file(), reason="build ua-cedar-authorizer first (cargo build)")

MANAGER = BrokerCaller(CALLER_MANAGER)
OWNER = PersonPrincipal("user-1", TEST_TEAM, ("owner",))
MEMBER_ID = "user-2"


def gmail_search(gate: BrokerAccessGate, context: dict, caller: BrokerCaller = MANAGER):
    return gate.check_broker_action(caller, "google", "gog.exec", {"argv": ["gmail", "search", "x"]}, context)


# -- caller tokens -----------------------------------------------------------------


def test_caller_tokens_name_one_process_and_reject_tampering() -> None:
    token = derive_caller_token("root", agent_caller_label("ResearchAgent"))

    caller = verify_caller_token("root", token)
    assert caller is not None and caller.agent_type == "ResearchAgent" and caller.runs_agents
    assert verify_caller_token("other-root", token) is None
    assert verify_caller_token("root", token.replace("ResearchAgent", "TeamArchitect")) is None
    assert verify_caller_token("root", "root") is None, "the root secret is not a caller token"
    assert verify_caller_token("root", None) is None
    gateway = verify_caller_token("root", derive_caller_token("root", CALLER_GATEWAY))
    assert gateway is not None and gateway.is_gateway and not gateway.runs_agents
    with pytest.raises(ValueError):
        derive_caller_token("root", "agent/../../etc")


# -- identity (both modes) ---------------------------------------------------------


def test_open_gate_allows_a_verified_agent_request() -> None:
    result = gmail_search(open_gate(), agent_context())

    assert result.allowed and result.person_id == "user-1"
    assert result.resolution is not None and result.resolution.level is AccessLevel.READ


@pytest.mark.parametrize(
    ("change", "fragment"),
    [
        (lambda c: c.pop("task_context"), "no signed context"),
        (lambda c: c.update(canonical_user_id="user-9"), "different person"),
        (lambda c: c.update(team_id="team-other"), "another team"),
        (lambda c: c.pop("agent_id"), "does not name the agent"),
        (lambda c: c["task_context"]["context"].update(person_id="user-9"), "changed or forged"),
    ],
)
def test_gate_refuses_unverified_identity(change, fragment) -> None:
    context = agent_context()
    change(context)

    result = gmail_search(open_gate(), context)

    assert not result.allowed and result.reason_code == REASON_IDENTITY_REJECTED
    assert fragment in result.message


def test_gate_refuses_expired_and_foreign_key_contexts() -> None:
    expired = context_signer().issue(
        team_id=TEST_TEAM, person_id="user-1", now=datetime.now(timezone.utc) - timedelta(days=30)
    )
    foreign = ContextSigner.from_value(ISSUER_GATEWAY, generate_signing_key(ISSUER_GATEWAY)[0]).issue(
        team_id=TEST_TEAM, person_id="user-1"
    )
    for signed, fragment in ((expired, "expired"), (foreign, "unknown key")):
        context = {**agent_context(), "task_context": signed.model_dump(mode="json")}
        result = gmail_search(open_gate(), context)
        assert not result.allowed and fragment in result.message


def test_agent_process_can_only_act_as_its_own_type_and_services_cannot_use_tools() -> None:
    gate = open_gate()
    research = BrokerCaller(agent_caller_label("ResearchAgent"))

    assert gmail_search(gate, agent_context(named_agent_type="ResearchAgent"), research).allowed
    refused = gmail_search(gate, agent_context(named_agent_type="TeamArchitect"), research)
    assert not refused.allowed and "cannot act as TeamArchitect" in refused.message
    assert not gmail_search(gate, agent_context(), BrokerCaller(CALLER_GATEWAY)).allowed


def test_unlisted_hosts_are_refused_in_both_modes() -> None:
    result = open_gate().check_broker_action(
        MANAGER, "secure_requests", "get", {"url": "https://unknown.example.test/x"}, agent_context()
    )

    assert not result.allowed and result.reason_code == REASON_UNLISTED_HOST
    assert "web API tool" in result.next_step


# -- decisions with grants (authorization enabled) ---------------------------------


@pytest.fixture
def enabled_gate(tmp_path):
    store = AccessRepository(
        resolve_storage_settings({"backend": "sqlite", "sqlite": {"path": str(tmp_path / "access.db")}})
    )
    catalogs = CatalogSource(None)
    cedar = CedarAccessClient(binary=str(BINARY))
    service = AccessService(store=store, catalog=catalogs.current(), cedar=cedar, team_id=TEST_TEAM)
    gate = BrokerAccessGate(
        catalogs=catalogs,
        verifier=ContextVerifier.from_value(context_verify_keys()),
        team_id=TEST_TEAM,
        access=service,
    )
    yield gate, service
    cedar.close()


def grant(subject: SubjectRefV1, tool_id: str, level: AccessLevel) -> GrantV1:
    return GrantV1(
        grant_id="new",
        team_id=TEST_TEAM,
        subject=subject,
        target=TargetRefV1(kind=TargetKind.TOOL, id=tool_id),
        level=level,
        granted_by=SubjectRefV1(kind=SubjectKind.USER, id="user-1"),
        created_at_utc=datetime(2026, 1, 1, tzinfo=timezone.utc),
    )


AGENT_TYPE = SubjectRefV1(kind=SubjectKind.AGENT_TYPE, id=f"{TEST_TEAM}/GeneralAssistant")


@needs_cedar
def test_enabled_gate_needs_a_grant_for_the_agent_and_the_person(enabled_gate) -> None:
    gate, service = enabled_gate
    owner_context = agent_context(roles=("owner",))
    member_context = agent_context(MEMBER_ID, roles=("member",))

    denied = gmail_search(gate, owner_context)
    assert not denied.allowed and denied.reason_code == REASON_ACCESS_DENIED
    assert "read access" in denied.message and denied.next_step

    service.grant_by_person(OWNER, grant(AGENT_TYPE, "cli:google", AccessLevel.READ))
    assert gmail_search(gate, owner_context).allowed
    send = gate.check_broker_action(
        MANAGER, "google", "gog.exec", {"argv": ["gmail", "send", "--to", "a@b.c"]}, owner_context
    )
    assert not send.allowed, "a read grant does not cover sending"

    # Gmail uses each person's own login, so it counts as the member's own on
    # the person side; a team tool needs the member to hold access too.
    assert gmail_search(gate, member_context).allowed
    service.grant_by_person(OWNER, grant(AGENT_TYPE, "cli:bitwarden", AccessLevel.READ))

    def vault_search(context):
        return gate.check_broker_action(MANAGER, "bitwarden", "credential.search", {"query": "x"}, context)

    assert vault_search(owner_context).allowed
    member = vault_search(member_context)
    assert not member.allowed and "agents never exceed their person" in member.message


# -- the real broker app -----------------------------------------------------------


@pytest.mark.asyncio
async def test_broker_runs_gog_with_the_checked_command_only(monkeypatch, tmp_path) -> None:
    commands: list[list[str]] = []

    class Completed:
        returncode = 0
        stdout = "[]"
        stderr = ""

    def fake_run(command, **_kwargs):
        commands.append(list(command))
        return Completed()

    # The person's Google sign-in is covered in tests/test_google_per_person.py.
    monkeypatch.setattr(
        "secure_tool_broker.executors.signed_in_google", lambda *_args, **_kwargs: contextlib.nullcontext({})
    )
    # Per-person broker state in files, as in the other broker unit tests.
    monkeypatch.setenv("SECURE_TOOL_BROKER_CREDENTIAL_STATE_BACKEND_MODE", "files")
    monkeypatch.setenv("SECURE_TOOL_BROKER_CREDENTIALS_DIR", str(tmp_path / "broker_credentials"))
    monkeypatch.setattr("secure_tool_broker.executors.shutil.which", lambda _name: "/usr/local/bin/gog")
    monkeypatch.setattr("secure_tool_broker.executors.subprocess.run", fake_run)
    service = SecureToolBrokerService(token=BROKER_ROOT, access_gate=open_gate())
    body = {
        "provider": "google",
        "action": "gog.exec",
        "params": {"argv": ["gmail", "search", "newer_than:1d"]},
        "reason": "check mail",
        "context": agent_context(task_id="task-1"),
    }
    async with TestClient(TestServer(service.build_app())) as client:
        allowed = await client.post("/v1/tools/google", headers=caller_headers(), json=body)
        assert allowed.status == 200, await allowed.text()
        allowed_body = await allowed.json()
        assert allowed_body["status"] == "success", allowed_body
        root_token = await client.post(
            "/v1/tools/google", headers={"Authorization": f"Bearer {BROKER_ROOT}"}, json=body
        )
        gateway = await client.post("/v1/tools/google", headers=gateway_headers(), json=body)
        unsigned = await client.post(
            "/v1/tools/google",
            headers=caller_headers(),
            json={**body, "context": {k: v for k, v in body["context"].items() if k != "task_context"}},
        )
        unsigned_error = json.loads(await unsigned.text())

    assert root_token.status == 401
    assert gateway.status == 403
    assert unsigned.status == 403
    assert unsigned_error["reason_code"] == REASON_IDENTITY_REJECTED
    gog_runs = [command for command in commands if "gmail" in command]
    assert len(gog_runs) == 1
    assert "--enable-commands-exact=gmail.search" in gog_runs[0]
    assert "--readonly" in gog_runs[0], "a read command runs under gog's --readonly"


def test_only_read_commands_run_under_gog_readonly() -> None:
    from secure_tool_broker.cli_passthrough import check_command_line

    def argv(*args: str) -> list[str]:
        return check_command_line("gog", list(args), instance_root=None, person_id=None).argv

    assert "--readonly" in argv("calendar", "events")
    assert "--readonly" not in argv("gmail", "send", "--to", "a@b.c", "--subject", "s")
    # Place search reads through a POST that gog's --readonly does not know.
    assert "--readonly" not in argv("maps", "places", "search", "cafe")


@pytest.mark.asyncio
async def test_gateway_routes_and_agent_routes_are_separate() -> None:
    service = SecureToolBrokerService(token=BROKER_ROOT, access_gate=open_gate())
    async with TestClient(TestServer(service.build_app())) as client:
        agent_on_auth = await client.get("/v1/auth/status", headers=caller_headers())
        gateway_on_mcp = await client.post(
            "/v1/mcp/connections/mcp_conn_x/invoke", headers=gateway_headers(), json={}
        )
        worker_on_hosting = await client.post(
            "/v1/mcp/hosted/h1/start", headers=caller_headers(agent_caller_label("ResearchAgent")), json={}
        )
        listing = await client.get("/v1/tools", headers=gateway_headers())

    assert agent_on_auth.status == 403
    assert gateway_on_mcp.status == 403
    assert worker_on_hosting.status == 403
    assert listing.status == 200


@needs_cedar
def test_the_trusted_profile_lets_team_lead_look_up_saved_credentials_but_not_use_them(enabled_gate) -> None:
    """Owner's decision 2026-09-27: "delete the lee bank credential" asked for two read approvals first."""

    from access_control.starter_profiles import apply_starter_profile

    gate, service = enabled_gate
    grants = apply_starter_profile(service, OWNER, "trusted", lead_agent_type="TeamArchitect")
    lead = agent_context(named_agent_type="TeamArchitect", roles=("owner",))

    def check(provider: str, action: str):
        return gate.check_broker_action(MANAGER, provider, action, {"item_name": "lee_bank_online_banking"}, lead)

    credential_grants = [grant for grant in grants if grant.target.id in {"cli:sealbox", "procedure:credentials"}]
    assert {(grant.target.id, grant.level, grant.delegation_depth) for grant in credential_grants} == {
        ("cli:sealbox", AccessLevel.READ, 0),
        ("procedure:credentials", AccessLevel.READ, 0),
    }, "read only, and TeamLead keeps it to itself"
    assert check("sealbox", "credential.search").allowed
    assert check("credential_catalog", "credential.get_metadata").allowed
    assert not check("sealbox", "credential.use_for_action").allowed, "using a credential still asks"

    member = PersonPrincipal(MEMBER_ID, TEST_TEAM, ("member",))
    member_grants = apply_starter_profile(service, member, "trusted", lead_agent_type="TeamArchitect")
    assert not {"cli:sealbox", "procedure:credentials"} & {grant.target.id for grant in member_grants}


class _Schedules:
    """Schedule rows for the gate's run checks (``task_identity.ScheduleFacts``)."""

    def __init__(self, *facts) -> None:
        self.rows = {fact.schedule_id: fact for fact in facts}

    def schedule_facts(self, schedule_id: str):
        return self.rows.get(schedule_id)


def _user_schedule(schedule_id: str, runner_type: str):
    from access_control.task_identity import ScheduleFacts

    return ScheduleFacts(schedule_id, True, "user-1", False, None, runner_type=runner_type)


@needs_cedar
def test_agents_in_a_scheduled_run_inherit_the_access_of_whom_they_work_for(tmp_path) -> None:
    """A worker running a person's job uses only the schedule's access; workers in
    the team lead's schedules and the runtime's timers get what the team lead
    hands on; the team lead keeps its own access for the person in every run."""

    from access_control.entities import schedule_subject
    from access_control.identity import ISSUER_SCHEDULER, verify_keys_value
    from access_control.task_identity import ScheduleFacts, ScheduleRunLookup
    from custom_messages.contracts.access_v2 import DelegationMode
    from tests.broker_access_support import _scheduler_key

    scheduler_private, scheduler_public = _scheduler_key()
    store = AccessRepository(resolve_storage_settings({"backend": "sqlite", "sqlite": {"path": str(tmp_path / "access.db")}}))
    catalogs = CatalogSource(None)
    cedar = CedarAccessClient(binary=str(BINARY))
    service = AccessService(store=store, catalog=catalogs.current(), cedar=cedar, team_id=TEST_TEAM)
    schedules = _Schedules(
        _user_schedule("digest", "GeneralAssistant"),
        _user_schedule("triage", "GeneralAssistant"),
        _user_schedule("morning", "TeamArchitect"),
        ScheduleFacts("heartbeat-launcher", True, "user-1", True, "heartbeat_launcher"),
    )
    gate = BrokerAccessGate(
        catalogs=catalogs,
        verifier=ContextVerifier.from_value(verify_keys_value([*json.loads(context_verify_keys()), scheduler_public])),
        team_id=TEST_TEAM,
        access=service,
        schedule_runs=ScheduleRunLookup(schedules, object()),
    )
    scheduler = ContextSigner.from_value(ISSUER_SCHEDULER, scheduler_private)

    def run_of(schedule_id: str, signer: ContextSigner = scheduler, agent_type: str = "GeneralAssistant") -> dict:
        signed = signer.issue(team_id=TEST_TEAM, person_id="user-1", person_roles=("owner",), schedule_id=schedule_id)
        return {**agent_context(roles=("owner",), named_agent_type=agent_type), "task_context": signed}

    try:
        # The digest job's access: every agent in its runs uses it, and only there.
        service.grant_by_person(
            OWNER,
            grant(schedule_subject(TEST_TEAM, "digest"), "cli:google", AccessLevel.READ).model_copy(
                update={"for_person_id": "user-1"}
            ),
        )
        assert gmail_search(gate, run_of("digest")).allowed
        assert not gmail_search(gate, run_of("triage")).allowed
        # Only the scheduler says a task is a schedule's run; the gateway's word is ignored.
        assert not gmail_search(gate, run_of("digest", context_signer())).allowed
        assert not gmail_search(gate, agent_context(roles=("owner",))).allowed

        # The team lead's lasting access for the person, which it may hand on.
        lead = SubjectRefV1(kind=SubjectKind.AGENT_TYPE, id=f"{TEST_TEAM}/TeamArchitect")
        service.grant_by_person(
            OWNER,
            grant(lead, "cli:google", AccessLevel.READ).model_copy(
                update={"for_person_id": "user-1", "delegation_depth": 1, "delegation_mode": DelegationMode.AUTOMATIC}
            ),
        )
        assert gmail_search(gate, agent_context(roles=("owner",))).allowed, "a delegated task inherits it"
        assert gmail_search(gate, run_of("morning")).allowed, "the team lead's own schedule"
        assert gmail_search(gate, run_of("heartbeat-item-1")).allowed, "a heartbeat item has no schedule row"
        assert gmail_search(gate, run_of("heartbeat-launcher")).allowed, "a runtime timer"
        assert not gmail_search(gate, run_of("triage")).allowed, "a job's worker uses only the job's access"
        assert gmail_search(gate, run_of("triage", agent_type="TeamArchitect")).allowed, "the team lead keeps its own"
    finally:
        cedar.close()


def test_a_process_that_cannot_read_schedules_reads_a_run_as_a_job_run(caplog) -> None:
    from access_control.identity import ISSUER_SCHEDULER
    from access_control.task_identity import job_schedule_id
    from custom_messages.contracts.access_v2 import TaskContextV1

    now = datetime.now(timezone.utc)
    context = TaskContextV1(
        context_id="ctx-1",
        issuer=ISSUER_SCHEDULER,
        team_id=TEST_TEAM,
        person_id="user-1",
        schedule_id="digest",
        issued_at_utc=now,
        expires_at_utc=now + timedelta(hours=1),
    )

    with caplog.at_level("WARNING"):
        assert job_schedule_id(context, None, lead_agent_type="TeamArchitect") == "digest"
    assert "event=schedule_kind_unknown" in caplog.text


@needs_cedar
def test_the_broker_treats_built_in_maintenance_agents_as_system_agents(tmp_path) -> None:
    from access_control.entities import AGENT_ROLE_SYSTEM, AGENT_ROLE_WORKER
    from secure_tool_broker.access_gate import build_broker_access_gate

    gate = build_broker_access_gate(
        instance_root=None,
        team_id=TEST_TEAM,
        authorization_enabled=True,
        storage_settings=resolve_storage_settings({"backend": "sqlite", "sqlite": {"path": str(tmp_path / "access.db")}}),
        cedar_binary=str(BINARY),
        verify_keys=context_verify_keys(),
    )
    try:
        assert gate.access is not None
        assert gate.access.agent_role_of("MemReviewAgent") == AGENT_ROLE_SYSTEM
        assert gate.access.agent_role_of("InternalImprovementAgent") == AGENT_ROLE_SYSTEM
        assert gate.access.agent_role_of("GeneralAssistant") == AGENT_ROLE_WORKER
    finally:
        gate.access.cedar.close()
