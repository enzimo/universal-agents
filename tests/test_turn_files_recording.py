"""Files sent with a request, a guidance or an answer are kept for the task and its conversation.

Uses the real admission path, the real steering handler, real SQLite task and
conversation stores and the gateway's own upload storage, for a team lead
working for Ann (signed task context). Covers:

- the request's own files are kept on its conversation turn at admission,
  and the task keeps the same records (the turn is marked as not knowing
  them all when any was dropped);
- guidance and answer files are added to the turn and recorded in the task's
  history before the message is acknowledged, named by id in the text the
  model reads, and stay the task's own files after the guidance is read and
  after a restart;
- records pointing outside this host's upload folders are never saved;
- a store error leaves the message unacknowledged, and the resend records
  each file once;
- only files the gateway sent with that message for this task are kept: a
  faulty agent process holding a worker's copy of the task's signed context,
  a context signed for another request, or a guidance's context that every
  agent process received (sent again with other files, under a new key or
  under the same key after a restart) gets none of its files onto the task,
  its turn or the model's text, at admission and on guidance and answers;
- a request the gateway signed before the update (its context names no task)
  is admitted without its files, logged with its own reason and advice; a
  guidance or interrupt answer under the gateway's status-check context
  (which names no task either) keeps none, without that advice;
- a delegated task keeps no files from guidance, and the lead sends guidance
  on without files, saying how many came with it;
- an answer refused as stale keeps none of its files, and a guidance or an
  answer refused after its files were kept is logged with those files, also
  when an earlier delivery kept them and the resend is refused (task ended,
  mid-turn steering turned off, no live session after a restart), while a
  late resend of a message the task did take is not reported as lost;
- a guidance restored from before the update names only files the task can
  open and says how many others came with it;
- the views that show the task's history to models (the history window, an
  isolated recovery's context and the turn summary prompt) name kept files
  without where their bytes are, while the stored records keep it.
"""

from __future__ import annotations

import asyncio
import json
import logging
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
import pytest_asyncio

from access_control.identity import ENV_VERIFY_KEYS
from assistant_gateway.inbound_attachments import (
    InboundAttachmentStoragePolicy,
    store_inbound_attachment_bytes_with_policy,
)
from access_control.ownership import steering_context_id
from custom_agents import sent_files_runtime, task_message_runtime, task_steering_runtime
from custom_agents.conversation_attachments import (
    TASK_FILES_EVENT,
    TaskAttachments,
    UploadFolders,
    admit_request_files,
    task_files_details,
)
from custom_agents.conversation_store import ConversationStore
from custom_agents.conversation_summary import ConversationSummaryConfig, LLMTurnSummaryGenerator, TurnSummaryRequest
from custom_agents.task_manager import TaskManager
from custom_agents.task_steering_runtime import (
    _build_interrupt_response_blocks,
    consume_pending_proactive_guidance_message,
    handle_task_steering_message,
)
from custom_messages.contracts.attachments_v1 import AttachmentStorageRef, UserTaskAttachment
from custom_messages.contracts.routing_v1 import GatewayContext, MessageHeader, MessageMeta
from custom_messages.contracts.task_v1 import Task, TaskHistoryEntry, TaskSteering
from custom_messages.enums import (
    MessageMetaType,
    ProgressState,
    RequesterType,
    TaskHistoryImportance,
    TaskSteeringStatus,
    TaskSteeringType,
)
from custom_tools.providers.diagnostics import get_recovery_context
from custom_tools.providers.heartbeat import get_task_history_window
from runtime_settings.instance_layout import InstanceLayout
from tests.broker_access_support import context_signer, context_verify_keys, forget_task_context_keys
from tests.task_work_support import use_agent
from tests.test_conversation_attachments_regression import ANN, CONVERSATION, SESSION, TEAM, _admit, _lead
from tests.test_conversation_summary import _RecordingSummaryModel

pytestmark = pytest.mark.integration

TASK = "task_recipe"
OUTSIDE = "att_outside"
ATTACHMENTS_LOGGER = "custom_agents.conversation_attachments"
RUNTIME_LOGGER = "custom_agents.conversation_runtime"


