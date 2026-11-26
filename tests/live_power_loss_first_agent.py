"""First agent of the timed-wait power-loss tests, run in its own OS process.

``tests/test_live_timed_wait_power_loss.py`` starts this script, waits for its
ready line and then kills it with SIGKILL, so the "power loss" is a real
process death: nothing of this agent (turn tasks, timers, presence or status
writers) can run or write afterwards. An in-process stand-in that cancelled
only the agent's main task left background work running, which wrote this
agent's status row with the next process's identity and made the takeover
test flaky (project review F2).

The script starts a real ``UniversalAgent`` on the given NATS server and
SQLite database, sends it the request, waits until the task is ``WAITING``
with its ``timed_wait`` record and the worker slot is free, prints one
``READY <json>`` line naming this process's identity, and then waits to be
killed. On any failure it exits non-zero with the traceback on stderr.

On release/0.10 the process first defers its SQLite flushes until it sends,
as ``strands_main`` does (``custom_agents.commit_flushes``, decision 82), so
the pause runs as in production and only the pause flush puts it on the disk.
SIGKILL keeps the operating system's page cache, so the kill alone cannot
lose unflushed commits; instead READY reports whether any commit of the task
runtime store is still unflushed (``_count_task_runtime_commits``), and the
test requires none (review F1). The status row and conversation share the
database file and the heartbeat writes the status row after the pause, so
"anything unflushed" would say nothing about the pause.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import sys
import threading
from pathlib import Path
from unittest.mock import patch

READY_PREFIX = "READY "
# How long READY waits for the pause flush after WAITING shows (the flush runs just after).
PAUSE_FLUSH_WAIT_SECONDS = 3.0


def _count_task_runtime_commits() -> dict[str, int]:
    """Count the task runtime store's commits and how many of them a finished flush covered.

    Wraps ``sqlite_connection._note_unflushed_commit`` (called for every deferred
    commit) and ``sqlite_connection.flush_unflushed_commits`` (the send barrier's
    and the pause's flush). A commit counts when it is made from
    ``storage/runtime_state_repository.py``. A flush covers what was counted
    before it started, so a commit made during a flush still counts as unflushed.
    """

    from storage import sqlite_connection

    counts = {"written": 0, "flushed": 0}
    lock = threading.Lock()
    marker = os.path.join("storage", "runtime_state_repository.py")
    note_commit = sqlite_connection._note_unflushed_commit
    flush_commits = sqlite_connection.flush_unflushed_commits

    def counting_note(path: str) -> None:
        frame = sys._getframe(1)
        while frame is not None:
            if frame.f_code.co_filename.endswith(marker):
                with lock:
                    counts["written"] += 1
                break
            frame = frame.f_back
        note_commit(path)

    def counting_flush(**kwargs: object) -> None:
        with lock:
            covered = counts["written"]
        flush_commits(**kwargs)  # type: ignore[arg-type]
        with lock:
            counts["flushed"] = max(counts["flushed"], covered)

    sqlite_connection._note_unflushed_commit = counting_note
    sqlite_connection.flush_unflushed_commits = counting_flush
    return counts


async def _run(args: dict[str, str]) -> None:
    from custom_agents.commit_flushes import flush_commits_before_sending
    from storage import sqlite_connection

    # As strands_main does, before any store opens; then count the task runtime's commits.
    flush_commits_before_sending()
    task_runtime_commits = _count_task_runtime_commits()

    from custom_agents import timed_waits as timed_waits_rules
    from custom_agents.task_human_interaction_blockers import get_human_interaction_blocker
    from custom_messages.enums import ProgressState
    from custom_tools.runtime_context import set_agent_ref
    from live_nats_client import LiveNatsClient
    from runtime_hosting.process_run import current_process_identity
    from runtime_settings.transport import TransportSettings
    from test_live_multi_agent_state_boundaries import _start_agent, _wait_for
    from test_live_timed_wait_power_loss import (
        BASE_TIME,
        _power_loss_agent,
        _PowerLossWaitModel,
        _request_envelope,
    )

    nats_url = args["nats_url"]
    agent_id = args["agent_id"]
    task_id = args["task_id"]
    model = _PowerLossWaitModel(reason=args["reason"], should_call_wait=True)
    transport = TransportSettings(nats_url=nats_url, nats_servers=[nats_url], storage="file")
    with (
        patch.object(timed_waits_rules, "utc_now", return_value=BASE_TIME),
        patch("custom_agents.strands_agent.UniversalAgent._configure_model", return_value=model),
    ):
        agent = _power_loss_agent(
            agent_id=agent_id,
            team_id=args["team_id"],
            named_agent_type=args["named_agent_type"],
            db_path=Path(args["db_path"]),
            workspace_dir=Path(args["workspace"]),
            nats_url=nats_url,
            same_role_takeover=True,
        )
        set_agent_ref(agent)
        async with LiveNatsClient(args["team_id"], transport=transport, servers=[nats_url]) as client:
            await _start_agent(agent)
            await client.publish(agent_id, _request_envelope(task_id, agent_id, args["reply_route"]))
            tasks = agent.task_manager
            await _wait_for(
                lambda: (
                    (info := tasks.get_task_info(task_id)) is not None
                    and info.state == ProgressState.WAITING
                    and (get_human_interaction_blocker(tasks, task_id) or {}).get("kind") == "timed_wait"
                    and tasks.task_state.current_in_progress_task_id is None
                ),
                timeout=20.0,
            )
            if model.wait_calls != 1:
                raise AssertionError(f"expected one wait call, got {model.wait_calls}")
            # The pause flush runs right after WAITING is saved; give it a moment. Without it
            # nothing flushes while the task waits (it sends nothing), and READY says so.
            try:
                await _wait_for(
                    lambda: task_runtime_commits["written"] <= task_runtime_commits["flushed"],
                    timeout=PAUSE_FLUSH_WAIT_SECONDS,
                )
            except AssertionError:
                pass  # reported as task_runtime_unflushed below
            ready = {
                "process_identity": current_process_identity().text(),
                "wait_calls": model.wait_calls,
                "flushes_deferred": sqlite_connection.flushes_deferred(),
                "task_runtime_commits": task_runtime_commits["written"],
                "task_runtime_unflushed": task_runtime_commits["written"] > task_runtime_commits["flushed"],
            }
            print(READY_PREFIX + json.dumps(ready), flush=True)
            # Keep running (presence, scheduler, status writes) until the test kills this process.
            await asyncio.Event().wait()


def main() -> None:
    logging.basicConfig(level=logging.WARNING, stream=sys.stderr)
    asyncio.run(_run(json.loads(sys.argv[1])))


if __name__ == "__main__":
    main()
