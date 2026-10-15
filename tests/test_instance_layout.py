"""Instance layout, person folders, and old-layout detection on real folders."""

import subprocess

import pytest

from runtime_settings.instance_layout import (
    InstanceLayout,
    person_folder_name,
    workspace_scope,
)
from runtime_settings.legacy_layout import (
    legacy_layout_message,
    legacy_layout_moves,
    render_move_commands,
)


@pytest.mark.parametrize(
    "person,expected",
    [("usr_3f9a1b2c4d5e", "usr_3f9a1b2c4d5e"), ("owner", "owner")],
)
def test_generated_ids_are_folder_names(person, expected):
    assert person_folder_name(person) == expected


@pytest.mark.parametrize("person", ["User", "Ada Lovelace", "../../etc", "a/b", "é", "x" * 90])
def test_other_ids_become_slug_plus_hash(person):
    name = person_folder_name(person)
    assert "-" in name and "/" not in name and ".." not in name
    assert name == person_folder_name(person)
    assert len(name) <= 37


def test_person_folders_never_collide():
    names = {person_folder_name(person) for person in ("User", "user", "USER", "user-1")}
    assert len(names) == 4


def test_scope_for_person_and_for_no_person(tmp_path):
    layout = InstanceLayout.at(tmp_path)
    scope = layout.scope_for("usr_aaaaaaaaaaaa")
    assert scope.home == layout.users_dir / "usr_aaaaaaaaaaaa"
    assert scope.roots == (scope.home, layout.team_dir, layout.agents_dir)
    assert not scope.contains(layout.users_dir / "usr_bbbbbbbbbbbb" / "notes.md")
    assert scope.contains(layout.team_dir / "shared" / "x.md")
    system = workspace_scope(layout.workspaces_dir, None)
    assert system.home == layout.agents_dir
    assert not system.contains(layout.users_dir / "usr_aaaaaaaaaaaa")


def test_instance_private_paths(tmp_path):
    layout = InstanceLayout.at(tmp_path)
    for private in (
        layout.soul_path,
        layout.task_database_path,
        layout.tool_results_dir,
        tmp_path / "team.env",
        tmp_path / "broker_credentials" / "x",
        tmp_path,
    ):
        assert layout.is_instance_private(private), private
    for reachable in (layout.workspaces_dir, layout.team_dir / "a", tmp_path.parent / "elsewhere"):
        assert not layout.is_instance_private(reachable), reachable


def test_fixed_locations_outside_the_agent_areas(tmp_path):
    """MCP, installed-permission, team tool and sign-in locations come from the root, never a setting."""

    layout = InstanceLayout.at(tmp_path)
    root = tmp_path.resolve()
    expected = {
        layout.mcp_connections_dir: root / "runtime" / "mcp-connections",
        layout.mcp_hosting_definitions_dir: root / "runtime" / "mcp-hosting" / "definitions",
        layout.mcp_hosting_state_dir: root / "runtime" / "mcp-hosting" / "state",
        layout.installed_permissions_dir: root / "runtime" / "installed-permissions",
        layout.instance_tools_dir: root / "control" / "access" / "tools",
        layout.auth_dir: root / "auth",
        layout.openai_codex_auth_path: root / "auth" / "openai_codex_auth.json",
        layout.xai_oauth_auth_path: root / "auth" / "xai_oauth_auth.json",
    }
    for actual, wanted in expected.items():
        assert actual == wanted
        # No agent tool or sandboxed code may reach any of them.
        assert layout.is_instance_private(actual), actual
        assert layout.agent_readable_skill_dir_of(actual) is None, actual


def _old_instance(root):
    workspace = root / "workspace"
    (workspace / "dbs" / "conversation_artifacts" / "c1").mkdir(parents=True)
    (workspace / "dbs" / "task_history.db").write_text("db")
    (workspace / "SOUL.md").write_text("soul")
    (workspace / "TEAM_AGENTS.yaml").write_text("roster")
    (workspace / "agent_bundles" / "team_lead_generated" / "X").mkdir(parents=True)
    (workspace / "artifacts" / "tool_results" / "t1").mkdir(parents=True)
    (workspace / "artifacts" / "web_research" / "t1").mkdir(parents=True)
    (workspace / "notes.md").write_text("team notes")
    return workspace


def test_new_instance_needs_no_moves(tmp_path):
    InstanceLayout.at(tmp_path).ensure_directories()
    assert legacy_layout_moves(tmp_path) == []


def test_old_instance_moves_every_file_to_its_area(tmp_path):
    _old_instance(tmp_path)
    layout = InstanceLayout.at(tmp_path)
    moves = {move.source.relative_to(tmp_path).as_posix(): move.destination for move in legacy_layout_moves(tmp_path)}
    assert moves["workspace/SOUL.md"] == layout.soul_path
    assert moves["workspace/TEAM_AGENTS.yaml"] == layout.team_composition_path
    assert moves["workspace/agent_bundles"] == layout.agent_bundles_dir
    assert moves["workspace/dbs/task_history.db"] == layout.task_database_path
    assert moves["workspace/dbs/conversation_artifacts"] == layout.dbs_dir / "conversation_artifacts"
    assert moves["workspace/artifacts/tool_results"] == layout.tool_results_dir
    # Unknown owner: shared with the team, as it was before.
    assert moves["workspace/artifacts/web_research"] == layout.team_dir / "artifacts" / "web_research"
    assert moves["workspace/notes.md"] == layout.team_dir / "notes.md"


def test_printed_commands_complete_the_move(tmp_path):
    workspace = _old_instance(tmp_path)
    script = render_move_commands(tmp_path, legacy_layout_moves(tmp_path))
    subprocess.run(["sh", "-c", script], check=True)
    layout = InstanceLayout.at(tmp_path)
    assert legacy_layout_moves(tmp_path) == []
    assert not workspace.exists()
    assert layout.soul_path.read_text() == "soul"
    assert layout.task_database_path.read_text() == "db"
    assert (layout.team_dir / "notes.md").read_text() == "team notes"


def test_existing_destination_is_reported_not_overwritten(tmp_path):
    _old_instance(tmp_path)
    layout = InstanceLayout.at(tmp_path)
    layout.control_dir.mkdir(parents=True)
    layout.soul_path.write_text("new soul")
    moves = legacy_layout_moves(tmp_path)
    conflict = next(move for move in moves if move.source.name == "SOUL.md")
    assert conflict.conflict
    message = legacy_layout_message(tmp_path, moves)
    assert "merge" in message and "legacy_instance_layout_present" in message
    assert f"mv -n {conflict.source}" not in message


def test_development_root_with_control_files_in_the_root(tmp_path):
    (tmp_path / "LLM_OVERRIDES.yaml").write_text("x")
    (tmp_path / "config.yaml").write_text("y")
    moves = legacy_layout_moves(tmp_path)
    assert [(m.source.name, m.destination) for m in moves] == [
        ("LLM_OVERRIDES.yaml", InstanceLayout.at(tmp_path).llm_overrides_path)
    ]
