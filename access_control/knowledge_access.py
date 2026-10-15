"""Whose knowledge records a person's Knowledge page request may touch.

One rule for both sides of ``ManagerKnowledgeCommand``: the gateway checks it
before sending (``GatewayManagerCommandService``) and the manager again with
the gateway-signed person (``custom_agents.knowledge_command_runtime``).
The decisions themselves are ``access_control.ownership``'s:

- team pages: every active team member reads, creates and edits them
  (``RESOURCE_KNOWLEDGE_TEAM_PAGE``; archiving is an edit, nobody deletes);
- personal pages: only their owner, and ``personal`` always names the signed-in
  person's own space, so the owner is that person;
- pending contributions: only the person whose work proposed them. The
  gateway cannot see a contribution's owner, so it checks the request as the
  person's own; the manager's knowledge service checks the stored owner
  (``KnowledgeService.get_contribution``) and answers "not found" for anyone
  else's, so ids reveal nothing.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable

from access_control.ownership import (
    PRIVATE_CREATE,
    PRIVATE_READ,
    PRIVATE_UPDATE,
    RESOURCE_KNOWLEDGE_CONTRIBUTION,
    RESOURCE_KNOWLEDGE_PERSONAL_PAGE,
    RESOURCE_KNOWLEDGE_TEAM_PAGE,
    OwnershipDecision,
    private_access,
)
from custom_messages.contracts.control_v1 import (
    KNOWLEDGE_CONTRIBUTION_ACTIONS,
    KNOWLEDGE_READ_ACTIONS,
    KnowledgeCommandAction,
    KnowledgeCommandPayload,
)

PERSONAL_SPACE = "personal"


@dataclass(frozen=True)
class KnowledgeTarget:
    """The private record a request acts on, in ``access_control.ownership`` terms."""

    resource_type: str
    resource_id: str
    action: str
    # The record's owner when the request itself fixes it; None for team pages.
    owner_person_id: str | None


def knowledge_target(command: KnowledgeCommandPayload, *, person_id: str) -> KnowledgeTarget:
    if command.action in KNOWLEDGE_CONTRIBUTION_ACTIONS:
        return KnowledgeTarget(
            RESOURCE_KNOWLEDGE_CONTRIBUTION,
            command.contribution_id or "collection",
            PRIVATE_READ if command.action in KNOWLEDGE_READ_ACTIONS else PRIVATE_UPDATE,
            person_id,
        )
    if command.action in KNOWLEDGE_READ_ACTIONS:
        action = PRIVATE_READ
    elif command.action is KnowledgeCommandAction.WRITE and not command.base_revision:
        action = PRIVATE_CREATE
    else:
        action = PRIVATE_UPDATE
    resource_id = f"{command.space}/{command.page or 'collection'}"
    if command.space == PERSONAL_SPACE:
        return KnowledgeTarget(RESOURCE_KNOWLEDGE_PERSONAL_PAGE, resource_id, action, person_id)
    return KnowledgeTarget(RESOURCE_KNOWLEDGE_TEAM_PAGE, resource_id, action, None)


def knowledge_command_access(
    command: KnowledgeCommandPayload, *, person_id: str, team_id: str, roles: Iterable[str]
) -> OwnershipDecision:
    """Whether the signed-in person may make this request (module docstring)."""

    target = knowledge_target(command, person_id=person_id)
    return private_access(
        person_id=person_id,
        owner_id=target.owner_person_id,
        resource_type=target.resource_type,
        resource_id=target.resource_id,
        action=target.action,
        team_id=team_id,
        roles=tuple(roles),
    )


__all__ = ["KnowledgeTarget", "knowledge_command_access", "knowledge_target"]
