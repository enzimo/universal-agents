"""Files a person sends with an answer to the team lead's question, at the team lead.

The real team lead (``UniversalAgent``, its model turn replaced) handles the
gateway's answer through ``_handle_task_query_message`` on a real task manager
and conversation store (SQLite, and PostgreSQL when ``LIVE_TEST_POSTGRES_DSN``
is set), with real Ed25519 task contexts. An answer's files are saved with
the request's conversation turn before the answer is recorded, named with
their ids in the question history the model reads and in the turn, and found
by id after a restart and in the next turn; a plan step's, recovery task's
or specialist's escalation's answer goes to its request's turn. Files are kept
only on the person's answer to the task's own question: anything else keeps
its text and loses its files.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock

import pytest

from access_control.identity import ENV_VERIFY_KEYS
from custom_agents.conversation_attachments import attachment_scope, request_files
from custom_agents.conversation_runtime import ensure_task_conversation_context
from custom_agents.conversation_store import ConversationStore
from custom_agents.steering_history_events import QUERY_ANSWER_FILES_KEY
from custom_agents.task_manager import TaskManager
from custom_messages.contracts.attachments_v1 import AttachmentStorageRef, UserTaskAttachment
from custom_messages.contracts.routing_v1 import GatewayContext, MessageHeader, MessageMeta
from custom_messages.contracts.runtime_snapshot_v3 import PlanStepTaskLink, RecoveryTaskLink
from custom_messages.contracts.task_v1 import Task, TaskQuery
from custom_messages.enums import MessageMetaType, RequesterType, TaskHistoryType
from runtime_settings.instance_layout import InstanceLayout
from tests.broker_access_support import context_verify_keys, forget_task_context_keys, signed_task_context
from tests.knowledge_support import BACKENDS, knowledge_storage
from tests.test_turn_action_outbox import _make_agent

TEAM = "test_team"
LEAD = "lead_answer_files"
GATEWAY = "test_team_gateway"
PERSON = "alice"
OTHER_PERSON = "bob"
ROOT = "task_turn_1"
QUESTION = "Which receipt do you mean?"


class _Lead:
    """One instance's uploads folder and stores, and the team lead holding the request ``ROOT``, which asked."""

    def __init__(self, storage, tmp_path: Path) -> None:
        self.storage = storage
        self.tmp_path = tmp_path
        self.layout = InstanceLayout.at(tmp_path / "instance")
        self.layout.ensure_directories()
        self.manager = self.open_manager()
        self.store = ConversationStore(
            db_path=self.manager._db_path,
            artifacts_dir=self.layout.conversation_artifacts_dir,
            storage_settings=storage,
        )
        self.agent = self.start_agent()
        self.conversation_id = self.admit(ROOT, [self.file("att_receipt", "receipt.jpg")])

    def open_manager(self) -> TaskManager:
        manager = TaskManager(storage_settings=self.storage)
        manager.set_owner_identity(LEAD, "Lead", TEAM, "TeamArchitect")
        return manager

    def start_agent(self) -> Any:
        agent = _make_agent(self.tmp_path, task_manager=self.manager)
        agent._conversation_store = self.store
        # The resumed model turn is not what these tests check.
        object.__setattr__(agent, "process_llm_interaction", AsyncMock(return_value=None))
        return agent

    def restart(self) -> None:
        self.manager = self.open_manager()
        assert self.manager.restore_runtime_state()

    def file(self, attachment_id: str, filename: str) -> UserTaskAttachment:
        path = self.layout.uploads_dir / "session_alice" / attachment_id
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"data")
        return UserTaskAttachment(
            attachment_id=attachment_id, filename=filename, content_type="image/jpeg", kind="image",
            size_bytes=4, sha256="0" * 64, storage_refs=[AttachmentStorageRef(storage_type="local", path=str(path))],
        )

    def admit(self, task_id: str, files: list[UserTaskAttachment], conversation_id: str | None = None) -> str:
        """Admit a person's request as the next turn of the conversation, as the lead does."""

        task = Task(task_id=task_id, task_type=MessageMetaType.REQUEST, problem_statement=f"Request {task_id}",
                    attachments=files)
        header = MessageHeader(from_agent_id=GATEWAY, response_routing_key=GATEWAY)
        meta = ensure_task_conversation_context(
            conversation_store=self.store, team_id=TEAM, owner_agent_id=LEAD, owner_agent_name="Lead",
            task_request=task, msg_header=header,
            msg_meta=MessageMeta(conversation_id=conversation_id, msg_correlation_id=f"corr_{task_id}"),
            owner_person_id=PERSON,
        )
        assert self.manager.add_task(task, header, meta, owner_person_id=PERSON)
        return str(meta.conversation_id)

    def task(
        self,
        task_id: str,
        *,
        parent_task_id: str | None = None,
        conversation_id: str | None = None,
        sender: str = LEAD,
        owner: str = PERSON,
    ) -> None:
        assert self.manager.add_task(
            Task(task_id=task_id, task_type=MessageMetaType.REQUEST, problem_statement=f"Work {task_id}",
                 parent_task_id=parent_task_id),
            MessageHeader(from_agent_id=sender, response_routing_key=sender),
            MessageMeta(conversation_id=conversation_id),
            owner_person_id=owner,
        )

    def escalation(self, *, owner: str = PERSON) -> str:
        """A specialist's escalation, as the lead holds it, under the task the lead delegated to it.

        The lead delegated ``task_S`` for ``ROOT``; the specialist's
        ``manager_escalation`` is a delegation to the lead whose parent is the
        specialist's task (``delegation_runtime``), admitted for the person its
        signed context names (``owner``).
        """

        self.task("task_S", parent_task_id=ROOT)
        self.task("task_E", parent_task_id="task_S", sender="worker_1", owner=owner)
        return "task_E"

    def plan_step(self, step_task_id: str) -> None:
        # A plan step is a new top-level task with a fresh conversation id and no files.
        self.task(step_task_id, conversation_id=f"plan_step_conv_{step_task_id}")
        self.manager.plan_state.plan_step_task_links[step_task_id] = PlanStepTaskLink(
            root_task_id=ROOT, plan_id="plan_1", step_id=step_task_id, step_task_id=step_task_id
        )

    def recovery(self, recovery_task_id: str) -> None:
        self.task(recovery_task_id, conversation_id=f"recov_conv_{recovery_task_id}")
        self.manager.plan_state.recovery_links[recovery_task_id] = RecoveryTaskLink(
            original_task_id=ROOT, original_plan_id="plan_1", recovery_task_id=recovery_task_id, branch_id="b",
            failure_type="step_failed", failure_summary="The step failed.",
        )

    def ask(self, task_id: str = ROOT) -> TaskQuery:
        """The lead asks the person on ``task_id``, recorded as its runtime records its own questions."""

        question = TaskQuery(query_type=MessageMetaType.REQUEST, parent_task_id=task_id, message=QUESTION)
        self.manager.add_task_query(task_id, question, agent_name="Lead", agent_id=LEAD)
        return question

    async def receive(
        self,
        query: TaskQuery,
        *,
        sender: str = GATEWAY,
        signed_for: str | None = PERSON,
    ) -> None:
        """The answer arrives as the gateway sends it: signed for the person (``build_gateway_context``)."""

        context = (
            GatewayContext(canonical_user_id=signed_for, task_context=signed_task_context(signed_for, team_id=TEAM))
            if signed_for
            else None
        )
        await self.agent._handle_task_query_message(
            task_query_msg=query,
            incoming_msg_header=MessageHeader(from_agent_id=sender, response_routing_key=sender),
            incoming_msg_meta=MessageMeta(
                msg_correlation_id=f"corr_{query.parent_task_id}", conversation_id=self.conversation_id,
                requester_type=RequesterType.USER_REQUESTER, gateway_context=context,
            ),
            from_runtime_mailbox=True,
        )

    def answer(self, question: TaskQuery, *files: UserTaskAttachment, text: str = "This one.") -> TaskQuery:
        return TaskQuery(
            task_query_id=question.task_query_id, query_type=MessageMetaType.RESPONSE,
            parent_task_id=question.parent_task_id, message=text, attachments=list(files),
        )

    def turn_file_ids(self, task_id: str = ROOT) -> list[str]:
        return [item["attachment_id"] for item in self.store.get_turn_by_task_id(task_id)["attachments"]]

    def scope(self, task_id: str = ROOT):
        request = request_files(self.manager, task_id)
        assert request is not None
        return attachment_scope(request, conversation_store=self.store, is_team_lead=True, person_id=PERSON)

    def answers_recorded(self, task_id: str = ROOT) -> list[TaskQuery]:
        return [query for query in self.manager.get_task_queries(task_id) if query.query_type == MessageMetaType.RESPONSE]

    def answer_history_files(self, task_id: str = ROOT) -> list[list[str]]:
        task = self.manager.get_task_info(task_id).task
        return [
            [item["attachment_id"] for item in entry.details.get(QUERY_ANSWER_FILES_KEY, [])]
            for entry in task.execution_history
            if entry.entry_type == TaskHistoryType.QUERY_EVENT and entry.details.get("query_type") == "response"
        ]


