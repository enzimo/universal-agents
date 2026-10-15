"""Export, inspect and import a team (the work behind ``assistant_cli team``).

Export reads the running instance with this version's own repositories and
writes every registered section into a new transfer file. Import reads a
verified file, plans every record against a fresh instance root and, unless
it is a dry run, applies the plan section by section.

A fresh instance is one without ``instance_config.yaml``: the import creates
the instance. While it writes, a marker file
(``InstanceLayout.team_import_marker_path``) records that the import has not
finished; ``require_finished_import`` refuses to start the runtime while it is
there, because a half-imported team must never run. A failed first import is
retried into a fresh root; a failed later batch requires restoring the pre-batch
backup. Import never guesses how to repair partially written state.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Any, Callable, Optional, Sequence

from team_transfer.container import TransferFileError, TransferReader, TransferWriter
from team_transfer.errors import TransferError
from team_transfer.crypto import (
    TransferKeys,
    check_passphrase,
    derive_keys,
    new_key_derivation,
)
from team_transfer.format import (
    ExporterV1,
    KeyDerivationV1,
    SectionDocumentV1,
    SectionEntryV1,
    TransferManifestV1,
)
from team_transfer.private_files import write_private_json
from team_transfer.runtime_guard import import_writer, require_finished_import
from team_transfer.sections import (
    DestinationChoices,
    ExportSource,
    encrypted_member,
    ImportContext,
    PlannedRecord,
    RecordOutcome,
    SectionFiles,
    TransferSection,
    read_section_records,
)

if TYPE_CHECKING:
    from assistant_gateway.config import GatewayInstanceConfig
    from runtime_settings.instance_layout import InstanceLayout

logger = logging.getLogger(__name__)

REPO_ROOT = Path(__file__).resolve().parents[1]


def application_version() -> str:
    return (REPO_ROOT / "VERSION").read_text(encoding="utf-8").strip()


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def default_sections() -> tuple[TransferSection, ...]:
    from team_transfer.registry import TRANSFER_SECTIONS

    return TRANSFER_SECTIONS


def _section_versions(sections: Sequence[TransferSection]) -> dict[str, int]:
    names = [section.name for section in sections]
    if len(names) != len(set(names)):
        raise TransferError("Two transfer sections share a name.")
    return {section.name: section.schema_version for section in sections}


# -- export ----------------------------------------------------------------------


@dataclass(frozen=True)
class ExportReport:
    path: Path
    manifest: TransferManifestV1


def export_source(config_path: str | Path) -> ExportSource:
    from assistant_gateway.config import GatewayInstanceConfig

    config: GatewayInstanceConfig = GatewayInstanceConfig.load_for_process(config_path)
    return ExportSource(
        config=config,
        layout=config.layout,
        team_id=config.gateway.team_id,
        instance_name=config.instance_name,
    )


def require_active_owner(
    config: "GatewayInstanceConfig", team_id: str, person_id: str
) -> None:
    """``--owner`` must be an active owner in the gateway's copy of the team's people."""

    from access_control.people import PERSON_ROLE_OWNER
    from custom_messages.contracts.access_v2 import PersonStanding
    from storage.access_repository import AccessRepository

    person = AccessRepository(config.manager_storage_settings).team_person(
        team_id, person_id
    )
    if (
        person is None
        or person.standing is not PersonStanding.ACTIVE
        or PERSON_ROLE_OWNER not in person.roles
    ):
        raise TransferError(
            f"{person_id} is not an active owner of team {team_id}. Name an owner with --owner "
            "(the gateway must have started once so its people records exist)."
        )


def export_team(
    source: ExportSource,
    out_path: str | Path,
    passphrase: str,
    *,
    owner_person_id: str,
    sections: Optional[Sequence[TransferSection]] = None,
    key_derivation: Optional[KeyDerivationV1] = None,
) -> ExportReport:
    check_passphrase(passphrase)
    chosen = tuple(default_sections() if sections is None else sections)
    _section_versions(chosen)
    derivation = key_derivation or new_key_derivation()
    keys = derive_keys(passphrase, derivation)
    writer = TransferWriter.create(out_path)
    try:
        entries = [_export_section(section, source, writer, keys) for section in chosen]
        manifest = TransferManifestV1(
            application_version=application_version(),
            exported_at_utc=utc_now_iso(),
            team_id=source.team_id,
            instance_name=source.instance_name,
            exported_by=ExporterV1(owner_person_id=owner_person_id),
            key_derivation=derivation,
            sections=tuple(entries),
        )
        signed = writer.publish(manifest, keys)
    except BaseException:
        writer.discard()
        raise
    return ExportReport(writer.destination, signed)


def _export_section(
    section: TransferSection,
    source: ExportSource,
    writer: TransferWriter,
    keys: TransferKeys,
) -> SectionEntryV1:
    exported = section.export(source, SectionFiles(section.name, writer, keys))
    if section.encrypted:
        if exported.ciphertext is None:
            raise TransferError(
                f"The section {section.name} returned no encrypted content."
            )
        writer.add_bytes(encrypted_member(section.name), exported.ciphertext)
    else:
        document = SectionDocumentV1(
            section=section.name,
            schema_version=section.schema_version,
            records=exported.records,
        )
        writer.add_section(document)
    return SectionEntryV1(
        name=section.name,
        schema_version=section.schema_version,
        record_count=exported.record_count
        if exported.record_count is not None
        else len(exported.records),
        encrypted=section.encrypted,
    )


# -- import ----------------------------------------------------------------------


@dataclass
class ImportPlan:
    manifest: TransferManifestV1
    target_root: Path
    records: list[PlannedRecord] = field(default_factory=list)
    # Sections in the file this version does not know, and known sections the file lacks.
    unknown_sections: list[str] = field(default_factory=list)
    missing_sections: list[str] = field(default_factory=list)
    # Encrypted sections (credentials): imported after the first start by
    # ``team import-credentials``, because only the running broker stores secrets.
    later_sections: list[str] = field(default_factory=list)
    skipped_records: list[PlannedRecord] = field(default_factory=list)

    def by_outcome(self, outcome: RecordOutcome) -> list[PlannedRecord]:
        return [record for record in self.records if record.outcome is outcome]


def require_fresh_target(layout: "InstanceLayout") -> None:
    if layout.team_import_marker_path.exists():
        require_finished_import(layout)
    if layout.instance_config_path.exists():
        raise TransferError(
            f"{layout.root} already holds an instance ({layout.instance_config_path.name} exists). A team is imported "
            "only into a fresh instance root."
        )
    for folder in (layout.control_dir, layout.dbs_dir, layout.state_dir):
        if folder.exists() and any(folder.iterdir()):
            raise TransferError(
                f"{folder} already holds instance data. Use a fresh root for the first import."
            )


def plan_import(
    reader: TransferReader,
    target_root: str | Path,
    *,
    sections: Optional[Sequence[TransferSection]] = None,
    choices: dict[str, set[str]] | None = None,
    overrides: dict[str, dict[str, dict[str, Any]]] | None = None,
    allow_existing: bool = False,
    destination: DestinationChoices | None = None,
) -> tuple[ImportPlan, ImportContext]:
    chosen = tuple(default_sections() if sections is None else sections)
    context, previous = _import_context(reader, target_root, chosen, allow_existing)
    if destination is not None:
        context.destination = destination
    plan = ImportPlan(manifest=reader.manifest, target_root=context.layout.root)
    in_file = {entry.name for entry in reader.manifest.sections}
    plan.unknown_sections = sorted(in_file - set(context.versions))
    plan.missing_sections = [
        section.name for section in chosen if section.name not in in_file
    ]
    for section in chosen:
        if section.name not in in_file:
            continue
        if section.after_start:
            _check_later_section(section, reader)
            plan.later_sections.append(section.name)
            continue
        records = _selected_records(
            section, context, plan, choices, overrides, previous
        )
        context._records[section.name] = records
        records = _skip_present(section, records, context, plan)
        context._records[section.name] = records
        planned = _plan_records(section, records, context)
        context.planned.extend(planned)
        plan.records.extend(planned)
        context.scratch.setdefault("planned_sections", set()).add(section.name)
        if context.existing and section.name == "settings":
            from team_transfer.batches import snapshot_target

            snapshot_target(context, chosen)
    _check_forward_dependencies(chosen, context, plan)
    return plan, context


def _plan_records(section, records, context):
    try:
        return section.plan(records, context)
    except ValueError as exc:
        if not section.encrypted:
            raise
        raise TransferError(
            f"The encrypted section {section.name} is invalid ({type(exc).__name__}); secret values are omitted from this error."
        ) from exc


def _import_context(reader, target_root, sections, allow_existing):
    from runtime_settings.instance_layout import InstanceLayout

    layout = InstanceLayout.at(target_root)
    context = ImportContext(
        reader=reader,
        target_root=layout.root,
        layout=layout,
        versions=_section_versions(sections),
    )
    if not allow_existing or not layout.instance_config_path.is_file():
        require_fresh_target(layout)
        return context, {}
    from assistant_gateway.config import GatewayInstanceConfig
    from team_transfer.batches import previous_records

    require_finished_import(layout)
    config = GatewayInstanceConfig.load_saved(layout.instance_config_path)
    if config.gateway.team_id != reader.manifest.team_id:
        raise TransferError(
            "The target instance and transfer file belong to different teams."
        )
    context.existing = True
    context.scratch["settings.config"] = config
    context.scratch["settings.original_config"] = config
    return context, previous_records(context)


def _check_later_section(section, reader):
    """An encrypted section imported after the first start must be readable now, before anything is written."""

    from team_transfer.sections import readable_version_problem

    problem = readable_version_problem(
        section.name,
        reader.manifest.section(section.name).schema_version,
        section.schema_version,
        exported_by=reader.manifest.application_version,
    )
    if problem:
        raise TransferError(problem)


def _selected_records(section, context, plan, choices, overrides, previous):
    records = read_section_records(context.reader, section.name, section.schema_version)
    if section.name == "schedules":
        from team_transfer.selection import keyed_records

        context.scratch["schedules.source_keys"] = set(
            keyed_records(section.name, records)
        )
    if choices is None and not context.existing:
        return records
    from team_transfer.selection import keyed_records

    keyed = keyed_records(section.name, records)
    selected = choices.get(section.name, set()) if choices is not None else set(keyed)
    if selected - set(keyed):
        raise TransferError(
            f"Selected {section.name} records are absent from this backup."
        )
    pending = []
    for key, raw in keyed.items():
        already_imported = key in previous.get(section.name, set())
        if key not in selected or already_imported:
            plan.skipped_records.append(
                PlannedRecord(
                    section.name,
                    key,
                    RecordOutcome.IMPORT,
                    "already imported" if already_imported else "not selected",
                )
            )
        else:
            pending.append((overrides or {}).get(section.name, {}).get(key, raw))
    return pending


def _skip_present(section, records, context, plan):
    if context.existing and section.name == "control":
        return _skip_edited_control(records, context, plan)
    if not context.existing or section.name in {
        "settings",
        "control",
        "operator_secrets",
        "pinned_memory",
    }:
        return records
    from team_transfer.selection import destination_identity, record_key

    present = {
        destination_identity(section.name, raw)
        for raw in context.baseline.get(section.name, ())
    }
    pending = []
    for raw in records:
        key = record_key(section.name, raw)
        if destination_identity(section.name, raw) in present:
            plan.skipped_records.append(
                PlannedRecord(
                    section.name,
                    key,
                    RecordOutcome.IMPORT,
                    "already present; kept the destination record",
                )
            )
        else:
            pending.append(raw)
    return pending


def _skip_edited_control(records, context, plan):
    from team_transfer.batches import control_was_edited

    pending = []
    for raw in records:
        key = raw["unit"]
        if control_was_edited(context, key):
            plan.skipped_records.append(
                PlannedRecord(
                    "control",
                    key,
                    RecordOutcome.IMPORT,
                    "the destination control file was edited; kept its current contents",
                )
            )
        else:
            pending.append(raw)
    return pending


def _check_forward_dependencies(sections, context, plan):
    # Schedule-owned grants point forward in the import order. Check them
    # once the schedule decisions are known, including dependent child grants.
    if "schedules" not in context.scratch.get("planned_sections", set()):
        return
    access = next((section for section in sections if section.name == "access"), None)
    if access is None:
        return
    replanned = access.plan(context.records("access"), context)
    context.planned = [r for r in context.planned if r.section != "access"] + replanned
    plan.records = [r for r in plan.records if r.section != "access"] + replanned


def import_team(
    reader: TransferReader,
    target_root: str | Path,
    *,
    dry_run: bool,
    sections: Optional[Sequence[TransferSection]] = None,
    choices: dict[str, set[str]] | None = None,
    overrides: dict[str, dict[str, dict[str, Any]]] | None = None,
    allow_existing: bool = False,
    require_valid: bool = False,
    destination: DestinationChoices | None = None,
    before_finish: Callable[[ImportContext], None] | None = None,
) -> ImportPlan:
    chosen = tuple(default_sections() if sections is None else sections)
    arguments = dict(
        sections=chosen,
        choices=choices,
        overrides=overrides,
        allow_existing=allow_existing,
        destination=destination,
    )
    plan, context = plan_import(reader, target_root, **arguments)
    if dry_run:
        return plan
    if require_valid and plan.by_outcome(RecordOutcome.REFUSED):
        raise TransferError(
            "Selected records were refused. Correct them or unselect them before importing."
        )
    from team_transfer.batches import import_lock, stopped_instance

    with import_lock(context.layout), stopped_instance(context.layout):
        # Check again while holding the lock, before writing the marker.
        plan, context = plan_import(reader, target_root, **arguments)
        if require_valid and plan.by_outcome(RecordOutcome.REFUSED):
            raise TransferError(
                "Selected records changed or were refused. Review the batch again."
            )
        _apply_import(chosen, plan, context, before_finish=before_finish)
    return plan


def _apply_import(
    chosen,
    plan: ImportPlan,
    context: ImportContext,
    *,
    before_finish: Callable[[ImportContext], None] | None = None,
) -> None:
    layout = context.layout
    from team_transfer.settings import require_empty_postgres

    require_empty_postgres(context)
    write_private_json(layout.team_import_marker_path, _report(plan, finished=False))
    from team_transfer.settings import storage_environment

    with import_writer(layout.root), storage_environment(context):
        _apply_sections(chosen, plan, context)
        # Onboarding must durably leave selected credentials for the broker
        # before this instance can start. A failure retains the unfinished
        # marker, just like a failed section write.
        if before_finish is not None:
            before_finish(context)
    report_path = (
        layout.team_import_dir
        / f"report-{datetime.now(timezone.utc):%Y%m%dT%H%M%S.%fZ}.json"
    )
    write_private_json(report_path, _report(plan, finished=True))
    from team_transfer.batches import receipt

    imported = {
        section.name: [
            r.key
            for r in plan.records
            if r.section == section.name and r.outcome is not RecordOutcome.REFUSED
        ]
        for section in chosen
    }
    write_private_json(layout.team_import_receipt_path, receipt(context, imported))
    layout.team_import_marker_path.unlink()


def _apply_sections(chosen, plan: ImportPlan, context: ImportContext) -> None:
    """Apply planned noncredential sections while the writer holds its permit."""

    in_file = {entry.name for entry in context.reader.manifest.sections}
    for section in chosen:
        if section.name not in in_file or section.after_start:
            continue
        planned = [record for record in plan.records if record.section == section.name]
        section.apply(context.records(section.name), planned, context)
        logger.info(
            "Team transfer section imported. event=team_transfer_section_imported section=%s records=%s refused=%s",
            section.name,
            len(planned),
            sum(1 for record in planned if record.outcome is RecordOutcome.REFUSED),
        )


def _report(plan: ImportPlan, *, finished: bool) -> dict[str, Any]:
    manifest = plan.manifest
    return {
        "finished": finished,
        "written_at_utc": utc_now_iso(),
        "source": {
            "application_version": manifest.application_version,
            "exported_at_utc": manifest.exported_at_utc,
            "team_id": manifest.team_id,
            "instance_name": manifest.instance_name,
            "exported_by_owner": manifest.exported_by.owner_person_id,
        },
        "imported_by_version": application_version(),
        "unknown_sections": plan.unknown_sections,
        "missing_sections": plan.missing_sections,
        "later_sections": plan.later_sections,
        "skipped_records": [
            {"section": r.section, "key": r.key, "reason": r.reason}
            for r in plan.skipped_records
        ],
        "records": [
            {
                "section": r.section,
                "key": r.key,
                "outcome": r.outcome.value,
                "reason": r.reason,
            }
            for r in plan.records
        ],
    }


__all__ = [
    "DestinationChoices",
    "ExportReport",
    "ImportPlan",
    "TransferError",
    "TransferFileError",
    "application_version",
    "export_source",
    "export_team",
    "import_team",
    "plan_import",
    "require_active_owner",
    "require_finished_import",
    "require_fresh_target",
]
