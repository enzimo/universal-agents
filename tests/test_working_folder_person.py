"""The working-folder line names the verified person's folder (``custom_agents.context_engine.default``).

The file tools work in the folder of the person the task's signed context
names (``custom_tools.workspace_scope``). The prompt line that tells the model
its working folder must name that same folder, chosen through the context
engine's ``verified_person_of`` adapter, never from the ``canonical_user_id``
a message's gateway context claims. Real Ed25519 keys, the real identity
resolver and the real tool-side folder resolver.

Covers:
- a turn signed for Ann that claims Bob names Ann's folder, the folder the
  tools use, and logs the mismatch;
- a claim without a signed context names only the shared folders;
- plan-step and recovery turns keep their verified folder while hiding gateway
  details from the prompt, and unsigned claims still select no person's folder;
- the agent gives the context engine and the memory manager the same
  verified-person check.
"""

from __future__ import annotations

import json
import logging
from collections import deque
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import pytest

from access_control.identity import ENV_VERIFY_KEYS
from tests.task_work_support import bind_task
from custom_agents.access_runtime import verified_meta_person_id
from custom_agents.context_engine import ContextAssemblyRequest, ContextEngineAdapters
from custom_messages.contracts.routing_v1 import GatewayContext, MessageHeader, MessageMeta
from custom_messages.contracts.task_v1 import Task
from custom_messages.enums import MessageMetaType
from runtime_settings.instance_layout import InstanceLayout
from tests.broker_access_support import context_signer, context_verify_keys, forget_task_context_keys

TEAM = "team-1"
ANN = "usr_ann"
BOB = "usr_bob"
ACCESS_LOGGER = "custom_agents.access_runtime"
TASK_ID = "task_context_1"  # the id the context engine test fixtures expect


