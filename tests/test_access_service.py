"""The access decision service against the real Cedar evaluator and SQLite.

Ported from the access-model lab (ua-architecture research 2026-09-23): the
gog and Portainer examples, the agent-plus-person check, hand-down rules,
cascading revocation, one-time and task grants, blocks, the platform
guardrail, tool sets, and failures that deny.
"""

from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from access_catalog.catalog import load_catalog
from access_control.cedar import CedarAccessClient
from access_control.decisions import AccessService, HandDownRefused
from access_control.entities import AgentPrincipal, PersonPrincipal, schedule_subject
from custom_messages.contracts.access_v2 import (
    OperationResolutionV1,
    AccessLevel,
    DelegationMode,
    GrantKind,
    GrantLifetime,
    GrantStatus,
    GrantV1,
    SubjectKind,
    SubjectRefV1,
    TargetKind,
    TargetRefV1,
)
from storage.access_repository import AccessRepository
from storage.config import resolve_storage_settings

ROOT = Path(__file__).resolve().parents[1]
BINARY = ROOT / "target" / "debug" / "ua-cedar-authorizer"
TEAM = "team_ua"
READ, WRITE, FULL = AccessLevel.READ, AccessLevel.WRITE, AccessLevel.FULL

pytestmark = pytest.mark.skipif(not BINARY.is_file(), reason="build ua-cedar-authorizer first (cargo build)")

ALICE = PersonPrincipal("alice", TEAM, ("owner",))
BOB = PersonPrincipal("bob", TEAM, ("member",))
LEAD = AgentPrincipal("agent_lead", "TeamArchitect", "team_lead", TEAM)
BOA = AgentPrincipal("agent_boa", "BackgroundOperationsAgent", "worker", TEAM)
BOA_TYPE = SubjectRefV1(kind=SubjectKind.AGENT_TYPE, id=f"{TEAM}/BackgroundOperationsAgent")
LEAD_TYPE = SubjectRefV1(kind=SubjectKind.AGENT_TYPE, id=f"{TEAM}/TeamArchitect")
GOOGLE = TargetRefV1(kind=TargetKind.TOOL, id="cli:google")
PORTAINER = TargetRefV1(kind=TargetKind.TOOL, id="http:portainer")


class Clock:
    def __init__(self) -> None:
        self.now = datetime(2026, 9, 24, 12, 0, tzinfo=timezone.utc)

    def __call__(self) -> datetime:
        return self.now


@pytest.fixture
def world(tmp_path):
    tools = tmp_path / "instance" / "control" / "access" / "tools"
    tools.mkdir(parents=True)
    (tools / "portainer.yaml").write_text(
        "tools:\n"
        "  - tool_id: http:portainer\n"
        "    display_name: Portainer\n"
        "    account: linked_credential\n"
        "    hosts: [portainer.example.test]\n"
        "    routes:\n"
        "      - {name: stacks.restart, method: POST, path: '/api/stacks/{id}/restart', level: write}\n",
        encoding="utf-8",
    )
    catalog = load_catalog(tmp_path / "instance")
    store = AccessRepository(
        resolve_storage_settings({"backend": "sqlite", "sqlite": {"path": str(tmp_path / "access.db")}})
    )
    cedar = CedarAccessClient(binary=str(BINARY))
    clock = Clock()
    service = AccessService(store=store, catalog=catalog, cedar=cedar, team_id=TEAM, clock=clock)
    yield service, clock
    cedar.close()


def spec(subject, target, level, **fields) -> GrantV1:
    return GrantV1(
        grant_id="new",
        team_id=TEAM,
        subject=subject,
        target=target,
        level=level,
        granted_by=SubjectRefV1(kind=SubjectKind.USER, id="placeholder"),
        created_at_utc=datetime(2026, 1, 1, tzinfo=timezone.utc),
        **fields,
    )


def gog(service, argv):
    return service.catalog.resolve_broker_action("google", "gog.exec", {"argv": argv})


