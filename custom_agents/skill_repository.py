"""Safe acquisition and static inspection of skills from public GitHub repositories.

This module deliberately has no Strands or authorization dependencies.  It is
the untrusted-input boundary shared by the agent installation workflow and its
live tests: repository coordinates are normalized, Git runs without ambient
credentials, selected skill trees are copied into quarantine, and a complete
SkillSpector report is required before an artifact can be considered safe.
"""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import stat
import subprocess
from typing import Any, Mapping, Sequence
from urllib.parse import urlparse

import yaml


GITHUB_HOST = "github.com"
GIT_CLONE_TIMEOUT_SECONDS = 90
SCANNER_TIMEOUT_SECONDS = 180
MAX_REPOSITORY_BYTES = 100 * 1024 * 1024
MAX_SKILL_FILES = 500
MAX_SKILL_BYTES = 20 * 1024 * 1024
MAX_SINGLE_FILE_BYTES = 5 * 1024 * 1024
MAX_SCANNER_OUTPUT_BYTES = 10 * 1024 * 1024
DEFAULT_SAFE_MAX_SCORE = 20
_SHA256_PATTERN = re.compile(r"[0-9a-f]{64}\Z")
_COMMIT_PATTERN = re.compile(r"[0-9a-f]{40}\Z")
_SKILL_NAME_PATTERN = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}\Z")
_LFS_POINTER_PREFIX = b"version https://git-lfs.github.com/spec/v1\n"
_STATIC_MODE_DISABLED_ANALYZERS = frozenset(
    {
        "meta_analyzer",
        "semantic_developer_intent",
        "semantic_quality_policy",
        "semantic_security_discovery",
    }
)


class SkillRepositoryError(RuntimeError):
    """Raised when an untrusted repository or skill cannot be accepted safely."""


class SkillSelectionRequired(SkillRepositoryError):
    """Raised when an exact candidate path is needed to disambiguate a request."""

    def __init__(self, message: str, candidates: Sequence["SkillCandidate"]) -> None:
        super().__init__(message)
        self.candidates = tuple(candidates)


class SkillScanRejected(SkillRepositoryError):
    """Raised when SkillSpector did not produce a complete SAFE result."""


@dataclass(frozen=True)
class SkillCandidate:
    name: str
    relative_path: str

    def as_dict(self) -> dict[str, str]:
        return {"name": self.name, "relative_path": self.relative_path}


@dataclass(frozen=True)
class SkillTreeEntry:
    path: str
    size_bytes: int
    sha256: str

    def as_dict(self) -> dict[str, Any]:
        return {
            "path": self.path,
            "size_bytes": self.size_bytes,
            "sha256": self.sha256,
        }


@dataclass(frozen=True)
class SkillTreeManifest:
    entries: tuple[SkillTreeEntry, ...]
    total_bytes: int
    tree_sha256: str

    def as_dict(self) -> dict[str, Any]:
        return {
            "entries": [entry.as_dict() for entry in self.entries],
            "file_count": len(self.entries),
            "total_bytes": self.total_bytes,
            "tree_sha256": self.tree_sha256,
        }


@dataclass(frozen=True)
class SkillScanResult:
    scanner_name: str
    scanner_version: str
    mode: str
    score: int
    severity: str
    recommendation: str
    issue_count: int
    report_sha256: str
    report: Mapping[str, Any]

    def summary(self) -> dict[str, Any]:
        return {
            "scanner_name": self.scanner_name,
            "scanner_version": self.scanner_version,
            "mode": self.mode,
            "score": self.score,
            "severity": self.severity,
            "recommendation": self.recommendation,
            "issue_count": self.issue_count,
            "report_sha256": self.report_sha256,
        }


def _canonical_json(value: Any) -> str:
    return json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        default=str,
    )


