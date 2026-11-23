from __future__ import annotations

import os
import threading
from pathlib import Path

import pytest

from assistant_gateway.application.workspace_browser import (
    WORKSPACE_PREVIEW_MAX_BYTES,
    GatewayWorkspaceAccess,
    WebsiteWorkspaceBrowser,
    WorkspacePathNotFoundError,
    WorkspacePathRejectedError,
    WorkspacePathTypeError,
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

    # Members may open the top level, which lists only users/ and team/ (both absent here).
    assert browser.list_directory(access=member, path="").entries == ()
    with pytest.raises(PermissionError, match="own folder and team/"):
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


def test_workspace_browser_shows_each_person_their_own_folder_and_team(
    tmp_path: Path,
) -> None:
    """Members browse users/<me>/ and team/; administrators also see agents/."""

    from runtime_settings.instance_layout import InstanceLayout

    layout = InstanceLayout.at(tmp_path / "instance")
    layout.ensure_directories()
    for person in ("usr_admin", "usr_other"):
        layout.person_dir(person).mkdir(parents=True)
        (layout.person_dir(person) / "notes.md").write_text(person, encoding="utf-8")
    (layout.team_dir / "shared.md").write_text("shared", encoding="utf-8")
    (layout.team_dir / "page.html").write_text("<h1>Team page</h1>", encoding="utf-8")
    (layout.agents_dir / "maintenance.log").write_text("agent log", encoding="utf-8")
    browser = _browser(layout.workspaces_dir)

    top = browser.list_directory(access=ADMIN_ACCESS, path="")
    users = browser.list_directory(access=ADMIN_ACCESS, path="users")

    assert {entry.name for entry in top.entries} == {"users", "team", "agents"}
    assert [entry.name for entry in users.entries] == ["usr_admin"]
    assert browser.inspect_file(access=ADMIN_ACCESS, path="users/usr_admin/notes.md")
    assert browser.inspect_file(access=ADMIN_ACCESS, path="team/shared.md")
    assert browser.inspect_file(access=ADMIN_ACCESS, path="agents/maintenance.log")
    with pytest.raises(WorkspacePathRejectedError, match="private"):
        browser.list_directory(access=ADMIN_ACCESS, path="users/usr_other")
    with pytest.raises(WorkspacePathRejectedError, match="private"):
        browser.read_file_content(access=ADMIN_ACCESS, path="users/usr_other/notes.md")
    with pytest.raises(WorkspacePathRejectedError, match="private"):
        browser.open_file_download(access=ADMIN_ACCESS, path="users/usr_other/notes.md")

    member = GatewayWorkspaceAccess("usr_other", ("member",))
    member_top = browser.list_directory(access=member, path="")
    member_users = browser.list_directory(access=member, path="users")
    member_team = browser.list_directory(access=member, path="team")

    assert {entry.name for entry in member_top.entries} == {"users", "team"}
    assert [entry.name for entry in member_users.entries] == ["usr_other"]
    assert "shared.md" in {entry.name for entry in member_team.entries}
    own_notes = browser.inspect_file(access=member, path="users/usr_other/notes.md")
    assert own_notes.content == "usr_other"
    team_notes = browser.inspect_file(access=member, path="team/shared.md")
    assert team_notes.content == "shared"
    team_html_preview = browser.read_file_content(access=member, path="team/page.html")
    assert team_html_preview.preview_kind == "html"
    assert team_html_preview.content == b"<h1>Team page</h1>"
    with browser.open_file_download(access=member, path="team/shared.md") as download:
        assert download.read_at(0, 64) == b"shared"
        assert download.content_type == "text/markdown"
    with pytest.raises(PermissionError, match="agents/ is for administrators"):
        browser.list_directory(access=member, path="agents")
    with pytest.raises(PermissionError, match="agents/ is for administrators"):
        browser.inspect_file(access=member, path="agents/maintenance.log")
    with pytest.raises(PermissionError, match="agents/ is for administrators"):
        browser.read_file_content(access=member, path="agents/maintenance.log")
    with pytest.raises(PermissionError, match="agents/ is for administrators"):
        browser.open_file_download(access=member, path="agents/maintenance.log")
    with pytest.raises(WorkspacePathRejectedError, match="private"):
        browser.inspect_file(access=member, path="users/usr_admin/notes.md")
    with pytest.raises(WorkspacePathRejectedError, match="private"):
        browser.open_file_download(access=member, path="users/usr_admin/notes.md")


def test_workspace_browser_open_file_download_streams_large_binary_and_closes_safely(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    large_size = WORKSPACE_PREVIEW_MAX_BYTES + 2048
    large_path = workspace / "recording.mp3"
    with open(large_path, "wb") as handle:
        handle.write(b"ID3" + b"\x00" * (large_size - 6) + b"END")
    unknown_bin = workspace / "payload.unknownext"
    unknown_bin.write_bytes(b"\x01\x02\x03\x04")
    browser = _browser(workspace)

    with browser.open_file_download(access=ADMIN_ACCESS, path="recording.mp3") as download:
        assert download.path == "recording.mp3"
        assert download.name == "recording.mp3"
        assert download.size_bytes == large_size
        assert download.content_type == "audio/mpeg"
        assert download.read_at(0, 3) == b"ID3"
        assert download.read_at(large_size - 3, 10) == b"END"
        assert download.read_at(large_size, 10) == b""
        assert download.read_at(0, 0) == b""
    assert download.closed is True
    download.close()  # idempotent
    with pytest.raises(ValueError, match="already closed"):
        download.read_at(0, 1)

    with browser.open_file_download(access=ADMIN_ACCESS, path="payload.unknownext") as binary:
        assert binary.content_type == "application/octet-stream"
        assert binary.read_at(1, 2) == b"\x02\x03"

    compressed_expectations = (
        ("bundle.tar.gz", "application/gzip"),
        ("notes.txt.gz", "application/gzip"),
        ("archive.tar.bz2", "application/x-bzip2"),
        ("snapshot.tar.xz", "application/x-xz"),
    )
    for filename, expected_type in compressed_expectations:
        (workspace / filename).write_bytes(b"\x1f\x8b\x08\x00")
        with browser.open_file_download(access=ADMIN_ACCESS, path=filename) as archive:
            assert archive.content_type == expected_type


def test_workspace_browser_open_file_download_serializes_concurrent_read_and_close(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "data.bin").write_bytes(b"abcdefgh" * 1024)
    browser = _browser(workspace)

    download = browser.open_file_download(access=ADMIN_ACCESS, path="data.bin")
    errors: list[BaseException] = []
    barrier = threading.Barrier(2)

    def reader() -> None:
        barrier.wait()
        for _ in range(50):
            try:
                chunk = download.read_at(0, 16)
                assert chunk == b"abcdefghabcdefgh"
            except ValueError:
                return
            except BaseException as exc:  # pragma: no cover
                errors.append(exc)
                return

    def closer() -> None:
        barrier.wait()
        download.close()
        download.close()

    t1 = threading.Thread(target=reader)
    t2 = threading.Thread(target=closer)
    t1.start()
    t2.start()
    t1.join(timeout=5)
    t2.join(timeout=5)

    assert not errors
    assert download.closed is True


def test_workspace_browser_open_file_download_refuses_unsafe_targets(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "folder").mkdir()
    (workspace / ".git").mkdir()
    (workspace / ".git" / "config").write_text("git", encoding="utf-8")
    (workspace / ".env").write_text("SECRET=1", encoding="utf-8")
    outside = tmp_path / "outside.txt"
    outside.write_text("outside", encoding="utf-8")
    os.symlink(outside, workspace / "link.txt")
    os.link(outside, workspace / "hardlink.txt")
    browser = _browser(workspace)

    with pytest.raises(WorkspacePathTypeError, match="regular file"):
        browser.open_file_download(access=ADMIN_ACCESS, path="folder")
    with pytest.raises(WorkspacePathNotFoundError):
        browser.open_file_download(access=ADMIN_ACCESS, path="missing.bin")
    with pytest.raises(WorkspacePathRejectedError, match="traversal"):
        browser.open_file_download(access=ADMIN_ACCESS, path="../outside.txt")
    with pytest.raises(WorkspacePathRejectedError, match="not available"):
        browser.open_file_download(access=ADMIN_ACCESS, path=".env")
    with pytest.raises(WorkspacePathRejectedError, match="not available"):
        browser.open_file_download(access=ADMIN_ACCESS, path=".git/config")
    with pytest.raises(WorkspacePathRejectedError, match="opened safely"):
        browser.open_file_download(access=ADMIN_ACCESS, path="link.txt")
    with pytest.raises(WorkspacePathRejectedError, match="Multi-linked"):
        browser.open_file_download(access=ADMIN_ACCESS, path="hardlink.txt")
