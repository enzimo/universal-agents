"""The attachment tools open a file the person sent by its id, within what the task may use.

``inspect_user_attachment`` and ``load_local_media_for_model_context`` find an
id through ``custom_tools.attachment_uploads``: the current request's files and
the files sent while it ran, and for the team lead's own request the files of
its conversation's earlier turns. These tests run the tools the way the agent
calls them (through Strands' input validation) on a real task manager and a
real SQLite conversation store, with the person taken from a gateway-signed
task context. tests/test_conversation_attachment_followup.py covers the
reported case (a photo opened in the next turn, as an image block).
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from custom_agents.conversation_store import ConversationStore
from custom_agents.task_manager import TaskManager
from custom_agents.task_steering_runtime import _record_guidance_history
from custom_messages.contracts.attachments_v1 import AttachmentStorageRef, UserTaskAttachment
from custom_messages.contracts.routing_v1 import GatewayContext, MessageHeader, MessageMeta
from custom_messages.contracts.runtime_snapshot_v3 import PlanStepTaskLink
from custom_messages.contracts.task_v1 import Task, TaskSteering
from custom_messages.enums import MessageMetaType, RequesterType, TaskSteeringType
from custom_tools.local_media_context_tool import load_local_media_for_model_context
from custom_tools.runtime_context import _agent_ctx
from custom_tools.user_attachment_tools import inspect_user_attachment
from runtime_settings.instance_layout import InstanceLayout
from tests.broker_access_support import context_signer
from tests.change_approval_support import use_signed_contexts
from tests.task_work_support import set_agent_ref

TEAM = "team_files_by_id"
PERSON = "alice"
OTHER_PERSON = "bob"
LEAD_ID = "lead_files"
CONVERSATION = "conv_alice"
ROOT = "task_turn_2"
NOTES = b"line one\nline two\n"
PNG = b"\x89PNG\r\n\x1a\nimage-bytes"


@pytest.fixture(autouse=True)
def _signed_contexts(monkeypatch: pytest.MonkeyPatch):
    use_signed_contexts(monkeypatch)
    _agent_ctx.set(None)
    yield
    _agent_ctx.set(None)


def _meta(conversation_id: str | None, person_id: str = PERSON) -> MessageMeta:
    # The person comes only from the gateway-signed task context.
    signed = context_signer().issue(team_id=TEAM, person_id=person_id, person_roles=("member",))
    return MessageMeta(
        conversation_id=conversation_id,
        requester_type=RequesterType.USER_REQUESTER,
        gateway_context=GatewayContext(canonical_user_id=person_id, task_context=signed),
    )


class _Instance:
    """One instance's uploads, conversation store and the team lead's task manager."""

    def __init__(self, tmp_path: Path) -> None:
        self.layout = InstanceLayout.at(tmp_path / "instance")
        self.layout.ensure_directories()
        self.store = ConversationStore(
            db_path=str(self.layout.root / "conversations.db"), artifacts_dir=self.layout.conversation_artifacts_dir
        )
        self.manager = TaskManager(db_path=str(self.layout.root / "tasks.db"))
        self.manager.set_owner_identity(LEAD_ID, "TeamLead", TEAM, "TeamArchitect")

    def file(
        self, attachment_id: str, filename: str, data: bytes = PNG, *, content_type: str = "image/png", kind: str = "image"
    ) -> UserTaskAttachment:
        path = self.layout.uploads_dir / "session_alice" / attachment_id
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
        return UserTaskAttachment(
            attachment_id=attachment_id,
            filename=filename,
            content_type=content_type,
            kind=kind,  # type: ignore[arg-type]
            size_bytes=len(data),
            sha256="ab" * 32,
            storage_refs=[AttachmentStorageRef(storage_type="local", path=str(path))],
        )

    def turn(
        self, task_id: str, files: list[UserTaskAttachment], *, conversation_id: str = CONVERSATION, person: str = PERSON
    ) -> None:
        self.store.ensure_thread(
            conversation_id=conversation_id,
            team_id=TEAM,
            owner_canonical_user_id=person,
            owner_agent_id=LEAD_ID,
            owner_agent_name="TeamLead",
            requester_name=person,
            requester_id=person,
            root_task_id=task_id,
        )
        self.store.append_turn(
            conversation_id=conversation_id,
            task_id=task_id,
            followup_to_task_id=None,
            owner_agent_id=LEAD_ID,
            requester_name=person,
            requester_id=person,
            correlation_id=f"corr_{task_id}",
            problem_text=f"Request {task_id}",
            owner_canonical_user_id=person,
            attachments=[file.model_dump(mode="json") for file in files],
        )

    def task(
        self,
        task_id: str,
        *,
        files: list[UserTaskAttachment] | None = None,
        conversation_id: str | None = CONVERSATION,
        parent_task_id: str | None = None,
    ) -> None:
        task = Task(
            task_id=task_id,
            task_type=MessageMetaType.REQUEST,
            problem_statement=f"Request {task_id}",
            parent_task_id=parent_task_id,
            attachments=files or [],
        )
        header = MessageHeader(from_agent_id="gateway", response_routing_key="gateway", agent_signature="")
        assert self.manager.add_task(task, header, _meta(conversation_id), owner_person_id=PERSON)

    def use(self, task_id: str, *, is_team_lead: bool = True, supports_image_input: bool = True) -> None:
        """Run the following tool calls as this instance's agent, in the turn of ``task_id``."""

        set_agent_ref(
            SimpleNamespace(
                id=LEAD_ID,
                team_id=TEAM,
                is_team_lead=is_team_lead,
                authorization_config={"enabled": False},
                task_manager=self.manager,
                _conversation_store=self.store,
                instance_root=str(self.layout.root),
                workspace_dir=str(self.layout.workspaces_dir),
                supports_image_input=supports_image_input,
                supports_file_input=True,
                test_task_id=task_id,
            )
        )


