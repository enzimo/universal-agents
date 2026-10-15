"""One entry per tool: how access grants for the same thing combine.

Why (owner's decision 2026-09-30): people saw several separate grants for one
tool on the Access page (read-and-change for one task, full until removed, one
operation), assumed the broadest replaced the others, and could not tell which
of them still worked. Removing the broadest one could leave another still
giving access. So access for one holder and one tool is one entry:

- **An entry** is every active access grant with the same holder (subject:
  an agent type, a schedule, a person...), serving the same person's work, on
  the same tool (a tool together with its operations), tool set, folder or
  file (``entry_key``).
- **Broader replaces narrower.** A new grant that covers an existing one
  (``grant_covers``: same or wider target, same or higher level, lasts at
  least as long, hands on at least as much) replaces it. What was handed on
  from the replaced grant moves to the new one, because it still narrows it;
  replacing never cuts off a worker. A tool set grant also replaces grants on
  its own tools.
- **Nothing new when already covered.** A new grant an existing one covers is
  not stored; the existing one stands.
- **Side by side only when neither covers the other**, typically lasting
  access plus a higher level for one task (a temporary step-up that ends with
  its task, ``AccessService.end_task_grants``). The Access page shows them as
  one entry, and removing the entry removes them all.

``plan_grants`` is pure: the access service reads the holder's grants, plans,
and the store applies the plan in the same transaction as the new grants.
Blocks and credential-use grants are not access and are stored as they are.
Pending hand-downs are not access yet; they are planned when approved.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Callable, Iterable, Sequence

from access_control.folders import covers as folder_covers
from custom_messages.contracts.access_v2 import (
    LIFETIME_RANK,
    TASK_BOUND_LIFETIMES,
    DelegationMode,
    GrantKind,
    GrantStatus,
    GrantV1,
    SubjectRefV1,
    TargetKind,
    TargetRefV1,
)

# Tool set id -> the tool ids in it, from the catalog.
ToolsInSet = Callable[[str], frozenset[str]]


@dataclass(frozen=True)
class EntryKey:
    """What one entry is about: holder, whose work, and the tool (operations count as their tool)."""

    subject: SubjectRefV1
    for_person_id: str | None
    target_kind: TargetKind
    target_id: str


def entry_target(target: TargetRefV1) -> tuple[TargetKind, str]:
    """The entry's target: an operation belongs to its tool's entry."""

    if target.kind is TargetKind.OPERATION:
        return TargetKind.TOOL, target.id.split("#", 1)[0]
    return target.kind, target.id


def entry_key(grant: GrantV1) -> EntryKey:
    kind, target_id = entry_target(grant.target)
    return EntryKey(grant.subject, grant.for_person_id, kind, target_id)


_FAR_FUTURE = datetime.max.replace(tzinfo=timezone.utc)


def lasting_order(grant: GrantV1) -> tuple[int, datetime, int]:
    """Sort key, largest first: longest-lasting, then highest level. An entry's main grant sorts first;
    the others are step-ups beside it."""

    return LIFETIME_RANK[grant.lifetime], grant.expires_at_utc or _FAR_FUTURE, grant.level.rank


def target_covers(outer: TargetRefV1, inner: TargetRefV1, tools_in_set: ToolsInSet) -> bool:
    """Whether everything ``inner`` names is inside ``outer`` (by catalog membership, not by Cedar)."""

    if outer == inner:
        return True
    if outer.kind is TargetKind.TOOL and inner.kind is TargetKind.OPERATION:
        return inner.id.split("#", 1)[0] == outer.id
    if outer.kind is TargetKind.TOOL_SET and inner.kind in {TargetKind.TOOL, TargetKind.OPERATION}:
        return inner.id.split("#", 1)[0] in tools_in_set(outer.id)
    if outer.kind is TargetKind.FOLDER and inner.kind in {TargetKind.FOLDER, TargetKind.FILE}:
        return folder_covers(outer.id, inner.id)
    return False


def lasts_as_long(outer: GrantV1, inner: GrantV1) -> bool:
    """Whether ``outer`` lasts at least as long as ``inner``: lifetime, expiry and task."""

    if LIFETIME_RANK[outer.lifetime] < LIFETIME_RANK[inner.lifetime]:
        return False
    if outer.lifetime in TASK_BOUND_LIFETIMES and inner.task_id != outer.task_id:
        return False
    if outer.expires_at_utc is not None and (
        inner.expires_at_utc is None or inner.expires_at_utc > outer.expires_at_utc
    ):
        return False
    return True


def hands_on_as_much(outer: GrantV1, inner: GrantV1) -> bool:
    """Whether ``outer`` lets its holder hand on at least what ``inner`` does."""

    if inner.delegation_depth == 0:
        return True
    if outer.delegation_depth < inner.delegation_depth:
        return False
    # Handing on without asking is more than asking each time.
    if outer.delegation_mode is DelegationMode.ASK_OWNER and inner.delegation_mode is DelegationMode.AUTOMATIC:
        return False
    # An empty list allows any recipient.
    if outer.delegate_to and (not inner.delegate_to or not set(inner.delegate_to) <= set(outer.delegate_to)):
        return False
    return True


def grant_covers(outer: GrantV1, inner: GrantV1, tools_in_set: ToolsInSet) -> bool:
    """Whether ``outer`` gives everything ``inner`` gives, to the same holder for the same work."""

    return (
        outer.kind is GrantKind.ACCESS
        and inner.kind is GrantKind.ACCESS
        and outer.subject == inner.subject
        and outer.for_person_id == inner.for_person_id
        and target_covers(outer.target, inner.target, tools_in_set)
        and outer.level.covers(inner.level)
        and lasts_as_long(outer, inner)
        and hands_on_as_much(outer, inner)
    )


@dataclass
class GrantPlan:
    """What storing a batch of new grants does to the holder's existing access."""

    # New grants to store, in order.
    new: list[GrantV1] = field(default_factory=list)
    # Existing grant id -> the new grant id that replaces it.
    replaced: dict[str, str] = field(default_factory=dict)
    # New grant id (as given) -> the existing grant that already covers it.
    covered: dict[str, GrantV1] = field(default_factory=dict)

    def replaced_by(self, grant_id: str) -> list[str]:
        return [old for old, new in self.replaced.items() if new == grant_id]


