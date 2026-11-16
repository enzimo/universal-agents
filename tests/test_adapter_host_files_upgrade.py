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
long or with a NUL byte; ``--files-gone`` then finishes), a folder it cannot
use (startup stops naming the folder and nothing changes), one error rule
checked by injecting an error at each file and folder operation in turn
(``test_one_error_rule_holds_at_every_operation``), a run stopped part way
(the next run finishes), a move across file systems (short writes, a folder
sync that fails or is not offered after the copy is in place), upload
retention and a person's removal finding the moved file, the launcher step
and the operator command. A held request still naming the old path gets
the stuck-task notice: ``tests/test_stuck_tasks.py`` (``ADAPTER_HOST_FOLDER``).
Runs on SQLite and, with ``LIVE_TEST_POSTGRES_DSN``, on PostgreSQL.
"""

from __future__ import annotations

import errno
import hashlib
import json
import logging
import os
import subprocess
import sys
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from uuid import uuid4

import pytest

import instance_upgrades.adapter_host_files as upgrade_module
from instance_upgrades.adapter_host_files import upgrade_adapter_host_files
from custom_agents.conversation_attachments import (
    UNRECORDED_TURNS_NOTE,
    LocatedUpload,
    TaskAttachments,
    UploadFolder,
    inventory_text,
    locate_upload,
)
from custom_agents.conversation_store import ConversationStore
from custom_messages.contracts.attachments_v1 import AttachmentStorageRef, UserTaskAttachment
from custom_messages.contracts.task_v1 import Task
from custom_messages.enums import MessageMetaType
from runtime_settings.instance_layout import InstanceLayout
from runtime_settings.interactive_apps import AdapterHostSettings
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
    root = (tmp_path / "instance").resolve()
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
    return [record.getMessage() for record in caplog.records if f"event={event}" in record.getMessage()]


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
        "moved=1 already_moved=0 missing=1 refused=0 no_record=1 gateway_rows_removed=1 turns_marked_unrecorded=1"
    )
    assert [record.levelno for record in caplog.records if "adapter_host_files_upgraded" in record.getMessage()] == [logging.WARNING]
    marker = json.loads(instance.layout.adapter_host_files_upgrade_path.read_text())
    assert (marker["moved"], marker["missing"], marker["no_record"]) == (1, 1, 1) and marker["finished_at_utc"]


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

    source = str(instance.photo_source)
    assert instance.gateway().get_attachment("att_photo").local_path == source
    assert instance.turn_paths("task_1")["att_photo"] == source and instance.turn_paths("task_2")["att_photo"] == source
    assert instance.photo_source.read_bytes() == CONTENT["att_photo"]


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
    assert instance.files()[attachment_id] == str(instance.photo_source), "its conversation entry is kept"
    assert instance.gateway().get_attachment(attachment_id).local_path == str(instance.photo_source)
    (entry,) = [item for item in instance.conversations().list_turn_attachments(CONVERSATION) if item.attachment.attachment_id == attachment_id][:1]
    _assert_not_opened(instance, entry.attachment)
    assert any(
        f"attachment_id={attachment_id} conversation_ids={CONVERSATION} " in message
        and message.endswith(f"records=kept reason={reason}")
        for message in _events(caplog, "adapter_host_file_not_moved")
    )
    assert os.path.lexists(instance.photo_source), "a refused file is left where it is"
    after = {path: path.read_bytes() for path in instance.root.rglob("*") if path.is_file() and not path.is_symlink() and "uploads" in path.parts}
    assert after == before, "nothing in state/uploads was written or replaced"


@pytest.mark.parametrize("other_there", [True, False], ids=["other_file_here", "other_file_absent"])
def test_a_conversation_entry_naming_another_path_does_not_follow_the_moved_file(instance: _Instance, caplog, other_there) -> None:
    """Agent processes write conversation turns, so an entry is moved only when it names the gateway's file.

    It is dropped only when the file it names is proven absent (rule 3).
    """

    if other_there:
        _write(instance.transfer / "adapter_upload_other", b"some other upload")
    record = instance.gateway().get_attachment("att_photo")
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
        attachments=[_record("att_photo", str(instance.transfer / "adapter_upload_other"), record.sha256)],
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
    assert any(message.endswith(f"records={records} reason=record_mismatch") for message in _events(caplog, "adapter_host_file_not_moved"))


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
    assert "--files-gone" in error and error.endswith(f"folder={instance.transfer} waiting=3")

    gone = instance.run(files_gone=True)

    assert gone.finished and (gone.counts.missing, gone.counts.no_record, gone.counts.moved) == (2, 1, 0)
    assert set(instance.files()) == {"att_web"}
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
    "rename_copy_bad_name": _Fault("rename", _copy, errno.EINVAL, "refused", "name_refused", cross_disk=True),
    "open_destination_io": _Fault(
        "open", _DESTINATION, errno.EIO, "refused", "unreadable", setup=_photo_already_at_destination
    ),
    "path_construction": _Fault("path", _PHOTO, ValueError("no file name"), "refused", "unsafe_name"),
    # Rule 2: a folder.
    "open_transfer_folder": _Fault("open", _TRANSFER, errno.EACCES, "folder", "transfer"),
    "stat_in_unsearchable_folder": _Fault("stat", _ORPHAN, errno.EACCES, "folder", "transfer"),
    "open_file_in_unsearchable_folder": _Fault(
        "open", _PHOTO, errno.EACCES, "folder", "transfer", also=(("stat", _PHOTO, errno.EACCES),)
    ),
    "mkdir_session_read_only": _Fault("mkdir", _SESSION, errno.EROFS, "folder", "uploads"),
    "rename_read_only": _Fault("rename", _PHOTO, errno.EROFS, "folder", "uploads"),
    "rename_destination_folder_gone": _Fault("rename", _PHOTO, errno.ENOENT, "folder", "uploads"),
    "write_copy_full": _Fault("write", _copy, errno.ENOSPC, "folder", "uploads", cross_disk=True),
    "fsync_folder_after_rename": _Fault("fsync", _SESSION, errno.EIO, "folder", "uploads"),
    "fsync_folder_after_copy": _Fault("fsync", _SESSION, errno.EIO, "folder", "uploads", cross_disk=True),
    "write_marker": _Fault("replace", _MARKER, errno.EROFS, "folder", "state"),
    # Rule 3: proven absent.
    "rename_file_vanished": _Fault("rename", _PHOTO, _vanish_then(errno.ENOENT), "missing"),
    # After the file and its records are safe, or a sync the file system does not offer.
    "stat_name_too_long_is_no_sign": _Fault("stat", _ORPHAN, errno.ENAMETOOLONG, "moved"),
    "unlink_source_after_copy": _Fault("unlink", _PHOTO, errno.EACCES, "moved", cross_disk=True),
    "fsync_folder_not_offered": _Fault("fsync", _SESSION, errno.EINVAL, "moved"),
    "fsync_copy_not_offered": _Fault("fsync", _copy, errno.EINVAL, "moved", cross_disk=True),
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
    marker = instance.layout.adapter_host_files_upgrade_path

    if fault.expect == "refused":
        assert result.finished and result.counts.refused == 1 and result.counts.moved == 0
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
        if fault.reason != "state":
            gateway = instance.gateway()
            assert gateway.get_attachment("att_photo").local_path == str(instance.photo_source), "nothing finished"
            assert gateway.get_attachment("att_lost") is not None, "nothing was dropped"
            assert instance.files() == before[0] and instance.recorded("task_1") == before[1]
        result = instance.run()
        assert result.finished and result.counts.moved + result.counts.already_moved <= 1
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


def test_a_new_instance_finishes_at_once(tmp_path: Path) -> None:
    root = (tmp_path / "fresh").resolve()
    layout = InstanceLayout.at(root)
    settings = resolve_storage_settings({"backend": "sqlite", "sqlite": {"path": str(tmp_path / "fresh.db")}})
    config = SimpleNamespace(
        layout=layout,
        instance_root_path=root,
        interactive_apps=SimpleNamespace(adapter_host=AdapterHostSettings()),
        state_storage_settings=settings,
        manager_storage_settings=settings,
    )

    result = upgrade_adapter_host_files(config)

    assert result.finished and result.counts == upgrade_module.UpgradeCounts()
    assert layout.adapter_host_files_upgrade_path.is_file()


# --- Stopping part way, and other file systems ------------------------------------------------------


class _Stop(RuntimeError):
    pass


@pytest.mark.parametrize("stop_at", ["before_conversations", "before_gateway"])
def test_a_run_stopped_part_way_is_finished_by_the_next_run(instance: _Instance, monkeypatch, stop_at) -> None:
    if stop_at == "before_conversations":
        original = _RelationalConversationRepository.relocate_turn_attachment

        def stop(self, turns, attachment_id, *, local_path):
            if local_path is not None:
                raise _Stop("the disk filled up")
            return original(self, turns, attachment_id, local_path=local_path)

        monkeypatch.setattr(_RelationalConversationRepository, "relocate_turn_attachment", stop)
    else:
        monkeypatch.setattr(
            _RelationalGatewayStateRepository, "set_attachment_local_path", lambda *_args: (_ for _ in ()).throw(_Stop())
        )

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
    assert any(message.endswith("records=kept reason=copy_mismatch") for message in _events(caplog, "adapter_host_file_not_moved"))
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
    assert "Moved 1 file(s) into state/uploads; 2 could not be moved" in completed.stdout
    assert "event=adapter_host_files_upgraded moved=1" in completed.stderr
    assert (config.layout.uploads_dir / "telegram_chat_7" / "att_photo.png").read_bytes() == CONTENT["att_photo"]
    assert config.layout.adapter_host_files_upgrade_path.is_file()


def test_the_fixture_matches_the_bytes_it_was_written_for() -> None:
    text = FIXTURE.read_text()
    for attachment_id, data in CONTENT.items():
        assert hashlib.sha256(data).hexdigest() in text, attachment_id
