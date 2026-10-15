"""Refresh externally managed skills without coupling to the agent SDK.

External repositories are cloned into temporary directories and fully staged
before the configured skills directory is changed.  A small manifest records
only the skill directories owned by this importer, allowing later refreshes to
remove upstream-deleted skills without touching unrelated local skills.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable
from dataclasses import dataclass
import json
import logging
import os
from pathlib import Path, PurePosixPath
import shutil
import tempfile
from typing import Any
import uuid

from custom_agents.skill_repository import (
    clone_public_github_repository,
    discover_skill_candidates,
    validate_github_repository_url,
)


MAX_EXTERNAL_SKILL_REPOSITORIES = 12
EXTERNAL_SKILLS_MANIFEST = ".ua-external-skills.json"
EXTERNAL_SKILLS_MANIFEST_SCHEMA_VERSION = 1
GIT_CLONE_TIMEOUT_SECONDS = 60
LOGGER = logging.getLogger(__name__)


class ExternalSkillsSyncError(RuntimeError):
    """Raised when an external skills refresh cannot be completed safely."""


@dataclass(frozen=True)
class ExternalSkillRepositoryResult:
    url: str
    skills: tuple[str, ...]

    def as_dict(self, *, destination: Path) -> dict[str, Any]:
        return {
            "url": self.url,
            "ok": True,
            "copied": len(self.skills),
            "skills": list(self.skills),
            "message": (
                f"Refreshed {len(self.skills)} skill"
                f"{'s' if len(self.skills) != 1 else ''}."
            ),
            "skills_dir": str(destination),
        }


@dataclass(frozen=True)
class ExternalSkillsSyncResult:
    destination: Path
    repositories: tuple[ExternalSkillRepositoryResult, ...]
    managed_skills: tuple[str, ...]
    removed_skills: tuple[str, ...]
    collisions: tuple[str, ...]

    @property
    def refreshed_count(self) -> int:
        return len(self.managed_skills)


CloneRepository = Callable[[str, Path], None]


def validate_external_skill_repository_url(raw_url: str) -> str:
    return validate_github_repository_url(raw_url)


def normalize_external_skill_repository_urls(raw_repositories: Any) -> list[str]:
    if raw_repositories is None:
        return []
    if not isinstance(raw_repositories, (list, tuple)):
        raise ValueError("External skill repositories must be a list of public GitHub URLs.")
    repositories: list[str] = []
    seen: set[str] = set()
    for raw_url in raw_repositories:
        rendered = str(raw_url or "").strip()
        if not rendered:
            continue
        url = validate_external_skill_repository_url(rendered)
        if url in seen:
            continue
        seen.add(url)
        repositories.append(url)
        if len(repositories) > MAX_EXTERNAL_SKILL_REPOSITORIES:
            raise ValueError(
                f"At most {MAX_EXTERNAL_SKILL_REPOSITORIES} external skill repositories are supported."
            )
    return repositories


def clone_external_skill_repository(url: str, destination: Path) -> None:
    try:
        clone_public_github_repository(
            url,
            destination,
            timeout_seconds=GIT_CLONE_TIMEOUT_SECONDS,
        )
    except Exception as exc:
        raise ExternalSkillsSyncError(str(exc)) from exc


def _remove_path(path: Path) -> None:
    if path.is_symlink() or path.is_file():
        path.unlink(missing_ok=True)
    elif path.is_dir():
        shutil.rmtree(path)


def _reject_symlinks(skill_dir: Path, *, repository_url: str) -> None:
    if skill_dir.is_symlink():
        raise ExternalSkillsSyncError(
            f"External skill {skill_dir.name!r} from {repository_url} is a symbolic link."
        )
    for path in skill_dir.rglob("*"):
        if path.is_symlink():
            raise ExternalSkillsSyncError(
                f"External skill {skill_dir.name!r} from {repository_url} contains a symbolic link: "
                f"{path.relative_to(skill_dir)}"
            )


def _discover_skill_directories(checkout: Path, *, repository_url: str) -> dict[str, Path]:
    discovered: dict[str, Path] = {}
    for candidate in discover_skill_candidates(checkout):
        if candidate.name in discovered:
            raise ExternalSkillsSyncError(
                f"External repository contains duplicate semantic skill name {candidate.name!r}: "
                f"{repository_url}"
            )
        candidate_path = checkout / PurePosixPath(candidate.relative_path)
        _reject_symlinks(candidate_path, repository_url=repository_url)
        discovered[candidate.name] = candidate_path
    return discovered


def _load_managed_skill_names(destination: Path) -> set[str]:
    manifest_path = destination / EXTERNAL_SKILLS_MANIFEST
    if not manifest_path.is_file():
        return set()
    try:
        payload = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        LOGGER.warning(
            "Ignoring unreadable external skills manifest %s; previously managed skills deleted upstream may remain: %s",
            manifest_path,
            exc,
        )
        return set()
    if payload.get("schema_version") != EXTERNAL_SKILLS_MANIFEST_SCHEMA_VERSION:
        LOGGER.warning(
            "Ignoring external skills manifest %s with unsupported schema_version=%r; "
            "previously managed skills deleted upstream may remain.",
            manifest_path,
            payload.get("schema_version"),
        )
        return set()
    raw_names = payload.get("managed_skills")
    if not isinstance(raw_names, list):
        LOGGER.warning(
            "Ignoring external skills manifest %s without a managed_skills list; "
            "previously managed skills deleted upstream may remain.",
            manifest_path,
        )
        return set()
    return {
        str(name).strip()
        for name in raw_names
        if str(name).strip() and Path(str(name).strip()).name == str(name).strip()
    }


def _write_manifest(
    destination: Path,
    *,
    repositories: Iterable[ExternalSkillRepositoryResult],
    managed_skills: Iterable[str],
) -> None:
    manifest_path = destination / EXTERNAL_SKILLS_MANIFEST
    temp_path = destination / f".{EXTERNAL_SKILLS_MANIFEST}.{uuid.uuid4().hex}.tmp"
    payload = {
        "schema_version": EXTERNAL_SKILLS_MANIFEST_SCHEMA_VERSION,
        "repositories": [
            {"url": result.url, "skills": list(result.skills)}
            for result in repositories
        ],
        "managed_skills": sorted(managed_skills),
    }
    temp_path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    os.replace(temp_path, manifest_path)


def _apply_staged_skills(
    destination: Path,
    *,
    staged_skills: dict[str, Path],
    previous_managed_skills: set[str],
    repository_results: tuple[ExternalSkillRepositoryResult, ...],
) -> tuple[str, ...]:
    destination.mkdir(parents=True, exist_ok=True)
    current_names = set(staged_skills)
    affected_names = previous_managed_skills | current_names
    removed_names = tuple(sorted(previous_managed_skills - current_names))
    backup_root = destination / f".ua-external-skills-backup-{uuid.uuid4().hex}"
    backup_root.mkdir()
    try:
        for name in sorted(affected_names):
            target = destination / name
            if target.exists() or target.is_symlink():
                shutil.move(str(target), str(backup_root / name))
        try:
            for name, source in sorted(staged_skills.items()):
                shutil.copytree(source, destination / name)
            _write_manifest(
                destination,
                repositories=repository_results,
                managed_skills=current_names,
            )
        except Exception:
            for name in sorted(affected_names):
                _remove_path(destination / name)
            for backup in sorted(backup_root.iterdir(), key=lambda path: path.name):
                shutil.move(str(backup), str(destination / backup.name))
            raise
    finally:
        shutil.rmtree(backup_root, ignore_errors=True)
    return removed_names


def sync_external_skill_repositories(
    repositories: Iterable[str],
    destination: str | Path,
    *,
    clone_repository: CloneRepository | None = None,
) -> ExternalSkillsSyncResult:
    normalized_repositories = normalize_external_skill_repository_urls(list(repositories))
    if not normalized_repositories:
        raise ExternalSkillsSyncError(
            "External skill refresh was requested, but no external skill repositories are configured."
        )
    destination_path = Path(destination).expanduser().resolve()
    if destination_path.exists() and not destination_path.is_dir():
        raise ExternalSkillsSyncError(
            f"Configured skills destination is not a directory: {destination_path}"
        )
    clone = clone_repository or clone_external_skill_repository
    staged_skills: dict[str, Path] = {}
    staged_skill_sources: dict[str, str] = {}
    collisions: set[str] = set()
    repository_results: list[ExternalSkillRepositoryResult] = []
    with tempfile.TemporaryDirectory(prefix="ua-external-skills-") as temp_dir:
        temp_root = Path(temp_dir)
        for index, url in enumerate(normalized_repositories, start=1):
            checkout = temp_root / f"repository-{index}"
            try:
                clone(url, checkout)
                discovered = _discover_skill_directories(checkout, repository_url=url)
            except (ExternalSkillsSyncError, ValueError):
                raise
            except Exception as exc:
                raise ExternalSkillsSyncError(
                    f"Could not stage external skills repository {url}: {exc}"
                ) from exc
            repository_results.append(
                ExternalSkillRepositoryResult(url=url, skills=tuple(sorted(discovered)))
            )
            for skill_name in discovered:
                if skill_name in staged_skill_sources:
                    collisions.add(skill_name)
                staged_skill_sources[skill_name] = url
            staged_skills.update(discovered)

        if collisions:
            LOGGER.warning(
                "External skill repositories contain duplicate skill names; later repositories won: %s",
                ", ".join(sorted(collisions)),
            )

        previous_managed_skills = _load_managed_skill_names(destination_path)
        result_tuple = tuple(repository_results)
        try:
            removed_skills = _apply_staged_skills(
                destination_path,
                staged_skills=staged_skills,
                previous_managed_skills=previous_managed_skills,
                repository_results=result_tuple,
            )
        except Exception as exc:
            if isinstance(exc, ExternalSkillsSyncError):
                raise
            raise ExternalSkillsSyncError(
                f"Could not apply refreshed external skills to {destination_path}: {exc}"
            ) from exc
    return ExternalSkillsSyncResult(
        destination=destination_path,
        repositories=result_tuple,
        managed_skills=tuple(sorted(staged_skills)),
        removed_skills=removed_skills,
        collisions=tuple(sorted(collisions)),
    )


__all__ = [
    "EXTERNAL_SKILLS_MANIFEST",
    "ExternalSkillsSyncError",
    "ExternalSkillsSyncResult",
    "MAX_EXTERNAL_SKILL_REPOSITORIES",
    "clone_external_skill_repository",
    "normalize_external_skill_repository_urls",
    "sync_external_skill_repositories",
    "validate_external_skill_repository_url",
]
