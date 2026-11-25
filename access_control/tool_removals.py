"""Validation, baseline pinning, and trusted-code review rows for proposed tool removals.

When TeamLead proposes removing an existing proposal-written web API (``http:*``)
or installed CLI (``cli:*``) tool through ``AccessProposalV1.tool_removals``:

- Only single-tool ``control/access/tools/proposed-*.yaml`` files for ``http:*``
  and installed ``cli:*`` tools are eligible. Built-in tools (``ua_definitions``),
  operator hand-written catalog files, MCP servers (``mcp:*``, managed through
  ``mcp_connections``), unknown tools, and unsafe/symlinked files are refused
  with a plain-language reason and a structured warning log
  (``event=proposal_tool_removal_refused``).
- Duplicate removals and proposals that combine removing a tool with adding,
  changing, granting, blocking, or linking that same tool are refused.
- Trusted code writes every review row under section ``tool_removal``: an
  overview row naming the tool, its kind, its hosts or compact CLI command
  summary (explicitly noting for ``cli:*`` that workspace files under
  ``team/tools/<name>/`` are not deleted), followed by one explicit row per
  active grant, active tool/operation block, and active account/gate link that
  approval will revoke, without silent truncation.
"""

from __future__ import annotations

import logging
import re
from collections.abc import Iterable
from datetime import datetime
from pathlib import Path
from typing import Any, NoReturn

from access_catalog.catalog import CatalogError, ToolCatalog, builtin_tool_ids
from access_catalog.instance_tools import ProposedToolFile, read_proposed_tool_file
from access_control.proposal_errors import ProposalRefused
from access_control.requests import LEVEL_WORDS
from access_control.stale_blocks import subject_name
from custom_messages.contracts.access_v2 import (
    AccessProposalV1,
    AccountLinkStatus,
    AccountLinkV1,
    GrantKind,
    GrantLifetime,
    GrantStatus,
    GrantV1,
    LinkOwnerKind,
    LinkPurpose,
    ReviewItemV1,
    TargetKind,
    TargetRefV1,
    ToolDefinitionV1,
    ToolKind,
)
from custom_messages.contracts.cli_tool_v1 import binary_parts

logger = logging.getLogger(__name__)

REMOVABLE_TOOL_KINDS: frozenset[ToolKind] = frozenset({ToolKind.HTTP, ToolKind.CLI})
_ROW_TEXT_LIMIT = 1600
_LIFETIME_WORDS = {
    GrantLifetime.ONE_TIME: "one-time use",
    GrantLifetime.TASK: "for one task",
    GrantLifetime.UNTIL: "until a set time",
    GrantLifetime.UNTIL_REVOKED: "until removed",
}


def target_matches_tool(target: TargetRefV1, tool_id: str) -> bool:
    """Whether ``target`` names ``tool_id`` as a whole tool or one of its ``tool_id#<op>`` operations."""

    if target.kind is TargetKind.TOOL:
        return target.id == tool_id
    if target.kind is TargetKind.OPERATION:
        prefix, sep, _ = target.id.partition("#")
        return bool(sep) and prefix == tool_id
    return False


def grant_targets_tool(grant: GrantV1, tool_id: str) -> bool:
    """Whether ``grant`` (access, credential use, or block) targets ``tool_id`` or one of its operations."""

    return target_matches_tool(grant.target, tool_id)


def is_live_grant(grant: GrantV1, *, now: datetime | None = None) -> bool:
    """Whether ``grant`` is active or pending, unexpired (when ``now`` is given), and has uses remaining."""

    if grant.status not in {GrantStatus.ACTIVE, GrantStatus.PENDING}:
        return False
    if now is not None and grant.expires_at_utc is not None and grant.expires_at_utc <= now:
        return False
    if grant.uses_remaining is not None and grant.uses_remaining <= 0:
        return False
    return True


def _grant_sort_key(grant: GrantV1) -> tuple[str, str, str, str]:
    return (grant.target.id, grant.subject.kind.value, grant.subject.id, grant.grant_id)


def active_grants_for_tool(
    grants: Iterable[GrantV1], tool_id: str, *, now: datetime | None = None
) -> tuple[list[GrantV1], list[GrantV1]]:
    """Split active/pending, unexpired grants targeting ``tool_id`` into ``(active_grants, active_blocks)``."""

    active_access: list[GrantV1] = []
    active_blocks: list[GrantV1] = []
    for grant in grants:
        if not is_live_grant(grant, now=now) or not grant_targets_tool(grant, tool_id):
            continue
        if grant.kind is GrantKind.BLOCK:
            active_blocks.append(grant)
        else:
            active_access.append(grant)
    active_access.sort(key=_grant_sort_key)
    active_blocks.sort(key=_grant_sort_key)
    return active_access, active_blocks


