"""Which uploaded files a task may name, and where their bytes are read from.

Covers ``custom_agents.conversation_attachments`` with a real SQLite
conversation store: a task's own files (its request, plus files recorded with
accepted guidance and answers), the files of earlier turns of its own
conversation for a top-level task that works for the conversation's person,
nothing from the conversation for a delegated task, bounded lists without
storage paths, and reading bytes only from this host's upload folders.
"""

from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import Any

import pytest

from assistant_gateway.config import GatewayInstanceConfig
from custom_agents.conversation_attachments import (
    INVENTORY_LIMIT,
    TASK_FILES_KEY,
    UPLOAD_FILE_MISSING,
    UPLOAD_NO_LOCAL_COPY,
    UPLOAD_NOT_ON_THIS_HOST,
    UPLOAD_OUTSIDE_FOLDERS,
    AttachmentSelectionError,
    LocatedUpload,
    TaskAttachments,
    UnavailableUpload,
    UploadFolders,
    locate_upload,
    task_files_details,
)
from custom_agents.conversation_store import ConversationStore
from custom_agents.task_records import TaskInfo
from custom_messages.contracts.attachments_v1 import AttachmentStorageRef, UserTaskAttachment
from custom_messages.contracts.routing_v1 import MessageHeader, MessageMeta
from custom_messages.contracts.task_v1 import Task, TaskHistoryEntry
from custom_messages.enums import MessageMetaType
from runtime_settings.instance_layout import InstanceLayout
from runtime_settings.interactive_apps import AdapterHostSettings
from storage.turn_attachments import TURN_ATTACHMENT_SUMMARY_FIELDS

ANN, BOB = "usr_ann", "usr_bob"
CONVERSATION, OTHER_CONVERSATION = "conv_files", "conv_other"
CURRENT = "task_current"
MODULE_LOGGER = "custom_agents.conversation_attachments"


@pytest.fixture
def layout(tmp_path: Path) -> InstanceLayout:
    layout = InstanceLayout.at(tmp_path / "instance")
    layout.ensure_directories()
    layout.uploads_dir.mkdir(parents=True)
    return layout


@pytest.fixture
def store(tmp_path: Path) -> ConversationStore:
    return ConversationStore(str(tmp_path / "conversations.db"), artifacts_dir=tmp_path / "conversation_artifacts")


def _upload(folder: Path, attachment_id: str, *, name: str = "", data: bytes = b"file bytes") -> UserTaskAttachment:
    """A file the gateway stored in ``folder``, with its record."""

    path = folder / "web_session" / f"{attachment_id}.bin"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    return _record(attachment_id, path, name=name)


def _record(attachment_id: str, path: Path | None, *, name: str = "") -> UserTaskAttachment:
    refs = [AttachmentStorageRef(storage_type="local", path=str(path))] if path is not None else []
    return UserTaskAttachment(
        attachment_id=attachment_id,
        filename=name or f"{attachment_id}.png",
        content_type="image/png",
        kind="image",
        size_bytes=10,
        sha256="0" * 64,
        storage_refs=refs,
    )


def _thread(store: ConversationStore, *, conversation_id: str = CONVERSATION, owner: str | None = ANN) -> None:
    store.ensure_thread(
        conversation_id=conversation_id,
        team_id="team",
        owner_agent_id="lead",
        owner_agent_name="Lead",
        requester_name=None,
        requester_id=None,
        root_task_id=f"root_{conversation_id}",
        owner_canonical_user_id=owner,
    )


def _turn(
    store: ConversationStore,
    task_id: str,
    attachments: list[UserTaskAttachment],
    *,
    conversation_id: str = CONVERSATION,
    owner: str | None = ANN,
) -> None:
    store.append_turn(
        conversation_id=conversation_id,
        task_id=task_id,
        followup_to_task_id=None,
        owner_agent_id="lead",
        requester_name=None,
        requester_id=None,
        correlation_id=None,
        problem_text=f"request {task_id}",
        owner_canonical_user_id=owner,
        attachments=attachments,
    )


