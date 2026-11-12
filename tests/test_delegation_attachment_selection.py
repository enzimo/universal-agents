"""The team lead chooses which files the person sent go to a delegated task (``attachment_ids``).

Left out, a delegated task gets the files of the delegating task's own
message, as before; a list, ``[]`` included, gives it exactly the files it
names. Ids are found only within the scope the lead's prompt lists and its
tools open (``custom_agents.conversation_attachments``): the request's files,
the files sent while it ran and, for the person's own conversation, its
earlier files. The new task gets the trusted records, never anything a model
wrote. These tests run the real team lead's outbox (``UniversalAgent``) on a
real task manager and a real SQLite conversation store, with the person taken
from a gateway-signed task context; the model's turn decision is parsed by the
same models the provider schemas come from. Specialists cannot choose files.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

import pytest
from pydantic import TypeAdapter, ValidationError
from strands.tools.structured_output.structured_output_tool import convert_pydantic_to_tool_spec

from custom_agents.conversation_store import ConversationStore
from custom_agents.structured_llm_messages import (
    COMPATIBLE_MODEL_AGENT_TURN_DECISION_FORCE_PROMPT,
    COMPATIBLE_WORKER_TURN_DECISION_FORCE_PROMPT,
    FIREWORKS_MODEL_AGENT_TURN_DECISION_FORCE_PROMPT,
    MODEL_AGENT_TURN_DECISION_FORCE_PROMPT,
    MODEL_WORKER_TURN_DECISION_FORCE_PROMPT,
    CompatibleModelAgentTurnDecision,
    CompatibleWorkerTurnDecision,
    DelegateTaskAction,
    ModelAgentTurnDecision,
    ModelWorkerTurnDecision,
    StructuredAgentTurnDecision,
    StructuredTaskPayload,
    TurnAction,
)
from custom_agents.turn_action_outbox_runtime import TurnActionRecoveryScheduled
from custom_messages.contracts.attachments_v1 import AttachmentStorageRef, UserTaskAttachment
from custom_messages.contracts.routing_v1 import GatewayContext, ManagerEscalationContext, MessageHeader, MessageMeta
from custom_messages.contracts.task_v1 import Task
from custom_messages.enums import MessageMetaType, ProgressState, RequesterType
from custom_tools.runtime_context import _agent_ctx
from runtime_settings.instance_layout import InstanceLayout
from tests.broker_access_support import context_signer
from tests.change_approval_support import use_signed_contexts
from tests.task_work_support import bind_task
from tests.test_turn_action_outbox import _make_agent, _make_task_manager

TEAM = "test_team"
PERSON = "alice"
OTHER_PERSON = "bob"
CONVERSATION = "conv_alice"
ROOT = "task_turn_2"
SPECIALIST = "WebResearchAgent"
LEFT_OUT = object()


@pytest.fixture(autouse=True)
def _signed_contexts(monkeypatch: pytest.MonkeyPatch):
    use_signed_contexts(monkeypatch)
    _agent_ctx.set(None)
    yield
    _agent_ctx.set(None)


class _Lead:
    """The team lead running turn 2 (``ROOT``, with list.png) of a conversation whose turn 1 sent notes and a photo."""

    def __init__(self, tmp_path: Path, *, signed_for: str | None = PERSON) -> None:
        self.layout = InstanceLayout.at(tmp_path / "instance")
        self.layout.ensure_directories()
        self.store = ConversationStore(
            db_path=str(self.layout.root / "conversations.db"), artifacts_dir=self.layout.conversation_artifacts_dir
        )
        self.manager = _make_task_manager(tmp_path)
        self.agent = _make_agent(tmp_path, task_manager=self.manager)
        self.agent._conversation_store = self.store
        self.agent.instance_root = str(self.layout.root)
        self.turn("task_turn_1", [self.file("att_notes", "notes.txt"), self.file("att_photo", "photo.png")])
        current = [self.file("att_list", "list.png")]
        self.turn(ROOT, current)
        self.start(ROOT, current, signed_for=signed_for)

    def file(self, attachment_id: str, filename: str) -> UserTaskAttachment:
        path = self.layout.uploads_dir / "session_alice" / attachment_id
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"data")
        return UserTaskAttachment(
            attachment_id=attachment_id, filename=filename, content_type="image/png", kind="image", size_bytes=4,
            sha256="ab" * 32, storage_refs=[AttachmentStorageRef(storage_type="local", path=str(path))],
        )

    def turn(
        self, task_id: str, files: list[UserTaskAttachment], *, conversation_id: str = CONVERSATION, person: str = PERSON
    ) -> None:
        self.store.ensure_thread(
            conversation_id=conversation_id, team_id=TEAM, owner_canonical_user_id=person,
            owner_agent_id=self.agent.id, owner_agent_name="manager", requester_name=person, requester_id=person,
            root_task_id=task_id,
        )
        self.store.append_turn(
            conversation_id=conversation_id, task_id=task_id, followup_to_task_id=None, owner_agent_id=self.agent.id,
            requester_name=person, requester_id=person, correlation_id=f"corr_{task_id}",
            problem_text=f"Request {task_id}", owner_canonical_user_id=person,
            attachments=[file.model_dump(mode="json") for file in files],
        )

    def start(self, task_id: str, files: list[UserTaskAttachment], *, signed_for: str | None) -> None:
        """The person's request, signed by the gateway for ``signed_for``, as the task the lead handles."""

        signed = (
            context_signer().issue(team_id=TEAM, person_id=signed_for, person_roles=("member",)) if signed_for else None
        )
        meta = MessageMeta(
            conversation_id=CONVERSATION, requester_type=RequesterType.USER_REQUESTER, is_response_requested=True,
            msg_correlation_id=f"corr_{task_id}",
            gateway_context=GatewayContext(canonical_user_id=PERSON, task_context=signed),
        )
        task = Task(task_id=task_id, task_type=MessageMetaType.REQUEST, problem_statement="Plan dinner.", attachments=files)
        header = MessageHeader(from_agent_id="gateway", response_routing_key="gateway")
        # Admitted for the person its signed context names, as the gateway's requests are.
        assert self.manager.add_task(task, header, meta, owner_person_id=signed_for)
        assert self.manager.get_next_task_to_process() == task_id
        assert self.manager.update_main_task_state(task_id, ProgressState.IN_PROGRESS, "2026-11-12T10:00:00+00:00")
        bind_task(task_id)

    def plan_step(self) -> str:
        """The first step of a plan the lead makes for ``ROOT``, started by the task manager and being handled."""

        plan = self.manager.activate_plan_execution(
            ROOT, steps=[{"title": "List", "objective": "Make the shopping list from the photo."}],
            final_review=False, agent_name="manager", agent_id=self.agent.id,
        )
        assert plan is not None
        return self._handle(str(plan.plan_steps[0].current_step_task_id))

    def recovery(self) -> str:
        """An isolated recovery task for ``ROOT``, started as the task manager starts one for a failed step."""

        self.manager.activate_plan_execution(
            ROOT, steps=[{"title": "List", "objective": "Make the shopping list from the photo."}],
            final_review=False, agent_name="manager", agent_id=self.agent.id,
        )
        root = self.manager.get_task_info(ROOT)
        meta = root.meta.model_copy(deep=True)
        meta.msg_correlation_id = "recovery_corr_recovery_task_1"
        meta.conversation_id = "recov_conv_1"
        info = self.manager.start_isolated_recovery_task(
            ROOT, failure_type="step_failed", failure_summary="The list step failed.",
            recovery_brief="Find a way to make the list.", header=root.header.model_copy(deep=True), meta=meta,
            recovery_task_id="recovery_task_1", notify_user=False, agent_name="manager", agent_id=self.agent.id,
        )
        assert info is not None
        return self._handle(info.task.task_id)

    def _handle(self, task_id: str) -> str:
        # Created without files of its own, as the task manager creates them.
        assert self.manager.get_task_info(task_id).task.attachments == []
        assert self.manager.update_main_task_state(task_id, ProgressState.IN_PROGRESS, "2026-11-12T10:00:00+00:00")
        bind_task(task_id)
        return task_id

    async def delegate(
        self, attachment_ids: Any = LEFT_OUT, *, model: type = ModelAgentTurnDecision, from_task: str = ROOT
    ) -> Task:
        """The lead's model delegates to a specialist from ``from_task``, as its provider schema lets it."""

        action: dict[str, Any] = {
            "action_type": "delegate_task",
            "task": {"problem_statement": "Make a shopping list from the photo."},
            "target_named_agent_type": SPECIALIST,
        }
        if attachment_ids is not LEFT_OUT:
            action["attachment_ids"] = attachment_ids
        decision = self.agent._turn_decision_from_structured_output(model.model_validate({"actions": [action]}))
        info = self.manager.get_task_info(from_task)
        return await self.agent._execute_turn_decision_outbox(
            decision=decision, incoming_content=info.task, incoming_header=info.header, incoming_meta=info.meta
        )

    def sent_files(self) -> list[UserTaskAttachment]:
        """The files of the task published to the specialist."""

        return list(self.agent.team_comms.send_message.await_args.kwargs["message"].msg_content.attachments)

    def subtasks(self) -> list[str]:
        return [info.task.task_id for info in self.manager.task_state.task_to_subtask_list.get(ROOT, [])]


