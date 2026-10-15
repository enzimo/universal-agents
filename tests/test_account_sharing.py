"""Shared or private team accounts (``access_control.account_sharing``), end to end.

An administrator chooses, per service, whether the team's account serves
everyone's work (shared) or nobody's (private), and whether people added
from now on may use the team's model keys. These tests use the real SQLite
access store and the shipped tool catalog: the rules, the broker's choice of
account and its link form, the setup steps TeamLead offers, the ``/accounts``
command, the Access page wording and the removed configuration setting.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from access_catalog.catalog import CatalogSource
from access_control.account_sharing import SharingNotChosen, TeamAccountSharing, default_sharing, fixed_sharing
from access_control.caller_tokens import CALLER_MANAGER, BrokerCaller
from assistant_gateway.access_map import account_words
from assistant_gateway.account_sharing import GatewayAccountSharing, service_line
from assistant_gateway.application.account_overview import AccountStatus, ServiceAccounts
from assistant_gateway.application.commands import (
    ACCOUNT_SHARING_USAGE,
    PERSONAL_SETUP_USAGE,
    AccountSharingCommandHandler,
    AccountSharingCommandRequest,
    GatewayActorContext,
)
from assistant_gateway.config import GatewayInstanceConfig
from assistant_gateway.onboarding_web.settings_import import REASON_REMOVED, read_onboarding_import
from assistant_gateway.personal_setup import PersonalSetupSteps
from custom_messages.contracts.access_v2 import (
    MODEL_KEYS_SHARING_SUBJECT,
    REASON_ACCOUNT_LINK_MISSING,
    AccessNoticeKind,
    AccessNoticeV1,
    AccountLinkRequestV1,
    AccountSharing,
    LinkOwnerKind,
    ToolDefinitionV1,
)
from custom_tools.tool_setup_check import _account_steps
from secure_tool_broker.account_links import (
    ACCESS_STORE_MISSING_TEXT,
    REASON_ACCESS_STORE_MISSING,
    REASON_TEAM_ACCOUNT_PRIVATE,
    AccountLinkRefused,
    linkable_tool,
)
from secure_tool_broker.tool_setup_status import tool_status_entry
from storage.access_repository import AccessRepository
from tests.broker_access_support import (
    TEST_TEAM,
    access_store,
    agent_context,
    choose_sharing,
    linked_gate,
    open_gate,
    store_account_link,
    team_sharing,
)

ADMIN = "usr_admin"
ALICE = "usr_alice"
BOB = "usr_bob"
EXA = "http:exa"
NAMES = {ADMIN: "Ada Admin", ALICE: "Alice", BOB: "Bob"}
EXA_SEARCH = "https://api.exa.ai/search"


@pytest.fixture
def links(tmp_path: Path) -> AccessRepository:
    return access_store(tmp_path / "access.db")


def catalog_tool(tool_id: str) -> ToolDefinitionV1:
    return CatalogSource(None).current().tools[tool_id]


def exa_call(gate, person_id: str):
    return gate.check_broker_action(
        BrokerCaller(CALLER_MANAGER), "secure_requests", "post", {"url": EXA_SEARCH}, agent_context(person_id)
    )


# -- the rules and the shipped defaults ---------------------------------------------------


@pytest.mark.parametrize(
    ("tool_id", "fixed", "default"),
    [
        # Services that supply a resource start shared, and the team may choose.
        ("http:exa", None, AccountSharing.SHARED),
        ("http:brave-search", None, AccountSharing.SHARED),
        ("cli:huggingface", None, AccountSharing.SHARED),
        # The team's own Crawl4AI server opens only with the team's token.
        ("http:crawl4ai", AccountSharing.SHARED, AccountSharing.SHARED),
        # X posts as the account, so it starts private; the team may share it.
        ("cli:x", None, AccountSharing.PRIVATE),
        # GitHub and Google act only as each person's own account (owner's choice).
        ("cli:github", AccountSharing.PRIVATE, AccountSharing.PRIVATE),
        ("cli:google", AccountSharing.PRIVATE, AccountSharing.PRIVATE),
        ("http:github", AccountSharing.PRIVATE, AccountSharing.PRIVATE),
    ],
)
def test_shipped_services_start_shared_or_private_as_the_owner_decided(
    tool_id: str, fixed: AccountSharing | None, default: AccountSharing
) -> None:
    tool = catalog_tool(tool_id)

    assert fixed_sharing(tool) is fixed
    assert default_sharing(tool) is default


def test_a_server_the_instance_runs_itself_takes_only_the_teams_account(tmp_path: Path) -> None:
    # A web API tool whose address is one of this instance's settings reaches
    # the team's own server, which only the team's token opens: keeping it
    # private would stop everyone, and a person has no account to link.
    from access_catalog.catalog import CatalogError, ToolCatalog

    definitions = tmp_path / "own.yaml"
    definitions.write_text(
        "tools:\n"
        "  - tool_id: http:crawl4ai\n"
        "    display_name: Crawl4AI web crawler\n"
        "    account: linked_credential\n"
        "    tool_sets: [internet]\n"
        "    service_url_setting: web_services.crawl4ai.base_url\n"
        "    routes:\n"
        "      - {name: crawl, method: POST, path: /crawl, level: read}\n"
        "    credential_injections:\n"
        "      - {placement: header, name: Authorization, field: api_token}\n"
    )

    with pytest.raises(CatalogError, match="link_owners: \\[team\\]"):
        ToolCatalog().load_file(definitions)


def test_only_a_tool_with_a_choice_may_name_a_default_and_definitions_round_trip() -> None:
    with pytest.raises(ValueError, match="default_sharing"):
        ToolDefinitionV1(tool_id="fn:notes", kind="fn", display_name="Notes", default_sharing="private")
    with pytest.raises(ValueError, match="default_sharing"):
        ToolDefinitionV1(
            tool_id="http:own", kind="http", display_name="Own", account="linked_credential",
            hosts=("own.example.test",), link_owners=("person",), default_sharing="shared",
        )

    tool = catalog_tool("cli:x")
    plain = catalog_tool("http:github")

    # A saved definition always carries the field; loading it again must not refuse.
    assert ToolDefinitionV1.model_validate(tool.model_dump(mode="json")) == tool
    assert ToolDefinitionV1.model_validate(plain.model_dump(mode="json")) == plain


def test_a_choice_is_stored_per_service_and_logged_with_the_previous_value(links: AccessRepository) -> None:
    sharing = team_sharing(links)
    exa = catalog_tool(EXA)

    first = sharing.choose_tool(exa, AccountSharing.PRIVATE, by=ADMIN)
    sharing.choose_tool(exa, AccountSharing.SHARED, by=ADMIN)
    sharing.choose_model_keys(AccountSharing.PRIVATE, by=ADMIN)

    assert first.changed_by_person_id == ADMIN
    assert sharing.for_tool(exa) is AccountSharing.SHARED
    assert sharing.model_keys() is AccountSharing.PRIVATE
    assert [record.subject for record in links.team_account_sharing(TEST_TEAM)] == [EXA, MODEL_KEYS_SHARING_SUBJECT]
    events = [event for event in links.events(TEST_TEAM) if event.kind == "account_sharing_changed"]
    assert [(event.detail["sharing"], event.detail["previous"]) for event in events] == [
        ("private", None),
        ("shared", "private"),
        ("private", None),
    ]
    assert events[0].actor is not None and events[0].actor.id == ADMIN


def test_nothing_chosen_means_tool_defaults_and_shared_model_keys(links: AccessRepository) -> None:
    sharing = team_sharing(links)

    assert sharing.model_keys() is AccountSharing.SHARED
    assert sharing.new_people_use_team_model_keys() is True
    assert sharing.link_owners(catalog_tool(EXA)) == ("person", "team")
    assert sharing.link_owners(catalog_tool("cli:x")) == ("person",)


def test_a_fixed_service_cannot_be_chosen(links: AccessRepository) -> None:
    with pytest.raises(SharingNotChosen, match="always private"):
        team_sharing(links).choose_tool(catalog_tool("cli:github"), AccountSharing.SHARED, by=ADMIN)

    assert links.team_account_sharing(TEST_TEAM) == []


# -- the broker: which account a call uses --------------------------------------------------


def test_a_shared_service_serves_people_without_their_own_and_work_for_nobody(links: AccessRepository) -> None:
    gate = linked_gate(links)
    team = store_account_link(links, EXA, "exa-team", owner_kind=LinkOwnerKind.TEAM, owner_id=TEST_TEAM)
    own = store_account_link(links, EXA, "exa-alice", owner_id=ALICE)

    assert exa_call(gate, ALICE).account_link == own
    assert exa_call(gate, BOB).account_link == team
    assert gate.account_link_for(EXA, None) == team


def test_a_private_service_never_uses_the_teams_account(links: AccessRepository) -> None:
    gate = linked_gate(links)
    store_account_link(links, EXA, "exa-team", owner_kind=LinkOwnerKind.TEAM, owner_id=TEST_TEAM)
    own = store_account_link(links, EXA, "exa-alice", owner_id=ALICE)
    choose_sharing(links, EXA, AccountSharing.PRIVATE)

    alice = exa_call(gate, ALICE)
    bob = exa_call(gate, BOB)

    assert alice.allowed and alice.account_link == own
    assert not bob.allowed and bob.reason_code == REASON_ACCOUNT_LINK_MISSING
    # The next step names only the person's own link: a team account would not be used.
    assert "/link http:exa" in bob.next_step and "/link http:exa team" not in bob.next_step
    assert "private" in bob.next_step
    # Work that serves nobody has no account to use.
    assert gate.account_link_for(EXA, None) is None


class _Schedules:
    def __init__(self, *facts) -> None:
        self.rows = {fact.schedule_id: fact for fact in facts}

    def schedule_facts(self, schedule_id: str):
        return self.rows.get(schedule_id)


def scheduled_runs_gate(links: AccessRepository):
    """A gate that verifies the scheduler's signed runs: Alice's job and a system timer that serves nobody."""

    import json

    from access_control.identity import ISSUER_SCHEDULER, ContextSigner, ContextVerifier, verify_keys_value
    from access_control.task_identity import ScheduleFacts, ScheduleRunLookup
    from custom_messages.enums import SystemScheduleKind
    from secure_tool_broker.access_gate import BrokerAccessGate
    from tests.broker_access_support import _scheduler_key, context_verify_keys

    private, public = _scheduler_key()
    gate = BrokerAccessGate(
        catalogs=CatalogSource(None),
        verifier=ContextVerifier.from_value(verify_keys_value([*json.loads(context_verify_keys()), public])),
        team_id=TEST_TEAM,
        access=None,
        links=links,
        schedule_runs=ScheduleRunLookup(
            _Schedules(
                ScheduleFacts("alice-digest", True, ALICE, False, None, runner_type="GeneralAssistant"),
                ScheduleFacts("upkeep", True, None, True, SystemScheduleKind.INTERNAL_IMPROVEMENT.value),
            ),
            object(),
        ),
    )
    scheduler = ContextSigner.from_value(ISSUER_SCHEDULER, private)

    def run(schedule_id: str, person_id: str | None) -> dict:
        signed = scheduler.issue(
            team_id=TEST_TEAM, person_id=person_id, person_roles=("member",) if person_id else (), schedule_id=schedule_id
        )
        return {**agent_context(person_id or ""), "canonical_user_id": person_id, "task_context": signed.model_dump(mode="json")}

    return gate, run


@pytest.mark.parametrize("tool_id", [EXA, "cli:x"])
def test_scheduled_runs_and_work_for_nobody_never_use_a_private_services_team_account(
    links: AccessRepository, tool_id: str
) -> None:
    gate, run = scheduled_runs_gate(links)
    team = store_account_link(links, tool_id, "team-link", owner_kind=LinkOwnerKind.TEAM, owner_id=TEST_TEAM)
    caller = BrokerCaller(CALLER_MANAGER)

    def call(context: dict):
        if tool_id == EXA:
            return gate.check_broker_action(caller, "secure_requests", "post", {"url": EXA_SEARCH}, context)
        return gate.check_broker_action(caller, "xurl", "auth.status", {}, context)

    runs = {"Alice's job": run("alice-digest", ALICE), "a system timer": run("upkeep", None)}
    choose_sharing(links, tool_id, AccountSharing.PRIVATE)
    private = {name: call(context) for name, context in runs.items()}
    choose_sharing(links, tool_id, AccountSharing.SHARED)
    shared = {name: call(context) for name, context in runs.items()}

    for name in runs:
        assert private[name].account_link is None, name
        assert shared[name].allowed and shared[name].account_link == team, name
        if tool_id == EXA:
            assert private[name].reason_code == REASON_ACCOUNT_LINK_MISSING, name
        else:
            # X lets the executor try the person's own sign-in; it only gets no team fallback.
            assert private[name].allowed, name


def test_the_next_step_never_calls_an_always_private_service_a_team_choice() -> None:
    from secure_tool_broker.access_gate import account_link_next_step

    github_api = account_link_next_step(catalog_tool("http:github"), team_used=False)
    exa = account_link_next_step(catalog_tool(EXA), team_used=False)

    assert github_api.startswith("GitHub API uses only each person's own account, so it is always private.")
    assert "/link http:github" in github_api and "keeps this service private" not in github_api
    assert exa.startswith("The team keeps this service private")
    # The team's own Crawl4AI server: only the team's link, never a person's own.
    crawl4ai = account_link_next_step(catalog_tool("http:crawl4ai"), team_used=True)
    assert crawl4ai.startswith("Crawl4AI web crawler uses only the team's account, so it is always shared.")
    assert "/link http:crawl4ai team" in crawl4ai and "links their own" not in crawl4ai


def test_sharing_a_private_service_again_uses_the_kept_team_account(links: AccessRepository) -> None:
    gate = linked_gate(links)
    team = store_account_link(links, EXA, "exa-team", owner_kind=LinkOwnerKind.TEAM, owner_id=TEST_TEAM)
    choose_sharing(links, EXA, AccountSharing.PRIVATE)
    refused = exa_call(gate, BOB)
    choose_sharing(links, EXA, AccountSharing.SHARED)

    assert not refused.allowed
    assert exa_call(gate, BOB).account_link == team


def link_request(owner: LinkOwnerKind, roles: tuple[str, ...], tool_id: str = EXA) -> AccountLinkRequestV1:
    return AccountLinkRequestV1(
        request_id=f"req-{owner.value}-{roles[0]}", team_id=TEST_TEAM, tool_id=tool_id, owner_kind=owner,
        person_id=ALICE, person_roles=roles, reason="test",
    )


def test_the_link_form_refuses_the_teams_account_for_a_private_service(links: AccessRepository) -> None:
    catalog = CatalogSource(None).current()
    choose_sharing(links, EXA, AccountSharing.PRIVATE)

    with pytest.raises(AccountLinkRefused) as refused:
        linkable_tool(catalog, link_request(LinkOwnerKind.TEAM, ("admin",)), team_sharing(links))
    own = linkable_tool(catalog, link_request(LinkOwnerKind.PERSON, ("member",)), team_sharing(links))
    choose_sharing(links, EXA, AccountSharing.SHARED)
    team = linkable_tool(catalog, link_request(LinkOwnerKind.TEAM, ("admin",)), team_sharing(links))

    assert refused.value.reason_code == REASON_TEAM_ACCOUNT_PRIVATE
    assert "/accounts share http:exa" in str(refused.value) and "/link http:exa" in str(refused.value)
    assert own.tool_id == EXA and team.tool_id == EXA


def test_a_broker_without_an_access_store_refuses_plainly(caplog) -> None:
    # Production always gives the broker the team's access store; a gate
    # built without one finds and saves no accounts and says so, never crashes.
    gate = open_gate()

    with caplog.at_level("ERROR", logger="secure_tool_broker.access_gate"):
        refused = exa_call(gate, ALICE)
    with pytest.raises(AccountLinkRefused) as link_form:
        linkable_tool(CatalogSource(None).current(), link_request(LinkOwnerKind.PERSON, ("member",)), gate.account_sharing())

    assert not refused.allowed and refused.reason_code == REASON_ACCOUNT_LINK_MISSING
    assert refused.message == ACCESS_STORE_MISSING_TEXT and "state.storage" in refused.next_step
    assert "event=broker_access_store_missing tool_id=http:exa person_id=usr_alice" in caplog.text
    assert link_form.value.reason_code == REASON_ACCESS_STORE_MISSING


def test_teamleads_proposal_cannot_ask_for_a_team_account_a_private_service_would_not_use(
    links: AccessRepository,
) -> None:
    # Refused when the proposal is made, so nobody approves a link the form would refuse.
    from access_control.proposals import ProposalCompiler, ProposalRefused
    from custom_messages.contracts.access_v2 import AccessProposalV1, ProposedAccountLinkV1

    compiler = ProposalCompiler(
        catalog=CatalogSource(None).current(), team_id=TEST_TEAM, holds=lambda *_: True, account_sharing=team_sharing(links)
    )

    def proposal(tool_id: str, owner: str = "team") -> AccessProposalV1:
        link = ProposedAccountLinkV1(tool_id=tool_id, owner=owner, purpose="Search for the person.")
        return AccessProposalV1(title="Link an account", reason="The task needs it.", account_links=(link,))

    choose_sharing(links, EXA, AccountSharing.PRIVATE)
    with pytest.raises(ProposalRefused) as private:
        compiler.compile(proposal(EXA), person=None, task_id=None)
    with pytest.raises(ProposalRefused, match="uses only each person's own account"):
        compiler.compile(proposal("http:github"), person=None, task_id=None)
    own = compiler.compile(proposal(EXA, "person"), person=None, task_id=None)
    choose_sharing(links, EXA, AccountSharing.SHARED)
    shared = compiler.compile(proposal(EXA), person=None, task_id=None)

    assert "Exa web search is private in this team" in str(private.value)
    assert "/accounts share http:exa" in str(private.value)
    assert "credential Exa web search uses (the team's account)" in shared.rows[-1].text
    assert "the account of the person this task works for" in own.rows[-1].text


def test_an_access_workflow_cannot_expect_a_team_account_a_private_service_would_not_use(
    links: AccessRepository,
) -> None:
    from custom_agents.access_workflow_setups import check_setup_expectations
    from custom_messages.contracts.access_workflow_v2 import WorkflowToolSetupV2

    step = type("Step", (), {"step_id": "link_x", "tool_setup": (WorkflowToolSetupV2(tool_id="cli:x", kind="account_link", owner="team"),)})
    catalog = CatalogSource(None).current()

    with pytest.raises(ValueError, match="Step link_x: X .* is private in this team"):
        check_setup_expectations([step], catalog, team_sharing(links))
    choose_sharing(links, "cli:x", AccountSharing.SHARED)
    check_setup_expectations([step], catalog, team_sharing(links))

    # An owner the tool never takes is refused as the link form would refuse it.
    def expecting(tool_id: str, owner: str):
        item = WorkflowToolSetupV2(tool_id=tool_id, kind="account_link", owner=owner)
        return type("Step", (), {"step_id": "link", "tool_setup": (item,)})

    with pytest.raises(ValueError, match="GitHub API uses only each person's own account"):
        check_setup_expectations([expecting("http:github", "team")], catalog, team_sharing(links))
    with pytest.raises(ValueError, match="Crawl4AI web crawler uses only the team's account"):
        check_setup_expectations([expecting("http:crawl4ai", "person")], catalog, team_sharing(links))


def test_the_github_api_takes_only_each_persons_own_account(links: AccessRepository) -> None:
    catalog = CatalogSource(None).current()

    with pytest.raises(AccountLinkRefused, match="each person's own account"):
        linkable_tool(catalog, link_request(LinkOwnerKind.TEAM, ("owner",), "http:github"), team_sharing(links))
    assert linkable_tool(
        catalog, link_request(LinkOwnerKind.PERSON, ("member",), "http:github"), team_sharing(links)
    ).tool_id == "http:github"


def test_setup_status_reports_a_team_account_only_while_it_is_used(links: AccessRepository) -> None:
    gate = linked_gate(links)
    store_account_link(links, EXA, "exa-team", owner_kind=LinkOwnerKind.TEAM, owner_id=TEST_TEAM)

    shared = tool_status_entry(EXA, BOB, gate)
    choose_sharing(links, EXA, AccountSharing.PRIVATE)
    private = tool_status_entry(EXA, BOB, gate)

    assert shared["link"] == {"own": False, "team": True}
    assert private["link"] == {"own": False, "team": False}


# -- the setup steps TeamLead offers --------------------------------------------------------


def test_teamlead_offers_only_the_persons_own_link_for_a_private_service(links: AccessRepository) -> None:
    exa = catalog_tool(EXA)
    sharing = team_sharing(links)
    nothing_linked = {"link": {"own": False, "team": False}}

    shared_steps = _account_steps(exa, nothing_linked, False, sharing.link_owners(exa))
    choose_sharing(links, EXA, AccountSharing.PRIVATE)
    private_steps = _account_steps(exa, nothing_linked, False, sharing.link_owners(exa))

    assert [step["owner"] for step in shared_steps] == ["person", "team"]
    assert [step["owner"] for step in private_steps] == ["person"]
    assert "keeps Exa web search private" in private_steps[0]["say"]
    # A linked team account does not make a private service ready.
    assert _account_steps(exa, {"link": {"own": False, "team": True}}, False, sharing.link_owners(exa)) != []


# -- /accounts ------------------------------------------------------------------------------


class Port:
    """The gateway's /accounts side on a real store and the shipped catalog; notices are kept."""

    def __init__(self, links: AccessRepository) -> None:
        self.notices: list[AccessNoticeV1] = []
        self.sharing = GatewayAccountSharing(
            store=lambda: links,
            catalog=CatalogSource(None).current,
            team_id=TEST_TEAM,
            send_notice=self.notices.append,
            setup=PersonalSetupSteps(store=lambda: links, catalog=CatalogSource(None).current, team_id=TEST_TEAM),
            display_name=NAMES.get,
        )


