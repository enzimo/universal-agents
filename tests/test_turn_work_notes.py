"""A resumed turn continues its task instead of starting it over (custom_agents.turn_work_notes).

Reproduces the 2026-10-19 report: a scheduled worker task fixed knowledge
pages, escalated its findings to the team lead and waited; when the lead
answered, the next turn saw only the original request and ran the whole audit
again. Real TaskManager and DefaultContextEngine; the turn executor's note is
checked with a scripted model.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import fields
from types import SimpleNamespace

import pytest

from custom_agents.context_engine import ContextAssemblyRequest
from custom_agents.prompt_context_sanitization import CONTINUING_REQUEST_LABEL
from custom_agents.sdk.strands.turn import LLMTurnExecutor, _TurnState
from custom_agents.sdk.strands.turn_ports import TurnRuntimeEffects, TurnTaskOperations
from custom_agents.structured_llm_messages import (
    DelegateTaskAction,
    NoOpAction,
    StructuredAgentTurnDecision,
    StructuredTaskPayload,
    TaskQueryAction,
    TaskResponseAction,
)
from custom_agents.task_manager import TaskManager
from custom_agents.task_operation_runtime import TaskOperationRuntime
from custom_agents.turn_work_notes import (
    CONTINUATION_NOTE,
    EARLIER_TURN_WORK_HEADING,
    ENDED_BY_OPEN,
    ENDED_BY_TOOL_LIMIT,
    ENDED_BY_WAIT,
    MAX_INPUT_PREVIEW_CHARS,
    MAX_TOOL_CALLS_PER_NOTE,
    TURN_WORK_EVENT,
    decision_starts_wait,
    leaves_task_open,
    note_details,
    tool_calls_from_messages,
    turn_work_notes,
)
from custom_messages.contracts.routing_v1 import ManagerEscalationContext, MessageHeader, MessageMeta
from custom_messages.contracts.task_v1 import Task, TaskQuery
from custom_messages.enums import MessageMetaType, ProgressState, RequesterType
from custom_messages.records.task_results import TaskResultUpdate
from tests.test_context_engine import _engine

AUDIT = "Weekly knowledge base audit: check every page, fix problems, report what changed."
FINDINGS = "Audit findings: linked the home page to five pages; marked two Discord IDs unverified."


def _now() -> str:
    from datetime import datetime, timezone

    return datetime.now(timezone.utc).isoformat()


def _tool_messages() -> list[dict]:
    """Strands messages of a turn that edited two pages and then answered."""

    return [
        {"role": "user", "content": [{"text": "prompt"}]},
        {
            "role": "assistant",
            "content": [
                {"toolUse": {"toolUseId": "t1", "name": "knowledge_write", "input": {"path": "team/home.md"}}},
                {"toolUse": {"toolUseId": "t2", "name": "knowledge_write", "input": {"path": "team/contacts.md"}}},
            ],
        },
        {
            "role": "user",
            "content": [
                {"toolResult": {"toolUseId": "t1", "status": "success", "content": [{"text": "ok"}]}},
                {"toolResult": {"toolUseId": "t2", "status": "error", "content": [{"text": "conflict"}]}},
            ],
        },
        {
            "role": "assistant",
            "content": [{"toolUse": {"toolUseId": "t3", "name": "AgentTurnDecision", "input": {"actions": []}}}],
        },
    ]


def _escalation(escalation_type: str = "worker_result") -> DelegateTaskAction:
    return DelegateTaskAction(
        task=StructuredTaskPayload(task_type=MessageMetaType.REQUEST, problem_statement=FINDINGS),
        manager_escalation=ManagerEscalationContext(escalation_type=escalation_type),
    )


def _response(answer: str = "done") -> TaskResponseAction:
    return TaskResponseAction(task=StructuredTaskPayload(task_type=MessageMetaType.RESPONSE, answer=answer))


def test_tool_calls_pair_results_skip_the_decision_tool_and_stay_short():
    calls = tool_calls_from_messages(_tool_messages(), exclude_tool_names=("AgentTurnDecision",))

    assert [(call["tool"], call["status"]) for call in calls] == [
        ("knowledge_write", "success"),
        ("knowledge_write", "error"),
    ]
    assert "team/home.md" in calls[0]["input"]

    long_input = [{"role": "assistant", "content": [{"toolUse": {"toolUseId": "x", "name": "shell", "input": "a" * 900}}]}]
    (call,) = tool_calls_from_messages(long_input)
    assert len(call["input"]) == MAX_INPUT_PREVIEW_CHARS
    assert call["status"] == "no_result"


def test_only_turns_that_wait_or_do_not_answer_leave_the_task_open():
    question = TaskQueryAction(
        task_query=TaskQuery(query_type=MessageMetaType.REQUEST, parent_task_id="run_1", message="Which page?")
    )
    escalate_only = StructuredAgentTurnDecision(actions=[_escalation()])
    escalate_and_answer = StructuredAgentTurnDecision(actions=[_escalation(), _response()])
    answer_only = StructuredAgentTurnDecision(actions=[_response()])
    ask = StructuredAgentTurnDecision(actions=[question])
    noop = StructuredAgentTurnDecision(actions=[NoOpAction()])

    assert decision_starts_wait(escalate_only) and leaves_task_open(escalate_only)
    # The final answer is held back while the escalation is pending.
    assert decision_starts_wait(escalate_and_answer) and leaves_task_open(escalate_and_answer)
    assert decision_starts_wait(ask) and leaves_task_open(ask)
    assert not decision_starts_wait(answer_only) and not leaves_task_open(answer_only)
    assert not decision_starts_wait(noop) and leaves_task_open(noop)


def test_note_counts_tool_calls_past_its_bound():
    calls = [{"tool": f"t{index}", "input": "", "status": "success"} for index in range(MAX_TOOL_CALLS_PER_NOTE + 3)]

    details = note_details(tool_calls=calls, sent=[], started_wait=False, ended_by=ENDED_BY_TOOL_LIMIT)

    assert len(details["tool_calls"]) == MAX_TOOL_CALLS_PER_NOTE
    assert details["tool_calls_not_shown"] == 3


def test_the_section_keeps_the_newest_turns_within_its_bound():
    from custom_messages.contracts.task_v1 import TaskHistoryEntry
    from custom_agents.turn_work_notes import MAX_SECTION_CHARS, render_earlier_turn_work

    big = [{"tool": "shell", "input": "x" * MAX_INPUT_PREVIEW_CHARS, "status": "success"}] * MAX_TOOL_CALLS_PER_NOTE
    notes = [
        TaskHistoryEntry(
            summary="turn",
            details=note_details(tool_calls=big, sent=[f"marker_{index}"], started_wait=True, ended_by=ENDED_BY_WAIT),
        )
        for index in range(8)
    ]

    rendered = render_earlier_turn_work(notes)

    assert len(rendered) <= MAX_SECTION_CHARS + 500
    assert "marker_7" in rendered and "marker_0" not in rendered
    assert "older turn(s) not shown." in rendered


def _worker_task_manager(tmp_path) -> TaskManager:
    manager = TaskManager(db_path=str(tmp_path / "worker.db"))
    manager.set_owner_identity("worker", "worker", "team", "KnowledgeCurator")
    manager.add_task(
        Task(task_id="run_1", task_type=MessageMetaType.REQUEST, problem_statement=AUDIT),
        MessageHeader(from_agent_id="scheduler"),
        MessageMeta(requester_type=RequesterType.TIMER_REQUESTER, is_response_requested=False),
        owner_person_id="person_1",
    )
    manager.update_main_task_state("run_1", ProgressState.IN_PROGRESS, _now())
    return manager


def _prompt(manager: TaskManager) -> str:
    info = manager.get_task_info("run_1")
    engine = _engine(task_manager=manager, context_window_tokens=200000)
    return engine.assemble(
        ContextAssemblyRequest(
            incoming_content=info.task,
            incoming_header=info.header,
            incoming_meta=info.meta,
            identity_meta=info.meta,
            execution_task_id="run_1",
            incoming_message_json=info.task.model_dump_json(),
        )
    ).prompt


def test_resumed_turn_after_an_answered_escalation_sees_the_work_already_done(tmp_path):
    manager = _worker_task_manager(tmp_path)
    first_prompt = _prompt(manager)
    assert EARLIER_TURN_WORK_HEADING not in first_prompt
    assert "Current Request:\n" + AUDIT in first_prompt

    # Turn 1 edited pages, escalated its findings and waited.
    decision = StructuredAgentTurnDecision(actions=[_escalation()])
    from custom_agents.turn_work_notes import sent_items

    manager.record_turn_work(
        "run_1",
        note_details(
            tool_calls=tool_calls_from_messages(_tool_messages(), exclude_tool_names=("AgentTurnDecision",)),
            sent=sent_items(decision),
            started_wait=True,
            ended_by=ENDED_BY_WAIT,
        ),
        agent_name="worker",
        agent_id="worker",
    )
    manager.add_subtask(
        "run_1",
        Task(task_id="esc_1", parent_task_id="run_1", task_type=MessageMetaType.REQUEST, problem_statement=FINDINGS),
        MessageHeader(from_agent_id="worker", to_routing_path="team_TeamArchitect"),
        MessageMeta(to_named_agent_type="TeamArchitect", requester_type=RequesterType.AGENT_REQUESTER),
    )
    manager.update_main_task_state("run_1", ProgressState.WAITING, _now())
    # The lead answers; the task resumes.
    manager.update_task_result("esc_1", TaskResultUpdate(answer="Thanks, I told the person."))
    manager.update_main_task_state("esc_1", ProgressState.COMPLETED, _now())
    manager.update_main_task_state("run_1", ProgressState.IN_PROGRESS, _now())

    resumed = _prompt(manager)

    assert EARLIER_TURN_WORK_HEADING in resumed
    assert CONTINUATION_NOTE in resumed
    assert "called `knowledge_write` (success)" in resumed
    assert "called `knowledge_write` (error)" in resumed
    assert "manager escalation (worker_result) to TeamArchitect: " + FINDINGS in resumed
    assert CONTINUING_REQUEST_LABEL + "\n" + AUDIT in resumed
    assert "Current Request:\n" not in resumed
    # The note survives a reload of the saved task.
    reloaded = TaskManager(db_path=str(tmp_path / "worker.db"))
    reloaded.set_owner_identity("worker", "worker", "team", "KnowledgeCurator")
    reloaded.restore_runtime_state()
    assert [entry.details["event"] for entry in turn_work_notes(reloaded.get_task("run_1"))] == [TURN_WORK_EVENT]


def _executor(task_manager: TaskManager, messages: list[dict]) -> LLMTurnExecutor:
    def unexpected(name):
        def fail(*args, **kwargs):
            raise AssertionError(f"Undeclared test interaction: {name}")

        return fail

    tasks = {item.name: unexpected(item.name) for item in fields(TurnTaskOperations)}
    tasks.update(
        runtime=TaskOperationRuntime(),
        objective_work_coordinator=None,
        record_turn_work=task_manager.record_turn_work,
    )
    effects = {item.name: unexpected(item.name) for item in fields(TurnRuntimeEffects)}
    return LLMTurnExecutor(
        settings=lambda: SimpleNamespace(id="worker", name="worker"),
        model=lambda: SimpleNamespace(messages=messages),
        context=lambda: None,
        debug_dump=lambda: None,
        logger=logging.getLogger("turn-work-notes-test"),
        tasks=TurnTaskOperations(**tasks),
        effects=TurnRuntimeEffects(**effects),
    )


def _turn(task_manager: TaskManager) -> _TurnState:
    info = task_manager.get_task_info("run_1")
    turn = _TurnState(msg_content=info.task, msg_header=info.header, msg_meta=info.meta)
    turn.active_task_id = "run_1"
    turn.turn_decision_tool_name = "AgentTurnDecision"
    return turn


@pytest.mark.parametrize(
    ("decision", "ended_by", "saved"),
    [
        (StructuredAgentTurnDecision(actions=[_escalation("needs_user_input")]), ENDED_BY_WAIT, True),
        (StructuredAgentTurnDecision(actions=[NoOpAction()]), ENDED_BY_OPEN, True),
        (StructuredAgentTurnDecision(actions=[_response()]), None, False),
        (None, ENDED_BY_TOOL_LIMIT, True),
    ],
)
def test_turn_executor_saves_the_note_only_when_the_task_stays_open(tmp_path, decision, ended_by, saved):
    manager = _worker_task_manager(tmp_path)
    executor = _executor(manager, _tool_messages())

    asyncio.run(
        executor._record_turn_work(
            _turn(manager), decision=decision, ended_by=ENDED_BY_TOOL_LIMIT if decision is None else None
        )
    )

    notes = turn_work_notes(manager.get_task("run_1"))
    assert bool(notes) is saved
    if saved:
        (note,) = notes
        assert note.details["ended_by"] == ended_by
        assert [call["tool"] for call in note.details["tool_calls"]] == ["knowledge_write", "knowledge_write"]


def test_turn_that_did_nothing_saves_no_note(tmp_path):
    manager = _worker_task_manager(tmp_path)
    executor = _executor(manager, [])

    asyncio.run(executor._record_turn_work(_turn(manager), decision=StructuredAgentTurnDecision(actions=[NoOpAction()])))

    assert turn_work_notes(manager.get_task("run_1")) == []