@pytest.fixture(autouse=True)
def _verify_keys(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv(ENV_VERIFY_KEYS, context_verify_keys())
    forget_task_context_keys()
    yield
    forget_task_context_keys()


@pytest.fixture(autouse=True)
def _no_background_work(monkeypatch: pytest.MonkeyPatch):
    # Each test plays the turn itself; admission and accepted answers would
    # otherwise start the queue drain that runs a model turn.
    monkeypatch.setattr(task_message_runtime, "schedule_pending_work", lambda _agent: None)
    monkeypatch.setattr(task_steering_runtime, "schedule_pending_work", lambda _agent: None)


@pytest.fixture
def statuses(monkeypatch: pytest.MonkeyPatch) -> list[tuple[str, TaskSteeringStatus, str]]:
    """Every steering status the handler sends back: (steering id, status, reason)."""

    sent: list[tuple[str, TaskSteeringStatus, str]] = []

    async def record(_agent, incoming, _header, _meta, *, status, reason, status_code=None):
        sent.append((incoming.steering_id, status, reason))

    monkeypatch.setattr(task_steering_runtime, "_send_steering_status_response", record)
    return sent


@pytest.fixture
def layout(tmp_path: Path) -> InstanceLayout:
    layout = InstanceLayout.at(tmp_path / "instance")
    layout.ensure_directories()
    return layout


def _upload(
    layout: InstanceLayout, name: str, *, session_key: str = SESSION, conversation_id: str = CONVERSATION
) -> UserTaskAttachment:
    """A file the gateway stored for a session (Ann's unless named), with its record."""

    return store_inbound_attachment_bytes_with_policy(
        data=b"\x89PNG\r\n\x1a\n" + name.encode(),
        filename=name,
        content_type="image/png",
        policy=InboundAttachmentStoragePolicy(cache_dir=layout.uploads_dir),
        session_key=session_key,
        conversation_id=conversation_id,
        origin="web_chat",
    ).attachment


def _outside(layout: InstanceLayout) -> UserTaskAttachment:
    """A record naming a file outside the upload folders, as a faulty sender could."""

    return UserTaskAttachment(
        attachment_id=OUTSIDE,
        filename="tasks.db",
        content_type="application/octet-stream",
        kind="document",
        size_bytes=1,
        sha256="0" * 64,
        storage_refs=[AttachmentStorageRef(storage_type="local", path=str(layout.root / "state" / "tasks.db"))],
    )


@pytest_asyncio.fixture
async def lead(tmp_path: Path, layout: InstanceLayout):
    """Ann's team lead with one admitted request (TASK) in CONVERSATION."""

    agent = _lead(tmp_path, layout)
    await _admit(agent, task_id=TASK, text="Read this recipe.", attachments=[_upload(layout, "page one.png")])
    return agent


def _steering(kind: TaskSteeringType, attachments: list[UserTaskAttachment], key: str, text: str) -> TaskSteering:
    return TaskSteering(
        steering_type=kind,
        target_task_id=TASK,
        conversation_id=CONVERSATION,
        guidance_text=text,
        idempotency_key=key,
        attachments=attachments,
    )


def _gateway_meta(
    steering: TaskSteering, *, task_id: str = TASK, conversation_id: str | None = CONVERSATION, person: str = ANN
) -> MessageMeta:
    """Message meta as the gateway sends ``steering``: a fresh context naming the task and conversation,
    with the id bound to this message and its files."""

    signed = context_signer().issue(
        team_id=TEAM,
        person_id=person,
        person_roles=("member",),
        root_task_id=task_id,
        conversation_id=conversation_id,
        context_id=steering_context_id(steering),
    )
    return MessageMeta(
        conversation_id=conversation_id,
        is_response_requested=True,
        gateway_context=GatewayContext(canonical_user_id=person, session_key=SESSION, task_context=signed),
    )


async def _send(agent, steering: TaskSteering, meta: MessageMeta | None = None) -> None:
    """Deliver ``steering`` as the gateway would, or with ``meta`` as another sender would."""

    await handle_task_steering_message(
        agent, steering, MessageHeader(from_agent_id="gateway"), meta or _gateway_meta(steering)
    )


def _turn_files(agent) -> list[str]:
    return [item["attachment_id"] for item in agent._conversation_store.get_turn_by_task_id(TASK)["attachments"]]


def _files_entries(task_manager: TaskManager, task_id: str = TASK) -> list[Any]:
    history = task_manager.get_task_info(task_id).task.execution_history
    return [entry for entry in history if entry.details.get("event") == TASK_FILES_EVENT]


def _own(task_manager: TaskManager, store: Any, task_id: str = TASK) -> list[str]:
    scope = TaskAttachments.load(task_manager.get_task_info(task_id), store)
    return [attachment.attachment_id for attachment in scope.own]


def _restarted(agent, tmp_path: Path) -> tuple[TaskManager, ConversationStore]:
    """The task and conversation stores as a restarted lead opens them."""

    manager = TaskManager(db_path=agent.task_manager._db_path)
    manager.set_owner_identity("lead", "Lead", TEAM, "TeamArchitect")
    assert manager.restore_runtime_state() is not None
    store = ConversationStore(agent.task_manager._db_path, artifacts_dir=tmp_path / "conversation_artifacts")
    return manager, store


def _messages(caplog: pytest.LogCaptureFixture, event: str) -> list[str]:
    return [record.getMessage() for record in caplog.records if f"event={event}" in record.getMessage()]


# --- The request's own files -------------------------------------------------------------------


@pytest.mark.asyncio
async def test_admission_keeps_the_requests_files_but_not_records_outside_the_upload_folders(
    tmp_path: Path, layout: InstanceLayout, caplog: pytest.LogCaptureFixture
) -> None:
    agent = _lead(tmp_path, layout)
    page = _upload(layout, "page one.png")

    with caplog.at_level(logging.WARNING, logger=ATTACHMENTS_LOGGER):
        await _admit(agent, task_id=TASK, text="Read this recipe.", attachments=[page, _outside(layout)])

    assert _turn_files(agent) == [page.attachment_id]
    assert agent.task_manager.get_task_info(TASK).task.attachments == [page]  # the task keeps what its turn keeps
    assert agent._conversation_store.get_turn_by_task_id(TASK)["attachments_recorded"] is False
    listed = agent._conversation_store.list_turn_attachments(CONVERSATION)
    assert [entry.attachment for entry in listed] == [page]
    assert _messages(caplog, "attachment_outside_upload_folders")[0].endswith(
        f"attachment_id={OUTSIDE} task_id={TASK} conversation_id={CONVERSATION}"
    )


# --- Guidance ----------------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_guidance_files_are_kept_before_it_is_acknowledged_and_survive_reading_and_restart(
    lead, layout: InstanceLayout, tmp_path: Path, statuses, caplog: pytest.LogCaptureFixture
) -> None:
    page_two = _upload(layout, "page two.png")
    steering = _steering(TaskSteeringType.PROACTIVE_GUIDANCE, [page_two, _outside(layout)], "guide_1", "Here is page two.")

    with caplog.at_level(logging.INFO):
        await _send(lead, steering)

    assert [status for _id, status, _reason in statuses] == [TaskSteeringStatus.ACCEPTED]
    first_page = lead.task_manager.get_task_info(TASK).task.attachments[0].attachment_id
    assert _turn_files(lead) == [first_page, page_two.attachment_id]
    [entry] = _files_entries(lead.task_manager)
    assert entry.details["message_id"] == steering.steering_id
    assert [item["attachment_id"] for item in entry.details["task_files"]] == [page_two.attachment_id]
    assert _own(lead.task_manager, lead._conversation_store) == [first_page, page_two.attachment_id]
    # The record outside the upload folders is dropped everywhere, with a warning.
    assert lead._pending_proactive_task_steering[TASK][0].attachments == [page_two]
    assert _messages(caplog, "attachment_outside_upload_folders")
    assert _messages(caplog, "conversation_turn_attachments_merged")

    # The model reads the guidance with its files named by id, never by path.
    text = await consume_pending_proactive_guidance_message(lead, TASK)
    assert "Here is page two." in text
    assert f"`{page_two.attachment_id}` page two.png (image/png, image," in text
    assert OUTSIDE not in text and str(layout.root) not in text

    manager, store = _restarted(lead, tmp_path)
    assert _own(manager, store) == [first_page, page_two.attachment_id]
    assert [entry.attachment.attachment_id for entry in store.list_turn_attachments(CONVERSATION)] == [
        page_two.attachment_id,
        first_page,
    ]


