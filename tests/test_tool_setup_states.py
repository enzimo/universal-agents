"""Last known tool setup states, kept by trusted code (owner's decision 2026-09-28).

Real SQLite access store, the real broker app and access gate, and the
broker's file-backed credential state. Covered:

- the store keeps one state per person and tool, writes a ready tool again
  only when something changed or an hour passed, and a check that cannot tell
  never replaces a known state;
- broker calls: success means ready, a setup failure means needs setup with
  the step, an unrelated failure changes nothing, and the gate's missing
  account link means needs an account link;
- TeamLead's context line lists only the task person's states;
- the periodic re-check updates due states, sends exactly one notice per
  problem, skips people who are not active, and is off at 0 days;
- removing a person deletes their states;
- TeamLead's guidance reads ready as "use it without checking".
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from urllib.error import URLError

import pytest
from aiohttp.test_utils import TestClient, TestServer

from access_control.tool_setup_states import (
    HOW_SIGNED_IN,
    HOW_TEAM_LINK,
    ToolSetupStates,
    describe_states,
    step_for_failure,
    verdict_from_status,
)
from custom_agents.tool_setup_runtime import tool_setup_context
from custom_messages.contracts.access_v2 import (
    AccessNoticeKind,
    LinkOwnerKind,
    PersonStanding,
    TeamPersonV1,
    ToolSetupState,
)
from secure_tool_broker import google_workspace
from secure_tool_broker.service import SecureToolBrokerService
from secure_tool_broker.tool_setup_recheck import recheck_due
from secure_tool_broker.tool_setup_tracking import note_broker_result
from secure_tools.providers import ToolContext, ToolResult, ValidatedAction
from storage.access_repository import AccessRepository
from storage.config import resolve_storage_settings
from tests.broker_access_support import BROKER_ROOT, agent_context, caller_headers, context_signer
from tests.test_account_links import (  # noqa: F401 - pytest fixtures
    PORTAINER_TOOL,
    _isolated_credentials,
    link,
)
from tests.test_tool_setup_check import (  # noqa: F401 - pytest fixtures
    GOOGLE_TEAM_APP,
    PERSON,
    TEAM,
    world,
)

ROOT = Path(__file__).resolve().parents[1]
NOW = datetime(2026, 9, 28, 12, 0, tzinfo=timezone.utc)
OTHER = "user-2"


class Clock:
    def __init__(self, moment: datetime = NOW) -> None:
        self.moment = moment

    def __call__(self) -> datetime:
        return self.moment


@pytest.fixture
def links_world(tmp_path: Path):
    """The broker's gate with authorization off and a Portainer web API tool (as in test_account_links)."""

    from access_catalog.catalog import CatalogSource
    from access_control.identity import ContextVerifier
    from secure_tool_broker.access_gate import BrokerAccessGate
    from tests.broker_access_support import context_verify_keys

    tools = tmp_path / "instance" / "control" / "access" / "tools"
    tools.mkdir(parents=True)
    (tools / "portainer.yaml").write_text(PORTAINER_TOOL, encoding="utf-8")
    access = AccessRepository(
        resolve_storage_settings({"backend": "sqlite", "sqlite": {"path": str(tmp_path / "access.db")}})
    )
    gate = BrokerAccessGate(
        catalogs=CatalogSource(tmp_path / "instance"),
        verifier=ContextVerifier.from_value(context_verify_keys()),
        team_id=TEAM,
        access=None,
        links=access,
    )
    return gate, access


@pytest.fixture
def store(tmp_path: Path) -> AccessRepository:
    return AccessRepository(
        resolve_storage_settings({"backend": "sqlite", "sqlite": {"path": str(tmp_path / "access.db")}})
    )


# -- the store -----------------------------------------------------------------------


