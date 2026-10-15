"""Blocks naming an operation the tool catalog no longer has (owner's decision 2026-10-10, option a).

A block may name one operation of a tool (``tool#operation``). Every way of
adding a block checks that operation against the catalog
(``AccessService.target_entities_for_ref``), so a stored block names a missing
operation only after the catalog changed under it: an update renamed or merged
a tool's functions (``find_tools`` moved from ``fn:access`` to
``fn:tool_loading``; the memory functions became ``memory(action=...)``), or an
MCP server dropped one of its tools. A Cedar link on the old name would match
nothing, so the block would quietly stop blocking. Instead:

- decisions apply such a block to its whole tool (``decided_grant``, used by
  ``AccessService._evaluate`` for every decision and for ``blocks_on``; the
  tool list leaves the tool out, ``access_catalog.tool_functions``), so it
  fails closed: whoever it stopped from one operation is stopped from the
  whole tool until an administrator replaces it. Nothing is rewritten; the
  stored block keeps naming what its author chose;
- administrators get one Inbox notice per such block (``report_stale_blocks``,
  run by the gateway at start), naming the block, whom it stops, the tool's
  operations now, and the commands that replace it with a block on one of them.

A team transfer converts the operation names its own release renamed
(``team_transfer.access``), so imported blocks keep their exact scope.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any, Iterable

from access_catalog.catalog import ToolCatalog, missing_operation
from access_control.people import PERSON_ROLE_ADMIN
from custom_messages.contracts.access_v2 import (
    AccessNoticeKind,
    AccessNoticeV1,
    GrantKind,
    GrantStatus,
    GrantV1,
    SubjectKind,
    SubjectRefV1,
    TargetKind,
    TargetRefV1,
)

logger = logging.getLogger(__name__)

STALE_BLOCK_NOTICE_PREFIX = "stale_block"
# Operations listed in a notice; a tool with more is summarized.
_LISTED_OPERATIONS = 30
_WARNED: set[str] = set()


def decided_grant(catalog: ToolCatalog, grant: GrantV1) -> GrantV1:
    """The grant as decisions apply it: a block on a missing operation covers its whole tool (module docstring)."""

    if grant.kind is not GrantKind.BLOCK:
        return grant
    operation = missing_operation(catalog.tools, grant.target)
    if operation is None:
        return grant
    tool_id = grant.target.id.split("#", 1)[0]
    if grant.grant_id not in _WARNED:
        _WARNED.add(grant.grant_id)
        logger.warning(
            "A block names an operation its tool no longer has, so it blocks the whole tool. "
            "event=stale_operation_block_applied grant_id=%s subject=%s:%s tool_id=%s operation=%s",
            grant.grant_id,
            grant.subject.kind.value,
            grant.subject.id,
            tool_id,
            operation,
        )
    return grant.model_copy(update={"target": TargetRefV1(kind=TargetKind.TOOL, id=tool_id)})


def stale_blocks(catalog: ToolCatalog, grants: Iterable[GrantV1], *, now: datetime) -> list[GrantV1]:
    """Active, unexpired blocks among ``grants`` that name an operation their tool no longer has."""

    return [
        grant
        for grant in grants
        if grant.kind is GrantKind.BLOCK
        and grant.status is GrantStatus.ACTIVE
        and (grant.expires_at_utc is None or grant.expires_at_utc > now)
        and missing_operation(catalog.tools, grant.target) is not None
    ]


def subject_text(subject: SubjectRefV1) -> str:
    """The subject as ``/access`` takes it (``access_control.operator_commands.parse_subject``)."""

    short = subject.id.split("/", 1)[-1]
    if subject.kind is SubjectKind.AGENT_TYPE:
        return f"agent-type:{short}"
    if subject.kind is SubjectKind.ROLE:
        return f"role:{short}"
    if subject.kind is SubjectKind.TEAM:
        return "team"
    if subject.kind is SubjectKind.USER:
        return f"user:{subject.id}"
    return f"{subject.kind.value}:{subject.id}"


def subject_name(subject: SubjectRefV1) -> str:
    """Whom a block stops, for people to read."""

    if subject.kind is SubjectKind.TEAM:
        return "everyone in the team"
    if subject.kind is SubjectKind.ROLE:
        return f"everyone with the {subject.id.split('/', 1)[-1]} role"
    return subject.id.split("/", 1)[-1] if subject.kind is SubjectKind.AGENT_TYPE else subject.id


def stale_block_notice(catalog: ToolCatalog, block: GrantV1, *, now: datetime) -> AccessNoticeV1:
    """The administrators' notice for one block on a missing operation."""

    tool_id, _, operation = block.target.id.partition("#")
    tool = catalog.tools[tool_id]
    names = sorted(definition.name for definition in tool.operations)
    listed = ", ".join(names[:_LISTED_OPERATIONS])
    if len(names) > _LISTED_OPERATIONS:
        listed += f" and {len(names) - _LISTED_OPERATIONS} more"
    subject, who = subject_text(block.subject), subject_name(block.subject)
    example = names[0] if names else "<operation>"
    return AccessNoticeV1(
        notice_id=f"{STALE_BLOCK_NOTICE_PREFIX}:{block.grant_id}"[:120],
        team_id=block.team_id,
        kind=AccessNoticeKind.BLOCK_WIDENED,
        recipient_roles=(PERSON_ROLE_ADMIN,),
        title=f"A block now stops {who} from using all of {tool.display_name}"[:400],
        summary=(
            f"Block {block.grant_id} stopped {who} from one operation of {tool_id}, {operation}, which this "
            f"version no longer has (an update renamed or removed it). So that it does not quietly allow that "
            f"operation again, it now blocks all of {tool_id} for {who}. To block one operation again, replace "
            f"this block in one step: `/access block {subject} {tool_id}#{example} --reason \"...\" "
            f"--replace {block.grant_id}`. The replacement keeps its expiry. To remove the restriction entirely, "
            f"use `/access unblock {block.grant_id}`. {tool_id} has these operations: {listed}. To keep "
            "the whole tool blocked, do nothing."
        )[:4000],
        created_at_utc=now,
    )


def report_stale_blocks(catalog: ToolCatalog, store: Any, team_id: str, *, now: datetime | None = None) -> list[str]:
    """Store one administrators' notice per block on a missing operation; the ids of blocks newly reported.

    ``store`` is the access store (``storage.access_repository.AccessRepository``).
    Notice ids come from the block, so a block is reported once however often
    this runs.
    """

    moment = now or datetime.now(timezone.utc)
    reported = []
    for block in stale_blocks(catalog, store.for_status(team_id, (GrantStatus.ACTIVE,)), now=moment):
        logger.warning(
            "A stored block names an operation its tool no longer has; it blocks the whole tool until an "
            "administrator replaces it. event=stale_operation_block_found grant_id=%s subject=%s:%s target=%s",
            block.grant_id,
            block.subject.kind.value,
            block.subject.id,
            block.target.id,
        )
        if store.create_notice_if_absent(stale_block_notice(catalog, block, now=moment)):
            reported.append(block.grant_id)
    return reported


__all__ = [
    "STALE_BLOCK_NOTICE_PREFIX",
    "decided_grant",
    "report_stale_blocks",
    "stale_block_notice",
    "stale_blocks",
    "subject_name",
    "subject_text",
]
