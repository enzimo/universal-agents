"""The gateway's part in the access model: people's roles and starter profiles.

People's team roles come from the gateway's membership records. When the
gateway starts with authorization on, it applies the configured starter
profile (``authorization.starter_profile``) as the instance's user, so the team
lead's first grants have that person at the root of their chain
(``access_control.starter_profiles``).
"""

from __future__ import annotations

import logging
from typing import Any

from access_control.decisions import HandDownRefused
from access_control.entities import PersonPrincipal
from access_control.operator_commands import open_access_service
from access_control.starter_profiles import (
    CHOSEN_IN_CONFIGURATION,
    applied_profile,
    apply_starter_profile,
    chosen_by_person,
)

logger = logging.getLogger(__name__)


class MembershipRoles:
    """Current team roles of a person, from the gateway identity repository."""

    def __init__(self, identity_repository: Any) -> None:
        self._repository = identity_repository

    @property
    def user_events(self) -> Any:
        """The identity repository's per-person event log (``gateway_user_events``)."""

        return self._repository

    @classmethod
    def for_config(cls, config: Any) -> "MembershipRoles":
        from assistant_gateway.store import GatewayStateStore

        settings = config.state_storage_settings
        return cls(GatewayStateStore(settings.sqlite_path or "", storage_settings=settings).identity_repository)

    def roles_of(self, person_id: str, team_id: str) -> tuple[str, ...] | None:
        user = self._repository.get_gateway_user(person_id)
        if user is None or user.status != "active":
            return None
        roles = sorted(
            {
                membership.role
                for membership in self._repository.list_team_memberships(person_id, status="active")
                if membership.team_id == team_id
            }
        )
        return tuple(roles) or None

    def display_name(self, person_id: str) -> str:
        """The person's name as the gateway knows it; the id when there is none."""

        user = self._repository.get_gateway_user(person_id)
        return str(getattr(user, "display_name", "") or "").strip() or person_id

    def team_people(self, team_id: str) -> list[tuple[str, str]]:
        """Active members of the team as (person id, name), for share and send pickers."""

        people: list[tuple[str, str]] = []
        for membership in self._repository.list_team_members(team_id):
            if getattr(membership, "status", "active") != "active":
                continue
            person_id = str(membership.canonical_user_id)
            if self.roles_of(person_id, team_id):
                people.append((person_id, self.display_name(person_id)))
        return sorted(people, key=lambda item: (item[1].lower(), item[0]))


class PersonChangeCheck:
    """Whether a signed-in person may make a platform change in the gateway's own controls.

    Used by controls that change configuration directly for a person (the MCP
    controls): with authorization on, the access service decides (the same
    people who could approve that kind of change in the Inbox); with it off,
    any active team member may (trusted single-person mode, decision D8).
    Roles come from current memberships, never from the browser.
    """

    def __init__(self, config: Any, memberships: MembershipRoles) -> None:
        self._config = config
        self._memberships = memberships
        self._service: Any = None

    def may_change(self, person_id: str | None, kind: str) -> bool:
        from access_control.inbox import StoredAccessInbox
        from access_control.requests import AccessRequests

        team_id = self._config.manager.team_id
        roles = self._memberships.roles_of(str(person_id or ""), team_id) if person_id else None
        if roles is None:
            return False
        if not self._config.authorization.enabled:
            return True
        if self._service is None:
            self._service = open_access_service(
                instance_root=self._config.instance_root_path,
                team_id=team_id,
                storage_settings=self._config.manager_storage_settings,
                cedar_binary=self._config.authorization.cedar_binary_path,
                lead_agent_type=self._config.manager.named_agent_type,
            )
        requests = AccessRequests(
            service=self._service,
            inbox=StoredAccessInbox(self._service.store),
            lead_agent_type=self._config.manager.named_agent_type,
        )
        return requests.may_change_directly(PersonPrincipal(str(person_id), team_id, roles), kind)

    def person(self, person_id: str) -> PersonPrincipal | None:
        """The active team member with their current roles, or None."""

        team_id = self._config.manager.team_id
        roles = self._memberships.roles_of(str(person_id or ""), team_id) if person_id else None
        return PersonPrincipal(str(person_id), team_id, roles) if roles else None

    def require(self, person_id: str | None, kind: str) -> str:
        """The person id, or PermissionError when they may not make this change."""

        if not self.may_change(person_id, kind):
            raise PermissionError("Only an administrator can make this change.")
        return str(person_id)

    def close(self) -> None:
        if self._service is not None:
            self._service.cedar.close()
            self._service = None


def apply_configured_starter_profile(config: Any, memberships: MembershipRoles) -> None:
    """Apply ``authorization.starter_profile`` for the instance's user until they choose their own.

    The setting is the first user's default (``access_control.starter_profiles``):
    once that person chooses a profile with ``/access profile`` or on the
    Access page, their choice stays across restarts. No-op with authorization off.
    """

    if not config.authorization.enabled:
        return
    team_id = config.manager.team_id
    person_id = config.user.canonical_user_id
    roles = memberships.roles_of(person_id, team_id)
    if roles is None:
        logger.error(
            "The starter profile was not applied: the instance's user is not an active team member. "
            "event=starter_profile_skipped person_id=%s team_id=%s",
            person_id,
            team_id,
        )
        return
    service = open_access_service(
        instance_root=config.instance_root_path,
        team_id=team_id,
        storage_settings=config.manager_storage_settings,
        cedar_binary=config.authorization.cedar_binary_path,
        lead_agent_type=config.manager.named_agent_type,
    )
    configured = config.authorization.starter_profile
    try:
        if chosen_by_person(service, person_id):
            chosen = applied_profile(service, person_id)
            if chosen != configured:
                logger.info(
                    "The first user chose their own starter profile, so authorization.starter_profile is not "
                    "applied. event=starter_profile_kept person_id=%s chosen=%s configured=%s",
                    person_id,
                    chosen,
                    configured,
                )
            return
        apply_starter_profile(
            service,
            PersonPrincipal(person_id, team_id, roles),
            configured,
            lead_agent_type=config.manager.named_agent_type,
            source=CHOSEN_IN_CONFIGURATION,
        )
    except (HandDownRefused, ValueError) as exc:
        logger.error(
            "The starter profile could not be applied. event=starter_profile_failed profile=%s error=%s",
            config.authorization.starter_profile,
            exc,
        )
    finally:
        service.cedar.close()


__all__ = ["MembershipRoles", "apply_configured_starter_profile"]
