"""Installed procedures: inert proposals and administrator-installed handlers.

An agent proposes a procedure by naming a trusted handler (prose and a
handler id only). An administrator installs it in the gateway (the gateway
checks the person through the access service, change kind
``installed_procedure``); installing writes the definition, nothing else.
Using it is the catalog tool ``procedure:<handler>``: the per-call access hook
decides each ``invoke_installed_permission`` call like any other tool, and
this module then checks the definition belongs to the task's person and team.
"""

from __future__ import annotations
from collections.abc import Iterator
from itertools import islice

from datetime import datetime
import hashlib
import json
import os
from pathlib import Path
from typing import Any, Mapping

from pydantic import BaseModel, ConfigDict

from custom_agents.access_runtime import acting_agent

from custom_messages.contracts.installed_permissions_v1 import (
    INSTALLED_PERMISSION_SCHEMA_VERSION,
    InstalledPermissionDefinitionV1,
    PermissionCapabilityProposalStatus,
    PermissionCapabilityProposalV1,
    normalize_installed_permission_definition_id,
    normalize_permission_proposal_id,
)
from custom_agents.installed_permission_registry import (
    TrustedInstalledPermissionHandler,
    TrustedInstalledPermissionRegistry,
    load_trusted_installed_permission_registry,
)
from custom_messages.contracts.common_v1 import (
    utc_now,
)
from custom_agents.runtime_paths import resolve_runtime_instance_root
from id_generation import new_id, new_id_suffix
from runtime_settings.instance_layout import InstanceLayout


INSTALLED_PERMISSION_COMPONENT = "installed_permission_executor"


# The catalog change kind that decides who may install or reject procedures
# (administrators; functions.yaml fn:capabilities ``changes:``).
INSTALLED_PROCEDURE_CHANGE_KIND = "installed_procedure"


class InstalledPermissionPaths(BaseModel):
    model_config = ConfigDict(arbitrary_types_allowed=True, frozen=True)

    root: Path
    proposals: Path
    installed: Path
    receipts: Path

    def ensure(self) -> None:
        for path in (self.root, self.proposals, self.installed, self.receipts):
            path.mkdir(parents=True, mode=0o700, exist_ok=True)


def resolve_installed_permission_paths(
    instance_root: str | Path,
) -> InstalledPermissionPaths:
    root = InstanceLayout.at(instance_root).installed_permissions_dir
    return InstalledPermissionPaths(
        root=root,
        proposals=root / "proposals",
        installed=root / "installed",
        receipts=root / "receipts",
    )


def resolve_agent_installed_permission_paths(agent: Any) -> InstalledPermissionPaths:
    instance_root = resolve_runtime_instance_root(
        bootstrap_config=dict(getattr(agent, "bootstrap_config", {}) or {}),
        instance_config_path=getattr(agent, "instance_config_path", None),
        instance_root=getattr(agent, "instance_root", None),
        workspace_dir=getattr(agent, "workspace_dir", None),
    )
    if not instance_root:
        raise RuntimeError(
            "installed permissions require a resolvable runtime instance root"
        )
    return resolve_installed_permission_paths(instance_root)


