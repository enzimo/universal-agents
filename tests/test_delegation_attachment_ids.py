"""A delegation sends the files the model chooses by id: the task's own when left out, none, or exactly those named.

The model names files only by ``attachment_id`` (``attachment_ids`` on every
model-facing delegation variant); trusted code looks the ids up among the
files the delegating task may use (``conversation_attachments.delegated_files``)
and puts those records on the new task's existing ``Task.attachments``.

Schema tests cover the five model-facing turn decisions (canonical lead and
worker, Fireworks, compatible lead and worker): left out, chosen, empty,
invalid ids, too many, and the field on another action type.

The runtime tests run a real team lead (``UniversalAgent`` with SQLite task
and conversation stores, its admission and steering handlers and its
turn-action outbox) for Ann, with an earlier turn's photo, the request's own
file and a file sent with guidance. Each delegation goes through the outbox:

- left out: the request's own files (its file and the guidance's);
- chosen: exactly the named files in the order named, including an earlier
  turn's photo;
- empty: none;
- an unknown id: no subtask and nothing sent; the abort is recorded and the
  model's recovery shows the unknown id and the files it may use, without
  storage paths;
- a plan step or isolated recovery task names its request's file and an
  earlier photo; left out, it sends its request's own files, as a delegation
  from the request does, also when the request's turn has no file records;
  a step whose request works for someone else sends none of them;
- a plan step of a job run's escalation (the work the lead plans for a
  worker's scheduled run) works on the escalation as on a request: it sends,
  names, lists and opens the run's file and the file Ann sent with an
  answer to an earlier step, which the escalation's result carries to the
  run;
- a worker can pass on only the files it was given, never its request's
  conversation;
- choosing named files (an earlier turn's, or an unknown id with its list of
  choices) reads the conversation store only off the event loop.
"""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from typing import Any

import pytest
import pytest_asyncio
from pydantic import ValidationError

from custom_agents import task_message_runtime
from custom_agents.conversation_attachments import QUESTION_ANSWER_MESSAGE_TYPE, AttachmentSelectionError
from custom_agents.sent_files_runtime import keep_escalation_result_files, result_with_answer_files
from custom_agents.strands_agent import TurnActionRecoveryScheduled
from custom_agents.structured_llm_messages import (
    COMPATIBLE_MODEL_AGENT_TURN_DECISION_FORCE_PROMPT,
    MODEL_AGENT_TURN_DECISION_FORCE_PROMPT,
    MODEL_WORKER_TURN_DECISION_FORCE_PROMPT,
    CompatibleModelAgentTurnDecision,
    CompatibleWorkerTurnDecision,
    DelegateTaskAction,
    FireworksModelAgentTurnDecision,
    ModelAgentTurnDecision,
    ModelManagerEscalationAction,
    ModelWorkerTurnDecision,
    StructuredTaskPayload,
)
from custom_messages.contracts.attachments_v1 import UserTaskAttachment
from custom_messages.contracts.routing_v1 import ManagerEscalationContext, MessageHeader, MessageMeta
from custom_messages.contracts.task_v1 import Task, TaskSteering
from custom_messages.enums import MessageMetaType, ProgressState, RequesterType, TaskHistoryType, TaskSteeringType
from custom_tools.user_attachment_tools import inspect_user_attachment
from runtime_settings.instance_layout import InstanceLayout
from storage.turn_attachments import INVENTORY_LIMIT
from tests.task_work_support import use_agent
from tests.test_conversation_attachment_prompt import _focused_task
from tests.test_conversation_attachments import saved_before_upgrade
from tests.test_conversation_attachments_regression import ANN, CONVERSATION, _admit, _answer, _lead
from tests.test_escalation_answer_files import (
    NOW,
    WORKER_TYPE,
    _deliver_claims,
    _escalate,
    _last_sent,
    _sent,
    _turn,
    _worker,
)
from tests.test_question_answer_files import (
    _answer_the_question_of,
    _ModelTurns,
    _prompt,
    _root_working_for_bob,
    _sections,
)
from tests.test_turn_files_recording import (  # noqa: F401  (autouse fixtures)
    TASK,
    _files_entries,
    _gateway_meta,
    _messages,
    _no_background_work,
    _own,
    _send,
    _upload,
    _verify_keys,
    statuses,
)

