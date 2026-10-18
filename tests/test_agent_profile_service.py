"""TeamArchitect's protected profile (SOUL.md).

The profile changes only through an approved runtime settings plan
(tests/test_runtime_settings_tools.py covers the approval); here: generic file
and shell tools cannot write it, and the profile service validates content and
replaces the file atomically.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from custom_agents.agent_profile_service import AgentProfileService
from custom_tools.non_interactive_shell import _execute_single_command
from custom_tools.path_policy import (
    PROTECTED_RUNTIME_CONTROL_DENIED,
    PathPolicyError,
    ensure_allowed_path,
)
from custom_tools.runtime_context import set_agent_ref
from runtime_settings.instance_layout import InstanceLayout


@pytest.fixture
def layout(tmp_path) -> InstanceLayout:
    layout = InstanceLayout.at(tmp_path / "instance")
    layout.ensure_directories()
    return layout


def test_generic_file_and_shell_boundaries_block_soul_writes(
    layout, monkeypatch, commands_without_agent
) -> None:
    # TeamArchitect's SOUL.md lives in control/, outside every folder agent
    # tools may open; tasks for no person work in workspaces/agents.
    monkeypatch.setenv("AGENT_WORKSPACE_DIR", str(layout.workspaces_dir))
    monkeypatch.setenv("TEAM_INSTANCE_ROOT", str(layout.root))
    set_agent_ref(None)

    for candidate in (str(layout.soul_path), "../../control/SOUL.md"):
        with pytest.raises(
            PathPolicyError, match="Team control files cannot be opened through generic tools"
        ) as raised:
            ensure_allowed_path(candidate, for_write=True)
        assert raised.value.reason_code == PROTECTED_RUNTIME_CONTROL_DENIED

    for command in (
        f"printf changed > {layout.soul_path}",
        "printf changed > ../../control/SOUL.md",
    ):
        result = _execute_single_command(
            command,
            default_work_dir=str(layout.agents_dir),
            default_timeout=5,
        )
        assert result["status"] == "error"
        assert result["exit_code"] == 126
        assert "Team control files" in result["error"]
        assert result["reason_code"] == PROTECTED_RUNTIME_CONTROL_DENIED
    assert not layout.soul_path.exists()


def test_profile_service_validates_and_replaces_the_profile_atomically(layout) -> None:
    layout.soul_path.write_text("old soul\n", encoding="utf-8")
    lead = SimpleNamespace(
        id="lead_1", team_id="team_1", is_team_lead=True,
        instance_root=str(layout.root), workspace_dir=str(layout.workspaces_dir),
    )

    result = AgentProfileService(lead).update_team_lead_soul("new soul\n")

    assert result["status"] == "updated" and result["bytes_written"] == len("new soul\n")
    assert layout.soul_path.read_text(encoding="utf-8") == "new soul\n"
    assert not list(layout.control_dir.glob(".SOUL.md.*.tmp"))
    with pytest.raises(ValueError, match="cannot be empty"):
        AgentProfileService(lead).update_team_lead_soul("   ")
    with pytest.raises(ValueError, match="cannot exceed"):
        AgentProfileService(lead).update_team_lead_soul("x" * 1_000_000)
    with pytest.raises(PermissionError, match="only TeamArchitect"):
        AgentProfileService(SimpleNamespace(is_team_lead=False)).update_team_lead_soul("worker soul\n")
    assert layout.soul_path.read_text(encoding="utf-8") == "new soul\n"