def plan_grants(new: Sequence[GrantV1], existing: Iterable[GrantV1], tools_in_set: ToolsInSet) -> GrantPlan:
    """Plan a batch against the holders' access in force (see the module docstring).

    ``existing`` must hold only grants in force now. Grants later in the batch
    see earlier ones, so a batch that gives a tool set and one of its tools
    stores only the set.
    """

    plan = GrantPlan()
    current = list(existing)
    for grant in new:
        if grant.kind is not GrantKind.ACCESS or grant.status is not GrantStatus.ACTIVE:
            plan.new.append(grant)
            continue
        cover = next((held for held in current if grant_covers(held, grant, tools_in_set)), None)
        if cover is not None:
            plan.covered[grant.grant_id] = cover
            continue
        narrower = [held for held in current if grant_covers(grant, held, tools_in_set)]
        batch_ids = {item.grant_id for item in plan.new}
        for held in narrower:
            if held.grant_id in batch_ids:
                # Replaced within the batch: never store it, and pass on what it replaced.
                plan.new = [item for item in plan.new if item.grant_id != held.grant_id]
                for old, by in list(plan.replaced.items()):
                    if by == held.grant_id:
                        plan.replaced[old] = grant.grant_id
            else:
                plan.replaced[held.grant_id] = grant.grant_id
        gone = {held.grant_id for held in narrower}
        current = [held for held in current if held.grant_id not in gone] + [grant]
        plan.new.append(grant)
    return plan


__all__ = [
    "EntryKey",
    "GrantPlan",
    "ToolsInSet",
    "entry_key",
    "entry_target",
    "grant_covers",
    "hands_on_as_much",
    "lasting_order",
    "lasts_as_long",
    "plan_grants",
    "target_covers",
]
