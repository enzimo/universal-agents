"""Reviewed, provider-independent generated-specialist provisioning.

The service owns a typed configuration transaction. Models may select only
behavioral text and default tools (tool catalog ids or tool set ids, checked
against the shipped catalog); the catalog maps them to Strands functions when
the definition loads (``custom_agents.agent_catalog``). Default tools grant
nothing: with authorization on they say what the new type should be given and
access comes from grants; with it off they are the specialist's tools, next to
its role's standard ones. Optional authorization changes the authority proof
required to apply a plan, not whether provisioning exists.
"""

from __future__ import annotations

import json
import logging
import os
import re
import shutil
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Literal

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    ValidationError,
    field_validator,
)

from access_catalog.catalog import ToolCatalog
from access_control.changes import ChangeApproval
from custom_agents.access_runtime import ActingAgent, acting_agent, runtime_access
from custom_agents.default_tools import builtin_tool_catalog
from custom_agents.governance.authorized_operations import (
    FilePlanStore,
    GovernanceAuditEvent,
    atomic_write_bytes,
    canonical_json_sha256,
    model_digest,
    sha256_bytes,
)
from custom_agents.governance.runtime_adapters import LoggingGovernanceAuditSink
from custom_messages.contracts.access_v2 import ChangeApprovalMode, ChangeProposalV1, OwnerScope, summary_line
from custom_messages.contracts.runtime_v1 import (
    AUTHORITY_MODE_AUTHORIZATION_DISABLED,
    AUTHORITY_MODE_PERSON_APPROVAL,
    TEAM_ARCHITECT_AGENT_TYPE,
)
from custom_agents.generated_agent_runtime import (
    GENERATED_AGENT_TEMPLATE_NAME,
    MAX_GENERATED_DEFAULT_TOOLS,
    GeneratedAgentProvisioningRecordV1,
    apply_generated_agent_provisioning,
)
from custom_messages.contracts.common_v1 import canonical_contract_json, utc_now
from custom_agents.team_composition import (
    generated_agent_bundle_path,
    generated_agent_bundle_root,
    load_team_composition,
    resolve_team_composition_path,
    validate_agent_type_name,
    write_generated_agent_bundle,
)
from id_generation import new_id, new_id_suffix, operational_id


logger = logging.getLogger(__name__)

# Long enough for a person to approve later; the roster snapshot, the
# default-tool check and the destination check still refuse stale plans.
GENERATED_AGENT_PROVISIONING_PLAN_TTL_SECONDS = 24 * 60 * 60
# Version 3: plans list default tools (tool catalog ids) instead of capability
# registry items. Applied through apply_change (a person's approval or agreement).
GENERATED_AGENT_PLAN_SCHEMA_VERSION = 3
# Tools a generated specialist may not name as defaults, besides platform
# controls (owner_scope: platform): host commands and unconfined execution.
# With authorization off a specialist's defaults are its tools, applied on a
# person's conversational agreement, so these stay with the shipped and
# operator-written definitions (they were operator-only as tool groups too).
NOT_GENERATED_DEFAULT_TOOLS = frozenset({"code:shell", "code:unconfined"})
AGENT_DEFINITION_CHANGE_KIND = "agent_definition"
AGENT_DEFINITION_ROLLBACK_CHANGE_KIND = "agent_definition_rollback"
_CHANGE_ID_PATTERN = re.compile(r"generated_agent_(?:[0-9a-f]{12}|[0-9a-f]{20})")
_ROLLBACK_ID_PATTERN = re.compile(r"generated_agent_rollback_(?:[0-9a-f]{12}|[0-9a-f]{20})")
_SHA256_PATTERN = re.compile(r"[0-9a-f]{64}")


# A new or removed specialist takes effect at once: launches, delegation and the
# agent type listings read the definition catalogs on every use
# (custom_agents.strands_team, LocalAgentTeamRuntime.resolve_definition), so no
# manager restart is needed and none is staged.
TAKES_EFFECT_NOW = "Available as soon as it is created; no restart."
REMOVAL_TAKES_EFFECT_NOW = (
    "Takes effect at once: nothing new starts as this type; a worker of this type "
    "that is already running keeps running until it stops. No restart."
)


def _created_next_step(agent_type: str) -> str:
    return (
        f"{agent_type} is available now, with no restart: get_available_agent_types lists it, and "
        "delegation and schedules can use it; delegated tasks wait in its queue and the worker pool "
        "controller starts its workers (default pool size; set another with set_worker_pool_limit). For a schedule that starts it automatically, add it to "
        "agent.schedule_workers.eligible_agent_types with plan_runtime_setting_change (keep the other "
        "entries), then configure the schedule."
    )


