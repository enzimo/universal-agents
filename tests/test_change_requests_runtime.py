"""A person's requests are opened in the change ledger when admitted and closed when they end.

Real signed task contexts (the test gateway and scheduler keys), the real
identity check (``access_runtime.resolve_meta_identity``) and a real SQLite
ledger; the agent is a stand-in that holds its task records.
"""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from access_control.identity import ISSUER_SCHEDULER, ContextSigner
from custom_agents.change_requests_runtime import close_ended_request, open_admitted_request, register_change_requests
from custom_messages.contracts.routing_v1 import GatewayContext
from custom_messages.enums import ProgressState
from storage.config import resolve_storage_settings
from storage.sqlite_connection import connect_sqlite
from tests.broker_access_support import _scheduler_key, context_signer, forget_task_context_keys, use_scheduler_key

TEAM = "team_test"


@pytest.fixture(autouse=True)
def _keys(monkeypatch: pytest.MonkeyPatch):
    use_scheduler_key(monkeypatch)
    yield
    forget_task_context_keys()


class _TaskManager:
    def __init__(self, db_path: Path) -> None:
        self.storage_settings = resolve_storage_settings({"backend": "sqlite", "sqlite": {"path": str(db_path)}})
        self.tasks: dict[str, Any] = {}
        self.terminal_callbacks: list[Any] = []

    def get_task_info(self, task_id: str) -> Any:
        return self.tasks.get(task_id)

    def add_task_terminal_callback(self, callback: Any) -> None:
        self.terminal_callbacks.append(callback)


class _Agent:
    team_id = TEAM
    id = "agent_lead"
    authorization_config = {"enabled": False}
    instance_root = ""

    def __init__(self, db_path: Path) -> None:
        self.task_manager = _TaskManager(db_path)
        register_change_requests(self)

    def admit(self, task_id: str, signed: Any, *, parent: str | None = None) -> Any:
        meta = SimpleNamespace(gateway_context=GatewayContext(source="test", task_context=signed))
        self.task_manager.tasks[task_id] = SimpleNamespace(task=SimpleNamespace(parent_task_id=parent), meta=meta)
        asyncio.run(open_admitted_request(self, SimpleNamespace(task_id=task_id, parent_task_id=parent), meta))
        return signed

    def end(self, task_id: str, state: ProgressState) -> None:
        for callback in self.task_manager.terminal_callbacks:
            callback(task_id, state, "2026-10-19T00:00:00+00:00")


def rows(db_path: Path) -> dict[str, str]:
    with connect_sqlite(str(db_path)) as conn:
        return {str(r[0]): str(r[1]) for r in conn.execute("SELECT request_id, state FROM change_requests").fetchall()}


def test_a_persons_request_opens_when_admitted_and_ends_with_its_first_task(tmp_path, caplog):
    agent = _Agent(tmp_path / "manager.db")
    signed = context_signer().issue(team_id=TEAM, person_id="alice", person_roles=("member",))

    with caplog.at_level(logging.INFO, logger="custom_agents.change_requests_runtime"):
        agent.admit("task_root", signed)
        assert rows(tmp_path / "manager.db") == {signed.context.context_id: "open"}
        # A delegated task carries the same request; it neither opens nor closes it.
        agent.admit("task_child", signed, parent="task_root")
        agent.end("task_child", ProgressState.COMPLETED)
        assert rows(tmp_path / "manager.db") == {signed.context.context_id: "open"}
        agent.end("task_root", ProgressState.COMPLETED)

    assert rows(tmp_path / "manager.db") == {signed.context.context_id: "ended"}
    assert "event=change_ledger_request_opened" in caplog.text
    assert "event=change_ledger_request_closed" in caplog.text


def test_a_stopped_request_is_closed_as_stopped(tmp_path):
    agent = _Agent(tmp_path / "manager.db")
    signed = agent.admit("task_root", context_signer().issue(team_id=TEAM, person_id="alice", person_roles=("member",)))

    agent.end("task_root", ProgressState.CANCELLED)

    assert rows(tmp_path / "manager.db") == {signed.context.context_id: "stopped"}


def test_scheduled_work_and_unsigned_tasks_have_no_request_row(tmp_path):
    agent = _Agent(tmp_path / "manager.db")
    scheduled = ContextSigner.from_value(ISSUER_SCHEDULER, _scheduler_key()[0]).issue(
        team_id=TEAM, person_id="alice", person_roles=("member",), schedule_id="sched_1"
    )

    agent.admit("task_scheduled", scheduled)
    agent.admit("task_unsigned", None)
    agent.end("task_scheduled", ProgressState.COMPLETED)
    agent.end("task_unsigned", ProgressState.COMPLETED)

    assert rows(tmp_path / "manager.db") == {}


def test_a_failed_close_is_logged_and_the_task_still_ends(tmp_path, caplog):
    agent = _Agent(tmp_path / "manager.db")
    agent.admit("task_root", context_signer().issue(team_id=TEAM, person_id="alice", person_roles=("member",)))

    def broken(**_kwargs: Any) -> None:
        raise RuntimeError("database unavailable")

    agent._change_request_store.close = broken
    with caplog.at_level(logging.ERROR, logger="custom_agents.change_requests_runtime"):
        close_ended_request(agent, "task_root", ProgressState.COMPLETED)

    assert "event=change_ledger_request_close_failed" in caplog.text
