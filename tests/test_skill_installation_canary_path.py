"""The skill-installation canary's team-notification path, without GitHub or SkillSpector.

``tests/live_skill_installation_canary.py`` runs in CI with a real GitHub skill and the real
scanner. Its last step, after a team skill changed, refreshes the lead's skills and tells the
team with a ``skills.changed`` event (``custom_agents.team_skill_changes``). This runs that same
step with the canary's own stand-in lead and event recorder, so its team-notification checks
are exercised where the canary cannot run.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from assistant_gateway.config import GatewayInstanceConfig
from custom_agents import agent_requests
from custom_agents.team_skill_changes import reload_runtime_skills_after_change
from tests.live_skill_installation_canary import DEFAULT_BINARY, _lead, _told, recording_team_events


@pytest.fixture
def lead(tmp_path: Path):
    config = GatewayInstanceConfig.create(instance_name="skill-installation-canary", instance_root=str(tmp_path / "instance"),
                                          team_id="team_skill_installation_canary", timezone="UTC")
    config_path = config.save(tmp_path / "instance" / "instance_config.yaml")
    return _lead(config, config_path, DEFAULT_BINARY)


def test_an_install_and_a_removal_each_tell_the_team_with_a_skills_changed_event(lead) -> None:
    installed = {"change_id": "skill_install_0123456789ab", "skill_name": "grill-me", "installed_tree_sha256": "a" * 64}
    removed = {"change_id": "skill_uninstall_0123456789ab", "skill_name": "grill-me", "installed_tree_sha256": "a" * 64}
    original = agent_requests.announce

    with recording_team_events(lead) as events:
        asyncio.run(reload_runtime_skills_after_change(lead, installed, operation="install"))
        assert _told(lead, "install", installed)
        asyncio.run(reload_runtime_skills_after_change(lead, removed, operation="uninstall"))
        assert _told(lead, "uninstall", removed)

    assert agent_requests.announce is original, "the recorder is removed afterwards"
    assert [kind for kind, _ in events] == ["skills.changed", "skills.changed"]
    for result in (installed, removed):
        refresh = result["runtime_skill_refresh"]
        assert (refresh["status"], refresh["team_notification"]) == ("completed", "published"), refresh
