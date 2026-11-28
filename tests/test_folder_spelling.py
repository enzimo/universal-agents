"""Unit tests for exact on-disk spelling and file-path folder-grant refusal (access_control.folders)."""

from __future__ import annotations

import os
import time
import unicodedata
from pathlib import Path

import pytest

from access_control import folders
from access_control.folders import (
    DataFolder,
    FolderGrantOnFileError,
    FolderIdError,
    FolderRoots,
    PathSpellingError,
    check_exact_spelling,
    check_folder_id_spelling,
    check_folder_target,
    clear_spelling_cache,
    folder_grant_file_refusal,
)
from tests.folder_spelling_support import CountingPosixFS, SimulatedFS, fake_stat_with_dev


@pytest.fixture(autouse=True)
def _clean_spelling_cache():
    clear_spelling_cache()
    yield
    clear_spelling_cache()


def _make_roots(tmp_path: Path) -> tuple[FolderRoots, Path, Path]:
    workspaces = tmp_path / "workspaces"
    (workspaces / "team" / "finance").mkdir(parents=True)
    (workspaces / "team" / "finance" / "payroll.csv").write_text("secret\n", encoding="utf-8")
    (workspaces / "agents" / "run").mkdir(parents=True)
    (workspaces / "agents" / "run" / "log.txt").write_text("ok\n", encoding="utf-8")
    (workspaces / "users" / "alice").mkdir(parents=True)
    (workspaces / "users" / "alice" / "secret.pdf").write_text("private\n", encoding="utf-8")
    data_dir = tmp_path / "external_reports"
    data_dir.mkdir()
    (data_dir / "q1.csv").write_text("1,2\n", encoding="utf-8")
    roots = FolderRoots(
        workspaces_dir=workspaces,
        data_folders=(DataFolder(name="reports", path=data_dir, writable=True),),
    )
    return roots, workspaces, data_dir


def test_case_insensitive_spelling_refuses_mismatched_components_and_reports_disk_name(tmp_path: Path) -> None:
    roots, _workspaces, _data_dir = _make_roots(tmp_path)
    sim = SimulatedFS(case_insensitive=True)
    fs = sim.as_spelling_fs()

    # Exact spelling succeeds and consults the seam.
    check_folder_id_spelling(roots, "team/finance/payroll.csv", fs=fs)
    assert "payroll.csv" in sim.stat_calls

    # Single mismatched component reports exact disk path.
    with pytest.raises(PathSpellingError) as exc_info:
        check_folder_id_spelling(roots, "team/Finance/payroll.csv", fs=fs)
    assert exc_info.value.typed == "team/Finance/payroll.csv"
    assert exc_info.value.disk == "team/finance/payroll.csv"
    assert exc_info.value.unicode_only is False
    assert "team/finance/payroll.csv" in str(exc_info.value)

    # Multiple mismatched components report the full corrected path.
    with pytest.raises(PathSpellingError) as multi:
        check_folder_id_spelling(roots, "team/Finance/PAYROLL.CSV", fs=fs)
    assert multi.value.disk == "team/finance/payroll.csv"

    # Misspelled parent with non-existent child still refuses and preserves child spelling.
    with pytest.raises(PathSpellingError) as new_in_bad_parent:
        check_folder_id_spelling(roots, "team/Finance/new_file.csv", fs=fs)
    assert new_in_bad_parent.value.disk == "team/finance/new_file.csv"

    # New file with any spelling inside a correctly spelled folder succeeds.
    check_folder_id_spelling(roots, "team/finance/NewReport.CSV", fs=fs)


def test_data_folder_spelling_refusal_reports_logical_id_without_host_path(tmp_path: Path) -> None:
    roots, _workspaces, data_dir = _make_roots(tmp_path)
    sim = SimulatedFS(case_insensitive=True)
    fs = sim.as_spelling_fs()

    with pytest.raises(PathSpellingError) as exc_info:
        check_folder_id_spelling(roots, "data/reports/Q1.csv", fs=fs)
    assert exc_info.value.typed == "data/reports/Q1.csv"
    assert exc_info.value.disk == "data/reports/q1.csv"
    assert "data/reports/q1.csv" in str(exc_info.value)
    assert str(data_dir) not in str(exc_info.value)
    assert "Q1.csv" in sim.stat_calls


