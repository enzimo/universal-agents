"""Fill a host role's generated NATS permission templates for one host.

contracts/messages/v1/README.md ("Grants for the agents a host runs"): trusted
membership code fills ``generated/permissions/<role>.json`` before it installs a
host's credential. ``{self}`` becomes the host's runtime id. A subject that names an
agent, pool or edge is repeated for each one the host runs or serves, with ``{team}``
set to that agent's, pool's or edge's own team, so a host that serves two teams gets no
grant on the other team's agent with the same id. Any other subject is repeated for
each team the host serves. No placeholder ever becomes a wildcard.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Mapping

# Placeholders that name one receiver; each belongs to one team (transport.yaml audience targets).
RECEIVER_PLACEHOLDERS = ("agent", "pool", "edge")
TEAM_PLACEHOLDER, SELF_PLACEHOLDER = "team", "self"
_PLACEHOLDER = re.compile(r"\{([a-z_]+)\}")


@dataclass(frozen=True)
class HostPlacement:
    """What one host serves: its runtime, its certificate's teams, and per team the agents, pools and edges it runs."""

    runtime: str
    teams: tuple[str, ...]
    agents: Mapping[str, tuple[str, ...]] = field(default_factory=dict)
    pools: Mapping[str, tuple[str, ...]] = field(default_factory=dict)
    edges: Mapping[str, tuple[str, ...]] = field(default_factory=dict)

    def __post_init__(self) -> None:
        for name in RECEIVER_PLACEHOLDERS:
            outside = set(getattr(self, f"{name}s")) - set(self.teams)
            if outside:
                raise ValueError(f"{name}s of teams the host does not serve: {sorted(outside)}")

    def receivers(self, placeholder: str) -> list[tuple[str, str]]:
        """(team, id) for every agent, pool or edge of the host."""

        return [(team, receiver) for team, ids in getattr(self, f"{placeholder}s").items() for receiver in ids]


def host_subjects(template: str, placement: HostPlacement) -> list[str]:
    """One template filled for one host; every result names exact ids."""

    names = set(_PLACEHOLDER.findall(template))
    unknown = names - {TEAM_PLACEHOLDER, SELF_PLACEHOLDER, *RECEIVER_PLACEHOLDERS}
    receivers = [name for name in RECEIVER_PLACEHOLDERS if name in names]
    if unknown or len(receivers) > 1:
        raise ValueError(f"cannot fill {template!r}: placeholders {sorted(names)}")
    filled = template.replace("{" + SELF_PLACEHOLDER + "}", placement.runtime)
    if receivers:
        name = receivers[0]
        return [filled.replace("{" + TEAM_PLACEHOLDER + "}", team).replace("{" + name + "}", receiver)
                for team, receiver in placement.receivers(name)]
    if TEAM_PLACEHOLDER in names:
        return [filled.replace("{" + TEAM_PLACEHOLDER + "}", team) for team in placement.teams]
    return [filled]


def host_permissions(document: Mapping[str, Any], placement: HostPlacement) -> dict[str, Any]:
    """A role's permission document filled for one host: publish and subscribe subjects, and its answer allowance."""

    filled: dict[str, Any] = {action: sorted({subject for template in document.get(action, ())
                                              for subject in host_subjects(template, placement)})
                              for action in ("publish", "subscribe")}
    if "allow_responses" in document:
        filled["allow_responses"] = document["allow_responses"]
    return filled
