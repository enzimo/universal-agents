"""Explicit live validation for external skill repository refresh."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from custom_agents.external_skills import sync_external_skill_repositories


pytestmark = [
    pytest.mark.live,
    pytest.mark.external,
    pytest.mark.skipif(
        os.getenv("RUN_LIVE_EXTERNAL_SKILLS_REFRESH") != "1",
        reason="Set RUN_LIVE_EXTERNAL_SKILLS_REFRESH=1 to run the public skill refresh test.",
    ),
]


def test_live_public_external_skills_refresh(tmp_path: Path) -> None:
    repository = os.getenv(
        "UA_LIVE_SKILLS_REPOSITORY",
        "https://github.com/enzimo/ua-skills",
    )
    destination = tmp_path / "workspace" / "skills"

    first = sync_external_skill_repositories([repository], destination)
    assert first.refreshed_count > 0
    assert "gh" in first.managed_skills
    marker = destination / "gh" / "stale-local-marker.txt"
    marker.write_text("must be removed by exact external replacement", encoding="utf-8")
    local_skill = destination / "local-only"
    local_skill.mkdir()
    (local_skill / "SKILL.md").write_text(
        "---\nname: local-only\ndescription: Local-only test skill.\n---\nlocal\n",
        encoding="utf-8",
    )

    second = sync_external_skill_repositories([repository], destination)

    assert second.managed_skills == first.managed_skills
    assert not marker.exists()
    assert (local_skill / "SKILL.md").is_file()
