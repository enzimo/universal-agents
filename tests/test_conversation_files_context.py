"""Prompts name the files a task may use (``DefaultContextEngine`` with ``conversation_attachments``).

These tests assemble real prompts with a real task manager (its plan-step and
recovery links and its saved history), a real SQLite conversation store and
the default context engine. They cover: the team lead's follow-up naming the
newest 20 files of earlier turns however little history the prompt shows;
files received while a request ran (guidance, answers) named with the current
request, for the team lead and for a specialist; plan-step and recovery
prompts naming their request's files; and the earlier list only for the
person who owns the conversation and never for a specialist.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from types import SimpleNamespace

import pytest

from custom_agents.context_engine import ContextAssemblyRequest, ContextEngineAdapters, DefaultContextEngine
from custom_agents.context_engine.base import AssembledContext
from custom_agents.conversation_attachments import EARLIER_FILES_HEADING, REQUEST_FILES_HEADING
from custom_agents.conversation_store import ConversationStore
from custom_agents.steering_history_events import QUERY_ANSWER_FILES_KEY
from custom_agents.task_manager import TaskManager
from custom_agents.task_steering_runtime import _record_guidance_history
from custom_messages.contracts.attachments_v1 import AttachmentStorageRef, UserTaskAttachment
from custom_messages.contracts.routing_v1 import GatewayContext, MessageHeader, MessageMeta
from custom_messages.contracts.runtime_snapshot_v3 import PlanStepTaskLink, RecoveryTaskLink
from custom_messages.contracts.task_v1 import Task, TaskSteering
from custom_messages.enums import MessageMetaType, TaskHistoryType, TaskSteeringType
from runtime_settings.instance_layout import InstanceLayout

TEAM = "team_prompt_files"
PERSON = "alice"
OTHER_PERSON = "bob"
CONVERSATION = "conv_alice"
ROOT = "task_turn_3"


def _file(layout: InstanceLayout, attachment_id: str, *, filename: str = "") -> UserTaskAttachment:
    path = layout.uploads_dir / "session_alice" / attachment_id
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"data")
    return UserTaskAttachment(
        attachment_id=attachment_id,
        filename=filename or f"{attachment_id}.png",
        content_type="image/png",
        kind="image",
        size_bytes=4,
        sha256="ab" * 32,
        storage_refs=[AttachmentStorageRef(storage_type="local", path=str(path))],
    )


def _meta(conversation_id: str | None, person_id: str | None = PERSON) -> MessageMeta:
    return MessageMeta(
        conversation_id=conversation_id,
        gateway_context=GatewayContext(canonical_user_id=person_id) if person_id else None,
    )


def _verified_person(meta: MessageMeta) -> str | None:
    """Stands in for the signed-context check (``access_runtime.verified_meta_person_id``)."""

    return getattr(meta.gateway_context, "canonical_user_id", None)


class _CountingModel:
    """Token counts for the context budget, without a model provider."""

    async def count_tokens(self, messages, tool_specs=None, system_prompt=None, system_prompt_content=None) -> int:
        return max(1, len(json.dumps(messages, default=str)) // 4)


class _Instance:
    """One agent's task manager and the team's conversation store, on one instance folder."""

    def __init__(self, tmp_path: Path) -> None:
        self.layout = InstanceLayout.at(tmp_path / "instance")
        self.layout.ensure_directories()
        self.store = ConversationStore(
            db_path=str(self.layout.root / "conversations.db"), artifacts_dir=self.layout.conversation_artifacts_dir
        )
        self.manager = TaskManager(db_path=str(self.layout.root / "tasks.db"))
        self.manager.set_owner_identity("lead", "TeamLead", TEAM, "TeamArchitect")

    def turn(self, task_id: str, files: list[UserTaskAttachment], *, text: str = "") -> None:
        """A person's turn with its files; earlier turns are answered (``ROOT`` is the one running)."""

        self.store.ensure_thread(
            conversation_id=CONVERSATION,
            team_id=TEAM,
            owner_canonical_user_id=PERSON,
            owner_agent_id="lead",
            owner_agent_name="TeamLead",
            requester_name=PERSON,
            requester_id=PERSON,
            root_task_id=task_id,
        )
        self.store.append_turn(
            conversation_id=CONVERSATION,
            task_id=task_id,
            followup_to_task_id=None,
            owner_agent_id="lead",
            requester_name=PERSON,
            requester_id=PERSON,
            correlation_id=f"corr_{task_id}",
            problem_text=f"Request {task_id} {text}".strip(),
            owner_canonical_user_id=PERSON,
            attachments=[file.model_dump(mode="json") for file in files],
        )
        if task_id != ROOT:
            self.store.update_turn_completion(
                task_id=task_id,
                task_status="completed",
                final_answer_text=f"Answer {task_id} {text}".strip(),
                turn_summary_text=f"Answered {task_id} {text[:400]}".strip(),
            )

    def task(
        self,
        task_id: str,
        *,
        files: list[UserTaskAttachment] | None = None,
        conversation_id: str | None = None,
        parent_task_id: str | None = None,
    ) -> Task:
        task = Task(
            task_id=task_id,
            task_type=MessageMetaType.REQUEST,
            problem_statement=f"Request {task_id}",
            parent_task_id=parent_task_id,
            attachments=files or [],
        )
        assert self.manager.add_task(
            task,
            MessageHeader(from_agent_id="gateway", response_routing_key="gateway", agent_signature=""),
            _meta(conversation_id),
            owner_person_id=PERSON,
        )
        return task

    def engine(
        self,
        *,
        is_team_lead: bool = True,
        thread_window: dict | None = None,
        verified_person_of=_verified_person,
        context_window_tokens: int = 200_000,
    ) -> DefaultContextEngine:
        return DefaultContextEngine(
            agent_id="lead" if is_team_lead else "worker",
            logger=logging.getLogger("test.prompt_files"),
            task_manager=self.manager,
            conversation_store=self.store,
            conversation_compactor=None,
            context_window_tokens=context_window_tokens,
            reserved_output_tokens=0,
            model=_CountingModel(),
            thread_window=thread_window,
            workspace_dir=str(self.layout.workspaces_dir),
            instance_root=str(self.layout.root),
            is_team_lead=is_team_lead,
            adapters=ContextEngineAdapters(
                build_request_time_context=lambda meta: "",
                build_request_conversation_context=lambda meta, text: "",
                build_team_routing_prompt_context=lambda: "",
                verified_person_of=verified_person_of,
            ),
        )

    def prompt(self, task_id: str, engine: DefaultContextEngine | None = None) -> str:
        return self.assembled(task_id, engine).prompt

    def assembled(self, task_id: str, engine: DefaultContextEngine | None = None) -> AssembledContext:
        info = self.manager.get_task_info(task_id)
        return (engine or self.engine()).assemble(
            ContextAssemblyRequest(
                incoming_content=info.task,
                incoming_header=info.header,
                incoming_meta=info.meta,
                identity_meta=info.meta,
                execution_task_id=task_id,
                incoming_message_json=json.dumps({"msg_content": {"task_id": task_id}}),
            )
        )

    def focused_prompt(self, task_id: str, *, plan_step: bool) -> str:
        """A plan step's or recovery task's prompt, with the placeholder ids the turn gives it."""

        info = self.manager.get_task_info(task_id)
        prompt_task = info.task.model_copy(update={"task_id": "current_plan_step_task", "parent_task_id": None})
        prompt_meta = info.meta.model_copy(
            update={"conversation_id": "current_plan_step_conversation", "gateway_context": None}
        )
        return self.engine().assemble(
            ContextAssemblyRequest(
                incoming_content=prompt_task,
                incoming_header=info.header,
                incoming_meta=prompt_meta,
                identity_meta=info.meta,
                execution_task_id=task_id,
                incoming_message_json=json.dumps({"msg_content": {"task_id": prompt_task.task_id}}),
                plan_step_task=plan_step,
                isolated_recovery_task=not plan_step,
            )
        ).prompt


