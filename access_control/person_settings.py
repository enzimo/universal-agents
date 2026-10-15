"""Read and change each person's shared settings (``PersonSettingsV1``).

Who uses what (owner's decisions 2026-10-02, details in
``custom_messages.contracts.person_settings_v1``):

- ``may_use_team_keys``: an administrator's switch per person. Work for a
  person without it may still use their own key or own model, never the
  team's (the broker refuses the team's key, ``secure_tool_broker.model_keys``),
  and they are told to set up their own model. People with no record were
  added before the switch existed and keep using the team's keys.
- ``own_model``: the model a person saved on ``/auth model me``; every
  agent's work for them uses it (``custom_agents.person_model_runtime``).
- ``shared_folders``: shared working folders an administrator named when
  adding them (``assistant_gateway.person_setup``).

The gateway decides who may change the switch (``access_control.people.
team_model_keys_refusal``) before calling ``set_team_keys``; this module only
reads and writes. Writes go through one transaction per change
(``AccessRepository.change_person_settings``), so the gateway's switch
and the broker's own model never overwrite each other.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Callable, Protocol

from custom_messages.contracts.person_settings_v1 import PersonModelProfileV1, PersonSettingsV1


class PersonSettingsStore(Protocol):
    def person_settings(self, team_id: str, person_id: str) -> PersonSettingsV1 | None: ...

    def team_person_settings(self, team_id: str) -> list[PersonSettingsV1]: ...

    def change_person_settings(
        self,
        team_id: str,
        person_id: str,
        change: Callable[[PersonSettingsV1 | None], PersonSettingsV1],
    ) -> PersonSettingsV1: ...


class PersonSettings:
    """One team's people's model settings (module docstring)."""

    def __init__(
        self,
        store: PersonSettingsStore,
        team_id: str,
        *,
        now: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
    ) -> None:
        self.store = store
        self.team_id = team_id
        self._now = now

    def settings(self, person_id: str) -> PersonSettingsV1 | None:
        return self.store.person_settings(self.team_id, person_id)

    def everyone(self) -> list[PersonSettingsV1]:
        return self.store.team_person_settings(self.team_id)

    def may_use_team_keys(self, person_id: str) -> bool:
        settings = self.settings(person_id)
        return settings is None or settings.may_use_team_keys

    def own_model(self, person_id: str) -> PersonModelProfileV1 | None:
        settings = self.settings(person_id)
        return settings.own_model if settings is not None else None

    def set_team_keys(self, person_id: str, allowed: bool, *, by: str | None) -> PersonSettingsV1:
        """Record the administrator's decision (checked by the caller)."""

        return self._change(person_id, by, uses_team_keys=allowed)

    def set_own_model(self, person_id: str, profile: PersonModelProfileV1 | None, *, by: str | None) -> PersonSettingsV1:
        """Save (or with None, remove) the person's own model; the caller tested it first."""

        return self._change(person_id, by, own_model=profile)

    def add_shared_folders(self, person_id: str, folders: tuple[str, ...], *, by: str | None) -> PersonSettingsV1:
        """Record shared folders for the person's work, keeping the ones already recorded."""

        at = self._now()

        def change(current: PersonSettingsV1 | None) -> PersonSettingsV1:
            base = current or PersonSettingsV1(team_id=self.team_id, person_id=person_id, changed_at_utc=at)
            return PersonSettingsV1.model_validate(
                {
                    **base.model_dump(),
                    "shared_folders": (*base.shared_folders, *folders),
                    "changed_at_utc": at,
                    "changed_by_person_id": by,
                }
            )

        return self.store.change_person_settings(self.team_id, person_id, change)

    def _change(self, person_id: str, by: str | None, **fields: object) -> PersonSettingsV1:
        at = self._now()

        def change(current: PersonSettingsV1 | None) -> PersonSettingsV1:
            base = current or PersonSettingsV1(team_id=self.team_id, person_id=person_id, changed_at_utc=at)
            return base.model_copy(update={**fields, "changed_at_utc": at, "changed_by_person_id": by})

        return self.store.change_person_settings(self.team_id, person_id, change)


__all__ = ["PersonSettingsStore", "PersonSettings"]