def _sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def validate_github_repository_url(raw_url: str) -> str:
    """Return one canonical, public, credential-free GitHub clone URL."""

    rendered = str(raw_url or "").strip()
    parsed = urlparse(rendered)
    if parsed.scheme != "https" or (parsed.hostname or "").casefold() != GITHUB_HOST:
        raise ValueError("Skill repositories must use a public https://github.com URL.")
    if parsed.username or parsed.password or parsed.port:
        raise ValueError("Skill repository URLs must not contain credentials or a port.")
    if parsed.query or parsed.fragment or parsed.params:
        raise ValueError("Skill repository URLs must not contain query parameters or fragments.")
    parts = [part for part in parsed.path.split("/") if part]
    if len(parts) != 2 or any(part in {".", ".."} for part in parts):
        raise ValueError("Use a GitHub repository URL in the form https://github.com/owner/repo.")
    owner, repository = parts
    if repository.endswith(".git"):
        repository = repository[:-4]
    component_pattern = re.compile(r"[A-Za-z0-9_.-]+\Z")
    if not owner or not repository or not component_pattern.fullmatch(owner) or not component_pattern.fullmatch(repository):
        raise ValueError("GitHub owner and repository names contain unsupported characters.")
    return f"https://github.com/{owner}/{repository}.git"


def _safe_process_environment(*, home: Path) -> dict[str, str]:
    env = {
        "GIT_TERMINAL_PROMPT": "0",
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_CONFIG_GLOBAL": os.devnull,
        "GIT_LFS_SKIP_SMUDGE": "1",
        "HOME": str(home),
        "LANG": "C.UTF-8",
        "LC_ALL": "C.UTF-8",
        "NO_COLOR": "1",
        "PATH": os.environ.get("PATH", "/usr/local/bin:/usr/bin:/bin"),
        "PYTHONNOUSERSITE": "1",
    }
    return env


def _last_output_line(*values: str) -> str:
    lines = [line.strip() for value in values for line in value.splitlines() if line.strip()]
    return lines[-1] if lines else "command failed without diagnostic output"


def _run_command(
    argv: Sequence[str],
    *,
    cwd: Path,
    env: Mapping[str, str],
    timeout_seconds: int,
) -> subprocess.CompletedProcess[str]:
    try:
        return subprocess.run(
            list(argv),
            cwd=str(cwd),
            env=dict(env),
            check=False,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            timeout=timeout_seconds,
        )
    except FileNotFoundError as exc:
        raise SkillRepositoryError(f"Required executable is unavailable: {argv[0]}") from exc
    except subprocess.TimeoutExpired as exc:
        raise SkillRepositoryError(
            f"Command timed out after {timeout_seconds} seconds: {argv[0]}"
        ) from exc


def _directory_size(root: Path) -> int:
    total = 0
    for current_root, directories, files in os.walk(root, followlinks=False):
        current = Path(current_root)
        for name in directories:
            path = current / name
            if path.is_symlink():
                total += path.lstat().st_size
        for name in files:
            path = current / name
            total += path.lstat().st_size
    return total


def clone_public_github_repository(
    repository_url: str,
    destination: Path,
    *,
    timeout_seconds: int = GIT_CLONE_TIMEOUT_SECONDS,
    max_repository_bytes: int = MAX_REPOSITORY_BYTES,
) -> str:
    """Clone the default branch without credentials and return its exact commit."""

    canonical_url = validate_github_repository_url(repository_url)
    if destination.exists():
        raise SkillRepositoryError(f"Quarantine destination already exists: {destination}")
    destination.parent.mkdir(parents=True, exist_ok=True)
    home = destination.parent / "git-home"
    home.mkdir(mode=0o700, exist_ok=True)
    env = _safe_process_environment(home=home)
    completed = _run_command(
        (
            "git",
            "-c",
            "credential.helper=",
            "-c",
            "protocol.file.allow=never",
            "clone",
            "--depth",
            "1",
            "--single-branch",
            "--no-tags",
            "--filter=blob:none",
            "--",
            canonical_url,
            str(destination),
        ),
        cwd=destination.parent,
        env=env,
        timeout_seconds=timeout_seconds,
    )
    if completed.returncode != 0:
        raise SkillRepositoryError(
            f"Could not clone {canonical_url}: "
            f"{_last_output_line(completed.stderr, completed.stdout)}"
        )
    if _directory_size(destination) > max_repository_bytes:
        raise SkillRepositoryError(
            f"Repository exceeds the {max_repository_bytes}-byte quarantine limit."
        )
    if (destination / ".gitmodules").exists():
        raise SkillRepositoryError("Repositories containing Git submodules are not accepted.")
    resolved = _run_command(
        ("git", "rev-parse", "HEAD"),
        cwd=destination,
        env=env,
        timeout_seconds=min(timeout_seconds, 30),
    )
    commit_sha = resolved.stdout.strip().lower()
    if resolved.returncode != 0 or not _COMMIT_PATTERN.fullmatch(commit_sha):
        raise SkillRepositoryError("Could not resolve an immutable 40-character Git commit.")
    return commit_sha


