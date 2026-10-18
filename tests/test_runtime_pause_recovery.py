from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from custom_agents.runtime_recovery import recover_runtime_after_restart
from custom_agents.task_human_interaction_blockers import (
    get_human_interaction_blocker,
    record_human_interaction_blocker,
)
from custom_agents.task_manager import TaskManager
from custom_messages.contracts.routing_v1 import MessageHeader, MessageMeta
from custom_messages.contracts.task_v1 import Task
from custom_messages.enums import (
    MessageMetaType,
    ProgressState,
)
from storage.config import resolve_storage_settings


def _manager(tmp_path: Path) -> TaskManager:
    settings = resolve_storage_settings(
        {"backend": "sqlite", "sqlite": {"path": "runtime/task_history.db"}},
        base_dir=str(tmp_path),
    )
    manager = TaskManager(
        storage_settings=settings,
        logger=logging.getLogger("test.runtime_pause_recovery"),
    )
    manager.set_owner_identity(
        "manager_1",
        "Manager",
        "team_1",
        "TeamArchitect",
    )
    return manager


@pytest.mark.asyncio
async def test_restart_preserves_waiting_task_with_durable_human_interaction_blocker(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manager = _manager(tmp_path)
    task = Task(
        task_id="task_waiting_for_user",
        task_type=MessageMetaType.REQUEST,
        problem_statement="Apply a protected change after approval.",
    )
    assert manager.add_task(task, MessageHeader(from_agent_id="gateway"), MessageMeta(), owner_person_id=None)
    assert manager.get_next_task_to_process() == task.task_id
    manager.update_main_task_state(
        task.task_id,
        ProgressState.WAITING,
        datetime.now(timezone.utc).isoformat(),
    )
    record_human_interaction_blocker(
        manager,
        task.task_id,
        {
            "kind": "access_request",
            "interaction_id": "human_int_exact",
            "access_request_id": "request_exact",
        },
    )

    restored_manager = _manager(tmp_path)
    agent = SimpleNamespace(
        deferred_inbox=SimpleNamespace(
            stored_team_id=AsyncMock(return_value=None),
            restore_role=AsyncMock(return_value=False),
            restore=AsyncMock(return_value=False),
        ),
        id="manager_1",
        team_id="team_1",
        named_agent_type="TeamArchitect",
        same_role_runtime_takeover_enabled=True,
        task_manager=restored_manager,
        _agent_state_store=None,
        _logger=logging.getLogger("test.runtime_human_interaction_recovery"),
    )

    await recover_runtime_after_restart(agent)

    restored = restored_manager.get_task_info(task.task_id)
    assert restored is not None
    assert restored.state == ProgressState.WAITING
    assert get_human_interaction_blocker(restored_manager, task.task_id) == {
        "kind": "access_request",
        "interaction_id": "human_int_exact",
        "access_request_id": "request_exact",
    }
    assert restored_manager.has_queued_top_level_tasks() is False


@pytest.mark.asyncio
async def test_overdue_waiting_worker_is_not_probed_on_every_idle_tick(tmp_path, monkeypatch):
    from tests.test_turn_action_outbox import _make_agent
    from custom_messages.contracts.task_v1 import TaskStatus

    manager = _manager(tmp_path)
    root = Task(task_id="root", task_type=MessageMetaType.REQUEST, problem_statement="Wait for delegated work")
    manager.add_task(root, MessageHeader(from_agent_id="gateway"), MessageMeta(), owner_person_id=None)
    child = Task(task_id="child", task_type=MessageMetaType.REQUEST, parent_task_id=root.task_id)
    manager.add_subtask(root.task_id, child, MessageHeader(to_routing_path="worker"), MessageMeta())
    stale = (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat()
    manager.update_main_task_state(child.task_id, ProgressState.WAITING, stale)
    agent = _make_agent(tmp_path, task_manager=manager, subtask_timeout_seconds=1200)
    probe = AsyncMock(return_value=TaskStatus(
        status_type=MessageMetaType.RESPONSE, task_id=child.task_id, is_known_task=True, task_state=ProgressState.WAITING,
    ))
    object.__setattr__(agent, "_probe_subtask_task_status", probe)
    await agent._recover_stalled_subtasks()
    history_count = len(manager.get_task_history_entries(root.task_id, limit=None))
    for _ in range(4):
        await agent._recover_stalled_subtasks()
    assert probe.await_count == 1
    assert len(manager.get_task_history_entries(root.task_id, limit=None)) == history_count
    assert manager.get_task_info(child.task_id).task.task_state[-1][1] == stale

    restored = _manager(tmp_path)
    restored.restore_runtime_state()
    replacement = _make_agent(tmp_path, task_manager=restored, subtask_timeout_seconds=1200)
    object.__setattr__(replacement, "_probe_subtask_task_status", probe)
    await replacement._recover_stalled_subtasks()
    assert probe.await_count == 1
    # Another probe is still due at the next configured threshold. Failure to
    # answer that probe does not cause a retry on every following idle tick.
    from custom_agents import subtask_watchdog_runtime
    from unittest.mock import Mock

    clock = Mock()
    clock.now.return_value = datetime.now(timezone.utc) + timedelta(seconds=301)
    monkeypatch.setattr(subtask_watchdog_runtime, "datetime", clock)
    probe.return_value = None
    await replacement._recover_stalled_subtasks()
    await replacement._recover_stalled_subtasks()
    assert probe.await_count == 2