def _ids(files) -> list[str]:
    return [file.attachment_id for file in files]


@pytest.mark.asyncio
@pytest.mark.parametrize("model", [ModelAgentTurnDecision, CompatibleModelAgentTurnDecision])
async def test_the_files_the_lead_names_go_to_the_specialist_with_their_trusted_records(
    tmp_path: Path, model: type, caplog: pytest.LogCaptureFixture
) -> None:
    lead = _Lead(tmp_path)
    caplog.set_level(logging.INFO)

    created = await lead.delegate(["att_photo", "att_notes", "att_photo"], model=model)

    # The earlier turn's photo and notes, each once, in the order named; the
    # current message's file is left out because the lead did not name it.
    assert _ids(created.attachments) == ["att_photo", "att_notes"]
    assert _ids(lead.manager.get_task_info(created.task_id).task.attachments) == ["att_photo", "att_notes"]
    assert lead.sent_files() == created.attachments
    # The records the gateway stored (with where the file is), so the specialist can open them.
    stored = {item["attachment_id"]: item for item in lead.store.get_turn_by_task_id("task_turn_1")["attachments"]}
    assert lead.sent_files()[0].storage_refs[0].path == str(lead.layout.uploads_dir / "session_alice" / "att_photo")
    assert lead.sent_files()[0].filename == stored["att_photo"]["filename"]
    assert (
        f"event=delegation_attachments_selected parent_task_id={ROOT} subtask_id={created.task_id} "
        "attachment_ids=att_photo,att_notes left_out_message_ids=att_list"
    ) in caplog.text


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("attachment_ids", "expected"),
    [
        # Left out: the current message's files, as before.
        (LEFT_OUT, ["att_list"]),
        (None, ["att_list"]),
        # An empty list: none.
        ([], []),
    ],
)
async def test_left_out_ids_pass_the_current_messages_files_and_an_empty_list_passes_none(
    tmp_path: Path, attachment_ids: Any, expected: list[str]
) -> None:
    lead = _Lead(tmp_path)

    for model in (ModelAgentTurnDecision, CompatibleModelAgentTurnDecision):
        created = await lead.delegate(attachment_ids, model=model)

        assert _ids(created.attachments) == expected
        assert _ids(lead.sent_files()) == expected