def _section(prompt: str, heading: str) -> list[str]:
    """The file lines listed under ``heading`` (empty when the prompt has no such section)."""

    lines = prompt.splitlines()
    starts = [index for index, line in enumerate(lines) if line.startswith(heading)]
    if not starts:
        return []
    assert len(starts) == 1, f"{heading} appears {len(starts)} times"
    listed = []
    for line in lines[starts[0] + 1 :]:
        if not line.startswith("- "):
            break
        listed.append(line)
    return listed


def _listed_ids(lines: list[str]) -> list[str]:
    return [line.split("(attachment_id ", 1)[1].split(",", 1)[0] for line in lines]


@pytest.fixture
def instance(tmp_path: Path) -> _Instance:
    return _Instance(tmp_path)


def test_a_follow_up_names_the_newest_20_earlier_files_however_little_history_it_shows(instance: _Instance) -> None:
    layout = instance.layout
    first = [_file(layout, f"att_t1_{index:02d}") for index in range(15)]
    # Turn 2 sends one of turn 1's files again: it is listed once, with turn 2.
    second = [_file(layout, f"att_t2_{index:02d}") for index in range(10)] + [first[0]]
    instance.turn("task_turn_1", first)
    instance.turn("task_turn_2", second)
    # This request brings 20 files of its own; they do not take earlier files' places.
    current = [_file(layout, "att_current", filename="current.png")]
    current += [_file(layout, f"att_current_{index:02d}") for index in range(19)]
    instance.turn(ROOT, current)
    instance.task(ROOT, files=current, conversation_id=CONVERSATION)

    # The prompt's history shows only turn 2 (the newest two turns are loaded; one is this request's).
    prompt = instance.prompt(ROOT, instance.engine(thread_window={"max_turns_to_load": 2, "recent_full_turns": 1}))

    assert "Request task_turn_1" not in prompt and "Request task_turn_2" in prompt
    earlier = _section(prompt, EARLIER_FILES_HEADING)
    # The 20 newest files of earlier turns, each once; the request's own files
    # are listed with the request instead.
    assert len(earlier) == 20
    assert [line.endswith("sent in turn 2)") for line in earlier].count(True) == 11
    assert [line.endswith("sent in turn 1)") for line in earlier].count(True) == 9
    listed = _listed_ids(earlier)
    assert len(set(listed)) == 20 and "att_t1_00" in listed
    assert not any(attachment_id.startswith("att_current") for attachment_id in listed)
    # Turn 1's oldest files fall outside the bound.
    assert "att_t1_05" not in prompt and "att_t1_06" in listed and "att_t1_14" in listed
    assert "current.png (id=att_current" in prompt and "id=att_current_18" in prompt
    # Names, ids, kind, type, size and turn; never where a file is stored.
    assert earlier[0].startswith("- att_t1_00.png (attachment_id att_t1_00, image, image/png, 4 bytes, sent in turn 2)")
    assert not any(str(layout.uploads_dir) in line or "ab" * 32 in line for line in earlier)
    assert _section(prompt, REQUEST_FILES_HEADING) == []