def portainer(service, method, path):
    return service.catalog.resolve_http(method, f"https://portainer.example.test{path}")


def allowed(service, agent, person, resolution, task="task-1"):
    return service.decide(agent=agent, person=person, resolution=resolution, task_id=task).allowed


def test_gog_example_hand_down_and_cascading_revocation(world):
    service, _clock = world
    lead_grant = service.grant_by_person(
        ALICE, spec(LEAD_TYPE, GOOGLE, FULL, delegation_depth=1, delegation_mode=DelegationMode.AUTOMATIC)
    )
    boa_grant = service.hand_down(LEAD, spec(BOA_TYPE, GOOGLE, WRITE), task_id="task-1")

    assert boa_grant.status is GrantStatus.ACTIVE
    assert boa_grant.parent_grant_id == lead_grant.grant_id
    assert allowed(service, BOA, ALICE, gog(service, ["gmail", "messages", "search", "x"]))
    assert allowed(service, BOA, ALICE, gog(service, ["gmail", "send", "--to", "a@b.c"]))
    denied = service.decide(agent=BOA, person=ALICE, resolution=gog(service, ["drive", "delete", "id"]), task_id="t")
    assert not denied.allowed
    assert "full access to Google Workspace" in denied.reason

    service.revoke(lead_grant.grant_id, actor=SubjectRefV1(kind=SubjectKind.USER, id="alice"))
    assert not allowed(service, BOA, ALICE, gog(service, ["gmail", "search", "x"]))
    assert service.store.get(boa_grant.grant_id).status is GrantStatus.REVOKED

    # Granting TeamLead again does not revive the old child.
    service.grant_by_person(ALICE, spec(LEAD_TYPE, GOOGLE, FULL, delegation_depth=1))
    assert not allowed(service, BOA, ALICE, gog(service, ["gmail", "search", "x"]))


def test_agents_never_exceed_their_person(world):
    service, _clock = world
    service.grant_by_person(ALICE, spec(BOA_TYPE, PORTAINER, WRITE))
    service.grant_by_person(ALICE, spec(BOA_TYPE, GOOGLE, WRITE))
    restart = portainer(service, "POST", "/api/stacks/3/restart")

    assert allowed(service, BOA, ALICE, restart)
    # Bob is a member without Portainer access: his tasks cannot use it.
    assert not allowed(service, BOA, BOB, restart)
    # A personal login is Bob's own on the person side.
    assert allowed(service, BOA, BOB, gog(service, ["gmail", "search", "x"]))
    # Giving the member role read access lets Bob's tasks read, not change.
    service.grant_by_person(ALICE, spec(SubjectRefV1(kind=SubjectKind.ROLE, id=f"{TEAM}/member"), PORTAINER, READ))
    assert allowed(service, BOA, BOB, portainer(service, "GET", "/api/stacks"))
    assert not allowed(service, BOA, BOB, restart)


def test_members_cannot_give_team_tools_and_nobody_exceeds_their_authority(world):
    service, _clock = world

    # Members authorize agents only for their own work, and only with what they hold.
    with pytest.raises(HandDownRefused, match="Only an administrator"):
        service.grant_by_person(BOB, spec(BOA_TYPE, PORTAINER, READ))
    with pytest.raises(HandDownRefused, match="beyond your own authority"):
        service.grant_by_person(BOB, spec(LEAD_TYPE, PORTAINER, READ, for_person_id="bob"))
    # Workers keep no lasting access for a person's work ("access follows the work").
    with pytest.raises(HandDownRefused, match="Workers keep no lasting access"):
        service.grant_by_person(ALICE, spec(BOA_TYPE, PORTAINER, READ, for_person_id="alice"))
    # With no delegable grant, TeamLead cannot hand anything down.
    with pytest.raises(HandDownRefused, match="holds no"):
        service.hand_down(LEAD, spec(BOA_TYPE, GOOGLE, READ), task_id="t")