def test_unicode_normalization_mismatch_refused_on_case_sensitive_apfs_simulation(tmp_path: Path) -> None:
    roots, workspaces, _data_dir = _make_roots(tmp_path)
    nfc_name = unicodedata.normalize("NFC", "café.txt")
    nfd_name = unicodedata.normalize("NFD", "café.txt")
    assert nfc_name != nfd_name
    (workspaces / "team" / "finance" / nfc_name).write_text("accent\n", encoding="utf-8")

    # macOS case-sensitive APFS: case-sensitive, yet Unicode-normalizing.
    sim = SimulatedFS(case_insensitive=False, unicode_normalizing=True)
    fs = sim.as_spelling_fs()

    check_folder_id_spelling(roots, f"team/finance/{nfc_name}", fs=fs)
    with pytest.raises(PathSpellingError) as exc_info:
        check_folder_id_spelling(roots, f"team/finance/{nfd_name}", fs=fs)
    assert exc_info.value.unicode_only is True
    assert exc_info.value.disk == f"team/finance/{nfc_name}"
    assert "Unicode normalization" in str(exc_info.value)
    assert nfd_name in sim.stat_calls


def test_non_ascii_first_component_does_not_poison_case_probe_cache(tmp_path: Path) -> None:
    """C02: names like 'Straße' or 'ﬁle' use ASCII-only case swap so case-insensitive devices are detected."""

    workspaces = tmp_path / "workspaces"
    data_dir = tmp_path / "host_share"
    data_dir.mkdir(parents=True)
    for special in ("Straße", "ﬁle"):
        (data_dir / special).mkdir()
        (data_dir / special / "payroll.csv").write_text("x\n", encoding="utf-8")

    for special in ("Straße", "ﬁle"):
        clear_spelling_cache()
        sim = SimulatedFS(case_insensitive=True)
        fs = sim.as_spelling_fs()
        roots = FolderRoots(workspaces_dir=workspaces, data_folders=(DataFolder(name="share", path=data_dir),))

        # First lookup starts with the non-ASCII component (where str.swapcase() would expand).
        check_folder_id_spelling(roots, f"data/share/{special}/payroll.csv", fs=fs)
        assert sim.list_calls >= 1

        # Later misspelling on the same device must still be refused.
        with pytest.raises(PathSpellingError) as exc_info:
            check_folder_id_spelling(roots, f"data/share/{special}/Payroll.csv", fs=fs)
        assert exc_info.value.disk == f"data/share/{special}/payroll.csv"


def test_non_ascii_name_without_ascii_letters_lists_when_device_unknown(tmp_path: Path) -> None:
    """C02: when a non-ASCII component has no ASCII letters, 'unknown means list' applies."""

    data_dir = tmp_path / "share"
    data_dir.mkdir()
    (data_dir / "äöü").write_text("x\n", encoding="utf-8")
    sim = SimulatedFS(case_insensitive=True)
    fs = sim.as_spelling_fs()

    with pytest.raises(PathSpellingError) as exc_info:
        check_exact_spelling(data_dir, ("ÄÖÜ",), display_prefix="data/share", fs=fs)
    assert exc_info.value.disk == "data/share/äöü"
    assert sim.list_calls == 1
    assert folders._fs_case_insensitive == {}


def test_concurrent_deletion_during_case_probe_leaves_cache_unset(tmp_path: Path) -> None:
    """C02: deleting an entry between stat(part) and stat(swapped) must not cache False."""

    workspaces = tmp_path / "workspaces"
    team = workspaces / "team"
    team.mkdir(parents=True)
    ephemeral = team / "ephemeral.txt"
    ephemeral.write_text("bye\n", encoding="utf-8")
    (team / "secret.txt").write_text("keep\n", encoding="utf-8")

    deleted = False

    def delete_on_probe(name: str, _dir_fd: int) -> None:
        nonlocal deleted
        if name == "EPHEMERAL.TXT" and not deleted:
            deleted = True
            ephemeral.unlink()

    # Probe fails because the entry disappeared mid-probe; device must remain uncached.
    sim_race = SimulatedFS(case_insensitive=True, on_stat=delete_on_probe)
    check_exact_spelling(team, ("ephemeral.txt",), display_prefix="team", fs=sim_race.as_spelling_fs())
    assert deleted is True
    assert folders._fs_case_insensitive == {}

    # Next check on the same device probes fresh and still catches misspellings.
    sim_after = SimulatedFS(case_insensitive=True)
    with pytest.raises(PathSpellingError) as exc_info:
        check_exact_spelling(team, ("Secret.txt",), display_prefix="team", fs=sim_after.as_spelling_fs())
    assert exc_info.value.disk == "team/secret.txt"