@pytest.mark.asyncio
async def test_a_store_error_leaves_the_guidance_unacknowledged_and_the_resend_records_each_file_once(
    lead, layout: InstanceLayout, statuses
) -> None:
    page_two = _upload(layout, "page two.png")
    steering = _steering(TaskSteeringType.PROACTIVE_GUIDANCE, [page_two], "guide_retry", "Here is page two.")
    store = lead._conversation_store
    failures = iter([OSError("database is locked")])

    def merge_once_failing(**kwargs):
        error = next(failures, None)
        if error is not None:
            raise error
        return store.merge_turn_attachments(**kwargs)

    lead._conversation_store = SimpleNamespace(merge_turn_attachments=merge_once_failing)
    with pytest.raises(OSError):
        await _send(lead, steering)

    assert statuses == []
    assert "guide_retry" not in lead._processed_task_steering_ids
    # The task's files entry is written first and decides; the merge failed after it.
    assert len(_files_entries(lead.task_manager)) == 1
    assert TASK not in lead._pending_proactive_task_steering

    # The gateway sends its held copy again.
    await _send(lead, steering)
    await _send(lead, steering)

    assert [status for _id, status, _reason in statuses] == [TaskSteeringStatus.ACCEPTED, TaskSteeringStatus.ACCEPTED]
    assert statuses[1][2] == "Duplicate steering event ignored."
    # The resend writes its entry again; the task's files and its turn have the file once.
    assert len(_files_entries(lead.task_manager)) == 2
    lead._conversation_store = store
    assert _own(lead.task_manager, store).count(page_two.attachment_id) == 1
    assert _turn_files(lead).count(page_two.attachment_id) == 1


@pytest.mark.asyncio
async def test_a_delegated_task_keeps_no_files_from_guidance(
    tmp_path: Path, layout: InstanceLayout, statuses, caplog: pytest.LogCaptureFixture
) -> None:
    worker = _lead(tmp_path, layout)
    worker.task_manager.add_task(
        Task(task_id="task_child", parent_task_id="task_parent", task_type=MessageMetaType.REQUEST, problem_statement="Parse it."),
        MessageHeader(from_agent_id="lead"),
        MessageMeta(conversation_id=CONVERSATION),
        owner_person_id=ANN,
    )
    merges: list[Any] = []
    worker._conversation_store = SimpleNamespace(merge_turn_attachments=lambda **kwargs: merges.append(kwargs))
    page_two = _upload(layout, "page two.png")
    forwarded = _steering(TaskSteeringType.PROACTIVE_GUIDANCE, [page_two], "guide_fwd", "Use page two too.")
    forwarded.target_task_id = "task_child"

    # Forwarded guidance carries no gateway context; even the task's own copied one would not do.
    with caplog.at_level(logging.WARNING, logger=ATTACHMENTS_LOGGER):
        await _send(worker, forwarded, MessageMeta(conversation_id=CONVERSATION, is_response_requested=True))

    assert [status for _id, status, _reason in statuses] == [TaskSteeringStatus.ACCEPTED]
    assert merges == []
    assert _files_entries(worker.task_manager, "task_child") == []
    assert _own(worker.task_manager, None, "task_child") == []
    assert worker._pending_proactive_task_steering["task_child"][0].attachments == []
    [refused] = _messages(caplog, "sent_file_refused")
    assert f"reason=delegated_task attachment_id={page_two.attachment_id} task_id=task_child" in refused


@pytest.mark.asyncio
async def test_the_lead_sends_guidance_on_to_its_workers_without_files(lead, layout: InstanceLayout, monkeypatch) -> None:
    child = SimpleNamespace(
        task=SimpleNamespace(task_id="task_child"),
        meta=SimpleNamespace(to_named_agent_type="RecipeParser", to_agent_name=None, msg_correlation_id="corr_child"),
    )
    monkeypatch.setattr(lead.task_manager, "get_pending_subtasks", lambda _task_id: [child])
    sent: list[Any] = []

    async def capture(envelope):
        sent.append(envelope)

    monkeypatch.setattr(lead, "_send_operational_message", capture)
    page_two = _upload(layout, "page two.png")

    await _send(lead, _steering(TaskSteeringType.PROACTIVE_GUIDANCE, [page_two], "guide_on", "Use page two too."))
    await _send(lead, _steering(TaskSteeringType.PROACTIVE_GUIDANCE, [], "guide_plain", "And keep it short."))

    with_file, without = sent
    assert with_file.msg_content.target_task_id == "task_child"
    # The worker learns a file came with it and who has it (review GEN-5), but gets none.
    assert with_file.msg_content.guidance_text == (
        "Use page two too.\n\n1 file(s) came with this guidance; the team lead has them."
    )
    assert with_file.msg_content.attachments == []
    assert without.msg_content.guidance_text == "And keep it short."
    # The lead itself keeps the file; it can name it in a new delegation.
    assert page_two.attachment_id in _own(lead.task_manager, lead._conversation_store)


