"""Files sent with a request or with guidance are kept with the request's conversation turn.

The request is admitted through the real ``ensure_task_conversation_context``
and guidance through the real ``handle_task_steering_message``, on a real task
manager and conversation store (SQLite, and PostgreSQL when
``LIVE_TEST_POSTGRES_DSN`` is set). Only the transport is a recorder. The
guidance's files are saved with the turn before the guidance is recorded or
answered, named with their ids when the model reads the guidance, and still
found by id after the guidance was read, after a restart and in the next turn.
"""

from __future__ import annotations

import logging
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from custom_agents.conversation_attachments import attachment_scope, request_files
from custom_agents.conversation_runtime import ensure_task_conversation_context
from custom_agents.conversation_store import ConversationStore
from custom_agents.steering_history_events import GUIDANCE_ACCEPTED_EVENT, HISTORY_EVENT_KEY
from custom_agents.task_manager import TaskManager
from custom_agents.task_steering_runtime import (
    consume_pending_proactive_guidance_message,
    handle_task_steering_message,
)
from custom_messages.contracts.attachments_v1 import AttachmentStorageRef, UserTaskAttachment
from custom_messages.contracts.envelope_v1 import MessageEnvelope
from custom_messages.contracts.routing_v1 import MessageHeader, MessageMeta
from custom_messages.contracts.task_v1 import Task, TaskSteering
from custom_messages.enums import (
    MessageMetaType,
    RequesterType,
    RoutingType,
    TaskSteeringStatus,
    TaskSteeringType,
)
from runtime_settings.instance_layout import InstanceLayout
from tests.knowledge_support import BACKENDS, knowledge_storage

TEAM = "team_guidance_files"
LEAD = "lead_files"
GATEWAY = "team_guidance_files_gateway_response"
PERSON = "alice"
ROOT = "task_turn_1"


class RecordingComms:
    """The lead's transport: records what it sends."""

    def __init__(self) -> None:
        self.sent: list[tuple[str, MessageEnvelope]] = []

    async def send_message(self, *, message: MessageEnvelope, routing_key: str) -> None:
        self.sent.append((routing_key, message))

    def answers(self) -> list[TaskSteering]:
        return [envelope.msg_content for key, envelope in self.sent if key == GATEWAY]


class _Instance:
    """One instance's uploads folder and stores, and the team lead holding the request ``ROOT``."""

    def __init__(self, storage, tmp_path: Path) -> None:
        self.storage = storage
        self.layout = InstanceLayout.at(tmp_path / "instance")
        self.layout.ensure_directories()
        self.manager = self.open_manager()
        self.store = ConversationStore(
            db_path=self.manager._db_path,
            artifacts_dir=self.layout.conversation_artifacts_dir,
            storage_settings=storage,
        )
        self.lead = self.agent()
        self.conversation_id = self.admit(ROOT, [self.file("att_receipt", "receipt.jpg")])

    def open_manager(self) -> TaskManager:
        manager = TaskManager(storage_settings=self.storage)
        manager.set_owner_identity(LEAD, "Lead", TEAM, "TeamArchitect")
        return manager

    def agent(self) -> Any:
        return SimpleNamespace(
            id=LEAD, name="Lead", named_agent_type="TeamArchitect", team_id=TEAM,
            mid_turn_steering={"enabled": True}, task_manager=self.manager, _conversation_store=self.store,
            _pending_proactive_task_steering={}, _processed_task_steering_ids=set(),
            _agent_id_by_name={}, named_agent_type_queue_map={},
            team_comms=RecordingComms(), _logger=logging.getLogger("test.guidance_files"),
        )

    def restart(self) -> None:
        self.manager = self.open_manager()
        assert self.manager.restore_runtime_state()
        self.lead = self.agent()

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

    def turn_file_ids(self, task_id: str = ROOT) -> list[str]:
        return [item["attachment_id"] for item in self.store.get_turn_by_task_id(task_id)["attachments"]]

    def scope(self, task_id: str = ROOT):
        request = request_files(self.manager, task_id)
        assert request is not None
        return attachment_scope(request, conversation_store=self.store, is_team_lead=True, person_id=PERSON)

    def accepted_entries(self) -> list[Any]:
        task = self.manager.get_task_info(ROOT).task
        return [entry for entry in task.execution_history if entry.details.get(HISTORY_EVENT_KEY) == GUIDANCE_ACCEPTED_EVENT]


