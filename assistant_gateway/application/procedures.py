"""Installed procedures in the gateway: reviewing agents' proposals (application contracts).

An installed procedure is a fixed operation a developer built into trusted
runtime code (``custom_agents.installed_permissions``). An agent may propose
using a registered handler; the proposal is inert text. An administrator adds
or rejects it in Settings → Team; adding creates the definition only, and
calling it is then the catalog tool ``procedure:<handler>``, checked on every
call like any other tool. The implementation is
``assistant_gateway.installed_procedures``.
"""

from __future__ import annotations

from datetime import datetime
from typing import Literal, Protocol

from pydantic import Field

from assistant_gateway.application.route_context import (
    GatewayRouteContextV1,
    GatewayRouteContractV1,
)
from custom_messages.contracts.installed_permissions_v1 import (
    InstalledPermissionDefinitionV1,
    PermissionCapabilityProposalViewV1,
)


class ProcedureListRequestV1(GatewayRouteContractV1):
    context: GatewayRouteContextV1
    limit: int = Field(default=20, ge=1, le=500)
    offset: int = Field(default=0, ge=0)


class ProcedureProposalListV1(GatewayRouteContractV1):
    has_more: bool = False
    total: int = Field(default=0, ge=0)
    proposals: tuple[PermissionCapabilityProposalViewV1, ...]


class InstalledProcedureViewV1(GatewayRouteContractV1):
    definition: InstalledPermissionDefinitionV1
    handler_registration_valid: bool


class InstalledProcedureListV1(GatewayRouteContractV1):
    has_more: bool = False
    total: int = Field(default=0, ge=0)
    definitions: tuple[InstalledProcedureViewV1, ...]


class InstalledProcedureReceiptV1(GatewayRouteContractV1):
    receipt_id: str = Field(min_length=1, max_length=240)
    proposal_id: str = Field(min_length=1, max_length=240)
    proposal_digest: str = Field(min_length=64, max_length=64)
    definition_id: str = Field(min_length=1, max_length=240)
    definition_digest: str = Field(min_length=64, max_length=64)
    handler_id: str = Field(min_length=1, max_length=240)
    installed_at_utc: datetime
    installed_by_principal_id: str = Field(min_length=1, max_length=240)
    explanation: str | None = Field(default=None, max_length=4000)


class ProcedureActionRequestV1(GatewayRouteContractV1):
    context: GatewayRouteContextV1
    proposal_id: str = Field(min_length=1, max_length=240)
    explanation: str | None = Field(default=None, max_length=4000)
    reason: str | None = Field(default=None, max_length=2000)


class ProcedureInstallResultV1(GatewayRouteContractV1):
    status: Literal["installed"] = "installed"
    proposal: PermissionCapabilityProposalViewV1
    definition: InstalledPermissionDefinitionV1
    receipt: InstalledProcedureReceiptV1


class ProcedureApplication(Protocol):
    """What Settings → Team needs; every call acts as the route's signed-in administrator."""

    def list_proposals(self, request: ProcedureListRequestV1) -> ProcedureProposalListV1: ...

    def list_installed(self, request: ProcedureListRequestV1) -> InstalledProcedureListV1: ...

    async def install(self, request: ProcedureActionRequestV1) -> ProcedureInstallResultV1: ...

    def reject(self, request: ProcedureActionRequestV1) -> PermissionCapabilityProposalViewV1: ...


__all__ = [
    "InstalledProcedureListV1",
    "InstalledProcedureReceiptV1",
    "InstalledProcedureViewV1",
    "ProcedureActionRequestV1",
    "ProcedureApplication",
    "ProcedureInstallResultV1",
    "ProcedureListRequestV1",
    "ProcedureProposalListV1",
]