def _task_info(
    *,
    attachments: list[UserTaskAttachment] = (),  # type: ignore[assignment]
    history: list[dict[str, Any]] = (),  # type: ignore[assignment]
    conversation_id: str | None = CONVERSATION,
    person: str | None = ANN,
    parent_task_id: str | None = None,
) -> TaskInfo:
    task = Task(
        task_id=CURRENT,
        task_type=MessageMetaType.REQUEST,
        problem_statement="current request",
        parent_task_id=parent_task_id,
        attachments=list(attachments),
    )
    info = TaskInfo(task, MessageHeader(from_agent_id="gateway"), MessageMeta(conversation_id=conversation_id), owner_person_id=person)
    for details in history:
        info.task.execution_history.append(TaskHistoryEntry(summary="accepted", details=details))
    return info


def _ids(records: list[Any]) -> list[str]:
    return [getattr(record, "attachment", record).attachment_id for record in records]


def test_a_top_level_task_uses_its_own_files_and_its_conversations(layout, store) -> None:
    _thread(store)
    photo, notes = _upload(layout.uploads_dir, "att_photo"), _upload(layout.uploads_dir, "att_notes")
    _turn(store, "task_first", [photo])
    _turn(store, "task_second", [notes])
    request_file = _upload(layout.uploads_dir, "att_request")
    guidance_file, answer_file = _upload(layout.uploads_dir, "att_guidance"), _upload(layout.uploads_dir, "att_answer")
    info = _task_info(
        attachments=[request_file],
        history=[{"event": "guidance"}, task_files_details([guidance_file]), task_files_details([answer_file, request_file])],
    )

    scope = TaskAttachments.load(info, store)

    # Own files in arrival order, one per id; the conversation is in scope.
    assert _ids(list(scope.own)) == ["att_request", "att_guidance", "att_answer"]
    assert scope.conversation_id == CONVERSATION
    found, unknown = scope.find([" att_photo ", "att_guidance", "att_missing", "att_photo", ""])
    assert (_ids(found), unknown) == (["att_photo", "att_guidance"], ["att_missing"])
    assert found[0] == photo  # the full record, for trusted code to open
    files, more = scope.conversation_files()
    assert [(entry.attachment.attachment_id, entry.turn_index, entry.task_id) for entry in files] == [
        ("att_notes", 2, "task_second"),
        ("att_photo", 1, "task_first"),
    ]
    assert more is False
    # Choices: own files newest first, then the conversation's, described without storage.
    choices = scope.choices()
    assert [choice["attachment_id"] for choice in choices] == [
        "att_answer", "att_guidance", "att_request", "att_notes", "att_photo",
    ]
    assert choices[0]["turn_index"] is None and choices[0]["task_id"] == CURRENT
    assert set(choices[-1]) == {*TURN_ATTACHMENT_SUMMARY_FIELDS, "turn_index", "task_id"}
    assert str(layout.root) not in repr(choices)


def test_a_delegated_task_uses_only_the_files_it_was_given(layout, store) -> None:
    _thread(store)
    _turn(store, "task_first", [_upload(layout.uploads_dir, "att_photo")])
    given = _upload(layout.uploads_dir, "att_given")
    info = _task_info(attachments=[given], parent_task_id="task_parent")

    scope = TaskAttachments.load(info, store)

    assert scope.conversation_id is None
    assert scope.find(["att_photo", "att_given"]) == ([given], ["att_photo"])
    assert scope.conversation_files() == ([], False)
    assert [choice["attachment_id"] for choice in scope.choices()] == ["att_given"]


