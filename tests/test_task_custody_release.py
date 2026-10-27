"""An agent stops running a task another agent took over (custom_agents.custody_loss).

The snapshot coordinator drops a taken-over task at the old holder's next save
(tests/test_task_custody.py); these tests cover what the agent does next: stop
the running turn as a transfer and send no failure reply for the task.
"""

from __future__ import annotations

import asyncio
import logging
import threading
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from custom_tools.runtime_context import TaskWork
from custom_agents.control_runtime import consume_handback_interrupt, handle_interrupted_turn
from custom_agents.custody_loss import custody_moved, install_custody_listeners
from custom_agents.sdk.strands.turn_tool_stop import CUSTODY_LOST_REASON
from custom_agents.turn_failure_runtime import fail_task_after_turn_error


def _agent(running_task_id: str | None, held: set[str]) -> SimpleNamespace:
    listener: dict[str, object] = {}
    manager = SimpleNamespace(
        set_custody_lost_listener=lambda callback: listener.update(callback=callback),
        get_task_info=lambda task_id: object() if task_id in held else None,
        fail_task=AsyncMock(),
    )
    return SimpleNamespace(
        id="worker_a",
        _logger=logging.getLogger("custody-release-test"),
        task_manager=manager,
        deferred_inbox=SimpleNamespace(on_custody_lost=None),
        # The turn running now (custom_agents.turn_registry), by its asyncio task.
        _running_turns={object(): TaskWork(task_id=running_task_id)},
        listener=listener,
    )


@pytest.mark.asyncio
async def test_a_released_running_turn_is_stopped_as_a_transfer_without_a_reply():
    agent = _agent("taken", held={"kept"})
    stops: list[tuple[str, set[str]]] = []

    async def interrupt_turn(_agent, *, root_task_id, graph_task_ids, reason):
        stops.append((root_task_id, set(graph_task_ids), reason))
        return asyncio.current_task()

    install_custody_listeners(agent, interrupt_turn=interrupt_turn)
    # The coordinator calls the listener on the storage thread.
    caller = threading.Thread(target=agent.listener["callback"], args=(("taken",),))
    caller.start()
    caller.join()
    for _ in range(20):
        await asyncio.sleep(0)
        if stops:
            break
    await agent._custody_release.interrupt

    assert stops == [("taken", {"taken"}, CUSTODY_LOST_REASON)]
    # The stopped turn is a transfer: no cancellation record, consumed once.
    assert consume_handback_interrupt(agent, "taken")
    assert not consume_handback_interrupt(agent, "taken")
    # A late failure of the released task sends nothing; a task this agent holds fails as usual.
    assert custody_moved(agent, "taken", RuntimeError("task record missing"))
    assert not custody_moved(agent, "kept", RuntimeError("boom"))
    agent._conversation_store = None
    await fail_task_after_turn_error(agent, "taken", None, RuntimeError("task record missing"))
    agent.task_manager.fail_task.assert_not_called()


@pytest.mark.asyncio
async def test_an_interrupted_released_turn_records_no_cancellation():
    agent = _agent("taken", held=set())
    install_custody_listeners(agent, interrupt_turn=AsyncMock(return_value=None))
    agent._custody_release.interrupting_task_ids.add("taken")
    agent._cancel_task_tree_locally = AsyncMock()
    await handle_interrupted_turn(agent, task_id="taken", task_info=None)
    agent._cancel_task_tree_locally.assert_not_called()


@pytest.mark.asyncio
async def test_a_task_this_agent_holds_again_is_its_own():
    """A redelivered request or a handed-back task it picked up again fails as usual."""
    agent = _agent(None, held=set())
    install_custody_listeners(agent, interrupt_turn=AsyncMock(return_value=None))
    agent.listener["callback"](("t1",))
    await asyncio.sleep(0)
    assert custody_moved(agent, "t1", RuntimeError())
    agent.task_manager.get_task_info = lambda task_id: object()
    assert not custody_moved(agent, "t1", RuntimeError())
    agent.task_manager.get_task_info = lambda task_id: None
    assert not custody_moved(agent, "t1", RuntimeError())  # it left the released set


def test_a_released_task_withdraws_its_access_requests_but_keeps_the_request_access(monkeypatch):
    """Regression (2026-10-17 review round 3): a task another agent took over kept its
    access request open in the person's Inbox, so the person could be asked twice.
    The request this agent's run waited on is withdrawn; access bound to the person's
    request stays, because the task goes on with its new holder."""
    from custom_agents import access_runtime

    withdrawn: list[tuple[str, str]] = []
    service = SimpleNamespace(
        store=SimpleNamespace(waiting_for_task=lambda team, agent_id, task_id: [SimpleNamespace(request_id="req_1")]),
        end_task_grants=lambda ids: pytest.fail("a released task must not end the request's access"),
    )
    requests = SimpleNamespace(service=service, withdraw=lambda request_id, why: withdrawn.append((request_id, why)))
    monkeypatch.setattr(access_runtime, "runtime_requests", lambda _agent: requests)
    released: list = []
    manager = SimpleNamespace(
        add_task_terminal_callback=lambda _callback: None, add_task_release_callback=released.append,
    )
    agent = SimpleNamespace(id="worker_1", team_id="team", is_team_lead=False, task_manager=manager)

    access_runtime.register_task_access_cleanup(agent)
    (callback,) = released
    callback("T")

    assert [request_id for request_id, _why in withdrawn] == ["req_1"]
    assert "moved to another agent" in withdrawn[0][1]