# --- Answers -----------------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_answer_files_without_a_conversation_record_stay_the_tasks_own_after_restart(
    lead, layout: InstanceLayout, tmp_path: Path, statuses
) -> None:
    # No conversation store: the task's history is the only place the answer's files are kept.
    lead._conversation_store = None
    photo = _upload(layout, "pan.png")
    answer = _steering(TaskSteeringType.INTERRUPT_RESPONSE, [photo, _outside(layout)], "answer_1", "This pan.")

    await _send(lead, answer)

    assert [status for _id, status, _reason in statuses] == [TaskSteeringStatus.ACCEPTED]
    assert statuses[0][2] == "Interrupt response accepted as durable recovery guidance."
    [entry] = _files_entries(lead.task_manager)
    assert entry.details["message_type"] == TaskSteeringType.INTERRUPT_RESPONSE.value
    # The restart path's resume note names the file by id.
    directive = lead.task_manager.read_runtime_resume_directive(TASK)
    assert "This pan." in directive and f"`{photo.attachment_id}` pan.png" in directive
    assert OUTSIDE not in directive and str(layout.root) not in directive

    manager, _store = _restarted(lead, tmp_path)
    own = _own(manager, None)
    assert photo.attachment_id in own and OUTSIDE not in own


@pytest.mark.asyncio
async def test_a_live_answer_names_its_files_and_adds_them_to_the_turn(lead, layout: InstanceLayout, statuses) -> None:
    session = SimpleNamespace(interrupts=[SimpleNamespace(interrupt_id="interrupt_1")])
    lead._interrupted_turn_sessions[TASK] = session
    photo = _upload(layout, "pan.png")
    answer = _steering(TaskSteeringType.INTERRUPT_RESPONSE, [photo], "answer_live", "This pan.")

    await _send(lead, answer)

    assert statuses[0][1:] == (TaskSteeringStatus.ACCEPTED, "Interrupt response accepted for live Strands resume.")
    assert photo.attachment_id in _turn_files(lead)
    assert len(_files_entries(lead.task_manager)) == 1
    [block] = _build_interrupt_response_blocks(session, lead._pending_interrupt_responses[TASK])
    response = block["interruptResponse"]["response"]
    assert response.startswith("This pan.")
    assert f"`{photo.attachment_id}` pan.png" in response and "attachment_id" in response


# --- Only files the gateway sent for the task are kept ------------------------------------------

BOB = "usr_bob"
OTHER_CONVERSATION = "conv_other"


def _copied_task_context(agent) -> MessageMeta:
    """What every worker of TASK holds: delegation deep-copies the task's admitted gateway context
    into each subtask's meta (``delegation_runtime._create_subtask_request``). Its signature is valid
    for Ann and it names TASK and CONVERSATION."""

    admitted = agent.task_manager.get_task_info(TASK).meta.gateway_context
    assert admitted.task_context.context.root_task_id == TASK
    return MessageMeta(
        conversation_id=CONVERSATION, is_response_requested=True, gateway_context=admitted.model_copy(deep=True)
    )


# Senders a faulty agent process could use, and why each is refused.
_FORGED_SENDERS = {
    "worker_copy_of_task_context": ("context_not_for_message", lambda agent, _steering: _copied_task_context(agent)),
    # Even bound to this very message: the gateway signed it for Ann's other request.
    "context_of_anns_other_request": (
        "another_task",
        lambda _agent, steering: _gateway_meta(steering, task_id="task_other", conversation_id=OTHER_CONVERSATION),
    ),
    "no_context": (
        "gateway_context_required",
        lambda _agent, _steering: MessageMeta(conversation_id=CONVERSATION, is_response_requested=True),
    ),
}


def _foreign_files(layout: InstanceLayout) -> list[UserTaskAttachment]:
    """(i) Bob's upload in Bob's session folder; (ii) Ann's upload from another conversation of the same session."""

    return [
        _upload(layout, "bobs scan.png", session_key="web:bob-session", conversation_id="conv_bob"),
        _upload(layout, "anns other.png", conversation_id=OTHER_CONVERSATION),
    ]


async def _model_text(agent, kind: TaskSteeringType) -> str:
    if kind == TaskSteeringType.PROACTIVE_GUIDANCE:
        return await consume_pending_proactive_guidance_message(agent, TASK)
    return agent.task_manager.read_runtime_resume_directive(TASK)


@pytest.mark.parametrize("sender", sorted(_FORGED_SENDERS))
@pytest.mark.parametrize("kind", [TaskSteeringType.PROACTIVE_GUIDANCE, TaskSteeringType.INTERRUPT_RESPONSE])
@pytest.mark.asyncio
async def test_guidance_and_answers_keep_no_files_the_gateway_did_not_send_for_the_task(
    lead, layout: InstanceLayout, statuses, caplog: pytest.LogCaptureFixture, sender: str, kind: TaskSteeringType
) -> None:
    reason, meta_for = _FORGED_SENDERS[sender]
    foreign = _foreign_files(layout)
    before_turn, before_own = _turn_files(lead), _own(lead.task_manager, lead._conversation_store)

    forged = _steering(kind, foreign, f"forged_{kind.value}", "Look at these.")
    with caplog.at_level(logging.WARNING, logger=ATTACHMENTS_LOGGER):
        await _send(lead, forged, meta_for(lead, forged))

    # The text is still taken (as before this change); none of the files are.
    assert [status for _id, status, _reason in statuses] == [TaskSteeringStatus.ACCEPTED]
    assert _turn_files(lead) == before_turn
    assert _files_entries(lead.task_manager) == []
    assert _own(lead.task_manager, lead._conversation_store) == before_own
    ids = [attachment.attachment_id for attachment in foreign]
    found, unknown = TaskAttachments.load(lead.task_manager.get_task_info(TASK), lead._conversation_store).find(ids)
    assert (found, unknown) == ([], ids)
    text = await _model_text(lead, kind)
    assert "Look at these." in text
    assert not any(attachment_id in text for attachment_id in ids)
    refused = _messages(caplog, "sent_file_refused")
    assert len(refused) == len(ids)
    for line, attachment_id in zip(refused, ids):
        assert f"reason={reason} attachment_id={attachment_id} task_id={TASK}" in line


