"""Reviewers choose how long access lasts and whether TeamLead may hand it on (owner's intent, D13).

Every person authorizes agents for their own work: for this task, for a
while, or until removed; a person may let TeamLead hand what they approved on
to workers without asking them again; and every member picks TeamLead's
standing access for their own work with a starter profile.
"""

from __future__ import annotations

from dataclasses import replace
from datetime import timedelta
from types import SimpleNamespace

import pytest

from access_control.decisions import HandDownRefused
from access_control.entities import AgentPrincipal, PersonPrincipal
from access_control.requests import ACCESS_PROPOSAL_CHANGE, FILE_DELIVERY_CHANGE
from access_control.starter_profiles import applied_profile, apply_starter_profile
from assistant_gateway.access_inbox import GatewayAccessInbox
from custom_messages.contracts.access_v2 import (
    AccessLevel,
    AccessReviewChoice,
    GrantLifetime,
    TargetKind,
    TargetRefV1,
)
from tests.test_access_requests import BINARY, LEAD, MEMBER, OWNER, SCHEDULES, TEAM, WORKER, ask, uses
from tests.test_access_requests import requests as requests_fixture

pytestmark = pytest.mark.skipif(not BINARY.is_file(), reason="build ua-cedar-authorizer first (cargo build)")

requests = requests_fixture
LEAD_AGENT = AgentPrincipal("agent-lead", LEAD, "team_lead", TEAM)


def test_kept_access_can_last_for_a_set_time(requests) -> None:
    outcome = ask(requests)
    with pytest.raises(HandDownRefused, match="hours"):
        requests.decide(outcome.request.request_id, OWNER, AccessReviewChoice.APPROVE, keep=True, keep_hours=0)
    before = requests.service.clock()
    decided = requests.decide(outcome.request.request_id, OWNER, AccessReviewChoice.APPROVE, keep=True, keep_hours=24)
    # Kept for the team lead (workers keep nothing); the worker has it for its task.
    grant, task_grant = [requests.service.store.get(grant_id) for grant_id in decided.grant_ids]
    assert grant.subject.id == f"{TEAM}/{LEAD}" and task_grant.lifetime is GrantLifetime.TASK
    assert grant.lifetime is GrantLifetime.UNTIL
    assert timedelta(hours=23, minutes=59) <= grant.expires_at_utc - before <= timedelta(hours=24, minutes=1)


def test_a_person_lets_teamlead_hand_on_what_it_asked_for_and_workers_then_get_it_without_asking(requests) -> None:
    lead_request = requests.submit(
        requester=LEAD_AGENT, person=OWNER, target=SCHEDULES, level=AccessLevel.WRITE, reason="Plan the week", task_id="task-1"
    )
    decided = requests.decide(lead_request.request.request_id, OWNER, AccessReviewChoice.APPROVE, keep=True, hand_on=True)
    [grant] = [requests.service.store.get(grant_id) for grant_id in decided.grant_ids]
    assert grant.delegation_depth == 1 and grant.for_person_id == OWNER.person_id

    handed = ask(requests)  # a worker asks while working for the same person
    assert handed.grant is not None and not handed.waiting, "handed down without asking the person"
    assert uses(requests, WORKER, OWNER, "fn:schedules#create_scheduled_job")


def test_workers_never_hand_on_and_their_kept_access_goes_to_the_team_lead(requests) -> None:
    outcome = ask(requests)
    decided = requests.decide(outcome.request.request_id, OWNER, AccessReviewChoice.APPROVE, hand_on=False)
    lead_grant, task_grant = [requests.service.store.get(grant_id) for grant_id in decided.grant_ids]
    # The worker's own access is for its task and cannot be handed on; the team
    # lead keeps it and may hand it on, since that is how workers get access.
    assert task_grant.subject == WORKER.type_subject() and task_grant.delegation_depth == 0
    assert lead_grant.subject.id == f"{TEAM}/{LEAD}" and lead_grant.delegation_depth == 1


def test_handing_on_starts_ticked_for_the_team_leads_own_requests(requests) -> None:
    def approve(hand_on):
        asked = requests.submit(
            requester=replace(LEAD_AGENT, work_id=f"task-{hand_on}"), person=OWNER, target=SCHEDULES,
            level=AccessLevel.READ, reason="Plan", task_id="lead-task",
        )
        decided = requests.decide(asked.request.request_id, OWNER, AccessReviewChoice.APPROVE_TASK, hand_on=hand_on)
        return requests.service.store.get(decided.grant_ids[0])

    # No box (chat buttons): yes. The web card sends its box, ticked or not.
    assert approve(None).delegation_depth == 1
    assert approve(True).delegation_depth == 1
    assert approve(False).delegation_depth == 0


def test_every_member_chooses_teamleads_standing_access_for_their_own_work(requests) -> None:
    other = PersonPrincipal("member-2", TEAM, ("member",))
    apply_starter_profile(requests.service, MEMBER, "trusted", lead_agent_type=LEAD)

    assert applied_profile(requests.service, MEMBER.person_id) == "trusted"
    assert applied_profile(requests.service, other.person_id) is None
    handed = ask(requests, person=MEMBER)
    assert handed.grant is not None and handed.grant.for_person_id == MEMBER.person_id
    assert ask(requests, person=other).waiting, "another member's work gets nothing from it"
    publishing = TargetRefV1(kind=TargetKind.TOOL, id="fn:publishing")
    assert not requests.service.holds(LEAD_AGENT, publishing, AccessLevel.WRITE, task_id=None, person_id=other.person_id)


def test_the_web_card_names_what_one_approval_covers() -> None:
    def labels(kind: str | None) -> list[str]:
        request = SimpleNamespace(change=SimpleNamespace(kind=kind) if kind else None)
        return list(GatewayAccessInbox._web_labels(request).values())

    assert labels(ACCESS_PROPOSAL_CHANGE) == ["Approve all", "Deny"], "one button approves every row"
    assert labels(FILE_DELIVERY_CHANGE) == ["Approve", "Deny"]
    assert labels(None) == ["Approve", "Approve reading only", "Deny"]