def test_a_working_tool_is_written_again_only_after_a_change_or_an_hour(store) -> None:
    clock = Clock()
    states = ToolSetupStates(store, clock=clock)

    assert states.note_ready(TEAM, PERSON, "cli:github", how=HOW_SIGNED_IN, account_label="exampleuser", source="tool_call")
    clock.moment = NOW + timedelta(minutes=30)
    assert not states.note_ready(TEAM, PERSON, "cli:github", how=HOW_SIGNED_IN, source="tool_call"), "throttled"
    clock.moment = NOW + timedelta(minutes=61)
    assert states.note_ready(TEAM, PERSON, "cli:github", how=HOW_SIGNED_IN, source="tool_call")

    state = states.get(TEAM, PERSON, "cli:github")
    assert (state.state, state.account_label, state.last_worked_at_utc) == (
        ToolSetupState.READY,
        "exampleuser",
        NOW + timedelta(minutes=61),
    )
    assert state.changed_at_utc == NOW, "the state itself did not change"
    assert states.note_needs_setup(TEAM, PERSON, "cli:github", step="sign_in", source="tool_call")
    assert not states.note_needs_setup(TEAM, PERSON, "cli:github", step="sign_in", source="tool_call")
    needs = states.get(TEAM, PERSON, "cli:github")
    assert (needs.state, needs.missing_step, needs.fixed_by) == (ToolSetupState.NEEDS_SETUP, "sign_in", "person")
    assert needs.last_worked_at_utc == NOW + timedelta(minutes=61), "when it last worked is kept"
    # A check that cannot tell never replaces a known state.
    assert not states.note_verdict(TEAM, PERSON, "cli:github", {"state": ToolSetupState.UNKNOWN}, source="setup_check")
    assert states.get(TEAM, PERSON, "cli:github").state is ToolSetupState.NEEDS_SETUP


@pytest.mark.parametrize(
    ("tool_id", "code", "step"),
    [
        ("cli:github", "auth_required", "sign_in"),
        ("cli:huggingface", "auth_required", "account_link"),
        ("cli:x", "x_account_missing", "sign_in"),
        ("cli:google", "google_oauth_client_missing", "team_sign_in_app"),
        ("http:portainer", "account_link_missing", "account_link"),
        ("cli:github", "api_request_failed", None),
        ("cli:github", "", None),
    ],
)
def test_setup_failures_are_told_from_other_failures(tool_id: str, code: str, step: str | None) -> None:
    assert step_for_failure(tool_id, code) == step


def test_a_status_entry_becomes_a_state() -> None:
    assert verdict_from_status({"known": True, "account_mode": "none"}) is None
    signed_in = {"known": True, "account_mode": "personal_login", "sign_in": {"state": "signed_in", "account": "exampleuser"}}
    assert verdict_from_status(signed_in)["how"] == HOW_SIGNED_IN
    x_team = {"known": True, "account_mode": "linked_credential", "sign_in": {"state": "missing"}, "link": {"team": True}}
    assert verdict_from_status(x_team)["how"] == HOW_TEAM_LINK
    no_app = {"known": True, "sign_in": {"state": "missing"}, "team_sign_in_app": "missing"}
    assert verdict_from_status(no_app) == {"state": ToolSetupState.NEEDS_SETUP, "step": "team_sign_in_app"}


# -- broker calls ------------------------------------------------------------------


def test_broker_results_update_the_state_and_other_failures_do_not(links_world) -> None:
    gate, store = links_world
    states = ToolSetupStates(store)
    context = ToolContext(canonical_user_id=PERSON, principal_type="user")
    github = ValidatedAction(provider="github", action="repo.view", params={"repo": "a/b"})

    note_broker_result(gate, github, context, ToolResult(status="success", content={}))
    assert states.get(TEAM, PERSON, "cli:github").how == HOW_SIGNED_IN

    note_broker_result(gate, github, context, ToolResult(status="error", error="boom", content={"error_code": "rate_limited"}))
    assert states.get(TEAM, PERSON, "cli:github").state is ToolSetupState.READY, "unrelated failures change nothing"

    note_broker_result(gate, github, context, ToolResult(status="error", content={"error_code": "auth_required"}))
    assert states.get(TEAM, PERSON, "cli:github").missing_step == "sign_in"

    # A function tool that acts with no account is not tracked; work for nobody is not either.
    note_broker_result(gate, ValidatedAction("credential_catalog", "template.list", {}), context, ToolResult("success"))
    note_broker_result(gate, github, ToolContext(principal_type="system"), ToolResult(status="success"))
    assert [state.tool_id for state in states.for_team(TEAM)] == ["cli:github"]