@pytest.mark.parametrize(
    ("person", "owner", "allowed"),
    [
        (ANN, ANN, True),
        (None, None, True),  # work for no person, in an ownerless conversation
        (ANN, BOB, False),  # signed for Ann, in Bob's conversation
        (None, ANN, False),  # unsigned work in Ann's conversation
        (ANN, None, False),  # Ann's work in a conversation that belongs to nobody
    ],
)
def test_only_work_for_the_conversations_person_sees_its_files(
    layout, store, caplog, person: str | None, owner: str | None, allowed: bool
) -> None:
    _thread(store, owner=owner)
    _turn(store, "task_first", [_upload(layout.uploads_dir, "att_photo")], owner=owner)

    with caplog.at_level(logging.WARNING, logger=MODULE_LOGGER):
        scope = TaskAttachments.load(_task_info(person=person), store)

    assert (scope.conversation_id == CONVERSATION) is allowed
    assert (scope.find(["att_photo"])[1] == []) is allowed
    mismatch = [record for record in caplog.records if "event=conversation_files_owner_mismatch" in record.getMessage()]
    assert bool(mismatch) is not allowed
    if mismatch:
        assert f"task_id={CURRENT}" in mismatch[0].getMessage()


def test_a_task_never_reaches_another_conversations_files(layout, store) -> None:
    _thread(store)
    _thread(store, conversation_id=OTHER_CONVERSATION)
    _turn(store, "task_first", [_upload(layout.uploads_dir, "att_mine")])
    _turn(store, "task_elsewhere", [_upload(layout.uploads_dir, "att_elsewhere")], conversation_id=OTHER_CONVERSATION)

    scope = TaskAttachments.load(_task_info(), store)

    assert scope.find(["att_mine", "att_elsewhere"])[1] == ["att_elsewhere"]
    assert _ids(scope.conversation_files()[0]) == ["att_mine"]
    with pytest.raises(AttachmentSelectionError) as refused:
        scope.require(["att_elsewhere"])
    assert "att_elsewhere" in str(refused.value) and "att_mine" in str(refused.value)


def test_no_store_no_conversation_or_no_thread_leaves_only_the_tasks_own_files(layout, store, caplog) -> None:
    own = _upload(layout.uploads_dir, "att_own")

    assert TaskAttachments.load(_task_info(attachments=[own]), None).conversation_id is None
    assert TaskAttachments.load(_task_info(attachments=[own], conversation_id=None), store).conversation_id is None
    with caplog.at_level(logging.WARNING, logger=MODULE_LOGGER):
        scope = TaskAttachments.load(_task_info(attachments=[own]), store)
    assert scope.conversation_id is None and _ids(list(scope.own)) == ["att_own"]
    assert "event=conversation_files_thread_missing" in caplog.text
    assert TaskAttachments.load(None, store) == TaskAttachments(task_id="")


class _CountingStore:
    """The conversation store, recording the limits the scope asks for."""

    def __init__(self, store: ConversationStore) -> None:
        self._store = store
        self.limits: list[int | None] = []

    def get_thread(self, conversation_id: str) -> Any:
        return self._store.get_thread(conversation_id)

    def list_turn_attachments(self, conversation_id: str, **kwargs: Any) -> Any:
        self.limits.append(kwargs.get("limit"))
        return self._store.list_turn_attachments(conversation_id, **kwargs)


@pytest.mark.parametrize(("turns", "more"), [(INVENTORY_LIMIT + 5, True), (INVENTORY_LIMIT + 1, False)])
def test_the_conversation_list_is_bounded_and_skips_files_listed_elsewhere(layout, store, turns: int, more: bool) -> None:
    _thread(store)
    for index in range(turns):
        _turn(store, f"task_{index:02d}", [_upload(layout.uploads_dir, f"att_{index:02d}")])
    newest = f"att_{turns - 1:02d}"
    counting = _CountingStore(store)
    scope = TaskAttachments.load(_task_info(), counting)

    files, has_more = scope.conversation_files(exclude_ids=[newest])

    expected = [f"att_{index:02d}" for index in range(turns - 2, -1, -1)][:INVENTORY_LIMIT]
    assert (_ids(files), has_more) == (expected, more)
    # Asked for the limit, plus the one excluded file, plus one to tell whether there are more.
    assert counting.limits == [INVENTORY_LIMIT + 2]


