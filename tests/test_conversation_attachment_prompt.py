"""Earlier files stay visible to the team lead in later prompts and in conversation slices.

Uses the real admission path, the real steering handler, real SQLite task and
conversation stores, the gateway's own upload storage and the lead's real
``DefaultContextEngine``, for a team lead working for Ann (signed task
context). Covers:

- the prompt's "Conversation Attachments" list names files from earlier turns
  and files sent with this request's guidance, newest first, by id, name and
  type, and leaves out the files the current request lists itself;
- the list comes from stored records, so files of turns the conversation
  history trimmed stay listed, and its tokens are reserved before the history
  is sized, so the prompt stays within the soft limit;
- the list is left out only when overflow recovery is told to drop it, which
  the default settings do not do; a task without a record gets no list and a
  warning;
- a focused turn (a real plan step or isolated recovery task of the request)
  lists its request's files and earlier turns' files and opens them by id,
  found only through the task manager's links; a step whose request works for
  someone else gets no list and a warning;
- ``get_conversation_context_slice`` and ``get_conversation_turns`` show each
  turn's files (at most 20 per turn in the slice summary, with the count)
  without storage references;
- a turn saved before files were recorded is marked as such in the prompt
  and in slices, not shown as a turn without files;
- no prompt or tool output shows where an upload is stored.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

import pytest
import pytest_asyncio

from custom_agents.context_engine import ContextAssemblyRequest
from custom_agents.conversation_attachments import (
    CONVERSATION_ATTACHMENTS_HEADING,
    CONVERSATION_ATTACHMENTS_SECTION,
    INVENTORY_LIMIT,
    UNRECORDED_TURNS_NOTE,
)
from custom_messages.contracts.attachments_v1 import UserTaskAttachment
from custom_messages.contracts.envelope_v1 import CommsMessage
from custom_messages.contracts.task_v1 import TaskSteering
from custom_messages.enums import TaskSteeringType
from custom_tools.user_attachment_tools import inspect_user_attachment
from custom_tools.providers.context_memory import get_conversation_context_slice, get_conversation_turns
from default_values import AGENT_DEFAULTS
from runtime_settings.instance_layout import InstanceLayout
from storage.turn_attachments import TURN_ATTACHMENT_SUMMARY_FIELDS
from tests.task_work_support import use_agent
from tests.test_conversation_attachments import saved_before_upgrade
from tests.test_conversation_attachments_regression import ANN, CONVERSATION, _admit, _answer, _lead
from tests.test_turn_files_recording import (  # noqa: F401  (autouse fixtures)
    _gateway_meta,
    _no_background_work,
    _send,
    _upload,
    _verify_keys,
    statuses,
)

pytestmark = pytest.mark.integration

FIRST, CURRENT = "task_first", "task_current"


@pytest.fixture
def layout(tmp_path: Path) -> InstanceLayout:
    layout = InstanceLayout.at(tmp_path / "instance")
    layout.ensure_directories()
    return layout


def _request(lead: Any, task_id: str = CURRENT, **flags: Any) -> ContextAssemblyRequest:
    """The request the runtime makes for ``task_id``'s next turn."""

    info = lead.task_manager.get_task_info(task_id)
    return ContextAssemblyRequest(
        incoming_content=info.task,
        incoming_header=info.header,
        incoming_meta=info.meta,
        identity_meta=info.meta,
        execution_task_id=task_id,
        incoming_message_json=CommsMessage(msg_content=info.task, msg_meta=info.meta).model_dump_json(),
        **flags,
    )


def _assemble(lead: Any, task_id: str = CURRENT, **flags: Any) -> Any:
    """The prompt the lead's engine builds for ``task_id``'s next turn."""

    return lead._context_engine.assemble(_request(lead, task_id, **flags))


