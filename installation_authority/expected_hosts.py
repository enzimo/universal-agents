"""The hosts the launcher starts, which the authority approves without asking anyone.

The launcher writes one entry per process it starts as a host: its runtime id, its
public key and the roles, teams, agent types and sessions it may ask for. A join that
presents exactly that key for that runtime, asking for no more, is approved at once
(the embedded profile's automatic local bootstrap). The file names public keys only.

The team lead's process runs workers of any type the team defines, including specialists
created while it runs, so its entry may say ``agent_types_from_catalog``: the authority
then also allows every agent type in the instance's agent catalog as it reads it at the
join (``InstallationAuthority.catalog_agent_types``), besides the types the host's current
certificate already grants. The authority runs no model, so no process that does can widen
its own grant.
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
    agent_types_from_catalog: bool = False


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
                                  int(entry.get("session_limit", 0)), bool(entry.get("agent_types_from_catalog", False)))
            for runtime, entry in document["hosts"].items()}


def write_expected_hosts(path: Path, hosts: Mapping[str, ExpectedHost]) -> None:
    """Replace the list atomically; the authority reads it at each join."""

    document = {"version": _VERSION, "hosts": {
        host.runtime: {"key_id": host.key_id, "public_key": host.public_key, "roles": sorted(host.roles),
                       "teams": sorted(host.teams), "agent_types": sorted(host.agent_types),
                       "session_limit": host.session_limit, "agent_types_from_catalog": host.agent_types_from_catalog}
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
