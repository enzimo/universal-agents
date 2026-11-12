"""Files sent with a request, a guidance or an answer are kept for the task and its conversation.

Uses the real admission path, the real steering handler, real SQLite task and
conversation stores and the gateway's own upload storage, for a team lead
working for Ann (signed task context). Covers:

- the request's own files are kept on its conversation turn at admission;
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
- a delegated task keeps no files from guidance, and the lead sends guidance
  on without files;
- an answer refused as stale keeps none of its files.
"""

from __future__ import annotations

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
from custom_agents import task_message_runtime, task_steering_runtime
from custom_agents.conversation_attachments import (
    TASK_FILES_EVENT,
    TaskAttachments,
    request_files_kept,
    task_files_details,
)
from custom_agents.conversation_store import ConversationStore
from custom_agents.task_manager import TaskManager
from custom_agents.task_steering_runtime import (
    _build_interrupt_response_blocks,
    consume_pending_proactive_guidance_message,
    handle_task_steering_message,
)
from custom_messages.contracts.attachments_v1 import AttachmentStorageRef, UserTaskAttachment
from custom_messages.contracts.routing_v1 import GatewayContext, MessageHeader, MessageMeta
from custom_messages.contracts.task_v1 import Task, TaskHistoryEntry, TaskSteering
from custom_messages.enums import MessageMetaType, RequesterType, TaskSteeringStatus, TaskSteeringType
from runtime_settings.instance_layout import InstanceLayout
from tests.broker_access_support import context_signer, context_verify_keys, forget_task_context_keys
from tests.test_conversation_attachments_regression import ANN, CONVERSATION, SESSION, TEAM, _admit, _lead

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
async def test_admission_keeps_the_requests_files_on_its_turn_but_not_records_outside_the_upload_folders(
    tmp_path: Path, layout: InstanceLayout, caplog: pytest.LogCaptureFixture
) -> None:
    agent = _lead(tmp_path, layout)
    page = _upload(layout, "page one.png")

    with caplog.at_level(logging.WARNING, logger=ATTACHMENTS_LOGGER):
        await _admit(agent, task_id=TASK, text="Read this recipe.", attachments=[page, _outside(layout)])

    assert _turn_files(agent) == [page.attachment_id]
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
    assert entry.details["steering_id"] == steering.steering_id
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
    assert _files_entries(lead.task_manager) == []
    assert TASK not in lead._pending_proactive_task_steering

    # The gateway sends its held copy again.
    await _send(lead, steering)
    await _send(lead, steering)

    assert [status for _id, status, _reason in statuses] == [TaskSteeringStatus.ACCEPTED, TaskSteeringStatus.ACCEPTED]
    assert statuses[1][2] == "Duplicate steering event ignored."
    assert len(_files_entries(lead.task_manager)) == 1
    lead._conversation_store = store
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

    [envelope] = sent
    assert envelope.msg_content.target_task_id == "task_child"
    assert envelope.msg_content.guidance_text == "Use page two too."
    assert envelope.msg_content.attachments == []
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
    assert entry.details["steering_type"] == TaskSteeringType.INTERRUPT_RESPONSE.value
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
                details=task_files_details([bobs], steering_id="s", steering_type="proactive_guidance"),
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
    assert lead._conversation_store.get_turn_by_task_id("task_forged")["attachments"] == []
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

    with caplog.at_level(logging.WARNING, logger=ATTACHMENTS_LOGGER):
        kept = request_files_kept(copy, _copied_task_context(lead), team_id=TEAM, person_id=ANN)
        unchanged = request_files_kept(admitted.task, _copied_task_context(lead), team_id=TEAM, person_id=ANN)

    assert kept.attachments == []
    assert unchanged.attachments == admitted.task.attachments
    [refused] = _messages(caplog, "sent_file_refused")
    assert f"reason=context_not_for_message attachment_id={bobs.attachment_id} task_id={TASK}" in refused


# --- A refused answer ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_an_answer_refused_as_stale_keeps_none_of_its_files(lead, layout: InstanceLayout, statuses, monkeypatch) -> None:
    monkeypatch.setattr(task_steering_runtime, "mid_turn_steering_config", lambda _agent: {"allow_restart_fallback": False})
    before_turn, before_own = _turn_files(lead), _own(lead.task_manager, lead._conversation_store)
    photo = _upload(layout, "pan.png")

    await _send(lead, _steering(TaskSteeringType.INTERRUPT_RESPONSE, [photo], "answer_late", "This pan."))

    assert statuses[0][1:] == (TaskSteeringStatus.STALE, "Live Strands interrupt session is no longer available.")
    assert _turn_files(lead) == before_turn
    assert _files_entries(lead.task_manager) == []
    assert _own(lead.task_manager, lead._conversation_store) == before_own
