"""A previous owner that is still running releases work another agent took over.

Real SQLite snapshot stores and two real task managers sharing them, as two
agent processes would. Before 2026-10-17 the previous owner's next save wrote
its row again, so two agents held and ran the same task (probe: the replacement
and the old startup agent both held ``work``, and a later merge kept the older
copy). Now its save is refused, it forgets the work without saving or replying,
and it keeps taking new work.
"""

from __future__ import annotations

import asyncio
import logging
from functools import partial
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from custom_agents.control_runtime import _interrupt_active_execution, consume_handback_interrupt
from custom_agents.custody_loss import custody_moved, install_custody_listeners
from custom_agents.deferred_message_runtime import DeferredInboxOwner, DeferredMessageInbox
from custom_agents.task_manager import TaskManager
from custom_agents.turn_failure_runtime import fail_task_after_turn_error
from custom_messages.contracts.routing_v1 import MessageHeader, MessageMeta
from custom_messages.contracts.runtime_snapshot_v3 import decode_runtime_snapshot
from custom_messages.contracts.task_v1 import Task
from custom_messages.enums import MessageMetaType, ProgressState
from storage.runtime_state_repository import CustodyStore, RuntimeCustodyLost, RuntimeRecoveryConflict
from tests.test_deferred_message_inbox import _envelope

OLD, NEW = "startup_old", "replacement"


def manager(tmp_path, owner: str) -> TaskManager:
    task_manager = TaskManager(db_path=str(tmp_path / "runtime.db"))
    task_manager.set_owner_identity(
        agent_id=owner, agent_name=owner, team_id="team", named_agent_type="ResearchAgent"
    )
    return task_manager


def admit(task_manager: TaskManager, task_id: str) -> None:
    assert task_manager.add_task(
        Task(task_id=task_id, task_type=MessageMetaType.REQUEST, problem_statement="Compare reports"),
        MessageHeader(),
        MessageMeta(),
        owner_person_id=None,
    )


def holders(task_manager: TaskManager) -> dict[str, list[str]]:
    rows = task_manager.runtime_state_store.peek_task_runtime_by_team("team")
    return {
        row.owner_agent_id: sorted(t.task_id for t in decode_runtime_snapshot(row.runtime_state_json).tasks)
        for row in rows
    }


def now() -> str:
    return datetime.now(timezone.utc).isoformat()


def test_the_coordinator_tracks_whether_it_holds_a_saved_row(tmp_path):
    old = manager(tmp_path, OLD)
    coordinator = old.runtime_snapshot_coordinator
    assert coordinator._holds_custody is False
    admit(old, "work")
    assert coordinator._holds_custody is True
    old.remove_task("work")
    assert coordinator._holds_custody is False and holders(old) == {}
    admit(old, "work")
    new = manager(tmp_path, NEW)
    new.restore_runtime_state_for_owner_ids([OLD])
    assert new.runtime_snapshot_coordinator._holds_custody is True


def test_a_still_running_previous_owner_cannot_duplicate_a_task_after_takeover(tmp_path, caplog):
    old = manager(tmp_path, OLD)
    admit(old, "work")
    new = manager(tmp_path, NEW)
    new.restore_runtime_state_for_owner_ids([OLD])
    released: list[tuple[str, ...]] = []
    old.set_custody_lost_listener(released.append)

    # The old owner keeps working; its next state change finds its row gone.
    with caplog.at_level(logging.WARNING), pytest.raises(RuntimeCustodyLost):
        old.update_main_task_state("work", ProgressState.IN_PROGRESS, now())
    assert released == [("work",)]
    assert old.list_task_infos() == []
    assert holders(old) == {NEW: ["work"]}
    assert any("event=runtime_custody_lost" in record.getMessage() for record in caplog.records)

    # It keeps taking new work, saved in a new row of its own.
    admit(old, "next")
    assert holders(old) == {NEW: ["work"], OLD: ["next"]}

    # A later takeover of both finds no task held twice.
    later = manager(tmp_path, "later")
    caplog.clear()
    with caplog.at_level(logging.WARNING):
        later.restore_runtime_state_for_owner_ids([OLD, NEW])
    assert sorted(info.task.task_id for info in later.list_task_infos()) == ["next", "work"]
    assert not [r for r in caplog.records if getattr(r, "reason", None) == "duplicate"]