def test_everyone_sees_whose_account_each_service_uses(links: AccessRepository) -> None:
    port = Port(links)
    store_account_link(links, EXA, "exa-team", owner_kind=LinkOwnerKind.TEAM, owner_id=TEST_TEAM)
    store_account_link(links, "http:brave-search", "brave-alice", owner_id=ALICE)
    choose_sharing(links, EXA, AccountSharing.PRIVATE)

    text = port.sharing.overview(person_id=ALICE, roles=("member",))

    # Names in bold, tool ids and commands as code, the account labels underlined.
    assert "- **Model keys**: shared: people added from now on may use the team's model keys." in text
    assert (
        "- **Exa web search** (`http:exa`): private. __Team account__: linked, not used while private. "
        "__Your own__: not linked; link it with `/link http:exa`."
    ) in text
    assert "- **Brave Search** (`http:brave-search`): shared. __Team account__: not linked. __Your own__: linked." in text
    assert "- **GitHub (gh)** (`cli:github`): always private: each person signs in with `/link cli:github`." in text
    # The command is given once, in "Your own".
    assert (
        "- **GitHub API** (`http:github`): always private: each person links their own. "
        "__Your own__: not linked; link it with `/link http:github`."
    ) in text
    assert "- **X (xurl)** (`cli:x`): private. __Team account__: not linked. __Your own__: sign in with `/link cli:x`." in text
    assert "Administrators change it with `/accounts share <tool_id|model>` or `/accounts private <tool_id|model>`." in text