@pytest.mark.parametrize(
    ("child_fields", "problem"),
    [
        # Rule 1 already refuses it: TeamLead holds no full access to hand on.
        ({"level": FULL}, "level|holds no full"),
        ({"delegation_depth": 1}, "delegation_depth"),
        ({"lifetime": GrantLifetime.UNTIL_REVOKED}, "lifetime|expiry"),
    ],
)
def test_hand_downs_only_narrow(world, child_fields, problem):
    service, clock = world
    service.grant_by_person(
        ALICE,
        spec(
            LEAD_TYPE,
            GOOGLE,
            WRITE,
            delegation_depth=1,
            lifetime=GrantLifetime.UNTIL,
            expires_at_utc=clock.now + timedelta(days=1),
        ),
    )
    fields = {"lifetime": GrantLifetime.UNTIL, "expires_at_utc": clock.now + timedelta(hours=1), **child_fields}
    level = fields.pop("level", READ)

    with pytest.raises(HandDownRefused, match=problem):
        service.hand_down(LEAD, spec(BOA_TYPE, GOOGLE, level, **fields), task_id="t")


def test_self_one_time_and_recipient_rules(world):
    service, _clock = world
    service.grant_by_person(
        ALICE,
        spec(LEAD_TYPE, GOOGLE, FULL, delegation_depth=1, delegate_to=(SubjectRefV1(kind=SubjectKind.ROLE, id=f"{TEAM}/worker"),)),
    )

    with pytest.raises(HandDownRefused, match="themselves"):
        service.hand_down(LEAD, spec(LEAD_TYPE, GOOGLE, READ), task_id="t")
    other_lead = SubjectRefV1(kind=SubjectKind.AGENT_TYPE, id=f"{TEAM}/TeamArchitect2")
    service.agent_role_of = lambda agent_type: "team_lead" if agent_type.startswith("TeamArchitect") else "worker"
    with pytest.raises(HandDownRefused, match="recipient"):
        service.hand_down(LEAD, spec(other_lead, GOOGLE, READ), task_id="t")
    assert service.hand_down(LEAD, spec(BOA_TYPE, GOOGLE, READ), task_id="t").status is GrantStatus.ACTIVE


def test_ask_owner_hand_downs_wait_for_the_approver(world):
    service, _clock = world
    service.grant_by_person(ALICE, spec(LEAD_TYPE, PORTAINER, WRITE, delegation_depth=1, delegation_mode=DelegationMode.ASK_OWNER))
    child = service.hand_down(LEAD, spec(BOA_TYPE, PORTAINER, WRITE), task_id="t")

    assert child.status is GrantStatus.PENDING
    assert not allowed(service, BOA, ALICE, portainer(service, "POST", "/api/stacks/1/restart"))
    with pytest.raises(HandDownRefused, match="approver"):
        service.approve_pending(BOB, child.grant_id)
    service.approve_pending(ALICE, child.grant_id)
    assert allowed(service, BOA, ALICE, portainer(service, "POST", "/api/stacks/1/restart"))


def test_one_time_task_and_expiring_grants(world):
    service, clock = world
    once = service.grant_by_person(
        ALICE, spec(BOA_TYPE, PORTAINER, WRITE, lifetime=GrantLifetime.ONE_TIME, task_id="task-1", uses_remaining=1)
    )
    restart = portainer(service, "POST", "/api/stacks/1/restart")

    first = service.decide(agent=BOA, person=ALICE, resolution=restart, task_id="task-1")
    assert first.allowed and first.consumed_grant_ids == (once.grant_id,)
    assert not allowed(service, BOA, ALICE, restart, task="task-1")

    service.grant_by_person(ALICE, spec(BOA_TYPE, GOOGLE, READ, lifetime=GrantLifetime.TASK, task_id="task-2"))
    assert allowed(service, BOA, ALICE, gog(service, ["gmail", "search", "x"]), task="task-2")
    assert not allowed(service, BOA, ALICE, gog(service, ["gmail", "search", "x"]), task="task-3")

    service.grant_by_person(
        ALICE, spec(BOA_TYPE, PORTAINER, READ, lifetime=GrantLifetime.UNTIL, expires_at_utc=clock.now + timedelta(minutes=5))
    )
    assert allowed(service, BOA, ALICE, portainer(service, "GET", "/api/stacks"))
    clock.now += timedelta(minutes=6)
    assert not allowed(service, BOA, ALICE, portainer(service, "GET", "/api/stacks"))