@pytest.mark.asyncio
async def test_web_requests_track_the_linked_account(links_world, monkeypatch) -> None:
    gate, store = links_world
    states = ToolSetupStates(store)
    service = SecureToolBrokerService(token=BROKER_ROOT, access_gate=gate)
    answers: list[object] = []

    class Response:
        headers = {"Content-Type": "application/json"}

        def __init__(self, status: int) -> None:
            self.status = status

        def read(self, _limit: int) -> bytes:
            return b"{}"

    def fake_send(request, *, timeout):
        answer = answers.pop(0)
        if isinstance(answer, Exception):
            raise answer
        return Response(int(answer))

    monkeypatch.setattr("secure_tool_broker.credential_catalog._send_api_request", fake_send)
    monkeypatch.setenv("PORTAINER_TOKEN", "portainer-secret")
    body = {
        "provider": "secure_requests",
        "action": "get",
        "params": {"url": "https://portainer.example.test/api/stacks"},
        "reason": "list stacks",
        "context": agent_context(PERSON, task_id="task-1"),
    }

    async def call() -> None:
        async with TestClient(TestServer(service.build_app())) as client:
            await client.post("/v1/tools/secure_requests", headers=caller_headers(), json=body)

    await call()  # no account linked: refused by the gate
    assert states.get(TEAM, PERSON, "http:portainer").missing_step == "account_link"

    from secure_tool_broker.credential_catalog_store import CredentialOwnerScope
    from tests.test_account_links import save_credentials, token_credential

    save_credentials(CredentialOwnerScope.team(TEAM), team_portainer=token_credential("PORTAINER_TOKEN"))
    link(store, LinkOwnerKind.TEAM, TEAM, "team_portainer")
    for answer, expected in (
        (200, (ToolSetupState.READY, HOW_TEAM_LINK)),
        (503, (ToolSetupState.READY, HOW_TEAM_LINK)),  # the service failing is not a setup problem
        (URLError("down"), (ToolSetupState.READY, HOW_TEAM_LINK)),
        (401, (ToolSetupState.NEEDS_SETUP, None)),  # the service refused the linked account
    ):
        answers.append(answer)
        await call()
        state = states.get(TEAM, PERSON, "http:portainer")
        assert (state.state, state.how) == expected, answer
    assert states.get(TEAM, PERSON, "http:portainer").missing_step == "account_link"


# -- TeamLead's context line -----------------------------------------------------


def _lead(tmp_path: Path, storage_path: Path, *, person: str | None, lead: bool = True):
    from custom_agents import access_runtime
    from custom_agents.task_manager import TaskManager

    access_runtime._runtime_access.cache_clear()
    manager = TaskManager(db_path=str(storage_path))
    signed = context_signer().issue(team_id=TEAM, person_id=person, person_roles=("member",)) if person else None
    meta = SimpleNamespace(gateway_context=SimpleNamespace(task_context=signed, schedule_run=None))
    agent = SimpleNamespace(
        id="agent-lead",
        team_id=TEAM,
        is_team_lead=lead,
        task_manager=manager,
        authorization_config={"enabled": False},
        instance_root=None,
        _logger=__import__("logging").getLogger("test"),
    )
    return agent, meta


