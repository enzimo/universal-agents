"""What the web layer needs from the Access page (``assistant_gateway.access_map``).

The person id always comes from the signed-in browser session; the
implementation looks up the person's roles again at every call. Views are
paged on the server (``ACCESS_MAP_PAGE_SIZE`` items); the browser chooses only
the view and the offset.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Protocol

ACCESS_MAP_PAGE_SIZE = 20
ACCESS_MAP_VIEW_AGENTS = "agents"
ACCESS_MAP_VIEW_PEOPLE = "people"
ACCESS_MAP_VIEW_TOOLS = "tools"
# Files and folders: shares between people (D15) and data folders (D16).
ACCESS_MAP_VIEW_FILES = "files"
ACCESS_MAP_VIEWS = (ACCESS_MAP_VIEW_AGENTS, ACCESS_MAP_VIEW_PEOPLE, ACCESS_MAP_VIEW_TOOLS, ACCESS_MAP_VIEW_FILES)


@dataclass(frozen=True)
class AccessBlockInput:
    """A block asked for in the Access page's form.

    ``subject_kind`` is ``agent_type``, ``agent``, ``user`` or ``team``;
    ``subject`` is the agent type's name, the agent's id or the person's id
    (empty for the whole team). ``tool_id`` may name a tool set, such as
    ``internet`` to turn internet access off. ``operation`` is
    empty to block the whole tool; ``hours`` is None for a block that lasts
    until someone removes it.
    """

    subject_kind: str
    subject: str
    tool_id: str
    operation: str
    reason: str
    hours: int | None


class AccessMapApplication(Protocol):
    async def view(self, person_id: str, *, view: str, offset: int = 0, agent: str = "") -> dict[str, Any]:
        """One page of the access map: by agent type, by person or by tool.

        ``agent`` names an agent type to open at (the page that contains it).
        """

    async def tool_operations(self, person_id: str, tool_id: str, *, offset: int = 0) -> dict[str, Any]:
        """One page of a catalog tool's operations with the level each needs."""

    async def block(self, person_id: str, block: AccessBlockInput) -> dict[str, Any]:
        """Add a block as the person; refused in plain words when they may not."""

    async def unblock(self, person_id: str, block_id: str) -> dict[str, Any]:
        """Remove a block as the person; refused in plain words when they may not."""

    async def stop_sharing(self, person_id: str, share_id: str) -> dict[str, Any]:
        """Stop a share the person made (the Files view)."""

    async def add_data_folder(self, person_id: str, name: str, path: str, *, writable: bool) -> dict[str, Any]:
        """Register a data folder; owners and administrators only."""

    async def remove_data_folder(self, person_id: str, name: str) -> dict[str, Any]:
        """Remove a data folder; owners and administrators only."""


__all__ = [
    "ACCESS_MAP_PAGE_SIZE",
    "ACCESS_MAP_VIEWS",
    "ACCESS_MAP_VIEW_AGENTS",
    "ACCESS_MAP_VIEW_FILES",
    "ACCESS_MAP_VIEW_PEOPLE",
    "ACCESS_MAP_VIEW_TOOLS",
    "AccessBlockInput",
    "AccessMapApplication",
]