@pytest.fixture
def instance(tmp_path: Path) -> _Instance:
    """Turn 1 sent notes and a photo; turn 2 (``ROOT``, running) sent a list."""

    instance = _Instance(tmp_path)
    earlier = [
        instance.file("att_notes", "notes.txt", NOTES, content_type="text/plain", kind="document"),
        instance.file("att_photo", "photo.png"),
    ]
    instance.turn("task_turn_1", earlier)
    current = [instance.file("att_list", "list.png")]
    instance.turn(ROOT, current)
    instance.task(ROOT, files=current)
    return instance


async def _call(tool: Any, tool_input: dict[str, Any]) -> tuple[str, dict[str, Any], list[dict[str, Any]]]:
    """Run a tool the way the agent does; its status, its JSON text block and its other blocks."""

    result = None
    async for event in tool.stream({"toolUseId": "call-1", "name": tool.tool_name, "input": tool_input}, {}):
        result = event
    assert result is not None
    content = result.tool_result["content"]
    return result.tool_result["status"], json.loads(content[0]["text"]), content[1:]


def _ids(listed: list[dict[str, Any]]) -> list[str]:
    return [item["attachment_id"] for item in listed]


@pytest.mark.asyncio
async def test_an_earlier_file_is_opened_by_id_without_showing_where_it_is_stored(instance: _Instance) -> None:
    instance.use(ROOT)

    status, inspected, _ = await _call(inspect_user_attachment, {"attachment_id": "att_notes"})
    copied_status, copied, _ = await _call(
        inspect_user_attachment, {"attachment_id": "att_notes", "copy_to_workspace": True}
    )
    image_status, image_label, image_blocks = await _call(load_local_media_for_model_context, {"attachment_id": "att_photo"})
    document_status, document_label, document_blocks = await _call(
        load_local_media_for_model_context, {"attachment_id": "att_notes", "purpose": "Read the notes."}
    )

    assert status == "success"
    assert inspected["attachment"] == {
        "attachment_id": "att_notes",
        "filename": "notes.txt",
        "content_type": "text/plain",
        "kind": "document",
        "size_bytes": len(NOTES),
        "turn_index": 1,
    }
    assert [line["text"] for line in inspected["inspection"]["lines"]] == ["line one", "line two"]
    assert "local_path" not in inspected
    # A copy in the person's own folder, for shell or Python.
    assert copied_status == "success"
    assert Path(copied["workspace_path"]).read_bytes() == NOTES
    assert copied["workspace_path"].startswith(str(instance.layout.person_dir(PERSON)))
    assert (image_status, image_blocks) == ("success", [{"image": {"format": "png", "source": {"bytes": PNG}}}])
    assert (image_label["attachment_id"], image_label["filename"], image_label["media_kind"]) == (
        "att_photo",
        "photo.png",
        "image",
    )
    assert document_status == "success" and document_label["purpose"] == "Read the notes."
    assert document_blocks == [{"document": {"format": "txt", "name": "notes", "source": {"bytes": NOTES}}}]
    for result in (inspected, image_label, document_label):
        assert "path" not in result and str(instance.layout.uploads_dir) not in json.dumps(result)


