"""Typed, staged TeamArchitect self-configuration boundary.

TeamArchitect stages an exact, digest-checked plan; trusted code applies it
through ``apply_change`` (``access_control.changes``). With authorization on a
person approves the exact plan in the Inbox (administrators; a setting owned by
one person, such as their delivery target, may also be approved by that
person), except a plan of model-choice settings only (``custom_agents.model_choice``),
which applies at once when TeamLead and its person hold model choice, and the
person gets a notice. With authorization off, the person's conversational
agreement applies it. The service never decides authority itself: ``apply`` receives the
``ChangeApproval`` the change mechanism verified and records it.
"""

from __future__ import annotations

import json
import logging
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Literal, Mapping, Optional

import yaml
from pydantic import (
    BaseModel,
    ConfigDict,
    field_validator,
)
from typing_extensions import NotRequired, TypedDict

from config_backup_retention import prune_config_backups
from config_file_updates import ConfigFileConflict, replace_config_file
from access_control.changes import ChangeApproval
from custom_agents.access_runtime import acting_agent, runtime_access
from custom_agents.agent_profile_service import AgentProfileService
from custom_agents.endpoint_switch import (
    ENDPOINT_SETTING_PATH,
    check_endpoint_change,
    check_endpoint_with_a_request,
    endpoint_change_effect,
)
from custom_agents.governance.authorized_operations import (
    FilePlanStore,
    GovernanceAuditEvent,
    atomic_write_bytes,
    canonical_json_sha256,
    model_digest,
    sha256_bytes,
)
from custom_agents.governance.runtime_adapters import LoggingGovernanceAuditSink
from custom_agents.runtime_settings_summary import summarize_runtime_settings
from custom_messages.contracts.access_v2 import ChangeApprovalMode, ChangeProposalV1
from custom_messages.contracts.runtime_v1 import (
    AUTHORITY_MODE_AUTHORIZATION_DISABLED,
    AUTHORITY_MODE_PERSON_APPROVAL,
    RESTART_SCOPE_ALL,
    RESTART_SCOPE_GATEWAY,
    RESTART_SCOPE_MANAGER,
    RESTART_SCOPE_NONE,
    TEAM_ARCHITECT_AGENT_TYPE,
)
from custom_agents.runtime_setting_values import (
    coerce_runtime_setting_value as _coerce_value,
)
from custom_agents.runtime_setting_registry import (
    CURRENT_AGENT_LLM_PATH_PREFIX,
    INSTANCE_LLM_PATH_PREFIX,
    WORKSPACE_DEFAULT_LLM_PATH_PREFIX,
    RUNTIME_SETTING_REGISTRY,
    OPERATOR_ONLY_RUNTIME_SETTINGS,
    RuntimeSettingDefinition,
    SettingDecisionClass,
    SettingTargetScope,
    SettingPersistenceTarget,
    next_turn_note,
    next_turn_targets,
)
from custom_agents.runtime_settings_llm import (
    RuntimeLLMModelCandidate,
    RuntimeLLMOverrideApplier,
)
from custom_agents import model_choice
from custom_agents.model_settings_view import model_settings_report
from custom_messages.enums import TaskHistoryImportance, TaskHistoryType
from id_generation import new_id


logger = logging.getLogger(__name__)

RUNTIME_SETTINGS_PLAN_SCHEMA_VERSION = 5
RUNTIME_SETTINGS_CHANGE_KIND = "runtime_settings"
# Long enough for a person to approve in the Inbox; the source snapshot check
# still refuses a plan whose file changed meanwhile.
RUNTIME_SETTINGS_PLAN_TTL_SECONDS = 24 * 60 * 60
RUNTIME_SETTINGS_COMPONENT = "runtime_settings_service"
_CHANGE_ID_PATTERN = re.compile(r"settings_change_(?:[0-9a-f]{12}|[0-9a-f]{20})")


class RuntimeSettingChange(TypedDict):
    path: str
    value: Any
    target_scope: NotRequired[str]
    target_agent_type: NotRequired[str]


class RuntimeSettingPlanChange(BaseModel):
    model_config = ConfigDict(extra="forbid")

    path: str
    actual_path: str
    value: Any
    previous_value: Any = None
    previous_value_hash: str
    value_type: str
    persistence_target: SettingPersistenceTarget
    target_scope: SettingTargetScope
    target_agent_type: Optional[str] = None
    decision_class: SettingDecisionClass
    agreement_required: bool
    restart_scope: str
    summary: str


class RuntimeSettingSourceSnapshot(BaseModel):
    model_config = ConfigDict(extra="forbid")

    source_kind: str
    path: str
    existed: bool
    sha256: str


