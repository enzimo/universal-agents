"""Opening files below a trusted folder without following links (access_control.safe_open)."""

from __future__ import annotations

import errno
import os
import stat
import subprocess
import sys
from pathlib import Path

import pytest

from access_control import safe_open
from access_control.safe_open import UnsafePath


@pytest.fixture
def root(tmp_path: Path) -> Path:
    root = tmp_path / "root"
    (root / "docs").mkdir(parents=True)
    (root / "docs" / "note.txt").write_text("hello", encoding="utf-8")
    return root


@pytest.fixture
def secret(tmp_path: Path) -> Path:
    secret = tmp_path / "control" / "secret.txt"
    secret.parent.mkdir()
    secret.write_text("secret", encoding="utf-8")
    return secret


def _reason(call) -> str:
    with pytest.raises(UnsafePath) as caught:
        call()
    return caught.value.reason


def test_a_file_below_the_root_is_read(root: Path) -> None:
    assert safe_open.read_bytes(root, ("docs", "note.txt")) == b"hello"


def test_a_pipe_is_refused_without_waiting_for_a_writer(root: Path) -> None:
    os.mkfifo(root / "docs" / "pipe")
    probe = """
import sys
from pathlib import Path
from access_control import safe_open
try:
    safe_open.read_bytes(Path(sys.argv[1]), ("docs", "pipe"))
except safe_open.UnsafePath as exc:
    assert exc.reason == safe_open.REASON_NOT_REGULAR_FILE
else:
    raise AssertionError("A pipe was read as a regular file")
"""
    result = subprocess.run(
        [sys.executable, "-c", probe, str(root)], capture_output=True, text=True, timeout=5,
    )
    assert result.returncode == 0, result.stderr


def test_a_link_as_the_file_or_on_the_way_is_refused(root: Path, secret: Path) -> None:
    (root / "docs" / "linked.txt").symlink_to(secret)
    (root / "away").symlink_to(secret.parent, target_is_directory=True)

    assert _reason(lambda: safe_open.read_bytes(root, ("docs", "linked.txt"))) == safe_open.REASON_LINK
    assert _reason(lambda: safe_open.read_bytes(root, ("away", "secret.txt"))) == safe_open.REASON_LINK


def test_parent_references_and_empty_components_are_refused(root: Path) -> None:
    for parts in (("..", "control", "secret.txt"), ("docs", "", "note.txt"), ("docs/note.txt",)):
        assert _reason(lambda: safe_open.read_bytes(root, parts)) == safe_open.REASON_UNSAFE_NAME


def test_a_hard_linked_file_is_neither_read_nor_replaced(root: Path, secret: Path) -> None:
    os.link(secret, root / "docs" / "hard.txt")

    assert _reason(lambda: safe_open.read_bytes(root, ("docs", "hard.txt"))) == safe_open.REASON_SEVERAL_LINKS
    assert _reason(lambda: safe_open.write_bytes(root, ("docs", "hard.txt"), b"x")) == safe_open.REASON_SEVERAL_LINKS
    assert secret.read_text(encoding="utf-8") == "secret"


def test_writing_never_follows_a_link_and_leaves_its_target_alone(root: Path, secret: Path) -> None:
    (root / "docs" / "out.txt").symlink_to(secret)
    (root / "exports").symlink_to(secret.parent, target_is_directory=True)

    assert _reason(lambda: safe_open.write_bytes(root, ("docs", "out.txt"), b"x")) == safe_open.REASON_LINK
    assert _reason(
        lambda: safe_open.write_bytes(root, ("exports", "new.txt"), b"x", create_directories=True)
    ) == safe_open.REASON_LINK
    assert secret.read_text(encoding="utf-8") == "secret"
    assert sorted(path.name for path in secret.parent.iterdir()) == ["secret.txt"]


