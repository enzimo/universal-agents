"""The ``knowledge`` section: the team's and each person's knowledge pages.

Carried: pages, every revision, notes, links, the names of merged pages
(redirects) and contributions
(``storage.knowledge_repository.KNOWLEDGE_TRANSFER_TABLES``), with their ids.

Version 2 added redirects (a merged page's name leading to the page it was
merged into).

Why these choices:

- Personal pages, their revisions, notes and links, and pending
  contributions belong to one person (``owner_person_id``). They travel only
  when the ``people`` section imports that person; people removed from the
  team leave theirs behind, as their personal memory does.
- Team page revisions keep the id of the person whose
  work produced them as provenance, even when that person is not imported.
- A row that refers to a page (revision, note, link or contribution) is imported
  only with its page, so the new store never holds a revision of a missing page. A redirect
  needs both its merged page and the page it leads to. Contributions that
  propose a new page need no existing page.
- The edit log is not carried: write limits start fresh. The search index is
  not carried either: each database rebuilds it from the page rows.
- Each row is checked with this version's own column list and record
  converters, so a value this version would refuse (an unknown space or
  status) is listed, not written.
- A later batch keeps what the destination already has: a page whose name is
  already taken there is refused and listed. Selected links must still appear
  in the retained page's current text, so later batches cannot restore links
  removed by destination edits. That is the destination's text even when the
  batch selects the page again, because the destination keeps its own row.
"""

from __future__ import annotations

import logging
from collections.abc import Collection
from enum import StrEnum
from typing import Any, ClassVar

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

logger = logging.getLogger(__name__)


class KnowledgeTable(StrEnum):
    PAGE = "page"
    REVISION = "revision"
    NOTE = "note"
    LINK = "link"
    REDIRECT = "redirect"
    CONTRIBUTION = "contribution"


_TABLES: dict[KnowledgeTable, str] = {
    KnowledgeTable.PAGE: "knowledge_pages",
    KnowledgeTable.REVISION: "knowledge_revisions",
    KnowledgeTable.NOTE: "knowledge_notes",
    KnowledgeTable.LINK: "knowledge_links",
    KnowledgeTable.REDIRECT: "knowledge_redirects",
    KnowledgeTable.CONTRIBUTION: "knowledge_contributions",
}
_ID_COLUMNS: dict[KnowledgeTable, tuple[str, ...]] = {
    KnowledgeTable.PAGE: ("page_id",),
    KnowledgeTable.REVISION: ("revision_id",),
    KnowledgeTable.NOTE: ("note_id",),
    KnowledgeTable.LINK: ("page_id", "target_space", "target_slug"),
    KnowledgeTable.REDIRECT: ("team_id", "space", "owner_person_id", "from_slug"),
    KnowledgeTable.CONTRIBUTION: ("contribution_id",),
}
# Rows that exist only with their page(s), by the columns naming them.
_PAGE_REFERENCES: dict[KnowledgeTable, tuple[str, ...]] = {
    KnowledgeTable.REVISION: ("page_id",),
    KnowledgeTable.NOTE: ("page_id",),
    KnowledgeTable.LINK: ("page_id",),
    KnowledgeTable.REDIRECT: ("from_page_id", "to_page_id"),
    KnowledgeTable.CONTRIBUTION: ("page_id",),
}
# Rows checked only for their space; the others with the store's record converters.
_SPACE_COLUMNS: dict[KnowledgeTable, str] = {
    KnowledgeTable.LINK: "target_space",
    KnowledgeTable.REDIRECT: "space",
}


class KnowledgeTransferRecordV1(TransferModel):
    """One stored knowledge row; ``row`` has exactly the table's columns."""

    table: KnowledgeTable
    row: dict[str, JsonValue] = Field(min_length=1)


def record_key(table: KnowledgeTable, row: dict[str, Any]) -> str:
    return f"{table.value}/" + "/".join(str(row.get(column) or "") for column in _ID_COLUMNS[table])


def _repository(storage_settings: Any) -> Any:
    from storage.knowledge_repository import create_knowledge_repository

    return create_knowledge_repository(storage_settings)


def _removed_people(source: ExportSource) -> set[str]:
    from access_control.people import MEMBERSHIP_STATUS_REMOVED
    from storage.gateway_state_repository import create_gateway_state_repository

    repository = create_gateway_state_repository(source.config.state_storage_settings)
    return {
        member.canonical_user_id
        for member in repository.list_team_people(source.team_id)
        if member.membership_status == MEMBERSHIP_STATUS_REMOVED
    }


def _row_problem(table: KnowledgeTable, row: dict[str, Any]) -> str:
    """Why this version refuses the row, or an empty string."""

    from knowledge_base.vocabulary import KnowledgeSpace
    from storage.knowledge_repository import KNOWLEDGE_TABLE_COLUMNS, knowledge_row_converters

    table_name = _TABLES[table]
    expected = set(KNOWLEDGE_TABLE_COLUMNS[table_name])
    if set(row) != expected:
        missing, extra = sorted(expected - set(row)), sorted(set(row) - expected)
        return f"this version refuses its columns (missing {missing}, unknown {extra})"
    try:
        if table in _SPACE_COLUMNS:
            KnowledgeSpace(str(row[_SPACE_COLUMNS[table]]))
        else:
            knowledge_row_converters()[table_name](row)
    except (ValueError, TypeError, KeyError) as exc:
        return f"this version refuses it: {exc}"
    return ""