def eligible_default_tools(catalog: ToolCatalog, default_tools: Any) -> list[str]:
    """A generated specialist's default tools, normalized; ValueError naming what is refused.

    Each entry is a tool catalog id or a tool set id from the shipped catalog.
    Platform controls and ``NOT_GENERATED_DEFAULT_TOOLS`` are refused, also
    inside a tool set.
    """

    normalized = sorted({str(item or "").strip() for item in default_tools or () if str(item or "").strip()})
    if not normalized or len(normalized) > MAX_GENERATED_DEFAULT_TOOLS:
        raise ValueError(
            f"Generated specialists need between one and {MAX_GENERATED_DEFAULT_TOOLS} default tools "
            "(tool catalog ids or tool set ids from find_tools)."
        )
    refused = sorted(
        tool.tool_id
        for entry in normalized
        for tool in _default_tool_members(catalog, entry)
        if tool.owner_scope is not OwnerScope.TEAM or tool.tool_id in NOT_GENERATED_DEFAULT_TOOLS
    )
    if refused:
        raise PermissionError(
            f"A generated specialist cannot have {', '.join(refused)} as a default tool: platform "
            "controls, administrator-only tools and host commands stay with shipped or operator-written definitions."
        )
    return normalized


def _default_tool_members(catalog: ToolCatalog, entry: str) -> list[Any]:
    """The catalog tools one default-tool entry names; ValueError for an unknown id."""

    if entry in catalog.tools:
        return [catalog.tools[entry]]
    if entry in catalog.tool_sets:
        return list(catalog.tools_in_set(entry))
    raise ValueError(f"{entry!r} is not a tool catalog id or tool set id; find_tools lists them.")


def _default_tool_name(catalog: ToolCatalog, entry: str) -> str:
    tool = catalog.tools.get(entry)
    if tool is not None:
        return tool.display_name
    tool_set = catalog.tool_sets.get(entry)
    return tool_set.display_name if tool_set is not None else entry


def _receipt_record(receipt_path: Path, receipt: dict[str, Any]) -> GeneratedAgentProvisioningRecordV1:
    """The record in an apply receipt; a clear error for one written before default tools."""

    try:
        return GeneratedAgentProvisioningRecordV1.model_validate(receipt["record"])
    except (KeyError, ValidationError) as exc:
        raise ValueError(
            f"{receipt_path} records a specialist created by an earlier version, before default tools "
            "replaced capability ids. Its definition no longer loads: move its folder out of "
            "control/agent_bundles/team_lead_generated and this receipt out of "
            "runtime/generated_agent_receipts, then create the specialist again."
        ) from exc


class GeneratedAgentSourceSnapshot(BaseModel):
    model_config = ConfigDict(extra="forbid")

    path: str
    existed: bool
    sha256: str


