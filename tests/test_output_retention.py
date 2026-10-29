"""Trusted cleanup of saved tool outputs (custom_agents.output_retention).

Real folders of a temporary instance and a real SQLite task database. Each
kind is deleted a moment after its retention period and kept a moment before
it; 0 keeps everything; links are removed as links or left alone, never
followed; conversations, task links and completed tasks are never touched.
Every reader of an offloaded conversation output says plainly when it was
deleted and keeps its summary (the tool readers are tested with their tools).
"""

from __future__ import annotations

import fcntl
import json
import logging
import os
import sqlite3
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

from custom_agents.conversation_store import ConversationStore
from custom_agents.internal_improvement_runtime import remove_old_outputs_for_maintenance
from custom_agents.output_retention import (
    GRACE_DAYS,
    OutputKind,
    remove_old_outputs,
    written_at_from_name,
)
from custom_messages.contracts.task_v1 import Task
from custom_messages.enums import MessageMetaType
from old_file_cleanup import SECONDS_PER_DAY
from runtime_hosting.builds import BUILD_LOCK_FILENAME
from runtime_settings.instance_layout import InstanceLayout
from storage.config import resolve_storage_settings
from storage.task_archive_repository import create_task_archive_repository

NOW = datetime(2026, 10, 27, 12, 0, tzinfo=timezone.utc).timestamp()
DAYS = 90
MINUTE = 60.0
# A moment past the period, and a moment before it.
EXPIRED = NOW - DAYS * SECONDS_PER_DAY - MINUTE
KEPT = NOW - DAYS * SECONDS_PER_DAY + MINUTE
BLOCK_TEXT = "block text"


def _stamp(moment: float, *, micro: bool = False) -> str:
    fmt = "%Y%m%dT%H%M%S%fZ" if micro else "%Y%m%dT%H%M%SZ"
    return datetime.fromtimestamp(moment, timezone.utc).strftime(fmt)


def _iso(moment: float) -> str:
    return datetime.fromtimestamp(moment, timezone.utc).isoformat()


def _touch(path: Path, moment: float) -> None:
    os.utime(path, (moment, moment), follow_symlinks=False)


def _file(path: Path, data: str = "x", *, mtime: float | None = None) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(data, encoding="utf-8")
    if mtime is not None:
        _touch(path, mtime)
    return path


def _tool_result(layout: InstanceLayout, task: str, written: float, *, name: str | None = None) -> Path:
    """A saved tool result as tool_result_offloader writes it: manifest.json and block files."""

    folder = layout.tool_results_dir / task / (name or f"{_stamp(written)}_tooluse_1_shell_0123456789abcdef")
    folder.mkdir(parents=True)
    _file(folder / "block_0.txt", BLOCK_TEXT)
    _file(folder / "manifest.json", _manifest_text(written))
    return folder


def _manifest_text(written: float) -> str:
    return json.dumps({"manifest_version": 1, "created_at": _iso(written), "blocks": [{"index": 0}]})


def _kind(result, kind: OutputKind):
    return next(item for item in result.kinds if item.kind == kind)


@pytest.fixture
def layout(tmp_path: Path) -> InstanceLayout:
    layout = InstanceLayout.at(tmp_path / "instance")
    layout.ensure_directories()
    return layout


@pytest.fixture
def store(layout: InstanceLayout) -> ConversationStore:
    return ConversationStore(
        db_path=str(layout.task_database_path), artifacts_dir=layout.conversation_artifacts_dir
    )


def _run(layout: InstanceLayout, store=None, *, days: int = DAYS):
    return remove_old_outputs(layout, retention_days=days, conversation_records=store, now=NOW)


# -- tool results -------------------------------------------------------------------------


def test_tool_results_go_a_moment_after_the_period_and_stay_a_moment_before(layout: InstanceLayout) -> None:
    expired = _tool_result(layout, "task_old", EXPIRED)
    kept = _tool_result(layout, "task_mixed", KEPT)
    expired_beside_kept = _tool_result(layout, "task_mixed", EXPIRED, name=f"{_stamp(EXPIRED)}_tooluse_2_web_fedcba9876543210")

    result = _run(layout)

    assert not expired.exists() and not expired.parent.exists(), "the emptied task folder goes too"
    assert not expired_beside_kept.exists()
    assert (kept / "manifest.json").is_file() and (kept / "block_0.txt").is_file()
    assert layout.tool_results_dir.is_dir()
    counts = _kind(result, OutputKind.TOOL_RESULTS)
    assert counts.items_removed == 2
    assert counts.files_removed == 4
    assert counts.bytes_removed == 2 * (len(BLOCK_TEXT) + len(_manifest_text(EXPIRED)))
    assert counts.folders_removed == 3, "two result folders and the emptied task folder"


def test_a_tool_result_without_a_dated_name_uses_its_manifest_then_its_last_change(layout: InstanceLayout) -> None:
    by_manifest = _tool_result(layout, "task_a", EXPIRED, name="undated_by_manifest")
    _touch(by_manifest, NOW)  # recently changed, but the manifest says when it was written
    by_change = layout.tool_results_dir / "task_b" / "undated_without_manifest"
    _file(by_change / "block_0.txt", "x", mtime=EXPIRED)
    _touch(by_change, EXPIRED)
    recent_manifest = _tool_result(layout, "task_c", KEPT, name="undated_recent")

    _run(layout)

    assert not by_manifest.exists()
    assert not by_change.exists()
    assert recent_manifest.exists()


def test_a_folder_name_dated_in_the_future_counts_as_undated(layout: InstanceLayout) -> None:
    """A saved tool result (trusted folder) falls back to its manifest and last change; research keeps it."""

    future = _stamp(NOW + 400 * SECONDS_PER_DAY)
    tool_result = layout.tool_results_dir / "task-1" / f"{future}_tooluse_1_shell_0123456789abcdef"
    _file(tool_result / "block_0.txt", "x", mtime=EXPIRED)
    _touch(tool_result, EXPIRED)
    research = layout.workspaces_dir / "agents" / "research" / "task-1" / f"{future}_web_x_abc"
    _file(research / "manifest.json", "{}", mtime=EXPIRED)
    _touch(research, EXPIRED)

    _run(layout)

    assert not tool_result.exists()
    assert research.exists(), "research keeps every folder without a usable stamp"


