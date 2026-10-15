"""A stand-in for a new system schedule kind, added to the table in a test.

Tests add a table row (``custom_messages.system_schedules``) and a preparation
hook to check classification and preparation for a per-person kind an
on-demand specialist runs, like the personal-facts memory review. A team row
also checks no-person permit issuance and identity verification, like
internal-improvement maintenance. Its fire path must still call
``sign_system_run``; these fixtures do not add that path. The rows model the
planned knowledge-base curator.

The stand-in reuses ``SystemScheduleKind.DELEGATION_ASSURANCE``: a schedule's
kind must be an enum member to be saved, and no current code creates that
obsolete kind, so swapping its row changes nothing else under test.
"""

from __future__ import annotations

from dataclasses import replace
from typing import Any

import pytest

from custom_messages import system_schedules
from custom_messages.enums import SystemScheduleKind
from custom_messages.system_schedules import SystemScheduleKindSpec, SystemScheduleScope

STAND_IN_KIND = SystemScheduleKind.DELEGATION_ASSURANCE
CURATOR_AGENT_TYPE = "KnowledgeCuratorAgent"
CURATOR_DELIVERY_MODE = "knowledge_review"
CURATOR_PREPARER = "knowledge_review"
PERSON_NAME_PREFIX = "knowledge_review:person:"
TEAM_NAME_PREFIX = "knowledge_review:team:"

# Like the personal-facts memory review: one per person, signed for them, run
# by its own specialist on an on-demand local worker.
PERSON_KIND = SystemScheduleKindSpec(
    kind=STAND_IN_KIND,
    name_prefix=PERSON_NAME_PREFIX,
    scope=SystemScheduleScope.PERSON,
    agent_type=CURATOR_AGENT_TYPE,
    local_worker=True,
    delivery_mode=CURATOR_DELIVERY_MODE,
    internal=True,
    preparer=CURATOR_PREPARER,
    coordinator_event=("knowledge_review_schedule", "advisory"),
)

# Like internal-improvement maintenance: the team's, and its runs work for no person.
NO_PERSON_KIND = replace(
    PERSON_KIND,
    name_prefix=TEAM_NAME_PREFIX,
    scope=SystemScheduleScope.TEAM,
    system_work=True,
)


def use_kind(monkeypatch: pytest.MonkeyPatch, spec: SystemScheduleKindSpec, *, preparer: Any = None) -> None:
    """Put ``spec`` in the table in place of the stand-in kind's row, and register its preparer."""

    rows = tuple(row for row in system_schedules.SYSTEM_SCHEDULE_KINDS if row.kind is not spec.kind)
    monkeypatch.setattr(system_schedules, "SYSTEM_SCHEDULE_KINDS", (*rows, spec))
    if spec.preparer is not None:
        from custom_agents import scheduled_runtime

        monkeypatch.setitem(
            scheduled_runtime.SYSTEM_SCHEDULE_PREPARERS,
            spec.preparer,
            preparer or (lambda _agent, _schedule_id, scheduled_message: scheduled_message),
        )