def _import_problem(
    table: KnowledgeTable,
    row: dict[str, Any],
    people: set[str],
    existing_ids: set[str],
    existing_names: set[tuple[Any, ...]],
    accepted_pages: Collection[str],
) -> str:
    """Why the import refuses this row, or an empty string."""

    problem = _row_problem(table, row)
    if problem:
        return problem
    owner = str(row.get("owner_person_id") or "")
    if owner and owner not in people:
        return f"it belongs to {owner}, who is not imported"
    if table is KnowledgeTable.PAGE:
        name = (row["team_id"], row["space"], owner, row["slug"])
        if name in existing_names and row["page_id"] not in existing_ids:
            return "a page with this name already exists in this instance"
    for column in _PAGE_REFERENCES.get(table, ()):
        # A proposed new page has no page id until it is published.
        if table is KnowledgeTable.CONTRIBUTION and not row[column]:
            continue
        if row[column] not in accepted_pages:
            return f"its page {row[column]} is not imported"
    return ""


def _link_problem(row: dict[str, Any], page: dict[str, Any]) -> str:
    """A retained page's current text, rather than an old export, owns its links."""

    from knowledge_base.text import link_targets
    from knowledge_base.vocabulary import KnowledgeSpace

    target = (KnowledgeSpace(row["target_space"]), row["target_slug"])
    if target not in link_targets(KnowledgeSpace(page["space"]), page["body"]):
        return "its link is absent from the page's current text"
    return ""


class KnowledgeSection:
    name: ClassVar[str] = "knowledge"
    schema_version: ClassVar[int] = 2
    encrypted: ClassVar[bool] = False
    after_start: ClassVar[bool] = False
    record_model: ClassVar[type[BaseModel]] = KnowledgeTransferRecordV1

    def export(self, source: ExportSource, files: SectionFiles) -> SectionExport:
        repository = _repository(source.config.manager_storage_settings)
        removed = _removed_people(source)
        records: list[dict[str, Any]] = []
        left_behind = 0
        for table, table_name in _TABLES.items():
            for row in repository.export_rows(table_name):
                if row.get("owner_person_id") and row["owner_person_id"] in removed:
                    left_behind += 1
                    continue
                records.append(KnowledgeTransferRecordV1(table=table, row=row).model_dump(mode="json"))
        if left_behind:
            logger.info(
                "Knowledge of people removed from the team stays behind. "
                "event=team_transfer_knowledge_removed_people rows=%s people=%s",
                left_behind,
                len(removed),
            )
        return SectionExport(records=records)

    # -- import ----------------------------------------------------------------

    def plan(self, records: SectionRecords, context: ImportContext) -> list[PlannedRecord]:
        people = context.accepted("people")
        existing_pages = self._existing_pages(context)
        existing_ids = set(existing_pages)
        existing_names = {
            (row["team_id"], row["space"], row["owner_person_id"] or "", row["slug"])
            for row in existing_pages.values()
        }
        accepted_pages = dict(existing_pages)
        order = list(KnowledgeTable)
        planned: list[PlannedRecord] = []
        for raw in sorted(records, key=lambda raw: order.index(KnowledgeTable(raw["table"]))):
            record = KnowledgeTransferRecordV1.model_validate(raw)
            row = dict(record.row)
            key = record_key(record.table, row)
            problem = _import_problem(record.table, row, people, existing_ids, existing_names, accepted_pages)
            if not problem and record.table is KnowledgeTable.LINK:
                problem = _link_problem(row, accepted_pages[str(row["page_id"])])
            if problem:
                planned.append(PlannedRecord(self.name, key, RecordOutcome.REFUSED, problem))
                continue
            # The destination keeps its own row for a page it already has
            # (``import_rows`` leaves present rows), so that row's text, not the
            # backup's, decides which of the page's links may arrive. Later
            # batches drop present pages before planning (``_skip_present``);
            # this keeps the rule true here without depending on that.
            if record.table is KnowledgeTable.PAGE and str(row["page_id"]) not in existing_pages:
                accepted_pages[str(row["page_id"])] = row
            planned.append(PlannedRecord(self.name, key, RecordOutcome.IMPORT))
        return planned

    def _existing_pages(self, context: ImportContext) -> dict[str, dict[str, Any]]:
        """Pages the destination already has (later batches), read without changing the store."""

        if not context.existing:
            return {}
        from storage.read_only import read_only_repositories
        from team_transfer.settings import planned_config, storage_environment

        with storage_environment(context), read_only_repositories():
            rows = _repository(planned_config(context).manager_storage_settings).export_rows("knowledge_pages")
        return {str(row["page_id"]): row for row in rows}

    def apply(self, records: SectionRecords, plan: list[PlannedRecord], context: ImportContext) -> None:
        from team_transfer.settings import planned_config

        accepted = {record.key for record in plan if record.outcome is not RecordOutcome.REFUSED}
        rows: dict[KnowledgeTable, list[dict[str, Any]]] = {table: [] for table in KnowledgeTable}
        for raw in records:
            record = KnowledgeTransferRecordV1.model_validate(raw)
            if record_key(record.table, dict(record.row)) in accepted:
                rows[record.table].append(dict(record.row))
        repository = _repository(planned_config(context).manager_storage_settings)
        written = {table.value: repository.import_rows(_TABLES[table], rows[table]) for table in KnowledgeTable}
        logger.info(
            "Team knowledge imported. event=team_transfer_knowledge_imported written=%s refused=%s",
            written,
            len(plan) - len(accepted),
        )



__all__ = ["KnowledgeSection", "KnowledgeTable", "KnowledgeTransferRecordV1", "record_key"]