def _frontmatter_name(skill_file: Path) -> str:
    try:
        raw = skill_file.read_bytes()
    except OSError as exc:
        raise SkillRepositoryError(f"Could not read {skill_file}: {exc}") from exc
    if len(raw) > MAX_SINGLE_FILE_BYTES:
        raise SkillRepositoryError(f"Skill definition exceeds the single-file limit: {skill_file}")
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise SkillRepositoryError(f"Skill definition must be UTF-8: {skill_file}") from exc
    if not text.startswith("---"):
        raise SkillRepositoryError(f"Skill definition requires YAML frontmatter: {skill_file}")
    lines = text.splitlines()
    try:
        closing = lines.index("---", 1)
    except ValueError as exc:
        raise SkillRepositoryError(f"Skill frontmatter is not closed: {skill_file}") from exc
    try:
        metadata = yaml.safe_load("\n".join(lines[1:closing])) or {}
    except yaml.YAMLError as exc:
        raise SkillRepositoryError(f"Skill frontmatter is invalid YAML: {skill_file}") from exc
    if not isinstance(metadata, Mapping):
        raise SkillRepositoryError(f"Skill frontmatter must be a mapping: {skill_file}")
    name = str(metadata.get("name") or "").strip()
    if not _SKILL_NAME_PATTERN.fullmatch(name):
        raise SkillRepositoryError(
            f"Skill name must match {_SKILL_NAME_PATTERN.pattern!r}: {skill_file}"
        )
    return name


def _is_below_skills_directory(relative: PurePosixPath) -> bool:
    parent_parts = relative.parent.parts
    return any(part.casefold() == "skills" for part in parent_parts[:-1])


def discover_skill_candidates(checkout: Path) -> tuple[SkillCandidate, ...]:
    """Discover skill roots beneath any directory named ``skills``."""

    candidates: list[SkillCandidate] = []
    seen_paths: set[str] = set()
    for current_root, directories, files in os.walk(checkout, followlinks=False):
        current = Path(current_root)
        directories[:] = [name for name in directories if name != ".git"]
        matching = sorted(name for name in files if name.casefold() == "skill.md")
        if not matching:
            continue
        if len(matching) != 1:
            raise SkillRepositoryError(f"Skill directory contains case-colliding definitions: {current}")
        definition = current / matching[0]
        relative_definition = PurePosixPath(definition.relative_to(checkout).as_posix())
        if not _is_below_skills_directory(relative_definition):
            continue
        relative_root = relative_definition.parent.as_posix()
        key = relative_root.casefold()
        if key in seen_paths:
            raise SkillRepositoryError(
                f"Repository contains case-colliding skill paths: {relative_root}"
            )
        seen_paths.add(key)
        candidates.append(
            SkillCandidate(
                name=_frontmatter_name(definition),
                relative_path=relative_root,
            )
        )
    candidates.sort(key=lambda item: (item.name.casefold(), item.relative_path.casefold()))
    if not candidates:
        raise SkillRepositoryError(
            "Repository contains no SKILL.md beneath a directory named 'skills'."
        )
    return tuple(candidates)