def test_names_carry_their_write_time_with_or_without_microseconds() -> None:
    assert written_at_from_name(f"{_stamp(EXPIRED)}_tooluse_shell_abc") == pytest.approx(EXPIRED, abs=1)
    assert written_at_from_name(f"{_stamp(EXPIRED, micro=True)}_exa_search_q_abc") == pytest.approx(EXPIRED, abs=1)
    assert written_at_from_name("no_stamp_here") is None
    assert written_at_from_name("20261399T000000Z_bad_month") is None


# -- zero keeps everything ----------------------------------------------------------------


def test_zero_days_keeps_every_kind(layout: InstanceLayout, store: ConversationStore) -> None:
    tool_result = _tool_result(layout, "task_old", EXPIRED)
    artifact = store.offload_large_text(
        conversation_id="conv_1", task_id="task_1", artifact_type="tool_output", raw_text="long", summary_text="s"
    )
    _touch(Path(artifact["storage_path"]), EXPIRED)
    research = layout.person_research_dir("alice") / "task-1" / f"{_stamp(EXPIRED)}_web_q_abc"
    _file(research / "manifest.json", "{}")
    run = layout.execution_runs_dir / "run_1.abcd"
    _file(run / "program.py", "print()", mtime=EXPIRED)
    _touch(run, EXPIRED)

    result = _run(layout, store, days=0)

    assert tool_result.exists() and Path(artifact["storage_path"]).exists()
    assert research.exists() and run.exists()
    assert result.kinds == []
    assert "turned off (tool_policy.output_retention_days is 0)" in result.instruction_sentence()


# -- conversation artifacts ---------------------------------------------------------------


def _age_artifact(layout: InstanceLayout, artifact: dict, moment: float) -> None:
    with sqlite3.connect(layout.task_database_path) as conn:
        conn.execute(
            "UPDATE conversation_artifacts SET created_at = ? WHERE artifact_id = ?", (_iso(moment), artifact["artifact_id"])
        )
    _touch(Path(artifact["storage_path"]), moment)


def _artifact_ids(layout: InstanceLayout) -> set[str]:
    with sqlite3.connect(layout.task_database_path) as conn:
        return {row[0] for row in conn.execute("SELECT artifact_id FROM conversation_artifacts")}


def test_conversation_artifacts_go_with_their_records_and_orphans_by_age(
    layout: InstanceLayout, store: ConversationStore, tmp_path: Path
) -> None:
    expired = store.offload_large_text(
        conversation_id="conv_old", task_id="task_1", artifact_type="tool_output", raw_text="old output", summary_text="s"
    )
    kept = store.offload_large_text(
        conversation_id="conv_new", task_id="task_2", artifact_type="tool_output", raw_text="new output", summary_text="s"
    )
    _age_artifact(layout, expired, EXPIRED)
    _age_artifact(layout, kept, KEPT)
    old_orphan = _file(layout.conversation_artifacts_dir / "conv_gone" / "task_x_artifact_y.txt", "orphan", mtime=EXPIRED)
    new_orphan = _file(layout.conversation_artifacts_dir / "conv_gone2" / "task_x_artifact_z.txt", "new", mtime=KEPT)
    outside = _file(tmp_path / "outside.txt", "not an artifact", mtime=EXPIRED)
    with sqlite3.connect(layout.task_database_path) as conn:  # a record naming a file outside the folder
        conn.execute(
            "INSERT INTO conversation_artifacts VALUES (?, ?, ?, ?, ?, ?, ?)",
            ("artifact_outside", "conv_x", "task_x", "tool_output", str(outside), "s", _iso(EXPIRED)),
        )

    result = _run(layout, store)

    assert not Path(expired["storage_path"]).exists() and not Path(expired["storage_path"]).parent.exists()
    assert Path(kept["storage_path"]).read_text(encoding="utf-8") == "new output"
    assert not old_orphan.exists() and new_orphan.exists()
    assert outside.read_text(encoding="utf-8") == "not an artifact"
    assert _artifact_ids(layout) == {kept["artifact_id"]}
    counts = _kind(result, OutputKind.CONVERSATION_ARTIFACTS)
    assert counts.items_removed == 2
    assert counts.files_removed == 2
    assert counts.bytes_removed == len("old output") + len("orphan")
    assert counts.skipped == 1


