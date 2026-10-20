"""Access requests, hand-downs, restrictions and revocations, with real Cedar and SQLite.

Covers the plan's flows F3–F7: a worker's request handed down at once under
an automatic team-lead grant (with a notice to the person at the root), a
request waiting for a person under the careful and manual profiles, each
review choice, a person who lacks the access themselves, "ask me each time"
hand-downs, double decisions, withdrawal, and an agent giving, restricting
and revoking.
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from pathlib import Path

import pytest

from access_catalog.catalog import load_catalog
from access_control.cedar import CedarAccessClient
from access_control.decisions import AccessService, HandDownRefused
from access_control.entities import AgentPrincipal, PersonPrincipal, agent_role_resolver, schedule_subject
from access_control.requests import AccessRequests
from access_control.starter_profiles import apply_starter_profile
from custom_messages.contracts.access_v2 import (
    AccessLevel,
    AccessNoticeV1,
    AccessRequestStatus,
    AccessRequestV1,
    AccessReviewChoice,
    DelegationMode,
    GrantLifetime,
    GrantStatus,
    GrantV1,
    OperationResolutionV1,
    SubjectKind,
    SubjectRefV1,
    TargetKind,
    TargetRefV1,
)
from storage.access_repository import AccessRepository
from storage.config import resolve_storage_settings

ROOT = Path(__file__).resolve().parents[1]
BINARY = ROOT / "target" / "debug" / "ua-cedar-authorizer"
TEAM = "team-1"
LEAD = "TeamArchitect"
OWNER = PersonPrincipal("owner-1", TEAM, ("owner",))
MEMBER = PersonPrincipal("member-1", TEAM, ("member",))
# work_id: the request its task works on (task-only access is bound to it).
WORKER = AgentPrincipal("agent-web", "WebResearchAgent", "worker", TEAM, work_id="task-1")
SCHEDULES = TargetRefV1(kind=TargetKind.TOOL, id="fn:schedules")
PUBLISHING = TargetRefV1(kind=TargetKind.TOOL, id="fn:publishing")
# A tool members do not hold by default (not in the standard set).
X_POSTS = TargetRefV1(kind=TargetKind.TOOL, id="cli:x")

pytestmark = pytest.mark.skipif(not BINARY.is_file(), reason="build ua-cedar-authorizer first (cargo build)")


@dataclass
class RecordingInbox:
    reviews: list[AccessRequestV1] = field(default_factory=list)
    notices: list[AccessNoticeV1] = field(default_factory=list)
    finished: list[AccessRequestV1] = field(default_factory=list)

    def open_review(self, request: AccessRequestV1) -> None:
        self.reviews.append(request)

    def send_notice(self, notice: AccessNoticeV1) -> None:
        self.notices.append(notice)

    def request_finished(self, request: AccessRequestV1) -> None:
        self.finished.append(request)


@pytest.fixture
def requests(tmp_path: Path):
    storage = resolve_storage_settings({"backend": "sqlite", "sqlite": {"path": str(tmp_path / "manager.db")}})
    service = AccessService(
        store=AccessRepository(storage),
        catalog=load_catalog(None),
        cedar=CedarAccessClient(binary=str(BINARY)),
        team_id=TEAM,
        agent_role_of=agent_role_resolver(LEAD),
    )
    yield AccessRequests(service=service, inbox=RecordingInbox(), lead_agent_type=LEAD)
    service.cedar.close()


def uses(requests: AccessRequests, agent: AgentPrincipal, person: PersonPrincipal, operation: str, *, task_id="task-1") -> bool:
    tool_id, _, name = operation.partition("#")
    level = requests.service.catalog.tools[tool_id].operation(name).level
    resolution = OperationResolutionV1(tool_id=tool_id, operation=name, level=level, source="trusted")
    return requests.service.decide(agent=agent, person=person, resolution=resolution, task_id=task_id).allowed


def ask(requests: AccessRequests, target=SCHEDULES, level=AccessLevel.WRITE, person=OWNER, **kwargs):
    return requests.submit(requester=WORKER, person=person, target=target, level=level, reason="Track the report", task_id="task-1", **kwargs)


def test_the_team_lead_hands_a_workers_request_down_at_once_and_the_owner_is_told(requests) -> None:
    apply_starter_profile(requests.service, OWNER, "trusted", lead_agent_type=LEAD)
    assert not uses(requests, WORKER, OWNER, "fn:schedules#create_scheduled_job")

    outcome = ask(requests)

    assert outcome.request.status is AccessRequestStatus.GRANTED and not outcome.waiting
    assert outcome.grant.granted_by == SubjectRefV1(kind=SubjectKind.AGENT_TYPE, id=f"{TEAM}/{LEAD}")
    # For the worker's task only; workers keep no lasting access ("access follows the work").
    assert outcome.grant.lifetime is GrantLifetime.TASK and outcome.grant.task_id == "task-1"
    assert uses(requests, WORKER, OWNER, "fn:schedules#create_scheduled_job")
    assert not uses(requests, WORKER, OWNER, "fn:schedules#create_scheduled_job", task_id="task-2")
    assert requests.inbox.notices == [], "access added to one task is not announced"
    assert requests.inbox.reviews == [], "nobody is asked"
    assert requests.service.store.get_request(outcome.request.request_id).status is AccessRequestStatus.GRANTED

    again = ask(requests)
    assert again.already_held and again.request is None


def test_under_the_careful_profile_a_change_request_waits_and_can_be_approved_read_only(requests) -> None:
    apply_starter_profile(requests.service, OWNER, "careful", lead_agent_type=LEAD)

    outcome = ask(requests)

    assert outcome.waiting and requests.inbox.reviews == [outcome.request]
    assert requests.inbox.notices == []
    decided = requests.decide(outcome.request.request_id, OWNER, AccessReviewChoice.APPROVE_READ, note="Read is enough")
    assert decided.status is AccessRequestStatus.GRANTED and decided.decision is AccessReviewChoice.APPROVE_READ
    # Kept: the team lead keeps it and may hand it on; the worker has it for its task.
    lead_grant, task_grant = [requests.service.store.get(grant_id) for grant_id in decided.grant_ids]
    assert lead_grant.subject.id == f"{TEAM}/{LEAD}" and lead_grant.delegation_depth == 1
    assert lead_grant.lifetime is GrantLifetime.UNTIL_REVOKED and task_grant.lifetime is GrantLifetime.TASK
    assert {lead_grant.level, task_grant.level} == {AccessLevel.READ}
    assert lead_grant.granted_by.id == OWNER.person_id
    assert uses(requests, WORKER, OWNER, "fn:schedules#prepare_scheduled_job_access")
    assert not uses(requests, WORKER, OWNER, "fn:schedules#create_scheduled_job")
    assert requests.inbox.finished == [decided]

    with pytest.raises(HandDownRefused, match="already granted"):
        requests.decide(outcome.request.request_id, OWNER, AccessReviewChoice.APPROVE)


def test_approving_for_one_task_binds_the_grant_to_that_task(requests) -> None:
    outcome = ask(requests)  # manual: the team lead holds nothing

    decided = requests.decide(outcome.request.request_id, OWNER, AccessReviewChoice.APPROVE_TASK)

    grant = requests.service.store.get(decided.grant_ids[0])
    assert grant.lifetime is GrantLifetime.TASK and grant.task_id == "task-1"
    assert uses(requests, WORKER, OWNER, "fn:schedules#create_scheduled_job")
    assert not uses(requests, WORKER, OWNER, "fn:schedules#create_scheduled_job", task_id="task-2")


def test_denial_and_withdrawal_create_nothing(requests) -> None:
    denied = requests.decide(ask(requests).request.request_id, OWNER, AccessReviewChoice.DENY, note="Not now")
    assert denied.status is AccessRequestStatus.DENIED and denied.grant_ids == ()

    withdrawn = requests.withdraw(ask(requests).request.request_id, why="The task was cancelled.")
    assert withdrawn.status is AccessRequestStatus.WITHDRAWN
    assert requests.service.store.list_team(TEAM) == []
    assert [request.status for request in requests.inbox.finished] == [AccessRequestStatus.DENIED, AccessRequestStatus.WITHDRAWN]


def test_a_person_without_the_access_needs_an_administrator_who_gives_it_to_both(requests) -> None:
    apply_starter_profile(requests.service, OWNER, "trusted", lead_agent_type=LEAD)

    outcome = ask(requests, person=MEMBER, target=X_POSTS)

    assert outcome.waiting and outcome.request.person_lacks_access, "no hand-down can help the member"
    assert not requests.may_decide(MEMBER, outcome.request)
    with pytest.raises(HandDownRefused):
        requests.decide(outcome.request.request_id, MEMBER, AccessReviewChoice.APPROVE)
    decided = requests.decide(outcome.request.request_id, OWNER, AccessReviewChoice.APPROVE)
    grants = [requests.service.store.get(grant_id) for grant_id in decided.grant_ids]
    assert {grant.subject for grant in grants} == {
        SubjectRefV1(kind=SubjectKind.USER, id=MEMBER.person_id),
        SubjectRefV1(kind=SubjectKind.AGENT_TYPE, id=f"{TEAM}/{LEAD}"),
        WORKER.type_subject(),
    }
    # The agent's access serves the member's work only (decision D13).
    assert {grant.for_person_id for grant in grants} == {None, MEMBER.person_id}
    assert uses(requests, WORKER, MEMBER, "cli:x#post.create")
    assert not uses(requests, WORKER, OWNER, "cli:x#post.create")


def test_a_person_who_holds_the_access_decides_for_their_own_work_and_nobody_else_does(requests) -> None:
    # The owner's starter profile serves the owner's work only, so it cannot help the member.
    apply_starter_profile(requests.service, OWNER, "trusted", lead_agent_type=LEAD)

    outcome = ask(requests, person=MEMBER)

    assert outcome.waiting and not outcome.request.person_lacks_access
    assert requests.may_decide(MEMBER, outcome.request)
    assert not requests.may_decide(OWNER, outcome.request), "administrators do not decide for a member's work"
    with pytest.raises(HandDownRefused):
        requests.decide(outcome.request.request_id, OWNER, AccessReviewChoice.APPROVE)
    decided = requests.decide(outcome.request.request_id, MEMBER, AccessReviewChoice.APPROVE)
    grant, _task_grant = [requests.service.store.get(grant_id) for grant_id in decided.grant_ids]
    assert grant.subject.id == f"{TEAM}/{LEAD}", "kept for the member's work by the team lead"
    assert grant.for_person_id == MEMBER.person_id and grant.granted_by.id == MEMBER.person_id
    assert uses(requests, WORKER, MEMBER, "fn:schedules#create_scheduled_job")
    assert not uses(requests, WORKER, OWNER, "fn:schedules#create_scheduled_job"), "nothing carries over"
    # The member can withdraw it again; so can the owner, who could have given it.
    assert requests.service.may_revoke(MEMBER, grant) and requests.service.may_revoke(OWNER, grant)


def test_a_hand_down_under_ask_me_each_time_waits_and_is_approved_in_place(requests) -> None:
    lead_grant = requests.service.grant_by_person(
        OWNER,
        GrantV1(
            grant_id="new",
            team_id=TEAM,
            subject=SubjectRefV1(kind=SubjectKind.AGENT_TYPE, id=f"{TEAM}/{LEAD}"),
            target=SCHEDULES,
            level=AccessLevel.WRITE,
            delegation_depth=1,
            delegation_mode=DelegationMode.ASK_OWNER,
            granted_by=SubjectRefV1(kind=SubjectKind.USER, id=OWNER.person_id),
            created_at_utc=requests.service.clock(),
        ),
    )

    outcome = ask(requests)

    [pending_id] = outcome.request.grant_ids
    assert outcome.waiting and requests.service.store.get(pending_id).status is GrantStatus.PENDING
    decided = requests.decide(outcome.request.request_id, OWNER, AccessReviewChoice.APPROVE)
    approved = requests.service.store.get(pending_id)
    assert decided.grant_ids == (pending_id,) and approved.status is GrantStatus.ACTIVE
    assert approved.parent_grant_id == lead_grant.grant_id
    assert uses(requests, WORKER, OWNER, "fn:schedules#create_scheduled_job")


def test_the_team_lead_gives_for_a_task_restricts_and_revokes(requests) -> None:
    apply_starter_profile(requests.service, OWNER, "trusted", lead_agent_type=LEAD)
    lead = AgentPrincipal("agent-lead", LEAD, "team_lead", TEAM, work_id="task-1")
    in_this_request = {"task_id": "task-1", "person_id": OWNER.person_id}

    # Workers get access for one task; lasting access for a person's work is refused.
    with pytest.raises(HandDownRefused, match="Workers keep no lasting access"):
        requests.give(lead, recipient=WORKER.type_subject(), target=SCHEDULES, level=AccessLevel.WRITE, reason="x", person=OWNER)
    given = requests.give(
        lead,
        recipient=WORKER.type_subject(),
        target=SCHEDULES,
        level=AccessLevel.WRITE,
        reason="Track the report",
        lifetime=GrantLifetime.TASK,
        task_id="task-1",
        person=OWNER,
    )
    assert given.grant.status is GrantStatus.ACTIVE and requests.inbox.notices == []
    assert given.grant.for_person_id == OWNER.person_id, "a hand-down serves the same person's work"
    assert uses(requests, WORKER, OWNER, "fn:schedules#create_scheduled_job")
    assert not uses(requests, WORKER, MEMBER, "fn:schedules#create_scheduled_job")
    # Working for the member, the lead holds nothing it may hand on.
    with pytest.raises(HandDownRefused, match="holds no"):
        requests.give(
            lead, recipient=WORKER.type_subject(), target=SCHEDULES, level=AccessLevel.WRITE, reason="x",
            lifetime=GrantLifetime.TASK, task_id="task-1", person=MEMBER,
        )

    block = requests.restrict(
        lead,
        recipient_agent_type=WORKER.agent_type,
        target=TargetRefV1(kind=TargetKind.OPERATION, id="fn:schedules#create_scheduled_job"),
        reason="Update existing schedules only",
        **in_this_request,
    )
    assert not uses(requests, WORKER, OWNER, "fn:schedules#create_scheduled_job")
    assert uses(requests, WORKER, OWNER, "fn:schedules#update_scheduled_job")
    with pytest.raises(HandDownRefused, match="only workers"):
        requests.restrict(WORKER, recipient_agent_type=LEAD, target=PUBLISHING, reason="No", **in_this_request)

    with pytest.raises(HandDownRefused, match="only access your agent type gave"):
        requests.revoke(WORKER, given.grant.grant_id, **in_this_request)
    assert requests.revoke(lead, block.grant_id, **in_this_request).revoked == [block.grant_id]
    assert uses(requests, WORKER, OWNER, "fn:schedules#create_scheduled_job")
    requests.revoke(lead, given.grant.grant_id, **in_this_request)
    assert not uses(requests, WORKER, OWNER, "fn:schedules#update_scheduled_job")


def test_requests_reject_targets_outside_the_catalog_and_task_lifetimes_without_a_task(requests) -> None:
    with pytest.raises(HandDownRefused):
        ask(requests, target=TargetRefV1(kind=TargetKind.TOOL, id="fn:nope"))
    with pytest.raises(ValueError, match="while working on a task"):
        requests.submit(
            requester=replace(WORKER, work_id=None), person=OWNER, target=SCHEDULES, level=AccessLevel.READ, reason="x",
            lifetime=GrantLifetime.TASK,
        )
    with pytest.raises(ValueError, match="hours"):
        ask(requests, lifetime=GrantLifetime.UNTIL)
    limited = ask(requests, lifetime=GrantLifetime.UNTIL, hours=2)
    decided = requests.decide(limited.request.request_id, OWNER, AccessReviewChoice.APPROVE)
    assert requests.service.store.get(decided.grant_ids[0]).expires_at_utc == limited.request.expires_at_utc


def test_access_kept_from_a_scheduled_run_is_for_that_schedule_only(requests) -> None:
    in_digest = replace(WORKER, schedule_id="digest")
    request = requests.submit(
        requester=in_digest, person=OWNER, target=X_POSTS, level=AccessLevel.READ, reason="Read mentions",
        task_id="run-1",
    ).request
    assert request.schedule_id == "digest"

    [grant_id] = requests.decide(request.request_id, OWNER, AccessReviewChoice.APPROVE, keep=True).grant_ids

    grant = requests.service.store.get(grant_id)
    assert grant.subject == schedule_subject(TEAM, "digest") and grant.lifetime is GrantLifetime.UNTIL_REVOKED
    assert uses(requests, in_digest, OWNER, "cli:x#auth.status", task_id="run-2")
    assert not uses(requests, replace(WORKER, schedule_id="triage"), OWNER, "cli:x#auth.status", task_id="run-3")
    assert not uses(requests, WORKER, OWNER, "cli:x#auth.status", task_id="task-9"), "not outside scheduled runs"


def test_the_team_lead_keeps_what_it_asked_for_in_a_scheduled_run_for_itself(requests) -> None:
    lead_in_digest = AgentPrincipal("agent-lead", LEAD, "team_lead", TEAM, schedule_id="digest", work_id="run-1")
    request = requests.submit(
        requester=lead_in_digest, person=OWNER, target=X_POSTS, level=AccessLevel.READ, reason="Read mentions",
        task_id="run-1",
    ).request

    [grant_id] = requests.decide(request.request_id, OWNER, AccessReviewChoice.APPROVE, keep=True).grant_ids

    grant = requests.service.store.get(grant_id)
    # Its lasting access for the person counts in all its work for them, scheduled runs included.
    assert grant.subject == SubjectRefV1(kind=SubjectKind.AGENT_TYPE, id=f"{TEAM}/{LEAD}")
    assert uses(requests, replace(lead_in_digest, schedule_id="triage"), OWNER, "cli:x#auth.status", task_id="run-2")
    assert not uses(requests, replace(WORKER, schedule_id="digest"), OWNER, "cli:x#auth.status", task_id="run-3")
