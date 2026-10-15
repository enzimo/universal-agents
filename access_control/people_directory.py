"""Who a person is in a team now, for trusted code outside the gateway.

The gateway owns people: their roles and whether they are active, disabled or
removed (``access_control.people``). It keeps a copy of each person's standing
and effective roles in the access store (``TeamPersonV1``), written whenever a
person is added, changes role, is disabled, enabled or removed, and in full at
each gateway start (``assistant_gateway.people_directory_sync``).

Why a copy: the manager's scheduler signs every scheduled run for the
schedule's owner, and it cannot read the gateway's records. Before this copy
existed, a scheduled run took the owner's roles from the gateway-signed
context saved on the schedule when it was made, so a demoted administrator's
schedules kept administrator roles, a disabled person's schedules kept running,
and a schedule the manager created itself had no roles until the gateway sent
a refresh. Now the scheduler reads the owner's current standing and roles at
every run (``custom_agents.task_context_runtime``), and the manager reads who
has left to end their work (``custom_agents.person_departure_runtime``).

Trust: the copy adds no authority. The manager already holds the key that
signs scheduled work for any person, and agents' code runs in the sandbox,
which cannot reach the access store.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from custom_messages.contracts.access_v2 import PersonStanding, TeamPersonV1


class PeopleStore(Protocol):
    def team_person(self, team_id: str, person_id: str) -> TeamPersonV1 | None: ...

    def team_people(self, team_id: str) -> list[TeamPersonV1]: ...

    def people_synced_at(self, team_id: str) -> object | None: ...


@dataclass(frozen=True)
class PersonNow:
    """One person's standing as the gateway last recorded it.

    ``standing`` is None when the gateway does not know the person in this
    team; ``synced`` is False until the gateway has recorded everyone once, and
    then an unknown person tells nothing (they may be new).
    """

    person_id: str
    standing: PersonStanding | None
    roles: tuple[str, ...] = ()
    synced: bool = True

    @property
    def active(self) -> bool:
        return self.standing is PersonStanding.ACTIVE

    def describe(self) -> str:
        if self.standing is not None:
            return self.standing.value
        return "not known to the gateway" if self.synced else "not recorded by the gateway yet"


class PeopleDirectory:
    """Reads the gateway's copy of the team's people (module docstring)."""

    def __init__(self, store: PeopleStore, team_id: str) -> None:
        self.store = store
        self.team_id = team_id

    def synced(self) -> bool:
        return self.store.people_synced_at(self.team_id) is not None

    def person(self, person_id: str) -> PersonNow:
        record = self.store.team_person(self.team_id, person_id)
        if record is None:
            return PersonNow(person_id, None, synced=self.synced())
        return PersonNow(person_id, record.standing, tuple(record.roles))

    def people(self) -> dict[str, PersonNow]:
        """Everyone the gateway recorded in the team, by person id."""

        return {
            record.person_id: PersonNow(record.person_id, record.standing, tuple(record.roles))
            for record in self.store.team_people(self.team_id)
        }


__all__ = ["PeopleDirectory", "PeopleStore", "PersonNow"]
