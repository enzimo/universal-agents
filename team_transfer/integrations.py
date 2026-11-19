"""The ``mcp`` and ``procedures`` sections: installed MCP servers and installed procedures.

These records are already versioned documents that carry their own digests
and approval (``MCPConnectionRecordV1``, ``MCPHostedServerDefinitionV1``,
``InstalledPermissionDefinitionV1``). Rebuilding them from separate export
models would mean recomputing those digests and approvals, so each travels as
its own document and this version's model validates it on import, the way the
runtime reads it. A record this version's model refuses is listed with the
model's reason; a format change between versions is handled by the section's
one-version-back conversion.

MCP: active and disabled connection records travel with their confirmed
operation levels; pending, rejected, drifted and removed records are history
and stay behind, as do receipts. The static baseline travels too: it records
that the one-time approval of the configured servers
(``mcp.static_reconciliation.upgrade_mode: grandfather_current``) already
happened. Without it the new instance would approve every configured server
again at its first start, including servers still waiting for review on the
old instance (found by the transfer tests, 2026-10-05). Hosted server
definitions travel; their sidecar tokens are regenerated.

Procedures: installed definitions travel. Each is usable only while its
handler's descriptor and input-schema digests match this build (the same rule
``InstalledPermissionService.invoke`` applies), so a definition whose handler
changed is refused and listed rather than carried as dead weight.

Under a new team id (owner's decision 103, ``team_transfer.team_ids``): a
connection's ``descriptor.team_id``, a hosted server definition's ``team_id``
and a procedure definition's ``team_id`` name the team. Each document's digest
covers that field, so it is computed again with the owning model's own
``build``; a hosted server's id is built from its digest, so it changes too,
and so does the ``source_locator`` of the connection that serves it (the
hosting service and the broker find one from the other by that text). A
connection id stays: the registry never derives it again for an existing
record, operation levels and the static baseline name it, and nothing at run
time compares it with the team. Approvals stay as they were given. A document
this version's model refuses is left as exported (the planner refuses it with
the model's reason, as today), and another team's document keeps its values.
"""

from __future__ import annotations

import json
import logging
from enum import StrEnum
from types import SimpleNamespace
from typing import Any, ClassVar, Optional

from pydantic import BaseModel, Field, JsonValue

from team_transfer.format import TransferModel
from team_transfer.sections import (
    TEAM_ID_RENAMES,
    ExportSource,
    ImportContext,
    PlannedRecord,
    RecordOutcome,
    SectionExport,
    SectionFiles,
    SectionRecords,
)
from team_transfer.team_ids import TeamIds

logger = logging.getLogger(__name__)


class McpKind(StrEnum):
    CONNECTION = "connection"
    HOSTED = "hosted"
    STATIC_BASELINE = "static_baseline"


class McpRecordV1(TransferModel):
    kind: McpKind
    record_id: str = Field(min_length=1, max_length=255)
    document: dict[str, JsonValue]
    # Connections only: the confirmed operation levels, when any were confirmed.
    levels: Optional[dict[str, JsonValue]] = None


class ProcedureRecordV1(TransferModel):
    definition_id: str = Field(min_length=1, max_length=255)
    document: dict[str, JsonValue]


def _documents(models: Any) -> list[dict[str, Any]]:
    return [model.model_dump(mode="json", exclude_none=True) for model in models]