def test_the_accounts_list_is_built_from_one_row_per_service(links: AccessRepository) -> None:
    """The chat list and the Settings Accounts tab read the same rows."""

    port = Port(links)
    store_account_link(links, EXA, "exa-team", owner_kind=LinkOwnerKind.TEAM, owner_id=TEST_TEAM)
    choose_sharing(links, EXA, AccountSharing.PRIVATE)
    port.sharing.change(subject="model", shared=False, person_id=ADMIN, roles=("admin",))

    rows = {row.subject: row for row in port.sharing.services(person_id=ALICE)}

    assert next(iter(rows)) == "model", "the model keys come first"
    assert rows["model"] == ServiceAccounts(
        subject="model", name="Model keys", sharing=AccountSharing.PRIVATE, fixed=False, setup_command="/auth model me"
    )
    assert rows[EXA].team_account is AccountStatus.NOT_USED and rows[EXA].own_account is AccountStatus.NOT_LINKED
    assert rows[EXA].setup_command == "/link http:exa" and not rows[EXA].signs_in
    github = rows["cli:github"]
    assert github.fixed and github.signs_in and github.team_account is None and github.own_account is None
    assert service_line(rows["model"]) == (
        "- **Model keys**: private: people added from now on set up their own model with `/auth model me`."
    )