def test_an_unknown_id_lists_at_most_twenty_choices_without_storage_paths(layout, store) -> None:
    _thread(store)
    for index in range(INVENTORY_LIMIT + 5):
        _turn(store, f"task_{index:02d}", [_upload(layout.uploads_dir, f"att_{index:02d}", name=f"photo {index}.png")])
    scope = TaskAttachments.load(_task_info(attachments=[_upload(layout.uploads_dir, "att_request")]), store)

    with pytest.raises(AttachmentSelectionError) as refused:
        scope.require(["att_request", "att_nope"])

    error = refused.value
    assert error.unknown_ids == ("att_nope",)
    assert len(error.choices) == INVENTORY_LIMIT
    assert error.choices[0]["attachment_id"] == "att_request"
    assert error.choices[1]["attachment_id"] == f"att_{INVENTORY_LIMIT + 4:02d}"
    message = str(error)
    assert "att_nope" in message and "photo 24.png" in message and "sent with this task" in message
    assert str(layout.root) not in message and "storage_refs" not in message
    assert str(AttachmentSelectionError(["att_nope"], [])).endswith("This task has no files it may use.")


def test_unreadable_task_file_entries_are_logged_and_skipped(layout, store, caplog) -> None:
    good = _upload(layout.uploads_dir, "att_good")
    history = [
        {TASK_FILES_KEY: "not a list"},
        {TASK_FILES_KEY: [{"attachment_id": "att_broken"}, good.model_dump(mode="json")]},
    ]

    with caplog.at_level(logging.WARNING, logger=MODULE_LOGGER):
        scope = TaskAttachments.load(_task_info(history=history, conversation_id=None), store)

    assert _ids(list(scope.own)) == ["att_good"]
    unreadable = [record.getMessage() for record in caplog.records if "event=task_files_entry_unreadable" in record.getMessage()]
    assert len(unreadable) == 2
    assert any("attachment_id=att_broken" in message for message in unreadable)


# --- Reading bytes only from this host's upload folders ----------------------------------------


def test_an_upload_inside_an_upload_folder_is_located(layout) -> None:
    folders = UploadFolders.for_instance(layout.root)
    photo = _upload(layout.uploads_dir, "att_photo", data=b"photo bytes")

    located = locate_upload(photo, folders, task_id=CURRENT, conversation_id=CONVERSATION)

    assert located == LocatedUpload(Path(photo.local_path()).resolve(), len(b"photo bytes"))


def test_a_deleted_upload_says_to_send_it_again(layout, caplog) -> None:
    photo = _upload(layout.uploads_dir, "att_photo")
    Path(photo.local_path()).unlink()

    with caplog.at_level(logging.INFO, logger=MODULE_LOGGER):
        result = locate_upload(photo, UploadFolders.for_instance(layout.root), task_id=CURRENT, conversation_id=CONVERSATION)

    assert isinstance(result, UnavailableUpload) and result.reason == UPLOAD_FILE_MISSING
    assert "send it again" in result.message and str(layout.root) not in result.message
    assert "event=attachment_upload_missing attachment_id=att_photo" in caplog.text


def test_an_upload_folder_missing_on_this_host_means_another_host(tmp_path: Path, layout) -> None:
    other_host = InstanceLayout.at(tmp_path / "worker_host")  # its state/uploads was never created
    photo = _record("att_photo", other_host.uploads_dir / "web_session" / "att_photo.bin")

    result = locate_upload(photo, UploadFolders.for_instance(other_host.root), task_id=CURRENT, conversation_id=None)

    assert isinstance(result, UnavailableUpload) and result.reason == UPLOAD_NOT_ON_THIS_HOST


