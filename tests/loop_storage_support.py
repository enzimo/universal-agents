"""Record SQLite opens made on a thread that is running an asyncio event loop.

The gateway's one loop also runs Discord, Telegram and every web request, so a
database call made on it stops all of them (website_handlers.off_loop). Tests
use ``record_loop_opens`` around the requests whose database work must run in
worker threads, then assert ``where`` is empty; each entry names the innermost
callers above the storage layer.
"""

from __future__ import annotations

import asyncio
import sqlite3
import traceback
from contextlib import contextmanager
from typing import Iterator

import pytest

_REPO_MARK = "/universal-agents/"


class LoopOpens:
    def __init__(self) -> None:
        self.where: list[str] = []

    def report(self) -> str:
        return "Database opened on the event loop:\n" + "\n".join(self.where)


@contextmanager
def record_loop_opens(monkeypatch: pytest.MonkeyPatch) -> Iterator[LoopOpens]:
    opens = LoopOpens()
    real = sqlite3.connect

    def connect(*args, **kwargs):
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            return real(*args, **kwargs)
        frames = [
            f"{frame.filename.rsplit(_REPO_MARK, 1)[-1]}:{frame.lineno} {frame.name}"
            for frame in traceback.extract_stack()[:-1]
            if _REPO_MARK in frame.filename
            and not any(part in frame.filename for part in ("/tests/", "/.venv/", "/storage/"))
        ]
        opens.where.append(" <- ".join(reversed(frames[-6:])))
        return real(*args, **kwargs)

    monkeypatch.setattr(sqlite3, "connect", connect)
    try:
        yield opens
    finally:
        monkeypatch.setattr(sqlite3, "connect", real)