def select_skill_candidate(
    candidates: Sequence[SkillCandidate],
    *,
    skill_name: str = "",
    skill_path: str = "",
) -> SkillCandidate:
    normalized_path = str(skill_path or "").strip().strip("/")
    normalized_name = str(skill_name or "").strip().casefold()
    if normalized_path:
        pure = PurePosixPath(normalized_path)
        if pure.is_absolute() or ".." in pure.parts or "." in pure.parts:
            raise ValueError("skill_path must be a normalized repository-relative path.")
        matches = [item for item in candidates if item.relative_path == pure.as_posix()]
        if normalized_name:
            matches = [item for item in matches if item.name.casefold() == normalized_name]
    elif normalized_name:
        matches = [
            item
            for item in candidates
            if item.name.casefold() == normalized_name
            or PurePosixPath(item.relative_path).name.casefold() == normalized_name
        ]
    else:
        matches = list(candidates)
    if len(matches) == 1:
        return matches[0]
    if not matches:
        raise SkillSelectionRequired(
            "No discovered skill matches the requested name/path.", candidates
        )
    raise SkillSelectionRequired(
        "Multiple skills match; provide the exact repository-relative skill_path.", matches
    )


def build_skill_tree_manifest(
    skill_root: Path,
    *,
    max_files: int = MAX_SKILL_FILES,
    max_total_bytes: int = MAX_SKILL_BYTES,
    max_single_file_bytes: int = MAX_SINGLE_FILE_BYTES,
) -> SkillTreeManifest:
    """Validate and digest every regular file in one selected skill tree."""

    if skill_root.is_symlink() or not skill_root.is_dir():
        raise SkillRepositoryError("Selected skill root must be a regular directory.")
    entries: list[SkillTreeEntry] = []
    casefold_paths: set[str] = set()
    total_bytes = 0
    for current_root, directories, files in os.walk(skill_root, followlinks=False):
        current = Path(current_root)
        for name in list(directories):
            path = current / name
            mode = path.lstat().st_mode
            if stat.S_ISLNK(mode):
                raise SkillRepositoryError(
                    f"Skill contains a symbolic link: {path.relative_to(skill_root)}"
                )
            if not stat.S_ISDIR(mode):
                raise SkillRepositoryError(
                    f"Skill contains a non-directory tree entry: {path.relative_to(skill_root)}"
                )
        for name in files:
            path = current / name
            mode = path.lstat().st_mode
            relative = path.relative_to(skill_root).as_posix()
            if stat.S_ISLNK(mode):
                raise SkillRepositoryError(f"Skill contains a symbolic link: {relative}")
            if not stat.S_ISREG(mode):
                raise SkillRepositoryError(f"Skill contains a non-regular file: {relative}")
            if any(ord(character) < 32 for character in relative):
                raise SkillRepositoryError("Skill paths must not contain control characters.")
            casefolded = relative.casefold()
            if casefolded in casefold_paths:
                raise SkillRepositoryError(f"Skill contains case-colliding paths: {relative}")
            casefold_paths.add(casefolded)
            data = path.read_bytes()
            if data.startswith(_LFS_POINTER_PREFIX):
                raise SkillRepositoryError(f"Git LFS pointer was not materialized: {relative}")
            if len(data) > max_single_file_bytes:
                raise SkillRepositoryError(
                    f"Skill file exceeds the {max_single_file_bytes}-byte limit: {relative}"
                )
            total_bytes += len(data)
            if total_bytes > max_total_bytes:
                raise SkillRepositoryError(
                    f"Skill exceeds the {max_total_bytes}-byte aggregate limit."
                )
            entries.append(
                SkillTreeEntry(path=relative, size_bytes=len(data), sha256=_sha256_bytes(data))
            )
            if len(entries) > max_files:
                raise SkillRepositoryError(f"Skill exceeds the {max_files}-file limit.")
    entries.sort(key=lambda item: item.path)
    if not any(entry.path.casefold() == "skill.md" for entry in entries):
        raise SkillRepositoryError("Selected skill tree does not contain SKILL.md.")
    manifest_payload = [entry.as_dict() for entry in entries]
    return SkillTreeManifest(
        entries=tuple(entries),
        total_bytes=total_bytes,
        tree_sha256=_sha256_bytes(_canonical_json(manifest_payload).encode("utf-8")),
    )