@pytest.mark.asyncio
async def test_an_id_the_task_may_not_use_is_not_found_and_lists_the_files_it_may_use(instance: _Instance) -> None:
    # The same person's other conversation, and another person's.
    instance.turn("task_elsewhere", [instance.file("att_elsewhere", "elsewhere.png")], conversation_id="conv_alice_2")
    instance.turn("task_bob", [instance.file("att_bob", "bob.png")], conversation_id="conv_bob", person=OTHER_PERSON)
    instance.use(ROOT)

    for attachment_id in ("att_elsewhere", "att_bob", "../att_notes", "att_unknown"):
        for tool in (inspect_user_attachment, load_local_media_for_model_context):
            status, payload, blocks = await _call(tool, {"attachment_id": attachment_id})

            assert (status, blocks) == ("error", [])
            assert payload["error"].startswith("No file with this attachment_id was sent")
            # The request's files, then the earlier ones, never another conversation's.
            assert _ids(payload["available_attachments"]) == ["att_list", "att_photo", "att_notes"]
            assert payload["available_attachments"][1]["turn_index"] == 1
            assert "storage_refs" not in json.dumps(payload) and str(instance.layout.root) not in json.dumps(payload)


@pytest.mark.asyncio
@pytest.mark.parametrize("signed_for", [OTHER_PERSON, None])
async def test_a_request_not_signed_for_the_conversations_person_cannot_open_its_earlier_files(
    instance: _Instance, signed_for: str | None
) -> None:
    """The person comes from the signed task context, never from the gateway context's ``canonical_user_id``."""

    current = instance.file("att_new", "new.png")
    instance.turn("task_turn_3", [current])
    signed = context_signer().issue(team_id=TEAM, person_id=signed_for, person_roles=("member",)) if signed_for else None
    meta = MessageMeta(
        conversation_id=CONVERSATION,
        requester_type=RequesterType.USER_REQUESTER,
        gateway_context=GatewayContext(canonical_user_id=PERSON, task_context=signed),
    )
    task = Task(task_id="task_turn_3", task_type=MessageMetaType.REQUEST, problem_statement="Again.", attachments=[current])
    header = MessageHeader(from_agent_id="gateway", response_routing_key="gateway", agent_signature="")
    assert instance.manager.add_task(task, header, meta, owner_person_id=signed_for)
    instance.use("task_turn_3")

    for tool in (inspect_user_attachment, load_local_media_for_model_context):
        status, payload, blocks = await _call(tool, {"attachment_id": "att_photo"})

        assert (status, blocks) == ("error", [])
        assert payload["error"].startswith("No file with this attachment_id was sent")
        # Only the request's own file: the conversation's earlier files are not offered.
        assert _ids(payload["available_attachments"]) == ["att_new"]
    status, _payload, _ = await _call(inspect_user_attachment, {"attachment_id": "att_new"})
    assert status == "success"


@pytest.mark.asyncio
async def test_the_files_offered_when_an_id_is_not_found_are_bounded(instance: _Instance) -> None:
    # A newer turn of the conversation (another request) with 25 files.
    instance.turn("task_turn_3", [instance.file(f"att_new_{index:02d}", f"new_{index}.png") for index in range(25)])
    instance.use(ROOT)

    _status, payload, _ = await _call(inspect_user_attachment, {"attachment_id": "att_unknown"})

    # The request's own file first, then the newest of the others.
    assert _ids(payload["available_attachments"]) == ["att_list"] + [f"att_new_{index:02d}" for index in range(24, 5, -1)]


@pytest.mark.asyncio
async def test_a_removed_upload_says_it_is_no_longer_stored(instance: _Instance) -> None:
    (instance.layout.uploads_dir / "session_alice" / "att_photo").unlink()
    instance.use(ROOT)

    for tool in (inspect_user_attachment, load_local_media_for_model_context):
        status, payload, blocks = await _call(tool, {"attachment_id": "att_photo"})

        assert (status, blocks) == ("error", [])
        assert "no longer stored" in payload["error"] and "send it again" in payload["error"]
        assert payload["attachment"]["attachment_id"] == "att_photo" and "storage_refs" not in payload["attachment"]


@pytest.mark.asyncio
async def test_a_file_with_no_copy_in_this_instances_uploads_needs_a_remote_copy(
    instance: _Instance, tmp_path: Path
) -> None:
    """A record naming a file outside ``state/uploads`` (another machine's, or not the gateway's) is never opened."""

    outside = tmp_path / "elsewhere" / "scan.png"
    outside.parent.mkdir()
    outside.write_bytes(PNG)
    record = instance.file("att_scan", "scan.png").model_copy(
        update={"storage_refs": [AttachmentStorageRef(storage_type="local", path=str(outside))]}
    )
    instance.turn("task_turn_3", [record])
    instance.task("task_turn_3", files=[record])
    instance.use("task_turn_3")

    for tool in (inspect_user_attachment, load_local_media_for_model_context):
        status, payload, blocks = await _call(tool, {"attachment_id": "att_scan"})

        assert (status, blocks) == ("remote_copy_required", [])
        assert payload["team_lead_action"].startswith("Request a gateway-backed NATS Object Store mirror")
        assert str(outside) not in json.dumps(payload)


