"""Who may use an MCP server once it is installed (access plan 7d).

An installed MCP server is one team tool, ``mcp:<name>``
(``access_catalog.catalog.mcp_tool_definition``). Installing one is an
administrator's decision; using it follows grants on that tool, like any other
tool, so an approval says both at once and the agent never comes back for each
of the server's functions:

- **Level.** Approving a server lets agent types use every function it has:
  ``full`` on the whole tool. Operations whose level nobody confirmed need
  ``full``, and the server may add operations later, so a narrower grant would
  send the agent back for each one. People who want read-only use confirm
  levels (``assistant_cli mcp-operation-levels``) and give a narrower grant.
- **Proposals** (``access_control.proposals``): TeamLead's proposal installs
  the server for the work of the person the proposal's task works for. When it
  names no grant rows for the server, trusted code adds one
  (``default_proposal_grants``): the team lead, which may hand it on to
  workers without asking (delegation depth 1). Workers keep no lasting access
  ("access follows the work"); each task they are given gets the server from
  the team lead. The row is on the review card with "Keep for later tasks".
- **Direct approvals** (the gateway's Settings page, a hosted server's
  install change): the approver is an administrator, so the server's target
  agent types may use it for everyone's work (``everyone_grants``); a server
  for every agent type (``*``, from the operator's configuration) is given to
  the whole team.

Credentials are separate: the broker picks the account link at call time (the
person's own first, then the team's). A grant never selects a credential.
"""

from __future__ import annotations

from collections.abc import Iterable
from datetime import datetime

from access_control.entities import agent_type_id
from custom_messages.contracts.access_v2 import (
    AccessLevel,
    GrantLifetime,
    GrantV1,
    ProposedGrantV1,
    SubjectKind,
    SubjectRefV1,
    TargetKind,
    TargetRefV1,
)
from id_generation import new_id

# The level an approval gives: every function of the server (module docstring).
MCP_SERVER_USE_LEVEL = AccessLevel.FULL
# A record naming every agent type (servers from the operator's configuration).
ALL_AGENT_TYPES = "*"


def server_target(tool_id: str) -> TargetRefV1:
    return TargetRefV1(kind=TargetKind.TOOL, id=tool_id)


def named_agent_types(target_agent_types: Iterable[str]) -> list[str]:
    """The concrete agent types a record names, in order, without ``*``."""

    return sorted({str(item).strip() for item in target_agent_types if str(item).strip() not in {"", ALL_AGENT_TYPES}})


def default_proposal_grants(tool_id: str, *, lead_agent_type: str) -> tuple[ProposedGrantV1, ...]:
    """The grant row for a proposal that installs a server and names none for it.

    The team lead keeps the server and hands it on without asking; workers get
    it for each task they are given ("access follows the work"), so there are
    no rows for them.
    """

    return (
        ProposedGrantV1(
            agent_type=lead_agent_type,
            target=server_target(tool_id),
            level=MCP_SERVER_USE_LEVEL,
            delegation_depth=1,
            reason="Use the MCP server and hand it on to workers for their tasks without asking.",
        ),
    )


def everyone_grants(
    *,
    team_id: str,
    tool_id: str,
    target_agent_types: Iterable[str],
    approved_by: str,
    approval_id: str | None,
    now: datetime,
) -> list[GrantV1]:
    """Grants an administrator's direct approval gives: the target types, for everyone's work."""

    targets = [str(item).strip() for item in target_agent_types]
    subjects = (
        [SubjectRefV1(kind=SubjectKind.TEAM, id=team_id)]
        if ALL_AGENT_TYPES in targets
        else [
            SubjectRefV1(kind=SubjectKind.AGENT_TYPE, id=agent_type_id(team_id, agent_type))
            for agent_type in named_agent_types(targets)
        ]
    )
    return [
        GrantV1(
            grant_id=new_id("grant"),
            team_id=team_id,
            subject=subject,
            target=server_target(tool_id),
            level=MCP_SERVER_USE_LEVEL,
            lifetime=GrantLifetime.UNTIL_REVOKED,
            granted_by=SubjectRefV1(kind=SubjectKind.USER, id=approved_by),
            approval_id=approval_id,
            reason=f"An administrator installed the MCP server {tool_id} for these agents.",
            created_at_utc=now,
            for_person_id=None,
        )
        for subject in subjects
    ]


__all__ = [
    "ALL_AGENT_TYPES",
    "MCP_SERVER_USE_LEVEL",
    "default_proposal_grants",
    "everyone_grants",
    "named_agent_types",
    "server_target",
]