def test_only_administrators_change_sharing(links: AccessRepository) -> None:
    port = Port(links)

    member = port.sharing.change(subject=EXA, shared=False, person_id=ALICE, roles=("member",))
    outsider = port.sharing.change(subject=EXA, shared=False, person_id=BOB, roles=())

    assert member == "Only an owner or an administrator can decide whether the team's accounts are shared."
    assert "not an active member" in outsider
    assert links.team_account_sharing(TEST_TEAM) == [] and port.notices == []


def test_an_administrator_makes_a_service_private_and_administrators_are_told(links: AccessRepository) -> None:
    port = Port(links)
    store_account_link(links, EXA, "exa-team", owner_kind=LinkOwnerKind.TEAM, owner_id=TEST_TEAM)

    reply = port.sharing.change(subject=EXA, shared=False, person_id=ADMIN, roles=("admin",))
    again = port.sharing.change(subject=EXA, shared=False, person_id=ADMIN, roles=("admin",))

    assert reply.startswith("Exa web search is now private: agents use only each person's own account")
    assert "/link http:exa" in reply and "The team's account stays linked but is not used." in reply
    # The administrator's own work loses the team's account too, and the reply says how to keep it.
    assert reply.endswith(
        "Your own work stops using the team's account too: to keep using it yourself, "
        "link your own account with /link http:exa."
    )
    assert again.startswith("Exa web search is already private")
    assert team_sharing(links).for_tool(catalog_tool(EXA)) is AccountSharing.PRIVATE
    assert len(port.notices) == 1
    notice = port.notices[0]
    assert notice.kind is AccessNoticeKind.ACCOUNT_SHARING_CHANGED
    assert notice.recipient_roles == ("admin",)
    assert notice.title == "Exa web search is now private"
    # Administrators read who changed it by name, never by id.
    assert notice.summary.startswith("Ada Admin made Exa web search (http:exa) private") and ADMIN not in notice.summary