def test_ending_a_task_ends_its_grants_and_their_hand_downs_only(world):
    service, _clock = world
    lead_task = service.grant_by_person(
        ALICE, spec(LEAD_TYPE, GOOGLE, WRITE, lifetime=GrantLifetime.TASK, task_id="task-1", delegation_depth=1)
    )
    handed = service.hand_down(
        LEAD, spec(BOA_TYPE, GOOGLE, READ, lifetime=GrantLifetime.TASK, task_id="task-1"), task_id="task-1"
    )
    other_task = service.grant_by_person(ALICE, spec(BOA_TYPE, PORTAINER, READ, lifetime=GrantLifetime.TASK, task_id="task-2"))
    lasting = service.grant_by_person(ALICE, spec(LEAD_TYPE, PORTAINER, WRITE))

    assert sorted(service.end_task_grants(["task-1"])) == sorted([lead_task.grant_id, handed.grant_id])

    assert service.store.get(lead_task.grant_id).status is GrantStatus.REVOKED
    assert service.store.get(handed.grant_id).status is GrantStatus.REVOKED
    assert service.store.get(other_task.grant_id).status is GrantStatus.ACTIVE
    assert service.store.get(lasting.grant_id).status is GrantStatus.ACTIVE
    events = {event.grant_id: event for event in service.store.events(TEAM) if event.kind == "grant_revoked"}
    assert events[lead_task.grant_id].detail == {"ended_with_task": "task-1"}
    assert events[handed.grant_id].detail in ({"ended_with_task": "task-1"}, {"cascade_from": lead_task.grant_id})
    assert service.end_task_grants(["task-1"]) == [], "ending again changes nothing"


def test_broader_access_replaces_narrower_and_keeps_what_was_handed_on(world):
    service, _clock = world
    narrow = service.grant_by_person(ALICE, spec(LEAD_TYPE, GOOGLE, WRITE, delegation_depth=1))
    handed = service.hand_down(LEAD, spec(BOA_TYPE, GOOGLE, READ), task_id="task-1")
    assert handed.parent_grant_id == narrow.grant_id

    broad = service.grant_by_person(ALICE, spec(LEAD_TYPE, GOOGLE, FULL, delegation_depth=1))

    assert service.store.get(narrow.grant_id).status is GrantStatus.REVOKED
    moved = service.store.get(handed.grant_id)
    assert moved.status is GrantStatus.ACTIVE and moved.parent_grant_id == broad.grant_id
    assert allowed(service, BOA, ALICE, gog(service, ["gmail", "search", "x"]))
    kinds = {(event.kind, event.grant_id): event.detail for event in service.store.events(TEAM)}
    assert kinds[("grant_revoked", narrow.grant_id)] == {"replaced_by": broad.grant_id}
    assert kinds[("grant_moved", handed.grant_id)] == {"from_parent": narrow.grant_id, "to_parent": broad.grant_id}
    # Removing the broader grant still ends what was handed on from it.
    service.revoke_by_person(ALICE, broad.grant_id)
    assert service.store.get(handed.grant_id).status is GrantStatus.REVOKED


def test_access_already_covered_adds_nothing(world):
    service, _clock = world
    full = service.grant_by_person(ALICE, spec(LEAD_TYPE, GOOGLE, FULL))

    again = service.grant_by_person(ALICE, spec(LEAD_TYPE, GOOGLE, READ, lifetime=GrantLifetime.TASK, task_id="task-1"))

    assert again.grant_id == full.grant_id
    assert [grant.grant_id for grant in service.store.for_subjects(TEAM, [LEAD_TYPE])] == [full.grant_id]


