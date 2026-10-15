"""People's standard roles in a team, and who may change them.

A person holds one role per team, stored in the gateway's team memberships
and read again at every decision, so a change takes effect at the next
decision (access plan section 4.2):

- ``owner``: whoever set up the instance (one or more people). An owner is
  also an administrator everywhere roles are checked (``effective_roles``);
  what an owner adds is changing who is an owner.
- ``admin``: team administrators. They manage people, agents and the team's
  tools, and approve access to team-owned and platform targets.
- ``member``: everyone else. Members manage what they own.

Who may change a role (section 5.4, "``owner`` for admin role changes"):

- only owners and administrators change roles;
- only an owner makes someone an owner or changes an owner's role; a team
  with no owner yet (an instance set up before owners existed) lets an
  administrator name the first one;
- a change never leaves the team without an owner, nor without an
  administrator (``ROLES_THAT_KEEP_A_HOLDER``; the gateway's store enforces
  this in the same statement that changes the role).

The same rule protects owners from being disabled: only an owner disables or
re-enables an owner.

Whether a person's work may use the team's model keys
(``access_control.person_settings``) is an administrator's decision
(``team_model_keys_refusal``): the team pays for those keys.

Where a person stands in the team (``person_standing``) comes from two
gateway fields: the person's status (active or disabled) and their team
membership's status (active or removed):

- disabling is reversible: the person keeps their membership and cannot use
  the team; their work stops and their schedules are deleted, and their
  files, memory, conversations, grants and linked accounts are kept;
- removing takes them off the team (``removal_refusal``): the membership is
  marked removed and kept for history, their sign-ins end, and their work and
  personal records are deleted, except their folder, personal memory and
  conversations, which are held for ``gateway.users.removed_data_retention_days``
  so an administrator can restore them. Only owners and administrators remove
  people, only an owner removes an owner, nobody removes themselves, and the
  store keeps the last owner and the last administrator
  (``ROLES_THAT_KEEP_A_HOLDER``) in the same statement that removes.

What leaving does to a person's work and records is carried out by the
gateway (``assistant_gateway.person_departure``,
``assistant_gateway.application.person_lifecycle``) and the manager
(``custom_agents.person_departure_runtime``). The runtime's own team schedules
never leave with a person: ``team_work_owner`` names who takes them over.

These are the only role checks outside Cedar. Access to tools is decided by
the access service (``access_control.decisions``), where the owner role sits
inside the admin role; this module decides who administers people.
"""

from __future__ import annotations

from typing import Iterable

from custom_messages.contracts.access_v2 import PersonStanding

# gateway_users.status
PERSON_STATUS_ACTIVE = "active"
PERSON_STATUS_DISABLED = "disabled"
# gateway_user_team_memberships.status; a removed membership keeps its row.
MEMBERSHIP_STATUS_ACTIVE = "active"
MEMBERSHIP_STATUS_REMOVED = "removed"

PERSON_ROLE_OWNER = "owner"
PERSON_ROLE_ADMIN = "admin"
PERSON_ROLE_MEMBER = "member"
PERSON_ROLES = (PERSON_ROLE_OWNER, PERSON_ROLE_ADMIN, PERSON_ROLE_MEMBER)
ADMINISTRATOR_ROLES = frozenset({PERSON_ROLE_OWNER, PERSON_ROLE_ADMIN})
# Each group must keep at least one active holder once it has one: the last
# owner, and the last administrator (owner or admin), cannot be removed.
ROLES_THAT_KEEP_A_HOLDER: tuple[tuple[str, ...], ...] = (
    (PERSON_ROLE_OWNER,),
    (PERSON_ROLE_OWNER, PERSON_ROLE_ADMIN),
)
REMOVAL_NEEDS_ADMINISTRATOR = "Only an owner or an administrator can remove people."
ROLE_WORDS = {
    PERSON_ROLE_OWNER: "an owner",
    PERSON_ROLE_ADMIN: "an administrator",
    PERSON_ROLE_MEMBER: "a member",
}


def normalized_roles(roles: Iterable[str]) -> frozenset[str]:
    return frozenset(str(role).strip().lower() for role in roles if str(role).strip())


def effective_roles(roles: Iterable[str]) -> tuple[str, ...]:
    """The roles with what they imply: an owner is also an administrator."""

    held = set(normalized_roles(roles))
    if PERSON_ROLE_OWNER in held:
        held.add(PERSON_ROLE_ADMIN)
    return tuple(sorted(held))


def is_owner(roles: Iterable[str]) -> bool:
    return PERSON_ROLE_OWNER in normalized_roles(roles)


def is_administrator(roles: Iterable[str]) -> bool:
    """Whether the roles make a person a team administrator (owners are)."""

    return bool(ADMINISTRATOR_ROLES & normalized_roles(roles))


def parse_person_role(text: str) -> str | None:
    role = str(text or "").strip().lower()
    return role if role in PERSON_ROLES else None


