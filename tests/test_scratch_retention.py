"""Trusted cleanup of scratch folders (custom_agents.scratch_retention).

Old files are deleted from every users/*/scratch and agents/scratch; new files,
everything outside scratch/, and anything reached through a link stay.
"""

from __future__ import annotations

import logging
import os
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from custom_agents.internal_improvement_runtime import prune_scratch_for_maintenance
from custom_agents.scratch_retention import SECONDS_PER_DAY, prune_scratch_folders
from runtime_settings.instance_layout import InstanceLayout

NOW = time.time()
OLD = NOW - 30 * SECONDS_PER_DAY
RECENT = NOW - 1 * SECONDS_PER_DAY


def _file(path: Path, data: str = "x", *, mtime: float = OLD) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(data, encoding="utf-8")
    os.utime(path, (mtime, mtime))
    return path


def _age_folder(path: Path, mtime: float = OLD) -> None:
    os.utime(path, (mtime, mtime))


@pytest.fixture
def layout(tmp_path: Path) -> InstanceLayout:
    layout = InstanceLayout.at(tmp_path / "instance")
    layout.ensure_directories()
    return layout


def test_old_scratch_files_are_removed_and_new_ones_kept(layout: InstanceLayout) -> None:
    alice = layout.person_scratch_dir("alice")
    old = _file(alice / "draft.md", "old draft")
    old_sent = _file(alice / "sent" / "outgoing_1" / "chart.png", "png")
    recent = _file(alice / "notes.md", mtime=RECENT)
    agents_old = _file(layout.person_scratch_dir(None) / "run.log")
    # The same names outside scratch/ are never touched.
    outside_person = _file(layout.person_dir("alice") / "draft.md")
    outside_team = _file(layout.team_dir / "scratch" / "shared.md")

    result = prune_scratch_folders(layout.workspaces_dir, retention_days=14, now=NOW)

    assert not old.exists()
    assert not old_sent.exists()
    assert not agents_old.exists()
    assert recent.exists()
    assert outside_person.exists()
    assert outside_team.exists()
    # Folders emptied by the cleanup go; the scratch/ folders themselves stay.
    assert not (alice / "sent").exists()
    assert alice.is_dir()
    assert layout.person_scratch_dir(None).is_dir()
    assert result.files_removed == 3
    assert result.bytes_removed == len("old draft") + len("png") + len("x")
    assert result.folders_removed == 2
    assert result.scratch_folders == 2


def test_links_in_scratch_are_never_followed(layout: InstanceLayout, caplog) -> None:
    alice = layout.person_scratch_dir("alice")
    alice.mkdir(parents=True)
    protected = _file(layout.control_dir / "old-control.yaml", "keep me")
    protected_folder_file = _file(layout.team_dir / "reports" / "q1.pdf", "keep me too")
    _age_folder(layout.team_dir / "reports")
    (alice / "control-link").symlink_to(protected)
    (alice / "folder-link").symlink_to(layout.team_dir / "reports", target_is_directory=True)
    os.utime(alice / "control-link", (OLD, OLD), follow_symlinks=False)

    with caplog.at_level(logging.INFO):
        result = prune_scratch_folders(layout.workspaces_dir, retention_days=14, now=NOW)

    assert protected.read_text(encoding="utf-8") == "keep me"
    assert protected_folder_file.exists()
    assert (alice / "control-link").is_symlink()
    assert (alice / "folder-link").is_symlink()
    assert result.files_removed == 0
    assert result.skipped == 2
    assert "event=scratch_prune_skipped reason=not_a_regular_file_or_folder" in caplog.text
    assert "event=scratch_pruned" in caplog.text


def test_a_scratch_folder_that_is_a_link_is_skipped(layout: InstanceLayout) -> None:
    elsewhere = _file(layout.control_dir / "keep" / "old.txt")
    layout.person_dir("alice").mkdir(parents=True)
    layout.person_scratch_dir("alice").symlink_to(elsewhere.parent, target_is_directory=True)
    # A link directly in users/ is not a person folder.
    (layout.users_dir / "not-a-person").symlink_to(layout.control_dir, target_is_directory=True)

    result = prune_scratch_folders(layout.workspaces_dir, retention_days=14, now=NOW)

    assert elsewhere.exists()
    assert result.skipped == 2
    assert result.files_removed == 0


def test_zero_days_turns_the_cleanup_off(layout: InstanceLayout) -> None:
    old = _file(layout.person_scratch_dir("alice") / "draft.md")

    result = prune_scratch_folders(layout.workspaces_dir, retention_days=0, now=NOW)

    assert old.exists()
    assert result.files_removed == 0
    assert "turned off" in result.instruction_sentence()


@pytest.mark.asyncio
async def test_maintenance_cleans_scratch_and_reports_the_counts(layout: InstanceLayout) -> None:
    old = _file(layout.person_scratch_dir("alice") / "draft.md", "draft")
    manager = SimpleNamespace(
        id="manager_1",
        _logger=logging.getLogger("test.scratch.manager"),
        instance_root=str(layout.root),
        workspace_dir=str(layout.workspaces_dir),
        scratch_retention_days=14,
    )

    sentence = await prune_scratch_for_maintenance(manager, "internal_improvement_1")

    assert not old.exists()
    assert "removed 1 files (5 bytes)" in sentence
    assert "14 days" in sentence


@pytest.mark.asyncio
async def test_maintenance_outside_an_instance_layout_reports_that_it_did_not_run(tmp_path: Path) -> None:
    manager = SimpleNamespace(
        id="manager_1",
        _logger=logging.getLogger("test.scratch.manager"),
        instance_root="",
        workspace_dir=str(tmp_path),
        scratch_retention_days=14,
    )

    sentence = await prune_scratch_for_maintenance(manager, "internal_improvement_1")

    assert "did not run" in sentence
