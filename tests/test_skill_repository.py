from __future__ import annotations

import json
from pathlib import Path
import subprocess

import pytest

import custom_agents.skill_repository as skill_repository
from custom_agents.skill_repository import (
    SkillRepositoryError,
    SkillScanRejected,
    SkillSelectionRequired,
    build_skill_tree_manifest,
    discover_skill_candidates,
    scan_skill_with_skillspector,
    select_skill_candidate,
    validate_github_repository_url,
)


def _write_skill(root: Path, relative: str, name: str = "example-skill") -> Path:
    skill = root / relative
    skill.mkdir(parents=True)
    (skill / "SKILL.md").write_text(
        f"---\nname: {name}\ndescription: Test skill.\n---\nUse this safely.\n",
        encoding="utf-8",
    )
    return skill


def _safe_report(*, score: int = 7, recommendation: str = "SAFE") -> dict:
    return {
        "skill": {"name": "example-skill", "source": "/quarantine/artifact"},
        "risk_assessment": {
            "score": score,
            "severity": "LOW" if score <= 20 else "MEDIUM",
            "recommendation": recommendation,
        },
        "components": [{"path": "SKILL.md", "type": "markdown"}],
        "issues": [{"id": "RP1", "severity": "medium"}],
        "metadata": {"skillspector_version": "2.5.1", "llm_requested": False},
        "execution_successful": True,
        "analysis_completeness": {
            "coverage_percent": 100.0,
            "execution_successful": True,
            "partially_inspected_files": 0,
            "entirely_uninspected_files": 0,
            "ledger_exceptions": [],
            "scope_exclusions": [],
            "analyzer_statuses": [
                {
                    "analyzer_id": "static_yara",
                    "status": "completed",
                    "failed": 0,
                    "unaccounted": 0,
                },
                {
                    "analyzer_id": "semantic_security_discovery",
                    "status": "disabled",
                    "failed": 0,
                    "unaccounted": 0,
                    "reason_code": "disabled_by_configuration",
                },
                {
                    "analyzer_id": "meta_analyzer",
                    "status": "disabled",
                    "failed": 0,
                    "unaccounted": 0,
                    "reason_code": "disabled_by_configuration",
                },
            ],
        },
    }


def test_github_repository_url_is_public_exact_and_canonical() -> None:
    assert (
        validate_github_repository_url("https://github.com/NVIDIA/skillspector")
        == "https://github.com/NVIDIA/skillspector.git"
    )
    assert (
        validate_github_repository_url("https://github.com/NVIDIA/skillspector.git")
        == "https://github.com/NVIDIA/skillspector.git"
    )
    for unsafe in (
        "http://github.com/NVIDIA/skillspector",
        "https://gitlab.com/NVIDIA/skillspector",
        "https://token@github.com/NVIDIA/skillspector",
        "https://github.com/NVIDIA/skillspector?ref=main",
        "https://github.com/NVIDIA/skillspector/tree/main/skills",
    ):
        with pytest.raises(ValueError):
            validate_github_repository_url(unsafe)


def test_nested_skill_discovery_and_exact_path_disambiguation(tmp_path: Path) -> None:
    checkout = tmp_path / "repo"
    _write_skill(checkout, "skills/alpha", "shared")
    _write_skill(checkout, "packages/agents/skills/beta", "shared")
    _write_skill(checkout, "docs/not-a-skill", "ignored")

    candidates = discover_skill_candidates(checkout)

    assert [item.relative_path for item in candidates] == [
        "packages/agents/skills/beta",
        "skills/alpha",
    ]
    with pytest.raises(SkillSelectionRequired) as ambiguous:
        select_skill_candidate(candidates, skill_name="shared")
    assert len(ambiguous.value.candidates) == 2
    selected = select_skill_candidate(
        candidates,
        skill_name="shared",
        skill_path="packages/agents/skills/beta",
    )
    assert selected.relative_path == "packages/agents/skills/beta"


