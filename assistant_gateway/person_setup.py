"""The gateway's side of setting a person up: their model key switch, shared folders, and how they stand.

Called when an administrator adds someone (``GatewayPersonLifecycle.after_added``)
or changes the switch (``/user team-keys``); the choices and the report are
``assistant_gateway.application.person_setup``.

- The switch and the person's shared folders are stored in the access store
  (``access_control.person_settings``), which the broker and the agents read.
  A new person always gets a record: their choice, or the team's model-key
  sharing (``access_control.account_sharing``, shared unless an
  administrator made it private with ``/accounts private model``). Someone
  added back keeps their earlier record unless the administrator chose again.
- Folders are created below ``workspaces/team/`` one component at a time and
  never through a link (``access_control.safe_open``): ``team/`` is
  agent-writable, so a planted link must not send the gateway elsewhere.

- A new person also gets a first-time setup (``assistant_gateway.personal_setup``):
  what they set up for themselves (their own model when they may not use the
  team's keys, and their own accounts for the services the team keeps
  private), shown on their first message. The report lists it.

A failure (the access store or a folder) is logged with the person's id and
reported in the reply as "Not done"; adding the person still counts.
"""

from __future__ import annotations

import logging
import os
from dataclasses import replace
from typing import Callable

from access_control import safe_open
from access_control.account_sharing import TeamAccountSharing
from access_control.person_settings import PersonSettings
from assistant_gateway.application.person_setup import PersonSettingsView, PersonSetupChoices
from assistant_gateway.personal_setup import GatewayPersonalSetup, setup_summary
from custom_messages.contracts.person_settings_v1 import PersonSettingsV1
from model_providers.endpoints import display_host
from runtime_settings.instance_layout import (
    TEAM_DIRNAME,
    TEAM_SHARED_DIRNAME,
    USERS_DIRNAME,
    InstanceLayout,
    person_folder_name,
)
from secure_tools.chat_commands import SETUP_COMMAND
from storage.access_repository import AccessRepository

logger = logging.getLogger(__name__)

EVERYONES_FOLDER = f"{TEAM_DIRNAME}/{TEAM_SHARED_DIRNAME}"


class GatewayPersonSetupStore:
    """Implements ``GatewayPersonSetup`` (module docstring)."""

    def __init__(
        self,
        *,
        access: Callable[[], AccessRepository],
        layout: InstanceLayout,
        team_id: str,
        personal_setup: GatewayPersonalSetup,
    ) -> None:
        self._access = access
        self._layout = layout
        self._team_id = team_id
        self._personal_setup = personal_setup

    def _settings(self) -> PersonSettings:
        return PersonSettings(self._access(), self._team_id)

    def set_up(self, person_id: str, choices: PersonSetupChoices, *, actor_id: str, new_person: bool) -> PersonSettingsView:
        problems: list[str] = []
        if new_person or choices.team_model_keys is not None:
            try:
                allowed = (
                    TeamAccountSharing(self._access(), self._team_id).new_people_use_team_model_keys()
                    if choices.team_model_keys is None
                    else choices.team_model_keys
                )
                self._settings().set_team_keys(person_id, allowed, by=actor_id)
            except Exception:  # noqa: BLE001 - reported in the reply
                logger.exception(
                    "Could not save whether a new person may use the team's model keys. "
                    "event=person_team_keys_save_failed person_id=%s",
                    person_id,
                )
                problems.append(
                    "whether they may use the team's model keys could not be saved; set it with /user team-keys."
                )
        folders = [TEAM_SHARED_DIRNAME] + ([choices.shared_folder] if choices.shared_folder else [])
        made = [name for name in folders if self._make_team_folder(name, person_id, problems)]
        if choices.shared_folder and choices.shared_folder in made:
            try:
                self._settings().add_shared_folders(person_id, (f"{TEAM_DIRNAME}/{choices.shared_folder}",), by=actor_id)
            except Exception:  # noqa: BLE001 - reported in the reply
                logger.exception(
                    "Could not record a person's shared folder. event=person_shared_folder_save_failed person_id=%s",
                    person_id,
                )
                problems.append(f"team/{choices.shared_folder} was created but not recorded as theirs.")
        setup_due = new_person and self._mark_setup_due(person_id, problems)
        logger.info(
            "Set up a person who was added. event=person_set_up person_id=%s actor=%s new_person=%s "
            "team_keys_choice=%s shared_folder=%s problems=%d",
            person_id,
            actor_id,
            new_person,
            choices.team_model_keys,
            choices.shared_folder,
            len(problems),
        )
        return replace(self.describe(person_id, problems=tuple(problems)), setup_due=setup_due)

    def _mark_setup_due(self, person_id: str, problems: list[str]) -> bool:
        """A new person sees their first-time setup on their first message (``assistant_gateway.personal_setup``); False when it could not be recorded."""

        try:
            self._personal_setup.mark_due(person_id)
        except Exception:  # noqa: BLE001 - reported in the reply
            logger.exception(
                "Could not record a new person's first-time setup. event=personal_setup_due_failed person_id=%s",
                person_id,
            )
            problems.append(f"their first-time setup could not be recorded; they can still use {SETUP_COMMAND}.")
            return False
        return True

    def set_team_keys(self, person_id: str, allowed: bool, *, actor_id: str) -> PersonSettingsView:
        self._settings().set_team_keys(person_id, allowed, by=actor_id)
        logger.info(
            "An administrator decided whether a person may use the team's model keys. "
            "event=person_team_keys_changed person_id=%s actor=%s allowed=%s",
            person_id,
            actor_id,
            allowed,
        )
        return self.describe(person_id)

    def describe(self, person_id: str, *, problems: tuple[str, ...] = ()) -> PersonSettingsView:
        record: PersonSettingsV1 | None = None
        try:
            record = self._settings().settings(person_id)
        except Exception:  # noqa: BLE001 - reported in the view
            logger.exception(
                "Could not read a person's settings. event=person_settings_read_failed person_id=%s", person_id
            )
            problems = (*problems, "their model settings could not be read from the access store.")
        own = record.own_model if record is not None else None
        still_to_set_up = ""
        try:
            still_to_set_up = setup_summary(self._personal_setup.steps.steps(person_id))
        except Exception:  # noqa: BLE001 - reported in the view
            logger.exception(
                "Could not read what a person still has to set up. event=personal_setup_read_failed person_id=%s",
                person_id,
            )
            problems = (*problems, "what they still have to set up could not be read.")
        return PersonSettingsView(
            person_id=person_id,
            personal_folder=f"{USERS_DIRNAME}/{person_folder_name(person_id)}",
            shared_folders=(EVERYONES_FOLDER, *(record.shared_folders if record is not None else ())),
            may_use_team_keys=record is None or record.may_use_team_keys,
            team_keys_decided=record is not None and record.uses_team_keys is not None,
            own_model=f"{own.model_id} at {display_host(own.address)}" if own is not None else None,
            still_to_set_up=still_to_set_up,
            problems=problems,
        )

    def _make_team_folder(self, name: str, person_id: str, problems: list[str]) -> bool:
        try:
            descriptor = safe_open.open_directory(self._layout.workspaces_dir, (TEAM_DIRNAME, name), create=True)
        except safe_open.UnsafePath as exc:
            logger.warning(
                "Could not create a shared folder. event=team_folder_create_failed person_id=%s folder=team/%s error=%s",
                person_id,
                name,
                exc,
            )
            problems.append(f"team/{name} could not be created ({exc}).")
            return False
        os.close(descriptor)
        return True


__all__ = ["EVERYONES_FOLDER", "GatewayPersonSetupStore"]
