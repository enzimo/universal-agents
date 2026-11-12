"""A photo sent in one turn stays usable in the next turn of the same conversation.

Regression for the reported defect: a person sends a photo, gets an answer,
then asks a follow-up about it, and the team lead can no longer name or open
the photo. The conversation record kept each turn's text only, and every way
to look a file up (the prompt's attachment list, ``inspect_user_attachment``,
``load_local_media_for_model_context``) saw only the current request's own
message.

The test uses real parts end to end: the gateway's own upload storage
(``assistant_gateway.inbound_attachments``), an Ed25519-signed task context
for the person, the real admission path
(``task_message_runtime._handle_task_request``), real SQLite task and
conversation stores, the lead's real ``DefaultContextEngine`` and the real
tools. Turn 2 runs in a restarted lead over the same databases, so nothing
depends on memory kept from turn 1.

The scenario itself is checked without an expected-failure marker
(``test_the_follow_up_scenario_is_set_up_as_a_real_second_turn``), so a
broken setup fails at once instead of passing as the expected failure.

Covers, for turn 2 (the follow-up, which has no files of its own):
- its prompt lists the photo from turn 1 by name and id;
- ``inspect_user_attachment`` opens the photo by id;
- ``load_local_media_for_model_context`` loads the photo by id;
- ``get_conversation_context_slice`` shows the photo on turn 1;
- none of these show the model where the upload is stored.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest
import pytest_asyncio

from access_control.identity import ENV_VERIFY_KEYS
from access_control.ownership import request_context_id
from assistant_gateway.inbound_attachments import (
    InboundAttachmentStoragePolicy,
    store_inbound_attachment_bytes_with_policy,
)
from custom_agents import task_message_runtime
from custom_agents.context_engine import ContextAssemblyRequest
from custom_agents.conversation_runtime import finalize_conversation_turn
from custom_agents.conversation_store import ConversationStore
from custom_agents.task_manager import TaskManager
from custom_messages.contracts.attachments_v1 import UserTaskAttachment
from custom_messages.contracts.envelope_v1 import CommsMessage
from custom_messages.contracts.routing_v1 import GatewayContext, MessageHeader, MessageMeta
from custom_messages.contracts.task_v1 import Task
from custom_messages.enums import MessageMetaType, ProgressState, RequesterType
from custom_tools.local_media_context_tool import load_local_media_for_model_context
from custom_tools.providers.context_memory import get_conversation_context_slice
from custom_tools.user_attachment_tools import inspect_user_attachment
from runtime_settings.instance_layout import InstanceLayout
from tests.broker_access_support import context_signer, context_verify_keys, forget_task_context_keys
from tests.task_work_support import use_agent

pytestmark = pytest.mark.integration

# The admission harness (tests.test_turn_action_outbox._make_agent) runs in this team.
TEAM = "test_team"
ANN = "usr_ann"
CONVERSATION = "conv_photo"
SESSION = "web:ann-session"
FIRST, FOLLOW_UP = "task_photo", "task_follow_up"
PHOTO_NAME = "garden plant.png"
PHOTO_BYTES = b"\x89PNG\r\n\x1a\n" + bytes(range(64))


@pytest.fixture(autouse=True)
def _verify_keys(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv(ENV_VERIFY_KEYS, context_verify_keys())
    forget_task_context_keys()
    yield
    forget_task_context_keys()


@dataclass(frozen=True)
class _FollowUp:
    """Turn 2's lead (restarted over turn 1's databases) and the photo turn 1 received."""

    lead: Any
    layout: InstanceLayout
    photo: UserTaskAttachment
    upload: Path


def _lead(tmp_path: Path, layout: InstanceLayout):
    """The team lead as the admission path sees it, with its stores in the instance's task database."""

    from tests.test_turn_action_outbox import _make_agent

    db_path = str(tmp_path / "tasks.db")
    tasks = TaskManager(db_path=db_path)
    tasks.set_owner_identity("lead", "Lead", TEAM, "TeamArchitect")
    agent = _make_agent(tmp_path, task_manager=tasks, instance_root=str(layout.root))
    agent._conversation_store = ConversationStore(db_path, artifacts_dir=tmp_path / "conversation_artifacts")
    agent.supports_image_input = True
    # The engine reads the conversation store it was built with.
    agent._create_context_engine()
    return agent