def test_a_history_write_without_a_saved_row_is_not_custody_loss(tmp_path):
    old = manager(tmp_path, OLD)
    admit(old, "work")
    old.runtime_state_store.clear_task_runtime(OLD)
    old.runtime_snapshot_coordinator._holds_custody = False
    with pytest.raises(RuntimeRecoveryConflict) as refused:
        old.runtime_snapshot_coordinator.persist_task_history("work")
    assert not isinstance(refused.value, RuntimeCustodyLost)
    # So a turn failing on it is failed and answered as usual.
    assert not custody_moved(SimpleNamespace(_custody_release=None), "work", refused.value)
    assert [info.task.task_id for info in old.list_task_infos()] == ["work"]


def agent_for(task_manager: TaskManager, inbox: DeferredMessageInbox) -> SimpleNamespace:
    return SimpleNamespace(
        id=OLD,
        task_manager=task_manager,
        deferred_inbox=inbox,
        team_comms=SimpleNamespace(send_message=AsyncMock()),
        _send_task_error_response=AsyncMock(),
        _logger=logging.getLogger("test.custody_loss"),
        _strands_agent=None,
        _active_execution_task=None,
        _active_execution_task_id=None,
        _custody_release=None,
        _lease_loss_wind_down=None,
    )


def inbox_for(owner: str, task_manager: TaskManager) -> DeferredMessageInbox:
    return DeferredMessageInbox(
        owner=DeferredInboxOwner(owner, owner, "team", "ResearchAgent"),
        repository=task_manager.runtime_state_store,
        is_execution_cancelled=AsyncMock(return_value=False),
        logger=logging.getLogger("test.custody_loss.inbox"),
    )


@pytest.mark.asyncio
async def test_custody_loss_stops_the_running_turn_without_a_handback_or_a_failure_reply(tmp_path):
    old = manager(tmp_path, OLD)
    agent = agent_for(old, inbox_for(OLD, old))
    install_custody_listeners(agent, interrupt_turn=_interrupt_active_execution)
    await old.operations_runtime.run(partial(admit, old, "work"))
    task_info = old.get_task_info("work")
    turn = asyncio.create_task(asyncio.sleep(60))
    agent._active_execution_task, agent._active_execution_task_id = turn, "work"
    manager(tmp_path, NEW).restore_runtime_state_for_owner_ids([OLD])

    with pytest.raises(RuntimeCustodyLost):
        await old.operations_runtime.run(old.update_main_task_state, "work", ProgressState.IN_PROGRESS, now())
    await asyncio.sleep(0)
    await agent._custody_release.interrupt
    assert turn.cancelled()
    # The interrupted turn is a transfer: no cancellation record, consumed once.
    assert consume_handback_interrupt(agent, "work") is True
    assert consume_handback_interrupt(agent, "work") is False

    # A turn of the released task that fails afterwards is not failed or answered here.
    await fail_task_after_turn_error(agent, "work", task_info, RuntimeError("provider timed out"))
    agent._send_task_error_response.assert_not_awaited()
    agent.team_comms.send_message.assert_not_awaited()
    assert holders(old) == {NEW: ["work"]}


def test_a_failure_caused_by_lost_custody_is_recognised_through_wrappers():
    agent = SimpleNamespace(_custody_release=None)
    try:
        try:
            raise RuntimeCustodyLost(OLD, CustodyStore.TASK_RUNTIME)
        except RuntimeCustodyLost as lost:
            raise RuntimeError("tool failed") from lost
    except RuntimeError as wrapped:
        assert custody_moved(agent, "other", wrapped)
    assert not custody_moved(agent, "other", RuntimeError("unrelated"))


@pytest.mark.asyncio
async def test_the_deferred_inbox_refuses_to_recreate_taken_over_messages(tmp_path):
    old_manager = manager(tmp_path, OLD)
    old_inbox = inbox_for(OLD, old_manager)
    released: list[int] = []
    old_inbox.on_custody_lost = released.append
    await old_inbox.admit(_envelope("first"))
    new_inbox = inbox_for(NEW, old_manager)
    assert await new_inbox.restore_owners([OLD])

    with pytest.raises(RuntimeCustodyLost):
        await old_inbox.admit(_envelope("second"))
    assert released == [1] and len(old_inbox) == 0
    repository = old_manager.runtime_state_store
    assert repository.load_deferred_messages_by_owner(OLD) == []
    assert len(repository.load_deferred_messages_by_owner(NEW)) == 1

    # The refused message was never accepted; a redelivery makes a new row.
    await old_inbox.admit(_envelope("second"))
    assert len(repository.load_deferred_messages_by_owner(OLD)) == 1


