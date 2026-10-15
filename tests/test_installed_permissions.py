"""Installed procedures: inert proposals, installation by a person, and invocation.

Covers: an agent's proposal names only a handler and takes its person from the
signed task context; it stays inert until trusted code registers the handler;
installing (a person the gateway checked) writes the definition and a receipt
and nothing else, in particular no persistent policy; invoking runs the
handler with no second authorization (the per-call access hook decides
``procedure:<handler>``), but only for the definition's own person and team
and only while the handler registration is unchanged; rejecting records who
rejected it.
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from jsonschema import ValidationError as SchemaValidationError

from access_catalog.catalog import load_catalog
from access_control.identity import IdentityRejected
from assistant_gateway.access_setup import MembershipRoles, PersonChangeCheck
from assistant_gateway.application.procedures import (
    ProcedureActionRequestV1,
    ProcedureInstallResultV1,
    ProcedureListRequestV1,
)
from assistant_gateway.application.route_context import GatewayRouteContextV1
from assistant_gateway.config import GatewayInstanceConfig
from assistant_gateway.installed_procedures import GatewayInstalledProcedures
from custom_agents import access_runtime
from custom_agents.installed_permission_registry import load_trusted_installed_permission_registry
from custom_agents.installed_permissions import (
    InstalledPermissionService,
    resolve_installed_permission_paths,
)
from custom_messages.contracts.installed_permissions_v1 import PermissionCapabilityProposalViewV1
from custom_messages.contracts.access_v2 import AccessLevel
from tests.change_approval_support import SignedTaskManager, change_agent, needs_cedar, use_signed_contexts

HANDLER_ID = "test.reviewed.echo"
OWNER_ID = "usr_installed_permission_owner"
ADMIN_ID = "usr_installed_permission_admin"
TEAM_ID = "team_installed_permissions"


@pytest.fixture(autouse=True)
def _signed_task_contexts(monkeypatch: pytest.MonkeyPatch):
    use_signed_contexts(monkeypatch)
    yield
    access_runtime._runtime_access.cache_clear()


def _write_registry(tmp_path: Path, *, include_handler: bool = True, description: str = "") -> Path:
    path = tmp_path / "handlers.yaml"
    handlers = ""
    if include_handler:
        handlers = f"""
  {HANDLER_ID}:
    display_name: Reviewed Echo
    description: {description or "Return one schema-validated value from a trusted test handler."}
    input_schema:
      type: object
      additionalProperties: false
      required: [value]
      properties:
        value:
          type: string
          maxLength: 80
    impact_description: Returns the supplied value without external side effects.
    risk: low
    level: write
    manager_delegable: true
