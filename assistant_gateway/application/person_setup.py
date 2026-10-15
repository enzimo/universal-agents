"""What an administrator chooses when adding a person, and the settings report they get back.

When an administrator adds someone (``/user <id> "Name"``, ``/user invite
--new "Name"``), the reply ends with a plain report of how the person is set
up, so nothing about them is a surprise later (owner's request 2026-10-02):
their role, how they were added, their timezone, their own folder, the shared
folders, whether their work may use the team's model keys, and whether they
have their own model.

Choices when adding (``PersonSetupChoices``):

- ``--team-keys`` / ``--own-keys``: whether their work may use the team's
  model keys (``access_control.person_settings``). Without either, the
  team's model-key sharing applies (``access_control.account_sharing``:
  shared unless an administrator ran ``/accounts private model``).
  ``/user team-keys <id> on|off`` changes it later.
- ``--shared <name>``: a shared working folder ``team/<name>`` for their
  work. Everyone has ``team/team-shared`` already. Folders under ``team/``
  are open to the whole team: every person's agents, administrators' agents
  and the team's own agents read and write them (``access_control.folders``,
  ``STANDARD_SHARED_FOLDERS``), so naming one needs no grant.

The gateway's adapter (``assistant_gateway.person_setup``) stores the choices
and creates folders; this module only carries them and writes the report.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Protocol, runtime_checkable

from secure_tools.chat_commands import OWN_MODEL_COMMAND, SETUP_COMMAND

TEAM_KEYS_COMMAND = "/user team-keys"
# One folder name below team/ (the same rule as data folder names).
SHARED_FOLDER_NAME = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}$")
TEAM_KEYS_OPTION = "--team-keys"
OWN_KEYS_OPTION = "--own-keys"
SHARED_OPTION = "--shared"
SETUP_OPTIONS_USAGE = f'[{TEAM_KEYS_OPTION}|{OWN_KEYS_OPTION}] [{SHARED_OPTION} folder-name]'


@dataclass(frozen=True)
class PersonSetupChoices:
    """The administrator's choices when adding a person (module docstring); None means "the default"."""

    team_model_keys: bool | None = None
    shared_folder: str | None = None


class SetupOptionProblem(ValueError):
    """An add command's options cannot be used; the message says what to type."""


def take_setup_options(parts: list[str]) -> tuple[list[str], PersonSetupChoices]:
    """Remove ``--team-keys``, ``--own-keys`` and ``--shared <name>`` from command words; raises ``SetupOptionProblem``."""

    remaining: list[str] = []
    team_keys: bool | None = None
    shared: str | None = None
    words = iter(parts)
    for word in words:
        lowered = word.lower()
        if lowered in (TEAM_KEYS_OPTION, OWN_KEYS_OPTION):
            choice = lowered == TEAM_KEYS_OPTION
            if team_keys is not None and team_keys != choice:
                raise SetupOptionProblem(f"Choose {TEAM_KEYS_OPTION} or {OWN_KEYS_OPTION}, not both.")
            team_keys = choice
        elif lowered == SHARED_OPTION:
            shared = shared_folder_name(next(words, ""))
        else:
            remaining.append(word)
    return remaining, PersonSetupChoices(team_model_keys=team_keys, shared_folder=shared)


def shared_folder_name(text: str) -> str:
    """A valid shared folder name (``team/<name>``), or ``SetupOptionProblem``."""

    name = str(text or "").strip().lower().removeprefix("team/").strip("/")
    if not SHARED_FOLDER_NAME.match(name):
        raise SetupOptionProblem(
            f"{SHARED_OPTION} needs a folder name of lowercase letters, digits, - and _ "
            "(up to 64 characters), such as research-notes."
        )
    return name


@dataclass(frozen=True)
class PersonSettingsView:
    """How one person is set up, for the onboarding report and ``/user status``."""

    person_id: str
    personal_folder: str
    shared_folders: tuple[str, ...]
    may_use_team_keys: bool
    team_keys_decided: bool  # False: added before the switch existed, so the team's keys as before
    own_model: str | None  # "model at host", or None
    # What they still set up for themselves (assistant_gateway.personal_setup.setup_summary); empty when nothing.
    still_to_set_up: str = ""
    # True only when adding them just recorded a first-time setup to show on their first message.
    setup_due: bool = False
    problems: tuple[str, ...] = ()


@runtime_checkable
class GatewayPersonSetup(Protocol):
    """The gateway's side of setting a person up (``assistant_gateway.person_setup``)."""

    def set_up(self, person_id: str, choices: PersonSetupChoices, *, actor_id: str, new_person: bool) -> PersonSettingsView:
        """Store the choices (the team default for a new person without one), create folders, and describe the result."""

    def set_team_keys(self, person_id: str, allowed: bool, *, actor_id: str) -> PersonSettingsView:
        """Record the administrator's decision (already checked) and describe the result."""

    def describe(self, person_id: str) -> PersonSettingsView:
        """How the person is set up now."""


def settings_report(
    view: PersonSettingsView,
    *,
    display_name: str,
    role: str,
    added_through: str,
    timezone: str,
) -> list[str]:
    """The onboarding report's lines (module docstring)."""

    lines = [
        f"Settings for {display_name} ({view.person_id}):",
        f"- Role: {role}",
    ]
    if added_through:
        lines.append(f"- Added through: {added_through}")
    if timezone:
        lines.append(f"- Timezone: {timezone}")
    lines += settings_lines(view)
    if view.setup_due and view.still_to_set_up:
        # Only a newly added person has a first-time setup due (assistant_gateway.personal_setup).
        lines.append("- First message: they are shown what is still to set up for themselves.")
    return lines


def settings_lines(view: PersonSettingsView) -> list[str]:
    """The model and folder lines, shared by the onboarding report and ``/user status``."""

    switch = f"{TEAM_KEYS_COMMAND} {view.person_id} {'off' if view.may_use_team_keys else 'on'}"
    if not view.team_keys_decided:
        team_keys = f"yes (added before this setting existed; change with {switch})"
    else:
        team_keys = f"{'yes' if view.may_use_team_keys else 'no'} (change with {switch})"
    if view.own_model:
        own_model = f"{view.own_model}; agents use it for all their work"
    elif view.may_use_team_keys:
        own_model = f"not set up (optional; they can add one with {OWN_MODEL_COMMAND})"
    else:
        own_model = (
            f"not set up yet. Their requests do not run until they set it up with {OWN_MODEL_COMMAND}"
        )
    lines = [
        f"- May use the team's model keys: {team_keys}",
        f"- Own model: {own_model}",
        f"- Personal folder: {view.personal_folder}",
        "- Shared folders: " + ", ".join(view.shared_folders)
        + " (open to the whole team: their agents, your agents and the team's agents read and write them)",
    ]
    if view.still_to_set_up:
        lines.append(f"- Still to set up: {view.still_to_set_up} ({SETUP_COMMAND} lists them)")
    lines += [f"- Not done: {problem}" for problem in view.problems]
    return lines


__all__ = [
    "GatewayPersonSetup",
    "PersonSettingsView",
    "PersonSetupChoices",
    "SETUP_OPTIONS_USAGE",
    "SetupOptionProblem",
    "TEAM_KEYS_COMMAND",
    "settings_lines",
    "settings_report",
    "shared_folder_name",
    "take_setup_options",
]
