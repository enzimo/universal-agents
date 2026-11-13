"""Which files a task may name and open by id (``custom_agents.conversation_attachments``).

These tests use a real task manager (its plan-step and recovery links, its
saved history and a restart from saved state) and a real SQLite conversation
store. They cover: files received while a request ran; plan steps and recovery
tasks resolving to the request they were made for, and subtasks never doing so;
the earlier turns of a conversation only for the team lead and the person who
owns it; saving files with the request's turn; delegation selection; whether a
file is still stored; and how files are named to a model.
"""

from __future__ import annotations

import errno
import logging
import os
from pathlib import Path
from types import SimpleNamespace

import pytest

from custom_agents.conversation_attachments import (
    MAX_SELECTED_ATTACHMENTS,
    UPLOAD_AVAILABLE,
    UPLOAD_DELETED,
    UPLOAD_REMOTE,
    UPLOAD_UNREADABLE,
    AttachmentSelectionError,
    attachment_scope,
    describe_attachment,
    locate_upload,
    received_attachments,
    request_files,
    save_turn_attachments,
    select_attachments,
    upload_state,
)
from custom_agents.conversation_store import ConversationStore
from custom_agents.steering_history_events import (
    GUIDANCE_ACCEPTED_EVENT,
    GUIDANCE_STEERING_KEY,
    HISTORY_EVENT_KEY,
    QUERY_ANSWER_FILES_KEY,
)
from custom_agents.task_manager import TaskManager
from custom_agents.task_steering_runtime import _record_guidance_history
from custom_messages.contracts.attachments_v1 import AttachmentStorageRef, UserTaskAttachment
from custom_messages.contracts.routing_v1 import MessageHeader, MessageMeta
from custom_messages.contracts.runtime_snapshot_v3 import PlanStepTaskLink, RecoveryTaskLink
from custom_messages.contracts.task_v1 import Task, TaskHistoryEntry, TaskSteering
from custom_messages.enums import MessageMetaType, TaskHistoryImportance, TaskHistoryType, TaskSteeringType
from custom_messages.records.task_results import TaskResultUpdate
from runtime_settings.instance_layout import InstanceLayout

TEAM = "team_files"
PERSON = "alice"
OTHER_PERSON = "bob"
CONVERSATION = "conv_alice"
ROOT = "task_turn_2"


def _file(layout: InstanceLayout, attachment_id: str, *, filename: str = "", stored: bool = True) -> UserTaskAttachment:
    path = layout.uploads_dir / "session_alice" / attachment_id
    if stored:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"data")
    return UserTaskAttachment(
        attachment_id=attachment_id,
        filename=filename or f"{attachment_id}.png",
        content_type="image/png",
        kind="image",
        size_bytes=4,
        sha256="0" * 64,
        storage_refs=[AttachmentStorageRef(storage_type="local", path=str(path))],
    )


