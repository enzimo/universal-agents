"""Where notify_user attachments may come from (decisions D15 and D16).

Besides the person's folder and team/ and agents/, a file may be attached
from a registered data folder or from what another person shared with the
person the message is for. The gateway checks the location again from the
access store; anything else is refused.
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

from assistant_gateway.attachments import (
    OUTSIDE_SOURCES,
    stage_user_notification_attachments,
    validate_user_notification_attachments,
)
from custom_messages.contracts.access_v2 import GrantV1, SubjectKind, SubjectRefV1, TargetKind, TargetRefV1
from runtime_settings.instance_layout import InstanceLayout, person_folder_name
from storage.access_repository import AccessRepository
from tests.data_folder_support import store_data_folder, sqlite_storage

TEAM = "team-data"


def config(tmp_path: Path) -> SimpleNamespace:
    layout = InstanceLayout.at(tmp_path / "instance")
    layout.ensure_directories()
    return SimpleNamespace(
        workspace_path=layout.workspaces_dir,
        instance_root_path=layout.root,
        manager_storage_settings=sqlite_storage(tmp_path / "manager.db"),
        manager=SimpleNamespace(team_id=TEAM),
        layout=layout,
    )


def share(cfg, owner: str, recipient: str, item: str, kind: TargetKind) -> None:
    AccessRepository(cfg.manager_storage_settings).create(
        GrantV1(
            grant_id=f"grant-{recipient}",
            team_id=TEAM,
            subject=SubjectRefV1(kind=SubjectKind.USER, id=recipient),
            target=TargetRefV1(kind=kind, id=f"users/{person_folder_name(owner)}/{item}"),
            level="read",
            granted_by=SubjectRefV1(kind=SubjectKind.USER, id=owner),
            created_at_utc=datetime.now(timezone.utc),
        )
    )


def test_a_data_folder_file_can_be_attached_and_is_copied_to_the_persons_scratch(tmp_path: Path) -> None:
    cfg = config(tmp_path)
    reports = tmp_path / "reports"
    reports.mkdir()
    (reports / "q1.md").write_text("quarter", encoding="utf-8")
    elsewhere = tmp_path / "elsewhere.md"
    elsewhere.write_text("x", encoding="utf-8")
    store_data_folder(cfg.manager_storage_settings, "reports", reports, writable=False)

    valid, errors = validate_user_notification_attachments(
        [str(reports / "q1.md"), str(elsewhere)], config=cfg, person_id="bob"
    )
    assert [Path(item.path).name for item in valid] == ["q1.md"]
    assert len(errors) == 1 and OUTSIDE_SOURCES in errors[0]
    [staged], copy_errors = stage_user_notification_attachments(valid, config=cfg, person_id="bob")
    assert copy_errors == [] and "/scratch/sent/" in staged.path and staged.sha256


def test_what_was_shared_with_the_person_can_be_attached_and_nothing_else_of_the_owners(tmp_path: Path) -> None:
    cfg = config(tmp_path)
    alice = cfg.layout.person_dir("alice")
    (alice / "reports").mkdir(parents=True)
    (alice / "reports" / "q1.md").write_text("quarter", encoding="utf-8")
    (alice / "plan.md").write_text("plan", encoding="utf-8")
    (alice / "diary.md").write_text("private", encoding="utf-8")
    share(cfg, "alice", "bob", "reports", TargetKind.FOLDER)
    share(cfg, "alice", "carol", "plan.md", TargetKind.FILE)

    bob_ok, bob_errors = validate_user_notification_attachments(
        [str(alice / "reports" / "q1.md"), str(alice / "diary.md"), str(alice / "plan.md")], config=cfg, person_id="bob"
    )
    carol_ok, _ = validate_user_notification_attachments(
        [str(alice / "plan.md"), str(alice / "reports" / "q1.md")], config=cfg, person_id="carol"
    )
    assert [Path(item.path).name for item in bob_ok] == ["q1.md"] and len(bob_errors) == 2
    assert [Path(item.path).name for item in carol_ok] == ["plan.md"]