def test_records_are_deleted_in_batches(
    layout: InstanceLayout, store: ConversationStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("custom_agents.output_retention.RECORD_BATCH", 2)
    artifacts = [
        store.offload_large_text(
            conversation_id="conv_1", task_id=f"task_{index}", artifact_type="tool_output", raw_text="x", summary_text="s"
        )
        for index in range(5)
    ]
    for artifact in artifacts:
        _age_artifact(layout, artifact, EXPIRED)

    result = _run(layout, store)

    assert _artifact_ids(layout) == set()
    assert _kind(result, OutputKind.CONVERSATION_ARTIFACTS).items_removed == 5


def test_transcripts_task_links_and_completed_tasks_are_never_deleted(
    layout: InstanceLayout, store: ConversationStore
) -> None:
    settings = resolve_storage_settings({"backend": "sqlite", "sqlite": {"path": str(layout.task_database_path)}})
    archive = create_task_archive_repository(settings, logger=logging.getLogger("test.output_retention"))
    task = Task(task_id="task_old", task_type=MessageMetaType.REQUEST, problem_statement="An old request")
    archive.save_completed_task(
        archive_id="agent_a::task_old", task_id=task.task_id, owner_agent_id="agent_a", owner_agent_name="Agent A",
        team_id="team_1", task_json=task.model_dump_json(), status="completed",
    )
    store.ensure_thread(
        conversation_id="conv_old", team_id="team_1", owner_agent_id="agent_a", owner_agent_name="Agent A",
        requester_name="Alice", requester_id="alice", root_task_id=task.task_id,
    )
    store.append_turn(
        conversation_id="conv_old", task_id=task.task_id, followup_to_task_id=None, owner_agent_id="agent_a",
        requester_name="Alice", requester_id="alice", correlation_id=None, problem_text="An old request",
    )
    artifact = store.offload_large_text(
        conversation_id="conv_old", task_id=task.task_id, artifact_type="tool_output", raw_text="long", summary_text="s"
    )
    _age_artifact(layout, artifact, EXPIRED)
    # Far older than the period, by every date these tables keep.
    dated = {"conversation_threads": "created_at", "conversation_task_links": "created_at", "completed_tasks": "timestamp"}
    tables = tuple(dated)
    with sqlite3.connect(layout.task_database_path) as conn:
        for table, column in dated.items():
            conn.execute(f"UPDATE {table} SET {column} = ?", (_iso(EXPIRED - 365 * SECONDS_PER_DAY),))
        before = {table: conn.execute(f"SELECT * FROM {table}").fetchall() for table in tables}

    _run(layout, store)

    with sqlite3.connect(layout.task_database_path) as conn:
        after = {table: conn.execute(f"SELECT * FROM {table}").fetchall() for table in tables}
    assert all(before[table] for table in tables)
    assert after == before
    assert store.get_thread("conv_old") is not None
    assert _artifact_ids(layout) == set()


def test_a_failing_kind_is_reported_and_the_others_still_run(layout: InstanceLayout, caplog) -> None:
    class Broken:
        def delete_artifacts_created_before(self, cutoff: str, *, limit: int) -> tuple[str, ...]:
            raise RuntimeError("database unavailable")

    expired = _tool_result(layout, "task_old", EXPIRED)
    research = layout.person_research_dir(None) / "task-1" / f"{_stamp(EXPIRED)}_web_q_abc"
    _file(research / "manifest.json", "{}")

    with caplog.at_level(logging.INFO):
        result = _run(layout, Broken())

    assert _kind(result, OutputKind.CONVERSATION_ARTIFACTS).failed
    assert not expired.exists() and not research.exists()
    sentence = result.instruction_sentence()
    assert "the cleanup failed (manager log event=output_retention_failed kind=conversation_artifacts)" in sentence
    assert "Report the failed cleanup to TeamArchitect." in sentence
    assert "event=output_retention_failed kind=conversation_artifacts" in caplog.text


# -- research results ---------------------------------------------------------------------


def test_research_results_go_by_their_dated_names_in_every_person_folder(layout: InstanceLayout) -> None:
    alice = layout.person_research_dir("alice")
    old_alice = alice / "task-1" / f"{_stamp(EXPIRED, micro=True)}_exa_search_q_0123456789ab"
    _file(old_alice / "manifest.json", "{}")
    _file(old_alice / "nested" / "page.md", "page")
    kept_alice = alice / "task-1" / f"{_stamp(KEPT)}_web_page_q_def0123456789"
    _file(kept_alice / "manifest.json", "{}")
    old_agents = layout.person_research_dir(None) / "task-2" / f"{_stamp(EXPIRED)}_web_q_ghi012345678"
    _file(old_agents / "manifest.json", "{}")
    # The scheme before result folders got a random ending: <stamp>_<kind>_<label>.
    older_scheme = alice / "task-3" / f"{_stamp(EXPIRED)}_search_weather-in-paris"
    _file(older_scheme / "manifest.json", "{}")
    # Not tool results: files directly in research/ and in a task folder stay.
    notes = _file(alice / "notes.md", "my notes", mtime=EXPIRED)
    task_note = _file(alice / "task-1" / "summary.md", "summary", mtime=EXPIRED)

    result = _run(layout)

    assert not old_alice.exists() and not old_agents.exists() and not older_scheme.exists()
    assert not old_agents.parent.exists() and not older_scheme.parent.exists(), "task folders this run emptied go"
    assert kept_alice.exists() and notes.exists() and task_note.exists()
    assert _kind(result, OutputKind.RESEARCH_RESULTS).items_removed == 3


def test_research_folders_without_the_tools_stamp_are_never_deleted(layout: InstanceLayout) -> None:
    """People keep their own folders under research/; their last change says nothing about a tool."""

    alice = layout.person_research_dir("alice")
    # An undated folder, untouched for months, with a file edited today.
    undated = alice / "my-project" / "notes"
    edited_today = _file(undated / "draft.md", "edited today", mtime=NOW)
    _touch(undated, EXPIRED)
    _touch(undated.parent, EXPIRED)
    # A nested folder a person made, old throughout.
    papers = alice / "reading-list" / "papers"
    paper = _file(papers / "paper.pdf", "pdf", mtime=EXPIRED)
    _touch(papers, EXPIRED)
    _touch(papers.parent, EXPIRED)
    # An empty folder a person made, and an old empty task folder.
    empty_own = alice / "ideas" / "later"
    empty_own.mkdir(parents=True)
    _touch(empty_own, EXPIRED)
    _touch(empty_own.parent, EXPIRED)
    empty_task = alice / "empty-task"
    empty_task.mkdir()
    _touch(empty_task, EXPIRED)

    result = _run(layout)

    assert edited_today.read_text(encoding="utf-8") == "edited today"
    assert paper.exists() and papers.is_dir()
    assert empty_own.is_dir() and empty_task.is_dir()
    counts = _kind(result, OutputKind.RESEARCH_RESULTS)
    assert (counts.items_removed, counts.files_removed, counts.folders_removed) == (0, 0, 0)


def test_links_in_research_folders_are_never_followed(layout: InstanceLayout, tmp_path: Path) -> None:
    protected = _file(layout.control_dir / "keep" / "control.yaml", "keep me", mtime=EXPIRED)
    _touch(protected.parent, EXPIRED)
    alice = layout.person_research_dir("alice")
    old = alice / "task-1" / f"{_stamp(EXPIRED)}_web_q_abc"
    _file(old / "manifest.json", "{}")
    (old / "planted-file").symlink_to(protected)
    (old / "planted-folder").symlink_to(protected.parent, target_is_directory=True)
    # A result folder that is a link, and a research folder that is a link.
    (alice / "task-1" / f"{_stamp(EXPIRED)}_linked_result_x").symlink_to(protected.parent, target_is_directory=True)
    layout.person_dir("bob").mkdir(parents=True)
    layout.person_research_dir("bob").symlink_to(protected.parent, target_is_directory=True)
    # An entry of users/ that is a link is not a person folder.
    (layout.users_dir / "not-a-person").symlink_to(layout.control_dir, target_is_directory=True)

    result = _run(layout)

    assert protected.read_text(encoding="utf-8") == "keep me"
    assert not old.exists(), "planted links inside an expired result go as links"
    assert (alice / "task-1" / f"{_stamp(EXPIRED)}_linked_result_x").is_symlink()
    assert layout.person_research_dir("bob").is_symlink()
    counts = _kind(result, OutputKind.RESEARCH_RESULTS)
    assert counts.items_removed == 1
    assert counts.files_removed == 1, "links are not counted as files"
    assert counts.skipped == 2


def test_links_in_saved_tool_results_are_never_followed(layout: InstanceLayout, tmp_path: Path) -> None:
    outside = tmp_path / "outside"
    _file(outside / "secret.txt", "keep me", mtime=EXPIRED)
    _touch(outside, EXPIRED)
    layout.tool_results_dir.mkdir(parents=True)
    (layout.tool_results_dir / "linked_task").symlink_to(outside, target_is_directory=True)

    result = _run(layout)

    assert (outside / "secret.txt").read_text(encoding="utf-8") == "keep me"
    assert (layout.tool_results_dir / "linked_task").is_symlink()
    assert _kind(result, OutputKind.TOOL_RESULTS).skipped == 1


# -- execution run and build folders --------------------------------------------------------


def test_old_run_and_build_folders_go_and_recent_ones_stay(layout: InstanceLayout) -> None:
    old_run = layout.execution_runs_dir / "run_1.abcd"
    _file(old_run / "entry.py", "print()", mtime=EXPIRED)
    _touch(old_run, EXPIRED)
    new_run = layout.execution_runs_dir / "run_2.efgh"
    _file(new_run / "entry.py", "print()", mtime=KEPT)
    _touch(new_run, KEPT)
    old_build = layout.execution_rust_builds_dir / ("a" * 64)
    _file(old_build / "main.rs", "fn main() {}", mtime=EXPIRED)
    _file(old_build / BUILD_LOCK_FILENAME, "", mtime=EXPIRED)
    _touch(old_build, EXPIRED)
    new_build = layout.execution_rust_builds_dir / ("b" * 64)
    _file(new_build / "main.rs", "fn main() {}", mtime=EXPIRED)
    _touch(new_build, KEPT)  # built again recently

    result = _run(layout)

    assert not old_run.exists() and not old_build.exists()
    assert new_run.exists() and new_build.exists()
    assert layout.execution_rust_builds_dir.is_dir()
    assert _kind(result, OutputKind.EXECUTION_RUNS).items_removed == 2


def test_a_build_folder_in_use_is_left_alone(layout: InstanceLayout) -> None:
    build = layout.execution_rust_builds_dir / ("c" * 64)
    lock_path = _file(build / BUILD_LOCK_FILENAME, "", mtime=EXPIRED)
    _touch(build, EXPIRED)
    with open(lock_path, "a+b") as lock:
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
        result = _run(layout)

    assert build.exists()
    assert _kind(result, OutputKind.EXECUTION_RUNS).skipped == 1


def _old_build(layout: InstanceLayout, digest: str) -> Path:
    build = layout.execution_rust_builds_dir / digest
    _file(build / BUILD_LOCK_FILENAME, "", mtime=EXPIRED)
    _file(build / "main.rs", "fn main() {}", mtime=EXPIRED)
    _file(build / "program", "old output", mtime=EXPIRED)
    _touch(build, EXPIRED)
    return build


def test_a_build_folders_lock_file_goes_last_whatever_the_listing_order(
    layout: InstanceLayout, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A build arriving while the folder is emptied waits on the old lock instead of losing its files.

    If the lock went first, a build arriving then would create a new lock,
    get it at once, write its source and output, and lose them to the
    cleanup's earlier listing (the critic's build-lock probe).
    """

    import old_file_cleanup
    from runtime_hosting.builds import _still_linked

    build = _old_build(layout, "d" * 64)
    real_listdir = os.listdir
    real_remove_entry = old_file_cleanup._remove_entry
    removed: list[str] = []
    builder: dict[str, bool] = {}

    def lock_first(folder):
        names = real_listdir(folder)
        if BUILD_LOCK_FILENAME in names:
            names.remove(BUILD_LOCK_FILENAME)
            names.insert(0, BUILD_LOCK_FILENAME)
        return names

    def remove_then_a_build_arrives(folder, name, shown, counts, *, depth):
        real_remove_entry(folder, name, shown, counts, depth=depth)
        removed.append(name)
        if name == BUILD_LOCK_FILENAME and not builder:
            # runtime_hosting.builds: make the folder, lock, write source and output.
            build.mkdir(parents=True, exist_ok=True)
            lock = os.open(build / BUILD_LOCK_FILENAME, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            builder["still_linked"] = _still_linked(lock, build / BUILD_LOCK_FILENAME)
            (build / "main.rs").write_text("fn main() { new(); }", encoding="utf-8")
            (build / "program").write_text("new output", encoding="utf-8")
            os.close(lock)

    monkeypatch.setattr(os, "listdir", lock_first)
    monkeypatch.setattr(old_file_cleanup, "_remove_entry", remove_then_a_build_arrives)

    result = _run(layout)

    assert removed[-1] == BUILD_LOCK_FILENAME and set(removed) == {BUILD_LOCK_FILENAME, "main.rs", "program"}
    assert builder["still_linked"] is True
    assert (build / "main.rs").read_text(encoding="utf-8") == "fn main() { new(); }"
    assert (build / "program").read_text(encoding="utf-8") == "new output"
    assert _kind(result, OutputKind.EXECUTION_RUNS).items_removed == 0, "the new build's folder stays"


def test_a_build_folder_built_again_before_the_lock_was_taken_stays(
    layout: InstanceLayout, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The age is checked again under the lock: a build may finish between the listing and the lock."""

    from custom_agents import output_retention

    build = _old_build(layout, "e" * 64)
    real_locked_now = output_retention._locked_now

    def a_build_finishes_first(lock):
        (build / "program").unlink()
        (build / "program").write_text("rebuilt", encoding="utf-8")
        return real_locked_now(lock)

    monkeypatch.setattr(output_retention, "_locked_now", a_build_finishes_first)

    result = _run(layout)

    assert (build / "program").read_text(encoding="utf-8") == "rebuilt"
    assert _kind(result, OutputKind.EXECUTION_RUNS).items_removed == 0


def test_an_old_build_folder_without_a_lock_file_goes_without_making_one(layout: InstanceLayout) -> None:
    build = layout.execution_rust_builds_dir / ("f" * 64)
    _file(build / "main.rs", "fn main() {}", mtime=EXPIRED)
    _touch(build, EXPIRED)

    result = _run(layout)

    assert not build.exists()
    assert _kind(result, OutputKind.EXECUTION_RUNS).items_removed == 1


def test_run_folders_are_never_deleted_younger_than_two_days(layout: InstanceLayout) -> None:
    """No run lasts longer than a day, so a short period never deletes a running one's folder."""

    running = layout.execution_runs_dir / "run_3.ijkl"
    _file(running / "entry.py", "print()", mtime=NOW - 1.5 * SECONDS_PER_DAY)
    _touch(running, NOW - 1.5 * SECONDS_PER_DAY)
    finished = layout.execution_runs_dir / "run_4.mnop"
    _file(finished / "entry.py", "print()", mtime=NOW - 2.5 * SECONDS_PER_DAY)
    _touch(finished, NOW - 2.5 * SECONDS_PER_DAY)

    _run(layout, days=1)

    assert running.exists()
    assert not finished.exists()


def test_a_link_in_place_of_the_execution_folder_is_not_followed(layout: InstanceLayout, tmp_path: Path) -> None:
    outside = tmp_path / "outside-runs"
    _file(outside / "run_1.abcd" / "entry.py", "keep", mtime=EXPIRED)
    _touch(outside / "run_1.abcd", EXPIRED)
    layout.agents_dir.mkdir(parents=True, exist_ok=True)
    layout.execution_runs_dir.symlink_to(outside, target_is_directory=True)

    result = _run(layout)

    assert (outside / "run_1.abcd" / "entry.py").exists()
    assert _kind(result, OutputKind.EXECUTION_RUNS).skipped == 1


# -- the maintenance run ------------------------------------------------------------------


@pytest.mark.asyncio
async def test_maintenance_deletes_old_outputs_and_reports_counts_and_bytes_per_kind(
    layout: InstanceLayout, store: ConversationStore
) -> None:
    _tool_result(layout, "task_old", datetime.now(timezone.utc).timestamp() - (DAYS + 1) * SECONDS_PER_DAY)
    _grace_ended(layout)
    manager = SimpleNamespace(
        id="manager_1",
        _logger=logging.getLogger("test.output_retention.manager"),
        instance_root=str(layout.root),
        workspace_dir=str(layout.workspaces_dir),
        output_retention_days=DAYS,
        _conversation_store=store,
    )

    sentence = await remove_old_outputs_for_maintenance(manager, "internal_improvement_1")

    assert not any(layout.tool_results_dir.iterdir())
    assert "written more than 90 days ago (tool_policy.output_retention_days)" in sentence
    assert "saved tool results: 1 results, 2 files," in sentence
    assert "long tool output saved from conversations: 0 records, 0 files, 0 bytes" in sentence
    assert "research results: 0 results" in sentence
    assert "execution run and build folders: 0 folders" in sentence
    assert "Include these counts in your run record" in sentence


@pytest.mark.asyncio
async def test_maintenance_outside_an_instance_layout_reports_that_it_did_not_run(tmp_path: Path) -> None:
    manager = SimpleNamespace(
        id="manager_1",
        _logger=logging.getLogger("test.output_retention.manager"),
        instance_root="",
        workspace_dir=str(tmp_path),
        output_retention_days=DAYS,
    )

    sentence = await remove_old_outputs_for_maintenance(manager, "internal_improvement_1")

    assert "did not run" in sentence


# -- the record of each run and the Status page -------------------------------------------


def test_each_run_is_recorded_and_a_failing_kind_keeps_the_last_success(layout: InstanceLayout) -> None:
    from custom_agents.output_retention import read_run_record, remove_old_outputs_and_record

    _tool_result(layout, "task_old", time.time() - (DAYS + 1) * SECONDS_PER_DAY)

    remove_old_outputs_and_record(layout, retention_days=DAYS, conversation_records=None)
    first = read_run_record(layout.output_retention_record_path)

    class Broken:
        def delete_artifacts_created_before(self, cutoff: str, *, limit: int) -> tuple[str, ...]:
            raise RuntimeError("database unavailable")

    remove_old_outputs_and_record(layout, retention_days=DAYS, conversation_records=Broken())
    second = read_run_record(layout.output_retention_record_path)

    tool_results = first["kinds"]["tool_results"]
    assert (tool_results["items"], tool_results["files"], tool_results["failed"]) == (1, 2, False)
    assert tool_results["bytes"] > len(BLOCK_TEXT)
    assert first["last_success_at"] == first["last_run_at"] and first["retention_days"] == DAYS
    assert second["kinds"]["conversation_artifacts"]["failed"] is True
    assert second["last_run_at"] != first["last_run_at"]
    assert second["last_success_at"] == first["last_success_at"], "a failed run is no success"
    assert second["first_recorded_at"] == first["first_recorded_at"]


def _lead(layout: InstanceLayout, **values):
    defaults = {
        "id": "manager_1", "named_agent_type": "TeamArchitect", "is_team_lead": True,
        "_logger": logging.getLogger("test.output_retention.manager"),
        "instance_root": str(layout.root), "workspace_dir": str(layout.workspaces_dir),
        "output_retention_days": DAYS, "scratch_retention_days": 21,
        "schedule_maintenance": {"internal_improvement_review_interval_seconds": 21600},
    }
    return SimpleNamespace(**{**defaults, **values})


def test_the_status_page_warns_when_no_cleanup_succeeded_for_two_days(layout: InstanceLayout) -> None:
    from custom_agents.output_retention import write_run_record
    from custom_agents.status_findings import agent_status_findings
    from custom_agents.output_retention import OutputRetentionResult

    path = layout.output_retention_record_path
    findings = lambda agent: [f for f in agent_status_findings(agent) if f.finding_id == "output_retention_overdue"]  # noqa: E731

    assert findings(_lead(layout)) == [], "nothing recorded yet"
    write_run_record(path, OutputRetentionResult(retention_days=DAYS), now=datetime.now(timezone.utc) - timedelta(days=1))
    assert findings(_lead(layout)) == []
    write_run_record(path, OutputRetentionResult(retention_days=DAYS), now=datetime.now(timezone.utc) - timedelta(days=3))

    [finding] = findings(_lead(layout))
    assert "last succeeded at" in finding.summary and "90 days" in finding.summary
    assert "maintenance schedule is on and not paused" in finding.action
    assert findings(_lead(layout, output_retention_days=0)) == [], "nothing to delete with retention off"
    worker = _lead(layout)
    worker.is_team_lead = False
    assert findings(worker) == [], "only the manager runs the cleanup"


def test_at_start_the_record_begins_and_a_schedule_that_is_off_is_logged(layout: InstanceLayout, caplog) -> None:
    from custom_agents.internal_improvement_runtime import note_trusted_cleanups_at_start
    from custom_agents.output_retention import overdue_since, read_run_record

    with caplog.at_level(logging.WARNING):
        note_trusted_cleanups_at_start(_lead(layout, schedule_maintenance={"internal_improvement_review_interval_seconds": 0}))

    record = read_run_record(layout.output_retention_record_path)
    assert record["first_recorded_at"] and record["last_success_at"] is None
    assert overdue_since(record, now=time.time() + 3 * SECONDS_PER_DAY) is not None, "a cleanup that never runs shows"
    assert "event=retention_cleanup_off reason=maintenance_schedule_off" in caplog.text
    caplog.clear()
    note_trusted_cleanups_at_start(_lead(layout))
    assert read_run_record(layout.output_retention_record_path) == record, "a restart keeps the record"
    assert "retention_cleanup_off" not in caplog.text


# -- the grace period before the first deletion ---------------------------------------------


def _grace_ended(layout: InstanceLayout) -> None:
    """A record saying retention became active more than the grace period ago."""

    started = datetime.now(timezone.utc) - timedelta(days=GRACE_DAYS + 1)
    path = layout.output_retention_record_path
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"active_since": started.isoformat(), "retention_days": DAYS}), encoding="utf-8")


