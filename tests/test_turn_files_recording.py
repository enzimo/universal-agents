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
- a delegated task records its files in its history only.
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
from custom_agents import task_message_runtime, task_steering_runtime
from custom_agents.conversation_attachments import TASK_FILES_EVENT, TaskAttachments
from custom_agents.conversation_store import ConversationStore
from custom_agents.task_manager import TaskManager
from custom_agents.task_steering_runtime import (
    _build_interrupt_response_blocks,
    consume_pending_proactive_guidance_message,
    handle_task_steering_message,
)
from custom_messages.contracts.attachments_v1 import AttachmentStorageRef, UserTaskAttachment
from custom_messages.contracts.routing_v1 import MessageHeader, MessageMeta
from custom_messages.contracts.task_v1 import Task, TaskSteering
from custom_messages.enums import MessageMetaType, TaskSteeringStatus, TaskSteeringType
from runtime_settings.instance_layout import InstanceLayout
from tests.broker_access_support import context_verify_keys, forget_task_context_keys
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


def _upload(layout: InstanceLayout, name: str) -> UserTaskAttachment:
    """A file the gateway stored for Ann's session, with its record."""

    return store_inbound_attachment_bytes_with_policy(
        data=b"\x89PNG\r\n\x1a\n" + name.encode(),
        filename=name,
        content_type="image/png",
        policy=InboundAttachmentStoragePolicy(cache_dir=layout.uploads_dir),
        session_key=SESSION,
        conversation_id=CONVERSATION,
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


async def _send(agent, steering: TaskSteering) -> None:
    await handle_task_steering_message(
        agent,
        steering,
        MessageHeader(from_agent_id="gateway"),
        MessageMeta(conversation_id=CONVERSATION, is_response_requested=True),
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
async def test_a_delegated_task_records_forwarded_guidance_files_in_its_history_only(
    tmp_path: Path, layout: InstanceLayout, statuses
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

    await _send(worker, forwarded)

    assert [status for _id, status, _reason in statuses] == [TaskSteeringStatus.ACCEPTED]
    assert merges == []
    assert _own(worker.task_manager, None, "task_child") == [page_two.attachment_id]


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
