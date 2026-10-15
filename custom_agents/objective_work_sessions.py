"""Single-manager coordination for bounded, resumable Objective execution."""

from __future__ import annotations

from custom_agents.task_manager_ports import ObjectiveWorkSessionCoordinatorOperations
from custom_agents.task_runtime_state import RuntimeOwnerIdentity
from custom_agents.task_runtime_state import PlanRuntimeState
from custom_agents.task_runtime_state import TaskRuntimeState
from collections.abc import Callable
import logging
from storage.objective_repository import ObjectiveRepository
from custom_agents.objective_work_config import ObjectiveWorkExecutionConfig
from storage.objective_work_repository import ObjectiveWorkRepository

from collections import deque
from datetime import datetime, timedelta, timezone
import re
from typing import Any, Dict, List, Optional, cast

from custom_agents.objective_work_config import (
    ObjectiveWorkCustomProfilesConfig,
    validate_agent_profile,
)
from custom_messages.contracts.common_v1 import contract_sha256
from custom_messages.records.objectives import ObjectiveWorkCheckpoint, ObjectiveWorkInstalledProfile, ObjectiveWorkPolicy, ObjectiveWorkProfile, ObjectiveWorkProfileProposal, ObjectiveWorkSession
from custom_messages.enums import (
    ObjectiveHealthDisposition,
    ObjectiveEventType,
    ObjectiveWorkInstalledProfileStatus,
    ObjectiveWorkPolicyStatus,
    ObjectiveWorkProfileProposalStatus,
    ObjectiveWorkProfileScope,
    ObjectiveWorkProfileSource,
    ObjectiveWorkSessionStatus,
    TaskActivityStage,
    TaskActivityType,
)


_PROFILE_NAME_PATTERN = re.compile(r"[a-z][a-z0-9_-]{0,63}")
_PROPOSAL_DIGEST_EXCLUSIONS = {
    "proposal_digest",
    "status",
    "applied_at",
    "applied_by_task_id",
    "acceptance_summary",
    "installed_profile_id",
    "applied_policy_id",
}


def _proposal_digest(proposal: ObjectiveWorkProfileProposal) -> str:
    return contract_sha256(
        proposal.model_dump(
            mode="json",
            exclude=_PROPOSAL_DIGEST_EXCLUSIONS,
            exclude_none=True,
        )
    )


def _profile_digest(profile: ObjectiveWorkProfile) -> str:
    return contract_sha256(profile.model_dump(mode="json"))


def _policy_state_digest(policy: ObjectiveWorkPolicy) -> str:
    """Bind proposal acceptance to stable policy control state, not scheduler churn."""

    return contract_sha256(
        {
            "policy_id": policy.policy_id,
            "profile_name": policy.profile_name,
            "profile_source": policy.profile_source.value,
            "profile_digest": policy.profile_digest,
            "status": policy.status.value,
            "cadence_cron": policy.cadence_cron,
            "cadence_timezone": policy.cadence_timezone,
            "budget": policy.budget.model_dump(mode="json"),
            "schedule_id": policy.schedule_id,
        }
    )


_TERMINAL_SESSION_STATES = {
    ObjectiveWorkSessionStatus.CHECKPOINTED,
    ObjectiveWorkSessionStatus.COMPLETED,
    ObjectiveWorkSessionStatus.FAILED,
    ObjectiveWorkSessionStatus.CANCELLED,
}


