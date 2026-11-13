"""Shared setup for the tests of each turn's saved files (``storage.conversation_repository``).

The SQLite tests (tests/test_conversation_turn_attachments.py) and the real
PostgreSQL tests (tests/test_conversation_turn_attachments_postgres.py) run
the same scenarios through these helpers.
"""

from __future__ import annotations

import threading
import time
from typing import Any, Callable

from tests.upgrade_schemas import previous_minor_fixture, read_schema_statements

OWNER = "alice"
TEAM = "team_turn_files"


def attachment_record(attachment_id: str, filename: str = "", *, kind: str = "image") -> dict[str, Any]:
    """A saved file record as trusted runtime code stores it (a ``UserTaskAttachment`` dump)."""

    return {
        "attachment_id": attachment_id,
        "filename": filename or f"{attachment_id}.png",
        "content_type": "image/png" if kind == "image" else "text/plain",
        "kind": kind,
        "size_bytes": 4,
        "sha256": "0" * 64,
        "origin": "web_chat",
        "storage_refs": [{"storage_type": "local", "path": f"/instance/state/uploads/s/{attachment_id}"}],
    }


def add_turn(
    repo: Any, conversation_id: str, task_id: str, attachments: list[dict[str, Any]] | None = None
) -> dict[str, Any]:
    """A person's turn in ``conversation_id`` (the thread is made on first use)."""

    repo.ensure_thread(
        conversation_id=conversation_id,
        team_id=TEAM,
        owner_canonical_user_id=OWNER,
        owner_agent_id="lead",
        owner_agent_name="TeamLead",
        requester_name=OWNER,
        requester_id=OWNER,
        root_task_id=task_id,
    )
    return repo.append_turn(
        conversation_id=conversation_id,
        task_id=task_id,
        followup_to_task_id=None,
        owner_agent_id="lead",
        requester_name=OWNER,
        requester_id=OWNER,
        correlation_id=f"corr_{task_id}",
        problem_text=f"Request {task_id}",
        requester_type="user_req",
        owner_canonical_user_id=OWNER,
        attachments=attachments,
    )


def ids(records: list[dict[str, Any]]) -> list[str]:
    return [record["attachment_id"] for record in records]


def previous_release_turns_table() -> str:
    """The ``conversation_task_links`` table as the previous minor version's releases created it."""

    statements = read_schema_statements(previous_minor_fixture())
    return next(statement for statement in statements if statement.startswith("CREATE TABLE conversation_task_links "))


# How long the concurrent writers wait for each other to start.
MERGE_START_TIMEOUT_SECONDS = 60.0


def merge_from_threads(
    open_repository: Callable[[], Any],
    conversation_id: str,
    task_id: str,
    *,
    writers: int,
    files_each: int,
    join_timeout: float = 120.0,
) -> None:
    """``writers`` threads, each with its own store, add their own files and one file they all share.

    Every thread opens its own store (its own connections), as separate
    processes would, and they start together. The helper cannot hang the test
    run: a writer whose store fails to open, or whose merge fails, records its
    error and breaks the start barrier, so the others stop at once instead of
    waiting for it; the barrier also gives up after
    ``MERGE_START_TIMEOUT_SECONDS``; the threads are daemons; and a writer still
    running after ``join_timeout`` fails the test.
    """

    start = threading.Barrier(writers, timeout=MERGE_START_TIMEOUT_SECONDS)
    errors: list[BaseException] = []

    def write(writer: int) -> None:
        try:
            repo = open_repository()
            start.wait()
            for index in range(files_each):
                repo.merge_turn_attachments(
                    conversation_id=conversation_id,
                    task_id=task_id,
                    attachments=[attachment_record(f"att_w{writer}_{index}"), attachment_record("att_shared")],
                )
        except BaseException as exc:  # reported by the caller's assertion
            errors.append(exc)
            start.abort()  # writers still waiting stop with BrokenBarrierError

    threads = [
        threading.Thread(target=write, args=(writer,), name=f"merge-writer-{writer}", daemon=True)
        for writer in range(writers)
    ]
    for thread in threads:
        thread.start()
    deadline = time.monotonic() + join_timeout
    for thread in threads:
        thread.join(timeout=max(0.0, deadline - time.monotonic()))
    still_running = [thread.name for thread in threads if thread.is_alive()]
    assert not still_running, f"Merge writers still running after {join_timeout} s: {still_running}"
    assert not errors, errors