@pytest.mark.asyncio
@pytest.mark.parametrize("handled", ["plan_step", "recovery"])
async def test_a_plan_step_or_recovery_task_passes_its_requests_files_when_the_lead_names_none(
    tmp_path: Path, handled: str
) -> None:
    # Turn 2 sent list.png; the lead made a plan, and a step (or a recovery
    # task for a failed step) delegates. Neither carries files of its own:
    # left out, the new task gets the files of the request's own message.
    lead = _Lead(tmp_path)
    task_id = getattr(lead, handled)()

    for model in (ModelAgentTurnDecision, CompatibleModelAgentTurnDecision):
        left_out = await lead.delegate(model=model, from_task=task_id)
        assert _ids(left_out.attachments) == ["att_list"]
        assert _ids(lead.sent_files()) == ["att_list"]
    assert _ids((await lead.delegate([], from_task=task_id)).attachments) == []
    assert _ids((await lead.delegate(["att_photo"], from_task=task_id)).attachments) == ["att_photo"]
    # The request's own task is unchanged: still its message's file.
    assert _ids(lead.manager.get_task_info(ROOT).task.attachments) == ["att_list"]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("attachment_ids", "problem"),
    [
        (["att_unknown"], "No file with attachment_id att_unknown was sent with this request or earlier in its conversation."),
        # The same person's other conversation, and another person's.
        (["att_elsewhere"], "No file with attachment_id att_elsewhere was sent"),
        (["att_bob"], "No file with attachment_id att_bob was sent"),
        # A path is never an id.
        (["../att_photo"], "No file with attachment_id ../att_photo was sent"),
        (["att_photo", "att_gone"], "The file with attachment_id att_gone is no longer stored."),
        ([f"att_{index}" for index in range(21)], "A delegation can pass at most 20 files; 21 were named."),
    ],
)
async def test_ids_the_task_may_not_pass_delegate_nothing_and_show_the_files_it_can_pass(
    tmp_path: Path, attachment_ids: list[str], problem: str, caplog: pytest.LogCaptureFixture
) -> None:
    lead = _Lead(tmp_path)
    lead.turn("task_elsewhere", [lead.file("att_elsewhere", "elsewhere.png")], conversation_id="conv_alice_2")
    lead.turn("task_bob", [lead.file("att_bob", "bob.png")], conversation_id="conv_bob", person=OTHER_PERSON)
    gone = lead.file("att_gone", "gone.png")
    lead.turn("task_turn_0", [gone])
    Path(gone.storage_refs[0].path).unlink()

    with pytest.raises(TurnActionRecoveryScheduled):
        await lead.delegate(attachment_ids)

    # Nothing was created or sent.
    lead.agent.team_comms.send_message.assert_not_awaited()
    assert lead.subtasks() == []
    [record] = lead.manager.execution_state.turn_action_outbox.values()
    assert (record.status, record.recoverability) == ("failed_recoverable", "model_recovery")
    # The model's recovery brief says why and lists the files it can pass, never where they are stored.
    brief = lead.manager.build_turn_action_recovery_brief(record.batch_id)
    assert problem in brief
    if "at most" not in problem:
        assert "Files you can pass:" in brief
        for name in ("list.png (attachment_id att_list", "photo.png (attachment_id att_photo", "notes.txt"):
            assert name in brief
        assert "att_bob" not in brief.split("Files you can pass:")[1]
        assert "att_elsewhere" not in brief.split("Files you can pass:")[1]
    assert str(lead.layout.uploads_dir) not in brief
    assert "event=delegation_attachment_selection_refused" in caplog.text