def _long_conversation(instance: _Instance, *, with_files: bool) -> AssembledContext:
    """29 long answered turns (two files each when ``with_files``), then this request, in a 12,000-token window."""

    layout = instance.layout
    for turn in range(1, 30):
        files = (
            [_file(layout, f"att_h{turn:02d}_{index}", filename=f"{'scan' * 25}_{turn}_{index}.png") for index in range(2)]
            if with_files
            else []
        )
        instance.turn(f"task_turn_h{turn:02d}", files, text="word " * 600)
    instance.turn(ROOT, [])
    instance.task(ROOT, conversation_id=CONVERSATION)
    engine = instance.engine(
        thread_window={"max_turns_to_load": 30, "recent_full_turns": 3}, context_window_tokens=12_000
    )
    return instance.assembled(ROOT, engine)


def _tokens(assembled: AssembledContext) -> dict[str, int]:
    return {source["name"]: source["estimated_tokens"] for source in assembled.source_lengths}


def test_a_long_conversation_leaves_room_for_its_earlier_files_within_the_soft_budget(
    instance: _Instance, tmp_path: Path
) -> None:
    """The thread's budget counts the earlier-files section, so a full history does not push the prompt over."""

    plain = _long_conversation(_Instance(tmp_path / "plain"), with_files=False)
    with_files = _long_conversation(instance, with_files=True)

    # Without files the history fills the soft budget.
    assert plain.estimated_tokens > 0.9 * plain.soft_limit_tokens and not plain.warnings
    # With files the history gives up room for the files section, listed in full.
    files_tokens = _tokens(with_files)["conversation_files"]
    assert files_tokens > 500 and len(_section(with_files.prompt, EARLIER_FILES_HEADING)) == 20
    assert _tokens(with_files)["conversation_thread_context"] < _tokens(plain)["conversation_thread_context"]
    assert with_files.estimated_tokens < with_files.soft_limit_tokens
    assert not [warning for warning in with_files.warnings if warning["warning"] == "context_soft_limit_reached"]