def _guidance(instance: _Instance, key: str = "corr_guidance") -> tuple[TaskSteering, MessageHeader, MessageMeta]:
    steering = TaskSteering(
        steering_type=TaskSteeringType.PROACTIVE_GUIDANCE, target_task_id=ROOT, target_agent_id=LEAD,
        guidance_text="Use this photo too.", idempotency_key=key,
        attachments=[instance.file("att_guidance", "pantry.jpg")],
    )
    header = MessageHeader(from_agent_id=GATEWAY, to_routing_path=RoutingType.ALL_AGENTS_ROUTE,
                           response_routing_key=GATEWAY)
    meta = MessageMeta(msg_correlation_id=key, conversation_id=instance.conversation_id,
                       requester_type=RequesterType.USER_REQUESTER, is_response_requested=True)
    return steering, header, meta


@pytest.fixture(params=BACKENDS)
def instance(request, tmp_path: Path):
    with knowledge_storage(request.param, tmp_path) as settings:
        yield _Instance(settings, tmp_path)


def _ids(files) -> list[str]:
    return [getattr(file, "attachment", file).attachment_id for file in files]


def test_a_requests_files_are_saved_with_its_turn(instance: _Instance) -> None:
    turn = instance.store.get_turn_by_task_id(ROOT)

    # Shown without where they are stored; the trusted lookup has the whole record.
    assert turn["attachments"] == [{
        "attachment_id": "att_receipt", "filename": "receipt.jpg", "content_type": "image/jpeg",
        "kind": "image", "size_bytes": 4,
    }]
    [listed] = instance.store.list_conversation_attachments(instance.conversation_id, limit=20)
    assert (listed["attachment_id"], listed["turn_index"], listed["task_id"]) == ("att_receipt", 1, ROOT)
    assert listed["storage_refs"][0]["path"].startswith(str(instance.layout.uploads_dir))
    # A request without files keeps an empty list.
    instance.admit("task_turn_2", [], instance.conversation_id)
    assert instance.turn_file_ids("task_turn_2") == []


@pytest.mark.asyncio
async def test_guidance_files_are_saved_named_and_found_after_reading_a_restart_and_in_the_next_turn(
    instance: _Instance,
) -> None:
    steering, header, meta = _guidance(instance)

    await handle_task_steering_message(instance.lead, steering, header, meta)

    assert [answer.status for answer in instance.lead.team_comms.answers()] == [TaskSteeringStatus.ACCEPTED]
    assert instance.turn_file_ids() == ["att_receipt", "att_guidance"]
    # The model reads the guidance with its file named by id, never its path.
    block = await consume_pending_proactive_guidance_message(instance.lead, ROOT)
    assert "- Use this photo too." in block
    assert "Files sent with this guidance (open one by its attachment_id):" in block
    assert "  - pantry.jpg (attachment_id att_guidance, image, image/jpeg, 4 bytes)" in block
    assert str(instance.layout.uploads_dir) not in block
    assert instance.lead._pending_proactive_task_steering == {}
    # Read and drained, then after a restart: still the request's file.
    for _ in range(2):
        assert _ids(request_files(instance.manager, ROOT).files) == ["att_receipt", "att_guidance"]
        assert instance.scope().find("att_guidance").attachment.filename == "pantry.jpg"
        instance.restart()
    # The next turn finds it with the turn it came with.
    instance.admit("task_turn_2", [], instance.conversation_id)
    earlier = instance.scope("task_turn_2").earlier_files()
    assert [(file.attachment.attachment_id, file.turn_index) for file in earlier] == [
        ("att_guidance", 1), ("att_receipt", 1),
    ]