@pytest.mark.asyncio
@pytest.mark.parametrize("signed_for", [OTHER_PERSON, None])
async def test_a_request_not_signed_for_the_conversations_person_cannot_pass_its_earlier_files(
    tmp_path: Path, signed_for: str | None
) -> None:
    # The request's gateway context names alice, but its signed context is
    # bob's or missing: the conversation is read only for its verified owner.
    lead = _Lead(tmp_path, signed_for=signed_for)

    with pytest.raises(TurnActionRecoveryScheduled):
        await lead.delegate(["att_photo"])

    [record] = lead.manager.execution_state.turn_action_outbox.values()
    brief = lead.manager.build_turn_action_recovery_brief(record.batch_id)
    listed = brief.split("Files you can pass:")[1]
    assert "No file with attachment_id att_photo was sent" in brief
    assert "att_list" in listed and "att_photo" not in listed and "att_notes" not in listed
    # Its own message's file can still be passed.
    lead.agent.team_comms.send_message.reset_mock()
    assert _ids((await lead.delegate(["att_list"])).attachments) == ["att_list"]


def _escalation(attachment_ids: Any = LEFT_OUT) -> dict[str, Any]:
    action: dict[str, Any] = {
        "action_type": "manager_escalation",
        "task": {"problem_statement": "I need the signed contract."},
        "escalation": {"escalation_type": "needs_user_input"},
    }
    if attachment_ids is not LEFT_OUT:
        action["attachment_ids"] = attachment_ids
    return action


