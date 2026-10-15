"""Installed-procedure reviews in the gateway (``ProcedureApplication``).

Administrators list agents' proposals to use a registered procedure handler,
add them (which creates the procedure definition only) or reject them; they
also see the procedures already installed. Every call acts as the route's
signed-in person and requires that they may make the ``installed_procedure``
change (``PersonChangeCheck``: administrators with authorization on). Access
to call an installed procedure is granted separately, like any tool
(``procedure:<handler>``). See ``assistant_gateway.application.procedures``.
"""

from __future__ import annotations

from typing import Any, Callable

from assistant_gateway.access_setup import PersonChangeCheck
from assistant_gateway.application.procedures import (
    InstalledProcedureListV1,
    InstalledProcedureViewV1,
    ProcedureActionRequestV1,
    ProcedureInstallResultV1,
    ProcedureListRequestV1,
    ProcedureProposalListV1,
)
from assistant_gateway.application.route_context import GatewayRouteContextV1, SignedInPerson
from custom_agents.installed_permissions import (
    INSTALLED_PROCEDURE_CHANGE_KIND,
    InstalledPermissionService,
    resolve_installed_permission_paths,
)
from custom_messages.contracts.installed_permissions_v1 import (
    InstalledPermissionDefinitionV1,
    PermissionCapabilityProposalViewV1,
)
from storage.pagination import counted_page


class GatewayInstalledProcedures:
    def __init__(
        self,
        config: Any,
        *,
        person_changes: PersonChangeCheck,
        signed_in: SignedInPerson,
        service_factory: Callable[[], InstalledPermissionService] | None = None,
    ) -> None:
        instance_root = config.instance_root_path
        self._team_id = str(config.manager.team_id)
        self._changes = person_changes
        self._signed_in = signed_in
        # A fresh service per call reads the handler registry as it is now.
        self._service = service_factory or (
            lambda: InstalledPermissionService(paths=resolve_installed_permission_paths(instance_root))
        )

    def _administrator(self, context: GatewayRouteContextV1) -> str:
        """The signed-in person, when they may add or reject procedures."""

        return self._changes.require(self._signed_in.person_id(context), INSTALLED_PROCEDURE_CHANGE_KIND)

    def list_proposals(self, request: ProcedureListRequestV1) -> ProcedureProposalListV1:
        self._administrator(request.context)
        page = counted_page(
            self._service().iter_proposals(team_id=self._team_id), offset=request.offset, limit=request.limit
        )
        return ProcedureProposalListV1(
            proposals=tuple(PermissionCapabilityProposalViewV1.model_validate(item) for item in page.items),
            has_more=page.has_more,
            total=page.total,
        )

    def list_installed(self, request: ProcedureListRequestV1) -> InstalledProcedureListV1:
        self._administrator(request.context)
        page = counted_page(
            self._service().iter_installed(team_id=self._team_id), offset=request.offset, limit=request.limit
        )
        return InstalledProcedureListV1(
            definitions=tuple(
                InstalledProcedureViewV1(
                    # The service adds the registry check next to the stored definition.
                    definition=InstalledPermissionDefinitionV1.model_validate(
                        {key: value for key, value in item.items() if key != "handler_registration_valid"}
                    ),
                    handler_registration_valid=bool(item.get("handler_registration_valid", False)),
                )
                for item in page.items
            ),
            has_more=page.has_more,
            total=page.total,
        )

    async def install(self, request: ProcedureActionRequestV1) -> ProcedureInstallResultV1:
        person_id = self._administrator(request.context)
        return ProcedureInstallResultV1.model_validate(
            self._service().install(
                proposal_id=request.proposal_id,
                person_id=person_id,
                team_id=self._team_id,
                explanation=str(request.explanation or "").strip(),
            )
        )

    def reject(self, request: ProcedureActionRequestV1) -> PermissionCapabilityProposalViewV1:
        return PermissionCapabilityProposalViewV1.model_validate(
            self._service().reject(
                proposal_id=request.proposal_id,
                person_id=self._administrator(request.context),
                team_id=self._team_id,
                reason=request.reason or "administrator_rejected",
            )
        )


__all__ = ["GatewayInstalledProcedures"]