class _CharacterCountingModel:
    """Counts about four characters per token, as tests.test_context_engine does."""

    def count_tokens(self, messages: Any, **_kwargs: Any) -> int:
        return max(1, len(json.dumps(messages, ensure_ascii=False, default=str)) // 4)


def _section(assembly: Any, name: str) -> str:
    return next((section.text for section in assembly.sections if section.name == name), "")


def _listed_ids(text: str) -> list[str]:
    """The attachment ids the list names, in order."""

    return [line.split("`")[1] for line in text.splitlines() if line.startswith("- `")]


def _earlier_turn(lead: Any, layout: InstanceLayout, index: int, *, files: int = 1, text_size: int = 0) -> list[UserTaskAttachment]:
    """Turn ``index`` of Ann's conversation, saved as the runtime saves a finished turn, with its files."""

    uploads = [_upload(layout, f"turn {index} file {number}.png") for number in range(files)]
    store = lead._conversation_store
    task_id = f"task_turn_{index:02d}"
    store.append_turn(
        conversation_id=CONVERSATION,
        task_id=task_id,
        followup_to_task_id=None,
        owner_agent_id=lead.id,
        requester_name=None,
        requester_id=None,
        correlation_id=None,
        problem_text=f"Earlier request {index} " + "x" * text_size,
        owner_canonical_user_id=ANN,
        attachments=uploads,
    )
    store.update_turn_completion(
        task_id=task_id,
        task_status="completed",
        final_answer_text=f"Earlier answer {index} " + "y" * text_size,
        turn_summary_text=f"Turn {index} was answered.",
    )
    return uploads


@pytest_asyncio.fixture
async def lead(tmp_path: Path, layout: InstanceLayout) -> Any:
    """Ann's team lead after one finished turn (FIRST, with a photo)."""

    agent = _lead(tmp_path, layout)
    await _admit(agent, task_id=FIRST, text="What plant is this?", attachments=[_upload(layout, "garden plant.png")])
    await _answer(agent, FIRST, "A young fig tree.")
    return agent


def _photo_id(lead: Any) -> str:
    return lead._conversation_store.get_turn_by_task_id(FIRST)["attachments"][0]["attachment_id"]


@pytest.mark.usefixtures("statuses")
@pytest.mark.asyncio
async def test_the_prompt_lists_earlier_and_mid_request_files_but_not_the_current_requests(
    lead: Any, layout: InstanceLayout
) -> None:
    page_one = _upload(layout, "page one.png")
    await _admit(lead, task_id=CURRENT, text="Read this recipe for that plant.", attachments=[page_one])
    page_two = _upload(layout, "page two.png")
    guidance = TaskSteering(
        steering_type=TaskSteeringType.PROACTIVE_GUIDANCE,
        target_task_id=CURRENT,
        conversation_id=CONVERSATION,
        guidance_text="Here is page two.",
        idempotency_key="key_page_two",
        attachments=[page_two],
    )
    # As the gateway sends it: a context for this message and its file, naming the request.
    await _send(lead, guidance, _gateway_meta(guidance, task_id=CURRENT))

    assembly = _assemble(lead)

    listed = _section(assembly, CONVERSATION_ATTACHMENTS_SECTION)
    assert listed.startswith(CONVERSATION_ATTACHMENTS_HEADING)
    # The guidance's file (newest, this request's) first, then turn 1's photo; the
    # request's own file is listed under Current Request Attachments instead.
    assert _listed_ids(listed) == [page_two.attachment_id, _photo_id(lead)]
    assert "page two.png (image/png, image" in listed and "sent with this task" in listed
    assert "garden plant.png (image/png, image, " in listed and f"; turn 1, task {FIRST})" in listed
    current = _section(assembly, "current_request")
    assert page_one.attachment_id in current and "local_path" not in current
    # Placed after the conversation history, which the list does not depend on.
    prompt = assembly.prompt
    assert prompt.index("Conversation Thread Context:") < prompt.index(CONVERSATION_ATTACHMENTS_HEADING)
    sources = {item["name"]: item for item in assembly.source_lengths}
    assert sources[CONVERSATION_ATTACHMENTS_SECTION]["pinned"] is True
    assert str(layout.uploads_dir) not in prompt and "storage_refs" not in prompt


@pytest.mark.asyncio
async def test_the_list_outlasts_trimmed_history_and_its_tokens_are_reserved(lead: Any, layout: InstanceLayout) -> None:
    turns = range(2, 2 + INVENTORY_LIMIT + 4)
    earlier = [_earlier_turn(lead, layout, index, text_size=600)[0] for index in turns]
    await _admit(lead, task_id=CURRENT, text="Which of these photos shows the fig?", attachments=[])
    engine = lead._context_engine
    # Count about four characters per token (the test agent's model counts every text as one).
    engine.model = _CharacterCountingModel()
    engine.turn_context_window = None
    # Only sections sized before the history count toward its budget; leave the
    # (unreserved) timing lines out so the check is about this list.
    engine.task_runtime_timing = {"enabled": False}
    engine.reserved_output_tokens = 0
    roomy = _assemble(lead)
    others = roomy.estimated_tokens - engine.estimate_tokens(_section(roomy, "conversation_thread_context"))
    # A window whose soft limit leaves the history room for a few earlier turns, not all of them.
    engine.context_window_tokens = int((others + 600) / engine.soft_context_ratio) + 1

    assembly = _assemble(lead)

    thread = _section(assembly, "conversation_thread_context")
    assert "Context trimmed:" in thread
    assert assembly.estimated_tokens <= assembly.soft_limit_tokens
    assert not [warning for warning in assembly.warnings if warning["warning"] == "context_soft_limit_reached"]
    # The newest files are listed, including those of turns the history left out.
    listed = _section(assembly, CONVERSATION_ATTACHMENTS_SECTION)
    newest = [attachment.attachment_id for attachment in reversed(earlier)][:INVENTORY_LIMIT]
    assert _listed_ids(listed) == newest
    left_out = [index for index in turns if f"task_id=task_turn_{index:02d}" not in thread]
    assert len(left_out) > 4 and f"task task_turn_{left_out[-1]:02d})" in listed
    assert f"Only the newest {INVENTORY_LIMIT} are listed" in listed


@pytest.mark.asyncio
async def test_overflow_recovery_drops_the_list_only_when_told_to(lead: Any, layout: InstanceLayout) -> None:
    await _admit(lead, task_id=CURRENT, text="And the photo?", attachments=[])

    assert _photo_id(lead) in _section(_assemble(lead), CONVERSATION_ATTACHMENTS_SECTION)
    degraded = _assemble(lead, degraded_sections=(CONVERSATION_ATTACHMENTS_SECTION,))
    assert CONVERSATION_ATTACHMENTS_HEADING not in degraded.prompt
    # Not dropped by default: it is bounded and exists so earlier files outlast trimmed history.
    recovery = AGENT_DEFAULTS.context_engine.value["overflow_recovery"]
    assert CONVERSATION_ATTACHMENTS_SECTION not in recovery["degrade_sections"]


def _focused_task(lead: Any, flag: str) -> Any:
    """A plan step or isolated recovery task of CURRENT, as the lead starts one (its own conversation id, no turn)."""

    manager = lead.task_manager
    assert manager.activate_plan_execution(
        CURRENT,
        steps=[{"title": "Look", "objective": "Look at the photos."}, {"title": "Answer", "objective": "Answer."}],
        final_review=False,
    )
    [step] = [info for info in manager.task_state.tasks.values() if manager.is_plan_step_task(info.task.task_id)]
    if flag == "plan_step_task":
        return step
    root = manager.get_task_info(CURRENT)
    meta = root.meta.model_copy(update={"conversation_id": "recov_conv_1", "msg_correlation_id": "recovery_corr_1"})
    return manager.start_isolated_recovery_task(
        CURRENT,
        failure_type="tool_failure",
        failure_summary="The page did not load.",
        recovery_brief="Try another way.",
        header=root.header.model_copy(deep=True),
        meta=meta,
        notify_user=False,
    )


@pytest.mark.parametrize("flag", ["plan_step_task", "isolated_recovery_task"])
@pytest.mark.asyncio
async def test_a_focused_turn_lists_and_opens_its_requests_files(lead: Any, layout: InstanceLayout, flag: str) -> None:
    page = _upload(layout, "page one.png")
    await _admit(lead, task_id=CURRENT, text="Read this recipe for that plant.", attachments=[page])
    focused = _focused_task(lead, flag)
    focused_id = focused.task.task_id
    # The step has a conversation id of its own and no turn; its request is found through the plan links.
    assert focused.meta.conversation_id != CONVERSATION and focused.task.parent_task_id is None
    assert lead.task_manager.resolve_root_task_id(focused_id) == CURRENT

    assembly = _assemble(lead, focused_id, **{flag: True})

    listed = _section(assembly, CONVERSATION_ATTACHMENTS_SECTION)
    # The request's own file (on its turn) first, then turn 1's photo, by turn and task.
    assert _listed_ids(listed) == [page.attachment_id, _photo_id(lead)]
    assert f"turn 2, task {CURRENT})" in listed and f"turn 1, task {FIRST})" in listed
    assert str(layout.uploads_dir) not in assembly.prompt and "storage_refs" not in assembly.prompt
    # The step's tool calls open them by id.
    use_agent(lead, focused_id)
    for attachment_id in (page.attachment_id, _photo_id(lead)):
        assert inspect_user_attachment._tool_func(attachment_id=attachment_id)["status"] == "success"


@pytest.mark.asyncio
async def test_a_step_whose_request_works_for_someone_else_gets_no_list(
    lead: Any, layout: InstanceLayout, caplog: pytest.LogCaptureFixture
) -> None:
    await _admit(lead, task_id=CURRENT, text="And the photo?", attachments=[])
    step = _focused_task(lead, "plan_step_task")
    # As if the request were Bob's: the step may not use its files.
    lead.task_manager.get_task_info(CURRENT).owner_person_id = "usr_bob"

    with caplog.at_level(logging.WARNING):
        assembly = _assemble(lead, step.task.task_id, plan_step_task=True)
        use_agent(lead, step.task.task_id)
        opened = inspect_user_attachment._tool_func(attachment_id=_photo_id(lead))

    assert CONVERSATION_ATTACHMENTS_HEADING not in assembly.prompt
    assert opened["status"] == "error"
    assert (
        f"event=step_request_files_not_usable task_id={step.task.task_id} root_task_id={CURRENT} reason=other_person"
        in caplog.text
    )


@pytest.mark.asyncio
async def test_a_task_without_a_record_gets_no_list_and_a_warning(
    lead: Any, layout: InstanceLayout, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    await _admit(lead, task_id=CURRENT, text="And the photo?", attachments=[])
    request = _request(lead)
    # As if the task's record were gone by the time its prompt is built.
    monkeypatch.setattr(lead._context_engine.task_manager, "get_task_info", lambda task_id: None)

    with caplog.at_level(logging.WARNING):
        assembly = lead._context_engine.assemble(request)

    assert CONVERSATION_ATTACHMENTS_HEADING not in assembly.prompt
    # The rest of the prompt is still built.
    assert "And the photo?" in _section(assembly, "current_request")
    assert "Conversation Thread Context:" in assembly.prompt
    assert f"event=conversation_attachments_task_missing task_id={CURRENT}" in caplog.text


@pytest.mark.asyncio
async def test_a_turn_saved_before_files_were_recorded_is_not_shown_as_a_turn_without_files(
    lead: Any, layout: InstanceLayout
) -> None:
    # Ann sent the photo in a turn saved before turns kept their files.
    saved_before_upgrade(lead._conversation_store, FIRST)
    await _admit(lead, task_id=CURRENT, text="Is the plant in my photo healthy?", attachments=[])
    use_agent(lead, CURRENT)

    listed = _section(_assemble(lead), CONVERSATION_ATTACHMENTS_SECTION)
    assert listed == "\n".join([CONVERSATION_ATTACHMENTS_HEADING, UNRECORDED_TURNS_NOTE.format(turn=1)])

    summary = json.loads(get_conversation_context_slice._tool_func(conversation_id=CONVERSATION, mode="summary"))
    turns = {turn["task_id"]: turn for turn in summary["turns"]}
    assert (turns[FIRST]["attachments_recorded"], turns[FIRST]["attachment_count"]) == (False, None)
    assert (turns[CURRENT]["attachments_recorded"], turns[CURRENT]["attachment_count"]) == (True, 0)
    full = json.loads(get_conversation_context_slice._tool_func(conversation_id=CONVERSATION, mode="full"))
    listed_turns = json.loads(get_conversation_turns._tool_func(conversation_id=CONVERSATION))
    for payload in (full, listed_turns):
        recorded = {turn["task_id"]: turn["attachments_recorded"] for turn in payload["turns"]}
        assert recorded == {FIRST: False, CURRENT: True}


@pytest.mark.asyncio
async def test_slices_and_turns_show_each_turns_files_without_storage(lead: Any, layout: InstanceLayout) -> None:
    many = _earlier_turn(lead, layout, 2, files=INVENTORY_LIMIT + 5)
    await _admit(lead, task_id=CURRENT, text="What did I send?", attachments=[])
    use_agent(lead, CURRENT)

    summary = json.loads(get_conversation_context_slice._tool_func(conversation_id=CONVERSATION, mode="summary"))
    turns = {turn["task_id"]: turn for turn in summary["turns"]}
    assert (turns[FIRST]["attachment_count"], [item["attachment_id"] for item in turns[FIRST]["attachments"]]) == (
        1,
        [_photo_id(lead)],
    )
    crowded = turns["task_turn_02"]
    assert crowded["attachment_count"] == INVENTORY_LIMIT + 5
    assert [item["attachment_id"] for item in crowded["attachments"]] == [
        attachment.attachment_id for attachment in many
    ][:INVENTORY_LIMIT]
    assert (turns[CURRENT]["attachment_count"], turns[CURRENT]["attachments"]) == (0, [])
    assert all(turn["attachments_recorded"] is True for turn in summary["turns"])

    full = json.loads(get_conversation_context_slice._tool_func(conversation_id=CONVERSATION, mode="full"))
    listed = json.loads(get_conversation_turns._tool_func(conversation_id=CONVERSATION))
    for payload in (summary, full, listed):
        for turn in payload["turns"]:
            for item in turn["attachments"]:
                assert set(item) == set(TURN_ATTACHMENT_SUMMARY_FIELDS)
    assert len({turn["task_id"]: turn for turn in full["turns"]}["task_turn_02"]["attachments"]) == INVENTORY_LIMIT + 5
    for text in (json.dumps(summary), json.dumps(full), json.dumps(listed)):
        assert str(layout.uploads_dir) not in text and "storage_refs" not in text
