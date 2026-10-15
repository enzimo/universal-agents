"""Keep the access store's copy of the team's people current.

The gateway owns people: their roles, and whether they are active, disabled
or removed (``access_control.people``). The manager's scheduler signs every
scheduled run for the schedule's owner but cannot read the gateway's records,
so the gateway copies each person's standing and effective roles into the
shared access store (``TeamPersonV1``), where the scheduler reads them at
every run (``access_control.people_directory``).

- ``record`` copies one person after any change to them: added, a new
  membership, a role change, disabled, enabled, removed or added back. The
  identity service calls it after its transaction commits
  (``GatewayPeopleDirectory`` port).
- ``sync_all`` copies everyone with a membership in the team (any status) in
  one transaction and marks the team synced. The gateway runs it at every
  start, after the first user is seeded and before it sends the maintenance
  schedules, so it also picks up people added while the gateway was stopped
  (onboarding, ``assistant_cli``).

Standing comes from ``access_control.people.person_standing``: a removed
membership is ``removed``; an active membership of a disabled person is
``disabled``; an active membership of an active person is ``active`` with
effective roles (an owner is also an administrator). A person with no
membership in this team is not recorded.

A failed write is logged (``event=people_directory_write_failed``) and never
undoes or blocks the change in the gateway's records. Until the next write
for that person or the next gateway start, the scheduler sees their old
standing: a disabled or removed person's schedules may keep running, and a
person whose role changed keeps their old roles in scheduled runs. Callers
that tell a person what happened use the returned ``False`` to say so.

``changed_at_utc`` is when the gateway saw the standing or roles change: an
unchanged person keeps their earlier time, so a restart does not make
everyone look changed. This works the same with authorization on and off; it
is about people, not access checks.
"""

from __future__ import annotations

import logging
from collections import Counter
from collections.abc import Callable

from access_control.people import effective_roles, person_standing
from assistant_gateway.application.ports import GatewayClock
from assistant_gateway.config import GatewayInstanceConfig
from assistant_gateway.ports.repositories import IdentityRepository
from custom_messages.contracts.access_v2 import PersonStanding, TeamPersonV1
from storage.access_repository import AccessRepository

# Logged as person_id when the whole team was being written.
_WHOLE_TEAM = "(everyone)"


class PeopleDirectorySync:
    """Copies people's standing from the gateway's records to the access store (module docstring)."""

    def __init__(
        self,
        *,
        identity: IdentityRepository,
        open_store: Callable[[], AccessRepository],
        team_id: str,
        clock: GatewayClock,
        logger: logging.Logger | None = None,
    ) -> None:
        self._identity = identity
        self._open_store = open_store
        self._store: AccessRepository | None = None
        self._team_id = team_id
        self._clock = clock
        self._logger = logger or logging.getLogger(__name__)

    def record(self, canonical_user_id: str) -> bool:
        """Copy one person's current standing; False when the write failed (logged)."""

        try:
            self._record(canonical_user_id)
        except Exception:
            self._write_failed(canonical_user_id)
            return False
        return True

    def sync_all(self) -> Counter[PersonStanding] | None:
        """Copy everyone in the team and mark it synced; the counts by standing, or None when the write failed (logged)."""

        try:
            counts = self._sync_all()
        except Exception:
            self._write_failed(_WHOLE_TEAM)
            return None
        self._logger.info(
            "Copied the team's people to the access store. event=people_directory_synced team_id=%s "
            "active=%d disabled=%d removed=%d",
            self._team_id,
            counts[PersonStanding.ACTIVE],
            counts[PersonStanding.DISABLED],
            counts[PersonStanding.REMOVED],
        )
        return counts

    def _record(self, canonical_user_id: str) -> None:
        person = self._identity.get_gateway_user(canonical_user_id)
        membership = next(
            (
                item
                for item in self._identity.list_team_memberships(canonical_user_id)
                if item.team_id == self._team_id
            ),
            None,
        )
        if person is None or membership is None:
            # Nothing to copy: only people with a membership in this team are recorded.
            return
        standing = person_standing(membership_status=membership.status, person_status=person.status)
        if standing is None:
            return
        store = self._access_store()
        previous = store.team_person(self._team_id, canonical_user_id)
        store.record_person(self._copy(canonical_user_id, standing, membership.role, previous))

    def _sync_all(self) -> Counter[PersonStanding]:
        store = self._access_store()
        previous = {person.person_id: person for person in store.team_people(self._team_id)}
        people: list[TeamPersonV1] = []
        for row in self._identity.list_team_people(self._team_id):
            standing = person_standing(membership_status=row.membership_status, person_status=row.person_status)
            if standing is None:
                continue
            people.append(self._copy(row.canonical_user_id, standing, row.role, previous.get(row.canonical_user_id)))
        store.replace_people(self._team_id, people, at=self._clock.now_utc())
        return Counter(person.standing for person in people)

    def _copy(
        self,
        person_id: str,
        standing: PersonStanding,
        role: str,
        previous: TeamPersonV1 | None,
    ) -> TeamPersonV1:
        """The record to store, keeping the earlier change time when nothing changed."""

        roles = effective_roles((role,)) if standing is PersonStanding.ACTIVE else ()
        changed_at = self._clock.now_utc()
        if previous is not None and previous.standing is standing and tuple(previous.roles) == roles:
            changed_at = previous.changed_at_utc
        return TeamPersonV1(
            team_id=self._team_id,
            person_id=person_id,
            standing=standing,
            roles=roles,
            changed_at_utc=changed_at,
        )

    def _access_store(self) -> AccessRepository:
        # Opened on first use, so a store that cannot be reached at start
        # fails only the write (logged) and the next write tries again.
        if self._store is None:
            self._store = self._open_store()
        return self._store

    def _write_failed(self, person_id: str) -> None:
        self._logger.exception(
            "Could not copy a person's standing to the access store; the gateway's own records "
            "changed anyway, and the scheduler keeps the old copy until the next write or gateway "
            "start. event=people_directory_write_failed team_id=%s person_id=%s",
            self._team_id,
            person_id,
        )


def build_people_directory_sync(
    config: GatewayInstanceConfig,
    *,
    identity: IdentityRepository,
    clock: GatewayClock,
    logger: logging.Logger | None = None,
) -> PeopleDirectorySync:
    """The sync for this gateway's team, writing to the access store the access Inbox uses."""

    storage_settings = config.manager_storage_settings
    return PeopleDirectorySync(
        identity=identity,
        open_store=lambda: AccessRepository(storage_settings),
        team_id=config.gateway.team_id,
        clock=clock,
        logger=logger,
    )


__all__ = ["PeopleDirectorySync", "build_people_directory_sync"]
