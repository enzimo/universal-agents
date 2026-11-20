"""The upgrade step moves files of chat apps an adapter host ran (0.9.13 and earlier) into ``state/uploads``.

Up to 0.9.13 such a file stayed in the adapter host's transfer folder, and its
path was saved in the gateway's attachment record and in the conversation
turns that received it. From 0.10 agents open files only in ``state/uploads``
(``custom_agents.conversation_attachments.UploadFolder``), so the step
(``instance_upgrades.adapter_host_files``) moves them and rewrites
both records, once, before the team starts. The starting state is written by
v0.9.13's own code (``tests/fixtures/upgrade/0.9.13-adapter-host-files.sql``):

- ``att_photo``: in the transfer folder, with a gateway record, on turns 1 and 2;
- ``att_lost``: a gateway record and turn 1, but its file is gone;
- ``att_orphan``: in the transfer folder, but only turn 2 names it (no gateway record);
- ``att_web``: a web chat file already in ``state/uploads`` (left alone).

Covered here: the move (same bytes and modification time, both records point
at it, it opens by id and is listed for the model), files proven absent
(dropped from their turns, which then say their request's files are not all
recorded), records it refuses and keeps (a link, another checksum, an id that
is not a file name, a destination already taken, a conversation entry naming
another path or with no gateway record while its file is here, a file that is
not what 0.9.13 wrote, a name the file system refuses, a NUL byte in a stored
name, a file it cannot read, a pipe in place of a file, refused without
waiting, a copy that reads back differently), the folder missing or holding
none of the files (nothing changes, also when one record names a file too
long or with a NUL byte; ``--files-gone`` then finishes, also beside a
running team that writes the same turns and records), a folder it cannot
use (startup stops naming the folder and nothing changes), one error rule
checked by injecting an error at each file and folder operation in turn
(``test_one_error_rule_holds_at_every_operation``), a run stopped part way
(the next run finishes), a move across file systems (short writes, a folder
sync that fails or is not offered after the copy is in place), upload
retention and a person's removal finding the moved file, records read past
one batch, a transfer folder set to ``state/uploads``, a run that waits for
another's lock (and a state folder without locks), the launcher step and the
operator command. Files left in the transfer folder that no record names
(a removed person's, one whose records were deleted while the step waited)
are counted and reported once by the run that writes the marker, also after a
run stopped part way: it lists the names the gateway gives (never another
name, never contents) in a file beside the marker, names the first ones in
its warning, and the warning's delete command, run by ``sh`` in an instance
whose root holds a space, deletes exactly those and nothing while the marker
is missing; moved and not-moved
files (named by any one kind of record), files the 0.10 gateway reads at its
start (of messages still ``accepted``, checked against the real adapter host
service) and other files are not counted, a link is not followed, and a
folder it cannot list or that changed after the moves gives a warning and no
count without stopping the start. A held request still naming the old path gets
the stuck-task notice: ``tests/test_stuck_tasks.py`` (``ADAPTER_HOST_FOLDER``).
Held guidance the gateway sends again after the step keeps its file through
the team lead's real steering handler:
``tests/test_adapter_host_held_guidance_resend.py``.
Runs on SQLite and, with ``LIVE_TEST_POSTGRES_DSN``, on PostgreSQL.
"""

from __future__ import annotations

import argparse
import asyncio
import errno
import fcntl
import hashlib
import json
import logging
import os
import re
import shlex
import shutil
import subprocess
import sys
import threading
from collections.abc import Callable, Iterator
from dataclasses import asdict, dataclass, replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from uuid import uuid4

import pytest

import instance_upgrades.adapter_host_files as upgrade_module
from instance_upgrades.adapter_host_files import (
    DELETE_REFUSED,
    LIST_ID_FIELD,
    NAMED_ELSEWHERE_FIELD,
    RECORDED_ELSEWHERE_FIELD,
    TRANSFER_FILE_PREFIX,
    NamedElsewhere,
    upgrade_adapter_host_files,
)
from assistant_gateway.adapter_host.domain import AdapterInboxRecord, AdapterUploadRecord
from assistant_gateway.adapter_host.service import AdapterHostApplicationService
from assistant_gateway.contracts.adapter_host_v1 import (
    ADAPTER_HOST_PROTOCOL_VERSION,
    AdapterAttachmentV1,
    ChannelInboundMessageV1,
    encode_adapter_host_message_v1,
)
from assistant_gateway.domain.request_state import HeldGuidanceRecord
from custom_agents.conversation_attachments import (
    UNRECORDED_TURNS_NOTE,
    LocatedUpload,
    TaskAttachments,
    UploadFolder,
    inventory_text,
    keep_upload_files,
    locate_upload,
)
from custom_agents.conversation_store import ConversationStore
from custom_messages.contracts.attachments_v1 import AttachmentStorageRef, UserTaskAttachment
from custom_messages.contracts.task_v1 import Task, TaskSteering
from custom_messages.enums import MessageMetaType, TaskSteeringType
from runtime_settings.instance_layout import InstanceLayout
from runtime_settings.interactive_apps import AdapterHostSettings
from storage.adapter_host_repository import create_adapter_host_repository
from storage.config import StorageSettings, resolve_storage_settings
from storage.conversation_repository import _RelationalConversationRepository, create_conversation_repository
from storage.gateway_state_repository import _RelationalGatewayStateRepository, create_gateway_state_repository
from storage.postgres_connection import connect_postgres
from storage.sqlite_connection import connect_sqlite

FIXTURE = Path(__file__).parent / "fixtures" / "upgrade" / "0.9.13-adapter-host-files.sql"
PLACEHOLDER_ROOT = "/instance/"
CONVERSATION = "conv_hosted"
PERSON = "usr_ann"
MODULE_LOGGER = upgrade_module.__name__
REPO_ROOT = Path(__file__).resolve().parents[1]
# The bytes v0.9.13's records were written for (the fixture generator's CONTENT).
CONTENT = {
    "att_photo": b"photo sent through a hosted chat in 0.9.13",
    "att_lost": b"a file whose bytes are gone",
    "att_orphan": b"a file only a conversation turn names",
    "att_web": b"a web chat file already in state/uploads",
}
SENT_AT = 1_700_000_000  # the photo's modification time in the transfer folder


@dataclass
class _Instance:
    root: Path
    layout: InstanceLayout
    gateway_settings: StorageSettings
    tasks_settings: StorageSettings

    @property
    def transfer(self) -> Path:
        return self.root / "runtime" / "adapter-host-uploads"

    @property
    def photo_source(self) -> Path:
        return self.transfer / "adapter_upload_photo"

    @property
    def photo_destination(self) -> Path:
        return self.layout.uploads_dir.resolve() / "telegram_chat_7" / "att_photo.png"

    def config(self) -> Any:
        """What the step reads of a ``GatewayInstanceConfig``."""

        return SimpleNamespace(
            layout=self.layout,
            instance_root=str(self.root),
            instance_root_path=self.root,
            interactive_apps=SimpleNamespace(adapter_host=AdapterHostSettings()),
            state_storage_settings=self.gateway_settings,
            manager_storage_settings=self.tasks_settings,
        )

    def run(self, **kwargs: Any) -> upgrade_module.UpgradeResult:
        return upgrade_adapter_host_files(self.config(), **kwargs)

    def conversations(self) -> ConversationStore:
        return ConversationStore(
            str(self.root / "unused.db"),
            artifacts_dir=self.layout.conversation_artifacts_dir,
            storage_settings=self.tasks_settings,
        )

    def gateway(self) -> Any:
        return create_gateway_state_repository(self.gateway_settings)

    def files(self) -> dict[str, str | None]:
        """Each listed file's recorded path, newest turn first."""

        return {entry.attachment.attachment_id: entry.attachment.local_path() for entry in self.conversations().list_turn_attachments(CONVERSATION)}

    def repository(self) -> Any:
        return create_conversation_repository(self.tasks_settings, artifacts_dir=self.layout.conversation_artifacts_dir)

    def turn_paths(self, task_id: str) -> dict[str, str | None]:
        """What one turn's stored column records: each file's local path."""

        return {item["attachment_id"]: _local(item) for item in json.loads(self._raw_attachments(task_id))}

    def _raw_attachments(self, task_id: str) -> str:
        bind = "%s" if self.tasks_settings.backend == "postgres" else "?"
        with self._tasks_connection() as conn:
            row = conn.execute(f"SELECT attachments_json FROM conversation_task_links WHERE task_id = {bind}", (task_id,)).fetchone()
        return str(row[0] or "[]")

    def recorded(self, task_id: str) -> int:
        bind = "%s" if self.tasks_settings.backend == "postgres" else "?"
        with self._tasks_connection() as conn:
            row = conn.execute(
                f"SELECT attachments_recorded FROM conversation_task_links WHERE task_id = {bind}", (task_id,)
            ).fetchone()
        return int(row[0])

    def _tasks_connection(self) -> Any:
        if self.tasks_settings.backend == "postgres":
            return connect_postgres(self.tasks_settings.postgres_dsn)
        return connect_sqlite(self.tasks_settings.sqlite_path)

    def gateway_connection(self) -> Any:
        if self.gateway_settings.backend == "postgres":
            return connect_postgres(self.gateway_settings.postgres_dsn)
        return connect_sqlite(self.gateway_settings.sqlite_path)


def _local(item: dict[str, Any]) -> str | None:
    return next((ref.get("path") for ref in item.get("storage_refs") or [] if ref.get("storage_type") == "local"), None)


def _sections(root: Path) -> dict[str, list[str]]:
    sections: dict[str, list[str]] = {}
    current = ""
    for line in FIXTURE.read_text().splitlines():
        if line.startswith("-- database: "):
            current = line.removeprefix("-- database: ")
        elif line.strip() and not line.startswith("--"):
            sections.setdefault(current, []).append(line.replace(PLACEHOLDER_ROOT, f"{root}/"))
    return sections


def _load(connect: Callable[[], Any], statements: list[str]) -> None:
    with connect() as conn:
        for statement in statements:
            conn.execute(statement)
        conn.commit()


def _write(path: Path, data: bytes) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    return path


@pytest.fixture(
    params=[
        "sqlite",
        pytest.param("postgres", marks=[pytest.mark.live, pytest.mark.postgres]),
    ]
)
def instance(tmp_path: Path, request: pytest.FixtureRequest) -> Iterator[_Instance]:
    root = (tmp_path / "my instance").resolve()  # a space, so the warning's delete command must quote (C04 of F6)
    layout = InstanceLayout.at(root)
    sections = _sections(root)
    made = _Instance(root, layout, *_databases(tmp_path, request, sections))
    photo = _write(made.photo_source, CONTENT["att_photo"])
    os.chmod(photo, 0o640)
    os.utime(photo, (SENT_AT, SENT_AT))
    _write(made.transfer / "adapter_upload_orphan", CONTENT["att_orphan"])
    _write(layout.uploads_dir / "web_ann" / "att_web.png", CONTENT["att_web"])
    yield made
    for cleanup in getattr(request.node, "_schema_cleanups", []):
        cleanup()


def _databases(tmp_path: Path, request: pytest.FixtureRequest, sections: dict[str, list[str]]) -> tuple[StorageSettings, StorageSettings]:
    if request.param == "sqlite":
        gateway_db, tasks_db = str(tmp_path / "gateway_state.db"), str(tmp_path / "tasks.db")
        _load(lambda: connect_sqlite(gateway_db), sections["gateway"])
        _load(lambda: connect_sqlite(tasks_db), sections["tasks"])
        return (
            resolve_storage_settings({"backend": "sqlite", "sqlite": {"path": gateway_db}}),
            resolve_storage_settings({"backend": "sqlite", "sqlite": {"path": tasks_db}}),
        )
    from psycopg.conninfo import make_conninfo

    dsn = os.environ.get("LIVE_TEST_POSTGRES_DSN")
    if not dsn:
        pytest.skip("Set LIVE_TEST_POSTGRES_DSN to a disposable database to run the PostgreSQL upgrade tests.")
    schema = "adapter_files_" + uuid4().hex
    with connect_postgres(dsn) as conn:
        conn.execute(f"CREATE SCHEMA {schema}")

    def drop() -> None:
        with connect_postgres(dsn) as conn:
            conn.execute(f"DROP SCHEMA {schema} CASCADE")

    request.node._schema_cleanups = [drop]
    scoped = make_conninfo(dsn, options=f"-csearch_path={schema}")
    _load(lambda: connect_postgres(scoped), sections["gateway"] + sections["tasks"])
    settings = resolve_storage_settings({"backend": "postgres", "postgres": {"dsn": scoped}})
    return settings, settings


def _events(caplog: pytest.LogCaptureFixture, event: str) -> list[str]:
    """Messages with exactly this event (one event name can be the start of another)."""

    return [record.getMessage() for record in caplog.records if f"event={event} " in f"{record.getMessage()} "]


# --- The move ------------------------------------------------------------------------------------


def test_a_hosted_chat_file_moves_into_state_uploads_and_opens_by_id(instance: _Instance, caplog) -> None:
    with caplog.at_level(logging.INFO, logger=MODULE_LOGGER):
        result = instance.run()

    destination = instance.photo_destination
    assert result.finished and destination.read_bytes() == CONTENT["att_photo"]
    assert int(destination.stat().st_mtime) == SENT_AT, "upload retention counts from when it was sent"
    assert not instance.photo_source.exists()
    # Both turns that recorded the photo, and the gateway's record, name the moved file.
    assert instance.turn_paths("task_1")["att_photo"] == str(destination)
    assert instance.turn_paths("task_2")["att_photo"] == str(destination)
    assert instance.gateway().get_attachment("att_photo").local_path == str(destination)
    # The web chat file was already where agents open files: untouched.
    assert instance.files()["att_web"] == str(instance.layout.uploads_dir.resolve() / "web_ann" / "att_web.png")
    # The lead's next request in the conversation lists the photo and opens it by id.
    request = Task(task_id="task_next", task_type=MessageMetaType.REQUEST, problem_statement="What was in the photo?")
    held = SimpleNamespace(task=request, meta=SimpleNamespace(conversation_id=CONVERSATION), owner_person_id=PERSON)
    scope = TaskAttachments.load(held, instance.conversations())
    (photo,), unknown = scope.find(["att_photo"])
    assert unknown == []
    located = locate_upload(photo, UploadFolder.for_instance(instance.root), task_id="task_next", conversation_id=CONVERSATION)
    assert isinstance(located, LocatedUpload) and located.read_bytes(max_bytes=100) == CONTENT["att_photo"]
    assert "att_photo" in inventory_text(scope, exclude_ids=[])
    # One summary line, a warning because two files could not be moved, and the marker with the counts.
    (summary,) = _events(caplog, "adapter_host_files_upgraded")
    assert summary.endswith(
        "moved=1 already_moved=0 missing=1 refused=0 no_record=1 gateway_rows_removed=1 turns_marked_unrecorded=1 "
        "files_without_records=0"
    )
    assert [record.levelno for record in caplog.records if "adapter_host_files_upgraded" in record.getMessage()] == [logging.WARNING]
    marker = json.loads(instance.layout.adapter_host_files_upgrade_path.read_text())
    assert (marker["moved"], marker["missing"], marker["no_record"]) == (1, 1, 1) and marker["finished_at_utc"]
    assert marker["files_without_records"] == 0 and not _events(caplog, "adapter_host_files_without_records")


def test_once_finished_it_does_not_run_again(instance: _Instance) -> None:
    from docker.launch_headless import build_adapter_host_files_task

    config_path = instance.root / "instance_config.yaml"
    env = {"POSTGRES_DSN": "from the gateway's environment"}
    name, command, step_env = build_adapter_host_files_task(instance.config(), config_path, env)
    assert name == "upgrade step for adapter-host files"
    assert command[-4:] == ["upgrade", "adapter-host-files", "--config", str(config_path)]
    assert step_env is env

    assert instance.run().finished
    _write(instance.transfer / "adapter_upload_late", b"written after the step")

    assert build_adapter_host_files_task(instance.config(), config_path, env) is None
    again = instance.run()
    assert again.already_done and again.counts == upgrade_module.UpgradeCounts()


# --- Files it cannot move ------------------------------------------------------------------------


def test_a_file_proven_absent_leaves_its_turns_and_the_model_is_told_to_ask_for_it(instance: _Instance, caplog) -> None:
    assert (instance.recorded("task_1"), instance.recorded("task_2")) == (1, 1)

    with caplog.at_level(logging.WARNING, logger=MODULE_LOGGER):
        instance.run()

    # The lost file is gone from a folder that opened: its records are dropped. The orphan (no gateway record, so
    # no chat session for its folder) is not moved, but its file is here, so its entry is kept (rule 3).
    assert set(instance.files()) == {"att_photo", "att_web", "att_orphan"}
    assert (instance.recorded("task_1"), instance.recorded("task_2")) == (0, 1)
    assert instance.conversations().last_turn_without_file_records(CONVERSATION) == 1
    assert instance.gateway().get_attachment("att_lost") is None
    assert instance.turn_paths("task_2")["att_orphan"] == str(instance.transfer / "adapter_upload_orphan")
    assert (instance.transfer / "adapter_upload_orphan").read_bytes() == CONTENT["att_orphan"], "the file is left alone"
    request = Task(task_id="task_next", task_type=MessageMetaType.REQUEST, problem_statement="And the PDF?")
    held = SimpleNamespace(task=request, meta=SimpleNamespace(conversation_id=CONVERSATION), owner_person_id=PERSON)
    scope = TaskAttachments.load(held, instance.conversations())
    (orphan,), unknown = scope.find(["att_lost", "att_orphan"])
    assert unknown == ["att_lost"]
    assert UNRECORDED_TURNS_NOTE.format(turn=1) in inventory_text(scope, exclude_ids=[])
    _assert_not_opened(instance, orphan)
    records = sorted(message.rsplit("records=", 1)[1] for message in _events(caplog, "adapter_host_file_not_moved"))
    assert records == ["dropped reason=missing", "kept reason=no_gateway_record"]
    (no_record,) = [message for message in _events(caplog, "adapter_host_file_not_moved") if "att_orphan" in message]
    assert "trying again does not help" in no_record and "restart" not in no_record, "F6: no retry advice"
    assert all(f"conversation_ids={CONVERSATION}" in message for message in _events(caplog, "adapter_host_file_not_moved"))


def test_a_conversation_entry_with_no_gateway_record_is_dropped_once_its_file_is_proven_absent(instance: _Instance, caplog) -> None:
    (instance.transfer / "adapter_upload_orphan").unlink()

    with caplog.at_level(logging.WARNING, logger=MODULE_LOGGER):
        result = instance.run()

    assert result.finished and result.counts.no_record == 1
    assert "att_orphan" not in instance.files() and instance.recorded("task_2") == 0
    assert any(
        "attachment_id=att_orphan" in message and message.endswith("records=dropped reason=no_gateway_record")
        for message in _events(caplog, "adapter_host_file_not_moved")
    )


def _assert_not_opened(instance: _Instance, attachment: UserTaskAttachment) -> None:
    """A kept record outside state/uploads is refused by the file tools, so the file is not opened."""

    located = locate_upload(attachment, UploadFolder.for_instance(instance.root), task_id="task_next", conversation_id=CONVERSATION)
    assert not isinstance(located, LocatedUpload)


def _assert_photo_kept(instance: _Instance) -> None:
    """The photo's records still name its file in the transfer folder, and the file is there."""

    _assert_photo_records_kept(instance)
    assert instance.photo_source.read_bytes() == CONTENT["att_photo"]


def _assert_photo_records_kept(instance: _Instance) -> None:
    """The photo's gateway record and both conversation entries still name its file in the transfer folder."""

    source = str(instance.photo_source)
    assert instance.gateway().get_attachment("att_photo").local_path == source
    assert instance.turn_paths("task_1")["att_photo"] == source and instance.turn_paths("task_2")["att_photo"] == source


def _link_source(instance: _Instance) -> None:
    elsewhere = _write(instance.root / "elsewhere" / "photo", CONTENT["att_photo"])
    instance.photo_source.unlink()
    instance.photo_source.symlink_to(elsewhere)


def _other_bytes(instance: _Instance) -> None:
    instance.photo_source.write_bytes(b"not the bytes the gateway recorded")


def _destination_taken(instance: _Instance) -> None:
    _write(instance.photo_destination, b"another file under the same name")


def _forged_id(instance: _Instance, attachment_id: str = "../escape") -> None:
    """A record whose id is not a file name, with a gateway record and a turn entry naming the photo's file."""

    source = str(instance.photo_source)
    record = instance.gateway().get_attachment("att_photo")
    gateway = instance.gateway()
    gateway.delete_attachment("att_photo")
    gateway.upsert_attachment(
        attachment_id=attachment_id,
        session_key=record.session_key,
        conversation_id=CONVERSATION,
        correlation_id=None,
        filename=record.filename,
        content_type=record.content_type,
        kind=record.kind,
        size_bytes=record.size_bytes,
        sha256=record.sha256,
        local_path=source,
        owner_canonical_user_id=PERSON,
    )
    conversations = instance.repository()
    conversations.relocate_turn_attachment([(CONVERSATION, "task_1"), (CONVERSATION, "task_2")], "att_photo", local_path=None)
    conversations.merge_turn_attachments(
        conversation_id=CONVERSATION,
        task_id="task_1",
        attachments=[_record(attachment_id, source, record.sha256)],
    )


def _record(attachment_id: str, path: str, sha256: str) -> UserTaskAttachment:
    return UserTaskAttachment(
        attachment_id=attachment_id,
        filename="photo.png",
        content_type="image/png",
        kind="image",
        size_bytes=len(CONTENT["att_photo"]),
        sha256=sha256,
        storage_refs=[AttachmentStorageRef(storage_type="local", path=path)],
    )


@pytest.mark.parametrize(
    ("change", "attachment_id", "reason"),
    [
        (_link_source, "att_photo", "link"),
        (_other_bytes, "att_photo", "sha256_mismatch"),
        (_destination_taken, "att_photo", "destination_taken"),
        (_forged_id, "../escape", "unsafe_attachment_id"),
    ],
    ids=["link", "sha256", "destination", "id"],
)
def test_a_file_it_refuses_keeps_its_records_and_is_not_opened(instance: _Instance, caplog, change, attachment_id, reason) -> None:
    change(instance)
    before = {path: path.read_bytes() for path in instance.root.rglob("*") if path.is_file() and not path.is_symlink() and "uploads" in path.parts}

    with caplog.at_level(logging.WARNING, logger=MODULE_LOGGER):
        result = instance.run()

    assert result.finished and result.counts.refused == 1 and result.counts.moved == 0
    assert result.counts.files_without_records == 0, "a file not moved is reported by its own warning"
    assert instance.files()[attachment_id] == str(instance.photo_source), "its conversation entry is kept"
    assert instance.gateway().get_attachment(attachment_id).local_path == str(instance.photo_source)
    (entry,) = [item for item in instance.conversations().list_turn_attachments(CONVERSATION) if item.attachment.attachment_id == attachment_id][:1]
    _assert_not_opened(instance, entry.attachment)
    (warning,) = [
        message
        for message in _events(caplog, "adapter_host_file_not_moved")
        if f"attachment_id={attachment_id} conversation_ids={CONVERSATION} " in message
    ]
    assert warning.endswith(f"records=kept reason={reason}")
    # C05: the delete advice keeps the files in sources=, so the transfer file is named there,
    # also when file= names the state/uploads copy that stood in the way.
    assert f" sources={instance.photo_source} folder=" in warning
    if reason == "destination_taken":
        assert f" file={instance.photo_destination} " in warning, "file= names the state/uploads side here"
    assert os.path.lexists(instance.photo_source), "a refused file is left where it is"
    after = {path: path.read_bytes() for path in instance.root.rglob("*") if path.is_file() and not path.is_symlink() and "uploads" in path.parts}
    assert after == before, "nothing in state/uploads was written or replaced"


@pytest.mark.parametrize(
    "named", ["other_file_here", "other_file_absent", "same_path_other_sha256"]
)
def test_a_conversation_entry_naming_another_path_does_not_follow_the_moved_file(instance: _Instance, caplog, named) -> None:
    """Agent processes write conversation turns, so an entry is moved only when it names the gateway's file.

    The same path with another sha256 is not the gateway's file either (EVI-2, U5). It is dropped only when the
    file it names is proven absent (rule 3); the moved photo's path is, once it moved.
    """

    other_there = named == "other_file_here"
    if other_there:
        _write(instance.transfer / "adapter_upload_other", b"some other upload")
    record = instance.gateway().get_attachment("att_photo")
    if named == "same_path_other_sha256":
        entry = _record("att_photo", str(instance.photo_source), hashlib.sha256(b"something else").hexdigest())
    else:
        entry = _record("att_photo", str(instance.transfer / "adapter_upload_other"), record.sha256)
    conversations = instance.conversations()
    conversations.append_turn(
        conversation_id=CONVERSATION,
        task_id="task_3",
        followup_to_task_id="task_2",
        owner_agent_id="lead",
        requester_name="Ann",
        requester_id=PERSON,
        correlation_id=None,
        problem_text="Look again",
        owner_canonical_user_id=PERSON,
        attachments=[entry],
    )

    with caplog.at_level(logging.WARNING, logger=MODULE_LOGGER):
        result = instance.run()

    assert result.counts.moved == 1
    if other_there:
        assert instance.turn_paths("task_3")["att_photo"] == str(instance.transfer / "adapter_upload_other")
        assert instance.recorded("task_3") == 1
    else:
        assert "att_photo" not in instance.turn_paths("task_3") and instance.recorded("task_3") == 0
    assert instance.turn_paths("task_1")["att_photo"] == str(instance.photo_destination)
    records = "kept" if other_there else "dropped"
    (warning,) = [message for message in _events(caplog, "adapter_host_file_not_moved") if "reason=record_mismatch" in message]
    assert warning.endswith(f"records={records} reason=record_mismatch")
    if other_there:
        assert "trying again does not help" in warning and "restart" not in warning, "F6: no retry advice"


