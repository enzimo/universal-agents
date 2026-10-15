"""Gateway commands name their person only through the gateway-signed context.

Until 2026-10-07 the manager compared a person written in the command body
with an unsigned copy in the same message, trusted the body's roles for team
memory and a "may change system schedules" flag from the gateway, and verified
the signature only for person standing commands. Anything able to publish to
the manager's control subject could claim any person and role.
"""

from __future__ import annotations

import logging
from pathlib import Path

import pytest

from access_control.identity import ContextSigner
from custom_agents.manager_control_runtime import (
    handle_manager_memory_command,
    verified_command_person,
)
from custom_messages.contracts.control_v1 import ManagerMemoryCommand, PinnedMemoryCommandPayload
from custom_messages.contracts.routing_v1 import GatewayContext, MessageHeader, MessageMeta
from tests.broker_access_support import context_signer, use_scheduler_key
from tests.test_manager_control_runtime import (  # noqa: F401
    _bootstrap_agent,
    _CaptureComms,
    _config,
    _gateway_signing_keys,
    _signed,
)


def _memory_create(text: str = "Team deploys after smoke tests.") -> ManagerMemoryCommand:
    return ManagerMemoryCommand(
        command=PinnedMemoryCommandPayload(bank="coremem", action="create", fact_text=text)
    )


async def _send(agent, command: ManagerMemoryCommand, context: GatewayContext) -> ManagerMemoryCommand:
    await handle_manager_memory_command(
        agent,
        command=command,
        msg_header=MessageHeader(from_agent_id="gateway", response_routing_key="gateway"),
        msg_meta=MessageMeta(gateway_context=context),
    )
    return agent.team_comms.messages[-1].msg_content


@pytest.fixture
def manager(tmp_path: Path):
    config = _config(tmp_path)
    agent = _bootstrap_agent(config)
    agent.team_comms = _CaptureComms()
    agent._logger = logging.getLogger("test.command_identity")
    return agent, config


@pytest.mark.asyncio
async def test_an_unsigned_command_is_refused_whoever_it_claims_to_be(manager) -> None:
    agent, _config_ = manager

    response = await _send(agent, _memory_create(), GatewayContext(canonical_user_id="Owner"))

    assert response.success is False
    assert response.rendered_result.startswith("unsigned_sender")
    assert agent._pinned_memory_repository.list_coremem(team_id=agent.team_id) == []


@pytest.mark.asyncio
async def test_a_context_the_gateway_did_not_sign_for_this_team_is_refused(manager, monkeypatch) -> None:
    agent, config = manager
    use_scheduler_key(monkeypatch)
    scheduler = ContextSigner.from_env("scheduler")
    assert scheduler is not None

    by_scheduler = await _send(
        agent,
        _memory_create(),
        GatewayContext(
            canonical_user_id="Owner",
            task_context=scheduler.issue(team_id=agent.team_id, person_id="Owner", person_roles=("owner",)),
        ),
    )
    other_team = await _send(
        agent,
        _memory_create(),
        GatewayContext(
            canonical_user_id="Owner",
            task_context=context_signer().issue(team_id="another_team", person_id="Owner", person_roles=("owner",)),
        ),
    )

    assert (by_scheduler.success, by_scheduler.rendered_result) == (False, "sender_not_gateway")
    assert other_team.success is False and other_team.rendered_result.startswith("unsigned_sender")


@pytest.mark.asyncio
async def test_team_memory_follows_the_signed_roles_not_a_claimed_person(manager, caplog) -> None:
    agent, config = manager
    # The unsigned copy claims an owner; the gateway signed a member.
    signed_member = _signed(
        GatewayContext(canonical_user_id="Member"), team_id=config.gateway.team_id, roles=["member"]
    )
    forged_claim = signed_member.model_copy(update={"canonical_user_id": "Owner"})

    with caplog.at_level(logging.WARNING, logger="test.command_identity"):
        denied = await _send(agent, _memory_create(), forged_claim)
    allowed = await _send(
        agent,
        _memory_create(),
        _signed(GatewayContext(canonical_user_id="Owner"), team_id=config.gateway.team_id, roles=["admin"]),
    )

    assert denied.success is False and denied.error_code == "authorization_denied"
    assert any("event=manager_command_person_mismatch" in record.getMessage() for record in caplog.records)
    assert allowed.success is True
    assert [row.fact_text for row in agent._pinned_memory_repository.list_coremem(team_id=agent.team_id)] == [
        "Team deploys after smoke tests."
    ]


def test_the_verified_person_comes_from_the_signature(manager) -> None:
    agent, config = manager
    meta = MessageMeta(
        gateway_context=_signed(
            GatewayContext(canonical_user_id="Owner"), team_id=config.gateway.team_id, roles=["owner"]
        )
    )

    person, refusal = verified_command_person(agent, meta)

    assert refusal is None and person is not None
    assert (person.canonical_user_id, person.team_id, person.roles) == ("Owner", config.gateway.team_id, ("owner",))


def test_commands_carry_no_person_or_authority_fields() -> None:
    from custom_messages.contracts.control_v1 import (
        KnowledgeCommandPayload,
        ManagerKnowledgeCommand,
        ManagerRuntimeWorkCommand,
        ManagerTopicCommand,
        PersonStandingCommandV1,
    )
    from custom_messages.contracts.schedule_v1 import ManagerScheduleCommand

    # The Knowledge page's command names nobody: the manager acts for the signed person.
    person_fields = {"principal", "person_id", "canonical_user_id", "owner_person_id", "roles", "team_id"}
    assert not person_fields & set(KnowledgeCommandPayload.model_fields)
    assert not person_fields & set(ManagerKnowledgeCommand.model_fields)
    for model in (
        ManagerKnowledgeCommand,
        ManagerMemoryCommand,
        ManagerRuntimeWorkCommand,
        ManagerScheduleCommand,
        ManagerTopicCommand,
        PersonStandingCommandV1,
    ):
        assert "principal" not in model.model_fields, model.__name__
    assert "may_change_system_schedules" not in ManagerScheduleCommand.model_fields
