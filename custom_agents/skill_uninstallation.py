"""Content-bound, recoverable removal of reviewed managed skill installations."""

from __future__ import annotations

from datetime import datetime, timedelta
import json
import os
from pathlib import Path
import re
import shutil
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, field_validator

from access_control.changes import ChangeApproval
from custom_agents.access_runtime import ActingAgent, acting_agent, runtime_access
from custom_agents.governance.authorized_operations import (
    FilePlanStore,
    GovernanceAuditEvent,
    atomic_write_bytes,
    model_digest,
    sha256_bytes,
)
from custom_agents.governance.runtime_adapters import LoggingGovernanceAuditSink
from custom_messages.contracts.access_v2 import ChangeApprovalMode, ChangeProposalV1, summary_line
from custom_messages.contracts.common_v1 import utc_now
from custom_messages.contracts.runtime_v1 import (
    AUTHORITY_MODE_AUTHORIZATION_DISABLED,
    AUTHORITY_MODE_PERSON_APPROVAL,
    TEAM_ARCHITECT_AGENT_TYPE,
)
from custom_agents.skill_installation import (
    SkillInstallationPaths,
    SkillInstallationSettings,
    resolve_skill_installation_paths,
    verify_managed_skill_attestation_paths,
)
from custom_agents.skill_repository import verify_sha256
from id_generation import new_id


# Version 2: applied through apply_change (access_control.changes).
SKILL_UNINSTALLATION_PLAN_SCHEMA_VERSION = 2
TEAM_SKILL_UNINSTALL_CHANGE_KIND = "team_skill_uninstall"
_CHANGE_ID_PATTERN = re.compile(r"skill_uninstall_[0-9a-f]{12}\Z")
_SKILL_NAME_PATTERN = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,159}\Z")


