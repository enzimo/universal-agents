"""The export's team id and the id an import gives the team (owner's decision 103, 2026-11-18).

Why: protocol 1 carries the team id in every message address, with a cap of 24 characters
of letters, digits, ``-`` and ``_`` (``custom_messages.wire.ids.TEAM_ID``). 0.9 built team ids
from the assistant's name with no cap, so many 0.9 teams cannot keep their id on 0.10. Import
keeps every id it carries, with this one deliberate exception: a first import into a fresh
instance may give a team whose id does not fit a new one. Nothing else is renamed (agent types,
agent ids and person ids keep theirs), and the export file is never changed.

How:

- ``TeamIds`` holds the export's id and the id the import writes. When they are equal the
  import behaves exactly as without it.
- Where it is chosen: ``team_transfer.team_id_choice`` (the suggestion, the check of a typed
  id, and where later steps read the id the first choice recorded). It lives apart from this
  module because it calls onboarding's id rule and reads the import receipt, and every
  section imports this module: keeping this one free of those imports keeps
  ``team_transfer.sections`` (which the gateway's ``/upgrade`` command reaches through
  ``export_requests``) from importing the gateway back (``test_runtime_architecture_boundaries``).
- Where it is applied: ``TransferReader.with_team_ids`` gives the open file the chosen ids, and
  ``sections.read_section_records`` renames each section's records right after its version
  step, through the rename the section registers in ``sections.TEAM_ID_RENAMES``. The review,
  record keys, selections, the receipt, every plan and every writer therefore see one set of
  records under the new id. Where the import supplies the team id itself (the new
  configuration, memberships, team memory), it takes ``ImportContext.team_id``.
- The one rename rule: every rename goes through ``TeamIds.rename`` (or ``rename_derived`` for
  a value the runtime builds from the team id, such as a hash). A value changes only when it
  equals the export's id, starts with the export's id followed by a separator the section
  names, or equals what the runtime derives from the export's id. Anything else, a third team's
  id included, comes out unchanged and gets today's outcome.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable, Iterable

# The receipt's field holding the id the first import gave the team; an older receipt without
# it means the export's own id.
RECEIPT_TEAM_ID = "team_id"


@dataclass(frozen=True)
class TeamIds:
    """The export's team id and the id the import writes; equal when the team keeps its id."""

    exported: str
    imported: str

    @classmethod
    def unchanged(cls, team_id: str) -> "TeamIds":
        return cls(team_id, team_id)

    @property
    def renamed(self) -> bool:
        return self.exported != self.imported

    def describe(self) -> str:
        """The team in words for summaries and reports: ``team X`` or ``team X, imported as Y``."""

        if not self.renamed:
            return f"team {self.exported}"
        return f"team {self.exported}, imported as {self.imported}"

    def rename(self, value: Any, *separators: str) -> Any:
        """``value`` with the export's id replaced by the new one; anything else unchanged.

        Changes a string equal to the export's id, or one that starts with it followed by one
        of ``separators`` (``team_x_TeamArchitect`` with ``"_"``, ``team_x/sched`` with
        ``"/"``). A third team's id, a longer id that merely starts with the export's id, and
        values that are not strings stay as they are.
        """

        if not self.renamed or not isinstance(value, str):
            return value
        if value == self.exported:
            return self.imported
        for separator in separators:
            head = self.exported + separator
            if value.startswith(head):
                return self.imported + separator + value[len(head) :]
        return value

    def rename_after(self, head: str, value: Any, *separators: str) -> Any:
        """``value`` with ``rename`` applied to what follows ``head``; anything else unchanged.

        For an id that names the team after a fixed head, such as the team lead's scope
        ``manager_scope:<team>:<agent type>`` (head ``manager_scope:``, separator ``:``).
        """

        if not self.renamed or not isinstance(value, str) or not value.startswith(head):
            return value
        return head + self.rename(value[len(head) :], *separators)

    def rename_derived(self, value: Any, derive: Callable[[str], str]) -> Any:
        """``derive(new id)`` when ``value`` is ``derive(export's id)``; ``value`` unchanged otherwise.

        For values the runtime builds from the team id that a text search cannot find, such
        as the maintenance schedule's id (a hash) or a hosted MCP server's id. ``derive`` is
        the owning module's own function, so the new value is what the runtime expects.
        """

        if not self.renamed or not isinstance(value, str):
            return value
        return derive(self.imported) if value == derive(self.exported) else value

    def renamed_fields(
        self, record: dict[str, Any], fields: Iterable[str], *separators: str
    ) -> dict[str, Any]:
        """A copy of ``record`` with each named top-level field passed through ``rename``.

        Fields the record lacks stay absent, so the record's model still sees what was exported.
        """

        if not self.renamed:
            return record
        changed = {name: self.rename(record[name], *separators) for name in fields if name in record}
        return {**record, **changed}


__all__ = ["RECEIPT_TEAM_ID", "TeamIds"]
