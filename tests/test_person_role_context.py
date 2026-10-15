"""TeamLead's role line for the person it works for (``custom_agents.person_role_context``).

The line comes only from the task's verified identity, appears only in
TeamLead's prompt, and is empty for work for nobody, so a role claimed in a
message never shapes guidance such as the onboarding tour's administrator
steps.
"""

from __future__ import annotations

import logging
from pathlib import Path
from types import SimpleNamespace

import pytest

from custom_agents.person_role_context import PERSON_ROLE_CONTEXT_PREFIX, describe_person_role, person_role_context
from tests.broker_access_support import context_signer, context_verify_keys

TEAM = "team-1"
PERSON = "usr_alice"


def _agent_and_meta(tmp_path: Path, *, roles: tuple[str, ...] | None, lead: bool = True, claimed: str | None = None):
    from custom_agents import access_runtime
    from custom_agents.task_manager import TaskManager

    access_runtime._runtime_access.cache_clear()
    signed = context_signer().issue(team_id=TEAM, person_id=PERSON, person_roles=roles) if roles is not None else None
    meta = SimpleNamespace(
        gateway_context=SimpleNamespace(task_context=signed, schedule_run=None, canonical_user_id=claimed)
    )
    agent = SimpleNamespace(
        id="agent-lead",
        team_id=TEAM,
        is_team_lead=lead,
        task_manager=TaskManager(db_path=str(tmp_path / "manager.db")),
        authorization_config={"enabled": False},
        instance_root=None,
        _logger=logging.getLogger("test"),
    )
    return agent, meta


@pytest.fixture(autouse=True)
def _verify_keys(monkeypatch) -> None:
    monkeypatch.setenv("ACCESS_CONTEXT_VERIFY_KEYS", context_verify_keys())


@pytest.mark.parametrize(
    ("roles", "expected"),
    [
        (("owner",), "an owner (a team administrator)"),
        (("admin",), "an administrator of the team"),
        (("member",), "a member (not a team administrator)"),
    ],
)
def test_teamlead_sees_the_signed_role(tmp_path, roles, expected) -> None:
    agent, meta = _agent_and_meta(tmp_path, roles=roles)

    line = person_role_context(agent, meta)

    assert line.startswith(f"{PERSON_ROLE_CONTEXT_PREFIX} {expected}.")
    assert "grants nothing" in line


def test_the_line_is_empty_for_workers_and_unverified_claims(tmp_path) -> None:
    worker, meta = _agent_and_meta(tmp_path, roles=("owner",), lead=False)
    assert person_role_context(worker, meta) == "", "only TeamLead gets the line"

    lead, unsigned = _agent_and_meta(tmp_path, roles=None, claimed=PERSON)
    assert person_role_context(lead, unsigned) == "", "a person named only in message fields shapes nothing"


def test_no_roles_reads_as_a_member() -> None:
    assert "a member (not a team administrator)" in describe_person_role(())


def test_the_context_engine_pins_the_role_line() -> None:
    from custom_agents.context_engine.base import ContextAssemblyRequest
    from custom_agents.context_engine.default import ContextEngineAdapters
    from custom_messages.contracts.routing_v1 import MessageHeader, MessageMeta
    from custom_messages.contracts.task_v1 import Task
    from custom_messages.enums import MessageMetaType
    from tests.test_context_engine import _engine

    engine = _engine()
    engine.adapters = ContextEngineAdapters(
        build_request_time_context=lambda meta: "Request Time Context:\n- now",
        build_request_conversation_context=lambda meta, text: "",
        build_team_routing_prompt_context=lambda: "",
        verified_person_of=lambda meta: None,
        build_person_role_context=lambda meta: describe_person_role(("admin",)),
    )
    task = Task(task_id="task_context_1", task_type=MessageMetaType.REQUEST, problem_statement="/tour")

    assembly = engine.assemble(
        ContextAssemblyRequest(
            incoming_content=task,
            execution_task_id=task.task_id,
            incoming_header=MessageHeader(),
            incoming_meta=MessageMeta(conversation_id="conversation_context_1"),
            identity_meta=MessageMeta(conversation_id="conversation_context_1"),
            incoming_message_json="{}",
        )
    )

    [section] = [section for section in assembly.sections if section.name == "person_role_context"]
    assert section.pinned and "an administrator of the team" in assembly.prompt
