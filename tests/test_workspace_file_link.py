"""Unit tests for the read-only ``workspace_file_link`` Strands tool in ``fn:files``."""

from __future__ import annotations

from datetime import datetime, timezone
import json
import os
from pathlib import Path
from types import SimpleNamespace

import pytest

from access_catalog.catalog import load_catalog
from access_control.data_folders import DataFolderRegistry
from access_control.entities import PersonPrincipal
from custom_agents import access_runtime
from custom_messages.contracts.access_v2 import AccessLevel
from custom_tools import file_access
from custom_tools.non_interactive_file_tools import workspace_file_link
from custom_tools.path_policy import (
    PERSON_WORKSPACE_DENIED,
    PROTECTED_RUNTIME_CONTROL_DENIED,
    SENSITIVE_PATH_DENIED,
)
from custom_tools.registry import FILES_PROVIDER
from runtime_settings.instance_layout import InstanceLayout, person_folder_name
from storage.access_repository import AccessRepository
from storage.config import resolve_storage_settings
from tests.broker_access_support import context_signer, context_verify_keys
from tests.task_work_support import set_agent_ref

TEAM = "team-download-links"


def _make_agent(
    layout: InstanceLayout,
    person: str | None,
    *,
    roles: tuple[str, ...] = ("member",),
    signed: bool = True,
) -> SimpleNamespace:
    context = (
        context_signer().issue(team_id=TEAM, person_id=person, person_roles=roles)
        if person
        else None
    )
    meta = SimpleNamespace(
        gateway_context=(
            SimpleNamespace(canonical_user_id=person, task_context=context if signed else None)
            if person
            else None
        )
    )
    storage = resolve_storage_settings(
        {"backend": "sqlite", "sqlite": {"path": str(layout.root / "manager.db")}}
    )
    task_manager = SimpleNamespace(
        current_task_id="task-link-1",
        get_task_info=lambda _task_id: SimpleNamespace(meta=meta),
        storage_settings=storage,
    )
    return SimpleNamespace(
        id="agent-link-1",
        team_id=TEAM,
        instance_root=str(layout.root),
        workspace_dir=str(layout.workspaces_dir),
        authorization_config={"enabled": False},
        task_manager=task_manager,
        test_task_id="task-link-1",
    )