def test_a_record_without_a_local_copy_is_reported(layout) -> None:
    remote = _record("att_remote", None)
    remote.storage_refs.append(AttachmentStorageRef(storage_type="nats_object", bucket="uploads", object_name="att_remote"))

    result = locate_upload(remote, UploadFolders.for_instance(layout.root), task_id=CURRENT, conversation_id=None)

    assert isinstance(result, UnavailableUpload) and result.reason == UPLOAD_NO_LOCAL_COPY


@pytest.mark.parametrize("trick", ["outside", "dot_dot", "link"])
def test_a_record_pointing_outside_the_upload_folders_is_refused(layout, caplog, trick: str) -> None:
    secret = layout.state_dir / "secret.db"
    secret.write_bytes(b"not an upload")
    if trick == "outside":
        path = secret
    elif trick == "dot_dot":
        path = layout.uploads_dir / ".." / "secret.db"
    else:
        path = layout.uploads_dir / "web_session" / "looks_like_an_upload.png"
        path.parent.mkdir(parents=True, exist_ok=True)
        os.symlink(secret, path)
    record = _record("att_forged", path)

    with caplog.at_level(logging.WARNING, logger=MODULE_LOGGER):
        result = locate_upload(record, UploadFolders.for_instance(layout.root), task_id=CURRENT, conversation_id=CONVERSATION)

    assert isinstance(result, UnavailableUpload) and result.reason == UPLOAD_OUTSIDE_FOLDERS
    assert str(secret) not in result.message
    assert (
        f"event=attachment_outside_upload_folders attachment_id=att_forged task_id={CURRENT} "
        f"conversation_id={CONVERSATION}"
    ) in caplog.text


def test_the_adapter_hosts_upload_folder_is_trusted_from_the_saved_settings(tmp_path: Path, layout, caplog) -> None:
    config = GatewayInstanceConfig.create(
        instance_name="files", instance_root=str(layout.root), team_id="team", timezone="UTC"
    )
    config.interactive_apps.adapter_host.upload_dir = "transfers/adapter-uploads"
    saved = config.save()
    adapter_folder = layout.root / "transfers" / "adapter-uploads"
    adapter_folder.mkdir(parents=True)
    agent = type("Agent", (), {"id": "lead", "instance_root": str(layout.root), "instance_config_path": str(saved)})()

    folders = UploadFolders.for_agent(agent)

    assert folders.paths == (layout.uploads_dir.resolve(), adapter_folder.resolve())
    photo = _upload(adapter_folder, "att_adapter")
    assert isinstance(locate_upload(photo, folders, task_id=CURRENT, conversation_id=None), LocatedUpload)
    Path(photo.local_path()).unlink()
    assert locate_upload(photo, folders, task_id=CURRENT, conversation_id=None).reason == UPLOAD_FILE_MISSING

    # Unreadable settings: logged, and only state/uploads is trusted.
    agent.instance_config_path = str(tmp_path / "missing" / "instance_config.yaml")
    with caplog.at_level(logging.WARNING, logger=MODULE_LOGGER):
        assert UploadFolders.for_agent(agent).paths == (layout.uploads_dir.resolve(),)
    assert "event=upload_folders_settings_unreadable agent_id=lead" in caplog.text


def test_the_adapter_host_upload_folder_resolves_under_the_instance_root(tmp_path: Path) -> None:
    settings = AdapterHostSettings(upload_dir="runtime/adapter-host-uploads")
    assert settings.resolved_upload_dir(tmp_path) == (tmp_path / "runtime" / "adapter-host-uploads").resolve()
    absolute = AdapterHostSettings(upload_dir=str(tmp_path / "elsewhere"))
    assert absolute.resolved_upload_dir(tmp_path / "instance") == (tmp_path / "elsewhere").resolve()