class McpSection:
    name: ClassVar[str] = "mcp"
    schema_version: ClassVar[int] = 1
    encrypted: ClassVar[bool] = False
    after_start: ClassVar[bool] = False
    record_model: ClassVar[type[BaseModel]] = McpRecordV1

    def export(self, source: ExportSource, files: SectionFiles) -> SectionExport:
        from mcp_control.hosting import MCPHostedServerStore, resolve_mcp_hosting_paths
        from mcp_control.operation_levels import operation_levels_dir
        from mcp_control.registry import MCPConnectionLifecycle, MCPConnectionStore, resolve_mcp_connection_paths

        records: list[dict[str, Any]] = []
        paths = resolve_mcp_connection_paths(source.layout.root)
        if paths.records.is_dir():
            kept = {MCPConnectionLifecycle.ACTIVE, MCPConnectionLifecycle.DISABLED}
            for record in MCPConnectionStore(paths).list_records():
                if record.lifecycle not in kept:
                    continue
                levels_path = operation_levels_dir(paths) / f"{record.connection_id}.json"
                levels = json.loads(levels_path.read_text(encoding="utf-8")) if levels_path.is_file() else None
                records.append(
                    McpRecordV1(
                        kind=McpKind.CONNECTION,
                        record_id=record.connection_id,
                        document=record.model_dump(mode="json", exclude_none=True),
                        levels=levels,
                    ).model_dump(mode="json")
                )
            baseline = MCPConnectionStore(paths).baseline()
            if baseline is not None:
                records.append(
                    McpRecordV1(
                        kind=McpKind.STATIC_BASELINE,
                        record_id="static-baseline",
                        document=baseline.model_dump(mode="json"),
                    ).model_dump(mode="json")
                )
        hosting = resolve_mcp_hosting_paths(SimpleNamespace(instance_root_path=source.layout.root))
        if hosting.definitions.is_dir():
            for definition in MCPHostedServerStore(hosting).list_definitions():
                records.append(
                    McpRecordV1(
                        kind=McpKind.HOSTED,
                        record_id=definition.hosted_server_id,
                        document=definition.model_dump(mode="json", exclude_none=True),
                    ).model_dump(mode="json")
                )
        return SectionExport(records=records)

    def plan(self, records: SectionRecords, context: ImportContext) -> list[PlannedRecord]:
        planned = []
        for raw in records:
            record = McpRecordV1.model_validate(raw)
            key = f"{record.kind.value}/{record.record_id}"
            try:
                _validate_mcp(record)
            except ValueError as exc:  # pydantic's ValidationError is a ValueError
                planned.append(PlannedRecord(self.name, key, RecordOutcome.REFUSED, f"this version refuses it: {exc}"))
                continue
            planned.append(PlannedRecord(self.name, key, RecordOutcome.IMPORT))
        return planned

    def apply(self, records: SectionRecords, plan: list[PlannedRecord], context: ImportContext) -> None:
        from mcp_control.hosting import MCPHostedServerStore, resolve_mcp_hosting_paths
        from mcp_control.operation_levels import operation_levels_dir
        from mcp_control.registry import MCPConnectionStore, resolve_mcp_connection_paths

        accepted = {record.key for record in plan if record.outcome is not RecordOutcome.REFUSED}
        connections = MCPConnectionStore(resolve_mcp_connection_paths(context.layout.root))
        hosted = MCPHostedServerStore(
            resolve_mcp_hosting_paths(SimpleNamespace(instance_root_path=context.layout.root))
        )
        for raw in records:
            record = McpRecordV1.model_validate(raw)
            if f"{record.kind.value}/{record.record_id}" not in accepted:
                continue
            if record.kind is McpKind.STATIC_BASELINE:
                connections.save_baseline(_validate_baseline(record))
                continue
            connection, levels, definition = _validate_mcp(record)
            if connection is not None:
                connections.save(connection)
                if levels is not None:
                    _write_levels(operation_levels_dir(connections.paths), levels)
            if definition is not None:
                hosted.save(definition)
        logger.info(
            "Installed MCP servers imported. event=team_transfer_mcp_imported records=%s refused=%s",
            len(accepted),
            len(plan) - len(accepted),
        )


def _validate_mcp(record: McpRecordV1) -> tuple[Any, Any, Any]:
    """This version's models for the record (raises ValueError when they refuse it)."""

    from mcp_control.hosting import MCPHostedServerDefinitionV1
    from mcp_control.operation_levels import MCPOperationLevelsV1
    from mcp_control.registry import MCPConnectionRecordV1

    if record.kind is McpKind.STATIC_BASELINE:
        _validate_baseline(record)
        return None, None, None
    if record.kind is McpKind.HOSTED:
        return None, None, MCPHostedServerDefinitionV1.model_validate(record.document)
    connection = MCPConnectionRecordV1.model_validate(record.document)
    levels = MCPOperationLevelsV1.model_validate(record.levels) if record.levels is not None else None
    if levels is not None and levels.connection_id != connection.connection_id:
        raise ValueError("its operation levels belong to another connection")
    return connection, levels, None


