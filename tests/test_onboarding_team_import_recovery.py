"""Onboarding can retry a failed review initialization and reopen saved edits."""

from __future__ import annotations

import shutil
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Barrier, Lock

import pytest
from aiohttp.test_utils import TestClient, TestServer

from access_control import safe_open
from assistant_gateway.onboarding_web.server import OnboardingWebService, OnboardingWebSettings
from runtime_settings.instance_layout import InstanceLayout
from team_transfer.container import TransferReader
from team_transfer.review import prepare_review, text_digest
from tests.test_team_transfer_review import PASSPHRASE, backup as backup


def _onboarding(root: Path) -> OnboardingWebService:
    return OnboardingWebService(
        OnboardingWebSettings(host="127.0.0.1", port=18090, instance_root=str(root))
    )


def _copy_export(reader, target: Path) -> Path:
    folder = InstanceLayout.at(target).import_dir
    folder.mkdir(parents=True)
    shutil.copy(reader.path, folder / "team.uatransfer")
    return InstanceLayout.at(target).team_import_review_dir / reader.manifest.mac[:24]


async def _open(client: TestClient):
    return await client.post(
        "/api/team-import/open",
        json={"file_name": "team.uatransfer", "passphrase": PASSPHRASE},
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("failure_at", ["editable", "metadata"])
async def test_initial_write_failure_can_retry_and_saved_review_survives_reopening(
    backup, monkeypatch: pytest.MonkeyPatch, failure_at: str
) -> None:
    reader, target, _directory = backup
    review_dir = _copy_export(reader, target)
    write_bytes = safe_open.write_bytes

    def fail_initial_write(root, parts, data, **kwargs):
        if parts == ("review.json",) and failure_at == "metadata":
            raise OSError("Injected review metadata write failure")
        result = write_bytes(root, parts, data, **kwargs)
        if parts != ("review.json",) and failure_at == "editable":
            raise OSError("Injected editable file write failure")
        return result

    async with TestClient(TestServer(_onboarding(target).build_app())) as client:
        monkeypatch.setattr(safe_open, "write_bytes", fail_initial_write)
        failed = await _open(client)
        assert failed.status == 400
        assert "Injected" in (await failed.json())["error"]
        published_after_failure = review_dir.exists()
        monkeypatch.setattr(safe_open, "write_bytes", write_bytes)

        retry = await _open(client)
        assert retry.status == 200, await retry.text()
        assert not published_after_failure, "a failed initialization must never publish a review"
        headers = {"X-Transfer-Token": (await retry.json())["token"]}
        review = await (await client.get("/api/team-import/review", headers=headers)).json()
        fact = next(row for row in review["records"] if row["section"] == "pinned_memory")
        schedule = next(row for row in review["records"] if row["key"] == "schedule/sched_first")
        edited_schedule = schedule["text"].replace("Run sched_first", "Send the saved reminder")
        for payload in (
            dict(section="pinned_memory", key=fact["key"], selected=False, text="Saved edited fact"),
            dict(section="schedules", key=schedule["key"], selected=True,
                 text=edited_schedule, confirmed=True),
        ):
            saved = await client.post("/api/team-import/record", headers=headers, json=payload)
            assert saved.status == 200, await saved.text()

    # A new onboarding service reads the completed folder, rather than rebuilding it.
    async with TestClient(TestServer(_onboarding(target).build_app())) as client:
        reopened = await _open(client)
        assert reopened.status == 200, await reopened.text()
        headers = {"X-Transfer-Token": (await reopened.json())["token"]}
        review = await (await client.get("/api/team-import/review", headers=headers)).json()
        records = {(row["section"], row["key"]): row for row in review["records"]}
        saved_fact = records[("pinned_memory", fact["key"])]
        assert saved_fact["selected"] is False and saved_fact["text"] == "Saved edited fact"
        saved_schedule = records[("schedules", schedule["key"])]
        assert saved_schedule["text"] == edited_schedule
        assert saved_schedule["confirmed_digest"] == text_digest(edited_schedule)
        assert review_dir.is_dir()
        assert review_dir.stat().st_mode & 0o777 == 0o700
        assert (review_dir / "review.json").stat().st_mode & 0o777 == 0o600
        assert list(review_dir.parent.iterdir()) == [review_dir]


@pytest.mark.asyncio
async def test_incomplete_folder_and_abandoned_temporary_folder_do_not_block_open(
    backup, caplog: pytest.LogCaptureFixture
) -> None:
    reader, target, _directory = backup
    review_dir = _copy_export(reader, target)
    (review_dir / "settings").mkdir(parents=True)
    (review_dir / "settings" / "incomplete.yaml").write_text("unfinished")
    abandoned = review_dir.with_name(f".{review_dir.name}.init-abandoned")
    abandoned.mkdir(mode=0o700)
    (abandoned / "partial.md").write_text("unfinished")

    async with TestClient(TestServer(_onboarding(target).build_app())) as client:
        opened = await _open(client)
        assert opened.status == 200, await opened.text()
        headers = {"X-Transfer-Token": (await opened.json())["token"]}
        review = await (await client.get("/api/team-import/review", headers=headers)).json()
        assert any(row["key"] == "schedule/sched_first" for row in review["records"])
    assert not (review_dir / "settings" / "incomplete.yaml").exists()
    assert (review_dir / "review.json").is_file()
    assert "event=team_transfer_review_initialization_recovered" in caplog.text


@pytest.mark.asyncio
async def test_corrupted_completed_review_is_refused_without_rebuilding(backup) -> None:
    reader, target, _directory = backup
    review_dir = _copy_export(reader, target)
    async with TestClient(TestServer(_onboarding(target).build_app())) as client:
        assert (await _open(client)).status == 200
        (review_dir / "review.json").write_text("corrupted review")
        reopened = await _open(client)
        assert reopened.status == 400
        assert (review_dir / "review.json").read_text() == "corrupted review"


@pytest.mark.asyncio
async def test_review_folder_link_is_refused_without_touching_its_target(backup) -> None:
    reader, target, _directory = backup
    review_dir = _copy_export(reader, target)
    review_dir.parent.mkdir(parents=True)
    outside = target.parent / "outside-review"
    outside.mkdir()
    sentinel = outside / "review.json"
    sentinel.write_text("private sentinel")
    review_dir.symlink_to(outside, target_is_directory=True)
    async with TestClient(TestServer(_onboarding(target).build_app())) as client:
        opened = await _open(client)
        assert opened.status == 400
        assert "link" in (await opened.json())["error"].lower()
    assert sentinel.read_text() == "private sentinel"
    assert list(outside.iterdir()) == [sentinel]


def test_concurrent_opens_recover_and_initialize_the_review_once(
    backup, monkeypatch: pytest.MonkeyPatch
) -> None:
    reader, target, directory = backup
    directory.mkdir(mode=0o700)
    (directory / "unfinished.md").write_text("unfinished")
    barrier = Barrier(2)
    count_lock = Lock()
    metadata_writes = 0
    write_bytes = safe_open.write_bytes

    def count_initialization(root, parts, data, **kwargs):
        nonlocal metadata_writes
        if parts == ("review.json",):
            with count_lock:
                metadata_writes += 1
        return write_bytes(root, parts, data, **kwargs)

    def open_review():
        with TransferReader.open(reader.path, PASSPHRASE) as source:
            barrier.wait(timeout=10)
            files = prepare_review(source, target, directory)
            return files.root, files.review.model_dump(mode="json"), files.digest()

    monkeypatch.setattr(safe_open, "write_bytes", count_initialization)
    with ThreadPoolExecutor(max_workers=2) as workers:
        first = workers.submit(open_review)
        second = workers.submit(open_review)
        assert first.result(timeout=20) == second.result(timeout=20)
    assert metadata_writes == 1
    assert (directory / "review.json").is_file()
    assert not (directory / "unfinished.md").exists()
