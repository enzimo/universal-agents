"""A follow-up in the same conversation finds and opens a file sent in an earlier turn.

The reported case: a person sent a photo of a recipe and the team lead read
it; the next message in the same conversation ("use the photo from before")
could no longer find the photo. This runs the real path end to end: each
turn is admitted into a real conversation store
(``conversation_runtime.ensure_task_conversation_context``), the follow-up's
prompt is assembled by the real context engine, and the follow-up's tools
open the file by its id the way the agent calls them (through Strands'
tool input validation).
"""

from __future__ import annotations

import hashlib
import json
import logging
import struct
import zlib
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from custom_agents.context_engine import (
    ContextAssemblyRequest,
    ContextEngineAdapters,
    DefaultContextEngine,
)
from custom_agents.conversation_runtime import ensure_task_conversation_context
from custom_agents.conversation_store import ConversationStore
from custom_agents.task_manager import TaskManager
from custom_messages.contracts.attachments_v1 import AttachmentStorageRef, UserTaskAttachment
from custom_messages.contracts.routing_v1 import GatewayContext, MessageHeader, MessageMeta
from custom_messages.contracts.task_v1 import Task
from custom_messages.enums import MessageMetaType, RequesterType
from custom_tools.local_media_context_tool import load_local_media_for_model_context
from custom_tools.runtime_context import _agent_ctx
from custom_tools.user_attachment_tools import inspect_user_attachment
from runtime_settings.instance_layout import InstanceLayout
from tests.broker_access_support import context_signer
from tests.change_approval_support import use_signed_contexts
from tests.task_work_support import set_agent_ref

TEAM = "team_followup"
PERSON = "alice"
LEAD_ID = "lead_followup"
PHOTO_ID = "att_photo1"


def png_bytes() -> bytes:
    """A real one-pixel PNG, so the image block holds a file a model could decode."""

    def chunk(kind: bytes, data: bytes) -> bytes:
        return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data) & 0xFFFFFFFF)

    header = struct.pack(">IIBBBBB", 1, 1, 8, 2, 0, 0, 0)
    pixels = zlib.compress(b"\x00\xff\x80\x00")
    return b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", header) + chunk(b"IDAT", pixels) + chunk(b"IEND", b"")


@pytest.fixture(autouse=True)
def _signed_contexts(monkeypatch: pytest.MonkeyPatch):
    use_signed_contexts(monkeypatch)
    _agent_ctx.set(None)
    yield
    _agent_ctx.set(None)


def _meta(conversation_id: str | None) -> MessageMeta:
    # The person comes only from the gateway-signed task context.
    signed = context_signer().issue(team_id=TEAM, person_id=PERSON, person_roles=("member",))
    return MessageMeta(
        from_agent_name="alice",
        conversation_id=conversation_id,
        requester_type=RequesterType.USER_REQUESTER,
        gateway_context=GatewayContext(canonical_user_id=PERSON, task_context=signed),
    )


def _admit(store: ConversationStore, task: Task, conversation_id: str | None) -> MessageMeta:
    return ensure_task_conversation_context(
        conversation_store=store,
        team_id=TEAM,
        owner_agent_id=LEAD_ID,
        owner_agent_name="TeamLead",
        task_request=task,
        msg_header=MessageHeader(from_agent_id="gateway", agent_signature=""),
        msg_meta=_meta(conversation_id),
        owner_person_id=PERSON,
        owner_named_agent_type="TeamArchitect",
    )


