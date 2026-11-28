"""Folder decisions for agent file use (access plan, Phase 5).

The agent file library (``custom_tools.file_access``) asks here before it
opens a file or folder in a registered root. The answer comes from the access
service (``AccessService.decide_folder``): the agent side, with the task's
person in the context so the agent may use that person's own folder, then the
person side. With authorization off there is no decision (decision D8); the
folder rules and the file library still apply.

A crawl or a Python environment writes many files into one folder in a single
tool call, so allowed answers are kept for a few seconds, keyed by the team,
the access store's version, the agent identity (including work and schedule
ids), the task, the person, the level, the scope (``folder`` or ``file``) and
the target id:

- A whole-folder answer (``folder`` scope) is kept only when
  ``AccessService.reusable_folder_allowance`` confirms both sides allow the
  whole folder at that level without one-time grants, together with the set of
  blocked direct-child paths excluded from reuse. A file lookup hits the folder
  entry only when its path is not in that excluded set; a blocked file falls
  through to Cedar and is refused.
- A file-only allowance (such as a decision D15 single-file share) is kept only
  under its exact ``file`` key, never under its parent folder's key, so a
  shared file never opens sibling files or its parent folder's listing.
- Store-version invariant: the store version is read once, before the lookup,
  and is part of every key. Every grant, revocation, block or hand-on strictly
  increments that version, so any answer computed before or during a store
  change is kept under an older version that later lookups can never hit. A
  grant that ends by time is seen within the few seconds. An answer that used a
  one-time grant is never kept, and refusals are never kept.
"""

from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass
from typing import Any

from access_control.decisions import FILES_TOOL_ID
from access_control.folders import folder_chain
from access_control.identity import IdentityRejected
from custom_agents.access_runtime import authorization_enabled, runtime_access, task_access
from custom_messages.contracts.access_v2 import AccessDecisionV1, AccessLevel

logger = logging.getLogger(__name__)

ALLOWED_ANSWER_SECONDS = 10.0
_MAX_KEPT_ANSWERS = 4096
_SCOPE_FOLDER = "folder"
_SCOPE_FILE = "file"


@dataclass(frozen=True)
class _KeptAnswer:
    expires: float
    blocked_children: frozenset[str] = frozenset()


_kept: dict[tuple[Any, ...], _KeptAnswer] = {}
_kept_lock = threading.Lock()


def _kept_entry(key: tuple[Any, ...]) -> _KeptAnswer | None:
    with _kept_lock:
        entry = _kept.get(key)
        if entry is None:
            return None
        if entry.expires <= time.monotonic():
            del _kept[key]
            return None
        return entry


def _keep(key: tuple[Any, ...], blocked_children: frozenset[str] = frozenset()) -> None:
    with _kept_lock:
        if len(_kept) >= _MAX_KEPT_ANSWERS:
            _kept.clear()
        _kept[key] = _KeptAnswer(
            expires=time.monotonic() + ALLOWED_ANSWER_SECONDS,
            blocked_children=blocked_children,
        )


def forget_kept_answers() -> None:
    """Drop every kept answer (tests, and after the access store is replaced)."""

    with _kept_lock:
        _kept.clear()


def _is_kept(key_prefix: tuple[Any, ...], chain: list[str], *, is_file: bool) -> bool:
    if not is_file:
        return _kept_entry((*key_prefix, _SCOPE_FOLDER, chain[0])) is not None
    if len(chain) > 1:
        folder_entry = _kept_entry((*key_prefix, _SCOPE_FOLDER, chain[1]))
        if folder_entry is not None and chain[0] not in folder_entry.blocked_children:
            return True
    return _kept_entry((*key_prefix, _SCOPE_FILE, chain[0])) is not None


def _store_allowed(
    service: Any,
    key_prefix: tuple[Any, ...],
    principal: Any,
    person: Any,
    chain: list[str],
    *,
    is_file: bool,
    level: AccessLevel,
    decision: AccessDecisionV1,
) -> None:
    if is_file and len(chain) == 1:
        _keep((*key_prefix, _SCOPE_FILE, chain[0]))
        return
    folder_target = chain[1] if is_file else chain[0]
    blocked = service.reusable_folder_allowance(
        agent=principal,
        person=person,
        folder_id=folder_target,
        level=level,
        task_id=principal.work_id,
        folder_decision=None if is_file else decision,
    )
    if blocked is not None:
        _keep((*key_prefix, _SCOPE_FOLDER, folder_target), blocked)
    elif is_file:
        _keep((*key_prefix, _SCOPE_FILE, chain[0]))


def decide_folder_use(agent: Any, target_id: str, *, is_file: bool, level: AccessLevel) -> AccessDecisionV1 | None:
    """The decision for one file or folder use, or None with authorization off."""

    # The switch is read first: with authorization off nothing else is needed.
    if not authorization_enabled(getattr(agent, "authorization_config", None)):
        return None
    access = runtime_access(agent)
    assert access.service is not None
    operation = f"{'file' if is_file else 'folder'}.{level.value}"
    try:
        principal, person, task_id = task_access(agent, access)
    except (IdentityRejected, ValueError) as exc:
        return AccessDecisionV1(
            allowed=False,
            tool_id=FILES_TOOL_ID,
            operation=operation,
            level=level,
            folder_id=target_id,
            reason=f"Nobody verifiably asked for this task ({exc}).",
            next_step="Tell the person; their request needs to come through the gateway again.",
        )
    chain = folder_chain(target_id)
    version = access.service.store.version(access.team_id)
    key_prefix = (
        access.team_id,
        version,
        principal.agent_id,
        principal.agent_type,
        principal.role,
        principal.work_id,
        principal.schedule_id,
        task_id,
        person.person_id if person else None,
        tuple(sorted(person.roles)) if person else (),
        level,
    )
    if _is_kept(key_prefix, chain, is_file=is_file):
        return AccessDecisionV1(
            allowed=True, tool_id=FILES_TOOL_ID, operation=operation, level=level, folder_id=chain[0], reason="allowed"
        )
    decision = access.service.decide_folder(
        agent=principal,
        person=person,
        target_id=chain[0],
        is_file=is_file,
        level=level,
        # Access for one task is bound to the request the task works on.
        task_id=principal.work_id,
        hand_on_for_task=True,
    )
    if decision.allowed and not decision.consumed_grant_ids:
        _store_allowed(
            access.service,
            key_prefix,
            principal,
            person,
            chain,
            is_file=is_file,
            level=level,
            decision=decision,
        )
    return decision


__all__ = ["ALLOWED_ANSWER_SECONDS", "decide_folder_use", "forget_kept_answers"]
