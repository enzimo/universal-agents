"""TeamLead's line saying whether the person it works for administers the team.

The line only shapes guidance: the ``user-onboarding-guide`` tour offers its
administrator steps (adding people, team model keys, growing the team, the
Status page) to administrators and keeps them out of a member's tour, and
command suggestions can match what the person may run (owner's decision
2026-10-04). The roles come from the task's verified identity
(``access_runtime.resolve_meta_identity``: the gateway-signed context or a
scheduled run's permit), never from message fields. The line grants nothing:
every action is still decided where it runs (``access_control.people``,
``access_control.decisions``).
"""

from __future__ import annotations

from typing import Any

from access_control.people import ROLE_WORDS, PERSON_ROLE_ADMIN, PERSON_ROLE_MEMBER, PERSON_ROLE_OWNER, is_administrator, is_owner

PERSON_ROLE_CONTEXT_PREFIX = "Person you work for:"


def describe_person_role(roles: Any) -> str:
    """One line naming the person's team role for TeamLead's prompt."""

    if is_owner(roles):
        standing = f"{ROLE_WORDS[PERSON_ROLE_OWNER]} (a team administrator)"
    elif is_administrator(roles):
        standing = f"{ROLE_WORDS[PERSON_ROLE_ADMIN]} of the team"
    else:
        standing = f"{ROLE_WORDS[PERSON_ROLE_MEMBER]} (not a team administrator)"
    return (
        f"{PERSON_ROLE_CONTEXT_PREFIX} {standing}. Use this only to choose which guidance and "
        "examples fit them; it grants nothing, and administrator-only actions are still checked when they run."
    )


def person_role_context(agent: Any, meta: Any) -> str:
    """TeamLead's pinned role line for the task's verified person.

    Empty for other agents and for work for nobody (system work or a task
    without a valid signed context), so an unverified claim never shapes the
    guidance.
    """

    from custom_agents.access_runtime import resolve_meta_identity

    if not getattr(agent, "is_team_lead", False):
        return ""
    identity = resolve_meta_identity(agent, meta)
    if identity is None or identity.person_id is None:
        return ""
    return describe_person_role(identity.person_roles)


__all__ = ["PERSON_ROLE_CONTEXT_PREFIX", "describe_person_role", "person_role_context"]