@pytest.mark.parametrize("kind", [TaskSteeringType.PROACTIVE_GUIDANCE, TaskSteeringType.INTERRUPT_RESPONSE])
@pytest.mark.asyncio
async def test_files_the_gateway_sent_for_the_task_are_kept(lead, layout: InstanceLayout, statuses, kind) -> None:
    page = _upload(layout, "page two.png")

    await _send(lead, _steering(kind, [page], f"gateway_{kind.value}", "Here it is."))

    assert page.attachment_id in _turn_files(lead)
    assert page.attachment_id in _own(lead.task_manager, lead._conversation_store)
    assert f"`{page.attachment_id}` page two.png" in await _model_text(lead, kind)


def _assert_not_kept(agent, bobs: UserTaskAttachment, before_turn: list[str], before_own: list[str]) -> None:
    assert _turn_files(agent) == before_turn
    assert _own(agent.task_manager, agent._conversation_store) == before_own
    found, unknown = TaskAttachments.load(agent.task_manager.get_task_info(TASK), agent._conversation_store).find(
        [bobs.attachment_id]
    )
    assert (found, unknown) == ([], [bobs.attachment_id])


@pytest.mark.parametrize("kind", [TaskSteeringType.PROACTIVE_GUIDANCE, TaskSteeringType.INTERRUPT_RESPONSE])
@pytest.mark.asyncio
async def test_a_guidance_context_every_agent_process_received_vouches_for_no_other_message(
    lead, layout: InstanceLayout, statuses, caplog: pytest.LogCaptureFixture, kind: TaskSteeringType
) -> None:
    """The gateway sends guidance on the operational subject that every agent process of the team receives.
    A faulty one sends the genuine guidance's meta again under a new key, with Bob's upload."""

    page = _upload(layout, "page two.png")
    genuine = _steering(TaskSteeringType.PROACTIVE_GUIDANCE, [page], "genuine", "Here it is.")
    overheard = _gateway_meta(genuine)
    await _send(lead, genuine, overheard)
    assert page.attachment_id in _turn_files(lead)
    before_turn, before_own = _turn_files(lead), _own(lead.task_manager, lead._conversation_store)
    bobs = _upload(layout, "bobs scan.png", session_key="web:bob-session", conversation_id="conv_bob")

    with caplog.at_level(logging.WARNING, logger=ATTACHMENTS_LOGGER):
        await _send(lead, _steering(kind, [bobs], f"replayed_{kind.value}", "Look."), overheard.model_copy(deep=True))

    _assert_not_kept(lead, bobs, before_turn, before_own)
    [refused] = _messages(caplog, "sent_file_refused")
    assert f"reason=context_not_for_message attachment_id={bobs.attachment_id} task_id={TASK}" in refused


@pytest.mark.parametrize("kind", [TaskSteeringType.PROACTIVE_GUIDANCE, TaskSteeringType.INTERRUPT_RESPONSE])
@pytest.mark.asyncio
async def test_a_status_check_context_on_guidance_keeps_no_files_and_is_not_blamed_on_the_update(
    lead, layout: InstanceLayout, statuses, caplog: pytest.LogCaptureFixture, kind: TaskSteeringType
) -> None:
    """The gateway's status check carries Ann's context naming no task, on the subject every agent process
    receives. A faulty one puts it on guidance with a file: refused for naming no task, and the warning
    does not send the operator after a request queued across the update."""

    before_turn, before_own = _turn_files(lead), _own(lead.task_manager, lead._conversation_store)
    scan = _upload(layout, "scan.png")
    status_check = context_signer().issue(team_id=TEAM, person_id=ANN, person_roles=("member",))
    meta = MessageMeta(
        conversation_id=CONVERSATION,
        is_response_requested=True,
        gateway_context=GatewayContext(canonical_user_id=ANN, session_key=SESSION, task_context=status_check),
    )

    with caplog.at_level(logging.WARNING, logger=ATTACHMENTS_LOGGER):
        await _send(lead, _steering(kind, [scan], f"probe_{kind.value}", "Look."), meta)

    _assert_not_kept(lead, scan, before_turn, before_own)
    [refused] = _messages(caplog, "sent_file_refused")
    assert f"reason=context_names_no_task attachment_id={scan.attachment_id} task_id={TASK}" in refused
    assert "does not vouch" in refused and "send the file again" not in refused


@pytest.mark.asyncio
async def test_an_answer_sent_again_after_a_restart_with_another_file_keeps_none(
    lead, layout: InstanceLayout, statuses, caplog: pytest.LogCaptureFixture
) -> None:
    """Answers' keys are not saved, so after a restart the lead does not know an answer's key.
    The same key and context sent again with Bob's upload still vouch only for the answer's own file."""

    photo = _upload(layout, "pan.png")
    answer = _steering(TaskSteeringType.INTERRUPT_RESPONSE, [photo], "answer_once", "This pan.")
    overheard = _gateway_meta(answer)
    await _send(lead, answer, overheard)
    assert photo.attachment_id in _turn_files(lead)
    before_turn, before_own = _turn_files(lead), _own(lead.task_manager, lead._conversation_store)
    lead._processed_task_steering_ids.clear()  # what a restarted lead remembers of answers' keys
    bobs = _upload(layout, "bobs scan.png", session_key="web:bob-session", conversation_id="conv_bob")
    replay = _steering(TaskSteeringType.INTERRUPT_RESPONSE, [bobs], "answer_once", "This pan.")

    with caplog.at_level(logging.WARNING, logger=ATTACHMENTS_LOGGER):
        await _send(lead, replay, overheard.model_copy(deep=True))

    assert statuses[-1][2] != "Duplicate steering event ignored."  # it reached the files check
    _assert_not_kept(lead, bobs, before_turn, before_own)
    [refused] = _messages(caplog, "sent_file_refused")
    assert f"reason=context_not_for_message attachment_id={bobs.attachment_id} task_id={TASK}" in refused