class _Runtime:
    """A team lead's task manager and conversation store, opened on one instance folder."""

    def __init__(self, tmp_path: Path) -> None:
        self.layout = InstanceLayout.at(tmp_path / "instance")
        self.layout.ensure_directories()
        self.store = ConversationStore(
            db_path=str(self.layout.root / "conversations.db"), artifacts_dir=self.layout.conversation_artifacts_dir
        )
        self.manager = self.open_manager()

    def open_manager(self) -> TaskManager:
        manager = TaskManager(db_path=str(self.layout.root / "tasks.db"))
        manager.set_owner_identity("lead", "TeamLead", TEAM, "TeamArchitect")
        return manager

    def restart(self) -> None:
        self.manager = self.open_manager()
        assert self.manager.restore_runtime_state()

    def turn(self, task_id: str, files: list[UserTaskAttachment], *, conversation_id: str = CONVERSATION, owner: str = PERSON) -> None:
        self.store.ensure_thread(
            conversation_id=conversation_id,
            team_id=TEAM,
            owner_canonical_user_id=owner,
            owner_agent_id="lead",
            owner_agent_name="TeamLead",
            requester_name=owner,
            requester_id=owner,
            root_task_id=task_id,
        )
        self.store.append_turn(
            conversation_id=conversation_id,
            task_id=task_id,
            followup_to_task_id=None,
            owner_agent_id="lead",
            requester_name=owner,
            requester_id=owner,
            correlation_id=f"corr_{task_id}",
            problem_text=f"Request {task_id}",
            owner_canonical_user_id=owner,
            attachments=[file.model_dump(mode="json") for file in files],
        )

    def task(
        self,
        task_id: str,
        *,
        files: list[UserTaskAttachment] | None = None,
        parent_task_id: str | None = None,
        conversation_id: str | None = None,
    ) -> None:
        assert self.manager.add_task(
            Task(
                task_id=task_id,
                task_type=MessageMetaType.REQUEST,
                problem_statement=f"Work {task_id}",
                parent_task_id=parent_task_id,
                attachments=files or [],
            ),
            MessageHeader(from_agent_id="gateway", response_routing_key="gateway", agent_signature=""),
            MessageMeta(conversation_id=conversation_id),
            owner_person_id=PERSON,
        )

    def plan_step(self, step_task_id: str, root_task_id: str = ROOT) -> None:
        # A plan step is a new top-level task with a fresh conversation id and no files.
        self.task(step_task_id, conversation_id=f"plan_step_conv_{step_task_id}")
        self.manager.plan_state.plan_step_task_links[step_task_id] = PlanStepTaskLink(
            root_task_id=root_task_id, plan_id="plan_1", step_id="s1", step_task_id=step_task_id
        )

    def recovery(self, recovery_task_id: str, original_task_id: str) -> None:
        self.task(recovery_task_id, conversation_id=f"recov_conv_{recovery_task_id}")
        self.manager.plan_state.recovery_links[recovery_task_id] = RecoveryTaskLink(
            original_task_id=original_task_id,
            original_plan_id="plan_1",
            recovery_task_id=recovery_task_id,
            branch_id="b",
            failure_type="step_failed",
            failure_summary="The step failed.",
        )

    def scope(self, task_id: str, *, is_team_lead: bool = True, person_id: str | None = PERSON):
        request = request_files(self.manager, task_id)
        assert request is not None
        return attachment_scope(request, conversation_store=self.store, is_team_lead=is_team_lead, person_id=person_id)

    def save(self, task_id: str, files: list[UserTaskAttachment]):
        return save_turn_attachments(task_id, files, task_manager=self.manager, conversation_store=self.store)


@pytest.fixture
def runtime(tmp_path: Path) -> _Runtime:
    """Turn 1 sent ``att_receipt``; turn 2 (``ROOT``, the request running now) sent ``att_list``."""

    runtime = _Runtime(tmp_path)
    runtime.turn("task_turn_1", [_file(runtime.layout, "att_receipt", filename="receipt.jpg")])
    runtime.turn(ROOT, [_file(runtime.layout, "att_list")])
    runtime.task(ROOT, files=[_file(runtime.layout, "att_list")], conversation_id=CONVERSATION)
    return runtime


def _ids(files) -> list[str]:
    return [getattr(file, "attachment", file).attachment_id for file in files]


@pytest.mark.asyncio
async def test_files_received_while_a_request_ran_are_found_after_the_drain_and_a_restart(runtime: _Runtime) -> None:
    guidance = TaskSteering(
        task_id=ROOT,
        steering_type=TaskSteeringType.PROACTIVE_GUIDANCE,
        guidance_text="Use this one too.",
        attachments=[_file(runtime.layout, "att_guidance")],
    )
    agent = SimpleNamespace(task_manager=runtime.manager)
    # Accepted, then applied to a model turn (the applied entry carries no files).
    assert await _record_guidance_history(agent, ROOT, guidance)
    assert await _record_guidance_history(agent, ROOT, guidance, applied=True)
    runtime.manager.append_history_entry(
        ROOT,
        "TaskQuery response: here is the photo",
        entry_type=TaskHistoryType.QUERY_EVENT,
        details={"query_type": "response", QUERY_ANSWER_FILES_KEY: [_file(runtime.layout, "att_answer").model_dump(mode="json")]},
    )

    assert _ids(received_attachments(runtime.manager.get_task_info(ROOT).task)) == ["att_guidance", "att_answer"]
    runtime.restart()
    assert _ids(received_attachments(runtime.manager.get_task_info(ROOT).task)) == ["att_guidance", "att_answer"]
    # The request's own files come first, then the received ones.
    assert _ids(request_files(runtime.manager, ROOT).files) == ["att_list", "att_guidance", "att_answer"]