def test_records_past_one_batch_are_all_read(instance: _Instance, monkeypatch) -> None:
    """EVI-2 (U1, U2): with a batch of one, both the gateway records and the turns must be read past the first."""

    monkeypatch.setattr(upgrade_module, "_BATCH", 1)

    result = instance.run()

    assert result.finished
    assert (result.counts.moved, result.counts.missing, result.counts.no_record) == (1, 1, 1)
    assert instance.turn_paths("task_1")["att_photo"] == str(instance.photo_destination), "the first turn"
    assert instance.turn_paths("task_2")["att_photo"] == str(instance.photo_destination), "a turn past the first batch"
    assert instance.gateway().get_attachment("att_photo").local_path == str(instance.photo_destination)
    assert instance.gateway().get_attachment("att_lost") is None, "a gateway record past the first batch"


@pytest.mark.parametrize("folder", [(), ("telegram_chat_7",)], ids=["state_uploads", "session_folder_inside"])
def test_a_transfer_folder_set_to_state_uploads_moves_and_drops_nothing(instance: _Instance, caplog, folder) -> None:
    """EVI-2 (U7): a file already inside state/uploads is where agents open it, even named as 0.9.13 named them.

    Nothing there is counted as left either (C01): a recorded file, and a file no record names, are both where
    agents open files, so the step gives no advice to delete them.
    """

    transfer = instance.layout.uploads_dir.resolve().joinpath(*folder)
    inside = transfer / "adapter_upload_inside"
    _write(inside, b"already where agents open it")
    _write(transfer / "adapter_upload_unnamed", b"no record names it")
    _record_named(instance, "att_inside", str(inside))
    config = instance.config()
    config.interactive_apps = SimpleNamespace(adapter_host=AdapterHostSettings(upload_dir=str(transfer)))
    before = _untouched(instance)

    with caplog.at_level(logging.INFO, logger=MODULE_LOGGER):
        result = upgrade_adapter_host_files(config)

    assert result.finished and result.waiting == 0
    assert result.counts == upgrade_module.UpgradeCounts(files_without_records=0)
    assert inside.read_bytes() == b"already where agents open it"
    assert instance.gateway().get_attachment("att_inside").local_path == str(inside)
    assert _untouched(instance) == before
    assert not _events(caplog, "adapter_host_files_without_records")
    _assert_no_list(instance.layout)


# --- Files left that no record names (F16) -------------------------------------------------------

STRAY = b"a file of a person removed on 0.9"


def _hex_name(label: str) -> str:
    """A transfer-file name of the gateway's own form (``new_id("adapter_upload")``): the prefix and 12 lowercase hex digits."""

    return "adapter_upload_" + hashlib.sha256(label.encode()).hexdigest()[:12]


STRAY_NAME = _hex_name("stray")


def _assert_listed(layout: InstanceLayout, names: list[str]) -> None:
    """The list beside the marker holds exactly ``names``, sorted, one per line (F6 of PR #150's review)."""

    path = layout.adapter_host_files_without_records_path
    assert path.read_bytes() == "".join(f"{name}\n" for name in sorted(names)).encode(), "exactly the counted files"
    assert not path.with_name(f".{path.name}.writing").exists()


def _assert_no_list(layout: InstanceLayout) -> None:
    path = layout.adapter_host_files_without_records_path
    assert not path.exists() and not path.with_name(f".{path.name}.writing").exists()


def _plant_earlier_list(layout: InstanceLayout) -> Path:
    """A list a run left: an earlier one's without its marker, or (a test writing the marker) another run's own."""

    path = layout.adapter_host_files_without_records_path
    _write(path, f"{_hex_name('earlier')}\n".encode())
    return path


def _list_id_entry(layout: InstanceLayout) -> str:
    """The marker's line naming its run's list id (F1 of PR #154's review), as the delete command looks for it."""

    marker = layout.adapter_host_files_upgrade_path
    list_id = json.loads(marker.read_text())[LIST_ID_FIELD]
    assert re.fullmatch("[0-9a-f]{12}", list_id), "a new random id per list"
    entry = f'"{LIST_ID_FIELD}": "{list_id}"'
    assert any(entry in line for line in marker.read_text().splitlines()), "on one line of the marker, as grep reads it"
    return entry


def _command_in(warning: str) -> str:
    """The delete command the warning gives."""

    return re.search(r"may delete exactly the listed files \((.+?)\) only while", warning).group(1)


def _paste(command: str, cwd: Path) -> subprocess.CompletedProcess[str]:
    """Run a warning's command as an administrator pastes it: ``sh -c``, from another folder."""

    return subprocess.run(["sh", "-c", command], cwd=cwd, capture_output=True, text=True, timeout=30)


def _refuse_removal(monkeypatch: pytest.MonkeyPatch, path: Path, *, times: int | None) -> list[str]:
    """Make removing ``path`` fail with EACCES ``times`` times (None: always); returns the attempts."""

    attempts: list[str] = []
    real_unlink = os.unlink

    def refusing(target, *args, **kwargs):
        if Path(os.fsdecode(target)) == path:
            attempts.append("unlink")
            if times is None or len(attempts) <= times:
                raise OSError(errno.EACCES, os.strerror(errno.EACCES))
        return real_unlink(target, *args, **kwargs)

    monkeypatch.setattr(os, "unlink", refusing)
    return attempts


def _opened_names(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """The last path component of every ``os.open`` from here on: what the step opened."""

    opened: list[str] = []
    real = os.open

    def recording(path, *args, **kwargs):
        opened.append(os.path.basename(os.fsdecode(path)))
        return real(path, *args, **kwargs)

    monkeypatch.setattr(os, "open", recording)
    return opened


def _assert_reported(caplog: pytest.LogCaptureFixture, instance: _Instance, listed: list[str], *, unlisted: int = 0) -> str:
    """The warning names the listed files and gives the delete advice of F6 of PR #150's review; returns it."""

    (warning,) = _events(caplog, "adapter_host_files_without_records")
    marker = instance.layout.adapter_host_files_upgrade_path
    leftover_list = instance.layout.adapter_host_files_without_records_path
    fields = f"folder={instance.transfer} files_without_records={len(listed) + unlisted} unlisted={unlisted}"
    assert warning.endswith(f"{fields} list={leftover_list}")
    shown = ", ".join(sorted(listed)[:10]) + (f" and {len(listed) - 10} more" if len(listed) > 10 else "")
    assert f"Listed in {leftover_list} ({len(listed)} file(s), one name per line): {shown}. If this folder" in warning
    # C04, C06 and C07: every path quoted, and nothing deleted, said aloud, unless the marker holds the id of
    # this list (F1 of PR #154's review).
    quoted = [shlex.quote(str(text)) for text in (_list_id_entry(instance.layout), marker, instance.transfer, leftover_list)]
    command = "if grep -qF -- {} {}; then cd {} && xargs rm -f -- < {}; else echo {} >&2; false; fi".format(
        *quoted, shlex.quote(DELETE_REFUSED)
    )
    assert f"Otherwise an administrator may delete exactly the listed files ({command}) only while the instance is stopped" in warning
    assert "files of people removed on 0.9" in warning, "C08: the causes are those of the gateway's names"
    assert f"only after the gateway has started on 0.10 since the step's marker {marker} was written" in warning, "C03"
    assert "after such a run, restart the instance once before deleting" in warning, "C03: a list beside the team"
    assert (
        "Before stopping the instance to delete, let the team run a few minutes after the gateway's start, so that "
        "messages a remote adapter host is still sending arrive." in warning
    ), "GEN-1 of PR #154's review"
    assert "(the operator's command, with or without --files-gone)" in warning, "C08: not only --files-gone"
    assert "! -newer" not in warning, "the list replaces the time limit"
    assert "never opens" not in warning, "0.10 reads the files of messages it had not handled"
    assert "another path under 0.9.13" in warning and "do not delete them" in warning, "C02"
    # C10 of F6: the one case where the list is wrong by design is said before the command that deletes on one paste.
    assert warning.index("do not delete them") < warning.index("may delete exactly"), "C10"
    # Putting the folder back alone changes nothing: the step reads only upload_dir (F1 of PR #150's review).
    assert "set upload_dir to that path, delete the marker and restart" in warning
    # C08: the list already leaves out what a record keeps, so the administrator takes nothing out of it.
    assert "The list names no file from the sources= field of an adapter_host_file_not_moved warning, and those must be kept" in warning
    assert STRAY.decode() not in warning, "no contents"
    return warning


def test_files_no_record_names_are_counted_and_reported_once(instance: _Instance, monkeypatch, caplog) -> None:
    """A1, A3, A5: only regular ``adapter_upload_*`` files directly in the folder, never opened, reported once.

    The list beside the marker holds exactly those (F6 of PR #150's review), and a re-run leaves it as it is.
    """

    stray = _write(instance.transfer / STRAY_NAME, STRAY)
    for other in (".moving-abc", "notes.txt", "sub/adapter_upload_nested"):
        _write(instance.transfer / other, b"not 0.9.13's")
    _write(instance.photo_destination.parent / ".moving-xyz", b"the step's temporary name")
    opened = _opened_names(monkeypatch)
    os.chmod(stray, 0)  # EVA-3: reading its content by any means would fail and leave the count unset

    with caplog.at_level(logging.INFO, logger=MODULE_LOGGER):
        result = instance.run()
    monkeypatch.undo()
    os.chmod(stray, 0o644)

    # The moved photo is gone from the folder; the orphan is kept and named by its turn.
    assert result.finished and result.counts.files_without_records == 1
    assert STRAY_NAME not in opened, "the count opens no file"
    assert stray.read_bytes() == STRAY, "nothing is deleted"
    _assert_reported(caplog, instance, [STRAY_NAME])
    (summary,) = _events(caplog, "adapter_host_files_upgraded")
    assert summary.endswith("files_without_records=1")
    assert json.loads(instance.layout.adapter_host_files_upgrade_path.read_text())["files_without_records"] == 1
    # The moved photo, the orphan its turn names, the temporary names, other names and the nested file are left out.
    _assert_listed(instance.layout, [STRAY_NAME])
    listed = instance.layout.adapter_host_files_without_records_path
    before = (listed.read_bytes(), listed.stat().st_ino)

    caplog.clear()
    with caplog.at_level(logging.INFO, logger=MODULE_LOGGER):
        again = instance.run()

    assert again.already_done and again.counts.files_without_records is None
    assert not _events(caplog, "adapter_host_files_without_records")
    assert (listed.read_bytes(), listed.stat().st_ino) == before, "a run after the marker leaves the list as it is"


def test_files_whose_records_were_deleted_while_the_step_waited_are_counted(instance: _Instance, caplog) -> None:
    """A5: the folder was missing at one start; meanwhile a file's record went (a person's removal on 0.9).

    The file is listed by the run that finishes (A4 of F6); the waiting run wrote no list.
    """

    deleted = _write(instance.transfer / _hex_name("deleted"), b"x")
    _record_named(instance, "att_deleted", str(deleted))
    moved_aside = instance.root / "unmounted"
    instance.transfer.rename(moved_aside)
    assert instance.run().folder_missing
    _assert_no_list(instance.layout)
    assert instance.gateway().delete_attachment("att_deleted")
    moved_aside.rename(instance.transfer)

    with caplog.at_level(logging.WARNING, logger=MODULE_LOGGER):
        result = instance.run()

    assert result.finished and result.counts.moved == 1 and result.counts.files_without_records == 1
    assert deleted.read_bytes() == b"x", "left where it is"
    _assert_reported(caplog, instance, [deleted.name])
    _assert_listed(instance.layout, [deleted.name])


def test_a_database_error_reading_the_adapter_hosts_records_leaves_the_count_unset(
    instance: _Instance, monkeypatch, caplog
) -> None:
    """A2: the count only reports, so a database error leaves it unset and the step still finishes (C07)."""

    _write(instance.transfer / STRAY_NAME, STRAY)
    _plant_earlier_list(instance.layout)

    def locked(*_args: Any, **_kwargs: Any) -> Any:
        raise RuntimeError("database is locked")

    monkeypatch.setattr(upgrade_module, "create_adapter_host_repository", locked)
    with caplog.at_level(logging.INFO, logger=MODULE_LOGGER):
        result = instance.run()

    assert result.finished and result.counts.moved == 1 and result.counts.files_without_records is None
    assert json.loads(instance.layout.adapter_host_files_upgrade_path.read_text())["files_without_records"] is None
    (uncounted,) = _events(caplog, "adapter_host_files_without_records_uncounted")
    assert uncounted.endswith(f"folder={instance.transfer} reason=RuntimeError: database is locked")
    assert not _events(caplog, "adapter_host_files_without_records")
    _assert_no_list(instance.layout)


def test_the_adapter_hosts_records_are_not_opened_when_no_file_is_left_to_check(instance: _Instance) -> None:
    """Opening the adapter host's records creates its tables, so it waits for a file no record names (C07)."""

    if instance.gateway_settings.backend == "postgres":
        query = "SELECT count(*) FROM information_schema.tables WHERE table_name = 'gateway_adapter_inbox' AND table_schema = current_schema()"
    else:
        query = "SELECT count(*) FROM sqlite_master WHERE type = 'table' AND name = 'gateway_adapter_inbox'"

    result = instance.run()

    assert result.finished and result.counts.files_without_records == 0
    _assert_no_list(instance.layout)
    with instance.gateway_connection() as conn:
        assert conn.execute(query).fetchone()[0] == 0, "the adapter host's tables are not created"


def test_a_link_in_the_transfer_folder_is_not_followed_or_counted(instance: _Instance, monkeypatch, tmp_path) -> None:
    """A2: links, and folders, named as 0.9.13 named files are neither followed nor counted."""

    outside_file = _write(tmp_path / "outside" / "secret", b"outside the folder")
    (instance.transfer / "adapter_upload_link").symlink_to(outside_file)
    (instance.transfer / "adapter_upload_folder_link").symlink_to(outside_file.parent, target_is_directory=True)
    _write(instance.transfer / "adapter_upload_dir" / "inside", b"in a folder")
    _write(instance.transfer / STRAY_NAME, STRAY)
    opened = _opened_names(monkeypatch)

    result = instance.run()
    monkeypatch.undo()

    assert result.finished and result.counts.files_without_records == 1
    assert not {"adapter_upload_link", "adapter_upload_folder_link", "adapter_upload_dir", "secret"} & set(opened)
    assert outside_file.read_bytes() == b"outside the folder"
    assert (instance.transfer / "adapter_upload_link").is_symlink()
    _assert_listed(instance.layout, [STRAY_NAME])


def test_a_run_stopped_part_way_reports_at_the_run_that_finishes(instance: _Instance, monkeypatch, caplog) -> None:
    """A3: a run that stops never counts or lists; the run that finishes reports and lists, and no later one does."""

    _write(instance.transfer / STRAY_NAME, STRAY)
    monkeypatch.setattr(
        _RelationalGatewayStateRepository, "set_attachment_local_path", lambda *_args: (_ for _ in ()).throw(_Stop())
    )
    reports: list[int] = []
    lists: list[bool] = []
    for run in range(3):
        caplog.clear()
        with caplog.at_level(logging.WARNING, logger=MODULE_LOGGER):
            if run == 0:
                with pytest.raises(_Stop):
                    instance.run()
                monkeypatch.undo()
            else:
                instance.run()
        reports.append(len(_events(caplog, "adapter_host_files_without_records")))
        lists.append(instance.layout.adapter_host_files_without_records_path.exists())

    assert reports == [0, 1, 0] and lists == [False, True, True]
    _assert_listed(instance.layout, [STRAY_NAME])
    assert json.loads(instance.layout.adapter_host_files_upgrade_path.read_text())["files_without_records"] == 1


def test_a_file_only_a_gateway_record_or_held_guidance_names_is_not_counted(instance: _Instance, caplog) -> None:
    """F3 of PR #150's review (EVA-1): each kind of record keeps its file out of the count on its own.

    A file only a gateway record names is refused (another sha256) and its
    not-moved warning names it in ``sources=``; a file only held guidance
    names is never moved and its own warning names it.
    """

    gateway_only = _write(instance.transfer / "adapter_upload_gateway_only", b"not the bytes its record names")
    _record_named(instance, "att_gateway_only", str(gateway_only))
    held_bytes = b"a file only held guidance names"
    held_only = _write(instance.transfer / "adapter_upload_held_only", held_bytes)
    _hold(instance, "corr_held_only", _record("att_held_only", str(held_only), hashlib.sha256(held_bytes).hexdigest()))

    with caplog.at_level(logging.WARNING, logger=MODULE_LOGGER):
        result = instance.run()

    assert result.finished and result.counts.files_without_records == 0
    assert not _events(caplog, "adapter_host_files_without_records")
    _assert_no_list(instance.layout)
    (warning,) = [m for m in _events(caplog, "adapter_host_file_not_moved") if "attachment_id=att_gateway_only" in m]
    assert f" sources={gateway_only} folder=" in warning and warning.endswith("records=kept reason=sha256_mismatch")
    (held,) = _events(caplog, "adapter_host_files_held_only")
    assert f"file={held_only}" in held
    assert gateway_only.is_file() and held_only.read_bytes() == held_bytes


# Counted names that are not of the gateway's form: never listed (C02 of F6). Each would pass a looser rule.
_OTHER_NAMES = {
    "space": "adapter_upload_my notes",
    # With re.match and "$" it would add a line xargs reads as the gateway's name before the newline.
    "newline": _hex_name("newline") + "\n",
    "uppercase": "adapter_upload_ABCDEF012345",
    "bare_prefix": "adapter_upload_",
    "not_utf8": os.fsdecode(_hex_name("not_utf8").encode() + b"\xff"),
    # SEC-1 of PR #154's review: the gateway's ids are exactly 12 hex digits; a longer run may encode chosen text.
    "11_hex_digits": "adapter_upload_" + "a" * 11,
    "13_hex_digits": "adapter_upload_" + "b" * 13,
}


@pytest.mark.parametrize("with_gateway_names", [True, False], ids=["beside_gateway_names", "only_other_names"])
def test_only_names_of_the_gateways_form_are_listed(instance: _Instance, caplog, with_gateway_names) -> None:
    """A1 of F6 (C02): a name the gateway did not give may be text someone chose, so it is counted, never shown.

    The gateway names each file ``adapter_upload_`` and 12 lowercase hex
    digits, matched in full. The warning names only those and says how many others
    it leaves out and why. Only other names: no list is written and the
    warning gives no delete advice.
    """

    others = []
    for name in _OTHER_NAMES.values():
        try:
            others.append(_write(instance.transfer / name, b"not a name the gateway gives"))
        except OSError as exc:  # a file system that only takes UTF-8 names
            pytest.skip(f"the file system refuses the name {name!r}: {exc}")
    gateway_names = [STRAY_NAME, _hex_name("another")] if with_gateway_names else []
    for name in gateway_names:
        _write(instance.transfer / name, STRAY)

    with caplog.at_level(logging.WARNING, logger=MODULE_LOGGER):
        result = instance.run()

    assert result.finished and result.counts.files_without_records == len(_OTHER_NAMES) + len(gateway_names)
    assert instance.layout.adapter_host_files_upgrade_path.is_file()
    if gateway_names:
        _assert_listed(instance.layout, gateway_names)
        # _assert_reported pins the names shown to exactly the gateway's ones.
        warning = _assert_reported(caplog, instance, gateway_names, unlisted=len(_OTHER_NAMES))
    else:
        _assert_no_list(instance.layout)
        (warning,) = _events(caplog, "adapter_host_files_without_records")
        assert warning.endswith(f"files_without_records={len(_OTHER_NAMES)} unlisted={len(_OTHER_NAMES)} list=-")
        assert "Listed in" not in warning and "xargs" not in warning and "may delete" not in warning
        # C08: no cause of the gateway's names, and no advice to point at.
        assert f"{len(_OTHER_NAMES)} file(s) are left in the adapter host's transfer folder that no record names. Not listed:" in warning
        assert "files of people removed" not in warning and "this advice" not in warning
    scope = "this advice does not cover them" if gateway_names else "the step gives no advice for them"
    assert f"Not listed: {len(_OTHER_NAMES)} file(s) whose names are not ones the gateway gives" in warning
    assert f"may be text someone chose; {scope}. See upgrading.md." in warning
    shown = [part for part in ("my notes", "ABCDEF", "\n", "\udcff", "a" * 11, "b" * 13) if part in warning]
    assert not shown, "no other name is shown"
    assert all(path.exists() for path in others), "nothing is deleted"


@pytest.mark.parametrize("count", [10, 11, 12])
def test_the_warning_names_the_first_files_and_says_where_the_rest_are(instance: _Instance, caplog, count) -> None:
    """A1 of F6: the log line stays bounded; the list beside the marker holds every name.

    Ten names are all shown, with no "more" (C09: the edge of the cap).
    """

    names = sorted(_hex_name(f"stray_{number}") for number in range(count))
    for name in names:
        _write(instance.transfer / name, STRAY)

    with caplog.at_level(logging.WARNING, logger=MODULE_LOGGER):
        result = instance.run()

    assert result.finished and result.counts.files_without_records == count
    warning = _assert_reported(caplog, instance, names)
    more = f" and {count - 10} more" if count > 10 else ""
    assert f"one name per line): {', '.join(names[:10])}{more}. If this folder" in warning
    assert not [name for name in names[10:] if name in warning]
    _assert_listed(instance.layout, names)


def test_the_delete_command_removes_exactly_the_listed_files_and_only_beside_the_marker(
    instance: _Instance, caplog, tmp_path
) -> None:
    """A2, C04 and C06 of F6: the warning's command, run by ``sh`` as an administrator would paste it.

    The instance root holds a space (the fixture's), so each path must be
    quoted. The files a record keeps, a name of another form, a nested file,
    a link and the orphan its turn names stay. Without the marker written
    with the list the list is not valid, and the command deletes nothing:
    no marker, or one holding another run's list id or none (F1 of PR
    #154's review).
    """

    assert " " in str(instance.root)
    listed = [_write(instance.transfer / _hex_name(label), STRAY) for label in ("one", "two")]
    kept_by_record = _write(instance.transfer / _hex_name("kept"), b"not the bytes its record names")
    _record_named(instance, "att_kept", str(kept_by_record))
    other_form = _write(instance.transfer / "adapter_upload_my notes", b"not a name the gateway gives")
    nested = _write(instance.transfer / "sub" / _hex_name("nested"), b"in a folder")
    outside = _write(tmp_path / "outside" / "target", b"outside the folder")
    link = instance.transfer / _hex_name("link")
    link.symlink_to(outside)
    orphan = instance.transfer / "adapter_upload_orphan"

    with caplog.at_level(logging.WARNING, logger=MODULE_LOGGER):
        result = instance.run()

    assert result.finished and result.counts.files_without_records == 3
    (not_moved,) = [m for m in _events(caplog, "adapter_host_file_not_moved") if "attachment_id=att_kept" in m]
    assert f" sources={kept_by_record} folder=" in not_moved
    warning = _assert_reported(caplog, instance, [path.name for path in listed], unlisted=1)
    command = _command_in(warning)
    marker = instance.layout.adapter_host_files_upgrade_path
    entry, written = _list_id_entry(instance.layout), marker.read_text()
    aside = marker.rename(marker.with_name("marker-aside"))

    refused = _paste(command, tmp_path)

    assert refused.returncode != 0 and all(path.read_bytes() == STRAY for path in listed), "C06: no marker, no delete"
    # C07: the refusal says why; grep's own line before it names the missing marker.
    assert refused.stderr.endswith(f"{DELETE_REFUSED}\n") and str(marker) in refused.stderr and not refused.stdout
    for other in (f'"{LIST_ID_FIELD}": "{"0" * 12}"', f'"{LIST_ID_FIELD}": null'):
        marker.write_text(written.replace(entry, other))
        refused = _paste(command, tmp_path)
        assert refused.returncode != 0 and refused.stderr == f"{DELETE_REFUSED}\n" and not refused.stdout, other
        assert all(path.read_bytes() == STRAY for path in listed), f"another run's marker, no delete: {other}"
    aside.replace(marker)

    done = _paste(command, tmp_path)

    assert done.returncode == 0 and not done.stderr and not done.stdout, done.stderr
    assert not any(path.exists() for path in listed), "exactly the listed files are gone"
    assert all(path.is_file() for path in (kept_by_record, other_form, nested, orphan)), "everything else stays"
    assert link.is_symlink() and outside.read_bytes() == b"outside the folder"


def test_the_run_that_writes_the_marker_replaces_or_removes_an_earlier_list(instance: _Instance) -> None:
    """F6: a list beside a marker is always that marker's run's.

    The documented re-run (delete the marker and restart) lists what is
    left now, and one with nothing to list leaves no list.
    """

    marker = instance.layout.adapter_host_files_upgrade_path
    first = _write(instance.transfer / STRAY_NAME, STRAY)
    assert instance.run().counts.files_without_records == 1
    _assert_listed(instance.layout, [first.name])

    first.unlink()
    second = _write(instance.transfer / _hex_name("second"), STRAY)
    marker.unlink()
    assert instance.run().counts.files_without_records == 1
    _assert_listed(instance.layout, [second.name])

    second.unlink()
    marker.unlink()
    assert instance.run().counts.files_without_records == 0
    _assert_no_list(instance.layout)


@pytest.mark.parametrize("stops", ["waits_for_its_folder", "folder_problem"])
def test_a_run_that_waits_or_stops_removes_an_earlier_list(instance: _Instance, monkeypatch, caplog, stops) -> None:
    """C01 of F6: a list is valid only beside the marker of the run that wrote it.

    A re-run after the marker was deleted removes the earlier list before
    it can wait for its folder or stop on a folder problem, so no list is
    left whose run's conditions may no longer hold (a folder that was at
    another path lists every file).
    """

    earlier = _plant_earlier_list(instance.layout)
    with caplog.at_level(logging.INFO, logger=MODULE_LOGGER):
        if stops == "waits_for_its_folder":
            instance.transfer.rename(instance.root / "unmounted")
            assert instance.run().folder_missing
        else:
            fired = _inject(instance, monkeypatch, _FAULTS["rename_read_only"])
            with pytest.raises(upgrade_module.FolderProblem):
                instance.run()
            assert fired == ["rename"]

    assert not instance.layout.adapter_host_files_upgrade_path.exists()
    _assert_no_list(instance.layout)
    (removed,) = _events(caplog, "adapter_host_files_without_records_list_removed")
    assert removed.endswith(f"list={earlier}") and "is not valid, so it was removed" in removed


# --- Records that name the transfer folder at another path (F7 of PR #154's review) ----------------------


def _name_photo(instance: _Instance, name: str) -> None:
    """Give the photo a name of the gateway's form, in its file, its gateway record and both turns, as 0.9.13 wrote it."""

    path = str(instance.transfer / name)
    assert instance.gateway().set_attachment_local_path("att_photo", path)
    turns = [(CONVERSATION, "task_1"), (CONVERSATION, "task_2")]
    instance.repository().relocate_turn_attachment(turns, "att_photo", local_path=path)
    instance.photo_source.rename(instance.transfer / name)


def _folder_moved_since_0_9_13(instance: _Instance) -> tuple[Any, Path, str]:
    """The reviewer's probe (DI-2 of PR #154's review): 0.9.13's records name the folder at P1, which is now at P2.

    The photo's gateway record and both turns name ``P1/<gateway name>``;
    the folder was renamed to P2 and ``upload_dir`` set to P2. Returns that
    config, P2 and the photo's name. The records of the lost file and the
    orphan name P1 too; the orphan's file, a name of another form, is in P2.
    """

    name = _hex_name("photo")
    _name_photo(instance, name)
    moved = instance.root / "moved-uploads"
    instance.transfer.rename(moved)
    config = instance.config()
    config.interactive_apps.adapter_host = AdapterHostSettings(upload_dir=str(moved))
    return config, moved, name


def _earlier_command(instance: _Instance, transfer: Path, name: str) -> str:
    """The delete command an earlier run's warning gave for a list naming ``name`` (the marker since deleted)."""

    leftover_list = instance.layout.adapter_host_files_without_records_path
    _write(leftover_list, f"{name}\n".encode())
    written = upgrade_module._WrittenList(leftover_list, "a1b2c3d4e5f6")
    return upgrade_module._delete_command(instance.layout.adapter_host_files_upgrade_path, transfer, written)


def _assert_earlier_command_refused(instance: _Instance, command: str, file: Path, cwd: Path) -> None:
    """A list the run could not remove is back with a live file in it, and the earlier command still deletes nothing."""

    _write(instance.layout.adapter_host_files_without_records_path, f"{file.name}\n".encode())
    refused = _paste(command, cwd)
    assert refused.returncode != 0 and refused.stderr == f"{DELETE_REFUSED}\n" and not refused.stdout
    assert file.is_file(), "the live file stays"


def test_the_gateway_query_finds_text_literally_in_any_folder(instance: _Instance) -> None:
    """A1: ``list_attachment_paths_containing`` reads records outside the transfer folder, matched literally and with case.

    ``%``, ``_`` and ``!`` are wildcards or escapes in ``LIKE``; here they
    match only themselves, on both databases, and pages follow the id.
    """

    other = {
        "att_q1": "/old/100%_!/adapter_upload_1",
        "att_q2": "/old/1000_!/adapter_upload_2",
        "att_q3": "/old/100X_!x/adapter_upload_3",
        "att_q4": "/old/ADAPTER_UPLOAD_4",
    }
    for attachment_id, path in other.items():
        _record_named(instance, attachment_id, path)
    gateway = instance.gateway()

    assert gateway.list_attachment_paths_containing("100%_!", limit=10) == [("att_q1", other["att_q1"])]
    pages: list[list[tuple[str, str]]] = []
    after = ""
    while not pages or len(pages[-1]) == 2:
        pages.append(gateway.list_attachment_paths_containing("/adapter_upload_", after_id=after, limit=2))
        after = pages[-1][-1][0] if pages[-1] else after

    assert [row for page in pages for row in page] == [
        ("att_lost", str(instance.transfer / "adapter_upload_lost")),
        ("att_photo", str(instance.photo_source)),
        ("att_q1", other["att_q1"]),
        ("att_q2", other["att_q2"]),
        ("att_q3", other["att_q3"]),
    ], "every folder, by id; the uppercase name and the web chat file are not matched"


def test_records_naming_the_folder_at_another_path_withhold_the_list_and_the_command(
    instance: _Instance, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture, tmp_path: Path
) -> None:
    """A1, A2, A5: the reviewer's probe. The listed photo is a live file whose records name P1, so nothing is listed.

    The run finishes once (the owner's choice for A3): the marker holds no
    list id, so an earlier warning's command refuses even beside a list; the
    summary line is logged as usual and one error replaces the
    files-without-records warning, naming P1 (from the gateway record) and
    how to still move the files. No listed file is opened, no record changes.
    """

    config, moved, name = _folder_moved_since_0_9_13(instance)
    old = instance.transfer
    marker = instance.layout.adapter_host_files_upgrade_path
    command = _earlier_command(instance, moved, name)
    opened = _opened_names(monkeypatch)

    with caplog.at_level(logging.INFO, logger=MODULE_LOGGER):
        result = upgrade_adapter_host_files(config)
    monkeypatch.undo()

    assert result.finished and result.counts.files_without_records == 2
    assert result.elsewhere == NamedElsewhere(
        names=1, by_gateway=1, by_held_guidance=0, by_turns=1, folders=(old,), gateway_folders=(old,)
    )
    assert name not in opened and (moved / name).read_bytes() == CONTENT["att_photo"]
    assert instance.gateway().get_attachment("att_photo").local_path == str(old / name)
    assert instance.turn_paths("task_1")["att_photo"] == instance.turn_paths("task_2")["att_photo"] == str(old / name)
    body = json.loads(marker.read_text())
    assert body[LIST_ID_FIELD] is None and body["files_without_records"] == 2
    assert body[NAMED_ELSEWHERE_FIELD] == 1, "C07: the finding stays on the instance after the log is gone"
    _assert_no_list(instance.layout)
    (summary,) = _events(caplog, "adapter_host_files_upgraded")
    assert summary.endswith("moved=0 already_moved=0 missing=0 refused=0 no_record=0 gateway_rows_removed=0 "
                            "turns_marked_unrecorded=0 files_without_records=2")
    assert not _events(caplog, "adapter_host_files_without_records")
    (error,) = _events(caplog, "adapter_host_files_folder_moved")
    assert [r.levelno for r in caplog.records if "event=adapter_host_files_folder_moved" in r.getMessage()] == [logging.ERROR]
    assert error.endswith(
        f"event=adapter_host_files_folder_moved folder={moved} other_folders={old} files_without_records=2 unlisted=1 "
        "named_elsewhere=1 by_gateway=1 by_sent_copies=0 by_instance_folders=0 by_held_guidance=0 by_turns=1"
    )
    assert error.startswith(
        "1 of the 2 file(s) left in the adapter host's transfer folder that no record of this folder names are named "
        f"by records in another folder ({old}), so this folder was most likely at another path under 0.9.13"
    )
    assert "No list and no delete command are given: do not delete any of them." in error
    assert "They were not moved into state/uploads, so the team cannot open them." in error, "C02"
    assert (
        f"To move the 1 the gateway has a record of receiving: move or mount this folder back at {old} (a link does "
        f"not work), set upload_dir to that path, delete the marker {marker} and restart" in error
    ), "C02: the way to still move them"
    assert "fix them, then do the same" in error, "C04: no promise that moving it back always finishes"
    assert "no record of receiving" not in error, "C05: a gateway record names the one match"
    assert name not in error and "xargs" not in error and "rm -f" not in error
    _assert_earlier_command_refused(instance, command, moved / name, tmp_path)

    assert upgrade_adapter_host_files(config).already_done, "reported once"


def test_a_database_error_checking_other_folders_gives_no_list_and_no_command(
    instance: _Instance, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture, tmp_path: Path
) -> None:
    """C03: a failing check for records in another folder must never leave the delete command. Uncounted, not stopped.

    C06: its warning says the files were counted and how to run the check again.
    """

    config, moved, name = _folder_moved_since_0_9_13(instance)
    command = _earlier_command(instance, moved, name)

    def locked(*_args: Any, **_kwargs: Any) -> Any:
        raise RuntimeError("database is locked")

    monkeypatch.setattr(_RelationalGatewayStateRepository, "list_attachment_paths_containing", locked)
    with caplog.at_level(logging.INFO, logger=MODULE_LOGGER):
        result = upgrade_adapter_host_files(config)

    assert result.finished and result.elsewhere.names == 0 and result.counts.files_without_records is None
    assert result.other_folders_unchecked == 2, "GEN-1 of PR #168's review: the operator command learns the count"
    marker = instance.layout.adapter_host_files_upgrade_path
    body = json.loads(marker.read_text())
    assert body[LIST_ID_FIELD] is None and body["files_without_records"] is None and body[NAMED_ELSEWHERE_FIELD] is None
    (uncounted,) = _events(caplog, "adapter_host_files_without_records_uncounted")
    assert uncounted.endswith(f"folder={moved} reason=other_folders_unchecked: RuntimeError: database is locked")
    assert uncounted.startswith(
        "2 file(s) are left in the adapter host's transfer folder that no record of this folder names, but the check "
        "for records naming them in another folder did not finish"
    ), "C06: they were counted; the check failed"
    assert "do not delete any of them" in uncounted and f"To check again, delete the marker {marker} and restart" in uncounted
    assert not _events(caplog, "adapter_host_files_without_records")
    assert not _events(caplog, "adapter_host_files_folder_moved")
    _assert_no_list(instance.layout)
    _assert_earlier_command_refused(instance, command, moved / name, tmp_path)


_LIVE = _hex_name("live")


@pytest.mark.parametrize(
    ("path_of", "in_this_folder"),
    [
        (lambda transfer, other: str(other / f"{_LIVE}0"), False),
        (lambda transfer, other: str(other / _LIVE[:-1]), False),
        (lambda transfer, other: str(other / (TRANSFER_FILE_PREFIX + _LIVE.removeprefix(TRANSFER_FILE_PREFIX).upper())), False),
        (lambda transfer, other: str(other / f"{_LIVE}.pdf"), False),
        (lambda transfer, other: f"{other.name}/{_LIVE}", False),
        (lambda transfer, other: f"{transfer}/../{transfer.name}/{_LIVE}", True),
        (lambda transfer, other: f"{transfer}//{_LIVE}", True),
    ],
    ids=["13_hex", "11_hex", "uppercase", "suffix", "relative", "dot_dot_into_this_folder", "double_slash_into_this_folder"],
)
def test_names_that_are_not_a_listed_name_in_another_folder_withhold_nothing(
    instance: _Instance, caplog: pytest.LogCaptureFixture, path_of: Callable[[Path, Path], str], in_this_folder: bool
) -> None:
    """C03: only a listed name exactly, in an absolute path of another folder, counts; each kind of record is tried.

    A gateway record, a turn and a held guidance copy name the path. Near
    misses of the listed name (another length, uppercase, a suffix, a
    relative path) leave the usual warning and command. A path that
    normalizes into the transfer folder is a record of this folder: its file
    moves, and the usual report covers the other file left. The near misses
    are not listed names, so this rests on exact membership in the list,
    not on the 12-hex rule in ``_elsewhere`` (EVA-2 of PR #168's review);
    ``test_only_the_gateway_s_own_names_count_in_another_folder`` tests that
    rule, where nothing is listed.
    """

    assert _LIVE.upper() != _LIVE
    live = _write(instance.transfer / _LIVE, b"x")
    _write(instance.transfer / STRAY_NAME, STRAY)
    path = path_of(instance.transfer, instance.root / "old-uploads")
    _record_named(instance, "att_named", path)
    instance.repository().merge_turn_attachments(
        conversation_id=CONVERSATION, task_id="task_1", attachments=[_record("att_named", path, hashlib.sha256(b"x").hexdigest())]
    )
    _hold(instance, "corr_named", _record("att_named", path, hashlib.sha256(b"x").hexdigest()))

    with caplog.at_level(logging.INFO, logger=MODULE_LOGGER):
        result = instance.run()

    assert result.finished and result.elsewhere.names == 0
    assert not _events(caplog, "adapter_host_files_folder_moved")
    left = [STRAY_NAME] if in_this_folder else [_LIVE, STRAY_NAME]
    _assert_reported(caplog, instance, left)
    _assert_listed(instance.layout, left)
    assert live.exists() != in_this_folder, "a record of this folder moves its file"


_SHA_X = hashlib.sha256(b"x").hexdigest()  # what ``_record_named`` records


def _run_operator_command(
    instance: _Instance, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], **args: Any
) -> str:
    """Run ``upgrade adapter-host-files`` on ``instance`` as the operator does; returns what it printed."""

    import assistant_cli

    monkeypatch.setattr(
        "assistant_cli.GatewayInstanceConfig.load_for_process", staticmethod(lambda _path: instance.config())
    )
    assistant_cli._upgrade_adapter_host_files(
        argparse.Namespace(**{"config": "instance_config.yaml", "files_gone": False, **args})
    )
    return capsys.readouterr().out


