"""Reviewed, content-bound agent installation of skills from public GitHub repos."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
import json
import logging
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import stat
from typing import Any, Literal, Mapping, Optional

from pydantic import BaseModel, ConfigDict, field_validator, model_validator
import yaml

from access_control.changes import ChangeApproval
from custom_agents.access_runtime import ActingAgent, acting_agent, runtime_access
from custom_agents.governance.authorized_operations import (
    FilePlanStore,
    GovernanceAuditEvent,
    atomic_write_bytes,
    canonical_json_sha256,
    model_digest,
)
from custom_agents.governance.runtime_adapters import LoggingGovernanceAuditSink
from custom_messages.contracts.access_v2 import ChangeApprovalMode, ChangeProposalV1, summary_line
from custom_messages.contracts.runtime_v1 import (
    AUTHORITY_MODE_AUTHORIZATION_DISABLED,
    AUTHORITY_MODE_PERSON_APPROVAL,
    TEAM_ARCHITECT_AGENT_TYPE,
)
from custom_messages.contracts.common_v1 import (
    utc_now,
)
from custom_agents.runtime_paths import resolve_runtime_instance_root
from custom_agents.skill_repository import (
    DEFAULT_SAFE_MAX_SCORE,
    MAX_REPOSITORY_BYTES,
    SCANNER_TIMEOUT_SECONDS,
    SkillCandidate,
    SkillSelectionRequired,
    build_skill_tree_manifest,
    clone_public_github_repository,
    copy_skill_to_quarantine,
    discover_skill_candidates,
    scan_skill_with_skillspector,
    select_skill_candidate,
    read_skill_name,
    validate_github_repository_url,
    verify_sha256,
)
from id_generation import new_id, new_id_suffix
from runtime_settings.instance_layout import InstanceLayout


logger = logging.getLogger(__name__)

# Version 2: applied through apply_change (access_control.changes).
SKILL_INSTALLATION_PLAN_SCHEMA_VERSION = 2
# Long enough for a person to approve later; the artifact and destination
# checks still refuse a plan that became stale.
SKILL_INSTALLATION_PLAN_TTL_SECONDS = 24 * 60 * 60
TEAM_SKILL_INSTALL_CHANGE_KIND = "team_skill_install"
DEFAULT_SKILLSPECTOR_EXECUTABLE = "skillspector"
_CHANGE_ID_PATTERN = re.compile(r"skill_install_[0-9a-f]{12}\Z")
_COMMIT_PATTERN = re.compile(r"[0-9a-f]{40}\Z")


@dataclass(frozen=True)
class SkillInstallationSettings:
    enabled: bool = True
    scanner_executable: str = DEFAULT_SKILLSPECTOR_EXECUTABLE
    scanner_timeout_seconds: int = SCANNER_TIMEOUT_SECONDS
    safe_max_score: int = DEFAULT_SAFE_MAX_SCORE
    plan_ttl_seconds: int = SKILL_INSTALLATION_PLAN_TTL_SECONDS
    max_repository_bytes: int = MAX_REPOSITORY_BYTES

    @classmethod
    def from_mapping(
        cls, value: Optional[Mapping[str, Any]]
    ) -> "SkillInstallationSettings":
        payload = dict(value or {})
        enabled = payload.get("enabled", True)
        if not isinstance(enabled, bool):
            normalized = str(enabled).strip().lower()
            if normalized not in {"true", "false", "1", "0", "yes", "no", "on", "off"}:
                raise ValueError("skill_installation.enabled must be a boolean")
            enabled = normalized in {"true", "1", "yes", "on"}
        scanner = str(
            payload.get("scanner_executable", DEFAULT_SKILLSPECTOR_EXECUTABLE) or ""
        ).strip()
        if not scanner:
            raise ValueError("skill_installation.scanner_executable must not be empty")
        timeout = int(payload.get("scanner_timeout_seconds", SCANNER_TIMEOUT_SECONDS))
        score = int(payload.get("safe_max_score", DEFAULT_SAFE_MAX_SCORE))
        ttl = int(payload.get("plan_ttl_seconds", SKILL_INSTALLATION_PLAN_TTL_SECONDS))
        repository_bytes = int(
            payload.get("max_repository_bytes", MAX_REPOSITORY_BYTES)
        )
        if timeout < 10 or timeout > 900:
            raise ValueError(
                "skill_installation.scanner_timeout_seconds must be between 10 and 900"
            )
        if score < 0 or score > 20:
            raise ValueError(
                "skill_installation.safe_max_score must be between 0 and 20"
            )
        if ttl < 60 or ttl > 86400:
            raise ValueError(
                "skill_installation.plan_ttl_seconds must be between 60 and 86400"
            )
        if repository_bytes < 1024 * 1024 or repository_bytes > 1024 * 1024 * 1024:
            raise ValueError(
                "skill_installation.max_repository_bytes must be between 1 MiB and 1 GiB"
            )
        return cls(
            enabled=enabled,
            scanner_executable=scanner,
            scanner_timeout_seconds=timeout,
            safe_max_score=score,
            plan_ttl_seconds=ttl,
            max_repository_bytes=repository_bytes,
        )


@dataclass(frozen=True)
class SkillInstallationPaths:
    instance_root: Path
    root: Path
    quarantine: Path
    plans: Path
    reports: Path
    receipts: Path
    installed: Path
    attestations: Path
    uninstallation_plans: Path
    uninstallation_receipts: Path
    removed: Path

    def ensure(self) -> None:
        for path in (
            self.root,
            self.quarantine,
            self.plans,
            self.reports,
            self.receipts,
            self.installed,
            self.attestations,
            self.uninstallation_plans,
            self.uninstallation_receipts,
            self.removed,
        ):
            path.mkdir(parents=True, mode=0o700, exist_ok=True)


class SkillInstallScanEvidence(BaseModel):
    model_config = ConfigDict(extra="forbid")

    scanner_name: str
    scanner_version: str
    mode: Literal["static_no_llm"]
    score: int
    severity: Literal["LOW"]
    recommendation: Literal["SAFE"]
    issue_count: int
    report_sha256: str
    report_path: str

    @field_validator("report_sha256")
    @classmethod
    def _validate_report_digest(cls, value: str) -> str:
        return verify_sha256(value, field_name="report_sha256")


class SkillInstallPlan(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: int = SKILL_INSTALLATION_PLAN_SCHEMA_VERSION
    change_id: str
    plan_digest: str = ""
    created_at_utc: datetime
    expires_at_utc: datetime
    actor_principal_id: str
    actor_named_agent_type: str
    team_id: str
    owner_canonical_user_id: str
    source_task_id: str
    authority_mode: Literal["person_approval", "authorization_disabled_agreement"]
    # "github": a public repository at an exact commit. "team_draft": a folder an
    # agent prepared in its task folders; creating or replacing a team skill
    # goes through this same scan and review (team skills live where agents
    # cannot write them).
    source_kind: Literal["github", "team_draft"] = "github"
    repository_url: Optional[str] = None
    commit_sha: Optional[str] = None
    draft_source_path: Optional[str] = None
    # Set when the plan replaces an installed team skill of the same name; the
    # installed tree must still match this digest at apply time.
    replaces_tree_sha256: Optional[str] = None
    skill_name: str
    skill_relative_path: str
    artifact_path: str
    artifact_tree_sha256: str
    artifact_file_count: int
    artifact_total_bytes: int
    destination_path: str
    scan: SkillInstallScanEvidence

    @field_validator("schema_version")
    @classmethod
    def _validate_schema_version(cls, value: int) -> int:
        if int(value) != SKILL_INSTALLATION_PLAN_SCHEMA_VERSION:
            raise ValueError(
                "Only skill installation plan schema version 1 is supported."
            )
        return int(value)

    @field_validator("change_id")
    @classmethod
    def _validate_change_id(cls, value: str) -> str:
        normalized = str(value or "").strip()
        if not _CHANGE_ID_PATTERN.fullmatch(normalized):
            raise ValueError("Invalid skill installation change id.")
        return normalized

    @field_validator("plan_digest", "artifact_tree_sha256")
    @classmethod
    def _validate_digests(cls, value: str, info: Any) -> str:
        if info.field_name == "plan_digest" and value == "":
            return value
        return verify_sha256(value, field_name=info.field_name)

    @field_validator("commit_sha")
    @classmethod
    def _validate_commit(cls, value: Optional[str]) -> Optional[str]:
        if value is None:
            return None
        normalized = str(value or "").strip().lower()
        if not _COMMIT_PATTERN.fullmatch(normalized):
            raise ValueError("commit_sha must be a 40-character lowercase Git commit")
        return normalized

    @model_validator(mode="after")
    def _validate_source(self) -> "SkillInstallPlan":
        if self.source_kind == "github":
            if not self.repository_url or not self.commit_sha:
                raise ValueError("A GitHub skill plan needs repository_url and commit_sha.")
            if self.draft_source_path or self.replaces_tree_sha256:
                raise ValueError("A GitHub skill plan cannot carry draft fields.")
        elif not self.draft_source_path or self.repository_url or self.commit_sha:
            raise ValueError("A team draft plan needs draft_source_path and no repository.")
        return self

    @property
    def replaces_existing(self) -> bool:
        return self.replaces_tree_sha256 is not None

    @property
    def source_description(self) -> str:
        if self.source_kind == "team_draft":
            return f"team draft {self.draft_source_path}"
        return f"{self.repository_url}@{self.commit_sha}:{self.skill_relative_path}"


def resolve_skill_installation_paths(agent: Any) -> SkillInstallationPaths:
    instance_root = resolve_runtime_instance_root(
        instance_root=getattr(agent, "instance_root", None),
        bootstrap_config=dict(getattr(agent, "bootstrap_config", {}) or {}),
        instance_config_path=getattr(agent, "instance_config_path", None),
        workspace_dir=getattr(agent, "workspace_dir", None),
    )
    if not instance_root:
        raise RuntimeError(
            "Skill installation requires a resolvable runtime instance root."
        )
    root_path = Path(instance_root).expanduser().resolve()
    runtime_root = InstanceLayout.at(root_path).skill_installation_dir
    workspace_raw = str(getattr(agent, "workspace_dir", "") or "").strip()
    if workspace_raw:
        workspace = Path(workspace_raw).expanduser().resolve()
        if runtime_root == workspace or workspace in runtime_root.parents:
            raise RuntimeError(
                "Managed skill installation storage must live outside the agent workspace."
            )
    return SkillInstallationPaths(
        instance_root=root_path,
        root=runtime_root,
        quarantine=runtime_root / "quarantine",
        plans=runtime_root / "plans",
        reports=runtime_root / "reports",
        receipts=runtime_root / "receipts",
        installed=runtime_root / "installed",
        attestations=runtime_root / "attestations",
        uninstallation_plans=runtime_root / "uninstallation-plans",
        uninstallation_receipts=runtime_root / "uninstallation-receipts",
        removed=runtime_root / "removed",
    )


def managed_skill_directory_for_runtime_config(config: Mapping[str, Any]) -> Path:
    """Resolve/create the shared managed source before constructing AgentSkills."""

    settings = SkillInstallationSettings.from_mapping(
        config.get("skill_installation_config")
    )
    bootstrap = dict(config.get("bootstrap_config", {}) or {})
    instance_root = resolve_runtime_instance_root(
        instance_root=config.get("instance_root"),
        bootstrap_config=bootstrap,
        instance_config_path=config.get("instance_config_path"),
        workspace_dir=config.get("workspace_dir"),
    )
    if not instance_root:
        raise RuntimeError(
            "Skill installation requires a resolvable runtime instance root."
        )
    installed = InstanceLayout.at(instance_root).installed_skills_dir
    if settings.enabled:
        installed.mkdir(parents=True, mode=0o700, exist_ok=True)
    return installed


def _candidate_payload(candidate: SkillCandidate) -> dict[str, str]:
    return candidate.as_dict()


def _unmanaged_skill_names(agent: Any, paths: SkillInstallationPaths) -> set[str]:
    """Names of loaded skills that did not come through reviewed installation."""

    managed = {
        item.name.casefold()
        for item in paths.installed.iterdir()
        if item.is_dir() and not item.name.startswith(".")
    } if paths.installed.is_dir() else set()
    managed_names = _skill_names_in([str(paths.installed)])
    return _existing_skill_names(agent) - managed_names - managed


def _existing_skill_names(agent: Any) -> set[str]:
    sources = [
        str(getattr(agent, "skills_dir", "") or ""),
        *(
            str(item)
            for item in (getattr(agent, "additional_skill_directories", []) or [])
        ),
    ]
    try:
        sources.append(str(resolve_skill_installation_paths(agent).installed))
    except RuntimeError:
        # Some isolated callers do not have an instance root. Their configured
        # sources are still checked, while installation itself remains disabled.
        pass
    return _skill_names_in(sources)


def _skill_definitions(raw_source: str) -> list[Path]:
    """The SKILL.md files one source names: the file itself, or every one below a folder."""

    if not raw_source:
        return []
    source = Path(raw_source).expanduser()
    if not source.exists():
        return []
    found = [source] if source.is_file() else source.rglob("SKILL.md")
    definitions = [(item / "SKILL.md") if item.is_dir() else item for item in found]
    return [item for item in definitions if item.is_file()]


def _declared_skill_name(definition: Path) -> str:
    """The ``name`` in a SKILL.md front matter, or its folder's name when it declares none."""

    try:
        text = definition.read_text(encoding="utf-8")
        if not text.startswith("---"):
            return definition.parent.name
        lines = text.splitlines()
        metadata = yaml.safe_load("\n".join(lines[1 : lines.index("---", 1)])) or {}
        return str(metadata.get("name") or definition.parent.name).strip()
    except (OSError, ValueError, yaml.YAMLError, AttributeError) as exc:
        logger.warning(
            "Could not read a skill's front matter; using its folder name. "
            "event=skill_front_matter_unreadable path=%s error=%s",
            definition,
            exc,
        )
        return definition.parent.name