class _CountingModel:
    """Token counts for the context budget, without a model provider."""

    async def count_tokens(self, messages, tool_specs=None, system_prompt=None, system_prompt_content=None) -> int:
        return max(1, len(json.dumps(messages, default=str)) // 4)


def _engine(task_manager: TaskManager, store: ConversationStore, layout: InstanceLayout) -> DefaultContextEngine:
    return DefaultContextEngine(
        agent_id=LEAD_ID,
        logger=logging.getLogger("test.followup"),
        task_manager=task_manager,
        conversation_store=store,
        conversation_compactor=None,
        context_window_tokens=200_000,
        reserved_output_tokens=0,
        model=_CountingModel(),
        workspace_dir=str(layout.workspaces_dir),
        instance_root=str(layout.root),
        is_team_lead=True,
        adapters=ContextEngineAdapters(
            build_request_time_context=lambda meta: "",
            build_request_conversation_context=lambda meta, text: "",
            build_team_routing_prompt_context=lambda: "",
            verified_person_of=lambda meta: PERSON,
        ),
    )


async def _call_tool(tool: Any, tool_input: dict[str, Any]) -> dict[str, Any]:
    """Run a tool the way the agent does: Strands validates the input against the tool's spec first."""

    result = None
    async for event in tool.stream({"toolUseId": "call-1", "name": tool.tool_name, "input": tool_input}, {}):
        result = event
    assert result is not None
    return result.tool_result


@pytest.mark.asyncio
async def test_a_follow_up_finds_and_opens_the_photo_sent_in_the_first_turn(tmp_path: Path) -> None:
    layout = InstanceLayout.at(tmp_path / "instance")
    layout.ensure_directories()
    photo = png_bytes()
    photo_path = layout.uploads_dir / "session_alice" / f"{PHOTO_ID}.png"
    photo_path.parent.mkdir(parents=True)
    photo_path.write_bytes(photo)
    attachment = UserTaskAttachment(
        attachment_id=PHOTO_ID,
        filename="tortellini.png",
        content_type="image/png",
        kind="image",
        size_bytes=len(photo),
        sha256=hashlib.sha256(photo).hexdigest(),
        storage_refs=[AttachmentStorageRef(storage_type="local", path=str(photo_path))],
    )
    store = ConversationStore(
        db_path=str(layout.root / "conversations.db"), artifacts_dir=layout.conversation_artifacts_dir
    )

    # Turn 1: the photo, read and answered.
    first = Task(
        task_id="task_turn_1",
        task_type=MessageMetaType.REQUEST,
        problem_statement="Parse the recipe in this photo.",
        attachments=[attachment],
    )
    first_meta = _admit(store, first, None)
    store.update_turn_completion(
        task_id=first.task_id,
        task_status="completed",
        final_answer_text="Tortellini: flour, eggs, ricotta.",
        turn_summary_text="Read the recipe from the photo.",
    )

    # Turn 2: a follow-up with no files of its own.
    second = Task(
        task_id="task_turn_2",
        task_type=MessageMetaType.REQUEST,
        problem_statement="Use the photo from before to make a shopping list.",
    )
    second_meta = _admit(store, second, first_meta.conversation_id)
    task_manager = TaskManager(db_path=str(layout.root / "tasks.db"))
    task_manager.set_owner_identity(LEAD_ID, "TeamLead", TEAM, "TeamArchitect")
    header = MessageHeader(from_agent_id="gateway", response_routing_key="gateway", agent_signature="")
    task_manager.add_task(second, header, second_meta, owner_person_id=PERSON)

    assembly = _engine(task_manager, store, layout).assemble(
        ContextAssemblyRequest(
            incoming_content=second,
            incoming_header=header,
            incoming_meta=second_meta,
            identity_meta=second_meta,
            execution_task_id=second.task_id,
            incoming_message_json=json.dumps({"msg_content": {"task_id": second.task_id}}),
        )
    )
    # The follow-up's prompt names the earlier photo and its id, with no storage path.
    assert PHOTO_ID in assembly.prompt
    assert "tortellini.png" in assembly.prompt
    assert str(layout.uploads_dir) not in assembly.prompt

    set_agent_ref(
        SimpleNamespace(
            id=LEAD_ID,
            team_id=TEAM,
            is_team_lead=True,
            authorization_config={"enabled": False},
            task_manager=task_manager,
            _conversation_store=store,
            instance_root=str(layout.root),
            workspace_dir=str(layout.workspaces_dir),
            supports_image_input=True,
            test_task_id=second.task_id,  # the tool calls are the follow-up's turn
        )
    )
    inspected = await _call_tool(inspect_user_attachment, {"attachment_id": PHOTO_ID})
    assert inspected["status"] == "success", inspected
    assert json.loads(inspected["content"][0]["text"])["attachment"]["attachment_id"] == PHOTO_ID

    loaded = await _call_tool(load_local_media_for_model_context, {"attachment_id": PHOTO_ID})
    assert loaded["status"] == "success", loaded
    assert loaded["content"][1] == {"image": {"format": "png", "source": {"bytes": photo}}}