def test_teamleads_context_lists_only_the_task_persons_states(tmp_path, monkeypatch) -> None:
    from tests.broker_access_support import context_verify_keys

    monkeypatch.setenv("ACCESS_CONTEXT_VERIFY_KEYS", context_verify_keys())
    db = tmp_path / "manager.db"
    agent, meta = _lead(tmp_path, db, person=PERSON)
    states = ToolSetupStates(AccessRepository(agent.task_manager.storage_settings))
    states.note_ready(TEAM, PERSON, "cli:github", how=HOW_SIGNED_IN, account_label="exampleuser", source="tool_call")
    states.note_needs_setup(TEAM, PERSON, "cli:x", step="sign_in", source="tool_call")
    states.note_ready(TEAM, OTHER, "cli:google", how=HOW_SIGNED_IN, account_label="other@example.com", source="tool_call")

    line = tool_setup_context(agent, meta)

    assert line.startswith("Tool setup for the person you work for")
    assert "GitHub ready (as exampleuser, worked today)" in line
    assert "X needs the person's sign-in" in line
    assert "Google" not in line and "other@example.com" not in line, "another person's accounts never show"
    worker, _ = _lead(tmp_path, db, person=PERSON, lead=False)
    assert tool_setup_context(worker, meta) == ""
    nobody_agent, nobody_meta = _lead(tmp_path, db, person=None)
    assert tool_setup_context(nobody_agent, nobody_meta) == ""


def test_the_context_line_is_short_and_says_when_it_last_worked(store) -> None:
    states = ToolSetupStates(store, clock=Clock(NOW - timedelta(days=3)))
    for index in range(12):
        states.note_ready(TEAM, PERSON, f"http:tool{index}", how=HOW_TEAM_LINK, source="tool_call")

    line = describe_states(states.for_person(TEAM, PERSON), {}, now=NOW)

    assert line.count(" ready (") == 8, "at most eight tools"
    assert "worked 3 days ago" in line and len(line) < 900


def test_the_context_engine_adds_the_tool_setup_line() -> None:
    from custom_agents.context_engine.base import ContextAssemblyRequest
    from custom_agents.context_engine.default import ContextEngineAdapters
    from custom_messages.contracts.routing_v1 import MessageHeader, MessageMeta
    from custom_messages.contracts.task_v1 import Task
    from custom_messages.enums import MessageMetaType
    from tests.test_context_engine import _engine

    engine = _engine()
    engine.adapters = ContextEngineAdapters(
        build_request_time_context=lambda meta: "Request Time Context:\n- now",
        build_request_conversation_context=lambda meta, text: "",
        build_team_routing_prompt_context=lambda: "",
        verified_person_of=lambda meta: None,
        build_tool_setup_context=lambda meta: "Tool setup for the person you work for: GitHub ready.",
    )
    task = Task(task_id="task_context_1", task_type=MessageMetaType.REQUEST, problem_statement="Check GitHub.")

    assembly = engine.assemble(
        ContextAssemblyRequest(
            incoming_content=task,
            execution_task_id=task.task_id,
            incoming_header=MessageHeader(),
            incoming_meta=MessageMeta(conversation_id="conversation_context_1"),
            identity_meta=MessageMeta(conversation_id="conversation_context_1"),
            incoming_message_json="{}",
        )
    )

    [section] = [section for section in assembly.sections if section.name == "tool_setup_context"]
    assert section.pinned and "GitHub ready" in assembly.prompt


# -- the periodic re-check -----------------------------------------------------------


def _old_ready(states: ToolSetupStates, person: str, tool_id: str) -> None:
    states.note_ready(TEAM, person, tool_id, how=HOW_SIGNED_IN, source="tool_call")