@pytest.fixture(autouse=True)
def _verify_keys(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv(ENV_VERIFY_KEYS, context_verify_keys())
    forget_task_context_keys()
    yield
    forget_task_context_keys()


def _agent(tmp_path: Path, meta: MessageMeta) -> SimpleNamespace:
    """An agent running ``meta``'s task, as the identity check and the file tools see it."""

    from custom_agents.task_manager import TaskManager

    tasks = TaskManager(db_path=str(tmp_path / "manager.db"))
    bind_task(TASK_ID)  # the agent's calls work for this task (tests.task_work_support)
    return SimpleNamespace(
        id="agent-lead",
        team_id=TEAM,
        workspace_dir=str(InstanceLayout.at(tmp_path).workspaces_dir),
        authorization_config={"enabled": True},
        instance_root=None,
        test_task_id=TASK_ID,
        task_manager=SimpleNamespace(
            storage_settings=tasks.storage_settings,
            get_task_info=lambda _task_id: SimpleNamespace(meta=meta),
        ),
    )


def _working_folder_line(agent: SimpleNamespace, meta: MessageMeta) -> str:
    from tests.test_context_engine import _engine

    engine = _engine(workspace_dir=agent.workspace_dir)
    engine.adapters = ContextEngineAdapters(
        build_request_time_context=lambda _meta: "",
        build_request_conversation_context=lambda _meta, _text: "",
        build_team_routing_prompt_context=lambda: "",
        verified_person_of=lambda incoming: verified_meta_person_id(agent, incoming),
    )
    task = Task(task_id=TASK_ID, task_type=MessageMetaType.REQUEST, problem_statement="Save my notes.")
    assembly = engine.assemble(
        ContextAssemblyRequest(
            incoming_content=task,
            execution_task_id=task.task_id,
            incoming_header=MessageHeader(),
            incoming_meta=meta,
            identity_meta=meta,
            incoming_message_json=json.dumps({}),
        )
    )
    [section] = [section for section in assembly.sections if section.name == "workspace_user_context"]
    return next(line for line in section.text.splitlines() if line.startswith("Working folder for this task:"))


def _meta(gateway_context: GatewayContext) -> MessageMeta:
    return MessageMeta(conversation_id="conversation_context_1", gateway_context=gateway_context)


def test_a_turn_signed_for_ann_that_claims_bob_names_anns_folder(tmp_path, caplog) -> None:
    from custom_tools.workspace_scope import current_workspace_scope

    signed = context_signer().issue(team_id=TEAM, person_id=ANN, person_roles=("member",))
    meta = _meta(GatewayContext(canonical_user_id=BOB, task_context=signed))
    agent = _agent(tmp_path, meta)
    layout = InstanceLayout.at(tmp_path)

    with caplog.at_level(logging.WARNING, logger=ACCESS_LOGGER):
        line = _working_folder_line(agent, meta)

    assert line.startswith(f"Working folder for this task: {layout.person_dir(ANN)} ")
    assert str(layout.person_dir(BOB)) not in line
    assert current_workspace_scope(agent).home == layout.person_dir(ANN), "the tools use the same folder"
    assert "event=claimed_person_mismatch" in caplog.text


def test_a_claim_without_a_signed_context_names_only_the_shared_folders(tmp_path, caplog) -> None:
    from custom_tools.workspace_scope import current_workspace_scope

    meta = _meta(GatewayContext(canonical_user_id=BOB))
    agent = _agent(tmp_path, meta)
    layout = InstanceLayout.at(tmp_path)

    with caplog.at_level(logging.WARNING, logger=ACCESS_LOGGER):
        line = _working_folder_line(agent, meta)

    assert line.startswith(f"Working folder for this task: {layout.agents_dir} ")
    assert "does not work for a specific person" in line
    assert current_workspace_scope(agent).home == layout.agents_dir, "the tools use the same folder"
    assert "event=unverified_person_ignored" in caplog.text


@pytest.mark.asyncio
@pytest.mark.parametrize("turn_kind", ["plan_step", "isolated_recovery"])
@pytest.mark.parametrize(
    "signed", [True, False], ids=["verified_person", "unsigned_claim"]
)
async def test_isolated_turns_name_the_tools_folder_without_exposing_gateway_context(
    tmp_path, turn_kind, signed, caplog
) -> None:
    from custom_agents.sdk.strands.turn import _TurnState
    from custom_tools.workspace_scope import current_workspace_scope
    from tests.test_context_engine import _engine
    from tests.test_runtime_owner_boundaries import build_turn_owner

    context = (
        context_signer().issue(team_id=TEAM, person_id=ANN, person_roles=("member",))
        if signed
        else None
    )
    meta = _meta(
        GatewayContext(
            canonical_user_id=BOB, session_key="private_session", task_context=context
        )
    )
    agent = _agent(tmp_path, meta)
    engine = _engine(workspace_dir=agent.workspace_dir)
    engine.adapters = ContextEngineAdapters(
        build_request_time_context=lambda _meta: "",
        build_request_conversation_context=lambda _meta, _text: "",
        build_team_routing_prompt_context=lambda: "",
        verified_person_of=lambda incoming: verified_meta_person_id(agent, incoming),
    )

    async def refuse_model_call(*_args, **_kwargs):
        raise AssertionError("Turn preparation must not call a model.")

    executor, _result = build_turn_owner(
        invoke=refuse_model_call, guidance=deque([""]), committed=[], cleared=[]
    )
    executor.context = lambda: engine
    executor.tasks = replace(
        executor.tasks,
        is_plan_step_task=lambda _task_id: turn_kind == "plan_step",
        is_isolated_recovery_task=lambda _task_id: turn_kind == "isolated_recovery",
    )
    executor.effects = replace(
        executor.effects,
        set_active_execution_context=lambda task_id: setattr(
            agent, "_active_execution_task_id", task_id
        ),
    )
    task = Task(
        task_id=TASK_ID,
        task_type=MessageMetaType.REQUEST,
        problem_statement="Save my notes.",
    )
    turn = _TurnState(task, MessageHeader(), meta)

    with caplog.at_level(logging.WARNING, logger=ACCESS_LOGGER):
        assert await executor._prepare_turn(turn) is None

    layout = InstanceLayout.at(tmp_path)
    expected_home = layout.person_dir(ANN) if signed else layout.agents_dir
    line = next(
        line
        for section in turn.assembly.sections
        if section.name == "workspace_user_context"
        for line in section.text.splitlines()
        if line.startswith("Working folder for this task:")
    )
    assert line.startswith(f"Working folder for this task: {expected_home} ")
    assert current_workspace_scope(agent).home == expected_home
    assert str(layout.person_dir(BOB)) not in turn.assembly.prompt
    assert turn.prompt_meta.gateway_context is None
    assert meta.gateway_context.task_context is context
    assert "private_session" not in turn.assembly.prompt
    assert "task_context" not in turn.assembly.prompt
    assert "schedule_run" not in turn.assembly.prompt
    assert "private_session" not in turn.context_request.incoming_message_json
    assert "task_context" not in turn.context_request.incoming_message_json
    expected_event = (
        "event=claimed_person_mismatch" if signed else "event=unverified_person_ignored"
    )
    assert expected_event in caplog.text


def test_the_agent_gives_its_context_engine_the_verified_person(tmp_path, monkeypatch) -> None:
    from custom_agents.strands_agent import UniversalAgent

    monkeypatch.chdir(tmp_path)
    agent = UniversalAgent(role="working folder check", tools=[], team_id=TEAM, workspace_dir=str(tmp_path))
    engine = agent._context_engine
    signed = context_signer().issue(team_id=TEAM, person_id=ANN, person_roles=("member",))

    assert engine.adapters.verified_person_of(_meta(GatewayContext(canonical_user_id=BOB, task_context=signed))) == ANN
    assert engine.adapters.verified_person_of(_meta(GatewayContext(canonical_user_id=BOB))) is None
    assert engine.adapters.verified_person_of is engine.memory_plugin_manager.verified_person_of
