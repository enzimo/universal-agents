"""Standard grants: what roles hold without any grant rows.

Section 4.2 of the access plan gives every role standard authority. For tools
these are three tool sets in the catalog (``ua_definitions/access/tools``):

- ``agent_runtime``: what every agent needs for its own work (messages, its
  task queue, its own context, skills, files in its task's folders);
- ``team_lead_runtime``: creating, messaging and steering the team's agents;
- ``system_duties``: fixed duties of built-in system agents (memory review,
  memory-review cleanup, model capability lookup); system agents also hold
  ``sandboxed_code`` (see ``STANDARD_TOOL_SETS_BY_ROLE``);
- ``internet``: web research tools and the network for code in the sandbox
  (decision D14: on by default; a block turns it off for an agent type, an
  agent, a person or the whole team).

Agents hold them by role. Members hold the runtime sets too, because an agent
never exceeds the person it works for and these sets only let agents do their
own work. Members also hold the ``standard`` set (everyday tools, never code
or platform controls), the ``sandboxed_code`` set (shell and Python in the
sandbox, never unconfined) and the ``mcp_servers`` set (MCP servers an
administrator installed), so that they can authorize agents for their own
work (decision D13) without an administrator deciding each request; an
administrator restricts a member with a block. Sandboxed code reaches only the
person's own folders and the shared folders, and the network only with
internet access, so it adds nothing beyond their file tools and internet. Administrators and owners
already hold everything team-owned (``std:admin`` in ``policies.cedar``).

Every role also holds ``write`` on the shared ``team`` and ``agents`` folders
(``access_control.folders``). A person's own folder needs no grant: the
``std:task-person-folder`` policy lets an agent use the folder of the person
its task works for, and ``std:owner`` lets that person use it.

Standard grants are generated per decision, are never delegable, and cannot be
revoked; restricting them needs a block.
"""

from __future__ import annotations

from datetime import datetime, timezone

from access_catalog.catalog import MCP_SERVERS_TOOL_SET
from access_control.entities import (
    AGENT_ROLE_SYSTEM,
    AGENT_ROLE_TEAM_LEAD,
    AGENT_ROLE_WORKER,
    agent_type_id,
    role_id,
)
from access_control.folders import STANDARD_SHARED_FOLDERS
from access_control.people import PERSON_ROLE_MEMBER
from custom_messages.contracts.runtime_v1 import KNOWLEDGE_CURATOR_AGENT_TYPE
from custom_messages.contracts.access_v2 import (
    AccessLevel,
    GrantV1,
    SubjectKind,
    SubjectRefV1,
    TargetKind,
    TargetRefV1,
)

AGENT_RUNTIME_SET = "agent_runtime"
TEAM_LEAD_RUNTIME_SET = "team_lead_runtime"
SYSTEM_DUTIES_SET = "system_duties"
INTERNET_SET = "internet"
STANDARD_SET = "standard"
# Shell and Python in the sandbox, never unconfined (owner's decision
# 2026-09-25): members hold it so they can authorize it for their own work,
# and the Trusted starter profile gives it to TeamLead.
SANDBOXED_CODE_SET = "sandboxed_code"
# Installed MCP servers (access plan 7d): members hold them like other team
# tools. Installing a server is an administrator's decision; once installed,
# a person still authorizes agents for their own work (grants on mcp:<name>,
# decision D13), and which credential a call uses comes from account links
# (the person's own first, then the team's), never from this set.
MCP_SERVERS_SET = MCP_SERVERS_TOOL_SET
# Publishing or rejecting a person's proposed team knowledge changes
# (fn:knowledge_curation): only the curator's agent type and people hold it
# (owner's decision 2026-10-10). Other system duties do not authorize publication.
KNOWLEDGE_REVIEW_SET = "knowledge_review"
# The tool that opens the network for sandboxed code (custom_agents.code_access).
INTERNET_TOOL_ID = "fn:internet"
STANDARD_TOOL_SETS_BY_ROLE: dict[str, tuple[str, ...]] = {
    AGENT_ROLE_WORKER: (AGENT_RUNTIME_SET,),
    AGENT_ROLE_TEAM_LEAD: (AGENT_RUNTIME_SET, TEAM_LEAD_RUNTIME_SET),
    # Built-in system agents (MemReviewAgent, InternalImprovementAgent) also
    # hold sandboxed code (access plan 7c): building team CLIs and skills is
    # InternalImprovementAgent's job, and with no person behind a task the
    # sandbox reaches only the shared agents/ and team/ folders. In work for a
    # person the person side still applies, as for every agent.
    AGENT_ROLE_SYSTEM: (AGENT_RUNTIME_SET, SYSTEM_DUTIES_SET, SANDBOXED_CODE_SET),
    PERSON_ROLE_MEMBER: (
        AGENT_RUNTIME_SET, TEAM_LEAD_RUNTIME_SET, STANDARD_SET, SANDBOXED_CODE_SET, MCP_SERVERS_SET, KNOWLEDGE_REVIEW_SET
    ),
}
STANDARD_TOOL_SETS_BY_AGENT_TYPE: dict[str, tuple[str, ...]] = {
    KNOWLEDGE_CURATOR_AGENT_TYPE: (KNOWLEDGE_REVIEW_SET,),
}
# Internet access is standard for every role at read (every internet operation
# is a read), decision D14.
INTERNET_ROLES = (AGENT_ROLE_WORKER, AGENT_ROLE_TEAM_LEAD, AGENT_ROLE_SYSTEM, PERSON_ROLE_MEMBER)
STANDARD_GRANT_PREFIX = "std-"
_EPOCH = datetime(2026, 1, 1, tzinfo=timezone.utc)


