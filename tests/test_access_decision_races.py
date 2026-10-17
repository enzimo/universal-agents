"""Deciding an access request once, and keeping grant records consistent, with real Cedar and SQLite.

Each test reproduces a concurrency defect found in the 2026-10-17 audit, or
in its review, on the real code: two processes (the gateway and the manager,
or two gateway threads) share one access store file. A person's decision now
stores its grants and its outcome in one transaction, so:

- a decision that fails, or cannot write at all, stores nothing, and a later
  Deny leaves no access (a failed approval used to leave its grants);
- a second decision, a withdrawal or another writer waits for the decision
  and never sees or builds on a grant before its outcome is stored (the loser
  of two approvals used to revoke the winner's grants, and a person's own
  grant or a worker's hand-down made in between was lost with the decision);
- access in force before the decision is replaced only together with the
  outcome, and a closed request denies its waiting hand-downs.

Also: "one entry per tool" is planned inside the write transaction (two
grants stored at once both landed, or a tool grant beside its tool set), two
account links stored at once leave one active, a grant carrying the approval
id of a request that was not granted (only an older version left such
grants) is ended, and a change applied at the decision renews its claim.
"""

from __future__ import annotations

import logging
import sqlite3
import threading
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from access_catalog.catalog import load_catalog
from access_control.cedar import CedarAccessClient
from access_control.decisions import AccessService, HandDownRefused
from access_control.entities import agent_role_resolver
from access_control.requests import DECISION_CLAIM_SECONDS, AccessRequests
from access_control.entities import AgentPrincipal
from access_control.starter_profiles import apply_starter_profile
from custom_messages.contracts.access_v2 import (
    AccessLevel,
    AccessRequestStatus,
    AccessReviewChoice,
    AccountLinkStatus,
    AccountLinkV1,
    DelegationMode,
    GrantKind,
    GrantLifetime,
    GrantStatus,
    GrantV1,
    LinkOwnerKind,
    SubjectKind,
    SubjectRefV1,
    TargetKind,
    TargetRefV1,
)
from storage.access_repository import (
    ONE_ACTIVE_LINK_INDEX,
    AccessRepository,
    DecisionClaim,
    DecisionClaimLost,
    DecisionInProgress,
)
from storage.config import resolve_storage_settings
from tests.test_access_requests import (
    BINARY,
    LEAD,
    OWNER,
    SCHEDULES,
    TEAM,
    WORKER,
    X_POSTS,
    RecordingInbox,
    ask,
    uses,
)

pytestmark = pytest.mark.skipif(not BINARY.is_file(), reason="build ua-cedar-authorizer first (cargo build)")

LEAD_SUBJECT = SubjectRefV1(kind=SubjectKind.AGENT_TYPE, id=f"{TEAM}/{LEAD}")
STANDARD_SET = TargetRefV1(kind=TargetKind.TOOL_SET, id="standard")
CREATE_JOB = "fn:schedules#create_scheduled_job"
READ_JOBS = "fn:schedules#prepare_scheduled_job_access"


class Clock:
    """A clock the test moves forward (claims run out after ``DECISION_CLAIM_SECONDS``)."""

    def __init__(self) -> None:
        self.now = datetime.now(timezone.utc)

    def __call__(self) -> datetime:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += timedelta(seconds=seconds)


@dataclass
class World:
    gateway: AccessRequests
    manager: AccessRequests
    clock: Clock
    db: Path
    request_id: str = ""

    @property
    def store(self) -> AccessRepository:
        return self.gateway.service.store

    def approval_grants(self, request_id: str) -> list[GrantV1]:
        """Grants in force or waiting that carry the request's approval id."""

        return [
            grant
            for grant in self.store.for_status(TEAM, (GrantStatus.ACTIVE, GrantStatus.PENDING))
            if grant.approval_id == request_id
        ]


