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
task's prompt. The files earlier turns received follow the same rule
(``conversation_files_access``): a task lists and opens them only while it
works for the conversation's person.

Which files become a task's and its conversation's in the first place is
decided by ``sent_files_access``: only files the gateway vouches it sent for
that task, in that conversation, for that person, with a signed context
bound to that one message and its files (``request_context_id``,
``steering_context_id``, ``answer_context_id``; see its docstring for why a
context another agent process holds or overheard cannot pass). Work done for
a request (a plan step, an isolated recovery task, work the team lead does
for an escalation) shares the request's files only when both work for the
same person (``request_files_access``), and the files people's answers bring
back to a worker with its escalation's result stay only with work for a
person (``escalation_result_files_access``).

Objectives are the team lead's records, all kept in its one durable scope, so
its Objective tools ask ``objective_access`` which ones a task may reach: the
Objectives of the person the task works for, and the team's Objectives (those
with no owner, made by work for no person). Every task reads team Objectives;
only work for no person or for an administrator changes them, the same rule
as team memory. A tool that takes another task's id as the source of new work
(an Objective made from it, a task continuing it) asks ``named_task_access``,
because that work then belongs to the named task's person. So does a tool
that reads another task's records, such as its execution digest.
"""

from __future__ import annotations

import hashlib
import json
import logging
from dataclasses import dataclass
from typing import Iterable, Sequence

from access_control.entities import AGENT_ROLE_SYSTEM
from access_control.identity import ISSUER_GATEWAY
from access_control.people import is_administrator, normalized_roles
from custom_messages.contracts.access_v2 import MESSAGE_CONTEXT_ID_PREFIX, TaskContextV1
from custom_messages.contracts.attachments_v1 import UserTaskAttachment
from custom_messages.contracts.task_v1 import Task, TaskQuery, TaskSteering

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
# The kinds of message a signed context can be bound to (``sent_files_access``);
# an id of one kind never equals an id of another.
_REQUEST_MESSAGE = "request"
_STEERING_MESSAGE = "steering"
_ANSWER_MESSAGE = "question_answer"
# ``sent_files_access``'s reason for a gateway context that names no task.
# Gateways before the update that started keeping each message's files signed
# a request's context without one, so on a request this is how one still
# queued for the team lead across that update arrives; on a guidance or an
# answer it is a context taken from another message
# (``custom_agents.conversation_attachments`` words each for the log).
CONTEXT_NAMES_NO_TASK = "context_names_no_task"
# ``escalation_result_files_access``'s reason for work for no person; the
# worker's log explains it (``custom_agents.sent_files_runtime``).
WORK_FOR_A_PERSON_REQUIRED = "work_for_a_person_required"


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

    return _conversation_person(person_id, owner_id, conversation_id, PRIVATE_UPDATE)


def conversation_files_access(
    *, person_id: str | None, owner_id: str | None, conversation_id: str
) -> OwnershipDecision:
    """Whether a task may list and open the files earlier turns of its conversation received.

    The same rule as ``conversation_continuation``: those files are part of the
    conversation's history, so only a task working for the conversation's
    person sees them, and work for no person only those of an ownerless
    conversation. ``person_id`` is whom the task verifiably works for
    (``TaskInfo.owner_person_id``, decided at admission from its signed
    context); ``owner_id`` is the conversation's recorded owner. The caller
    (``custom_agents.conversation_attachments``) asks only for the task's own
    conversation.
    """

    return _conversation_person(person_id, owner_id, conversation_id, PRIVATE_READ)


def sent_files_access(
    *,
    sender: TaskContextV1 | None,
    task_id: str,
    conversation_id: str | None,
    person_id: str | None,
    message_context_id: str,
) -> OwnershipDecision:
    """Whether the gateway vouches for the files on a message a person sent to one task.

    Uploads are the person's private records, and the agent side cannot read
    the gateway's upload records, so it keeps a message's files only when the
    gateway vouches that it sent exactly those files with this message, for
    this task: ``sender`` is the message's own signed context, already
    verified by the caller (None when missing, forged or expired). It must be
    the gateway's, name the person the task verifiably works for
    (``person_id``, decided at admission), name the task as ``root_task_id``,
    name its conversation, and have the id the caller derived from the
    message as it arrived (``message_context_id``: ``request_context_id``,
    ``steering_context_id`` or ``answer_context_id``). The gateway signs
    ``root_task_id`` and ``conversation_id``, and an id derived the same way,
    only on a new request's context, on the context of each guidance or
    interrupt answer it sends to that request, and on the context of each
    answer to a question, which names the task that asked: the request, a
    plan step or isolated recovery task of it, or the team lead's record of
    a worker's escalation (``assistant_gateway.application.request_context``,
    ``assistant_gateway.application.clarification``). A gateway context that
    names no task is refused as ``CONTEXT_NAMES_NO_TASK`` rather than
    ``another_task``, because the gateway never signs one for a message with
    files: on a request it is the context a gateway from before this rule
    signed, for a request still queued when the instance was updated, whose
    files the person must send again. Guidance and interrupt answers are
    signed again each time they are sent, and that gateway sent no files with
    answers to questions, so on those it is a context taken from another
    message, such as the gateway's status check. This rule decides only
    whether the gateway vouches for the files; which tasks then keep them is
    the agent runtime's decision (``custom_agents.sent_files_runtime``).

    Why the id: other agent processes hold or see valid gateway contexts for
    the task. Delegation copies the request's admitted context into every
    subtask, and guidance and answers travel on the operational subject that
    every agent process of the team receives, so any of them can see each
    guidance's context. Signed contexts are bearer proofs, so a process
    could send one again with other files. The id names one message (the
    request's task id, or the key the lead deduplicates guidance and answers
    on) and digests its file records exactly as the gateway sent them. A
    context reused on another message, or with any other file, has the wrong
    id. Sent again on the same message with the same files, it brings only
    files the gateway sent for the task, and recording them again adds
    nothing. A request's own context has a request id, so a worker's copy
    never passes for a guidance or an answer.
    """

    if sender is None:
        reason = "gateway_context_required"
    elif sender.issuer != ISSUER_GATEWAY:
        reason = "sender_not_gateway"
    elif _person(sender.person_id) != _person(person_id):
        reason = "another_person"
    elif not sender.root_task_id:
        reason = CONTEXT_NAMES_NO_TASK
    elif not task_id or sender.root_task_id != task_id:
        reason = "another_task"
    elif not conversation_id or sender.conversation_id != conversation_id:
        reason = "another_conversation"
    elif sender.context_id != message_context_id:
        reason = "context_not_for_message"
    else:
        return OwnershipDecision(True, "sent_by_gateway_for_task")
    return _denied(reason, person_id, RESOURCE_CONVERSATION, conversation_id or "", PRIVATE_UPDATE)


def request_files_access(
    *, person_id: str | None, request_person_id: str | None, request_task_id: str
) -> OwnershipDecision:
    """Whether work done for a request may use the request's files: read, keep and pass them on.

    A plan step, an isolated recovery task, or work the team lead does for a
    worker's escalation runs for a request (found only through the task
    manager's links, ``custom_agents.conversation_attachments.linked_request``).
    The request's files and the files sent to that work are the request's
    person's private records, so the work shares them only when it works for
    that same person (``person_id``, its own verified person recorded at
    admission; ``request_person_id``, the request's). As in
    ``named_task_access``, work for no person shares only with a request for
    no person: a missing person and a person differ in both directions.
    """

    person = _person(person_id)
    owner = _person(request_person_id)
    if person == owner:
        return OwnershipDecision(True, "same_person_request" if person else "no_person_request")
    return _denied("same_person_request_required", person, RESOURCE_TASK, request_task_id, PRIVATE_READ)


def escalation_result_files_access(*, person_id: str | None, task_id: str) -> OwnershipDecision:
    """Whether a worker's task keeps the files the team lead's result for its escalation brought.

    Those files came with people's answers to the escalation's questions, and
    the lead carries only answers the gateway vouched for the escalation's
    person (``sent_files_access``). An escalation from work for a person
    carries that person's signed context, so the files are that person's own
    and stay with the work (``person_id``, whom the waiting task verifiably
    works for). Work for no person keeps none: the lead re-signs its
    escalation for the maintenance schedule's owner, an administrator
    (``custom_agents.task_context_runtime.sign_system_escalation``), so the
    files are that administrator's, while the run's conversation belongs to
    nobody (later runs for no person would list and open them) and its home
    folder, ``workspaces/agents``, is readable by every person's work.
    """

    if _person(person_id):
        return OwnershipDecision(True, "work_for_a_person")
    return _denied(WORK_FOR_A_PERSON_REQUIRED, None, RESOURCE_TASK, task_id, PRIVATE_UPDATE)


def request_context_id(task: Task) -> str:
    """The id of the signed context the gateway issues for the new request ``task`` (``sent_files_access``)."""

    return _message_context_id(_REQUEST_MESSAGE, task.task_id, task.attachments)


def steering_context_id(steering: TaskSteering) -> str:
    """The id of the signed context the gateway issues for one guidance or answer (``sent_files_access``).

    It names the message by the key the lead deduplicates it on
    (``idempotency_key``, else ``steering_id``); the gateway keeps both
    across resends of the same held message, so every resend gets the same id.
    """

    return _message_context_id(_STEERING_MESSAGE, steering.idempotency_key or steering.steering_id, steering.attachments)


def answer_context_id(answer: TaskQuery) -> str:
    """The id of the signed context the gateway issues for a person's answer to a question (``sent_files_access``).

    It names the message by the question it answers (``task_query_id``): the
    gateway sends at most one answer per question
    (``GatewayPendingQuestions.claim_answer``), and a resend of that answer
    carries the same id and files.
    """

    return _message_context_id(_ANSWER_MESSAGE, answer.task_query_id, answer.attachments)


def _message_context_id(kind: str, message_key: str, attachments: Sequence[UserTaskAttachment]) -> str:
    # Canonical JSON of what the message is and every field of each file
    # record, in order; 80 characters, inside TaskContextV1.context_id's 120.
    material = json.dumps(
        {"kind": kind, "key": message_key, "files": [attachment.model_dump(mode="json") for attachment in attachments]},
        sort_keys=True,
        separators=(",", ":"),
    )
    return f"{MESSAGE_CONTEXT_ID_PREFIX}_{hashlib.sha256(material.encode('utf-8')).hexdigest()}"


def _person(person_id: str | None) -> str | None:
    return str(person_id or "").strip() or None


def _conversation_person(
    person_id: str | None, owner_id: str | None, conversation_id: str, action: str
) -> OwnershipDecision:
    person = _person(person_id)
    owner = _person(owner_id)
    if person == owner:
        return OwnershipDecision(True, "conversation_owner" if person else "no_person_conversation")
    return _denied("conversation_owner_required", person, RESOURCE_CONVERSATION, conversation_id, action)


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


def named_task_access(
    *, person_id: str | None, owner_id: str | None, task_id: str, action: str = PRIVATE_UPDATE
) -> OwnershipDecision:
    """Whether a task may name another task, to read it (``PRIVATE_READ``) or as a source of work.

    An Objective made from a task belongs to that task's person, and a task
    continuing an Objective works for its source task's person, so a task may
    name only a task that works for the same person; reading a task's records
    follows the same rule. As in ``conversation_continuation``, work for no
    person may name only a task for no person: a missing person and a person
    differ in both directions. ``owner_id`` is the named task's person,
    recorded at admission.
    """

    person = str(person_id or "").strip() or None
    owner = str(owner_id or "").strip() or None
    if person == owner:
        return OwnershipDecision(True, "same_person_task" if person else "no_person_task")
    return _denied("same_person_task_required", person, RESOURCE_TASK, task_id, action)


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
    "WORK_FOR_A_PERSON_REQUIRED",
    "answer_context_id",
    "conversation_continuation",
    "conversation_files_access",
    "escalation_result_files_access",
    "named_task_access",
    "objective_access",
    "private_access",
    "request_context_id",
    "request_files_access",
    "sent_files_access",
    "steering_context_id",
    "system_work_schedule_access",
]