def test_the_periodic_check_updates_states_and_tells_each_problem_once(world) -> None:  # noqa: F811
    gate, store = world.gate, world.store
    assert google_workspace.store_team_client(TEAM, GOOGLE_TEAM_APP, stored_by="admin-1").status == "success"
    long_ago = Clock(NOW - timedelta(days=40))
    old = ToolSetupStates(store, clock=long_ago)
    _old_ready(old, PERSON, "cli:github")  # the stand-in gh check still signs in
    _old_ready(old, PERSON, "cli:google")  # the stand-in Google check asks for a new sign-in
    _old_ready(old, "user-gone", "cli:google")  # not active: skipped
    ToolSetupStates(store, clock=Clock(NOW - timedelta(days=2))).note_ready(
        TEAM, "user-3", "cli:google", how=HOW_SIGNED_IN, source="tool_call"
    )  # used recently: not due
    store.replace_people(
        TEAM,
        [
            TeamPersonV1(team_id=TEAM, person_id=PERSON, standing=PersonStanding.ACTIVE, roles=("member",), changed_at_utc=NOW),
            TeamPersonV1(team_id=TEAM, person_id="user-3", standing=PersonStanding.ACTIVE, roles=("member",), changed_at_utc=NOW),
            TeamPersonV1(team_id=TEAM, person_id="user-gone", standing=PersonStanding.DISABLED, changed_at_utc=NOW),
        ],
        at=NOW,
    )

    assert recheck_due(gate, every_days=0, now=NOW).checked == 0, "0 turns it off"
    report = recheck_due(gate, every_days=30, now=NOW)

    assert (report.checked, report.still_ready, report.now_needs_setup, report.notices) == (2, 1, 1, 1)
    states = ToolSetupStates(store)
    assert states.get(TEAM, PERSON, "cli:github").state is ToolSetupState.READY
    google = states.get(TEAM, PERSON, "cli:google")
    assert (google.state, google.missing_step, google.source) == (ToolSetupState.NEEDS_SETUP, "sign_in", "periodic_check")
    assert states.get(TEAM, "user-gone", "cli:google").state is ToolSetupState.READY, "not re-checked"
    [(notice, _read)] = store.notices_for(TEAM, person_id=PERSON, roles=("member",))
    assert notice.kind is AccessNoticeKind.TOOL_SETUP_NEEDED
    assert notice.summary == "Your Google sign-in no longer works. Run /link cli:google, or ask TeamLead to walk you through it."
    # Nothing is due now, and the same problem is never told twice.
    assert recheck_due(gate, every_days=30, now=NOW).checked == 0
    from secure_tool_broker.tool_setup_recheck import send_setup_notice

    assert send_setup_notice(gate, google, now=NOW) is False
    assert len(store.notices_for(TEAM, person_id=PERSON, roles=("member",))) == 1


# -- removal and guidance ------------------------------------------------------------


def test_removing_a_person_deletes_their_states(store) -> None:
    states = ToolSetupStates(store)
    states.note_ready(TEAM, PERSON, "cli:github", how=HOW_SIGNED_IN, source="tool_call")
    states.note_ready(TEAM, OTHER, "cli:github", how=HOW_SIGNED_IN, source="tool_call")

    store.forget_person_records(TEAM, PERSON, request_ids=())

    assert states.for_person(TEAM, PERSON) == []
    assert [state.person_id for state in states.for_team(TEAM)] == [OTHER]


def test_teamlead_uses_a_ready_tool_and_sets_up_the_rest_without_asking() -> None:
    prompt = (ROOT / "ua_definitions/agent_definitions/prompts/team_architect.md").read_text(encoding="utf-8")
    skill = (ROOT / "custom_agents/skill_templates/secure-credential-workflows/SKILL.md").read_text(encoding="utf-8")

    assert "A tool listed as ready needs no call: use it." in prompt
    # Owner's decision 2026-09-28: set_up_tools at once, never "shall I start setup?".
    assert "call `set_up_tools` immediately" in prompt
    assert "Never ask the person whether to start setup" in prompt
    assert "counts as a request to set up and answer" in prompt
    assert "check_tool_setup" not in prompt and "tool_setup_request" not in prompt
    assert "A tool listed as `ready` is used without any check." in " ".join(skill.split())
    assert json.dumps({"ok": True})  # keeps json imported for fixtures that need it
