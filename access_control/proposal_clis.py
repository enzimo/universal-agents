"""The "add a CLI" item of an access proposal: its review rows and its installation (owner's decision 2026-09-28).

``access_control.proposals`` compiles and applies proposals; this module holds
what is particular to CLIs (``ProposedCliToolV1``):

- **Review rows**, in trusted code's words: the program (its team-workspace
  path, pinned SHA-256, version and download host), whose account it uses
  and how the credential reaches it (an environment variable or a file in its
  private home folder, never its command line), the hosts it talks to (not
  enforced: Landlock limits TCP by port, not host), each command's level with
  unlisted commands at full, blocked commands and flags, and its setup steps.
  Flags: a new program, a download host, full commands, the team's account.
- **Installation** at approval, in the gateway: the program already in the
  workspace must match the approved SHA-256; otherwise it is downloaded from
  the approved https address (``access_control.cli_binaries``), checked, and
  written without following links. The caller removes what was written if
  anything later fails, so nothing is applied.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from pathlib import Path
from urllib.parse import urlsplit

from access_control.cli_binaries import (
    REASON_DIGEST_MISMATCH,
    REASON_PROGRAM_MISSING,
    Downloader,
    ProgramRefused,
    extract_member,
    install_program,
    workspace_digest,
    workspaces_root,
)
from custom_messages.contracts.access_v2 import InjectionPlacement, ProposedCliToolV1, ReviewItemV1
from custom_messages.contracts.cli_tool_v1 import binary_parts

logger = logging.getLogger(__name__)

_LEVEL_WORDS = {
    "read": "Reading (read)",
    "write": "Changes (read-and-change)",
    "full": "Full (needs full access; asked for each time otherwise)",
}


def _whose(cli: ProposedCliToolV1) -> str:
    owners = cli.link_owners
    if owners == ("team",):
        return "the team's account (an administrator links it)"
    if owners == ("person",):
        return "each person's own account"
    return "each person's own account, or else the team's"


def _delivery(cli: ProposedCliToolV1) -> str:
    places = [
        f"the environment variable {item.name}" if item.placement is InjectionPlacement.ENV else f"the file ~/{item.name}"
        for item in cli.credential_injections
    ]
    return " and ".join(places)


def _program_row(index: int, cli: ProposedCliToolV1) -> ReviewItemV1:
    program = cli.installed_cli
    source = (
        f", version {program.version or 'unnamed'} downloaded from {urlsplit(program.source_url).hostname}"
        if program.source_url
        else " (already in the team workspace)"
    )
    account = (
        f" It uses {_whose(cli)}; the broker gives it the credential in {_delivery(cli)}, never on its command "
        "line, and removes it from its output."
        if cli.credential_injections
        else " It uses no account."
    )
    hosts = (
        f" It talks to {', '.join(program.allowed_hosts)} (not enforced: the sandbox cannot limit hosts)."
        if program.allowed_hosts
        else ""
    )
    flags = ["new_program"] + (["download_host"] if program.source_url else [])
    if cli.credential_injections and "team" in cli.link_owners:
        flags.append("team_account")
    return ReviewItemV1(
        item_id=f"cli-{index}",
        section="tool",
        text=(
            f"New command-line tool {cli.display_name} ({cli.tool_id}): runs the program {program.binary_path} "
            f"with SHA-256 {program.sha256}{source}, checked before every run and run inside the sandbox "
            f"(only its own private folder; network open).{account}{hosts}"
        ),
        flags=tuple(flags),
    )


def _commands_row(index: int, cli: ProposedCliToolV1) -> ReviewItemV1:
    program = cli.installed_cli
    groups: dict[str, list[str]] = {}
    for path, level in sorted(program.commands.items()):
        groups.setdefault(level, []).append(path.replace(".", " "))
    parts = [f"{_LEVEL_WORDS[level]}: {', '.join(groups[level])}" for level in ("read", "write", "full") if level in groups]
    blocked = ", ".join(path.replace(".", " ") for path in program.blocked_commands)
    blocked_flags = ", ".join(program.blocked_flags)
    text = f"{cli.display_name} commands. " + ". ".join(parts) + ". Any other command needs full."
    if blocked:
        text += f" Never run: {blocked}."
    if blocked_flags:
        text += f" Refused options: {blocked_flags}."
    full = "full" in groups
    return ReviewItemV1(item_id=f"cli-{index}-commands", section="tool", text=text[:1600], flags=("full_access",) if full else ())


def cli_rows(index: int, cli: ProposedCliToolV1, setup_rows) -> list[ReviewItemV1]:  # noqa: ANN001 - proposals' helper
    """The review rows of one proposed CLI (module docstring)."""

    return [_program_row(index, cli), _commands_row(index, cli), *setup_rows(f"cli-{index}", cli)]


def install_programs(
    instance_root: Path, clis: tuple[ProposedCliToolV1, ...], download: Downloader,
    *, before_write: Callable[[Path, tuple[str, ...], int, bytes], None] | None = None,
) -> None:
    """Check existing programs or record each newly allocated file before writing its bytes."""

    root = workspaces_root(instance_root)
    for cli in clis:
        program = cli.installed_cli
        parts = binary_parts(program.binary_path)
        present = workspace_digest(root, parts)
        if present is None:
            if not program.source_url:
                raise ProgramRefused(
                    f"The program is not at {program.binary_path}; put it there or propose its download address.",
                    reason_code=REASON_PROGRAM_MISSING,
                )
            data = download(program.source_url)
            if program.archive_member:
                data = extract_member(data, program.archive_member)
            prepare = None if before_write is None else lambda fd, body: before_write(root, parts, fd, body)
            install_program(root, parts, data, program.sha256, before_write=prepare)
        elif present != program.sha256:
            raise ProgramRefused(
                f"The program at {program.binary_path} is not the reviewed one (SHA-256 {present[:12]}..., "
                f"reviewed {program.sha256[:12]}...).",
                reason_code=REASON_DIGEST_MISMATCH,
                actual_sha256=present,
            )


__all__ = ["cli_rows", "install_programs"]
