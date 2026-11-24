"""The hosts the launcher starts, which the authority approves without asking anyone.

The launcher writes one entry per process it starts as a host: its runtime id, its
public key, roles, teams, agent types, session limit, connection class and deployment.
The authority checks joins and renewals against that entry. The file names public keys only.

The lead's entry may allow agent types from the current catalog. Its current certificate's
types remain allowed too: removing a specialist leaves its running workers running.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Mapping, Optional

from custom_messages.wire.encoding import format_timestamp
from custom_messages.wire.values import parse_timestamp
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
    # Per-team gateway routes/edges this host serves (decision 102, several gateways).
    edges: Mapping[str, tuple[str, ...]] = field(default_factory=dict)
    # Optional bootstrap NATS user public key issued for this host's join (S1), and its JWT expiry (C12).
    bootstrap_nats_public_key: Optional[str] = None
    bootstrap_nats_expires_at: Optional[datetime] = None

    def __post_init__(self) -> None:
        if self.edges:
            normalized = {str(team): tuple(sorted(set(str(route) for route in routes)))
                          for team, routes in sorted(self.edges.items())}
            object.__setattr__(self, "edges", normalized)
        elif not isinstance(self.edges, dict):
            object.__setattr__(self, "edges", {})


def read_expected_hosts(path: Path) -> dict[str, ExpectedHost]:
    """The launcher's list; empty when there is none, so every join is refused."""

    try:
        document = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}
    if document.get("version") != _VERSION:
        raise ValueError(f"{path} has an unknown format")
    result: dict[str, ExpectedHost] = {}
    for runtime, entry in document["hosts"].items():
        raw_edges = entry.get("edges") or {}
        edges = {str(team): tuple(sorted(set(str(route) for route in routes)))
                 for team, routes in raw_edges.items()}
        bootstrap_key = entry.get("bootstrap_nats_public_key")
        bootstrap_exp_raw = entry.get("bootstrap_nats_expires_at")
        bootstrap_exp = parse_timestamp(str(bootstrap_exp_raw)) if bootstrap_exp_raw else None
        result[runtime] = ExpectedHost(
            runtime,
            str(entry["key_id"]),
            str(entry["public_key"]),
            frozenset(entry["roles"]),
            frozenset(entry["teams"]),
            frozenset(entry.get("agent_types", ())),
            int(entry.get("session_limit", 0)),
            connection_class=str(entry.get("connection_class", "connected")),
            deployment=str(entry.get("deployment", "local")),
            agent_types_from_catalog=bool(entry.get("agent_types_from_catalog", False)),
            edges=edges,
            bootstrap_nats_public_key=str(bootstrap_key) if bootstrap_key else None,
            bootstrap_nats_expires_at=bootstrap_exp,
        )
    return result


def write_expected_hosts(path: Path, hosts: Mapping[str, ExpectedHost]) -> None:
    """Replace the list atomically; the authority reads it at each join."""

    serialized: dict[str, dict[str, object]] = {}
    for host in hosts.values():
        entry: dict[str, object] = {
            "key_id": host.key_id,
            "public_key": host.public_key,
            "roles": sorted(host.roles),
            "teams": sorted(host.teams),
            "agent_types": sorted(host.agent_types),
            "session_limit": host.session_limit,
            "connection_class": host.connection_class,
            "deployment": host.deployment,
            "agent_types_from_catalog": host.agent_types_from_catalog,
        }
        if host.edges:
            entry["edges"] = {team: list(routes) for team, routes in sorted(host.edges.items())}
        if host.bootstrap_nats_public_key:
            entry["bootstrap_nats_public_key"] = host.bootstrap_nats_public_key
        if host.bootstrap_nats_expires_at is not None:
            entry["bootstrap_nats_expires_at"] = format_timestamp(host.bootstrap_nats_expires_at)
        serialized[host.runtime] = entry
    document = {"version": _VERSION, "hosts": serialized}
    write_private_file(path, (json.dumps(document, indent=2, sort_keys=True) + "\n").encode())
