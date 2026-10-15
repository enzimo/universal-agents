"""Cedar entities for access decisions, built only by trusted code.

Principal hierarchy: ``Agent in AgentType in Role in Team`` and
``User in Role in Team``; owners also hold the ``admin`` role. Target
hierarchy: ``Operation in Tool in ToolSet`` and ``File in Folder in … in
Folder(root)`` (``access_control.folders``). Every target carries its team and
the team's administrator role, so the standard administrator policy applies
without per-target grants, a ``platform`` flag for the agents-never-full
guardrail, and an ``adminOnly`` flag for tools only administrators hold
(``guard:admin-only-tools``). Nothing here is read from fields an agent
controls.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable

from access_catalog.catalog import ToolCatalog
from access_control.folders import folder_chain
from access_control.system_agents import builtin_system_agent_types
from access_control.people import PERSON_ROLE_ADMIN, PERSON_ROLE_MEMBER, effective_roles
from custom_messages.contracts.access_v2 import (
    GrantKind,
    GrantV1,
    OperationResolutionV1,
    OwnerScope,
    SubjectKind,
    SubjectRefV1,
    TargetKind,
    TargetRefV1,
)

AGENT_ROLE_TEAM_LEAD = "team_lead"
AGENT_ROLE_WORKER = "worker"
AGENT_ROLE_SYSTEM = "system"
ADMIN_ROLE = PERSON_ROLE_ADMIN


def agent_role_resolver(lead_agent_type: str) -> Callable[[str], str]:
    """The role each agent type holds: the configured team lead, a built-in
    system agent (``access_role: system`` in its release definition,
    ``access_control.system_agents``), or worker."""

    system = builtin_system_agent_types()

    def role_of(agent_type: str) -> str:
        if agent_type == lead_agent_type:
            return AGENT_ROLE_TEAM_LEAD
        return AGENT_ROLE_SYSTEM if agent_type in system else AGENT_ROLE_WORKER

    return role_of


def uid(kind: str, ident: str) -> dict[str, str]:
    return {"type": kind, "id": ident}


def ref(kind: str, ident: str) -> dict[str, Any]:
    return {"__entity": uid(kind, ident)}


def role_id(team_id: str, role: str) -> str:
    return f"{team_id}/{role}"


def agent_type_id(team_id: str, agent_type: str) -> str:
    return f"{team_id}/{agent_type}"


def agent_type_of(team_id: str, subject_id: str) -> str | None:
    """The agent type an ``agent_type_id`` of this team names, or None for any other id."""

    prefix = f"{team_id}/"
    if not subject_id.startswith(prefix):
        return None
    return subject_id[len(prefix):] or None


def schedule_subject_id(team_id: str, schedule_id: str) -> str:
    """The grant subject id of one scheduled job (``SubjectKind.SCHEDULE``)."""

    return f"{team_id}/{schedule_id}"


def schedule_subject(team_id: str, schedule_id: str) -> SubjectRefV1:
    return SubjectRefV1(kind=SubjectKind.SCHEDULE, id=schedule_subject_id(team_id, schedule_id))


def operation_id(tool_id: str, operation: str) -> str:
    return f"{tool_id}#{operation}"


@dataclass(frozen=True)
class AgentPrincipal:
    agent_id: str
    agent_type: str
    role: str
    team_id: str
    # The scheduled job this agent's current task is a run of, from the run's
    # verified identity only (access_control.task_identity.job_schedule_id):
    # a person's schedule a worker type runs. In a run the agent is in the
    # schedule, so the schedule's access counts there and nowhere else. None
    # outside job runs (lead-run schedules and system timers work like a
    # person's request).
    schedule_id: str | None = None
    # The request the agent's current task works on: its verified signed
    # context id, carried unchanged by every task of the request. Access for
    # one task (GrantV1.task_id) is bound to it. None outside a task.
    work_id: str | None = None

    def subjects(self) -> tuple[SubjectRefV1, ...]:
        """Every grant subject that covers this agent (its schedule only during one of its runs)."""

        schedule = (
            (SubjectRefV1(kind=SubjectKind.SCHEDULE, id=schedule_subject_id(self.team_id, self.schedule_id)),)
            if self.schedule_id
            else ()
        )
        return (
            SubjectRefV1(kind=SubjectKind.AGENT, id=self.agent_id),
            SubjectRefV1(kind=SubjectKind.AGENT_TYPE, id=agent_type_id(self.team_id, self.agent_type)),
            *schedule,
            SubjectRefV1(kind=SubjectKind.ROLE, id=role_id(self.team_id, self.role)),
            SubjectRefV1(kind=SubjectKind.TEAM, id=self.team_id),
        )

    def type_subject(self) -> SubjectRefV1:
        return SubjectRefV1(kind=SubjectKind.AGENT_TYPE, id=agent_type_id(self.team_id, self.agent_type))

    def uid(self) -> dict[str, str]:
        return uid("Agent", self.agent_id)


@dataclass(frozen=True)
class PersonPrincipal:
    person_id: str
    team_id: str
    roles: tuple[str, ...] = field(default=(PERSON_ROLE_MEMBER,))

    def role_ids(self) -> tuple[str, ...]:
        # An owner is also an administrator (access_control.people).
        return tuple(sorted(role_id(self.team_id, role) for role in effective_roles(self.roles)))

    def subjects(self) -> tuple[SubjectRefV1, ...]:
        return (
            SubjectRefV1(kind=SubjectKind.USER, id=self.person_id),
            *(SubjectRefV1(kind=SubjectKind.ROLE, id=role) for role in self.role_ids()),
            SubjectRefV1(kind=SubjectKind.TEAM, id=self.team_id),
        )

    def uid(self) -> dict[str, str]:
        return uid("User", self.person_id)


def _team_entities(team_id: str, roles: set[str]) -> list[dict[str, Any]]:
    entities: list[dict[str, Any]] = [{"uid": uid("Team", team_id), "attrs": {}, "parents": []}]
    # Owners also hold the admin role directly (PersonPrincipal.role_ids).
    for role in sorted(roles | {ADMIN_ROLE}):
        entities.append(
            {
                "uid": uid("Role", role_id(team_id, role)),
                "attrs": {"team": ref("Team", team_id)},
                "parents": [uid("Team", team_id)],
            }
        )
    return entities


def agent_entities(agent: AgentPrincipal) -> list[dict[str, Any]]:
    team = agent.team_id
    type_uid = uid("AgentType", agent_type_id(team, agent.agent_type))
    schedule = (
        [{"uid": uid("Schedule", schedule_subject_id(team, agent.schedule_id)), "attrs": {"team": ref("Team", team)},
          "parents": [uid("Team", team)]}]
        if agent.schedule_id
        else []
    )
    return [
        *_team_entities(team, {agent.role}),
        {
            "uid": type_uid,
            "attrs": {"team": ref("Team", team)},
            "parents": [uid("Role", role_id(team, agent.role)), uid("Team", team)],
        },
        *schedule,
        {
            "uid": agent.uid(),
            "attrs": {"team": ref("Team", team), "agentType": {"__entity": type_uid}},
            "parents": [type_uid, *(item["uid"] for item in schedule), uid("Role", role_id(team, agent.role)), uid("Team", team)],
        },
    ]


def person_entities(person: PersonPrincipal) -> list[dict[str, Any]]:
    team = person.team_id
    roles = {role_ref.split("/", 1)[1] for role_ref in person.role_ids()}
    return [
        *_team_entities(team, roles),
        {
            "uid": person.uid(),
            "attrs": {"team": ref("Team", team)},
            "parents": [*(uid("Role", role) for role in person.role_ids()), uid("Team", team)],
        },
    ]


def _target_attrs(
    team_id: str, *, platform: bool | None = None, admin_only: bool | None = None, owner: str | None = None
) -> dict[str, Any]:
    attrs: dict[str, Any] = {"team": ref("Team", team_id), "adminRole": ref("Role", role_id(team_id, ADMIN_ROLE))}
    if platform is not None:
        attrs["platform"] = platform
    if admin_only is not None:
        attrs["adminOnly"] = admin_only
    if owner:
        attrs["owner"] = ref("User", owner)
    return attrs


def target_entities(
    catalog: ToolCatalog,
    team_id: str,
    resolution: OperationResolutionV1,
    *,
    owner: str | None = None,
) -> tuple[dict[str, str], list[dict[str, Any]]]:
    """The operation's uid and the entities for it, its tool, and its tool sets.

    ``owner`` marks the tool as a person's own (a personal login on the person
    side of the check).
    """

    tool = catalog.tools.get(resolution.tool_id)
    platform = bool(tool and tool.owner_scope is OwnerScope.PLATFORM)
    admin_only = bool(tool and tool.owner_scope is OwnerScope.ADMINISTRATORS)
    tool_sets = tool.tool_sets if tool else ()
    entities: list[dict[str, Any]] = [
        {"uid": uid("ToolSet", tool_set), "attrs": _target_attrs(team_id), "parents": []}
        for tool_set in tool_sets
    ]
    entities.append(
        {
            "uid": uid("Tool", resolution.tool_id),
            "attrs": {
                **_target_attrs(team_id, platform=platform, admin_only=admin_only, owner=owner),
                "kind": resolution.tool_id.split(":", 1)[0],
            },
            "parents": [uid("ToolSet", tool_set) for tool_set in tool_sets],
        }
    )
    operation_uid = uid("Operation", operation_id(resolution.tool_id, resolution.operation))
    entities.append(
        {
            "uid": operation_uid,
            "attrs": {
                **_target_attrs(team_id, platform=platform, admin_only=admin_only, owner=owner),
                "requiredLevel": resolution.level.value,
            },
            "parents": [uid("Tool", resolution.tool_id)],
        }
    )
    return operation_uid, entities


def folder_entities(
    team_id: str, target_id: str, *, is_file: bool, owner: str | None = None
) -> tuple[dict[str, str], list[dict[str, Any]]]:
    """The uid of a file or folder and the entities of it and every folder up to its root.

    ``owner`` marks a person's own folder (``access_control.folders.folder_owner``);
    everything below an owned folder has the same owner.
    """

    chain = folder_chain(target_id)
    attrs = {**_target_attrs(team_id, owner=owner), "path": chain[0]}
    kind = "File" if is_file else "Folder"
    entities: list[dict[str, Any]] = [
        {
            "uid": uid(kind, chain[0]),
            "attrs": attrs,
            "parents": [uid("Folder", chain[1])] if len(chain) > 1 else [],
        }
    ]
    for index, folder_id in enumerate(chain[1:], start=1):
        entities.append(
            {
                "uid": uid("Folder", folder_id),
                "attrs": {**_target_attrs(team_id, owner=owner), "path": folder_id},
                "parents": [uid("Folder", chain[index + 1])] if index + 1 < len(chain) else [],
            }
        )
    return uid(kind, chain[0]), entities


def subject_uid(subject: SubjectRefV1) -> dict[str, str]:
    kinds = {
        SubjectKind.AGENT: "Agent",
        SubjectKind.AGENT_TYPE: "AgentType",
        SubjectKind.ROLE: "Role",
        SubjectKind.TEAM: "Team",
        SubjectKind.USER: "User",
        SubjectKind.SCHEDULE: "Schedule",
    }
    return uid(kinds[subject.kind], subject.id)


def target_uid(target: TargetRefV1) -> dict[str, str]:
    kinds = {
        TargetKind.TOOL: "Tool",
        TargetKind.TOOL_SET: "ToolSet",
        TargetKind.OPERATION: "Operation",
        TargetKind.CREDENTIAL: "Credential",
        TargetKind.FOLDER: "Folder",
        TargetKind.FILE: "File",
    }
    return uid(kinds[target.kind], target.id)


def grant_links(grant: GrantV1) -> list[dict[str, Any]]:
    """The Cedar template links for one currently valid grant."""

    principal = subject_uid(grant.subject)
    resource = target_uid(grant.target)
    if grant.kind is GrantKind.BLOCK:
        return [{"template": "tpl:block", "id": f"block:{grant.grant_id}", "principal": principal, "resource": resource}]
    if grant.kind is GrantKind.CREDENTIAL_USE:
        return [
            {"template": "tpl:credential.use", "id": f"cred:{grant.grant_id}", "principal": principal, "resource": resource}
        ]
    links = [
        {
            "template": f"tpl:grant.{grant.level.value}",
            "id": f"grant:{grant.grant_id}",
            "principal": principal,
            "resource": resource,
        }
    ]
    if grant.delegation_depth >= 1:
        links.append(
            {
                "template": f"tpl:delegate.{grant.level.value}",
                "id": f"deleg:{grant.grant_id}",
                "principal": principal,
                "resource": resource,
            }
        )
    return links


__all__ = [
    "agent_role_resolver",
    "AGENT_ROLE_SYSTEM",
    "AGENT_ROLE_TEAM_LEAD",
    "AGENT_ROLE_WORKER",
    "AgentPrincipal",
    "PersonPrincipal",
    "agent_entities",
    "folder_entities",
    "grant_links",
    "operation_id",
    "person_entities",
    "role_id",
    "subject_uid",
    "target_entities",
    "target_uid",
    "uid",
]
