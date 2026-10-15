"""Names shared by the knowledge store, service, tools, transfer and gateway.

Each value is stored in the database, so changing one is a persisted-format
change (team transfer section ``knowledge``).
"""

from __future__ import annotations

from enum import StrEnum

# Id prefixes (``id_generation.new_id``).
PAGE_ID_PREFIX = "kb_page"
REVISION_ID_PREFIX = "kb_rev"
NOTE_ID_PREFIX = "kb_note"
CONTRIBUTION_ID_PREFIX = "kb_contrib"
EVENT_ID_PREFIX = "kb_evt"


class KnowledgeSpace(StrEnum):
    """Where a page lives.

    ``team``: every piece of work for the team may read it. ``personal``: one
    person's knowledge, readable only in work for that person.
    """

    TEAM = "team"
    PERSONAL = "personal"


class PageType(StrEnum):
    CONCEPT = "concept"
    HOW_TO = "how-to"
    DECISION = "decision"
    LESSON = "lesson"
    REFERENCE = "reference"
    SOURCE = "source"


DEFAULT_PAGE_TYPE = PageType.REFERENCE


class PageStatus(StrEnum):
    ACTIVE = "active"
    ARCHIVED = "archived"


class ChangeKind(StrEnum):
    """Why a revision exists."""

    CREATE = "create"
    EDIT = "edit"
    ARCHIVE = "archive"
    RESTORE = "restore"
    REVERT = "revert"
    # Both pages of a merge: the target's new text, and the source archived
    # with its name kept as a redirect to the target (``KnowledgeService.merge``).
    MERGE = "merge"


class ActorKind(StrEnum):
    """Who made a change: an agent, a person in the gateway, or trusted code."""

    AGENT = "agent"
    PERSON = "person"
    SYSTEM = "system"


class ContributionKind(StrEnum):
    """A team-space change proposed from a person's work."""

    CREATE = "create"
    EDIT = "edit"
    NOTE = "note"
    ARCHIVE = "archive"


class ContributionStatus(StrEnum):
    PENDING = "pending"
    PUBLISHED = "published"
    REJECTED = "rejected"
    WITHDRAWN = "withdrawn"


class NoteStatus(StrEnum):
    OPEN = "open"
    RESOLVED = "resolved"
    DISMISSED = "dismissed"


class WriteAction(StrEnum):
    """What a write did, recorded in the edit log for limits and the activity report.

    ``freeze`` and ``unfreeze`` change no page text: they are recorded so the
    team run's preparation can tell administrators about recent freezes, and
    the activity report does not count them as writes (``PAGE_STATE_ACTIONS``).
    """

    CREATE = "create"
    EDIT = "edit"
    NOTE = "note"
    ARCHIVE = "archive"
    RESTORE = "restore"
    DECIDE = "decide"
    MERGE = "merge"
    FREEZE = "freeze"
    UNFREEZE = "unfreeze"


# Edit-log actions that change a page's state but not its text.
PAGE_STATE_ACTIONS = (WriteAction.FREEZE, WriteAction.UNFREEZE)
# The edit-log reason of a freeze the store set itself after an edit war.
FREEZE_REASON_EDIT_WAR = "edit_war"


class WriteOutcome(StrEnum):
    """``applied`` changed a page; ``pending`` proposed a team change; ``refused`` changed nothing.

    Limits count applied and pending writes; refusals are kept for the
    curator's activity report.
    """

    APPLIED = "applied"
    PENDING = "pending"
    REFUSED = "refused"
