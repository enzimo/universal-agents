"""Agents' restrictions and revocations reach only their own request and person.

A person often has several requests open at once, and they may use the same
worker type. The team lead's restriction in one request, and its revocation of
what it handed down, must not change another request's workers, the person's
or anyone else's (owner's decision 2026-10-19). People's and administrators'
blocks stay team-wide.

Real Cedar, the real access service and the access store on SQLite and on
PostgreSQL (``LIVE_TEST_POSTGRES_DSN``).
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import Iterator

import pytest
from pydantic import ValidationError

from access_catalog.catalog import load_catalog
from access_control.cedar import CedarAccessClient
from access_control.decisions import AccessService, HandDownRefused
from access_control.entities import AgentPrincipal, PersonPrincipal, agent_role_resolver
from access_control.requests import AccessRequests
from access_control.starter_profiles import apply_starter_profile
from custom_messages.contracts.access_v2 import (
    AccessLevel,
    GrantKind,
    GrantLifetime,
    GrantStatus,
    GrantV1,
    OperationResolutionV1,
    SubjectKind,
    SubjectRefV1,
    TargetKind,
    TargetRefV1,
)
from id_generation import new_id
from storage.access_repository import AccessRepository
from storage.config import resolve_storage_settings
from tests.test_access_requests import RecordingInbox
from tests.test_access_store_postgres import postgres_storage

ROOT = Path(__file__).resolve().parents[1]
BINARY = ROOT / "target" / "debug" / "ua-cedar-authorizer"
TEAM = "team-1"
LEAD = "TeamArchitect"
OWNER = PersonPrincipal("owner-1", TEAM, ("owner",))
MEMBER = PersonPrincipal("member-1", TEAM, ("member",))
SCHEDULES = TargetRefV1(kind=TargetKind.TOOL, id="fn:schedules")
CREATE = TargetRefV1(kind=TargetKind.OPERATION, id="fn:schedules#create_scheduled_job")

pytestmark = pytest.mark.skipif(not BINARY.is_file(), reason="build ua-cedar-authorizer first (cargo build)")


@pytest.fixture(
    params=[
        "sqlite",
        pytest.param("postgres", marks=[pytest.mark.postgres, pytest.mark.live, pytest.mark.integration]),
    ]
)
def requests(request, tmp_path: Path) -> Iterator[AccessRequests]:
    def build(settings) -> AccessRequests:
        service = AccessService(
            store=AccessRepository(settings),
            catalog=load_catalog(None),
            cedar=CedarAccessClient(binary=str(BINARY)),
            team_id=TEAM,
            agent_role_of=agent_role_resolver(LEAD),
        )
        for person in (OWNER, MEMBER):
            apply_starter_profile(service, person, "trusted", lead_agent_type=LEAD)
        return AccessRequests(service=service, inbox=RecordingInbox(), lead_agent_type=LEAD)

    if request.param == "postgres":
        with postgres_storage() as settings:
            built = build(settings)
            yield built
            built.service.cedar.close()
        return
    built = build(resolve_storage_settings({"backend": "sqlite", "sqlite": {"path": str(tmp_path / "manager.db")}}))
    yield built
    built.service.cedar.close()


def lead_in(request_id: str) -> AgentPrincipal:
    """The team lead working on one request (``work_id``: the request's signed context id)."""

    return AgentPrincipal("agent-lead", LEAD, "team_lead", TEAM, work_id=request_id)


def worker_in(request_id: str) -> AgentPrincipal:
    return AgentPrincipal(f"agent-web-{request_id}", "WebResearchAgent", "worker", TEAM, work_id=request_id)


def may_create(requests: AccessRequests, request_id: str, person: PersonPrincipal) -> bool:
    """Whether the worker type, on a task of ``request_id`` for ``person``, may create a schedule."""

    resolution = OperationResolutionV1(
        tool_id="fn:schedules", operation="create_scheduled_job", level=AccessLevel.WRITE, source="trusted"
    )
    # As the per-call hook decides: what the team lead may hand on is added to the task at once.
    decision = requests.service.decide(
        agent=worker_in(request_id), person=person, resolution=resolution, task_id=request_id, hand_on_for_task=True
    )
    return decision.allowed


def restrict_in(requests: AccessRequests, request_id: str, person: PersonPrincipal) -> GrantV1:
    return requests.restrict(
        lead_in(request_id),
        recipient_agent_type="WebResearchAgent",
        target=CREATE,
        reason="Only change existing schedules",
        task_id=request_id,
        person_id=person.person_id,
    )


def hand_down_in(requests: AccessRequests, request_id: str, person: PersonPrincipal) -> GrantV1:
    given = requests.give(
        lead_in(request_id),
        recipient=worker_in(request_id).type_subject(),
        target=SCHEDULES,
        level=AccessLevel.WRITE,
        reason="Track the report",
        lifetime=GrantLifetime.TASK,
        task_id=request_id,
        person=person,
    )
    assert given.grant is not None and given.grant.status is GrantStatus.ACTIVE
    return given.grant


def test_a_restriction_made_in_one_request_does_not_reach_another(requests) -> None:
    block = restrict_in(requests, "request-a", OWNER)

    stored = requests.service.store.get(block.grant_id)
    assert stored.kind is GrantKind.BLOCK
    assert (stored.lifetime, stored.task_id, stored.for_person_id) == (GrantLifetime.TASK, "request-a", OWNER.person_id)
    assert not may_create(requests, "request-a", OWNER)
    assert may_create(requests, "request-b", OWNER), "the person's other request keeps the worker type's access"
    assert may_create(requests, "request-c", MEMBER), "another person's request keeps it too"

    # The restriction ends with its request, like access given for one task.
    assert block.grant_id in requests.service.end_task_grants(["request-a"])
    assert may_create(requests, "request-a", OWNER)


def test_an_agent_revocation_for_another_persons_request_is_refused(requests) -> None:
    members = hand_down_in(requests, "request-m", MEMBER)

    with pytest.raises(HandDownRefused, match="request you are working on"):
        requests.revoke(lead_in("request-o"), members.grant_id, task_id="request-o", person_id=OWNER.person_id)

    assert requests.service.store.get(members.grant_id).status is GrantStatus.ACTIVE
    revoked = requests.revoke(lead_in("request-m"), members.grant_id, task_id="request-m", person_id=MEMBER.person_id)
    assert revoked.revoked == [members.grant_id] and revoked.kept_by_people == []


def test_an_agent_revocation_ends_only_its_own_requests_part_of_an_entry(requests) -> None:
    first = hand_down_in(requests, "request-a", OWNER)
    second = hand_down_in(requests, "request-b", OWNER)
    # Same worker type, person and tool: one entry, with a part for each request.
    assert {grant.grant_id for grant in requests.service.entry_grants(first)} == {first.grant_id, second.grant_id}

    with pytest.raises(HandDownRefused, match="request you are working on"):
        requests.revoke(lead_in("request-a"), second.grant_id, task_id="request-a", person_id=OWNER.person_id)
    revoked = requests.revoke(lead_in("request-a"), first.grant_id, task_id="request-a", person_id=OWNER.person_id)

    assert revoked.revoked == [first.grant_id]
    assert requests.service.store.get(second.grant_id).status is GrantStatus.ACTIVE
    assert requests.service.holds(
        worker_in("request-b"), SCHEDULES, AccessLevel.WRITE, task_id="request-b", person_id=OWNER.person_id
    )


def test_another_requests_restriction_and_a_team_wide_block_are_out_of_an_agents_reach(requests) -> None:
    block = restrict_in(requests, "request-a", OWNER)
    with pytest.raises(HandDownRefused, match="request you are working on"):
        requests.revoke(lead_in("request-b"), block.grant_id, task_id="request-b", person_id=OWNER.person_id)
    assert not may_create(requests, "request-a", OWNER)

    # A block an agent made before restrictions were bound to their request applies to the whole team;
    # only a person removes it.
    team_wide = requests.service.store.create(
        GrantV1(
            grant_id=new_id("grant"),
            team_id=TEAM,
            kind=GrantKind.BLOCK,
            subject=worker_in("x").type_subject(),
            target=CREATE,
            granted_by=lead_in("x").type_subject(),
            reason="Made before this change",
            created_at_utc=requests.service.clock(),
        )
    )
    with pytest.raises(HandDownRefused, match="request you are working on"):
        requests.revoke(lead_in("request-b"), team_wide.grant_id, task_id="request-b", person_id=OWNER.person_id)
    requests.unblock(OWNER, team_wide.grant_id)

    revoked = requests.revoke(lead_in("request-a"), block.grant_id, task_id="request-a", person_id=OWNER.person_id)
    assert revoked.revoked == [block.grant_id]
    assert may_create(requests, "request-a", OWNER)


def test_peoples_and_administrators_blocks_stay_team_wide(requests) -> None:
    worker_type = worker_in("x").type_subject()
    block = requests.block(OWNER, subject=worker_type, target=CREATE, reason="No new schedules for now")

    assert block.task_id is None and block.for_person_id is None and block.lifetime is GrantLifetime.UNTIL_REVOKED
    for request_id, person in (("request-a", OWNER), ("request-b", OWNER), ("request-c", MEMBER)):
        assert not may_create(requests, request_id, person), (request_id, person.person_id)
    with pytest.raises(HandDownRefused, match="only access your agent type gave"):
        requests.revoke(lead_in("request-a"), block.grant_id, task_id="request-a", person_id=OWNER.person_id)

    # Blocking the whole team stops every request at once too.
    requests.unblock(OWNER, block.grant_id)
    team = requests.block(
        OWNER, subject=SubjectRefV1(kind=SubjectKind.TEAM, id=TEAM), target=CREATE, reason="Pause schedule creation"
    )
    assert not may_create(requests, "request-a", OWNER) and not may_create(requests, "request-c", MEMBER)
    requests.unblock(OWNER, team.grant_id)
    assert may_create(requests, "request-a", OWNER) and may_create(requests, "request-c", MEMBER)


def test_a_restriction_needs_the_request_it_is_made_in(requests) -> None:
    with pytest.raises(HandDownRefused, match="request you are working on"):
        requests.restrict(
            lead_in("request-a"),
            recipient_agent_type="WebResearchAgent",
            target=CREATE,
            reason="x",
            task_id=None,
            person_id=OWNER.person_id,
        )


def test_a_restriction_for_one_persons_work_is_bound_to_one_request() -> None:
    fields = dict(
        grant_id="g",
        team_id=TEAM,
        kind=GrantKind.BLOCK,
        subject=worker_in("x").type_subject(),
        target=CREATE,
        granted_by=lead_in("x").type_subject(),
        created_at_utc=datetime.now(timezone.utc),
        for_person_id=OWNER.person_id,
    )

    bound = GrantV1(**fields, lifetime=GrantLifetime.TASK, task_id="request-a")
    assert bound.for_person_id == OWNER.person_id
    with pytest.raises(ValidationError, match="bound to one request"):
        GrantV1(**fields)
    on_a_person = {**fields, "subject": SubjectRefV1(kind=SubjectKind.USER, id=MEMBER.person_id)}
    with pytest.raises(ValidationError, match="people's grants cannot"):
        GrantV1(**on_a_person, lifetime=GrantLifetime.TASK, task_id="request-a")