def test_mount_point_entry_does_not_cache_mounted_device_as_case_sensitive(tmp_path: Path) -> None:
    """C02: sensitivity is keyed on fstat(parent_dir).st_dev, not the child mount-point's st_dev."""

    workspaces = tmp_path / "workspaces"
    mounted = workspaces / "team" / "mounted"
    mounted.mkdir(parents=True)
    (mounted / "secret.txt").write_text("host\n", encoding="utf-8")

    parent_dev = 10_001
    mounted_dev = 20_002
    mounted_ino = os.stat(mounted).st_ino
    mounted_fds: set[int] = set()

    def fstat_override(dir_fd: int, info: os.stat_result) -> os.stat_result:
        if info.st_ino == mounted_ino:
            mounted_fds.add(dir_fd)
            return fake_stat_with_dev(info, mounted_dev)
        return fake_stat_with_dev(info, parent_dev)

    def stat_override(_name: str, _dir_fd: int, info: os.stat_result) -> os.stat_result:
        if info.st_ino == mounted_ino:
            return fake_stat_with_dev(info, mounted_dev)
        return info

    def is_ci_for_fd(dir_fd: int) -> bool:
        return dir_fd in mounted_fds

    sim = SimulatedFS(
        case_insensitive=False,
        fstat_override=fstat_override,
        stat_override=stat_override,
        case_insensitive_for_fd=is_ci_for_fd,
    )
    roots = FolderRoots(workspaces_dir=workspaces)

    # First walk reaches mounted (child st_dev=20_002) from case-sensitive team (dir st_dev=10_001).
    with pytest.raises(PathSpellingError) as exc_info:
        check_folder_id_spelling(roots, "team/mounted/Secret.txt", fs=sim.as_spelling_fs())
    assert exc_info.value.disk == "team/mounted/secret.txt"
    assert folders._fs_case_insensitive.get(parent_dev) is False
    assert folders._fs_case_insensitive.get(mounted_dev) is True


def test_directory_name_cache_avoids_repeated_listings_for_exact_hits_and_relists_on_mismatch(tmp_path: Path) -> None:
    """C08: bounded directory-name cache serves exact hits when mtime is old enough and re-lists mismatches."""

    roots, workspaces, _data_dir = _make_roots(tmp_path)
    # Use a clock safely in the future (10s past real mtime) so the directory qualifies for caching.
    future_now = time.time_ns() + 10_000_000_000
    sim = SimulatedFS(case_insensitive=True, now_ns=lambda: future_now)
    fs = sim.as_spelling_fs()

    check_folder_id_spelling(roots, "team/finance/payroll.csv", fs=fs)
    first_lists = sim.list_calls
    assert first_lists >= 1

    # Repeated exact-spelling checks reuse the cached directory names with zero extra listings.
    for _ in range(10):
        check_folder_id_spelling(roots, "team/finance/payroll.csv", fs=fs)
    assert sim.list_calls == first_lists

    # A misspelling always re-lists fresh and refuses.
    with pytest.raises(PathSpellingError) as exc_info:
        check_folder_id_spelling(roots, "team/finance/Payroll.csv", fs=fs)
    assert exc_info.value.disk == "team/finance/payroll.csv"
    assert sim.list_calls > first_lists

    # Recent directories (within 2s of mtime) are not cached.
    clear_spelling_cache()
    recent_now = os.stat(workspaces / "team" / "finance").st_mtime_ns + 100_000_000
    sim_recent = SimulatedFS(case_insensitive=True, now_ns=lambda: recent_now)
    check_folder_id_spelling(roots, "team/finance/payroll.csv", fs=sim_recent.as_spelling_fs())
    lists_after_first = sim_recent.list_calls
    check_folder_id_spelling(roots, "team/finance/payroll.csv", fs=sim_recent.as_spelling_fs())
    assert sim_recent.list_calls > lists_after_first


def test_case_sensitive_filesystem_does_not_list_directories_per_call(tmp_path: Path) -> None:
    """S4: on this case-sensitive ext4 filesystem, repeated checks make zero directory listings."""

    roots, workspaces, _data_dir = _make_roots(tmp_path)
    nfc_name = unicodedata.normalize("NFC", "café.txt")
    (workspaces / "team" / "finance" / nfc_name).write_text("accent\n", encoding="utf-8")

    counting = CountingPosixFS()
    fs = counting.as_spelling_fs()

    for _ in range(50):
        check_folder_id_spelling(roots, "team/finance/payroll.csv", fs=fs)
        check_folder_id_spelling(roots, f"team/finance/{nfc_name}", fs=fs)
        check_folder_id_spelling(roots, "data/reports/q1.csv", fs=fs)

    assert counting.list_calls == 0
    assert "payroll.csv" in counting.stat_calls