def _skill_names_in(sources: list[str]) -> set[str]:
    """Case-folded names of the skills in these sources, for conflict checks."""

    names = {_declared_skill_name(definition) for raw in sources for definition in _skill_definitions(raw)}
    return {name.casefold() for name in names if name}


class SkillInstallationService:
    """Stage, apply (after approval), and inspect one exact reviewed skill artifact.

    Team skills are team-owned (decision L2): an administrator may let the team
    lead manage them (a grant on ``fn:team_skills``), and otherwise a person
    approves each exact install in the Inbox (``access_control.changes``).
    """

    def __init__(self, agent: Any) -> None:
        self.agent = agent
        self.audit = LoggingGovernanceAuditSink()

    @staticmethod
    def _plan_store(paths: SkillInstallationPaths) -> FilePlanStore[SkillInstallPlan]:
        return FilePlanStore(
            paths.plans,
            SkillInstallPlan,
            change_id_pattern=_CHANGE_ID_PATTERN,
            invalid_change_id_message="Invalid skill installation change id.",
            missing_plan_message="Skill installation plan was not found: {change_id}",
        )

    def stage(
        self,
        *,
        repository_url: str,
        skill_name: str = "",
        skill_path: str = "",
    ) -> dict[str, Any]:
        settings = self._settings()
        self._require_enabled(settings)
        actor, task_id = self._staging_identity()
        canonical_url = validate_github_repository_url(repository_url)
        paths = resolve_skill_installation_paths(self.agent)
        paths.ensure()
        change_id = new_id("skill_install")
        quarantine = paths.quarantine / change_id
        checkout = quarantine / "checkout"
        artifact = quarantine / "artifact"
        quarantine.mkdir(mode=0o700)
        try:
            commit_sha = clone_public_github_repository(
                canonical_url,
                checkout,
                max_repository_bytes=settings.max_repository_bytes,
            )
            candidates = discover_skill_candidates(checkout)
            try:
                selected = select_skill_candidate(
                    candidates,
                    skill_name=skill_name,
                    skill_path=skill_path,
                )
            except SkillSelectionRequired as exc:
                result = {
                    "status": "selection_required",
                    "repository_url": canonical_url,
                    "commit_sha": commit_sha,
                    "candidates": [_candidate_payload(item) for item in exc.candidates],
                    "next_step": (
                        "Call plan_skill_installation again with the exact skill_path. "
                        "The repository will be resolved and scanned afresh."
                    ),
                }
                shutil.rmtree(quarantine, ignore_errors=True)
                return result
            source = checkout / PurePosixPath(selected.relative_path)
            source = source.resolve()
            if checkout.resolve() not in source.parents:
                raise PermissionError(
                    "Selected skill path escaped the repository checkout."
                )
            manifest = copy_skill_to_quarantine(source, artifact)
            if selected.name.casefold() in _existing_skill_names(self.agent):
                raise ValueError(
                    f"A loaded skill already uses semantic name {selected.name!r}; skill names must be unique."
                )
            destination = paths.installed / selected.name
            if destination.exists() or destination.is_symlink():
                raise ValueError(
                    f"Managed skill destination already exists: {destination}"
                )
            scan = scan_skill_with_skillspector(
                artifact,
                scanner_executable=settings.scanner_executable,
                safe_max_score=settings.safe_max_score,
                timeout_seconds=settings.scanner_timeout_seconds,
            )
            report_path = paths.reports / f"{change_id}.json"
            report_bytes = (
                json.dumps(scan.report, indent=2, sort_keys=True) + "\n"
            ).encode("utf-8")
            atomic_write_bytes(report_path, report_bytes, immutable=True)
            public_report_path = self._write_public_report(
                change_id=change_id,
                source=f"{canonical_url}@{commit_sha}:{selected.relative_path}",
                scan_report=dict(scan.report),
            )
            now = utc_now()
            authority_mode = self._authority_mode()
            plan = SkillInstallPlan(
                change_id=change_id,
                created_at_utc=now,
                expires_at_utc=now + timedelta(seconds=settings.plan_ttl_seconds),
                actor_principal_id=actor.agent.agent_id,
                actor_named_agent_type=actor.agent.agent_type,
                team_id=actor.agent.team_id,
                owner_canonical_user_id=actor.person_id or "",
                source_task_id=task_id,
                authority_mode=authority_mode,
                repository_url=canonical_url,
                commit_sha=commit_sha,
                skill_name=selected.name,
                skill_relative_path=selected.relative_path,
                artifact_path=str(artifact),
                artifact_tree_sha256=manifest.tree_sha256,
                artifact_file_count=len(manifest.entries),
                artifact_total_bytes=manifest.total_bytes,
                destination_path=str(destination),
                scan=SkillInstallScanEvidence(
                    **scan.summary(),
                    report_path=str(report_path),
                ),
            )
            shutil.rmtree(checkout, ignore_errors=True)
            return self._save_staged_plan(
                plan, paths, scan=scan, public_report_path=public_report_path
            )
        except SkillSelectionRequired:
            raise
        except Exception:
            shutil.rmtree(quarantine, ignore_errors=True)
            raise
        finally:
            if checkout.exists() and not artifact.exists():
                shutil.rmtree(checkout, ignore_errors=True)

    def stage_draft(
        self,
        *,
        draft_path: str,
        replace_existing: bool = False,
    ) -> dict[str, Any]:
        """Stage a new or changed team skill from a draft folder.

        Team skills load into every agent, so agents cannot write them; a draft
        in the task's own folders goes through the same quarantine, SkillSpector
        scan, one-use approval, and attested install as a GitHub skill.
        Replacing an installed team skill keeps the old version recoverable.
        """

        settings = self._settings()
        self._require_enabled(settings)
        actor, task_id = self._staging_identity()
        from custom_tools import file_access
        from custom_tools.path_policy import readable_skill_dir

        location, entry = file_access.status(draft_path, agent=self.agent)
        source = location.path
        if readable_skill_dir(source, self.agent) is not None:
            # Installed skills are readable, but a proposal must be a copy the
            # agent edited in its own folders, so the diff shows what changed.
            raise ValueError(
                "A skill draft must be in this task's folders. Copy the installed "
                "skill into your folder, edit the copy, then propose it with "
                "replace_existing=true."
            )
        if entry is None or not stat.S_ISDIR(entry.st_mode):
            # A link is reported as a link, never followed (custom_tools.file_access).
            raise ValueError(f"The skill draft must be a folder: {source}")
        paths = resolve_skill_installation_paths(self.agent)
        paths.ensure()
        change_id = new_id("skill_install")
        quarantine = paths.quarantine / change_id
        artifact = quarantine / "artifact"
        quarantine.mkdir(mode=0o700)
        try:
            name = read_skill_name(source)
            manifest = copy_skill_to_quarantine(source, artifact)
            destination = paths.installed / name
            replaced_digest, changes = self._draft_replacement(
                name, paths, destination, manifest, replace_existing=replace_existing
            )
            scan = scan_skill_with_skillspector(
                artifact,
                scanner_executable=settings.scanner_executable,
                safe_max_score=settings.safe_max_score,
                timeout_seconds=settings.scanner_timeout_seconds,
            )
            report_path = paths.reports / f"{change_id}.json"
            atomic_write_bytes(
                report_path,
                (json.dumps(scan.report, indent=2, sort_keys=True) + "\n").encode("utf-8"),
                immutable=True,
            )
            public_report_path = self._write_public_report(
                change_id=change_id,
                source=f"team draft {source}",
                scan_report=dict(scan.report),
            )
            now = utc_now()
            authority_mode = self._authority_mode()
            plan = SkillInstallPlan(
                change_id=change_id,
                created_at_utc=now,
                expires_at_utc=now + timedelta(seconds=settings.plan_ttl_seconds),
                actor_principal_id=actor.agent.agent_id,
                actor_named_agent_type=actor.agent.agent_type,
                team_id=actor.agent.team_id,
                owner_canonical_user_id=actor.person_id or "",
                source_task_id=task_id,
                authority_mode=authority_mode,
                source_kind="team_draft",
                draft_source_path=str(source),
                replaces_tree_sha256=replaced_digest,
                skill_name=name,
                skill_relative_path=".",
                artifact_path=str(artifact),
                artifact_tree_sha256=manifest.tree_sha256,
                artifact_file_count=len(manifest.entries),
                artifact_total_bytes=manifest.total_bytes,
                destination_path=str(destination),
                scan=SkillInstallScanEvidence(**scan.summary(), report_path=str(report_path)),
            )
            result = self._save_staged_plan(
                plan, paths, scan=scan, public_report_path=public_report_path
            )
            return {**result, "changes": changes}
        except Exception:
            shutil.rmtree(quarantine, ignore_errors=True)
            raise

    def _draft_replacement(
        self,
        name: str,
        paths: SkillInstallationPaths,
        destination: Path,
        manifest: Any,
        *,
        replace_existing: bool,
    ) -> tuple[Optional[str], dict[str, Any]]:
        """The installed digest being replaced (None for a new skill) and a diff."""

        if name.casefold() in _unmanaged_skill_names(self.agent, paths):
            raise ValueError(
                f"{name!r} is a built-in or operator-provided skill. Team skill changes can "
                "create new skills or replace skills installed through review; choose another name."
            )
        if not (destination.exists() or destination.is_symlink()):
            if replace_existing:
                raise ValueError(f"There is no installed team skill named {name!r} to replace.")
            return None, {"kind": "new_skill", "files": [e.path for e in manifest.entries]}
        if not replace_existing:
            raise ValueError(
                f"A team skill named {name!r} is already installed. Pass replace_existing=true "
                "to propose replacing it."
            )
        installed_digest = verify_managed_skill_attestation_paths(
            skill_path=destination,
            installed_root=paths.installed,
            attestations_root=paths.attestations,
        )
        before = {e.path: e.sha256 for e in build_skill_tree_manifest(destination).entries}
        after = {e.path: e.sha256 for e in manifest.entries}
        return installed_digest, {
            "kind": "replacement",
            "added": sorted(set(after) - set(before)),
            "removed": sorted(set(before) - set(after)),
            "changed": sorted(p for p in set(before) & set(after) if before[p] != after[p]),
        }

    def _verify_destination_state(
        self, plan: SkillInstallPlan, paths: SkillInstallationPaths, destination: Path
    ) -> None:
        """The destination is free for a new skill, or still the reviewed version."""

        if plan.skill_name.casefold() in _unmanaged_skill_names(self.agent, paths):
            raise PermissionError(
                "A loaded skill now collides with this plan's semantic name."
            )
        exists = destination.exists() or destination.is_symlink()
        if not plan.replaces_existing:
            if exists or plan.skill_name.casefold() in _existing_skill_names(self.agent):
                raise PermissionError("Skill installation destination already exists.")
            return
        if not exists:
            raise PermissionError("The team skill this plan replaces is no longer installed.")
        current = verify_managed_skill_attestation_paths(
            skill_path=destination,
            installed_root=paths.installed,
            attestations_root=paths.attestations,
        )
        if current != plan.replaces_tree_sha256:
            raise PermissionError(
                "The installed team skill changed after this replacement was reviewed."
            )

    def _save_staged_plan(
        self,
        plan: SkillInstallPlan,
        paths: SkillInstallationPaths,
        *,
        scan: Any,
        public_report_path: Optional[Path],
    ) -> dict[str, Any]:
        plan = plan.model_copy(update={"plan_digest": model_digest(plan)})
        plan_path = self._plan_store(paths).save(plan)
        self.audit.record(
            GovernanceAuditEvent(
                operation="skill_installation",
                phase="staged",
                change_id=plan.change_id,
                plan_digest=plan.plan_digest,
                task_id=plan.source_task_id,
                authority_mode=plan.authority_mode,
            )
        )
        return {
            **plan.model_dump(mode="json"),
            "status": "safe_pending_approval",
            "plan_path": str(plan_path),
            "scan_report_artifact": str(public_report_path) if public_report_path else None,
            "scan_issues": list(scan.report.get("issues") or []),
            "next_step": (
                "Present the scan result and findings to the person, then call apply_change(change_id, reason). "
                "If you may manage team skills it installs at once and administrators are told; otherwise a "
                "person approves the exact install in the Inbox."
                if plan.authority_mode == AUTHORITY_MODE_PERSON_APPROVAL
                else "Present the scan result, get the person's agreement in the conversation, then call "
                "apply_change(change_id, reason, confirmed=true)."
            ),
        }

    def _staging_identity(self) -> tuple[ActingAgent, str]:
        actor = acting_agent(self.agent)
        if actor.agent.agent_type != TEAM_ARCHITECT_AGENT_TYPE:
            raise PermissionError("Only TeamArchitect may install or change team skills.")
        if not actor.person_id:
            raise PermissionError("Team skill changes must be staged while TeamArchitect works for a person.")
        if not actor.task_id:
            raise PermissionError("Team skill changes must be staged from an active task.")
        return actor, actor.task_id

    def load_plan(self, change_id: str) -> SkillInstallPlan:
        paths = resolve_skill_installation_paths(self.agent)
        plan = self._plan_store(paths).load(str(change_id or "").strip())
        if plan.authority_mode != self._authority_mode():
            raise PermissionError("Authorization was turned on or off after planning; stage and scan a fresh plan.")
        self._verify_plan(plan, actor=acting_agent(self.agent))
        return plan

    def _verify_current(self, plan: SkillInstallPlan, paths: SkillInstallationPaths) -> Path:
        """The plan can still apply: not applied yet, the scanned files and the destination unchanged.

        Runs before a person is asked (``describe``) and again when applying,
        so nobody approves a change that can only fail. Returns the destination.
        """

        if (paths.receipts / f"{plan.change_id}.json").exists():
            raise FileExistsError(f"Skill installation plan {plan.change_id} was already applied.")
        self._verify_artifact_and_report(plan)
        destination = Path(plan.destination_path).resolve()
        if destination != (paths.installed / plan.skill_name).resolve():
            raise PermissionError("Skill installation destination no longer matches the runtime-owned path.")
        self._verify_destination_state(plan, paths, destination)
        return destination

    def describe(self, change_id: str) -> ChangeProposalV1:
        plan = self.load_plan(change_id)
        self._verify_current(plan, resolve_skill_installation_paths(self.agent))
        verb = "Replace" if plan.replaces_existing else "Install"
        issues = plan.scan.model_dump(mode="json")
        return ChangeProposalV1(
            kind=TEAM_SKILL_INSTALL_CHANGE_KIND,
            change_id=plan.change_id,
            plan_digest=plan.plan_digest,
            title=f"{verb} the team skill {plan.skill_name}",
            summary=summary_line(
                f"{verb} the team skill {plan.skill_name} (security scan: {issues.get('verdict') or 'passed'}). "
                "Takes effect now."
            ),
            effect=(
                f"{verb}s the team skill {plan.skill_name} from {plan.source_description}.",
                f"{plan.artifact_file_count} files, {plan.artifact_total_bytes} bytes; security scan: "
                f"{issues.get('verdict') or 'passed'} (score {issues.get('score', 'n/a')}).",
                "Skills give agents instructions, not access; running their scripts still needs code access.",
            ),
        )

    def apply(self, change_id: str, approval: ChangeApproval) -> dict[str, Any]:
        """Install an approved plan (only the change mechanism calls this)."""

        settings = self._settings()
        self._require_enabled(settings)
        paths = resolve_skill_installation_paths(self.agent)
        paths.ensure()
        normalized = str(change_id or "").strip()
        receipt_path = paths.receipts / f"{normalized}.json"
        if receipt_path.exists():
            receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
            return {
                "error": "Skill installation plan was already applied.",
                "reason_code": "skill_installation_replay",
                "change_id": normalized,
                "receipt": receipt,
            }
        plan = self.load_plan(normalized)
        agreement = approval.mode is ChangeApprovalMode.AGREEMENT
        if agreement != (plan.authority_mode == AUTHORITY_MODE_AUTHORIZATION_DISABLED):
            raise PermissionError("This plan needs approval or held access, not agreement (or the reverse).")
        destination = self._verify_current(plan, paths)
        authority = {"mode": plan.authority_mode, "approval": approval.record()}

        installed_manifest = self._install_atomically(plan, paths)
        applied_at = utc_now()
        receipt = {
            "schema_version": SKILL_INSTALLATION_PLAN_SCHEMA_VERSION,
            "receipt_id": new_id("skill_inst_rcpt"),
            "change_id": plan.change_id,
            "plan_digest": plan.plan_digest,
            "applied_at_utc": applied_at.isoformat(),
            "actor_principal_id": plan.actor_principal_id,
            "team_id": plan.team_id,
            "skill_name": plan.skill_name,
            "source_kind": plan.source_kind,
            "repository_url": plan.repository_url,
            "commit_sha": plan.commit_sha,
            "draft_source_path": plan.draft_source_path,
            "replaced_tree_sha256": plan.replaces_tree_sha256,
            "skill_relative_path": plan.skill_relative_path,
            "artifact_tree_sha256": plan.artifact_tree_sha256,
            "installed_tree_sha256": installed_manifest.tree_sha256,
            "installed_path": str(destination),
            "scan": plan.scan.model_dump(mode="json"),
            "authority": authority,
        }
        atomic_write_bytes(
            receipt_path,
            (json.dumps(receipt, indent=2, sort_keys=True) + "\n").encode("utf-8"),
            immutable=True,
        )
        self.audit.record(
            GovernanceAuditEvent(
                operation="skill_installation",
                phase="applied",
                change_id=plan.change_id,
                plan_digest=plan.plan_digest,
                task_id=plan.source_task_id,
                authority_mode=plan.authority_mode,
            )
        )
        refresh = getattr(self.agent, "_refresh_skills_plugins", None)
        if callable(refresh):
            refresh(force=True)
        shutil.rmtree(Path(plan.artifact_path).parent, ignore_errors=True)
        return {
            "status": "installed",
            **receipt,
            "receipt_path": str(receipt_path),
            "next_step": (
                f"Activate the installed skill with the skills tool using skill_name="
                f"{plan.skill_name!r}. No runtime restart is required. Tools, shell commands "
                "or MCP operations the skill asks for still need their own access."
            ),
        }

    def status(self, change_id: str) -> dict[str, Any]:
        paths = resolve_skill_installation_paths(self.agent)
        normalized = str(change_id or "").strip()
        receipt_path = paths.receipts / f"{normalized}.json"
        plan_path = paths.plans / f"{normalized}.json"
        if receipt_path.is_file():
            receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
            return {"status": "installed", "change_id": normalized, "receipt": receipt}
        if plan_path.is_file():
            plan = self._plan_store(paths).load(normalized)
            return {
                "status": "pending" if plan.expires_at_utc > utc_now() else "expired",
                "plan": plan.model_dump(mode="json"),
            }
        return {"status": "missing", "change_id": normalized}

    def _install_atomically(
        self, plan: SkillInstallPlan, paths: SkillInstallationPaths
    ):
        artifact = Path(plan.artifact_path).resolve()
        destination = Path(plan.destination_path).resolve()
        incoming = paths.installed / f".incoming-{plan.change_id}-{new_id_suffix()}"
        lock_path = paths.root / ".skill-lifecycle.lock"
        lock_handle = lock_path.open("a+b")
        try:
            try:
                import fcntl

                fcntl.flock(lock_handle.fileno(), fcntl.LOCK_EX)
            except (
                ImportError
            ):  # pragma: no cover - Windows fallback uses process ownership.
                pass
            self._verify_destination_state(plan, paths, destination)
            shutil.copytree(artifact, incoming, symlinks=False)
            copied = build_skill_tree_manifest(incoming)
            if copied.tree_sha256 != plan.artifact_tree_sha256:
                raise PermissionError(
                    "Skill artifact changed during final installation copy."
                )
            retired = None
            if plan.replaces_existing:
                # Keep the replaced version recoverable, as uninstallation does.
                retired = paths.removed / f"{plan.skill_name}.{plan.change_id}"
                os.rename(destination, retired)
            try:
                os.rename(incoming, destination)
            except Exception:
                if retired is not None:
                    os.rename(retired, destination)
                raise
            attestation_path = paths.attestations / f"{plan.skill_name}.json"
            attestation = {
                "schema_version": SKILL_INSTALLATION_PLAN_SCHEMA_VERSION,
                "change_id": plan.change_id,
                "plan_digest": plan.plan_digest,
                "skill_name": plan.skill_name,
                "installed_path": str(destination),
                "tree_sha256": copied.tree_sha256,
                "source_kind": plan.source_kind,
                "repository_url": plan.repository_url,
                "commit_sha": plan.commit_sha,
                "draft_source_path": plan.draft_source_path,
                "skill_relative_path": plan.skill_relative_path,
                "scan_report_sha256": plan.scan.report_sha256,
            }
            try:
                atomic_write_bytes(
                    attestation_path,
                    (json.dumps(attestation, indent=2, sort_keys=True) + "\n").encode(
                        "utf-8"
                    ),
                    immutable=True,
                )
            except Exception:
                shutil.rmtree(destination, ignore_errors=True)
                if retired is not None:
                    os.rename(retired, destination)
                raise
            return copied
        finally:
            shutil.rmtree(incoming, ignore_errors=True)
            try:
                import fcntl

                fcntl.flock(lock_handle.fileno(), fcntl.LOCK_UN)
            except ImportError:  # pragma: no cover
                pass
            lock_handle.close()

    def _verify_plan(self, plan: SkillInstallPlan, *, actor: ActingAgent) -> None:
        if plan.plan_digest != model_digest(plan):
            raise PermissionError("Skill installation plan digest mismatch.")
        if plan.expires_at_utc <= utc_now():
            raise PermissionError("Skill installation plan has expired. Stage and scan a fresh plan.")
        if (
            plan.actor_principal_id != actor.agent.agent_id
            or plan.actor_named_agent_type != actor.agent.agent_type
            or plan.team_id != actor.agent.team_id
            or plan.owner_canonical_user_id != (actor.person_id or "")
        ):
            raise PermissionError("Skill installation plan belongs to another agent, team or person.")
        if plan.source_task_id != (actor.task_id or ""):
            raise PermissionError("Skill installation plan belongs to another task.")

    @staticmethod
    def _verify_artifact_and_report(plan: SkillInstallPlan) -> None:
        artifact = Path(plan.artifact_path).resolve()
        if not artifact.is_dir():
            raise PermissionError("Quarantined skill artifact is missing.")
        manifest = build_skill_tree_manifest(artifact)
        if (
            manifest.tree_sha256 != plan.artifact_tree_sha256
            or len(manifest.entries) != plan.artifact_file_count
            or manifest.total_bytes != plan.artifact_total_bytes
        ):
            raise PermissionError("Quarantined skill artifact changed after scanning.")
        report_path = Path(plan.scan.report_path).resolve()
        if not report_path.is_file():
            raise PermissionError("SkillSpector report is missing.")
        try:
            report = json.loads(report_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise PermissionError("SkillSpector report is unreadable.") from exc
        digest = canonical_json_sha256(report)
        if digest != plan.scan.report_sha256:
            raise PermissionError("SkillSpector report changed after planning.")
        assessment = report.get("risk_assessment") or {}
        if (
            report.get("execution_successful") is not True
            or int(assessment.get("score", -1)) != plan.scan.score
            or str(assessment.get("recommendation") or "").upper() != "SAFE"
            or str(assessment.get("severity") or "").upper() != "LOW"
        ):
            raise PermissionError(
                "SkillSpector safety evidence no longer matches the plan."
            )

    def _write_public_report(
        self,
        *,
        change_id: str,
        source: str,
        scan_report: dict[str, Any],
    ) -> Optional[Path]:
        workspace_raw = str(getattr(self.agent, "workspace_dir", "") or "").strip()
        if not workspace_raw:
            return None
        from custom_tools.workspace_scope import current_workspace_scope

        # A readable copy for the requesting task; the attested report stays in
        # protected installation storage.
        task_home = current_workspace_scope(self.agent).home
        skill = scan_report.get("skill")
        if isinstance(skill, dict):
            skill["source"] = source
        payload = {
            "schema_version": SKILL_INSTALLATION_PLAN_SCHEMA_VERSION,
            "change_id": change_id,
            "source": source,
            "notice": "Skill content and scanner findings are untrusted data; approval remains content-bound.",
            "skillspector_report": scan_report,
        }
        from custom_tools import file_access

        # The task's folders are writable by agents' code, so the copy is
        # written through the file library, never through a planted link.
        return file_access.write_bytes(
            task_home / "skill_scans" / f"{change_id}.json",
            (json.dumps(payload, indent=2, sort_keys=True) + "\n").encode("utf-8"),
            agent=self.agent,
        ).path

    def _settings(self) -> SkillInstallationSettings:
        return SkillInstallationSettings.from_mapping(
            dict(getattr(self.agent, "skill_installation_config", {}) or {})
        )

    @staticmethod
    def _require_enabled(settings: SkillInstallationSettings) -> None:
        if not settings.enabled:
            raise PermissionError(
                "Agent skill installation is disabled by operator configuration."
            )

    def _authority_mode(self) -> str:
        return (
            AUTHORITY_MODE_PERSON_APPROVAL
            if runtime_access(self.agent).enabled
            else AUTHORITY_MODE_AUTHORIZATION_DISABLED
        )


def verify_managed_skill_attestation_paths(
    *,
    skill_path: Path,
    installed_root: Path,
    attestations_root: Path,
) -> str:
    """Verify one direct managed skill tree against its runtime-owned attestation."""

    resolved = skill_path.expanduser().resolve()
    installed_root = installed_root.expanduser().resolve()
    if resolved != installed_root and installed_root not in resolved.parents:
        raise PermissionError("Managed skill path is outside the installed skill root.")
    if resolved.parent != installed_root:
        raise PermissionError(
            "Managed skill path is not a direct installed skill root."
        )
    attestation_path = (
        attestations_root.expanduser().resolve() / f"{resolved.name}.json"
    )
    if not attestation_path.is_file():
        raise PermissionError("Managed skill attestation is missing.")
    try:
        attestation = json.loads(attestation_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise PermissionError("Managed skill attestation is unreadable.") from exc
    if (
        attestation.get("skill_name") != resolved.name
        or Path(str(attestation.get("installed_path") or "")).resolve() != resolved
    ):
        raise PermissionError("Managed skill attestation targets another skill.")
    expected = verify_sha256(attestation.get("tree_sha256"), field_name="tree_sha256")
    actual = build_skill_tree_manifest(resolved).tree_sha256
    if actual != expected:
        raise PermissionError(
            "Managed skill contents changed after their reviewed installation."
        )
    return actual


def verify_managed_skill_attestation(agent: Any, skill_path: Path) -> Optional[str]:
    """Return the attested tree digest for managed skills, or ``None`` otherwise."""

    try:
        paths = resolve_skill_installation_paths(agent)
    except RuntimeError:
        return None
    resolved = skill_path.expanduser().resolve()
    installed_root = paths.installed.resolve()
    if resolved != installed_root and installed_root not in resolved.parents:
        return None
    return verify_managed_skill_attestation_paths(
        skill_path=resolved,
        installed_root=installed_root,
        attestations_root=paths.attestations,
    )


__all__ = [
    "DEFAULT_SKILLSPECTOR_EXECUTABLE",
    "TEAM_SKILL_INSTALL_CHANGE_KIND",
    "SKILL_INSTALLATION_PLAN_SCHEMA_VERSION",
    "SkillInstallPlan",
    "SkillInstallScanEvidence",
    "SkillInstallationPaths",
    "SkillInstallationService",
    "SkillInstallationSettings",
    "managed_skill_directory_for_runtime_config",
    "resolve_skill_installation_paths",
    "verify_managed_skill_attestation",
    "verify_managed_skill_attestation_paths",
]