def active_grants_for_tool_in_store(
    store: Any, team_id: str, tool_id: str, *, now: datetime
) -> list[GrantV1]:
    """All active or pending, unexpired grants and blocks targeting ``tool_id`` in ``store`` (stable order)."""

    matched = [
        grant
        for grant in store.for_status(team_id, (GrantStatus.ACTIVE, GrantStatus.PENDING))
        if is_live_grant(grant, now=now) and grant_targets_tool(grant, tool_id)
    ]
    matched.sort(key=_grant_sort_key)
    return matched


def _refuse_removal(reason: str, tool_id: str, message: str, *, extra: str = "") -> NoReturn:
    suffix = f" {extra}" if extra else ""
    logger.warning(
        "Refused a proposed tool removal. event=proposal_tool_removal_refused reason=%s tool_id=%s%s",
        reason,
        tool_id,
        suffix,
    )
    raise ProposalRefused(message)


def load_removable_tool_file(
    instance_root: Path, catalog: ToolCatalog, tool_id: str
) -> ProposedToolFile:
    """Read and verify the single-tool ``proposed-*.yaml`` file eligible for ``tool_removals``."""

    if tool_id.startswith("mcp:"):
        _refuse_removal(
            "mcp_tool",
            tool_id,
            f"{tool_id} is an MCP server and cannot be removed through tool_removals; "
            "manage MCP servers with mcp_connections.",
        )
    if tool_id in builtin_tool_ids():
        _refuse_removal(
            "builtin_tool",
            tool_id,
            f"{tool_id} is a built-in tool and cannot be removed through an access proposal.",
        )
    existing = catalog.tools.get(tool_id)
    if existing is None:
        _refuse_removal(
            "unknown_tool",
            tool_id,
            f"{tool_id} is not in the tool catalog, so there is nothing to remove.",
        )
    if existing.kind is ToolKind.MCP:
        _refuse_removal(
            "mcp_tool",
            tool_id,
            f"{tool_id} is an MCP server and cannot be removed through tool_removals; "
            "manage MCP servers with mcp_connections.",
        )
    if existing.kind not in REMOVABLE_TOOL_KINDS:
        _refuse_removal(
            "unsupported_tool_kind",
            tool_id,
            f"Only proposal-written web API (http:*) and installed CLI (cli:*) tools can be removed "
            f"through tool_removals, not {tool_id}.",
            extra=f"kind={existing.kind.value}",
        )
    if tool_id in catalog.reached_through or any(
        owner == tool_id for owner in catalog.http_origins.values()
    ):
        _refuse_removal(
            "operator_managed_file",
            tool_id,
            f"{tool_id} uses operator-configured routing and cannot be removed through an access proposal.",
        )
    if existing.kind is ToolKind.CLI and existing.installed_cli is None:
        _refuse_removal(
            "operator_managed_file",
            tool_id,
            f"{tool_id} is not an installed CLI tool, so it cannot be removed through an access proposal.",
        )
    try:
        return read_proposed_tool_file(instance_root, tool_id, allowed_kinds=REMOVABLE_TOOL_KINDS)
    except FileNotFoundError as exc:
        logger.warning(
            "Refused a proposed tool removal for an operator-managed catalog file. "
            "event=proposal_tool_removal_refused reason=operator_managed_file tool_id=%s",
            tool_id,
        )
        raise ProposalRefused(
            f"{tool_id} is defined in an operator-managed catalog file, not a proposal-written file, "
            "so it cannot be removed through an access proposal."
        ) from exc
    except CatalogError as exc:
        logger.warning(
            "Refused a proposed tool removal because its catalog file is invalid. "
            "event=proposal_tool_removal_refused reason=invalid_proposed_file tool_id=%s error=%s",
            tool_id,
            exc,
        )
        raise ProposalRefused(f"{tool_id} cannot be removed: {exc}") from exc


