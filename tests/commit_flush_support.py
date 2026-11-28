"""Switch a test to the flush mode an agent process runs in (decision 82).

``strands_main`` calls ``custom_agents.commit_flushes.flush_commits_before_sending``
once per process. Tests change that process-wide state, so they start from a clean
copy through ``monkeypatch``, which restores it afterwards.
"""

from __future__ import annotations

import pytest

from custom_agents import commit_flushes
from custom_transport import send_barrier
from storage import sqlite_connection


def reset_flush_state(monkeypatch: pytest.MonkeyPatch) -> None:
    """No send barrier, no deferral, nothing unflushed and no earlier failed flush."""

    monkeypatch.setattr(send_barrier, "_BEFORE_SEND", None)
    monkeypatch.setattr(sqlite_connection, "_FLUSHES_DEFERRED", False)
    monkeypatch.setattr(sqlite_connection, "_UNFLUSHED", {})
    monkeypatch.setattr(commit_flushes, "_FAILED", None)


def defer_flushes_like_an_agent_process(monkeypatch: pytest.MonkeyPatch) -> None:
    """Start from a clean state, then flush only before sending, as an agent process does."""

    reset_flush_state(monkeypatch)
    commit_flushes.flush_commits_before_sending()