def role_change_refusal(
    *,
    actor_roles: Iterable[str],
    current_role: str,
    new_role: str,
    team_has_owner: bool,
) -> str | None:
    """Why the actor may not change a person's role, or None when they may.

    The last-owner and last-administrator protection is enforced where the
    role is stored (``ROLES_THAT_KEEP_A_HOLDER``), so it also holds for two
    changes made at the same moment.
    """

    if parse_person_role(new_role) is None:
        return "Choose owner, admin or member."
    if not is_administrator(actor_roles):
        return "Only an owner or an administrator can change someone's role."
    changes_owner = PERSON_ROLE_OWNER in {str(current_role).strip().lower(), new_role}
    if changes_owner and team_has_owner and not is_owner(actor_roles):
        return "Only an owner can make someone an owner or change an owner's role."
    return None


def status_change_refusal(*, actor_roles: Iterable[str], target_role: str | None) -> str | None:
    """Why the actor may not disable or re-enable this person, or None when they may."""

    if not is_administrator(actor_roles):
        return "Only an owner or an administrator can enable or disable people."
    if str(target_role or "").strip().lower() == PERSON_ROLE_OWNER and not is_owner(actor_roles):
        return "Only an owner can disable or enable an owner."
    return None


def team_model_keys_refusal(*, actor_roles: Iterable[str]) -> str | None:
    """Why the actor may not decide whether someone's work uses the team's model keys, or None when they may."""

    if not is_administrator(actor_roles):
        return "Only an owner or an administrator can decide whether someone uses the team's model keys."
    return None


def account_sharing_refusal(*, actor_roles: Iterable[str]) -> str | None:
    """Why the actor may not decide whether the team's account for a service serves everyone, or None when they may.

    The team's accounts are linked by administrators, so administrators
    decide whose work uses them (``access_control.account_sharing``).
    """

    if not is_administrator(actor_roles):
        return "Only an owner or an administrator can decide whether the team's accounts are shared."
    return None


def removal_refusal(
    *,
    actor_id: str,
    actor_roles: Iterable[str],
    target_id: str,
    target_role: str | None,
) -> str | None:
    """Why the actor may not remove this person from the team, or None when they may.

    The last-owner and last-administrator protection is enforced where the
    membership is stored, as for role changes.
    """

    if not is_administrator(actor_roles):
        return REMOVAL_NEEDS_ADMINISTRATOR
    if str(actor_id).strip() == str(target_id).strip():
        return "You cannot remove yourself from the team. Ask another owner or administrator."
    if str(target_role or "").strip().lower() == PERSON_ROLE_OWNER and not is_owner(actor_roles):
        return "Only an owner can remove an owner."
    return None


def team_work_owner(
    people: Iterable[tuple[str, bool, Iterable[str]]], *, preferred: str | None
) -> str | None:
    """Who owns the runtime's own team schedules, or None when nobody can.

    ``people`` are ``(person_id, active, roles)``. The runtime's team-wide
    schedules (maintenance, memory audits, the heartbeat launcher) run for one
    person so their findings reach someone. ``preferred`` keeps them while
    that person is active: the gateway passes the instance's first user, the
    manager the schedule's current owner. When the preferred person is
    disabled or removed they move to an active owner, else an active
    administrator, the lowest person id first, so the gateway and the manager
    choose the same person from the same records.
    """

    ranked: list[tuple[int, str]] = []
    for person_id, active, roles in people:
        if not active:
            continue
        if person_id == preferred:
            return person_id
        held = effective_roles(roles)
        if is_owner(held):
            ranked.append((0, person_id))
        elif is_administrator(held):
            ranked.append((1, person_id))
    return min(ranked)[1] if ranked else None


def person_standing(*, membership_status: str | None, person_status: str | None) -> PersonStanding | None:
    """Where a person stands in a team, from the gateway's two status fields.

    None when the person has no membership in the team. A removed membership
    wins over a disabled person. A membership status other than active or
    removed never lets the person act, so it counts as disabled.
    """

    if membership_status is None:
        return None
    membership = str(membership_status).strip().lower()
    if membership == MEMBERSHIP_STATUS_REMOVED:
        return PersonStanding.REMOVED
    if membership == MEMBERSHIP_STATUS_ACTIVE and str(person_status or "").strip().lower() == PERSON_STATUS_ACTIVE:
        return PersonStanding.ACTIVE
    return PersonStanding.DISABLED


__all__ = [
    "ADMINISTRATOR_ROLES",
    "MEMBERSHIP_STATUS_ACTIVE",
    "MEMBERSHIP_STATUS_REMOVED",
    "PERSON_ROLES",
    "PERSON_STATUS_ACTIVE",
    "PERSON_STATUS_DISABLED",
    "PERSON_ROLE_ADMIN",
    "PERSON_ROLE_MEMBER",
    "PERSON_ROLE_OWNER",
    "REMOVAL_NEEDS_ADMINISTRATOR",
    "ROLES_THAT_KEEP_A_HOLDER",
    "ROLE_WORDS",
    "PersonStanding",
    "account_sharing_refusal",
    "effective_roles",
    "is_administrator",
    "is_owner",
    "normalized_roles",
    "parse_person_role",
    "person_standing",
    "removal_refusal",
    "role_change_refusal",
    "status_change_refusal",
    "team_model_keys_refusal",
    "team_work_owner",
]
