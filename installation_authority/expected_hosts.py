"""The hosts the launcher starts, which the authority approves without asking anyone.

The launcher writes one entry per process it starts as a host: its runtime id, its
public key, roles, teams, agent types, session limit, connection class and deployment.
The authority checks joins and renewals against that entry. The file names public keys only.

The lead's entry may allow agent types from the current catalog. Its current certificate's
types remain allowed too: removing a specialist leaves its running workers running.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping

from custom_transport.protocol1.private_files import write_private_file

_VERSION = 1


@dataclass(frozen=True)
class ExpectedHost:
    runtime: str
    key_id: str
    public_key: str
    roles: frozenset[str]
    teams: frozenset[str]
    agent_types: frozenset[str] = frozenset()
    session_limit: int = 0
    agent_types_from_catalog: bool = False
    # Processes the launcher starts stay connected and run on this machine.
    connection_class: str = "connected"
    deployment: str = "local"


def read_expected_hosts(path: Path) -> dict[str, ExpectedHost]:
    """The launcher's list; empty when there is none, so every join is refused."""

    try:
        document = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}
    if document.get("version") != _VERSION:
        raise ValueError(f"{path} has an unknown format")
    return {runtime: ExpectedHost(runtime, str(entry["key_id"]), str(entry["public_key"]), frozenset(entry["roles"]),
                                  frozenset(entry["teams"]), frozenset(entry.get("agent_types", ())),
                                  int(entry.get("session_limit", 0)),
                                  connection_class=str(entry.get("connection_class", "connected")),
                                  deployment=str(entry.get("deployment", "local")),
                                  agent_types_from_catalog=bool(entry.get("agent_types_from_catalog", False)))
            for runtime, entry in document["hosts"].items()}


def write_expected_hosts(path: Path, hosts: Mapping[str, ExpectedHost]) -> None:
    """Replace the list atomically; the authority reads it at each join."""

    document = {"version": _VERSION, "hosts": {
        host.runtime: {"key_id": host.key_id, "public_key": host.public_key, "roles": sorted(host.roles),
                       "teams": sorted(host.teams), "agent_types": sorted(host.agent_types),
                       "session_limit": host.session_limit, "connection_class": host.connection_class,
                       "deployment": host.deployment, "agent_types_from_catalog": host.agent_types_from_catalog}
        for host in hosts.values()}}
    write_private_file(path, (json.dumps(document, indent=2, sort_keys=True) + "\n").encode())
