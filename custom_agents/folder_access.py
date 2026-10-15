"""Folder decisions for agent file use (access plan, Phase 5).

The agent file library (``custom_tools.file_access``) asks here before it
opens a file or folder in a registered root. The answer comes from the access
service (``AccessService.decide_folder``): the agent side, with the task's
person in the context so the agent may use that person's own folder, then the
person side. With authorization off there is no decision (decision D8); the
folder rules and the file library still apply.

A crawl or a Python environment writes many files into one folder in a single
tool call, so allowed answers are kept for a few seconds, keyed by the
folder the file is in, the agent, the task, the person and the access store's
version. Every grant, revocation or block changes that version, so a change
applies to the next file; a grant that ends by time is seen within the
few seconds. An answer that used a one-time grant is never kept, and refusals
are never kept.
"""

from __future__ import annotations

import logging
import threading
import time
from typing import Any

from access_control.decisions import FILES_TOOL_ID
from access_control.folders import folder_chain
from access_control.identity import IdentityRejected
from custom_agents.access_runtime import authorization_enabled, runtime_access, task_access
from custom_messages.contracts.access_v2 import AccessDecisionV1, AccessLevel

logger = logging.getLogger(__name__)

ALLOWED_ANSWER_SECONDS = 10.0
_MAX_KEPT_ANSWERS = 4096
_kept: dict[tuple[Any, ...], float] = {}
_kept_lock = threading.Lock()


def _kept_answer(key: tuple[Any, ...]) -> bool:
    with _kept_lock:
        expires = _kept.get(key)
        if expires is None:
            return False
        if expires <= time.monotonic():
            del _kept[key]
            return False
        return True


def _keep(key: tuple[Any, ...]) -> None:
    with _kept_lock:
        if len(_kept) >= _MAX_KEPT_ANSWERS:
            _kept.clear()
        _kept[key] = time.monotonic() + ALLOWED_ANSWER_SECONDS


def forget_kept_answers() -> None:
    """Drop every kept answer (tests, and after the access store is replaced)."""

    with _kept_lock:
        _kept.clear()


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
    # Files directly in one folder always get the same answer: grants and
    # blocks name folders, never single files.
    folder_key = chain[1] if is_file and len(chain) > 1 else chain[0]
    key = (
        access.team_id,
        access.service.store.version(access.team_id),
        principal.agent_id,
        principal.agent_type,
        principal.role,
        task_id,
        person.person_id if person else None,
        tuple(sorted(person.roles)) if person else (),
        folder_key,
        level,
    )
    if _kept_answer(key):
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
        _keep(key)
    return decision


__all__ = ["ALLOWED_ANSWER_SECONDS", "decide_folder_use", "forget_kept_answers"]
