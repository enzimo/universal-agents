"""The gateway's cleanup of old chat uploads (assistant_gateway.upload_retention).

Uploads are real files in a real uploads folder with real records in the
gateway database: SQLite always, and PostgreSQL when ``LIVE_TEST_POSTGRES_DSN``
is set (``tests/knowledge_support.knowledge_storage`` gives each test its own
schema).
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

import assistant_gateway.upload_retention as upload_retention
import storage.gateway_state_repository as gateway_state_repository
from assistant_gateway.inbound_attachments import (
    InboundAttachmentStoragePolicy,
    StoredInboundAttachment,
    store_inbound_attachment_bytes_with_policy,
)
from assistant_gateway.store import GatewayStateStore
from assistant_gateway.upload_retention import UploadRetention, prune_uploads
from assistant_gateway.application.gateway_application import GatewayApplicationResponse
from custom_messages.contracts.delivery_v1 import UserNotificationAttachment
from tests.knowledge_support import BACKENDS, knowledge_storage
from tests.test_gateway_transcript import (
    CONVERSATION as TRANSCRIPT_CONVERSATION,
    SESSION as TRANSCRIPT_SESSION,
    _create_web_session,
    _route,
    _service,
)

NOW = datetime(2026, 10, 20, 12, 0, tzinfo=timezone.utc)
CONVERSATION = "website:web-chat:thread_a"
OTHER_CONVERSATION = "website:web-chat:thread_b"


def _store(backend: str, tmp_path: Path):
    return knowledge_storage(backend, tmp_path)


def _upload(
    store: GatewayStateStore,
    uploads_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    session_key: str,
    name: str,
    age_days: float,
) -> StoredInboundAttachment:
    """Save and record an upload as if it was sent ``age_days`` before NOW."""

    when = NOW - timedelta(days=age_days)
    stored = store_inbound_attachment_bytes_with_policy(
        data=f"bytes of {name}".encode(),
        filename=f"{name}.txt",
        policy=InboundAttachmentStoragePolicy(cache_dir=uploads_dir),
        session_key=session_key,
        conversation_id=None,
    )
    os.utime(stored.local_path, (when.timestamp(), when.timestamp()))
    monkeypatch.setattr(gateway_state_repository, "utc_now_iso", lambda: when.isoformat())
    attachment = stored.attachment
    store.upsert_attachment(
        attachment_id=attachment.attachment_id,
        session_key=session_key,
        conversation_id=None,
        correlation_id=None,
        filename=attachment.filename,
        content_type=attachment.content_type,
        kind=attachment.kind,
        size_bytes=attachment.size_bytes,
        sha256=attachment.sha256,
        local_path=str(stored.local_path),
        owner_canonical_user_id="usr_alpha",
    )
    monkeypatch.undo()
    return stored


def _aged_file(path: Path, age_days: float) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"left behind")
    when = (NOW - timedelta(days=age_days)).timestamp()
    os.utime(path, (when, when))
    return path


@pytest.mark.parametrize("backend", BACKENDS)
def test_uploads_older_than_the_retention_are_deleted_with_their_records(
    backend: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    uploads_dir = tmp_path / "state" / "uploads"
    with _store(backend, tmp_path) as settings:
        store = GatewayStateStore(str(tmp_path / "gateway.db"), storage_settings=settings)
        old = _upload(store, uploads_dir, monkeypatch, session_key=CONVERSATION, name="old", age_days=100)
        recent = _upload(store, uploads_dir, monkeypatch, session_key=CONVERSATION, name="recent", age_days=10)
        alone = _upload(store, uploads_dir, monkeypatch, session_key=OTHER_CONVERSATION, name="alone", age_days=95)

        result = prune_uploads(records=store, uploads_dir=uploads_dir, retention_days=90, now=NOW)

        assert (result.records_removed, result.files_removed, result.folders_removed, result.skipped) == (2, 2, 1, 0)
        assert store.get_attachment(old.attachment.attachment_id) is None
        assert store.get_attachment(alone.attachment.attachment_id) is None
        assert store.get_attachment(recent.attachment.attachment_id) is not None
    assert not old.local_path.exists()
    assert not alone.local_path.parent.exists(), "the emptied conversation folder goes too"
    assert recent.local_path.read_bytes() == b"bytes of recent"
    assert uploads_dir.is_dir(), "the uploads folder itself stays"


def test_old_files_without_a_record_are_deleted_and_recent_ones_kept(tmp_path: Path) -> None:
    uploads_dir = tmp_path / "uploads"
    store = GatewayStateStore(str(tmp_path / "gateway.db"))
    stray = _aged_file(uploads_dir / "website_web-chat_gone" / "att_stray.txt", age_days=120)
    fresh = _aged_file(uploads_dir / "website_web-chat_gone" / "att_fresh.txt", age_days=1)

    result = prune_uploads(records=store, uploads_dir=uploads_dir, retention_days=90, now=NOW)

    assert (result.records_removed, result.files_removed) == (0, 1)
    assert not stray.exists()
    assert fresh.exists()


def test_zero_days_keeps_every_upload(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog) -> None:
    uploads_dir = tmp_path / "uploads"
    store = GatewayStateStore(str(tmp_path / "gateway.db"))
    old = _upload(store, uploads_dir, monkeypatch, session_key=CONVERSATION, name="old", age_days=4000)
    caplog.set_level(logging.INFO, logger=upload_retention.__name__)

    result = prune_uploads(records=store, uploads_dir=uploads_dir, retention_days=0, now=NOW)

    assert (result.records_removed, result.files_removed) == (0, 0)
    assert old.local_path.exists()
    assert store.get_attachment(old.attachment.attachment_id) is not None
    assert "event=upload_prune_disabled" in caplog.text


def test_records_and_files_outside_the_uploads_folder_are_kept(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    uploads_dir = tmp_path / "uploads"
    store = GatewayStateStore(str(tmp_path / "gateway.db"))
    outside = _upload(store, tmp_path / "elsewhere", monkeypatch, session_key=CONVERSATION, name="outside", age_days=200)

    result = prune_uploads(records=store, uploads_dir=uploads_dir, retention_days=90, now=NOW)

    assert (result.records_removed, result.files_removed, result.skipped) == (0, 0, 0)
    assert store.get_attachment(outside.attachment.attachment_id) is not None
    assert outside.local_path.exists()


@pytest.mark.parametrize("backend", BACKENDS)
def test_upload_cleanup_preserves_the_transcript_record_for_a_file_sent_by_an_agent(
    backend: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A retained scratch copy must remain downloadable from its original chat."""

    uploads_dir = tmp_path / "state" / "uploads"
    with _store(backend, tmp_path) as settings:
        store = GatewayStateStore(str(tmp_path / "gateway.db"), storage_settings=settings)
        _create_web_session(store)
        service = _service(store)
        session = store.session_repository.get_session(TRANSCRIPT_SESSION)
        accepted = service.accept_user_message(
            session=session, route=_route(), conversation_id=TRANSCRIPT_CONVERSATION,
            text="Give me a report", attachments=(),
        )
        sent = _aged_file(tmp_path / "workspaces" / "users" / "user_alpha" / "scratch" / "sent" / "report.txt", 100)
        response = GatewayApplicationResponse(
            text="Your report", conversation_id=TRANSCRIPT_CONVERSATION, correlation_id="corr_reply",
            attachments=(UserNotificationAttachment(
                path=str(sent), filename=sent.name, content_type="text/plain", kind="document",
                sha256=hashlib.sha256(sent.read_bytes()).hexdigest(),
            ),),
        )
        with monkeypatch.context() as old_clock:
            old_clock.setattr(gateway_state_repository, "utc_now_iso", lambda: (NOW - timedelta(days=100)).isoformat())
            projected = service.append_gateway_response(
                session=session, route=_route(), response=response, reply_to=accepted
            )
        attachment_id = projected.attachment_references[0].attachment_id
        old_upload = _upload(store, uploads_dir, monkeypatch, session_key=CONVERSATION, name="old", age_days=100)

        result = prune_uploads(records=store, uploads_dir=uploads_dir, retention_days=90, now=NOW)

        assert (result.records_removed, result.files_removed, result.skipped) == (1, 1, 0)
        assert store.get_attachment(old_upload.attachment.attachment_id) is None
        record = store.get_attachment(attachment_id)
        assert record is not None
        assert Path(record.local_path).read_bytes() == b"left behind"