def test_an_administrator_with_their_own_account_is_told_it_keeps_working(links: AccessRepository) -> None:
    port = Port(links)
    store_account_link(links, EXA, "exa-team", owner_kind=LinkOwnerKind.TEAM, owner_id=TEST_TEAM)
    store_account_link(links, EXA, "exa-admin", owner_id=ADMIN)
    store_account_link(links, "cli:x", "x-team", owner_kind=LinkOwnerKind.TEAM, owner_id=TEST_TEAM)
    choose_sharing(links, "cli:x", AccountSharing.SHARED)

    exa = port.sharing.change(subject=EXA, shared=False, person_id=ADMIN, roles=("admin",))
    x = port.sharing.change(subject="cli:x", shared=False, person_id=ADMIN, roles=("admin",))

    assert exa.endswith("Your own work keeps using your own account.")
    assert x.endswith("to keep using it yourself, sign in with /link cli:x.")


def test_sharing_a_service_without_a_team_account_says_how_to_link_one(links: AccessRepository) -> None:
    port = Port(links)

    reply = port.sharing.change(subject="cli:x", shared=True, person_id=ADMIN, roles=("owner",))

    assert reply.startswith("X (xurl) is now shared")
    assert "No team account is linked yet: an administrator links it with /link cli:x team." in reply


