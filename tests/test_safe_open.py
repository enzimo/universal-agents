"""Opening files below a trusted folder without following links (access_control.safe_open)."""

from __future__ import annotations

import os
import stat
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


def test_an_existing_file_is_replaced_in_place_keeping_its_mode(root: Path) -> None:
    script = root / "docs" / "run.sh"
    script.write_text("old", encoding="utf-8")
    script.chmod(0o755)

    safe_open.write_bytes(root, ("docs", "run.sh"), b"new")

    assert script.read_bytes() == b"new"
    assert stat.S_IMODE(script.stat().st_mode) == 0o755


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