@pytest.mark.asyncio
async def test_a_specialist_opens_its_own_tasks_files_and_never_the_conversations(instance: _Instance) -> None:
    chosen = instance.file("att_chosen", "chosen.png")
    instance.task("task_specialist", files=[chosen], parent_task_id=ROOT)
    instance.use("task_specialist", is_team_lead=False)

    status, _payload, blocks = await _call(load_local_media_for_model_context, {"attachment_id": "att_chosen"})
    refused_status, refused, _ = await _call(inspect_user_attachment, {"attachment_id": "att_notes"})

    assert status == "success" and blocks[0]["image"]["source"]["bytes"] == PNG
    assert refused_status == "error" and _ids(refused["available_attachments"]) == ["att_chosen"]


@pytest.mark.asyncio
async def test_a_guidance_file_is_opened_by_id_after_the_guidance_was_read_and_after_a_restart(
    instance: _Instance,
) -> None:
    """The tools read the task's saved history, not the guidance waiting to be read."""

    guidance = TaskSteering(
        task_id=ROOT,
        steering_type=TaskSteeringType.PROACTIVE_GUIDANCE,
        guidance_text="Use this label too.",
        attachments=[instance.file("att_label", "label.png")],
    )
    assert await _record_guidance_history(SimpleNamespace(task_manager=instance.manager), ROOT, guidance)

    for restarted in (False, True):
        if restarted:
            instance.manager = TaskManager(db_path=str(instance.layout.root / "tasks.db"))
            instance.manager.set_owner_identity(LEAD_ID, "TeamLead", TEAM, "TeamArchitect")
            assert instance.manager.restore_runtime_state()
        instance.use(ROOT)

        status, label, blocks = await _call(load_local_media_for_model_context, {"attachment_id": "att_label"})

        assert status == "success" and label["attachment_id"] == "att_label" and blocks[0]["image"]["source"]["bytes"] == PNG


@pytest.mark.asyncio
async def test_a_plan_step_opens_its_requests_files_and_the_earlier_ones(instance: _Instance) -> None:
    # A step is a new top-level task with no files and a fresh conversation id.
    instance.task("task_step", conversation_id="plan_step_conv_1")
    instance.manager.plan_state.plan_step_task_links["task_step"] = PlanStepTaskLink(
        root_task_id=ROOT, plan_id="plan_1", step_id="s1", step_task_id="task_step"
    )
    instance.use("task_step")

    for attachment_id in ("att_list", "att_photo"):
        status, label, _ = await _call(load_local_media_for_model_context, {"attachment_id": attachment_id})
        assert status == "success" and label["attachment_id"] == attachment_id


@pytest.mark.asyncio
async def test_the_media_loader_takes_exactly_one_file_and_checks_the_model_by_id_too(instance: _Instance) -> None:
    spec = load_local_media_for_model_context.tool_spec["inputSchema"]["json"]
    assert {"path", "attachment_id"} <= set(spec["properties"]) and not set(spec.get("required", [])) & {
        "path",
        "attachment_id",
    }
    instance.use(ROOT, supports_image_input=False)

    neither = await _call(load_local_media_for_model_context, {})
    both = await _call(load_local_media_for_model_context, {"attachment_id": "att_list", "path": "list.png"})
    unsupported = await _call(load_local_media_for_model_context, {"attachment_id": "att_list"})

    for status, payload, _ in (neither, both):
        assert status == "error" and payload["error"].startswith("Name exactly one file")
    status, payload, blocks = unsupported
    assert (status, blocks) == ("error", []) and payload["capability_field"] == "supports_image_input"
    assert payload["attachment_id"] == "att_list" and "path" not in payload


def test_logging_is_quiet_for_a_found_file(instance: _Instance, caplog: pytest.LogCaptureFixture) -> None:
    """Only a refused or missing file is logged (ids only)."""

    instance.use(ROOT)
    with caplog.at_level(logging.INFO, logger="custom_tools.attachment_uploads"):
        found = inspect_user_attachment._tool_func(attachment_id="att_list")
        missing = inspect_user_attachment._tool_func(attachment_id="att_bob")

    assert found["status"] == "success" and missing["status"] == "error"
    assert [record.getMessage().split("event=")[1].split()[0] for record in caplog.records] == ["attachment_id_not_found"]