class SkillUninstallPlan(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: int = SKILL_UNINSTALLATION_PLAN_SCHEMA_VERSION
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
    skill_name: str
    installed_path: str
    installed_tree_sha256: str
    attestation_path: str
    attestation_sha256: str
    installation_change_id: str
    installation_plan_digest: str
    installation_receipt_path: str
    installation_receipt_sha256: str

    @field_validator("schema_version")
    @classmethod
    def _validate_schema_version(cls, value: int) -> int:
        if int(value) != SKILL_UNINSTALLATION_PLAN_SCHEMA_VERSION:
            raise ValueError(
                f"only skill uninstallation plan schema version {SKILL_UNINSTALLATION_PLAN_SCHEMA_VERSION} is supported"
            )
        return int(value)

    @field_validator("change_id")
    @classmethod
    def _validate_change_id(cls, value: str) -> str:
        normalized = str(value or "").strip()
        if not _CHANGE_ID_PATTERN.fullmatch(normalized):
            raise ValueError("invalid skill uninstallation change id")
        return normalized

    @field_validator("skill_name")
    @classmethod
    def _validate_skill_name(cls, value: str) -> str:
        normalized = str(value or "").strip()
        if not _SKILL_NAME_PATTERN.fullmatch(normalized):
            raise ValueError("skill_name must be one direct managed skill name")
        return normalized

    @field_validator(
        "plan_digest",
        "installed_tree_sha256",
        "attestation_sha256",
        "installation_plan_digest",
        "installation_receipt_sha256",
    )
    @classmethod
    def _validate_digest(cls, value: str, info: Any) -> str:
        if info.field_name == "plan_digest" and value == "":
            return value
        return verify_sha256(value, field_name=info.field_name)


class SkillUninstallationService:
    """Stage and recoverably remove one exact managed skill tree after approval.

    Applied through ``apply_change`` like installation: held access on
    ``fn:team_skills``, a person's approval, or agreement with authorization off.
    """

    def __init__(self, agent: Any) -> None:
        self.agent = agent
        self.audit = LoggingGovernanceAuditSink()

    @staticmethod
    def _plan_store(paths: SkillInstallationPaths) -> FilePlanStore[SkillUninstallPlan]:
        return FilePlanStore(
            paths.uninstallation_plans,
            SkillUninstallPlan,
            change_id_pattern=_CHANGE_ID_PATTERN,
            invalid_change_id_message="invalid skill uninstallation change id",
            missing_plan_message="skill uninstallation plan was not found: {change_id}",
        )

    def stage(self, *, skill_name: str) -> dict[str, Any]:
        settings = self._settings()
        self._require_enabled(settings)
        actor = acting_agent(self.agent)
        if actor.agent.agent_type != TEAM_ARCHITECT_AGENT_TYPE:
            raise PermissionError("only TeamArchitect may remove reviewed managed skills")
        if not actor.person_id:
            raise PermissionError("skill removal must be staged while TeamArchitect works for a person")
        task_id = actor.task_id
        if not task_id:
            raise PermissionError("skill removal requires an active task")
        name = str(skill_name or "").strip()
        if not _SKILL_NAME_PATTERN.fullmatch(name):
            raise ValueError("skill_name must be one direct managed skill name")
        paths = resolve_skill_installation_paths(self.agent)
        paths.ensure()
        installed = (paths.installed / name).resolve()
        if (
            installed.parent != paths.installed.resolve()
            or not installed.is_dir()
            or installed.is_symlink()
        ):
            raise FileNotFoundError(
                f"managed installed skill was not found: {name}; workspace and external skills are not removable here"
            )
        tree_digest = verify_managed_skill_attestation_paths(
            skill_path=installed,
            installed_root=paths.installed,
            attestations_root=paths.attestations,
        )
        attestation_path = (paths.attestations / f"{name}.json").resolve()
        attestation_bytes = attestation_path.read_bytes()
        attestation = json.loads(attestation_bytes)
        installation_change_id = str(attestation.get("change_id") or "").strip()
        installation_plan_digest = verify_sha256(
            attestation.get("plan_digest"), field_name="installation_plan_digest"
        )
        installation_receipt_path = (
            paths.receipts / f"{installation_change_id}.json"
        ).resolve()
        if not installation_receipt_path.is_file():
            raise PermissionError("managed skill installation receipt is missing")
        receipt_bytes = installation_receipt_path.read_bytes()
        receipt = json.loads(receipt_bytes)
        if (
            receipt.get("change_id") != installation_change_id
            or receipt.get("plan_digest") != installation_plan_digest
            or receipt.get("skill_name") != name
            or receipt.get("installed_tree_sha256") != tree_digest
        ):
            raise PermissionError(
                "managed skill installation receipt does not match its attestation"
            )
        now = utc_now()
        plan = SkillUninstallPlan(
            change_id=new_id("skill_uninstall"),
            created_at_utc=now,
            expires_at_utc=now + timedelta(seconds=settings.plan_ttl_seconds),
            actor_principal_id=actor.agent.agent_id,
            actor_named_agent_type=actor.agent.agent_type,
            team_id=actor.agent.team_id,
            owner_canonical_user_id=actor.person_id,
            source_task_id=task_id,
            authority_mode=self._authority_mode(),
            skill_name=name,
            installed_path=str(installed),
            installed_tree_sha256=tree_digest,
            attestation_path=str(attestation_path),
            attestation_sha256=sha256_bytes(attestation_bytes),
            installation_change_id=installation_change_id,
            installation_plan_digest=installation_plan_digest,
            installation_receipt_path=str(installation_receipt_path),
            installation_receipt_sha256=sha256_bytes(receipt_bytes),
        )
        plan = plan.model_copy(update={"plan_digest": model_digest(plan)})
        self._plan_store(paths).save(plan)
        self.audit.record(
            GovernanceAuditEvent(
                operation="skill_uninstallation",
                phase="staged",
                change_id=plan.change_id,
                plan_digest=plan.plan_digest,
                task_id=plan.source_task_id,
                authority_mode=plan.authority_mode,
            )
        )
        return {
            "status": "pending_approval",
            **plan.model_dump(mode="json"),
            "current_turn_note": (
                "Removal affects future skill discovery immediately. Instructions already loaded into an active model turn cannot be recalled."
            ),
            "recoverable": True,
            "next_step": (
                "Call apply_change(change_id, reason); it removes the skill at once if you may manage "
                "team skills, and otherwise a person approves it in the Inbox."
                if plan.authority_mode == AUTHORITY_MODE_PERSON_APPROVAL
                else "Get the person's agreement in the conversation, then call "
                "apply_change(change_id, reason, confirmed=true)."
            ),
        }

    def load_plan(self, change_id: str) -> SkillUninstallPlan:
        paths = resolve_skill_installation_paths(self.agent)
        plan = self._plan_store(paths).load(str(change_id or "").strip())
        if plan.authority_mode != self._authority_mode():
            raise PermissionError("authorization was turned on or off after planning; create a fresh plan")
        self._verify_plan(plan, actor=acting_agent(self.agent))
        return plan

    def _verify_current(self, plan: SkillUninstallPlan, paths: SkillInstallationPaths) -> None:
        """The plan can still apply: not applied yet and the installed skill unchanged.

        Runs before a person is asked (``describe``) and again when applying,
        so nobody approves a change that can only fail.
        """

        if (paths.uninstallation_receipts / f"{plan.change_id}.json").is_file():
            raise FileExistsError(f"Skill removal plan {plan.change_id} was already applied.")
        self._verify_installed_state(plan, paths)

    def describe(self, change_id: str) -> ChangeProposalV1:
        plan = self.load_plan(change_id)
        self._verify_current(plan, resolve_skill_installation_paths(self.agent))
        return ChangeProposalV1(
            kind=TEAM_SKILL_UNINSTALL_CHANGE_KIND,
            change_id=plan.change_id,
            plan_digest=plan.plan_digest,
            title=f"Remove the team skill {plan.skill_name}",
            summary=summary_line(
                f"Remove the team skill {plan.skill_name} (kept in a recoverable folder). Takes effect now."
            ),
            effect=(
                f"Removes the installed team skill {plan.skill_name}; it is kept in a recoverable folder.",
                "Agents stop finding it at once; a turn that already loaded it keeps it until that turn ends.",
            ),
        )

    def apply(self, change_id: str, approval: ChangeApproval) -> dict[str, Any]:
        """Remove an approved plan's skill (only the change mechanism calls this)."""

        settings = self._settings()
        self._require_enabled(settings)
        paths = resolve_skill_installation_paths(self.agent)
        paths.ensure()
        normalized = str(change_id or "").strip()
        receipt_path = paths.uninstallation_receipts / f"{normalized}.json"
        if receipt_path.is_file():
            return {
                "error": "skill uninstallation plan was already applied",
                "reason_code": "skill_uninstallation_replay",
                "receipt": json.loads(receipt_path.read_text(encoding="utf-8")),
            }
        plan = self.load_plan(normalized)
        agreement = approval.mode is ChangeApprovalMode.AGREEMENT
        if agreement != (plan.authority_mode == AUTHORITY_MODE_AUTHORIZATION_DISABLED):
            raise PermissionError("this plan needs approval or held access, not agreement (or the reverse)")
        self._verify_current(plan, paths)
        authority: dict[str, Any] = {"mode": plan.authority_mode, "approval": approval.record()}
        removed_skill, removed_attestation = self._remove_atomically(plan, paths)
        try:
            applied_at = utc_now()
            receipt = {
                "schema_version": SKILL_UNINSTALLATION_PLAN_SCHEMA_VERSION,
                "receipt_id": new_id("skill_un_rcpt"),
                "change_id": plan.change_id,
                "plan_digest": plan.plan_digest,
                "applied_at_utc": applied_at.isoformat(),
                "actor_principal_id": plan.actor_principal_id,
                "team_id": plan.team_id,
                "skill_name": plan.skill_name,
                "installed_tree_sha256": plan.installed_tree_sha256,
                "installation_change_id": plan.installation_change_id,
                "installation_receipt_sha256": plan.installation_receipt_sha256,
                "removed_skill_path": str(removed_skill),
                "removed_attestation_path": str(removed_attestation),
                "authority": authority,
                "recoverable": True,
            }
            atomic_write_bytes(
                receipt_path,
                (json.dumps(receipt, indent=2, sort_keys=True) + "\n").encode("utf-8"),
                immutable=True,
            )
        except Exception:
            self._restore_after_failed_receipt(
                plan, paths, removed_skill, removed_attestation
            )
            raise
        self.audit.record(
            GovernanceAuditEvent(
                operation="skill_uninstallation",
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
        return {
            "status": "uninstalled",
            **receipt,
            "receipt_path": str(receipt_path),
            "current_turn_note": (
                "The skill is absent from future discovery. A model turn that already loaded its instructions may retain them until that turn ends."
            ),
        }

    def status(self, change_id: str) -> dict[str, Any]:
        paths = resolve_skill_installation_paths(self.agent)
        normalized = str(change_id or "").strip()
        receipt_path = paths.uninstallation_receipts / f"{normalized}.json"
        if receipt_path.is_file():
            return {"status": "uninstalled", "receipt": json.loads(receipt_path.read_text(encoding="utf-8"))}
        plan_path = paths.uninstallation_plans / f"{normalized}.json"
        if plan_path.is_file():
            plan = self._plan_store(paths).load(normalized)
            return {
                "status": "pending" if plan.expires_at_utc > utc_now() else "expired",
                "plan": plan.model_dump(mode="json"),
            }
        return {"status": "missing", "change_id": normalized}

    def _remove_atomically(
        self,
        plan: SkillUninstallPlan,
        paths: SkillInstallationPaths,
    ) -> tuple[Path, Path]:
        installed = Path(plan.installed_path).resolve()
        attestation = Path(plan.attestation_path).resolve()
        tombstone = (paths.removed / plan.change_id).resolve()
        removed_skill = tombstone / plan.skill_name
        removed_attestation = tombstone / f"{plan.skill_name}.attestation.json"
        lock_path = paths.root / ".skill-lifecycle.lock"
        lock_handle = lock_path.open("a+b")
        try:
            try:
                import fcntl

                fcntl.flock(lock_handle.fileno(), fcntl.LOCK_EX)
            except ImportError:  # pragma: no cover
                pass
            self._verify_installed_state(plan, paths)
            if tombstone.exists():
                raise PermissionError("skill removal tombstone already exists")
            tombstone.mkdir(parents=True, mode=0o700)
            os.rename(installed, removed_skill)
            try:
                os.rename(attestation, removed_attestation)
            except Exception:
                os.rename(removed_skill, installed)
                shutil.rmtree(tombstone, ignore_errors=True)
                raise
            return removed_skill, removed_attestation
        finally:
            try:
                import fcntl

                fcntl.flock(lock_handle.fileno(), fcntl.LOCK_UN)
            except ImportError:  # pragma: no cover
                pass
            lock_handle.close()

    @staticmethod
    def _restore_after_failed_receipt(
        plan: SkillUninstallPlan,
        paths: SkillInstallationPaths,
        removed_skill: Path,
        removed_attestation: Path,
    ) -> None:
        installed = Path(plan.installed_path).resolve()
        attestation = Path(plan.attestation_path).resolve()
        lock_path = paths.root / ".skill-lifecycle.lock"
        lock_handle = lock_path.open("a+b")
        try:
            try:
                import fcntl

                fcntl.flock(lock_handle.fileno(), fcntl.LOCK_EX)
            except ImportError:  # pragma: no cover
                pass
            if removed_skill.exists() and not installed.exists():
                os.rename(removed_skill, installed)
            if removed_attestation.exists() and not attestation.exists():
                os.rename(removed_attestation, attestation)
            shutil.rmtree(removed_skill.parent, ignore_errors=True)
        finally:
            try:
                import fcntl

                fcntl.flock(lock_handle.fileno(), fcntl.LOCK_UN)
            except ImportError:  # pragma: no cover
                pass
            lock_handle.close()

    @staticmethod
    def _verify_installed_state(
        plan: SkillUninstallPlan, paths: SkillInstallationPaths
    ) -> None:
        installed = Path(plan.installed_path).resolve()
        attestation = Path(plan.attestation_path).resolve()
        receipt = Path(plan.installation_receipt_path).resolve()
        if installed != (paths.installed / plan.skill_name).resolve():
            raise PermissionError("skill removal plan targets a non-managed path")
        if attestation != (paths.attestations / f"{plan.skill_name}.json").resolve():
            raise PermissionError(
                "skill removal plan targets a non-managed attestation"
            )
        actual_tree = verify_managed_skill_attestation_paths(
            skill_path=installed,
            installed_root=paths.installed,
            attestations_root=paths.attestations,
        )
        if actual_tree != plan.installed_tree_sha256:
            raise PermissionError("installed skill tree changed after removal planning")
        if sha256_bytes(attestation.read_bytes()) != plan.attestation_sha256:
            raise PermissionError(
                "managed skill attestation changed after removal planning"
            )
        if sha256_bytes(receipt.read_bytes()) != plan.installation_receipt_sha256:
            raise PermissionError(
                "managed skill installation receipt changed after removal planning"
            )

    def _verify_plan(self, plan: SkillUninstallPlan, *, actor: ActingAgent) -> None:
        if plan.plan_digest != model_digest(plan):
            raise PermissionError("skill removal plan digest mismatch")
        if plan.expires_at_utc <= utc_now():
            raise PermissionError("skill removal plan has expired; create a fresh plan")
        if (
            plan.actor_principal_id != actor.agent.agent_id
            or plan.actor_named_agent_type != actor.agent.agent_type
            or plan.team_id != actor.agent.team_id
            or plan.owner_canonical_user_id != (actor.person_id or "")
        ):
            raise PermissionError("skill removal plan belongs to another agent, team or person")
        if plan.source_task_id != (actor.task_id or ""):
            raise PermissionError("skill removal plan belongs to another task")

    def _settings(self) -> SkillInstallationSettings:
        return SkillInstallationSettings.from_mapping(
            dict(getattr(self.agent, "skill_installation_config", {}) or {})
        )

    @staticmethod
    def _require_enabled(settings: SkillInstallationSettings) -> None:
        if not settings.enabled:
            raise PermissionError(
                "managed skill lifecycle is disabled by operator configuration"
            )

    def _authority_mode(self) -> str:
        return (
            AUTHORITY_MODE_PERSON_APPROVAL
            if runtime_access(self.agent).enabled
            else AUTHORITY_MODE_AUTHORIZATION_DISABLED
        )


__all__ = [
    "TEAM_SKILL_UNINSTALL_CHANGE_KIND",
    "SKILL_UNINSTALLATION_PLAN_SCHEMA_VERSION",
    "SkillUninstallPlan",
    "SkillUninstallationService",
]