def check_tool_removal_conflicts(proposal: AccessProposalV1) -> tuple[str, ...]:
    """Refuse duplicate tool removals and same-proposal conflicts on any removed tool_id."""

    if not proposal.tool_removals:
        return ()
    ordered: list[str] = []
    removed: set[str] = set()
    for item in proposal.tool_removals:
        if item.tool_id in removed:
            _refuse_removal(
                "duplicate_removal",
                item.tool_id,
                f"{item.tool_id} is listed more than once in tool_removals.",
            )
        removed.add(item.tool_id)
        ordered.append(item.tool_id)

    added_ids = {tool.tool_id for tool in proposal.tools} | {cli.tool_id for cli in proposal.clis}
    changed_ids = {changed.tool_id for changed in proposal.tool_changes}
    linked_ids = {link.tool_id for link in proposal.account_links}
    for tool_id in ordered:
        if tool_id in added_ids:
            _refuse_removal(
                "conflict_with_add",
                tool_id,
                f"{tool_id} cannot be both removed and added in the same proposal.",
            )
        if tool_id in changed_ids:
            _refuse_removal(
                "conflict_with_change",
                tool_id,
                f"{tool_id} cannot be both removed and changed in the same proposal.",
            )
        if any(target_matches_tool(grant.target, tool_id) for grant in proposal.grants):
            _refuse_removal(
                "conflict_with_grant",
                tool_id,
                f"{tool_id} cannot be removed while the same proposal grants access to it.",
            )
        if any(target_matches_tool(block.target, tool_id) for block in proposal.blocks):
            _refuse_removal(
                "conflict_with_block",
                tool_id,
                f"{tool_id} cannot be removed while the same proposal adds a restriction on it.",
            )
        if tool_id in linked_ids:
            _refuse_removal(
                "conflict_with_link",
                tool_id,
                f"{tool_id} cannot be removed while the same proposal links a credential to it.",
            )
    return tuple(ordered)


def with_tool_removal_baselines(
    proposal: AccessProposalV1, *, instance_root: Path, catalog: ToolCatalog
) -> AccessProposalV1:
    """Validate tool removals and pin ``expected_sha256`` from each current ``proposed-*.yaml`` file.

    Always overwrites any caller-supplied digest so agents cannot pin their own
    baseline; runs before compiling and storing the proposal.
    """

    if not proposal.tool_removals:
        return proposal
    check_tool_removal_conflicts(proposal)
    pinned = []
    for item in proposal.tool_removals:
        tool_file = load_removable_tool_file(instance_root, catalog, item.tool_id)
        pinned.append(item.model_copy(update={"expected_sha256": tool_file.sha256}))
    return proposal.model_copy(update={"tool_removals": tuple(pinned)})


def _review_row(item_id: str, text: str, *, flags: tuple[str, ...] = ()) -> ReviewItemV1:
    """Build one ``tool_removal`` review row without ever truncating its text."""

    if len(text) > _ROW_TEXT_LIMIT:
        raise ValueError(
            f"Review row {item_id!r} is {len(text)} characters, exceeding the {_ROW_TEXT_LIMIT}-character limit."
        )
    return ReviewItemV1(item_id=item_id, section="tool_removal", text=text, flags=flags)


def _plural(count: int, singular: str, plural: str) -> str:
    return f"{count} {singular if count == 1 else plural}"


def _cli_commands_summary(definition: ToolDefinitionV1) -> str:
    cli = definition.installed_cli
    assert cli is not None
    total = len(cli.commands)
    counts = {level: sum(1 for val in cli.commands.values() if val == level) for level in ("read", "write", "full")}
    breakdown = ", ".join(f"{count} {level}" for level, count in counts.items() if count)
    commands_part = (
        f"{_plural(total, 'classified command', 'classified commands')} ({breakdown})"
        if breakdown
        else "0 classified commands"
    )
    folder = f"team/tools/{binary_parts(cli.binary_path)[2]}/"
    return (
        f"program {cli.binary_path}, {commands_part}. "
        f"Workspace files under {folder} are not deleted."
    )


def _impact_summary(grant_count: int, block_count: int, link_count: int) -> str:
    parts: list[str] = []
    if grant_count:
        parts.append(_plural(grant_count, "active grant", "active grants"))
    if block_count:
        parts.append(_plural(block_count, "active block", "active blocks"))
    if link_count:
        parts.append(_plural(link_count, "active credential link", "active credential links"))
    if not parts:
        return "No active grants, blocks, or credential links are affected."
    if len(parts) == 1:
        listed = parts[0]
    elif len(parts) == 2:
        listed = f"{parts[0]} and {parts[1]}"
    else:
        listed = f"{parts[0]}, {parts[1]}, and {parts[2]}"
    return f"Revokes {listed}."