@pytest.mark.parametrize("source", ["turn", "held_guidance"])
def test_a_match_only_a_turn_or_held_guidance_names_gives_no_steps_that_cannot_move_it(
    instance: _Instance,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    capsys: pytest.CaptureFixture[str],
    source: str,
) -> None:
    """A1, A4, C05: no gateway record names the listed file in another folder, so no run can move it there either.

    The error and the operator's line give no move-back steps, only that
    the file must be kept. A turn's folder is shown nowhere, in the log or
    the operator's output (agent processes write turns); a held guidance
    copy's is named.
    """

    live = _write(instance.transfer / _LIVE, b"x")
    old = instance.root / "old-uploads"
    record = _record("att_named", str(old / _LIVE), _SHA_X)
    if source == "turn":
        instance.repository().merge_turn_attachments(conversation_id=CONVERSATION, task_id="task_1", attachments=[record])
    else:
        _hold(instance, "corr_named", record)

    with caplog.at_level(logging.DEBUG):
        printed = _run_operator_command(instance, monkeypatch, capsys)

    (error,) = _events(caplog, "adapter_host_files_folder_moved")
    by = "by_sent_copies=0 by_instance_folders=0 by_held_guidance=0 by_turns=1" if source == "turn" else "by_sent_copies=0 by_instance_folders=0 by_held_guidance=1 by_turns=0"
    shown = "-" if source == "turn" else str(old)
    assert error.endswith(
        f"folder={instance.transfer} other_folders={shown} files_without_records=1 unlisted=0 named_elsewhere=1 "
        f"by_gateway=0 {by}"
    )
    assert (
        "The gateway has no record of receiving them, so the step cannot move them at any path: keep them, and the "
        "people who sent them can send them again." in error
    )
    assert "move or mount" not in error and "delete the marker" not in error, "C05: no steps that cannot succeed"
    assert "1 file(s) are left in the transfer folder that no record of it names, and records in another folder name 1" in printed
    assert "The gateway has no record of receiving them, so they cannot be moved: keep them" in printed
    assert "move or mount" not in printed
    assert "which may be deleted" not in printed
    if source == "turn":
        assert all(old.name not in record.getMessage() for record in caplog.records) and old.name not in printed, "A4"
    body = json.loads(instance.layout.adapter_host_files_upgrade_path.read_text())
    assert body[LIST_ID_FIELD] is None and body[NAMED_ELSEWHERE_FIELD] == 1
    _assert_no_list(instance.layout)
    assert live.read_bytes() == b"x"