@pytest.mark.asyncio
async def test_files_in_a_subtasks_returned_history_never_count_as_sent_by_the_person(runtime: _Runtime) -> None:
    """A worker's result history is copied into the request's history; its file records are not the person's."""

    agent = SimpleNamespace(task_manager=runtime.manager)
    own_guidance = TaskSteering(
        task_id=ROOT,
        steering_type=TaskSteeringType.PROACTIVE_GUIDANCE,
        guidance_text="Use this one too.",
        attachments=[_file(runtime.layout, "att_guidance")],
    )
    assert await _record_guidance_history(agent, ROOT, own_guidance)
    assert runtime.manager.add_subtask(
        ROOT,
        Task(task_id="subtask_1", task_type=MessageMetaType.REQUEST, problem_statement="Research", parent_task_id=ROOT),
        MessageHeader(from_agent_id="lead", response_routing_key="lead", agent_signature=""),
        MessageMeta(conversation_id="conv_subtask_1", to_agent_name="Researcher"),
    )
    # Another person's upload, named by entries shaped like the ones this
    # runtime records itself.
    other = _file(runtime.layout, "att_other_person")
    other.storage_refs[0].path = str(runtime.layout.uploads_dir / "session_bob" / "att_other_person")
    forwarded = TaskSteering(
        task_id="subtask_1", steering_type=TaskSteeringType.PROACTIVE_GUIDANCE, guidance_text="x", attachments=[other]
    )
    returned_history = [
        TaskHistoryEntry(
            summary="Mid-turn proactive steering accepted: x",
            entry_type=TaskHistoryType.TASK_EVENT,
            importance=TaskHistoryImportance.HIGH,
            details={HISTORY_EVENT_KEY: GUIDANCE_ACCEPTED_EVENT, GUIDANCE_STEERING_KEY: forwarded.model_dump(mode="json")},
        ),
        TaskHistoryEntry(
            summary="TaskQuery response: here",
            entry_type=TaskHistoryType.QUERY_EVENT,
            importance=TaskHistoryImportance.HIGH,
            details={QUERY_ANSWER_FILES_KEY: [other.model_dump(mode="json")]},
        ),
    ]

    # As the lead does with a worker's response, then its result.
    assert runtime.manager.update_task_result(
        "subtask_1", TaskResultUpdate(answer="done", execution_history=returned_history)
    )
    runtime.manager.bubble_subtask_result(ROOT, "subtask_1")

    root_task = runtime.manager.get_task_info(ROOT).task
    # Both entries were copied with their details as the worker wrote them.
    copied = [entry for entry in root_task.execution_history if entry.details.get("bubbled_entry_id")]
    assert [entry.entry_type for entry in copied] == [TaskHistoryType.SUBTASK_EVENT] * 2
    assert GUIDANCE_STEERING_KEY in copied[0].details and QUERY_ANSWER_FILES_KEY in copied[1].details
    assert _ids(received_attachments(root_task)) == ["att_guidance"]
    assert _ids(request_files(runtime.manager, ROOT).files) == ["att_list", "att_guidance"]
    scope = runtime.scope(ROOT)
    assert scope.find("att_other_person") is None and scope.find("att_guidance") is not None
    with pytest.raises(AttachmentSelectionError):
        select_attachments(scope, ["att_other_person"], uploads_dir=runtime.layout.uploads_dir)


def test_an_unreadable_received_record_is_reported_and_skipped(runtime: _Runtime, caplog: pytest.LogCaptureFixture) -> None:
    runtime.manager.append_history_entry(
        ROOT, "TaskQuery response", entry_type=TaskHistoryType.QUERY_EVENT,
        details={QUERY_ANSWER_FILES_KEY: [{"filename": "no id"}, _file(runtime.layout, "att_ok").model_dump(mode="json")]},
    )

    with caplog.at_level(logging.WARNING):
        assert _ids(received_attachments(runtime.manager.get_task_info(ROOT).task)) == ["att_ok"]
    assert f"event=attachment_record_unreadable task_id={ROOT} source=answer" in caplog.text