def test_remove_takes_the_whole_entry_including_a_step_up_for_one_task(world):
    service, _clock = world
    lasting = service.grant_by_person(ALICE, spec(LEAD_TYPE, GOOGLE, WRITE, for_person_id="alice"))
    step_up = service.grant_by_person(
        ALICE, spec(LEAD_TYPE, GOOGLE, FULL, lifetime=GrantLifetime.TASK, task_id="task-1", for_person_id="alice")
    )
    other_tool = service.grant_by_person(ALICE, spec(LEAD_TYPE, PORTAINER, READ, for_person_id="alice"))
    assert {grant.grant_id for grant in service.entry_grants(lasting)} == {lasting.grant_id, step_up.grant_id}
    drive_delete = gog(service, ["drive", "delete", "id"])
    assert allowed(service, LEAD, ALICE, drive_delete, task="task-1"), "the step-up works in its task"
    assert not allowed(service, LEAD, ALICE, drive_delete, task="task-2")

    revoked = service.revoke_by_person(ALICE, lasting.grant_id)

    assert set(revoked) == {lasting.grant_id, step_up.grant_id}
    assert not allowed(service, LEAD, ALICE, gog(service, ["gmail", "search", "x"]), task="task-1")
    assert service.store.get(other_tool.grant_id).status is GrantStatus.ACTIVE, "other tools stay"


def test_a_person_removes_every_part_serving_their_work_whoever_gave_it(world):
    service, _clock = world
    # Bob's own-work grant, and an administrator's grant for Bob's work on the same tool.
    service.grant_by_person(ALICE, spec(SubjectRefV1(kind=SubjectKind.USER, id="bob"), PORTAINER, READ))
    bobs = service.grant_by_person(BOB, spec(LEAD_TYPE, PORTAINER, READ, for_person_id="bob"))
    admins = service.grant_by_person(
        ALICE, spec(LEAD_TYPE, PORTAINER, WRITE, lifetime=GrantLifetime.TASK, task_id="task-1", for_person_id="bob")
    )
    assert admins.grant_id != bobs.grant_id

    # Bob may always remove what serves his own work, so the whole entry goes.
    assert set(service.revoke_by_person(BOB, bobs.grant_id)) == {bobs.grant_id, admins.grant_id}


def test_an_approved_hand_down_joins_the_entry(world):
    service, _clock = world
    service.grant_by_person(
        ALICE, spec(LEAD_TYPE, GOOGLE, FULL, delegation_depth=1, delegation_mode=DelegationMode.ASK_OWNER)
    )
    narrow = service.grant_by_person(ALICE, spec(BOA_TYPE, GOOGLE, READ))
    waiting = service.hand_down(LEAD, spec(BOA_TYPE, GOOGLE, WRITE), task_id="task-1")
    assert waiting.status is GrantStatus.PENDING
    assert service.store.get(narrow.grant_id).status is GrantStatus.ACTIVE, "a waiting hand-down replaces nothing"

    approved = service.approve_pending(ALICE, waiting.grant_id)

    assert approved.status is GrantStatus.ACTIVE
    assert service.store.get(narrow.grant_id).status is GrantStatus.REVOKED
    # A second waiting hand-down that the approved one already covers is not needed.
    covered = service.hand_down(LEAD, spec(BOA_TYPE, GOOGLE, READ), task_id="task-1")
    assert service.approve_pending(ALICE, covered.grant_id).grant_id == waiting.grant_id
    assert service.store.get(covered.grant_id).status is GrantStatus.DENIED