@pytest.mark.asyncio
async def test_a_request_keeps_only_files_the_gateway_sent_for_it(
    lead, layout: InstanceLayout, caplog: pytest.LogCaptureFixture
) -> None:
    """A faulty agent process starts a new request for Ann with a worker's copy of TASK's context."""

    bobs = _foreign_files(layout)[0]
    forged = Task(
        task_id="task_forged",
        task_type=MessageMetaType.REQUEST,
        problem_statement="Describe this.",
        attachments=[bobs],
        # A files entry naming Bob's scan, shaped as if this runtime had written it.
        execution_history=[
            TaskHistoryEntry(
                summary="Files sent with a guidance accepted: bobs scan.png",
                details=task_files_details([bobs], message_id="s", message_type="proactive_guidance"),
            )
        ],
    )
    meta = _copied_task_context(lead).model_copy(
        update={"msg_correlation_id": "corr_forged", "requester_type": RequesterType.USER_REQUESTER}
    )

    with caplog.at_level(logging.WARNING, logger=ATTACHMENTS_LOGGER):
        await task_message_runtime._handle_task_request(
            lead, forged, MessageHeader(from_agent_id="worker", response_routing_key="worker_route"), meta
        )

    info = lead.task_manager.get_task_info("task_forged")
    assert info.owner_person_id == ANN  # the copied signature is valid for Ann
    assert info.task.attachments == []
    assert _files_entries(lead.task_manager, "task_forged") == []
    forged_turn = lead._conversation_store.get_turn_by_task_id("task_forged")
    assert (forged_turn["attachments"], forged_turn["attachments_recorded"]) == ([], False)
    listed = lead._conversation_store.list_turn_attachments(CONVERSATION)
    assert bobs.attachment_id not in [entry.attachment.attachment_id for entry in listed]
    found, unknown = TaskAttachments.load(info, lead._conversation_store).find([bobs.attachment_id])
    assert (found, unknown) == ([], [bobs.attachment_id])
    [refused] = _messages(caplog, "sent_file_refused")
    assert f"reason=another_task attachment_id={bobs.attachment_id} task_id=task_forged" in refused
    assert _messages(caplog, "incoming_task_files_entries_dropped")


def test_a_copy_of_the_request_with_other_files_keeps_none(
    lead, layout: InstanceLayout, caplog: pytest.LogCaptureFixture
) -> None:
    """A copy of TASK's own request under its own context, with Bob's upload instead of its files.

    Admission drops a copy while TASK is held or finished; a lead taking over a task whose holder is
    gone admits one, so the files check must hold for it too."""

    bobs = _foreign_files(layout)[0]
    admitted = lead.task_manager.get_task_info(TASK)
    copy = admitted.task.model_copy(update={"attachments": [bobs]})

    def folders() -> UploadFolders:
        return UploadFolders.for_agent(lead)

    with caplog.at_level(logging.WARNING, logger=ATTACHMENTS_LOGGER):
        kept = admit_request_files(copy, _copied_task_context(lead), team_id=TEAM, person_id=ANN, folders=folders)
        unchanged = admit_request_files(
            admitted.task, _copied_task_context(lead), team_id=TEAM, person_id=ANN, folders=folders
        )

    assert (kept.task.attachments, kept.not_kept) == ([], 1)
    assert (unchanged.task.attachments, unchanged.not_kept) == (admitted.task.attachments, 0)
    [refused] = _messages(caplog, "sent_file_refused")
    assert f"reason=context_not_for_message attachment_id={bobs.attachment_id} task_id={TASK}" in refused


@pytest.mark.asyncio
async def test_a_request_signed_before_the_update_is_admitted_without_its_files_and_says_why(
    tmp_path: Path, layout: InstanceLayout, caplog: pytest.LogCaptureFixture
) -> None:
    """A request still queued for the lead when the instance was updated: the gateway before the update
    signed only the team, the person and their roles, so the context names no task."""

    agent = _lead(tmp_path, layout)
    photo = _upload(layout, "photo.png")
    task = Task(task_id=TASK, task_type=MessageMetaType.REQUEST, problem_statement="What is in it?", attachments=[photo])
    signed = context_signer().issue(team_id=TEAM, person_id=ANN, person_roles=("member",))
    meta = MessageMeta(
        conversation_id=CONVERSATION,
        msg_correlation_id="corr_queued",
        requester_type=RequesterType.USER_REQUESTER,
        gateway_context=GatewayContext(canonical_user_id=ANN, session_key=SESSION, task_context=signed),
    )

    with caplog.at_level(logging.WARNING, logger=ATTACHMENTS_LOGGER):
        await task_message_runtime._handle_task_request(
            agent, task, MessageHeader(from_agent_id="gateway", response_routing_key="gateway_route"), meta
        )

    info = agent.task_manager.get_task_info(TASK)
    assert info.owner_person_id == ANN  # the request itself is admitted for Ann
    assert info.task.attachments == []
    assert _turn_files(agent) == []
    assert agent._conversation_store.get_turn_by_task_id(TASK)["attachments_recorded"] is False
    [refused] = _messages(caplog, "sent_file_refused")
    assert f"reason=context_names_no_task attachment_id={photo.attachment_id} task_id={TASK}" in refused
    assert "names no task" in refused and "the person must send the file again" in refused


# --- Refused guidance and answers ---------------------------------------------------------------


@pytest.mark.asyncio
async def test_an_answer_refused_as_stale_keeps_none_of_its_files(
    lead, layout: InstanceLayout, statuses, monkeypatch, caplog: pytest.LogCaptureFixture
) -> None:
    monkeypatch.setattr(task_steering_runtime, "mid_turn_steering_config", lambda _agent: {"allow_restart_fallback": False})
    before_turn, before_own = _turn_files(lead), _own(lead.task_manager, lead._conversation_store)
    photo = _upload(layout, "pan.png")

    with caplog.at_level(logging.WARNING):
        await _send(lead, _steering(TaskSteeringType.INTERRUPT_RESPONSE, [photo], "answer_late", "This pan."))

    assert statuses[0][1:] == (TaskSteeringStatus.STALE, "Live Strands interrupt session is no longer available.")
    assert _turn_files(lead) == before_turn
    assert _files_entries(lead.task_manager) == []
    assert _own(lead.task_manager, lead._conversation_store) == before_own
    assert _messages(caplog, "sent_files_kept_message_refused") == []  # nothing was kept