async def _admit(agent, *, task_id: str, text: str, attachments: list[UserTaskAttachment]) -> None:
    task = Task(task_id=task_id, task_type=MessageMetaType.REQUEST, problem_statement=text, attachments=attachments)
    # As the gateway signs a new request: naming its task and conversation, with
    # an id bound to the request and its files, vouches for those files.
    signed = context_signer().issue(
        team_id=TEAM,
        person_id=ANN,
        person_roles=("member",),
        root_task_id=task_id,
        conversation_id=CONVERSATION,
        context_id=request_context_id(task),
    )
    await task_message_runtime._handle_task_request(
        agent,
        task,
        MessageHeader(from_agent_id="gateway", response_routing_key="test_team_gateway_response"),
        MessageMeta(
            conversation_id=CONVERSATION,
            msg_correlation_id=f"corr_{task_id}",
            requester_type=RequesterType.USER_REQUESTER,
            gateway_context=GatewayContext(canonical_user_id=ANN, session_key=SESSION, task_context=signed),
        ),
    )


async def _answer(agent, task_id: str, answer: str) -> None:
    """Finish a turn the way the runtime does, recording the answer in the conversation."""

    info = agent.task_manager.get_task_info(task_id)
    info.task.answer = answer
    await finalize_conversation_turn(
        conversation_store=agent._conversation_store,
        conversation_compactor=None,
        conversation_artifact_offloader=None,
        strands_messages=[],
        task_manager=agent.task_manager,
        task_info=info,
        task_status=ProgressState.COMPLETED.value,
        logger=logging.getLogger("test.conversation_attachments"),
        agent_id=agent.id,
        team_id=TEAM,
        named_agent_type="TeamArchitect",
    )