pytestmark = pytest.mark.integration

FIRST = "task_first"
DECISIONS = [
    ModelAgentTurnDecision,
    ModelWorkerTurnDecision,
    FireworksModelAgentTurnDecision,
    CompatibleModelAgentTurnDecision,
    CompatibleWorkerTurnDecision,
]
FLAT_DECISIONS = DECISIONS[2:]
_UNSET = object()


# --- The model-facing schemas -----------------------------------------------------------------------


def _decision(decision_type: type, attachment_ids: Any = _UNSET, *, action_type: str = "delegate_task") -> Any:
    action: dict[str, Any] = {"action_type": action_type, "task": {"problem_statement": "Look at the photo."}}
    if action_type == "delegate_task":
        action["target_named_agent_type"] = WORKER_TYPE
    else:
        action["task"] = {"answer": "Done."}
    if attachment_ids is not _UNSET:
        action["attachment_ids"] = attachment_ids
    return decision_type.model_validate({"selected_message_type": "AgentTurnDecision", "actions": [action]})


@pytest.mark.parametrize("decision_type", DECISIONS, ids=lambda item: item.__name__)
@pytest.mark.parametrize(
    ("given", "expected"),
    [
        (_UNSET, None),
        ([" att_photo ", "att_page"], ["att_photo", "att_page"]),
        ([], []),
    ],
    ids=["left_out", "chosen", "empty"],
)
def test_every_delegation_variant_takes_attachment_ids(decision_type: type, given: Any, expected: Any) -> None:
    [action] = _decision(decision_type, given).to_runtime_decision().actions

    assert isinstance(action, DelegateTaskAction)
    assert action.attachment_ids == expected


@pytest.mark.parametrize("decision_type", DECISIONS, ids=lambda item: item.__name__)
@pytest.mark.parametrize(
    ("attachment_ids", "action_type"),
    [
        ([""], "delegate_task"),
        ([f"att_{number}" for number in range(INVENTORY_LIMIT + 1)], "delegate_task"),
        (["att_photo"], "task_response"),
    ],
    ids=["empty_id", "too_many", "not_a_delegation"],
)
def test_attachment_ids_are_refused_when_invalid(decision_type: type, attachment_ids: list[str], action_type: str) -> None:
    with pytest.raises(ValidationError) as refused:
        _decision(decision_type, attachment_ids, action_type=action_type)
    if decision_type in FLAT_DECISIONS and action_type != "delegate_task":
        # The flat action says why before the canonical contract is asked.
        assert "attachment_ids may only be set when action_type is delegate_task" in str(refused.value)


@pytest.mark.parametrize("decision_type", DECISIONS, ids=lambda item: item.__name__)
def test_every_provider_schema_describes_the_three_cases(decision_type: type) -> None:
    schema = decision_type.model_json_schema()
    definitions = schema.get("$defs", {})
    fields = [
        definition["properties"]["attachment_ids"]
        for definition in definitions.values()
        if "attachment_ids" in definition.get("properties", {})
    ]
    assert fields, decision_type.__name__
    for field in fields:
        description = field["description"]
        assert "Leave it out to send this task's own files" in description
        assert "[] sends none" in description and "exactly those files" in description
    if decision_type in FLAT_DECISIONS:
        # Providers get a plain list of strings; the canonical contract checks the rest.
        [field] = fields
        assert field["anyOf"][0] == {"items": {"type": "string"}, "type": "array"}


def test_the_recovery_prompts_name_the_three_cases() -> None:
    for prompt in (
        MODEL_AGENT_TURN_DECISION_FORCE_PROMPT,
        MODEL_WORKER_TURN_DECISION_FORCE_PROMPT,
        COMPATIBLE_MODEL_AGENT_TURN_DECISION_FORCE_PROMPT,
    ):
        assert "Leave `actions[].attachment_ids` out to send this task's own files" in prompt