def _after_the_turn_merge(monkeypatch: pytest.MonkeyPatch, then) -> None:
    """Run ``then`` once a message's files are merged into the turn, as the lead's other work could
    while the handler waits for the merge."""

    merge = sent_files_runtime.record_turn_attachments

    def merge_then(*args, **kwargs):
        added = merge(*args, **kwargs)
        then()
        return added

    monkeypatch.setattr(sent_files_runtime, "record_turn_attachments", merge_then)


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", [TaskSteeringType.PROACTIVE_GUIDANCE, TaskSteeringType.INTERRUPT_RESPONSE])
async def test_a_message_refused_after_its_files_were_kept_is_logged_with_them(
    lead, layout: InstanceLayout, statuses, monkeypatch, caplog: pytest.LogCaptureFixture, kind
) -> None:
    # The window sent_files_runtime names: the task's files entry and the turn are written, then
    # the request's turn finishes before the guidance is recorded, or the live session waiting for
    # the answer ends while the restart fallback is off.
    if kind == TaskSteeringType.PROACTIVE_GUIDANCE:
        _after_the_turn_merge(monkeypatch, lambda: lead.task_manager.task_state.tasks.pop(TASK))
    else:
        monkeypatch.setattr(
            task_steering_runtime, "mid_turn_steering_config", lambda _agent: {"allow_restart_fallback": False}
        )
        lead._interrupted_turn_sessions[TASK] = object()
        _after_the_turn_merge(monkeypatch, lambda: lead._interrupted_turn_sessions.pop(TASK))
    photo = _upload(layout, "pan.png")
    steering = _steering(kind, [photo], "late", "This pan.")

    with caplog.at_level(logging.WARNING):
        await _send(lead, steering)

    assert [status for _id, status, _reason in statuses] == [TaskSteeringStatus.STALE]
    assert photo.attachment_id in _turn_files(lead)
    [warning] = _messages(caplog, "sent_files_kept_message_refused")
    assert warning.endswith(
        f"task_id={TASK} steering_id={steering.steering_id} steering_type={kind.value} attachment_ids={photo.attachment_id}"
    )


async def _first_delivery_stops_after_keeping_files(monkeypatch: pytest.MonkeyPatch, lead, steering: TaskSteering) -> None:
    """The first delivery records the message's files and then stops before the message itself is recorded
    (a store error, or the process stopping), so no status is sent and the gateway sends it again later."""

    keep = task_steering_runtime.keep_sent_files

    async def keep_then_stop(*args: Any, **kwargs: Any) -> Any:
        await keep(*args, **kwargs)
        raise OSError("the process stopped after recording the files")

    monkeypatch.setattr(task_steering_runtime, "keep_sent_files", keep_then_stop)
    with pytest.raises(OSError):
        await _send(lead, steering)
    monkeypatch.setattr(task_steering_runtime, "keep_sent_files", keep)
    # A restart: the live session and the handler's memory of seen messages are gone.
    lead._interrupted_turn_sessions.pop(TASK, None)
    lead._processed_task_steering_ids.clear()


