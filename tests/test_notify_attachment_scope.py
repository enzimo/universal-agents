"""notify_user attachments come only from the recipient's folders.

The gateway checks each attachment path against the folders of the person the
message is for: their own folder plus the shared team/ and agents/ folders.
It then copies the file once, without following links, into the person's
scratch/sent/ folder and records the copy's SHA-256. Every reader
(StagedAttachmentReader) refuses a copy that became a link or changed since.
"""

from __future__ import annotations

import hashlib
import os
import stat
from pathlib import Path
from types import SimpleNamespace

import pytest

from assistant_gateway.attachments import (
    STAGED_CHANGED,
    STAGED_MISSING,
    STAGED_NO_DIGEST,
    STAGED_OUTSIDE_FOLDERS,
    STAGED_UNSAFE,
    StagedAttachmentUnavailable,
    gateway_staged_attachment_reader,
    stage_user_notification_attachments,
    validate_user_notification_attachments,
)
from runtime_settings.instance_layout import InstanceLayout


@pytest.fixture
def layout(tmp_path):
    layout = InstanceLayout.at(tmp_path / "instance")
    layout.ensure_directories()
    for person in ("alice", "bob"):
        layout.person_dir(person).mkdir(parents=True)
        (layout.person_dir(person) / "chart.png").write_bytes(b"png")
    (layout.team_dir / "team.pdf").write_bytes(b"pdf")
    (layout.agents_dir / "run.txt").write_text("log", encoding="utf-8")
    layout.soul_path.write_text("soul", encoding="utf-8")
    return layout


def _validate(layout: InstanceLayout, person: str | None, *paths: str):
    config = SimpleNamespace(workspace_path=layout.workspaces_dir)
    return validate_user_notification_attachments(list(paths), config=config, person_id=person)


def test_attachments_from_the_recipients_folders_are_accepted(layout):
    accepted, errors = _validate(
        layout,
        "alice",
        "chart.png",
        str(layout.team_dir / "team.pdf"),
        str(layout.agents_dir / "run.txt"),
    )

    assert errors == []
    assert [item.path for item in accepted] == [
        str(layout.person_dir("alice") / "chart.png"),
        str(layout.team_dir / "team.pdf"),
        str(layout.agents_dir / "run.txt"),
    ]


def test_another_persons_file_and_control_files_are_refused(layout):
    accepted, errors = _validate(
        layout,
        "alice",
        str(layout.person_dir("bob") / "chart.png"),
        str(layout.soul_path),
        "../bob/chart.png",
    )

    assert accepted == []
    assert len(errors) == 3


def test_messages_for_no_person_cannot_attach_personal_files(layout):
    accepted, errors = _validate(layout, None, str(layout.person_dir("alice") / "chart.png"))

    assert accepted == []
    assert len(errors) == 1


def _stage(layout: InstanceLayout, person: str | None, attachments):
    config = SimpleNamespace(workspace_path=layout.workspaces_dir, instance_root_path=layout.root)
    return stage_user_notification_attachments(attachments, config=config, person_id=person)


def test_a_link_to_a_control_file_is_refused(layout):
    (layout.person_dir("alice") / "report.md").symlink_to(layout.soul_path)

    accepted, errors = _validate(layout, "alice", "report.md")

    assert accepted == []
    assert len(errors) == 1


def test_channels_receive_a_copy_in_the_persons_scratch_folder(layout):
    accepted, _errors = _validate(layout, "alice", "chart.png")

    staged, errors = _stage(layout, "alice", accepted)

    assert errors == []
    copy = Path(staged[0].path)
    assert copy.parent.parent == layout.person_scratch_dir("alice") / "sent"
    assert copy.name == "chart.png"
    assert copy.read_bytes() == b"png"
    assert staged[0].sha256 == hashlib.sha256(b"png").hexdigest()
    assert staged[0].size_bytes == 3
    (layout.person_dir("alice") / "chart.png").write_bytes(b"changed later")
    assert copy.read_bytes() == b"png"


def test_copies_for_work_for_no_person_go_to_the_agents_scratch_folder(layout):
    accepted, _errors = _validate(layout, None, str(layout.agents_dir / "run.txt"))

    staged, errors = _stage(layout, None, accepted)

    assert errors == []
    assert Path(staged[0].path).parent.parent == layout.person_scratch_dir(None) / "sent"
    assert layout.person_scratch_dir(None) == layout.agents_dir / "scratch"


