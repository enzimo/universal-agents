"""The conversation tools show each turn's files without where they are stored.

Turns now keep the files the person sent (``storage.conversation_repository``).
The tools that return turns to a model (``get_conversation_summary``,
``get_conversation_turns`` and ``get_conversation_context_slice``) must show a
file's name, type, size and id, never its path in the instance's uploads
folder or its digest. The tools run for the conversation's owner through the
real signed-context check.
"""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from custom_agents.conversation_store import ConversationStore
from custom_agents.task_manager import TaskManager
from custom_messages.contracts.attachments_v1 import AttachmentStorageRef, UserTaskAttachment
from custom_messages.contracts.routing_v1 import GatewayContext, MessageHeader, MessageMeta
from custom_messages.contracts.task_v1 import Task
from custom_messages.enums import MessageMetaType, RequesterType
from custom_tools.providers.context_memory import (
    get_conversation_context_slice,
    get_conversation_summary,
    get_conversation_turns,
)
from custom_tools.runtime_context import _agent_ctx
from runtime_settings.instance_layout import InstanceLayout
from tests.broker_access_support import context_signer
from tests.change_approval_support import use_signed_contexts
from tests.task_work_support import set_agent_ref

TEAM = "team_tools"
PERSON = "alice"
CONVERSATION = "conv_tools"
TASK = "task_tools"


@pytest.fixture(autouse=True)
def _signed_contexts(monkeypatch: pytest.MonkeyPatch):
    use_signed_contexts(monkeypatch)
    _agent_ctx.set(None)
    yield
    _agent_ctx.set(None)


def test_the_conversation_tools_never_show_where_a_file_is_stored(tmp_path: Path) -> None:
    layout = InstanceLayout.at(tmp_path / "instance")
    layout.ensure_directories()
    upload = layout.uploads_dir / "session_alice" / "att_receipt"
    receipt = UserTaskAttachment(
        attachment_id="att_receipt",
        filename="receipt.jpg",
        content_type="image/jpeg",
        kind="image",
        size_bytes=2048,
        sha256="ab" * 32,
        storage_refs=[AttachmentStorageRef(storage_type="local", path=str(upload))],
    )
    store = ConversationStore(db_path=str(layout.root / "conversations.db"), artifacts_dir=layout.conversation_artifacts_dir)
    store.ensure_thread(
        conversation_id=CONVERSATION,
        team_id=TEAM,
        owner_canonical_user_id=PERSON,
        owner_agent_id="lead",
        owner_agent_name="TeamLead",
        requester_name=PERSON,
        requester_id=PERSON,
        root_task_id=TASK,
    )
    store.append_turn(
        conversation_id=CONVERSATION,
        task_id=TASK,
        followup_to_task_id=None,
        owner_agent_id="lead",
        requester_name=PERSON,
        requester_id=PERSON,
        correlation_id="corr_tools",
        problem_text="Here is my receipt.",
        owner_canonical_user_id=PERSON,
        attachments=[receipt.model_dump(mode="json")],
    )
    # The tools run in the person's request, whose signed context names them.
    task_manager = TaskManager(db_path=str(layout.root / "tasks.db"))
    task_manager.set_owner_identity("lead", "TeamLead", TEAM, "TeamArchitect")
    signed = context_signer().issue(team_id=TEAM, person_id=PERSON, person_roles=("member",))
    task_manager.add_task(
        Task(task_id=TASK, task_type=MessageMetaType.REQUEST, problem_statement="Here is my receipt."),
        MessageHeader(from_agent_id="gateway", agent_signature=""),
        MessageMeta(
            conversation_id=CONVERSATION,
            requester_type=RequesterType.USER_REQUESTER,
            gateway_context=GatewayContext(canonical_user_id=PERSON, task_context=signed),
        ),
        owner_person_id=PERSON,
    )
    set_agent_ref(
        SimpleNamespace(
            id="lead",
            team_id=TEAM,
            is_team_lead=True,
            authorization_config={"enabled": False},
            task_manager=task_manager,
            _conversation_store=store,
            instance_root=str(layout.root),
            test_task_id=TASK,
        )
    )

    outputs = {
        "summary": get_conversation_summary(conversation_id=CONVERSATION),
        "turns": get_conversation_turns(conversation_id=CONVERSATION),
        "slice": get_conversation_context_slice(conversation_id=CONVERSATION, mode="full"),
    }

    for name, output in outputs.items():
        payload = json.loads(output)
        assert "error" not in payload, (name, payload)
        assert "att_receipt" in output and "receipt.jpg" in output, name
        assert str(layout.uploads_dir) not in output, name
        assert "storage_refs" not in output and receipt.sha256 not in output, name
    turn = json.loads(outputs["turns"])["turns"][0]
    assert turn["attachments"] == [
        {
            "attachment_id": "att_receipt",
            "filename": "receipt.jpg",
            "content_type": "image/jpeg",
            "kind": "image",
            "size_bytes": 2048,
        }
    ]