def test_plan_steps_and_recovery_tasks_use_their_requests_files_and_conversation(runtime: _Runtime) -> None:
    runtime.plan_step("step_2")
    runtime.recovery("recovery_1", "step_2")

    for task_id in ("step_2", "recovery_1"):
        request = request_files(runtime.manager, task_id)
        assert (request.request_task_id, request.conversation_id) == (ROOT, CONVERSATION)
        assert _ids(request.files) == ["att_list"]
        scope = runtime.scope(task_id)
        # The current request's file and the turn-1 receipt, by id.
        assert scope.find("att_list").attachment.attachment_id == "att_list"
        earlier = scope.find("att_receipt")
        assert (earlier.attachment.filename, earlier.turn_index) == ("receipt.jpg", 1)
        assert _ids(scope.files().earlier) == ["att_receipt"]


def test_a_delegated_subtask_never_reaches_its_requesters_files(runtime: _Runtime) -> None:
    runtime.task("subtask_1", parent_task_id=ROOT, files=[_file(runtime.layout, "att_chosen")])

    request = request_files(runtime.manager, "subtask_1")
    scope = runtime.scope("subtask_1")

    assert (request.request_task_id, request.conversation_id) == ("subtask_1", None)
    assert _ids(scope.available()) == ["att_chosen"]
    assert scope.find("att_list") is None and scope.find("att_receipt") is None


def test_a_link_to_a_request_no_longer_held_falls_back_to_the_tasks_own_files(runtime: _Runtime, caplog) -> None:
    runtime.plan_step("step_orphan", root_task_id="task_gone")

    with caplog.at_level(logging.WARNING):
        request = request_files(runtime.manager, "step_orphan")

    # No conversation is offered for a request that cannot be checked.
    assert (request.request_task_id, request.conversation_id, request.files) == ("step_orphan", None, ())
    assert "event=attachment_request_missing task_id=step_orphan missing_task_ids=task_gone" in caplog.text
    assert request_files(runtime.manager, "task_unknown") is None


def test_a_recovery_task_for_an_ended_step_reaches_its_request_without_a_warning(runtime: _Runtime, caplog) -> None:
    runtime.plan_step("step_2")
    runtime.recovery("recovery_1", "step_2")
    # The step ends: archived and removed from the held tasks; its link stays.
    assert runtime.manager.remove_task("step_2")

    with caplog.at_level(logging.DEBUG, logger="custom_agents.conversation_attachments"):
        for _ in range(3):  # every prompt and tool call reads it
            request = request_files(runtime.manager, "recovery_1")

    assert (request.request_task_id, request.conversation_id) == (ROOT, CONVERSATION)
    assert _ids(request.files) == ["att_list"]
    ours = [record for record in caplog.records if record.name == "custom_agents.conversation_attachments"]
    assert [record.levelno for record in ours] == [logging.DEBUG] * 3
    assert "event=attachment_request_link_ended task_id=recovery_1 ended_task_ids=step_2" in caplog.text
    assert "attachment_request_missing" not in caplog.text


def test_earlier_conversation_files_are_only_for_the_team_lead_working_for_their_owner(
    runtime: _Runtime, caplog: pytest.LogCaptureFixture
) -> None:
    runtime.turn("task_bob", [_file(runtime.layout, "att_bob")], conversation_id="conv_bob", owner=OTHER_PERSON)

    assert runtime.scope(ROOT).find("att_receipt") is not None
    # A specialist (here, a worker's scheduled job run continuing its owner's
    # conversation) gets only its own task's files.
    assert runtime.scope(ROOT, is_team_lead=False).find("att_receipt") is None
    assert runtime.scope(ROOT, is_team_lead=False).files().earlier == ()
    # Another conversation's file is not found, and nothing says it exists.
    assert runtime.scope(ROOT).find("att_bob") is None
    with caplog.at_level(logging.INFO):
        assert runtime.scope(ROOT, person_id=OTHER_PERSON).find("att_receipt") is None
        assert runtime.scope(ROOT, person_id=None).find("att_receipt") is None
    refusals = [record for record in caplog.records if "event=attachment_scope_conversation_refused" in record.message]
    # Another person is unexpected (a warning); work for nobody is not.
    assert [(record.levelname, record.message.rsplit("reason=", 1)[1]) for record in refusals] == [
        ("WARNING", "resource_owner_required"),
        ("INFO", "no_verified_person"),
    ]
    # The request's own files stay usable either way.
    assert runtime.scope(ROOT, person_id=None).find("att_list") is not None