def test_fixed_and_unknown_services_are_explained(links: AccessRepository) -> None:
    port = Port(links)

    github = port.sharing.change(subject="cli:github", shared=True, person_id=ADMIN, roles=("admin",))
    unknown = port.sharing.change(subject="http:nothing", shared=True, person_id=ADMIN, roles=("admin",))
    no_account = port.sharing.change(subject="fn:web_research", shared=True, person_id=ADMIN, roles=("admin",))

    assert github == (
        "GitHub (gh) uses each person's own sign-in and has no team account, so it is always private."
    )
    assert "is not a service that uses an account" in unknown and "is not a service" in no_account
    assert port.notices == []


def test_model_keys_sharing_sets_what_people_added_from_now_on_get(links: AccessRepository) -> None:
    port = Port(links)

    reply = port.sharing.change(subject="model", shared=False, person_id=ADMIN, roles=("admin",))

    assert reply.startswith(
        "Model keys are now private: people added from now on set up their own model with /auth model me."
    )
    assert "/user team-keys" in reply
    assert team_sharing(links).new_people_use_team_model_keys() is False
    assert port.notices[0].title == "The team's model keys are now private"


def test_a_failed_notice_still_keeps_the_change_and_says_so(links: AccessRepository, caplog) -> None:
    def refuse(_notice: AccessNoticeV1) -> None:
        raise RuntimeError("inbox down")

    port = GatewayAccountSharing(
        store=lambda: links,
        catalog=CatalogSource(None).current,
        team_id=TEST_TEAM,
        send_notice=refuse,
        setup=PersonalSetupSteps(store=lambda: links, catalog=CatalogSource(None).current, team_id=TEST_TEAM),
        display_name=NAMES.get,
    )

    with caplog.at_level("ERROR", logger="assistant_gateway.account_sharing"):
        reply = port.change(subject=EXA, shared=False, person_id=ADMIN, roles=("admin",))

    assert reply.endswith("Administrators could not be told in the Inbox; the change still applies.")
    assert f"event=account_sharing_notice_failed subject=http:exa person_id={ADMIN}" in caplog.text
    assert team_sharing(links).for_tool(catalog_tool(EXA)) is AccountSharing.PRIVATE


class _Identity:
    def __init__(self, roles: tuple[str, ...]) -> None:
        self.roles = roles

    def canonical_user_id_for_context(self, **_context) -> str | None:
        return ALICE

    def team_roles_for_person(self, canonical_user_id: str) -> tuple[str, ...]:
        return self.roles