class GeneratedAgentProvisioningPlan(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: int = GENERATED_AGENT_PLAN_SCHEMA_VERSION
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
    agent_type: str
    display_name: str
    role: str
    description: str
    prompt: str
    visible: bool = False
    reason: str
    template_name: Literal["TaskSpecialistTemplate"] = GENERATED_AGENT_TEMPLATE_NAME
    # Tool catalog ids or tool set ids, sorted (eligible_default_tools).
    default_tools: list[str] = Field(min_length=1, max_length=MAX_GENERATED_DEFAULT_TOOLS)
    composition_snapshot: GeneratedAgentSourceSnapshot
    final_bundle_path: str

    @field_validator("schema_version")
    @classmethod
    def _validate_schema_version(cls, value: int) -> int:
        if int(value) != GENERATED_AGENT_PLAN_SCHEMA_VERSION:
            raise ValueError(
                f"Only generated-agent provisioning plan schema version {GENERATED_AGENT_PLAN_SCHEMA_VERSION} is supported."
            )
        return int(value)

    @field_validator("change_id")
    @classmethod
    def _validate_change_id(cls, value: str) -> str:
        normalized = str(value or "").strip()
        if not _CHANGE_ID_PATTERN.fullmatch(normalized):
            raise ValueError("Invalid generated-agent change id.")
        return normalized


class GeneratedAgentRollbackPlan(BaseModel):
    """A staged removal of one generated specialist, bound to its saved record."""

    model_config = ConfigDict(extra="forbid")

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
    provisioning_change_id: str
    agent_type: str
    record_sha256: str
    reason: str

    @field_validator("change_id")
    @classmethod
    def _validate_change_id(cls, value: str) -> str:
        normalized = str(value or "").strip()
        if not _ROLLBACK_ID_PATTERN.fullmatch(normalized):
            raise ValueError("Invalid generated-agent rollback id.")
        return normalized


class GeneratedAgentProvisioningService:
    """Stage, apply (after approval), inspect, and roll back generated definitions.

    Plans are applied through ``apply_change`` (``access_control.changes``):
    with authorization on a person approves the exact specialist; with it off
    the person's conversational agreement applies it.
    """

    def __init__(self, agent: Any) -> None:
        self.agent = agent
        self.audit = LoggingGovernanceAuditSink()

    @staticmethod
    def _rollback_store(config: Any) -> FilePlanStore[GeneratedAgentRollbackPlan]:
        return FilePlanStore(
            GeneratedAgentProvisioningService._plan_dir(config),
            GeneratedAgentRollbackPlan,
            change_id_pattern=_ROLLBACK_ID_PATTERN,
            invalid_change_id_message="Invalid generated-agent rollback id.",
            missing_plan_message="Generated-agent rollback plan not found: {change_id}",
        )

    @staticmethod
    def _plan_store(config: Any) -> FilePlanStore[GeneratedAgentProvisioningPlan]:
        return FilePlanStore(
            GeneratedAgentProvisioningService._plan_dir(config),
            GeneratedAgentProvisioningPlan,
            change_id_pattern=_CHANGE_ID_PATTERN,
            invalid_change_id_message="Invalid generated-agent change id.",
            missing_plan_message="Generated-agent plan not found: {change_id}",
        )

    def stage(
        self,
        *,
        agent_type: str,
        display_name: str,
        role: str,
        description: str,
        prompt: str,
        default_tools: list[str],
        reason: str,
        visible: bool = False,
    ) -> dict[str, Any]:
        authority_mode = self._authority_mode()
        actor = acting_agent(self.agent)
        self._require_team_lead(actor)
        if not actor.person_id:
            raise PermissionError("Generated-agent provisioning requires TeamArchitect to work for a person.")
        task_id = actor.task_id
        if not task_id:
            raise PermissionError(
                "Generated-agent provisioning must be staged from an active task."
            )

        normalized_type = validate_agent_type_name(agent_type)
        text = {
            "display_name": self._bounded_text(display_name, "display_name", 512),
            "role": self._bounded_text(role, "role", 4_096),
            "description": self._bounded_text(description, "description", 16_384),
            "prompt": self._bounded_text(prompt, "prompt", 131_072),
        }
        normalized_tools = eligible_default_tools(self._catalog(), default_tools)

        config = self._load_canonical_config()
        composition_path = self._composition_path(config)
        composition = load_team_composition(composition_path)
        final_path = generated_agent_bundle_path(
            control_dir=config.layout.control_dir,
            config=composition,
            agent_type=normalized_type,
        )
        if final_path.exists():
            raise FileExistsError(
                f"Generated agent definition already exists: {final_path}"
            )

        from custom_tools.agent_catalog_port import lookup_agent_type

        if (
            lookup_agent_type(
                (),
                agent_type=normalized_type,
                include_configured=True,
            )
            is not None
        ):
            raise ValueError(
                f"Agent type {normalized_type!r} already exists in the loaded catalog."
            )

        composition_bytes = (
            composition_path.read_bytes() if composition_path.exists() else b""
        )
        now = utc_now()
        change_id = new_id("generated_agent")
        plan = GeneratedAgentProvisioningPlan(
            change_id=change_id,
            created_at_utc=now,
            expires_at_utc=now
            + timedelta(seconds=GENERATED_AGENT_PROVISIONING_PLAN_TTL_SECONDS),
            actor_principal_id=actor.agent.agent_id,
            actor_named_agent_type=actor.agent.agent_type,
            team_id=actor.agent.team_id,
            owner_canonical_user_id=actor.person_id,
            source_task_id=task_id,
            authority_mode=authority_mode,
            agent_type=normalized_type,
            display_name=text["display_name"],
            role=text["role"],
            description=text["description"],
            prompt=text["prompt"],
            visible=bool(visible),
            reason=str(reason or "").strip()
            or f"Create reviewed specialist {normalized_type}.",
            default_tools=normalized_tools,
            composition_snapshot=GeneratedAgentSourceSnapshot(
                path=str(composition_path),
                existed=composition_path.exists(),
                sha256=sha256_bytes(composition_bytes),
            ),
            final_bundle_path=str(final_path),
        )
        plan = plan.model_copy(update={"plan_digest": model_digest(plan)})
        plan_path = self._plan_store(config).save(plan)
        self.audit.record(
            GovernanceAuditEvent(
                operation="generated_agent_provisioning",
                phase="staged",
                change_id=plan.change_id,
                plan_digest=plan.plan_digest,
                task_id=plan.source_task_id,
                authority_mode=plan.authority_mode,
            )
        )
        return {
            **plan.model_dump(mode="json"),
            "plan_path": str(plan_path),
            "instructions": self._plan_instructions(authority_mode),
        }

    def load_plan(self, change_id: str) -> GeneratedAgentProvisioningPlan:
        """The stored plan, checked: unchanged, unexpired, same mode, agent, person and task."""

        config = self._load_canonical_config()
        plan = self._plan_store(config).load(str(change_id or "").strip())
        if plan.authority_mode != self._authority_mode():
            raise PermissionError("Authorization was turned on or off after planning. Stage a fresh plan.")
        actor = acting_agent(self.agent)
        self._require_team_lead(actor)
        self._verify_plan(plan, actor=actor)
        return plan

    def _verify_current(self, config: Any, plan: GeneratedAgentProvisioningPlan) -> tuple[Any, Path, Path]:
        """The plan can still apply: not applied yet, defaults still eligible, sources unchanged, destination free.

        Runs before a person is asked (``describe``) and again when applying,
        so nobody approves a change that can only fail. Returns the team
        roster, the generated-bundle folder and the new bundle's path.
        """

        if self._receipt_path(config, plan.change_id).is_file():
            raise FileExistsError(f"Generated-agent plan {plan.change_id} was already applied.")
        # A deploy between staging and approval may have changed the catalog.
        eligible_default_tools(self._catalog(), plan.default_tools)
        self._verify_sources(plan)
        composition = load_team_composition(plan.composition_snapshot.path)
        generated_root = generated_agent_bundle_root(control_dir=config.layout.control_dir, config=composition)
        final_path = Path(plan.final_bundle_path)
        expected_final = generated_agent_bundle_path(
            control_dir=config.layout.control_dir,
            config=composition,
            agent_type=plan.agent_type,
        )
        if final_path != expected_final:
            raise PermissionError("Generated bundle destination changed after planning.")
        if final_path.exists():
            raise FileExistsError(f"Generated agent definition already exists: {final_path}")
        return composition, generated_root, final_path

    def describe(self, change_id: str) -> ChangeProposalV1:
        if _ROLLBACK_ID_PATTERN.fullmatch(str(change_id or "").strip()):
            return self._describe_rollback(change_id)
        plan = self.load_plan(change_id)
        self._verify_current(self._load_canonical_config(), plan)
        catalog = self._catalog()
        defaults = ", ".join(f"{_default_tool_name(catalog, item)} ({item})" for item in plan.default_tools)
        return ChangeProposalV1(
            kind=AGENT_DEFINITION_CHANGE_KIND,
            change_id=plan.change_id,
            plan_digest=plan.plan_digest,
            title=f"Create the specialist {plan.agent_type}",
            summary=summary_line(
                f"Create the specialist {plan.agent_type} ({plan.display_name}) with "
                f"{len(plan.default_tools)} default tool{'s' if len(plan.default_tools) != 1 else ''}. "
                f"{TAKES_EFFECT_NOW}"
            ),
            effect=(
                f"Creates a new agent type {plan.agent_type} ({plan.display_name}): {plan.role}"[:1000],
                f"Its default tools: {defaults}."[:1000],
                f"Instructions: {plan.prompt}"[:1000],
                (
                    f"{TAKES_EFFECT_NOW} Default tools grant nothing: the specialist "
                    "uses a tool only while it holds access for the person it works for."
                    if plan.authority_mode == AUTHORITY_MODE_PERSON_APPROVAL
                    else f"{TAKES_EFFECT_NOW} With authorization off its default tools are its tools."
                ),
                f"Reason: {plan.reason}"[:1000],
            ),
        )

    def apply(self, change_id: str, approval: ChangeApproval) -> dict[str, Any]:
        """Apply an approved plan or rollback (only the change mechanism calls this)."""

        if _ROLLBACK_ID_PATTERN.fullmatch(str(change_id or "").strip()):
            return self._apply_rollback(change_id, approval)
        config = self._load_canonical_config()
        normalized_change_id = str(change_id or "").strip()
        receipt_path = self._receipt_path(config, normalized_change_id)
        if receipt_path.is_file():
            return {
                "error": "Generated-agent plan was already applied.",
                "reason_code": "generated_agent_provisioning_replay",
                "change_id": normalized_change_id,
                "receipt_path": str(receipt_path),
            }
        plan = self.load_plan(normalized_change_id)
        agreement = approval.mode is ChangeApprovalMode.AGREEMENT
        if agreement != (plan.authority_mode == AUTHORITY_MODE_AUTHORIZATION_DISABLED):
            raise PermissionError("This plan needs a person's approval, not agreement (or the reverse).")
        composition, generated_root, final_path = self._verify_current(config, plan)
        authority = {"mode": plan.authority_mode, "approval": approval.record()}

        staging_root = generated_root / ".provisioning" / plan.change_id
        staging_config = composition.model_copy(
            update={"generated_agent_bundles_dir": str(staging_root)}
        )
        staged_bundle = staging_root / plan.agent_type
        shutil.rmtree(staging_root, ignore_errors=True)
        activated = False
        try:
            catalog_path = write_generated_agent_bundle(
                control_dir=config.layout.control_dir,
                config=staging_config,
                agent_type=plan.agent_type,
                template_name=plan.template_name,
                params={
                    "display_name": plan.display_name,
                    "role": plan.role,
                    "description": plan.description,
                    "prompt": plan.prompt,
                    "visible": plan.visible,
                },
                overrides={
                    "default_tools": list(plan.default_tools),
                    "provisioning_record_ref": "provisioning.json",
                },
            )
            catalog_sha256 = sha256_bytes(catalog_path.read_bytes())
            applied_at = utc_now()
            record = GeneratedAgentProvisioningRecordV1(
                provisioning_id=new_id("gen_agent_prov"),
                change_id=plan.change_id,
                plan_digest=plan.plan_digest,
                status="ready",
                agent_type=plan.agent_type,
                team_id=plan.team_id,
                owner_canonical_user_id=plan.owner_canonical_user_id,
                actor_principal_id=plan.actor_principal_id,
                default_tools=tuple(plan.default_tools),
                catalog_sha256=catalog_sha256,
                approval=approval.to_record(),
                created_at_utc=plan.created_at_utc,
                applied_at_utc=applied_at,
            )
            manifest_path = staged_bundle / "provisioning.json"
            atomic_write_bytes(
                manifest_path,
                (canonical_contract_json(record) + "\n").encode("utf-8"),
                immutable=True,
            )
            self._validate_staged_bundle(
                config=config,
                plan=plan,
                catalog_path=catalog_path,
                manifest_path=manifest_path,
            )
            final_path.parent.mkdir(parents=True, exist_ok=True)
            os.replace(staged_bundle, final_path)
            activated = True
            receipt = {
                "schema_version": 1,
                "receipt_type": "generated_agent_provisioning_apply",
                "record": record.model_dump(mode="json", exclude_none=True),
                "authority": authority,
                "bundle_path": str(final_path),
                "catalog_path": str(final_path / "agent.yaml"),
                "manifest_path": str(final_path / "provisioning.json"),
                "receipt_path": str(receipt_path),
            }
            atomic_write_bytes(
                receipt_path,
                (json.dumps(receipt, indent=2, sort_keys=True) + "\n").encode("utf-8"),
                immutable=True,
            )
        except Exception:
            if activated and final_path.exists() and not staged_bundle.exists():
                staged_bundle.parent.mkdir(parents=True, exist_ok=True)
                os.replace(final_path, staged_bundle)
            raise
        finally:
            shutil.rmtree(staging_root, ignore_errors=True)

        self.audit.record(
            GovernanceAuditEvent(
                operation="generated_agent_provisioning",
                phase="applied",
                change_id=plan.change_id,
                plan_digest=plan.plan_digest,
                task_id=plan.source_task_id,
                authority_mode=plan.authority_mode,
            )
        )
        return {
            "status": "ready",
            "change_id": plan.change_id,
            "provisioning_id": record.provisioning_id,
            "agent_type": plan.agent_type,
            "default_tools": list(record.default_tools),
            "authority": authority,
            "bundle_path": str(final_path),
            "receipt_path": str(receipt_path),
            "next_step": _created_next_step(plan.agent_type),
        }

    def status(self, *, change_id: str = "", agent_type: str = "") -> dict[str, Any]:
        config = self._load_canonical_config()
        normalized_change = str(change_id or "").strip()
        normalized_type = (
            validate_agent_type_name(agent_type)
            if str(agent_type or "").strip()
            else ""
        )
        if not normalized_change and not normalized_type:
            raise ValueError("change_id or agent_type is required.")
        receipt_path, receipt = self._latest_receipt(
            config, change_id=normalized_change, agent_type=normalized_type
        )
        record = _receipt_record(receipt_path, receipt)
        rollback_path = self._rollback_receipt_path(config, record.change_id)
        if rollback_path.is_file():
            rollback = json.loads(rollback_path.read_text(encoding="utf-8"))
            return {**rollback, "receipt_path": str(rollback_path)}
        bundle_path = Path(receipt["bundle_path"])
        state = (
            "ready"
            if self._record_matches_bundle(record, bundle_path)
            else "drifted_or_missing"
        )
        return {
            "status": state,
            "record_scope": "initial_generated_definition",
            "next_step": (
                "record.default_tools are the tools the person approved as the specialist's "
                "defaults. What it may use comes from each task it is given: it gets what you "
                "may hand on when its task needs it. Ask for what you lack with request_access "
                "and hand_on=true."
            ),
            "record": record.model_dump(mode="json", exclude_none=True),
            "bundle_path": str(bundle_path),
            "receipt_path": str(receipt_path),
        }

    def _latest_receipt(self, config: Any, *, change_id: str, agent_type: str) -> tuple[Path, dict[str, Any]]:
        """The newest apply receipt for a change id or agent type (matched before the record is validated)."""

        receipts = sorted(self._receipt_dir(config).glob("generated_agent_*.json"))
        for receipt_path in reversed(receipts):
            if receipt_path.name.endswith(".rollback.json"):
                continue
            receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
            raw_record = receipt.get("record") or {}
            if change_id and raw_record.get("change_id") != change_id:
                continue
            if agent_type and raw_record.get("agent_type") != agent_type:
                continue
            return receipt_path, receipt
        raise FileNotFoundError(
            "No generated-agent provisioning receipt matched the query."
        )

    def stage_rollback(self, change_id: str, *, reason: str) -> dict[str, Any]:
        """Stage removing one generated specialist; apply it with apply_change."""

        config = self._load_canonical_config()
        actor = acting_agent(self.agent)
        self._require_team_lead(actor)
        if not actor.person_id or not actor.task_id:
            raise PermissionError("A rollback must be staged from a task that works for a person.")
        receipt, record = self._applied_record(config, change_id)
        if actor.agent.team_id != record.team_id:
            raise PermissionError("TeamArchitect cannot roll back another team's generated definition.")
        now = utc_now()
        plan = GeneratedAgentRollbackPlan(
            # "generated_agent_rollback_<hex>": the shared id prefix limit is 15 characters.
            change_id=operational_id("generated_agent", f"rollback_{new_id_suffix()}"),
            created_at_utc=now,
            expires_at_utc=now + timedelta(seconds=GENERATED_AGENT_PROVISIONING_PLAN_TTL_SECONDS),
            actor_principal_id=actor.agent.agent_id,
            actor_named_agent_type=actor.agent.agent_type,
            team_id=actor.agent.team_id,
            owner_canonical_user_id=actor.person_id,
            source_task_id=actor.task_id,
            authority_mode=self._authority_mode(),
            provisioning_change_id=record.change_id,
            agent_type=record.agent_type,
            record_sha256=canonical_json_sha256(receipt["record"]),
            reason=str(reason or "").strip() or f"Remove the generated specialist {record.agent_type}.",
        )
        plan = plan.model_copy(update={"plan_digest": model_digest(plan)})
        self._rollback_store(config).save(plan)
        return {
            **plan.model_dump(mode="json"),
            "instructions": (
                "Explain what removing the specialist means, then call apply_change(change_id, reason)"
                + ("; a person approves it in the Inbox." if plan.authority_mode == AUTHORITY_MODE_PERSON_APPROVAL
                   else " with confirmed=true once the person agrees.")
            ),
        }

    def _applied_record(self, config: Any, change_id: str) -> tuple[dict[str, Any], GeneratedAgentProvisioningRecordV1]:
        receipt_path = self._receipt_path(config, change_id)
        if not receipt_path.is_file():
            raise FileNotFoundError(f"Generated-agent provisioning receipt not found: {change_id}")
        if self._rollback_receipt_path(config, change_id).is_file():
            raise FileExistsError(f"The generated specialist from {change_id} was already rolled back.")
        receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
        return receipt, _receipt_record(receipt_path, receipt)

    def _load_rollback(self, change_id: str) -> tuple[Any, GeneratedAgentRollbackPlan, dict[str, Any], GeneratedAgentProvisioningRecordV1]:
        config = self._load_canonical_config()
        plan = self._rollback_store(config).load(str(change_id or "").strip())
        actor = acting_agent(self.agent)
        self._require_team_lead(actor)
        if plan.plan_digest != model_digest(plan) or plan.expires_at_utc <= utc_now():
            raise PermissionError("Generated-agent rollback plan is changed or expired. Stage it again.")
        if plan.authority_mode != self._authority_mode():
            raise PermissionError("Authorization was turned on or off after planning. Stage it again.")
        if (
            plan.actor_principal_id != actor.agent.agent_id
            or plan.team_id != actor.agent.team_id
            or plan.owner_canonical_user_id != actor.person_id
            or plan.source_task_id != actor.task_id
        ):
            raise PermissionError("Generated-agent rollback plan belongs to another agent, person or task.")
        receipt, record = self._applied_record(config, plan.provisioning_change_id)
        if canonical_json_sha256(receipt["record"]) != plan.record_sha256:
            raise PermissionError("The specialist's record changed after the rollback was staged.")
        if not self._record_matches_bundle(record, Path(receipt["bundle_path"])):
            raise PermissionError("Generated definition changed or is missing; refusing destructive rollback.")
        return config, plan, receipt, record

    def _describe_rollback(self, change_id: str) -> ChangeProposalV1:
        _config, plan, _receipt, record = self._load_rollback(change_id)
        return ChangeProposalV1(
            kind=AGENT_DEFINITION_ROLLBACK_CHANGE_KIND,
            change_id=plan.change_id,
            plan_digest=plan.plan_digest,
            title=f"Remove the specialist {record.agent_type}",
            summary=summary_line(
                f"Remove the specialist {record.agent_type} (kept in a recoverable folder). "
                "Takes effect at once."
            ),
            effect=(
                f"Removes the generated agent type {record.agent_type}; its definition is kept in a "
                "recoverable folder.",
                REMOVAL_TAKES_EFFECT_NOW,
                f"Reason: {plan.reason}"[:1000],
            ),
        )

    def _apply_rollback(self, change_id: str, approval: ChangeApproval) -> dict[str, Any]:
        config, plan, receipt, record = self._load_rollback(change_id)
        agreement = approval.mode is ChangeApprovalMode.AGREEMENT
        if agreement != (plan.authority_mode == AUTHORITY_MODE_AUTHORIZATION_DISABLED):
            raise PermissionError("This rollback needs a person's approval, not agreement (or the reverse).")
        reason = plan.reason
        rollback_receipt_path = self._rollback_receipt_path(config, record.change_id)
        bundle_path = Path(receipt["bundle_path"])
        composition = load_team_composition(self._composition_path(config))
        generated_root = generated_agent_bundle_root(
            control_dir=config.layout.control_dir,
            config=composition,
        )
        quarantine = generated_root / ".rolled_back" / record.provisioning_id
        if quarantine.exists():
            raise FileExistsError(f"Rollback quarantine already exists: {quarantine}")
        quarantine.parent.mkdir(parents=True, exist_ok=True)
        os.replace(bundle_path, quarantine)
        rolled_back = record.model_copy(
            update={"status": "rolled_back", "rolled_back_at_utc": utc_now()}
        )
        payload = {
            "schema_version": 1,
            "receipt_type": "generated_agent_provisioning_rollback",
            "status": "rolled_back",
            "record": rolled_back.model_dump(mode="json", exclude_none=True),
            "reason": reason,
            "rollback_change_id": plan.change_id,
            "approval": approval.record(),
            "original_bundle_path": str(bundle_path),
            "quarantine_path": str(quarantine),
        }
        try:
            atomic_write_bytes(
                rollback_receipt_path,
                (json.dumps(payload, indent=2, sort_keys=True) + "\n").encode("utf-8"),
                immutable=True,
            )
        except Exception:
            os.replace(quarantine, bundle_path)
            raise
        self.audit.record(
            GovernanceAuditEvent(
                operation="generated_agent_provisioning",
                phase="rolled_back",
                change_id=record.change_id,
                plan_digest=record.plan_digest,
                authority_mode=plan.authority_mode,
            )
        )
        return {**payload, "receipt_path": str(rollback_receipt_path)}

    def _verify_plan(self, plan: GeneratedAgentProvisioningPlan, *, actor: ActingAgent) -> None:
        if plan.plan_digest != model_digest(plan):
            raise PermissionError("Generated-agent plan digest mismatch.")
        if plan.expires_at_utc <= utc_now():
            raise PermissionError("Generated-agent plan has expired. Stage a fresh plan.")
        if (
            plan.actor_principal_id != actor.agent.agent_id
            or plan.actor_named_agent_type != actor.agent.agent_type
            or plan.team_id != actor.agent.team_id
            or plan.owner_canonical_user_id != (actor.person_id or "")
        ):
            raise PermissionError("Generated-agent plan actor, team, or person no longer matches.")
        if plan.source_task_id != (actor.task_id or ""):
            raise PermissionError("Generated-agent provisioning must continue in its staging task.")

    @staticmethod
    def _verify_sources(plan: GeneratedAgentProvisioningPlan) -> None:
        snapshot = plan.composition_snapshot
        path = Path(snapshot.path)
        exists = path.exists()
        content = path.read_bytes() if exists else b""
        if exists != snapshot.existed or sha256_bytes(content) != snapshot.sha256:
            raise PermissionError(
                "TEAM_AGENTS.yaml changed after generated-agent planning."
            )
        if Path(plan.final_bundle_path).exists():
            raise FileExistsError(
                "Generated-agent destination appeared after planning."
            )

    def _validate_staged_bundle(
        self,
        *,
        config: Any,
        plan: GeneratedAgentProvisioningPlan,
        catalog_path: Path,
        manifest_path: Path,
    ) -> None:
        from custom_tools.agent_catalog_port import lookup_agent_type

        definition = lookup_agent_type(
            [catalog_path],
            agent_type=plan.agent_type,
            include_configured=True,
        )
        if definition is None:
            raise ValueError("Staged generated definition did not hydrate.")
        if definition.default_tools != tuple(plan.default_tools):
            raise ValueError(
                "Staged generated definition default tools changed during hydration."
            )
        runtime_config = dict(definition.runtime_config)
        runtime_config.update(
            {
                "team_id": plan.team_id,
                "authorization_config": getattr(self.agent, "authorization_config", {}),
            }
        )
        runtime_config["provisioning_record_path"] = str(manifest_path)
        apply_generated_agent_provisioning(runtime_config)

    @staticmethod
    def _record_matches_bundle(
        record: GeneratedAgentProvisioningRecordV1,
        bundle_path: Path,
    ) -> bool:
        catalog_path = bundle_path / "agent.yaml"
        manifest_path = bundle_path / "provisioning.json"
        if not catalog_path.is_file() or not manifest_path.is_file():
            return False
        try:
            manifest = GeneratedAgentProvisioningRecordV1.model_validate_json(
                manifest_path.read_text(encoding="utf-8")
            )
        except (OSError, ValidationError):
            return False
        return (
            manifest == record
            and sha256_bytes(catalog_path.read_bytes()) == record.catalog_sha256
        )

    @staticmethod
    def _bounded_text(value: str, field_name: str, maximum_bytes: int) -> str:
        normalized = str(value or "").strip()
        if not normalized:
            raise ValueError(f"{field_name} must be non-empty.")
        if len(normalized.encode("utf-8")) > maximum_bytes:
            raise ValueError(f"{field_name} exceeds {maximum_bytes} UTF-8 bytes.")
        return normalized

    def _authority_mode(self) -> str:
        return (
            AUTHORITY_MODE_PERSON_APPROVAL
            if runtime_access(self.agent).enabled
            else AUTHORITY_MODE_AUTHORIZATION_DISABLED
        )

    @staticmethod
    def _require_team_lead(actor: ActingAgent) -> None:
        if actor.agent.agent_type != TEAM_ARCHITECT_AGENT_TYPE or actor.agent.role != "team_lead":
            raise PermissionError("Only TeamArchitect may provision generated agent definitions.")

    def _load_canonical_config(self) -> Any:
        from assistant_gateway.config import GatewayInstanceConfig

        configured_path = str(
            getattr(self.agent, "instance_config_path", "") or ""
        ).strip()
        if not configured_path:
            raise ValueError(
                "No instance_config.yaml path is available for generated-agent provisioning."
            )
        return GatewayInstanceConfig.load_saved(configured_path)

    @staticmethod
    def _catalog() -> ToolCatalog:
        return builtin_tool_catalog()

    def _composition_path(self, config: Any) -> Path:
        control = config.layout.control_dir
        path = resolve_team_composition_path(
            control_dir=control,
            explicit_path=getattr(self.agent, "team_composition_path", None),
        )
        if control not in path.parents:
            raise ValueError(
                f"Team composition path {path} is outside the control folder {control}."
            )
        return path

    @staticmethod
    def _plan_dir(config: Any) -> Path:
        path = config.instance_root_path / "runtime" / "generated_agent_plans"
        path.mkdir(parents=True, exist_ok=True)
        return path

    @staticmethod
    def _receipt_dir(config: Any) -> Path:
        path = config.instance_root_path / "runtime" / "generated_agent_receipts"
        path.mkdir(parents=True, exist_ok=True)
        return path

    def _receipt_path(self, config: Any, change_id: str) -> Path:
        normalized = str(change_id or "").strip()
        if not _CHANGE_ID_PATTERN.fullmatch(normalized):
            raise ValueError("Invalid generated-agent change id.")
        return self._receipt_dir(config) / f"{normalized}.json"

    def _rollback_receipt_path(self, config: Any, change_id: str) -> Path:
        normalized = str(change_id or "").strip()
        if not _CHANGE_ID_PATTERN.fullmatch(normalized):
            raise ValueError("Invalid generated-agent change id.")
        return self._receipt_dir(config) / f"{normalized}.rollback.json"

    @staticmethod
    def _plan_instructions(authority_mode: str) -> str:
        if authority_mode == AUTHORITY_MODE_PERSON_APPROVAL:
            return (
                "Explain the specialist and its default tools, then call "
                "apply_change(change_id, reason). A person approves the exact specialist in the Inbox; "
                "your task waits for the decision."
            )
        return (
            "Explain the specialist and its default tools; get the person's "
            "agreement in the conversation; then call apply_change(change_id, reason, confirmed=true)."
        )