def test_discovery_requires_frontmatter_and_rejects_case_collisions(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    checkout = tmp_path / "repo"
    skill = checkout / "nested" / "skills" / "bad"
    skill.mkdir(parents=True)
    (skill / "SKILL.md").write_text("No frontmatter", encoding="utf-8")
    with pytest.raises(SkillRepositoryError, match="frontmatter"):
        discover_skill_candidates(checkout)

    checkout = tmp_path / "collision"
    skill = _write_skill(checkout, "skills/collision")
    # APFS is commonly case-insensitive, so simulate the directory entry list
    # that a case-sensitive production filesystem can expose.
    monkeypatch.setattr(
        skill_repository.os,
        "walk",
        lambda *_args, **_kwargs: iter([(str(skill), [], ["SKILL.md", "skill.md"])]),
    )
    with pytest.raises(SkillRepositoryError, match="case-colliding"):
        discover_skill_candidates(checkout)


def test_tree_manifest_binds_every_file_and_rejects_symlinks_and_lfs(tmp_path: Path) -> None:
    skill = _write_skill(tmp_path, "skills/example")
    (skill / "references").mkdir()
    resource = skill / "references" / "notes.md"
    resource.write_text("first", encoding="utf-8")
    first = build_skill_tree_manifest(skill)
    resource.write_text("second", encoding="utf-8")
    second = build_skill_tree_manifest(skill)
    assert first.tree_sha256 != second.tree_sha256

    (skill / "linked").symlink_to(resource)
    with pytest.raises(SkillRepositoryError, match="symbolic link"):
        build_skill_tree_manifest(skill)
    (skill / "linked").unlink()

    resource.write_bytes(
        b"version https://git-lfs.github.com/spec/v1\n"
        b"oid sha256:" + b"a" * 64 + b"\nsize 100\n"
    )
    with pytest.raises(SkillRepositoryError, match="LFS pointer"):
        build_skill_tree_manifest(skill)


def test_skillspector_static_scan_accepts_only_complete_safe_report(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    skill = _write_skill(tmp_path, "artifact")
    report = _safe_report()
    calls: list[tuple[tuple[str, ...], dict[str, str]]] = []

    def fake_run(argv, *, cwd, env, timeout_seconds):
        calls.append((tuple(argv), dict(env)))
        if "--version" in argv:
            return subprocess.CompletedProcess(argv, 0, "SkillSpector v2.5.1\n", "")
        return subprocess.CompletedProcess(argv, 0, json.dumps(report), "")

    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "must-not-pass")
    monkeypatch.setenv("OPENAI_API_KEY", "must-not-pass")
    monkeypatch.setattr(skill_repository, "_run_command", fake_run)

    result = scan_skill_with_skillspector(
        skill,
        scanner_executable="/opt/skillspector/bin/skillspector",
    )

    assert result.score == 7
    assert result.issue_count == 1
    assert result.recommendation == "SAFE"
    assert calls[1][0][-3:] == ("--no-llm", "--format", "json")
    assert "AWS_SECRET_ACCESS_KEY" not in calls[1][1]
    assert "OPENAI_API_KEY" not in calls[1][1]


@pytest.mark.parametrize(
    ("mutate", "message"),
    [
        (lambda report: report.update(execution_successful=False), "complete"),
        (
            lambda report: report["analysis_completeness"].update(
                entirely_uninspected_files=1
            ),
            "uninspected",
        ),
        (
            lambda report: report["analysis_completeness"]["analyzer_statuses"][0].update(
                failed=1
            ),
            "incomplete",
        ),
        (
            lambda report: report["analysis_completeness"]["analyzer_statuses"].append(
                {
                    "analyzer_id": "unreviewed_disabled_analyzer",
                    "status": "disabled",
                    "failed": 0,
                    "unaccounted": 0,
                    "reason_code": "disabled_by_configuration",
                }
            ),
            "Unexpected disabled",
        ),
        (
            lambda report: report["risk_assessment"].update(
                score=21,
                severity="MEDIUM",
                recommendation="CAUTION",
            ),
            "rejected",
        ),
    ],
)
def test_skillspector_scan_fails_closed_on_unsafe_or_incomplete_evidence(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    mutate,
    message: str,
) -> None:
    skill = _write_skill(tmp_path, "artifact")
    report = _safe_report()
    mutate(report)

    def fake_run(argv, *, cwd, env, timeout_seconds):
        if "--version" in argv:
            return subprocess.CompletedProcess(argv, 0, "SkillSpector v2.5.1\n", "")
        return subprocess.CompletedProcess(argv, 0, json.dumps(report), "")

    monkeypatch.setattr(skill_repository, "_run_command", fake_run)
    with pytest.raises(SkillScanRejected, match=message):
        scan_skill_with_skillspector(skill, scanner_executable="skillspector")


def test_skillspector_scan_rejects_malformed_json(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    skill = _write_skill(tmp_path, "artifact")

    def malformed(argv, *, cwd, env, timeout_seconds):
        if "--version" in argv:
            return subprocess.CompletedProcess(argv, 0, "SkillSpector v2.5.1\n", "")
        return subprocess.CompletedProcess(argv, 0, "not-json", "")

    monkeypatch.setattr(skill_repository, "_run_command", malformed)
    with pytest.raises(SkillScanRejected, match="valid JSON"):
        scan_skill_with_skillspector(skill, scanner_executable="skillspector")


def test_scanner_subprocess_timeout_is_fail_closed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def timed_out(*args, **kwargs):
        raise subprocess.TimeoutExpired(cmd="skillspector", timeout=12)

    monkeypatch.setattr(skill_repository.subprocess, "run", timed_out)
    with pytest.raises(SkillRepositoryError, match="timed out after 12 seconds"):
        skill_repository._run_command(
            ("skillspector", "scan"),
            cwd=tmp_path,
            env={},
            timeout_seconds=12,
        )