def test_an_existing_file_is_replaced_keeping_its_mode(root: Path) -> None:
    script = root / "docs" / "run.sh"
    script.write_text("old", encoding="utf-8")
    script.chmod(0o755)

    safe_open.write_bytes(root, ("docs", "run.sh"), b"new")

    assert script.read_bytes() == b"new"
    assert stat.S_IMODE(script.stat().st_mode) == 0o755


def test_a_reader_sees_the_old_content_until_the_new_content_is_complete(root: Path) -> None:
    with safe_open.writable_file(root, ("docs", "note.txt")) as writer:
        writer.write(b"new content, first half")
        writer.flush()
        # Mid-write, a reader still gets the whole old file, never an empty or partial one.
        assert safe_open.read_bytes(root, ("docs", "note.txt")) == b"hello"
        writer.write(b" and second half")

    assert safe_open.read_bytes(root, ("docs", "note.txt")) == b"new content, first half and second half"


def test_two_writers_of_one_file_never_mix_their_content(root: Path) -> None:
    long_text, short_text = b"A" * 10, b"BB"
    with safe_open.writable_file(root, ("docs", "note.txt")) as first:
        with safe_open.writable_file(root, ("docs", "note.txt")) as second:
            first.write(long_text)
            first.flush()
            second.write(short_text)
            second.flush()
        assert safe_open.read_bytes(root, ("docs", "note.txt")) == short_text

    # The last writer to finish wins with its whole content.
    assert safe_open.read_bytes(root, ("docs", "note.txt")) == long_text


def test_a_write_that_fails_part_way_leaves_the_file_and_folder_as_they_were(root: Path) -> None:
    with pytest.raises(RuntimeError):
        with safe_open.writable_file(root, ("docs", "note.txt")) as writer:
            writer.write(b"partial")
            writer.flush()
            raise RuntimeError("the source could not be read")

    assert safe_open.read_bytes(root, ("docs", "note.txt")) == b"hello"
    assert [name for name, _entry in safe_open.list_directory(root, ("docs",))] == ["note.txt"]


def test_a_new_file_gets_the_usual_mode_and_no_temporary_file_stays(root: Path) -> None:
    mask = os.umask(0o022)
    try:
        safe_open.write_bytes(root, ("docs", "new.txt"), b"made")
    finally:
        os.umask(mask)

    assert (root / "docs" / "new.txt").read_bytes() == b"made"
    assert stat.S_IMODE((root / "docs" / "new.txt").stat().st_mode) == 0o644
    assert sorted(path.name for path in (root / "docs").iterdir()) == ["new.txt", "note.txt"]


def test_a_folder_in_the_files_place_is_not_replaced(root: Path) -> None:
    (root / "docs" / "folder").mkdir()

    assert _reason(lambda: safe_open.write_bytes(root, ("docs", "folder"), b"x")) == safe_open.REASON_NOT_REGULAR_FILE
    assert (root / "docs" / "folder").is_dir()


def test_missing_folders_are_created_but_never_through_a_link(root: Path, secret: Path) -> None:
    safe_open.write_bytes(root, ("a", "b", "c.txt"), b"made", create_directories=True)
    (root / "a" / "planted").symlink_to(secret.parent, target_is_directory=True)

    assert (root / "a" / "b" / "c.txt").read_bytes() == b"made"
    assert _reason(
        lambda: safe_open.open_directory(root, ("a", "planted", "deeper"), create=True)
    ) == safe_open.REASON_LINK
    assert not (secret.parent / "deeper").exists()


def test_a_folder_swapped_for_a_link_after_a_check_fails_the_open(root: Path, secret: Path) -> None:
    """The check-then-open race: whatever was checked, the open itself refuses a link."""

    checked = safe_open.entry_status(root, ("docs",))
    assert checked is not None and stat.S_ISDIR(checked.st_mode)
    (root / "docs").rename(root / "docs.old")
    (root / "docs").symlink_to(secret.parent, target_is_directory=True)

    assert _reason(lambda: safe_open.read_bytes(root, ("docs", "secret.txt"))) == safe_open.REASON_LINK