def _pack_text_rows(
    item_id_prefix: str,
    header: str,
    entries: list[str],
    *,
    separator: str = ", ",
    suffix: str = ".",
) -> list[ReviewItemV1]:
    """Pack complete ``entries`` across as many rows as needed so no entry is ever cut."""

    rows: list[ReviewItemV1] = []
    chunk: list[str] = []
    for entry in entries:
        candidate = f"{header}{separator.join([*chunk, entry])}{suffix}"
        if chunk and len(candidate) > _ROW_TEXT_LIMIT:
            rows.append(_review_row(f"{item_id_prefix}-{len(rows)}", f"{header}{separator.join(chunk)}{suffix}"))
            chunk = [entry]
        else:
            chunk.append(entry)
    if chunk:
        rows.append(_review_row(f"{item_id_prefix}-{len(rows)}", f"{header}{separator.join(chunk)}{suffix}"))
    return rows


def _overview_rows(
    index: int,
    definition: ToolDefinitionV1,
    *,
    grant_count: int,
    block_count: int,
    link_count: int,
) -> list[ReviewItemV1]:
    impact = _impact_summary(grant_count, block_count, link_count)
    if definition.kind is ToolKind.CLI and definition.installed_cli is not None:
        detail = _cli_commands_summary(definition)
        text = f"Remove installed CLI tool {definition.display_name} ({definition.tool_id}); {detail} {impact}"
        return [_review_row(f"remove-{index}", text)]
    hosts = ", ".join(definition.hosts) if definition.hosts else "no hosts"
    inline = f"Remove web API tool {definition.display_name} ({definition.tool_id}); hosts: {hosts}. {impact}"
    if len(inline) <= _ROW_TEXT_LIMIT or not definition.hosts:
        return [_review_row(f"remove-{index}", inline)]
    compact = (
        f"Remove web API tool {definition.display_name} ({definition.tool_id}); "
        f"{_plural(len(definition.hosts), 'host', 'hosts')} (listed below). {impact}"
    )
    return [
        _review_row(f"remove-{index}", compact),
        *_pack_text_rows(
            f"remove-{index}-hosts",
            f"Hosts removed with {definition.display_name} ({definition.tool_id}): ",
            list(definition.hosts),
        ),
    ]


def _grant_scope_words(grant: GrantV1) -> str:
    parts = [_LIFETIME_WORDS[grant.lifetime]]
    if grant.for_person_id:
        parts.append(f"for {grant.for_person_id}'s work")
    return ", ".join(parts)


def _grant_impact_rows(
    index: int, definition: ToolDefinitionV1, grants: list[GrantV1]
) -> list[ReviewItemV1]:
    rows: list[ReviewItemV1] = []
    for grant_index, grant in enumerate(grants):
        who = subject_name(grant.subject)
        level = LEVEL_WORDS[grant.level]
        scope = _grant_scope_words(grant)
        if grant.target.kind is TargetKind.OPERATION:
            op = grant.target.id.partition("#")[2]
            target_words = f"{definition.display_name} ({definition.tool_id}, operation {op})"
        else:
            target_words = f"{definition.display_name} ({definition.tool_id})"
        status_word = "pending" if grant.status is GrantStatus.PENDING else "active"
        rows.append(
            _review_row(
                f"remove-{index}-grant-{grant_index}",
                f"Revoke {status_word} grant {grant.grant_id}: {who} loses {level} access to {target_words} ({scope}).",
            )
        )
    return rows


def _block_impact_rows(
    index: int, definition: ToolDefinitionV1, blocks: list[GrantV1]
) -> list[ReviewItemV1]:
    rows: list[ReviewItemV1] = []
    for block_index, block in enumerate(blocks):
        who = subject_name(block.subject)
        scope = _grant_scope_words(block)
        if block.target.kind is TargetKind.OPERATION:
            op = block.target.id.partition("#")[2]
            text = (
                f"Revoke active operation block {block.grant_id}: {who} on "
                f"{definition.display_name} ({definition.tool_id}, operation {op}) ({scope})."
            )
        else:
            text = (
                f"Revoke active tool block {block.grant_id}: {who} on "
                f"{definition.display_name} ({definition.tool_id}) ({scope})."
            )
        rows.append(_review_row(f"remove-{index}-block-{block_index}", text))
    return rows