@pytest.fixture
def world(tmp_path: Path):
    db = tmp_path / "manager.db"
    storage = resolve_storage_settings({"backend": "sqlite", "sqlite": {"path": str(db)}})
    clock = Clock()

    def side() -> AccessRequests:
        service = AccessService(
            store=AccessRepository(storage),
            catalog=load_catalog(None),
            cedar=CedarAccessClient(binary=str(BINARY)),
            team_id=TEAM,
            agent_role_of=agent_role_resolver(LEAD),
            clock=clock,
        )
        return AccessRequests(service=service, inbox=RecordingInbox(), lead_agent_type=LEAD)

    gateway, manager = side(), side()
    yield World(gateway, manager, clock, db)
    gateway.service.cedar.close()
    manager.service.cedar.close()


def careful_request(world: World) -> str:
    apply_starter_profile(world.gateway.service, OWNER, "careful", lead_agent_type=LEAD)
    outcome = ask(world.gateway)
    assert outcome.waiting
    return outcome.request.request_id


# -- deciding once ------------------------------------------------------------------------


def test_a_decision_that_cannot_write_stores_nothing_and_a_later_deny_leaves_no_access(world) -> None:
    request_id = careful_request(world)
    other = sqlite3.connect(str(world.db), isolation_level=None)
    other.execute("BEGIN IMMEDIATE")  # another process holds the write lock past the 2.5 s busy timeout
    try:
        with pytest.raises(sqlite3.OperationalError, match="locked"):
            world.gateway.decide(request_id, OWNER, AccessReviewChoice.APPROVE)
    finally:
        other.execute("ROLLBACK")
        other.close()

    assert world.store.get_request(request_id).status is AccessRequestStatus.WAITING
    assert world.approval_grants(request_id) == []
    denied = world.manager.decide(request_id, OWNER, AccessReviewChoice.DENY)
    assert denied.status is AccessRequestStatus.DENIED
    assert not uses(world.gateway, WORKER, OWNER, CREATE_JOB), "a denied request leaves no access"


def test_a_failure_while_storing_the_outcome_stores_none_of_the_decisions_grants(world, monkeypatch) -> None:
    request_id = careful_request(world)
    [standard] = [grant for grant in world.store.for_subjects(TEAM, [LEAD_SUBJECT]) if grant.target == STANDARD_SET]

    def locked(*_args, **_kwargs):
        raise sqlite3.OperationalError("database is locked")

    def no_undo(*_args, **_kwargs):
        raise AssertionError("nothing needs taking back: the decision's grants were never stored")

    monkeypatch.setattr(world.store, "finish_request", locked)
    monkeypatch.setattr(world.gateway.service, "revoke", no_undo)
    with pytest.raises(sqlite3.OperationalError):
        world.gateway.decide(request_id, OWNER, AccessReviewChoice.APPROVE_READ)
    monkeypatch.undo()

    assert world.approval_grants(request_id) == []
    assert world.store.get(standard.grant_id).status is GrantStatus.ACTIVE, "access in force before stays"
    assert world.store.get_request(request_id).status is AccessRequestStatus.WAITING
    assert not uses(world.gateway, WORKER, OWNER, CREATE_JOB)


def _while_deciding(world: World, monkeypatch, other, *, fail: bool = False) -> tuple[object, object]:
    """Decide (approve) with ``other`` running in a second thread while the decision's transaction is open.

    The decision pauses after storing its grants and before its outcome and
    gives the other thread up to a second to finish: it reads, then waits for
    the write lock the decision holds (code that stored grants before the
    outcome let it finish against the undecided grants). With ``fail`` the
    decision then fails, so it stores nothing. Returns (the decision's result
    or error, the other thread's result or error).
    """

    uses(world.manager, WORKER, OWNER, CREATE_JOB)  # start the manager's evaluator before the window
    store = world.store
    original = store.finish_request
    outcomes: dict[str, object] = {}
    started = threading.Event()

    def run_other() -> None:
        started.set()
        try:
            outcomes["other"] = other()
        except BaseException as exc:  # noqa: BLE001 - returned to the test
            outcomes["other"] = exc

    thread = threading.Thread(target=run_other)

    def finish_after_other_started(updated, **kwargs):
        thread.start()
        assert started.wait(5)
        thread.join(1.0)  # well under the other side's 2.5 s busy timeout
        if fail:
            raise sqlite3.OperationalError("database is locked")
        return original(updated, **kwargs)

    monkeypatch.setattr(store, "finish_request", finish_after_other_started)
    try:
        outcomes["decision"] = world.gateway.decide(world.request_id, OWNER, AccessReviewChoice.APPROVE)
    except BaseException as exc:  # noqa: BLE001 - returned to the test
        outcomes["decision"] = exc
    thread.join(10)
    monkeypatch.undo()
    return outcomes["decision"], outcomes["other"]


