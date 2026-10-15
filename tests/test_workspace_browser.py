from __future__ import annotations

import os
from pathlib import Path

import pytest

from assistant_gateway.application.workspace_browser import (
    GatewayWorkspaceAccess,
    WebsiteWorkspaceBrowser,
    WorkspacePathRejectedError,
    WorkspacePreviewUnavailableError,
)
from assistant_gateway.runtime_ports import OwnershipPrivateResourceAuthorizer


ADMIN_ACCESS = GatewayWorkspaceAccess(
    canonical_user_id="usr_admin",
    roles=("admin",),
)


def _browser(workspace_root: Path) -> WebsiteWorkspaceBrowser:
    return WebsiteWorkspaceBrowser(
        workspace_root=workspace_root,
        authorizer=OwnershipPrivateResourceAuthorizer(),
        team_id="team_1",
    )


def test_workspace_browser_lists_safe_entries_and_classifies_previews(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "docs").mkdir()
    (workspace / "README.md").write_text("# Render me\n", encoding="utf-8")
    (workspace / "page.html").write_text(
        "<!doctype html><h1>Rendered HTML</h1>",
        encoding="utf-8",
    )
    (workspace / "notes.txt").write_text("Plain text\n", encoding="utf-8")
    (workspace / "image.png").write_bytes(b"\x89PNG\r\n\x1a\n")
    (workspace / "report.pdf").write_bytes(b"%PDF-1.4\n")
    (workspace / "archive.bin").write_bytes(b"\x00\x01")
    (workspace / ".env").write_text("TOKEN=secret\n", encoding="utf-8")
    (workspace / "debug.env").write_text("TOKEN=secret\n", encoding="utf-8")
    (workspace / ".git").mkdir()
    browser = _browser(workspace)

    listing = browser.list_directory(access=ADMIN_ACCESS, path="")

    entries = {entry.name: entry for entry in listing.entries}
    assert list(entries) == [
        "docs",
        "archive.bin",
        "image.png",
        "notes.txt",
        "page.html",
        "README.md",
        "report.pdf",
    ]
    assert entries["README.md"].preview_kind == "markdown"
    assert entries["page.html"].preview_kind == "html"
    assert entries["notes.txt"].preview_kind == "text"
    assert entries["image.png"].preview_kind == "image"
    assert entries["report.pdf"].preview_kind == "pdf"
    assert entries["archive.bin"].preview_kind == "unsupported"
    assert ".env" not in entries
    assert "debug.env" not in entries
    assert ".git" not in entries


def test_workspace_browser_returns_text_and_renderable_content_separately(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "README.md").write_text("# Render me\n", encoding="utf-8")
    html_bytes = b"<!doctype html><h1>Rendered HTML</h1>"
    (workspace / "page.html").write_bytes(html_bytes)
    browser = _browser(workspace)

    markdown = browser.inspect_file(access=ADMIN_ACCESS, path="README.md")
    html = browser.inspect_file(access=ADMIN_ACCESS, path="page.html")
    html_content = browser.read_file_content(access=ADMIN_ACCESS, path="page.html")

    assert markdown.preview_kind == "markdown"
    assert markdown.content == "# Render me\n"
    assert html.preview_kind == "html"
    assert html.content is None
    assert html_content.preview_kind == "html"
    assert html_content.content_type == "text/html"
    assert html_content.content == html_bytes


def test_workspace_browser_rejects_traversal_secrets_symlinks_and_shared_files_for_members(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "notes.txt").write_text("shared notes", encoding="utf-8")
    outside = tmp_path / "outside.txt"
    outside.write_text("outside", encoding="utf-8")
    os.symlink(outside, workspace / "escape.txt")
    browser = _browser(workspace)
    member = GatewayWorkspaceAccess("usr_member", ("member",))

    # Members may open the top level, but it lists only users/ (absent here).
    assert browser.list_directory(access=member, path="").entries == ()
    with pytest.raises(PermissionError, match="only your own folder"):
        browser.inspect_file(access=member, path="notes.txt")
    with pytest.raises(WorkspacePathRejectedError, match="traversal"):
        browser.inspect_file(access=ADMIN_ACCESS, path="../outside.txt")
    with pytest.raises(WorkspacePathRejectedError, match="not available"):
        browser.inspect_file(access=ADMIN_ACCESS, path="debug.env")
    with pytest.raises(WorkspacePathRejectedError, match="opened safely"):
        browser.inspect_file(access=ADMIN_ACCESS, path="escape.txt")
    assert "escape.txt" not in {
        entry.name
        for entry in browser.list_directory(access=ADMIN_ACCESS, path="").entries
    }