@pytest_asyncio.fixture
async def follow_up(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> _FollowUp:
    # The test plays each turn itself, so admission does not start the queue
    # drain that would run a model turn in the background.
    monkeypatch.setattr(task_message_runtime, "schedule_pending_work", lambda _agent: None)
    layout = InstanceLayout.at(tmp_path / "instance")
    layout.ensure_directories()
    stored = store_inbound_attachment_bytes_with_policy(
        data=PHOTO_BYTES,
        filename=PHOTO_NAME,
        content_type="image/png",
        policy=InboundAttachmentStoragePolicy(cache_dir=layout.uploads_dir),
        session_key=SESSION,
        conversation_id=CONVERSATION,
        origin="web_chat",
    )

    first_lead = _lead(tmp_path, layout)
    await _admit(first_lead, task_id=FIRST, text="What plant is this?", attachments=[stored.attachment])
    await _answer(first_lead, FIRST, "It looks like a young fig tree.")

    lead = _lead(tmp_path, layout)
    await _admit(lead, task_id=FOLLOW_UP, text="How often should I water the plant in that photo?", attachments=[])
    use_agent(lead, FOLLOW_UP)
    return _FollowUp(lead=lead, layout=layout, photo=stored.attachment, upload=stored.local_path)


def _follow_up_prompt(lead) -> Any:
    info = lead.task_manager.get_task_info(FOLLOW_UP)
    return lead._context_engine.assemble(
        ContextAssemblyRequest(
            incoming_content=info.task,
            incoming_header=info.header,
            incoming_meta=info.meta,
            identity_meta=info.meta,
            execution_task_id=FOLLOW_UP,
            incoming_message_json=CommsMessage(msg_content=info.task, msg_meta=info.meta).model_dump_json(),
        )
    )


def _payload(result: dict) -> dict:
    return json.loads(result["content"][0]["text"])


def _sections(assembly: Any) -> dict[str, str]:
    return {section.name: section.text for section in assembly.sections}


# The regression tests below are expected to fail only at their own checks
# (an assertion or a missing key), never in setup or with another error.
_MISSING_BEHAVIOR = (AssertionError, KeyError)


@pytest.mark.asyncio
async def test_the_follow_up_scenario_is_set_up_as_a_real_second_turn(follow_up: _FollowUp) -> None:
    store = follow_up.lead._conversation_store
    first = store.get_turn_by_task_id(FIRST)
    second = store.get_turn_by_task_id(FOLLOW_UP)

    # Both turns are Ann's, in one conversation, and turn 2 follows turn 1.
    assert store.get_thread(CONVERSATION)["owner_canonical_user_id"] == ANN
    assert (first["conversation_id"], first["owner_canonical_user_id"]) == (CONVERSATION, ANN)
    assert (second["conversation_id"], second["owner_canonical_user_id"]) == (CONVERSATION, ANN)
    assert second["followup_to_task_id"] == FIRST
    assert (first["task_status"], first["final_answer_text"]) == ("completed", "It looks like a young fig tree.")
    # The photo's bytes are in the instance's upload folder.
    assert follow_up.upload.resolve().is_relative_to(follow_up.layout.uploads_dir.resolve())
    assert follow_up.upload.read_bytes() == PHOTO_BYTES
    # Turn 1's text reaches the follow-up; its photo is what went missing.
    assert "What plant is this?" in _sections(_follow_up_prompt(follow_up.lead))["conversation_thread_context"]


@pytest.mark.xfail(strict=True, raises=_MISSING_BEHAVIOR, reason="regression: earlier uploads are not listed in follow-up turns")
@pytest.mark.asyncio
async def test_the_follow_up_prompt_lists_the_photo_from_turn_one(follow_up: _FollowUp) -> None:
    assembly = _follow_up_prompt(follow_up.lead)

    listed = _sections(assembly).get("conversation_attachments", "")
    assert follow_up.photo.attachment_id in listed
    assert PHOTO_NAME in listed
    assert str(follow_up.upload) not in assembly.prompt


@pytest.mark.xfail(strict=True, raises=_MISSING_BEHAVIOR, reason="regression: earlier uploads cannot be inspected by id")
@pytest.mark.asyncio
async def test_the_follow_up_turn_inspects_the_photo_by_id(follow_up: _FollowUp) -> None:
    result = inspect_user_attachment._tool_func(attachment_id=follow_up.photo.attachment_id)

    assert result["status"] == "success", result
    payload = _payload(result)
    assert payload["attachment"]["attachment_id"] == follow_up.photo.attachment_id
    assert payload["attachment"]["filename"] == PHOTO_NAME
    assert str(follow_up.upload) not in result["content"][0]["text"]


@pytest.mark.xfail(strict=True, raises=_MISSING_BEHAVIOR, reason="regression: earlier uploads cannot be loaded for the model by id")
@pytest.mark.asyncio
async def test_the_follow_up_turn_loads_the_photo_for_the_model_by_id(follow_up: _FollowUp) -> None:
    schema = load_local_media_for_model_context.tool_spec["inputSchema"]["json"]
    assert "attachment_id" in schema["properties"]

    result = load_local_media_for_model_context._tool_func(attachment_id=follow_up.photo.attachment_id)

    assert result["status"] == "success", result
    label, block = result["content"]
    assert block == {"image": {"format": "png", "source": {"bytes": PHOTO_BYTES}}}
    assert str(follow_up.upload) not in label["text"]


@pytest.mark.xfail(strict=True, raises=_MISSING_BEHAVIOR, reason="regression: conversation turns do not record their uploads")
@pytest.mark.asyncio
async def test_the_conversation_slice_shows_the_photo_on_turn_one(follow_up: _FollowUp) -> None:
    text = get_conversation_context_slice._tool_func(conversation_id=CONVERSATION, mode="summary")

    turns = {turn["task_id"]: turn for turn in json.loads(text)["turns"]}
    first = turns[FIRST]
    assert first["attachment_count"] == 1
    assert [item["attachment_id"] for item in first["attachments"]] == [follow_up.photo.attachment_id]
    assert first["attachments"][0]["filename"] == PHOTO_NAME
    assert turns[FOLLOW_UP]["attachment_count"] == 0
    assert str(follow_up.upload) not in text