def _every_kind(layout: InstanceLayout, store: ConversationStore) -> dict[str, Path]:
    """One expired output of each kind, plus a conversation artifact file no record names."""

    artifact = store.offload_large_text(
        conversation_id="conv_old", task_id="task_1", artifact_type="tool_output", raw_text="old output", summary_text="s"
    )
    _age_artifact(layout, artifact, EXPIRED)
    run = layout.execution_runs_dir / "run_1.abcd"
    _file(run / "entry.py", "print('run')", mtime=EXPIRED)
    _touch(run, EXPIRED)
    build = layout.execution_rust_builds_dir / ("a" * 64)
    _file(build / BUILD_LOCK_FILENAME, "", mtime=EXPIRED)
    _file(build / "main.rs", "fn main() {}", mtime=EXPIRED)
    _touch(build, EXPIRED)
    research = layout.person_research_dir("alice") / "task-1" / f"{_stamp(EXPIRED)}_web_q_0123456789ab"
    _file(research / "manifest.json", '{"research": true}')
    return {
        "tool result": _tool_result(layout, "task_old", EXPIRED),
        "artifact": Path(artifact["storage_path"]),
        "orphan": _file(layout.conversation_artifacts_dir / "conv_gone" / "orphan.txt", "orphan", mtime=EXPIRED),
        "research": research,
        "run": run,
        "build": build,
    }