@pytest.mark.parametrize("backend", BACKENDS)
def test_upload_folder_matching_is_literal_and_excludes_siblings(
    backend: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    uploads_dir = tmp_path / "uploads_%"
    with _store(backend, tmp_path) as settings:
        store = GatewayStateStore(str(tmp_path / "gateway.db"), storage_settings=settings)
        inbound = _upload(store, uploads_dir, monkeypatch, session_key=CONVERSATION, name="inbound", age_days=100)
        others = [
            _upload(store, folder, monkeypatch, session_key=CONVERSATION, name="outside", age_days=200)
            for folder in (tmp_path / "uploads_%_sibling", tmp_path / "uploads_xy")
        ]

        result = prune_uploads(records=store, uploads_dir=uploads_dir, retention_days=90, now=NOW)

        assert (result.records_removed, result.files_removed) == (1, 1)
        assert store.get_attachment(inbound.attachment.attachment_id) is None
        assert all(store.get_attachment(item.attachment.attachment_id) is not None for item in others)
        assert all(item.local_path.exists() for item in others)


def test_links_in_the_uploads_folder_are_never_followed(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    uploads_dir = tmp_path / "uploads"
    store = GatewayStateStore(str(tmp_path / "gateway.db"))
    target = _aged_file(tmp_path / "precious" / "keep.txt", age_days=300)
    recorded = _upload(store, uploads_dir, monkeypatch, session_key=CONVERSATION, name="swapped", age_days=200)
    # The recorded file was swapped for a link, and another link sits beside it.
    recorded.local_path.unlink()
    recorded.local_path.symlink_to(target)
    beside = recorded.local_path.parent / "beside.txt"
    beside.symlink_to(target)
    old = (NOW - timedelta(days=300)).timestamp()
    os.utime(beside, (old, old), follow_symlinks=False)

    result = prune_uploads(records=store, uploads_dir=uploads_dir, retention_days=90, now=NOW)

    assert result.records_removed == 1
    assert result.skipped >= 2
    assert target.read_bytes() == b"left behind"
    assert recorded.local_path.is_symlink() and beside.is_symlink()


def test_records_are_deleted_in_batches_until_none_are_left(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    uploads_dir = tmp_path / "uploads"
    store = GatewayStateStore(str(tmp_path / "gateway.db"))
    uploads = [
        _upload(store, uploads_dir, monkeypatch, session_key=CONVERSATION, name=f"old{index}", age_days=100 + index)
        for index in range(5)
    ]
    monkeypatch.setattr(upload_retention, "RECORD_BATCH", 2)

    result = prune_uploads(records=store, uploads_dir=uploads_dir, retention_days=90, now=NOW)

    assert (result.records_removed, result.files_removed) == (5, 5)
    assert all(store.get_attachment(item.attachment.attachment_id) is None for item in uploads)


class _RecordingRecords:
    def __init__(self, *, fail: bool = False) -> None:
        self.cutoffs: list[datetime] = []
        self.fail = fail

    def delete_uploads_created_before(self, cutoff: str, *, uploads_dir: str, limit: int) -> tuple[str, ...]:
        if self.fail:
            raise RuntimeError("database unavailable")
        self.cutoffs.append(datetime.fromisoformat(cutoff))
        return ()


async def _until(condition) -> None:
    for _ in range(200):
        if condition():
            return
        await asyncio.sleep(0.01)
    raise AssertionError("condition not reached")


@pytest.mark.asyncio
async def test_the_cleanup_runs_at_start_and_again_with_the_current_number_of_days(tmp_path: Path) -> None:
    records = _RecordingRecords()
    retention = UploadRetention(
        records=records, uploads_dir=tmp_path / "uploads", retention_days=90, interval=timedelta(milliseconds=20)
    )
    stop = asyncio.Event()
    running = asyncio.create_task(retention.run(stop))

    await _until(lambda: len(records.cutoffs) >= 1)
    retention.retention_days = 7  # an administrator saved a new number
    await _until(lambda: len(records.cutoffs) >= 3)
    stop.set()
    await asyncio.wait_for(running, timeout=5)

    first, *later = records.cutoffs
    now = datetime.now(timezone.utc)
    assert abs((now - first) - timedelta(days=90)) < timedelta(minutes=1)
    assert abs((now - later[-1]) - timedelta(days=7)) < timedelta(minutes=1)


@pytest.mark.asyncio
async def test_a_failed_cleanup_is_logged_and_tried_again_later(tmp_path: Path, caplog) -> None:
    retention = UploadRetention(records=_RecordingRecords(fail=True), uploads_dir=tmp_path, retention_days=90)

    assert await retention.run_once() is None
    assert "event=upload_prune_failed" in caplog.text
