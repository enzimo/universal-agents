"""Who may see or change a person's private records.

Private records (a person's schedules, conversations, tasks, objectives,
memory, personal knowledge pages and pending knowledge contributions, gateway
sessions, workflows) belong to one person, and only that person may read or
change them. Three exceptions: every team member may read team memory and
administrators manage it; every team member may read and edit team knowledge
pages; and in the gateway's workspace
browser every member may read their own folder (``users/<their folder>``),
while administrators also see ``team/`` and ``agents/``. Nobody browses
another person's folder there. Agents reach files through folder decisions
instead (``access_control.folders``, ``AccessService.decide_folder``).

After-action report reviews (lessons from one person's work) follow the
same rule inside their storage queries: a review task sees only reviews
recorded for its verified person, and work for no person only reviews of work
for no person (``custom_agents.after_action_report_identity``).

This limits which stored records are in view; it is not a grant. Grants decide
what an agent may do (``access_control.decisions``); this decides whose data a
request may touch. The person comes from trusted identity only: the verified
signed task context on the agent side (``custom_agents.access_runtime``), the
signed-in session in the gateway. Keeping it in one place replaces the
earlier local Cedar evaluator (``authorize_private_owner_access``) at every
call site with the same rules.

System work (a built-in system agent's task that works for no person, such as
InternalImprovementAgent's maintenance run) touches no person's records. Its
one exception is ``system_work_schedule_access``: the run may read and record
the run of the schedule its signed context names, when the runtime manages
that schedule (access plan 7c).

A new task continues an existing conversation only when it works for the
person the conversation belongs to, and work for no person only an ownerless
one (``conversation_continuation``), because the earlier turns go into the
task's prompt.

Objectives are the team lead's records, all kept in its one durable scope, so
its Objective tools ask ``objective_access`` which ones a task may reach: the
Objectives of the person the task works for, and the team's Objectives (those
with no owner, made by work for no person). Every task reads team Objectives;
only work for no person or for an administrator changes them, the same rule
as team memory. A tool that takes another task's id as the source of new work
(an Objective made from it, a task continuing it) asks ``named_task_access``,
because that work then belongs to the named task's person.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Iterable

from access_control.entities import AGENT_ROLE_SYSTEM
from access_control.people import is_administrator, normalized_roles

logger = logging.getLogger(__name__)

PRIVATE_CREATE = "private.create"
PRIVATE_READ = "private.read"
PRIVATE_UPDATE = "private.update"
PRIVATE_DELETE = "private.delete"
PRIVATE_ACTIONS = frozenset({PRIVATE_CREATE, PRIVATE_READ, PRIVATE_UPDATE, PRIVATE_DELETE})

RESOURCE_CONVERSATION = "Conversation"
# A memory review job: made for one person (its source's canonical_user_id)
# and holding that person's conversation turns or memory facts.
RESOURCE_MEMORY_REVIEW = "MemoryReview"
RESOURCE_OBJECTIVE = "Objective"
RESOURCE_SCHEDULE = "Schedule"
RESOURCE_TASK = "Task"
RESOURCE_USER_MEMORY = "UserMemory"
RESOURCE_TEAM_MEMORY = "TeamMemory"
RESOURCE_FILE_PATH = "FilePath"
# Knowledge base (``knowledge_base``): a person's own pages, the team's pages,
# and a team change proposed from a person's work (private to that person
# until the curator or the person publishes it).
RESOURCE_KNOWLEDGE_PERSONAL_PAGE = "KnowledgePersonalPage"
RESOURCE_KNOWLEDGE_TEAM_PAGE = "KnowledgeTeamPage"
RESOURCE_KNOWLEDGE_CONTRIBUTION = "KnowledgeContribution"
WORKSPACE_FILE_READ = "tool.file.read"  # the gateway's workspace browser
# Memory and knowledge pages are created through this check; other records
# are created by trusted code for the signed-in or verified person.
_CREATABLE = frozenset(
    {RESOURCE_USER_MEMORY, RESOURCE_TEAM_MEMORY, RESOURCE_KNOWLEDGE_PERSONAL_PAGE, RESOURCE_KNOWLEDGE_TEAM_PAGE}
)


@dataclass(frozen=True)
class OwnershipDecision:
    allowed: bool
    reason: str


def private_access(
    *,
    person_id: str | None,
    owner_id: str | None,
    resource_type: str,
    resource_id: str = "",
    action: str = PRIVATE_READ,
    team_id: str | None = None,
    roles: Iterable[str] = (),
) -> OwnershipDecision:
    """Whether a person may act on one private record.

    ``team_id`` and ``roles`` are the person's current team and roles; they
    matter only for team memory and workspace browsing. Denials are logged
    with the record id.
    """

    role_set = normalized_roles(roles)
    # Owners are administrators too (access_control.people).
    administrator = is_administrator(role_set)
    if action == WORKSPACE_FILE_READ and resource_type == RESOURCE_FILE_PATH:
        return _workspace_read(person_id, team_id, role_set, administrator, resource_id)
    if action not in PRIVATE_ACTIONS:
        return _denied("unknown_private_action", person_id, resource_type, resource_id, action)
    if action == PRIVATE_CREATE and resource_type not in _CREATABLE:
        return _denied("private_create_resource_unsupported", person_id, resource_type, resource_id, action)
    if resource_type == RESOURCE_TEAM_MEMORY:
        member = bool(person_id and team_id and role_set)
        if member and action == PRIVATE_READ:
            return OwnershipDecision(True, "same_team_member")
        if member and administrator:
            return OwnershipDecision(True, "team_admin")
        return _denied(
            "team_admin_required" if member else "same_team_member_required",
            person_id,
            resource_type,
            resource_id,
            action,
        )
    if resource_type == RESOURCE_KNOWLEDGE_TEAM_PAGE:
        return _team_knowledge(person_id, team_id, role_set, resource_id, action)
    if person_id and owner_id and person_id == owner_id:
        return OwnershipDecision(True, "resource_owner")
    return _denied("resource_owner_required", person_id, resource_type, resource_id, action)


def _team_knowledge(
    person_id: str | None, team_id: str | None, role_set: frozenset[str], resource_id: str, action: str
) -> OwnershipDecision:
    """Team knowledge pages: every member reads and edits them (owner's decision 2026-10-10).

    Nobody deletes a team page; archiving is an edit and keeps every revision.
    """

    if not (person_id and team_id and role_set):
        return _denied("same_team_member_required", person_id, RESOURCE_KNOWLEDGE_TEAM_PAGE, resource_id, action)
    if action == PRIVATE_DELETE:
        return _denied("team_knowledge_never_deleted", person_id, RESOURCE_KNOWLEDGE_TEAM_PAGE, resource_id, action)
    return OwnershipDecision(True, "same_team_member")


def conversation_continuation(
    *, person_id: str | None, owner_id: str | None, conversation_id: str
) -> OwnershipDecision:
    """Whether a new task may continue an existing conversation.

    A task that continues a conversation adds its turn to it, and the earlier
    turns are shown to its model (``custom_agents.context_engine``). So only the
    person the conversation belongs to continues it, and work for no person
    continues only a conversation that belongs to nobody: a missing person and
    a person are different in both directions. Unlike ``private_access``, two
    missing ones match, because system work keeps its own conversations.
    ``person_id`` is whom the task verifiably works for; ``owner_id`` is the
    conversation's recorded owner.
    """

    person = str(person_id or "").strip() or None
    owner = str(owner_id or "").strip() or None
    if person == owner:
        return OwnershipDecision(True, "conversation_owner" if person else "no_person_conversation")
    return _denied("conversation_owner_required", person, RESOURCE_CONVERSATION, conversation_id, PRIVATE_UPDATE)


def objective_access(
    *,
    person_id: str | None,
    owner_id: str | None,
    objective_id: str,
    action: str = PRIVATE_READ,
    roles: Iterable[str] = (),
) -> OwnershipDecision:
    """Whether a team lead task may read or change one Objective.

    ``person_id`` and ``roles`` are whom the task verifiably works for (None
    for work for no person); ``owner_id`` is the Objective's recorded owner,
    set from the source task's person when it was created. The person's own
    Objectives are theirs to read and change. An Objective with no owner is
    the team's: every task reads it, and work for no person or for an
    administrator changes it (owner's decision 2026-10-16, like team memory).
    Another person's Objective is out of view. The listing query in
    ``storage.objective_repository`` (``ObjectiveView``) applies the same
    reading rule.
    """

    person = str(person_id or "").strip() or None
    owner = str(owner_id or "").strip() or None
    if action not in PRIVATE_ACTIONS:
        return _denied("unknown_private_action", person, RESOURCE_OBJECTIVE, objective_id, action)
    if owner is None:
        if action == PRIVATE_READ:
            return OwnershipDecision(True, "team_objective")
        if person is None:
            return OwnershipDecision(True, "team_objective_system_work")
        if is_administrator(normalized_roles(roles)):
            return OwnershipDecision(True, "team_objective_admin")
        return _denied("team_objective_admin_required", person, RESOURCE_OBJECTIVE, objective_id, action)
    if person == owner:
        return OwnershipDecision(True, "resource_owner")
    return _denied("resource_owner_required", person, RESOURCE_OBJECTIVE, objective_id, action)


def named_task_access(*, person_id: str | None, owner_id: str | None, task_id: str) -> OwnershipDecision:
    """Whether a task may name another task as the source of new work or records.

    An Objective made from a task belongs to that task's person, and a task
    continuing an Objective works for its source task's person, so a task may
    name only a task that works for the same person. As in
    ``conversation_continuation``, work for no person may name only a task
    for no person: a missing person and a person differ in both directions.
    ``owner_id`` is the named task's person, recorded at admission.
    """

    person = str(person_id or "").strip() or None
    owner = str(owner_id or "").strip() or None
    if person == owner:
        return OwnershipDecision(True, "same_person_task" if person else "no_person_task")
    return _denied("same_person_task_required", person, RESOURCE_TASK, task_id, PRIVATE_UPDATE)


def system_work_schedule_access(
    *,
    agent_role: str,
    signed_schedule_id: str | None,
    schedule_id: str,
    system_managed: bool,
    action: str = PRIVATE_READ,
) -> OwnershipDecision:
    """Whether system work may read, or record the run of, one schedule.

    Only for a task that works for no person (the caller checks that from the
    verified signed context): a built-in system agent (``agent_role``, from
    its own settings) may read the schedule its signed context names
    (``signed_schedule_id``) and update its run record, when the runtime
    manages that schedule. Nothing else: people's schedules are private, and
    changing a system schedule needs a person (``custom_agents.system_schedule_changes``).
    """

    if agent_role != AGENT_ROLE_SYSTEM:
        reason = "system_agent_required"
    elif action not in {PRIVATE_READ, PRIVATE_UPDATE}:
        reason = "read_or_record_only"
    elif not signed_schedule_id or schedule_id != signed_schedule_id:
        reason = "signed_schedule_only"
    elif not system_managed:
        reason = "system_schedule_required"
    else:
        return OwnershipDecision(True, "signed_system_schedule")
    return _denied(reason, None, RESOURCE_SCHEDULE, schedule_id, action)


def _workspace_read(
    person_id: str | None, team_id: str | None, role_set: frozenset[str], administrator: bool, resource_id: str
) -> OwnershipDecision:
    """The workspace browser: a member's own folder; administrators also the shared folders.

    ``resource_id`` is ``workspace/<path below workspaces/>``. The browser
    also refuses other people's folders by path; this is the rule it asks.
    """

    from runtime_settings.instance_layout import USERS_DIRNAME, person_folder_name

    if not (person_id and team_id and role_set):
        return _denied("team_member_required", person_id, RESOURCE_FILE_PATH, resource_id, WORKSPACE_FILE_READ)
    parts = [part for part in resource_id.removeprefix("workspace/").split("/") if part not in {"", "."}]
    own = [USERS_DIRNAME, person_folder_name(person_id)]
    if parts[:2] == own or parts in ([], [USERS_DIRNAME]):
        return OwnershipDecision(True, "own_workspace_folder")
    if administrator and parts[:1] != [USERS_DIRNAME]:
        return OwnershipDecision(True, "team_admin_workspace_read")
    return _denied("own_folder_only", person_id, RESOURCE_FILE_PATH, resource_id, WORKSPACE_FILE_READ)


def _denied(reason: str, person_id: str | None, resource_type: str, resource_id: str, action: str) -> OwnershipDecision:
    logger.info(
        "Private record access refused. event=private_access_refused reason=%s person_id=%s "
        "resource_type=%s resource_id=%s action=%s",
        reason,
        person_id,
        resource_type,
        resource_id,
        action,
    )
    return OwnershipDecision(False, reason)


__all__ = [
    "OwnershipDecision",
    "PRIVATE_ACTIONS",
    "PRIVATE_CREATE",
    "PRIVATE_DELETE",
    "PRIVATE_READ",
    "PRIVATE_UPDATE",
    "RESOURCE_CONVERSATION",
    "RESOURCE_FILE_PATH",
    "RESOURCE_KNOWLEDGE_CONTRIBUTION",
    "RESOURCE_KNOWLEDGE_PERSONAL_PAGE",
    "RESOURCE_KNOWLEDGE_TEAM_PAGE",
    "RESOURCE_MEMORY_REVIEW",
    "RESOURCE_OBJECTIVE",
    "RESOURCE_SCHEDULE",
    "RESOURCE_TASK",
    "RESOURCE_TEAM_MEMORY",
    "RESOURCE_USER_MEMORY",
    "WORKSPACE_FILE_READ",
    "conversation_continuation",
    "named_task_access",
    "objective_access",
    "private_access",
    "system_work_schedule_access",
]