class ObjectiveWorkSessionCoordinator:
    """Applies opt-in work-session policy without changing ordinary task flow."""

    def __init__(
        self,
        *,
        operations: ObjectiveWorkSessionCoordinatorOperations,
        identity: RuntimeOwnerIdentity,
        plan_state: PlanRuntimeState,
        task_state: TaskRuntimeState,
        activity_enabled: Callable[[], bool],
        logger: logging.Logger,
        objective_repository: ObjectiveRepository,
        objective_work_config: ObjectiveWorkExecutionConfig | None,
        objective_work_repository: ObjectiveWorkRepository | None,
    ) -> None:
        self.operations = operations
        self.identity = identity
        self.plan_state = plan_state
        self.task_state = task_state
        self.activity_enabled = activity_enabled
        self._logger = logger
        self.objective_repository = objective_repository
        self.objective_work_config = objective_work_config
        self.objective_work_repository = objective_work_repository


    @property
    def enabled(self) -> bool:
        return self.objective_work_config is not None and self.objective_work_repository is not None

    def _require_enabled(self) -> ObjectiveWorkExecutionConfig:
        if not self.enabled:
            raise RuntimeError(
                "Bounded Objective execution is unavailable. Enable workspace.objective_execution_enabled first."
            )
        return cast(ObjectiveWorkExecutionConfig, self.objective_work_config)

    def _require_owner(self) -> str:
        owner = self.operations.durable_manager_scope_id()
        if not owner:
            raise RuntimeError("Objective work sessions require an initialized manager owner identity.")
        return cast(str, owner)

    @staticmethod
    def _require_policy_user(
        policy: ObjectiveWorkPolicy,
        owner_canonical_user_id: Optional[str],
    ) -> None:
        """Reject cross-user access while allowing trusted runtime calls without user context."""
        if (
            owner_canonical_user_id is not None
            and policy.owner_canonical_user_id
            and policy.owner_canonical_user_id != owner_canonical_user_id
        ):
            raise PermissionError(
                "This Objective work policy belongs to another authenticated user."
            )

    def publish_inventory(self) -> None:
        """Seed the activity projection with canonical state after a manager restart."""
        if not self.activity_enabled() or not self.operations.objectives_enabled() or not self.identity.owner_agent_id:
            return
        owner = self._require_owner()
        try:
            rows = self.objective_repository.list_objectives(
                limit=100,
                owner_agent_id=owner,
                include_closed=True,
            )
            for row in rows:
                objective = self.objective_repository.get_objective(
                    row.objective_id,
                    owner_agent_id=owner,
                )
                if objective is None:
                    continue
                plan = self.operations.get_plan(objective.current_plan_id) if objective.current_plan_id else None
                self.operations.emit_activity(
                    activity_type=TaskActivityType.OBJECTIVE_UPDATED,
                    stage=TaskActivityStage.PLANNING,
                    task_id=objective.related_task_ids[-1] if objective.related_task_ids else None,
                    summary=f"Objective inventory: {objective.title}",
                    detail=objective.progress_summary or objective.desired_outcome,
                    details={
                        "objective": objective.model_dump(mode="json"),
                        "plan": plan.model_dump(mode="json") if plan else None,
                        "objective_work": self.snapshot(objective.objective_id),
                    },
                )
        except Exception as exc:
            self._logger.warning("Failed to publish Objective inventory snapshot: %s", exc)

    def suggest(
        self,
        objective_id: str,
        *,
        estimated_sessions: int,
        owner_canonical_user_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Return an inert suggestion; this never creates or enables a policy."""
        config = self._require_enabled()
        objective = self.operations.get_objective(objective_id)
        if objective is None:
            raise ValueError(f"Unknown Objective {objective_id!r} for this manager.")
        if getattr(objective.status, "value", objective.status) != "active":
            raise ValueError(
                f"Objective {objective_id!r} is {objective.status.value}; bounded execution is suggested only while it is active."
            )
        eligible = (
            config.suggestions.enabled
            and objective.horizon.value in config.suggestions.eligible_horizons
            and estimated_sessions >= config.suggestions.minimum_estimated_sessions
        )
        response = {
            "suggested": eligible,
            "objective_id": objective_id,
            "profile_name": config.default_profile,
            "estimated_sessions": estimated_sessions,
            "requires_explicit_acceptance": True,
            "reason": (
                "The Objective spans multiple bounded work windows."
                if eligible
                else "The Objective does not meet the configured suggestion threshold."
            ),
        }
        if owner_canonical_user_id:
            response["available_profiles"] = self.profile_catalog(
                owner_canonical_user_id=owner_canonical_user_id,
                include_proposals=False,
            )["profiles"]
        response["custom_profile_proposals_enabled"] = config.custom_profiles.enabled
        return response

    @staticmethod
    def _normalize_profile_name(profile_name: str) -> str:
        normalized = str(profile_name or "").strip().lower()
        if not _PROFILE_NAME_PATTERN.fullmatch(normalized):
            raise ValueError(
                "profile_name must start with a lowercase letter and contain only lowercase letters, digits, '_' or '-'."
            )
        return normalized

    def _configured_profile(self, profile_name: str) -> Optional[ObjectiveWorkProfile]:
        config = self._require_enabled()
        key = profile_name.casefold()
        return next(
            (profile for name, profile in config.profiles.items() if name.casefold() == key),
            None,
        )

    def resolve_profile(
        self,
        profile_name: Optional[str],
        *,
        owner_canonical_user_id: Optional[str],
    ) -> tuple[
        str,
        ObjectiveWorkProfile,
        ObjectiveWorkProfileSource,
        Optional[str],
        Optional[str],
    ]:
        config = self._require_enabled()
        selected_name = str(profile_name or config.default_profile).strip()
        configured = self._configured_profile(selected_name)
        if configured is not None:
            configured_name = next(
                name
                for name in config.profiles
                if name.casefold() == selected_name.casefold()
            )
            return (
                configured_name,
                configured,
                ObjectiveWorkProfileSource.CONFIGURED,
                None,
                None,
            )
        if owner_canonical_user_id and config.custom_profiles.enabled:
            installed = self.objective_work_repository.get_installed_profile(
                selected_name,
                owner_agent_id=self._require_owner(),
                owner_canonical_user_id=owner_canonical_user_id,
            )
            if installed is not None:
                validate_agent_profile(
                    installed.profile,
                    settings=config.custom_profiles,
                )
                return (
                    installed.profile_name,
                    installed.profile,
                    ObjectiveWorkProfileSource.INSTALLED,
                    installed.installed_profile_id,
                    installed.source_proposal_id,
                )
        raise ValueError(f"Unknown objective work profile {selected_name!r}.")

    def profile_catalog(
        self,
        *,
        owner_canonical_user_id: str,
        include_proposals: bool = True,
    ) -> Dict[str, Any]:
        config = self._require_enabled()
        owner = self._require_owner()
        configured = [
            {
                "profile_name": name,
                "profile_source": ObjectiveWorkProfileSource.CONFIGURED.value,
                "profile": profile.model_dump(mode="json"),
            }
            for name, profile in config.profiles.items()
        ]
        installed = self.objective_work_repository.list_installed_profiles(
            owner_agent_id=owner,
            owner_canonical_user_id=owner_canonical_user_id,
            limit=config.custom_profiles.max_installed_profiles_per_owner,
        )
        payload: Dict[str, Any] = {
            "profiles": configured
            + [
                {
                    **item.model_dump(mode="json"),
                    "profile_source": ObjectiveWorkProfileSource.INSTALLED.value,
                }
                for item in installed
            ],
            "default_profile": config.default_profile,
            "custom_profile_limits": config.custom_profiles.model_dump(mode="json"),
        }
        if include_proposals:
            payload["proposals"] = [
                item.model_dump(mode="json")
                for item in self.objective_work_repository.list_profile_proposals(
                    owner_agent_id=owner,
                    owner_canonical_user_id=owner_canonical_user_id,
                    limit=50,
                )
            ]
        return payload

    def _require_profile_proposal_target(
        self,
        *,
        scope: ObjectiveWorkProfileScope,
        objective_id: Optional[str],
        settings: ObjectiveWorkCustomProfilesConfig,
    ) -> None:
        if scope == ObjectiveWorkProfileScope.REUSABLE:
            if not settings.allow_reusable_installation:
                raise ValueError("Reusable Objective work profile installation is disabled.")
            return
        objective = self.operations.get_objective(str(objective_id or "").strip())
        if objective is None:
            raise ValueError(f"Unknown Objective {objective_id!r} for this manager.")
        if getattr(objective.status, "value", objective.status) != "active":
            raise ValueError("One-time profiles can target only an active Objective.")

    def _require_installed_profile_capacity(
        self,
        *,
        owner: str,
        owner_canonical_user_id: str,
        settings: ObjectiveWorkCustomProfilesConfig,
    ) -> None:
        active_count = len(
            self.objective_work_repository.list_installed_profiles(
                owner_agent_id=owner,
                owner_canonical_user_id=owner_canonical_user_id,
                limit=settings.max_installed_profiles_per_owner + 1,
            )
        )
        if active_count >= settings.max_installed_profiles_per_owner:
            raise ValueError("The installed Objective work profile limit has been reached.")

    def _profile_replacement_target(
        self,
        *,
        scope: ObjectiveWorkProfileScope,
        profile_name: str,
        owner: str,
        owner_canonical_user_id: str,
        replace_existing: bool,
        settings: ObjectiveWorkCustomProfilesConfig,
    ) -> Optional[ObjectiveWorkInstalledProfile]:
        if scope != ObjectiveWorkProfileScope.REUSABLE:
            return None
        existing = self.objective_work_repository.get_installed_profile(
            profile_name,
            owner_agent_id=owner,
            owner_canonical_user_id=owner_canonical_user_id,
            include_removed=True,
        )
        active_existing = (
            existing
            if existing is not None
            and existing.status == ObjectiveWorkInstalledProfileStatus.ACTIVE
            else None
        )
        if active_existing is not None and not replace_existing:
            raise ValueError(
                f"Reusable profile {profile_name!r} already exists; set replace_existing only after discussing replacement."
            )
        if active_existing is None:
            self._require_installed_profile_capacity(
                owner=owner,
                owner_canonical_user_id=owner_canonical_user_id,
                settings=settings,
            )
        return cast(Optional[ObjectiveWorkInstalledProfile], active_existing)

    def _proposal_policy_state(
        self,
        *,
        scope: ObjectiveWorkProfileScope,
        objective_id: Optional[str],
        owner: str,
    ) -> Optional[ObjectiveWorkPolicy]:
        if scope != ObjectiveWorkProfileScope.OBJECTIVE:
            return None
        return cast(
            Optional[ObjectiveWorkPolicy],
            self.objective_work_repository.get_policy(
                cast(str, objective_id),
                owner_agent_id=owner,
            ),
        )

    def propose_profile(
        self,
        *,
        owner_canonical_user_id: str,
        source_task_id: str,
        scope: ObjectiveWorkProfileScope | str,
        objective_id: Optional[str],
        profile_name: str,
        profile: ObjectiveWorkProfile,
        rationale: str,
        replace_existing: bool,
    ) -> ObjectiveWorkProfileProposal:
        config = self._require_enabled()
        owner = self._require_owner()
        settings = config.custom_profiles
        normalized_scope = ObjectiveWorkProfileScope(scope)
        normalized_name = self._normalize_profile_name(profile_name)
        validate_agent_profile(profile, settings=settings)
        normalized_rationale = str(rationale or "").strip()
        if not normalized_rationale:
            raise ValueError("A profile proposal rationale is required.")
        if self._configured_profile(normalized_name) is not None:
            raise ValueError(
                "Agent-authored profiles cannot replace an operator-configured profile name."
            )
        self._require_profile_proposal_target(
            scope=normalized_scope,
            objective_id=objective_id,
            settings=settings,
        )
        active_existing = self._profile_replacement_target(
            scope=normalized_scope,
            profile_name=normalized_name,
            owner=owner,
            owner_canonical_user_id=owner_canonical_user_id,
            replace_existing=replace_existing,
            settings=settings,
        )
        current_policy = self._proposal_policy_state(
            scope=normalized_scope,
            objective_id=objective_id,
            owner=owner,
        )

        now = datetime.now(timezone.utc)
        proposal = ObjectiveWorkProfileProposal(
            owner_agent_id=owner,
            owner_canonical_user_id=owner_canonical_user_id,
            source_task_id=source_task_id,
            scope=normalized_scope,
            objective_id=(
                str(objective_id).strip()
                if normalized_scope == ObjectiveWorkProfileScope.OBJECTIVE
                else None
            ),
            profile_name=normalized_name,
            profile=profile,
            rationale=normalized_rationale,
            replace_existing=bool(replace_existing and active_existing is not None),
            replaces_installed_profile_id=(
                active_existing.installed_profile_id if active_existing else None
            ),
            replaces_installed_profile_digest=(
                _profile_digest(active_existing.profile) if active_existing else None
            ),
            expected_policy_state_digest=(
                _policy_state_digest(current_policy) if current_policy else None
            ),
            proposal_digest="0" * 64,
            created_at=now.isoformat(),
            expires_at=(
                now + timedelta(seconds=settings.proposal_ttl_seconds)
            ).isoformat(),
        )
        proposal.proposal_digest = _proposal_digest(proposal)
        self.objective_work_repository.save_profile_proposal(proposal)
        return proposal

    def require_profile_proposal(
        self,
        proposal_id: str,
        *,
        proposal_digest: str,
        owner_canonical_user_id: str,
    ) -> ObjectiveWorkProfileProposal:
        config = self._require_enabled()
        proposal = cast(
            Optional[ObjectiveWorkProfileProposal],
            self.objective_work_repository.get_profile_proposal(
                str(proposal_id or "").strip(),
                owner_agent_id=self._require_owner(),
                owner_canonical_user_id=owner_canonical_user_id,
            ),
        )
        if proposal is None:
            raise ValueError("Unknown Objective work profile proposal.")
        if proposal.proposal_digest != _proposal_digest(proposal):
            raise PermissionError("Objective work profile proposal digest mismatch.")
        if proposal.proposal_digest != str(proposal_digest or "").strip().lower():
            raise PermissionError("The accepted proposal digest does not match.")
        if proposal.status == ObjectiveWorkProfileProposalStatus.APPLIED:
            return proposal
        if datetime.fromisoformat(proposal.expires_at.replace("Z", "+00:00")) <= datetime.now(timezone.utc):
            raise PermissionError("The Objective work profile proposal has expired.")
        validate_agent_profile(
            proposal.profile,
            settings=config.custom_profiles,
        )
        if proposal.scope == ObjectiveWorkProfileScope.OBJECTIVE:
            current_policy = self.objective_work_repository.get_policy(
                cast(str, proposal.objective_id),
                owner_agent_id=self._require_owner(),
            )
            current_policy_digest = (
                _policy_state_digest(current_policy) if current_policy else None
            )
            failed_partial_application = bool(
                current_policy is not None
                and current_policy.profile_proposal_id == proposal.proposal_id
                and current_policy.profile_source == ObjectiveWorkProfileSource.ONE_TIME
                and current_policy.profile_digest == _profile_digest(proposal.profile)
                and current_policy.status == ObjectiveWorkPolicyStatus.DISABLED
            )
            if (
                current_policy_digest != proposal.expected_policy_state_digest
                and not failed_partial_application
            ):
                raise PermissionError(
                    "The Objective work policy changed after this profile was proposed; create a fresh proposal."
                )
        return proposal

    def install_reusable_profile(
        self,
        proposal: ObjectiveWorkProfileProposal,
        *,
        accepted_by_task_id: str,
        acceptance_summary: str,
    ) -> ObjectiveWorkInstalledProfile:
        config = self._require_enabled()
        if proposal.scope != ObjectiveWorkProfileScope.REUSABLE:
            raise ValueError("Only reusable profile proposals can be installed.")
        if not config.custom_profiles.allow_reusable_installation:
            raise ValueError("Reusable Objective work profile installation is disabled.")
        normalized_summary = str(acceptance_summary or "").strip()
        if not normalized_summary:
            raise ValueError("An explicit user acceptance summary is required.")
        existing = cast(
            Optional[ObjectiveWorkInstalledProfile],
            self.objective_work_repository.get_installed_profile(
                proposal.profile_name,
                owner_agent_id=self._require_owner(),
                owner_canonical_user_id=proposal.owner_canonical_user_id,
                include_removed=True,
            ),
        )
        if (
            existing is not None
            and existing.status == ObjectiveWorkInstalledProfileStatus.ACTIVE
            and existing.source_proposal_id == proposal.proposal_id
            and existing.proposal_digest == proposal.proposal_digest
        ):
            return existing
        if proposal.replace_existing and (
            existing is None
            or existing.status != ObjectiveWorkInstalledProfileStatus.ACTIVE
            or existing.installed_profile_id
            != proposal.replaces_installed_profile_id
            or _profile_digest(existing.profile)
            != proposal.replaces_installed_profile_digest
        ):
            raise PermissionError(
                "The reusable Objective work profile changed after this replacement was proposed; create a fresh proposal."
            )
        if existing is not None and existing.status == ObjectiveWorkInstalledProfileStatus.ACTIVE and not proposal.replace_existing:
            raise ValueError(f"Reusable profile {proposal.profile_name!r} already exists.")
        now = datetime.now(timezone.utc).isoformat()
        installed = ObjectiveWorkInstalledProfile(
            owner_agent_id=proposal.owner_agent_id,
            owner_canonical_user_id=proposal.owner_canonical_user_id,
            profile_name=proposal.profile_name,
            profile=proposal.profile.model_copy(deep=True),
            source_proposal_id=proposal.proposal_id,
            proposal_digest=proposal.proposal_digest,
            installed_by_task_id=accepted_by_task_id,
            acceptance_summary=normalized_summary,
            created_at=existing.created_at if existing else now,
            updated_at=now,
        )
        self.objective_work_repository.save_installed_profile(installed)
        return installed

    def mark_profile_proposal_applied(
        self,
        proposal: ObjectiveWorkProfileProposal,
        *,
        accepted_by_task_id: str,
        acceptance_summary: str,
        installed_profile_id: Optional[str] = None,
        applied_policy_id: Optional[str] = None,
    ) -> ObjectiveWorkProfileProposal:
        if proposal.status == ObjectiveWorkProfileProposalStatus.APPLIED:
            return proposal
        proposal.status = ObjectiveWorkProfileProposalStatus.APPLIED
        proposal.applied_at = datetime.now(timezone.utc).isoformat()
        proposal.applied_by_task_id = accepted_by_task_id
        proposal.acceptance_summary = acceptance_summary.strip()
        proposal.installed_profile_id = installed_profile_id
        proposal.applied_policy_id = applied_policy_id
        self.objective_work_repository.save_profile_proposal(proposal)
        return proposal

    def remove_installed_profile(
        self,
        profile_name: str,
        *,
        owner_canonical_user_id: str,
        removed_by_task_id: str,
        removal_summary: str,
    ) -> ObjectiveWorkInstalledProfile:
        normalized_name = self._normalize_profile_name(profile_name)
        normalized_summary = str(removal_summary or "").strip()
        if not normalized_summary:
            raise ValueError("An explicit removal summary is required.")
        installed = cast(
            Optional[ObjectiveWorkInstalledProfile],
            self.objective_work_repository.get_installed_profile(
                normalized_name,
                owner_agent_id=self._require_owner(),
                owner_canonical_user_id=owner_canonical_user_id,
            ),
        )
        if installed is None:
            raise ValueError(f"Unknown installed Objective work profile {normalized_name!r}.")
        installed.status = ObjectiveWorkInstalledProfileStatus.REMOVED
        installed.removed_at = datetime.now(timezone.utc).isoformat()
        installed.removed_by_task_id = removed_by_task_id
        installed.removal_summary = normalized_summary
        installed.updated_at = installed.removed_at
        self.objective_work_repository.save_installed_profile(installed)
        return installed

    def enable_policy(
        self,
        objective_id: str,
        *,
        profile_name: Optional[str],
        accepted_by_task_id: str,
        acceptance_summary: str,
        owner_canonical_user_id: Optional[str] = None,
        profile_override: Optional[ObjectiveWorkProfile] = None,
        profile_source: ObjectiveWorkProfileSource = ObjectiveWorkProfileSource.CONFIGURED,
        installed_profile_id: Optional[str] = None,
        profile_proposal_id: Optional[str] = None,
    ) -> ObjectiveWorkPolicy:
        config = self._require_enabled()
        owner = self._require_owner()
        objective = self.operations.get_objective(objective_id)
        if objective is None:
            raise ValueError(f"Unknown Objective {objective_id!r} for this manager.")
        if getattr(objective.status, "value", objective.status) != "active":
            raise ValueError(
                f"Objective {objective_id!r} is {objective.status.value}; bounded execution can be enabled only while it is active."
            )
        existing = self.objective_work_repository.get_policy(
            objective_id,
            owner_agent_id=owner,
        )
        if (
            existing is not None
            and existing.owner_canonical_user_id
            and existing.owner_canonical_user_id != owner_canonical_user_id
        ):
            raise PermissionError(
                "This Objective work policy belongs to another authenticated user."
            )
        if profile_override is None:
            (
                selected_name,
                profile,
                resolved_source,
                resolved_installed_id,
                resolved_proposal_id,
            ) = self.resolve_profile(
                profile_name,
                owner_canonical_user_id=owner_canonical_user_id,
            )
            profile_source = resolved_source
            installed_profile_id = resolved_installed_id
            profile_proposal_id = resolved_proposal_id
        else:
            selected_name = self._normalize_profile_name(str(profile_name or ""))
            profile = profile_override
            validate_agent_profile(profile, settings=config.custom_profiles)
        if not str(acceptance_summary or "").strip():
            raise ValueError("An explicit user acceptance summary is required.")
        now = datetime.now(timezone.utc).isoformat()
        policy_fields = {
            "objective_id": objective_id,
            "owner_agent_id": owner,
            "profile_name": selected_name,
            "profile_source": profile_source,
            "profile_digest": _profile_digest(profile),
            "owner_canonical_user_id": owner_canonical_user_id,
            "installed_profile_id": installed_profile_id,
            "profile_proposal_id": profile_proposal_id,
            "status": ObjectiveWorkPolicyStatus.ACTIVE,
            "cadence_cron": profile.cadence_cron,
            "cadence_timezone": profile.cadence_timezone,
            "required_skills": list(config.required_skills),
            "budget": profile.budget.model_copy(deep=True),
            "accepted_by_task_id": accepted_by_task_id,
            "acceptance_summary": acceptance_summary.strip(),
            "schedule_id": existing.schedule_id if existing else None,
            "next_run_at": existing.next_run_at if existing else None,
            "created_at": existing.created_at if existing else now,
            "updated_at": now,
        }
        if existing:
            policy_fields["policy_id"] = existing.policy_id
        policy = ObjectiveWorkPolicy.model_validate(policy_fields)
        self.objective_work_repository.save_policy(policy)
        self.operations.append_objective_event(
            objective,
            ObjectiveEventType.WORK_SESSION_POLICY_ENABLED,
            "Enabled bounded Objective work sessions after explicit acceptance.",
            task_id=accepted_by_task_id,
            schedule_id=policy.schedule_id,
            user_visible=True,
            details={"policy": policy.model_dump(mode="json")},
        )
        self.operations.save_objective(objective)
        self._emit_snapshot(objective_id, "Bounded Objective execution enabled.", task_id=accepted_by_task_id)
        return policy

    def set_schedule(self, objective_id: str, *, schedule_id: str, next_run_at: Optional[str] = None) -> ObjectiveWorkPolicy:
        owner = self._require_owner()
        policy = self.objective_work_repository.get_policy(objective_id, owner_agent_id=owner)
        if policy is None:
            raise ValueError(f"No bounded execution policy exists for Objective {objective_id!r}.")
        policy.schedule_id = schedule_id
        policy.next_run_at = next_run_at
        policy.updated_at = datetime.now(timezone.utc).isoformat()
        self.objective_work_repository.save_policy(policy)
        return cast(ObjectiveWorkPolicy, policy)

    def disable_policy(
        self,
        objective_id: str,
        *,
        owner_canonical_user_id: Optional[str] = None,
        by_objective_control: bool = False,
    ) -> ObjectiveWorkPolicy:
        owner = self._require_owner()
        policy = self.objective_work_repository.get_policy(objective_id, owner_agent_id=owner)
        if policy is None:
            raise ValueError(f"No bounded execution policy exists for Objective {objective_id!r}.")
        self._require_policy_user(policy, owner_canonical_user_id)
        if (
            policy.status == ObjectiveWorkPolicyStatus.DISABLED
            and by_objective_control
            and not policy.disabled_by_objective_control
        ):
            return cast(ObjectiveWorkPolicy, policy)
        policy.status = ObjectiveWorkPolicyStatus.DISABLED
        policy.disabled_by_objective_control = bool(by_objective_control)
        policy.updated_at = datetime.now(timezone.utc).isoformat()
        self.objective_work_repository.save_policy(policy)
        objective = self.operations.get_objective(objective_id)
        if objective is not None:
            self.operations.append_objective_event(
                objective,
                ObjectiveEventType.WORK_SESSION_POLICY_DISABLED,
                "Disabled bounded Objective work sessions.",
                user_visible=True,
                details={"policy": policy.model_dump(mode="json")},
            )
            self.operations.save_objective(objective)
        self._emit_snapshot(objective_id, "Bounded Objective execution disabled.", task_id=None)
        return cast(ObjectiveWorkPolicy, policy)

    def enable_policy_after_objective_control(
        self,
        objective_id: str,
        *,
        owner_canonical_user_id: Optional[str] = None,
    ) -> ObjectiveWorkPolicy:
        """Resume only a policy disabled by an Objective lifecycle control."""

        owner = self._require_owner()
        policy = self.objective_work_repository.get_policy(
            objective_id,
            owner_agent_id=owner,
        )
        if policy is None:
            raise ValueError(
                f"No bounded execution policy exists for Objective {objective_id!r}."
            )
        self._require_policy_user(policy, owner_canonical_user_id)
        if not policy.disabled_by_objective_control:
            return cast(ObjectiveWorkPolicy, policy)
        policy.status = ObjectiveWorkPolicyStatus.ACTIVE
        policy.disabled_by_objective_control = False
        policy.updated_at = datetime.now(timezone.utc).isoformat()
        self.objective_work_repository.save_policy(policy)
        objective = self.operations.get_objective(objective_id)
        if objective is not None:
            self.operations.append_objective_event(
                objective,
                ObjectiveEventType.WORK_SESSION_POLICY_ENABLED,
                "Resumed bounded Objective work sessions after Objective control.",
                user_visible=True,
                details={"policy": policy.model_dump(mode="json")},
            )
            self.operations.save_objective(objective)
        self._emit_snapshot(
            objective_id,
            "Bounded Objective execution resumed.",
            task_id=None,
        )
        return cast(ObjectiveWorkPolicy, policy)

    def start_session(
        self,
        objective_id: str,
        *,
        task_id: str,
        owner_canonical_user_id: Optional[str] = None,
    ) -> ObjectiveWorkSession:
        self._require_enabled()
        owner = self._require_owner()
        policy = self.objective_work_repository.get_policy(objective_id, owner_agent_id=owner)
        if policy is None or policy.status != ObjectiveWorkPolicyStatus.ACTIVE:
            raise ValueError(f"Objective {objective_id!r} has no active bounded execution policy.")
        self._require_policy_user(policy, owner_canonical_user_id)
        task_info = self.operations.get_task_info(task_id)
        if task_info is None:
            raise ValueError(f"Unknown task {task_id!r} for the work session.")
        objective = self.operations.get_objective(objective_id)
        if objective is None:
            raise ValueError(f"Unknown Objective {objective_id!r} for this manager.")
        if getattr(objective.status, "value", objective.status) != "active":
            raise ValueError(
                f"Objective {objective_id!r} is {objective.status.value}; work sessions start only while it is active."
            )
        missing_reserves = [
            field_name
            for field_name in (
                "checkpoint_reserve_seconds",
                "checkpoint_reserve_turns",
                "checkpoint_reserve_tokens",
                "checkpoint_reserve_tool_calls",
            )
            if getattr(policy.budget, field_name) <= 0
        ]
        if missing_reserves:
            raise ValueError(
                "This Objective policy predates checkpoint resource reserves. "
                "Discuss and accept a current profile before another run. Missing: "
                + ", ".join(missing_reserves)
            )
        existing = self.objective_work_repository.get_session_by_task(task_id, owner_agent_id=owner)
        if existing and existing.status not in _TERMINAL_SESSION_STATES:
            return cast(ObjectiveWorkSession, existing)
        now_dt = datetime.now(timezone.utc)
        safe_seconds = policy.budget.active_seconds - policy.budget.checkpoint_reserve_seconds
        session = ObjectiveWorkSession(
            policy_id=policy.policy_id,
            objective_id=objective_id,
            owner_agent_id=owner,
            task_id=task_id,
            status=ObjectiveWorkSessionStatus.RUNNING,
            budget=policy.budget.model_copy(deep=True),
            required_skills=list(policy.required_skills),
            started_at=now_dt.isoformat(),
            safe_deadline_at=(now_dt + timedelta(seconds=safe_seconds)).isoformat(),
            hard_deadline_at=(now_dt + timedelta(seconds=policy.budget.active_seconds)).isoformat(),
        )
        task_info.meta.objective_id = objective_id
        task_info.meta.objective_work_session_id = session.session_id
        self.objective_work_repository.save_session(session)
        self.operations.persist_runtime_state()
        self.operations.append_objective_event(
            objective,
            ObjectiveEventType.WORK_SESSION_STARTED,
            f"Started bounded work session {session.session_id}.",
            task_id=task_id,
            user_visible=True,
            details={"session": session.model_dump(mode="json")},
        )
        self.operations.save_objective(objective)
        self._emit_snapshot(objective_id, "Objective work session started.", task_id=task_id)
        return session

    def resume_existing_plan(self, session: ObjectiveWorkSession) -> bool:
        """Use a timer task as the new root anchor for the next durable plan step."""
        objective = self.operations.get_objective(session.objective_id)
        root_info = self.operations.get_task_info(session.task_id)
        plan = self.operations.get_plan(objective.current_plan_id) if objective and objective.current_plan_id else None
        if root_info is None or plan is None or not plan.plan_steps:
            return False
        if any(getattr(step.status, "value", step.status) == "in_progress" for step in plan.plan_steps):
            return False
        plan.active_execution_task_id = session.task_id
        plan.root_conversation_id = getattr(root_info.meta, "conversation_id", None) or plan.root_conversation_id
        plan.updated_at = datetime.now(timezone.utc).isoformat()
        self.operations.save_plan(plan)
        self.plan_state.task_to_plan_id[session.task_id] = plan.plan_id
        admitted = self.operations.enqueue_next_plan_step_task(
            plan,
            root_task_info=root_info,
            timestamp=plan.updated_at,
        ) is not None
        if admitted:
            self.task_state.task_queue = deque(
                task_id for task_id in self.task_state.task_queue if task_id != session.task_id
            )
        else:
            # A blocked or finished plan has no child that can wake the root.
            # Keep the timer task runnable so it can checkpoint and finish.
            plan.active_execution_task_id = None
            self.operations.save_plan(plan)
        self.operations.persist_runtime_state()
        return admitted

    def session_for_task(self, task_id: Optional[str]) -> Optional[ObjectiveWorkSession]:
        if not task_id or not self.enabled or not self.identity.owner_agent_id:
            return None
        return cast(
            Optional[ObjectiveWorkSession],
            self.objective_work_repository.get_session_by_task(
                task_id, owner_agent_id=self._require_owner()
            ),
        )

    def session_for_meta(self, session_id: Optional[str]) -> Optional[ObjectiveWorkSession]:
        if not session_id or not self.enabled or not self.identity.owner_agent_id:
            return None
        return cast(
            Optional[ObjectiveWorkSession],
            self.objective_work_repository.get_session(
                session_id, owner_agent_id=self._require_owner()
            ),
        )

    def record_skill_activation(self, task_id: str, skill_name: str) -> None:
        session = self.session_for_task(task_id)
        if session is None:
            task_info = self.operations.get_task_info(task_id)
            session = self.session_for_meta(
                getattr(getattr(task_info, "meta", None), "objective_work_session_id", None)
            )
        if session is None or skill_name not in session.required_skills:
            return
        if skill_name not in session.activated_skills:
            session.activated_skills.append(skill_name)
            session.updated_at = datetime.now(timezone.utc).isoformat()
            self.objective_work_repository.save_session(session)

    @staticmethod
    def missing_required_skills(session: ObjectiveWorkSession) -> List[str]:
        activated = set(session.activated_skills)
        return [name for name in session.required_skills if name not in activated]

    def admits_plan_step(self, plan: Any, root_task_info: Any) -> bool:
        """Block only opted-in Objectives that are outside a live work window."""
        if not self.enabled or not getattr(plan, "objective_id", None):
            return True
        owner = self._require_owner()
        policy = self.objective_work_repository.get_policy(plan.objective_id, owner_agent_id=owner)
        if policy is None or policy.status != ObjectiveWorkPolicyStatus.ACTIVE:
            return True
        session = self.session_for_meta(
            getattr(root_task_info.meta, "objective_work_session_id", None)
        )
        if session is None:
            return False
        if session.status in {ObjectiveWorkSessionStatus.YIELD_REQUESTED, ObjectiveWorkSessionStatus.OVERRUN}:
            if not session.checkpoint_id:
                self.degraded_checkpoint(session, reason=session.yield_reason or "The work-session boundary was reached.")
            return False
        if session.status != ObjectiveWorkSessionStatus.RUNNING:
            return False
        if self.should_yield(session):
            self.degraded_checkpoint(session, reason="The configured safe work-session boundary was reached between plan steps.")
            return False
        return True

    @staticmethod
    def _parse_time(value: Optional[str]) -> Optional[datetime]:
        if not value:
            return None
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)

    def should_yield(self, session: ObjectiveWorkSession) -> bool:
        if session.status != ObjectiveWorkSessionStatus.RUNNING:
            return True
        safe_deadline = self._parse_time(session.safe_deadline_at)
        return bool(
            (safe_deadline and datetime.now(timezone.utc) >= safe_deadline)
            or session.turns_used >= session.budget.turns
            or session.total_tokens_used >= session.budget.total_tokens
            or session.tool_calls_used >= session.budget.tool_calls
        )

    def invocation_limits(self, session: ObjectiveWorkSession) -> Dict[str, int]:
        """Return material-work limits with the checkpoint budget held back."""

        return {
            "turns": max(
                1,
                session.budget.turns
                - session.turns_used
                - session.budget.checkpoint_reserve_turns,
            ),
            "total_tokens": max(
                1,
                session.budget.total_tokens
                - session.total_tokens_used
                - session.budget.checkpoint_reserve_tokens,
            ),
        }

    def checkpoint_invocation_limits(
        self, session: ObjectiveWorkSession
    ) -> Dict[str, int]:
        """Return the remaining portion of the checkpoint-only model reserve."""

        remaining_turns = max(1, session.budget.turns - session.turns_used)
        remaining_tokens = max(
            1, session.budget.total_tokens - session.total_tokens_used
        )
        return {
            "turns": max(
                1,
                min(session.budget.checkpoint_reserve_turns, remaining_turns),
            ),
            "total_tokens": max(
                1,
                min(session.budget.checkpoint_reserve_tokens, remaining_tokens),
            ),
        }

    @staticmethod
    def remaining_work_tool_calls(session: ObjectiveWorkSession) -> int:
        return max(
            1,
            session.budget.tool_calls
            - session.tool_calls_used
            - session.budget.checkpoint_reserve_tool_calls,
        )

    def prompt_context(self, session: ObjectiveWorkSession) -> str:
        skills = ", ".join(session.required_skills)
        remaining_turns = max(0, session.budget.turns - session.turns_used)
        remaining_tokens = max(0, session.budget.total_tokens - session.total_tokens_used)
        remaining_tools = max(0, session.budget.tool_calls - session.tool_calls_used)
        return (
            "\n\nBOUNDED OBJECTIVE WORK SESSION\n"
            f"Session: {session.session_id}\n"
            f"Safe checkpoint boundary: {session.safe_deadline_at}; hard boundary: {session.hard_deadline_at}.\n"
            f"Remaining budget: {remaining_turns} turns, {remaining_tokens} total tokens, "
            f"and {remaining_tools} tool calls.\n"
            "The runtime holds back "
            f"{session.budget.checkpoint_reserve_turns} turns, "
            f"{session.budget.checkpoint_reserve_tokens} tokens, and "
            f"{session.budget.checkpoint_reserve_tool_calls} tool calls for a "
            "checkpoint-only pass. Do not treat that reserve as material-work capacity.\n"
            f"Required skills: {skills}. Activate each through the skills tool before material work.\n"
            "Resume by revalidating the durable Objective, plan, authority, procedures, evidence, and any in-flight effects. "
            "Measure progress as evidence delta, not activity or an invented percentage. "
            "As the safe boundary approaches, stop starting new work and leave an OODA checkpoint: "
            "Observe, Orient, Decide, Act, health disposition, evidence delta, blockers, artifacts, and the next subgoal."
        )

    def account_invocation(
        self,
        session: ObjectiveWorkSession,
        *,
        elapsed_seconds: float,
        turns: int,
        input_tokens: int,
        output_tokens: int,
        total_tokens: int,
        stop_reason: Optional[str],
        cache_read_input_tokens: int = 0,
        cache_write_input_tokens: int = 0,
        tool_calls: int = 0,
    ) -> ObjectiveWorkSession:
        persisted = self.objective_work_repository.get_session(
            session.session_id,
            owner_agent_id=session.owner_agent_id,
        )
        if persisted is not None:
            session = persisted
        session.active_seconds_used += max(0.0, elapsed_seconds)
        session.turns_used += max(0, turns)
        session.input_tokens_used += max(0, input_tokens)
        session.output_tokens_used += max(0, output_tokens)
        session.total_tokens_used += max(0, total_tokens)
        session.cache_read_input_tokens_used += max(0, cache_read_input_tokens)
        session.cache_write_input_tokens_used += max(0, cache_write_input_tokens)
        session.tool_calls_used += max(0, tool_calls)
        if session.status not in _TERMINAL_SESSION_STATES:
            if str(stop_reason or "").startswith("limit_") or self.should_yield(session):
                session.status = ObjectiveWorkSessionStatus.YIELD_REQUESTED
                session.yield_reason = str(stop_reason or "safe boundary reached")
            hard_deadline = self._parse_time(session.hard_deadline_at)
            if hard_deadline and datetime.now(timezone.utc) > hard_deadline:
                session.status = ObjectiveWorkSessionStatus.OVERRUN
                session.yield_reason = "The active operation crossed the hard deadline and was allowed to finish safely."
        session.updated_at = datetime.now(timezone.utc).isoformat()
        self.objective_work_repository.save_session(session)
        return session

    def finish_for_task(
        self,
        task_id: str,
        terminal_state: Any,
        *,
        timestamp: Optional[str] = None,
    ) -> Optional[ObjectiveWorkSession]:
        """Synchronize a bounded work session with its Task terminal state."""

        session = self.session_for_task(task_id)
        if session is None or session.status in _TERMINAL_SESSION_STATES:
            return session
        state_value = str(getattr(terminal_state, "value", terminal_state) or "")
        session.status = {
            "completed": ObjectiveWorkSessionStatus.COMPLETED,
            "cancelled": ObjectiveWorkSessionStatus.CANCELLED,
        }.get(state_value, ObjectiveWorkSessionStatus.FAILED)
        session.ended_at = timestamp or datetime.now(timezone.utc).isoformat()
        session.updated_at = session.ended_at
        if state_value == "cancelled" and not session.yield_reason:
            session.yield_reason = "The operator cancelled this execution window."
        self.objective_work_repository.save_session(session)
        self._emit_snapshot(
            session.objective_id,
            f"Objective work session ended with state {session.status.value}.",
            task_id=task_id,
        )
        return session

    def checkpoint(
        self,
        session_id: str,
        *,
        observe: str,
        orient: str,
        decide: str,
        act: str,
        health: ObjectiveHealthDisposition | str,
        evidence_delta: Optional[List[str]] = None,
        completed_subgoals: Optional[List[str]] = None,
        next_subgoal: Optional[str] = None,
        blockers: Optional[List[str]] = None,
        in_flight_effects: Optional[List[str]] = None,
        artifacts: Optional[List[str]] = None,
        degraded: bool = False,
    ) -> ObjectiveWorkCheckpoint:
        owner = self._require_owner()
        session = self.objective_work_repository.get_session(session_id, owner_agent_id=owner)
        if session is None:
            raise ValueError(f"Unknown Objective work session {session_id!r}.")
        checkpoint = ObjectiveWorkCheckpoint(
            session_id=session.session_id,
            policy_id=session.policy_id,
            objective_id=session.objective_id,
            owner_agent_id=owner,
            health=ObjectiveHealthDisposition(health),
            observe=observe.strip(), orient=orient.strip(), decide=decide.strip(), act=act.strip(),
            evidence_delta=list(evidence_delta or []),
            completed_subgoals=list(completed_subgoals or []), next_subgoal=next_subgoal,
            blockers=list(blockers or []), in_flight_effects=list(in_flight_effects or []),
            artifacts=list(artifacts or []), degraded=degraded,
        )
        self.objective_work_repository.save_checkpoint(checkpoint)
        session.checkpoint_id = checkpoint.checkpoint_id
        session.status = ObjectiveWorkSessionStatus.CHECKPOINTED
        session.ended_at = datetime.now(timezone.utc).isoformat()
        session.updated_at = session.ended_at
        self.objective_work_repository.save_session(session)
        objective = self.operations.get_objective(session.objective_id)
        if objective is not None:
            self.operations.append_objective_event(
                objective,
                ObjectiveEventType.WORK_SESSION_CHECKPOINTED,
                f"Checkpointed bounded work session {session.session_id}.",
                task_id=session.task_id,
                user_visible=True,
                details={"checkpoint": checkpoint.model_dump(mode="json")},
            )
            self.operations.save_objective(objective)
        self._emit_snapshot(session.objective_id, "Objective work session checkpointed.", task_id=session.task_id)
        return checkpoint

    def degraded_checkpoint(self, session: ObjectiveWorkSession, *, reason: str) -> ObjectiveWorkCheckpoint:
        objective = self.operations.get_objective(session.objective_id)
        plan = self.operations.get_plan(objective.current_plan_id) if objective and objective.current_plan_id else None
        completed = [
            step.title for step in (getattr(plan, "plan_steps", None) or [])
            if getattr(getattr(step, "status", None), "value", None) == "completed"
        ]
        next_step = next(
            (step.title for step in (getattr(plan, "plan_steps", None) or [])
             if getattr(getattr(step, "status", None), "value", None) in {"planned", "in_progress", "blocked"}),
            None,
        )
        return self.checkpoint(
            session.session_id,
            health=ObjectiveHealthDisposition.NEEDS_HUMAN if self.missing_required_skills(session) else ObjectiveHealthDisposition.AT_RISK,
            observe=reason,
            orient="The runtime reached a safe session boundary before the agent supplied a complete OODA checkpoint.",
            decide="Preserve durable state and resume only in a later accepted work session.",
            act="Revalidate the Objective, evidence, authority, required skill, and in-flight effects before continuing.",
            completed_subgoals=completed,
            next_subgoal=next_step,
            blockers=[f"Required skill not activated: {name}" for name in self.missing_required_skills(session)],
            degraded=True,
        )

    def snapshot(
        self,
        objective_id: str,
        *,
        owner_canonical_user_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        if not self.enabled or not self.identity.owner_agent_id:
            return {}
        owner = self._require_owner()
        policy = self.objective_work_repository.get_policy(objective_id, owner_agent_id=owner)
        if policy is not None:
            self._require_policy_user(policy, owner_canonical_user_id)
        sessions = self.objective_work_repository.list_sessions(
            objective_id, owner_agent_id=owner, limit=20
        )
        limit = self.objective_work_config.visibility.checkpoint_history_limit
        checkpoints = self.objective_work_repository.list_checkpoints(
            objective_id, owner_agent_id=owner, limit=limit
        )
        latest_session = sessions[0] if sessions else None
        tuning_signals: Optional[Dict[str, Any]] = None
        if latest_session is not None:
            work_token_allowance = max(
                1,
                latest_session.budget.total_tokens
                - latest_session.budget.checkpoint_reserve_tokens,
            )
            work_tool_call_allowance = max(
                1,
                latest_session.budget.tool_calls
                - latest_session.budget.checkpoint_reserve_tool_calls,
            )
            average_tokens_per_turn = (
                latest_session.total_tokens_used / latest_session.turns_used
                if latest_session.turns_used
                else None
            )
            limit_dimension = None
            if str(latest_session.yield_reason or "").startswith("limit_"):
                limit_dimension = str(latest_session.yield_reason).removeprefix("limit_")
            tuning_signals = {
                "needs_profile_review": limit_dimension is not None,
                "limit_dimension": limit_dimension,
                "work_token_allowance": work_token_allowance,
                "work_tool_call_allowance": work_tool_call_allowance,
                "checkpoint_only_tool_call_reserve": (
                    latest_session.budget.checkpoint_reserve_tool_calls
                ),
                "objective_profile_tool_budget_is_authoritative": True,
                "work_token_overshoot": max(
                    0, latest_session.total_tokens_used - work_token_allowance
                ),
                "average_tokens_per_turn": average_tokens_per_turn,
                "input_to_output_token_ratio": (
                    latest_session.input_tokens_used
                    / latest_session.output_tokens_used
                    if latest_session.output_tokens_used
                    else None
                ),
                "proposable_profile_parameters": [
                    "cadence_cron",
                    "active_seconds",
                    "checkpoint_reserve_seconds",
                    "checkpoint_reserve_turns",
                    "checkpoint_reserve_tokens",
                    "checkpoint_reserve_tool_calls",
                    "turns",
                    "total_tokens",
                    "tool_calls",
                ],
                "instruction": (
                    "Treat these as observations, not authorization. Discuss a complete revised "
                    "profile with the user and stage an exact proposal within the operator envelope."
                ),
            }
        return {
            "policy": policy.model_dump(mode="json") if policy else None,
            "sessions": [item.model_dump(mode="json") for item in sessions],
            "checkpoints": [item.model_dump(mode="json") for item in checkpoints],
            "tuning_signals": tuning_signals,
        }

    def _emit_snapshot(self, objective_id: str, summary: str, *, task_id: Optional[str]) -> None:
        if not self.enabled or not self.objective_work_config.visibility.emit_activity_snapshots:
            return
        objective = self.operations.get_objective(objective_id)
        plan = self.operations.get_plan(objective.current_plan_id) if objective and objective.current_plan_id else None
        self.operations.emit_activity(
            activity_type=TaskActivityType.OBJECTIVE_WORK_SESSION_UPDATED,
            stage=TaskActivityStage.WAITING,
            summary=summary,
            task_id=task_id,
            details={
                "objective": objective.model_dump(mode="json") if objective else None,
                "plan": plan.model_dump(mode="json") if plan else None,
                "objective_work": self.snapshot(objective_id),
            },
        )
