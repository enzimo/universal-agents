"""One entry per tool: the pure rules for how grants combine (access_control.grant_entries)."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from access_control.grant_entries import entry_key, grant_covers, plan_grants
from custom_messages.contracts.access_v2 import (
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

TEAM = "team-1"
LEAD = SubjectRefV1(kind=SubjectKind.AGENT_TYPE, id=f"{TEAM}/TeamArchitect")
WORKER = SubjectRefV1(kind=SubjectKind.AGENT_TYPE, id=f"{TEAM}/WebResearchAgent")
GOOGLE = TargetRefV1(kind=TargetKind.TOOL, id="cli:google")
GMAIL_SEND = TargetRefV1(kind=TargetKind.OPERATION, id="cli:google#gmail_send")
WORKSPACE = TargetRefV1(kind=TargetKind.TOOL_SET, id="google_workspace")
START = datetime(2026, 9, 30, tzinfo=timezone.utc)


def tools_in_set(tool_set_id: str) -> frozenset[str]:
    return frozenset({"cli:google"}) if tool_set_id == "google_workspace" else frozenset()


def grant(grant_id: str, level: AccessLevel = AccessLevel.WRITE, **fields) -> GrantV1:
    values = {
        "grant_id": grant_id,
        "team_id": TEAM,
        "subject": LEAD,
        "target": GOOGLE,
        "level": level,
        "granted_by": SubjectRefV1(kind=SubjectKind.USER, id="alice"),
        "created_at_utc": START,
        "for_person_id": "alice",
    }
    values.update(fields)
    return GrantV1(**values)


def task(grant_id: str, level: AccessLevel, task_id: str = "task-1", **fields) -> GrantV1:
    return grant(grant_id, level, lifetime=GrantLifetime.TASK, task_id=task_id, **fields)


def test_the_example_three_gog_grants_become_one_entry() -> None:
    # What the owner saw: read-and-change for one task, one operation for one
    # task, then full until removed that can be handed on.
    drafts = task("drafts", AccessLevel.WRITE)
    send = task("send", AccessLevel.WRITE, target=GMAIL_SEND, delegation_depth=0)
    full = grant("full", AccessLevel.FULL, delegation_depth=1)

    plan = plan_grants([full], [drafts, send], tools_in_set)

    assert [item.grant_id for item in plan.new] == ["full"]
    assert plan.replaced == {"drafts": "full", "send": "full"}
    assert entry_key(send) == entry_key(full), "an operation belongs to its tool's entry"


def test_access_already_covered_stores_nothing() -> None:
    full = grant("full", AccessLevel.FULL, delegation_depth=1)

    plan = plan_grants([task("again", AccessLevel.WRITE)], [full], tools_in_set)

    assert plan.new == [] and plan.replaced == {}
    assert plan.covered["again"].grant_id == "full"


def test_a_higher_level_for_one_task_sits_beside_lasting_access() -> None:
    lasting = grant("lasting", AccessLevel.WRITE)
    step_up = task("step-up", AccessLevel.FULL)

    plan = plan_grants([step_up], [lasting], tools_in_set)

    assert [item.grant_id for item in plan.new] == ["step-up"] and plan.replaced == {}
    # Lasting full later replaces both.
    later = plan_grants([grant("full", AccessLevel.FULL)], [lasting, step_up], tools_in_set)
    assert later.replaced == {"lasting": "full", "step-up": "full"}


@pytest.mark.parametrize(
    ("outer", "inner", "covers"),
    [
        # Level
        (grant("a", AccessLevel.READ), grant("b", AccessLevel.WRITE), False),
        # Lifetime and expiry
        (grant("a", lifetime=GrantLifetime.UNTIL, expires_at_utc=START + timedelta(days=1)), grant("b"), False),
        (
            grant("a", lifetime=GrantLifetime.UNTIL, expires_at_utc=START + timedelta(days=2)),
            grant("b", lifetime=GrantLifetime.UNTIL, expires_at_utc=START + timedelta(days=1)),
            True,
        ),
        (task("a", AccessLevel.FULL, "task-1"), task("b", AccessLevel.READ, "task-2"), False),
        (
            task("a", AccessLevel.FULL),
            grant("b", AccessLevel.READ, lifetime=GrantLifetime.ONE_TIME, task_id="task-1", uses_remaining=1),
            True,
        ),
        # Handing on
        (grant("a"), grant("b", delegation_depth=1), False),
        (
            grant("a", delegation_depth=1, delegation_mode=DelegationMode.ASK_OWNER),
            grant("b", delegation_depth=1),
            False,
        ),
        (grant("a", delegation_depth=1, delegate_to=(WORKER,)), grant("b", delegation_depth=1), False),
        (grant("a", delegation_depth=1), grant("b", delegation_depth=1, delegate_to=(WORKER,)), True),
        # Who and whose work
        (grant("a"), grant("b", subject=WORKER), False),
        (grant("a"), grant("b", for_person_id="bob"), False),
        # Targets: a tool covers its operations, a set its tools, never the other way round
        (grant("a"), grant("b", target=GMAIL_SEND), True),
        (grant("a", target=GMAIL_SEND), grant("b"), False),
        (grant("a", target=WORKSPACE), grant("b"), True),
        (grant("a"), grant("b", target=WORKSPACE), False),
        # Blocks are not access
        (grant("a"), grant("b", kind=GrantKind.BLOCK, for_person_id=None), False),
    ],
)
def test_what_covers_what(outer: GrantV1, inner: GrantV1, covers: bool) -> None:
    assert grant_covers(outer, inner, tools_in_set) is covers


def test_a_batch_keeps_only_its_broadest_grants() -> None:
    old = grant("old", AccessLevel.READ)
    batch = [grant("tool", AccessLevel.WRITE), grant("set", AccessLevel.WRITE, target=WORKSPACE)]

    plan = plan_grants(batch, [old], tools_in_set)

    assert [item.grant_id for item in plan.new] == ["set"]
    assert plan.replaced == {"old": "set"}, "what the dropped grant replaced passes to the one that replaced it"


def test_pending_hand_downs_and_blocks_are_stored_as_they_are() -> None:
    full = grant("full", AccessLevel.FULL)
    pending = grant("pending", AccessLevel.READ, status=GrantStatus.PENDING)
    block = grant("block", kind=GrantKind.BLOCK, for_person_id=None)

    plan = plan_grants([pending, block], [full], tools_in_set)

    assert [item.grant_id for item in plan.new] == ["pending", "block"] and not plan.covered
