"""Memory facts in the change ledger (``storage.change_ledger``): their fields, and who changes them.

A memory fact (coremem or usermem) is one ledger record of kind
``memory_fact``, keyed by its internal id (``fact_uuid``), never by its number:
a deleted fact's number names the next new fact. Its fields are what a change
can set, named as the agent ``memory`` tool names them: ``fact_text``,
``tags``, ``metadata``, ``valid_from_utc`` and ``invalid_after_utc``. A
request that was shown another writer's change may still change the fields
that change did not set (coordination plan 7.10, revision 4).

- Creating a fact, and an undo that brings back a deleted or expired one,
  create the record: recorded, never refused.
- A replace sets the fields whose values it changes; an undo of a replace sets
  the fields it restores.
- A delete, and an undo of a create, remove the record: every field.
- Expiry is not recorded. It is system upkeep, which never counts against a
  request, and a personal fact's row has no team id while the ledger is kept
  per team. The fact's version still changes, so a change based on the
  expired fact is refused by the store's version check.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping

from custom_messages.enums import ChangeRecordKind
from storage.change_ledger import ALL_FIELDS, ChangeType, ChangeWriter, ChangeWriterKind, PlannedChange, RecordRef

# A fact's fields as the ledger and briefs name them, and the stored column of each.
FACT_FIELD_COLUMNS = {
    "fact_text": "fact_text",
    "tags": "tags_json",
    "metadata": "metadata_json",
    "valid_from_utc": "valid_from_utc",
    "invalid_after_utc": "invalid_after_utc",
}
# How a brief shows a fact that never expires.
_NEVER_EXPIRES = "never"


@dataclass(frozen=True)
class MemoryWriter:
    """Who changes a fact: the verified writer, and the team whose change ledger records it.

    The team comes from the writer's side because a personal fact's row has no
    team id. Agent tools build the writer from the task's verified identity
    (``custom_agents.change_writers``), people's commands from the person the
    gateway signed for, and trusted imports use ``MemoryWriter.system``.
    """

    team_id: str
    writer: ChangeWriter

    @classmethod
    def system(cls, team_id: str) -> "MemoryWriter":
        return cls(team_id=team_id, writer=ChangeWriter(ChangeWriterKind.SYSTEM))


def fact_label(row: Mapping[str, Any]) -> str:
    return f"{row['bank']} #{row['memory_id']}"


def fact_values(row: Mapping[str, Any]) -> dict[str, str]:
    """A fact's field values as text, from a stored row or the values a change will store."""

    data = dict(row)  # database rows (sqlite3.Row) have no .get
    values = {name: str(data.get(column) or "") for name, column in FACT_FIELD_COLUMNS.items()}
    values["invalid_after_utc"] = values["invalid_after_utc"] or _NEVER_EXPIRES
    return values


def planned_creation(row: Mapping[str, Any]) -> PlannedChange:
    """A new fact, or a deleted or expired one an undo brings back."""

    return PlannedChange(
        _record(row), ChangeType.CREATE, label=fact_label(row), fields=frozenset({ALL_FIELDS}), planned=fact_values(row)
    )


def planned_update(current: Mapping[str, Any], next_values: Mapping[str, Any]) -> PlannedChange:
    """A change to an existing fact: the fields whose values differ from ``current``."""

    now, planned = fact_values(current), fact_values({**dict(current), **dict(next_values)})
    changed = frozenset(name for name, value in planned.items() if value != now[name])
    return PlannedChange(
        _record(current),
        ChangeType.CHANGE,
        label=fact_label(current),
        fields=changed,
        current=now,
        planned={name: planned[name] for name in sorted(changed)},
    )


def planned_removal(current: Mapping[str, Any]) -> PlannedChange:
    """A delete, or an undo of a create: every field."""

    return PlannedChange(
        _record(current),
        ChangeType.CHANGE,
        label=fact_label(current),
        fields=frozenset({ALL_FIELDS}),
        current=fact_values(current),
    )


def _record(row: Mapping[str, Any]) -> RecordRef:
    return RecordRef(ChangeRecordKind.MEMORY_FACT, str(row["fact_uuid"]))


__all__ = [
    "FACT_FIELD_COLUMNS",
    "MemoryWriter",
    "fact_label",
    "fact_values",
    "planned_creation",
    "planned_removal",
    "planned_update",
]