def copy_skill_to_quarantine(source: Path, destination: Path) -> SkillTreeManifest:
    """Copy an already selected tree and return the digest of the copy."""

    source_manifest = build_skill_tree_manifest(source)
    if destination.exists():
        raise SkillRepositoryError(f"Quarantined artifact already exists: {destination}")
    shutil.copytree(source, destination, symlinks=False)
    copied_manifest = build_skill_tree_manifest(destination)
    if copied_manifest != source_manifest:
        raise SkillRepositoryError("Skill changed while it was copied into quarantine.")
    return copied_manifest


def _scanner_version(executable: str, *, cwd: Path, env: Mapping[str, str]) -> str:
    completed = _run_command(
        (executable, "--version"),
        cwd=cwd,
        env=env,
        timeout_seconds=30,
    )
    if completed.returncode != 0:
        raise SkillScanRejected(
            f"SkillSpector version check failed: {_last_output_line(completed.stderr, completed.stdout)}"
        )
    match = re.search(r"(?:SkillSpector\s+)?v?([0-9]+(?:\.[0-9]+){1,3})", completed.stdout)
    if not match:
        raise SkillScanRejected("SkillSpector returned an unrecognized version string.")
    return match.group(1)


def _validate_scan_completeness(report: Mapping[str, Any]) -> None:
    if report.get("execution_successful") is not True:
        raise SkillScanRejected("SkillSpector did not complete successfully.")
    completeness = report.get("analysis_completeness")
    if not isinstance(completeness, Mapping):
        raise SkillScanRejected("SkillSpector report lacks analysis_completeness.")
    if completeness.get("execution_successful") is not True:
        raise SkillScanRejected("SkillSpector completeness ledger reports execution failure.")
    if float(completeness.get("coverage_percent", 0)) != 100.0:
        raise SkillScanRejected("SkillSpector did not inspect 100% of discovered components.")
    for field in (
        "partially_inspected_files",
        "entirely_uninspected_files",
    ):
        if int(completeness.get(field, 0) or 0) != 0:
            raise SkillScanRejected(f"SkillSpector reported {field.replace('_', ' ')}.")
    if completeness.get("ledger_exceptions") or completeness.get("scope_exclusions"):
        raise SkillScanRejected("SkillSpector reported ledger exceptions or scope exclusions.")
    statuses = completeness.get("analyzer_statuses")
    if not isinstance(statuses, list) or not statuses:
        raise SkillScanRejected("SkillSpector report lacks analyzer status evidence.")
    for raw in statuses:
        if not isinstance(raw, Mapping):
            raise SkillScanRejected("SkillSpector analyzer status is malformed.")
        status_value = str(raw.get("status") or "")
        analyzer_id = str(raw.get("analyzer_id") or "")
        failed = int(raw.get("failed", 0) or 0)
        unaccounted = int(raw.get("unaccounted", 0) or 0)
        if failed or unaccounted:
            raise SkillScanRejected(f"SkillSpector analyzer {analyzer_id!r} was incomplete.")
        if status_value == "disabled" and not (
            analyzer_id in _STATIC_MODE_DISABLED_ANALYZERS
            and raw.get("reason_code") == "disabled_by_configuration"
        ):
            raise SkillScanRejected(
                f"Unexpected disabled SkillSpector analyzer: {analyzer_id or 'unknown'}"
            )
        if status_value not in {"completed", "not_applicable", "disabled"}:
            raise SkillScanRejected(
                f"SkillSpector analyzer {analyzer_id!r} has unsafe status {status_value!r}."
            )