def test_lowering_access_in_one_change_removes_the_old_grant_and_keeps_the_new(world):
    # An approved proposal that removes full access and gives read in the same change.
    service, clock = world
    full = service.grant_by_person(ALICE, spec(BOA_TYPE, PORTAINER, FULL))
    lower = spec(BOA_TYPE, PORTAINER, READ).model_copy(update={"grant_id": "grant_lower", "created_at_utc": clock()})

    without_ending = service.plan_grants([lower])
    assert without_ending.covered["grant_lower"].grant_id == full.grant_id, "full would hide the lower grant"
    plan = service.plan_grants([lower], ending=[full.grant_id])

    assert [grant.grant_id for grant in plan.new] == ["grant_lower"] and plan.replaced == {}
    service.store.apply_together(
        TEAM, grants=plan.new, replaced=plan.replaced, revoke_ids=[full.grant_id],
        actor=SubjectRefV1(kind=SubjectKind.USER, id="alice"), at=clock(),
    )
    assert service.store.get(full.grant_id).status is GrantStatus.REVOKED
    assert service.store.get("grant_lower").status is GrantStatus.ACTIVE
    assert allowed(service, BOA, ALICE, portainer(service, "GET", "/api/stacks"))
    assert not allowed(service, BOA, ALICE, portainer(service, "POST", "/api/stacks/1/restart"))


def test_a_schedules_access_works_only_in_that_schedules_runs(world):
    service, _clock = world
    digest = service.grant_by_person(ALICE, spec(schedule_subject(TEAM, "digest"), GOOGLE, WRITE, for_person_id="alice"))
    service.grant_by_person(ALICE, spec(schedule_subject(TEAM, "triage"), GOOGLE, READ, for_person_id="alice"))
    send = gog(service, ["gmail", "send", "--to", "a@b.c"])
    search = gog(service, ["gmail", "search", "x"])

    def run_of(schedule_id, agent=BOA):
        return replace(agent, schedule_id=schedule_id)

    assert allowed(service, run_of("digest"), ALICE, send)
    assert allowed(service, run_of("triage"), ALICE, search)
    assert not allowed(service, run_of("triage"), ALICE, send), "another schedule of the same worker type cannot send"
    assert not allowed(service, BOA, ALICE, search), "outside scheduled runs neither applies"
    assert allowed(service, run_of("digest", LEAD), ALICE, send), "any agent in the run uses the schedule's access"
    assert [grant.grant_id for grant in service.entry_grants(digest)] == [digest.grant_id]

    # The team lead's lasting access for Alice counts in all its work for her, scheduled runs too;
    # the job's worker still uses only the job's access.
    service.grant_by_person(ALICE, spec(LEAD_TYPE, GOOGLE, FULL, delegation_depth=1, for_person_id="alice"))
    assert allowed(service, LEAD, ALICE, send)
    assert allowed(service, run_of("triage", LEAD), ALICE, send), "the team lead keeps its access in a job's run"
    assert not allowed(service, run_of("triage"), ALICE, send), "the job's worker works for the schedule"
    assert service.hand_on_sources(run_of("triage"), "alice") == []
    assert service.hand_on_sources(BOA, "alice"), "a delegated task can draw on the team lead's access"


def test_workers_use_their_tasks_access_and_old_lasting_grants_stop_counting(world):
    service, _clock = world
    service.grant_by_person(ALICE, spec(LEAD_TYPE, GOOGLE, WRITE, delegation_depth=1, for_person_id="alice"))
    search = gog(service, ["gmail", "search", "x"])

    # The team lead hands a worker access for its task only; lasting access for Alice's work is refused.
    with pytest.raises(HandDownRefused, match="Workers keep no lasting access"):
        service.hand_down(LEAD, spec(BOA_TYPE, GOOGLE, READ), task_id="task-1", person_id="alice")
    service.hand_down(
        LEAD, spec(BOA_TYPE, GOOGLE, READ, lifetime=GrantLifetime.TASK, task_id="boa-task"), task_id="task-1", person_id="alice"
    )
    assert allowed(service, BOA, ALICE, search, task="boa-task")
    assert not allowed(service, BOA, ALICE, search, task="boa-other-task")

    # A worker grant from before this rule is kept in the store but never counts (no migration).
    old = spec(BOA_TYPE, PORTAINER, READ, for_person_id="alice").model_copy(
        update={"grant_id": "grant_old_worker", "granted_by": SubjectRefV1(kind=SubjectKind.USER, id="alice")}
    )
    service.store.create(old)
    assert not allowed(service, BOA, ALICE, portainer(service, "GET", "/api/stacks"))