def test_a_second_approval_waits_for_the_first_and_finds_it_decided(world, monkeypatch) -> None:
    world.request_id = careful_request(world)

    first, second = _while_deciding(
        world, monkeypatch, lambda: world.manager.decide(world.request_id, OWNER, AccessReviewChoice.APPROVE)
    )

    assert first.status is AccessRequestStatus.GRANTED
    assert isinstance(second, HandDownRefused) and "already granted" in str(second)
    assert {world.store.get(grant_id).status for grant_id in first.grant_ids} == {GrantStatus.ACTIVE}
    assert uses(world.gateway, WORKER, OWNER, CREATE_JOB)


def test_a_withdrawal_during_a_decision_waits_and_keeps_the_access_in_force(world, monkeypatch) -> None:
    world.request_id = careful_request(world)
    [standard] = [grant for grant in world.store.for_subjects(TEAM, [LEAD_SUBJECT]) if grant.target == STANDARD_SET]

    decided, withdrawal = _while_deciding(
        world, monkeypatch, lambda: world.manager.withdraw(world.request_id, why="The task ended first.")
    )

    assert decided.status is AccessRequestStatus.GRANTED
    assert isinstance(withdrawal, HandDownRefused) and "already decided" in str(withdrawal)
    assert world.store.get(standard.grant_id).status is GrantStatus.ACTIVE


def test_a_persons_grant_made_during_a_failing_decision_is_kept(world, monkeypatch) -> None:
    # Before 2026-10-17 (review): the person's grant was answered "already
    # covered" by the decision's undecided grant, so nothing was stored, and
    # the decision then failed and took its grant back.
    read_grant = _lead_reads_x_and_worker_holds_it(world)
    world.request_id = ask(world.gateway, target=X_POSTS).request.request_id

    def direct_grant() -> GrantV1:
        return world.manager.service.grant_by_person(
            OWNER,
            GrantV1(
                grant_id="new",
                team_id=TEAM,
                subject=LEAD_SUBJECT,
                target=X_POSTS,
                level=AccessLevel.WRITE,
                delegation_depth=1,
                for_person_id=OWNER.person_id,
                granted_by=SubjectRefV1(kind=SubjectKind.USER, id=OWNER.person_id),
                created_at_utc=world.clock(),
            ),
        )

    failed, direct = _while_deciding(world, monkeypatch, direct_grant, fail=True)

    assert isinstance(failed, sqlite3.OperationalError)
    assert isinstance(direct, GrantV1) and direct.approval_id is None, "stored, not covered by the undecided grant"
    assert world.store.get(direct.grant_id).status is GrantStatus.ACTIVE
    assert world.store.get(read_grant.grant_id).status is GrantStatus.REVOKED, "the person's grant replaced it"
    assert world.approval_grants(world.request_id) == []
    assert uses(world.gateway, WORKER, OWNER, "cli:x#auth.status"), "the worker's hand-down moved to it"