@pytest.mark.asyncio
async def test_a_failed_save_records_nothing_and_the_resent_guidance_is_saved_once(
    instance: _Instance, monkeypatch: pytest.MonkeyPatch
) -> None:
    steering, header, meta = _guidance(instance)
    merge = instance.store.merge_turn_attachments
    failures = [RuntimeError("database is locked")]

    def merge_failing_once(**kwargs):
        if failures:
            raise failures.pop()
        return merge(**kwargs)

    monkeypatch.setattr(instance.store, "merge_turn_attachments", merge_failing_once)

    with pytest.raises(RuntimeError, match="locked"):
        await handle_task_steering_message(instance.lead, steering, header, meta)
    # Nothing recorded or answered: the delivery comes again, as does the gateway's held guidance.
    assert instance.accepted_entries() == []
    assert instance.lead.team_comms.answers() == []
    assert instance.lead._processed_task_steering_ids == set()
    assert instance.lead._pending_proactive_task_steering == {}
    assert instance.turn_file_ids() == ["att_receipt"]

    # Sent again, then once more as a repeat.
    await handle_task_steering_message(instance.lead, steering, header, meta)
    await handle_task_steering_message(instance.lead, steering, header, meta)

    assert instance.turn_file_ids() == ["att_receipt", "att_guidance"]
    assert len(instance.accepted_entries()) == 1
    assert [answer.status for answer in instance.lead.team_comms.answers()] == [TaskSteeringStatus.ACCEPTED] * 2
    assert _ids(request_files(instance.manager, ROOT).files) == ["att_receipt", "att_guidance"]


@pytest.mark.asyncio
async def test_a_workers_forwarded_guidance_files_stay_with_its_subtask(instance: _Instance) -> None:
    """A worker holds a delegated subtask: no conversation turn, the files only in its own history."""

    worker_manager = TaskManager(storage_settings=instance.storage)
    worker_manager.set_owner_identity("worker_1", "Researcher", TEAM, "WebResearchAgent")
    assert worker_manager.add_task(
        Task(task_id="task_child", parent_task_id=ROOT, task_type=MessageMetaType.REQUEST, problem_statement="Research."),
        MessageHeader(from_agent_id=LEAD, response_routing_key=LEAD), MessageMeta(conversation_id="conv_child"),
        owner_person_id=PERSON,
    )
    worker = SimpleNamespace(
        id="worker_1", name="Researcher", named_agent_type="WebResearchAgent", team_id=TEAM,
        mid_turn_steering={"enabled": True}, task_manager=worker_manager, _conversation_store=instance.store,
        _pending_proactive_task_steering={}, _processed_task_steering_ids=set(),
        _agent_id_by_name={}, named_agent_type_queue_map={},
        team_comms=RecordingComms(), _logger=logging.getLogger("test.guidance_files.worker"),
    )
    steering = TaskSteering(
        steering_type=TaskSteeringType.PROACTIVE_GUIDANCE, target_task_id="task_child", target_agent_id="worker_1",
        guidance_text="Use this photo too.", idempotency_key="corr_guidance:task_child",
        attachments=[instance.file("att_guidance", "pantry.jpg")],
    )

    await handle_task_steering_message(
        worker, steering, MessageHeader(from_agent_id=LEAD, response_routing_key=LEAD),
        MessageMeta(msg_correlation_id="corr_guidance:task_child", is_response_requested=False),
    )

    assert instance.turn_file_ids() == ["att_receipt"]
    worker_request = request_files(worker_manager, "task_child")
    assert (worker_request.conversation_id, _ids(worker_request.files)) == (None, ["att_guidance"])