@pytest.fixture
def layout(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> InstanceLayout:
    inst = InstanceLayout.at(tmp_path / "instance")
    inst.ensure_directories()
    for person in ("alice", "bob"):
        inst.person_dir(person).mkdir(parents=True, exist_ok=True)
        (inst.person_dir(person) / "notes.txt").write_text(f"{person} notes", encoding="utf-8")
    inst.team_dir.mkdir(parents=True, exist_ok=True)
    inst.agents_dir.mkdir(parents=True, exist_ok=True)
    inst.soul_path.write_text("soul", encoding="utf-8")
    (inst.skills_dir / "example-skill").mkdir(parents=True, exist_ok=True)
    (inst.skills_dir / "example-skill" / "SKILL.md").write_text("# skill", encoding="utf-8")
    for name in ("TEAM_INSTANCE_ROOT", "TEAM_INSTANCE_CONFIG", "TOOL_DENY_PATHS"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("INTERNAL_WEBSITE_BASE_URL", "https://team.example.test/")
    monkeypatch.setenv("ACCESS_CONTEXT_VERIFY_KEYS", context_verify_keys())
    access_runtime._runtime_access.cache_clear()
    yield inst
    set_agent_ref(None)
    access_runtime._runtime_access.cache_clear()


def _payload(result: dict) -> dict:
    assert result["status"] == "success", result
    return json.loads(result["content"][0]["text"])


def test_workspace_file_link_is_registered_in_files_provider_and_catalog_at_read() -> None:
    assert workspace_file_link in FILES_PROVIDER.tools
    catalog = load_catalog()
    resolution = catalog.resolve_function("workspace_file_link", {"path": "notes.txt"})
    assert resolution.tool_id == "fn:files"
    assert resolution.level is AccessLevel.READ


def test_member_gets_encoded_download_and_view_urls_for_own_large_and_utf8_files(
    layout: InstanceLayout,
) -> None:
    set_agent_ref(_make_agent(layout, "alice", roles=("member",)))
    alice = layout.person_dir("alice")
    target = alice / "audio" / "episode (1) café.mp3"
    target.parent.mkdir(parents=True)
    with target.open("wb") as handle:
        handle.truncate(12 * 1024 * 1024)

    data = _payload(workspace_file_link(path="audio/episode (1) café.mp3"))

    own_folder = person_folder_name("alice")
    expected_path = f"users/{own_folder}/audio/episode (1) café.mp3"
    expected_encoded = (
        f"users%2F{own_folder}%2Faudio%2Fepisode%20%281%29%20caf%C3%A9.mp3"
    )
    assert data == {
        "content_type": "audio/mpeg",
        "download_url": f"https://team.example.test/api/web-chat/workspace/download?path={expected_encoded}",
        "filename": "episode (1) café.mp3",
        "size_bytes": 12 * 1024 * 1024,
        "view_url": f"https://team.example.test/webchat?view=files&path={expected_encoded}",
        "workspace_path": expected_path,
    }


def test_member_gets_link_for_team_folder_file_including_archive_mime(
    layout: InstanceLayout,
) -> None:
    set_agent_ref(_make_agent(layout, "alice", roles=("member",)))
    archive = layout.team_dir / "releases" / "bundle.tar.gz"
    archive.parent.mkdir(parents=True)
    archive.write_bytes(b"\x1f\x8b\x08\x00test")

    data = _payload(workspace_file_link(path=str(archive)))

    assert data["workspace_path"] == "team/releases/bundle.tar.gz"
    assert data["filename"] == "bundle.tar.gz"
    assert data["content_type"] == "application/gzip"
    assert data["size_bytes"] == 8
    assert data["download_url"] == (
        "https://team.example.test/api/web-chat/workspace/download"
        "?path=team%2Freleases%2Fbundle.tar.gz"
    )
    assert data["view_url"] == (
        "https://team.example.test/webchat?view=files&path=team%2Freleases%2Fbundle.tar.gz"
    )


def test_agents_folder_requires_admin_for_person_tasks_and_allows_admins_and_system(
    layout: InstanceLayout,
) -> None:
    report = layout.agents_dir / "nightly.json"
    report.write_text('{"ok": true}', encoding="utf-8")

    set_agent_ref(_make_agent(layout, "alice", roles=("member",)))
    member_result = workspace_file_link(path=str(report))
    assert member_result["status"] == "error"
    assert member_result["reason_code"] == "not_downloadable_location"
    assert "administrators" in member_result["content"][0]["text"]

    for role in ("admin", "owner"):
        set_agent_ref(_make_agent(layout, "alice", roles=(role,)))
        data = _payload(workspace_file_link(path=str(report)))
        assert data["workspace_path"] == "agents/nightly.json"
        assert data["content_type"] == "application/json"

    set_agent_ref(_make_agent(layout, None))
    system_data = _payload(workspace_file_link(path="nightly.json"))
    assert system_data["workspace_path"] == "agents/nightly.json"


def test_refuses_missing_empty_directory_and_hard_linked_files(
    layout: InstanceLayout,
) -> None:
    set_agent_ref(_make_agent(layout, "alice", roles=("member",)))
    alice = layout.person_dir("alice")
    (alice / "subdir").mkdir()
    os.link(alice / "notes.txt", alice / "hardlink.txt")

    empty = workspace_file_link(path="   ")
    assert empty["status"] == "error"
    assert empty["reason_code"] == "invalid_path"

    missing = workspace_file_link(path="does-not-exist.mp3")
    assert missing["status"] == "error"
    assert missing["reason_code"] == "not_found"

    directory = workspace_file_link(path="subdir")
    assert directory["status"] == "error"
    assert directory["reason_code"] == file_access.NOT_A_REGULAR_FILE

    hardlink = workspace_file_link(path="hardlink.txt")
    assert hardlink["status"] == "error"
    assert hardlink["reason_code"] == file_access.SEVERAL_HARD_LINKS


def test_refuses_symlinks_even_within_persons_own_folder_or_parent_dirs(
    layout: InstanceLayout,
) -> None:
    set_agent_ref(_make_agent(layout, "alice", roles=("member",)))
    alice = layout.person_dir("alice")
    real_dir = alice / "real_dir"
    real_dir.mkdir()
    (real_dir / "item.txt").write_text("ok", encoding="utf-8")

    (alice / "shortcut.txt").symlink_to(alice / "notes.txt")
    (alice / "dir_link").symlink_to(real_dir, target_is_directory=True)
    (alice / "to_soul.txt").symlink_to(layout.soul_path)
    (alice / "to_bob.txt").symlink_to(layout.person_dir("bob") / "notes.txt")

    own_shortcut = workspace_file_link(path="shortcut.txt")
    assert own_shortcut["status"] == "error"
    assert own_shortcut["reason_code"] == file_access.LINK_NOT_FOLLOWED

    via_dir_link = workspace_file_link(path="dir_link/item.txt")
    assert via_dir_link["status"] == "error"
    assert via_dir_link["reason_code"] == file_access.LINK_NOT_FOLLOWED

    via_parent_traversal = workspace_file_link(path="dir_link/../notes.txt")
    assert via_parent_traversal["status"] == "error"
    assert via_parent_traversal["reason_code"] == file_access.LINK_NOT_FOLLOWED

    abs_shortcut = workspace_file_link(path=str(alice / "shortcut.txt"))
    assert abs_shortcut["status"] == "error"
    assert abs_shortcut["reason_code"] == file_access.LINK_NOT_FOLLOWED

    soul_link = workspace_file_link(path="to_soul.txt")
    assert soul_link["status"] == "error"
    assert soul_link["reason_code"] == PROTECTED_RUNTIME_CONTROL_DENIED

    bob_link = workspace_file_link(path="to_bob.txt")
    assert bob_link["status"] == "error"
    assert bob_link["reason_code"] == PERSON_WORKSPACE_DENIED


@pytest.mark.parametrize(
    "relative_path",
    [
        ".git/config",
        ".hg/dirstate",
        ".svn/entries",
        ".venv/bin/python",
        "__pycache__/mod.cpython-314.pyc",
        "node_modules/pkg/index.js",
    ],
)
def test_refuses_hidden_tooling_paths(
    layout: InstanceLayout, relative_path: str
) -> None:
    set_agent_ref(_make_agent(layout, "alice", roles=("admin",)))
    target = layout.team_dir / relative_path
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text("hidden", encoding="utf-8")

    result = workspace_file_link(path=str(target))
    assert result["status"] == "error"
    assert result["reason_code"] == "hidden_workspace_path"


@pytest.mark.parametrize("filename", [".env", "id_rsa", "service.pem", ".ssh/config"])
def test_refuses_sensitive_files(layout: InstanceLayout, filename: str) -> None:
    set_agent_ref(_make_agent(layout, "alice", roles=("admin",)))
    secret = layout.person_dir("alice") / filename
    secret.parent.mkdir(parents=True, exist_ok=True)
    secret.write_text("TOP_SECRET=1", encoding="utf-8")

    result = workspace_file_link(path=filename)
    assert result["status"] == "error"
    assert result["reason_code"] == SENSITIVE_PATH_DENIED


def test_refuses_host_data_folders_skill_folders_and_other_persons_folder(
    layout: InstanceLayout, tmp_path: Path
) -> None:
    agent = _make_agent(layout, "alice", roles=("admin",))
    set_agent_ref(agent)

    # Register a real host data/ folder so file_access allows reading it,
    # and prove workspace_file_link still refuses it as non-downloadable.
    host_data = tmp_path / "external_datasets"
    host_data.mkdir()
    dataset = host_data / "metrics.csv"
    dataset.write_text("a,b\n1,2\n", encoding="utf-8")
    store = AccessRepository(agent.task_manager.storage_settings)
    DataFolderRegistry(store, TEAM).register(
        PersonPrincipal("admin-1", TEAM, ("admin",)),
        name="datasets",
        path=str(host_data),
        writable=False,
        reason="test dataset",
        protected=(layout.root,),
        now=datetime.now(timezone.utc),
    )
    _, readable_text = file_access.read_text(str(dataset))
    assert readable_text == "a,b\n1,2\n"

    data_result = workspace_file_link(path=str(dataset))
    assert data_result["status"] == "error"
    assert data_result["reason_code"] == "not_downloadable_location"

    # Read-only team skill file is readable via file_access, but not downloadable.
    skill_result = workspace_file_link(
        path=str(layout.skills_dir / "example-skill" / "SKILL.md")
    )
    assert skill_result["status"] == "error"
    assert skill_result["reason_code"] == "not_downloadable_location"

    # Another person's folder is refused even for administrators.
    other_result = workspace_file_link(path=str(layout.person_dir("bob") / "notes.txt"))
    assert other_result["status"] == "error"
    assert other_result["reason_code"] == PERSON_WORKSPACE_DENIED


def test_refuses_other_persons_folder_even_when_file_access_allows_a_share(
    layout: InstanceLayout, monkeypatch: pytest.MonkeyPatch
) -> None:
    set_agent_ref(_make_agent(layout, "alice", roles=("admin",)))
    bob_file = layout.person_dir("bob") / "shared.pdf"
    bob_file.write_bytes(b"%PDF-1.7\n")
    bob_stat = bob_file.stat()
    bob_location = file_access.FileLocation(
        root=layout.person_dir("bob"),
        parts=("shared.pdf",),
        task_folder=False,
    )
    monkeypatch.setattr(
        file_access,
        "inspect_regular_file",
        lambda _raw, agent=None: (bob_location, bob_stat),
    )

    result = workspace_file_link(path=str(bob_file))
    assert result["status"] == "error"
    assert result["reason_code"] == "not_downloadable_location"


def test_refuses_backslash_in_filename_and_toctou_swap(
    layout: InstanceLayout, monkeypatch: pytest.MonkeyPatch
) -> None:
    set_agent_ref(_make_agent(layout, "alice", roles=("member",)))
    alice = layout.person_dir("alice")
    weird = alice / "bad\\name.txt"
    weird.write_text("hi", encoding="utf-8")

    backslash = workspace_file_link(path=str(weird))
    assert backslash["status"] == "error"
    assert backslash["reason_code"] == "unsafe_workspace_path"

    # Simulate a symlink swapped in between inspect_regular_file and the final workspace open.
    swap_target = alice / "swapped.txt"
    swap_target.write_text("original", encoding="utf-8")
    real_inspect = file_access.inspect_regular_file

    def _inspect_then_swap(raw: str | Path, *, agent=None):
        location, st = real_inspect(raw, agent=agent)
        swap_target.unlink()
        swap_target.symlink_to(alice / "notes.txt")
        return location, st

    monkeypatch.setattr(file_access, "inspect_regular_file", _inspect_then_swap)
    swapped = workspace_file_link(path="swapped.txt")
    assert swapped["status"] == "error"
    assert swapped["reason_code"] == file_access.LINK_NOT_FOLLOWED


def test_relative_path_to_team_works_and_access_denied_is_surfaced(
    layout: InstanceLayout, monkeypatch: pytest.MonkeyPatch
) -> None:
    set_agent_ref(_make_agent(layout, "alice", roles=("member",)))
    shared = layout.team_dir / "summary.md"
    shared.write_text("# Summary\n", encoding="utf-8")

    # Relative traversal with no symlinks from users/<alice> into team/ succeeds.
    data = _payload(workspace_file_link(path="../../team/summary.md"))
    assert data["workspace_path"] == "team/summary.md"
    assert data["content_type"] == "text/markdown"

    # Authorization denial from file_access surfaces ACCESS_DENIED cleanly.
    def _deny(_raw: str | Path, *, agent=None):
        raise file_access.PathPolicyError("Folder read denied.", reason_code=file_access.ACCESS_DENIED)

    monkeypatch.setattr(file_access, "inspect_regular_file", _deny)
    denied = workspace_file_link(path="notes.txt")
    assert denied["status"] == "error"
    assert denied["reason_code"] == file_access.ACCESS_DENIED