def accepted_mcp_tool_definitions(context: ImportContext) -> dict[str, Any]:
    """Operations of the accepted active connections, exactly as runtime builds them."""

    from access_catalog.catalog import mcp_tool_definition
    from mcp_control.registry import MCPConnectionLifecycle

    accepted = context.accepted("mcp")
    definitions = {}
    for raw in [*context.baseline.get("mcp", ()), *context.records("mcp")]:
        record = McpRecordV1.model_validate(raw)
        if record.kind is not McpKind.CONNECTION or f"connection/{record.record_id}" not in accepted:
            continue
        connection, levels, _ = _validate_mcp(record)
        if connection.lifecycle is MCPConnectionLifecycle.ACTIVE:
            tool = mcp_tool_definition(connection, levels.operations if levels is not None else None)
            definitions[tool.tool_id] = tool
    return definitions


def _validate_baseline(record: McpRecordV1) -> Any:
    from mcp_control.registry import MCPStaticBaselineV1

    return MCPStaticBaselineV1.model_validate(record.document)


def _write_levels(directory: Any, levels: Any) -> None:
    directory.mkdir(parents=True, mode=0o700, exist_ok=True)
    path = directory / f"{levels.connection_id}.json"
    path.write_text(levels.model_dump_json(indent=2), encoding="utf-8")


class ProceduresSection:
    name: ClassVar[str] = "procedures"
    schema_version: ClassVar[int] = 1
    encrypted: ClassVar[bool] = False
    after_start: ClassVar[bool] = False
    record_model: ClassVar[type[BaseModel]] = ProcedureRecordV1

    def __init__(self, registry: Any = None) -> None:
        # The trusted handler registry; tests pass their own, as with InstalledPermissionService.
        self._registry = registry

    def export(self, source: ExportSource, files: SectionFiles) -> SectionExport:
        from custom_agents.installed_permissions import resolve_installed_permission_paths
        from custom_messages.contracts.installed_permissions_v1 import InstalledPermissionDefinitionV1

        installed = resolve_installed_permission_paths(source.layout.root).installed
        records = []
        for path in sorted(installed.glob("*.json")) if installed.is_dir() else []:
            definition = InstalledPermissionDefinitionV1.model_validate_json(path.read_text(encoding="utf-8"))
            records.append(
                ProcedureRecordV1(
                    definition_id=definition.definition_id,
                    document=definition.model_dump(mode="json"),
                ).model_dump(mode="json")
            )
        return SectionExport(records=records)

    def plan(self, records: SectionRecords, context: ImportContext) -> list[PlannedRecord]:
        from custom_agents.installed_permission_registry import load_trusted_installed_permission_registry
        from custom_messages.contracts.installed_permissions_v1 import InstalledPermissionDefinitionV1

        registry = self._registry or load_trusted_installed_permission_registry()
        planned = []
        for raw in records:
            record = ProcedureRecordV1.model_validate(raw)
            try:
                definition = InstalledPermissionDefinitionV1.model_validate(record.document)
                handler = registry.get(definition.handler_id)
                if handler is None:
                    raise ValueError(f"this version has no procedure handler {definition.handler_id}")
                if (
                    handler.descriptor_digest != definition.handler_descriptor_digest
                    or handler.input_schema_digest != definition.input_schema_digest
                ):
                    raise ValueError(f"the handler {definition.handler_id} changed since it was installed; install it again")
            except ValueError as exc:
                planned.append(PlannedRecord(self.name, record.definition_id, RecordOutcome.REFUSED, str(exc)))
                continue
            planned.append(PlannedRecord(self.name, record.definition_id, RecordOutcome.IMPORT))
        return planned

    def apply(self, records: SectionRecords, plan: list[PlannedRecord], context: ImportContext) -> None:
        from custom_agents.installed_permissions import _atomic_write_json, resolve_installed_permission_paths
        from custom_messages.contracts.installed_permissions_v1 import InstalledPermissionDefinitionV1

        accepted = {record.key for record in plan if record.outcome is not RecordOutcome.REFUSED}
        paths = resolve_installed_permission_paths(context.layout.root)
        paths.ensure()
        for raw in records:
            record = ProcedureRecordV1.model_validate(raw)
            if record.definition_id not in accepted:
                continue
            definition = InstalledPermissionDefinitionV1.model_validate(record.document)
            # Installed definitions are written read-only, as InstalledPermissionService.install does.
            _atomic_write_json(
                paths.installed / f"{definition.definition_id}.json",
                definition.model_dump(mode="json"),
                immutable=True,
            )
        logger.info(
            "Installed procedures imported. event=team_transfer_procedures_imported records=%s refused=%s",
            len(accepted),
            len(plan) - len(accepted),
        )