def test_ids_that_this_runtime_never_gives_out_are_not_looked_up(runtime: _Runtime) -> None:
    scope = runtime.scope(ROOT)

    for wanted in ("", "%", "att_%", "../att_receipt", "att receipt", "x" * 129):
        assert scope.find(wanted) is None


def test_files_are_saved_with_the_turn_of_the_request_a_step_or_recovery_task_serves(runtime: _Runtime) -> None:
    runtime.plan_step("step_2")
    runtime.recovery("recovery_1", "step_2")

    saved = runtime.save("step_2", [_file(runtime.layout, "att_step_answer")])
    resent = runtime.save("step_2", [_file(runtime.layout, "att_step_answer")])
    runtime.save("recovery_1", [_file(runtime.layout, "att_recovery_answer")])

    assert _ids(UserTaskAttachment.model_validate(record) for record in saved) == ["att_list", "att_step_answer"]
    assert resent == saved
    turn = runtime.store.get_turn_by_task_id(ROOT)
    assert [item["attachment_id"] for item in turn["attachments"]] == ["att_list", "att_step_answer", "att_recovery_answer"]
    # The next step and the request itself find the step's answer file and
    # list it with the current request, not with earlier turns.
    runtime.plan_step("step_3")
    for task_id in ("step_3", ROOT):
        scope = runtime.scope(task_id)
        assert scope.find("att_step_answer").turn_index == 2
        files = scope.files()
        assert _ids(files.current) == ["att_list", "att_recovery_answer", "att_step_answer"]
        assert _ids(files.earlier) == ["att_receipt"]
        assert _ids(scope.available()) == ["att_list", "att_recovery_answer", "att_step_answer", "att_receipt"]
    # The next turn lists them as earlier files.
    runtime.turn("task_turn_3", [])
    runtime.task("task_turn_3", conversation_id=CONVERSATION)
    assert "att_recovery_answer" in _ids(runtime.scope("task_turn_3").files().earlier)