# -- review 2026-10-17: a refused admission and tasks this agent holds again ------


def test_a_new_request_refused_by_the_custody_check_is_admitted_on_redelivery(tmp_path):
    """The first save after the takeover is a new request: it was never saved here."""
    old = manager(tmp_path, OLD)
    admit(old, "work")
    manager(tmp_path, NEW).restore_runtime_state_for_owner_ids([OLD])
    released: list[tuple[str, ...]] = []
    old.set_custody_lost_listener(released.append)
    with pytest.raises(RuntimeCustodyLost):  # not a KeyError from the admission rollback
        admit(old, "next")
    assert released == [("work",)]
    admit(old, "next")  # the request is redelivered
    assert holders(old) == {NEW: ["work"], OLD: ["next"]}


def failing_agent(task_manager: TaskManager, monkeypatch) -> SimpleNamespace:
    agent = agent_for(task_manager, inbox_for(OLD, task_manager))
    agent._notify_context_after_turn = AsyncMock()
    for name in ("_conversation_store", "_conversation_compactor", "_conversation_artifact_offloader",
                 "_conversation_summary_generator"):
        setattr(agent, name, None)
    agent.team_id, agent.named_agent_type = "team", "ResearchAgent"
    monkeypatch.setattr("custom_agents.turn_failure_runtime.finalize_conversation_turn", AsyncMock())
    return agent


@pytest.mark.asyncio
async def test_a_readmitted_task_that_fails_gets_its_failure_reply(tmp_path, monkeypatch):
    old = manager(tmp_path, OLD)
    agent = failing_agent(old, monkeypatch)
    install_custody_listeners(agent, interrupt_turn=_interrupt_active_execution)
    await old.operations_runtime.run(partial(admit, old, "work"))
    manager(tmp_path, NEW).restore_runtime_state_for_owner_ids([OLD])
    with pytest.raises(RuntimeCustodyLost):
        await old.operations_runtime.run(partial(admit, old, "next"))
    await asyncio.sleep(0)
    assert agent._custody_release.released_task_ids == {"work"}

    await old.operations_runtime.run(partial(admit, old, "next"))
    await fail_task_after_turn_error(agent, "next", old.get_task_info("next"), RuntimeError("provider timed out"))
    agent._send_task_error_response.assert_awaited_once()
    assert old.get_task_from_db("next") is not None  # failed and archived here


@pytest.mark.asyncio
async def test_a_released_task_handed_back_to_this_agent_is_its_own_again(tmp_path, monkeypatch):
    old = manager(tmp_path, OLD)
    agent = failing_agent(old, monkeypatch)
    install_custody_listeners(agent, interrupt_turn=_interrupt_active_execution)
    agent._custody_release.released_task_ids.add("work")
    await old.operations_runtime.run(partial(admit, old, "work"))
    await fail_task_after_turn_error(agent, "work", old.get_task_info("work"), RuntimeError("tool failed"))
    agent._send_task_error_response.assert_awaited_once()
    assert agent._custody_release.released_task_ids == set()


@pytest.mark.asyncio
async def test_recording_a_failure_is_the_first_save_after_the_takeover(tmp_path, monkeypatch):
    """The owner does not know yet: failing the task finds its row gone, so nothing is sent."""
    old = manager(tmp_path, OLD)
    agent = failing_agent(old, monkeypatch)
    install_custody_listeners(agent, interrupt_turn=_interrupt_active_execution)
    await old.operations_runtime.run(partial(admit, old, "work"))
    info = old.get_task_info("work")
    manager(tmp_path, NEW).restore_runtime_state_for_owner_ids([OLD])
    await fail_task_after_turn_error(agent, "work", info, RuntimeError("provider timed out"))
    agent._send_task_error_response.assert_not_awaited()
    assert holders(old) == {NEW: ["work"]}


def test_only_a_refused_task_snapshot_save_counts_as_moved_custody():
    agent = SimpleNamespace(_custody_release=None)
    for store, moved in ((CustodyStore.TASK_RUNTIME, True), (CustodyStore.DEFERRED_MESSAGES, False)):
        try:
            try:
                raise RuntimeCustodyLost(OLD, store)
            except RuntimeCustodyLost as lost:
                raise RuntimeError("turn failed") from lost
        except RuntimeError as wrapped:
            assert custody_moved(agent, "work", wrapped) is moved