def _graced(layout: InstanceLayout, store: ConversationStore, moment: float, *, days: int = DAYS):
    from custom_agents.output_retention import remove_old_outputs_and_record

    return remove_old_outputs_and_record(layout, retention_days=days, conversation_records=store, now=moment)


def _counts(result) -> dict[str, tuple[int, int, int]]:
    return {str(kind.kind): (kind.items_removed, kind.files_removed, kind.bytes_removed) for kind in result.kinds}


def test_nothing_is_deleted_during_the_grace_period_and_deletion_starts_after_it(
    layout: InstanceLayout, store: ConversationStore
) -> None:
    outputs = _every_kind(layout, store)

    first = _graced(layout, store, NOW)
    last_day = _graced(layout, store, NOW + GRACE_DAYS * SECONDS_PER_DAY - MINUTE)

    assert first.dry_run and last_day.dry_run
    assert first.deletes_from == pytest.approx(NOW + GRACE_DAYS * SECONDS_PER_DAY)
    assert all(path.exists() for path in outputs.values()), "nothing is deleted before day 7"
    assert len(_artifact_ids(layout)) == 1

    after = _graced(layout, store, NOW + GRACE_DAYS * SECONDS_PER_DAY + MINUTE)

    assert not after.dry_run and after.deletes_from is None
    assert not any(path.exists() for path in outputs.values())
    assert _artifact_ids(layout) == set()


