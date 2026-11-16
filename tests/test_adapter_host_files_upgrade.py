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
at it, it opens by id and is listed for the model), files it cannot move
(dropped from their turns, which then say their request's files are not all
recorded), records it refuses (a link, another checksum, an id that is not a
file name, a destination already taken, a conversation entry naming another
path), the folder missing (nothing changes; ``--files-gone`` then finishes),
a run stopped part way (the next run finishes), a move across file systems,
upload retention and a person's removal finding the moved file, the launcher
step and the operator command. A held request still naming the old path gets
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
        "moved=1 already_moved=0 missing=1 refused=0 no_record=1 gateway_rows_removed=1 turns_marked_unrecorded=2"
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


def test_files_it_cannot_move_leave_their_turns_and_the_model_is_told_to_ask_for_them(instance: _Instance, caplog) -> None:
    assert (instance.recorded("task_1"), instance.recorded("task_2")) == (1, 1)

    with caplog.at_level(logging.WARNING, logger=MODULE_LOGGER):
        instance.run()

    # The lost file (gone from the folder) and the orphan (no gateway record, so no chat session for its folder).
    assert set(instance.files()) == {"att_photo", "att_web"}
    assert (instance.recorded("task_1"), instance.recorded("task_2")) == (0, 0)
    assert instance.conversations().last_turn_without_file_records(CONVERSATION) == 2
    assert instance.gateway().get_attachment("att_lost") is None
    assert (instance.transfer / "adapter_upload_orphan").read_bytes() == CONTENT["att_orphan"], "the file is left alone"
    request = Task(task_id="task_next", task_type=MessageMetaType.REQUEST, problem_statement="And the PDF?")
    held = SimpleNamespace(task=request, meta=SimpleNamespace(conversation_id=CONVERSATION), owner_person_id=PERSON)
    scope = TaskAttachments.load(held, instance.conversations())
    assert scope.find(["att_lost", "att_orphan"])[1] == ["att_lost", "att_orphan"]
    assert UNRECORDED_TURNS_NOTE.format(turn=2) in inventory_text(scope, exclude_ids=[])
    reasons = sorted(message.rsplit("reason=", 1)[1] for message in _events(caplog, "adapter_host_file_not_moved"))
    assert reasons == ["missing", "no_gateway_record"]
    assert all(f"conversation_ids={CONVERSATION}" in message for message in _events(caplog, "adapter_host_file_not_moved"))


def _link_source(instance: _Instance) -> None:
    elsewhere = _write(instance.root / "elsewhere" / "photo", CONTENT["att_photo"])
    instance.photo_source.unlink()
    instance.photo_source.symlink_to(elsewhere)


def _other_bytes(instance: _Instance) -> None:
    instance.photo_source.write_bytes(b"not the bytes the gateway recorded")


def _destination_taken(instance: _Instance) -> None:
    _write(instance.photo_destination, b"another file under the same name")


def _forged_id(instance: _Instance) -> None:
    """A record whose id is not a file name, with a gateway record and a turn entry naming the photo's file."""

    source = str(instance.photo_source)
    record = instance.gateway().get_attachment("att_photo")
    gateway = instance.gateway()
    gateway.delete_attachment("att_photo")
    gateway.upsert_attachment(
        attachment_id="../escape",
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
        attachments=[_record("../escape", source, record.sha256)],
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
def test_a_file_it_refuses_is_not_moved_and_not_offered(instance: _Instance, caplog, change, attachment_id, reason) -> None:
    change(instance)
    before = {path: path.read_bytes() for path in instance.root.rglob("*") if path.is_file() and not path.is_symlink() and "uploads" in path.parts}

    with caplog.at_level(logging.WARNING, logger=MODULE_LOGGER):
        result = instance.run()

    assert result.finished and result.counts.refused == 1 and result.counts.moved == 0
    assert attachment_id not in instance.files()
    assert instance.gateway().get_attachment(attachment_id) is None
    assert instance.recorded("task_1") == 0
    assert any(message.endswith(f"attachment_id={attachment_id} conversation_ids={CONVERSATION} reason={reason}") for message in _events(caplog, "adapter_host_file_not_moved"))
    assert os.path.lexists(instance.photo_source), "a refused file is left where it is"
    after = {path: path.read_bytes() for path in instance.root.rglob("*") if path.is_file() and not path.is_symlink() and "uploads" in path.parts}
    assert after == before, "nothing in state/uploads was written or replaced"


def test_a_conversation_entry_naming_another_path_does_not_follow_the_moved_file(instance: _Instance, caplog) -> None:
    """Agent processes write conversation turns, so an entry is moved only when it names the gateway's file."""

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
    assert "att_photo" not in instance.turn_paths("task_3") and instance.recorded("task_3") == 0
    assert instance.turn_paths("task_1")["att_photo"] == str(instance.photo_destination)
    assert any(message.endswith("reason=record_mismatch") for message in _events(caplog, "adapter_host_file_not_moved"))


# --- The transfer folder is not here -------------------------------------------------------------


def test_with_the_folder_missing_nothing_changes_until_the_files_are_declared_gone(instance: _Instance, caplog) -> None:
    moved_aside = instance.root / "unmounted"
    instance.transfer.rename(moved_aside)
    before = (instance.files(), instance.recorded("task_1"), instance.gateway().get_attachment("att_photo"))

    with caplog.at_level(logging.ERROR, logger=MODULE_LOGGER):
        waiting = instance.run()

    assert not waiting.finished and waiting.folder_missing and waiting.waiting == 3
    assert (instance.files(), instance.recorded("task_1"), instance.gateway().get_attachment("att_photo")) == before
    assert not instance.layout.adapter_host_files_upgrade_path.exists()
    (error,) = _events(caplog, "adapter_host_files_folder_missing")
    assert "--files-gone" in error and error.endswith(f"folder={instance.transfer} waiting=3")

    gone = instance.run(files_gone=True)

    assert gone.finished and (gone.counts.missing, gone.counts.no_record, gone.counts.moved) == (2, 1, 0)
    assert set(instance.files()) == {"att_web"}
    assert instance.layout.adapter_host_files_upgrade_path.is_file()


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