class RuntimeSettingsChangePlan(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: int = RUNTIME_SETTINGS_PLAN_SCHEMA_VERSION
    change_id: str
    plan_digest: str = ""
    created_at_utc: datetime
    expires_at_utc: datetime
    actor_principal_id: str
    actor_named_agent_type: str
    team_id: str
    owner_canonical_user_id: str | None = None
    reason: str = ""
    decision_class: SettingDecisionClass
    # person_approval: a person approves the exact plan (authorization on);
    # authorization_disabled_agreement: conversational agreement applies it.
    authority_mode: Literal["person_approval", "authorization_disabled_agreement"]
    agreement_required: bool
    restart_scope: str
    requires_restart: bool
    changes: list[RuntimeSettingPlanChange]
    source_snapshots: list[RuntimeSettingSourceSnapshot]

    @field_validator("schema_version")
    @classmethod
    def _validate_schema_version(cls, value: int) -> int:
        if int(value) != RUNTIME_SETTINGS_PLAN_SCHEMA_VERSION:
            raise ValueError(
                f"Only runtime settings plan schema {RUNTIME_SETTINGS_PLAN_SCHEMA_VERSION} is supported."
            )
        return int(value)

    @field_validator("change_id")
    @classmethod
    def _validate_change_id(cls, value: str) -> str:
        normalized = str(value or "").strip()
        if not _CHANGE_ID_PATTERN.fullmatch(normalized):
            raise ValueError("Invalid runtime settings change id.")
        return normalized



def _shown(value: Any) -> str:
    """A value as a person reads it in a review (long text shortened)."""

    text = json.dumps(value, ensure_ascii=False, default=str) if not isinstance(value, str) else repr(value)
    return text if len(text) <= 300 else text[:297] + "..."


_RESTART_ORDER = {
    RESTART_SCOPE_NONE: 0,
    RESTART_SCOPE_MANAGER: 1,
    RESTART_SCOPE_GATEWAY: 1,
    RESTART_SCOPE_ALL: 2,
}
_DECISION_ORDER = {
    SettingDecisionClass.LEAD_MANAGED: 0,
    SettingDecisionClass.USER_AUTHORIZED: 1,
    SettingDecisionClass.OPERATOR_ONLY: 2,
}


def runtime_settings_options_payload() -> dict[str, Any]:
    return {
        "agent_controllable": [
            {"path": path, **definition.public_metadata()}
            for path, definition in sorted(RUNTIME_SETTING_REGISTRY.items())
        ],
        "operator_only": [
            {
                "path": path,
                "decision_class": SettingDecisionClass.OPERATOR_ONLY.value,
                "reason": reason,
            }
            for path, reason in sorted(OPERATOR_ONLY_RUNTIME_SETTINGS.items())
        ],
        "decision_classes": {
            SettingDecisionClass.LEAD_MANAGED.value: (
                "TeamArchitect may apply the bounded change after conversational user agreement. "
                "Agreement is an interaction requirement, not an authorization credential."
            ),
            SettingDecisionClass.USER_AUTHORIZED.value: (
                "With authorization enabled, the plan needs an authenticated one-time gateway "
                "decision. With authorization disabled, the same bounded change uses explicit "
                "conversational agreement and records no authorization provenance."
            ),
            SettingDecisionClass.OPERATOR_ONLY.value: (
                "Agents cannot stage or apply the setting. Change it through operator-owned configuration."
            ),
        },
        "llm_precedence": [
            {
                "path_prefix": "llm.*",
                "source": "instance_config.yaml llm",
                "role": "team baseline",
            },
            {
                "path_prefix": "workspace_default.llm.*",
                "source": "LLM_OVERRIDES.yaml default",
                "role": "workspace default that overrides the instance baseline",
            },
            {
                "path_prefix": "current_agent.llm.*",
                "source": "LLM_OVERRIDES.yaml agent_types.<AgentType>",
                "role": "self or exact named-type override that overrides the workspace default",
            },
        ],
        "workflow": [
            "Stage one or more changes and review old/new values, target scope, digest, and expiry.",
            "For lead_managed changes, ask for conversational user agreement before apply.",
            "Follow the plan's immutable authority_mode: authenticated gateway decision when enabled, conversational agreement when disabled.",
            "Apply before expiry. Model-affecting baseline and workspace changes are tested at staging and again before any write; failed tests preserve both live and persisted profiles.",
            "Model settings stored in LLM_OVERRIDES.yaml need no restart. A change explicitly targeting the applying agent's own type switches that agent at once. Workspace-default changes and changes for other types reach affected running agents at their next turn, including the applying agent when it inherits the default. Until then their status shows the previous values; the apply result's running_agents says so. Check saved values with get_runtime_configuration_context(agent_types=[...]).",
            "A stale source snapshot, modified plan, replay, or changed actor is rejected.",
        ],
    }


def operator_only_runtime_setting_reason(path: str) -> str | None:
    for pattern, reason in OPERATOR_ONLY_RUNTIME_SETTINGS.items():
        if pattern.endswith(".*") and str(path).startswith(pattern[:-1]):
            return reason
        if "*" in pattern:
            prefix, suffix = pattern.split("*", 1)
            if str(path).startswith(prefix) and str(path).endswith(suffix):
                return reason
        elif path == pattern:
            return reason
    return None


def _source_snapshot(path: Path, source_kind: str) -> RuntimeSettingSourceSnapshot:
    existed = path.exists()
    content = path.read_bytes() if existed else b""
    return RuntimeSettingSourceSnapshot(
        source_kind=source_kind,
        path=str(path),
        existed=existed,
        sha256=sha256_bytes(content),
    )


def _combine_restart_scope(current: str, new: str) -> str:
    if current == new:
        return current
    if {current, new} == {RESTART_SCOPE_MANAGER, RESTART_SCOPE_GATEWAY}:
        return RESTART_SCOPE_ALL
    return (
        new if _RESTART_ORDER.get(new, 0) > _RESTART_ORDER.get(current, 0) else current
    )


class RuntimeSettingsService:
    """Stage and apply bounded runtime/team changes for TeamArchitect."""

    def __init__(self, agent: Any) -> None:
        self.agent = agent
        self.audit = LoggingGovernanceAuditSink()

    @staticmethod
    def _plan_store(config: Any) -> FilePlanStore[RuntimeSettingsChangePlan]:
        return FilePlanStore(
            RuntimeSettingsService._plan_dir(config),
            RuntimeSettingsChangePlan,
            change_id_pattern=_CHANGE_ID_PATTERN,
            invalid_change_id_message="Invalid runtime settings change id.",
            missing_plan_message="Runtime settings change plan not found: {change_id}",
        )

    def stage(
        self,
        changes: list[RuntimeSettingChange],
        *,
        reason: str = "",
        target_scope: str = "",
        target_agent_type: str = "",
    ) -> dict[str, Any]:
        if not changes:
            raise ValueError("At least one change is required.")
        if not bool(getattr(self.agent, "is_team_lead", False)):
            raise PermissionError(
                "Only TeamArchitect may stage team runtime settings changes."
            )
        config = self._load_canonical_config()
        actor = acting_agent(self.agent)
        now = datetime.now(timezone.utc)
        normalized_changes: list[RuntimeSettingPlanChange] = []
        snapshots: dict[str, RuntimeSettingSourceSnapshot] = {}
        restart_scope = RESTART_SCOPE_NONE
        decision_class = SettingDecisionClass.LEAD_MANAGED
        seen_targets: set[tuple[str, str | None, str]] = set()

        for raw_change in changes:
            if not isinstance(raw_change, dict):
                raise ValueError(
                    "Each runtime setting change must be an object with 'path' and 'value' fields."
                )
            path = str(raw_change.get("path") or "").strip()
            if not path:
                raise ValueError(
                    "Each runtime setting change must include a non-empty 'path' field."
                )
            definition = RUNTIME_SETTING_REGISTRY.get(path)
            if definition is None:
                operator_reason = operator_only_runtime_setting_reason(path)
                if operator_reason:
                    raise ValueError(
                        f"Runtime setting {path!r} is operator-only: {operator_reason}"
                    )
                raise ValueError(f"Runtime setting {path!r} is not agent-controllable.")
            value = _coerce_value(raw_change.get("value"), definition)
            change_target_scope = str(
                raw_change.get("target_scope", target_scope) or ""
            ).strip()
            change_target_agent_type = str(
                raw_change.get("target_agent_type", target_agent_type) or ""
            ).strip()
            resolved_scope, resolved_agent_type = self._resolve_target(
                definition,
                requested_scope=change_target_scope,
                requested_agent_type=change_target_agent_type,
            )
            # Model settings in LLM_OVERRIDES.yaml need no restart for any
            # agent type. Every agent reads it at the start of each turn
            # (``refresh_persistent_llm_override``), and new agents read it at
            # start. Settings in instance_config.yaml are read only at start.
            change_restart_scope = definition.restart_scope
            source_path, source_kind = self._source_for_change(
                config,
                definition,
                target_agent_type=resolved_agent_type,
            )
            actual_path = self._change_actual_path(config, definition, resolved_scope, resolved_agent_type)
            snapshot = snapshots.setdefault(
                str(source_path),
                _source_snapshot(source_path, source_kind),
            )
            _ = snapshot
            previous_value = self._read_previous_value(
                config,
                definition,
                actual_path=actual_path,
                source_path=source_path,
                target_agent_type=resolved_agent_type,
            )
            from runtime_settings.schedule_workers import SCHEDULE_WORKER_ELIGIBILITY_PATH

            if path == ENDPOINT_SETTING_PATH:
                # Stages the form of the address that answered a test (with or without /v1).
                value = check_endpoint_change(self.agent, config, value)
            if path == SCHEDULE_WORKER_ELIGIBILITY_PATH:
                from custom_agents.schedule_worker_settings import validate_worker_eligibility_change

                validate_worker_eligibility_change(self.agent, config.agent.schedule_workers, value)
            target_key = (path, resolved_agent_type, actual_path)
            if target_key in seen_targets:
                raise ValueError(
                    f"Duplicate runtime setting target in one plan: {path!r}."
                )
            seen_targets.add(target_key)
            normalized_changes.append(
                RuntimeSettingPlanChange(
                    path=path,
                    actual_path=actual_path,
                    value=value,
                    previous_value=previous_value,
                    previous_value_hash=canonical_json_sha256(previous_value),
                    value_type=definition.value_type,
                    persistence_target=definition.persistence_target,
                    target_scope=resolved_scope,
                    target_agent_type=resolved_agent_type,
                    decision_class=definition.decision_class,
                    agreement_required=definition.agreement_required,
                    restart_scope=change_restart_scope,
                    summary=definition.summary,
                )
            )
            restart_scope = _combine_restart_scope(restart_scope, change_restart_scope)
            if (
                _DECISION_ORDER[definition.decision_class]
                > _DECISION_ORDER[decision_class]
            ):
                decision_class = definition.decision_class

        if len(snapshots) != 1:
            raise ValueError(
                "Stage 1 settings plans must target one persisted source so apply remains atomic. "
                "Create separate plans for changes stored in different files or agent definitions."
            )
        self._check_one_target(normalized_changes)
        self._check_providers(config, normalized_changes)
        change_id = new_id("settings_change")
        candidates = self._model_candidates(config, normalized_changes, snapshots)
        model_test = self._test_model(candidates, change_id=change_id) or self._test_tool_limit(config, normalized_changes)

        plan = RuntimeSettingsChangePlan(
            change_id=change_id,
            created_at_utc=now,
            expires_at_utc=now + timedelta(seconds=RUNTIME_SETTINGS_PLAN_TTL_SECONDS),
            actor_principal_id=actor.agent.agent_id,
            actor_named_agent_type=actor.agent.agent_type or TEAM_ARCHITECT_AGENT_TYPE,
            team_id=actor.agent.team_id or str(getattr(self.agent, "team_id", "") or ""),
            owner_canonical_user_id=actor.person_id,
            reason=str(reason or "").strip(),
            decision_class=decision_class,
            authority_mode=self._authority_mode(),
            agreement_required=bool(model_test and model_test.get("tested_before_write")) or any(
                change.agreement_required for change in normalized_changes
            ),
            restart_scope=restart_scope,
            requires_restart=restart_scope != RESTART_SCOPE_NONE,
            changes=normalized_changes,
            source_snapshots=list(snapshots.values()),
        )
        plan.plan_digest = model_digest(plan)
        plan_path = self._plan_store(config).save(plan)
        self.audit.record(
            GovernanceAuditEvent(
                operation="runtime_settings",
                phase="staged",
                change_id=plan.change_id,
                plan_digest=plan.plan_digest,
                task_id=actor.task_id or "",
                authority_mode=plan.authority_mode,
            )
        )
        payload = plan.model_dump(mode="json")
        context_window_notice = self._context_window_notice(candidates)
        instructions = self._plan_instructions(plan, model_test)
        payload.update(
            {
                "plan_path": str(plan_path),
                "instructions": f"{context_window_notice} {instructions}" if context_window_notice else instructions,
            }
        )
        if model_test is not None:
            payload["model_test"] = model_test
        if context_window_notice:
            payload["context_window_notice"] = context_window_notice
        return payload

    @staticmethod
    def _context_window_notice(candidates: list[RuntimeLLMModelCandidate]) -> str:
        """Say which window the runtime will assume when a switch sets none.

        An override switch resets the old model's context window
        (``model_runtime_profile.stale_model_fields``); the switch sets the new
        model's window from the capability lookup (``custom_agents.model_choice``).
        Staging still accepts a switch without one, for models no source knows,
        but the plan says plainly which window will apply.
        """

        from custom_agents.strands_model_factory import (
            known_context_window_tokens,
            resolve_context_window_tokens,
        )

        notices = []
        for candidate in candidates:
            prefix = next((
                prefix for prefix in (CURRENT_AGENT_LLM_PATH_PREFIX, WORKSPACE_DEFAULT_LLM_PATH_PREFIX, INSTANCE_LLM_PATH_PREFIX)
                if candidate.paths & {f"{prefix}model_id", f"{prefix}provider"}
            ), None)
            if prefix is None or candidate.profile.context_window_tokens:
                continue
            model_id = candidate.profile.model_id
            provider = candidate.profile.provider
            if known_context_window_tokens(provider=provider, model_id=model_id) is not None:
                continue
            assumed = resolve_context_window_tokens(provider=provider, model_id=model_id)
            notices.append(
                f"This switch sets no context window for {candidate.target if candidate.target != 'default' else 'the team default'}, so the runtime "
                f"will assume {assumed:,} tokens for {model_id}. Stage {prefix}context_window_tokens with the "
                "window lookup_model_capability_candidates reports, or ask the person when no source knows it."
            )
        return " ".join(notices)

    def _model_candidates(
        self,
        config: Any,
        changes: list[RuntimeSettingPlanChange],
        snapshots: Mapping[str, RuntimeSettingSourceSnapshot],
    ) -> list[RuntimeLLMModelCandidate]:
        target = changes[0].persistence_target
        if target == SettingPersistenceTarget.WORKSPACE_LLM_OVERRIDES:
            return RuntimeLLMOverrideApplier(self.agent).prepare_candidates(config, Path(next(iter(snapshots))), changes)
        if target == SettingPersistenceTarget.INSTANCE_CONFIG:
            candidate_config = self._instance_config_candidate(config, changes)
            return RuntimeLLMOverrideApplier.instance_candidates(candidate_config, changes)
        return []

    def _test_model(
        self,
        candidates: list[RuntimeLLMModelCandidate],
        *,
        change_id: str,
    ) -> dict[str, Any] | None:
        """Try each target model once before its plan is staged.

        The person is then asked to confirm a model and settings that already
        answered; a failed test stages nothing and its error tells the agent
        what to change. Plans that change no model setting return None.
        """

        if not candidates:
            return None
        preflight, failure = RuntimeLLMOverrideApplier(self.agent).test_candidates(candidates, change_id=change_id)
        if failure is not None:
            candidate = failure.get("candidate") or {}
            raise ValueError(
                f"The model {candidate.get('model_id')} on {candidate.get('provider')} did not answer its test "
                f"call, so nothing was staged and nothing changed: {failure.get('error')}. Tell the person the "
                "error, and stage again only with settings they agree to try."
            )
        return preflight

    def _check_providers(self, config: Any, changes: list[RuntimeSettingPlanChange]) -> None:
        """With authorization on, a provider change names a provider this instance is set up for.

        See ``custom_agents.model_choice``; with authorization off the
        preflight model call still tests the candidate before any write.
        """

        if self._authority_mode() != AUTHORITY_MODE_PERSON_APPROVAL:
            return
        if not any(change.path == model_choice.PROVIDER_PATH for change in changes):
            return
        usable = model_choice.usable_providers(self.agent, config, self._llm_override_path(config))
        model_choice.refuse_unusable_provider(changes, usable)

    def load_plan(self, change_id: str) -> RuntimeSettingsChangePlan:
        """The stored plan, checked: unchanged, unexpired, same mode, same actor and person."""

        if not bool(getattr(self.agent, "is_team_lead", False)):
            raise PermissionError("Only TeamArchitect may apply team runtime settings changes.")
        config = self._load_canonical_config()
        plan = self._plan_store(config).load(str(change_id or "").strip())
        if plan.plan_digest != model_digest(plan):
            raise PermissionError("Runtime settings change plan digest mismatch.")
        if plan.expires_at_utc <= datetime.now(timezone.utc):
            raise PermissionError("Runtime settings change plan has expired. Stage a fresh plan.")
        if self._authority_mode() != plan.authority_mode:
            raise PermissionError("Authorization was turned on or off after planning. Stage a fresh plan.")
        actor = acting_agent(self.agent)
        if (
            plan.actor_principal_id != actor.agent.agent_id
            or plan.team_id != actor.agent.team_id
            or plan.actor_named_agent_type != actor.agent.agent_type
            or plan.owner_canonical_user_id != actor.person_id
        ):
            raise PermissionError("Runtime settings change plan actor, team or person no longer matches.")
        return plan

    def _verify_current(self, config: Any, plan: RuntimeSettingsChangePlan) -> None:
        """The plan can still apply: not applied yet, and the settings files unchanged since planning.

        Runs before a person is asked (``describe``) and again when applying,
        so nobody approves a change that can only fail.
        """

        if (self._receipt_dir(config) / f"{plan.change_id}.json").exists():
            raise FileExistsError(f"Runtime settings change {plan.change_id} was already applied.")
        self._verify_sources(plan)

    def describe(self, change_id: str) -> ChangeProposalV1:
        """The exact effect a person approves (access_control.changes)."""

        plan = self.load_plan(change_id)
        self._verify_current(self._load_canonical_config(), plan)
        next_turn = next_turn_targets(plan.changes, self._current_agent_type())
        summary = summarize_runtime_settings(plan.changes, plan.restart_scope, next_turn=bool(next_turn))
        effect = [
            f"{change.summary}: {change.path}"
            + (f" for {change.target_agent_type}" if change.target_agent_type else "")
            + f" changes from {_shown(change.previous_value)} to {_shown(change.value)}."
            for change in plan.changes
        ]
        if plan.changes[0].persistence_target == SettingPersistenceTarget.MODEL_CAPABILITY:
            from custom_agents.model_tool_limit import describe_raise

            # The cap belongs to a model, not to the asking agent type: name the model and who it affects.
            summary, effect = describe_raise(plan.changes[0])
        effect.extend(
            endpoint_change_effect(str(change.value)) for change in plan.changes if change.path == ENDPOINT_SETTING_PATH
        )
        if plan.requires_restart:
            effect.append(f"Takes effect after a restart ({plan.restart_scope}); the restart is a separate approval.")
        if next_turn:
            effect.append(next_turn_note(next_turn))
        if plan.reason:
            effect.append(f"Reason: {plan.reason}")
        paths = ", ".join(change.path for change in plan.changes)
        # A plan of model-choice settings only is its own change kind, which
        # applies at once for people who hold model choice (custom_agents.model_choice).
        kind = (
            model_choice.MODEL_CHOICE_CHANGE_KIND
            if model_choice.is_model_choice(plan.changes)
            else RUNTIME_SETTINGS_CHANGE_KIND
        )
        return ChangeProposalV1(
            kind=kind,
            change_id=plan.change_id,
            plan_digest=plan.plan_digest,
            title=f"Change runtime settings: {paths}"[:400],
            summary=summary,
            effect=tuple(line[:1000] for line in effect[:40]),
            owner_person_id=(
                plan.owner_canonical_user_id
                if plan.decision_class == SettingDecisionClass.USER_AUTHORIZED
                else None
            ),
            agreement_required=plan.agreement_required,
        )

    def apply(self, change_id: str, approval: ChangeApproval) -> dict[str, Any]:
        """Apply a staged plan that the change mechanism approved (never call it otherwise)."""

        config = self._load_canonical_config()
        normalized_change_id = str(change_id or "").strip()
        receipt_path = self._receipt_dir(config) / f"{normalized_change_id}.json"
        if receipt_path.exists():
            receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
            return {
                "error": "Runtime settings change was already applied.",
                "reason_code": "settings_change_replay",
                "change_id": normalized_change_id,
                "receipt_path": str(receipt_path),
                "applied_at_utc": receipt.get("applied_at_utc"),
            }
        plan = self.load_plan(normalized_change_id)
        agreement = approval.mode is ChangeApprovalMode.AGREEMENT
        if agreement != (plan.authority_mode == AUTHORITY_MODE_AUTHORIZATION_DISABLED):
            raise PermissionError("This plan needs a person's approval, not agreement (or the reverse).")
        actor = acting_agent(self.agent)
        self._verify_current(config, plan)
        result = self._apply_plan(config, plan)
        if result.get("error"):
            return result

        applied_at = datetime.now(timezone.utc)
        receipt = {
            "schema_version": RUNTIME_SETTINGS_PLAN_SCHEMA_VERSION,
            "receipt_id": new_id("set_rcpt"),
            "change_id": plan.change_id,
            "plan_digest": plan.plan_digest,
            "applied_at_utc": applied_at.isoformat(),
            "actor_principal_id": actor.agent.agent_id,
            "actor_named_agent_type": actor.agent.agent_type,
            "team_id": actor.agent.team_id,
            "decision_class": plan.decision_class.value,
            "authority_mode": plan.authority_mode,
            "approval": approval.record(),
            "preflight": result.get("preflight"),
            "persistence_verification": result.get("persistence_verification"),
            "persistence_target": result["persistence_target"],
            "saved_path": result["saved_path"],
            "backup_path": result["backup_path"],
            "previous_sha256": result["previous_sha256"],
            "sha256": result["sha256"],
            "undo": {
                "kind": "restore_backup",
                "backup_path": result["backup_path"],
                "target_path": result["saved_path"],
                "expected_current_sha256": result["sha256"],
                "automatic_undo_available": False,
            },
            "receipt_path": str(receipt_path),
        }
        try:
            atomic_write_bytes(
                receipt_path,
                json.dumps(receipt, indent=2, sort_keys=True).encode("utf-8"),
                immutable=True,
            )
        except Exception:
            self._undo_unrecorded(result)
            raise

        self._append_history(plan, receipt)
        self.audit.record(
            GovernanceAuditEvent(
                operation="runtime_settings",
                phase="applied",
                change_id=plan.change_id,
                plan_digest=plan.plan_digest,
                task_id=actor.task_id or "",
                authority_mode=plan.authority_mode,
            )
        )
        response = {
            "status": "applied",
            "change_id": plan.change_id,
            "plan_digest": plan.plan_digest,
            "decision_class": plan.decision_class.value,
            "authority_mode": plan.authority_mode,
            "approval": approval.record(),
            "persistence_target": result["persistence_target"],
            "saved_path": result["saved_path"],
            "backup_path": result["backup_path"],
            "receipt_path": str(receipt_path),
            "undo": receipt["undo"],
            "live_applied": result.get("live_applied", []),
            "requires_restart": plan.requires_restart,
            "restart_scope": plan.restart_scope,
            "restart_next_step": (
                "Stage a restart with plan_runtime_restart and apply it with apply_change; "
                "a person approves the restart separately."
                if plan.requires_restart
                else None
            ),
            "capability_follow_up": result.get("capability_follow_up"),
            "preflight": result.get("preflight"),
            "persistence_verification": result.get("persistence_verification"),
        }
        next_turn = next_turn_targets(plan.changes, self._current_agent_type())
        if next_turn:
            response["running_agents"] = next_turn_note(next_turn)
        if model_choice.is_model_choice(plan.changes):
            response["message"] = model_choice.applied_message(plan.changes, self._current_agent_type())
            self._tell_person_about_model_choice(plan, approval)
        model_settings = self._model_settings_reports(plan)
        if model_settings:
            response["model_settings"] = model_settings
        if "llm_override" in result:
            response["llm_override"] = result["llm_override"]
        if "model_tool_limit" in result:
            response["model_tool_limit"] = result["model_tool_limit"]
        if "workspace_default_llm_override" in result:
            response["workspace_default_llm_override"] = result[
                "workspace_default_llm_override"
            ]
        return response

    def _current_agent_type(self) -> str:
        return str(
            getattr(self.agent, "named_agent_type", None)
            or getattr(self.agent, "name", None)
            or TEAM_ARCHITECT_AGENT_TYPE
        ).strip()

    def _model_settings_reports(self, plan: RuntimeSettingsChangePlan) -> list[dict[str, Any]]:
        """What each agent type whose model settings changed now runs with (``model_settings_view``).

        Read after the change is saved, through the same effective-value
        lookup as the plan's previous values. The change is already applied,
        so a failure here is logged and leaves the table out.
        """

        agent_types = list(
            dict.fromkeys(
                change.target_agent_type or self._current_agent_type()
                for change in plan.changes
                if change.path.startswith(CURRENT_AGENT_LLM_PATH_PREFIX)
            )
        )
        if not agent_types:
            return []
        try:
            config = self._load_canonical_config()
            payload = self._read_yaml_mapping(self._llm_override_path(config))
            return [
                model_settings_report(
                    agent_type,
                    lambda field, agent_type=agent_type: self._effective_previous_llm_value(
                        config, payload, field_name=field, target_agent_type=agent_type
                    ),
                )
                for agent_type in agent_types
            ]
        except Exception:  # noqa: BLE001 - see docstring
            logger.exception(
                "Could not read the model settings after a change. event=model_settings_report_failed change_id=%s",
                plan.change_id,
            )
            return []

    def _tell_person_about_model_choice(self, plan: RuntimeSettingsChangePlan, approval: ChangeApproval) -> None:
        """Nobody reviewed a model choice applied with held access, so its person gets a notice.

        The change is already applied and stays applied; a failure here is logged only.
        """

        if approval.mode is not ChangeApprovalMode.HELD_ACCESS:
            return
        try:
            model_choice.tell_person(
                self.agent,
                person_id=plan.owner_canonical_user_id,
                actor_type=plan.actor_named_agent_type,
                changes=plan.changes,
            )
        except Exception:  # noqa: BLE001 - see docstring
            logger.exception(
                "Could not tell the person about a model change. event=model_choice_notice_failed change_id=%s",
                plan.change_id,
            )

    def _authority_mode(self) -> str:
        return (
            AUTHORITY_MODE_PERSON_APPROVAL
            if runtime_access(self.agent).enabled
            else AUTHORITY_MODE_AUTHORIZATION_DISABLED
        )

    def _load_canonical_config(self):
        from assistant_gateway.config import GatewayInstanceConfig

        configured_path = str(
            getattr(self.agent, "instance_config_path", "") or ""
        ).strip()
        if not configured_path:
            raise ValueError(
                "No instance_config.yaml path is available for runtime settings changes."
            )
        return GatewayInstanceConfig.load_saved(configured_path)

    @staticmethod
    def _plan_dir(config: Any) -> Path:
        path = config.instance_root_path / "runtime" / "settings_change_plans"
        path.mkdir(parents=True, exist_ok=True)
        return path

    @staticmethod
    def _receipt_dir(config: Any) -> Path:
        path = config.instance_root_path / "runtime" / "settings_change_receipts"
        path.mkdir(parents=True, exist_ok=True)
        return path

    def _resolve_target(
        self,
        definition: RuntimeSettingDefinition,
        *,
        requested_scope: str,
        requested_agent_type: str,
    ) -> tuple[SettingTargetScope, str | None]:
        normalized_scope = str(requested_scope or "").strip().lower()
        normalized_agent_type = str(requested_agent_type or "").strip()
        if normalized_scope:
            try:
                scope = SettingTargetScope(normalized_scope)
            except ValueError as exc:
                raise ValueError(
                    "target_scope must be self, agent_type, or team."
                ) from exc
        elif normalized_agent_type:
            scope = SettingTargetScope.AGENT_TYPE
        else:
            scope = definition.default_target_scope
        if scope not in definition.allowed_target_scopes:
            allowed = ", ".join(item.value for item in definition.allowed_target_scopes)
            raise ValueError(
                f"Runtime setting {definition.path!r} supports target scopes: {allowed}."
            )

        current_type = self._current_agent_type()
        if scope == SettingTargetScope.SELF:
            if normalized_agent_type and normalized_agent_type != current_type:
                raise ValueError(
                    "target_scope=self cannot name a different agent type."
                )
            return scope, current_type
        if scope == SettingTargetScope.AGENT_TYPE:
            if not normalized_agent_type:
                raise ValueError(
                    "target_agent_type is required when target_scope=agent_type."
                )
            from custom_agents.team_composition import validate_agent_type_name

            return scope, validate_agent_type_name(normalized_agent_type)
        if normalized_agent_type:
            raise ValueError("target_agent_type must be empty when target_scope=team.")
        return scope, None

    @staticmethod
    def _actual_path(
        definition: RuntimeSettingDefinition,
        scope: SettingTargetScope,
        target_agent_type: str | None,
    ) -> str:
        if definition.path.startswith("agent.schedule_worker_rules."):
            return f"agent_types.{target_agent_type}.{definition.path}"
        if definition.path.startswith(CURRENT_AGENT_LLM_PATH_PREFIX):
            field_name = definition.path.removeprefix(CURRENT_AGENT_LLM_PATH_PREFIX)
            if (
                definition.persistence_target
                == SettingPersistenceTarget.INSTANCE_CONFIG
            ):
                return f"agent_types.{target_agent_type}.llm.{field_name}"
            return f"agent_types.{target_agent_type}.{field_name}"
        if definition.path.startswith(WORKSPACE_DEFAULT_LLM_PATH_PREFIX):
            field_name = definition.path.removeprefix(WORKSPACE_DEFAULT_LLM_PATH_PREFIX)
            return f"default.{field_name}"
        if (
            definition.persistence_target
            == SettingPersistenceTarget.GENERATED_AGENT_DEFINITION
        ):
            return f"generated_agent_types.{target_agent_type}.{definition.path.removeprefix('specialist.')}"
        if definition.persistence_target == SettingPersistenceTarget.TEAM_COMPOSITION:
            suffix = (
                f".{target_agent_type}"
                if scope == SettingTargetScope.AGENT_TYPE
                else ""
            )
            return f"{definition.path}{suffix}"
        return definition.path

    @staticmethod
    def _check_one_target(changes: list[RuntimeSettingPlanChange]) -> None:
        """One plan, one way of applying: the model profile and a model's tool cap share LLM_OVERRIDES.yaml.

        A tool cap plan also names one model, so each raise is tested and approved on its own.
        """

        if len({change.persistence_target for change in changes}) != 1 or (
            changes[0].persistence_target == SettingPersistenceTarget.MODEL_CAPABILITY and len(changes) != 1
        ):
            raise ValueError(
                "Stage a model's tool cap (model.max_tools_per_request) in its own plan, for one model, "
                "without other settings."
            )

    def _test_tool_limit(self, config: Any, changes: list[RuntimeSettingPlanChange]) -> dict[str, Any] | None:
        """A tool cap raise is checked and tested once before it is staged (``custom_agents.model_tool_limit``)."""

        if changes[0].persistence_target != SettingPersistenceTarget.MODEL_CAPABILITY:
            return None
        from custom_agents.model_tool_limit import check_and_test

        return check_and_test(self.agent, config, self._llm_override_path(config), changes[0])

    def _change_actual_path(
        self,
        config: Any,
        definition: RuntimeSettingDefinition,
        scope: SettingTargetScope,
        target_agent_type: str | None,
    ) -> str:
        if definition.persistence_target == SettingPersistenceTarget.MODEL_CAPABILITY:
            from custom_agents.model_tool_limit import target_model

            # Where the cap of the target's model is stored (custom_agents.model_tool_limit).
            model, _profile = target_model(self.agent, config, self._llm_override_path(config), target_agent_type)
            return model.actual_path
        return self._actual_path(definition, scope, target_agent_type)

    def _source_for_change(
        self,
        config: Any,
        definition: RuntimeSettingDefinition,
        *,
        target_agent_type: str | None,
    ) -> tuple[Path, str]:
        if definition.persistence_target == SettingPersistenceTarget.INSTANCE_CONFIG:
            return config.config_path, definition.persistence_target.value
        if definition.persistence_target in (
            SettingPersistenceTarget.WORKSPACE_LLM_OVERRIDES,
            SettingPersistenceTarget.MODEL_CAPABILITY,
        ):
            return self._llm_override_path(config), definition.persistence_target.value
        if definition.persistence_target == SettingPersistenceTarget.TEAM_LEAD_PROFILE:
            agent_root_raw = str(getattr(self.agent, "instance_root", "") or "").strip()
            if not agent_root_raw:
                raise ValueError("TeamArchitect instance_root is not configured.")
            if Path(agent_root_raw).expanduser().resolve() != config.instance_root_path:
                raise ValueError(
                    "TeamArchitect instance_root does not match the canonical instance; "
                    "refusing to stage a profile change."
                )
            return config.layout.soul_path, definition.persistence_target.value
        if definition.persistence_target == SettingPersistenceTarget.TEAM_COMPOSITION:
            return self._team_composition_path(
                config
            ), definition.persistence_target.value
        if (
            definition.persistence_target
            == SettingPersistenceTarget.GENERATED_AGENT_DEFINITION
        ):
            if not target_agent_type:
                raise ValueError(
                    "Generated specialist settings require target_agent_type."
                )
            from custom_agents.team_composition import (
                generated_agent_bundle_path,
                load_team_composition,
            )

            composition = load_team_composition(self._team_composition_path(config))
            return (
                generated_agent_bundle_path(
                    control_dir=config.layout.control_dir,
                    config=composition,
                    agent_type=target_agent_type,
                )
                / "agent.yaml",
                definition.persistence_target.value,
            )
        raise ValueError(
            f"Unsupported persistence target {definition.persistence_target.value}."
        )

    def _llm_override_path(self, config: Any) -> Path:
        from config_loader import resolve_llm_override_path

        control = config.layout.control_dir
        resolved = resolve_llm_override_path(
            instance_root=str(config.instance_root_path),
            explicit_override_path=getattr(self.agent, "llm_override_path", None),
        )
        if control not in resolved.parents:
            raise ValueError(
                f"LLM override path {resolved} is outside the control folder {control}."
            )
        return resolved

    def _team_composition_path(self, config: Any) -> Path:
        from custom_agents.team_composition import resolve_team_composition_path

        control = config.layout.control_dir
        resolved = resolve_team_composition_path(
            control_dir=control,
            explicit_path=getattr(self.agent, "team_composition_path", None),
        )
        if control not in resolved.parents:
            raise ValueError(
                f"Team composition path {resolved} is outside the control folder {control}."
            )
        return resolved

    def _read_previous_value(
        self,
        config: Any,
        definition: RuntimeSettingDefinition,
        *,
        actual_path: str,
        source_path: Path,
        target_agent_type: str | None,
    ) -> Any:
        if definition.persistence_target == SettingPersistenceTarget.INSTANCE_CONFIG:
            return self._get_config_value(config, actual_path)
        if definition.persistence_target == SettingPersistenceTarget.MODEL_CAPABILITY:
            from custom_agents.model_tool_limit import effective_tool_limit, model_from_path

            return effective_tool_limit(config, source_path, model_from_path(actual_path))
        if (
            definition.persistence_target
            == SettingPersistenceTarget.WORKSPACE_LLM_OVERRIDES
        ):
            payload = self._read_yaml_mapping(source_path)
            if definition.path.startswith(WORKSPACE_DEFAULT_LLM_PATH_PREFIX):
                field_name = definition.path.removeprefix(
                    WORKSPACE_DEFAULT_LLM_PATH_PREFIX
                )
                default_entry = payload.get("default")
                if isinstance(default_entry, Mapping):
                    if field_name == "provider" and "model_provider" in default_entry:
                        return default_entry.get("provider") or default_entry.get(
                            "model_provider"
                        )
                    if field_name in default_entry:
                        return default_entry.get(field_name)
                return getattr(config.llm, field_name, None)
            field_name = definition.path.removeprefix(CURRENT_AGENT_LLM_PATH_PREFIX)
            return self._effective_previous_llm_value(
                config,
                payload,
                field_name=field_name,
                target_agent_type=target_agent_type or "",
            )
        if definition.persistence_target == SettingPersistenceTarget.TEAM_LEAD_PROFILE:
            return (
                source_path.read_text(encoding="utf-8") if source_path.exists() else ""
            )
        if (
            definition.persistence_target
            == SettingPersistenceTarget.GENERATED_AGENT_DEFINITION
        ):
            payload = self._read_yaml_mapping(source_path, must_exist=True)
            entry = (payload.get("agent_types") or {}).get(
                target_agent_type or ""
            ) or {}
            field_name = definition.path.removeprefix("specialist.")
            params = (
                entry.get("params") if isinstance(entry.get("params"), dict) else {}
            )
            overrides = (
                entry.get("overrides")
                if isinstance(entry.get("overrides"), dict)
                else {}
            )
            if field_name in overrides:
                return overrides.get(field_name)
            if field_name in params:
                return params.get(field_name)
            return None
        if definition.persistence_target == SettingPersistenceTarget.TEAM_COMPOSITION:
            from custom_agents.team_composition import load_team_composition

            composition = load_team_composition(source_path)
            return self._get_composition_value(
                composition,
                definition.path,
                target_agent_type=target_agent_type,
            )
        raise ValueError(
            f"Unsupported persistence target {definition.persistence_target.value}."
        )

    def _effective_previous_llm_value(
        self,
        config: Any,
        payload: Mapping[str, Any],
        *,
        field_name: str,
        target_agent_type: str,
    ) -> Any:
        agent_types = payload.get("agent_types")
        entry = (
            agent_types.get(target_agent_type)
            if isinstance(agent_types, Mapping)
            else None
        )
        if isinstance(entry, Mapping) and field_name in entry:
            return entry.get(field_name)

        default_entry = payload.get("default")
        if isinstance(default_entry, Mapping) and field_name in default_entry:
            return default_entry.get(field_name)

        current_type = self._current_agent_type()
        if target_agent_type == current_type:
            runtime_field = "model_provider" if field_name == "provider" else field_name
            return getattr(self.agent, runtime_field, None)

        target_settings = config.agent_types.get(target_agent_type)
        target_llm = (
            getattr(target_settings, "llm", None)
            if target_settings is not None
            else None
        )
        if target_llm is not None:
            target_value = getattr(target_llm, field_name, None)
            if target_value is not None:
                return target_value
        return getattr(config.llm, field_name, None)

    @staticmethod
    def _get_config_value(config: Any, path: str) -> Any:
        if path.startswith("agent.debug.context_dump."):
            field_name = path.removeprefix("agent.debug.context_dump.")
            return ((config.agent.debug or {}).get("context_dump") or {}).get(
                field_name
            )
        target: Any = config
        for part in path.split("."):
            if target is None:
                return None
            if isinstance(target, Mapping):
                target = target.get(part)
            else:
                target = getattr(target, part, None)
        return target

    @staticmethod
    def _get_composition_value(
        composition: Any, path: str, *, target_agent_type: str | None
    ) -> Any:
        if path == "team.startup_agents.mode":
            return composition.startup_agents.mode
        if path.startswith("team.startup_agents."):
            entry = next(
                (
                    item
                    for item in composition.startup_agents.agents
                    if item.named_agent_type == target_agent_type
                ),
                None,
            )
            field_name = path.removeprefix("team.startup_agents.")
            return getattr(entry, field_name, None) if entry is not None else None
        if path == "team.ephemeral_agents.default_idle_ttl_seconds":
            return composition.ephemeral_agents.default_idle_ttl_seconds
        if path.startswith("team.ephemeral_agents."):
            entry = composition.ephemeral_agents.agent_types.get(
                target_agent_type or ""
            )
            field_name = path.removeprefix("team.ephemeral_agents.")
            return getattr(entry, field_name, None) if entry is not None else None
        raise ValueError(f"Unsupported team composition setting {path!r}.")

    @staticmethod
    def _read_yaml_mapping(path: Path, *, must_exist: bool = False) -> dict[str, Any]:
        if not path.exists():
            if must_exist:
                raise FileNotFoundError(
                    f"Required settings source does not exist: {path}"
                )
            return {}
        try:
            loaded = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        except yaml.YAMLError as exc:
            raise ValueError(f"Invalid YAML in settings source {path}: {exc}") from exc
        if not isinstance(loaded, dict):
            raise ValueError(f"Settings source must contain a YAML mapping: {path}")
        return dict(loaded)

    @staticmethod
    def _plan_instructions(plan: RuntimeSettingsChangePlan, model_test: dict[str, Any] | None = None) -> str:
        tools_sent = (model_test or {}).get("tools_sent")
        tested = bool(model_test and (model_test.get("tested_before_write") or tools_sent))
        answered = (
            f"The model accepted a test request with {tools_sent} tools. "
            if tools_sent
            else "The new model answered a test call with these settings. " if tested else ""
        )
        if plan.authority_mode == AUTHORITY_MODE_PERSON_APPROVAL and model_choice.is_model_choice(plan.changes):
            # Owner's decision 2026-10-10: try once, then ask. Holding model
            # choice spares the Inbox review, not the person's go-ahead.
            confirm = (
                "Tell the person it works, show the old and new values, and ask them to confirm. When they "
                "confirm, call apply_change(change_id, reason)"
                if tested
                else "Call apply_change(change_id, reason)"
            )
            return (
                f"{answered}This plan changes only the choice of model. {confirm}: when you hold model choice "
                "for the person you work for (an administrator), it applies at once and they get a notice; "
                "otherwise a person approves the exact plan in the Inbox and your task waits. Then tell the "
                "person the result's message."
            )
        if plan.authority_mode == AUTHORITY_MODE_PERSON_APPROVAL:
            return (
                f"{answered}Explain the old and new values and why, then call apply_change(change_id, reason). "
                "A person approves the exact plan in the Inbox and the change is applied then; "
                "your task waits until they decide."
            )
        if tested:
            return (
                f"Authorization is off. {answered}Tell the person it works, show the old and new values, and "
                "ask them to confirm. When they confirm, call apply_change(change_id, reason, confirmed=true) "
                "before expires_at_utc. If requires_restart is true, restart only when they also agreed to it."
            )
        return (
            "Authorization is off. Explain the old and new values and the impact, get the person's "
            "agreement in the conversation, then call apply_change(change_id, reason, confirmed=true) "
            "before expires_at_utc. If requires_restart is true, restart only when they also agreed to it."
        )

    @staticmethod
    def _verify_sources(plan: RuntimeSettingsChangePlan) -> None:
        for snapshot in plan.source_snapshots:
            path = Path(snapshot.path)
            exists = path.exists()
            content = path.read_bytes() if exists else b""
            if exists != snapshot.existed or sha256_bytes(content) != snapshot.sha256:
                raise PermissionError(
                    f"Runtime settings source changed after planning: {path}. Stage a fresh plan."
                )

    def _apply_plan(
        self, config: Any, plan: RuntimeSettingsChangePlan
    ) -> dict[str, Any]:
        target = plan.changes[0].persistence_target
        snapshot = plan.source_snapshots[0]
        source_path = Path(snapshot.path)
        if target == SettingPersistenceTarget.WORKSPACE_LLM_OVERRIDES:
            return RuntimeLLMOverrideApplier(self.agent).apply(
                config,
                source_path,
                plan.changes,
                snapshot=snapshot,
                change_id=plan.change_id,
            )
        if target == SettingPersistenceTarget.MODEL_CAPABILITY:
            from custom_agents.model_tool_limit import ModelToolLimitApplier

            return ModelToolLimitApplier(self.agent).apply(
                config, source_path, plan.changes, snapshot=snapshot, change_id=plan.change_id
            )

        preflight, failure = self._test_before_write(config, plan)
        if failure is not None:
            return failure
        # A model or endpoint probe may take long enough for another writer to
        # edit the source. Re-check after the calls, before creating any backup.
        self._verify_sources(plan)
        previous_bytes = source_path.read_bytes() if source_path.exists() else b""
        backup_path = source_path.with_name(
            f"{source_path.name}.bak.runtime-settings.{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S%fZ')}"
        )
        backup_path.parent.mkdir(parents=True, exist_ok=True)
        if target != SettingPersistenceTarget.INSTANCE_CONFIG:
            atomic_write_bytes(backup_path, previous_bytes)

        written_revision: str | None = None
        try:
            extras: dict[str, Any] = {}
            if target == SettingPersistenceTarget.INSTANCE_CONFIG:
                written_revision = self._apply_instance_config(
                    plan.changes, source_path, expected_revision=snapshot.sha256, backup_path=backup_path
                )
                previous_bytes = backup_path.read_bytes()
                extras.update(self._apply_live_context_dump(plan.changes))
                if preflight is not None:
                    extras["preflight"] = preflight
            elif target == SettingPersistenceTarget.TEAM_LEAD_PROFILE:
                extras["profile_update"] = AgentProfileService(self.agent).update_team_lead_soul(
                    str(plan.changes[0].value)
                )
            elif target == SettingPersistenceTarget.GENERATED_AGENT_DEFINITION:
                self._apply_generated_agent_definition(
                    source_path, plan.changes, config
                )
            elif target == SettingPersistenceTarget.TEAM_COMPOSITION:
                self._apply_team_composition(source_path, plan.changes)
            else:
                raise ValueError(f"Unsupported persistence target {target.value}.")
            current_bytes = source_path.read_bytes() if source_path.exists() else b""
            prune_config_backups(
                source_path,
                event_logger=logger,
                preserve_paths=(backup_path,),
            )
            return {
                "persistence_target": target.value,
                "saved_path": str(source_path),
                "backup_path": str(backup_path),
                "previous_existed": snapshot.existed,
                "previous_sha256": sha256_bytes(previous_bytes),
                "sha256": written_revision or sha256_bytes(current_bytes),
                **extras,
            }
        except Exception:
            if target == SettingPersistenceTarget.INSTANCE_CONFIG:
                if written_revision is not None:
                    try:
                        replace_config_file(
                            source_path,
                            previous_bytes if snapshot.existed else None,
                            expected_revision=written_revision,
                        )
                    except ConfigFileConflict:
                        logger.warning(
                            "A failed settings change could not be undone because another writer saved later. "
                            "The later settings were kept. event=runtime_settings_rollback_conflict path=%s",
                            source_path,
                        )
            elif snapshot.existed:
                atomic_write_bytes(source_path, previous_bytes)
            else:
                source_path.unlink(missing_ok=True)
            raise

    def _test_before_write(
        self, config: Any, plan: RuntimeSettingsChangePlan
    ) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
        """Test restart-time model settings and any next-turn endpoint change."""

        snapshots = {snapshot.path: snapshot for snapshot in plan.source_snapshots}
        candidates = self._model_candidates(config, plan.changes, snapshots)
        model_test = None
        if candidates:
            model_test, failure = RuntimeLLMOverrideApplier(self.agent).test_candidates(candidates, change_id=plan.change_id)
            if failure is not None:
                return None, failure
        endpoint_test = self._endpoint_test(config, plan)
        if endpoint_test is not None and endpoint_test.get("error"):
            return None, endpoint_test
        if endpoint_test is not None and model_test is not None:
            return {**endpoint_test, "model_test": model_test}, None
        return endpoint_test if endpoint_test is not None else model_test, None

    def _endpoint_test(self, config: Any, plan: RuntimeSettingsChangePlan) -> dict[str, Any] | None:
        """A switched model endpoint answers one real request before anything is saved (``endpoint_switch``)."""

        change = next((item for item in plan.changes if item.path == ENDPOINT_SETTING_PATH), None)
        return None if change is None else check_endpoint_with_a_request(self.agent, config, str(change.value))

    @staticmethod
    def _set_nested_model_attr(root: Any, path: str, value: Any) -> None:
        target = root
        parts = path.split(".")
        for part in parts[:-1]:
            target = getattr(target, part)
        setattr(target, parts[-1], value)

    def _apply_instance_config(
        self,
        changes: list[RuntimeSettingPlanChange],
        source_path: Path,
        *,
        expected_revision: str,
        backup_path: Path,
    ) -> str:
        from assistant_gateway.config import GatewayInstanceConfig

        # Build from the same saved file whose approved revision the write
        # checks. The earlier model may predate a concurrent settings writer.
        config = GatewayInstanceConfig.load(source_path)
        validated = self._instance_config_candidate(config, changes)
        validated.save(source_path, expected_revision=expected_revision, backup_path=backup_path)
        revision: str | None = validated.source_revision
        assert revision is not None
        return revision

    def _instance_config_candidate(
        self,
        config: Any,
        changes: list[RuntimeSettingPlanChange],
    ) -> Any:
        """Build the validated configuration used for testing and saving, without I/O."""

        from runtime_settings.models import AgentTypeLLMOverrideSettings
        from runtime_settings.agent_types import AgentTypeRuntimeSettings
        from assistant_gateway.config import GatewayInstanceConfig
        from runtime_settings.schedule_workers import SCHEDULE_WORKER_ELIGIBILITY_PATH

        config = config.model_copy(deep=True)
        # Definition registration can change after planning without changing YAML.
        # Validate against the current runtime before mutating any candidate state.
        for change in changes:
            if change.path == SCHEDULE_WORKER_ELIGIBILITY_PATH:
                from custom_agents.schedule_worker_settings import validate_worker_eligibility_change

                validate_worker_eligibility_change(self.agent, config.agent.schedule_workers, change.value)

        for change in changes:
            path = change.actual_path
            if path.startswith("agent.debug.context_dump."):
                field_name = path.removeprefix("agent.debug.context_dump.")
                debug_config = dict(config.agent.debug or {})
                context_dump = dict(debug_config.get("context_dump") or {})
                context_dump[field_name] = change.value
                debug_config["context_dump"] = context_dump
                config.agent.debug = debug_config
            elif path.startswith("agent.schedule_workers.defaults."):
                from runtime_settings.schedule_workers import ScheduleWorkersConfig

                payload = config.agent.schedule_workers.model_dump(mode="json")
                payload["defaults"][path.removeprefix("agent.schedule_workers.defaults.")] = change.value
                config.agent.schedule_workers = ScheduleWorkersConfig.model_validate(payload)
            elif path.startswith("agent_types."):
                parts = path.split(".")
                is_worker_rule = len(parts) == 5 and parts[2:4] == ["agent", "schedule_worker_rules"]
                if not is_worker_rule and (len(parts) != 4 or parts[2] != "llm"):
                    raise ValueError(f"Unsupported agent-type setting path: {path}")
                agent_type = parts[1]
                if agent_type not in config.agent_types:
                    config.agent_types[agent_type] = AgentTypeRuntimeSettings()
                settings = config.agent_types[agent_type]
                if is_worker_rule:
                    rules = dict(settings.agent.schedule_worker_rules or {})
                    rules[parts[4]] = change.value
                    config.agent.schedule_workers.resolve_rules(rules)
                    settings.agent.schedule_worker_rules = rules
                    continue
                if settings.llm is None:
                    settings.llm = AgentTypeLLMOverrideSettings()
                setattr(settings.llm, parts[3], change.value)
            else:
                self._set_nested_model_attr(config, path, change.value)
        return GatewayInstanceConfig.model_validate(config.model_dump(mode="json"))

    def _apply_live_context_dump(
        self,
        changes: list[RuntimeSettingPlanChange],
    ) -> dict[str, Any]:
        paths = {change.path for change in changes}
        context_paths = {
            "agent.debug.context_dump.enabled",
            "agent.debug.context_dump.mode",
        }
        if not paths & context_paths or not hasattr(
            self.agent, "apply_runtime_context_dump_settings"
        ):
            return {"live_applied": []}
        reloaded = self._load_canonical_config()
        context_dump = dict((reloaded.agent.debug or {}).get("context_dump") or {})
        self.agent.apply_runtime_context_dump_settings(
            enabled=bool(context_dump.get("enabled", False)),
            mode=str(context_dump.get("mode") or "on_error"),
            source="user_runtime_settings_change",
        )
        return {"live_applied": sorted(paths & context_paths)}

    def _apply_generated_agent_definition(
        self,
        source_path: Path,
        changes: list[RuntimeSettingPlanChange],
        config: Any,
    ) -> None:
        payload = self._read_yaml_mapping(source_path, must_exist=True)
        target_agent_type = str(changes[0].target_agent_type or "")
        entry = (payload.get("agent_types") or {}).get(target_agent_type) or {}
        if not isinstance(entry, dict) or not entry:
            raise ValueError(
                f"Generated agent definition {target_agent_type!r} was not found."
            )
        if "extends" not in entry:
            raise ValueError(
                "Only TeamArchitect-generated template definitions are lead-manageable."
            )
        params = entry.get("params")
        if not isinstance(params, dict):
            params = {}
            entry["params"] = params
        overrides = entry.get("overrides")
        if not isinstance(overrides, dict):
            overrides = {}
        for change in changes:
            if change.target_agent_type != target_agent_type:
                raise ValueError(
                    "One specialist metadata plan may target only one agent type."
                )
            field_name = change.path.removeprefix("specialist.")
            if field_name not in {"display_name", "role", "description", "prompt"}:
                raise PermissionError(
                    f"Generated specialist field {field_name!r} is not behavioral metadata."
                )
            target = overrides if field_name in overrides else params
            target[field_name] = change.value
        atomic_write_bytes(
            source_path, yaml.safe_dump(payload, sort_keys=False).encode("utf-8")
        )

        from custom_tools.agent_catalog_port import lookup_agent_type
        from custom_agents.team_composition import (
            generated_agent_bundle_root,
            load_team_composition,
        )

        composition = load_team_composition(self._team_composition_path(config))
        generated_root = generated_agent_bundle_root(
            control_dir=config.layout.control_dir,
            config=composition,
        )
        if (
            lookup_agent_type(
                [generated_root],
                agent_type=target_agent_type,
                include_configured=True,
            )
            is None
        ):
            raise ValueError(
                f"Updated generated definition {target_agent_type!r} failed catalog hydration."
            )

    def _apply_team_composition(
        self,
        source_path: Path,
        changes: list[RuntimeSettingPlanChange],
    ) -> None:
        from custom_agents.team_composition import (
            load_team_composition,
            save_team_composition,
            set_ephemeral_agent_policy,
            set_startup_agent,
        )

        composition = load_team_composition(source_path)
        for change in changes:
            if change.path == "team.startup_agents.mode":
                composition.startup_agents.mode = str(change.value)
            elif change.path in {
                "team.startup_agents.count",
                "team.startup_agents.enabled",
            }:
                agent_type = str(change.target_agent_type or "")
                existing = next(
                    (
                        item
                        for item in composition.startup_agents.agents
                        if item.named_agent_type == agent_type
                    ),
                    None,
                )
                count = (
                    int(change.value)
                    if change.path.endswith(".count")
                    else int(getattr(existing, "count", 1))
                )
                enabled = (
                    bool(change.value)
                    if change.path.endswith(".enabled")
                    else bool(getattr(existing, "enabled", True))
                )
                composition = set_startup_agent(
                    composition,
                    agent_type=agent_type,
                    count=count,
                    enabled=enabled,
                    mode=composition.startup_agents.mode,
                )
            elif change.path in {
                "team.ephemeral_agents.enabled",
                "team.ephemeral_agents.idle_ttl_seconds",
            }:
                agent_type = str(change.target_agent_type or "")
                existing = composition.ephemeral_agents.agent_types.get(agent_type)
                enabled = (
                    bool(change.value)
                    if change.path.endswith(".enabled")
                    else bool(getattr(existing, "enabled", True))
                )
                idle_ttl = (
                    change.value
                    if change.path.endswith(".idle_ttl_seconds")
                    else getattr(existing, "idle_ttl_seconds", None)
                )
                composition = set_ephemeral_agent_policy(
                    composition,
                    agent_type=agent_type,
                    idle_ttl_seconds=idle_ttl,
                    enabled=enabled,
                )
            elif change.path == "team.ephemeral_agents.default_idle_ttl_seconds":
                composition.ephemeral_agents.default_idle_ttl_seconds = change.value
            else:
                raise ValueError(
                    f"Unsupported team composition setting {change.path!r}."
                )
        save_team_composition(source_path, composition)

    def _undo_unrecorded(self, result: Mapping[str, Any]) -> None:
        """Put back a change whose receipt could not be written: the file, and a model's running tool cap with it."""

        try:
            self._restore_backup(result)
        except ConfigFileConflict:
            logger.warning(
                "A failed settings receipt could not be undone because another writer saved later. "
                "The later settings were kept. event=runtime_settings_rollback_conflict path=%s",
                result["saved_path"],
            )
            return
        if result["persistence_target"] == SettingPersistenceTarget.MODEL_CAPABILITY.value:
            from custom_agents.model_tool_limit import use_saved_caps

            use_saved_caps(self.agent, Path(str(result["saved_path"])))

    @staticmethod
    def _restore_backup(result: Mapping[str, Any]) -> None:
        target_path = Path(str(result["saved_path"]))
        backup_path = Path(str(result["backup_path"]))
        if result["persistence_target"] == SettingPersistenceTarget.INSTANCE_CONFIG.value:
            replace_config_file(
                target_path,
                backup_path.read_bytes() if bool(result.get("previous_existed")) else None,
                expected_revision=str(result["sha256"]),
            )
            return
        if bool(result.get("previous_existed")):
            atomic_write_bytes(target_path, backup_path.read_bytes())
        else:
            target_path.unlink(missing_ok=True)

    def _append_history(
        self,
        plan: RuntimeSettingsChangePlan,
        receipt: Mapping[str, Any],
    ) -> None:
        task_manager = getattr(self.agent, "task_manager", None)
        task_id = (task_manager.current_task_id if task_manager is not None else None)
        if not task_id or not hasattr(task_manager, "append_history_entry"):
            return
        try:
            task_manager.append_history_entry(
                task_id,
                f"Applied staged runtime settings change {plan.change_id}.",
                entry_type=TaskHistoryType.TASK_EVENT,
                importance=TaskHistoryImportance.HIGH,
                details={
                    "change_id": plan.change_id,
                    "plan_digest": plan.plan_digest,
                    "decision_class": plan.decision_class.value,
                    "changes": [
                        {
                            "path": change.path,
                            "target_scope": change.target_scope.value,
                            "target_agent_type": change.target_agent_type,
                        }
                        for change in plan.changes
                    ],
                    "receipt_path": receipt.get("receipt_path"),
                    "restart_required": plan.requires_restart,
                    "restart_scope": plan.restart_scope,
                    "restart_authority": "separate_runtime.restart_workflow",
                },
            )
        except Exception:
            logger.exception(
                "Failed to append runtime settings audit breadcrumb. "
                "event=runtime_settings_history_append_failed change_id=%s task_id=%s",
                plan.change_id,
                task_id,
            )