def test_a_match_through_a_gateway_record_on_a_later_page_gives_the_steps_for_that_file(
    instance: _Instance, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """A1, C05: gateway records outside the folder are read in pages, and only the file one names gets the move-back steps.

    With one record per page, a record of another folder naming no listed
    file is read first. A second listed file only a turn names is counted
    apart: it is kept, and its turn's folder is not shown.
    """

    monkeypatch.setattr(upgrade_module, "_BATCH", 1)
    by_gateway = _write(instance.transfer / _hex_name("by gateway"), b"x")
    by_turn = _write(instance.transfer / _hex_name("by turn"), b"x")
    old = instance.root / "old-uploads"
    _record_named(instance, "att_a1", str(old / _hex_name("not here")))
    _record_named(instance, "att_zz", str(old / by_gateway.name))
    instance.repository().merge_turn_attachments(
        conversation_id=CONVERSATION, task_id="task_1",
        attachments=[_record("att_turn", str(instance.root / "agent-folder" / by_turn.name), _SHA_X)],
    )

    with caplog.at_level(logging.INFO, logger=MODULE_LOGGER):
        result = instance.run()

    assert result.elsewhere == NamedElsewhere(
        names=2, by_gateway=1, by_held_guidance=0, by_turns=1, folders=(old,), gateway_folders=(old,)
    )
    (error,) = _events(caplog, "adapter_host_files_folder_moved")
    assert error.endswith(
        f"other_folders={old} files_without_records=2 unlisted=0 named_elsewhere=2 by_gateway=1 by_sent_copies=0 "
        "by_instance_folders=0 by_held_guidance=0 by_turns=1"
    )
    assert "To move the 1 the gateway has a record of receiving: move or mount this folder back" in error
    assert (
        "For the other 1 the gateway has no record of receiving them, so no run can move them: keep them" in error
    )
    assert "reports each" not in error, "GEN-2: a run at the gateway's path does not report a file only a turn names"
    assert "agent-folder" not in error and not _events(caplog, "adapter_host_files_without_records")
    assert json.loads(instance.layout.adapter_host_files_upgrade_path.read_text())[LIST_ID_FIELD] is None
    _assert_no_list(instance.layout)


def test_gateway_records_in_two_other_folders_give_steps_for_each_path(
    instance: _Instance, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture, capsys: pytest.CaptureFixture[str]
) -> None:
    """C08: gateway records place two listed files in two folders, so one move back moves one of them.

    The error's steps name both gateway folders, not the folder only a held
    copy names (nothing can move there), and say to repeat them; so does the
    operator's line. Following them for the first folder moves its file and
    reports the other again, with the steps for that one path.
    """

    old_a, old_b, old_c = (instance.root / f"old-{label}" for label in "abc")
    file_a = _write(instance.transfer / _hex_name("sent at a"), b"x")
    file_b = _write(instance.transfer / _hex_name("sent at b"), b"x")
    _record_named(instance, "att_a", str(old_a / file_a.name))
    _record_named(instance, "att_b", str(old_b / file_b.name))
    _hold(instance, "corr_a", _record("att_a", str(old_c / file_a.name), _SHA_X))
    marker = instance.layout.adapter_host_files_upgrade_path

    with caplog.at_level(logging.INFO, logger=MODULE_LOGGER):
        printed = _run_operator_command(instance, monkeypatch, capsys)

    (error,) = _events(caplog, "adapter_host_files_folder_moved")
    assert error.endswith(
        f"other_folders={old_a},{old_b},{old_c} files_without_records=2 unlisted=0 named_elsewhere=2 by_gateway=2 "
        "by_sent_copies=0 by_instance_folders=0 by_held_guidance=1 by_turns=0"
    )
    assert (
        f"To move the 2 the gateway has a record of receiving: move or mount this folder back at one of the 2 paths "
        f"their gateway records name ({old_a}, {old_b}) (a link does not work), set upload_dir to that path, delete "
        f"the marker {marker} and restart; the step then runs again and moves the files whose gateway records name "
        "that path and reports again the files whose gateway records name another of those paths, so do this for "
        "each of them. "
        "If this folder was just moved here from one of those paths" in error
    )
    assert "one of the 2 paths the log's steps name" in printed and "so do this for each path" in printed
    caplog.clear()

    instance.transfer.rename(old_a)
    marker.unlink()
    config = instance.config()
    config.interactive_apps.adapter_host = AdapterHostSettings(upload_dir=str(old_a))
    with caplog.at_level(logging.INFO, logger=MODULE_LOGGER):
        result = upgrade_adapter_host_files(config)

    assert result.finished and result.counts.moved == 1 and not (old_a / file_a.name).exists()
    assert result.elsewhere == NamedElsewhere(names=1, by_gateway=1, folders=(old_b,), gateway_folders=(old_b,))
    (error,) = _events(caplog, "adapter_host_files_folder_moved")
    assert f"other_folders={old_b} " in error and (old_a / file_b.name).read_bytes() == b"x"
    assert f"move or mount this folder back at {old_b} (a link does not work)" in error
    assert "one of the" not in error and "for each of them" not in error


def test_a_record_in_another_folder_whose_name_is_not_listed_keeps_the_list_and_is_reported_on_its_own(
    instance: _Instance, caplog: pytest.LogCaptureFixture
) -> None:
    """A7: its file is not here (still in its own folder, deleted or moved earlier), so it says nothing about this folder.

    The listed file is named by no record anywhere, so the list and the
    command stay as they are. The record's own file was not moved either,
    so the count of files recorded in another folder reports it in its own
    error (C12 of PR #168's fix round), which names no listed file.
    """

    _write(instance.transfer / STRAY_NAME, STRAY)
    old = instance.root / "old-uploads"
    _record_named(instance, "att_named", str(old / _hex_name("somewhere else")))

    with caplog.at_level(logging.INFO, logger=MODULE_LOGGER):
        result = instance.run()

    assert result.elsewhere.names == 0
    assert result.recorded_elsewhere == NamedElsewhere(names=1, by_gateway=1, folders=(old,), gateway_folders=(old,))
    _assert_reported(caplog, instance, [STRAY_NAME])
    _assert_listed(instance.layout, [STRAY_NAME])
    body = json.loads(instance.layout.adapter_host_files_upgrade_path.read_text())
    assert body[NAMED_ELSEWHERE_FIELD] == 0 and body[RECORDED_ELSEWHERE_FIELD] == 1
    (error,) = _events(caplog, "adapter_host_files_folder_moved")
    assert error.startswith("Gateway records or held guidance name 1 file(s)") and STRAY_NAME not in error


@pytest.mark.parametrize("refused_at_p2", [False, True], ids=["moved_at_p2", "refused_at_p2"])
def test_records_of_both_paths_move_what_each_path_can_and_the_error_s_steps_finish_it(
    instance: _Instance, caplog: pytest.LogCaptureFixture, refused_at_p2: bool
) -> None:
    """C01, C04 and A3's finish path: records name the folder at P1 and, for a newer file, at P2, where it is now.

    At P2 the newer file moves, which the summary line counts, or is
    refused (another sha256), and the photo its records place at P1 gives
    the error. The error's steps (the folder back at P1, upload_dir there,
    the marker deleted, a restart) move the photo. A file refused at P2
    keeps records naming P2, so that run names P2 for it instead of
    finishing quietly.
    """

    config, moved, name = _folder_moved_since_0_9_13(instance)
    newer = _write(moved / _hex_name("sent at P2"), b"not what was recorded" if refused_at_p2 else b"x")
    _record_named(instance, "att_p2", str(newer))

    with caplog.at_level(logging.INFO, logger=MODULE_LOGGER):
        first = upgrade_adapter_host_files(config)

    assert (first.counts.moved, first.counts.refused) == ((0, 1) if refused_at_p2 else (1, 0))
    (summary,) = _events(caplog, "adapter_host_files_upgraded")
    assert f"moved={first.counts.moved} already_moved=0 missing=0 refused={first.counts.refused} " in summary, "C01"
    (error,) = _events(caplog, "adapter_host_files_folder_moved")
    assert f"other_folders={instance.transfer} " in error and first.elsewhere.names == 1
    caplog.clear()

    moved.rename(instance.transfer)
    instance.layout.adapter_host_files_upgrade_path.unlink()
    with caplog.at_level(logging.INFO, logger=MODULE_LOGGER):
        second = instance.run()

    assert second.finished and second.counts.moved == 1
    assert instance.gateway().get_attachment("att_photo").local_path == str(instance.photo_destination)
    assert instance.photo_destination.read_bytes() == CONTENT["att_photo"] and not (instance.transfer / name).exists()
    if refused_at_p2:
        (error,) = _events(caplog, "adapter_host_files_folder_moved")
        assert f"other_folders={moved} " in error and "by_gateway=1" in error
        assert (instance.transfer / newer.name).is_file() and second.elsewhere.names == 1
    else:
        assert not _events(caplog, "adapter_host_files_folder_moved") and second.elsewhere.names == 0
        assert json.loads(instance.layout.adapter_host_files_upgrade_path.read_text())[NAMED_ELSEWHERE_FIELD] == 0


def test_files_gone_as_the_first_run_to_find_the_match_reports_it_the_same_way(
    instance: _Instance, caplog: pytest.LogCaptureFixture
) -> None:
    """C02: a P2-era record whose file is gone makes the normal run wait, so ``--files-gone`` is the first to see the match.

    It drops only that record; the records naming P1 stay as they are, and
    its error carries the steps, since the administrator ran it for
    another reason.
    """

    config, moved, name = _folder_moved_since_0_9_13(instance)
    _record_named(instance, "att_p2", str(moved / _hex_name("gone at P2")))
    with caplog.at_level(logging.INFO, logger=MODULE_LOGGER):
        waiting = upgrade_adapter_host_files(config)
    assert not waiting.finished and waiting.waiting == 1 and not _events(caplog, "adapter_host_files_folder_moved")
    caplog.clear()

    with caplog.at_level(logging.INFO, logger=MODULE_LOGGER):
        result = upgrade_adapter_host_files(config, files_gone=True)

    assert result.finished and result.counts.gateway_rows_removed == 1 and result.elsewhere.names == 1
    (error,) = _events(caplog, "adapter_host_files_folder_moved")
    assert f"other_folders={instance.transfer} " in error
    assert (
        f"To move the 1 the gateway has a record of receiving: move or mount this folder back at {instance.transfer} "
        "(a link" in error
    )
    assert instance.gateway().get_attachment("att_photo").local_path == str(instance.transfer / name)
    assert instance.turn_paths("task_1")["att_photo"] == str(instance.transfer / name)
    assert (moved / name).read_bytes() == CONTENT["att_photo"]
    body = json.loads(instance.layout.adapter_host_files_upgrade_path.read_text())
    assert body[LIST_ID_FIELD] is None and body[NAMED_ELSEWHERE_FIELD] == 1
    _assert_no_list(instance.layout)

def test_one_matched_listed_name_withholds_the_unmatched_listed_name_too(
    instance: _Instance, caplog: pytest.LogCaptureFixture, tmp_path: Path
) -> None:
    """A2, EVA-1 of PR #168's review: a record in another folder names one listed file; the other listed file no record names.

    The other file is just as likely a live one whose records were cleaned
    up, so neither is listed: no list, no list id, and an earlier warning's
    command naming the unmatched file refuses. Dropping only the matched
    name would leave a list that looks safe.
    """

    matched = _write(instance.transfer / _hex_name("matched"), b"x")
    unmatched = _write(instance.transfer / _hex_name("unmatched"), b"y")
    _record_named(instance, "att_matched", str(instance.root / "old-uploads" / matched.name))
    command = _earlier_command(instance, instance.transfer, unmatched.name)

    with caplog.at_level(logging.INFO, logger=MODULE_LOGGER):
        result = instance.run()

    assert result.counts.files_without_records == 2 and result.elsewhere.names == 1
    body = json.loads(instance.layout.adapter_host_files_upgrade_path.read_text())
    assert body[LIST_ID_FIELD] is None and body[NAMED_ELSEWHERE_FIELD] == 1
    _assert_no_list(instance.layout)
    (error,) = _events(caplog, "adapter_host_files_folder_moved")
    assert error.startswith("1 of the 2 file(s) left") and unmatched.name not in error
    assert not _events(caplog, "adapter_host_files_without_records")
    _assert_earlier_command_refused(instance, command, unmatched, tmp_path)
    assert matched.read_bytes() == b"x" and unmatched.read_bytes() == b"y"


def _sent_copy(instance: _Instance, attachment_id: str, path: Path, data: bytes) -> Path:
    """A copy of a file the gateway sent to a person, staged and recorded as ``assistant_gateway.attachments`` does."""

    _write(path, data)
    instance.gateway().upsert_attachment(
        attachment_id=attachment_id,
        session_key="web:chat:usr_ann",
        conversation_id=CONVERSATION,
        correlation_id=None,
        filename=path.name,
        content_type="image/png",
        kind="image",
        size_bytes=len(data),
        sha256=hashlib.sha256(data).hexdigest(),
        local_path=str(path),
        owner_canonical_user_id=PERSON,
    )
    return path


_SENT_FOLDER = ("users", "usr_ann", "scratch", "sent", "outgoing_abc")


@pytest.mark.parametrize(
    ("attachment_id", "folder_of", "kind"),
    [
        ("web_attachment_1", lambda instance: instance.layout.workspaces_dir.joinpath(*_SENT_FOLDER), "sent_copy"),
        ("att_copy", lambda instance: instance.root.parent.joinpath("old instance", "workspaces", *_SENT_FOLDER), "sent_copy"),
        ("web_attachment_2", lambda instance: instance.root / "old-outgoing", "sent_copy"),
        ("att_team", lambda instance: instance.layout.workspaces_dir / "team" / "files", "instance_folder"),
        ("att_state", lambda instance: instance.layout.state_dir / "kept", "instance_folder"),
        (None, lambda instance: instance.layout.workspaces_dir / "users" / "usr_ann" / "uploads", "held_copy"),
        # A listed name a record places inside state/uploads still withholds the list (C13 of the fix round leaves
        # such records out only of the count of files recorded in another folder).
        ("att_uploads", lambda instance: instance.layout.uploads_dir / "adapter", "instance_folder"),
        (None, lambda instance: instance.layout.uploads_dir / "adapter", "held_copy"),
    ],
    ids=[
        "sent_copy", "sent_copy_under_an_old_root", "sent_file_id", "under_workspaces", "under_state",
        "held_copy_under_workspaces", "under_state_uploads", "held_copy_under_state_uploads",
    ],
)
def test_a_record_in_a_folder_the_step_never_names_withholds_the_command_and_gives_no_steps(
    instance: _Instance,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    capsys: pytest.CaptureFixture[str],
    attachment_id: str | None,
    folder_of: Callable[[_Instance], Path],
    kind: str,
) -> None:
    """SEC-1 of PR #168's review: a copy of a file sent to a person names a listed file; its folder is never offered.

    An agent chose the copy's name, so the copy can carry a leftover's
    generated name. It is recognized by a ``scratch/sent`` folder anywhere
    in its path (also under an earlier instance root) or by its
    ``web_attachment_`` id; as a backstop no folder under this instance's
    workspaces/ or state/ is offered either (a held guidance copy there
    too). The record withholds the list and the command like any match and
    its folder is named nowhere: putting the transfer folder there would
    hand every person's files to agents working for one person. C11 of the
    fix round: the text stays true for each kind. A sent copy is no record
    of receiving, so no run moves its file and it does not show the folder
    moved (only that the file is one a person sent); a record of receiving
    inside this instance's folders is counted apart and pointed at
    upgrading.md, never told it cannot move.
    """

    leftover = _write(instance.transfer / _hex_name("cleaned up"), b"a person's file whose records were cleaned up")
    folder = folder_of(instance)
    if attachment_id is None:
        _hold(instance, "corr_copy", _record("att_held", str(folder / leftover.name), _SHA_X))
    else:
        _sent_copy(instance, attachment_id, folder / leftover.name, b"agent copy")

    with caplog.at_level(logging.DEBUG):
        printed = _run_operator_command(instance, monkeypatch, capsys)

    by = {
        "sent_copy": "by_sent_copies=1 by_instance_folders=0 by_held_guidance=0",
        "instance_folder": "by_sent_copies=0 by_instance_folders=1 by_held_guidance=0",
        "held_copy": "by_sent_copies=0 by_instance_folders=0 by_held_guidance=1",
    }[kind]
    (error,) = _events(caplog, "adapter_host_files_folder_moved")
    assert error.endswith(f"other_folders=- files_without_records=1 unlisted=0 named_elsewhere=1 by_gateway=0 {by} by_turns=0")
    assert "(not shown: the step shows no folder from a conversation record" in error
    assert "move or mount" not in error and "set upload_dir" not in error, "no steps toward the copy's folder"
    assert "move or mount" not in printed
    if kind == "sent_copy":
        assert "names have copies that the gateway sent to people (not shown:" in error
        assert "), so they are files people sent. No list" in error
        assert "most likely at another path" not in error and "most likely at another path" not in printed, "C11 (b)"
        assert "and copies of 1 of them were sent to people, so they are files people sent." in printed
    else:
        assert "are named by records in another folder (not shown:" in error
        assert "most likely at another path under 0.9.13" in error and "most likely at another path" in printed
    if kind == "instance_folder":
        assert (
            "1 of them have gateway records of receiving only inside this instance's workspaces or state folder, which "
            "the step never shows or offers: keep them; if the transfer folder really was there under 0.9.13, "
            "upgrading.md says how to move them." in error
        ), "C11 (a): such a file can move, by the steps upgrading.md gives"
        assert "no record of receiving" not in error and "no record of receiving" not in printed
        assert "which the upgrade does not offer: keep them; see upgrading.md" in printed
    else:
        assert "The gateway has no record of receiving them, so the step cannot move them at any path: keep them" in error
        assert "The gateway has no record of receiving them, so they cannot be moved: keep them" in printed
    assert all(str(folder) not in record.getMessage() for record in caplog.records) and str(folder) not in printed
    body = json.loads(instance.layout.adapter_host_files_upgrade_path.read_text())
    assert body[LIST_ID_FIELD] is None and body[NAMED_ELSEWHERE_FIELD] == 1
    _assert_no_list(instance.layout)
    assert leftover.is_file()


def test_a_record_naming_this_instance_s_folders_through_the_configured_link_is_never_offered(
    instance: _Instance, caplog: pytest.LogCaptureFixture, tmp_path: Path
) -> None:
    """C10 of PR #168's fix round: the configured root is a link to the real root, and a record names its folders through it.

    The layout resolves the root, so the backstop also compares the root as
    configured, normalized without following a link. A record of receiving
    under the link's ``workspaces/`` is counted apart and never shown, like
    one under the resolved path.
    """

    alias = tmp_path / "alias root"
    alias.symlink_to(instance.root, target_is_directory=True)
    leftover = _write(instance.transfer / _hex_name("through the link"), b"a person's file")
    through_link = alias / "workspaces" / "team" / "files"
    _record_named(instance, "att_link", str(through_link / leftover.name))
    config = instance.config()
    config.instance_root = str(alias)
    assert config.layout.root == instance.root, "the layout is the resolved root"

    with caplog.at_level(logging.DEBUG):
        result = upgrade_adapter_host_files(config)

    assert result.elsewhere == NamedElsewhere(names=1, by_instance_folders=1)
    (error,) = _events(caplog, "adapter_host_files_folder_moved")
    assert " other_folders=- " in error and "move or mount" not in error
    assert all(str(through_link) not in record.getMessage() for record in caplog.records)
    _assert_no_list(instance.layout)


def test_a_transfer_folder_that_was_inside_state_is_never_offered_and_upgrading_md_s_steps_move_its_files(
    instance: _Instance, caplog: pytest.LogCaptureFixture
) -> None:
    """C11 (a) of PR #168's fix round, the critic's probe: 0.9.13's upload_dir was state/adapter-uploads, now the default.

    The photo's gateway record and turns name the old folder inside state/,
    which the step never offers. The error must not say the gateway has no
    record of receiving it; it points at upgrading.md, whose steps (put the
    folder back there, set upload_dir to it, delete the marker, restart)
    move the photo.
    """

    name = _hex_name("photo")
    old = instance.layout.state_dir / "adapter-uploads"
    assert instance.gateway().set_attachment_local_path("att_photo", str(old / name))
    instance.repository().relocate_turn_attachment(
        [(CONVERSATION, "task_1"), (CONVERSATION, "task_2")], "att_photo", local_path=str(old / name)
    )
    instance.photo_source.rename(instance.transfer / name)

    with caplog.at_level(logging.INFO, logger=MODULE_LOGGER):
        first = instance.run()

    assert first.elsewhere == NamedElsewhere(names=1, by_instance_folders=1, by_turns=1)
    (error,) = _events(caplog, "adapter_host_files_folder_moved")
    assert "most likely at another path under 0.9.13" in error and "upgrading.md says how to move them" in error
    assert "no record of receiving" not in error and str(old) not in error
    caplog.clear()

    instance.transfer.rename(old)
    config = instance.config()
    config.interactive_apps.adapter_host = AdapterHostSettings(upload_dir=str(old))
    instance.layout.adapter_host_files_upgrade_path.unlink()
    with caplog.at_level(logging.INFO, logger=MODULE_LOGGER):
        second = upgrade_adapter_host_files(config)

    assert second.finished and second.counts.moved == 1 and second.elsewhere.names == 0
    assert instance.gateway().get_attachment("att_photo").local_path == str(instance.photo_destination)
    assert instance.photo_destination.read_bytes() == CONTENT["att_photo"]
    assert not _events(caplog, "adapter_host_files_folder_moved")


def test_a_copy_sent_back_unchanged_is_never_offered_and_the_steps_move_the_file_under_its_own_record(
    instance: _Instance, caplog: pytest.LogCaptureFixture
) -> None:
    """CRIT-1 of PR #168's review, the critic's probe: the folder moved from P1, and an agent once sent the photo back unchanged.

    The copy's gateway record has the photo's name and sha256 in a
    person's scratch/sent folder. The error's steps name only P1. Following
    them moves the photo under its own record, leaves the copy and its
    record as they were and puts no other file in the scratch folder; the
    other person's file, which no record anywhere names, gets the usual
    warning at P1.
    """

    config, moved, name = _folder_moved_since_0_9_13(instance)
    old = instance.transfer
    other = _write(moved / _hex_name("someone else's file"), b"another person's file, records cleaned up")
    copy = _sent_copy(
        instance, "web_attachment_1", instance.layout.workspaces_dir.joinpath(*_SENT_FOLDER, name), CONTENT["att_photo"]
    )

    with caplog.at_level(logging.INFO, logger=MODULE_LOGGER):
        first = upgrade_adapter_host_files(config)

    assert first.elsewhere == NamedElsewhere(
        names=1, by_gateway=1, by_sent_copies=1, by_turns=1, folders=(old,), gateway_folders=(old,)
    )
    (error,) = _events(caplog, "adapter_host_files_folder_moved")
    assert f"other_folders={old} " in error and f"move or mount this folder back at {old} (a link does not work)" in error
    assert str(copy.parent) not in error and "scratch" not in error and "for each" not in error
    caplog.clear()

    moved.rename(old)
    instance.layout.adapter_host_files_upgrade_path.unlink()
    with caplog.at_level(logging.INFO, logger=MODULE_LOGGER):
        second = instance.run()

    assert second.finished and second.counts.moved == 1 and second.elsewhere.names == 0
    gateway = instance.gateway()
    assert gateway.get_attachment("att_photo").local_path == str(instance.photo_destination)
    assert instance.photo_destination.read_bytes() == CONTENT["att_photo"]
    assert gateway.get_attachment("web_attachment_1").local_path == str(copy)
    assert sorted(path.name for path in copy.parent.iterdir()) == [name] and copy.read_bytes() == CONTENT["att_photo"]
    assert not _events(caplog, "adapter_host_files_folder_moved")
    _assert_reported(caplog, instance, [other.name])
    assert (old / other.name).is_file()


# --- Records of receiving in another folder while nothing is listed here (DI-1 of PR #168's review) --------


def test_upload_dir_naming_a_folder_without_the_files_reports_where_the_records_are_and_the_steps_move_them(
    instance: _Instance, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture, capsys: pytest.CaptureFixture[str]
) -> None:
    """DI-1, the reviewer's probe: 0.9.13's records name P1, which still holds the photo; the 0.10 upload_dir names a new folder.

    Nothing is listed, so before this the run finished silently. Now it
    logs one error with the folder the gateway record names and the steps,
    keeps the count in the marker's own field (not ``named_elsewhere``,
    which counts listed names), and the operator command says so. Following
    the steps moves the photo.
    """

    name = _hex_name("photo")
    _name_photo(instance, name)
    p1 = instance.transfer
    marker = instance.layout.adapter_host_files_upgrade_path
    config = instance.config()
    config.interactive_apps.adapter_host = AdapterHostSettings(upload_dir=str(instance.root / "brand-new-uploads"))
    monkeypatch.setattr("assistant_cli.GatewayInstanceConfig.load_for_process", staticmethod(lambda _path: config))
    import assistant_cli

    with caplog.at_level(logging.INFO, logger=MODULE_LOGGER):
        assistant_cli._upgrade_adapter_host_files(argparse.Namespace(config="instance_config.yaml", files_gone=False))
    printed = capsys.readouterr().out

    body = json.loads(marker.read_text())
    assert body["files_without_records"] == 0 and body[NAMED_ELSEWHERE_FIELD] == 0
    assert body[RECORDED_ELSEWHERE_FIELD] == 1 and body[LIST_ID_FIELD] is None
    (error,) = _events(caplog, "adapter_host_files_folder_moved")
    assert [r.levelno for r in caplog.records if "event=adapter_host_files_folder_moved" in r.getMessage()] == [logging.ERROR]
    assert error.startswith(
        f"Gateway records or held guidance name 1 file(s) of chat apps the adapter host ran in another folder ({p1}), "
        "and none of them is left in the transfer folder upload_dir names, so the transfer folder was most likely at "
        "another path when 0.9 received them"
    )
    assert (
        f"To move the 1 the gateway has a record of receiving: with the folder that holds them at {p1} (move or mount "
        f"it there if it is not; a link does not work), set upload_dir to that path, delete the marker {marker} and "
        "restart" in error
    )
    assert "If those files are gone, nothing more is needed." in error
    assert error.endswith(
        f"folder={instance.root / 'brand-new-uploads'} other_folders={p1} files_without_records=0 unlisted=0 "
        "recorded_elsewhere=1 by_gateway=1 by_instance_folders=0 by_held_guidance=0"
    )
    assert name not in error
    assert printed.splitlines()[-1].startswith(
        "Gateway records or held guidance name 1 file(s) of the adapter host's chat apps in another folder"
    )
    assert "with the folder that holds them at the path the log's steps name" in printed
    assert "If those files are gone, nothing more is needed." in printed and str(p1) not in printed
    caplog.clear()

    marker.unlink()
    config.interactive_apps.adapter_host = AdapterHostSettings(upload_dir=str(p1))
    with caplog.at_level(logging.INFO, logger=MODULE_LOGGER):
        second = upgrade_adapter_host_files(config)

    assert second.finished and second.counts.moved == 1 and second.recorded_elsewhere.names == 0
    assert instance.gateway().get_attachment("att_photo").local_path == str(instance.photo_destination)
    assert json.loads(marker.read_text())[RECORDED_ELSEWHERE_FIELD] == 0
    assert not _events(caplog, "adapter_host_files_folder_moved")


def test_a_normal_instance_gets_no_error_for_records_in_another_folder(instance: _Instance, caplog: pytest.LogCaptureFixture) -> None:
    """DI-1: no false error. The photo, named as 0.9.13 named it, moves; its records then name state/uploads under its id.

    Beside it, an agent sent a copy back under the photo's generated name
    (a sent copy is never counted) and a conversation turn names a file of
    the gateway's form in another folder (turns are not read for this:
    agents write them).
    """

    name = _hex_name("photo")
    _name_photo(instance, name)
    _sent_copy(instance, "web_attachment_1", instance.layout.workspaces_dir.joinpath(*_SENT_FOLDER, name), CONTENT["att_photo"])
    instance.repository().merge_turn_attachments(
        conversation_id=CONVERSATION, task_id="task_1",
        attachments=[_record("att_turn", str(instance.root / "agent-folder" / _hex_name("turn")), _SHA_X)],
    )

    with caplog.at_level(logging.INFO, logger=MODULE_LOGGER):
        result = instance.run()

    assert result.finished and result.counts.moved == 1 and result.counts.files_without_records == 0
    assert result.recorded_elsewhere == NamedElsewhere()
    assert json.loads(instance.layout.adapter_host_files_upgrade_path.read_text())[RECORDED_ELSEWHERE_FIELD] == 0
    assert not _events(caplog, "adapter_host_files_folder_moved")
    assert not [record for record in caplog.records if record.levelno >= logging.ERROR]


@pytest.mark.parametrize(
    ("kind", "expected"),
    [
        ("held_copy", NamedElsewhere(names=1, by_held_guidance=1)),
        ("instance_folder", NamedElsewhere(names=1, by_instance_folders=1)),
        ("sent_copy", NamedElsewhere()),
    ],
)
def test_with_nothing_listed_each_kind_of_record_counts_by_its_rule(
    instance: _Instance, caplog: pytest.LogCaptureFixture, capsys: pytest.CaptureFixture[str], kind: str, expected: NamedElsewhere
) -> None:
    """DI-1 with SEC-1's rules: a held copy's folder is shown, a record of receiving in this instance's folders is counted and not shown, a sent copy is not counted.

    The operator's line starts as the log's error does, naming both kinds
    of record: a held copy alone is no record of receiving (C14 of PR
    #168's fix round).
    """

    old = instance.root / "old-uploads"
    name = _hex_name(kind)
    if kind == "held_copy":
        _hold(instance, "corr_named", _record("att_held", str(old / name), _SHA_X))
        expected = replace(expected, folders=(old,))
    elif kind == "instance_folder":
        _record_named(instance, "att_team", str(instance.layout.workspaces_dir / "team" / "files" / name))
    else:
        _sent_copy(instance, "web_attachment_1", old / name, b"agent copy")

    with caplog.at_level(logging.INFO, logger=MODULE_LOGGER):
        result = instance.run()

    assert result.counts.files_without_records == 0 and result.recorded_elsewhere == expected
    errors = _events(caplog, "adapter_host_files_folder_moved")
    assert len(errors) == (0 if kind == "sent_copy" else 1)
    if kind == "held_copy":
        assert f" other_folders={old} " in errors[0]
        assert "The gateway has no record of receiving them, so the step cannot move them at any path" in errors[0]
    elif kind == "instance_folder":
        assert " other_folders=- " in errors[0] and "workspaces/team" not in errors[0]
        assert "upgrading.md says how to move them" in errors[0]
    if errors:
        import assistant_cli

        assistant_cli._print_folder_moved(result.recorded_elsewhere)
        printed = capsys.readouterr().out
        assert printed.startswith(
            "Gateway records or held guidance name 1 file(s) of the adapter host's chat apps in another folder"
        ), "C14: as the log's error starts"
        assert "Records of receiving" not in printed
        if kind == "held_copy":
            assert "The gateway has no record of receiving them, so they cannot be moved" in printed


@pytest.mark.parametrize(
    ("file_name", "counted"),
    [
        (_LIVE, True),
        (f"{_LIVE}0", False),
        (_LIVE[:-1], False),
        (TRANSFER_FILE_PREFIX + _LIVE.removeprefix(TRANSFER_FILE_PREFIX).upper(), False),
        (f"{_LIVE}.pdf", False),
    ],
    ids=["12_hex", "13_hex", "11_hex", "uppercase", "suffix"],
)
def test_only_the_gateway_s_own_names_count_in_another_folder(
    instance: _Instance, caplog: pytest.LogCaptureFixture, file_name: str, counted: bool
) -> None:
    """EVA-2 of PR #168's review: with nothing listed, the 12-hex rule in ``_elsewhere`` alone decides what counts.

    The tests with listed names rest on exact membership in the list; here
    any name of the gateway's form counts, so a near miss (another length,
    uppercase, a suffix) counts only if the full match were dropped.
    """

    _record_named(instance, "att_named", str(instance.root / "old-uploads" / file_name))

    with caplog.at_level(logging.INFO, logger=MODULE_LOGGER):
        result = instance.run()

    assert result.counts.files_without_records == 0
    assert result.recorded_elsewhere.names == int(counted)
    assert len(_events(caplog, "adapter_host_files_folder_moved")) == int(counted)


def test_a_database_error_checking_other_folders_with_nothing_listed_keeps_the_count_and_says_how_to_check_again(
    instance: _Instance, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture, capsys: pytest.CaptureFixture[str]
) -> None:
    """DI-1 with GEN-1 and FA-4: the files were counted (none), the check did not finish, and the operator line says that."""

    def locked(*_args: Any, **_kwargs: Any) -> Any:
        raise RuntimeError("database is locked")

    monkeypatch.setattr(_RelationalGatewayStateRepository, "list_attachment_paths_containing", locked)
    with caplog.at_level(logging.INFO, logger=MODULE_LOGGER):
        printed = _run_operator_command(instance, monkeypatch, capsys)

    body = json.loads(instance.layout.adapter_host_files_upgrade_path.read_text())
    assert body["files_without_records"] == 0 and body[RECORDED_ELSEWHERE_FIELD] is None
    (unchecked,) = _events(caplog, "adapter_host_files_other_folders_unchecked")
    assert unchecked.endswith(
        f"folder={instance.transfer} reason=other_folders_unchecked: RuntimeError: database is locked"
    )
    assert f"To check again, delete the marker {instance.layout.adapter_host_files_upgrade_path} and restart" in unchecked
    assert not _events(caplog, "adapter_host_files_without_records_uncounted")
    assert "could not be counted" not in printed
    assert "The check for records naming files of the adapter host's chat apps in another folder did not finish" in printed
    assert "To check again, delete the marker and restart" in printed


def test_a_listed_file_beside_files_recorded_in_another_folder_gives_both_reports_and_the_steps_move_them(
    instance: _Instance, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture, capsys: pytest.CaptureFixture[str]
) -> None:
    """C12 of PR #168's fix round, the critic's probe: 0.9's records name P1, which still holds the photo; upload_dir names P2, which holds one file no record names.

    For example ``upload_dir`` was changed during 0.9 and P2 kept a
    leftover of a removed person. That file gets the usual warning, its
    list and its command; the photo, whose name is not listed, gets the
    error with the steps, through the operator command too. Following the
    steps moves the photo and opens or deletes no listed file.
    """

    name = _hex_name("photo")
    _name_photo(instance, name)
    p1 = instance.transfer
    p2 = instance.root / "newer-uploads"
    stray = _write(p2 / STRAY_NAME, STRAY)
    marker = instance.layout.adapter_host_files_upgrade_path
    leftover_list = instance.layout.adapter_host_files_without_records_path
    config = instance.config()
    config.interactive_apps.adapter_host = AdapterHostSettings(upload_dir=str(p2))
    monkeypatch.setattr("assistant_cli.GatewayInstanceConfig.load_for_process", staticmethod(lambda _path: config))
    import assistant_cli

    with caplog.at_level(logging.INFO, logger=MODULE_LOGGER):
        assistant_cli._upgrade_adapter_host_files(argparse.Namespace(config="instance_config.yaml", files_gone=False))
    printed = capsys.readouterr().out

    body = json.loads(marker.read_text())
    assert body["files_without_records"] == 1 and body[NAMED_ELSEWHERE_FIELD] == 0
    assert body[RECORDED_ELSEWHERE_FIELD] == 1 and body[LIST_ID_FIELD] is not None
    _assert_listed(instance.layout, [STRAY_NAME])
    (warning,) = _events(caplog, "adapter_host_files_without_records")
    assert warning.endswith(f"folder={p2} files_without_records=1 unlisted=0 list={leftover_list}")
    assert "may delete exactly the listed files (if grep -qF" in warning, "the stray's command is given"
    (error,) = _events(caplog, "adapter_host_files_folder_moved")
    assert error.startswith(
        f"Gateway records or held guidance name 1 file(s) of chat apps the adapter host ran in another folder ({p1}), "
    )
    assert f"with the folder that holds them at {p1} (move or mount it there if it is not" in error
    assert error.endswith(
        f"folder={p2} other_folders={p1} files_without_records=1 unlisted=0 "
        "recorded_elsewhere=1 by_gateway=1 by_instance_folders=0 by_held_guidance=0"
    )
    assert name not in error and STRAY_NAME not in error
    assert "1 file(s) are left in the transfer folder that no record names" in printed
    assert printed.splitlines()[-1].startswith("Gateway records or held guidance name 1 file(s)")
    caplog.clear()

    marker.unlink()
    config.interactive_apps.adapter_host = AdapterHostSettings(upload_dir=str(p1))
    with caplog.at_level(logging.INFO, logger=MODULE_LOGGER):
        second = upgrade_adapter_host_files(config)

    assert second.finished and second.counts.moved == 1 and second.recorded_elsewhere == NamedElsewhere()
    assert instance.gateway().get_attachment("att_photo").local_path == str(instance.photo_destination)
    assert instance.photo_destination.read_bytes() == CONTENT["att_photo"]
    assert stray.read_bytes() == STRAY
    assert not _events(caplog, "adapter_host_files_folder_moved")


def test_a_database_error_in_the_later_count_keeps_the_list_and_says_how_to_check_again(
    instance: _Instance, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture, capsys: pytest.CaptureFixture[str]
) -> None:
    """C12 of PR #168's fix round: the listed names were checked, so a failing count of other files recorded elsewhere keeps the list.

    The usual warning gives its list and command, the marker keeps the
    count with ``recorded_elsewhere`` unset, the count's own event says how
    to check again, and the operator command prints both lines.
    """

    _write(instance.transfer / STRAY_NAME, STRAY)
    real = _RelationalGatewayStateRepository.list_attachment_paths_containing
    calls: list[str] = []

    def later_scan_locked(self: Any, *args: Any, **kwargs: Any) -> Any:
        calls.append("scan")
        if len(calls) > 1:
            raise RuntimeError("database is locked")
        return real(self, *args, **kwargs)

    monkeypatch.setattr(_RelationalGatewayStateRepository, "list_attachment_paths_containing", later_scan_locked)
    with caplog.at_level(logging.INFO, logger=MODULE_LOGGER):
        printed = _run_operator_command(instance, monkeypatch, capsys)

    assert calls == ["scan", "scan"], "the listed names' check first, one page; then the count"
    body = json.loads(instance.layout.adapter_host_files_upgrade_path.read_text())
    assert body["files_without_records"] == 1 and body[NAMED_ELSEWHERE_FIELD] == 0
    assert body[RECORDED_ELSEWHERE_FIELD] is None
    _assert_reported(caplog, instance, [STRAY_NAME])
    _assert_listed(instance.layout, [STRAY_NAME])
    (unchecked,) = _events(caplog, "adapter_host_files_other_folders_unchecked")
    assert unchecked.endswith(
        f"folder={instance.transfer} reason=other_folders_unchecked: RuntimeError: database is locked"
    )
    assert not _events(caplog, "adapter_host_files_without_records_uncounted")
    assert not _events(caplog, "adapter_host_files_folder_moved")
    assert "1 file(s) are left in the transfer folder that no record names" in printed
    assert "The check for records naming files of the adapter host's chat apps in another folder did not finish" in printed
    assert "could not be counted" not in printed and "Nothing is listed" not in printed


@pytest.mark.parametrize("kind", ["gateway_record", "held_copy", "gateway_record_through_link"])
def test_records_of_files_already_inside_state_uploads_give_no_error(
    instance: _Instance, caplog: pytest.LogCaptureFixture, tmp_path: Path, kind: str
) -> None:
    """C13 of PR #168's fix round, the critic's probe: 0.9 kept the transfer folder at state/uploads/adapter, now upload_dir is the default.

    The file is where agents open it, so nothing needs moving and the count
    of files recorded in another folder leaves it out, also when the record
    names state/uploads through the configured root's link (C10).
    """

    name = _hex_name(kind)
    config = instance.config()
    kept = instance.layout.uploads_dir / "adapter"
    _write(kept / name, b"a person's file, where agents open it")
    if kind == "gateway_record_through_link":
        alias = tmp_path / "alias root"
        alias.symlink_to(instance.root, target_is_directory=True)
        config.instance_root = str(alias)
        _record_named(instance, "att_kept", str(alias / "state" / "uploads" / "adapter" / name))
    elif kind == "held_copy":
        _hold(instance, "corr_kept", _record("att_kept", str(kept / name), _SHA_X))
    else:
        _record_named(instance, "att_kept", str(kept / name))

    with caplog.at_level(logging.INFO, logger=MODULE_LOGGER):
        result = upgrade_adapter_host_files(config)

    assert result.finished and result.counts.files_without_records == 0
    assert result.recorded_elsewhere == NamedElsewhere()
    assert json.loads(instance.layout.adapter_host_files_upgrade_path.read_text())[RECORDED_ELSEWHERE_FIELD] == 0
    assert not _events(caplog, "adapter_host_files_folder_moved")
    assert (kept / name).is_file()


_EPOCH = "epoch_1"
_RECEIVED = datetime(2026, 11, 1, tzinfo=timezone.utc)


def _adapter_upload(instance: _Instance, name: str, *, epoch: str = _EPOCH, status: str = "completed") -> AdapterAttachmentV1:
    """A file a hosted chat sent into the transfer folder, with the adapter host's upload record (as 0.9.13 kept it)."""

    data = f"bytes of {name}".encode()
    path = _write(instance.transfer / _hex_name(name), data)
    upload = AdapterUploadRecord(
        upload_id=path.name, upload_request_id=f"upload_{name}", idempotency_key=f"upload_idem_{name}",
        authenticated_host_id="host", plugin_id="telegram", adapter_instance_id="telegram_1", registration_epoch=epoch,
        filename=f"{name}.png", content_type="image/png", kind="image", size_bytes=len(data),
        sha256=hashlib.sha256(data).hexdigest(), storage_reference=f"adapter-upload:{path.name}", local_path=str(path),
        status=status, expires_at_utc="2026-11-08T00:00:00+00:00", created_at_utc="2026-11-01T00:00:00+00:00",
        completed_at_utc=None if status == "pending" else "2026-11-01T00:00:01+00:00",
    )
    create_adapter_host_repository(instance.gateway_settings).save_upload(upload)
    return AdapterAttachmentV1(
        attachment_id=f"att_{name}", filename=upload.filename, content_type=upload.content_type, kind=upload.kind,
        size_bytes=upload.size_bytes, sha256=upload.sha256, storage_reference=upload.storage_reference,
    )


def _admitted(instance: _Instance, position: int, files: list[AdapterAttachmentV1], *, status: str = "accepted") -> None:
    """A hosted-chat message the gateway took in and stored as ``AdapterHostApplicationService.accept_request`` does."""

    message = ChannelInboundMessageV1(
        protocol_version=ADAPTER_HOST_PROTOCOL_VERSION, message_type="channel.inbound.v1",
        request_id=f"request_{position}", idempotency_key=f"inbound_idem_{position}", plugin_id="telegram",
        adapter_instance_id="telegram_1", session_epoch=_EPOCH, stream_position=position, channel_name="telegram",
        external_user_id="7", chat_id="7", text="Here", attachments=tuple(files), received_at_utc=_RECEIVED,
    )
    at = (_RECEIVED + timedelta(seconds=position)).isoformat()
    record = asdict(AdapterInboxRecord(
        authenticated_host_id="host", plugin_id="telegram", adapter_instance_id="telegram_1", registration_epoch=_EPOCH,
        idempotency_key=message.idempotency_key, request_id=message.request_id, stream_position=position,
        message_type=message.message_type,
        payload_json=json.dumps(encode_adapter_host_message_v1(message), sort_keys=True),
        correlation_id=f"corr_{position}", admission_json="{}", status=status, result_json=None, error_code=None,
        detail=None, created_at_utc=at, updated_at_utc=at,
    ))
    bind = "%s" if instance.gateway_settings.backend == "postgres" else "?"
    with instance.gateway_connection() as conn:
        conn.execute(
            f"INSERT INTO gateway_adapter_inbox ({', '.join(record)}) VALUES ({', '.join(bind for _ in record)})",
            tuple(record.values()),
        )
        conn.commit()


@pytest.mark.parametrize("batch", [None, 1], ids=["one_page", "a_page_per_message"])
def test_files_the_gateway_reads_at_its_start_are_not_counted(instance: _Instance, monkeypatch, batch) -> None:
    """F2 of PR #150's review: a file of a message still ``accepted`` is one 0.10 reads, then removes.

    Left out only as the gateway resolves it (``resolve_upload_reference``):
    a completed upload of the message's own registration epoch, up to the
    first reference that does not resolve. A message in ``processing``, an
    upload no message names, and the references after one that does not
    resolve are never read again, so their files are counted.
    """

    if batch is not None:
        monkeypatch.setattr(upgrade_module, "_BATCH", batch)
    _admitted(instance, 1, [_adapter_upload(instance, "read_1")])
    _admitted(instance, 2, [_adapter_upload(instance, "read_2")])
    _admitted(instance, 3, [_adapter_upload(instance, "other_epoch", epoch="epoch_0"), _adapter_upload(instance, "after_it")])
    _admitted(instance, 4, [_adapter_upload(instance, "upload_pending", status="pending")])
    _admitted(instance, 5, [_adapter_upload(instance, "processing")], status="processing")
    _adapter_upload(instance, "upload_only")

    result = instance.run()

    assert result.finished and result.counts.files_without_records == 5
    assert (instance.transfer / _hex_name("read_1")).is_file(), "nothing is deleted"
    counted = ["other_epoch", "after_it", "upload_pending", "processing", "upload_only"]
    _assert_listed(instance.layout, [_hex_name(name) for name in counted])


@pytest.mark.asyncio
async def test_the_file_left_out_is_the_one_the_real_gateway_reads_and_removes_at_its_start(tmp_path: Path) -> None:
    """F2 of PR #150's review, against the real adapter host service and request handler.

    A message taken in before a stop stays ``accepted``; its file is not
    counted. At the next start the gateway handles it, reads the file and
    removes it, and only the file no record names is left: the one the
    advice lets an administrator delete once the gateway has run.
    """

    from tests.test_gateway_question_queue import TELEGRAM_ID, _two_requests_ask
    from tests.test_hosted_chat_uploads import PHOTO, _HostedChat
    from tests.test_gateway_adapter_host_phase4 import ADAPTER_ID, NOW, PLUGIN_ID, _peer

    hosted = _HostedChat(tmp_path)  # its service has no request handler: a message it takes in stays accepted
    await _two_requests_ask(hosted.chat)
    hosted.chat.finish_each_answered_task()
    sent = hosted.upload("receipt.png", PHOTO)
    hosted.service.accept_request(_peer(), ChannelInboundMessageV1(
        protocol_version=ADAPTER_HOST_PROTOCOL_VERSION, message_type="channel.inbound.v1", request_id="request-1",
        idempotency_key="inbound-idem-1", plugin_id=PLUGIN_ID, adapter_instance_id=ADAPTER_ID,
        session_epoch=hosted.epoch, stream_position=1, channel_name="telegram", external_user_id=TELEGRAM_ID,
        chat_id=TELEGRAM_ID, text="Here is the receipt", attachments=(sent,), received_at_utc=NOW,
    ))
    (read_at_start,) = hosted.transfer_copies()
    stray = _write(hosted.transfer_dir / STRAY_NAME, STRAY)
    repository = hosted.service._repository
    # The chat's own gateway records, which the check for records in another folder reads (F7 of PR #154's review).
    gateway = create_gateway_state_repository(resolve_storage_settings({"backend": "sqlite", "sqlite": {"path": str(tmp_path / "state.db")}}))
    step = upgrade_module._Step(
        transfer=hosted.transfer_dir, uploads=hosted.chat.config.layout.uploads_dir.resolve(), gateway=gateway,
        adapter_host=lambda: repository, conversations=None, marker=tmp_path / "marker", progress=tmp_path / "progress",
    )

    assert step.files_without_records() == upgrade_module._Leftovers(
        (STRAY_NAME,), recorded_elsewhere=NamedElsewhere()
    ), "only the file no record names"

    restarted = AdapterHostApplicationService(repository, hosted.service._policy, request_handler=hosted.handler)
    hosted.service = restarted  # the handler's service resolver
    await restarted.start()
    for _ in range(100):
        record = repository.get_inbox_by_idempotency(
            authenticated_host_id=_peer().host_id, plugin_id=PLUGIN_ID, adapter_instance_id=ADAPTER_ID,
            registration_epoch=hosted.epoch, idempotency_key="inbound-idem-1",
        )
        if record.status != "accepted" and record.status != "processing":
            break
        await asyncio.sleep(0.01)
    await restarted.close()

    assert record.status == "completed" and hosted.chat.lead.answers()
    assert not read_at_start.exists() and hosted.transfer_copies() == [stray]
    assert step.files_without_records() == upgrade_module._Leftovers((STRAY_NAME,), recorded_elsewhere=NamedElsewhere())



# --- Guidance the gateway holds ------------------------------------------------------------------


def _hold(instance: _Instance, correlation_id: str, *files: UserTaskAttachment, raw: str | None = None) -> str:
    """Guidance the gateway held across the upgrade, naming ``files``; its stored copy is returned."""

    steering = TaskSteering(
        steering_type=TaskSteeringType.PROACTIVE_GUIDANCE,
        guidance_text=f"Use this file too ({correlation_id}).",
        attachments=list(files),
    )
    stored = raw if raw is not None else steering.model_dump_json()
    instance.gateway().hold_guidance(
        HeldGuidanceRecord(
            correlation_id=correlation_id,
            target_correlation_id="corr_running",
            session_key="telegram:chat:7",
            conversation_id=CONVERSATION,
            steering_json=stored,
            created_at="2026-11-01T00:00:00+00:00",
        )
    )
    return stored


def _held_copy(instance: _Instance, correlation_id: str) -> str:
    return instance.gateway().get_held_guidance(correlation_id).steering_json


def _turn_record(instance: _Instance, attachment_id: str) -> UserTaskAttachment:
    """The 0.9.13 record a turn keeps, with the transfer folder path and the gateway's sha256."""

    return next(
        entry.attachment
        for entry in instance.conversations().list_turn_attachments(CONVERSATION)
        if entry.attachment.attachment_id == attachment_id
    )


def test_held_guidance_follows_its_moved_file_and_the_lead_keeps_it_when_sent_again(instance: _Instance, caplog) -> None:
    """F2 (DATA-1): the gateway signs held guidance again from its copy, so the copy must name the moved file.

    Only a copy naming the gateway record's path and sha256 follows it; the others stay exactly as stored and are
    named in a warning. Guidance naming a file no other record names makes nothing wait.
    """

    photo, lost = _turn_record(instance, "att_photo"), _turn_record(instance, "att_lost")
    _hold(instance, "guide_photo", photo)
    untouched = {
        "guide_lost": _hold(instance, "guide_lost", lost),
        "guide_other_sha": _hold(
            instance, "guide_other_sha", photo.model_copy(update={"sha256": hashlib.sha256(b"else").hexdigest()})
        ),
        "guide_held_only": _hold(
            instance,
            "guide_held_only",
            _record("att_stray", str(_write(instance.transfer / "adapter_upload_stray", b"stray")), "0" * 64),
        ),
        "guide_unreadable": _hold(instance, "guide_unreadable", raw="{not json"),
    }
    before = TaskSteering.model_validate_json(_held_copy(instance, "guide_photo"))

    with caplog.at_level(logging.WARNING, logger=MODULE_LOGGER):
        result = instance.run()

    assert result.finished and result.counts.moved == 1, "guidance naming a file nothing else names waits for nothing"
    after = TaskSteering.model_validate_json(_held_copy(instance, "guide_photo"))
    (moved,) = after.attachments
    assert moved.local_path() == str(instance.photo_destination)
    assert after.model_dump(exclude={"attachments"}) == before.model_dump(exclude={"attachments"})
    assert moved.model_dump(exclude={"storage_refs"}) == photo.model_dump(exclude={"storage_refs"})
    # What the team lead does with the files of guidance it receives (sent_files_runtime._kept_upload_files);
    # the whole send is in tests/test_adapter_host_held_guidance_resend.py.
    folder = UploadFolder.for_instance(instance.root)
    assert keep_upload_files(after.attachments, folder, task_id="task_2", conversation_id=CONVERSATION) == [moved]
    located = locate_upload(moved, folder, task_id="task_2", conversation_id=CONVERSATION)
    assert isinstance(located, LocatedUpload) and located.read_bytes(max_bytes=100) == CONTENT["att_photo"]
    assert {key: _held_copy(instance, key) for key in untouched} == untouched, "the others stay exactly as stored"
    not_moved = _events(caplog, "adapter_host_file_not_moved")
    (lost_warning,) = [message for message in not_moved if "attachment_id=att_lost" in message]
    assert "held_guidance_ids=guide_lost " in lost_warning and "sent without the file" in lost_warning
    (mismatch,) = [message for message in not_moved if "reason=record_mismatch" in message]
    assert "attachment_id=att_photo" in mismatch and "held_guidance_ids=guide_other_sha " in mismatch
    assert mismatch.endswith("records=kept reason=record_mismatch") and "restart" not in mismatch
    (held_only,) = _events(caplog, "adapter_host_files_held_only")
    assert "attachment_id=att_stray correlation_id=guide_held_only" in held_only
    (unreadable,) = _events(caplog, "adapter_host_files_held_guidance_unreadable")
    assert unreadable.endswith("correlation_id=guide_unreadable")


def test_a_held_copy_released_or_already_moved_is_left_alone(instance: _Instance) -> None:
    """The gateway may release a copy while an operator's run works beside it; a second rewrite changes nothing."""

    photo = _turn_record(instance, "att_photo")
    _hold(instance, "guide_photo", photo)
    gateway = instance.gateway()
    destination = str(instance.photo_destination)

    assert gateway.relocate_held_guidance_attachment(["released", "guide_photo"], "att_photo", destination) == 1
    once = _held_copy(instance, "guide_photo")
    assert gateway.relocate_held_guidance_attachment(["guide_photo"], "att_photo", destination) == 0
    assert gateway.relocate_held_guidance_attachment(["guide_photo"], "att_other", "/elsewhere") == 0
    assert _held_copy(instance, "guide_photo") == once


# --- Runs that overlap ---------------------------------------------------------------------------


def _hold_the_lock(instance: _Instance) -> int:
    """Another run holds the step's lock: a descriptor of its own with the lock taken."""

    path = instance.layout.adapter_host_files_upgrade_lock_path
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor = os.open(path, os.O_RDWR | os.O_CREAT, 0o600)
    fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
    return descriptor


class _Seen(logging.Handler):
    """Sets ``event`` when a log line names ``name``."""

    def __init__(self, name: str) -> None:
        super().__init__(logging.WARNING)
        self.name_seen = name
        self.event = threading.Event()

    def emit(self, record: logging.LogRecord) -> None:
        if f"event={self.name_seen}" in record.getMessage():
            self.event.set()


@pytest.mark.parametrize("other_run", ["finished_the_step", "stopped"])
def test_a_run_that_finds_the_lock_held_waits_for_the_other(instance: _Instance, other_run) -> None:
    """F13 (CONC-4): runs never overlap, so they never move the same file or share a temporary copy.

    A run that waited reads the marker again: when the other run finished, it does nothing; when the other
    stopped, it does the work.
    """

    held = _hold_the_lock(instance)
    seen = _Seen("adapter_host_files_waiting_for_other_run")
    module_logger = logging.getLogger(MODULE_LOGGER)
    module_logger.addHandler(seen)
    results: list[upgrade_module.UpgradeResult] = []
    waiting = threading.Thread(target=lambda: results.append(instance.run()), daemon=True)
    try:
        waiting.start()
        assert seen.event.wait(30), "the second run says it waits"
        waiting.join(0.5)
        assert waiting.is_alive() and not results, "it waits while the lock is held"
        assert instance.files()["att_photo"] == str(instance.photo_source), "and has changed nothing"
        if other_run == "finished_the_step":
            instance.layout.adapter_host_files_upgrade_path.write_text("{}\n")
            their_list = _plant_earlier_list(instance.layout)  # the list the finished run wrote beside its marker
            theirs = their_list.read_bytes()
    finally:
        os.close(held)
        module_logger.removeHandler(seen)
    waiting.join(30)

    (result,) = results
    if other_run == "finished_the_step":
        assert result.already_done and instance.files()["att_photo"] == str(instance.photo_source)
        # C05 of F6: it removes an earlier list only after it saw no marker, inside the lock.
        assert their_list.read_bytes() == theirs, "the finished run's list, which its warning points at, stays"
    else:
        assert result.finished and not result.already_done and result.counts.moved == 1
        assert instance.files()["att_photo"] == str(instance.photo_destination)


@pytest.mark.parametrize("cross_disk", [False, True], ids=["same_disk", "cross_disk"])
def test_the_run_holds_its_lock_from_find_to_the_marker(instance: _Instance, monkeypatch, cross_disk) -> None:
    """C11 (F13): the lock covers every phase, the cross-disk copy and the marker included, not only its taking.

    Each phase tries the lock from a descriptor of its own, as another run would; it must find it held. The
    list is already written when the marker is (C11 of F6): a stop between the two then leaves a list the next
    run removes, never a marker counting files with no list beside it.
    """

    if cross_disk:
        _across_file_systems(monkeypatch)
    lock_path = instance.layout.adapter_host_files_upgrade_lock_path
    marker = instance.layout.adapter_host_files_upgrade_path
    held_in: dict[str, bool] = {}

    def lock_is_held() -> bool:
        descriptor = os.open(lock_path, os.O_RDWR)
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return True
        finally:
            os.close(descriptor)
        return False

    def probed(name: str, real: Callable[..., Any]) -> Callable[..., Any]:
        def probe(*args, **kwargs):
            held_in[name] = held_in.get(name, True) and lock_is_held()
            return real(*args, **kwargs)

        return probe

    phases = ["find", "survey", "start", "handle", "forget_unmovable"] + (["_copy_into_place"] if cross_disk else [])
    for phase in phases:
        monkeypatch.setattr(upgrade_module._Step, phase, probed(phase, getattr(upgrade_module._Step, phase)))
    # C05 of F6: the start's removal of an earlier list and the list's write hold the lock too.
    _plant_earlier_list(instance.layout)
    _write(instance.transfer / STRAY_NAME, STRAY)
    list_steps = ["_remove_leftover_list", "_settle_leftover_list"]
    for name in list_steps:
        monkeypatch.setattr(upgrade_module, name, probed(name, getattr(upgrade_module, name)))
    real_write = upgrade_module._write_state_file
    leftover_list = instance.layout.adapter_host_files_without_records_path
    list_at_marker: list[bytes | None] = []

    def write(path, body):
        if Path(path) == Path(marker):
            held_in["marker"] = lock_is_held()
            list_at_marker.append(leftover_list.read_bytes() if leftover_list.exists() else None)
        return real_write(path, body)

    monkeypatch.setattr(upgrade_module, "_write_state_file", write)

    result = instance.run()

    assert result.finished and result.counts.moved == 1
    assert held_in == {phase: True for phase in [*phases, *list_steps, "marker"]}
    assert list_at_marker == [f"{STRAY_NAME}\n".encode()], "C11 of F6: the list is written before the marker"
    _assert_listed(instance.layout, [STRAY_NAME])
    assert not lock_is_held(), "the lock is released when the run ends"


def test_a_state_folder_without_file_locks_warns_and_goes_on(instance: _Instance, monkeypatch, caplog) -> None:
    """F13: ENOLCK (a network state folder) must neither stop every start nor pass silently."""

    def no_locks(_descriptor, _mode):
        raise OSError(errno.ENOLCK, os.strerror(errno.ENOLCK))

    monkeypatch.setattr(upgrade_module.fcntl, "flock", no_locks)
    with caplog.at_level(logging.WARNING, logger=MODULE_LOGGER):
        result = instance.run()

    assert result.finished and result.counts.moved == 1
    (warning,) = _events(caplog, "adapter_host_files_lock_unsupported")
    assert "do not run it twice at once" in warning


# --- The transfer folder is not here -------------------------------------------------------------


def test_with_the_folder_missing_nothing_changes_until_the_files_are_declared_gone(instance: _Instance, caplog) -> None:
    moved_aside = instance.root / "unmounted"
    instance.transfer.rename(moved_aside)
    before = (instance.files(), instance.recorded("task_1"), instance.gateway().get_attachment("att_photo"))

    with caplog.at_level(logging.ERROR, logger=MODULE_LOGGER):
        waiting = instance.run()

    assert not waiting.finished and waiting.folder_missing and waiting.waiting == 3  # two gateway records, one orphan
    assert (instance.files(), instance.recorded("task_1"), instance.gateway().get_attachment("att_photo")) == before
    assert not instance.layout.adapter_host_files_upgrade_path.exists()
    (error,) = _events(caplog, "adapter_host_files_folder_missing")
    assert "--files-gone" in error and error.endswith(f"folder={instance.transfer} waiting=3 recorded=- current=-")

    gone = instance.run(files_gone=True)

    assert gone.finished and (gone.counts.missing, gone.counts.no_record, gone.counts.moved) == (2, 1, 0)
    assert gone.counts.files_without_records == 0, "a folder that is not there holds none"
    assert set(instance.files()) == {"att_web"}
    assert instance.layout.adapter_host_files_upgrade_path.is_file()


def test_a_files_gone_run_beside_the_running_team_keeps_what_the_team_writes(instance: _Instance, monkeypatch) -> None:
    """The operator runs ``--files-gone`` with ``docker exec`` while the team runs (C20).

    Between the step's scan and its rewrites, an agent merges a file sent after
    the upgrade into a turn the step changes, and the gateway's history
    clean-up deletes a record the step is about to drop. The merged file stays
    (turns are rewritten from a fresh read under row locks, not from the scan)
    and the deleted record is skipped.
    """

    instance.transfer.rename(instance.root / "unmounted")
    assert instance.run().folder_missing
    new_path = str(instance.layout.uploads_dir.resolve() / "telegram_chat_7" / "att_new.png")
    new_file = _record("att_new", new_path, hashlib.sha256(b"sent after the upgrade").hexdigest())
    real = _RelationalConversationRepository.relocate_turn_attachment
    team_wrote: list[str] = []

    def beside_the_team(self, turns, attachment_id, *, local_path):
        if not team_wrote:
            team_wrote.append(attachment_id)
            assert instance.repository().merge_turn_attachments(
                conversation_id=CONVERSATION, task_id="task_1", attachments=[new_file]
            ) == 1
            assert instance.gateway().delete_attachment("att_lost")
        return real(self, turns, attachment_id, local_path=local_path)

    monkeypatch.setattr(_RelationalConversationRepository, "relocate_turn_attachment", beside_the_team)
    result = instance.run(files_gone=True)
    monkeypatch.undo()

    assert team_wrote, "the team wrote between the scan and the rewrites"
    assert result.finished and (result.counts.missing, result.counts.no_record) == (2, 1)
    assert result.counts.gateway_rows_removed == 1, "att_photo's record; att_lost's was already gone"
    assert result.counts.files_without_records == 0
    assert instance.turn_paths("task_1") == {"att_new": new_path}
    assert instance.recorded("task_1") == 0
    assert instance.gateway().get_attachment("att_photo") is None
    assert instance.layout.adapter_host_files_upgrade_path.is_file()


def _untouched(instance: _Instance) -> tuple[Any, ...]:
    """What a run that changes nothing leaves as it found it: the records, the turns' markers and the files."""

    gateway = instance.gateway()
    return (
        instance.files(),
        instance.recorded("task_1"),
        instance.recorded("task_2"),
        gateway.get_attachment("att_photo"),
        gateway.get_attachment("att_lost"),
        sorted(str(path) for path in instance.root.rglob("adapter_upload_*")),
    )


def _empty_mount_point(instance: _Instance) -> None:
    """The transfer disk is not mounted: its mount point is an empty folder."""

    for path in instance.transfer.iterdir():
        path.unlink()


def _recreated_by_the_new_adapter_host(instance: _Instance) -> None:
    """A run found the folder missing; then 0.10's adapter host created it for a new upload."""

    instance.transfer.rename(instance.root / "unmounted")
    assert instance.run().folder_missing
    _write(instance.transfer / "adapter_upload_new", b"sent after the upgrade, recorded under state/uploads")


@pytest.mark.parametrize("change", [_empty_mount_point, _recreated_by_the_new_adapter_host], ids=["empty", "recreated"])
def test_a_folder_holding_none_of_the_files_changes_nothing(instance: _Instance, caplog, change) -> None:
    change(instance)
    before = _untouched(instance)

    with caplog.at_level(logging.ERROR, logger=MODULE_LOGGER):
        result = instance.run()

    assert not result.finished and result.folder_missing and result.waiting == 3
    assert _untouched(instance) == before
    assert not instance.layout.adapter_host_files_upgrade_path.exists()
    assert _events(caplog, "adapter_host_files_folder_missing")


def _unreadable_transfer(instance: _Instance) -> Path:
    os.chmod(instance.transfer, 0)
    return instance.transfer


def _read_only_transfer(instance: _Instance) -> Path:
    """The move would also remove the name from the transfer folder, which cannot be written."""

    os.chmod(instance.transfer, 0o555)
    return instance.transfer


def _read_only_uploads(instance: _Instance) -> Path:
    uploads = instance.layout.uploads_dir.resolve()
    os.chmod(uploads, 0o555)
    return uploads


@pytest.mark.skipif(os.geteuid() == 0, reason="permissions do not stop root")
@pytest.mark.parametrize(
    "change", [_unreadable_transfer, _read_only_transfer, _read_only_uploads], ids=["transfer", "transfer_read_only", "uploads"]
)
def test_a_folder_it_cannot_use_stops_startup_and_changes_nothing(instance: _Instance, caplog, change) -> None:
    folder = change(instance)
    try:
        before = _untouched(instance)  # read while the change holds; rglob skips the unreadable folder
        with caplog.at_level(logging.ERROR, logger=MODULE_LOGGER):
            with pytest.raises(upgrade_module.FolderProblem) as raised:
                instance.run()
        assert _untouched(instance) == before, "the lost file's records stay until the folder works"
    finally:
        os.chmod(folder, 0o755)

    assert raised.value.folder == folder and str(folder) in str(raised.value)
    assert not instance.layout.adapter_host_files_upgrade_path.exists()
    (error,) = _events(caplog, "adapter_host_files_folder_unusable")
    assert f"folder={folder}" in error
    assert instance.run().counts.moved == 1, "a restart after the fix finishes the work"


def test_a_name_the_file_system_refuses_is_refused_not_retried_forever(instance: _Instance, caplog) -> None:
    long_id = "att_" + "x" * 252  # a 256-character id, which 0.9.13 accepted, plus its suffix: too long a file name
    _forged_id(instance, long_id)

    with caplog.at_level(logging.WARNING, logger=MODULE_LOGGER):
        result = instance.run()

    assert result.finished and result.counts.refused == 1 and result.counts.moved == 0
    assert instance.gateway().get_attachment(long_id).local_path == str(instance.photo_source), "its record is kept"
    assert instance.photo_source.read_bytes() == CONTENT["att_photo"]
    assert any(message.endswith("records=kept reason=name_refused") for message in _events(caplog, "adapter_host_file_not_moved"))


def _rerun_after_fix(instance: _Instance) -> upgrade_module.UpgradeCounts:
    """What the operator does after fixing a file the step refused: delete the marker and start again."""

    instance.layout.adapter_host_files_upgrade_path.unlink()
    return instance.run().counts


@pytest.mark.skipif(os.geteuid() == 0, reason="permissions do not stop root")
def test_a_file_it_cannot_read_is_refused_naming_that_file_and_keeps_its_records(instance: _Instance, caplog) -> None:
    """The folder is fine and only the photo is unreadable: that record is refused and kept, and startup goes on."""

    os.chmod(instance.photo_source, 0)
    try:
        with caplog.at_level(logging.WARNING, logger=MODULE_LOGGER):
            result = instance.run()
    finally:
        os.chmod(instance.photo_source, 0o640)

    assert result.finished and result.counts.refused == 1 and result.counts.moved == 0
    _assert_photo_kept(instance)
    (warning,) = [message for message in _events(caplog, "adapter_host_file_not_moved") if "attachment_id=att_photo" in message]
    assert f"folder={instance.transfer} file={instance.photo_source} records=kept reason=unreadable" in warning
    assert str(instance.layout.adapter_host_files_upgrade_path) in warning, "it says how to try again"
    assert _rerun_after_fix(instance).moved == 1, "after the fix, a run without the marker moves it"


def _photo_already_at_destination(instance: _Instance) -> Path:
    _write(instance.photo_destination, CONTENT["att_photo"])
    return instance.photo_destination


def _photo_renamed_before_records(instance: _Instance) -> Path:
    """An earlier run renamed the photo into ``state/uploads`` and stopped before rewriting its records (C18)."""

    instance.photo_destination.parent.mkdir(parents=True, exist_ok=True)
    instance.photo_source.rename(instance.photo_destination)
    return instance.photo_destination


@pytest.mark.parametrize(
    "where", ["source", "destination"], ids=["source", "destination"]
)
def test_a_read_error_refuses_that_file_naming_it_and_keeps_its_records(
    instance: _Instance, monkeypatch, caplog, where
) -> None:
    """An I/O error (a bad sector) while reading one file refuses that record only: it never stops startup."""

    if where == "destination":
        file, folder = _photo_already_at_destination(instance), instance.layout.uploads_dir.resolve()
    else:
        file, folder = instance.photo_source, instance.transfer

    def failing_pread(*_args):
        raise OSError(errno.EIO, "Input/output error")

    monkeypatch.setattr(upgrade_module.os, "pread", failing_pread)
    with caplog.at_level(logging.WARNING, logger=MODULE_LOGGER):
        result = instance.run()
    monkeypatch.undo()

    assert result.finished and result.counts.refused == 1
    _assert_photo_kept(instance)
    (warning,) = [message for message in _events(caplog, "adapter_host_file_not_moved") if "attachment_id=att_photo" in message]
    assert "Input/output error" in warning and f"folder={folder} file={file} records=kept reason=unreadable" in warning
    counts = _rerun_after_fix(instance)
    assert counts.moved + counts.already_moved == 1, "after the fix, a run without the marker finishes the work"


def test_a_pipe_in_place_of_a_file_is_refused_without_waiting(instance: _Instance, caplog) -> None:
    """A pipe with no writer would block a plain open; the step opens without waiting and refuses it."""

    import signal

    instance.photo_source.unlink()
    os.mkfifo(instance.photo_source, 0o600)

    def stuck(*_args):
        raise AssertionError("the step waited for a writer on the pipe")

    previous = signal.signal(signal.SIGALRM, stuck)
    signal.alarm(5)
    try:
        with caplog.at_level(logging.WARNING, logger=MODULE_LOGGER):
            result = instance.run()
    finally:
        signal.alarm(0)
        signal.signal(signal.SIGALRM, previous)

    assert result.finished and result.counts.moved == 0 and result.counts.refused == 1
    assert instance.gateway().get_attachment("att_photo").local_path == str(instance.photo_source), "its record is kept"
    assert instance.photo_source.is_fifo(), "the pipe is left where it is"
    assert any(
        "attachment_id=att_photo" in message and message.endswith("records=kept reason=not_regular_file")
        for message in _events(caplog, "adapter_host_file_not_moved")
    )


def test_a_name_too_long_for_the_file_system_is_no_sign_the_folder_holds_the_files(instance: _Instance, caplog) -> None:
    """One record whose stored file name the file system refuses cannot make an empty mount point look found."""

    _empty_mount_point(instance)
    long_path = instance.transfer / ("adapter_upload_" + "x" * 300)
    instance.gateway().upsert_attachment(
        attachment_id="att_long",
        session_key="telegram:chat:7",
        conversation_id=CONVERSATION,
        correlation_id=None,
        filename="long.png",
        content_type="image/png",
        kind="image",
        size_bytes=1,
        sha256=hashlib.sha256(b"x").hexdigest(),
        local_path=str(long_path),
        owner_canonical_user_id=PERSON,
    )
    before = _untouched(instance)

    with caplog.at_level(logging.ERROR, logger=MODULE_LOGGER):
        result = instance.run()

    assert not result.finished and result.folder_missing and result.waiting == 4
    assert _untouched(instance) == before
    assert instance.gateway().get_attachment("att_long") is not None
    assert not instance.layout.adapter_host_files_upgrade_path.exists()
    assert _events(caplog, "adapter_host_files_folder_missing")


def _record_named(instance: _Instance, attachment_id: str, local_path: str) -> None:
    instance.gateway().upsert_attachment(
        attachment_id=attachment_id,
        session_key="telegram:chat:7",
        conversation_id=CONVERSATION,
        correlation_id=None,
        filename="named.png",
        content_type="image/png",
        kind="image",
        size_bytes=1,
        sha256=hashlib.sha256(b"x").hexdigest(),
        local_path=local_path,
        owner_canonical_user_id=PERSON,
    )


@pytest.mark.parametrize("files_here", [True, False], ids=["files_here", "empty_mount_point"])
def test_a_nul_byte_in_a_stored_name_refuses_only_that_record(instance: _Instance, request, caplog, files_here) -> None:
    """A corrupt record whose file name holds a NUL byte (only SQLite can store one) never stops startup (C16).

    With the files here it is refused and kept, and the rest moves, also with ``--files-gone``; on an empty
    mount point it is no sign of the files, so the step waits and nothing changes.
    """

    if "postgres" in request.node.callspec.id:
        pytest.skip("PostgreSQL refuses a NUL byte in text, so it cannot hold such a record.")
    if not files_here:
        _empty_mount_point(instance)
    nul_path = str(instance.transfer / "adapter_upload_a\x00b")
    _record_named(instance, "att_nul", nul_path)
    before = _untouched(instance)

    with caplog.at_level(logging.WARNING, logger=MODULE_LOGGER):
        result = instance.run()

    assert instance.gateway().get_attachment("att_nul").local_path == nul_path
    if not files_here:
        assert result.folder_missing and result.waiting == 4 and _untouched(instance) == before
        assert instance.run(files_gone=True).finished, "--files-gone finishes; the NUL record is refused, not an error"
        assert instance.gateway().get_attachment("att_nul").local_path == nul_path
        return
    assert result.finished and result.counts.moved == 1 and result.counts.refused == 1
    assert any(
        "attachment_id=att_nul" in message and message.endswith("records=kept reason=unsafe_name")
        for message in _events(caplog, "adapter_host_file_not_moved")
    )


# --- One error rule at every operation ----------------------------------------------------------------
#
# Each case injects one error at one operation the step performs on the photo, its folders or its copy, and
# checks which case of the module's rule it falls under: "refused" (rule 1: only that record is refused, its
# records are kept, startup goes on), "folder" (rule 2: FolderProblem names the folder, nothing is dropped, no
# marker, and a run after the fix finishes the work), "missing" (rule 3: proven absent, records dropped) or
# "moved" (the error comes after the file and its records are safe, or is a sync the file system does not offer).


def _is(path: Callable[[_Instance], Path]) -> Callable[[_Instance, Path], bool]:
    return lambda instance, subject: subject == path(instance)


def _copy(instance: _Instance, subject: Path) -> bool:
    return subject.parent == instance.photo_destination.parent and subject.name.startswith(".moving-")


_PHOTO = _is(lambda instance: instance.photo_source)
_ORPHAN = _is(lambda instance: instance.transfer / "adapter_upload_orphan")
_TRANSFER = _is(lambda instance: instance.transfer)
_SESSION = _is(lambda instance: instance.photo_destination.parent)
_DESTINATION = _is(lambda instance: instance.photo_destination)
_MARKER = _is(lambda instance: instance.layout.adapter_host_files_upgrade_path)
_PROGRESS = _is(lambda instance: instance.layout.adapter_host_files_upgrade_progress_path)
_LOCK = _is(lambda instance: instance.layout.adapter_host_files_upgrade_lock_path)
_FD_FUNCTIONS = {"pread", "write", "fsync", "fstat", "fchmod"}


def _subject(function: str, args: tuple[Any, ...], kwargs: dict[str, Any]) -> Path | None:
    """The path an ``os`` call works on: its descriptor's file, or its name in its folder descriptor."""

    first = args[0] if args else None
    if function in _FD_FUNCTIONS or isinstance(first, int):
        try:
            return Path(os.readlink(f"/proc/self/fd/{first}"))
        except OSError:
            return None
    if function == "rename":
        folder = kwargs.get("src_dir_fd")
    elif function == "replace":
        folder, first = None, args[1]  # what it replaces
    else:
        folder = kwargs.get("dir_fd")
    name = os.fsdecode(first)
    if folder is not None:
        return Path(os.readlink(f"/proc/self/fd/{folder}")) / name
    return Path(os.path.normpath(name))


def _vanish_then(error: int) -> Callable[[_Instance], OSError]:
    def make(instance: _Instance) -> OSError:
        instance.photo_source.unlink()  # someone removed the file between the check and the rename
        return OSError(error, os.strerror(error))

    return make


@dataclass(frozen=True)
class _Fault:
    function: str
    target: Callable[[_Instance, Path], bool]
    error: Any  # an errno, an exception, or a callable making one from the instance
    expect: str
    reason: str = ""  # the refusal's reason, or which folder a FolderProblem names ("transfer", "uploads", "state")
    cross_disk: bool = False
    also: tuple[tuple[str, Callable[[_Instance, Path], bool], Any], ...] = ()
    setup: Callable[[_Instance], Any] | None = None
    counted: bool = True  # False: counting the files no record names fails, which only gives a warning and None


def _plant_stray(instance: _Instance) -> Path:
    return _write(instance.transfer / STRAY_NAME, STRAY)


_STRAY = _is(lambda instance: instance.transfer / STRAY_NAME)
_LIST = _is(lambda instance: instance.layout.adapter_host_files_without_records_path)


_FAULTS = {
    # Rule 1: one file or one record.
    "open_file_permission": _Fault("open", _PHOTO, errno.EACCES, "refused", "unreadable"),
    "open_file_bad_name": _Fault("open", _PHOTO, ValueError("embedded null byte"), "refused", "unsafe_name"),
    "open_file_name_too_long": _Fault("open", _PHOTO, errno.ENAMETOOLONG, "refused", "name_refused"),
    "fstat_file_io": _Fault("fstat", _PHOTO, errno.EIO, "refused", "unreadable"),
    "read_file_io": _Fault("pread", _PHOTO, errno.EIO, "refused", "unreadable"),
    "rename_file_io": _Fault("rename", _PHOTO, errno.EIO, "refused", "move_failed"),
    "rename_file_bad_name": _Fault("rename", _PHOTO, errno.EINVAL, "refused", "name_refused"),
    "unlink_stale_copy_io": _Fault("unlink", _copy, errno.EISDIR, "refused", "move_failed", cross_disk=True),
    "open_copy_io": _Fault("open", _copy, errno.EIO, "refused", "move_failed", cross_disk=True),
    "write_copy_io": _Fault("write", _copy, errno.EIO, "refused", "move_failed", cross_disk=True),
    "fsync_copy_io": _Fault("fsync", _copy, errno.EIO, "refused", "move_failed", cross_disk=True),
    "read_back_copy_io": _Fault("pread", _copy, errno.EIO, "refused", "unreadable", cross_disk=True),
    "fchmod_copy_io": _Fault("fchmod", _copy, errno.EIO, "refused", "move_failed", cross_disk=True),
    "close_copy_io": _Fault("close", _copy, errno.EIO, "refused", "move_failed", cross_disk=True),
    "rename_copy_bad_name": _Fault("rename", _copy, errno.EINVAL, "refused", "name_refused", cross_disk=True),
    "open_destination_io": _Fault(
        "open", _DESTINATION, errno.EIO, "refused", "unreadable", setup=_photo_already_at_destination
    ),
    "open_destination_io_after_earlier_rename": _Fault(
        "open", _DESTINATION, errno.EIO, "refused", "unreadable", setup=_photo_renamed_before_records
    ),
    # Nor can it be told whether anything is at the destination: the records are kept, startup goes on (C21).
    "open_and_stat_destination_io_after_earlier_rename": _Fault(
        "open",
        _DESTINATION,
        errno.EIO,
        "refused",
        "unreadable",
        also=(("stat", _DESTINATION, errno.EIO),),
        setup=_photo_renamed_before_records,
    ),
    "path_construction": _Fault("path", _PHOTO, ValueError("no file name"), "refused", "unsafe_name"),
    # Rule 2: a folder. The first open of the transfer folder is the one that reads its identity.
    "open_transfer_folder": _Fault("open", _TRANSFER, errno.EACCES, "folder", "transfer"),
    "fstat_transfer_identity": _Fault("fstat", _TRANSFER, errno.EIO, "folder", "transfer"),
    "read_progress_record": _Fault("open", _PROGRESS, errno.EIO, "folder", "state"),
    "write_progress_record": _Fault("replace", _PROGRESS, errno.EROFS, "folder", "state"),
    "open_lock_read_only": _Fault("open", _LOCK, errno.EROFS, "folder", "state"),
    "stat_in_unsearchable_folder": _Fault("stat", _ORPHAN, errno.EACCES, "folder", "transfer"),
    "open_file_in_unsearchable_folder": _Fault(
        "open", _PHOTO, errno.EACCES, "folder", "transfer", also=(("stat", _PHOTO, errno.EACCES),)
    ),
    "mkdir_session_read_only": _Fault("mkdir", _SESSION, errno.EROFS, "folder", "uploads"),
    "rename_read_only": _Fault("rename", _PHOTO, errno.EROFS, "folder", "uploads"),
    "rename_destination_folder_gone": _Fault("rename", _PHOTO, errno.ENOENT, "folder", "uploads"),
    "write_copy_full": _Fault("write", _copy, errno.ENOSPC, "folder", "uploads", cross_disk=True),
    "close_copy_full": _Fault("close", _copy, errno.ENOSPC, "folder", "uploads", cross_disk=True),
    "fsync_folder_after_rename": _Fault("fsync", _SESSION, errno.EIO, "folder", "uploads"),
    "fsync_folder_after_copy": _Fault("fsync", _SESSION, errno.EIO, "folder", "uploads", cross_disk=True),
    "write_marker": _Fault("replace", _MARKER, errno.EROFS, "folder", "state", setup=_plant_stray),
    # Rule 3: proven absent.
    "rename_file_vanished": _Fault("rename", _PHOTO, _vanish_then(errno.ENOENT), "missing"),
    # After the file and its records are safe, or a sync the file system does not offer.
    "stat_name_too_long_is_no_sign": _Fault("stat", _ORPHAN, errno.ENAMETOOLONG, "moved"),
    "unlink_source_after_copy": _Fault("unlink", _PHOTO, errno.EACCES, "moved", cross_disk=True),
    "fsync_folder_not_offered": _Fault("fsync", _SESSION, errno.EINVAL, "moved"),
    "fsync_copy_not_offered": _Fault("fsync", _copy, errno.EINVAL, "moved", cross_disk=True),
    # Counting the files no record names only reports: an error there never stops startup or the marker (A2).
    "listdir_transfer_io": _Fault("listdir", _TRANSFER, errno.EIO, "moved", counted=False),
    "stat_stray_io": _Fault("stat", _STRAY, errno.EIO, "moved", setup=_plant_stray, counted=False),
}


def _inject(instance: _Instance, monkeypatch: pytest.MonkeyPatch, fault: _Fault) -> list[str]:
    """Make the fault's operations fail; the returned list names each failure as it happens."""

    fired: list[str] = []
    if fault.cross_disk:
        _across_file_systems(monkeypatch)
    for function, target, error in ((fault.function, fault.target, fault.error), *fault.also):
        if function == "path":
            real_sanitize = upgrade_module.sanitize_attachment_filename

            def refuse(value, error=error):
                if value == "photo.png":  # only the photo's record
                    fired.append("path")
                    raise error
                return real_sanitize(value)

            monkeypatch.setattr(upgrade_module, "sanitize_attachment_filename", refuse)
            continue
        real = getattr(os, function)

        def failing(*args, real=real, function=function, target=target, error=error, **kwargs):
            subject = _subject(function, args, kwargs)
            if subject is not None and target(instance, subject):
                fired.append(function)
                if function == "close":
                    real(*args, **kwargs)  # a close that reports an error still releases the descriptor
                made = error(instance) if callable(error) and not isinstance(error, BaseException) else error
                raise OSError(made, os.strerror(made)) if isinstance(made, int) else made
            return real(*args, **kwargs)

        monkeypatch.setattr(os, function, failing)
    return fired


@pytest.mark.parametrize("case", sorted(_FAULTS))
def test_one_error_rule_holds_at_every_operation(instance: _Instance, monkeypatch, caplog, case) -> None:
    fault = _FAULTS[case]
    if fault.setup is not None:
        fault.setup(instance)
    if fault.expect == "folder" and fault.reason != "state":
        before = _untouched(instance)
    fired = _inject(instance, monkeypatch, fault)
    with caplog.at_level(logging.WARNING, logger=MODULE_LOGGER):
        if fault.expect == "folder":
            with pytest.raises(upgrade_module.FolderProblem) as raised:
                instance.run()
        else:
            result = instance.run()
    monkeypatch.undo()
    assert fault.function in fired, "the error was injected"
    assert all(function in fired for function, _target, _error in fault.also), "every extra error was injected"
    marker = instance.layout.adapter_host_files_upgrade_path

    if fault.expect == "refused":
        assert result.finished and result.counts.refused == 1 and result.counts.moved == 0
        if fault.setup is _photo_renamed_before_records:
            _assert_photo_records_kept(instance)  # the file sits at its destination; its records are not dropped
        else:
            _assert_photo_kept(instance)
        assert fault.setup is not None or not instance.photo_destination.exists()
        assert not list(instance.photo_destination.parent.glob(".moving-*"))
        (warning,) = [message for message in _events(caplog, "adapter_host_file_not_moved") if "attachment_id=att_photo" in message]
        assert " folder=/" in warning and " file=/" in warning and warning.endswith(f"records=kept reason={fault.reason}")
        assert marker.is_file()
        counts = _rerun_after_fix(instance)
        assert counts.moved + counts.already_moved == 1, "after the fix, a run without the marker moves it"
    elif fault.expect == "folder":
        folder = {
            "transfer": instance.transfer,
            "uploads": instance.layout.uploads_dir.resolve(),
            "state": marker.parent,
        }[fault.reason]
        problem = raised.value
        assert problem.folder == folder and str(folder) in str(problem)
        (error,) = _events(caplog, "adapter_host_files_folder_unusable")
        assert f"folder={folder}" in error
        assert not marker.exists()
        # EVA-2: a run that wrote no marker reports no count (write_marker plants a file no record names),
        # and leaves no list: one written just before a marker that failed is removed again (C01 of F6).
        assert not _events(caplog, "adapter_host_files_without_records")
        _assert_no_list(instance.layout)
        if fault.reason != "state":
            gateway = instance.gateway()
            assert gateway.get_attachment("att_photo").local_path == str(instance.photo_source), "nothing finished"
            assert gateway.get_attachment("att_lost") is not None, "nothing was dropped"
            assert instance.files() == before[0] and instance.recorded("task_1") == before[1]
        result = instance.run()
        assert result.finished and result.counts.moved + result.counts.already_moved <= 1
        if case == "write_marker":
            _assert_listed(instance.layout, [STRAY_NAME])
    elif fault.expect == "missing":
        assert result.finished and result.counts.missing == 2
        assert instance.gateway().get_attachment("att_photo") is None and "att_photo" not in instance.files()
    else:
        assert result.finished and result.counts.moved == 1
    assert instance.gateway().get_attachment("att_photo") is None or (
        instance.gateway().get_attachment("att_photo").local_path == str(instance.photo_destination)
    )
    if fault.expect != "missing":
        assert instance.photo_destination.read_bytes() == CONTENT["att_photo"]
    if case == "unlink_source_after_copy":
        assert instance.photo_source.exists() and _events(caplog, "adapter_host_file_source_left")
    elif fault.expect != "missing":
        assert not instance.photo_source.exists()
    if case.endswith("not_offered"):
        assert _events(caplog, "adapter_host_files_sync_unsupported")
    if not fault.counted:
        assert result.counts.files_without_records is None and marker.is_file()
        assert json.loads(marker.read_text())["files_without_records"] is None
        (uncounted,) = _events(caplog, "adapter_host_files_without_records_uncounted")
        assert f"folder={instance.transfer} reason=" in uncounted and os.strerror(errno.EIO) in uncounted
        assert not _events(caplog, "adapter_host_files_without_records")
        _assert_no_list(instance.layout)
    elif fault.expect != "folder":
        # A file it did not move keeps a record naming it, so it is never counted as one no record names.
        assert result.counts.files_without_records == 0
        assert not _events(caplog, "adapter_host_files_without_records_uncounted")
        _assert_no_list(instance.layout)


@pytest.mark.parametrize("first_removal", ["succeeds", "fails"])
def test_a_list_that_cannot_be_written_never_stops_the_start(instance: _Instance, monkeypatch, caplog, first_removal) -> None:
    """A3 of F6: the list only reports; a failure writing it is logged, and the marker is written with the count.

    An earlier list the start could not remove is removed again after the
    failed write (EA-1 of PR #154's review), so no list is left beside the
    marker that is not its run's.
    """

    _write(instance.transfer / STRAY_NAME, STRAY)
    listed = _plant_earlier_list(instance.layout)
    removals = _refuse_removal(monkeypatch, listed, times=1 if first_removal == "fails" else 0)
    fired = _inject(instance, monkeypatch, _Fault("replace", _LIST, errno.ENOSPC, "moved"))
    with caplog.at_level(logging.WARNING, logger=MODULE_LOGGER):
        result = instance.run()
    monkeypatch.undo()

    assert fired == ["replace"]
    assert removals == ["unlink", "unlink"], "at the start and once more after the failed write"
    assert result.finished and result.counts.moved == 1 and result.counts.files_without_records == 1
    marker = json.loads(instance.layout.adapter_host_files_upgrade_path.read_text())
    assert marker["files_without_records"] == 1 and marker[LIST_ID_FIELD] is None, "no list, no id"
    *removal_failed, failed = _events(caplog, "adapter_host_files_without_records_list_failed")
    assert len(removal_failed) == (1 if first_removal == "fails" else 0)
    assert all("could not be removed" in message for message in removal_failed)
    assert failed.endswith(f"list={listed} reason=ENOSPC: {os.strerror(errno.ENOSPC)}")
    assert "could not be written; a list still at that path is not this run's, so do not use it" in failed
    _assert_no_list(instance.layout)  # the earlier list is gone, and no temporary file is left
    # The count is still reported; only the names shown may go, and there is no command for a list that is not there.
    (warning,) = _events(caplog, "adapter_host_files_without_records")
    assert warning.endswith(f"folder={instance.transfer} files_without_records=1 unlisted=0 list=-")
    assert f"The 1 file(s) with names the gateway gives: {STRAY_NAME}. The full list could not be written" in warning
    assert "Otherwise an administrator may delete exactly the files named here only while" in warning
    assert "xargs" not in warning
    assert warning.index("do not delete them") < warning.index("may delete exactly"), "C10"
    assert "This warning names no file from the sources= field of an adapter_host_file_not_moved warning" in warning
    assert "only after the gateway has started on 0.10 since the step's marker" in warning


@pytest.mark.parametrize("fails", ["once", "always"])
def test_a_list_that_cannot_be_removed_never_stops_the_start(
    instance: _Instance, monkeypatch, caplog, tmp_path, fails
) -> None:
    """A3 and C01 of F6: an earlier list is removed at the start and, if that fails, once more before the marker.

    A failure is logged and never stops the start. A list that can never be
    removed (a bind-mounted file answers EBUSY, an immutable one EPERM)
    stays beside the new marker, and both warnings say it is not this
    run's. The earlier run's command, pasted after the later run, deletes
    nothing (F1 of PR #154's review): its listed file has since gained a
    record, so the later run keeps it and says so in its not-moved warning.
    """

    stray = _write(instance.transfer / STRAY_NAME, STRAY)
    with caplog.at_level(logging.WARNING, logger=MODULE_LOGGER):
        assert instance.run().counts.files_without_records == 1
    earlier_command = _command_in(_assert_reported(caplog, instance, [STRAY_NAME]))
    earlier = instance.layout.adapter_host_files_without_records_path
    before = earlier.read_bytes()
    _record_named(instance, "att_stray", str(stray))  # another sha256: refused and kept
    instance.layout.adapter_host_files_upgrade_path.unlink()  # the documented re-run
    attempts = _refuse_removal(monkeypatch, earlier, times=1 if fails == "once" else None)
    caplog.clear()
    with caplog.at_level(logging.INFO, logger=MODULE_LOGGER):
        result = instance.run()
    monkeypatch.undo()

    assert result.finished and result.counts.files_without_records == 0
    marker = instance.layout.adapter_host_files_upgrade_path
    assert marker.is_file(), "the marker is still written"
    assert json.loads(marker.read_text())[LIST_ID_FIELD] is None, "this run wrote no list"
    assert attempts == ["unlink", "unlink"], "tried at the start and again before the marker"
    (kept,) = [m for m in _events(caplog, "adapter_host_file_not_moved") if "attachment_id=att_stray" in m]
    assert f" sources={stray} folder=" in kept and kept.endswith("records=kept reason=sha256_mismatch")
    failed = _events(caplog, "adapter_host_files_without_records_list_failed")
    removed = _events(caplog, "adapter_host_files_without_records_list_removed")
    reason = f"list={earlier} reason=EACCES: {os.strerror(errno.EACCES)}"
    assert all(message.endswith(reason) and "could not be removed" in message for message in failed)
    if fails == "once":
        assert len(failed) == 1 and len(removed) == 1
        _assert_no_list(instance.layout)
    else:
        assert len(failed) == 2 and not removed and earlier.read_bytes() == before
        assert all("a list still at that path is not this run's, so do not use it" in message for message in failed)

    pasted = _paste(earlier_command, tmp_path)

    assert pasted.returncode != 0 and pasted.stderr == f"{DELETE_REFUSED}\n" and not pasted.stdout
    assert stray.read_bytes() == STRAY, "the file the later run keeps is not deleted"


def test_only_files_0_9_13_wrote_are_moved(instance: _Instance) -> None:
    """Directly in the folder and named ``adapter_upload_<id>``: an operator's folder may hold other files."""

    gateway = instance.gateway()
    for attachment_id, path in {
        "att_nested": instance.transfer / "sub" / "adapter_upload_nested",
        "att_named": instance.transfer / "notes.png",
    }.items():
        data = f"{attachment_id} bytes".encode()
        _write(path, data)
        gateway.upsert_attachment(
            attachment_id=attachment_id,
            session_key="telegram:chat:7",
            conversation_id=CONVERSATION,
            correlation_id=None,
            filename="other.png",
            content_type="image/png",
            kind="image",
            size_bytes=len(data),
            sha256=hashlib.sha256(data).hexdigest(),
            local_path=str(path),
            owner_canonical_user_id=PERSON,
        )

    result = instance.run()

    assert result.finished and result.counts.moved == 1 and result.counts.refused == 0
    assert gateway.get_attachment("att_nested").local_path == str(instance.transfer / "sub" / "adapter_upload_nested")
    assert gateway.get_attachment("att_named").local_path == str(instance.transfer / "notes.png")
    assert (instance.transfer / "notes.png").exists() and (instance.transfer / "sub" / "adapter_upload_nested").exists()


def _fresh_config(tmp_path: Path, upload_dir: Path | None = None) -> SimpleNamespace:
    """A new instance's configuration: no records at all, and the transfer folder at ``upload_dir`` when given."""

    root = (tmp_path / "fresh").resolve()
    settings = resolve_storage_settings({"backend": "sqlite", "sqlite": {"path": str(tmp_path / "fresh.db")}})
    adapter_host = AdapterHostSettings(upload_dir=str(upload_dir)) if upload_dir else AdapterHostSettings()
    return SimpleNamespace(
        layout=InstanceLayout.at(root),
        instance_root=str(root),
        instance_root_path=root,
        interactive_apps=SimpleNamespace(adapter_host=adapter_host),
        state_storage_settings=settings,
        manager_storage_settings=settings,
    )


def test_a_new_instance_finishes_at_once(tmp_path: Path) -> None:
    config = _fresh_config(tmp_path)

    result = upgrade_adapter_host_files(config)

    assert result.finished and result.counts == upgrade_module.UpgradeCounts(files_without_records=0)
    assert config.layout.adapter_host_files_upgrade_path.is_file()
    _assert_no_list(config.layout)


@pytest.mark.skipif(os.geteuid() == 0, reason="permissions do not stop root")
@pytest.mark.parametrize("mode", [0, 0o444], ids=["unreadable", "readable_not_searchable"])
def test_a_transfer_folder_it_cannot_list_gives_a_warning_and_no_count(tmp_path: Path, caplog, mode) -> None:
    """A2: the count only reports, so the step finishes and writes its marker; it never gives a wrong number.

    A folder that can be read but not searched lists its names but cannot
    say what they are: that is a warning too, not a silent 0.
    """

    transfer = (tmp_path / "transfer").resolve()
    _write(transfer / STRAY_NAME, STRAY)
    config = _fresh_config(tmp_path, transfer)
    os.chmod(transfer, mode)
    try:
        with caplog.at_level(logging.INFO, logger=MODULE_LOGGER):
            result = upgrade_adapter_host_files(config)
    finally:
        os.chmod(transfer, 0o755)

    assert result.finished and result.counts == upgrade_module.UpgradeCounts()
    marker = config.layout.adapter_host_files_upgrade_path
    assert json.loads(marker.read_text())["files_without_records"] is None
    (summary,) = _events(caplog, "adapter_host_files_upgraded")
    assert summary.endswith("files_without_records=-"), "EVA-4"
    (uncounted,) = _events(caplog, "adapter_host_files_without_records_uncounted")
    assert f"folder={transfer} reason=" in uncounted and os.strerror(errno.EACCES) in uncounted
    # It says the failed count changed nothing, not the run, which may have moved files (C04).
    assert "counting them changed nothing and startup goes on" in uncounted
    assert not _events(caplog, "adapter_host_files_without_records")
    assert not _events(caplog, "adapter_host_files_folder_unusable")
    assert (transfer / STRAY_NAME).read_bytes() == STRAY
    _assert_no_list(config.layout)


# --- Stopping part way, and other file systems ------------------------------------------------------


class _Stop(RuntimeError):
    pass


@pytest.mark.parametrize("stop_at", ["before_conversations", "before_held_guidance", "before_gateway"])
def test_a_run_stopped_part_way_is_finished_by_the_next_run(instance: _Instance, monkeypatch, stop_at) -> None:
    _hold(instance, "guide_photo", _turn_record(instance, "att_photo"))
    if stop_at == "before_conversations":
        original = _RelationalConversationRepository.relocate_turn_attachment

        def stop(self, turns, attachment_id, *, local_path):
            if local_path is not None:
                raise _Stop("the disk filled up")
            return original(self, turns, attachment_id, local_path=local_path)

        monkeypatch.setattr(_RelationalConversationRepository, "relocate_turn_attachment", stop)
    else:
        # F2: held guidance is rewritten before the gateway record, which is what makes the next run find the file.
        method = "relocate_held_guidance_attachment" if stop_at == "before_held_guidance" else "set_attachment_local_path"
        monkeypatch.setattr(_RelationalGatewayStateRepository, method, lambda *_args: (_ for _ in ()).throw(_Stop()))

    with pytest.raises(_Stop):
        instance.run()

    assert instance.photo_destination.read_bytes() == CONTENT["att_photo"] and not instance.photo_source.exists()
    assert not instance.layout.adapter_host_files_upgrade_path.exists()
    monkeypatch.undo()

    result = instance.run()

    assert result.finished and (result.counts.moved, result.counts.already_moved) == (0, 1)
    assert instance.turn_paths("task_1")["att_photo"] == str(instance.photo_destination)
    assert instance.turn_paths("task_2")["att_photo"] == str(instance.photo_destination)
    assert instance.gateway().get_attachment("att_photo").local_path == str(instance.photo_destination)
    assert instance.photo_destination.read_bytes() == CONTENT["att_photo"]
    (held,) = TaskSteering.model_validate_json(_held_copy(instance, "guide_photo")).attachments
    assert held.local_path() == str(instance.photo_destination)


# --- The progress record: a run stopped part way, then another folder or none ----------------------------
#
# Each partial state comes from a real run stopped by an exception, so the progress record it leaves is the
# one a real stop leaves.


def _identity(path: Path) -> tuple[int, int]:
    info = path.stat()
    return (info.st_dev, info.st_ino)


def _recorded(instance: _Instance) -> tuple[int, int]:
    body = json.loads(instance.layout.adapter_host_files_upgrade_progress_path.read_text())
    assert body["transfer_folder"] == str(instance.transfer) and body["started_at_utc"]
    return (body["device"], body["inode"])


def _point_record_at(instance: _Instance, identity: tuple[int, int]) -> None:
    path = instance.layout.adapter_host_files_upgrade_progress_path
    body = json.loads(path.read_text())
    body["device"], body["inode"] = identity
    path.write_text(json.dumps(body))


def _stopped_after_the_rename(instance: _Instance, monkeypatch: pytest.MonkeyPatch) -> None:
    """A real run renamed the photo into state/uploads and stopped before rewriting its records."""

    original = _RelationalConversationRepository.relocate_turn_attachment

    def stop(self, turns, attachment_id, *, local_path):
        if local_path is not None:
            raise _Stop("stopped right after the rename")
        return original(self, turns, attachment_id, local_path=local_path)

    monkeypatch.setattr(_RelationalConversationRepository, "relocate_turn_attachment", stop)
    with pytest.raises(_Stop):
        instance.run()
    monkeypatch.undo()
    assert instance.photo_destination.read_bytes() == CONTENT["att_photo"] and not instance.photo_source.exists()
    _assert_photo_records_kept(instance)
    assert _recorded(instance) == _identity(instance.transfer), "the record names the folder the run started on"


def _throw(*_args: Any) -> None:
    raise _Stop("stopped while dropping records")


def _stopped_while_dropping(instance: _Instance, monkeypatch: pytest.MonkeyPatch, stop_in: str, **run: Any) -> None:
    """A real run stopped at the start of forget_unmovable, or in _forget between a turn and its gateway record."""

    if stop_in == "forget_unmovable":
        monkeypatch.setattr(upgrade_module._Step, "forget_unmovable", _throw)
    else:
        monkeypatch.setattr(_RelationalGatewayStateRepository, "delete_attachment", _throw)
    with pytest.raises(_Stop):
        instance.run(**run)
    monkeypatch.undo()


def _mount_back(instance: _Instance) -> None:
    if instance.transfer.exists():
        instance.transfer.rmdir()
    (instance.root / "unmounted").rename(instance.transfer)


@pytest.mark.parametrize("record_names", ["the_real_folder", "the_empty_folder"])
def test_after_a_partial_run_an_empty_mount_point_drops_nothing(instance: _Instance, monkeypatch, caplog, record_names) -> None:
    """FAIL-2: the moved photo at its destination is no sign the folder is here; only its identity is.

    With the record pointed at the empty folder (``the_empty_folder``) the step takes the empty folder for the
    real one and drops the records of files that still exist: the identity comparison is what protects them.
    """

    _stopped_after_the_rename(instance, monkeypatch)
    real = _identity(instance.transfer)
    instance.transfer.rename(instance.root / "unmounted")
    instance.transfer.mkdir()  # the volume is not mounted: an empty folder in its place
    empty = _identity(instance.transfer)
    assert empty != real
    if record_names == "the_empty_folder":
        _point_record_at(instance, empty)
    before = _untouched(instance)

    with caplog.at_level(logging.ERROR, logger=MODULE_LOGGER):
        result = instance.run()

    if record_names == "the_empty_folder":
        assert result.finished and "att_orphan" not in instance.files(), "the orphan's file still exists elsewhere"
        return
    assert not result.finished and result.folder_missing and result.waiting == 3
    assert _untouched(instance) == before and not instance.layout.adapter_host_files_upgrade_path.exists()
    assert _recorded(instance) == real, "a waiting run changes nothing, the record included"
    (error,) = _events(caplog, "adapter_host_files_folder_missing")
    assert "--files-gone" in error and "probably another folder on the same disk" in error, "C12: same device, another inode"
    assert "The device number changed" not in error, "C08: no remount advice when only the inode differs"
    assert error.endswith("recorded=device=%s,inode=%s current=device=%s,inode=%s" % (*real, *empty))

    _mount_back(instance)
    finished = instance.run()

    assert finished.finished and (finished.counts.already_moved, finished.counts.missing) == (1, 1)
    assert instance.turn_paths("task_1")["att_photo"] == str(instance.photo_destination)
    assert instance.gateway().get_attachment("att_photo").local_path == str(instance.photo_destination)
    assert instance.files()["att_orphan"] == str(instance.transfer / "adapter_upload_orphan"), "its file is here: kept"


def test_after_a_partial_run_a_new_device_number_gets_the_remount_advice(instance: _Instance, monkeypatch, caplog) -> None:
    """C08: only a changed device number is explained by a remount, so only then is --files-gone called safe.

    The test's empty folder is on the same file system, so its device number is shifted to stand for a remount.
    """

    _stopped_after_the_rename(instance, monkeypatch)
    real = _recorded(instance)
    _empty_in_its_place(instance)  # holds none of the waiting files
    read_identity = upgrade_module._Step._folder_identity

    def remounted(self, *, absent_is_missing):
        identity = read_identity(self, absent_is_missing=absent_is_missing)
        return None if identity is None else (identity[0] + 1, identity[1])

    monkeypatch.setattr(upgrade_module._Step, "_folder_identity", remounted)
    before = _untouched(instance)
    with caplog.at_level(logging.ERROR, logger=MODULE_LOGGER):
        result = instance.run()

    assert result.folder_missing and result.waiting == 3 and _untouched(instance) == before
    assert _recorded(instance) == real
    (error,) = _events(caplog, "adapter_host_files_folder_missing")
    assert "The device number changed" in error and "--files-gone finishes the step safely" in error
    assert "another folder on the same disk" not in error


def test_after_a_partial_run_a_missing_folder_waits_until_the_files_are_declared_gone(
    instance: _Instance, monkeypatch, caplog
) -> None:
    """EVI-1: the step waits as the docs say, naming --files-gone, and --files-gone finishes without a record."""

    _stopped_after_the_rename(instance, monkeypatch)
    recorded = _recorded(instance)
    instance.transfer.rename(instance.root / "unmounted")
    before = _untouched(instance)

    with caplog.at_level(logging.ERROR, logger=MODULE_LOGGER):
        waiting = instance.run()

    assert waiting.folder_missing and waiting.waiting == 3 and _untouched(instance) == before
    (error,) = _events(caplog, "adapter_host_files_folder_missing")
    assert "--files-gone" in error and "it is now not here" in error and "mounted again" not in error
    assert error.endswith("recorded=device=%s,inode=%s current=-" % recorded)

    real_write = upgrade_module._write_state_file

    def no_progress_record(path, body):
        assert path != instance.layout.adapter_host_files_upgrade_progress_path, "--files-gone needs no record"
        return real_write(path, body)

    monkeypatch.setattr(upgrade_module, "_write_state_file", no_progress_record)
    gone = instance.run(files_gone=True)
    monkeypatch.undo()

    assert gone.finished and (gone.counts.already_moved, gone.counts.missing, gone.counts.no_record) == (1, 1, 1)
    assert instance.turn_paths("task_1")["att_photo"] == str(instance.photo_destination)
    assert instance.gateway().get_attachment("att_photo").local_path == str(instance.photo_destination)
    assert set(instance.files()) == {"att_photo", "att_web"} and instance.gateway().get_attachment("att_lost") is None
    assert _recorded(instance) == recorded and instance.layout.adapter_host_files_upgrade_path.is_file()


def test_a_files_gone_run_stopped_while_dropping_is_finished_by_another(instance: _Instance, monkeypatch, caplog) -> None:
    _stopped_after_the_rename(instance, monkeypatch)
    instance.transfer.rename(instance.root / "unmounted")
    _stopped_while_dropping(instance, monkeypatch, "delete_attachment", files_gone=True)
    assert instance.gateway().get_attachment("att_photo").local_path == str(instance.photo_destination)
    assert "att_lost" not in instance.turn_paths("task_1") and instance.gateway().get_attachment("att_lost") is not None

    with caplog.at_level(logging.ERROR, logger=MODULE_LOGGER):
        waiting = instance.run()

    assert waiting.folder_missing and waiting.waiting == 2  # att_lost's gateway record, the orphan's entry
    (error,) = _events(caplog, "adapter_host_files_folder_missing")
    assert "--files-gone" in error
    gone = instance.run(files_gone=True)
    assert gone.finished and instance.gateway().get_attachment("att_lost") is None and "att_orphan" not in instance.files()


@pytest.mark.parametrize("stop_in", ["forget_unmovable", "delete_attachment"])
def test_a_run_stopped_while_dropping_records_is_finished_by_the_next_start(instance: _Instance, monkeypatch, stop_in) -> None:
    """FAIL-1: once the photo moved only absent files wait, so only the recorded identity says the folder is here."""

    (instance.transfer / "adapter_upload_orphan").unlink()
    _stopped_while_dropping(instance, monkeypatch, stop_in)
    assert _recorded(instance) == _identity(instance.transfer)
    assert instance.gateway().get_attachment("att_photo").local_path == str(instance.photo_destination)
    if stop_in == "delete_attachment":
        assert "att_lost" not in instance.turn_paths("task_1"), "its turn entry was dropped"
        assert instance.gateway().get_attachment("att_lost") is not None, "its gateway record was not yet"

    result = instance.run()

    assert result.finished and (result.counts.missing, result.counts.no_record) == (1, 1)
    assert instance.gateway().get_attachment("att_lost") is None
    assert set(instance.files()) == {"att_photo", "att_web"} and instance.recorded("task_1") == 0
    assert instance.layout.adapter_host_files_upgrade_path.is_file()


def test_a_copied_folder_holding_a_waiting_file_is_here_and_the_record_names_it(instance: _Instance, monkeypatch) -> None:
    _stopped_after_the_rename(instance, monkeypatch)
    old = _recorded(instance)
    shutil.copytree(instance.transfer, instance.root / "copied")
    instance.transfer.rename(instance.root / "old-disk")
    (instance.root / "copied").rename(instance.transfer)
    assert _identity(instance.transfer) != old

    result = instance.run()

    assert result.finished and result.counts.already_moved == 1
    assert instance.files()["att_orphan"] == str(instance.transfer / "adapter_upload_orphan")
    assert _recorded(instance) == _identity(instance.transfer), "the record was rewritten before the first change"


@pytest.mark.parametrize(
    "body", [b"not json", b"[]", b'{"device": "1", "inode": 2}', b"\xff"], ids=["text", "list", "string", "bytes"]
)
def test_a_progress_record_that_does_not_parse_is_ignored_and_the_step_waits(instance: _Instance, monkeypatch, caplog, body) -> None:
    (instance.transfer / "adapter_upload_orphan").unlink()
    _stopped_while_dropping(instance, monkeypatch, "forget_unmovable")
    instance.layout.adapter_host_files_upgrade_progress_path.write_bytes(body)

    with caplog.at_level(logging.WARNING, logger=MODULE_LOGGER):
        result = instance.run()

    assert result.folder_missing and instance.gateway().get_attachment("att_lost") is not None
    assert len(_events(caplog, "adapter_host_files_progress_unreadable")) == 1
    assert instance.run(files_gone=True).finished and instance.gateway().get_attachment("att_lost") is None


def _vanish(instance: _Instance) -> None:
    instance.transfer.rename(instance.root / "unmounted")


def _empty_in_its_place(instance: _Instance) -> None:
    _vanish(instance)
    instance.transfer.mkdir()


@pytest.mark.parametrize(
    ("change", "before"),
    [
        (_vanish, "handle"),
        (_vanish, "forget_unmovable"),
        (_empty_in_its_place, "handle"),
        (_empty_in_its_place, "forget_unmovable"),
    ],
    ids=["vanished_before_the_moves", "vanished_before_the_drops", "emptied_before_the_moves", "emptied_before_the_drops"],
)
def test_a_transfer_folder_that_changes_during_the_run_drops_nothing(
    instance: _Instance, monkeypatch, caplog, change, before
) -> None:
    """U14 and C06: every look opens the folder by path, so it is checked again before anything is dropped.

    Only a folder that is no longer there names --files-gone; one replaced by another folder does not.
    """

    real = getattr(upgrade_module._Step, before)
    changed: list[bool] = []

    def change_first(self, *args):
        if not changed:
            changed.append(True)
            change(instance)
        return real(self, *args)

    monkeypatch.setattr(upgrade_module._Step, before, change_first)
    with caplog.at_level(logging.ERROR, logger=MODULE_LOGGER):
        with pytest.raises(upgrade_module.FolderProblem) as raised:
            instance.run()
    monkeypatch.undo()

    problem = raised.value
    assert problem.folder == instance.transfer
    assert ("--files-gone" in str(problem)) == (change is _vanish)
    assert instance.gateway().get_attachment("att_lost") is not None and instance.recorded("task_1") == 1, "nothing dropped"
    assert "att_orphan" in instance.files() and not instance.layout.adapter_host_files_upgrade_path.exists()
    _mount_back(instance)
    result = instance.run()
    assert result.finished and instance.gateway().get_attachment("att_lost") is None
    assert instance.gateway().get_attachment("att_photo").local_path == str(instance.photo_destination)


@pytest.mark.parametrize("change", [_vanish, _empty_in_its_place], ids=["vanished", "empty_in_its_place"])
def test_a_folder_changed_after_the_moves_is_not_counted(instance: _Instance, monkeypatch, caplog, change) -> None:
    """F4 of PR #150's review (FA-3): a folder gone or swapped since ``survey`` would look empty; no count, not 0."""

    _write(instance.transfer / STRAY_NAME, STRAY)
    real = upgrade_module._Step.files_without_records

    def change_first(self):
        change(instance)
        return real(self)

    monkeypatch.setattr(upgrade_module._Step, "files_without_records", change_first)
    with caplog.at_level(logging.WARNING, logger=MODULE_LOGGER):
        result = instance.run()

    assert result.finished and result.counts.files_without_records is None and result.counts.moved == 1
    assert json.loads(instance.layout.adapter_host_files_upgrade_path.read_text())["files_without_records"] is None
    (uncounted,) = _events(caplog, "adapter_host_files_without_records_uncounted")
    assert f"folder={instance.transfer} reason=folder_changed: it changed during the run: device=" in uncounted
    assert not _events(caplog, "adapter_host_files_without_records")
    _assert_no_list(instance.layout)


@pytest.mark.skipif(os.geteuid() == 0, reason="permissions do not stop root")
def test_files_gone_is_not_advised_for_a_folder_it_cannot_read(instance: _Instance) -> None:
    """--files-gone only turns a folder that is not there into proven absence; it cannot get past EACCES."""

    os.chmod(instance.transfer, 0)
    try:
        for files_gone in (False, True):
            with pytest.raises(upgrade_module.FolderProblem) as raised:
                instance.run(files_gone=files_gone)
            assert raised.value.folder == instance.transfer and "--files-gone" not in str(raised.value)
    finally:
        os.chmod(instance.transfer, 0o755)


def test_across_file_systems_it_copies_and_removes_the_original_after_both_records_change(instance: _Instance, monkeypatch) -> None:
    real_rename = os.rename

    def rename(source, destination, *, src_dir_fd=None, dst_dir_fd=None):
        if src_dir_fd is not None and dst_dir_fd is not None and src_dir_fd != dst_dir_fd:
            raise OSError(errno.EXDEV, "Invalid cross-device link")
        return real_rename(source, destination, src_dir_fd=src_dir_fd, dst_dir_fd=dst_dir_fd)

    original_set = _RelationalGatewayStateRepository.set_attachment_local_path
    seen: list[bool] = []

    def set_path(self, attachment_id, local_path):
        seen.append(instance.photo_source.exists())
        return original_set(self, attachment_id, local_path)

    monkeypatch.setattr(upgrade_module.os, "rename", rename)
    monkeypatch.setattr(_RelationalGatewayStateRepository, "set_attachment_local_path", set_path)

    result = instance.run()

    assert result.counts.moved == 1 and seen == [True], "the original stays until both records name the copy"
    destination = instance.photo_destination
    assert destination.read_bytes() == CONTENT["att_photo"] and not instance.photo_source.exists()
    assert int(destination.stat().st_mtime) == SENT_AT and (destination.stat().st_mode & 0o777) == 0o640
    assert not list(destination.parent.glob(".*")), "no temporary copy is left"


def _across_file_systems(monkeypatch: pytest.MonkeyPatch) -> None:
    real_rename = os.rename

    def rename(source, destination, *, src_dir_fd=None, dst_dir_fd=None):
        if src_dir_fd is not None and dst_dir_fd is not None and src_dir_fd != dst_dir_fd:
            raise OSError(errno.EXDEV, "Invalid cross-device link")
        return real_rename(source, destination, src_dir_fd=src_dir_fd, dst_dir_fd=dst_dir_fd)

    monkeypatch.setattr(upgrade_module.os, "rename", rename)


def test_a_short_write_across_file_systems_is_finished(instance: _Instance, monkeypatch) -> None:
    _across_file_systems(monkeypatch)
    real_write = os.write
    shortened: list[int] = []

    def write(descriptor, data):
        if not shortened:
            shortened.append(len(data) // 2)
            return real_write(descriptor, bytes(data)[: len(data) // 2])
        return real_write(descriptor, data)

    monkeypatch.setattr(upgrade_module.os, "write", write)

    result = instance.run()

    assert shortened and result.counts.moved == 1
    assert instance.photo_destination.read_bytes() == CONTENT["att_photo"] and not instance.photo_source.exists()


def test_a_copy_that_reads_back_differently_keeps_the_original_and_its_records(instance: _Instance, monkeypatch, caplog) -> None:
    _across_file_systems(monkeypatch)
    real_write = os.write
    monkeypatch.setattr(upgrade_module.os, "write", lambda descriptor, data: real_write(descriptor, b"x" * len(data)))

    with caplog.at_level(logging.WARNING, logger=MODULE_LOGGER):
        result = instance.run()

    assert result.finished and result.counts.refused == 1
    _assert_photo_kept(instance)
    assert not instance.photo_destination.exists() and not list(instance.photo_destination.parent.glob(".*"))
    (warning,) = [message for message in _events(caplog, "adapter_host_file_not_moved") if message.endswith("records=kept reason=copy_mismatch")]
    assert f" sources={instance.photo_source} folder=" in warning, "C05: file= names the temporary copy, sources= the original"
    monkeypatch.undo()
    assert _rerun_after_fix(instance).moved == 1


@pytest.mark.parametrize("error", [errno.EIO, errno.EINVAL], ids=["fails", "not_offered"])
def test_a_folder_sync_after_the_copy_is_in_place_never_drops_the_copied_files_records(
    instance: _Instance, monkeypatch, caplog, error
) -> None:
    """Once the copy is renamed into place, a folder sync that fails stops startup with every record kept, and the
    next run counts the file already moved; a sync the file system does not offer (EINVAL) is logged and the step
    goes on (C17)."""

    _across_file_systems(monkeypatch)
    real_fsync = os.fsync

    def fsync(descriptor):
        if Path(os.readlink(f"/proc/self/fd/{descriptor}")) == instance.photo_destination.parent:
            raise OSError(error, os.strerror(error))
        return real_fsync(descriptor)

    monkeypatch.setattr(upgrade_module.os, "fsync", fsync)
    with caplog.at_level(logging.WARNING, logger=MODULE_LOGGER):
        if error == errno.EIO:
            with pytest.raises(upgrade_module.FolderProblem) as raised:
                instance.run()
        else:
            result = instance.run()
    monkeypatch.undo()

    if error == errno.EIO:
        problem = raised.value
        assert (problem.folder, problem.file, problem.attachment_id) == (
            instance.layout.uploads_dir.resolve(), instance.photo_destination, "att_photo"
        )
        assert instance.gateway().get_attachment("att_photo").local_path == str(instance.photo_source)
        assert instance.turn_paths("task_1")["att_photo"] == str(instance.photo_source)
        assert instance.gateway().get_attachment("att_lost") is not None, "nothing was dropped"
        assert not instance.layout.adapter_host_files_upgrade_path.exists()
        result = instance.run()
        assert result.counts.already_moved == 1
    else:
        assert result.counts.moved == 1
        (note,) = _events(caplog, "adapter_host_files_sync_unsupported")
        assert f"folder={instance.layout.uploads_dir.resolve()}" in note
    assert instance.gateway().get_attachment("att_photo").local_path == str(instance.photo_destination)
    assert instance.photo_destination.read_bytes() == CONTENT["att_photo"] and not instance.photo_source.exists()


def test_the_operator_command_stops_with_the_folder_named(monkeypatch, capsys) -> None:
    import assistant_cli

    folder = Path("/srv/transfer")
    monkeypatch.setattr(assistant_cli.GatewayInstanceConfig, "load_for_process", staticmethod(lambda _path: object()))

    def refuse(_config, *, files_gone):
        raise upgrade_module.FolderProblem(folder, "root_unavailable", "Permission denied")

    monkeypatch.setattr(upgrade_module, "upgrade_adapter_host_files", refuse)

    with pytest.raises(SystemExit) as raised:
        assistant_cli._upgrade_adapter_host_files(SimpleNamespace(config="instance_config.yaml", files_gone=False))

    assert str(folder) in str(raised.value.code) and "restart" in str(raised.value.code)
    assert "--files-gone" not in str(raised.value.code)


def test_the_operator_command_names_files_gone_where_it_helps(monkeypatch, capsys) -> None:
    import assistant_cli

    monkeypatch.setattr(assistant_cli.GatewayInstanceConfig, "load_for_process", staticmethod(lambda _path: object()))
    problem = upgrade_module.FolderProblem(Path("/srv/transfer"), "root_unavailable", "No such file", not_there=True)
    problem.files_gone_helps = True
    waiting = upgrade_module.UpgradeResult(finished=False, counts=upgrade_module.UpgradeCounts(), waiting=2)
    outcomes: list[Any] = [problem, waiting]

    def run(_config, *, files_gone):
        outcome = outcomes.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome

    monkeypatch.setattr(upgrade_module, "upgrade_adapter_host_files", run)
    args = SimpleNamespace(config="instance_config.yaml", files_gone=False)
    with pytest.raises(SystemExit) as raised:
        assistant_cli._upgrade_adapter_host_files(args)
    assert "--files-gone" in str(raised.value.code)

    assistant_cli._upgrade_adapter_host_files(args)

    printed = capsys.readouterr().out
    assert "2 file(s) wait" in printed and "not the folder an earlier run started on" in printed
    assert "--files-gone" in printed


# --- After the step ------------------------------------------------------------------------------


def test_upload_retention_and_a_persons_removal_find_the_moved_file(instance: _Instance) -> None:
    instance.run()
    uploads = str(instance.layout.uploads_dir.resolve())
    moved = str(instance.photo_destination)

    assert instance.gateway().upload_bytes_by_owner(uploads_dir=uploads)[PERSON] == len(CONTENT["att_photo"]) + len(CONTENT["att_web"])
    removed = instance.gateway().forget_person_records(PERSON)
    assert moved in removed.attachment_paths


def test_upload_retention_deletes_the_moved_file_by_when_it_was_sent(instance: _Instance) -> None:
    instance.run()

    deleted = instance.gateway().delete_uploads_created_before(
        "9999-01-01T00:00:00+00:00", uploads_dir=str(instance.layout.uploads_dir.resolve()), limit=10
    )

    assert str(instance.photo_destination) in deleted


# --- The operator command ------------------------------------------------------------------------


def test_the_operator_command_runs_the_step_on_a_saved_instance(tmp_path: Path) -> None:
    from assistant_gateway.config import GatewayInstanceConfig

    config = GatewayInstanceConfig.create(
        instance_name="upgradetest", instance_root=str(tmp_path / "instance"), team_id="team_test", timezone="UTC"
    )
    config_path = config.save()
    root = config.instance_root_path.resolve()
    sections = _sections(root)
    for settings in (config.state_storage_settings, config.manager_storage_settings):
        Path(settings.sqlite_path).parent.mkdir(parents=True, exist_ok=True)
    _load(lambda: connect_sqlite(config.state_storage_settings.sqlite_path), sections["gateway"])
    _load(lambda: connect_sqlite(config.manager_storage_settings.sqlite_path), sections["tasks"])
    _write(root / "runtime" / "adapter-host-uploads" / "adapter_upload_photo", CONTENT["att_photo"])
    _write(root / "runtime" / "adapter-host-uploads" / STRAY_NAME, STRAY)
    env = {key: value for key, value in os.environ.items() if not key.startswith(("NATS_", "POSTGRES_"))}

    completed = subprocess.run(
        [sys.executable, "-B", str(REPO_ROOT / "assistant_cli.py"), "upgrade", "adapter-host-files", "--config", str(config_path)],
        cwd=REPO_ROOT,
        env=env,
        capture_output=True,
        text=True,
        timeout=120,
    )

    assert completed.returncode == 0, completed.stderr
    assert "Moved 1 file(s) into state/uploads; 1 could not be moved" in completed.stdout
    assert "1 file(s) have no gateway record" in completed.stdout and "trying again does not help" in completed.stdout
    assert "event=adapter_host_files_upgraded moved=1" in completed.stderr
    assert "1 file(s) are left in the transfer folder that no record names" in completed.stdout
    assert "event=adapter_host_files_without_records " in completed.stderr
    assert f"one name per line): {STRAY_NAME}. If this folder was at another path under 0.9.13" in completed.stderr
    assert "do not delete them; move or mount the folder back" in completed.stderr
    assert "Otherwise an administrator may delete exactly the listed files" in completed.stderr
    assert (config.layout.uploads_dir / "telegram_chat_7" / "att_photo.png").read_bytes() == CONTENT["att_photo"]
    assert config.layout.adapter_host_files_upgrade_path.is_file()
    _assert_listed(config.layout, [STRAY_NAME])


def test_the_operator_command_says_when_the_leftover_files_could_not_be_counted(monkeypatch, capsys) -> None:
    import assistant_cli

    monkeypatch.setattr(assistant_cli.GatewayInstanceConfig, "load_for_process", staticmethod(lambda _path: object()))
    outcomes = [
        upgrade_module.UpgradeResult(finished=True, counts=upgrade_module.UpgradeCounts()),
        upgrade_module.UpgradeResult(finished=True, counts=upgrade_module.UpgradeCounts(files_without_records=0)),
        upgrade_module.UpgradeResult(finished=True, counts=upgrade_module.UpgradeCounts(files_without_records=2)),
        upgrade_module.UpgradeResult(finished=True, counts=upgrade_module.UpgradeCounts(), other_folders_unchecked=2),
    ]
    monkeypatch.setattr(upgrade_module, "upgrade_adapter_host_files", lambda _config, *, files_gone: outcomes.pop(0))
    args = argparse.Namespace(config="instance_config.yaml", files_gone=False)

    assistant_cli._upgrade_adapter_host_files(args)
    assert "could not be counted; see the log" in capsys.readouterr().out
    assistant_cli._upgrade_adapter_host_files(args)
    printed = capsys.readouterr().out
    assert "transfer folder" not in printed, "a count of 0 adds no line"
    assistant_cli._upgrade_adapter_host_files(args)
    printed = capsys.readouterr().out
    assert "2 file(s) are left in the transfer folder that no record names; the team cannot open them." in printed
    assert "never opens" not in printed, "0.10 reads the files of messages it had not handled (PR #150's review)"
    assistant_cli._upgrade_adapter_host_files(args)
    printed = capsys.readouterr().out
    assert "could not be counted" not in printed, "GEN-1 and FA-4 of PR #168's review: they were counted"
    assert (
        "2 file(s) are left in the transfer folder that no record of it names. They were counted, but the check for "
        "records naming them in another folder did not finish, so nothing is listed and no delete command is given: "
        "do not delete any of them. To check again, delete the marker and restart; see the log." in printed
    )


def test_the_operator_command_gives_the_steps_only_for_files_a_gateway_record_names(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """C05, C08: after a run that found records in another folder, the line replaces the delete advice and splits like the error."""

    import assistant_cli

    monkeypatch.setattr("assistant_cli.GatewayInstanceConfig.load_for_process", staticmethod(lambda _path: object()))
    counts = upgrade_module.UpgradeCounts(files_without_records=3)
    outcomes = [
        upgrade_module.UpgradeResult(finished=True, counts=counts, elsewhere=NamedElsewhere(names=1, by_gateway=1)),
        upgrade_module.UpgradeResult(finished=True, counts=counts, elsewhere=NamedElsewhere(names=2, by_gateway=1, by_turns=1)),
        upgrade_module.UpgradeResult(finished=True, counts=counts, elsewhere=NamedElsewhere(names=1, by_held_guidance=1)),
        upgrade_module.UpgradeResult(
            finished=True, counts=counts,
            elsewhere=NamedElsewhere(names=2, by_gateway=2, gateway_folders=(Path("/old-a"), Path("/old-b"))),
        ),
    ]
    monkeypatch.setattr(upgrade_module, "upgrade_adapter_host_files", lambda _config, *, files_gone: outcomes.pop(0))
    args = argparse.Namespace(config="instance_config.yaml", files_gone=False)
    printed = []
    for _ in range(4):
        assistant_cli._upgrade_adapter_host_files(args)
        printed.append(capsys.readouterr().out)

    for text in printed:
        assert "3 file(s) are left in the transfer folder that no record of it names, and records in another folder" in text
        assert "do not delete any of them" in text and "See event=adapter_host_files_folder_moved in the log." in text
        assert "which may be deleted" not in text
    steps = (
        "To move the 1 the gateway has a record of receiving: move or mount the folder back at the path the log's "
        "steps name"
    )
    assert steps in printed[0] and "no record of receiving" not in printed[0] and "for each" not in printed[0]
    assert steps in printed[1]
    assert "For the other 1 the gateway has no record of receiving them, so they cannot be moved: keep them" in printed[1]
    assert "The gateway has no record of receiving them, so they cannot be moved: keep them" in printed[2]
    assert "move or mount" not in printed[2]
    assert (
        "To move the 2 the gateway has a record of receiving: move or mount the folder back at one of the 2 paths the "
        "log's steps name (a link does not work), set upload_dir to that path, delete the marker and restart. That run moves only "
        "the files whose gateway records name that path, so do this for each path." in printed[3]
    ), "C08: one move back is not enough with two folders"
    assert "old-a" not in printed[3], "the log names the folders"


def test_the_moved_folder_texts_split_records_of_receiving_in_this_instance_s_folders_from_the_rest(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """C11 of PR #168's fix round: with every kind at once, each group gets its own true sentence, in the error and the operator line."""

    import assistant_cli

    elsewhere = NamedElsewhere(names=3, by_gateway=1, by_instance_folders=1, by_turns=1, gateway_folders=(Path("/old"),))
    advice = upgrade_module._folder_moved_advice(Path("/m.json"), elsewhere)
    assert advice.startswith(" To move the 1 the gateway has a record of receiving: move or mount this folder back at /old")
    assert (
        " 1 of them have gateway records of receiving only inside this instance's workspaces or state folder, which "
        "the step never shows or offers: keep them;" in advice
    )
    assert advice.endswith(
        " For the other 1 the gateway has no record of receiving them, so no run can move them: keep them, and the "
        "people who sent them can send them again."
    )
    monkeypatch.setattr("assistant_cli.GatewayInstanceConfig.load_for_process", staticmethod(lambda _path: object()))
    result = upgrade_module.UpgradeResult(
        finished=True, counts=upgrade_module.UpgradeCounts(files_without_records=3), elsewhere=elsewhere
    )
    monkeypatch.setattr(upgrade_module, "upgrade_adapter_host_files", lambda _config, *, files_gone: result)
    assistant_cli._upgrade_adapter_host_files(argparse.Namespace(config="instance_config.yaml", files_gone=False))
    printed = capsys.readouterr().out
    assert "most likely at another path under 0.9.13" in printed
    assert "To move the 1 the gateway has a record of receiving: move or mount the folder back at the path" in printed
    assert "1 of them have gateway records of receiving only inside this instance's workspaces or state folder" in printed
    assert "For the other 1 the gateway has no record of receiving them, so they cannot be moved: keep them" in printed


def test_the_fixture_matches_the_bytes_it_was_written_for() -> None:
    text = FIXTURE.read_text()
    for attachment_id, data in CONTENT.items():
        assert hashlib.sha256(data).hexdigest() in text, attachment_id
