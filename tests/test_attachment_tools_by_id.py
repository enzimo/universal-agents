"""The attachment tools open a person's uploads by id, only within the task's scope.

``inspect_user_attachment`` and ``load_local_media_for_model_context`` with an
``attachment_id``, through the real admission path, real SQLite task and
conversation stores and the gateway's own upload storage, for Ann's team lead
(signed task context). Covers:

- a follow-up turn inspects, copies and loads files from an earlier turn by
  id, and no result shows where an upload is stored;
- the media tool uses the upload's recorded type and file name, so a file
  whose name has no suffix still loads as an image, and names each document
  after its file and id, so two uploads with one name do not repeat a name;
- a deleted upload gives ``upload_unavailable`` asking for the file again,
  also when it is deleted after it was found and before it is read;
- an upload that cannot be opened safely is not read, and the error does not
  say where it is stored; a failed copy into the task's folder says why;
- an upload this host cannot check (a link loop, no permission) gives
  ``upload_unavailable`` without its location, logged with the ids;
- a copy made before the upload is deleted is inspected and reported, and an
  error reading the copy keeps its ``workspace_path``;
- a workspace path that is missing or a folder is a plain error, not
  ``remote_copy_required``;
- an id outside the task's scope gives ``unknown_attachment_id`` with the
  files it may use: work for another person in Ann's conversation, Ann's
  other conversation, and a delegated task, which opens only the files its
  delegation gave it;
- a record pointing outside the upload folders is refused by both tools;
- an id from a turn saved before files were recorded says so;
- an answer's files open by id after a restart without a conversation store;
- the media tool takes exactly one of path and id, and checks the model and
  the size before reading an upload.
"""

from __future__ import annotations

import logging
import os
import re
from pathlib import Path
from typing import Any

import pytest
import pytest_asyncio

from access_control import safe_open
from access_control.identity import ENV_VERIFY_KEYS
from assistant_gateway.inbound_attachments import (
    InboundAttachmentStoragePolicy,
    store_inbound_attachment_bytes_with_policy,
)
from custom_agents import task_message_runtime, task_steering_runtime
from custom_messages.contracts.attachments_v1 import AttachmentStorageRef, UserTaskAttachment
from custom_messages.contracts.routing_v1 import MessageHeader, MessageMeta
from custom_messages.contracts.task_v1 import Task
from custom_messages.enums import MessageMetaType, TaskSteeringType
from custom_tools import file_access, task_uploads
from custom_tools.local_media_context_tool import load_local_media_for_model_context
from custom_tools.task_uploads import (
    RESULT_REFUSED,
    RESULT_UNKNOWN_ATTACHMENT,
    RESULT_UPLOAD_UNAVAILABLE,
)
from custom_tools import user_attachment_tools
from custom_tools.user_attachment_tools import inspect_user_attachment
from custom_tools.workspace_scope import current_workspace_scope
from runtime_settings.instance_layout import InstanceLayout
from tests.broker_access_support import context_verify_keys, forget_task_context_keys
from tests.task_work_support import use_agent
from tests.test_conversation_attachments import saved_before_upgrade
from tests.test_conversation_attachments_regression import ANN, CONVERSATION, SESSION, _admit, _answer, _lead, _payload
from tests.test_turn_files_recording import _send, _steering

pytestmark = pytest.mark.integration

FIRST, FOLLOW_UP = "task_first", "task_follow_up"
BOB = "usr_bob"
PHOTO_BYTES = b"\x89PNG\r\n\x1a\n" + bytes(range(32))
CSV_TEXT = "plant,water\nfig,weekly\n"
PDF_BYTES = b"%PDF-1.4\nnot a real pdf\n"
TOOLS_LOGGER = "custom_tools.task_uploads"