class _RecordingPort:
    def __init__(self) -> None:
        self.calls: list[tuple] = []

    def overview(self, *, person_id: str, roles: tuple[str, ...]) -> str:
        self.calls.append(("overview", person_id, roles))
        return "overview"

    def change(self, *, subject: str, shared: bool, person_id: str, roles: tuple[str, ...]) -> str:
        self.calls.append(("change", subject, shared, person_id, roles))
        return "changed"

    def setup(self, *, person_id: str, roles: tuple[str, ...]) -> str:
        self.calls.append(("setup", person_id, roles))
        return "setup list"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("command", "expected", "call"),
    [
        ("/accounts", "overview", ("overview", ALICE, ("admin",))),
        ("/accounts share http:exa", "changed", ("change", "http:exa", True, ALICE, ("admin",))),
        ("/accounts PRIVATE model", "changed", ("change", "model", False, ALICE, ("admin",))),
        ("/accounts help", ACCOUNT_SHARING_USAGE, None),
        ("/accounts lend http:exa", ACCOUNT_SHARING_USAGE, None),
        ("/accounts share", ACCOUNT_SHARING_USAGE, None),
        ("/setup", "setup list", ("setup", ALICE, ("admin",))),
        ("/setup now", PERSONAL_SETUP_USAGE, None),
    ],
)
async def test_the_accounts_command_parses_its_words(command: str, expected: str, call: tuple | None) -> None:
    port = _RecordingPort()
    handler = AccountSharingCommandHandler(identity=_Identity(("admin",)), sharing=port)

    answer = await handler.execute(
        AccountSharingCommandRequest(command=command, actor=GatewayActorContext(channel_name="telegram", user_id="1"))
    )

    assert answer == expected
    assert port.calls == ([call] if call else [])


# -- the Access page --------------------------------------------------------------------------


def test_the_access_page_says_whether_the_team_account_is_used(links: AccessRepository) -> None:
    sharing = team_sharing(links)
    exa = catalog_tool(EXA)
    x = catalog_tool("cli:x")

    shared = account_words(exa, sharing)
    choose_sharing(links, EXA, AccountSharing.PRIVATE)

    assert shared == "Uses the linked account of the person the task works for, else the team's, which the team shares"
    assert account_words(exa, sharing) == (
        "Uses only the linked account of the person the task works for (the team keeps it private)"
    )
    assert account_words(x, sharing) == (
        "Uses the sign-in of the person the task works for (/link cli:x) (the team keeps it private)"
    )
    assert account_words(catalog_tool("http:github"), sharing) == (
        "Uses only the linked account of the person the task works for"
    )


# -- the removed setting --------------------------------------------------------------------


def test_the_removed_team_model_keys_setting_is_refused_and_listed_on_import(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match=r"new_people_use_team_model_keys has been removed.*/accounts private model"):
        GatewayInstanceConfig.model_validate(
            {"instance_root": str(tmp_path), "gateway": {"users": {"new_people_use_team_model_keys": False}}}
        )

    imported = read_onboarding_import(
        "instance_config.yaml", "gateway:\n  users:\n    new_people_use_team_model_keys: false\n"
    )

    assert ("gateway.users.new_people_use_team_model_keys", REASON_REMOVED) in [
        (item.path, item.reason) for item in imported.skipped
    ]


def test_web_chat_accepts_the_accounts_command() -> None:
    from assistant_gateway.interactive_apps.website_support import _WEB_CHAT_ALLOWED_COMMANDS

    assert "/accounts" in _WEB_CHAT_ALLOWED_COMMANDS


# -- onboarding's choices (access_control.account_sharing.onboarding_choices) ----------


def test_onboarding_choices_cover_the_model_keys_and_every_service_with_a_choice() -> None:
    from access_control.account_sharing import onboarding_choices

    tools = CatalogSource(None).current().tools

    started = onboarding_choices(tools, share_with_team=True, choices={})
    chosen = onboarding_choices(tools, share_with_team=True, choices={"http:exa": "private", "model_keys": "private"})
    alone = onboarding_choices(tools, share_with_team=False, choices={"http:exa": "shared"})

    assert started == {
        MODEL_KEYS_SHARING_SUBJECT: AccountSharing.SHARED,
        "cli:huggingface": AccountSharing.SHARED,
        "cli:x": AccountSharing.PRIVATE,
        "http:brave-search": AccountSharing.SHARED,
        "http:exa": AccountSharing.SHARED,
    }
    assert chosen["http:exa"] is AccountSharing.PRIVATE and chosen[MODEL_KEYS_SHARING_SUBJECT] is AccountSharing.PRIVATE
    assert set(alone.values()) == {AccountSharing.PRIVATE}, "sharing off keeps everything for the administrator alone"


@pytest.mark.parametrize("choices", [{"cli:github": "shared"}, {"http:nothing": "shared"}, {"http:exa": "everyone"}])
def test_onboarding_choices_refuse_what_the_team_cannot_choose(choices: dict[str, str]) -> None:
    from access_control.account_sharing import onboarding_choices

    with pytest.raises(ValueError):
        onboarding_choices(CatalogSource(None).current().tools, share_with_team=True, choices=choices)