def test_a_workers_escalation_shortcut_takes_no_attachment_ids() -> None:
    # The shortcut keeps sending the worker task's own files; a worker that must
    # choose uses delegate_task with manager_escalation.
    with pytest.raises(ValidationError):
        ModelManagerEscalationAction.model_validate(
            {
                "action_type": "manager_escalation",
                "task": {"problem_statement": "Ask the person."},
                "escalation": {"escalation_type": "needs_user_input"},
                "attachment_ids": ["att_photo"],
            }
        )


# --- The runtime --------------------------------------------------------------------------------------


@pytest.fixture
def layout(tmp_path: Path) -> InstanceLayout:
    layout = InstanceLayout.at(tmp_path / "instance")
    layout.ensure_directories()
    return layout


class _Files:
    def __init__(self, photo: UserTaskAttachment, page: UserTaskAttachment, page_two: UserTaskAttachment) -> None:
        self.photo, self.page, self.page_two = photo, page, page_two


@pytest_asyncio.fixture
async def lead(tmp_path: Path, layout: InstanceLayout, statuses: Any) -> tuple[Any, _Files]:  # noqa: F811
    """Ann's lead: turn 1 sent a photo; the request (TASK) has a page, and a guidance brought page two."""

    agent = _lead(tmp_path, layout)
    photo = _upload(layout, "garden plant.png")
    await _admit(agent, task_id=FIRST, text="What plant is this?", attachments=[photo])
    await _answer(agent, FIRST, "A young fig tree.")
    page = _upload(layout, "page one.png")
    await _admit(agent, task_id=TASK, text="Ask the cook about this recipe.", attachments=[page])
    page_two = _upload(layout, "page two.png")
    guidance = TaskSteering(
        steering_type=TaskSteeringType.PROACTIVE_GUIDANCE,
        target_task_id=TASK,
        conversation_id=CONVERSATION,
        guidance_text="Here is page two.",
        idempotency_key="key_page_two",
        attachments=[page_two],
    )
    await _send(agent, guidance, _gateway_meta(guidance, task_id=TASK))
    return agent, _Files(photo, page, page_two)


def _delegation(attachment_ids: Any = _UNSET, **fields: Any) -> DelegateTaskAction:
    extra = {} if attachment_ids is _UNSET else {"attachment_ids": attachment_ids}
    return DelegateTaskAction(
        task=StructuredTaskPayload(task_type=MessageMetaType.REQUEST, problem_statement="Look at these files."),
        target_named_agent_type=fields.pop("target", WORKER_TYPE),
        requester_type=RequesterType.AGENT_REQUESTER,
        **extra,
        **fields,
    )


async def _run_turn(agent: Any, task_id: str, actions: list[Any]) -> Any:
    """``task_id``'s turn carries out the model's ``actions`` through the agent's outbox."""

    from custom_agents.structured_llm_messages import StructuredAgentTurnDecision

    manager = agent.task_manager
    manager.update_main_task_state(task_id, ProgressState.IN_PROGRESS, NOW)
    use_agent(agent, task_id)
    info = manager.get_task_info(task_id)
    return await agent._execute_turn_decision_outbox(
        decision=StructuredAgentTurnDecision(actions=actions),
        incoming_content=info.task,
        incoming_header=info.header,
        incoming_meta=info.meta,
    )


def _ids(files: list[UserTaskAttachment]) -> list[str]:
    return [attachment.attachment_id for attachment in files]


