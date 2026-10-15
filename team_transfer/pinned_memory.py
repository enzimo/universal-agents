"""The ``pinned_memory`` section: the team's and each person's pinned facts.

Pinned memories are the short facts agents see on every turn: the team's
(``coremem``) and each person's (``usermem``). The section reuses the memory
export and import (``custom_agents.pinned_memory_transfer``) so both paths
decide the same way: each fact travels as that module's ``ExportedFactV1``
and the import uses its one planner and writer.

Why these choices:

- People keep their ids in a team transfer, so each personal fact goes to the
  same person id, and only when the ``people`` section imports that person.
  Facts of people removed from the team stay behind (their memories are held
  only until deletion); facts of anyone else who is not imported are refused
  and listed.
- The new instance's memory limits (bank capacity, tag and metadata limits)
  decide, so a fact the new version would refuse is listed, not forced in.
  The plan reads them from the configuration the ``settings`` section
  planned, the same way ``open_memory_instance`` reads them from the saved
  file, so ``--dry-run`` and the import decide alike.
- Imported facts are recorded as written by the memory import, with their
  original author and creation time in ``imported_from`` metadata, as the
  memory import does.
- The learned memory graph is not carried; the new instance learns it again.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any, ClassVar

from pydantic import BaseModel, Field, JsonValue

from team_transfer.format import TransferModel
from team_transfer.sections import (
    ExportSource,
    ImportContext,
    PlannedRecord,
    RecordOutcome,
    SectionExport,
    SectionFiles,
    SectionRecords,
)

if TYPE_CHECKING:
    from assistant_gateway.config import GatewayInstanceConfig
    from custom_agents.pinned_memory_transfer import (
        ExportedFactV1,
        PinnedMemoryExportV1,
        PlannedFact,
    )

logger = logging.getLogger(__name__)


class PinnedMemoryFactRecordV1(TransferModel):
    """One pinned fact; ``document`` is ``custom_agents.pinned_memory_transfer.ExportedFactV1``."""

    document: dict[str, JsonValue] = Field(min_length=1)


def _fact_key(fact: "ExportedFactV1") -> str:
    owner = fact.person_id or "team"
    return f"{fact.bank}/{owner}/{fact.memory_id}"


def memory_config(config: "GatewayInstanceConfig") -> dict[str, Any]:
    """The memory limits of the team lead, which owns the pinned memory store (as ``open_memory_instance``)."""

    from assistant_gateway.runtime_config import (
        build_runtime_config_from_instance_config,
    )
    from config_loader import resolve_agent_type_settings
    from custom_agents.memory_plugins.config import normalize_memory_config

    runtime = resolve_agent_type_settings(
        build_runtime_config_from_instance_config(config),
        config.gateway.named_agent_type,
    )
    return normalize_memory_config(runtime.get("memory_config"))


def _removed_people(source: ExportSource) -> list[str]:
    from access_control.people import MEMBERSHIP_STATUS_REMOVED
    from storage.gateway_state_repository import create_gateway_state_repository

    repository = create_gateway_state_repository(source.config.state_storage_settings)
    return sorted(
        member.canonical_user_id
        for member in repository.list_team_people(source.team_id)
        if member.membership_status == MEMBERSHIP_STATUS_REMOVED
    )


class PinnedMemorySection:
    name: ClassVar[str] = "pinned_memory"
    schema_version: ClassVar[int] = 1
    encrypted: ClassVar[bool] = False
    after_start: ClassVar[bool] = False
    record_model: ClassVar[type[BaseModel]] = PinnedMemoryFactRecordV1

    def export(self, source: ExportSource, files: SectionFiles) -> SectionExport:
        from custom_agents.pinned_memory_transfer import ExportSelection, build_export
        from storage.pinned_memory_repository import create_pinned_memory_repository

        rows = create_pinned_memory_repository(
            source.config.manager_storage_settings
        ).list_unexpired_facts()
        result = build_export(
            rows,
            instance_name=source.instance_name,
            team_id=source.team_id,
            selection=ExportSelection(),
            removed_people=_removed_people(source),
        )
        if result.removed_people:
            logger.info(
                "Pinned memories of people removed from the team stay behind. "
                "event=team_transfer_pinned_memory_removed_people people=%s",
                ",".join(result.removed_people),
            )
        records = [
            PinnedMemoryFactRecordV1(document=fact.model_dump(mode="json")).model_dump(
                mode="json"
            )
            for fact in result.document.facts
        ]
        return SectionExport(records=records)

    # -- import ----------------------------------------------------------------

    def plan(
        self, records: SectionRecords, context: ImportContext
    ) -> list[PlannedRecord]:
        return [self._outcome(planned) for planned in _plan(records, context)]

    def _outcome(self, planned: "PlannedFact") -> PlannedRecord:
        from custom_agents.pinned_memory_transfer import ImportOutcome

        key = _fact_key(planned.fact)
        if planned.outcome is ImportOutcome.IMPORTED:
            return PlannedRecord(self.name, key, RecordOutcome.IMPORT)
        if planned.outcome is ImportOutcome.NO_PERSON:
            reason = f"it belongs to {planned.fact.person_id}, who is not imported"
        elif planned.outcome is ImportOutcome.ALREADY_THERE:
            reason = "the same fact is carried twice; it is imported once"
        else:
            reason = (
                f"{planned.outcome.value}: {planned.reason}"
                if planned.reason
                else planned.outcome.value
            )
        return PlannedRecord(self.name, key, RecordOutcome.REFUSED, reason)

    def apply(
        self, records: SectionRecords, plan: list[PlannedRecord], context: ImportContext
    ) -> None:
        from custom_agents.pinned_memory_transfer import ImportOutcome, apply_import
        from storage.pinned_memory_repository import create_pinned_memory_repository
        from team_transfer.settings import planned_config

        config = planned_config(context)
        accepted = {
            record.key for record in plan if record.outcome is not RecordOutcome.REFUSED
        }
        # The same planner again, so the writer gets its checked tags and
        # metadata; only facts the reported plan accepts are written.
        planned = [
            fact for fact in _plan(records, context) if _fact_key(fact.fact) in accepted
        ]
        applied = apply_import(
            create_pinned_memory_repository(config.manager_storage_settings),
            planned,
            memory_config=memory_config(config),
        )
        no_room = [
            _fact_key(fact.fact)
            for fact in applied
            if fact.outcome is ImportOutcome.NO_ROOM
        ]
        if no_room:
            logger.warning(
                "Some pinned memories found their bank full while writing. "
                "event=team_transfer_pinned_memory_no_room facts=%s",
                ",".join(no_room),
            )
            from team_transfer.errors import TransferError

            raise TransferError(
                "Pinned memory capacity changed while importing; the import is unfinished."
            )
        logger.info(
            "Team pinned memory imported. event=team_transfer_pinned_memory_imported facts=%s refused=%s",
            len(applied) - len(no_room),
            len(plan) - len(accepted),
        )


def _document(
    records: SectionRecords, context: ImportContext
) -> "PinnedMemoryExportV1":
    from custom_agents.pinned_memory_transfer import (
        ExportedFactV1,
        PinnedMemoryExportV1,
    )

    manifest = context.reader.manifest
    return PinnedMemoryExportV1(
        exported_at_utc=manifest.exported_at_utc,
        source_instance=manifest.instance_name,
        source_team_id=manifest.team_id,
        facts=[
            ExportedFactV1.model_validate(
                PinnedMemoryFactRecordV1.model_validate(raw).document
            )
            for raw in records
        ],
    )


def _plan(records: SectionRecords, context: ImportContext) -> list["PlannedFact"]:
    """The memory import's planner against the fresh, empty store, people kept under their own ids."""

    from custom_agents.pinned_memory_transfer import PersonTargets, plan_import
    from team_transfer.settings import planned_config

    people = context.accepted("people")
    existing_rows = []
    if context.existing:
        from storage.pinned_memory_repository import create_pinned_memory_repository
        from storage.read_only import read_only_repositories
        from team_transfer.settings import storage_environment

        with storage_environment(context), read_only_repositories():
            existing_rows = create_pinned_memory_repository(
                planned_config(context).manager_storage_settings
            ).list_unexpired_facts()
    return plan_import(
        _document(records, context),
        existing_rows=existing_rows,
        team_id=context.reader.manifest.team_id,
        targets=PersonTargets(
            explicit={person: person for person in people}, known=None
        ),
        memory_config=memory_config(planned_config(context)),
    )


__all__ = ["PinnedMemoryFactRecordV1", "PinnedMemorySection", "memory_config"]