def test_a_snapshot_reads_the_teams_choices_once_for_a_page_of_tools(links: AccessRepository) -> None:
    # The Access page asks about every tool on a page; it reads the choices once.
    choose_sharing(links, EXA, AccountSharing.PRIVATE)
    reads: list[str] = []

    class Counting:
        def account_sharing(self, team_id, subject):
            reads.append(subject)
            return links.account_sharing(team_id, subject)

        def team_account_sharing(self, team_id):
            reads.append("all")
            return links.team_account_sharing(team_id)

        def set_account_sharing(self, record):
            return links.set_account_sharing(record)

    snapshot = TeamAccountSharing(Counting(), TEST_TEAM).snapshot()
    tools = list(CatalogSource(None).current().tools.values())
    used = {tool.tool_id: snapshot.team_account_used(tool) for tool in tools}

    assert reads == ["all"] and len(tools) > 10
    assert used[EXA] is False and used["http:brave-search"] is True
    with pytest.raises(RuntimeError, match="read-only"):
        snapshot.choose_tool(catalog_tool(EXA), AccountSharing.SHARED, by=ADMIN)


def test_a_failing_accounts_command_is_logged_with_who_ran_it(caplog) -> None:
    import asyncio
    from types import SimpleNamespace

    from assistant_gateway.application.interactive_commands import _run_account_sharing_command

    class Failing:
        async def execute(self, _request):
            raise RuntimeError("the access store is locked")

    request = SimpleNamespace(channel_name="webchat", user_id="web-1", chat_id="chat-1", thread_id=None, canonical_user_id=ADMIN)
    with caplog.at_level("WARNING", logger="assistant_gateway.application.interactive_commands"):
        reply = asyncio.run(_run_account_sharing_command(Failing(), request, "/accounts private http:exa"))

    assert "did not finish because of an unexpected gateway error" in reply
    assert (
        f"event=account_sharing_command_failed person_id={ADMIN} channel=webchat command=/accounts private http:exa"
    ) in caplog.text


# -- Settings > Accounts ---------------------------------------------------------------


@pytest.mark.asyncio
async def test_the_settings_accounts_tab_reads_the_same_rows_as_the_signed_in_person(
    links: AccessRepository, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import asyncio
    from dataclasses import replace
    from types import SimpleNamespace

    from aiohttp.test_utils import TestClient, TestServer

    from assistant_gateway.access_inbox import GatewayAccessInbox, access_inbox_settings
    from assistant_gateway.access_setup import MembershipRoles
    from assistant_gateway.store import GatewayStateStore
    from tests.loop_storage_support import record_loop_opens
    from tests.test_gateway_website_app import InternalWebsiteInteractiveApp, _config, _UserTokenStatefulFakeGateway

    # The web token signs in as user_alpha; their roles are read on every request.
    store_account_link(links, EXA, "exa-alpha", owner_id="user_alpha")
    config = _config(tmp_path)
    config.interactive_apps.internal_website.web_chat_enabled = True
    state_store = GatewayStateStore(str(tmp_path / "gateway.db"))
    state_store.create_gateway_user(canonical_user_id="user_alpha", display_name="Alpha", timezone="UTC")
    state_store.create_team_membership(canonical_user_id="user_alpha", team_id=TEST_TEAM, role="admin")
    gateway = _UserTokenStatefulFakeGateway(state_store)
    gateway.access_inbox = GatewayAccessInbox(
        settings=replace(access_inbox_settings(config), team_id=TEST_TEAM, storage_settings=links.settings),
        memberships=MembershipRoles(state_store.identity_repository),
        manager_client=SimpleNamespace(),
        session_repository=state_store.session_repository,
        proactive_session=lambda: None,
        emit_delivery_event=gateway.emit_delivery_event,
    )
    gateway.accounts = Port(links).sharing
    website = InternalWebsiteInteractiveApp(config, gateway=gateway, bridge=SimpleNamespace())

    async with TestClient(TestServer(website.build_app())) as client:
        assert (await client.get("/api/web-chat/accounts")).status == 401
        assert (await client.post("/api/web-chat/auth", json={"token": "ua_user_token"})).status == 200

        # Both identity and account reads must stay off the loop shared by the chat apps.
        with record_loop_opens(monkeypatch) as loop_opens:
            # The browser cannot name another person: the page is the session's own.
            read = await client.get("/api/web-chat/accounts", params={"person_id": ALICE})
            assert read.status == 200 and read.headers["Cache-Control"] == "no-store"
            page = await read.json()
            services = {row["subject"]: row for row in page["services"]}
            assert page["status"] == "ok" and page["can_change"] is True
            assert (page["share_command"], page["private_command"]) == ("/accounts share", "/accounts private")
            assert page["services"][0]["subject"] == "model"
            assert services[EXA] == {
                "subject": EXA, "name": "Exa web search", "sharing": "shared", "fixed": False,
                "team_account": "not_linked", "own_account": "linked", "setup_command": "/link http:exa", "signs_in": False,
            }
            assert services["cli:github"]["fixed"] and services["cli:github"]["signs_in"]

            for role, can_change in (("owner", True), ("member", False)):
                await asyncio.to_thread(
                    state_store.change_team_membership_role, canonical_user_id="user_alpha", team_id=TEST_TEAM, role=role
                )
                refreshed = await client.get("/api/web-chat/accounts")
                assert refreshed.status == 200
                assert (await refreshed.json())["can_change"] is can_change

            await asyncio.to_thread(state_store.remove_team_membership, canonical_user_id="user_alpha", team_id=TEST_TEAM)
            refused = await client.get("/api/web-chat/accounts")
            assert refused.status == 403
            assert "not an active member" in (await refused.json())["error"]

        assert loop_opens.where == [], loop_opens.report()
