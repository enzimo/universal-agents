"""Each person's settings that trusted processes share: team model keys, own model, shared folders.

Owner's decisions (2026-10-02):

- An administrator decides, per person, whether work for them may use the
  team's model keys (``uses_team_keys``). People added from now on start
  with the team's model-key sharing (``access_control.account_sharing``,
  shared unless an administrator made it private with ``/accounts private
  model``), or the ``--team-keys``/``--own-keys`` choice made when adding them. People added before this existed have no record and keep
  using the team's keys (``None`` counts as allowed), so nothing changes for
  them without a decision.
- A person may save their own model (``own_model``) on ``/auth model me``:
  any provider and address, tested with one real request before it is
  saved. Every agent's work for them then uses it, including agent types an
  administrator pinned to a model. Work for nobody keeps the configured
  models; embeddings and voice keep the team's.
- An administrator may name shared working folders for a person when adding
  them (``shared_folders``, folder ids such as ``team/research``). Folders
  under ``team/`` are open to the whole team anyway; the record says which
  ones this person's work is meant to share, for ``/user status``.
  ``team/team-shared`` is everyone's and is not stored per person.

The record lives in the shared access store (``storage.access_repository``)
because the gateway (the switch), the broker (the own model, and refusing
team keys) and the agent processes (using the own model) all open it. The
key itself never appears here: it is in the person's own credential scope in
the broker (``secure_tool_broker.model_keys``).
"""

from __future__ import annotations

import re
from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from model_providers.endpoints import EndpointKind, checked_endpoint_url, endpoint_address

# The smallest context window a person may give their model (as onboarding requires).
MIN_CONTEXT_WINDOW_TOKENS = 1024
# One folder directly under team/ (the same name rule as data folders).
TEAM_FOLDER_ID = re.compile(r"^team/[a-z0-9][a-z0-9_-]{0,63}$")


class PersonSettingsContractModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class PersonModelProfileV1(PersonSettingsContractModel):
    """A person's own model, as they saved and tested it.

    ``base_url`` is the address that answered the test (with or without
    ``/v1``, ``model_providers.connection_test.check_endpoint_address``).
    ``context_window_tokens`` is None when the person did not say; agents
    then use the window of the model they would otherwise use.
    """

    kind: EndpointKind
    base_url: str = Field(min_length=1, max_length=520)
    model_id: str = Field(min_length=1, max_length=300)
    api_mode: Literal["chat_completions", "responses"] = "chat_completions"
    supports_images: bool = False
    supports_files: bool = False
    context_window_tokens: int | None = Field(default=None, ge=MIN_CONTEXT_WINDOW_TOKENS)
    tested_at_utc: datetime

    @field_validator("base_url")
    @classmethod
    def _checked_url(cls, value: str) -> str:
        return checked_endpoint_url(value)

    @property
    def address(self) -> str:
        return endpoint_address(self.base_url)


class PersonSettingsV1(PersonSettingsContractModel):
    """One person's record (module docstring); ``uses_team_keys`` None means "never decided": allowed."""

    schema_version: Literal[1] = 1
    team_id: str = Field(min_length=1, max_length=120)
    person_id: str = Field(min_length=1, max_length=240)
    uses_team_keys: bool | None = None
    own_model: PersonModelProfileV1 | None = None
    shared_folders: tuple[str, ...] = Field(default=(), max_length=20)
    changed_at_utc: datetime
    changed_by_person_id: str | None = Field(default=None, max_length=240)

    @field_validator("shared_folders")
    @classmethod
    def _team_folders(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        for folder in value:
            if not TEAM_FOLDER_ID.match(folder):
                raise ValueError(f"A shared folder must be one folder under team/, such as team/research: {folder!r}.")
        return tuple(dict.fromkeys(value))

    @property
    def may_use_team_keys(self) -> bool:
        return self.uses_team_keys is not False


__all__ = [
    "MIN_CONTEXT_WINDOW_TOKENS",
    "TEAM_FOLDER_ID",
    "PersonModelProfileV1",
    "PersonSettingsV1",
]