def test_a_link_planted_where_copies_go_is_not_followed(layout):
    accepted, _errors = _validate(layout, "alice", "chart.png")
    scratch = layout.person_scratch_dir("alice")
    scratch.mkdir(parents=True)
    (scratch / "sent").symlink_to(layout.control_dir, target_is_directory=True)

    staged, errors = _stage(layout, "alice", accepted)

    assert staged == []
    assert len(errors) == 1
    assert list(layout.control_dir.rglob("chart.png")) == []


def test_a_link_swapped_in_after_the_check_is_not_sent(layout):
    folder = layout.person_dir("alice") / "charts"
    folder.mkdir()
    (folder / "SOUL.md").write_text("mine", encoding="utf-8")
    accepted, _errors = _validate(layout, "alice", str(folder / "SOUL.md"))
    folder.rename(folder.with_name("charts.old"))
    folder.symlink_to(layout.control_dir, target_is_directory=True)

    staged, errors = _stage(layout, "alice", accepted)

    assert staged == []
    assert len(errors) == 1
    assert not any(layout.person_scratch_dir("alice").rglob("SOUL.md"))


@pytest.fixture
def staged_chart(layout):
    accepted, _errors = _validate(layout, "alice", "chart.png")
    staged, _errors = _stage(layout, "alice", accepted)
    return staged[0]


def _reader(layout: InstanceLayout):
    return gateway_staged_attachment_reader(SimpleNamespace(instance_root_path=layout.root))


def _refusal(reader, attachment) -> str:
    with pytest.raises(StagedAttachmentUnavailable) as refused:
        reader.read(attachment)
    return refused.value.reason


def test_the_reader_returns_the_checked_bytes(layout, staged_chart):
    assert _reader(layout).read(staged_chart) == b"png"


def test_the_reader_refuses_bytes_changed_after_staging(layout, staged_chart, caplog):
    Path(staged_chart.path).write_bytes(b"not the checked file")

    with caplog.at_level("WARNING"):
        reason = _refusal(_reader(layout), staged_chart)

    assert reason == STAGED_CHANGED
    assert "event=staged_attachment_refused reason=changed" in caplog.text


def test_the_reader_refuses_a_link_swapped_in_after_staging(layout, staged_chart):
    copy = Path(staged_chart.path)
    copy.unlink()
    copy.symlink_to(layout.soul_path)

    assert _refusal(_reader(layout), staged_chart) == STAGED_UNSAFE


def test_the_reader_refuses_a_folder_link_swapped_in_after_staging(layout, staged_chart):
    folder = Path(staged_chart.path).parent
    folder.rename(folder.with_name("moved"))
    folder.symlink_to(layout.control_dir, target_is_directory=True)

    assert _refusal(_reader(layout), staged_chart) == STAGED_UNSAFE


def test_the_reader_refuses_a_hard_linked_copy(layout, staged_chart):
    os.link(staged_chart.path, layout.person_dir("alice") / "second-name.png")

    assert _refusal(_reader(layout), staged_chart) == STAGED_UNSAFE


def test_the_reader_refuses_missing_copies_digests_and_other_folders(layout, staged_chart):
    reader = _reader(layout)

    assert _refusal(reader, staged_chart.model_copy(update={"sha256": None})) == STAGED_NO_DIGEST
    assert _refusal(reader, staged_chart.model_copy(update={"path": str(layout.soul_path)})) == STAGED_OUTSIDE_FOLDERS
    Path(staged_chart.path).unlink()
    assert _refusal(reader, staged_chart) == STAGED_MISSING


def test_the_private_copy_is_owner_only_outside_the_workspaces_and_removed(layout, staged_chart):
    reader = _reader(layout)

    with reader.private_copy(staged_chart) as private:
        assert private.read_bytes() == b"png"
        assert stat.S_IMODE(private.stat().st_mode) == 0o600
        assert layout.state_dir in private.parents
        seen = private

    assert not seen.exists()
    Path(staged_chart.path).write_bytes(b"changed")
    with pytest.raises(StagedAttachmentUnavailable):
        with reader.private_copy(staged_chart):
            pass
    assert list(layout.attachment_copies_dir.iterdir()) == []