def test_size_limits_are_enforced(root: Path, tmp_path: Path) -> None:
    assert _reason(lambda: safe_open.read_bytes(root, ("docs", "note.txt"), max_bytes=3)) == safe_open.REASON_TOO_LARGE
    destination = tmp_path / "copy.txt"
    assert safe_open.copy_file(root, ("docs", "note.txt"), destination, max_bytes=5) == 5
    assert destination.read_bytes() == b"hello"


def test_status_and_listing_report_links_without_following_them(root: Path, secret: Path) -> None:
    (root / "docs" / "linked.txt").symlink_to(secret)

    linked = safe_open.entry_status(root, ("docs", "linked.txt"))
    listed = dict(safe_open.list_directory(root, ("docs",)))

    assert linked is not None and stat.S_ISLNK(linked.st_mode)
    assert stat.S_ISLNK(listed["linked.txt"].st_mode)
    assert safe_open.entry_status(root, ("docs", "missing.txt")) is None


def test_folder_sizes_count_regular_files_below_and_never_follow_a_link(root: Path, secret: Path, tmp_path: Path) -> None:
    (root / "docs" / "deep").mkdir()
    (root / "docs" / "deep" / "data.bin").write_bytes(b"x" * 20)
    os.symlink(secret.parent, root / "docs" / "deep" / "out")
    os.symlink(secret, root / "docs" / "secret-link.txt")
    os.symlink(secret.parent, root / "linked")

    assert safe_open.folder_bytes(root, ("docs",)) == len("hello") + 20
    assert safe_open.folder_bytes(root, ("missing",)) == 0
    assert safe_open.folder_bytes(tmp_path / "no-root", ()) == 0
    assert _reason(lambda: safe_open.folder_bytes(root, ("linked",))) == safe_open.REASON_LINK


@pytest.mark.parametrize("failure", [errno.EACCES, errno.EIO])
@pytest.mark.parametrize("operation", ["root", "stat", "child"])
def test_folder_sizes_do_not_hide_permission_or_io_failures(root: Path, monkeypatch, failure: int, operation: str) -> None:
    (root / "docs" / "deep").mkdir()
    opened, stated = os.open, os.stat

    def open_path(path, flags, *args, **kwargs):
        if (operation == "root" and path == root) or (operation == "child" and path == "deep"):
            raise OSError(failure, "measurement failed")
        return opened(path, flags, *args, **kwargs)

    def stat_path(path, *args, **kwargs):
        if operation == "stat" and path == "note.txt" and "dir_fd" in kwargs:
            raise OSError(failure, "measurement failed")
        return stated(path, *args, **kwargs)

    monkeypatch.setattr(os, "open", open_path)
    monkeypatch.setattr(os, "stat", stat_path)
    with pytest.raises(OSError):
        safe_open.folder_bytes(root, ("docs",))


def test_folder_sizes_refuse_a_partial_total_at_the_depth_limit(root: Path, monkeypatch) -> None:
    (root / "docs" / "deep").mkdir()
    monkeypatch.setattr(safe_open, "FOLDER_BYTES_MAX_DEPTH", 0)
    with pytest.raises(OSError, match="deep"):
        safe_open.folder_bytes(root, ("docs",))


@pytest.mark.parametrize("failure", [errno.ENOENT, errno.ENOTDIR, errno.ELOOP])
def test_folder_sizes_still_skip_entries_removed_or_replaced_during_the_walk(root: Path, monkeypatch, failure: int) -> None:
    (root / "docs" / "deep").mkdir()
    opened = os.open

    def open_path(path, flags, *args, **kwargs):
        if path == "deep":
            raise OSError(failure, "entry changed during measurement")
        return opened(path, flags, *args, **kwargs)

    monkeypatch.setattr(os, "open", open_path)
    assert safe_open.folder_bytes(root, ("docs",)) == len("hello")