def _link_impact_rows(
    index: int, definition: ToolDefinitionV1, links: list[AccountLinkV1]
) -> list[ReviewItemV1]:
    rows: list[ReviewItemV1] = []
    for link_index, link in enumerate(links):
        if link.purpose is LinkPurpose.GATE:
            text = (
                f"Revoke active team gate credential link {link.link_id}: "
                f"on {definition.display_name} ({definition.tool_id})."
            )
        elif link.owner_kind is LinkOwnerKind.TEAM:
            text = (
                f"Revoke active team account link {link.link_id}: "
                f"on {definition.display_name} ({definition.tool_id})."
            )
        else:
            text = (
                f"Revoke active personal account link {link.link_id}: "
                f"for {link.owner_id} on {definition.display_name} ({definition.tool_id})."
            )
        rows.append(_review_row(f"remove-{index}-link-{link_index}", text))
    return rows


def tool_removal_rows(
    index: int,
    definition: ToolDefinitionV1,
    *,
    active_grants: Iterable[GrantV1],
    active_blocks: Iterable[GrantV1],
    active_links: Iterable[AccountLinkV1],
) -> list[ReviewItemV1]:
    """Build trusted-code review rows for one ``tool_removals`` item (no silent truncation)."""

    grants = sorted(
        (g for g in active_grants if g.kind is not GrantKind.BLOCK and is_live_grant(g)),
        key=_grant_sort_key,
    )
    blocks = sorted(
        (b for b in active_blocks if b.kind is GrantKind.BLOCK and is_live_grant(b)),
        key=_grant_sort_key,
    )
    links = sorted(
        (link for link in active_links if link.status is AccountLinkStatus.ACTIVE),
        key=lambda link: (link.purpose.value, link.owner_kind.value, link.owner_id, link.link_id),
    )
    return [
        *_overview_rows(
            index,
            definition,
            grant_count=len(grants),
            block_count=len(blocks),
            link_count=len(links),
        ),
        *_grant_impact_rows(index, definition, grants),
        *_block_impact_rows(index, definition, blocks),
        *_link_impact_rows(index, definition, links),
    ]


_OVERVIEW_ROW_ID = re.compile(r"^remove-\d+(?:-hosts-\d+)?$")
_IMPACT_ROW_ID = re.compile(r"^remove-\d+-(grant|block|link)-\d+$")


def is_tool_removal_overview_row(item: ReviewItemV1) -> bool:
    """Whether ``item`` is a public tool-removal overview or host-list continuation row."""

    return item.section == "tool_removal" and _OVERVIEW_ROW_ID.match(item.item_id) is not None


def parse_tool_removal_row_target(item: ReviewItemV1) -> tuple[str, str] | None:
    """Extract ``(kind, record_id)`` from a ``tool_removal`` detail row, or ``None`` if not parseable."""

    if item.section != "tool_removal":
        return None
    matched = _IMPACT_ROW_ID.match(item.item_id)
    if matched is None:
        return None
    kind = matched.group(1)
    head, sep, _ = item.text.partition(":")
    if not sep:
        return None
    record_id = head.rsplit(" ", 1)[-1].strip()
    return (kind, record_id) if record_id else None


def reviewed_removal_impact_ids(items: Iterable[ReviewItemV1]) -> tuple[frozenset[str], frozenset[str]]:
    """Return ``(reviewed_grant_and_block_ids, reviewed_link_ids)`` listed on a reviewed proposal card."""

    grant_ids: set[str] = set()
    link_ids: set[str] = set()
    for item in items:
        parsed = parse_tool_removal_row_target(item)
        if parsed is None:
            continue
        kind, record_id = parsed
        if kind == "link":
            link_ids.add(record_id)
        else:
            grant_ids.add(record_id)
    return frozenset(grant_ids), frozenset(link_ids)


__all__ = [
    "REMOVABLE_TOOL_KINDS",
    "active_grants_for_tool",
    "active_grants_for_tool_in_store",
    "check_tool_removal_conflicts",
    "grant_targets_tool",
    "is_live_grant",
    "is_tool_removal_overview_row",
    "load_removable_tool_file",
    "parse_tool_removal_row_target",
    "reviewed_removal_impact_ids",
    "target_matches_tool",
    "tool_removal_rows",
    "with_tool_removal_baselines",
]
