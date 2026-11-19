"""A fresh import refuses, before it writes anything, a team whose ids protocol 1 cannot carry.

Why (G1 of pull request 152's review): protocol 1 carries the team id and each agent type in
message addresses, with a cap of 24 characters each (``custom_messages.wire.ids``). The
launcher's id check (``instance_upgrades.protocol1_ids``) refuses an instance whose ids do
not fit at its first start, but after an import that comes once the whole team is written
and onboarding has ended, with advice meant for an instance upgraded in place. A transfer
keeps the team id and the agent types, so a fresh import applies the same caps first:
onboarding's Import when the export is opened, before its review is prepared
(``assistant_gateway.onboarding_web.team_import``), and every plan for a fresh root
(``service._import_context``), which covers the command-line import, the operator's review
page and onboarding's own validation and import.

What it checks, as the launcher does for the catalog: the team id the import writes
(``TransferReader.team_ids``: the export's own, or the new id the import gives the team,
owner's decision 103 and ``team_transfer.team_ids``) and every agent type its ``control``
section defines (``control.defined_agent_types``), selected or not. The release's own agent
types fit. Agent ids are not checked: no section carries the records that hold them. A team
id that does not fit is refused naming how to give the team a new one; an agent type keeps
its name, so its refusal tells the owner to rename it on the instance the export came from.

A later batch into an instance that exists is not checked: its team id is the instance's, and
an agent type added there later gets no session on the message host, which the Status page
explains (``agent_type_not_certifiable``), as for a type added any other way.
"""

from __future__ import annotations

import logging

from instance_upgrades.protocol1_ids import (
    IMPORT_REFUSED_EVENT,
    IdOverCap,
    agent_types_over_cap,
    import_refusal_message,
    team_id_over_cap,
)
from team_transfer.container import TransferReader
from team_transfer.errors import TransferError

LOGGER = logging.getLogger(__name__)


def ids_over_cap(reader: TransferReader) -> list[IdOverCap]:
    """The team id the import writes and the export's agent types, where protocol 1 cannot carry them."""

    from team_transfer.control import defined_agent_types

    return [*team_id_over_cap(reader.team_ids.imported), *agent_types_over_cap(defined_agent_types(reader))]


def require_protocol1_ids(reader: TransferReader) -> None:
    """Return when every id of the export fits; ``TransferError`` with what to do otherwise."""

    found = ids_over_cap(reader)
    if not found:
        return
    LOGGER.warning(
        "A team export holds ids protocol 1 cannot carry, so it is not imported. event=%s team_id=%r "
        "imported_team_id=%r ids=%r",
        IMPORT_REFUSED_EVENT,
        reader.manifest.team_id,
        reader.team_ids.imported,
        [f"{item.rule_what}:{item.value}" for item in found],
    )
    raise TransferError(import_refusal_message(found))


__all__ = ["ids_over_cap", "require_protocol1_ids"]
