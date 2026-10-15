from __future__ import annotations

import json
from pathlib import Path
import shutil

import pytest

from custom_agents.external_skills import (
    EXTERNAL_SKILLS_MANIFEST,
    ExternalSkillsSyncError,
    normalize_external_skill_repository_urls,
    sync_external_skill_repositories,
)


def _write_repository(root: Path, skills: dict[str, dict[str, str]]) -> Path:
    checkout = root
    for skill_name, files in skills.items():
        skill_dir = checkout / "skills" / skill_name
        skill_dir.mkdir(parents=True, exist_ok=True)
        for relative_path, content in files.items():
            path = skill_dir / relative_path
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(content, encoding="utf-8")
    return checkout


def _clone_from(sources: dict[str, Path]):
    def clone(url: str, destination: Path) -> None:
        shutil.copytree(sources[url], destination, symlinks=True)

    return clone


def test_sync_external_skills_replaces_managed_directories_and_preserves_local_skills(
    tmp_path: Path,
) -> None:
    first_url = "https://github.com/example/first.git"
    second_url = "https://github.com/example/second.git"
    first = _write_repository(
        tmp_path / "first",
        {
            "alpha": {"SKILL.md": "---\nname: alpha\ndescription: first\n---\nfirst\n"},
            "beta": {"SKILL.md": "---\nname: beta\ndescription: beta\n---\nbeta\n"},
        },
    )
    second = _write_repository(
        tmp_path / "second",
        {"alpha": {"SKILL.md": "---\nname: alpha\ndescription: second\n---\nsecond\n"}},
    )
    destination = tmp_path / "workspace" / "skills"
    (destination / "local-only").mkdir(parents=True)
    (destination / "local-only" / "SKILL.md").write_text("local", encoding="utf-8")
    (destination / "alpha").mkdir()
    (destination / "alpha" / "stale.txt").write_text("stale", encoding="utf-8")

    result = sync_external_skill_repositories(
        [first_url, second_url],
        destination,
        clone_repository=_clone_from({first_url: first, second_url: second}),
    )

    assert result.managed_skills == ("alpha", "beta")
    assert result.collisions == ("alpha",)
    assert result.removed_skills == ()
    assert "description: second" in (destination / "alpha" / "SKILL.md").read_text(encoding="utf-8")
    assert not (destination / "alpha" / "stale.txt").exists()
    assert (destination / "beta" / "SKILL.md").is_file()
    assert (destination / "local-only" / "SKILL.md").read_text(encoding="utf-8") == "local"
    manifest = json.loads((destination / EXTERNAL_SKILLS_MANIFEST).read_text(encoding="utf-8"))
    assert manifest["managed_skills"] == ["alpha", "beta"]


def test_sync_external_skills_removes_upstream_deleted_managed_skill(tmp_path: Path) -> None:
    url = "https://github.com/example/skills.git"
    first = _write_repository(
        tmp_path / "first",
        {
            "alpha": {"SKILL.md": "---\nname: alpha\ndescription: one\n---\none\n"},
            "beta": {"SKILL.md": "---\nname: beta\ndescription: one\n---\none\n"},
        },
    )
    second = _write_repository(
        tmp_path / "second",
        {"alpha": {"SKILL.md": "---\nname: alpha\ndescription: two\n---\ntwo\n"}},
    )
    destination = tmp_path / "skills"
    sync_external_skill_repositories(
        [url],
        destination,
        clone_repository=_clone_from({url: first}),
    )
    (destination / "local-only").mkdir()
    (destination / "local-only" / "SKILL.md").write_text("local", encoding="utf-8")

    result = sync_external_skill_repositories(
        [url],
        destination,
        clone_repository=_clone_from({url: second}),
    )

    assert result.removed_skills == ("beta",)
    assert not (destination / "beta").exists()
    assert "description: two" in (destination / "alpha" / "SKILL.md").read_text(encoding="utf-8")
    assert (destination / "local-only" / "SKILL.md").is_file()


def test_sync_external_skills_does_not_mutate_destination_when_staging_fails(
    tmp_path: Path,
) -> None:
    destination = tmp_path / "skills"
    destination.mkdir()
    marker = destination / "keep.txt"
    marker.write_text("unchanged", encoding="utf-8")

    def fail_clone(_url: str, _destination: Path) -> None:
        raise RuntimeError("network down")

    with pytest.raises(ExternalSkillsSyncError, match="network down"):
        sync_external_skill_repositories(
            ["https://github.com/example/skills.git"],
            destination,
            clone_repository=fail_clone,
        )

    assert marker.read_text(encoding="utf-8") == "unchanged"
    assert list(destination.iterdir()) == [marker]


def test_external_skill_repository_normalization_rejects_credentials_and_deduplicates() -> None:
    assert normalize_external_skill_repository_urls(
        ["", "https://github.com/example/skills", "https://github.com/example/skills.git"]
    ) == ["https://github.com/example/skills.git"]
    with pytest.raises(ValueError, match="must not contain credentials"):
        normalize_external_skill_repository_urls(
            ["https://user:secret@github.com/example/skills.git"]
        )


def test_sync_external_skills_rejects_repository_symlinks(tmp_path: Path) -> None:
    url = "https://github.com/example/skills.git"
    checkout = _write_repository(
        tmp_path / "checkout",
        {"alpha": {"SKILL.md": "---\nname: alpha\ndescription: one\n---\none\n"}},
    )
    (checkout / "skills" / "alpha" / "outside").symlink_to(tmp_path)

    with pytest.raises(ExternalSkillsSyncError, match="symbolic link"):
        sync_external_skill_repositories(
            [url],
            tmp_path / "destination",
            clone_repository=_clone_from({url: checkout}),
        )


def test_sync_external_skills_discovers_nested_skills_directories(tmp_path: Path) -> None:
    url = "https://github.com/example/nested-skills.git"
    checkout = tmp_path / "checkout"
    skill_dir = checkout / "packages" / "agents" / "skills" / "nested"
    skill_dir.mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text(
        "---\nname: nested\ndescription: Nested test skill.\n---\nNested.\n",
        encoding="utf-8",
    )

    result = sync_external_skill_repositories(
        [url],
        tmp_path / "destination",
        clone_repository=_clone_from({url: checkout}),
    )

    assert result.managed_skills == ("nested",)
    assert (result.destination / "nested" / "SKILL.md").is_file()