def _atomic_write_json(
    path: Path, value: Mapping[str, Any], *, immutable: bool = False
) -> None:
    path.parent.mkdir(parents=True, mode=0o700, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{new_id_suffix()}.tmp")
    try:
        temporary.write_text(
            json.dumps(dict(value), indent=2, sort_keys=True, default=str) + "\n",
            encoding="utf-8",
        )
        os.replace(temporary, path)
        if immutable:
            path.chmod(0o444)
    finally:
        temporary.unlink(missing_ok=True)


def _definition_for_handler(
    *,
    proposal: PermissionCapabilityProposalV1,
    handler: TrustedInstalledPermissionHandler,
    installed_by: str,
    now: datetime,
) -> InstalledPermissionDefinitionV1:
    descriptor = handler.descriptor
    definition_id = f"installed_permission.{handler.descriptor.handler_id}.{handler.descriptor_digest[:16]}"
    return InstalledPermissionDefinitionV1.build(
        {
            "definition_id": definition_id,
            "proposal_id": proposal.proposal_id,
            "installed_at_utc": now,
            "installed_by_principal_id": installed_by,
            "team_id": proposal.team_id,
            "owner_canonical_user_id": proposal.owner_canonical_user_id,
            "handler_id": descriptor.handler_id,
            "handler_descriptor_digest": handler.descriptor_digest,
            "input_schema_digest": handler.input_schema_digest,
            "display_name": descriptor.display_name,
            "description": descriptor.description,
            "impact_description": descriptor.impact_description,
            "risk": descriptor.risk,
            "manager_delegable": descriptor.manager_delegable,
        }
    )




class InstalledPermissionService:
    def __init__(
        self,
        *,
        paths: InstalledPermissionPaths,
        registry: TrustedInstalledPermissionRegistry | None = None,
    ) -> None:
        self.paths = paths
        self.registry = registry or load_trusted_installed_permission_registry()
        self.paths.ensure()

    @classmethod
    def for_agent(
        cls,
        agent: Any,
        *,
        registry: TrustedInstalledPermissionRegistry | None = None,
    ) -> "InstalledPermissionService":
        return cls(
            paths=resolve_agent_installed_permission_paths(agent), registry=registry
        )

    def propose(
        self,
        *,
        agent: Any,
        handler_id: str,
        display_name: str,
        purpose: str,
        justification: str,
    ) -> dict[str, Any]:
        actor = acting_agent(agent)
        task_id = actor.task_id
        if not actor.person_id or not task_id:
            raise PermissionError(
                "installed permission proposals require an active task that works for a person"
            )
        normalized_handler_id = str(handler_id or "").strip()
        now = utc_now()
        registered = self.registry.get(normalized_handler_id)
        proposal = PermissionCapabilityProposalV1.build(
            {
                "proposal_id": new_id("perm_cap_prop"),
                "created_at_utc": now,
                "updated_at_utc": now,
                "proposer_principal_id": actor.agent.agent_id,
                "proposer_named_agent_type": actor.agent.agent_type,
                "team_id": actor.agent.team_id,
                "owner_canonical_user_id": actor.person_id,
                "source_task_id": task_id,
                "requested_handler_id": normalized_handler_id,
                "display_name": str(display_name or "").strip(),
                "purpose": str(purpose or "").strip(),
                "justification": str(justification or "").strip(),
                "status": (
                    PermissionCapabilityProposalStatus.READY_FOR_ADMIN
                    if registered is not None
                    else PermissionCapabilityProposalStatus.PENDING_REGISTRATION
                ),
            }
        )
        _atomic_write_json(
            self.paths.proposals / f"{proposal.proposal_id}.json",
            proposal.model_dump(mode="json"),
            immutable=False,
        )
        return self._proposal_payload(proposal)

    def list_proposals(
        self, *, team_id: str, limit: int | None = None, offset: int = 0
    ) -> list[dict[str, Any]]:
        return list(
            islice(
                self.iter_proposals(team_id=team_id),
                offset,
                None if limit is None else offset + limit,
            )
        )

    def iter_proposals(self, *, team_id: str) -> Iterator[dict[str, Any]]:
        for path in sorted(self.paths.proposals.glob("perm_cap_prop_*.json")):
            proposal = PermissionCapabilityProposalV1.model_validate_json(
                path.read_text(encoding="utf-8")
            )
            if proposal.team_id == str(team_id or "").strip():
                yield self._proposal_payload(proposal)

    def list_installed(
        self, *, team_id: str, limit: int | None = None, offset: int = 0
    ) -> list[dict[str, Any]]:
        return list(
            islice(
                self.iter_installed(team_id=team_id),
                offset,
                None if limit is None else offset + limit,
            )
        )

    def iter_installed(self, *, team_id: str) -> Iterator[dict[str, Any]]:
        for path in sorted(self.paths.installed.glob("installed_permission.*.json")):
            definition = self._load_definition_path(path)
            if definition.team_id == str(team_id or "").strip():
                handler = self.registry.get(definition.handler_id)
                yield (
                    {
                        **definition.model_dump(mode="json"),
                        "handler_registration_valid": self._handler_matches(
                            definition, handler
                        ),
                    }
                )

    def install(
        self,
        *,
        proposal_id: str,
        person_id: str,
        team_id: str,
        explanation: str = "",
    ) -> dict[str, Any]:
        """Install a registered handler for a proposal (the gateway checked the person).

        Installing adds the procedure definition only; agents use it when a
        grant covers its catalog tool (``procedure:<handler>``), decided on
        every call.
        """

        proposal = self._load_proposal(proposal_id)
        if proposal.status in {
            PermissionCapabilityProposalStatus.INSTALLED,
            PermissionCapabilityProposalStatus.REJECTED,
        }:
            raise ValueError(f"proposal is already {proposal.status.value}")
        if team_id != proposal.team_id:
            raise PermissionError("an administrator of the proposal's team is required")
        handler = self.registry.require(proposal.requested_handler_id)
        now = utc_now()
        definition = _definition_for_handler(
            proposal=proposal,
            handler=handler,
            installed_by=person_id,
            now=now,
        )
        definition_path = self.paths.installed / f"{definition.definition_id}.json"
        if definition_path.exists():
            existing = self._load_definition_path(definition_path)
            if (
                existing.proposal_id != proposal.proposal_id
                or existing.team_id != proposal.team_id
                or existing.owner_canonical_user_id != proposal.owner_canonical_user_id
                or existing.handler_id != handler.descriptor.handler_id
                or existing.handler_descriptor_digest != handler.descriptor_digest
                or existing.input_schema_digest != handler.input_schema_digest
            ):
                raise PermissionError("installed permission definition id collision")
            definition = existing
        else:
            _atomic_write_json(
                definition_path,
                definition.model_dump(mode="json"),
                immutable=True,
            )
        receipt = {
            "schema_version": INSTALLED_PERMISSION_SCHEMA_VERSION,
            "receipt_id": new_id("perm_def_rcpt"),
            "proposal_id": proposal.proposal_id,
            "proposal_digest": proposal.proposal_digest,
            "definition_id": definition.definition_id,
            "definition_digest": definition.definition_digest,
            "handler_id": definition.handler_id,
            "installed_at_utc": now.isoformat(),
            "installed_by_principal_id": person_id,
            "explanation": str(explanation or "").strip() or None,
        }
        receipt_path = self.paths.receipts / f"{proposal.proposal_id}.json"
        if not receipt_path.exists():
            _atomic_write_json(receipt_path, receipt, immutable=True)
        resolved = proposal.model_copy(
            update={
                "status": PermissionCapabilityProposalStatus.INSTALLED,
                "updated_at_utc": now,
                "resolved_at_utc": now,
                "resolved_by_principal_id": person_id,
                "resolution_reason": "trusted_handler_installed",
                "installed_definition_id": definition.definition_id,
            }
        )
        _atomic_write_json(
            self.paths.proposals / f"{proposal.proposal_id}.json",
            resolved.model_dump(mode="json"),
        )
        return {
            "status": "installed",
            "proposal": self._proposal_payload(resolved),
            "definition": definition.model_dump(mode="json"),
            "receipt": receipt,
        }

    def reject(
        self,
        *,
        proposal_id: str,
        person_id: str,
        team_id: str,
        reason: str,
    ) -> dict[str, Any]:
        """Reject a proposal (the gateway checked the person)."""

        proposal = self._load_proposal(proposal_id)
        if team_id != proposal.team_id:
            raise PermissionError("an administrator of the proposal's team is required")
        if proposal.status in {
            PermissionCapabilityProposalStatus.INSTALLED,
            PermissionCapabilityProposalStatus.REJECTED,
        }:
            raise ValueError(f"proposal is already {proposal.status.value}")
        now = utc_now()
        resolved = proposal.model_copy(
            update={
                "status": PermissionCapabilityProposalStatus.REJECTED,
                "updated_at_utc": now,
                "resolved_at_utc": now,
                "resolved_by_principal_id": person_id,
                "resolution_reason": str(reason or "administrator_rejected").strip(),
            }
        )
        _atomic_write_json(
            self.paths.proposals / f"{proposal.proposal_id}.json",
            resolved.model_dump(mode="json"),
        )
        return self._proposal_payload(resolved)

    def invoke(
        self,
        *,
        agent: Any,
        definition_id: str,
        arguments: Mapping[str, Any],
    ) -> dict[str, Any]:
        """Run an installed procedure; the per-call access hook already decided it.

        The hook resolves ``invoke_installed_permission`` to the definition's
        catalog tool (``procedure:<handler>``) and checks the agent and its
        person. Here trusted code checks the definition belongs to the task's
        person and team and that its handler registration is unchanged.
        """

        definition = self._load_definition(definition_id)
        actor = acting_agent(agent)
        if (
            actor.agent.team_id != definition.team_id
            or actor.person_id != definition.owner_canonical_user_id
        ):
            raise PermissionError(
                "installed permission belongs to another owner or team"
            )
        handler = self.registry.require(definition.handler_id)
        if not self._handler_matches(definition, handler):
            raise PermissionError(
                "installed permission handler registration changed after installation"
            )
        normalized_arguments = handler.validate_arguments(arguments)
        invocation_digest = hashlib.sha256(
            json.dumps(
                normalized_arguments,
                sort_keys=True,
                separators=(",", ":"),
                ensure_ascii=False,
            ).encode("utf-8")
        ).hexdigest()
        result = handler.executor(agent, normalized_arguments)
        return {
            "status": "executed",
            "definition_id": definition.definition_id,
            "definition_digest": definition.definition_digest,
            "handler_id": definition.handler_id,
            "invocation_digest": invocation_digest,
            "result": result,
        }

    def _proposal_payload(
        self, proposal: PermissionCapabilityProposalV1
    ) -> dict[str, Any]:
        registered = self.registry.get(proposal.requested_handler_id)
        payload = proposal.model_dump(mode="json")
        if proposal.status not in {
            PermissionCapabilityProposalStatus.INSTALLED,
            PermissionCapabilityProposalStatus.REJECTED,
        }:
            payload["status"] = (
                PermissionCapabilityProposalStatus.READY_FOR_ADMIN.value
                if registered is not None
                else PermissionCapabilityProposalStatus.PENDING_REGISTRATION.value
            )
        payload.update(
            {
                "handler_registered": registered is not None,
                "administrator_installable": (
                    registered is not None
                    and proposal.status
                    not in {
                        PermissionCapabilityProposalStatus.INSTALLED,
                        PermissionCapabilityProposalStatus.REJECTED,
                    }
                ),
                "trusted_handler": (
                    {
                        "handler_id": registered.descriptor.handler_id,
                        "display_name": registered.descriptor.display_name,
                        "description": registered.descriptor.description,
                        "impact_description": registered.descriptor.impact_description,
                        "risk": registered.descriptor.risk.value,
                        "level": registered.descriptor.level.value,
                        "handler_descriptor_digest": registered.descriptor_digest,
                        "input_schema_digest": registered.input_schema_digest,
                    }
                    if registered is not None
                    else None
                ),
            }
        )
        return payload

    def _load_proposal(self, proposal_id: str) -> PermissionCapabilityProposalV1:
        normalized = normalize_permission_proposal_id(proposal_id)
        path = self.paths.proposals / f"{normalized}.json"
        if not path.is_file():
            raise FileNotFoundError(
                f"installed permission proposal was not found: {normalized}"
            )
        return PermissionCapabilityProposalV1.model_validate_json(
            path.read_text(encoding="utf-8")
        )

    def _load_definition(self, definition_id: str) -> InstalledPermissionDefinitionV1:
        normalized = normalize_installed_permission_definition_id(definition_id)
        path = self.paths.installed / f"{normalized}.json"
        if not path.is_file():
            raise FileNotFoundError(
                f"installed permission definition was not found: {normalized}"
            )
        return self._load_definition_path(path)

    @staticmethod
    def _load_definition_path(path: Path) -> InstalledPermissionDefinitionV1:
        return InstalledPermissionDefinitionV1.model_validate_json(
            path.read_text(encoding="utf-8")
        )

    @staticmethod
    def _handler_matches(
        definition: InstalledPermissionDefinitionV1,
        handler: TrustedInstalledPermissionHandler | None,
    ) -> bool:
        return bool(
            handler is not None
            and definition.handler_descriptor_digest == handler.descriptor_digest
            and definition.input_schema_digest == handler.input_schema_digest
            and definition.manager_delegable == handler.descriptor.manager_delegable
            and definition.risk == handler.descriptor.risk
        )


__all__ = [
    "INSTALLED_PROCEDURE_CHANGE_KIND",
    "INSTALLED_PERMISSION_COMPONENT",
    "INSTALLED_PERMISSION_SCHEMA_VERSION",
    "InstalledPermissionDefinitionV1",
    "InstalledPermissionPaths",
    "InstalledPermissionService",
    "PermissionCapabilityProposalStatus",
    "PermissionCapabilityProposalV1",
    "resolve_agent_installed_permission_paths",
    "resolve_installed_permission_paths",
]
