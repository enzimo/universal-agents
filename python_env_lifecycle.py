"""Request environment names, use locks, and whole-environment retention.

Locks live outside workspaces so generated code cannot replace a lock while
maintenance uses it. Both tool calls and cleanup hold the same per-environment
lock. Cleanup checks activity only after acquiring it, including after a long
install or script run. Named environments are kept until explicitly deleted.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import stat
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path

from access_control import safe_open
from file_locking import exclusive_file_lock
from runtime_settings.instance_layout import (
    AGENTS_DIRNAME, PERSON_PYTHON_ENVS_DIRNAME, USERS_DIRNAME, InstanceLayout,
)

logger = logging.getLogger(__name__)

REQUEST_ENV_PREFIX = "request-"
REQUEST_ENV_RETENTION_DAYS = 7
MANIFEST_FILENAME = "manifest.json"
REQUEST_DEFAULT_FIELD = "request_default"
LAST_USED_FIELD = "last_used_at"


def request_environment_name(context_id: str) -> str:
    return REQUEST_ENV_PREFIX + hashlib.sha256(context_id.encode("utf-8")).hexdigest()


@contextmanager
def environment_lock(
    layout: InstanceLayout, env_dir: Path, *, cancelled: Callable[[], bool] | None = None,
) -> Iterator[None]:
    relative = env_dir.relative_to(layout.workspaces_dir).as_posix()
    key = hashlib.sha256(relative.encode("utf-8")).hexdigest()
    layout.python_env_locks_dir.mkdir(parents=True, exist_ok=True)
    with exclusive_file_lock(layout.python_env_locks_dir / key, purpose="Python environment", cancelled=cancelled):
        yield


def prune_request_environments(layout: InstanceLayout, *, now: float | None = None) -> int:
    """Remove whole request-default environments unused for seven days.

    Read and remove through descriptor-based safe_open operations. A planted
    link cannot redirect cleanup to another folder. Old environments without
    the explicit default marker, including the old named `default`, stay.
    """

    cutoff = (time.time() if now is None else now) - REQUEST_ENV_RETENTION_DAYS * 86400
    removed = 0
    homes: list[tuple[str, ...]] = [(AGENTS_DIRNAME,)]
    for name, info in _entries(layout, (USERS_DIRNAME,)):
        if stat.S_ISDIR(info.st_mode):
            homes.append((USERS_DIRNAME, name))
    for home in homes:
        parent = (*home, PERSON_PYTHON_ENVS_DIRNAME)
        for name, info in _entries(layout, parent):
            if not name.startswith(REQUEST_ENV_PREFIX):
                continue
            parts = (*parent, name)
            if not stat.S_ISDIR(info.st_mode):
                logger.warning("Python environment cleanup skipped. event=python_env_prune_skipped folder=%s reason=not_a_folder", parts)
                continue
            try:
                with environment_lock(layout, layout.workspaces_dir.joinpath(*parts)):
                    if _expired(layout, parts, cutoff):
                        removed += int(safe_open.remove_directory(layout.workspaces_dir, parts))
            except (OSError, ValueError, TypeError, safe_open.UnsafePath) as exc:
                logger.warning("Python environment cleanup skipped. event=python_env_prune_skipped folder=%s reason=%s", parts, exc)
    logger.info("Request Python environments removed. event=python_env_pruned removed=%s retention_days=%s",
                removed, REQUEST_ENV_RETENTION_DAYS)
    return removed


def _entries(layout: InstanceLayout, parts: tuple[str, ...]) -> list[tuple[str, os.stat_result]]:
    try:
        return safe_open.list_directory(layout.workspaces_dir, parts)
    except safe_open.UnsafePath as exc:
        if exc.reason != safe_open.REASON_NOT_FOUND:
            logger.warning("Python environment cleanup skipped. event=python_env_prune_skipped folder=%s reason=%s", parts, exc)
        return []


def _expired(layout: InstanceLayout, parts: tuple[str, ...], cutoff: float) -> bool:
    try:
        raw = safe_open.read_bytes(layout.workspaces_dir, (*parts, MANIFEST_FILENAME), max_bytes=65536)
    except safe_open.UnsafePath as exc:
        if exc.reason == safe_open.REASON_NOT_FOUND:
            return False
        raise
    manifest = json.loads(raw)
    if not isinstance(manifest, dict) or manifest.get(REQUEST_DEFAULT_FIELD) is not True:
        return False
    last_used = datetime.fromisoformat(manifest.get(LAST_USED_FIELD, ""))
    if last_used.tzinfo is None:
        raise ValueError("Python environment activity has no timezone")
    return last_used.timestamp() < cutoff