def test_a_workers_request_during_a_failing_decision_is_not_handed_down_from_the_undecided_grant(
    world, monkeypatch
) -> None:
    # Before 2026-10-17 (review): trusted code handed the request down at once
    # from the decision's undecided grant, recorded it as granted, and the
    # decision then failed and cut the hand-down off.
    _lead_reads_x_and_worker_holds_it(world)
    world.request_id = ask(world.gateway, target=X_POSTS).request.request_id
    other_worker = AgentPrincipal("agent-web-2", "WebResearchAgent", "worker", TEAM, work_id="task-2")

    failed, outcome = _while_deciding(
        world,
        monkeypatch,
        lambda: world.manager.submit(
            requester=other_worker,
            person=OWNER,
            target=X_POSTS,
            level=AccessLevel.WRITE,
            reason="Post the update",
            task_id="task-2",
        ),
        fail=True,
    )

    assert isinstance(failed, sqlite3.OperationalError)
    assert outcome.waiting, "the team lead holds read only, so a person decides"
    assert outcome.grant is None
    assert not uses(world.gateway, other_worker, OWNER, "cli:x#post.create", task_id="task-2")


def test_grants_an_older_version_left_without_a_granted_outcome_are_ended(world, caplog) -> None:
    request_id = careful_request(world)
    request = world.store.get_request(request_id)
    [lead_spec, *_rest] = world.gateway.approval_specs(
        request,
        level=AccessLevel.WRITE,
        lifetime=GrantLifetime.UNTIL_REVOKED,
        expires_at=None,
        granted_by=SubjectRefV1(kind=SubjectKind.USER, id=OWNER.person_id),
    )
    # An older version stored a decision's grants before its outcome and stopped in between.
    left = world.gateway.service.grant_by_person(OWNER, lead_spec.model_copy(update={"approval_id": request_id}))
    withdrawn = careful_withdrawn_request_with_grant(world)

    assert {grant.grant_id for grant, _request in world.store.grants_without_granted_outcome(TEAM)} == {
        left.grant_id,
        withdrawn.grant_id,
    }
    with caplog.at_level(logging.WARNING, logger="storage.access_repository"):
        ended = world.store.end_grants_without_granted_outcome(TEAM, world.clock())

    assert set(ended) >= {left.grant_id, withdrawn.grant_id}
    assert "event=grant_without_granted_outcome_ended" in caplog.text
    assert world.store.grants_without_granted_outcome(TEAM) == []
    assert world.store.end_grants_without_granted_outcome(TEAM, world.clock()) == [], "nothing the next time"
    granted = world.gateway.decide(request_id, OWNER, AccessReviewChoice.APPROVE)
    assert world.store.grants_without_granted_outcome(TEAM) == [], "a granted request's grants are in place"
    assert granted.status is AccessRequestStatus.GRANTED


def careful_withdrawn_request_with_grant(world: World) -> GrantV1:
    """A withdrawn request that an older version still stored a grant for."""

    outcome = ask(world.gateway, target=X_POSTS)
    request = world.store.get_request(outcome.request.request_id)
    world.manager.withdraw(request.request_id, why="The task ended first.")
    [lead_spec, *_rest] = world.gateway.approval_specs(
        request,
        level=AccessLevel.WRITE,
        lifetime=GrantLifetime.UNTIL_REVOKED,
        expires_at=None,
        granted_by=SubjectRefV1(kind=SubjectKind.USER, id=OWNER.person_id),
    )
    return world.gateway.service.grant_by_person(OWNER, lead_spec.model_copy(update={"approval_id": request.request_id}))


# -- one entry per tool, planned inside the write transaction -------------------------


