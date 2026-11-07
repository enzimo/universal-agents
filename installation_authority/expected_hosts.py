"""The hosts the launcher starts, which the authority approves without asking anyone.

The launcher writes one entry per process it starts as a host: its runtime id, its
public key, the roles, teams, agent types and sessions it may ask for, its connection
class and its deployment. A join that presents exactly that key for that runtime, asking
for no more, in that connection class and deployment, is approved at once (the embedded
profile's automatic local bootstrap); a renewal is checked against the same entry. The
file names public keys only.
"""

from __future__ import annotations

import json
import os
import secrets
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping

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
                                  int(entry.get("session_limit", 0)), str(entry.get("connection_class", "connected")),
                                  str(entry.get("deployment", "local")))
            for runtime, entry in document["hosts"].items()}


def write_expected_hosts(path: Path, hosts: Mapping[str, ExpectedHost]) -> None:
    """Replace the list atomically; the authority reads it at each join."""

    document = {"version": _VERSION, "hosts": {
        host.runtime: {"key_id": host.key_id, "public_key": host.public_key, "roles": sorted(host.roles),
                       "teams": sorted(host.teams), "agent_types": sorted(host.agent_types),
                       "session_limit": host.session_limit, "connection_class": host.connection_class,
                       "deployment": host.deployment}
        for host in hosts.values()}}
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    temporary = path.with_name(f".{path.name}.{secrets.token_hex(8)}")
    descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600)
    with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
        json.dump(document, handle, indent=2, sort_keys=True)
        handle.write("\n")
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temporary, path)