def scan_skill_with_skillspector(
    skill_root: Path,
    *,
    scanner_executable: str,
    safe_max_score: int = DEFAULT_SAFE_MAX_SCORE,
    timeout_seconds: int = SCANNER_TIMEOUT_SECONDS,
    max_output_bytes: int = MAX_SCANNER_OUTPUT_BYTES,
) -> SkillScanResult:
    """Run static-only SkillSpector and require its strict SAFE band."""

    executable = str(scanner_executable or "").strip()
    if not executable:
        raise SkillScanRejected("SkillSpector executable is not configured.")
    scanner_home = skill_root.parent / "scanner-home"
    scanner_tmp = skill_root.parent / "scanner-tmp"
    scanner_home.mkdir(mode=0o700, exist_ok=True)
    scanner_tmp.mkdir(mode=0o700, exist_ok=True)
    env = _safe_process_environment(home=scanner_home)
    env["TMPDIR"] = str(scanner_tmp)
    version = _scanner_version(executable, cwd=skill_root.parent, env=env)
    completed = _run_command(
        (executable, "scan", str(skill_root), "--no-llm", "--format", "json"),
        cwd=skill_root.parent,
        env=env,
        timeout_seconds=timeout_seconds,
    )
    output_size = len(completed.stdout.encode("utf-8")) + len(completed.stderr.encode("utf-8"))
    if output_size > max_output_bytes:
        raise SkillScanRejected("SkillSpector output exceeded the configured size limit.")
    if completed.returncode != 0:
        raise SkillScanRejected(
            "SkillSpector scan failed: "
            + _last_output_line(completed.stderr, completed.stdout)
        )
    try:
        report = json.loads(completed.stdout)
    except json.JSONDecodeError as exc:
        raise SkillScanRejected("SkillSpector did not return a valid JSON report.") from exc
    if not isinstance(report, Mapping):
        raise SkillScanRejected("SkillSpector JSON report must be an object.")
    _validate_scan_completeness(report)
    assessment = report.get("risk_assessment")
    if not isinstance(assessment, Mapping):
        raise SkillScanRejected("SkillSpector report lacks risk_assessment.")
    try:
        score = int(assessment.get("score"))
    except (TypeError, ValueError) as exc:
        raise SkillScanRejected("SkillSpector risk score is invalid.") from exc
    severity = str(assessment.get("severity") or "").upper()
    recommendation = str(assessment.get("recommendation") or "").upper()
    if score < 0 or score > safe_max_score or severity != "LOW" or recommendation != "SAFE":
        raise SkillScanRejected(
            f"SkillSpector rejected the skill: score={score}, severity={severity}, "
            f"recommendation={recommendation}; required score <= {safe_max_score}, LOW, SAFE."
        )
    issues = report.get("issues")
    if not isinstance(issues, list):
        raise SkillScanRejected("SkillSpector issues field is malformed.")
    canonical_report = _canonical_json(report).encode("utf-8")
    return SkillScanResult(
        scanner_name="SkillSpector",
        scanner_version=version,
        mode="static_no_llm",
        score=score,
        severity=severity,
        recommendation=recommendation,
        issue_count=len(issues),
        report_sha256=_sha256_bytes(canonical_report),
        report=report,
    )


def verify_sha256(value: str, *, field_name: str) -> str:
    normalized = str(value or "").strip().lower()
    if not _SHA256_PATTERN.fullmatch(normalized):
        raise ValueError(f"{field_name} must be a lowercase SHA-256 digest.")
    return normalized


__all__ = [
    "DEFAULT_SAFE_MAX_SCORE",
    "MAX_REPOSITORY_BYTES",
    "MAX_SCANNER_OUTPUT_BYTES",
    "MAX_SINGLE_FILE_BYTES",
    "MAX_SKILL_BYTES",
    "MAX_SKILL_FILES",
    "SCANNER_TIMEOUT_SECONDS",
    "SkillCandidate",
    "SkillRepositoryError",
    "SkillScanRejected",
    "SkillScanResult",
    "SkillSelectionRequired",
    "SkillTreeEntry",
    "SkillTreeManifest",
    "build_skill_tree_manifest",
    "clone_public_github_repository",
    "copy_skill_to_quarantine",
    "discover_skill_candidates",
    "scan_skill_with_skillspector",
    "select_skill_candidate",
    "validate_github_repository_url",
    "verify_sha256",
]


def read_skill_name(skill_root: Path) -> str:
    """The validated frontmatter name of the SKILL.md directly inside ``skill_root``."""

    definitions = [
        item
        for item in skill_root.iterdir()
        if item.is_file() and not item.is_symlink() and item.name.casefold() == "skill.md"
    ]
    if len(definitions) != 1:
        raise SkillRepositoryError(
            "A skill draft folder must contain exactly one SKILL.md at its top level."
        )
    return _frontmatter_name(definitions[0])