def _standard(
    team_id: str, holder: str, suffix: str, target: TargetRefV1, level: AccessLevel,
    *, subject: SubjectRefV1 | None = None,
) -> GrantV1:
    return GrantV1(
        grant_id=f"{STANDARD_GRANT_PREFIX}{holder}-{suffix}",
        team_id=team_id,
        subject=subject or SubjectRefV1(kind=SubjectKind.ROLE, id=role_id(team_id, holder)),
        target=target,
        level=level,
        granted_by=SubjectRefV1(kind=SubjectKind.TEAM, id=team_id),
        reason="Standard grant for the agent type." if subject else "Standard grant for the role.",
        created_at_utc=_EPOCH,
    )


def standard_grants(team_id: str) -> list[GrantV1]:
    grants: list[GrantV1] = []
    for role, tool_sets in STANDARD_TOOL_SETS_BY_ROLE.items():
        for tool_set in tool_sets:
            grants.append(
                _standard(team_id, role, tool_set, TargetRefV1(kind=TargetKind.TOOL_SET, id=tool_set), AccessLevel.FULL)
            )
        if role in INTERNET_ROLES:
            grants.append(
                _standard(team_id, role, INTERNET_SET, TargetRefV1(kind=TargetKind.TOOL_SET, id=INTERNET_SET), AccessLevel.READ)
            )
        for folder in STANDARD_SHARED_FOLDERS:
            grants.append(
                _standard(
                    team_id, role, f"folder-{folder}", TargetRefV1(kind=TargetKind.FOLDER, id=folder), AccessLevel.WRITE
                )
            )
    # Type-specific duties use the same Cedar grant path as role duties. A
    # system role alone must never make another maintenance agent a curator.
    for agent_type, tool_sets in STANDARD_TOOL_SETS_BY_AGENT_TYPE.items():
        subject = SubjectRefV1(kind=SubjectKind.AGENT_TYPE, id=agent_type_id(team_id, agent_type))
        for tool_set in tool_sets:
            grants.append(
                _standard(
                    team_id, agent_type, tool_set, TargetRefV1(kind=TargetKind.TOOL_SET, id=tool_set),
                    AccessLevel.FULL, subject=subject,
                )
            )
    return grants


def is_standard_grant(grant_id: str) -> bool:
    return grant_id.startswith(STANDARD_GRANT_PREFIX)


__all__ = [
    "AGENT_RUNTIME_SET",
    "INTERNET_SET",
    "INTERNET_TOOL_ID",
    "MCP_SERVERS_SET",
    "SANDBOXED_CODE_SET",
    "STANDARD_SET",
    "STANDARD_TOOL_SETS_BY_ROLE",
    "SYSTEM_DUTIES_SET",
    "TEAM_LEAD_RUNTIME_SET",
    "is_standard_grant",
    "standard_grants",
]
