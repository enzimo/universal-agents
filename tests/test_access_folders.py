"""Folder decisions against the real Cedar evaluator (access plan, Phase 5, example 6.3).

Every role holds write on the shared team/ and agents/ folders; an agent may
use the folder of the person its task works for and nobody else's; data
folders registered by the operator need grants, which hand down and narrow
like tool grants; blocks win.
"""

from __future__ import annotations

import pytest

from access_control.decisions import HandDownRefused
from access_control.entities import AgentPrincipal
from access_control.folders import FolderIdError, covers, folder_chain, grantable_folder_id, normalize_folder_id
from custom_messages.contracts.access_v2 import AccessLevel, GrantKind, TargetKind, TargetRefV1
from runtime_settings.instance_layout import person_folder_name
from tests.test_access_service import ALICE, BINARY, BOA, BOA_TYPE, BOB, LEAD, LEAD_TYPE, TEAM, spec
from tests.test_access_service import world as _service_world

world = _service_world  # the real evaluator with an SQLite access store
# Tests that decide through the evaluator skip until it is built, as in
# test_access_service.py (whose module-level skip does not reach this module).
# Without it they ran against a missing binary and failed closed, so a fresh
# checkout reported five failures that CI, which builds it, never saw.
needs_evaluator = pytest.mark.skipif(not BINARY.is_file(), reason="build ua-cedar-authorizer first (cargo build)")
READ, WRITE = AccessLevel.READ, AccessLevel.WRITE
SYSTEM = AgentPrincipal("agent_mem", "MemoryReviewAgent", "system", TEAM)


def folder(folder_id: str) -> TargetRefV1:
    return TargetRefV1(kind=TargetKind.FOLDER, id=folder_id)


def decide(service, agent, person, target_id, level, *, is_file=True):
    return service.decide_folder(
        agent=agent, person=person, target_id=target_id, is_file=is_file, level=level, task_id="task-1"
    )


def test_folder_ids_stay_below_a_registered_root() -> None:
    assert normalize_folder_id("/team/reports/") == "team/reports"
    assert folder_chain("data/reports/2026/q1.md") == ["data/reports/2026/q1.md", "data/reports/2026", "data/reports"]
    assert covers("data/reports", "data/reports/2026") and not covers("data/reports", "data/reports-old")
    for bad in ("../control", "team/../control", "control/x", "data", "team//x", "users"):
        with pytest.raises(FolderIdError):
            normalize_folder_id(bad)
    # A person's folder can be named (its owner shares it, decision D15).
    assert grantable_folder_id("users/alice/reports") == "users/alice/reports"


@needs_evaluator
def test_standard_folders_and_the_task_persons_own_folder(world) -> None:
    service, _clock = world
    bob_home = f"users/{person_folder_name('bob')}"
    alice_home = f"users/{person_folder_name('alice')}"

    for target in ("team/notes.md", "agents/run/log.txt", f"{bob_home}/draft.md"):
        assert decide(service, BOA, BOB, target, WRITE).allowed, target
    other = decide(service, BOA, BOB, f"{alice_home}/private.md", READ)
    assert not other.allowed
    assert "another person's private folder" in other.reason
    # Administrators have no special access to someone else's folder either.
    assert not decide(service, BOA, ALICE, f"{bob_home}/draft.md", READ).allowed


@needs_evaluator
def test_work_for_no_person_reaches_only_the_shared_folders(world) -> None:
    service, _clock = world

    assert decide(service, SYSTEM, None, "agents/memory/review.md", WRITE).allowed
    assert not decide(service, SYSTEM, None, f"users/{person_folder_name('bob')}/x.md", READ).allowed
    assert not decide(service, BOA, None, "team/x.md", READ).allowed


@needs_evaluator
def test_data_folders_follow_grants_and_hand_downs_narrow_them(world) -> None:
    """Example 6.3: the admin gives TeamLead write on data/reports; TeamLead gives BOA read on 2026."""

    service, _clock = world
    assert not decide(service, BOA, ALICE, "data/reports/2026/q1.md", READ).allowed

    service.grant_by_person(ALICE, spec(LEAD_TYPE, folder("data/reports"), WRITE, delegation_depth=1))
    service.hand_down(LEAD, spec(BOA_TYPE, folder("data/reports/2026"), READ), task_id="task-1")

    assert decide(service, BOA, ALICE, "data/reports/2026/q1.md", READ).allowed
    assert not decide(service, BOA, ALICE, "data/reports/2026/q1.md", WRITE).allowed
    assert not decide(service, BOA, ALICE, "data/reports/2025/q4.md", READ).allowed
    refused = decide(service, BOA, ALICE, "data/reports/2025/q4.md", READ)
    assert "request_access with folder='data/reports/2025'" in refused.next_step
    # A member without access to the data folder: the agent never exceeds its person.
    assert not decide(service, BOA, BOB, "data/reports/2026/q1.md", READ).allowed


@needs_evaluator
def test_hand_downs_stay_inside_the_folder_they_come_from(world) -> None:
    service, _clock = world
    service.grant_by_person(ALICE, spec(LEAD_TYPE, folder("data/reports/2026"), WRITE, delegation_depth=1))

    for target in ("data/reports", "data/reports-old/2026", "team"):
        with pytest.raises(HandDownRefused):
            service.hand_down(LEAD, spec(BOA_TYPE, folder(target), READ), task_id="task-1")
    with pytest.raises(HandDownRefused):
        service.grant_by_person(ALICE, spec(BOA_TYPE, folder(f"users/{person_folder_name('alice')}"), READ))


@needs_evaluator
def test_a_block_on_a_subfolder_wins_over_the_standard_grant(world) -> None:
    service, _clock = world
    service.grant_by_person(ALICE, spec(BOA_TYPE, folder("team/finance"), WRITE, kind=GrantKind.BLOCK))

    blocked = decide(service, BOA, BOB, "team/finance/budget.xlsx", READ)

    assert not blocked.allowed
    assert any(item.startswith("block:") for item in blocked.blocking_policy_ids)
    assert "only a person can remove the block" in blocked.next_step
    assert decide(service, BOA, BOB, "team/other/notes.md", READ).allowed