def test_a_prompts_files_come_from_one_listing_of_the_conversation(
    runtime: _Runtime, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Both groups a prompt shows (current and earlier files) are read with one bounded query."""

    listings: list[tuple[str, int]] = []
    listing = runtime.store.list_conversation_attachments

    def counted(conversation_id: str, *, limit: int = 20, **kwargs):
        listings.append((conversation_id, limit))
        return listing(conversation_id, limit=limit, **kwargs)

    monkeypatch.setattr(runtime.store, "list_conversation_attachments", counted)
    scope = runtime.scope(ROOT)

    files = scope.files()
    assert (_ids(files.current), _ids(files.earlier)) == (["att_list"], ["att_receipt"])
    assert listings == [(CONVERSATION, 20)]
    assert _ids(scope.available(limit=1)) == ["att_list"] and len(listings) == 2
    # A specialist's scope has no conversation to list.
    assert _ids(runtime.scope(ROOT, is_team_lead=False).files().current) == ["att_list"] and len(listings) == 2


def test_a_subtasks_files_stay_in_its_history_and_a_missing_turn_is_reported(runtime: _Runtime, caplog) -> None:
    runtime.task("subtask_1", parent_task_id=ROOT)
    runtime.task("task_no_turn", conversation_id=CONVERSATION)

    assert runtime.save("subtask_1", [_file(runtime.layout, "att_sub")]) is None
    assert runtime.save(ROOT, []) is None
    with caplog.at_level(logging.WARNING):
        assert runtime.save("task_no_turn", [_file(runtime.layout, "att_lost")]) is None

    assert "event=conversation_turn_missing_for_attachments task_id=task_no_turn" in caplog.text
    assert "attachment_ids=att_lost" in caplog.text
    assert runtime.scope(ROOT).find("att_sub") is None


def test_a_store_error_while_saving_reaches_the_caller(runtime: _Runtime, monkeypatch: pytest.MonkeyPatch) -> None:
    def fail(**_kwargs):
        raise RuntimeError("database is locked")

    monkeypatch.setattr(runtime.store, "merge_turn_attachments", fail)

    with pytest.raises(RuntimeError, match="locked"):
        runtime.save(ROOT, [_file(runtime.layout, "att_retry")])


def test_a_delegation_selects_exact_files_including_earlier_ones(runtime: _Runtime) -> None:
    scope = runtime.scope(ROOT)
    uploads = runtime.layout.uploads_dir

    assert _ids(select_attachments(scope, ["att_receipt", "att_list", "att_receipt"], uploads_dir=uploads)) == [
        "att_receipt",
        "att_list",
    ]
    assert select_attachments(scope, [], uploads_dir=uploads) == []


def test_unknown_too_many_or_deleted_files_are_refused_with_the_files_that_can_be_passed(runtime: _Runtime) -> None:
    runtime.turn("task_turn_0", [_file(runtime.layout, "att_gone", stored=False)])
    scope = runtime.scope(ROOT)
    uploads = runtime.layout.uploads_dir

    with pytest.raises(AttachmentSelectionError) as unknown:
        select_attachments(scope, [f"att_unknown{index}" for index in range(12)] + ["att_list"], uploads_dir=uploads)
    message = str(unknown.value)
    assert "att_unknown0" in message and "att_unknown9" in message and "and 2 more" in message
    assert "att_unknown10" not in message
    assert "receipt.jpg (attachment_id att_receipt" in message and "att_list" in message
    assert str(uploads) not in message
    assert len(unknown.value.unknown_ids) == 12

    with pytest.raises(AttachmentSelectionError, match="no longer stored") as deleted:
        select_attachments(scope, ["att_gone"], uploads_dir=uploads)
    assert deleted.value.deleted_ids == ["att_gone"]

    with pytest.raises(AttachmentSelectionError, match=f"at most {MAX_SELECTED_ATTACHMENTS}"):
        select_attachments(scope, [f"att_{index}" for index in range(MAX_SELECTED_ATTACHMENTS + 1)], uploads_dir=uploads)


def test_a_file_counts_as_stored_only_inside_this_instances_uploads(runtime: _Runtime, tmp_path: Path) -> None:
    uploads = runtime.layout.uploads_dir
    outside = tmp_path / "elsewhere" / "secret.txt"
    outside.parent.mkdir()
    outside.write_text("not an upload")
    link = uploads / "session_alice" / "att_link"
    link.parent.mkdir(parents=True, exist_ok=True)
    os.symlink(outside, link)

    def at(path: Path | None) -> UserTaskAttachment:
        refs = [AttachmentStorageRef(storage_type="local", path=str(path))] if path else []
        return UserTaskAttachment(attachment_id="att_x", filename="x", size_bytes=1, sha256="0", storage_refs=refs)

    assert upload_state(_file(runtime.layout, "att_here"), uploads) == UPLOAD_AVAILABLE
    assert upload_state(_file(runtime.layout, "att_removed", stored=False), uploads) == UPLOAD_DELETED
    # Not a copy this runtime may open: elsewhere on disk, a link out of the
    # uploads folder, the folder itself, or stored on another machine.
    assert upload_state(at(outside), uploads) == UPLOAD_REMOTE
    assert upload_state(at(link), uploads) == UPLOAD_REMOTE
    assert upload_state(at(uploads), uploads) == UPLOAD_REMOTE
    assert upload_state(at(None), uploads) == UPLOAD_REMOTE


def test_a_file_is_named_for_a_model_without_where_it_is_stored(runtime: _Runtime) -> None:
    earlier = runtime.scope(ROOT).find("att_receipt")
    odd = _file(runtime.layout, "att_odd", filename="line one\nline two " + "x" * 200)

    assert describe_attachment(earlier) == (
        "receipt.jpg (attachment_id att_receipt, image, image/png, 4 bytes, sent in turn 1)"
    )
    shown = describe_attachment(odd)
    assert "\n" not in shown and shown.startswith("line one line two x") and "…" in shown
    assert str(runtime.layout.root) not in shown


def _stat_fails(monkeypatch: pytest.MonkeyPatch, path: Path, error: OSError) -> None:
    """Looking the stored file up raises ``error``, as a permission or disk fault would; other paths are untouched."""

    real_stat = os.stat
    target = os.path.realpath(path)

    def failing_stat(value, *args, **kwargs):
        if os.fspath(value) == target:
            raise error
        return real_stat(value, *args, **kwargs)

    monkeypatch.setattr(os, "stat", failing_stat)


def _path(attachment: UserTaskAttachment) -> Path:
    return Path(attachment.local_path())


@pytest.mark.parametrize(
    "fault, error_type, error_number",
    [
        (PermissionError(errno.EACCES, os.strerror(errno.EACCES)), "PermissionError", errno.EACCES),
        (OSError(errno.EIO, os.strerror(errno.EIO)), "OSError", errno.EIO),
    ],
)
def test_a_stored_file_that_cannot_be_read_is_not_called_removed(
    runtime: _Runtime, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture, fault, error_type, error_number
) -> None:
    attachment = _file(runtime.layout, "att_fault")
    _stat_fails(monkeypatch, _path(attachment), fault)

    with caplog.at_level(logging.INFO, logger="custom_agents.conversation_attachments"):
        location = locate_upload(attachment, runtime.layout.uploads_dir, task_id=ROOT)

    assert (location.state, location.path, location.size_bytes) == (UPLOAD_UNREADABLE, None, None)
    [logged] = [record for record in caplog.records if "event=attachment_upload_unreadable" in record.getMessage()]
    assert logged.levelno == logging.WARNING
    assert f"task_id={ROOT} attachment_id=att_fault error_type={error_type} errno={error_number}" in logged.getMessage()
    assert str(runtime.layout.root) not in caplog.text and "session_alice" not in caplog.text


def test_a_removed_upload_or_folder_is_gone_and_a_layout_fault_is_unreadable(
    runtime: _Runtime, caplog: pytest.LogCaptureFixture
) -> None:
    uploads = runtime.layout.uploads_dir

    def at(path: Path, attachment_id: str) -> UserTaskAttachment:
        return UserTaskAttachment(
            attachment_id=attachment_id, filename="x.png", size_bytes=4, sha256="0" * 64,
            storage_refs=[AttachmentStorageRef(storage_type="local", path=str(path))],
        )

    stored = _file(runtime.layout, "att_here")
    removed = _file(runtime.layout, "att_removed", stored=False)
    # A clean-up removed the upload's whole folder.
    in_removed_folder = at(uploads / "session_removed" / "att_in_removed", "att_in_removed")
    # A file sits where the upload's folder should be: looking inside it raises
    # NotADirectoryError, which no removal produces.
    (uploads / "session_file").write_bytes(b"not a folder")
    under_a_file = at(uploads / "session_file" / "att_under_file", "att_under_file")
    a_folder = _file(runtime.layout, "att_folder", stored=False)
    _path(a_folder).mkdir(parents=True)

    with caplog.at_level(logging.INFO, logger="custom_agents.conversation_attachments"):
        found = locate_upload(stored, uploads, task_id=ROOT)
        states = [
            locate_upload(item, uploads, task_id=ROOT).state
            for item in (removed, in_removed_folder, under_a_file, a_folder)
        ]

    assert (found.state, found.path, found.size_bytes) == (UPLOAD_AVAILABLE, _path(stored).resolve(), 4)
    assert states == [UPLOAD_DELETED, UPLOAD_DELETED, UPLOAD_UNREADABLE, UPLOAD_UNREADABLE]
    logged = [record.getMessage() for record in caplog.records if record.levelno >= logging.INFO]
    assert len(logged) == 2
    assert all(record.levelno == logging.WARNING for record in caplog.records if record.levelno >= logging.INFO)
    assert (
        f"event=attachment_upload_unreadable task_id={ROOT} attachment_id=att_under_file "
        f"error_type=NotADirectoryError errno={errno.ENOTDIR}"
    ) in logged[0]
    assert "attachment_id=att_folder error_type=not_a_regular_file" in logged[1]
    assert str(runtime.layout.root) not in caplog.text and "session_file" not in caplog.text


def test_a_delegation_passes_on_a_file_it_cannot_read_now(
    runtime: _Runtime, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    _stat_fails(monkeypatch, runtime.layout.uploads_dir / "session_alice" / "att_list", PermissionError(errno.EACCES, "denied"))

    with caplog.at_level(logging.WARNING, logger="custom_agents.conversation_attachments"):
        chosen = select_attachments(runtime.scope(ROOT), ["att_list"], uploads_dir=runtime.layout.uploads_dir)

    # The fault may clear; the specialist's tool says so if it does not.
    assert _ids(chosen) == ["att_list"]
    assert f"event=attachment_upload_unreadable task_id={ROOT} attachment_id=att_list" in caplog.text