@pytest.mark.parametrize("condition", ["eligible", "ask_owner", "no_depth", "other_person", "expired", "scheduled", "no_task", "worker_block", "person_block", "lead_block", "person_lacks_access"])
def test_hand_on_eligibility_uses_real_policy_without_writing_grants(world, condition):
    service, clock = world
    person = BOB if condition == "person_lacks_access" else ALICE
    fields = {
        "for_person_id": "bob" if condition in {"other_person", "person_lacks_access"} else "alice",
        "delegation_depth": 0 if condition == "no_depth" else 1,
        "delegation_mode": DelegationMode.ASK_OWNER if condition == "ask_owner" else DelegationMode.AUTOMATIC,
    }
    if condition == "expired":
        fields.update(lifetime=GrantLifetime.UNTIL, expires_at_utc=clock() + timedelta(seconds=1))
    parent = service.grant_by_person(ALICE, spec(LEAD_TYPE, PORTAINER, WRITE, **fields))
    if condition == "expired":
        clock.now += timedelta(seconds=2)
    blocked = {"worker_block": BOA_TYPE, "person_block": ALICE.subjects()[0], "lead_block": LEAD_TYPE}
    if condition in blocked:
        service.grant_by_person(ALICE, spec(blocked[condition], PORTAINER, FULL, kind=GrantKind.BLOCK))
    worker = replace(BOA, schedule_id="digest") if condition == "scheduled" else BOA
    task_id = None if condition == "no_task" else "task-1"
    before, events = service.store.version(TEAM), service.store.events(TEAM)
    worker_grants = service.store.for_subjects(TEAM, [BOA_TYPE])

    assert service.may_hand_on_for_task(worker, person, PORTAINER, READ, task_id) is (condition == "eligible")
    assert not service.may_hand_on_for_task(worker, person, PORTAINER, FULL, task_id), "cannot exceed the parent's level"
    assert service.store.version(TEAM) == before and service.store.events(TEAM) == events
    assert service.store.for_subjects(TEAM, [BOA_TYPE]) == worker_grants
    if condition == "eligible":
        grant = service.hand_on_for_task(worker, person, PORTAINER, READ, task_id)
        assert grant.parent_grant_id == parent.grant_id and grant.for_person_id == "alice"
        assert allowed(service, worker, person, portainer(service, "GET", "/api/stacks"))


def test_blocks_win_over_grants(world):
    service, _clock = world
    service.grant_by_person(ALICE, spec(BOA_TYPE, PORTAINER, FULL))
    service.grant_by_person(ALICE, spec(BOA_TYPE, PORTAINER, FULL, kind=GrantKind.BLOCK))

    decision = service.decide(agent=BOA, person=ALICE, resolution=portainer(service, "GET", "/api/x"), task_id="t")

    assert not decision.allowed
    assert any(item.startswith("block:") for item in decision.blocking_policy_ids)


def test_agents_never_hold_full_on_platform_tools(world):
    service, _clock = world
    service.grant_by_person(ALICE, spec(LEAD_TYPE, TargetRefV1(kind=TargetKind.TOOL, id="fn:runtime_settings"), FULL))

    stage = service.catalog.resolve_function("plan_runtime_setting_change")
    apply = OperationResolutionV1(
        tool_id="fn:runtime_settings", operation="change:runtime_settings", level=FULL, source="trusted"
    )

    assert allowed(service, LEAD, ALICE, stage)
    decision = service.decide(agent=LEAD, person=ALICE, resolution=apply, task_id="t")
    assert not decision.allowed
    assert "guard:agents-no-platform-full" in decision.blocking_policy_ids


def test_tool_set_grants_cover_their_tools(world):
    service, _clock = world
    service.grant_by_person(ALICE, spec(BOA_TYPE, TargetRefV1(kind=TargetKind.TOOL_SET, id="standard"), WRITE))

    assert allowed(service, BOA, ALICE, service.catalog.resolve_function("file_write"))
    assert not allowed(service, BOA, ALICE, service.catalog.resolve_function("remove_scheduled_job"))
    assert not allowed(service, BOA, ALICE, service.catalog.resolve_function("shell"))