@pytest.fixture(autouse=True)
def _verify_keys(monkeypatch: pytest.MonkeyPatch):
    """The lead trusts the test gateway key, as it trusts the gateway's."""

    monkeypatch.setenv(ENV_VERIFY_KEYS, context_verify_keys())
    forget_task_context_keys()
    yield
    forget_task_context_keys()


@pytest.fixture(params=BACKENDS)
def lead(request, tmp_path: Path):
    with knowledge_storage(request.param, tmp_path) as settings:
        yield _Lead(settings, tmp_path)


def _ids(files) -> list[str]:
    return [getattr(file, "attachment", file).attachment_id for file in files]


@pytest.mark.asyncio
async def test_an_answers_files_are_saved_named_and_found_after_a_restart_and_in_the_next_turn(lead: _Lead) -> None:
    question = lead.ask()

    await lead.receive(lead.answer(question, lead.file("att_answer", "receipt-march.jpg")))

    # Saved with the request's turn, recorded with the answer, and the turn resumed.
    assert lead.turn_file_ids() == ["att_receipt", "att_answer"]
    assert lead.answer_history_files() == [["att_answer"]]
    lead.agent.process_llm_interaction.assert_awaited_once()
    # Named with its id in the question history the model reads, never with its path.
    history = lead.manager.render_task_query_history_markdown(ROOT)
    assert "Files sent with this answer (open one by its attachment_id):" in history
    assert "  - receipt-march.jpg (attachment_id att_answer, image, image/jpeg, 4 bytes)" in history
    assert str(lead.layout.uploads_dir) not in history
    # The conversation turn keeps the answer with its file named.
    clarifications = lead.store.get_turn_by_task_id(ROOT)["clarification_summary_text"]
    assert "response from" in clarifications and "attachment_id att_answer" in clarifications
    assert str(lead.layout.uploads_dir) not in clarifications
    # The request's file now, and after a restart rebuilt from saved state.
    for _ in range(2):
        assert _ids(request_files(lead.manager, ROOT).files) == ["att_receipt", "att_answer"]
        assert lead.scope().find("att_answer").attachment.filename == "receipt-march.jpg"
        lead.restart()
    # The next turn lists it with the turn it came with.
    lead.admit("task_turn_2", [], lead.conversation_id)
    earlier = lead.scope("task_turn_2").files().earlier
    assert [(file.attachment.attachment_id, file.turn_index) for file in earlier] == [
        ("att_answer", 1), ("att_receipt", 1),
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize("link", ["plan_step", "recovery"])
async def test_a_plan_steps_or_recovery_tasks_answer_files_go_to_its_requests_turn(lead: _Lead, link: str) -> None:
    getattr(lead, link)("task_asking")
    question = lead.ask("task_asking")

    await lead.receive(lead.answer(question, lead.file("att_answer", "pantry.jpg")))

    assert lead.turn_file_ids() == ["att_receipt", "att_answer"]
    # The task that asked holds it with its request's files; another step of
    # the same request sees it with the current request (saved with its turn).
    assert _ids(request_files(lead.manager, "task_asking").files) == ["att_receipt", "att_answer"]
    lead.plan_step("task_next_step")
    next_step = lead.scope("task_next_step")
    assert _ids(request_files(lead.manager, "task_next_step").files) == ["att_receipt"]
    assert _ids(next_step.files().current) == ["att_receipt", "att_answer"]
    assert next_step.find("att_answer").attachment.filename == "pantry.jpg"
    # And the next turn lists it as sent in this request's turn.
    lead.admit("task_turn_2", [], lead.conversation_id)
    assert _ids(lead.scope("task_turn_2").files().earlier) == ["att_answer", "att_receipt"]


@pytest.mark.asyncio
async def test_the_persons_answer_on_a_specialists_escalation_keeps_its_files_for_the_lead_and_the_request(
    lead: _Lead,
) -> None:
    escalation = lead.escalation()
    # The lead asks the person while it works on the escalation; the question
    # goes to the person's chat (test_message_routing).
    question = lead.ask(escalation)

    await lead.receive(lead.answer(question, lead.file("att_contract", "contract.pdf")))

    # Recorded with the answer on the escalation and named in its question
    # history, so the lead's resumed turn on it names the file by id.
    [recorded] = lead.answers_recorded(escalation)
    assert _ids(recorded.attachments) == ["att_contract"]
    assert lead.answer_history_files(escalation) == [["att_contract"]]
    assert "contract.pdf (attachment_id att_contract" in lead.manager.render_task_query_history_markdown(escalation)
    lead.agent.process_llm_interaction.assert_awaited_once()
    # Saved with the turn of the request the escalation serves (through the
    # task the lead delegated), so the request and later turns find it.
    assert lead.turn_file_ids() == ["att_receipt", "att_contract"]
    for _ in range(2):
        # The lead opens it on the escalation (the files it may hand to the
        # specialist), and on the request; again after a restart.
        assert _ids(request_files(lead.manager, escalation).files) == ["att_contract"]
        assert lead.scope(escalation).find("att_contract").attachment.filename == "contract.pdf"
        assert _ids(lead.scope().files().current) == ["att_receipt", "att_contract"]
        lead.restart()
    lead.admit("task_turn_2", [], lead.conversation_id)
    assert _ids(lead.scope("task_turn_2").files().earlier) == ["att_contract", "att_receipt"]


@pytest.mark.asyncio
async def test_an_escalation_for_another_persons_request_keeps_its_answers_files_with_the_escalation_only(
    lead: _Lead, caplog: pytest.LogCaptureFixture
) -> None:
    # The escalation names a parent whose request works for someone else.
    escalation = lead.escalation(owner=OTHER_PERSON)
    question = lead.ask(escalation)

    with caplog.at_level(logging.WARNING):
        await lead.receive(lead.answer(question, lead.file("att_contract", "contract.pdf")), signed_for=OTHER_PERSON)

    # The answer is that person's own, for their escalation: kept there, but
    # never saved with the other person's conversation turn.
    assert lead.answer_history_files(escalation) == [["att_contract"]]
    assert _ids(request_files(lead.manager, escalation).files) == ["att_contract"]
    assert lead.turn_file_ids() == ["att_receipt"]
    assert lead.scope().find("att_contract") is None
    assert (
        f"event=answer_files_request_other_person task_id={escalation} request_task_id={ROOT}"
    ) in caplog.text


@pytest.mark.asyncio
async def test_a_failed_save_records_nothing_and_the_answer_sent_again_is_recorded_once(
    lead: _Lead, monkeypatch: pytest.MonkeyPatch
) -> None:
    question = lead.ask()
    answer = lead.answer(question, lead.file("att_answer", "receipt-march.jpg"))
    merge = lead.store.merge_turn_attachments
    failures = [RuntimeError("database is locked")]

    def merge_failing_once(**kwargs):
        if failures:
            raise failures.pop()
        return merge(**kwargs)

    monkeypatch.setattr(lead.store, "merge_turn_attachments", merge_failing_once)

    with pytest.raises(RuntimeError, match="locked"):
        await lead.receive(answer)
    # Nothing recorded and the turn not resumed: the delivery comes again.
    assert lead.answers_recorded() == []
    assert lead.answer_history_files() == []
    assert lead.turn_file_ids() == ["att_receipt"]
    lead.agent.process_llm_interaction.assert_not_awaited()

    await lead.receive(answer)

    assert [recorded.attachments for recorded in lead.answers_recorded()] == [answer.attachments]
    assert lead.answer_history_files() == [["att_answer"]]
    assert lead.turn_file_ids() == ["att_receipt", "att_answer"]
    lead.agent.process_llm_interaction.assert_awaited_once()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("case", "sender", "signed_for", "reason"),
    [
        # An answer to a question this task never asked.
        ("no_question", GATEWAY, PERSON, "no_question"),
        # A worker sends an answer: no signed context.
        ("answer", "worker_1", None, "no_verified_person"),
        # Signed, but for someone other than the person the request works for.
        ("answer", "worker_1", OTHER_PERSON, "other_person"),
        # A question (not an answer) never carries the person's files.
        ("question", "worker_1", PERSON, "not_an_answer"),
        # On a specialist's escalation the same rules hold.
        ("escalation_unsigned", "worker_1", None, "no_verified_person"),
        ("escalation_other_person", GATEWAY, OTHER_PERSON, "other_person"),
    ],
)
async def test_files_that_are_not_the_persons_answer_to_the_tasks_own_question_are_kept_nowhere(
    lead: _Lead, caplog: pytest.LogCaptureFixture, case: str, sender: str, signed_for: str | None, reason: str
) -> None:
    task_id = lead.escalation() if case.startswith("escalation") else ROOT
    question = lead.ask(task_id)
    query = lead.answer(question, lead.file("att_forged", "passport.jpg"), text="Text still counts.")
    if case == "no_question":
        query = query.model_copy(update={"task_query_id": "question_never_asked"})
    elif case == "question":
        query = query.model_copy(update={"query_type": MessageMetaType.REQUEST, "task_query_id": "question_2"})
        # The lead's model answers the question.
        lead.agent.process_llm_interaction.return_value = lead.answer(query)

    with caplog.at_level(logging.WARNING):
        await lead.receive(query, sender=sender, signed_for=signed_for)

    # The text is recorded as usual; the file is nowhere.
    [recorded] = [
        item for item in lead.manager.get_task_queries(task_id)
        if (item.task_query_id, item.query_type) == (query.task_query_id, query.query_type)
    ]
    assert (recorded.message, recorded.attachments) == ("Text still counts.", [])
    assert all(files == [] for files in lead.answer_history_files(task_id))
    assert lead.turn_file_ids() == ["att_receipt"]
    assert _ids(request_files(lead.manager, task_id).files) == ([] if task_id != ROOT else ["att_receipt"])
    assert lead.scope().find("att_forged") is None
    assert "att_forged" not in lead.manager.render_task_query_history_markdown(task_id)
    assert (
        f"event=answer_files_refused task_id={task_id} task_query_id={query.task_query_id} "
        f"query_type={query.query_type.value} sender={sender} reason={reason} attachment_ids=att_forged"
    ) in caplog.text