def test_the_dry_run_counts_match_what_is_then_deleted(layout: InstanceLayout, store: ConversationStore) -> None:
    _every_kind(layout, store)
    _graced(layout, store, NOW)  # retention becomes active

    dry = _graced(layout, store, NOW + (GRACE_DAYS - 1) * SECONDS_PER_DAY)
    real = _graced(layout, store, NOW + GRACE_DAYS * SECONDS_PER_DAY + MINUTE)

    assert dry.dry_run and not real.dry_run
    assert _counts(dry) == _counts(real)
    assert all(items > 0 and files > 0 and size > 0 for items, files, size in _counts(real).values())
    assert (dry.items, dry.bytes) == (real.items, real.bytes)


def test_the_dry_run_counts_expired_artifacts_whose_files_changed_recently(
    layout: InstanceLayout, store: ConversationStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    from custom_agents import output_retention

    monkeypatch.setattr(output_retention, "RECORD_BATCH", 2)
    paths = []
    for index in range(3):
        artifact = store.offload_large_text(
            conversation_id="conv_old", task_id=f"task_{index}", artifact_type="tool_output",
            raw_text=f"saved output {index}", summary_text="summary",
        )
        _age_artifact(layout, artifact, EXPIRED)
        path = Path(artifact["storage_path"])
        _touch(path, NOW)
        paths.append(path)

    dry = remove_old_outputs(layout, retention_days=DAYS, conversation_records=store, now=NOW, dry_run=True)
    assert all(path.exists() for path in paths)
    real = remove_old_outputs(layout, retention_days=DAYS, conversation_records=store, now=NOW)

    assert _counts(dry) == _counts(real)
    artifacts = _kind(dry, OutputKind.CONVERSATION_ARTIFACTS)
    assert artifacts.items_removed == artifacts.files_removed == 3
    assert artifacts.bytes_removed == sum(len(f"saved output {index}") for index in range(3))
    assert not any(path.exists() for path in paths)


def test_the_grace_period_starts_again_after_retention_was_turned_off(
    layout: InstanceLayout, store: ConversationStore
) -> None:
    from custom_agents.output_retention import ensure_run_record

    _graced(layout, store, NOW)
    assert not _graced(layout, store, NOW + 8 * SECONDS_PER_DAY).dry_run
    _graced(layout, store, NOW + 9 * SECONDS_PER_DAY, days=0)  # turned off at a run
    outputs = _every_kind(layout, store)

    again = _graced(layout, store, NOW + 10 * SECONDS_PER_DAY)

    assert again.dry_run and again.deletes_from == pytest.approx(NOW + (10 + GRACE_DAYS) * SECONDS_PER_DAY)
    assert all(path.exists() for path in outputs.values())

    # Turned off and on again between two runs: the manager forgot the active period at its start.
    assert not _graced(layout, store, NOW + 18 * SECONDS_PER_DAY).dry_run
    ensure_run_record(layout.output_retention_record_path, retention_days=0)
    assert _graced(layout, store, NOW + 19 * SECONDS_PER_DAY).dry_run


def test_a_restart_or_a_shorter_period_does_not_start_the_grace_period_again(
    layout: InstanceLayout, store: ConversationStore
) -> None:
    from custom_agents.output_retention import ensure_run_record

    _graced(layout, store, NOW)
    ensure_run_record(layout.output_retention_record_path, retention_days=DAYS)  # an ordinary restart
    outputs = _every_kind(layout, store)

    after = _graced(layout, store, NOW + GRACE_DAYS * SECONDS_PER_DAY + MINUTE)

    assert not after.dry_run and not any(path.exists() for path in outputs.values())
    # Shortening an active period is the administrator's explicit choice: it applies at once.
    assert not _graced(layout, store, NOW + 9 * SECONDS_PER_DAY, days=30).dry_run


def test_the_notice_says_when_how_much_why_and_what_administrators_can_do(
    layout: InstanceLayout, store: ConversationStore
) -> None:
    from custom_agents.output_retention import grace_notice

    _every_kind(layout, store)
    result = _graced(layout, store, NOW)

    title, text = grace_notice(result)

    when = datetime.fromtimestamp(NOW + GRACE_DAYS * SECONDS_PER_DAY, timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    assert when in title and when in text and "90 days" in title
    assert f"about {result.items} items (" in text and result.items == 5
    for kind in ("oversized tool results", "conversation artifacts", "research results", "execution run folders"):
        assert kind in text
    assert "deleted automatically once they are 90 days old" in text
    assert "can produce again" in text and "growing without limit" in text
    assert "Chat transcripts, task links, conversation summaries and completed tasks are kept" in text
    assert "keep this default" in text and "Settings > Team > File Retention" in text and "set it to 0" in text
    assert "applies after the next restart" in text


@pytest.mark.asyncio
async def test_administrators_are_told_once_per_grace_period_and_the_status_page_shows_it_until_deletion(
    layout: InstanceLayout, store: ConversationStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    from custom_agents import access_runtime
    from custom_agents.status_findings import agent_status_findings

    _every_kind(layout, store)
    sent: list[dict] = []
    monkeypatch.setattr(access_runtime, "send_runtime_notice_once", lambda agent, **notice: sent.append(notice) or True)
    manager = _lead(layout, _conversation_store=store)
    grace = lambda: [f for f in agent_status_findings(manager) if f.finding_id == "output_retention_grace_period"]  # noqa: E731

    sentence = await remove_old_outputs_for_maintenance(manager, "internal_improvement_1")
    await remove_old_outputs_for_maintenance(manager, "internal_improvement_1")

    assert "grace period, so nothing was deleted" in sentence
    assert len({notice["notice_id"] for notice in sent}) == 1, "one notice per grace period"
    assert sent[0]["roles"] == ("admin",) and str(sent[0]["kind"]) == "saved_output_retention"
    [finding] = grace()
    assert "about 5 items" in finding.summary and "Settings → Team → File Retention" in finding.action

    _grace_ended(layout)
    await remove_old_outputs_for_maintenance(manager, "internal_improvement_1")
    assert grace() == [], "it clears once deletion starts"


# -- readers of a deleted conversation artifact ---------------------------------------------


def _retention(layout: InstanceLayout, days: int = DAYS):
    from custom_agents.output_retention import SavedOutputRetention

    return SavedOutputRetention.for_layout(layout, days)


def _turn_with_artifacts(kept_path: Path, deleted_path: Path, *, unchanged: str = "conv_1/reworded.txt") -> dict:
    """A turn whose offloaded outputs were saved; the retention deleted one since.

    The third entry names its output the way an LLM summary may reword it,
    which no reader may call deleted.
    """

    old = datetime.fromtimestamp(time.time() - (DAYS + 30) * SECONDS_PER_DAY, timezone.utc).isoformat()
    manifest = [
        {"artifact_type": "tool_output", "storage_path": str(kept_path), "summary_text": "kept output summary",
         "created_at": old},
        {"artifact_type": "tool_output", "storage_path": str(deleted_path), "summary_text": "deleted output summary",
         "created_at": old},
        {"artifact_type": "tool_output", "storage_path": unchanged, "summary_text": "reworded output summary"},
    ]
    return {
        "turn_index": 1,
        "task_id": "task_1",
        "problem_text": "Look this up.",
        "final_answer_text": "Here it is.",
        "tool_artifact_manifest": manifest,
        "artifact_summary": manifest,
    }


@pytest.fixture
def artifact_paths(layout: InstanceLayout) -> tuple[Path, Path]:
    kept = _file(layout.conversation_artifacts_dir / "conv_1" / "task_1_artifact_kept.txt", "kept")
    deleted = layout.conversation_artifacts_dir / "conv_1" / "task_1_artifact_deleted.txt"
    return kept, deleted


def test_a_deleted_output_is_marked_and_its_summary_kept(layout: InstanceLayout, artifact_paths) -> None:
    from custom_agents.output_retention import DELETED_OUTPUT_FLAG, with_deleted_outputs_marked

    kept, deleted = artifact_paths
    turn = _turn_with_artifacts(kept, deleted)

    marked = with_deleted_outputs_marked([*turn["tool_artifact_manifest"], "not an entry"], _retention(layout))

    assert marked[0] == turn["tool_artifact_manifest"][0]
    assert marked[1]["summary_text"] == "deleted output summary"
    assert marked[1][DELETED_OUTPUT_FLAG] is True
    assert "deleted under the team's retention policy" in marked[1]["storage_path"]
    assert "kept 90 days" in marked[1]["storage_path"]
    assert marked[2] == turn["tool_artifact_manifest"][2]
    assert len(marked) == 3
    assert turn["tool_artifact_manifest"][1]["storage_path"] == str(deleted), "the stored turn is not changed"
    assert with_deleted_outputs_marked(None, _retention(layout)) == []
    assert with_deleted_outputs_marked(turn["tool_artifact_manifest"], None) == turn["tool_artifact_manifest"]


def test_only_outputs_the_retention_deleted_are_called_deleted(layout: InstanceLayout, tmp_path: Path) -> None:
    """Missing alone is not enough: the period, the folder and the age must all say the cleanup deleted it."""

    retention = _retention(layout)
    gone_artifact = layout.conversation_artifacts_dir / "conv_1" / "task_1_artifact_gone.txt"
    gone_result = layout.tool_results_dir / "task-1" / f"{_stamp(EXPIRED)}_tooluse_1_shell_abc" / "manifest.json"
    present = _file(layout.conversation_artifacts_dir / "conv_1" / "task_1_artifact_here.txt", "here")
    old, recent = _iso(EXPIRED), _iso(KEPT)

    # Deleted: inside a cleaned folder, gone, and old (or of unknown age).
    assert retention.deleted(str(gone_artifact), created_at=old, now=NOW)
    assert retention.deleted(str(gone_result), now=NOW)
    # Not deleted by the retention:
    not_deleted = {
        "period 0": (_retention(layout, 0), str(gone_artifact), old),
        "relative path": (retention, "dbs/conversation_artifacts/conv_1/task_1_artifact_gone.txt", old),
        "placeholder": (retention, "(inline)", old),
        "url": (retention, "https://example.org/page", old),
        "reworded by a summary": (retention, "conversation artifact of task_1", old),
        "outside the cleaned folders": (retention, str(tmp_path / "elsewhere" / "output.txt"), old),
        "older layout": (retention, str(layout.root / "workspace" / "runtime" / "conversation_artifacts" / "x.txt"), old),
        "escapes the folder": (retention, f"{layout.conversation_artifacts_dir}/../../outside.txt", old),
        "recent": (retention, str(gone_artifact), recent),
        "still there": (retention, str(present), old),
    }
    for case, (policy, path, created_at) in not_deleted.items():
        assert not policy.deleted(path, created_at=created_at, now=NOW), case


def test_prompts_and_summaries_say_a_deleted_output_was_deleted(layout: InstanceLayout, artifact_paths) -> None:
    from custom_agents.context_engine.thread_renderer import (
        BudgetedThreadContextRenderer,
        BudgetedThreadContextRendererConfig,
    )
    from custom_agents.conversation_compaction.base import ConversationCompactionConfig
    from custom_agents.conversation_compaction.heuristic_compactor import HeuristicSlidingWindowCompactor
    from custom_agents.conversation_compaction.llm_compactor import LLMSummarizingConversationCompactor
    from custom_agents.conversation_compaction.noop_compactor import NoOpConversationCompactor
    from custom_tools.providers.context_memory import _conversation_turn_summary_payload

    kept, deleted = artifact_paths
    turn = _turn_with_artifacts(kept, deleted)
    retention = _retention(layout)
    config = ConversationCompactionConfig(saved_outputs=retention)
    heuristic = HeuristicSlidingWindowCompactor(config)
    renderer = BudgetedThreadContextRenderer(config=BudgetedThreadContextRendererConfig(saved_outputs=retention))
    rendered = {
        "thread renderer": renderer._render_artifacts(turn["artifact_summary"]),
        "heuristic turn": heuristic._render_raw_turn(turn),
        "heuristic synopsis": json.dumps(heuristic._merge_synopsis({}, [turn])),
        "noop turn": NoOpConversationCompactor(config)._render_raw_turn(turn),
        "llm input": json.dumps(LLMSummarizingConversationCompactor._turn_payload(heuristic, turn)),
        "conversation tool": json.dumps(_conversation_turn_summary_payload(turn, retention)),
    }

    for reader, text in rendered.items():
        assert "deleted under the team's retention policy" in text, reader
        assert str(deleted) not in text, reader
        assert str(kept) in text, reader
        assert "conv_1/reworded.txt" in text, reader  # not a path the cleanup deletes: shown as stored
        assert "deleted output summary" in text, reader  # what the output was about stays


def test_without_the_retention_policy_readers_show_references_as_stored(artifact_paths) -> None:
    from custom_agents.context_engine.thread_renderer import BudgetedThreadContextRenderer

    kept, deleted = artifact_paths
    text = BudgetedThreadContextRenderer()._render_artifacts(_turn_with_artifacts(kept, deleted)["artifact_summary"])

    assert str(deleted) in text and "deleted under the team's retention policy" not in text