@pytest.mark.asyncio
async def test_files_received_while_the_request_ran_are_named_with_it(instance: _Instance) -> None:
    layout = instance.layout
    instance.turn("task_turn_1", [_file(layout, "att_receipt")])
    request_file = _file(layout, "att_list")
    instance.turn(ROOT, [request_file])
    instance.task(ROOT, files=[request_file], conversation_id=CONVERSATION)
    guidance = TaskSteering(
        task_id=ROOT,
        steering_type=TaskSteeringType.PROACTIVE_GUIDANCE,
        guidance_text="Use this one too.",
        attachments=[_file(layout, "att_guidance", filename="label.png")],
    )
    assert await _record_guidance_history(SimpleNamespace(task_manager=instance.manager), ROOT, guidance)
    instance.manager.append_history_entry(
        ROOT,
        "TaskQuery response: here is the contract",
        entry_type=TaskHistoryType.QUERY_EVENT,
        details={QUERY_ANSWER_FILES_KEY: [_file(layout, "att_answer", filename="contract.png").model_dump(mode="json")]},
    )

    prompt = instance.prompt(ROOT)

    received = _section(prompt, REQUEST_FILES_HEADING)
    assert _listed_ids(received) == ["att_guidance", "att_answer"]
    assert received[0].startswith("- label.png (attachment_id att_guidance, image")
    # The message's own file is listed with the message, not again.
    assert "att_list" not in _listed_ids(received) and "id=att_list" in prompt
    assert _listed_ids(_section(prompt, EARLIER_FILES_HEADING)) == ["att_receipt"]


@pytest.mark.asyncio
@pytest.mark.parametrize("shape", ["delegated_subtask", "scheduled_job_run"])
async def test_a_specialist_is_named_its_own_files_and_never_the_conversations(instance: _Instance, shape: str) -> None:
    """A delegated task, or a worker's job run that continues its owner's conversation (top level, same person)."""

    layout = instance.layout
    instance.turn("task_turn_1", [_file(layout, "att_receipt")])
    chosen = _file(layout, "att_chosen")
    if shape == "delegated_subtask":
        instance.task("task_specialist", files=[chosen], conversation_id=CONVERSATION, parent_task_id=ROOT)
    else:
        instance.task("task_specialist", files=[chosen], conversation_id=CONVERSATION)
    forwarded = TaskSteering(
        task_id="task_specialist",
        steering_type=TaskSteeringType.PROACTIVE_GUIDANCE,
        guidance_text="And this one.",
        attachments=[_file(layout, "att_forwarded")],
    )
    assert await _record_guidance_history(SimpleNamespace(task_manager=instance.manager), "task_specialist", forwarded)

    prompt = instance.prompt("task_specialist", instance.engine(is_team_lead=False))

    assert _listed_ids(_section(prompt, REQUEST_FILES_HEADING)) == ["att_forwarded"]
    assert _section(prompt, EARLIER_FILES_HEADING) == []
    assert "att_receipt" not in prompt


@pytest.mark.parametrize("plan_step", [True, False], ids=["plan_step", "recovery_task"])
def test_a_plan_step_or_recovery_task_names_its_requests_files_and_the_earlier_ones(
    instance: _Instance, plan_step: bool
) -> None:
    layout = instance.layout
    instance.turn("task_turn_1", [_file(layout, "att_receipt")])
    request_file = _file(layout, "att_list")
    instance.turn(ROOT, [request_file])
    instance.task(ROOT, files=[request_file], conversation_id=CONVERSATION)
    # A step is a new top-level task with no files and a fresh conversation id.
    instance.task("task_step", conversation_id="plan_step_conv_1")
    instance.manager.plan_state.plan_step_task_links["task_step"] = PlanStepTaskLink(
        root_task_id=ROOT, plan_id="plan_1", step_id="s1", step_task_id="task_step"
    )
    task_id = "task_step"
    if not plan_step:
        instance.task("task_recovery", conversation_id="recov_conv_1")
        instance.manager.plan_state.recovery_links["task_recovery"] = RecoveryTaskLink(
            original_task_id="task_step",
            original_plan_id="plan_1",
            recovery_task_id="task_recovery",
            branch_id="b",
            failure_type="step_failed",
            failure_summary="The step failed.",
        )
        task_id = "task_recovery"

    prompt = instance.focused_prompt(task_id, plan_step=plan_step)

    assert _listed_ids(_section(prompt, REQUEST_FILES_HEADING)) == ["att_list"]
    assert _listed_ids(_section(prompt, EARLIER_FILES_HEADING)) == ["att_receipt"]


@pytest.mark.parametrize("person_id", [OTHER_PERSON, None], ids=["another_person", "nobody_verified"])
def test_earlier_files_are_named_only_for_the_person_who_owns_the_conversation(
    instance: _Instance, person_id: str | None
) -> None:
    layout = instance.layout
    instance.turn("task_turn_1", [_file(layout, "att_receipt")])
    instance.turn(ROOT, [])
    instance.task(ROOT, conversation_id=CONVERSATION)

    prompt = instance.prompt(ROOT, instance.engine(verified_person_of=lambda meta: person_id))

    assert _section(prompt, EARLIER_FILES_HEADING) == [] and "att_receipt" not in prompt
    # The owner's own prompt names it.
    assert _listed_ids(_section(instance.prompt(ROOT), EARLIER_FILES_HEADING)) == ["att_receipt"]
