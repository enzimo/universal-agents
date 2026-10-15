"""How a tool is set up beyond sign-ins and account links (owner's decision 2026-09-28).

A catalog tool may carry optional ``setup`` data that ``set_up_tools``
(``custom_agents.tool_setup_runs``) reads generically, so no tool needs its own
setup code:

- ``external_steps``: plain steps the person (or an administrator) does
  outside Universal Agents, such as creating an API token on the service's
  website, before the account can be linked. Each becomes a checklist item on
  the access workflow page that the person marks done; its words are shown
  exactly as an administrator approved them.
- ``prerequisites``: other catalog tools that must be set up first (their
  pieces come first in the plan).

The data is people-facing text an agent may propose (``propose_access_change``),
so it is checked strictly here: bounded lengths, links only to ``https``
addresses without sign-in details or key-like query values, and no text that
looks like a secret (tokens, keys, passwords). It grants nothing and never
carries a credential.
"""

from __future__ import annotations

import re
from typing import Literal
from urllib.parse import parse_qsl, urlsplit

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

MAX_EXTERNAL_STEPS = 6
MAX_PREREQUISITES = 4
STEP_ID_PATTERN = re.compile(r"^[a-z][a-z0-9_]{0,39}$")
# Same shape as access_v2.TOOL_ID_PATTERN (kept here so this module stands alone).
PREREQUISITE_PATTERN = re.compile(r"^(fn|cli|http|mcp|procedure|code):[a-z0-9][a-z0-9_.-]{0,95}$")
EXTERNAL_STEP_BY_PERSON = "person"
EXTERNAL_STEP_BY_ADMINISTRATOR = "administrator"
ExternalStepWho = Literal["person", "administrator"]

# Text that looks like a credential: known token prefixes, JSON web tokens,
# and "password=..." assignments; plus long runs of key-like characters that
# mix letters and several digits (words and link paths do not).
_SECRET_TEXT = re.compile(
    r"(?i)\b(?:ghp|gho|ghu|ghs|ghr|github_pat|glpat|xox[baprs]|sk|pk|rk|hf|AKIA)[_-][A-Za-z0-9_=-]{8,}"
    r"|\b[A-Za-z0-9_-]{16,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\b"
    r"|\b(?:password|passwd|secret|token|api[_-]?key|client[_-]?secret)\s*[:=]\s*(?=\S*\d)\S{6,}"
)
_KEY_LIKE_RUN = re.compile(r"[A-Za-z0-9+_=-]{32,}")
_KEY_LIKE_QUERY = re.compile(r"(?i)(token|key|secret|password|passwd|sig|signature|code|auth|session)")


def looks_secret(text: str) -> bool:
    """Whether text contains something shaped like a credential (never stored in setup data)."""

    text = text or ""
    if _SECRET_TEXT.search(text):
        return True
    return any(
        sum(char.isdigit() for char in run) >= 4 and any(char.isalpha() for char in run)
        for run in _KEY_LIKE_RUN.findall(text)
    )


def _plain(value: str, *, what: str) -> str:
    value = " ".join(str(value or "").split())
    if looks_secret(value):
        raise ValueError(f"The {what} looks like it contains a secret; setup steps never carry credentials.")
    return value


class ExternalSetupStepV1(BaseModel):
    """One plain step done outside Universal Agents, marked done by the person."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    step_id: str = Field(description="Stable id, lowercase letters, digits and underscores.")
    title: str = Field(min_length=3, max_length=120, description="What to do, for example 'Create a token'.")
    instructions: str = Field(min_length=3, max_length=500, description="How to do it, in plain words.")
    url: str | None = Field(default=None, max_length=500, description="Where to do it (https only).")
    verify: str = Field(
        default="", max_length=200, description="How the person can tell it worked, in plain words (optional)."
    )
    who: ExternalStepWho = EXTERNAL_STEP_BY_PERSON

    @field_validator("step_id")
    @classmethod
    def _valid_step_id(cls, value: str) -> str:
        if not STEP_ID_PATTERN.fullmatch(value):
            raise ValueError("A step id is 1-40 lowercase letters, digits or underscores, starting with a letter.")
        return value

    @field_validator("title")
    @classmethod
    def _plain_title(cls, value: str) -> str:
        return _plain(value, what="step title")

    @field_validator("instructions")
    @classmethod
    def _plain_instructions(cls, value: str) -> str:
        return _plain(value, what="step instructions")

    @field_validator("verify")
    @classmethod
    def _plain_verify(cls, value: str) -> str:
        return _plain(value, what="check") if value else ""

    @field_validator("url")
    @classmethod
    def _safe_url(cls, value: str | None) -> str | None:
        if value is None or not value.strip():
            return None
        value = value.strip()
        parts = urlsplit(value)
        if parts.scheme != "https" or not parts.hostname:
            raise ValueError("A setup step links only to an https address.")
        if parts.username or parts.password or "@" in parts.netloc:
            raise ValueError("A setup step link cannot contain sign-in details.")
        if any(_KEY_LIKE_QUERY.search(key) for key, _ in parse_qsl(parts.query, keep_blank_values=True)):
            raise ValueError("A setup step link cannot carry tokens, keys or codes in its address.")
        if looks_secret(value):
            raise ValueError("The setup step link looks like it contains a secret.")
        return value


class ToolSetupSpecV1(BaseModel):
    """Optional setup data of a catalog tool (module docstring)."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    external_steps: tuple[ExternalSetupStepV1, ...] = Field(default=(), max_length=MAX_EXTERNAL_STEPS)
    prerequisites: tuple[str, ...] = Field(default=(), max_length=MAX_PREREQUISITES)

    @field_validator("prerequisites")
    @classmethod
    def _valid_prerequisites(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        for tool_id in value:
            if not PREREQUISITE_PATTERN.fullmatch(tool_id):
                raise ValueError(f"A prerequisite is a catalog tool id such as cli:github, not {tool_id!r}.")
        if len(set(value)) != len(value):
            raise ValueError("A prerequisite is listed twice.")
        return value

    @model_validator(mode="after")
    def _unique_steps(self) -> "ToolSetupSpecV1":
        ids = [step.step_id for step in self.external_steps]
        if len(ids) != len(set(ids)):
            raise ValueError("Two setup steps share a step id.")
        return self

    def step(self, step_id: str) -> ExternalSetupStepV1 | None:
        return next((item for item in self.external_steps if item.step_id == step_id), None)


__all__ = [
    "EXTERNAL_STEP_BY_ADMINISTRATOR",
    "EXTERNAL_STEP_BY_PERSON",
    "ExternalSetupStepV1",
    "MAX_EXTERNAL_STEPS",
    "MAX_PREREQUISITES",
    "ToolSetupSpecV1",
    "looks_secret",
]
