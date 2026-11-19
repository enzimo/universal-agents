"""Where the team id an import writes comes from (owner's decision 103, 2026-11-18).

``team_transfer.team_ids`` holds the mapping (``TeamIds``) and its one rename rule; this
module decides the mapping:

- For a first import into a fresh root, onboarding's Import page and ``--new-team-id`` choose
  the id (``new_team_ids``), checked with onboarding's rule for a typed team id. The page
  explains why the export's id does not fit (``team_id_explanation``) and suggests the old id
  cut to fit (``suggested_team_id``).
- After that the id comes from what the first choice recorded: the review folder
  (``review.ImportReviewV1.team_id``, read by ``review.ReviewFiles.open``) for an import of a
  prepared review, and the first import's receipt (``runtime/team-import/receipt.json``, read
  only through ``batches.import_receipt``, which also checks it came from the same backup;
  ``recorded_team_ids``) for later batches and credentials. ``target_team_ids`` decides the ids
  of a new review or a dry run from both.

Why a module of its own: these steps call ``assistant_gateway.onboarding_engine`` and read the
receipt through ``team_transfer.batches``, and both lead back to the gateway, while every
section imports ``team_ids``. Only the steps that choose or read the id (the review, the command
line, the credentials import and onboarding's Import) import this module.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from team_transfer.errors import NewTeamIdRefused, TransferError
from team_transfer.team_ids import RECEIPT_TEAM_ID, TeamIds

if TYPE_CHECKING:
    from runtime_settings.instance_layout import InstanceLayout
    from team_transfer.format import TransferManifestV1


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
    # "protocol 1" is named once here because the id rule's own refusals under the field use it.
    # The last sentence promises only what holds: free text such as a schedule's instructions
    # keeps the old id as written (only fields that hold the id are renamed).
    return (
        f"This version's messages (protocol 1) carry the team id in their addresses, and this team's id "
        f"{exported!r} {problem}. Choose a new team id: the team is imported under it, and the export file "
        "stays as it is."
    )


def new_team_ids(exported: str, requested: str | None) -> TeamIds:
    """The ids for a first import into a fresh root: the requested new id, checked, or the export's own.

    ``requested`` is None when no new id was asked for. A new id is given only to a team whose
    id does not fit (owner's answer, 2026-11-19), and it must pass the rule onboarding applies
    to a typed team id (``onboarding_engine.team_id_answer``). Raises ``NewTeamIdRefused`` for an
    id another one would replace and ``TransferError`` for a new id asked for a team that keeps
    its own, each with what to do; nothing has been written then.
    """

    from custom_messages.wire.ids import TEAM_ID

    if requested is None:
        return TeamIds.unchanged(exported)
    typed = requested.strip()
    if not typed:
        raise NewTeamIdRefused(
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
        raise NewTeamIdRefused(str(exc)) from exc
    return TeamIds(exported, chosen)


def recorded_team_id(receipt: dict[str, Any], exported: str) -> str:
    """The team id the first import gave the team, from its receipt (``batches.import_receipt``).

    The export's id for a receipt from before this field (an import that kept the id).
    """

    if RECEIPT_TEAM_ID not in receipt:
        return exported
    recorded = receipt[RECEIPT_TEAM_ID]
    if not isinstance(recorded, str) or not recorded:
        raise TransferError("The import receipt names no valid team id.")
    return recorded


def recorded_team_ids(layout: "InstanceLayout", manifest: "TransferManifestV1") -> TeamIds:
    """The export's team id and the one the first import into ``layout`` recorded in its receipt.

    ``batches.import_receipt`` refuses a root without a receipt and a receipt of another backup.
    Later batches (``target_team_ids``) and the credentials stored after the first start
    (``broker_credentials.import_credential_bundle``) read the export under these ids, so the id
    chosen once is never asked for again.
    """

    from team_transfer.batches import import_receipt

    return TeamIds(manifest.team_id, recorded_team_id(import_receipt(layout, manifest), manifest.team_id))


def target_team_ids(manifest: "TransferManifestV1", layout: "InstanceLayout", requested: str | None) -> TeamIds:
    """The ids an import of the export with ``manifest`` into ``layout`` reads under, before any review is opened.

    A fresh root (no ``instance_config.yaml``): ``new_team_ids``. A root that holds an
    instance: only one a finished first import from this same backup created
    (``runtime_guard.require_finished_import`` and ``batches.import_receipt``, which a later
    batch checks too), read under the id that import recorded. A requested id must be that
    one, so a value the tool already has is never asked again and a different one is refused
    naming both. Each refusal says what the folder really holds: an unfinished import, an
    instance no import from this backup created, or another backup's import.
    """

    if not layout.instance_config_path.is_file():
        return new_team_ids(manifest.team_id, requested)
    from team_transfer.runtime_guard import require_finished_import

    require_finished_import(layout)
    recorded = recorded_team_ids(layout, manifest)
    if requested is not None and requested.strip() != recorded.imported:
        raise TransferError(
            f"This instance's first import gave the team the id {recorded.imported!r}, and later batches keep it; "
            f"{requested.strip()!r} was asked for. Leave the new team id out."
        )
    return recorded


__all__ = [
    "new_team_ids",
    "recorded_team_id",
    "recorded_team_ids",
    "suggested_team_id",
    "target_team_ids",
    "team_id_explanation",
]
