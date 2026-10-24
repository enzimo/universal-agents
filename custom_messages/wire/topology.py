"""Which edges a message may travel (contracts/messages/v1/team-topologies.yaml).

A receiver works out a message's edge from its own place relative to the sender for
the task the message is about, read from the task's thread record: ``up`` to the
task's requester, ``down`` to its holder (or the agent given it), ``sideways`` to
another agent of the team, ``person`` to a gateway route, ``out`` to an outside
agent or another team. It then checks the edge with ``edge_allowed`` and refuses a
message on an edge it does not allow with ``delivery.refused``. Kinds whose
``topology_edge`` is ``none`` (sent to the team services, or only by hosts) and
messages that hosts send without an agent are not topology checks.
"""

from __future__ import annotations

from custom_messages.wire.kinds import KINDS, TOPOLOGIES

PERSON = "person"
# A kind that is not a topology check: sent to the team services, or only by hosts.
NOT_CHECKED = "none"


def edge_allowed(kind: str, topology: str, sender_role: str, edge: str, *, given_by_person: bool = False) -> bool:
    """Whether an agent in ``sender_role`` may send ``kind`` on ``edge`` in ``topology``.

    The kind must list the edge (registry ``topology_edge``), and the topology must
    allow it for the role. ``given_by_person`` is true for work a person gave the
    sender directly, a run of the person's own schedule or system work with a
    configured route: its output goes to the person whatever the role
    (team-topologies.yaml fixed rules).
    """

    edges = KINDS[kind].topology_edges
    if edges == (NOT_CHECKED,):
        return True
    if edge not in edges:
        return False
    if edge == PERSON and given_by_person:
        return True
    return edge in TOPOLOGIES[topology].get(sender_role, ())
