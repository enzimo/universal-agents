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
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

from custom_agents.conversation_store import ConversationStore
from custom_agents.internal_improvement_runtime import remove_old_outputs_for_maintenance
from custom_agents.output_retention import (
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
    future = layout.workspaces_dir / "agents" / "research" / "task-1" / f"{_stamp(NOW + 400 * SECONDS_PER_DAY)}_web_x_abc"
    _file(future / "manifest.json", "{}", mtime=EXPIRED)
    _touch(future, EXPIRED)

    _run(layout)

    assert not future.exists(), "a future date in an agent-written name cannot keep a folder forever"


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
    old_alice = alice / "task-1" / f"{_stamp(EXPIRED, micro=True)}_exa_search_q_abc"
    _file(old_alice / "manifest.json", "{}")
    _file(old_alice / "nested" / "page.md", "page")
    kept_alice = alice / "task-1" / f"{_stamp(KEPT)}_web_page_q_def"
    _file(kept_alice / "manifest.json", "{}")
    old_agents = layout.person_research_dir(None) / "task-2" / f"{_stamp(EXPIRED)}_web_q_ghi"
    _file(old_agents / "manifest.json", "{}")
    undated_old = alice / "task-3" / "older-layout-folder"
    _file(undated_old / "result.json", "{}", mtime=EXPIRED)
    _touch(undated_old, EXPIRED)
    # Not tool results: files directly in research/ and in a task folder stay.
    notes = _file(alice / "notes.md", "my notes", mtime=EXPIRED)
    task_note = _file(alice / "task-1" / "summary.md", "summary", mtime=EXPIRED)

    result = _run(layout)

    assert not old_alice.exists() and not old_agents.exists() and not undated_old.exists()
    assert not old_agents.parent.exists() and not undated_old.parent.exists()
    assert kept_alice.exists() and notes.exists() and task_note.exists()
    assert _kind(result, OutputKind.RESEARCH_RESULTS).items_removed == 3


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


# -- readers of a deleted conversation artifact ---------------------------------------------


def _turn_with_artifacts(kept_path: Path, deleted_path: Path) -> dict:
    """A turn whose offloaded outputs were saved; one was deleted under the retention policy since."""

    manifest = [
        {"artifact_type": "tool_output", "storage_path": str(kept_path), "summary_text": "kept output summary"},
        {"artifact_type": "tool_output", "storage_path": str(deleted_path), "summary_text": "deleted output summary"},
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


def test_a_deleted_output_is_marked_and_its_summary_kept(artifact_paths) -> None:
    from custom_agents.output_retention import DELETED_OUTPUT_FLAG, with_deleted_outputs_marked

    kept, deleted = artifact_paths
    turn = _turn_with_artifacts(kept, deleted)

    marked = with_deleted_outputs_marked([*turn["tool_artifact_manifest"], "not an entry"])

    assert marked[0] == turn["tool_artifact_manifest"][0]
    assert marked[1]["summary_text"] == "deleted output summary"
    assert marked[1][DELETED_OUTPUT_FLAG] is True
    assert "deleted under the team's retention policy" in marked[1]["storage_path"]
    assert len(marked) == 2
    assert turn["tool_artifact_manifest"][1]["storage_path"] == str(deleted), "the stored turn is not changed"
    assert with_deleted_outputs_marked(None) == []


def test_prompts_and_summaries_say_a_deleted_output_was_deleted(artifact_paths) -> None:
    from custom_agents.context_engine.thread_renderer import BudgetedThreadContextRenderer
    from custom_agents.conversation_compaction.base import ConversationCompactionConfig
    from custom_agents.conversation_compaction.heuristic_compactor import HeuristicSlidingWindowCompactor
    from custom_agents.conversation_compaction.llm_compactor import LLMSummarizingConversationCompactor
    from custom_agents.conversation_compaction.noop_compactor import NoOpConversationCompactor
    from custom_tools.providers.context_memory import _conversation_turn_summary_payload

    kept, deleted = artifact_paths
    turn = _turn_with_artifacts(kept, deleted)
    heuristic = HeuristicSlidingWindowCompactor(ConversationCompactionConfig())
    rendered = {
        "thread renderer": BudgetedThreadContextRenderer()._render_artifacts(turn["artifact_summary"]),
        "heuristic turn": heuristic._render_raw_turn(turn),
        "heuristic synopsis": json.dumps(heuristic._merge_synopsis({}, [turn])),
        "noop turn": NoOpConversationCompactor._render_raw_turn(turn),
        "llm input": json.dumps(LLMSummarizingConversationCompactor._turn_payload(heuristic, turn)),
        "conversation tool": json.dumps(_conversation_turn_summary_payload(turn)),
    }

    for reader, text in rendered.items():
        assert "deleted under the team's retention policy" in text, reader
        assert str(deleted) not in text, reader
        assert str(kept) in text, reader
        assert "deleted output summary" in text, reader  # what the output was about stays
