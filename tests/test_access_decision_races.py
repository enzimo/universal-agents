"""Deciding an access request once, and keeping grant records consistent, with real Cedar and SQLite.

Each test reproduces a concurrency defect found in the 2026-10-17 audit on the
real code: two processes (the gateway and the manager, or two gateway
threads) share one access store file.

- A failed approval took nothing back, so a later Deny left access in force.
- An approval that lost to a withdrawal revoked access that was in force
  before the request (the team lead's starter-profile grant).
- Two approvals at once: the loser revoked the winner's grants.
- A decision whose process ended left its grants and its claim behind.
- "One entry per tool" was planned outside the write transaction, so two
  grants stored at once both landed (also a tool grant beside its tool set).
- Two account links stored at once both stayed active.
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
from access_control.starter_profiles import apply_starter_profile
from custom_messages.contracts.access_v2 import (
    AccessLevel,
    AccessRequestStatus,
    AccessReviewChoice,
    AccountLinkStatus,
    AccountLinkV1,
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
from storage.access_repository import ONE_ACTIVE_LINK_INDEX, AccessRepository
from storage.config import resolve_storage_settings
from team_transfer.access import AccessKind, AccessRecordV1, _newest_links, _older_link
from tests.test_access_requests import BINARY, LEAD, OWNER, SCHEDULES, TEAM, WORKER, RecordingInbox, ask, uses

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


def test_a_failed_approval_takes_back_its_grants_and_a_later_deny_leaves_no_access(world, monkeypatch) -> None:
    request_id = careful_request(world)
    store = world.store
    original = store.finish_request

    def finish_while_another_writer_holds_the_lock(updated, **kwargs):
        # Another process holds the write lock longer than the 2.5 s busy timeout.
        other = sqlite3.connect(str(world.db), isolation_level=None)
        other.execute("BEGIN IMMEDIATE")
        try:
            return original(updated, **kwargs)
        finally:
            other.execute("ROLLBACK")
            other.close()

    monkeypatch.setattr(store, "finish_request", finish_while_another_writer_holds_the_lock)
    with pytest.raises(sqlite3.OperationalError, match="locked"):
        world.gateway.decide(request_id, OWNER, AccessReviewChoice.APPROVE)
    monkeypatch.setattr(store, "finish_request", original)

    assert store.get_request(request_id).status is AccessRequestStatus.WAITING
    assert world.approval_grants(request_id) == [], "the approval's grants were taken back"
    assert not uses(world.gateway, WORKER, OWNER, CREATE_JOB)

    denied = world.manager.decide(request_id, OWNER, AccessReviewChoice.DENY)
    assert denied.status is AccessRequestStatus.DENIED
    assert not uses(world.gateway, WORKER, OWNER, CREATE_JOB), "a denied request leaves no access"


def test_grants_a_failed_approval_could_not_take_back_end_when_its_claim_runs_out(world, monkeypatch, caplog) -> None:
    request_id = careful_request(world)
    service = world.gateway.service

    def locked(*_args, **_kwargs):
        raise sqlite3.OperationalError("database is locked")

    monkeypatch.setattr(service.store, "finish_request", locked)
    monkeypatch.setattr(service, "revoke", locked)
    with pytest.raises(sqlite3.OperationalError):
        world.gateway.decide(request_id, OWNER, AccessReviewChoice.APPROVE)
    monkeypatch.undo()

    left = world.approval_grants(request_id)
    assert left and uses(world.gateway, WORKER, OWNER, CREATE_JOB), "the undo failed, so the grants are still there"
    with pytest.raises(HandDownRefused, match="deciding this request right now"):
        world.manager.decide(request_id, OWNER, AccessReviewChoice.DENY)
    assert world.store.recover_expired_claims(TEAM, world.clock()) == [], "the claim has not run out yet"

    world.clock.advance(DECISION_CLAIM_SECONDS + 1)
    with caplog.at_level(logging.WARNING, logger="storage.access_repository"):
        assert world.store.recover_expired_claims(TEAM, world.clock()) == [request_id]
    assert "event=access_decision_claim_recovered" in caplog.text
    assert "event=access_decision_leftovers_ended" in caplog.text
    assert world.approval_grants(request_id) == []
    assert {world.store.get(grant.grant_id).status for grant in left} == {GrantStatus.REVOKED}
    assert not uses(world.gateway, WORKER, OWNER, CREATE_JOB)
    assert world.store.get_request(request_id).status is AccessRequestStatus.WAITING
    assert world.manager.decide(request_id, OWNER, AccessReviewChoice.DENY).status is AccessRequestStatus.DENIED


def test_a_decision_whose_process_ended_is_taken_over_once_its_claim_runs_out(world) -> None:
    request_id = careful_request(world)
    request = world.store.get_request(request_id)
    # A gateway claimed the request, stored read-and-change for the team lead, then its process ended.
    now = world.clock()
    world.store.claim_decision(
        request_id, claim_id="decision_ended", at=now, until=now + timedelta(seconds=DECISION_CLAIM_SECONDS)
    )
    [lead_spec, *_rest] = world.gateway.approval_specs(
        request,
        level=AccessLevel.WRITE,
        lifetime=GrantLifetime.UNTIL_REVOKED,
        expires_at=None,
        granted_by=SubjectRefV1(kind=SubjectKind.USER, id=OWNER.person_id),
    )
    left = world.gateway.service.grant_by_person(OWNER, lead_spec.model_copy(update={"approval_id": request_id}))

    with pytest.raises(HandDownRefused, match="deciding this request right now"):
        world.manager.decide(request_id, OWNER, AccessReviewChoice.APPROVE_READ)
    world.clock.advance(DECISION_CLAIM_SECONDS + 1)
    decided = world.manager.decide(request_id, OWNER, AccessReviewChoice.APPROVE_READ)

    assert decided.status is AccessRequestStatus.GRANTED
    assert world.store.get(left.grant_id).status is GrantStatus.REVOKED, "the unfinished decision's grant ended first"
    assert left.grant_id not in decided.grant_ids
    assert uses(world.gateway, WORKER, OWNER, READ_JOBS)
    assert not uses(world.gateway, WORKER, OWNER, CREATE_JOB), "only what this decision approved is in force"


def test_an_approval_that_loses_to_a_withdrawal_keeps_the_access_that_was_in_force(world, monkeypatch) -> None:
    request_id = careful_request(world)
    [standard] = [grant for grant in world.store.for_subjects(TEAM, [LEAD_SUBJECT]) if grant.target == STANDARD_SET]
    store = world.store
    original = store.finish_request

    def task_ends_first(updated, **kwargs):
        if kwargs.get("claim_id"):
            world.manager.withdraw(request_id, why="The task ended first.")
        return original(updated, **kwargs)

    monkeypatch.setattr(store, "finish_request", task_ends_first)
    # Read only: the team lead's part is covered by its standard grant, which this approval did not create.
    with pytest.raises(HandDownRefused, match="already decided"):
        world.gateway.decide(request_id, OWNER, AccessReviewChoice.APPROVE_READ)

    assert store.get(standard.grant_id).status is GrantStatus.ACTIVE, "access in force before the request stays"
    assert store.get_request(request_id).status is AccessRequestStatus.WITHDRAWN
    assert world.approval_grants(request_id) == []


def test_a_second_approval_while_the_first_runs_is_refused_and_the_first_stands(world, monkeypatch) -> None:
    request_id = careful_request(world)
    store = world.store
    original = store.finish_request
    refusals: list[str] = []

    def second_click(updated, **kwargs):
        # The same person presses Approve again (or a chat button and the web card at once).
        try:
            world.manager.decide(request_id, OWNER, AccessReviewChoice.APPROVE)
        except HandDownRefused as exc:
            refusals.append(str(exc))
        return original(updated, **kwargs)

    monkeypatch.setattr(store, "finish_request", second_click)
    decided = world.gateway.decide(request_id, OWNER, AccessReviewChoice.APPROVE)

    assert refusals and "deciding this request right now" in refusals[0]
    assert decided.status is AccessRequestStatus.GRANTED
    assert {store.get(grant_id).status for grant_id in decided.grant_ids} == {GrantStatus.ACTIVE}
    assert uses(world.gateway, WORKER, OWNER, CREATE_JOB)
    assert len(world.approval_grants(request_id)) == len(decided.grant_ids), "the second click stored nothing"


def test_a_deny_ends_access_an_unfinished_decision_left_and_a_withdrawal_does_too(world) -> None:
    request_id = careful_request(world)
    request = world.store.get_request(request_id)
    [lead_spec, *_rest] = world.gateway.approval_specs(
        request,
        level=AccessLevel.WRITE,
        lifetime=GrantLifetime.UNTIL_REVOKED,
        expires_at=None,
        granted_by=SubjectRefV1(kind=SubjectKind.USER, id=OWNER.person_id),
    )
    # A grant an earlier, unfinished decision stored (no claim is left: it was released).
    left = world.gateway.service.grant_by_person(OWNER, lead_spec.model_copy(update={"approval_id": request_id}))
    withdrawn = world.manager.withdraw(request_id, why="The task ended first.")
    assert withdrawn.status is AccessRequestStatus.WITHDRAWN
    assert world.store.get(left.grant_id).status is GrantStatus.REVOKED


# -- one entry per tool, planned inside the write transaction -------------------------


def _store_after_both_planned(world: World, monkeypatch, *calls) -> list[object]:
    """Run ``calls`` at once in two threads, both past their checks before either writes."""

    barrier = threading.Barrier(len(calls))
    for side in (world.gateway, world.manager):
        original = side.service.store.store_planned

        def store_planned(grant, plan, _original=original):
            barrier.wait(10)
            return _original(grant, plan)

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


def test_a_transfer_file_with_two_links_for_one_tool_and_owner_refuses_the_older(world) -> None:
    now = world.clock()
    records = [
        AccessRecordV1(kind=AccessKind.ACCOUNT_LINK, record_id=link.link_id, document=link.model_dump(mode="json"))
        for link in (_link("link_new", "x_new", now), _link("link_old", "x_old", now - timedelta(minutes=5)))
    ]
    newest = _newest_links([record.model_dump(mode="json") for record in records])
    new, old = records

    assert _older_link(new, newest) == ""
    assert "the newer account link link_new" in _older_link(old, newest)