def _store_after_both_planned(world: World, monkeypatch, *calls) -> list[object]:
    """Run ``calls`` at once in two threads, both past their checks before either writes."""

    barrier = threading.Barrier(len(calls))
    for side in (world.gateway, world.manager):
        original = side.service.store.store_planned

        def store_planned(grant, plan, _original=original, **kwargs):
            barrier.wait(10)
            return _original(grant, plan, **kwargs)

        monkeypatch.setattr(side.service.store, "store_planned", store_planned)
    results: list[object] = [None] * len(calls)
    errors: list[BaseException] = []

    def run(index: int, call) -> None:
        try:
            results[index] = call()
        except BaseException as exc:  # noqa: BLE001 - reported below
            errors.append(exc)

    threads = [threading.Thread(target=run, args=(index, call)) for index, call in enumerate(calls)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert not errors, errors
    return results


def test_two_first_calls_at_once_store_one_task_grant(world, monkeypatch) -> None:
    apply_starter_profile(world.gateway.service, OWNER, "trusted", lead_agent_type=LEAD)

    def first_call(side: AccessRequests):
        return lambda: side.service.hand_on_for_task(WORKER, OWNER, SCHEDULES, AccessLevel.WRITE, "task-1")

    first, second = _store_after_both_planned(world, monkeypatch, first_call(world.gateway), first_call(world.manager))

    held = [
        grant
        for grant in world.store.for_subjects(TEAM, [WORKER.type_subject()])
        if grant.target == SCHEDULES and grant.kind is GrantKind.ACCESS
    ]
    assert len(held) == 1, held
    assert first.grant_id == second.grant_id == held[0].grant_id, "the second call got the first one's grant"


def test_a_tool_set_grant_and_a_tool_grant_stored_at_once_make_one_entry(world, monkeypatch) -> None:
    def lead_grant(target: TargetRefV1) -> GrantV1:
        return GrantV1(
            grant_id="new",
            team_id=TEAM,
            subject=LEAD_SUBJECT,
            target=target,
            level=AccessLevel.READ,
            lifetime=GrantLifetime.UNTIL_REVOKED,
            granted_by=SubjectRefV1(kind=SubjectKind.USER, id=OWNER.person_id),
            created_at_utc=world.clock(),
            for_person_id=OWNER.person_id,
        )

    _store_after_both_planned(
        world,
        monkeypatch,
        lambda: world.gateway.service.grant_by_person(OWNER, lead_grant(SCHEDULES)),
        lambda: world.manager.service.grant_by_person(OWNER, lead_grant(STANDARD_SET)),
    )

    in_force = [
        grant.target
        for grant in world.store.for_subjects(TEAM, [LEAD_SUBJECT])
        if grant.target in (SCHEDULES, STANDARD_SET)
    ]
    assert in_force == [STANDARD_SET], "the tool grant was replaced by, or never stored beside, its tool set"


# -- account links ---------------------------------------------------------------------


def _link(link_id: str, key: str, at: datetime) -> AccountLinkV1:
    return AccountLinkV1(
        link_id=link_id,
        team_id=TEAM,
        tool_id="cli:x",
        owner_kind=LinkOwnerKind.PERSON,
        owner_id=OWNER.person_id,
        credential_key=key,
        created_by=SubjectRefV1(kind=SubjectKind.USER, id=OWNER.person_id),
        created_at_utc=at,
    )


def _active_x_links(store: AccessRepository) -> list[str]:
    return [link.link_id for link in store.list_links(TEAM) if link.tool_id == "cli:x"]


def test_two_links_stored_at_once_leave_one_active_link(world, monkeypatch) -> None:
    barrier = threading.Barrier(2)
    stores = [world.gateway.service.store, world.manager.service.store]
    for store in stores:
        original = store.create_link

        def create_link(link, _original=original):
            barrier.wait(10)
            return _original(link)

        monkeypatch.setattr(store, "create_link", create_link)
    now = world.clock()
    threads = [
        threading.Thread(target=stores[0].create_link, args=(_link("link_a", "x_account_a", now),)),
        threading.Thread(target=stores[1].create_link, args=(_link("link_b", "x_account_b", now),)),
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    [active] = _active_x_links(world.store)
    replaced = {"link_a", "link_b"} - {active}
    [older] = [link for link in world.store.list_links(TEAM, include_revoked=True) if link.link_id in replaced]
    assert older.status is AccountLinkStatus.REVOKED
    world.store.revoke_link(active, actor=SubjectRefV1(kind=SubjectKind.USER, id=OWNER.person_id), at=now)
    assert world.store.active_link(TEAM, "cli:x", LinkOwnerKind.PERSON, OWNER.person_id) is None, (
        "unlinking leaves no older credential in use"
    )


def test_the_store_refuses_a_second_active_link_for_one_tool_and_owner(world) -> None:
    world.store.create_link(_link("link_a", "x_account_a", world.clock()))
    row = sqlite3.connect(str(world.db)).execute("SELECT * FROM access_account_links").fetchone()
    with sqlite3.connect(str(world.db)) as conn, pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "INSERT INTO access_account_links VALUES (?,?,?,?,?,?,?,?,?,?)", ("link_b", *row[1:])
        )


def test_an_older_store_with_two_active_links_keeps_the_newest(tmp_path: Path, caplog) -> None:
    db = tmp_path / "old.db"
    settings = resolve_storage_settings({"backend": "sqlite", "sqlite": {"path": str(db)}})
    store = AccessRepository(settings)
    now = datetime.now(timezone.utc)
    store.create_link(_link("link_old", "x_account_old", now - timedelta(minutes=5)))
    # A store written before the index existed: two active links for one tool and owner.
    with sqlite3.connect(str(db)) as conn:
        conn.execute(f"DROP INDEX {ONE_ACTIVE_LINK_INDEX}")
        row = conn.execute("SELECT * FROM access_account_links").fetchone()
        newer = _link("link_new", "x_account_new", now)
        conn.execute(
            "INSERT INTO access_account_links VALUES (?,?,?,?,?,?,?,?,?,?)",
            ("link_new", *row[1:8], newer.model_dump_json(), now.isoformat()),
        )

    with caplog.at_level(logging.WARNING, logger="storage.access_repository"):
        reopened = AccessRepository(settings)

    assert _active_x_links(reopened) == ["link_new"], "the newest, the one in use, stays"
    assert "event=account_link_duplicate_revoked" in caplog.text and "revoked_link_id=link_old" in caplog.text
    [event] = [event for event in reopened.events(TEAM) if event.kind == "account_link_revoked"]
    assert event.detail["replaced_by"] == "link_new" and event.detail["reason"] == "duplicate_active_link"
    caplog.clear()
    with caplog.at_level(logging.WARNING, logger="storage.access_repository"):
        AccessRepository(settings)
    assert "account_link_duplicate_revoked" not in caplog.text, "later opens change nothing"


# -- waiting hand-downs end with their request -------------------------------------------


def _lead_asks_each_time(world: World) -> None:
    """The team lead holds read-and-change on schedules that it may hand on only after asking the owner."""

    world.gateway.service.grant_by_person(
        OWNER,
        GrantV1(
            grant_id="new",
            team_id=TEAM,
            subject=LEAD_SUBJECT,
            target=SCHEDULES,
            level=AccessLevel.WRITE,
            delegation_depth=1,
            delegation_mode=DelegationMode.ASK_OWNER,
            granted_by=SubjectRefV1(kind=SubjectKind.USER, id=OWNER.person_id),
            created_at_utc=world.clock(),
        ),
    )


def _close(world: World, request_id: str, how: str) -> None:
    if how == "deny":
        assert world.gateway.decide(request_id, OWNER, AccessReviewChoice.DENY).status is AccessRequestStatus.DENIED
    else:
        assert world.manager.withdraw(request_id, why="The task ended first.").status is AccessRequestStatus.WITHDRAWN


@pytest.mark.parametrize("how", ["deny", "withdraw"])
def test_a_closed_request_denies_its_waiting_hand_down(world, how) -> None:
    _lead_asks_each_time(world)
    outcome = ask(world.gateway)
    [pending_id] = outcome.request.grant_ids
    assert world.store.get(pending_id).status is GrantStatus.PENDING

    _close(world, outcome.request.request_id, how)

    assert world.store.get(pending_id).status is GrantStatus.DENIED, "a closed request leaves nothing waiting"


@pytest.mark.parametrize("how", ["deny", "withdraw"])
def test_a_hand_down_an_agent_gave_that_waits_is_denied_with_its_request(world, how) -> None:
    _lead_asks_each_time(world)
    lead = AgentPrincipal("agent-lead", LEAD, "team_lead", TEAM, work_id="task-1")
    given = world.gateway.give(
        lead,
        recipient=WORKER.type_subject(),
        target=SCHEDULES,
        level=AccessLevel.WRITE,
        reason="Track the report",
        lifetime=GrantLifetime.TASK,
        task_id="task-1",
        person=OWNER,
    )
    assert given.waiting and given.grant.status is GrantStatus.PENDING

    _close(world, given.request.request_id, how)

    assert world.store.get(given.grant.grant_id).status is GrantStatus.DENIED


# -- access in force before a decision ends only with its outcome -----------------------


def _lead_reads_x_and_worker_holds_it(world: World) -> GrantV1:
    """An earlier approval: the team lead keeps read on X and hands it to the worker's task."""

    apply_starter_profile(world.gateway.service, OWNER, "careful", lead_agent_type=LEAD)
    first = ask(world.gateway, target=X_POSTS)
    world.gateway.decide(first.request.request_id, OWNER, AccessReviewChoice.APPROVE_READ)
    [read_grant] = [g for g in world.store.for_subjects(TEAM, [LEAD_SUBJECT]) if g.target == X_POSTS]
    assert read_grant.level is AccessLevel.READ and uses(world.gateway, WORKER, OWNER, "cli:x#auth.status")
    return read_grant


def test_a_failed_approval_keeps_narrower_access_it_would_have_replaced(world, monkeypatch) -> None:
    read_grant = _lead_reads_x_and_worker_holds_it(world)
    second = ask(world.gateway, target=X_POSTS)

    def locked(*_args, **_kwargs):
        raise sqlite3.OperationalError("database is locked")

    monkeypatch.setattr(world.store, "finish_request", locked)
    with pytest.raises(sqlite3.OperationalError):
        world.gateway.decide(second.request.request_id, OWNER, AccessReviewChoice.APPROVE)
    monkeypatch.undo()

    assert world.store.get(read_grant.grant_id).status is GrantStatus.ACTIVE
    assert uses(world.gateway, WORKER, OWNER, "cli:x#auth.status")
    assert world.approval_grants(second.request.request_id) == []
    assert world.store.get_request(second.request.request_id).status is AccessRequestStatus.WAITING


def test_an_approval_replaces_narrower_access_when_its_outcome_is_stored(world) -> None:
    read_grant = _lead_reads_x_and_worker_holds_it(world)
    second = ask(world.gateway, target=X_POSTS)

    decided = world.gateway.decide(second.request.request_id, OWNER, AccessReviewChoice.APPROVE)

    replaced = world.store.get(read_grant.grant_id)
    assert replaced.status is GrantStatus.REVOKED, "one entry per tool: the broader grant replaced it"
    [lead_write] = [g for g in world.store.for_subjects(TEAM, [LEAD_SUBJECT]) if g.target == X_POSTS]
    assert lead_write.level is AccessLevel.WRITE and lead_write.grant_id in decided.grant_ids
    [event] = [e for e in world.store.events(TEAM, grant_id=read_grant.grant_id) if e.kind == "grant_revoked"]
    assert event.detail == {"replaced_by": lead_write.grant_id}
    assert uses(world.gateway, WORKER, OWNER, "cli:x#post.create")


# -- claims: changes applied at the decision ---------------------------------------------


def test_a_renewed_claim_keeps_a_live_decision_from_being_taken_over(world) -> None:
    request_id = careful_request(world)
    request, claim = world.gateway._claim(request_id)
    world.clock.advance(DECISION_CLAIM_SECONDS - 60)
    world.gateway.renew_claim(claim)  # before a long step, such as a download
    world.clock.advance(DECISION_CLAIM_SECONDS - 60)

    with pytest.raises(HandDownRefused, match="deciding this request right now"):
        world.manager.decide(request_id, OWNER, AccessReviewChoice.DENY)

    world.clock.advance(120)  # the renewed claim ran out too: now it may be taken over
    assert world.manager.decide(request_id, OWNER, AccessReviewChoice.DENY).status is AccessRequestStatus.DENIED
    with pytest.raises(DecisionClaimLost, match="already denied by another decision"):
        world.gateway.renew_claim(claim)


def test_a_change_whose_claim_was_taken_over_stores_no_outcome(world) -> None:
    request_id = careful_request(world)
    now = world.clock()
    first = DecisionClaim(request_id, "decision_first")
    world.store.claim_decision(request_id, claim_id=first.claim_id, at=now, until=now + timedelta(seconds=1))
    world.clock.advance(2)
    world.store.claim_decision(
        request_id, claim_id="decision_second", at=world.clock(), until=world.clock() + timedelta(minutes=10)
    )
    request = world.store.get_request(request_id)

    with pytest.raises(DecisionClaimLost, match="another decision took it"):
        world.store.apply_together(
            TEAM,
            finish=lambda _plan: request.model_copy(update={"status": AccessRequestStatus.GRANTED}),
            claim_id=first.claim_id,
            actor=SubjectRefV1(kind=SubjectKind.USER, id=OWNER.person_id),
            at=world.clock(),
        )
    assert world.store.get_request(request_id).status is AccessRequestStatus.WAITING
    with pytest.raises(DecisionInProgress):
        world.store.claim_decision(request_id, claim_id="decision_third", at=world.clock(), until=world.clock())


# -- reading inside the write transaction, and the approval id column -----------------


def test_a_grant_plan_reads_through_the_write_transactions_connection(world, monkeypatch) -> None:
    import storage.access_repository as access_repository

    apply_starter_profile(world.gateway.service, OWNER, "trusted", lead_agent_type=LEAD)
    service = world.gateway.service
    original = service.store.store_planned
    opened: list[str] = []
    connect = access_repository.connect_sqlite

    def counting(*args, **kwargs):
        opened.append(str(args[0]))
        return connect(*args, **kwargs)

    def store_planned(grant, plan, **kwargs):
        monkeypatch.setattr(access_repository, "connect_sqlite", counting)
        try:
            return original(grant, plan, **kwargs)
        finally:
            monkeypatch.setattr(access_repository, "connect_sqlite", connect)

    monkeypatch.setattr(service.store, "store_planned", store_planned)
    grant = service.hand_on_for_task(WORKER, OWNER, SCHEDULES, AccessLevel.WRITE, "task-1")

    assert grant is not None
    assert len(opened) == 1, "the plan's reads borrowed the write transaction's connection"


def test_an_older_store_gets_the_approval_id_column_filled_from_its_grants(tmp_path: Path, caplog) -> None:
    db = tmp_path / "old.db"
    settings = resolve_storage_settings({"backend": "sqlite", "sqlite": {"path": str(db)}})
    store = AccessRepository(settings)
    grant = GrantV1(
        grant_id="grant_old",
        team_id=TEAM,
        subject=LEAD_SUBJECT,
        target=SCHEDULES,
        level=AccessLevel.READ,
        granted_by=SubjectRefV1(kind=SubjectKind.USER, id=OWNER.person_id),
        approval_id="access_req_old",
        created_at_utc=datetime.now(timezone.utc),
    )
    store.create(grant)
    with sqlite3.connect(str(db)) as conn:  # a store from before 2026-10-17
        conn.execute("DROP INDEX access_grants_approval")
        conn.execute("ALTER TABLE access_grants DROP COLUMN approval_id")

    with caplog.at_level(logging.INFO, logger="storage.access_repository"):
        AccessRepository(settings)

    with sqlite3.connect(str(db)) as conn:
        assert conn.execute("SELECT approval_id FROM access_grants").fetchall() == [("access_req_old",)]
        assert conn.execute("SELECT name FROM sqlite_master WHERE name = 'access_grants_approval'").fetchone()
    assert "event=access_grants_approval_id_added grants=1" in caplog.text
