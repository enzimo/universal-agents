"""Stable persistence scopes for manager-owned durable records.

Runtime agent ids identify one process incarnation.  Objectives, Plans, and
their execution policies must instead survive replacement managers, so their
owner is derived from the durable team/role identity.
"""

from __future__ import annotations

from typing import Optional


MANAGER_SCOPE_PREFIX = "manager_scope"


def manager_durable_scope_id(
    *,
    team_id: Optional[str],
    named_agent_type: Optional[str],
    fallback_agent_id: Optional[str] = None,
) -> Optional[str]:
    normalized_team_id = str(team_id or "").strip()
    normalized_named_agent_type = str(named_agent_type or "").strip()
    if normalized_team_id and normalized_named_agent_type:
        return (
            f"{MANAGER_SCOPE_PREFIX}:{normalized_team_id}:"
            f"{normalized_named_agent_type}"
        )
    return str(fallback_agent_id or "").strip() or None


__all__ = ["MANAGER_SCOPE_PREFIX", "manager_durable_scope_id"]
