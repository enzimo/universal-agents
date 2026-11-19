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
- Where it is chosen: onboarding's Import page and ``--new-team-id`` choose it for a first
  import into a fresh root (``new_team_ids``). After that the id comes from what the first
  import recorded: its receipt (``state/team-import/receipt.json``, ``recorded_team_id``) for
  later batches and credentials, and the review folder for an import of a prepared review
  (``target_team_ids``).
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

import json
import logging
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Callable, Iterable

from team_transfer.errors import TransferError

if TYPE_CHECKING:
    from runtime_settings.instance_layout import InstanceLayout

LOGGER = logging.getLogger(__name__)

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


def suggested_team_id(exported: str) -> str:
    """The new id the Import page offers for an export whose id does not fit; it always fits.

    The same cut as onboarding's own ids (``onboarding_engine.shortened_team_id``), so a team
    called ``team_research_assistant_pro`` is offered ``team_research_assistant``.
    """

    from assistant_gateway.onboarding_engine import shortened_team_id

    return shortened_team_id(exported)


def team_id_explanation(exported: str) -> str | None:
    """Why the export's team id cannot be kept, in plain words for the Import page; None when it fits.

    Built from the contract's own words (``TEAM_ID.problem``), as the launcher's refusal is,
    so an id that is too long and one with characters the rule does not allow each get the
    reason that is true for them.
    """

    from custom_messages.wire.ids import TEAM_ID

    problem = TEAM_ID.problem(exported)
    if problem is None:
        return None
    return (
        f"This version carries the team id in every message address, and this team's id {exported!r} {problem}. "
        "Choose a new team id: everything imported gets the new id, and the export file stays as it is."
    )


def new_team_ids(exported: str, requested: str | None) -> TeamIds:
    """The ids for a first import into a fresh root: the requested new id, checked, or the export's own.

    ``requested`` is None when no new id was asked for. A new id is given only to a team whose
    id does not fit (owner's answer, 2026-11-19), and it must pass the rule onboarding applies
    to a typed team id (``onboarding_engine.team_id_answer``). Raises ``TransferError`` with
    what to do; nothing has been written then.
    """

    from custom_messages.wire.ids import TEAM_ID

    if requested is None:
        return TeamIds.unchanged(exported)
    typed = requested.strip()
    if not typed:
        raise TransferError(
            f"The new team id is empty. Type one of at most {TEAM_ID.cap} letters, digits, '-' and '_', "
            "starting with a letter or digit."
        )
    if TEAM_ID.fits(exported):
        raise TransferError(
            f"The export's team id {exported!r} fits this version, so the team keeps it; a new team id is only for "
            "a team whose id does not fit."
        )
    from assistant_gateway.onboarding_engine import team_id_answer

    try:
        chosen = team_id_answer(typed, "")
    except ValueError as exc:
        raise TransferError(str(exc)) from exc
    return TeamIds(exported, chosen)


def recorded_team_id(layout: "InstanceLayout", exported: str) -> str:
    """The team id the first import into ``layout`` gave the team, from its receipt.

    The export's id when there is no receipt yet or the receipt predates this field (an
    import that kept the id). The caller still checks the receipt belongs to this backup
    (``batches.previous_records``) and the instance is this team (``service._import_context``).
    """

    path = layout.team_import_receipt_path
    if not path.is_file():
        return exported
    try:
        saved = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise TransferError(f"The import receipt {path} cannot be read: {exc}") from exc
    if not isinstance(saved, dict) or RECEIPT_TEAM_ID not in saved:
        return exported
    recorded = saved[RECEIPT_TEAM_ID]
    if not isinstance(recorded, str) or not recorded:
        raise TransferError(f"The import receipt {path} names no valid team id.")
    return recorded


def target_team_ids(exported: str, layout: "InstanceLayout", requested: str | None) -> TeamIds:
    """The ids an import into ``layout`` reads under, before any review is opened.

    A fresh root (no ``instance_config.yaml``): ``new_team_ids``. A root an earlier batch
    imported into: the id its first import recorded; a requested id must be that one, so a
    value the tool already has is never asked again and a different one is refused naming both.
    """

    if not layout.instance_config_path.is_file():
        return new_team_ids(exported, requested)
    recorded = recorded_team_id(layout, exported)
    if requested is not None and requested.strip() != recorded:
        raise TransferError(
            f"This instance's first import gave the team the id {recorded!r}, and later batches keep it; "
            f"{requested.strip()!r} was asked for. Leave the new team id out."
        )
    return TeamIds(exported, recorded)


__all__ = [
    "RECEIPT_TEAM_ID",
    "TeamIds",
    "new_team_ids",
    "recorded_team_id",
    "suggested_team_id",
    "target_team_ids",
    "team_id_explanation",
]