def test_hard_links_on_case_sensitive_fs_do_not_cache_device_as_case_insensitive(tmp_path: Path) -> None:
    """Hard links 'a' and 'A' on ext4 must not poison the case-sensitivity cache."""

    team = tmp_path / "team"
    team.mkdir()
    a = team / "a"
    a.write_text("same\n", encoding="utf-8")
    os.link(a, team / "A")
    (team / "normal.txt").write_text("ok\n", encoding="utf-8")

    counting = CountingPosixFS()
    fs = counting.as_spelling_fs()

    # First check sees st_nlink == 2: lists once for safety, leaves device uncached.
    check_exact_spelling(team, ("a",), display_prefix="team", fs=fs)
    assert counting.list_calls == 1
    assert folders._fs_case_insensitive == {}

    # Normal single-link file probes and caches False; subsequent calls do zero listings.
    check_exact_spelling(team, ("normal.txt",), display_prefix="team", fs=fs)
    lists_before = counting.list_calls
    for _ in range(10):
        check_exact_spelling(team, ("normal.txt",), display_prefix="team", fs=fs)
    assert counting.list_calls == lists_before


def test_folder_grant_file_refusal_and_check_folder_target(tmp_path: Path) -> None:
    roots, workspaces, data_dir = _make_roots(tmp_path)

    # Regular files under shared/data roots name the containing folder to grant instead.
    msg = folder_grant_file_refusal(roots, "team/finance/payroll.csv")
    assert msg is not None
    assert "team/finance/payroll.csv" in msg and "team/finance" in msg

    msg_agents = folder_grant_file_refusal(roots, "agents/run/log.txt")
    assert msg_agents is not None and "agents/run" in msg_agents

    msg_data = folder_grant_file_refusal(roots, "data/reports/q1.csv")
    assert msg_data is not None and "data/reports" in msg_data
    assert str(data_dir) not in msg_data

    # Regular file in a person's folder tells caller its owner shares it.
    msg_user = folder_grant_file_refusal(roots, "users/alice/secret.pdf")
    assert msg_user is not None and "/share" in msg_user

    # Existing directories and non-existent paths are allowed.
    assert folder_grant_file_refusal(roots, "team/finance") is None
    assert folder_grant_file_refusal(roots, "data/reports") is None
    assert folder_grant_file_refusal(roots, "users/alice") is None
    assert folder_grant_file_refusal(roots, "team/future/sub") is None
    assert folder_grant_file_refusal(roots, "data/unregistered/x.csv") is None

    # Path descending through an existing file is refused.
    msg_inside_file = folder_grant_file_refusal(roots, "team/finance/payroll.csv/child")
    assert msg_inside_file is not None and "not a path inside a file" in msg_inside_file

    # Symlink is refused.
    os.symlink(workspaces / "team" / "finance", workspaces / "team" / "link_dir")
    msg_link = folder_grant_file_refusal(roots, "team/link_dir")
    assert msg_link is not None and "symbolic link" in msg_link

    # check_folder_target: grants refuse files, blocks allow files, both refuse misspellings.
    with pytest.raises(FolderGrantOnFileError) as grant_err:
        check_folder_target(roots, "team/finance/payroll.csv", for_block=False)
    assert grant_err.value.target == "team/finance/payroll.csv"
    assert grant_err.value.suggested_folder == "team/finance"

    check_folder_target(roots, "team/finance/payroll.csv", for_block=True)

    # Clear cache before switching from real ext4 (_POSIX_FS) to simulated case-insensitive FS.
    clear_spelling_cache()
    sim = SimulatedFS(case_insensitive=True)
    with pytest.raises(PathSpellingError) as block_err:
        check_folder_target(roots, "team/Finance/payroll.csv", for_block=True, fs=sim.as_spelling_fs())
    assert block_err.value.disk == "team/finance/payroll.csv"

    # Malformed ids still raise FolderIdError.
    with pytest.raises(FolderIdError):
        check_folder_target(roots, "../outside")


def test_exact_spelling_never_follows_symlinks(tmp_path: Path) -> None:
    workspaces = tmp_path / "workspaces"
    (workspaces / "team").mkdir(parents=True)
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "target.txt").write_text("secret\n", encoding="utf-8")
    os.symlink(outside, workspaces / "team" / "escape")

    sim = SimulatedFS(case_insensitive=True)
    roots = FolderRoots(workspaces_dir=workspaces)
    # Exact symlink name stops at the link without descending into outside/.
    check_folder_id_spelling(roots, "team/escape/Target.txt", fs=sim.as_spelling_fs())
    assert "escape" not in sim.open_dir_calls

    # Misspelled symlink name itself is still caught without following it.
    with pytest.raises(PathSpellingError) as exc_info:
        check_folder_id_spelling(roots, "team/Escape/Target.txt", fs=sim.as_spelling_fs())
    assert exc_info.value.disk == "team/escape/Target.txt"
    assert "escape" not in sim.open_dir_calls