def test_unknown_tools_missing_people_and_evaluation_failures_deny(world, monkeypatch):
    service, _clock = world
    service.grant_by_person(ALICE, spec(BOA_TYPE, GOOGLE, FULL))

    unlisted = service.catalog.resolve_http("GET", "https://elsewhere.example/x")
    assert not allowed(service, BOA, ALICE, unlisted)
    assert not service.decide(agent=BOA, person=None, resolution=gog(service, ["gmail", "search", "x"]), task_id="t").allowed

    def broken(**_kwargs):
        from access_control.cedar import AccessEvaluationError

        raise AccessEvaluationError("worker crashed")

    monkeypatch.setattr(service.cedar, "decide", broken)
    assert not allowed(service, BOA, ALICE, gog(service, ["gmail", "search", "x"]))


def test_every_change_bumps_the_version_and_is_logged(world):
    service, _clock = world
    grant = service.grant_by_person(ALICE, spec(BOA_TYPE, GOOGLE, READ))
    before = service.store.version(TEAM)
    service.revoke(grant.grant_id, actor=SubjectRefV1(kind=SubjectKind.USER, id="alice"))

    assert service.store.version(TEAM) == before + 1
    kinds = [event.kind for event in service.store.events(TEAM, grant_id=grant.grant_id)]
    assert kinds == ["grant_created", "grant_revoked"]


def test_standard_grants_cover_each_roles_own_tools(world):
    from access_control.entities import agent_role_resolver

    service, _clock = world
    service.agent_role_of = agent_role_resolver("TeamArchitect")
    memory = AgentPrincipal("agent_mem", "MemReviewAgent", "system", TEAM)
    send = service.catalog.resolve_function("send_message")
    instantiate = service.catalog.resolve_function("instantiate_agent")
    review = service.catalog.resolve_function("list_pending_memory_review_jobs")

    # Every agent does its own work without grants, for any team member.
    assert allowed(service, BOA, BOB, send)
    # Managing workers is the team lead's, and a worker cannot.
    assert allowed(service, LEAD, BOB, instantiate)
    assert not allowed(service, BOA, ALICE, instantiate)
    # System agents do their duties, even for work with no person.
    assert service.decide(agent=memory, person=None, resolution=review, task_id="t").allowed
    assert not service.decide(agent=BOA, person=None, resolution=send, task_id="t").allowed
    # Standard grants are never delegable.
    with pytest.raises(HandDownRefused):
        service.hand_down(LEAD, spec(BOA_TYPE, TargetRefV1(kind=TargetKind.TOOL_SET, id="team_lead_runtime"), FULL), task_id="t")


@pytest.mark.parametrize("function", ["publish_contribution", "reject_contribution", "revert_knowledge_page"])
def test_curation_standard_access_belongs_only_to_the_curator_and_people(world, function):
    from access_control.entities import agent_role_resolver
    from custom_messages.contracts.runtime_v1 import KNOWLEDGE_CURATOR_AGENT_TYPE

    service, _clock = world
    service.agent_role_of = agent_role_resolver("TeamArchitect")
    operation = service.catalog.resolve_function(function)
    curator = AgentPrincipal("curator", KNOWLEDGE_CURATOR_AGENT_TYPE, "system", TEAM)
    assert allowed(service, curator, BOB, operation)
    assert allowed(service, None, BOB, operation)
    assert allowed(service, curator, None, operation)
    for agent_type in ("MemReviewAgent", "InternalImprovementAgent"):
        other = AgentPrincipal("other", agent_type, "system", TEAM)
        assert not allowed(service, other, BOB, operation)
        assert not allowed(service, other, None, operation)
    assert not allowed(service, LEAD, BOB, operation)
    assert not allowed(service, BOA, BOB, operation)