def test_workspace_browser_hides_credentials_and_tooling_folders(
    tmp_path: Path,
) -> None:
    # Credential names come from the list every file check shares
    # (access_control.sensitive_paths); version-control and dependency
    # folders are the browser's own list.
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "notes.md").write_text("visible\n", encoding="utf-8")
    for name in (".envrc", "id_ed25519", "team.env", "server.pem"):
        (workspace / name).write_text("secret\n", encoding="utf-8")
    for name in (".ssh", "node_modules", "__pycache__"):
        (workspace / name).mkdir()
    (workspace / ".ssh" / "config").write_text("Host *\n", encoding="utf-8")
    (workspace / "node_modules" / "index.js").write_text("x\n", encoding="utf-8")
    browser = _browser(workspace)

    listing = browser.list_directory(access=ADMIN_ACCESS, path="")

    assert [entry.name for entry in listing.entries] == ["notes.md"]
    for path in (".envrc", "id_ed25519", "team.env", ".ssh/config", "node_modules/index.js"):
        with pytest.raises(WorkspacePathRejectedError, match="not available"):
            browser.inspect_file(access=ADMIN_ACCESS, path=path)


def test_workspace_browser_rejects_hard_links_to_files_outside_the_workspace(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    outside = tmp_path / "outside.txt"
    outside.write_text("outside", encoding="utf-8")
    os.link(outside, workspace / "hard-link.txt")
    browser = _browser(workspace)

    assert "hard-link.txt" not in {
        entry.name
        for entry in browser.list_directory(access=ADMIN_ACCESS, path="").entries
    }
    with pytest.raises(WorkspacePathRejectedError, match="Multi-linked"):
        browser.inspect_file(access=ADMIN_ACCESS, path="hard-link.txt")


def test_workspace_browser_does_not_return_unsupported_binary_content(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "archive.bin").write_bytes(b"\x00\x01\x02")
    browser = _browser(workspace)

    file_view = browser.inspect_file(access=ADMIN_ACCESS, path="archive.bin")

    assert file_view.preview_kind == "unsupported"
    assert file_view.content is None
    assert file_view.message == "Preview is not available for .bin files."
    with pytest.raises(WorkspacePreviewUnavailableError):
        browser.read_file_content(access=ADMIN_ACCESS, path="archive.bin")


def test_workspace_browser_shows_each_person_only_their_own_folder(
    tmp_path: Path,
) -> None:
    """Administrators browse team/, agents/ and their own folder; members only their own."""

    from runtime_settings.instance_layout import InstanceLayout

    layout = InstanceLayout.at(tmp_path / "instance")
    layout.ensure_directories()
    for person in ("usr_admin", "usr_other"):
        layout.person_dir(person).mkdir(parents=True)
        (layout.person_dir(person) / "notes.md").write_text(person, encoding="utf-8")
    (layout.team_dir / "shared.md").write_text("shared", encoding="utf-8")
    browser = _browser(layout.workspaces_dir)

    top = browser.list_directory(access=ADMIN_ACCESS, path="")
    users = browser.list_directory(access=ADMIN_ACCESS, path="users")

    assert {entry.name for entry in top.entries} == {"users", "team", "agents"}
    assert [entry.name for entry in users.entries] == ["usr_admin"]
    assert browser.inspect_file(access=ADMIN_ACCESS, path="users/usr_admin/notes.md")
    assert browser.inspect_file(access=ADMIN_ACCESS, path="team/shared.md")
    with pytest.raises(WorkspacePathRejectedError, match="private"):
        browser.list_directory(access=ADMIN_ACCESS, path="users/usr_other")
    with pytest.raises(WorkspacePathRejectedError, match="private"):
        browser.read_file_content(access=ADMIN_ACCESS, path="users/usr_other/notes.md")

    member = GatewayWorkspaceAccess("usr_other", ("member",))
    member_top = browser.list_directory(access=member, path="")
    member_users = browser.list_directory(access=member, path="users")

    assert [entry.name for entry in member_top.entries] == ["users"]
    assert [entry.name for entry in member_users.entries] == ["usr_other"]
    own_notes = browser.inspect_file(access=member, path="users/usr_other/notes.md")
    assert own_notes.content == "usr_other"
    with pytest.raises(PermissionError, match="only your own folder"):
        browser.list_directory(access=member, path="team")
    with pytest.raises(PermissionError, match="only your own folder"):
        browser.inspect_file(access=member, path="team/shared.md")
    with pytest.raises(WorkspacePathRejectedError, match="private"):
        browser.inspect_file(access=member, path="users/usr_admin/notes.md")
