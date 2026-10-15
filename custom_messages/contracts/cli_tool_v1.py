"""A command-line tool the team installs through an access proposal (owner's decision 2026-09-28).

When a service has a CLI, TeamLead may propose it as a catalog tool
``cli:<name>`` instead of a web API tool: the model sees one compact
``secure_cli`` provider, each command has its own access level, and the
broker keeps the token away from agents. This is data, no code per CLI:

- **The program** lives in the team workspace at ``team/tools/<name>/<file>``,
  where agents (or InternalImprovementAgent, building from source in the
  sandbox) put it. The administrator's approval pins its SHA-256. A download
  also names its https source and version, so trusted code can fetch it into
  that path at apply time. Because the team workspace is agent-writable, the
  broker never runs the workspace file: on every run it opens it without
  following links (``access_control.safe_open``), copies it into a private run
  folder, checks the copy's SHA-256 against the approved one, and runs only
  the checked copy, inside the same Landlock sandbox agents' code uses
  (``secure_tool_broker.installed_clis``). Updating a CLI is a new proposal
  with the new digest.
- **Commands** map dotted command paths to levels (like
  ``ua_definitions/access/cli/gh.yaml``); unlisted commands need ``full``.
  Blocked commands and flags are refused whatever access is held.
- **Hosts** it talks to are recorded for the review card only: Landlock limits
  TCP by port, not by host, so the broker cannot restrict where a CLI connects.

How the credential reaches the program (environment variable or a file in its
private home folder) and whose account it uses are the tool's
``credential_injections`` and ``link_owners`` (``access_v2.ToolDefinitionV1``).
"""

from __future__ import annotations

import re
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

CLI_NAME_PATTERN = re.compile(r"^[a-z0-9][a-z0-9_-]{0,39}$")
_FILE_NAME_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,99}$")
_COMMAND_PATTERN = re.compile(r"^[a-z0-9][a-z0-9_-]*(\.[a-z0-9][a-z0-9_-]*)*$")
_FLAG_PATTERN = re.compile(r"^-{1,2}[A-Za-z0-9][A-Za-z0-9_-]*$")
_HOST_PATTERN = re.compile(r"^[a-z0-9][a-z0-9.-]{0,252}$")
TEAM_TOOLS_PREFIX = ("team", "tools")
CliLevel = Literal["read", "write", "full"]
MAX_COMMANDS = 300


def binary_parts(path: str) -> tuple[str, ...]:
    """``team/tools/<name>/<file>`` as path parts, refusing anything else."""

    parts = tuple(str(path or "").strip().split("/"))
    if len(parts) != 4 or parts[:2] != TEAM_TOOLS_PREFIX:
        raise ValueError("The program lives at team/tools/<name>/<file> in the team workspace.")
    if not CLI_NAME_PATTERN.fullmatch(parts[2]) or not _FILE_NAME_PATTERN.fullmatch(parts[3]):
        raise ValueError("The program's folder and file names are plain names (letters, digits, - _ .).")
    return parts


class InstalledCliV1(BaseModel):
    """The program and command rules of one installed CLI (module docstring)."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    binary_path: str = Field(description="team/tools/<name>/<file> in the team workspace.")
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$", description="The approved program's SHA-256.")
    version: str = Field(default="", max_length=60)
    source_url: str | None = Field(default=None, max_length=500, description="https download of this version.")
    archive_member: str | None = Field(
        default=None, max_length=200, description="The program's path inside a .tar.gz or .zip download."
    )
    commands: dict[str, CliLevel] = Field(default_factory=dict, description="Dotted command path -> level.")
    blocked_commands: dict[str, str] = Field(default_factory=dict, description="Dotted command path -> why.")
    blocked_flags: tuple[str, ...] = Field(default=(), max_length=60)
    global_flags: tuple[str, ...] = Field(default=("--help", "--version"), max_length=40)
    global_flags_with_value: tuple[str, ...] = Field(default=(), max_length=40)
    allowed_hosts: tuple[str, ...] = Field(default=(), max_length=10, description="For the review card only.")

    @field_validator("binary_path")
    @classmethod
    def _valid_path(cls, value: str) -> str:
        binary_parts(value)
        return value

    @field_validator("source_url")
    @classmethod
    def _https_source(cls, value: str | None) -> str | None:
        if value is None:
            return None
        from urllib.parse import urlsplit

        parts = urlsplit(value.strip())
        if parts.scheme != "https" or not parts.hostname or parts.username or parts.password:
            raise ValueError("A CLI download comes from an https address without sign-in details.")
        return value.strip()

    @field_validator("archive_member")
    @classmethod
    def _plain_member(cls, value: str | None) -> str | None:
        if value is None:
            return None
        if value.startswith("/") or ".." in value.split("/") or "\\" in value:
            raise ValueError("The archive member is a relative path inside the archive.")
        return value

    @field_validator("commands", "blocked_commands")
    @classmethod
    def _valid_commands(cls, value: dict[str, str]) -> dict[str, str]:
        if len(value) > MAX_COMMANDS:
            raise ValueError(f"At most {MAX_COMMANDS} commands.")
        for path in value:
            if not _COMMAND_PATTERN.fullmatch(path):
                raise ValueError(f"A command is dotted words such as 'repo.list', not {path!r}.")
        return value

    @field_validator("blocked_flags", "global_flags", "global_flags_with_value")
    @classmethod
    def _valid_flags(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        for flag in value:
            if not _FLAG_PATTERN.fullmatch(flag):
                raise ValueError(f"A flag is -x or --name, not {flag!r}.")
        return value

    @field_validator("allowed_hosts")
    @classmethod
    def _valid_hosts(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        hosts = tuple(host.strip().lower() for host in value)
        for host in hosts:
            if not _HOST_PATTERN.fullmatch(host):
                raise ValueError(f"Hosts are exact names: {host!r}")
        return hosts

    @model_validator(mode="after")
    def _consistent(self) -> "InstalledCliV1":
        overlap = [
            path
            for path in self.commands
            if any(path == blocked or path.startswith(blocked + ".") for blocked in self.blocked_commands)
        ]
        if overlap:
            raise ValueError(f"Blocked commands cannot also have a level: {overlap[:5]}")
        if self.archive_member and not self.source_url:
            raise ValueError("An archive member comes with a download address.")
        return self

    @property
    def name(self) -> str:
        return binary_parts(self.binary_path)[2]

    def command_table(self) -> dict[str, object]:
        """The command table in the shape of ``ua_definitions/access/cli/*.yaml``."""

        return {
            "cli": self.name,
            "commands": dict(self.commands),
            "blocked_commands": dict(self.blocked_commands),
            "blocked_flags": list(self.blocked_flags),
            "global_flags": list(self.global_flags),
            "global_flags_with_value": list(self.global_flags_with_value),
        }


__all__ = ["CLI_NAME_PATTERN", "InstalledCliV1", "TEAM_TOOLS_PREFIX", "binary_parts"]