"""
    path.write_text(f"schema_version: 1\nhandlers:{handlers or ' {}'}\n", encoding="utf-8")
    return path


class _Handler:
    """The trusted handler compiled into the runtime (records its calls)."""

    def __init__(self) -> None:
        self.calls: list[tuple[Any, dict[str, Any]]] = []

    def __call__(self, agent: Any, arguments: dict[str, Any]) -> dict[str, Any]:
        self.calls.append((agent, dict(arguments)))
        return {"echo": arguments["value"]}


def _registry(tmp_path: Path, handler: _Handler, **kwargs: Any):
    return load_trusted_installed_permission_registry(
        registry_path=_write_registry(tmp_path, **kwargs), executors={HANDLER_ID: handler}
    )


def _setup(tmp_path: Path, *, authorization_enabled: bool = False):
    root = tmp_path / "instance"
    config = GatewayInstanceConfig.create(
        instance_name="installed-permission-test",
        instance_root=str(root),
        team_id=TEAM_ID,
        timezone="UTC",
    )
    config.user.canonical_user_id = OWNER_ID
    config_path = config.save(root / "instance_config.yaml")
    handler = _Handler()
    service = InstalledPermissionService(
        paths=resolve_installed_permission_paths(root), registry=_registry(tmp_path, handler)
    )
    specialist = change_agent(config, config_path, authorization_enabled=authorization_enabled, is_team_lead=False)
    return config, service, specialist, handler


def _propose(service: InstalledPermissionService, agent: Any) -> dict[str, Any]:
    return service.propose(
        agent=agent,
        handler_id=HANDLER_ID,
        display_name="Reviewed Echo",
        purpose="Exercise a registered low-risk operation.",
        justification="The person asked for an echo.",
    )


def _files(root: Path) -> set[str]:
    return {str(path.relative_to(root)) for path in root.rglob("*") if path.is_file()}


def test_unknown_handler_proposal_is_inert_until_registration(tmp_path: Path) -> None:
    _, service, specialist, _ = _setup(tmp_path)
    proposal = service.propose(
        agent=specialist,
        handler_id="future.unregistered.operation",
        display_name="Future Operation",
        purpose="Record a desired operation for administrator review.",
        justification="The handler does not exist yet.",
    )

    assert proposal["status"] == "pending_registration"
    assert proposal["handler_registered"] is False
    assert proposal["administrator_installable"] is False


def test_proposal_takes_its_person_from_the_signed_task_context(tmp_path: Path) -> None:
    _, service, specialist, _ = _setup(tmp_path)

    proposal = _propose(service, specialist)

    assert proposal["owner_canonical_user_id"] == OWNER_ID
    assert proposal["team_id"] == TEAM_ID
    assert proposal["proposer_principal_id"] == specialist.id
    assert proposal["proposer_named_agent_type"] == "CodeWritingAgent"
    assert proposal["source_task_id"] == specialist.task_manager.current_task_id
    specialist.task_manager.meta.gateway_context.task_context = None
    with pytest.raises(IdentityRejected):
        _propose(service, specialist)


def test_registry_requires_matching_code_and_metadata(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="code/metadata mismatch"):
        load_trusted_installed_permission_registry(
            registry_path=_write_registry(tmp_path),
            executors={},
        )


def test_registry_refuses_the_removed_authorization_mapping(tmp_path: Path) -> None:
    """Handlers name only their level; the old action/resource mapping is gone."""

    path = _write_registry(tmp_path)
    path.write_text(
        path.read_text(encoding="utf-8").replace(
            "    manager_delegable: true\n",
            "    manager_delegable: true\n    authorization:\n      component: old_mapping\n",
        ),
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="authorization"):
        load_trusted_installed_permission_registry(registry_path=path, executors={HANDLER_ID: _Handler()})


def test_existing_proposal_becomes_installable_after_trusted_registration(
    tmp_path: Path,
) -> None:
    config, service, specialist, _ = _setup(tmp_path)
    empty_registry = load_trusted_installed_permission_registry(
        registry_path=_write_registry(tmp_path, include_handler=False),
        executors={},
    )
    unregistered = InstalledPermissionService(
        paths=resolve_installed_permission_paths(config.instance_root_path),
        registry=empty_registry,
    )
    proposal = unregistered.propose(
        agent=specialist,
        handler_id=HANDLER_ID,
        display_name="Agent-Supplied Label",
        purpose="Wait for a trusted handler registration.",
        justification="The administrator should not be able to install this yet.",
    )
    assert proposal["status"] == "pending_registration"
    with pytest.raises(ValueError, match="not registered"):
        unregistered.install(proposal_id=proposal["proposal_id"], person_id=ADMIN_ID, team_id=TEAM_ID)

    [refreshed] = service.list_proposals(team_id=TEAM_ID)
    assert refreshed["status"] == "ready_for_admin"
    assert refreshed["administrator_installable"] is True
    assert refreshed["trusted_handler"]["display_name"] == "Reviewed Echo"


def test_installing_a_procedure_writes_only_its_definition_and_receipt(tmp_path: Path) -> None:
    config, service, specialist, handler = _setup(tmp_path)
    proposal = _propose(service, specialist)
    before = _files(config.instance_root_path)

    installed = service.install(
        proposal_id=proposal["proposal_id"],
        person_id=ADMIN_ID,
        team_id=TEAM_ID,
        explanation="The person needs a reviewed echo.",
    )

    definition = installed["definition"]
    receipt = installed["receipt"]
    assert installed["status"] == "installed"
    assert installed["proposal"]["status"] == "installed"
    assert installed["proposal"]["resolved_by_principal_id"] == ADMIN_ID
    assert definition["installed_by_principal_id"] == ADMIN_ID
    assert definition["owner_canonical_user_id"] == OWNER_ID
    assert definition["team_id"] == TEAM_ID
    assert definition["handler_id"] == HANDLER_ID
    assert receipt["installed_by_principal_id"] == ADMIN_ID
    assert receipt["explanation"] == "The person needs a reviewed echo."
    assert receipt["definition_id"] == definition["definition_id"]
    assert not {key for key in receipt if "policy" in key or "snapshot" in key}
    assert _files(config.instance_root_path) - before == {
        f"runtime/installed-permissions/installed/{definition['definition_id']}.json",
        f"runtime/installed-permissions/receipts/{proposal['proposal_id']}.json",
    }, "no persistent policy or other authority is written"
    assert handler.calls == []
    with pytest.raises(ValueError, match="already installed"):
        service.install(proposal_id=proposal["proposal_id"], person_id=ADMIN_ID, team_id=TEAM_ID)
    [listed] = service.list_installed(team_id=TEAM_ID)
    assert listed["handler_registration_valid"] is True


def test_procedure_results_match_the_gateway_contracts(tmp_path: Path) -> None:
    """The gateway validates what these services return before the browser sees it."""

    _, service, specialist, _ = _setup(tmp_path)
    installed_proposal = _propose(service, specialist)
    rejected_proposal = _propose(service, specialist)
    listed = service.list_proposals(team_id=TEAM_ID)
    installed = service.install(proposal_id=installed_proposal["proposal_id"], person_id=ADMIN_ID, team_id=TEAM_ID)
    rejected = service.reject(proposal_id=rejected_proposal["proposal_id"], person_id=ADMIN_ID, team_id=TEAM_ID, reason="No.")

    # As GatewayInstalledProcedures.list_proposals/install/reject do:
    for item in listed:
        PermissionCapabilityProposalViewV1.model_validate(item)
    ProcedureInstallResultV1.model_validate(installed)
    PermissionCapabilityProposalViewV1.model_validate(rejected)


class _Identity:
    def __init__(self, roles: dict[str, str]) -> None:
        self.roles = roles

    def get_gateway_user(self, person_id: str) -> Any:
        return SimpleNamespace(status="active", display_name=person_id) if person_id in self.roles else None

    def list_team_memberships(self, person_id: str, status: str = "active") -> list[Any]:
        role = self.roles.get(person_id)
        return [SimpleNamespace(team_id=TEAM_ID, role=role)] if role else []


class _SignedInAs:
    """Tests name the signed-in person in the route; the gateway reads it from the session."""

    def person_id(self, context: GatewayRouteContextV1) -> str:
        return str(context.canonical_user_id or "")


def _route(person_id: str) -> GatewayRouteContextV1:
    return GatewayRouteContextV1(session_key="session-test", canonical_user_id=person_id)


@needs_cedar
@pytest.mark.asyncio
async def test_administrators_review_proposals_in_the_gateway_and_members_cannot(tmp_path: Path) -> None:
    config, service, specialist, handler = _setup(tmp_path, authorization_enabled=True)
    config.authorization.enabled = True
    added = _propose(service, specialist)
    declined = _propose(service, specialist)
    changes = PersonChangeCheck(config, MembershipRoles(_Identity({ADMIN_ID: "admin", "usr_member": "member"})))
    procedures = GatewayInstalledProcedures(
        config, person_changes=changes, signed_in=_SignedInAs(), service_factory=lambda: service
    )
    try:
        for person_id in ("usr_member", "usr_stranger"):
            with pytest.raises(PermissionError, match="Only an administrator"):
                procedures.list_proposals(ProcedureListRequestV1(context=_route(person_id)))
            with pytest.raises(PermissionError, match="Only an administrator"):
                await procedures.install(
                    ProcedureActionRequestV1(context=_route(person_id), proposal_id=added["proposal_id"])
                )

        listed = procedures.list_proposals(ProcedureListRequestV1(context=_route(ADMIN_ID)))
        assert listed.total == 2
        assert {item.administrator_installable for item in listed.proposals} == {True}
        result = await procedures.install(
            ProcedureActionRequestV1(context=_route(ADMIN_ID), proposal_id=added["proposal_id"], explanation="Checked.")
        )
        rejected = procedures.reject(
            ProcedureActionRequestV1(context=_route(ADMIN_ID), proposal_id=declined["proposal_id"])
        )
        installed = procedures.list_installed(ProcedureListRequestV1(context=_route(ADMIN_ID)))
    finally:
        changes.close()

    assert result.receipt.installed_by_principal_id == ADMIN_ID
    assert result.receipt.explanation == "Checked."
    assert rejected.status.value == "rejected"
    assert rejected.resolution_reason == "administrator_rejected"
    [view] = installed.definitions
    assert view.definition.definition_id == result.definition.definition_id
    assert view.handler_registration_valid is True
    assert handler.calls == [], "adding a procedure runs nothing"


def test_installing_for_another_team_is_refused(tmp_path: Path) -> None:
    config, service, specialist, _ = _setup(tmp_path)
    proposal = _propose(service, specialist)

    with pytest.raises(PermissionError, match="administrator of the proposal's team"):
        service.install(proposal_id=proposal["proposal_id"], person_id=ADMIN_ID, team_id="team_other")
    assert service.list_installed(team_id=TEAM_ID) == []


def test_invoking_runs_the_handler_for_the_definitions_own_person(tmp_path: Path) -> None:
    _, service, specialist, handler = _setup(tmp_path)
    proposal = _propose(service, specialist)
    definition_id = service.install(proposal_id=proposal["proposal_id"], person_id=ADMIN_ID, team_id=TEAM_ID)[
        "definition"
    ]["definition_id"]

    executed = service.invoke(agent=specialist, definition_id=definition_id, arguments={"value": "hello"})

    assert executed["status"] == "executed"
    assert executed["result"] == {"echo": "hello"}
    assert executed["handler_id"] == HANDLER_ID
    assert "authorization" not in executed
    assert handler.calls == [(specialist, {"value": "hello"})]
    with pytest.raises(SchemaValidationError):
        service.invoke(agent=specialist, definition_id=definition_id, arguments={"value": "x", "extra": True})
    assert len(handler.calls) == 1


@needs_cedar
def test_invoking_needs_no_second_authorization_with_authorization_on(tmp_path: Path) -> None:
    """The agent holds no grant here: the per-call hook decides, the service does not decide again."""

    _, service, specialist, handler = _setup(tmp_path, authorization_enabled=True)
    proposal = _propose(service, specialist)
    definition_id = service.install(proposal_id=proposal["proposal_id"], person_id=ADMIN_ID, team_id=TEAM_ID)[
        "definition"
    ]["definition_id"]

    executed = service.invoke(agent=specialist, definition_id=definition_id, arguments={"value": "hello"})

    assert executed["status"] == "executed"
    assert handler.calls == [(specialist, {"value": "hello"})]


def test_the_access_hook_decides_invocation_as_the_handlers_procedure_tool(tmp_path: Path) -> None:
    _, service, specialist, _ = _setup(tmp_path)
    proposal = _propose(service, specialist)
    definition_id = service.install(proposal_id=proposal["proposal_id"], person_id=ADMIN_ID, team_id=TEAM_ID)[
        "definition"
    ]["definition_id"]
    catalog = load_catalog(None)
    catalog.load_procedures(_write_registry(tmp_path))

    resolution = catalog.resolve_function("invoke_installed_permission", {"definition_id": definition_id})
    unknown = catalog.resolve_function(
        "invoke_installed_permission", {"definition_id": "installed_permission.other.handler.0123456789abcdef"}
    )

    assert (resolution.tool_id, resolution.operation, resolution.level) == (
        f"procedure:{HANDLER_ID}",
        "invoke",
        AccessLevel.WRITE,
    )
    assert (unknown.tool_id, unknown.level) == ("procedure:unregistered", AccessLevel.FULL)


def test_invoking_a_procedure_of_another_person_or_team_is_refused(tmp_path: Path) -> None:
    config, service, specialist, handler = _setup(tmp_path)
    proposal = _propose(service, specialist)
    definition_id = service.install(proposal_id=proposal["proposal_id"], person_id=ADMIN_ID, team_id=TEAM_ID)[
        "definition"
    ]["definition_id"]

    other_person = SimpleNamespace(
        **{
            **vars(specialist),
            "task_manager": SignedTaskManager(
                team_id=TEAM_ID, person_id="usr_someone_else", storage_settings=config.manager_storage_settings
            ),
        }
    )
    other_team = SimpleNamespace(
        **{
            **vars(specialist),
            "team_id": "team_other",
            "task_manager": SignedTaskManager(
                team_id="team_other", person_id=OWNER_ID, storage_settings=config.manager_storage_settings
            ),
        }
    )
    for agent in (other_person, other_team):
        with pytest.raises(PermissionError, match="another owner or team"):
            service.invoke(agent=agent, definition_id=definition_id, arguments={"value": "hello"})
    assert handler.calls == []


def test_a_changed_handler_registration_stops_invocation(tmp_path: Path) -> None:
    config, service, specialist, _ = _setup(tmp_path)
    proposal = _propose(service, specialist)
    definition_id = service.install(proposal_id=proposal["proposal_id"], person_id=ADMIN_ID, team_id=TEAM_ID)[
        "definition"
    ]["definition_id"]
    changed_handler = _Handler()
    changed = InstalledPermissionService(
        paths=resolve_installed_permission_paths(config.instance_root_path),
        registry=_registry(tmp_path, changed_handler, description="A changed description after installation."),
    )

    with pytest.raises(PermissionError, match="registration changed after installation"):
        changed.invoke(agent=specialist, definition_id=definition_id, arguments={"value": "hello"})
    assert changed_handler.calls == []
    assert changed.list_installed(team_id=TEAM_ID)[0]["handler_registration_valid"] is False


def test_rejecting_a_proposal_records_who_rejected_it(tmp_path: Path) -> None:
    _, service, specialist, _ = _setup(tmp_path)
    proposal = _propose(service, specialist)

    rejected = service.reject(proposal_id=proposal["proposal_id"], person_id=ADMIN_ID, team_id=TEAM_ID, reason="Not needed.")

    assert rejected["status"] == "rejected"
    assert (rejected["resolved_by_principal_id"], rejected["resolution_reason"]) == (ADMIN_ID, "Not needed.")
    assert rejected["administrator_installable"] is False
    with pytest.raises(ValueError, match="already rejected"):
        service.reject(proposal_id=proposal["proposal_id"], person_id=ADMIN_ID, team_id=TEAM_ID, reason="Again.")
    with pytest.raises(ValueError, match="already rejected"):
        service.install(proposal_id=proposal["proposal_id"], person_id=ADMIN_ID, team_id=TEAM_ID)


def test_an_administrator_of_another_team_cannot_reject_a_proposal(tmp_path: Path) -> None:
    _, service, specialist, _ = _setup(tmp_path)
    proposal = _propose(service, specialist)

    with pytest.raises(PermissionError, match="proposal's team"):
        service.reject(proposal_id=proposal["proposal_id"], person_id=ADMIN_ID, team_id="another_team", reason="No.")
