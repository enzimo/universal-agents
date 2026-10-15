"""The ``mcp`` and ``procedures`` transfer sections carry installed MCP servers and installed procedures."""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from team_transfer.container import TransferReader
from team_transfer.crypto import new_key_derivation
from team_transfer.integrations import McpSection, ProceduresSection
from team_transfer.sections import RecordOutcome
from team_transfer.service import export_source, export_team, import_team
from team_transfer.settings import SettingsSection
from tests.pinned_memory_support import new_instance
from tests.test_installed_permissions import HANDLER_ID, _Handler, _write_registry

PASSPHRASE = "correct horse battery staple"
FAST_KEYS = {"opslimit": 1, "memlimit": 8192}
DOCS = {"name": "docs", "transport": "streamable_http", "server_url": "https://mcp.example.test/mcp"}
LATER = {"name": "later", "transport": "streamable_http", "server_url": "https://later.example.test/mcp"}


def _transfer(tmp_path: Path, config_path: Path, sections: tuple[Any, ...], import_sections: tuple[Any, ...] | None = None):
    out = tmp_path / "team.uatransfer"
    export_team(
        export_source(config_path),
        out,
        PASSPHRASE,
        owner_person_id="usr_owner",
        sections=sections,
        key_derivation=new_key_derivation(**FAST_KEYS),
    )
    target = tmp_path / "new"
    with TransferReader.open(out, PASSPHRASE) as reader:
        plan = import_team(reader, target, dry_run=False, sections=import_sections or sections)
    return plan, target


def test_active_mcp_servers_arrive_and_the_new_instance_does_not_ask_again(tmp_path: Path) -> None:
    from assistant_gateway.config import GatewayInstanceConfig
    from mcp_control.registry import (
        MCPConnectionLifecycle,
        MCPConnectionStore,
        reconcile_instance_mcp_connections,
        resolve_mcp_connection_paths,
    )

    path, config = new_instance(tmp_path, "alpha", "team_a")
    config.mcp.static_reconciliation.upgrade_mode = "grandfather_current"
    config.agent.external_mcp_servers = [DOCS]
    config.save(path)
    [active] = reconcile_instance_mcp_connections(config)
    assert active.lifecycle is MCPConnectionLifecycle.ACTIVE
    # A server added after the first start waits for review: history, not carried.
    config.agent.external_mcp_servers = [DOCS, LATER]
    config.save(path)
    pending = [r for r in reconcile_instance_mcp_connections(config) if r.lifecycle is MCPConnectionLifecycle.PENDING]
    assert len(pending) == 1

    plan, target = _transfer(tmp_path, path, (SettingsSection(), McpSection()))

    mcp = {record.key: record.outcome for record in plan.records if record.section == "mcp"}
    assert mcp == {
        f"connection/{active.connection_id}": RecordOutcome.IMPORT,
        "static_baseline/static-baseline": RecordOutcome.IMPORT,
    }
    imported = GatewayInstanceConfig.load(target / "instance_config.yaml")
    store = MCPConnectionStore(resolve_mcp_connection_paths(target))
    assert [r.connection_id for r in store.list_records()] == [active.connection_id]
    # The new instance's own startup reconciliation recognises the carried record.
    after = reconcile_instance_mcp_connections(imported)
    by_name = {r.descriptor.connection_name: r.lifecycle for r in after}
    assert by_name["docs"] is MCPConnectionLifecycle.ACTIVE
    assert by_name["later"] is MCPConnectionLifecycle.PENDING, "the unreviewed server is reviewed again, as before"


def _install_procedure(root: Path, registry: Any, team_id: str) -> str:
    from custom_agents.installed_permissions import _atomic_write_json, resolve_installed_permission_paths
    from custom_messages.contracts.installed_permissions_v1 import InstalledPermissionDefinitionV1

    handler = registry.require(HANDLER_ID)
    descriptor = handler.descriptor
    definition = InstalledPermissionDefinitionV1.build(
        {
            "definition_id": f"installed_permission.{HANDLER_ID}.{handler.descriptor_digest[:16]}",
            "proposal_id": "installed_permission_proposal_test",
            "installed_at_utc": datetime(2026, 9, 1, tzinfo=timezone.utc),
            "installed_by_principal_id": "usr_admin",
            "team_id": team_id,
            "owner_canonical_user_id": "usr_owner",
            "handler_id": HANDLER_ID,
            "handler_descriptor_digest": handler.descriptor_digest,
            "input_schema_digest": handler.input_schema_digest,
            "display_name": descriptor.display_name,
            "description": descriptor.description,
            "impact_description": descriptor.impact_description,
            "risk": descriptor.risk,
            "manager_delegable": descriptor.manager_delegable,
        }
    )
    paths = resolve_installed_permission_paths(root)
    _atomic_write_json(paths.installed / f"{definition.definition_id}.json", definition.model_dump(mode="json"), immutable=True)
    return definition.definition_id


def _registry(tmp_path: Path, name: str, **kwargs: Any) -> Any:
    from custom_agents.installed_permission_registry import load_trusted_installed_permission_registry

    folder = tmp_path / name
    folder.mkdir()
    executors = {HANDLER_ID: _Handler()} if kwargs.get("include_handler", True) else {}
    return load_trusted_installed_permission_registry(registry_path=_write_registry(folder, **kwargs), executors=executors)


def test_procedures_arrive_only_while_their_handler_is_unchanged(tmp_path: Path) -> None:
    from custom_agents.installed_permissions import resolve_installed_permission_paths

    path, config = new_instance(tmp_path / "old", "alpha", "team_a")
    same = _registry(tmp_path, "same")
    definition_id = _install_procedure(config.instance_root_path, same, "team_a")

    for name, registry, expected, reason in (
        ("same", same, RecordOutcome.IMPORT, ""),
        ("changed", _registry(tmp_path, "changed", description="A different trusted handler now."), RecordOutcome.REFUSED, "changed since it was installed"),
        ("missing", _registry(tmp_path, "missing", include_handler=False), RecordOutcome.REFUSED, "no procedure handler"),
    ):
        work = tmp_path / name / "run"
        work.mkdir(parents=True)
        plan, target = _transfer(
            work, path, (SettingsSection(), ProceduresSection(same)), (SettingsSection(), ProceduresSection(registry))
        )
        [record] = [r for r in plan.records if r.section == "procedures"]
        assert (record.key, record.outcome) == (definition_id, expected), name
        assert reason in record.reason
        written = resolve_installed_permission_paths(target).installed / f"{definition_id}.json"
        assert written.is_file() is (expected is RecordOutcome.IMPORT)
