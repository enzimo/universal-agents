"""A person's own tool (catalog ``owner_scope: person``): history clean-up.

Real Cedar and the SQLite access store. The tool belongs to the person whose
work or request it is, never to whoever acts:

- each person holds their own (``std:owner``), and an agent holds it only
  through that person's grant for their own work;
- only the person gives it, only to TeamLead, only for their own work and
  never handed on: grants for everyone, for someone else's work, to workers,
  schedules, roles or people, and with delegation are refused, for
  administrators too, and no starter profile may name it;
- agents cannot ask for it with ``request_access``;
- a staged clean-up is decided only by its person, never by an
  administrator, and applies at once only when both sides hold it;
- a block on it is team-wide, so only administrators add one;
- the catalog keeps it out of every tool set; and a decided review keeps its
  after-decision lines only.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

import pytest

from access_catalog.catalog import CatalogError, load_catalog
from access_control.cedar import CedarAccessClient
from access_control.decisions import PERSON_OWNED_BY_NOBODY, AccessService, HandDownRefused
from access_control.entities import AgentPrincipal, PersonPrincipal, agent_role_resolver, schedule_subject
from access_control.requests import AccessRequests
from access_control.starter_profiles import StarterProfile, ProfileGrant, _wanted_grants
from custom_messages.contracts.access_v2 import (
    AccessLevel,
    AccessNoticeV1,
    AccessRequestStatus,
    AccessRequestV1,
    AccessReviewChoice,
    ChangeProposalV1,
    ClassificationSource,
    DelegationMode,
    GrantKind,
    GrantV1,
    OperationResolutionV1,
    OwnerScope,
    SubjectKind,
    SubjectRefV1,
    TargetKind,
    TargetRefV1,
)
from storage.access_repository import AccessRepository
from storage.config import resolve_storage_settings

ROOT = Path(__file__).resolve().parents[1]
BINARY = ROOT / "target" / "debug" / "ua-cedar-authorizer"
TEAM = "team-own"
LEAD = "TeamArchitect"
ADMIN = PersonPrincipal("admin-1", TEAM, ("admin",))
ALICE = PersonPrincipal("alice-1", TEAM, ("member",))
BOB = PersonPrincipal("bob-1", TEAM, ("member",))
LEAD_TYPE = SubjectRefV1(kind=SubjectKind.AGENT_TYPE, id=f"{TEAM}/{LEAD}")
WORKER_TYPE = SubjectRefV1(kind=SubjectKind.AGENT_TYPE, id=f"{TEAM}/WebResearchAgent")
TOOL = TargetRefV1(kind=TargetKind.TOOL, id="fn:history_cleanup")
CHANGE = TargetRefV1(kind=TargetKind.OPERATION, id="fn:history_cleanup#change:history_cleanup")
NOW = datetime(2026, 10, 27, 12, 0, tzinfo=timezone.utc)

pytestmark = pytest.mark.skipif(not BINARY.is_file(), reason="build ua-cedar-authorizer first (cargo build)")


@dataclass
class RecordingInbox:
    reviews: list[AccessRequestV1] = field(default_factory=list)
    notices: list[AccessNoticeV1] = field(default_factory=list)

    def open_review(self, request: AccessRequestV1) -> None:
        self.reviews.append(request)

    def send_notice(self, notice: AccessNoticeV1) -> None:
        self.notices.append(notice)

    def request_finished(self, request: AccessRequestV1) -> None:
        return None


@pytest.fixture
def requests(tmp_path: Path):
    storage = resolve_storage_settings({"backend": "sqlite", "sqlite": {"path": str(tmp_path / "manager.db")}})
    service = AccessService(
        store=AccessRepository(storage),
        catalog=load_catalog(None),
        cedar=CedarAccessClient(binary=str(BINARY)),
        team_id=TEAM,
        agent_role_of=agent_role_resolver(LEAD),
        clock=lambda: NOW,
    )
    yield AccessRequests(service=service, inbox=RecordingInbox(), lead_agent_type=LEAD)
    service.cedar.close()


def lead_for(person: PersonPrincipal) -> AgentPrincipal:
    return AgentPrincipal("agent-lead", LEAD, "team_lead", TEAM, work_id=f"work-{person.person_id}")


def grant(by: PersonPrincipal, **fields) -> GrantV1:
    values = {
        "grant_id": "new",
        "team_id": TEAM,
        "subject": LEAD_TYPE,
        "target": TOOL,
        "level": AccessLevel.FULL,
        "granted_by": SubjectRefV1(kind=SubjectKind.USER, id=by.person_id),
        "created_at_utc": NOW,
        "for_person_id": by.person_id,
    }
    values.update(fields)
    return GrantV1(**values)


def proposal(person: PersonPrincipal) -> ChangeProposalV1:
    return ChangeProposalV1(
        kind="history_cleanup",
        change_id="history_cleanup_0123456789ab",
        plan_digest="digest",
        title="Delete part of your chat history",
        summary="Delete 1 conversation of yours. Cannot be undone.",
        effect=("“Planning the trip”: 3 turns.",),
        effect_after_decision=("Conversation conv-1: 3 turns.",),
        owner_person_id=person.person_id,
    )


def test_the_catalog_marks_history_cleanup_as_each_persons_own_and_in_no_tool_set(requests) -> None:
    tool = requests.service.catalog.tools["fn:history_cleanup"]
    assert tool.owner_scope is OwnerScope.PERSON and tool.tool_sets == ()
    assert requests.service.catalog.changes["history_cleanup"] == ("fn:history_cleanup", AccessLevel.FULL)


def test_a_person_own_tool_in_a_tool_set_is_refused(tmp_path: Path) -> None:
    tools = tmp_path / "instance" / "control" / "access" / "tools"
    tools.mkdir(parents=True)
    (tools / "own.yaml").write_text(
        "tools:\n  - tool_id: http:own\n    display_name: Own\n    owner_scope: person\n"
        "    hosts: [own.example.test]\n    tool_sets: [standard]\n",
        encoding="utf-8",
    )
    with pytest.raises(CatalogError):
        load_catalog(tmp_path / "instance").validate()


def test_each_person_holds_their_own_and_nobody_holds_it_for_someone_else(requests) -> None:
    service = requests.service
    for person in (ALICE, BOB, ADMIN):
        assert service.holds(person, TOOL, AccessLevel.FULL, task_id=None)
        assert service.holds(person, CHANGE, AccessLevel.FULL, task_id=None)
    # An administrator deciding Alice's request does not own Alice's tool.
    assert not service.may_give(ADMIN, CHANGE, AccessLevel.FULL, task_id=None, for_person_id=ALICE.person_id)
    assert service.may_give(ALICE, CHANGE, AccessLevel.FULL, task_id=None, for_person_id=ALICE.person_id)
    assert service.person_owner("fn:history_cleanup", None) == PERSON_OWNED_BY_NOBODY
    assert service.person_owner("fn:files", ALICE.person_id) is None


def test_teamlead_holds_it_only_for_the_person_who_gave_it(requests) -> None:
    service = requests.service
    service.grant_by_person(ALICE, grant(ALICE))
    resolution = OperationResolutionV1(
        tool_id="fn:history_cleanup", operation="change:history_cleanup", level=AccessLevel.FULL,
        source=ClassificationSource.TRUSTED,
    )
    for_alice = service.decide(agent=lead_for(ALICE), person=ALICE, resolution=resolution, task_id="t-a")
    for_bob = service.decide(agent=lead_for(BOB), person=BOB, resolution=resolution, task_id="t-b")
    assert for_alice.allowed and not for_bob.allowed


@pytest.mark.parametrize(
    ("by", "fields", "reason"),
    [
        (ADMIN, {"for_person_id": None}, "each person gives it only for their own work"),
        (ADMIN, {"for_person_id": ALICE.person_id}, "each person gives it only for their own work"),
        (ALICE, {"for_person_id": BOB.person_id}, "each person gives it only for their own work"),
        (ALICE, {"delegation_depth": 1}, "cannot be handed on"),
        (ALICE, {"subject": WORKER_TYPE}, "only TeamLead can be given it|Workers keep no lasting access"),
        (ALICE, {"subject": SubjectRefV1(kind=SubjectKind.ROLE, id=f"{TEAM}/worker")}, "only TeamLead can be given it|Workers keep no lasting access"),
        (ALICE, {"subject": schedule_subject(TEAM, "sched-1")}, "only TeamLead can be given it"),
        (ALICE, {"subject": SubjectRefV1(kind=SubjectKind.USER, id=BOB.person_id), "for_person_id": None}, "only TeamLead"),
    ],
)
def test_grants_other_than_the_persons_own_to_teamlead_are_refused(requests, by, fields, reason) -> None:
    with pytest.raises(HandDownRefused, match=reason):
        requests.service.grant_by_person(by, grant(by, **fields))


def test_a_member_may_give_it_to_teamlead_for_their_own_work_and_remove_it(requests) -> None:
    given = requests.service.grant_by_person(ALICE, grant(ALICE))
    assert given.for_person_id == ALICE.person_id and given.delegation_depth == 0
    assert requests.service.revoke_by_person(ALICE, given.grant_id)


def test_agents_cannot_ask_for_it(requests) -> None:
    with pytest.raises(HandDownRefused, match="Nobody asks for fn:history_cleanup"):
        requests.submit(requester=lead_for(ALICE), person=ALICE, target=TOOL, level=AccessLevel.FULL, reason="please")


def test_a_cleanup_review_is_decided_by_its_person_and_never_by_an_administrator(requests) -> None:
    outcome = requests.submit_change(
        requester=lead_for(ALICE), person=ALICE, proposal=proposal(ALICE), reason="clean up", task_id="t-a"
    )
    assert outcome.request is not None and not outcome.already_held
    assert requests.may_decide(ALICE, outcome.request)
    assert not requests.may_decide(ADMIN, outcome.request)
    assert not requests.may_decide(BOB, outcome.request)
    with pytest.raises(HandDownRefused):
        requests.decide(outcome.request.request_id, ADMIN, AccessReviewChoice.APPROVE)
    decided = requests.decide(outcome.request.request_id, ALICE, AccessReviewChoice.DENY)
    assert decided.status is AccessRequestStatus.DENIED
    stored = requests.service.store.get_request(outcome.request.request_id)
    assert stored is not None and stored.change is not None
    assert stored.change.effect == ("Conversation conv-1: 3 turns.",)
    assert stored.change.effect_after_decision == ()


def test_with_the_persons_grant_the_cleanup_applies_at_once(requests) -> None:
    requests.service.grant_by_person(ALICE, grant(ALICE))
    outcome = requests.submit_change(
        requester=lead_for(ALICE), person=ALICE, proposal=proposal(ALICE), reason="clean up", task_id="t-a"
    )
    assert outcome.already_held and outcome.request is None


def test_only_administrators_block_it_because_a_block_is_team_wide(requests) -> None:
    with pytest.raises(HandDownRefused, match="only an administrator adds one"):
        requests.block(ALICE, subject=LEAD_TYPE, target=TOOL, reason="stop")
    block = requests.block(ADMIN, subject=LEAD_TYPE, target=TOOL, reason="stop for now")
    assert block.kind is GrantKind.BLOCK


def test_no_starter_profile_may_name_it(requests) -> None:
    profile = StarterProfile(
        name="sneaky", display_name="Sneaky", description="", grants=(ProfileGrant(target=TOOL, level=AccessLevel.FULL, delegation_depth=0, delegation_mode=DelegationMode.AUTOMATIC),)
    )
    with pytest.raises(ValueError, match="only its person opens"):
        _wanted_grants(requests.service, ALICE, profile, LEAD_TYPE, NOW)