@pytest.mark.asyncio
@pytest.mark.parametrize("refused_because", ["task_ended", "steering_turned_off", "no_live_session"])
async def test_a_resend_refused_after_an_earlier_delivery_kept_its_files_is_logged_with_them(
    lead, layout: InstanceLayout, statuses, monkeypatch, caplog: pytest.LogCaptureFixture, refused_because: str
) -> None:
    """FAIL-1: the resend of a message whose files an earlier delivery kept is refused; the files stay,
    and the refusal says so with the message and file ids."""

    photo = _upload(layout, "pan.png")
    if refused_because == "no_live_session":
        monkeypatch.setattr(task_steering_runtime, "mid_turn_steering_config", lambda _agent: {"allow_restart_fallback": False})
        lead._interrupted_turn_sessions[TASK] = object()
        steering = _steering(TaskSteeringType.INTERRUPT_RESPONSE, [photo], "answer_resent", "This pan.")
    else:
        steering = _steering(TaskSteeringType.PROACTIVE_GUIDANCE, [photo], "guide_resent", "Use this pan.")
    await _first_delivery_stops_after_keeping_files(monkeypatch, lead, steering)
    assert statuses == [] and photo.attachment_id in _turn_files(lead)
    if refused_because == "task_ended":
        lead.task_manager.get_task_info(TASK).state = ProgressState.COMPLETED
    elif refused_because == "steering_turned_off":
        monkeypatch.setattr(task_steering_runtime, "mid_turn_steering_enabled", lambda _agent: False)

    with caplog.at_level(logging.WARNING):
        await _send(lead, steering)

    assert [status for _id, status, _reason in statuses] == [
        TaskSteeringStatus.REJECTED if refused_because == "steering_turned_off" else TaskSteeringStatus.STALE
    ]
    assert photo.attachment_id in _turn_files(lead)
    [warning] = _messages(caplog, "sent_files_kept_message_refused")
    assert warning.endswith(
        f"task_id={TASK} steering_id={steering.steering_id} steering_type={steering.steering_type.value} "
        f"attachment_ids={photo.attachment_id}"
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("taken", ["guidance_then_task_ended", "guidance_then_steering_off", "answer_then_task_ended"])
async def test_a_late_resend_of_a_message_the_task_took_is_not_reported_as_lost(
    lead, layout: InstanceLayout, statuses, monkeypatch, caplog: pytest.LogCaptureFixture, taken: str
) -> None:
    """The message was taken in full, but its ACCEPTED status never went out (a restart), so the gateway sends
    it again after the task ended or steering was turned off: refused as before, and nothing was lost."""

    photo = _upload(layout, "pan.png")
    if taken.startswith("answer"):
        # No live session: the answer is taken as restart-safe recovery guidance.
        steering = _steering(TaskSteeringType.INTERRUPT_RESPONSE, [photo], "answer_taken", "This pan.")
    else:
        steering = _steering(TaskSteeringType.PROACTIVE_GUIDANCE, [photo], "guide_taken", "Use this pan.")
    await _send(lead, steering)
    assert statuses[-1][1] == TaskSteeringStatus.ACCEPTED
    lead._processed_task_steering_ids.clear()  # a restart
    if taken.endswith("steering_off"):
        monkeypatch.setattr(task_steering_runtime, "mid_turn_steering_enabled", lambda _agent: False)
    else:
        lead.task_manager.get_task_info(TASK).state = ProgressState.COMPLETED

    with caplog.at_level(logging.INFO):
        await _send(lead, steering)

    assert statuses[-1][1] in {TaskSteeringStatus.STALE, TaskSteeringStatus.REJECTED}
    assert _messages(caplog, "sent_files_kept_message_refused") == []
    [noted] = _messages(caplog, "sent_files_taken_message_resend_refused")
    assert f"task_id={TASK} steering_id={steering.steering_id} attachment_ids={photo.attachment_id}" in noted


def _saved_before_the_update(lead, steering: TaskSteering) -> None:
    """Guidance the lead accepted before the update: the whole message saved in its history, files included,
    and no files entry, as the runtime before the update wrote it."""

    lead.task_manager.append_history_entry(
        TASK,
        f"Mid-turn proactive steering accepted: {steering.guidance_text}",
        importance=TaskHistoryImportance.HIGH,
        details={
            "event": task_steering_runtime.GUIDANCE_ACCEPTED_EVENT,
            "steering_id": steering.steering_id,
            "conversation_id": steering.conversation_id,
            "dedupe_key": steering.idempotency_key or steering.steering_id,
            "steering": steering.model_dump(mode="json"),
        },
    )


@pytest.mark.asyncio
async def test_guidance_restored_from_before_the_update_names_only_files_the_task_can_open(
    lead, layout: InstanceLayout, statuses
) -> None:
    """GEN-3: a restored guidance names no id the tools refuse, and says how many of its files cannot be opened."""

    old_photo, new_page = _upload(layout, "receipt.png"), _upload(layout, "page two.png")
    _saved_before_the_update(
        lead, _steering(TaskSteeringType.PROACTIVE_GUIDANCE, [old_photo], "guide_old", "Use this receipt instead.")
    )
    await _send(lead, _steering(TaskSteeringType.PROACTIVE_GUIDANCE, [new_page], "guide_new", "Here is page two."))
    # A restart: only the history is left.
    lead._pending_proactive_task_steering.clear()

    text = await consume_pending_proactive_guidance_message(lead, TASK)

    assert "- Use this receipt instead." in text and "- Here is page two." in text
    assert old_photo.attachment_id not in text
    assert "1 file(s) sent with this guidance were saved before files were recorded, so they cannot be opened" in text
    assert f"`{new_page.attachment_id}` page two.png" in text
    assert old_photo.attachment_id not in _own(lead.task_manager, lead._conversation_store)

# --- What models see of the task's history -----------------------------------------------------------


@pytest.mark.asyncio
async def test_history_views_name_kept_files_without_where_their_bytes_are(
    lead, layout: InstanceLayout, statuses
) -> None:
    # The history keeps full records (the files entries, and the guidance copy
    # held for a restart), with where the bytes are; models read history through
    # get_task_history_window, get_recovery_context and the turn summary prompt.
    files = [_upload(layout, "page two.png"), _upload(layout, "pan.png")]
    await _send(lead, _steering(TaskSteeringType.PROACTIVE_GUIDANCE, files[:1], "guide_views", "Here is page two."))
    await _send(lead, _steering(TaskSteeringType.INTERRUPT_RESPONSE, files[1:], "answer_views", "This pan."))
    assert [status for _id, status, _reason in statuses] == [TaskSteeringStatus.ACCEPTED] * 2
    history = lead.task_manager.get_task_info(TASK).task.execution_history
    stored = json.dumps([entry.details for entry in history])
    assert all(str(file.local_path()) in stored for file in files)

    use_agent(lead, TASK)
    # Strands runs these synchronous tools off the event loop that owns the task manager.
    window = await asyncio.to_thread(get_task_history_window._tool_func, task_id=TASK, limit=100)
    await lead.task_manager.operations_runtime.run(lead.task_manager.set_task_plan, TASK, "Read both pages.")
    recovery = await lead.task_manager.operations_runtime.run(
        lead.task_manager.start_isolated_recovery_task,
        TASK,
        failure_type="delegated_specialist_timeout",
        failure_summary="A step timed out.",
        recovery_brief="Decide how to finish reading the recipe.",
        recovery_task_id="task_recipe_recovery",
        header=MessageHeader(from_agent_id="lead"),
        meta=MessageMeta(conversation_id=CONVERSATION),
    )
    assert recovery is not None
    use_agent(lead, recovery.task.task_id)
    # "all" covers both history sections: recent_history and failure_window.
    recovery_text = await asyncio.to_thread(get_recovery_context._tool_func, detail_level="all", max_entries=30)
    model = _RecordingSummaryModel()
    generator = LLMTurnSummaryGenerator(
        config=ConversationSummaryConfig(), logger=logging.getLogger("test"), summarization_model=model
    )
    await asyncio.to_thread(generator.summarize_turn, TurnSummaryRequest(task=lead.task_manager.get_task_info(TASK).task))
    [summary_call] = model.calls

    views = {"history window": window, "recovery": recovery_text, "summary prompt": json.dumps(summary_call)}
    showing_paths = [view for view, text in views.items() if str(layout.uploads_dir) in text or "storage_refs" in text]
    assert showing_paths == []
    for view, text in views.items():
        for file in files:
            assert file.sha256 not in text, view
            # The recovery view keeps no runtime ids (its own rule), so it names files by their other fields.
            assert (file.filename if view == "recovery" else file.attachment_id) in text, view
        assert "image/png" in text, view
    # The views filter copies: the stored records still say where the bytes are.
    after = json.dumps([entry.details for entry in lead.task_manager.get_task_info(TASK).task.execution_history])
    assert all(str(file.local_path()) in after for file in files)
