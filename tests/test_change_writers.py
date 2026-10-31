"""Who an agent's change is for, from verified identity only (custom_agents.change_writers)."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from access_control.identity import ISSUER_GATEWAY
from custom_agents import access_runtime
from custom_agents.change_writers import agent_change_writer, verified_work
from storage.change_ledger import ChangeWriter, ChangeWriterKind


def _agent(context, *, checks: bool = True) -> SimpleNamespace:
    agent = SimpleNamespace(_request_conflict_checks=checks)
    agent.context = context
    return agent


@pytest.fixture(autouse=True)
def _verified(monkeypatch):
    monkeypatch.setattr(access_runtime, "verified_task_context", lambda agent: agent.context)


def test_a_context_the_gateway_signed_for_a_person_is_their_request() -> None:
    context = SimpleNamespace(issuer=ISSUER_GATEWAY, person_id="alice", context_id="ctx_1")

    work = verified_work(_agent(context))

    assert (work.person_id, work.work_id, work.request_id) == ("alice", "ctx_1", "ctx_1")
    assert agent_change_writer(_agent(context, checks=False)) == ChangeWriter(
        ChangeWriterKind.REQUEST, request_id="ctx_1", person_id="alice", checked=False
    )


def test_a_scheduler_signed_context_is_scheduled_work_and_no_context_is_system_upkeep() -> None:
    scheduled = SimpleNamespace(issuer="scheduler", person_id="alice", context_id="ctx_run")

    assert agent_change_writer(_agent(scheduled)) == ChangeWriter(ChangeWriterKind.SCHEDULED, person_id="alice")
    assert agent_change_writer(_agent(None)) == ChangeWriter(ChangeWriterKind.SYSTEM)
    # A gateway context with no person is work for nobody, never a request.
    unowned = SimpleNamespace(issuer=ISSUER_GATEWAY, person_id="", context_id="ctx_2")
    assert agent_change_writer(_agent(unowned)) == ChangeWriter(ChangeWriterKind.SYSTEM)