@pytest.mark.parametrize("model", [ModelWorkerTurnDecision, CompatibleWorkerTurnDecision])
def test_specialists_turn_answers_have_no_file_choice(model: type) -> None:
    assert "attachment_ids" not in json.dumps(model.model_json_schema())
    assert "attachment_ids" not in json.dumps(convert_pydantic_to_tool_spec(model))
    delegate = {"action_type": "delegate_task", "task": {"problem_statement": "Help."}, "attachment_ids": ["att_list"]}
    for action in (delegate, _escalation(["att_list"]), _escalation([])):
        with pytest.raises(ValidationError):
            model.model_validate({"actions": [action]})
    [escalation] = model.model_validate({"actions": [_escalation()]}).to_runtime_decision().actions
    assert escalation.attachment_ids is None


@pytest.mark.parametrize("model", [ModelAgentTurnDecision, CompatibleModelAgentTurnDecision])
def test_the_leads_turn_answers_offer_an_optional_file_choice_on_delegation_only(model: type) -> None:
    spec = json.dumps(convert_pydantic_to_tool_spec(model))
    assert "attachment_ids" in spec and "Leave out to send the files that came with the request's own message" in spec
    for attachment_ids, expected in ((LEFT_OUT, None), ([], []), (["att_photo"], ["att_photo"])):
        action: dict[str, Any] = {
            "action_type": "delegate_task", "task": {"problem_statement": "Help."}, "target_named_agent_type": SPECIALIST,
        }
        if attachment_ids is not LEFT_OUT:
            action["attachment_ids"] = attachment_ids
        [delegation] = model.model_validate({"actions": [action]}).to_runtime_decision().actions
        assert delegation.attachment_ids == expected
    # Not on any other action.
    with pytest.raises(ValidationError):
        model.model_validate(
            {"actions": [{"action_type": "task_response", "task": {"answer": "Done."}, "attachment_ids": ["att_photo"]}]}
        )


def test_the_leads_recovery_prompts_name_the_file_choice_and_the_specialists_do_not() -> None:
    for prompt in (
        MODEL_AGENT_TURN_DECISION_FORCE_PROMPT,
        FIREWORKS_MODEL_AGENT_TURN_DECISION_FORCE_PROMPT,
        COMPATIBLE_MODEL_AGENT_TURN_DECISION_FORCE_PROMPT,
    ):
        assert "list their ids in `actions[].attachment_ids`; `[]` passes none." in prompt
    for prompt in (MODEL_WORKER_TURN_DECISION_FORCE_PROMPT, COMPATIBLE_WORKER_TURN_DECISION_FORCE_PROMPT):
        assert "attachment_ids" not in prompt


def test_a_saved_delegation_keeps_left_out_apart_from_an_empty_list() -> None:
    # The outbox saves actions with exclude_none and reads them back after a
    # restart; an action saved before the field existed reads as left out.
    adapter = TypeAdapter(TurnAction)
    task = StructuredTaskPayload(task_type=MessageMetaType.REQUEST, problem_statement="Help.")
    for attachment_ids in (None, [], ["att_photo"]):
        saved = DelegateTaskAction(task=task, attachment_ids=attachment_ids).model_dump(mode="json", exclude_none=True)
        assert adapter.validate_python(saved).attachment_ids == attachment_ids
    older = DelegateTaskAction(task=task).model_dump(mode="json", exclude_none=True)
    assert "attachment_ids" not in older and adapter.validate_python(older).attachment_ids is None


@pytest.mark.asyncio
async def test_a_specialists_delegation_naming_files_is_refused(tmp_path: Path) -> None:
    lead = _Lead(tmp_path)
    worker = lead.agent
    worker.named_agent_type = SPECIALIST
    worker.is_team_lead = False
    decision = StructuredAgentTurnDecision(
        actions=[
            DelegateTaskAction(
                task=StructuredTaskPayload(task_type=MessageMetaType.REQUEST, problem_statement="I need the contract."),
                manager_escalation=ManagerEscalationContext(escalation_type="needs_user_input"),
                attachment_ids=["att_list"],
            )
        ]
    )
    info = lead.manager.get_task_info(ROOT)

    with pytest.raises(TurnActionRecoveryScheduled, match="Only TeamArchitect chooses which files"):
        await worker._execute_turn_decision_outbox(
            decision=decision, incoming_content=info.task, incoming_header=info.header, incoming_meta=info.meta
        )
    worker.team_comms.send_message.assert_not_awaited()