@pytest.fixture(autouse=True)
def _verify_keys(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv(ENV_VERIFY_KEYS, context_verify_keys())
    forget_task_context_keys()
    yield
    forget_task_context_keys()


@pytest.fixture(autouse=True)
def _quiet_runtime(monkeypatch: pytest.MonkeyPatch):
    # Each test plays the turns itself: no queue drain, and steering statuses are not published.
    monkeypatch.setattr(task_message_runtime, "schedule_pending_work", lambda _agent: None)
    monkeypatch.setattr(task_steering_runtime, "schedule_pending_work", lambda _agent: None)

    async def no_status(*_args, **_kwargs):
        return None

    monkeypatch.setattr(task_steering_runtime, "_send_steering_status_response", no_status)


@pytest.fixture
def layout(tmp_path: Path) -> InstanceLayout:
    layout = InstanceLayout.at(tmp_path / "instance")
    layout.ensure_directories()
    return layout


def _stored(layout: InstanceLayout, name: str, data: bytes, content_type: str) -> UserTaskAttachment:
    """A file the gateway stored for Ann's session, with its record."""

    return store_inbound_attachment_bytes_with_policy(
        data=data,
        filename=name,
        content_type=content_type,
        policy=InboundAttachmentStoragePolicy(cache_dir=layout.uploads_dir),
        session_key=SESSION,
        conversation_id=CONVERSATION,
        origin="web_chat",
    ).attachment


@pytest_asyncio.fixture
async def earlier(tmp_path: Path, layout: InstanceLayout) -> dict[str, Any]:
    """Turn 1 received a photo, a CSV and a PDF; turn 2 (no files) runs in a restarted lead."""

    files = {
        "photo": _stored(layout, "garden plant.png", PHOTO_BYTES, "image/png"),
        "csv": _stored(layout, "watering.csv", CSV_TEXT.encode(), "text/csv"),
        "pdf": _stored(layout, "Care Guide.pdf", PDF_BYTES, "application/pdf"),
    }
    first_lead = _lead(tmp_path, layout)
    await _admit(first_lead, task_id=FIRST, text="Here are my plant files.", attachments=list(files.values()))
    await _answer(first_lead, FIRST, "Thanks, I have them.")
    lead = _lead(tmp_path, layout)
    await _admit(lead, task_id=FOLLOW_UP, text="How often should I water it?", attachments=[])
    use_agent(lead, FOLLOW_UP)
    return {"lead": lead, "layout": layout, **files}


def _inspect(attachment_id: str, **kwargs: Any) -> dict[str, Any]:
    return inspect_user_attachment._tool_func(attachment_id=attachment_id, **kwargs)


def _load(attachment_id: str = "", **kwargs: Any) -> dict[str, Any]:
    return load_local_media_for_model_context._tool_func(attachment_id=attachment_id, **kwargs)


def _texts(result: dict[str, Any]) -> str:
    return "\n".join(block.get("text", "") for block in result["content"] if "text" in block)


def _no_storage_details(result: dict[str, Any], layout: InstanceLayout) -> None:
    """No path into ``state/`` (which holds the upload folder) and no storage field reaches the model."""

    text = _texts(result)
    assert str(layout.state_dir) not in text and str(layout.uploads_dir) not in text
    assert "storage_refs" not in text and "local_path" not in text


def _unknown(result: dict[str, Any], attachment_id: str) -> list[str]:
    """The ids an ``unknown_attachment_id`` result offers instead of ``attachment_id``."""

    assert result["status"] == "error", result
    payload = _payload(result)
    assert (payload["result"], payload["attachment_id"]) == (RESULT_UNKNOWN_ATTACHMENT, attachment_id)
    return [item["attachment_id"] for item in payload["available_attachments"]]


# --- A follow-up turn opens earlier files ---------------------------------------------------------


@pytest.mark.asyncio
async def test_a_follow_up_inspects_and_copies_an_earlier_text_file_by_id(earlier) -> None:
    csv: UserTaskAttachment = earlier["csv"]

    result = _inspect(csv.attachment_id, copy_to_workspace=True)

    assert result["status"] == "success", result
    payload = _payload(result)
    assert payload["attachment"]["filename"] == "watering.csv"
    assert payload["attachment"]["sha256"] == csv.sha256
    assert [line["text"] for line in payload["inspection"]["lines"]] == ["plant,water", "fig,weekly"]
    # The copy is in Ann's own folder, where shell and Python can process it.
    copy = Path(payload["workspace_path"])
    assert copy.is_relative_to(earlier["layout"].person_dir(ANN))
    assert copy.read_text(encoding="utf-8") == CSV_TEXT
    _no_storage_details(result, earlier["layout"])


@pytest.mark.asyncio
async def test_a_follow_up_loads_an_earlier_pdf_as_a_document_named_after_its_file(earlier) -> None:
    earlier["lead"].supports_file_input = True

    result = _load(earlier["pdf"].attachment_id, purpose="Read the care guide.")

    assert result["status"] == "success", result
    label, block = result["content"]
    pdf_id = earlier["pdf"].attachment_id
    name = "Care Guide (" + pdf_id.replace("_", "-") + ")"
    assert block == {"document": {"format": "pdf", "name": name, "source": {"bytes": PDF_BYTES}}}
    metadata = _payload({"content": [label]})
    assert (metadata["attachment_id"], metadata["filename"]) == (pdf_id, "Care Guide.pdf")
    assert metadata["content_type"] == "application/pdf"
    _no_storage_details(result, earlier["layout"])


@pytest.mark.asyncio
async def test_two_uploads_with_the_same_file_name_load_as_documents_with_different_names(
    tmp_path: Path, layout: InstanceLayout
) -> None:
    """Bedrock refuses a request in which two document blocks share a name, compared without regard to case."""

    lead = _lead(tmp_path, layout)
    lead.supports_file_input = True
    first = _stored(layout, "invoice.pdf", PDF_BYTES, "application/pdf")
    corrected = _stored(layout, "Invoice (final) v2_draft #3.PDF", PDF_BYTES + b"corrected", "application/pdf")
    await _admit(lead, task_id=FIRST, text="Compare these invoices.", attachments=[first, corrected])
    use_agent(lead, FIRST)

    names = [_load(item.attachment_id)["content"][1]["document"]["name"] for item in (first, corrected)]

    # The gateway saved the second name as "Invoice _final_ v2_draft _3.PDF" (sanitize_attachment_filename).
    assert names == [
        "invoice (" + first.attachment_id.replace("_", "-") + ")",
        "Invoice final v2 draft 3 (" + corrected.attachment_id.replace("_", "-") + ")",
    ]
    assert len({name.casefold() for name in names}) == 2
    # Only letters, digits, single spaces, hyphens, parentheses and square brackets.
    assert all(re.fullmatch(r"[A-Za-z0-9()\[\]-]+( [A-Za-z0-9()\[\]-]+)*", name) for name in names)


@pytest.mark.asyncio
async def test_an_upload_without_a_file_suffix_loads_by_its_recorded_type(tmp_path: Path, layout: InstanceLayout) -> None:
    lead = _lead(tmp_path, layout)
    # The stored file is named after the id plus the name's suffix, here none, so only the record knows its type.
    photo = _stored(layout, "photo from phone", PHOTO_BYTES, "image/png")
    await _admit(lead, task_id=FIRST, text="What is this?", attachments=[photo])
    use_agent(lead, FIRST)

    result = _load(photo.attachment_id)

    assert result["status"] == "success", result
    assert result["content"][1] == {"image": {"format": "png", "source": {"bytes": PHOTO_BYTES}}}
    assert _payload(result)["content_type"] == "image/png"


@pytest.mark.asyncio
async def test_a_deleted_upload_asks_for_the_file_again_from_both_tools(earlier, caplog) -> None:
    photo: UserTaskAttachment = earlier["photo"]
    Path(photo.local_path()).unlink()

    with caplog.at_level(logging.INFO):
        results = [_inspect(photo.attachment_id), _load(photo.attachment_id)]

    for result in results:
        assert result["status"] == "error", result
        payload = _payload(result)
        assert payload["result"] == RESULT_UPLOAD_UNAVAILABLE
        assert "send it again" in payload["error"]
        assert payload["attachment"]["attachment_id"] == photo.attachment_id
        _no_storage_details(result, earlier["layout"])
    assert f"event=attachment_upload_missing attachment_id={photo.attachment_id} task_id={FOLLOW_UP}" in caplog.text


@pytest.mark.asyncio
async def test_an_upload_deleted_while_it_is_opened_asks_for_the_file_again_without_its_location(
    earlier, monkeypatch, caplog
) -> None:
    csv: UserTaskAttachment = earlier["csv"]
    photo: UserTaskAttachment = earlier["photo"]
    earlier["lead"].supports_image_input = True
    found = task_uploads.locate_upload

    def found_then_deleted(attachment: UserTaskAttachment, *args: Any, **kwargs: Any) -> Any:
        # Upload retention deletes the file after the tool found it and before it reads it.
        located = found(attachment, *args, **kwargs)
        Path(attachment.local_path()).unlink()
        return located

    monkeypatch.setattr(task_uploads, "locate_upload", found_then_deleted)

    with caplog.at_level(logging.WARNING, logger=TOOLS_LOGGER):
        results = {
            "inspect": (_inspect(csv.attachment_id), csv),
            "copy": (_inspect(earlier["pdf"].attachment_id, copy_to_workspace=True), earlier["pdf"]),
            "load": (_load(photo.attachment_id), photo),
        }

    for name, (result, attachment) in results.items():
        assert result["status"] == "error", (name, result)
        payload = _payload(result)
        assert (payload["result"], payload["reason"]) == (RESULT_UPLOAD_UNAVAILABLE, safe_open.REASON_NOT_FOUND), name
        assert "send it again" in payload["error"], name
        assert payload["attachment"]["attachment_id"] == attachment.attachment_id
        _no_storage_details(result, earlier["layout"])
    assert f"event=attachment_read_failed attachment_id={photo.attachment_id}" in caplog.text


@pytest.mark.asyncio
async def test_an_upload_that_cannot_be_opened_safely_is_not_read_and_its_location_stays_hidden(earlier, caplog) -> None:
    photo: UserTaskAttachment = earlier["photo"]
    # A second hard link makes the file reachable from elsewhere; it is not opened.
    os.link(photo.local_path(), Path(photo.local_path()).with_name("second_link.png"))
    earlier["lead"].supports_image_input = True

    with caplog.at_level(logging.WARNING, logger=TOOLS_LOGGER):
        loaded = _load(photo.attachment_id)

    assert loaded["status"] == "error"
    payload = _payload(loaded)
    assert (payload["reason"], payload["attachment"]["attachment_id"]) == (safe_open.REASON_SEVERAL_LINKS, photo.attachment_id)
    assert all("image" not in block for block in loaded["content"])
    _no_storage_details(loaded, earlier["layout"])
    assert f"event=attachment_read_failed attachment_id={photo.attachment_id}" in caplog.text


@pytest.mark.asyncio
async def test_a_failed_copy_into_the_task_folder_says_why(earlier, monkeypatch) -> None:
    def full_folder(*_args, **_kwargs):
        raise file_access.PathPolicyError("The folder users/ann is full.", reason_code="quota")

    monkeypatch.setattr(file_access, "writable", full_folder)

    result = _inspect(earlier["csv"].attachment_id, copy_to_workspace=True)

    assert result["status"] == "error"
    assert _payload(result)["error"] == "The folder users/ann is full."
    _no_storage_details(result, earlier["layout"])


@pytest.mark.asyncio
@pytest.mark.parametrize("cause", ["link_loop", "no_permission"])
async def test_an_upload_this_host_cannot_check_answers_upload_unavailable_without_its_location(
    earlier, caplog, cause: str
) -> None:
    """SEC-2: any error while checking an upload (not only "not found") gives a fixed answer and no path."""

    csv: UserTaskAttachment = earlier["csv"]
    earlier["lead"].supports_image_input = True
    upload = Path(csv.local_path())
    if cause == "link_loop":
        upload.unlink()
        upload.symlink_to(upload.with_name("loop_back"))
        upload.with_name("loop_back").symlink_to(upload)
    elif os.geteuid() == 0:
        pytest.skip("root reads a folder without permissions")
    else:
        upload.parent.chmod(0)

    try:
        with caplog.at_level(logging.WARNING, logger="custom_agents.conversation_attachments"):
            results = [_inspect(csv.attachment_id), _inspect(csv.attachment_id, copy_to_workspace=True), _load(csv.attachment_id)]
    finally:
        upload.parent.chmod(0o755)

    for result in results:
        assert result["status"] == "error", result
        payload = _payload(result)
        assert (payload["result"], payload["reason"]) == (RESULT_UPLOAD_UNAVAILABLE, "unreadable")
        assert "send it again" in payload["error"]
        assert payload["attachment"]["attachment_id"] == csv.attachment_id
        assert "workspace_path" not in payload
        _no_storage_details(result, earlier["layout"])
    unreadable = [record.getMessage() for record in caplog.records if "event=attachment_upload_unreadable" in record.getMessage()]
    assert len(unreadable) == 3
    assert all(f"attachment_id={csv.attachment_id} task_id={FOLLOW_UP}" in message for message in unreadable)
    assert not any(str(earlier["layout"].uploads_dir) in message for message in unreadable)


@pytest.mark.asyncio
async def test_a_copy_made_before_the_upload_is_deleted_is_inspected_and_reported(earlier, monkeypatch) -> None:
    """FAIL-3: upload retention deletes the upload right after the copy; the copy's result is not lost."""

    csv: UserTaskAttachment = earlier["csv"]
    copy_into_task_folder = user_attachment_tools._copy_into_task_folder

    def copy_then_retention(upload: Any) -> Path:
        copied = copy_into_task_folder(upload)
        Path(upload.attachment.local_path()).unlink()
        return copied

    monkeypatch.setattr(user_attachment_tools, "_copy_into_task_folder", copy_then_retention)

    result = _inspect(csv.attachment_id, copy_to_workspace=True)

    assert result["status"] == "success", result
    payload = _payload(result)
    assert Path(payload["workspace_path"]).read_text(encoding="utf-8") == CSV_TEXT
    assert [line["text"] for line in payload["inspection"]["lines"]] == ["plant,water", "fig,weekly"]
    _no_storage_details(result, earlier["layout"])


@pytest.mark.asyncio
async def test_an_error_reading_the_copy_keeps_its_workspace_path(earlier, monkeypatch) -> None:
    def unreadable_copy(*_args: Any, **_kwargs: Any) -> Any:
        raise file_access.PathPolicyError("The file uploads/notes.csv cannot be read.", reason_code="denied")

    monkeypatch.setattr(file_access, "read_bytes", unreadable_copy)

    result = _inspect(earlier["csv"].attachment_id, copy_to_workspace=True)

    assert result["status"] == "error"
    payload = _payload(result)
    assert payload["error"] == "The file uploads/notes.csv cannot be read."
    assert Path(payload["workspace_path"]).read_text(encoding="utf-8") == CSV_TEXT
    assert "result" not in payload  # neither upload_unavailable nor a request to send the file again
    _no_storage_details(result, earlier["layout"])


@pytest.mark.asyncio
@pytest.mark.parametrize("target", ["missing", "folder"])
async def test_a_workspace_path_that_is_not_a_file_is_not_a_remote_copy(earlier, target: str) -> None:
    """A path names a file in the task's own folders: a typo or a folder is not a person's upload on another host."""

    home = current_workspace_scope().home
    home.mkdir(parents=True, exist_ok=True)
    named = home / "notes-typo.csv" if target == "missing" else home

    for result in (inspect_user_attachment._tool_func(path=str(named)), _load(path=str(named))):
        assert result["status"] == "error", result
        payload = _payload(result)
        assert payload["error"] == "Local file is unavailable."
        assert "result" not in payload and "remote_copy_required" not in _texts(result)

# --- Files outside the task's scope ----------------------------------------------------------------


@pytest.mark.asyncio
async def test_work_for_another_person_in_anns_conversation_cannot_open_its_files(earlier, caplog) -> None:
    lead = earlier["lead"]
    lead.task_manager.add_task(
        Task(task_id="task_bob", task_type=MessageMetaType.REQUEST, problem_statement="Show me Ann's photo."),
        MessageHeader(from_agent_id="gateway"),
        MessageMeta(conversation_id=CONVERSATION),
        owner_person_id=BOB,
    )
    use_agent(lead, "task_bob")
    photo: UserTaskAttachment = earlier["photo"]

    with caplog.at_level(logging.INFO):
        results = [_inspect(photo.attachment_id), _load(photo.attachment_id)]

    for result in results:
        assert _unknown(result, photo.attachment_id) == []
        _no_storage_details(result, earlier["layout"])
    assert "event=conversation_files_owner_mismatch" in caplog.text
    assert f"event=attachment_id_not_available attachment_id={photo.attachment_id} task_id=task_bob" in caplog.text


@pytest.mark.asyncio
async def test_anns_other_conversation_cannot_open_this_conversations_files(earlier) -> None:
    lead = earlier["lead"]
    await _admit(lead, task_id="task_elsewhere", text="Another topic.", attachments=[], conversation_id="conv_other")
    use_agent(lead, "task_elsewhere")
    photo: UserTaskAttachment = earlier["photo"]

    for result in (_inspect(photo.attachment_id), _load(photo.attachment_id)):
        assert _unknown(result, photo.attachment_id) == []


@pytest.mark.asyncio
async def test_a_delegated_task_opens_only_the_files_its_delegation_gave_it(earlier) -> None:
    lead = earlier["lead"]
    given: UserTaskAttachment = earlier["csv"]
    lead.task_manager.add_task(
        Task(
            task_id="task_worker",
            parent_task_id=FOLLOW_UP,
            task_type=MessageMetaType.REQUEST,
            problem_statement="Read the watering table.",
            attachments=[given],
        ),
        MessageHeader(from_agent_id="lead"),
        MessageMeta(conversation_id=CONVERSATION),
        owner_person_id=ANN,
    )
    use_agent(lead, "task_worker")

    assert _inspect(given.attachment_id)["status"] == "success"
    photo: UserTaskAttachment = earlier["photo"]
    for result in (_inspect(photo.attachment_id), _load(photo.attachment_id)):
        # It cannot reach the conversation's other files, and is offered only its own.
        assert _unknown(result, photo.attachment_id) == [given.attachment_id]
        _no_storage_details(result, earlier["layout"])


@pytest.mark.asyncio
async def test_a_record_pointing_outside_the_upload_folders_is_refused_by_both_tools(earlier, caplog) -> None:
    lead, layout = earlier["lead"], earlier["layout"]
    secret = layout.state_dir / "secret.png"
    secret.write_bytes(PHOTO_BYTES)
    forged = UserTaskAttachment(
        attachment_id="att_forged",
        filename="looks fine.png",
        content_type="image/png",
        kind="image",
        size_bytes=len(PHOTO_BYTES),
        sha256="0" * 64,
        storage_refs=[AttachmentStorageRef(storage_type="local", path=str(secret))],
    )
    # A delegated task's files are not vouched for by the gateway; any agent process could send this one.
    lead.task_manager.add_task(
        Task(
            task_id="task_forged",
            parent_task_id=FOLLOW_UP,
            task_type=MessageMetaType.REQUEST,
            problem_statement="Look at this.",
            attachments=[forged],
        ),
        MessageHeader(from_agent_id="lead"),
        MessageMeta(conversation_id=CONVERSATION),
        owner_person_id=ANN,
    )
    use_agent(lead, "task_forged")

    with caplog.at_level(logging.WARNING):
        results = [_inspect("att_forged"), _load("att_forged")]

    for result in results:
        assert result["status"] == "error", result
        assert _payload(result)["result"] == RESULT_REFUSED
        assert all("image" not in block for block in result["content"])
        _no_storage_details(result, layout)
    assert "event=attachment_outside_upload_folders attachment_id=att_forged task_id=task_forged" in caplog.text


@pytest.mark.asyncio
async def test_an_id_from_a_turn_saved_before_files_were_recorded_says_so(earlier) -> None:
    saved_before_upgrade(earlier["lead"]._conversation_store, FIRST)
    photo: UserTaskAttachment = earlier["photo"]

    result = _inspect(photo.attachment_id)

    assert _unknown(result, photo.attachment_id) == []
    assert _payload(result)["note"].startswith("Some files sent with earlier requests (the latest in turn 1)")


# --- An answer's files after a restart --------------------------------------------------------------


@pytest.mark.asyncio
async def test_an_answers_files_open_by_id_after_a_restart_without_a_conversation_store(
    tmp_path: Path, layout: InstanceLayout
) -> None:
    from tests.test_turn_files_recording import TASK

    lead = _lead(tmp_path, layout)
    await _admit(lead, task_id=TASK, text="Which pan should I use?", attachments=[])
    lead._conversation_store = None  # the task's history is the only record of the answer's files
    pan = _stored(layout, "pan.png", PHOTO_BYTES, "image/png")
    await _send(lead, _steering(TaskSteeringType.INTERRUPT_RESPONSE, [pan], "answer_pan", "This pan."))

    restarted = _lead(tmp_path, layout)
    restarted._conversation_store = None
    assert restarted.task_manager.restore_runtime_state() is not None
    use_agent(restarted, TASK)

    inspected = _inspect(pan.attachment_id)
    loaded = _load(pan.attachment_id)

    assert inspected["status"] == "success", inspected
    assert _payload(inspected)["attachment"]["filename"] == "pan.png"
    assert loaded["status"] == "success", loaded
    assert loaded["content"][1] == {"image": {"format": "png", "source": {"bytes": PHOTO_BYTES}}}


# --- The media tool's checks -------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_the_media_tool_takes_exactly_one_of_path_and_attachment_id(earlier) -> None:
    photo: UserTaskAttachment = earlier["photo"]

    for result in (_load(), _load(photo.attachment_id, path="garden plant.png")):
        assert result["status"] == "error"
        assert _payload(result)["error"].startswith("Give exactly one of attachment_id")


@pytest.mark.asyncio
async def test_the_media_tool_checks_the_model_and_the_size_before_reading_an_upload(earlier, monkeypatch) -> None:
    lead, photo = earlier["lead"], earlier["photo"]

    def not_read(*_args, **_kwargs):
        raise AssertionError("the upload was read before the checks")

    monkeypatch.setattr("custom_agents.conversation_attachments.LocatedUpload.read_bytes", not_read)
    lead.supports_image_input = False
    no_images = _load(photo.attachment_id)
    lead.supports_image_input = True
    too_large = _load(photo.attachment_id, max_bytes=4)

    assert _payload(no_images)["capability_field"] == "supports_image_input"
    assert _payload(too_large)["error"] == "File is too large to insert into model context."
    assert _payload(too_large)["size_bytes"] == len(PHOTO_BYTES)
    for result in (no_images, too_large):
        assert result["status"] == "error"
        assert _payload(result)["attachment_id"] == photo.attachment_id
        _no_storage_details(result, earlier["layout"])