@pytest.mark.parametrize("case", ["left_out", "chosen", "empty"])
@pytest.mark.asyncio
async def test_a_delegation_sends_the_files_the_model_chose(
    lead: tuple[Any, _Files], case: str, caplog: pytest.LogCaptureFixture
) -> None:
    agent, files = lead
    chosen = {
        # The request's own files: its file, then the guidance's (C04).
        "left_out": (_UNSET, [files.page, files.page_two], "parent_files"),
        # Exactly these, in the order named, each once; turn 1's photo comes from the conversation.
        "chosen": (
            [files.page_two.attachment_id, files.photo.attachment_id, files.page_two.attachment_id],
            [files.page_two, files.photo],
            "named",
        ),
        "empty": ([], [], "none"),
    }
    attachment_ids, expected, choice = chosen[case]

    with caplog.at_level(logging.INFO):
        await _run_turn(agent, TASK, [_delegation(attachment_ids)])

    delegated = _last_sent(agent, Task).msg_content
    assert delegated.parent_task_id == TASK
    # The records are this runtime's own, storage references included, for the worker to open.
    assert delegated.attachments == expected
    [logged] = _messages(caplog, "delegated_task_files")
    assert f"choice={choice} parent_task_id={TASK} subtask_id={delegated.task_id}" in logged


@pytest.mark.asyncio
async def test_an_unknown_id_fails_the_delegation_and_shows_the_files_it_may_use(
    lead: tuple[Any, _Files], layout: InstanceLayout
) -> None:
    agent, files = lead
    manager = agent.task_manager

    with pytest.raises(TurnActionRecoveryScheduled):
        await _run_turn(agent, TASK, [_delegation(["att_missing", files.photo.attachment_id])])

    # No subtask and nothing sent.
    assert manager.get_subtasks(TASK) == []
    assert not [envelope for _route, envelope in _sent(agent) if isinstance(envelope.msg_content, Task)]
    history = manager.get_task_history_entries(TASK, limit=None)
    [abort] = [entry for entry in history if entry.details.get("event") == "delegation_subtask_registration_aborted"]
    assert abort.details["final_failure_phase"] == "attachment_selection_failed"
    # The model's recovery names the unknown id and the files it may use, newest first, without paths.
    [failure] = [
        entry for entry in history if entry.entry_type == TaskHistoryType.ERROR_EVENT and "needs model recovery" in entry.summary
    ]
    error = failure.details["error"]
    assert "These attachment ids are not files this task may use: att_missing." in error
    listed = [line.split("`")[1] for line in error.splitlines() if line.startswith("- `")]
    assert listed == [files.page_two.attachment_id, files.page.attachment_id, files.photo.attachment_id]
    assert str(layout.uploads_dir) not in error and "storage_refs" not in error


def _store_reads_on_the_event_loop(store: Any) -> list[str]:
    """Record each conversation store read made by a thread running an event loop (the agent's)."""

    on_loop: list[str] = []
    for name in ("list_turn_attachments", "get_thread", "last_turn_without_file_records"):
        original = getattr(store, name)

        def read(*args: Any, __original: Any = original, __name: str = name, **kwargs: Any) -> Any:
            try:
                asyncio.get_running_loop()
                on_loop.append(__name)
            except RuntimeError:
                pass  # a worker thread
            return __original(*args, **kwargs)

        setattr(store, name, read)
    return on_loop


@pytest.mark.parametrize("named", ["earlier_turn", "unknown"])
@pytest.mark.asyncio
async def test_choosing_named_files_never_reads_the_conversation_store_on_the_event_loop(
    lead: tuple[Any, _Files], named: str
) -> None:
    """CONC-1: an earlier turn's id and an unknown id both look the conversation's files up."""

    agent, files = lead
    on_loop = _store_reads_on_the_event_loop(agent._conversation_store)

    if named == "earlier_turn":
        await _run_turn(agent, TASK, [_delegation([files.photo.attachment_id])])
        assert _last_sent(agent, Task).msg_content.attachments == [files.photo]
    else:
        with pytest.raises(TurnActionRecoveryScheduled):
            await _run_turn(agent, TASK, [_delegation(["att_missing"])])

    assert on_loop == []