# -- under a new team id (module docstring) -------------------------------------------------


def _readable(raw: dict[str, Any], model: type[BaseModel], section: str) -> Any:
    """``raw``'s document as this version's ``model``; None, logged, when the model refuses it."""

    try:
        return model.model_validate(raw.get("document"))
    except ValueError as exc:
        logger.warning(
            "A record the import cannot read keeps its exported team id. "
            "event=team_transfer_integration_rename_skipped section=%s record_id=%s error=%s",
            section,
            raw.get("record_id", raw.get("definition_id")),
            exc,
        )
        return None


def _renamed_hosted(raw: dict[str, Any], team_ids: TeamIds, hosted_ids: dict[str, str]) -> dict[str, Any]:
    """A hosted server definition under the new team id; its new id is added to ``hosted_ids``."""

    from mcp_control.hosting import MCPHostedServerDefinitionV1

    definition = _readable(raw, MCPHostedServerDefinitionV1, McpSection.name)
    if definition is None or team_ids.rename(definition.team_id) == definition.team_id:
        return raw
    values = definition.model_dump(mode="python", exclude={"schema_version", "hosted_server_id", "definition_digest"})
    renamed = MCPHostedServerDefinitionV1.build(**{**values, "team_id": team_ids.rename(definition.team_id)})
    hosted_ids[definition.hosted_server_id] = renamed.hosted_server_id
    return {**raw, "record_id": renamed.hosted_server_id, "document": renamed.model_dump(mode="json", exclude_none=True)}


def _renamed_connection(raw: dict[str, Any], team_ids: TeamIds, hosted_ids: dict[str, str]) -> dict[str, Any]:
    """A connection of the team under the new team id, naming its hosted server by the server's new id."""

    from mcp_control.hosting import hosted_source_locator
    from mcp_control.registry import MCPConnectionRecordV1

    connection = _readable(raw, MCPConnectionRecordV1, McpSection.name)
    if connection is None or team_ids.rename(connection.descriptor.team_id) == connection.descriptor.team_id:
        return raw
    locators = {hosted_source_locator(old): hosted_source_locator(new) for old, new in hosted_ids.items()}
    locator = connection.descriptor.source_locator
    descriptor = {
        **connection.descriptor.model_dump(mode="python"),
        "team_id": team_ids.rename(connection.descriptor.team_id),
        "source_locator": locators.get(locator, locator),
    }
    values = connection.model_dump(mode="python", exclude={"record_digest", "operation_catalog_sha256"})
    renamed = MCPConnectionRecordV1.build(**{**values, "descriptor": descriptor})
    return {**raw, "document": renamed.model_dump(mode="json", exclude_none=True)}


def _renamed_mcp(records: SectionRecords, team_ids: TeamIds) -> SectionRecords:
    """The ``mcp`` records under the new team id; hosted servers first, so connections find their new ids."""

    hosted_ids: dict[str, str] = {}
    renamed = [
        _renamed_hosted(raw, team_ids, hosted_ids) if raw.get("kind") == McpKind.HOSTED.value else raw
        for raw in records
    ]
    return [
        _renamed_connection(raw, team_ids, hosted_ids) if raw.get("kind") == McpKind.CONNECTION.value else raw
        for raw in renamed
    ]


def _renamed_procedures(records: SectionRecords, team_ids: TeamIds) -> SectionRecords:
    """Installed procedure definitions of the team under the new team id (the definition id stays)."""

    from custom_messages.contracts.installed_permissions_v1 import InstalledPermissionDefinitionV1

    renamed = []
    for raw in records:
        definition = _readable(raw, InstalledPermissionDefinitionV1, ProceduresSection.name)
        if definition is not None and team_ids.rename(definition.team_id) != definition.team_id:
            rebuilt = InstalledPermissionDefinitionV1.build(
                {**definition.model_dump(mode="python"), "team_id": team_ids.rename(definition.team_id)}
            )
            raw = {**raw, "document": rebuilt.model_dump(mode="json")}
        renamed.append(raw)
    return renamed


TEAM_ID_RENAMES[McpSection.name] = _renamed_mcp
TEAM_ID_RENAMES[ProceduresSection.name] = _renamed_procedures


__all__ = ["McpKind", "McpRecordV1", "McpSection", "ProcedureRecordV1", "ProceduresSection", "accepted_mcp_tool_definitions"]