@pytest.mark.parametrize("flag", ["plan_step_task", "isolated_recovery_task"])
@pytest.mark.asyncio
async def test_a_plan_step_sends_its_requests_files_when_it_leaves_them_out_or_names_them(
    lead: tuple[Any, _Files], layout: InstanceLayout, flag: str, caplog: pytest.LogCaptureFixture
) -> None:
    agent, files = lead
    step = _focused_task(agent, flag, root_id=TASK)
    step_id = step.task.task_id
    # The step asks Ann, and she answers with a file: the step's own, and its request's too.
    object.__setattr__(agent, "process_llm_interaction", _ModelTurns(agent))
    notes = _upload(layout, "step notes.png")
    await _answer_the_question_of(agent, step, [notes])

    await _run_turn(agent, step_id, [_delegation([files.page.attachment_id, files.photo.attachment_id])])
    named = _last_sent(agent, Task).msg_content
    with caplog.at_level(logging.INFO):
        await _run_turn(agent, step_id, [_delegation()])
    left_out = _last_sent(agent, Task).msg_content
    await _run_turn(agent, step_id, [_delegation([])])
    empty = _last_sent(agent, Task).msg_content

    # The step names its request's file and an earlier turn's photo, found through the plan links.
    assert (named.parent_task_id, named.attachments) == (step_id, [files.page, files.photo])
    # Left out, it sends its request's own files in the order they came (the request's file,
    # the guidance's, the answer's), as a delegation from the request does; never the
    # conversation's earlier photo.
    assert (left_out.parent_task_id, left_out.attachments) == (step_id, [files.page, files.page_two, notes])
    [logged] = _messages(caplog, "delegated_task_files")
    assert f"choice=parent_files parent_task_id={step_id}" in logged
    assert (empty.parent_task_id, empty.attachments) == (step_id, [])


@pytest.mark.asyncio
async def test_a_plan_step_sends_its_requests_files_when_the_requests_turn_has_no_records(
    lead: tuple[Any, _Files],
) -> None:
    agent, files = lead
    # The request was admitted before turns kept their files, and is still running.
    saved_before_upgrade(agent._conversation_store, TASK)
    step_id = _focused_task(agent, "plan_step_task", root_id=TASK).task.task_id

    await _run_turn(agent, step_id, [_delegation()])
    left_out = _last_sent(agent, Task).msg_content
    await _run_turn(agent, step_id, [_delegation([files.page.attachment_id])])
    named = _last_sent(agent, Task).msg_content

    assert left_out.attachments == [files.page, files.page_two]
    assert named.attachments == [files.page]


@pytest.mark.asyncio
async def test_a_step_whose_request_works_for_someone_else_sends_none_of_its_files(
    lead: tuple[Any, _Files], monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    agent, files = lead
    step = _focused_task(agent, "plan_step_task", root_id=TASK)
    step_id = step.task.task_id
    # As if its request were Bob's: the step may not use that request's files.
    bob = _root_working_for_bob(agent.task_manager, step, monkeypatch)

    with caplog.at_level(logging.WARNING):
        await _run_turn(agent, step_id, [_delegation()])
        left_out = _last_sent(agent, Task).msg_content
        with pytest.raises(TurnActionRecoveryScheduled):
            await _run_turn(agent, step_id, [_delegation([files.page.attachment_id])])

    assert (left_out.parent_task_id, left_out.attachments) == (step_id, [])
    assert f"event=step_request_files_not_usable task_id={step_id} root_task_id={bob} reason=other_person" in caplog.text


@pytest.mark.asyncio
async def test_a_plan_step_of_a_job_runs_escalation_works_on_the_escalations_files(
    tmp_path: Path, layout: InstanceLayout, caplog: pytest.LogCaptureFixture
) -> None:
    """The lead plans the work a job run's escalation asks for; its steps work on the escalation as on a request.

    Ann's scheduled job run is a top-level task in the worker; it has a photo
    and asks the lead for a specialist, and the escalation brings the run's
    photo. The task manager's links from a step end at the escalation, whose
    parent (the run) the lead never held. Before this change a step could
    neither send nor name the photo (``step_request_files_not_usable
    reason=delegated``), and a later step could not see the file Ann sent
    with an answer to the first, though that file was kept on the escalation.
    """

    lead, runner = _lead(tmp_path, layout), _worker(tmp_path, layout)
    manager = lead.task_manager
    photo = _upload(layout, "fridge.png")
    await _admit(runner, task_id="task_run", text="Plan this week's dinners.", attachments=[photo])
    await _turn(runner, "task_run", [_escalate("needs_specialist_support")])
    sent = _last_sent(runner, Task)
    escalation_id = sent.msg_content.task_id
    await task_message_runtime._handle_task_request(lead, sent.msg_content, sent.msg_header, sent.msg_meta)
    await _deliver_claims(lead, runner)
    escalation = manager.get_task_info(escalation_id)
    assert (escalation.task.parent_task_id, escalation.owner_person_id) == ("task_run", ANN)
    assert escalation.task.attachments == [photo]
    plan = manager.activate_plan_execution(
        escalation_id,
        steps=[{"title": "Look", "objective": "Look in the fridge."}, {"title": "Plan", "objective": "Plan dinners."}],
        final_review=False,
    )
    first = manager.get_task_info(plan.plan_steps[0].current_step_task_id)
    assert manager.resolve_root_task_id(first.task.task_id) == escalation_id

    with caplog.at_level(logging.INFO):
        # Ann answers the first step's question with a file: kept on the step and on the escalation.
        model = _ModelTurns(lead)
        object.__setattr__(lead, "process_llm_interaction", model)
        notes = _upload(layout, "list.png")
        await _answer_the_question_of(lead, first, [notes])
        assert len(_files_entries(manager, first.task.task_id)) == 1
        first.task.answer = "The fridge holds eggs and spinach."
        manager.complete_task(first.task.task_id, NOW)
        second_id = manager.get_plan(plan.plan_id).plan_steps[1].current_step_task_id
        assert manager.is_plan_step_task(second_id)
        # The next step sends the escalation's files when it leaves them out, and names them.
        await _run_turn(lead, second_id, [_delegation()])
        left_out = _last_sent(lead, Task).msg_content
        await _run_turn(lead, second_id, [_delegation([notes.attachment_id, photo.attachment_id])])
        named = _last_sent(lead, Task).msg_content

    assert _messages(caplog, "sent_file_refused") == []
    assert _messages(caplog, "step_request_files_not_usable") == []
    assert _messages(caplog, "sent_files_root_not_usable") == []
    [entry] = _files_entries(manager, escalation_id)
    assert entry.details["message_type"] == QUESTION_ANSWER_MESSAGE_TYPE and first.task.task_id in entry.summary
    # The first step's turn already listed the run's photo as its own.
    [first_prompt] = model.prompts
    assert f"`{photo.attachment_id}` fridge.png" in _sections(first_prompt)["conversation_attachments"]
    assert (left_out.parent_task_id, left_out.attachments) == (second_id, [photo, notes])
    assert named.attachments == [notes, photo]
    # The next step's turn lists both, and its tools open the answer's file by id.
    second = manager.get_task_info(second_id)
    use_agent(lead, second_id)
    listed = _sections(_prompt(lead, second.task, second.header, second.meta))["conversation_attachments"]
    assert f"`{photo.attachment_id}` fridge.png" in listed and f"`{notes.attachment_id}` list.png" in listed
    assert inspect_user_attachment._tool_func(attachment_id=notes.attachment_id)["status"] == "success"
    # The escalation's result carries the answer's file to the run, which keeps it as its own.
    reply = await result_with_answer_files(
        lead,
        Task(task_id=escalation_id, parent_task_id="task_run", task_type=MessageMetaType.RESPONSE, answer="Planned."),
        manager.get_task_info(escalation_id).task,
    )
    assert reply.attachments == [notes]
    await keep_escalation_result_files(runner, runner.task_manager.get_task_info(escalation_id), reply)
    assert _own(runner.task_manager, runner._conversation_store, "task_run") == [photo.attachment_id, notes.attachment_id]


@pytest.mark.asyncio
async def test_a_worker_can_pass_on_only_the_files_it_was_given(tmp_path: Path, layout: InstanceLayout) -> None:
    worker = _worker(tmp_path, layout)
    manager = worker.task_manager
    page, photo = _upload(layout, "page one.png"), _upload(layout, "garden plant.png")
    # The worker's task was given the page; Ann's conversation (in its store too) holds the photo.
    worker._conversation_store.ensure_thread(
        conversation_id=CONVERSATION,
        team_id=None,
        owner_canonical_user_id=ANN,
        owner_agent_id="lead",
        owner_agent_name="Lead",
        requester_name=None,
        requester_id=None,
        root_task_id=FIRST,
        subject_preview="What plant is this?",
    )
    worker._conversation_store.append_turn(
        conversation_id=CONVERSATION,
        task_id=FIRST,
        followup_to_task_id=None,
        owner_agent_id="lead",
        requester_name=None,
        requester_id=None,
        correlation_id=None,
        problem_text="What plant is this?",
        owner_canonical_user_id=ANN,
        attachments=[photo],
    )
    assert manager.add_task(
        Task(
            task_id="task_work",
            parent_task_id=TASK,
            task_type=MessageMetaType.REQUEST,
            problem_statement="Read the recipe.",
            attachments=[page],
        ),
        MessageHeader(from_agent_id="lead", response_routing_key="lead"),
        MessageMeta(conversation_id=CONVERSATION),
        owner_person_id=ANN,
    )
    escalation = ManagerEscalationContext(escalation_type="needs_user_input")

    def escalate(attachment_ids: list[str]) -> DelegateTaskAction:
        return _delegation(attachment_ids, target="TeamArchitect", manager_escalation=escalation)

    await _run_turn(worker, "task_work", [escalate([page.attachment_id])])
    sent = _last_sent(worker, Task).msg_content
    with pytest.raises(TurnActionRecoveryScheduled):
        await _run_turn(worker, "task_work", [escalate([photo.attachment_id])])

    assert sent.attachments == [page]
    [failure] = [
        entry
        for entry in manager.get_task_history_entries("task_work", limit=None)
        if entry.entry_type == TaskHistoryType.ERROR_EVENT and "needs model recovery" in entry.summary
    ]
    # Only its own file is offered: a worker never reads the conversation.
    assert photo.attachment_id in failure.details["error"]
    assert [line.split("`")[1] for line in failure.details["error"].splitlines() if line.startswith("- `")] == [
        page.attachment_id
    ]


@pytest.mark.asyncio
async def test_a_delegated_task_on_the_lead_never_reads_its_requests_conversation(lead: tuple[Any, _Files]) -> None:
    """The lead's record of a worker's escalation runs for the request through the delegation links, but is delegated work."""

    agent, files = lead
    manager = agent.task_manager
    assert manager.add_subtask(
        TASK,
        Task(task_id="task_work", parent_task_id=TASK, task_type=MessageMetaType.REQUEST, problem_statement="Read it."),
        MessageHeader(from_agent_id=agent.id, to_routing_path="worker_1"),
        MessageMeta(conversation_id=CONVERSATION),
    )
    assert manager.add_task(
        Task(task_id="task_escalation", parent_task_id="task_work", task_type=MessageMetaType.REQUEST, problem_statement="Ask."),
        MessageHeader(from_agent_id="worker_1", response_routing_key="worker_1"),
        MessageMeta(conversation_id=CONVERSATION, manager_escalation=ManagerEscalationContext(escalation_type="needs_user_input")),
        owner_person_id=ANN,
    )
    assert manager.resolve_root_task_id("task_escalation") == TASK

    with pytest.raises(TurnActionRecoveryScheduled):
        await _run_turn(agent, "task_escalation", [_delegation([files.photo.attachment_id])])

    [failure] = [
        entry
        for entry in manager.get_task_history_entries("task_escalation", limit=None)
        if entry.entry_type == TaskHistoryType.ERROR_EVENT and "needs model recovery" in entry.summary
    ]
    assert "This task has no files it may use." in failure.details["error"]


def test_the_selection_error_is_a_value_error() -> None:
    # The outbox sends any exception at this phase to model recovery with its text.
    assert issubclass(AttachmentSelectionError, ValueError)
